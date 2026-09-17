"""Opt-in real pinned engine; only the labelled disposable local lifecycle."""

import asyncio
import importlib
import json
import logging
import math
import os
import signal
import socket
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.embedder.openai import OpenAIEmbedder
from redis.exceptions import ResponseError

from cairn.catalogue.audit import Classification, TrustClass
from cairn.projection.adapter import ProjectedFactState
from cairn.projection.fact_chunks import EmbeddedLocalFact, UnitSpan
from cairn.projection.fact_unit_index import FactUnitIndex, build_record, publish
from cairn.projection.fact_vectors import FactRepresentation, FactVectorError
from cairn.projection.graphiti import BoundedFalkorDriver
from cairn.projection.semantic_evidence import GRADE_LIMIT, local_representation_sha256

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_FACT_DB_TESTS") != "1",
    reason="owned disposable DB tests require opt-in",
)
GROUP = "b" * 64
IDENTITY = "11111111-1111-4111-8111-111111111111"
INVALID_HEX_STRINGS = (
    ("length-63", "a" * 63),
    ("length-65", "a" * 65),
    ("uppercase", "A" + "a" * 63),
    ("nonhex-ascii", "g" * 64),
    ("newline-64", "a" * 63 + "\n"),
    ("unicode-64", "é" * 64),
)


@pytest.fixture(scope="module")
def owned_db() -> Iterator[int]:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    launcher = importlib.import_module("evaluate_semantic_memory")
    image = launcher.falkordb_image((scripts.parent / "deploy/images.lock").read_text())
    previous_logging = logging.root.manager.disable
    logging.disable(
        logging.CRITICAL
    )  # Upstream query errors include vector parameters.
    previous_alarm = signal.getsignal(signal.SIGALRM)

    def expired(*args: Any) -> None:
        raise TimeoutError("owned_engine_test_deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(480)  # Reserve remaining invocation envelope for owned cleanup.
    owner = None
    try:
        with launcher.disposable_falkordb(image) as (owner, port):
            ids = {}
            for kind in ("container", "network"):
                args = [kind, "ls"] + (["--all"] if kind == "container" else [])
                ids[kind] = launcher.docker(
                    *args,
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"label={launcher.LABEL}={owner}",
                )
            print(
                json.dumps(
                    {
                        "engine_start": datetime.now(UTC).isoformat(),
                        "owner": owner,
                        "ids": ids,
                        "port": port,
                    }
                ),
                flush=True,
            )
            original_connect = socket.socket.connect

            def local_only(sock: socket.socket, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6) and address[:2] != (
                    "127.0.0.1",
                    port,
                ):
                    raise AssertionError("external_socket_forbidden")
                return original_connect(sock, address)

            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(socket.socket, "connect", local_only)
                yield port
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        logging.disable(previous_logging)
        sys.path.remove(str(scripts))
        if owner is not None:
            for kind in ("container", "network"):
                args = [kind, "ls"] + (["--all"] if kind == "container" else [])
                assert (
                    launcher.docker(
                        *args, "--quiet", "--filter", f"label={launcher.LABEL}={owner}"
                    )
                    == ""
                )
            print(
                json.dumps(
                    {
                        "engine_finish": datetime.now(UTC).isoformat(),
                        "owner": owner,
                        "owned_cleanup_verified": True,
                    }
                ),
                flush=True,
            )


def state() -> ProjectedFactState:
    return ProjectedFactState(
        UUID(IDENTITY),
        "acme\n[]",
        "a" * 31 + "\n" + "b" * 31 + "\n",
        "acme",
        (),
        Classification.INTERNAL,
        TrustClass.VALIDATED,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        None,
        None,
    )


def representation() -> FactRepresentation:
    return FactRepresentation(
        cast(
            OpenAIEmbedder,
            SimpleNamespace(
                config=SimpleNamespace(
                    embedding_model="text-embedding-3-small", embedding_dim=1024
                )
            ),
        )
    )


def values() -> EmbeddedLocalFact:
    return EmbeddedLocalFact(
        (UnitSpan(0, 32, 0), UnitSpan(32, 64, 1)),
        ([1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022),
        [1.0] + [0.0] * 1023,
    )


def numeric_vector(*components: int | float) -> list[int | float]:
    assert len(components) <= 1024
    return [*components] + [0] * (1024 - len(components))


def basis(index: int) -> list[float]:
    return [0.0] * index + [1.0] + [0.0] * (1023 - index)


def test_initial_engine_syntax_publication_and_coverage(owned_db: int) -> None:
    async def run() -> None:
        driver = BoundedFalkorDriver(
            host="127.0.0.1", port=owned_db, database=GROUP, concurrency_limit=2
        )
        failures = []
        try:
            # Diagnostics inspect synthetic primitive values only, never vectors.
            for name in ("typeOf", "valueType", "toJSON"):
                try:
                    rows, _, _ = await driver.execute_query(
                        f"RETURN {name}(1) AS integer, {name}(true) AS boolean, {name}([1]) AS array"
                    )
                    print(json.dumps({"type_probe": name, "result": rows}), flush=True)
                except Exception as error:
                    print(
                        json.dumps(
                            {"type_probe": name, "unsupported": type(error).__name__}
                        ),
                        flush=True,
                    )
            await driver.execute_query(
                "CREATE (:Episodic {uuid:$uuid, group_id:$group, source_description:'cairn.fact.projected'})",
                uuid=IDENTITY,
                group=GROUP,
            )
            try:
                await publish(driver, state(), GROUP, values(), representation())
                await publish(driver, state(), GROUP, values(), representation())
            except Exception as error:
                failures.append(
                    "publication:" + type(error).__name__ + ":" + str(error)[:180]
                )
                # Independent setup permits examining query syntax even if replacement failed.
                record = build_record(state(), GROUP, values())
                await driver.execute_query(
                    "MATCH (n) WHERE n:CairnFactChunk OR n:CairnFactChunkSet DETACH DELETE n"
                )
                await driver.execute_query(
                    "CREATE (h:CairnFactChunkSet) SET h=$header",
                    header=record["header"],
                )
                for child in record["children"]:
                    await driver.execute_query(
                        "MATCH (h:CairnFactChunkSet) CREATE (c:CairnFactChunk) SET c=$child CREATE (h)-[:CAIRN_UNIT]->(c)",
                        child=child,
                    )
            try:
                index = FactUnitIndex(representation())
                await index.preflight(driver, GROUP)
                count, hits = await index.search(driver, GROUP, [1.0] + [0.0] * 1023)
                assert count == 1 and hits[0][0] == IDENTITY
                assert await index.search(
                    driver, GROUP, [0.0, 0.0, 1.0] + [0.0] * 1021
                ) == (0, ())
                await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet)-[:CAIRN_UNIT]->(c:CairnFactChunk) WHERE c.slot=0 CREATE (h)-[:CAIRN_UNIT]->(c)"
                )
                with pytest.raises(FactVectorError):
                    await index.search(driver, GROUP, [1.0] + [0.0] * 1023)
            except Exception as error:
                failures.append(
                    "coverage:" + type(error).__name__ + ":" + str(error)[:180]
                )
            assert not failures, failures
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=90))


