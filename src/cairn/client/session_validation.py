"""Validate complete session snapshots before exposing recovery or custody."""

import hashlib
import json
import re
from datetime import datetime
from typing import TypedDict, Unpack, cast
from uuid import RFC_4122, UUID

from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.mutations import IngestAssertion, _ingest_digest
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import parse_timestamp
from cairn.client.errors import FailureMetadata, MemoryOperationFailure
from cairn.client.session_types import SessionOperationResult, SessionSnapshot
from cairn.client.types import DurableObservation, FrozenJSONObject, freeze_object
from cairn.client.validation import (
    _object,
    _timestamp,
    _uuid,
    _validate_receipts,
    validate_remember_success,
)
from cairn.session_identity import remember_key
from cairn.transports.memory.session_models import SessionSnapshotBody

# Canonical preparation is at most 73,728 UTF-8 bytes. Six wire bytes per
# canonical byte covers JSON Unicode escaping; 16 KiB covers fixed snapshot,
# scope and receipt metadata. No session inventory is returned. Failures keep
# the existing separate 16 KiB cap. Compression is refused before consumption.
SESSION_WIRE_BYTES = 6 * 73728 + 16384


def require_preparation_binding(
    known: SessionSnapshot, terminal: SessionSnapshot
) -> None:
    """Custody covers observations, not the whole immutable generation claim."""
    fields = (
        "session_id",
        "instance_id",
        "principal_id",
        "scope",
        "classification",
        "turn_id",
        "attempt_id",
        "replaces_turn_id",
        "response",
        "observations",
    )
    if known.state not in {"prepared", "committed", "skipped"} or any(
        getattr(known, field) != getattr(terminal, field) for field in fields
    ):
        raise MemoryOperationFailure(
            "turn-commit",
            FailureMetadata(
                "invalid_response",
                "Cairn returned an invalid response.",
                "never",
                None,
                None,
            ),
        )


class SnapshotContext(TypedDict):
    session_id: UUID
    turn_id: UUID | None
    instance_id: UUID
    principal_id: UUID
    scope: Scope
    scope_json: dict[str, object]
    classification: Classification
    attempt_id: UUID | None


def session_uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("invalid_session_identity")
    identity = UUID(value)
    if str(identity) != value or identity.variant != RFC_4122:
        raise ValueError("invalid_session_identity")
    return identity


def _time(value: object) -> datetime | None:
    text = _timestamp(value, nullable=True)
    return None if text is None else parse_timestamp(text)


