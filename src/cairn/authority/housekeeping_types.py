"""Immutable suggestions: attributed evidence, never mutation instructions."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from cairn.authority.memory_types import (
    MemoryCorrection,
    MemoryDisagreement,
    MemoryFact,
)
from cairn.catalogue.audit import Scope


@dataclass(frozen=True, slots=True)
class Suggest:
    scope: Scope
    observation: str | None = None
    fact_ids: tuple[UUID, ...] = ()
    budget: int = 16384
    limit: int = 8


@dataclass(frozen=True, slots=True)
class Suggestion:
    kind: Literal[
        "exact_duplicate",
        "possible_duplicate",
        "possible_correction",
        "related_disagreement",
    ]
    facts: tuple[MemoryFact, ...]
    reason: str
    match_basis: Literal[
        "exact_body",
        "retrieval_candidate",
        "recorded_correction",
        "recorded_disagreement",
    ]
    corrections: tuple[MemoryCorrection, ...] = ()
    disagreements: tuple[MemoryDisagreement, ...] = ()


@dataclass(frozen=True, slots=True)
class SuggestionResult:
    items: tuple[Suggestion, ...]
    budget_consumed: int
    budget_exhausted: bool
    semantic_degraded: bool
    policy: str | None
