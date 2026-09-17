"""Per-route ``/v1`` request wire models (I-71, P-33).

One strict model per route, mirroring the frozen application command field
for field. Enumeration-valued fields are ``str`` on the model and coerced
through the closed vocabularies in ``routes.py``: Pydantic's strict mode
refuses raw member values for enum-typed fields, and the adapter-side
coercion mirrors the ``audit.py`` convention of a guarded lookup per
closed vocabulary rather than a bare constructor call.

Task 6 seeds ``IngestRequest`` alongside the router; Tasks 7-9 add their
route groups' models here. Task 10 attaches each closed vocabulary's
published enumeration to the field that carries it, sourced from the
vocabulary itself so the artefact cannot drift from it.
"""

from pydantic import Field

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import SourceType
from cairn.catalogue.audit import Classification, TrustClass
from cairn.transports.v1.wire import WireModel, vocabulary, vocabulary_items


class SegmentBody(WireModel):
    kind: str
    identifier: str


class ScopeBody(WireModel):
    realm: str
    segments: list[SegmentBody]


class FactBody(WireModel):
    body: str
    valid_from: str | None = None
    valid_to: str | None = None


class IngestRequest(WireModel):
    scope: ScopeBody
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    source_type: str = Field(json_schema_extra=vocabulary(SourceType))
    facts: list[FactBody]
    requested_trust: str = Field(
        default="candidate", json_schema_extra=vocabulary(TrustClass)
    )
    observed_at: str | None = None
    # I-71: metadata travels as an inline strict JSON object; the adapter
    # canonicalises it to the I-30 canonical string before command
    # construction. evidence_payload is a JSON string carrying the exact
    # UTF-8 text — I-30 admits no binary, so there is no base64 envelope.
    metadata: dict[str, object] | None = None
    evidence_payload: str | None = None


class EvidenceIdBody(WireModel):
    evidence_id: str


class ExternalEvidenceBody(WireModel):
    external_uri: str
    payload_digest: str


class PromoteRequest(WireModel):
    fact_ids: list[str]
    # I-71: exactly one of the two evidence forms; strictness makes the
    # union total — a body carrying fields of both shapes matches neither.
    evidence: EvidenceIdBody | ExternalEvidenceBody
    target_scope: ScopeBody | None = None
    target_classification: str | None = Field(
        default=None, json_schema_extra=vocabulary(Classification)
    )
    reason: str


class InvalidateRequest(WireModel):
    fact_ids: list[str]
    reason: str
    superseded_by: str | None = None


class CreatePrincipalRequest(WireModel):
    realm_id: str
    kind: str = Field(json_schema_extra=vocabulary(PrincipalKind))
    label: str


class IssueCredentialRequest(WireModel):
    realm_id: str
    principal_id: str
    expires_at: str | None = None


class RevokeCredentialRequest(WireModel):
    realm_id: str
    credential_id: str
    reason_code: str


class GrantBody(WireModel):
    # I-71: delegable_operations is deliberately absent — I-61 rejects
    # every proposal carrying a value for it, so the wire cannot express
    # one.
    principal_id: str
    realm_id: str
    segments: list[SegmentBody]
    operations: list[str] = Field(json_schema_extra=vocabulary_items(GrantOperation))
    read_clearance: str = Field(json_schema_extra=vocabulary(Classification))
    write_classifications: list[str] = Field(
        json_schema_extra=vocabulary_items(Classification)
    )
    expires_at: str | None = None


class CreateGrantRequest(WireModel):
    realm_id: str
    grant: GrantBody


class RevokeGrantRequest(WireModel):
    realm_id: str
    grant_id: str
    reason_code: str


class ReadEvidenceRequest(WireModel):
    scope: ScopeBody
    evidence_id: str


class RetrieveRequest(WireModel):
    """I-71/P-44. ``trust_filters`` empty or absent means the I-80
    ``validated`` default; duplicates are rejected by strict parsing rather
    than silently collapsed. No classification ceiling field exists — it is
    derived from the caller's grants server-side and a caller may not ask
    for one it does not hold."""

    scope: ScopeBody
    query: str = Field(
        description="Opaque UTF-8, 1 byte to 8 KiB after encoding. Cairn "
        "owns no query syntax and passes this to the index unparsed."
    )
    budget: int = Field(
        description="Disclosure budget in bytes of fact body, 1 to 1048576. "
        "Whole facts are appended until the next would not fit."
    )
    trust_filters: list[str] = Field(
        default_factory=list, json_schema_extra=vocabulary_items(TrustClass)
    )
    as_of: str | None = None


class ReadAuditEventsRequest(WireModel):
    realm_id: str
    scope_prefix: list[SegmentBody]
    after_sequence: int = 0
    limit: int = Field(
        default=100,
        description=(
            "Between 1 and 500. A limit of 1 cannot make pagination "
            "progress: a successful read appends its own audit event to "
            "the chain it reads, so next_after_sequence never returns "
            "null at that limit."
        ),
    )
