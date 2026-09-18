from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from cairn_install.core import Context, InstallError
from cairn_install.docker import Backend, _check_loopback_port
from cairn_install.docker_assets import _SECRET_INIT, render_compose

OWNER_LABELS = {
    "io.cairn.install.instance": "11111111-1111-4111-8111-111111111111",
    "io.cairn.install.run": "22222222-2222-4222-8222-222222222222",
}

LOCAL_FALKORDB_IMAGE = "cairn.local/falkordb-runtime@sha256:" + "b" * 64


def test_local_runtime_compose_cannot_fall_back_to_registry(tmp_path: Path) -> None:
    import yaml

    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    ctx.outputs[("docker", "image", "inspect", LOCAL_FALKORDB_IMAGE)] = json.dumps(
        [
            {
                "RepoDigests": [LOCAL_FALKORDB_IMAGE],
                "Os": "linux",
                "Architecture": "amd64",
            }
        ]
    )
    backend = Backend(cast(Context, ctx))
    backend.prepare()

    services = yaml.safe_load(backend.compose_path.read_text())["services"]
    assert services["falkordb"]["image"] == LOCAL_FALKORDB_IMAGE
    assert services["falkordb"]["pull_policy"] == "never"
    assert (
        ctx.state["resources"]["docker"]["semantic"]["falkordb_image"]
        == LOCAL_FALKORDB_IMAGE
    )
    assert not any(command[:2] == ["docker", "pull"] for command, _, _ in ctx.commands)


