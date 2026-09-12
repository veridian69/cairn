"""Closed commands and disclosed values for the independent memory/v1 surface."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from cairn.authority.custody import FactProvenance, SourceType
from cairn.catalogue.audit import Classification, Scope, TrustClass

_SCORE_POLICY = (
    "lexical-age/v1: distinct token overlap + 0.5 semantic membership "
    "+ 0.25/(1+age_days/30); semantic limit 256"
)
POLICY = _SCORE_POLICY + "; no cutoff or reinforcement"
RELEVANT_POLICY = (
    _SCORE_POLICY + "; relevant_only: score > 0.25; no age cutoff or reinforcement"
)
GRADED_POLICY = "lexical-graded/v2"
GRADED_RELEVANT_POLICY = (
    GRADED_POLICY + "; relevant_only: lexical overlap or semantic membership"
)
SEMANTIC_UNAVAILABLE = "; semantic-unavailable"
MAX_HISTORY_RECORDS = 128


@dataclass(frozen=True, slots=True)
class Recall:
    scope: Scope
    query: str
    budget: int = 16384
    trust_filters: frozenset[TrustClass] = frozenset()
    relevant_only: bool = False


@dataclass(frozen=True, slots=True)
class History:
    scope: Scope
    fact_id: UUID
    budget: int = 16384


@dataclass(frozen=True, slots=True)
class Disagree:
    scope: Scope
    left_fact_id: UUID
    right_fact_id: UUID
    classification: Classification
    reason: str


@dataclass(frozen=True, slots=True)
class Resolve:
    scope: Scope
    disagreement_id: UUID
    evidence_id: UUID
    selected_fact_id: UUID | None
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryFactRecord:
    fact_id: UUID
    body: str
    scope: Scope
    classification: Classification
    trust: TrustClass
    provenance: FactProvenance | None
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    invalidated_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryFact:
    fact: MemoryFactRecord
    source_principal_id: UUID | None
    source_type: SourceType | None
    relevance_score: float
    has_disagreement: bool = False
    disagreement_context_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class MemoryCorrection:
    fact_id: UUID
    superseded_by: UUID | None
    principal_id: UUID
    reason: str
    invalidated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryDisagreement:
    relationship_id: UUID
    scope: Scope
    left_fact_id: UUID
    right_fact_id: UUID
    classification: Classification
    principal_id: UUID
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    relationship_id: UUID
    scope: Scope
    disagreement_id: UUID
    evidence_id: UUID
    selected_fact_id: UUID | None
    classification: Classification
    principal_id: UUID
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RelationshipRecorded:
    relationship_id: UUID


@dataclass(frozen=True, slots=True)
class RecallResult:
    hits: tuple[MemoryFact, ...]
    budget_consumed: int
    budget_exhausted: bool
    policy: str = POLICY
    disagreements: tuple[MemoryDisagreement, ...] = ()
    resolutions: tuple[MemoryResolution, ...] = ()
    semantic_degraded: bool = False


@dataclass(frozen=True, slots=True)
class MemoryHistory:
    facts: tuple[MemoryFact, ...]
    corrections: tuple[MemoryCorrection, ...]
    disagreements: tuple[MemoryDisagreement, ...]
    resolutions: tuple[MemoryResolution, ...]
    budget_consumed: int
    budget_exhausted: bool
