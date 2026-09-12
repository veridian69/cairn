"""Immutable host-input admission for the opt-in conversation adapter.

The configured host supplies this bundle before serving model tools. Its text
is context, not proof of human identity, approval or truth. No MCP operation can
admit sources. This module never writes a transcript, memory or retry store.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import RFC_4122, UUID

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client.diagnostics import _unique_object
from cairn.client.profiles import MemoryProfile

SCHEMA = "cairn.conversation-sources/v1"
MAX_SOURCE_BYTES = 16384
MAX_DOCUMENT_BYTES = 131072


class SourceError(ValueError):
    """Closed source-admission errors, containing no input or path."""

    def __init__(self, code: str = "invalid_sources") -> None:
        self.code = code
        super().__init__(code)


def _uuid(value: object, *, v4: bool = False) -> UUID:
    if type(value) is not str:
        raise SourceError()
    identity = UUID(value)
    if (
        str(identity) != value
        or identity.variant != RFC_4122
        or (v4 and identity.version != 4)
    ):
        raise SourceError()
    return identity


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _mapping(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise SourceError()
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True, repr=False)
class HostSource:
    source_id: UUID
    body: str
    origin: Literal["host_input"] = "host_input"

    def __post_init__(self) -> None:
        if (
            type(self.source_id) is not UUID
            or self.source_id.variant != RFC_4122
            or type(self.body) is not str
            or not self.body.strip()
            or type(self.origin) is not str
            or self.origin != "host_input"
        ):
            raise SourceError()
        try:
            if len(self.body.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise SourceError("source_too_large")
        except UnicodeError:
            raise SourceError() from None

    def to_document(self) -> dict[str, object]:
        return {
            "source_id": str(self.source_id),
            "origin": self.origin,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True, repr=False)
class SourceBundle:
    instance_id: UUID
    principal_id: UUID
    scope: Scope
    classification: Classification
    session_id: UUID
    sources: tuple[HostSource, ...]

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not UUID or value.variant != RFC_4122
                for value in (self.instance_id, self.principal_id, self.session_id)
            )
            or self.instance_id.version != 4
            or self.principal_id.version != 4
            or type(self.scope) is not Scope
            or type(self.classification) is not Classification
            or type(self.sources) is not tuple
            or not 1 <= len(self.sources) <= 4
            or any(type(source) is not HostSource for source in self.sources)
        ):
            raise SourceError()
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise SourceError()
        if len(_canonical(self.to_document()).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise SourceError("sources_too_large")

    def require_context(self, profile: MemoryProfile, expected_principal: UUID) -> None:
        if (
            type(profile) is not MemoryProfile
            or type(expected_principal) is not UUID
            or self.instance_id != profile.expected_instance_id
            or self.principal_id != expected_principal
            or self.scope != profile.scope
            or self.classification != profile.classification
            or self.session_id != profile.session_id
        ):
            raise SourceError("source_context_mismatch")

    def to_document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "instance_id": str(self.instance_id),
            "principal_id": str(self.principal_id),
            "scope": {
                "realm": self.scope.realm,
                "segments": [
                    {"kind": segment.kind, "identifier": segment.identifier}
                    for segment in self.scope.segments
                ],
            },
            "classification": self.classification.value,
            "session_id": str(self.session_id),
            "sources": [source.to_document() for source in self.sources],
        }

    def source(self, source_id: UUID) -> HostSource:
        if type(source_id) is not UUID:
            raise SourceError("unknown_source")
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise SourceError("unknown_source")

    def evidence_payload(self, source_id: UUID) -> str:
        document = self.to_document()
        document.pop("sources")
        document["schema"] = "cairn.conversation-evidence/v1"
        document["source"] = self.source(source_id).to_document()
        return _canonical(document)


def create_source_bundle(
    profile: MemoryProfile, *, expected_principal: UUID, sources: tuple[HostSource, ...]
) -> SourceBundle:
    """Trusted host construction; not exposed through the model tool surface."""
    if type(profile) is not MemoryProfile or profile.session_id is None:
        raise SourceError("source_context_mismatch")
    return SourceBundle(
        profile.expected_instance_id,
        expected_principal,
        profile.scope,
        profile.classification,
        profile.session_id,
        sources,
    )


def _read_private(path: Path) -> bytes:
    """Read one bounded owner-private regular file, without following symlinks."""
    directory: int | None = None
    descriptor: int | None = None
    try:
        if os.name != "posix" or not isinstance(path, Path):
            raise SourceError("sources_unavailable")
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
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise SourceError("sources_unavailable")
        data = bytearray()
        while len(data) <= MAX_DOCUMENT_BYTES:
            chunk = os.read(descriptor, MAX_DOCUMENT_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise SourceError("sources_too_large")
        return bytes(data)
    except OSError:
        raise SourceError("sources_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _constant(value: str) -> object:
    raise SourceError()


def load_sources(
    path: Path, *, profile: MemoryProfile, expected_principal: UUID
) -> SourceBundle:
    """Snapshot explicit host input once; later file mutations have no effect."""
    try:
        document = _mapping(
            json.loads(
                _read_private(path).decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_constant,
            ),
            {
                "schema",
                "instance_id",
                "principal_id",
                "scope",
                "classification",
                "session_id",
                "sources",
            },
        )
        if document["schema"] != SCHEMA or type(document["sources"]) is not list:
            raise SourceError()
        scope_document = _mapping(document["scope"], {"realm", "segments"})
        if (
            type(scope_document["realm"]) is not str
            or type(scope_document["segments"]) is not list
        ):
            raise SourceError()
        segments: list[ScopeSegment] = []
        for raw in cast(list[object], scope_document["segments"]):
            segment = _mapping(raw, {"kind", "identifier"})
            if (
                type(segment["kind"]) is not str
                or type(segment["identifier"]) is not str
            ):
                raise SourceError()
            segments.append(ScopeSegment(segment["kind"], segment["identifier"]))
        admitted: list[HostSource] = []
        for raw in cast(list[object], document["sources"]):
            source = _mapping(raw, {"source_id", "origin", "body"})
            if source["origin"] != "host_input" or type(source["body"]) is not str:
                raise SourceError()
            admitted.append(HostSource(_uuid(source["source_id"]), source["body"]))
        if type(document["classification"]) is not str:
            raise SourceError()
        bundle = SourceBundle(
            _uuid(document["instance_id"], v4=True),
            _uuid(document["principal_id"], v4=True),
            Scope(scope_document["realm"], tuple(segments)),
            Classification(document["classification"]),
            _uuid(document["session_id"]),
            tuple(admitted),
        )
        bundle.require_context(profile, expected_principal)
        return bundle
    except SourceError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise SourceError() from None
