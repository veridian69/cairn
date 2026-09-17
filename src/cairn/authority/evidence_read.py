"""Authorised immutable source custody, independent of fact search or trust."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER
from cairn.authority.gate import (
    AUTHORISATION_DENIED_MESSAGE,
    INVALID_REQUEST_MESSAGE,
    NOT_FOUND_MESSAGE,
    Actor,
    fetch_from,
    instance_denial_draft,
    instance_id,
    realm_draft,
    realm_exists,
)
from cairn.authority.grants import is_scope_prefix
from cairn.authority.mutations import (
    _scope_shape_failure,
    _sorted_grants,
    _stored_scope,
)
from cairn.authority.retrieval import _covering_retrieve_grants
from cairn.catalogue.audit import ActionKind, Classification, Outcome, Scope
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
    RetryClass,
    StableFailure,
)
from cairn.evidence.adapter import (
    AtticAdapter,
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
)
from cairn.operations.metrics import Metrics
from cairn.runtime.logging import LogEvent, SafeLogger


@dataclass(frozen=True, slots=True)
class ReadEvidence:
    scope: Scope
    evidence_id: UUID


@dataclass(frozen=True, slots=True)
class EvidenceReadResult:
    evidence_id: UUID
    payload: str
    sha256: str
    byte_length: int
    media_type: str = "text/plain; charset=utf-8"


def read_evidence(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    command: ReadEvidence,
    *,
    correlation_id: UUID,
    clock: Callable[[], datetime],
    enabled: bool,
    attic: AtticAdapter | None,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> EvidenceReadResult | Rejected:
    """Resolve catalogue authority before consulting the untrusted byte store.

    Actor authentication belongs to the transport, as for retrieve. Grants are
    read afresh on every call. Source evidence is not a validated fact: trust
    and fact invalidation do not erase the source needed to inspect a claim.
    """
    scope = command.scope
    grant_id: UUID | None = None
    with read_connection(data_path) as connection:
        fetch = fetch_from(connection)

        def reject(
            reason: str,
            code: FailureCode = FailureCode.INVALID_REQUEST,
            retry: RetryClass = RetryClass.NEVER,
        ) -> Rejected:
            if code is FailureCode.EVIDENCE_CORRUPT:
                if metrics is not None:
                    metrics.observe_evidence_digest_mismatch()
                if logger is not None:
                    logger.emit(
                        LogEvent.EVIDENCE_DIGEST_MISMATCH,
                        evidence_id=command.evidence_id,
                        transport=None,
                    )
            messages = {
                FailureCode.INVALID_REQUEST: INVALID_REQUEST_MESSAGE,
                FailureCode.NOT_FOUND: NOT_FOUND_MESSAGE,
                FailureCode.AUTHORISATION_DENIED: AUTHORISATION_DENIED_MESSAGE,
                FailureCode.EVIDENCE_PENDING: "Evidence delivery is pending.",
                FailureCode.EVIDENCE_CORRUPT: "Evidence integrity check failed.",
                FailureCode.DEPENDENCY_UNAVAILABLE: "Dependency unavailable.",
            }
            draft = (
                instance_denial_draft(
                    instance_id(fetch),
                    actor,
                    "read-evidence",
                    reason,
                    correlation_id,
                    action_kind=ActionKind.DATA,
                )
                if grant_id is None
                else realm_draft(
                    realm_id=scope.realm,
                    actor=actor,
                    grant_id=grant_id,
                    action_kind=ActionKind.DATA,
                    action_code="read-evidence",
                    requested_scope=scope,
                    outcome=Outcome.DENY,
                    reason_code=reason,
                    correlation_id=correlation_id,
                )
            )
            return transactions.reject(
                draft,
                StableFailure(
                    code=code,
                    safe_message=messages[code],
                    correlation_id=correlation_id,
                    retry=retry,
                ),
            )

        shape_failure = _scope_shape_failure(scope)
        if shape_failure is not None:
            return reject(shape_failure)
        if not realm_exists(fetch, scope.realm):
            return reject("realm_not_found", FailureCode.NOT_FOUND)
        covering = _covering_retrieve_grants(
            _sorted_grants(fetch, actor.principal_id, scope.realm), scope, clock()
        )
        if not covering:
            return reject("retrieve_grant_not_held", FailureCode.AUTHORISATION_DENIED)
        grant_id = covering[0].grant_id
        if type(command.evidence_id) is not UUID or command.evidence_id.version != 4:
            return reject("invalid_evidence_id")
        if not enabled:
            return reject("evidence_disabled")
        row = connection.execute(
            "SELECT realm_id, scope_segments, classification, payload_digest, "
            "payload_length, assertion_id FROM evidence_records WHERE evidence_id = ?",
            (str(command.evidence_id),),
        ).fetchone()
        if row is None:
            return reject("evidence_not_found", FailureCode.NOT_FOUND)
        realm, segments, classification, digest, length, assertion_id = row
        try:
            record_scope = _stored_scope(realm, segments)
            clearance = CLEARANCE_ORDER[Classification(classification)]
        except (TypeError, ValueError):
            return reject("evidence_not_found", FailureCode.NOT_FOUND)
        ceiling = max(CLEARANCE_ORDER[g.read_clearance] for g in covering)
        if (
            record_scope.realm != scope.realm
            or not is_scope_prefix(record_scope.segments, scope.segments)
            or clearance > ceiling
            or assertion_id is None
        ):
            return reject("evidence_not_found", FailureCode.NOT_FOUND)
        if attic is None:
            return reject(
                "evidence_unavailable",
                FailureCode.DEPENDENCY_UNAVAILABLE,
                RetryClass.AFTER_DELAY,
            )
        try:
            result = attic.fetch(command.evidence_id)
        except Exception:
            return reject(
                "evidence_fetch_failed",
                FailureCode.DEPENDENCY_UNAVAILABLE,
                RetryClass.AFTER_DELAY,
            )
        if isinstance(result, PayloadAbsent):
            pending = connection.execute(
                "SELECT 1 FROM evidence_outbox WHERE evidence_id = ? LIMIT 1",
                (str(command.evidence_id),),
            ).fetchone()
            if pending:
                return reject(
                    "evidence_pending",
                    FailureCode.EVIDENCE_PENDING,
                    RetryClass.AFTER_DELAY,
                )
            return reject(
                "evidence_unavailable",
                FailureCode.DEPENDENCY_UNAVAILABLE,
                RetryClass.AFTER_DELAY,
            )
        if isinstance(result, PayloadCorrupt) or not isinstance(result, FetchedPayload):
            return reject("evidence_corrupt", FailureCode.EVIDENCE_CORRUPT)
        payload = result.payload
        if (
            type(payload) is not bytes
            or len(payload) != length
            or hashlib.sha256(payload).digest() != digest
        ):
            return reject("evidence_corrupt", FailureCode.EVIDENCE_CORRUPT)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return reject("evidence_corrupt", FailureCode.EVIDENCE_CORRUPT)
        transactions.append_audit(
            realm_draft(
                realm_id=scope.realm,
                actor=actor,
                grant_id=grant_id,
                action_kind=ActionKind.DATA,
                action_code="read-evidence",
                requested_scope=scope,
                outcome=Outcome.ALLOW,
                reason_code="evidence_read_completed",
                correlation_id=correlation_id,
                affected_evidence_ids=(command.evidence_id,),
            )
        )
        return EvidenceReadResult(command.evidence_id, text, digest.hex(), length)
