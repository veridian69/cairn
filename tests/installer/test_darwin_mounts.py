"""Darwin deletion refuses unknown or nested mount boundaries."""

import ctypes
import os
import platform
import stat
import struct
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import destruction
from cairn_install.core import InstallError, open_context


def entry_record(
    name: bytes,
    mode: int = stat.S_IFREG | 0o600,
    mount_status: int = 0,
    error: int | None = None,
) -> bytes:
    common = 0x80020001
    directory = 0x4 if stat.S_ISDIR(mode) else 0
    if error is not None:
        common |= 0x20000000
    header = struct.pack("=6I", 0, common, 0, directory, 0, 0)
    failure = b"" if error is None else struct.pack("=I", error)
    name_reference = struct.pack(
        "=iII", 12 + (4 if directory else 0), len(name) + 1, mode
    )
    attributes = struct.pack("=I", mount_status) if directory else b""
    record = header + failure + name_reference + attributes + name + b"\0"
    record += b"\0" * (-len(record) % 8)
    return struct.pack("=I", len(record)) + record[4:]


def emulate_attributes(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    mounted: dict[Path, int] | None = None,
    omit: str | None = None,
) -> list[Path]:
    """Emulate covered-vnode records; do not traverse any symlink targets."""
    mounted = mounted or {}
    by_identity = {}
    for current, _, _ in os.walk(root, followlinks=False):
        path = Path(current)
        info = path.stat()
        by_identity[info.st_dev, info.st_ino] = path
    calls: list[Path] = []
    emitted: set[Path] = set()

    def path_for(fd: int) -> Path:
        info = os.fstat(fd)
        return by_identity[info.st_dev, info.st_ino]

    def single(fd: int, attributes: Any, buffer: Any, size: int, options: int) -> int:
        request = ctypes.cast(
            attributes, ctypes.POINTER(destruction._DarwinAttrList)
        ).contents
        assert request.bitmapcount == 5
        assert request.commonattr == 0x80000000
        assert request.dirattr == 0x4
        assert options == 0
        assert size == 28
        path = path_for(fd)
        calls.append(path)
        record = struct.pack("=7I", 28, 0x80000000, 0, 0x4, 0, 0, mounted.get(path, 0))
        ctypes.memmove(buffer, record, len(record))
        return 0

    def bulk(fd: int, attributes: Any, buffer: Any, size: int, options: int) -> int:
        request = ctypes.cast(
            attributes, ctypes.POINTER(destruction._DarwinAttrList)
        ).contents
        assert request.bitmapcount == 5
        assert request.commonattr == 0xA0020001
        assert not request.commonattr & 0x8  # OBJTYPE would bypass the safe fallback.
        assert request.dirattr == 0x4
        assert options == 0
        path = path_for(fd)
        if path in emitted:
            return 0
        emitted.add(path)
        records = []
        for child in path.iterdir():
            if child.name != omit:
                records.append(
                    entry_record(
                        os.fsencode(child.name),
                        child.lstat().st_mode,
                        mounted.get(child, 0),
                    )
                )
        data = b"".join(records)
        assert len(data) <= size
        ctypes.memmove(buffer, data, len(data))
        return len(records)

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            fgetattrlist=single, getattrlistbulk=bulk
        ),
    )
    return calls


def test_darwin_check_never_inspects_machine_mount_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        (ctx.root / "data").mkdir()
        (ctx.root / "data" / "facts").write_text("local")
        outside = tmp_path / "unavailable-volume"
        (ctx.root / "external").symlink_to(outside, target_is_directory=True)
        calls = emulate_attributes(monkeypatch, ctx.directory)
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        def reject_inventory() -> list[Path]:
            raise AssertionError("must not inventory or traverse unrelated mounts")

        monkeypatch.setattr(destruction, "_mount_points", reject_inventory)
        destruction.check_instance_tree(ctx)
        assert calls == [ctx.directory, ctx.root, ctx.root / "data"]
        assert not outside.exists()


@pytest.mark.parametrize("status", [1, 2, 3, 4])
def test_darwin_mount_or_trigger_refused_before_opening_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    mounted = tmp_path / "nested mount"
    mounted.mkdir()
    calls = emulate_attributes(monkeypatch, tmp_path, {mounted: status})
    original = os.open

    def guarded_open(path: Any, flags: int, **kwargs: Any) -> int:
        assert path != mounted.name, "must not enter mounted child"
        return original(path, flags, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(InstallError, match="mounted directory"):
        destruction._check_darwin_mounts(tmp_path)
    assert calls == [tmp_path]


def test_darwin_mount_at_instance_root_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = emulate_attributes(monkeypatch, tmp_path, {tmp_path: 1})
    with pytest.raises(InstallError, match="mounted directory"):
        destruction._check_darwin_mounts(tmp_path)
    assert calls == [tmp_path]


def test_darwin_skipped_lookup_refuses_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "unreadable").mkdir()
    emulate_attributes(monkeypatch, tmp_path, omit="unreadable")
    with pytest.raises(InstallError, match="Cannot inspect Darwin mount"):
        destruction._check_darwin_mounts(tmp_path)


