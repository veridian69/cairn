"""Canonical operational payloads, restart decoding and future-ingest translation."""

import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.mutations import AssertionIngested, IngestAssertion
from cairn.authority.session_types import (
    DurableObservation,
    PrepareTurn,
    SessionSnapshot,
)
from cairn.catalogue.audit import ChainKind, Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import canonical_timestamp, parse_timestamp
from cairn.catalogue.transactions import AuditReceipt, MutationReceipt


def _default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        timestamp = canonical_timestamp(value)
        # The catalogue's fixed-width timestamp format is also the future
        # custody boundary. Some libc implementations omit year padding;
        # never accept preparation that cannot survive its restart decoder.
        if len(timestamp) != 27:
            raise ValueError("invalid_session_timestamp")
        return timestamp
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError("invalid_session_value")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        default=_default,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def future_ingest(
    command: PrepareTurn, classification: Classification
) -> IngestAssertion:
    """Same immutable candidate payload consumed by preparation and future custody.

    Empty batches are deliberately not ingest commands; callers skip this helper.
    Per-observation times still require validate_ingest_payload's explicit tuple.
    """
    return IngestAssertion(
        command.scope,
        classification,
        SourceType.AGENT_CLAIM,
        tuple(
            FactDraft(o.body, o.valid_from, o.valid_to) for o in command.observations
        ),
        observed_at=command.observations[0].observed_at,
    )


def _time(value: str | None) -> datetime | None:
    return None if value is None else parse_timestamp(value)


def observations_from(values: list[dict[str, Any]]) -> tuple[DurableObservation, ...]:
    return tuple(
        DurableObservation(
            v["body"],
            _time(v["valid_from"]),
            _time(v["valid_to"]),
            _time(v["observed_at"]),
        )
        for v in values
    )


def _receipt(value: dict[str, str]) -> MutationReceipt:
    return MutationReceipt(
        UUID(value["mutation_id"]), bytes.fromhex(value["command_digest"])
    )


def encode(value: SessionSnapshot, receipt: MutationReceipt) -> bytes:
    return canonical({"snapshot": asdict(value), "mutation_receipt": asdict(receipt)})


def decode(data: bytes) -> tuple[SessionSnapshot, MutationReceipt]:
    document = json.loads(data)
    value = document["snapshot"]
    for name in (
        "session_id",
        "instance_id",
        "principal_id",
        "turn_id",
        "attempt_id",
        "replaces_turn_id",
        "visit_id",
    ):
        value[name] = None if value[name] is None else UUID(value[name])
    scope = value["scope"]
    value["scope"] = Scope(
        scope["realm"],
        tuple(ScopeSegment(s["kind"], s["identifier"]) for s in scope["segments"]),
    )
    value["classification"] = Classification(value["classification"])
    value["observations"] = observations_from(value["observations"])
    value["operational_receipt"] = _receipt(value["operational_receipt"])
    if value["custody_receipt"] is not None:
        value["custody_receipt"] = _receipt(value["custody_receipt"])
    result = value.get("custody_result")
    if result is not None:
        value["custody_result"] = AssertionIngested(
            UUID(result["assertion_id"]),
            tuple(UUID(fid) for fid in result["fact_ids"]),
            None if result["evidence_id"] is None else UUID(result["evidence_id"]),
        )
    audit = value.get("custody_audit_receipt")
    if audit is not None:
        value["custody_audit_receipt"] = AuditReceipt(
            UUID(audit["event_id"]),
            ChainKind(audit["chain_kind"]),
            audit["chain_identity"],
            audit["sequence"],
            parse_timestamp(audit["recorded_at"]),
            bytes.fromhex(audit["event_hash"]),
        )
    key = value.get("custody_idempotency_key")
    if key is not None:
        value["custody_idempotency_key"] = UUID(key)
    for name in ("acknowledged_at", "visit_at"):
        value[name] = _time(value[name])
    return SessionSnapshot(**value), _receipt(document["mutation_receipt"])
