"""The P-41 background delivery loop: lifecycle, resilience and shutdown,
plus the test-mode in-memory index the image smoke needs.

Two levels. The loop function is driven directly against a real catalogue
whose outbox rows were written by a real ingest, with counting adapters
standing in for Attic and Graphiti — so the deliverers do their genuine
work and the test observes only the loop's own behaviour. The composed
application is then exercised through its real lifespan, proving the loop
starts after verification, drains while the server runs, and stops without
abandoning durable work.

Timing stays out of the assertions: intervals are tiny and the tests wait
on an event the adapter sets, rather than sleeping and hoping.
"""

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

import cairn.runtime.composition as composition
import cairn.runtime.delivery as runtime_delivery
from cairn.authority.credentials import mint_token
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority, IngestAssertion
from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions, Committed
from cairn.catalogue.verification import VerificationError
from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.delivery import DeliveryReport
from cairn.projection.memory import (
    MemoryIndex,
    MemoryIndexRefused,
    build_memory_index,
)
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    DeliveryConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)
from cairn.runtime.delivery import run_delivery_loop
from cairn.runtime.logging import configure_logging
from cairn.screening import SecretScreen

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
PRINCIPAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")


def _config(
    data_path: Path,
    *,
    attic_enabled: bool = False,
    graphiti_enabled: bool = False,
    interval_seconds: int = 1,
    chunk_size: int = 1,
) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
        attic=AtticConfig(enabled=attic_enabled),
        graphiti=GraphitiConfig(enabled=graphiti_enabled),
        delivery=DeliveryConfig(
            interval_seconds=interval_seconds, chunk_size=chunk_size
        ),
    )


# --- adapters ----------------------------------------------------------------


class _CountingAttic:
    """Counts drains and sets an event on each, so a test can wait for a
    pass rather than sleeping past one."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises
        self.drained = threading.Event()

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        self.calls += 1
        self.drained.set()
        if self.raises:
            raise RuntimeError("attic is unwell")
        return PayloadStored()

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        return PayloadAbsent()

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        return ()


class _CountingIndex:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises
        self.drained = threading.Event()

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.calls += 1
        self.drained.set()
        if self.raises:
            raise RuntimeError("index is unwell")
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


class _BulkRecordingIndex:
    """Records whether ``project`` or ``project_many`` ran, and sets
    ``drained`` from whichever path fires — the loop-level check that
    ``chunk_size`` actually reaches the bulk path (P-82)."""

    def __init__(self) -> None:
        self.single_calls = 0
        self.bulk_calls = 0
        self.drained = threading.Event()

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.single_calls += 1
        self.drained.set()
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk_calls += 1
        self.drained.set()
        return tuple(FactProjected() for _ in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


class _RaisingTransactions:
    """Stands in for ``CatalogueTransactions`` and fails every batch fetch —
    the unexpected-failure arm the deliverers do *not* convert into a
    recorded attempt, and therefore the one the loop's guard must absorb."""

    def __init__(self) -> None:
        self.calls = 0
        self.raised = threading.Event()

    def execute_compound(self, work: Callable[[object], object]) -> object:
        self.calls += 1
        self.raised.set()
        raise RuntimeError("catalogue is unwell")


# --- the loop function -------------------------------------------------------


def test_the_loop_drains_both_queues_each_pass(tmp_path: Path) -> None:
    config = _config(tmp_path, attic_enabled=True)
    _seed_instance(config)
    _ingest(tmp_path, with_payload=True)
    assert _depth(tmp_path, "evidence_outbox") == 1
    assert _depth(tmp_path, "projection_outbox") == 1
    attic = _CountingAttic()
    index = _CountingIndex()

    _run_loop_until(tmp_path, attic=attic, index=index, until=(attic, index))

    assert attic.calls >= 1
    assert index.calls >= 1
    assert _depth(tmp_path, "evidence_outbox") == 0
    assert _depth(tmp_path, "projection_outbox") == 0


