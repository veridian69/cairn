"""Real custody, response loss and writer-fence regressions for durable turns."""

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
import test_retrieval as f
from test_ingest_composed_guard import _revoke
from test_sessions import _call, _service, _start

from cairn.authority import session_types as api
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.mutations import IngestAssertion
from cairn.authority.sessions import CairnSessions
from cairn.catalogue.audit import Classification
from cairn.catalogue.sqlite import _open_write_connection, read_connection
from cairn.catalogue.transactions import Committed, Rejected, Replayed


def _prepared(path: Path, *, empty: bool = False) -> tuple[CairnSessions, UUID, UUID]:
    service, sid, tid, attempt = _start(path, api)
    assert isinstance(
        _call(
            service,
            "prepare",
            api.PrepareTurn(
                f._SCOPE,
                sid,
                tid,
                attempt,
                "completed response",
                ()
                if empty
                else (
                    api.DurableObservation("first observation"),
                    api.DurableObservation("second observation"),
                ),
            ),
        ),
        Committed,
    )
    return service, sid, tid


def _commit(
    service: CairnSessions, sid: UUID, tid: UUID, key: UUID | None = None
) -> Any:
    assert hasattr(api, "CommitTurn"), "actual session custody is not implemented"
    return _call(service, "commit", api.CommitTurn(f._SCOPE, sid, tid), key)


def _read(service: CairnSessions, sid: UUID, tid: UUID) -> Any:
    return service.read(
        f._agent_actor(),
        api.ReadSession(f._SCOPE, sid, tid),
        correlation_id=f._CORRELATION_ID,
    )


def _facts(path: Path) -> list[tuple[Any, ...]]:
    return f._rows(path, "SELECT fact_id, body FROM facts ORDER BY body")


def _another_prepared(service: CairnSessions, sid: UUID) -> UUID:
    tid, attempt = uuid4(), uuid4()
    assert isinstance(
        _call(service, "begin", api.BeginTurn(f._SCOPE, sid, tid, attempt)), Committed
    )
    assert isinstance(
        _call(
            service,
            "prepare",
            api.PrepareTurn(
                f._SCOPE,
                sid,
                tid,
                attempt,
                "other response",
                (api.DurableObservation("other observation"),),
            ),
        ),
        Committed,
    )
    return tid


def test_cross_turn_caller_key_reuse_rejects_before_any_new_custody(
    tmp_path: Path,
) -> None:
    service, sid, first = _prepared(tmp_path)
    key = uuid4()
    assert isinstance(_commit(service, sid, first, key), Committed)
    second = _another_prepared(service, sid)
    before = _facts(tmp_path)
    rejected = _commit(service, sid, second, key)
    assert isinstance(rejected, Rejected)
    assert rejected.failure.code.value == "idempotency_conflict"
    assert _facts(tmp_path) == before
    assert _read(service, sid, second).state == "prepared"


def test_concurrent_cross_turn_caller_key_reuse_has_only_winners_custody(
    tmp_path: Path,
) -> None:
    service, sid, first = _prepared(tmp_path)
    second = _another_prepared(service, sid)
    barrier = threading.Barrier(2)
    key = uuid4()

    def run(tid: UUID) -> Any:
        first_write = True

        def boundary() -> None:
            nonlocal first_write
            if first_write:
                first_write = False
                barrier.wait(timeout=10)

        contender = _service(tmp_path, boundary=boundary)
        return _commit(contender, sid, tid, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, (first, second)))
    assert sum(isinstance(o, Committed) for o in outcomes) == 1
    assert sum(isinstance(o, Rejected) for o in outcomes) == 1
    winner = next(o for o in outcomes if isinstance(o, Committed))
    assert {row[0] for row in _facts(tmp_path)} == set(
        map(str, winner.value.custody_result.fact_ids)
    )
    assert f._rows(tmp_path, "SELECT count(*) FROM assertions") == [(1,)]


