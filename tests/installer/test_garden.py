"""Synthetic HTTP/TLS boundaries for optional Garden enrolment and exports."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
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
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn

from cairn.bootstrap.procedures import bootstrap_realm
from cairn.catalogue.migration import migrate_catalogue
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn_install import garden
from cairn_install.core import Context, InstallError, open_context, read_owned
from cairn_install.verification import request

from .tls_support import self_signed


@pytest.fixture
def configuration(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    self_signed(cert, key, "garden.example.test", days=2)
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
        self.revoked_targets: set[str] = set()
        self.lose_response: str | None = None
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
        elif path in ("/v1/revoke-credential", "/v1/revoke-grant"):
            field = "credential_id" if path == "/v1/revoke-credential" else "grant_id"
            if body[field] in self.revoked_targets:
                # Cairn refuses a fresh key for an already revoked target.
                return 400, {"failure": {"code": "invalid_request"}}
            self.revoked_targets.add(body[field])
            result = {field: body[field], "revoked_at": now}
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
        if path == self.lose_response:
            # Cairn committed, but the response was lost once.
            self.lose_response = None
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


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.1/mcp",
        "https://2130706433/mcp",
        "https://0x7f000001/mcp",
        "https://0177.0.0.1/mcp",
        "https://127.1./mcp",
        "https://garden.example:/mcp",
        "https://[2001:db8::1%25eth0]/mcp",
        "https://[::ffff:127.0.0.1]/mcp",
    ],
)
def test_public_endpoint_rejects_ambiguous_authority(
    configuration: tuple[Path, dict[str, Any]], endpoint: str
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"endpoint": endpoint}))
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
        assert cfg["public_endpoint"] == "https://garden.example.test:8443/mcp"
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
        opencode = json.loads(
            (
                ctx.root / "garden/profiles/helper/opencode.profile.example.json"
            ).read_text()
        )
        assert opencode["host_api_version"] == "REPLACE_WITH_1_OR_2"


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
                legacy.setattr(garden, "_publish_profile_json", legacy_json)
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


@pytest.mark.parametrize("public_port", [443, 8443, 9444])
@pytest.mark.parametrize("problem", ["participant", "hostname", "encoding"])
def test_tls_verification_pins_hostname_and_each_participant(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    problem: str,
    public_port: int,
) -> None:
    path, raw = configuration
    raw["endpoint"] = f"https://garden.example.test:{public_port}/mcp"
    path.write_text(json.dumps(raw))
    expected_host = "garden.example.test" + (
        f":{public_port}" if public_port != 443 else ""
    )
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
                if self.headers["Host"] != expected_host:
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


@pytest.mark.parametrize("wildcard", [False, True])
def test_listener_preflight_rejects_an_occupied_garden_port(
    wildcard: bool,
) -> None:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0" if wildcard else "127.0.0.1", 0))
        port = listener.getsockname()[1]

        with pytest.raises(InstallError, match="Garden listener port"):
            garden.require_listener_available(port, wildcard=wildcard)


@pytest.mark.parametrize("with_ca", [False, True])
def test_bundle_readme_leads_with_plugin_and_keeps_legacy_examples(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    with_ca: bool,
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        if not with_ca:
            ctx.state["garden"]["tls"].pop("ca_file")
        garden.profiles(ctx)
        readmes = {
            Path(directory).name: (Path(directory) / "README.md").read_text()
            for directory in ctx.state["garden"]["profiles"].values()
        }

    assert set(readmes) == {"val", "spike", "helper"}
    for name, adapter in ctx.state["garden"]["options"]["participants"].items():
        readme = readmes[name]
        assert "garden home add-identity" in readme
        assert "Install the plugin" in readme
        assert "Legacy MCP profile" in readme
        assert readme.index("Install the plugin") < readme.index("Legacy MCP profiles")
        assert "`garden/scripts/install-user`" in readme
        # Only a fresh home is initialised; an existing one's CA is never overwritten.
        assert "Initialise only a home directory that does not exist yet" in readme
        assert "If `~/.garden/garden.json` does not exist" not in readme
        assert "If `~/.garden` already exists, leave it unchanged" in readme
        assert f"`garden-config --host {adapter} --profile" in readme
        assert ("codex plugin marketplace add" in readme) == (adapter == "codex")
        assert ("garden.ts" in readme) == (adapter == "opencode")
        bundle = Path(ctx.state["garden"]["profiles"][name])
        home = json.loads((bundle / "garden.json").read_text())
        assert home["defaults"] == {adapter: name}
        if with_ca:
            assert home["garden_tls_ca_file"] == "tls/ca.crt"
            assert (bundle / "tls/ca.crt").read_text()
        else:
            assert "garden_tls_ca_file" not in home
            assert not (bundle / "tls/ca.crt").exists()


def _legacy_profiles(ctx: Context) -> None:
    """Baseline renderer from 35fd338a, kept for the old-install resume test."""
    state = garden._state(ctx)
    options = state["options"]
    exports = {}
    for name, adapter in options["participants"].items():
        directory = ctx.root / "garden" / "profiles" / name
        credential = Path(state["agents"][name]["token_file"])
        ctx.check_file(credential)
        ctx.write_file(
            directory / "agent.token", ctx.read_secret(credential) + "\n", secret=True
        )
        profile: dict[str, Any] = {
            "garden_endpoint": options["endpoint"],
            "credential_file": "agent.token",
            "instance_id": ctx.instance_id,
            "scope": options["scope"],
            "classification": options["classification"],
            "participant": name,
            "adapter": adapter,
        }
        if state["tls"].get("ca_file"):
            ca = Path(state["tls"]["ca_file"])
            ctx.check_file(ca)
            ctx.write_file(directory / "ca.crt", read_owned(ca).decode("ascii"))
            profile["garden_tls_ca_file"] = "ca.crt"
        if adapter == "codex":
            profile.update(
                session_id="REPLACE_WITH_EXISTING_TASK_ID",
                codex_socket="/REPLACE/WITH/EXISTING/CODEX/CONTROL.sock",
            )
        elif adapter == "opencode":
            profile.update(
                session_id="REPLACE_WITH_EXISTING_SESSION_ID",
                host_endpoint="http://127.0.0.1:4096",
                host_username="opencode",
                host_api_version="REPLACE_WITH_1_OR_2",
                host_credential_file="REPLACE_WITH_HOST_PASSWORD_FILE",
            )
        filename = adapter + ".profile.example.json"
        garden.write_json_config(ctx, directory / filename, profile)
        command = "/REPLACE/WITH/INSTALLED/garden"
        arguments = ["connect", "--profile", "/REPLACE/WITH/BUNDLE/profile.json"]
        if adapter == "codex":
            ctx.write_file(
                directory / "codex.mcp.example.toml",
                "[mcp_servers.garden]\ncommand = "
                + json.dumps(command)
                + "\nargs = "
                + json.dumps(arguments)
                + "\n",
            )
        else:
            mcp: dict[str, Any]
            if adapter == "claude":
                mcp = {
                    "mcpServers": {"garden": {"command": command, "args": arguments}}
                }
            else:
                mcp = {
                    "mcp": {
                        "garden": {
                            "type": "local",
                            "command": [command, *arguments],
                            "enabled": True,
                        }
                    }
                }
            garden.write_json_config(
                ctx, directory / (adapter + ".mcp.example.json"), mcp
            )
        if adapter == "claude":
            setup = (
                "Copy claude.profile.example.json to profile.json; it needs no edits. "
                "In claude.mcp.example.json, replace the REPLACE paths with the installed "
                "garden binary and this bundle's profile.json. "
            )
        elif adapter == "codex":
            setup = (
                "Copy codex.profile.example.json to profile.json and replace its REPLACE "
                "task and control-socket values. In codex.mcp.example.toml, replace the "
                "REPLACE paths with the installed garden binary and this bundle's profile.json. "
            )
        else:
            setup = (
                "Copy opencode.profile.example.json to profile.json and replace its REPLACE "
                'host-session values. Set host_api_version to "2" for an OpenCode 2.x server '
                'or "1" for 1.x; Garden cannot tell them apart. In opencode.mcp.example.json, '
                "replace the REPLACE paths "
                "with the installed garden binary and this bundle's profile.json. Explicitly configure "
                "the local server endpoint and protected password file. "
            )
        ctx.write_file(
            directory / "README.md",
            f"# Garden participant {name}\n\n"
            "Copy only this participant's directory to its agent machine, using a secure channel.\n"
            "Keep agent.token mode 0600 and this directory mode 0700. Do not distribute installer "
            "administrator credentials, issuance captures or other participants' directories.\n\n"
            "Install the garden binary and helper scripts on this machine with "
            "`garden/scripts/install-user` from the Cairn source checkout. "
            + setup
            + "Then merge the completed profile into the host's MCP configuration with "
            f"`garden-config --host {adapter} --profile /absolute/path/profile.json "
            "--binary /absolute/path/garden --config /absolute/path/to/host-mcp-config`; "
            "the example files above show the resulting shape if you prefer to edit by hand. "
            "No host session is created.\n\n"
            "Run `garden doctor --profile /absolute/path/profile.json` to verify the Garden "
            "binding, then "
            "`garden connect --profile /absolute/path/profile.json` for the MCP stdio bridge. "
            "Configure the agent's MCP command to this garden connect invocation; no bearer belongs in argv.\n\n"
            "For Claude use garden connect as its MCP server. For Codex/OpenCode run "
            "`garden attend --profile /absolute/path/profile.json` alongside the MCP bridge. "
            "Only one attention consumer may own this participant. The profile pins the deployment "
            "identity and scope; tokens remain subject to Cairn revocation and expiry.\n\n"
            "For Claude's unlisted development channel, use the MCP server name garden and "
            "`claude --dangerously-load-development-channels server:garden` only after reviewing "
            "the adapter and completing host channel consent. Organisation policy still applies. "
            "Claude must call acknowledge_delivery on receipt of each channel message.\n\n"
            "Codex requires an existing app-server control socket, which Garden reaches "
            "directly over WebSocket (verified with Codex 0.163.0). An arbitrary desktop "
            "or CLI session without that endpoint cannot be attached.\n",
        )
        exports[name] = str(directory)
    state["profiles"] = exports
    ctx.save()


def test_old_bundle_resume_replaces_readme_and_publishes_plugin_home(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        _legacy_profiles(ctx)
        directory = ctx.root / "garden/profiles/val"
        legacy_readme = (directory / "README.md").read_text()
        token = (directory / "agent.token").read_bytes()
        assert not (directory / "garden.json").exists()
        assert not (directory / "tls/ca.crt").exists()

    with open_context(tmp_path / "state", "example") as resumed:
        garden.profiles(resumed)
        garden.profiles(resumed)
        readme = (directory / "README.md").read_text()
        assert readme != legacy_readme
        assert "Install the plugin" in readme
        assert "garden home add-identity val" in readme
        home = json.loads((directory / "garden.json").read_text())
        assert home["defaults"] == {"codex": "val"}
        assert home["garden_tls_ca_file"] == "tls/ca.crt"
        assert (directory / "tls/ca.crt").read_bytes() == (
            directory / "ca.crt"
        ).read_bytes()
        assert (directory / "agent.token").read_bytes() == token


@pytest.mark.parametrize("change", ["contents", "permissions", "unowned", "symlink"])
def test_bundle_replacement_refuses_changed_or_unowned_files(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    change: str,
) -> None:
    with installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx:
        path = ctx.root / "garden/profiles/val/README.md"
        ctx.write_file(path, "old bundle instructions\n")
        if change == "contents":
            path.write_text("local edits\n")
        elif change == "permissions":
            path.chmod(0o644)
        elif change == "unowned":
            ctx.state["owned_files"].pop(str(path))
            ctx.state["file_intents"].pop(str(path))
        else:
            target = path.with_name("elsewhere")
            path.rename(target)
            path.symlink_to(target)
        with pytest.raises(InstallError):
            garden._publish_profile_file(ctx, path, "plugin instructions\n")


@pytest.mark.parametrize("published", [False, True])
def test_bundle_json_resumes_interrupted_replacement(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    published: bool,
) -> None:
    with installation(tmp_path, garden.load_options(configuration[0], "native")) as ctx:
        path = ctx.root / "garden/profiles/val/garden.json"
        old = '{"defaults": {"codex": "val"}}\n'
        value = {"defaults": {"codex": "val2"}}
        new = json.dumps(value, indent=2, sort_keys=True) + "\n"
        ctx.write_file(path, old)
        ctx.state["file_replacements"] = {
            str(path): {
                "old": hashlib.sha256(old.encode()).hexdigest(),
                "new": hashlib.sha256(new.encode()).hexdigest(),
            }
        }
        if published:
            path.write_text(new)
        ctx.save()
    with open_context(tmp_path / "state", "example") as resumed:
        garden._publish_profile_json(resumed, path, value)
        assert path.read_text() == new
        assert not resumed.pending_replacements()
        resumed.check_file(path)


@pytest.fixture(scope="module")
def built_garden(tmp_path_factory: pytest.TempPathFactory) -> Path:
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go is unavailable; cannot check Garden home compatibility")
    directory = tmp_path_factory.mktemp("garden-build")
    source = Path(__file__).resolve().parents[2] / "garden"
    # The caches garden/Makefile prepares, so a populated cache needs no network.
    env = dict(os.environ)
    env.setdefault("GOCACHE", str(source / ".cache" / "go-build"))
    env.setdefault("GOMODCACHE", str(source / ".cache" / "go-mod"))
    env["GOFLAGS"] = "-modcacherw"
    binary = directory / "garden"
    result = subprocess.run(
        [go, "build", "-trimpath", "-o", str(binary), "."],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize("with_ca", [False, True])
def test_generated_home_is_accepted_by_real_garden(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    built_garden: Path,
    with_ca: bool,
) -> None:
    # Garden checks every ancestor through /. Some user namespaces expose the
    # root as an unmapped UID, so no absolute GARDEN_HOME can pass StrictModes.
    if Path("/").stat().st_uid not in (0, os.getuid()):
        pytest.skip(
            "Garden home acceptance unavailable: sandbox root has an unmapped owner"
        )
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        if not with_ca:
            ctx.state["garden"]["tls"].pop("ca_file")
        garden.profiles(ctx)
        bundle = Path(ctx.state["garden"]["profiles"]["val"])
        home = tmp_path / "client-home"
        home.mkdir(mode=0o700)
        shutil.copyfile(bundle / "garden.json", home / "garden.json")
        (home / "garden.json").chmod(0o600)
        if with_ca:
            shutil.copytree(bundle / "tls", home / "tls")
        else:
            assert "garden_tls_ca_file" not in json.loads(
                (home / "garden.json").read_text()
            )
            assert not (bundle / "tls/ca.crt").exists()
        result = subprocess.run(
            [
                str(built_garden),
                "home",
                "add-identity",
                "val",
                "--token-file",
                str(bundle / "agent.token"),
                "--default",
                "codex",
            ],
            env={**os.environ, "GARDEN_HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (home / "identities/val.token").read_text() == (
            bundle / "agent.token"
        ).read_text()
        assert (home / "identities/val.token").stat().st_mode & 0o777 == 0o600
        assert json.loads((home / "garden.json").read_text())["defaults"] == {
            "codex": "val"
        }

        # The real parser must refuse a legacy profile masquerading as a home.
        (home / "garden.json").write_text(
            (bundle / "codex.profile.example.json").read_text()
        )
        refused = subprocess.run(
            [
                str(built_garden),
                "home",
                "add-identity",
                "val",
                "--token-file",
                str(bundle / "agent.token"),
            ],
            env={**os.environ, "GARDEN_HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert refused.returncode != 0


@pytest.mark.parametrize("outage", ["http_503", "tool_unavailable"])
def test_verification_retries_while_garden_authority_is_starting(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    outage: str,
) -> None:
    """Garden binds its listener before Cairn and its stream are reachable."""
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
        outages: list[str] = []
        pauses: list[float] = []
        monkeypatch.setattr(time, "sleep", pauses.append)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = names[self.headers["Authorization"].removeprefix("Bearer ")]
                if outage == "http_503" and not outages:
                    outages.append(name)
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Service Unavailable\n")
                    return
                if body["method"] == "initialize":
                    result: dict[str, Any] = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "garden", "version": "0.7.12"},
                    }
                elif outage == "tool_unavailable" and not outages:
                    outages.append(name)
                    result = {
                        "isError": True,
                        "structuredContent": {
                            "code": "unavailable",
                            "message": "Garden operation unavailable",
                        },
                        "content": [{"type": "text", "text": "unavailable"}],
                    }
                else:
                    result = {
                        "structuredContent": {
                            "binding": binding,
                            "participant": name,
                            "generation": "2026-09-17T00:00:00Z",
                            "participants": sorted(names.values()),
                        }
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        assert len(outages) == 1 and len(pauses) == 1
        assert set(ctx.state["garden"]["verification"]) == set(names.values())


def test_verification_keeps_binding_mismatch_and_denial_fatal(
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
        monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("must not retry"))
        answers: Iterator[dict[str, Any]] = iter(
            [
                {"protocolVersion": "2025-11-25"},
                {"isError": True, "structuredContent": {"code": "forbidden"}},
            ]
        )

        def rpc(*_: Any, **__: Any) -> dict[str, Any]:
            return next(answers)

        monkeypatch.setattr(garden, "_rpc", rpc)
        monkeypatch.setattr(
            garden, "_LocalTLS", lambda *_: SimpleNamespace(close=lambda: None)
        )
        with pytest.raises(InstallError, match="differs from the retained deployment"):
            garden.verify(ctx)


def test_test_certificates_are_backdated_ten_minutes(tmp_path: Path) -> None:
    """Operator, 25 September 2026: a clock stepped back by time synchronisation must
    not make a freshly generated certificate 'not yet valid'."""
    import calendar
    import time

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    before = time.time()
    self_signed(cert, key, "garden.example.test", days=2)
    text = subprocess.check_output(
        ["openssl", "x509", "-in", str(cert), "-noout", "-startdate", "-enddate",
         "-ext", "subjectAltName,basicConstraints"], text=True,
    )  # fmt: skip
    start, end = (
        calendar.timegm(time.strptime(line.split("=", 1)[1], "%b %d %H:%M:%S %Y GMT"))
        for line in text.splitlines()[:2]
    )
    assert before - 600 - 2 <= start <= time.time() - 600 + 2
    assert end - start == 2 * 86400
    assert "DNS:garden.example.test" in text and "CA:TRUE" in text
    assert key.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("0.0.0.0", "0.0.0.0"),
        ("127.0.0.1", "127.0.0.1"),
        ("::", "::"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
    ],
)
def test_listen_address_accepts_and_canonicalises_literals(
    configuration: tuple[Path, dict[str, Any]], given: str, expected: str
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"listen_address": given}))
    options = garden.load_options(path, "native")
    assert options["listen_address"] == expected
    assert garden.listen_address(options) == expected


@pytest.mark.parametrize(
    "given",
    [
        "localhost",
        "garden.example.test",
        "fe80::1%eth0",
        "192.0.2.256",
        "[::1]",
        "0x7f.0.0.1",
        "",
        127,
        None,
        True,
        ["::1"],
    ],
)
def test_listen_address_rejects_non_literals(
    configuration: tuple[Path, dict[str, Any]], given: Any
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"listen_address": given}))
    with pytest.raises(InstallError, match="listen_address"):
        garden.load_options(path, "native")


def test_listen_formats_ipv4_and_bracketed_ipv6() -> None:
    assert garden.listen({}, 8443) == "0.0.0.0:8443"
    assert garden.listen({"listen_address": "127.0.0.1"}, 8443) == "127.0.0.1:8443"
    assert garden.listen({"listen_address": "::1"}, 8443) == "[::1]:8443"
    assert garden.listen({"listen_address": "::"}, 9000) == "[::]:9000"


def test_connect_address_follows_a_specific_listen_address() -> None:
    assert garden.connect_address({}) == "127.0.0.1"
    assert garden.connect_address({"listen_address": "0.0.0.0"}) == "127.0.0.1"
    assert garden.connect_address({"listen_address": "::"}) == "::1"
    assert garden.connect_address({"listen_address": "192.0.2.10"}) == "192.0.2.10"
    assert garden.connect_address({"listen_address": "fd00::7"}) == "fd00::7"


def test_verify_dials_the_configured_listen_address(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker publishes Garden only on listen_address; loopback is refused there."""
    path, _ = configuration
    options = garden.load_options(path, "native")
    options["listen_address"] = "192.0.2.10"
    dialled: list[tuple[str, int]] = []

    class Dialled(Exception):
        pass

    def connection(
        _host: str, _port: int, connect_host: str, connect_port: int, *_: Any
    ) -> Any:
        dialled.append((connect_host, connect_port))
        raise Dialled

    with installation(tmp_path, options) as ctx, authority(ctx) as fake:
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        monkeypatch.setattr(garden, "_LocalTLS", connection)
        with pytest.raises(Dialled):
            garden.verify(ctx)
    assert dialled == [("192.0.2.10", options["port"])]


