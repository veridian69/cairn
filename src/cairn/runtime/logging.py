import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO
from uuid import UUID

_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# An adapter error's safe ``code`` identifier (I-32): lower-case snake,
# bounded, nothing that could carry content.
_ADAPTER_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_STACK_BASENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,127}\Z")
# Bounds an outbox row's raw work_id exactly as tightly as migration 0003's
# CHECK does — length 36, hex digits and dashes only — without asserting it
# parses as a real UUID. It may not: the CHECK pins that shape but not that
# every wildcard-matched position is a hex digit rather than another dash,
# so a schema-legal row can still fail UUID() construction (see
# cairn.evidence.delivery._parse_batch). Logging the raw string is what lets
# an operator find the row by primary key without this module trying to
# round-trip a value it cannot trust as a real identity.
_OUTBOX_WORK_ID = re.compile(r"[0-9a-f-]{36}\Z")


def is_safe_outbox_identity(value: str) -> bool:
    """Whether ``value`` is safe to pass as ``SafeLogger.emit``'s
    ``work_id``. Exposed so a caller holding a raw, not-yet-trusted
    identity (e.g. ``cairn.evidence.delivery._parse_batch``, reading a row
    whose shape CHECK a restored-from-pre-STRICT catalogue might not
    actually satisfy) can decide *before* calling ``emit`` whether it holds
    a safe value — the guard must not depend on ``emit``'s own validation
    being the last line of defence, since raising there is exactly the
    crash this exists to prevent."""
    return type(value) is str and _OUTBOX_WORK_ID.fullmatch(value) is not None


class LogEvent(StrEnum):
    CATALOGUE_VERIFIED = "catalogue_verified"
    RUNTIME_START_FAILED = "runtime_start_failed"
    RUNTIME_STARTED = "runtime_started"
    RUNTIME_STOPPED = "runtime_stopped"
    REQUEST_COMPLETED = "request_completed"
    EVIDENCE_DELIVERY_COMPLETED = "evidence_delivery_completed"
    EVIDENCE_DIGEST_MISMATCH = "evidence_digest_mismatch"
    EVIDENCE_RECORD_UNREADABLE = "evidence_record_unreadable"
    EVIDENCE_OUTBOX_ROW_UNREADABLE = "evidence_outbox_row_unreadable"
    EVIDENCE_FETCH_FAILED = "evidence_fetch_failed"
    EVIDENCE_OUTBOX_AGE_UNREADABLE = "evidence_outbox_age_unreadable"
    EVIDENCE_DELIVERY_FAILED = "evidence_delivery_failed"
    PROJECTION_DELIVERY_COMPLETED = "projection_delivery_completed"
    PROJECTION_OUTBOX_ROW_UNREADABLE = "projection_outbox_row_unreadable"
    PROJECTION_OUTBOX_AGE_UNREADABLE = "projection_outbox_age_unreadable"
    PROJECTION_DELIVERY_FAILED = "projection_delivery_failed"
    PROJECTION_BULK_DEMOTED = "projection_bulk_demoted"
    STALE_INDEX_CANDIDATE = "stale_index_candidate"
    # P-90 remediation ruling R4 (29 August 2026): the projection cache's
    # four events, carrying only the closed CacheKind/CacheReason enums
    # and the bounded cache_hits/cache_misses/cache_rows counters.
    PROJECTION_CACHE_MISS = "projection_cache_miss"
    PROJECTION_CACHE_WRITE_DROPPED = "projection_cache_write_dropped"
    PROJECTION_EXTRACTION_CACHE_BATCH = "projection_extraction_cache_batch"
    PROJECTION_EMBEDDING_CACHE_BATCH = "projection_embedding_cache_batch"
    EDGE_BATCH_ITEM_MISSING = "edge_batch_item_missing"
    SEMANTIC_REBUILD_NEEDED = "semantic_rebuild_needed"


