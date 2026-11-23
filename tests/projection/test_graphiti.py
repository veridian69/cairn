"""The arms of the real Graphiti adapter reachable without a FalkorDB:
input validation that refuses before any I/O, foreign-identity screening,
and the configuration default that keeps the whole path off. The live
path is exercised only by the env-gated ``scripts/graphiti-smoke``,
because CI never constructs the real adapter (P-39)."""

import asyncio
import json
import threading
from collections.abc import Callable
from contextvars import Context
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import uvloop
from graphiti_core.edges import EntityEdge, EpisodicEdge
from graphiti_core.embedder.openai import OpenAIEmbedder
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.search.search_utils import edge_fulltext_search, fulltext_query
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk_tx
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


def _refused_search(
    during: Callable[[Any], None], *, in_flight: int = 0, verified: bool = True
) -> list[str]:
    """One nonblank search through the real ``_call`` and ``_search``, refused
    by a stubbed coverage gate after ``during`` has run; the events logged."""
    from cairn.projection.fact_vectors import FactVectorError

    stream = StringIO()
    adapter = cast(Any, GraphitiIndex.__new__(GraphitiIndex))
    adapter._safe_logger = configure_logging(stream)
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    adapter._search_bound = asyncio.Semaphore(2)
    adapter._in_flight = in_flight
    adapter.set_coverage_verified(verified)

    async def refuse(*_: object) -> tuple[()]:
        during(adapter)
        raise FactVectorError("fact_vector_rebuild_required")

    adapter._search_with_leased_drivers = refuse
    adapter._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=adapter._loop.run_forever)
    thread.start()
    try:
        with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
            adapter._call(adapter._search("plain query", 1, ("acme\n[]",)))
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        thread.join()
        adapter._loop.close()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert all(set(record) == {"event", "time"} for record in records)
    return [record["event"] for record in records]


def _nothing(adapter: Any) -> None:
    pass


def _projection_ends(adapter: Any) -> None:
    adapter._in_flight = 0


def _projection_begins_and_ends(adapter: Any) -> None:
    with adapter._projecting():
        pass


def test_search_refused_during_a_projection_logs_in_flight_not_rebuild() -> None:
    """#281: the refusal is unchanged; the event no longer asks for a
    rebuild while a save is simply being projected."""
    assert _refused_search(_nothing, in_flight=1) == ["semantic_projection_in_flight"]


def test_projection_ending_before_the_refusal_is_logged_still_counts() -> None:
    """The gate saw the episode in flight; the projection finished before
    the refusal reached the request thread that logs it."""
    assert _refused_search(_projection_ends, in_flight=1) == [
        "semantic_projection_in_flight"
    ]


def test_projection_beginning_and_ending_inside_the_search_counts() -> None:
    assert _refused_search(_projection_begins_and_ends) == [
        "semantic_projection_in_flight"
    ]


def test_refusal_with_no_projection_overlapping_keeps_the_rebuild_warning() -> None:
    assert _refused_search(_nothing) == ["semantic_rebuild_needed"]


def test_unverified_index_keeps_the_rebuild_warning_during_a_projection() -> None:
    """I-99: the unverified refusal is not a projection's doing."""
    assert _refused_search(_nothing, in_flight=1, verified=False) == [
        "semantic_rebuild_needed"
    ]


def test_a_projections_own_vector_failure_keeps_the_rebuild_warning() -> None:
    from cairn.projection.fact_vectors import FactVectorError

    stream = StringIO()
    adapter = cast(Any, GraphitiIndex.__new__(GraphitiIndex))
    adapter._safe_logger = configure_logging(stream)
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    adapter._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=adapter._loop.run_forever)
    thread.start()

    async def fail() -> None:
        raise FactVectorError("fact_vector_rebuild_required")

    adapter._project = lambda state: fail()
    try:
        with pytest.raises(GraphitiIndexError, match="fact_vector_rebuild_required"):
            adapter.project(object())
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        thread.join()
        adapter._loop.close()
    assert [json.loads(line)["event"] for line in stream.getvalue().splitlines()] == [
        "semantic_rebuild_needed"
    ]
    assert adapter._in_flight == 0