def test_legacy_option_files_store_no_new_keys(
    configuration: tuple[Path, dict[str, Any]],
) -> None:
    path, raw = configuration
    options = garden.load_options(path, "native")
    # Keys the installer already defaulted or derived; nothing new may appear.
    assert set(options) == set(raw) | {
        "port",
        "classification",
        "kubernetes_service_type",
        "allowed_cidrs",
        "tls_hashes",
    }
    assert "listen_address" not in options
    assert "stream" not in options


@pytest.mark.parametrize(
    "age", ["168h", "0", "1h30m", "45s", "1h", "30m", "0h", "2h0m5s"]
)
def test_stream_accepts_go_duration_units_h_m_s(
    configuration: tuple[Path, dict[str, Any]], age: str
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"stream": {"max_age": age, "max_bytes": 0}}))
    options = garden.load_options(path, "native")
    assert options["stream"] == {"max_age": age, "max_bytes": 0}


@pytest.mark.parametrize(
    "stream",
    [
        {"max_age": "-1h", "max_bytes": 0},
        {"max_age": "10d", "max_bytes": 0},
        {"max_age": "1.5h", "max_bytes": 0},
        {"max_age": "", "max_bytes": 0},
        {"max_age": "00", "max_bytes": 0},
        {"max_age": "168", "max_bytes": 0},
        {"max_age": "1h1h", "max_bytes": 0},
        {"max_age": "99999999999999h", "max_bytes": 0},
        {"max_age": 168, "max_bytes": 0},
        {"max_age": "1h", "max_bytes": True},
        {"max_age": "1h", "max_bytes": -1},
        {"max_age": "1h", "max_bytes": "1"},
        {"max_age": "1h", "max_bytes": 2**63},
        {"max_age": "1h", "max_bytes": 0, "extra": 1},
        {"max_age": "1h"},
        "1h",
    ],
)
def test_stream_rejects_invalid_retention(
    configuration: tuple[Path, dict[str, Any]], stream: Any
) -> None:
    path, raw = configuration
    path.write_text(json.dumps(raw | {"stream": stream}))
    with pytest.raises(InstallError, match="stream"):
        garden.load_options(path, "native")


