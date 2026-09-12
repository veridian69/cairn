import hashlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import cairn.administration.commands as commands_module
from cairn.administration.commands import (
    Actor,
    CairnAdministration,
    CreateGrant,
    CreatePrincipal,
    CredentialIssued,
    CredentialRevoked,
    GrantCreated,
    GrantRevoked,
    IssueCredential,
    PlaintextUnavailable,
    PrincipalCreated,
    RevokeCredential,
    RevokeGrant,
)
from cairn.authority.credentials import (
    DATA_OPERATIONS,
    TOKEN_PATTERN,
    GrantOperation,
    PrincipalKind,
)
from cairn.authority.grants import ProposedGrant
from cairn.catalogue.audit import Classification, ScopeSegment
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME, _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    FailureCode,
    FailureDetail,
    Rejected,
    Replayed,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.screening import POLICY_VERSION, SecretScreen

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_REALM = "acme"
_OTHER_REALM = "other-realm"

_MANAGER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGER_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_OUTSIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OUTSIDER_CREDENTIAL_ID = UUID("bbbbbbbb-cccc-4bbb-8bbb-bbbbbbbbbbbb")
_TARGET_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_TARGET_CREDENTIAL_ID = UUID("cccccccc-dddd-4ccc-8ccc-cccccccccccc")
_WORKLOAD_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_MANAGER_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_SCOPED_MANAGER_GRANT_ID = UUID("eeeeeeee-2222-4eee-8eee-eeeeeeeeeeee")
_TARGET_GRANT_ID = UUID("ffffffff-1111-4fff-8fff-ffffffffffff")

_REPO = ScopeSegment(kind="repository", identifier="acme-repo")

_IDEMPOTENCY_KEY = UUID("77777777-7777-4777-8777-777777777777")
_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=_INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _uuid_seq(start: int) -> Callable[[], UUID]:
    counter = itertools.count(start)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def _fixed_entropy(value: int = 7) -> Callable[[int], bytes]:
    return lambda size: bytes([value]) * size


def _administration(
    data_path: Path,
    *,
    now: datetime = _NOW,
    uuid_factory: Callable[[], UUID] | None = None,
    entropy: Callable[[int], bytes] | None = None,
) -> CairnAdministration:
    transactions = CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: now,
        uuid_factory=_uuid_seq(0x10000000),
    )
    return CairnAdministration(
        data_path,
        transactions,
        clock=lambda: now,
        uuid_factory=uuid_factory or _uuid_seq(0x20000000),
        entropy=entropy or _fixed_entropy(),
        screen=SecretScreen(),
    )


def _add_realm(data_path: Path, realm_id: str) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (realm_id, _TS)
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (realm_id, bytes(32)),
        )
        connection.commit()


def _seed_catalogue(data_path: Path, *, realms: tuple[str, ...] = (_REALM,)) -> None:
    migrate_catalogue(_config(data_path), lambda: _NOW)
    for realm_id in realms:
        _add_realm(data_path, realm_id)


def _insert_principal(
    data_path: Path,
    principal_id: UUID,
    *,
    kind: PrincipalKind = PrincipalKind.HUMAN,
    label: str,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(principal_id), kind.value, label, _TS),
        )
        connection.commit()


def _insert_credential(
    data_path: Path,
    credential_id: UUID,
    principal_id: UUID,
    *,
    verifier: bytes = hashlib.sha256(b"fixture-secret").digest(),
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(credential_id), str(principal_id), verifier, _TS),
        )
        connection.commit()


def _segments_json(segments: tuple[ScopeSegment, ...]) -> str:
    return json.dumps(
        [{"id": s.identifier, "kind": s.kind} for s in segments],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _set_json(values: frozenset[str]) -> str:
    return json.dumps(
        sorted(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _insert_grant(
    data_path: Path,
    *,
    grant_id: UUID,
    principal_id: UUID,
    realm_id: str = _REALM,
    segments: tuple[ScopeSegment, ...] = (),
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.GRANT_MANAGE}),
    read_clearance: Classification = Classification.RESTRICTED,
    write_classifications: frozenset[Classification] = frozenset(
        {Classification.PUBLIC, Classification.INTERNAL, Classification.RESTRICTED}
    ),
    delegable_operations: frozenset[GrantOperation] | None = frozenset(
        DATA_OPERATIONS | {GrantOperation.AUDIT_READ}
    ),
    issued_by: UUID | None = None,
    expires_at: str | None = None,
    created_at: str = _TS,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(grant_id),
                str(principal_id),
                realm_id,
                _segments_json(segments),
                _set_json(frozenset(op.value for op in operations)),
                read_clearance.value,
                _set_json(frozenset(c.value for c in write_classifications)),
                None
                if delegable_operations is None
                else _set_json(frozenset(op.value for op in delegable_operations)),
                None if issued_by is None else str(issued_by),
                expires_at,
                created_at,
            ),
        )
        connection.commit()


def _revoke_grant_row(data_path: Path, grant_id: UUID) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grant_revocations "
            "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
            (str(grant_id), _TS, None, "superseded"),
        )
        connection.commit()


def _revoke_credential_row(data_path: Path, credential_id: UUID) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credential_revocations "
            "(credential_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
            (str(credential_id), _TS, None, "superseded"),
        )
        connection.commit()


