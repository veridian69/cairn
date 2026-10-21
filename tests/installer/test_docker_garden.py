from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from cairn_install import docker, garden, garden_upgrade, workflow
from cairn_install.core import Context, InstallError, open_context
from cairn_install.docker import Backend
from cairn_install.docker_assets import render_compose

from .test_docker import OWNER_LABELS, FakeContext
from .test_docker_blitz_backends import OWNER, DockerContext


def garden_context(tmp_path: Path) -> FakeContext:
    ctx = FakeContext(tmp_path)
    ctx.state["garden"] = {
        "options": {
            "port": 18443,
            "endpoint": "https://garden.example/mcp",
            "image": None,
        }
    }
    (ctx.source / "garden").mkdir()
    (ctx.source / "garden" / "Dockerfile").write_text("FROM scratch\n")
    for name in ("server.crt", "server.key"):
        ctx.write_file(
            ctx.root / "garden" / "tls" / name,
            "synthetic TLS\n",
            secret=name.endswith("key"),
        )
    return ctx


def test_compose_garden_uses_only_network_namespace_and_own_storage(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(
        render_compose(
            image="cairn:owned",
            garden_image="garden:owned",
            garden_port=18443,
            port=18080,
            root=tmp_path,
            instance_id=OWNER["io.cairn.install.instance"],
            run_id=OWNER["io.cairn.install.run"],
            semantic=False,
        )
    )
    services = document["services"]
    central = services["garden"]
    assert central["network_mode"] == "service:cairn"
    assert central["profiles"] == ["garden"]
    assert central["user"] == "65532:65532"
    assert "ports" not in central and "networks" not in central
    assert set(services["cairn"]["ports"]) == {
        "127.0.0.1:18080:8000",
        "0.0.0.0:18443:9443",
    }
    assert {mount["target"] for mount in central["volumes"]} == {
        "/var/lib/garden",
        "/etc/garden/host.json",
        "/var/run/secrets/garden",
    }
    assert all(
        mount["source"] not in {"cairn-data", "cairn-credentials", "./credentials"}
        for mount in central["volumes"]
    )
    initialise = services["garden-secret-init"]
    assert initialise["network_mode"] == "none" and initialise["profiles"] == ["garden"]
    assert {"garden-data", "garden-tls"} <= document["volumes"].keys()


def test_initial_prepare_builds_owned_garden_image_without_starting_profile(
    tmp_path: Path,
) -> None:
    ctx = garden_context(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    builds = [
        command for command, _, _ in ctx.commands if command[:2] == ["docker", "build"]
    ]
    assert {command[-1] for command in builds} == {
        str(ctx.source),
        str(ctx.source / "garden"),
    }
    assert ctx.state["resources"]["docker"]["garden_image"]["id"] == "sha256:image"
    assert not any("--profile" in command for command, _, _ in ctx.commands)
    assert not (ctx.root / "garden" / "host.json").exists()


def test_docker_refuses_an_operator_image_instead_of_ignoring_it(
    tmp_path: Path,
) -> None:
    ctx = garden_context(tmp_path)
    ctx.state["garden"]["options"]["image"] = "foreign/garden:latest"
    backend = Backend(cast(Context, ctx))
    with pytest.raises(InstallError, match="source build"):
        backend.prepare()
    assert not any(command[:2] == ["docker", "build"] for command, _, _ in ctx.commands)


class GardenLifecycleContext(FakeContext):
    def __init__(self, tmp_path: Path) -> None:
        base = garden_context(tmp_path)
        self.__dict__.update(base.__dict__)
        self.cairn = "cairn-one"
        self.central: str | None = None
        self.namespace = ""
        self.running = False
        self.generation = 0
        self.foreign = False

    def command(self, argv: Any, **kwargs: Any) -> str:
        command = list(argv)
        self.commands.append((command, kwargs.get("cwd"), kwargs.get("env")))
        if command[:3] == ["docker", "container", "ls"]:
            return "\n".join([self.cairn] + ([self.central] if self.central else []))
        if command[:3] == ["docker", "container", "inspect"]:
            if command[-2] == "{{.HostConfig.NetworkMode}}":
                return self.namespace
            labels = OWNER_LABELS | {
                "com.docker.compose.project": "cairn-install-alpha",
                "com.docker.compose.service": "cairn"
                if command[-1] == self.cairn
                else "garden",
            }
            if self.foreign and command[-1] == self.central:
                labels["io.cairn.install.instance"] = "foreign"
            return json.dumps(labels)
        if "--no-start" in command and command[-1] == "garden":
            self.generation += 1
            self.central = f"garden-{self.generation}"
            self.namespace = f"container:{self.cairn}"
        if "up" in command and "--no-start" not in command and command[-1] == "garden":
            self.running = True
        if "stop" in command and command[-1] == "garden":
            self.running = False
        if "ps" in command:
            return "cairn\ngarden\n" if self.running else "cairn\n"
        return super().command(argv, **kwargs)


def test_garden_recreates_namespace_after_cairn_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = GardenLifecycleContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    monkeypatch.setattr(
        garden,
        "gateway_config",
        lambda ctx, **kw: {**kw, "principals": {"test-principal": "alice"}},
    )
    backend.garden_prepare()
    backend.garden_start()
    first = ctx.central
    assert ctx.running and ctx.namespace == "container:cairn-one"
    backend.garden_stop()
    ctx.cairn = "cairn-two"
    backend.garden_start()
    assert (
        ctx.running and ctx.central != first and ctx.namespace == "container:cairn-two"
    )
    config = json.loads((ctx.root / "garden" / "host.json").read_text())
    assert config["gateway"]["cairn_url"] == "http://127.0.0.1:8000/memory/v1/diagnose"
    assert "token" not in (ctx.root / "garden" / "host.json").read_text()
    ctx.foreign = True
    before = len(ctx.commands)
    with pytest.raises(InstallError, match="foreign"):
        backend.garden_start()
    assert not any("up" in command for command, _, _ in ctx.commands[before:])


def test_blitz_validates_both_images_before_deleting_and_removes_garden_first(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    ctx.state["garden"] = {"options": {"port": 18443, "image": None}}
    backend = Backend(cast(Context, ctx))
    labels = OWNER | {"com.docker.compose.project": backend.project}
    ctx.containers["cairn-id"] = labels | {"com.docker.compose.service": "cairn"}
    ctx.containers["garden-id"] = labels | {"com.docker.compose.service": "garden"}
    for short in ("garden-data", "garden-tls"):
        ctx.volumes[f"{backend.project}_{short}"] = labels
    image: dict[str, Any] = {"Id": "sha256:garden", "Config": {"Labels": OWNER}}
    ctx.images[backend.garden_image] = image
    ctx.images["sha256:garden"] = image
    ctx.state["resources"]["docker"]["garden_image"] = {
        "reference": backend.garden_image,
        "id": "sha256:garden",
    }
    image["Config"]["Labels"] = OWNER | {"io.cairn.install.instance": "foreign"}
    with pytest.raises(InstallError, match="foreign"):
        backend.blitz()
    assert not any("rm" in command for command in ctx.commands)
    image["Config"]["Labels"] = OWNER
    backend.blitz()
    removals = [command for command in ctx.commands if "rm" in command]
    assert removals[0] == ["docker", "container", "rm", "--force", "garden-id"]
    assert ["docker", "image", "rm", "sha256:garden"] in removals
    assert not ctx.volumes


def test_namespace_mismatch_fails_before_start(tmp_path: Path) -> None:
    class WrongNamespace(GardenLifecycleContext):
        def command(self, argv: Any, **kwargs: Any) -> str:
            output = super().command(argv, **kwargs)
            if "--no-start" in argv:
                self.namespace = "container:foreign-cairn"
            return output

    ctx = WrongNamespace(tmp_path)
    backend = Backend(cast(Context, ctx))
    with pytest.raises(InstallError, match="network namespace"):
        backend.garden_start()
    assert not ctx.running


def test_rollback_includes_garden_profile_and_preserves_data(tmp_path: Path) -> None:
    ctx = garden_context(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    ctx.outputs[
        (
            "docker",
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=cairn-install-alpha",
        )
    ] = "garden-id\n"
    ctx.outputs[
        (
            "docker",
            "container",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            "garden-id",
        )
    ] = json.dumps(
        OWNER_LABELS
        | {
            "com.docker.compose.project": backend.project,
            "com.docker.compose.service": "garden",
        }
    )
    backend.rollback()
    down = [command for command, _, _ in ctx.commands if command[-1:] == ["down"]]
    assert len(down) == 1 and "--profile" in down[0] and "garden" in down[0]
    assert "--volumes" not in down[0]


def test_actual_compose_accepts_garden_and_excludes_it_by_default(
    tmp_path: Path,
) -> None:
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI unavailable")
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        render_compose(
            image="cairn:owned",
            garden_image="garden:owned",
            garden_port=18443,
            port=18080,
            root=tmp_path,
            instance_id="test-instance",
            run_id="test-run",
            semantic=False,
        )
    )
    command = ["docker", "compose", "-f", str(compose), "-p", "garden-test"]
    default = subprocess.run(
        [*command, "config", "--services"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert default.stdout.splitlines() == ["cairn"]
    all_services = subprocess.run(
        [*command, "--profile", "garden", "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    document = json.loads(all_services.stdout)
    assert document["services"]["garden"]["network_mode"] == "service:cairn"
    assert document["services"]["garden-secret-init"]["network_mode"] == "none"


# Digests of the renders produced by main 63030582 for these exact inputs.
# Regenerate only from main; a mismatch means an existing install would change.
MAIN_LEGACY_DIGESTS = {
    "plain compose": "7024ed87f012c2b79554801155cfa318d3ab1db10edd78063ad1352b6f670894",
    "garden compose": "29efcad94dc1c3ca129a6f4c5a8598dce2419a853316f2bdffa98c3035581a9e",
    "garden host.json": "f959748c05dbb8959b22de29bd771416a991eab7d051a350fbcd0481cc8a4c33",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_renders_are_byte_identical_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(garden, "gateway_config", lambda ctx, **kw: {"fixed": kw})
    plain = FakeContext(tmp_path)
    Backend(cast(Context, plain)).prepare()
    assert _digest(plain.root / "compose.yaml") == MAIN_LEGACY_DIGESTS["plain compose"]

    (tmp_path / "garden").mkdir()
    ctx = garden_context(tmp_path / "garden")
    backend = Backend(cast(Context, ctx))
    backend.prepare()
    assert _digest(ctx.root / "compose.yaml") == MAIN_LEGACY_DIGESTS["garden compose"]
    backend.garden_prepare()
    assert (
        _digest(ctx.root / "garden" / "host.json")
        == MAIN_LEGACY_DIGESTS["garden host.json"]
    )


@pytest.mark.parametrize(
    ("listen", "publish"),
    [
        ("0.0.0.0", "0.0.0.0:18443:9443"),
        ("192.0.2.10", "192.0.2.10:18443:9443"),
        ("::1", "[::1]:18443:9443"),
        ("2001:db8::7", "[2001:db8::7]:18443:9443"),
    ],
)
def test_garden_publish_address_uses_compose_port_syntax(
    tmp_path: Path, listen: str, publish: str
) -> None:
    document = yaml.safe_load(
        render_compose(
            image="cairn:owned",
            garden_image="garden:owned",
            garden_port=18443,
            garden_listen=listen,
            port=18080,
            root=tmp_path,
            instance_id=OWNER["io.cairn.install.instance"],
            run_id=OWNER["io.cairn.install.run"],
            semantic=False,
        )
    )
    assert set(document["services"]["cairn"]["ports"]) == {
        "127.0.0.1:18080:8000",
        publish,
    }


def test_listener_check_binds_the_configured_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, bool, str | None]] = []
    monkeypatch.setattr(
        garden,
        "require_listener_available",
        lambda port, *, wildcard, address=None: seen.append((port, wildcard, address)),
    )
    ctx = garden_context(tmp_path)
    backend = Backend(cast(Context, ctx))
    backend._check_garden_listener()  # noqa: SLF001
    ctx.state["garden"]["options"]["listen_address"] = "::1"
    backend._check_garden_listener()  # noqa: SLF001
    assert seen == [(18443, True, None), (18443, True, "::1")]


def test_added_retention_reaches_host_json_and_gateway_stays_wildcard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    def gateway(ctx: Any, **kwargs: Any) -> dict[str, bool]:
        captured.append(kwargs)
        return {"fixed": True}

    monkeypatch.setattr(garden, "gateway_config", gateway)
    ctx = garden_context(tmp_path)
    ctx.state["garden"]["options"] |= {
        "listen_address": "::1",
        "stream": dict(garden.ADD_RETENTION),
    }
    Backend(cast(Context, ctx)).garden_prepare()
    host = json.loads((ctx.root / "garden" / "host.json").read_text())
    assert host["stream"] == {"max_age": "168h", "max_bytes": 1073741824}
    assert captured[0]["listen"] == "0.0.0.0:9443"


class Interrupted(BaseException):
    """Stands in for SIGTERM or a crash at the chosen point."""


GARDEN_PORT = 18443
PLAIN_PORT = 18080


def _filter_value(command: Sequence[str], prefix: str) -> str | None:
    for index, value in enumerate(command[:-1]):
        if value == "--filter" and command[index + 1].startswith(prefix):
            return command[index + 1][len(prefix) :]
    return None


class DockerSim:
    """In-memory Docker daemon and one Compose project; no host Docker is used.

    Compose reads the compose file from disk on every call, so a rendered file that
    Compose would reject fails here too. Named volumes and containers in use cannot
    be removed, and a Cairn namespace cannot be replaced while a container joins it.
    """

    def __init__(self) -> None:
        self.images: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, str]] = {}
        self.log: list[list[str]] = []
        self.builds: list[str] = []
        self.interrupt: tuple[Callable[[list[str]], bool], str] | None = None
        self._counter = 0
        self._partial = False

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def command(self, argv: Sequence[str], **kwargs: Any) -> str:
        del kwargs
        command = list(argv)
        self.log.append(command)
        when = None
        if self.interrupt is not None and self.interrupt[0](command):
            when = self.interrupt[1]
            self.interrupt = None
        if when == "before":
            raise Interrupted(f"before {command[:4]}")
        self._partial = when == "partial"
        try:
            output = self._run(command)
        finally:
            self._partial = False
        if when == "after":
            raise Interrupted(f"after {command[:4]}")
        return output

    def running_services(self) -> set[str]:
        return {c["service"] for c in self.containers.values() if c["running"]}

    def garden_image_ids(self, reference: str) -> list[str]:
        return [i for i, image in self.images.items() if reference in image["refs"]]

    def _run(self, command: list[str]) -> str:
        if command[:2] == ["docker", "compose"]:
            return self._compose(command)
        if command[1:3] == ["network", "ls"]:
            return ""
        if command[1:3] == ["image", "ls"]:
            reference = _filter_value(command, "reference=")
            return "\n".join(
                image_id
                for image_id, image in self.images.items()
                if reference is None or reference in image["refs"]
            )
        if command[1:3] == ["image", "inspect"]:
            image_id = self._resolve_image(command[-1])
            labels = self.images[image_id]["labels"]
            return json.dumps([{"Id": image_id, "Config": {"Labels": labels}}])
        if command[1] == "build":
            built_labels: dict[str, str] = {}
            tag = ""
            for index, value in enumerate(command[:-1]):
                if value == "--label":
                    key, _, text = command[index + 1].partition("=")
                    built_labels[key] = text
                if value == "--tag":
                    tag = command[index + 1]
            image_id = f"sha256:{self._next():064x}"
            self.images[image_id] = {"labels": built_labels, "refs": {tag}}
            self.builds.append(tag)
            return ""
        if command[1:3] == ["image", "rm"]:
            image_id = self._resolve_image(command[-1])
            if any(c["image_id"] == image_id for c in self.containers.values()):
                raise InstallError(f"image is used by a container: {command[-1]}")
            del self.images[image_id]
            return ""
        if command[1:3] == ["container", "ls"]:
            project = _filter_value(command, "label=com.docker.compose.project=")
            ancestor = _filter_value(command, "ancestor=")
            return "\n".join(
                cid
                for cid, c in self.containers.items()
                if (
                    project is None
                    or c["labels"].get("com.docker.compose.project") == project
                )
                and (ancestor is None or ancestor in (c["image_id"], c["image_ref"]))
            )
        if command[1:3] == ["container", "inspect"]:
            container = self._container(command[-1])
            if "{{json .Config.Labels}}" in command:
                return json.dumps(container["labels"])
            return str(container["netmode"])
        if command[1:3] == ["container", "rm"]:
            container = self._container(command[-1])
            del self.containers[container["id"]]
            return ""
        if command[1:3] == ["volume", "ls"]:
            name = (_filter_value(command, "name=") or "").strip("^$")
            return "\n".join(v for v in self.volumes if not name or v == name)
        if command[1:3] == ["volume", "inspect"]:
            return json.dumps(self.volumes[command[-1]])
        if command[1:3] == ["volume", "rm"]:
            name = command[-1]
            if name not in self.volumes:
                raise InstallError(f"no such volume: {name}")
            if any(name in c["volumes"] for c in self.containers.values()):
                raise InstallError(f"volume is in use: {name}")
            del self.volumes[name]
            return ""
        raise InstallError(f"unsupported command in simulated daemon: {command}")

    def _resolve_image(self, identifier: str) -> str:
        for image_id, image in self.images.items():
            if identifier in (image_id, *image["refs"]):
                return image_id
        raise InstallError(f"no such image: {identifier}")

    def _container(self, identifier: str) -> dict[str, Any]:
        if identifier not in self.containers:
            raise InstallError(f"no such container: {identifier}")
        return self.containers[identifier]

    def _find(self, project: str, service: str) -> dict[str, Any] | None:
        for container in self.containers.values():
            labels = container["labels"]
            if (
                labels.get("com.docker.compose.project") == project
                and labels.get("com.docker.compose.service") == service
            ):
                return container
        return None

    def _compose(self, command: list[str]) -> str:
        document = yaml.safe_load(Path(command[command.index("-f") + 1]).read_text())
        project = command[command.index("-p") + 1]
        rest = command[command.index("-p") + 2 :]
        profiles: set[str] = set()
        while rest and rest[0] == "--profile":
            profiles.add(rest[1])
            rest = rest[2:]
        verb, args = rest[0], rest[1:]
        services = document["services"]
        if verb == "config":
            return ""
        if verb in {"up", "create"}:
            extra = ["--no-start"] if verb == "create" else []
            self._up(document, project, profiles, [*extra, *args])
            return ""
        if verb == "stop":
            names = {a for a in args if a in services}
            for c in self.containers.values():
                if c["labels"].get("com.docker.compose.project") == project and (
                    not names or c["service"] in names
                ):
                    c["running"] = False
            return ""
        if verb == "ps":
            return "\n".join(sorted(self.running_services()))
        raise InstallError(f"unsupported Compose verb in simulated daemon: {verb}")

    def _up(
        self,
        document: dict[str, Any],
        project: str,
        profiles: set[str],
        args: list[str],
    ) -> None:
        services = document["services"]
        force = "--force-recreate" in args
        no_start = "--no-start" in args
        named = list(dict.fromkeys(a for a in args if a in services))
        default = [
            name
            for name, service in services.items()
            if not service.get("profiles") or set(service["profiles"]) & profiles
        ]
        for name in named or default:
            service = services[name]
            if service.get("profiles") and not set(service["profiles"]) & profiles:
                raise InstallError(f"service {name} needs its profile to start")
            for mount in service.get("volumes", []):
                if mount["type"] == "volume":
                    self._ensure_volume(document, project, mount["source"])
            current = self._find(project, name)
            one_shot = service.get("restart") == "no"
            if current is not None and not force:
                if not no_start and not one_shot:
                    current["running"] = True
                continue
            if current is not None:
                for other in self.containers.values():
                    if other["netmode"] == f"container:{current['id']}":
                        raise InstallError(
                            f"{name} still joins the replaced container namespace"
                        )
                del self.containers[current["id"]]
                if self._partial:
                    raise Interrupted("old container removed, replacement not created")
            netmode = service.get("network_mode", "default")
            if netmode.startswith("service:"):
                owner = self._find(project, netmode.split(":", 1)[1])
                if owner is None:
                    raise InstallError(f"{name} has no network namespace owner")
                netmode = f"container:{owner['id']}"
            image_ids = self.garden_image_ids(service["image"]) or [
                i for i, img in self.images.items() if service["image"] in img["refs"]
            ]
            if not image_ids:
                raise InstallError(f"no such image for {name}: {service['image']}")
            labels = dict(service.get("labels", {}))
            labels |= {
                "com.docker.compose.project": project,
                "com.docker.compose.service": name,
            }
            cid = f"c{self._next():012x}"
            self.containers[cid] = {
                "id": cid,
                "service": name,
                "labels": labels,
                "running": not no_start and not one_shot,
                "image_id": image_ids[0],
                "image_ref": service["image"],
                "netmode": netmode,
                "volumes": [
                    f"{project}_{m['source']}"
                    for m in service.get("volumes", [])
                    if m["type"] == "volume"
                ],
            }

    def _ensure_volume(
        self, document: dict[str, Any], project: str, short: str
    ) -> None:
        name = f"{project}_{short}"
        if name not in self.volumes:
            labels = dict(document["volumes"][short].get("labels") or {})
            labels["com.docker.compose.project"] = project
            self.volumes[name] = labels


def _arm_publish(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    operation: str,
    when: str | None,
) -> list[str]:
    """Count publishes to target; with when set, interrupt the first one."""
    real = getattr(os, operation)
    published: list[str] = []
    armed = [when is not None]

    def publish(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(destination) != target:
            return real(source, destination, *args, **kwargs)
        if armed[0] and when == "before":
            armed[0] = False
            raise Interrupted(f"before publish {target}")
        result = real(source, destination, *args, **kwargs)
        published.append(str(target))
        if armed[0] and when == "after":
            armed[0] = False
            raise Interrupted(f"after publish {target}")
        return result

    monkeypatch.setattr(os, operation, publish)
    return published


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    listeners: list[str | None] = []
    monkeypatch.setattr(Backend, "_bridge_preflight", lambda self: None)
    monkeypatch.setattr(garden, "gateway_config", lambda ctx, **kw: {"fixed": kw})
    monkeypatch.setattr(
        garden,
        "require_listener_available",
        lambda port, *, wildcard, address=None: listeners.append(address),
    )
    return SimpleNamespace(
        listeners=listeners, monkeypatch=monkeypatch, sim=DockerSim()
    )


def _make_source(root: Path) -> None:
    source = root / "source"
    (source / "garden").mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM scratch\n")
    (source / "garden" / "Dockerfile").write_text("FROM scratch\n")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "drystane-cairn"\nversion = "7.8.9"\n'
    )


def _state_root(root: Path) -> Path:
    return root / "state"


def _bind(ctx: Context, sim: DockerSim) -> Backend:
    ctx.command = sim.command  # type: ignore[method-assign]
    return Backend(ctx)


def _open(root: Path, *, create: bool = False) -> Any:
    options: dict[str, Any] | None = None
    if create:
        options = {
            "mode": "docker",
            "port": PLAIN_PORT,
            "semantic": False,
            "source": str(root / "source"),
            "source_fingerprint": "a" * 64,
        }
    return open_context(_state_root(root), "alpha", create=options)


def _record_garden(ctx: Context, options: dict[str, Any] | None = None) -> None:
    ctx.state["garden"] = {
        "options": {
            "port": GARDEN_PORT,
            "endpoint": "https://garden.example/mcp",
            "image": None,
        }
        | (options or {}),
        "origin": "added",
    }
    for name in ("server.crt", "server.key"):
        ctx.write_file(
            ctx.root / "garden" / "tls" / name,
            f"synthetic {name}\n",
            secret=name.endswith("key"),
        )
    ctx.save()


def _plain_install(root: Path, daemon: SimpleNamespace) -> str:
    """A verified non-Garden Docker install; returns its pre-add Compose text."""
    _make_source(root)
    with _open(root, create=True) as ctx:
        backend = _bind(ctx, daemon.sim)
        backend.prepare()
        backend.start()
        return str((ctx.root / "compose.yaml").read_text())


def _expected_garden_compose(
    ctx: Context, backend: Backend, *, listen: str = "0.0.0.0"
) -> str:
    return render_compose(
        image=backend.image,
        port=ctx.port,
        root=ctx.root,
        instance_id=ctx.instance_id,
        run_id=ctx.run_id,
        semantic=False,
        garden_image=backend.garden_image,
        garden_port=GARDEN_PORT,
        garden_listen=listen,
        falkordb_local=False,
    )


def _expected_plain_compose(ctx: Context, backend: Backend) -> str:
    return render_compose(
        image=backend.image,
        port=ctx.port,
        root=ctx.root,
        instance_id=ctx.instance_id,
        run_id=ctx.run_id,
        semantic=False,
        falkordb_local=False,
    )


def _mutations(log: list[list[str]]) -> list[list[str]]:
    return [
        command
        for command in log
        if command[1:2] == ["build"]
        or command[1:3] in (["image", "rm"], ["container", "rm"], ["volume", "rm"])
        or (
            command[:2] == ["docker", "compose"]
            and {"up", "stop", "create"} & set(command)
        )
    ]


def _assert_attached(
    root: Path,
    daemon: SimpleNamespace,
    plain: str,
    *,
    listen: str = "0.0.0.0",
    builds: int = 1,
) -> None:
    sim = daemon.sim
    with _open(root) as ctx:
        backend = _bind(ctx, sim)
        assert (ctx.root / "compose.yaml").read_text() == _expected_garden_compose(
            ctx, backend, listen=listen
        )
        copy = ctx.root / "garden" / "attach" / "compose.pre-add.yaml"
        assert copy.read_text() == plain
        assert stat_mode(copy) == 0o600
        assert (
            ctx.state["garden"]["attach"]["pre_add_compose"]
            == hashlib.sha256(plain.encode()).hexdigest()
        )
        assert ctx.state["garden"]["attach"]["phase"] == "recreated"
        assert ctx.pending_replacements() == {}
    assert sim.builds.count(backend.garden_image) == builds
    assert sim.running_services() == {"cairn"}


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_attach_orders_journal_build_and_recreate_and_leaves_garden_stopped(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    seen: dict[str, tuple[bool, str]] = {}

    def probe(command: list[str]) -> bool:
        # Observes, never interrupts: the journal state when each step starts.
        if command[:2] == ["docker", "build"]:
            seen.setdefault("build", (copy.exists(), phase()))
        if "--force-recreate" in command and command[-1] == "cairn":
            seen.setdefault("recreate", (copy.exists(), phase()))
        return False

    with _open(tmp_path) as ctx:
        _record_garden(ctx, {"listen_address": "::1"})
        copy = ctx.root / "garden" / "attach" / "compose.pre-add.yaml"

        def phase() -> str:
            return str(ctx.state["garden"].get("attach", {}).get("phase", ""))

        daemon.sim.interrupt = (probe, "before")
        _bind(ctx, daemon.sim).garden_attach()
        daemon.sim.interrupt = None
        assert "[::1]:18443:9443" in (ctx.root / "compose.yaml").read_text()
        assert daemon.listeners == ["::1"]
    # The copy exists and the replacement is journaled before any build or recreate.
    assert seen["build"] == (True, "compose_replaced")
    assert seen["recreate"] == (True, "built")
    _assert_attached(tmp_path, daemon, plain, listen="::1")


ATTACH_PUBLISH_CRASHES = [
    ("copy", "before"),
    ("copy", "after"),
    ("compose", "before"),
    ("compose", "after"),
]
ATTACH_COMMAND_CRASHES = [
    ("build", "before"),
    ("build", "after"),
    ("config", "before"),
    ("recreate", "before"),
    ("recreate", "after"),
    ("recreate", "partial"),
]


def _command_crash(kind: str) -> Callable[[list[str]], bool]:
    predicates: dict[str, Callable[[list[str]], bool]] = {
        "build": lambda c: c[:2] == ["docker", "build"],
        "config": lambda c: c[-2:] == ["config", "--quiet"],
        "recreate": lambda c: "--force-recreate" in c and c[-1] == "cairn",
        "container-rm": lambda c: c[:3] == ["docker", "container", "rm"],
        "image-rm": lambda c: c[:3] == ["docker", "image", "rm"],
        "volume-rm": lambda c: c[:3] == ["docker", "volume", "rm"],
    }
    return predicates[kind]


def _attach_paths(root: Path) -> tuple[Path, Path]:
    with _open(root) as ctx:
        return (
            ctx.root / "compose.yaml",
            ctx.root / "garden" / "attach" / "compose.pre-add.yaml",
        )


@pytest.mark.parametrize(("target", "when"), ATTACH_PUBLISH_CRASHES)
def test_attach_resumes_after_each_publish_interruption(
    tmp_path: Path, daemon: SimpleNamespace, target: str, when: str
) -> None:
    plain = _plain_install(tmp_path, daemon)
    compose, copy = _attach_paths(tmp_path)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
    published = _arm_publish(
        daemon.monkeypatch,
        copy if target == "copy" else compose,
        operation="link" if target == "copy" else "replace",
        when=when,
    )
    with _open(tmp_path) as ctx:
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).garden_attach()
    _assert_attached(tmp_path, daemon, plain)
    assert len(published) == 1


@pytest.mark.parametrize(("kind", "when"), ATTACH_COMMAND_CRASHES)
def test_attach_resumes_after_each_command_interruption(
    tmp_path: Path, daemon: SimpleNamespace, kind: str, when: str
) -> None:
    plain = _plain_install(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
        daemon.sim.interrupt = (_command_crash(kind), when)
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).garden_attach()
    _assert_attached(tmp_path, daemon, plain)


def test_attach_pending_compose_replacement_passes_validation(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    compose, _ = _attach_paths(tmp_path)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
    _arm_publish(daemon.monkeypatch, compose, operation="replace", when="after")
    with _open(tmp_path) as ctx:
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    with _open(tmp_path) as ctx:
        assert str(compose) in ctx.pending_replacements()
        _bind(ctx, daemon.sim).validate_ownership()
        assert str(compose) in ctx.pending_replacements()


def test_attach_rerun_after_success_changes_nothing(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
        _bind(ctx, daemon.sim).garden_attach()
    daemon.sim.log.clear()
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).garden_attach()
    assert _mutations(daemon.sim.log) == []


def test_attach_refuses_to_run_over_an_unfinished_detach(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
        ctx.state["resources"]["docker"]["garden_detach"] = {
            "phase": "containers_removed",
            "delete_data": False,
            "cairn_running": True,
        }
        with pytest.raises(InstallError, match="detach is unfinished"):
            _bind(ctx, daemon.sim).garden_attach()


def _add_garden(root: Path, daemon: SimpleNamespace) -> None:
    """The add flow's Docker stages, with Garden stopped at the end."""
    with _open(root) as ctx:
        _record_garden(ctx)
        backend = _bind(ctx, daemon.sim)
        backend.garden_attach()
        backend.garden_prepare()
        backend.garden_start()
        backend.garden_stop()


def _detach(root: Path, daemon: SimpleNamespace, *, delete_data: bool) -> None:
    with _open(root) as ctx:
        _bind(ctx, daemon.sim).garden_detach(delete_data=delete_data)


def _assert_detached(
    root: Path, daemon: SimpleNamespace, plain: str, *, delete_data: bool
) -> None:
    sim = daemon.sim
    with _open(root) as ctx:
        backend = _bind(ctx, sim)
        assert (ctx.root / "compose.yaml").read_text() == plain
        for name in (
            "garden/host.json",
            "garden/tls/server.crt",
            "garden/tls/server.key",
            "garden/attach/compose.pre-add.yaml",
        ):
            assert not (ctx.root / name).exists(), name
            assert str(ctx.root / name) not in ctx.state["owned_files"], name
        assert "attach" not in ctx.state["garden"]
        assert ctx.pending_replacements() == {}
        assert ctx.state["resources"]["docker"]["garden_detach"]["phase"] == "complete"
        retained = ctx.state["resources"]["docker"].get("retained_garden_data")
        assert (retained == {"volume": f"{backend.project}_garden-data"}) == (
            not delete_data
        )
    assert {c["service"] for c in sim.containers.values()} == {"cairn"}
    assert sim.running_services() == {"cairn"}
    assert sim.garden_image_ids(backend.garden_image) == []
    assert f"{backend.project}_garden-tls" not in sim.volumes
    assert (f"{backend.project}_garden-data" in sim.volumes) is not delete_data


@pytest.mark.parametrize("delete_data", [False, True])
def test_detach_restores_the_exact_pre_add_compose_and_removes_garden(
    tmp_path: Path, daemon: SimpleNamespace, delete_data: bool
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    daemon.sim.log.clear()
    _detach(tmp_path, daemon, delete_data=delete_data)
    _assert_detached(tmp_path, daemon, plain, delete_data=delete_data)
    # Cairn's namespace is replaced only after the Garden container is gone; the
    # simulator refuses the recreate otherwise.
    # Second run changes nothing.
    daemon.sim.log.clear()
    _detach(tmp_path, daemon, delete_data=delete_data)
    assert _mutations(daemon.sim.log) == []
    _assert_detached(tmp_path, daemon, plain, delete_data=delete_data)


DETACH_COMMAND_CRASHES = [
    ("container-rm", "before"),
    ("recreate", "before"),
    ("recreate", "after"),
    ("recreate", "partial"),
    ("image-rm", "before"),
    ("volume-rm", "before"),
    ("volume-rm", "after"),
]


@pytest.mark.parametrize(("kind", "when"), DETACH_COMMAND_CRASHES)
def test_detach_resumes_after_each_command_interruption(
    tmp_path: Path, daemon: SimpleNamespace, kind: str, when: str
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    daemon.sim.interrupt = (_command_crash(kind), when)
    with pytest.raises(Interrupted):
        _detach(tmp_path, daemon, delete_data=True)
    _detach(tmp_path, daemon, delete_data=True)
    _assert_detached(tmp_path, daemon, plain, delete_data=True)


@pytest.mark.parametrize(
    ("target", "when"), [("compose", "before"), ("compose", "after")]
)
def test_detach_resumes_after_compose_restore_interruption(
    tmp_path: Path, daemon: SimpleNamespace, target: str, when: str
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    compose, _ = _attach_paths(tmp_path)
    _arm_publish(daemon.monkeypatch, compose, operation="replace", when=when)
    with pytest.raises(Interrupted):
        _detach(tmp_path, daemon, delete_data=False)
    _detach(tmp_path, daemon, delete_data=False)
    _assert_detached(tmp_path, daemon, plain, delete_data=False)


@pytest.mark.parametrize("when", ["before", "after"])
def test_detach_resumes_after_a_file_removal_interruption(
    tmp_path: Path, daemon: SimpleNamespace, when: str
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    armed = [True]
    real_unlink = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if armed[0] and self.name == "host.json":
            armed[0] = False
            if when == "before":
                raise Interrupted("before removing host.json")
            real_unlink(self, *args, **kwargs)
            raise Interrupted("after removing host.json")
        real_unlink(self, *args, **kwargs)

    daemon.monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(Interrupted):
        _detach(tmp_path, daemon, delete_data=False)
    _detach(tmp_path, daemon, delete_data=False)
    _assert_detached(tmp_path, daemon, plain, delete_data=False)


def test_detach_refuses_a_different_data_choice_when_resumed(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    daemon.sim.interrupt = (_command_crash("container-rm"), "before")
    with pytest.raises(Interrupted):
        _detach(tmp_path, daemon, delete_data=False)
    with pytest.raises(InstallError, match="same data choice"):
        _detach(tmp_path, daemon, delete_data=True)


def test_detach_of_garden_installed_with_cairn_renders_the_plain_layout(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _make_source(tmp_path)
    with _open(tmp_path, create=True) as ctx:
        _record_garden(ctx)
        backend = _bind(ctx, daemon.sim)
        backend.prepare()
        backend.start()
        backend.garden_prepare()
        backend.garden_start()
        backend.garden_stop()
    _detach(tmp_path, daemon, delete_data=False)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, daemon.sim)
        assert "attach" not in ctx.state["garden"]
        assert (ctx.root / "compose.yaml").read_text() == _expected_plain_compose(
            ctx, backend
        )
    assert {c["service"] for c in daemon.sim.containers.values()} == {"cairn"}
    assert f"{backend.project}_garden-data" in daemon.sim.volumes


def test_attach_works_again_after_detach(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    _detach(tmp_path, daemon, delete_data=False)
    with _open(tmp_path) as ctx:
        # The caller deletes Garden state once detach completes.
        del ctx.state["garden"]
        ctx.save()
    _add_garden(tmp_path, daemon)
    _assert_attached(tmp_path, daemon, plain, builds=2)
    # The data volume kept by the first detach is adopted, not recreated.
    assert any(name.endswith("_garden-data") for name in daemon.sim.volumes)


def test_stopped_cairn_stays_stopped_through_attach_and_detach(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).stop()
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
        _bind(ctx, daemon.sim).garden_attach()
    # Attach recreates Cairn but must not start it; start() owns that step.
    assert daemon.sim.running_services() == set()
    assert any(c["service"] == "cairn" for c in daemon.sim.containers.values())
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).start()
    assert daemon.sim.running_services() == {"cairn"}

    with _open(tmp_path) as ctx:
        backend = _bind(ctx, daemon.sim)
        backend.garden_prepare()
        backend.garden_start()
        backend.garden_stop()
        backend.stop()
    _detach(tmp_path, daemon, delete_data=False)
    # Detach mirrors the Cairn state it found: a stopped Cairn stays stopped.
    assert daemon.sim.running_services() == set()
    with _open(tmp_path) as ctx:
        assert (ctx.root / "compose.yaml").read_text() == plain


def _docker_record(root: Path) -> dict[str, Any]:
    with _open(root) as ctx:
        record: dict[str, Any] = copy.deepcopy(ctx.state["resources"]["docker"])
        return record


def test_kept_data_is_retained_outside_garden_state_and_blitz_removes_it(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    _detach(tmp_path, daemon, delete_data=False)
    name = next(iter(n for n in daemon.sim.volumes if n.endswith("_garden-data")))
    record = _docker_record(tmp_path)
    assert record["retained_garden_data"] == {"volume": name}
    assert "garden-data" not in record["volumes"]
    # The caller deletes Garden state after detach; blitz must still find the data.
    with _open(tmp_path) as ctx:
        del ctx.state["garden"]
        ctx.save()
        _bind(ctx, daemon.sim).blitz()
    assert name not in daemon.sim.volumes
    assert not any(n.endswith("_garden-tls") for n in daemon.sim.volumes)
    record = _docker_record(tmp_path)
    assert "retained_garden_data" not in record
    assert record["blitz"] == "complete"


def test_readd_reuses_the_retained_volume_with_one_receipt(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    name = next(iter(n for n in daemon.sim.volumes if n.endswith("_garden-data")))
    original = daemon.sim.volumes[name]
    _detach(tmp_path, daemon, delete_data=False)
    with _open(tmp_path) as ctx:
        del ctx.state["garden"]
        ctx.save()
    _add_garden(tmp_path, daemon)
    # Compose mounted the same named volume: same object, not a replacement.
    assert daemon.sim.volumes[name] is original
    record = _docker_record(tmp_path)
    assert record["volumes"]["garden-data"] == name
    assert "retained_garden_data" not in record
    assert sum(1 for n in daemon.sim.volumes if n.endswith("_garden-data")) == 1


def test_delete_detach_leaves_no_retained_record(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    _detach(tmp_path, daemon, delete_data=True)
    assert "retained_garden_data" not in _docker_record(tmp_path)
    assert not any(n.endswith("_garden-data") for n in daemon.sim.volumes)


def test_detach_removes_owned_participant_profile_bundles(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        profile = ctx.root / "garden" / "profiles" / "alice"
        ctx.write_file(profile / "README.md", "bundle\n")
        ctx.write_file(profile / "agent.token", "token\n", secret=True)
        files = [profile / "README.md", profile / "agent.token"]
    _detach(tmp_path, daemon, delete_data=False)
    with _open(tmp_path) as ctx:
        for path in files:
            assert not path.exists()
            assert str(path) not in ctx.state["owned_files"]
            assert str(path) not in ctx.state.get("file_intents", {})
        assert not (ctx.root / "garden" / "profiles").exists()
    _assert_detached(tmp_path, daemon, plain, delete_data=False)


def test_pending_compose_refuses_content_matching_neither_journal_digest(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    _plain_install(tmp_path, daemon)
    compose, _ = _attach_paths(tmp_path)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
    _arm_publish(daemon.monkeypatch, compose, operation="replace", when="before")
    with _open(tmp_path) as ctx:
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    compose.write_text("services: {}\n")  # foreign content, neither old nor new
    with _open(tmp_path) as ctx:
        assert str(compose) in ctx.pending_replacements()
        with pytest.raises(InstallError, match="unexpected content"):
            _bind(ctx, daemon.sim).validate_ownership()


@pytest.mark.parametrize("when", ["before", "after"])
def test_pending_compose_accepts_either_journal_digest(
    tmp_path: Path, daemon: SimpleNamespace, when: str
) -> None:
    _plain_install(tmp_path, daemon)
    compose, _ = _attach_paths(tmp_path)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
    _arm_publish(daemon.monkeypatch, compose, operation="replace", when=when)
    with _open(tmp_path) as ctx:
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).validate_ownership()
        assert str(compose) in ctx.pending_replacements()


@pytest.mark.parametrize(
    "relative", ["garden/host.json", "garden/attach/compose.pre-add.yaml"]
)
def test_pending_non_compose_file_is_refused_by_validation(
    tmp_path: Path, daemon: SimpleNamespace, relative: str
) -> None:
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    # The pre-add copy is outside the validated path list; it must still be refused.
    host = tmp_path / "state" / "alpha" / "instance" / relative
    with _open(tmp_path) as ctx:
        digest = ctx.state["owned_files"][str(host)]
        ctx.state["file_replacements"] = {str(host): {"old": digest, "new": "0" * 64}}
        ctx.save()
    with _open(tmp_path) as ctx:
        with pytest.raises(InstallError, match="interrupted replacement"):
            _bind(ctx, daemon.sim).validate_ownership()


def test_attach_recovers_when_the_old_cairn_was_removed_and_not_replaced(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        _record_garden(ctx)
        daemon.sim.interrupt = (_command_crash("recreate"), "partial")
        with pytest.raises(Interrupted):
            _bind(ctx, daemon.sim).garden_attach()
    assert not any(c["service"] == "cairn" for c in daemon.sim.containers.values())
    with _open(tmp_path) as ctx:
        _bind(ctx, daemon.sim).garden_attach()
    _assert_attached(tmp_path, daemon, plain)


def test_detach_recovers_when_the_old_cairn_was_removed_and_not_replaced(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    plain = _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    daemon.sim.interrupt = (_command_crash("recreate"), "partial")
    with pytest.raises(Interrupted):
        _detach(tmp_path, daemon, delete_data=False)
    assert not any(c["service"] == "cairn" for c in daemon.sim.containers.values())
    _detach(tmp_path, daemon, delete_data=False)
    _assert_detached(tmp_path, daemon, plain, delete_data=False)


def test_detach_removes_owned_garden_credential_captures(
    tmp_path: Path, daemon: SimpleNamespace
) -> None:
    """A kept capture would make a later garden-add resume the revoked token."""
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    with _open(tmp_path) as ctx:
        capture = ctx.root / "credentials" / "garden" / "spike" / "issue.json"
        ctx.write_file(capture, '{"retained": true}\n', secret=True)
        token = capture.with_name("agent.token")
        ctx.write_file(token, "token\n", secret=True)
    _detach(tmp_path, daemon, delete_data=False)
    with _open(tmp_path) as ctx:
        assert not capture.exists()
        assert not token.exists()
        credentials = str(ctx.root / "credentials" / "garden")
        for table in ("owned_files", "file_intents"):
            assert not [
                key for key in ctx.state.get(table, {}) if key.startswith(credentials)
            ]


SNAPSHOT_ID = "20261019T210000Z-test-20261019t210000z"


class GardenHookSim(DockerSim):
    """Run the emitted Python volume helpers locally; Docker remains simulated."""

    def __init__(self, volume: Path) -> None:
        super().__init__()
        self.volume = volume
        (volume / "data").mkdir(parents=True)
        (volume / "data/state.db").write_bytes(b"old database\n")
        self.version = "garden version 7.8.9\n"
        self.snapshot_output = f"snapshot created: {SNAPSHOT_ID} (12 KiB)\n"
        self.interrupt_snapshot = False
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def command(self, argv: Sequence[str], **kwargs: Any) -> str:
        command = list(argv)
        self.calls.append((command, kwargs))
        if command[:2] == ["docker", "run"]:
            self.log.append(command)
            if command[-1] == "--version":
                return self.version
            offset = command.index("-c")
            code = command[offset + 1].replace("/src", str(self.volume))
            input_path = kwargs.get("stdin_path")
            result: subprocess.CompletedProcess[bytes] = subprocess.run(
                [sys.executable, "-c", code, *command[offset + 2 :]],
                input=input_path.read_bytes() if input_path else None,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise InstallError(result.stderr.decode())
            output = kwargs.get("stdout_path")
            if output:
                with output.open("xb") as stream:
                    os.chmod(output, 0o600)
                    stream.write(result.stdout)
                return ""
            return result.stdout.decode()
        if command[:2] == ["docker", "compose"] and "snapshot" in command:
            self.log.append(command)
            root = self.volume / "backups" / SNAPSHOT_ID
            root.mkdir(parents=True, exist_ok=True)
            (root / "manifest.json").write_text('{"format_version": 1}\n')
            (root / "data").mkdir(exist_ok=True)
            (root / "data/state.db").write_bytes(b"snapshot database\n")
            if self.interrupt_snapshot:
                self.interrupt_snapshot = False
                raise KeyboardInterrupt
            return self.snapshot_output
        return super().command(argv, **kwargs)


@pytest.fixture
def hook_daemon(tmp_path: Path, daemon: SimpleNamespace) -> SimpleNamespace:
    daemon.sim = GardenHookSim(tmp_path / "volume")
    _plain_install(tmp_path, daemon)
    _add_garden(tmp_path, daemon)
    return daemon


def _cold_copy(ctx: Context, backend: Backend) -> Path:
    ctx.state["garden"]["upgrade"] = {"id": "upgrade-1", "phase": "stopped"}
    path = ctx.root / "garden-upgrades/upgrade-1/data.tar"
    backend.garden_copy(path)
    ctx.state["garden"]["upgrade"]["cold_copy"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }
    ctx.save()
    return path


def _assert_volume_argv(
    command: list[str], backend: Backend, *, writable: bool
) -> None:
    assert command[:3] == ["docker", "run", "--rm"]
    for option, value in (
        ("--network", "none"),
        ("--user", "65532:65532"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--entrypoint", "python"),
        ("-v", f"{backend.project}_garden-data:/src:{'rw' if writable else 'ro'}"),
    ):
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command and "--privileged" not in command
    assert {command[i + 1] for i, arg in enumerate(command) if arg == "--label"} == {
        f"{key}={value}" for key, value in backend._owner_labels.items()
    }
    assert command[command.index("python") + 1] == backend.image


def test_garden_stage_builds_new_source_and_checks_version_once(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    new = tmp_path / "new"
    new.mkdir()
    _make_source(new)
    source = new / "source"
    (source / "garden/Dockerfile").write_text("FROM scratch\n# new\n")
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        old = backend.garden_image
        artefact = backend.garden_stage(source)
        assert artefact == {
            "image": f"{backend.project}-garden:g-{garden.garden_fingerprint(source)[:22]}",
            "id": hook_daemon.sim.garden_image_ids(artefact["image"])[0],
        }
        # Only a published release is reused; the engine discards the rest.
        ctx.state["garden"]["previous"] = {"artefact": artefact}
        assert backend.garden_stage(source) == artefact
        assert hook_daemon.sim.builds.count(artefact["image"]) == 1
        build = next(
            c for c in hook_daemon.sim.log if "--tag" in c and artefact["image"] in c
        )
        assert build[-1] == str(source / "garden")
        assert build[build.index("--build-arg") + 1] == "VERSION=7.8.9"
        assert (
            f"io.cairn.source.digest=sha256:{garden.garden_fingerprint(source)}"
            in build
        )
        assert backend.garden_image == old
        version = [c for c in hook_daemon.sim.log if c[-1] == "--version"][-1]
        assert version[-3:] == ["/usr/local/bin/garden", artefact["id"], "--version"]
        hook_daemon.sim.version = "garden version dev\n"
        with pytest.raises(InstallError, match="unexpected version"):
            backend.garden_stage(source)


def test_garden_stage_refuses_replaced_or_wrong_source_image(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefact = backend.garden_stage(ctx.source)
        ctx.state["garden"]["previous"] = {"artefact": artefact}
        image = hook_daemon.sim.images[artefact["id"]]
        image["labels"]["io.cairn.source.digest"] = "sha256:wrong"
        with pytest.raises(InstallError, match="fingerprint changed"):
            backend.garden_stage(ctx.source)
        image["labels"]["io.cairn.install.run"] = "foreign"
        with pytest.raises(InstallError, match="foreign"):
            backend.garden_stage(ctx.source)


def test_garden_copy_uses_private_large_capture_and_records_hash_and_listing(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    # This exceeds Context.check_file/read_owned's 2 MiB bound as well.
    (hook_daemon.sim.volume / "data/state.db").write_bytes(b"x" * (3 * 1024**2))
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        path = _cold_copy(ctx, backend)
        backend.garden_copy(path)
        captures = [
            (c, kw) for c, kw in hook_daemon.sim.calls if kw.get("stdout_path") == path
        ]
        assert len(captures) == 1
        command, kwargs = captures[0]
        _assert_volume_argv(command, backend, writable=False)
        assert kwargs["stdout_limit"] == 8 * 1024**3 and kwargs["private"]
        assert stat_mode(path) == 0o600 and stat_mode(path.parent) == 0o700
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert ctx.state["owned_files"][str(path)] == expected
        assert backend._docker["garden_captures"][str(path)] == {
            "sha256": expected,
            "bytes": path.stat().st_size,
        }
        with tarfile.open(path) as archive:
            assert "data/state.db" in archive.getnames()
        path.write_bytes(path.read_bytes() + b"changed")
        with pytest.raises(InstallError, match="changed"):
            backend.garden_copy(path)


def test_garden_copy_syncs_its_directory_before_the_receipt(
    tmp_path: Path, hook_daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        synced: list[Path] = []

        def sync(path: Path) -> None:
            assert not any(Path(key).parent == path for key in ctx.state["owned_files"])
            synced.append(path)

        monkeypatch.setattr(docker, "_fsync_directory", sync)
        path = _cold_copy(ctx, backend)
        assert synced == [path.parent]


@pytest.mark.parametrize("ownership", ["owned", "intent", "capture", "partial"])
@pytest.mark.parametrize("missing", [False, True])
def test_delete_data_clears_copy_records(
    tmp_path: Path, hook_daemon: SimpleNamespace, ownership: str, missing: bool
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        path = _cold_copy(ctx, backend)
        key = str(path)
        digest = ctx.state["owned_files"].pop(key)
        capture = backend._docker["garden_captures"].pop(key)
        if ownership == "owned":
            ctx.state["owned_files"][key] = digest
        elif ownership == "intent":
            ctx.state.setdefault("file_intents", {})[key] = digest
        elif ownership == "capture":
            backend._docker["garden_captures"][key] = capture
        else:
            backend._docker["garden_captures"][key] = {"intent": "capture"}
            path.write_bytes(b"partial capture")
        if missing:
            path.unlink()
            path.parent.rmdir()
            path.parent.parent.rmdir()
        ctx.save()
        backend._delete_garden_copies()
        backend._delete_garden_copies()
        assert not path.exists()
        for table in (
            ctx.state["owned_files"],
            ctx.state.get("file_intents", {}),
            backend._docker["garden_captures"],
        ):
            assert key not in table


def test_delete_data_removes_an_abandoned_capture(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        path = ctx.root / "garden-upgrades/upgrade-1/data.tar"
        path.parent.mkdir(mode=0o700, parents=True)
        path.parent.parent.chmod(0o700)
        path.write_bytes(b"partial capture")
        path.chmod(0o600)
        backend._docker.setdefault("garden_captures", {})[str(path)] = {
            "intent": "capture"
        }
        ctx.save()
        outside = ctx.root / "keep"
        outside.write_bytes(b"unrelated")
        outside.chmod(0o600)
        escape = f"{ctx.root}/garden-upgrades/../keep"
        backend._docker["garden_captures"][escape] = {"intent": "capture"}
        with pytest.raises(InstallError, match="not normalised"):
            backend._delete_garden_copies()
        assert outside.read_bytes() == b"unrelated"
        del backend._docker["garden_captures"][escape]
        backend._delete_garden_copies()
        assert not (ctx.root / "garden-upgrades").exists()
        assert str(path) not in backend._docker["garden_captures"]


def test_garden_copy_needs_twice_the_measured_volume_size(
    tmp_path: Path, hook_daemon: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    (hook_daemon.sim.volume / "data/state.db").write_bytes(b"x" * 4096)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        ctx.state["garden"]["upgrade"] = {"id": "upgrade-1", "phase": "stopped"}
        path = ctx.root / "garden-upgrades/upgrade-1/data.tar"
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _: SimpleNamespace(total=0, used=0, free=8191),
        )
        with pytest.raises(InstallError, match="twice the data size"):
            backend.garden_copy(path)
        assert not path.exists()
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _: SimpleNamespace(total=0, used=0, free=1 << 40),
        )
        backend.garden_copy(path)
        assert path.exists()


@pytest.mark.parametrize("bad", ["missing", "symlink", "fifo"])
def test_garden_copy_refuses_missing_database_and_nonregular_data(
    tmp_path: Path, hook_daemon: SimpleNamespace, bad: str
) -> None:
    data = hook_daemon.sim.volume / "data"
    (data / "state.db").unlink()
    if bad == "symlink":
        (data / "state.db").symlink_to(tmp_path / "outside")
    if bad == "fifo":
        os.mkfifo(data / "state.db")
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        with pytest.raises(InstallError, match="lacks|non-regular"):
            _cold_copy(ctx, backend)
        assert not any("data.tar" in key for key in ctx.state["owned_files"])


def test_garden_swap_keeps_both_receipts_and_source_rendering_survives_reopen(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        original = backend.compose_path.read_bytes()
        old = dict(backend._docker["garden_image"])
        artefact = backend.garden_stage(ctx.source)
        cairn = copy.deepcopy(hook_daemon.sim._find(backend.project, "cairn"))
        backend.garden_swap(artefact)
        backend.garden_swap(artefact)
        assert backend._docker["garden_images"] == {
            "current": {"reference": artefact["image"], "id": artefact["id"]},
            "previous": old,
        }
        assert backend.compose_path.read_bytes() != original
        assert (
            yaml.safe_load(backend.compose_path.read_text())["services"]["garden"][
                "image"
            ]
            == artefact["image"]
        )
        assert hook_daemon.sim._find(backend.project, "cairn") == cairn
        ctx.state["garden"]["source"] = {"artefact": artefact}
        ctx.save()
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        assert (
            backend._compose_text(garden_enabled=True).encode()
            == backend.compose_path.read_bytes()
        )
        for receipt in backend._docker["garden_images"].values():
            image = hook_daemon.sim.images[receipt["id"]]
            image["labels"]["io.cairn.install.run"] = "foreign"
            with pytest.raises(InstallError, match="foreign"):
                backend.validate_ownership()
            image["labels"]["io.cairn.install.run"] = ctx.run_id
        with pytest.raises(InstallError, match="recorded owned"):
            backend.garden_swap({"image": "foreign:latest", "id": "sha256:foreign"})
        # Resume would rebuild from the Cairn source, not the Garden source.
        del hook_daemon.sim.images[backend._docker["garden_images"]["current"]["id"]]
        with pytest.raises(InstallError, match="Upgraded Garden image is missing"):
            backend.validate_ownership()


def test_garden_swap_resumes_a_landed_compose_replacement(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefact = backend.garden_stage(ctx.source)
        _arm_publish(
            hook_daemon.monkeypatch,
            backend.compose_path,
            operation="replace",
            when="after",
        )
        with pytest.raises(Interrupted):
            backend.garden_swap(artefact)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        backend.garden_swap(artefact)
        assert ctx.pending_replacements() == {}
        assert (
            backend._docker["garden_images"]["previous"]["reference"]
            == backend._legacy_garden_image
        )


@pytest.mark.parametrize("choice", ["restore", "keep"])
def test_rollback_recovers_missing_current_image(
    tmp_path: Path, hook_daemon: SimpleNamespace, choice: str
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        path = _cold_copy(ctx, backend)
        cold_copy = ctx.state["garden"]["upgrade"]["cold_copy"]
        previous = dict(backend._docker["garden_image"])
        artefact = backend.garden_stage(ctx.source)
        backend.garden_swap(artefact)
        before = {"artefact": {"image": previous["reference"], "id": previous["id"]}}
        ctx.state["garden"]["source"] = {"artefact": artefact}
        ctx.state["garden"]["previous"] = before
        journal = {
            "id": "rollback-1",
            "direction": "rollback",
            "phase": "recorded",
            "from": {"artefact": artefact},
            "to": before,
            "new_started": False,
            "data_choice": choice,
            "cold_copy": cold_copy,
            "aside": None,
            "generation_before": {"val": "generation-1"},
        }
        ctx.state["garden"]["upgrade"] = journal
        ctx.state["garden"]["options"]["participants"] = {"val": "codex"}
        ctx.save()
        del hook_daemon.sim.images[artefact["id"]]
        (hook_daemon.sim.volume / "data/state.db").write_bytes(b"new database\n")

    def verify(ctx: Context, adapter: workflow.GardenAdapter) -> None:
        assert "garden" in hook_daemon.sim.running_services()
        ctx.state["garden"]["verification"] = {"val": {"generation": "generation-1"}}

    hook_daemon.monkeypatch.setattr(workflow, "_verify_garden", verify)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        garden_upgrade._rollback(
            ctx, cast(workflow.GardenAdapter, backend), ctx.state["garden"]["upgrade"]
        )
        assert backend._docker["garden_images"]["current"] == previous
        assert ctx.state["garden"]["source"] == before
        assert path.exists()
        assert (hook_daemon.sim.volume / "data/state.db").read_bytes() == (
            b"old database\n" if choice == "restore" else b"new database\n"
        )


@pytest.mark.parametrize("landed", [False, True])
def test_first_upgrade_swaps_from_the_legacy_image(
    tmp_path: Path, hook_daemon: SimpleNamespace, landed: bool
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        legacy = dict(backend._docker["garden_image"])
        assert "garden_images" not in backend._docker
        artefact = backend.garden_stage(ctx.source)
        target = {"artefact": artefact}
        ctx.state["garden"]["upgrade"] = {
            "id": "upgrade-1",
            "direction": "upgrade",
            "phase": "swapped",
            "from": {"artefact": {"image": legacy["reference"], "id": legacy["id"]}},
            "to": target,
            "new_started": False,
            "data_choice": None,
            "cold_copy": None,
            "aside": None,
            "generation_before": {"val": "generation-1"},
        }
        ctx.state["garden"]["options"]["participants"] = {"val": "codex"}
        ctx.save()
        backend.garden_stop()
        if landed:
            # Interrupted after the swap landed, before the source was published.
            backend.garden_swap(artefact)

    def verify(ctx: Context, adapter: workflow.GardenAdapter) -> None:
        assert "garden" in hook_daemon.sim.running_services()
        ctx.state["garden"]["verification"] = {"val": {"generation": "generation-1"}}

    hook_daemon.monkeypatch.setattr(workflow, "_verify_garden", verify)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        garden_upgrade._select_and_verify(
            ctx, cast(workflow.GardenAdapter, backend), ctx.state["garden"]["upgrade"]
        )
        assert backend._docker["garden_images"] == {
            "current": {"reference": artefact["image"], "id": artefact["id"]},
            "previous": legacy,
        }
        assert ctx.state["garden"]["source"] == target
        assert "garden" in hook_daemon.sim.running_services()


@pytest.mark.parametrize(
    "invalid",
    ["upgrade", "complete", "different_target", "missing_target", "foreign_target"],
)
def test_missing_current_image_guard_requires_owned_retained_rollback_target(
    tmp_path: Path, hook_daemon: SimpleNamespace, invalid: str
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        previous = dict(backend._docker["garden_image"])
        artefact = backend.garden_stage(ctx.source)
        backend.garden_swap(artefact)
        target = {"image": previous["reference"], "id": previous["id"]}
        journal = {
            "direction": "rollback",
            "phase": "stopped",
            "to": {"artefact": target},
        }
        ctx.state["garden"]["upgrade"] = journal
        del hook_daemon.sim.images[artefact["id"]]
        if invalid == "upgrade":
            journal["direction"] = "upgrade"
        elif invalid == "complete":
            journal["phase"] = "complete"
        elif invalid == "different_target":
            target["id"] = "sha256:" + "f" * 64
        elif invalid == "missing_target":
            del hook_daemon.sim.images[previous["id"]]
        else:
            hook_daemon.sim.images[previous["id"]]["labels"]["io.cairn.install.run"] = (
                "foreign"
            )
        before = len(hook_daemon.sim.log)
        with pytest.raises(InstallError, match="missing|foreign"):
            backend.garden_stop()
        assert not _mutations(hook_daemon.sim.log[before:])


def test_legacy_resume_can_rebuild_a_missing_image(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        previous = dict(backend._docker["garden_image"])
        del hook_daemon.sim.images[previous["id"]]
        backend.prepare()
        rebuilt = backend._docker["garden_image"]
        assert rebuilt["reference"] == previous["reference"]
        assert rebuilt["id"] != previous["id"]
        assert rebuilt["id"] in hook_daemon.sim.images


def test_garden_restore_streams_stdin_and_keeps_one_aside_on_retry(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        path = _cold_copy(ctx, backend)
        (hook_daemon.sim.volume / "data/state.db").write_bytes(b"new database\n")
        backend.garden_restore_data(path)
        backend.garden_restore_data(path)
        volume = hook_daemon.sim.volume
        assert (volume / "data/state.db").read_bytes() == b"old database\n"
        assert (
            volume / "data.pre-restore-upgrade-1/state.db"
        ).read_bytes() == b"new database\n"
        assert not list(volume.glob("data.restore-*"))
        assert (
            ctx.state["garden"]["upgrade"]["aside"]
            == "/var/lib/garden/data.pre-restore-upgrade-1"
        )
        command, kwargs = [
            (c, kw) for c, kw in hook_daemon.sim.calls if kw.get("stdin_path")
        ][-1]
        _assert_volume_argv(command, backend, writable=True)
        assert "--interactive" in command and "--tty" not in command
        assert kwargs["stdin_path"] == path and kwargs["private"]
        # A retry after the publication landed revalidates it against the copy.
        (volume / "data/extra").write_bytes(b"")
        with pytest.raises(InstallError, match="changed after interrupted restore"):
            backend.garden_restore_data(path)
        (volume / "data/extra").unlink()
        (volume / "data/state.db").write_bytes(b"later database\n")
        with pytest.raises(InstallError, match="changed after interrupted restore"):
            backend.garden_restore_data(path)
        (volume / "data/state.db").write_bytes(b"old database\n")
        backend.garden_restore_data(path)
        ctx.state["garden"]["upgrade"]["cold_copy"]["sha256"] = "bad"
        with pytest.raises(InstallError, match="digest, size or path"):
            backend.garden_restore_data(path)


def test_snapshot_retry_recovers_by_name_without_receipt(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        hook_daemon.sim.interrupt_snapshot = True
        with pytest.raises(KeyboardInterrupt):
            backend.garden_snapshot("test-20261019t210000z")
        snapshot = hook_daemon.sim.volume / "backups" / SNAPSHOT_ID
        (snapshot / "data/state.db").write_bytes(b"retained snapshot\n")
        backend = _bind(ctx, hook_daemon.sim)
        assert backend.garden_snapshot("test-20261019t210000z") == SNAPSHOT_ID
        assert (snapshot / "data/state.db").read_bytes() == b"retained snapshot\n"
        assert len([c for c in hook_daemon.sim.log if "snapshot" in c]) == 1
        listing = [c for c in hook_daemon.sim.log if c[:2] == ["docker", "run"]][-1]
        _assert_volume_argv(listing, backend, writable=False)


def test_snapshot_lookup_does_not_confuse_overlapping_names(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        (hook_daemon.sim.volume / "backups" / "20261019T110000Z-weekly-backup1").mkdir(
            parents=True
        )
        assert backend.garden_snapshot("backup1") != "20261019T110000Z-weekly-backup1"
        assert [c for c in hook_daemon.sim.log if "snapshot" in c]


def test_snapshot_refuses_multiple_matching_names(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        root = hook_daemon.sim.volume / "backups"
        for stamp in ("20261019T120000Z", "20261019T120001Z"):
            (root / f"{stamp}-backup1").mkdir(parents=True)
        with pytest.raises(InstallError, match="ambiguous"):
            backend.garden_snapshot("backup1")
        assert not [c for c in hook_daemon.sim.log if "snapshot" in c]


def test_garden_snapshot_export_and_delete_use_expected_commands(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        snapshot_id = backend.garden_snapshot("test-20261019t210000z")
        assert snapshot_id == SNAPSHOT_ID
        command = next(c for c in hook_daemon.sim.log if "snapshot" in c)
        assert command[len(backend._compose) :] == [
            "--profile",
            "garden",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/usr/local/bin/garden",
            "garden",
            "snapshot",
            "create",
            "--host-config",
            "/etc/garden/host.json",
            "--backup-dir",
            "/var/lib/garden/backups",
            "test-20261019t210000z",
        ]
        dest = ctx.root / "garden-backups/export"
        backend.garden_export(snapshot_id, dest)
        backend.garden_export(snapshot_id, dest)
        assert (dest / "data/state.db").read_bytes() == b"snapshot database\n"
        assert stat_mode(dest / "manifest.json") == 0o600
        captures = [
            (c, kw)
            for c, kw in hook_daemon.sim.calls
            if kw.get("stdout_path") and "backups/" + snapshot_id in c
        ]
        assert len(captures) == 2
        for c, kw in captures:
            _assert_volume_argv(c, backend, writable=False)
            assert kw["stdout_limit"] == 8 * 1024**3 and kw["private"]
        assert not list(dest.glob(".garden-export-*"))
        backend.garden_snapshot_delete(snapshot_id)
        backend.garden_snapshot_delete(snapshot_id)
        assert not (hook_daemon.sim.volume / "backups" / snapshot_id).exists()
        _assert_volume_argv(hook_daemon.sim.log[-1], backend, writable=True)
        hook_daemon.sim.snapshot_output = "snapshot created: ../../escape (1 B)\n"
        with pytest.raises(InstallError, match="snapshot id"):
            backend.garden_snapshot("test")


@pytest.mark.parametrize("operation", ["garden_export", "garden_snapshot_delete"])
@pytest.mark.parametrize(
    "identifier", ["../escape", "/absolute", "--option", "a/b", ""]
)
def test_snapshot_paths_are_validated_before_commands(
    tmp_path: Path, hook_daemon: SimpleNamespace, operation: str, identifier: str
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        before = len(hook_daemon.sim.log)
        args: list[Any] = [identifier]
        if operation == "garden_export":
            args.append(ctx.root / "export")
        with pytest.raises(InstallError, match="Invalid Garden snapshot id"):
            getattr(backend, operation)(*args)
        assert len(hook_daemon.sim.log) == before


def test_garden_prune_only_removes_unreferenced_owned_builds_and_is_idempotent(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefacts = []
        for number in range(3):
            (ctx.source / "garden/Dockerfile").write_text(f"FROM scratch\n# {number}\n")
            artefacts.append(backend.garden_stage(ctx.source))
        ctx.state["garden"]["previous"] = {"artefact": artefacts[0]}
        backend.garden_prune([artefacts[2]])
        assert artefacts[0]["id"] in hook_daemon.sim.images
        assert artefacts[1]["id"] not in hook_daemon.sim.images
        assert artefacts[2]["id"] in hook_daemon.sim.images
        assert artefacts[1]["image"] not in backend._docker["garden_builds"]
        before = len([c for c in hook_daemon.sim.log if c[1:3] == ["image", "rm"]])
        backend.garden_prune([artefacts[2]])
        assert (
            len([c for c in hook_daemon.sim.log if c[1:3] == ["image", "rm"]]) == before
        )
        del ctx.state["garden"]["previous"]
        hook_daemon.sim.images[artefacts[0]["id"]]["labels"][
            "io.cairn.install.instance"
        ] = "foreign"
        with pytest.raises(InstallError, match="foreign"):
            backend.garden_prune([artefacts[2]])
        assert artefacts[0]["id"] in hook_daemon.sim.images


def test_garden_prune_after_rollback_removes_the_rejected_release(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefacts = []
        for number in range(2):
            (ctx.source / "garden/Dockerfile").write_text(f"FROM scratch\n# {number}\n")
            artefacts.append(backend.garden_stage(ctx.source))
        restored, rejected = artefacts
        backend.garden_swap(restored)
        backend.garden_swap(rejected)
        backend.garden_swap(restored)
        # _finish prunes before the rollback journal becomes terminal.
        ctx.state["garden"]["source"] = {"artefact": restored}
        ctx.state["garden"]["upgrade"] = {
            "id": "rollback-1",
            "direction": "rollback",
            "phase": "verified",
            "from": {"artefact": rejected},
            "to": {"artefact": restored},
        }
        backend.garden_prune([restored])
        assert restored["id"] in hook_daemon.sim.images
        assert rejected["id"] not in hook_daemon.sim.images
        assert rejected["image"] not in backend._docker["garden_builds"]


@pytest.mark.parametrize("delete_data", [False, True])
def test_detach_handles_upgrade_copies_previous_image_and_volume_asides(
    tmp_path: Path, hook_daemon: SimpleNamespace, delete_data: bool
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        cold = _cold_copy(ctx, backend)
        previous_id = backend._docker["garden_image"]["id"]
        artefact = backend.garden_stage(ctx.source)
        backend.garden_swap(artefact)
        ctx.state["garden"]["source"] = {"artefact": artefact}
        backend.garden_restore_data(cold)
        backend.garden_detach(delete_data=delete_data)
        backend.garden_detach(delete_data=delete_data)
        assert cold.exists() is not delete_data
        assert (previous_id in hook_daemon.sim.images) is not delete_data
        assert artefact["id"] not in hook_daemon.sim.images
        assert (
            f"{backend.project}_garden-data" in hook_daemon.sim.volumes
        ) is not delete_data
        if delete_data:
            assert not (ctx.root / "garden-upgrades").exists()
            assert "garden_images" not in backend._docker
        else:
            # Blitz must still find the retained image after garden-remove clears state.
            del ctx.state["garden"]
            backend.blitz()
            assert previous_id not in hook_daemon.sim.images


@pytest.mark.parametrize("delete_data", [False, True])
@pytest.mark.parametrize("when", ["before", "after"])
def test_detach_resumes_after_upgraded_image_removal_lands(
    tmp_path: Path, hook_daemon: SimpleNamespace, delete_data: bool, when: str
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        plain = backend.attach_copy_path.read_text()
        artefact = backend.garden_stage(ctx.source)
        backend.garden_swap(artefact)
        ctx.state["garden"]["source"] = {"artefact": artefact}
        hook_daemon.sim.interrupt = (
            lambda command: (
                command[:3] == ["docker", "image", "rm"]
                and command[-1] == artefact["id"]
            ),
            when,
        )
        with pytest.raises(Interrupted):
            backend.garden_detach(delete_data=delete_data)
    assert (artefact["id"] not in hook_daemon.sim.images) == (when == "after")

    with _open(tmp_path) as ctx:
        # The public remove workflow stops Garden before calling detach on retry.
        backend = _bind(ctx, hook_daemon.sim)
        assert (
            backend._docker["garden_image_removal"]["receipt"]["id"] == artefact["id"]
        )
        backend.garden_stop()
    _detach(tmp_path, hook_daemon, delete_data=delete_data)
    _assert_detached(tmp_path, hook_daemon, plain, delete_data=delete_data)


@pytest.mark.parametrize("build_intent", [False, True])
def test_detach_accepts_an_absent_legacy_image_without_an_identity(
    tmp_path: Path, hook_daemon: SimpleNamespace, build_intent: bool
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        plain = backend.attach_copy_path.read_text()
        receipt = backend._docker["garden_image"]
        del hook_daemon.sim.images[receipt.pop("id")]
        if build_intent:
            receipt["intent"] = "build"
        ctx.save()
    _detach(tmp_path, hook_daemon, delete_data=False)
    _assert_detached(tmp_path, hook_daemon, plain, delete_data=False)


@pytest.mark.parametrize(
    "operation",
    [
        "garden_stage",
        "garden_copy",
        "garden_swap",
        "garden_restore_data",
        "garden_snapshot",
        "garden_export",
        "garden_prune",
        "garden_snapshot_delete",
    ],
)
def test_new_hooks_refuse_kubernetes_without_commands(
    tmp_path: Path, operation: str
) -> None:
    ctx = garden_context(tmp_path)
    backend = Backend(cast(Context, ctx))
    ctx.state["mode"] = "kubernetes"
    arguments: dict[str, tuple[Any, ...]] = {
        "garden_stage": (ctx.source,),
        "garden_copy": (ctx.root / "data.tar",),
        "garden_swap": ({"image": "unused", "id": "unused"},),
        "garden_restore_data": (ctx.root / "data.tar",),
        "garden_snapshot": ("test",),
        "garden_export": (SNAPSHOT_ID, ctx.root / "export"),
        "garden_prune": ([],),
        "garden_snapshot_delete": (SNAPSHOT_ID,),
    }
    with pytest.raises(InstallError, match="Docker mode"):
        getattr(backend, operation)(*arguments[operation])
    assert ctx.commands == []


def test_copy_retries_an_interrupted_capture_without_adopting_partial_tar(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        cold = _cold_copy(ctx, backend)
        # Even a valid tar with state.db is not proof the helper finished adding
        # every member. A capture intent must rerun the command after a crash.
        backend._docker["garden_captures"][str(cold)] = {"intent": "capture"}
        del ctx.state["owned_files"][str(cold)]
        (hook_daemon.sim.volume / "data/extra.db").write_bytes(b"also required")
        ctx.save()
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        backend.garden_copy(cold)
        with tarfile.open(cold) as archive:
            assert "data/extra.db" in archive.getnames()
        cold.write_bytes(b"truncated tar")
        backend._docker["garden_captures"][str(cold)] = {"intent": "capture"}
        del ctx.state["owned_files"][str(cold)]
        backend.garden_copy(cold)
        with tarfile.open(cold) as archive:
            assert "data/state.db" in archive.getnames()


@pytest.mark.parametrize("when", ["during", "after"])
def test_export_recovers_partial_write_or_landed_member_publication(
    tmp_path: Path, hook_daemon: SimpleNamespace, when: str
) -> None:
    import shutil

    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        snapshot_id = backend.garden_snapshot("test")
        dest = ctx.root / "export"
        target = dest / "manifest.json"
        if when == "after":
            _arm_publish(
                hook_daemon.monkeypatch, target, operation="link", when="after"
            )
        else:
            real = shutil.copyfileobj
            armed = [True]

            def interrupted(source: Any, output: Any, length: int = 0) -> None:
                if armed[0]:
                    armed[0] = False
                    output.write(source.read(1))
                    raise Interrupted("during export file write")
                real(source, output, length)

            hook_daemon.monkeypatch.setattr(shutil, "copyfileobj", interrupted)
        with pytest.raises(Interrupted):
            backend.garden_export(snapshot_id, dest)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        backend.garden_export(snapshot_id, dest)
        assert target.read_text() == '{"format_version": 1}\n'
        backend._check_garden_file(target)
        assert not list(dest.rglob(".export-*"))


def test_second_upgrade_preserves_legacy_image_receipt_for_delete_data(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        legacy = dict(backend._docker["garden_image"])
        artefacts = []
        for number in range(2):
            (ctx.source / "garden/Dockerfile").write_text(f"FROM scratch\n# {number}\n")
            artefact = backend.garden_stage(ctx.source)
            backend.garden_swap(artefact)
            ctx.state["garden"]["source"] = {"artefact": artefact}
            artefacts.append(artefact)
        ctx.state["garden"]["previous"] = {"artefact": artefacts[0]}
        backend.garden_prune(artefacts)
        assert backend._docker["garden_builds"][legacy["reference"]] == legacy
        backend.garden_detach(delete_data=True)
        assert legacy["id"] not in hook_daemon.sim.images
        assert not any(a["id"] in hook_daemon.sim.images for a in artefacts)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("cleanup", ["blitz", "garden_prune"])
def test_interrupted_staged_build_reconciles_receipt_before_cleanup(
    tmp_path: Path, hook_daemon: SimpleNamespace, when: str, cleanup: str
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        reference = (
            f"{backend.project}-garden:g-{garden.garden_fingerprint(ctx.source)[:22]}"
        )
        hook_daemon.sim.interrupt = (lambda c: c[:2] == ["docker", "build"], when)
        with pytest.raises(Interrupted):
            backend.garden_stage(ctx.source)
        assert backend._docker["garden_builds"][reference]["intent"] == "build"
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        if cleanup == "blitz":
            backend.blitz()
        else:
            backend.garden_prune([])
        assert hook_daemon.sim.garden_image_ids(reference) == []


def test_garden_stage_refuses_an_unpublished_build_that_survived_the_discard(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefact = backend.garden_stage(ctx.source)
        hook_daemon.sim.containers["c-leftover"] = {
            "id": "c-leftover",
            "service": "leftover",
            "labels": {},
            "running": False,
            "image_id": artefact["id"],
            "image_ref": artefact["image"],
            "netmode": "none",
            "volumes": [],
        }
        backend.garden_prune([])
        assert artefact["image"] in backend._docker["garden_builds"]
        builds = len(hook_daemon.sim.builds)
        with pytest.raises(InstallError, match="could not be discarded"):
            backend.garden_stage(ctx.source)
        assert len(hook_daemon.sim.builds) == builds
        del hook_daemon.sim.containers["c-leftover"]
        backend.garden_prune([])
        assert backend.garden_stage(ctx.source)["image"] == artefact["image"]
        assert len(hook_daemon.sim.builds) == builds + 1
        # A published release under the same reference is reused, not refused.
        ctx.state["garden"]["previous"] = {"artefact": artefact}
        backend.garden_stage(ctx.source)


def test_prune_protects_same_immutable_image_in_another_record(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        artefact = backend.garden_stage(ctx.source)
        backend.garden_prune([{"image": "another:reference", "id": artefact["id"]}])
        assert artefact["id"] in hook_daemon.sim.images
        # No known receipt may be inferred merely from the project's tag pattern.
        foreign_ref = f"{backend.project}-garden:g-{'f' * 22}"
        hook_daemon.sim.images["sha256:unknown"] = {
            "labels": backend._owner_labels,
            "refs": {foreign_ref},
        }
        backend.garden_prune([artefact])
        assert "sha256:unknown" in hook_daemon.sim.images


@pytest.mark.parametrize(
    "bad", ["traversal", "absolute", "link", "duplicate", "truncated"]
)
def test_cold_copy_tar_validation_refuses_unsafe_members(
    tmp_path: Path, bad: str
) -> None:
    import io

    path = tmp_path / "copy.tar"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("data/state.db")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"db\n"))
        member = tarfile.TarInfo(
            {
                "traversal": "data/../outside",
                "absolute": "/outside",
                "link": "data/link",
                "duplicate": "data/state.db",
                "truncated": "data/big.db",
            }[bad]
        )
        if bad == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
        archive.addfile(member)
    path.chmod(0o600)
    if bad == "truncated":
        # Preserve the second header but claim more payload than the file holds.
        raw = bytearray(path.read_bytes())
        header = tarfile.TarInfo("data/big.db")
        header.size = 100000
        raw[1024:1536] = header.tobuf()
        path.write_bytes(raw)
    with pytest.raises(InstallError, match="Unsafe|Truncated|validate"):
        Backend._garden_tar(path, "data", cold=True)


def test_export_process_death_reclaims_recorded_work_before_retry(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    import shutil

    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        snapshot_id = backend.garden_snapshot("test")
        dest = ctx.root / "export"
        child = os.fork()
        if child == 0:

            def die(source: Any, output: Any, length: int = 0) -> None:
                output.write(source.read(1))
                output.flush()
                os._exit(99)

            hook_daemon.monkeypatch.setattr(shutil, "copyfileobj", die)
            backend.garden_export(snapshot_id, dest)
            os._exit(98)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 99
        work = dest / f".garden-export-{snapshot_id}"
        assert list(work.glob(".export-*"))
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        backend.garden_export(snapshot_id, dest)
        assert {p.relative_to(dest).as_posix() for p in dest.rglob("*")} == {
            "manifest.json",
            "data",
            "data/state.db",
        }
        assert not backend._docker["garden_exports"]


def test_export_needs_no_write_access_to_destination_parent(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        snapshot_id = backend.garden_snapshot("test")
        parent = tmp_path / "exports"
        parent.mkdir(mode=0o700)
        dest = parent / "owned"
        dest.mkdir(mode=0o700)
        parent.chmod(0o500)
        try:
            backend.garden_export(snapshot_id, dest)
            assert (dest / "manifest.json").is_file()
            assert not list(dest.glob(".garden-export-*"))
        finally:
            parent.chmod(0o700)


@pytest.mark.parametrize("recorded", [False, True])
def test_export_refuses_unowned_or_symlink_working_directory(
    tmp_path: Path, hook_daemon: SimpleNamespace, recorded: bool
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        snapshot_id = backend.garden_snapshot("test")
        dest = ctx.root / "export"
        dest.mkdir(mode=0o700)
        work = dest / f".garden-export-{snapshot_id}"
        outside = ctx.root / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "keep"
        sentinel.write_text("keep")
        if recorded:
            backend._docker["garden_exports"] = {str(dest): snapshot_id}
            work.symlink_to(outside, target_is_directory=True)
        else:
            work.mkdir(mode=0o700)
            (work / "keep").write_text("unowned")
        with pytest.raises(InstallError, match="unowned|symlinks refused"):
            backend.garden_export(snapshot_id, dest)
        assert sentinel.read_text() == "keep"
        assert work.exists()


def test_readd_after_upgrade_uses_live_image_as_the_next_previous_receipt(
    tmp_path: Path, hook_daemon: SimpleNamespace
) -> None:
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        first = backend.garden_stage(ctx.source)
        backend.garden_swap(first)
        ctx.state["garden"]["source"] = {"artefact": first}
        backend.garden_detach(delete_data=False)
        del ctx.state["garden"]
        ctx.save()
    _add_garden(tmp_path, hook_daemon)
    with _open(tmp_path) as ctx:
        backend = _bind(ctx, hook_daemon.sim)
        live = dict(backend._docker["garden_image"])
        (ctx.source / "garden/Dockerfile").write_text("FROM scratch\n# rebuild\n")
        backend.garden_swap(backend.garden_stage(ctx.source))
        assert backend._docker["garden_images"]["previous"] == live
        assert live["id"] in hook_daemon.sim.images
