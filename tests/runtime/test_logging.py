import json
import logging
import re
from io import StringIO
from typing import Any, cast
from uuid import UUID

import pytest

from cairn.operations.metrics import Metrics
from cairn.runtime.logging import (
    BulkDemotionShape,
    CacheKind,
    CacheReason,
    Dependency,
    LogEvent,
    Operation,
    OutcomeCode,
    RuntimeFailureCode,
    Transport,
    configure_logging,
)

SENTINEL = "super-secret-value"
CORRELATION_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_semantic_rebuild_event_has_no_content_fields() -> None:
    stream = StringIO()
    logger = configure_logging(stream)
    logger.emit(LogEvent.SEMANTIC_REBUILD_NEEDED, transport=None, level=logging.WARNING)
    payload = json.loads(stream.getvalue())
    assert payload == {"event": "semantic_rebuild_needed", "time": payload["time"]}


@pytest.mark.parametrize(
    "fields",
    [
        {"adapter_code": "arbitrary_content"},
        {"candidate_id": CORRELATION_ID},
        {"operation": Operation.MEMORY_RECALL},
        {"cache_rows": 1},
        {"exception_type": "ProviderError"},
    ],
)
def test_semantic_rebuild_event_rejects_unrelated_fields(
    fields: dict[str, Any],
) -> None:
    stream = StringIO()
    with pytest.raises(TypeError):
        configure_logging(stream).emit(
            LogEvent.SEMANTIC_REBUILD_NEEDED, transport=None, **fields
        )
    assert not stream.getvalue()


def test_safe_logger_emits_one_json_object_with_allowed_fields() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    logger.emit(
        LogEvent.REQUEST_COMPLETED, correlation_id=CORRELATION_ID, transport=None
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "correlation_id": "11111111-1111-4111-8111-111111111111",
        "event": "request_completed",
        "time": payload["time"],
    }
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        payload["time"],
    )


def test_safe_logger_rejects_unknown_field_name() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    with pytest.raises(TypeError):
        cast(Any, logger).emit(
            LogEvent.REQUEST_COMPLETED,
            submitted_text=SENTINEL,
        )

    assert stream.getvalue() == ""


def test_safe_logger_requires_event_positionally() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    with pytest.raises(TypeError):
        cast(Any, logger).emit(event=LogEvent.REQUEST_COMPLETED)

    assert stream.getvalue() == ""


def test_exception_event_omits_exception_message() -> None:
    stream = StringIO()
    logger = configure_logging(stream)
    rejected_stream = StringIO()
    rejected_logger = configure_logging(rejected_stream)

    try:
        raise RuntimeError(SENTINEL)
    except RuntimeError as error:
        with pytest.raises(TypeError):
            cast(Any, rejected_logger).emit(
                LogEvent.REQUEST_COMPLETED,
                instance_id=error,
            )
        logger.emit(
            LogEvent.REQUEST_COMPLETED,
            transport=None,
            outcome_code=OutcomeCode.INTERNAL_ERROR,
            exception_type=type(error).__name__,
            stack_location="middleware.py:42",
        )

    payload = json.loads(stream.getvalue())
    assert payload["exception_type"] == "RuntimeError"
    assert payload["stack_location"] == "middleware.py:42"
    assert SENTINEL not in stream.getvalue()
    assert rejected_stream.getvalue() == ""
    assert SENTINEL not in rejected_stream.getvalue()


