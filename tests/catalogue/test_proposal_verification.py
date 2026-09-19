"""Offline proposal integrity against real, disposable authority history."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import test_verification as f

from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import (
    CairnAuthority,
    IngestAssertion,
    InvalidateFacts,
    PromoteFacts,
)
from cairn.authority.proposal_types import AcceptProposal, ProposeMemory, RejectProposal
from cairn.authority.proposals import CairnProposals
from cairn.catalogue.audit import (
    ActionKind,
    Classification,
    Scope,
    ScopeRole,
    ScopeSegment,
    canonical_audit_bytes,
    hash_audit_event,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.proposal_verification import (
    ProposalVerificationError,
    verify_proposals,
)
from cairn.catalogue.sqlite import CATALOGUE_FILENAME, canonical_timestamp
from cairn.catalogue.transactions import CatalogueTransactions, Committed, Replayed
from cairn.catalogue.verification import VerificationError, verify_catalogue
from cairn.operations.backup import create_backup
from cairn.operations.restore import restore_bundle
from cairn.screening import SecretScreen

ACTOR = Actor(f._MANAGER_ID, f._MANAGER_CREDENTIAL_ID)
SOURCE = Scope("local", (ScopeSegment("job", "verification"),))
TARGET = Scope("local", ())


def _services(
    path: Path, clock: Callable[[], datetime] = lambda: f.NOW
) -> tuple[CairnAuthority, CairnProposals]:
    tx = CatalogueTransactions(
        path, writer_gate=threading.Lock(), clock=clock, uuid_factory=uuid4
    )
    screen = SecretScreen()
    authority = CairnAuthority(path, tx, clock, uuid4, True, screen)
    return authority, CairnProposals(path, tx, clock, screen, authority)


def _call(service: Any, method: str, command: Any, **kwargs: Any) -> Any:
    result = getattr(service, method)(
        kwargs.pop("actor", ACTOR),
        command,
        idempotency_key=kwargs.pop("key", uuid4()),
        correlation_id=uuid4(),
        **kwargs,
    )
    assert isinstance(result, (Committed, Replayed)), result
    return result


def _seed(path: Path, state: str = "pending") -> tuple[Any, Any, Any]:
    f._seed_authority_catalogue(path)
    with closing(sqlite3.connect(path / CATALOGUE_FILENAME)) as c, c:
        c.execute(
            "INSERT INTO grants VALUES (?, ?, 'local', '[]', "
            '\'["ingest","invalidate","promote","retrieve"]\', \'restricted\', '
            '\'["internal","restricted"]\', NULL, NULL, NULL, ?)',
            (str(uuid4()), str(ACTOR.principal_id), "2026-08-05T12:00:00.000000Z"),
        )
    authority, service = _services(path)
    ingest = _call(
        authority,
        "ingest",
        IngestAssertion(
            SOURCE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            (FactDraft("A reusable finding", None, None),),
            evidence_payload=b"Synthetic verification evidence",
        ),
    )
    p = ProposeMemory(
        SOURCE, uuid4(), ingest.value.fact_ids[0], TARGET, "A bounded reason"
    )
    _call(service, "propose", p)
    command = AcceptProposal(
        SOURCE, p.proposal_id, ingest.value.evidence_id, Classification.INTERNAL
    )
    key = uuid4()
    if state == "accepted":
        _call(service, "accept", command, key=key)
        _call(service, "accept", command, key=key)
    elif state == "rejected":
        rejection = RejectProposal(SOURCE, p.proposal_id, "Insufficient support")
        _call(service, "reject", rejection, key=key)
        _call(service, "reject", rejection, key=key)
    return p, command, key


@contextmanager
def _edit(path: Path) -> Iterator[sqlite3.Connection]:
    # Retain CHECKs and FKs, restoring exact trigger bytes before verification.
    with closing(sqlite3.connect(path / CATALOGUE_FILENAME)) as c, c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA defer_foreign_keys=ON")
        triggers = c.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type='trigger'"
        ).fetchall()
        for name, _ in triggers:
            c.execute(f'DROP TRIGGER "{name}"')
        yield c
        for _, sql in triggers:
            c.execute(sql)
        assert c.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert c.execute("PRAGMA ignore_check_constraints").fetchone() == (0,)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("state", ["pending", "rejected", "accepted"])
def test_real_history_and_replay_verify(tmp_path: Path, state: str) -> None:
    _seed(tmp_path, state)
    verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "state,sql",
    [
        ("pending", "UPDATE memory_proposals SET reason='Changed reason'"),
        ("pending", "UPDATE memory_proposals SET target_segments=scope_segments"),
        ("pending", "UPDATE memory_proposals SET classification='public'"),
        (
            "pending",
            "UPDATE memory_proposals SET recorded_at='2026-02-30T12:00:00.000000Z'",
        ),
        ("pending", "UPDATE memory_proposals SET proposal_id=upper(proposal_id)"),
        ("pending", "UPDATE memory_proposals SET target_segments='[null]'"),
        ("pending", "UPDATE memory_proposals SET target_segments='[] '"),
        ("pending", "UPDATE memory_proposals SET command_digest=zeroblob(32)"),
        ("pending", "DELETE FROM memory_proposals"),
        ("rejected", "UPDATE memory_proposal_decisions SET reason='Changed judgement'"),
        ("rejected", "DELETE FROM memory_proposal_decisions"),
        ("accepted", "DELETE FROM memory_proposal_decisions"),
        (
            "accepted",
            "UPDATE memory_proposal_decisions SET promoted_fact_id=(SELECT source_fact_id FROM memory_proposals)",
        ),
        (
            "accepted",
            "UPDATE memory_proposal_decisions SET mutation_id=(SELECT mutation_id FROM memory_proposals)",
        ),
        (
            "accepted",
            "UPDATE facts SET body='Different publication' WHERE derived_from IS NOT NULL",
        ),
        (
            "accepted",
            "UPDATE facts SET trust='candidate' WHERE derived_from IS NOT NULL",
        ),
        (
            "accepted",
            "UPDATE facts SET promoted_by='cccccccc-cccc-4ccc-8ccc-cccccccccccc' WHERE derived_from IS NOT NULL",
        ),
    ],
)
def test_fk_valid_semantic_damage_is_refused(
    tmp_path: Path, state: str, sql: str
) -> None:
    _seed(tmp_path, state)
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        c.execute(sql)
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_rehashed_extra_result_member_is_not_a_proposal_receipt(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _edit(tmp_path) as c:
        raw = c.execute(
            "SELECT result_bytes FROM idempotency_records WHERE operation='memory-propose'"
        ).fetchone()[0]
        document = json.loads(raw)
        document["invented"] = True
        changed = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        c.execute(
            "UPDATE idempotency_records SET result_bytes=?,result_digest=? WHERE operation='memory-propose'",
            (changed, hashlib.sha256(changed).digest()),
        )
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def _events(c: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(e.event_id): e
        for (raw,) in c.execute("SELECT canonical_event FROM audit_events")
        for e in (parse_canonical_audit_bytes(raw),)
    }


@pytest.mark.parametrize("state", ["pending", "rejected", "accepted"])
@pytest.mark.parametrize("damage", ["delete_both", "relabel", "result_schema", "key"])
def test_reverse_receipt_inventory(tmp_path: Path, state: str, damage: str) -> None:
    _seed(tmp_path, state)
    operation = {
        "pending": "memory-propose",
        "accepted": "memory-proposal-accept",
        "rejected": "memory-proposal-reject",
    }[state]
    table = "memory_proposals" if state == "pending" else "memory_proposal_decisions"
    with _edit(tmp_path) as c:
        if damage == "delete_both":
            c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM idempotency_records WHERE operation=?", (operation,))
        elif damage == "relabel":
            c.execute(f"DELETE FROM {table}")
            c.execute(
                "UPDATE idempotency_records SET operation='promote' WHERE operation=?",
                (operation,),
            )
        elif damage == "key":
            key = str(uuid4())
            c.execute(f"UPDATE {table} SET idempotency_key=?", (key,))
            c.execute(
                "UPDATE idempotency_records SET idempotency_key=? WHERE operation=?",
                (key, operation),
            )
        else:
            c.execute(
                "UPDATE idempotency_records SET result_schema='unknown/v1' WHERE operation=?",
                (operation,),
            )
        # Isolate semantic verification where the general receipt/outbox check
        # would already refuse the corruption with its earlier error code.
        with pytest.raises(ProposalVerificationError):
            verify_proposals(c, f.INSTANCE_ID, _events(c))
    with pytest.raises(VerificationError):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("action_code", "memory-propose"),
        ("requested_scope", SOURCE),
        ("target_scope", SOURCE),
        ("action_kind", ActionKind.ADMINISTRATION),
        ("reason_code", "invented_reason"),
        ("principal_id", f._WORKLOAD_ID),
        ("command_digest", bytes(32)),
        ("idempotency_key", f._NONEXISTENT_ID),
        ("affected_fact_ids", ()),
    ],
)
def test_exact_original_and_replay_audit_binding(
    tmp_path: Path, replay: bool, field: str, value: Any
) -> None:
    _seed(tmp_path, "accepted")
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        events = _events(c)
        event = next(
            e
            for e in events.values()
            if e.draft.action_code == "promote"
            and (e.draft.replay_of_mutation_id is not None) == replay
        )
        changes = {field: value}
        if field in {"action_code", "action_kind"}:
            changes.update(evidence_reference=None, evidence_digest=None)
        events[str(event.event_id)] = replace(
            event, draft=replace(event.draft, **changes)
        )
        with pytest.raises(ProposalVerificationError):
            verify_proposals(c, f.INSTANCE_ID, events)


@pytest.mark.parametrize(
    "damage",
    [
        "decision_before_proposal",
        "source_after_proposal",
        "duplicate_publication",
        "publication_outbox",
        "evidence_after_acceptance",
    ],
)
def test_history_and_publication_inventory(tmp_path: Path, damage: str) -> None:
    p, command, _ = _seed(tmp_path, "accepted")
    with _edit(tmp_path) as c:
        events = _events(c)
        creation = next(
            e for e in events.values() if e.draft.action_code == "memory-propose"
        )
        acceptance = next(
            e
            for e in events.values()
            if e.draft.action_code == "promote" and e.draft.mutation_id is not None
        )
        ingest = next(e for e in events.values() if e.draft.action_code == "ingest")
        if damage == "decision_before_proposal":
            events[str(acceptance.event_id)] = replace(
                acceptance, sequence=creation.sequence
            )
        elif damage == "source_after_proposal":
            events[str(ingest.event_id)] = replace(
                ingest, sequence=acceptance.sequence + 1
            )
        elif damage == "evidence_after_acceptance":
            events[str(ingest.event_id)] = replace(
                ingest, draft=replace(ingest.draft, affected_evidence_ids=())
            )
            later = replace(
                ingest,
                event_id=uuid4(),
                sequence=acceptance.sequence + 1,
                draft=replace(ingest.draft, affected_fact_ids=()),
            )
            events[str(later.event_id)] = later
        elif damage == "duplicate_publication":
            # A second successful mutation claiming the same real publication.
            later = replace(
                acceptance,
                event_id=uuid4(),
                sequence=acceptance.sequence + 2,
                draft=replace(
                    acceptance.draft, action_code="ingest", mutation_id=uuid4()
                ),
            )
            events[str(later.event_id)] = later
        else:
            c.execute(
                "UPDATE projection_outbox SET fact_id=? WHERE kind='fact-promoted'",
                (str(p.source_fact_id),),
            )
        with pytest.raises(ProposalVerificationError):
            verify_proposals(c, f.INSTANCE_ID, events)


def test_multiple_proposals_legacy_promotion_and_historical_grants(
    tmp_path: Path,
) -> None:
    p, command, key = _seed(tmp_path, "accepted")
    authority, service = _services(tmp_path)
    second = replace(p, proposal_id=uuid4())
    _call(service, "propose", second)
    _call(service, "accept", replace(command, proposal_id=second.proposal_id))
    _call(service, "propose", replace(p, proposal_id=uuid4()))
    legacy = PromoteFacts(
        (p.source_fact_id,),
        command.evidence_id,
        TARGET,
        Classification.INTERNAL,
        p.reason,
    )
    legacy_key = uuid4()
    _call(authority, "promote", legacy, key=legacy_key)
    _call(authority, "promote", legacy, key=legacy_key)
    _call(
        authority,
        "invalidate",
        InvalidateFacts((p.source_fact_id,), "Later invalidation", None),
    )
    _call(service, "accept", command, key=key)
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        c.execute(
            "INSERT INTO grant_revocations SELECT grant_id, ?, ?, 'revoked' FROM grants WHERE grant_id NOT IN (SELECT grant_id FROM grant_revocations)",
            ("2026-08-06T12:00:00.000000Z", str(ACTOR.principal_id)),
        )
    verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize("state", ["pending", "rejected", "accepted"])
def test_backup_restore_and_recovery_at_most_once(tmp_path: Path, state: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    p, command, key = _seed(source, state)
    before = verify_catalogue(f._config(source))
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restored"
    target.mkdir()
    assert restore_bundle(f._config(target), bundle.bundle_path).report == before
    _, service = _services(target)
    if state != "rejected":
        first = _call(service, "accept", command, key=key)
        again = _call(service, "accept", command, key=key)
        assert isinstance(again, Replayed)
        assert (
            again.value == first.value
            and again.mutation_receipt == first.mutation_receipt
        )
    with closing(sqlite3.connect(target / CATALOGUE_FILENAME)) as c, c:
        assert c.execute(
            "SELECT count(*) FROM facts WHERE derived_from=?", (str(p.source_fact_id),)
        ).fetchone() == (0 if state == "rejected" else 1,)
    verify_catalogue(f._config(target))


@pytest.mark.parametrize("state", ["pending", "rejected", "accepted"])
def test_corrupt_backup_fails_offline_verification_and_restore(
    tmp_path: Path, state: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed(source, state)
    with _edit(source) as c:
        c.execute("UPDATE memory_proposals SET reason='Corrupt but canonical'")
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(source))
    # Backup currently preserves bytes, including corruption. It has no semantic
    # admission hook. Restore must refuse even a correctly hashed such bundle.
    bundle = create_backup(f._config(source), tmp_path / "backups", clock=lambda: f.NOW)
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        restore_bundle(f._config(target), bundle.bundle_path)
    # Restore leaves refused bytes available for diagnosis; they are not admitted.
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(target))


def test_fresh_process_verifies_without_authority_transport_or_client(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, "accepted")
    code = """
