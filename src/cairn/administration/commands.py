"""CairnAdministration: principal, credential and grant mutations.

Every mutation runs an **outer gate** on a read-only connection, before the
write lock is ever acquired: caller-input shape (label/reason-code/scope
syntax, tz-aware expiry), realm existence, and full authorisation. Any
failure there short-circuits straight to ``CatalogueTransactions.reject``
without ever calling ``mutate_idempotent`` — this is load-bearing, not
advisory: ``mutate_idempotent`` returns a stored ``Replayed`` result straight
from the idempotency record without invoking the mutation callback, so if
authorisation were only checked inside that callback, a replay of a stale
idempotency key would skip re-authorisation entirely (violating I-44, which
requires fresh authorisation on every replay). Realm existence, principal/
credential/grant existence and (P-02) cross-realm grant control are safe to
decide once in the outer gate because every relevant row in this schema is
immutable or append-only (nothing is ever un-created, un-revoked or
un-expired), so their truth value cannot regress between the outer read and
the write. The one exception is ``_principal_live_realms`` for P-02, which is
a genuinely growing set (a concurrent writer can grant the target principal
live access in a new realm while the outer gate's read is still in flight),
so ``issue_credential`` re-evaluates it fresh inside the transaction rather
than trusting the outer gate's pass — see its in-transaction P-02 re-check.

Only one thing genuinely needs re-verification *inside* the write-locked
transaction: whether the **specific** grant the outer gate found still
authorises, since a grant can be revoked by a concurrent writer between the
outer read and this transaction acquiring the lock. The mutation callback
re-derives authorisation and compares it by grant_id against what the outer
gate captured, raising ``MutationRejection`` on any mismatch — fail closed;
the spurious denial this occasionally produces is safe to retry since
denials are never cached as idempotency results.

Two further checks stay inside the transaction despite being logically
"first-attempt-only": duplicate-label detection and double-revocation
detection. These race against *other concurrent callers*, not against time,
so an outer-only check would leave a live window for two callers to attempt
the same label or the same revocation past the outer gate and collide on a
raw uniqueness/primary-key constraint inside SQL.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.authority.credentials import PrincipalKind, mint_token
from cairn.authority.gate import (
    AUTHORISATION_DENIED_MESSAGE as _AUTHORISATION_DENIED_MESSAGE,
)
from cairn.authority.gate import INVALID_REQUEST_MESSAGE as _INVALID_REQUEST_MESSAGE
from cairn.authority.gate import NOT_FOUND_MESSAGE as _NOT_FOUND_MESSAGE
from cairn.authority.gate import SECRET_REJECTED_MESSAGE as _SECRET_REJECTED_MESSAGE
from cairn.authority.gate import SELF_ISSUER as _SELF_ISSUER
from cairn.authority.gate import Actor as Actor
from cairn.authority.gate import Fetch as _Fetch
from cairn.authority.gate import credential_principal as _credential_principal
from cairn.authority.gate import credential_revoked as _credential_revoked
from cairn.authority.gate import delegating_grant as _delegating_grant
from cairn.authority.gate import denial as _denial
from cairn.authority.gate import fetch_from as _fetch_from
from cairn.authority.gate import grant_by_id as _grant_by_id
from cairn.authority.gate import holds_any_grant_manage as _holds_any_grant_manage
from cairn.authority.gate import instance_denial_draft as _instance_denial_draft
from cairn.authority.gate import instance_id as _instance_id
from cairn.authority.gate import label_exists as _label_exists
from cairn.authority.gate import principal_kind as _principal_kind
from cairn.authority.gate import principal_live_realms as _principal_live_realms
from cairn.authority.gate import realm_draft as _admin_draft
from cairn.authority.gate import realm_exists as _realm_exists
from cairn.authority.gate import reverify_root_manage as _reverify_root_manage
from cairn.authority.gate import revoking_authority as _revoking_authority
from cairn.authority.gate import root_grant_manage as _root_grant_manage
from cairn.authority.gate import validate_expiry as _validate_expiry
from cairn.authority.gate import validate_label as _validate_label
from cairn.authority.gate import validate_realm_id as _validate_realm_id
from cairn.authority.gate import validate_reason_code as _validate_reason_code
from cairn.authority.gate import validate_segments as _validate_segments
from cairn.authority.grants import ProposedGrant
from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    Outcome,
    Scope,
)
from cairn.catalogue.sqlite import (
    canonical_timestamp,
    parse_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    FailureDetail,
    MutationOutcome,
    MutationReceipt,
    MutationRejection,
    Rejected,
    RetryClass,
    StableFailure,
    _MutationTransaction,
)
from cairn.screening import (
    POLICY_VERSION,
    SecretFinding,
    SecretScreen,
    audit_reason_code,
    first_finding,
)

_ADMIN_SCHEMA = "cairn.admin/v1"


# --- commands (closed, frozen; I-60) ----------------------------------------


@dataclass(frozen=True, slots=True)
class CreatePrincipal:
    realm_id: str
    kind: PrincipalKind
    label: str


@dataclass(frozen=True, slots=True)
class IssueCredential:
    realm_id: str
    principal_id: UUID
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RevokeCredential:
    realm_id: str
    credential_id: UUID
    reason_code: str


@dataclass(frozen=True, slots=True)
class CreateGrant:
    realm_id: str
    grant: ProposedGrant


@dataclass(frozen=True, slots=True)
class RevokeGrant:
    realm_id: str
    grant_id: UUID
    reason_code: str


# --- results -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrincipalCreated:
    principal_id: UUID
    kind: PrincipalKind
    label: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlaintextUnavailable:
    pass


@dataclass(frozen=True, slots=True)
class CredentialIssued:
    credential_id: UUID
    principal_id: UUID
    expires_at: datetime | None
    created_at: datetime
    plaintext: str | PlaintextUnavailable


@dataclass(frozen=True, slots=True)
class CredentialRevoked:
    credential_id: UUID
    revoked_at: datetime


@dataclass(frozen=True, slots=True)
class GrantCreated:
    grant_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GrantRevoked:
    grant_id: UUID
    revoked_at: datetime


class CairnAdministration:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        entropy: Callable[[int], bytes],
        screen: SecretScreen,
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._entropy = entropy
        self._screen = screen

    def create_principal(
        self,
        actor: Actor,
        command: CreatePrincipal,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[PrincipalCreated]:
        shape_error = _validate_realm_id(command.realm_id) or _validate_label(
            command.label
        )
        if shape_error is not None:
            return self._reject_shape(
                actor,
                "create-principal",
                shape_error,
                correlation_id,
                fingerprint_source=(
                    command.realm_id if shape_error == "invalid_realm_id" else None
                ),
            )

        now = self._clock()
        new_principal_id = self._uuid_factory()
        requested_scope = Scope(command.realm_id, ())

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, command.realm_id):
                return self._reject_unknown_realm(
                    fetch, actor, "create-principal", command.realm_id, correlation_id
                )
            manager = _root_grant_manage(
                fetch, actor.principal_id, command.realm_id, now
            )
            if manager is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "create-principal",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "grant_manage_not_held",
                    correlation_id,
                )

        digest = _digest(
            "create-principal",
            {
                "realm_id": command.realm_id,
                "kind": command.kind.value,
                "label": command.label,
            },
        )
        draft = _admin_draft(
            realm_id=command.realm_id,
            actor=actor,
            grant_id=manager.grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="create-principal",
            requested_scope=requested_scope,
            outcome=Outcome.ALLOW,
            reason_code="principal_created",
            correlation_id=correlation_id,
        )

        def mutation(transaction: _MutationTransaction) -> PrincipalCreated:
            fetch = transaction.query
            _reverify_root_manage(
                fetch,
                actor,
                command.realm_id,
                manager,
                now,
                action_code="create-principal",
                requested_scope=requested_scope,
                correlation_id=correlation_id,
            )
            if _label_exists(fetch, command.label):
                raise _denial(
                    FailureCode.INVALID_REQUEST,
                    _INVALID_REQUEST_MESSAGE,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=manager.grant_id,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="create-principal",
                    requested_scope=requested_scope,
                    reason_code="label_exists",
                )
            # After value validation (I-74) and after the in-transaction
            # reverification, inside the mutation callback — the placement
            # P-26 originally approved, restored by Operator's ruling of 7 August
            # 2026; see ``CairnAuthority.ingest`` for the three contracts the
            # pre-gate placement silently dropped. Last of the state checks,
            # matching both revoke commands: a command refused for catalogue
            # state was never eligible to run, so it earns the state failure
            # rather than a screening verdict and its I-72 disclosure. A
            # label is screened like any other text rather than trusted for
            # its shape: I-74 is explicit that a 63-character lowercase name
            # can carry a lowercase hexadecimal token.
            finding = first_finding(self._screen, (("label", command.label),))
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=manager.grant_id,
                    action_code="create-principal",
                    requested_scope=requested_scope,
                )
            transaction.execute(
                "INSERT INTO principals (principal_id, kind, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(new_principal_id),
                    command.kind.value,
                    command.label,
                    canonical_timestamp(now),
                ),
            )
            return PrincipalCreated(
                principal_id=new_principal_id,
                kind=command.kind,
                label=command.label,
                created_at=now,
            )

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="create-principal",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.admin.principal/v1",
            mutation=mutation,
            encode=_encode_principal_created,
            decode=_decode_principal_created,
        )

    def issue_credential(
        self,
        actor: Actor,
        command: IssueCredential,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[CredentialIssued]:
        shape_error = _validate_realm_id(command.realm_id) or _validate_expiry(
            command.expires_at
        )
        if shape_error is not None:
            return self._reject_shape(
                actor,
                "issue-credential",
                shape_error,
                correlation_id,
                fingerprint_source=(
                    command.realm_id if shape_error == "invalid_realm_id" else None
                ),
            )

        now = self._clock()
        new_credential_id = self._uuid_factory()
        minted = mint_token(new_credential_id, self._entropy)
        requested_scope = Scope(command.realm_id, ())

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, command.realm_id):
                return self._reject_unknown_realm(
                    fetch, actor, "issue-credential", command.realm_id, correlation_id
                )
            manager = _root_grant_manage(
                fetch, actor.principal_id, command.realm_id, now
            )
            if manager is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "issue-credential",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "grant_manage_not_held",
                    correlation_id,
                )
            if _principal_kind(fetch, command.principal_id) is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    manager.grant_id,
                    "issue-credential",
                    requested_scope,
                    FailureCode.NOT_FOUND,
                    _NOT_FOUND_MESSAGE,
                    "principal_not_found",
                    correlation_id,
                )
            live_realms = _principal_live_realms(fetch, command.principal_id, now)
            for live_realm in live_realms:
                if (
                    _root_grant_manage(fetch, actor.principal_id, live_realm, now)
                    is None
                ):
                    return self._reject_realm(
                        command.realm_id,
                        actor,
                        manager.grant_id,
                        "issue-credential",
                        requested_scope,
                        FailureCode.AUTHORISATION_DENIED,
                        _AUTHORISATION_DENIED_MESSAGE,
                        "cross_realm_principal",
                        correlation_id,
                    )

        digest = _digest(
            "issue-credential",
            {
                "realm_id": command.realm_id,
                "principal_id": str(command.principal_id),
                "expires_at": None
                if command.expires_at is None
                else canonical_timestamp(command.expires_at),
            },
        )
        draft = _admin_draft(
            realm_id=command.realm_id,
            actor=actor,
            grant_id=manager.grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="issue-credential",
            requested_scope=requested_scope,
            outcome=Outcome.ALLOW,
            reason_code="credential_issued",
            correlation_id=correlation_id,
        )

        def mutation(transaction: _MutationTransaction) -> CredentialIssued:
            fetch = transaction.query
            _reverify_root_manage(
                fetch,
                actor,
                command.realm_id,
                manager,
                now,
                action_code="issue-credential",
                requested_scope=requested_scope,
                correlation_id=correlation_id,
            )
            # P-02 re-check: the outer gate's live_realms set can only grow
            # while it waits for the write lock (a concurrent writer can
            # grant the target principal live access in a realm the actor
            # doesn't control), so a pass there is not authoritative — it
            # must be re-evaluated fresh, under the lock, right before the
            # credential is actually written.
            live_realms = _principal_live_realms(fetch, command.principal_id, now)
            for live_realm in live_realms:
                if (
                    _root_grant_manage(fetch, actor.principal_id, live_realm, now)
                    is None
                ):
                    raise _denial(
                        FailureCode.AUTHORISATION_DENIED,
                        _AUTHORISATION_DENIED_MESSAGE,
                        correlation_id,
                        realm_id=command.realm_id,
                        actor=actor,
                        grant_id=None,
                        action_kind=ActionKind.ADMINISTRATION,
                        action_code="issue-credential",
                        requested_scope=requested_scope,
                        reason_code="cross_realm_principal",
                    )
            transaction.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(new_credential_id),
                    str(command.principal_id),
                    minted.verifier,
                    canonical_timestamp(now),
                    None
                    if command.expires_at is None
                    else canonical_timestamp(command.expires_at),
                ),
            )
            return CredentialIssued(
                credential_id=new_credential_id,
                principal_id=command.principal_id,
                expires_at=command.expires_at,
                created_at=now,
                plaintext=minted.text,
            )

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="issue-credential",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.admin.credential/v1",
            mutation=mutation,
            encode=_encode_credential_issued,
            decode=_decode_credential_issued,
        )

    def revoke_credential(
        self,
        actor: Actor,
        command: RevokeCredential,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[CredentialRevoked]:
        shape_error = _validate_realm_id(command.realm_id) or _validate_reason_code(
            command.reason_code
        )
        if shape_error is not None:
            return self._reject_shape(
                actor,
                "revoke-credential",
                shape_error,
                correlation_id,
                fingerprint_source=(
                    command.realm_id if shape_error == "invalid_realm_id" else None
                ),
            )

        now = self._clock()
        requested_scope = Scope(command.realm_id, ())

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, command.realm_id):
                return self._reject_unknown_realm(
                    fetch, actor, "revoke-credential", command.realm_id, correlation_id
                )
            manager = _root_grant_manage(
                fetch, actor.principal_id, command.realm_id, now
            )
            if manager is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "revoke-credential",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "grant_manage_not_held",
                    correlation_id,
                )
            target_principal_id = _credential_principal(fetch, command.credential_id)
            if target_principal_id is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    manager.grant_id,
                    "revoke-credential",
                    requested_scope,
                    FailureCode.NOT_FOUND,
                    _NOT_FOUND_MESSAGE,
                    "credential_not_found",
                    correlation_id,
                )
            # NOTE: "already revoked" is deliberately NOT checked here. A
            # replay of the very call that revoked this credential must still
            # reach mutate_idempotent and return the cached result; checking
            # this in the outer gate would reject that legitimate replay
            # before it ever got a chance to match its idempotency record.
            # It is checked inside the transaction instead, where it only
            # runs on a genuine first attempt (mutate_idempotent skips the
            # callback entirely for replays) and also guards against a
            # concurrent caller revoking the same credential first.
            #
            # Unlike issue_credential's P-02 check, this one is deliberately
            # outer-only with no in-transaction re-check — not the round-2 bug
            # fixed there. It tests membership of a fixed, already-resolved
            # principal rather than re-deriving a growing set, and it only
            # ever denies a revocation, never grants access; going stale in
            # the race window therefore fails open toward revoking, the safe
            # direction for incident response.
            live_realms = _principal_live_realms(fetch, target_principal_id, now)
            if live_realms and command.realm_id not in live_realms:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    manager.grant_id,
                    "revoke-credential",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "cross_realm_principal",
                    correlation_id,
                )

        digest = _digest(
            "revoke-credential",
            {
                "realm_id": command.realm_id,
                "credential_id": str(command.credential_id),
                "reason_code": command.reason_code,
            },
        )
        draft = _admin_draft(
            realm_id=command.realm_id,
            actor=actor,
            grant_id=manager.grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="revoke-credential",
            requested_scope=requested_scope,
            outcome=Outcome.ALLOW,
            reason_code="credential_revoked",
            correlation_id=correlation_id,
        )

        def mutation(transaction: _MutationTransaction) -> CredentialRevoked:
            fetch = transaction.query
            _reverify_root_manage(
                fetch,
                actor,
                command.realm_id,
                manager,
                now,
                action_code="revoke-credential",
                requested_scope=requested_scope,
                correlation_id=correlation_id,
            )
            if _credential_revoked(fetch, command.credential_id):
                raise _denial(
                    FailureCode.INVALID_REQUEST,
                    _INVALID_REQUEST_MESSAGE,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=manager.grant_id,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="revoke-credential",
                    requested_scope=requested_scope,
                    reason_code="credential_already_revoked",
                )
            # See ``create_principal`` for the in-callback placement. I-74
            # screens reason codes for the same reason it screens labels: the
            # grammar is narrow, but a lowercase hexadecimal token fits
            # inside it. Last of the three checks, matching ``revoke_grant``:
            # a command already refused for its target's state was never
            # eligible to run, so it gets the state failure rather than a
            # screening verdict and its I-72 disclosure — the same reasoning
            # that leaves an idempotency conflict undisclosed.
            finding = first_finding(
                self._screen, (("reason_code", command.reason_code),)
            )
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=manager.grant_id,
                    action_code="revoke-credential",
                    requested_scope=requested_scope,
                )
            transaction.execute(
                "INSERT INTO credential_revocations "
                "(credential_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
                (
                    str(command.credential_id),
                    canonical_timestamp(now),
                    str(actor.principal_id),
                    command.reason_code,
                ),
            )
            return CredentialRevoked(
                credential_id=command.credential_id, revoked_at=now
            )

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="revoke-credential",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.admin.credential_revocation/v1",
            mutation=mutation,
            encode=_encode_credential_revoked,
            decode=_decode_credential_revoked,
        )

    def create_grant(
        self,
        actor: Actor,
        command: CreateGrant,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[GrantCreated]:
        proposal = command.grant
        shape_error = (
            _validate_realm_id(command.realm_id)
            or _validate_segments(proposal.segments)
            or _validate_expiry(proposal.expires_at)
        )
        if shape_error is not None:
            return self._reject_shape(
                actor,
                "create-grant",
                shape_error,
                correlation_id,
                fingerprint_source=(
                    command.realm_id if shape_error == "invalid_realm_id" else None
                ),
            )

        now = self._clock()
        new_grant_id = self._uuid_factory()
        requested_scope = Scope(command.realm_id, proposal.segments)

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, command.realm_id):
                return self._reject_unknown_realm(
                    fetch, actor, "create-grant", command.realm_id, correlation_id
                )
            # Checked before the principal lookup below: an actor holding no
            # grant-manage grant at all in this realm is denied identically
            # whether or not the target principal exists, so existence is
            # never disclosed to an actor with zero standing to act on it.
            if not _holds_any_grant_manage(fetch, actor.principal_id, command.realm_id):
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "create-grant",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "grant_manage_not_held",
                    correlation_id,
                )
            kind = _principal_kind(fetch, proposal.principal_id)
            if kind is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "create-grant",
                    requested_scope,
                    FailureCode.NOT_FOUND,
                    _NOT_FOUND_MESSAGE,
                    "principal_not_found",
                    correlation_id,
                )
            authorising, reason = _delegating_grant(
                fetch, actor.principal_id, command.realm_id, proposal, kind, now
            )
            if authorising is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "create-grant",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    reason,
                    correlation_id,
                )

        digest = _digest(
            "create-grant",
            {"realm_id": command.realm_id, "grant": _proposal_document(proposal)},
        )
        draft = _admin_draft(
            realm_id=command.realm_id,
            actor=actor,
            grant_id=authorising.grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="create-grant",
            requested_scope=requested_scope,
            outcome=Outcome.ALLOW,
            reason_code="grant_created",
            affected_grant_ids=(new_grant_id,),
            correlation_id=correlation_id,
        )

        def mutation(transaction: _MutationTransaction) -> GrantCreated:
            fetch = transaction.query
            current, current_reason = _delegating_grant(
                fetch, actor.principal_id, command.realm_id, proposal, kind, now
            )
            if current is None or current.grant_id != authorising.grant_id:
                raise _denial(
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=None,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="create-grant",
                    requested_scope=requested_scope,
                    reason_code=(
                        current_reason
                        if current is None
                        else "authorising_grant_changed"
                    ),
                )
            transaction.execute(
                "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
                "operations, read_clearance, write_classifications, delegable_operations, "
                "issued_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(new_grant_id),
                    str(proposal.principal_id),
                    proposal.realm_id,
                    _json_column(
                        [
                            {"id": s.identifier, "kind": s.kind}
                            for s in proposal.segments
                        ]
                    ),
                    _json_column(sorted(op.value for op in proposal.operations)),
                    proposal.read_clearance.value,
                    _json_column(
                        sorted(c.value for c in proposal.write_classifications)
                    ),
                    None
                    if proposal.delegable_operations is None
                    else _json_column(
                        sorted(op.value for op in proposal.delegable_operations)
                    ),
                    str(actor.principal_id),
                    None
                    if proposal.expires_at is None
                    else canonical_timestamp(proposal.expires_at),
                    canonical_timestamp(now),
                ),
            )
            return GrantCreated(grant_id=new_grant_id, created_at=now)

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="create-grant",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.admin.grant/v1",
            mutation=mutation,
            encode=_encode_grant_created,
            decode=_decode_grant_created,
            restate_identities=_restate_grant_identities,
        )

    def revoke_grant(
        self,
        actor: Actor,
        command: RevokeGrant,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[GrantRevoked]:
        shape_error = _validate_realm_id(command.realm_id) or _validate_reason_code(
            command.reason_code
        )
        if shape_error is not None:
            return self._reject_shape(
                actor,
                "revoke-grant",
                shape_error,
                correlation_id,
                fingerprint_source=(
                    command.realm_id if shape_error == "invalid_realm_id" else None
                ),
            )

        now = self._clock()
        requested_scope = Scope(command.realm_id, ())

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, command.realm_id):
                return self._reject_unknown_realm(
                    fetch, actor, "revoke-grant", command.realm_id, correlation_id
                )
            target = _grant_by_id(fetch, command.grant_id)
            is_self_issuer = (
                target is not None
                and target.realm_id == command.realm_id
                and target.issued_by == actor.principal_id
            )
            # Checked before the not-found branch below: an actor who is
            # neither the target's self-issuer (GRANT-08, safe regardless —
            # they already know their own issued grant ids) nor a holder of
            # any grant-manage grant in this realm is denied identically
            # whether or not the target grant exists, so existence is never
            # disclosed to an actor with zero standing to act on it.
            if not is_self_issuer and not _holds_any_grant_manage(
                fetch, actor.principal_id, command.realm_id
            ):
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "revoke-grant",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "grant_manage_not_held",
                    correlation_id,
                )
            if target is None or target.realm_id != command.realm_id:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "revoke-grant",
                    requested_scope,
                    FailureCode.NOT_FOUND,
                    _NOT_FOUND_MESSAGE,
                    "grant_not_found",
                    correlation_id,
                )
            # NOTE: "already revoked" is deliberately NOT checked here — see
            # the identical note in revoke_credential. A replay of the call
            # that revoked this grant must still reach mutate_idempotent.
            authority = _revoking_authority(fetch, actor, command.realm_id, target, now)
            if authority is None:
                return self._reject_realm(
                    command.realm_id,
                    actor,
                    None,
                    "revoke-grant",
                    requested_scope,
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    "revocation_not_authorised",
                    correlation_id,
                )

        authorising_grant_id = authority if isinstance(authority, UUID) else None
        digest = _digest(
            "revoke-grant",
            {
                "realm_id": command.realm_id,
                "grant_id": str(command.grant_id),
                "reason_code": command.reason_code,
            },
        )
        draft = _admin_draft(
            realm_id=command.realm_id,
            actor=actor,
            grant_id=authorising_grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="revoke-grant",
            requested_scope=requested_scope,
            outcome=Outcome.ALLOW,
            reason_code="grant_revoked",
            affected_grant_ids=(command.grant_id,),
            correlation_id=correlation_id,
        )

        def mutation(transaction: _MutationTransaction) -> GrantRevoked:
            fetch = transaction.query
            current_target = _grant_by_id(fetch, command.grant_id)
            if current_target is None or current_target.revoked:
                raise _denial(
                    FailureCode.INVALID_REQUEST,
                    _INVALID_REQUEST_MESSAGE,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=None,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="revoke-grant",
                    requested_scope=requested_scope,
                    reason_code="grant_already_revoked",
                )
            current_authority = _revoking_authority(
                fetch, actor, command.realm_id, current_target, now
            )
            if authority is _SELF_ISSUER:
                matched = current_authority is _SELF_ISSUER
            else:
                matched = (
                    isinstance(current_authority, UUID)
                    and current_authority == authority
                )
            if not matched:
                raise _denial(
                    FailureCode.AUTHORISATION_DENIED,
                    _AUTHORISATION_DENIED_MESSAGE,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=None,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="revoke-grant",
                    requested_scope=requested_scope,
                    reason_code=(
                        "revocation_not_authorised"
                        if current_authority is None
                        else "authorising_grant_changed"
                    ),
                )
            # See ``create_principal`` for the in-callback placement and
            # ``revoke_credential`` for why a reason code is not trusted for
            # its grammar. The grant named is whatever the eventual allow
            # event would have named — ``None`` for a self-issued revocation,
            # since no grant-manage grant authorised it.
            finding = first_finding(
                self._screen, (("reason_code", command.reason_code),)
            )
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=command.realm_id,
                    actor=actor,
                    grant_id=authorising_grant_id,
                    action_code="revoke-grant",
                    requested_scope=requested_scope,
                )
            transaction.execute(
                "INSERT INTO grant_revocations "
                "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
                (
                    str(command.grant_id),
                    canonical_timestamp(now),
                    str(actor.principal_id),
                    command.reason_code,
                ),
            )
            return GrantRevoked(grant_id=command.grant_id, revoked_at=now)

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="revoke-grant",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.admin.grant_revocation/v1",
            mutation=mutation,
            encode=_encode_grant_revoked,
            decode=_decode_grant_revoked,
        )

    # --- outer-gate rejection helpers (bypass mutate_idempotent entirely) --

    def _reject(
        self,
        draft: AuditDraft,
        code: FailureCode,
        message: str,
        correlation_id: UUID,
    ) -> Rejected:
        failure = StableFailure(
            code=code,
            safe_message=message,
            correlation_id=correlation_id,
            retry=RetryClass.NEVER,
        )
        return self._transactions.reject(draft, failure)

    def _reject_shape(
        self,
        actor: Actor,
        action_code: str,
        reason_code: str,
        correlation_id: UUID,
        *,
        fingerprint_source: str | None = None,
    ) -> Rejected:
        with read_connection(self._data_path) as connection:
            instance_id = _instance_id(_fetch_from(connection))
        draft = _instance_denial_draft(
            instance_id,
            actor,
            action_code,
            reason_code,
            correlation_id,
            action_kind=ActionKind.ADMINISTRATION,
            safe_request_fingerprint=(
                None
                if fingerprint_source is None
                else hashlib.sha256(fingerprint_source.encode()).digest()
            ),
        )
        return self._reject(
            draft, FailureCode.INVALID_REQUEST, _INVALID_REQUEST_MESSAGE, correlation_id
        )

    def _reject_unknown_realm(
        self,
        fetch: _Fetch,
        actor: Actor,
        action_code: str,
        attempted_realm_id: str,
        correlation_id: UUID,
    ) -> Rejected:
        draft = _instance_denial_draft(
            _instance_id(fetch),
            actor,
            action_code,
            "realm_not_found",
            correlation_id,
            action_kind=ActionKind.ADMINISTRATION,
            safe_request_fingerprint=hashlib.sha256(
                attempted_realm_id.encode()
            ).digest(),
        )
        return self._reject(
            draft, FailureCode.NOT_FOUND, _NOT_FOUND_MESSAGE, correlation_id
        )

    def _reject_realm(
        self,
        realm_id: str,
        actor: Actor,
        grant_id: UUID | None,
        action_code: str,
        requested_scope: Scope,
        code: FailureCode,
        message: str,
        reason_code: str,
        correlation_id: UUID,
    ) -> Rejected:
        draft = _admin_draft(
            realm_id=realm_id,
            actor=actor,
            grant_id=grant_id,
            action_kind=ActionKind.ADMINISTRATION,
            action_code=action_code,
            requested_scope=requested_scope,
            outcome=Outcome.DENY,
            reason_code=reason_code,
            correlation_id=correlation_id,
        )
        return self._reject(draft, code, message, correlation_id)


# --- the custody secret denial (I-74, P-26) ----------------------------------


def _secret_denial(
    finding: SecretFinding,
    correlation_id: UUID,
    *,
    realm_id: str,
    actor: Actor,
    grant_id: UUID | None,
    action_code: str,
    requested_scope: Scope,
) -> MutationRejection:
    """A custody-screen refusal, raised from inside the mutation callback.

    The event's ``reason_code`` carries the rule and nothing else; the field
    path travels back to the caller in ``detail`` and never into the chain,
    per P-26.
    """
    return _denial(
        FailureCode.SECRET_REJECTED,
        _SECRET_REJECTED_MESSAGE,
        correlation_id,
        realm_id=realm_id,
        actor=actor,
        grant_id=grant_id,
        action_kind=ActionKind.ADMINISTRATION,
        action_code=action_code,
        requested_scope=requested_scope,
        reason_code=audit_reason_code(finding.rule),
        detail=FailureDetail(
            policy=POLICY_VERSION,
            rule=finding.rule,
            field_path=finding.field_path,
        ),
    )


# --- JSON and digests -------------------------------------------------------


def _json_column(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _digest(command_name: str, fields: dict[str, object]) -> bytes:
    document: dict[str, object] = {"command": command_name, "schema": _ADMIN_SCHEMA}
    document.update(fields)
    return hashlib.sha256(_canonical_json(document)).digest()


def _proposal_document(grant: ProposedGrant) -> dict[str, object]:
    return {
        "principal_id": str(grant.principal_id),
        "realm_id": grant.realm_id,
        "segments": [
            {"id": segment.identifier, "kind": segment.kind}
            for segment in grant.segments
        ],
        "operations": sorted(operation.value for operation in grant.operations),
        "read_clearance": grant.read_clearance.value,
        "write_classifications": sorted(
            value.value for value in grant.write_classifications
        ),
        "delegable_operations": (
            None
            if grant.delegable_operations is None
            else sorted(operation.value for operation in grant.delegable_operations)
        ),
        "expires_at": None
        if grant.expires_at is None
        else canonical_timestamp(grant.expires_at),
    }


def _receipt_document(receipt: MutationReceipt) -> dict[str, object]:
    return {
        "command_digest": receipt.command_digest.hex(),
        "mutation_id": str(receipt.mutation_id),
    }


def _receipt_from_document(document: dict[str, object]) -> MutationReceipt:
    return MutationReceipt(
        mutation_id=UUID(cast(str, document["mutation_id"])),
        command_digest=bytes.fromhex(cast(str, document["command_digest"])),
    )


# --- result encode/decode ---------------------------------------------------


def _encode_principal_created(
    value: PrincipalCreated, receipt: MutationReceipt
) -> bytes:
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "principal_id": str(value.principal_id),
                "kind": value.kind.value,
                "label": value.label,
                "created_at": canonical_timestamp(value.created_at),
            },
        }
    )


def _decode_principal_created(data: bytes) -> tuple[PrincipalCreated, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = PrincipalCreated(
        principal_id=UUID(result["principal_id"]),
        kind=PrincipalKind(result["kind"]),
        label=result["label"],
        created_at=parse_timestamp(result["created_at"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _encode_credential_issued(
    value: CredentialIssued, receipt: MutationReceipt
) -> bytes:
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "credential_id": str(value.credential_id),
                "principal_id": str(value.principal_id),
                "expires_at": (
                    None
                    if value.expires_at is None
                    else canonical_timestamp(value.expires_at)
                ),
                "created_at": canonical_timestamp(value.created_at),
            },
        }
    )


def _decode_credential_issued(data: bytes) -> tuple[CredentialIssued, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = CredentialIssued(
        credential_id=UUID(result["credential_id"]),
        principal_id=UUID(result["principal_id"]),
        expires_at=(
            None
            if result["expires_at"] is None
            else parse_timestamp(result["expires_at"])
        ),
        created_at=parse_timestamp(result["created_at"]),
        plaintext=PlaintextUnavailable(),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _encode_credential_revoked(
    value: CredentialRevoked, receipt: MutationReceipt
) -> bytes:
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "credential_id": str(value.credential_id),
                "revoked_at": canonical_timestamp(value.revoked_at),
            },
        }
    )


def _decode_credential_revoked(
    data: bytes,
) -> tuple[CredentialRevoked, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = CredentialRevoked(
        credential_id=UUID(result["credential_id"]),
        revoked_at=parse_timestamp(result["revoked_at"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _restate_grant_identities(draft: AuditDraft, value: GrantCreated) -> AuditDraft:
    """``create_grant`` mints its grant identity before ``mutate_idempotent``,
    so a replay would otherwise name a grant that was never created. The only
    administration command that needs this: every other one names identities
    the caller supplied, which survive a replay unchanged."""
    return replace(draft, affected_grant_ids=(value.grant_id,))


def _encode_grant_created(value: GrantCreated, receipt: MutationReceipt) -> bytes:
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "grant_id": str(value.grant_id),
                "created_at": canonical_timestamp(value.created_at),
            },
        }
    )


def _decode_grant_created(data: bytes) -> tuple[GrantCreated, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = GrantCreated(
        grant_id=UUID(result["grant_id"]),
        created_at=parse_timestamp(result["created_at"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _encode_grant_revoked(value: GrantRevoked, receipt: MutationReceipt) -> bytes:
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "grant_id": str(value.grant_id),
                "revoked_at": canonical_timestamp(value.revoked_at),
            },
        }
    )


def _decode_grant_revoked(data: bytes) -> tuple[GrantRevoked, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = GrantRevoked(
        grant_id=UUID(result["grant_id"]),
        revoked_at=parse_timestamp(result["revoked_at"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])
