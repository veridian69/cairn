import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cairn.catalogue.migration import (
    _PACKAGED_MIGRATIONS,
    Advanced,
    Created,
    Current,
    MigrationError,
    _migrate_catalogue,
    execute_migration_statements,
    load_migration_set,
    migrate_catalogue,
)
from cairn.catalogue.sqlite import APPLICATION_ID, CATALOGUE_FILENAME, read_connection
from cairn.catalogue.verification import verify_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.lease import DataDirectoryLease, LeaseError

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)


def _config(
    data_path: Path,
    instance_id: UUID = INSTANCE_ID,
) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=instance_id,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _migration_root_with_second(tmp_path: Path, sql: bytes) -> Path:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    document = json.loads((migration_root / "manifest.json").read_text("utf-8"))
    assert isinstance(document, list)
    # Derived from the packaged set rather than hardcoded: the manifest
    # rejects a duplicate or out-of-order version, so a fixed number here
    # breaks the moment a real migration claims it — as 0005 did.
    version = len(document) + 1
    resource = f"{version:04d}_test_advance.sql"
    (migration_root / resource).write_bytes(sql)
    document.append(
        {
            "version": version,
            "name": "test_advance",
            "resource": resource,
            "sha256": hashlib.sha256(sql).hexdigest(),
        }
    )
    (migration_root / "manifest.json").write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    return migration_root


def test_valid_manifest_loads_one_verified_migration(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    digest = "b4e0497804e46e0a0b0b8c31975b062152d551bac49c3c2e80932567b4085dcd"
    resource = "0001_catalogue_foundation.sql"
    (tmp_path / resource).write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "catalogue_foundation",
                    "resource": resource,
                    "sha256": digest,
                }
            ]
        ),
        encoding="utf-8",
    )

    migrations = load_migration_set(tmp_path)

    assert len(migrations) == 1
    assert migrations[0].version == 1
    assert migrations[0].name == "catalogue_foundation"
    assert migrations[0].resource == resource
    assert migrations[0].sha256.hex() == digest
    assert migrations[0].statements == ("SELECT 1;",)


