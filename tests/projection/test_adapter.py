"""P-38: the index adapter port's behaviour, defined by its tests the way
I-69's Attic protocol was.

Two adapters live here. ``InMemoryIndex`` is the deterministic positive:
substring matching over projected bodies, honest partitions, stable order.
``HostileIndex`` is the negative: it returns sibling, descendant,
cross-realm, unknown, stale, duplicate and over-classified identities on
demand, ignoring the partition keys it was given. Neither is a security
boundary — the point of the port is that a hostile index *cannot* widen
disclosure, because I-79 reconciliation happens in the catalogue after
these identities come back. These tests fix the port's shape; Task 6
proves the reconciliation that makes hostility harmless.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from cairn.catalogue.audit import Classification, ScopeSegment, TrustClass
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.memory import MemoryIndex

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
REALM = "acme"
PARTITION = 'acme\n[{"kind":"repo","id":"api"}]'
SIBLING_PARTITION = 'acme\n[{"kind":"repo","id":"web"}]'


def fact(
    identity: str,
    body: str,
    *,
    partition_key: str = PARTITION,
    realm_id: str = REALM,
    classification: Classification = Classification.INTERNAL,
    trust: TrustClass = TrustClass.VALIDATED,
    invalidated_at: datetime | None = None,
) -> ProjectedFactState:
    return ProjectedFactState(
        fact_id=UUID(identity),
        partition_key=partition_key,
        body=body,
        realm_id=realm_id,
        segments=(ScopeSegment("repo", "api"),),
        classification=classification,
        trust=trust,
        recorded_at=NOW,
        valid_from=None,
        valid_to=None,
        invalidated_at=invalidated_at,
    )


FACT_ONE = fact("11111111-1111-4111-8111-111111111111", "the retry budget is four")
FACT_TWO = fact("22222222-2222-4222-8222-222222222222", "the retry policy is fixed")
FACT_THREE = fact(
    "33333333-3333-4333-8333-333333333333",
    "the sibling observation",
    partition_key=SIBLING_PARTITION,
)


class InMemoryIndex:
    """Deterministic positive adapter: substring match over projected
    bodies, restricted to the requested partitions, ordered by fact
    identity so a result is reproducible rather than merely correct.

    It does not truncate. P-48 as amended makes ``limit`` a fetch bound
    rather than a return cap, and that is a port rule every conforming
    adapter obeys — an adapter that slices decides candidate membership,
    which is upstream of everything I-82 orders."""

    def __init__(self) -> None:
        self._states: dict[UUID, ProjectedFactState] = {}
        self.projected: list[UUID] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self._states[state.fact_id] = state
        self.projected.append(state.fact_id)
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        matched = sorted(
            state.fact_id
            for state in self._states.values()
            if state.partition_key in partition_keys and query in state.body
        )
        return tuple(matched)

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        if partition_keys is None:
            self._states.clear()
            return
        for fact_id, state in list(self._states.items()):
            if state.partition_key in partition_keys:
                del self._states[fact_id]

    def state_of(self, fact_id: UUID) -> ProjectedFactState | None:
        return self._states.get(fact_id)


class HostileIndex:
    """Deterministic negative adapter: returns exactly the identities it
    was told to, regardless of query, partition or whether the catalogue
    has ever heard of them. Duplicates are returned as given, because a
    real index deduplicating for us is an assumption reconciliation must
    not make."""

    def __init__(self, candidates: tuple[UUID, ...]) -> None:
        self._candidates = candidates
        self.searched_partitions: list[tuple[str, ...]] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        self.searched_partitions.append(partition_keys)
        return self._candidates

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


class RefusingIndex:
    """Returns the typed domain failure rather than raising — the arm the
    deliverer records as a retryable attempt."""

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return ProjectionFailed(code="index_unavailable")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


def test_the_test_adapters_satisfy_the_protocol() -> None:
    """Structural conformance, asserted rather than assumed: if the
    protocol gains an operation, these fail here instead of at the first
    call site."""
    adapters: list[IndexAdapter] = [
        InMemoryIndex(),
        HostileIndex(()),
        RefusingIndex(),
    ]
    assert len(adapters) == 3


def test_projection_is_idempotent_by_fact_identity() -> None:
    index = InMemoryIndex()

    assert index.project(FACT_ONE) == FactProjected()
    assert index.project(FACT_ONE) == FactProjected()

    assert index.search("retry", 10, (PARTITION,)) == (FACT_ONE.fact_id,)


def test_reprojection_replaces_the_state_the_index_held() -> None:
    """I-68's content-free rows mean the deliverer reads current state at
    delivery time, so a second projection of the same identity carries
    newer truth and must win."""
    index = InMemoryIndex()
    index.project(FACT_ONE)
    invalidated = replace(FACT_ONE, invalidated_at=NOW)

    index.project(invalidated)

    held = index.state_of(FACT_ONE.fact_id)
    assert held is not None
    assert held.invalidated_at == NOW


def test_an_invalidated_fact_stays_searchable() -> None:
    """P-38: the index never deletes an invalidated fact. I-81 must still
    find it for an ``as_of`` before its invalidation; visibility is
    reconciliation's decision, not the index's."""
    index = InMemoryIndex()

    index.project(replace(FACT_ONE, invalidated_at=NOW))

    assert index.search("retry", 10, (PARTITION,)) == (FACT_ONE.fact_id,)


def test_search_returns_identities_only_and_does_not_cap_at_the_limit() -> None:
    """P-48 as amended, 10 August 2026: ``limit`` bounds the fetch an
    adapter performs, never the candidates it hands back. This assertion
    was inverted deliberately — it previously pinned ``limit=1`` returning
    one hit, which is the truncation the amendment removed."""
    index = InMemoryIndex()
    index.project(FACT_ONE)
    index.project(FACT_TWO)

    hits = index.search("retry", 1, (PARTITION,))

    assert hits == (FACT_ONE.fact_id, FACT_TWO.fact_id)
    assert all(type(hit) is UUID for hit in hits)


def test_search_is_ordered_deterministically() -> None:
    forwards = InMemoryIndex()
    forwards.project(FACT_ONE)
    forwards.project(FACT_TWO)
    backwards = InMemoryIndex()
    backwards.project(FACT_TWO)
    backwards.project(FACT_ONE)

    assert forwards.search("retry", 10, (PARTITION,)) == backwards.search(
        "retry", 10, (PARTITION,)
    )


def test_an_honest_index_confines_search_to_the_named_partitions() -> None:
    index = InMemoryIndex()
    index.project(FACT_ONE)
    index.project(FACT_THREE)

    assert index.search("the", 10, (PARTITION,)) == (FACT_ONE.fact_id,)
    assert index.search("the", 10, (SIBLING_PARTITION,)) == (FACT_THREE.fact_id,)
    assert set(index.search("the", 10, (PARTITION, SIBLING_PARTITION))) == {
        FACT_ONE.fact_id,
        FACT_THREE.fact_id,
    }


@pytest.mark.parametrize(
    "keys",
    [
        pytest.param(None, id="everything"),
        pytest.param((PARTITION,), id="named-partition"),
    ],
)
def test_clear_removes_what_it_was_asked_to(keys: tuple[str, ...] | None) -> None:
    index = InMemoryIndex()
    index.project(FACT_ONE)

    index.clear(keys)

    assert index.search("retry", 10, (PARTITION,)) == ()


def test_clear_of_another_partition_leaves_this_one_alone() -> None:
    index = InMemoryIndex()
    index.project(FACT_ONE)

    index.clear((SIBLING_PARTITION,))

    assert index.search("retry", 10, (PARTITION,)) == (FACT_ONE.fact_id,)


def test_a_refused_projection_is_a_typed_value_not_an_exception() -> None:
    """P-14/I-69's split, restated for this port: a domain refusal is
    returned so the deliverer can record a retryable attempt; only
    infrastructure failures raise."""
    outcome = RefusingIndex().project(FACT_ONE)

    assert outcome == ProjectionFailed(code="index_unavailable")


def test_a_hostile_index_returns_whatever_it_likes() -> None:
    """The port permits this deliberately. An index that returns unknown,
    duplicated and foreign identities while ignoring the partitions it was
    given is well within its contract — which is precisely why disclosure
    cannot depend on it."""
    unknown = UUID("99999999-9999-4999-8999-999999999999")
    index = HostileIndex((FACT_THREE.fact_id, unknown, unknown))

    hits = index.search("anything", 10, (PARTITION,))

    assert hits == (FACT_THREE.fact_id, unknown, unknown)
    assert index.searched_partitions == [(PARTITION,)]


def test_project_many_matches_repeated_project() -> None:
    index = MemoryIndex()
    states = tuple(
        fact(f"{i:08x}-1111-4111-8111-111111111111", f"the state number {i}")
        for i in range(3)
    )

    results = index.project_many(states)

    assert results == tuple(FactProjected() for _ in states)
    for state in states:
        assert index.search(state.body, 10, (state.partition_key,)) == (state.fact_id,)


def test_project_many_of_nothing_is_nothing() -> None:
    assert MemoryIndex().project_many(()) == ()