def _seed_manager(data_path: Path, *, realm_id: str = _REALM) -> None:
    """A principal with a live realm-root grant-manage grant in realm_id."""
    _insert_principal(data_path, _MANAGER_ID, label="manager")
    _insert_credential(data_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_grant(
        data_path,
        grant_id=_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=realm_id,
    )


def _seed_scoped_manager(
    data_path: Path,
    *,
    segments: tuple[ScopeSegment, ...] = (_REPO,),
    delegable_operations: frozenset[GrantOperation] = frozenset(
        {GrantOperation.RETRIEVE}
    ),
) -> None:
    """A principal with a live grant-manage grant scoped to ``segments`` only
    (not realm-root), so widening beyond that envelope is rejectable."""
    _insert_principal(data_path, _MANAGER_ID, label="manager")
    _insert_credential(data_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_grant(
        data_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=segments,
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        delegable_operations=delegable_operations,
    )


def _seed_outsider(data_path: Path) -> None:
    """A principal with no grants anywhere."""
    _insert_principal(data_path, _OUTSIDER_ID, label="outsider")
    _insert_credential(data_path, _OUTSIDER_CREDENTIAL_ID, _OUTSIDER_ID)


def _manager_actor() -> Actor:
    return Actor(principal_id=_MANAGER_ID, credential_id=_MANAGER_CREDENTIAL_ID)


def _outsider_actor() -> Actor:
    return Actor(principal_id=_OUTSIDER_ID, credential_id=_OUTSIDER_CREDENTIAL_ID)


def _catalogue_bytes(data_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(data_path.iterdir())
        if path.is_file() and path.name.startswith(CATALOGUE_FILENAME)
    )


def _audit_rows(data_path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return connection.execute(
            "SELECT outcome, reason_code FROM audit_events ORDER BY sequence"
        ).fetchall()
    finally:
        connection.close()


# === Step 1: authorisation ===================================================


def test_create_principal_denied_without_root_grant_manage(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _outsider_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "grant_manage_not_held")]


def test_create_principal_succeeds_for_root_grant_manage_holder(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, PrincipalCreated)
    assert outcome.value.label == "new-op"
    assert outcome.value.kind is PrincipalKind.HUMAN
    assert outcome.value.created_at == _NOW
    assert _audit_rows(tmp_path) == [("allow", "principal_created")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    canonical_event = connection.execute(
        "SELECT canonical_event FROM audit_events"
    ).fetchone()[0]
    document = json.loads(canonical_event)
    assert document["grant_id"] == str(_MANAGER_GRANT_ID)


def test_create_principal_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id="ghost-realm", kind=PrincipalKind.HUMAN, label="x"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_create_principal_duplicate_label_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="manager"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "label_exists")]


def test_denial_audit_reason_is_precise_while_public_failure_stays_coarse(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    admin = _administration(tmp_path)

    denials = [
        admin.create_principal(
            _outsider_actor(),
            CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="a"),
            idempotency_key=UUID("70000000-0000-4000-8000-000000000001"),
            correlation_id=_CORRELATION_ID,
        ),
        admin.create_principal(
            _outsider_actor(),
            CreatePrincipal(realm_id="ghost", kind=PrincipalKind.HUMAN, label="b"),
            idempotency_key=UUID("70000000-0000-4000-8000-000000000002"),
            correlation_id=_CORRELATION_ID,
        ),
    ]

    assert all(isinstance(denial, Rejected) for denial in denials)
    public_codes = {denial.failure.code for denial in denials}  # type: ignore[union-attr]
    assert public_codes == {FailureCode.AUTHORISATION_DENIED, FailureCode.NOT_FOUND}
    reasons = {row[1] for row in _audit_rows(tmp_path)}
    assert reasons == {"grant_manage_not_held", "realm_not_found"}


def test_issue_credential_unknown_principal_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(
            realm_id=_REALM,
            principal_id=UUID("99999999-9999-4999-8999-999999999999"),
            expires_at=None,
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_issue_credential_denied_without_root_grant_manage(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _outsider_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_issue_credential_for_principal_granted_in_uncontrolled_realm_is_denied(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_manager(tmp_path, realm_id=_REALM)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_OTHER_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
    )
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "cross_realm_principal")]


def test_issue_credential_for_principal_granted_only_in_controlled_realm_succeeds(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
    )
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)


def test_issue_credential_for_principal_with_no_live_grants_succeeds(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)


def test_revoke_credential_unknown_credential_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=UUID("99999999-9999-4999-8999-999999999999"),
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_revoke_credential_denied_without_root_grant_manage(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _outsider_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_revoke_credential_for_principal_live_only_in_another_realm_is_denied(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_manager(tmp_path, realm_id=_REALM)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_OTHER_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "cross_realm_principal")]


def test_revoke_credential_for_principal_live_in_named_realm_succeeds(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, CredentialRevoked)


def test_revoke_credential_for_principal_with_no_live_grants_succeeds(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)


def test_create_grant_denied_without_any_grant_manage_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    proposal = ProposedGrant(
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.PUBLIC,
        write_classifications=frozenset(),
        delegable_operations=None,
        expires_at=_NOW,
    )
    outcome = admin.create_grant(
        _outsider_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "grant_manage_not_held")]


def test_create_grant_denied_when_manage_grants_envelope_does_not_cover(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_grant(
        tmp_path,
        grant_id=_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
    )
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    proposal = ProposedGrant(
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        segments=(),
        operations=frozenset({GrantOperation.PROMOTE}),
        read_clearance=Classification.RESTRICTED,
        write_classifications=frozenset(),
        delegable_operations=None,
        expires_at=_NOW,
    )
    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "operation_outside_envelope")]


