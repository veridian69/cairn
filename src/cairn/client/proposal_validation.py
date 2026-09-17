"""Strict proposal decoding, request binding and finite public response bounds."""

import hashlib
import json
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from cairn.authority.custody import validate_reason
from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.catalogue.sqlite import canonical_timestamp, parse_timestamp
from cairn.client.proposal_types import (
    FactsPromoted,
    ProposalDecision,
    ProposalMutation,
    ProposalPage,
    ProposalRecorded,
    ProposalSnapshot,
)
from cairn.client.types import freeze_object
from cairn.client.validation import _object
from cairn.transports.memory.proposal_models import (
    ProposalPageBody,
    ProposalSnapshotBody,
)

# A scope: 63 ASCII realm chars, 16 segments with 63 kind/255 identifier
# chars, and <=1024 JSON punctuation/spacing bytes. Two scopes per snapshot.
# Two reasons each <=4096 UTF-8 bytes, each byte needs <=6 JSON escape bytes.
# <=9 UUIDs, two 27-byte times, closed enums/bools/keys fit in 4096 bytes.
# This deliberately includes both reasons even though acceptance has only one.
PROPOSAL_SCOPE_BYTES = 63 + 16 * (63 + 255) + 1024
PROPOSAL_ITEM_BYTES = 2 * PROPOSAL_SCOPE_BYTES + 2 * 4096 * 6 + 4096
PROPOSAL_PAGE_BYTES = 100 * (PROPOSAL_ITEM_BYTES + 1) + 1024
PROPOSAL_MUTATION_BYTES = 4096


def _command_scope(scope: Scope) -> dict[str, object]:
    return {
        "realm": scope.realm,
        "segments": [{"kind": s.kind, "id": s.identifier} for s in scope.segments],
    }


def _command_digest(document: dict[str, object]) -> bytes:
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).digest()


def recorded_digest(
    scope: Scope,
    proposal_id: UUID,
    reason: str,
    *,
    source_fact_id: UUID | None = None,
    target_scope: Scope | None = None,
) -> bytes:
    """Reconstruct the accepted proposal command domain, never a server call."""
    document: dict[str, object] = {
        "operation": "memory-proposal-reject"
        if source_fact_id is None
        else "memory-propose",
        "scope": _command_scope(scope),
        "proposal_id": str(proposal_id),
        "reason": reason,
    }
    if source_fact_id is not None:
        assert target_scope is not None
        document.update(
            source_fact_id=str(source_fact_id),
            target_scope=_command_scope(target_scope),
        )
    return _command_digest(document)


def acceptance_digest(
    known: ProposalSnapshot, evidence_id: UUID, classification: Classification
) -> bytes:
    """Bind the actual single-source promotion and proposal-specific domain."""
    promotion = _command_digest(
        {
            "command": "promote",
            "schema": "cairn.authority/v1",
            "fact_ids": [str(known.source_fact_id)],
            "evidence": {"evidence_id": str(evidence_id)},
            "target_scope": _command_scope(known.target_scope),
            "target_classification": classification.value,
            "reason": known.reason,
        }
    )
    return _command_digest(
        {
            "command": "memory-proposal-accept",
            "proposal_id": str(known.proposal_id),
            "promotion_digest": promotion.hex(),
        }
    )


