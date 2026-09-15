"""Attic: the real SQLite-backed ``AtticAdapter`` (I-69).

A separate database from the catalogue, at ``<paths.data>/attic.sqlite3``,
carrying the I-49 durability profile and an FTS5 index. Digests are
verified on every read, so storage-level corruption surfaces as
``PayloadCorrupt`` rather than silently returning tampered bytes; a
re-store against an existing identity compares the stored bytes
themselves, for the same reason.
"""

import hashlib
import os
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)

ATTIC_FILENAME = "attic.sqlite3"

_ATTIC_OPEN_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_QUERY_MAX_BYTES = 8192

_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS payloads ("
    "id INTEGER PRIMARY KEY, "
    "evidence_id TEXT NOT NULL UNIQUE, "
    "payload BLOB NOT NULL, "
    "digest BLOB NOT NULL"
    ") STRICT"
)
_CREATE_FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS payloads_fts "
    "USING fts5(content, content='', tokenize='unicode61')"
)


class AtticStorageError(Exception):
    """Closed vocabulary of Attic infrastructure and argument-validation
    codes.

    Codes: ``fts5_unavailable``, ``attic_file_invalid``,
    ``attic_file_unavailable``, ``attic_open_failed``,
    ``attic_profile_unavailable``, ``attic_store_failed``,
    ``attic_fetch_failed``, ``attic_search_failed``, ``attic_corrupt``,
    ``query_too_large``, ``invalid_query``, ``invalid_limit``.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"attic storage error: {code}")


class SqliteAttic:
    """Real Attic adapter (I-69): implements ``AtticAdapter`` against
    ``<paths.data>/attic.sqlite3``."""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        digest = _digest(payload)
        # Every storage failure leaves as AtticStorageError. Domain outcomes
        # are returned, never raised (adapter.py), so the only thing a caller
        # must catch is this one type — and until this wrapper existed the
        # contract said so while SQLite's own exceptions escaped underneath
        # it. ``_write_transaction``'s BEGIN IMMEDIATE makes that reachable
        # on ordinary lock contention, not just on corruption.
        try:
            with (
                _connect(self._data_path) as connection,
                _write_transaction(connection),
            ):
                existing = connection.execute(
                    "SELECT payload, digest FROM payloads WHERE evidence_id = ?",
                    (str(evidence_id),),
                ).fetchone()
                if existing is not None:
                    # The stored bytes decide, not the stored digest.
                    # Comparing digest columns alone would report a re-store
                    # of an already-corrupted row as PayloadStored: the column
                    # still holds the digest written at first store, so it
                    # matches whatever the caller now offers, while the
                    # payload beside it has drifted. Delivery would then
                    # delete the outbox row that was the last copy of the real
                    # bytes. Both halves are checked, so a tampered payload
                    # and a tampered digest are each PayloadCorrupt, and only
                    # a row that is byte-identical and self-consistent is the
                    # idempotent no-op (I-69).
                    stored_payload, stored_digest = existing
                    if stored_payload == payload and stored_digest == digest:
                        return PayloadStored()
                    return PayloadCorrupt()
                row_id = connection.execute(
                    "INSERT INTO payloads(evidence_id, payload, digest) "
                    "VALUES (?, ?, ?)",
                    (str(evidence_id), payload, digest),
                ).lastrowid
                connection.execute(
                    "INSERT INTO payloads_fts(rowid, content) VALUES (?, ?)",
                    (row_id, payload.decode("utf-8", errors="replace")),
                )
                return PayloadStored()
        # Re-raised first and unchanged: _connect's own typed failures already
        # name what went wrong more precisely than attic_store_failed could,
        # and AtticStorageError is not a sqlite3.Error, so this is explicit
        # rather than load-bearing.
        except AtticStorageError:
            raise
        except sqlite3.Error as error:
            raise AtticStorageError("attic_store_failed") from error

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        # As for store: a missing payload and a corrupt one are returned as
        # domain values, so the only thing raised is AtticStorageError.
        try:
            with _connect(self._data_path) as connection:
                row = connection.execute(
                    "SELECT payload, digest FROM payloads WHERE evidence_id = ?",
                    (str(evidence_id),),
                ).fetchone()
        except AtticStorageError:
            raise
        except sqlite3.Error as error:
            raise AtticStorageError("attic_fetch_failed") from error
        if row is None:
            return PayloadAbsent()
        payload, digest = row
        if _digest(payload) != digest:
            return PayloadCorrupt()
        return FetchedPayload(payload=payload)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        if type(query) is not str or len(query.encode("utf-8")) > _QUERY_MAX_BYTES:
            raise AtticStorageError("query_too_large")
        if type(limit) is not int or limit < 1:
            raise AtticStorageError("invalid_limit")
        # The inner catch stays as it is: a caller's malformed FTS5 syntax is
        # reported as invalid_query, which is the more useful answer and is
        # already the documented one. The outer arm exists for what that
        # cannot name — a corrupt database surfacing as sqlite3.DatabaseError
        # rather than OperationalError, which previously escaped raw and made
        # this method the worst of the three: it looked handled.
        try:
            with _connect(self._data_path) as connection:
                try:
                    rows = connection.execute(
                        "SELECT p.evidence_id FROM payloads_fts "
                        "JOIN payloads p ON p.id = payloads_fts.rowid "
                        "WHERE payloads_fts MATCH ? ORDER BY p.id LIMIT ?",
                        (query, limit),
                    ).fetchall()
                except sqlite3.OperationalError as error:
                    raise AtticStorageError("invalid_query") from error
        except AtticStorageError:
            raise
        except sqlite3.Error as error:
            raise AtticStorageError("attic_search_failed") from error
        return tuple(_stored_identity(row[0]) for row in rows)


def _digest(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def _stored_identity(value: object) -> UUID:
    """The single reader of ``payloads.evidence_id``.

    ``STRICT`` with ``TEXT NOT NULL`` bounds the column's storage class and
    says nothing about its meaning: the text it admits includes text that is
    no UUID at all, and a catalogue restored from a pre-``STRICT`` schema —
    or written by anything other than ``store`` — can hold a value of another
    type entirely. This is the same shape-without-meaning gap the reading
    guards in ``cairn.evidence.delivery`` and
    ``cairn.evidence.reconciliation`` exist to close, reached here through
    ``search``'s only unguarded read.

    Both arms are needed. ``UUID()`` raises ``ValueError`` for malformed
    text, but for a non-string it reaches ``.replace`` before it validates
    anything and raises ``AttributeError`` instead — so a type check cannot
    be folded into the ``ValueError`` guard. Either would otherwise escape
    ``search`` raw, which is exactly the contract breach the wrappers around
    it were just added to close.
    """
    if type(value) is not str:
        raise AtticStorageError("attic_corrupt")
    try:
        return UUID(value)
    except ValueError as error:
        raise AtticStorageError("attic_corrupt") from error


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """One transaction spanning a read-then-write sequence.

    ``_connect`` runs the connection in autocommit (``isolation_level=None``),
    under which every statement commits on its own. That is wrong for
    ``store`` twice over: the ``payloads`` insert and the ``payloads_fts``
    insert that indexes it would be two separate commits, so a crash or an
    FTS failure between them would leave a stored payload absent from the
    index — invisible to ``search`` while ``fetch`` still returns it, with
    nothing to notice the gap; and the existence check would be committed
    apart from the insert that depends on it, so a second writer could take
    the same identity in between and turn the insert into a raw UNIQUE
    violation. ``BEGIN IMMEDIATE`` takes the write lock before the check
    reads, so the check and the writes see one world and land together or
    not at all.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


