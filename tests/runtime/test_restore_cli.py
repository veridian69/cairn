"""Slice 8 Task 2: the P-66 restore command.

The claims proven here, each a plan-named test: a full round trip
through backup and restore on a fresh directory yields a catalogue
``cairn verify`` passes and ``serve`` opens; a tampered member is
refused before any install; a bundle for a different ``instance_id`` is
refused per I-41 without touching the target; restore onto a non-empty
directory is refused without touching it; a manifest naming anything
but the two known members is refused, which is the path-traversal
guard.

Module-local fixtures for the reason the transport test modules record:
``tests`` has no package markers, so no ``conftest.py`` can be shared.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

import cairn.operations.restore as restore_module
import cairn.runtime.cli as cli
from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.evidence.attic import ATTIC_FILENAME, SqliteAttic
from cairn.evidence.delivery import deliver_evidence_outbox
from cairn.operations.backup import MANIFEST_FILENAME, BundleMember, create_backup
from cairn.operations.restore import RestoreError, restore_bundle
from cairn.runtime.composition import build_application
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    HttpConfig,
    PathConfig,
)
from cairn.runtime.lease import DataDirectoryLease, LeaseError

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_INSTANCE_ID = UUID("99999999-9999-4999-8999-999999999999")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
GRANT_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
REPO = {"kind": "repository", "identifier": "acme-repo"}
LOCK_NAME = ".cairn-instance.lock"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def make_config(
    base: Path,
    *,
    attic: bool = True,
    instance_id: UUID = INSTANCE_ID,
) -> CairnConfig:
    data = base / "data"
    credentials = base / "credentials"
    data.mkdir(parents=True, exist_ok=True)
    credentials.mkdir(parents=True, exist_ok=True)
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=instance_id,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
        attic=AtticConfig(enabled=attic),
    )


def write_config(path: Path, config: CairnConfig) -> None:
    path.write_text(
        "schema_version: cairn.config/v1\n"
        f"instance_id: {config.instance_id}\n"
        "mode: test\n"
        "http:\n"
        "  host: 127.0.0.1\n"
        "  port: 8000\n"
        "paths:\n"
        f"  data: {config.paths.data}\n"
        f"  credentials: {config.paths.credentials}\n"
        "attic:\n"
        f"  enabled: {'true' if config.attic.enabled else 'false'}\n",
        encoding="utf-8",
    )


def seed(config: CairnConfig) -> str:
    """Migrates and seeds the realm, one principal with a credential and
    one ingest-capable grant — the direct-row recipe of
    ``tests/transports/rest/v1/test_data_routes.py`` — returning the
    bearer token."""
    migrate_catalogue(config, lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (REALM, TS),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "workload", "w", TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, TS, None),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                _canonical([{"id": REPO["identifier"], "kind": REPO["kind"]}]),
                _canonical(["ingest"]),
                "restricted",
                _canonical(["internal", "public", "restricted"]),
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()
    return minted.text


async def rest_ingest(
    client: AsyncClient,
    token: str,
    *,
    body: str,
    payload: str,
) -> HTTPXResponse:
    return await client.post(
        "/v1/ingest",
        content=json.dumps(
            {
                "scope": {"realm": REALM, "segments": [REPO]},
                "classification": "internal",
                "source_type": "agent-claim",
                "facts": [{"body": body}],
                "requested_trust": "candidate",
                "evidence_payload": payload,
            }
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid4()),
        },
    )


async def populated_bundle(base: Path) -> Path:
    """A seeded, ingested, delivered instance backed up: the bundle
    carries both members and both delivered and pending outbox state."""
    config = make_config(base)
    token = seed(config)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://t",
            timeout=30.0,
        ) as client:
            for ordinal in range(3):
                response = await rest_ingest(
                    client,
                    token,
                    body=f"pre-backup fact {ordinal}",
                    payload=f"attic evidence pre-backup fact {ordinal}",
                )
                assert response.status_code == 200, response.text
    deliver_evidence_outbox(
        CatalogueTransactions(
            config.paths.data,
            writer_gate=threading.Lock(),
            clock=lambda: datetime.now(UTC),
            uuid_factory=uuid4,
        ),
        SqliteAttic(config.paths.data),
        clock=lambda: datetime.now(UTC),
    )
    result = create_backup(config, base / "backups", clock=lambda: datetime.now(UTC))
    return result.bundle_path


def migrated_bundle(base: Path) -> Path:
    """The fast variant: a freshly migrated catalogue, no Attic member."""
    config = make_config(base, attic=False)
    migrate_catalogue(config, lambda: NOW)
    result = create_backup(config, base / "backups", clock=lambda: NOW)
    return result.bundle_path


@pytest.mark.anyio
async def test_round_trip_yields_a_verifiable_serveable_instance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = await populated_bundle(tmp_path / "a")
    restored = make_config(tmp_path / "b")
    config_path = tmp_path / "restored.yaml"
    write_config(config_path, restored)

    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    captured = capsys.readouterr()
    assert result == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["operation"] == "restore"
    assert payload["instance_id"] == str(INSTANCE_ID)
    assert [member["name"] for member in payload["members"]] == [
        CATALOGUE_FILENAME,
        ATTIC_FILENAME,
    ]
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["audit_boundary"] == manifest["audit_boundary"]
    assert [member["name"] for member in manifest["members"]] == [
        CATALOGUE_FILENAME,
        ATTIC_FILENAME,
    ]
    with sqlite3.connect(restored.paths.data / ATTIC_FILENAME) as connection:
        restored_payloads = {
            row[0] for row in connection.execute("SELECT payload FROM payloads")
        }
    assert restored_payloads == {
        f"attic evidence pre-backup fact {ordinal}".encode() for ordinal in range(3)
    }

    assert cli.main(["verify", "--config", str(config_path)]) == 0, (
        capsys.readouterr().err
    )
    verify_payload = json.loads(capsys.readouterr().out)
    assert verify_payload["status"] == "ok"
    assert verify_payload["instance_id"] == str(INSTANCE_ID)
    assert verify_payload["fact_count"] == 3

    application = build_application(restored)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://t",
            timeout=30.0,
        ) as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 200, ready.text


@pytest.mark.anyio
async def test_a_tampered_member_is_refused_before_any_install(
    tmp_path: Path,
) -> None:
    bundle_path = await populated_bundle(tmp_path / "a")
    attic_member = bundle_path / ATTIC_FILENAME
    attic_member.write_bytes(attic_member.read_bytes() + b"tamper")
    restored = make_config(tmp_path / "b")

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(restored, bundle_path)

    assert refusal.value.code == "digest_mismatch"
    assert refusal.value.member == ATTIC_FILENAME
    # Never partial-installing: the lease file is the only thing the
    # refused restore may have created (the lease precedes the digest
    # pass by design, so a concurrent serve cannot slip in between).
    assert {entry.name for entry in restored.paths.data.iterdir()} <= {LOCK_NAME}


def test_a_foreign_instance_bundle_is_refused_untouched(tmp_path: Path) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    foreign = make_config(tmp_path / "b", instance_id=OTHER_INSTANCE_ID)
    config_path = tmp_path / "foreign.yaml"
    write_config(config_path, foreign)

    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    assert result == 4
    # The identity refusal precedes the lease, so the target is wholly
    # untouched — not even a lock file (I-41: no rebind).
    assert list(foreign.paths.data.iterdir()) == []


def test_a_foreign_instance_refusal_names_its_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    foreign = make_config(tmp_path / "b", instance_id=OTHER_INSTANCE_ID)

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(foreign, bundle_path)

    assert refusal.value.code == "instance_mismatch"
    capsys.readouterr()


def test_a_non_empty_data_directory_is_refused_untouched(tmp_path: Path) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    stray = target.paths.data / "existing.txt"
    stray.write_text("live state", encoding="utf-8")

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "data_directory_not_empty"
    assert [entry.name for entry in target.paths.data.iterdir()] == ["existing.txt"]
    assert stray.read_text(encoding="utf-8") == "live state"


def test_a_directory_populated_after_the_lease_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The emptiness refusal is repeated under the lease.

    Checking before the lease is the pinned order (the lease file is the
    first thing restore creates), but between the two anything at all may
    write to the directory — and ``O_EXCL`` would only catch a collision
    on the two member names. The second check closes the window.
    """
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    config_path = tmp_path / "target.yaml"
    write_config(config_path, target)

    class RacingLease(DataDirectoryLease):
        def acquire(self) -> None:
            super().acquire()
            (target.paths.data / "live-state.txt").write_text("racer", encoding="utf-8")

    monkeypatch.setattr(restore_module, "DataDirectoryLease", RacingLease)

    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    assert result == 4
    assert json.loads(capsys.readouterr().err) == {
        "code": "data_directory_not_empty",
        "recovery": "delete_data_directory_and_retry",
        "status": "error",
    }
    assert not (target.paths.data / CATALOGUE_FILENAME).exists()
    assert (target.paths.data / LOCK_NAME).is_file()


