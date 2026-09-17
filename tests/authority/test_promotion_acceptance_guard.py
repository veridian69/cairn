"""The internal proposal seam against real catalogue transactions."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import test_mutations as h

from cairn.authority import mutations
from cairn.authority.credentials import GrantOperation
from cairn.authority.gate import Actor, denial
from cairn.catalogue.audit import ActionKind, Classification
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import (
    Committed,
    FailureCode,
    MutationOutcome,
    MutationRejection,
    Rejected,
    Replayed,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.screening import SecretScreen


def _context(
    proposal_id: UUID,
    guard: Callable[[_GuardedTransaction], None] = lambda tx: None,
    record: Callable[[_MutationTransaction, mutations.FactsPromoted], None] = (
        lambda tx, result: None
    ),
) -> mutations.ProposalAcceptanceContext:
    context_type = getattr(mutations, "ProposalAcceptanceContext", None)
    assert context_type is not None, "promotion needs the bounded acceptance context"
    return cast(
        "mutations.ProposalAcceptanceContext", context_type(proposal_id, guard, record)
    )


def _authority(
    path: Path,
    *,
    clock: Callable[[], datetime] = lambda: h._NOW,
    before_lock: Callable[[], None] = lambda: None,
) -> mutations.CairnAuthority:
    return mutations.CairnAuthority(
        path,
        h._GrantRaceTransactions(
            path,
            writer_gate=threading.Lock(),
            clock=clock,
            uuid_factory=uuid4,
            interfere=lambda path: before_lock(),
        ),
        clock=clock,
        uuid_factory=uuid4,
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )


def _accept(
    authority: mutations.CairnAuthority,
    context: mutations.ProposalAcceptanceContext | None,
    *,
    actor: Actor | None = None,
    key: UUID = h._IDEMPOTENCY_KEY,
    command: mutations.PromoteFacts | None = None,
) -> MutationOutcome[mutations.FactsPromoted]:
    return authority.promote(
        actor or h._agent_actor(),
        command or h._promote_command(),
        idempotency_key=key,
        correlation_id=h._CORRELATION_ID,
        acceptance=context,
    )


def _deny(actor: Actor | None = None) -> MutationRejection:
    return denial(
        FailureCode.INVALID_REQUEST,
        "proposal already decided",
        h._CORRELATION_ID,
        realm_id=h._REALM,
        actor=actor or h._agent_actor(),
        grant_id=None,
        action_kind=ActionKind.DATA,
        action_code="promote",
        requested_scope=h._SOURCE_SCOPE,
        reason_code="proposal_conflict",
    )


def _claims(path: Path) -> None:
    # Test-only consumer, never part of the catalogue schema. The reference
    # cannot resolve until mutate_idempotent writes its normal receipt.
    with _open_write_connection(path, create=False) as connection:
        connection.execute(
            "CREATE TABLE test_proposal_claims (proposal_id TEXT PRIMARY KEY, "
            "mutation_id TEXT NOT NULL UNIQUE REFERENCES idempotency_records(mutation_id) "
            "DEFERRABLE INITIALLY DEFERRED, fact_id TEXT NOT NULL REFERENCES facts(fact_id))"
        )
        connection.commit()


def _claim_context(proposal_id: UUID) -> mutations.ProposalAcceptanceContext:
    def record(tx: _MutationTransaction, result: mutations.FactsPromoted) -> None:
        try:
            tx.execute(
                "INSERT INTO test_proposal_claims VALUES (?, ?, ?)",
                (str(proposal_id), str(tx.mutation_id), str(result.promotions[0][1])),
            )
        except sqlite3.IntegrityError as error:
            raise _deny() from error

    return _context(proposal_id, record=record)


def test_record_links_actual_publication_and_only_runs_on_fresh_success(
    tmp_path: Path,
) -> None:
    h._seed_promotable(tmp_path)
    _claims(tmp_path)
    context = _claim_context(uuid4())
    first = _accept(_authority(tmp_path), context)
    assert isinstance(first, Committed)
    replay = _accept(_authority(tmp_path), context)
    assert isinstance(replay, Replayed)
    assert replay.value == first.value
    assert replay.mutation_receipt == first.mutation_receipt
    assert h._rows(
        tmp_path, "SELECT mutation_id, fact_id FROM test_proposal_claims"
    ) == [(str(first.mutation_receipt.mutation_id), str(first.value.promotions[0][1]))]
    assert h._rows(
        tmp_path,
        "SELECT derived_from, evidence_id, trust FROM facts WHERE derived_from IS NOT NULL",
    ) == [(str(h._SOURCE_FACT_ID), str(first.value.evidence_id), "validated")]
    assert h._rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(1,)]
    assert h._audit_rows(tmp_path) == [
        ("realm", "data", "promote", "allow", "facts_promoted"),
        ("realm", "data", "promote", "allow", "idempotent_replay"),
    ]


@pytest.mark.parametrize("replay", [False, True])
def test_guard_denies_before_publication_or_replay(
    tmp_path: Path, replay: bool
) -> None:
    h._seed_promotable(tmp_path)
    proposal_id = uuid4()
    if replay:
        assert isinstance(
            _accept(_authority(tmp_path), _context(proposal_id)), Committed
        )

    def guard(tx: _GuardedTransaction) -> None:
        raise _deny()

    result = _accept(_authority(tmp_path), _context(proposal_id, guard))
    assert isinstance(result, Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2 if replay else 1,)]
    assert h._audit_rows(tmp_path)[-1][-2:] == ("deny", "proposal_conflict")


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("grant_id", [h._RETRIEVE_GRANT_ID, h._PROMOTE_GRANT_ID])
@pytest.mark.parametrize("change", ["expiry", "revocation"])
def test_locked_authority_precedes_guard_and_replay(
    tmp_path: Path, replay: bool, grant_id: UUID, change: str
) -> None:
    h._seed_catalogue(tmp_path)
    h._insert_principal(tmp_path, h._AGENT_ID, label="reviewer")
    h._insert_credential(tmp_path, h._AGENT_CREDENTIAL_ID, h._AGENT_ID)
    for identity, operation in (
        (h._RETRIEVE_GRANT_ID, GrantOperation.RETRIEVE),
        (h._PROMOTE_GRANT_ID, GrantOperation.PROMOTE),
    ):
        h._insert_grant(
            tmp_path,
            grant_id=identity,
            operations=frozenset({operation}),
            expires_at="2026-08-06T00:00:00.000000Z"
            if identity == grant_id
            else h._FUTURE_TS,
        )
    h._insert_assertion_row(tmp_path)
    h._insert_fact_row(tmp_path)
    h._insert_evidence_row(tmp_path)
    proposal_id = uuid4()
    if replay:
        assert isinstance(
            _accept(_authority(tmp_path), _context(proposal_id)), Committed
        )
    now = h._NOW

    def change_at_gate() -> None:
        nonlocal now
        if change == "expiry":
            now = datetime(2026, 8, 6, tzinfo=UTC)
        else:
            h._revoke_grant_row(tmp_path, grant_id)

    def guard(tx: _GuardedTransaction) -> None:
        pytest.fail("normal authorisation must refuse before the proposal guard")

    outcome = _accept(
        _authority(tmp_path, clock=lambda: now, before_lock=change_at_gate),
        _context(proposal_id, guard),
    )
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert h._audit_rows(tmp_path)[-1][-1] == (
        "source_retrieve_denied"
        if grant_id == h._RETRIEVE_GRANT_ID
        else "target_promote_denied"
    )
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2 if replay else 1,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM idempotency_records") == [
        (int(replay),)
    ]


def test_record_failure_rolls_back_actual_facts_evidence_outbox_and_claim(
    tmp_path: Path,
) -> None:
    h._seed_promotable(tmp_path)
    _claims(tmp_path)
    claim = _claim_context(uuid4())

    def record(tx: _MutationTransaction, result: mutations.FactsPromoted) -> None:
        claim.record(tx, result)
        assert tx.query("SELECT count(*) FROM facts") == ((2,),)
        assert tx.query("SELECT count(*) FROM evidence_records") == ((2,),)
        assert tx.query("SELECT count(*) FROM projection_outbox") == ((1,),)
        raise _deny()

    result = _accept(
        _authority(tmp_path),
        _context(claim.proposal_id, record=record),
        command=h._promote_command(evidence=h._external_reference()),
    )
    assert isinstance(result, Rejected)
    for table, count in (
        ("facts", 1),
        ("evidence_records", 1),
        ("projection_outbox", 0),
        ("evidence_outbox", 0),
        ("idempotency_records", 0),
        ("test_proposal_claims", 0),
    ):
        assert h._rows(tmp_path, f"SELECT count(*) FROM {table}") == [(count,)]
    assert h._audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "proposal_conflict")
    ]


@pytest.mark.parametrize("invalidate_at_gate", [False, True])
def test_invalidation_refuses_fresh_but_preserves_accepted_replay(
    tmp_path: Path, invalidate_at_gate: bool
) -> None:
    h._seed_promotable(tmp_path)
    _claims(tmp_path)
    context = _claim_context(uuid4())
    first = _accept(_authority(tmp_path), context)
    assert isinstance(first, Committed)
    if not invalidate_at_gate:
        h._invalidate_fact_row(tmp_path)
    replay = _accept(
        _authority(
            tmp_path,
            before_lock=(lambda: h._invalidate_fact_row(tmp_path))
            if invalidate_at_gate
            else lambda: None,
        ),
        context,
    )
    assert isinstance(replay, Replayed)
    assert replay.value == first.value
    fresh = _accept(_authority(tmp_path), _claim_context(uuid4()), key=uuid4())
    assert isinstance(fresh, Rejected)
    assert h._audit_rows(tmp_path)[-1][-1] == "source_invalidated"
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM test_proposal_claims") == [(1,)]


def test_same_key_cannot_replay_another_proposal(tmp_path: Path) -> None:
    h._seed_promotable(tmp_path)
    _claims(tmp_path)
    assert isinstance(_accept(_authority(tmp_path), _claim_context(uuid4())), Committed)
    other = _accept(_authority(tmp_path), _claim_context(uuid4()))
    assert isinstance(other, Rejected)
    assert other.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    assert h._rows(tmp_path, "SELECT count(*) FROM test_proposal_claims") == [(1,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]


@pytest.mark.parametrize(
    "field", ["fact_ids", "evidence", "target_scope", "target_classification", "reason"]
)
def test_acceptance_digest_binds_the_complete_promotion_command(
    tmp_path: Path, field: str
) -> None:
    h._seed_promotable(tmp_path)
    other_fact = uuid4()
    h._insert_fact_row(tmp_path, other_fact)
    _claims(tmp_path)
    context = _claim_context(uuid4())
    command = h._promote_command()
    assert isinstance(
        _accept(_authority(tmp_path), context, command=command), Committed
    )
    alternatives = {
        "fact_ids": replace(command, fact_ids=(other_fact,)),
        "evidence": replace(command, evidence=h._external_reference()),
        "target_scope": replace(command, target_scope=h._SOURCE_SCOPE),
        "target_classification": replace(
            command, target_classification=Classification.RESTRICTED
        ),
        "reason": replace(command, reason="another independently checked reason"),
    }
    result = _accept(_authority(tmp_path), context, command=alternatives[field])
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(3,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM test_proposal_claims") == [(1,)]


@pytest.mark.parametrize("legacy_first", [False, True])
def test_legacy_and_acceptance_have_separate_replay_domains(
    tmp_path: Path, legacy_first: bool
) -> None:
    h._seed_promotable(tmp_path)
    _claims(tmp_path)
    context = _claim_context(uuid4())
    contexts = (None, context) if legacy_first else (context, None)
    first, second = [_accept(_authority(tmp_path), item) for item in contexts]
    assert isinstance(first, Committed)
    assert isinstance(second, Committed)
    assert first.value != second.value
    assert (
        first.mutation_receipt.command_digest != second.mutation_receipt.command_digest
    )
    assert h._rows(
        tmp_path, "SELECT operation FROM idempotency_records ORDER BY operation"
    ) == [("memory-proposal-accept",), ("promote",)]
    assert h._rows(tmp_path, "SELECT count(*) FROM test_proposal_claims") == [(1,)]
    for item in contexts:
        assert isinstance(_accept(_authority(tmp_path), item), Replayed)


def test_two_principals_cannot_claim_one_proposal(tmp_path: Path) -> None:
    h._seed_promotable(tmp_path)
    h._seed_outsider(tmp_path)
    h._insert_grant(
        tmp_path,
        grant_id=uuid4(),
        principal_id=h._OUTSIDER_ID,
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.PROMOTE}),
    )
    _claims(tmp_path)
    proposal_id = uuid4()
    barrier = threading.Barrier(2)

    def meet_at_gate() -> None:
        barrier.wait(timeout=10)

    def accept(actor: Actor) -> MutationOutcome[mutations.FactsPromoted]:
        return _accept(
            _authority(tmp_path, before_lock=meet_at_gate),
            _claim_context(proposal_id),
            actor=actor,
            key=uuid4(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, (h._agent_actor(), h._outsider_actor())))
    assert sum(isinstance(result, Committed) for result in results) == 1
    assert sum(isinstance(result, Rejected) for result in results) == 1
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(1,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM test_proposal_claims") == [(1,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM idempotency_records") == [(1,)]


@pytest.mark.parametrize(
    "field,value", [("proposal_id", "not-a-uuid"), ("guard", None), ("record", None)]
)
def test_malformed_context_refused_locally_before_any_write(
    tmp_path: Path, field: str, value: object
) -> None:
    h._seed_promotable(tmp_path)
    context = _context(uuid4())
    object.__setattr__(context, field, value)
    with pytest.raises(ValueError, match="acceptance"):
        _accept(_authority(tmp_path), context)
    assert h._audit_rows(tmp_path) == []
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]


@pytest.mark.parametrize("constraint", ["source", "evidence", "target"])
def test_acceptance_cannot_bypass_normal_classification_constraints(
    tmp_path: Path, constraint: str
) -> None:
    h._seed_promotable(
        tmp_path,
        classification=Classification.RESTRICTED
        if constraint == "source"
        else Classification.INTERNAL,
        evidence_classification=Classification.RESTRICTED
        if constraint == "evidence"
        else Classification.INTERNAL,
        read_clearance=Classification.INTERNAL,
        write_classifications=frozenset({Classification.PUBLIC})
        if constraint == "target"
        else h._ALL_CLASSIFICATIONS,
    )
    result = _accept(_authority(tmp_path), _context(uuid4()))
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
