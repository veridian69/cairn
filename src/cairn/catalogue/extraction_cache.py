"""P-90 projection cache storage (accepted 28 August 2026).

Keys and payloads are opaque strings: their construction needs
graphiti-specific knowledge that belongs to the projection seam
(``cairn.projection.graphiti_extraction_cache``), not the catalogue.

Failure posture, accepted with the design: a read that fails for any
reason is a miss returning ``None``; a write that fails is logged and
dropped — the projection result is already in hand and must not be
forfeited to a cache problem. Nothing here ever raises to a caller.

Diagnostics travel only the I-32 safe stream (R4): composition threads
its ``SafeLogger`` through, events carry closed cache-kind/reason enums
and bounded counters, and with no logger the store is silent — there is
no ordinary-logger fallback, so no table name, payload or exception
text can reach a log line.
"""

import logging
import sqlite3
from _thread import LockType
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import (
    CatalogueContention,
    CatalogueTransactionError,
    _begin_immediate,
    _write_transaction,
)
from cairn.runtime.logging import CacheKind, CacheReason, LogEvent, SafeLogger

_CACHE_KIND_BY_TABLE = {
    "projection_extraction_cache": CacheKind.EXTRACTION,
    "projection_embedding_cache": CacheKind.EMBEDDING,
}


class ExtractionCacheStore:
    def __init__(
        self,
        data_path: Path,
        *,
        writer_gate: LockType,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        logger: SafeLogger | None = None,
    ) -> None:
        self._data_path = data_path
        self._logger = logger
        # I3/R5: every catalogue writer takes CatalogueTransactions'
        # in-process writer gate before opening BEGIN IMMEDIATE — one
        # gate, required, never optional: an ungated store is how a burst
        # of cache writes could exhaust the busy timeout and make a real
        # mutation fail as dependency_unavailable. Composition passes the
        # same gate object CatalogueTransactions holds; reads never take
        # it.
        self._writer_gate = writer_gate
        self._clock = clock

    def _read(
        self,
        table: str,
        cache_key: str,
        fact_id: str | None = None,
        kind: CacheKind | None = None,
    ) -> str | None:
        try:
            with read_connection(self._data_path) as connection:
                if fact_id is None:
                    # R3: extraction rows are fact-owned, so one cache_key
                    # may hold several rows there. A keyless read (the
                    # shared timestamp path, whose valid outputs are
                    # content-pure under the accepted key) must still be
                    # deterministic — always the lowest fact_id, never
                    # whichever row the engine happens to return. The
                    # embedding table keys on cache_key alone and has no
                    # fact_id column to order by.
                    order = (
                        " ORDER BY fact_id LIMIT 1"
                        if table == "projection_extraction_cache"
                        else ""
                    )
                    row = connection.execute(
                        f"SELECT payload FROM {table} WHERE cache_key = ?{order}",
                        (cache_key,),
                    ).fetchone()
                else:
                    # C1: the key is content-scoped, so two distinct facts
                    # with byte-identical bodies share it. A caller that
                    # knows which fact it wants passes fact_id here; a row
                    # owned by a different fact reads as a miss.
                    row = connection.execute(
                        f"SELECT payload FROM {table}"
                        " WHERE cache_key = ? AND fact_id = ?",
                        (cache_key, fact_id),
                    ).fetchone()
        except (CatalogueStorageError, sqlite3.Error):
            if self._logger is not None:
                self._logger.emit(
                    LogEvent.PROJECTION_CACHE_MISS,
                    level=logging.WARNING,
                    transport=None,
                    cache_kind=kind or _CACHE_KIND_BY_TABLE[table],
                    cache_reason=CacheReason.READ_FAILED,
                )
            return None
        if row is None:
            return None
        payload = row[0]
        return payload if isinstance(payload, str) else None

    def _write(
        self,
        table: str,
        statement: str,
        rows: list[tuple[str, ...]],
        kind: CacheKind | None = None,
    ) -> None:
        if not rows:
            return
        try:
            with self._writer_gate:
                with _write_transaction(self._data_path) as connection:
                    _begin_immediate(connection)
                    try:
                        connection.executemany(statement, rows)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
        except (
            CatalogueStorageError,
            CatalogueContention,
            CatalogueTransactionError,
            sqlite3.Error,
        ):
            if self._logger is not None:
                self._logger.emit(
                    LogEvent.PROJECTION_CACHE_WRITE_DROPPED,
                    level=logging.WARNING,
                    transport=None,
                    cache_kind=kind or _CACHE_KIND_BY_TABLE[table],
                    cache_rows=len(rows),
                )

    def get(
        self,
        cache_key: str,
        fact_id: str | None = None,
        *,
        kind: CacheKind | None = None,
    ) -> str | None:
        # ``kind`` names the cache the caller speaks for. Seam 2 (edge
        # timestamps) shares this table under its own key shape, so
        # without it every timestamp-cache failure would be reported to
        # operators as an extraction-cache failure and CacheKind.TIMESTAMPS
        # could never appear on a store-level event at all.
        return self._read("projection_extraction_cache", cache_key, fact_id, kind)

    def put(
        self,
        cache_key: str,
        fact_id: str,
        payload: str,
        *,
        kind: CacheKind | None = None,
    ) -> None:
        self.put_many([(cache_key, fact_id, payload)], kind=kind)

    def put_many(
        self, rows: list[tuple[str, str, str]], *, kind: CacheKind | None = None
    ) -> None:
        created_at = canonical_timestamp(self._clock())
        self._write(
            "projection_extraction_cache",
            "INSERT OR IGNORE INTO projection_extraction_cache"
            " (cache_key, fact_id, payload, created_at) VALUES (?, ?, ?, ?)",
            [(key, fact_id, payload, created_at) for key, fact_id, payload in rows],
            kind,
        )

    def get_embedding(self, cache_key: str) -> str | None:
        return self._read("projection_embedding_cache", cache_key)

    def put_embedding(self, cache_key: str, payload: str) -> None:
        self.put_embedding_many([(cache_key, payload)])

    def put_embedding_many(self, rows: list[tuple[str, str]]) -> None:
        created_at = canonical_timestamp(self._clock())
        self._write(
            "projection_embedding_cache",
            "INSERT OR IGNORE INTO projection_embedding_cache"
            " (cache_key, payload, created_at) VALUES (?, ?, ?)",
            [(cache_key, payload, created_at) for cache_key, payload in rows],
        )

    def _repair_embedding_many(self, rows: list[tuple[str, str, str]]) -> None:
        """CAS opaque payloads observed invalid by the projection wrapper.

        A changed row wins, even if the caller would also consider it invalid.
        Ordinary misses continue to use put_embedding_many's INSERT OR IGNORE.
        """
        unique: dict[str, tuple[str, str]] = {}
        for key, observed, replacement in rows:
            unique.setdefault(key, (observed, replacement))
        created_at = canonical_timestamp(self._clock())
        self._write(
            "projection_embedding_cache",
            "INSERT INTO projection_embedding_cache"
            " (cache_key, payload, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(cache_key) DO UPDATE SET"
            " payload = excluded.payload, created_at = excluded.created_at"
            " WHERE projection_embedding_cache.payload = ?",
            [
                (key, replacement, created_at, observed)
                for key, (observed, replacement) in unique.items()
            ],
        )