def test_claim_response_loss_reserves_key_without_custody_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sid, first = _prepared(tmp_path)
    second = _another_prepared(service, sid)
    key = uuid4()
    actual = service._transactions.mutate_idempotent

    def lose_claim(*args: Any, **kwargs: Any) -> Any:
        result = actual(*args, **kwargs)
        if kwargs["operation"] == "session-commit-claim":
            assert isinstance(result, Committed)
            raise RuntimeError("claim response lost")
        return result

    monkeypatch.setattr(service._transactions, "mutate_idempotent", lose_claim)
    with pytest.raises(RuntimeError, match="claim response lost"):
        _commit(service, sid, first, key)
    assert _facts(tmp_path) == []
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_commit_claims") == [
        (1,)
    ]
    reopened = _service(tmp_path)
    assert isinstance(_commit(reopened, sid, second, key), Rejected)
    assert _facts(tmp_path) == []
    recovered = _commit(reopened, sid, first, key)
    assert isinstance(recovered, Committed)
    assert len(_facts(tmp_path)) == 2
    replay = _commit(_service(tmp_path), sid, first, key)
    assert isinstance(replay, Replayed) and replay.value == recovered.value


@pytest.mark.parametrize("replay", [False, True])
def test_claim_writer_requires_current_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay: bool
) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid4()
    if replay:
        assert isinstance(_commit(service, sid, tid, key), Committed)
    before = _facts(tmp_path)
    actual = service._transactions.mutate_idempotent

    def lost_grant(*args: Any, **kwargs: Any) -> Any:
        if kwargs["operation"] == "session-commit-claim":
            guard = kwargs["reauthorise"]

            def revoke(transaction: Any) -> None:
                transaction.execute(
                    "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
                    (
                        str(f._SECOND_GRANT_ID),
                        f._TS,
                        str(f._AGENT_ID),
                        "synthetic_revocation",
                    ),
                )
                guard(transaction)

            kwargs["reauthorise"] = revoke
        return actual(*args, **kwargs)

    monkeypatch.setattr(service._transactions, "mutate_idempotent", lost_grant)
    assert isinstance(_commit(service, sid, tid, key), Rejected)
    assert _facts(tmp_path) == before
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_commit_claims") == [
        (1 if replay else 0,)
    ]


@pytest.mark.parametrize("replay", [False, True])
def test_ingest_writer_requires_exact_durable_claim_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay: bool
) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid4()
    if replay:
        assert isinstance(_commit(service, sid, tid, key), Committed)
    before = _facts(tmp_path)
    actual = service._authority.ingest

    def changed_claim(*args: Any, **kwargs: Any) -> Any:
        guard = kwargs["commit_guard"]

        def change(transaction: Any) -> None:
            transaction.execute("DROP TRIGGER memory_session_commit_claims_no_update")
            transaction.execute(
                "UPDATE memory_session_commit_claims SET command_digest=?", (b"x" * 32,)
            )
            guard(transaction)

        kwargs["commit_guard"] = change
        return actual(*args, **kwargs)

    monkeypatch.setattr(service._authority, "ingest", changed_claim)
    assert isinstance(_commit(service, sid, tid, key), Rejected)
    assert _facts(tmp_path) == before


def test_historical_terminal_without_claim_still_reserves_caller_key(
    tmp_path: Path,
) -> None:
    service, sid, first = _prepared(tmp_path)
    key = uuid4()
    original = _commit(service, sid, first, key)
    assert isinstance(original, Committed)
    # Retain exactly the terminal/ingest state that pre-0011 code persisted.
    with _open_write_connection(tmp_path, create=False) as connection:
        for name in (
            "memory_session_commit_claims_no_delete",
            "trg_idempotency_records_no_delete",
        ):
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name=?", (name,)
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {name}")
            if name == "memory_session_commit_claims_no_delete":
                connection.execute("DELETE FROM memory_session_commit_claims")
            else:
                connection.execute(
                    "DELETE FROM idempotency_records WHERE operation='session-commit-claim'"
                )
            connection.execute(sql)
        connection.commit()
    second = _another_prepared(service, sid)
    before = _facts(tmp_path)
    assert isinstance(_commit(service, sid, second, key), Rejected)
    assert _facts(tmp_path) == before
    replay = _commit(_service(tmp_path), sid, first, key)
    assert isinstance(replay, Replayed) and replay.value == original.value


