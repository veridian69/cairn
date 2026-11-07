"""#204 (I-99): targeted repair of retrieval-index coverage.

The deliverer deletes an outbox row once ``project`` returns, and SQLite
records that durably. FalkorDB can still lose its last writes in a host
hard reset, which leaves a fact torn — an episode without its marker or
vectors — or absent altogether, with no row left to retry it. The torn
case closes both read-side coverage gates for its whole partition, so
semantic recall stays degraded until an operator rebuilds everything;
the absent case passes the gates and silently drops the fact from
semantic search.

This module finds exactly those facts and writes a ``fact-rebuild`` row
for each, the row shape ``rebuild.py`` writes, so the normal deliverer
repairs them through ``project``. That path already resumes an unmarked
episode and re-runs ``_ensure_fact_vectors`` for a marked one (P-38).
Nothing is cleared. Recall keeps reporting degraded until the last row
drains, ``/v1/retrieve`` through I-83 and graded memory recall through the
verification gate below, so a repair in progress never serves a partial
result.

Two callers, by Operator's ruling (21 October 2026): the delivery loop at
startup, up to ``delivery.repair_cap`` facts, and the CLI-only
``repair-index``, uncapped. The cap bounds what the server spends on
providers without an operator: re-projection reruns extraction for a fact
whose marker was lost and embeddings for its vectors, both normally
served by the P-90 caches, but not guaranteed to be. Above the cap the
startup pass enqueues nothing at all rather than a first slice — a
partial repair spends money without reopening the gate — and says so.
Until a startup audit completes within the cap and every fact it found
torn or absent has projected, the server keeps the index unverified and semantic search fails closed: an
absent fact passes both gates, and graded memory recall does not read the
outbox, so nothing else would reveal the gap (Sol review P1s, same ruling;
the drain condition from the Codex review of PR #207).

Both run with nothing else writing the graph: startup repair inside the
delivery task before its first drain, and the CLI under the instance
lease. The catalogue snapshot is read before the graph, so a fact
ingested in between only has a pending row the snapshot does not see,
never a graph entry it cannot explain.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import batched
from typing import cast
from uuid import UUID

from cairn.catalogue.transactions import CatalogueTransactions, CompoundTransaction
from cairn.projection.adapter import CoverageAuditor, IndexAdapter
from cairn.projection.partition import canonical_partition
from cairn.runtime.logging import LogEvent, SafeLogger


@dataclass(frozen=True, slots=True)
class RepairReport:
    """``targets`` counts facts the index owes that no outbox row covers;
    ``enqueued`` the rows this pass wrote, zero over the cap or on a dry
    run. ``unrepairable`` counts refused episodes no catalogue fact in that
    partition explains: a row cannot name them, so only ``rebuild-index``
    reopens a gate they hold closed. ``affected`` names every fact the
    audit found torn or absent, including those left to a row they already
    had: coverage is restored only once none of them has a row left."""

    targets: int
    enqueued: int
    unrepairable: int
    over_cap: bool
    affected: frozenset[str] = field(default=frozenset(), compare=False)


def coverage_auditor(index: IndexAdapter) -> CoverageAuditor | None:
    """The index's optional audit capability, or ``None`` without one."""
    audit = getattr(index, "coverage_audit", None)
    verified = getattr(index, "set_coverage_verified", None)
    return (
        cast(CoverageAuditor, index) if callable(audit) and callable(verified) else None
    )


