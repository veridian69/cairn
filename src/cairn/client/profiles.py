"""Explicit Linux/WSL connection configuration for the everyday memory command.

Loading never discovers credentials, opens a network connection, generates a
session identity or updates local state. Tokens are read separately immediately
before constructing the HTTP client and never retained in the profile object.
"""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import RFC_4122, UUID

import httpx

from cairn.authority.credentials import parse_token
from cairn.catalogue.audit import AuditValueError, Classification, Scope, ScopeSegment
from cairn.client.diagnostics import _unique_object
from cairn.client.memory import _validate_base_url

_SCHEMA = "cairn.memory-profile/v1"
_FIELDS = frozenset(
    {
        "schema",
        "endpoint",
        "expected_instance_id",
        "scope",
        "classification",
        "credential_file",
    }
)
_PROFILE_BYTES = 32768
_CREDENTIAL_BYTES = 512


class ProfileError(ValueError):
    """Input-free local failures: never include paths, endpoint or token data."""


@dataclass(frozen=True, slots=True, repr=False)
class MemoryProfile:
    endpoint: str
    expected_instance_id: UUID
    scope: Scope
    classification: Classification
    credential_file: Path
    session_id: UUID | None = None


def _read_regular(path: Path, *, limit: int, kind: str) -> bytes:
    """Open every component without following symlinks, then read a bounded file."""
    directory: int | None = None
    descriptor: int | None = None
    try:
        if os.name != "posix" or not isinstance(path, Path):
            raise ProfileError(f"{kind}_unavailable")
        absolute = path if path.is_absolute() else Path.cwd() / path
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in absolute.parts[1:-1]:
            next_directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            absolute.name or ".",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProfileError(f"{kind}_unavailable")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, limit + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ProfileError(f"{kind}_too_large")
        return bytes(data)
    except (OSError, ValueError) as error:
        if isinstance(error, ProfileError):
            raise
        raise ProfileError(f"{kind}_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != fields:
        raise ProfileError("invalid_profile")
    return cast(dict[str, object], value)


def _identity(value: object, *, instance: bool = False) -> UUID:
    if type(value) is not str:
        raise ProfileError("invalid_profile")
    identity = UUID(value)
    if (
        str(identity) != value
        or identity.variant != RFC_4122
        or (instance and identity.version != 4)
    ):
        raise ProfileError("invalid_profile")
    return identity


def _endpoint(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ProfileError("invalid_profile")
    if any(character in value for character in ("@", "?", "#")):
        raise ProfileError("invalid_profile")
    url = httpx.URL(value)
    _validate_base_url(url)
    # MemoryClient joins root-relative memory/v1 paths. Refuse a base path it
    # would silently discard rather than contacting a different route.
    if url.path not in ("", "/") or url.userinfo:
        raise ProfileError("invalid_profile")
    return value


def _credential_path(value: object, profile_path: Path) -> Path:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        raise ProfileError("invalid_profile")
    if any(ord(character) < 32 for character in value) or any(
        character in value for character in ("$", "~", "\\")
    ):
        raise ProfileError("invalid_profile")
    path = Path(value)
    if not path.is_absolute():
        path = profile_path.parent / path
    # Preserve parent components: collapsing link/.. before the descriptor
    # walk would bypass the no-follow policy and change filesystem semantics.
    return path if path.is_absolute() else Path.cwd() / path


def load_profile(path: Path) -> MemoryProfile:
    """Load one strict versioned JSON document from the explicitly supplied path."""
    try:
        if not isinstance(path, Path):
            raise ProfileError("profile_unavailable")
        path = path if path.is_absolute() else Path.cwd() / path
    except OSError:
        raise ProfileError("profile_unavailable") from None
    raw = _read_regular(path, limit=_PROFILE_BYTES, kind="profile")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or not _FIELDS <= value.keys() <= _FIELDS | {
            "session_id"
        }:
            raise ProfileError("invalid_profile")
        if value["schema"] != _SCHEMA:
            raise ProfileError("invalid_profile")
        body = _mapping(value["scope"], frozenset({"realm", "segments"}))
        if type(body["segments"]) is not list:
            raise ProfileError("invalid_profile")
        segments: list[ScopeSegment] = []
        for item in body["segments"]:
            segment = _mapping(item, frozenset({"kind", "identifier"}))
            segments.append(
                ScopeSegment(
                    cast(str, segment["kind"]), cast(str, segment["identifier"])
                )
            )
        return MemoryProfile(
            endpoint=_endpoint(value["endpoint"]),
            expected_instance_id=_identity(
                value["expected_instance_id"], instance=True
            ),
            scope=Scope(cast(str, body["realm"]), tuple(segments)),
            classification=Classification(value["classification"]),
            credential_file=_credential_path(value["credential_file"], path),
            session_id=_identity(value["session_id"])
            if "session_id" in value
            else None,
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        AuditValueError,
        httpx.InvalidURL,
    ):
        raise ProfileError("invalid_profile") from None


def load_credential(profile: MemoryProfile) -> str:
    """Read only this profile's designated token; no ambient fallback or caching."""
    if type(profile) is not MemoryProfile:
        raise ProfileError("invalid_profile")
    raw = _read_regular(
        profile.credential_file, limit=_CREDENTIAL_BYTES, kind="credential"
    )
    try:
        token = raw.decode("ascii").strip()
    except UnicodeError:
        raise ProfileError("invalid_credential") from None
    if parse_token(token) is None:
        raise ProfileError("invalid_credential")
    return token
