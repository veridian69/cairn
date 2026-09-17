"""Offline regressions for init tasks created by the pinned driver constructor."""

import asyncio
import gc
import runpy
import threading
import time
import warnings
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import CoroutineType, SimpleNamespace
from typing import Any, cast

import pytest
from graphiti_core.driver.falkordb_driver import FalkorDriver

from cairn.projection import graphiti
from cairn.projection.fact_vectors import _ready
from cairn.projection.graphiti import BoundedFalkorDriver, GraphitiIndexError


class _Client:
    def __init__(self) -> None:
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


def _offline_index(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: Any = None,
    provider: Any = None,
) -> graphiti.GraphitiIndex:
    """Keep the actual adapter/driver constructors; replace only external clients."""
    from graphiti_core.driver import falkordb_driver

    client = client if client is not None else _Client()
    provider = (
        provider if provider is not None else SimpleNamespace(close=client.aclose)
    )

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(falkordb_driver, "FalkorDB", lambda **kwargs: client)
    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
    monkeypatch.setattr(
        graphiti, "_openai_provider_clients", lambda: (provider, None, None, None)
    )
    monkeypatch.setattr(graphiti, "_construct_graphiti", lambda *args, **kwargs: None)
    return graphiti.GraphitiIndex(host="offline.invalid", port=1)


def _finish_retained_index(index: graphiti.GraphitiIndex) -> None:
    """Test-only teardown after releasing deliberately uncooperative work."""

    async def drain() -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    if index._thread.is_alive():
        asyncio.run_coroutine_threadsafe(drain(), index._loop).result(timeout=2)
        index._loop.call_soon_threadsafe(index._loop.stop)
        index._thread.join(timeout=2)
    assert not index._thread.is_alive()
    if not index._loop.is_closed():
        index._loop.close()


def test_index_closes_once_and_retains_work_failure_with_secondary_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BadClient(_Client):
        async def aclose(self) -> None:
            await super().aclose()
            raise ValueError("private client failure")

    client = BadClient()
    provider = BadClient()
    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=provider.aclose)
    )

    async def fail() -> None:
        raise GraphitiIndexError("work_failed")

    with pytest.raises(GraphitiIndexError, match="work_failed"):
        index._call(fail())
    try:
        for _ in range(2):
            with pytest.raises(GraphitiIndexError, match="work_failed") as caught:
                index.close()
            result = caught.value.close_result
            assert result is not None
            assert result.client_failed and result.provider_failed
            assert not result.cleanup_unverified
        assert client.closes == provider.closes == 1
        assert index._loop.is_closed()
    finally:
        _finish_retained_index(index)


@pytest.mark.parametrize("after_schedule", [False, True])
def test_index_constructor_unwinds_before_driver_assignment(
    monkeypatch: pytest.MonkeyPatch, after_schedule: bool
) -> None:
    original = FalkorDriver.__init__
    client = _Client()
    captured: list[Any] = []

    def broken(self: Any, **kwargs: Any) -> None:
        if after_schedule:
            original(self, **kwargs)
        else:
            self.client = client
        captured.append(self)
        raise ValueError("construction failed")

    monkeypatch.setattr(FalkorDriver, "__init__", broken)
    with pytest.raises(GraphitiIndexError, match="graphiti_unavailable"):
        _offline_index(monkeypatch, client=client)
    assert client.closes == 1
    assert not captured[0]._init_owner.active
    assert captured[0]._init_owner.loop.is_closed()


def test_provider_close_starts_while_init_resists_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    provider = _Client()
    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=provider.aclose)
    )
    release = asyncio.Event()
    started = threading.Event()

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def clone() -> Any:
        return index._driver.clone("resistant")

    handle = index._call(clone())
    assert started.wait(1)
    monkeypatch.setattr(graphiti, "_CALL_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(GraphitiIndexError) as caught:
            index.close()
        assert caught.value.close_result.cleanup_unverified  # type: ignore[union-attr]
        assert provider.closes == 1
        assert not handle._init_task.done()
        assert index._thread.is_alive() and not index._loop.is_closed()
        with pytest.raises(GraphitiIndexError):
            index.close()
    finally:
        index._loop.call_soon_threadsafe(release.set)
        _finish_retained_index(index)
    assert client.closes == provider.closes == 1


def test_stalled_loop_close_submission_is_retained_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    provider = _Client()
    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=provider.aclose)
    )
    release = threading.Event()
    started = threading.Event()

    def stall() -> None:
        started.set()
        release.wait(2)

    index._loop.call_soon_threadsafe(stall)
    assert started.wait(1)
    monkeypatch.setattr(graphiti, "_CALL_TIMEOUT_SECONDS", 0.05)
    before = time.monotonic()
    try:
        for _ in range(2):
            with pytest.raises(GraphitiIndexError):
                index.close()
        assert time.monotonic() - before < 0.5
        assert index._thread.is_alive() and not index._loop.is_closed()
        assert client.closes == provider.closes == 0
    finally:
        release.set()
        _finish_retained_index(index)
    assert client.closes == provider.closes == 1


