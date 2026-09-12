"""Shared ``/v1`` wire encodings, envelopes and the failure body.

The frozen wire encodings are I-71's: UUIDs are canonical lowercase
hyphenated strings, timestamps are the catalogue's canonical 27-character
RFC 3339 UTC form with exactly six fractional digits and a trailing ``Z``,
and digests are 64-character lowercase hex. The success envelope and the
failure body with its two ``detail`` forms are I-72's. Every model is
strict per P-33 — unknown fields rejected, values frozen — and these are
the only Pydantic models outside ``cairn.runtime.config``.

The wire-validation rule identities are the closed vocabulary the adapter
may name in an ``invalid_request`` ``detail``. They are bare snake_case,
versioned by ``/v1`` itself rather than independently — unlike the secret
policy's ``cairn.secret/v1/...`` names, whose namespace exists because
I-31 versions that policy apart from the API (Operator, 7 August 2026).
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cairn.authority.gate import INVALID_REQUEST_MESSAGE
from cairn.catalogue.audit import ChainKind
from cairn.catalogue.sqlite import canonical_timestamp
from cairn.catalogue.transactions import FailureCode, RetryClass, StableFailure

# The closed wire-validation rule vocabulary. ``WIRE_RULES`` pins the full
# set the same way P-25's parity constant pins the detector inventory: a
# rule added or renamed without updating it fails a test rather than
# drifting into the contract unnoticed.
RULE_BODY_NOT_OBJECT = "body_not_object"
RULE_BODY_TOO_LARGE = "body_too_large"
RULE_DUPLICATE_JSON_KEY = "duplicate_json_key"
RULE_IDEMPOTENCY_KEY_DUPLICATED = "idempotency_key_duplicated"
RULE_IDEMPOTENCY_KEY_FORBIDDEN = "idempotency_key_forbidden"
RULE_IDEMPOTENCY_KEY_MALFORMED = "idempotency_key_malformed"
RULE_IDEMPOTENCY_KEY_MISSING = "idempotency_key_missing"
RULE_INVALID_CONTENT_TYPE = "invalid_content_type"
RULE_INVALID_ENCODING = "invalid_encoding"
RULE_INVALID_VALUE = "invalid_value"
RULE_MALFORMED_JSON = "malformed_json"
RULE_METHOD_NOT_ALLOWED = "method_not_allowed"
RULE_MISSING_FIELD = "missing_field"
RULE_UNKNOWN_FIELD = "unknown_field"

# I-29's contract identity: what ``/v1/instance`` reports and what the
# generated artefact names itself. Shared rather than private to
# ``routes.py`` since Task 10 gave it a second reader.
CONTRACT_IDENTITY = "cairn/v1"

WIRE_RULES = frozenset(
    {
        RULE_BODY_NOT_OBJECT,
        RULE_BODY_TOO_LARGE,
        RULE_DUPLICATE_JSON_KEY,
        RULE_IDEMPOTENCY_KEY_DUPLICATED,
        RULE_IDEMPOTENCY_KEY_FORBIDDEN,
        RULE_IDEMPOTENCY_KEY_MALFORMED,
        RULE_IDEMPOTENCY_KEY_MISSING,
        RULE_INVALID_CONTENT_TYPE,
        RULE_INVALID_ENCODING,
        RULE_INVALID_VALUE,
        RULE_MALFORMED_JSON,
        RULE_METHOD_NOT_ALLOWED,
        RULE_MISSING_FIELD,
        RULE_UNKNOWN_FIELD,
    }
)


def _enumeration(values: Iterable[str]) -> dict[str, JsonValue]:
    """A published enumeration, in the shape ``json_schema_extra`` takes."""
    published: list[JsonValue] = list(values)
    return {"enum": published}


def vocabulary(members: type[StrEnum]) -> dict[str, JsonValue]:
    """The published enumeration for a wire field whose type is ``str``.

    I-71 fixes the wire encodings, enumerations included — it spells out
    each route's members inline — and I-76 names the failure-code
    enumeration in the artefact specifically. P-33's strict models
    nonetheless keep these fields ``str``, because Pydantic's
    strict mode refuses raw member values for an enum-typed field, so the
    coercion lives adapter-side in ``routes.py``. Sourcing the published
    enumeration from the vocabulary itself is what stops the contract and
    the code it describes from drifting (Operator, 7 August 2026).
    """
    return _enumeration(member.value for member in members)


def vocabulary_items(members: type[StrEnum]) -> dict[str, JsonValue]:
    """``vocabulary`` for a ``list[str]`` field: the enumeration belongs to
    the items, and ``json_schema_extra`` replaces rather than merges, so
    the item type is restated here."""
    items: JsonValue = {"type": "string", **vocabulary(members)}
    return {"items": items}


def encode_uuid(value: UUID) -> str:
    return str(value)


def encode_timestamp(value: datetime) -> str:
    return canonical_timestamp(value)


def encode_digest(value: bytes) -> str:
    return value.hex()


class WireModel(BaseModel):
    """Base for every ``/v1`` wire model: the P-33 discipline in one place."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WireOutcome(StrEnum):
    COMMITTED = "committed"
    REPLAYED = "replayed"