def _mutation(value: object) -> FrozenJSONObject:
    receipt = _object(value, frozenset({"mutation_id", "command_digest"}))
    _uuid(receipt["mutation_id"])
    if (
        type(receipt["command_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", receipt["command_digest"]) is None
    ):
        raise ValueError("invalid_receipt")
    return freeze_object(receipt)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_snapshot(
    value: object,
    *,
    session_id: UUID,
    turn_id: UUID | None,
    instance_id: UUID,
    principal_id: UUID,
    scope: Scope,
    scope_json: dict[str, object],
    classification: Classification,
    attempt_id: UUID | None = None,
) -> SessionSnapshot:
    document = _object(value, frozenset(SessionSnapshotBody.model_fields))
    body = SessionSnapshotBody.model_validate(document)
    for observation in cast(list[object], document["observations"]):
        _object(
            observation, frozenset({"body", "valid_from", "valid_to", "observed_at"})
        )
    sid = session_uuid(body.session_id)
    iid = UUID(cast(str, _uuid(body.instance_id)))
    owner = UUID(cast(str, _uuid(body.principal_id)))
    tid = None if body.turn_id is None else session_uuid(body.turn_id)
    attempt = None if body.attempt_id is None else session_uuid(body.attempt_id)
    predecessor = (
        None if body.replaces_turn_id is None else session_uuid(body.replaces_turn_id)
    )
    if (
        sid != session_id
        or iid != instance_id
        or owner != principal_id
        or tid != turn_id
        or body.scope.model_dump() != scope_json
        or body.classification != classification.value
        or (attempt_id is not None and attempt != attempt_id)
    ):
        raise ValueError("session_context_mismatch")
    if body.state == "open":
        if (
            any(
                v is not None
                for v in (
                    tid,
                    attempt,
                    predecessor,
                    body.response,
                    body.abandonment_reason,
                )
            )
            or body.observations
        ):
            raise ValueError("invalid_open_state")
    elif tid is None or attempt is None or body.turn_count < 1 or predecessor == tid:
        raise ValueError("invalid_turn_state")
    prepared = body.state in {"prepared", "committed", "skipped"}
    if prepared != (body.response is not None):
        raise ValueError("invalid_prepared_state")
    if not prepared and body.observations:
        raise ValueError("unexpected_observations")
    if (body.state == "abandoned") != (body.abandonment_reason is not None):
        raise ValueError("invalid_abandonment")
    if body.abandonment_reason is not None:
        from cairn.authority.custody import validate_reason

        validate_reason(body.abandonment_reason)
    observations = tuple(
        DurableObservation(
            o.body, _time(o.valid_from), _time(o.valid_to), _time(o.observed_at)
        )
        for o in body.observations
    )
    for o in observations:
        if (
            not o.body
            or len(o.body.encode("utf-8")) > 4096
            or (
                o.valid_from is not None
                and o.valid_to is not None
                and o.valid_to <= o.valid_from
            )
        ):
            raise ValueError("invalid_observation")
    if observations and any(
        o.observed_at != observations[0].observed_at for o in observations
    ):
        raise ValueError("invalid_observation_times")
    receipt = _mutation(document["operational_receipt"])
    if prepared:
        assert body.response is not None
        if len(body.response.encode("utf-8")) > 32768:
            raise ValueError("response_too_large")
        payload = _canonical(
            {
                "schema": "cairn.session.preparation/v1",
                "instance_id": body.instance_id,
                "principal_id": body.principal_id,
                "classification": body.classification,
                "operation": "session-prepare",
                "command": {
                    "scope": scope_json,
                    "session_id": body.session_id,
                    "turn_id": body.turn_id,
                    "attempt_id": body.attempt_id,
                    "response": body.response,
                    "observations": [o.model_dump() for o in body.observations],
                },
            }
        )
        if len(payload) > 73728 or body.prepared_bytes < len(payload):
            raise ValueError("invalid_prepared_size")
        if (
            body.state == "prepared"
            and receipt["command_digest"] != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("invalid_preparation_digest")
    if body.prepared_bytes > body.turn_count * 73728:
        raise ValueError("invalid_counts")
    ack_at = _time(body.acknowledged_at)
    if (body.acknowledged_watermark == 0) != (ack_at is None):
        raise ValueError("invalid_acknowledgement")
    visit_id = None if body.visit_id is None else UUID(cast(str, _uuid(body.visit_id)))
    visit_at = _time(body.visit_at)
    visit_values = (visit_id, body.visit_watermark, visit_at)
    if any(v is not None for v in visit_values):
        if (
            any(v is None for v in visit_values)
            or type(body.visit_watermark) is not int
            or not 1 <= body.visit_watermark <= 2**63 - 1
            or body.visit_watermark <= body.acknowledged_watermark
            or body.state != "open"
            or (ack_at is not None and visit_at is not None and visit_at < ack_at)
        ):
            raise ValueError("invalid_visit")
    custody_fields = (
        body.custody_receipt,
        body.custody_result,
        body.custody_audit_receipt,
        body.custody_idempotency_key,
    )
    custody: FrozenJSONObject | None = None
    result: FrozenJSONObject | None = None
    audit: FrozenJSONObject | None = None
    key: UUID | None = None
    if body.state == "committed":
        if not observations or any(v is None for v in custody_fields):
            raise ValueError("incomplete_custody")
        validated = validate_remember_success(
            {
                "outcome": "committed",
                "result": document["custody_result"],
                "mutation_receipt": document["custody_receipt"],
                "audit_receipt": document["custody_audit_receipt"],
            },
            fact_count=len(observations),
            realm=scope.realm,
        )
        custody = freeze_object(validated["mutation_receipt"])
        result = freeze_object(validated["result"])
        audit = freeze_object(validated["audit_receipt"])
        key = session_uuid(body.custody_idempotency_key)
        assert tid is not None
        command = IngestAssertion(
            scope,
            classification,
            SourceType.AGENT_CLAIM,
            tuple(FactDraft(o.body, o.valid_from, o.valid_to) for o in observations),
            observed_at=observations[0].observed_at,
        )
        if custody["command_digest"] != _ingest_digest(command).hex():
            raise ValueError("custody_payload_mismatch")
        if (
            key != remember_key(sid, tid)
            or custody["mutation_id"] == receipt["mutation_id"]
        ):
            raise ValueError("invalid_custody_identity")
    elif any(v is not None for v in custody_fields):
        raise ValueError("unexpected_custody")
    if body.state == "skipped" and observations:
        raise ValueError("invalid_skipped_state")
    return SessionSnapshot(
        sid,
        iid,
        owner,
        scope,
        classification,
        body.state,
        receipt,
        custody,
        tid,
        attempt,
        predecessor,
        body.response,
        observations,
        body.abandonment_reason,
        body.acknowledged_watermark,
        ack_at,
        body.turn_count,
        body.prepared_bytes,
        visit_id,
        body.visit_watermark,
        visit_at,
        result,
        audit,
        key,
    )


def validate_operation(
    value: object, **context: Unpack[SnapshotContext]
) -> SessionOperationResult:
    document = _object(
        value, frozenset({"outcome", "result", "mutation_receipt", "audit_receipt"})
    )
    if document["outcome"] not in {"committed", "replayed"}:
        raise ValueError("invalid_outcome")
    snapshot = validate_snapshot(document["result"], **context)
    _validate_receipts(document, snapshot.scope.realm)
    mutation = _mutation(document["mutation_receipt"])
    if mutation != snapshot.operational_receipt:
        raise ValueError("mismatched_operation_receipt")
    audit = freeze_object(document["audit_receipt"])
    if snapshot.custody_audit_receipt is not None and (
        snapshot.custody_audit_receipt["event_id"] == audit["event_id"]
        or cast(int, snapshot.custody_audit_receipt["sequence"])
        >= cast(int, audit["sequence"])
    ):
        raise ValueError("mismatched_operation_audit")
    return SessionOperationResult(document["outcome"], snapshot, mutation, audit)
