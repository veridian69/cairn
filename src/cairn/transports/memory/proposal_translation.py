"""REST and MCP translate to the same accepted proposal commands."""

from uuid import UUID

from cairn.authority.custody import CustodyValueError, validate_reason
from cairn.authority.proposal_types import (
    AcceptProposal,
    ListProposals,
    ProposalCommand,
    ProposeMemory,
    ReadProposal,
    RejectProposal,
)
from cairn.catalogue.audit import Classification
from cairn.transports.memory.proposal_models import (
    AcceptProposalRequest,
    ListProposalsRequest,
    ProposalContext,
    ProposeRequest,
    ReadProposalRequest,
    RejectProposalRequest,
)
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.translation import _scope, _uuid
from cairn.transports.v1.wire import RULE_INVALID_VALUE


def proposal_command(request: ProposalContext) -> tuple[UUID, ProposalCommand]:
    expected = _uuid(request.expected_instance_id, "expected_instance_id")
    scope = _scope(request.scope, "scope")
    if isinstance(request, ListProposalsRequest):
        return expected, ListProposals(
            scope,
            request.limit,
            None if request.after is None else _uuid(request.after, "after"),
        )
    assert isinstance(request, ReadProposalRequest)
    pid = _uuid(request.proposal_id, "proposal_id")
    if isinstance(request, (ProposeRequest, RejectProposalRequest)):
        try:
            validate_reason(request.reason)
        except CustodyValueError:
            raise WireRejection(400, RULE_INVALID_VALUE, "reason") from None
    if isinstance(request, ProposeRequest):
        return expected, ProposeMemory(
            scope,
            pid,
            _uuid(request.source_fact_id, "source_fact_id"),
            _scope(request.target_scope, "target_scope"),
            request.reason,
        )
    if isinstance(request, AcceptProposalRequest):
        return expected, AcceptProposal(
            scope,
            pid,
            _uuid(request.evidence_id, "evidence_id"),
            Classification(request.target_classification),
        )
    if isinstance(request, RejectProposalRequest):
        return expected, RejectProposal(scope, pid, request.reason)
    return expected, ReadProposal(scope, pid)
