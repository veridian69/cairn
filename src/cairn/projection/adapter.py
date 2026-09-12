"""Retrieval index adapter protocol (P-38): the closed four-operation
contract for the rebuildable retrieval index, and its frozen result values.

Modelled on ``cairn.evidence.adapter`` clause for clause, because the two
adapters answer to the same authority rule: domain outcomes are returned
as frozen values rather than raised, infrastructure failures raise, and
**results are always candidates**. The index never authorises anything —
``search`` returns bare fact identities, never bodies, snippets or scores,
so nothing it returns can bypass the I-79 reconciliation the catalogue
performs before a single hit is disclosed. A hostile index is therefore a
correctness problem the catalogue already solves, not a security boundary
this module has to defend.

``ProjectedFactState`` is what the deliverer reads from the catalogue at
delivery time, per I-68's content-free outbox rows: the fact as it stands
*now*, not as it stood when the work was queued.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cairn.catalogue.audit import Classification, ScopeSegment, TrustClass


@dataclass(frozen=True, slots=True)
class ProjectedFactState:
    """One fact's current catalogue state, as projected into the index.

    ``partition_key`` is I-78's ``(realm_id, scope_segments)`` pair already
    encoded for the index to use as an opaque namespace — the index learns
    distinct keys, never scope semantics. It carries a scope path and is
    therefore never safe to log (I-32).

    ``invalidated_at`` is present when an invalidation row exists. The fact
    is projected regardless: I-81 point-in-time recall must still find it
    for an ``as_of`` before its invalidation, so the index never deletes
    what the catalogue only ended belief in (P-38).
    """

    fact_id: UUID
    partition_key: str
    body: str
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    classification: Classification
    trust: TrustClass
    recorded_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    invalidated_at: datetime | None


@dataclass(frozen=True, slots=True)
class FactProjected:
    """``project`` succeeded: the fact's current state is visible to
    ``search`` under its partition key. Idempotent by fact identity —
    re-projecting the same state is a success no-op, and re-projecting a
    changed state replaces what the index held."""


@dataclass(frozen=True, slots=True)
class ProjectionFailed:
    """The index refused this fact for a reason of its own — the typed
    domain failure the deliverer records as a retryable attempt rather
    than a crash. ``code`` is a safe lowercase identifier, never text
    derived from fact content."""

    code: str


class IndexAdapter(Protocol):
    """Closed four-operation protocol. The index never authorises
    anything — every result is a candidate for the caller to reconcile
    against the catalogue."""

    def project(
        self, state: ProjectedFactState
    ) -> FactProjected | ProjectionFailed: ...

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        """P-82: project several states sharing one partition key.

        The result aligns index-for-index with ``states``. Callers
        guarantee the shared partition; adapters may assume it. A raised
        exception is an infrastructure failure for the whole call — the
        deliverer responds by demoting the chunk to per-fact ``project``
        calls. It means no result from this call is trustworthy and the
        underlying store may already be partially applied; ``project_many``
        is not required to be atomic. Correct recovery depends only on
        each state's projection being idempotent and resumable when
        retried individually, which ``project`` already guarantees.
        """
        ...

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        """Candidate fact identities only.

        ``query`` is opaque UTF-8 within the I-30 8 KiB bound, passed
        unparsed — Cairn owns no query syntax. ``partition_keys`` is the
        request scope's ancestry chain; an adapter that ignores it is
        exactly what I-79 defends against, which is why honouring it is a
        performance property and not a security one.

        ``limit`` is a **fetch bound, not a return cap** (P-48 as amended,
        10 August 2026). It bounds the work an adapter asks of each
        partition; the adapter then returns every deduplicated candidate
        it found, applying no cross-partition truncation and no ranking.
        Ranking and budget are I-82's, at the reconciliation layer, and
        the caller already treats this bound as owed nothing by the
        adapter. An adapter that truncates is deciding candidate
        membership, which is upstream of everything reconciliation
        orders — with an ancestry chain fetched in order, truncation lets
        the realm root starve the request's own scope. This paragraph
        exists because its absence let exactly that ship through two
        reviews.
        """
        ...

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        """Remove the named partitions, or everything for ``None``.

        Exists solely for the P-45 rebuild. Nothing in the serving path
        may call it.
        """
        ...
