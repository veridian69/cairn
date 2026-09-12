import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import cairn.evidence.attic as attic_module
from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.evidence.attic import ATTIC_FILENAME, AtticStorageError, SqliteAttic

_EVIDENCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_OTHER_EVIDENCE_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_store_then_fetch_round_trips_exact_bytes(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)

    stored = attic.store(_EVIDENCE_ID, b"exact evidence payload")
    fetched = attic.fetch(_EVIDENCE_ID)

    assert stored == PayloadStored()
    assert fetched == FetchedPayload(payload=b"exact evidence payload")


def test_restoring_identical_payload_is_an_idempotent_no_op(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"identical payload")

    result = attic.store(_EVIDENCE_ID, b"identical payload")

    assert result == PayloadStored()
    assert attic.fetch(_EVIDENCE_ID) == FetchedPayload(payload=b"identical payload")


def test_restoring_different_bytes_under_same_identity_is_corrupt_and_preserves_original(
    tmp_path: Path,
) -> None:
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"original payload")

    result = attic.store(_EVIDENCE_ID, b"different payload")

    assert result == PayloadCorrupt()
    assert attic.fetch(_EVIDENCE_ID) == FetchedPayload(payload=b"original payload")


def test_fetch_of_unknown_identity_is_absent(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)

    assert attic.fetch(_EVIDENCE_ID) == PayloadAbsent()


def test_fetch_of_directly_corrupted_row_is_corrupt(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"trustworthy payload")

    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "UPDATE payloads SET payload = ? WHERE evidence_id = ?",
            (b"tampered payload", str(_EVIDENCE_ID)),
        )
        connection.commit()

    assert attic.fetch(_EVIDENCE_ID) == PayloadCorrupt()


def test_search_matches_fts5_content_and_returns_identities_only(
    tmp_path: Path,
) -> None:
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"the quick brown fox")
    attic.store(_OTHER_EVIDENCE_ID, b"a slow green turtle")

    results = attic.search("fox", limit=10)

    assert results == (_EVIDENCE_ID,)


def test_search_honours_limit(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"shared term one")
    attic.store(_OTHER_EVIDENCE_ID, b"shared term two")

    results = attic.search("shared", limit=1)

    assert len(results) == 1
    assert results[0] in (_EVIDENCE_ID, _OTHER_EVIDENCE_ID)


def test_search_rejects_query_over_i30_8kib_bound(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    oversize_query = "x" * 8193

    with pytest.raises(AtticStorageError) as raised:
        attic.search(oversize_query, limit=10)

    assert raised.value.code == "query_too_large"


def test_search_accepts_query_at_exactly_8kib_bound(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    boundary_query = "x" * 8192

    assert attic.search(boundary_query, limit=10) == ()


@pytest.mark.parametrize("limit", [0, -1])
def test_search_rejects_invalid_limit(tmp_path: Path, limit: int) -> None:
    attic = SqliteAttic(tmp_path)

    with pytest.raises(AtticStorageError) as raised:
        attic.search("term", limit=limit)

    assert raised.value.code == "invalid_limit"


def test_search_rejects_malformed_fts5_syntax_with_typed_error(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)

    with pytest.raises(AtticStorageError) as raised:
        attic.search('"unterminated', limit=10)

    assert raised.value.code == "invalid_query"


def test_store_creates_group_writable_file_with_i49_durability_pragmas(
    tmp_path: Path,
) -> None:
    attic = SqliteAttic(tmp_path)

    attic.store(_EVIDENCE_ID, b"durability probe")

    attic_path = tmp_path / ATTIC_FILENAME
    assert attic_path == tmp_path / "attic.sqlite3"
    assert stat.S_IMODE(attic_path.stat().st_mode) == 0o660

    with attic_module._connect(tmp_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert connection.execute("PRAGMA trusted_schema").fetchone() == (0,)


def test_missing_fts5_build_is_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        attic_module,
        "_CREATE_FTS_SQL",
        "CREATE VIRTUAL TABLE payloads_fts USING fts5_nonexistent(content)",
    )
    attic = SqliteAttic(tmp_path)

    with pytest.raises(AtticStorageError) as raised:
        attic.store(_EVIDENCE_ID, b"payload")

    assert raised.value.code == "fts5_unavailable"


def test_store_uses_stable_evidence_identity_across_calls(tmp_path: Path) -> None:
    attic = SqliteAttic(tmp_path)
    evidence_id = uuid4()

    attic.store(evidence_id, b"identity probe")

    assert attic.fetch(evidence_id) == FetchedPayload(payload=b"identity probe")


def test_restoring_over_a_tampered_payload_is_corrupt_not_stored(
    tmp_path: Path,
) -> None:
    """The digest column alone cannot decide a re-store.

    Tampering with the payload and leaving the digest beside it untouched is
    the shape storage corruption actually takes here — the digest was written
    once, at first store, and nothing rewrites it. Comparing digests would
    match the honest caller's bytes against that stale column and report
    PayloadStored, and delivery would then delete the outbox row holding the
    last good copy of the payload. Only comparing the stored bytes catches it.
    """
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"trustworthy payload")

    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "UPDATE payloads SET payload = ? WHERE evidence_id = ?",
            (b"tampered payload", str(_EVIDENCE_ID)),
        )
        connection.commit()

    assert attic.store(_EVIDENCE_ID, b"trustworthy payload") == PayloadCorrupt()