def test_a_raising_drain_is_logged_and_the_loop_survives() -> None:
    """The property that matters at three in the morning: an adapter or
    catalogue defect degrades delivery, it does not take the instance down.
    The failure reaches an operator, and the class name is all that is
    logged — I-32 forbids the message, which may carry scope or body."""
    stream = StringIO()
    logger = configure_logging(stream)
    transactions = _RaisingTransactions()
    attic = _CountingAttic()

    async def exercise() -> None:
        async with anyio.create_task_group() as scope:
            scope.start_soon(
                lambda: run_delivery_loop(
                    cast(CatalogueTransactions, transactions),
                    attic=attic,
                    index=None,
                    interval_seconds=0.01,
                    clock=lambda: NOW,
                    logger=logger,
                )
            )
            await _await_event(transactions.raised)
            # Long enough for several further passes: the loop must keep
            # going rather than having died on the first raise.
            await anyio.sleep(0.15)
            scope.cancel_scope.cancel()

    anyio.run(exercise)

    assert transactions.calls > 1
    logged = stream.getvalue()
    assert '"event": "evidence_delivery_failed"' in logged
    assert '"exception_type": "RuntimeError"' in logged
    assert "catalogue is unwell" not in logged


def test_a_disabled_subsystem_is_not_drained(tmp_path: Path) -> None:
    """Attic disabled: its queue is left alone while the projection queue
    still drains. The evidence row here was written while Attic was on, so
    this is the operator turning it off with work outstanding."""
    config = _config(tmp_path, attic_enabled=True)
    _seed_instance(config)
    _ingest(tmp_path, with_payload=True)
    index = _CountingIndex()

    _run_loop_until(tmp_path, attic=None, index=index, until=(index,))

    assert _depth(tmp_path, "projection_outbox") == 0
    assert _depth(tmp_path, "evidence_outbox") == 1


def test_the_loop_forwards_chunk_size_to_the_bulk_path(tmp_path: Path) -> None:
    """P-82: chunk_size threads from run_delivery_loop through _drain_once
    into deliver_projection_outbox — with it above one, the bulk path
    runs, not the per-fact one."""
    config = _config(tmp_path, graphiti_enabled=True)
    _seed_instance(config)
    _ingest(tmp_path, with_payload=False)
    index = _BulkRecordingIndex()
    transactions = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW,
        uuid_factory=uuid4,
    )

    async def exercise() -> None:
        async with anyio.create_task_group() as scope:
            scope.start_soon(
                lambda: run_delivery_loop(
                    transactions,
                    attic=None,
                    index=index,
                    interval_seconds=0.01,
                    clock=lambda: NOW,
                    chunk_size=2,
                )
            )
            await _await_event(index.drained)
            await anyio.sleep(0.1)
            scope.cancel_scope.cancel()

    anyio.run(exercise)

    assert index.bulk_calls >= 1
    assert index.single_calls == 0


def test_the_loop_runs_with_both_subsystems_absent() -> None:
    """No adapters at all is a legitimate deployment — the default one —
    and the loop simply sleeps, rather than the lifecycle needing a
    conditional task."""
    transactions = _RaisingTransactions()

    async def exercise() -> None:
        with anyio.move_on_after(0.05):
            await run_delivery_loop(
                cast(CatalogueTransactions, transactions),
                attic=None,
                index=None,
                interval_seconds=0.01,
                clock=lambda: NOW,
            )

    anyio.run(exercise)

    assert transactions.calls == 0


# --- the composed application ------------------------------------------------


def test_the_loop_drains_a_real_ingest_and_stops_cleanly(tmp_path: Path) -> None:
    """End to end through the real lifespan: an ingest over ``/v1`` queues
    projection work, the loop drains it while the server runs, and shutdown
    leaves nothing outstanding."""
    config = _config(tmp_path, graphiti_enabled=True)
    token = _seed_instance(config)
    index = _CountingIndex()
    application = composition.build_application(config, index_adapter=index)

    async def exercise() -> None:
        async with LifespanManager(application):
            await _ingest_over_http(application, token)
            await _await_event(index.drained, 10.0)

    asyncio.run(exercise())

    assert index.calls >= 1
    assert _depth(tmp_path, "projection_outbox") == 0


def test_the_loop_does_not_start_before_verification(tmp_path: Path) -> None:
    """A catalogue that fails verification must not have been drained: the
    loop starts after that gate, so a failed start leaves the outbox
    untouched rather than delivering from an instance that never proved it
    owns the data."""
    config = _config(tmp_path, graphiti_enabled=True)
    index = _CountingIndex()
    application = composition.build_application(config, index_adapter=index)

    async def exercise() -> None:
        with pytest.raises(VerificationError):
            async with LifespanManager(application):
                pass

    asyncio.run(exercise())

    assert index.calls == 0


