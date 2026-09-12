"""Per-route ``/v1`` response wire models (I-72, P-33).

Result shapes mirror the application receipt values and are content-free.
Nullable fields serialise as explicit ``null`` — the success envelope is
dumped without ``exclude_none``, matching I-54's every-key-present
discipline for the audit document.

Task 6 seeds ``IngestResult``; Tasks 7-9 add their route groups' models.
Task 10 attaches the published enumerations and the audit-event reference,
so the artefact it generates describes these shapes fully.
"""

from enum import StrEnum
from typing import Any

from pydantic import Field, JsonValue
from pydantic.json_schema import SkipJsonSchema

from cairn.authority.credentials import PrincipalKind
from cairn.catalogue.audit import (
    AUDIT_SCHEMA,
    ActionKind,
    ChainKind,
    Classification,
    Outcome,
    TrustClass,
)
from cairn.transports.v1.requests import ScopeBody
from cairn.transports.v1.wire import WireModel, vocabulary

# The audit document is referenced in JSON Schema's own ``$defs``
# namespace rather than OpenAPI's ``components/schemas``: this model is
# shared by both transports, and one of them has no components section.
# The REST generator translates the reference into its document's layout,
# which is the generator's job rather than the model's.
AUDIT_EVENT_SCHEMA_NAME = "AuditEvent"
AUDIT_EVENT_REF = f"#/$defs/{AUDIT_EVENT_SCHEMA_NAME}"

_AUDIT_EVENT_ITEMS: dict[str, JsonValue] = {"items": {"$ref": AUDIT_EVENT_REF}}


def _drop_default(schema: dict[str, Any]) -> None:
    """Removes the ``default: null`` Pydantic writes for a defaulted
    field. Used with ``SkipJsonSchema[None]``, which strips the null
    *type* but not the null default — and a string-typed property whose
    default is null is a schema at war with itself."""
    schema.pop("default", None)


_TIMESTAMP: dict[str, object] = {
    "type": "string",
    "format": "date-time",
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
}
_DIGEST: dict[str, object] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_UUID: dict[str, object] = {"type": "string", "format": "uuid"}


def _nullable(schema: dict[str, object]) -> dict[str, object]:
    """The 2020-12 nullable form — a type union, not the 3.0 ``nullable``
    keyword OpenAPI 3.1 dropped. One form for scalars and objects alike, so
    a reader never has to ask which of two spellings a null wears."""
    return {**schema, "type": [schema["type"], "null"]}


def _uuid_array() -> dict[str, object]:
    return {"type": "array", "items": dict(_UUID)}


def _transition(members: type[StrEnum]) -> dict[str, object]:
    values = [member.value for member in members]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "from": {"type": ["string", "null"], "enum": [*values, None]},
            "to": {"type": "string", "enum": values},
        },
        "required": ["from", "to"],
    }


# The canonical ``cairn.audit/v1`` document of I-42 and I-54, authored by
# hand because it is not a Pydantic model anywhere: both adapters pass the
# authoritative bytes through verbatim rather than re-modelling them, which
# is exactly the drift I-72 wanted avoided. Fidelity is held by a test that
# compares this property set against a real event's canonical bytes, not by
# this literal being read carefully. Unlike the wire models, the shape is
# stated to the character — the canonical form is normative here.
#
# It lives beside the model that references it, and therefore in the shared
# package, because both transports publish it: the OpenAPI artefact as a
# component and the I-89 MCP manifest as a tool's output ``$defs`` entry.
# A copy per transport is precisely the second transcription I-72 forbids.
_AUDIT_SCOPE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "realm": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                },
                "required": ["id", "kind"],
            },
        },
    },
    "required": ["realm", "segments"],
}

_AUDIT_EVENT_PROPERTIES: dict[str, object] = {
    "action_code": {"type": "string"},
    "action_kind": {"type": "string", **vocabulary(ActionKind)},
    "affected_assertion_ids": _uuid_array(),
    "affected_evidence_ids": _uuid_array(),
    "affected_fact_ids": _uuid_array(),
    "affected_grant_ids": _uuid_array(),
    "chain_identity": {"type": "string"},
    "chain_kind": {"type": "string", **vocabulary(ChainKind)},
    "classification_transition": _nullable(_transition(Classification)),
    "command_digest": _nullable(_DIGEST),
    "correlation_id": dict(_UUID),
    "credential_verifier_id": _nullable(_UUID),
    "event_id": dict(_UUID),
    "evidence_digest": _nullable(_DIGEST),
    "evidence_reference": _nullable(_UUID),
    "grant_id": _nullable(_UUID),
    "idempotency_key": _nullable(_UUID),
    "mutation_id": _nullable(_UUID),
    "outcome": {"type": "string", **vocabulary(Outcome)},
    "previous_hash": dict(_DIGEST),
    "principal_id": _nullable(_UUID),
    "reason_code": {"type": "string"},
    "recorded_at": dict(_TIMESTAMP),
    "replay_of_mutation_id": _nullable(_UUID),
    "requested_scope": _nullable(_AUDIT_SCOPE),
    "safe_request_fingerprint": _nullable(_DIGEST),
    "schema": {"type": "string", "const": AUDIT_SCHEMA},
    "sequence": {"type": "integer", "minimum": 1},
    "source_scope": _nullable(_AUDIT_SCOPE),
    "target_scope": _nullable(_AUDIT_SCOPE),
    "trust_transition": _nullable(_transition(TrustClass)),
}

