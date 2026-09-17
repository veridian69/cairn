import math
from enum import StrEnum

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from cairn.runtime.logging import Operation, OutcomeCode, Transport


class OutboxQueue(StrEnum):
    EVIDENCE = "evidence"
    PROJECTION = "projection"


class Metrics:
    def __init__(self) -> None:
        self._registry = CollectorRegistry()
        self._requests = Counter(
            "cairn_http_requests",
            "Cairn HTTP requests by operation, transport and stable outcome.",
            ("operation", "transport", "outcome_code"),
            registry=self._registry,
        )
        self._request_duration = Histogram(
            "cairn_http_request_duration_seconds",
            "Cairn HTTP request duration by operation, transport and stable outcome.",
            ("operation", "transport", "outcome_code"),
            registry=self._registry,
        )
        self._evidence_digest_mismatch = Counter(
            "cairn_evidence_digest_mismatch_total",
            "Evidence reconciliation candidates whose fetched payload digest "
            "did not match the catalogue's recorded digest (I-69).",
            registry=self._registry,
        )
        self._stale_index = Counter(
            "cairn_stale_index_total",
            "Retrieval reconciliation candidates naming a fact identity the "
            "catalogue has never held — evidence of index corruption rather "
            "than lag (I-83, P-47).",
            registry=self._registry,
        )
        self._outbox_depth = Gauge(
            "cairn_outbox_depth",
            "Pending rows in a Cairn outbox queue (P-18).",
            ("queue",),
            registry=self._registry,
        )
        self._outbox_oldest_age = Gauge(
            "cairn_outbox_oldest_age_seconds",
            "Age in seconds of the oldest pending row in a Cairn outbox queue (P-18).",
            ("queue",),
            registry=self._registry,
        )

    def observe_request(
        self,
        operation: Operation,
        *,
        transport: Transport,
        outcome_code: OutcomeCode,
        duration_ms: float,
    ) -> None:
        """One request, labelled by the surface that served it.

        ``transport`` is keyword-only and **undefaulted** (P-57). A default
        of ``rest`` would silently re-label every MCP request the one time
        a caller forgot to pass it, which is precisely the confusion this
        dimension exists to prevent — and it would do so invisibly, since
        the resulting series is indistinguishable from real REST traffic.
        """
        if type(operation) is not Operation:
            raise TypeError("operation must be an Operation")
        if type(transport) is not Transport:
            raise TypeError("transport must be a Transport")
        if type(outcome_code) is not OutcomeCode:
            raise TypeError("outcome_code must be an OutcomeCode")
        if type(duration_ms) not in {int, float}:
            raise TypeError("duration_ms must be a finite non-negative number")
        try:
            safe_duration_ms = float(duration_ms)
        except OverflowError:
            raise TypeError(
                "duration_ms must be a finite non-negative number"
            ) from None
        if not math.isfinite(safe_duration_ms) or safe_duration_ms < 0:
            raise TypeError("duration_ms must be a finite non-negative number")
        labels = {
            "operation": operation.value,
            "transport": transport.value,
            "outcome_code": outcome_code.value,
        }
        self._requests.labels(**labels).inc()
        self._request_duration.labels(**labels).observe(safe_duration_ms / 1000)

    def observe_evidence_digest_mismatch(self) -> None:
        self._evidence_digest_mismatch.inc()

    def observe_stale_index(self) -> None:
        self._stale_index.inc()

    def set_outbox_state(
        self,
        queue: OutboxQueue,
        *,
        depth: int,
        oldest_age_seconds: float,
    ) -> None:
        if type(queue) is not OutboxQueue:
            raise TypeError("queue must be an OutboxQueue")
        if type(depth) is not int or depth < 0:
            raise TypeError("depth must be a non-negative integer")
        if type(oldest_age_seconds) not in {int, float}:
            raise TypeError("oldest_age_seconds must be a finite non-negative number")
        try:
            safe_age_seconds = float(oldest_age_seconds)
        except OverflowError:
            raise TypeError(
                "oldest_age_seconds must be a finite non-negative number"
            ) from None
        if not math.isfinite(safe_age_seconds) or safe_age_seconds < 0:
            raise TypeError("oldest_age_seconds must be a finite non-negative number")
        self._outbox_depth.labels(queue=queue.value).set(depth)
        self._outbox_oldest_age.labels(queue=queue.value).set(safe_age_seconds)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self._registry), CONTENT_TYPE_LATEST