def test_create_grant_unknown_principal_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    proposal = ProposedGrant(
        principal_id=UUID("99999999-9999-4999-8999-999999999999"),
        realm_id=_REALM,
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.PUBLIC,
        write_classifications=frozenset(),
        delegable_operations=None,
        expires_at=_NOW,
    )
    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_create_grant_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    proposal = ProposedGrant(
        principal_id=_MANAGER_ID,
        realm_id="ghost",
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.PUBLIC,
        write_classifications=frozenset(),
        delegable_operations=None,
        expires_at=_NOW,
    )
    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id="ghost", grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_revoke_grant_unknown_grant_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM,
            grant_id=UUID("99999999-9999-4999-8999-999999999999"),
            reason_code="superseded",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_revoke_grant_denied_for_non_issuer_non_root_holder(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_TARGET_ID,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _outsider_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


# === Step 2: plaintext custody ===============================================


def test_committed_credential_carries_a_single_matching_token(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, CredentialIssued)
    assert isinstance(outcome.value.plaintext, str)
    matches = TOKEN_PATTERN.findall(outcome.value.plaintext)
    assert len(matches) == 1
    assert TOKEN_PATTERN.fullmatch(outcome.value.plaintext) is not None


def test_plaintext_secret_never_touches_stored_bytes(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, CredentialIssued)
    secret = outcome.value.plaintext
    assert isinstance(secret, str)
    secret_component = secret.rsplit(".", 1)[1].encode()

    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        result_bytes = connection.execute(
            "SELECT result_bytes FROM idempotency_records"
        ).fetchone()[0]
        stored_verifier = connection.execute(
            "SELECT verifier FROM credentials WHERE credential_id = ?",
            (str(outcome.value.credential_id),),
        ).fetchone()[0]
    finally:
        connection.close()

    assert secret_component not in result_bytes
    assert stored_verifier == hashlib.sha256(secret_component).digest()
    assert secret_component not in _catalogue_bytes(tmp_path)


def test_replayed_issue_credential_returns_plaintext_unavailable(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)
    command = IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None)

    committed = admin.issue_credential(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    replayed = admin.issue_credential(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(committed, Committed)
    assert isinstance(replayed, Replayed)
    assert isinstance(replayed.value, CredentialIssued)
    assert replayed.value.plaintext == PlaintextUnavailable()
    assert replayed.value.credential_id == committed.value.credential_id
    assert replayed.audit_receipt.event_id != committed.audit_receipt.event_id
    rows = _audit_rows(tmp_path)
    assert rows[-1] == ("allow", "idempotent_replay")


# === Step 3: grant lifecycle GRANT-01..10 ====================================


def _proposal(
    *,
    principal_id: UUID = _WORKLOAD_ID,
    realm_id: str = _REALM,
    segments: tuple[ScopeSegment, ...] = (_REPO,),
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.RETRIEVE}),
    read_clearance: Classification = Classification.INTERNAL,
    write_classifications: frozenset[Classification] = frozenset(
        {Classification.PUBLIC}
    ),
    delegable_operations: frozenset[GrantOperation] | None = None,
    expires_at: datetime | None = _NOW,
) -> ProposedGrant:
    return ProposedGrant(
        principal_id=principal_id,
        realm_id=realm_id,
        segments=segments,
        operations=operations,
        read_clearance=read_clearance,
        write_classifications=write_classifications,
        delegable_operations=delegable_operations,
        expires_at=expires_at,
    )


def test_grant_01_create_grant_within_envelope_succeeds(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, GrantCreated)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT principal_id, issued_by FROM grants WHERE grant_id = ?",
        (str(outcome.value.grant_id),),
    ).fetchone()
    assert row == (str(_WORKLOAD_ID), str(_MANAGER_ID))
    audit_row = connection.execute("SELECT reason_code FROM audit_events").fetchone()
    assert audit_row == ("grant_created",)
    scope_row = connection.execute(
        "SELECT segment_kind, segment_id FROM audit_scope_index WHERE role = 'requested'"
    ).fetchone()
    assert scope_row == ("repository", "acme-repo")


def test_grant_02_scope_widening_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_scoped_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    proposal = _proposal(
        segments=(ScopeSegment(kind="repository", identifier="other-repo"),)
    )

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "scope_outside_envelope")]


def test_grant_03_operation_widening_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_scoped_manager(
        tmp_path, delegable_operations=frozenset({GrantOperation.RETRIEVE})
    )
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    proposal = _proposal(
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.PROMOTE})
    )

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "operation_outside_envelope")]


def test_grant_04_clearance_widening_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    proposal = _proposal(read_clearance=Classification.RESTRICTED)
    _insert_grant(
        tmp_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=(_REPO,),
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        read_clearance=Classification.INTERNAL,
        write_classifications=frozenset({Classification.PUBLIC}),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
    )

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    # The realm-root grant (RESTRICTED clearance) still authorises this.
    assert isinstance(outcome, Committed)


def test_grant_04_clearance_widening_is_rejected_when_only_narrow_manager_exists(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    _insert_grant(
        tmp_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=(_REPO,),
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        read_clearance=Classification.INTERNAL,
        write_classifications=frozenset({Classification.PUBLIC}),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
    )
    admin = _administration(tmp_path)
    proposal = _proposal(read_clearance=Classification.RESTRICTED)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "clearance_exceeds_envelope")]


def test_grant_05_write_classification_widening_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    _insert_grant(
        tmp_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=(),
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        read_clearance=Classification.INTERNAL,
        write_classifications=frozenset(),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
    )
    admin = _administration(tmp_path)
    proposal = _proposal(write_classifications=frozenset({Classification.PUBLIC}))

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "classification_outside_envelope")]


def test_grant_06_later_expiry_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    _insert_grant(
        tmp_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=(),
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        read_clearance=Classification.INTERNAL,
        write_classifications=frozenset({Classification.PUBLIC}),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
        expires_at="2026-08-05T11:00:00.000000Z",
    )
    admin = _administration(tmp_path)
    proposal = _proposal(expires_at=_NOW + timedelta(hours=2))

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "expiry_exceeds_envelope")]


def test_grant_07_grant_manage_delegation_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    proposal = _proposal(operations=frozenset({GrantOperation.GRANT_MANAGE}))

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert _audit_rows(tmp_path) == [("deny", "grant_manage_not_delegable")]


def test_grant_08_issuer_may_revoke_its_issued_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_OUTSIDER_ID,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _outsider_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, GrantRevoked)
    assert _audit_rows(tmp_path) == [("allow", "grant_revoked")]


def test_grant_09_root_manage_holder_may_revoke_any_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_TARGET_ID,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)


def test_grant_10_non_root_holder_may_not_revoke_anothers_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_grant(
        tmp_path,
        grant_id=_SCOPED_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        segments=(_REPO,),
        operations=frozenset({GrantOperation.GRANT_MANAGE}),
        delegable_operations=frozenset({GrantOperation.RETRIEVE}),
    )
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_TARGET_ID,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_revoked_grant_no_longer_authorises_subsequent_evaluation(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    _revoke_grant_row(tmp_path, _MANAGER_GRANT_ID)
    admin = _administration(tmp_path)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_expired_grant_no_longer_authorises_subsequent_evaluation(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    _insert_grant(
        tmp_path,
        grant_id=_MANAGER_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_REALM,
        expires_at=_TS,
    )
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path, now=_NOW + timedelta(seconds=1))

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal(expires_at=_NOW)),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_grant_replacement_is_create_new_then_revoke_old_both_audited(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_WORKLOAD_ID,
        realm_id=_REALM,
        segments=(_REPO,),
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_MANAGER_ID,
        expires_at="2026-08-06T00:00:00.000000Z",
    )
    admin = _administration(tmp_path)

    created = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000001"),
        correlation_id=_CORRELATION_ID,
    )
    revoked = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000002"),
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(created, Committed)
    assert isinstance(revoked, Committed)
    assert _audit_rows(tmp_path) == [
        ("allow", "grant_created"),
        ("allow", "grant_revoked"),
    ]


