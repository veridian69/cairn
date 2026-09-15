"""Connection-level durability, including Darwin's stronger flush request."""

import sqlite3
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from cairn.catalogue.sqlite import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_read_connection,
    _open_verification_connection,
    _open_write_connection,
)
from cairn.evidence.attic import AtticStorageError, _connect

_CONNECTION_KINDS = ("writer", "reader", "verification", "attic")


def _connection(kind: str, data: Path) -> AbstractContextManager[sqlite3.Connection]:
    if kind == "attic":
        return _connect(data)
    if kind == "reader":
        return _open_read_connection(data)
    if kind == "verification":
        return _open_verification_connection(data)
    assert kind == "writer"
    return _open_write_connection(data, create=False)


def _seed_catalogue(data: Path) -> None:
    with _open_write_connection(data, create=True) as connection:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


@pytest.mark.parametrize("kind", _CONNECTION_KINDS)
@pytest.mark.parametrize("platform,expected", [("darwin", 1), ("linux", 0)])
def test_every_connection_requests_the_platform_durability_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    platform: str,
    expected: int,
) -> None:
    _seed_catalogue(tmp_path)
    monkeypatch.setattr(sys, "platform", platform)

    # Reopen too: fullfsync is connection-local, not a persistent DB property.
    for _ in range(2):
        with _connection(kind, tmp_path) as connection:
            assert connection.execute("PRAGMA fullfsync").fetchone() == (expected,)
            assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("kind", _CONNECTION_KINDS)
@pytest.mark.parametrize("ignored_operation", ["write", "read"])
def test_darwin_refuses_a_connection_without_verified_fullfsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    ignored_operation: str,
) -> None:
    _seed_catalogue(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    real_connect = sqlite3.connect

    def authorise(
        operation: int,
        argument: str | None,
        value: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if operation == sqlite3.SQLITE_PRAGMA and argument == "fullfsync":
            if (value is None) == (ignored_operation == "read"):
                return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK

    def connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(database, **kwargs)
        # Emulate a VFS/build that ignores the request or cannot report it;
        # keep the real SQLite connection and all other storage behaviour.
        connection.set_authorizer(authorise)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    expected_error: type[AtticStorageError | CatalogueStorageError] = (
        AtticStorageError if kind == "attic" else CatalogueStorageError
    )
    expected_code = (
        "attic_profile_unavailable" if kind == "attic" else "sqlite_profile_unavailable"
    )

    with pytest.raises(expected_error) as caught:
        with _connection(kind, tmp_path):
            pytest.fail("An unverified Darwin connection was exposed to its caller")

    assert isinstance(caught.value, (AtticStorageError, CatalogueStorageError))
    assert caught.value.code == expected_code
