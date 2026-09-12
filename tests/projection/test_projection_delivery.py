"""P-40: the projection deliverer, and P-39's other half — that a
graphiti-disabled instance queues no projection work at all.

Scaffolding mirrors ``tests/evidence/test_delivery.py``: a real migrated
catalogue, a real realm and ingester seeded directly, and real ingest /
promote / invalidate commands producing the outbox rows under test. What
differs is what the rows carry — nothing (I-68) — so most of these tests
are about state read at delivery time rather than bytes carried from
enqueue.
"""

import itertools
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import (
    CairnAuthority,
    IngestAssertion,
    InvalidateFacts,
)
from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.catalogue.transactions import CatalogueTransactions, Committed
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.delivery import (
    DeliveryReport,
    _chunk_batch,
    _oldest_age_seconds,
    _parse_batch,
    deliver_projection_outbox,
)
from cairn.projection.partition import canonical_partition, canonical_segments_json
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import LogEvent, configure_logging
from cairn.screening import SecretScreen

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 8, 9, 10, 0, 0, tzinfo=UTC)
_REALM = "acme"
_JOB = ScopeSegment(kind="job", identifier="job-1")
_SCOPE = Scope(_REALM, (_JOB,))
_AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_AGENT_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")
# Passes migration 0004's shape CHECK but fails UUID(): the CHECK's `?`
# wildcards accept a dash where UUID() requires a hex digit.
_HOSTILE_WORK_ID = "0000000--0000-4000-8000-000000000000"
_ALL_CLASSIFICATIONS = frozenset(Classification)


class RecordingIndex:
    """Accepts everything and remembers what it was asked to project."""

    def __init__(self) -> None:
        self.projected: list[ProjectedFactState] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.projected.append(state)
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


class RaisingIndex:
    """P-14: adapter infrastructure failures raise."""

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        raise RuntimeError("index unreachable")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


class RefusingIndex:
    """Returns the typed domain refusal, with a code of the caller's
    choosing so the storage guard can be exercised."""

    def __init__(self, code: str = "index_rebuilding") -> None:
        self._code = code

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return ProjectionFailed(code=self._code)

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


class StrangeResultIndex:
    """Returns a value the Protocol does not enforce at runtime."""

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return cast(FactProjected, "delivered, honest")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


# --- scaffolding -------------------------------------------------------


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=_INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _uuid_seq(start: int) -> Callable[[], UUID]:
    counter = itertools.count(start)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def _idem(n: int) -> UUID:
    return UUID(f"{n:08x}-0000-4000-8000-000000000000")


def _json_column(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _seed_catalogue(data_path: Path) -> None:
    migrate_catalogue(_config(data_path), lambda: _NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (_REALM, canonical_timestamp(_NOW)),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (_REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                str(_AGENT_ID),
                PrincipalKind.WORKLOAD.value,
                "agent",
                canonical_timestamp(_NOW),
            ),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (
                str(_AGENT_CREDENTIAL_ID),
                str(_AGENT_ID),
                bytes(32),
                canonical_timestamp(_NOW),
            ),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(_GRANT_ID),
                str(_AGENT_ID),
                _REALM,
                _json_column([{"id": _JOB.identifier, "kind": _JOB.kind}]),
                _json_column(
                    [
                        GrantOperation.INGEST.value,
                        GrantOperation.INVALIDATE.value,
                        GrantOperation.PROMOTE.value,
                        GrantOperation.RETRIEVE.value,
                    ]
                ),
                Classification.RESTRICTED.value,
                _json_column(sorted(c.value for c in _ALL_CLASSIFICATIONS)),
                "2027-01-01T00:00:00.000000Z",
                canonical_timestamp(_NOW),
            ),
        )
        connection.commit()


def _transactions(
    data_path: Path, *, now: datetime = _NOW, uuid_seed: int = 0x10000000
) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: now,
        uuid_factory=_uuid_seq(uuid_seed),
    )


def _authority(
    data_path: Path,
    *,
    now: datetime = _NOW,
    seed: int = 0x30000000,
    retrieval_index_enabled: bool = True,
) -> CairnAuthority:
    return CairnAuthority(
        data_path,
        _transactions(data_path, now=now, uuid_seed=seed),
        clock=lambda: now,
        uuid_factory=_uuid_seq(seed),
        exact_evidence_enabled=False,
        screen=SecretScreen(),
        retrieval_index_enabled=retrieval_index_enabled,
    )


def _actor() -> Actor:
    return Actor(principal_id=_AGENT_ID, credential_id=_AGENT_CREDENTIAL_ID)