def test_affected_grant_ids_recorded_for_create_and_revoke(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    created = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000001"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(created, Committed)
    assert isinstance(created.value, GrantCreated)

    revoked = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=created.value.grant_id, reason_code="superseded"
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000002"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(revoked, Committed)

    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    rows = connection.execute(
        "SELECT sequence, segment_id FROM audit_scope_index "
        "WHERE role = 'target' ORDER BY sequence"
    ).fetchall()
    assert (
        rows == []
    )  # affected_grant_ids is not scope-indexed; check event bytes instead
    events = connection.execute(
        "SELECT canonical_event FROM audit_events ORDER BY sequence"
    ).fetchall()
    documents = [json.loads(row[0]) for row in events]
    assert documents[0]["affected_grant_ids"] == [str(created.value.grant_id)]
    assert documents[1]["affected_grant_ids"] == [str(created.value.grant_id)]


def test_idempotent_replay_of_create_grant_returns_original_result(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    command = CreateGrant(realm_id=_REALM, grant=_proposal())

    first = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    assert first.value == second.value
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert (
        connection.execute("SELECT count(*) FROM grants").fetchone()[0] == 2
    )  # manager + new


def test_a_create_grant_replay_event_names_the_grant_that_exists(
    tmp_path: Path,
) -> None:
    """``create_grant`` mints its grant identity before ``mutate_idempotent``,
    so a replay re-mints one and puts it on the draft — while writing nothing.
    Without restating from the stored result the replay event names a grant
    that exists nowhere, which is a false entry in the load-bearing artefact.

    The data-plane commands carry the same shape; this is the administration
    instance of it.
    """
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    command = CreateGrant(realm_id=_REALM, grant=_proposal())

    first = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        stored = {row[0] for row in connection.execute("SELECT grant_id FROM grants")}
        events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT canonical_event FROM audit_events "
                "WHERE action_code = 'create-grant' ORDER BY sequence"
            )
        ]
    finally:
        connection.close()
    replay = events[-1]
    assert replay["reason_code"] == "idempotent_replay"
    assert replay["affected_grant_ids"] == [str(first.value.grant_id)]
    assert set(replay["affected_grant_ids"]) <= stored


def test_reused_idempotency_key_with_different_command_is_conflict(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    first = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.create_grant(
        _manager_actor(),
        CreateGrant(
            realm_id=_REALM, grant=_proposal(read_clearance=Classification.PUBLIC)
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.IDEMPOTENCY_CONFLICT


def test_a_revoke_grant_conflict_event_claims_no_affected_grant(
    tmp_path: Path,
) -> None:
    """Administration's half of the shared conflict draft, which slice 4
    changed to clear all four identity sets on every idempotency conflict.

    The data-plane justification does not carry over unaltered here. A
    promotion's identities are minted for the request and no row is ever
    written under them, so clearing them removes a fiction. ``revoke-grant``
    names a grant that already existed and was not minted for anything, so
    what is cleared is a true statement about which grant the refused command
    was aimed at.

    The current behaviour is nonetheless the one pinned: the event refuses a
    *different* command, and it revoked nothing. Pinned because only a
    promote test held it, so an administration-side regression — in either
    direction — would go unnoticed.
    """
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    created = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000003"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(created, Committed)
    assert isinstance(created.value, GrantCreated)
    target = created.value.grant_id

    revoked = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(realm_id=_REALM, grant_id=target, reason_code="superseded"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    conflict = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(realm_id=_REALM, grant_id=target, reason_code="rotated"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(revoked, Committed)
    assert isinstance(conflict, Rejected)
    assert conflict.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT canonical_event FROM audit_events "
                "WHERE action_code = 'revoke-grant' ORDER BY sequence"
            )
        ]
    finally:
        connection.close()
    allowed, refused = events
    # The allow event names the grant, so the empty set below is the conflict
    # draft's doing and not a command that never named one.
    assert allowed["affected_grant_ids"] == [str(target)]
    assert refused["reason_code"] == "idempotency_conflict"
    assert refused["affected_grant_ids"] == []


# === Additional coverage: unknown-realm fallback, already-revoked guards,
# === and replay for every command (not just issue-credential/create-grant).


def test_issue_credential_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id="ghost", principal_id=_MANAGER_ID, expires_at=None),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT chain_kind, chain_identity, reason_code FROM audit_events"
    ).fetchone()
    assert row == ("instance", str(_INSTANCE_ID), "realm_not_found")


def test_revoke_credential_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id="ghost",
            credential_id=_MANAGER_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_revoke_credential_already_revoked_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    _revoke_credential_row(tmp_path, _TARGET_CREDENTIAL_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="compromised",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "credential_already_revoked")]


def test_revoke_grant_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id="ghost", grant_id=_MANAGER_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND


def test_revoke_grant_already_revoked_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_OUTSIDER_ID,
    )
    _revoke_grant_row(tmp_path, _TARGET_GRANT_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _outsider_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "grant_already_revoked")]


