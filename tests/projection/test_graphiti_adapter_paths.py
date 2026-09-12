"""The real Graphiti adapter's projection and search paths, deterministically.

These are the paths `ad17203` changed after the live smoke found the
adapter unable to project or recall a single fact, and which Val's review
of 10 August 2026 found wholly untested — `_project` and `_search` were at
0% while the module reported 33%. Only Redis/FalkorDB compatibility
inherently needs a live database; everything here is reachable with fake
async collaborators, so it is tested here rather than trusted to an opt-in
smoke.

The coroutine bodies are driven directly with ``asyncio.run``, bypassing
the loop-bridge thread — this fixes what ``_project`` and ``_search`` do,
not how ``_call`` gets them onto a loop.
"""

import asyncio
import inspect
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import CancelledError, Future
from contextvars import Context
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_config import SearchConfig

import cairn.projection.graphiti as graphiti_module
from cairn.catalogue.audit import Classification, ScopeSegment, TrustClass
from cairn.projection.adapter import FactProjected, ProjectedFactState
from cairn.projection.graphiti import (
    _BULK_INCOMPLETE,
    _PROJECTION_DONE,
    _PROJECTION_PENDING,
    GraphitiIndex,
    GraphitiIndexError,
)
from cairn.projection.partition import derive_group_id

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
PARTITION = 'acme\n[{"kind":"repo","id":"api"}]'
PARENT_PARTITION = "acme\n[]"
IDENTITY = "11111111-1111-4111-8111-111111111111"
OTHER_IDENTITY = "22222222-2222-4222-8222-222222222222"


def fact(
    identity: str = IDENTITY, *, partition_key: str = PARTITION
) -> ProjectedFactState:
    return ProjectedFactState(
        fact_id=UUID(identity),
        partition_key=partition_key,
        body="the retry budget is four",
        realm_id="acme",
        segments=(ScopeSegment("repo", "api"),),
        classification=Classification.INTERNAL,
        trust=TrustClass.VALIDATED,
        recorded_at=NOW,
        valid_from=None,
        valid_to=None,
        invalidated_at=None,
    )


