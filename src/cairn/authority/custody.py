"""Cairn custody value vocabulary: assertions, facts, invalidations and evidence.

Reuses ``Scope``, ``ScopeSegment``, ``Classification`` and ``TrustClass`` from
``cairn.catalogue.audit`` as the single scope and classification vocabulary
(I-65) — there is no parallel realm, segment or classification validation
here. Realm and segment shape is enforced by constructing a ``Scope`` purely
for its ``__post_init__`` side effect, so a violation there raises
``AuditValueError``, not ``CustodyValueError``.

``Classification`` and UUID-typed fields are deliberately not validated at
this layer: no code in the closed ``CustodyValueError`` vocabulary covers
them, mypy enforces their static type, migration 0003's ``CHECK`` constraints
back them at the storage layer, and Task 12's custody verification checks
stored values. ``SourceType`` and ``TrustClass`` differ only because the
approved vocabulary gave them dedicated codes.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass

_BODY_MAX_BYTES = 65536
_REASON_MAX_BYTES = 4096
_METADATA_MAX_BYTES = 65536
_METADATA_MAX_DEPTH = 8
_METADATA_MAX_KEYS = 256
_PAYLOAD_MAX_LENGTH = 1048576
_URI_MAX_BYTES = 2048
_DIGEST_LENGTH = 32

_EXTERNAL_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[!-~]*\Z")


class SourceType(StrEnum):
    AGENT_CLAIM = "agent-claim"
    VERIFIED_CHECK = "verified-check"
    HUMAN = "human"


# The batch codes the command layer reports, named here rather than repeated
# as bare literals by each command: the layers must agree on the string, and
# a comment is not agreement. MAX_BATCH_FACTS is the I-30 bound they are
# reported against. DUPLICATE_IDENTITY joined them with promotion, the first
# command whose batch is caller-supplied identities rather than drafts.
EMPTY_BATCH = "empty_batch"
BATCH_TOO_LARGE = "batch_too_large"
DUPLICATE_IDENTITY = "duplicate_identity"
MAX_BATCH_FACTS = 100


class CustodyValueError(Exception):
    """Closed vocabulary of custody value-validation codes.

    Codes: ``invalid_body``, ``invalid_validity``, ``invalid_trust``,
    ``invalid_source_type``, ``invalid_metadata``, ``invalid_payload``,
    ``invalid_reason``, ``invalid_uri``, ``invalid_digest``,
    ``empty_batch``, ``batch_too_large``, ``duplicate_identity``.

    ``EMPTY_BATCH``, ``BATCH_TOO_LARGE`` and ``DUPLICATE_IDENTITY`` are
    reserved for the command layer (Tasks 7-9 batch handling); this module
    never raises them.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"custody value error: {code}")


@dataclass(frozen=True, slots=True)
class FactDraft:
    body: str
    valid_from: datetime | None
    valid_to: datetime | None

    def __post_init__(self) -> None:
        _validate_body(self.body)
        _validate_validity_window(self.valid_from, self.valid_to)


@dataclass(frozen=True, slots=True)
class IngestedProvenance:
    assertion_id: UUID


@dataclass(frozen=True, slots=True)
class PromotedProvenance:
    derived_from: UUID
    promoted_by: UUID
    evidence_id: UUID


type FactProvenance = IngestedProvenance | PromotedProvenance


@dataclass(frozen=True, slots=True)
class AssertionRecord:
    assertion_id: UUID
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    classification: Classification
    source_type: SourceType
    principal_id: UUID
    observed_at: datetime | None
    metadata: str | None
    recorded_at: datetime

    def __post_init__(self) -> None:
        _validate_scope(self.realm_id, self.segments)
        if type(self.source_type) is not SourceType:
            raise CustodyValueError("invalid_source_type")
        if self.observed_at is not None:
            _validate_tz_aware(self.observed_at)
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        _validate_tz_aware(self.recorded_at)


@dataclass(frozen=True, slots=True)
class FactRecord:
    fact_id: UUID
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    body: str
    trust: TrustClass
    classification: Classification
    provenance: FactProvenance
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime

    def __post_init__(self) -> None:
        _validate_scope(self.realm_id, self.segments)
        _validate_body(self.body)
        if type(self.trust) is not TrustClass:
            raise CustodyValueError("invalid_trust")
        _validate_validity_window(self.valid_from, self.valid_to)
        _validate_tz_aware(self.recorded_at)


@dataclass(frozen=True, slots=True)
class InvalidationRecord:
    fact_id: UUID
    invalidated_at: datetime
    principal_id: UUID
    superseded_by: UUID | None
    reason: str

    def __post_init__(self) -> None:
        _validate_tz_aware(self.invalidated_at)
        validate_reason(self.reason)


@dataclass(frozen=True, slots=True)
class ExactEvidence:
    assertion_id: UUID
    payload_length: int
    payload_digest: bytes

    def __post_init__(self) -> None:
        _validate_payload_length(self.payload_length)
        _validate_digest(self.payload_digest)


@dataclass(frozen=True, slots=True)
class ExternalEvidence:
    external_uri: str
    payload_digest: bytes

    def __post_init__(self) -> None:
        _validate_external_uri(self.external_uri)
        _validate_digest(self.payload_digest)