class ControlledEmbedder:
    config = SimpleNamespace(
        embedding_model="text-embedding-3-small", embedding_dim=1024
    )

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled_embedding_failure")
        return [
            ([0.0, 1.0] if text.startswith("b") else [1.0, 0.0]) + [0.0] * 1022
            for text in texts
        ]


def rep(embedder: ControlledEmbedder) -> FactRepresentation:
    return FactRepresentation(cast(OpenAIEmbedder, embedder))


async def reset(
    driver: BoundedFalkorDriver, fact: ProjectedFactState | None = None
) -> None:
    await driver.execute_query("MATCH (n) DETACH DELETE n")
    if fact is not None:
        await driver.execute_query(
            "CREATE (:Episodic {uuid:$uuid, group_id:$group, source_description:'cairn.fact.projected'})",
            uuid=str(fact.fact_id),
            group=GROUP,
        )


def driver_for(port: int) -> BoundedFalkorDriver:
    return BoundedFalkorDriver(
        host="127.0.0.1", port=port, database=GROUP, concurrency_limit=2
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "MATCH (h:CairnFactChunkSet) REMOVE h.fingerprint",
        "MATCH (h:CairnFactChunkSet) SET h.fingerprint='ABC'",
        "MATCH (h:CairnFactChunkSet) SET h.complete=1",
        "MATCH (h:CairnFactChunkSet) SET h.dim=1024.0",
        "MATCH (h:CairnFactChunkSet) SET h.body_bytes=64.0",
        "MATCH (h:CairnFactChunkSet) SET h.occurrence_count=2.0",
        "MATCH (h:CairnFactChunkSet) SET h.vector_count=2.0",
        "MATCH (h:CairnFactChunkSet) SET h.starts=[false,32]",
        "MATCH (h:CairnFactChunkSet) SET h.ends=[32.0,64]",
        "MATCH (h:CairnFactChunkSet) SET h.slots=[false,1]",
        "MATCH (h:CairnFactChunkSet) SET h.starts=[0,33]",
        "MATCH (h:CairnFactChunkSet) SET h.ends=[32,63]",
        "MATCH (h:CairnFactChunkSet) SET h.slots=[0,0]",
        "MATCH (h:CairnFactChunkSet) SET h.slots=[1,0]",
        "MATCH (h:CairnFactChunkSet) SET h.starts='not-an-array'",
        "MATCH (h:CairnFactChunkSet) SET h.vector_keys=['bad','bad']",
        "MATCH (c:CairnFactChunk) WHERE c.slot=1 SET c.slot=0",
        "MATCH (c:CairnFactChunk) WHERE c.slot=1 SET c.slot=1.0",
        "MATCH (c:CairnFactChunk) WHERE c.slot=0 SET c.slot=false",
        "MATCH (c:CairnFactChunk) SET c.input_bytes=32.0",
        "MATCH (c:CairnFactChunk) SET c.dim=1024.0",
        "MATCH (c:CairnFactChunk) SET c.embedding=[1.0]",
        "MATCH (c:CairnFactChunk) SET c.input_sha256='bad'",
        "MATCH (c:CairnFactChunk) SET c.group_id='other'",
        "MATCH (h:CairnFactChunkSet)-[:CAIRN_UNIT]->(c) WHERE c.slot=0 CREATE (h)-[:CAIRN_UNIT]->(c)",
        "MATCH (h:CairnFactChunkSet)-[r:CAIRN_UNIT]->(c) WHERE c.slot=0 DELETE r",
        "MATCH (h:CairnFactChunkSet) CREATE (x:CairnFactChunkSet) SET x=properties(h)",
        "MATCH (c:CairnFactChunk) WHERE c.slot=0 CREATE (x:CairnFactChunk) SET x=properties(c)",
        "embedding-int64-max",
        "embedding-int64-min",
        "embedding-negative-int64-max",
        "embedding-bool",
        "embedding-string",
        "embedding-missing-property-null",
        "embedding-positive-infinity",
        "embedding-negative-infinity",
        "embedding-float-max",
    ],
    ids=[
        "missing-fingerprint",
        "bad-fingerprint",
        "complete-int",
        "dim-float",
        "bytes-float",
        "occurrence-float",
        "vectors-float",
        "start-bool",
        "end-float",
        "slot-bool",
        "gap",
        "lost-tail",
        "unused-slot",
        "first-use-order",
        "wrong-array-type",
        "bad-keys",
        "duplicate-slot",
        "child-slot-float",
        "child-slot-bool",
        "child-bytes-float",
        "child-dim-float",
        "bad-vector",
        "bad-input-hash",
        "foreign-child",
        "duplicate-edge",
        "missing-edge",
        "duplicate-header",
        "orphan-child",
        "embedding-int64-max",
        "embedding-int64-min",
        "embedding-negative-int64-max",
        "embedding-bool",
        "embedding-string",
        "embedding-missing-property-null",
        "embedding-positive-infinity",
        "embedding-negative-infinity",
        "embedding-float-max",
    ],
)
def test_corruption_after_preflight_fails_even_zero_hits_and_redelivery_repairs(
    owned_db: int, mutation: str
) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        embedder = ControlledEmbedder()
        index = FactUnitIndex(rep(embedder))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            await index.preflight(driver, GROUP)
            embedding_cases: dict[str, tuple[list[Any], str]] = {
                "embedding-int64-max": ([2**63 - 1] + [0] * 1023, "integer"),
                "embedding-int64-min": ([-(2**63), 1] + [0] * 1022, "integer"),
                "embedding-negative-int64-max": (
                    [-(2**63 - 1)] + [0] * 1023,
                    "integer",
                ),
                "embedding-bool": ([True] + [0] * 1023, "boolean"),
                "embedding-string": (["invalid"] + [0] * 1023, "string"),
                "embedding-positive-infinity": (
                    [float("inf")] + [0] * 1023,
                    "float",
                ),
                "embedding-negative-infinity": (
                    [float("-inf")] + [0] * 1023,
                    "float",
                ),
                "embedding-float-max": (
                    [3.4028234663852886e38] + [0] * 1023,
                    "float",
                ),
            }
            engine_scalar_expressions = {
                "embedding-positive-infinity": "toFloat('Infinity')",
                "embedding-negative-infinity": "toFloat('-Infinity')",
            }
            # Falkor treats numerically equal SET values as unchanged, including
            # their old type. Remove first so these cases really store floats.
            float_fields = {
                "h.dim",
                "h.body_bytes",
                "h.occurrence_count",
                "h.vector_count",
                "h.ends",
                "c.slot",
                "c.input_bytes",
                "c.dim",
            }
            match, separator, assignment = mutation.partition(" SET ")
            field, _, literal = assignment.partition("=")
            if mutation == "embedding-missing-property-null":
                await driver.execute_query(
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 REMOVE c.embedding"
                )
                stored, _, _ = await driver.execute_query(
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "RETURN c.embedding IS NULL AS missing"
                )
                assert stored == [{"missing": True}]
            elif mutation in embedding_cases:
                embedding, expected_type = embedding_cases[mutation]
                if mutation in engine_scalar_expressions:
                    scalar = engine_scalar_expressions[mutation]
                    zeros = ",".join(["0"] * 1023)
                    await driver.execute_query(
                        "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                        f"SET c.embedding=[{scalar},{zeros}]"
                    )
                else:
                    await driver.execute_query(
                        "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                        "SET c.embedding=$embedding",
                        embedding=embedding,
                    )
                stored, _, _ = await driver.execute_query(
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "RETURN size(c.embedding) AS size, "
                    "typeOf(c.embedding[0]) AS first_type, "
                    "c.embedding[0] AS first, c.embedding[1] AS second"
                )
                assert len(stored) == 1
                row = stored[0]
                assert row["size"] == 1024
                assert row["first_type"].lower() in (
                    {"float", "double"} if expected_type == "float" else {expected_type}
                )
                assert row["second"] == embedding[1]
                if mutation in {
                    "embedding-positive-infinity",
                    "embedding-negative-infinity",
                }:
                    assert math.isinf(row["first"])
                    assert math.copysign(1.0, row["first"]) == math.copysign(
                        1.0, embedding[0]
                    )
                elif mutation == "embedding-float-max":
                    assert math.isfinite(row["first"])
                    assert row["first"] > 3.4e38
                    assert math.isclose(
                        row["first"], embedding[0], rel_tol=1e-14, abs_tol=0.0
                    )
                else:
                    assert row["first"] == embedding[0]
            elif separator and field in float_fields and ".0" in literal:
                alias, property_name = field.split(".")
                selected, _, _ = await driver.execute_query(
                    match + f" RETURN id({alias}) AS identity"
                )
                ids = [row["identity"] for row in selected]
                assert ids
                target = "MATCH (n) WHERE id(n) IN $ids"
                await driver.execute_query(
                    target + f" REMOVE n.{property_name}", ids=ids
                )
                await driver.execute_query(
                    target + f" SET n.{property_name}={literal}", ids=ids
                )
                expression = f"n.{property_name}" + ("[0]" if field == "h.ends" else "")
                stored, _, _ = await driver.execute_query(
                    target + f" RETURN typeOf({expression}) AS stored_type", ids=ids
                )
                assert len(stored) == len(ids)
                assert all(row["stored_type"] == "Float" for row in stored)
            else:
                await driver.execute_query(mutation)
            # Coverage must fail closed for a relevant fact and for a query
            # returning no hits, despite the successful preflight before corruption.
            for vector in ([1.0] + [0.0] * 1023, [0.0, 0.0, 1.0] + [0.0] * 1021):
                with pytest.raises((FactVectorError, ResponseError)):
                    await index.search(driver, GROUP, vector)
            # Independent search revalidation above must not rely on this
            # subsequent preflight to notice corruption introduced after it.
            with pytest.raises((FactVectorError, ResponseError)):
                await index.preflight(driver, GROUP)
            await index.ensure(driver, GROUP, [state()])
            await index.preflight(driver, GROUP)
            assert (await index.search(driver, GROUP, [1.0] + [0.0] * 1023))[0] == 1
            count = embedder.calls
            embedder.fail = True
            await index.ensure(driver, GROUP, [state()])
            assert embedder.calls == count
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


