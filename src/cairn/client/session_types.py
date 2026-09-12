"""Immutable validated public session values and truthful recovery failures."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope
from cairn.client.briefing import ArrivalBriefing
from cairn.client.errors import FailureMetadata, MemoryOperationFailure
from cairn.client.types import (
    DurableObservation,
    FrozenJSONObject,
    ModelTurn,
    PersistenceReceipt,
)

type DurableStage = Literal[
    "unconfirmed", "open", "started", "prepared", "committed", "skipped", "abandoned"
]


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: UUID
    instance_id: UUID
    principal_id: UUID
    scope: Scope
    classification: Classification
    state: Literal["open", "started", "prepared", "abandoned", "committed", "skipped"]
    operational_receipt: FrozenJSONObject
    custody_receipt: FrozenJSONObject | None
    turn_id: UUID | None
    attempt_id: UUID | None
    replaces_turn_id: UUID | None
    response: str | None
    observations: tuple[DurableObservation, ...]
    abandonment_reason: str | None
    acknowledged_watermark: int
    acknowledged_at: datetime | None
    turn_count: int
    prepared_bytes: int
    visit_id: UUID | None
    visit_watermark: int | None
    visit_at: datetime | None
    custody_result: FrozenJSONObject | None
    custody_audit_receipt: FrozenJSONObject | None
    custody_idempotency_key: UUID | None


@dataclass(frozen=True, slots=True)
class SessionOperationResult:
    outcome: Literal["committed", "replayed"]
    snapshot: SessionSnapshot
    mutation_receipt: FrozenJSONObject
    audit_receipt: FrozenJSONObject


@dataclass(frozen=True, slots=True)
class DurableTurnResult:
    session_id: UUID
    turn_id: UUID
    attempt_id: UUID
    state: Literal["interrupted", "prepared", "committed", "skipped", "abandoned"]
    completed_turn: ModelTurn | None
    persistence: PersistenceReceipt | None
    snapshot: SessionSnapshot


@dataclass(frozen=True, slots=True)
class DurableArrival:
    briefing: ArrivalBriefing
    visit: SessionOperationResult
    clock_rollback: bool


@dataclass(frozen=True, slots=True)
class DurableProgress:
    phase: Literal[
        "processing",
        "pending",
        "failed",
        "interrupted",
        "prepared",
        "committed",
        "skipped",
        "abandoned",
    ]
    failure_code: str | None = None
    searchability: Literal["unconfirmed", "not-applicable"] = "unconfirmed"
    operation: str | None = None
    last_confirmed_stage: DurableStage = "unconfirmed"


class DurableSessionFailure(MemoryOperationFailure):
    """Unconfirmed state is pending, never a fabricated rejection or receipt."""

    def __init__(
        self,
        operation: str,
        failure: FailureMetadata,
        *,
        session_id: UUID,
        turn_id: UUID,
        state: Literal["pending", "failed"],
        completed_turn: ModelTurn | None = None,
        last_confirmed_stage: DurableStage = "unconfirmed",
    ) -> None:
        super().__init__(operation, failure)
        self.session_id = session_id
        self.turn_id = turn_id
        self.state = state
        self.completed_turn = completed_turn
        self.last_confirmed_stage = last_confirmed_stage
