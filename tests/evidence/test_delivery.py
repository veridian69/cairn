import hashlib
import itertools
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority, IngestAssertion
from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import CatalogueTransactions, Committed
from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.evidence.attic import SqliteAttic
from cairn.evidence.delivery import DeliveryReport, deliver_evidence_outbox
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import LogEvent, configure_logging
from cairn.screening import SecretScreen

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
_REALM = "acme"
_JOB = ScopeSegment(kind="job", identifier="job-1")
_SCOPE = Scope(_REALM, (_JOB,))
_AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_AGENT_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_INGEST_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")
# Both pass migration 0003's shape CHECK (length 36, GLOB-legal, charset
# restricted to hex digits and dashes) but fail UUID() construction: the
# CHECK's `?` wildcards accept a dash in a position UUID() requires to be
# hex. See the corrected column enumeration in the task-10 report follow-up.
_HOSTILE_WORK_ID = "0000000--0000-4000-8000-000000000000"
_HOSTILE_EVIDENCE_ID = "--------------4----8----------------"
_ALL_CLASSIFICATIONS = frozenset(Classification)


class _RaisingAttic:
    """A test-local hostile adapter (I-69): raises on every ``store``,
    per P-14's "adapter infrastructure failures raise"."""

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise RuntimeError("attic unreachable")

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _ReturnsGarbageAttic:
    """A hostile adapter that breaks the ``AtticAdapter`` Protocol's
    contract at runtime — mypy bounds the one in-repo implementation, but
    the Protocol itself enforces nothing at runtime, and I-69 already
    declares every adapter untrusted."""

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        return None  # type: ignore[return-value]

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _AlwaysCorruptAttic:
    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        return PayloadCorrupt()

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _RecordingAttic:
    """Delegates to a real ``SqliteAttic`` but records the payloads it was
    asked to store, in call order."""

    def __init__(self, data_path: Path) -> None:
        self._real = SqliteAttic(data_path)
        self.stored_payloads: list[bytes] = []

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        self.stored_payloads.append(payload)
        return self._real.store(evidence_id, payload)

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        return self._real.fetch(evidence_id)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _BlockingAttic:
    """Blocks the first ``store`` call until released, recording entry and
    exit order — used to prove the delivery gate is held for the whole run."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        self.order.append("store-start")
        self.entered.set()
        assert self.release.wait(timeout=5), "release was never signalled"
        self.order.append("store-end")
        return PayloadStored()

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _RaceSimulatingAttic:
    """Between the caller's batch-read and its confirming write, this
    simulates a second deliverer already having won the row: it stores
    successfully but also bumps the row's retry state directly, as if
    another process's failure-confirm had just landed."""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        _simulate_lost_race(self._data_path, evidence_id)
        return PayloadStored()

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _RaceSimulatingCorruptAttic:
    """The same race as ``_RaceSimulatingAttic``, but on the ``store``-
    returns-``PayloadCorrupt`` branch: delivery routes both branches through
    ``_confirm_failure``, but each is a textually distinct call site, so
    proving the lost-race skip on the raise branch doesn't also prove it
    here."""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        _simulate_lost_race(self._data_path, evidence_id)
        return PayloadCorrupt()

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _RaceSimulatingRaisingAttic:
    """The same race again, but on the ``store``-raises branch — the third
    and last of the three confirming-write call sites, each of which needs
    its own proof that a lost race is skipped rather than mis-counted."""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        _simulate_lost_race(self._data_path, evidence_id)
        raise RuntimeError("attic unreachable")

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


