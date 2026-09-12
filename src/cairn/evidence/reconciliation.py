"""Evidence reconciliation (I-69): cross-checks untrusted Attic adapter
results against catalogue truth before anything is disclosed.

Every candidate must clear four independent checks against
``evidence_records`` before Attic is even asked for its bytes: the record
must exist, its realm must match, its scope must satisfy the caller's
ancestry rule (``scope_direction``, below), and its classification must be
within the caller's clearance. Only then is
``attic.fetch`` called, and even its answer is not trusted outright: the
fetched payload's digest is recomputed and compared against the digest the
catalogue recorded at ingest time. Hostile adapter output — wrong bytes, a
reported ``PayloadCorrupt``, simply nothing (``PayloadAbsent``), or a value
outside the adapter contract altogether — can therefore only ever narrow
what is disclosed, never widen it beyond what the catalogue and clearance
already allow.

A digest mismatch (whether computed here or reported by the adapter itself)
increments the digest-mismatch counter and logs one safe event carrying only
the evidence identity. Every other kind of exclusion — unknown identity,
duplicate, wrong realm, non-ancestor scope, classification above clearance,
absent payload — is silent: none of them are a public failure, and none but
the digest-mismatch case are even an operator-visible event, since they are
ordinary, expected outcomes of authority the caller never had.

I-69 fixes *that* a candidate's scope must satisfy the request's ancestry
rule; which direction that is belongs to the caller, so ``scope_direction``
names it explicitly rather than leaving one caller's rule embedded here as
if it were everyone's (P-43 as amended 9 August 2026). An audit-style read
wants records at the request scope or below it (``AT_OR_BELOW``, the
default this module was built with); retrieval wants the inherited-ancestor
direction SCOPE-01 gives facts, records at the request scope or above it
(``AT_OR_ABOVE``), so that the evidence gate and the fact filters compose
to the ancestry chain instead of intersecting to the request scope alone.

The one exception is a stored ``scope_segments`` value that is schema-legal
but semantically malformed (see ``_stored_segments``): that candidate is
still discarded silently to the caller, but is logged as unreadable, since
catalogue corruption must not be invisible to the operator even though it
must never be disclosed. Task 12's verification is the authoritative
detector; this log event is only the runtime hint that someone should run
it.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER
from cairn.authority.grants import is_scope_prefix
from cairn.catalogue.audit import AuditValueError, Classification, ScopeSegment
from cairn.catalogue.sqlite import read_connection
from cairn.evidence.adapter import (
    AtticAdapter,
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
)
from cairn.operations.metrics import Metrics
from cairn.runtime.logging import LogEvent, SafeLogger


class ScopeDirection(StrEnum):
    """Which way the caller's ancestry rule runs.

    ``AT_OR_BELOW`` admits records at the request scope and its
    descendants — the audit-read direction, and this module's original
    behaviour. ``AT_OR_ABOVE`` admits records at the request scope and its
    ancestors — SCOPE-01's inherited-ancestor rule, which retrieval needs.
    """

    AT_OR_BELOW = "at-or-below"
    AT_OR_ABOVE = "at-or-above"


@dataclass(frozen=True, slots=True)
class DisclosedEvidence:
    evidence_id: UUID
    payload: bytes
    classification: Classification
    segments: tuple[ScopeSegment, ...]


def reconcile_evidence(
    data_path: Path,
    attic: AtticAdapter,
    *,
    realm_id: str,
    segments: tuple[ScopeSegment, ...],
    read_clearance: Classification,
    candidates: Sequence[UUID],
    scope_direction: ScopeDirection = ScopeDirection.AT_OR_BELOW,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> tuple[DisclosedEvidence, ...]:
    disclosed: list[DisclosedEvidence] = []
    seen: set[UUID] = set()
    with read_connection(data_path) as connection:
        for evidence_id in candidates:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)

            row = connection.execute(
                "SELECT realm_id, scope_segments, classification, payload_digest "
                "FROM evidence_records WHERE evidence_id = ?",
                (str(evidence_id),),
            ).fetchone()
            if row is None:
                continue
            (
                record_realm_id,
                scope_segments_value,
                classification_value,
                record_digest,
            ) = _stored_row(row)

            if record_realm_id != realm_id:
                continue
            try:
                record_segments = _stored_segments(scope_segments_value)
            except AuditValueError:
                _emit(logger, LogEvent.EVIDENCE_RECORD_UNREADABLE, evidence_id)
                continue
            if not _scope_admits(scope_direction, segments, record_segments):
                continue
            # classification is a full IN-list CHECK (migration 0003): every
            # stored value already is one of the three enum spellings, so
            # this construction cannot raise.
            record_classification = Classification(classification_value)
            if CLEARANCE_ORDER[record_classification] > CLEARANCE_ORDER[read_clearance]:
                continue

            try:
                fetched = attic.fetch(evidence_id)
            except Exception:
                _emit(logger, LogEvent.EVIDENCE_FETCH_FAILED, evidence_id)
                continue
            if isinstance(fetched, PayloadAbsent):
                continue
            if isinstance(fetched, PayloadCorrupt):
                _record_digest_mismatch(metrics, logger, evidence_id)
                continue
            # Fail-closed on the result type itself, as deliver_evidence_outbox
            # already is for store: AtticAdapter is a Protocol, so nothing
            # enforces its return union at runtime, and an adapter may hand
            # back a value that is none of the three — None, a bare bytes, an
            # object of its own. Reading .payload from it would raise
            # AttributeError, which is not the TypeError guarded below, so it
            # would escape and deny the whole batch. Only an explicit
            # FetchedPayload is treated as an answer; anything else is a
            # failed fetch and is logged like a raised one.
            if not isinstance(fetched, FetchedPayload):
                _emit(logger, LogEvent.EVIDENCE_FETCH_FAILED, evidence_id)
                continue
            # I-69 declares every adapter untrusted, not just its content:
            # a hostile or buggy adapter can hand back a FetchedPayload
            # whose payload isn't even bytes, and hashlib would raise a raw
            # TypeError. One bad candidate must not deny the rest of the
            # batch (ruling 5), so this is caught exactly like a raised
            # fetch and logged the same way.
            try:
                digest_matches = (
                    hashlib.sha256(fetched.payload).digest() == record_digest
                )
            except TypeError:
                _emit(logger, LogEvent.EVIDENCE_FETCH_FAILED, evidence_id)
                continue
            if not digest_matches:
                _record_digest_mismatch(metrics, logger, evidence_id)
                continue

            disclosed.append(
                DisclosedEvidence(
                    evidence_id=evidence_id,
                    payload=fetched.payload,
                    classification=record_classification,
                    segments=record_segments,
                )
            )
    return tuple(disclosed)


def _scope_admits(
    direction: ScopeDirection,
    request_segments: tuple[ScopeSegment, ...],
    record_segments: tuple[ScopeSegment, ...],
) -> bool:
    if direction is ScopeDirection.AT_OR_BELOW:
        return is_scope_prefix(request_segments, record_segments)
    return is_scope_prefix(record_segments, request_segments)


def _stored_row(row: tuple[object, ...]) -> tuple[str, str, str, bytes]:
    return cast(tuple[str, str, str, bytes], row)


def _stored_segments(value: str) -> tuple[ScopeSegment, ...]:
    """The single reader of ``evidence_records.scope_segments`` in this
    module, mirroring ``cairn.authority.mutations._stored_scope``.

    ``ck_evidence_records_scope_segments`` (migration 0003) pins minified-
    JSON-array shape only — valid JSON, an array, at most 16 elements — and
    nothing about what each element contains. A row the schema accepts can
    still carry e.g. ``[{"kind": 7, "id": 1}]``, which ``ScopeSegment``
    construction refuses as a typed ``AuditValueError`` rather than letting a
    bare ``TypeError`` escape from ``re`` matching a non-``str``.
    """
    documents = json.loads(value)
    if type(documents) is not list:
        raise AuditValueError("invalid_scope")
    segments: list[ScopeSegment] = []
    for document in documents:
        if type(document) is not dict or set(document) != {"kind", "id"}:
            raise AuditValueError("invalid_scope")
        segments.append(ScopeSegment(kind=document["kind"], identifier=document["id"]))
    return tuple(segments)


def _record_digest_mismatch(
    metrics: Metrics | None, logger: SafeLogger | None, evidence_id: UUID
) -> None:
    if metrics is not None:
        metrics.observe_evidence_digest_mismatch()
    _emit(logger, LogEvent.EVIDENCE_DIGEST_MISMATCH, evidence_id)


def _emit(logger: SafeLogger | None, event: LogEvent, evidence_id: UUID) -> None:
    if logger is not None:
        logger.emit(event, evidence_id=evidence_id, transport=None)
