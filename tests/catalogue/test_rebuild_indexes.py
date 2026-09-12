"""0013 contracts against disposable catalogues, without a projection engine."""

import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_migration import NOW, _config, _copied_v0004

from cairn.catalogue import sqlite
from cairn.catalogue.migration import (
    _PACKAGED_MIGRATIONS,
    Advanced,
    Created,
    Current,
    MigrationError,
    _migrate_catalogue,
    migrate_catalogue,
)
from cairn.catalogue.sqlite import _open_write_connection, read_connection
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.projection import rebuild
from cairn.runtime.config import CairnConfig


def _v12(tmp_path: Path) -> CairnConfig:
    """Apply genuine old SQL, not a current database relabelled as historical."""
    _, config = _copied_v0004(tmp_path)
    root = tmp_path / "v12-migrations"
    root.mkdir()
    manifest = json.loads((_PACKAGED_MIGRATIONS / "manifest.json").read_text())[:12]
    for entry in manifest:
        shutil.copyfile(
            _PACKAGED_MIGRATIONS / entry["resource"], root / entry["resource"]
        )
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert _migrate_catalogue(
        config, lambda: NOW, root, expected_schema_version=12
    ) == Advanced(4, 12)
    with _open_write_connection(config.paths.data, create=False) as connection:
        # Reuse valid provenance from the historical fixture; new facts share a
        # timestamp so a page boundary necessarily cuts through a tie.
        for number in (5, 1, 4, 2, 3):
            fact_id = f"bbbbbbbb-bbbb-4bbb-8bbb-{number:012d}"
            connection.execute(
                "INSERT INTO facts SELECT ?, realm_id, scope_segments, body, trust, "
                "classification, assertion_id, derived_from, promoted_by, evidence_id, "
                "valid_from, valid_to, '2026-08-10T00:00:00.000000Z' "
                "FROM facts ORDER BY fact_id LIMIT 1",
                (fact_id,),
            )
            for kind in ("fact-rebuild", "fact-ingested"):
                connection.execute(
                    "INSERT INTO projection_outbox "
                    "(work_id, kind, fact_id, mutation_id, created_at, attempts) "
                    "VALUES (?, ?, ?, ?, '2026-08-10T00:00:00.000000Z', 0)",
                    (
                        str(uuid4()),
                        kind,
                        fact_id,
                        None if kind == "fact-rebuild" else str(uuid4()),
                    ),
                )
        connection.execute(
            "INSERT INTO fact_invalidations "
            "(fact_id, invalidated_at, principal_id, superseded_by, reason) "
            "SELECT 'bbbbbbbb-bbbb-4bbb-8bbb-000000000003', "
            "'2026-08-11T00:00:00.000000Z', principal_id, NULL, 'fixture invalidation' "
            "FROM principals LIMIT 1"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (12,)
    return config


def _rows(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "facts",
            "fact_invalidations",
            "projection_outbox",
            "schema_migrations",
        )
    }


def test_public_fresh_catalogue_has_exact_rebuild_indexes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert migrate_catalogue(config, lambda: NOW) == Created(13)
    with read_connection(tmp_path) as connection:
        for table, name, columns in (
            (
                "projection_outbox",
                "ix_projection_outbox_fact_kind",
                ["fact_id", "kind"],
            ),
            ("facts", "ix_facts_recorded_fact", ["recorded_at", "fact_id"]),
        ):
            indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
            assert [(row[2], row[3], row[4]) for row in indexes if row[1] == name] == [
                (0, "c", 0)
            ]
            assert [
                row[2] for row in connection.execute(f"PRAGMA index_info({name})")
            ] == columns
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate_catalogue(config, lambda: NOW) == Current(13)