@pytest.mark.parametrize(
    ("site", "value"),
    [
        pytest.param(site, value, id=f"{site}-{case}")
        for site in ("header-fingerprint", "child-input-sha256", "child-key")
        for case, value in INVALID_HEX_STRINGS
    ]
    + [pytest.param("child-input-sha256", None, id="child-input-sha256-missing")],
)
def test_persisted_hex_corruption_is_rejected_after_preflight(
    owned_db: int,
    site: str,
    value: str | None,
) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            await index.preflight(driver, GROUP)
            if site == "header-fingerprint":
                assert value is not None
                await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet) SET h.fingerprint=$value",
                    value=value,
                )
                await driver.execute_query(
                    "MATCH (c:CairnFactChunk) SET c.fingerprint=$value",
                    value=value,
                )
                stored, _, _ = await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet) "
                    "MATCH (c:CairnFactChunk) "
                    "RETURN h.fingerprint AS header_value, "
                    "typeOf(h.fingerprint) AS header_type, "
                    "collect(c.fingerprint) AS child_values, "
                    "collect(typeOf(c.fingerprint)) AS child_types"
                )
                assert len(stored) == 1
                assert stored[0]["header_value"] == value
                assert stored[0]["header_type"] == "String"
                assert stored[0]["child_values"] == [value, value]
                assert stored[0]["child_types"] == ["String", "String"]
            elif site == "child-input-sha256":
                if value is None:
                    await driver.execute_query(
                        "MATCH (c:CairnFactChunk) WHERE c.slot=0 REMOVE c.input_sha256"
                    )
                else:
                    await driver.execute_query(
                        "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                        "SET c.input_sha256=$value",
                        value=value,
                    )
                stored, _, _ = await driver.execute_query(
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "RETURN c.input_sha256 AS value, "
                    "typeOf(c.input_sha256) AS value_type, "
                    "c.input_sha256 IS NULL AS missing"
                )
                assert len(stored) == 1
                assert stored[0]["value"] == value
                assert stored[0]["value_type"] == (
                    "Null" if value is None else "String"
                )
                assert stored[0]["missing"] is (value is None)
            else:
                assert site == "child-key" and value is not None
                selected, _, _ = await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet) RETURN h.vector_keys AS keys"
                )
                assert len(selected) == 1
                vector_keys = cast(list[str], selected[0]["keys"])
                assert len(vector_keys) == 2
                vector_keys[0] = value
                await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet) "
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "SET c.key=$value, h.vector_keys=$vector_keys",
                    value=value,
                    vector_keys=vector_keys,
                )
                stored, _, _ = await driver.execute_query(
                    "MATCH (h:CairnFactChunkSet) "
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "RETURN c.key AS child_value, typeOf(c.key) AS child_type, "
                    "h.vector_keys AS vector_keys, "
                    "typeOf(h.vector_keys[0]) AS binding_type"
                )
                assert len(stored) == 1
                assert stored[0]["child_value"] == value
                assert stored[0]["child_type"] == "String"
                assert stored[0]["vector_keys"] == vector_keys
                assert stored[0]["vector_keys"][0] == value
                assert stored[0]["binding_type"] == "String"
            # Search must independently revalidate corruption introduced after
            # the successful preflight, including when no fact would be returned.
            for query in (basis(0), basis(2)):
                with pytest.raises((FactVectorError, ResponseError)):
                    await index.search(driver, GROUP, query)
            with pytest.raises((FactVectorError, ResponseError)):
                await index.preflight(driver, GROUP)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_integer_unit_embedding_remains_valid_for_hit_and_zero_hit(
    owned_db: int,
) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            integer_unit = cast(list[float], [1] + [0] * 1023)
            integer_zero_hit = cast(list[float], [0, 0, 1] + [0] * 1021)
            await driver.execute_query("MATCH (c:CairnFactChunk) REMOVE c.embedding")
            await driver.execute_query(
                "MATCH (c:CairnFactChunk) SET c.embedding=$embedding",
                embedding=integer_unit,
            )
            stored, _, _ = await driver.execute_query(
                "MATCH (c:CairnFactChunk) "
                "RETURN collect(DISTINCT typeOf(c.embedding[0])) AS types, "
                "collect(DISTINCT size(c.embedding)) AS sizes"
            )
            assert stored == [{"types": ["Integer"], "sizes": [1024]}]
            await index.preflight(driver, GROUP)
            count, hits = await index.search(driver, GROUP, integer_unit)
            assert count == 1 and [identity for identity, _, _ in hits] == [IDENTITY]
            assert await index.search(driver, GROUP, integer_zero_hit) == (0, ())
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_nan_embedding_transport_rejection_preserves_healthy_projection(
    owned_db: int,
) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        snapshot_query = (
            "MATCH (c:CairnFactChunk) "
            "RETURN c.slot AS slot, size(c.embedding) AS size, "
            "typeOf(c.embedding[0]) AS first_type, c.embedding[0] AS first "
            "ORDER BY c.slot"
        )
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            await index.preflight(driver, GROUP)
            before, _, _ = await driver.execute_query(snapshot_query)
            with pytest.raises(
                ResponseError, match="Failed to parse query parameter 'embedding' value"
            ):
                await driver.execute_query(
                    "MATCH (c:CairnFactChunk) WHERE c.slot=0 "
                    "SET c.embedding=$embedding",
                    embedding=[float("nan")] + [0] * 1023,
                )
            after, _, _ = await driver.execute_query(snapshot_query)
            assert after == before
            await index.preflight(driver, GROUP)
            assert (await index.search(driver, GROUP, [1.0] + [0.0] * 1023))[0] == 1
            assert await index.search(
                driver, GROUP, [0.0, 0.0, 1.0] + [0.0] * 1021
            ) == (0, ())
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