def _ingest(
    data_path: Path,
    bodies: tuple[str, ...],
    n: int,
    *,
    now: datetime = _NOW,
    retrieval_index_enabled: bool = True,
) -> tuple[UUID, ...]:
    authority = _authority(
        data_path,
        now=now,
        seed=0x30000000 + n * 0x1000,
        retrieval_index_enabled=retrieval_index_enabled,
    )
    outcome = authority.ingest(
        _actor(),
        IngestAssertion(
            scope=_SCOPE,
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=tuple(
                FactDraft(body=body, valid_from=None, valid_to=None) for body in bodies
            ),
            requested_trust=TrustClass.CANDIDATE,
        ),
        idempotency_key=_idem(n),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed), outcome
    return outcome.value.fact_ids


def _invalidate(
    data_path: Path, fact_ids: tuple[UUID, ...], n: int, *, now: datetime
) -> None:
    authority = _authority(data_path, now=now, seed=0x50000000 + n * 0x1000)
    outcome = authority.invalidate(
        _actor(),
        InvalidateFacts(fact_ids=fact_ids, reason="superseded", superseded_by=None),
        idempotency_key=_idem(0x900 + n),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed), outcome


def _outbox_rows(data_path: Path) -> list[tuple[object, ...]]:
    with _open_write_connection(data_path, create=False) as connection:
        return connection.execute(
            "SELECT sequence, work_id, kind, fact_id, attempts, last_attempt_at, "
            "last_failure_code FROM projection_outbox ORDER BY sequence"
        ).fetchall()


def _audit_event_count(data_path: Path) -> int:
    with _open_write_connection(data_path, create=False) as connection:
        row = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()
        return cast(int, row[0])


# --- P-39: no index, no queued work ------------------------------------


def test_a_disabled_index_queues_no_projection_work(tmp_path: Path) -> None:
    """P-39: with no index there is no deliverer, so rows would only
    accumulate. The catalogue is authoritative and rebuild-index
    re-projects from it, so nothing is lost."""
    _seed_catalogue(tmp_path)

    fact_ids = _ingest(
        tmp_path, ("the build is green",), 1, retrieval_index_enabled=False
    )

    assert len(fact_ids) == 1
    assert _outbox_rows(tmp_path) == []


def test_an_enabled_index_queues_one_row_per_fact(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)

    fact_ids = _ingest(tmp_path, ("the build is green", "the tests pass"), 1)

    rows = _outbox_rows(tmp_path)
    assert [row[2] for row in rows] == ["fact-ingested", "fact-ingested"]
    assert {row[3] for row in rows} == {str(fact_id) for fact_id in fact_ids}


# --- P-40: delivery ----------------------------------------------------


def test_delivery_of_an_empty_outbox_reports_zero(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    index = RecordingIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=0)
    assert index.projected == []


def test_a_pending_row_projects_current_state_and_is_confirmed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("the build is green",), 1)
    index = RecordingIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=1, failed=0, remaining=0)
    assert _outbox_rows(tmp_path) == []
    (state,) = index.projected
    assert state.fact_id == fact_ids[0]
    assert state.body == "the build is green"
    assert state.realm_id == _REALM
    assert state.segments == (_JOB,)
    assert state.classification is Classification.INTERNAL
    assert state.trust is TrustClass.CANDIDATE
    assert state.invalidated_at is None
    assert state.partition_key == canonical_partition(
        _REALM, canonical_segments_json((_JOB,))
    )


def test_a_fact_invalidated_after_enqueue_projects_as_invalidated(
    tmp_path: Path,
) -> None:
    """I-68's rows are content-free, so what is delivered is the state the
    catalogue holds *now*. This is the property that lets one project()
    serve all three work kinds."""
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("the build is green",), 1)
    later = _NOW + timedelta(minutes=5)
    _invalidate(tmp_path, fact_ids, 1, now=later)
    index = RecordingIndex()

    deliver_projection_outbox(_transactions(tmp_path), index, clock=lambda: later)

    # Two rows: the ingest's and the invalidation's, both for one fact.
    assert len(index.projected) == 2
    for state in index.projected:
        assert state.fact_id == fact_ids[0]
        assert state.invalidated_at == later
    assert _outbox_rows(tmp_path) == []


def test_rows_deliver_in_sequence_order_and_the_limit_bounds_one_run(
    tmp_path: Path,
) -> None:
    """I-78: two rows written in one transaction share a created_at, so
    ordering is the sequence column's job and nothing else's."""
    _seed_catalogue(tmp_path)
    first = _ingest(tmp_path, ("fact one", "fact two"), 1)
    second = _ingest(tmp_path, ("fact three",), 2)
    index = RecordingIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW, limit=2
    )

    assert report == DeliveryReport(delivered=2, failed=0, remaining=1)
    assert [state.fact_id for state in index.projected] == list(first)
    remaining = _outbox_rows(tmp_path)
    assert [row[3] for row in remaining] == [str(second[0])]


