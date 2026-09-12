"""The arms of the real Graphiti adapter reachable without a FalkorDB:
input validation that refuses before any I/O, foreign-identity screening,
and the configuration default that keeps the whole path off. The live
path is exercised only by the env-gated ``scripts/graphiti-smoke``,
because CI never constructs the real adapter (P-39)."""

import asyncio
import json
import threading
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import uvloop
from graphiti_core.embedder.openai import OpenAIEmbedder
from openai import DEFAULT_TIMEOUT

import cairn.projection.graphiti as graphiti_module
import cairn.projection.graphiti_bulk as graphiti_bulk_module
from cairn.projection.graphiti import (
    BoundedFalkorDriver,
    GraphitiIndex,
    GraphitiIndexError,
    _episode_identity,
)
from cairn.runtime.config import CairnConfig, GraphitiConfig
from cairn.runtime.logging import SafeLogger, configure_logging


@pytest.mark.parametrize(
    "code,logged",
    [("fact_vector_rebuild_required", True), ("fact_vector_invalid", False)],
)
def test_vector_failure_emits_only_closed_rebuild_signal(
    code: str, logged: bool
) -> None:
    from cairn.projection.fact_vectors import FactVectorError

    stream = StringIO()
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._safe_logger = configure_logging(stream)
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    adapter._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=adapter._loop.run_forever)
    thread.start()

    async def fail() -> None:
        raise FactVectorError(code)

    try:
        with pytest.raises(GraphitiIndexError, match=code):
            adapter._call(fail())
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        thread.join()
        adapter._loop.close()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == int(logged)
    if logged:
        assert records[0] == {
            "event": "semantic_rebuild_needed",
            "time": records[0]["time"],
        }


def test_index_event_loop_is_uvloop() -> None:
    loop = graphiti_module._new_index_loop()
    try:
        assert isinstance(loop, uvloop.Loop)
    finally:
        loop.close()


def test_provider_roles_share_one_pool_that_survives_stage_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        assert llm_client.client is shared
        assert embedder.client is shared
        assert cross_encoder.client is shared
        pool = cast(Any, shared._client._transport)._pool
        assert pool._keepalive_expiry is None
        assert pool._max_connections == 1000
        assert pool._max_keepalive_connections == 100
        assert shared._client.timeout == DEFAULT_TIMEOUT
    finally:
        asyncio.run(shared.close())


def test_graphiti_construction_selects_the_incremental_bulk_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        graphiti = graphiti_module._construct_graphiti(
            BoundedFalkorDriver(falkor_db=_CountingFalkorDB(), concurrency_limit=1),
            llm_client,
            embedder,
            cross_encoder,
            # Not exercising edge batching here: a default batch size would
            # install the real proxy onto graphiti_core's shared, process-
            # global prompt_library with no teardown in this file.
            edge_batch_size=1,
        )
        assert type(graphiti) is graphiti_module._CairnGraphiti
        # No cache store must still install Kuhn's validating boundary.
        assert graphiti.embedder is not embedder
        assert isinstance(graphiti.embedder, OpenAIEmbedder)
        assert graphiti.embedder.config is embedder.config
    finally:
        asyncio.run(shared.close())


def test_graphiti_construction_installs_edge_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[str, object] = {}
    safe_logger = configure_logging(StringIO())

    def record(
        llm_client: object,
        *,
        batch_size: int,
        linger_ms: int,
        max_facts: int,
        logger: SafeLogger | None,
    ) -> None:
        installed.update(
            client=llm_client,
            batch_size=batch_size,
            linger_ms=linger_ms,
            max_facts=max_facts,
            logger=logger,
        )
        return None

    monkeypatch.setattr(graphiti_module, "install_edge_batching", record)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        graphiti_module._construct_graphiti(
            BoundedFalkorDriver(falkor_db=_CountingFalkorDB(), concurrency_limit=1),
            llm_client,
            embedder,
            cross_encoder,
            safe_logger=safe_logger,
        )
        assert installed["client"] is llm_client
        assert installed["batch_size"] == 10
        assert installed["linger_ms"] == 75
        assert installed["max_facts"] == 80
        assert installed["logger"] is safe_logger
    finally:
        asyncio.run(shared.close())


