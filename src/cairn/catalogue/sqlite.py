import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

CATALOGUE_FILENAME = "catalogue.sqlite3"
APPLICATION_ID = 0x43414952
CURRENT_SCHEMA_VERSION = 13
MINIMUM_SQLITE_VERSION = (3, 38, 0)

_CATALOGUE_OPEN_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class CatalogueStorageError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"catalogue storage error: {code}")


def canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def parse_timestamp(text: str) -> datetime:
    try:
        return datetime.strptime(text, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise CatalogueStorageError("timestamp_malformed") from error


@contextmanager
def _open_write_connection(
    data_path: Path,
    *,
    create: bool,
) -> Iterator[sqlite3.Connection]:
    if _sqlite_version_info() < MINIMUM_SQLITE_VERSION:
        raise CatalogueStorageError("sqlite_version_unsupported")

    catalogue_path = _prepare_catalogue_path(data_path, create=create)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{catalogue_path.as_uri()}?mode=rw&nofollow=1",
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal_mode != ("wal",):
            raise CatalogueStorageError("sqlite_profile_unavailable")
        connection.execute("PRAGMA synchronous = FULL")
        _verify_profile(connection, query_only=False)
    except CatalogueStorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise CatalogueStorageError("catalogue_open_failed") from error

    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _open_read_connection(data_path: Path) -> Iterator[sqlite3.Connection]:
    with _open_read_only_connection(data_path) as connection:
        yield connection


@contextmanager
def read_connection(data_path: Path) -> Iterator[sqlite3.Connection]:
    with _open_read_connection(data_path) as connection:
        yield connection


@contextmanager
def _open_verification_connection(
    data_path: Path,
) -> Iterator[sqlite3.Connection]:
    with _open_read_only_connection(data_path, verify_identity=False) as connection:
        yield connection


@contextmanager
def _open_read_only_connection(
    data_path: Path,
    *,
    verify_identity: bool = True,
) -> Iterator[sqlite3.Connection]:
    if _sqlite_version_info() < MINIMUM_SQLITE_VERSION:
        raise CatalogueStorageError("sqlite_version_unsupported")

    catalogue_path = _prepare_catalogue_path(
        data_path,
        create=False,
        writable=False,
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{catalogue_path.as_uri()}?mode=ro&nofollow=1",
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA query_only = ON")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        if journal_mode != ("wal",):
            raise CatalogueStorageError("sqlite_profile_unavailable")
        _verify_profile(connection, query_only=True)
        if verify_identity:
            identity = (
                connection.execute("PRAGMA application_id").fetchone(),
                connection.execute("PRAGMA user_version").fetchone(),
            )
            if identity != ((APPLICATION_ID,), (CURRENT_SCHEMA_VERSION,)):
                raise CatalogueStorageError("catalogue_identity_mismatch")
    except CatalogueStorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise CatalogueStorageError("catalogue_open_failed") from error

    try:
        yield connection
    finally:
        connection.close()


def _prepare_catalogue_path(
    data_path: Path,
    *,
    create: bool,
    writable: bool = True,
) -> Path:
    os.umask(0o007)
    directory_fd: int | None = None
    catalogue_fd: int | None = None
    try:
        directory_fd = os.open(
            data_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        flags = (
            _CATALOGUE_OPEN_FLAGS
            if writable
            else (os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        )
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        catalogue_fd = os.open(
            CATALOGUE_FILENAME,
            flags,
            0o660,
            dir_fd=directory_fd,
        )
        status = os.fstat(catalogue_fd)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o660:
            raise CatalogueStorageError("catalogue_file_invalid")
    except CatalogueStorageError:
        raise
    except FileExistsError as error:
        raise CatalogueStorageError("catalogue_file_exists") from error
    except OSError as error:
        raise CatalogueStorageError("catalogue_file_unavailable") from error
    finally:
        if catalogue_fd is not None:
            os.close(catalogue_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    return data_path / CATALOGUE_FILENAME


def _sqlite_version_info() -> tuple[int, int, int]:
    return sqlite3.sqlite_version_info


def _verify_profile(
    connection: sqlite3.Connection,
    *,
    query_only: bool,
) -> None:
    expected = {
        "busy_timeout": 5000,
        "foreign_keys": 1,
        "synchronous": 2,
        "trusted_schema": 0,
    }
    if query_only:
        expected["query_only"] = 1
    for pragma, value in expected.items():
        if connection.execute(f"PRAGMA {pragma}").fetchone() != (value,):
            raise CatalogueStorageError("sqlite_profile_unavailable")
