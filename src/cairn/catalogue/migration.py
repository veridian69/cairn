import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from cairn.catalogue.sqlite import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.runtime.config import CairnConfig
from cairn.runtime.lease import DataDirectoryLease

_MIGRATION_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_MIGRATION_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_TRANSACTION,
    }
)
_FORBIDDEN_MIGRATION_FUNCTIONS = frozenset(
    {
        "current_date",
        "current_time",
        "current_timestamp",
        "date",
        "datetime",
        "julianday",
        "load_extension",
        "random",
        "randomblob",
        "strftime",
        "time",
        "timediff",
        "unixepoch",
    }
)
_PACKAGED_MIGRATIONS = Path(__file__).with_name("migrations")
_CATALOGUE_FORMAT = "cairn.catalogue/v1"


class MigrationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"migration error: {code}")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    resource: str
    sha256: bytes
    statements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Created:
    version: int


@dataclass(frozen=True, slots=True)
class Advanced:
    previous_version: int
    version: int


@dataclass(frozen=True, slots=True)
class Current:
    version: int


MigrationResult = Created | Advanced | Current


def migrate_catalogue(
    config: CairnConfig,
    clock: Callable[[], datetime],
) -> MigrationResult:
    return _migrate_catalogue(
        config,
        clock,
        _PACKAGED_MIGRATIONS,
        expected_schema_version=CURRENT_SCHEMA_VERSION,
    )


def _migrate_catalogue(
    config: CairnConfig,
    clock: Callable[[], datetime],
    migration_root: Path,
    *,
    expected_schema_version: int | None = None,
) -> MigrationResult:
    migrations = load_migration_set(migration_root)
    if (
        expected_schema_version is not None
        and migrations[-1].version != expected_schema_version
    ):
        raise MigrationError("packaged_schema_version_mismatch")

    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        with _open_migration_connection(config.paths.data) as connection:
            previous_version = _validate_catalogue_state(
                connection,
                migrations,
                str(config.instance_id),
            )
            target_version = migrations[-1].version
            if previous_version == target_version:
                return Current(version=target_version)
            applied_at = _canonical_timestamp(clock())
            _apply_pending_migrations(
                connection,
                migrations[previous_version:],
                instance_id=str(config.instance_id),
                applied_at=applied_at,
                target_version=target_version,
                fresh=previous_version == 0,
            )
    finally:
        lease.release()

    if previous_version == 0:
        return Created(version=target_version)
    return Advanced(previous_version=previous_version, version=target_version)


@contextmanager
def _open_migration_connection(data_path: Path) -> Iterator[sqlite3.Connection]:
    stack = ExitStack()
    try:
        try:
            connection = stack.enter_context(
                _open_write_connection(data_path, create=True)
            )
        except CatalogueStorageError as error:
            stack.close()
            if error.code != "catalogue_file_exists":
                raise
            stack = ExitStack()
            connection = stack.enter_context(
                _open_write_connection(data_path, create=False)
            )
        with stack:
            yield connection
    finally:
        stack.close()


def _validate_catalogue_state(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    instance_id: str,
) -> int:
    application_id = _pragma_integer(connection, "application_id")
    user_version = _pragma_integer(connection, "user_version")
    object_count = cast(
        tuple[int],
        connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone(),
    )[0]
    if application_id == 0 and user_version == 0 and object_count == 0:
        return 0
    if application_id != APPLICATION_ID:
        raise MigrationError("unrecognised_catalogue")
    if user_version > migrations[-1].version:
        raise MigrationError("catalogue_ahead")

    try:
        applied_rows = connection.execute(
            "SELECT version, name, sql_sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        metadata_rows = connection.execute(
            "SELECT format, instance_id FROM catalogue_metadata"
        ).fetchall()
    except sqlite3.Error as error:
        raise MigrationError("catalogue_history_invalid") from error

    if len(applied_rows) != user_version:
        raise MigrationError("catalogue_history_invalid")
    for expected_version, row in enumerate(applied_rows, start=1):
        if expected_version > len(migrations):
            raise MigrationError("catalogue_ahead")
        migration = migrations[expected_version - 1]
        if row != (migration.version, migration.name, migration.sha256):
            raise MigrationError("catalogue_history_mismatch")
    if metadata_rows != [(_CATALOGUE_FORMAT, instance_id)]:
        if len(metadata_rows) == 1 and metadata_rows[0][0] == _CATALOGUE_FORMAT:
            raise MigrationError("instance_mismatch")
        raise MigrationError("catalogue_metadata_invalid")
    return user_version


