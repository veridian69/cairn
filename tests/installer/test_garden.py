"""Synthetic HTTP/TLS boundaries for optional Garden enrolment and exports."""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from cairn_install.core import Context, InstallError, open_context
from cairn_install.verification import request


@pytest.fixture
def configuration(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
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
    value: dict[str, Any] = {
        "endpoint": "https://garden.example.test:8443/mcp",
        "tls_cert_file": str(cert),
        "tls_key_file": str(key),
        "tls_ca_file": str(cert),
        "scope": {
            "realm": "local",
            "segments": [{"kind": "job", "identifier": "garden"}],
        },
        "participants": {"val": "codex", "spike": "claude", "helper": "opencode"},
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    path = tmp_path / "garden.json"
    path.write_text(json.dumps(value))
    return path, value


class Authority:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.revoked = False
        self.no_grants = False
        self.bad_scope = False
        self.expire_read = False
        self.lost_credential = False
        self.admin = "cairn1." + str(uuid4()) + "." + "a" * 43

    def result(
        self, path: str, body: dict[str, Any], bearer: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((path, body, key))
        if path == "/v1/instance":
            return 200, {
                "contract_identity": "cairn/v1",
                "instance_id": self.ctx.instance_id,
            }
        if path == "/memory/v1/diagnose":
            if self.revoked or bearer not in self.tokens:
                return 401, {"failure": {"code": "authentication_failed"}}
            scope = (
                body["scope"]
                if not self.bad_scope
                else {"realm": "local", "segments": []}
            )
            return 200, {
                "instance_id": self.ctx.instance_id,
                "product_version": "0.1.0rc2",
                "contract_identity": "cairn.memory/v1",
                "contract_digest": hashlib.sha256(
                    (
                        self.ctx.source
                        / "src/cairn/contracts/cairn-memory-openapi-v1.json"
                    ).read_bytes()
                ).hexdigest(),
                "mcp_contract_digest": hashlib.sha256(
                    (
                        self.ctx.source
                        / "src/cairn/contracts/cairn-memory-mcp-tools-v1.json"
                    ).read_bytes()
                ).hexdigest(),
                "principal_id": self.tokens[bearer],
                "principal_kind": "workload",
                "scope": scope,
                "classification": body["classification"],
                "permissions": {
                    "retrieve": not self.no_grants,
                    "ingest": not self.no_grants,
                    "promote": False,
                    "invalidate": False,
                },
                "evaluated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "permission_basis": "current_grants_only",
            }
        assert bearer == self.admin
        assert key
        if key in self.results:
            response = json.loads(json.dumps(self.results[key]))
            response["outcome"] = "replayed"
            if path == "/v1/issue-credential":
                response["result"]["plaintext"] = None
            return 200, response
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if path == "/v1/create-principal":
            result = {
                "principal_id": str(uuid4()),
                "kind": body["kind"],
                "label": body["label"],
                "created_at": now,
            }
        elif path == "/v1/issue-credential":
            credential_id = str(uuid4())
            token = "cairn1." + credential_id + "." + "b" * 43
            self.tokens[token] = body["principal_id"]
            result = {
                "principal_id": body["principal_id"],
                "credential_id": credential_id,
                "created_at": now,
                "expires_at": body["expires_at"],
                "plaintext": token,
            }
        elif path == "/v1/create-grant":
            result = {"grant_id": str(uuid4()), "created_at": now}
        else:
            raise AssertionError(path)
        response = {
            "outcome": "committed",
            "result": result,
            "mutation_receipt": {
                "mutation_id": str(uuid4()),
                "command_digest": "c" * 64,
            },
            "audit_receipt": {
                "event_id": str(uuid4()),
                "chain_kind": "realm",
                "chain_identity": "local",
                "sequence": len(self.results) + 1,
                "recorded_at": now,
                "event_hash": "d" * 64,
            },
        }
        self.results[key] = response
        if path == "/v1/issue-credential" and self.lost_credential:
            return 503, {}
        return 200, response


@contextmanager
def authority(ctx: Context) -> Iterator[Authority]:
    fake = Authority(ctx)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.respond({})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/memory/v1/diagnose" and "Idempotency-Key" in self.headers:
                self.send_response(400)
                self.end_headers()
                return
            self.respond(body)

        def respond(self, body: dict[str, Any]) -> None:
            status, value = fake.result(
                self.path,
                body,
                self.headers.get("Authorization", "").removeprefix("Bearer "),
                self.headers.get("Idempotency-Key", ""),
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx.state["port"] = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def installation(tmp_path: Path, options: dict[str, Any]) -> Iterator[Context]:
    with open_context(
        tmp_path / "state",
        "example",
        create={
            "source": str(Path(__file__).resolve().parents[2]),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        ctx.state["garden"] = {"options": options}
        ctx.save()
        yield ctx


def test_options_validate_and_keep_only_file_references(
    configuration: tuple[Path, dict[str, Any]],
) -> None:
    path, raw = configuration
    options = garden.load_options(path, "native")
    assert options["port"] == 8443
    assert options["classification"] == "internal"
    assert options["scope"] == raw["scope"]
    assert "PRIVATE KEY" not in json.dumps(options)
    assert "CERTIFICATE" not in json.dumps(options)
    with pytest.raises(InstallError, match="requires native, docker or kubernetes"):
        garden.load_options(path, "disposable")
    cases: list[dict[str, Any]] = [
        {"unknown": True},
        {"port": True},
        {"port": 0},
        {"endpoint": "http://garden.example.test:8443/mcp"},
        {"endpoint": "https://user:pass@garden.example.test:8443/mcp"},
        {"endpoint": "https://127.0.0.1:8443/mcp"},
        {"scope": {"realm": "other", "segments": []}},
        {"classification": "secret"},
        {"participants": {"../escape": "claude"}},
        {"participants": {"val": "unknown"}},
        {"expires_at": "2000-01-01T00:00:00.000000Z"},
        {"tls_key_file": "relative.key"},
        {"image": "example:latest"},
    ]
    for change in cases:
        path.write_text(json.dumps(raw | change))
        with pytest.raises(InstallError):
            garden.load_options(path, "native")
    path.write_text(json.dumps(raw)[:-1] + ',"port":8443,"port":443}')
    with pytest.raises(InstallError):
        garden.load_options(path, "native")


def test_public_endpoint_port_is_independent_of_local_listener(
    configuration: tuple[Path, dict[str, Any]],
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"endpoint": "https://garden.example.test/mcp"}))
    assert garden.load_options(path, "native")["port"] == 8443


def test_kubernetes_requires_pinned_image_and_explicit_ingress(
    configuration: tuple[Path, dict[str, Any]],
) -> None:
    path, raw = configuration
    with pytest.raises(InstallError):
        garden.load_options(path, "kubernetes")
    path.write_text(
        json.dumps(
            raw
            | {
                "image": "registry.example/garden@sha256:" + "f" * 64,
                "allowed_cidrs": ["192.0.2.0/24"],
            }
        )
    )
    options = garden.load_options(path, "kubernetes")
    assert options["kubernetes_service_type"] == "ClusterIP"
    assert options["allowed_cidrs"] == ["192.0.2.0/24"]


def test_prepare_is_owned_and_detects_scope_or_tls_change(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with installation(tmp_path, garden.load_options(path, "native")) as ctx:
        garden.prepare(ctx)
        garden.prepare(ctx)
        key = ctx.root / "garden" / "tls" / "server.key"
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.read_bytes() == Path(configuration[1]["tls_key_file"]).read_bytes()
        ctx.state["garden"]["options"]["scope"]["segments"] = []
        with pytest.raises(InstallError):
            garden.prepare(ctx)


def test_enrol_is_idempotent_private_and_exactly_scoped(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        mutation_count = len(fake.results)
        garden.enrol(ctx, fake.admin)
        assert len(fake.results) == mutation_count == 9
        assert len([p for p, _, _ in fake.calls if p == "/memory/v1/diagnose"]) == 6
        assert (
            len(
                {
                    entry["principal_id"]
                    for entry in ctx.state["garden"]["agents"].values()
                }
            )
            == 3
        )
        for name, entry in ctx.state["garden"]["agents"].items():
            token = Path(entry["token_file"]).read_text().strip()
            assert token not in json.dumps(ctx.state)
            assert token not in (ctx.directory / "commands.log").read_text()
            assert fake.tokens[token] == entry["principal_id"]
            assert name in {"val", "spike", "helper"}
        for endpoint, body, _ in fake.calls:
            if endpoint == "/v1/create-grant":
                assert body["grant"]["segments"] == [
                    {"kind": "job", "identifier": "garden"}
                ]
                assert body["grant"]["operations"] == ["retrieve", "ingest"]
                assert body["grant"]["write_classifications"] == ["internal"]
        cfg = garden.gateway_config(
            ctx,
            data_dir="/var/lib/garden/data",
            daemon_url_file="/var/lib/garden/run/daemon.url",
            cert_file="/certs/server.crt",
            key_file="/certs/server.key",
            listen="0.0.0.0:8443",
            cairn_url="http://127.0.0.1:8000",
        )
        assert cfg["auth"]["endpoint"] == "http://127.0.0.1:8000/memory/v1/diagnose"
        assert set(cfg["principals"].values()) == {"val", "spike", "helper"}
        garden.profiles(ctx)
        for token in fake.tokens:
            assert token not in json.dumps(ctx.state)
        files = list((ctx.root / "garden" / "profiles").rglob("*.json"))
        assert len(files) >= 3
        assert all(
            "session_id" not in json.loads(f.read_text())
            or "REPLACE" in json.loads(f.read_text())["session_id"]
            for f in files
            if ".profile." in f.name
        )


@pytest.mark.parametrize("problem", ["revoked", "no_grants", "bad_scope"])
def test_resume_does_not_restore_revoked_or_mismatched_authority(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]], problem: str
) -> None:
    with (
        installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        before = len(fake.results)
        setattr(fake, problem, True)
        with pytest.raises(InstallError):
            garden.enrol(ctx, fake.admin)
        assert len(fake.results) == before


def test_lost_plaintext_reuses_key_and_requires_explicit_recovery(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    with (
        installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        fake.lost_credential = True
        with pytest.raises(InstallError):
            garden.enrol(ctx, fake.admin)
        fake.lost_credential = False
        with pytest.raises(InstallError) as caught:
            garden.enrol(ctx, fake.admin)
        assert caught.value.code == "needs_credential_recovery"
        issued = [key for path, _, key in fake.calls if path == "/v1/issue-credential"]
        assert len(issued) == 2 and len(set(issued)) == 1
        assert len(fake.tokens) == 1


def test_retained_credential_cannot_be_replaced_or_silently_rotated(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    with (
        installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        agent = ctx.state["garden"]["agents"]["helper"]
        Path(agent["token_file"]).write_text("replaced-token\n")
        before = len(fake.results)
        with pytest.raises(InstallError):
            garden.enrol(ctx, fake.admin)
        assert len(fake.results) == before


def test_captured_plaintext_survives_interruption_before_token_publication(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        original = ctx.write_file

        def interrupted(path: Path, content: str, **kwargs: Any) -> None:
            if path.name == "agent.token":
                raise InstallError("synthetic interruption")
            original(path, content, **kwargs)

        monkeypatch.setattr(ctx, "write_file", interrupted)
        with pytest.raises(InstallError):
            garden.enrol(ctx, fake.admin)
        monkeypatch.setattr(ctx, "write_file", original)
        garden.enrol(ctx, fake.admin)
        assert len(fake.results) == 9
        assert len([p for p, _, _ in fake.calls if p == "/v1/issue-credential"]) == 3


def test_prepare_rejects_tls_source_replacement(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    options = garden.load_options(configuration[0], "native")
    Path(options["tls_key_file"]).write_text("replacement")
    with installation(tmp_path, options) as ctx, pytest.raises(InstallError):
        garden.prepare(ctx)


def test_native_rollback_resume_preserves_enrolled_host_config_across_state_reload(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install.garden_native import GardenBackend

    from .test_garden_native import manager

    manager(monkeypatch, tmp_path)
    ctx = open_context(
        tmp_path / "state",
        "resumed",
        create={
            "source": str(Path(__file__).resolve().parents[2]),
            "mode": "native",
            "port": 19000,
            "semantic": False,
            "garden": {"options": garden.load_options(configuration[0], "native")},
        },
    )
    try:
        with authority(ctx) as fake:
            garden.prepare(ctx)
            garden.enrol(ctx, fake.admin)
            backend = GardenBackend(ctx)
            ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)

            # Seed the exact pre-fix serialisation, as retained live installs use it.
            def legacy_json(
                context: Context,
                path: Path,
                value: dict[str, Any],
                *,
                mode: int = 0o600,
            ) -> None:
                context.write_file(path, json.dumps(value, indent=2) + "\n", mode=mode)

            with monkeypatch.context() as legacy:
                legacy.setattr(garden, "write_json_config", legacy_json)
                backend.garden_prepare()
                garden.profiles(ctx)
            backend.garden_start()
            retained_config = backend.config_path.read_bytes()
            retained_profiles = {
                p: p.read_bytes()
                for p in (ctx.root / "garden/profiles").rglob("*")
                if p.is_file()
            }
            retained_ids = {
                name: agent["credential_id"]
                for name, agent in ctx.state["garden"]["agents"].items()
            }
            backend.rollback()
            ctx.state["status"] = "rolled_back"
            ctx.save()
            ctx.__exit__(None, None, None)
            with open_context(tmp_path / "state", "resumed") as resumed:
                garden.prepare(resumed)
                garden.enrol(resumed, fake.admin)
                restarted = GardenBackend(resumed)
                restarted.garden_prepare()
                restarted.garden_start()
                garden.profiles(resumed)
                assert restarted.config_path.read_bytes() == retained_config
                assert all(
                    p.read_bytes() == content
                    for p, content in retained_profiles.items()
                )
                assert {
                    name: agent["credential_id"]
                    for name, agent in resumed.state["garden"]["agents"].items()
                } == retained_ids
                assert len(fake.results) == 9
    finally:
        ctx.__exit__(None, None, None)


def test_docker_host_config_preserves_legacy_bytes_after_state_reload(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install.docker import Backend

    monkeypatch.setattr(Backend, "validate_ownership", lambda self: None)
    monkeypatch.setattr(Backend, "_command", lambda self, *args, **kwargs: "")
    with (
        installation(tmp_path, garden.load_options(configuration[0], "docker")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        backend = Backend(ctx)
        config = garden.gateway_config(
            ctx,
            data_dir="/var/lib/garden/data",
            daemon_url_file="/var/lib/garden/run/daemon.url",
            cert_file="/var/run/secrets/garden/server.crt",
            key_file="/var/run/secrets/garden/server.key",
            listen="0.0.0.0:9443",
            cairn_url="http://127.0.0.1:8000/memory/v1/diagnose",
        )
        legacy = (
            json.dumps(
                {"gateway": config, "stream": {"max_age": "0", "max_bytes": 0}},
                indent=2,
            )
            + "\n"
        )
        path = ctx.root / "garden/host.json"
        ctx.write_file(path, legacy, mode=0o644)
        ctx.save()
        ctx.state = json.loads((ctx.directory / "state.json").read_text())
        backend = Backend(ctx)
        backend.garden_prepare()
        assert path.read_text() == legacy
        assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "change", ["configuration", "contents", "permissions", "unowned"]
)
def test_equivalent_json_resume_still_rejects_changed_or_unowned_files(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]], change: str
) -> None:
    with installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx:
        path = ctx.root / "garden/host.json"
        original = {"gateway": {"scope": {"realm": "local", "segments": []}}}
        garden.write_json_config(ctx, path, original)
        expected = original
        if change == "configuration":
            expected = {"gateway": {"scope": {"realm": "other", "segments": []}}}
        elif change == "contents":
            path.write_text(json.dumps(original))
        elif change == "permissions":
            path.chmod(0o644)
        else:
            ctx.state["owned_files"].pop(str(path))
            ctx.state["file_intents"].pop(str(path))
        with pytest.raises(InstallError):
            garden.write_json_config(ctx, path, expected)


def test_diagnose_helper_does_not_send_mutation_header(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    with (
        installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx,
        authority(ctx) as fake,
    ):
        token = "synthetic"
        fake.tokens[token] = str(uuid4())
        value = request(
            ctx,
            "/memory/v1/diagnose",
            token,
            data=json.dumps(
                {"scope": configuration[1]["scope"], "classification": "internal"}
            ).encode(),
        )
        assert value["permissions"]["retrieve"] is True


def test_enrol_against_real_disposable_cairn_http(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    with installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx:
        data, credentials = tmp_path / "cairn-data", tmp_path / "cairn-credentials"
        data.mkdir()
        credentials.mkdir()
        config = CairnConfig(
            schema_version="cairn.config/v1",
            instance_id=UUID(ctx.instance_id),
            mode="test",
            http=HttpConfig(host="127.0.0.1", port=8000),
            paths=PathConfig(data=data, credentials=credentials),
        )

        def clock() -> datetime:
            return datetime.now(UTC)

        migrate_catalogue(config, clock)
        bootstrap = bootstrap_realm(
            config,
            realm_id="local",
            label="synthetic-garden-admin",
            clock=clock,
            uuid_factory=uuid4,
            entropy=secrets.token_bytes,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ctx.state["port"] = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    build_application(config, clock=clock),
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                )
            )
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started:
                    if not worker.is_alive() or time.monotonic() >= deadline:
                        pytest.fail("Disposable Cairn HTTP startup failed")
                    time.sleep(0.01)
                garden.prepare(ctx)
                ctx.state["receipts"]["instance"] = request(
                    ctx, "/v1/instance", bootstrap.token
                )
                garden.enrol(ctx, bootstrap.token)
                retained = {
                    name: (
                        agent["principal_id"],
                        agent["credential_id"],
                        agent["grant_id"],
                        Path(agent["token_file"]).read_bytes(),
                    )
                    for name, agent in ctx.state["garden"]["agents"].items()
                }
                garden.enrol(ctx, bootstrap.token)
                assert retained == {
                    name: (
                        agent["principal_id"],
                        agent["credential_id"],
                        agent["grant_id"],
                        Path(agent["token_file"]).read_bytes(),
                    )
                    for name, agent in ctx.state["garden"]["agents"].items()
                }
                # Revocation is exercised through the same public administration API.
                agent = ctx.state["garden"]["agents"]["helper"]
                request(
                    ctx,
                    "/v1/revoke-credential",
                    bootstrap.token,
                    data=json.dumps(
                        {
                            "realm_id": "local",
                            "credential_id": agent["credential_id"],
                            "reason_code": "synthetic_test",
                        }
                    ).encode(),
                    key=str(uuid4()),
                )
                with pytest.raises(InstallError):
                    garden.enrol(ctx, bootstrap.token)
            finally:
                server.should_exit = True
                worker.join(timeout=10)
                assert not worker.is_alive()


@pytest.mark.parametrize("problem", ["participant", "hostname", "encoding"])
def test_tls_verification_pins_hostname_and_each_participant(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]], problem: str
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        binding = {
            "instance_id": ctx.instance_id,
            "scope": ctx.state["garden"]["options"]["scope"],
            "classification": "internal",
        }
        names = {
            Path(a["token_file"]).read_text().strip(): name
            for name, a in ctx.state["garden"]["agents"].items()
        }
        seen: set[str] = set()
        mismatch = False

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if not self.headers["Host"].startswith("127.0.0.1:"):
                    self.send_response(403)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = names[self.headers["Authorization"].removeprefix("Bearer ")]
                if body["method"] == "initialize":
                    result: dict[str, Any] = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "garden", "version": "0.1.0"},
                    }
                else:
                    assert (
                        body["method"] == "tools/call"
                        and body["params"]["name"] == "status"
                    )
                    seen.add(name)
                    result = {
                        "structuredContent": {
                            "binding": binding,
                            "participant": "wrong" if mismatch else name,
                            "generation": "2026-09-17T00:00:00Z",
                            "participants": sorted(names.values()),
                        }
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if mismatch and problem == "encoding":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    ).encode()
                )

            def log_message(self, *_: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            ctx.root / "garden/tls/server.crt", ctx.root / "garden/tls/server.key"
        )
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            garden.verify(ctx, connect_port=server.server_port)
            assert seen == {"val", "spike", "helper"}
            mismatch = True
            if problem == "hostname":
                ctx.state["garden"]["options"]["endpoint"] = (
                    "https://wrong.example.test:8443/mcp"
                )
                del ctx.state["garden"]["options_digest"]
            with pytest.raises(InstallError):
                garden.verify(ctx, connect_port=server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_tls_verification_reports_openssl_reason(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)

        def reject_certificate(*_: Any, **__: Any) -> dict[str, Any]:
            raise ssl.SSLCertVerificationError(
                1,
                "certificate verify failed: CA cert does not include key usage extension",
            )

        monkeypatch.setattr(garden, "_rpc", reject_certificate)

        with pytest.raises(
            InstallError, match="CA cert does not include key usage extension"
        ):
            garden.verify(ctx)