def test_commit_preserves_actual_complete_receipts_and_exact_response_loss_replay(
    tmp_path: Path,
) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid4()
    first = _commit(service, sid, tid, key)
    assert isinstance(first, Committed)
    value = first.value
    assert value.state == "committed" and value.response == "completed response"
    assert value.operational_receipt == first.mutation_receipt
    assert value.custody_receipt != value.operational_receipt
    assert value.custody_result is not None and value.custody_audit_receipt is not None
    assert value.custody_idempotency_key == uuid5(
        UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{sid}:{tid}"
    )
    assert {row[0] for row in _facts(tmp_path)} == set(
        map(str, value.custody_result.fact_ids)
    )
    assert [row[1] for row in _facts(tmp_path)] == [
        "first observation",
        "second observation",
    ]
    assert f._rows(
        tmp_path,
        "SELECT mutation_id, original_event_id FROM idempotency_records WHERE operation='ingest'",
    ) == [
        (
            str(value.custody_receipt.mutation_id),
            str(value.custody_audit_receipt.event_id),
        )
    ]
    replay = _commit(_service(tmp_path), sid, tid, key)
    assert isinstance(replay, Replayed) and replay.value == value
    assert _read(_service(tmp_path), sid, tid) == value
    assert len(_facts(tmp_path)) == 2


def test_crash_gap_reopens_and_replays_real_ingest_without_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sid, tid = _prepared(tmp_path)
    actual = service._authority.ingest
    captured = []

    def crash(*args: Any, **kwargs: Any) -> Any:
        outcome = actual(*args, **kwargs)
        assert isinstance(outcome, Committed)
        captured.append(outcome)
        raise RuntimeError("injected custody acknowledgement loss")

    monkeypatch.setattr(service._authority, "ingest", crash)
    with pytest.raises(RuntimeError, match="acknowledgement loss"):
        _commit(service, sid, tid)
    before = _facts(tmp_path)
    assert len(before) == 2 and _read(_service(tmp_path), sid, tid).state == "prepared"
    recovered = _commit(_service(tmp_path), sid, tid)
    assert isinstance(recovered, Committed)
    assert recovered.value.custody_result == captured[0].value
    assert recovered.value.custody_receipt == captured[0].mutation_receipt
    assert recovered.value.custody_audit_receipt is not None
    with read_connection(tmp_path) as connection:
        row = connection.execute(
            "SELECT canonical_event FROM audit_events WHERE event_id=?",
            (str(recovered.value.custody_audit_receipt.event_id),),
        ).fetchone()
        assert json.loads(row[0])["replay_of_mutation_id"] == str(
            captured[0].mutation_receipt.mutation_id
        )
    assert _facts(tmp_path) == before
    # The authority accepts no model callback; reopening uses stored output only.


def test_empty_preparation_skips_without_any_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sid, tid = _prepared(tmp_path, empty=True)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("empty preparation must not invoke ingest")

    monkeypatch.setattr(service._authority, "ingest", forbidden)
    key = uuid4()
    first = _commit(service, sid, tid, key)
    assert isinstance(first, Committed) and first.value.state == "skipped"
    assert (
        first.value.custody_result,
        first.value.custody_receipt,
        first.value.custody_audit_receipt,
        first.value.custody_idempotency_key,
    ) == (None, None, None, None)
    assert _read(_service(tmp_path), sid, tid) == first.value
    assert _facts(tmp_path) == []
    assert isinstance(_commit(service, sid, tid, key), Replayed)