@pytest.mark.parametrize("fails", [False, True])
def test_projection_counts_itself_in_flight_only_while_it_runs(fails: bool) -> None:
    adapter = cast(Any, GraphitiIndex.__new__(GraphitiIndex))
    seen: list[int] = []

    def call(coroutine: Any, **_: object) -> tuple[()]:
        coroutine.close()
        seen.append(adapter._in_flight)
        if fails:
            raise GraphitiIndexError("graphiti_unavailable")
        return ()

    adapter._call = call
    adapter._project = lambda state: asyncio.sleep(0)
    adapter._project_many = lambda states: asyncio.sleep(0)
    runs: tuple[Callable[[], object], ...] = (
        lambda: adapter.project(object()),
        lambda: adapter.project_many((object(),)),
    )
    for run in runs:
        if fails:
            with pytest.raises(GraphitiIndexError):
                run()
        else:
            run()
    assert seen == [1, 1]
    assert adapter._in_flight == 0


def test_index_event_loop_is_uvloop() -> None:
    loop = graphiti_module._new_index_loop()
    try:
        assert isinstance(loop, uvloop.Loop)
    finally:
        loop.close()


def test_task_factory_serves_the_real_index_loop() -> None:
    """The loop chooses which create_task keywords a factory receives, and
    uvloop 0.23 began passing ``eager_start``. A factory that refuses one
    fails every task on the adapter's loop, so this runs on the real loop
    class, not the stdlib loop the containment tests use."""
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = graphiti_module._new_index_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    thread.start()
    try:

        async def child() -> str:
            return "child"

        async def parent() -> tuple[str, str]:
            task = asyncio.get_running_loop().create_task(child(), name="named")
            return await task, task.get_name()

        future = asyncio.run_coroutine_threadsafe(parent(), adapter._loop)
        assert future.result(timeout=5.0) == ("child", "named")

        async def eager_parent() -> None:
            inner = object()

            async def sets_its_own_scope() -> None:
                graphiti_module._GRAPHITI_TASK_SCOPE.set(inner)

            # An eager task runs its first step inside the constructor. The
            # factory must file it under the scope its creator had (none
            # here), never under one the task set for itself.
            # Through the factory itself: loop.create_task takes eager_start
            # only from Python 3.14, asyncio.Task from 3.12.
            await adapter._scoped_task_factory(
                asyncio.get_running_loop(),
                sets_its_own_scope(),
                context=Context(),
                eager_start=True,
            )
            assert inner not in adapter._scoped_tasks

        eager = asyncio.run_coroutine_threadsafe(eager_parent(), adapter._loop)
        eager.result(timeout=5.0)
        assert adapter._scoped_tasks == {}
        # A keyword no released loop sends yet must reach the task untouched.
        probe = child()
        with pytest.raises(TypeError, match="not_a_task_keyword"):
            adapter._scoped_task_factory(adapter._loop, probe, not_a_task_keyword=1)
        probe.close()
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        thread.join(timeout=5.0)
        adapter._loop.close()


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
        combined_call: Any,
    ) -> None:
        installed.update(
            client=llm_client,
            batch_size=batch_size,
            linger_ms=linger_ms,
            max_facts=max_facts,
            logger=logger,
            combined_call=combined_call,
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
        # The combined call alone reasons (I-102): a second client on the
        # same models and provider pool, and the first one left as it was.
        combined = cast(Any, installed["combined_call"]).__self__
        assert combined is not llm_client
        assert combined.reasoning == "low"
        assert llm_client.reasoning == "none"
        assert combined.client is shared
        assert (combined.model, combined.small_model) == (
            llm_client.model,
            llm_client.small_model,
        )
        assert installed["combined_call"] == combined.generate_response
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
        combined_call: object,
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


# --- Issue #275: oversized bulk writes go out in slices ---


class _RecordingFalkorDB:
    """A fake connection that records each query with its parameters and,
    optionally, fails the query at one position."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail_at = fail_at
        self.first_sent = asyncio.Event()

    def select_graph(self, name: str) -> "_RecordingGraph":
        return _RecordingGraph(self)


class _RecordingGraph:
    def __init__(self, owner: _RecordingFalkorDB) -> None:
        self._owner = owner

    async def query(self, cypher: str, params: dict[str, Any]) -> object:
        if len(self._owner.sent) == self._owner.fail_at:
            raise RuntimeError("slice refused")
        self._owner.sent.append((cypher, params))
        self._owner.first_sent.set()
        await asyncio.sleep(0)
        return SimpleNamespace(header=[], result_set=[])


_UNWIND = "UNWIND $rows AS row MERGE (n:Thing {uuid: row.uuid})"


def _rows(count: int) -> list[dict[str, int]]:
    return [{"uuid": i} for i in range(count)]


def _run_session(fake: _RecordingFalkorDB, query: Any, **kwargs: Any) -> None:
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)
    asyncio.run(driver.session().run(query, **kwargs))


def test_a_bulk_write_at_the_cap_goes_out_as_one_query() -> None:
    fake = _RecordingFalkorDB()
    rows = _rows(graphiti_module._BULK_WRITE_ROWS)

    _run_session(fake, _UNWIND, rows=rows, group_id="g")

    assert fake.sent == [(_UNWIND, {"rows": rows, "group_id": "g"})]


def test_an_oversized_bulk_write_goes_out_in_ordered_slices() -> None:
    """Every row is sent exactly once, in order, in slices no longer than
    the cap, and every other parameter rides along unchanged."""
    fake = _RecordingFalkorDB()
    cap = graphiti_module._BULK_WRITE_ROWS
    rows = _rows(2 * cap + 7)

    _run_session(fake, "\n    " + _UNWIND, rows=rows, group_id="g")

    assert [len(params["rows"]) for _, params in fake.sent] == [cap, cap, 7]
    assert [row for _, params in fake.sent for row in params["rows"]] == rows
    assert all(params["group_id"] == "g" for _, params in fake.sent)
    assert all(cypher == "\n    " + _UNWIND for cypher, _ in fake.sent)


def test_a_list_of_query_pairs_is_split_the_same_way() -> None:
    """The entity-node save reaches ``run`` as (cypher, params) pairs; an
    oversized pair is sliced in place and its neighbours keep their order."""
    fake = _RecordingFalkorDB()
    cap = graphiti_module._BULK_WRITE_ROWS
    rows = _rows(cap + 1)

    _run_session(
        fake,
        [
            ("RETURN 1", {"a": 1}),
            (_UNWIND, {"rows": rows, "group_id": "g"}),
            ("RETURN 2", {"b": 2}),
        ],
    )

    assert fake.sent == [
        ("RETURN 1", {"a": 1}),
        (_UNWIND, {"rows": rows[:cap], "group_id": "g"}),
        (_UNWIND, {"rows": rows[cap:], "group_id": "g"}),
        ("RETURN 2", {"b": 2}),
    ]


def test_a_query_that_does_not_open_with_unwind_is_not_split() -> None:
    fake = _RecordingFalkorDB()
    rows = _rows(graphiti_module._BULK_WRITE_ROWS + 1)
    query = "MATCH (n) WITH n UNWIND $rows AS row RETURN row"

    _run_session(fake, query, rows=rows)

    assert fake.sent == [(query, {"rows": rows})]


def test_each_slice_takes_the_bound_on_its_own() -> None:
    """With a bound of 1, a query issued while a split write is under way
    gets in between its slices, so the bound is released after each one."""
    fake = _RecordingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)
    rows = _rows(2 * graphiti_module._BULK_WRITE_ROWS)

    async def issue() -> None:
        async def other() -> None:
            await fake.first_sent.wait()
            await driver.execute_query("RETURN 1")

        await asyncio.gather(driver.session().run(_UNWIND, rows=rows), other())

    asyncio.run(issue())

    assert [cypher for cypher, _ in fake.sent] == [_UNWIND, "RETURN 1", _UNWIND]
    assert driver._query_bound._value == 1


def test_a_failing_slice_stops_the_write_and_propagates() -> None:
    fake = _RecordingFalkorDB(fail_at=1)
    cap = graphiti_module._BULK_WRITE_ROWS
    rows = _rows(3 * cap)
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)

    with pytest.raises(RuntimeError, match="slice refused"):
        asyncio.run(driver.session().run(_UNWIND, rows=rows))

    assert [params["rows"] for _, params in fake.sent] == [rows[:cap]]
    assert driver._query_bound._value == 1


def test_the_pinned_graphiti_bulk_save_queries_are_splittable() -> None:
    """The splitter only helps while graphiti-core's FalkorDB bulk save sends
    episodes, episodic edges and entity edges as one ``UNWIND $<name> AS``
    query each over its list parameter (entity nodes go one per query). An
    upgrade that changes the shape, the parameter names or the dispatch path
    must fail here rather than quietly restore the 190 MB query."""
    fake = _RecordingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)
    assert driver.graph_operations_interface is None
    episode = EpisodicNode(
        name="e",
        group_id="g",
        source=EpisodeType.message,
        source_description="s",
        content="c",
        valid_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    source = EntityNode(name="a", group_id="g", name_embedding=[0.0])
    target = EntityNode(name="b", group_id="g", name_embedding=[0.0])
    mention = EpisodicEdge(
        source_node_uuid=episode.uuid,
        target_node_uuid=source.uuid,
        group_id="g",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    relation = EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        name="R",
        fact="a relates to b",
        group_id="g",
        episodes=[episode.uuid],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        fact_embedding=[0.0],
    )

    asyncio.run(
        add_nodes_and_edges_bulk_tx(
            driver.session(),
            [episode],
            [mention],
            [source, target],
            [relation],
            cast(Any, None),
            driver,
        )
    )

    names = []
    for cypher, params in fake.sent:
        match = graphiti_module._BULK_WRITE_UNWIND.match(cypher)
        assert match is not None, cypher
        assert isinstance(params[match.group(1)], list)
        names.append(match.group(1))
    assert names == ["episodes", "nodes", "nodes", "episodic_edges", "entity_edges"]


def test_the_pinned_graphiti_edge_search_uses_the_returned_relationship() -> None:
    """Exercise the dependency boundary that replaced Cairn's old query shim."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)

    asyncio.run(
        edge_fulltext_search(
            driver,
            "opaque",
            SearchFilters(),
            ["opaque"],
            20,
        )
    )

    assert len(fake.queries) == 1
    query = " ".join(fake.queries[0].split())
    assert "MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)" not in query
    assert "WITH rel AS e, score, startNode(rel) AS n, endNode(rel) AS m" in query
    assert "WHERE n:Entity AND m:Entity" in query
    assert "e.group_id IN $group_ids" in query
    assert "ORDER BY score DESC LIMIT $limit" in query


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        (
            "child agents named <tier>__<semantic>_<model>",
            "child | agents | named | tier | semantic | model",
        ),
        ("alpha _ beta", "alpha | beta"),
        ("snake_case _leading trailing_", "snake_case | _leading | trailing_"),
    ],
)
def test_a_search_phrase_drops_words_made_only_of_underscores(
    text: str, terms: str
) -> None:
    """RediSearch rejects a bare ``_`` term, and graphiti-core passes it on.

    Reproduced on FalkorDB 22 November 2026: ``alpha _ beta`` fails the whole
    fulltext query with a syntax error; the opt-in database test holds that.
    """
    driver = BoundedFalkorDriver(falkor_db=_CountingFalkorDB(), concurrency_limit=1)

    assert driver.build_fulltext_query(text, ["g1"]) == f'(@group_id:"g1") ({terms})'


def test_a_search_phrase_of_only_underscores_is_empty() -> None:
    driver = BoundedFalkorDriver(falkor_db=_CountingFalkorDB(), concurrency_limit=1)

    assert driver.build_fulltext_query("_ __", ["g1"]) == ""


def test_every_graphiti_fulltext_search_builds_its_phrase_through_the_driver() -> None:
    """The override is only a fix while upstream routes every search here."""
    fake = _CountingFalkorDB()
    driver = BoundedFalkorDriver(falkor_db=fake, concurrency_limit=1)

    assert (
        fulltext_query("alpha _ beta", ["g1"], driver)
        == '(@group_id:"g1") (alpha | beta)'
    )