def test_undelivered_rows_survive_a_shutdown(tmp_path: Path) -> None:
    """I-25: shutdown leaves undelivered rows durable. The index refuses
    every projection, so the loop records failed attempts and the row is
    still queued — with its attempt recorded — after the instance stops."""
    config = _config(tmp_path, graphiti_enabled=True)
    token = _seed_instance(config)
    index = _CountingIndex(raises=True)
    application = composition.build_application(config, index_adapter=index)

    async def exercise() -> None:
        async with LifespanManager(application):
            await _ingest_over_http(application, token)
            await _await_event(index.drained, 10.0)

    asyncio.run(exercise())

    assert _depth(tmp_path, "projection_outbox") == 1
    assert _max_attempts(tmp_path) >= 1


def test_the_configured_index_is_used_when_no_adapter_is_injected(
    tmp_path: Path,
) -> None:
    """The composition seam is an override, not the only path: a test-mode
    instance with graphiti enabled builds the in-memory index itself, and
    the ingested fact becomes searchable once the loop has drained."""
    config = _config(tmp_path, graphiti_enabled=True)
    token = _seed_instance(config)
    application = composition.build_application(config)

    async def exercise() -> None:
        async with LifespanManager(application):
            await _ingest_over_http(application, token)
            await anyio.to_thread.run_sync(_wait_for_drain, tmp_path)

    asyncio.run(exercise())

    assert _depth(tmp_path, "projection_outbox") == 0


def test_the_composed_application_forwards_configured_chunk_size(
    tmp_path: Path,
) -> None:
    """P-82: composition.py's ``partial(run_delivery_loop, ...)`` (near
    line 455) reads ``config.delivery.chunk_size`` — end to end through
    the real lifespan, not just as an explicit argument to the loop
    function, so a chunk_size above one reaches the bulk path from a
    genuinely composed application rather than only from a hand-built
    call."""
    config = _config(tmp_path, graphiti_enabled=True, chunk_size=2)
    token = _seed_instance(config)
    index = _BulkRecordingIndex()
    application = composition.build_application(config, index_adapter=index)

    async def exercise() -> None:
        async with LifespanManager(application):
            await _ingest_over_http(application, token)
            await _await_event(index.drained, 10.0)

    asyncio.run(exercise())

    assert index.bulk_calls >= 1
    assert index.single_calls == 0


# --- the in-memory index ------------------------------------------------------


def test_the_memory_index_is_refused_outside_test_mode() -> None:
    with pytest.raises(MemoryIndexRefused):
        build_memory_index("production")


def test_the_memory_index_matches_projected_bodies_within_its_partitions() -> None:
    index = MemoryIndex()
    first = _projected("aaaaaaaa-0000-4000-8000-000000000001", "the build is green")
    second = _projected("aaaaaaaa-0000-4000-8000-000000000002", "the build is RED")
    elsewhere = _projected(
        "aaaaaaaa-0000-4000-8000-000000000003",
        "the build is green",
        partition_key="other",
    )
    for state in (first, second, elsewhere):
        assert index.project(state) == FactProjected()

    assert index.search("BUILD", 10, ("here",)) == (first.fact_id, second.fact_id)
    assert index.search("red", 10, ("here",)) == (second.fact_id,)
    # P-48 as amended, 10 August 2026: ``limit`` is a fetch bound, not a
    # return cap. This line previously asserted that a limit of 1 returned
    # one candidate; truncating here decides candidate membership, which
    # is I-82's at the reconciliation layer, so every match comes back.
    assert index.search("build", 1, ("here",)) == (first.fact_id, second.fact_id)
    assert index.search("build", 10, ("other",)) == (elsewhere.fact_id,)
    assert index.search("absent", 10, ("here",)) == ()


def test_the_memory_index_clears_by_partition_and_entirely() -> None:
    index = MemoryIndex()
    here = _projected("aaaaaaaa-0000-4000-8000-000000000001", "kept")
    there = _projected(
        "aaaaaaaa-0000-4000-8000-000000000002", "dropped", partition_key="other"
    )
    index.project(here)
    index.project(there)

    index.clear(("other",))
    assert index.search("dropped", 10, ("other",)) == ()
    assert index.search("kept", 10, ("here",)) == (here.fact_id,)

    index.clear(None)
    assert index.search("kept", 10, ("here",)) == ()