def test_idempotent_replay_of_create_principal_returns_original_result(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)
    command = CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op")

    first = admin.create_principal(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.create_principal(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    assert first.value == second.value
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("SELECT count(*) FROM principals").fetchone()[0] == 2


def test_idempotent_replay_of_revoke_credential_returns_original_result(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)
    command = RevokeCredential(
        realm_id=_REALM, credential_id=_TARGET_CREDENTIAL_ID, reason_code="compromised"
    )

    first = admin.revoke_credential(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.revoke_credential(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    assert first.value == second.value
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert (
        connection.execute("SELECT count(*) FROM credential_revocations").fetchone()[0]
        == 1
    )


def test_idempotent_replay_of_revoke_grant_returns_original_result(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_OUTSIDER_ID,
    )
    admin = _administration(tmp_path)
    command = RevokeGrant(
        realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
    )

    first = admin.revoke_grant(
        _outsider_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    second = admin.revoke_grant(
        _outsider_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    assert first.value == second.value
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert (
        connection.execute("SELECT count(*) FROM grant_revocations").fetchone()[0] == 1
    )


# === Fix round 1: finding 1 — the specific pre-checked grant is re-verified,
# === not independently re-derived and silently swapped.


def _canonical_event_documents(tmp_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events ORDER BY sequence"
        ).fetchall()
    finally:
        connection.close()
    return [json.loads(row[0]) for row in rows]


@contextmanager
def _read_connection_then(
    data_path: Path, side_effect: Callable[[], None]
) -> Iterator[None]:
    """Patches cairn.administration.commands.read_connection so the given
    side effect runs immediately after the outer gate's read connection
    closes — simulating a concurrent writer acting in the window between
    the outer (unlocked) authorisation read and the write-locked
    transaction that follows it."""
    original = commands_module.read_connection  # type: ignore[attr-defined]
    fired = False

    @contextmanager
    def patched(path: Path) -> Iterator[sqlite3.Connection]:
        nonlocal fired
        with original(path) as connection:
            yield connection
        if not fired:
            fired = True
            side_effect()

    commands_module.read_connection = patched  # type: ignore[attr-defined,assignment]
    try:
        yield
    finally:
        commands_module.read_connection = original  # type: ignore[attr-defined]


def test_grant_revoked_between_precheck_and_transaction_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    first_grant = UUID("10000000-0000-4000-8000-000000000001")
    second_grant = UUID("20000000-0000-4000-8000-000000000002")
    # Two live root grant-manage grants: the outer gate's deterministic sort
    # (by str(grant_id)) picks first_grant. Revoking it in the window before
    # the transaction acquires the write lock must not let the mutation
    # silently proceed and record second_grant instead.
    _insert_grant(
        tmp_path, grant_id=first_grant, principal_id=_MANAGER_ID, realm_id=_REALM
    )
    _insert_grant(
        tmp_path, grant_id=second_grant, principal_id=_MANAGER_ID, realm_id=_REALM
    )
    admin = _administration(tmp_path)

    with _read_connection_then(
        tmp_path, lambda: _revoke_grant_row(tmp_path, first_grant)
    ):
        outcome = admin.create_principal(
            _manager_actor(),
            CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op"),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "authorising_grant_changed")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("SELECT count(*) FROM principals").fetchone()[0] == 1
    assert (
        connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0]
        == 0
    )


def _seed_two_manager_grants(tmp_path: Path) -> tuple[UUID, UUID]:
    """Actor holds two live root grant-manage grants; the deterministic sort
    in _root_grant_manage picks the lexicographically-first one."""
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MANAGER_ID)
    first_grant = UUID("10000000-0000-4000-8000-000000000001")
    second_grant = UUID("20000000-0000-4000-8000-000000000002")
    _insert_grant(
        tmp_path, grant_id=first_grant, principal_id=_MANAGER_ID, realm_id=_REALM
    )
    _insert_grant(
        tmp_path, grant_id=second_grant, principal_id=_MANAGER_ID, realm_id=_REALM
    )
    return first_grant, second_grant


def test_issue_credential_grant_revoked_between_precheck_and_transaction_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    first_grant, _ = _seed_two_manager_grants(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    with _read_connection_then(
        tmp_path, lambda: _revoke_grant_row(tmp_path, first_grant)
    ):
        outcome = admin.issue_credential(
            _manager_actor(),
            IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "authorising_grant_changed")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    # 1, not 0: _seed_two_manager_grants already inserted the manager's own
    # credential; no new one was issued for the target by the denied call.
    assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1


def test_issue_credential_cross_realm_grant_created_mid_flight_fails_closed(
    tmp_path: Path,
) -> None:
    # Fix round 2, finding 1: the outer gate's live_realms set can only
    # GROW while it waits for the write lock — a concurrent writer granting
    # the target principal live access in a realm the actor doesn't control
    # flips the P-02 check from pass to fail, but nothing re-evaluated it
    # under the old mutation() body. Inject exactly that: the target has no
    # live grants when the outer gate checks, then gains one in a realm the
    # actor doesn't control before the transaction runs.
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)

    def grant_cross_realm_access() -> None:
        _insert_grant(
            tmp_path,
            grant_id=_TARGET_GRANT_ID,
            principal_id=_TARGET_ID,
            realm_id=_OTHER_REALM,
            operations=frozenset({GrantOperation.RETRIEVE}),
            delegable_operations=None,
        )

    with _read_connection_then(tmp_path, grant_cross_realm_access):
        outcome = admin.issue_credential(
            _manager_actor(),
            IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "cross_realm_principal")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1


def test_revoke_credential_grant_revoked_between_precheck_and_transaction_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    first_grant, _ = _seed_two_manager_grants(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)

    with _read_connection_then(
        tmp_path, lambda: _revoke_grant_row(tmp_path, first_grant)
    ):
        outcome = admin.revoke_credential(
            _manager_actor(),
            RevokeCredential(
                realm_id=_REALM,
                credential_id=_TARGET_CREDENTIAL_ID,
                reason_code="compromised",
            ),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "authorising_grant_changed")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert (
        connection.execute("SELECT count(*) FROM credential_revocations").fetchone()[0]
        == 0
    )


def test_create_grant_grant_revoked_between_precheck_and_transaction_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    first_grant, _ = _seed_two_manager_grants(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    with _read_connection_then(
        tmp_path, lambda: _revoke_grant_row(tmp_path, first_grant)
    ):
        outcome = admin.create_grant(
            _manager_actor(),
            CreateGrant(realm_id=_REALM, grant=_proposal()),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "authorising_grant_changed")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    # Only the two seeded manager grants — nothing new was created.
    assert connection.execute("SELECT count(*) FROM grants").fetchone()[0] == 2


def test_revoke_grant_grant_revoked_between_precheck_and_transaction_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    first_grant, _ = _seed_two_manager_grants(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_TARGET_ID,  # not the manager, so revocation needs root-manage authority
    )
    admin = _administration(tmp_path)

    with _read_connection_then(
        tmp_path, lambda: _revoke_grant_row(tmp_path, first_grant)
    ):
        outcome = admin.revoke_grant(
            _manager_actor(),
            RevokeGrant(
                realm_id=_REALM, grant_id=_TARGET_GRANT_ID, reason_code="superseded"
            ),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [("deny", "authorising_grant_changed")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    # The side effect itself revoked first_grant (one row); _TARGET_GRANT_ID
    # must not have been revoked by the denied call.
    assert (
        connection.execute(
            "SELECT count(*) FROM grant_revocations WHERE grant_id = ?",
            (str(_TARGET_GRANT_ID),),
        ).fetchone()[0]
        == 0
    )


def test_create_grant_finds_nothing_precheck_denies_even_if_state_now_allows(
    tmp_path: Path,
) -> None:
    # The outer gate found no candidate (target principal did not exist
    # yet). Even if the principal is created in the window before the
    # transaction runs, the mutation must still deny rather than proceed
    # with an ALLOW event that cites no authorising grant.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)
    proposal = _proposal(principal_id=_WORKLOAD_ID)

    with _read_connection_then(
        tmp_path,
        lambda: _insert_principal(
            tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
        ),
    ):
        outcome = admin.create_grant(
            _manager_actor(),
            CreateGrant(realm_id=_REALM, grant=proposal),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert connection.execute("SELECT count(*) FROM grants").fetchone()[0] == 1


def test_allow_events_record_the_grant_that_actually_authorised_each_command(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)

    create_principal_outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op"),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000001"),
        correlation_id=_CORRELATION_ID,
    )
    issue_credential_outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=None),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000002"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(issue_credential_outcome, Committed)
    revoke_credential_outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=issue_credential_outcome.value.credential_id,
            reason_code="compromised",
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000003"),
        correlation_id=_CORRELATION_ID,
    )
    create_grant_outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=_proposal()),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000004"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(create_grant_outcome, Committed)
    revoke_grant_outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM,
            grant_id=create_grant_outcome.value.grant_id,
            reason_code="superseded",
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000005"),
        correlation_id=_CORRELATION_ID,
    )

    for outcome in (
        create_principal_outcome,
        issue_credential_outcome,
        revoke_credential_outcome,
        create_grant_outcome,
        revoke_grant_outcome,
    ):
        assert isinstance(outcome, Committed)

    documents = _canonical_event_documents(tmp_path)
    assert len(documents) == 5
    for document in documents:
        assert document["outcome"] == "allow"
    # create_principal, issue_credential, revoke_credential and create_grant
    # are all authorised by the manager's realm-root grant-manage grant.
    for document in documents[:4]:
        assert document["grant_id"] == str(_MANAGER_GRANT_ID)
    # revoke_grant here is GRANT-08 self-issuer revocation (the manager
    # revoking a grant it itself issued via the preceding create_grant), so
    # there is no "authorising grant" distinct from the target being
    # revoked — grant_id is null by design, not a bug.
    assert documents[4]["grant_id"] is None

    # And at least one denial event: no grant at all, so grant_id is null.
    denied = admin.create_principal(
        _outsider_actor(),
        CreatePrincipal(
            realm_id=_REALM, kind=PrincipalKind.HUMAN, label="never-created"
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000006"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(denied, Rejected)
    denial_document = _canonical_event_documents(tmp_path)[-1]
    assert denial_document["outcome"] == "deny"
    assert denial_document["grant_id"] is None


# === Fix round 1: finding 2 — malformed caller input is a typed, coarse,
# === audited invalid_request, never a raw crash.


def test_create_principal_malformed_label_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="New Op"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT chain_kind, chain_identity, reason_code FROM audit_events"
    ).fetchone()
    assert row == ("instance", str(_INSTANCE_ID), "invalid_label")
    # 1, not 0: _seed_manager already inserted the manager principal itself.
    assert connection.execute("SELECT count(*) FROM principals").fetchone()[0] == 1


def test_create_principal_malformed_realm_id_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id="Not A Realm!!", kind=PrincipalKind.HUMAN, label="x"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "invalid_realm_id")]
    # Fix round 2, finding 2: repeated probes with the same malformed
    # realm_id must correlate via a forensic fingerprint, matching the
    # existing unknown-realm denial's behaviour.
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    canonical_event = connection.execute(
        "SELECT canonical_event FROM audit_events"
    ).fetchone()[0]
    document = json.loads(canonical_event)
    assert (
        document["safe_request_fingerprint"]
        == hashlib.sha256(b"Not A Realm!!").hexdigest()
    )


def test_create_principal_malformed_label_carries_no_fingerprint(
    tmp_path: Path,
) -> None:
    # Only the realm_id case fingerprints its input — a malformed label
    # isn't a "which realm was this attempt against" question, so there's
    # nothing to correlate against.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="New Op"),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    canonical_event = connection.execute(
        "SELECT canonical_event FROM audit_events"
    ).fetchone()[0]
    document = json.loads(canonical_event)
    assert document["safe_request_fingerprint"] is None


def test_revoke_credential_malformed_reason_code_is_invalid_request(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code="Compromised Badly!",
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT chain_kind, reason_code FROM audit_events"
    ).fetchone()
    assert row == ("instance", "invalid_reason_code")
    assert (
        connection.execute("SELECT count(*) FROM credential_revocations").fetchone()[0]
        == 0
    )


def test_revoke_grant_malformed_reason_code_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM, grant_id=_MANAGER_GRANT_ID, reason_code="bad reason!"
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST


def test_create_grant_over_16_segments_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    segments = tuple(
        ScopeSegment(kind="repository", identifier=f"repo-{index}")
        for index in range(17)
    )
    proposal = _proposal(segments=segments)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT chain_kind, reason_code FROM audit_events"
    ).fetchone()
    assert row == ("instance", "invalid_scope")
    assert connection.execute("SELECT count(*) FROM grants").fetchone()[0] == 1


