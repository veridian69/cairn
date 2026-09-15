"""The shared authorisation gate: grant lookup, evaluation, audit draft
construction and request-shape validation used by every Cairn command that
must authorise itself against the grant model before mutating or reading.

This module holds no policy of its own beyond what the callers already
encoded: it is the mechanical extraction of helpers that used to live
privately inside ``cairn.administration.commands`` so that data-plane
commands (custody assertions, facts, evidence) can reuse the same
authorisation machinery as the administration commands, instead of
duplicating it.
"""

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.grants import (
    GrantRecord,
    ProposedGrant,
    delegation_violation,
    find_authorising_grant,
    is_live,
)
from cairn.catalogue.audit import (
    MAX_SCOPE_SEGMENTS,
    ActionKind,
    AuditDraft,
    AuditValueError,
    ChainKind,
    Classification,
    ClassificationTransition,
    Outcome,
    Scope,
    ScopeSegment,
    TrustTransition,
)
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.catalogue.transactions import (
    FailureCode,
    FailureDetail,
    MutationRejection,
    RetryClass,
    StableFailure,
)

_REALM_ID_PATTERN = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LABEL_PATTERN = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REASON_CODE_PATTERN = re.compile(r"[a-z](?:[a-z0-9_]{0,61}[a-z0-9])?\Z")

type Fetch = Callable[[str, Sequence[object]], tuple[tuple[object, ...], ...]]
type _GrantRow = tuple[
    str, str, str, str, str, str, str, str | None, str | None, str | None, str, int
]


@dataclass(frozen=True, slots=True)
class Actor:
    principal_id: UUID
    credential_id: UUID


class SelfIssuer:
    pass


SELF_ISSUER = SelfIssuer()


# --- shape validators (I-26: typed, coarse, audited — never a raw crash) ---


def validate_realm_id(realm_id: str) -> str | None:
    return None if _REALM_ID_PATTERN.fullmatch(realm_id) else "invalid_realm_id"


def validate_label(label: str) -> str | None:
    return None if _LABEL_PATTERN.fullmatch(label) else "invalid_label"


def validate_reason_code(reason_code: str) -> str | None:
    return (
        None if _REASON_CODE_PATTERN.fullmatch(reason_code) else "invalid_reason_code"
    )


def validate_segments(segments: tuple[ScopeSegment, ...]) -> str | None:
    return None if len(segments) <= MAX_SCOPE_SEGMENTS else "invalid_scope"


def validate_expiry(expires_at: datetime | None) -> str | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        return "invalid_expiry"
    # Preserve the accepted four-digit UTC year boundary independently of
    # libc/Python strftime padding (Python 3.14 pads years below 1000).
    try:
        utc = expires_at.astimezone(UTC)
        if utc.year < 1000 or len(canonical_timestamp(utc)) != 27:
            return "invalid_expiry"
    except (OverflowError, ValueError):
        return "invalid_expiry"
    return None


# --- shared read helpers -------------------------------------------------


def fetch_from(connection: sqlite3.Connection) -> Fetch:
    def fetch(
        sql: str, parameters: Sequence[object] = ()
    ) -> tuple[tuple[object, ...], ...]:
        return tuple(connection.execute(sql, parameters).fetchall())

    return fetch


def realm_exists(fetch: Fetch, realm_id: str) -> bool:
    return bool(fetch("SELECT 1 FROM realms WHERE realm_id = ?", (realm_id,)))


def principal_kind(fetch: Fetch, principal_id: UUID) -> PrincipalKind | None:
    rows = fetch(
        "SELECT kind FROM principals WHERE principal_id = ?", (str(principal_id),)
    )
    if not rows:
        return None
    return PrincipalKind(cast(str, rows[0][0]))


def label_exists(fetch: Fetch, label: str) -> bool:
    return bool(fetch("SELECT 1 FROM principals WHERE label = ?", (label,)))


def credential_principal(fetch: Fetch, credential_id: UUID) -> UUID | None:
    rows = fetch(
        "SELECT principal_id FROM credentials WHERE credential_id = ?",
        (str(credential_id),),
    )
    if not rows:
        return None
    return _stored_credential_uuid(cast(str, rows[0][0]))


def credential_revoked(fetch: Fetch, credential_id: UUID) -> bool:
    return bool(
        fetch(
            "SELECT 1 FROM credential_revocations WHERE credential_id = ?",
            (str(credential_id),),
        )
    )


