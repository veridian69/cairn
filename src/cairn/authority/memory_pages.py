"""Ordered, continuable memory recall over the existing memory authority.

Selection reuses legacy candidate ranking; pages admit a monotone whole-record
prefix (never skip-to-fit), then bounded relationship context. Every page
re-checks grants, clearance, validity and trust before disclosure.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from cairn.authority.gate import Actor, Fetch, fetch_from, instance_id, realm_draft
from cairn.authority.memory import (
    _ALL_TRUST,
    CairnMemory,
    _flag_context,
    _json,
    _policy,
    _score,
    _size,
)
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_order import order_ids
from cairn.authority.memory_page_types import (
    MAX_PAGE_LIMIT,
    SNAPSHOT_CAPACITY,
    ContinuationUnavailable,
    Order,
    Ordering,
    PageBudgetTooSmall,
    PagedMemoryFact,
    PageRejected,
    RecallContinue,
    RecallPage,
    RecallPageResult,
    TimeBasis,
)
from cairn.authority.memory_types import (
    MAX_HISTORY_RECORDS,
    MemoryDisagreement,
    MemoryResolution,
)
from cairn.authority.recall_snapshots import (
    Binding,
    CapacityExceeded,
    Snapshot,
    SnapshotStore,
)
from cairn.authority.retrieval import (
    MAX_QUERY_BYTES,
    RetrievedFact,
    _admitted,
    _covering_retrieve_grants,
    _load_candidates,
)
from cairn.authority.source_time import project_sources
from cairn.catalogue.audit import ActionKind, Outcome, Scope, TrustClass
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import (
    FailureCode,
    MutationRejection,
    Rejected,
    RetryClass,
    StableFailure,
)

ACTION = "memory-recall-page"
_CHUNK = 128


class _Detailed(Exception):
    def __init__(
        self,
        error: MutationRejection,
        detail: PageBudgetTooSmall | ContinuationUnavailable,
    ) -> None:
        self.error, self.detail = error, detail


class _ProjectionChanged(Exception):
    """A disclosed hit's source-time status differs from its creation value (R5/F1)."""


@dataclass(slots=True)
class _Page:
    hits: list[PagedMemoryFact]
    links: list[MemoryDisagreement]
    resolutions: list[MemoryResolution]
    consumed: int
    exhausted: bool
    context_incomplete: bool
    next_position: int | None