class MutationReceiptBody(WireModel):
    mutation_id: str
    command_digest: str


class AuditReceiptBody(WireModel):
    event_id: str
    chain_kind: str = Field(json_schema_extra=vocabulary(ChainKind))
    chain_identity: str
    sequence: int
    recorded_at: str
    event_hash: str


class SuccessEnvelope[ResultT: WireModel](WireModel):
    """I-72's closed mutation success envelope.

    Replay is a first-class wire outcome: a replay returns the same HTTP
    status as the original commit and ``outcome`` is the only distinction.
    """

    outcome: WireOutcome
    result: ResultT
    mutation_receipt: MutationReceiptBody
    audit_receipt: AuditReceiptBody


class InvalidRequestDetail(WireModel):
    """The ``detail`` form I-72 licenses on an adapter-raised
    ``invalid_request`` — the rejected field path and the wire rule."""

    field_path: str
    rule: str = Field(json_schema_extra=_enumeration(sorted(WIRE_RULES)))


class SecretRejectedDetail(WireModel):
    """The ``detail`` form I-72 licenses on ``secret_rejected`` — policy
    version, rule identity and field path, and nothing else.

    Neither ``policy`` nor ``rule`` publishes an enumeration, unlike the
    wire rules above: I-31 versions the secret policy apart from the API,
    so pinning its rule vocabulary into the ``/v1`` contract would make
    every policy release a contract change I-29 does not license.
    """

    policy: str
    rule: str
    field_path: str


class FailureBody(WireModel):
    """The I-26 stable failure on the wire.

    Field order matches the foundation middleware's ``internal_error``
    body, so a ``/v1`` failure and the middleware's catch-all are the same
    shape; ``detail`` is absent unless one of the two licensed forms rides
    along.
    """

    code: str = Field(json_schema_extra=vocabulary(FailureCode))
    message: str
    retry: str = Field(json_schema_extra=vocabulary(RetryClass))
    correlation_id: str
    detail: InvalidRequestDetail | SecretRejectedDetail | None = None


class FailureEnvelope(WireModel):
    failure: FailureBody


def invalid_request_envelope(
    *,
    field_path: str,
    rule: str,
    correlation_id: UUID,
) -> FailureEnvelope:
    """The I-72 body an I-71 admission refusal carries, on either transport.

    I-86 requires that "a scenario asserting a rule identity asserts the
    same string on both transports". One constructor is how that becomes a
    property of the code rather than of two renderings agreeing by
    inspection. What differs is only the wrapper: REST returns this body
    under the refined I-73 status (413/415), MCP puts it in a JSON-RPC
    error object's ``data`` under I-88, where those statuses have no
    counterpart.
    """
    return FailureEnvelope(
        failure=FailureBody(
            code=FailureCode.INVALID_REQUEST.value,
            message=INVALID_REQUEST_MESSAGE,
            retry=RetryClass.NEVER.value,
            correlation_id=str(correlation_id),
            detail=InvalidRequestDetail(field_path=field_path, rule=rule),
        )
    )


def failure_envelope(failure: StableFailure) -> FailureEnvelope:
    """The I-72 body a ``StableFailure`` carries, on either transport.

    ``detail`` is rendered only for the two codes I-72 licenses — the
    secret form for ``secret_rejected``, the field-path form for
    ``invalid_request`` — so a failure constructed with a disclosure the
    contract does not permit loses it here rather than leaking it.

    Shared for ``invalid_request_envelope``'s reason, and the licensing
    rule above is the sharp end of it: a second copy is a second place
    for a ``secret_rejected`` detail to survive onto a code that must not
    carry one. What differs per transport is only the wrapper — REST puts
    this under the I-73 status with that table's headers, MCP returns it
    as the text block of an ``isError`` tool result (I-88).
    """
    detail: InvalidRequestDetail | SecretRejectedDetail | None = None
    if failure.detail is not None:
        if failure.code is FailureCode.SECRET_REJECTED:
            detail = SecretRejectedDetail(
                policy=failure.detail.policy,
                rule=failure.detail.rule,
                field_path=failure.detail.field_path,
            )
        elif failure.code is FailureCode.INVALID_REQUEST:
            detail = InvalidRequestDetail(
                field_path=failure.detail.field_path,
                rule=failure.detail.rule,
            )
    return FailureEnvelope(
        failure=FailureBody(
            code=failure.code.value,
            message=failure.safe_message,
            retry=failure.retry.value,
            correlation_id=str(failure.correlation_id),
            detail=detail,
        )
    )