class Operation(StrEnum):
    CATALOGUE_MIGRATE = "catalogue_migrate"
    CATALOGUE_VERIFY = "catalogue_verify"
    HEALTH_LIVE = "health_live"
    HEALTH_STARTUP = "health_startup"
    HEALTH_READY = "health_ready"
    METRICS = "metrics"
    # One member per I-70 ``/v1`` route (P-32), so the foundation
    # middleware's safe request logging and metrics label every operation.
    # Added together in Task 6; Tasks 7-9 register the remaining routes
    # against the members already named here.
    INGEST = "ingest"
    PROMOTE = "promote"
    INVALIDATE = "invalidate"
    READ_EVIDENCE = "read_evidence"
    RETRIEVE = "retrieve"
    CREATE_PRINCIPAL = "create_principal"
    ISSUE_CREDENTIAL = "issue_credential"
    REVOKE_CREDENTIAL = "revoke_credential"
    CREATE_GRANT = "create_grant"
    REVOKE_GRANT = "revoke_grant"
    READ_AUDIT_EVENTS = "read_audit_events"
    INSTANCE = "instance"
    MEMORY_DIAGNOSE = "memory_diagnose"
    MEMORY_REMEMBER = "memory_remember"
    MEMORY_RECALL = "memory_recall"
    MEMORY_HISTORY = "memory_history"
    MEMORY_SUGGEST = "memory_suggest"
    MEMORY_PROPOSE = "memory_propose"
    MEMORY_PROPOSAL_LIST = "memory_proposal_list"
    MEMORY_PROPOSAL_READ = "memory_proposal_read"
    MEMORY_PROPOSAL_ACCEPT = "memory_proposal_accept"
    MEMORY_PROPOSAL_REJECT = "memory_proposal_reject"
    MEMORY_DISAGREE = "memory_disagree"
    MEMORY_RESOLVE = "memory_resolve"
    MEMORY_CORRECT = "memory_correct"
    MEMORY_SESSION_OPEN = "memory_session_open"
    MEMORY_TURN_BEGIN = "memory_turn_begin"
    MEMORY_TURN_PREPARE = "memory_turn_prepare"
    MEMORY_TURN_COMMIT = "memory_turn_commit"
    MEMORY_TURN_ABANDON = "memory_turn_abandon"
    MEMORY_SESSION_READ = "memory_session_read"
    MEMORY_VISIT_ISSUE = "memory_visit_issue"
    MEMORY_VISIT_ACKNOWLEDGE = "memory_visit_acknowledge"


class Transport(StrEnum):
    """Which surface served a request (I-84's one new dimension, P-57).

    Low-cardinality by construction — the closed set of transports Cairn
    publishes, never a caller-supplied string. The ``Operation`` above says
    *what* was asked; this says *through which surface*, because slice 7
    mounts a second one over the same eleven operations and a metric that
    could not tell them apart would make an MCP regression invisible behind
    REST's traffic.

    P-57 pins the argument carrying this as explicit and undefaulted at
    every call site: a default would silently re-label the very thing the
    dimension exists to distinguish.
    """

    REST = "rest"
    MCP = "mcp"


class OutcomeCode(StrEnum):
    SUCCESS = "success"
    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"
    UNAVAILABLE = "unavailable"


class Dependency(StrEnum):
    CATALOGUE = "catalogue"


class RuntimeFailureCode(StrEnum):
    ALREADY_LOCKED = "already_locked"
    CATALOGUE_INVALID = "catalogue_invalid"
    CONTRACT_UNAVAILABLE = "contract_unavailable"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    DATA_UNAVAILABLE = "data_unavailable"
    INSTANCE_MISMATCH = "instance_mismatch"


