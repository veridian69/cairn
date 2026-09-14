"""Remove a private instance tree without losing interrupted-deletion recovery."""

from __future__ import annotations

import json
import os
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


def check_instance_tree(ctx: Context) -> None:
    secure_directory(ctx.directory, private=True)
    canonical = ctx.directory.resolve(strict=True)
    if any(
        point == canonical or canonical in point.parents for point in _mount_points()
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
