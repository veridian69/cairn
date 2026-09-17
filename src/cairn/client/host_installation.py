"""Explicit native-Linux skill installation; no profile discovery or memory I/O.

All path traversal uses directory descriptors and refuses symlinks. The command
uses assets shipped beside this module unless an explicit source is supplied.
Existing installations must match this release exactly.
"""

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LIMIT = 65536
_MANIFEST = ".cairn-install.json"
_PACKAGE = "cairn-memory"
_FOLDERS = {"codex": ".agents", "claude": ".claude"}


class InstallationError(ValueError):
    """Content-free refusal code; never include file contents in diagnostics."""


@dataclass(frozen=True)
class InstallationResult:
    provider: str
    path: Path
    state: Literal["preview", "installed", "unchanged"]


def _validate_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.anchor != "/"
        or len(path.parts) < 3
        or path.is_relative_to("/mnt")
    ):
        raise InstallationError("unsafe_path")


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open every component without following even intermediate symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _child(parent: int, name: str) -> Iterator[int]:
    fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent,
    )
    try:
        yield fd
    finally:
        os.close(fd)


def _read(parent: int, name: str) -> bytes:
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=parent,
    )
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
            raise InstallationError("invalid_asset")
        content = stream.read(_LIMIT + 1)
    if not content or len(content) > _LIMIT:
        raise InstallationError("invalid_asset")
    return content


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _matches(parent: int, files: dict[str, bytes]) -> None:
    with _child(parent, _PACKAGE) as package:
        # Bound directory enumeration too: stop on the first foreign entry.
        seen: set[str] = set()
        with os.scandir(package) as entries:
            for entry in entries:
                if entry.name not in files:
                    raise InstallationError("installation_collision")
                seen.add(entry.name)
        if seen != files.keys():
            raise InstallationError("installation_collision")
        for name, content in files.items():
            if _read(package, name) != content:
                raise InstallationError("installation_collision")


def _publish(parent: int, files: dict[str, bytes]) -> None:
    # Exclusive mkdir is the ownership boundary. Never repair/delete a package
    # whose creation lost a race. Interrupted installs remain refused collisions.
    os.mkdir(_PACKAGE, mode=0o700, dir_fd=parent)
    with _child(parent, _PACKAGE) as package:
        for name, content in files.items():
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=package,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(package)
    os.fsync(parent)


def _install(
    parent: int, components: tuple[str, ...], files: dict[str, bytes], apply: bool
) -> Literal["preview", "installed", "unchanged"]:
    if components:
        name, *remaining = components
        if not _exists(parent, name):
            if not apply:
                return "preview"
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass  # The no-follow directory open still validates the winner.
        with _child(parent, name) as child:
            return _install(child, tuple(remaining), files, apply)
    if _exists(parent, _PACKAGE):
        _matches(parent, files)
        return "unchanged"
    if not apply:
        return "preview"
    _publish(parent, files)
    return "installed"


def install_host_workflow(
    provider: str, destination: Path, *, assets_root: Path, apply: bool = False
) -> InstallationResult:
    """Preview/install one bundled skill beneath an existing explicit host root.

    ``assets_root`` contains ``{codex,claude}/cairn-memory/SKILL.md``. Both
    roots must be absolute native paths with no symlink components. No upgrade,
    overwrite, uninstall, global hooks, credentials or host settings are exposed.
    An exact matching package is a no-op; every other existing package is refused.
    """
    if provider not in _FOLDERS:
        raise InstallationError("unsupported_provider")
    _validate_path(destination)
    _validate_path(assets_root)
    try:
        with _directory(assets_root / provider / _PACKAGE) as source:
            skill = _read(source, "SKILL.md")
        skill.decode("utf-8", errors="strict")
        if b"\0" in skill:
            raise InstallationError("invalid_asset")
        manifest = {
            "schema": "cairn-host-installation-v1",
            "provider": provider,
            "files": {"SKILL.md": hashlib.sha256(skill).hexdigest()},
        }
        files = {
            "SKILL.md": skill,
            _MANIFEST: (json.dumps(manifest, sort_keys=True) + "\n").encode(),
        }
        with _directory(destination) as root:
            state = _install(root, (_FOLDERS[provider], "skills"), files, apply)
    except (OSError, UnicodeError) as exc:
        raise InstallationError("unsafe_or_inaccessible_installation") from exc
    return InstallationResult(
        provider, destination / _FOLDERS[provider] / "skills" / _PACKAGE, state
    )


def main() -> int:
    """Standalone installer until the shared CLI chooses its integration seam."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=tuple(_FOLDERS))
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=Path(__file__).parent.parent / "host_workflows",
        help="explicit asset directory; default is this installed package's bundled assets",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write; default is preview"
    )
    args = parser.parse_args()
    try:
        result = install_host_workflow(
            args.provider,
            args.destination,
            assets_root=args.assets_root,
            apply=args.apply,
        )
    except InstallationError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "provider": result.provider,
                "path": str(result.path),
                "state": result.state,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