class BulkDemotionShape(StrEnum):
    """P-82 gate-4 ruling (25 August 2026): the partition-free failure
    shape carried by ``projection_bulk_demoted``. Kept separate from
    ``RuntimeFailureCode``, which is composition's startup vocabulary.

    A closed enum rather than free text because the shape is derived from
    what the index adapter returned, and nothing derived from an adapter's
    answer may reach a log line unbounded."""

    ADAPTER_RAISED = "adapter_raised"
    ANSWER_NOT_A_TUPLE = "answer_not_a_tuple"
    ANSWER_LENGTH_MISMATCH = "answer_length_mismatch"
    ANSWER_ELEMENT_INVALID = "answer_element_invalid"


class CacheKind(StrEnum):
    """Which P-90 projection cache an event is about (R4). A closed enum
    precisely so no table name ever reaches a log line."""

    EXTRACTION = "extraction"
    EMBEDDING = "embedding"
    TIMESTAMPS = "timestamps"


class CacheReason(StrEnum):
    """Why a P-90 cache read produced no completed answer (R4).
    Release-controlled and partition-free, like every other closed enum
    here — never derived from an exception's text."""

    READ_FAILED = "read_failed"
    PAYLOAD_INVALID = "payload_invalid"
    TIMESTAMP_CALL_FAILED = "timestamp_call_failed"
    TIMESTAMP_PARSE_FAILED = "timestamp_parse_failed"


@dataclass(frozen=True, slots=True)
class _LogPayload:
    event: str
    instance_id: str | None
    correlation_id: str | None
    operation: str | None
    transport: str | None
    outcome_code: str | None
    duration_ms: float | None
    dependency: str | None
    failure_code: str | None
    failure_shape: str | None
    adapter_code: str | None
    retry_attempt: int | None
    exception_type: str | None
    stack_location: str | None
    delivered: int | None
    failed: int | None
    remaining: int | None
    chunk_size: int | None
    evidence_id: str | None
    work_id: str | None
    candidate_id: str | None
    cache_kind: str | None
    cache_reason: str | None
    cache_hits: int | None
    cache_misses: int | None
    cache_rows: int | None
    item: int | None
    size: int | None
    answered: int | None