def test_issue_credential_naive_expiry_is_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)
    naive = datetime(2026, 8, 6, 0, 0, 0)  # no tzinfo

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=naive),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute(
        "SELECT chain_kind, reason_code FROM audit_events"
    ).fetchone()
    assert row == ("instance", "invalid_expiry")
    # 1, not 0: _seed_manager already inserted the manager's own credential.
    assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1


def test_create_grant_naive_expiry_is_invalid_request_not_a_crash(
    tmp_path: Path,
) -> None:
    # Before the fix, a naive expires_at reaching delegation_violation's
    # aware-vs-naive datetime comparison raised an uncaught TypeError.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    naive = datetime(2026, 8, 6, 0, 0, 0)
    proposal = _proposal(expires_at=naive)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "invalid_expiry")]
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    row = connection.execute("SELECT chain_kind FROM audit_events").fetchone()
    assert row == ("instance",)  # not the realm chain


def test_issue_credential_expiry_with_fixed_offset_timezone_is_accepted(
    tmp_path: Path,
) -> None:
    # tz-aware but not UTC must still be accepted — only naive is rejected.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)
    aware = datetime(2026, 8, 6, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=aware),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Committed)


# === Fix round 1: finding 3 — replay re-authorises, per I-44.


def test_replay_after_authorising_grant_revoked_is_denied_not_replayed(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)
    command = CreatePrincipal(realm_id=_REALM, kind=PrincipalKind.HUMAN, label="new-op")

    committed = admin.create_principal(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(committed, Committed)

    _revoke_grant_row(tmp_path, _MANAGER_GRANT_ID)

    replay_attempt = admin.create_principal(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(replay_attempt, Rejected)
    assert replay_attempt.failure.code is FailureCode.AUTHORISATION_DENIED
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    assert (
        connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0]
        == 1
    )
    stored_mutation_id = connection.execute(
        "SELECT mutation_id FROM idempotency_records"
    ).fetchone()[0]
    assert stored_mutation_id == str(committed.mutation_receipt.mutation_id)
    # 2, not 1: _seed_manager's own principal plus the one "new-op" commit —
    # the rejected replay attempt must not have added a second "new-op".
    assert connection.execute("SELECT count(*) FROM principals").fetchone()[0] == 2
    rows = _audit_rows(tmp_path)
    assert rows[-1] == ("deny", "grant_manage_not_held")
    assert not any(reason == "idempotent_replay" for _outcome, reason in rows)


def test_replay_after_authorising_grant_revoked_never_calls_the_mutation_body(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)
    command = CreateGrant(realm_id=_REALM, grant=_proposal())
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )

    committed = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(committed, Committed)
    grants_after_commit = (
        sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
        .execute("SELECT count(*) FROM grants")
        .fetchone()[0]
    )

    _revoke_grant_row(tmp_path, _MANAGER_GRANT_ID)

    replay_attempt = admin.create_grant(
        _manager_actor(),
        command,
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(replay_attempt, Rejected)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    # No new grant row was inserted by a wrongly-permitted replay.
    assert connection.execute("SELECT count(*) FROM grants").fetchone()[0] == (
        grants_after_commit
    )


# === Fix wave: final review ==================================================


def test_issue_credential_pre_epoch_expiry_is_invalid_request_not_a_crash(
    tmp_path: Path,
) -> None:
    # Before the fix, a year < 1000 expiry reached _ts()'s unpadded strftime
    # (e.g. "500-01-01T...Z", 26 chars) and then the schema's fixed-width
    # ck_credentials_expires_at CHECK, escaping as a raw, unaudited
    # sqlite3.IntegrityError instead of a typed, audited denial.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)
    pre_epoch = datetime(500, 1, 1, tzinfo=UTC)

    outcome = admin.issue_credential(
        _manager_actor(),
        IssueCredential(realm_id=_REALM, principal_id=_TARGET_ID, expires_at=pre_epoch),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "invalid_expiry")]


