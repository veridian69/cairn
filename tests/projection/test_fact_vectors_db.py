"""Opt-in pinned Falkor tests. Only the labelled disposable evaluator lifecycle.

CAIRN_FACT_DB_TESTS=1 enables this file. Embeddings are deterministic, no provider
client or credential is constructed. Never point these tests at an existing DB.
"""

import asyncio
import importlib
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from graphiti_core.embedder.openai import OpenAIEmbedder

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_FACT_DB_TESTS") != "1",
    reason="owned disposable DB tests require opt-in",
)
GROUP = "a" * 64
IDENTITY = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def owned_db() -> Iterator[int]:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        launcher = importlib.import_module("evaluate_semantic_memory")
        image = launcher.falkordb_image(
            (scripts.parent / "deploy/images.lock").read_text()
        )
        with launcher.disposable_falkordb(image) as (_, port):
            yield port
    finally:
        sys.path.remove(str(scripts))


class Embedder:
    config = SimpleNamespace(embedding_model="text-embedding-3-small", embedding_dim=2)

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.inputs: list[list[str]] = []

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.inputs.append(texts)
        if self.fail:
            raise RuntimeError("controlled failure")
        return [[1.0] + [0.0] * (self.config.embedding_dim - 1) for _ in texts]

    async def create(self, input_data: Any) -> list[float]:
        raise AssertionError("fact/query embeddings must use create_batch")


async def episode(driver: Any, identity: str = IDENTITY) -> None:
    from graphiti_core.nodes import EpisodeType, EpisodicNode

    now = datetime(2024, 1, 1, tzinfo=UTC)
    await EpisodicNode(
        uuid=identity,
        group_id=GROUP,
        name="synthetic",
        source=EpisodeType.text,
        source_description="cairn.fact.projected",
        content="synthetic fact",
        created_at=now,
        valid_at=now,
    ).save(driver)


def test_same_statement_reports_empty_and_missing_coverage(owned_db: int) -> None:
    from cairn.projection.fact_vectors import (
        FactRepresentation,
        FactVectorError,
        FactVectorIndex,
    )
    from cairn.projection.graphiti import BoundedFalkorDriver

    async def run() -> None:
        driver = BoundedFalkorDriver(
            host="127.0.0.1", port=owned_db, database=GROUP, concurrency_limit=2
        )
        try:
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            index = FactVectorIndex(
                FactRepresentation(cast(OpenAIEmbedder, Embedder()))
            )
            await index.preflight(driver, GROUP)
            assert await index.search(driver, GROUP, [1.0, 0.0], 1) == []
            await episode(driver)
            with pytest.raises(FactVectorError, match="fact_vector_rebuild_required"):
                await index.search(driver, GROUP, [1.0, 0.0], 1)
        finally:
            await driver.close()

    asyncio.run(run())


