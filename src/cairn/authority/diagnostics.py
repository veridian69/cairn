"""Authenticated, exact-scope grant snapshots; never a mutation preflight.

No realm discovery or grant administration capability is added. An authenticated
principal with no applicable grants receives an empty permission snapshot,
including for an unknown realm. Future operations still authorise independently.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation, PrincipalKind
from cairn.authority.gate import (
    Actor,
    credential_principal,
    credential_revoked,
    fetch_from,
    instance_denial_draft,
    instance_id,
    principal_kind,
    realm_draft,
    realm_exists,
)
from cairn.authority.grants import find_authorising_grant
from cairn.authority.mutations import _scope_shape_failure, _sorted_grants
from cairn.authority.retrieval import _covering_retrieve_grants
from cairn.catalogue.audit import (
    ActionKind,
    AuditValueError,
    Classification,
    Outcome,
    Scope,
)
from cairn.catalogue.sqlite import parse_timestamp, read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
    RetryClass,
    StableFailure,
)


@dataclass(frozen=True, slots=True)
class Diagnose:
    scope: Scope
    classification: Classification


@dataclass(frozen=True, slots=True)
class Permissions:
    """Grant checks only, at the requested exact scope and classification.

    Retrieve is the memory read clearance; ingest/promote use the same first
    authorising grant as writes. Invalidate has no classification check by policy.
    Promote describes target authority; source/evidence checks remain outstanding.
    """

    retrieve: bool
    ingest: bool
    promote: bool
    invalidate: bool


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    instance_id: UUID
    principal_id: UUID
    principal_kind: PrincipalKind
    scope: Scope
    classification: Classification
    permissions: Permissions
    evaluated_at: datetime


class CairnDiagnostics:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock

    def diagnose(
        self, actor: Actor, command: Diagnose, *, correlation_id: UUID
    ) -> DiagnosticSnapshot | Rejected:
        with read_connection(self._data_path) as connection:
            fetch = fetch_from(connection)
            now = self._clock()
            identity = instance_id(fetch)

            def refuse(code: FailureCode) -> Rejected:
                return self._transactions.reject(
                    instance_denial_draft(
                        identity,
                        actor,
                        "memory-diagnose",
                        code.value,
                        correlation_id,
                        action_kind=ActionKind.DATA,
                    ),
                    StableFailure(
                        code,
                        "The diagnostic request could not be completed.",
                        correlation_id,
                        RetryClass.NEVER,
                    ),
                )

            if (
                _scope_shape_failure(command.scope) is not None
                or type(command.classification) is not Classification
            ):
                return refuse(FailureCode.INVALID_REQUEST)
            kind = principal_kind(fetch, actor.principal_id)
            # The transport verified the secret. Recheck identity/expiry/revocation
            # at the snapshot instant without accepting a caller's identity claim.
            expiry = fetch(
                "SELECT expires_at FROM credentials WHERE credential_id = ?",
                (str(actor.credential_id),),
            )
            if (
                kind is None
                or credential_principal(fetch, actor.credential_id)
                != actor.principal_id
                or credential_revoked(fetch, actor.credential_id)
                or not expiry
                or (
                    isinstance(expiry[0][0], str)
                    and now >= parse_timestamp(expiry[0][0])
                )
            ):
                return refuse(FailureCode.AUTHENTICATION_FAILED)
            try:
                grants = _sorted_grants(fetch, actor.principal_id, command.scope.realm)
            except AuditValueError:
                return refuse(FailureCode.AUTHORISATION_DENIED)

            def permitted(operation: GrantOperation) -> bool:
                grant = find_authorising_grant(
                    grants,
                    realm_id=command.scope.realm,
                    segments=command.scope.segments,
                    operation=operation,
                    at=now,
                )
                return grant is not None and (
                    operation is GrantOperation.INVALIDATE
                    or command.classification in grant.write_classifications
                )

            permissions = Permissions(
                retrieve=any(
                    CLEARANCE_ORDER[g.read_clearance]
                    >= CLEARANCE_ORDER[command.classification]
                    for g in _covering_retrieve_grants(grants, command.scope, now)
                ),
                ingest=permitted(GrantOperation.INGEST),
                promote=permitted(GrantOperation.PROMOTE),
                invalidate=permitted(GrantOperation.INVALIDATE),
            )
            if realm_exists(fetch, command.scope.realm):
                draft = realm_draft(
                    realm_id=command.scope.realm,
                    actor=actor,
                    grant_id=None,
                    action_kind=ActionKind.DATA,
                    action_code="memory-diagnose",
                    requested_scope=command.scope,
                    outcome=Outcome.ALLOW,
                    reason_code="memory_diagnostics_completed",
                    correlation_id=correlation_id,
                )
            else:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [
                            command.scope.realm,
                            [(s.kind, s.identifier) for s in command.scope.segments],
                            command.classification.value,
                        ],
                        separators=(",", ":"),
                    ).encode()
                ).digest()
                draft = replace(
                    instance_denial_draft(
                        identity,
                        actor,
                        "memory-diagnose",
                        "memory_diagnostics_completed",
                        correlation_id,
                        action_kind=ActionKind.DATA,
                        safe_request_fingerprint=fingerprint,
                    ),
                    outcome=Outcome.ALLOW,
                )
            result = DiagnosticSnapshot(
                UUID(identity),
                actor.principal_id,
                kind,
                command.scope,
                command.classification,
                permissions,
                now,
            )
        self._transactions.append_audit(draft)
        return result