def test_retention_prefers_stream_and_falls_back_to_legacy() -> None:
    legacy = {"max_age": "0", "max_bytes": 0}
    assert garden.retention({}, legacy) == legacy
    assert garden.retention({}, None) is None
    options = {"stream": {"max_age": "1h", "max_bytes": 5}}
    assert garden.retention(options, legacy) == {"max_age": "1h", "max_bytes": 5}
    assert garden.retention(options, None) == {"max_age": "1h", "max_bytes": 5}
    returned = garden.retention(options, None)
    assert returned is not None
    returned["max_bytes"] = 9
    assert options["stream"]["max_bytes"] == 5


def test_with_add_defaults_fixes_retention_without_mutating_input() -> None:
    options: dict[str, Any] = {"port": 8443}
    added = garden.with_add_defaults(options)
    assert added["stream"] == {"max_age": "168h", "max_bytes": 1073741824}
    assert "stream" not in options
    added["stream"]["max_age"] = "1h"
    assert garden.ADD_RETENTION["max_age"] == "168h"
    explicit = {"stream": {"max_age": "1h", "max_bytes": 1}}
    assert garden.with_add_defaults(explicit)["stream"] == explicit["stream"]


def test_listener_preflight_binds_an_explicit_address() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with pytest.raises(InstallError, match="Garden listener port"):
            garden.require_listener_available(port, wildcard=False, address="127.0.0.1")


