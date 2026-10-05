import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cairn.authority.memory_page_types import Order
from cairn.authority.recall_snapshots import (
    Binding,
    CapacityExceeded,
    Snapshot,
    SnapshotStore,
)
from cairn.catalogue.audit import Scope

_NOW = datetime(2026, 10, 5, tzinfo=UTC)
_IDS = tuple(UUID(int=i, version=4) for i in range(1, 6))


def _binding(principal: UUID | None = None) -> Binding:
    return Binding(
        "instance",
        principal or UUID(int=99, version=4),
        Scope("acme", ()),
        Order.RELEVANCE,
        None,
        True,
        frozenset(),
        b"\x00" * 32,
        "policy",
        False,
        2,
        True,
    )


def _publish(
    store: SnapshotStore, binding: Binding, now: datetime = _NOW
) -> tuple[Snapshot, str]:
    return store.publish(binding, _IDS, (1.0,) * 5, (False,) * 5, 2, now)


def test_token_resolves_to_its_immutable_position() -> None:
    store = SnapshotStore()
    snapshot, token = _publish(store, _binding())
    assert len(token) == 43
    assert store.resolve(token, _NOW) == (snapshot, 2)


def test_expired_and_invented_tokens_resolve_identically_to_none() -> None:
    store = SnapshotStore()
    _, token = _publish(store, _binding())
    assert store.resolve(token, _NOW + timedelta(seconds=300)) is None
    assert store.resolve("A" * 43, _NOW) is None


def test_fifth_snapshot_for_one_principal_evicts_its_own_oldest() -> None:
    store = SnapshotStore()
    principal = uuid4()
    tokens = [
        _publish(store, _binding(principal), _NOW + timedelta(seconds=i))[1]
        for i in range(5)
    ]
    assert store.resolve(tokens[0], _NOW + timedelta(seconds=5)) is None
    assert all(store.resolve(t, _NOW + timedelta(seconds=5)) for t in tokens[1:])


def test_process_cap_refuses_rather_than_evicting_another_principal() -> None:
    store = SnapshotStore(per_process=2)
    first = _publish(store, _binding(uuid4()))[1]
    _publish(store, _binding(uuid4()))
    with pytest.raises(CapacityExceeded):
        _publish(store, _binding(uuid4()))
    assert store.resolve(first, _NOW) is not None


def test_byte_ceiling_counts_entries_and_tokens() -> None:
    store = SnapshotStore(ceiling_bytes=1024 + 25 * 5 + 64)
    snapshot, _ = _publish(store, _binding())
    assert store.usage() == (1, 1024 + 25 * 5 + 64)
    assert store.token_for(snapshot, 3, _NOW) is None  # would exceed ceiling


def test_concurrent_interning_creates_one_token_per_position() -> None:
    store = SnapshotStore()
    snapshot, _ = _publish(store, _binding())
    seen: list[str | None] = []
    threads = [
        threading.Thread(target=lambda: seen.append(store.token_for(snapshot, 4, _NOW)))
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 1 and seen[0] is not None
    assert store.usage()[1] == 1024 + 25 * 5 + 2 * 64


def test_discard_and_close_drop_every_token() -> None:
    store = SnapshotStore()
    snapshot, token = _publish(store, _binding())
    store.discard(snapshot)
    assert store.resolve(token, _NOW) is None
    _, other = _publish(store, _binding())
    store.close()
    assert store.resolve(other, _NOW) is None and store.usage() == (0, 0)


def test_refused_publish_does_not_evict_own_oldest() -> None:
    store = SnapshotStore(per_process=4)
    principal = uuid4()
    tokens = [
        _publish(store, _binding(principal), _NOW + timedelta(seconds=i))[1]
        for i in range(4)
    ]
    with pytest.raises(CapacityExceeded):
        _publish(store, _binding(uuid4()), _NOW + timedelta(seconds=4))
    # byte ceiling: own eviction frees too little for a larger snapshot
    small = SnapshotStore(ceiling_bytes=4 * (1024 + 25 * 5 + 64))
    owner = uuid4()
    kept = [
        _publish(small, _binding(owner), _NOW + timedelta(seconds=i))[1]
        for i in range(4)
    ]
    big_ids = tuple(UUID(int=i, version=4) for i in range(1, 50))
    with pytest.raises(CapacityExceeded):
        small.publish(
            _binding(owner),
            big_ids,
            (1.0,) * 49,
            (False,) * 49,
            2,
            _NOW + timedelta(seconds=4),
        )
    at = _NOW + timedelta(seconds=5)
    assert all(store.resolve(t, at) for t in tokens)
    assert all(small.resolve(t, at) for t in kept)