@pytest.mark.parametrize("operation", ["project", "project_many", "clear"])
def test_partition_init_finishes_before_first_operation(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    class ReachedOperation(Exception):
        pass

    async def scenario() -> None:
        release = asyncio.Event()
        reached: list[bool] = []

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            await release.wait()

        async def first(*args: Any, **kwargs: Any) -> Any:
            reached.append(True)
            raise ReachedOperation

        async def close() -> None:
            pass

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        monkeypatch.setattr(EpisodicNode, "get_by_uuid", first)
        monkeypatch.setattr(graphiti_module, "clear_data", first)
        root = graphiti_module.BoundedFalkorDriver(
            falkor_db=SimpleNamespace(aclose=close), concurrency_limit=2
        )
        adapter = GraphitiIndex.__new__(GraphitiIndex)
        adapter._driver = root
        call: Callable[[], Coroutine[Any, Any, Any]] = {
            "project": lambda: adapter._project(fact()),
            "project_many": lambda: adapter._project_many((fact(),)),
            "clear": lambda: adapter._clear((PARTITION,)),
        }[operation]
        task = asyncio.create_task(call())
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            assert reached == []
            release.set()
            with pytest.raises(ReachedOperation):
                await task
            assert reached == [True]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await root.close()

    asyncio.run(scenario())


class FakeDriver:
    """Records the databases it was cloned for; is otherwise inert."""

    def __init__(self) -> None:
        self.cloned: list[str] = []
        self.vectors: dict[str, dict[str, object]] = {}

    def clone(self, database: str) -> "FakeDriver":
        self.cloned.append(database)
        return self

    async def execute_query(self, query: str, **parameters: Any) -> Any:
        # These tests isolate extraction/identity reconciliation. The new SQL's
        # real coverage semantics are exercised by test_fact_vectors_db.py.
        if "AS coverage_valid" in query:
            return [{"coverage_valid": True, "candidates": []}], None, None
        if query.startswith("CREATE INDEX"):
            return [], None, None
        if query.startswith("MATCH (v:CairnFactVector)"):
            return (
                [self.vectors[key] for key in parameters["ids"] if key in self.vectors],
                None,
                None,
            )
        if query.startswith("MERGE (v:CairnFactVector"):
            self.vectors[parameters["uuid"]] = parameters
            return [], None, None
        raise AssertionError("unexpected vector query")


@dataclass
class StoredEpisode:
    uuid: str
    source_description: str
    content: str
    entity_edges: list[str]


class FakeStore:
    """Stands in for the episode table, recording write order.

    ``save`` mirrors the real ``MERGE … SET n = {…}`` whole-property
    replacement, which is the reason the adapter must re-load before it
    stamps completion. ``lookup_failure``, when set, is what ``get_by_uuid``
    raises instead of answering — the store that cannot say, as distinct
    from the store that says nothing is there.
    """

    def __init__(self) -> None:
        self.episodes: dict[str, StoredEpisode] = {}
        self.writes: list[tuple[str, str]] = []
        self.lookup_failure: Exception | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = self

        async def get_by_uuid(
            cls: type[object], driver: object, uuid: str
        ) -> SimpleNamespace:
            if store.lookup_failure is not None:
                raise store.lookup_failure
            found = store.episodes.get(uuid)
            if found is None:
                # The real ``EpisodicNode.get_by_uuid`` raises exactly this
                # when its query returns zero records, and nothing else
                # means absence.
                raise NodeNotFoundError(uuid)
            node = SimpleNamespace(
                uuid=found.uuid,
                source_description=found.source_description,
                content=found.content,
                entity_edges=list(found.entity_edges),
            )

            async def save_loaded(driver: object) -> None:
                store.write(
                    node.uuid,
                    node.source_description,
                    node.content,
                    list(node.entity_edges),
                )

            node.save = save_loaded
            return node

        async def save(self: SimpleNamespace, driver: object) -> None:
            store.write(
                self.uuid,
                self.source_description,
                self.content,
                list(self.entity_edges),
            )

        monkeypatch.setattr(
            "cairn.projection.graphiti.EpisodicNode.get_by_uuid",
            classmethod(get_by_uuid),
        )
        monkeypatch.setattr("cairn.projection.graphiti.EpisodicNode.save", save)

    def write(
        self, uuid: str, source_description: str, content: str, entity_edges: list[str]
    ) -> None:
        self.episodes[uuid] = StoredEpisode(
            uuid=uuid,
            source_description=source_description,
            content=content,
            entity_edges=entity_edges,
        )
        self.writes.append(("save", source_description))


class FakeGraphiti:
    """``add_episode`` records its calls; ``search_`` returns a scripted result.

    ``add_episode`` also writes ``entity_edges`` onto the stored episode,
    as the real library does, so a stamp that discarded them is visible.
    """

    def __init__(
        self,
        store: FakeStore,
        *,
        fails: bool = False,
        episodes: tuple[str, ...] = (),
        edges: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        self._store = store
        self._fails = fails
        self._episodes = episodes
        self._edges = edges
        self.add_calls: list[dict[str, object]] = []
        self.bulk_calls: list[tuple[str, ...]] = []
        self.omit_from_results: set[str] = set()
        self.search_configs: list[SearchConfig] = []
        self.search_group_ids: list[list[str]] = []

        async def embed(texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

        self.embedder = SimpleNamespace(
            config=SimpleNamespace(
                embedding_model="text-embedding-3-small", embedding_dim=2
            ),
            create_batch=embed,
        )

    async def add_episode(self, **kwargs: object) -> None:
        self.add_calls.append(kwargs)
        self._store.writes.append(("add_episode", str(kwargs["source_description"])))
        if self._fails:
            raise RuntimeError("extraction failed")
        uuid = str(kwargs["uuid"])
        existing = self._store.episodes[uuid]
        existing.entity_edges = ["edge-a", "edge-b"]

    async def add_episode_bulk(
        self, bulk_episodes: list[object], group_id: str | None = None
    ) -> SimpleNamespace:
        self.bulk_calls.append(
            tuple(str(getattr(episode, "uuid")) for episode in bulk_episodes)  # noqa: B009
        )
        self._store.writes.append(("add_episode_bulk", str(len(bulk_episodes))))
        if self._fails:
            raise RuntimeError("bulk extraction failed")
        completed = []
        for episode in bulk_episodes:
            existing = self._store.episodes[str(getattr(episode, "uuid"))]  # noqa: B009
            existing.entity_edges = ["edge-a", "edge-b"]
            completed.append(existing)
        # The shape ``AddBulkEpisodeResults`` actually has: an ``episodes``
        # list naming what the bulk call completed. ``omit_from_results``
        # simulates a partial answer without failing the call.
        return SimpleNamespace(
            episodes=[
                episode
                for episode in completed
                if episode.uuid not in self.omit_from_results
            ]
        )

    async def search_(
        self, query: str, config: SearchConfig, *, group_ids: list[str], driver: object
    ) -> object:
        self.search_configs.append(config)
        self.search_group_ids.append(group_ids)
        return SimpleNamespace(
            episodes=[SimpleNamespace(uuid=value) for value in self._episodes],
            edges=[SimpleNamespace(episodes=list(group)) for group in self._edges],
        )

    async def search_with_vector(
        self,
        query: str,
        config: SearchConfig,
        *,
        group_ids: list[str],
        driver: object,
        query_vector: list[float],
    ) -> object:
        return await self.search_(query, config, group_ids=group_ids, driver=driver)


class SearchAdmissionProbe:
    """Loop-owned staged search double with thread-visible barriers."""

    def __init__(self, queries: tuple[str, ...]) -> None:
        self.partitions = {query: f"{query}\n[]" for query in queries}
        self.groups = {
            derive_group_id(partition): query
            for query, partition in self.partitions.items()
        }
        self.identities = {
            query: str(UUID(int=position))
            for position, query in enumerate(queries, start=1)
        }
        self.stages: list[tuple[str, str]] = []
        self.group_calls: dict[str, list[str]] = {}
        self._reached = {
            (query, stage): threading.Event()
            for query in queries
            for stage in ("preflight", "embed", "graph", "fact")
        }
        self._graph_release = {query: asyncio.Event() for query in queries}
        self._fact_release = {query: asyncio.Event() for query in queries}
        self.graph_errors: dict[str, Exception] = {}
        self._guard = threading.Lock()
        self.active_graph = 0
        self.max_active_graph = 0
        probe = self

        class Representation:
            async def embed_text(self, query: str) -> list[float]:
                probe.record(query, "embed")
                return [1.0, 0.0]

        class FactVectors:
            representation = Representation()

            async def preflight(self, driver: Any, group_id: str) -> None:
                probe.record(probe.groups[group_id], "preflight")

            async def search(
                self,
                driver: Any,
                group_id: str,
                query_vector: list[float],
                limit: int,
            ) -> list[str]:
                query = probe.groups[group_id]
                probe.record(query, "fact")
                await probe._fact_release[query].wait()
                return [probe.identities[query]]

        self.fact_vectors = FactVectors()

    def record(self, query: str, stage: str) -> None:
        with self._guard:
            self.stages.append((query, stage))
        self._reached[query, stage].set()

    def reached(self, query: str, stage: str) -> bool:
        return self._reached[query, stage].is_set()

    def wait_for(self, query: str, stage: str) -> None:
        assert self._reached[query, stage].wait(timeout=2.0)

    def release_graph(self, loop: asyncio.AbstractEventLoop, query: str) -> None:
        loop.call_soon_threadsafe(self._graph_release[query].set)

    def release_fact(self, loop: asyncio.AbstractEventLoop, query: str) -> None:
        loop.call_soon_threadsafe(self._fact_release[query].set)

    def release_all(self, loop: asyncio.AbstractEventLoop) -> None:
        for query in self.partitions:
            self.release_graph(loop, query)
            self.release_fact(loop, query)

    async def search_with_vector(
        self,
        query: str,
        config: SearchConfig,
        *,
        group_ids: list[str],
        driver: object,
        query_vector: list[float],
    ) -> object:
        self.record(query, "graph")
        with self._guard:
            self.active_graph += 1
            self.max_active_graph = max(self.max_active_graph, self.active_graph)
            self.group_calls[query] = group_ids
        try:
            await self._graph_release[query].wait()
            error = self.graph_errors.get(query)
            if error is not None:
                raise error
            return SimpleNamespace(episodes=[], edges=[])
        finally:
            with self._guard:
                self.active_graph -= 1


class SearchAdmissionClone:
    def __init__(self, database: str) -> None:
        self.database = database
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


class SearchAdmissionDriver:
    def __init__(self) -> None:
        self.clones: list[SearchAdmissionClone] = []

    def clone(self, database: str) -> SearchAdmissionClone:
        clone = SearchAdmissionClone(database)
        self.clones.append(clone)
        return clone


class SearchAdmissionProvider:
    async def close(self) -> None:
        pass


class SearchAdmissionAttempts:
    """Expose loop-side attempts while retaining real semaphore semantics."""

    def __init__(self, bound: asyncio.Semaphore) -> None:
        self._bound = bound
        self._attempts = 0
        self._guard = threading.Lock()
        self.second_attempt = threading.Event()

    async def __aenter__(self) -> None:
        with self._guard:
            self._attempts += 1
            if self._attempts == 2:
                self.second_attempt.set()
        await self._bound.acquire()

    async def __aexit__(self, *args: Any) -> None:
        self._bound.release()


def owned_search_index(
    monkeypatch: pytest.MonkeyPatch,
    probe: SearchAdmissionProbe,
    *,
    index_concurrency_limit: int,
) -> GraphitiIndex:
    async def construct(self: GraphitiIndex, *args: Any) -> SearchAdmissionDriver:
        return SearchAdmissionDriver()

    monkeypatch.setattr(GraphitiIndex, "_construct", construct)
    monkeypatch.setattr(
        graphiti_module,
        "_openai_provider_clients",
        lambda: (SearchAdmissionProvider(), object(), object(), object()),
    )
    monkeypatch.setattr(
        graphiti_module,
        "_construct_graphiti",
        lambda *args, **kwargs: probe,
    )
    adapter = GraphitiIndex(
        host="unused",
        port=0,
        index_concurrency_limit=index_concurrency_limit,
    )
    monkeypatch.setattr(adapter, "_fact_vector_index", lambda: probe.fact_vectors)
    return adapter


def submit_search(
    adapter: GraphitiIndex,
    probe: SearchAdmissionProbe,
    query: str,
) -> Future[tuple[UUID, ...]]:
    return asyncio.run_coroutine_threadsafe(
        adapter._search(query, 1, (probe.partitions[query],)), adapter._loop
    )


def pass_owned_loop(adapter: GraphitiIndex) -> None:
    async def marker() -> None:
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run_coroutine_threadsafe(marker(), adapter._loop).result(timeout=2.0)


def clean_up_searches(
    adapter: GraphitiIndex,
    probe: SearchAdmissionProbe,
    searches: list[Future[tuple[UUID, ...]]],
) -> None:
    probe.release_all(adapter._loop)
    for search in searches:
        search.cancel()
    for search in searches:
        try:
            search.result(timeout=2.0)
        except (CancelledError, RuntimeError):
            pass
    adapter.close()


def index(graphiti: FakeGraphiti, driver: FakeDriver) -> GraphitiIndex:
    built = GraphitiIndex.__new__(GraphitiIndex)
    built._graphiti = graphiti  # type: ignore[assignment]
    built._driver = driver  # type: ignore[assignment]
    built._search_bound = asyncio.Semaphore(2)
    built._close_requested = threading.Event()
    built._init_owner = cast(Any, SimpleNamespace(admit=lambda: None))
    built._search_driver_leases = {}
    return built


# --- P-38's completion invariant ----------------------------------------


@pytest.mark.parametrize(
    ("index_concurrency_limit", "expected_bound"), [(1, 1), (2, 2), (8, 2)]
)
def test_full_search_admission_bounds_the_whole_nonblank_search(
    monkeypatch: pytest.MonkeyPatch,
    index_concurrency_limit: int,
    expected_bound: int,
) -> None:
    queries = ("one", "two", "three")
    probe = SearchAdmissionProbe(queries)
    adapter = owned_search_index(
        monkeypatch, probe, index_concurrency_limit=index_concurrency_limit
    )
    searches: list[Future[tuple[UUID, ...]]] = []
    try:
        for position, query in enumerate(queries):
            searches.append(submit_search(adapter, probe, query))
            if position < expected_bound:
                probe.wait_for(query, "graph")
        pass_owned_loop(adapter)

        for query in queries[expected_bound:]:
            assert not probe.reached(query, "preflight")
        assert probe.max_active_graph == expected_bound

        # Admission remains held after Graphiti returns, through Cairn's
        # final coverage/scoring query for the partition.
        for query in queries[:expected_bound]:
            probe.release_graph(adapter._loop, query)
            probe.wait_for(query, "fact")
        pass_owned_loop(adapter)
        for query in queries[expected_bound:]:
            assert not probe.reached(query, "preflight")

        probe.release_fact(adapter._loop, queries[0])
        if expected_bound < len(queries):
            probe.wait_for(queries[expected_bound], "preflight")
        probe.release_all(adapter._loop)

        assert [search.result(timeout=2.0) for search in searches] == [
            (UUID(probe.identities[query]),) for query in queries
        ]
        assert probe.max_active_graph == expected_bound
        assert probe.group_calls == {
            query: [derive_group_id(probe.partitions[query])] for query in queries
        }
    finally:
        clean_up_searches(adapter, probe, searches)


def test_full_search_admission_queued_cancellation_does_no_work_or_steal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("active", "cancelled", "successor"))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=1)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    try:
        probe.wait_for("active", "graph")
        queued = submit_search(adapter, probe, "cancelled")
        searches.append(queued)
        pass_owned_loop(adapter)
        assert not probe.reached("cancelled", "preflight")

        queued.cancel()
        with pytest.raises(CancelledError):
            queued.result(timeout=2.0)
        probe.release_graph(adapter._loop, "active")
        probe.wait_for("active", "fact")
        probe.release_fact(adapter._loop, "active")
        assert active.result(timeout=2.0) == (UUID(probe.identities["active"]),)

        successor = submit_search(adapter, probe, "successor")
        searches.append(successor)
        probe.wait_for("successor", "preflight")
        probe.release_all(adapter._loop)
        assert successor.result(timeout=2.0) == (UUID(probe.identities["successor"]),)
        assert not any(query == "cancelled" for query, _ in probe.stages)
    finally:
        clean_up_searches(adapter, probe, searches)


@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_full_search_admission_active_failure_releases_the_permit(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    probe = SearchAdmissionProbe(("active", "successor"))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=1)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    try:
        probe.wait_for("active", "graph")
        successor = submit_search(adapter, probe, "successor")
        searches.append(successor)
        pass_owned_loop(adapter)
        assert not probe.reached("successor", "preflight")

        if outcome == "error":
            probe.graph_errors["active"] = RuntimeError("deliberate search failure")
            probe.release_graph(adapter._loop, "active")
            with pytest.raises(RuntimeError, match="deliberate search failure"):
                active.result(timeout=2.0)
        else:
            active.cancel()
            with pytest.raises(CancelledError):
                active.result(timeout=2.0)

        probe.wait_for("successor", "preflight")
        probe.release_all(adapter._loop)
        assert successor.result(timeout=2.0) == (UUID(probe.identities["successor"]),)
    finally:
        clean_up_searches(adapter, probe, searches)


def test_full_search_admission_outer_timeout_cancels_queued_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("active", "timed-out"))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=1)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    codes: list[str] = []
    try:
        probe.wait_for("active", "graph")

        def call_queued() -> None:
            try:
                adapter._call(
                    adapter._search("timed-out", 1, (probe.partitions["timed-out"],)),
                    timeout=0.1,
                )
            except GraphitiIndexError as error:
                codes.append(error.code)

        caller = threading.Thread(target=call_queued)
        caller.start()
        caller.join(timeout=2.0)
        assert not caller.is_alive()
        assert codes == ["graphiti_timeout"]
        pass_owned_loop(adapter)
        assert not probe.reached("timed-out", "preflight")
    finally:
        clean_up_searches(adapter, probe, searches)