@pytest.mark.parametrize(
    ("vector", "valid", "orthogonal"),
    [
        (numeric_vector(0, 0.6, 0.8), True, basis(0)),
        (numeric_vector(-1), True, basis(1)),
        (numeric_vector(0, -0.6, 0.8), True, basis(0)),
        ([1.0 / 32.0] * 1024, True, numeric_vector(2**-0.5, -(2**-0.5))),
        (numeric_vector(1.0 - 9e-6), True, basis(1)),
        (numeric_vector(1.0 + 9e-6), True, basis(1)),
        (numeric_vector(), False, None),
        (numeric_vector(1.0 - 11e-6), False, None),
        (numeric_vector(1.0 + 11e-6), False, None),
        (numeric_vector(0.5), False, None),
        (numeric_vector(2.0), False, None),
    ],
    ids=[
        "mixed-positive-unit",
        "negative-integer-unit",
        "negative-mixed-unit",
        "dense-float-unit",
        "tolerance-low-inside",
        "tolerance-high-inside",
        "zero-vector",
        "tolerance-low-outside",
        "tolerance-high-outside",
        "finite-nonunit-low",
        "finite-nonunit-high",
    ],
)
def test_persisted_numeric_vector_norm_semantics(
    owned_db: int,
    vector: list[int | float],
    valid: bool,
    orthogonal: list[int | float] | None,
) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            await driver.execute_query("MATCH (c:CairnFactChunk) REMOVE c.embedding")
            await driver.execute_query(
                "MATCH (c:CairnFactChunk) SET c.embedding=$embedding",
                embedding=vector,
            )
            stored, _, _ = await driver.execute_query(
                "MATCH (c:CairnFactChunk) "
                "RETURN c.slot AS slot, c.embedding AS embedding ORDER BY c.slot"
            )
            assert [row["slot"] for row in stored] == [0, 1]
            expected_magnitude = math.sqrt(
                math.fsum(float(value) * float(value) for value in vector)
            )
            for row in stored:
                stored_vector = cast(list[int | float], row["embedding"])
                assert len(stored_vector) == 1024
                assert [type(value) for value in stored_vector] == [
                    type(value) for value in vector
                ]
                for actual, expected in zip(stored_vector, vector, strict=True):
                    if type(expected) is int:
                        assert actual == expected
                    else:
                        assert math.isclose(
                            actual, expected, rel_tol=1e-14, abs_tol=1e-15
                        )
                stored_magnitude = math.sqrt(
                    math.fsum(float(value) * float(value) for value in stored_vector)
                )
                assert math.isclose(
                    stored_magnitude,
                    expected_magnitude,
                    rel_tol=1e-14,
                    abs_tol=1e-15,
                )
            if valid:
                assert orthogonal is not None
                await index.preflight(driver, GROUP)
                count, hits = await index.search(
                    driver, GROUP, cast(list[float], vector)
                )
                assert count == 1
                assert [identity for identity, _, _ in hits] == [IDENTITY]
                assert await index.search(
                    driver, GROUP, cast(list[float], orthogonal)
                ) == (0, ())
            else:
                with pytest.raises((FactVectorError, ResponseError)):
                    await index.preflight(driver, GROUP)
                for query in (basis(0), basis(2)):
                    with pytest.raises((FactVectorError, ResponseError)):
                        await index.search(driver, GROUP, query)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_empty_and_missing_coverage(
    owned_db: int,
) -> None:
    from cairn.projection import fact_unit_index as module

    async def run() -> None:
        driver = driver_for(owned_db)
        embedder = ControlledEmbedder()
        index = FactUnitIndex(rep(embedder))
        try:
            await reset(driver)
            rows, _, _ = await driver.execute_query(
                module._COVERAGE_ONLY,
                group_id=GROUP,
                representation=local_representation_sha256(),
            )
            assert rows == [{"coverage_valid": True}]
            assert await cast(Any, index).preflight(driver, GROUP) is None
            assert await index.search(driver, GROUP, [1.0] + [0.0] * 1023) == (0, ())
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            rows, _, _ = await driver.execute_query(
                module._COVERAGE_ONLY,
                group_id=GROUP,
                representation=local_representation_sha256(),
            )
            assert rows == [{"coverage_valid": True}]
            assert await cast(Any, index).preflight(driver, GROUP) is None
            fact = replace(state(), body="a\n" * 32768)
            await reset(driver, fact)
            rows, _, _ = await driver.execute_query(
                module._SEARCH,
                group_id=GROUP,
                representation=local_representation_sha256(),
                scoring=False,
                query_vector=[1.0] + [0.0] * 1023,
                cutoff=module.LOCAL_CUTOFF,
                limit=GRADE_LIMIT,
            )
            assert rows == [
                {"coverage_valid": False, "eligible_count": 0, "grades": []}
            ]
            rows, _, _ = await driver.execute_query(
                module._COVERAGE_ONLY,
                group_id=GROUP,
                representation=local_representation_sha256(),
            )
            assert rows == [{"coverage_valid": False}]
            with pytest.raises(FactVectorError):
                await index.preflight(driver, GROUP)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=30))