GRANT_SELECT = (
    "SELECT grant_id, principal_id, realm_id, scope_segments, operations, "
    "read_clearance, write_classifications, delegable_operations, issued_by, "
    "expires_at, created_at, "
    "EXISTS(SELECT 1 FROM grant_revocations gr WHERE gr.grant_id = grants.grant_id) "
    "FROM grants WHERE "
)


def grants_for_principal(
    fetch: Fetch, principal_id: UUID, realm_id: str
) -> tuple[GrantRecord, ...]:
    rows = fetch(
        GRANT_SELECT + "principal_id = ? AND realm_id = ?",
        (str(principal_id), realm_id),
    )
    return tuple(_row_to_grant(row) for row in rows)


def grants_for_principal_any_realm(
    fetch: Fetch, principal_id: UUID
) -> tuple[GrantRecord, ...]:
    rows = fetch(GRANT_SELECT + "principal_id = ?", (str(principal_id),))
    return tuple(_row_to_grant(row) for row in rows)


def grant_by_id(fetch: Fetch, grant_id: UUID) -> GrantRecord | None:
    rows = fetch(GRANT_SELECT + "grant_id = ?", (str(grant_id),))
    if not rows:
        return None
    return _row_to_grant(rows[0])


def principal_live_realms(
    fetch: Fetch, principal_id: UUID, now: datetime
) -> frozenset[str]:
    grants = grants_for_principal_any_realm(fetch, principal_id)
    return frozenset(grant.realm_id for grant in grants if is_live(grant, now))


def root_grant_manage(
    fetch: Fetch, principal_id: UUID, realm_id: str, now: datetime
) -> GrantRecord | None:
    # Sorted for the same reason delegating_grant sorts: grants_for_principal
    # carries no ORDER BY, so two evaluations of the same state must still
    # agree on which grant they'd pick.
    grants = tuple(
        sorted(
            grants_for_principal(fetch, principal_id, realm_id),
            key=lambda grant: str(grant.grant_id),
        )
    )
    return find_authorising_grant(
        grants,
        realm_id=realm_id,
        segments=(),
        operation=GrantOperation.GRANT_MANAGE,
        at=now,
    )


def reverify_root_manage(
    fetch: Fetch,
    actor: Actor,
    realm_id: str,
    manager: GrantRecord,
    now: datetime,
    *,
    action_code: str,
    requested_scope: Scope,
    correlation_id: UUID,
) -> None:
    """Re-derives the actor's root grant-manage grant inside the write lock
    and raises ``MutationRejection`` if it no longer matches the outer
    gate's ``manager`` — see the administration module docstring on why this
    is the one check that must be repeated here."""
    current = root_grant_manage(fetch, actor.principal_id, realm_id, now)
    if current is None or current.grant_id != manager.grant_id:
        raise denial(
            FailureCode.AUTHORISATION_DENIED,
            AUTHORISATION_DENIED_MESSAGE,
            correlation_id,
            realm_id=realm_id,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.ADMINISTRATION,
            action_code=action_code,
            requested_scope=requested_scope,
            reason_code=(
                "grant_manage_not_held"
                if current is None
                else "authorising_grant_changed"
            ),
        )


def grant_manage_candidates(
    fetch: Fetch, principal_id: UUID, realm_id: str
) -> tuple[GrantRecord, ...]:
    return tuple(
        sorted(
            (
                grant
                for grant in grants_for_principal(fetch, principal_id, realm_id)
                if GrantOperation.GRANT_MANAGE in grant.operations
            ),
            key=lambda grant: str(grant.grant_id),
        )
    )


def holds_any_grant_manage(fetch: Fetch, principal_id: UUID, realm_id: str) -> bool:
    return bool(grant_manage_candidates(fetch, principal_id, realm_id))


def delegating_grant(
    fetch: Fetch,
    actor_principal_id: UUID,
    realm_id: str,
    proposal: ProposedGrant,
    principal_kind: PrincipalKind,
    now: datetime,
) -> tuple[GrantRecord | None, str]:
    """Returns (authorising grant, "") when authorised, else (None, reason)."""
    candidates = grant_manage_candidates(fetch, actor_principal_id, realm_id)
    if not candidates:
        # Unreachable via either of create_grant's current callers: both
        # check holds_any_grant_manage over this same row set (existence of
        # a grant-manage-op row, immutable once created) before ever calling
        # here, so it can't have gone empty in between. Kept as a defensive
        # fallback rather than an assertion, in case a future caller invokes
        # this without that upstream guard.
        return None, "grant_manage_not_held"
    first_reason: str | None = None
    for candidate in candidates:
        violation = delegation_violation(candidate, proposal, principal_kind, now)
        if violation is None:
            return candidate, ""
        if first_reason is None:
            first_reason = violation
    assert first_reason is not None
    return None, first_reason