import importlib.abc, sqlite3, sys
from uuid import UUID
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('cairn.authority', 'cairn.transports', 'cairn.client', 'httpx', 'fastapi')):
            raise AssertionError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from cairn.catalogue.verification import _verify_connection
with sqlite3.connect(sys.argv[1]) as c:
    assert _verify_connection(c, UUID(sys.argv[2])).fact_count == 2
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / CATALOGUE_FILENAME),
            str(f.INSTANCE_ID),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _rehash(c: sqlite3.Connection, events: dict[str, Any]) -> None:
    """Repair outer chain/index checks so semantic damage reaches the oracle."""
    heads: dict[tuple[str, str], bytes] = {}
    c.execute("DELETE FROM audit_scope_index")
    for event in sorted(
        events.values(),
        key=lambda e: (e.draft.chain_kind, e.draft.chain_identity, e.sequence),
    ):
        d = event.draft
        chain = (d.chain_kind.value, d.chain_identity)
        event = replace(event, previous_hash=heads.get(chain, bytes(32)))
        digest = hash_audit_event(event)
        c.execute(
            "UPDATE audit_events SET recorded_at=?,previous_hash=?,event_hash=?,action_kind=?,action_code=?,outcome=?,reason_code=?,canonical_event=? WHERE event_id=?",
            (
                canonical_timestamp(event.recorded_at),
                event.previous_hash,
                digest,
                d.action_kind.value,
                d.action_code,
                d.outcome.value,
                d.reason_code,
                canonical_audit_bytes(event),
                str(event.event_id),
            ),
        )
        for role, scope in (
            (ScopeRole.SOURCE, d.source_scope),
            (ScopeRole.REQUESTED, d.requested_scope),
            (ScopeRole.TARGET, d.target_scope),
        ):
            CatalogueTransactions._insert_scope(c, event, role, scope)
        c.execute(
            "UPDATE audit_heads SET last_hash=? WHERE chain_kind=? AND chain_identity=?",
            (digest, *chain),
        )
        heads[chain] = digest


