"""One translation for REST and MCP; attribution only comes from authority."""

from cairn.authority.evidence_window import EvidenceWindow, EvidenceWindowResult
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_page_types import (
    Order,
    RecallContinue,
    RecallPage,
    RecallPageResult,
    TimeBasis,
)
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
    EvidenceWindowBody,
    EvidenceWindowRequest,
    HistoryBody,
    HistoryRequest,
    RecallBody,
    RecallPageBody,
    RecallPageRequest,
    RecallRequest,
    RememberRequest,
    ResolveRequest,
)
from cairn.transports.v1.parsing import WireRejection
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
from cairn.transports.v1.wire import (
    RULE_INVALID_VALUE,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
)

_CONTINUATION_FIELDS = frozenset({"scope", "cursor", "budget", "limit"})


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


def recall_page_command(model: RecallPageRequest) -> RecallPage | RecallContinue:
    """Initial or continuation page; a continuation is exactly scope, cursor,
    budget and limit, because everything else is bound at creation."""
    if model.cursor is not None:
        extra = sorted(model.model_fields_set - _CONTINUATION_FIELDS)
        if extra:
            raise WireRejection(400, RULE_UNKNOWN_FIELD, extra[0])
        return RecallContinue(
            _scope(model.scope, "scope"), model.cursor, model.budget, model.limit
        )
    if model.query is None:
        raise WireRejection(400, RULE_MISSING_FIELD, "query")
    order = Order(model.order)
    if "time_basis" in model.model_fields_set and order is Order.RELEVANCE:
        raise WireRejection(400, RULE_INVALID_VALUE, "time_basis")
    base = retrieve_command(
        RetrieveRequest(
            scope=model.scope,
            query=model.query,
            budget=model.budget,
            trust_filters=model.trust_filters,
        )
    )
    return RecallPage(
        base.scope,
        base.query,
        order,
        None if model.time_basis is None else TimeBasis(model.time_basis),
        model.relevant_only,
        base.budget,
        model.limit,
        base.trust_filters,
    )


def recall_page_result(value: RecallPageResult) -> RecallPageBody:
    return RecallPageBody.model_validate(memory_value(value))


def evidence_window_command(model: EvidenceWindowRequest) -> EvidenceWindow:
    """Query or offset, never both; every other bound belongs to the authority."""
    if model.query is not None and model.start is not None:
        raise WireRejection(400, RULE_INVALID_VALUE, "start")
    return EvidenceWindow(
        _scope(model.scope, "scope"),
        _uuid(model.evidence_id, "evidence_id"),
        model.budget,
        model.query,
        model.start,
    )


def evidence_window_result(value: EvidenceWindowResult) -> EvidenceWindowBody:
    return EvidenceWindowBody.model_validate(memory_value(value))
