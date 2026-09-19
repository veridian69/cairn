"""Independent verification of real session history, including crash boundaries."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
import test_verification as f

from cairn.authority import session_types as api
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority, IngestAssertion
from cairn.authority.session_codec import encode
from cairn.authority.sessions import CairnSessions
from cairn.catalogue.audit import (
    ActionKind,
    Classification,
    Scope,
    ScopeSegment,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.session_verification import (
    SessionVerificationError,
    verify_sessions,
)
from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    MutationReceipt,
    Rejected,
)
from cairn.catalogue.verification import VerificationError, verify_catalogue
from cairn.operations.backup import create_backup
from cairn.operations.restore import restore_bundle
from cairn.screening import SecretScreen

SCOPE = Scope("local", ())
ACTOR = Actor(f._MANAGER_ID, f._MANAGER_CREDENTIAL_ID)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _service(path: Path) -> CairnSessions:
    tx = CatalogueTransactions(
        path, writer_gate=threading.Lock(), clock=lambda: f.NOW, uuid_factory=uuid4
    )
    screen = SecretScreen()
    authority = CairnAuthority(path, tx, lambda: f.NOW, uuid4, True, screen)
    return CairnSessions(path, tx, lambda: f.NOW, uuid4, screen, authority)


def _call(service: CairnSessions, method: str, command: Any) -> Any:
    result = getattr(service, method)(
        ACTOR, command, idempotency_key=uuid4(), correlation_id=uuid4()
    )
    assert isinstance(result, Committed), result
    return result


def _seed(path: Path, *, empty: bool = False) -> tuple[CairnSessions, UUID, UUID]:
    f._seed_authority_catalogue(path)
    with closing(sqlite3.connect(path / CATALOGUE_FILENAME)) as c, c:
        c.execute(
            "INSERT INTO grants VALUES (?, ?, 'local', '[]', '[\"ingest\",\"retrieve\"]', 'restricted', '[\"internal\"]', NULL, NULL, NULL, ?)",
            (str(uuid4()), str(f._MANAGER_ID), "2026-08-05T12:00:00.000000Z"),
        )
    service = _service(path)
    sid, tid, attempt = uuid4(), uuid4(), uuid4()
    _call(service, "open", api.OpenSession(SCOPE, sid, Classification.INTERNAL))
    _call(service, "begin", api.BeginTurn(SCOPE, sid, tid, attempt))
    _call(
        service,
        "prepare",
        api.PrepareTurn(
            SCOPE,
            sid,
            tid,
            attempt,
            "A completed response",
            ()
            if empty
            else (
                api.DurableObservation("First observation"),
                api.DurableObservation("Second observation"),
            ),
        ),
    )
    return service, sid, tid


@contextmanager
def _edit(path: Path) -> Iterator[sqlite3.Connection]:
    # Deliberately bypass immutability, then restore the exact schema so the
    # semantic verifier, rather than the trigger inventory, must catch damage.
    with closing(sqlite3.connect(path / CATALOGUE_FILENAME)) as c, c:
        triggers = c.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type='trigger'"
        ).fetchall()
        for name, _ in triggers:
            c.execute(f'DROP TRIGGER "{name}"')
        yield c
        for _, sql in triggers:
            c.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE memory_sessions SET classification='public'",
        "UPDATE memory_sessions SET principal_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc'",
        'UPDATE memory_sessions SET scope_segments=\'[{"id":"different","kind":"job"}]\'',
        "UPDATE memory_sessions SET instance_id='99999999-9999-4999-8999-999999999999'",
        "UPDATE memory_session_turns SET attempt_id='99999999-9999-4999-8999-999999999999'",
        "UPDATE memory_session_preparations SET payload_digest=zeroblob(32)",
        "UPDATE memory_session_preparations SET payload=CAST(replace(CAST(payload AS TEXT),'First observation','Wrong observation') AS BLOB)",
        "UPDATE memory_session_operations SET command_digest=zeroblob(32) WHERE operation='session-begin'",
        "UPDATE memory_session_operations SET recorded_at='2026-02-30T12:00:00.000000Z'",
        "INSERT INTO memory_session_abandonments SELECT session_id,turn_id,'contradiction',mutation_id FROM memory_session_preparations",
    ],
)
def test_rejects_session_context_and_preparation_corruption(
    tmp_path: Path, sql: str
) -> None:
    _seed(tmp_path)
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        c.execute(sql)
    with pytest.raises(VerificationError):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM memory_session_commit_claims",
        "UPDATE memory_session_commit_claims SET command_digest=zeroblob(32)",
        "UPDATE memory_session_terminals SET command_digest=zeroblob(32)",
        "UPDATE idempotency_records SET result_schema='unrecognised/v1' WHERE operation='ingest'",
        "UPDATE memory_session_terminals SET custody_event_id=(SELECT original_event_id FROM idempotency_records WHERE operation='session-open')",
        "UPDATE facts SET body='altered observation'",
        "UPDATE facts SET trust='validated'",
        "UPDATE assertions SET source_type='human'",
    ],
)
def test_rejects_claim_and_actual_custody_corruption(tmp_path: Path, sql: str) -> None:
    service, sid, tid = _seed(tmp_path)
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        c.execute(sql)
    with pytest.raises(VerificationError):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "started"),
        ("turn_count", 200),
        ("prepared_bytes", 0),
        ("acknowledged_watermark", 90),
        ("response", "fabricated response"),
        ("custody_idempotency_key", "99999999-9999-4999-8999-999999999999"),
        (
            "custody_result",
            {
                "assertion_id": "99999999-9999-4999-8999-999999999999",
                "fact_ids": [],
                "evidence_id": None,
            },
        ),
    ],
)
def test_rejects_rehashed_terminal_snapshot(
    tmp_path: Path, field: str, value: Any
) -> None:
    service, sid, tid = _seed(tmp_path)
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    with _edit(tmp_path) as c:
        mid, raw = c.execute(
            "SELECT mutation_id,result FROM memory_session_terminals"
        ).fetchone()
        document = json.loads(raw)
        document["snapshot"][field] = value
        changed = _canonical(document)
        c.execute("UPDATE memory_session_terminals SET result=?", (changed,))
        c.execute(
            "UPDATE idempotency_records SET result_bytes=?,result_digest=? WHERE mutation_id=?",
            (changed, hashlib.sha256(changed).digest(), mid),
        )
    with pytest.raises(VerificationError):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "phase", ["prepared", "claim", "custody", "committed", "skipped"]
)
def test_recoverable_phases_backup_restore_real_catalogues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    service, sid, tid = _seed(source, empty=phase == "skipped")
    if phase in ("committed", "skipped"):
        _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    elif phase in ("claim", "custody"):
        ingest = service._authority.ingest

        def crash(*args: Any, **kwargs: Any) -> Any:
            if phase == "custody":
                ingest(*args, **kwargs)
            raise RuntimeError("simulated process loss")

        with monkeypatch.context() as patch:
            patch.setattr(service._authority, "ingest", crash)
            with pytest.raises(RuntimeError, match="simulated process loss"):
                _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    before = verify_catalogue(f._config(source))
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restored"
    target.mkdir()
    restored = restore_bundle(f._config(target), bundle.bundle_path)
    assert restored.report == before
    if phase in ("prepared", "claim", "custody"):
        result = _call(_service(target), "commit", api.CommitTurn(SCOPE, sid, tid))
        assert result.value.state == "committed"
    assert verify_catalogue(f._config(target)).fact_count == (
        0 if phase == "skipped" else 2
    )


def test_historical_results_survive_later_turns_and_visits(tmp_path: Path) -> None:
    service, sid, tid = _seed(tmp_path)
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    _call(service, "begin", api.BeginTurn(SCOPE, sid, uuid4(), uuid4()))
    visits = [
        _call(service, "issue_visit", api.IssueVisit(SCOPE, sid)).value
        for _ in range(2)
    ]
    for visit in reversed(visits):
        _call(
            service,
            "acknowledge_visit",
            api.AcknowledgeVisit(SCOPE, sid, visit.visit_id),
        )
    verify_catalogue(f._config(tmp_path))


def test_open_ended_validity_with_only_end_is_valid(tmp_path: Path) -> None:
    service, sid, _ = _seed(tmp_path)
    tid, attempt = uuid4(), uuid4()
    _call(service, "begin", api.BeginTurn(SCOPE, sid, tid, attempt))
    _call(
        service,
        "prepare",
        api.PrepareTurn(
            SCOPE,
            sid,
            tid,
            attempt,
            "Response",
            (
                api.DurableObservation(
                    "Observation with an end", valid_to=f.NOW + timedelta(days=1)
                ),
            ),
        ),
    )
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    verify_catalogue(f._config(tmp_path))


def test_session_operation_replays_remain_valid(tmp_path: Path) -> None:
    service, sid, tid = _seed(tmp_path)
    key = uuid4()
    for _ in range(2):
        service.commit(
            ACTOR,
            api.CommitTurn(SCOPE, sid, tid),
            idempotency_key=key,
            correlation_id=uuid4(),
        )
    verify_catalogue(f._config(tmp_path))


def test_actual_0010_terminal_without_any_claim_history_is_valid(
    tmp_path: Path,
) -> None:
    service, sid, tid = _seed(tmp_path)
    snapshot = service.read(
        ACTOR, api.ReadSession(SCOPE, sid, tid), correlation_id=uuid4()
    )
    assert isinstance(snapshot, api.SessionSnapshot)
    # Build the 0010 terminal using actual normal ingest and an actual audited
    # transaction. No claim event/row is ever produced in this history.
    from uuid import uuid5

    stable = uuid5(UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{sid}:{tid}")
    custody = service._authority.ingest(
        ACTOR,
        IngestAssertion(
            SCOPE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            tuple(
                FactDraft(o.body, o.valid_from, o.valid_to)
                for o in snapshot.observations
            ),
        ),
        idempotency_key=stable,
        correlation_id=uuid4(),
    )
    assert isinstance(custody, Committed)
    with _edit(tmp_path) as c:
        preparation = c.execute(
            "SELECT mutation_id,payload_digest FROM memory_session_preparations"
        ).fetchone()
        draft = parse_canonical_audit_bytes(
            c.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code='session-prepare'"
            ).fetchone()[0]
        ).draft
        digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "cairn.session.commit/v1",
                    "operation": "session-commit",
                    "command": {
                        "scope": {"realm": "local", "segments": []},
                        "session_id": str(sid),
                        "turn_id": str(tid),
                    },
                    "principal_id": str(ACTOR.principal_id),
                    "preparation_mutation_id": preparation[0],
                    "preparation_digest": preparation[1].hex(),
                }
            )
        ).digest()
        key = uuid4()

        def mutation(tx: Any) -> Any:
            receipt = MutationReceipt(tx.mutation_id, digest)
            result = replace(
                snapshot,
                state="committed",
                operational_receipt=receipt,
                custody_receipt=custody.mutation_receipt,
                custody_result=custody.value,
                custody_audit_receipt=custody.audit_receipt,
                custody_idempotency_key=stable,
            )
            tx.execute(
                "INSERT INTO memory_session_terminals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(sid),
                    str(tid),
                    preparation[0],
                    preparation[1],
                    "committed",
                    str(tx.mutation_id),
                    str(ACTOR.principal_id),
                    "session-commit",
                    str(key),
                    digest,
                    "2026-08-05T12:00:00.000000Z",
                    encode(result, receipt),
                    str(custody.mutation_receipt.mutation_id),
                    str(custody.audit_receipt.event_id),
                    str(custody.value.assertion_id),
                ),
            )
            return result

        from cairn.authority.session_codec import decode

        terminal = service._transactions.mutate_idempotent(
            replace(draft, action_code="session-commit"),
            principal_id=ACTOR.principal_id,
            operation="session-commit",
            idempotency_key=key,
            command_digest=digest,
            result_schema="cairn.session.snapshot/v1",
            mutation=mutation,
            encode=encode,
            decode=decode,
        )
        assert isinstance(terminal, Committed)
    verify_catalogue(f._config(tmp_path))


def test_deleted_claim_and_idempotency_cannot_masquerade_as_legacy(
    tmp_path: Path,
) -> None:
    service, sid, tid = _seed(tmp_path)
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    with _edit(tmp_path) as c:
        c.execute("DELETE FROM memory_session_commit_claims")
        c.execute(
            "DELETE FROM idempotency_records WHERE operation='session-commit-claim'"
        )
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_verifier_runs_without_loading_producer_or_transport(tmp_path: Path) -> None:
    service, sid, tid = _seed(tmp_path)
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    script = """
import sys, importlib.abc
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith(("cairn.authority", "cairn.client", "cairn.services", "cairn.http", "cairn.runtime.composition")) or "session_codec" in fullname:
            raise RuntimeError("producer dependency: " + fullname)