def _simulate_lost_race(data_path: Path, evidence_id: UUID) -> None:
    """Between our own store() and our confirming write, bump the row's
    retry state directly, as if another deliverer's confirm had just
    landed — models the P-15 defence-in-depth scenario regardless of which
    of the two failure branches (or the success branch) is calling it."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE evidence_outbox SET attempts = attempts + 1, "
            "last_attempt_at = ?, last_failure_code = 'attic_unavailable' "
            "WHERE evidence_id = ?",
            (_canonical_ts(_NOW), str(evidence_id)),
        )
        connection.commit()


def _canonical_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    """A valid v4-shaped idempotency key for test use."""
    return UUID(f"{n:08x}-0000-4000-8000-000000000000")


def _add_realm(data_path: Path) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (_REALM, _canonical_ts(_NOW)),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (_REALM, bytes(32)),
        )
        connection.commit()


def _seed_catalogue(data_path: Path) -> None:
    migrate_catalogue(_config(data_path), lambda: _NOW)
    _add_realm(data_path)


def _json_column(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _insert_evidence_record_raw(
    data_path: Path, evidence_id_raw: str, *, payload_digest: bytes
) -> None:
    """Inserts a catalogue evidence record whose evidence_id is itself the
    hostile string, purely so a hostile evidence_outbox row can satisfy the
    FK to it — external-custody form, no assertion needed."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
            "classification, payload_digest, assertion_id, payload_length, "
            "external_uri, recorded_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                evidence_id_raw,
                _REALM,
                _json_column([{"id": _JOB.identifier, "kind": _JOB.kind}]),
                Classification.INTERNAL.value,
                payload_digest,
                "https://example.test/evidence/hostile",
                _canonical_ts(_NOW),
            ),
        )
        connection.commit()


def _insert_outbox_row_raw(
    data_path: Path,
    *,
    work_id_raw: str,
    evidence_id_raw: str,
    mutation_id: UUID,
    payload: bytes,
    created_at: datetime = _NOW,
    created_at_raw: str | None = None,
) -> None:
    """Inserts an evidence_outbox row directly, bypassing ingest entirely,
    so the row's identity columns can be set to values the schema's shape
    CHECK accepts but UUID() cannot parse. ``created_at_raw``, when given,
    bypasses canonicalisation of ``created_at`` entirely — for a row whose
    created_at satisfies the shape CHECK but not parse_timestamp, the same
    kind of gap as the identity columns above but on the column the gauge
    wiring reads."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence_outbox (work_id, kind, evidence_id, mutation_id, "
            "payload, created_at, attempts) "
            "VALUES (?, 'store-payload', ?, ?, ?, ?, 0)",
            (
                work_id_raw,
                evidence_id_raw,
                str(mutation_id),
                payload,
                created_at_raw
                if created_at_raw is not None
                else _canonical_ts(created_at),
            ),
        )
        connection.commit()


def _insert_outbox_row_bypassing_check(
    data_path: Path,
    *,
    work_id_raw: str,
    evidence_id_raw: str,
    mutation_id: UUID,
    payload: bytes,
    created_at: datetime = _NOW,
) -> None:
    """Like ``_insert_outbox_row_raw``, but also disables CHECK enforcement
    for the insert — models a row whose work_id doesn't even match the
    shape the schema is supposed to guarantee, as a catalogue restored from
    a pre-STRICT schema might carry. STRICT typing itself still applies
    (the pragma only lifts CHECK, not column type affinity)."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("PRAGMA ignore_check_constraints = 1")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence_outbox (work_id, kind, evidence_id, mutation_id, "
            "payload, created_at, attempts) "
            "VALUES (?, 'store-payload', ?, ?, ?, ?, 0)",
            (
                work_id_raw,
                evidence_id_raw,
                str(mutation_id),
                payload,
                _canonical_ts(created_at),
            ),
        )
        connection.commit()


def _seed_ingester(data_path: Path) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                str(_AGENT_ID),
                PrincipalKind.WORKLOAD.value,
                "agent",
                _canonical_ts(_NOW),
            ),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(_AGENT_CREDENTIAL_ID), str(_AGENT_ID), bytes(32), _canonical_ts(_NOW)),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(_INGEST_GRANT_ID),
                str(_AGENT_ID),
                _REALM,
                _json_column([{"id": _JOB.identifier, "kind": _JOB.kind}]),
                _json_column([GrantOperation.INGEST.value]),
                Classification.RESTRICTED.value,
                _json_column(sorted(c.value for c in _ALL_CLASSIFICATIONS)),
                "2027-01-01T00:00:00.000000Z",
                _canonical_ts(_NOW),
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
    idempotency_seed: int = 0x30000000,
) -> CairnAuthority:
    # The mutation/event uuid_factory and the idempotency-key seed must both
    # vary together, or a second CairnAuthority built for a second ingest in
    # the same test would restart its audit event_id sequence from the same
    # point as the first and collide.
    return CairnAuthority(
        data_path,
        _transactions(data_path, now=now, uuid_seed=idempotency_seed),
        clock=lambda: now,
        uuid_factory=_uuid_seq(idempotency_seed),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )


