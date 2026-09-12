"""The I-25 background delivery loop (P-41).

One asynchronous task, started by the lifespan after startup verification
and cancelled at shutdown, draining both outboxes — evidence first, then
projection — every ``delivery.interval_seconds``. Slices 4 and 5 built the
deliverers and deferred the loop that calls them; this is that loop, and
nothing else. All delivery semantics stay in the two library functions,
which own their own non-reentrant gates.

Three properties this module exists to hold:

**A drain never kills the server.** Each pass is wrapped: a deliverer that
raises is logged safely and the loop sleeps and tries again. Delivery is
best-effort catch-up over durable rows, so the correct response to an
adapter outage is to retry later, not to take down an instance that is
still serving reads and accepting custody.

**Delivery runs off the event loop.** Both deliverers are synchronous and
take the writer gate, so they run under ``anyio.to_thread`` exactly as the
request write paths do (P-29). The shared ``CatalogueTransactions`` means
the loop's confirming writes queue on the same inner gate as everything
else rather than racing it.

**Shutdown is cancellation between passes, not mid-transaction.** Cancelling
the task scope interrupts the sleep; a pass already running in a worker
thread is not interrupted, because ``anyio.to_thread.run_sync`` without
cancellation support lets the thread finish. So the active confirming
transaction commits or rolls back on its own terms and undelivered rows stay
durable, which is what I-25 requires.

Readiness is deliberately unaffected by outbox depth: the ingest
acknowledgement proved custody, and searchability is a later, separate
promise. An instance with a deep outbox is behind, not unhealthy — and
I-83 already tells a *reader* when that lag would affect its answer.
"""

from collections.abc import Callable
from datetime import datetime

import anyio

from cairn.catalogue.transactions import CatalogueTransactions
from cairn.evidence.adapter import AtticAdapter
from cairn.evidence.delivery import deliver_evidence_outbox
from cairn.operations.metrics import Metrics
from cairn.projection.adapter import IndexAdapter
from cairn.projection.delivery import deliver_projection_outbox
from cairn.runtime.logging import LogEvent, SafeLogger


async def run_delivery_loop(
    transactions: CatalogueTransactions,
    *,
    attic: AtticAdapter | None,
    index: IndexAdapter | None,
    interval_seconds: float,
    clock: Callable[[], datetime],
    chunk_size: int = 1,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> None:
    """Drain both outboxes forever, one pass per interval.

    ``attic`` and ``index`` are ``None`` when their subsystem is disabled,
    and that queue is simply not drained — mirroring how absence, not a
    null object, represents a disabled adapter everywhere else (P-14). With
    the index disabled no projection rows are written either (P-39), so the
    skipped queue stays empty rather than growing unattended.

    Returns only by cancellation. With both adapters absent it still runs,
    sleeping: the caller's lifecycle is simpler for the task always
    existing, and an idle loop costs one sleeping task.

    P-86 measured the interval as a trough: at chunk 100 the corpus spent
    minutes asleep between passes that each had a full queue waiting. A
    pass that delivered something with rows still remaining therefore
    rolls straight into the next pass; the interval paces only an idle
    queue — or a stalled one, because remaining rows with *nothing*
    delivered is an adapter outage, and continuing immediately would
    hammer a dead adapter in a hot loop.
    """
    while True:
        delivered, remaining = await _drain_once(
            transactions,
            attic=attic,
            index=index,
            clock=clock,
            chunk_size=chunk_size,
            metrics=metrics,
            logger=logger,
        )
        if delivered > 0 and remaining > 0:
            continue
        await anyio.sleep(interval_seconds)


async def _drain_once(
    transactions: CatalogueTransactions,
    *,
    attic: AtticAdapter | None,
    index: IndexAdapter | None,
    clock: Callable[[], datetime],
    chunk_size: int = 1,
    metrics: Metrics | None,
    logger: SafeLogger | None,
) -> tuple[int, int]:
    """One pass over both queues. Evidence first: exact evidence is custody
    the catalogue has already promised, where a projection row only affects
    searchability, so the queue whose lag is more consequential drains
    first when a pass is cut short by cancellation.

    Returns the pass's total ``(delivered, remaining)`` across both queues
    so the loop can pace itself; a drain that raised contributes nothing to
    either, which reads as a stall and keeps the interval in force.
    """
    delivered = 0
    remaining = 0
    if attic is not None:
        evidence_report = await _guarded(
            lambda: deliver_evidence_outbox(
                transactions,
                attic,
                clock=clock,
                metrics=metrics,
                logger=logger,
            ),
            LogEvent.EVIDENCE_DELIVERY_FAILED,
            logger,
        )
        if evidence_report is not None:
            delivered += evidence_report.delivered
            remaining += evidence_report.remaining
    if index is not None:
        projection_report = await _guarded(
            lambda: deliver_projection_outbox(
                transactions,
                index,
                clock=clock,
                # P-86: the deliverer's own default fetch ceiling of 100
                # capped the effective chunk at min(chunk_size, 100); a
                # configured chunk above it now takes effect, and the
                # historic ceiling holds for anything smaller.
                limit=max(100, chunk_size),
                chunk_size=chunk_size,
                metrics=metrics,
                logger=logger,
            ),
            LogEvent.PROJECTION_DELIVERY_FAILED,
            logger,
        )
        if projection_report is not None:
            delivered += projection_report.delivered
            remaining += projection_report.remaining
    return delivered, remaining


async def _guarded[ReportT](
    drain: Callable[[], ReportT],
    event: LogEvent,
    logger: SafeLogger | None,
) -> ReportT | None:
    """Run one drain on a worker thread; log and swallow anything it raises.
    Returns the drain's report, or ``None`` when it raised.

    The catch is deliberately broad and deliberately not a bug-hider: the
    deliverers already convert every *expected* adapter failure into a
    recorded retryable attempt, so anything reaching here is unexpected —
    a defect, a corrupt row past its CHECK, an adapter breaking its
    contract. None of those is a reason to stop serving, and all of them
    are reasons an operator should see an event. ``exception_type`` is the
    class name only: I-32 forbids the message, which may carry scope paths
    or fact content.

    ``BaseException`` is not caught, so cancellation propagates and
    shutdown still works.
    """
    try:
        return await anyio.to_thread.run_sync(drain)
    except Exception as error:
        if logger is not None:
            logger.emit(event, exception_type=type(error).__name__, transport=None)
        return None