def test_lost_and_found_alone_is_tolerated(tmp_path: Path) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    (target.paths.data / "lost+found").mkdir()

    result = restore_bundle(target, bundle_path)

    assert result.instance_id == INSTANCE_ID
    installed = {entry.name for entry in target.paths.data.iterdir()}
    assert CATALOGUE_FILENAME in installed


def test_an_absent_data_directory_is_refused(tmp_path: Path) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    target.paths.data.rmdir()

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "data_directory_unavailable"


def test_a_manifest_naming_a_foreign_member_is_refused(tmp_path: Path) -> None:
    """The path-traversal guard: only the two known member names are ever
    installable, so a hostile manifest cannot reach outside the data
    directory."""
    bundle_path = migrated_bundle(tmp_path / "a")
    manifest_path = bundle_path / MANIFEST_FILENAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["members"][0]["name"] = "../evil.sqlite3"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    target = make_config(tmp_path / "b")

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "manifest_invalid"
    assert list(target.paths.data.iterdir()) == []
    assert not (tmp_path / "evil.sqlite3").exists()


def test_a_tampered_member_refusal_reaches_the_cli_with_its_member(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    catalogue_member = bundle_path / CATALOGUE_FILENAME
    catalogue_member.write_bytes(catalogue_member.read_bytes() + b"x")
    target = make_config(tmp_path / "b")
    config_path = tmp_path / "target.yaml"
    write_config(config_path, target)

    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "digest_mismatch",
        "member": CATALOGUE_FILENAME,
        # Nothing was installed, but the lease file was created, and a
        # retry onto a directory holding it refuses as non-empty — so the
        # refusal carries P-66's recovery all the same.
        "recovery": "delete_data_directory_and_retry",
        "status": "error",
    }
    assert captured.out == ""


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("not json", "manifest_invalid"),
        ("[]", "manifest_invalid"),
        (json.dumps({"schema_version": "wrong"}), "manifest_invalid"),
        (json.dumps({"schema_version": "cairn.backup/v1"}), "manifest_invalid"),
        (
            json.dumps(
                {"schema_version": "cairn.backup/v1", "instance_id": "not-a-uuid"}
            ),
            "manifest_invalid",
        ),
    ],
)
def test_manifest_refusals_are_typed(
    tmp_path: Path,
    document: str,
    expected: str,
) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text(document, encoding="utf-8")

    with pytest.raises(RestoreError) as refusal:
        restore_module._load_manifest(tmp_path)

    assert refusal.value.code == expected


