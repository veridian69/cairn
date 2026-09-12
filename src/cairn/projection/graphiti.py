"""The real Graphiti index adapter (P-39 as amended, P-48).

One ``GraphitiIndex`` owns one FalkorDB connection and one private asyncio
event loop on a dedicated thread. Graphiti's API is async; this port is
synchronous and runs on ``anyio.to_thread`` worker threads, so every call
submits its coroutine to the adapter's own loop and blocks for the result
(P-48). A single long-lived loop, rather than ``asyncio.run`` per call,
because the Graphiti and FalkorDB clients bind their connections to the
loop that created them.

Partition handling is the P-39 split: callers speak canonical partition
encodings — the Cairn identity — and this module derives the Graphiti
``group_id`` spelling at the boundary via ``partition.derive_group_id``.
Under FalkorDB each ``group_id`` is its own graph (the library clones its
driver per database on the write path, and P-48's fan-out does the
equivalent for search), so a partition's blast radius is one graph.

Infrastructure failures raise ``GraphitiIndexError`` with a safe code and
no content, mirroring ``AtticStorageError``; the deliverer records them as
retryable attempts. Default tests do not provision FalkorDB. Opt-in owned
engine fixtures substitute controlled provider clients; provider-backed checks
remain separately authorised gates.
"""

import asyncio
import logging
import re
import threading
import time
from collections.abc import Coroutine, Iterator, Sequence
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, replace
from enum import Enum
from importlib.metadata import version
from typing import Any, cast
from uuid import UUID

import httpx
import uvloop
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.driver import GraphDriver, GraphDriverSession
from graphiti_core.driver.falkordb_driver import FalkorDriver, FalkorDriverSession
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.openai import OpenAIEmbedder
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search import search as graphiti_search
from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    EpisodeReranker,
    EpisodeSearchConfig,
    EpisodeSearchMethod,
    SearchConfig,
    SearchResults,
)
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.utils.bulk_utils import RawEpisode
from graphiti_core.utils.maintenance.graph_data_operations import clear_data
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import BaseModel

