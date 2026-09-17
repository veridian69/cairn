import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import cast
from uuid import RFC_4122, UUID

from cairn.catalogue.audit import (
    ZERO_HASH,
    AuditEvent,
    AuditValueError,
    Scope,
    ScopeRole,
    ScopeSegment,
    canonical_audit_bytes,
    hash_audit_event,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.migration import (
    _CATALOGUE_FORMAT,
    _PACKAGED_MIGRATIONS,
    Migration,
    MigrationError,
    execute_migration_statements,
    load_migration_set,
)
from cairn.catalogue.proposal_verification import (
    ProposalVerificationError,
    verify_proposals,
)
from cairn.catalogue.session_verification import (
    SessionVerificationError,
    verify_sessions,
)
from cairn.catalogue.sqlite import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_verification_connection,
    _open_write_connection,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.runtime.config import CairnConfig
from cairn.runtime.lease import DataDirectoryLease, LeaseError

# Re-derived rather than trusted, for the same reason the enum and octet
# bounds are: a catalogue restored from a pre-STRICT schema carries values its
# CHECKs would now refuse. The policy is applied to every total CHECK this
# phase depends on, not to some of them.
_MAX_SCOPE_SEGMENTS = 16

# The closed sets behind the grant model's three enum-valued JSON columns.
#
# Restated rather than imported for the same reason the bound above is
# re-derived: this phase has to detect a catalogue carrying values the live
# vocabulary would now refuse, and importing that vocabulary would hide
# exactly the drift it exists to find.
#
# _GRANT_OPERATIONS has a second, independent reason — GrantOperation lives
# in cairn.authority, and the catalogue layer must not import the authority
# layer above it. _CLASSIFICATIONS has no such excuse: Classification lives
# in cairn.catalogue.audit, the same layer, already imported at the top of
# this file. Re-derivation is the whole of its justification.
#
# Both are pinned against their enums in tests/catalogue/test_verification.py,
# so adding a member to either is a failing test rather than a verify that
# starts rejecting healthy catalogues — the false positive that would be
# worse for a verification tool than the false negative it was added to fix.
_GRANT_OPERATIONS = (
    "audit-read",
    "grant-manage",
    "ingest",
    "invalidate",
    "promote",
    "retrieve",
)
_CLASSIFICATIONS = ("public", "internal", "restricted")


class VerificationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"catalogue verification error: {code}")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: int
    instance_id: UUID
    realm_count: int
    event_count: int
    idempotency_count: int
    principal_count: int
    credential_count: int
    grant_count: int
    assertion_count: int
    fact_count: int
    invalidation_count: int
    evidence_count: int
    evidence_outbox_depth: int
    projection_outbox_depth: int


@dataclass(frozen=True, slots=True)
class _CustodyInventory:
    assertion_count: int
    fact_count: int
    invalidation_count: int
    evidence_count: int
    evidence_outbox_depth: int
    projection_outbox_depth: int


@dataclass(frozen=True, slots=True)
class _StoredFact:
    realm_id: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class _StoredEvidence:
    realm_id: str
    payload_digest: bytes
    payload_length: int | None


@dataclass(frozen=True, slots=True)
class _VerifiedAudit:
    events: dict[str, AuditEvent]
    heads: dict[tuple[str, str], tuple[int, bytes]]


@dataclass(frozen=True, slots=True)
class _ParsedAudit:
    row: tuple[object, ...]
    event: AuditEvent
    canonical: bytes
    event_hash: bytes


def verify_catalogue(config: CairnConfig) -> VerificationReport:
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    try:
        lease.acquire()
        return _verify_catalogue_locked(config)
    except LeaseError as error:
        raise VerificationError("catalogue_unavailable") from error
    finally:
        try:
            lease.release()
        except LeaseError as error:
            raise VerificationError("catalogue_unavailable") from error


def _verify_catalogue_locked(config: CairnConfig) -> VerificationReport:
    try:
        with _open_verification_connection(config.paths.data) as connection:
            return _verify_connection(connection, config.instance_id)
    except VerificationError:
        raise
    except CatalogueStorageError as error:
        raise VerificationError("catalogue_unavailable") from error
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise VerificationError("catalogue_invalid") from error


def _verify_connection(
    connection: sqlite3.Connection,
    instance_id: UUID,
    *,
    verify_scope_index: bool = True,
) -> VerificationReport:
    _verify_sqlite(connection)
    _verify_migrations(connection)
    realm_count = _verify_identity(connection, instance_id)
    audit = _verify_audit(connection, verify_scope_index=verify_scope_index)
    _verify_heads(connection, instance_id, audit)
    idempotency_count = _verify_idempotency(connection, audit.events)
    principal_count, credential_count, grant_count = _verify_authority(connection)
    custody = _verify_custody(connection, audit.events)
    _verify_memory(connection, audit.events)
    try:
        verify_sessions(connection, instance_id, audit.events)
    except SessionVerificationError as error:
        raise VerificationError("session_integrity_invalid") from error
    try:
        verify_proposals(connection, instance_id, audit.events)
    except ProposalVerificationError as error:
        raise VerificationError("proposal_integrity_invalid") from error
    return VerificationReport(
        schema_version=CURRENT_SCHEMA_VERSION,
        instance_id=instance_id,
        realm_count=realm_count,
        event_count=len(audit.events),
        idempotency_count=idempotency_count,
        principal_count=principal_count,
        credential_count=credential_count,
        grant_count=grant_count,
        assertion_count=custody.assertion_count,
        fact_count=custody.fact_count,
        invalidation_count=custody.invalidation_count,
        evidence_count=custody.evidence_count,
        evidence_outbox_depth=custody.evidence_outbox_depth,
        projection_outbox_depth=custody.projection_outbox_depth,
    )


def _verify_sqlite(connection: sqlite3.Connection) -> None:
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        raise VerificationError("sqlite_integrity_failed") from error
    if integrity != [("ok",)]:
        raise VerificationError("sqlite_integrity_failed")
    try:
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise VerificationError("foreign_key_failed") from error
    if foreign_keys:
        raise VerificationError("foreign_key_failed")


