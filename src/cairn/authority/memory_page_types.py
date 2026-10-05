"""Ordered, paged recall and bounded evidence windows: memory/v1 additions only.

These values never appear in legacy recall/history packets (I-82 stays frozen).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from cairn.authority.memory_types import (
    MemoryDisagreement,
    MemoryFact,
    MemoryResolution,
)
from cairn.catalogue.audit import Scope, TrustClass
from cairn.catalogue.transactions import StableFailure

ORDER_POLICY = "memory-order/v1"
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
SNAPSHOT_CAPACITY = 4096


class Order(StrEnum):
    RELEVANCE = "relevance"
    NEWEST = "newest"
    OLDEST = "oldest"


class TimeBasis(StrEnum):
    SOURCE = "source"
    RECORDED = "recorded"


class SourceTimeStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RecallPage:
    scope: Scope
    query: str
    order: Order = Order.RELEVANCE
    # None means the default: source for chronological orders, absent for relevance.
    time_basis: TimeBasis | None = None
    relevant_only: bool = True
    budget: int = 16384
    limit: int = DEFAULT_PAGE_LIMIT
    trust_filters: frozenset[TrustClass] = frozenset()


@dataclass(frozen=True, slots=True)
class RecallContinue:
    scope: Scope
    cursor: str
    budget: int = 16384
    limit: int = DEFAULT_PAGE_LIMIT


@dataclass(frozen=True, slots=True)
class PagedMemoryFact:
    memory: MemoryFact
    observed_at: datetime | None
    source_time_status: SourceTimeStatus
    ordering_time_basis: TimeBasis | None
    source_evidence_id: UUID | None


@dataclass(frozen=True, slots=True)
class Ordering:
    order: Order
    time_basis: TimeBasis | None
    policy: str = ORDER_POLICY


@dataclass(frozen=True, slots=True)
class RecallPageResult:
    hits: tuple[PagedMemoryFact, ...]
    disagreements: tuple[MemoryDisagreement, ...]
    resolutions: tuple[MemoryResolution, ...]
    budget_consumed: int
    budget_exhausted: bool
    policy: str
    semantic_degraded: bool
    ordering: Ordering
    snapshot_created_at: datetime
    snapshot_expires_at: datetime | None
    next_cursor: str | None
    facts_remaining: bool
    context_incomplete: bool
    selection_complete: bool


@dataclass(frozen=True, slots=True)
class PageBudgetTooSmall:
    minimum_budget: int
    reason: str = "page_budget_too_small"


@dataclass(frozen=True, slots=True)
class ContinuationUnavailable:
    reason: str = "continuation_unavailable"


@dataclass(frozen=True, slots=True)
class PageRejected:
    """An audited refusal whose operation-local detail legacy envelopes cannot carry."""

    failure: StableFailure
    detail: PageBudgetTooSmall | ContinuationUnavailable