def test_full_search_admission_shutdown_cancels_queued_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("active", "shutdown"))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=1)
    admission = SearchAdmissionAttempts(adapter._search_bound)
    monkeypatch.setattr(adapter, "_search_bound", admission)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    codes: list[str] = []
    try:
        probe.wait_for("active", "graph")

        def call_queued() -> None:
            try:
                adapter._call(
                    adapter._search("shutdown", 1, (probe.partitions["shutdown"],)),
                    timeout=5.0,
                )
            except GraphitiIndexError as error:
                codes.append(error.code)

        caller = threading.Thread(target=call_queued)
        caller.start()
        assert admission.second_attempt.wait(timeout=2.0)
        assert not probe.reached("shutdown", "preflight")
        adapter.request_close()
        caller.join(timeout=2.0)
        assert not caller.is_alive()
        assert codes == ["graphiti_shutdown"]

        active.cancel()
        with pytest.raises(CancelledError):
            active.result(timeout=2.0)
        pass_owned_loop(adapter)
        assert not probe.reached("shutdown", "preflight")
    finally:
        clean_up_searches(adapter, probe, searches)


def test_full_search_admission_does_not_queue_the_blank_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("active",))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=1)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    try:
        probe.wait_for("active", "graph")
        blank = asyncio.run_coroutine_threadsafe(
            adapter._search(" \n\t", 1, ("unused",)), adapter._loop
        )
        assert blank.result(timeout=0.5) == ()
        assert probe.stages == [
            ("active", "preflight"),
            ("active", "embed"),
            ("active", "graph"),
        ]
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_lease_shares_one_clone_for_overlapping_same_group_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("creator", "borrower"))
    probe.partitions["borrower"] = probe.partitions["creator"]
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    driver = cast(SearchAdmissionDriver, adapter._driver)
    creator = submit_search(adapter, probe, "creator")
    borrower = submit_search(adapter, probe, "borrower")
    searches = [creator, borrower]
    try:
        probe.wait_for("creator", "graph")
        probe.wait_for("borrower", "graph")
        group = derive_group_id(probe.partitions["creator"])
        assert len(driver.clones) == 1
        assert adapter._search_driver_leases[group].refcount == 2
        assert sum(stage == "preflight" for _, stage in probe.stages) == 2

        probe.release_graph(adapter._loop, "creator")
        probe.release_graph(adapter._loop, "borrower")
        probe.wait_for("creator", "fact")
        pass_owned_loop(adapter)
        assert adapter._search_driver_leases[group].refcount == 2
        assert sum(stage == "fact" for _, stage in probe.stages) == 2

        probe.release_all(adapter._loop)
        creator.result(timeout=2.0)
        borrower.result(timeout=2.0)
        assert adapter._search_driver_leases == {}
        assert driver.clones[0].closes == 0
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_leases_isolate_overlapping_different_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("one", "two"))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    driver = cast(SearchAdmissionDriver, adapter._driver)
    searches = [
        submit_search(adapter, probe, "one"),
        submit_search(adapter, probe, "two"),
    ]
    try:
        probe.wait_for("one", "graph")
        probe.wait_for("two", "graph")
        assert {clone.database for clone in driver.clones} == {
            derive_group_id(probe.partitions["one"]),
            derive_group_id(probe.partitions["two"]),
        }
        assert len(adapter._search_driver_leases) == 2
        assert all(
            lease.refcount == 1 for lease in adapter._search_driver_leases.values()
        )

        probe.release_all(adapter._loop)
        for search in searches:
            search.result(timeout=2.0)
        assert adapter._search_driver_leases == {}
        assert all(clone.closes == 0 for clone in driver.clones)
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_lease_final_release_requires_a_fresh_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("first", "second"))
    probe.partitions["second"] = probe.partitions["first"]
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    driver = cast(SearchAdmissionDriver, adapter._driver)
    searches: list[Future[tuple[UUID, ...]]] = []
    try:
        first = submit_search(adapter, probe, "first")
        searches.append(first)
        probe.wait_for("first", "graph")
        probe.release_all(adapter._loop)
        first.result(timeout=2.0)
        assert adapter._search_driver_leases == {}

        second = submit_search(adapter, probe, "second")
        searches.append(second)
        probe.wait_for("second", "graph")
        probe.release_all(adapter._loop)
        second.result(timeout=2.0)
        assert len(driver.clones) == 2
        assert driver.clones[0] is not driver.clones[1]
        assert adapter._search_driver_leases == {}
        assert all(clone.closes == 0 for clone in driver.clones)
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_lease_clone_failure_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("broken",))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)

    class BrokenDriver(SearchAdmissionDriver):
        def clone(self, database: str) -> SearchAdmissionClone:
            raise RuntimeError("clone construction failed")

    adapter._driver = cast(Any, BrokenDriver())
    search = submit_search(adapter, probe, "broken")
    searches = [search]
    try:
        with pytest.raises(RuntimeError, match="clone construction failed"):
            search.result(timeout=2.0)
        assert adapter._search_driver_leases == {}
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_lease_release_removes_only_the_exact_leased_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("one",))
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    group = derive_group_id(probe.partitions["one"])

    async def replace_while_borrowed() -> tuple[Any, Any]:
        with adapter._lease_search_driver(group):
            original = adapter._search_driver_leases[group]
            replacement = SimpleNamespace(
                driver=SearchAdmissionClone(group), refcount=1
            )
            adapter._search_driver_leases[group] = cast(Any, replacement)
        return original, replacement

    original, replacement = adapter._call(replace_while_borrowed())
    try:
        assert original.refcount == 0
        assert adapter._search_driver_leases[group] is replacement
    finally:
        adapter._search_driver_leases.clear()
        adapter.close()