type EvidenceCustody = ExactEvidence | ExternalEvidence


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: UUID
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    classification: Classification
    custody: EvidenceCustody
    recorded_at: datetime

    def __post_init__(self) -> None:
        _validate_scope(self.realm_id, self.segments)
        _validate_tz_aware(self.recorded_at)


def _validate_scope(realm_id: str, segments: tuple[ScopeSegment, ...]) -> None:
    # Both steps are needed. Scope validates the realm, the tuple type, the
    # length bound and that every member *is* a ScopeSegment; it never looks
    # inside one, because segment content is ScopeSegment's own invariant.
    # Records are built from stored rows as well as from validated commands,
    # so this must not depend on the caller having checked content already.
    # The Scope call comes first: it is what makes the attribute reads below
    # safe from AttributeError.
    Scope(realm=realm_id, segments=segments)
    for segment in segments:
        ScopeSegment(kind=segment.kind, identifier=segment.identifier)


def _validate_body(value: str) -> None:
    if type(value) is not str or not _byte_length_within(value, 1, _BODY_MAX_BYTES):
        raise CustodyValueError("invalid_body")


def validate_reason(value: str) -> None:
    """Public because promotion has a reason but no record to carry it.

    An invalidation's reason lands in ``fact_invalidations.reason``, so
    ``InvalidationRecord`` validates it on the way past. A promotion's reason
    has nowhere durable to go under the approved schema — it survives only as
    an input to the P-20 command digest — so the command layer must reach the
    same validator directly rather than grow a parallel one.
    """
    if type(value) is not str or not _byte_length_within(value, 1, _REASON_MAX_BYTES):
        raise CustodyValueError("invalid_reason")


def _byte_length_within(value: str, minimum: int, maximum: int) -> bool:
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return minimum <= length <= maximum


def _validate_tz_aware(value: datetime) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise CustodyValueError("invalid_validity")


def _validate_validity_window(
    valid_from: datetime | None, valid_to: datetime | None
) -> None:
    if valid_from is not None:
        _validate_tz_aware(valid_from)
    if valid_to is not None:
        _validate_tz_aware(valid_to)
    if valid_from is not None and valid_to is not None and valid_from >= valid_to:
        raise CustodyValueError("invalid_validity")


def _validate_payload_length(value: int) -> None:
    if type(value) is not int or not (1 <= value <= _PAYLOAD_MAX_LENGTH):
        raise CustodyValueError("invalid_payload")


def _validate_digest(value: bytes) -> None:
    if type(value) is not bytes or len(value) != _DIGEST_LENGTH:
        raise CustodyValueError("invalid_digest")


def _validate_external_uri(value: str) -> None:
    if type(value) is not str or not _byte_length_within(value, 1, _URI_MAX_BYTES):
        raise CustodyValueError("invalid_uri")
    if _EXTERNAL_URI.fullmatch(value) is None:
        raise CustodyValueError("invalid_uri")


def _validate_metadata(value: str) -> None:
    if type(value) is not str or not _byte_length_within(value, 1, _METADATA_MAX_BYTES):
        raise CustodyValueError("invalid_metadata")
    # Reject on raw nesting depth before parsing. json.loads's scanner
    # recurses per nesting level, so parsing untrusted, deeply-nested text
    # first would let a value well within the byte bound blow Python's
    # recursion limit and leak a bare RecursionError instead of a stable
    # CustodyValueError. This scan is iterative and bounded by the
    # byte-length check above, so it is safe at any nesting depth.
    if _raw_json_nesting_depth(value) > _METADATA_MAX_DEPTH:
        raise CustodyValueError("invalid_metadata")
    try:
        parsed = json.loads(value, parse_constant=_reject_metadata_constant)
    except json.JSONDecodeError as error:
        raise CustodyValueError("invalid_metadata") from error
    if _json_depth(parsed) > _METADATA_MAX_DEPTH:
        raise CustodyValueError("invalid_metadata")
    if _json_key_count(parsed) > _METADATA_MAX_KEYS:
        raise CustodyValueError("invalid_metadata")
    canonical = json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical.encode("utf-8") != value.encode("utf-8"):
        raise CustodyValueError("invalid_metadata")


def _reject_metadata_constant(_value: str) -> NoReturn:
    raise CustodyValueError("invalid_metadata")


def _raw_json_nesting_depth(text: str) -> int:
    """Peak ``{``/``[`` nesting depth of raw JSON text, without parsing it.

    A single linear pass, so it is safe against arbitrarily deep input —
    unlike a recursive-descent parser, it cannot blow the interpreter's
    recursion limit. Braces and brackets inside string literals are
    skipped, tracked via a minimal string/escape state machine; it does not
    otherwise validate JSON syntax, since malformed text is still rejected
    by the real parser afterwards.
    """
    depth = 0
    peak = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
            peak = max(peak, depth)
        elif character in "}]":
            depth -= 1
    return peak


def _json_depth(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _json_key_count(value: object) -> int:
    if isinstance(value, dict):
        return len(value) + sum(_json_key_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_json_key_count(item) for item in value)
    return 0