def identity(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("invalid_identity")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("invalid_identity")
    return result


def scope_value(value: object) -> Scope:
    body = _object(value, frozenset({"realm", "segments"}))
    if type(body["realm"]) is not str or type(body["segments"]) is not list:
        raise ValueError("invalid_scope")
    segments = []
    for raw in cast(list[object], body["segments"]):
        segment = _object(raw, frozenset({"kind", "identifier"}))
        segments.append(
            ScopeSegment(cast(str, segment["kind"]), cast(str, segment["identifier"]))
        )
    return Scope(body["realm"], tuple(segments))


def legal_target(scope: Scope, target: Scope) -> None:
    if (
        type(target) is not Scope
        or target.realm != scope.realm
        or target.segments != scope.segments[: len(target.segments)]
    ):
        raise ValueError("invalid_target")


def timestamp(value: str) -> datetime:
    result = parse_timestamp(value)
    if canonical_timestamp(result) != value:
        raise ValueError("invalid_timestamp")
    return result


def snapshot(
    value: object, *, scope: Scope, proposal_id: UUID | None = None
) -> ProposalSnapshot:
    document = _object(value, frozenset(ProposalSnapshotBody.model_fields))
    body = ProposalSnapshotBody.model_validate(document)
    returned_scope, target = (
        scope_value(document["scope"]),
        scope_value(document["target_scope"]),
    )
    pid = identity(body.proposal_id)
    if returned_scope != scope or (proposal_id is not None and pid != proposal_id):
        raise ValueError("foreign_proposal")
    legal_target(scope, target)
    validate_reason(body.reason)
    recorded = timestamp(body.recorded_at)
    mutation = identity(body.mutation_id)
    decision = None
    if body.decision is not None:
        d = body.decision
        if body.state != d.state:
            raise ValueError("inconsistent_decision")
        if d.state == "accepted":
            if d.reason is not None:
                raise ValueError("invalid_acceptance")
        else:
            if d.reason is None:
                raise ValueError("missing_reason")
            validate_reason(d.reason)
            if d.evidence_id is not None or d.promoted_fact_id is not None:
                raise ValueError("invalid_rejection")
        decided = timestamp(d.recorded_at)
        mid = identity(d.mutation_id)
        evidence = None if d.evidence_id is None else identity(d.evidence_id)
        promoted = None if d.promoted_fact_id is None else identity(d.promoted_fact_id)
        if mid == mutation or promoted == identity(body.source_fact_id):
            raise ValueError("inconsistent_decision")
        decision = ProposalDecision(
            d.state, identity(d.decided_by), decided, mid, d.reason, evidence, promoted
        )
    if (body.state == "pending") != (decision is None):
        raise ValueError("inconsistent_state")
    return ProposalSnapshot(
        pid,
        returned_scope,
        identity(body.source_fact_id),
        TrustClass(body.source_trust),
        body.source_invalidated,
        target,
        Classification(body.classification),
        body.reason,
        identity(body.proposed_by),
        recorded,
        mutation,
        body.state,
        decision,
    )


def page(
    value: object, *, scope: Scope, limit: int, after: UUID | None
) -> ProposalPage:
    document = _object(value, frozenset(ProposalPageBody.model_fields))
    body = ProposalPageBody.model_validate(document)
    if len(body.items) > limit:
        raise ValueError("oversized_page")
    items = tuple(
        snapshot(raw, scope=scope) for raw in cast(list[object], document["items"])
    )
    ids = [str(item.proposal_id) for item in items]
    cursor = None if body.next_cursor is None else identity(body.next_cursor)
    if ids != sorted(set(ids)) or (
        after is not None and any(i <= str(after) for i in ids)
    ):
        raise ValueError("invalid_order")
    if cursor is not None and (len(items) != limit or cursor != items[-1].proposal_id):
        raise ValueError("invalid_cursor")
    return ProposalPage(items, cursor)


def _digest(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or bytes.fromhex(value).hex() != value
    ):
        raise ValueError("invalid_digest")
    return value


def mutation(
    value: object,
    *,
    proposal_id: UUID,
    scope: Scope,
    command_digest: bytes,
    source_fact_id: UUID | None = None,
    evidence_id: UUID | None = None,
    accepted_decision: ProposalDecision | None = None,
) -> ProposalMutation[ProposalRecorded] | ProposalMutation[FactsPromoted]:
    document = _object(
        value, frozenset({"outcome", "result", "mutation_receipt", "audit_receipt"})
    )
    if type(document["outcome"]) is not str or document["outcome"] not in {
        "committed",
        "replayed",
    }:
        raise ValueError("invalid_outcome")
    receipt = _object(
        document["mutation_receipt"], frozenset({"mutation_id", "command_digest"})
    )
    identity(receipt["mutation_id"])
    if _digest(receipt["command_digest"]) != command_digest.hex():
        raise ValueError("foreign_operation")
    audit = _object(
        document["audit_receipt"],
        frozenset(
            {
                "event_id",
                "chain_kind",
                "chain_identity",
                "sequence",
                "recorded_at",
                "event_hash",
            }
        ),
    )
    identity(audit["event_id"])
    _digest(audit["event_hash"])
    if (
        audit["chain_kind"] != "realm"
        or audit["chain_identity"] != scope.realm
        or type(audit["sequence"]) is not int
        or not 1 <= audit["sequence"] <= 2**63 - 1
    ):
        raise ValueError("invalid_audit")
    if type(audit["recorded_at"]) is not str:
        raise ValueError("invalid_time")
    timestamp(audit["recorded_at"])
    outcome = cast(Literal["committed", "replayed"], document["outcome"])
    if source_fact_id is None:
        result = _object(document["result"], frozenset({"proposal_id"}))
        if identity(result["proposal_id"]) != proposal_id:
            raise ValueError("foreign_proposal")
        return ProposalMutation(
            outcome,
            ProposalRecorded(proposal_id),
            freeze_object(receipt),
            freeze_object(audit),
        )
    result = _object(document["result"], frozenset({"promotions", "evidence_id"}))
    if (
        identity(result["evidence_id"]) != evidence_id
        or type(result["promotions"]) is not list
        or len(result["promotions"]) != 1
    ):
        raise ValueError("invalid_publication")
    pair = _object(
        result["promotions"][0], frozenset({"source_fact_id", "derived_fact_id"})
    )
    source, derived = (
        identity(pair["source_fact_id"]),
        identity(pair["derived_fact_id"]),
    )
    if source != source_fact_id or derived == source:
        raise ValueError("foreign_source")
    assert evidence_id is not None
    if accepted_decision is not None and (
        outcome != "replayed"
        or identity(receipt["mutation_id"]) != accepted_decision.mutation_id
        or (
            accepted_decision.promoted_fact_id is not None
            and derived != accepted_decision.promoted_fact_id
        )
        or (
            accepted_decision.evidence_id is not None
            and evidence_id != accepted_decision.evidence_id
        )
    ):
        # Null references disclose no identity. Only compare those actually
        # read; the mutation identity is always part of an accepted decision.
        raise ValueError("foreign_decision_result")
    return ProposalMutation(
        outcome,
        FactsPromoted(((source, derived),), evidence_id),
        freeze_object(receipt),
        freeze_object(audit),
    )
