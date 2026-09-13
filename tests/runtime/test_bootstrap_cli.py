import hashlib
import importlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import cairn.bootstrap.procedures as procedures_module
from cairn.authority.credentials import DATA_OPERATIONS, TOKEN_PATTERN, GrantOperation
from cairn.bootstrap.procedures import (
    BootstrapError,
    BootstrapResult,
    RecoveryResult,
    bootstrap_realm,
    recover_realm,
)
from cairn.catalogue.audit import AuditEvent, Scope, parse_canonical_audit_bytes
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
)
from cairn.catalogue.transactions import (
    CatalogueTransactionError,
    CatalogueTransactions,
    CommitAmbiguity,
)
from cairn.catalogue.verification import VerificationReport, verify_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.lease import DataDirectoryLease

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_OTHER_INSTANCE_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
_REALM = "acme"
_LABEL = "operator"


def _config(data_path: Path, *, instance_id: UUID = INSTANCE_ID) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=instance_id,
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


def _migrated(tmp_path: Path) -> CairnConfig:
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    return config


def _uuid_seq(start: int) -> Callable[[], UUID]:
    counter = itertools.count(start)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def _fixed_entropy(value: int = 7) -> Callable[[int], bytes]:
    return lambda size: bytes([value]) * size


def _bootstrap(config: CairnConfig, *, realm_id: str = _REALM) -> BootstrapResult:
    return bootstrap_realm(
        config,
        realm_id=realm_id,
        label=_LABEL,
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x10000000),
        entropy=_fixed_entropy(),
    )


def _lose_after_commit(connection: sqlite3.Connection) -> None:
    connection.commit()
    raise CommitAmbiguity


def _ambiguous_transactions(
    data_path: Path,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=clock,
        uuid_factory=uuid_factory,
        commit=_lose_after_commit,
    )


def _catalogue_bytes(data_path: Path) -> bytes:
    # Recursive and unfiltered: by the time a procedure returns, SQLite has
    # already checkpointed and removed the -wal file on last-connection
    # close, so a scan limited to files named after CATALOGUE_FILENAME would
    # only ever see the main database file post-hoc. Scanning the whole tree
    # (lock file included) is what actually backs "nowhere in the data
    # directory" rather than silently proving nothing about the WAL.
    return b"".join(
        path.read_bytes() for path in sorted(data_path.rglob("*")) if path.is_file()
    )