@pytest.mark.parametrize("digests", [[], ["other@sha256:" + "b" * 64]])
def test_local_runtime_refuses_missing_exact_engine_digest(
    tmp_path: Path, digests: list[str]
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    ctx.outputs[("docker", "image", "inspect", LOCAL_FALKORDB_IMAGE)] = json.dumps(
        [{"RepoDigests": digests, "Os": "linux", "Architecture": "amd64"}]
    )
    backend = Backend(cast(Context, ctx))
    with pytest.raises(InstallError, match="local.*FalkorDB|FalkorDB.*local"):
        backend.prepare()
    assert not any(command[:2] == ["docker", "build"] for command, _, _ in ctx.commands)


def test_local_runtime_refuses_changed_retained_semantic_image(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    backend = Backend(cast(Context, ctx))
    ctx.state["resources"]["docker"]["semantic"] = {
        "falkordb_image": "other@sha256:" + "a" * 64
    }
    with pytest.raises(InstallError, match="FalkorDB.*changed"):
        backend.prepare()


def test_local_runtime_resume_refuses_changed_image_without_preflight(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    backend = Backend(cast(Context, ctx))
    ctx.state["resources"]["docker"]["semantic"] = {
        "falkordb_image": "other@sha256:" + "a" * 64
    }
    with pytest.raises(InstallError, match="FalkorDB.*changed"):
        backend.validate_ownership()


def test_legacy_semantic_resume_retains_its_recorded_falkordb_image(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    legacy = "ghcr.io/example/legacy-falkordb@sha256:" + "a" * 64
    backend = Backend(cast(Context, ctx))
    backend._docker["semantic"] = {"falkordb_image": legacy}  # noqa: SLF001

    assert backend._falkordb_image() == legacy  # noqa: SLF001


class FakeContext:
    def __init__(self, tmp_path: Path, *, semantic: bool = False) -> None:
        self.directory = tmp_path / "journal"
        self.root = tmp_path / "instance"
        self.source = tmp_path / "source"
        self.directory.mkdir()
        self.root.mkdir()
        self.source.mkdir()
        (self.source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        credentials = self.root / "credentials"
        credentials.mkdir(mode=0o700)
        provider = credentials / "openai-api-key"
        provider.write_text("provider-secret\n", encoding="utf-8")
        provider.chmod(0o600)
        self.state: dict[str, Any] = {
            "name": "alpha",
            "mode": "docker",
            "port": 18080,
            "semantic": semantic,
            "instance_id": OWNER_LABELS["io.cairn.install.instance"],
            "run_id": OWNER_LABELS["io.cairn.install.run"],
            "source_fingerprint": "abc123",
            "provider_key_file": str(provider),
            "resources": {},
            "owned_files": {},
            "steps": {},
            "receipts": {},
        }
        self.state["owned_files"][str(provider)] = hashlib.sha256(
            provider.read_bytes()
        ).hexdigest()
        self.commands: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.saved = 0
        self.checked: list[Path] = []
        self.secrets: list[str] = []
        self.outputs: dict[tuple[str, ...], str] = {}
        self.down = False

    @property
    def name(self) -> str:
        return str(self.state["name"])

    @property
    def mode(self) -> str:
        return str(self.state["mode"])

    @property
    def port(self) -> int:
        return int(self.state["port"])

    @property
    def semantic(self) -> bool:
        return bool(self.state["semantic"])

    @property
    def instance_id(self) -> str:
        return str(self.state["instance_id"])

    @property
    def run_id(self) -> str:
        return str(self.state["run_id"])

    def command(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        private: bool = False,
        stdout_path: Path | None = None,
        allowed: tuple[int, ...] = (0,),
    ) -> str:
        del timeout, private, stdout_path, allowed
        command = list(argv)
        self.commands.append((command, cwd, env))
        if command[-1:] == ["down"]:
            self.down = True
            return ""
        if self.down and command[:3] in (
            ["docker", "container", "ls"],
            ["docker", "network", "ls"],
        ):
            return ""
        exact = self.outputs.get(tuple(command))
        if exact is not None:
            return exact
        if (
            command[:3]
            in (["docker", "network", "inspect"], ["docker", "container", "inspect"])
            and command[-1].startswith("cairn-preflight-")
            and "{{json" in command[-2]
        ):
            return _labels_json(
                **{"io.cairn.install.preflight": "222222222222422282222222"}
            )
        if command[:3] == ["docker", "network", "inspect"] and command[-1].startswith(
            "cairn-preflight-"
        ):
            return "bridge false\n"
        if command[:2] == ["docker", "exec"]:
            return "ready\n"
        if command[:2] == ["docker", "run"] and any(
            value.startswith("cairn-preflight-client-") for value in command
        ):
            return "DNS_OK\nTCP_OK\nHTTP_OK\n"
        if command[:3] == ["docker", "container", "ls"]:
            return ""
        if command[:3] == ["docker", "network", "ls"]:
            return ""
        if command[:3] == ["docker", "volume", "ls"]:
            return ""
        if command[:3] == ["docker", "image", "ls"]:
            return ""
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": "sha256:image",
                        "Config": {"Labels": OWNER_LABELS},
                    }
                ]
            )
        return ""

    def write_file(
        self,
        path: Path,
        content: str,
        *,
        mode: int = 0o600,
        secret: bool = False,
    ) -> None:
        del secret
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        self.state["owned_files"][str(path)] = hashlib.sha256(
            content.encode()
        ).hexdigest()

    def check_file(self, path: Path) -> None:
        self.checked.append(path)
        if not path.is_file():
            raise InstallError(f"owned file is missing: {path}")

    def note(self, message: str) -> None:
        del message

    def save(self) -> None:
        self.saved += 1

    def add_secret(self, value: str) -> None:
        self.secrets.append(value)

    def read_secret(self, path: Path) -> str:
        value = path.read_text(encoding="utf-8").strip()
        self.add_secret(value)
        return value


def _labels_json(**extra: str) -> str:
    return json.dumps(OWNER_LABELS | extra)


def test_lifecycle_argv_pins_every_compose_input_outside_the_checkout(
    tmp_path: Path,
) -> None:
    """Dropping an explicit flag would let cwd or ambient Compose state redirect it."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))

    argv = backend.lifecycle_argv("verify")

    assert argv == [
        "docker",
        "compose",
        "--project-directory",
        str(ctx.root),
        "--env-file",
        str(ctx.root / "compose.env"),
        "-f",
        str(ctx.root / "compose.yaml"),
        "-p",
        "cairn-install-alpha",
        "run",
        "--rm",
        "--no-deps",
        "cairn",
        "verify",
        "--config",
        "/etc/cairn/config.yaml",
    ]


def test_preflight_rejects_an_unsupported_host_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowing Python 3.11 would violate the launcher's tested runtime boundary."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    monkeypatch.setattr(sys, "version_info", (3, 11, 9))

    with pytest.raises(InstallError, match="Python 3.12–3.14"):
        backend.preflight()


def test_port_probe_refuses_a_listener_but_allows_immediate_rebind_after_close() -> (
    None
):
    """TCP teardown state must not make a stopped Docker publication look live."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    with pytest.raises(InstallError, match="unavailable"):
        _check_loopback_port(port)

    client = socket.create_connection(("127.0.0.1", port))
    server, _ = listener.accept()
    server.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b""
    client.close()
    server.close()
    listener.close()

    _check_loopback_port(port)


def test_prepare_materialises_an_interpolation_free_locked_stack(
    tmp_path: Path,
) -> None:
    """Inherited COMPOSE/CAIRN variables must have no value the manifest can expand."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))

    backend.prepare()

    compose = (ctx.root / "compose.yaml").read_text(encoding="utf-8")
    config = (ctx.root / "config.yaml").read_text(encoding="utf-8")
    assert "${" not in compose
    assert "127.0.0.1:18080:8000" in compose
    assert f'io.cairn.install.instance: "{ctx.instance_id}"' in compose
    assert f'io.cairn.install.run: "{ctx.run_id}"' in compose
    assert "read_only: true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "source: cairn-data" in compose
    assert f"instance_id: {ctx.instance_id}" in config
    assert "  data: /var/lib/cairn" in config
    assert "  enabled: false" in config
    assert (ctx.root / "compose.env").read_text(encoding="utf-8") == "\n"
    build = next(
        command for command, _, _ in ctx.commands if command[:2] == ["docker", "build"]
    )
    assert build[-1] == str(ctx.source)
    assert "--pull=false" in build
    assert all(cwd == ctx.directory for _, cwd, _ in ctx.commands)
    assert all(env == {} for _, _, env in ctx.commands)
    assert any(
        command[-2:] == ["create", "--no-build"] for command, _, _ in ctx.commands
    )
    assert not any("up" in command for command, _, _ in ctx.commands)
    assert ctx.saved >= 2


def test_semantic_prepare_copies_secrets_through_an_isolated_init_container(
    tmp_path: Path,
) -> None:
    """Mounting the owner-only provider file directly into Cairn would make it unreadable."""
    ctx = FakeContext(tmp_path, semantic=True)
    backend = Backend(cast(Context, ctx))

    backend.prepare()

    compose = (ctx.root / "compose.yaml").read_text(encoding="utf-8")
    config = (ctx.root / "config.yaml").read_text(encoding="utf-8")
    lock = Path(__file__).resolve().parents[2] / "deploy/images.lock"
    locked_images = dict(
        line.split("=", 1)
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert f'image: "{locked_images["FALKORDB_IMAGE"]}"' in compose
    assert "network_mode: none" in compose
    assert "source: cairn-credentials" in compose
    assert "source: falkordb-config" in compose
    assert "source: falkordb-data" in compose
    assert 'source: "./credentials/openai-api-key"' in compose
    assert "provider-secret" not in compose
    assert "provider-secret" not in repr(ctx.commands)
    assert "  enabled: true\n  host: falkordb\n  port: 6379" in config
    assert "provider-secret" in ctx.secrets


def test_semantic_secret_publish_can_replay_after_interruption_before_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption must leave no truncated destination that blocks safe replay."""
    namespace: dict[str, Any] = {"__name__": "secret_init_test"}
    exec(_SECRET_INIT, namespace)  # noqa: S102 - execute the shipped init program
    publish = cast(Callable[[Path, bytes, int], None], namespace["publish"])
    real_fchown = os.fchown
    real_link = os.link
    monkeypatch.setattr(
        os,
        "fchown",
        lambda descriptor, _uid, _gid: real_fchown(
            descriptor, os.getuid(), os.getgid()
        ),
    )

    def interrupt_link(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    target = tmp_path / "volume" / "openai-api-key"
    target.parent.mkdir()
    monkeypatch.setattr(os, "link", interrupt_link)
    with pytest.raises(KeyboardInterrupt):
        publish(target, b"provider-secret\n", os.getuid())
    assert not target.exists()
    assert list(target.parent.glob(".install-*")) == []

    monkeypatch.setattr(os, "link", real_link)
    publish(target, b"provider-secret\n", os.getuid())
    assert target.read_bytes() == b"provider-secret\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400


def test_compose_preserves_a_literal_dollar_in_an_absolute_source_path(
    tmp_path: Path,
) -> None:
    """Compose interpolation must not turn a literal $HOME component into host HOME."""
    root = tmp_path / "$HOME" / "instance"
    root.mkdir(parents=True)
    compose_path = root / "compose.yaml"
    compose_path.write_text(
        render_compose(
            image="cairn-install-test:source",
            port=18080,
            root=root,
            instance_id=OWNER_LABELS["io.cairn.install.instance"],
            run_id=OWNER_LABELS["io.cairn.install.run"],
            semantic=False,
        ),
        encoding="utf-8",
    )
    env_file = root / "compose.env"
    env_file.write_text("\n", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "CAIRN_"))
    }

    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(compose_path),
            "-p",
            "cairn-install-dollar-test",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    document = json.loads(result.stdout)
    sources = {mount["source"] for mount in document["services"]["cairn"]["volumes"]}
    # `compose config` re-escapes a literal dollar as `$$` so its output can be
    # consumed by Compose again. It must neither expand HOME nor lose the path.
    assert str(root / "config.yaml").replace("$", "$$") in sources
    assert str(root / "credentials").replace("$", "$$") in sources
    assert not any(os.environ["HOME"] in source for source in sources)


def test_semantic_prepare_refuses_a_preexisting_unowned_index_password(
    tmp_path: Path,
) -> None:
    """An owner-readable file at the expected path is still not installer-owned."""
    ctx = FakeContext(tmp_path, semantic=True)
    password = ctx.root / "credentials" / "falkordb-password.source"
    password.parent.mkdir(mode=0o700, exist_ok=True)
    password.write_text("foreign-password\n", encoding="utf-8")
    password.chmod(0o600)
    backend = Backend(cast(Context, ctx))

    with pytest.raises(InstallError, match="unowned FalkorDB credential"):
        backend.prepare()


def test_validate_ownership_refuses_a_foreign_project_container(tmp_path: Path) -> None:
    """A container with only Compose's project label must never be adopted."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    ctx.outputs[
        (
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=cairn-install-alpha",
        )
    ] = "foreign-container\n"
    ctx.outputs[
        (
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            "foreign-container",
        )
    ] = _labels_json(**{"io.cairn.install.run": "foreign"})

    with pytest.raises(InstallError, match="foreign Docker container"):
        backend.validate_ownership()


def test_validate_ownership_refuses_to_recreate_a_missing_recorded_volume(
    tmp_path: Path,
) -> None:
    """Silently recreating a deleted named volume would attach an empty catalogue."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    ctx.state["resources"]["docker"]["volumes"] = {
        "cairn-data": "sha256:recorded-volume"
    }

    with pytest.raises(InstallError, match="recorded Docker volume is missing"):
        backend.validate_ownership()


