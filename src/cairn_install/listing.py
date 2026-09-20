"""Read-only discovery of recorded installer instances."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from .core import MAX_OUTPUT, NAME, InstallError
from .output import features_label

_MODES = frozenset({"disposable", "native", "docker", "kubernetes"})
_IMAGE_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-fA-F]{64}\Z")
_KUBERNETES_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
_STATUSES = frozenset(
    {
        "planned",
        "installing",
        "verified",
        "failed",
        "interrupted",
        "rolled_back",
        "blitzing",
    }
)
_JOURNAL_SUFFIX = ".blitz.json"


class _Unavailable(Exception):
    pass


def _open_root(path: Path) -> int | None:
    """Open every component without following symlinks; never create the root."""
    absolute = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError as error:
                raise InstallError(
                    f"Not a real directory (symlinks refused): {absolute}"
                ) from error
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise InstallError(
                f"Directory must be owned by you with mode 0700: {absolute}"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _locked_root(path: Path) -> Iterator[int | None]:
    fd = _open_root(path)
    if fd is None:
        yield None
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(
                "Installer state cleanup is busy; retry shortly"
            ) from error
        yield fd
    finally:
        os.close(fd)


def _read_document(parent_fd: int, name: str) -> dict[str, Any]:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _Unavailable from error
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise _Unavailable
            raw = stream.read(MAX_OUTPUT + 1)
            if len(raw) > MAX_OUTPUT:
                raise _Unavailable
    except OSError as error:
        raise _Unavailable from error
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise _Unavailable from error
    if not isinstance(value, dict):
        raise _Unavailable
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise _Unavailable
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise _Unavailable from error
    if str(parsed) != value:
        raise _Unavailable
    return value


def _valid_kubernetes(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    context = value.get("context")
    namespace = value.get("namespace")
    storage_class = value.get("storage_class")
    image = value.get("image")
    policy = value.get("image_policy")
    preloaded = value.get("preloaded_image")
    if not all(
        isinstance(item, str) and item and not any(char.isspace() for char in item)
        for item in (context, storage_class)
    ):
        return False
    if (
        not isinstance(namespace, str)
        or not _KUBERNETES_NAMESPACE.fullmatch(namespace)
        or not isinstance(image, str)
        or not _IMAGE_DIGEST.fullmatch(image)
        or type(preloaded) is not bool
    ):
        return False
    return policy == ("IfNotPresent" if preloaded else "Always")


def _valid_garden(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    options = value.get("options")
    if not isinstance(options, dict):
        return False
    endpoint = options.get("endpoint")
    port = options.get("port")
    return (
        isinstance(endpoint, str)
        and endpoint.startswith("https://")
        and endpoint.endswith("/mcp")
        and not any(ord(char) < 32 or char.isspace() for char in endpoint)
        and type(port) is int
        and 1024 <= port <= 65535
    )


def _row(value: dict[str, Any], name: str) -> dict[str, object]:
    mode = value.get("mode")
    status_value = value.get("status")
    port = value.get("port")
    semantic = value.get("semantic")
    if (
        type(value.get("schema")) is not int
        or value.get("schema") != 1
        or type(value.get("owner_uid")) is not int
        or value.get("owner_uid") != os.getuid()
        or value.get("name") != name
        or type(mode) is not str
        or mode not in _MODES
        or type(status_value) is not str
        or status_value not in _STATUSES
        or type(port) is not int
        or not 1 <= port <= 65535
        or type(semantic) is not bool
        or (mode == "kubernetes" and not _valid_kubernetes(value.get("kubernetes")))
        or ("garden" in value and not _valid_garden(value.get("garden")))
    ):
        raise _Unavailable
    instance_id = _uuid(value.get("instance_id"))
    features = features_label(semantic, "garden" in value)
    return {
        "name": name,
        "mode": mode,
        "status": status_value,
        "port": port,
        "features": features,
        "instance_id": instance_id,
    }


def _journal_row(root_fd: int, name: str) -> dict[str, object]:
    value = _read_document(root_fd, f".{name}{_JOURNAL_SUFFIX}")
    if (
        value.get("status") != "blitzing"
        or value.get("blitz_phase") != "resources_removed"
    ):
        raise _Unavailable
    return _row(value, name)


def _state_row(root_fd: int, name: str) -> dict[str, object]:
    try:
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except OSError as error:
        raise _Unavailable from error
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise _Unavailable
        return _row(_read_document(directory_fd, "state.json"), name)
    finally:
        os.close(directory_fd)


def _candidate_names(root_fd: int) -> tuple[set[str], set[str]]:
    instances: set[str] = set()
    journals: set[str] = set()
    try:
        entries = os.listdir(root_fd)
    except OSError as error:
        raise InstallError("Cannot safely list installer state") from error
    for entry in entries:
        if entry.startswith(".") and entry.endswith(_JOURNAL_SUFFIX):
            name = entry[1 : -len(_JOURNAL_SUFFIX)]
            if NAME.fullmatch(name):
                journals.add(name)
            continue
        if not NAME.fullmatch(entry):
            continue
        instances.add(entry)
    return instances, journals


def list_instances(state_root: Path) -> list[dict[str, object]]:
    """List protected recorded state without probing or changing an instance."""
    with _locked_root(state_root) as root_fd:
        if root_fd is None:
            return []
        instances, journals = _candidate_names(root_fd)
        rows: list[dict[str, object]] = []
        for name in sorted(instances | journals):
            try:
                if name in journals:
                    rows.append(_journal_row(root_fd, name))
                else:
                    rows.append(_state_row(root_fd, name))
            except _Unavailable:
                rows.append(
                    {
                        "name": name,
                        "status": "unavailable",
                        "error": (
                            "invalid blitz recovery journal"
                            if name in journals
                            else "invalid installer state"
                        ),
                    }
                )
        return rows
