"""P-45: rebuilding the retrieval index from the catalogue.

The index is derived state. The catalogue is authoritative, and every
projected fact can be recomputed from it, so losing the index — a wiped
FalkorDB, a restarted in-memory instance, an operator turning the feature
on after running without it — is a recoverable condition rather than data
loss. This module is that recovery, and it is the reason P-39's disabled
posture can decline to queue outbox rows at all.

CLI-only, by the I-39/I-63 precedent for procedures that must not be
network-reachable: it clears the whole index and re-projects everything,
which is exactly the shape of operation that should require a shell on the
host rather than a bearer token.

It takes the instance lease for the duration, so it cannot run against a
catalogue a serving instance owns. That is not only about write safety —
a rebuild racing a delivery loop would have the two disagreeing about
what the index should contain, and the loop would win for some facts and
lose for others, leaving a state neither produced.

Invalidated facts are projected too, with their invalidation state. I-81
point-in-time recall must still find them for an ``as_of`` before their
invalidation, so an index rebuilt without them would answer historical
queries differently from one built incrementally — the same divergence
P-38 forbids the deliverer from creating.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.catalogue.audit import (
    AuditValueError,
    Classification,
    ScopeSegment,
    TrustClass,
)
from cairn.catalogue.sqlite import (
    parse_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import CatalogueTransactions, CompoundTransaction
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.delivery import bulk_answer_shape, emit_bulk_demoted
from cairn.projection.partition import canonical_partition
from cairn.runtime.logging import BulkDemotionShape, SafeLogger


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """``superseded_rows`` counts pending rows the enqueue replaced — work
    the rebuild took over rather than work that was lost. Rows this pass
    could not discharge are still in the outbox when it returns, which is
    the point of them, so no count reports "kept": ``failed`` and
    ``unreadable`` already name them."""

    projected: int
    failed: int
    unreadable: int
    superseded_rows: int


def rebuild_index(
    data_path: Path,
    transactions: CatalogueTransactions,
    index: IndexAdapter,
    *,
    uuid_factory: Callable[[], UUID],
    batch_size: int = 500,
    chunk_size: int = 1,
    logger: SafeLogger | None = None,
) -> RebuildReport:
    """Enqueue rebuild work for every fact, clear the index, then
    re-project in ``recorded_at`` order, discharging each fact's row as it
    succeeds.

    The enqueue comes first and that ordering is the whole design. Clearing
    destroys index entries for every fact, but the outbox holds rows only
    for facts whose delivery has not yet happened — after a drained queue,
    none — so a re-projection that fails part way through would leave the
    index short with nothing recording what it owes, and retrieval's
    silent-discard discipline guarantees no caller could tell. With a row
    written for every fact before the index is touched, incompleteness is
    durable from the moment it becomes possible, and a row is removed only
    by the one thing that removes the doubt: that fact projecting
    successfully. What survives a partial pass covers every fact the index
    lacks, which I-83 reads as ``index_pending`` for the affected scopes
    and the next serving instance's delivery loop then drains. Covers,
    not equals: delivery is at-least-once, so a row may outlive the work
    it names — a redundant row costs an idempotent replay, where a missing
    one costs a silently short index.

    Every interruption lands in the same safe state. A crash mid-pass
    leaves rows for what was not done. A crash between the enqueue and the
    clear leaves rows for everything and an intact index, which costs a
    replay of projections P-38 already makes idempotent.

    Order still matters within the pass, for an index that keeps only the
    latest state per fact: replaying in the order the catalogue recorded
    things reproduces what incremental delivery would have produced.
    ``fact_id`` breaks ties, as it does for I-82, so two facts from one
    assertion replay identically every time.

    Failures are counted, not raised: an index refusing one fact should not
    abandon the other ten thousand, and the caller exits non-zero on any
    failure so the operator knows the rebuild was partial. A fact whose
    stored row the value layer refuses is counted separately — that is
    catalogue corruption, which ``verify`` diagnoses properly — and keeps
    its row like any other fact the index does not hold.

    Above ``chunk_size`` 1, states are buffered per partition and flushed
    through ``project_many`` rather than projected one at a time. Every fact
    in a rebuild appears exactly once in the stream, unlike the outbox's
    multi-row-per-fact rows, so grouping by partition while preserving each
    partition's ``recorded_at`` order cannot reorder any fact's states — the
    final index equals the sequential replay's. Peak buffer memory is
    ``O(partitions × chunk_size)`` — safe for the single-partition P-74
    migration corpus this was built for, but unbounded across the whole
    partition set, so a caller feeding many partitions at a large
    ``chunk_size`` should be aware.
    """
    superseded = _enqueue_rebuild_work(data_path, transactions, uuid_factory)
    index.clear(None)
    projected = 0
    failed = 0
    unreadable = 0
    buffers: dict[str, list[ProjectedFactState]] = {}

    def flush(states: list[ProjectedFactState]) -> tuple[int, int]:
        results: object = None
        error: Exception | None = None
        try:
            results = index.project_many(tuple(states))
        except Exception as caught:
            error = caught
        if (
            not isinstance(results, tuple)
            or len(results) != len(states)
            or not all(
                isinstance(result, (FactProjected, ProjectionFailed))
                for result in results
            )
        ):
            # The same silent slowness as delivery's demotion, and the same
            # event (P-82 gate-4 ruling, 25 August 2026). A raised adapter
            # reaches this branch with ``results`` still ``None``, so the
            # exception — where there was one — decides the shape before
            # the answer is classified at all.
            emit_bulk_demoted(
                logger,
                len(states),
                error,
                BulkDemotionShape.ADAPTER_RAISED
                if error is not None
                else bulk_answer_shape(results, len(states)),
            )
            done = 0
            bad = 0
            for state in states:
                d, b = project_one(state)
                done += d
                bad += b
            return (done, bad)
        successful = tuple(
            state.fact_id
            for state, result in zip(states, results, strict=True)
            if isinstance(result, FactProjected)
        )
        done = _discharge_bulk(transactions, successful)
        return (done, len(states) - len(successful))

    def project_one(state: ProjectedFactState) -> tuple[int, int]:
        try:
            result = index.project(state)
        except Exception:
            return (0, 1)
        if isinstance(result, FactProjected):
            _discharge(transactions, state.fact_id)
            return (1, 0)
        return (0, 1)

    for state in _stored_facts(data_path, batch_size):
        if state is None:
            unreadable += 1
            continue
        if chunk_size <= 1:
            d, f = project_one(state)
            projected += d
            failed += f
            continue
        buffer = buffers.setdefault(state.partition_key, [])
        buffer.append(state)
        if len(buffer) >= chunk_size:
            d, f = flush(buffer)
            projected += d
            failed += f
            buffers[state.partition_key] = []
    for buffer in buffers.values():
        if buffer:
            d, f = flush(buffer)
            projected += d
            failed += f

    return RebuildReport(
        projected=projected,
        failed=failed,
        unreadable=unreadable,
        superseded_rows=superseded,
    )


def _stored_facts(
    data_path: Path, batch_size: int
) -> Iterator[ProjectedFactState | None]:
    """Every fact, oldest first, with its invalidation state.

    Paged by ``(recorded_at, fact_id)`` rather than read whole: a catalogue
    large enough to need a rebuild is large enough that materialising every
    fact body at once is a poor idea. ``None`` marks a row the value layer
    refuses, so the caller can count it without this generator deciding
    what that means.
    """
    after: tuple[str, str] | None = None
    while True:
        with read_connection(data_path) as connection:
            if after is None:
                rows = connection.execute(
                    "SELECT f.fact_id, f.realm_id, f.scope_segments, f.body, "
                    "f.trust, f.classification, f.valid_from, f.valid_to, "
                    "f.recorded_at, i.invalidated_at "
                    "FROM facts f "
                    "LEFT JOIN fact_invalidations i ON i.fact_id = f.fact_id "
                    "ORDER BY f.recorded_at, f.fact_id LIMIT ?",
                    (batch_size,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT f.fact_id, f.realm_id, f.scope_segments, f.body, "
                    "f.trust, f.classification, f.valid_from, f.valid_to, "
                    "f.recorded_at, i.invalidated_at "
                    "FROM facts f "
                    "LEFT JOIN fact_invalidations i ON i.fact_id = f.fact_id "
                    "WHERE (f.recorded_at, f.fact_id) > (?, ?) "
                    "ORDER BY f.recorded_at, f.fact_id LIMIT ?",
                    (*after, batch_size),
                ).fetchall()
        if not rows:
            return
        for row in rows:
            yield _projected_state(row)
        last = cast(tuple[str, ...], rows[-1])
        after = (last[8], last[0])
        if len(rows) < batch_size:
            return


def _projected_state(row: tuple[object, ...]) -> ProjectedFactState | None:
    (
        fact_id,
        realm_id,
        scope_segments,
        body,
        trust,
        classification,
        valid_from,
        valid_to,
        recorded_at,
        invalidated_at,
    ) = cast(
        tuple[str, str, str, str, str, str, str | None, str | None, str, str | None],
        row,
    )
    try:
        segments = _stored_segments(scope_segments)
        return ProjectedFactState(
            fact_id=UUID(fact_id),
            partition_key=canonical_partition(realm_id, scope_segments),
            body=body,
            realm_id=realm_id,
            segments=segments,
            classification=Classification(classification),
            trust=TrustClass(trust),
            recorded_at=parse_timestamp(recorded_at),
            valid_from=None if valid_from is None else parse_timestamp(valid_from),
            valid_to=None if valid_to is None else parse_timestamp(valid_to),
            invalidated_at=(
                None if invalidated_at is None else parse_timestamp(invalidated_at)
            ),
        )
    except Exception:
        # Every reading guard's failure mode at once — a malformed scope, a
        # shape-legal but impossible timestamp — because the caller's only
        # response to any of them is the same count.
        return None


def _stored_segments(value: str) -> tuple[ScopeSegment, ...]:
    """The same reader ``cairn.authority.mutations._stored_scope`` and
    ``cairn.evidence.reconciliation._stored_segments`` are: migration
    0003's CHECK pins minified-JSON-array shape and nothing about element
    contents, so reconstructing ``ScopeSegment`` is what refuses a row the
    schema accepts but the value layer does not."""
    documents = json.loads(value)
    if type(documents) is not list:
        raise AuditValueError("invalid_scope")
    segments: list[ScopeSegment] = []
    for document in documents:
        if type(document) is not dict or set(document) != {"kind", "id"}:
            raise AuditValueError("invalid_scope")
        segments.append(ScopeSegment(kind=document["kind"], identifier=document["id"]))
    return tuple(segments)


def _enqueue_rebuild_work(
    data_path: Path,
    transactions: CatalogueTransactions,
    uuid_factory: Callable[[], UUID],
) -> int:
    """One ``fact-rebuild`` row per catalogue fact, replacing whatever the
    outbox held, in a single transaction.

    Superseding rather than adding is required, not tidiness. A fact left
    holding both a custody row and a rebuild row would keep the custody row
    after re-projection deleted the rebuild one, and I-83 would refuse that
    scope for ever — a rebuild that permanently disabled retrieval for the
    facts it had just successfully restored. What the superseded rows asked
    for is a subset of what this pass does anyway.

    Each row is stamped with its fact's ``recorded_at``, not the clock.
    I-83 compares ``created_at`` against the request's ``as_of``, so rows
    stamped now would guard queries at the current instant and leave every
    historical read to be served from the very gap the rows exist to
    declare. The index has owed each fact since it was recorded, and that
    is what the column then says.
    """
    with read_connection(data_path) as connection:
        facts = connection.execute(
            "SELECT fact_id, recorded_at FROM facts ORDER BY recorded_at, fact_id"
        ).fetchall()

    def work(transaction: CompoundTransaction) -> int:
        rows = transaction.query("SELECT COUNT(*) FROM projection_outbox", ())
        transaction.execute("DELETE FROM projection_outbox", ())
        for fact_id, recorded_at in facts:
            transaction.execute(
                "INSERT INTO projection_outbox "
                "(work_id, kind, fact_id, mutation_id, created_at, attempts) "
                "VALUES (?, 'fact-rebuild', ?, NULL, ?, 0)",
                (str(uuid_factory()), fact_id, recorded_at),
            )
        return cast(int, rows[0][0])

    return transactions.execute_compound(work)


def _discharge_bulk(
    transactions: CatalogueTransactions, fact_ids: tuple[UUID, ...]
) -> int:
    """Confirm only validated bulk successes, at most 500 per transaction.

    P-45/P-82 amendment, 11 September 2026: keep the per-fact predicate,
    but commit a bounded group together. A library projection chunk may
    exceed 500, so this bounds each transaction, not total pending replay.
    SQL/commit failures propagate; unconfirmed groups never count as done.
    """
    done = 0
    for group in batched(fact_ids, 500):

        def work(
            transaction: CompoundTransaction, group: tuple[UUID, ...] = group
        ) -> None:
            for fact_id in group:
                transaction.execute(
                    "DELETE FROM projection_outbox WHERE fact_id = ? AND kind = 'fact-rebuild'",
                    (str(fact_id),),
                )

        transactions.execute_compound(work)
        done += len(group)
    return done


def _discharge(transactions: CatalogueTransactions, fact_id: UUID) -> None:
    """Delete a fact's rebuild row, the moment its projection succeeded.

    Per fact rather than in one pass at the end: a rebuild interrupted
    after ten thousand successes should keep the ten thousand it proved and
    owe only the rest, which a single closing delete could not express.
    """

    def work(transaction: CompoundTransaction) -> None:
        transaction.execute(
            "DELETE FROM projection_outbox WHERE fact_id = ? AND kind = 'fact-rebuild'",
            (str(fact_id),),
        )

    transactions.execute_compound(work)
