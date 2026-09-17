"""Offline release import must authenticate bytes before touching Docker."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/falkordb_release.py"


@pytest.fixture
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("falkordb_release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def descriptor(tmp_path: Path, **changes: Any) -> Path:
    data = {
        "schema_version": 1,
        "image": "ghcr.io/veridian69/cairn-falkordb:v4.20.4-cairn.1@sha256:" + "a" * 64,
        "local_image": "cairn-local/falkordb-server:v4.20.4-runtime-fix",
        "archive_sha256": hashlib.sha256(b"checked archive").hexdigest(),
    }
    data.update(changes)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"image": "--help"},
        {"image": "ghcr.io/veridian69/cairn-falkordb:latest"},
        {"image": "ghcr.io/other/cairn-falkordb:v4.20.4-cairn.1@sha256:" + "a" * 64},
        {"local_image": "$(touch /tmp/not-allowed)"},
        {"archive_sha256": "A" * 64},
        {"command": "anything"},
    ],
)
def test_invalid_descriptors_fail_without_docker(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    path = descriptor(tmp_path, **changes)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Docker called"))
    with pytest.raises(tool.ReleaseError):
        tool.read_descriptor(path)


def test_duplicate_descriptor_fields_refused(tool: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}')
    with pytest.raises(tool.ReleaseError, match="duplicate"):
        tool.read_descriptor(path)


def test_fifo_archive_is_refused_without_blocking(tmp_path: Path) -> None:
    path = descriptor(tmp_path)
    archive = tmp_path / "pipe.tar"
    os.mkfifo(archive)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "load",
            "--descriptor",
            str(path),
            "--archive",
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert result.returncode == 1
    assert "regular file" in result.stderr


@pytest.mark.parametrize(
    "content,pinned", [(b"wrong", True), (b"checked archive", False)]
)
def test_untrusted_archive_never_reaches_docker(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    pinned: bool,
) -> None:
    path = descriptor(tmp_path, **({} if pinned else {"archive_sha256": None}))
    archive = tmp_path / "image.tar"
    archive.write_bytes(content)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Docker called"))
    with pytest.raises(tool.ReleaseError):
        tool.load(tool.read_descriptor(path), archive)


def docker_info() -> dict[str, Any]:
    return {
        "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
        "OSType": "linux",
        "Architecture": "x86_64",
    }


def image_info() -> dict[str, Any]:
    return {
        "Id": "sha256:" + "a" * 64,
        "RepoDigests": ["ghcr.io/veridian69/cairn-falkordb@sha256:" + "a" * 64],
        "Os": "linux",
        "Architecture": "amd64",
    }


@pytest.mark.parametrize("wrong", [False, True])
def test_load_uses_verified_snapshot_and_checks_exact_identity(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong: bool
) -> None:
    path = descriptor(tmp_path)
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"checked archive")
    calls = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[1] == "info":
            # A path replacement after hashing must not change the loaded bytes.
            archive.write_bytes(b"changed after verification")
            result = docker_info()
        elif args[1:3] == ["image", "load"]:
            assert kwargs["stdin"].read() == b"checked archive"
            return subprocess.CompletedProcess(args, 0, b"loaded", b"")
        else:
            assert args[1:3] == ["image", "inspect"]
            assert args[3].endswith("@sha256:" + "a" * 64)
            result = image_info()
            if wrong:
                result["RepoDigests"] = []
        return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    if wrong:
        with pytest.raises(tool.ReleaseError, match="identity"):
            tool.load(tool.read_descriptor(path), archive)
    else:
        tool.load(tool.read_descriptor(path), archive)
    assert [c[1] for c in calls] == ["info", "image", "image"]


@pytest.mark.parametrize(
    "changes",
    [{"DriverStatus": []}, {"Architecture": "aarch64"}, {"OSType": "windows"}],
)
def test_unsupported_daemon_refused_before_load(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"checked archive")
    info = docker_info() | changes

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert args[1] == "info", "unsupported daemon was mutated"
        return subprocess.CompletedProcess(args, 0, json.dumps(info).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(tool.ReleaseError):
        tool.load(tool.read_descriptor(descriptor(tmp_path)), archive)


def test_prepare_refuses_existing_archive_without_docker(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"keep me")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Docker called"))
    with pytest.raises(tool.ReleaseError):
        tool.prepare(tool.read_descriptor(descriptor(tmp_path)), archive)
    assert archive.read_bytes() == b"keep me"


def test_prepare_rejects_wrong_source_before_tagging(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if args[1] == "info":
            result = docker_info()
        else:
            assert args[1:3] == ["image", "inspect"]
            result = image_info() | {"Id": "sha256:" + "b" * 64}
        return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(tool.ReleaseError, match="identity"):
        tool.prepare(tool.read_descriptor(descriptor(tmp_path)), tmp_path / "image.tar")


def test_graph_check_rejects_missing_index(tool: ModuleType) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    archive.seek(0)
    with pytest.raises(tool.ReleaseError, match="index"):
        tool.verify_archive_graph(archive, "sha256:" + "a" * 64)


def oci_archive(
    *, missing_layer: bool = False, corrupt_layer: bool = False
) -> tuple[io.BytesIO, str]:
    blobs = {}

    def blob(data: bytes) -> dict[str, str]:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs[digest] = data
        return {"digest": digest}

    layer = blob(b"layer data")
    config = blob(json.dumps({"config": {"Entrypoint": ["redis-server"]}}).encode())
    manifest = blob(
        json.dumps({"schemaVersion": 2, "config": config, "layers": [layer]}).encode()
    )
    index = blob(json.dumps({"schemaVersion": 2, "manifests": [manifest]}).encode())
    if missing_layer:
        del blobs[layer["digest"]]
    if corrupt_layer:
        blobs[layer["digest"]] = b"corrupt"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for digest, payload in blobs.items():
            info = tarfile.TarInfo("blobs/sha256/" + digest.split(":")[1])
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    output.seek(0)
    return output, index["digest"]


@pytest.mark.parametrize("damage", [None, "missing_layer", "corrupt_layer"])
def test_graph_check_traverses_all_index_blobs(
    tool: ModuleType, damage: str | None
) -> None:
    archive, digest = oci_archive(**({damage: True} if damage else {}))
    if damage:
        with pytest.raises(tool.ReleaseError):
            tool.verify_archive_graph(archive, digest)
    else:
        tool.verify_archive_graph(archive, digest)
    assert archive.tell() == 0


def test_prepare_writes_archive_with_complete_index_and_reports_checksum(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, digest = oci_archive()
    expected = payload.getvalue()
    path = descriptor(
        tmp_path,
        image="ghcr.io/veridian69/cairn-falkordb:v4.20.4-cairn.1@" + digest,
        archive_sha256=None,
    )
    original_descriptor = path.read_bytes()
    info = image_info() | {
        "Id": digest,
        "RepoDigests": ["ghcr.io/veridian69/cairn-falkordb@" + digest],
    }

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if args[1] == "info":
            result = docker_info()
        elif args[1:3] == ["image", "inspect"]:
            result = info
        elif args[1:3] == ["image", "tag"]:
            assert "@" not in args[-1]
            return subprocess.CompletedProcess(args, 0, b"", b"")
        else:
            assert args[1:3] == ["image", "save"]
            kwargs["stdout"].write(expected)
            return subprocess.CompletedProcess(args, 0, None, b"")
        return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    archive = tmp_path / "new.tar"
    result = tool.prepare(tool.read_descriptor(path), archive)
    assert archive.read_bytes() == expected
    assert result["archive_sha256"] == hashlib.sha256(expected).hexdigest()
    assert path.read_bytes() == original_descriptor
