"""The node staging helper authenticates input and publishes only complete receipts."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/kubernetes_image_stage.py"
DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.example/cairn/falkordb@" + DIGEST
SOURCE = "cairn-local/falkordb:stage"


@pytest.fixture
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kubernetes_image_stage", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _json_blob(document: dict[str, Any]) -> tuple[str, bytes]:
    payload = json.dumps(document, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest(), payload


def oci_archive(tmp_path: Path) -> tuple[Path, str]:
    config_digest, config = _json_blob({"architecture": "amd64", "os": "linux"})
    layer = b"layer"
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "config": {"digest": config_digest, "size": len(config)},
        "layers": [{"digest": layer_digest, "size": len(layer)}],
    }
    manifest_payload = json.dumps(manifest, separators=(",", ":")).encode()
    assert "sha256:" + hashlib.sha256(manifest_payload).hexdigest() != DIGEST
    # The tests use a literal image digest, so make its corresponding blob content
    # available under that name and let the graph verifier catch any mutation.
    image = IMAGE.replace(
        DIGEST, "sha256:" + hashlib.sha256(manifest_payload).hexdigest()
    )
    digest = image.split("@", 1)[1]
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": digest,
                "size": len(manifest_payload),
                "annotations": {
                    "io.containerd.image.name": SOURCE,
                    "org.opencontainers.image.ref.name": "falkordb:stage",
                },
            }
        ],
    }
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        for name, payload in (
            ("index.json", json.dumps(index).encode()),
            ("blobs/sha256/" + digest.removeprefix("sha256:"), manifest_payload),
            ("blobs/sha256/" + config_digest.removeprefix("sha256:"), config),
            ("blobs/sha256/" + layer_digest.removeprefix("sha256:"), layer),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return path, image


def node(name: str, uid: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "metadata": {"name": name, "uid": uid},
        "spec": {},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "nodeInfo": {
                "architecture": "amd64",
                "operatingSystem": "linux",
                "containerRuntimeVersion": "containerd://2.1.4",
            },
        },
    }
    for dotted, replacement in changes.items():
        target = value
        parts = dotted.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = replacement
    return value


class FakeCommands:
    def __init__(self, nodes: list[dict[str, Any]], image: str) -> None:
        self.nodes = nodes
        self.image = image
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail_identity_alias: str | None = None
        self.fail_preflight_alias: str | None = None
        self.transferred: dict[str, bytes] = {}
        self.replace_node_after_stage = False
        self.kubectl_calls = 0
        self.existing_identity = False

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        stdin = kwargs.get("stdin")
        body = stdin.read() if stdin is not None else None
        if stdin is not None:
            stdin.seek(0)
        self.calls.append((args, body))
        if args[0] == "kubectl":
            assert args[1:] == [
                "--context",
                "test-context",
                "get",
                "nodes",
                "-o",
                "json",
            ]
            self.kubectl_calls += 1
            nodes = self.nodes
            if self.replace_node_after_stage and self.kubectl_calls > 1:
                nodes = [node("worker-a", "replacement-uid")]
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"items": nodes}).encode(), b""
            )
        assert args[0] == "ssh"
        assert args[1:8] == [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
        ]
        alias = args[8]
        command = args[9]
        if "mktemp" in command:
            return subprocess.CompletedProcess(
                args, 0, b"/var/tmp/cairn-image-stage.Ab12Cd34\n", b""
            )
        if alias == self.fail_preflight_alias and "/usr/bin/test -x" in command:
            return subprocess.CompletedProcess(args, 1, b"", b"missing tool")
        if "tee" in command:
            assert body is not None
            self.transferred[alias] = body
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if "sha256sum" in command:
            checksum = hashlib.sha256(self.transferred[alias]).hexdigest()
            return subprocess.CompletedProcess(
                args, 0, (checksum + "  archive\n").encode(), b""
            )
        if "crictl" in command:
            optional = "||" in command
            repo_digests = (
                [self.image]
                if (not optional and alias != self.fail_identity_alias)
                or (optional and self.existing_identity)
                else []
            )
            payload = {"status": {"repoDigests": repo_digests}}
            return subprocess.CompletedProcess(
                args, 0, json.dumps(payload).encode(), b""
            )
        return subprocess.CompletedProcess(args, 0, b"", b"")


def test_complete_stage_publishes_closed_receipt(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, image = oci_archive(tmp_path)
    output = tmp_path / "receipt.json"
    fake = FakeCommands([node("worker-a", "uid-a"), node("worker-b", "uid-b")], image)
    monkeypatch.setattr(tool.subprocess, "run", fake)

    result = tool.stage(
        context="test-context",
        archive=archive,
        image=image,
        mappings=["worker-a=ssh-a", "worker-b=ssh-b"],
        output=output,
    )

    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = {
        "schema_version": 1,
        "image": image,
        "archive_sha256": checksum,
        "nodes": [
            {"name": "worker-a", "uid": "uid-a"},
            {"name": "worker-b", "uid": "uid-b"},
        ],
    }
    assert result == expected
    assert json.loads(output.read_text()) == expected
    ssh_commands = [call[0][9] for call in fake.calls if call[0][0] == "ssh"]
    assert sum("images import" in command for command in ssh_commands) == 2
    assert sum("--all-platforms" in command for command in ssh_commands) == 2
    assert sum("images tag" in command for command in ssh_commands) == 2
    assert all(
        "/run/containerd/containerd.sock" in command
        for command in ssh_commands
        if "ctr" in command or "crictl" in command
    )
    assert sum("rm -rf --" in command for command in ssh_commands) == 2


@pytest.mark.parametrize(
    "case",
    ["bad-image", "unsafe-alias", "existing-output", "corrupt-archive"],
)
def test_bad_local_input_fails_before_external_commands(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    archive, image = oci_archive(tmp_path)
    output = tmp_path / "receipt.json"
    mappings = ["worker-a=ssh-a"]
    if case == "bad-image":
        image = "registry.example/falkordb:latest"
    elif case == "unsafe-alias":
        mappings = ["worker-a=-oProxyCommand=bad"]
    elif case == "existing-output":
        output.write_text("keep")
    else:
        archive.write_bytes(b"not an OCI archive")
    monkeypatch.setattr(
        tool.subprocess, "run", lambda *a, **k: pytest.fail("external command called")
    )

    with pytest.raises(tool.StageError):
        tool.stage("test-context", archive, image, mappings, output)
    assert not output.exists() or output.read_text() == "keep"


@pytest.mark.parametrize(
    "changes",
    [
        {"spec__unschedulable": True},
        {"spec__taints": [{"key": "reserved", "effect": "NoSchedule"}]},
        {"status__conditions": [{"type": "Ready", "status": "False"}]},
        {
            "status__nodeInfo": {
                "architecture": "arm64",
                "operatingSystem": "linux",
                "containerRuntimeVersion": "containerd://2.1.4",
            }
        },
        {
            "status__nodeInfo": {
                "architecture": "amd64",
                "operatingSystem": "linux",
                "containerRuntimeVersion": "cri-o://1.33",
            }
        },
    ],
)
def test_ineligible_node_fails_before_ssh(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    archive, image = oci_archive(tmp_path)
    fake = FakeCommands([node("worker-a", "uid-a", **changes)], image)
    monkeypatch.setattr(tool.subprocess, "run", fake)

    with pytest.raises(tool.StageError, match="not eligible"):
        tool.stage(
            "test-context",
            archive,
            image,
            ["worker-a=ssh-a"],
            tmp_path / "receipt.json",
        )
    assert [call[0][0] for call in fake.calls] == ["kubectl"]


def test_identity_failure_cleans_owned_files_without_partial_receipt(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, image = oci_archive(tmp_path)
    output = tmp_path / "receipt.json"
    fake = FakeCommands([node("worker-a", "uid-a"), node("worker-b", "uid-b")], image)
    fake.fail_identity_alias = "ssh-b"
    monkeypatch.setattr(tool.subprocess, "run", fake)

    with pytest.raises(tool.StageError, match="repoDigest"):
        tool.stage(
            "test-context",
            archive,
            image,
            ["worker-a=ssh-a", "worker-b=ssh-b"],
            output,
        )
    assert not output.exists()
    cleanup = [
        call[0]
        for call in fake.calls
        if call[0][0] == "ssh" and "rm -rf --" in call[0][9]
    ]
    assert [call[8] for call in cleanup] == ["ssh-a", "ssh-b"]


def test_all_remote_prerequisites_are_checked_before_transfer(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, image = oci_archive(tmp_path)
    fake = FakeCommands([node("worker-a", "uid-a"), node("worker-b", "uid-b")], image)
    fake.fail_preflight_alias = "ssh-b"
    monkeypatch.setattr(tool.subprocess, "run", fake)

    with pytest.raises(tool.StageError, match="ssh command failed"):
        tool.stage(
            "test-context",
            archive,
            image,
            ["worker-a=ssh-a", "worker-b=ssh-b"],
            tmp_path / "receipt.json",
        )
    assert not any(call[0][0] == "ssh" and "tee" in call[0][9] for call in fake.calls)


def test_existing_exact_digest_alias_is_not_overwritten(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, image = oci_archive(tmp_path)
    fake = FakeCommands([node("worker-a", "uid-a")], image)
    fake.existing_identity = True
    monkeypatch.setattr(tool.subprocess, "run", fake)

    tool.stage(
        "test-context",
        archive,
        image,
        ["worker-a=ssh-a"],
        tmp_path / "receipt.json",
    )
    assert not any(
        call[0][0] == "ssh" and "images tag" in call[0][9] for call in fake.calls
    )


def test_replaced_node_is_refused_after_staging(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, image = oci_archive(tmp_path)
    output = tmp_path / "receipt.json"
    fake = FakeCommands([node("worker-a", "uid-a")], image)
    fake.replace_node_after_stage = True
    monkeypatch.setattr(tool.subprocess, "run", fake)

    with pytest.raises(tool.StageError, match="changed during staging"):
        tool.stage("test-context", archive, image, ["worker-a=ssh-a"], output)
    assert not output.exists()
