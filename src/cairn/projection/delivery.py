"""Projection delivery (P-40): drains ``projection_outbox`` into the
retrieval index with durable, at-least-once retry.

``cairn.evidence.delivery``'s clauses, restated for this queue, because
I-78 makes them the same contract: a library function only, under the
single-consumer outbox rule (I-25, I-68, I-78); its own dedicated
non-reentrant gate, held for the whole run and deliberately *not* the
catalogue writer gate, which ``execute_compound`` takes itself; each
attempt calls the index outside any catalogue transaction and records the
outcome through a separate short confirming transaction, conditioned on
the ``(work_id, attempts)`` pair observed when the row was read.

Two things differ from the evidence queue, both from I-78:

*Ordering is by ``sequence``*, the column migration 0004 added, rather
than by ``created_at``. That is the whole reason the column exists: two
rows written in one transaction share a timestamp, so ``created_at``
alone leaves their order undefined.

*Rows are content-free* (I-68), so the batch reads the fact's **current**
catalogue state in the same short transaction that fetches the row, and
projects that. A fact invalidated between enqueue and delivery therefore
projects as invalidated, and all four work kinds — ingested, promoted,
invalidated, rebuild — are one ``project`` call, because what is
delivered is state rather than an event. An invalidated fact is still projected, never
deleted (P-38): I-81 must find it for an ``as_of`` before its
invalidation, and visibility is reconciliation's decision.

Delivery is operational work: it appends no audit events and emits only
safe logs and metrics. Scope paths never reach either (I-32) — the
partition encoding travels to the adapter and no further.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from cairn.catalogue.audit import (
    AuditValueError,
    Classification,
    ScopeSegment,
    TrustClass,
)
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions, CompoundTransaction
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.partition import canonical_partition
from cairn.runtime.logging import (
    BulkDemotionShape,
    LogEvent,
    SafeLogger,
    is_safe_outbox_identity,
)

_INDEX_UNAVAILABLE = "index_unavailable"
_INDEX_REFUSED = "index_refused"
# ck_projection_outbox_last_failure_code (migration 0003): an adapter's own
# ProjectionFailed.code is caller data until it has passed this, and a code
# the CHECK rejects would abort the confirming transaction — losing the
# retry record over a badly behaved adapter.
_SAFE_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,61}[a-z0-9]\Z|[a-z]\Z")

# Process-local, non-reentrant, and distinct from the evidence queue's
# gate: the two queues drain independently, and one blocking the other
# would make a stalled index delay evidence custody it has no part in.
_DELIVERY_GATE = threading.Lock()


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    delivered: int
    failed: int
    remaining: int


@dataclass(frozen=True, slots=True)
class _ChunkEntry:
    """One fact within a chunk: the state projected once, and every
    outbox row that projection satisfies (I-68 rows are content-free and
    the batch reads current state in one query, so rows sharing a fact
    share a state)."""

    state: ProjectedFactState
    rows: tuple[tuple[UUID, int], ...]


def _chunk_batch(
    batch: tuple[tuple[UUID, int, ProjectedFactState], ...],
    chunk_size: int,
) -> tuple[tuple[_ChunkEntry, ...], ...]:
    """Cut the sequence-ordered batch into contiguous chunks: a chunk
    grows while the partition stays the same and the cap is unreached,
    and every partition change cuts a new one — so confirms happen in
    batch (``sequence``) order, per I-78. Only an *adjacent* run of rows
    sharing a fact collapses into one entry — collapsing a later
    re-appearance into an earlier entry would confirm its row out of
    order — so a fact re-appearing after other rows cuts a new chunk and
    projects once per appearance, which idempotent projection makes
    safe."""
    chunks: list[tuple[_ChunkEntry, ...]] = []
    current: list[tuple[ProjectedFactState, list[tuple[UUID, int]]]] = []
    current_facts: set[UUID] = set()
    current_partition: str | None = None

    def cut() -> None:
        if current:
            chunks.append(
                tuple(
                    _ChunkEntry(state=state, rows=tuple(rows))
                    for state, rows in current
                )
            )
            current.clear()
            current_facts.clear()

    for work_id, attempts, state in batch:
        if state.partition_key != current_partition:
            cut()
            current_partition = state.partition_key
        if current and current[-1][0].fact_id == state.fact_id:
            current[-1][1].append((work_id, attempts))
            continue
        if state.fact_id in current_facts or len(current) >= chunk_size:
            cut()
        current.append((state, [(work_id, attempts)]))
        current_facts.add(state.fact_id)
    cut()
    return tuple(chunks)


def _record_outcome(
    transactions: CatalogueTransactions,
    work_id: UUID,
    attempts: int,
    clock: Callable[[], datetime],
    result: object,
) -> tuple[int, int]:
    """The single place a projection outcome becomes a confirm: exactly the
    existing loop's rules, extracted so the per-fact, chunked and fallback
    paths cannot drift. Anything that is not an explicit ``FactProjected``
    or ``ProjectionFailed`` — including an exception's sentinel — records
    as ``index_unavailable`` (fail-closed)."""
    if isinstance(result, FactProjected):
        if _confirm_delivered(transactions, work_id, attempts):
            return (1, 0)
        return (0, 0)
    failure_code = (
        _safe_failure_code(result)
        if isinstance(result, ProjectionFailed)
        else _INDEX_UNAVAILABLE
    )
    if _confirm_failure(transactions, work_id, attempts, clock, failure_code):
        return (0, 1)
    return (0, 0)


def _deliver_chunk(
    transactions: CatalogueTransactions,
    index: IndexAdapter,
    chunk: tuple[_ChunkEntry, ...],
    clock: Callable[[], datetime],
    logger: SafeLogger | None,
) -> tuple[int, int]:
    """One bulk attempt for one partition's chunk. Anything short of an
    aligned per-state answer — an exception, a wrong length, a non-tuple,
    an element that is not an explicit ``FactProjected``/``ProjectionFailed`` —
    demotes the whole chunk to the per-fact path. Recovery does not rely
    on ``project_many`` being atomic — a failed or misaligned call may
    leave the underlying store partially applied — it relies only on each
    state's projection being idempotent and resumable, which the per-fact
    path re-establishes one confirm at a time.

    Every demotion emits ``projection_bulk_demoted`` (P-82 gate-4 ruling,
    25 August 2026): the fallback still delivers, so without the event the
    only operator-visible symptom is unexplained slowness — the extraction
    work the bulk attempt already paid for is paid a second time, fact by
    fact."""
    states = tuple(entry.state for entry in chunk)
    try:
        results = index.project_many(states)
    except Exception as error:
        emit_bulk_demoted(logger, len(chunk), error, BulkDemotionShape.ADAPTER_RAISED)
        return _deliver_fallback(transactions, index, chunk, clock)
    if (
        not isinstance(results, tuple)
        or len(results) != len(states)
        or not all(
            isinstance(result, (FactProjected, ProjectionFailed)) for result in results
        )
    ):
        emit_bulk_demoted(
            logger, len(chunk), None, bulk_answer_shape(results, len(states))
        )
        return _deliver_fallback(transactions, index, chunk, clock)
    delivered = 0
    failed = 0
    for entry, result in zip(chunk, results, strict=True):
        for work_id, attempts in entry.rows:
            d, f = _record_outcome(transactions, work_id, attempts, clock, result)
            delivered += d
            failed += f
    return (delivered, failed)


def _deliver_fallback(
    transactions: CatalogueTransactions,
    index: IndexAdapter,
    chunk: tuple[_ChunkEntry, ...],
    clock: Callable[[], datetime],
) -> tuple[int, int]:
    delivered = 0
    failed = 0
    for entry in chunk:
        try:
            result: object = index.project(entry.state)
        except Exception:
            result = None
        for work_id, attempts in entry.rows:
            d, f = _record_outcome(transactions, work_id, attempts, clock, result)
            delivered += d
            failed += f
    return (delivered, failed)


def deliver_projection_outbox(
    transactions: CatalogueTransactions,
    index: IndexAdapter,
    *,
    clock: Callable[[], datetime],
    limit: int = 100,
    chunk_size: int = 1,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> DeliveryReport:
    with _DELIVERY_GATE:
        raw_batch = transactions.execute_compound(lambda tx: _fetch_batch(tx, limit))
        batch, unreadable_work_ids = _parse_batch(raw_batch)
        for raw_work_id in unreadable_work_ids:
            _emit_unreadable_row(logger, raw_work_id)

        delivered = 0
        failed = 0
        if chunk_size <= 1:
            for work_id, attempts, state in batch:
                try:
                    result: object = index.project(state)
                except Exception:
                    result = None
                d, f = _record_outcome(transactions, work_id, attempts, clock, result)
                delivered += d
                failed += f
        else:
            for chunk in _chunk_batch(batch, chunk_size):
                d, f = _deliver_chunk(transactions, index, chunk, clock, logger)
                delivered += d
                failed += f

        remaining, oldest_created_at = transactions.execute_compound(_remaining_state)

    report = DeliveryReport(delivered=delivered, failed=failed, remaining=remaining)
    if metrics is not None:
        # As in the evidence deliverer: durable work has already committed,
        # so an unreadable created_at costs the run its age gauge, never its
        # report. Depth is exact regardless — COUNT(*) parses nothing.
        try:
            oldest_age_seconds = _oldest_age_seconds(oldest_created_at, clock())
        except CatalogueStorageError:
            _emit_unreadable_age(logger)
        else:
            metrics.set_outbox_state(
                OutboxQueue.PROJECTION,
                depth=remaining,
                oldest_age_seconds=oldest_age_seconds,
            )
    if logger is not None:
        logger.emit(
            LogEvent.PROJECTION_DELIVERY_COMPLETED,
            transport=None,
            delivered=report.delivered,
            failed=report.failed,
            remaining=report.remaining,
        )
    return report


def _fetch_batch(tx: CompoundTransaction, limit: int) -> tuple[tuple[object, ...], ...]:
    """One query takes the row and the fact's current state together, in
    the same short transaction, so what is projected is what the catalogue
    holds now (I-68) rather than what it held at enqueue.

    ``fk_projection_outbox_fact`` makes the join total, so a missing fact
    is unreachable and no rule is invented for it. The ``LEFT JOIN`` onto
    ``fact_invalidations`` is what carries P-38's invalidation state.

    Ordering is ``sequence`` (I-78). Every stored column read here is
    shape-CHECKed only, so ``_parse_batch`` guards the meaning — the same
    gap ``cairn.evidence.delivery`` documents at length.
    """
    return tx.query(
        "SELECT p.work_id, p.attempts, f.fact_id, f.realm_id, f.scope_segments, "
        "f.body, f.classification, f.trust, f.recorded_at, f.valid_from, "
        "f.valid_to, i.invalidated_at "
        "FROM projection_outbox p "
        "JOIN facts f ON f.fact_id = p.fact_id "
        "LEFT JOIN fact_invalidations i ON i.fact_id = f.fact_id "
        "ORDER BY p.sequence ASC LIMIT ?",
        (limit,),
    )


def _parse_batch(
    raw_batch: tuple[tuple[object, ...], ...],
) -> tuple[
    tuple[tuple[UUID, int, ProjectedFactState], ...],
    tuple[str | None, ...],
]:
    """Splits the batch into rows whose stored values all parse and the
    raw ``work_id`` of every row that does not — or ``None`` where even
    that is unsafe to log.

    A row in the unreadable group is never handed to the index: there is
    no trustworthy state to project. It is neither delivered nor failed,
    stays in the outbox, and is counted by the next ``_remaining_state``,
    exactly as its evidence-queue counterpart is.
    """
    parsed: list[tuple[UUID, int, ProjectedFactState]] = []
    unreadable: list[str | None] = []
    for row in raw_batch:
        raw_work_id = row[0]
        try:
            parsed.append(_parse_row(row))
        except (ValueError, TypeError, AuditValueError, CatalogueStorageError):
            safe = (
                raw_work_id
                if type(raw_work_id) is str and is_safe_outbox_identity(raw_work_id)
                else None
            )
            unreadable.append(safe)
    return tuple(parsed), tuple(unreadable)


def _parse_row(row: tuple[object, ...]) -> tuple[UUID, int, ProjectedFactState]:
    (
        work_id,
        attempts,
        fact_id,
        realm_id,
        scope_segments,
        body,
        classification,
        trust,
        recorded_at,
        valid_from,
        valid_to,
        invalidated_at,
    ) = row
    if type(attempts) is not int or type(body) is not str:
        raise TypeError("projection row carries a non-textual body or attempts")
    if type(realm_id) is not str or type(scope_segments) is not str:
        raise TypeError("projection row carries a non-textual scope")
    if type(classification) is not str or type(trust) is not str:
        raise TypeError("projection row carries a non-textual trust or class")
    return (
        UUID(cast(str, work_id)),
        attempts,
        ProjectedFactState(
            fact_id=UUID(cast(str, fact_id)),
            # The stored column *is* the canonical JSON — every writer goes
            # through the authority module's single encoder — so the
            # partition encoding is assembled from it directly rather than
            # re-serialised from parsed segments, which could differ if the
            # two encoders ever drifted.
            partition_key=canonical_partition(realm_id, scope_segments),
            body=body,
            realm_id=realm_id,
            segments=_stored_segments(scope_segments),
            classification=Classification(classification),
            trust=TrustClass(trust),
            recorded_at=parse_timestamp(cast(str, recorded_at)),
            valid_from=_optional_timestamp(valid_from),
            valid_to=_optional_timestamp(valid_to),
            invalidated_at=_optional_timestamp(invalidated_at),
        ),
    )


def _stored_segments(value: str) -> tuple[ScopeSegment, ...]:
    """Mirrors ``cairn.evidence.reconciliation._stored_segments``: the
    schema pins minified-JSON-array shape and nothing about each element,
    so ``[{"kind": 7, "id": 1}]`` is a legal row whose construction must
    fail as a typed ``AuditValueError`` rather than a bare ``TypeError``
    escaping from ``re``."""
    documents = json.loads(value)
    if type(documents) is not list:
        raise AuditValueError("invalid_scope")
    segments: list[ScopeSegment] = []
    for document in documents:
        if type(document) is not dict or set(document) != {"kind", "id"}:
            raise AuditValueError("invalid_scope")
        segments.append(ScopeSegment(kind=document["kind"], identifier=document["id"]))
    return tuple(segments)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("timestamp column is not textual")
    return parse_timestamp(value)


def _safe_failure_code(result: ProjectionFailed) -> str:
    code = result.code
    if type(code) is not str or _SAFE_FAILURE_CODE.fullmatch(code) is None:
        # The adapter refused, which is real and must be recorded; only its
        # spelling is untrustworthy, so the refusal is kept under this
        # module's own code rather than discarded.
        return _INDEX_REFUSED
    return code


def bulk_answer_shape(results: object, expected: int) -> BulkDemotionShape:
    """Public to this package: classifies a bulk answer the demotion check
    has already rejected, in the same order that check tests it. A
    malformed answer carries no exception, so this is the only thing that
    tells an operator which adapter defect they are looking at — a
    non-tuple, a misalignment or an element that is not an explicit
    result. It never reads a fact, a partition or an element's value."""
    if not isinstance(results, tuple):
        return BulkDemotionShape.ANSWER_NOT_A_TUPLE
    if len(results) != expected:
        return BulkDemotionShape.ANSWER_LENGTH_MISMATCH
    return BulkDemotionShape.ANSWER_ELEMENT_INVALID


