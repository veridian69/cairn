"""Ephemeral, process-local recall snapshots behind opaque position tokens.

Holds identities, frozen scores and request bindings only: no bodies, raw
queries, credentials or vectors. One lock guards all state and is never held
across catalogue, provider or Attic I/O. Restart drops everything.
"""

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from cairn.authority.memory_page_types import Order, TimeBasis
from cairn.catalogue.audit import Scope, TrustClass

TTL = timedelta(seconds=300)
FIXED_BYTES = 1024
ENTRY_BYTES = 25  # 16-byte identity, 8-byte frozen score, availability bit
TOKEN_BYTES = 64  # 43-character token, snapshot number and position


@dataclass(frozen=True, slots=True)
class Binding:
    instance_id: str
    principal_id: UUID
    scope: Scope
    order: Order
    time_basis: TimeBasis | None
    relevant_only: bool
    trust_filters: frozenset[TrustClass]
    query_fingerprint: bytes
    policy: str
    semantic_degraded: bool
    ceiling: int
    selection_complete: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    number: int
    binding: Binding
    fact_ids: tuple[UUID, ...]
    scores: tuple[float, ...]
    source_available: tuple[bool, ...]
    created_at: datetime
    expires_at: datetime


class CapacityExceeded(Exception):
    pass


class SnapshotStore:
    def __init__(
        self,
        *,
        ttl: timedelta = TTL,
        per_principal: int = 4,
        per_process: int = 64,
        ceiling_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._ttl = ttl
        self._per_principal = per_principal
        self._per_process = per_process
        self._ceiling = ceiling_bytes
        self._lock = threading.Lock()
        self._next = 0
        self._snapshots: dict[int, Snapshot] = {}
        self._tokens: dict[str, tuple[int, int]] = {}
        self._positions: dict[tuple[int, int], str] = {}
        self._bytes = 0

    def _cost(self, snapshot: Snapshot) -> int:
        return FIXED_BYTES + ENTRY_BYTES * len(snapshot.fact_ids)

    def _footprint(self, number: int) -> int:
        tokens = sum(1 for key in self._positions if key[0] == number)
        return self._cost(self._snapshots[number]) + TOKEN_BYTES * tokens

    def _drop(self, number: int) -> None:
        snapshot = self._snapshots.pop(number)
        self._bytes -= self._cost(snapshot)
        for key in [k for k in self._positions if k[0] == number]:
            del self._tokens[self._positions.pop(key)]
            self._bytes -= TOKEN_BYTES

    def _purge(self, now: datetime) -> None:
        for number in [n for n, s in self._snapshots.items() if s.expires_at <= now]:
            self._drop(number)

    def _intern(self, number: int, position: int) -> str | None:
        existing = self._positions.get((number, position))
        if existing is not None:
            return existing
        if self._bytes + TOKEN_BYTES > self._ceiling:
            return None
        token = secrets.token_urlsafe(32)
        while token in self._tokens:
            token = secrets.token_urlsafe(32)
        self._tokens[token] = (number, position)
        self._positions[(number, position)] = token
        self._bytes += TOKEN_BYTES
        return token

    def publish(
        self,
        binding: Binding,
        fact_ids: tuple[UUID, ...],
        scores: tuple[float, ...],
        source_available: tuple[bool, ...],
        position: int,
        now: datetime,
    ) -> tuple[Snapshot, str]:
        with self._lock:
            self._purge(now)
            own = sorted(
                (
                    s
                    for s in self._snapshots.values()
                    if s.binding.principal_id == binding.principal_id
                ),
                key=lambda s: (s.created_at, s.number),
            )
            evict = own[: max(0, len(own) - self._per_principal + 1)]
            snapshot = Snapshot(
                self._next,
                binding,
                fact_ids,
                scores,
                source_available,
                now,
                now + self._ttl,
            )
            cost = self._cost(snapshot) + TOKEN_BYTES
            freed = sum(self._footprint(s.number) for s in evict)
            if (
                len(self._snapshots) - len(evict) >= self._per_process
                or self._bytes - freed + cost > self._ceiling
            ):
                raise CapacityExceeded
            for victim in evict:
                self._drop(victim.number)
            self._next += 1
            self._snapshots[snapshot.number] = snapshot
            self._bytes += self._cost(snapshot)
            token = self._intern(snapshot.number, position)
            assert token is not None
            return snapshot, token

    def resolve(self, token: str, now: datetime) -> tuple[Snapshot, int] | None:
        with self._lock:
            self._purge(now)
            located = self._tokens.get(token)
            if located is None:
                return None
            return self._snapshots[located[0]], located[1]

    def token_for(self, snapshot: Snapshot, position: int, now: datetime) -> str | None:
        with self._lock:
            self._purge(now)
            if self._snapshots.get(snapshot.number) is not snapshot:
                return None
            return self._intern(snapshot.number, position)

    def discard(self, snapshot: Snapshot) -> None:
        with self._lock:
            if self._snapshots.get(snapshot.number) is snapshot:
                self._drop(snapshot.number)

    def close(self) -> None:
        with self._lock:
            for number in list(self._snapshots):
                self._drop(number)

    def usage(self) -> tuple[int, int]:
        with self._lock:
            return len(self._snapshots), self._bytes