def test_dormant_family_binds_lazily_and_refuses_another_loop() -> None:
    class Client(_Client):
        def select_graph(self, name: str) -> Any:
            async def query(*args: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(header=[], result_set=[])

            return SimpleNamespace(query=query)

    root = BoundedFalkorDriver(falkor_db=Client(), concurrency_limit=2)
    clone = root.clone("dormant")
    session = clone.session()
    assert root._init_task is None
    assert not root._init_owner.active
    first = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    try:
        first.run_until_complete(session.run("RETURN 1"))
        with pytest.raises(GraphitiIndexError, match="graphiti_wrong_loop"):
            other.run_until_complete(root.execute_query("RETURN 1"))
        first.run_until_complete(root.close())
    finally:
        first.close()
        other.close()


def test_explicit_schema_build_on_dormant_driver_is_a_normal_owned_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def build(self: Any, delete_existing: bool = False) -> None:
        calls.append(delete_existing)

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
    root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)

    async def scenario() -> None:
        try:
            await root.build_indices_and_constraints(delete_existing=True)
            assert calls == [True]
            assert not root._init_owner.active
        finally:
            await root.close()

    asyncio.run(scenario())


def test_schema_queries_do_not_await_their_own_init_and_admission_closes() -> None:
    class Client(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.queries: list[str] = []

        def select_graph(self, name: str) -> Any:
            async def query(cypher: str, *args: Any, **kwargs: Any) -> Any:
                self.queries.append(cypher)
                return SimpleNamespace(header=[], result_set=[])

            return SimpleNamespace(query=query)

    async def scenario() -> None:
        client = Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=1)
        session = root.session()
        try:
            await asyncio.wait_for(_ready(root), timeout=1)
            assert client.queries
            root._init_owner.requested.set()
            with pytest.raises(GraphitiIndexError, match="graphiti_shutdown"):
                await session.run("RETURN 1")
            with pytest.raises(GraphitiIndexError, match="graphiti_shutdown"):
                await root.execute_query("RETURN 1")
            with pytest.raises(GraphitiIndexError, match="graphiti_shutdown"):
                root.clone("late")
            with pytest.raises(GraphitiIndexError, match="graphiti_shutdown"):
                root.session()
        finally:
            await root.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("query_fails", [False, True])
def test_running_upstream_schema_stops_at_shutdown_admission_without_false_failure(
    query_fails: bool,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class Client(_Client):
            def __init__(self) -> None:
                super().__init__()
                self.queries = 0

            def select_graph(self, name: str) -> Any:
                async def query(*args: Any, **kwargs: Any) -> Any:
                    self.queries += 1
                    started.set()
                    await release.wait()
                    if query_fails:
                        raise ValueError("genuine schema query failure")
                    return SimpleNamespace(header=[], result_set=[])

                return SimpleNamespace(query=query)

        client = Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=1)
        await started.wait()
        # Queue the schema continuation before the shutdown coordinator. Its
        # next query admission sees closing before settle() cancels the task.
        release.set()
        if query_fails:
            with pytest.raises(GraphitiIndexError, match="graphiti_init_failed"):
                await root.close()
        else:
            await root.close()
            assert root._init_task is not None and root._init_task.cancelled()
        assert root._init_owner.result.init_failed is query_fails
        assert not root._init_owner.result.cleanup_unverified
        assert not root._init_owner.active
        assert client.queries == client.closes == 1
        assert root._query_bound._value == 1

    asyncio.run(scenario())


def test_schema_waiting_for_query_slot_is_cancelled_at_shutdown_admission() -> None:
    async def scenario() -> None:
        # No client query is allowed: schema is held before its first query.
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=1)
        await root._query_bound.acquire()
        await asyncio.sleep(0)
        root._query_bound.release()
        await root.close()
        assert root._init_task is not None and root._init_task.cancelled()
        assert not root._init_owner.result.init_failed
        assert root._query_bound._value == 1

    asyncio.run(scenario())


