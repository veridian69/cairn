"""memory-order/v1: requested time direction, ascending canonical UUID ties.

Source order never interleaves import dates with source dates: facts without an
authorised source time form a final group ordered by recorded time.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID

from cairn.authority.memory_page_types import Order, TimeBasis
from cairn.authority.retrieval import RetrievedFact
from cairn.authority.source_time import SourceProjection

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: datetime) -> int:
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def order_ids(
    order: Order,
    basis: TimeBasis | None,
    facts: Mapping[UUID, RetrievedFact],
    relevance_key: Callable[[UUID], tuple[object, ...]],
    projection: Mapping[UUID, SourceProjection] | None,
) -> list[UUID]:
    if order is Order.RELEVANCE:
        return sorted(facts, key=relevance_key)
    direction = -1 if order is Order.NEWEST else 1
    if basis is TimeBasis.RECORDED:
        return sorted(
            facts,
            key=lambda i: (direction * _micros(facts[i].recorded_at), str(i)),
        )
    assert basis is TimeBasis.SOURCE and projection is not None

    def source_key(identity: UUID) -> tuple[int, int, str]:
        observed = projection[identity].observed_at
        if observed is None:
            return (1, direction * _micros(facts[identity].recorded_at), str(identity))
        return (0, direction * _micros(observed), str(identity))

    return sorted(facts, key=source_key)