def revoking_authority(
    fetch: Fetch,
    actor: Actor,
    realm_id: str,
    target: GrantRecord,
    now: datetime,
) -> UUID | SelfIssuer | None:
    """Returns the authorising grant_id, ``SELF_ISSUER`` for a bare
    self-issuer revocation (GRANT-08, immutable and therefore never racy),
    or ``None`` when revocation is not authorised."""
    if target.issued_by == actor.principal_id:
        return SELF_ISSUER
    manager = root_grant_manage(fetch, actor.principal_id, realm_id, now)
    return manager.grant_id if manager is not None else None


def _stored_grant_segments(scope_segments: str) -> tuple[ScopeSegment, ...]:
    """The single reader of a grant's stored ``scope_segments`` column.

    ``ck_grants_scope_segments`` constrains shape — valid JSON, an array, at
    most 16 elements — and nothing about what each element contains, the same
    gap ``cairn.authority.mutations._stored_scope`` closes for
    ``facts.scope_segments``. A row the schema accepts can still carry
    ``[1]`` or ``[{"kind": 7, "id": 1}]``. Without the explicit dict-shape
    check below, the former raises a raw ``TypeError`` from subscripting an
    int — no code, no audit reason, no durable event — and reconstructing
    ``ScopeSegment`` is what refuses the latter as a typed
    ``AuditValueError``.
    """
    documents = json.loads(scope_segments)
    segments: list[ScopeSegment] = []
    for document in documents:
        if type(document) is not dict or set(document) != {"kind", "id"}:
            raise AuditValueError("invalid_scope")
        segments.append(ScopeSegment(kind=document["kind"], identifier=document["id"]))
    return tuple(segments)


def _stored_grant_uuid(value: str) -> UUID:
    """The single reader of a grant UUID column (``grant_id``, ``principal_id``,
    ``issued_by``).

    ``ck_grants_grant_id`` (and its siblings on the other two columns) bound
    ``length = 36`` plus a GLOB character class — but ``?`` matches *any*
    character and the negative class ``NOT GLOB '*[^0-9a-f-]*'`` permits
    ``-`` anywhere, so the GLOB constrains shape only, not that the dashes
    sit in the canonical positions. ``'0000000--0000-4000-8000-000000000000'``
    passes every CHECK and is 36 characters of only hex digits and dashes,
    but ``UUID(value)`` raises a raw, uncoded ``ValueError`` on it — the same
    failure mode as every other unguarded column in this function, on a
    column previously judged total by assuming the GLOB was total rather
    than checking it against SQLite.
    """
    try:
        return UUID(value)
    except ValueError as error:
        raise AuditValueError("grant_uuid_malformed") from error


def _stored_credential_uuid(value: str) -> UUID:
    """The single reader of ``credentials.principal_id``.

    Weaker than the grant columns ``_stored_grant_uuid`` guards, not stronger:
    that column carries no CHECK of its own (migration 0002), only a foreign
    key to ``principals.principal_id``, whose CHECK is the same shape-only
    GLOB proved non-total above. So the same
    ``'0000000--0000-4000-8000-000000000000'`` survives every constraint and
    raises a raw, uncoded ``ValueError`` here.

    Its own code rather than ``grant_uuid_malformed``: the defect is on a
    credential row, and an audit reason naming a grant would misdirect the
    operator who has to go and find it.
    """
    try:
        return UUID(value)
    except ValueError as error:
        raise AuditValueError("credential_uuid_malformed") from error


