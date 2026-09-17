"""Opt-in real Cairn + built Garden host acceptance, using only temporary data."""

from __future__ import annotations

import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn

from cairn.bootstrap.procedures import bootstrap_realm
from cairn.catalogue.migration import migrate_catalogue
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn_install import garden
from cairn_install.core import open_context
from cairn_install.verification import ready


@pytest.mark.skipif(
    not os.environ.get("GARDEN_TEST_BINARY"),
    reason="explicit built Garden integration gate",
)
def test_managed_enrolment_tls_host_restart_and_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = Path(os.environ["GARDEN_TEST_BINARY"])
    assert binary.is_absolute() and binary.is_file()
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=garden.example.test",
            "-addext",
            "subjectAltName=DNS:garden.example.test",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    with socket.socket() as allocated:
        allocated.bind(("127.0.0.1", 0))
        garden_port = allocated.getsockname()[1]
    options_file = tmp_path / "garden.json"
    options_file.write_text(
        json.dumps(
            {
                "endpoint": "https://garden.example.test/mcp",
                "port": garden_port,
                "tls_cert_file": str(cert),
                "tls_key_file": str(key),
                "tls_ca_file": str(cert),
                "scope": {
                    "realm": "local",
                    "segments": [
                        {"kind": "job", "identifier": "acceptance"},
                        {"kind": "run", "identifier": "isolated"},
                    ],
                },
                "participants": {"val": "codex", "spike": "claude"},
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            }
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        cairn_port = listener.getsockname()[1]
        with open_context(
            tmp_path / "state",
            "acceptance",
            create={
                "source": str(Path(__file__).resolve().parents[2]),
                "mode": "native",
                "semantic": False,
                "port": cairn_port,
                "garden": {"options": garden.load_options(options_file, "native")},
            },
        ) as ctx:
            data, credentials = ctx.root / "data", ctx.root / "credentials"
            data.mkdir(mode=0o700)
            credentials.mkdir(mode=0o700)
            config = CairnConfig(
                schema_version="cairn.config/v1",
                instance_id=UUID(ctx.instance_id),
                mode="test",
                http=HttpConfig(host="127.0.0.1", port=cairn_port),
                paths=PathConfig(data=data, credentials=credentials),
            )

            def clock() -> datetime:
                return datetime.now(UTC)

            migrate_catalogue(config, clock)
            admin = bootstrap_realm(
                config,
                realm_id="local",
                label="integration-only",
                clock=clock,
                uuid_factory=uuid4,
                entropy=secrets.token_bytes,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    build_application(config),
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                )
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            host = None
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    assert thread.is_alive() and time.monotonic() < deadline
                    time.sleep(0.01)
                garden.prepare(ctx)
                ctx.write_file(
                    credentials / "admin.token", admin.token + "\n", secret=True
                )
                garden.enrol(ctx, ready(ctx))
                host_config = ctx.root / "garden" / "host.json"
                ctx.write_file(
                    host_config,
                    json.dumps(
                        {
                            "gateway": garden.gateway_config(
                                ctx,
                                data_dir=str(ctx.root / "garden" / "data"),
                                cert_file=str(
                                    ctx.root / "garden" / "tls" / "server.crt"
                                ),
                                key_file=str(
                                    ctx.root / "garden" / "tls" / "server.key"
                                ),
                                daemon_url_file=str(
                                    ctx.root / "garden" / "run" / "daemon.url"
                                ),
                                listen=f"127.0.0.1:{garden_port}",
                                cairn_url=f"http://127.0.0.1:{cairn_port}/memory/v1/diagnose",
                            )
                        }
                    ),
                )
                monkeypatch.setattr(garden, "READY_SECONDS", 15)
                token = ctx.read_secret(
                    Path(ctx.state["garden"]["agents"]["val"]["token_file"])
                )
                tls = ssl.create_default_context(cafile=str(cert))

                def call(name: str, arguments: dict[str, object]) -> dict[str, Any]:
                    connection = garden._LocalTLS(
                        "garden.example.test", 443, "127.0.0.1", garden_port, tls, 5
                    )
                    try:
                        result = garden._rpc(
                            connection,
                            token,
                            2,
                            "tools/call",
                            {"name": name, "arguments": arguments},
                        )
                        assert not result.get("isError")
                        return result["structuredContent"]  # type: ignore[no-any-return]
                    finally:
                        connection.close()

                with (tmp_path / "host.log").open("wb") as log:
                    host = subprocess.Popen(
                        [str(binary), "host", "--config", str(host_config)],
                        stdout=log,
                        stderr=log,
                    )
                    garden.verify(ctx)
                    generation = ctx.state["garden"]["verification"]["val"][
                        "generation"
                    ]
                    sent = call(
                        "send_message",
                        {"content": "isolated retention proof", "recipients": ["val"]},
                    )
                    host.terminate()
                    assert host.wait(timeout=10) == 0
                    host = subprocess.Popen(
                        [str(binary), "host", "--config", str(host_config)],
                        stdout=log,
                        stderr=log,
                    )
                    garden.enrol(ctx, admin.token)
                    garden.verify(ctx)
                    assert (
                        ctx.state["garden"]["verification"]["val"]["generation"]
                        == generation
                    )
                    history = call(
                        "read_messages", {"generation": generation, "limit": 20}
                    )
                    assert any(
                        item["id"] == sent["id"]
                        and item["content"] == "isolated retention proof"
                        for item in history["messages"]
                    )
                    garden.profiles(ctx)
            finally:
                if host is not None and host.poll() is None:
                    host.terminate()
                    host.wait(timeout=10)
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