from cairn.projection import graphiti_bulk as graphiti_bulk_module
from cairn.projection.adapter import (
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.fact_unit_index import FactUnitIndex
from cairn.projection.fact_unit_index import policy_metadata as unit_policy_metadata
from cairn.projection.fact_vectors import (
    FactRepresentation,
    FactVectorError,
    FactVectorIndex,
    _ready,
    policy_metadata,
)
from cairn.projection.graphiti_edge_batch import install_edge_batching
from cairn.projection.graphiti_extraction_cache import (
    _CacheStore,
    build_caching_embedder,
    cached_extract_nodes_and_edges_bulk,
    install_edge_timestamp_caching,
    search_embedding_passthrough,
)
from cairn.projection.partition import derive_group_id
from cairn.projection.semantic_evidence import (
    PARTITION_LIMIT,
    SEARCH_POLICY,
    FactGrade,
    PartitionGrades,
    SemanticEvidence,
    SemanticEvidenceError,
    SemanticEvidenceSource,
    local_representation_sha256,
    query_sha256,
)
from cairn.runtime.logging import LogEvent, SafeLogger

_QUERY_MAX_BYTES = 8 * 1024
# The derivation's output shape: how ``clear(None)`` recognises graphs
# this adapter created, and nothing else, on the FalkorDB workload I-13
# dedicates to Cairn.
_DERIVED_GRAPH_NAME = re.compile(r"[0-9a-f]{64}\Z")
_CALL_TIMEOUT_SECONDS = 300.0
# P-82: a bulk call carries a whole chunk, so its deadline scales with the
# chunk — per-fact proportional, ``n × _CALL_TIMEOUT_SECONDS`` — under this
# cap: 30 minutes covers the largest permitted chunk (500) at the
# rehearsal's measured rate with margin, while a small chunk cannot hold
# the single-consumer loop longer than its per-fact equivalent would and a
# hung call cannot hold it for hours. Shutdown never waits for either
# deadline — ``_call`` polls in short slices and a close request cancels
# the in-flight coroutine within one slice (I-20). The chunk-size sweep
# replaces the estimate behind this cap with a measurement.
_BULK_TIMEOUT_SECONDS = 1800.0
_CALL_POLL_SECONDS = 1.0
_BULK_INCOMPLETE = "bulk_incomplete"
_GRAPHITI_CORE_VERSION = version("graphiti-core")
_GRAPHITI_EDGE_SEARCH_COMPATIBILITY_VERSION = "0.29.3"
_GRAPHITI_EDGE_REMATCH = """YIELD relationship AS rel, score
    MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)"""
_GRAPHITI_EDGE_DIRECT = """YIELD relationship AS e, score
    WITH e, score, startNode(e) AS n, endNode(e) AS m"""
_GRAPHITI_TASK_SCOPE: ContextVar[object | None] = ContextVar(
    "cairn_graphiti_task_scope", default=None
)
_TASK_CLEANUP_WAIT_SECONDS = 5.0
_PROVIDER_KEEPALIVE_EXPIRY: float | None = None
_PROVIDER_MAX_CONNECTIONS = 1000
_PROVIDER_MAX_KEEPALIVE_CONNECTIONS = 100
_PROVIDER_MEDIUM_MODEL = "gpt-5.4-nano"
_PROVIDER_SMALL_MODEL = "gpt-4.1-nano"


def _openai_provider_clients() -> tuple[
    AsyncOpenAI, OpenAIClient, OpenAIEmbedder, OpenAIRerankerClient
]:
    """One provider client and pool for Graphiti's three OpenAI roles.

    The OpenAI SDK otherwise gives each role its own pool and expires idle
    connections after five seconds. Graphiti's stage gaps repeatedly cross
    that boundary, turning the next burst into DNS, TCP and TLS churn. The
    SDK's connection ceilings and timeout behaviour stay unchanged; only the
    pool ownership and idle lifetime differ.
    """
    http_client = DefaultAsyncHttpxClient(
        limits=httpx.Limits(
            max_connections=_PROVIDER_MAX_CONNECTIONS,
            max_keepalive_connections=_PROVIDER_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_PROVIDER_KEEPALIVE_EXPIRY,
        )
    )
    shared = AsyncOpenAI(http_client=http_client)
    llm_config = LLMConfig(
        model=_PROVIDER_MEDIUM_MODEL,
        small_model=_PROVIDER_SMALL_MODEL,
    )
    return (
        shared,
        OpenAIClient(config=llm_config, client=shared, reasoning="none"),
        OpenAIEmbedder(client=shared),
        OpenAIRerankerClient(
            config=LLMConfig(model=_PROVIDER_SMALL_MODEL),
            client=shared,
        ),
    )


def _rewrite_graphiti_edge_search(cypher: str) -> str:
    """Compatibility seam for graphiti-core 0.29.3 upstream #1272/#1506.

    Falkor's full-text procedure already returns the relationship. Graphiti
    nevertheless re-MATCHes every hit by UUID before applying ``LIMIT``;
    Falkor plans that as a label/edge scan per hit. The retained P-82 sweep
    measured the procedure itself returning 528 hits in 1 ms, the first 20
    plus their MATCH in 198 ms, and this shipped query timing out after five
    seconds on only 1,456 relationships.

    Replace the one exact defective fragment and nothing broader. All filters,
    result projection, score ordering and limiting remain Graphiti's. If a
    later dependency still emits this fragment, refuse: its source must be
    reviewed before Cairn carries the workaround forward. If upstream removes
    the fragment, this becomes a no-op and can be deleted with the version pin.
    """
    occurrences = cypher.count(_GRAPHITI_EDGE_REMATCH)
    if occurrences == 0:
        return cypher
    if (
        _GRAPHITI_CORE_VERSION != _GRAPHITI_EDGE_SEARCH_COMPATIBILITY_VERSION
        or occurrences != 1
    ):
        raise RuntimeError("graphiti_compatibility_version")
    return cypher.replace(_GRAPHITI_EDGE_REMATCH, _GRAPHITI_EDGE_DIRECT, 1)


class GraphitiIndexError(Exception):
    """Adapter infrastructure failure. ``code`` is a safe identifier;
    the triggering exception is chained for operator logs but its text
    must never reach a public failure (I-32)."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.close_result: _CloseResult | None = None
        super().__init__(f"graphiti index error: {code}")


@dataclass
class _CloseResult:
    """Bounded, content-free outcomes; observation never clears a failure."""

    first_code: str | None = None
    init_failed: bool = False
    client_failed: bool = False
    provider_failed: bool = False
    cleanup_unverified: bool = False

    def fail(self, code: str) -> None:
        if self.first_code is None:
            self.first_code = code

    def unverified(self) -> None:
        self.cleanup_unverified = True
        self.fail("graphiti_cleanup_unverified")

    def raise_if_failed(self) -> None:
        if self.first_code is not None:
            error = GraphitiIndexError(self.first_code)
            error.close_result = replace(self)
            raise error


@dataclass(eq=False)
class _InitRecord:
    task: asyncio.Task[Any] | None = None
    coroutine: Coroutine[Any, Any, None] | None = None
    cancel_requested: bool = False


@dataclass(eq=False)
class _CleanupRecord:
    code: str
    work: Coroutine[Any, Any, None]
    task: asyncio.Task[None] | None = None
    runner: Coroutine[Any, Any, None] | None = None


class _DriverInitOwner:
    """One loop-bound family, retaining live obligations but no task history."""

    def __init__(self, requested: threading.Event | None = None) -> None:
        self.requested = requested if requested is not None else threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = "OPEN"
        self.active: set[_InitRecord] = set()
        self.cleanup: set[_CleanupRecord] = set()
        self.client: Any = None
        self.result = _CloseResult()
        self.deadline: float | None = None
        self.shutdown: asyncio.Task[None] | None = None
        self._shutdown_coroutine: Coroutine[Any, Any, None] | None = None
        self._closed_result: _CloseResult | None = None

    def check_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Dormant construction/session creation schedules no work.
        if self.loop is None:
            self.loop = loop
        elif self.loop is not loop:
            raise GraphitiIndexError("graphiti_wrong_loop")

    def admit(self, init_record: _InitRecord | None = None) -> None:
        self.check_loop()
        if self.state != "OPEN" or self.requested.is_set():
            if (
                init_record is not None
                and init_record in self.active
                and init_record.task is asyncio.current_task()
            ):
                # A running schema build can reach its next admission before
                # the shutdown coordinator gets to cancel it. Only this owned
                # task receives cancellation; external work still gets refusal.
                init_record.cancel_requested = True
                raise asyncio.CancelledError
            raise GraphitiIndexError("graphiti_shutdown")

    def reserve(self) -> _InitRecord:
        self.admit()
        record = _InitRecord()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return record  # Dormant handles have no scheduled obligation.
        self.active.add(record)
        return record

    def attach(self, record: _InitRecord, task: asyncio.Task[Any]) -> None:
        self.check_loop()
        if record.task is task:
            return
        if record not in self.active or record.task is not None:
            self.unverified()
            raise GraphitiIndexError("graphiti_init_handoff_failed")
        record.task = task
        task.add_done_callback(lambda _: self.observe(record))

    def observe(self, record: _InitRecord) -> None:
        task = record.task
        if task is None or not task.done():
            return
        failed = (
            not record.cancel_requested
            if task.cancelled()
            else task.exception() is not None
        )
        if failed:
            self.result.init_failed = True
            # Resource failures are secondary even if their independent close
            # finishes before this init's cancellation/failure is observed.
            self.result.first_code = "graphiti_init_failed"
        self.active.discard(record)

    def unverified(self) -> None:
        self.result.unverified()
        self.state = "UNVERIFIED"

    async def ready(self, record: _InitRecord) -> None:
        self.admit()
        try:
            if record.task is not None:
                await asyncio.shield(record.task)
        finally:
            self.observe(record)

    async def settle(self, records: tuple[_InitRecord, ...], deadline: float) -> None:
        pending = set()
        for record in records:
            task = record.task
            if task is not None and not task.done():
                record.cancel_requested = True
                task.cancel()
                pending.add(task)
        if pending:
            await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        for record in records:
            self.observe(record)
        if any(record in self.active for record in records):
            self.unverified()

    def _observe_cleanup(self, record: _CleanupRecord) -> None:
        task = record.task
        if task is None or not task.done():
            return
        failed = task.cancelled() or task.exception() is not None
        if failed:
            if record.code == "graphiti_client_close_failed":
                self.result.client_failed = True
            else:
                self.result.provider_failed = True
            self.result.fail(record.code)
        self.cleanup.discard(record)

    async def _run_cleanup(self, record: _CleanupRecord) -> None:
        record.task = asyncio.current_task()
        assert record.task is not None
        record.task.add_done_callback(lambda _: self._observe_cleanup(record))
        await record.work

    def _start_cleanup(self, coroutine: Coroutine[Any, Any, None], code: str) -> None:
        record = _CleanupRecord(code, coroutine)
        self.cleanup.add(record)
        record.runner = self._run_cleanup(record)
        try:
            record.task = asyncio.create_task(record.runner)
        except Exception:
            self.unverified()  # Retain the reservation, including uncertain scheduling.

    async def _close_provider(self, provider: Any) -> None:
        await provider.close()

    async def _close_client(self) -> None:
        # Pinned FalkorDriver 0.29.3 selection, without per-clone task/client close.
        if _GRAPHITI_CORE_VERSION != "0.29.3":
            raise GraphitiIndexError("graphiti_init_compatibility")
        if hasattr(self.client, "aclose"):
            await self.client.aclose()
        elif hasattr(self.client.connection, "aclose"):
            await self.client.connection.aclose()
        elif hasattr(self.client.connection, "close"):
            await self.client.connection.close()

    async def _shutdown(self, provider: Any) -> None:
        assert self.deadline is not None
        if self.shutdown is None:
            self.shutdown = asyncio.current_task()
            assert self.shutdown is not None
            self.shutdown.add_done_callback(self._observe_shutdown)
        if provider is not None:
            self._start_cleanup(
                self._close_provider(provider), "graphiti_provider_close_failed"
            )
        await self.settle(tuple(self.active), self.deadline)
        # On expiry this is emergency transport shutdown, not proof init exited.
        if self.client is not None:
            self._start_cleanup(self._close_client(), "graphiti_client_close_failed")
        tasks = [record.task for record in self.cleanup if record.task is not None]
        if tasks:
            await asyncio.wait(
                tasks,
                timeout=max(0.0, self.deadline - time.monotonic()),
            )
        for record in tuple(self.cleanup):
            self._observe_cleanup(record)
        if self.active or self.cleanup or time.monotonic() > self.deadline:
            self.unverified()
        elif not self.result.cleanup_unverified:
            self.state = "CLOSED"

    def _observe_shutdown(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.unverified()

    async def close(
        self, provider: Any = None, *, deadline: float | None = None
    ) -> None:
        self.check_loop()
        if self.deadline is None:
            self.requested.set()
            if self.state == "OPEN":
                self.state = "CLOSING"
            self.deadline = (
                deadline
                if deadline is not None
                else time.monotonic() + _CALL_TIMEOUT_SECONDS
            )
            token = _GRAPHITI_TASK_SCOPE.set(None)
            self._shutdown_coroutine = self._shutdown(provider)
            try:
                self.shutdown = asyncio.create_task(self._shutdown_coroutine)
                self.shutdown.add_done_callback(self._observe_shutdown)
            except Exception:
                self.unverified()
            finally:
                _GRAPHITI_TASK_SCOPE.reset(token)
        if self.shutdown is not None:
            try:
                await asyncio.shield(self.shutdown)
            except Exception:
                self.unverified()
        if self._closed_result is None:
            self._closed_result = replace(self.result)
        self._closed_result.raise_if_failed()


class _ContainmentPhase(Enum):
    PENDING = "pending"
    RUNNING = "running"
    ABANDONED = "abandoned"
    FINISHED = "finished"


class _ContainedCall[ResultT]:
    """One atomic hand-off between the bridge and its loop wrapper."""

    def __init__(self, coroutine: Coroutine[object, object, ResultT]) -> None:
        self.coroutine = coroutine
        self.finished = threading.Event()
        self._guard = threading.Lock()
        self._phase = _ContainmentPhase.PENDING

    def begin(self) -> Coroutine[object, object, ResultT] | None:
        """Claim pending work, or decline work the bridge abandoned."""
        with self._guard:
            if self._phase is _ContainmentPhase.ABANDONED:
                return None
            if self._phase is not _ContainmentPhase.PENDING:
                raise RuntimeError("graphiti_containment_state")
            self._phase = _ContainmentPhase.RUNNING
            return self.coroutine

    def abandon_if_not_running(self) -> bool:
        """Close pending work atomically; finished work is already safe."""
        with self._guard:
            if self._phase is _ContainmentPhase.PENDING:
                self._phase = _ContainmentPhase.ABANDONED
                self.coroutine.close()
                return True
            return self._phase in {
                _ContainmentPhase.ABANDONED,
                _ContainmentPhase.FINISHED,
            }

    def finish(self) -> None:
        with self._guard:
            self._phase = _ContainmentPhase.FINISHED
        self.finished.set()


class _BoundedFalkorDriverSession(FalkorDriverSession):
    """The session arm of ``BoundedFalkorDriver``'s bound: graphiti's bulk
    node-and-edge writes run through ``driver.session()``, not
    ``execute_query``, so a session must draw from the same semaphore or
    the bound misses the bulk path's heaviest load."""

    def __init__(
        self, graph: Any, query_bound: asyncio.Semaphore, owner: _DriverInitOwner
    ) -> None:
        super().__init__(graph)
        self._query_bound = query_bound
        self._init_owner = owner

    async def run(self, query: str | list, **kwargs: Any) -> Any:  # type: ignore[type-arg]
        self._init_owner.admit()
        async with self._query_bound:
            self._init_owner.admit()
            return await super().run(query, **kwargs)


class BoundedFalkorDriver(FalkorDriver):
    """``FalkorDriver`` with a ceiling on in-flight index queries (P-82
    gate-4 ruling, 25 August 2026).

    ``graphiti.semaphore_limit`` bounds the in-flight work of one
    ``add_episode`` call; graphiti-core's ``semaphore_gather`` builds a
    fresh semaphore per call, so a bulk chunk's episodes multiply that
    bound and nothing relates the product to the index's
    ``MAX_QUEUED_QUERIES`` ceiling. The chunk-size sweep of 25 August
    2026 measured the result: 353 ``Too many connections`` refusals and
    every chunk silently demoted to the per-fact path. Every query
    reaches the index through this driver — ``execute_query`` directly,
    or a session from ``session()`` — so this is the one place a bound
    can hold.

    The semaphore is shared with every clone graphiti creates per group
    id and with every session, because a copy holding its own semaphore
    is no bound at all. It is created here without a running loop and
    binds lazily to the adapter's owned loop on first acquire, which is
    the only loop that ever awaits it."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
        falkor_db: Any = None,
        database: str = "default_db",
        concurrency_limit: int | None = None,
        shared_bound: asyncio.Semaphore | None = None,
        init_owner: _DriverInitOwner | None = None,
        owns_client: bool = True,
    ) -> None:
        if shared_bound is not None:
            self._query_bound = shared_bound
        elif concurrency_limit is not None and concurrency_limit >= 1:
            self._query_bound = asyncio.Semaphore(concurrency_limit)
        else:
            raise ValueError(
                "BoundedFalkorDriver needs a concurrency_limit of at least 1 "
                "or an existing shared_bound"
            )
        self._init_owner = init_owner if init_owner is not None else _DriverInitOwner()
        self._owns_client = owns_client
        self._init_record = self._init_owner.reserve()
        self._constructing = True
        token = _GRAPHITI_TASK_SCOPE.set(None)
        try:
            super().__init__(
                host=host,
                port=port,
                username=username,
                password=password,
                falkor_db=falkor_db,
                database=database,
            )
        finally:
            # Also runs when the upstream constructor failed before returning.
            # The synchronous factory retains its coroutine before create_task,
            # so missing assignment cannot discard it or be mistaken for no task.
            try:
                if self._owns_client:
                    self._init_owner.client = getattr(self, "client", None)
                task = getattr(self, "_init_task", None)
                if task is not None:
                    self._init_owner.attach(self._init_record, task)
                elif self._init_record.coroutine is not None:
                    self._init_owner.unverified()
                else:
                    self._init_owner.active.discard(self._init_record)
            finally:
                self._constructing = False
                _GRAPHITI_TASK_SCOPE.reset(token)

    def build_indices_and_constraints(
        self, delete_existing: bool = False
    ) -> Coroutine[Any, Any, None]:
        # This synchronous handoff is deliberately narrower than copying the
        # upstream constructor; it also detects swallowed create_task errors.
        if not self._constructing:
            return self._build_schema(delete_existing)
        self._init_record.coroutine = self._initialise(delete_existing)
        return self._init_record.coroutine

    async def _initialise(self, delete_existing: bool) -> None:
        record = self._init_record
        task = asyncio.current_task()
        assert task is not None
        if record.task is None:
            self._init_owner.attach(record, task)
        if self._init_owner.state != "OPEN" or self._init_owner.requested.is_set():
            record.cancel_requested = True
            raise asyncio.CancelledError
        await self._build_schema(delete_existing)

    async def _build_schema(self, delete_existing: bool) -> None:
        self._init_owner.admit(self._init_record)
        await super().build_indices_and_constraints(delete_existing)  # type: ignore[no-untyped-call]

    async def wait_ready(self) -> None:
        await self._init_owner.ready(self._init_record)

    async def close(self) -> None:
        if self._owns_client:
            await self._init_owner.close()
        else:
            self._init_owner.check_loop()
            await self._init_owner.settle(
                (self._init_record,), time.monotonic() + _CALL_TIMEOUT_SECONDS
            )
            self._init_owner.result.raise_if_failed()

    async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
        self._init_owner.admit(self._init_record)
        cypher_query_ = _rewrite_graphiti_edge_search(cypher_query_)
        async with self._query_bound:
            self._init_owner.admit(self._init_record)
            return await super().execute_query(cypher_query_, **kwargs)

    def session(self, database: str | None = None) -> GraphDriverSession:
        self._init_owner.admit()
        return _BoundedFalkorDriverSession(
            self._get_graph(database), self._query_bound, self._init_owner
        )

    def clone(self, database: str) -> GraphDriver:
        self._init_owner.admit()
        # Mirrors the base class's three arms exactly, except that every
        # clone is bounded and shares this driver's semaphore.
        if database == self._database:
            return self
        if database == self.default_group_id:
            return BoundedFalkorDriver(
                falkor_db=self.client,
                shared_bound=self._query_bound,
                init_owner=self._init_owner,
                owns_client=False,
            )
        return BoundedFalkorDriver(
            falkor_db=self.client,
            database=database,
            shared_bound=self._query_bound,
            init_owner=self._init_owner,
            owns_client=False,
        )


class _CairnGraphiti(Graphiti):
    """Graphiti 0.29.3 with Cairn's incremental bulk-dedupe seam."""

    _cairn_extraction_cache: _CacheStore | None = None
    _cairn_safe_logger: SafeLogger | None = None

    async def search_with_vector(
        self,
        query: str,
        config: SearchConfig,
        *,
        group_ids: list[str],
        driver: GraphDriver,
        query_vector: list[float],
    ) -> SearchResults:
        if _GRAPHITI_CORE_VERSION != "0.29.3":
            raise GraphitiIndexError("graphiti_compatibility_version")
        return await graphiti_search(
            self.clients,
            query,
            group_ids,
            config,
            SearchFilters(),
            driver=driver,
            query_vector=query_vector,
        )

    async def _extract_and_dedupe_nodes_bulk(
        self,
        episode_context: list[tuple[EpisodicNode, list[EpisodicNode]]],
        edge_type_map: dict[tuple[str, str], list[str]],
        edge_types: dict[str, type[BaseModel]] | None,
        entity_types: dict[str, type[BaseModel]] | None,
        excluded_entity_types: list[str] | None,
        custom_extraction_instructions: str | None = None,
    ) -> tuple[
        dict[str, list[EntityNode]],
        dict[str, str],
        list[list[EntityEdge]],
    ]:
        (
            extracted_nodes_bulk,
            extracted_edges_bulk,
        ) = await cached_extract_nodes_and_edges_bulk(
            self._cairn_extraction_cache,
            self.clients,
            episode_context,
            edge_type_map=edge_type_map,
            edge_types=edge_types,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=custom_extraction_instructions,
            logger=self._cairn_safe_logger,
        )
        (
            nodes_by_episode,
            uuid_map,
        ) = await graphiti_bulk_module.dedupe_nodes_bulk_incremental(
            self.clients,
            extracted_nodes_bulk,
            episode_context,
            entity_types,
        )
        return nodes_by_episode, uuid_map, extracted_edges_bulk


def _construct_graphiti(
    driver: GraphDriver,
    llm_client: OpenAIClient,
    embedder: OpenAIEmbedder,
    cross_encoder: OpenAIRerankerClient,
    *,
    edge_batch_size: int = 10,
    edge_batch_linger_ms: int = 75,
    edge_batch_max_facts: int = 80,
    extraction_cache: _CacheStore | None = None,
    safe_logger: SafeLogger | None = None,
) -> _CairnGraphiti:
    graphiti_bulk_module._require_graphiti_bulk_compatibility()
    install_edge_batching(
        llm_client,
        batch_size=edge_batch_size,
        linger_ms=edge_batch_linger_ms,
        max_facts=edge_batch_max_facts,
        logger=safe_logger,
    )
    # None disables cache I/O, not validation. The wrapper must exist before
    # Graphiti stores the embedder in its clients bundle.
    embedder = build_caching_embedder(extraction_cache, embedder, safe_logger)
    instance = _CairnGraphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )
    if extraction_cache is not None:
        install_edge_timestamp_caching(extraction_cache, llm_client, safe_logger)
        instance._cairn_extraction_cache = extraction_cache
        instance._cairn_safe_logger = safe_logger
    return instance