def test_packaged_manifest_has_exact_foundation_digest() -> None:
    migrations = load_migration_set(_PACKAGED_MIGRATIONS)

    assert [
        (migration.version, migration.name, migration.resource, migration.sha256.hex())
        for migration in migrations
    ] == [
        (
            1,
            "catalogue_foundation",
            "0001_catalogue_foundation.sql",
            "0392b92d0f1fc4b98e5570bbcfabb455d3969e09bc1647fccefc2dcdc1df9f7f",
        ),
        (
            2,
            "authority_administration",
            "0002_authority_administration.sql",
            "cc977dda576b643af1026b8142800c966e102e818bda4437a3cf9049b8b761ad",
        ),
        (
            3,
            "custody_lifecycle",
            "0003_custody_lifecycle.sql",
            "9191c1ecb152a2d9db82d9bcea655e5669672d98efc0eb72e4c0e098b2322f63",
        ),
        (
            4,
            "projection_sequence",
            "0004_projection_sequence.sql",
            "c3391bb5f1a4746cb94f044a99b94517d6e9ab7c523c02bd6114c23adb9ad775",
        ),
        (
            5,
            "projection_rebuild_work",
            "0005_projection_rebuild_work.sql",
            "8781b879500fa112e412701ec9a72bd729ea833917f1b5c16ab9fa0c16269ad8",
        ),
        (
            6,
            "idempotency_uuid_versions",
            "0006_idempotency_uuid_versions.sql",
            "0d4efac3076afce6c43ca988a4f769c2e882d58928b42b1d32fe0407c3e521c4",
        ),
        (
            7,
            "projection_extraction_cache",
            "0007_projection_extraction_cache.sql",
            "0f7e7c1764a2870780115ec54cd64d65dfe0474da92a14aeb608c2fceccb1d63",
        ),
        (
            8,
            "shared_memory",
            "0008_shared_memory.sql",
            "420c3412f5810c0fa3f6128e2a58f1b7732cb9b9f17040205e693bf89b3e5070",
        ),
        (
            9,
            "memory_sessions",
            "0009_memory_sessions.sql",
            "9381a552f0f9c5c7e8f750aefb3de136767c594dfed565af707bef0f2c900799",
        ),
        (
            10,
            "session_custody",
            "0010_session_custody.sql",
            "4a2f8920d602b24628ad89412dd04671971fdd3a1c61041cc4ca3611bac5a04a",
        ),
        (
            11,
            "session_commit_claims",
            "0011_session_commit_claims.sql",
            "500fd2101a2ac853ab33fff040abd8ce614c6c4f375a84ec77247ed18ce35973",
        ),
        (
            12,
            "memory_proposals",
            "0012_memory_proposals.sql",
            "2893654d84e2cd16efbb1e2679a58d7466c23c4536ce3577141856f0b5aa4870",
        ),
        (
            13,
            "rebuild_indexes",
            "0013_rebuild_indexes.sql",
            "c9c31e90f3cb4c84c5e39707c24271034a5043d5e7695493305f116704fa48ea",
        ),
    ]


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        '[{"version":1,"version":2,"name":"x",'
        '"resource":"0001_x.sql","sha256":"' + "0" * 64 + '"}]',
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_unknown_entry_fields(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    (tmp_path / "0001_x.sql").write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": "0001_x.sql",
                    "sha256": hashlib.sha256(sql).hexdigest(),
                    "surprise": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_bytes(b"\xff")

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_reports_missing_manifest_without_leaking_os_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "migration_manifest_unavailable"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        ["not-an-entry"],
        [{"version": True, "name": "x", "resource": "0001_x.sql", "sha256": "0" * 64}],
        [
            {
                "version": 1,
                "name": "Bad-Name",
                "resource": "0001_Bad-Name.sql",
                "sha256": "0" * 64,
            }
        ],
    ],
)
def test_manifest_rejects_invalid_shapes_and_names(
    tmp_path: Path,
    document: object,
) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_noncontiguous_versions(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    (tmp_path / "0002_x.sql").write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 2,
                    "name": "x",
                    "resource": "0002_x.sql",
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_duplicate_migration_names(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    for resource in ("0001_same.sql", "0002_same.sql"):
        (tmp_path / resource).write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": version,
                    "name": "same",
                    "resource": f"{version:04d}_same.sql",
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
                for version in (1, 2)
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_resource_name_mismatch(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    (tmp_path / "0001_wrong.sql").write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "right",
                    "resource": "0001_wrong.sql",
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_unlisted_sql_resource(tmp_path: Path) -> None:
    sql = b"SELECT 1;\n"
    resource = "0001_catalogue_foundation.sql"
    (tmp_path / resource).write_bytes(sql)
    (tmp_path / "0002_surprise.sql").write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "catalogue_foundation",
                    "resource": resource,
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_digest_mismatch(tmp_path: Path) -> None:
    resource = "0001_x.sql"
    (tmp_path / resource).write_bytes(b"SELECT 1;\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": resource,
                    "sha256": "0" * 64,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "migration_digest_mismatch"


def test_manifest_rejects_malformed_digest(tmp_path: Path) -> None:
    resource = "0001_x.sql"
    (tmp_path / resource).write_bytes(b"SELECT 1;\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": resource,
                    "sha256": "not-a-digest",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_empty_sql(tmp_path: Path) -> None:
    resource = "0001_x.sql"
    sql = b""
    (tmp_path / resource).write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": resource,
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_migration_sql"


def test_manifest_rejects_incomplete_trailing_sql(tmp_path: Path) -> None:
    resource = "0001_x.sql"
    sql = b"SELECT 1"
    (tmp_path / resource).write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": resource,
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_migration_sql"


def test_manifest_rejects_invalid_sql_encoding(tmp_path: Path) -> None:
    resource = "0001_x.sql"
    sql = b"\xff"
    (tmp_path / resource).write_bytes(sql)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": resource,
                    "sha256": hashlib.sha256(sql).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "invalid_migration_sql"


def test_manifest_rejects_missing_sql_resource(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "version": 1,
                    "name": "x",
                    "resource": "0001_x.sql",
                    "sha256": "0" * 64,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        load_migration_set(tmp_path)

    assert caught.value.code == "migration_resource_unavailable"


@pytest.mark.parametrize(
    "statement",
    [
        "BEGIN;",
        "SAVEPOINT nested;",
        "ATTACH DATABASE ':memory:' AS other;",
        "DETACH DATABASE other;",
        "PRAGMA user_version = 1;",
        "CREATE TEMP TABLE scratch(value TEXT);",
    ],
)
def test_migration_authorizer_rejects_control_and_temporary_sql(
    statement: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(MigrationError) as caught:
        execute_migration_statements(connection, (statement,))

    assert caught.value.code == "forbidden_migration_sql"


def test_migration_execution_rejects_vacuum_inside_owned_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(MigrationError) as caught:
        execute_migration_statements(connection, ("VACUUM;",))

    assert caught.value.code == "migration_execution_failed"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT random();",
        "SELECT randomblob(16);",
        "SELECT datetime('now');",
        "SELECT CURRENT_TIMESTAMP;",
        "SELECT load_extension('surprise');",
    ],
)
def test_migration_authorizer_rejects_runtime_dependent_functions(
    statement: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(MigrationError) as caught:
        execute_migration_statements(connection, (statement,))

    assert caught.value.code == "forbidden_migration_sql"


def test_migration_authorizer_allows_main_ddl_and_deterministic_dml() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("BEGIN IMMEDIATE")

    execute_migration_statements(
        connection,
        (
            "CREATE TABLE example(value INTEGER NOT NULL);",
            "INSERT INTO example(value) VALUES (41 + 1);",
        ),
    )

    assert connection.execute("SELECT value FROM example").fetchone() == (42,)
    assert connection.execute("PRAGMA user_version").fetchone() == (0,)


def test_migration_execution_requires_python_owned_transaction() -> None:
    connection = sqlite3.connect(":memory:")

    with pytest.raises(MigrationError) as caught:
        execute_migration_statements(
            connection,
            ("CREATE TABLE residue(value INTEGER);",),
        )

    assert caught.value.code == "migration_transaction_required"
    assert (
        connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'residue'"
        ).fetchone()
        is None
    )


def test_fresh_migration_creates_exact_foundation_catalogue(tmp_path: Path) -> None:
    result = migrate_catalogue(_config(tmp_path), lambda: NOW)

    assert result == Created(version=13)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
    assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    assert connection.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall() == [
        ("assertions",),
        ("audit_events",),
        ("audit_heads",),
        ("audit_scope_index",),
        ("catalogue_metadata",),
        ("credential_revocations",),
        ("credentials",),
        ("evidence_outbox",),
        ("evidence_records",),
        ("fact_invalidations",),
        ("facts",),
        ("grant_revocations",),
        ("grants",),
        ("idempotency_records",),
        ("memory_disagreements",),
        ("memory_proposal_decisions",),
        ("memory_proposals",),
        ("memory_resolutions",),
        ("memory_session_abandonments",),
        ("memory_session_acknowledgements",),
        ("memory_session_commit_claims",),
        ("memory_session_operations",),
        ("memory_session_preparations",),
        ("memory_session_terminals",),
        ("memory_session_turns",),
        ("memory_session_visits",),
        ("memory_sessions",),
        ("principals",),
        ("projection_embedding_cache",),
        ("projection_extraction_cache",),
        ("projection_outbox",),
        ("realms",),
        ("schema_migrations",),
    ]
    assert connection.execute(
        "SELECT format, instance_id, created_at FROM catalogue_metadata"
    ).fetchone() == (
        "cairn.catalogue/v1",
        str(INSTANCE_ID),
        "2026-08-05T10:11:12.123456Z",
    )
    assert connection.execute(
        "SELECT version, name, length(sql_sha256), applied_at FROM schema_migrations"
    ).fetchall() == [
        (1, "catalogue_foundation", 32, "2026-08-05T10:11:12.123456Z"),
        (2, "authority_administration", 32, "2026-08-05T10:11:12.123456Z"),
        (3, "custody_lifecycle", 32, "2026-08-05T10:11:12.123456Z"),
        (4, "projection_sequence", 32, "2026-08-05T10:11:12.123456Z"),
        (5, "projection_rebuild_work", 32, "2026-08-05T10:11:12.123456Z"),
        (6, "idempotency_uuid_versions", 32, "2026-08-05T10:11:12.123456Z"),
        (7, "projection_extraction_cache", 32, "2026-08-05T10:11:12.123456Z"),
        (8, "shared_memory", 32, "2026-08-05T10:11:12.123456Z"),
        (9, "memory_sessions", 32, "2026-08-05T10:11:12.123456Z"),
        (10, "session_custody", 32, "2026-08-05T10:11:12.123456Z"),
        (11, "session_commit_claims", 32, "2026-08-05T10:11:12.123456Z"),
        (12, "memory_proposals", 32, "2026-08-05T10:11:12.123456Z"),
        (13, "rebuild_indexes", 32, "2026-08-05T10:11:12.123456Z"),
    ]
    assert connection.execute(
        "SELECT chain_kind, chain_identity, last_sequence, hex(last_hash) "
        "FROM audit_heads"
    ).fetchall() == [("instance", str(INSTANCE_ID), 0, "0" * 64)]
    assert connection.execute("SELECT count(*) FROM realms").fetchone() == (0,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    strict_tables = {
        row[1]: row[5]
        for row in connection.execute("PRAGMA table_list").fetchall()
        if row[1]
        in {
            "assertions",
            "audit_events",
            "audit_heads",
            "audit_scope_index",
            "catalogue_metadata",
            "credential_revocations",
            "credentials",
            "evidence_outbox",
            "evidence_records",
            "fact_invalidations",
            "facts",
            "grant_revocations",
            "grants",
            "idempotency_records",
            "principals",
            "projection_embedding_cache",
            "projection_extraction_cache",
            "projection_outbox",
            "realms",
            "schema_migrations",
        }
    }
    assert strict_tables == {
        "assertions": 1,
        "audit_events": 1,
        "audit_heads": 1,
        "audit_scope_index": 1,
        "catalogue_metadata": 1,
        "credential_revocations": 1,
        "credentials": 1,
        "evidence_outbox": 1,
        "evidence_records": 1,
        "fact_invalidations": 1,
        "facts": 1,
        "grant_revocations": 1,
        "grants": 1,
        "idempotency_records": 1,
        "principals": 1,
        "projection_embedding_cache": 1,
        "projection_extraction_cache": 1,
        "projection_outbox": 1,
        "realms": 1,
        "schema_migrations": 1,
    }


def test_migration_acquires_lease_before_opening_catalogue(tmp_path: Path) -> None:
    lease = DataDirectoryLease(tmp_path, INSTANCE_ID)
    lease.acquire()
    try:
        with pytest.raises(LeaseError) as caught:
            migrate_catalogue(_config(tmp_path), lambda: NOW)
    finally:
        lease.release()

    assert caught.value.code == "already_locked"
    assert not (tmp_path / CATALOGUE_FILENAME).exists()


def test_current_migration_is_a_verified_no_op(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)

    def clock_must_not_be_read() -> datetime:
        raise AssertionError("current migration read the clock")

    result = migrate_catalogue(_config(tmp_path), clock_must_not_be_read)

    assert result == Current(version=13)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone() == (
        13,
    )


def test_migration_rejects_edited_packaged_sql_before_open(tmp_path: Path) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    resource = migration_root / "0001_catalogue_foundation.sql"
    resource.write_bytes(resource.read_bytes() + b"\nSELECT 1;\n")
    data_path = tmp_path / "data"
    data_path.mkdir()

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(
            _config(data_path),
            lambda: NOW,
            migration_root,
        )

    assert caught.value.code == "migration_digest_mismatch"
    assert not (data_path / CATALOGUE_FILENAME).exists()


def test_current_catalogue_rejects_rewritten_packaged_history(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    migrate_catalogue(_config(data_path), lambda: NOW)
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    resource_name = "0001_catalogue_foundation.sql"
    resource = migration_root / resource_name
    sql = resource.read_bytes() + b"\nSELECT 1;\n"
    resource.write_bytes(sql)
    document = json.loads((migration_root / "manifest.json").read_text("utf-8"))
    assert isinstance(document, list)
    document[0]["sha256"] = hashlib.sha256(sql).hexdigest()
    (migration_root / "manifest.json").write_text(
        json.dumps(document),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(
            _config(data_path),
            lambda: NOW,
            migration_root,
        )

    assert caught.value.code == "catalogue_history_mismatch"


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("name = ?", "renamed_foundation"),
        ("sql_sha256 = ?", bytes(32)),
    ],
)
def test_migration_rejects_edited_applied_history(
    tmp_path: Path,
    assignment: str,
    value: str | bytes,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    connection.execute("DROP TRIGGER trg_schema_migrations_no_update")
    connection.execute(
        f"UPDATE schema_migrations SET {assignment} WHERE version = 1", (value,)
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError) as caught:
        migrate_catalogue(_config(tmp_path), lambda: NOW)

    assert caught.value.code == "catalogue_history_mismatch"


def test_migration_rejects_missing_applied_version(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    connection.execute("DROP TRIGGER trg_schema_migrations_no_delete")
    connection.execute("DELETE FROM schema_migrations")
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError) as caught:
        migrate_catalogue(_config(tmp_path), lambda: NOW)

    assert caught.value.code == "catalogue_history_invalid"


def test_migration_rejects_catalogue_ahead_of_package(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    connection.execute("PRAGMA user_version = 14")
    connection.close()

    with pytest.raises(MigrationError) as caught:
        migrate_catalogue(_config(tmp_path), lambda: NOW)

    assert caught.value.code == "catalogue_ahead"


def test_migration_rejects_instance_mismatch(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)

    with pytest.raises(MigrationError) as caught:
        migrate_catalogue(
            _config(tmp_path, UUID("22222222-2222-4222-8222-222222222222")),
            lambda: NOW,
        )

    assert caught.value.code == "instance_mismatch"


def test_migration_rejects_unrecognised_sqlite_file(tmp_path: Path) -> None:
    catalogue_path = tmp_path / CATALOGUE_FILENAME
    connection = sqlite3.connect(catalogue_path)
    connection.execute("CREATE TABLE foreign_table(value TEXT)")
    connection.close()
    catalogue_path.chmod(0o660)

    with pytest.raises(MigrationError) as caught:
        migrate_catalogue(_config(tmp_path), lambda: NOW)

    assert caught.value.code == "unrecognised_catalogue"


def test_migration_advances_existing_catalogue_to_injected_second_version(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    migrate_catalogue(_config(data_path), lambda: NOW)
    migration_root = _migration_root_with_second(
        tmp_path,
        b"CREATE TABLE migration_two(value TEXT) STRICT;\n",
    )

    result = _migrate_catalogue(
        _config(data_path),
        lambda: NOW,
        migration_root,
    )

    assert result == Advanced(previous_version=13, version=14)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA user_version").fetchone() == (14,)
    assert connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (1, "catalogue_foundation"),
        (2, "authority_administration"),
        (3, "custody_lifecycle"),
        (4, "projection_sequence"),
        (5, "projection_rebuild_work"),
        (6, "idempotency_uuid_versions"),
        (7, "projection_extraction_cache"),
        (8, "shared_memory"),
        (9, "memory_sessions"),
        (10, "session_custody"),
        (11, "session_commit_claims"),
        (12, "memory_proposals"),
        (13, "rebuild_indexes"),
        (14, "test_advance"),
    ]


def test_failed_pending_set_rolls_back_all_schema_and_history(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    migration_root = _migration_root_with_second(
        tmp_path,
        (
            b"CREATE TABLE migration_residue(value TEXT) STRICT;\n"
            b"INSERT INTO migration_residue(value) VALUES ('made');\n"
            b"INSERT INTO missing_table(value) VALUES ('fail');\n"
        ),
    )

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(
            _config(data_path),
            lambda: NOW,
            migration_root,
        )

    assert caught.value.code == "migration_execution_failed"
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA application_id").fetchone() == (0,)
    assert connection.execute("PRAGMA user_version").fetchone() == (0,)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        == []
    )

    assert migrate_catalogue(_config(data_path), lambda: NOW) == Created(version=13)


_FOUNDATION_SQL = (_PACKAGED_MIGRATIONS / "0001_catalogue_foundation.sql").read_bytes()


@settings(max_examples=25, deadline=None)
@given(
    instance_id=st.uuids(version=4),
    moment=st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2099, 12, 31),
        timezones=st.just(UTC),
    ),
)
def test_migration_reaches_the_same_current_state_for_any_instance_and_clock(
    instance_id: UUID,
    moment: datetime,
) -> None:
    with TemporaryDirectory() as raw_path:
        data_path = Path(raw_path)
        config = _config(data_path, instance_id)

        created = migrate_catalogue(config, lambda: moment)
        repeated = migrate_catalogue(config, lambda: moment)

        assert created == Created(version=13)
        assert repeated == Current(version=13)
        connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
        try:
            assert connection.execute("PRAGMA user_version").fetchone() == (13,)
            assert connection.execute("PRAGMA application_id").fetchone() == (
                APPLICATION_ID,
            )
            assert connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone() == (13,)
            assert connection.execute(
                "SELECT instance_id FROM catalogue_metadata"
            ).fetchone() == (str(instance_id),)
            assert connection.execute(
                "SELECT chain_kind, chain_identity, last_sequence FROM audit_heads"
            ).fetchall() == [("instance", str(instance_id), 0)]
        finally:
            connection.close()


@settings(max_examples=50, deadline=None)
@given(
    index=st.integers(min_value=0, max_value=len(_FOUNDATION_SQL) - 1),
    replacement=st.integers(min_value=0, max_value=255),
)
def test_any_single_byte_change_to_packaged_migration_sql_is_rejected(
    index: int,
    replacement: int,
) -> None:
    assume(replacement != _FOUNDATION_SQL[index])
    corrupted = bytearray(_FOUNDATION_SQL)
    corrupted[index] = replacement

    with TemporaryDirectory() as raw_path:
        migration_root = Path(raw_path) / "migrations"
        shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
        (migration_root / "0001_catalogue_foundation.sql").write_bytes(bytes(corrupted))

        with pytest.raises(MigrationError) as caught:
            load_migration_set(migration_root)

    assert caught.value.code in {
        "migration_digest_mismatch",
        "invalid_migration_sql",
    }


_FIXTURE_DB = Path(__file__).parent / "fixtures" / "v0001" / "catalogue.sqlite3"
_FIXTURE_INSTANCE_ID = UUID("9d3f5a72-4c81-4f2e-9b6a-2e7c8d1f0a35")

_TS = "2026-08-05T10:11:12.123456Z"
_REALM_ID = "acme"
_HUMAN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_WORKLOAD_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_GRANT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_CREDENTIAL_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def test_v0001_fixture_upgrades_exactly_once_to_authority_schema(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    fixture_copy = data_path / CATALOGUE_FILENAME
    shutil.copy(_FIXTURE_DB, fixture_copy)
    fixture_copy.chmod(0o660)
    config = _config(data_path, _FIXTURE_INSTANCE_ID)

    advanced = migrate_catalogue(config, lambda: NOW)

    assert advanced == Advanced(previous_version=1, version=13)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    assert connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (1, "catalogue_foundation"),
        (2, "authority_administration"),
        (3, "custody_lifecycle"),
        (4, "projection_sequence"),
        (5, "projection_rebuild_work"),
        (6, "idempotency_uuid_versions"),
        (7, "projection_extraction_cache"),
        (8, "shared_memory"),
        (9, "memory_sessions"),
        (10, "session_custody"),
        (11, "session_commit_claims"),
        (12, "memory_proposals"),
        (13, "rebuild_indexes"),
    ]
    assert connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' "
        "AND name IN ('principals', 'credentials', 'grants', "
        "'credential_revocations', 'grant_revocations') ORDER BY name"
    ).fetchall() == [
        ("credential_revocations",),
        ("credentials",),
        ("grant_revocations",),
        ("grants",),
        ("principals",),
    ]
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    current = migrate_catalogue(config, lambda: NOW)

    assert current == Current(version=13)


def test_migration_rejects_edited_authority_schema_sql_before_open(
    tmp_path: Path,
) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    resource = migration_root / "0002_authority_administration.sql"
    resource.write_bytes(resource.read_bytes() + b"\nSELECT 1;\n")
    data_path = tmp_path / "data"
    data_path.mkdir()

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(_config(data_path), lambda: NOW, migration_root)

    assert caught.value.code == "migration_digest_mismatch"
    assert not (data_path / CATALOGUE_FILENAME).exists()


def test_migration_rejects_edited_custody_schema_sql_before_open(
    tmp_path: Path,
) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    resource = migration_root / "0003_custody_lifecycle.sql"
    resource.write_bytes(resource.read_bytes() + b"\nSELECT 1;\n")
    data_path = tmp_path / "data"
    data_path.mkdir()

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(_config(data_path), lambda: NOW, migration_root)

    assert caught.value.code == "migration_digest_mismatch"
    assert not (data_path / CATALOGUE_FILENAME).exists()


_V0002_FIXTURE_DB = Path(__file__).parent / "fixtures" / "v0002" / "catalogue.sqlite3"
_V0002_FIXTURE_INSTANCE_ID = UUID("6e0f4a9c-3d21-4b87-9c5a-1f2e3d4c5b6a")
_V0002_REALM_ID = "local"


def test_v0002_fixture_upgrades_exactly_once_to_custody_schema(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    fixture_copy = data_path / CATALOGUE_FILENAME
    shutil.copy(_V0002_FIXTURE_DB, fixture_copy)
    fixture_copy.chmod(0o660)
    config = _config(data_path, _V0002_FIXTURE_INSTANCE_ID)

    advanced = migrate_catalogue(config, lambda: NOW)

    assert advanced == Advanced(previous_version=2, version=13)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    assert connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (1, "catalogue_foundation"),
        (2, "authority_administration"),
        (3, "custody_lifecycle"),
        (4, "projection_sequence"),
        (5, "projection_rebuild_work"),
        (6, "idempotency_uuid_versions"),
        (7, "projection_extraction_cache"),
        (8, "shared_memory"),
        (9, "memory_sessions"),
        (10, "session_custody"),
        (11, "session_commit_claims"),
        (12, "memory_proposals"),
        (13, "rebuild_indexes"),
    ]
    assert connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' "
        "AND name IN ('assertions', 'facts', 'fact_invalidations', "
        "'evidence_records', 'evidence_outbox', 'projection_outbox') "
        "ORDER BY name"
    ).fetchall() == [
        ("assertions",),
        ("evidence_outbox",),
        ("evidence_records",),
        ("fact_invalidations",),
        ("facts",),
        ("projection_outbox",),
    ]
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT realm_id FROM realms").fetchall() == [
        (_V0002_REALM_ID,)
    ]
    assert connection.execute("SELECT label FROM principals").fetchall() == [
        ("operator",)
    ]
    assert connection.execute("SELECT count(*) FROM grants").fetchone() == (3,)
    assert connection.execute(
        "SELECT sequence FROM audit_events WHERE chain_kind = 'realm' "
        "AND chain_identity = ? ORDER BY sequence",
        (_V0002_REALM_ID,),
    ).fetchall() == [(1,), (2,)]
    connection.close()

    current = migrate_catalogue(config, lambda: NOW)

    assert current == Current(version=13)
    verify_catalogue(config)


_V0003_FIXTURE_DB = Path(__file__).parent / "fixtures" / "v0003" / "catalogue.sqlite3"
_V0003_FIXTURE_INSTANCE_ID = UUID("5c1f8a2b-7d43-4e96-8b1a-2f3c4d5e6a7b")
_V0003_REALM_ID = "local"


def test_v0003_fixture_upgrades_exactly_once_to_projection_sequence_schema(
    tmp_path: Path,
) -> None:
    """I-78's ordering column arrives on a table slice 4 already ships
    with rows in it, so the upgrade is what has to be proved: every row
    survives, each gains a distinct sequence in (created_at, work_id)
    order, and the identity freeze now covers the new column."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    fixture_copy = data_path / CATALOGUE_FILENAME
    shutil.copy(_V0003_FIXTURE_DB, fixture_copy)
    fixture_copy.chmod(0o660)
    config = _config(data_path, _V0003_FIXTURE_INSTANCE_ID)

    before = sqlite3.connect(fixture_copy)
    expected_order = before.execute(
        "SELECT work_id FROM projection_outbox ORDER BY created_at, work_id"
    ).fetchall()
    before.close()

    advanced = migrate_catalogue(config, lambda: NOW)

    assert advanced == Advanced(previous_version=3, version=13)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    assert connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (1, "catalogue_foundation"),
        (2, "authority_administration"),
        (3, "custody_lifecycle"),
        (4, "projection_sequence"),
        (5, "projection_rebuild_work"),
        (6, "idempotency_uuid_versions"),
        (7, "projection_extraction_cache"),
        (8, "shared_memory"),
        (9, "memory_sessions"),
        (10, "session_custody"),
        (11, "session_commit_claims"),
        (12, "memory_proposals"),
        (13, "rebuild_indexes"),
    ]

    rows = connection.execute(
        "SELECT sequence, work_id FROM projection_outbox ORDER BY sequence"
    ).fetchall()
    assert [(work_id,) for _, work_id in rows] == expected_order
    sequences = [sequence for sequence, _ in rows]
    assert sequences == sorted(set(sequences))
    assert all(type(sequence) is int for sequence in sequences)

    # The scratch table the migration copies through is gone, and the
    # sibling outbox is untouched.
    assert connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' "
        "AND name LIKE 'projection_outbox%' ORDER BY name"
    ).fetchall() == [("projection_outbox",)]
    assert connection.execute("SELECT count(*) FROM evidence_outbox").fetchone() == (0,)

    # AUTOINCREMENT: a delete must not let the next insert reuse the value.
    # The replacement row borrows the deleted row's fact and mutation
    # identities, so the catalogue stays internally consistent and the
    # verify_catalogue call at the end of this test still means something.
    highest = sequences[-1]
    fact_id, mutation_id = cast(
        tuple[str, str],
        connection.execute(
            "SELECT fact_id, mutation_id FROM projection_outbox WHERE sequence = ?",
            (highest,),
        ).fetchone(),
    )
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM projection_outbox WHERE sequence = ?", (highest,))
    connection.execute(
        "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
        "created_at, attempts) VALUES (?, 'fact-ingested', ?, ?, ?, 0)",
        (
            "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            fact_id,
            mutation_id,
            "2026-08-08T13:00:00.000000Z",
        ),
    )
    connection.commit()
    assert connection.execute(
        "SELECT sequence FROM projection_outbox WHERE work_id = ?",
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",),
    ).fetchone() == (highest + 1,)

    # The identity freeze covers the new column, and still covers the old.
    for column, value in (
        ("sequence", 9999),
        ("work_id", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        ("created_at", "2026-08-08T14:00:00.000000Z"),
    ):
        with pytest.raises(sqlite3.IntegrityError) as caught:
            connection.execute(
                f"UPDATE projection_outbox SET {column} = ? WHERE work_id = ?",
                (value, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            )
        assert "projection_outbox_identity_frozen" in str(caught.value)
        # The refused statement still opened sqlite3's implicit
        # transaction; leaving it open would break the next BEGIN.
        connection.rollback()

    # Retry state remains updatable: the freeze is on identity only.
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE projection_outbox SET attempts = 1, last_attempt_at = ?, "
        "last_failure_code = 'index_unavailable' WHERE work_id = ?",
        ("2026-08-08T13:00:01.000000Z", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
    )
    connection.commit()

    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT realm_id FROM realms").fetchall() == [
        (_V0003_REALM_ID,)
    ]
    assert connection.execute("SELECT count(*) FROM facts").fetchone() == (4,)
    connection.close()

    current = migrate_catalogue(config, lambda: NOW)

    assert current == Current(version=13)
    verify_catalogue(config)


_V0004_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v0004"
_V0004_FIXTURE_DB = _V0004_FIXTURE_DIR / "catalogue.sqlite3"
_V0004_FIXTURE_INSTANCE_ID = UUID("9a7b6c5d-4e3f-4a2b-8c1d-0e9f8a7b6c5d")
# Six rows were allocated and all six drained by the real deliverer, so
# the mark survives in sqlite_sequence with no row left to imply it.
_V0004_HIGH_WATER = 6


def _copied_v0004(tmp_path: Path) -> tuple[Path, CairnConfig]:
    data_path = tmp_path / "data"
    data_path.mkdir()
    fixture_copy = data_path / CATALOGUE_FILENAME
    shutil.copy(_V0004_FIXTURE_DB, fixture_copy)
    fixture_copy.chmod(0o660)
    return fixture_copy, _config(data_path, _V0004_FIXTURE_INSTANCE_ID)


def _allocated_sequence(catalogue: Path) -> int:
    """Insert one row through the real constraints and report the sequence
    AUTOINCREMENT gave it. Fact and mutation identities are borrowed from
    the catalogue so nothing invented enters it."""
    connection = sqlite3.connect(catalogue)
    try:
        fact_id = cast(
            tuple[str], connection.execute("SELECT fact_id FROM facts").fetchone()
        )[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
            "created_at, attempts) VALUES (?, 'fact-rebuild', ?, NULL, ?, 0)",
            (
                "abababab-abab-4bab-8bab-abababababab",
                fact_id,
                "2026-08-09T10:00:00.000000Z",
            ),
        )
        connection.commit()
        return cast(
            tuple[int],
            connection.execute(
                "SELECT sequence FROM projection_outbox WHERE work_id = ?",
                ("abababab-abab-4bab-8bab-abababababab",),
            ).fetchone(),
        )[0]
    finally:
        connection.close()


def test_v0004_fixture_upgrades_exactly_once_to_rebuild_work_schema(
    tmp_path: Path,
) -> None:
    """0005 recreates ``projection_outbox``, and ``DROP TABLE`` destroys the
    ``sqlite_sequence`` row that holds AUTOINCREMENT's promise.

    This fixture is the state that makes the loss visible: six rows were
    allocated and all six delivered, so the catalogue arrives with an empty
    queue and a high-water mark of six that no surviving row implies. A
    migration that rebased allocation on the copied rows would restart at
    one and hand out every sequence again — the exact reuse 0004
    introduced AUTOINCREMENT to prevent, and undetectable from the rows
    themselves. The assertion is therefore against the *old mark*, not
    against ``MAX(sequence)``.
    """
    fixture_copy, config = _copied_v0004(tmp_path)
    before = sqlite3.connect(fixture_copy)
    assert before.execute("PRAGMA user_version").fetchone() == (4,)
    assert before.execute("SELECT count(*) FROM projection_outbox").fetchone() == (0,)
    assert before.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'projection_outbox'"
    ).fetchone() == (_V0004_HIGH_WATER,)
    before.close()

    advanced = migrate_catalogue(config, lambda: NOW)

    assert advanced == Advanced(previous_version=4, version=13)
    connection = sqlite3.connect(fixture_copy)
    assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    assert connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (1, "catalogue_foundation"),
        (2, "authority_administration"),
        (3, "custody_lifecycle"),
        (4, "projection_sequence"),
        (5, "projection_rebuild_work"),
        (6, "idempotency_uuid_versions"),
        (7, "projection_extraction_cache"),
        (8, "shared_memory"),
        (9, "memory_sessions"),
        (10, "session_custody"),
        (11, "session_commit_claims"),
        (12, "memory_proposals"),
        (13, "rebuild_indexes"),
    ]
    # The mark itself survived the drop.
    assert connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'projection_outbox'"
    ).fetchone() == (_V0004_HIGH_WATER,)
    # No scratch table survives, including the one carrying the mark.
    assert connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' "
        "AND name LIKE 'projection_outbox%' ORDER BY name"
    ).fetchall() == [("projection_outbox",)]
    connection.close()

    assert _allocated_sequence(fixture_copy) > _V0004_HIGH_WATER
    verify_catalogue(config)


def test_v0004_fixture_preserves_idempotency_rows_and_accepts_uuid5_keys(
    tmp_path: Path,
) -> None:
    fixture_copy, config = _copied_v0004(tmp_path)
    before = sqlite3.connect(fixture_copy)
    existing = before.execute(
        "SELECT * FROM idempotency_records "
        "ORDER BY principal_id, operation, idempotency_key"
    ).fetchall()
    assert existing
    before.close()

    advanced = migrate_catalogue(config, lambda: NOW)

    assert advanced == Advanced(previous_version=4, version=13)
    connection = sqlite3.connect(fixture_copy)
    assert (
        connection.execute(
            "SELECT * FROM idempotency_records "
            "ORDER BY principal_id, operation, idempotency_key"
        ).fetchall()
        == existing
    )
    source = existing[0]
    connection.execute(
        "INSERT INTO idempotency_records ("
        "principal_id, operation, idempotency_key, command_digest, "
        "result_schema, result_bytes, result_digest, mutation_id, "
        "original_event_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source[0],
            source[1],
            "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
            source[3],
            source[4],
            source[5],
            source[6],
            "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
            source[8],
            source[9],
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO idempotency_records ("
            "principal_id, operation, idempotency_key, command_digest, "
            "result_schema, result_bytes, result_digest, mutation_id, "
            "original_event_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source[0],
                source[1],
                "aaaaaaaa-aaaa-5aaa-7aaa-aaaaaaaaaaaa",
                source[3],
                source[4],
                source[5],
                source[6],
                "dededede-dede-4ede-8ede-dededededede",
                source[8],
                source[9],
            ),
        )
    connection.close()


def test_the_upgrade_keeps_the_mark_above_rows_that_outlived_higher_ones(
    tmp_path: Path,
) -> None:
    """The same loss with rows still present, which is what makes it
    subtle: a surviving low row would let the recreated table look
    plausible while allocating over identities already issued to rows since
    delivered.

    The row is put back deliberately — the deliverer drains oldest first,
    so a real catalogue reaches this shape by other routes (a rebuild's own
    per-fact discharge, out-of-order confirmation) rather than by ordinary
    draining, and the fixture cannot hold both this and the empty case.
    """
    fixture_copy, config = _copied_v0004(tmp_path)
    connection = sqlite3.connect(fixture_copy)
    fact_id = cast(
        tuple[str], connection.execute("SELECT fact_id FROM facts LIMIT 1").fetchone()
    )[0]
    # The mutation identity is not drawn from the audit chain, and this
    # test does not call ``verify_catalogue`` as a result: what is under
    # test is sequence allocation across the recreation, for which the
    # schema's own constraints on the row are the relevant ones.
    mutation_id = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO projection_outbox (sequence, work_id, kind, fact_id, "
        "mutation_id, created_at, attempts) "
        "VALUES (2, ?, 'fact-ingested', ?, ?, ?, 0)",
        (
            "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
            fact_id,
            mutation_id,
            "2026-08-09T09:00:00.000000Z",
        ),
    )
    connection.commit()
    connection.close()

    migrate_catalogue(config, lambda: NOW)

    connection = sqlite3.connect(fixture_copy)
    # The row survives with its sequence, and the mark stays above it
    # rather than collapsing onto it.
    assert connection.execute("SELECT sequence FROM projection_outbox").fetchall() == [
        (2,)
    ]
    assert connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'projection_outbox'"
    ).fetchone() == (_V0004_HIGH_WATER,)
    connection.close()

    assert _allocated_sequence(fixture_copy) > _V0004_HIGH_WATER


def test_migration_rejects_edited_projection_sequence_sql_before_open(
    tmp_path: Path,
) -> None:
    migration_root = tmp_path / "migrations"
    shutil.copytree(_PACKAGED_MIGRATIONS, migration_root)
    resource = migration_root / "0004_projection_sequence.sql"
    resource.write_bytes(resource.read_bytes() + b"\nSELECT 1;\n")
    data_path = tmp_path / "data"
    data_path.mkdir()

    with pytest.raises(MigrationError) as caught:
        _migrate_catalogue(_config(data_path), lambda: NOW, migration_root)

    assert caught.value.code == "migration_digest_mismatch"
    assert not (data_path / CATALOGUE_FILENAME).exists()


def test_a_fresh_catalogue_and_an_upgraded_one_converge(tmp_path: Path) -> None:
    """0004 recreates a table rather than altering it, so the fresh-create
    and upgrade paths could diverge in ways `user_version` would not
    catch. Compare the full schema of both."""
    upgraded_path = tmp_path / "upgraded"
    upgraded_path.mkdir()
    shutil.copy(_V0003_FIXTURE_DB, upgraded_path / CATALOGUE_FILENAME)
    (upgraded_path / CATALOGUE_FILENAME).chmod(0o660)
    migrate_catalogue(_config(upgraded_path, _V0003_FIXTURE_INSTANCE_ID), lambda: NOW)

    fresh_path = tmp_path / "fresh"
    fresh_path.mkdir()
    migrate_catalogue(_config(fresh_path), lambda: NOW)

    def schema(path: Path) -> list[tuple[str, str, str, str]]:
        connection = sqlite3.connect(path / CATALOGUE_FILENAME)
        try:
            return cast(
                list[tuple[str, str, str, str]],
                connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall(),
            )
        finally:
            connection.close()

    assert schema(upgraded_path) == schema(fresh_path)


def _authority_schema_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    for migration in load_migration_set(_PACKAGED_MIGRATIONS):
        execute_migration_statements(connection, migration.statements)
    connection.commit()
    return connection


def _seed_realm_and_principals(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (_REALM_ID, _TS)
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_HUMAN_ID, "human", "operator", _TS),
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_WORKLOAD_ID, "workload", "worker", _TS),
    )
    connection.commit()


def _insert_grant(
    connection: sqlite3.Connection,
    *,
    grant_id: str = _GRANT_ID,
    principal_id: str = _HUMAN_ID,
    realm_id: str = _REALM_ID,
    scope_segments: str = "[]",
    operations: str = '["retrieve"]',
    read_clearance: str = "internal",
    write_classifications: str = "[]",
    delegable_operations: str | None = None,
    issued_by: str | None = None,
    expires_at: str | None = None,
    created_at: str = _TS,
) -> None:
    connection.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, delegable_operations, "
        "issued_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            principal_id,
            realm_id,
            scope_segments,
            operations,
            read_clearance,
            write_classifications,
            delegable_operations,
            issued_by,
            expires_at,
            created_at,
        ),
    )


def _seeded_authority_connection() -> sqlite3.Connection:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO credentials (credential_id, principal_id, verifier, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_CREDENTIAL_ID, _HUMAN_ID, bytes(32), _TS),
    )
    _insert_grant(connection, expires_at=_TS)
    connection.execute(
        "INSERT INTO credential_revocations "
        "(credential_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
        (_CREDENTIAL_ID, _TS, _HUMAN_ID, "superseded"),
    )
    connection.execute(
        "INSERT INTO grant_revocations "
        "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
        (_GRANT_ID, _TS, _HUMAN_ID, "superseded"),
    )
    connection.commit()
    return connection


_GUARD_TABLES = [
    (
        "principals",
        "principal_id",
        _HUMAN_ID,
        "label",
        "renamed",
        "immutable_principal",
    ),
    (
        "credentials",
        "credential_id",
        _CREDENTIAL_ID,
        "created_at",
        _TS,
        "immutable_credential",
    ),
    (
        "grants",
        "grant_id",
        _GRANT_ID,
        "read_clearance",
        "public",
        "immutable_grant",
    ),
    (
        "credential_revocations",
        "credential_id",
        _CREDENTIAL_ID,
        "reason_code",
        "changed",
        "immutable_credential_revocation",
    ),
    (
        "grant_revocations",
        "grant_id",
        _GRANT_ID,
        "reason_code",
        "changed",
        "immutable_grant_revocation",
    ),
]


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_value", "update_column", "update_value", "message"),
    _GUARD_TABLES,
)
def test_authority_tables_reject_update(
    table: str,
    pk_column: str,
    pk_value: str,
    update_column: str,
    update_value: str,
    message: str,
) -> None:
    connection = _seeded_authority_connection()

    with pytest.raises(sqlite3.IntegrityError, match=message):
        connection.execute(
            f"UPDATE {table} SET {update_column} = ? WHERE {pk_column} = ?",
            (update_value, pk_value),
        )


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_value", "update_column", "update_value", "message"),
    _GUARD_TABLES,
)
def test_authority_tables_reject_delete(
    table: str,
    pk_column: str,
    pk_value: str,
    update_column: str,
    update_value: str,
    message: str,
) -> None:
    connection = _seeded_authority_connection()

    with pytest.raises(sqlite3.IntegrityError, match=message):
        connection.execute(f"DELETE FROM {table} WHERE {pk_column} = ?", (pk_value,))


def test_workload_expiry_trigger_rejects_null_expiry_for_workload_grant() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(connection, principal_id=_WORKLOAD_ID, expires_at=None)


def test_workload_expiry_trigger_accepts_null_expiry_for_human_grant() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    _insert_grant(connection, principal_id=_HUMAN_ID, expires_at=None)
    connection.commit()

    assert connection.execute("SELECT count(*) FROM grants").fetchone() == (1,)


def test_workload_expiry_trigger_accepts_workload_grant_with_expiry() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    _insert_grant(connection, principal_id=_WORKLOAD_ID, expires_at=_TS)
    connection.commit()

    assert connection.execute("SELECT count(*) FROM grants").fetchone() == (1,)


def test_principal_uuid_shape_check_rejects_malformed_id() -> None:
    connection = _authority_schema_connection()
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("not-a-uuid", "human", "operator", _TS),
        )


def test_principal_created_at_shape_check_rejects_malformed_timestamp() -> None:
    connection = _authority_schema_connection()
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (_HUMAN_ID, "human", "operator", "not-a-timestamp"),
        )


def test_principal_label_syntax_check_rejects_uppercase_label() -> None:
    connection = _authority_schema_connection()
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (_HUMAN_ID, "human", "Operator", _TS),
        )


def test_credential_verifier_length_check_rejects_short_verifier() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at) "
            "VALUES (?, ?, ?, ?)",
            (_CREDENTIAL_ID, _HUMAN_ID, b"short", _TS),
        )


def test_grant_read_clearance_enum_check_rejects_unknown_value() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(connection, read_clearance="top-secret", expires_at=_TS)


def test_grant_delegable_operations_required_when_grant_manage_present() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(
            connection,
            operations='["grant-manage"]',
            delegable_operations=None,
            expires_at=_TS,
        )


def test_grant_delegable_operations_forbidden_without_grant_manage() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(
            connection,
            operations='["retrieve"]',
            delegable_operations="[]",
            expires_at=_TS,
        )


def test_grant_delegable_operations_never_contains_grant_manage() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(
            connection,
            operations='["grant-manage"]',
            delegable_operations='["grant-manage"]',
            expires_at=_TS,
        )


def test_grant_delegable_operations_accepts_consistent_grant_manage() -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")

    _insert_grant(
        connection,
        operations='["grant-manage"]',
        delegable_operations="[]",
        expires_at=_TS,
    )
    connection.commit()

    assert connection.execute("SELECT count(*) FROM grants").fetchone() == (1,)


# --- Custody lifecycle (0003) ---------------------------------------------

_ASSERTION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
_FACT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
_FACT_PROMOTED_ID = "11111111-1111-4111-9111-111111111111"
_EVIDENCE_ID = "22222222-2222-4222-9222-222222222222"
_EVIDENCE_EXTERNAL_ID = "33333333-3333-4333-a333-333333333333"
_EVIDENCE_WORK_ID = "44444444-4444-4444-b444-444444444444"
_PROJECTION_WORK_ID = "55555555-5555-4555-8555-555555555555"
_OUTBOX_MUTATION_ID = "66666666-6666-4666-9666-666666666666"
_PROJECTION_MUTATION_ID = "77777777-7777-4777-a777-777777777777"


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: dict[str, object],
) -> None:
    columns = list(row)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def _insert_assertion(connection: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "assertion_id": _ASSERTION_ID,
        "realm_id": _REALM_ID,
        "scope_segments": "[]",
        "classification": "internal",
        "source_type": "human",
        "principal_id": _HUMAN_ID,
        "observed_at": None,
        "metadata": None,
        "recorded_at": _TS,
    }
    row.update(overrides)
    _insert_row(connection, "assertions", row)


def _insert_fact(connection: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "fact_id": _FACT_ID,
        "realm_id": _REALM_ID,
        "scope_segments": "[]",
        "body": "hello",
        "trust": "candidate",
        "classification": "internal",
        "assertion_id": _ASSERTION_ID,
        "derived_from": None,
        "promoted_by": None,
        "evidence_id": None,
        "valid_from": None,
        "valid_to": None,
        "recorded_at": _TS,
    }
    row.update(overrides)
    _insert_row(connection, "facts", row)


def _insert_fact_invalidation(
    connection: sqlite3.Connection, **overrides: object
) -> None:
    row: dict[str, object] = {
        "fact_id": _FACT_ID,
        "invalidated_at": _TS,
        "principal_id": _HUMAN_ID,
        "superseded_by": None,
        "reason": "obsolete",
    }
    row.update(overrides)
    _insert_row(connection, "fact_invalidations", row)


def _insert_evidence_record(
    connection: sqlite3.Connection, **overrides: object
) -> None:
    row: dict[str, object] = {
        "evidence_id": _EVIDENCE_ID,
        "realm_id": _REALM_ID,
        "scope_segments": "[]",
        "classification": "internal",
        "payload_digest": bytes(32),
        "assertion_id": _ASSERTION_ID,
        "payload_length": 10,
        "external_uri": None,
        "recorded_at": _TS,
    }
    row.update(overrides)
    _insert_row(connection, "evidence_records", row)


def _insert_evidence_outbox(
    connection: sqlite3.Connection, **overrides: object
) -> None:
    row: dict[str, object] = {
        "work_id": _EVIDENCE_WORK_ID,
        "kind": "store-payload",
        "evidence_id": _EVIDENCE_ID,
        "mutation_id": _OUTBOX_MUTATION_ID,
        "payload": b"x" * 10,
        "created_at": _TS,
        "attempts": 0,
        "last_attempt_at": None,
        "last_failure_code": None,
    }
    row.update(overrides)
    _insert_row(connection, "evidence_outbox", row)


def _insert_projection_outbox(
    connection: sqlite3.Connection, **overrides: object
) -> None:
    row: dict[str, object] = {
        "work_id": _PROJECTION_WORK_ID,
        "kind": "fact-ingested",
        "fact_id": _FACT_ID,
        "mutation_id": _PROJECTION_MUTATION_ID,
        "created_at": _TS,
        "attempts": 0,
        "last_attempt_at": None,
        "last_failure_code": None,
    }
    row.update(overrides)
    _insert_row(connection, "projection_outbox", row)


def _seeded_custody_connection() -> sqlite3.Connection:
    """A realm, a principal, one assertion, one exact-custody evidence
    record, one ingested fact and its invalidation — all mutually
    consistent, giving every custody CHECK test a valid base to violate."""
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")
    _insert_assertion(connection)
    _insert_evidence_record(connection)
    _insert_fact(connection)
    _insert_fact_invalidation(connection)
    connection.commit()
    return connection


@pytest.fixture
def custody_connection() -> sqlite3.Connection:
    return _seeded_custody_connection()


_CUSTODY_GUARD_TABLES = [
    (
        "assertions",
        "assertion_id",
        _ASSERTION_ID,
        "classification",
        "public",
        "immutable_assertion",
    ),
    (
        "facts",
        "fact_id",
        _FACT_ID,
        "trust",
        "validated",
        "immutable_fact",
    ),
    (
        "fact_invalidations",
        "fact_id",
        _FACT_ID,
        "reason",
        "changed",
        "immutable_fact_invalidation",
    ),
    (
        "evidence_records",
        "evidence_id",
        _EVIDENCE_ID,
        "classification",
        "public",
        "immutable_evidence_record",
    ),
]


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_value", "update_column", "update_value", "message"),
    _CUSTODY_GUARD_TABLES,
)
def test_custody_tables_reject_update(
    custody_connection: sqlite3.Connection,
    table: str,
    pk_column: str,
    pk_value: str,
    update_column: str,
    update_value: str,
    message: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=message):
        custody_connection.execute(
            f"UPDATE {table} SET {update_column} = ? WHERE {pk_column} = ?",
            (update_value, pk_value),
        )


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_value", "update_column", "update_value", "message"),
    _CUSTODY_GUARD_TABLES,
)
def test_custody_tables_reject_delete(
    custody_connection: sqlite3.Connection,
    table: str,
    pk_column: str,
    pk_value: str,
    update_column: str,
    update_value: str,
    message: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=message):
        custody_connection.execute(
            f"DELETE FROM {table} WHERE {pk_column} = ?", (pk_value,)
        )


def _seeded_outbox_connection() -> sqlite3.Connection:
    connection = _seeded_custody_connection()
    connection.execute("BEGIN IMMEDIATE")
    _insert_evidence_outbox(connection)
    _insert_projection_outbox(connection)
    connection.commit()
    return connection


@pytest.fixture
def outbox_connection() -> sqlite3.Connection:
    return _seeded_outbox_connection()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("work_id", "88888888-8888-4888-8888-888888888888"),
        # The identity-frozen trigger fires BEFORE the row is written, so it
        # rejects this change before the single-value kind CHECK is ever
        # evaluated.
        ("kind", "not-a-real-kind"),
        ("evidence_id", _EVIDENCE_EXTERNAL_ID),
        ("mutation_id", "88888888-8888-4888-8888-888888888888"),
        ("payload", b"different"),
        ("created_at", "2026-08-06T00:00:00.000000Z"),
    ],
)
def test_evidence_outbox_identity_frozen_rejects_protected_update(
    outbox_connection: sqlite3.Connection,
    column: str,
    value: object,
) -> None:
    if column == "evidence_id":
        _insert_evidence_record(
            outbox_connection,
            evidence_id=_EVIDENCE_EXTERNAL_ID,
            assertion_id=None,
            payload_length=None,
            external_uri="https://example.com/other",
        )
    with pytest.raises(sqlite3.IntegrityError, match="evidence_outbox_identity_frozen"):
        outbox_connection.execute(
            f"UPDATE evidence_outbox SET {column} = ? WHERE work_id = ?",
            (value, _EVIDENCE_WORK_ID),
        )


def test_evidence_outbox_permits_retry_column_update_and_delete(
    outbox_connection: sqlite3.Connection,
) -> None:
    outbox_connection.execute(
        "UPDATE evidence_outbox SET attempts = 1, last_attempt_at = ?, "
        "last_failure_code = 'delivery_timeout' WHERE work_id = ?",
        (_TS, _EVIDENCE_WORK_ID),
    )
    assert outbox_connection.execute(
        "SELECT attempts, last_attempt_at, last_failure_code FROM evidence_outbox "
        "WHERE work_id = ?",
        (_EVIDENCE_WORK_ID,),
    ).fetchone() == (1, _TS, "delivery_timeout")

    outbox_connection.execute(
        "DELETE FROM evidence_outbox WHERE work_id = ?", (_EVIDENCE_WORK_ID,)
    )
    assert outbox_connection.execute(
        "SELECT count(*) FROM evidence_outbox"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("work_id", "88888888-8888-4888-8888-888888888888"),
        ("kind", "fact-promoted"),
        ("fact_id", _FACT_PROMOTED_ID),
        ("mutation_id", "88888888-8888-4888-8888-888888888888"),
        ("created_at", "2026-08-06T00:00:00.000000Z"),
    ],
)
def test_projection_outbox_identity_frozen_rejects_protected_update(
    outbox_connection: sqlite3.Connection,
    column: str,
    value: object,
) -> None:
    if column == "fact_id":
        _insert_fact(
            outbox_connection,
            fact_id=_FACT_PROMOTED_ID,
            assertion_id=None,
            derived_from=_FACT_ID,
            promoted_by=_HUMAN_ID,
            evidence_id=_EVIDENCE_ID,
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="projection_outbox_identity_frozen"
    ):
        outbox_connection.execute(
            f"UPDATE projection_outbox SET {column} = ? WHERE work_id = ?",
            (value, _PROJECTION_WORK_ID),
        )


def test_projection_outbox_permits_retry_column_update_and_delete(
    outbox_connection: sqlite3.Connection,
) -> None:
    outbox_connection.execute(
        "UPDATE projection_outbox SET attempts = 1, last_attempt_at = ?, "
        "last_failure_code = 'delivery_timeout' WHERE work_id = ?",
        (_TS, _PROJECTION_WORK_ID),
    )
    assert outbox_connection.execute(
        "SELECT attempts, last_attempt_at, last_failure_code FROM "
        "projection_outbox WHERE work_id = ?",
        (_PROJECTION_WORK_ID,),
    ).fetchone() == (1, _TS, "delivery_timeout")

    outbox_connection.execute(
        "DELETE FROM projection_outbox WHERE work_id = ?", (_PROJECTION_WORK_ID,)
    )
    assert outbox_connection.execute(
        "SELECT count(*) FROM projection_outbox"
    ).fetchone() == (0,)


# The retry columns are the schema's sole mutable-by-exception surface —
# every other custody row is frozen by a no-update trigger — and
# cairn.evidence.delivery writes all three of them on every attempt. The
# permits-retry-column-update tests above prove the exception exists; these
# prove the CHECKs still hold across it, which is a different claim and the
# one that matters, since an UPDATE is the only way a bad value can now get
# in.
_RETRY_COLUMN_VIOLATIONS = [
    pytest.param("attempts", -1, "attempts", id="attempts-negative"),
    pytest.param(
        "last_attempt_at", "not-a-timestamp", "last_attempt_at", id="attempt-timestamp"
    ),
    pytest.param(
        "last_failure_code", "Not Valid!", "last_failure_code", id="failure-code"
    ),
]


@pytest.mark.parametrize(("column", "value", "constraint"), _RETRY_COLUMN_VIOLATIONS)
def test_evidence_outbox_retry_columns_are_checked_on_update(
    outbox_connection: sqlite3.Connection,
    column: str,
    value: object,
    constraint: str,
) -> None:
    with pytest.raises(
        sqlite3.IntegrityError, match=f"ck_evidence_outbox_{constraint}"
    ):
        outbox_connection.execute(
            f"UPDATE evidence_outbox SET {column} = ? WHERE work_id = ?",
            (value, _EVIDENCE_WORK_ID),
        )


@pytest.mark.parametrize(("column", "value", "constraint"), _RETRY_COLUMN_VIOLATIONS)
def test_projection_outbox_retry_columns_are_checked_on_update(
    outbox_connection: sqlite3.Connection,
    column: str,
    value: object,
    constraint: str,
) -> None:
    with pytest.raises(
        sqlite3.IntegrityError, match=f"ck_projection_outbox_{constraint}"
    ):
        outbox_connection.execute(
            f"UPDATE projection_outbox SET {column} = ? WHERE work_id = ?",
            (value, _PROJECTION_WORK_ID),
        )


_BIG_CHAR = "\U0001d54a"  # 4 UTF-8 bytes per character


def test_facts_body_multi_byte_boundary_is_octet_not_character_counted(
    custody_connection: sqlite3.Connection,
) -> None:
    accepted = _BIG_CHAR * 16384
    rejected = accepted + "x"
    assert len(accepted) == 16384
    assert len(accepted.encode("utf-8")) == 65536
    assert len(rejected) == 16385
    assert len(rejected.encode("utf-8")) == 65537
    # Both bodies are far below 65536 characters, so a naive length(body)
    # check would accept both; only the octet bound tells them apart.

    custody_connection.execute("BEGIN IMMEDIATE")
    _insert_fact(
        custody_connection,
        fact_id="99999999-9999-4999-8999-999999999999",
        body=accepted,
    )
    custody_connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="ck_facts_body"):
        _insert_fact(
            custody_connection,
            fact_id="10101010-1010-4101-8101-101010101010",
            body=rejected,
        )


_ASSERTION_CHECK_CASES = [
    pytest.param({"assertion_id": "not-a-uuid"}, "ck_assertions_assertion_id"),
    pytest.param(
        {
            "scope_segments": json.dumps(
                [str(i) for i in range(17)], separators=(",", ":")
            )
        },
        "ck_assertions_scope_segments",
    ),
    pytest.param({"scope_segments": "not-json"}, "ck_assertions_scope_segments"),
    # The json_type clause, which nothing else in this CHECK can stand in
    # for: a JSON object is valid JSON, json_array_length() answers 0 for it,
    # and json() leaves it byte-identical — so deleting that clause admits
    # this row and the three reading guards that call their not-a-list branch
    # unreachable stop being right about that.
    pytest.param({"scope_segments": '{"a":1}'}, "ck_assertions_scope_segments"),
    pytest.param({"classification": "top-secret"}, "ck_assertions_classification"),
    pytest.param({"source_type": "guess"}, "ck_assertions_source_type"),
    pytest.param({"observed_at": "not-a-timestamp"}, "ck_assertions_observed_at"),
    pytest.param(
        {"metadata": json.dumps({"k": "x" * 70000})}, "ck_assertions_metadata"
    ),
    pytest.param({"recorded_at": "not-a-timestamp"}, "ck_assertions_recorded_at"),
]


@pytest.mark.parametrize(("overrides", "constraint"), _ASSERTION_CHECK_CASES)
def test_assertions_check_rejects_violating_row(
    custody_connection: sqlite3.Connection,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    custody_connection.execute("BEGIN IMMEDIATE")
    row = {"assertion_id": "99999999-9999-4999-8999-999999999999", **overrides}
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_assertion(custody_connection, **row)


def test_assertions_scope_segments_rejects_pretty_accepts_minified(
    custody_connection: sqlite3.Connection,
) -> None:
    segment = [{"kind": "job", "id": "1"}]
    pretty = json.dumps(segment)
    minified = json.dumps(segment, separators=(",", ":"))
    assert pretty != minified  # same logical array, different bytes

    custody_connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="ck_assertions_scope_segments"):
        _insert_assertion(
            custody_connection,
            assertion_id="99999999-9999-4999-8999-999999999999",
            scope_segments=pretty,
        )

    _insert_assertion(
        custody_connection,
        assertion_id="99999999-9999-4999-8999-999999999999",
        scope_segments=minified,
    )
    custody_connection.commit()

    assert custody_connection.execute(
        "SELECT scope_segments FROM assertions WHERE assertion_id = ?",
        ("99999999-9999-4999-8999-999999999999",),
    ).fetchone() == (minified,)


_FACT_CHECK_CASES = [
    pytest.param({"fact_id": "not-a-uuid"}, "ck_facts_fact_id"),
    pytest.param(
        {
            "scope_segments": json.dumps(
                [str(i) for i in range(17)], separators=(",", ":")
            )
        },
        "ck_facts_scope_segments",
    ),
    # See the assertions case: only the json_type clause refuses an object.
    pytest.param({"scope_segments": '{"a":1}'}, "ck_facts_scope_segments"),
    pytest.param({"body": ""}, "ck_facts_body"),
    pytest.param({"trust": "maybe"}, "ck_facts_trust"),
    pytest.param({"classification": "top-secret"}, "ck_facts_classification"),
    pytest.param(
        {
            "assertion_id": _ASSERTION_ID,
            "derived_from": _FACT_ID,
            "promoted_by": _HUMAN_ID,
            "evidence_id": _EVIDENCE_ID,
        },
        "ck_facts_provenance_form",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "derived_from": None,
            "promoted_by": None,
            "evidence_id": None,
        },
        "ck_facts_provenance_form",
    ),
    pytest.param({"valid_from": "not-a-timestamp"}, "ck_facts_valid_from"),
    pytest.param({"valid_to": "not-a-timestamp"}, "ck_facts_valid_to"),
    pytest.param({"valid_from": _TS, "valid_to": _TS}, "ck_facts_validity_order"),
    pytest.param({"recorded_at": "not-a-timestamp"}, "ck_facts_recorded_at"),
]


@pytest.mark.parametrize(("overrides", "constraint"), _FACT_CHECK_CASES)
def test_facts_check_rejects_violating_row(
    custody_connection: sqlite3.Connection,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    custody_connection.execute("BEGIN IMMEDIATE")
    row = {"fact_id": "99999999-9999-4999-8999-999999999999", **overrides}
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_fact(custody_connection, **row)


def test_facts_scope_segments_rejects_pretty_accepts_minified(
    custody_connection: sqlite3.Connection,
) -> None:
    segment = [{"kind": "job", "id": "1"}]
    pretty = json.dumps(segment)
    minified = json.dumps(segment, separators=(",", ":"))
    assert pretty != minified  # same logical array, different bytes

    custody_connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="ck_facts_scope_segments"):
        _insert_fact(
            custody_connection,
            fact_id="99999999-9999-4999-8999-999999999999",
            scope_segments=pretty,
        )

    _insert_fact(
        custody_connection,
        fact_id="99999999-9999-4999-8999-999999999999",
        scope_segments=minified,
    )
    custody_connection.commit()

    assert custody_connection.execute(
        "SELECT scope_segments FROM facts WHERE fact_id = ?",
        ("99999999-9999-4999-8999-999999999999",),
    ).fetchone() == (minified,)


_FACT_INVALIDATION_CHECK_CASES = [
    pytest.param(
        {"invalidated_at": "not-a-timestamp"},
        "ck_fact_invalidations_invalidated_at",
    ),
    pytest.param({"reason": "x" * 4097}, "ck_fact_invalidations_reason"),
    pytest.param({"reason": ""}, "ck_fact_invalidations_reason"),
]


@pytest.mark.parametrize(("overrides", "constraint"), _FACT_INVALIDATION_CHECK_CASES)
def test_fact_invalidations_check_rejects_violating_row(
    overrides: dict[str, object],
    constraint: str,
) -> None:
    connection = _authority_schema_connection()
    _seed_realm_and_principals(connection)
    connection.execute("BEGIN IMMEDIATE")
    _insert_assertion(connection)
    _insert_fact(connection)
    _insert_fact(connection, fact_id=_FACT_PROMOTED_ID, body="other")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_fact_invalidation(connection, fact_id=_FACT_PROMOTED_ID, **overrides)


_EVIDENCE_RECORD_CHECK_CASES = [
    pytest.param({"evidence_id": "not-a-uuid"}, "ck_evidence_records_evidence_id"),
    pytest.param(
        {
            "scope_segments": json.dumps(
                [str(i) for i in range(17)], separators=(",", ":")
            )
        },
        "ck_evidence_records_scope_segments",
    ),
    # See the assertions case: only the json_type clause refuses an object.
    pytest.param({"scope_segments": '{"a":1}'}, "ck_evidence_records_scope_segments"),
    pytest.param(
        {"classification": "top-secret"}, "ck_evidence_records_classification"
    ),
    pytest.param({"payload_digest": bytes(31)}, "ck_evidence_records_payload_digest"),
    pytest.param({"payload_digest": bytes(33)}, "ck_evidence_records_payload_digest"),
    pytest.param({"payload_length": 0}, "ck_evidence_records_payload_length"),
    pytest.param({"payload_length": 1048577}, "ck_evidence_records_payload_length"),
    pytest.param(
        {
            "assertion_id": _ASSERTION_ID,
            "payload_length": 10,
            "external_uri": "https://example.com/both-forms",
        },
        "ck_evidence_records_custody_form",
    ),
    pytest.param(
        {"assertion_id": None, "payload_length": None, "external_uri": None},
        "ck_evidence_records_custody_form",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "payload_length": None,
            "external_uri": "https://example.com/a b",
        },
        "ck_evidence_records_external_uri",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "payload_length": None,
            "external_uri": "https://example.com/\x01",
        },
        "ck_evidence_records_external_uri",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "payload_length": None,
            "external_uri": "https://example.com/é",
        },
        "ck_evidence_records_external_uri",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "payload_length": None,
            "external_uri": "not-a-uri",
        },
        "ck_evidence_records_external_uri",
    ),
    pytest.param(
        {
            "assertion_id": None,
            "payload_length": None,
            "external_uri": "https://example.com/" + "x" * 2040,
        },
        "ck_evidence_records_external_uri",
    ),
    pytest.param({"recorded_at": "not-a-timestamp"}, "ck_evidence_records_recorded_at"),
]


@pytest.mark.parametrize(("overrides", "constraint"), _EVIDENCE_RECORD_CHECK_CASES)
def test_evidence_records_check_rejects_violating_row(
    custody_connection: sqlite3.Connection,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    custody_connection.execute("BEGIN IMMEDIATE")
    row = {"evidence_id": "99999999-9999-4999-8999-999999999999", **overrides}
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_evidence_record(custody_connection, **row)


def test_evidence_records_scope_segments_rejects_pretty_accepts_minified(
    custody_connection: sqlite3.Connection,
) -> None:
    segment = [{"kind": "job", "id": "1"}]
    pretty = json.dumps(segment)
    minified = json.dumps(segment, separators=(",", ":"))
    assert pretty != minified  # same logical array, different bytes

    custody_connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(
        sqlite3.IntegrityError, match="ck_evidence_records_scope_segments"
    ):
        _insert_evidence_record(
            custody_connection,
            evidence_id="99999999-9999-4999-8999-999999999999",
            scope_segments=pretty,
        )

    _insert_evidence_record(
        custody_connection,
        evidence_id="99999999-9999-4999-8999-999999999999",
        scope_segments=minified,
    )
    custody_connection.commit()

    assert custody_connection.execute(
        "SELECT scope_segments FROM evidence_records WHERE evidence_id = ?",
        ("99999999-9999-4999-8999-999999999999",),
    ).fetchone() == (minified,)


_EVIDENCE_OUTBOX_CHECK_CASES = [
    pytest.param({"work_id": "not-a-uuid"}, "ck_evidence_outbox_work_id"),
    pytest.param({"kind": "delete-payload"}, "ck_evidence_outbox_kind"),
    pytest.param({"mutation_id": "not-a-uuid"}, "ck_evidence_outbox_mutation_id"),
    pytest.param({"payload": b""}, "ck_evidence_outbox_payload"),
    pytest.param({"created_at": "not-a-timestamp"}, "ck_evidence_outbox_created_at"),
    pytest.param({"attempts": -1}, "ck_evidence_outbox_attempts"),
    pytest.param(
        {"last_attempt_at": "not-a-timestamp"}, "ck_evidence_outbox_last_attempt_at"
    ),
    pytest.param(
        {"last_failure_code": "Not Valid!"},
        "ck_evidence_outbox_last_failure_code",
    ),
]


@pytest.mark.parametrize(("overrides", "constraint"), _EVIDENCE_OUTBOX_CHECK_CASES)
def test_evidence_outbox_check_rejects_violating_row(
    custody_connection: sqlite3.Connection,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    custody_connection.execute("BEGIN IMMEDIATE")
    row = {"work_id": "88888888-8888-4888-8888-888888888888", **overrides}
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_evidence_outbox(custody_connection, **row)


_PROJECTION_OUTBOX_CHECK_CASES = [
    pytest.param({"work_id": "not-a-uuid"}, "ck_projection_outbox_work_id"),
    pytest.param({"kind": "fact-deleted"}, "ck_projection_outbox_kind"),
    pytest.param({"mutation_id": "not-a-uuid"}, "ck_projection_outbox_mutation_id"),
    pytest.param({"created_at": "not-a-timestamp"}, "ck_projection_outbox_created_at"),
    pytest.param({"attempts": -1}, "ck_projection_outbox_attempts"),
    pytest.param(
        {"last_attempt_at": "not-a-timestamp"},
        "ck_projection_outbox_last_attempt_at",
    ),
    pytest.param(
        {"last_failure_code": "Not Valid!"},
        "ck_projection_outbox_last_failure_code",
    ),
]


@pytest.mark.parametrize(("overrides", "constraint"), _PROJECTION_OUTBOX_CHECK_CASES)
def test_projection_outbox_check_rejects_violating_row(
    custody_connection: sqlite3.Connection,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    custody_connection.execute("BEGIN IMMEDIATE")
    row = {"work_id": "88888888-8888-4888-8888-888888888888", **overrides}
    with pytest.raises(sqlite3.IntegrityError, match=constraint):
        _insert_projection_outbox(custody_connection, **row)


def test_migration_0007_creates_the_projection_cache_tables(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    with read_connection(tmp_path) as connection:
        extraction = connection.execute(
            "SELECT name, pk FROM pragma_table_info('projection_extraction_cache')"
        ).fetchall()
        embedding = connection.execute(
            "SELECT name FROM pragma_table_info('projection_embedding_cache')"
        ).fetchall()
    # R3: storage is fact-owned — the composite primary key is what lets
    # two facts with byte-identical bodies each keep their own row.
    assert extraction == [
        ("cache_key", 1),
        ("fact_id", 2),
        ("payload", 0),
        ("created_at", 0),
    ]
    assert [column for (column,) in embedding] == [
        "cache_key",
        "payload",
        "created_at",
    ]