def test_concurrent_index_close_uses_one_deadline_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    provider = _Client()
    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=provider.aclose)
    )
    index.request_close()
    deadline = index._close_deadline
    assert deadline is not None and 299 < deadline - time.monotonic() <= 300
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: index.close(), range(4)))
        assert index._close_deadline == deadline
        assert client.closes == provider.closes == 1
        assert not index._thread.is_alive() and index._loop.is_closed()
    finally:
        _finish_retained_index(index)


@pytest.mark.parametrize("stuck_provider", [False, True])
def test_stuck_close_obligation_survives_deadline_and_is_observed_later(
    monkeypatch: pytest.MonkeyPatch, stuck_provider: bool
) -> None:
    release = asyncio.Event()

    class Stuck(_Client):
        async def aclose(self) -> None:
            await super().aclose()
            await release.wait()
            raise ValueError("private close failure")

    client = _Client() if stuck_provider else Stuck()
    provider = Stuck() if stuck_provider else _Client()
    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=provider.aclose)
    )
    monkeypatch.setattr(graphiti, "_CALL_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(GraphitiIndexError):
            index.close()
        assert index._init_owner.cleanup
        assert index._thread.is_alive() and not index._loop.is_closed()
        with pytest.raises(GraphitiIndexError):
            index.close()
    finally:
        index._loop.call_soon_threadsafe(release.set)
        _finish_retained_index(index)
    assert client.closes == provider.closes == 1
    assert not index._init_owner.cleanup
    assert (
        index._init_owner.result.provider_failed
        if stuck_provider
        else index._init_owner.result.client_failed
    )


def test_unrelated_live_task_prevents_loop_close_without_becoming_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _offline_index(monkeypatch)
    release = asyncio.Event()

    async def spawn() -> asyncio.Task[bool]:
        return asyncio.create_task(release.wait())

    task = index._call(spawn())
    try:
        with pytest.raises(GraphitiIndexError):
            index.close()
        assert not task.done()
        assert not index._loop.is_closed()
    finally:
        index._loop.call_soon_threadsafe(release.set)
        _finish_retained_index(index)


def test_join_failure_keeps_loop_open_and_does_not_retry_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _offline_index(monkeypatch)
    thread = index._thread
    joins: list[float] = []

    class FailedJoin:
        def join(self, timeout: float) -> None:
            joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(index, "_thread", FailedJoin())
    try:
        for _ in range(2):
            with pytest.raises(GraphitiIndexError):
                index.close()
        assert joins == [5.0]
        assert not index._loop.is_closed()
    finally:
        monkeypatch.setattr(index, "_thread", thread)
        thread.join(timeout=2)
        _finish_retained_index(index)


def test_init_failure_keeps_priority_over_independent_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                raise ValueError("private init failure") from None

        async def provider_close() -> None:
            raise ValueError("private provider failure")

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        client = _Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=1)
        await started.wait()
        with pytest.raises(GraphitiIndexError, match="graphiti_init_failed") as caught:
            await root._init_owner.close(SimpleNamespace(close=provider_close))
        assert caught.value.close_result is not None
        assert caught.value.close_result.provider_failed
        assert client.closes == 1

    asyncio.run(scenario())


def test_ambiguous_close_task_submission_is_owned_on_late_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _offline_index(monkeypatch)
    original = index._scoped_task_factory
    reports: list[dict[str, Any]] = []
    failed = False

    def interrupted(
        loop: asyncio.AbstractEventLoop,
        coro: Coroutine[Any, Any, Any],
        context: Any = None,
    ) -> asyncio.Task[Any]:
        nonlocal failed
        task = original(loop, coro, context)
        if not failed:
            failed = True
            raise RuntimeError("private submission error")
        return task

    async def install() -> None:
        index._loop.set_exception_handler(lambda loop, context: reports.append(context))
        index._loop.set_task_factory(interrupted)

    index._call(install())
    monkeypatch.setattr(graphiti, "_CALL_TIMEOUT_SECONDS", 0.05)
    try:
        for _ in range(2):
            with pytest.raises(GraphitiIndexError):
                index.close()
        assert reports == []
        assert index._close_task is not None and index._close_task.done()
    finally:
        _finish_retained_index(index)