class CairnMemoryPages:
    def __init__(self, memory: CairnMemory, snapshots: SnapshotStore) -> None:
        self._memory = memory
        self._snapshots = snapshots

    def _invalid(
        self, fetch: Fetch, actor: Actor, cid: UUID, reason: str, scope: Scope
    ) -> MutationRejection:
        return self._memory._refuse(
            fetch, actor, cid, ACTION, FailureCode.INVALID_REQUEST, reason, scope=scope
        )

    @staticmethod
    def _capacity(actor: Actor, cid: UUID, scope: Scope) -> MutationRejection:
        """Built after the read view closes; scope is already authorised here."""
        return MutationRejection(
            StableFailure(
                FailureCode.DEPENDENCY_UNAVAILABLE,
                "Dependency unavailable.",
                cid,
                RetryClass.AFTER_DELAY,
            ),
            realm_draft(
                realm_id=scope.realm,
                actor=actor,
                grant_id=None,
                action_kind=ActionKind.DATA,
                action_code=ACTION,
                requested_scope=scope,
                outcome=Outcome.DENY,
                reason_code="recall_page_capacity",
                correlation_id=cid,
            ),
        )

    def _assemble(
        self,
        fetch: Fetch,
        snapshot_ids: tuple[UUID, ...],
        scores: tuple[float, ...],
        start: int,
        basis: TimeBasis | None,
        scope: Scope,
        ceiling: int,
        now: datetime,
        budget: int,
        limit: int,
        trust_filters: frozenset[TrustClass],
        expected_available: tuple[bool, ...] | None = None,
    ) -> _Page | PageBudgetTooSmall:
        """expected_available is the snapshot's creation projection. On a
        source-basis continuation every still-admitted fact that gets projected,
        each disclosed hit and also the one that then fails the budget check,
        raises _ProjectionChanged when its fresh status differs. The fact that
        only ends the page at the limit is not projected and not compared."""
        memory = self._memory
        hits: list[PagedMemoryFact] = []
        selected: dict[UUID, RetrievedFact] = {}
        consumed, exhausted = 0, False
        next_position: int | None = None
        position = start
        while position < len(snapshot_ids) and next_position is None:
            chunk = snapshot_ids[position : position + _CHUNK]
            loaded, _ = _load_candidates(fetch, chunk)
            current = {f.fact_id: f for f in loaded}
            for offset, identity in enumerate(chunk):
                fact = current.get(identity)
                if (
                    fact is None
                    or not _admitted(fact, scope, ceiling, _ALL_TRUST, now)
                    or (trust_filters and fact.trust not in trust_filters)
                ):
                    continue
                if len(hits) == limit:
                    next_position = position + offset
                    break
                projection = project_sources(
                    fetch, {identity: fact}, scope, ceiling, now
                )[identity]
                if (
                    expected_available is not None
                    and basis is TimeBasis.SOURCE
                    and expected_available[position + offset]
                    != (projection.observed_at is not None)
                ):
                    raise _ProjectionChanged
                paged = PagedMemoryFact(
                    memory._fact(
                        fetch,
                        fact,
                        scope,
                        ceiling,
                        now,
                        relevance_score=scores[position + offset],
                    ),
                    projection.observed_at,
                    projection.status,
                    basis,
                    projection.evidence_id,
                )
                # Both flags are still false, their longer JSON form: this
                # reserves their maximum cost before admission.
                size = len(memory_value_bytes(paged))
                if consumed + size > budget:
                    if not hits:
                        return PageBudgetTooSmall(size)
                    exhausted, next_position = True, position + offset
                    break
                hits.append(paged)
                selected[identity] = fact
                consumed += size
            position += len(chunk)
        links, resolutions, context_limited = memory._links(
            fetch, selected, scope, ceiling, now, limit=MAX_HISTORY_RECORDS
        )
        omitted = context_limited
        disclosed_links: list[MemoryDisagreement] = []
        for link in links:
            if consumed + _size(link) <= budget:
                disclosed_links.append(link)
                consumed += _size(link)
            else:
                omitted = True
        shown = {link.relationship_id for link in disclosed_links}
        disclosed_resolutions: list[MemoryResolution] = []
        for resolution in resolutions:
            if resolution.disagreement_id not in shown:
                continue
            if consumed + _size(resolution) <= budget:
                disclosed_resolutions.append(resolution)
                consumed += _size(resolution)
            else:
                omitted = True
        flagged = _flag_context(
            fetch,
            [h.memory for h in hits],
            scope,
            ceiling,
            now,
            disclosed_links,
            disclosed_resolutions,
        )
        hits = [replace(h, memory=f) for h, f in zip(hits, flagged, strict=True)]
        consumed = (
            sum(len(memory_value_bytes(h)) for h in hits)
            + sum(_size(link) for link in disclosed_links)
            + sum(_size(r) for r in disclosed_resolutions)
        )
        return _Page(
            hits,
            disclosed_links,
            disclosed_resolutions,
            consumed,
            exhausted or omitted,
            omitted or any(h.memory.disagreement_context_incomplete for h in hits),
            next_position,
        )

    def recall_page(
        self, actor: Actor, command: RecallPage, *, correlation_id: UUID
    ) -> RecallPageResult | Rejected | PageRejected:
        memory, cid = self._memory, correlation_id
        now = memory._clock()
        try:
            with read_connection(memory._data_path) as connection:
                connection.execute("BEGIN")  # R9: one coherent view, always
                fetch = fetch_from(connection)
                grants, ceiling = memory._access(
                    fetch, actor, command.scope, now, cid, ACTION
                )
                memory._budget(
                    fetch, actor, command.budget, cid, ACTION, scope=command.scope
                )
                if (
                    type(command.limit) is not int
                    or not 1 <= command.limit <= MAX_PAGE_LIMIT
                ):
                    raise self._invalid(
                        fetch, actor, cid, "invalid_limit", command.scope
                    )
                if (
                    type(command.order) is not Order
                    or (
                        command.time_basis is not None
                        and type(command.time_basis) is not TimeBasis
                    )
                    or (
                        command.order is Order.RELEVANCE
                        and command.time_basis is not None
                    )
                ):
                    raise self._invalid(
                        fetch, actor, cid, "invalid_order", command.scope
                    )
                if (
                    type(command.query) is not str
                    or not command.query
                    or len(command.query.encode()) > MAX_QUERY_BYTES
                    or type(command.trust_filters) is not frozenset
                    or any(type(t) is not TrustClass for t in command.trust_filters)
                    or type(command.relevant_only) is not bool
                ):
                    raise self._invalid(
                        fetch, actor, cid, "invalid_query", command.scope
                    )
                memory._screen_text(
                    fetch, actor, command.query, cid, ACTION, scope=command.scope
                )
                found = memory._candidates(
                    fetch,
                    command.scope,
                    command.query,
                    ceiling,
                    now,
                    command.trust_filters,
                )
                # The relevance key reproduces legacy recall's exact comparison.
                key: Callable[[UUID], tuple[object, ...]]
                if found.graded:
                    scores = {
                        i: found.ranks[i].units / 1_000_000 for i in found.allowed
                    }
                    relevant = {i: found.ranks[i].relevant for i in found.allowed}

                    def key(i: UUID) -> tuple[object, ...]:
                        return found.ranks[i].key

                else:
                    scores = {
                        i: _score(
                            found.admitted[i],
                            now,
                            command.query,
                            i in found.semantic_ids,
                        )
                        for i in found.allowed
                    }
                    relevant = {i: scores[i] > 0.25 for i in found.allowed}

                    def key(i: UUID) -> tuple[object, ...]:
                        return (-scores[i], str(i))

                eligible = {
                    i: found.admitted[i]
                    for i in found.allowed
                    if not command.relevant_only or relevant[i]
                }
                basis = (
                    None
                    if command.order is Order.RELEVANCE
                    else command.time_basis or TimeBasis.SOURCE
                )
                projection = (
                    project_sources(fetch, eligible, command.scope, ceiling, now)
                    if basis is TimeBasis.SOURCE
                    else None
                )
                ordered = order_ids(command.order, basis, eligible, key, projection)
                selection_complete = len(ordered) <= SNAPSHOT_CAPACITY
                snapshot_ids = tuple(ordered[:SNAPSHOT_CAPACITY])
                frozen = tuple(scores[i] for i in snapshot_ids)
                available = tuple(
                    projection is not None and projection[i].observed_at is not None
                    for i in snapshot_ids
                )
                page = self._assemble(
                    fetch,
                    snapshot_ids,
                    frozen,
                    0,
                    basis,
                    command.scope,
                    ceiling,
                    now,
                    command.budget,
                    command.limit,
                    command.trust_filters,
                )
                if isinstance(page, PageBudgetTooSmall):
                    raise _Detailed(
                        self._invalid(
                            fetch, actor, cid, "page_budget_too_small", command.scope
                        ),
                        page,
                    )
                binding = Binding(
                    instance_id(fetch),
                    actor.principal_id,
                    command.scope,
                    command.order,
                    basis,
                    command.relevant_only,
                    command.trust_filters,
                    hashlib.sha256(command.query.encode()).digest(),
                    _policy(
                        found.graded,
                        command.relevant_only,
                        found.semantic_degraded,
                        memory._semantic_evidence is not None,
                    ),
                    found.semantic_degraded,
                    ceiling,
                    selection_complete,
                )
                authorising = _covering_retrieve_grants(grants, command.scope, now)[0]
            # R4: a snapshot exists only when there is somewhere to continue to.
            snapshot: Snapshot | None = None
            token: str | None = None
            if page.next_position is not None:
                try:
                    snapshot, token = self._snapshots.publish(
                        binding,
                        snapshot_ids,
                        frozen,
                        available,
                        page.next_position,
                        now,
                    )
                except CapacityExceeded:
                    raise self._capacity(actor, cid, command.scope) from None
            # Audit commits before the cursor is exposed; an unaudited
            # reservation is released.
            try:
                memory._audit_read(
                    actor,
                    command.scope,
                    authorising.grant_id,
                    cid,
                    ACTION,
                    tuple(h.memory.fact.fact_id for h in page.hits),
                )
            except BaseException:
                if snapshot is not None:
                    self._snapshots.discard(snapshot)
                raise
            return self._result(page, binding, now, snapshot, token)
        except _Detailed as detailed:
            rejected = memory._reject(detailed.error)
            return PageRejected(rejected.failure, detailed.detail)
        except MutationRejection as error:
            return memory._reject(error)

    def recall_continue(
        self, actor: Actor, command: RecallContinue, *, correlation_id: UUID
    ) -> RecallPageResult | Rejected | PageRejected:
        memory, cid = self._memory, correlation_id
        now = memory._clock()
        try:
            with read_connection(memory._data_path) as connection:
                connection.execute("BEGIN")  # R9: one coherent view, always
                fetch = fetch_from(connection)
                grants, ceiling = memory._access(
                    fetch, actor, command.scope, now, cid, ACTION
                )
                memory._budget(
                    fetch, actor, command.budget, cid, ACTION, scope=command.scope
                )
                if (
                    type(command.limit) is not int
                    or not 1 <= command.limit <= MAX_PAGE_LIMIT
                ):
                    raise self._invalid(
                        fetch, actor, cid, "invalid_limit", command.scope
                    )
                located = (
                    self._snapshots.resolve(command.cursor, now)
                    if type(command.cursor) is str
                    else None
                )
                # Expired, invented, foreign and rebound cursors are one refusal.
                # R5: a source-basis projection is a pure function of the
                # clearance ceiling while the clock does not step back.
                if located is None or not _bound(
                    located[0], instance_id(fetch), actor, command.scope, ceiling, now
                ):
                    raise self._unavailable(fetch, actor, cid, command.scope)
                snapshot, position = located
                binding = snapshot.binding
                try:
                    page = self._assemble(
                        fetch,
                        snapshot.fact_ids,
                        snapshot.scores,  # R10: frozen, never recomputed
                        position,
                        binding.time_basis,
                        command.scope,
                        ceiling,
                        now,
                        command.budget,
                        command.limit,
                        binding.trust_filters,
                        snapshot.source_available,
                    )
                except _ProjectionChanged:
                    raise self._unavailable(fetch, actor, cid, command.scope) from None
                if isinstance(page, PageBudgetTooSmall):
                    raise _Detailed(
                        self._invalid(
                            fetch, actor, cid, "page_budget_too_small", command.scope
                        ),
                        page,
                    )
                authorising = _covering_retrieve_grants(grants, command.scope, now)[0]
            # R12: a snapshot lost before interning still yields the page, with
            # facts_remaining from next_position and no cursor.
            token = (
                None
                if page.next_position is None
                else self._snapshots.token_for(snapshot, page.next_position, now)
            )
            memory._audit_read(
                actor,
                command.scope,
                authorising.grant_id,
                cid,
                ACTION,
                tuple(h.memory.fact.fact_id for h in page.hits),
            )
            return self._result(page, binding, now, snapshot, token)
        except _Detailed as detailed:
            rejected = memory._reject(detailed.error)
            return PageRejected(rejected.failure, detailed.detail)
        except MutationRejection as error:
            return memory._reject(error)

    def _unavailable(
        self, fetch: Fetch, actor: Actor, cid: UUID, scope: Scope
    ) -> _Detailed:
        return _Detailed(
            self._invalid(fetch, actor, cid, "continuation_unavailable", scope),
            ContinuationUnavailable(),
        )

    def _result(
        self,
        page: _Page,
        binding: Binding,
        now: datetime,
        snapshot: Snapshot | None,
        token: str | None,
    ) -> RecallPageResult:
        # R11: created_at is the selection instant; expiry only when published.
        return RecallPageResult(
            tuple(page.hits),
            tuple(page.links),
            tuple(page.resolutions),
            page.consumed,
            page.exhausted,
            binding.policy,
            binding.semantic_degraded,
            Ordering(binding.order, binding.time_basis),
            now if snapshot is None else snapshot.created_at,
            None if snapshot is None else snapshot.expires_at,
            token,
            page.next_position is not None,
            page.context_incomplete,
            binding.selection_complete,
        )


def _bound(
    snapshot: Snapshot,
    instance: str,
    actor: Actor,
    scope: Scope,
    ceiling: int,
    now: datetime,
) -> bool:
    binding = snapshot.binding
    return (
        binding.instance_id == instance
        and binding.principal_id == actor.principal_id
        and binding.scope == scope
        and (
            binding.time_basis is not TimeBasis.SOURCE
            or (binding.ceiling == ceiling and now >= snapshot.created_at)
        )
    )


def memory_value_bytes(value: PagedMemoryFact) -> bytes:
    return _json(memory_value(value))
