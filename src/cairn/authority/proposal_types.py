"""Frozen transport-independent proposal commands and disclosure results."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope, TrustClass


@dataclass(frozen=True, slots=True)
class ProposeMemory:
    scope: Scope
    proposal_id: UUID
    source_fact_id: UUID
    target_scope: Scope
    reason: str


@dataclass(frozen=True, slots=True)
class ReadProposal:
    scope: Scope
    proposal_id: UUID


@dataclass(frozen=True, slots=True)
class ListProposals:
    """One explicit source; UUID keyset order, 1–100 results per page.

    The cursor names a currently readable proposal in this exact source.
    Pages are not a snapshot: inserts before the cursor can be missed and
    changed grants may make a previously issued cursor unusable.
    """

    scope: Scope
    limit: int = 50
    after: UUID | None = None


@dataclass(frozen=True, slots=True)
class AcceptProposal:
    scope: Scope
    proposal_id: UUID
    evidence_id: UUID
    target_classification: Classification


@dataclass(frozen=True, slots=True)
class RejectProposal:
    scope: Scope
    proposal_id: UUID
    reason: str


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
    # Null means absent or not currently readable; no hidden-identity hints.
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


type ProposalCommand = (
    ProposeMemory | ReadProposal | ListProposals | AcceptProposal | RejectProposal
)