def test_reprojection_replaces_state_and_keeps_position() -> None:
    index = MemoryIndex()
    first = _projected("aaaaaaaa-0000-4000-8000-000000000001", "first body")
    second = _projected("aaaaaaaa-0000-4000-8000-000000000002", "second body")
    index.project(first)
    index.project(second)

    index.project(_projected(str(first.fact_id), "first body amended"))

    assert index.search("body", 10, ("here",)) == (first.fact_id, second.fact_id)
    assert index.search("amended", 10, ("here",)) == (first.fact_id,)


# --- helpers ------------------------------------------------------------------


def _seed_instance(config: CairnConfig) -> str:
    """A migrated catalogue with one realm and one actor holding every data
    operation, by the conformance harness's direct-row recipe (bootstrap is
    CLI-only per I-63). Returns the bearer token."""
    data_path = config.paths.data
    config.paths.credentials.mkdir(parents=True, exist_ok=True)
    migrate_catalogue(config, lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (REALM, TS)
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'human', 'loop-actor', ?)",
            (str(PRINCIPAL_ID), TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, TS),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, '[]', ?, 'restricted', ?, NULL, NULL, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                '["ingest","invalidate","promote","retrieve"]',
                '["internal","public","restricted"]',
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()
    return minted.text


def _ingest(data_path: Path, *, with_payload: bool) -> None:
    """One assertion through the real authority, so the outbox rows the loop
    drains are the ones production would write."""
    authority = CairnAuthority(
        data_path,
        CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=lambda: NOW,
            uuid_factory=uuid4,
        ),
        lambda: NOW,
        uuid4,
        exact_evidence_enabled=with_payload,
        screen=SecretScreen(),
    )
    outcome = authority.ingest(
        Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
        IngestAssertion(
            scope=Scope(REALM, ()),
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=(
                FactDraft(body="the loop drains this", valid_from=None, valid_to=None),
            ),
            requested_trust=TrustClass.CANDIDATE,
            evidence_payload=b"loop evidence" if with_payload else None,
        ),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
    )
    assert isinstance(outcome, Committed)


async def _ingest_over_http(application: FastAPI, token: str) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://cairn"
    ) as client:
        response = await client.post(
            "/v1/ingest",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "scope": {"realm": REALM, "segments": []},
                "classification": "internal",
                "source_type": "human",
                "facts": [{"body": "the loop drains this"}],
            },
        )
        assert response.status_code == 200, response.text


def _run_loop_until(
    data_path: Path,
    *,
    attic: _CountingAttic | None,
    index: _CountingIndex | None,
    until: tuple[_CountingAttic | _CountingIndex, ...],
) -> None:
    transactions = CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW,
        uuid_factory=uuid4,
    )

    async def exercise() -> None:
        async with anyio.create_task_group() as scope:
            scope.start_soon(
                lambda: run_delivery_loop(
                    transactions,
                    attic=attic,
                    index=index,
                    interval_seconds=0.01,
                    clock=lambda: NOW,
                )
            )
            for adapter in until:
                await _await_event(adapter.drained)
            # One further interval, so the confirming write of the last
            # delivery has landed before the assertions read the outbox.
            await anyio.sleep(0.1)
            scope.cancel_scope.cancel()

    anyio.run(exercise)


def _projected(
    fact_id: str, body: str, *, partition_key: str = "here"
) -> ProjectedFactState:
    return ProjectedFactState(
        fact_id=UUID(fact_id),
        partition_key=partition_key,
        body=body,
        realm_id=REALM,
        segments=(),
        classification=Classification.INTERNAL,
        trust=TrustClass.VALIDATED,
        recorded_at=NOW,
        valid_from=None,
        valid_to=None,
        invalidated_at=None,
    )


async def _await_event(event: threading.Event, seconds: float = 5.0) -> None:
    """Waits on a ``threading.Event`` without blocking the event loop.

    The adapters set theirs from the worker thread a drain runs on, so the
    wait itself goes to a thread too: blocking on ``Event.wait`` here would
    stall the very loop that is supposed to be running the drain.
    """
    assert await anyio.to_thread.run_sync(partial(event.wait, seconds)), (
        "the delivery loop did not reach the adapter within its bound"
    )