@pytest.mark.parametrize("state", ["missing", "started", "abandoned"])
def test_unprepared_turn_never_commits(tmp_path: Path, state: str) -> None:
    service, sid, tid, _ = _start(tmp_path, api)
    if state == "missing":
        tid = uuid4()
    elif state == "abandoned":
        assert isinstance(
            _call(
                service, "abandon", api.AbandonTurn(f._SCOPE, sid, tid, "interrupted")
            ),
            Committed,
        )
    assert isinstance(_commit(service, sid, tid), Rejected)
    assert _facts(tmp_path) == []


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("grant", [f._SECOND_GRANT_ID, f._DATA_GRANT_ID])
def test_current_grant_loss_inside_actual_custody_transaction_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay: bool, grant: UUID
) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid4()
    if replay:
        assert isinstance(_commit(service, sid, tid, key), Committed)
    before = _facts(tmp_path)
    actual = service._authority.ingest

    def fenced(*args: Any, **kwargs: Any) -> Any:
        guard = kwargs["commit_guard"]

        def revoke_then_guard(transaction: Any) -> None:
            transaction.execute(
                "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
                (str(grant), f._TS, str(f._AGENT_ID), "synthetic_revocation"),
            )
            guard(transaction)

        kwargs["commit_guard"] = revoke_then_guard
        return actual(*args, **kwargs)

    monkeypatch.setattr(service._authority, "ingest", fenced)
    assert isinstance(_commit(service, sid, tid, key), Rejected)
    assert _facts(tmp_path) == before
    assert _read(_service(tmp_path), sid, tid).state == (
        "committed" if replay else "prepared"
    )


def test_revoked_owner_cannot_recover_crash_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sid, tid = _prepared(tmp_path)
    actual = service._authority.ingest

    def crash(*args: Any, **kwargs: Any) -> Any:
        result = actual(*args, **kwargs)
        assert isinstance(result, Committed)
        raise RuntimeError("crash")

    monkeypatch.setattr(service._authority, "ingest", crash)
    with pytest.raises(RuntimeError, match="crash"):
        _commit(service, sid, tid)
    _revoke(tmp_path, f._SECOND_GRANT_ID)
    assert isinstance(_commit(_service(tmp_path), sid, tid), Rejected)
    assert len(_facts(tmp_path)) == 2
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_terminals") == [(0,)]