def _connection(data_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(data_path / CATALOGUE_FILENAME)


def _count(data_path: Path, table: str) -> int:
    connection = _connection(data_path)
    try:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _audit_event(data_path: Path, chain_identity: str, sequence: int) -> AuditEvent:
    connection = _connection(data_path)
    try:
        row = connection.execute(
            "SELECT canonical_event FROM audit_events WHERE chain_kind = 'realm' "
            "AND chain_identity = ? AND sequence = ?",
            (chain_identity, sequence),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return parse_canonical_audit_bytes(row[0])


def _grant_rows(data_path: Path, realm_id: str) -> list[Any]:
    connection = _connection(data_path)
    try:
        return connection.execute(
            "SELECT grant_id, principal_id, operations, delegable_operations, "
            "issued_by, expires_at FROM grants WHERE realm_id = ? ORDER BY grant_id",
            (realm_id,),
        ).fetchall()
    finally:
        connection.close()


# === Step 1: bootstrap_realm =================================================


def test_bootstrap_creates_everything_in_one_transaction(tmp_path: Path) -> None:
    config = _migrated(tmp_path)

    result = _bootstrap(config)

    assert _count(config.paths.data, "realms") == 1
    assert _count(config.paths.data, "principals") == 1
    assert _count(config.paths.data, "credentials") == 1
    assert _count(config.paths.data, "grants") == 3

    genesis = _audit_event(config.paths.data, _REALM, 1)
    assert genesis.sequence == 1
    assert genesis.draft.action_kind.value == "system"
    assert genesis.draft.action_code == "realm-genesis"
    assert genesis.draft.outcome.value == "allow"
    assert genesis.draft.reason_code == "realm_created"
    assert genesis.draft.principal_id is None

    admin = _audit_event(config.paths.data, _REALM, 2)
    assert admin.sequence == 2
    assert admin.draft.action_kind.value == "administration"
    assert admin.draft.action_code == "realm-bootstrap"
    assert admin.draft.outcome.value == "allow"
    assert admin.draft.reason_code == "bootstrap_completed"
    assert admin.draft.principal_id is None
    assert admin.draft.requested_scope == Scope(_REALM, ())
    assert admin.draft.affected_grant_ids == tuple(sorted(result.grant_ids, key=str))

    grants = _grant_rows(config.paths.data, _REALM)
    assert len(grants) == 3
    operations_by_grant = {row[0]: json.loads(row[2]) for row in grants}
    assert sorted(operations_by_grant.values()) == [
        ["audit-read"],
        ["grant-manage"],
        sorted(op.value for op in DATA_OPERATIONS),
    ]
    for (
        _grant_id,
        principal_id,
        _operations,
        _delegable,
        issued_by,
        expires_at,
    ) in grants:
        assert principal_id == str(result.principal_id)
        assert issued_by is None
        assert expires_at is None
    manage_grant = next(row for row in grants if json.loads(row[2]) == ["grant-manage"])
    assert json.loads(manage_grant[3]) == [
        "audit-read",
        "ingest",
        "invalidate",
        "promote",
        "retrieve",
    ]


def test_bootstrap_realm_output_verifies_cleanly(tmp_path: Path) -> None:
    config = _migrated(tmp_path)

    _bootstrap(config)

    assert verify_catalogue(config) == VerificationReport(
        schema_version=CURRENT_SCHEMA_VERSION,
        instance_id=INSTANCE_ID,
        realm_count=1,
        event_count=2,
        idempotency_count=0,
        principal_count=1,
        credential_count=1,
        grant_count=3,
        assertion_count=0,
        fact_count=0,
        invalidation_count=0,
        evidence_count=0,
        evidence_outbox_depth=0,
        projection_outbox_depth=0,
    )


def test_bootstrap_refuses_existing_realm_without_residue(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    _bootstrap(config)
    before = verify_catalogue(config)

    with pytest.raises(BootstrapError) as caught:
        _bootstrap(config)

    assert caught.value.code == "realm_exists"
    assert verify_catalogue(config) == before


def test_injected_failure_after_genesis_leaves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _migrated(tmp_path)

    def raise_after_genesis(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(procedures_module, "_insert_root_grant", raise_after_genesis)

    with pytest.raises(RuntimeError):
        _bootstrap(config)

    assert _count(config.paths.data, "realms") == 0
    assert _count(config.paths.data, "principals") == 0
    assert _count(config.paths.data, "credentials") == 0
    assert _count(config.paths.data, "grants") == 0
    assert _count(config.paths.data, "audit_events") == 0


def test_bootstrap_refuses_absent_catalogue(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = _config(data_path)

    with pytest.raises(CatalogueStorageError):
        _bootstrap(config)


def test_bootstrap_refuses_non_current_catalogue(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    connection.execute("PRAGMA user_version = 1")
    connection.close()

    with pytest.raises(BootstrapError) as caught:
        _bootstrap(config)

    assert caught.value.code == "catalogue_not_current"


def test_bootstrap_refuses_instance_mismatch(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    migrate_catalogue(_config(data_path, instance_id=INSTANCE_ID), lambda: NOW)
    mismatched_config = _config(data_path, instance_id=_OTHER_INSTANCE_ID)

    with pytest.raises(BootstrapError) as caught:
        bootstrap_realm(
            mismatched_config,
            realm_id=_REALM,
            label=_LABEL,
            clock=lambda: NOW,
            uuid_factory=_uuid_seq(0x10000000),
            entropy=_fixed_entropy(),
        )

    assert caught.value.code == "instance_mismatch"
    assert _count(data_path, "realms") == 0


def test_bootstrap_ambiguous_commit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _migrated(tmp_path)
    monkeypatch.setattr(procedures_module, "_transactions", _ambiguous_transactions)

    with pytest.raises(CatalogueTransactionError) as caught:
        _bootstrap(config)

    assert caught.value.code == "commit_outcome_unknown"
    # the write actually landed despite the ambiguous acknowledgement, so a
    # re-run correctly refuses with realm_exists rather than double-creating.
    assert _count(config.paths.data, "realms") == 1


def test_bootstrap_token_secret_never_touches_stored_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _migrated(tmp_path)
    # Capture the data directory's bytes right after COMMIT but before the
    # connection closes — confirmed empirically that this, not any point
    # mid-transaction, is where the -wal file genuinely holds the just
    # written pages (a small transaction like this stays entirely in
    # SQLite's private page cache until commit, so a mid-transaction capture
    # sees a 0-byte -wal file). SQLite checkpoints and removes -wal/-shm
    # when the last connection closes, so a post-return-only scan can never
    # observe this window at all.
    commit_snapshot: list[bytes] = []

    def commit_and_capture(connection: sqlite3.Connection) -> None:
        connection.commit()
        commit_snapshot.append(_catalogue_bytes(config.paths.data))

    def capturing_transactions(
        data_path: Path,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
    ) -> CatalogueTransactions:
        return CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=clock,
            uuid_factory=uuid_factory,
            commit=commit_and_capture,
        )

    monkeypatch.setattr(procedures_module, "_transactions", capturing_transactions)

    result = _bootstrap(config)

    assert TOKEN_PATTERN.fullmatch(result.token) is not None
    secret_component = result.token.rsplit(".", 1)[1].encode()

    connection = _connection(config.paths.data)
    try:
        stored_verifier = connection.execute(
            "SELECT verifier FROM credentials WHERE credential_id = ?",
            (str(result.credential_id),),
        ).fetchone()[0]
    finally:
        connection.close()

    assert stored_verifier == hashlib.sha256(secret_component).digest()
    assert len(commit_snapshot) == 1
    assert len(commit_snapshot[0]) > 0
    assert secret_component not in commit_snapshot[0]
    assert secret_component not in _catalogue_bytes(config.paths.data)


# === Step 2: recover_realm ====================================================


def test_recover_with_principal_creates_replacement_grant_manage_grant(
    tmp_path: Path,
) -> None:
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)

    result = recover_realm(
        config,
        realm_id=_REALM,
        principal_id=bootstrapped.principal_id,
        label=None,
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x30000000),
        entropy=_fixed_entropy(9),
    )

    assert isinstance(result, RecoveryResult)
    assert result.principal_id == bootstrapped.principal_id
    assert result.credential_id is None
    assert result.token is None
    assert _count(config.paths.data, "principals") == 1
    assert _count(config.paths.data, "credentials") == 1

    grants = _grant_rows(config.paths.data, _REALM)
    new_grant = next(row for row in grants if row[0] == str(result.grant_id))
    assert json.loads(new_grant[2]) == ["grant-manage"]
    assert new_grant[4] is None  # issued_by
    assert json.loads(new_grant[3]) == [
        "audit-read",
        "ingest",
        "invalidate",
        "promote",
        "retrieve",
    ]

    event = _audit_event(config.paths.data, _REALM, 3)
    assert event.draft.action_kind.value == "administration"
    assert event.draft.action_code == "realm-recover"
    assert event.draft.reason_code == "recovery_grant_issued"
    assert event.draft.principal_id is None
    assert event.draft.affected_grant_ids == (result.grant_id,)


def test_recover_with_label_creates_new_principal_and_credential(
    tmp_path: Path,
) -> None:
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)

    result = recover_realm(
        config,
        realm_id=_REALM,
        principal_id=None,
        label="rescuer",
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x30000000),
        entropy=_fixed_entropy(9),
    )

    assert result.principal_id != bootstrapped.principal_id
    assert result.credential_id is not None
    assert result.token is not None
    assert TOKEN_PATTERN.fullmatch(result.token) is not None
    assert _count(config.paths.data, "principals") == 2
    assert _count(config.paths.data, "credentials") == 2


def test_recover_refuses_unknown_realm(tmp_path: Path) -> None:
    config = _migrated(tmp_path)

    with pytest.raises(BootstrapError) as caught:
        recover_realm(
            config,
            realm_id=_REALM,
            principal_id=UUID("22222222-0000-4000-8000-000000000000"),
            label=None,
            clock=lambda: NOW,
            uuid_factory=_uuid_seq(0x30000000),
            entropy=_fixed_entropy(),
        )

    assert caught.value.code == "realm_unknown"


def test_recover_refuses_unknown_principal(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    _bootstrap(config)

    with pytest.raises(BootstrapError) as caught:
        recover_realm(
            config,
            realm_id=_REALM,
            principal_id=UUID("99999999-0000-4000-8000-000000000000"),
            label=None,
            clock=lambda: NOW,
            uuid_factory=_uuid_seq(0x30000000),
            entropy=_fixed_entropy(),
        )

    assert caught.value.code == "principal_unknown"


def test_recover_refuses_both_selectors(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)

    with pytest.raises(BootstrapError) as caught:
        recover_realm(
            config,
            realm_id=_REALM,
            principal_id=bootstrapped.principal_id,
            label="rescuer",
            clock=lambda: NOW,
            uuid_factory=_uuid_seq(0x30000000),
            entropy=_fixed_entropy(),
        )

    assert caught.value.code == "invalid_selector"


def test_recover_refuses_neither_selector(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    _bootstrap(config)

    with pytest.raises(BootstrapError) as caught:
        recover_realm(
            config,
            realm_id=_REALM,
            principal_id=None,
            label=None,
            clock=lambda: NOW,
            uuid_factory=_uuid_seq(0x30000000),
            entropy=_fixed_entropy(),
        )

    assert caught.value.code == "invalid_selector"


def test_recover_rerun_creates_further_replacement_grant(tmp_path: Path) -> None:
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)

    first = recover_realm(
        config,
        realm_id=_REALM,
        principal_id=bootstrapped.principal_id,
        label=None,
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x30000000),
        entropy=_fixed_entropy(9),
    )
    second = recover_realm(
        config,
        realm_id=_REALM,
        principal_id=bootstrapped.principal_id,
        label=None,
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x40000000),
        entropy=_fixed_entropy(9),
    )

    assert first.grant_id != second.grant_id
    connection = _connection(config.paths.data)
    try:
        revoked = connection.execute(
            "SELECT grant_id FROM grant_revocations"
        ).fetchall()
    finally:
        connection.close()
    assert revoked == []
    grants = _grant_rows(config.paths.data, _REALM)
    grant_ids = {row[0] for row in grants}
    assert {str(first.grant_id), str(second.grant_id)} <= grant_ids


def test_recover_new_grant_delegable_operations_never_contains_grant_manage(
    tmp_path: Path,
) -> None:
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)

    result = recover_realm(
        config,
        realm_id=_REALM,
        principal_id=bootstrapped.principal_id,
        label=None,
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x30000000),
        entropy=_fixed_entropy(9),
    )

    grants = _grant_rows(config.paths.data, _REALM)
    new_grant = next(row for row in grants if row[0] == str(result.grant_id))
    assert GrantOperation.GRANT_MANAGE.value not in json.loads(new_grant[3])