def test_darwin_scanner_closes_descriptors_after_nested_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    mounted = child / "mount"
    mounted.mkdir(parents=True)
    emulate_attributes(monkeypatch, tmp_path, {mounted: 1})
    original_open, original_close = os.open, os.close
    opened, closed = [], []

    def record_open(path: Any, flags: int, **kwargs: Any) -> int:
        fd = original_open(path, flags, **kwargs)
        opened.append(fd)
        return fd

    def record_close(fd: int) -> None:
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)
    with pytest.raises(InstallError, match="mounted directory"):
        destruction._check_darwin_mounts(tmp_path)
    assert len(opened) >= 2
    assert Counter(closed) == Counter(opened)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (stat.S_IFREG | 0o600, False),
        (stat.S_IFDIR | 0o700, True),
        (stat.S_IFLNK | 0o777, False),
    ],
)
def test_darwin_parser_recognises_type_without_objtype(
    mode: int, expected: bool
) -> None:
    assert destruction._darwin_entry(entry_record(b"space and \xff", mode)) == (
        os.fsdecode(b"space and \xff"),
        expected,
    )


@pytest.mark.parametrize("name", [b"", b".", b"..", b"../escape", b"embedded\0nul"])
def test_darwin_parser_refuses_unsafe_names(name: bytes) -> None:
    with pytest.raises(ValueError, match="name"):
        destruction._darwin_entry(entry_record(name))


def test_darwin_parser_refuses_entry_error() -> None:
    with pytest.raises(OSError):
        destruction._darwin_entry(entry_record(b"unreadable", error=13))


@pytest.mark.parametrize(
    "change",
    [
        "length",
        "attribute",
        "mode",
        "mount",
        "name_offset",
        "name_length",
        "unterminated",
    ],
)
def test_darwin_parser_refuses_malformed_records(change: str) -> None:
    data = bytearray(entry_record(b"child", stat.S_IFDIR | 0o700))
    if change == "length":
        struct.pack_into("=I", data, 0, len(data) + 4)
    elif change == "attribute":
        struct.pack_into("=I", data, 4, 0x80020009)
    elif change == "mode":
        struct.pack_into("=I", data, 4, 0x80000001)
    elif change == "mount":
        struct.pack_into("=I", data, 12, 0)
    elif change == "name_offset":
        struct.pack_into("=i", data, 24, -24)
    elif change == "name_length":
        struct.pack_into("=I", data, 28, 0xFFFFFFFF)
    else:
        data[45] = ord("x")
    with pytest.raises(ValueError):
        destruction._darwin_entry(bytes(data))


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires Darwin ABI")
def test_actual_darwin_local_tree_ignores_external_symlinks(tmp_path: Path) -> None:
    (tmp_path / "directory with spaces é").mkdir()
    (tmp_path / "directory with spaces é" / "data").write_text("local")
    (tmp_path / "external").symlink_to(
        "/Volumes/unavailable-test-volume", target_is_directory=True
    )
    destruction._check_darwin_mounts(tmp_path)


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires Darwin ABI")
def test_actual_darwin_filesystem_root_is_refused() -> None:
    with pytest.raises(InstallError, match="mounted directory"):
        destruction._check_darwin_mounts(Path("/"))


@pytest.mark.skipif(platform.system() != "Darwin", reason="requires Darwin volumes")
def test_actual_darwin_mounted_child_is_refused_and_preserved(tmp_path: Path) -> None:
    instance = tmp_path / "instance with spaces"
    mounted = instance / "mounted"
    mounted.mkdir(parents=True)
    image = tmp_path / "probe.dmg"
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-size",
            "16m",
            "-fs",
            "HFS+",
            "-volname",
            "CairnMountProbe",
            str(image),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mounted), str(image)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        sentinel = mounted / "must-survive"
        sentinel.write_text("mounted contents stay untouched")
        with pytest.raises(InstallError, match="mounted directory"):
            destruction._check_darwin_mounts(instance)
        assert sentinel.read_text() == "mounted contents stay untouched"
    finally:
        subprocess.run(
            ["hdiutil", "detach", "-force", str(mounted)],
            check=True,
            capture_output=True,
            timeout=30,
        )