def test_exact_dedup_maximum_manifest(owned_db: int) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            fact = replace(state(), body="a\n" * 32768)
            await reset(driver, fact)
            await index.ensure(driver, GROUP, [fact])
            rows, _, _ = await driver.execute_query(
                "MATCH (h:CairnFactChunkSet)-[:CAIRN_UNIT]->(c) RETURN h.occurrence_count AS occurrences, count(c) AS vectors"
            )
            assert rows == [{"occurrences": 2048, "vectors": 1}]
            await index.preflight(driver, GROUP)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=30))


def test_exact_maximum_distinct_manifest_search_and_reuse(owned_db: int) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        embedder = ControlledEmbedder()
        index = FactUnitIndex(rep(embedder))
        fact = replace(state(), body="".join(f"{i:030d}.\n" for i in range(2048)))
        try:
            await reset(driver, fact)
            await index.ensure(driver, GROUP, [fact])
            rows, _, _ = await driver.execute_query(
                "MATCH (h:CairnFactChunkSet)-[:CAIRN_UNIT]->(c) "
                "RETURN h.occurrence_count AS occurrences, h.vector_count AS vectors, count(c) AS children"
            )
            assert rows == [{"occurrences": 2048, "vectors": 2048, "children": 2048}]
            started = time.monotonic()
            await index.preflight(driver, GROUP)
            print(
                json.dumps(
                    {"maximum_distinct_preflight_seconds": time.monotonic() - started}
                ),
                flush=True,
            )
            assert (await index.search(driver, GROUP, [1.0] + [0.0] * 1023))[0] == 1
            before = embedder.calls
            embedder.fail = True
            await index.ensure(driver, GROUP, [fact])
            assert embedder.calls == before
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=90))