def _ingest_evidence(
    data_path: Path,
    payload: bytes,
    n: int,
    *,
    now: datetime = _NOW,
) -> UUID:
    """Ingests one assertion carrying an exact-evidence payload through the
    real command, exactly as Task 7 built it, and returns the evidence_id.

    ``n`` distinguishes repeated calls within one test: each call constructs
    a fresh ``CairnAuthority`` (a fresh uuid_factory and idempotency key), so
    without a distinct seed per call, a second ingest in the same test would
    mint a colliding assertion_id and its idempotency key would collide too.
    """
    authority = _authority(data_path, now=now, idempotency_seed=0x30000000 + n * 0x1000)
    outcome = authority.ingest(
        Actor(principal_id=_AGENT_ID, credential_id=_AGENT_CREDENTIAL_ID),
        IngestAssertion(
            scope=_SCOPE,
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=(
                FactDraft(body="the build is green", valid_from=None, valid_to=None),
            ),
            requested_trust=TrustClass.CANDIDATE,
            evidence_payload=payload,
        ),
        idempotency_key=_idem(n),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id is not None
    return outcome.value.evidence_id


def _outbox_rows(data_path: Path) -> list[tuple[object, ...]]:
    with _open_write_connection(data_path, create=False) as connection:
        return connection.execute(
            "SELECT work_id, evidence_id, payload, attempts, last_attempt_at, "
            "last_failure_code FROM evidence_outbox ORDER BY created_at"
        ).fetchall()


def _audit_event_count(data_path: Path) -> int:
    with _open_write_connection(data_path, create=False) as connection:
        row = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()
        return cast(int, row[0])


def test_delivery_of_empty_outbox_returns_zero_report(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _RaisingAttic(), clock=lambda: _NOW, metrics=Metrics()
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=0)


def test_pending_row_is_delivered_with_exact_bytes_and_confirmed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    evidence_id = _ingest_evidence(tmp_path, b"exact evidence payload", 1)
    transactions = _transactions(tmp_path)
    attic = _RecordingAttic(tmp_path)

    report = deliver_evidence_outbox(transactions, attic, clock=lambda: _NOW)

    assert report == DeliveryReport(delivered=1, failed=0, remaining=0)
    assert attic.stored_payloads == [b"exact evidence payload"]
    assert _outbox_rows(tmp_path) == []
    fetched = SqliteAttic(tmp_path).fetch(evidence_id)
    assert fetched == FetchedPayload(payload=b"exact evidence payload")


def test_store_raising_marks_attic_unavailable_and_retains_row(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(transactions, _RaisingAttic(), clock=lambda: _NOW)

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"payload one"
    assert attempts == 1
    assert last_attempt_at == _canonical_ts(_NOW)
    assert last_failure_code == "attic_unavailable"


def test_payload_corrupt_marks_attic_corruption_and_retains_row(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _AlwaysCorruptAttic(), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"payload one"
    assert attempts == 1
    assert last_attempt_at == _canonical_ts(_NOW)
    assert last_failure_code == "attic_corruption"


def test_store_returning_an_unrecognised_result_is_treated_as_unavailable_not_delivered(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"irreplaceable payload", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _ReturnsGarbageAttic(), clock=lambda: _NOW
    )

    # Fail-closed: only an explicit PayloadStored counts as delivered.
    # Anything else — including a value the Protocol doesn't actually
    # enforce at runtime — must not delete the row, since that row is the
    # only remaining copy of the payload bytes.
    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"irreplaceable payload"
    assert attempts == 1
    assert last_attempt_at == _canonical_ts(_NOW)
    assert last_failure_code == "attic_unavailable"


def test_pending_work_survives_restart_and_completes_on_second_run(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"durable payload", 1)

    first_run_transactions = _transactions(tmp_path)
    first_report = deliver_evidence_outbox(
        first_run_transactions, _RaisingAttic(), clock=lambda: _NOW
    )
    assert first_report == DeliveryReport(delivered=0, failed=1, remaining=1)

    # A fresh CatalogueTransactions over the same data directory, as a
    # restarted process would construct — not a same-connection read.
    second_run_transactions = _transactions(tmp_path)
    second_report = deliver_evidence_outbox(
        second_run_transactions, _RecordingAttic(tmp_path), clock=lambda: _NOW
    )

    assert second_report == DeliveryReport(delivered=1, failed=0, remaining=0)
    assert _outbox_rows(tmp_path) == []


def test_crash_between_store_and_confirmation_recovers_via_idempotent_restore(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    evidence_id = _ingest_evidence(tmp_path, b"crash-recovery payload", 1)

    # Simulate a crash between a successful store() and its confirming
    # delete: the payload reaches Attic, but the outbox row is never
    # touched, exactly as if the process died right there.
    real_attic = SqliteAttic(tmp_path)
    assert real_attic.store(evidence_id, b"crash-recovery payload") == PayloadStored()
    assert len(_outbox_rows(tmp_path)) == 1

    transactions = _transactions(tmp_path)
    report = deliver_evidence_outbox(transactions, real_attic, clock=lambda: _NOW)

    assert report == DeliveryReport(delivered=1, failed=0, remaining=0)
    assert _outbox_rows(tmp_path) == []


def test_rows_deliver_oldest_first_and_limit_bounds_one_run(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"first", 1, now=_NOW)
    _ingest_evidence(tmp_path, b"second", 2, now=_NOW + timedelta(seconds=1))
    _ingest_evidence(tmp_path, b"third", 3, now=_NOW + timedelta(seconds=2))
    transactions = _transactions(tmp_path)
    attic = _RecordingAttic(tmp_path)

    report = deliver_evidence_outbox(transactions, attic, clock=lambda: _NOW, limit=2)

    assert report == DeliveryReport(delivered=2, failed=0, remaining=1)
    assert attic.stored_payloads == [b"first", b"second"]
    remaining_rows = _outbox_rows(tmp_path)
    assert len(remaining_rows) == 1
    assert remaining_rows[0][2] == b"third"


def test_no_audit_event_is_appended_by_delivery(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1)
    transactions = _transactions(tmp_path)
    before = _audit_event_count(tmp_path)

    deliver_evidence_outbox(transactions, _RecordingAttic(tmp_path), clock=lambda: _NOW)

    assert _audit_event_count(tmp_path) == before


def test_conditional_write_defence_skips_a_row_another_deliverer_won(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"contested payload", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _RaceSimulatingAttic(tmp_path), clock=lambda: _NOW
    )

    # Our own store() succeeded, but by the time we tried to confirm, the
    # observed (work_id, attempts) pair had moved under us: skipped, not
    # counted as delivered and not counted as failed.
    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"contested payload"
    assert attempts == 1
    assert last_failure_code == "attic_unavailable"


def test_conditional_write_defence_also_skips_on_the_payload_corrupt_path(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"contested payload", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _RaceSimulatingCorruptAttic(tmp_path), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"contested payload"
    assert attempts == 1
    assert last_failure_code == "attic_unavailable"


def test_conditional_write_defence_also_skips_on_the_raise_path(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"contested payload", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _RaceSimulatingRaisingAttic(tmp_path), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    _work_id, _evidence_id, payload, attempts, last_attempt_at, last_failure_code = (
        rows[0]
    )
    assert payload == b"contested payload"
    assert attempts == 1
    assert last_failure_code == "attic_unavailable"


def test_second_delivery_run_blocks_on_the_delivery_gate_until_the_first_finishes(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"gated payload", 1)
    transactions = _transactions(tmp_path)
    attic = _BlockingAttic()

    first_reports: list[DeliveryReport] = []
    second_reports: list[DeliveryReport] = []

    first_thread = threading.Thread(
        target=lambda: first_reports.append(
            deliver_evidence_outbox(transactions, attic, clock=lambda: _NOW)
        )
    )
    first_thread.start()
    assert attic.entered.wait(timeout=5), "first run never entered store()"

    second_thread = threading.Thread(
        target=lambda: second_reports.append(
            deliver_evidence_outbox(transactions, attic, clock=lambda: _NOW)
        )
    )
    second_thread.start()
    time.sleep(0.2)
    # The second run must not have made any progress at all while the first
    # is still inside its store() call: non-reentrant means blocked, not
    # interleaved.
    assert attic.order == ["store-start"]

    attic.release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert attic.order == ["store-start", "store-end"]
    assert first_reports == [DeliveryReport(delivered=1, failed=0, remaining=0)]
    # By the time the second run got the gate, the row was already
    # delivered and confirmed by the first run.
    assert second_reports == [DeliveryReport(delivered=0, failed=0, remaining=0)]


def test_delivery_run_emits_one_safe_summary_log_event(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"logged payload", 1)
    transactions = _transactions(tmp_path)
    stream = StringIO()
    logger = configure_logging(stream)

    deliver_evidence_outbox(
        transactions, _RecordingAttic(tmp_path), clock=lambda: _NOW, logger=logger
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == LogEvent.EVIDENCE_DELIVERY_COMPLETED.value
    assert payload["delivered"] == 1
    assert payload["failed"] == 0
    assert payload["remaining"] == 0
    assert "logged payload" not in stream.getvalue()


def test_gauge_reads_zero_after_delivery_empties_the_queue(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1)
    transactions = _transactions(tmp_path)
    metrics = Metrics()

    deliver_evidence_outbox(
        transactions, _RecordingAttic(tmp_path), clock=lambda: _NOW, metrics=metrics
    )

    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    # An explicitly zeroed series must still render — see the equivalent
    # assertion in test_rest_foundation.py for why absence and a set zero
    # are not the same thing.
    assert 'cairn_outbox_depth{queue="evidence"} 0.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 0.0' in text


def test_gauge_reflects_a_row_retained_by_a_failed_delivery(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1, now=_NOW)
    transactions = _transactions(tmp_path)
    metrics = Metrics()
    sampled_at = _NOW + timedelta(seconds=45)

    report = deliver_evidence_outbox(
        transactions, _RaisingAttic(), clock=lambda: sampled_at, metrics=metrics
    )

    # The row failed and was fail-closed rather than deleted, so it must
    # still count toward depth and still contribute its (unchanged)
    # created_at to the age — a gauge that dropped a retained failure would
    # under-report exactly the backlog an operator most needs to see.
    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    assert 'cairn_outbox_depth{queue="evidence"} 1.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 45.0' in text


def test_gauge_age_tracks_the_oldest_retained_row_not_the_newest(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"older", 1, now=_NOW)
    _ingest_evidence(tmp_path, b"newer", 2, now=_NOW + timedelta(seconds=10))
    transactions = _transactions(tmp_path)
    metrics = Metrics()
    sampled_at = _NOW + timedelta(seconds=30)

    report = deliver_evidence_outbox(
        transactions, _RaisingAttic(), clock=lambda: sampled_at, metrics=metrics
    )

    assert report == DeliveryReport(delivered=0, failed=2, remaining=2)
    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    assert 'cairn_outbox_depth{queue="evidence"} 2.0' in text
    # 30s since the older row, not 20s since the newer one — proves the
    # gauge tracks MIN(created_at), not an arbitrary or newest row.
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 30.0' in text


def test_a_future_dated_row_reports_zero_age_and_still_returns_a_report(
    tmp_path: Path,
) -> None:
    """A clock that has stepped backwards since the row was written makes the
    age negative, and ``set_outbox_state`` refuses a negative age with a
    ``TypeError``.

    Unclamped, that ``TypeError`` escapes here — after the run's deletes and
    retry updates have already committed — costing the caller a report about
    durable work that really happened, over a gauge. No corruption is needed
    to reach it: ``created_at`` is written by whichever process ingested the
    row, and clocks step. Zero is the honest reading of "nothing has been
    waiting", and unlike an unreadable ``created_at`` it is a value, not a
    refusal, so no age-unreadable event is emitted.
    """
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1, now=_NOW)
    transactions = _transactions(tmp_path)
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)
    sampled_at = _NOW - timedelta(seconds=60)

    report = deliver_evidence_outbox(
        transactions,
        _RaisingAttic(),
        clock=lambda: sampled_at,
        metrics=metrics,
        logger=logger,
    )

    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    assert 'cairn_outbox_depth{queue="evidence"} 1.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 0.0' in text
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert not [
        e for e in events if e["event"] == LogEvent.EVIDENCE_OUTBOX_AGE_UNREADABLE.value
    ]


def test_unreadable_oldest_created_at_degrades_gracefully(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    good_evidence_id = _ingest_evidence(tmp_path, b"good payload", 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM evidence_outbox WHERE evidence_id = ?",
            (str(good_evidence_id),),
        )
        connection.commit()
    # Schema-legal (passes ck_evidence_outbox_created_at's shape CHECK,
    # confirmed against a real SQLite connection — see the task-11 report)
    # but not a real calendar timestamp, so parse_timestamp rejects it.
    _insert_outbox_row_raw(
        tmp_path,
        work_id_raw=str(uuid4()),
        evidence_id_raw=str(good_evidence_id),
        mutation_id=UUID("99999999-4444-4000-8000-000000000000"),
        payload=b"good payload",
        created_at_raw="9999-99-99T99:99:99.999999Z",
    )
    transactions = _transactions(tmp_path)
    metrics = Metrics()
    # A prior known-good sample, standing in for the last successful
    # startup/delivery sample — must survive this run untouched, not be
    # overwritten with a synthetic (and misleading) zero.
    metrics.set_outbox_state(OutboxQueue.EVIDENCE, depth=99, oldest_age_seconds=12345.0)
    stream = StringIO()
    logger = configure_logging(stream)

    report = deliver_evidence_outbox(
        transactions,
        _RaisingAttic(),
        clock=lambda: _NOW,
        metrics=metrics,
        logger=logger,
    )

    # The run's own summary must survive corruption in the read the gauge
    # alone depends on — losing it would be worse than "one bad row must
    # not stop the run" (Task 10): it would discard a run's outcome over
    # corruption that cannot affect any delivery decision already made.
    assert report == DeliveryReport(delivered=0, failed=1, remaining=1)
    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    assert 'cairn_outbox_depth{queue="evidence"} 99.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 12345.0' in text

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    age_unreadable = [
        line
        for line in lines
        if line["event"] == LogEvent.EVIDENCE_OUTBOX_AGE_UNREADABLE.value
    ]
    assert len(age_unreadable) == 1
    completed = [
        line
        for line in lines
        if line["event"] == LogEvent.EVIDENCE_DELIVERY_COMPLETED.value
    ]
    assert completed == [
        {
            "event": LogEvent.EVIDENCE_DELIVERY_COMPLETED.value,
            "time": completed[0]["time"],
            "delivered": 0,
            "failed": 1,
            "remaining": 1,
        }
    ]


def test_no_metrics_seam_leaves_delivery_unaffected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _ingest_evidence(tmp_path, b"payload one", 1)
    transactions = _transactions(tmp_path)

    report = deliver_evidence_outbox(
        transactions, _RecordingAttic(tmp_path), clock=lambda: _NOW
    )

    assert report == DeliveryReport(delivered=1, failed=0, remaining=0)


def test_hostile_work_id_row_is_skipped_logged_and_does_not_stop_the_run(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    good_evidence_id = _ingest_evidence(tmp_path, b"good payload", 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM evidence_outbox WHERE evidence_id = ?",
            (str(good_evidence_id),),
        )
        connection.commit()
    _insert_outbox_row_raw(
        tmp_path,
        work_id_raw=_HOSTILE_WORK_ID,
        evidence_id_raw=str(good_evidence_id),
        mutation_id=UUID("99999999-0000-4000-8000-000000000000"),
        payload=b"good payload",
        created_at=_NOW,
    )
    # A second, entirely normal row, ordered after the hostile one — proves
    # the hostile row doesn't stop the run from reaching it.
    _ingest_evidence(tmp_path, b"second payload", 2, now=_NOW + timedelta(seconds=1))
    transactions = _transactions(tmp_path)
    attic = _RecordingAttic(tmp_path)
    stream = StringIO()
    logger = configure_logging(stream)

    report = deliver_evidence_outbox(
        transactions, attic, clock=lambda: _NOW, logger=logger
    )

    # The hostile row is neither delivered nor failed — it never reaches
    # attic.store() at all, since there is no trustworthy identity to pass
    # it. It stays in the outbox (remaining), and the good row after it
    # still delivers normally.
    assert report == DeliveryReport(delivered=1, failed=0, remaining=1)
    assert attic.stored_payloads == [b"second payload"]
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][0] == _HOSTILE_WORK_ID
    assert rows[0][3] == 0  # attempts: attic was never contacted for it
    assert rows[0][5] is None  # last_failure_code: untouched

    lines = stream.getvalue().splitlines()
    events = [json.loads(line) for line in lines]
    unreadable = [
        e for e in events if e["event"] == LogEvent.EVIDENCE_OUTBOX_ROW_UNREADABLE.value
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["work_id"] == _HOSTILE_WORK_ID
    assert "good payload" not in stream.getvalue()
    completed = [
        e for e in events if e["event"] == LogEvent.EVIDENCE_DELIVERY_COMPLETED.value
    ]
    assert completed == [
        {
            "event": LogEvent.EVIDENCE_DELIVERY_COMPLETED.value,
            "time": completed[0]["time"],
            "delivered": 1,
            "failed": 0,
            "remaining": 1,
        }
    ]


def test_hostile_evidence_id_row_is_also_skipped_and_logged(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record_raw(
        tmp_path,
        _HOSTILE_EVIDENCE_ID,
        payload_digest=hashlib.sha256(b"irrelevant").digest(),
    )
    work_id = UUID("99999999-1111-4000-8000-000000000000")
    _insert_outbox_row_raw(
        tmp_path,
        work_id_raw=str(work_id),
        evidence_id_raw=_HOSTILE_EVIDENCE_ID,
        mutation_id=UUID("99999999-2222-4000-8000-000000000000"),
        payload=b"irrelevant",
    )
    transactions = _transactions(tmp_path)
    stream = StringIO()
    logger = configure_logging(stream)

    report = deliver_evidence_outbox(
        transactions, _RaisingAttic(), clock=lambda: _NOW, logger=logger
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    rows = _outbox_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][3] == 0  # attempts untouched — never reached attic.store()
    lines = stream.getvalue().splitlines()
    unreadable = [
        json.loads(line)
        for line in lines
        if json.loads(line)["event"] == LogEvent.EVIDENCE_OUTBOX_ROW_UNREADABLE.value
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["work_id"] == str(work_id)


def test_pathological_work_id_is_omitted_from_the_log_rather_than_crashing(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    good_evidence_id = _ingest_evidence(tmp_path, b"good payload", 1)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM evidence_outbox WHERE evidence_id = ?",
            (str(good_evidence_id),),
        )
        connection.commit()
    # Not just non-UUID (that's the case above) — this doesn't even match
    # the schema's own shape CHECK, simulating a row that predates STRICT
    # or the CHECK itself. SafeLogger.emit's work_id validator would raise
    # TypeError on this exact value if handed it directly, which is the
    # failure mode this guard exists to prevent.
    _insert_outbox_row_bypassing_check(
        tmp_path,
        work_id_raw="not-a-uuid-at-all",
        evidence_id_raw=str(good_evidence_id),
        mutation_id=UUID("99999999-3333-4000-8000-000000000000"),
        payload=b"good payload",
    )
    transactions = _transactions(tmp_path)
    stream = StringIO()
    logger = configure_logging(stream)

    # Must not raise.
    report = deliver_evidence_outbox(
        transactions, _RaisingAttic(), clock=lambda: _NOW, logger=logger
    )

    assert report == DeliveryReport(delivered=0, failed=0, remaining=1)
    lines = stream.getvalue().splitlines()
    events = [json.loads(line) for line in lines]
    unreadable = [
        e for e in events if e["event"] == LogEvent.EVIDENCE_OUTBOX_ROW_UNREADABLE.value
    ]
    assert len(unreadable) == 1
    # The raw value isn't safe to log verbatim — it doesn't match the
    # bounded shape the CHECK is supposed to guarantee — so it's omitted
    # rather than trusting arbitrary content from a corrupted row.
    assert "work_id" not in unreadable[0]
    assert "not-a-uuid-at-all" not in stream.getvalue()