def test_restoring_over_a_tampered_digest_is_corrupt_not_stored(
    tmp_path: Path,
) -> None:
    """The other half of the same row. Here the bytes are the caller's own and
    it is the digest column that has drifted, so a bytes-only comparison would
    call the row healthy. Both halves must agree before a re-store is the
    idempotent no-op."""
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"trustworthy payload")

    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "UPDATE payloads SET digest = ? WHERE evidence_id = ?",
            (b"not the real digest", str(_EVIDENCE_ID)),
        )
        connection.commit()

    assert attic.store(_EVIDENCE_ID, b"trustworthy payload") == PayloadCorrupt()


def test_a_failed_fts_insert_rolls_back_the_payload_row(tmp_path: Path) -> None:
    """The payload insert and the FTS insert that indexes it are one
    transaction.

    A plain table standing where the virtual one should be is the cheapest
    way to make the second insert fail after the first has succeeded:
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` leaves it alone, so schema
    preparation passes and the failure lands exactly between the two writes.
    Under autocommit the payload row would already be committed and would
    survive as a payload no ``search`` could ever find, with nothing to
    notice the gap.
    """
    tmp_path.joinpath(ATTIC_FILENAME).touch(mode=0o660)
    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute("CREATE TABLE payloads_fts(other TEXT)")
        connection.commit()
    attic = SqliteAttic(tmp_path)

    with pytest.raises(AtticStorageError):
        attic.store(_EVIDENCE_ID, b"payload that must not survive")

    assert attic.fetch(_EVIDENCE_ID) == PayloadAbsent()
    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payloads").fetchone() == (0,)


def test_a_failed_store_raises_the_typed_error_not_a_raw_sqlite_error(
    tmp_path: Path,
) -> None:
    """The adapter contract is that infrastructure failures arrive as
    ``AtticStorageError`` (adapter.py) — so nothing from ``sqlite3`` may
    escape ``store`` unwrapped.

    A consumer that reasonably catches ``AtticStorageError`` around a store
    would otherwise still be bitten by, say, ``sqlite3.OperationalError`` from
    ``BEGIN IMMEDIATE`` under lock contention, which the transaction wrapper
    made an ordinary occurrence rather than a corruption-only one. The forced
    insert failure below is used in preference to real contention because it
    reaches the same wrapper without waiting out the 5s busy timeout.
    """
    tmp_path.joinpath(ATTIC_FILENAME).touch(mode=0o660)
    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute("CREATE TABLE payloads_fts(other TEXT)")
        connection.commit()

    with pytest.raises(AtticStorageError) as caught:
        SqliteAttic(tmp_path).store(_EVIDENCE_ID, b"payload")

    assert caught.value.code == "attic_store_failed"
    # Chained, not swallowed: the operator still needs the underlying cause.
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    # The `except AtticStorageError: raise` arm that keeps _connect's more
    # precise codes from being flattened into this generic one is pinned by
    # test_missing_fts5_build_is_a_typed_error above, which asserts
    # fts5_unavailable survives a store call unchanged.