AUDIT_EVENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "The canonical audit event. Every key is always present; a value "
        "that does not apply is null. These are the exact bytes the "
        "hash chain covers."
    ),
    "properties": _AUDIT_EVENT_PROPERTIES,
    # I-54's every-key-present guarantee, stated the way JSON Schema
    # states it. Without this the object is all-optional and the empty
    # document validates, which is not what the description above says
    # and not what any reader of the chain may assume.
    "required": sorted(_AUDIT_EVENT_PROPERTIES),
}


class IngestResult(WireModel):
    assertion_id: str
    fact_ids: list[str]
    evidence_id: str | None


class PromotionPairBody(WireModel):
    source_fact_id: str
    derived_fact_id: str


class PromoteResult(WireModel):
    # Pairs in command order per I-72 — deliberately not the sorted order
    # the audit event's affected_fact_ids uses.
    promotions: list[PromotionPairBody]
    evidence_id: str


class InvalidateResult(WireModel):
    fact_ids: list[str]
    invalidated_at: str


class CreatePrincipalResult(WireModel):
    principal_id: str
    kind: str = Field(json_schema_extra=vocabulary(PrincipalKind))
    label: str
    created_at: str


class IssueCredentialResult(WireModel):
    # I-72: plaintext is the token string exactly when the outcome is
    # committed and null exactly when it is replayed — the wire form of
    # I-60's plaintext-once discipline and amended I-24's single emission.
    credential_id: str
    principal_id: str
    expires_at: str | None
    created_at: str
    plaintext: str | None


class RevokeCredentialResult(WireModel):
    credential_id: str
    revoked_at: str


class CreateGrantResult(WireModel):
    grant_id: str
    created_at: str


class RevokeGrantResult(WireModel):
    grant_id: str
    revoked_at: str


class ReadAuditEventsResult(WireModel):
    # I-72: each event is the parsed canonical ``cairn.audit/v1`` document
    # verbatim, so no REST re-modelling exists to drift from the
    # authoritative bytes. The items reference the audit-event schema
    # ``openapi.py`` publishes, whose fidelity a test pins against real
    # canonical bytes.
    events: list[dict[str, object]] = Field(json_schema_extra=_AUDIT_EVENT_ITEMS)
    next_after_sequence: int | None


class RetrievedFactBody(WireModel):
    """I-77: exactly the I-67 stored fields, nothing invented for the wire.

    The provenance columns appear in their two lawful forms — ``assertion_id``
    for an ingested fact, or ``derived_from``/``promoted_by``/``evidence_id``
    for a promoted one — so the shape mirrors the catalogue's own
    ``ck_facts_provenance_form`` rather than flattening the distinction.
    ``invalidated_at`` carries an invalidation only where this ``as_of``
    can see it (P-42), which in v0.1 means it is always null: I-79 withholds
    the fact entirely once its invalidation is visible, so any hit's
    invalidation necessarily lies in the caller's future. The field remains
    part of the closed I-77 hit shape regardless.
    """

    fact_id: str
    body: str
    scope: ScopeBody
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    trust: str = Field(json_schema_extra=vocabulary(TrustClass))
    assertion_id: str | None = None
    derived_from: str | None = None
    promoted_by: str | None = None
    evidence_id: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str
    invalidated_at: str | None = None


class RetrieveResult(WireModel):
    """I-77's bare result: a read returns no mutation envelope, as on the
    other two read routes. ``budget_exhausted`` distinguishes "that is
    everything" from "there was more" (I-82)."""

    hits: list[RetrievedFactBody]
    budget_consumed: int
    budget_exhausted: bool


class InstanceResult(WireModel):
    # I-72 marks nullable what is nullable and marks nothing here: the
    # digest is always present, because an instance whose packaged
    # contract cannot be read refuses to start rather than serving a null
    # (Operator, 7 August 2026). Task 9's ``str | None`` was the placeholder
    # for the artefact this task generates.
    instance_id: str
    product_version: str
    contract_identity: str
    contract_digest: str
    # I-89's added field, and the exception to the paragraph above. It is
    # optional in the *schema* because I-29 only licenses an added
    # response field that existing clients may not know about — a
    # required one would be a breaking change to the same document — and
    # never absent in practice, for the identical reason ``contract_digest``
    # is never null: an instance that cannot read its packaged manifest
    # refuses to start. ``contract_digest`` keeps its I-76 meaning, the
    # OpenAPI artefact alone, because redefining it as a digest of the
    # pair would silently break every consumer that pinned it.
    #
    # Optional is not nullable, and the published schema must say only
    # the former (Val's gate review of ``5b6a5ec``, finding 5): I-89
    # licenses a field carrying a SHA-256 digest, so the null the Python
    # default needs is kept out of the schema by ``SkipJsonSchema`` and
    # the contradictory ``default: null`` a string-typed property cannot
    # honour is dropped with it.
    mcp_contract_digest: str | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=_drop_default
    )