@contextmanager
def _connect(data_path: Path) -> Iterator[sqlite3.Connection]:
    attic_path = _prepare_attic_path(data_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{attic_path.as_uri()}?mode=rw&nofollow=1",
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        # Match catalogue custody: FULL alone does not request Darwin's
        # stronger flush of drive buffers, and fullfsync is connection-local.
        if sys.platform == "darwin":
            connection.execute("PRAGMA fullfsync = ON")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal_mode != ("wal",):
            raise AtticStorageError("attic_profile_unavailable")
        connection.execute("PRAGMA synchronous = FULL")
        _verify_profile(connection)
        _ensure_schema(connection)
    except AtticStorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise AtticStorageError("attic_open_failed") from error

    try:
        yield connection
    finally:
        connection.close()


def _prepare_attic_path(data_path: Path) -> Path:
    os.umask(0o007)
    directory_fd: int | None = None
    attic_fd: int | None = None
    try:
        directory_fd = os.open(
            data_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        attic_fd = os.open(
            ATTIC_FILENAME,
            _ATTIC_OPEN_FLAGS,
            0o660,
            dir_fd=directory_fd,
        )
        status = os.fstat(attic_fd)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o660:
            raise AtticStorageError("attic_file_invalid")
    except AtticStorageError:
        raise
    except OSError as error:
        raise AtticStorageError("attic_file_unavailable") from error
    finally:
        if attic_fd is not None:
            os.close(attic_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    return data_path / ATTIC_FILENAME


def _verify_profile(connection: sqlite3.Connection) -> None:
    expected = {
        "busy_timeout": 5000,
        "foreign_keys": 1,
        "synchronous": 2,
        "trusted_schema": 0,
    }
    if sys.platform == "darwin":
        expected["fullfsync"] = 1
    for pragma, value in expected.items():
        if connection.execute(f"PRAGMA {pragma}").fetchone() != (value,):
            raise AtticStorageError("attic_profile_unavailable")


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_CREATE_TABLE_SQL)
    try:
        connection.execute(_CREATE_FTS_SQL)
    except sqlite3.OperationalError as error:
        raise AtticStorageError("fts5_unavailable") from error