def emit_bulk_demoted(
    logger: SafeLogger | None,
    chunk_size: int,
    error: Exception | None,
    shape: BulkDemotionShape,
) -> None:
    """Public to this package: ``cairn.projection.rebuild``'s chunked loop
    demotes the same way and must sound the same. Every demotion carries
    the chunk size and a partition-free failure shape (P-82 gate-4
    ruling); ``error`` additionally names the exception class where one
    demoted the chunk, and is ``None`` for a malformed bulk answer.

    ``adapter_code`` carries the adapter error's safe ``code`` identifier
    when it has one — six demotions in eight measured runs said only
    ``adapter_raised`` because this event withheld the one field that
    distinguishes a timeout from an unavailable index. Duck-typed rather
    than importing the adapter (the port stays adapter-agnostic), and
    emitted only when it already looks like a safe identifier, so a
    foreign exception's ``code`` cannot smuggle content into the log."""
    if logger is not None:
        code = getattr(error, "code", None)
        adapter_code = (
            code
            if isinstance(code, str) and _SAFE_FAILURE_CODE.fullmatch(code)
            else None
        )
        logger.emit(
            LogEvent.PROJECTION_BULK_DEMOTED,
            level=logging.WARNING,
            transport=None,
            chunk_size=chunk_size,
            exception_type=type(error).__name__ if error is not None else None,
            failure_shape=shape,
            adapter_code=adapter_code,
        )


