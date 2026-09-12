"""Slice 8 Task 1: the P-65 backup command and its mutation barrier.

The bundle claims proven here: a backup taken under concurrent mutation
load is one instant (its catalogue's audit heads equal the manifest's
declared boundary), the manifest digests verify, the Attic member is
never behind the catalogue's outbox state, and credentials and derived
state appear nowhere in the bundle. The barrier claim is proven from
both sides: a mutation attempted mid-barrier surfaces the typed
retryable failure through REST as 503 and through MCP as the same
``dependency_unavailable`` envelope — never an internal error — and
succeeds on retry; a backup attempted against a held write lock reports
the same code from the CLI.

Module-local fixtures for the reason the transport test modules record:
``tests`` has no package markers, so no ``conftest.py`` can be shared.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

import cairn.catalogue.transactions as transaction_module
import cairn.operations.backup as backup_module
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
from cairn.operations.backup import (
    MANIFEST_FILENAME,
    BackupError,
    create_backup,
)
from cairn.runtime import cli
from cairn.runtime.composition import build_application
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    HttpConfig,
    PathConfig,
)
from cairn.transports.v1.paths import MCP_MOUNT_PATH

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

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

PAUSED_BACKUP_PROCESS = """
import sys
import time
from pathlib import Path

import cairn.operations.backup as backup_module
from cairn.runtime import cli

marker = Path(sys.argv[1])
release = Path(sys.argv[2])
real_copy_member = backup_module._copy_member

def paused_copy(source_path, target_path):
    marker.touch()
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("parent did not release the backup copy")
        time.sleep(0.01)
    real_copy_member(source_path, target_path)

backup_module._copy_member = paused_copy
raise SystemExit(cli.main(sys.argv[3:]))
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def make_config(
    tmp_path: Path,
    *,
    attic: bool = True,
    instance_id: UUID = INSTANCE_ID,
) -> CairnConfig:
    data = tmp_path / "data"
    credentials = tmp_path / "credentials"
    data.mkdir(exist_ok=True)
    credentials.mkdir(exist_ok=True)
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
    idempotency_key: str,
    body: str,
    payload: str | None = None,
) -> HTTPXResponse:
    request: dict[str, object] = {
        "scope": {"realm": REALM, "segments": [REPO]},
        "classification": "internal",
        "source_type": "agent-claim",
        "facts": [{"body": body}],
        "requested_trust": "candidate",
    }
    if payload is not None:
        request["evidence_payload"] = payload
    return await client.post(
        "/v1/ingest",
        content=json.dumps(request),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
    )


async def mcp_ingest(
    client: AsyncClient,
    token: str,
    *,
    idempotency_key: str,
    body: str,
) -> dict[str, Any]:
    """One ``tools/call`` ingest frame, returning the ``CallToolResult``."""
    response = await client.post(
        MCP_MOUNT_PATH,
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ingest",
                    "arguments": {
                        "scope": {"realm": REALM, "segments": [REPO]},
                        "classification": "internal",
                        "source_type": "agent-claim",
                        "requested_trust": "candidate",
                        "facts": [{"body": body, "valid_from": None, "valid_to": None}],
                        "observed_at": None,
                        "metadata": None,
                        "evidence_payload": None,
                        "idempotency_key": idempotency_key,
                    },
                },
            }
        ),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    frame = response.json()
    assert "error" not in frame, frame
    result: dict[str, Any] = frame["result"]
    return result


