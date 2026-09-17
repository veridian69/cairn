import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn, cast
from uuid import RFC_4122, UUID

from cairn.catalogue.sqlite import CatalogueStorageError
from cairn.catalogue.sqlite import canonical_timestamp as _catalogue_canonical_timestamp
from cairn.catalogue.sqlite import parse_timestamp as _catalogue_parse_timestamp

AUDIT_SCHEMA = "cairn.audit/v1"
ZERO_HASH = bytes(32)
_HASH_DOMAIN = b"cairn.audit/v1\x00"

# The canonical definition of the scope-path depth bound. ``Scope`` is where a
# scope path is constituted, so this is the bound every other in-process copy
# has to agree with — ``cairn.authority.gate`` imports it rather than keeping
# a second private literal. The migrations restate it in SQL and
# ``cairn.catalogue.verification`` re-derives it deliberately; neither is a
# copy that may drift, for reasons stated where they sit.
MAX_SCOPE_SEGMENTS = 16

_REALM_OR_KIND = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SEGMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:/@+%\-]{0,254}\Z")
_ACTION_CODE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REASON_CODE = re.compile(r"[a-z](?:[a-z0-9_]{0,61}[a-z0-9])?\Z")
_LOWER_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_FIELDS = frozenset(
    {
        "action_code",
        "action_kind",
        "affected_assertion_ids",
        "affected_evidence_ids",
        "affected_fact_ids",
        "affected_grant_ids",
        "chain_identity",
        "chain_kind",
        "classification_transition",
        "command_digest",
        "correlation_id",
        "credential_verifier_id",
        "event_id",
        "evidence_digest",
        "evidence_reference",
        "grant_id",
        "idempotency_key",
        "mutation_id",
        "outcome",
        "previous_hash",
        "principal_id",
        "reason_code",
        "recorded_at",
        "replay_of_mutation_id",
        "requested_scope",
        "safe_request_fingerprint",
        "schema",
        "sequence",
        "source_scope",
        "target_scope",
        "trust_transition",
    }
)


class AuditValueError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"audit value error: {code}")


class ChainKind(StrEnum):
    INSTANCE = "instance"
    REALM = "realm"


class ActionKind(StrEnum):
    DATA = "data"
    ADMINISTRATION = "administration"
    SYSTEM = "system"


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"


