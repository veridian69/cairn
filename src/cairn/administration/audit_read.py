"""Metadata-only Cairn audit reads.

A read is authorised by a live ``audit-read`` grant covering the requested
scope prefix. Candidates are located cheaply through ``audit_scope_index``
(an over-inclusive scan by realm chain and prefix ordinals), but disclosure
is decided only after each candidate's canonical bytes are loaded and
re-parsed: P-03 is re-checked against the *parsed* scopes, so a drifted
index row can point a query at the wrong sequence without ever disclosing
the event that actually lives there. The successful read itself is appended
as a durable ``allow`` event on the realm chain *after* the candidate page
is assembled, so it never appears in its own page (AUDIT-02, AUDIT-03).

This has a pagination consequence at ``limit=1``: because every successful
read appends one matching ``allow`` event to the very chain it reads, the
next page always has at least that event waiting, so ``next_after_sequence``
can never come back ``None`` at that limit — the sequence never terminates
on its own. Callers must use ``limit >= 2`` or otherwise handle non-
termination; API layers built on this module should not assume a bare
``limit=1`` loop will ever finish.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.authority.credentials import GrantOperation
from cairn.authority.gate import (
    AUTHORISATION_DENIED_MESSAGE as _AUTHORISATION_DENIED_MESSAGE,
)
from cairn.authority.gate import INVALID_REQUEST_MESSAGE as _INVALID_REQUEST_MESSAGE
from cairn.authority.gate import NOT_FOUND_MESSAGE as _NOT_FOUND_MESSAGE
from cairn.authority.gate import Actor
from cairn.authority.gate import Fetch as _Fetch
from cairn.authority.gate import fetch_from as _fetch_from
from cairn.authority.gate import grants_for_principal as _grants_for_principal
from cairn.authority.gate import instance_denial_draft as _instance_denial_draft
from cairn.authority.gate import instance_id as _instance_id
from cairn.authority.gate import realm_draft as _admin_draft
from cairn.authority.gate import realm_exists as _realm_exists
from cairn.authority.gate import validate_realm_id as _validate_realm_id
from cairn.authority.gate import validate_segments as _validate_segments
from cairn.authority.grants import find_authorising_grant, is_scope_prefix
from cairn.catalogue.audit import (
    ActionKind,
    AuditEvent,
    Outcome,
    Scope,
    ScopeSegment,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
    RetryClass,
    StableFailure,
)

_MIN_LIMIT = 1
_MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ReadAuditEvents:
    realm_id: str
    scope_prefix: tuple[ScopeSegment, ...]
    after_sequence: int = 0
    limit: int = 100


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    events: tuple[AuditEvent, ...]
    next_after_sequence: int | None


def read_audit_events(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    command: ReadAuditEvents,
    *,
    correlation_id: UUID,
    clock: Callable[[], datetime],
) -> AuditEventPage | Rejected:
    shape_error = (
        _validate_realm_id(command.realm_id)
        or _validate_limit(command.limit)
        or _validate_segments(command.scope_prefix)
    )
    if shape_error is not None:
        return _reject_shape(
            data_path,
            transactions,
            actor,
            shape_error,
            correlation_id,
            fingerprint_source=(
                command.realm_id if shape_error == "invalid_realm_id" else None
            ),
        )

    now = clock()
    requested_scope = Scope(command.realm_id, command.scope_prefix)

    with read_connection(data_path) as connection:
        fetch = _fetch_from(connection)
        if not _realm_exists(fetch, command.realm_id):
            return _reject_unknown_realm(
                transactions, fetch, actor, command.realm_id, correlation_id
            )

        grants = sorted(
            _grants_for_principal(fetch, actor.principal_id, command.realm_id),
            key=lambda grant: str(grant.grant_id),
        )
        authorising = find_authorising_grant(
            grants,
            realm_id=command.realm_id,
            segments=command.scope_prefix,
            operation=GrantOperation.AUDIT_READ,
            at=now,
        )
        if authorising is None:
            return _reject_denied(
                transactions, command.realm_id, actor, requested_scope, correlation_id
            )

        candidates = _candidate_sequences(
            fetch, command.realm_id, command.scope_prefix, command.after_sequence
        )
        matched: list[AuditEvent] = []
        for sequence in candidates:
            event = parse_canonical_audit_bytes(
                _load_canonical(fetch, command.realm_id, sequence)
            )
            if _matches_prefix(event, command.scope_prefix):
                matched.append(event)
                if len(matched) > command.limit:
                    break

    if len(matched) > command.limit:
        page_events = tuple(matched[: command.limit])
        next_after_sequence: int | None = page_events[-1].sequence
    else:
        page_events = tuple(matched)
        next_after_sequence = None

    allow_draft = _admin_draft(
        realm_id=command.realm_id,
        actor=actor,
        grant_id=authorising.grant_id,
        action_kind=ActionKind.ADMINISTRATION,
        action_code="audit-read",
        requested_scope=requested_scope,
        outcome=Outcome.ALLOW,
        reason_code="audit_read_completed",
        correlation_id=correlation_id,
    )
    transactions.append_audit(allow_draft)

    return AuditEventPage(events=page_events, next_after_sequence=next_after_sequence)


# --- shape validation --------------------------------------------------------


def _validate_limit(limit: int) -> str | None:
    return None if _MIN_LIMIT <= limit <= _MAX_LIMIT else "invalid_limit"


# --- candidate location and reconciliation -----------------------------------


def _candidate_sequences(
    fetch: _Fetch,
    realm_id: str,
    prefix: tuple[ScopeSegment, ...],
    after_sequence: int,
) -> tuple[int, ...]:
    if not prefix:
        rows = fetch(
            "SELECT sequence FROM audit_events WHERE chain_kind = 'realm' "
            "AND chain_identity = ? AND sequence > ? ORDER BY sequence",
            (realm_id, after_sequence),
        )
        return tuple(cast(int, row[0]) for row in rows)

    rows = fetch(
        "SELECT sequence, role, ordinal, segment_kind, segment_id "
        "FROM audit_scope_index WHERE chain_kind = 'realm' AND chain_identity = ? "
        "AND sequence > ? AND ordinal BETWEEN 0 AND ? "
        "ORDER BY sequence, role, ordinal",
        (realm_id, after_sequence, len(prefix) - 1),
    )
    grouped: dict[tuple[int, str], dict[int, tuple[str, str]]] = {}
    for sequence, role, ordinal, segment_kind, segment_id in cast(
        tuple[tuple[int, str, int, str, str], ...], rows
    ):
        grouped.setdefault((sequence, role), {})[ordinal] = (segment_kind, segment_id)

    prefix_key = tuple((segment.kind, segment.identifier) for segment in prefix)
    matches: set[int] = set()
    for (sequence, _role), by_ordinal in grouped.items():
        if tuple(by_ordinal.get(i) for i in range(len(prefix))) == prefix_key:
            matches.add(sequence)
    return tuple(sorted(matches))


def _load_canonical(fetch: _Fetch, realm_id: str, sequence: int) -> bytes:
    rows = fetch(
        "SELECT canonical_event FROM audit_events WHERE chain_kind = 'realm' "
        "AND chain_identity = ? AND sequence = ?",
        (realm_id, sequence),
    )
    return cast(bytes, rows[0][0])


def _matches_prefix(event: AuditEvent, prefix: tuple[ScopeSegment, ...]) -> bool:
    """P-03: every non-null scope must be at or below ``prefix``; an event
    with no scopes at all matches only the realm-root prefix."""
    scopes = (
        event.draft.source_scope,
        event.draft.requested_scope,
        event.draft.target_scope,
    )
    non_null = tuple(scope for scope in scopes if scope is not None)
    if not non_null:
        return len(prefix) == 0
    return all(is_scope_prefix(prefix, scope.segments) for scope in non_null)


# --- denial helpers -----------------------------------------------------------


def _reject_shape(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    reason_code: str,
    correlation_id: UUID,
    *,
    fingerprint_source: str | None,
) -> Rejected:
    with read_connection(data_path) as connection:
        instance_id = _instance_id(_fetch_from(connection))
    draft = _instance_denial_draft(
        instance_id,
        actor,
        "audit-read",
        reason_code,
        correlation_id,
        action_kind=ActionKind.ADMINISTRATION,
        safe_request_fingerprint=(
            None
            if fingerprint_source is None
            else hashlib.sha256(fingerprint_source.encode()).digest()
        ),
    )
    failure = StableFailure(
        code=FailureCode.INVALID_REQUEST,
        safe_message=_INVALID_REQUEST_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
    )
    return transactions.reject(draft, failure)


def _reject_unknown_realm(
    transactions: CatalogueTransactions,
    fetch: _Fetch,
    actor: Actor,
    attempted_realm_id: str,
    correlation_id: UUID,
) -> Rejected:
    draft = _instance_denial_draft(
        _instance_id(fetch),
        actor,
        "audit-read",
        "realm_not_found",
        correlation_id,
        action_kind=ActionKind.ADMINISTRATION,
        safe_request_fingerprint=hashlib.sha256(attempted_realm_id.encode()).digest(),
    )
    failure = StableFailure(
        code=FailureCode.NOT_FOUND,
        safe_message=_NOT_FOUND_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
    )
    return transactions.reject(draft, failure)


def _reject_denied(
    transactions: CatalogueTransactions,
    realm_id: str,
    actor: Actor,
    requested_scope: Scope,
    correlation_id: UUID,
) -> Rejected:
    draft = _admin_draft(
        realm_id=realm_id,
        actor=actor,
        grant_id=None,
        action_kind=ActionKind.ADMINISTRATION,
        action_code="audit-read",
        requested_scope=requested_scope,
        outcome=Outcome.DENY,
        reason_code="audit_read_grant_not_held",
        correlation_id=correlation_id,
    )
    failure = StableFailure(
        code=FailureCode.AUTHORISATION_DENIED,
        safe_message=_AUTHORISATION_DENIED_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
    )
    return transactions.reject(draft, failure)