def _apply_pending_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    *,
    instance_id: str,
    applied_at: str,
    target_version: int,
    fresh: bool,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in migrations:
            execute_migration_statements(connection, migration.statements)
            if fresh and migration.version == 1:
                connection.execute(
                    "INSERT INTO catalogue_metadata "
                    "(singleton, format, instance_id, created_at) "
                    "VALUES (1, ?, ?, ?)",
                    (_CATALOGUE_FORMAT, instance_id, applied_at),
                )
                connection.execute(
                    "INSERT INTO audit_heads "
                    "(chain_kind, chain_identity, last_sequence, last_hash) "
                    "VALUES ('instance', ?, 0, ?)",
                    (instance_id, bytes(32)),
                )
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, sql_sha256, applied_at) VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.sha256,
                    applied_at,
                ),
            )
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {target_version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _pragma_integer(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    if not isinstance(row, tuple) or len(row) != 1 or type(row[0]) is not int:
        raise MigrationError("catalogue_header_invalid")
    return row[0]


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MigrationError("invalid_clock")
    return canonical_timestamp(value)


def execute_migration_statements(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    if not connection.in_transaction:
        raise MigrationError("migration_transaction_required")
    denied = False

    def authorize(
        action: int,
        _argument_one: str | None,
        argument_two: str | None,
        database: str | None,
        _trigger: str | None,
    ) -> int:
        nonlocal denied
        forbidden_function = (
            action == sqlite3.SQLITE_FUNCTION
            and argument_two in _FORBIDDEN_MIGRATION_FUNCTIONS
        )
        if (
            action in _FORBIDDEN_MIGRATION_ACTIONS
            or database == "temp"
            or forbidden_function
        ):
            denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        for statement in statements:
            connection.execute(statement)
    except sqlite3.DatabaseError as error:
        code = "forbidden_migration_sql" if denied else "migration_execution_failed"
        raise MigrationError(code) from error
    finally:
        connection.set_authorizer(None)


def load_migration_set(root: Path) -> tuple[Migration, ...]:
    try:
        manifest_bytes = (root / "manifest.json").read_bytes()
    except OSError as error:
        raise MigrationError("migration_manifest_unavailable") from error
    try:
        manifest = manifest_bytes.decode("utf-8", errors="strict")
        document = cast(
            object,
            json.loads(manifest, object_pairs_hook=_unique_object),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError("invalid_manifest") from error
    if not isinstance(document, list) or not document:
        raise MigrationError("invalid_manifest")

    migrations: list[Migration] = []
    names: set[str] = set()
    resources: set[str] = set()
    for expected_version, raw_entry in enumerate(document, start=1):
        if not isinstance(raw_entry, dict):
            raise MigrationError("invalid_manifest")
        entry = cast(dict[str, object], raw_entry)
        if set(entry) != {"version", "name", "resource", "sha256"}:
            raise MigrationError("invalid_manifest")
        version = entry.get("version")
        name = entry.get("name")
        resource = entry.get("resource")
        digest = entry.get("sha256")
        if (
            type(version) is not int
            or not isinstance(name, str)
            or not isinstance(resource, str)
            or not isinstance(digest, str)
        ):
            raise MigrationError("invalid_manifest")
        if version != expected_version:
            raise MigrationError("invalid_manifest")
        if (
            _MIGRATION_NAME.fullmatch(name) is None
            or resource != f"{version:04d}_{name}.sql"
            or _SHA256_HEX.fullmatch(digest) is None
            or name in names
            or resource in resources
        ):
            raise MigrationError("invalid_manifest")
        names.add(name)
        resources.add(resource)
        try:
            sql = (root / resource).read_bytes()
        except OSError as error:
            raise MigrationError("migration_resource_unavailable") from error
        try:
            statements = _split_statements(sql.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as error:
            raise MigrationError("invalid_migration_sql") from error
        migrations.append(
            Migration(
                version=version,
                name=name,
                resource=resource,
                sha256=bytes.fromhex(digest),
                statements=statements,
            )
        )
        if hashlib.sha256(sql).digest() != migrations[-1].sha256:
            raise MigrationError("migration_digest_mismatch")
    packaged_sql = {entry.name for entry in root.iterdir() if entry.suffix == ".sql"}
    listed_sql = {migration.resource for migration in migrations}
    if packaged_sql != listed_sql:
        raise MigrationError("invalid_manifest")
    return tuple(migrations)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError("invalid_manifest")
        result[key] = value
    return result


def _split_statements(sql: str) -> tuple[str, ...]:
    if "\x00" in sql:
        raise MigrationError("invalid_migration_sql")
    statements: list[str] = []
    pending = ""
    for character in sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        raise MigrationError("invalid_migration_sql")
    if not statements:
        raise MigrationError("invalid_migration_sql")
    return tuple(statements)