sys.meta_path.insert(0, Boundary())
from pathlib import Path
from uuid import UUID
from cairn.catalogue.verification import verify_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
p = Path(sys.argv[1])
assert verify_catalogue(CairnConfig(schema_version="cairn.config/v1", instance_id=UUID("11111111-1111-4111-8111-111111111111"), mode="test", http=HttpConfig(host="127.0.0.1", port=8000), paths=PathConfig(data=p, credentials=p / "credentials"))).fact_count == 2
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("response", "é" * 16385),
        (
            "observations",
            [
                {
                    "body": "é" * 2049,
                    "valid_from": None,
                    "valid_to": None,
                    "observed_at": None,
                }
            ],
        ),
        (
            "observations",
            [
                {
                    "body": "Observation",
                    "valid_from": None,
                    "valid_to": None,
                    "observed_at": None,
                }
            ]
            * 9,
        ),
        (
            "observations",
            [
                {
                    "body": "Observation",
                    "valid_from": "2026-08-06T00:00:00.000000Z",
                    "valid_to": "2026-08-05T00:00:00.000000Z",
                    "observed_at": None,
                }
            ],
        ),
        (
            "observations",
            [
                {
                    "body": "One",
                    "valid_from": None,
                    "valid_to": None,
                    "observed_at": "2026-08-05T00:00:00.000000Z",
                },
                {
                    "body": "Two",
                    "valid_from": None,
                    "valid_to": None,
                    "observed_at": None,
                },
            ],
        ),
    ],
)
def test_independent_limits_even_when_producer_accepts_bad_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    service, sid, _ = _seed(tmp_path)
    tid, attempt = uuid4(), uuid4()
    _call(service, "begin", api.BeginTurn(SCOPE, sid, tid, attempt))
    original = service._payload

    def faulty_encoder(*args: Any, **kwargs: Any) -> bytes:
        document = json.loads(original(*args, **kwargs))
        document["command"][field] = value
        return _canonical(document)

    monkeypatch.setattr(service, "_payload", faulty_encoder)
    _call(
        service,
        "prepare",
        api.PrepareTurn(
            SCOPE,
            sid,
            tid,
            attempt,
            "Response",
            (api.DurableObservation("Observation"),),
        ),
    )
    # All operation digests, results and audit hashes are real and consistent.
    # The offline verifier must still reject a faulty producer's output.
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_abandon_replacement_and_late_claim_are_valid(tmp_path: Path) -> None:
    service, sid, tid = _seed(tmp_path)
    abandoned = uuid4()
    _call(service, "begin", api.BeginTurn(SCOPE, sid, abandoned, uuid4()))
    _call(
        service,
        "abandon",
        api.AbandonTurn(SCOPE, sid, abandoned, "Interrupted before preparation"),
    )
    _call(service, "begin", api.BeginTurn(SCOPE, sid, uuid4(), uuid4(), abandoned))
    _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    # A second caller can claim after terminal but cannot create another one.
    service.commit(
        ACTOR,
        api.CommitTurn(SCOPE, sid, tid),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
    )
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        c.execute(
            "UPDATE memory_session_turns SET replaces_turn_id=? WHERE replaces_turn_id IS NOT NULL",
            (str(tid),),
        )
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_visit_dates_can_roll_back_with_consistent_audited_results(
    tmp_path: Path,
) -> None:
    service, sid, _ = _seed(tmp_path)
    _call(service, "issue_visit", api.IssueVisit(SCOPE, sid))
    second = _call(service, "issue_visit", api.IssueVisit(SCOPE, sid)).value
    _call(
        service, "acknowledge_visit", api.AcknowledgeVisit(SCOPE, sid, second.visit_id)
    )
    earlier = "2026-08-04T12:00:00.000000Z"
    with _edit(tmp_path) as c:
        c.execute(
            "UPDATE memory_session_visits SET issued_at=? WHERE watermark=2", (earlier,)
        )
        for mid, raw in c.execute(
            "SELECT mutation_id,result_bytes FROM idempotency_records WHERE operation IN ('session-issue-visit','session-acknowledge-visit')"
        ).fetchall():
            document = json.loads(raw)
            if document["snapshot"]["visit_watermark"] == 2:
                document["snapshot"]["visit_at"] = earlier
            if document["snapshot"]["acknowledged_watermark"] == 2:
                document["snapshot"]["acknowledged_at"] = earlier
            data = _canonical(document)
            c.execute(
                "UPDATE idempotency_records SET result_bytes=?,result_digest=? WHERE mutation_id=?",
                (data, hashlib.sha256(data).digest(), mid),
            )
    verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_code", "session-open"),
        ("principal_id", f._WORKLOAD_ID),
        ("command_digest", bytes(32)),
        ("replay_of_mutation_id", UUID("99999999-9999-4999-8999-999999999999")),
    ],
)
def test_session_replay_audit_must_bind_original_operation(
    tmp_path: Path, field: str, value: Any
) -> None:
    service, sid, tid = _seed(tmp_path)
    key = uuid4()
    for _ in range(2):
        service.commit(
            ACTOR,
            api.CommitTurn(SCOPE, sid, tid),
            idempotency_key=key,
            correlation_id=uuid4(),
        )
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        events = {
            str(e.event_id): e
            for (raw,) in c.execute("SELECT canonical_event FROM audit_events")
            for e in (parse_canonical_audit_bytes(raw),)
        }
        event = next(
            e
            for e in events.values()
            if e.draft.action_code == "session-commit"
            and e.draft.replay_of_mutation_id is not None
        )
        events[str(event.event_id)] = replace(
            event, draft=replace(event.draft, **{field: value})
        )
        with pytest.raises(SessionVerificationError):
            verify_sessions(c, f.INSTANCE_ID, events)