def _wait_for_drain(data_path: Path, seconds: float = 10.0) -> None:
    """Polls the outbox from a worker thread until it empties. Used where
    the adapter is the composition's own and so has no event to set."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _depth(data_path, "projection_outbox") == 0:
            return
        time.sleep(0.02)
    raise AssertionError("the projection outbox did not drain within its bound")


def _depth(data_path: Path, table: str) -> int:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return cast(
            int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
    finally:
        connection.close()


def _max_attempts(data_path: Path) -> int:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        row = connection.execute(
            "SELECT MAX(attempts) FROM projection_outbox"
        ).fetchone()
    finally:
        connection.close()
    return cast(int, row[0] or 0)


def test_a_progressing_pass_continues_without_the_interval_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-86's trough: at interval 5 the loop slept after every pass even
    with rows queued, idling the provider pipe between passes. A pass that
    delivered something with work still remaining now rolls straight into
    the next pass; the interval only paces an idle or stalled queue. The
    interval here is far longer than the test's own bound, so reaching the
    second pass at all proves the sleep was skipped."""
    second_pass = threading.Event()
    passes: list[float] = []

    def fake_projection(*arguments: object, **keywords: object) -> DeliveryReport:
        passes.append(time.monotonic())
        if len(passes) >= 2:
            second_pass.set()
            return DeliveryReport(delivered=1, failed=0, remaining=0)
        return DeliveryReport(delivered=1, failed=0, remaining=5)

    monkeypatch.setattr(runtime_delivery, "deliver_projection_outbox", fake_projection)

    async def exercise() -> None:
        async with anyio.create_task_group() as scope:
            scope.start_soon(
                lambda: run_delivery_loop(
                    cast(CatalogueTransactions, _RaisingTransactions()),
                    attic=None,
                    index=cast(IndexAdapter, object()),
                    interval_seconds=30.0,
                    clock=lambda: NOW,
                )
            )
            await _await_event(second_pass)
            scope.cancel_scope.cancel()

    anyio.run(exercise)

    assert len(passes) >= 2
    assert passes[1] - passes[0] < 5.0


def test_a_stalled_pass_still_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows remaining with nothing delivered is an adapter outage, not
    progress — continuing immediately would hammer a dead adapter in a hot
    loop. The pass count over a fixed window stays near what the interval
    allows."""
    passes: list[float] = []

    def fake_projection(*arguments: object, **keywords: object) -> DeliveryReport:
        passes.append(time.monotonic())
        return DeliveryReport(delivered=0, failed=1, remaining=5)

    monkeypatch.setattr(runtime_delivery, "deliver_projection_outbox", fake_projection)

    async def exercise() -> None:
        with anyio.move_on_after(0.5):
            await run_delivery_loop(
                cast(CatalogueTransactions, _RaisingTransactions()),
                attic=None,
                index=cast(IndexAdapter, object()),
                interval_seconds=0.1,
                clock=lambda: NOW,
            )

    anyio.run(exercise)

    assert 1 <= len(passes) <= 8


def test_the_outbox_fetch_ceiling_follows_chunk_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-86: `deliver_projection_outbox`'s default `limit` of 100 capped
    the effective chunk at min(chunk_size, 100). The loop now passes the
    ceiling through, so a configured chunk above 100 takes effect — and
    the historic ceiling holds for anything smaller."""
    seen: list[int] = []
    done = threading.Event()

    def fake_projection(*arguments: object, **keywords: object) -> DeliveryReport:
        seen.append(cast(int, keywords.get("limit")))
        done.set()
        return DeliveryReport(delivered=0, failed=0, remaining=0)

    monkeypatch.setattr(runtime_delivery, "deliver_projection_outbox", fake_projection)

    for chunk_size, expected_limit in ((200, 200), (2, 100)):
        seen.clear()
        done.clear()

        async def exercise(chunk_size: int = chunk_size) -> None:
            async with anyio.create_task_group() as scope:
                scope.start_soon(
                    lambda: run_delivery_loop(
                        cast(CatalogueTransactions, _RaisingTransactions()),
                        attic=None,
                        index=cast(IndexAdapter, object()),
                        interval_seconds=30.0,
                        clock=lambda: NOW,
                        chunk_size=chunk_size,
                    )
                )
                await _await_event(done)
                scope.cancel_scope.cancel()

        anyio.run(exercise)

        assert seen and seen[0] == expected_limit
