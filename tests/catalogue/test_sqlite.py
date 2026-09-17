import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import cairn.catalogue.sqlite as catalogue_sqlite
from cairn.catalogue.sqlite import (
    APPLICATION_ID,
    CATALOGUE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_read_connection,
    _open_verification_connection,
    _open_write_connection,
    canonical_timestamp,
    parse_timestamp,
)


def _create_current_catalogue(data_path: Path) -> None:
    with _open_write_connection(data_path, create=True) as connection:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


def test_fresh_writer_creates_portable_catalogue_with_fixed_profile(
    tmp_path: Path,
) -> None:
    catalogue_path = tmp_path / CATALOGUE_FILENAME

    with _open_write_connection(tmp_path, create=True) as connection:
        assert catalogue_path.is_file()
        assert stat.S_IMODE(catalogue_path.stat().st_mode) == 0o660
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert connection.execute("PRAGMA trusted_schema").fetchone() == (0,)

        connection.execute("CREATE TABLE profile_probe(value INTEGER)")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{catalogue_path}{suffix}")
            assert sidecar.is_file()
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o660


def test_writer_enforces_foreign_keys_without_translating_operation_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with _open_write_connection(tmp_path, create=True) as connection:
            connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE child("
                "id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES parent(id))"
            )
            connection.execute("INSERT INTO child(id, parent_id) VALUES (1, 99)")


def test_existing_portable_catalogue_reopens_without_owner_assumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _open_write_connection(tmp_path, create=True):
        pass

    real_fstat = os.fstat

    def report_replacement_owner(file_descriptor: int) -> SimpleNamespace:
        status = real_fstat(file_descriptor)
        return SimpleNamespace(st_mode=status.st_mode, st_uid=status.st_uid + 1)

    monkeypatch.setattr(os, "fstat", report_replacement_owner)

    with _open_write_connection(tmp_path, create=False) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_writer_rejects_catalogue_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o660)
    (tmp_path / CATALOGUE_FILENAME).symlink_to(target)

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_write_connection(tmp_path, create=False):
            pass

    assert caught.value.code == "catalogue_file_unavailable"


def test_writer_rejects_catalogue_special_file(tmp_path: Path) -> None:
    catalogue_path = tmp_path / CATALOGUE_FILENAME
    os.mkfifo(catalogue_path, mode=0o660)

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_write_connection(tmp_path, create=False):
            pass

    assert caught.value.code == "catalogue_file_invalid"


def test_writer_rejects_catalogue_with_incompatible_mode(tmp_path: Path) -> None:
    catalogue_path = tmp_path / CATALOGUE_FILENAME
    catalogue_path.touch(mode=0o600)

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_write_connection(tmp_path, create=False):
            pass

    assert caught.value.code == "catalogue_file_invalid"


def test_read_and_verification_connections_are_short_lived_and_read_only(
    tmp_path: Path,
) -> None:
    _create_current_catalogue(tmp_path)

    with _open_read_connection(tmp_path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert connection.execute("PRAGMA trusted_schema").fetchone() == (0,)
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with _open_verification_connection(tmp_path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value INTEGER)")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    ("pragma", "value"),
    [
        ("application_id", APPLICATION_ID + 1),
        ("user_version", CURRENT_SCHEMA_VERSION + 1),
    ],
)
def test_reader_rejects_wrong_catalogue_header_identity(
    tmp_path: Path,
    pragma: str,
    value: int,
) -> None:
    _create_current_catalogue(tmp_path)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(f"PRAGMA {pragma} = {value}")

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_read_connection(tmp_path):
            pass

    assert caught.value.code == "catalogue_identity_mismatch"


def test_open_rejects_sqlite_without_required_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalogue_sqlite, "_sqlite_version_info", lambda: (3, 36, 0))

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_write_connection(tmp_path, create=True):
            pass

    assert caught.value.code == "sqlite_version_unsupported"
    assert not (tmp_path / CATALOGUE_FILENAME).exists()


def test_create_refuses_to_replace_existing_catalogue(tmp_path: Path) -> None:
    _create_current_catalogue(tmp_path)

    with pytest.raises(CatalogueStorageError) as caught:
        with _open_write_connection(tmp_path, create=True):
            pass

    assert caught.value.code == "catalogue_file_exists"


def test_canonical_timestamp_renders_27_character_utc_form() -> None:
    value = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)

    rendered = canonical_timestamp(value)

    assert rendered == "2026-08-05T10:11:12.123456Z"
    assert len(rendered) == 27


def test_parse_timestamp_round_trips_canonical_form() -> None:
    parsed = parse_timestamp("2026-08-05T10:11:12.123456Z")

    assert parsed == datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-08-05T10:11:12.123456",
        "2026-08-05 10:11:12.123456Z",
        "not-a-timestamp",
        "2026-08-05T10:11:12Z",
    ],
)
def test_parse_timestamp_rejects_malformed_text_with_typed_error(text: str) -> None:
    with pytest.raises(CatalogueStorageError) as caught:
        parse_timestamp(text)

    assert caught.value.code == "timestamp_malformed"


@settings(max_examples=200, deadline=None)
@given(
    value=st.datetimes(
        min_value=datetime(1000, 1, 1),
        max_value=datetime(9999, 12, 31, 23, 59, 59, 999999),
    ).map(lambda naive: naive.replace(tzinfo=UTC))
)
def test_canonical_timestamp_and_parse_timestamp_round_trip(value: datetime) -> None:
    assert parse_timestamp(canonical_timestamp(value)) == value