def test_a_raising_index_records_a_retryable_attempt_and_retains_the_row(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)
    later = _NOW + timedelta(seconds=30)

    report = deliver_projection_outbox(
        _transactions(tmp_path), RaisingIndex(), clock=lambda: later
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    (row,) = _outbox_rows(tmp_path)
    assert row[4] == 1
    assert row[5] == canonical_timestamp(later)
    assert row[6] == "index_unavailable"


def test_a_typed_refusal_keeps_its_own_code(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    report = deliver_projection_outbox(
        _transactions(tmp_path), RefusingIndex(), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    (row,) = _outbox_rows(tmp_path)
    assert row[6] == "index_rebuilding"


def test_an_unstorable_refusal_code_is_replaced_not_dropped(
    tmp_path: Path,
) -> None:
    """The refusal is real and must be recorded; only its spelling is the
    adapter's to get wrong. A code the column's CHECK would reject would
    otherwise abort the confirming transaction and lose the attempt."""
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    report = deliver_projection_outbox(
        _transactions(tmp_path),
        RefusingIndex(code="Index Unavailable; DROP TABLE facts--"),
        clock=lambda: _NOW,
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    (row,) = _outbox_rows(tmp_path)
    assert row[6] == "index_refused"


def test_an_unrecognised_result_is_treated_as_unavailable_not_delivered(
    tmp_path: Path,
) -> None:
    """Fail-closed, as the evidence deliverer is: only an explicit
    FactProjected deletes a row."""
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    report = deliver_projection_outbox(
        _transactions(tmp_path), StrangeResultIndex(), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    (row,) = _outbox_rows(tmp_path)
    assert row[6] == "index_unavailable"


def test_pending_work_survives_restart_and_completes_on_a_second_run(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)
    deliver_projection_outbox(
        _transactions(tmp_path), RaisingIndex(), clock=lambda: _NOW
    )

    index = RecordingIndex()
    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=1, failed=0, remaining=0)
    assert len(index.projected) == 1
    assert _outbox_rows(tmp_path) == []


def test_a_row_another_deliverer_won_is_skipped_not_double_counted(
    tmp_path: Path,
) -> None:
    """The ``(work_id, attempts)`` condition is defence in depth behind the
    single-consumer rule: zero affected rows means another deliverer won,
    and the run skips it rather than counting a delivery it did not make."""
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    class _StealingIndex:
        def project(
            self, state: ProjectedFactState
        ) -> FactProjected | ProjectionFailed:
            with _open_write_connection(tmp_path, create=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM projection_outbox")
                connection.commit()
            return FactProjected()

        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            return tuple(self.project(state) for state in states)

        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            raise NotImplementedError

        def clear(self, partition_keys: tuple[str, ...] | None) -> None:
            raise NotImplementedError

    report = deliver_projection_outbox(
        _transactions(tmp_path), _StealingIndex(), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=0)


def test_an_unreadable_row_is_skipped_logged_and_left_in_place(
    tmp_path: Path,
) -> None:
    """A schema-legal work_id that UUID() cannot parse is never handed to
    the index: there is no identity left to trust it with."""
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("the build is green",), 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projection_outbox")
        connection.execute(
            "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
            "created_at, attempts) VALUES (?, 'fact-ingested', ?, ?, ?, 0)",
            (
                _HOSTILE_WORK_ID,
                str(fact_ids[0]),
                "20000000-0000-4000-8000-000000000000",
                canonical_timestamp(_NOW),
            ),
        )
        connection.commit()
    stream = StringIO()
    logger = configure_logging(stream)
    index = RecordingIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW, logger=logger
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    assert index.projected == []
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(
        event["event"] == LogEvent.PROJECTION_OUTBOX_ROW_UNREADABLE.value
        for event in events
    )


def test_a_row_whose_stored_scope_is_meaningless_is_unreadable_not_crashing(
    tmp_path: Path,
) -> None:
    """The scope_segments CHECK pins minified-array shape only, so
    ``[{"kind": 7, "id": 1}]`` is a legal row whose ScopeSegment
    construction must fail as a typed error rather than escaping."""
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)
    # facts carries a BEFORE UPDATE trigger, so the meaningless scope is
    # inserted as its own row rather than edited into an existing one —
    # which is also the more honest shape: this is what a catalogue
    # restored from a pre-STRICT schema could actually hold.
    hostile_fact_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projection_outbox")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, derived_from, promoted_by, evidence_id, "
            "valid_from, valid_to, recorded_at) "
            "SELECT ?, realm_id, ?, body, trust, classification, assertion_id, "
            "derived_from, promoted_by, evidence_id, valid_from, valid_to, "
            "recorded_at FROM facts LIMIT 1",
            (hostile_fact_id, '[{"id":1,"kind":7}]'),
        )
        connection.execute(
            "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
            "created_at, attempts) VALUES (?, 'fact-ingested', ?, ?, ?, 0)",
            (
                "0000000a-0000-4000-8000-000000000000",
                hostile_fact_id,
                "20000000-0000-4000-8000-000000000000",
                canonical_timestamp(_NOW),
            ),
        )
        connection.commit()
    index = RecordingIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    assert index.projected == []


def test_delivery_appends_no_audit_event(tmp_path: Path) -> None:
    """Delivery is operational work (P-40)."""
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)
    before = _audit_event_count(tmp_path)

    deliver_projection_outbox(
        _transactions(tmp_path), RecordingIndex(), clock=lambda: _NOW
    )

    assert _audit_event_count(tmp_path) == before


def test_metrics_report_depth_and_age_for_the_projection_queue(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("fact one", "fact two"), 1)
    metrics = Metrics()
    later = _NOW + timedelta(seconds=90)

    deliver_projection_outbox(
        _transactions(tmp_path),
        RaisingIndex(),
        clock=lambda: later,
        metrics=metrics,
    )

    rendered, _ = metrics.render()
    body = rendered.decode("utf-8")
    assert f'queue="{OutboxQueue.PROJECTION.value}"' in body
    assert f'queue="{OutboxQueue.EVIDENCE.value}"' not in body


def test_the_completion_log_carries_only_safe_counts(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)
    stream = StringIO()
    logger = configure_logging(stream)

    deliver_projection_outbox(
        _transactions(tmp_path), RecordingIndex(), clock=lambda: _NOW, logger=logger
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    completed = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_DELIVERY_COMPLETED.value
    ]
    assert len(completed) == 1
    assert completed[0]["delivered"] == 1
    # I-32: no scope path, body or partition encoding anywhere in the line.
    line = json.dumps(completed[0])
    assert _JOB.identifier not in line
    assert "the build is green" not in line


# --- the defensive arms, unit level ------------------------------------


def test_an_empty_queue_reports_zero_age_not_an_unreadable_one() -> None:
    assert _oldest_age_seconds(None, _NOW) == 0.0


def test_a_future_dated_row_clamps_to_zero_rather_than_going_negative() -> None:
    """A clock stepping backwards between the write and this read is
    ordinary operational reality, and a negative age would cost the run
    its report over a gauge."""
    ahead = canonical_timestamp(_NOW + timedelta(minutes=5))

    assert _oldest_age_seconds(ahead, _NOW) == 0.0


def test_a_corrupt_created_at_costs_the_age_gauge_not_the_report(
    tmp_path: Path,
) -> None:
    """ck_projection_outbox_created_at is shape-only: '9999-99-99T99:99:...'
    passes it while being no calendar timestamp. Depth is still exact,
    because COUNT(*) parses nothing."""
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("the build is green",), 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projection_outbox")
        connection.execute(
            "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
            "created_at, attempts) VALUES (?, 'fact-ingested', ?, ?, ?, 0)",
            (
                "0000000b-0000-4000-8000-000000000000",
                str(fact_ids[0]),
                "20000000-0000-4000-8000-000000000000",
                "9999-99-99T99:99:99.999999Z",
            ),
        )
        connection.commit()
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    report = deliver_projection_outbox(
        _transactions(tmp_path),
        RaisingIndex(),
        clock=lambda: _NOW,
        metrics=metrics,
        logger=logger,
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rendered, _ = metrics.render()
    assert f'queue="{OutboxQueue.PROJECTION.value}"' not in rendered.decode("utf-8")
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(
        event["event"] == LogEvent.PROJECTION_OUTBOX_AGE_UNREADABLE.value
        for event in events
    )


def _row(**overrides: object) -> tuple[object, ...]:
    row: list[object] = [
        "0000000c-0000-4000-8000-000000000000",  # work_id
        0,  # attempts
        "0000000d-0000-4000-8000-000000000000",  # fact_id
        _REALM,
        canonical_segments_json((_JOB,)),
        "the build is green",
        Classification.INTERNAL.value,
        TrustClass.CANDIDATE.value,
        canonical_timestamp(_NOW),
        None,
        None,
        None,
    ]
    positions = {
        "attempts": 1,
        "realm_id": 3,
        "scope_segments": 4,
        "body": 5,
        "classification": 6,
        "trust": 7,
        "recorded_at": 8,
        "valid_from": 9,
    }
    for name, value in overrides.items():
        row[positions[name]] = value
    return tuple(row)


def test_a_well_formed_row_parses() -> None:
    """The positive that makes the guard tests below meaningful: without
    it they could all pass against a parser that rejected everything."""
    parsed, unreadable = _parse_batch((_row(),))

    assert unreadable == ()
    assert len(parsed) == 1


def test_rows_carrying_values_of_the_wrong_type_are_unreadable() -> None:
    """STRICT bounds a column's storage class, not its meaning, and a
    catalogue restored from a pre-STRICT schema can hold anything. Every
    such row is skipped rather than crashing the run."""
    hostile = (
        _row(attempts="two"),
        _row(body=7),
        _row(realm_id=1),
        _row(scope_segments=None),
        _row(classification=3),
        _row(trust=object()),
        _row(valid_from=17),
        _row(recorded_at="not a timestamp"),
        _row(scope_segments='{"kind":"repo"}'),
        _row(scope_segments='[{"kind":7,"id":1}]'),
        _row(scope_segments='[{"kind":"repo"}]'),
    )

    parsed, unreadable = _parse_batch(hostile)

    assert parsed == ()
    assert len(unreadable) == len(hostile)
    assert set(unreadable) == {"0000000c-0000-4000-8000-000000000000"}


def test_an_unsafe_work_id_is_logged_as_none_rather_than_echoed() -> None:
    """``_parse_batch`` must never hand the logger a value that could make
    ``emit`` raise — the crash the guard exists to prevent."""
    parsed, unreadable = _parse_batch(
        (_row(body=7)[:0] + ("../etc/passwd",) + _row(body=7)[1:],)
    )

    assert parsed == ()
    assert unreadable == (None,)


class _StealThenFail:
    """Deletes the row mid-attempt, then fails — so the conditional
    confirming write finds nothing and the run must not count a failure it
    did not record. Mirrors the evidence deliverer's two equivalents."""

    def __init__(self, data_path: Path, *, raise_it: bool) -> None:
        self._data_path = data_path
        self._raise_it = raise_it

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        with _open_write_connection(self._data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projection_outbox")
            connection.commit()
        if self._raise_it:
            raise RuntimeError("index unreachable")
        return ProjectionFailed(code="index_rebuilding")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


def test_the_conditional_write_defence_skips_a_stolen_row_on_the_raise_path(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    report = deliver_projection_outbox(
        _transactions(tmp_path),
        _StealThenFail(tmp_path, raise_it=True),
        clock=lambda: _NOW,
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=0)


def test_the_conditional_write_defence_skips_a_stolen_row_on_the_refusal_path(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, ("the build is green",), 1)

    report = deliver_projection_outbox(
        _transactions(tmp_path),
        _StealThenFail(tmp_path, raise_it=False),
        clock=lambda: _NOW,
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=0)


def test_an_unreadable_age_without_a_logger_still_completes(tmp_path: Path) -> None:
    """The logger is optional throughout; losing the age gauge must not
    depend on there being somewhere to say so."""
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("the build is green",), 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projection_outbox")
        connection.execute(
            "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
            "created_at, attempts) VALUES (?, 'fact-ingested', ?, ?, ?, 0)",
            (
                "0000000e-0000-4000-8000-000000000000",
                str(fact_ids[0]),
                "20000000-0000-4000-8000-000000000000",
                "9999-99-99T99:99:99.999999Z",
            ),
        )
        connection.commit()

    report = deliver_projection_outbox(
        _transactions(tmp_path),
        RaisingIndex(),
        clock=lambda: _NOW,
        metrics=Metrics(),
    )

    # The row is retained, so its corrupt created_at is still the MIN the
    # gauge would have parsed — the arm this test exists for.
    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)


# --- chunk grouping helpers ---


def _batch_row(
    n: int, fact: UUID, partition: str
) -> tuple[UUID, int, ProjectedFactState]:
    state = ProjectedFactState(
        fact_id=fact,
        partition_key=partition,
        body=f"fact {fact}",
        realm_id="acme",
        segments=(),
        classification=Classification.INTERNAL,
        trust=TrustClass.CANDIDATE,
        recorded_at=_NOW,
        valid_from=None,
        valid_to=None,
        invalidated_at=None,
    )
    return (UUID(int=n), n, state)


def test_chunks_respect_partition_and_size() -> None:
    a, b = "acme\n[]", 'acme\n[{"kind":"job","id":"j"}]'
    batch = (
        _batch_row(1, UUID(int=101), a),
        _batch_row(2, UUID(int=102), b),
        _batch_row(3, UUID(int=103), a),
        _batch_row(4, UUID(int=104), a),
    )
    chunks = _chunk_batch(batch, 2)
    partitions = [{e.state.partition_key for e in chunk} for chunk in chunks]
    assert all(len(p) == 1 for p in partitions)
    assert max(len(chunk) for chunk in chunks) <= 2
    projected = [e.state.fact_id for chunk in chunks for e in chunk]
    assert sorted(projected) == sorted(
        [UUID(int=101), UUID(int=102), UUID(int=103), UUID(int=104)]
    )


def test_rows_sharing_a_fact_collapse_into_one_entry_with_both_rows() -> None:
    fact = UUID(int=105)
    batch = (
        _batch_row(1, fact, "acme\n[]"),
        _batch_row(2, fact, "acme\n[]"),
    )
    chunks = _chunk_batch(batch, 10)
    assert len(chunks) == 1 and len(chunks[0]) == 1
    assert chunks[0][0].rows == ((UUID(int=1), 1), (UUID(int=2), 2))


def test_chunking_preserves_batch_order_within_a_partition() -> None:
    batch = tuple(_batch_row(n, UUID(int=200 + n), "acme\n[]") for n in range(5))
    chunks = _chunk_batch(batch, 2)
    flattened = [e.state.fact_id for chunk in chunks for e in chunk]
    assert flattened == [UUID(int=200 + n) for n in range(5)]


def test_chunking_preserves_global_sequence_order_across_partitions() -> None:
    """I-78: the batch arrives in ``sequence`` order and confirms in that
    order. Interleaved partitions cut chunk boundaries; they never let a
    later row's confirm overtake an earlier row's."""
    a, b = "acme\n[]", 'acme\n[{"kind":"job","id":"j"}]'
    batch = (
        _batch_row(1, UUID(int=101), a),
        _batch_row(2, UUID(int=102), b),
        _batch_row(3, UUID(int=103), a),
    )
    chunks = _chunk_batch(batch, 10)
    flattened = [e.state.fact_id for chunk in chunks for e in chunk]
    assert flattened == [UUID(int=101), UUID(int=102), UUID(int=103)]


def test_non_adjacent_rows_sharing_a_fact_confirm_in_sequence_order() -> None:
    """I-78 within one partition: X@1, Y@2, X@3 must confirm 1, 2, 3.
    Collapsing X@3 into X@1's entry would confirm 1, 3, 2 — so only
    adjacent same-fact runs collapse, and a fact re-appearing later in
    the chunk cuts a new one."""
    x, y = UUID(int=110), UUID(int=111)
    batch = (
        _batch_row(1, x, "acme\n[]"),
        _batch_row(2, y, "acme\n[]"),
        _batch_row(3, x, "acme\n[]"),
    )
    chunks = _chunk_batch(batch, 10)
    confirm_order = [
        work_id for chunk in chunks for e in chunk for work_id, _ in e.rows
    ]
    assert confirm_order == [UUID(int=1), UUID(int=2), UUID(int=3)]


def test_rows_sharing_a_fact_across_a_partition_break_stay_ordered() -> None:
    """Contiguous chunking dedups only within one chunk: a fact whose rows
    straddle another partition's row projects once per run, keeping global
    order — idempotent projection makes the repeat safe."""
    fact = UUID(int=105)
    a, b = "acme\n[]", 'acme\n[{"kind":"job","id":"j"}]'
    batch = (
        _batch_row(1, fact, a),
        _batch_row(2, UUID(int=106), b),
        _batch_row(3, fact, a),
    )
    chunks = _chunk_batch(batch, 10)
    assert [len(chunk) for chunk in chunks] == [1, 1, 1]
    assert chunks[0][0].rows == ((UUID(int=1), 1),)
    assert chunks[2][0].rows == ((UUID(int=3), 3),)


# --- P-82: the chunked delivery path and its fallback -------------------


class BulkRecordingIndex:
    """Records every call so tests can assert which path ran."""

    def __init__(self) -> None:
        self.single: list[ProjectedFactState] = []
        self.bulk: list[tuple[ProjectedFactState, ...]] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.single.append(state)
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk.append(states)
        return tuple(FactProjected() for _ in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise NotImplementedError


class BulkBrokenIndex(BulkRecordingIndex):
    """Bulk always breaks; per-fact works — the demotion case."""

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk.append(states)
        raise RuntimeError("bulk path down")


def _delivery_fixture[IndexT: IndexAdapter](
    tmp_path: Path, *, facts: int, index: IndexT
) -> tuple[CatalogueTransactions, IndexT]:
    _seed_catalogue(tmp_path)
    _ingest(tmp_path, tuple(f"fact {n}" for n in range(facts)), 1)
    return _transactions(tmp_path), index


def test_the_default_chunk_size_never_calls_project_many(tmp_path: Path) -> None:
    transactions, index = _delivery_fixture(
        tmp_path, facts=3, index=BulkRecordingIndex()
    )
    report = deliver_projection_outbox(transactions, index, clock=lambda: _NOW)
    assert report.delivered == 3 and index.bulk == [] and len(index.single) == 3


def test_a_chunked_pass_projects_through_project_many_and_confirms_every_row(
    tmp_path: Path,
) -> None:
    transactions, index = _delivery_fixture(
        tmp_path, facts=5, index=BulkRecordingIndex()
    )
    report = deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=2
    )
    assert report.delivered == 5 and report.remaining == 0
    assert index.single == []
    assert [len(states) for states in index.bulk] == [2, 2, 1]


def test_a_broken_bulk_call_demotes_the_chunk_to_the_per_fact_path(
    tmp_path: Path,
) -> None:
    transactions, index = _delivery_fixture(tmp_path, facts=3, index=BulkBrokenIndex())
    report = deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=3
    )
    assert report.delivered == 3 and report.remaining == 0
    assert len(index.bulk) == 1 and len(index.single) == 3


def test_a_misaligned_bulk_result_also_demotes(tmp_path: Path) -> None:
    class ShortIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return (FactProjected(),)  # always one, whatever was asked

    transactions, index = _delivery_fixture(tmp_path, facts=2, index=ShortIndex())
    report = deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=2
    )
    assert report.delivered == 2 and len(index.single) == 2


def test_an_invalid_result_element_demotes_the_chunk(tmp_path: Path) -> None:
    """An aligned tuple carrying anything that is not an explicit
    ``FactProjected``/``ProjectionFailed`` is not a per-state answer: the
    whole chunk demotes to the per-fact path instead of recording the
    invalid element as a failure."""

    class LyingIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return tuple(
                cast(FactProjected, None) for _ in states
            )  # aligned, but no element is an explicit result

    transactions, index = _delivery_fixture(tmp_path, facts=2, index=LyingIndex())
    report = deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=2
    )
    assert report.delivered == 2 and report.failed == 0
    assert len(index.bulk) == 1 and len(index.single) == 2


def test_a_demoted_chunk_emits_the_bulk_demoted_event(tmp_path: Path) -> None:
    """P-82 gate-4 ruling (25 August 2026): demotion must be observable.
    A chunk that falls back to the per-fact path emits one safe event
    carrying the chunk's size and the exception class that demoted it —
    without it an operator sees only unexplained slowness."""
    stream = StringIO()
    logger = configure_logging(stream)
    transactions, index = _delivery_fixture(tmp_path, facts=3, index=BulkBrokenIndex())

    deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=3, logger=logger
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    assert demoted[0]["chunk_size"] == 3
    assert demoted[0]["exception_type"] == "RuntimeError"
    assert demoted[0]["failure_shape"] == "adapter_raised"
    assert demoted[0].get("adapter_code") is None


class _CodedError(Exception):
    def __init__(self, code: object) -> None:
        self.code = code
        super().__init__("coded adapter failure")


class BulkCodedBrokenIndex(BulkRecordingIndex):
    """Bulk raises an adapter error carrying a safe ``code`` identifier."""

    def __init__(self, code: object) -> None:
        super().__init__()
        self._code = code

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk.append(states)
        raise _CodedError(self._code)


def test_a_demotion_by_a_coded_adapter_error_carries_the_safe_code(
    tmp_path: Path,
) -> None:
    """Six demotions in eight measured runs and every event said only
    ``adapter_raised``: the adapter's safe ``code`` — an identifier by
    contract (I-32) — is exactly the diagnostic the event withheld."""
    stream = StringIO()
    logger = configure_logging(stream)
    transactions, index = _delivery_fixture(
        tmp_path, facts=3, index=BulkCodedBrokenIndex("graphiti_timeout")
    )

    deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=3, logger=logger
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    assert demoted[0]["adapter_code"] == "graphiti_timeout"


@pytest.mark.parametrize(
    "unsafe_code",
    ["Secret Value!", "x" * 65, 42, None],
    ids=["not-an-identifier", "too-long", "not-a-string", "absent"],
)
def test_a_demotion_never_emits_an_unsafe_adapter_code(
    tmp_path: Path, unsafe_code: object
) -> None:
    """Only a string that already looks like a safe identifier travels;
    anything else — arbitrary text, the wrong type — is withheld, so a
    non-adapter exception with a ``code`` attribute cannot smuggle
    content into the log (I-32)."""
    stream = StringIO()
    logger = configure_logging(stream)
    transactions, index = _delivery_fixture(
        tmp_path, facts=3, index=BulkCodedBrokenIndex(unsafe_code)
    )

    deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=3, logger=logger
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    assert demoted[0].get("adapter_code") is None


def _demotion_event(
    tmp_path: Path, index: IndexAdapter, *, facts: int
) -> dict[str, object]:
    stream = StringIO()
    logger = configure_logging(stream)
    transactions, _ = _delivery_fixture(tmp_path, facts=facts, index=index)

    deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=facts, logger=logger
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    return cast(dict[str, object], demoted[0])


def test_a_misaligned_bulk_result_names_the_length_mismatch(tmp_path: Path) -> None:
    """P-82 gate-4 ruling: the event names the chunk size *and* a
    partition-free failure shape. A malformed answer has no exception to
    name, so without the shape the operator sees a size and nothing else."""

    class ShortIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return (FactProjected(),)

    demoted = _demotion_event(tmp_path, ShortIndex(), facts=2)

    assert demoted["chunk_size"] == 2
    assert demoted.get("exception_type") is None
    assert demoted["failure_shape"] == "answer_length_mismatch"


def test_a_non_tuple_bulk_result_names_that_shape(tmp_path: Path) -> None:
    """A bulk answer that is not a tuple at all is a different adapter
    defect from a length mismatch and must read as one."""

    class ListIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return cast(
                tuple[FactProjected | ProjectionFailed, ...],
                [FactProjected() for _ in states],
            )

    demoted = _demotion_event(tmp_path, ListIndex(), facts=2)

    assert demoted["chunk_size"] == 2
    assert demoted.get("exception_type") is None
    assert demoted["failure_shape"] == "answer_not_a_tuple"


def test_an_invalid_result_element_names_that_shape(tmp_path: Path) -> None:
    """An aligned tuple of the right length whose elements are not
    explicit results is the third malformed shape."""

    class LyingIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return tuple(cast(FactProjected, None) for _ in states)

    demoted = _demotion_event(tmp_path, LyingIndex(), facts=2)

    assert demoted["chunk_size"] == 2
    assert demoted.get("exception_type") is None
    assert demoted["failure_shape"] == "answer_element_invalid"


def test_fallback_confirms_stay_conditioned_on_the_observed_row(
    tmp_path: Path,
) -> None:
    """The fallback path inherits the per-fact path's confirm rule: a row
    another deliverer has already won is skipped, never double-counted."""

    class StealingBulkBrokenIndex(BulkBrokenIndex):
        def project(
            self, state: ProjectedFactState
        ) -> FactProjected | ProjectionFailed:
            with _open_write_connection(tmp_path, create=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM projection_outbox")
                connection.commit()
            self.single.append(state)
            return FactProjected()

    transactions, index = _delivery_fixture(
        tmp_path, facts=2, index=StealingBulkBrokenIndex()
    )
    report = deliver_projection_outbox(
        transactions, index, clock=lambda: _NOW, chunk_size=2
    )
    assert report.delivered == 0 and report.failed == 0
    assert len(index.bulk) == 1 and len(index.single) == 2


def test_a_poison_fact_fails_alone_under_fallback(tmp_path: Path) -> None:
    """Bulk is always broken (``BulkBrokenIndex``), so the chunk demotes to
    the per-fact path; one fact's per-fact call also raises. The other two
    facts still deliver, and the poison row is left retryable with its
    attempt recorded — the same shape as
    ``test_a_raising_index_records_a_retryable_attempt_and_retains_the_row``."""
    _seed_catalogue(tmp_path)
    fact_ids = _ingest(tmp_path, ("fact one", "fact two", "fact three"), 1)
    poison = fact_ids[1]

    class PoisonIndex(BulkBrokenIndex):
        def project(
            self, state: ProjectedFactState
        ) -> FactProjected | ProjectionFailed:
            self.single.append(state)
            if state.fact_id == poison:
                raise RuntimeError("poison fact")
            return FactProjected()

    index = PoisonIndex()

    report = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: _NOW, chunk_size=3
    )

    assert report.delivered == 2
    assert report.failed == 1
    (row,) = _outbox_rows(tmp_path)
    assert row[3] == str(poison)
    assert row[4] == 1
