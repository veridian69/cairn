"""Strict memory requests and fully described, attributed result packets."""

from typing import Literal

from pydantic import Field

from cairn.authority.custody import SourceType
from cairn.authority.retrieval import MAX_BUDGET_BYTES
from cairn.catalogue.audit import Classification, TrustClass
from cairn.transports.v1.requests import FactBody, InvalidateRequest, ScopeBody
from cairn.transports.v1.responses import RetrievedFactBody
from cairn.transports.v1.wire import WireModel, vocabulary, vocabulary_items


class DiagnoseRequest(WireModel):
    scope: ScopeBody
    classification: Literal["public", "internal", "restricted"]


class CorrectRequest(InvalidateRequest):
    scope: ScopeBody | None = None
    superseded_by: str | None = Field(
        default=None,
        description=(
            "Verified replacement fact ID, already committed and read back. "
            "Required by the replacement workflow; absent/null means deliberate "
            "withdrawal without a link. Preserve unchanged parts of compound "
            "facts before withdrawing them."
        ),
    )


class PermissionsBody(WireModel):
    retrieve: bool
    ingest: bool
    promote: bool
    invalidate: bool


class DiagnoseBody(WireModel):
    instance_id: str
    product_version: str = Field(
        min_length=1, max_length=64, pattern=r"^[0-9][a-zA-Z0-9.+-]*$"
    )
    contract_identity: Literal["cairn.memory/v1"] = "cairn.memory/v1"
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mcp_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str
    principal_kind: Literal["human", "workload"]
    scope: ScopeBody
    classification: Literal["public", "internal", "restricted"]
    permissions: PermissionsBody
    evaluated_at: str
    permission_basis: Literal["current_grants_only"] = Field(
        default="current_grants_only",
        description="Exact-scope grant snapshot at evaluated_at, not readiness or a promise of operation success. Ingest/promote include target write classification; retrieve includes memory read clearance; invalidate is classification independent. Source, evidence, content screening and fresh authorisation checks still apply.",
    )


class RememberRequest(WireModel):
    scope: ScopeBody
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    facts: list[FactBody] = Field(
        description=(
            "Independently changeable candidate statements. Separate capacity "
            "from schedule, location, proposals and unfinished work. Prefer "
            "one fact per call; batch result IDs do not correspond to input order."
        )
    )
    observed_at: str | None = None
    metadata: dict[str, object] | None = None
    evidence_payload: str | None = Field(
        default=None,
        description=(
            "Supply the relevant bounded conversation/source excerpt for a "
            "conversational save. It is stored as screened supporting evidence "
            "in Attic when enabled, not as validated truth. Do not fabricate an "
            "excerpt. If absent, fact custody can still succeed but this call "
            "supplies no Attic text; there is no automatic transcript capture."
        ),
    )


class RecallRequest(WireModel):
    scope: ScopeBody
    query: str
    relevant_only: bool = Field(
        default=False,
        description="Require a lexical or semantic relevance signal; recency alone does not select a memory.",
    )
    budget: int = Field(
        default=16384,
        ge=1,
        le=MAX_BUDGET_BYTES,
        description=(
            f"UTF-8 byte budget (1–{MAX_BUDGET_BYTES}) for the sum of canonical JSON disclosed "
            "records, including attribution, reasons and relationships. Envelope "
            "punctuation is excluded. Facts are admitted before bounded relationship "
            "context; per-fact flags identify visible disagreement and omitted context."
        ),
    )
    trust_filters: list[str] = Field(
        default_factory=list, json_schema_extra=vocabulary_items(TrustClass)
    )


class HistoryRequest(WireModel):
    scope: ScopeBody
    fact_id: str
    budget: int = Field(
        default=16384,
        ge=1,
        le=MAX_BUDGET_BYTES,
        description=(
            f"UTF-8 byte budget (1–{MAX_BUDGET_BYTES}) for the sum of canonical JSON disclosed "
            "records, including attribution, reasons and relationships. Envelope "
            "punctuation is excluded. Facts are admitted before bounded relationship "
            "context; per-fact flags identify visible disagreement and omitted context."
        ),
    )


class DisagreeRequest(WireModel):
    scope: ScopeBody
    left_fact_id: str
    right_fact_id: str
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    reason: str


class ResolveRequest(WireModel):
    scope: ScopeBody
    disagreement_id: str
    evidence_id: str
    selected_fact_id: str | None
    reason: str


class MemoryFactBody(RetrievedFactBody):
    source_principal_id: str | None
    source_type: str | None = Field(
        json_schema_extra={"enum": [*[member.value for member in SourceType], None]}
    )
    relevance_score: float
    has_disagreement: bool
    disagreement_context_incomplete: bool


class CorrectionBody(WireModel):
    fact_id: str
    superseded_by: str | None
    principal_id: str
    reason: str
    invalidated_at: str


class DisagreementBody(WireModel):
    relationship_id: str
    scope: ScopeBody
    left_fact_id: str
    right_fact_id: str
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    principal_id: str
    reason: str
    recorded_at: str


class ResolutionBody(WireModel):
    relationship_id: str
    scope: ScopeBody
    disagreement_id: str
    evidence_id: str
    selected_fact_id: str | None
    classification: str = Field(json_schema_extra=vocabulary(Classification))
    principal_id: str
    reason: str
    recorded_at: str


class RelationshipResult(WireModel):
    relationship_id: str


class RecallBody(WireModel):
    hits: list[MemoryFactBody]
    disagreements: list[DisagreementBody]
    resolutions: list[ResolutionBody]
    budget_consumed: int
    budget_exhausted: bool
    policy: str
    semantic_degraded: bool = False


class HistoryBody(WireModel):
    facts: list[MemoryFactBody]
    corrections: list[CorrectionBody]
    disagreements: list[DisagreementBody]
    resolutions: list[ResolutionBody]
    budget_consumed: int
    budget_exhausted: bool