def test_reuse_and_coverage_counts_have_exact_integer_transport(owned_db: int) -> None:
    from cairn.projection import fact_unit_index as module

    async def run() -> None:
        driver = driver_for(owned_db)
        embedder = ControlledEmbedder()
        index = FactUnitIndex(rep(embedder))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            rows, _, _ = await driver.execute_query(module._REUSE, uuid=IDENTITY)
            assert index._reusable(rows, state(), GROUP)
            assert type(rows[0]["outgoing"]) is int
            for counts in rows[0]["relationships"]:
                assert type(counts["total"]) is int and counts["total"] == 1
                assert type(counts["units"]) is int and counts["units"] == 1
            coverage, _, _ = await driver.execute_query(
                module._GATHER + " RETURN outgoing, relationships"
            )
            assert type(coverage[0]["outgoing"]) is int
            for counts in coverage[0]["relationships"]:
                assert type(counts["total"]) is int and counts["total"] == 1
                assert type(counts["units"]) is int and counts["units"] == 1
            before = embedder.calls
            embedder.fail = True
            await index.ensure(driver, GROUP, [state()])
            assert embedder.calls == before
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


@pytest.mark.parametrize("field", ["occurrence_count", "vector_count", "body_bytes"])
@pytest.mark.parametrize("value", [None, True, "invalid", -1, 0, 2049, 2.0])
def test_invalid_count_operands_return_coverage_false(
    owned_db: int, field: str, value: Any
) -> None:
    from cairn.projection import fact_unit_index as module

    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            await reset(driver, state())
            await index.ensure(driver, GROUP, [state()])
            await driver.execute_query(f"MATCH (h:CairnFactChunkSet) REMOVE h.{field}")
            if value is not None:
                await driver.execute_query(
                    f"MATCH (h:CairnFactChunkSet) SET h.{field}=$value", value=value
                )
            for scoring in (False, True):
                rows, _, _ = await driver.execute_query(
                    module._SEARCH,
                    group_id=GROUP,
                    representation=local_representation_sha256(),
                    scoring=scoring,
                    query_vector=[1.0] + [0.0] * 1023,
                    cutoff=module.LOCAL_CUTOFF,
                    limit=GRADE_LIMIT,
                )
                assert rows == [
                    {"coverage_valid": False, "eligible_count": 0, "grades": []}
                ]
            with pytest.raises(FactVectorError):
                await index.preflight(driver, GROUP)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_fact_limit_and_full_coverage_before_topk(owned_db: int) -> None:
    async def run() -> None:
        driver = driver_for(owned_db)
        index = FactUnitIndex(rep(ControlledEmbedder()))
        try:
            await reset(driver)
            for i in range(1, 258):
                fact = replace(state(), fact_id=UUID(int=i))
                await driver.execute_query(
                    "CREATE (:Episodic {uuid:$uuid,group_id:$group,source_description:'cairn.fact.projected'})",
                    uuid=str(fact.fact_id),
                    group=GROUP,
                )
                await publish(driver, fact, GROUP, values(), representation())
            count, hits = await index.search(driver, GROUP, [1.0] + [0.0] * 1023)
            assert count == 257 and len(hits) == 256
            assert [identity for identity, _, _ in hits] == [
                str(UUID(int=i)) for i in range(1, 257)
            ]
            await index.preflight(driver, GROUP)
            await driver.execute_query(
                "MATCH (h:CairnFactChunkSet {uuid:$uuid}) REMOVE h.fingerprint",
                uuid=str(UUID(int=257)),
            )
            with pytest.raises(FactVectorError):
                await index.search(driver, GROUP, [1.0] + [0.0] * 1023)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=90))