def _emit_unreadable_row(logger: SafeLogger | None, work_id: str | None) -> None:
    if logger is not None:
        logger.emit(
            LogEvent.PROJECTION_OUTBOX_ROW_UNREADABLE, work_id=work_id, transport=None
        )


def _emit_unreadable_age(logger: SafeLogger | None) -> None:
    if logger is not None:
        logger.emit(LogEvent.PROJECTION_OUTBOX_AGE_UNREADABLE, transport=None)


def _remaining_state(tx: CompoundTransaction) -> tuple[int, str | None]:
    rows = tx.query("SELECT COUNT(*), MIN(created_at) FROM projection_outbox")
    return cast(tuple[int, str | None], rows[0])


def _oldest_age_seconds(oldest_created_at: str | None, now: datetime) -> float:
    if oldest_created_at is None:
        return 0.0
    # Clamped for the reason the evidence deliverer documents: a clock that
    # stepped backwards between the write and this read is ordinary
    # operational reality, and a negative age would cost the run its report.
    return max(0.0, (now - parse_timestamp(oldest_created_at)).total_seconds())


def _confirm_delivered(
    transactions: CatalogueTransactions, work_id: UUID, attempts: int
) -> bool:
    def work(tx: CompoundTransaction) -> bool:
        rows = tx.query(
            "DELETE FROM projection_outbox WHERE work_id = ? AND attempts = ? "
            "RETURNING work_id",
            (str(work_id), attempts),
        )
        return len(rows) == 1

    return transactions.execute_compound(work)


def _confirm_failure(
    transactions: CatalogueTransactions,
    work_id: UUID,
    attempts: int,
    clock: Callable[[], datetime],
    failure_code: str,
) -> bool:
    def work(tx: CompoundTransaction) -> bool:
        rows = tx.query(
            "UPDATE projection_outbox SET attempts = attempts + 1, "
            "last_attempt_at = ?, last_failure_code = ? "
            "WHERE work_id = ? AND attempts = ? RETURNING work_id",
            (canonical_timestamp(clock()), failure_code, str(work_id), attempts),
        )
        return len(rows) == 1

    return transactions.execute_compound(work)