@pytest.fixture
def adapter(owned_db: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:

    from cairn.projection import graphiti

    embedder = Embedder()

    class Client:
        async def close(self) -> None:
            pass

    class ControlledGraphiti:
        def __init__(self, driver: Any) -> None:
            self.embedder = embedder
            self.extractions = 0
            self.omit = False
            self.clients: Any = SimpleNamespace(
                driver=driver, embedder=embedder, cross_encoder=None, tracer=None
            )

        async def add_episode(self, **kwargs: Any) -> None:
            self.extractions += 1

        async def add_episode_bulk(self, raw: Any, group_id: str) -> Any:
            self.extractions += len(raw)
            return SimpleNamespace(
                episodes=[]
                if self.omit
                else [SimpleNamespace(uuid=entry.uuid) for entry in raw]
            )

        # Execute the production pinned seam, not a second copy of it.
        search_with_vector = graphiti._CairnGraphiti.search_with_vector

    monkeypatch.setattr(
        graphiti, "_openai_provider_clients", lambda: (Client(), None, embedder, None)
    )
    monkeypatch.setattr(
        graphiti,
        "_construct_graphiti",
        lambda driver, *args, **kwargs: ControlledGraphiti(driver),
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


def state(identity: str = IDENTITY, partition: str = "synthetic\n[]") -> Any:
    from cairn.catalogue.audit import Classification, TrustClass
    from cairn.projection.adapter import ProjectedFactState

    return ProjectedFactState(
        UUID(identity),
        partition,
        "The warehouse entrance requires two separate approvals.",
        "synthetic",
        (),
        Classification.INTERNAL,
        TrustClass.CANDIDATE,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        None,
        None,
    )


def test_relation_free_fact_recalled_with_one_embedding_across_partitions(
    adapter: Any,
) -> None:
    from cairn.projection.adapter import FactProjected

    assert adapter.project(state()) == FactProjected()
    before = adapter._graphiti.embedder.calls
    result = adapter.search(
        "How many authorisations let someone enter storage?",
        10,
        ("synthetic\n[]", "empty\n[]"),
    )
    assert result == (UUID(IDENTITY),)
    assert adapter._graphiti.embedder.calls - before == 1


def test_embedding_failure_retry_keeps_completed_extraction(adapter: Any) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.graphiti import GraphitiIndexError

    adapter._graphiti.embedder.fail = True
    with pytest.raises(GraphitiIndexError):
        adapter.project(state())
    assert adapter._graphiti.extractions == 1
    adapter._graphiti.embedder.fail = False
    assert adapter.project(state()) == FactProjected()
    assert adapter._graphiti.extractions == 1
    before = adapter._graphiti.embedder.calls
    assert adapter.project(state()) == FactProjected()
    assert adapter._graphiti.embedder.calls == before


def test_blank_legacy_query_does_not_certify_coverage(adapter: Any) -> None:
    from cairn.projection.graphiti import GraphitiIndexError
    from cairn.projection.partition import derive_group_id

    adapter.project(state())
    driver = adapter._driver.clone(database=derive_group_id("synthetic\n[]"))
    adapter._call(driver.execute_query("MATCH (v:CairnFactVector) DELETE v"))
    before = adapter._graphiti.embedder.calls
    assert adapter.search(" \n\t", 1, ("synthetic\n[]",)) == ()
    with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
        adapter.search("nonblank", 1, ("synthetic\n[]",))
    assert adapter._graphiti.embedder.calls == before


@pytest.mark.parametrize(
    "mutation",
    [
        "v.dim=99",
        "v.representation='old'",
        "v.embedding=[0.0,0.0]",
        "v.embedding=[1.0]",
    ],
)
def test_malformed_coverage_is_detected_and_redelivery_repairs(
    adapter: Any, mutation: str
) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.graphiti import GraphitiIndexError
    from cairn.projection.partition import derive_group_id

    adapter.project_many((state(), state(OTHER)))
    driver = adapter._driver.clone(database=derive_group_id("synthetic\n[]"))
    adapter._call(
        driver.execute_query(
            "MATCH (v:CairnFactVector {uuid:$uuid}) SET " + mutation, uuid=OTHER
        )
    )
    with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
        adapter.search("nonblank", 1, ("synthetic\n[]",))
    assert adapter.project(state(OTHER)) == FactProjected()
    assert adapter.search("nonblank", 2, ("synthetic\n[]",)) == (
        UUID(IDENTITY),
        UUID(OTHER),
    )


def test_bulk_projection_and_interrupted_extraction(adapter: Any) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.graphiti import GraphitiIndexError

    adapter._graphiti.omit = True
    with pytest.raises(GraphitiIndexError):
        adapter.project_many((state(), state(OTHER)))
    assert adapter._graphiti.embedder.calls == 0
    adapter._graphiti.omit = False
    assert adapter.project_many((state(), state(OTHER))) == (
        FactProjected(),
        FactProjected(),
    )
    assert adapter._graphiti.embedder.calls == 1
    assert adapter.search("authorisations storage", 10, ("synthetic\n[]",)) == (
        UUID(IDENTITY),
        UUID(OTHER),
    )


@pytest.mark.parametrize(
    "fingerprint",
    [
        None,
        "stale-other-body",
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "a" * 63 + "\n",
        17,
        ["a" * 64],
        True,
    ],
    ids=[
        "missing",
        "malformed",
        "empty",
        "short",
        "long",
        "uppercase",
        "nonhex",
        "newline",
        "integer",
        "array",
        "boolean",
    ],
)
def test_fingerprint_coverage_rejects_noncanonical_and_redelivery_repairs(
    adapter: Any,
    fingerprint: Any,
) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.graphiti import GraphitiIndexError
    from cairn.projection.partition import derive_group_id

    adapter.project_many((state(), state(OTHER)))
    driver = adapter._driver.clone(database=derive_group_id("synthetic\n[]"))
    adapter._call(
        driver.execute_query(
            "MATCH (v:CairnFactVector {uuid:$uuid}) SET v.fingerprint=$fingerprint",
            uuid=OTHER,
            fingerprint=fingerprint,
        )
    )
    before = adapter._graphiti.embedder.calls
    extractions = adapter._graphiti.extractions
    with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
        adapter.search("nonblank", 1, ("synthetic\n[]",))
    assert adapter._graphiti.embedder.calls == before
    assert adapter.project(state(OTHER)) == FactProjected()
    assert adapter._graphiti.extractions == extractions
    assert adapter._graphiti.embedder.calls == before + 1
    assert adapter.search("nonblank", 2, ("synthetic\n[]",)) == (
        UUID(IDENTITY),
        UUID(OTHER),
    )


@pytest.mark.parametrize(
    "fingerprint", [None, "malformed"], ids=["missing", "malformed"]
)
@pytest.mark.parametrize("zero_hits", [False, True], ids=["outside-top-k", "zero-hits"])
def test_fingerprint_mutation_after_preflight_discards_all_candidates(
    adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: Any,
    zero_hits: bool,
) -> None:
    from cairn.projection.graphiti import GraphitiIndexError
    from cairn.projection.partition import derive_group_id

    adapter.project(state())
    adapter.project(state(OTHER, "later\n[]"))
    adapter.project(state("00000000-0000-4000-8000-000000000001", "later\n[]"))
    original = adapter._graphiti.search_with_vector
    later = derive_group_id("later\n[]")

    async def mutate(query: str, config: Any, **kwargs: Any) -> Any:
        result = await original(query, config, **kwargs)
        if kwargs["group_ids"] == [later]:
            await kwargs["driver"].execute_query(
                "MATCH (v:CairnFactVector {uuid:$uuid}) SET v.fingerprint=$fingerprint",
                uuid=OTHER,
                fingerprint=fingerprint,
            )
        return result

    async def query_embedding(texts: list[str]) -> list[list[float]]:
        return [[-1.0, 0.0] for _ in texts]

    monkeypatch.setattr(adapter._graphiti, "search_with_vector", mutate)
    if zero_hits:
        monkeypatch.setattr(adapter._graphiti.embedder, "create_batch", query_embedding)
    with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
        adapter.search("nonblank", 1, ("synthetic\n[]", "later\n[]"))


def test_two_populated_partitions_remain_isolated_and_clear_is_scoped(
    adapter: Any,
) -> None:
    adapter.project(state())
    adapter.project(state(OTHER, "second\n[]"))
    assert adapter.search("unrelated", 1, ("synthetic\n[]",)) == (UUID(IDENTITY),)
    assert adapter.search("unrelated", 1, ("second\n[]",)) == (UUID(OTHER),)
    assert adapter.search("unrelated", 1, ("synthetic\n[]", "second\n[]")) == (
        UUID(IDENTITY),
        UUID(OTHER),
    )
    adapter.clear(("synthetic\n[]",))
    assert adapter.search("unrelated", 1, ("synthetic\n[]", "second\n[]")) == (
        UUID(OTHER),
    )
    adapter.project(state())
    assert adapter.search("unrelated", 1, ("synthetic\n[]",)) == (UUID(IDENTITY),)
    adapter.clear(None)
    assert adapter.search("unrelated", 1, ("synthetic\n[]", "second\n[]")) == ()


def test_valid_vector_survives_episode_property_replacement(adapter: Any) -> None:
    from cairn.projection.partition import derive_group_id

    adapter.project(state())
    before = adapter._graphiti.embedder.calls
    driver = adapter._driver.clone(database=derive_group_id("synthetic\n[]"))
    adapter._call(
        driver.execute_query(
            "MATCH (e:Episodic) SET e.source_description='cairn.fact.pending'"
        )
    )
    adapter.project(state())
    assert adapter._graphiti.extractions == 2
    assert adapter._graphiti.embedder.calls == before
    assert adapter.search("unrelated", 1, ("synthetic\n[]",)) == (UUID(IDENTITY),)


def test_failed_second_vector_write_preserves_first_and_retry_only_missing(
    adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.projection.graphiti import BoundedFalkorDriver, GraphitiIndexError

    original = BoundedFalkorDriver.execute_query

    async def fail_second(self: Any, query: str, **kwargs: Any) -> Any:
        if query.startswith("MERGE (v:CairnFactVector") and kwargs.get("uuid") == OTHER:
            raise RuntimeError("controlled write failure")
        return await original(self, query, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(BoundedFalkorDriver, "execute_query", fail_second)
        with pytest.raises(GraphitiIndexError):
            adapter.project_many((state(), state(OTHER)))
    before = adapter._graphiti.embedder.calls
    adapter.project_many((state(), state(OTHER)))
    assert adapter._graphiti.extractions == 2
    assert adapter._graphiti.embedder.calls == before + 1
    assert len(adapter._graphiti.embedder.inputs[-1]) == 1
    assert adapter.search("unrelated", 2, ("synthetic\n[]",)) == (
        UUID(IDENTITY),
        UUID(OTHER),
    )


def test_max_query_reuses_one_batch_across_seventeen_partitions(adapter: Any) -> None:
    adapter.project(state())
    query = "q\n" * 4096
    before = adapter._graphiti.embedder.calls
    partitions = ("synthetic\n[]",) + tuple(f"empty-{i}\n[]" for i in range(16))
    assert adapter.search(query, 1, partitions) == (UUID(IDENTITY),)
    assert adapter._graphiti.embedder.calls == before + 1
    assert len(adapter._graphiti.embedder.inputs[-1]) == 4
    assert "".join(adapter._graphiti.embedder.inputs[-1]) == query.replace("\n", " ")


def test_single_and_bulk_unicode_vectors_are_identical(adapter: Any) -> None:
    from cairn.projection.partition import derive_group_id

    body = "🙂e\u0301漢<|endoftext|>\r\n" * 2000
    adapter.project(replace(state(), body=body))
    adapter.project_many((replace(state(OTHER), body=body),))
    driver = adapter._driver.clone(database=derive_group_id("synthetic\n[]"))
    rows, _, _ = adapter._call(
        driver.execute_query(
            "MATCH (v:CairnFactVector) RETURN v.embedding AS vector ORDER BY v.uuid"
        )
    )
    assert len(rows) == 2
    assert rows[0]["vector"] == rows[1]["vector"]


def test_real_index_rebuild_keeps_pending_work_and_historical_visibility(
    adapter: Any, tmp_path: Path
) -> None:
    # Reuse the existing bounded synthetic catalogue fixture; no evaluator
    # corpus, held-out queries or credentials are loaded.
    fixture = importlib.import_module("test_rebuild")
    from cairn.projection.delivery import deliver_projection_outbox
    from cairn.projection.rebuild import rebuild_index

    fixture._seed(fixture._config(tmp_path))
    old = fixture._ingest(tmp_path, "first synthetic fact", now=fixture.NOW)
    new = fixture._ingest(
        tmp_path, "second synthetic fact", now=fixture.NOW + timedelta(minutes=1)
    )
    fixture._invalidate(tmp_path, old, now=fixture.NOW + timedelta(minutes=2))
    transactions = fixture._transactions(tmp_path)
    report = rebuild_index(tmp_path, transactions, adapter, uuid_factory=uuid4)
    assert report.projected == 2 and report.failed == 0
    assert fixture._depth(tmp_path) == 0
    adapter._graphiti.embedder.fail = True
    report = rebuild_index(tmp_path, transactions, adapter, uuid_factory=uuid4)
    assert report.projected == 0 and report.failed == 2
    assert fixture._depth(tmp_path) == 2
    adapter._graphiti.embedder.fail = False
    deliver_projection_outbox(
        transactions, adapter, clock=lambda: fixture.NOW, limit=10
    )
    assert fixture._depth(tmp_path) == 0
    now = fixture.NOW + timedelta(hours=1)
    current = fixture._retrieve(tmp_path, adapter, query="unrelated", now=now)
    historical = fixture._retrieve(
        tmp_path, adapter, query="unrelated", as_of=fixture.NOW, now=now
    )
    assert [hit.fact_id for hit in current.hits] == [new]
    assert [hit.fact_id for hit in historical.hits] == [old]


@pytest.mark.parametrize("dimension", [2, 1024])
def test_actual_float32_cosine_cutoff_and_ties(
    owned_db: int, monkeypatch: pytest.MonkeyPatch, dimension: int
) -> None:
    from cairn.projection import fact_vectors
    from cairn.projection.graphiti import BoundedFalkorDriver

    async def run() -> None:
        driver = BoundedFalkorDriver(
            host="127.0.0.1", port=owned_db, database=GROUP, concurrency_limit=2
        )
        try:
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            embedder = Embedder()
            embedder.config = SimpleNamespace(
                embedding_model="text-embedding-3-small", embedding_dim=dimension
            )
            index = fact_vectors.FactVectorIndex(
                fact_vectors.FactRepresentation(cast(OpenAIEmbedder, embedder))
            )
            for identity in (OTHER, IDENTITY):
                await episode(driver, identity)
            await index.ensure(driver, GROUP, [state(OTHER), state()])
            padding = [0.0] * (dimension - 2)
            assert (
                await index.search(driver, GROUP, [0.19, 0.9817840903] + padding, 1)
                == []
            )
            assert await index.search(
                driver, GROUP, [0.21, 0.9777013859] + padding, 1
            ) == [IDENTITY]
            # Exactly representable orthogonal cosine proves strict >, without
            # relying on decimal 0.2 surviving float32 quantisation exactly.
            monkeypatch.setattr(fact_vectors, "FACT_CUTOFF", 0.5)
            assert await index.search(driver, GROUP, [0.0, 1.0] + padding, 2) == []
            monkeypatch.setattr(fact_vectors, "FACT_CUTOFF", 0.499)
            assert await index.search(driver, GROUP, [0.0, 1.0] + padding, 2) == [
                IDENTITY,
                OTHER,
            ]
        finally:
            await driver.close()

    asyncio.run(run())


@pytest.mark.parametrize("query", [[1.0, 0.0], [-1.0, 0.0]])
def test_deletion_after_preflight_is_not_hidden_by_hits_or_limit(
    owned_db: int, query: list[float]
) -> None:
    from cairn.projection.fact_vectors import (
        FactRepresentation,
        FactVectorError,
        FactVectorIndex,
    )
    from cairn.projection.graphiti import BoundedFalkorDriver

    async def run() -> None:
        driver = BoundedFalkorDriver(
            host="127.0.0.1", port=owned_db, database=GROUP, concurrency_limit=2
        )
        try:
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            index = FactVectorIndex(
                FactRepresentation(cast(OpenAIEmbedder, Embedder()))
            )
            for identity in (IDENTITY, OTHER):
                await episode(driver, identity)
            await index.ensure(
                driver,
                GROUP,
                [state(identity) for identity in (IDENTITY, OTHER)],
            )
            await index.preflight(driver, GROUP)
            assert await index.search(driver, GROUP, [1.0, 0.0], 1) == [IDENTITY]
            await driver.execute_query(
                "MATCH (v:CairnFactVector {uuid:$uuid}) DELETE v", uuid=OTHER
            )
            with pytest.raises(FactVectorError, match="fact_vector_rebuild_required"):
                await index.search(driver, GROUP, query, 1)
        finally:
            await driver.close()

    asyncio.run(run())
