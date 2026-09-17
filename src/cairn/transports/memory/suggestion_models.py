"""Closed read-only suggestion wire values; authority owns all evidence."""

from typing import Literal, Self

from pydantic import Field, model_validator

from cairn.transports.memory.models import (
    CorrectionBody,
    DisagreementBody,
    MemoryFactBody,
)
from cairn.transports.v1.requests import ScopeBody
from cairn.transports.v1.wire import WireModel


class SuggestRequest(WireModel):
    scope: ScopeBody
    expected_instance_id: str
    observation: str | None = Field(default=None, max_length=4096)
    fact_ids: list[str] = Field(default_factory=list, max_length=8)
    budget: int = Field(default=16384, ge=1, le=65536)
    limit: int = Field(default=8, ge=1, le=16)

    @model_validator(mode="after")
    def input_mode(self) -> Self:
        if self.observation is None:
            if not self.fact_ids:
                raise ValueError("invalid_input_mode")
        elif self.fact_ids or not 1 <= len(self.observation.encode("utf-8")) <= 4096:
            raise ValueError("invalid_input_mode")
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("duplicate_fact_ids")
        return self


class SuggestionBody(WireModel):
    kind: Literal[
        "exact_duplicate",
        "possible_duplicate",
        "possible_correction",
        "related_disagreement",
    ]
    facts: list[MemoryFactBody]
    reason: str
    match_basis: Literal[
        "exact_body",
        "retrieval_candidate",
        "recorded_correction",
        "recorded_disagreement",
    ]
    corrections: list[CorrectionBody]
    disagreements: list[DisagreementBody]


class SuggestionResultBody(WireModel):
    items: list[SuggestionBody]
    budget_consumed: int
    budget_exhausted: bool
    semantic_degraded: bool
    policy: str | None