def test_scope_encoding_must_not_drop_unknown_members(tmp_path: Path) -> None:
    service, _, _ = _seed(tmp_path)
    sid = uuid4()
    _call(
        service,
        "open",
        api.OpenSession(
            Scope("local", (ScopeSegment("job", "job-1"),)),
            sid,
            Classification.INTERNAL,
        ),
    )
    with _edit(tmp_path) as c:
        c.execute(
            "UPDATE memory_sessions SET scope_segments=? WHERE session_id=?",
            ('[{"extra":"silently ignored","id":"job-1","kind":"job"}]', str(sid)),
        )
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize("action", ["session-prepare", "ingest"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("source_scope", SCOPE),
        ("target_scope", SCOPE),
        ("action_kind", ActionKind.ADMINISTRATION),
    ],
)
def test_audit_action_and_scope_roles_match_the_actual_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, field: str, value: Any
) -> None:
    service, sid, tid = _seed(tmp_path)
    ingest = service._authority.ingest

    def crash(*args: Any, **kwargs: Any) -> Any:
        ingest(*args, **kwargs)
        raise RuntimeError("custody gap")

    monkeypatch.setattr(service._authority, "ingest", crash)
    with pytest.raises(RuntimeError, match="custody gap"):
        _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        events = {
            str(e.event_id): e
            for (raw,) in c.execute("SELECT canonical_event FROM audit_events")
            for e in (parse_canonical_audit_bytes(raw),)
        }
        event = next(e for e in events.values() if e.draft.action_code == action)
        events[str(event.event_id)] = replace(
            event, draft=replace(event.draft, **{field: value})
        )
        with pytest.raises(SessionVerificationError):
            verify_sessions(c, f.INSTANCE_ID, events)


