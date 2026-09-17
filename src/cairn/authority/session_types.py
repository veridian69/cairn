"""Immutable, transport-independent session commands and owner-private values."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from cairn.authority.mutations import AssertionIngested
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.transactions import AuditReceipt, MutationReceipt


@dataclass(frozen=True, slots=True)
class DurableObservation:
    body: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OpenSession:
    scope: Scope
    session_id: UUID
    classification: Classification


@dataclass(frozen=True, slots=True)
class BeginTurn:
    scope: Scope
    session_id: UUID
    turn_id: UUID
    attempt_id: UUID
    replaces_turn_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PrepareTurn:
    scope: Scope
    session_id: UUID
    turn_id: UUID
    attempt_id: UUID
    response: str
    observations: tuple[DurableObservation, ...]


@dataclass(frozen=True, slots=True)
class AbandonTurn:
    scope: Scope
    session_id: UUID
    turn_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class CommitTurn:
    scope: Scope
    session_id: UUID
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class ReadSession:
    scope: Scope
    session_id: UUID
    turn_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class IssueVisit:
    scope: Scope
    session_id: UUID


@dataclass(frozen=True, slots=True)
class AcknowledgeVisit:
    scope: Scope
    session_id: UUID
    visit_id: UUID


type SessionMutation = (
    OpenSession | BeginTurn | PrepareTurn | AbandonTurn | IssueVisit | AcknowledgeVisit
)
type SessionCommand = SessionMutation | ReadSession | CommitTurn


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: UUID
    instance_id: UUID
    principal_id: UUID
    scope: Scope
    classification: Classification
    state: Literal["open", "started", "prepared", "abandoned", "committed", "skipped"]
    operational_receipt: MutationReceipt
    # Operational acknowledgement and actual normal-ingest custody are distinct.
    custody_receipt: MutationReceipt | None = None
    turn_id: UUID | None = None
    attempt_id: UUID | None = None
    replaces_turn_id: UUID | None = None
    response: str | None = None
    observations: tuple[DurableObservation, ...] = ()
    abandonment_reason: str | None = None
    acknowledged_watermark: int = 0
    acknowledged_at: datetime | None = None
    turn_count: int = 0
    prepared_bytes: int = 0
    visit_id: UUID | None = None
    visit_watermark: int | None = None
    visit_at: datetime | None = None
    custody_result: AssertionIngested | None = None
    custody_audit_receipt: AuditReceipt | None = None
    custody_idempotency_key: UUID | None = None