@pytest.mark.parametrize("same_key", [False, True])
def test_competing_commits_produce_one_fact_batch_and_one_terminal(
    tmp_path: Path, same_key: bool
) -> None:
    _, sid, tid = _prepared(tmp_path)
    barrier = threading.Barrier(2)
    key = uuid4()

    def run() -> Any:
        service = _service(tmp_path)
        barrier.wait(timeout=10)
        return _commit(service, sid, tid, key if same_key else uuid4())

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run(), range(2)))
    assert sum(isinstance(o, Committed) for o in outcomes) == 1
    assert all(isinstance(o, (Committed, Replayed, Rejected)) for o in outcomes)
    assert len(_facts(tmp_path)) == 2
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_terminals") == [(1,)]


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "instance",
        "owner",
        "scope",
        "classification",
        "attempt",
        "digest",
        "payload",
        "missing",
    ],
)
def test_custody_writer_checks_every_preparation_binding_before_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    field: str,
) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid4()
    if replay:
        assert isinstance(_commit(service, sid, tid, key), Committed)
    before = _facts(tmp_path)
    actual = service._authority.ingest

    def tamper(*args: Any, **kwargs: Any) -> Any:
        guard = kwargs["commit_guard"]

        def changed(transaction: Any) -> None:
            # Simulate corrupted/racing state inside the writer itself. The
            # denial must roll this transaction back, including dropped triggers.
            table, sql, values = {
                "instance": (
                    "memory_sessions",
                    "UPDATE memory_sessions SET instance_id=?",
                    (str(uuid4()),),
                ),
                "owner": (
                    "memory_sessions",
                    "UPDATE memory_sessions SET principal_id=?",
                    (str(uuid4()),),
                ),
                "scope": (
                    "memory_sessions",
                    "UPDATE memory_sessions SET scope_segments='[]'",
                    (),
                ),
                "classification": (
                    "memory_sessions",
                    "UPDATE memory_sessions SET classification='restricted'",
                    (),
                ),
                "attempt": (
                    "memory_session_turns",
                    "UPDATE memory_session_turns SET attempt_id=?",
                    (str(uuid4()),),
                ),
                "digest": (
                    "memory_session_preparations",
                    "UPDATE memory_session_preparations SET payload_digest=?",
                    (b"x" * 32,),
                ),
                "payload": (
                    "memory_session_preparations",
                    "UPDATE memory_session_preparations SET payload=?",
                    (b"{}",),
                ),
                "missing": (
                    "memory_session_preparations",
                    "DELETE FROM memory_session_preparations",
                    (),
                ),
            }[field]
            transaction.execute(
                f"DROP TRIGGER {table}_no_{'delete' if field == 'missing' else 'update'}"
            )
            if field in {"missing", "digest"} and replay:
                transaction.execute("DROP TRIGGER memory_session_terminals_no_delete")
                transaction.execute("DELETE FROM memory_session_terminals")
            if field in {"missing", "digest"}:
                transaction.execute(
                    "DROP TRIGGER memory_session_commit_claims_no_delete"
                )
                transaction.execute("DELETE FROM memory_session_commit_claims")
            if field == "owner":
                transaction.execute(
                    "INSERT INTO principals VALUES (?, 'workload', 'synthetic-other', ?)",
                    (values[0], f._TS),
                )
            transaction.execute(sql, values)
            guard(transaction)

        kwargs["commit_guard"] = changed
        return actual(*args, **kwargs)

    monkeypatch.setattr(service._authority, "ingest", tamper)
    assert isinstance(_commit(service, sid, tid, key), Rejected)
    assert _facts(tmp_path) == before
    assert _read(_service(tmp_path), sid, tid).state == (
        "committed" if replay else "prepared"
    )


@pytest.mark.parametrize("empty", [False, True])
def test_grant_loss_at_terminal_writer_never_records_completion(
    tmp_path: Path, empty: bool
) -> None:
    _, sid, tid = _prepared(tmp_path, empty=empty)
    calls = 0

    def boundary() -> None:
        nonlocal calls
        calls += 1
        if calls == (2 if empty else 3):
            _revoke(tmp_path, f._SECOND_GRANT_ID)

    outcome = _commit(_service(tmp_path, boundary=boundary), sid, tid)
    assert isinstance(outcome, Rejected)
    assert len(_facts(tmp_path)) == (0 if empty else 2)
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_terminals") == [(0,)]