def repair_index(
    transactions: CatalogueTransactions,
    auditor: CoverageAuditor,
    *,
    uuid_factory: Callable[[], UUID],
    cap: int | None,
    dry_run: bool = False,
    logger: SafeLogger | None = None,
) -> RepairReport:
    """Audit every catalogue partition and enqueue rows for what it owes.

    ``cap`` ``None`` is uncapped. Each row is stamped with its fact's
    ``recorded_at``, for the I-83 reason ``rebuild.py`` gives: the gap is
    real for every ``as_of`` since the fact was recorded."""
    partitions = transactions.execute_compound(_snapshot)
    targets: list[tuple[str, str]] = []
    affected: set[str] = set()
    unrepairable = 0
    for partition in sorted(partitions):
        facts, pending = partitions[partition]
        audit = auditor.coverage_audit(partition)
        unrepairable += audit.unidentified + len(audit.defective - facts.keys())
        for fact_id, recorded_at in facts.items():
            if fact_id in audit.defective or fact_id not in audit.present:
                affected.add(fact_id)
                # A fact with a row of its own is repaired by that row.
                if fact_id not in pending:
                    targets.append((fact_id, recorded_at))
    targets.sort(key=lambda target: (target[1], target[0]))

    owed, enqueued = (
        transactions.execute_compound(
            lambda tx: _enqueue(tx, targets, uuid_factory, cap, dry_run)
        )
        if targets
        else (0, 0)
    )
    over_cap = cap is not None and owed > cap
    if logger is not None:
        if over_cap:
            logger.emit(
                LogEvent.PROJECTION_REPAIR_OVER_CAP,
                transport=None,
                level=logging.WARNING,
                remaining=owed,
            )
        if enqueued:
            logger.emit(
                LogEvent.PROJECTION_REPAIR_ENQUEUED, transport=None, remaining=enqueued
            )
        if unrepairable:
            logger.emit(
                LogEvent.PROJECTION_REPAIR_UNREPAIRABLE,
                transport=None,
                level=logging.WARNING,
                failed=unrepairable,
            )
    return RepairReport(
        targets=owed,
        enqueued=enqueued,
        unrepairable=unrepairable,
        over_cap=over_cap,
        affected=frozenset(affected),
    )


def rows_remain(transactions: CatalogueTransactions, fact_ids: frozenset[str]) -> bool:
    """Whether any of these facts still has an outbox row of any kind.

    A row is deleted only when its fact projected successfully, so none
    left means every one of them is back in the index — whether the repair
    wrote the row or the fact already had a custody row that it left to
    deliver. Batched under SQLite's bound on query parameters."""

    def work(tx: CompoundTransaction) -> bool:
        for group in batched(sorted(fact_ids), 500):
            marks = ",".join("?" * len(group))
            if tx.query(
                f"SELECT 1 FROM projection_outbox WHERE fact_id IN ({marks}) LIMIT 1",
                group,
            ):
                return True
        return False

    return transactions.execute_compound(work)


def _snapshot(
    tx: CompoundTransaction,
) -> dict[str, tuple[dict[str, str], frozenset[str]]]:
    """Per partition: every fact's ``recorded_at``, and the facts that
    already have a row of any kind. One transaction, so the two agree."""
    rows = tx.query(
        "SELECT f.fact_id, f.realm_id, f.scope_segments, f.recorded_at, "
        "EXISTS (SELECT 1 FROM projection_outbox p WHERE p.fact_id = f.fact_id) "
        "FROM facts f",
        (),
    )
    facts: dict[str, dict[str, str]] = {}
    pending: dict[str, set[str]] = {}
    for fact_id, realm_id, scope_segments, recorded_at, has_row in rows:
        partition = canonical_partition(cast(str, realm_id), cast(str, scope_segments))
        facts.setdefault(partition, {})[cast(str, fact_id)] = cast(str, recorded_at)
        if has_row:
            pending.setdefault(partition, set()).add(cast(str, fact_id))
    return {
        partition: (members, frozenset(pending.get(partition, ())))
        for partition, members in facts.items()
    }


def _enqueue(
    tx: CompoundTransaction,
    targets: list[tuple[str, str]],
    uuid_factory: Callable[[], UUID],
    cap: int | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Returns ``(owed, enqueued)``. A fact that gained a row since the
    snapshot is no longer owed: its own row repairs it, and a second would
    replay it twice for nothing. The cap applies to what is owed now, in
    the transaction that writes, so a row a mutation added meanwhile can
    bring the pass back under it."""
    owed = [
        (fact_id, recorded_at)
        for fact_id, recorded_at in targets
        if not tx.query("SELECT 1 FROM projection_outbox WHERE fact_id = ?", (fact_id,))
    ]
    if dry_run or (cap is not None and len(owed) > cap):
        return len(owed), 0
    for fact_id, recorded_at in owed:
        tx.execute(
            "INSERT INTO projection_outbox "
            "(work_id, kind, fact_id, mutation_id, created_at, attempts) "
            "VALUES (?, 'fact-rebuild', ?, NULL, ?, 0)",
            (str(uuid_factory()), fact_id, recorded_at),
        )
    return len(owed), len(owed)