@pytest.mark.parametrize("damage", ["audit_reason", "audit_scope", "command_digest"])
def test_rehashed_outer_checks_do_not_validate_false_proposal_history(
    tmp_path: Path, damage: str
) -> None:
    _seed(tmp_path, "accepted")
    with _edit(tmp_path) as c:
        events = _events(c)
        event = next(
            e for e in events.values() if e.draft.action_code == "memory-propose"
        )
        changes: dict[str, Any]
        if damage == "audit_reason":
            changes = {"reason_code": "invented_success"}
        elif damage == "audit_scope":
            changes = {"requested_scope": TARGET}
        else:
            changes = {"command_digest": bytes(32)}
            mid = str(event.draft.mutation_id)
            raw = c.execute(
                "SELECT result_bytes FROM idempotency_records WHERE mutation_id=?",
                (mid,),
            ).fetchone()[0]
            result = json.loads(raw)
            result["command_digest"] = bytes(32).hex()
            payload = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            c.execute("UPDATE memory_proposals SET command_digest=zeroblob(32)")
            c.execute(
                "UPDATE idempotency_records SET command_digest=zeroblob(32),result_bytes=?,result_digest=? WHERE mutation_id=?",
                (payload, hashlib.sha256(payload).digest(), mid),
            )
        events[str(event.event_id)] = replace(
            event, draft=replace(event.draft, **changes)
        )
        _rehash(c, events)
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize("different_principal", [False, True])
def test_cross_proposal_and_principal_decision_receipts_do_not_swap(
    tmp_path: Path, different_principal: bool
) -> None:
    p, command, _ = _seed(tmp_path, "accepted")
    _, service = _services(tmp_path)
    second = replace(p, proposal_id=uuid4())
    _call(service, "propose", second)
    actor = ACTOR
    if different_principal:
        actor = Actor(uuid4(), uuid4())
        with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
            c.execute(
                "INSERT INTO principals VALUES (?, 'human', 'second-reviewer', ?)",
                (str(actor.principal_id), canonical_timestamp(f.NOW)),
            )
            c.execute(
                "INSERT INTO credentials VALUES (?, ?, ?, ?, NULL)",
                (
                    str(actor.credential_id),
                    str(actor.principal_id),
                    hashlib.sha256(b"synthetic-reviewer").digest(),
                    canonical_timestamp(f.NOW),
                ),
            )
            c.execute(
                "INSERT INTO grants VALUES (?, ?, 'local', '[]', '[\"promote\",\"retrieve\"]', 'restricted', '[\"internal\"]', NULL, NULL, NULL, ?)",
                (str(uuid4()), str(actor.principal_id), canonical_timestamp(f.NOW)),
            )
    _call(
        service, "accept", replace(command, proposal_id=second.proposal_id), actor=actor
    )
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        rows = c.execute(
            "SELECT * FROM memory_proposal_decisions ORDER BY proposal_id"
        ).fetchall()
        c.execute("DELETE FROM memory_proposal_decisions")
        for index, row in enumerate(rows):
            swapped = (row[0], *rows[1 - index][1:])
            c.execute(
                "INSERT INTO memory_proposal_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                swapped,
            )
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


