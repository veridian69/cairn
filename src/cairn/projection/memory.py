"""A deterministic in-memory retrieval index, for test-mode instances only.

This exists so a real running instance — the image smoke's container, an
operator kicking the tyres — can exercise ingest, delivery and
`/v1/retrieve` end to end without FalkorDB and model providers. It is
product code because the image needs it, and it is fenced accordingly:
``build_memory_index`` refuses to construct under ``mode: production``,
so no deployment can serve a fake index, however the configuration is
written (Operator's ruling, 9 August 2026, for slice 6 task 12).

It is not a search engine and does not pretend to be one. ``search``
matches a case-folded substring of the projected body and returns
identities in projection order. That is enough to prove the pipeline is
connected, and nothing about retrieval's *disclosure* rests on it: I-79
reconciliation re-derives every hit from the catalogue, so the worst a
weak index can do is find too little. The conformance suite makes the
same bet deliberately, which is why the hostile adapter — an index doing
its worst — is the case that actually proves the security property.

State lives in the process and dies with it. A restarted instance has an
empty index until ``rebuild-index`` re-projects from the catalogue, which
is the documented recovery path for exactly this situation.
"""

import threading
from uuid import UUID

from cairn.projection.adapter import (
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)


class MemoryIndex:
    """Thread-safe by a plain lock: the delivery loop projects from a worker
    thread while request handlers search from theirs, so the two must not
    tear the same dict. The lock is held only for dict operations, never
    across a caller's work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Insertion-ordered by first projection, which is what makes
        # ``search`` deterministic. Re-projecting a fact replaces its state
        # and keeps its original position.
        self._facts: dict[UUID, ProjectedFactState] = {}

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        with self._lock:
            self._facts[state.fact_id] = state
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        # P-48 as amended: ``limit`` is a fetch bound, not a return cap.
        # This adapter scans its own dictionary, so there is no per-partition
        # fetch to bound and nothing to truncate — every match is returned
        # and reconciliation applies I-82's budget. Slicing here would
        # decide candidate membership in dictionary order, which is the
        # same defect the real adapter carried in ancestry order.
        wanted = frozenset(partition_keys)
        needle = query.casefold()
        with self._lock:
            return tuple(
                fact_id
                for fact_id, state in self._facts.items()
                if state.partition_key in wanted and needle in state.body.casefold()
            )

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        with self._lock:
            if partition_keys is None:
                self._facts.clear()
                return
            wanted = frozenset(partition_keys)
            self._facts = {
                fact_id: state
                for fact_id, state in self._facts.items()
                if state.partition_key not in wanted
            }


class MemoryIndexRefused(Exception):
    """Raised when a production-mode instance asks for the in-memory index.

    A start failure, like an unreadable contract: the operator asked for
    something that cannot be honoured, and serving a fake index while
    reporting success would be the worse answer.
    """


def build_memory_index(mode: str) -> MemoryIndex:
    if mode != "test":
        raise MemoryIndexRefused(
            "the in-memory retrieval index is available only in test mode"
        )
    return MemoryIndex()
