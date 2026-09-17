"""One translation for REST and MCP; attribution only comes from authority."""

from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_types import (
    Disagree,
    History,
    MemoryHistory,
    Recall,
    RecallResult,
    Resolve,
)
from cairn.authority.mutations import IngestAssertion, InvalidateFacts
from cairn.catalogue.audit import Classification, Scope
from cairn.transports.memory.models import (
    CorrectRequest,
    DisagreeRequest,
    HistoryBody,
    HistoryRequest,
    RecallBody,
    RecallRequest,
    RememberRequest,
    ResolveRequest,
)
from cairn.transports.v1.requests import (
    IngestRequest,
    RetrieveRequest,
)
from cairn.transports.v1.translation import (
    _coerce,
    _scope,
    _uuid,
    ingest_command,
    invalidate_command,
    retrieve_command,
)


def correct_command(model: CorrectRequest) -> tuple[InvalidateFacts, Scope | None]:
    """Normal correction command plus the optional memory-only host restriction."""
    return invalidate_command(model), None if model.scope is None else _scope(
        model.scope, "scope"
    )


def remember_command(model: RememberRequest) -> IngestAssertion:
    return ingest_command(
        IngestRequest(
            **model.model_dump(), source_type="agent-claim", requested_trust="candidate"
        )
    )


def recall_command(model: RecallRequest) -> Recall:
    command = retrieve_command(
        RetrieveRequest(**model.model_dump(exclude={"relevant_only"}))
    )
    return Recall(
        command.scope,
        command.query,
        command.budget,
        command.trust_filters,
        relevant_only=model.relevant_only,
    )


def history_command(model: HistoryRequest) -> History:
    return History(
        _scope(model.scope, "scope"), _uuid(model.fact_id, "fact_id"), model.budget
    )


def disagree_command(model: DisagreeRequest) -> Disagree:
    return Disagree(
        _scope(model.scope, "scope"),
        _uuid(model.left_fact_id, "left_fact_id"),
        _uuid(model.right_fact_id, "right_fact_id"),
        _coerce(Classification, model.classification, "classification"),
        model.reason,
    )


def resolve_command(model: ResolveRequest) -> Resolve:
    return Resolve(
        _scope(model.scope, "scope"),
        _uuid(model.disagreement_id, "disagreement_id"),
        _uuid(model.evidence_id, "evidence_id"),
        None
        if model.selected_fact_id is None
        else _uuid(model.selected_fact_id, "selected_fact_id"),
        model.reason,
    )


def recall_result(value: RecallResult) -> RecallBody:
    return RecallBody.model_validate(memory_value(value))


def history_result(value: MemoryHistory) -> HistoryBody:
    return HistoryBody.model_validate(memory_value(value))