def test_listener_preflight_checks_ipv6_addresses() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("::1", 0))
        except OSError:
            pytest.skip("IPv6 loopback is unavailable")
        port = probe.getsockname()[1]
        with pytest.raises(InstallError, match="Garden listener port"):
            garden.require_listener_available(port, wildcard=False, address="::1")


def test_revoke_removes_credentials_then_grants_with_saved_keys(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        garden.revoke(ctx, fake.admin)
        paths = [p for p, _, _ in fake.calls if p.startswith("/v1/revoke-")]
        assert paths == ["/v1/revoke-credential", "/v1/revoke-grant"] * 3
        keys = [k for p, _, k in fake.calls if p.startswith("/v1/revoke-")]
        assert len(set(keys)) == 6
        revocations = ctx.state["garden"]["revocations"]
        assert set(revocations) == {"helper", "spike", "val"}
        for name, record in revocations.items():
            agent = ctx.state["garden"]["agents"][name]
            credential = record["operations"]["revoke-credential"]
            grant = record["operations"]["revoke-grant"]
            assert credential["body"]["credential_id"] == agent["credential_id"]
            assert grant["body"]["grant_id"] == agent["grant_id"]
            assert (
                credential["receipt"]["result"]["credential_id"]
                == agent["credential_id"]
            )
            assert grant["receipt"]["result"]["grant_id"] == agent["grant_id"]
        assert "plaintext" not in json.dumps(ctx.state)
        before = len(fake.calls)
        garden.revoke(ctx, fake.admin)
        assert [
            p for p, _, _ in fake.calls[before:] if p.startswith("/v1/revoke-")
        ] == []


def test_revoke_replay_reuses_lost_key_and_never_resends_receipts(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        fake.lose_response = "/v1/revoke-grant"
        with pytest.raises(InstallError):
            garden.revoke(ctx, fake.admin)
        lost = ctx.state["garden"]["revocations"]["helper"]["operations"]
        assert "receipt" not in lost["revoke-grant"]
        lost_key = lost["revoke-grant"]["key"]
        garden.revoke(ctx, fake.admin)
        calls = [(p, b, k) for p, b, k in fake.calls if p.startswith("/v1/revoke-")]
        assert [p for p, _, _ in calls] == [
            "/v1/revoke-credential",
            "/v1/revoke-grant",
            "/v1/revoke-grant",
            "/v1/revoke-credential",
            "/v1/revoke-grant",
            "/v1/revoke-credential",
            "/v1/revoke-grant",
        ]
        assert [k for _, b, k in calls if "grant_id" in b].count(lost_key) == 2
        assert (
            ctx.state["garden"]["revocations"]["helper"]["operations"]["revoke-grant"][
                "receipt"
            ]["outcome"]
            == "replayed"
        )


def test_revoke_surfaces_refusal_without_inventing_success(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        fake.revoked_targets.add(
            ctx.state["garden"]["agents"]["helper"]["credential_id"]
        )
        with pytest.raises(
            InstallError,
            match="returned HTTP 400.*already revoked outside the installer",
        ):
            garden.revoke(ctx, fake.admin)
        record = ctx.state["garden"]["revocations"]["helper"]["operations"]
        assert record["revoke-credential"]["key"]
        assert "receipt" not in record["revoke-credential"]
        assert all(p != "/v1/revoke-grant" for p, _, _ in fake.calls)


def test_revoke_crash_before_request_reuses_saved_key(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        real = request

        def interrupted(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[1] == "/v1/revoke-credential":
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(garden, "request", interrupted)
            with pytest.raises(KeyboardInterrupt):
                garden.revoke(ctx, fake.admin)
        saved = ctx.state["garden"]["revocations"]["helper"]["operations"]
        key = saved["revoke-credential"]["key"]
        assert "receipt" not in saved["revoke-credential"]
        assert not [p for p, _, _ in fake.calls if p.startswith("/v1/revoke-")]
        garden.revoke(ctx, fake.admin)
        first = next(c for c in fake.calls if c[0] == "/v1/revoke-credential")
        assert first[2] == key


def test_revoke_rejects_mismatched_revocation_result(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        real = request

        def mismatched(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = real(*args, **kwargs)
            if args[1] == "/v1/revoke-grant":
                response["result"]["grant_id"] = str(uuid4())
            return response

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(garden, "request", mismatched)
            with pytest.raises(InstallError, match="differs from removal"):
                garden.revoke(ctx, fake.admin)


def test_re_add_reuses_dormant_principals_and_issues_new_authority(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    from cairn_install import workflow

    path, _ = configuration
    options = garden.load_options(path, "native")
    with (
        installation(tmp_path, options) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        garden.revoke(ctx, fake.admin)
        first = ctx.state.pop("garden")
        ctx.state["garden_history"] = [first]
        for name in first["agents"]:
            directory = ctx.root / "credentials" / "garden" / name
            for leaf in ("issue.json", "agent.token"):
                (directory / leaf).unlink()
                ctx.state["owned_files"].pop(str(directory / leaf), None)
                ctx.state.get("file_intents", {}).pop(str(directory / leaf), None)
        ctx.state["garden"] = {
            "options": options,
            "agents": workflow._retained_principals(ctx, options),
        }
        before = len(fake.calls)
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        paths = [p for p, _, _ in fake.calls[before:] if p.startswith("/v1/")]
        assert "/v1/create-principal" not in paths
        assert paths.count("/v1/issue-credential") == len(first["agents"])
        assert paths.count("/v1/create-grant") == len(first["agents"])
        for name, agent in ctx.state["garden"]["agents"].items():
            old = first["agents"][name]
            assert agent["principal_id"] == old["principal_id"]
            assert agent["credential_id"] != old["credential_id"]
            assert agent["grant_id"] != old["grant_id"]


def test_re_add_replays_a_principal_whose_response_was_lost(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    from cairn_install import workflow

    path, _ = configuration
    options = garden.load_options(path, "native")
    with (
        installation(tmp_path, options) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        fake.lose_response = "/v1/create-principal"
        with pytest.raises(InstallError):
            garden.enrol(ctx, fake.admin)
        first = ctx.state.pop("garden")
        (name,) = first["agents"]
        lost = first["agents"][name]["operations"]["create-principal"]
        assert "receipt" not in lost
        committed = fake.results[lost["key"]]["result"]["principal_id"]
        ctx.state["garden_history"] = [first]
        ctx.state["garden"] = {
            "options": options,
            "agents": workflow._retained_principals(ctx, options),
        }
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        keys = [k for p, _, k in fake.calls if p == "/v1/create-principal"]
        assert keys.count(lost["key"]) == 2
        agent = ctx.state["garden"]["agents"][name]
        assert agent["principal_id"] == committed
        assert agent["operations"]["create-principal"]["receipt"]["outcome"] == (
            "replayed"
        )


def _garden_tree(source: Path) -> Path:
    (source / "garden" / "cmd").mkdir(parents=True)
    (source / "garden" / "cmd" / "main.go").write_text("package cmd\n")
    (source / "garden" / "go.mod").write_text("module example\n")
    (source / "src" / "cairn").mkdir(parents=True)
    (source / "src" / "cairn" / "module.py").write_text("VALUE = 1\n")
    (source / "pyproject.toml").write_text('[project]\nversion = "1"\n')
    return source


def test_garden_fingerprint_is_stable_and_tracks_garden_sources(tmp_path: Path) -> None:
    source = _garden_tree(tmp_path / "source")
    first = garden.garden_fingerprint(source)
    assert garden.garden_fingerprint(source) == first
    (source / "src" / "cairn" / "module.py").write_text("VALUE = 2\n")
    (source / "pyproject.toml").write_text('[project]\nversion = "2"\n')
    assert garden.garden_fingerprint(source) == first
    (source / "garden" / ".cache" / "go-build").mkdir(parents=True)
    (source / "garden" / ".cache" / "go-build" / "entry").write_text("cache")
    (source / "garden" / "garden").write_bytes(b"built binary")
    (source / "garden" / "cmd" / "__pycache__").mkdir()
    (source / "garden" / "cmd" / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert garden.garden_fingerprint(source) == first
    (source / "garden" / "cmd" / "main.go").write_text("package cmd\n// changed\n")
    changed = garden.garden_fingerprint(source)
    assert changed != first
    (source / "garden" / "cmd" / "extra.go").write_text("package cmd\n")
    assert garden.garden_fingerprint(source) not in {first, changed}


def test_garden_fingerprint_hashes_relative_paths(tmp_path: Path) -> None:
    source = _garden_tree(tmp_path / "source")
    first = garden.garden_fingerprint(source)
    (source / "garden" / "go.mod").rename(source / "garden" / "go.sum")
    assert garden.garden_fingerprint(source) != first


def test_garden_fingerprint_refuses_symlinks(tmp_path: Path) -> None:
    source = _garden_tree(tmp_path / "source")
    (source / "garden" / "link").symlink_to(source / "pyproject.toml")
    with pytest.raises(InstallError, match="symlink"):
        garden.garden_fingerprint(source)


def test_garden_fingerprint_uses_cli_length_prefixed_framing(tmp_path: Path) -> None:
    source = _garden_tree(tmp_path / "source")
    expected = hashlib.sha256()
    for relative in ("cmd/main.go", "go.mod"):
        encoded = relative.encode()
        expected.update(len(encoded).to_bytes(4, "big"))
        expected.update(encoded)
        expected.update((source / "garden" / relative).read_bytes())
    assert garden.garden_fingerprint(source) == expected.hexdigest()


def test_revoke_reports_changed_saved_request_neutrally(
    tmp_path: Path, configuration: tuple[Path, dict[str, Any]]
) -> None:
    path, _ = configuration
    with (
        installation(tmp_path, garden.load_options(path, "native")) as ctx,
        authority(ctx) as fake,
    ):
        garden.prepare(ctx)
        garden.enrol(ctx, fake.admin)
        garden.revoke(ctx, fake.admin)
        ctx.state["garden"]["revocations"]["helper"]["operations"]["revoke-credential"][
            "body"
        ]["reason_code"] = "other"
        with pytest.raises(InstallError, match="administration request changed"):
            garden.revoke(ctx, fake.admin)