def read_manifest(bundle_path: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (bundle_path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    return manifest


def member_digests_verify(bundle_path: Path, manifest: dict[str, Any]) -> bool:
    for member in manifest["members"]:
        raw = (bundle_path / member["name"]).read_bytes()
        if len(raw) != member["bytes"]:
            return False
        if hashlib.sha256(raw).hexdigest() != member["sha256"]:
            return False
    return True


def wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


def bundle_heads(bundle_path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(bundle_path / CATALOGUE_FILENAME) as connection:
        rows = connection.execute(
            "SELECT chain_kind, chain_identity, last_sequence, last_hash "
            "FROM audit_heads ORDER BY chain_kind, chain_identity"
        ).fetchall()
    return [
        {
            "chain_kind": kind,
            "chain_identity": identity,
            "sequence": sequence,
            "head_digest": head.hex(),
        }
        for kind, identity, sequence, head in rows
    ]


@pytest.mark.anyio
async def test_backup_under_mutation_load_is_one_declared_instant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ingest load; the bundle's catalogue must carry exactly
    the audit heads the manifest declares, pass ``integrity_check``, and
    have every member digest verify."""
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)
    output = tmp_path / "backups"

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://t",
            timeout=30.0,
        ) as client:
            for ordinal in range(4):
                warmed = await rest_ingest(
                    client,
                    token,
                    idempotency_key=str(uuid4()),
                    body=f"warm fact {ordinal}",
                    payload=json.dumps({"run": ordinal}),
                )
                assert warmed.status_code == 200, warmed.text

            copy_entered = threading.Event()
            release_copy = threading.Event()
            mutation_begin_attempted = threading.Event()
            mutation_call = threading.local()
            statuses: list[int] = []
            backup_results: list[object] = []

            real_copy_member = backup_module._copy_member
            real_mutate = CatalogueTransactions.mutate_idempotent
            real_begin_immediate = transaction_module._begin_immediate

            def paused_copy(source_path: Path, target_path: Path) -> None:
                copy_entered.set()
                assert release_copy.wait(5), "test did not release the backup copy"
                real_copy_member(source_path, target_path)

            def observed_mutate(
                transactions: CatalogueTransactions,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                mutation_call.active = True
                try:
                    return real_mutate(transactions, *args, **kwargs)
                finally:
                    mutation_call.active = False

            def observed_begin(connection: sqlite3.Connection) -> None:
                if getattr(mutation_call, "active", False):
                    mutation_begin_attempted.set()
                real_begin_immediate(connection)

            monkeypatch.setattr(backup_module, "_copy_member", paused_copy)
            monkeypatch.setattr(
                CatalogueTransactions, "mutate_idempotent", observed_mutate
            )
            monkeypatch.setattr(transaction_module, "_begin_immediate", observed_begin)

            async def mutate_under_barrier() -> None:
                response = await rest_ingest(
                    client,
                    token,
                    idempotency_key=str(uuid4()),
                    body=f"load fact {uuid4()}",
                    payload=json.dumps({"load": True}),
                )
                statuses.append(response.status_code)

            async def take_backup() -> None:
                backup_results.append(
                    await anyio.to_thread.run_sync(
                        partial(
                            create_backup,
                            config,
                            output,
                            clock=lambda: datetime.now(UTC),
                        )
                    )
                )

            async with anyio.create_task_group() as load:
                load.start_soon(take_backup)
                assert await anyio.to_thread.run_sync(partial(copy_entered.wait, 5)), (
                    "backup never entered its first member copy"
                )
                for _ in range(4):
                    load.start_soon(mutate_under_barrier)
                assert await anyio.to_thread.run_sync(
                    partial(mutation_begin_attempted.wait, 5)
                ), "no mutation attempted BEGIN IMMEDIATE under the barrier"
                release_copy.set()

    assert len(backup_results) == 1
    result = backup_results[0]
    assert isinstance(result, backup_module.BackupResult)
    assert statuses == [200] * 4
    manifest = read_manifest(result.bundle_path)
    assert manifest["schema_version"] == "cairn.backup/v1"
    assert manifest["instance_id"] == str(INSTANCE_ID)
    assert manifest["catalogue_schema_version"] == CURRENT_SCHEMA_VERSION
    assert bundle_heads(result.bundle_path) == manifest["audit_boundary"]
    assert member_digests_verify(result.bundle_path, manifest)
    assert result.barrier_ms > 0
    with sqlite3.connect(result.bundle_path / CATALOGUE_FILENAME) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.anyio
async def test_mutation_mid_barrier_is_typed_retryable_and_succeeds_on_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The declared trade-off, observed from every side at once: while
    the write lock is held, a REST mutation answers 503
    ``dependency_unavailable`` with the after-delay retry class — never
    500 — an MCP mutation answers the same failure envelope, a backup
    attempt reports the same code from the CLI, and the barred REST
    request succeeds when retried with its own idempotency key."""
    config = make_config(tmp_path, attic=False)
    token = seed(config)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)
    application = build_application(config)
    rest_key = "aaaaaaaa-1111-4111-8111-111111111111"

    barrier = sqlite3.connect(
        config.paths.data / CATALOGUE_FILENAME,
        timeout=5.0,
        isolation_level=None,
    )
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://t",
            timeout=60.0,
        ) as client:
            barrier.execute("BEGIN IMMEDIATE")
            try:
                barred_rest: list[HTTPXResponse] = []
                backup_exit: list[int] = []
                backup_process: list[subprocess.CompletedProcess[str]] = []

                async def barred_rest_probe() -> None:
                    barred_rest.append(
                        await rest_ingest(
                            client,
                            token,
                            idempotency_key=rest_key,
                            body="a fact barred mid-barrier",
                        )
                    )

                async def barred_backup_probe() -> None:
                    backup_exit.append(
                        await anyio.to_thread.run_sync(
                            cli.main,
                            [
                                "backup",
                                "--config",
                                str(config_path),
                                "--output",
                                str(tmp_path / "backups"),
                            ],
                        )
                    )

                async def barred_backup_process_probe() -> None:
                    backup_process.append(
                        await anyio.to_thread.run_sync(
                            partial(
                                subprocess.run,
                                [
                                    sys.executable,
                                    "-m",
                                    "cairn",
                                    "backup",
                                    "--config",
                                    str(config_path),
                                    "--output",
                                    str(tmp_path / "process-backups"),
                                ],
                                capture_output=True,
                                text=True,
                                check=False,
                                timeout=15,
                            )
                        )
                    )

                # Concurrent on purpose: each contender waits out its own
                # 5,000 ms busy timeout, so overlapping them keeps the
                # test near one timeout rather than a sum of them.
                async with anyio.create_task_group() as probes:
                    probes.start_soon(barred_rest_probe)
                    probes.start_soon(barred_backup_probe)
                    probes.start_soon(barred_backup_process_probe)

                response = barred_rest[0]
                assert response.status_code == 503, response.text
                failure = response.json()["failure"]
                assert failure["code"] == "dependency_unavailable"
                assert failure["retry"] == "after-delay"
                assert response.headers["Retry-After"] == "1"

                assert backup_exit == [3]
                assert len(backup_process) == 1
                assert backup_process[0].returncode == 3
                assert json.loads(backup_process[0].stderr) == {
                    "code": "dependency_unavailable",
                    "status": "error",
                }
                assert backup_process[0].stdout == ""
                # The live application's safe log shares stderr with the
                # CLI here, so the error document is a line among log
                # lines rather than the whole stream.
                captured = capsys.readouterr()
                assert {
                    "code": "dependency_unavailable",
                    "status": "error",
                } in [json.loads(line) for line in captured.err.splitlines() if line]

                mcp_result = await mcp_ingest(
                    client,
                    token,
                    idempotency_key="bbbbbbbb-2222-4222-8222-222222222222",
                    body="a tool call barred mid-barrier",
                )
                assert mcp_result["isError"] is True
                assert "structuredContent" not in mcp_result
                mcp_failure = json.loads(mcp_result["content"][0]["text"])["failure"]
                assert mcp_failure["code"] == "dependency_unavailable"
                assert mcp_failure["retry"] == "after-delay"
            finally:
                barrier.execute("ROLLBACK")
                barrier.close()

            retried = await rest_ingest(
                client,
                token,
                idempotency_key=rest_key,
                body="a fact barred mid-barrier",
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["outcome"] == "committed"


@pytest.mark.anyio
async def test_backup_subprocess_barrier_blocks_a_live_mutation(
    tmp_path: Path,
) -> None:
    """The deployed direction: a second-process backup owns the barrier;
    the live server reports typed contention, then accepts the retry."""
    config = make_config(tmp_path, attic=False)
    token = seed(config)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)
    application = build_application(config)
    marker = tmp_path / "backup-holds-barrier"
    release = tmp_path / "release-backup"
    rest_key = "cccccccc-3333-4333-8333-333333333333"

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://t",
            timeout=30.0,
        ) as client:
            process = await anyio.to_thread.run_sync(
                partial(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "-c",
                        PAUSED_BACKUP_PROCESS,
                        str(marker),
                        str(release),
                        "backup",
                        "--config",
                        str(config_path),
                        "--output",
                        str(tmp_path / "process-backups"),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            try:
                assert await anyio.to_thread.run_sync(
                    partial(wait_for_path, marker, 5)
                ), "backup subprocess never acquired the mutation barrier"
                barred = await rest_ingest(
                    client,
                    token,
                    idempotency_key=rest_key,
                    body="a fact barred by the backup subprocess",
                )
                assert barred.status_code == 503, barred.text
                failure = barred.json()["failure"]
                assert failure["code"] == "dependency_unavailable"
                assert failure["retry"] == "after-delay"
                assert barred.headers["Retry-After"] == "1"
            finally:
                release.touch()
                stdout, stderr = await anyio.to_thread.run_sync(
                    partial(process.communicate, timeout=15)
                )

            assert process.returncode == 0
            assert json.loads(stdout)["operation"] == "backup"
            assert stderr == ""

            retried = await rest_ingest(
                client,
                token,
                idempotency_key=rest_key,
                body="a fact barred by the backup subprocess",
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["outcome"] == "committed"


@pytest.mark.anyio
async def test_bundle_members_are_closed_and_attic_is_never_behind(
    tmp_path: Path,
) -> None:
    """The bundle holds exactly the declared members; every evidence row
    the catalogue records as delivered (its outbox row gone) has its
    payload in the Attic member; and no credential material appears in
    any member."""
    config = make_config(tmp_path)
    token = seed(config)
    output = tmp_path / "backups"

    async def ingest_batch(bodies: list[str]) -> None:
        application = build_application(config)
        async with LifespanManager(application):
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://t",
                timeout=30.0,
            ) as client:
                for body in bodies:
                    response = await rest_ingest(
                        client,
                        token,
                        idempotency_key=str(uuid4()),
                        body=body,
                        payload=json.dumps({"body": body}),
                    )
                    assert response.status_code == 200, response.text

    await ingest_batch(["delivered one", "delivered two"])
    # Drain as the server's loop would, then ingest more without another
    # drain: the catalogue now records both delivered and pending outbox
    # state, which is what the Attic-order claim is about.
    transactions = CatalogueTransactions(
        config.paths.data,
        writer_gate=threading.Lock(),
        clock=lambda: datetime.now(UTC),
        uuid_factory=uuid4,
    )
    deliver_evidence_outbox(
        transactions,
        SqliteAttic(config.paths.data),
        clock=lambda: datetime.now(UTC),
    )
    await ingest_batch(["pending one", "pending two"])

    result = create_backup(config, output, clock=lambda: datetime.now(UTC))

    listed = sorted(entry.name for entry in result.bundle_path.iterdir())
    assert listed == [ATTIC_FILENAME, CATALOGUE_FILENAME, MANIFEST_FILENAME]

    with sqlite3.connect(result.bundle_path / CATALOGUE_FILENAME) as connection:
        evidence_ids = {
            row[0]
            for row in connection.execute(
                "SELECT evidence_id FROM evidence_records"
            ).fetchall()
        }
        pending_ids = {
            row[0]
            for row in connection.execute(
                "SELECT evidence_id FROM evidence_outbox"
            ).fetchall()
        }
    with sqlite3.connect(result.bundle_path / ATTIC_FILENAME) as connection:
        attic_ids = {
            row[0]
            for row in connection.execute("SELECT evidence_id FROM payloads").fetchall()
        }
    delivered_ids = evidence_ids - pending_ids
    assert delivered_ids, "the drain delivered nothing; the claim was not exercised"
    assert pending_ids, "no pending outbox state; the claim was not exercised"
    assert delivered_ids <= attic_ids
    assert attic_ids <= evidence_ids

    token_bytes = token.encode()
    for name in listed:
        assert token_bytes not in (result.bundle_path / name).read_bytes()


def test_backup_refuses_a_foreign_instance(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    foreign = make_config(tmp_path, instance_id=OTHER_INSTANCE_ID)

    with pytest.raises(BackupError) as refusal:
        create_backup(foreign, tmp_path / "backups", clock=lambda: NOW)
    assert refusal.value.code == "instance_mismatch"


def test_backup_refuses_a_catalogue_with_the_wrong_storage_identity(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    with sqlite3.connect(config.paths.data / CATALOGUE_FILENAME) as connection:
        connection.execute("PRAGMA application_id = 0")

    with pytest.raises(CatalogueStorageError) as refusal:
        create_backup(config, tmp_path / "backups", clock=lambda: NOW)
    assert refusal.value.code == "catalogue_identity_mismatch"


def test_backup_refuses_a_malformed_audit_boundary(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    with sqlite3.connect(config.paths.data / CATALOGUE_FILENAME) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE audit_heads SET last_hash = ? WHERE chain_kind = 'instance'",
            (b"not-a-sha-256-digest",),
        )

    with pytest.raises(BackupError) as refusal:
        create_backup(config, tmp_path / "backups", clock=lambda: NOW)
    assert refusal.value.code == "audit_boundary_invalid"


def test_backup_refuses_an_existing_bundle_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path, attic=False)
    migrate_catalogue(config, lambda: NOW)
    output = tmp_path / "backups"
    output.mkdir()
    (output / f"cairn-backup-{INSTANCE_ID}-20260812T120000Z").mkdir()

    with pytest.raises(BackupError) as refusal:
        create_backup(config, output, clock=lambda: NOW)
    assert refusal.value.code == "bundle_exists"


def test_backup_refuses_an_unusable_output_path(tmp_path: Path) -> None:
    config = make_config(tmp_path, attic=False)
    migrate_catalogue(config, lambda: NOW)
    output = tmp_path / "not-a-directory"
    output.write_text("occupied", encoding="utf-8")

    with pytest.raises(BackupError) as refusal:
        create_backup(config, output, clock=lambda: NOW)
    assert refusal.value.code == "output_unavailable"


def test_backup_maps_bundle_directory_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path, attic=False)
    migrate_catalogue(config, lambda: NOW)
    output = tmp_path / "backups"
    output.mkdir()
    real_mkdir = Path.mkdir

    def refusing_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.parent == output:
            raise PermissionError("injected bundle-directory refusal")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refusing_mkdir)
    with pytest.raises(BackupError) as refusal:
        create_backup(config, output, clock=lambda: NOW)
    assert refusal.value.code == "output_unavailable"


def test_member_copy_maps_source_open_failure(tmp_path: Path) -> None:
    with pytest.raises(BackupError) as refusal:
        backup_module._copy_member(
            tmp_path / "absent.sqlite3",
            tmp_path / "copied.sqlite3",
        )
    assert refusal.value.code == "member_copy_failed"


def test_member_copy_maps_online_backup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect
    connect_count = 0

    class FailingSource:
        def backup(self, target: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("injected online-backup failure")

        def close(self) -> None:
            pass

    def injected_connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_count
        connect_count += 1
        if connect_count == 1:
            return FailingSource()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", injected_connect)
    with pytest.raises(BackupError) as refusal:
        backup_module._copy_member(
            tmp_path / "source.sqlite3",
            tmp_path / "copied.sqlite3",
        )
    assert refusal.value.code == "member_copy_failed"


def test_member_copy_maps_finalisation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE member (value INTEGER) STRICT")

    def refusing_chmod(path: Path, mode: int) -> None:
        raise PermissionError("injected chmod failure")

    monkeypatch.setattr(os, "chmod", refusing_chmod)
    with pytest.raises(BackupError) as refusal:
        backup_module._copy_member(source, tmp_path / "copied.sqlite3")
    assert refusal.value.code == "member_copy_failed"


def test_manifest_refuses_a_short_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def short_write(fd: int, payload: bytes) -> int:
        return len(payload) - 1

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(BackupError) as refusal:
        backup_module._write_manifest(
            tmp_path,
            instance_id=INSTANCE_ID,
            created_at=NOW,
            boundary=(),
            members=(),
        )
    assert refusal.value.code == "manifest_write_failed"


def test_manifest_maps_publication_failure(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text("occupied", encoding="utf-8")

    with pytest.raises(BackupError) as refusal:
        backup_module._write_manifest(
            tmp_path,
            instance_id=INSTANCE_ID,
            created_at=NOW,
            boundary=(),
            members=(),
        )
    assert refusal.value.code == "manifest_write_failed"


def test_backup_omits_the_attic_member_when_absent(tmp_path: Path) -> None:
    """Attic enabled but never written: the file does not exist, and the
    bundle honestly carries no Attic member rather than refusing."""
    config = make_config(tmp_path)
    migrate_catalogue(config, lambda: NOW)

    result = create_backup(config, tmp_path / "backups", clock=lambda: NOW)

    assert [member.name for member in result.members] == [CATALOGUE_FILENAME]
    manifest = read_manifest(result.bundle_path)
    assert [member["name"] for member in manifest["members"]] == [CATALOGUE_FILENAME]


def test_backup_cli_reports_the_bundle_it_wrote(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path, attic=False)
    migrate_catalogue(config, lambda: NOW)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)
    output = tmp_path / "backups"

    result = cli.main(["backup", "--config", str(config_path), "--output", str(output)])

    captured = capsys.readouterr()
    assert result == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["operation"] == "backup"
    assert payload["instance_id"] == str(INSTANCE_ID)
    assert payload["barrier_ms"] > 0
    bundle_path = Path(payload["bundle"])
    assert bundle_path.parent == output
    assert [member["name"] for member in payload["members"]] == [CATALOGUE_FILENAME]
    manifest = read_manifest(bundle_path)
    assert manifest["members"] == payload["members"]
    assert member_digests_verify(bundle_path, manifest)
    assert captured.err == ""


def test_backup_cli_reports_a_foreign_instance_refusal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    foreign = make_config(tmp_path, instance_id=OTHER_INSTANCE_ID)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, foreign)

    result = cli.main(
        [
            "backup",
            "--config",
            str(config_path),
            "--output",
            str(tmp_path / "backups"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert json.loads(captured.err) == {
        "code": "instance_mismatch",
        "status": "error",
    }
    assert captured.out == ""