def test_logging_configuration_silences_the_graphiti_dependency_namespace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I-32: graphiti-core emits parameter-bearing and unstructured logs.

    Gate-5 observed full query parameters from its FalkorDB driver and an
    unstructured edge-resolution warning from a second child logger. Cairn
    owns the process logging boundary, so configuring its safe logger must
    quarantine the dependency namespace rather than one known child.
    """
    dependencies = [
        logging.getLogger("graphiti_core"),
        logging.getLogger("graphiti_core.driver.falkordb_driver"),
        logging.getLogger("graphiti_core.utils.maintenance.edge_operations"),
    ]
    originals = [
        (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in dependencies
    ]
    try:
        for dependency in dependencies:
            dependency.handlers = []
            dependency.setLevel(logging.DEBUG)
            dependency.propagate = True
            dependency.disabled = False
        safe_stream = StringIO()

        configure_logging(safe_stream)
        dependencies[1].error("unsafe dependency query: %s", SENTINEL)
        dependencies[2].warning("unsafe dependency edge warning: %s", SENTINEL)

        assert SENTINEL not in caplog.text
        assert safe_stream.getvalue() == ""
    finally:
        for dependency, original in zip(dependencies, originals, strict=True):
            handlers, level, propagate, disabled = original
            dependency.handlers = handlers
            dependency.setLevel(level)
            dependency.propagate = propagate
            dependency.disabled = disabled


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("level", True, id="boolean-level"),
        pytest.param(
            "correlation_id",
            RuntimeError(SENTINEL),
            id="exception-correlation-id",
        ),
        pytest.param("operation", SENTINEL, id="string-operation"),
        pytest.param("outcome_code", SENTINEL, id="unsafe-outcome-code"),
        pytest.param("outcome_code", "password123", id="secret-shaped-outcome"),
        pytest.param("duration_ms", True, id="boolean-duration"),
        pytest.param("duration_ms", float("nan"), id="non-finite-duration"),
        pytest.param("duration_ms", 10**400, id="unrepresentable-duration"),
        pytest.param("dependency", SENTINEL, id="unsafe-dependency"),
        pytest.param("dependency", "secret_token", id="secret-shaped-dependency"),
        pytest.param("failure_code", SENTINEL, id="unsafe-runtime-failure"),
        pytest.param(
            "failure_code",
            "password123",
            id="secret-shaped-runtime-failure",
        ),
        pytest.param("retry_attempt", True, id="boolean-retry-attempt"),
        pytest.param("retry_attempt", -1, id="negative-retry-attempt"),
        pytest.param(
            "exception_type",
            RuntimeError(SENTINEL),
            id="exception-as-exception-type",
        ),
        pytest.param(
            "stack_location",
            f"/tmp/{SENTINEL}.py:42",
            id="absolute-stack-path",
        ),
        pytest.param(
            "stack_location",
            f"{SENTINEL}.py:42",
            id="unsafe-stack-basename",
        ),
        pytest.param("failure_shape", SENTINEL, id="unsafe-failure-shape"),
        pytest.param(
            "failure_shape",
            "password123",
            id="secret-shaped-failure-shape",
        ),
    ],
)
def test_safe_logger_rejects_unsafe_allowed_field_before_writing(
    field: str,
    value: object,
) -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    with pytest.raises(TypeError):
        cast(Any, logger).emit(
            LogEvent.REQUEST_COMPLETED,
            **{field: value},
        )

    assert stream.getvalue() == ""


def test_safe_logger_accepts_all_safe_field_forms() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    logger.emit(
        LogEvent.REQUEST_COMPLETED,
        transport=None,
        level=logging.WARNING,
        instance_id=CORRELATION_ID,
        correlation_id=CORRELATION_ID,
        operation=Operation.HEALTH_LIVE,
        outcome_code=OutcomeCode.INTERNAL_ERROR,
        duration_ms=1.25,
        dependency=Dependency.CATALOGUE,
        failure_code=RuntimeFailureCode.ALREADY_LOCKED,
        retry_attempt=0,
        exception_type="RuntimeError",
        stack_location="middleware.py:42",
        failure_shape=BulkDemotionShape.ANSWER_LENGTH_MISMATCH,
    )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "request_completed"
    assert payload["instance_id"] == str(CORRELATION_ID)
    assert payload["correlation_id"] == str(CORRELATION_ID)
    assert payload["operation"] == "health_live"
    assert payload["outcome_code"] == "internal_error"
    assert payload["duration_ms"] == 1.25
    assert payload["dependency"] == "catalogue"
    assert payload["failure_code"] == "already_locked"
    assert payload["retry_attempt"] == 0
    assert payload["exception_type"] == "RuntimeError"
    assert payload["stack_location"] == "middleware.py:42"
    assert payload["failure_shape"] == "answer_length_mismatch"


def test_safe_logger_accepts_the_projection_cache_events() -> None:
    # R4 (I-32 amendment, 29 August 2026): four cache events with closed
    # kind/reason enums and bounded non-negative counters — never a table
    # name, cache key, payload or exception text.
    stream = StringIO()
    logger = configure_logging(stream)

    logger.emit(
        LogEvent.PROJECTION_CACHE_MISS,
        transport=None,
        level=logging.WARNING,
        cache_kind=CacheKind.TIMESTAMPS,
        cache_reason=CacheReason.TIMESTAMP_CALL_FAILED,
    )
    logger.emit(
        LogEvent.PROJECTION_CACHE_WRITE_DROPPED,
        transport=None,
        level=logging.WARNING,
        cache_kind=CacheKind.EXTRACTION,
        cache_rows=3,
    )
    logger.emit(
        LogEvent.PROJECTION_EXTRACTION_CACHE_BATCH,
        transport=None,
        cache_hits=1,
        cache_misses=2,
    )
    logger.emit(
        LogEvent.PROJECTION_EMBEDDING_CACHE_BATCH,
        transport=None,
        cache_hits=4,
        cache_misses=0,
    )

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [payload["event"] for payload in payloads] == [
        "projection_cache_miss",
        "projection_cache_write_dropped",
        "projection_extraction_cache_batch",
        "projection_embedding_cache_batch",
    ]
    assert payloads[0]["cache_kind"] == "timestamps"
    assert payloads[0]["cache_reason"] == "timestamp_call_failed"
    assert payloads[1]["cache_kind"] == "extraction"
    assert payloads[1]["cache_rows"] == 3
    assert payloads[2] == {
        "event": "projection_extraction_cache_batch",
        "cache_hits": 1,
        "cache_misses": 2,
        "time": payloads[2]["time"],
    }
    assert payloads[3]["cache_hits"] == 4
    assert payloads[3]["cache_misses"] == 0


def test_safe_logger_emits_the_closed_edge_batch_item_missing_event() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    logger.emit(
        LogEvent.EDGE_BATCH_ITEM_MISSING,
        transport=None,
        level=logging.WARNING,
        item=7,
        size=8,
        answered=7,
    )

    payload = json.loads(stream.getvalue())
    assert payload == {
        "answered": 7,
        "event": "edge_batch_item_missing",
        "item": 7,
        "size": 8,
        "time": payload["time"],
    }
    assert SENTINEL not in stream.getvalue()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item", True),
        ("item", -1),
        ("size", "8"),
        ("answered", 1.5),
    ],
)
def test_safe_logger_rejects_unsafe_edge_batch_counts(
    field: str, value: object
) -> None:
    stream = StringIO()
    logger = configure_logging(stream)
    fields: dict[str, object] = {"item": 7, "size": 8, "answered": 7}
    fields[field] = value

    with pytest.raises(TypeError):
        cast(Any, logger).emit(
            LogEvent.EDGE_BATCH_ITEM_MISSING,
            transport=None,
            **fields,
        )

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"size": 8, "answered": 7},
        {"item": 7, "answered": 7},
        {"item": 7, "size": 8},
        {"item": 7, "size": 8, "answered": 7, "cache_hits": 0},
        {
            "item": 7,
            "size": 8,
            "answered": 7,
            "outcome_code": OutcomeCode.SUCCESS,
        },
        {"item": 7, "size": 8, "answered": 7, "transport": Transport.REST},
    ],
)
def test_edge_batch_item_missing_rejects_missing_or_unrelated_fields(
    fields: dict[str, object],
) -> None:
    stream = StringIO()
    logger = configure_logging(stream)
    arguments = dict(fields)
    transport = arguments.pop("transport", None)

    with pytest.raises(TypeError):
        cast(Any, logger).emit(
            LogEvent.EDGE_BATCH_ITEM_MISSING,
            transport=transport,
            **arguments,
        )

    assert stream.getvalue() == ""


def test_edge_batch_counts_are_refused_on_every_other_event() -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    with pytest.raises(TypeError):
        logger.emit(
            LogEvent.REQUEST_COMPLETED,
            transport=None,
            item=7,
            size=8,
            answered=7,
        )

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cache_kind", "extraction"),
        ("cache_kind", 1),
        ("cache_reason", "read_failed"),
        ("cache_hits", -1),
        ("cache_misses", "2"),
        ("cache_rows", 1.5),
    ],
)
def test_safe_logger_rejects_unsafe_cache_fields(field: str, value: object) -> None:
    stream = StringIO()
    logger = configure_logging(stream)

    with pytest.raises(TypeError):
        cast(Any, logger).emit(
            LogEvent.PROJECTION_CACHE_MISS,
            transport=None,
            **{field: value},
        )

    assert stream.getvalue() == ""


def test_metrics_reject_unknown_operation_label() -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        metrics.observe_request(
            cast(Any, "submitted_text"),
            transport=Transport.REST,
            outcome_code=OutcomeCode.SUCCESS,
            duration_ms=1.0,
        )


def test_metrics_render_has_no_identity_or_submitted_text() -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        cast(Any, metrics).observe_request(
            Operation.HEALTH_LIVE,
            transport=Transport.REST,
            outcome_code=OutcomeCode.SUCCESS,
            duration_ms=1.0,
            instance_id=CORRELATION_ID,
        )
    with pytest.raises(TypeError):
        cast(Any, metrics).observe_request(
            Operation.HEALTH_LIVE,
            transport=Transport.REST,
            outcome_code=OutcomeCode.SUCCESS,
            duration_ms=1.0,
            submitted_text=SENTINEL,
        )
    metrics.observe_request(
        Operation.HEALTH_LIVE,
        transport=Transport.REST,
        outcome_code=OutcomeCode.SUCCESS,
        duration_ms=1.0,
    )
    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")

    # Rendered alphabetically by the client, not in declaration order.
    assert (
        text.count('operation="health_live",outcome_code="success",transport="rest"')
        >= 2
    )
    assert SENTINEL not in text
    assert str(CORRELATION_ID) not in text


@pytest.mark.parametrize("duration_ms", [True, float("nan"), -1.0, 10**400])
def test_metrics_rejects_invalid_duration(duration_ms: object) -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        metrics.observe_request(
            Operation.HEALTH_LIVE,
            transport=Transport.REST,
            outcome_code=OutcomeCode.SUCCESS,
            duration_ms=cast(Any, duration_ms),
        )