@pytest.mark.parametrize("interrupt_at", [1, 2, 3])
def test_root_shutdown_never_retries_an_ambiguous_close_attempt(
    monkeypatch: pytest.MonkeyPatch, interrupt_at: int
) -> None:
    async def scenario() -> None:
        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            pass

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        client = _Client()
        provider = _Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=1)
        await _ready(root)
        loop = asyncio.get_running_loop()
        submitted = 0
        tasks: list[asyncio.Task[Any]] = []

        def interrupted(
            loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any], **kw: Any
        ) -> asyncio.Task[Any]:
            nonlocal submitted
            task = asyncio.Task(coro, loop=loop, **kw)
            tasks.append(task)
            submitted += 1
            if submitted == interrupt_at:
                raise RuntimeError("private handoff error")
            return task

        loop.set_task_factory(interrupted)
        try:
            for _ in range(2):
                with pytest.raises(GraphitiIndexError):
                    await root._init_owner.close(SimpleNamespace(close=provider.aclose))
            for _ in range(10):
                await asyncio.sleep(0)
            assert client.closes == provider.closes == 1
            assert not root._init_owner.cleanup
        finally:
            loop.set_task_factory(None)
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_provider_close_invocation_failure_still_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()

    def fail() -> None:
        raise ValueError("provider close invocation failed")

    index = _offline_index(
        monkeypatch, client=client, provider=SimpleNamespace(close=fail)
    )
    try:
        with pytest.raises(GraphitiIndexError) as caught:
            index.close()
        assert caught.value.close_result is not None
        assert caught.value.close_result.provider_failed
        assert not caught.value.close_result.cleanup_unverified
        assert client.closes == 1
    finally:
        _finish_retained_index(index)


def test_upstream_group_dispatch_clones_join_the_root_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphiti_core.decorators import handle_multiple_group_ids

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    @handle_multiple_group_ids
    async def dispatch(
        self: Any, group_ids: list[str], driver: Any = None
    ) -> list[Any]:
        return [driver]

    async def scenario() -> None:
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        sdk = SimpleNamespace(clients=SimpleNamespace(driver=root))
        clones = await dispatch(sdk, group_ids=["one", "two"])
        clones += await dispatch(sdk, group_ids=["three"])
        await root.close()
        assert len(clones) == 3
        assert all(driver._init_task.done() for driver in clones)
        assert root.client.closes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fixture_file", ["test_fact_vectors_db.py", "test_fact_unit_index_db.py"]
)
def test_db_fixture_preserves_active_primary_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch, fixture_file: str
) -> None:
    fixture = runpy.run_path(str(Path(__file__).with_name(fixture_file)))["adapter"]
    primary = ValueError("test failed")
    cleanup = GraphitiIndexError("graphiti_client_close_failed")

    def close() -> None:
        raise cleanup

    index = SimpleNamespace(clear=lambda partitions: None, close=close)
    monkeypatch.setattr(graphiti, "GraphitiIndex", lambda **kwargs: index)
    generator = fixture.__wrapped__(1, monkeypatch)
    assert next(generator) is index
    with pytest.raises(BaseExceptionGroup) as caught:
        generator.throw(primary)
    assert caught.value.exceptions == (primary, cleanup)


def test_root_close_settles_all_clone_initialisations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def scenario() -> None:
        client = _Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=2)
        clones = [root.clone("one"), root.clone("two")]
        await asyncio.sleep(0)
        tasks = [cast(Any, driver)._init_task for driver in [root, *clones]]
        try:
            await root.close()
            assert all(task.done() for task in tasks)
            assert client.closes == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_failure_is_observed_without_readiness_or_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        if self._database != "default_db":
            raise ValueError("private schema failure")

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        reports: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda loop, context: reports.append(context))
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        for name in ("one", "two"):
            root.clone(name)
        for _ in range(4):
            await asyncio.sleep(0)
        gc.collect()
        try:
            assert reports == []
            assert not root._init_owner.active
            assert root._init_owner.result.init_failed
            assert root._init_owner.result.first_code == "graphiti_init_failed"
        finally:
            with pytest.raises(GraphitiIndexError, match="graphiti_init_failed"):
                await root.close()

    asyncio.run(scenario())


def test_family_has_no_clone_cap_and_forgets_completed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            await release.wait()

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        client = _Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=2)
        clones = [root.clone(str(i)) for i in range(300)]
        assert root.clone("default_db") is root
        default = clones[0].clone(root.default_group_id)
        assert default._database == "default_db"
        try:
            assert len(root._init_owner.active) == 302
            release.set()
            await asyncio.gather(*(_ready(d) for d in [root, *clones, default]))
            assert not root._init_owner.active
        finally:
            await asyncio.gather(root.close(), root.close())
            await root.close()
        assert client.closes == 1

    asyncio.run(scenario())