def test_search_driver_lease_hit_preserves_shutdown_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("active", "refused"))
    probe.partitions["refused"] = probe.partitions["active"]
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    driver = cast(SearchAdmissionDriver, adapter._driver)
    active = submit_search(adapter, probe, "active")
    searches = [active]
    try:
        probe.wait_for("active", "graph")
        before = sum(stage == "preflight" for _, stage in probe.stages)
        adapter.request_close()
        refused = submit_search(adapter, probe, "refused")
        searches.append(refused)
        with pytest.raises(GraphitiIndexError, match="graphiti_shutdown"):
            refused.result(timeout=2.0)
        searches.remove(refused)
        assert sum(stage == "preflight" for _, stage in probe.stages) == before
        assert len(driver.clones) == 1
        group = derive_group_id(probe.partitions["active"])
        assert adapter._search_driver_leases[group].refcount == 1
    finally:
        clean_up_searches(adapter, probe, searches)


def test_search_driver_leases_release_every_partition_after_search_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = SearchAdmissionProbe(("failing",))
    second_partition = "failing-other\n[]"
    probe.groups[derive_group_id(second_partition)] = "failing"
    probe.graph_errors["failing"] = RuntimeError("partition search failed")
    adapter = owned_search_index(monkeypatch, probe, index_concurrency_limit=2)
    driver = cast(SearchAdmissionDriver, adapter._driver)
    search = asyncio.run_coroutine_threadsafe(
        adapter._search(
            "failing",
            1,
            (probe.partitions["failing"], second_partition),
        ),
        adapter._loop,
    )
    searches = [search]
    try:
        probe.wait_for("failing", "graph")
        assert len(adapter._search_driver_leases) == 2
        probe.release_graph(adapter._loop, "failing")
        with pytest.raises(RuntimeError, match="partition search failed"):
            search.result(timeout=2.0)
        assert adapter._search_driver_leases == {}
        assert len(driver.clones) == 2
        assert all(clone.closes == 0 for clone in driver.clones)
    finally:
        clean_up_searches(adapter, probe, searches)