# === Step 3: CLI ==============================================================


def test_bootstrap_cli_outputs_one_sorted_json_object_with_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["operation"] == "bootstrap"
    assert payload["instance_id"] == str(INSTANCE_ID)
    assert payload["realm_id"] == _REALM
    assert UUID(payload["principal_id"]).version == 4
    assert UUID(payload["credential_id"]).version == 4
    assert len(payload["grant_ids"]) == 3
    assert TOKEN_PATTERN.fullmatch(payload["token"]) is not None
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_recover_cli_with_principal_omits_credential_and_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "recover",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--principal",
            str(bootstrapped.principal_id),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    payload = json.loads(captured.out)
    assert payload["operation"] == "recover"
    assert payload["principal_id"] == str(bootstrapped.principal_id)
    assert payload["credential_id"] is None
    assert payload["token"] is None
    assert captured.err == ""


def test_recover_cli_with_label_returns_token_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "recover",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            "rescuer",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    payload = json.loads(captured.out)
    assert payload["credential_id"] is not None
    assert TOKEN_PATTERN.fullmatch(payload["token"]) is not None


def test_bootstrap_cli_missing_label_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    with pytest.raises(SystemExit) as raised:
        cli.main(["bootstrap", "--config", str(config_path), "--realm", _REALM])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert json.loads(captured.err) == {
        "code": "invalid_arguments",
        "status": "error",
    }
    assert captured.out == ""