def _new_index_loop() -> asyncio.AbstractEventLoop:
    """The adapter's private loop is a uvloop loop, constructed directly
    rather than through the global policy: the policy is process-wide
    state uvicorn also touches, and this loop exists before ``serve``
    hands control to uvicorn. The projection path is bound on this one
    thread, so per-callback loop overhead is paid on the critical path.
    """
    return uvloop.new_event_loop()


@dataclass(eq=False)
class _SearchDriverLease:
    driver: GraphDriver
    refcount: int = 1


class GraphitiIndex:
    """The production ``IndexAdapter`` over graphiti-core and FalkorDB.

    Model and embedding providers are configured exclusively through the
    environment graphiti-core itself reads (``OPENAI_API_KEY`` and
    friends), per P-39: ``cairn.yaml`` carries no secret. Under I-92 that
    environment is set by composition from a file under
    ``paths.credentials`` immediately before construction, so the
    adapter's requirement is unchanged and only its source moved.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        index_concurrency_limit: int = 16,
        edge_batch_size: int = 10,
        edge_batch_linger_ms: int = 75,
        edge_batch_max_facts: int = 80,
        extraction_cache: _CacheStore | None = None,
        safe_logger: SafeLogger | None = None,
    ) -> None:
        self._index_concurrency_limit = index_concurrency_limit
        # Per adapter, not global: this trades overload for head-of-line queueing
        # inside the existing request deadline. Contention binds it to the owned loop.
        self._search_bound = asyncio.Semaphore(
            max(1, min(2, self._index_concurrency_limit))
        )
        # Search-local overlap only: the final borrower evicts the handle, but
        # does not close it because schema-init custody remains with the owner.
        self._search_driver_leases: dict[str, _SearchDriverLease] = {}
        self._safe_logger = safe_logger
        self._loop = _new_index_loop()
        self._scoped_tasks: dict[object, set[asyncio.Task[Any]]] = {}
        self._loop.set_task_factory(self._scoped_task_factory)
        self._close_requested = threading.Event()
        self._containment_breached = threading.Event()
        # Retain acquisition/cleanup state before the root constructor can fail.
        self._init_owner = _DriverInitOwner(self._close_requested)
        self._close_guard = threading.Lock()
        self._close_done = threading.Event()
        self._close_deadline: float | None = None
        self._close_future: Future[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_coroutine: Coroutine[Any, Any, None] | None = None
        self._close_result: _CloseResult | None = None
        self._work_failure: str | None = None
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="cairn-graphiti-index",
            daemon=True,
        )
        self._thread.start()
        try:
            self._driver = self._call(self._construct(host, port, username, password))
            (
                self._provider_client,
                llm_client,
                embedder,
                cross_encoder,
            ) = _openai_provider_clients()
            self._graphiti = _construct_graphiti(
                self._driver,
                llm_client,
                embedder,
                cross_encoder,
                edge_batch_size=edge_batch_size,
                edge_batch_linger_ms=edge_batch_linger_ms,
                edge_batch_max_facts=edge_batch_max_facts,
                extraction_cache=extraction_cache,
                safe_logger=safe_logger,
            )
        except BaseException as error:
            self._work_failure = (
                error.code
                if isinstance(error, GraphitiIndexError)
                else "graphiti_unavailable"
            )
            try:
                self.close()
            except GraphitiIndexError as cleanup_error:
                if isinstance(error, GraphitiIndexError):
                    error.close_result = cleanup_error.close_result
                else:
                    error.add_note("graphiti_constructor_cleanup_failed")
            raise

    async def _construct(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
    ) -> FalkorDriver:
        # Constructed inside the owned loop so the driver's connections,
        # and the index build it schedules on a running loop, belong here.
        return BoundedFalkorDriver(
            host=host,
            port=port,
            username=username,
            password=password,
            concurrency_limit=self._index_concurrency_limit,
            init_owner=self._init_owner,
        )

    def request_close(self) -> None:
        """Composition lifecycle, not part of the port: marks shutdown so
        any in-flight ``_call`` cancels its coroutine within one poll
        slice instead of waiting out its deadline (I-20). Synchronous and
        non-blocking — composition calls it before waiting for the
        delivery pass."""
        self._close_requested.set()
        with self._close_guard:
            if self._close_deadline is None:
                self._close_deadline = time.monotonic() + _CALL_TIMEOUT_SECONDS

    def close(self) -> None:
        """One bounded resource shutdown, then a verified five-second loop join.

        Unverified cleanup permanently refuses use and requires process-level
        teardown. Its daemon loop and obligations remain available for observation.
        """
        self.request_close()
        with self._close_guard:
            first = self._close_future is None
            if first:
                # Reserve before submission: a stalled/ambiguous callback never
                # authorises a second coordinator or cancellation of this one.
                self._close_future = Future()
                self._close_coroutine = self._close_resources()
            future = self._close_future
            deadline = self._close_deadline
        assert future is not None and deadline is not None
        if not first:
            self._close_done.wait(max(0.0, deadline + 5.0 - time.monotonic()))
            result = self._close_result
            if result is None:
                result = replace(self._init_owner.result)
                result.unverified()
            result.raise_if_failed()
            return
        try:
            self._loop.call_soon_threadsafe(self._submit_close)
            future.result(timeout=max(0.0, deadline - time.monotonic()))
            if not self._init_owner.result.cleanup_unverified:
                self._stop_loop()
        except Exception:
            self._init_owner.unverified()
        finally:
            if self._init_owner.result.cleanup_unverified:
                self._containment_breached.set()
            result = replace(self._init_owner.result)
            if self._work_failure is not None and result.first_code is not None:
                result.first_code = self._work_failure
            self._close_result = result
            self._close_done.set()
        result.raise_if_failed()

    def _submit_close(self) -> None:
        token = _GRAPHITI_TASK_SCOPE.set(None)
        try:
            assert self._close_coroutine is not None
            self._close_task = self._loop.create_task(self._close_coroutine)
            self._close_task.add_done_callback(self._close_finished)
        except Exception:
            # A task factory may schedule and then raise. Keep the reservation;
            # late entry claims it, and no second submission is ever attempted.
            self._init_owner.unverified()
        finally:
            _GRAPHITI_TASK_SCOPE.reset(token)

    def _close_finished(self, task: asyncio.Task[None]) -> None:
        assert self._close_future is not None
        if task.cancelled() or task.exception() is not None:
            self._init_owner.unverified()
        self._close_future.set_result(None)

    async def _close_resources(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.current_task()
            assert self._close_task is not None
            self._close_task.add_done_callback(self._close_finished)
        try:
            await self._init_owner.close(
                getattr(self, "_provider_client", None), deadline=self._close_deadline
            )
        except GraphitiIndexError:
            pass  # Safe fields are sticky; still check whether loop disposal is safe.
        other_live = asyncio.all_tasks() - {asyncio.current_task()}
        if self._init_owner.active or self._init_owner.cleanup or other_live:
            self._init_owner.unverified()

    # --- The IndexAdapter operations ------------------------------------

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return self._call(self._project(state))

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        if not states:
            return ()
        timeout = min(_BULK_TIMEOUT_SECONDS, len(states) * _CALL_TIMEOUT_SECONDS)
        return self._call(
            self._project_many(states), timeout=timeout, contain_tasks=True
        )

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        if type(query) is not str or len(query.encode("utf-8")) > _QUERY_MAX_BYTES:
            raise GraphitiIndexError("query_too_large")
        if type(limit) is not int or limit < 1:
            raise GraphitiIndexError("invalid_limit")
        self._last_search_limit = limit
        return self._call(self._search(query, limit, partition_keys))

    def index_policy_metadata(self) -> dict[str, Any]:
        metadata = policy_metadata(self._fact_vector_index().representation)
        limit = getattr(self, "_last_search_limit", None)
        return {
            **metadata,
            "local_evidence": unit_policy_metadata(
                self.memory_evidence_source() is not None
            ),
            "last_requested_limit": limit,
            "libraries": {
                name: version(name) for name in ("graphiti-core", "falkordb", "openai")
            },
            "backend": {
                "name": "FalkorDB",
                "server_version": "unavailable",
                "image_evidence": "live evaluator records falkordb_image from its launcher",
            },
            "query_max_bytes": _QUERY_MAX_BYTES,
            "graphiti_search": {
                **_SEARCH_CONFIG.model_dump(mode="json"),
                "limit": limit,
            },
        }

    def memory_evidence_source(self) -> SemanticEvidenceSource | None:
        config = getattr(self._graphiti.embedder, "config", None)
        dimension = getattr(config, "embedding_dim", None)
        model = getattr(config, "embedding_model", None)
        return (
            self
            if (
                type(dimension) is int
                and dimension == 1024
                and type(model) is str
                and model == "text-embedding-3-small"
            )
            else None
        )

    def search_with_evidence(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> SemanticEvidence:
        try:
            if (
                self.memory_evidence_source() is None
                or type(limit) is not int
                or limit < 1
                or type(partition_keys) is not tuple
                or not 1 <= len(partition_keys) <= PARTITION_LIMIT
                or any(type(key) is not str for key in partition_keys)
                or len(set(partition_keys)) != len(partition_keys)
            ):
                raise SemanticEvidenceError()
            query_hash = query_sha256(query)
            self._last_search_limit = limit
            return self._call(
                self._search_with_evidence(query, limit, partition_keys, query_hash)
            )
        except Exception:
            # Protocol errors carry no provider body, query or partition details.
            raise SemanticEvidenceError() from None

    async def _search_with_evidence(
        self, query: str, limit: int, partitions: tuple[str, ...], query_hash: str
    ) -> SemanticEvidence:
        grades: list[PartitionGrades] = []
        candidates = await self._search(query, limit, partitions, grades=grades)
        return SemanticEvidence(
            query_hash,
            local_representation_sha256(),
            SEARCH_POLICY,
            candidates,
            tuple(grades),
        )

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        self._call(self._clear(partition_keys))

    # --- Coroutine bodies ------------------------------------------------

    async def _project(
        self, state: ProjectedFactState
    ) -> FactProjected | ProjectionFailed:
        group_id = derive_group_id(state.partition_key)
        identity = str(state.fact_id)
        driver = self._driver.clone(database=group_id)
        await _ready(driver)
        try:
            existing: EpisodicNode | None = await EpisodicNode.get_by_uuid(
                driver, identity
            )
        except NodeNotFoundError:
            # The one exception that means *absent*: ``get_by_uuid`` raises
            # it only when the query returned zero records. Everything else
            # — a dead connection, a decode failure over a record that did
            # come back — propagates, because "the store could not answer"
            # is not "there is nothing there". Swallowing those turned an
            # infrastructure failure into a first projection, which then
            # re-saved the episode as pending over whatever was already
            # there and reported the outcome of a question never answered.
            existing = None
        if existing is not None and existing.source_description == _PROJECTION_DONE:
            # P-38's completion invariant: the marker, never mere
            # existence. Fact bodies are immutable and invalidation state
            # is reconciliation's to read from the catalogue, so a
            # *completed* episode needs no repeated extraction. Vector
            # coverage is a separate obligation, including for legacy episodes.
            await self._ensure_fact_vectors(driver, group_id, [state])
            return FactProjected()
        # An episode that exists without the marker is a projection
        # interrupted between its two writes — by this adapter or by the
        # pre-marker code that wrote ``"cairn.fact"`` before extracting; it
        # is resumed by falling through, not trusted and not deleted.
        #
        # graphiti-core 0.29.3's ``add_episode(uuid=...)`` is an *update*:
        # it loads the episode by that uuid and raises NodeNotFoundError
        # when absent, so passing the identity on a first projection can
        # never succeed — found live by scripts/graphiti-smoke, invisible
        # to every mocked test. The identity must still be the episode
        # uuid (search maps ``edge.episodes`` entries straight back to
        # fact UUIDs), so the episode is created here with the pinned
        # uuid — a MERGE-by-uuid save of exactly the node ``add_episode``
        # would have built — and the library's update path then runs its
        # extraction over the node it now finds. It is saved *pending*,
        # because until extraction returns that is what it is.
        episode = EpisodicNode(
            uuid=identity,
            name=identity,
            group_id=group_id,
            labels=[],
            source=EpisodeType.text,
            content=state.body,
            source_description=_PROJECTION_PENDING,
            created_at=state.recorded_at,
            valid_at=state.recorded_at,
        )
        await episode.save(driver)
        await self._graphiti.add_episode(
            name=identity,
            episode_body=state.body,
            source_description=_PROJECTION_PENDING,
            reference_time=state.recorded_at,
            source=EpisodeType.text,
            group_id=group_id,
            uuid=identity,
        )
        # Re-load before stamping. ``add_episode`` saved the episode again
        # with the ``entity_edges`` extraction produced, and the save query
        # is ``MERGE … SET n = {…}`` — a whole-property replacement — so
        # stamping from the stale pre-save object above would silently
        # discard them. Nothing but the marker changes here.
        projected = await EpisodicNode.get_by_uuid(driver, identity)
        projected.source_description = _PROJECTION_DONE
        await projected.save(driver)
        await self._ensure_fact_vectors(driver, group_id, [state])
        # Any failure above propagates: ``_call`` maps it to
        # ``graphiti_unavailable`` and the outbox row is redelivered, which
        # is why no compensating delete is needed. The marker makes an
        # interrupted projection detectable, so the retry resumes it rather
        # than mistaking it for finished work.
        return FactProjected()

    async def _project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        # P-82: the batched form of ``_project``, same two-write protocol
        # per fact. The caller guarantees one partition per call.
        group_id = derive_group_id(states[0].partition_key)
        driver = self._driver.clone(database=group_id)
        await _ready(driver)
        results: list[FactProjected | ProjectionFailed | None] = [None] * len(states)
        pending: list[tuple[int, ProjectedFactState]] = []
        for position, state in enumerate(states):
            identity = str(state.fact_id)
            try:
                existing: EpisodicNode | None = await EpisodicNode.get_by_uuid(
                    driver, identity
                )
            except NodeNotFoundError:
                existing = None
            if existing is not None and existing.source_description == _PROJECTION_DONE:
                results[position] = FactProjected()
            else:
                pending.append((position, state))
        if pending:
            for _, state in pending:
                episode = EpisodicNode(
                    uuid=str(state.fact_id),
                    name=str(state.fact_id),
                    group_id=group_id,
                    labels=[],
                    source=EpisodeType.text,
                    content=state.body,
                    source_description=_PROJECTION_PENDING,
                    created_at=state.recorded_at,
                    valid_at=state.recorded_at,
                )
                await episode.save(driver)
            raw = [
                RawEpisode(
                    name=str(state.fact_id),
                    uuid=str(state.fact_id),
                    content=state.body,
                    source_description=_PROJECTION_PENDING,
                    source=EpisodeType.text,
                    reference_time=state.recorded_at,
                )
                for _, state in pending
            ]
            bulk_results = await self._graphiti.add_episode_bulk(raw, group_id=group_id)
            # Spec verification item 4: ``AddBulkEpisodeResults.episodes``
            # is the per-episode completion evidence, validated as a
            # complete set BEFORE any marker is stamped. An omitted
            # episode fails the whole call — the adapter never partially
            # confirms — and every episode stays pending-marked,
            # resumable by the P-38 marker logic when the deliverer's
            # fallback retries per fact.
            completed = {str(episode.uuid) for episode in bulk_results.episodes}
            if any(str(state.fact_id) not in completed for _, state in pending):
                raise GraphitiIndexError(_BULK_INCOMPLETE)
            for position, state in pending:
                projected = await EpisodicNode.get_by_uuid(driver, str(state.fact_id))
                projected.source_description = _PROJECTION_DONE
                await projected.save(driver)
                results[position] = FactProjected()
        await self._ensure_fact_vectors(driver, group_id, states)
        return tuple(cast(FactProjected | ProjectionFailed, r) for r in results)

    def _fact_vector_index(self) -> FactVectorIndex:
        return FactVectorIndex(
            FactRepresentation(cast(OpenAIEmbedder, self._graphiti.embedder))
        )

    def _fact_unit_index(self) -> FactUnitIndex:
        return FactUnitIndex(self._fact_vector_index().representation)

    async def _ensure_fact_vectors(
        self, driver: GraphDriver, group: str, states: Sequence[ProjectedFactState]
    ) -> None:
        if self.memory_evidence_source() is not None:
            await self._fact_unit_index().ensure(driver, group, states)
        else:
            await self._fact_vector_index().ensure(driver, group, states)

    async def _search(
        self,
        query: str,
        limit: int,
        partition_keys: tuple[str, ...],
        *,
        grades: list[PartitionGrades] | None = None,
    ) -> tuple[UUID, ...]:
        # Match pinned Graphiti's benign blank query. This is not a coverage
        # check: a later nonblank query must still validate every partition.
        if not query.strip():
            return ()
        async with self._search_bound:
            return await self._search_nonblank(query, limit, partition_keys, grades)

    async def _search_nonblank(
        self,
        query: str,
        limit: int,
        partition_keys: tuple[str, ...],
        grades: list[PartitionGrades] | None,
    ) -> tuple[UUID, ...]:
        with ExitStack() as driver_leases:
            return await self._search_with_leased_drivers(
                query, limit, partition_keys, grades, driver_leases
            )

    @contextmanager
    def _lease_search_driver(self, group_id: str) -> Iterator[GraphDriver]:
        # Admission applies equally to a new clone and an overlapping hit.
        self._init_owner.admit()
        lease = self._search_driver_leases.get(group_id)
        if lease is None:
            # Construct before registration: a failed clone leaves no entry.
            driver = self._driver.clone(database=group_id)
            lease = _SearchDriverLease(driver)
            self._search_driver_leases[group_id] = lease
        else:
            lease.refcount += 1
        try:
            yield lease.driver
        finally:
            lease.refcount -= 1
            if (
                lease.refcount == 0
                and self._search_driver_leases.get(group_id) is lease
            ):
                del self._search_driver_leases[group_id]

    async def _search_with_leased_drivers(
        self,
        query: str,
        limit: int,
        partition_keys: tuple[str, ...],
        grades: list[PartitionGrades] | None,
        driver_leases: ExitStack,
    ) -> tuple[UUID, ...]:
        # P-48: one search per partition, because the library scopes a
        # search to its driver's current database and a partition is its
        # own database under FalkorDB. At most seventeen partitions — the
        # realm root through a sixteen-segment path.
        candidates: list[UUID] = []
        seen: set[UUID] = set()
        # ``limit`` bounds candidates per search branch/partition, not the
        # whole-partition coverage and exact-cosine scan cost. It is not a
        # cross-partition cap on what this method returns.
        config = _SEARCH_CONFIG.model_copy(update={"limit": limit})
        fact_vectors = self._fact_vector_index()
        fact_units = self._fact_unit_index() if grades is not None else None
        partitions = []
        for partition in partition_keys:
            group_id = derive_group_id(partition)
            driver = driver_leases.enter_context(self._lease_search_driver(group_id))
            await fact_vectors.preflight(driver, group_id)
            if fact_units is not None:
                await fact_units.preflight(driver, group_id)
            partitions.append((partition, group_id, driver))
        if not partitions:
            return ()
        # Seam 3: every search_ call in this loop must see live embeddings,
        # never the extraction cache's — wrapped here, around the whole
        # loop, rather than inside it, so no partition's call is missed.
        with search_embedding_passthrough():
            query_vector = await fact_vectors.representation.embed_text(
                query.replace("\n", " ")
            )
            for partition, group_id, driver in partitions:
                results = await self._graphiti.search_with_vector(
                    query,
                    config,
                    group_ids=[group_id],
                    driver=driver,
                    query_vector=query_vector,
                )
                # This statement also rechecks coverage after preflight, before
                # any candidates escape. A defect discards the whole search.
                fact_hits = await fact_vectors.search(
                    driver, group_id, query_vector, limit
                )
                # Preserve existing episode/edge candidate order, then append
                # relation-independent fact-body candidates.
                values = [episode.uuid for episode in results.episodes]
                for edge in results.edges:
                    values.extend(edge.episodes)
                values.extend(fact_hits)
                if fact_units is not None and grades is not None:
                    count, unit_hits = await fact_units.search(
                        driver, group_id, query_vector
                    )
                    grades.append(
                        PartitionGrades(
                            partition,
                            True,
                            count,
                            tuple(
                                FactGrade(UUID(identity), partition, fingerprint, score)
                                for identity, fingerprint, score in unit_hits
                            ),
                        )
                    )
                    values.extend(identity for identity, _, _ in unit_hits)
                for value in values:
                    identity = _episode_identity(value)
                    if identity is None or identity in seen:
                        # Not an identity this adapter wrote, or already
                        # collected. Either way it is not a candidate:
                        # reconciliation dedupes defensively regardless,
                        # but an honest adapter does not manufacture
                        # duplicates it can see.
                        continue
                    seen.add(identity)
                    candidates.append(identity)
        # No cross-partition truncation. Slicing here decided candidate
        # *membership*, which is upstream of everything I-82 orders: with
        # each partition fetched to the full limit and appended in
        # ancestry order, the realm root could consume the whole allowance
        # and starve the request's own scope. Ranking and budget belong to
        # reconciliation, which already treats this bound as "owed nothing
        # by the adapter" (retrieval.py). What crosses the port is bounded
        # only by what the store holds: each partition contributes up to
        # ``limit`` episode hits *plus* up to ``limit`` edge hits, and an
        # edge carries however many episode uuids it was extracted from.
        # Bare UUIDs, deduplicated, and nothing is disclosed on their
        # strength alone — reconciliation re-derives every hit from the
        # catalogue and I-82 applies the budget.
        return tuple(candidates)

    async def _clear(self, partition_keys: tuple[str, ...] | None) -> None:
        if partition_keys is not None:
            for partition in partition_keys:
                group_id = derive_group_id(partition)
                driver = self._driver.clone(database=group_id)
                await _ready(driver)
                await clear_data(driver)
            return
        # Everything: enumerate the graphs whose names have the derived
        # shape and drop them. Legitimate because the FalkorDB workload is
        # Cairn's own (I-13); a graph named like a derivation output on a
        # shared instance would be exactly the co-tenancy I-13 excludes.
        graphs = await self._driver.client.list_graphs()
        for name in graphs:
            if _DERIVED_GRAPH_NAME.fullmatch(name) is not None:
                graph = self._driver.client.select_graph(name)
                await graph.delete()

    # --- The loop bridge --------------------------------------------------

    def _scoped_task_factory(
        self,
        loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        """Register child tasks against the adapter call that spawned them.

        ``asyncio.gather`` creates its children through the loop task factory,
        and child contexts inherit ``_GRAPHITI_TASK_SCOPE``. This gives each
        concurrent adapter call its own cancellation set without changing
        asyncio globally or confusing a retrieval call with bulk delivery.
        """
        task = asyncio.Task(coroutine, loop=loop, context=context)
        scope = (
            context.get(_GRAPHITI_TASK_SCOPE)
            if context is not None
            else _GRAPHITI_TASK_SCOPE.get()
        )
        if scope is None:
            return task
        tasks = self._scoped_tasks.setdefault(scope, set())
        tasks.add(task)

        def forget(completed: asyncio.Task[Any]) -> None:
            scoped = self._scoped_tasks.get(scope)
            if scoped is not None:
                scoped.discard(completed)

        task.add_done_callback(forget)
        return task

    async def _settle_scoped_tasks(self, scope: object, *, cancel: bool) -> None:
        """Wait until a call owns no live child task, cancelling on failure.

        The loop repeats because cancellation cleanup can itself schedule a
        child with the inherited scope. ``return_exceptions`` is deliberate:
        cleanup must drain every sibling before the original failure crosses
        back to the delivery fallback.
        """
        while True:
            pending = tuple(
                task for task in self._scoped_tasks.get(scope, set()) if not task.done()
            )
            if not pending:
                return
            if cancel:
                for task in pending:
                    task.cancel()
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            if not cancel:
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        raise outcome
            await asyncio.sleep(0)

    async def _contain_tasks[ResultT](
        self,
        submission: _ContainedCall[ResultT],
    ) -> ResultT:
        """Make one bulk call quiescent before it returns or raises."""
        coroutine = submission.begin()
        if coroutine is None:
            submission.finish()
            raise asyncio.CancelledError
        scope = object()
        token = _GRAPHITI_TASK_SCOPE.set(scope)
        try:
            try:
                result = await coroutine
                await self._settle_scoped_tasks(scope, cancel=False)
                return result
            except BaseException:
                await self._settle_scoped_tasks(scope, cancel=True)
                raise
        finally:
            self._scoped_tasks.pop(scope, None)
            _GRAPHITI_TASK_SCOPE.reset(token)
            submission.finish()

    def _call[ResultT](
        self,
        coroutine: Coroutine[object, object, ResultT],
        *,
        timeout: float = _CALL_TIMEOUT_SECONDS,
        interruptible: bool = True,
        contain_tasks: bool = False,
    ) -> ResultT:
        if interruptible and self._close_requested.is_set():
            coroutine.close()
            raise GraphitiIndexError("graphiti_shutdown")
        if interruptible and self._containment_breached.is_set():
            coroutine.close()
            raise GraphitiIndexError("graphiti_containment_failed")
        # Waited in short slices rather than one blocking result() so a
        # close request lands within a slice: shutdown is bounded by the
        # poll granularity, never by the call deadline (I-20).
        containment: _ContainedCall[ResultT] | None = None
        submitted = coroutine
        if contain_tasks:
            containment = _ContainedCall(coroutine)
            submitted = self._contain_tasks(containment)
        future = asyncio.run_coroutine_threadsafe(submitted, self._loop)
        deadline = time.monotonic() + timeout

        def finish_containment_cancellation() -> None:
            if containment is None or containment.finished.wait(
                _TASK_CLEANUP_WAIT_SECONDS
            ):
                return
            if not containment.abandon_if_not_running():
                self._containment_breached.set()

        try:
            while True:
                if interruptible and self._close_requested.is_set():
                    future.cancel()
                    finish_containment_cancellation()
                    raise GraphitiIndexError("graphiti_shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    finish_containment_cancellation()
                    raise GraphitiIndexError("graphiti_timeout")
                try:
                    return future.result(timeout=min(_CALL_POLL_SECONDS, remaining))
                except TimeoutError as error:
                    if future.done():
                        # The coroutine's own TimeoutError, not the
                        # slice's: an infrastructure failure like any
                        # other library exception.
                        raise GraphitiIndexError("graphiti_unavailable") from error
                    continue
        except GraphitiIndexError as error:
            if getattr(self, "_work_failure", None) is None:
                self._work_failure = error.code
            raise
        except FactVectorError as error:
            if getattr(self, "_work_failure", None) is None:
                self._work_failure = error.code
            logger = getattr(self, "_safe_logger", None)
            if error.code == "fact_vector_rebuild_required" and logger is not None:
                logger.emit(
                    LogEvent.SEMANTIC_REBUILD_NEEDED,
                    transport=None,
                    level=logging.WARNING,
                )
            raise GraphitiIndexError(error.code) from None
        except Exception as error:
            # Library and connection failures of every stripe surface as
            # one safe infrastructure code; the chained exception carries
            # the detail for operator logs only.
            if getattr(self, "_work_failure", None) is None:
                self._work_failure = "graphiti_unavailable"
            raise GraphitiIndexError("graphiti_unavailable") from error

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if (
            self._thread.is_alive()
            or self._loop.is_running()
            or asyncio.all_tasks(self._loop)
        ):
            raise GraphitiIndexError("graphiti_cleanup_unverified")
        self._loop.close()


# P-38's completion invariant, carried in the episode's own
# ``source_description``. Projection is two writes — pre-save, then
# extraction — so "an episode exists" proves only that the first landed.
# The marker is written deliberately after extraction returns, because no
# natural one exists: an episode whose body yields no extractable relation
# legitimately has zero edges, which is exactly the case the live smoke
# hit. ``add_episode`` cannot write it for us — on its update path it
# loads the episode by uuid and applies none of the passed fields.
#
# The completion value is deliberately *new*. ``ad17203`` pre-saved the
# episode as ``"cairn.fact"`` before extraction ran, so any episode that
# code left behind mid-projection carries that string while being exactly
# the partial state the marker exists to detect. Reusing it would have
# read every one of those as complete — the original defect, preserved
# for precisely the episodes it stranded. ``"cairn.fact"`` is therefore
# not a marker this adapter recognises, and an episode carrying it is
# resumed like any other unmarked one.
#
# No value that means "done" is ever written before extraction returns:
# the pre-save and the ``add_episode`` call both pass the pending value,
# so the invariant does not rest on the library continuing to ignore the
# ``source_description`` it is handed on its update path.
_PROJECTION_PENDING = "cairn.fact.projecting"
_PROJECTION_DONE = "cairn.fact.projected"


# The default search() is edge-only, and edges exist only where entity
# extraction found a relation — a one-clause fact body can extract none at
# all (observed live by scripts/graphiti-smoke: a projected fact invisible
# to its own search). Episodes exist for every projected fact, so episode
# BM25 is the recall floor; the edge layer keeps the semantic reach the
# extraction adds where it succeeded. Nodes and communities are omitted:
# neither carries an episode identity a fact could be recovered from.
_SEARCH_CONFIG = SearchConfig(
    edge_config=EdgeSearchConfig(
        search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
        reranker=EdgeReranker.rrf,
    ),
    episode_config=EpisodeSearchConfig(
        search_methods=[EpisodeSearchMethod.bm25],
        reranker=EpisodeReranker.rrf,
    ),
)


def _episode_identity(value: str) -> UUID | None:
    """An episode uuid this adapter wrote is a canonical fact UUID; any
    other shape is foreign data, dropped without becoming a candidate."""
    try:
        identity = UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return identity if str(identity) == value else None
