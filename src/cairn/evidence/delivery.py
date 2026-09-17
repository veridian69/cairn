"""Evidence delivery (P-15): drains ``evidence_outbox`` into Attic with
durable, at-least-once retry.

A library function only — slice 4 ships no daemon, timer or transport hook;
runtime wiring is a slice 5/6 concern. Each attempt calls ``attic.store``
outside any catalogue transaction, then records the outcome through a
separate, short confirming transaction: on success the outbox row is
re-checked and deleted; on failure ``attempts``, ``last_attempt_at`` and
``last_failure_code`` are updated. A crash between store and confirmation is
safe because ``store`` is idempotent by identity (I-69) — a successfully
stored payload with an unconfirmed row is harmless and simply retried.

Outbox delivery is single-consumer. Concurrency is excluded structurally:
cross-process by a caller-held data-directory lease (a precondition this
function cannot enforce itself — the signature has nowhere to take one, so
it is documented here rather than checked), in-process by the dedicated
non-reentrant delivery gate below, held for the whole run. The gate is
deliberately not the catalogue writer gate: holding it across Attic I/O
would block mutations, and ``execute_compound`` acquires the writer gate
itself, so a run already holding it would deadlock on the non-reentrant
lock. As defence in depth, the confirming delete and the retry-state update
are conditional on the ``(work_id, attempts)`` pair observed when the row
was read; zero affected rows means another deliverer won that row, and the
run skips it without treating that as failure.

Delivery is operational work: it appends no audit events and emits only
safe logs and metrics.

The batch fetch has no identity filter — it scans oldest-first regardless
of content — so a row whose ``work_id``/``evidence_id`` is schema-legal but
unparseable as a UUID (the shape CHECK permits a dash where UUID() requires
a hex digit) is genuinely reachable here, unlike a lookup keyed by an
already-validated identity. Such a row is discarded rather than raised: it
is never passed to Attic, is neither delivered nor failed, and is logged
with its raw work_id so an operator can find it, without ever stopping the
rest of the run. See ``_parse_batch``.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions, CompoundTransaction
from cairn.evidence.adapter import AtticAdapter, PayloadCorrupt, PayloadStored
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.runtime.logging import LogEvent, SafeLogger, is_safe_outbox_identity

_ATTIC_UNAVAILABLE = "attic_unavailable"
_ATTIC_CORRUPTION = "attic_corruption"

# Process-local, non-reentrant: P-15 requires a dedicated delivery gate, held
# for the whole run, distinct from the catalogue writer gate (see module
# docstring for why re-entrancy or gate-sharing would each be wrong here).
_DELIVERY_GATE = threading.Lock()


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    delivered: int
    failed: int
    remaining: int


def deliver_evidence_outbox(
    transactions: CatalogueTransactions,
    attic: AtticAdapter,
    *,
    clock: Callable[[], datetime],
    limit: int = 100,
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
        for work_id, evidence_id, payload, attempts in batch:
            try:
                result = attic.store(evidence_id, payload)
            except Exception:
                if _confirm_failure(
                    transactions, work_id, attempts, clock, _ATTIC_UNAVAILABLE
                ):
                    failed += 1
                continue
            if isinstance(result, PayloadStored):
                if _confirm_delivered(transactions, work_id, attempts):
                    delivered += 1
                continue
            # Fail-closed: only an explicit PayloadStored counts as success.
            # PayloadCorrupt keeps its own failure code; anything else —
            # including a value the AtticAdapter Protocol doesn't actually
            # enforce at runtime — is treated as unavailable rather than
            # trusted, since store is the one place the only copy of the
            # payload bytes changes hands (I-69: every adapter is untrusted).
            failure_code = (
                _ATTIC_CORRUPTION
                if isinstance(result, PayloadCorrupt)
                else _ATTIC_UNAVAILABLE
            )
            if _confirm_failure(transactions, work_id, attempts, clock, failure_code):
                failed += 1

        # One query serves both the report and the gauge: remaining is a
        # fresh COUNT(*) so the two can never disagree, and a row retained
        # by a failed delivery (fail-closed above) is exactly as reachable
        # here as one still awaiting its first attempt — a retained failure
        # must still count toward depth and still contribute its
        # created_at, or the gauge would under-report the backlog an
        # operator most needs to see.
        remaining, oldest_created_at = transactions.execute_compound(_remaining_state)

    report = DeliveryReport(delivered=delivered, failed=failed, remaining=remaining)
    if metrics is not None:
        # By this point real durable work has already committed — deletes
        # for delivered rows, retry-state updates for failed ones — so a
        # corrupt created_at (the same shape-only-CHECK gap _remaining_state
        # documents) must not cost the caller the report or its completion
        # log: that would discard a mostly-successful run's summary over
        # corruption that can only affect the aggregate age, never a
        # delivery decision already made. Depth is still accurate (COUNT(*)
        # doesn't parse anything) and is not withheld; age is left alone
        # rather than substituted with 0, which the plan reserves to mean
        # "empty queue", a different state to "unreadable".
        try:
            oldest_age_seconds = _oldest_age_seconds(oldest_created_at, clock())
        except CatalogueStorageError:
            _emit_unreadable_age(logger)
        else:
            metrics.set_outbox_state(
                OutboxQueue.EVIDENCE,
                depth=remaining,
                oldest_age_seconds=oldest_age_seconds,
            )
    if logger is not None:
        logger.emit(
            LogEvent.EVIDENCE_DELIVERY_COMPLETED,
            transport=None,
            delivered=report.delivered,
            failed=report.failed,
            remaining=report.remaining,
        )
    return report


def _fetch_batch(
    tx: CompoundTransaction, limit: int
) -> tuple[tuple[str, str, bytes, int], ...]:
    # evidence_outbox's columns read here: work_id/evidence_id's GLOB CHECKs
    # (migration 0003) pin length and dash/version/variant positions, and
    # restrict every character to the class [0-9a-f-] — but that class
    # includes the dash itself, so a `?`-matched position can legally hold a
    # dash instead of a hex digit. The CHECK is therefore shape-only, not
    # meaning-constraining, and UUID() can raise on a schema-legal row; see
    # _parse_batch, the reading guard this gap requires. payload is an
    # opaque BLOB passed through unparsed; attempts is a plain
    # CHECK(>= 0) integer with no further structure to violate. created_at
    # drives ORDER BY in SQL directly rather than being parsed into a
    # datetime, so its own shape-only CHECK needs no reading guard here
    # either. This SELECT has no identity filter — it scans oldest-first
    # regardless of content — so every row's work_id/evidence_id is
    # genuinely reachable, unlike a lookup keyed by an already-validated
    # identity (see reconcile_evidence, which is exactly such a lookup and
    # needs no equivalent guard).
    rows = tx.query(
        "SELECT work_id, evidence_id, payload, attempts FROM evidence_outbox "
        "ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return cast(tuple[tuple[str, str, bytes, int], ...], rows)


def _parse_batch(
    raw_batch: tuple[tuple[str, str, bytes, int], ...],
) -> tuple[tuple[tuple[UUID, UUID, bytes, int], ...], tuple[str | None, ...]]:
    """Splits a raw batch into rows whose identities parse as real UUIDs and
    the raw work_id of every row that doesn't — or ``None`` in its place
    when even that raw value isn't safe to log (see
    ``is_safe_outbox_identity``): the schema's shape CHECK is supposed to
    bound it, but a catalogue restored from a pre-STRICT schema might not
    actually satisfy it, and this function must not depend on the logging
    layer to catch that — it must never hand the logger a value that could
    make ``emit`` raise. A row in the unreadable group is never passed to
    Attic at all — there is no identity left to trust it with — so it is
    neither delivered nor failed, just skipped; the caller logs it and it
    remains in the outbox, counted by the next ``_remaining_state``."""
    parsed: list[tuple[UUID, UUID, bytes, int]] = []
    unreadable: list[str | None] = []
    for work_id, evidence_id, payload, attempts in raw_batch:
        try:
            parsed.append((UUID(work_id), UUID(evidence_id), payload, attempts))
        except ValueError:
            unreadable.append(work_id if is_safe_outbox_identity(work_id) else None)
    return tuple(parsed), tuple(unreadable)


def _emit_unreadable_row(logger: SafeLogger | None, work_id: str | None) -> None:
    if logger is not None:
        logger.emit(
            LogEvent.EVIDENCE_OUTBOX_ROW_UNREADABLE, work_id=work_id, transport=None
        )


def _emit_unreadable_age(logger: SafeLogger | None) -> None:
    if logger is not None:
        logger.emit(LogEvent.EVIDENCE_OUTBOX_AGE_UNREADABLE, transport=None)


def _remaining_state(tx: CompoundTransaction) -> tuple[int, str | None]:
    # Reads the same created_at column _fetch_batch orders by, but this
    # query parses it (see _oldest_age_seconds) rather than only ordering
    # by it — so unlike _fetch_batch, this one does need the reading guard.
    # ck_evidence_outbox_created_at (migration 0003) pins length and the
    # dash/colon/dot/T/Z positions and restricts every character to the
    # class [0-9TZ:.-], but that class also permits a separator character
    # at a digit position, so e.g. '9999-99-99T99:99:99.999999Z' passes
    # the CHECK while being no real calendar timestamp: shape-only, not
    # meaning-constraining, the same gap as the identity GLOB CHECKs
    # _parse_batch guards against.
    rows = tx.query("SELECT COUNT(*), MIN(created_at) FROM evidence_outbox")
    return cast(tuple[int, str | None], rows[0])


def _oldest_age_seconds(oldest_created_at: str | None, now: datetime) -> float:
    if oldest_created_at is None:
        return 0.0
    # Clamped, because a future-dated row makes this negative and
    # set_outbox_state refuses a negative age with a TypeError — which would
    # escape here, after the run's deletes and retry updates have already
    # committed, and cost the caller its report over a gauge. A row can be
    # future-dated without any corruption at all: the timestamp is written by
    # whichever process ingested it, and a clock stepping backwards between
    # that write and this read is ordinary operational reality. Zero is the
    # honest reading of "nothing has been waiting", and it is the same value
    # an empty queue reports — unlike a corrupt created_at, which is
    # unreadable rather than young and is left to _emit_unreadable_age.
    return max(0.0, (now - parse_timestamp(oldest_created_at)).total_seconds())


def _confirm_delivered(
    transactions: CatalogueTransactions, work_id: UUID, attempts: int
) -> bool:
    def work(tx: CompoundTransaction) -> bool:
        rows = tx.query(
            "DELETE FROM evidence_outbox WHERE work_id = ? AND attempts = ? "
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
            "UPDATE evidence_outbox SET attempts = attempts + 1, "
            "last_attempt_at = ?, last_failure_code = ? "
            "WHERE work_id = ? AND attempts = ? RETURNING work_id",
            (canonical_timestamp(clock()), failure_code, str(work_id), attempts),
        )
        return len(rows) == 1

    return transactions.execute_compound(work)
