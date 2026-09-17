from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from cairn_install import garden
from cairn_install.core import Context, InstallError
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
    (ctx.source / "a2a").mkdir()
    (ctx.source / "a2a" / "Dockerfile").write_text("FROM scratch\n")
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
        str(ctx.source / "a2a"),
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
