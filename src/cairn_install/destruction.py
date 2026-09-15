"""Remove a private instance tree without losing interrupted-deletion recovery."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
from pathlib import Path

from .core import (
    Context,
    InstallError,
    atomic_write,
    secure_directory,
    state_root_guard,
)


class _DarwinStatFS(ctypes.Structure):
    # Darwin's 64-bit-inode statfs ABI (xnu bsd/sys/mount.h). Use the
    # INODE64 symbol on Intel: its legacy getfsstat symbol has another layout.
    _fields_ = [
        ("block_size", ctypes.c_uint32),
        ("io_size", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5),
        ("fsid", ctypes.c_int32 * 2),
        ("attributes", ctypes.c_uint32 * 4),
        ("filesystem", ctypes.c_char * 16),
        ("mountpoint", ctypes.c_char * 1024),
        ("source", ctypes.c_char * 1024),
        ("reserved", ctypes.c_uint32 * 8),
    ]


def _darwin_mount_points() -> list[Path]:
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        symbol = {
            "arm64": "getfsstat",
            "x86_64": "getfsstat$INODE64",
        }[platform.machine()]
        query = getattr(library, symbol)
        query.argtypes = [ctypes.POINTER(_DarwinStatFS), ctypes.c_int, ctypes.c_int]
        query.restype = ctypes.c_int
        count = query(None, 0, 2)  # MNT_NOWAIT: inspect without network I/O.
        if not 0 < count <= 4096:
            raise ValueError("invalid mount count")
        capacity = count + 16
        buffer = (_DarwinStatFS * capacity)()
        observed = query(buffer, ctypes.sizeof(buffer), 2)
        if not 0 < observed < capacity:
            raise ValueError("mount inventory changed or failed")
        points = []
        for index in range(observed):
            raw = ctypes.string_at(
                ctypes.addressof(buffer[index]) + _DarwinStatFS.mountpoint.offset,
                1024,
            )
            if b"\0" not in raw or not raw.startswith(b"/"):
                raise ValueError("invalid mount path")
            points.append(Path(os.fsdecode(raw.split(b"\0", 1)[0])))
        return points
    except (AttributeError, KeyError, OSError, ValueError) as error:
        raise InstallError(
            "Cannot inspect Darwin mount boundaries for blitz"
        ) from error


def _mount_points() -> list[Path]:
    if platform.system() == "Darwin":
        return _darwin_mount_points()
    # st_dev alone misses bind mounts of directories on the same filesystem.
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError as error:
        raise InstallError("Cannot inspect mount boundaries for blitz") from error
    points: list[Path] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            raise InstallError("Cannot parse mount boundaries for blitz")
        decoded = re.sub(
            r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
        )
        points.append(Path(decoded))
    return points


def _darwin_mount_within(point: Path, directory: Path) -> bool:
    # APFS can be case insensitive; firmlinks and symlinks also give the same
    # directory different spellings. Compare ancestors by filesystem identity.
    try:
        owned = directory.stat()
        expected = (owned.st_dev, owned.st_ino)
        resolved = point.resolve(strict=True)
        for ancestor in (resolved, *resolved.parents):
            observed = ancestor.stat()
            if (observed.st_dev, observed.st_ino) == expected:
                return True
        return False
    except (OSError, RuntimeError) as error:
        raise InstallError(
            "Cannot resolve Darwin mount boundaries for blitz"
        ) from error


def check_instance_tree(ctx: Context) -> None:
    secure_directory(ctx.directory, private=True)
    canonical = ctx.directory.resolve(strict=True)
    mounts = _mount_points()
    if any(point == canonical or canonical in point.parents for point in mounts) or (
        platform.system() == "Darwin"
        and any(_darwin_mount_within(point, canonical) for point in mounts)
    ):
        raise InstallError(
            "Blitz refuses a mounted directory within the instance; unmount it first"
        )
    # This directory was exclusively created for the named instance. Symlinks
    # belong to it, their targets do not; neither inspection nor rmtree follows them.
    for current, directories, files in os.walk(ctx.directory, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current) / name
            info = path.lstat()
            if info.st_uid != os.getuid():
                raise InstallError(f"Blitz refuses a foreign-owned file: {path}")


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_instance_files(ctx: Context) -> None:
    root = ctx.directory.parent
    with state_root_guard(root, exclusive=True):
        check_instance_tree(ctx)
        actual = (ctx.directory / "installer.lock").lstat()
        locked = os.fstat(ctx._lock)
        if (actual.st_dev, actual.st_ino) != (locked.st_dev, locked.st_ino):
            raise InstallError("Instance lock changed; refusing blitz")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise InstallError("This platform cannot safely remove an instance tree")
        journal = root / f".{ctx.name}.blitz.json"
        # Publish and fsync recovery outside the tree before deleting any of it.
        # open_context reserves the name while this journal exists and can
        # recover even after state.json and the old lock have disappeared.
        atomic_write(journal, (json.dumps(ctx.state, sort_keys=True) + "\n").encode())
        _sync(root)
        try:
            shutil.rmtree(ctx.directory)
            _sync(root)
            journal.unlink()
            _sync(root)
        except OSError as error:
            raise InstallError(
                f"Blitz could not finish local deletion; run blitz again. Recovery: {journal}"
            ) from error
        # Never save or log through this Context after deletion. Its old lock
        # descriptor remains open until the caller exits the context manager.