def _stored_grant_operations(value: str) -> frozenset[GrantOperation]:
    """The single reader of a grant's ``operations`` and ``delegable_operations``
    columns — both are JSON arrays of ``GrantOperation`` spellings, so one
    reader serves both.

    ``ck_grants_operations`` (and the shape half of
    ``ck_grants_delegable_operations_shape``) constrain valid JSON and array
    type — shape only, nothing about whether each element is one of the
    closed ``GrantOperation`` spellings. A row the schema accepts can still
    carry ``'["not-a-real-op"]'`` or ``'[1]'``; ``GrantOperation(value)``
    raises a raw, uncoded ``ValueError`` on either, escaping every command's
    typed-denial handling exactly as the unguarded timestamp and scope
    columns did. One code, ``grant_enum_malformed``, covers this column and
    ``write_classifications`` below: the defect is identical regardless of
    which enum-valued column is hostile, so there is no reason to mint one
    per column.
    """
    try:
        return frozenset(GrantOperation(value) for value in json.loads(value))
    except ValueError as error:
        raise AuditValueError("grant_enum_malformed") from error


def _stored_grant_classifications(value: str) -> frozenset[Classification]:
    """The single reader of a grant's ``write_classifications`` column. See
    ``_stored_grant_operations`` — the same gap, the same shared code."""
    try:
        return frozenset(Classification(value) for value in json.loads(value))
    except ValueError as error:
        raise AuditValueError("grant_enum_malformed") from error


def _stored_grant_timestamp(value: str) -> datetime:
    """The single reader of a stored grant timestamp (``expires_at`` or
    ``created_at``).

    ``ck_grants_expires_at`` and ``ck_grants_created_at`` pin the *shape* —
    27 characters matching the canonical GLOB — but nothing checks that the
    instant exists, so ``2026-13-45T99:99:99.000000Z`` is a row SQLite
    accepts. ``parse_timestamp`` answers that with ``CatalogueStorageError``,
    which is neither a ``CustodyValueError`` nor an ``AuditValueError`` and so
    would escape every command's typed-denial handling entirely. Converting
    here reuses the storage layer's own code as the audit reason rather than
    inventing vocabulary, mirroring
    ``cairn.authority.mutations._stored_timestamp``.
    """
    try:
        return parse_timestamp(value)
    except CatalogueStorageError as error:
        raise AuditValueError(error.code) from error


def _row_to_grant(row: tuple[object, ...]) -> GrantRecord:
    (
        grant_id,
        principal_id,
        realm_id,
        scope_segments,
        operations,
        read_clearance,
        write_classifications,
        delegable_operations,
        issued_by,
        expires_at,
        created_at,
        revoked,
    ) = cast(_GrantRow, row)
    return GrantRecord(
        grant_id=_stored_grant_uuid(grant_id),
        principal_id=_stored_grant_uuid(principal_id),
        realm_id=realm_id,
        segments=_stored_grant_segments(scope_segments),
        operations=_stored_grant_operations(operations),
        read_clearance=Classification(read_clearance),
        write_classifications=_stored_grant_classifications(write_classifications),
        delegable_operations=(
            None
            if delegable_operations is None
            else _stored_grant_operations(delegable_operations)
        ),
        issued_by=None if issued_by is None else _stored_grant_uuid(issued_by),
        expires_at=None if expires_at is None else _stored_grant_timestamp(expires_at),
        created_at=_stored_grant_timestamp(created_at),
        revoked=bool(revoked),
    )


# --- audit draft / denial helpers -----------------------------------------

# The stable safe-message surface: coarse, generic text (I-26) that never
# discloses which specific check failed. Single-sourced here so every
# administration and data-plane caller reports identical text for the same
# failure class.
NOT_FOUND_MESSAGE = "The referenced resource was not found."
AUTHORISATION_DENIED_MESSAGE = "The operation is not authorised."
AUTHENTICATION_FAILED_MESSAGE = "The request could not be authenticated."
INVALID_REQUEST_MESSAGE = "The request is invalid."
# The one message whose failure carries a ``FailureDetail`` beside it: I-72
# licenses naming the policy, rule and field path, so the message itself does
# not have to strain to be useful and stays as coarse as the rest.
SECRET_REJECTED_MESSAGE = "The request carries content the secret policy rejects."
# What an adapter says when it caught something it cannot describe. The
# foundation middleware carries the identical text in its own catch-all
# body (``rest/middleware.py``), spelled out there rather than imported
# because the dependency runs ``/v1`` → foundation and never back;
# ``test_the_internal_error_message_is_one_message`` holds the two equal.
INTERNAL_ERROR_MESSAGE = "The request could not be completed."


