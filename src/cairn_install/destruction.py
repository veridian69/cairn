"""Remove a private instance tree without losing interrupted-deletion recovery."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import stat
import struct
from collections.abc import Callable
from pathlib import Path

from .core import (
    Context,
    InstallError,
    atomic_write,
    secure_directory,
    state_root_guard,
)


def _mount_points() -> list[Path]:
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


class _DarwinAttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


_ATTR_NAME = 0x00000001
_ATTR_MODE = 0x00020000
_ATTR_ERROR = 0x20000000
_ATTR_RETURNED = 0x80000000
_ATTR_MOUNT_STATUS = 0x00000004
_ATTR_BULK = _ATTR_NAME | _ATTR_MODE | _ATTR_ERROR | _ATTR_RETURNED
_MOUNT_REFUSAL = (
    "Blitz refuses a mounted directory within the instance; unmount it first"
)


def _darwin_attributes(data: bytes, common: int) -> tuple[int, int, int]:
    if len(data) < 24:
        raise ValueError("truncated Darwin attributes")
    length, actual, volume, directory, file, fork = struct.unpack_from("=6I", data)
    if (
        length != len(data)
        or actual & ~common
        or not actual & _ATTR_RETURNED
        or volume
        or file
        or fork
        or directory & ~_ATTR_MOUNT_STATUS
    ):
        raise ValueError("invalid Darwin attributes")
    offset = 24
    if actual & _ATTR_ERROR:
        if len(data) < offset + 4:
            raise ValueError("truncated Darwin error")
        error = struct.unpack_from("=I", data, offset)[0]
        if error:
            raise OSError(error, "Cannot inspect Darwin directory entry")
        offset += 4
    return actual, directory, offset


def _darwin_entry(data: bytes) -> tuple[str, bool]:
    actual, directory, offset = _darwin_attributes(data, _ATTR_BULK)
    if actual & (_ATTR_NAME | _ATTR_MODE) != _ATTR_NAME | _ATTR_MODE:
        raise ValueError("missing Darwin entry attributes")
    if len(data) < offset + 12:
        raise ValueError("truncated Darwin directory entry")
    # In the forced vnode-backed XNU fallback, getattrlist_internal adds
    # S_IFMT bits to ATTR_CMN_ACCESSMASK before packing it (unlike an access
    # mask alone). Requesting OBJTYPE would select a different, unsafe path.
    displacement, length, mode = struct.unpack_from("=iII", data, offset)
    name_offset = offset + displacement
    fixed_end = offset + 12
    if stat.S_IFMT(mode) not in (
        stat.S_IFREG,
        stat.S_IFDIR,
        stat.S_IFLNK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
        stat.S_IFCHR,
        stat.S_IFBLK,
    ):
        raise ValueError("invalid Darwin entry type")
    is_directory = stat.S_ISDIR(mode)
    if bool(directory) != is_directory:
        raise ValueError("missing or unexpected Darwin mount status")
    if is_directory:
        if len(data) < fixed_end + 4:
            raise ValueError("truncated Darwin mount status")
        mounted = struct.unpack_from("=I", data, fixed_end)[0]
        if mounted:
            # Refuse mount points, automount triggers, and unknown future flags.
            raise InstallError(_MOUNT_REFUSAL)
        fixed_end += 4
    if length < 2 or name_offset < fixed_end or name_offset + length > len(data):
        raise ValueError("invalid Darwin entry name bounds")
    raw_name = data[name_offset : name_offset + length]
    if (
        raw_name[-1:] != b"\0"
        or b"\0" in raw_name[:-1]
        or b"/" in raw_name
        or raw_name[:-1] in (b".", b"..")
    ):
        raise ValueError("invalid Darwin entry name")
    return os.fsdecode(raw_name[:-1]), is_directory


def _darwin_directory_entries(fd: int, query: Callable[..., int]) -> dict[str, bool]:
    attributes = _DarwinAttrList(5, 0, _ATTR_BULK, 0, _ATTR_MOUNT_STATUS, 0, 0)
    buffer = ctypes.create_string_buffer(8192)
    entries: dict[str, bool] = {}
    while True:
        ctypes.memset(buffer, 0, len(buffer))
        count = query(fd, ctypes.byref(attributes), buffer, len(buffer), 0)
        if count < 0:
            raise OSError(ctypes.get_errno(), "Cannot inspect Darwin directory")
        if count == 0:
            break
        if count > len(buffer) // 24:
            raise ValueError("invalid Darwin entry count")
        offset = 0
        for _ in range(count):
            if offset + 4 > len(buffer):
                raise ValueError("truncated Darwin entries")
            length = struct.unpack_from("=I", buffer, offset)[0]
            if length < 24 or length % 4 or offset + length > len(buffer):
                raise ValueError("invalid Darwin entry length")
            name, is_directory = _darwin_entry(buffer.raw[offset : offset + length])
            if name in entries:
                raise ValueError("duplicate Darwin entry")
            entries[name] = is_directory
            offset += length
    # XNU's fallback skips failed lookups. Refuse incomplete or changing trees
    # instead of treating a skipped entry as safe. listdir reads only this dir.
    verification = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if set(entries) != set(os.listdir(verification)):
            raise ValueError("Darwin directory changed or could not be inspected")
    finally:
        os.close(verification)
    return entries


def _check_darwin_mounts(directory: Path) -> None:
    # Inspect the owned tree, never paths from the machine-wide mount inventory.
    # Crucially, do NOT request ATTR_CMN_OBJTYPE: this selects XNU's readdirattr
    # fallback, whose lookup uses NOCROSSMOUNT without FOLLOW. Its non-null vnode
    # supplies mount/trigger flags even when the mounted filesystem is stale.
    # Pinned implementation: apple-oss-distributions/xnu commit
    # f6217f891ac0bb64f3d375211650a4c1ff8ca1ea, bsd/vfs/vfs_attrlist.c:
    # getattrlistbulk, readdirattr and attr_pack_dir. ACCESSMASK supplies type.
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        bulk = library.getattrlistbulk
        single = library.fgetattrlist
        bulk.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_DarwinAttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
        ]
        single.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_DarwinAttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
        ]
        bulk.restype = single.restype = ctypes.c_int
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def inspect(fd: int) -> None:
            attributes = _DarwinAttrList(
                5, 0, _ATTR_RETURNED, 0, _ATTR_MOUNT_STATUS, 0, 0
            )
            buffer = ctypes.create_string_buffer(28)
            if single(fd, ctypes.byref(attributes), buffer, len(buffer), 0) != 0:
                raise OSError(ctypes.get_errno(), "Cannot inspect Darwin directory")
            _, returned, offset = _darwin_attributes(buffer.raw, _ATTR_RETURNED)
            if returned != _ATTR_MOUNT_STATUS:
                raise ValueError("missing Darwin directory mount status")
            if struct.unpack_from("=I", buffer, offset)[0]:
                raise InstallError(_MOUNT_REFUSAL)
            entries = _darwin_directory_entries(fd, bulk)
            for name, is_directory in entries.items():
                if is_directory:
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        inspect(child)
                    finally:
                        os.close(child)

        fd = os.open(directory, flags)
        try:
            inspect(fd)
        finally:
            os.close(fd)
    except (AttributeError, OSError, ValueError, RecursionError) as error:
        raise InstallError(
            "Cannot inspect Darwin mount boundaries for blitz"
        ) from error


def check_instance_tree(ctx: Context) -> None:
    secure_directory(ctx.directory, private=True)
    canonical = ctx.directory.resolve(strict=True)
    if platform.system() == "Darwin":
        _check_darwin_mounts(canonical)
    elif any(
        point == canonical or canonical in point.parents for point in _mount_points()
    ):
        raise InstallError(_MOUNT_REFUSAL)
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