def test_real_process_exit_after_custody_recovers_same_batch(tmp_path: Path) -> None:
    _, sid, tid = _prepared(tmp_path)
    code = """
import os, sys
from pathlib import Path
from uuid import UUID
sys.path.insert(0, 'tests/authority')
from test_session_custody import _commit
from test_sessions import _service
from cairn.catalogue.transactions import Committed
service = _service(Path(sys.argv[1]))
actual = service._authority.ingest
def crash(*args, **kwargs):
    outcome = actual(*args, **kwargs)
    assert isinstance(outcome, Committed)
    os._exit(73)
service._authority.ingest = crash
_commit(service, UUID(sys.argv[2]), UUID(sys.argv[3]))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(sid), str(tid)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr
    before = _facts(tmp_path)
    assert len(before) == 2
    service = _service(tmp_path)
    assert _read(service, sid, tid).state == "prepared"
    assert isinstance(_commit(service, sid, tid), Committed)
    assert _facts(tmp_path) == before


@pytest.mark.parametrize("wrong", ["preparation", "digest"])
def test_terminal_fk_binds_exact_preparation_not_just_any_existing_row(
    tmp_path: Path, wrong: str
) -> None:
    service, sid, tid = _prepared(tmp_path)
    assert isinstance(_commit(service, sid, tid), Committed)
    other, attempt = uuid4(), uuid4()
    assert isinstance(
        _call(service, "begin", api.BeginTurn(f._SCOPE, sid, other, attempt)), Committed
    )
    prepared = _call(
        service, "prepare", api.PrepareTurn(f._SCOPE, sid, other, attempt, "", ())
    )
    assert isinstance(prepared, Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("DROP TRIGGER memory_session_terminals_no_update")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            if wrong == "preparation":
                connection.execute(
                    "UPDATE memory_session_terminals SET preparation_mutation_id=?",
                    (str(prepared.mutation_receipt.mutation_id),),
                )
            else:
                connection.execute(
                    "UPDATE memory_session_terminals SET preparation_digest=?",
                    (b"x" * 32,),
                )
        connection.rollback()


def test_rejected_normal_ingest_cannot_become_terminal(tmp_path: Path) -> None:
    service, sid, tid = _prepared(tmp_path)
    key = uuid5(UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{sid}:{tid}")
    existing = service._authority.ingest(
        f._agent_actor(),
        IngestAssertion(
            f._SCOPE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            (FactDraft("different existing payload", None, None),),
        ),
        idempotency_key=key,
        correlation_id=f._CORRELATION_ID,
    )
    assert isinstance(existing, Committed)
    before = _facts(tmp_path)
    outcome = _commit(service, sid, tid)
    assert isinstance(outcome, Rejected)
    assert _read(service, sid, tid).state == "prepared"
    assert _facts(tmp_path) == before
    assert f._rows(tmp_path, "SELECT count(*) FROM memory_session_terminals") == [(0,)]


def test_commit_uses_canonical_times_and_normal_candidate_provenance(
    tmp_path: Path,
) -> None:
    service, sid, tid, attempt = _start(tmp_path, api)
    observed = datetime(2026, 8, 5, 9, tzinfo=UTC)
    valid_from = datetime(2026, 8, 5, 8, tzinfo=UTC)
    valid_to = datetime(2026, 8, 5, 12, tzinfo=UTC)
    assert isinstance(
        _call(
            service,
            "prepare",
            api.PrepareTurn(
                f._SCOPE,
                sid,
                tid,
                attempt,
                "completed",
                (
                    api.DurableObservation(
                        "dated observation", valid_from, valid_to, observed
                    ),
                ),
            ),
        ),
        Committed,
    )
    result = _commit(_service(tmp_path), sid, tid)
    assert isinstance(result, Committed)
    assert f._rows(
        tmp_path,
        "SELECT body, trust, classification, valid_from, valid_to, evidence_id FROM facts",
    ) == [
        (
            "dated observation",
            "candidate",
            "internal",
            "2026-08-05T08:00:00.000000Z",
            "2026-08-05T12:00:00.000000Z",
            None,
        )
    ]
    assert f._rows(
        tmp_path, "SELECT source_type, principal_id, observed_at FROM assertions"
    ) == [("agent-claim", str(f._AGENT_ID), "2026-08-05T09:00:00.000000Z")]


def test_loss_of_terminal_acknowledgement_replays_exact_recorded_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sid, tid = _prepared(tmp_path)
    actual = service._transactions.mutate_idempotent
    captured = []

    def response_loss(*args: Any, **kwargs: Any) -> Any:
        result = actual(*args, **kwargs)
        if kwargs["operation"] == "session-commit":
            assert isinstance(result, Committed)
            captured.append(result)
            raise RuntimeError("terminal response loss")
        return result

    monkeypatch.setattr(service._transactions, "mutate_idempotent", response_loss)
    key = uuid4()
    with pytest.raises(RuntimeError, match="terminal response loss"):
        _commit(service, sid, tid, key)
    replay = _commit(_service(tmp_path), sid, tid, key)
    assert isinstance(replay, Replayed)
    assert replay.value == captured[0].value
    assert replay.mutation_receipt == captured[0].mutation_receipt
    assert _read(_service(tmp_path), sid, tid) == replay.value
    assert len(_facts(tmp_path)) == 2