def test_recover_cli_both_selectors_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    bootstrapped = _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "recover",
                "--config",
                str(config_path),
                "--realm",
                _REALM,
                "--principal",
                str(bootstrapped.principal_id),
                "--label",
                "rescuer",
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert json.loads(captured.err) == {
        "code": "invalid_arguments",
        "status": "error",
    }


def test_recover_cli_neither_selector_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    with pytest.raises(SystemExit) as raised:
        cli.main(["recover", "--config", str(config_path), "--realm", _REALM])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert json.loads(captured.err) == {
        "code": "invalid_arguments",
        "status": "error",
    }


def test_bootstrap_cli_existing_realm_exits_four(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "realm_exists",
        "status": "error",
    }
    assert captured.out == ""


def test_recover_cli_unknown_realm_exits_four(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "recover",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            "rescuer",
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "realm_unknown",
        "status": "error",
    }
    assert captured.out == ""


def test_bootstrap_cli_ambiguous_commit_exits_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    monkeypatch.setattr(procedures_module, "_transactions", _ambiguous_transactions)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "commit_outcome_unknown",
        "status": "error",
    }
    assert captured.out == ""


def test_bootstrap_cli_unexpected_exception_is_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    sentinel = "raw-exception-secret-sentinel"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "bootstrap_realm", fail, raising=False)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "internal_error",
        "status": "error",
    }
    assert captured.out == ""
    assert sentinel not in captured.err


