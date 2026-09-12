import importlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from cairn.catalogue.migration import Advanced, migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME, CURRENT_SCHEMA_VERSION
from cairn.catalogue.verification import VerificationReport
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
_TS = "2026-08-05T12:00:00.000000Z"
_PRINCIPAL_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_ASSERTION_ID = UUID("30001000-0000-4000-8000-000000000000")
_FACT_ID = UUID("30001001-0000-4000-8000-000000000000")


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _write_config(path: Path, config: CairnConfig) -> None:
    path.write_text(
        "schema_version: cairn.config/v1\n"
        f"instance_id: {config.instance_id}\n"
        "mode: test\n"
        "http:\n"
        "  host: 127.0.0.1\n"
        "  port: 8000\n"
        "paths:\n"
        f"  data: {config.paths.data}\n"
        f"  credentials: {config.paths.credentials}\n",
        encoding="utf-8",
    )


def test_migrate_outputs_one_safe_json_object(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _config(data_path))

    result = cli.main(["migrate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out) == {
        "applied_versions": list(range(1, CURRENT_SCHEMA_VERSION + 1)),
        "ending_version": CURRENT_SCHEMA_VERSION,
        "instance_id": str(INSTANCE_ID),
        "operation": "migrate",
        "starting_version": 0,
        "status": "ok",
    }
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_current_migrate_reports_no_applied_versions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(["migrate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out) == {
        "applied_versions": [],
        "ending_version": CURRENT_SCHEMA_VERSION,
        "instance_id": str(INSTANCE_ID),
        "operation": "migrate",
        "starting_version": CURRENT_SCHEMA_VERSION,
        "status": "ok",
    }


def test_advanced_migrate_reports_each_applied_version() -> None:
    cli = importlib.import_module("cairn.runtime.cli")

    assert cli._migration_payload(
        Advanced(previous_version=1, version=4),
        INSTANCE_ID,
    ) == {
        "applied_versions": [2, 3, 4],
        "ending_version": 4,
        "instance_id": str(INSTANCE_ID),
        "operation": "migrate",
        "starting_version": 1,
        "status": "ok",
    }


def test_verify_outputs_only_safe_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(["verify", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out) == {
        "assertion_count": 0,
        "credential_count": 0,
        "event_count": 0,
        "evidence_count": 0,
        "evidence_outbox_depth": 0,
        "fact_count": 0,
        "grant_count": 0,
        "idempotency_count": 0,
        "instance_id": str(INSTANCE_ID),
        "invalidation_count": 0,
        "operation": "verify",
        "principal_count": 0,
        "projection_outbox_depth": 0,
        "realm_count": 0,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "status": "ok",
    }
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert list(json.loads(captured.out)) == sorted(json.loads(captured.out))


def test_custody_corruption_returns_exit_four_without_content_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    body = "fact-body-secret-sentinel"
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(
        "INSERT INTO realms (realm_id, created_at) VALUES ('local', ?)", (_TS,)
    )
    connection.execute(
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', 'local', 0, zeroblob(32))"
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, 'workload', 'agent', ?)",
        (str(_PRINCIPAL_ID), _TS),
    )
    connection.execute(
        "INSERT INTO assertions (assertion_id, realm_id, scope_segments, "
        "classification, source_type, principal_id, observed_at, metadata, "
        "recorded_at) VALUES (?, 'local', '[]', 'internal', 'agent-claim', ?, "
        "NULL, NULL, ?)",
        (str(_ASSERTION_ID), str(_PRINCIPAL_ID), _TS),
    )
    # Canonical JSON, but no scope path — the gap the schema's array-shape
    # CHECK cannot close.
    connection.execute(
        "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
        "classification, assertion_id, recorded_at) "
        "VALUES (?, 'local', '[1,2,3]', ?, 'candidate', 'internal', ?, ?)",
        (str(_FACT_ID), body, str(_ASSERTION_ID), _TS),
    )
    connection.commit()
    connection.close()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(["verify", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "custody_value_invalid",
        "status": "error",
    }
    assert captured.out == ""
    assert body not in captured.err
    assert "facts" not in captured.err


def test_verify_payload_maps_every_count_to_its_own_key() -> None:
    """Twelve distinct values, so no key can read another's count unseen.

    The safe-inventory test above runs against an empty catalogue where every
    count is 0, which cannot tell a correct mapping from a permuted one.
    """
    cli = importlib.import_module("cairn.runtime.cli")

    assert cli._verification_payload(
        VerificationReport(
            schema_version=6,
            instance_id=INSTANCE_ID,
            realm_count=1,
            event_count=2,
            idempotency_count=3,
            principal_count=4,
            credential_count=5,
            grant_count=6,
            assertion_count=7,
            fact_count=8,
            invalidation_count=9,
            evidence_count=10,
            evidence_outbox_depth=11,
            projection_outbox_depth=12,
        )
    ) == {
        "assertion_count": 7,
        "credential_count": 5,
        "event_count": 2,
        "evidence_count": 10,
        "evidence_outbox_depth": 11,
        "fact_count": 8,
        "grant_count": 6,
        "idempotency_count": 3,
        "instance_id": str(INSTANCE_ID),
        "invalidation_count": 9,
        "operation": "verify",
        "principal_count": 4,
        "projection_outbox_depth": 12,
        "realm_count": 1,
        "schema_version": 6,
        "status": "ok",
    }


def test_missing_catalogue_returns_exit_three_without_path_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "missing-catalogue-secret-sentinel"
    data_path = tmp_path / sentinel
    data_path.mkdir()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _config(data_path))

    result = cli.main(["verify", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "catalogue_unavailable",
        "status": "error",
    }
    assert captured.out == ""
    assert sentinel not in captured.err


def test_corrupt_catalogue_returns_exit_four_without_sql_or_hash_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute("PRAGMA application_id = 7")
    connection.close()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(["verify", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "application_id_mismatch",
        "status": "error",
    }
    assert captured.out == ""
    assert "PRAGMA" not in captured.err
    assert "0x" not in captured.err


def test_migration_rejection_returns_exit_four_without_sql_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    catalogue_path = data_path / CATALOGUE_FILENAME
    connection = sqlite3.connect(catalogue_path)
    connection.execute("CREATE TABLE forbidden_secret(value TEXT)")
    connection.close()
    catalogue_path.chmod(0o660)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _config(data_path))

    result = cli.main(["migrate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "unrecognised_catalogue",
        "status": "error",
    }
    assert captured.out == ""
    assert "forbidden_secret" not in captured.err
    assert "CREATE TABLE" not in captured.err


def test_unexpected_catalogue_exception_is_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _config(data_path))
    sentinel = "raw-exception-secret-sentinel"

    def fail(_config: CairnConfig) -> None:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "verify_catalogue", fail, raising=False)

    result = cli.main(["verify", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "internal_error",
        "status": "error",
    }
    assert captured.out == ""
    assert sentinel not in captured.err