def test_prepare_refuses_a_preexisting_unlabelled_volume(tmp_path: Path) -> None:
    """A matching Compose name alone provides no authority to adopt existing data."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    name = "cairn-install-alpha_cairn-data"
    ctx.outputs[
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"name=^{name}$",
        )
    ] = "foreign-volume\n"
    ctx.outputs[
        (
            "docker",
            "volume",
            "inspect",
            "--format",
            "{{json .Labels}}",
            "foreign-volume",
        )
    ] = json.dumps({"com.docker.compose.project": "cairn-install-alpha"})

    with pytest.raises(InstallError, match="foreign Docker volume"):
        backend.prepare()


def test_index_down_does_not_hide_a_running_cairn_lease_from_stop(
    tmp_path: Path,
) -> None:
    """Bootstrap must stop Cairn even when the semantic dependency is already down."""
    ctx = FakeContext(tmp_path, semantic=True)
    backend = Backend(cast(Context, ctx))
    project = "cairn-install-alpha"
    container_query = (
        "docker",
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={project}",
    )
    ctx.outputs[container_query] = "cairn-container\nfalkordb-container\n"
    for identifier, service in (
        ("cairn-container", "cairn"),
        ("falkordb-container", "falkordb"),
    ):
        ctx.outputs[
            (
                "docker",
                "container",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                identifier,
            )
        ] = _labels_json(
            **{
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            }
        )
    ctx.outputs[
        tuple([*backend._compose, "ps", "--status", "running", "--services"])
    ] = "cairn\n"

    assert backend.is_running() is True
    backend.stop()

    assert any(
        command[-3:] == ["stop", "--timeout", "60"] for command, _, _ in ctx.commands
    )


def test_rollback_cleans_a_recorded_interrupted_bridge_preflight(
    tmp_path: Path,
) -> None:
    """A killed installer must not strand its labelled probe on rollback."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    token = "222222222222422282222222"
    network = f"cairn-preflight-{token}"
    server = f"cairn-preflight-server-{token}"
    client = f"cairn-preflight-client-{token}"
    ctx.state["resources"]["docker"]["bridge_preflight"] = {
        "network": network,
        "server": server,
        "client": client,
        "label": token,
        "intent": "create",
    }
    for name in (client, server):
        ctx.outputs[
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                f"name=^/{name}$",
            )
        ] = name + "\n"
    ctx.outputs[
        (
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"name=^{network}$",
        )
    ] = network + "\n"

    backend.rollback()

    removed = [command for command, _, _ in ctx.commands]
    assert ["docker", "container", "rm", "--force", client] in removed
    assert ["docker", "container", "rm", "--force", server] in removed
    assert ["docker", "network", "rm", network] in removed
    assert (
        ctx.state["resources"]["docker"]["bridge_preflight"]["cleanup"]
        == "rollback_verified"
    )


