"""Immutable values exchanged by Cairn's provider-neutral memory client."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope

if TYPE_CHECKING:
    from cairn.client.errors import FailureMetadata

type FrozenJSON = (
    None
    | bool
    | int
    | float
    | str
    | tuple["FrozenJSON", ...]
    | Mapping[str, "FrozenJSON"]
)
type FrozenJSONObject = Mapping[str, FrozenJSON]


def freeze_json(value: object) -> FrozenJSON:
    """Recursively detach and freeze a decoded JSON value."""
    if value is None or type(value) in {bool, int}:
        return cast(None | bool | int, value)
    if type(value) is float:
        number = value
        if not math.isfinite(number):
            raise ValueError("non_finite_json_number")
        return number
    if type(value) is str:
        text = value
        text.encode("utf-8")
        return text
    if type(value) is list:
        return tuple(freeze_json(item) for item in cast(list[object], value))
    if type(value) is dict:
        copied: dict[str, FrozenJSON] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("json_object_key_not_string")
            copied[key] = freeze_json(item)
        return MappingProxyType(copied)
    raise ValueError("value_not_json")


def freeze_object(value: object) -> FrozenJSONObject:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("json_body_not_object")
    return frozen


def _validate_timestamp(value: datetime | None, field: str) -> None:
    if value is not None and (
        type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None
    ):
        raise ValueError(f"{field}_must_be_timezone_aware")


@dataclass(frozen=True, slots=True)
class DurableObservation:
    """A callback-selected fact; host authority supplies all policy fields."""

    body: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.body) is not str:
            raise TypeError("body must be str")
        _validate_timestamp(self.valid_from, "valid_from")
        _validate_timestamp(self.valid_to, "valid_to")
        _validate_timestamp(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    """A labelled, immutable data packet kept separate from model instructions."""

    data: FrozenJSONObject
    source: Literal["cairn-memory/v1"] = "cairn-memory/v1"
    content_role: Literal["untrusted-data"] = "untrusted-data"


@dataclass(frozen=True, slots=True)
class TurnInput:
    user_input: str
    recalled: RecalledMemory

    def __post_init__(self) -> None:
        if type(self.user_input) is not str:
            raise TypeError("user_input must be str")


@dataclass(frozen=True, slots=True)
class ModelTurn:
    response: str
    observations: tuple[DurableObservation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.response) is not str:
            raise TypeError("response must be str")
        if type(self.observations) is not tuple or not all(
            type(item) is DurableObservation for item in self.observations
        ):
            raise TypeError("observations must be a tuple of DurableObservation")


type ModelCallback = Callable[[TurnInput], Awaitable[ModelTurn]]


class PersistenceStatus(StrEnum):
    SKIPPED = "skipped"
    COMMITTED = "committed"
    REPLAYED = "replayed"


class ConnectionStatus(StrEnum):
    """Memory handshake status; READY does not promise later operation success."""

    READY = "ready"
    UNREACHABLE = "unreachable"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORISATION_DENIED = "authorisation_denied"
    NO_AUTHORISED_OPERATIONS = "no_authorised_operations"
    INCOMPATIBLE = "incompatible"
    INSTANCE_MISMATCH = "instance_mismatch"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class DiagnosticPermissions:
    """Current exact-scope grant checks, not an operation preflight.

    Retrieve includes memory read clearance; ingest/promote include target
    write classification. Invalidate is classification independent. Source,
    evidence, content screening and fresh authorisation checks still apply.
    """

    retrieve: bool
    ingest: bool
    promote: bool
    invalidate: bool


@dataclass(frozen=True, slots=True)
class ConnectionDiagnostics:
    """Validated metadata from the authenticated memory diagnostic endpoint."""

    status: ConnectionStatus
    instance_id: UUID | None = None
    product_version: str | None = None
    contract_identity: str | None = None
    contract_digest: str | None = None
    mcp_contract_digest: str | None = None
    failure: FailureMetadata | None = None
    principal_id: UUID | None = None
    principal_kind: Literal["human", "workload"] | None = None
    scope: Scope | None = None
    classification: Classification | None = None
    permissions: DiagnosticPermissions | None = None
    evaluated_at: datetime | None = None
    permission_basis: Literal["current_grants_only"] | None = None


@dataclass(frozen=True, slots=True)
class PersistenceReceipt:
    status: PersistenceStatus
    idempotency_key: UUID | None
    result: FrozenJSONObject | None = None
    mutation_receipt: FrozenJSONObject | None = None
    audit_receipt: FrozenJSONObject | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    response: str
    persistence: PersistenceReceipt
