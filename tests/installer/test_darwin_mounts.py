"""Darwin deletion refuses unknown or nested mount boundaries."""

import ctypes
import platform
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import destruction
from cairn_install.core import InstallError


def inventory(monkeypatch: pytest.MonkeyPatch, paths: list[bytes]) -> None:
    def getfsstat(buffer: Any, size: int, flags: int) -> int:
        assert flags == 2
        if buffer is None:
            return len(paths)
        for index, path in enumerate(paths):
            buffer[index].mountpoint = path
        return len(paths)

    monkeypatch.setattr(
        destruction, "platform", SimpleNamespace(machine=lambda: "arm64")
    )
    monkeypatch.setattr(
        ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(getfsstat=getfsstat)
    )


def test_darwin_inventory_preserves_spaces_and_multiple_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory(monkeypatch, [b"/", b"/Volumes/Memory Disk", b"/private/tmp/test/data"])
    assert destruction._darwin_mount_points() == [
        Path("/"),
        Path("/Volumes/Memory Disk"),
        Path("/private/tmp/test/data"),
    ]


@pytest.mark.parametrize("paths", [[], [b"relative"], [b"x" * 1024]])
def test_darwin_inventory_refuses_empty_or_invalid_boundaries(
    monkeypatch: pytest.MonkeyPatch, paths: list[bytes]
) -> None:
    inventory(monkeypatch, paths)
    with pytest.raises(InstallError, match="mount"):
        destruction._darwin_mount_points()


def test_darwin_inventory_does_not_use_legacy_intel_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory(monkeypatch, [b"/"])
    monkeypatch.setattr(
        destruction, "platform", SimpleNamespace(machine=lambda: "x86_64")
    )
    with pytest.raises(InstallError, match="mount"):
        destruction._darwin_mount_points()


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires Darwin ABI")
def test_actual_darwin_mount_inventory_contains_root() -> None:
    points = destruction._darwin_mount_points()
    assert Path("/") in points
    assert all(path.is_absolute() for path in points)


@pytest.mark.parametrize("count", [-1, 0, 4097])
def test_darwin_failed_or_unbounded_count_refuses_deletion(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    def query(*args: Any) -> int:
        return count

    monkeypatch.setattr(
        destruction, "platform", SimpleNamespace(machine=lambda: "arm64")
    )
    monkeypatch.setattr(
        ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(getfsstat=query)
    )
    with pytest.raises(InstallError, match="mount"):
        destruction._darwin_mount_points()


def test_mount_alias_is_detected_by_directory_identity(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    child = instance / "data"
    child.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(instance, target_is_directory=True)
    assert destruction._darwin_mount_within(alias / "data", instance)
    direct_child = tmp_path / "direct-child-alias"
    direct_child.symlink_to(child, target_is_directory=True)
    assert destruction._darwin_mount_within(direct_child, instance)
    assert not destruction._darwin_mount_within(tmp_path, instance)


def test_missing_mount_path_refuses_deletion(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="mount"):
        destruction._darwin_mount_within(tmp_path / "missing", tmp_path)


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires Darwin volume")
def test_actual_darwin_case_alias_is_detected(tmp_path: Path) -> None:
    instance = tmp_path / "CairnCaseProbe"
    instance.mkdir()
    alias = tmp_path / "cairncaseprobe"
    if not alias.exists():
        pytest.skip("volume is case sensitive")
    assert destruction._darwin_mount_within(alias, instance)