class _SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not isinstance(record.msg, _LogPayload):
            raise TypeError("safe logger received an invalid payload")
        payload: dict[str, object] = {
            key: value for key, value in asdict(record.msg).items() if value is not None
        }
        payload["time"] = datetime.fromtimestamp(record.created, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        return json.dumps(payload, sort_keys=True)


class SafeLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(
        self,
        event: LogEvent,
        /,
        *,
        level: int = logging.INFO,
        instance_id: UUID | None = None,
        correlation_id: UUID | None = None,
        operation: Operation | None = None,
        transport: Transport | None,
        outcome_code: OutcomeCode | None = None,
        duration_ms: float | None = None,
        dependency: Dependency | None = None,
        failure_code: RuntimeFailureCode | None = None,
        failure_shape: BulkDemotionShape | None = None,
        adapter_code: str | None = None,
        retry_attempt: int | None = None,
        exception_type: str | None = None,
        stack_location: str | None = None,
        delivered: int | None = None,
        failed: int | None = None,
        remaining: int | None = None,
        chunk_size: int | None = None,
        evidence_id: UUID | None = None,
        work_id: str | None = None,
        candidate_id: UUID | None = None,
        cache_kind: CacheKind | None = None,
        cache_reason: CacheReason | None = None,
        cache_hits: int | None = None,
        cache_misses: int | None = None,
        cache_rows: int | None = None,
        item: int | None = None,
        size: int | None = None,
        answered: int | None = None,
    ) -> None:
        if type(event) is not LogEvent:
            raise TypeError("event must be a LogEvent")
        if type(level) is not int or level < 0:
            raise TypeError("level must be a non-negative integer")
        if instance_id is not None and type(instance_id) is not UUID:
            raise TypeError("instance_id must be a UUID")
        if correlation_id is not None and type(correlation_id) is not UUID:
            raise TypeError("correlation_id must be a UUID")
        if operation is not None and type(operation) is not Operation:
            raise TypeError("operation must be an Operation")
        # Optional here rather than required as it is on ``observe_request``:
        # most events this logger carries are lifecycle or background work
        # that no transport served. P-57's undefaulted rule still holds
        # where it bites — there is no ``rest`` default anywhere, so an
        # omitted transport is absent from the line rather than wrong on it.
        if transport is not None and type(transport) is not Transport:
            raise TypeError("transport must be a Transport")
        if outcome_code is not None and type(outcome_code) is not OutcomeCode:
            raise TypeError("outcome_code must be an OutcomeCode")
        safe_duration_ms = _validate_optional_duration(duration_ms)
        if dependency is not None and type(dependency) is not Dependency:
            raise TypeError("dependency must be a Dependency")
        if failure_code is not None and type(failure_code) is not RuntimeFailureCode:
            raise TypeError("failure_code must be a RuntimeFailureCode")
        if failure_shape is not None and type(failure_shape) is not BulkDemotionShape:
            raise TypeError("failure_shape must be a BulkDemotionShape")
        if adapter_code is not None and (
            type(adapter_code) is not str
            or _ADAPTER_CODE.fullmatch(adapter_code) is None
        ):
            raise TypeError("adapter_code must be a safe identifier")
        if retry_attempt is not None and (
            type(retry_attempt) is not int or retry_attempt < 0
        ):
            raise TypeError("retry_attempt must be a non-negative integer")
        if exception_type is not None and (
            type(exception_type) is not str
            or _EXCEPTION_CLASS.fullmatch(exception_type) is None
        ):
            raise TypeError("exception_type must be an exception class name")
        _validate_optional_stack_location(stack_location)
        _validate_optional_count(delivered, "delivered")
        _validate_optional_count(failed, "failed")
        _validate_optional_count(remaining, "remaining")
        _validate_optional_count(chunk_size, "chunk_size")
        if evidence_id is not None and type(evidence_id) is not UUID:
            raise TypeError("evidence_id must be a UUID")
        if work_id is not None and (
            type(work_id) is not str or _OUTBOX_WORK_ID.fullmatch(work_id) is None
        ):
            raise TypeError("work_id must be a 36-character hex/dash identity")
        if candidate_id is not None and type(candidate_id) is not UUID:
            raise TypeError("candidate_id must be a UUID")
        if cache_kind is not None and type(cache_kind) is not CacheKind:
            raise TypeError("cache_kind must be a CacheKind")
        if cache_reason is not None and type(cache_reason) is not CacheReason:
            raise TypeError("cache_reason must be a CacheReason")
        _validate_optional_count(cache_hits, "cache_hits")
        _validate_optional_count(cache_misses, "cache_misses")
        _validate_optional_count(cache_rows, "cache_rows")
        _validate_optional_count(item, "item")
        _validate_optional_count(size, "size")
        _validate_optional_count(answered, "answered")
        _validate_edge_batch_item_missing_shape(
            event,
            item=item,
            size=size,
            answered=answered,
            unrelated=(
                instance_id,
                correlation_id,
                operation,
                transport,
                outcome_code,
                duration_ms,
                dependency,
                failure_code,
                failure_shape,
                adapter_code,
                retry_attempt,
                exception_type,
                stack_location,
                delivered,
                failed,
                remaining,
                chunk_size,
                evidence_id,
                work_id,
                candidate_id,
                cache_kind,
                cache_reason,
                cache_hits,
                cache_misses,
                cache_rows,
            ),
        )

        # No partition, query, identity, vector or exception detail belongs in
        # this operator action signal. The event itself is the complete shape.
        if event is LogEvent.SEMANTIC_REBUILD_NEEDED and any(
            value is not None
            for value in (
                instance_id,
                correlation_id,
                operation,
                transport,
                outcome_code,
                duration_ms,
                dependency,
                failure_code,
                failure_shape,
                adapter_code,
                retry_attempt,
                exception_type,
                stack_location,
                delivered,
                failed,
                remaining,
                chunk_size,
                evidence_id,
                work_id,
                candidate_id,
                cache_kind,
                cache_reason,
                cache_hits,
                cache_misses,
                cache_rows,
                item,
                size,
                answered,
            )
        ):
            raise TypeError("semantic rebuild event accepts no payload fields")

        self._logger.log(
            level,
            _LogPayload(
                event=event.value,
                instance_id=str(instance_id) if instance_id is not None else None,
                correlation_id=(
                    str(correlation_id) if correlation_id is not None else None
                ),
                operation=operation.value if operation is not None else None,
                transport=transport.value if transport is not None else None,
                outcome_code=(outcome_code.value if outcome_code is not None else None),
                duration_ms=safe_duration_ms,
                dependency=dependency.value if dependency is not None else None,
                failure_code=(failure_code.value if failure_code is not None else None),
                failure_shape=(
                    failure_shape.value if failure_shape is not None else None
                ),
                adapter_code=adapter_code,
                retry_attempt=retry_attempt,
                exception_type=exception_type,
                stack_location=stack_location,
                delivered=delivered,
                failed=failed,
                remaining=remaining,
                chunk_size=chunk_size,
                evidence_id=str(evidence_id) if evidence_id is not None else None,
                work_id=work_id,
                candidate_id=(str(candidate_id) if candidate_id is not None else None),
                cache_kind=cache_kind.value if cache_kind is not None else None,
                cache_reason=(cache_reason.value if cache_reason is not None else None),
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                cache_rows=cache_rows,
                item=item,
                size=size,
                answered=answered,
            ),
        )


def _validate_optional_duration(value: float | None) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise TypeError("duration_ms must be a finite non-negative number")
    try:
        safe_value = float(value)
    except OverflowError:
        raise TypeError("duration_ms must be a finite non-negative number") from None
    if not math.isfinite(safe_value) or safe_value < 0:
        raise TypeError("duration_ms must be a finite non-negative number")
    return safe_value


def _validate_optional_count(value: int | None, field: str) -> None:
    if value is None:
        return
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")


def _validate_edge_batch_item_missing_shape(
    event: LogEvent,
    *,
    item: int | None,
    size: int | None,
    answered: int | None,
    unrelated: tuple[object | None, ...],
) -> None:
    counts = (item, size, answered)
    if event is LogEvent.EDGE_BATCH_ITEM_MISSING:
        if any(value is None for value in counts):
            raise TypeError("edge_batch_item_missing requires item, size, and answered")
        if any(value is not None for value in unrelated):
            raise TypeError("edge_batch_item_missing refuses unrelated fields")
    elif any(value is not None for value in counts):
        raise TypeError("item, size, and answered require edge_batch_item_missing")


def _validate_optional_stack_location(value: str | None) -> None:
    if value is None:
        return
    if type(value) is not str or len(value) > 140:
        raise TypeError("stack_location must be a basename and line number")
    basename, separator, line = value.rpartition(":")
    if (
        separator != ":"
        or _STACK_BASENAME.fullmatch(basename) is None
        or not line.isascii()
        or not line.isdecimal()
        or int(line) < 1
    ):
        raise TypeError("stack_location must be a basename and line number")


def configure_logging(stream: TextIO) -> SafeLogger:
    # graphiti-core logs outside Cairn's safe event vocabulary: its FalkorDB
    # driver includes arbitrary exception messages, Cypher and complete query
    # parameters, while other children emit unstructured warnings during
    # normal extraction. Parameters can contain fact bodies and retrieval
    # queries, and I-32 permits application-owned structured events only. Own
    # the whole dependency namespace before composition constructs the adapter;
    # a NullHandler alone is insufficient while propagation remains enabled.
    dependency = logging.getLogger("graphiti_core")
    dependency.handlers = [logging.NullHandler()]
    dependency.propagate = False

    handler = logging.StreamHandler(stream)
    handler.setFormatter(_SafeJsonFormatter())
    logger = logging.Logger("cairn.safe", level=logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    return SafeLogger(logger)
