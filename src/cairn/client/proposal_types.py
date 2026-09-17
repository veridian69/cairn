"""Detached immutable public proposal values and actual mutation receipts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.client.types import FrozenJSONObject

__all__ = [
    "FactsPromoted",
    "ProposalDecision",
    "ProposalPage",
    "ProposalRecorded",
    "ProposalSnapshot",
    "ProposalMutation",
]


@dataclass(frozen=True, slots=True)
class FactsPromoted:
    promotions: tuple[tuple[UUID, UUID], ...]
    evidence_id: UUID


@dataclass(frozen=True, slots=True)
class ProposalRecorded:
    proposal_id: UUID


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    state: Literal["accepted", "rejected"]
    decided_by: UUID
    recorded_at: datetime
    mutation_id: UUID
    reason: str | None
    evidence_id: UUID | None
    promoted_fact_id: UUID | None


@dataclass(frozen=True, slots=True)
class ProposalSnapshot:
    proposal_id: UUID
    scope: Scope
    source_fact_id: UUID
    source_trust: TrustClass
    source_invalidated: bool
    target_scope: Scope
    classification: Classification
    reason: str
    proposed_by: UUID
    recorded_at: datetime
    mutation_id: UUID
    state: Literal["pending", "accepted", "rejected"]
    decision: ProposalDecision | None


@dataclass(frozen=True, slots=True)
class ProposalPage:
    items: tuple[ProposalSnapshot, ...]
    next_cursor: UUID | None


@dataclass(frozen=True, slots=True)
class ProposalMutation[T: (ProposalRecorded, FactsPromoted)]:
    """A proposal receipt records state; only FactsPromoted records publication."""

    outcome: Literal["committed", "replayed"]
    result: T
    mutation_receipt: FrozenJSONObject
    audit_receipt: FrozenJSONObject