def _verify_migrations(connection: sqlite3.Connection) -> None:
    try:
        migrations = load_migration_set(_PACKAGED_MIGRATIONS)
        expected_schema = _expected_schema(migrations)
    except MigrationError as error:
        raise VerificationError("packaged_migrations_invalid") from error
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
        rows = connection.execute(
            "SELECT version, name, sql_sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        schema = _read_schema(connection)
    except sqlite3.Error as error:
        raise VerificationError("migration_history_invalid") from error
    if application_id != (APPLICATION_ID,):
        raise VerificationError("application_id_mismatch")
    if user_version != (CURRENT_SCHEMA_VERSION,):
        raise VerificationError("schema_version_mismatch")
    expected_rows = [
        (migration.version, migration.name, migration.sha256)
        for migration in migrations
    ]
    if rows != expected_rows:
        raise VerificationError("migration_history_invalid")
    if schema != expected_schema:
        raise VerificationError("catalogue_schema_invalid")


def _expected_schema(
    migrations: tuple[Migration, ...],
) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration in migrations:
            execute_migration_statements(connection, migration.statements)
        return _read_schema(connection)
    finally:
        connection.close()


def _read_schema(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        cast(
            list[tuple[str, str, str, str]],
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall(),
        )
    )


def _verify_identity(connection: sqlite3.Connection, instance_id: UUID) -> int:
    try:
        metadata = connection.execute(
            "SELECT format, instance_id, created_at FROM catalogue_metadata"
        ).fetchall()
        realms = connection.execute(
            "SELECT realm_id, created_at FROM realms ORDER BY realm_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("catalogue_identity_invalid") from error
    expected_id = str(instance_id)
    if len(metadata) != 1 or metadata[0][0] != _CATALOGUE_FORMAT:
        raise VerificationError("catalogue_identity_invalid")
    if metadata[0][1] != expected_id:
        raise VerificationError("instance_mismatch")
    if not _canonical_timestamp(metadata[0][2]):
        raise VerificationError("catalogue_identity_invalid")
    for realm_id, created_at in realms:
        if not _valid_realm(realm_id) or not _canonical_timestamp(created_at):
            raise VerificationError("catalogue_identity_invalid")
    return len(realms)


def _verify_audit(
    connection: sqlite3.Connection,
    *,
    verify_scope_index: bool,
) -> _VerifiedAudit:
    try:
        rows = connection.execute(
            "SELECT chain_kind, chain_identity, sequence, event_id, recorded_at, "
            "previous_hash, event_hash, action_kind, action_code, outcome, "
            "reason_code, canonical_event FROM audit_events "
            "ORDER BY chain_kind, chain_identity, sequence"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("audit_event_invalid") from error
    parsed: list[_ParsedAudit] = []
    event_ids: set[str] = set()
    for row in rows:
        if len(row) != 12 or type(row[11]) is not bytes:
            raise VerificationError("audit_event_invalid")
        try:
            event = parse_canonical_audit_bytes(row[11])
            canonical = canonical_audit_bytes(event)
            event_hash = hash_audit_event(event)
        except AuditValueError as error:
            raise VerificationError("audit_event_invalid") from error
        event_id = str(event.event_id)
        if event_id in event_ids:
            raise VerificationError("audit_event_invalid")
        event_ids.add(event_id)
        parsed.append(
            _ParsedAudit(
                row=row,
                event=event,
                canonical=canonical,
                event_hash=event_hash,
            )
        )

    for item in parsed:
        event = item.event
        draft = event.draft
        projection = (
            draft.chain_kind.value,
            draft.chain_identity,
            event.sequence,
            str(event.event_id),
            canonical_timestamp(event.recorded_at),
            event.previous_hash,
            item.event_hash,
            draft.action_kind.value,
            draft.action_code,
            draft.outcome.value,
            draft.reason_code,
            item.canonical,
        )
        if item.row != projection:
            raise VerificationError("audit_projection_invalid")

    if verify_scope_index:
        for item in parsed:
            _verify_scope_projection(connection, item.event)

    events: dict[str, AuditEvent] = {}
    heads: dict[tuple[str, str], tuple[int, bytes]] = {}
    for item in parsed:
        event = item.event
        draft = event.draft
        chain = (draft.chain_kind.value, draft.chain_identity)
        prior_sequence, prior_hash = heads.get(chain, (0, ZERO_HASH))
        if event.sequence != prior_sequence + 1 or event.previous_hash != prior_hash:
            raise VerificationError("audit_chain_invalid")
        events[str(event.event_id)] = event
        heads[chain] = (event.sequence, item.event_hash)
    return _VerifiedAudit(events=events, heads=heads)


def _verify_scope_projection(
    connection: sqlite3.Connection,
    event: AuditEvent,
) -> None:
    draft = event.draft
    try:
        actual = connection.execute(
            "SELECT role, ordinal, segment_kind, segment_id "
            "FROM audit_scope_index WHERE chain_kind = ? "
            "AND chain_identity = ? AND sequence = ? ORDER BY role, ordinal",
            (draft.chain_kind.value, draft.chain_identity, event.sequence),
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("scope_index_invalid") from error
    if actual != _scope_rows(event):
        raise VerificationError("scope_index_invalid")


def _scope_rows(event: AuditEvent) -> list[tuple[str, int, str | None, str | None]]:
    expected: list[tuple[str, int, str | None, str | None]] = []
    for role, scope in (
        (ScopeRole.SOURCE, event.draft.source_scope),
        (ScopeRole.REQUESTED, event.draft.requested_scope),
        (ScopeRole.TARGET, event.draft.target_scope),
    ):
        if scope is None:
            continue
        if not scope.segments:
            expected.append((role.value, -1, None, None))
        else:
            expected.extend(_scope_segments(role, scope))
    return sorted(expected)


def _scope_segments(
    role: ScopeRole,
    scope: Scope,
) -> list[tuple[str, int, str, str]]:
    return [
        (role.value, ordinal, segment.kind, segment.identifier)
        for ordinal, segment in enumerate(scope.segments)
    ]


def _verify_heads(
    connection: sqlite3.Connection,
    instance_id: UUID,
    audit: _VerifiedAudit,
) -> None:
    try:
        realms = [row[0] for row in connection.execute("SELECT realm_id FROM realms")]
        rows = connection.execute(
            "SELECT chain_kind, chain_identity, last_sequence, last_hash "
            "FROM audit_heads ORDER BY chain_kind, chain_identity"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("audit_head_invalid") from error
    expected_chains = {("instance", str(instance_id))}
    expected_chains.update(("realm", realm) for realm in realms)
    if {(row[0], row[1]) for row in rows} != expected_chains:
        raise VerificationError("audit_head_invalid")
    for chain_kind, chain_identity, sequence, event_hash in rows:
        expected = audit.heads.get((chain_kind, chain_identity), (0, ZERO_HASH))
        if (sequence, event_hash) != expected:
            raise VerificationError("audit_head_invalid")
    if not set(audit.heads).issubset(expected_chains):
        raise VerificationError("audit_chain_invalid")


def _verify_idempotency(
    connection: sqlite3.Connection,
    events: dict[str, AuditEvent],
) -> int:
    try:
        rows = connection.execute(
            "SELECT principal_id, operation, idempotency_key, command_digest, "
            "result_schema, result_bytes, result_digest, mutation_id, "
            "original_event_id, created_at FROM idempotency_records "
            "ORDER BY principal_id, operation, idempotency_key"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("idempotency_invalid") from error
    for row in rows:
        if len(row) != 10:
            raise VerificationError("idempotency_invalid")
        (
            principal_id,
            operation,
            idempotency_key,
            command_digest,
            result_schema,
            result_bytes,
            result_digest,
            mutation_id,
            original_event_id,
            created_at,
        ) = row
        if (
            not _canonical_uuid(principal_id)
            or not _valid_label(operation)
            or not _canonical_idempotency_uuid(idempotency_key)
            or not _digest(command_digest)
            or type(result_schema) is not str
            or type(result_bytes) is not bytes
            or type(result_digest) is not bytes
            or hashlib.sha256(result_bytes).digest() != result_digest
            or not _canonical_uuid(mutation_id)
            or not _canonical_timestamp(created_at)
        ):
            raise VerificationError("idempotency_invalid")
        event = events.get(cast(str, original_event_id))
        if event is None:
            raise VerificationError("idempotency_invalid")
        draft = event.draft
        if (
            str(draft.principal_id) != principal_id
            or str(draft.idempotency_key) != idempotency_key
            or str(draft.mutation_id) != mutation_id
            or draft.command_digest != command_digest
        ):
            raise VerificationError("idempotency_invalid")
        # Proposal recorded results use a flat receipt. Their independent
        # verifier checks the complete canonical result and actual command.
        if not (
            operation in ("memory-propose", "memory-proposal-reject")
            and result_schema == "cairn.proposal.recorded/v1"
        ):
            _verify_result_bytes(result_bytes, mutation_id, command_digest)
    return len(rows)


def _verify_result_bytes(
    result_bytes: bytes,
    mutation_id: str,
    command_digest: bytes,
) -> None:
    try:
        document = json.loads(result_bytes.decode("utf-8", errors="strict"))
        canonical = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise VerificationError("idempotency_invalid") from error
    if canonical != result_bytes or not isinstance(document, dict):
        raise VerificationError("idempotency_invalid")
    receipt = document.get("mutation_receipt")
    if receipt != {
        "command_digest": command_digest.hex(),
        "mutation_id": mutation_id,
    }:
        raise VerificationError("idempotency_invalid")


def _verify_authority(connection: sqlite3.Connection) -> tuple[int, int, int]:
    principals = _verify_principals(connection)
    credentials = _verify_credentials(connection)
    grants = _verify_grants(connection, principals)
    _verify_credential_revocations(connection, credentials)
    _verify_grant_revocations(connection, grants)
    return len(principals), len(credentials), len(grants)


def _verify_principals(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute(
            "SELECT principal_id, kind, label, created_at FROM principals "
            "ORDER BY principal_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("authority_value_invalid") from error
    principals: dict[str, str] = {}
    for principal_id, kind, label, created_at in rows:
        if (
            kind not in ("human", "workload")
            or not _valid_label(label)
            or not _canonical_timestamp(created_at)
        ):
            raise VerificationError("authority_value_invalid")
        principals[cast(str, principal_id)] = cast(str, kind)
    return principals


def _verify_credentials(connection: sqlite3.Connection) -> dict[str, str]:
    """A credential row's values.

    ``principal_id`` is selected here and nowhere else in this phase. It is
    the column ``cairn.authority.credentials.authenticate`` and
    ``gate._stored_credential_uuid`` both refuse as
    ``credential_uuid_malformed``, and from slice 5 it sits on the
    authentication path of every request — so without this an operator whose
    every request fails would run ``cairn verify`` and be told the catalogue
    is clean, which is finding A's argument one table over.

    Deliberately not added to ``_verify_principals``. ``principals.principal_id``
    is the parent of five foreign keys — ``credentials``, ``grants``
    (twice), ``assertions``, ``facts.promoted_by`` and
    ``fact_invalidations`` — and ``PRAGMA foreign_key_check`` has already
    proven every child value equals a parent value. Validating it there would
    therefore make all five of those downstream checks unreachable, turning
    proven checks into dead code and their tamper cases into tests that pass
    for a different reason. Measured, not assumed: see the fix-wave report.
    A principal referenced by nothing is the only row that check would add,
    and no command ever reads one, so it cannot fail while verify says clean.
    """
    try:
        rows = connection.execute(
            "SELECT credential_id, principal_id, verifier, created_at, expires_at "
            "FROM credentials ORDER BY credential_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("authority_value_invalid") from error
    credentials: dict[str, str] = {}
    for credential_id, principal_id, verifier, created_at, expires_at in rows:
        if type(verifier) is not bytes or len(verifier) != 32:
            raise VerificationError("authority_verifier_invalid")
        # ck_credentials_credential_id is the same shape-only GLOB the grant
        # columns carry; principal_id has no CHECK of its own at all, only
        # the foreign key.
        if not _canonical_uuid(credential_id) or not _canonical_uuid(principal_id):
            raise VerificationError("authority_value_invalid")
        if not _canonical_timestamp(created_at) or (
            expires_at is not None and not _canonical_timestamp(expires_at)
        ):
            raise VerificationError("authority_value_invalid")
        credentials[cast(str, credential_id)] = cast(str, created_at)
    return credentials


def _verify_grants(
    connection: sqlite3.Connection, principals: dict[str, str]
) -> dict[str, str]:
    """A grant row's values, checked against what ``gate._row_to_grant``
    refuses at read time.

    Those three reading guards — ``_stored_grant_uuid``,
    ``_stored_grant_operations``/``_stored_grant_classifications`` and
    ``_stored_grant_segments`` — each exist because a ``grants`` CHECK
    constrains shape without constraining meaning, and each names this phase
    as the detector an operator is meant to reach for. Without the same three
    checks here, a catalogue holding such a row fails every authorising
    command with ``invalid_request`` while ``cairn verify`` reports it clean,
    which is the one answer verification must never give.
    """
    try:
        rows = connection.execute(
            "SELECT grant_id, principal_id, scope_segments, operations, "
            "read_clearance, write_classifications, delegable_operations, "
            "issued_by, expires_at, created_at FROM grants ORDER BY grant_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("authority_value_invalid") from error
    grants: dict[str, str] = {}
    for (
        grant_id,
        principal_id,
        scope_segments,
        operations,
        read_clearance,
        write_classifications,
        delegable_operations,
        issued_by,
        expires_at,
        created_at,
    ) in rows:
        segments = _canonical_json_array(scope_segments)
        if segments is None:
            raise VerificationError("authority_json_not_canonical")
        # ck_grants_scope_segments bounds the array and its length and says
        # nothing about the elements, so '[1,2,3]' is a row SQLite accepts.
        if not _valid_scope_segments(segments):
            raise VerificationError("authority_value_invalid")
        operation_values = _canonical_string_set(operations)
        classification_values = _canonical_string_set(write_classifications)
        if operation_values is None or classification_values is None:
            raise VerificationError("authority_json_not_canonical")
        delegable_values: list[str] | None = None
        if delegable_operations is not None:
            delegable_values = _canonical_string_set(delegable_operations)
            if delegable_values is None:
                raise VerificationError("authority_json_not_canonical")
        # Canonicality and sortedness are not membership: the three enum-
        # valued JSON columns have no IN-list CHECK to re-derive, so a
        # spelling outside the closed set survives every constraint and only
        # fails when a command reads it.
        if (
            _outside_set(operation_values, _GRANT_OPERATIONS)
            or _outside_set(classification_values, _CLASSIFICATIONS)
            or (
                delegable_values is not None
                and _outside_set(delegable_values, _GRANT_OPERATIONS)
            )
        ):
            raise VerificationError("authority_value_invalid")
        has_grant_manage = "grant-manage" in operation_values
        if has_grant_manage != (delegable_values is not None) or (
            delegable_values is not None and "grant-manage" in delegable_values
        ):
            raise VerificationError("authority_delegable_inconsistent")
        # The three UUID columns: their GLOBs constrain shape only, and the
        # dashes are not pinned to the canonical positions.
        if (
            not _canonical_uuid(grant_id)
            or not _canonical_uuid(principal_id)
            or (issued_by is not None and not _canonical_uuid(issued_by))
        ):
            raise VerificationError("authority_value_invalid")
        if read_clearance not in _CLASSIFICATIONS:
            raise VerificationError("authority_value_invalid")
        if not _canonical_timestamp(created_at) or (
            expires_at is not None and not _canonical_timestamp(expires_at)
        ):
            raise VerificationError("authority_value_invalid")
        if principals.get(cast(str, principal_id)) == "workload" and expires_at is None:
            raise VerificationError("authority_workload_expiry_missing")
        grants[cast(str, grant_id)] = cast(str, created_at)
    return grants


def _outside_set(values: list[str], allowed: tuple[str, ...]) -> bool:
    return any(value not in allowed for value in values)


def _verify_credential_revocations(
    connection: sqlite3.Connection, credentials: dict[str, str]
) -> None:
    try:
        rows = connection.execute(
            "SELECT credential_id, revoked_at FROM credential_revocations"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("authority_value_invalid") from error
    for credential_id, revoked_at in rows:
        if not _canonical_timestamp(revoked_at):
            raise VerificationError("authority_value_invalid")
        created_at = credentials.get(cast(str, credential_id))
        if created_at is not None and cast(str, revoked_at) < created_at:
            raise VerificationError("authority_revocation_inconsistent")


def _verify_grant_revocations(
    connection: sqlite3.Connection, grants: dict[str, str]
) -> None:
    try:
        rows = connection.execute(
            "SELECT grant_id, revoked_at FROM grant_revocations"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("authority_value_invalid") from error
    for grant_id, revoked_at in rows:
        if not _canonical_timestamp(revoked_at):
            raise VerificationError("authority_value_invalid")
        created_at = grants.get(cast(str, grant_id))
        if created_at is not None and cast(str, revoked_at) < created_at:
            raise VerificationError("authority_revocation_inconsistent")


def _verify_custody(
    connection: sqlite3.Connection,
    events: dict[str, AuditEvent],
) -> _CustodyInventory:
    """Custody content: assertions, facts, invalidations, evidence and the two
    outbox queues.

    The order is a dependency order, not a preference. Evidence records name
    assertions, facts name both, invalidations name facts, and the outbox rows
    name evidence and facts — so each phase returns the realms and values the
    next one needs to cross-check against, and a reference can only ever be
    judged against a row already verified.

    What this phase deliberately does not do is re-derive what
    ``PRAGMA foreign_key_check`` has already proven in ``_verify_sqlite``. An
    invalidation for a missing fact, an orphan outbox row and a
    ``superseded_by`` naming no fact are all reported there as
    ``foreign_key_failed``; a second check of the same thing here could not be
    proven by deleting it, because the foreign-key phase would fail the test
    either way. Every reference check below is therefore about agreement the
    schema cannot express — realm agreement, digest agreement, and a
    ``mutation_id`` with no foreign key by design.
    """
    assertions = _verify_assertions(connection)
    evidence = _verify_evidence_records(connection, assertions)
    facts = _verify_facts(connection, assertions, evidence)
    invalidations = _verify_invalidations(connection, facts)
    mutations = _audit_mutation_ids(events)
    return _CustodyInventory(
        assertion_count=len(assertions),
        fact_count=len(facts),
        invalidation_count=invalidations,
        evidence_count=len(evidence),
        evidence_outbox_depth=_verify_evidence_outbox(connection, evidence, mutations),
        projection_outbox_depth=_verify_projection_outbox(connection, mutations),
    )


def _audit_mutation_ids(events: dict[str, AuditEvent]) -> set[str]:
    """Every mutation identity the verified audit chain attests to (P-17).

    The outbox tables carry ``mutation_id`` without a foreign key precisely so
    that a row whose mutation never happened is detectable rather than
    structurally impossible. These identities come from events already parsed
    and hash-chained by ``_verify_audit``, so membership here also settles the
    column's form: a value that is not a canonical UUID cannot be in this set.
    """
    return {
        str(event.draft.mutation_id)
        for event in events.values()
        if event.draft.mutation_id is not None
    }


def _verify_assertions(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute(
            "SELECT assertion_id, realm_id, CAST(scope_segments AS BLOB), "
            "classification, source_type, principal_id, observed_at, "
            "CAST(metadata AS BLOB), recorded_at FROM assertions "
            "ORDER BY assertion_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    assertions: dict[str, str] = {}
    for (
        assertion_id,
        realm_id,
        scope_segments,
        classification,
        source_type,
        principal_id,
        observed_at,
        metadata,
        recorded_at,
    ) in rows:
        _verify_scope_segments(scope_segments)
        if metadata is not None and not _canonical_json_column(metadata):
            raise VerificationError("custody_json_not_canonical")
        if (
            not _canonical_uuid(assertion_id)
            # principals.principal_id is validated nowhere else: _verify_
            # principals reads the row but never its identity, so a
            # malformed-but-schema-legal one would otherwise survive here.
            or not _canonical_uuid(principal_id)
            or classification not in ("public", "internal", "restricted")
            or source_type not in ("agent-claim", "verified-check", "human")
            or not _canonical_timestamp(recorded_at)
            or (observed_at is not None and not _canonical_timestamp(observed_at))
        ):
            raise VerificationError("custody_value_invalid")
        assertions[cast(str, assertion_id)] = cast(str, realm_id)
    return assertions


def _verify_evidence_records(
    connection: sqlite3.Connection, assertions: dict[str, str]
) -> dict[str, _StoredEvidence]:
    try:
        rows = connection.execute(
            "SELECT evidence_id, realm_id, CAST(scope_segments AS BLOB), "
            "classification, payload_digest, assertion_id, payload_length, "
            "length(CAST(external_uri AS BLOB)), recorded_at "
            "FROM evidence_records ORDER BY evidence_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    evidence: dict[str, _StoredEvidence] = {}
    for (
        evidence_id,
        realm_id,
        scope_segments,
        classification,
        payload_digest,
        assertion_id,
        payload_length,
        uri_length,
        recorded_at,
    ) in rows:
        _verify_scope_segments(scope_segments)
        # The URI itself is never read, only its octet length: it is not the
        # verifier's to disclose, and length() answers the only question this
        # phase asks of it. Its own CHECK excludes whitespace and control
        # characters, so unlike the canonical-JSON columns above there is no
        # undecodable-byte gap left behind by reading it this way.
        exact = (
            assertion_id is not None
            and payload_length is not None
            and uri_length is None
        )
        external = (
            assertion_id is None and payload_length is None and uri_length is not None
        )
        if exact == external:
            raise VerificationError("custody_custody_form_invalid")
        if (
            not _canonical_uuid(evidence_id)
            or not _digest(payload_digest)
            or classification not in ("public", "internal", "restricted")
            or not _canonical_timestamp(recorded_at)
            or (exact and not 1 <= cast(int, payload_length) <= 1048576)
            or (external and not 1 <= cast(int, uri_length) <= 2048)
        ):
            raise VerificationError("custody_value_invalid")
        if _realm_disagrees(assertions.get(cast(str, assertion_id)), realm_id):
            raise VerificationError("custody_reference_invalid")
        evidence[cast(str, evidence_id)] = _StoredEvidence(
            realm_id=cast(str, realm_id),
            payload_digest=cast(bytes, payload_digest),
            payload_length=cast("int | None", payload_length),
        )
    return evidence


def _verify_facts(
    connection: sqlite3.Connection,
    assertions: dict[str, str],
    evidence: dict[str, _StoredEvidence],
) -> dict[str, _StoredFact]:
    try:
        rows = connection.execute(
            "SELECT fact_id, realm_id, CAST(scope_segments AS BLOB), "
            "length(CAST(body AS BLOB)), trust, classification, assertion_id, "
            "derived_from, promoted_by, evidence_id, valid_from, valid_to, "
            "recorded_at FROM facts ORDER BY fact_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    facts: dict[str, _StoredFact] = {}
    lineage: list[tuple[str, str]] = []
    for (
        fact_id,
        realm_id,
        scope_segments,
        body_length,
        trust,
        classification,
        assertion_id,
        derived_from,
        promoted_by,
        evidence_id,
        valid_from,
        valid_to,
        recorded_at,
    ) in rows:
        _verify_scope_segments(scope_segments)
        # The body is never selected, only measured. Verification must not
        # handle fact bodies, and a TEXT column can hold octets sqlite3
        # refuses to decode, which would fail the read before any check.
        ingested = (
            assertion_id is not None
            and derived_from is None
            and promoted_by is None
            and evidence_id is None
        )
        promoted = (
            assertion_id is None
            and derived_from is not None
            and promoted_by is not None
            and evidence_id is not None
        )
        if ingested == promoted:
            raise VerificationError("custody_provenance_invalid")
        if (
            not _canonical_uuid(fact_id)
            or (promoted and not _canonical_uuid(promoted_by))
            or not 1 <= cast(int, body_length) <= 65536
            or trust not in ("candidate", "validated", "failed-approach")
            or classification not in ("public", "internal", "restricted")
            or not _canonical_timestamp(recorded_at)
            or (valid_from is not None and not _canonical_timestamp(valid_from))
            or (valid_to is not None and not _canonical_timestamp(valid_to))
        ):
            raise VerificationError("custody_value_invalid")
        if (
            valid_from is not None
            and valid_to is not None
            and cast(str, valid_from) >= cast(str, valid_to)
        ):
            raise VerificationError("custody_temporal_invalid")
        if _realm_disagrees(assertions.get(cast(str, assertion_id)), realm_id):
            raise VerificationError("custody_reference_invalid")
        if _realm_disagrees(_evidence_realm(evidence, evidence_id), realm_id):
            raise VerificationError("custody_reference_invalid")
        if promoted:
            lineage.append((cast(str, derived_from), cast(str, realm_id)))
        facts[cast(str, fact_id)] = _StoredFact(
            realm_id=cast(str, realm_id),
            recorded_at=cast(str, recorded_at),
        )

    # Deferred: a source may sort after the fact derived from it, so realm
    # agreement along the lineage can only be judged once every fact is known.
    for derived_from, realm_id in lineage:
        source = facts.get(derived_from)
        if source is not None and source.realm_id != realm_id:
            raise VerificationError("custody_reference_invalid")
    return facts


def _verify_invalidations(
    connection: sqlite3.Connection, facts: dict[str, _StoredFact]
) -> int:
    try:
        rows = connection.execute(
            "SELECT fact_id, invalidated_at, principal_id, superseded_by, "
            "length(CAST(reason AS BLOB)) FROM fact_invalidations ORDER BY fact_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    for fact_id, invalidated_at, principal_id, superseded_by, reason_length in rows:
        if (
            not _canonical_uuid(principal_id)
            or not _canonical_timestamp(invalidated_at)
            or not 1 <= cast(int, reason_length) <= 4096
        ):
            raise VerificationError("custody_value_invalid")
        fact = facts.get(cast(str, fact_id))
        if fact is None:
            continue
        # Canonical timestamps sort lexicographically, which is only sound
        # because the canonicality of both has just been established.
        if cast(str, invalidated_at) < fact.recorded_at:
            raise VerificationError("custody_temporal_invalid")
        superseded = (
            None if superseded_by is None else facts.get(cast(str, superseded_by))
        )
        if superseded is not None and superseded.realm_id != fact.realm_id:
            raise VerificationError("custody_reference_invalid")
    return len(rows)


def _verify_evidence_outbox(
    connection: sqlite3.Connection,
    evidence: dict[str, _StoredEvidence],
    mutations: set[str],
) -> int:
    depth = 0
    try:
        # Iterated rather than fetched whole: this is the one query that reads
        # payload bytes, up to 1 MiB a row, and an operator running verify
        # against a backlogged queue must not need the whole queue in memory
        # at once to find out that it is backlogged.
        for (
            work_id,
            kind,
            evidence_id,
            mutation_id,
            payload,
            created_at,
            attempts,
            last_attempt_at,
        ) in connection.execute(
            "SELECT work_id, kind, evidence_id, mutation_id, payload, created_at, "
            "attempts, last_attempt_at FROM evidence_outbox ORDER BY work_id"
        ):
            depth += 1
            _verify_outbox_row(
                work_id=work_id,
                mutation_id=mutation_id,
                created_at=created_at,
                attempts=attempts,
                last_attempt_at=last_attempt_at,
                mutations=mutations,
            )
            if kind != "store-payload":
                raise VerificationError("custody_value_invalid")
            record = evidence.get(cast(str, evidence_id))
            # payload is cast rather than type-checked: STRICT refuses a
            # non-BLOB value in this column even with CHECK enforcement
            # lifted, so a guard here would be a branch no tamper could reach
            # and no deletion mutant could kill.
            #
            # An external-custody record has no payload_length at all, so a
            # payload queued against one fails here rather than needing a rule
            # of its own: the queue exists only to carry exact bytes.
            bytes_payload = cast(bytes, payload)
            if record is not None and (
                record.payload_length != len(bytes_payload)
                or record.payload_digest != hashlib.sha256(bytes_payload).digest()
            ):
                raise VerificationError("custody_outbox_invalid")
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    return depth


def _verify_projection_outbox(
    connection: sqlite3.Connection, mutations: set[str]
) -> int:
    try:
        rows = connection.execute(
            "SELECT work_id, kind, mutation_id, created_at, attempts, "
            "last_attempt_at FROM projection_outbox ORDER BY work_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise VerificationError("custody_value_invalid") from error
    for work_id, kind, mutation_id, created_at, attempts, last_attempt_at in rows:
        if kind not in (
            "fact-ingested",
            "fact-promoted",
            "fact-invalidated",
            "fact-rebuild",
        ):
            raise VerificationError("custody_value_invalid")
        _verify_outbox_row(
            work_id=work_id,
            mutation_id=mutation_id,
            created_at=created_at,
            attempts=attempts,
            last_attempt_at=last_attempt_at,
            mutations=mutations,
            # I-68, amended: a rebuild row descends from no mutation, so
            # there is no audit event whose mutation identity it could
            # match. The null is the row's assertion that it has none, and
            # is checked here rather than trusted from the CHECK — this
            # phase exists to disbelieve the schema.
            expect_mutation=kind != "fact-rebuild",
        )
    return len(rows)


def _verify_outbox_row(
    *,
    work_id: object,
    mutation_id: object,
    created_at: object,
    attempts: object,
    last_attempt_at: object,
    mutations: set[str],
    expect_mutation: bool = True,
) -> None:
    """The columns both queues share (P-17), checked identically for each.

    ``expect_mutation`` is false only for the projection outbox's
    ``fact-rebuild`` rows, which have no mutation to name; every evidence
    row and every custody projection row still has to match an audited
    mutation identity.
    """
    if (
        not _canonical_uuid(work_id)
        or not _canonical_timestamp(created_at)
        or (last_attempt_at is not None and not _canonical_timestamp(last_attempt_at))
    ):
        raise VerificationError("custody_value_invalid")
    if cast(int, attempts) < 0:
        raise VerificationError("custody_outbox_invalid")
    if expect_mutation:
        if mutation_id not in mutations:
            raise VerificationError("custody_outbox_invalid")
    elif mutation_id is not None:
        raise VerificationError("custody_outbox_invalid")


def _realm_disagrees(referenced_realm: str | None, realm_id: object) -> bool:
    """Whether a custody reference crosses a realm boundary.

    Absence is not disagreement, and is deliberately not reported here: a
    reference naming no row has already failed ``PRAGMA foreign_key_check``
    in ``_verify_sqlite``, the same division ``_verify_credential_revocations``
    draws for a missing credential. What this phase adds is the part no
    foreign key can express — that the row it names is in the *same realm*.
    Every writer enforces it (promotion refuses a cross-realm target as
    ``target_not_ancestor``, invalidation refuses a cross-realm
    ``superseded_by`` as ``superseded_by_unknown``) and nothing durable does,
    which is exactly the gap scope isolation cannot afford.
    """
    return referenced_realm is not None and referenced_realm != realm_id


def _evidence_realm(
    evidence: dict[str, _StoredEvidence], evidence_id: object
) -> str | None:
    record = None if evidence_id is None else evidence.get(cast(str, evidence_id))
    return None if record is None else record.realm_id


def _verify_scope_segments(value: object) -> None:
    """A custody ``scope_segments`` column, read as raw octets.

    ``json_valid()`` accepts an invalid UTF-8 byte inside a JSON string
    literal, so a value every CHECK admits can still be octets sqlite3
    refuses to decode — selecting the column as TEXT raises inside the driver
    before any check of ours could run. Reading the octets keeps that
    corruption here, where it is a canonicality failure like any other.

    The element rule itself lives in ``_valid_scope_segments``: the authority
    phase needs the identical rule on ``grants.scope_segments`` and only the
    code differs, so it is stated once.
    """
    if not _canonical_json_column(value):
        raise VerificationError("custody_json_not_canonical")
    documents = json.loads(cast(bytes, value))
    if type(documents) is not list or not _valid_scope_segments(documents):
        raise VerificationError("custody_value_invalid")


def _valid_scope_segments(documents: list[object]) -> bool:
    """Whether a decoded ``scope_segments`` array is a scope path.

    Canonical JSON is necessary but not sufficient: ``[1,2,3]`` is perfectly
    canonical and no segment path at all. ``cairn.authority.mutations.
    _stored_scope``, ``cairn.authority.gate._stored_grant_segments`` and
    ``cairn.evidence.reconciliation._stored_segments`` all refuse such a row
    at read time and all name this phase as the authoritative detector, so the
    rule is reconstructed here from the same ``ScopeSegment`` they use rather
    than restated as a fourth copy.
    """
    if len(documents) > _MAX_SCOPE_SEGMENTS:
        return False
    for document in documents:
        # The key set is checked before the values are read, so a segment
        # missing ``id`` is refused here rather than raising KeyError below.
        if type(document) is not dict or set(document) != {"kind", "id"}:
            return False
        try:
            ScopeSegment(kind=document["kind"], identifier=document["id"])
        except AuditValueError:
            return False
    return True


def _canonical_json_column(value: object) -> bool:
    """Whether a column's raw octets are exactly what ``json.dumps`` canonical
    settings produce. Takes bytes, not text, for the decoding reason above."""
    if type(value) is not bytes:
        return False
    try:
        text = value.decode("utf-8", errors="strict")
        canonical = json.dumps(
            json.loads(text),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return canonical == text


def _canonical_json_array(value: object) -> list[object] | None:
    if type(value) is not str:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    canonical = json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical != value or type(parsed) is not list:
        return None
    return cast(list[object], parsed)


def _canonical_string_set(value: object) -> list[str] | None:
    parsed = _canonical_json_array(value)
    if parsed is None or any(type(item) is not str for item in parsed):
        return None
    strings = cast(list[str], parsed)
    if strings != sorted(strings):
        return None
    return strings


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str or not value.isascii():
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and parsed.variant == RFC_4122 and str(parsed) == value


def _canonical_idempotency_uuid(value: object) -> bool:
    if type(value) is not str or not value.isascii():
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.variant == RFC_4122 and str(parsed) == value


def _canonical_timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 27 or not value.endswith("Z"):
        return False
    try:
        parsed = parse_timestamp(value)
    except CatalogueStorageError:
        return False
    return canonical_timestamp(parsed) == value


def _valid_realm(value: object) -> bool:
    return _valid_label(value) and len(cast(str, value)) <= 63


def _valid_label(value: object) -> bool:
    if type(value) is not str or not value or not value.isascii():
        return False
    return (
        value[0].islower()
        and value[0].isalpha()
        and value[-1].isalnum()
        and value == value.lower()
        and all(character.isalnum() or character == "-" for character in value)
    )


def _digest(value: object) -> bool:
    return type(value) is bytes and len(value) == 32


def _rebuild_scope_index(config: CairnConfig) -> VerificationReport:
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    try:
        lease.acquire()
        with _open_write_connection(config.paths.data, create=False) as connection:
            report = _verify_connection(
                connection,
                config.instance_id,
                verify_scope_index=False,
            )
            events = connection.execute(
                "SELECT canonical_event FROM audit_events "
                "ORDER BY chain_kind, chain_identity, sequence"
            ).fetchall()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM audit_scope_index")
                for (canonical_event,) in events:
                    event = parse_canonical_audit_bytes(canonical_event)
                    coordinates = (
                        event.draft.chain_kind.value,
                        event.draft.chain_identity,
                        event.sequence,
                    )
                    connection.executemany(
                        "INSERT INTO audit_scope_index (chain_kind, "
                        "chain_identity, sequence, role, ordinal, segment_kind, "
                        "segment_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(*coordinates, *row) for row in _scope_rows(event)],
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return report
    except VerificationError:
        raise
    except (CatalogueStorageError, LeaseError, sqlite3.Error) as error:
        raise VerificationError("catalogue_unavailable") from error
    finally:
        try:
            lease.release()
        except LeaseError as error:
            raise VerificationError("catalogue_unavailable") from error


def _verify_memory(
    connection: sqlite3.Connection, events: dict[str, AuditEvent]
) -> None:
    """Reconcile immutable opinions against facts, evidence and their audited receipt."""
    mutations = {
        str(event.draft.mutation_id): event
        for event in events.values()
        if event.draft.outcome.value == "allow" and event.draft.mutation_id is not None
    }
    links: dict[str, tuple[str, str, str, str, str, str]] = {}
    for row in connection.execute(
        "SELECT relationship_id, realm_id, scope_segments, left_fact_id, right_fact_id, classification, principal_id, reason, recorded_at, mutation_id FROM memory_disagreements"
    ):
        (
            identity,
            realm,
            segments,
            left,
            right,
            classification,
            principal,
            reason,
            recorded,
            mutation,
        ) = cast(tuple[str, str, str, str, str, str, str, str, str, str], row)
        _verify_memory_value(
            identity, segments, classification, principal, reason, recorded, mutation
        )
        if left == right:
            raise VerificationError("memory_reference_invalid")
        for endpoint in (left, right):
            fact = connection.execute(
                "SELECT realm_id, scope_segments, classification, recorded_at FROM facts WHERE fact_id = ?",
                (endpoint,),
            ).fetchone()
            if (
                fact is None
                or fact[0] != realm
                or fact[1] != segments
                or _CLASSIFICATIONS.index(fact[2])
                > _CLASSIFICATIONS.index(classification)
                or fact[3] > recorded
            ):
                raise VerificationError("memory_reference_invalid")
        _verify_memory_receipt(
            connection,
            mutations,
            identity,
            mutation,
            principal,
            realm,
            segments,
            recorded,
            "memory-disagree",
            (left, right),
        )
        links[identity] = (realm, segments, left, right, classification, recorded)
    for row in connection.execute(
        "SELECT relationship_id, disagreement_id, evidence_id, selected_fact_id, classification, principal_id, reason, recorded_at, mutation_id FROM memory_resolutions"
    ):
        (
            identity,
            disagreement,
            evidence,
            selected,
            classification,
            principal,
            reason,
            recorded,
            mutation,
        ) = cast(tuple[str, str, str, str | None, str, str, str, str, str], row)
        link = links.get(disagreement)
        if link is None:
            raise VerificationError("memory_reference_invalid")
        realm, segments, left, right, link_classification, link_recorded = link
        _verify_memory_value(
            identity, segments, classification, principal, reason, recorded, mutation
        )
        if (
            selected not in (None, left, right)
            or _CLASSIFICATIONS.index(classification)
            < _CLASSIFICATIONS.index(link_classification)
            or recorded < link_recorded
        ):
            raise VerificationError("memory_reference_invalid")
        evidence_row = connection.execute(
            "SELECT realm_id, scope_segments, classification, recorded_at FROM evidence_records WHERE evidence_id = ?",
            (evidence,),
        ).fetchone()
        if evidence_row is None:
            raise VerificationError("memory_reference_invalid")
        evidence_segments = json.loads(evidence_row[1])
        if (
            evidence_row[0] != realm
            or json.loads(segments)[: len(evidence_segments)] != evidence_segments
            or _CLASSIFICATIONS.index(evidence_row[2])
            > _CLASSIFICATIONS.index(classification)
            or evidence_row[3] > recorded
        ):
            raise VerificationError("memory_reference_invalid")
        _verify_memory_receipt(
            connection,
            mutations,
            identity,
            mutation,
            principal,
            realm,
            segments,
            recorded,
            "memory-resolve",
            (left, right),
        )


def _verify_memory_value(
    identity: str,
    segments: str,
    classification: str,
    principal: str,
    reason: str,
    recorded: str,
    mutation: str,
) -> None:
    if (
        not all(_canonical_uuid(value) for value in (identity, principal, mutation))
        or not _canonical_timestamp(recorded)
        or classification not in _CLASSIFICATIONS
        or not 1 <= len(reason.encode()) <= 4096
    ):
        raise VerificationError("memory_value_invalid")
    _verify_scope_segments(segments.encode())


def _verify_memory_receipt(
    connection: sqlite3.Connection,
    mutations: dict[str, AuditEvent],
    identity: str,
    mutation: str,
    principal: str,
    realm: str,
    segments: str,
    recorded: str,
    operation: str,
    endpoints: tuple[str, str],
) -> None:
    event = mutations.get(mutation)
    if event is None:
        raise VerificationError("memory_audit_invalid")
    draft = event.draft
    requested = draft.requested_scope
    if (
        draft.action_code != operation
        or str(draft.principal_id) != principal
        or requested is None
        or requested.realm != realm
        or json.loads(segments)
        != [{"kind": s.kind, "id": s.identifier} for s in requested.segments]
        or canonical_timestamp(event.recorded_at) < recorded
        or tuple(sorted(endpoints)) != tuple(str(f) for f in draft.affected_fact_ids)
    ):
        raise VerificationError("memory_audit_invalid")
    result = connection.execute(
        "SELECT operation, result_bytes FROM idempotency_records WHERE mutation_id = ?",
        (mutation,),
    ).fetchall()
    if (
        len(result) != 1
        or result[0][0] != operation
        or json.loads(result[0][1]).get("relationship_id") != identity
    ):
        raise VerificationError("memory_audit_invalid")
