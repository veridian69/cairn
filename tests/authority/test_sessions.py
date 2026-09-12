"""Session operations exercise real SQLite custody boundaries, never model calls."""

import importlib
import json
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import pytest
import test_retrieval as f
from test_ingest_composed_guard import _revoke, _seed

from cairn.authority.credentials import GrantOperation
from cairn.authority.mutations import CairnAuthority, validate_ingest_payload
from cairn.authority.session_codec import future_ingest
from cairn.authority.sessions import CairnSessions
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    Rejected,
    Replayed,
)
from cairn.screening import SecretScreen


def test_session_authority_exists() -> None:
    assert importlib.util.find_spec("cairn.authority.sessions") is not None, (
        "session authority is not implemented"
    )


def _service(
    path: Path,
    clock: Callable[[], datetime] = lambda: f._NOW,
    boundary: Callable[[], None] = lambda: None,
) -> CairnSessions:
    # Lazy import makes the first red an assertion about the absent feature.
    from cairn.authority.sessions import CairnSessions

    class Transactions(CatalogueTransactions):
        def mutate_idempotent(self, *args: Any, **kwargs: Any) -> Any:
            boundary()
            return super().mutate_idempotent(*args, **kwargs)

    tx = Transactions(
        path, writer_gate=threading.Lock(), clock=clock, uuid_factory=uuid4
    )
    screen = SecretScreen()
    authority = CairnAuthority(path, tx, clock, uuid4, True, screen)
    return CairnSessions(path, tx, clock, uuid4, screen, authority)


def _call(
    service: CairnSessions, method: str, command: Any, key: UUID | None = None
) -> Any:
    return getattr(service, method)(
        f._agent_actor(),
        command,
        idempotency_key=key or uuid4(),
        correlation_id=f._CORRELATION_ID,
    )


@pytest.fixture
def api() -> ModuleType:
    assert importlib.util.find_spec("cairn.authority.sessions") is not None, (
        "session authority is not implemented"
    )
    return importlib.import_module("cairn.authority.session_types")


def _start(path: Path, api: ModuleType) -> tuple[CairnSessions, UUID, UUID, UUID]:
    _seed(path)
    service = _service(path)
    sid, tid, attempt = uuid4(), uuid4(), uuid4()
    assert isinstance(
        _call(service, "open", api.OpenSession(f._SCOPE, sid, Classification.INTERNAL)),
        Committed,
    )
    assert isinstance(
        _call(service, "begin", api.BeginTurn(f._SCOPE, sid, tid, attempt)), Committed
    )
    return service, sid, tid, attempt


