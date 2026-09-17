"""Strict session-only wire values; these never enter the frozen v1 inventory."""

from typing import Literal

from pydantic import Field

from cairn.transports.v1.requests import ScopeBody
from cairn.transports.v1.responses import IngestResult
from cairn.transports.v1.wire import AuditReceiptBody, MutationReceiptBody, WireModel


class SessionRequest(WireModel):
    scope: ScopeBody
    session_id: str
    expected_instance_id: str


class OpenSessionRequest(SessionRequest):
    classification: Literal["public", "internal", "restricted"]


class TurnRequest(SessionRequest):
    turn_id: str


class BeginTurnRequest(TurnRequest):
    attempt_id: str
    replaces_turn_id: str | None = None


class SessionObservationBody(WireModel):
    body: str = Field(max_length=4096)
    valid_from: str | None = None
    valid_to: str | None = None
    observed_at: str | None = None


class PrepareTurnRequest(TurnRequest):
    attempt_id: str
    response: str = Field(max_length=32768)
    observations: list[SessionObservationBody] = Field(max_length=8)


class AbandonTurnRequest(TurnRequest):
    reason: str


class ReadSessionRequest(SessionRequest):
    turn_id: str | None = None


class AcknowledgeVisitRequest(SessionRequest):
    visit_id: str


class SessionSnapshotBody(WireModel):
    session_id: str
    instance_id: str
    principal_id: str
    scope: ScopeBody
    classification: Literal["public", "internal", "restricted"]
    state: Literal["open", "started", "prepared", "abandoned", "committed", "skipped"]
    operational_receipt: MutationReceiptBody
    custody_receipt: MutationReceiptBody | None
    turn_id: str | None
    attempt_id: str | None
    replaces_turn_id: str | None
    response: str | None
    observations: list[SessionObservationBody] = Field(max_length=8)
    abandonment_reason: str | None
    acknowledged_watermark: int = Field(ge=0, le=2**63 - 1)
    acknowledged_at: str | None
    turn_count: int = Field(ge=0, le=2**63 - 1)
    prepared_bytes: int = Field(ge=0, le=2**63 - 1)
    visit_id: str | None
    visit_watermark: int | None
    visit_at: str | None
    custody_result: IngestResult | None
    custody_audit_receipt: AuditReceiptBody | None
    custody_idempotency_key: str | None