@pytest.fixture
def adapter(owned_db: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from cairn.projection import graphiti

    embedder = ControlledEmbedder()

    class Client:
        async def close(self) -> None:
            pass

    class Graph:
        def __init__(self, driver: Any) -> None:
            self.embedder = embedder
            self.extractions = 0
            self.omit = False
            self.clients = SimpleNamespace(
                driver=driver, embedder=embedder, cross_encoder=None, tracer=None
            )

        async def add_episode(self, **kwargs: Any) -> None:
            self.extractions += 1

        async def add_episode_bulk(self, raw: Any, group_id: str) -> Any:
            self.extractions += len(raw)
            return SimpleNamespace(
                episodes=[]
                if self.omit
                else [SimpleNamespace(uuid=e.uuid) for e in raw]
            )

        search_with_vector = graphiti._CairnGraphiti.search_with_vector

    monkeypatch.setattr(
        graphiti, "_openai_provider_clients", lambda: (Client(), None, embedder, None)
    )
    monkeypatch.setattr(
        graphiti, "_construct_graphiti", lambda driver, *args, **kwargs: Graph(driver)
    )
    index = graphiti.GraphitiIndex(
        host="127.0.0.1", port=owned_db, index_concurrency_limit=2
    )
    try:
        index.clear(None)
        yield index
    except BaseException as primary:
        try:
            index.close()
        except BaseException as cleanup:
            raise BaseExceptionGroup(
                "test and index cleanup failed", [primary, cleanup]
            ) from None
        raise
    else:
        index.close()


def test_actual_adapter_relation_free_recall_and_blank_query(adapter: Any) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.semantic_evidence import local_body_fingerprint

    assert adapter.project(state()) == FactProjected()
    assert adapter.memory_evidence_source() is adapter
    embedder = adapter._graphiti.embedder
    before = embedder.calls
    result = adapter.search_with_evidence("entry", 1, (state().partition_key,))
    assert result.partitions[0].grades[0].fact_id == state().fact_id
    assert result.partitions[0].grades[0].fingerprint == local_body_fingerprint(
        state().body
    )
    assert state().fact_id in result.candidate_ids
    assert embedder.calls == before + 1
    before = embedder.calls
    assert (
        adapter.search_with_evidence(" \n", 1, (state().partition_key,)).partitions
        == ()
    )
    assert embedder.calls == before


def test_actual_adapter_retry_bulk_and_exact_pool_parity(adapter: Any) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.graphiti import GraphitiIndexError
    from cairn.projection.partition import derive_group_id

    embedder = adapter._graphiti.embedder
    embedder.fail = True
    with pytest.raises(GraphitiIndexError):
        adapter.project(state())
    assert adapter._graphiti.extractions == 1
    embedder.fail = False
    assert adapter.project(state()) == FactProjected()
    assert adapter._graphiti.extractions == 1
    count = embedder.calls
    assert adapter.project(state()) == FactProjected()
    assert embedder.calls == count
    second = replace(state(), fact_id=UUID(int=2), body=("e\u0301🙂漢; " * 300))
    third = replace(second, fact_id=UUID(int=3))
    assert adapter.project_many((second, third)) == (FactProjected(), FactProjected())

    async def read() -> Any:
        from cairn.projection.fact_vectors import _ready

        driver = adapter._driver.clone(database=derive_group_id(state().partition_key))
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            "MATCH (v:CairnFactVector) WHERE v.uuid IN $ids RETURN v.uuid AS uuid, v.embedding AS embedding ORDER BY v.uuid",
            ids=[str(second.fact_id), str(third.fact_id)],
        )
        expected = await rep(ControlledEmbedder()).embed_text(second.body)
        assert [row["embedding"] for row in rows] == [expected, expected]

    adapter._call(read())