def test_preparation_survives_restart_and_has_no_fact_custody(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    command = api.PrepareTurn(
        f._SCOPE,
        sid,
        tid,
        attempt,
        "completed response",
        (api.DurableObservation("synthetic durable fact"),),
    )
    key = uuid4()
    prepared = _call(service, "prepare", command, key)
    assert isinstance(prepared, Committed)
    assert prepared.value.state == "prepared"
    assert prepared.value.custody_receipt is None
    assert prepared.value.operational_receipt == prepared.mutation_receipt
    reopened = _service(tmp_path).read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )
    assert not isinstance(reopened, Rejected)
    assert reopened == prepared.value
    assert reopened.response == "completed response"
    assert reopened.observations == command.observations
    assert reopened.turn_count == 1 and reopened.prepared_bytes > 0
    replay = _call(service, "prepare", command, key)
    assert isinstance(replay, Replayed) and replay.value == prepared.value
    for table in ("assertions", "facts", "evidence_records", "projection_outbox"):
        assert f._rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_begin_replay_and_contender_never_permit_generation(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, _, _ = _start(tmp_path, api)
    command = api.BeginTurn(f._SCOPE, sid, uuid4(), uuid4())
    key = uuid4()
    first = _call(service, "begin", command, key)
    replay = _call(service, "begin", command, key)
    assert isinstance(first, Committed) and isinstance(replay, Replayed)
    assert isinstance(_call(service, "begin", command), Rejected)
    assert isinstance(
        _call(service, "begin", replace(command, attempt_id=uuid4())), Rejected
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_turns") == [(2,)]


def test_immutable_preparation_and_attempt_fence(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    command = api.PrepareTurn(f._SCOPE, sid, tid, attempt, "", ())
    assert isinstance(
        _call(service, "prepare", replace(command, attempt_id=uuid4())), Rejected
    )
    key = uuid4()
    accepted = _call(service, "prepare", command, key)
    assert isinstance(accepted, Committed) and accepted.value.state == "prepared"
    assert accepted.value.response == "" and accepted.value.custody_receipt is None
    for retry_key in (key, uuid4()):
        denied = _call(
            service, "prepare", replace(command, response="changed"), retry_key
        )
        assert isinstance(denied, Rejected)
        assert denied.failure.code == "idempotency_conflict"
    assert isinstance(
        _call(service, "abandon", api.AbandonTurn(f._SCOPE, sid, tid, "cancelled")),
        Rejected,
    )


@pytest.mark.parametrize(
    "case",
    [
        "response",
        "body",
        "count",
        "envelope",
        "secret_response",
        "secret_body",
        "times",
        "naive",
        "surrogate",
        "validity",
        "mutable",
    ],
)
def test_preparation_rejects_invalid_or_secret_output_without_storage(
    tmp_path: Path, api: ModuleType, case: str
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    response = "ok"
    observations: Any = (api.DurableObservation("safe"),)
    if case == "response":
        response = "é" * 16385
    if case == "body":
        observations = (api.DurableObservation("é" * 2049),)
    if case == "count":
        observations = (api.DurableObservation("safe"),) * 9
    if case == "envelope":
        response, observations = "\x01" * 16000, ()
    if case == "secret_response":
        response = "-----BEGIN RSA PRIVATE KEY-----"
    if case == "secret_body":
        observations = (api.DurableObservation("-----BEGIN RSA PRIVATE KEY-----"),)
    if case == "times":
        observations = (
            api.DurableObservation("one", observed_at=f._NOW),
            api.DurableObservation("two"),
        )
    if case == "naive":
        observations = (
            api.DurableObservation("one", observed_at=datetime(2026, 1, 1)),
        )
    if case == "surrogate":
        response = "\ud800"
    if case == "validity":
        observations = (
            api.DurableObservation(
                "one", valid_from=f._NOW, valid_to=f._NOW - timedelta(days=1)
            ),
        )
    if case == "mutable":
        observations = [{"body": "safe"}]
    result = _call(
        service,
        "prepare",
        api.PrepareTurn(f._SCOPE, sid, tid, attempt, response, observations),
    )
    assert isinstance(result, Rejected)
    assert result.failure.code == (
        "secret_rejected" if case.startswith("secret") else "invalid_request"
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (0,)
    ]


def test_abandonment_fences_late_prepare_and_unique_replacement(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    replacement = api.BeginTurn(f._SCOPE, sid, uuid4(), uuid4(), tid)
    assert isinstance(_call(service, "begin", replacement), Rejected)
    abandoned = _call(
        service, "abandon", api.AbandonTurn(f._SCOPE, sid, tid, "interrupted")
    )
    assert isinstance(abandoned, Committed) and abandoned.value.state == "abandoned"
    assert isinstance(
        _call(
            service, "prepare", api.PrepareTurn(f._SCOPE, sid, tid, attempt, "late", ())
        ),
        Rejected,
    )
    assert isinstance(_call(service, "begin", replacement), Committed)
    assert isinstance(
        _call(
            service, "begin", replace(replacement, turn_id=uuid4(), attempt_id=uuid4())
        ),
        Rejected,
    )
    assert isinstance(
        _call(service, "begin", replace(replacement, attempt_id=attempt)), Rejected
    )


def test_owner_and_exact_scope_denials_are_opaque(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    f._seed_outsider(tmp_path)
    f._insert_grant(tmp_path, grant_id=uuid4(), principal_id=f._OUTSIDER_ID)
    commands = [
        api.ReadSession(f._SCOPE, sid, tid),
        api.ReadSession(f._SCOPE, uuid4()),
        api.ReadSession(Scope(f._REALM, (f._SIBLING_JOB,)), sid),
    ]
    for actor in (f._agent_actor(), f._outsider_actor()):
        for command in commands[1:] if actor == f._agent_actor() else commands:
            result = service.read(actor, command, correlation_id=f._CORRELATION_ID)
            assert (
                isinstance(result, Rejected)
                and result.failure.code == "authorisation_denied"
            )
    denied = service.open(
        f._outsider_actor(),
        api.OpenSession(f._SCOPE, sid, Classification.INTERNAL),
        idempotency_key=uuid4(),
        correlation_id=f._CORRELATION_ID,
    )
    assert (
        isinstance(denied, Rejected) and denied.failure.code == "authorisation_denied"
    )


@pytest.mark.parametrize("grant", [f._DATA_GRANT_ID, f._SECOND_GRANT_ID])
@pytest.mark.parametrize("replay", [False, True])
def test_grants_rechecked_at_writer_boundary_including_replay(
    tmp_path: Path, api: ModuleType, grant: UUID, replay: bool
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    command = api.PrepareTurn(f._SCOPE, sid, tid, attempt, "ready", ())
    key = uuid4()
    if replay:
        assert isinstance(_call(service, "prepare", command, key), Committed)
    racing = _service(tmp_path, boundary=lambda: _revoke(tmp_path, grant))
    result = _call(racing, "prepare", command, key)
    assert (
        isinstance(result, Rejected) and result.failure.code == "authorisation_denied"
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (int(replay),)
    ]


def test_read_needs_retrieve_only_and_expiry_denies_recovery(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    _revoke(tmp_path, f._DATA_GRANT_ID)
    assert not isinstance(
        service.read(
            f._agent_actor(),
            api.ReadSession(f._SCOPE, sid, tid),
            correlation_id=f._CORRELATION_ID,
        ),
        Rejected,
    )
    expired = _service(tmp_path, lambda: f._NOW + timedelta(minutes=2))
    assert isinstance(
        expired.read(
            f._agent_actor(),
            api.ReadSession(f._SCOPE, sid, tid),
            correlation_id=f._CORRELATION_ID,
        ),
        Rejected,
    )


def test_visit_acknowledgement_is_explicit_and_monotonic(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    visit1 = _call(service, "issue_visit", api.IssueVisit(f._SCOPE, sid))
    visit2 = _call(service, "issue_visit", api.IssueVisit(f._SCOPE, sid))
    assert isinstance(visit1, Committed) and isinstance(visit2, Committed)
    assert visit2.value.visit_watermark > visit1.value.visit_watermark
    assert visit2.value.visit_at >= visit1.value.visit_at
    read = service.read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )
    assert not isinstance(read, Rejected)
    assert read.acknowledged_watermark == 0 and read.acknowledged_at is None
    assert isinstance(
        _call(
            service, "acknowledge_visit", api.AcknowledgeVisit(f._SCOPE, sid, uuid4())
        ),
        Rejected,
    )
    newer = _call(
        service,
        "acknowledge_visit",
        api.AcknowledgeVisit(f._SCOPE, sid, visit2.value.visit_id),
    )
    older = _call(
        service,
        "acknowledge_visit",
        api.AcknowledgeVisit(f._SCOPE, sid, visit1.value.visit_id),
    )
    assert isinstance(newer, Committed) and isinstance(older, Committed)
    assert (
        newer.value.acknowledged_watermark
        == older.value.acknowledged_watermark
        == visit2.value.visit_watermark
    )
    assert older.value.acknowledged_at == visit2.value.visit_at


def test_simultaneous_hosts_cannot_both_begin(tmp_path: Path, api: ModuleType) -> None:
    _, sid, _, _ = _start(tmp_path, api)
    barrier = threading.Barrier(2)
    tid = uuid4()

    def wait_at_gate() -> None:
        barrier.wait()

    def compete() -> Any:
        return _call(
            _service(tmp_path, boundary=wait_at_gate),
            "begin",
            api.BeginTurn(f._SCOPE, sid, tid, uuid4()),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(compete), pool.submit(compete)
        outcomes = [left.result(timeout=10), right.result(timeout=10)]
    assert sum(isinstance(o, Committed) for o in outcomes) == 1
    assert sum(isinstance(o, Rejected) for o in outcomes) == 1


@pytest.mark.parametrize(
    "method", ["open", "begin", "abandon", "issue_visit", "acknowledge_visit"]
)
@pytest.mark.parametrize("replay", [False, True])
def test_every_write_reauthorises_before_fresh_or_replay(
    tmp_path: Path, api: ModuleType, method: str, replay: bool
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    visit = _call(service, "issue_visit", api.IssueVisit(f._SCOPE, sid))
    command = {
        "open": api.OpenSession(f._SCOPE, uuid4(), Classification.INTERNAL),
        "begin": api.BeginTurn(f._SCOPE, sid, uuid4(), uuid4()),
        "abandon": api.AbandonTurn(f._SCOPE, sid, tid, "interrupted"),
        "issue_visit": api.IssueVisit(f._SCOPE, sid),
        "acknowledge_visit": api.AcknowledgeVisit(f._SCOPE, sid, visit.value.visit_id),
    }[method]
    key = uuid4()
    if replay:
        assert isinstance(_call(service, method, command, key), Committed)
    before = f._rows(tmp_path, "SELECT count(*) FROM memory_session_operations")
    racing = _service(tmp_path, boundary=lambda: _revoke(tmp_path, f._SECOND_GRANT_ID))
    denied = _call(racing, method, command, key)
    assert (
        isinstance(denied, Rejected) and denied.failure.code == "authorisation_denied"
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_operations") == before
    assert f._rows(
        tmp_path, "SELECT outcome FROM audit_events ORDER BY rowid DESC LIMIT 1"
    ) == [("deny",)]


def test_expiry_while_waiting_for_writer_denies_preparation(
    tmp_path: Path, api: ModuleType
) -> None:
    _, sid, tid, attempt = _start(tmp_path, api)
    moments = [f._NOW]

    def expire() -> None:
        moments[0] += timedelta(minutes=2)

    racing = _service(tmp_path, lambda: moments[0], expire)
    denied = _call(
        racing, "prepare", api.PrepareTurn(f._SCOPE, sid, tid, attempt, "ready", ())
    )
    assert (
        isinstance(denied, Rejected) and denied.failure.code == "authorisation_denied"
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (0,)
    ]


def test_exact_limits_and_common_offset_observation_times(
    tmp_path: Path, api: ModuleType
) -> None:
    from datetime import timezone

    service, sid, tid, attempt = _start(tmp_path, api)
    observations = tuple(
        api.DurableObservation(
            "é" * 2048, observed_at=f._NOW.astimezone(timezone(timedelta(hours=i)))
        )
        for i in range(8)
    )
    prepared = _call(
        service,
        "prepare",
        api.PrepareTurn(f._SCOPE, sid, tid, attempt, "é" * 16384, observations),
    )
    assert isinstance(prepared, Committed)
    assert len(prepared.value.response.encode()) == 32768
    assert len(prepared.value.observations) == 8
    assert 65536 < prepared.value.prepared_bytes <= 73728


def test_secret_abandonment_has_no_raw_audit_or_storage(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    secret = "-----BEGIN RSA PRIVATE KEY-----"
    denied = _call(service, "abandon", api.AbandonTurn(f._SCOPE, sid, tid, secret))
    assert isinstance(denied, Rejected) and denied.failure.code == "secret_rejected"
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_abandonments") == [
        (0,)
    ]
    assert secret not in repr(
        f._rows(tmp_path, "SELECT canonical_event FROM audit_events")
    )


def test_instance_binding_is_rechecked_and_unknown_owner_stays_opaque(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    # Synthetic corruption: the writer gate must not mistake a foreign-instance
    # recovery record for local authority. Normal SQL cannot mutate this row.
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("DROP TRIGGER memory_sessions_no_update")
        connection.execute(
            "UPDATE memory_sessions SET instance_id = ?", (str(uuid4()),)
        )
        connection.commit()
    denied = service.read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )
    assert (
        isinstance(denied, Rejected) and denied.failure.code == "authorisation_denied"
    )


def test_classification_requires_both_clearance_and_write_grant(
    tmp_path: Path, api: ModuleType
) -> None:
    _seed(tmp_path)
    _revoke(tmp_path, f._SECOND_GRANT_ID)
    f._insert_grant(
        tmp_path,
        grant_id=uuid4(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.PUBLIC,
    )
    service = _service(tmp_path)
    denied = _call(
        service, "open", api.OpenSession(f._SCOPE, uuid4(), Classification.INTERNAL)
    )
    assert (
        isinstance(denied, Rejected) and denied.failure.code == "authorisation_denied"
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_sessions") == [(0,)]


def test_operation_records_are_immutable_and_bind_real_audit_receipts(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    prepared = _call(
        service, "prepare", api.PrepareTurn(f._SCOPE, sid, tid, attempt, "output", ())
    )
    assert isinstance(prepared, Committed)
    records = f._rows(
        tmp_path,
        "SELECT o.mutation_id, o.command_digest, i.mutation_id, i.command_digest, i.original_event_id, a.canonical_event FROM memory_session_operations o JOIN idempotency_records i ON i.principal_id=o.principal_id AND i.operation=o.operation AND i.idempotency_key=o.idempotency_key JOIN audit_events a ON a.event_id=i.original_event_id",
    )
    assert len(records) == 3
    for mid, digest, stored_mid, stored_digest, event_id, event in records:
        assert mid == stored_mid and digest == stored_digest
        assert isinstance(event, bytes)
        assert json.loads(event)["mutation_id"] == mid
        assert event_id is not None
    with _open_write_connection(tmp_path, create=False) as connection:
        for table in (
            "memory_sessions",
            "memory_session_operations",
            "memory_session_turns",
            "memory_session_preparations",
        ):
            for sql in (
                f"DELETE FROM {table}",
                f"UPDATE {table} SET mutation_id = mutation_id",
            ):
                with pytest.raises(
                    sqlite3.IntegrityError, match="immutable_session_record"
                ):
                    connection.execute(sql)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_visit_replay_and_clock_rollback_never_advance_automatically(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, _, _ = _start(tmp_path, api)
    key = uuid4()
    first = _call(service, "issue_visit", api.IssueVisit(f._SCOPE, sid), key)
    replay = _call(service, "issue_visit", api.IssueVisit(f._SCOPE, sid), key)
    assert (
        isinstance(replay, Replayed) and replay.value.visit_id == first.value.visit_id
    )
    backwards = _service(tmp_path, lambda: f._NOW - timedelta(seconds=1))
    second = _call(backwards, "issue_visit", api.IssueVisit(f._SCOPE, sid))
    assert (
        second.value.visit_watermark == 2
        and second.value.visit_at == first.value.visit_at
    )
    assert second.value.acknowledged_at is None


def test_host_generated_stable_uuid5_identities_are_accepted(
    tmp_path: Path, api: ModuleType
) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    sid = uuid5(f._INSTANCE_ID, "synthetic-session")
    tid = uuid5(sid, "synthetic-turn")
    attempt = uuid5(tid, "synthetic-attempt")
    assert isinstance(
        _call(service, "open", api.OpenSession(f._SCOPE, sid, Classification.INTERNAL)),
        Committed,
    )
    assert isinstance(
        _call(service, "begin", api.BeginTurn(f._SCOPE, sid, tid, attempt)), Committed
    )


@pytest.mark.parametrize("field", ["observed_at", "valid_from", "valid_to"])
def test_unserialisable_custody_timestamp_is_rejected_before_preparation(
    tmp_path: Path, api: ModuleType, field: str
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    observation = api.DurableObservation(
        "synthetic old observation", **{field: datetime(1, 1, 1, tzinfo=UTC)}
    )
    result = _call(
        service,
        "prepare",
        api.PrepareTurn(f._SCOPE, sid, tid, attempt, "ready", (observation,)),
    )
    assert isinstance(result, Rejected) and result.failure.code == "invalid_request"
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (0,)
    ]


def test_dst_fold_window_invalid_after_normalisation_rejects_preparation(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    zone = ZoneInfo("Europe/Zurich")
    command = api.PrepareTurn(
        f._SCOPE,
        sid,
        tid,
        attempt,
        "ready",
        (
            api.DurableObservation(
                "synthetic fold observation",
                valid_from=datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1),
                valid_to=datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0),
            ),
        ),
    )
    # Existing same-zone comparison accepts the wall-clock ordering. Its
    # semantics stay frozen; preparation must additionally validate stored UTC.
    assert (
        validate_ingest_payload(
            f._agent_actor(),
            future_ingest(command, Classification.INTERNAL),
            effective_at=f._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(None,),
        )
        is None
    )
    before = f._rows(tmp_path, "SELECT count(*) FROM idempotency_records")
    result = _call(service, "prepare", command)
    assert isinstance(result, Rejected) and result.failure.code == "invalid_request"
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (0,)
    ]
    assert f._rows(tmp_path, "SELECT count(*) FROM idempotency_records") == before
    recovered = _service(tmp_path).read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )
    assert not isinstance(recovered, Rejected)
    assert recovered.state == "started" and recovered.response is None


@pytest.mark.parametrize("representation", ["zurich", "utc"])
def test_valid_dst_fold_window_reopens_as_valid_future_ingest(
    tmp_path: Path, api: ModuleType, representation: str
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    expected_from = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
    expected_to = datetime(2026, 10, 25, 1, 45, tzinfo=UTC)
    zone = ZoneInfo("Europe/Zurich")
    start = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=0)
    end = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=1)
    if representation == "utc":
        start, end = expected_from, expected_to
    command = api.PrepareTurn(
        f._SCOPE,
        sid,
        tid,
        attempt,
        "ready",
        (api.DurableObservation("synthetic fold observation", start, end),),
    )
    result = _call(service, "prepare", command)
    assert isinstance(result, Committed)
    recovered = _service(tmp_path).read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )
    assert not isinstance(recovered, Rejected)
    assert recovered.state == "prepared" and recovered.custody_receipt is None
    assert recovered.observations[0].valid_from == expected_from
    assert recovered.observations[0].valid_to == expected_to
    recovered_command = replace(
        command,
        response=recovered.response,
        observations=recovered.observations,
    )
    assert (
        validate_ingest_payload(
            f._agent_actor(),
            future_ingest(recovered_command, recovered.classification),
            effective_at=f._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=tuple(o.observed_at for o in recovered.observations),
        )
        is None
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM facts") == [(0,)]


def test_original_invalid_dst_fold_window_is_not_repaired_by_normalisation(
    tmp_path: Path, api: ModuleType
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    zone = ZoneInfo("Europe/Zurich")
    start = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    end = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    # UTC ordering is valid, but frozen original-value validation rejects the
    # reversed same-zone wall times. Normalisation must not bypass that check.
    assert start.astimezone(UTC) < end.astimezone(UTC)
    result = _call(
        service,
        "prepare",
        api.PrepareTurn(
            f._SCOPE,
            sid,
            tid,
            attempt,
            "ready",
            (api.DurableObservation("synthetic fold observation", start, end),),
        ),
    )
    assert isinstance(result, Rejected) and result.failure.code == "invalid_request"
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_preparations") == [
        (0,)
    ]
