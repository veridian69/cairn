"""Grant records, evaluation and the delegation envelope."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation, PrincipalKind
from cairn.catalogue.audit import Classification, ScopeSegment


@dataclass(frozen=True, slots=True)
class GrantRecord:
    grant_id: UUID
    principal_id: UUID
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    operations: frozenset[GrantOperation]
    read_clearance: Classification
    write_classifications: frozenset[Classification]
    delegable_operations: frozenset[GrantOperation] | None
    issued_by: UUID | None
    expires_at: datetime | None
    created_at: datetime
    revoked: bool


@dataclass(frozen=True, slots=True)
class ProposedGrant:
    principal_id: UUID
    realm_id: str
    segments: tuple[ScopeSegment, ...]
    operations: frozenset[GrantOperation]
    read_clearance: Classification
    write_classifications: frozenset[Classification]
    delegable_operations: frozenset[GrantOperation] | None
    expires_at: datetime | None


def is_live(grant: GrantRecord, at: datetime) -> bool:
    if grant.revoked:
        return False
    if grant.expires_at is not None and at >= grant.expires_at:
        return False
    return True


def is_scope_prefix(
    prefix: tuple[ScopeSegment, ...], candidate: tuple[ScopeSegment, ...]
) -> bool:
    if len(prefix) > len(candidate):
        return False
    return candidate[: len(prefix)] == prefix


def find_authorising_grant(
    grants: Sequence[GrantRecord],
    *,
    realm_id: str,
    segments: tuple[ScopeSegment, ...],
    operation: GrantOperation,
    at: datetime,
) -> GrantRecord | None:
    for grant in grants:
        if grant.realm_id != realm_id:
            continue
        if operation not in grant.operations:
            continue
        if not is_scope_prefix(grant.segments, segments):
            continue
        if not is_live(grant, at):
            continue
        return grant
    return None


def delegation_violation(
    manager: GrantRecord,
    proposed: ProposedGrant,
    principal_kind: PrincipalKind,
    at: datetime,
) -> str | None:
    if not is_live(manager, at):
        return "manager_grant_not_live"
    if GrantOperation.GRANT_MANAGE in proposed.operations:
        return "grant_manage_not_delegable"
    if proposed.delegable_operations is not None:
        return "delegable_operations_not_permitted"
    if proposed.realm_id != manager.realm_id:
        return "realm_outside_envelope"
    if not is_scope_prefix(manager.segments, proposed.segments):
        return "scope_outside_envelope"
    if not proposed.operations <= (manager.delegable_operations or frozenset()):
        return "operation_outside_envelope"
    if (
        CLEARANCE_ORDER[proposed.read_clearance]
        > CLEARANCE_ORDER[manager.read_clearance]
    ):
        return "clearance_exceeds_envelope"
    if not proposed.write_classifications <= manager.write_classifications:
        return "classification_outside_envelope"
    if manager.expires_at is not None and (
        proposed.expires_at is None or proposed.expires_at > manager.expires_at
    ):
        return "expiry_exceeds_envelope"
    if principal_kind is PrincipalKind.WORKLOAD and proposed.expires_at is None:
        return "workload_grant_requires_expiry"
    return None