def test_readiness_failure_is_sticky_even_after_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            await release.wait()
            raise ValueError("private schema failure")

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        waiter = asyncio.create_task(_ready(root))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        with pytest.raises(ValueError):
            await _ready(root)
        for _ in range(2):
            with pytest.raises(GraphitiIndexError, match="graphiti_init_failed"):
                await root.close()

    asyncio.run(scenario())


def test_bulk_cancellation_does_not_own_schema_or_its_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        adapter = graphiti.GraphitiIndex.__new__(graphiti.GraphitiIndex)
        adapter._scoped_tasks = {}
        loop.set_task_factory(adapter._scoped_task_factory)
        init_started = asyncio.Event()
        ordinary_stopped = asyncio.Event()
        descendants: list[asyncio.Task[Any]] = []

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            child = asyncio.create_task(asyncio.Event().wait())
            descendants.append(child)
            init_started.set()
            await child

        async def ordinary() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                ordinary_stopped.set()

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        handles: list[Any] = []

        async def bulk() -> None:
            handles.append(root.clone("bulk"))
            asyncio.create_task(ordinary())
            await init_started.wait()
            await _ready(handles[0])

        call = asyncio.create_task(
            adapter._contain_tasks(graphiti._ContainedCall(bulk()))
        )
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert ordinary_stopped.is_set()
            assert all(not t.done() for t in descendants)
            assert not handles[0]._init_task.done()
        finally:
            await root.close()
            loop.set_task_factory(None)
        assert all(t.done() for t in descendants)

    asyncio.run(scenario())


@pytest.mark.parametrize("after_schedule", [False, True])
def test_constructor_failure_retains_acquired_client_and_scheduled_task(
    monkeypatch: pytest.MonkeyPatch, after_schedule: bool
) -> None:
    original = FalkorDriver.__init__
    captured: list[Any] = []

    def broken(self: Any, **kwargs: Any) -> None:
        captured.append(self)
        if after_schedule:
            original(self, **kwargs)
        else:
            self.client = kwargs["falkor_db"]
        raise ValueError("construction failed")

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(FalkorDriver, "__init__", broken)
    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def scenario() -> None:
        owner = graphiti._DriverInitOwner()
        client = _Client()
        with pytest.raises(ValueError):
            BoundedFalkorDriver(falkor_db=client, concurrency_limit=2, init_owner=owner)
        await owner.close()
        assert client.closes == 1
        assert not owner.active
        if after_schedule:
            assert captured[0]._init_task.done()

    asyncio.run(scenario())


def test_interrupted_task_assignment_is_unverified_but_late_task_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        tasks: list[asyncio.Task[Any]] = []
        ran: list[bool] = []

        def interrupted(
            loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any], **kw: Any
        ) -> asyncio.Task[Any]:
            tasks.append(asyncio.Task(coro, loop=loop, **kw))
            raise RuntimeError("handoff interrupted")

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            ran.append(True)

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        loop.set_task_factory(interrupted)
        try:
            root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        finally:
            loop.set_task_factory(None)
        with pytest.raises(GraphitiIndexError):
            await root.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert ran == []
        assert root._init_owner.result.cleanup_unverified
        assert not root._init_owner.active

    asyncio.run(scenario())


@pytest.mark.parametrize("after_creation", [False, True])
def test_rejected_init_submission_retains_coroutine_without_premature_close(
    monkeypatch: pytest.MonkeyPatch, after_creation: bool
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        tasks: list[asyncio.Task[Any]] = []
        ran: list[bool] = []

        def reject(
            loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any], **kw: Any
        ) -> asyncio.Task[Any]:
            if after_creation:
                tasks.append(asyncio.Task(coro, loop=loop, **kw))
            # In the pre-creation arm the factory deliberately retains nothing.
            raise RuntimeError("init submission rejected")

        async def build(self: Any, *args: Any, **kwargs: Any) -> None:
            ran.append(True)

        monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", RuntimeWarning)
            loop.set_task_factory(reject)
            try:
                root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=1)
            finally:
                loop.set_task_factory(None)
            try:
                gc.collect()
                assert not [w for w in captured if "never awaited" in str(w.message)]
                coroutine = root._init_record.coroutine
                assert isinstance(coroutine, CoroutineType)
                assert coroutine.cr_frame is not None  # Not prematurely closed.
                with pytest.raises(GraphitiIndexError) as caught:
                    await root.close()
                assert caught.value.close_result is not None
                assert caught.value.close_result.cleanup_unverified
                assert ran == []
                if after_creation:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    assert tasks[0].cancelled()
                    assert not root._init_owner.active
                else:
                    # No task handle is proof of neither acceptance nor rejection
                    # to production code. Keep the reservation and coroutine.
                    assert root._init_record in root._init_owner.active
                    assert coroutine.cr_frame is not None
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
                # Only this test factory knows it never scheduled this coroutine.
                # Production must retain it when scheduling is ambiguous.
                retained = getattr(root._init_record, "coroutine", None)
                if retained is not None and not after_creation:
                    retained.close()

    asyncio.run(scenario())


