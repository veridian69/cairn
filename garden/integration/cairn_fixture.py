"""Serve a disposable, real Cairn for the Garden cross-language acceptance test.

The parent receives synthetic credentials over a private pipe. No configuration
or credentials are discovered from the host. Closing stdin shuts the server down.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "transports" / "memory"))

from memory_support import REALM, SCOPE, Instance  # noqa: E402

from cairn.authority.credentials import mint_token  # noqa: E402
from cairn.catalogue.sqlite import (  # noqa: E402
    _open_write_connection,
    canonical_timestamp,
)


def add_admin(instance: Instance) -> tuple[UUID, str]:
    """Add a realm-wide administrator for the invite flow, opt-in via GARDEN_FIXTURE_ADMIN=1.

    create-principal and issue-credential need a live grant-manage grant with empty segments,
    so the administrator cannot be narrowed to SCOPE. Its delegation envelope is what
    create-grant checks: only retrieve and ingest may be delegated, so every grant it issues
    is limited to those operations, within the envelope's clearance and classifications.
    """
    # Grants are immutable and the schema rejects grant-manage without a delegation envelope,
    # so the rows are inserted in their final shape, as Instance.add_actor inserts them.
    principal, credential, grant = uuid4(), uuid4(), uuid4()
    minted = mint_token(credential, lambda count: bytes(range(count)))
    ts = canonical_timestamp(instance.clock())
    with _open_write_connection(instance.data_path, create=False) as connection:
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'workload', 'fixture-admin', ?)",
            (str(principal), ts),
        )
        connection.execute(
            "INSERT INTO credentials (credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(credential), str(principal), minted.verifier, ts),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, operations, "
            "read_clearance, write_classifications, delegable_operations, issued_by, expires_at, "
            "created_at) VALUES (?, ?, ?, '[]', '[\"grant-manage\"]', 'restricted', "
            '\'["internal","public","restricted"]\', \'["ingest","retrieve"]\', NULL, ?, ?)',
            (
                str(grant),
                str(principal),
                REALM,
                canonical_timestamp(datetime(2040, 1, 1, tzinfo=UTC)),
                ts,
            ),
        )
        connection.commit()
    return principal, minted.text


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="garden-cairn-fixture-") as directory:
        instance = Instance(Path(directory), attic=False)
        alice, alice_token = instance.add_actor(operations=["retrieve", "ingest"])
        bob, bob_token = instance.add_actor(operations=["retrieve", "ingest"])
        reader, reader_token = instance.add_actor(operations=["retrieve"])
        admin: tuple[UUID, str] | None = None
        if os.environ.get("GARDEN_FIXTURE_ADMIN") == "1":
            admin = add_admin(instance)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cert = os.environ.get("GARDEN_FIXTURE_TLS_CERT", "")
            key = os.environ.get("GARDEN_FIXTURE_TLS_KEY", "")
            if bool(cert) != bool(key):
                raise RuntimeError("fixture TLS certificate and key must be paired")
            server = uvicorn.Server(
                uvicorn.Config(
                    instance.application(),
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                    **({"ssl_certfile": cert, "ssl_keyfile": key} if cert else {}),
                )
            )
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started:
                    if not worker.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("disposable Cairn did not start")
                    time.sleep(0.01)
                published: dict[str, object] = {
                    "endpoint": (
                        f"{'https' if cert else 'http'}://127.0.0.1:{listener.getsockname()[1]}"
                        "/memory/v1/diagnose"
                    ),
                    "instance_id": str(instance.config.instance_id),
                    "scope": SCOPE,
                    "classification": "internal",
                    "actors": {
                        "alice": {"id": str(alice), "token": alice_token},
                        "bob": {"id": str(bob), "token": bob_token},
                        "reader": {"id": str(reader), "token": reader_token},
                    },
                }
                if admin is not None:
                    published["admin"] = {"id": str(admin[0]), "token": admin[1]}
                print(json.dumps(published), flush=True)
                for line in sys.stdin:
                    command = json.loads(line)
                    if command == {"revoke": "bob"}:
                        with _open_write_connection(
                            instance.data_path, create=False
                        ) as connection:
                            connection.execute(
                                "INSERT INTO credential_revocations "
                                "(credential_id, revoked_at, reason_code) "
                                "SELECT credential_id, created_at, 'fixture_revocation' "
                                "FROM credentials WHERE principal_id = ?",
                                (str(bob),),
                            )
                            connection.commit()
                        print('{"revoked":"bob"}', flush=True)
                    else:
                        raise ValueError("unsupported fixture command")
            finally:
                server.should_exit = True
                worker.join(timeout=10)
                if worker.is_alive():
                    raise RuntimeError("disposable Cairn did not stop")


if __name__ == "__main__":
    main()