@pytest.mark.parametrize("query", ["", " ", "\n\t", "\u2003"])
def test_blank_search_is_benign_without_asserting_coverage(query: str) -> None:
    # Deliberately no driver or embedder: blank search must inspect neither.
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    assert asyncio.run(adapter._search(query, 1, ("realm",))) == ()


def test_a_first_projection_saves_pending_then_extracts_then_stamps_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering is the invariant: the episode is pending until
    extraction returns, because until then that is what it is."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    result = asyncio.run(adapter._project(fact()))

    assert result == FactProjected()
    assert store.writes == [
        ("save", _PROJECTION_PENDING),
        ("add_episode", _PROJECTION_PENDING),
        ("save", _PROJECTION_DONE),
    ]
    assert store.episodes[IDENTITY].source_description == _PROJECTION_DONE


def test_the_pre_saved_episode_carries_the_pinned_identity_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search maps episode uuids straight back to fact UUIDs, so the
    identity must be the episode uuid and not an incidental name."""
    store = FakeStore()
    store.install(monkeypatch)
    adapter = index(FakeGraphiti(store), FakeDriver())

    asyncio.run(adapter._project(fact()))

    stored = store.episodes[IDENTITY]
    assert stored.uuid == IDENTITY
    assert stored.content == "the retry budget is four"


def test_completion_stamping_preserves_the_edges_extraction_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The save query replaces the whole property map, so stamping from
    the stale pre-save object would silently discard ``entity_edges``."""
    store = FakeStore()
    store.install(monkeypatch)
    adapter = index(FakeGraphiti(store), FakeDriver())

    asyncio.run(adapter._project(fact()))

    assert store.episodes[IDENTITY].entity_edges == ["edge-a", "edge-b"]


def test_an_absent_episode_is_projected_from_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NodeNotFoundError`` is the store answering "nothing here", and
    the only answer that means it. The projection proceeds; the raise
    never reaches the caller."""
    store = FakeStore()
    store.install(monkeypatch)
    adapter = index(FakeGraphiti(store), FakeDriver())
    assert store.episodes == {}

    assert asyncio.run(adapter._project(fact())) == FactProjected()

    assert store.episodes[IDENTITY].source_description == _PROJECTION_DONE


def test_a_lookup_infrastructure_failure_propagates_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that *cannot answer* is not a store saying the episode is
    absent. Treating the two alike re-saved a completed episode as pending
    over live state and reported the outcome of a question never asked;
    the failure must reach ``_call``, which makes it a retryable attempt."""
    store = FakeStore()
    store.install(monkeypatch)
    store.lookup_failure = ConnectionError("falkordb unreachable")
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    with pytest.raises(ConnectionError):
        asyncio.run(adapter._project(fact()))

    assert store.writes == []
    assert graphiti.add_calls == []


def test_a_legacy_partial_episode_is_resumed_not_mistaken_for_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ad17203`` pre-saved the episode as ``"cairn.fact"`` *before*
    extraction, so every episode it stranded mid-projection carries that
    string. Reusing it as the completion value would have read each one as
    finished — the original defect, preserved for exactly the facts it
    stranded."""
    store = FakeStore()
    store.install(monkeypatch)
    store.write(IDENTITY, "cairn.fact", "the retry budget is four", [])
    store.writes.clear()
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    result = asyncio.run(adapter._project(fact()))

    assert result == FactProjected()
    assert len(graphiti.add_calls) == 1
    assert store.episodes[IDENTITY].source_description == _PROJECTION_DONE
    assert store.episodes[IDENTITY].entity_edges == ["edge-a", "edge-b"]


def test_the_completion_value_is_never_written_before_extraction_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant must not rest on the library continuing to ignore the
    ``source_description`` handed to its update path: if ``add_episode``
    ever applied it, passing the completion value would stamp an episode
    as done before extraction had run."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    asyncio.run(adapter._project(fact()))

    assert graphiti.add_calls[0]["source_description"] == _PROJECTION_PENDING
    extraction = store.writes.index(("add_episode", _PROJECTION_PENDING))
    assert store.writes[:extraction] == [("save", _PROJECTION_PENDING)]


def test_a_completed_episode_is_an_idempotent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())
    asyncio.run(adapter._project(fact()))
    store.writes.clear()

    result = asyncio.run(adapter._project(fact()))

    assert result == FactProjected()
    assert store.writes == []
    assert graphiti.add_calls == [graphiti.add_calls[0]]


def test_a_failed_extraction_leaves_the_episode_pending_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No compensating delete: the marker makes the interruption
    detectable, and ``_call`` maps the raise to a retryable failure."""
    store = FakeStore()
    store.install(monkeypatch)
    adapter = index(FakeGraphiti(store, fails=True), FakeDriver())

    with pytest.raises(RuntimeError):
        asyncio.run(adapter._project(fact()))

    assert store.episodes[IDENTITY].source_description == _PROJECTION_PENDING


