"""Closed public proposal wire records; no authority or publication policy."""

from typing import Literal

from pydantic import Field

from cairn.transports.v1.requests import ScopeBody
from cairn.transports.v1.wire import WireModel


class ProposalContext(WireModel):
    scope: ScopeBody
    expected_instance_id: str


class ReadProposalRequest(ProposalContext):
    proposal_id: str


class ProposeRequest(ReadProposalRequest):
    source_fact_id: str
    target_scope: ScopeBody
    reason: str = Field(min_length=1, max_length=4096)


class ListProposalsRequest(ProposalContext):
    limit: int = Field(default=50, ge=1, le=100)
    after: str | None = None


class AcceptProposalRequest(ReadProposalRequest):
    evidence_id: str
    target_classification: Literal["public", "internal", "restricted"]


class RejectProposalRequest(ReadProposalRequest):
    reason: str = Field(min_length=1, max_length=4096)


class ProposalRecordedBody(WireModel):
    proposal_id: str


class ProposalDecisionBody(WireModel):
    state: Literal["accepted", "rejected"]
    decided_by: str
    recorded_at: str
    mutation_id: str
    reason: str | None
    evidence_id: str | None
    promoted_fact_id: str | None


class ProposalSnapshotBody(WireModel):
    proposal_id: str
    scope: ScopeBody
    source_fact_id: str
    source_trust: Literal["candidate", "validated", "failed-approach"]
    source_invalidated: bool
    target_scope: ScopeBody
    classification: Literal["public", "internal", "restricted"]
    reason: str
    proposed_by: str
    recorded_at: str
    mutation_id: str
    state: Literal["pending", "accepted", "rejected"]
    decision: ProposalDecisionBody | None


class ProposalPageBody(WireModel):
    items: list[ProposalSnapshotBody]
    next_cursor: str | None
