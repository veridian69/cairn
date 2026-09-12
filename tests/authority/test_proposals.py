"""Proposal authority, real SQLite persistence and normal promotion fencing."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import test_mutations as h

from cairn.authority.credentials import GrantOperation
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import Classification
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import Committed, Rejected, Replayed
from cairn.screening import SecretScreen


def api() -> Any:
    assert importlib.util.find_spec("cairn.authority.proposals") is not None, (
        "proposal authority is not implemented"
    )
    return importlib.import_module("cairn.authority.proposals")


def service(
    path: Path,
    before_lock: Callable[[], None] = lambda: None,
    clock: Callable[[], datetime] = lambda: h._NOW,
) -> Any:
    tx = h._GrantRaceTransactions(
        path,
        writer_gate=threading.Lock(),
        clock=clock,
        uuid_factory=uuid4,
        interfere=lambda path: before_lock(),
    )
    authority = CairnAuthority(
        path,
        tx,
        clock=clock,
        uuid_factory=uuid4,
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )
    return api().CairnProposals(path, tx, clock, SecretScreen(), authority)


def seed(path: Path) -> None:
    h._seed_promotable(path)
    h._insert_grant(path, grant_id=h._INGEST_GRANT_ID)


def proposal(**changes: Any) -> Any:
    return api().ProposeMemory(
        **dict(
            scope=h._SOURCE_SCOPE,
            proposal_id=uuid4(),
            source_fact_id=h._SOURCE_FACT_ID,
            target_scope=h._PARENT_SCOPE,
            reason="A reusable finding",
            **changes,
        )
    )


def invoke(
    s: Any,
    method: str,
    command: Any,
    *,
    key: UUID | None = None,
    actor: Actor | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"correlation_id": h._CORRELATION_ID}
    if method not in {"read", "list"}:
        kwargs["idempotency_key"] = key or uuid4()
    return getattr(s, method)(actor or h._agent_actor(), command, **kwargs)


def accept(p: Any, **changes: Any) -> Any:
    return replace(
        api().AcceptProposal(
            p.scope, p.proposal_id, h._NAMED_EVIDENCE_ID, Classification.INTERNAL
        ),
        **changes,
    )


@pytest.mark.parametrize("method", ["propose", "accept", "reject"])
@pytest.mark.parametrize(
    "key",
    [
        *(UUID(f"22222222-2222-5222-{n}222-222222222222") for n in "01234567cdef"),
        UUID(int=0),
        UUID(int=(1 << 128) - 1),
        "bad-key",
        None,
        1,
    ],
)
def test_i27_direct_key_refusal_is_safe_and_audited(
    tmp_path: Path, method: str, key: Any
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    if method != "propose":
        assert isinstance(invoke(s, "propose", p), Committed)
    command = (
        p
        if method == "propose"
        else accept(p)
        if method == "accept"
        else api().RejectProposal(p.scope, p.proposal_id, "No")
    )
    tables = [
        row[0]
        for row in h._rows(
            tmp_path, "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not str(row[0]).startswith("audit_") and row[0] != "sqlite_sequence"
    ]
    before = {name: h._rows(tmp_path, f'SELECT * FROM "{name}"') for name in tables}
    audit_count = len(h._audit_rows(tmp_path))
    result = getattr(s, method)(
        h._agent_actor(), command, idempotency_key=key, correlation_id=h._CORRELATION_ID
    )
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "invalid_request"
    assert {
        name: h._rows(tmp_path, f'SELECT * FROM "{name}"') for name in tables
    } == before
    assert len(h._audit_rows(tmp_path)) == audit_count + 1
    events = h._rows(
        tmp_path, "SELECT canonical_event FROM audit_events WHERE outcome='deny'"
    )
    encoded = events[-1][0]
    assert isinstance(encoded, bytes)
    event = json.loads(encoded)
    assert event["chain_kind"] == "instance"
    assert event["idempotency_key"] is None
    assert event["mutation_id"] is None
    assert event["command_digest"] is None
    assert event["safe_request_fingerprint"] is None
    assert event["action_code"] == (
        "memory-propose" if method == "propose" else f"memory-proposal-{method}"
    )
    assert "bad-key" not in json.dumps(event)


@pytest.mark.parametrize("method", ["propose", "accept", "reject"])
@pytest.mark.parametrize("version", "025f")
@pytest.mark.parametrize("variant", "89ab")
def test_i27_direct_rfc_keys_commit_and_replay_without_version_pin(
    tmp_path: Path, method: str, version: str, variant: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    if method != "propose":
        assert isinstance(invoke(s, "propose", p), Committed)
    command = (
        p
        if method == "propose"
        else accept(p)
        if method == "accept"
        else api().RejectProposal(p.scope, p.proposal_id, "No")
    )
    key = UUID(f"22222222-2222-{version}222-{variant}222-222222222222")
    committed = invoke(s, method, command, key=key)
    replay = invoke(s, method, command, key=key)
    assert isinstance(committed, Committed) and isinstance(replay, Replayed)
    assert committed.mutation_receipt == replay.mutation_receipt
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [
        (2 if method == "accept" else 1,)
    ]


def read(s: Any, p: Any, actor: Actor | None = None) -> Any:
    return invoke(s, "read", api().ReadProposal(p.scope, p.proposal_id), actor=actor)


def reviewer(
    path: Path,
    *,
    clearance: Classification = Classification.RESTRICTED,
    segments: Any = h._PARENT_SCOPE.segments,
) -> Actor:
    actor = Actor(uuid4(), uuid4())
    h._insert_principal(
        path, actor.principal_id, label="reviewer-" + actor.principal_id.hex
    )
    h._insert_credential(path, actor.credential_id, actor.principal_id)
    h._insert_grant(
        path,
        grant_id=uuid4(),
        principal_id=actor.principal_id,
        segments=segments,
        read_clearance=clearance,
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.PROMOTE}),
    )
    return actor


def test_worker_proposes_without_publication_or_target_rights(tmp_path: Path) -> None:
    seed(tmp_path)
    h._revoke_grant_row(tmp_path, h._PROMOTE_GRANT_ID)
    p, s = proposal(), service(tmp_path)
    source_before = h._rows(tmp_path, "SELECT * FROM facts")
    result = invoke(s, "propose", p)
    assert isinstance(result, Committed)
    assert result.value.proposal_id == p.proposal_id
    assert h._rows(tmp_path, "SELECT * FROM facts") == source_before
    view = read(s, p)
    assert view.classification is Classification.INTERNAL
    assert view.proposed_by == h._AGENT_ID and view.state == "pending"
    assert isinstance(invoke(s, "accept", accept(p)), Rejected)
    assert isinstance(
        invoke(s, "reject", api().RejectProposal(p.scope, p.proposal_id, "No")),
        Rejected,
    )
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposal_decisions") == [(0,)]


def test_acceptance_replay_has_real_receipt_and_preserves_source(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    before = h._rows(tmp_path, "SELECT * FROM memory_proposals")
    first = invoke(s, "accept", accept(p), key=key)
    assert isinstance(first, Committed)
    again = invoke(service(tmp_path), "accept", accept(p), key=key)
    assert isinstance(again, Replayed)
    assert (
        again.value == first.value and again.mutation_receipt == first.mutation_receipt
    )
    assert h._rows(tmp_path, "SELECT * FROM memory_proposals") == before
    view = read(s, p)
    assert view.state == "accepted"
    assert view.decision.mutation_id == first.mutation_receipt.mutation_id
    assert view.decision.promoted_fact_id == first.value.promotions[0][1]
    assert view.decision.evidence_id == first.value.evidence_id
    assert h._rows(tmp_path, "PRAGMA foreign_key_check") == []
    assert h._rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(1,)]


@pytest.mark.parametrize("other", ["accept", "reject"])
def test_distinct_reviewers_race_one_atomic_decision(
    tmp_path: Path, other: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    second = reviewer(tmp_path)
    barrier = threading.Barrier(2)

    def synchronise() -> None:
        barrier.wait(timeout=10)

    command = (
        accept(p)
        if other == "accept"
        else api().RejectProposal(p.scope, p.proposal_id, "Not reusable")
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                invoke,
                service(tmp_path, synchronise),
                "accept",
                accept(p),
            ),
            pool.submit(
                invoke,
                service(tmp_path, synchronise),
                other,
                command,
                actor=second,
            ),
        ]
        results = [f.result(timeout=20) for f in futures]
    assert sum(isinstance(r, Committed) for r in results) == 1
    assert sum(isinstance(r, Rejected) for r in results) == 1
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposal_decisions") == [(1,)]
    accepted = read(s, p).state == "accepted"
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(1 + int(accepted),)]
    assert h._rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [
        (int(accepted),)
    ]


@pytest.mark.parametrize("grant", [h._RETRIEVE_GRANT_ID, h._INGEST_GRANT_ID])
@pytest.mark.parametrize("replay", [False, True])
def test_proposal_current_grants_inside_writer_before_replay(
    tmp_path: Path, grant: UUID, replay: bool
) -> None:
    seed(tmp_path)
    p, key = proposal(), uuid4()
    if replay:
        assert isinstance(invoke(service(tmp_path), "propose", p, key=key), Committed)
    result = invoke(
        service(tmp_path, lambda: h._revoke_grant_row(tmp_path, grant)),
        "propose",
        p,
        key=key,
    )
    assert isinstance(result, Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposals") == [
        (int(replay),)
    ]


@pytest.mark.parametrize("method", ["accept", "reject"])
@pytest.mark.parametrize("replay", [False, True])
def test_decision_current_target_grant_inside_writer(
    tmp_path: Path, method: str, replay: bool
) -> None:
    seed(tmp_path)
    p, key = proposal(), uuid4()
    assert isinstance(invoke(service(tmp_path), "propose", p), Committed)
    command = (
        accept(p)
        if method == "accept"
        else api().RejectProposal(p.scope, p.proposal_id, "Not reusable")
    )
    if replay:
        assert isinstance(
            invoke(service(tmp_path), method, command, key=key), Committed
        )
    result = invoke(
        service(tmp_path, lambda: h._revoke_grant_row(tmp_path, h._PROMOTE_GRANT_ID)),
        method,
        command,
        key=key,
    )
    assert isinstance(result, Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposal_decisions") == [
        (int(replay),)
    ]


def test_invalidation_refuses_fresh_but_preserves_bound_acceptance_replay(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, q, s, key = proposal(), proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(invoke(s, "propose", q), Committed)
    first = invoke(s, "accept", accept(p), key=key)
    assert isinstance(first, Committed)
    h._invalidate_fact_row(tmp_path)
    replay = invoke(s, "accept", accept(p), key=key)
    assert isinstance(replay, Replayed) and replay.value == first.value
    assert isinstance(invoke(s, "accept", accept(q)), Rejected)
    assert isinstance(invoke(s, "accept", accept(p)), Rejected)
    assert isinstance(
        invoke(s, "accept", accept(p), key=key, actor=reviewer(tmp_path)), Rejected
    )


def test_same_key_cross_proposals_and_legacy_domains(tmp_path: Path) -> None:
    seed(tmp_path)
    p, q, s, key = proposal(), proposal(), service(tmp_path), uuid4()
    for command in (p, q):
        assert isinstance(invoke(s, "propose", command), Committed)
    plain = h._promote_command(
        target_scope=p.target_scope,
        target_classification=Classification.INTERNAL,
        reason=p.reason,
    )
    legacy = h._promote(h._authority(tmp_path), plain, idempotency_key=key)
    assert isinstance(legacy, Committed)
    first = invoke(s, "accept", accept(p), key=key)
    assert isinstance(first, Committed) and first.value != legacy.value
    assert isinstance(invoke(s, "accept", accept(q), key=key), Rejected)
    assert read(s, q).state == "pending"
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(3,)]


@pytest.mark.parametrize(
    "target", [h._SIBLING_SCOPE, h._CHILD_SCOPE, h._CROSS_REALM_SCOPE]
)
def test_proposal_target_must_be_legal_ancestor(tmp_path: Path, target: Any) -> None:
    seed(tmp_path)
    assert isinstance(
        invoke(service(tmp_path), "propose", replace(proposal(), target_scope=target)),
        Rejected,
    )
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposals") == [(0,)]


@pytest.mark.parametrize(
    "reason",
    ["", "é" * 2049, "-----BEGIN RSA PRIVATE KEY-----"],
    ids=["empty", "bytes", "secret"],
)
@pytest.mark.parametrize("method", ["propose", "reject"])
def test_screening_and_byte_limits_before_custody(
    tmp_path: Path, reason: str, method: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    if method == "propose":
        command = replace(p, reason=reason)
    else:
        assert isinstance(invoke(s, "propose", p), Committed)
        command = api().RejectProposal(p.scope, p.proposal_id, reason)
    assert isinstance(invoke(s, method, command), Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposal_decisions") == [(0,)]


def test_rejection_is_attributed_replayable_and_does_not_invalidate(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    command = api().RejectProposal(
        p.scope, p.proposal_id, "Evidence does not support reuse"
    )
    first = invoke(s, "reject", command, key=key)
    assert isinstance(first, Committed)
    assert isinstance(invoke(s, "reject", command, key=key), Replayed)
    assert isinstance(invoke(s, "accept", accept(p)), Rejected)
    assert isinstance(
        invoke(s, "reject", replace(command, reason="Changed"), key=key), Rejected
    )
    view = read(s, p)
    assert (
        view.decision.decided_by == h._AGENT_ID
        and view.decision.reason == command.reason
    )
    assert view.decision.promoted_fact_id is None
    assert h._rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(0,)]


def test_explicit_listing_filters_classification_before_cursor_and_has_no_descendants(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    reader = reviewer(tmp_path, clearance=Classification.INTERNAL)
    listing = api().ListProposals(p.scope, limit=1)
    before = invoke(s, "list", listing, actor=reader)
    h._insert_fact_row(
        tmp_path, h._SECOND_FACT_ID, classification=Classification.RESTRICTED
    )
    hidden = replace(proposal(), source_fact_id=h._SECOND_FACT_ID)
    assert isinstance(invoke(s, "propose", hidden), Committed)
    after = invoke(s, "list", listing, actor=reader)
    assert after == before and after.next_cursor is None
    assert isinstance(read(s, hidden, reader), Rejected)
    missing = read(s, replace(hidden, proposal_id=uuid4()), reader)
    assert missing.failure == read(s, hidden, reader).failure
    parent = invoke(s, "list", api().ListProposals(h._PARENT_SCOPE), actor=reader)
    assert parent.items == ()
    assert isinstance(
        invoke(s, "list", replace(listing, after=hidden.proposal_id), actor=reader),
        Rejected,
    )
    visible = proposal()
    assert isinstance(invoke(s, "propose", visible), Committed)
    first = invoke(s, "list", listing, actor=reader)
    assert first.next_cursor == first.items[-1].proposal_id
    second = invoke(s, "list", replace(listing, after=first.next_cursor), actor=reader)
    assert {first.items[0].proposal_id, second.items[0].proposal_id} == {
        p.proposal_id,
        visible.proposal_id,
    }


@pytest.mark.parametrize("change", ["evidence", "classification", "scope"])
def test_acceptance_boundaries_and_changed_command_replay(
    tmp_path: Path, change: str
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(invoke(s, "accept", accept(p), key=key), Committed)
    variants: dict[str, dict[str, Any]] = {
        "evidence": {"evidence_id": h._UNKNOWN_EVIDENCE_ID},
        "classification": {"target_classification": Classification.PUBLIC},
        "scope": {"scope": h._PARENT_SCOPE},
    }
    assert isinstance(
        invoke(s, "accept", accept(p, **variants[change]), key=key), Rejected
    )
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]


def test_proposal_records_are_append_only(tmp_path: Path) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(
        invoke(s, "reject", api().RejectProposal(p.scope, p.proposal_id, "No")),
        Committed,
    )
    for table in ("memory_proposals", "memory_proposal_decisions"):
        with _open_write_connection(tmp_path, create=False) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET proposal_id = proposal_id")


def test_hidden_evidence_and_unknown_evidence_have_identical_denials_and_audit(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    hidden = uuid4()
    h._insert_evidence_row(tmp_path, hidden, scope=h._SIBLING_SCOPE)
    denied = invoke(s, "accept", accept(p, evidence_id=hidden))
    missing = invoke(s, "accept", accept(p, evidence_id=uuid4()))
    assert isinstance(denied, Rejected) and isinstance(missing, Rejected)
    assert denied.failure == missing.failure
    audits = [row for row in h._audit_rows(tmp_path) if row[-2] == "deny"]
    assert audits[-1] == audits[-2]
    assert audits[-1][0] == "instance"


def test_source_only_reader_gets_no_hidden_publication_reference(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(
        invoke(s, "accept", accept(p, target_classification=Classification.RESTRICTED)),
        Committed,
    )
    source_reader = reviewer(
        tmp_path, clearance=Classification.INTERNAL, segments=h._SOURCE_SCOPE.segments
    )
    view = read(s, p, source_reader)
    assert view.state == "accepted" and view.decision.promoted_fact_id is None
    assert view.decision.evidence_id == h._NAMED_EVIDENCE_ID
    public_reader = reviewer(tmp_path, segments=())
    # Retrieve is a prefix grant. To model a parent-only reader while the
    # origin is private, use source restricted / target restricted separately.
    assert isinstance(
        invoke(
            s,
            "read",
            api().ReadProposal(h._PARENT_SCOPE, p.proposal_id),
            actor=public_reader,
        ),
        Rejected,
    )


def test_failed_decision_insert_rolls_back_normal_publication(tmp_path: Path) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(
            "CREATE TRIGGER test_refuse_decision BEFORE INSERT ON memory_proposal_decisions BEGIN SELECT RAISE(ABORT, 'synthetic_failure'); END"
        )
        connection.commit()
    assert isinstance(invoke(s, "accept", accept(p)), Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]
    assert h._rows(tmp_path, "SELECT count(*) FROM idempotency_records") == [(1,)]
    assert read(s, p).state == "pending"


@pytest.mark.parametrize("identity", ["bad", None])
def test_malformed_proposal_identity_is_audited_before_sqlite(
    tmp_path: Path, identity: Any
) -> None:
    seed(tmp_path)
    result = invoke(
        service(tmp_path), "propose", replace(proposal(), proposal_id=identity)
    )
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "invalid_request"


@pytest.mark.parametrize("method", ["propose", "accept", "reject"])
def test_expiry_inside_writer_denies_replay(tmp_path: Path, method: str) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    if method == "propose":
        command = p
    else:
        assert isinstance(invoke(s, "propose", p), Committed)
        command = (
            accept(p)
            if method == "accept"
            else api().RejectProposal(p.scope, p.proposal_id, "No")
        )
    first = invoke(s, method, command, key=key)
    assert isinstance(first, Committed)
    now = h._NOW

    def expire() -> None:
        nonlocal now
        now = datetime(2100, 1, 1, tzinfo=h._NOW.tzinfo)

    result = invoke(service(tmp_path, expire, lambda: now), method, command, key=key)
    assert isinstance(result, Rejected)


@pytest.mark.parametrize("field", ["evidence", "classification"])
def test_accepted_replay_refuses_otherwise_valid_changed_command(
    tmp_path: Path, field: str
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(invoke(s, "accept", accept(p), key=key), Committed)
    evidence_id = uuid4()
    h._insert_evidence_row(tmp_path, evidence_id)
    command = (
        accept(p, evidence_id=evidence_id)
        if field == "evidence"
        else accept(p, target_classification=Classification.RESTRICTED)
    )
    assert isinstance(invoke(s, "accept", command, key=key), Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]


def test_sibling_proposals_do_not_change_explicit_source_page(tmp_path: Path) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    actor = reviewer(tmp_path)
    command = api().ListProposals(p.scope, limit=1)
    before = invoke(s, "list", command, actor=actor)
    h._insert_grant(
        tmp_path,
        grant_id=uuid4(),
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.INGEST}),
    )
    assertion, fact = uuid4(), uuid4()
    h._insert_assertion_row(tmp_path, assertion_id=assertion, scope=h._SIBLING_SCOPE)
    h._insert_fact_row(tmp_path, fact, scope=h._SIBLING_SCOPE, assertion_id=assertion)
    sibling = replace(
        proposal(),
        scope=h._SIBLING_SCOPE,
        source_fact_id=fact,
        target_scope=h._ROOT_SCOPE,
    )
    assert isinstance(invoke(s, "propose", sibling), Committed)
    assert invoke(s, "list", command, actor=actor) == before
    hidden = invoke(
        s, "read", api().ReadProposal(p.scope, sibling.proposal_id), actor=actor
    )
    absent = invoke(s, "read", api().ReadProposal(p.scope, uuid4()), actor=actor)
    assert isinstance(hidden, Rejected) and isinstance(absent, Rejected)
    assert hidden.failure == absent.failure


@pytest.mark.parametrize("method", ["propose", "reject"])
def test_source_classification_requires_writable_grant(
    tmp_path: Path, method: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    if method == "propose":
        h._revoke_grant_row(tmp_path, h._INGEST_GRANT_ID)
        operation = GrantOperation.INGEST
        command = p
    else:
        assert isinstance(invoke(s, "propose", p), Committed)
        h._revoke_grant_row(tmp_path, h._PROMOTE_GRANT_ID)
        operation = GrantOperation.PROMOTE
        command = api().RejectProposal(p.scope, p.proposal_id, "No")
    h._insert_grant(
        tmp_path,
        grant_id=uuid4(),
        operations=frozenset({operation}),
        write_classifications=frozenset({Classification.PUBLIC}),
    )
    assert isinstance(invoke(s, method, command), Rejected)


def test_invalidation_inside_publication_writer_keeps_proposal_pending(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p = proposal()
    assert isinstance(invoke(service(tmp_path), "propose", p), Committed)
    result = invoke(
        service(tmp_path, lambda: h._invalidate_fact_row(tmp_path)), "accept", accept(p)
    )
    assert isinstance(result, Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert read(service(tmp_path), p).state == "pending"


@pytest.mark.parametrize("limit", [0, -1, 101, True])
def test_list_budget_is_bounded(tmp_path: Path, limit: int) -> None:
    seed(tmp_path)
    result = invoke(
        service(tmp_path), "list", api().ListProposals(h._SOURCE_SCOPE, limit=limit)
    )
    assert isinstance(result, Rejected)


def test_proposal_creation_replay_binds_reason_identity_and_receipt(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    first = invoke(s, "propose", p, key=key)
    assert isinstance(first, Committed)
    replay = invoke(service(tmp_path), "propose", p, key=key)
    assert (
        isinstance(replay, Replayed)
        and replay.mutation_receipt == first.mutation_receipt
    )
    assert isinstance(
        invoke(s, "propose", replace(p, reason="Changed"), key=key), Rejected
    )
    assert isinstance(
        invoke(s, "propose", replace(p, proposal_id=uuid4()), key=key), Rejected
    )
    assert isinstance(invoke(s, "propose", p), Rejected)
    assert h._rows(tmp_path, "SELECT count(*) FROM memory_proposals") == [(1,)]


@pytest.mark.parametrize("table", ["memory_proposals", "memory_proposal_decisions"])
@pytest.mark.parametrize("method", ["read", "list", "accept", "reject"])
def test_malformed_stored_timestamp_is_an_opaque_audited_refusal(
    tmp_path: Path, table: str, method: str
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    assert isinstance(invoke(s, "propose", p), Committed)
    if table == "memory_proposal_decisions":
        decision_method = "reject" if method == "reject" else "accept"
        decision = (
            api().RejectProposal(p.scope, p.proposal_id, "No")
            if decision_method == "reject"
            else accept(p)
        )
        assert isinstance(invoke(s, decision_method, decision, key=key), Committed)
    malformed = "2026-13-45T99:99:99.000000Z"
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(f"DROP TRIGGER {table}_no_update")
        connection.execute(
            f"UPDATE {table} SET recorded_at=? WHERE proposal_id=?",
            (malformed, str(p.proposal_id)),
        )
        connection.commit()
    commands = {
        "read": api().ReadProposal(p.scope, p.proposal_id),
        "list": api().ListProposals(p.scope),
        "accept": accept(p),
        "reject": api().RejectProposal(p.scope, p.proposal_id, "No"),
    }
    before_audit = len(h._audit_rows(tmp_path))
    before_records = h._rows(tmp_path, "SELECT count(*) FROM idempotency_records")
    result = invoke(s, method, commands[method], key=key)
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
    assert len(h._audit_rows(tmp_path)) == before_audit + 1
    assert (
        h._rows(tmp_path, "SELECT count(*) FROM idempotency_records") == before_records
    )
    denials = [row for row in h._audit_rows(tmp_path) if row[-2] == "deny"]
    assert denials == [
        (
            "instance",
            "data",
            f"memory-proposal-{method}",
            "deny",
            "proposal_authorisation_denied",
        )
    ]
    assert malformed not in repr(result)


@pytest.mark.parametrize("method", ["read", "list"])
def test_source_only_grant_reads_inherited_ancestor_publication_and_evidence(
    tmp_path: Path, method: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    evidence = uuid4()
    h._insert_evidence_row(tmp_path, evidence, scope=h._PARENT_SCOPE)
    assert isinstance(invoke(s, "propose", p), Committed)
    published = invoke(s, "accept", accept(p, evidence_id=evidence))
    assert isinstance(published, Committed)
    actor = reviewer(tmp_path, segments=p.scope.segments)
    command = (
        api().ReadProposal(p.scope, p.proposal_id)
        if method == "read"
        else api().ListProposals(p.scope)
    )
    result = invoke(s, method, command, actor=actor)
    view = result if method == "read" else result.items[0]
    assert view.decision.promoted_fact_id == published.value.promotions[0][1]
    assert view.decision.evidence_id == evidence


@pytest.mark.parametrize("method", ["read", "list"])
def test_inherited_references_still_require_source_clearance(
    tmp_path: Path, method: str
) -> None:
    seed(tmp_path)
    p, s, evidence = proposal(), service(tmp_path), uuid4()
    h._insert_evidence_row(
        tmp_path,
        evidence,
        scope=h._PARENT_SCOPE,
        classification=Classification.RESTRICTED,
    )
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(
        invoke(
            s,
            "accept",
            accept(
                p, evidence_id=evidence, target_classification=Classification.RESTRICTED
            ),
        ),
        Committed,
    )
    actor = reviewer(
        tmp_path, clearance=Classification.INTERNAL, segments=p.scope.segments
    )
    command = (
        api().ReadProposal(p.scope, p.proposal_id)
        if method == "read"
        else api().ListProposals(p.scope)
    )
    result = invoke(s, method, command, actor=actor)
    view = result if method == "read" else result.items[0]
    assert view.decision.evidence_id is None
    assert view.decision.promoted_fact_id is None


@pytest.mark.parametrize(
    "corruption",
    [
        "proposal_uuid",
        "target_shape",
        "target_json",
        "source_enum",
        "decision_uuid",
        "decision_enum",
    ],
)
def test_stored_decode_families_have_one_opaque_list_refusal(
    tmp_path: Path, corruption: str
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    if corruption.startswith("decision"):
        assert isinstance(invoke(s, "accept", accept(p)), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        # proposal_uuid and target_shape satisfy the real schema checks. The
        # other cases model corrupt storage that escaped schema enforcement.
        if corruption not in ("proposal_uuid", "target_shape"):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("DROP TRIGGER memory_proposals_no_update")
        connection.execute("DROP TRIGGER memory_proposal_decisions_no_update")
        if corruption == "proposal_uuid":
            connection.execute("UPDATE memory_proposals SET proposal_id=?", ("x" * 36,))
        elif corruption == "target_shape":
            connection.execute(
                "UPDATE memory_proposals SET target_segments=?",
                ('[{"kind":7,"id":null}]',),
            )
        elif corruption == "target_json":
            connection.execute(
                "UPDATE memory_proposals SET target_segments=?", ("[broken",)
            )
        elif corruption == "source_enum":
            connection.execute("DROP TRIGGER trg_facts_no_update")
            connection.execute("UPDATE facts SET trust='unknown'")
        elif corruption == "decision_uuid":
            connection.execute(
                "UPDATE memory_proposal_decisions SET evidence_id=?", ("x" * 36,)
            )
        else:
            connection.execute("UPDATE memory_proposal_decisions SET state='unknown'")
        connection.commit()
        if corruption in ("proposal_uuid", "target_shape"):
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = len(h._audit_rows(tmp_path))
    result = invoke(s, "list", api().ListProposals(p.scope))
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
    assert len(h._audit_rows(tmp_path)) == before + 1
    assert [r for r in h._audit_rows(tmp_path) if r[-2] == "deny"] == [
        (
            "instance",
            "data",
            "memory-proposal-list",
            "deny",
            "proposal_authorisation_denied",
        )
    ]


@pytest.mark.parametrize(
    "error_type",
    [ValueError, TypeError, KeyError, IndexError, AttributeError, AssertionError],
)
def test_stored_decode_guard_does_not_swallow_unrelated_snapshot_bugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)

    def broken_snapshot(*args: Any, **kwargs: Any) -> None:
        raise error_type("unrelated implementation fault")

    monkeypatch.setattr(s, "_snapshot", broken_snapshot)
    with pytest.raises(error_type, match="unrelated implementation fault"):
        read(s, p)


@pytest.mark.parametrize(
    "method,shape",
    [
        (method, shape)
        for method in ("propose", "accept", "reject")
        for shape in ("missing", "container", "uuid_type")
    ]
    + [("accept", "empty_promotions")],
)
def test_malformed_stored_replay_payload_is_opaque_and_audited(
    tmp_path: Path, method: str, shape: str
) -> None:
    seed(tmp_path)
    p, s, key = proposal(), service(tmp_path), uuid4()
    if method != "propose":
        assert isinstance(invoke(s, "propose", p), Committed)
    command = {
        "propose": p,
        "accept": accept(p),
        "reject": api().RejectProposal(p.scope, p.proposal_id, "No"),
    }[method]
    first = invoke(s, method, command, key=key)
    assert isinstance(first, Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        row = connection.execute(
            "SELECT result_bytes FROM idempotency_records WHERE mutation_id=?",
            (str(first.mutation_receipt.mutation_id),),
        ).fetchone()
        payload = json.loads(row[0])
        if shape == "missing":
            payload = {}
        elif shape == "container":
            payload = []
        elif shape == "empty_promotions":
            payload["result"]["promotions"] = []
        elif method == "accept":
            payload["result"]["evidence_id"] = []
        else:
            payload["proposal_id"] = []
        encoded = json.dumps(payload).encode()
        connection.execute("DROP TRIGGER trg_idempotency_records_no_update")
        connection.execute(
            "UPDATE idempotency_records SET result_bytes=?, result_digest=? WHERE mutation_id=?",
            (
                encoded,
                hashlib.sha256(encoded).digest(),
                str(first.mutation_receipt.mutation_id),
            ),
        )
        connection.commit()
    before = len(h._audit_rows(tmp_path))
    result = invoke(s, method, command, key=key)
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
    assert len(h._audit_rows(tmp_path)) == before + 1


@pytest.mark.parametrize("method", ["read", "list"])
@pytest.mark.parametrize("reference_scope", [h._SIBLING_SCOPE, h._CHILD_SCOPE])
def test_reference_disclosure_never_widens_explicit_source_closure(
    tmp_path: Path, method: str, reference_scope: Any
) -> None:
    seed(tmp_path)
    p, s = proposal(), service(tmp_path)
    assert isinstance(invoke(s, "propose", p), Committed)
    assert isinstance(invoke(s, "accept", accept(p)), Committed)
    evidence, fact = uuid4(), uuid4()
    h._insert_evidence_row(tmp_path, evidence, scope=reference_scope)
    h._insert_fact_row(tmp_path, fact, scope=reference_scope)
    # Corrupted-but-SQL-valid references must not widen this source's view,
    # even for a reader with broad realm authority. Live history now refuses
    # a decision that disagrees with its actual receipt/publication (F2).
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("DROP TRIGGER memory_proposal_decisions_no_update")
        connection.execute(
            "UPDATE memory_proposal_decisions SET evidence_id=?, promoted_fact_id=? WHERE proposal_id=?",
            (str(evidence), str(fact), str(p.proposal_id)),
        )
        connection.commit()
    actor = reviewer(tmp_path, segments=())
    command = (
        api().ReadProposal(p.scope, p.proposal_id)
        if method == "read"
        else api().ListProposals(p.scope)
    )
    result = invoke(s, method, command, actor=actor)
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