def test_borrower_close_does_not_close_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def scenario() -> None:
        client = _Client()
        root = BoundedFalkorDriver(falkor_db=client, concurrency_limit=2)
        clone = cast(BoundedFalkorDriver, root.clone("one"))
        await asyncio.sleep(0)
        try:
            await clone.close()
            assert client.closes == 0
        finally:
            await root.close()
        assert client.closes == 1

    asyncio.run(scenario())


def test_cancelled_readiness_waiter_does_not_cancel_owned_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)

    async def scenario() -> None:
        root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)
        waiter = asyncio.create_task(_ready(root))
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert root._init_task is not None
            assert not root._init_task.done()
        finally:
            await root.close()

    asyncio.run(scenario())


def test_search_driver_lease_cancelled_creator_does_not_cancel_shared_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal builds
        builds += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
    root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)

    async def scenario() -> None:
        index = graphiti.GraphitiIndex.__new__(graphiti.GraphitiIndex)
        index._driver = root
        index._init_owner = root._init_owner
        index._search_driver_leases = {}
        creator_entered = asyncio.Event()
        borrower_entered = asyncio.Event()

        async def borrow(entered: asyncio.Event) -> Any:
            with index._lease_search_driver("shared") as driver:
                entered.set()
                await _ready(driver)
                return driver

        creator = asyncio.create_task(borrow(creator_entered))
        await asyncio.wait_for(creator_entered.wait(), timeout=1.0)
        borrower = asyncio.create_task(borrow(borrower_entered))
        await asyncio.wait_for(borrower_entered.wait(), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        leased = index._search_driver_leases["shared"]
        assert leased.refcount == 2
        assert builds == 1

        creator.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creator
        assert leased.refcount == 1
        leased_driver = cast(BoundedFalkorDriver, leased.driver)
        assert leased_driver._init_task is not None
        assert not leased_driver._init_task.done()

        release.set()
        assert await borrower is leased.driver
        assert index._search_driver_leases == {}
        assert not root._init_owner.active
        await root.close()

    asyncio.run(scenario())


def test_search_driver_lease_shared_init_failure_reaches_all_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    async def build(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal builds
        builds += 1
        started.set()
        await release.wait()
        raise RuntimeError("schema init failed")

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
    root = BoundedFalkorDriver(falkor_db=_Client(), concurrency_limit=2)

    async def scenario() -> None:
        index = graphiti.GraphitiIndex.__new__(graphiti.GraphitiIndex)
        index._driver = root
        index._init_owner = root._init_owner
        index._search_driver_leases = {}
        entered = [asyncio.Event(), asyncio.Event()]

        async def borrow(signal: asyncio.Event) -> None:
            with index._lease_search_driver("shared") as driver:
                signal.set()
                await _ready(driver)

        borrowers = [asyncio.create_task(borrow(signal)) for signal in entered]
        await asyncio.wait_for(
            asyncio.gather(*(signal.wait() for signal in entered)), timeout=1.0
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert builds == 1
        assert index._search_driver_leases["shared"].refcount == 2

        release.set()
        outcomes = await asyncio.gather(*borrowers, return_exceptions=True)
        assert len(outcomes) == 2
        assert all(
            isinstance(outcome, RuntimeError) and str(outcome) == "schema init failed"
            for outcome in outcomes
        )
        assert index._search_driver_leases == {}
        assert root._init_owner.result.init_failed
        assert root._init_owner.result.first_code == "graphiti_init_failed"
        assert not root._init_owner.active
        with pytest.raises(GraphitiIndexError, match="graphiti_init_failed"):
            await root.close()

    asyncio.run(scenario())