def test_create_grant_pre_epoch_expiry_is_invalid_request_not_a_crash(
    tmp_path: Path,
) -> None:
    # Same escape as issue_credential's, via ProposedGrant.expires_at and
    # ck_grants_expires_at.
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(
        tmp_path, _WORKLOAD_ID, kind=PrincipalKind.WORKLOAD, label="worker"
    )
    admin = _administration(tmp_path)
    pre_epoch = datetime(500, 1, 1, tzinfo=UTC)
    proposal = _proposal(expires_at=pre_epoch)

    outcome = admin.create_grant(
        _manager_actor(),
        CreateGrant(realm_id=_REALM, grant=proposal),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("deny", "invalid_expiry")]


def test_create_grant_unprivileged_actor_denial_does_not_disclose_principal_existence(
    tmp_path: Path,
) -> None:
    # An actor with zero grant-manage grants in the realm must be denied
    # identically whether the proposed target principal exists or not — the
    # existence check must not run (and leak) before authorisation.
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    admin = _administration(tmp_path)
    unknown_principal_id = UUID("99999999-9999-4999-8999-999999999999")

    def attempt(principal_id: UUID) -> Rejected:
        proposal = ProposedGrant(
            principal_id=principal_id,
            realm_id=_REALM,
            segments=(),
            operations=frozenset({GrantOperation.RETRIEVE}),
            read_clearance=Classification.PUBLIC,
            write_classifications=frozenset(),
            delegable_operations=None,
            expires_at=_NOW,
        )
        outcome = admin.create_grant(
            _outsider_actor(),
            CreateGrant(realm_id=_REALM, grant=proposal),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(outcome, Rejected)
        return outcome

    known = attempt(_TARGET_ID)
    unknown = attempt(unknown_principal_id)

    assert known.failure.code is FailureCode.AUTHORISATION_DENIED
    assert known.failure.code == unknown.failure.code
    assert known.failure.safe_message == unknown.failure.safe_message
    assert _audit_rows(tmp_path) == [
        ("deny", "grant_manage_not_held"),
        ("deny", "grant_manage_not_held"),
    ]


def test_revoke_grant_unprivileged_actor_denial_does_not_disclose_grant_existence(
    tmp_path: Path,
) -> None:
    # Same disclosure hazard as create_grant's, but via revoke_grant's
    # _grant_by_id lookup: an actor with zero grant-manage grants in the
    # realm and no self-issuer relationship to the target grant must be
    # denied identically whether that grant exists or not.
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_TARGET_ID,
    )
    admin = _administration(tmp_path)
    unknown_grant_id = UUID("99999999-9999-4999-8999-999999999999")

    def attempt(grant_id: UUID) -> Rejected:
        outcome = admin.revoke_grant(
            _outsider_actor(),
            RevokeGrant(realm_id=_REALM, grant_id=grant_id, reason_code="superseded"),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(outcome, Rejected)
        return outcome

    known = attempt(_TARGET_GRANT_ID)
    unknown = attempt(unknown_grant_id)

    assert known.failure.code is FailureCode.AUTHORISATION_DENIED
    assert known.failure.code == unknown.failure.code
    assert known.failure.safe_message == unknown.failure.safe_message
    assert _audit_rows(tmp_path) == [
        ("deny", "grant_manage_not_held"),
        ("deny", "grant_manage_not_held"),
    ]


# === The custody secret screen (I-74, P-26) =================================
#
# Labels and reason codes have narrow grammars, and I-74 declines to trust
# them for it: a 63-character lowercase name has room for a lowercase
# hexadecimal token. Both literals below were run through the real
# ``SecretScreen`` before being written here, and both are valid under
# ``_LABEL_PATTERN`` and ``_REASON_CODE_PATTERN`` respectively — a value that
# failed shape validation would be refused as ``invalid_request`` first and
# would prove nothing about the screen.
_SECRET_LABEL = "password-a3f8b2c9d4e6f1a7b3c8d2e9f4a6b1c7"
_SECRET_REASON_CODE = "password_a3f8b2c9d4e6f1a7b3c8d2e9f4a6b1c7"
_ENTROPY_RULE = f"{POLICY_VERSION}/contextual-entropy"


def _admin_rows(data_path: Path) -> dict[str, object]:
    """Every table an administration mutation can write, counted. The
    leak-negative proof is the whole set unchanged, not one table checked and
    the rest assumed."""
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("principals", "credentials", "grants", "idempotency_records")
        }
    finally:
        connection.close()