def test_an_interrupted_projection_is_resumed_not_mistaken_for_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the defect the invariant exists for: before it, an episode
    left behind by a failed extraction read as a finished projection and
    the outbox row discharged, stranding the fact unextracted forever."""
    store = FakeStore()
    store.install(monkeypatch)
    failing = FakeGraphiti(store, fails=True)
    adapter = index(failing, FakeDriver())
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._project(fact()))

    recovering = FakeGraphiti(store)
    resumed = index(recovering, FakeDriver())
    result = asyncio.run(resumed._project(fact()))

    assert result == FactProjected()
    assert len(recovering.add_calls) == 1
    assert store.episodes[IDENTITY].source_description == _PROJECTION_DONE
    assert store.episodes[IDENTITY].entity_edges == ["edge-a", "edge-b"]


def test_projection_targets_the_partitions_own_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    store.install(monkeypatch)
    driver = FakeDriver()
    adapter = index(FakeGraphiti(store), driver)

    asyncio.run(adapter._project(fact()))

    assert driver.cloned == [derive_group_id(PARTITION)]


# --- P-82: the batched projection path -----------------------------------


def test_project_many_pre_saves_all_pending_then_bulks_then_stamps_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single path's two-write protocol, batched: every episode is
    pending until the one bulk extraction returns, and only then stamped."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    driver = FakeDriver()
    adapter = index(graphiti, driver)

    results = asyncio.run(adapter._project_many((fact(), fact(OTHER_IDENTITY))))

    assert results == (FactProjected(), FactProjected())
    assert graphiti.bulk_calls == [(IDENTITY, OTHER_IDENTITY)]
    assert graphiti.add_calls == []
    assert store.writes == [
        ("save", _PROJECTION_PENDING),
        ("save", _PROJECTION_PENDING),
        ("add_episode_bulk", "2"),
        ("save", _PROJECTION_DONE),
        ("save", _PROJECTION_DONE),
    ]
    assert store.episodes[IDENTITY].source_description == _PROJECTION_DONE
    assert store.episodes[OTHER_IDENTITY].source_description == _PROJECTION_DONE
    assert driver.cloned == [derive_group_id(PARTITION)]


def test_project_many_short_circuits_already_completed_episodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-38's marker, not existence: a completed episode is the idempotent
    no-op and never re-enters the bulk call."""
    store = FakeStore()
    store.install(monkeypatch)
    store.write(IDENTITY, _PROJECTION_DONE, "already projected", ["edge-a"])
    store.writes.clear()
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    results = asyncio.run(adapter._project_many((fact(), fact(OTHER_IDENTITY))))

    assert results == (FactProjected(), FactProjected())
    assert graphiti.bulk_calls == [(OTHER_IDENTITY,)]
    assert store.episodes[IDENTITY].entity_edges == ["edge-a"]
    assert store.episodes[OTHER_IDENTITY].source_description == _PROJECTION_DONE


def test_project_many_of_nothing_makes_no_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty batch returns before the loop bridge is ever involved."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    adapter = index(graphiti, FakeDriver())

    assert adapter.project_many(()) == ()
    assert graphiti.bulk_calls == []
    assert store.writes == []


@pytest.mark.parametrize(
    ("chunk", "expected_timeout"),
    [(2, 600.0), (6, 1800.0), (500, 1800.0)],
)
def test_a_bulk_deadline_scales_with_chunk_size_capped_at_the_bulk_bound(
    chunk: int, expected_timeout: float
) -> None:
    """Third-review Spec finding 2: a flat 30-minute bulk deadline widens
    small-chunk failure exposure — a two-fact chunk could hold the
    single-consumer delivery gate for 1,800 seconds where two per-fact
    calls bound it at 600. The deadline is per-fact-proportional and
    capped: ``min(1800, n × 300)``."""
    adapter = index(FakeGraphiti(FakeStore()), FakeDriver())
    captured: list[float] = []

    def capture(
        coroutine: object, *, timeout: float, contain_tasks: bool
    ) -> tuple[object, ...]:
        coroutine.close()  # type: ignore[attr-defined]
        captured.append(timeout)
        assert contain_tasks is True
        return ()

    adapter._call = capture  # type: ignore[assignment,method-assign]
    states = tuple(fact(str(UUID(int=position + 1))) for position in range(chunk))

    adapter.project_many(states)

    assert captured == [expected_timeout]


def test_a_failed_contained_call_cancels_and_drains_its_children() -> None:
    """graphiti-core's ``semaphore_gather`` uses bare ``asyncio.gather``:
    its first exception returns while siblings keep running. The retained
    sweep observed 390–429 child tasks after one bulk timeout. Cairn must not
    enter per-fact fallback until that call's children have been cancelled
    and their cleanup has completed."""
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    child_drained = threading.Event()
    try:
        child_started = asyncio.Event()

        async def child() -> None:
            child_started.set()
            try:
                await asyncio.sleep(120.0)
            finally:
                await asyncio.sleep(0.05)
                child_drained.set()

        async def fail_after_spawning() -> None:
            asyncio.create_task(child())
            await child_started.wait()
            raise RuntimeError("adapter failure")

        with pytest.raises(GraphitiIndexError) as raised:
            adapter._call(fail_after_spawning(), contain_tasks=True)

        assert raised.value.code == "graphiti_unavailable"
        assert child_drained.is_set()
        assert adapter._scoped_tasks == {}
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_contained_failure_does_not_cancel_an_uncontained_call() -> None:
    """Containment is per adapter call. Retrieval or another partition may
    legitimately share the owned loop; a bulk failure is not authority to
    cancel unrelated work."""
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    healthy_started = threading.Event()
    healthy_finished = threading.Event()
    healthy_result: list[str] = []
    try:

        async def healthy_child() -> str:
            healthy_started.set()
            await asyncio.sleep(0.15)
            healthy_finished.set()
            return "healthy"

        async def healthy_call() -> str:
            task = asyncio.create_task(healthy_child())
            return await task

        def run_healthy() -> None:
            healthy_result.append(adapter._call(healthy_call()))

        thread = threading.Thread(target=run_healthy)
        thread.start()
        assert healthy_started.wait(timeout=2.0)

        async def failing_child() -> None:
            await asyncio.sleep(120.0)

        async def failing_call() -> None:
            asyncio.create_task(failing_child())
            await asyncio.sleep(0)
            raise RuntimeError("adapter failure")

        with pytest.raises(GraphitiIndexError):
            adapter._call(failing_call(), contain_tasks=True)

        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert healthy_finished.is_set()
        assert healthy_result == ["healthy"]
        assert adapter._scoped_tasks == {}
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_a_timed_out_contained_call_drains_before_returning() -> None:
    """The bridge cancels its submitted future on its own deadline. That
    cancellation must still run the scope cleanup before the synchronous
    caller can demote into fallback."""
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    child_drained = threading.Event()
    try:

        async def child() -> None:
            try:
                await asyncio.sleep(120.0)
            finally:
                await asyncio.sleep(0.05)
                child_drained.set()

        async def hang_after_spawning() -> None:
            asyncio.create_task(child())
            await asyncio.sleep(120.0)

        with pytest.raises(GraphitiIndexError) as raised:
            adapter._call(hang_after_spawning(), timeout=0.1, contain_tasks=True)

        assert raised.value.code == "graphiti_timeout"
        assert child_drained.is_set()
        assert adapter._scoped_tasks == {}
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_cleanup_overrun_poisons_the_adapter_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a child does not finish cancellation within the bridge's bound,
    returning the bulk timeout is safe only when later calls cannot submit
    fallback work to the same loop. The poison is permanent for this adapter:
    a process restart, not an optimistic later call, restores service."""
    monkeypatch.setattr(graphiti_module, "_TASK_CLEANUP_WAIT_SECONDS", 0.01)
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    child_drained = threading.Event()
    fallback_started = threading.Event()
    try:

        async def slow_cancellation() -> None:
            try:
                await asyncio.sleep(120.0)
            finally:
                await asyncio.sleep(0.15)
                child_drained.set()

        async def hang_after_spawning() -> None:
            asyncio.create_task(slow_cancellation())
            await asyncio.sleep(120.0)

        with pytest.raises(GraphitiIndexError) as timed_out:
            adapter._call(hang_after_spawning(), timeout=0.05, contain_tasks=True)

        assert timed_out.value.code == "graphiti_timeout"
        assert adapter._containment_breached.is_set()

        async def fallback() -> None:
            fallback_started.set()

        with pytest.raises(GraphitiIndexError) as blocked:
            adapter._call(fallback())

        assert blocked.value.code == "graphiti_containment_failed"
        assert not fallback_started.is_set()
        assert child_drained.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while adapter._scoped_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter._scoped_tasks == {}
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_prestarted_shutdown_closes_contained_work_without_poisoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close request already present at the bridge is not a cleanup
    failure. The work must be closed before submission, even while the owned
    loop is busy, and the otherwise healthy adapter must not be poisoned."""
    monkeypatch.setattr(graphiti_module, "_TASK_CLEANUP_WAIT_SECONDS", 0.01)
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    adapter._close_requested = threading.Event()
    adapter._close_requested.set()
    adapter._containment_breached = threading.Event()
    work_started = threading.Event()

    async def work() -> None:
        work_started.set()

    pending = work()
    with pytest.raises(GraphitiIndexError) as raised:
        adapter._call(pending, contain_tasks=True)

    assert raised.value.code == "graphiti_shutdown"
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    assert not work_started.is_set()
    assert not adapter._containment_breached.is_set()
    adapter._loop.close()