@pytest.mark.parametrize(
    "damage",
    [
        "origin",
        "evidence",
        "source",
        "publication_scope",
        "validity",
        "classification",
        "source_scope",
    ],
)
def test_real_origin_evidence_and_publication_substitutions(
    tmp_path: Path, damage: str
) -> None:
    p, _, _ = _seed(tmp_path, "accepted")
    authority, _ = _services(tmp_path)
    other = _call(
        authority,
        "ingest",
        IngestAssertion(
            SOURCE,
            Classification.INTERNAL,
            SourceType.AGENT_CLAIM,
            (FactDraft("A reusable finding", None, None),),
            evidence_payload=b"Other synthetic evidence",
        ),
    )
    verify_catalogue(f._config(tmp_path))
    with _edit(tmp_path) as c:
        if damage == "origin":
            c.execute(
                "UPDATE facts SET derived_from=? WHERE derived_from IS NOT NULL",
                (str(other.value.fact_ids[0]),),
            )
        elif damage == "source":
            c.execute(
                "UPDATE memory_proposals SET source_fact_id=?",
                (str(other.value.fact_ids[0]),),
            )
        elif damage == "evidence":
            c.execute(
                "UPDATE memory_proposal_decisions SET evidence_id=?",
                (str(other.value.evidence_id),),
            )
            c.execute(
                "UPDATE facts SET evidence_id=? WHERE derived_from IS NOT NULL",
                (str(other.value.evidence_id),),
            )
        elif damage == "publication_scope":
            c.execute(
                "UPDATE facts SET scope_segments=(SELECT scope_segments FROM memory_proposals) WHERE derived_from IS NOT NULL"
            )
        elif damage == "validity":
            c.execute(
                "UPDATE facts SET valid_from='2026-08-05T12:00:00.000000Z' WHERE derived_from IS NOT NULL"
            )
        elif damage == "classification":
            c.execute(
                "UPDATE memory_proposal_decisions SET target_classification='restricted'"
            )
        else:
            c.execute("UPDATE memory_proposals SET scope_segments='[]'")
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_historical_wall_clock_can_move_backwards(tmp_path: Path) -> None:
    _seed(tmp_path, "accepted")
    with _edit(tmp_path) as c:
        events = _events(c)
        # Preserve logical audit order while making each operation's timestamp
        # earlier. No fact/decision equality to wall time is a valid invariant.
        for key, event in events.items():
            at = f.NOW - timedelta(seconds=event.sequence)
            events[key] = replace(event, recorded_at=at)
            if event.draft.mutation_id is not None:
                c.execute(
                    "UPDATE idempotency_records SET created_at=? WHERE mutation_id=?",
                    (canonical_timestamp(at), str(event.draft.mutation_id)),
                )
        _rehash(c, events)
    verify_catalogue(f._config(tmp_path))