def test_a_failed_fetch_raises_the_typed_error_not_a_raw_sqlite_error(
    tmp_path: Path,
) -> None:
    """``fetch`` owes the same contract as ``store``: absence and corruption
    are domain values, so the only thing it may raise is
    ``AtticStorageError``.

    A ``payloads`` table without the column the read names is the real-path
    way to reach it — ``CREATE TABLE IF NOT EXISTS`` leaves the impostor
    alone, so schema preparation passes and the SELECT is what fails.
    """
    tmp_path.joinpath(ATTIC_FILENAME).touch(mode=0o660)
    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "CREATE TABLE payloads ("
            "id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL UNIQUE, "
            "digest BLOB NOT NULL) STRICT"
        )
        connection.commit()

    with pytest.raises(AtticStorageError) as caught:
        SqliteAttic(tmp_path).fetch(_EVIDENCE_ID)

    assert caught.value.code == "attic_fetch_failed"
    assert isinstance(caught.value.__cause__, sqlite3.Error)


def test_a_corrupt_database_makes_search_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``search``'s gap was the narrowest and the most misleading: it already
    caught ``OperationalError`` around its query, so it looked handled, while
    every other ``sqlite3`` failure escaped raw.

    ``DatabaseError`` — what a corrupt page actually raises — is the parent of
    ``OperationalError``, not an instance of it, so the existing inner catch
    never saw it. Corruption is stubbed rather than manufactured: forcing a
    malformed page that survives schema reading but fails the row read is
    version-dependent and would make this test flake, and what is under test
    is the translation, not SQLite's behaviour.
    """

    class _CorruptConnection:
        def execute(self, *args: object) -> object:
            raise sqlite3.DatabaseError("database disk image is malformed")

    @contextmanager
    def _corrupt_connect(data_path: Path) -> Iterator[_CorruptConnection]:
        yield _CorruptConnection()

    monkeypatch.setattr(attic_module, "_connect", _corrupt_connect)

    with pytest.raises(AtticStorageError) as caught:
        SqliteAttic(tmp_path).search("term", limit=10)

    assert caught.value.code == "attic_search_failed"
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    # That the outer arm does not swallow the inner one — a caller's bad FTS5
    # syntax staying invalid_query rather than becoming the generic search
    # failure — is pinned by test_search_rejects_malformed_fts5_syntax_with_
    # typed_error above.


def test_a_corrupted_stored_identity_is_a_typed_error_not_a_raw_value_error(
    tmp_path: Path,
) -> None:
    """``search`` returns identities read straight out of the database, and
    ``payloads.evidence_id`` being ``TEXT NOT NULL`` constrains storage class,
    not meaning.

    Text that is no UUID makes ``UUID()`` raise ``ValueError``, which is
    neither a ``sqlite3`` error nor an ``AtticStorageError``, so it escaped
    every wrapper around it. Nothing in this slice calls ``search``, so this
    was unreachable when written — which is precisely why it is closed now
    rather than left to surface once a retrieval path makes it reachable.
    """
    attic = SqliteAttic(tmp_path)
    attic.store(_EVIDENCE_ID, b"findme in the index")

    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "UPDATE payloads SET evidence_id = ? WHERE evidence_id = ?",
            ("not-a-uuid-at-all", str(_EVIDENCE_ID)),
        )
        connection.commit()

    with pytest.raises(AtticStorageError) as caught:
        attic.search("findme", limit=10)

    assert caught.value.code == "attic_corrupt"
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_stored_identity_of_the_wrong_type_is_also_a_typed_error(
    tmp_path: Path,
) -> None:
    """The other arm, and the reason it cannot be folded into the
    ``ValueError`` guard above: ``UUID()`` calls ``.replace`` on its argument
    before validating it, so a non-string raises ``AttributeError``, not
    ``ValueError``.

    Reachable only from a ``payloads`` table without ``STRICT`` — that is, a
    database restored from a pre-``STRICT`` schema rather than one this
    module created.
    """
    tmp_path.joinpath(ATTIC_FILENAME).touch(mode=0o660)
    with sqlite3.connect(tmp_path / ATTIC_FILENAME) as connection:
        connection.execute(
            "CREATE TABLE payloads ("
            "id INTEGER PRIMARY KEY, evidence_id, payload BLOB, digest BLOB)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE payloads_fts "
            "USING fts5(content, content='', tokenize='unicode61')"
        )
        connection.execute(
            "INSERT INTO payloads(id, evidence_id, payload, digest) "
            "VALUES (1, 12345, x'00', x'00')"
        )
        connection.execute(
            "INSERT INTO payloads_fts(rowid, content) VALUES (1, 'findme')"
        )
        connection.commit()

    with pytest.raises(AtticStorageError) as caught:
        SqliteAttic(tmp_path).search("findme", limit=10)

    assert caught.value.code == "attic_corrupt"