def test_graphiti_construction_passes_an_edge_batch_size_of_one_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision to install or not lives inside ``install_edge_batching``
    (batch_size <= 1 is a no-op there); the caller passes the value through
    unconditionally rather than branching on it itself."""
    installed: dict[str, object] = {}

    def record(
        llm_client: object,
        *,
        batch_size: int,
        linger_ms: int,
        max_facts: int,
        logger: SafeLogger | None,
    ) -> None:
        installed.update(
            batch_size=batch_size,
            linger_ms=linger_ms,
            max_facts=max_facts,
            logger=logger,
        )
        return None

    monkeypatch.setattr(graphiti_module, "install_edge_batching", record)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        graphiti_module._construct_graphiti(
            BoundedFalkorDriver(falkor_db=_CountingFalkorDB(), concurrency_limit=1),
            llm_client,
            embedder,
            cross_encoder,
            edge_batch_size=1,
        )
        assert installed["batch_size"] == 1
        assert installed["logger"] is None
    finally:
        asyncio.run(shared.close())


def test_graphiti_construction_refuses_an_unreviewed_bulk_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graphiti_bulk_module, "_GRAPHITI_CORE_VERSION", "0.29.4")

    with pytest.raises(RuntimeError, match="graphiti_bulk_compatibility_version"):
        graphiti_module._construct_graphiti(
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
        )


def test_the_graphiti_block_defaults_to_disabled() -> None:
    """P-39: a configuration that never mentions graphiti has it off, the
    same posture as attic."""
    assert CairnConfig.model_fields["graphiti"].default == GraphitiConfig(enabled=False)


def test_the_disabled_default_carries_loopback_connection_values() -> None:
    config = GraphitiConfig(enabled=False)

    assert config.host == "127.0.0.1"
    assert config.port == 6379


def test_an_oversize_query_refuses_before_any_io() -> None:
    """8 KiB is the I-30 retrieval-query bound, mirrored from the Attic
    adapter. The check runs before the loop bridge, so no FalkorDB is
    needed to prove it — which is also why this test can exist in CI."""
    index = GraphitiIndex.__new__(GraphitiIndex)  # no connection: validation only

    with pytest.raises(GraphitiIndexError) as caught:
        index.search("q" * (8 * 1024 + 1), 10, ())

    assert caught.value.code == "query_too_large"


def test_a_multibyte_query_is_measured_in_bytes() -> None:
    index = GraphitiIndex.__new__(GraphitiIndex)

    with pytest.raises(GraphitiIndexError) as caught:
        index.search("ü" * (4 * 1024 + 1), 10, ())

    assert caught.value.code == "query_too_large"


@pytest.mark.parametrize("limit", [0, -1, True])
def test_an_invalid_limit_refuses_before_any_io(limit: int) -> None:
    index = GraphitiIndex.__new__(GraphitiIndex)

    with pytest.raises(GraphitiIndexError) as caught:
        index.search("query", limit, ())

    assert caught.value.code == "invalid_limit"


def test_foreign_episode_identities_are_screened() -> None:
    """An episode uuid this adapter wrote is a canonical fact UUID; a
    hostile or co-tenant store can hold anything else, and anything else
    never becomes a candidate."""
    canonical = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    assert _episode_identity(canonical) == UUID(canonical)
    assert _episode_identity(canonical.upper()) is None
    assert _episode_identity("urn:uuid:" + canonical) is None
    assert _episode_identity("not-a-uuid") is None
    assert _episode_identity("") is None


# --- P-82 gate-4 ruling (25 August 2026): the driver-level query bound --


class _CountingFalkorDB:
    """A fake connection that counts concurrent ``query`` calls. Peak
    concurrency is the whole assertion: the driver's semaphore is the only
    thing standing between graphiti's multiplied gathers and the index's
    queued-query ceiling."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.queries: list[str] = []

    def select_graph(self, name: str) -> "_CountingGraph":
        return _CountingGraph(self)


