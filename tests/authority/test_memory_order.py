from datetime import UTC, datetime, timedelta
from uuid import UUID

from test_retrieval import _retrieved_fact

from cairn.authority.memory_order import order_ids
from cairn.authority.memory_page_types import Order, TimeBasis
from cairn.authority.retrieval import RetrievedFact
from cairn.authority.source_time import SourceProjection

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _facts() -> dict[UUID, RetrievedFact]:
    a, b, c, d = (UUID(int=i) for i in (1, 2, 3, 4))
    return {
        a: _retrieved_fact(a, recorded_at=_T0 + timedelta(days=4)),
        b: _retrieved_fact(b, recorded_at=_T0 + timedelta(days=1)),
        c: _retrieved_fact(c, recorded_at=_T0 + timedelta(days=4)),
        d: _retrieved_fact(d, recorded_at=_T0 + timedelta(days=9)),
    }


def _no_key(_identity: UUID) -> tuple[object, ...]:
    return ()


def test_recorded_newest_breaks_equal_times_by_ascending_uuid() -> None:
    ids = order_ids(Order.NEWEST, TimeBasis.RECORDED, _facts(), _no_key, None)
    assert [i.int for i in ids] == [4, 1, 3, 2]


def test_recorded_oldest_keeps_ascending_uuid_ties() -> None:
    ids = order_ids(Order.OLDEST, TimeBasis.RECORDED, _facts(), _no_key, None)
    assert [i.int for i in ids] == [2, 1, 3, 4]


def test_source_order_puts_unavailable_last_ordered_by_recorded_time() -> None:
    facts = _facts()
    a, b, c, d = sorted(facts, key=lambda u: u.int)
    projection = {
        a: SourceProjection(_T0 - timedelta(days=300), None),
        b: SourceProjection(None, None),
        c: SourceProjection(_T0 - timedelta(days=10), None),
        d: SourceProjection(None, None),
    }
    newest = order_ids(Order.NEWEST, TimeBasis.SOURCE, facts, _no_key, projection)
    oldest = order_ids(Order.OLDEST, TimeBasis.SOURCE, facts, _no_key, projection)
    assert [i.int for i in newest] == [3, 1, 4, 2]
    assert [i.int for i in oldest] == [1, 3, 2, 4]


def test_relevance_uses_the_supplied_policy_key() -> None:
    facts = _facts()
    keys: dict[UUID, tuple[object, ...]] = {i: (-i.int,) for i in facts}
    ids = order_ids(Order.RELEVANCE, None, facts, keys.__getitem__, None)
    assert [i.int for i in ids] == [4, 3, 2, 1]