def test_actual_two_populated_partitions_seventeen_fanout_and_scoped_clear(
    adapter: Any,
) -> None:
    from cairn.projection.semantic_evidence import SemanticEvidenceError

    parts = ("acme\n[]",) + tuple(
        'acme\n[{"kind":"repo","id":"r' + str(i) + '"}]' for i in range(16)
    )
    adapter.project(state())
    other = replace(state(), fact_id=UUID(int=2), partition_key=parts[-1])
    adapter.project(other)
    before = adapter._graphiti.embedder.calls
    result = adapter.search_with_evidence("entry", 1, parts)
    assert adapter._graphiti.embedder.calls == before + 1
    assert len(result.partitions) == 17
    assert {g.fact_id for p in result.partitions for g in p.grades} == {
        state().fact_id,
        other.fact_id,
    }
    adapter.clear((parts[-1],))
    result = adapter.search_with_evidence("entry", 1, parts)
    assert {g.fact_id for p in result.partitions for g in p.grades} == {state().fact_id}
    with pytest.raises(SemanticEvidenceError):
        adapter.search_with_evidence("entry", 1, parts + ("acme\n[]",))


def test_lost_publication_response_is_reused_without_second_embedding(
    owned_db: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.projection import fact_unit_index as module

    async def run() -> None:
        driver = driver_for(owned_db)
        embedder = ControlledEmbedder()
        index = FactUnitIndex(rep(embedder))
        original = driver.execute_query

        async def uncertain(query: str, **kwargs: Any) -> Any:
            response = await original(query, **kwargs)
            if query == module._PUBLISH:
                raise RuntimeError("controlled_response_loss")
            return response

        try:
            await reset(driver, state())
            monkeypatch.setattr(driver, "execute_query", uncertain)
            with pytest.raises(RuntimeError, match="controlled_response_loss"):
                await index.ensure(driver, GROUP, [state()])
            monkeypatch.setattr(driver, "execute_query", original)
            calls = embedder.calls
            embedder.fail = True
            await index.ensure(driver, GROUP, [state()])
            assert embedder.calls == calls
            await index.preflight(driver, GROUP)
        finally:
            await driver.close()

    asyncio.run(asyncio.wait_for(run(), timeout=30))