def test_create_principal_rejects_a_secret_in_the_label(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    admin = _administration(tmp_path)

    outcome = admin.create_principal(
        _manager_actor(),
        CreatePrincipal(
            realm_id=_REALM, kind=PrincipalKind.WORKLOAD, label=_SECRET_LABEL
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_ENTROPY_RULE, field_path="label"
    )
    assert _audit_rows(tmp_path) == [("deny", "secret_contextual_entropy")]


def test_a_secret_rejected_principal_is_never_written(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    before = _admin_rows(tmp_path)

    assert isinstance(
        _administration(tmp_path).create_principal(
            _manager_actor(),
            CreatePrincipal(
                realm_id=_REALM, kind=PrincipalKind.WORKLOAD, label=_SECRET_LABEL
            ),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        ),
        Rejected,
    )

    assert _admin_rows(tmp_path) == before
    assert _SECRET_LABEL.encode() not in _catalogue_bytes(tmp_path)


def test_revoke_credential_rejects_a_secret_in_the_reason_code(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_credential(tmp_path, _TARGET_CREDENTIAL_ID, _TARGET_ID)
    admin = _administration(tmp_path)

    outcome = admin.revoke_credential(
        _manager_actor(),
        RevokeCredential(
            realm_id=_REALM,
            credential_id=_TARGET_CREDENTIAL_ID,
            reason_code=_SECRET_REASON_CODE,
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_ENTROPY_RULE, field_path="reason_code"
    )
    assert _audit_rows(tmp_path) == [("deny", "secret_contextual_entropy")]
    assert _SECRET_REASON_CODE.encode() not in _catalogue_bytes(tmp_path)


def test_revoke_grant_rejects_a_secret_in_the_reason_code(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _TARGET_ID, label="target")
    _insert_grant(
        tmp_path,
        grant_id=_TARGET_GRANT_ID,
        principal_id=_TARGET_ID,
        realm_id=_REALM,
        operations=frozenset({GrantOperation.RETRIEVE}),
        delegable_operations=None,
        issued_by=_MANAGER_ID,
    )
    admin = _administration(tmp_path)

    outcome = admin.revoke_grant(
        _manager_actor(),
        RevokeGrant(
            realm_id=_REALM,
            grant_id=_TARGET_GRANT_ID,
            reason_code=_SECRET_REASON_CODE,
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_ENTROPY_RULE, field_path="reason_code"
    )
    assert _audit_rows(tmp_path) == [("deny", "secret_contextual_entropy")]
    assert _SECRET_REASON_CODE.encode() not in _catalogue_bytes(tmp_path)


def test_a_secret_denial_event_names_the_rule_but_never_the_field_path(
    tmp_path: Path,
) -> None:
    """P-26's split, on the administration chain as on the realm chain."""
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)

    assert isinstance(
        _administration(tmp_path).create_principal(
            _manager_actor(),
            CreatePrincipal(
                realm_id=_REALM, kind=PrincipalKind.WORKLOAD, label=_SECRET_LABEL
            ),
            idempotency_key=_IDEMPOTENCY_KEY,
            correlation_id=_CORRELATION_ID,
        ),
        Rejected,
    )

    event = _canonical_event_documents(tmp_path)[-1]
    assert event["reason_code"] == "secret_contextual_entropy"
    assert "label" not in json.dumps(event)


def test_a_malformed_label_is_refused_before_the_screen_sees_it(
    tmp_path: Path,
) -> None:
    """I-74 orders the screen after value validation. An upper-case label
    carrying the same entropy is ``invalid_request``, and it never reaches the
    instance-chain denial with a ``detail`` attached."""
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)

    outcome = _administration(tmp_path).create_principal(
        _manager_actor(),
        CreatePrincipal(
            realm_id=_REALM,
            kind=PrincipalKind.WORKLOAD,
            label=_SECRET_LABEL.upper(),
        ),
        idempotency_key=_IDEMPOTENCY_KEY,
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert outcome.failure.detail is None