class _CountingGraph:
    def __init__(self, owner: _CountingFalkorDB) -> None:
        self._owner = owner

    async def query(self, cypher: str, params: object) -> object:
        self._owner.calls += 1
        self._owner.queries.append(cypher)
        self._owner.in_flight += 1
        self._owner.peak = max(self._owner.peak, self._owner.in_flight)
        # Yield repeatedly so every unbounded sibling task would get a
        # chance to enter before this one leaves.
        for _ in range(3):
            await asyncio.sleep(0)
        self._owner.in_flight -= 1
        return SimpleNamespace(header=[], result_set=[])


def test_the_bounded_driver_caps_in_flight_index_queries() -> None:
    """Six concurrent queries against a bound of 2 never see more than 2
    in flight, and all six still run."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=2)

    async def issue() -> None:
        await asyncio.gather(*(driver.execute_query("RETURN 1") for _ in range(6)))

    asyncio.run(issue())

    assert fake.calls == 6
    assert fake.peak <= 2


def test_a_bounded_driver_clone_shares_its_parents_bound() -> None:
    """graphiti clones the driver per group id; a clone with its own
    semaphore would multiply the bound away, exactly the defect the sweep
    found."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=2)
    clone = driver.clone(database="another-group")

    assert isinstance(clone, BoundedFalkorDriver)

    async def issue() -> None:
        await asyncio.gather(
            *(driver.execute_query("RETURN 1") for _ in range(3)),
            *(clone.execute_query("RETURN 1") for _ in range(3)),
        )

    asyncio.run(issue())

    assert fake.calls == 6
    assert fake.peak <= 2


def test_a_bounded_driver_session_shares_the_same_bound() -> None:
    """graphiti's bulk node-and-edge writes go through ``driver.session()``
    rather than ``execute_query`` — a bound that missed the session path
    would miss the sweep's actual write load."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=2)
    session = driver.session()

    async def issue() -> None:
        await asyncio.gather(
            *(driver.execute_query("RETURN 1") for _ in range(3)),
            *(session.run("RETURN 1") for _ in range(3)),
        )

    asyncio.run(issue())

    assert fake.calls == 6
    assert fake.peak <= 2


_GRAPHITI_0293_EDGE_SEARCH = """CALL db.idx.fulltext.queryRelationships('RELATES_TO', $query)
    YIELD relationship AS rel, score
    MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)
     WHERE e.group_id IN $group_ids
    WITH e, score, n, m
    RETURN e.uuid AS uuid
    ORDER BY score DESC
    LIMIT $limit
    """


def test_the_pinned_graphiti_edge_search_uses_the_returned_relationship() -> None:
    """graphiti-core 0.29.3 re-MATCHes every Falkor full-text hit before
    applying its result limit (upstream #1272/#1506). The live sweep measured
    528 full-text hits in 1 ms, the first twenty plus their MATCH in 198 ms,
    and the shipped query timing out after five seconds. The compatibility
    seam removes only that exact, pinned fragment and preserves the filters,
    projection, ordering and limit around it."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)

    asyncio.run(
        driver.execute_query(
            _GRAPHITI_0293_EDGE_SEARCH,
            query="opaque",
            group_ids=["opaque"],
            limit=20,
        )
    )

    assert fake.queries == [
        _GRAPHITI_0293_EDGE_SEARCH.replace(
            """YIELD relationship AS rel, score
    MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)""",
            """YIELD relationship AS e, score
    WITH e, score, startNode(e) AS n, endNode(e) AS m""",
        )
    ]


def test_the_edge_search_compatibility_refuses_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workaround must become an explicit upgrade decision if a later
    graphiti-core still emits the defective query, never a permanent silent
    fork of unknown upstream code."""
    monkeypatch.setattr(graphiti_module, "_GRAPHITI_CORE_VERSION", "0.29.4")
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)

    with pytest.raises(RuntimeError, match="graphiti_compatibility_version"):
        asyncio.run(driver.execute_query(_GRAPHITI_0293_EDGE_SEARCH))

    assert fake.calls == 0
