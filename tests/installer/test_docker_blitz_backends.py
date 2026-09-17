from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from cairn_install.core import Context, InstallError
from cairn_install.docker import Backend

OWNER = {
    "io.cairn.install.instance": "11111111-1111-4111-8111-111111111111",
    "io.cairn.install.run": "22222222-2222-4222-8222-222222222222",
}


class DockerContext:
    def __init__(self, tmp_path: Path) -> None:
        self.directory = tmp_path / "journal"
        self.root = tmp_path / "root"
        self.source = tmp_path / "source"
        self.directory.mkdir()
        self.root.mkdir()
        self.source.mkdir()
        self.state: dict[str, Any] = {
            "name": "alpha",
            "mode": "docker",
            "semantic": False,
            "instance_id": OWNER["io.cairn.install.instance"],
            "run_id": OWNER["io.cairn.install.run"],
            "source_fingerprint": "abc123",
            "resources": {},
            "owned_files": {},
        }
        self.commands: list[list[str]] = []
        self.containers: dict[str, dict[str, str]] = {}
        self.networks: dict[str, tuple[str, dict[str, str]]] = {}
        self.volumes: dict[str, dict[str, str]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.image_users: list[str] = []

    @property
    def name(self) -> str:
        return str(self.state["name"])

    @property
    def mode(self) -> str:
        return str(self.state["mode"])

    @property
    def semantic(self) -> bool:
        return bool(self.state["semantic"])

    @property
    def instance_id(self) -> str:
        return str(self.state["instance_id"])

    @property
    def run_id(self) -> str:
        return str(self.state["run_id"])

    def command(self, argv: Sequence[str], **kwargs: Any) -> str:
        del kwargs
        command = list(argv)
        self.commands.append(command)
        if command[:3] == ["docker", "container", "ls"]:
            selector = command[-1]
            if selector.startswith("label=com.docker.compose.project="):
                project = selector.rsplit("=", 1)[1]
                return "\n".join(
                    identifier
                    for identifier, labels in self.containers.items()
                    if labels.get("com.docker.compose.project") == project
                )
            if selector.startswith("ancestor="):
                return "\n".join(self.image_users)
            return ""
        if command[:3] == ["docker", "network", "ls"]:
            name = command[-1].removeprefix("name=^").removesuffix("$")
            return "\n".join(
                identifier
                for identifier, (network_name, _) in self.networks.items()
                if network_name == name
            )
        if command[:3] == ["docker", "volume", "ls"]:
            name = command[-1].removeprefix("name=^").removesuffix("$")
            return name if name in self.volumes else ""
        if command[:3] == ["docker", "image", "ls"]:
            identifiers = {str(image["Id"]) for image in self.images.values()}
            if command[-1].startswith("reference="):
                reference = command[-1].removeprefix("reference=")
                image = self.images.get(reference)
                return "" if image is None else str(image["Id"])
            return "\n".join(sorted(identifiers))
        if (
            command[:3]
            in (
                ["docker", "container", "inspect"],
                ["docker", "network", "inspect"],
                ["docker", "volume", "inspect"],
            )
            and "--format" in command
        ):
            identifier = command[-1]
            if command[1] == "container":
                labels = self.containers[identifier]
            elif command[1] == "network":
                labels = self.networks[identifier][1]
            else:
                labels = self.volumes[identifier]
            return json.dumps(labels)
        if command[:3] == ["docker", "image", "inspect"]:
            image = self.images.get(command[-1])
            return "[]" if image is None else json.dumps([image])
        if command[:4] == ["docker", "container", "rm", "--force"]:
            self.containers.pop(command[-1], None)
            return command[-1]
        if command[:3] == ["docker", "network", "rm"]:
            self.networks.pop(command[-1], None)
            return command[-1]
        if command[:3] == ["docker", "volume", "rm"]:
            self.volumes.pop(command[-1], None)
            return command[-1]
        if command[:3] == ["docker", "image", "rm"]:
            identifier = command[-1]
            image = self.images.get(identifier)
            if image is not None:
                self.images = {
                    key: value
                    for key, value in self.images.items()
                    if value is not image
                }
            return identifier
        return ""

    def save(self) -> None:
        pass

    def note(self, message: str) -> None:
        del message


def _owned_project_labels() -> dict[str, str]:
    return OWNER | {
        "com.docker.compose.project": "cairn-install-alpha",
        "com.docker.compose.service": "cairn",
    }


def _image() -> dict[str, Any]:
    return {"Id": "sha256:owned-image", "Config": {"Labels": OWNER}}


def test_blitz_removes_owned_project_data_and_exact_built_image_retryably(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    volume = "cairn-install-alpha_cairn-data"
    ctx.containers["container-id"] = _owned_project_labels()
    ctx.networks["network-id"] = (
        "cairn-install-alpha_default",
        OWNER | {"com.docker.compose.project": "cairn-install-alpha"},
    )
    ctx.volumes[volume] = OWNER | {"com.docker.compose.project": "cairn-install-alpha"}
    image = _image()
    ctx.images[backend.image] = image
    ctx.images["sha256:owned-image"] = image
    docker = ctx.state["resources"]["docker"]
    docker["volumes"] = {"cairn-data": volume}
    docker["image"] = {"reference": backend.image, "id": "sha256:owned-image"}

    backend.blitz()
    backend.blitz()

    removals = [command for command in ctx.commands if "rm" in command]
    assert removals == [
        ["docker", "container", "rm", "--force", "container-id"],
        ["docker", "network", "rm", "network-id"],
        ["docker", "volume", "rm", volume],
        ["docker", "image", "rm", "sha256:owned-image"],
    ]
    assert docker["blitz"] == "complete"


def test_blitz_rejects_foreign_replacement_before_deleting_any_project_resource(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    volume = "cairn-install-alpha_cairn-data"
    ctx.containers["container-id"] = _owned_project_labels()
    ctx.volumes[volume] = {
        **OWNER,
        "io.cairn.install.instance": "foreign",
        "com.docker.compose.project": "cairn-install-alpha",
    }
    ctx.state["resources"]["docker"]["volumes"] = {"cairn-data": volume}

    with pytest.raises(InstallError, match="foreign Docker volume"):
        backend.blitz()

    assert not any("rm" in command for command in ctx.commands)


def test_blitz_does_not_force_an_instance_image_used_by_another_container(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    image = _image()
    ctx.images[backend.image] = image
    ctx.images["sha256:owned-image"] = image
    ctx.image_users = ["foreign-container"]
    ctx.state["resources"]["docker"]["image"] = {
        "reference": backend.image,
        "id": "sha256:owned-image",
    }

    with pytest.raises(InstallError, match="used by another container"):
        backend.blitz()

    assert ["docker", "image", "rm", "sha256:owned-image"] not in ctx.commands


def test_blitz_reconciles_an_owned_image_built_before_its_id_was_published(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    image = _image()
    ctx.images[backend.image] = image
    ctx.images["sha256:owned-image"] = image
    ctx.state["resources"]["docker"]["image"] = {
        "reference": backend.image,
        "intent": "build",
    }

    backend.blitz()

    assert ["docker", "image", "rm", "sha256:owned-image"] in ctx.commands
    assert ctx.state["resources"]["docker"]["image"] == {
        "reference": backend.image,
        "id": "sha256:owned-image",
        "status": "removed",
    }


def test_blitz_before_any_docker_creation_intent_needs_no_docker_daemon(
    tmp_path: Path,
) -> None:
    ctx = DockerContext(tmp_path)
    backend = Backend(cast(Context, ctx))

    backend.blitz()

    assert ctx.commands == []
    assert ctx.state["resources"]["docker"]["blitz"] == "complete"