def test_rollback_removes_only_owned_containers_and_network(tmp_path: Path) -> None:
    """Adding Compose's volume-removal switch would destroy preserved catalogue data."""
    ctx = FakeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    project = "cairn-install-alpha"
    ctx.outputs[
        (
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        )
    ] = "container-id\n"
    ctx.outputs[
        (
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            "container-id",
        )
    ] = _labels_json(
        **{
            "com.docker.compose.project": project,
            "com.docker.compose.service": "cairn",
        }
    )
    ctx.outputs[
        (
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"name=^{project}_default$",
        )
    ] = "network-id\n"
    ctx.outputs[
        (
            "docker",
            "network",
            "inspect",
            "--format",
            "{{json .Labels}}",
            "network-id",
        )
    ] = _labels_json(**{"com.docker.compose.project": project})
    ctx.outputs[
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"name=^{project}_cairn-data$",
        )
    ] = "volume-id\n"
    ctx.outputs[
        (
            "docker",
            "volume",
            "inspect",
            "--format",
            "{{json .Labels}}",
            "volume-id",
        )
    ] = _labels_json(**{"com.docker.compose.project": project})

    backend.rollback()

    down = next(command for command, _, _ in ctx.commands if command[-1:] == ["down"])
    assert "-v" not in down
    assert "--volumes" not in down
    assert (ctx.root / "config.yaml").exists()
    assert ctx.state["resources"]["docker"]["rollback"] == "containers_removed"