def test_unaudited_extra_publication_cannot_hide_beside_a_valid_acceptance(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, "accepted")
    with _edit(tmp_path) as c:
        c.execute(
            "INSERT INTO facts SELECT ?,realm_id,scope_segments,body,trust,classification,assertion_id,derived_from,promoted_by,evidence_id,valid_from,valid_to,recorded_at FROM facts WHERE derived_from IS NOT NULL",
            (str(uuid4()),),
        )
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_invalidation_before_original_acceptance_is_not_legal_history(
    tmp_path: Path,
) -> None:
    p, _, _ = _seed(tmp_path, "accepted")
    authority, _ = _services(tmp_path)
    _call(
        authority,
        "invalidate",
        InvalidateFacts((p.source_fact_id,), "Later correction", None),
    )
    verify_catalogue(f._config(tmp_path))
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        events = _events(c)
        acceptance = next(
            e
            for e in events.values()
            if e.draft.action_code == "promote" and e.draft.mutation_id is not None
        )
        invalidation = next(
            e for e in events.values() if e.draft.action_code == "invalidate"
        )
        events[str(invalidation.event_id)] = replace(
            invalidation, sequence=acceptance.sequence - 1
        )
        with pytest.raises(ProposalVerificationError):
            verify_proposals(c, f.INSTANCE_ID, events)