def test_bootstrap_cli_non_current_catalogue_exits_four_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "catalogue_not_current",
        "status": "error",
    }
    assert captured.out == ""
    assert "PRAGMA" not in captured.err


def test_bootstrap_cli_absent_catalogue_exits_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _config(data_path))

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "catalogue_unavailable",
        "status": "error",
    }
    assert captured.out == ""


def test_bootstrap_cli_instance_mismatch_exits_four(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    migrate_catalogue(_config(data_path, instance_id=INSTANCE_ID), lambda: NOW)
    mismatched_config = _config(data_path, instance_id=_OTHER_INSTANCE_ID)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, mismatched_config)

    result = cli.main(
        [
            "bootstrap",
            "--config",
            str(config_path),
            "--realm",
            _REALM,
            "--label",
            _LABEL,
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "instance_mismatch",
        "status": "error",
    }
    assert captured.out == ""


def test_bootstrap_cli_refuses_serving_instance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        result = cli.main(
            [
                "bootstrap",
                "--config",
                str(config_path),
                "--realm",
                _REALM,
                "--label",
                _LABEL,
            ]
        )
    finally:
        lease.release()

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.err) == {
        "code": "catalogue_unavailable",
        "status": "error",
    }
    assert captured.out == ""


@pytest.mark.parametrize("command", ["bootstrap", "recover"])
@pytest.mark.parametrize(
    "label",
    ["Local administrator", "", "1admin", "-admin", "admin-", "a" * 64, "admin\n"],
)
def test_cli_invalid_label_is_explicit_and_leaves_catalogue_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    label: str,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    if command == "recover":
        _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    with sqlite3.connect(config.paths.data / CATALOGUE_FILENAME) as connection:
        before = tuple(connection.iterdump())

    result = cli.main(
        [command, "--config", str(config_path), "--realm", _REALM, f"--label={label}"]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "error", "code": "invalid_label"}
    with sqlite3.connect(config.paths.data / CATALOGUE_FILENAME) as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("command", ["bootstrap", "recover"])
@pytest.mark.parametrize("label", ["a", "local-administrator", "a" * 63])
def test_cli_accepts_valid_label_boundaries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    label: str,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    if command == "recover":
        _bootstrap(config)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)

    result = cli.main(
        [command, "--config", str(config_path), "--realm", _REALM, "--label", label]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert TOKEN_PATTERN.fullmatch(payload["token"]) is not None
    with sqlite3.connect(config.paths.data / CATALOGUE_FILENAME) as connection:
        assert connection.execute(
            "SELECT label FROM principals WHERE principal_id = ?",
            (payload["principal_id"],),
        ).fetchone() == (label,)


@pytest.mark.parametrize("command", ["bootstrap", "recover"])
def test_cli_invalid_label_is_rejected_before_catalogue_lease(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = _migrated(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        result = cli.main(
            [
                command,
                "--config",
                str(config_path),
                "--realm",
                _REALM,
                "--label",
                "Local administrator",
            ]
        )
    finally:
        lease.release()
    captured = capsys.readouterr()
    assert result == 4
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "error", "code": "invalid_label"}