def test_prestart_timeout_closes_work_without_poisoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the loop cannot start a contained submission before its deadline,
    no Graphiti child exists to drain. Closing that unstarted work is enough;
    poisoning is reserved for a scope that actually started and failed to
    become quiescent."""
    monkeypatch.setattr(graphiti_module, "_TASK_CLEANUP_WAIT_SECONDS", 0.01)
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    adapter._loop.set_task_factory(adapter._scoped_task_factory)
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def block_loop() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=2.0)

    adapter._loop.call_soon(block_loop)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    assert blocker_started.wait(timeout=1.0)
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    work_started = threading.Event()
    try:

        async def work() -> None:
            work_started.set()

        pending = work()
        with pytest.raises(GraphitiIndexError) as raised:
            adapter._call(pending, timeout=0.05, contain_tasks=True)

        assert raised.value.code == "graphiti_timeout"
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
        assert not work_started.is_set()
        assert not adapter._containment_breached.is_set()

        release_blocker.set()
        assert adapter._call(asyncio.sleep(0)) is None
    finally:
        release_blocker.set()
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_a_queued_wrapper_does_not_touch_abandoned_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop may create the wrapper Task, then run another callback before
    the Task's first step. If the bridge times out in that gap, the wrapper
    must observe abandonment and exit cleanly instead of awaiting the closed
    inner coroutine."""
    monkeypatch.setattr(graphiti_module, "_TASK_CLEANUP_WAIT_SECONDS", 0.01)
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._scoped_tasks = {}
    wrapper_created = threading.Event()
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def block_before_first_step() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=2.0)

    def gate_first_task(
        loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        if not wrapper_created.is_set():
            loop.call_soon(block_before_first_step)
            wrapper_created.set()
        return adapter._scoped_task_factory(loop, coroutine, context)

    adapter._loop.set_task_factory(gate_first_task)
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._containment_breached = threading.Event()
    work_started = threading.Event()
    touched_after_abandonment = threading.Event()
    try:

        async def work() -> None:
            work_started.set()

        class WorkProbe:
            def __init__(self) -> None:
                self.inner = work()
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self.inner.close()

            def __await__(self) -> Any:
                if self.closed:
                    touched_after_abandonment.set()
                return self.inner.__await__()

        probe = WorkProbe()
        pending = cast(Coroutine[object, object, None], probe)
        with pytest.raises(GraphitiIndexError) as raised:
            adapter._call(pending, timeout=0.05, contain_tasks=True)

        assert raised.value.code == "graphiti_timeout"
        assert wrapper_created.is_set()
        assert blocker_started.is_set()
        assert probe.closed
        assert not work_started.is_set()
        assert not adapter._containment_breached.is_set()

        release_blocker.set()
        assert adapter._call(asyncio.sleep(0)) is None
        assert adapter._call(asyncio.sleep(0)) is None
        assert not touched_after_abandonment.is_set()
    finally:
        release_blocker.set()
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


def test_a_bulk_failure_propagates_and_stamps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bulk call leaves every episode pending-marked — resumable
    by the marker logic — and the exception reaches the caller whole; the
    deliverer's chunk demotion owns recovery, never the adapter."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store, fails=True)
    adapter = index(graphiti, FakeDriver())

    with pytest.raises(RuntimeError):
        asyncio.run(adapter._project_many((fact(), fact(OTHER_IDENTITY))))

    assert store.episodes[IDENTITY].source_description == _PROJECTION_PENDING
    assert store.episodes[OTHER_IDENTITY].source_description == _PROJECTION_PENDING


def test_completion_stamping_preserves_bulk_extraction_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As on the single path: the save query replaces the whole property
    map, so stamping from a stale pre-save object would silently discard
    the ``entity_edges`` the bulk extraction produced."""
    store = FakeStore()
    store.install(monkeypatch)
    adapter = index(FakeGraphiti(store), FakeDriver())

    asyncio.run(adapter._project_many((fact(),)))

    assert store.episodes[IDENTITY].entity_edges == ["edge-a", "edge-b"]


def test_an_incomplete_bulk_result_fails_whole_and_stamps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec verification item 4, under the adapter rule: the returned
    ``AddBulkEpisodeResults.episodes`` set is validated as complete before
    any marker is stamped. An omitted episode fails the whole call — no
    partial confirm — and every episode stays pending-marked, resumable
    by the P-38 marker logic."""
    store = FakeStore()
    store.install(monkeypatch)
    graphiti = FakeGraphiti(store)
    graphiti.omit_from_results.add(OTHER_IDENTITY)
    adapter = index(graphiti, FakeDriver())

    with pytest.raises(GraphitiIndexError) as raised:
        asyncio.run(adapter._project_many((fact(), fact(OTHER_IDENTITY))))

    assert raised.value.code == _BULK_INCOMPLETE
    assert store.episodes[IDENTITY].source_description == _PROJECTION_PENDING
    assert store.episodes[OTHER_IDENTITY].source_description == _PROJECTION_PENDING


def test_a_close_request_interrupts_a_waiting_call_quickly() -> None:
    """I-20: shutdown never waits out a call deadline. A close request
    lands within one poll slice, cancels the in-flight coroutine and
    surfaces as an infrastructure failure the deliverer records
    fail-closed."""
    adapter = GraphitiIndex.__new__(GraphitiIndex)
    adapter._loop = asyncio.new_event_loop()
    adapter._thread = threading.Thread(target=adapter._loop.run_forever, daemon=True)
    adapter._thread.start()
    adapter._close_requested = threading.Event()
    adapter._close_guard = threading.Lock()
    adapter._close_deadline = None
    adapter._containment_breached = threading.Event()
    try:

        async def hang() -> None:
            await asyncio.sleep(120.0)

        def request_soon() -> None:
            time.sleep(0.3)
            adapter.request_close()

        threading.Thread(target=request_soon, daemon=True).start()
        started = time.monotonic()
        with pytest.raises(GraphitiIndexError) as raised:
            adapter._call(hang(), timeout=120.0)
        assert raised.value.code == "graphiti_shutdown"
        assert time.monotonic() - started < 10.0
    finally:
        adapter._loop.call_soon_threadsafe(adapter._loop.stop)
        adapter._thread.join(timeout=5.0)
        adapter._loop.close()


# --- P-48 as amended: limit is a fetch bound, not a return cap ----------


def test_episode_and_edge_identities_both_become_candidates() -> None:
    """Episode BM25 is the recall floor — an episode exists for every
    projected fact — with the edge layer's semantic reach on top."""
    adapter = index(
        FakeGraphiti(FakeStore(), episodes=(IDENTITY,), edges=((OTHER_IDENTITY,),)),
        FakeDriver(),
    )

    candidates = asyncio.run(adapter._search("q", 10, (PARTITION,)))

    assert set(candidates) == {UUID(IDENTITY), UUID(OTHER_IDENTITY)}


def test_an_episode_only_result_still_recalls() -> None:
    """The live defect: a one-clause body extracts no edges, and an
    edge-only search left the fact invisible to its own search."""
    adapter = index(FakeGraphiti(FakeStore(), episodes=(IDENTITY,)), FakeDriver())

    assert asyncio.run(adapter._search("q", 10, (PARTITION,))) == (UUID(IDENTITY),)


def test_an_edge_only_result_still_recalls() -> None:
    adapter = index(FakeGraphiti(FakeStore(), edges=((IDENTITY,),)), FakeDriver())

    assert asyncio.run(adapter._search("q", 10, (PARTITION,))) == (UUID(IDENTITY),)


def test_overlapping_episode_and_edge_hits_are_deduplicated() -> None:
    adapter = index(
        FakeGraphiti(FakeStore(), episodes=(IDENTITY,), edges=((IDENTITY, IDENTITY),)),
        FakeDriver(),
    )

    assert asyncio.run(adapter._search("q", 10, (PARTITION,))) == (UUID(IDENTITY),)


def test_foreign_identities_never_become_candidates() -> None:
    """A co-tenant or hostile store can hold anything; anything that is
    not a canonical fact UUID is dropped rather than reconciled."""
    adapter = index(
        FakeGraphiti(
            FakeStore(),
            episodes=("not-a-uuid", IDENTITY),
            edges=(("urn:uuid:" + IDENTITY,),),
        ),
        FakeDriver(),
    )

    assert asyncio.run(adapter._search("q", 10, (PARTITION,))) == (UUID(IDENTITY),)


def test_results_exceeding_the_limit_are_not_truncated() -> None:
    """P-48 as amended: ``limit`` bounds the fetch, not the return.
    Truncating here would decide candidate membership, which is upstream
    of everything I-82 orders."""
    adapter = index(
        FakeGraphiti(FakeStore(), episodes=(IDENTITY,), edges=((OTHER_IDENTITY,),)),
        FakeDriver(),
    )

    candidates = asyncio.run(adapter._search("q", 1, (PARTITION,)))

    assert len(candidates) == 2


def test_a_later_ancestry_partition_is_never_starved_by_an_earlier_one() -> None:
    """The realm root is searched first. Under the old global slice it
    could consume the whole allowance and the request's own scope — the
    most specific, usually the most relevant — contributed nothing."""
    adapter = index(
        FakeGraphiti(FakeStore(), episodes=(IDENTITY, OTHER_IDENTITY)),
        FakeDriver(),
    )

    candidates = asyncio.run(adapter._search("q", 1, (PARENT_PARTITION, PARTITION)))

    assert len(candidates) == 2


def test_the_limit_is_passed_to_each_partition_as_its_fetch_bound() -> None:
    graphiti = FakeGraphiti(FakeStore(), episodes=(IDENTITY,))
    adapter = index(graphiti, FakeDriver())

    asyncio.run(adapter._search("q", 7, (PARENT_PARTITION, PARTITION)))

    assert [config.limit for config in graphiti.search_configs] == [7, 7]


def test_each_ancestry_partition_is_searched_in_its_own_database() -> None:
    """P-48's fan-out: the library scopes a search to its driver's current
    database, so one search cannot span the ancestry chain."""
    driver = FakeDriver()
    graphiti = FakeGraphiti(FakeStore())
    adapter = index(graphiti, driver)

    asyncio.run(adapter._search("q", 10, (PARENT_PARTITION, PARTITION)))

    assert driver.cloned == [
        derive_group_id(PARENT_PARTITION),
        derive_group_id(PARTITION),
    ]
    assert graphiti.search_group_ids == [
        [derive_group_id(PARENT_PARTITION)],
        [derive_group_id(PARTITION)],
    ]


def test_the_search_config_selects_episode_bm25_and_the_edge_layer() -> None:
    """Recall rests on this configuration: the default ``search()`` is
    edge-only, which is what made a projected fact invisible."""
    graphiti = FakeGraphiti(FakeStore())
    adapter = index(graphiti, FakeDriver())

    asyncio.run(adapter._search("q", 10, (PARTITION,)))

    config = graphiti.search_configs[0]
    assert config.episode_config is not None
    assert config.edge_config is not None