def realm_draft(
    *,
    realm_id: str,
    actor: Actor,
    grant_id: UUID | None,
    action_kind: ActionKind,
    action_code: str,
    requested_scope: Scope | None,
    outcome: Outcome,
    reason_code: str,
    correlation_id: UUID,
    affected_grant_ids: tuple[UUID, ...] = (),
    affected_assertion_ids: tuple[UUID, ...] = (),
    affected_fact_ids: tuple[UUID, ...] = (),
    affected_evidence_ids: tuple[UUID, ...] = (),
    evidence_reference: UUID | None = None,
    evidence_digest: bytes | None = None,
    # Promotion is the first action to move custody between scopes and
    # classes, so it is the first to populate these four. Every other caller
    # leaves them absent, which is what the audit record should say.
    source_scope: Scope | None = None,
    target_scope: Scope | None = None,
    classification_transition: ClassificationTransition | None = None,
    trust_transition: TrustTransition | None = None,
) -> AuditDraft:
    return AuditDraft(
        chain_kind=ChainKind.REALM,
        chain_identity=realm_id,
        principal_id=actor.principal_id,
        credential_verifier_id=actor.credential_id,
        grant_id=grant_id,
        action_kind=action_kind,
        action_code=action_code,
        source_scope=source_scope,
        requested_scope=requested_scope,
        target_scope=target_scope,
        outcome=outcome,
        reason_code=reason_code,
        affected_assertion_ids=affected_assertion_ids,
        affected_fact_ids=affected_fact_ids,
        affected_evidence_ids=affected_evidence_ids,
        affected_grant_ids=affected_grant_ids,
        classification_transition=classification_transition,
        trust_transition=trust_transition,
        evidence_reference=evidence_reference,
        evidence_digest=evidence_digest,
        correlation_id=correlation_id,
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=None,
    )


def denial(
    code: FailureCode,
    message: str,
    correlation_id: UUID,
    *,
    realm_id: str,
    actor: Actor,
    grant_id: UUID | None,
    action_kind: ActionKind,
    action_code: str,
    requested_scope: Scope,
    reason_code: str,
    detail: FailureDetail | None = None,
) -> MutationRejection:
    # ``detail`` is defaulted and passed by exactly one caller class — the
    # custody secret denials, whose I-72 disclosure it carries. Convention
    # keeps it absent elsewhere: I-26's coarse-failure rule licenses a
    # ``detail`` on ``secret_rejected`` and ``invalid_request`` only.
    failure = StableFailure(
        code=code,
        safe_message=message,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
        detail=detail,
    )
    draft = realm_draft(
        realm_id=realm_id,
        actor=actor,
        grant_id=grant_id,
        action_kind=action_kind,
        action_code=action_code,
        requested_scope=requested_scope,
        outcome=Outcome.DENY,
        reason_code=reason_code,
        correlation_id=correlation_id,
    )
    return MutationRejection(failure, draft)


def instance_id(fetch: Fetch) -> str:
    rows = fetch("SELECT instance_id FROM catalogue_metadata", ())
    return cast(str, rows[0][0])


def instance_denial_draft(
    instance_id: str,
    actor: Actor | None,
    action_code: str,
    reason_code: str,
    correlation_id: UUID,
    *,
    action_kind: ActionKind,
    safe_request_fingerprint: bytes | None = None,
) -> AuditDraft:
    """A denial with no realm chain to record on — either the realm itself
    is unknown, or the request's shape was rejected before a realm was even
    consulted — falls back to the always-present instance chain.

    ``actor`` is ``None`` for an authentication failure: nothing verified,
    so the event names no principal and no credential — an unverified
    caller claim is not an identity (Operator, 7 August 2026). Correlation of
    repeated attempts rides on ``safe_request_fingerprint`` instead.
    """
    return AuditDraft(
        chain_kind=ChainKind.INSTANCE,
        chain_identity=instance_id,
        principal_id=actor.principal_id if actor is not None else None,
        credential_verifier_id=actor.credential_id if actor is not None else None,
        grant_id=None,
        action_kind=action_kind,
        action_code=action_code,
        source_scope=None,
        requested_scope=None,
        target_scope=None,
        outcome=Outcome.DENY,
        reason_code=reason_code,
        affected_assertion_ids=(),
        affected_fact_ids=(),
        affected_evidence_ids=(),
        affected_grant_ids=(),
        classification_transition=None,
        trust_transition=None,
        evidence_reference=None,
        evidence_digest=None,
        correlation_id=correlation_id,
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=safe_request_fingerprint,
    )