class ScopeRole(StrEnum):
    SOURCE = "source"
    REQUESTED = "requested"
    TARGET = "target"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class TrustClass(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    FAILED_APPROACH = "failed-approach"


@dataclass(frozen=True, slots=True)
class ScopeSegment:
    kind: str
    identifier: str

    def __post_init__(self) -> None:
        # Type first: the regexes accept only str, so a non-str would raise
        # TypeError from inside re — no code, no audit reason, no durable
        # denial. Scope paths reach here from stored JSON columns as well as
        # from callers, and `[{"kind": 7, "id": 1}]` parses to ints without
        # anything having been tampered with. The codes are the ones these
        # fields already use, so nothing previously valid becomes invalid.
        if type(self.kind) is not str or _REALM_OR_KIND.fullmatch(self.kind) is None:
            raise AuditValueError("invalid_scope_segment_kind")
        if (
            type(self.identifier) is not str
            or _SEGMENT_ID.fullmatch(self.identifier) is None
        ):
            raise AuditValueError("invalid_scope_segment_id")


@dataclass(frozen=True, slots=True)
class Scope:
    realm: str
    segments: tuple[ScopeSegment, ...]

    def __post_init__(self) -> None:
        # Type first, for the reason given on ScopeSegment above.
        if type(self.realm) is not str or _REALM_OR_KIND.fullmatch(self.realm) is None:
            raise AuditValueError("invalid_realm")
        if type(self.segments) is not tuple or len(self.segments) > MAX_SCOPE_SEGMENTS:
            raise AuditValueError("invalid_scope")
        if not all(type(segment) is ScopeSegment for segment in self.segments):
            raise AuditValueError("invalid_scope")


@dataclass(frozen=True, slots=True)
class ClassificationTransition:
    previous: Classification | None
    current: Classification

    def __post_init__(self) -> None:
        if self.previous is not None and type(self.previous) is not Classification:
            raise AuditValueError("invalid_classification_transition")
        if type(self.current) is not Classification:
            raise AuditValueError("invalid_classification_transition")


@dataclass(frozen=True, slots=True)
class TrustTransition:
    previous: TrustClass | None
    current: TrustClass

    def __post_init__(self) -> None:
        if self.previous is not None and type(self.previous) is not TrustClass:
            raise AuditValueError("invalid_trust_transition")
        if type(self.current) is not TrustClass:
            raise AuditValueError("invalid_trust_transition")


@dataclass(frozen=True, slots=True)
class AuditDraft:
    chain_kind: ChainKind
    chain_identity: str
    principal_id: UUID | None
    credential_verifier_id: UUID | None
    grant_id: UUID | None
    action_kind: ActionKind
    action_code: str
    source_scope: Scope | None
    requested_scope: Scope | None
    target_scope: Scope | None
    outcome: Outcome
    reason_code: str
    affected_assertion_ids: tuple[UUID, ...]
    affected_fact_ids: tuple[UUID, ...]
    affected_evidence_ids: tuple[UUID, ...]
    affected_grant_ids: tuple[UUID, ...]
    classification_transition: ClassificationTransition | None
    trust_transition: TrustTransition | None
    evidence_reference: UUID | None
    evidence_digest: bytes | None
    correlation_id: UUID
    idempotency_key: UUID | None
    mutation_id: UUID | None
    command_digest: bytes | None
    replay_of_mutation_id: UUID | None
    safe_request_fingerprint: bytes | None

    def __post_init__(self) -> None:
        if type(self.chain_kind) is not ChainKind:
            raise AuditValueError("invalid_chain_kind")
        _validate_chain_identity(self.chain_kind, self.chain_identity)
        for value in (
            self.principal_id,
            self.credential_verifier_id,
            self.grant_id,
            self.evidence_reference,
            self.mutation_id,
            self.replay_of_mutation_id,
        ):
            _validate_optional_uuid(value)
        _validate_optional_idempotency_uuid(self.idempotency_key)
        _validate_uuid(self.correlation_id)
        if type(self.action_kind) is not ActionKind:
            raise AuditValueError("invalid_action_kind")
        if _ACTION_CODE.fullmatch(self.action_code) is None:
            raise AuditValueError("invalid_action_code")
        for scope in (self.source_scope, self.requested_scope, self.target_scope):
            if scope is not None and type(scope) is not Scope:
                raise AuditValueError("invalid_scope")
        if self.chain_kind is ChainKind.INSTANCE and any(
            scope is not None
            for scope in (self.source_scope, self.requested_scope, self.target_scope)
        ):
            raise AuditValueError("instance_scope_forbidden")
        if type(self.outcome) is not Outcome:
            raise AuditValueError("invalid_outcome")
        if _REASON_CODE.fullmatch(self.reason_code) is None:
            raise AuditValueError("invalid_reason_code")
        for values in (
            self.affected_assertion_ids,
            self.affected_fact_ids,
            self.affected_evidence_ids,
            self.affected_grant_ids,
        ):
            _validate_sorted_uuid_set(values)
        if (
            self.classification_transition is not None
            and type(self.classification_transition) is not ClassificationTransition
        ):
            raise AuditValueError("invalid_classification_transition")
        if (
            self.trust_transition is not None
            and type(self.trust_transition) is not TrustTransition
        ):
            raise AuditValueError("invalid_trust_transition")
        _validate_optional_digest(self.evidence_digest)
        _validate_optional_digest(self.command_digest)
        _validate_optional_digest(self.safe_request_fingerprint)
        _validate_evidence_fields(
            self.action_kind,
            self.action_code,
            self.outcome,
            self.evidence_reference,
            self.evidence_digest,
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    draft: AuditDraft
    sequence: int
    event_id: UUID
    recorded_at: datetime
    previous_hash: bytes

    def __post_init__(self) -> None:
        if type(self.draft) is not AuditDraft:
            raise AuditValueError("invalid_audit_draft")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise AuditValueError("invalid_sequence")
        _validate_uuid(self.event_id)
        _canonical_timestamp(self.recorded_at)
        _validate_digest(self.previous_hash)
        if self.sequence == 1 and self.previous_hash != ZERO_HASH:
            raise AuditValueError("invalid_previous_hash")


type JsonValue = str | int | bool | None | list[JsonValue] | dict[str, JsonValue]


def canonical_audit_bytes(event: AuditEvent) -> bytes:
    document = _event_document(event)
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_audit_event(event: AuditEvent) -> bytes:
    return hashlib.sha256(_HASH_DOMAIN + canonical_audit_bytes(event)).digest()


def parse_canonical_audit_bytes(data: bytes) -> AuditEvent:
    if type(data) is not bytes:
        raise AuditValueError("invalid_event_bytes")
    try:
        text = data.decode("utf-8", errors="strict")
        raw = cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_float=_reject_float,
                parse_constant=_reject_constant,
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditValueError("invalid_event_json") from error

    document = _require_object(raw, _EVENT_FIELDS)
    if _required_string(document, "schema") != AUDIT_SCHEMA:
        raise AuditValueError("invalid_event_schema")
    draft = AuditDraft(
        chain_kind=_chain_kind(document["chain_kind"]),
        chain_identity=_required_string(document, "chain_identity"),
        principal_id=_optional_uuid(document["principal_id"]),
        credential_verifier_id=_optional_uuid(document["credential_verifier_id"]),
        grant_id=_optional_uuid(document["grant_id"]),
        action_kind=_action_kind(document["action_kind"]),
        action_code=_required_string(document, "action_code"),
        source_scope=_scope_value(document["source_scope"]),
        requested_scope=_scope_value(document["requested_scope"]),
        target_scope=_scope_value(document["target_scope"]),
        outcome=_outcome(document["outcome"]),
        reason_code=_required_string(document, "reason_code"),
        affected_assertion_ids=_uuid_array(document["affected_assertion_ids"]),
        affected_fact_ids=_uuid_array(document["affected_fact_ids"]),
        affected_evidence_ids=_uuid_array(document["affected_evidence_ids"]),
        affected_grant_ids=_uuid_array(document["affected_grant_ids"]),
        classification_transition=_classification_value(
            document["classification_transition"]
        ),
        trust_transition=_trust_value(document["trust_transition"]),
        evidence_reference=_optional_uuid(document["evidence_reference"]),
        evidence_digest=_optional_digest(document["evidence_digest"]),
        correlation_id=_required_uuid(document["correlation_id"]),
        idempotency_key=_optional_idempotency_uuid(document["idempotency_key"]),
        mutation_id=_optional_uuid(document["mutation_id"]),
        command_digest=_optional_digest(document["command_digest"]),
        replay_of_mutation_id=_optional_uuid(document["replay_of_mutation_id"]),
        safe_request_fingerprint=_optional_digest(document["safe_request_fingerprint"]),
    )
    sequence = document["sequence"]
    if type(sequence) is not int:
        raise AuditValueError("invalid_sequence")
    event = AuditEvent(
        draft=draft,
        sequence=sequence,
        event_id=_required_uuid(document["event_id"]),
        recorded_at=_timestamp_value(document["recorded_at"]),
        previous_hash=_required_digest(document["previous_hash"]),
    )
    if canonical_audit_bytes(event) != data:
        raise AuditValueError("non_canonical_event")
    return event


def _event_document(event: AuditEvent) -> dict[str, JsonValue]:
    draft = event.draft
    return {
        "action_code": draft.action_code,
        "action_kind": draft.action_kind.value,
        "affected_assertion_ids": _uuid_strings(draft.affected_assertion_ids),
        "affected_evidence_ids": _uuid_strings(draft.affected_evidence_ids),
        "affected_fact_ids": _uuid_strings(draft.affected_fact_ids),
        "affected_grant_ids": _uuid_strings(draft.affected_grant_ids),
        "chain_identity": draft.chain_identity,
        "chain_kind": draft.chain_kind.value,
        "classification_transition": _classification_document(
            draft.classification_transition
        ),
        "command_digest": _digest_hex(draft.command_digest),
        "correlation_id": str(draft.correlation_id),
        "credential_verifier_id": _uuid_string(draft.credential_verifier_id),
        "event_id": str(event.event_id),
        "evidence_digest": _digest_hex(draft.evidence_digest),
        "evidence_reference": _uuid_string(draft.evidence_reference),
        "grant_id": _uuid_string(draft.grant_id),
        "idempotency_key": _uuid_string(draft.idempotency_key),
        "mutation_id": _uuid_string(draft.mutation_id),
        "outcome": draft.outcome.value,
        "previous_hash": event.previous_hash.hex(),
        "principal_id": _uuid_string(draft.principal_id),
        "reason_code": draft.reason_code,
        "recorded_at": _canonical_timestamp(event.recorded_at),
        "replay_of_mutation_id": _uuid_string(draft.replay_of_mutation_id),
        "requested_scope": _scope_document(draft.requested_scope),
        "safe_request_fingerprint": _digest_hex(draft.safe_request_fingerprint),
        "schema": AUDIT_SCHEMA,
        "sequence": event.sequence,
        "source_scope": _scope_document(draft.source_scope),
        "target_scope": _scope_document(draft.target_scope),
        "trust_transition": _trust_document(draft.trust_transition),
    }


def _scope_document(scope: Scope | None) -> dict[str, JsonValue] | None:
    if scope is None:
        return None
    return {
        "realm": scope.realm,
        "segments": [
            {"id": segment.identifier, "kind": segment.kind}
            for segment in scope.segments
        ],
    }


def _classification_document(
    transition: ClassificationTransition | None,
) -> dict[str, JsonValue] | None:
    if transition is None:
        return None
    return {
        "from": transition.previous.value if transition.previous else None,
        "to": transition.current.value,
    }


def _trust_document(
    transition: TrustTransition | None,
) -> dict[str, JsonValue] | None:
    if transition is None:
        return None
    return {
        "from": transition.previous.value if transition.previous else None,
        "to": transition.current.value,
    }


def _uuid_strings(values: tuple[UUID, ...]) -> list[JsonValue]:
    return [str(value) for value in values]


def _uuid_string(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _digest_hex(value: bytes | None) -> str | None:
    return value.hex() if value is not None else None


def _validate_chain_identity(kind: ChainKind, identity: str) -> None:
    if kind is ChainKind.INSTANCE:
        _parse_uuid4(identity)
    elif _REALM_OR_KIND.fullmatch(identity) is None:
        raise AuditValueError("invalid_chain_identity")


def _validate_uuid(value: UUID) -> None:
    if type(value) is not UUID or value.version != 4 or value.variant != RFC_4122:
        raise AuditValueError("invalid_uuid")


def _validate_optional_uuid(value: UUID | None) -> None:
    if value is not None:
        _validate_uuid(value)


def _validate_optional_idempotency_uuid(value: UUID | None) -> None:
    if value is not None and (type(value) is not UUID or value.variant != RFC_4122):
        raise AuditValueError("invalid_uuid")


def _parse_uuid4(value: str) -> UUID:
    if type(value) is not str or not value.isascii():
        raise AuditValueError("invalid_uuid")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AuditValueError("invalid_uuid") from error
    _validate_uuid(parsed)
    if str(parsed) != value:
        raise AuditValueError("invalid_uuid")
    return parsed


def _validate_sorted_uuid_set(values: tuple[UUID, ...]) -> None:
    if type(values) is not tuple:
        raise AuditValueError("invalid_identity_set")
    for value in values:
        _validate_uuid(value)
    canonical = tuple(sorted(values, key=str))
    if values != canonical or len(set(values)) != len(values):
        raise AuditValueError("invalid_identity_set")


def _validate_digest(value: bytes) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise AuditValueError("invalid_digest")


def _validate_optional_digest(value: bytes | None) -> None:
    if value is not None:
        _validate_digest(value)


def _validate_evidence_fields(
    action_kind: ActionKind,
    action_code: str,
    outcome: Outcome,
    evidence_reference: UUID | None,
    evidence_digest: bytes | None,
) -> None:
    # P-16: evidence_reference and evidence_digest are complementary, not
    # mutually exclusive — they name which evidence supports the event and
    # its content identity. A data-plane allow-promote event requires both
    # (I-66); a data-plane allow-ingest event carries both when it creates
    # an evidence record and neither otherwise; every other action_kind,
    # action_code and outcome combination carries neither — the evidence
    # may be precisely what is unknown or unauthorised. Gated on
    # action_kind as well as action_code so an administration- or
    # system-kind event cannot be forced into evidence fields merely by
    # reusing the action_code "promote" or "ingest".
    is_data_allow = action_kind is ActionKind.DATA and outcome is Outcome.ALLOW
    if is_data_allow and action_code == "promote":
        if evidence_reference is None or evidence_digest is None:
            raise AuditValueError("evidence_fields_required")
    elif is_data_allow and action_code == "ingest":
        if (evidence_reference is None) != (evidence_digest is None):
            raise AuditValueError("evidence_fields_unpaired")
    elif evidence_reference is not None or evidence_digest is not None:
        raise AuditValueError("evidence_fields_forbidden")


def _canonical_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AuditValueError("invalid_timestamp")
    return _catalogue_canonical_timestamp(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuditValueError("duplicate_event_field")
        result[key] = value
    return result


def _reject_float(_value: str) -> NoReturn:
    raise AuditValueError("float_forbidden")


def _reject_constant(_value: str) -> NoReturn:
    raise AuditValueError("invalid_event_json")


def _require_object(
    value: object,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AuditValueError("invalid_event_fields")
    if not all(type(key) is str for key in value):
        raise AuditValueError("invalid_event_fields")
    return cast(dict[str, object], value)


def _required_string(document: dict[str, object], field: str) -> str:
    value = document[field]
    if type(value) is not str or not value.isascii():
        raise AuditValueError("invalid_event_field")
    return value


def _required_uuid(value: object) -> UUID:
    if type(value) is not str:
        raise AuditValueError("invalid_uuid")
    return _parse_uuid4(value)


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _required_uuid(value)


def _optional_idempotency_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if type(value) is not str or not value.isascii():
        raise AuditValueError("invalid_uuid")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AuditValueError("invalid_uuid") from error
    _validate_optional_idempotency_uuid(parsed)
    if str(parsed) != value:
        raise AuditValueError("invalid_uuid")
    return parsed


def _uuid_array(value: object) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        raise AuditValueError("invalid_identity_set")
    return tuple(_required_uuid(item) for item in value)


def _required_digest(value: object) -> bytes:
    if type(value) is not str or _LOWER_HEX_DIGEST.fullmatch(value) is None:
        raise AuditValueError("invalid_digest")
    return bytes.fromhex(value)


def _optional_digest(value: object) -> bytes | None:
    if value is None:
        return None
    return _required_digest(value)


def _chain_kind(value: object) -> ChainKind:
    if type(value) is not str:
        raise AuditValueError("invalid_chain_kind")
    try:
        return ChainKind(value)
    except (TypeError, ValueError) as error:
        raise AuditValueError("invalid_chain_kind") from error


def _action_kind(value: object) -> ActionKind:
    if type(value) is not str:
        raise AuditValueError("invalid_action_kind")
    try:
        return ActionKind(value)
    except (TypeError, ValueError) as error:
        raise AuditValueError("invalid_action_kind") from error


def _outcome(value: object) -> Outcome:
    if type(value) is not str:
        raise AuditValueError("invalid_outcome")
    try:
        return Outcome(value)
    except (TypeError, ValueError) as error:
        raise AuditValueError("invalid_outcome") from error


def _scope_value(value: object) -> Scope | None:
    if value is None:
        return None
    document = _require_object(value, frozenset({"realm", "segments"}))
    realm = _required_string(document, "realm")
    raw_segments = document["segments"]
    if not isinstance(raw_segments, list):
        raise AuditValueError("invalid_scope")
    segments: list[ScopeSegment] = []
    for raw_segment in raw_segments:
        segment = _require_object(raw_segment, frozenset({"id", "kind"}))
        segments.append(
            ScopeSegment(
                kind=_required_string(segment, "kind"),
                identifier=_required_string(segment, "id"),
            )
        )
    return Scope(realm=realm, segments=tuple(segments))


def _classification_value(value: object) -> ClassificationTransition | None:
    if value is None:
        return None
    document = _require_object(value, frozenset({"from", "to"}))
    previous_value = document["from"]
    previous = None if previous_value is None else _classification(previous_value)
    return ClassificationTransition(
        previous=previous,
        current=_classification(document["to"]),
    )


def _classification(value: object) -> Classification:
    if type(value) is not str:
        raise AuditValueError("invalid_classification_transition")
    try:
        return Classification(value)
    except (TypeError, ValueError) as error:
        raise AuditValueError("invalid_classification_transition") from error


def _trust_value(value: object) -> TrustTransition | None:
    if value is None:
        return None
    document = _require_object(value, frozenset({"from", "to"}))
    previous_value = document["from"]
    previous = None if previous_value is None else _trust_class(previous_value)
    return TrustTransition(
        previous=previous,
        current=_trust_class(document["to"]),
    )


def _trust_class(value: object) -> TrustClass:
    if type(value) is not str:
        raise AuditValueError("invalid_trust_transition")
    try:
        return TrustClass(value)
    except (TypeError, ValueError) as error:
        raise AuditValueError("invalid_trust_transition") from error


def _timestamp_value(value: object) -> datetime:
    if type(value) is not str or not value.isascii() or len(value) != 27:
        raise AuditValueError("invalid_timestamp")
    try:
        parsed = _catalogue_parse_timestamp(value)
    except CatalogueStorageError as error:
        raise AuditValueError("invalid_timestamp") from error
    if _canonical_timestamp(parsed) != value:
        raise AuditValueError("invalid_timestamp")
    return parsed
