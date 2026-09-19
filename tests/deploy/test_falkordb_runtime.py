"""Local runtime export must preserve identity without claiming publication."""

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def tool() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/falkordb_runtime.py"
    spec = importlib.util.spec_from_file_location("falkordb_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_output_is_preserved(tool: ModuleType, tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "retain"
    sentinel.write_text("operator data")
    with pytest.raises(tool.RuntimeError, match="exists"):
        tool.export_runtime("cairn-local/falkordb-server:rebuild", output)
    assert sentinel.read_text() == "operator data"


def test_invalid_reference_rejected_before_docker(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    with pytest.raises(tool.RuntimeError, match="reference"):
        tool.export_runtime("--all", tmp_path / "output")


def test_failed_graph_verification_emits_no_runtime_descriptor(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_docker(args: list[str], **kwargs: object) -> bytes:
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64"}
            ).encode()
        if args[:2] == ["image", "save"]:
            Path(args[args.index("--output") + 1]).write_bytes(b"invalid image")
        return b""

    monkeypatch.setattr(tool, "docker", fake_docker)
    output = tmp_path / "output"
    with pytest.raises(tool.RuntimeError, match="archive"):
        tool.export_runtime("cairn-local/falkordb-server:rebuild", output)
    assert not output.exists()
    assert not any("push" in call for call in calls)


def test_export_records_actual_index_and_archive_checksum(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import tarfile

    index = b'{"schemaVersion":2,"manifests":[]}'
    digest = hashlib.sha256(index).hexdigest()
    image = "cairn.local/falkordb-runtime@sha256:" + digest
    calls = []

    def fake_docker(args: list[str], **kwargs: object) -> bytes:
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                {"Id": "sha256:" + digest, "Os": "linux", "Architecture": "amd64"}
            ).encode()
        if args[:2] == ["image", "save"]:
            with tarfile.open(args[args.index("--output") + 1], "w") as archive:
                root = json.dumps(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                "digest": "sha256:" + digest,
                                "annotations": {
                                    "io.containerd.image.name": "cairn.local/falkordb-runtime:build-"
                                    + digest
                                },
                            }
                        ],
                    }
                ).encode()
                root_entry = tarfile.TarInfo("index.json")
                root_entry.size = len(root)
                archive.addfile(root_entry, io.BytesIO(root))
                entry = tarfile.TarInfo("blobs/sha256/" + digest)
                entry.size = len(index)
                archive.addfile(entry, io.BytesIO(index))
        return b""

    monkeypatch.setattr(tool, "docker", fake_docker)
    output = tmp_path / "output"
    tool.export_runtime("cairn-local/falkordb-server:rebuild", output)
    record = json.loads((output / "runtime.json").read_text())
    assert record["image"] == image
    assert (
        record["archive_sha256"]
        == hashlib.sha256((output / "image.tar").read_bytes()).hexdigest()
    )
    assert record["platform"] == "linux/amd64"
    assert record["published"] is False
    assert not any("push" in call for call in calls)


def test_export_refuses_graph_without_importable_root(
    tool: ModuleType, tmp_path: Path
) -> None:
    import hashlib
    import tarfile

    payload = b'{"schemaVersion":2,"manifests":[]}'
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        entry = tarfile.TarInfo("blobs/sha256/" + digest)
        entry.size = len(payload)
        archive.addfile(entry, io.BytesIO(payload))
    with pytest.raises(tool.RuntimeError, match="archive"):
        tool.verify_archive(path, "sha256:" + digest)


@pytest.mark.parametrize("alias", [123, "-unsafe", "other.example/runtime:wrong"])
def test_export_refuses_unusable_import_alias(
    tool: ModuleType, tmp_path: Path, alias: object
) -> None:
    import hashlib
    import tarfile

    payload = b'{"schemaVersion":2,"manifests":[]}'
    digest = hashlib.sha256(payload).hexdigest()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:" + digest,
                    "annotations": {"io.containerd.image.name": alias},
                }
            ],
        }
    ).encode()
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in (("index.json", index), ("blobs/sha256/" + digest, payload)):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    with pytest.raises(tool.RuntimeError, match="archive"):
        tool.verify_archive(path, "sha256:" + digest, "cairn.local/runtime:expected")