_ABSENT = object()


def _provenance_manifest(key: str, value: object) -> str:
    document: dict[str, object] = {
        "schema_version": "cairn.backup/v1",
        "instance_id": str(INSTANCE_ID),
        "created_at": TS,
        "catalogue_schema_version": CURRENT_SCHEMA_VERSION,
        "audit_boundary": [],
        "members": [{"name": CATALOGUE_FILENAME, "bytes": 0, "sha256": "0" * 64}],
    }
    if value is _ABSENT:
        del document[key]
    else:
        document[key] = value
    return json.dumps(document)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("created_at", _ABSENT),
        ("created_at", False),
        ("created_at", "not a timestamp"),
        ("catalogue_schema_version", _ABSENT),
        ("catalogue_schema_version", False),
        ("catalogue_schema_version", CURRENT_SCHEMA_VERSION + 1),
    ],
)
def test_manifest_provenance_fields_are_validated(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    """P-65 writes ``created_at`` and ``catalogue_schema_version``; a
    field no reader validates is decorative. ``False`` is in the table
    because ``True == 1`` in Python and a bool must not pass for a
    schema version."""
    (tmp_path / MANIFEST_FILENAME).write_text(
        _provenance_manifest(key, value), encoding="utf-8"
    )

    with pytest.raises(RestoreError) as refusal:
        restore_module._load_manifest(tmp_path)

    assert refusal.value.code == "manifest_invalid"


def test_a_current_manifest_loads(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text(
        _provenance_manifest("created_at", TS), encoding="utf-8"
    )

    manifest = restore_module._load_manifest(tmp_path)

    assert manifest.instance_id == INSTANCE_ID


def test_manifest_refuses_an_unreadable_or_absent_bundle(tmp_path: Path) -> None:
    with pytest.raises(RestoreError) as absent:
        restore_module._load_manifest(tmp_path / "absent")
    assert absent.value.code == "bundle_unreadable"

    with pytest.raises(RestoreError) as missing_manifest:
        restore_module._load_manifest(tmp_path)
    assert missing_manifest.value.code == "bundle_unreadable"


@pytest.mark.parametrize(
    "members",
    [
        None,
        [],
        ["not-an-object"],
        [{"name": CATALOGUE_FILENAME}],
        [{"name": ATTIC_FILENAME, "bytes": 0, "sha256": "0" * 64}],
        [
            {"name": CATALOGUE_FILENAME, "bytes": 0, "sha256": "0" * 64},
            {"name": CATALOGUE_FILENAME, "bytes": 0, "sha256": "0" * 64},
        ],
    ],
)
def test_member_manifest_refusals_are_typed(members: object) -> None:
    with pytest.raises(RestoreError) as refusal:
        restore_module._validated_members(members)
    assert refusal.value.code == "manifest_invalid"


@pytest.mark.parametrize(
    "boundary",
    [None, ["not-an-object"], [{"chain_kind": "instance"}]],
)
def test_audit_boundary_manifest_refusals_are_typed(boundary: object) -> None:
    with pytest.raises(RestoreError) as refusal:
        restore_module._validated_boundary(boundary)
    assert refusal.value.code == "manifest_invalid"


def _bundle_member(bundle: Path, payload: bytes) -> BundleMember:
    """A real member entry for ``payload``, written into ``bundle``."""
    (bundle / CATALOGUE_FILENAME).write_bytes(payload)
    return BundleMember(
        CATALOGUE_FILENAME,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )


def test_a_missing_member_is_a_digest_mismatch(tmp_path: Path) -> None:
    member = BundleMember(CATALOGUE_FILENAME, 0, "0" * 64)
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(tmp_path, data, member)

    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_an_unstatable_member_is_a_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor that cannot be classified is hostile bundle input,
    not an internal error, and must create no staging file."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    member = _bundle_member(bundle, b"catalogue")
    data = tmp_path / "data"
    data.mkdir()

    def refusing_fstat(fd: int) -> os.stat_result:
        raise OSError("injected fstat failure")

    monkeypatch.setattr(os, "fstat", refusing_fstat)
    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(bundle, data, member)

    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_a_fifo_member_is_refused_before_it_is_read(tmp_path: Path) -> None:
    """A named pipe is not a bounded bundle member and must never reach
    the copy loop. The writer makes the old blocking open reproducible;
    cleanup supplies a reader if the fixed implementation rejects the
    descriptor before the writer has connected."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    fifo = bundle / CATALOGUE_FILENAME
    os.mkfifo(fifo, mode=0o660)
    payload = b"not a regular bundle member"
    member = BundleMember(
        CATALOGUE_FILENAME,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )
    data = tmp_path / "data"
    data.mkdir()
    writer_errors: list[BaseException] = []

    def write_fifo() -> None:
        try:
            with fifo.open("wb", buffering=0) as stream:
                stream.write(payload)
        except BrokenPipeError:
            pass
        except BaseException as error:
            writer_errors.append(error)

    writer = threading.Thread(target=write_fifo, daemon=True)
    writer.start()
    try:
        with pytest.raises(RestoreError) as refusal:
            restore_module._stage_member(bundle, data, member)
    finally:
        reader_fd: int | None = None
        if writer.is_alive():
            reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        writer.join(timeout=1)
        if reader_fd is not None:
            os.close(reader_fd)

    assert not writer.is_alive()
    assert writer_errors == []
    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_an_oversized_member_is_refused_after_one_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's byte count bounds the copy. One extra byte proves
    a mismatch; Cairn must not stream the rest of a hostile large file
    into the replacement volume before refusing it."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    payload = b"oversized"
    (bundle / CATALOGUE_FILENAME).write_bytes(payload)
    member = BundleMember(CATALOGUE_FILENAME, 1, hashlib.sha256(b"o").hexdigest())
    data = tmp_path / "data"
    data.mkdir()
    real_read = os.read
    requested_sizes: list[int] = []

    def recording_read(fd: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", recording_read)
    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(bundle, data, member)

    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert requested_sizes == [2]
    assert list(data.iterdir()) == []


def test_a_same_size_member_with_the_wrong_digest_is_refused(
    tmp_path: Path,
) -> None:
    """Matching the manifest byte count is insufficient: a wrong digest
    must remove the completed staging file and refuse the member."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    payload = b"catalogue"
    (bundle / CATALOGUE_FILENAME).write_bytes(payload)
    member = BundleMember(CATALOGUE_FILENAME, len(payload), "0" * 64)
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(bundle, data, member)

    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_an_unreadable_member_is_a_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that fails mid-digest is a refusal about the member, not an
    internal error: the bundle, not Cairn, is what went wrong. The
    staging file it leaves behind is unlinked."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    member = _bundle_member(bundle, b"catalogue")
    data = tmp_path / "data"
    data.mkdir()

    def refusing_read(fd: int, size: int) -> bytes:
        raise OSError("injected read failure")

    monkeypatch.setattr(os, "read", refusing_read)
    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(bundle, data, member)

    assert (refusal.value.code, refusal.value.member) == (
        "digest_mismatch",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_in_place_modification_after_the_digest_cannot_be_installed(
    tmp_path: Path,
) -> None:
    """The verified bytes are the installed bytes.

    The digest is computed over the staged copy as it is written, and
    the bundle member is never read again — so mutating the member's
    inode in place after the hash, the window a held descriptor alone
    does not close, changes nothing about what lands.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    verified_bytes = b"the verified catalogue bytes"
    member = _bundle_member(bundle, verified_bytes)
    data = tmp_path / "data"
    data.mkdir()

    staged = restore_module._stage_member(bundle, data, member)
    # Mutate the member's inode in place — no unlink, same file.
    with (bundle / CATALOGUE_FILENAME).open("r+b") as writer:
        writer.write(b"changed-in-place")
    os.link(staged, data / CATALOGUE_FILENAME)
    os.unlink(staged)

    assert (data / CATALOGUE_FILENAME).read_bytes() == verified_bytes


def test_install_refusals_name_the_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    member = _bundle_member(bundle, b"catalogue")
    data = tmp_path / "data"
    data.mkdir()
    (data / f"{CATALOGUE_FILENAME}{restore_module._STAGING_SUFFIX}").write_bytes(
        b"stale staging file"
    )

    # An occupied staging name refuses rather than clobbering.
    with pytest.raises(RestoreError) as occupied:
        restore_module._stage_member(bundle, data, member)
    assert (occupied.value.code, occupied.value.member) == (
        "install_failed",
        CATALOGUE_FILENAME,
    )

    (data / f"{CATALOGUE_FILENAME}{restore_module._STAGING_SUFFIX}").unlink()
    monkeypatch.setattr(os, "write", lambda fd, payload: len(payload) - 1)
    with pytest.raises(RestoreError) as short_write:
        restore_module._stage_member(bundle, data, member)
    assert (short_write.value.code, short_write.value.member) == (
        "install_failed",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_an_occupied_final_name_refuses_and_cleans_the_staging(
    tmp_path: Path,
) -> None:
    """The link is the install, and it refuses an occupied name — the
    no-clobber property O_EXCL used to provide — leaving no staging
    residue behind."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    member = _bundle_member(bundle, b"catalogue")
    data = tmp_path / "data"
    data.mkdir()
    (data / CATALOGUE_FILENAME).write_bytes(b"occupied")

    with pytest.raises(RestoreError) as refusal:
        restore_module._install_members(bundle, data, (member,))

    assert (refusal.value.code, refusal.value.member) == (
        "install_failed",
        CATALOGUE_FILENAME,
    )
    assert {entry.name for entry in data.iterdir()} == {CATALOGUE_FILENAME}
    assert (data / CATALOGUE_FILENAME).read_bytes() == b"occupied"


def test_install_maps_finalisation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    member = _bundle_member(bundle, b"catalogue")
    data = tmp_path / "data"
    data.mkdir()

    def refusing_fchmod(fd: int, mode: int) -> None:
        raise PermissionError("injected finalisation failure")

    monkeypatch.setattr(os, "fchmod", refusing_fchmod)
    with pytest.raises(RestoreError) as refusal:
        restore_module._stage_member(bundle, data, member)
    assert (refusal.value.code, refusal.value.member) == (
        "install_failed",
        CATALOGUE_FILENAME,
    )
    assert list(data.iterdir()) == []


def test_an_install_failure_carries_the_recovery_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P-66 step 7: a refusal that leaves the target dirty says how to
    recover. An install failure can leave a partly written member, and it
    always leaves the lease file, so the next attempt refuses the
    directory as non-empty until it is deleted."""
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    config_path = tmp_path / "target.yaml"
    write_config(config_path, target)

    def refusing_install(bundle: Path, data_path: Path, members: Any) -> None:
        raise RestoreError("install_failed", CATALOGUE_FILENAME)

    monkeypatch.setattr(restore_module, "_install_members", refusing_install)
    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "install_failed",
        "member": CATALOGUE_FILENAME,
        "recovery": "delete_data_directory_and_retry",
        "status": "error",
    }


def test_restore_refuses_an_audit_boundary_different_from_the_catalogue(
    tmp_path: Path,
) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    manifest_path = bundle_path / MANIFEST_FILENAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["audit_boundary"] = []
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    target = make_config(tmp_path / "b")

    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "audit_boundary_mismatch"


def test_staging_cleanup_never_replaces_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup is a courtesy, not a step: a staging file that cannot be
    unlinked must not turn the refusal in flight into an OSError."""
    target = tmp_path / "staging"
    target.write_bytes(b"residue")

    def refusing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("injected unlink failure")

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    restore_module._unlink_quietly(target)

    monkeypatch.undo()
    assert target.read_bytes() == b"residue"


def test_an_unreadable_installed_boundary_is_a_typed_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure reading the installed catalogue's heads is post-install:
    it must surface as a typed refusal carrying the recovery
    instruction, not escape as a raw storage error without one."""
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")
    config_path = tmp_path / "target.yaml"
    write_config(config_path, target)

    def refusing_read(data_path: Path) -> Any:
        raise CatalogueStorageError("catalogue_unavailable")

    monkeypatch.setattr(restore_module, "read_connection", refusing_read)
    result = cli.main(
        ["restore", "--config", str(config_path), "--bundle", str(bundle_path)]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "audit_boundary_unreadable",
        "recovery": "delete_data_directory_and_retry",
        "status": "error",
    }


def test_a_release_failure_after_success_is_typed_and_carries_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The members are installed and verified, but the command failed,
    and a retry would refuse the non-empty directory — so even this
    failure names the recovery."""
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")

    class UnreleasableLease(DataDirectoryLease):
        def release(self) -> None:
            super().release()
            raise LeaseError("data_unavailable")

    monkeypatch.setattr(restore_module, "DataDirectoryLease", UnreleasableLease)
    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "lease_release_failed"
    assert refusal.value.code in cli._DIRTY_TARGET_RESTORE_CODES


def test_a_release_failure_does_not_mask_the_refusal_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal in flight is the honest signal and already carries
    its own recovery; a lease error on top of it must not replace it."""
    bundle_path = migrated_bundle(tmp_path / "a")
    catalogue_member = bundle_path / CATALOGUE_FILENAME
    catalogue_member.write_bytes(catalogue_member.read_bytes() + b"tamper")
    target = make_config(tmp_path / "b")

    class UnreleasableLease(DataDirectoryLease):
        def release(self) -> None:
            super().release()
            raise LeaseError("data_unavailable")

    monkeypatch.setattr(restore_module, "DataDirectoryLease", UnreleasableLease)
    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "digest_mismatch"
    assert refusal.value.member == CATALOGUE_FILENAME


def test_a_directory_fsync_failure_after_install_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = migrated_bundle(tmp_path / "a")
    target = make_config(tmp_path / "b")

    def refusing_fsync(path: Path) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(restore_module, "_fsync_directory", refusing_fsync)
    with pytest.raises(RestoreError) as refusal:
        restore_bundle(target, bundle_path)

    assert refusal.value.code == "install_failed"
    assert refusal.value.code in cli._DIRTY_TARGET_RESTORE_CODES


def test_attic_integrity_refusals_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RestoreError) as open_refusal:
        restore_module._verify_attic(tmp_path / "absent.sqlite3")
    assert open_refusal.value.code == "attic_integrity_failed"

    class BrokenIntegrityConnection:
        def execute(self, statement: str) -> Any:
            class Cursor:
                def fetchall(self) -> list[tuple[str]]:
                    return [("not ok",)]

            return Cursor()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: BrokenIntegrityConnection()
    )
    with pytest.raises(RestoreError) as integrity_refusal:
        restore_module._verify_attic(tmp_path / ATTIC_FILENAME)
    assert integrity_refusal.value.code == "attic_integrity_failed"

    class FtsFailureConnection:
        def execute(self, statement: str) -> Any:
            if statement == "PRAGMA integrity_check":

                class Cursor:
                    def fetchall(self) -> list[tuple[str]]:
                        return [("ok",)]

                return Cursor()
            raise sqlite3.DatabaseError("injected FTS integrity failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: FtsFailureConnection()
    )
    with pytest.raises(RestoreError) as fts_refusal:
        restore_module._verify_attic(tmp_path / ATTIC_FILENAME)
    assert fts_refusal.value.code == "attic_integrity_failed"