def test_restore_rejects_semantic_damage_even_with_valid_bundle_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed(source)
    with _edit(source) as c:
        c.execute("UPDATE memory_sessions SET classification='public'")
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restore"
    target.mkdir()
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        restore_bundle(f._config(target), bundle.bundle_path)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE memory_session_visits SET watermark=watermark+3",
        "UPDATE memory_session_visits SET issued_at='2026-08-04T12:00:00.000000Z'",
        "DELETE FROM memory_session_acknowledgements",
    ],
)
def test_rejects_visit_audit_and_ordinal_corruption(tmp_path: Path, sql: str) -> None:
    service, sid, _ = _seed(tmp_path)
    visit = _call(service, "issue_visit", api.IssueVisit(SCOPE, sid)).value
    _call(
        service, "acknowledge_visit", api.AcknowledgeVisit(SCOPE, sid, visit.visit_id)
    )
    with _edit(tmp_path) as c:
        c.execute(sql)
    with pytest.raises(VerificationError):
        verify_catalogue(f._config(tmp_path))


def _custody_gap(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[UUID, UUID]:
    service, sid, tid = _seed(path)
    ingest = service._authority.ingest

    def crash(*args: Any, **kwargs: Any) -> Any:
        result = ingest(*args, **kwargs)
        assert isinstance(result, Committed)
        raise RuntimeError("lost custody acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(service._authority, "ingest", crash)
        with pytest.raises(RuntimeError, match="lost custody acknowledgement"):
            _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    return sid, tid


@pytest.mark.parametrize(
    "damage",
    [
        "DELETE FROM idempotency_records WHERE operation='ingest'",
        "UPDATE idempotency_records SET operation='other-operation' WHERE operation='ingest'",
    ],
)
@pytest.mark.parametrize("restore", [False, True])
def test_audited_custody_gap_cannot_lose_its_ingest_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, restore: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sid, tid = _custody_gap(source, monkeypatch)
    assert verify_catalogue(f._config(source)).fact_count == 2
    with _edit(source) as c:
        c.execute(damage)
    target = source
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        if restore:
            bundle = create_backup(
                f._config(source), tmp_path / "backups", clock=lambda: f.NOW
            )
            target = tmp_path / "restored"
            target.mkdir()
            restore_bundle(f._config(target), bundle.bundle_path)
        else:
            verify_catalogue(f._config(source))
        # This is the consumer's verify/restore-before-recovery boundary.
        # Before the fix both checks returned success and this made 2 more facts.
        _call(_service(target), "commit", api.CommitTurn(SCOPE, sid, tid))
    with closing(sqlite3.connect(target / CATALOGUE_FILENAME)) as c, c:
        assert c.execute("SELECT count(*) FROM facts").fetchone() == (2,)


@pytest.mark.parametrize("empty", [False, True])
def test_existing_different_ingest_key_is_a_valid_runtime_conflict(
    tmp_path: Path, empty: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    service, sid, tid = _seed(source, empty=empty)
    stable = uuid5(UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{sid}:{tid}")
    existing = service._authority.ingest(
        ACTOR,
        IngestAssertion(
            SCOPE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            (FactDraft("A different existing command", None, None),),
        ),
        idempotency_key=stable,
        correlation_id=uuid4(),
    )
    assert isinstance(existing, Committed)
    result = service.commit(
        ACTOR,
        api.CommitTurn(SCOPE, sid, tid),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
    )
    if empty:
        assert isinstance(result, Committed) and result.value.state == "skipped"
    else:
        assert (
            isinstance(result, Rejected)
            and result.failure.code.value == "idempotency_conflict"
        )
    assert verify_catalogue(f._config(source)).fact_count == 1
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restored"
    target.mkdir()
    assert restore_bundle(f._config(target), bundle.bundle_path).report.fact_count == 1
    recovered = _service(target).commit(
        ACTOR,
        api.CommitTurn(SCOPE, sid, tid),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
    )
    assert isinstance(recovered, Rejected)
    assert verify_catalogue(f._config(target)).fact_count == 1


def test_matching_ingest_before_preparation_can_be_replayed(tmp_path: Path) -> None:
    service, sid, _ = _seed(tmp_path)
    tid, attempt = uuid4(), uuid4()
    _call(service, "begin", api.BeginTurn(SCOPE, sid, tid, attempt))
    stable = uuid5(UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{sid}:{tid}")
    existing = service._authority.ingest(
        ACTOR,
        IngestAssertion(
            SCOPE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            (FactDraft("Existing matching observation", None, None),),
        ),
        idempotency_key=stable,
        correlation_id=uuid4(),
    )
    assert isinstance(existing, Committed)
    _call(
        service,
        "prepare",
        api.PrepareTurn(
            SCOPE,
            sid,
            tid,
            attempt,
            "Response",
            (api.DurableObservation("Existing matching observation"),),
        ),
    )
    assert verify_catalogue(f._config(tmp_path)).fact_count == 1
    result = _call(service, "commit", api.CommitTurn(SCOPE, sid, tid))
    assert result.value.custody_receipt == existing.mutation_receipt
    assert verify_catalogue(f._config(tmp_path)).fact_count == 1


@pytest.mark.parametrize(
    "damage",
    [
        "DELETE FROM idempotency_records WHERE operation='ingest'",
        "UPDATE idempotency_records SET operation='other-operation' WHERE operation='ingest'",
    ],
)
def test_duplicate_custody_after_bypassed_verification_stays_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sid, tid = _custody_gap(source, monkeypatch)
    with _edit(source) as c:
        c.execute(damage)
    # Deliberately bypass verification to reproduce the reviewer's second
    # stage. This task changes the offline gate, not authority corruption guards.
    _call(_service(source), "commit", api.CommitTurn(SCOPE, sid, tid))
    with closing(sqlite3.connect(source / CATALOGUE_FILENAME)) as c, c:
        assert c.execute("SELECT count(*) FROM facts").fetchone() == (4,)
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        verify_catalogue(f._config(source))
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(VerificationError, match="session_integrity_invalid"):
        restore_bundle(f._config(target), bundle.bundle_path)