def test_public_v12_upgrade_preserves_rows_and_actual_rebuild_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _v12(tmp_path)
    data = config.paths.data
    statements: list[str] = []

    @contextmanager
    def traced_reader(path: Path) -> Iterator[sqlite3.Connection]:
        with read_connection(path) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    # Only the baseline reader emulates the old application's version check.
    # Both upgrade and post-upgrade reading use unmodified public validation.
    with monkeypatch.context() as baseline:
        baseline.setattr(sqlite, "CURRENT_SCHEMA_VERSION", 12)
        baseline.setattr(rebuild, "read_connection", traced_reader)
        before_states = list(rebuild._stored_facts(data, batch_size=2))
        with read_connection(data) as connection:
            before = _rows(connection)
    assert all(state is not None for state in before_states)
    tied_ids = [
        str(state.fact_id)
        for state in before_states
        if state is not None and str(state.fact_id).startswith("bbbbbbbb")
    ]
    assert tied_ids == [f"bbbbbbbb-bbbb-4bbb-8bbb-{n:012d}" for n in range(1, 6)]
    assert any(
        state is not None and state.invalidated_at is not None
        for state in before_states
    )
    page_sql = [sql for sql in statements if sql.startswith("SELECT f.fact_id")]
    assert any("WHERE (f.recorded_at, f.fact_id) >" in sql for sql in page_sql)
    assert any("WHERE" not in sql for sql in page_sql)
    with _open_write_connection(data, create=False) as connection:
        baseline_pages = [connection.execute(sql).fetchall() for sql in page_sql]

    assert migrate_catalogue(config, lambda: NOW) == Advanced(12, 13)
    assert list(rebuild._stored_facts(data, batch_size=2)) == before_states
    with read_connection(data) as connection:
        after = _rows(connection)
        assert after.pop("schema_migrations")[:-1] == before.pop("schema_migrations")
        assert after == before
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
        for sql, expected in zip(page_sql, baseline_pages, strict=True):
            assert connection.execute(sql).fetchall() == expected
            plan = " ".join(
                row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)
            )
            assert "ix_facts_recorded_fact" in plan
            assert "TEMP B-TREE" not in plan
        plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN DELETE FROM projection_outbox "
                "WHERE fact_id = ? AND kind = 'fact-rebuild'",
                (tied_ids[0],),
            )
        )
        assert "ix_projection_outbox_fact_kind" in plan
    assert migrate_catalogue(config, lambda: NOW) == Current(13)

    transactions = CatalogueTransactions(
        data, writer_gate=threading.Lock(), clock=lambda: NOW, uuid_factory=uuid4
    )
    rebuild._discharge(transactions, UUID(tied_ids[0]))
    with read_connection(data) as connection:
        remaining = connection.execute(
            "SELECT * FROM projection_outbox ORDER BY 1"
        ).fetchall()
        # Compare every surviving column, not merely the other kind's count.
        expected = [
            row
            for row in before["projection_outbox"]
            if not (row[2] == "fact-rebuild" and row[3] == tied_ids[0])
        ]
        assert len(expected) == len(before["projection_outbox"]) - 1
        assert remaining == expected
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with _open_write_connection(data, create=False) as connection:
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


def test_second_index_failure_rolls_back_first_index_history_and_version(
    tmp_path: Path,
) -> None:
    config = _v12(tmp_path)
    with _open_write_connection(config.paths.data, create=False) as connection:
        # A pre-existing name collision fails the actual second CREATE; no
        # modified migration bytes or mocked transaction machinery are needed.
        connection.execute("CREATE INDEX ix_facts_recorded_fact ON facts(body)")
        before = _rows(connection)
        schema = connection.execute(
            "SELECT * FROM sqlite_schema ORDER BY name"
        ).fetchall()
    with pytest.raises(MigrationError, match="migration_execution_failed"):
        migrate_catalogue(config, lambda: NOW)
    with _open_write_connection(config.paths.data, create=False) as connection:
        assert _rows(connection) == before
        assert connection.execute("PRAGMA user_version").fetchone() == (12,)
        assert (
            connection.execute("SELECT * FROM sqlite_schema ORDER BY name").fetchall()
            == schema
        )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