@pytest.mark.parametrize(
    "raw",
    [
        '[{"kind":"job","id":"a","id":"b"}]',
        '[{"id":"a","kind":"job","extra":"value"}]',
        '[{"id":"a","kind":7}]',
        "[" * 990 + "null" + "]" * 990,
    ],
)
def test_noncanonical_and_malformed_scope_is_opaque(tmp_path: Path, raw: str) -> None:
    _seed(tmp_path)
    with _edit(tmp_path) as c:
        c.execute("UPDATE memory_proposals SET target_segments=?", (raw,))
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_rejection_cannot_conceal_an_unaudited_source_invalidation(
    tmp_path: Path,
) -> None:
    p, _, _ = _seed(tmp_path, "rejected")
    with _edit(tmp_path) as c:
        c.execute(
            "INSERT INTO fact_invalidations VALUES (?, ?, ?, NULL, 'Rejected proposal')",
            (
                str(p.source_fact_id),
                canonical_timestamp(f.NOW),
                str(ACTOR.principal_id),
            ),
        )
    with pytest.raises(VerificationError, match="proposal_integrity_invalid"):
        verify_catalogue(f._config(tmp_path))


def test_advancing_clock_acceptance_and_replay_preserve_distinct_record_times(
    tmp_path: Path,
) -> None:
    p, command, _ = _seed(tmp_path)
    now = f.NOW

    def clock() -> datetime:
        nonlocal now
        now += timedelta(microseconds=1)
        return now

    _, service = _services(tmp_path, clock)
    p = replace(p, proposal_id=uuid4())
    _call(service, "propose", p)
    command = replace(command, proposal_id=p.proposal_id)
    key = uuid4()
    first = _call(service, "accept", command, key=key)
    replay = _call(service, "accept", command, key=key)
    assert isinstance(first, Committed) and isinstance(replay, Replayed)
    assert first.value == replay.value
    assert first.mutation_receipt == replay.mutation_receipt
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as c, c:
        for table in ("memory_proposals", "memory_proposal_decisions"):
            at, receipt_at, audit_at = c.execute(
                f"SELECT p.recorded_at,r.created_at,a.recorded_at FROM {table} p "
                "JOIN idempotency_records r ON r.mutation_id=p.mutation_id "
                "JOIN audit_events a ON a.event_id=r.original_event_id "
                "WHERE p.proposal_id=?",
                (str(p.proposal_id),),
            ).fetchone()
            assert at < receipt_at == audit_at
        assert c.execute(
            "SELECT count(*) FROM facts WHERE derived_from=?", (str(p.source_fact_id),)
        ).fetchone() == (1,)
    verify_catalogue(f._config(tmp_path))
