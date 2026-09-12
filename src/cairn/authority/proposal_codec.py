"""Strict canonical boundary for persisted proposals, decisions and receipts.

Stored text is never normalised into another lookup identity. Row decoders
validate every column, including fields unused by the current operation.
Only parser failures become custody errors; authority/programming faults are
not caught here. Public commands and the shared promotion codec are unchanged.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from cairn.authority.custody import CustodyValueError, validate_reason
from cairn.authority.gate import Fetch
from cairn.authority.mutations import (
    FactsPromoted,
    PromoteFacts,
    _canonical_json,
    _proposal_accept_digest,
    _scope_document,
    _segments_column,
    _SourceFact,
    _stored_scope,
    _target_shape_failure,
)
from cairn.authority.proposal_types import (
    ProposalRecorded,
    ProposeMemory,
    RejectProposal,
)
from cairn.catalogue.audit import AuditValueError, Classification, Scope, TrustClass
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.catalogue.transactions import MutationReceipt


def invalid() -> CustodyValueError:
    return CustodyValueError("invalid_proposal_record")


def text(value: object) -> str:
    if type(value) is not str:
        raise invalid()
    return value


def identity(value: object) -> UUID:
    raw = text(value)
    try:
        result = UUID(raw)
    except ValueError:
        raise invalid() from None
    if str(result) != raw:
        raise invalid()
    return result


def timestamp(value: object) -> datetime:
    raw = text(value)
    try:
        result = parse_timestamp(raw)
    except CatalogueStorageError:
        raise invalid() from None
    if canonical_timestamp(result) != raw:
        raise invalid()
    return result


def binary_digest(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise invalid()
    return value


def hex_digest(value: object) -> bytes:
    raw = text(value)
    try:
        result = bytes.fromhex(raw)
    except ValueError:
        raise invalid() from None
    if len(result) != 32 or result.hex() != raw:
        raise invalid()
    return result


def classification(value: object) -> Classification:
    try:
        return Classification(text(value))
    except ValueError:
        raise invalid() from None


def reason(value: object) -> str:
    result = text(value)
    validate_reason(result)
    return result


def _json(raw: str) -> object:
    try:
        document = json.loads(raw)
        canonical = _canonical_json(document)
    except (ValueError, RecursionError):
        raise invalid() from None
    # This also rejects duplicate keys, whitespace, escaped aliases and NaN
    # spelling variants. Field validators reject non-finite/scalar values.
    if canonical != raw:
        raise invalid()
    return document


def scope(realm: object, segments: object) -> Scope:
    raw = text(segments)
    _json(raw)
    try:
        result = _stored_scope(text(realm), raw)
    except AuditValueError:
        raise invalid() from None
    if _segments_column(result.segments) != raw:
        raise invalid()
    return result


def load_sources(fetch: Fetch, fact_ids: tuple[UUID, ...]) -> dict[UUID, _SourceFact]:
    """Decode proposal sources strictly without changing legacy promotion."""
    slots = ",".join("?" for _ in fact_ids)
    rows = fetch(
        "SELECT fact_id, realm_id, scope_segments, body, trust, classification, "
        f"valid_from, valid_to FROM facts WHERE fact_id IN ({slots})",
        tuple(str(fact_id) for fact_id in fact_ids),
    )
    sources: dict[UUID, _SourceFact] = {}
    for row in rows:
        fact, realm, segments, body, trust, level, start, end = row
        fact_id = identity(fact)
        try:
            trust_class = TrustClass(text(trust))
        except ValueError:
            raise invalid() from None
        sources[fact_id] = _SourceFact(
            fact_id,
            scope(realm, segments),
            text(body),
            trust_class,
            classification(level),
            None if start is None else timestamp(start),
            None if end is None else timestamp(end),
        )
    return sources


def validate_evidence(fetch: Fetch, evidence_id: UUID) -> None:
    """Normal promotion still decides visibility; validate its stored inputs."""
    rows = fetch(
        "SELECT realm_id, scope_segments, classification, payload_digest "
        "FROM evidence_records WHERE evidence_id=?",
        (str(evidence_id),),
    )
    if rows:
        realm, segments, level, digest = rows[0]
        scope(realm, segments)
        classification(level)
        binary_digest(digest)


def _members(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or value.keys() != keys:
        raise invalid()
    return cast(dict[str, object], value)


def _document(data: bytes) -> object:
    if type(data) is not bytes:
        raise invalid()
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid() from None
    return _json(raw)


@dataclass(frozen=True)
class StoredBinding:
    principal_id: UUID
    operation: str
    idempotency_key: UUID
    command_digest: bytes
    mutation_id: UUID
    recorded_at: datetime


@dataclass(frozen=True)
class StoredProposal:
    proposal_id: UUID
    source_fact_id: UUID
    scope: Scope
    target_scope: Scope
    classification: Classification
    reason: str
    binding: StoredBinding


@dataclass(frozen=True)
class StoredDecision:
    proposal_id: UUID
    state: Literal["accepted", "rejected"]
    binding: StoredBinding
    reason: str | None
    evidence_id: UUID | None
    promoted_fact_id: UUID | None
    target_classification: Classification | None


PROPOSAL_COLUMNS = (
    "proposal_id, source_fact_id, realm_id, scope_segments, target_segments, "
    "classification, reason, principal_id, operation, idempotency_key, "
    "command_digest, mutation_id, recorded_at"
)
DECISION_COLUMNS = (
    "proposal_id, state, principal_id, operation, idempotency_key, command_digest, "
    "mutation_id, recorded_at, reason, evidence_id, promoted_fact_id, target_classification"
)


def _binding(values: tuple[object, ...], operation: str) -> StoredBinding:
    principal, action, key, digest, mutation, at = values
    if text(action) != operation:
        raise invalid()
    return StoredBinding(
        identity(principal),
        operation,
        identity(key),
        binary_digest(digest),
        identity(mutation),
        timestamp(at),
    )


def decode_proposal(row: tuple[object, ...]) -> StoredProposal:
    if len(row) != 13:
        raise invalid()
    proposal, source, realm, segments, target, level, why = row[:7]
    source_scope, target_scope = scope(realm, segments), scope(realm, target)
    level_value = classification(level)
    if (
        _target_shape_failure(source_scope, level_value, target_scope, level_value)
        is not None
    ):
        raise invalid()
    return StoredProposal(
        identity(proposal),
        identity(source),
        source_scope,
        target_scope,
        level_value,
        reason(why),
        _binding(row[7:], "memory-propose"),
    )


def decode_decision(row: tuple[object, ...]) -> StoredDecision:
    if len(row) != 12:
        raise invalid()
    proposal, state = row[:2]
    why, evidence, fact, level = row[8:]
    if state == "accepted":
        if why is not None:
            raise invalid()
        return StoredDecision(
            identity(proposal),
            "accepted",
            _binding(row[2:8], "memory-proposal-accept"),
            None,
            identity(evidence),
            identity(fact),
            classification(level),
        )
    if state == "rejected":
        if (evidence, fact, level) != (None, None, None):
            raise invalid()
        return StoredDecision(
            identity(proposal),
            "rejected",
            _binding(row[2:8], "memory-proposal-reject"),
            reason(why),
            None,
            None,
            None,
        )
    raise invalid()


def digest(command: ProposeMemory | RejectProposal) -> bytes:
    document: dict[str, object] = {
        "operation": "memory-propose"
        if isinstance(command, ProposeMemory)
        else "memory-proposal-reject",
        "scope": _scope_document(command.scope),
        "proposal_id": str(command.proposal_id),
        "reason": command.reason,
    }
    if isinstance(command, ProposeMemory):
        document.update(
            source_fact_id=str(command.source_fact_id),
            target_scope=_scope_document(command.target_scope),
        )
    return hashlib.sha256(_canonical_json(document).encode()).digest()


def encode(value: ProposalRecorded, receipt: MutationReceipt) -> bytes:
    return _canonical_json(
        {
            "proposal_id": str(value.proposal_id),
            "mutation_id": str(receipt.mutation_id),
            "command_digest": receipt.command_digest.hex(),
        }
    ).encode()


def decode(data: bytes) -> tuple[ProposalRecorded, MutationReceipt]:
    value = _members(_document(data), {"proposal_id", "mutation_id", "command_digest"})
    return ProposalRecorded(identity(value["proposal_id"])), MutationReceipt(
        identity(value["mutation_id"]), hex_digest(value["command_digest"])
    )


def decode_acceptance(data: bytes) -> tuple[FactsPromoted, MutationReceipt]:
    document = _members(_document(data), {"result", "mutation_receipt"})
    result = _members(document["result"], {"promotions", "evidence_id"})
    receipt = _members(document["mutation_receipt"], {"mutation_id", "command_digest"})
    pairs = result["promotions"]
    if type(pairs) is not list or len(pairs) != 1:
        raise invalid()
    pair = pairs[0]
    if type(pair) is not list or len(pair) != 2:
        raise invalid()
    return FactsPromoted(
        ((identity(pair[0]), identity(pair[1])),), identity(result["evidence_id"])
    ), MutationReceipt(
        identity(receipt["mutation_id"]), hex_digest(receipt["command_digest"])
    )


def _bound_payload(
    fetch: Fetch, binding: StoredBinding, command_digest: bytes, schema: str
) -> bytes:
    """The key and mutation must identify the same actual receipt."""
    rows = fetch(
        "SELECT command_digest, mutation_id, result_schema, result_bytes, result_digest "
        "FROM idempotency_records WHERE principal_id=? AND operation=? AND idempotency_key=?",
        (str(binding.principal_id), binding.operation, str(binding.idempotency_key)),
    )
    if not rows:
        raise invalid()
    stored_digest, mutation, stored_schema, payload, payload_digest = rows[0]
    if (
        binding.command_digest != command_digest
        or binary_digest(stored_digest) != command_digest
        or identity(mutation) != binding.mutation_id
        or stored_schema != schema
        or type(payload) is not bytes
        or hashlib.sha256(payload).digest() != binary_digest(payload_digest)
    ):
        raise invalid()
    return payload


def _recorded_history(
    fetch: Fetch, binding: StoredBinding, command: ProposeMemory | RejectProposal
) -> None:
    expected_digest = digest(command)
    value, receipt = decode(
        _bound_payload(fetch, binding, expected_digest, "cairn.proposal.recorded/v1")
    )
    if value.proposal_id != command.proposal_id or receipt != MutationReceipt(
        binding.mutation_id, expected_digest
    ):
        raise invalid()


def validate_history(
    fetch: Fetch, proposal: StoredProposal, decision: StoredDecision | None
) -> None:
    """Validate actual persisted commands/results independently of the reader.

    This is integrity, not authorisation or visibility. Historical principals
    need no current grant, and source invalidation does not rewrite history.
    The service applies the current reader's scope/clearance separately.
    """
    _recorded_history(
        fetch,
        proposal.binding,
        ProposeMemory(
            proposal.scope,
            proposal.proposal_id,
            proposal.source_fact_id,
            proposal.target_scope,
            proposal.reason,
        ),
    )
    if decision is None:
        return
    if decision.proposal_id != proposal.proposal_id:
        raise invalid()
    if decision.state == "rejected":
        _recorded_history(
            fetch,
            decision.binding,
            RejectProposal(
                proposal.scope,
                proposal.proposal_id,
                reason(decision.reason),
            ),
        )
        return
    evidence, fact, level = (
        decision.evidence_id,
        decision.promoted_fact_id,
        decision.target_classification,
    )
    if evidence is None or fact is None or level is None:
        raise invalid()
    if (
        _target_shape_failure(
            proposal.scope, proposal.classification, proposal.target_scope, level
        )
        is not None
    ):
        raise invalid()
    promotion = PromoteFacts(
        (proposal.source_fact_id,),
        evidence,
        proposal.target_scope,
        level,
        proposal.reason,
    )
    expected_digest = _proposal_accept_digest(promotion, proposal.proposal_id)
    result, receipt = decode_acceptance(
        _bound_payload(
            fetch,
            decision.binding,
            expected_digest,
            "cairn.authority.promotion/v1",
        )
    )
    if receipt != MutationReceipt(
        decision.binding.mutation_id, expected_digest
    ) or result != FactsPromoted(((proposal.source_fact_id, fact),), evidence):
        raise invalid()
    actual = fetch(
        "SELECT derived_from, evidence_id, promoted_by, realm_id, scope_segments, classification, trust "
        "FROM facts WHERE fact_id=?",
        (str(fact),),
    )
    if actual != (
        (
            str(proposal.source_fact_id),
            str(evidence),
            str(decision.binding.principal_id),
            proposal.target_scope.realm,
            _segments_column(proposal.target_scope.segments),
            level.value,
            "validated",
        ),
    ):
        raise invalid()
