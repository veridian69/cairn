import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from cairn.authority import gate
from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.catalogue.audit import (
    ZERO_HASH,
    ActionKind,
    AuditDraft,
    AuditEvent,
    AuditValueError,
    Outcome,
    Scope,
    canonical_audit_bytes,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, read_connection
from cairn.catalogue.transactions import (
    FailureCode,
    MutationRejection,
    RetryClass,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_REALM = "acme"

_MANAGER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGER_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGER_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_OUTSIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OUTSIDER_GRANT_ID = UUID("eeeeeeee-2222-4eee-8eee-eeeeeeeeeeee")
_EXPIRED_GRANT_ID = UUID("eeeeeeee-3333-4eee-8eee-eeeeeeeeeeee")
_REVOKED_GRANT_ID = UUID("eeeeeeee-4444-4eee-8eee-eeeeeeeeeeee")
_OTHER_REALM = "other"
_OTHER_REALM_GRANT_ID = UUID("eeeeeeee-5555-4eee-8eee-eeeeeeeeeeee")
# Passes ck_principals_principal_id — 36 characters, the GLOB's literal
# dashes and the '4'/variant nibbles all in place, nothing outside
# [0-9a-f-] — and still raises ValueError from UUID(), because the GLOB's
# '?' wildcards accept a dash where a hex digit belongs.
_MALFORMED_PRINCIPAL_ID = "--------------4----8----------------"

_EVENT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")


# --- draft generalisation (Step 5) -------------------------------------------


def _wrap_event(draft: AuditDraft) -> AuditEvent:
    """Simulates chain assignment: the sequence/event_id/recorded_at/
    previous_hash a real durable append would attach, held fixed here so the
    comparison isolates the draft's own fields."""
    return AuditEvent(
        draft=draft,
        sequence=1,
        event_id=_EVENT_ID,
        recorded_at=_NOW,
        previous_hash=ZERO_HASH,
    )


def _realm_draft(action_kind: ActionKind) -> AuditDraft:
    return gate.realm_draft(
        realm_id=_REALM,
        actor=gate.Actor(
            principal_id=_MANAGER_ID, credential_id=_MANAGER_CREDENTIAL_ID
        ),
        grant_id=_MANAGER_GRANT_ID,
        action_kind=action_kind,
        action_code="create-principal",
        requested_scope=Scope(_REALM, ()),
        outcome=Outcome.ALLOW,
        reason_code="principal_created",
        correlation_id=_CORRELATION_ID,
    )


# Golden reference bytes, recovered from the pre-generalisation
# ``_admin_draft`` at the accepted slice 3 commit (3d92625) — NOT computed
# via the current ``gate.realm_draft`` — by calling that exact old function
# body (extracted verbatim from ``git show 3d92625:...commands.py``) with
# the fixed inputs ``_realm_draft`` below uses, wrapping it in the same
# fixed chain assignment, and taking ``canonical_audit_bytes``. This is the
# independent anchor: it proves the generalised builder, given
# ``action_kind=ActionKind.ADMINISTRATION``, still emits precisely what the
# hardcoded builder always did — not merely what the current code agrees
# with itself. ``_ADMINISTRATION_GOLDEN_BYTES`` is that recovered literal
# unmodified; ``_DATA_GOLDEN_BYTES`` is derived from it by substituting only
# the ``action_kind`` field value, independent of any gate.py code, so the
# pair also proves the two drafts differ in that field and nowhere else.
_ADMINISTRATION_GOLDEN_BYTES = (
    b'{"action_code":"create-principal","action_kind":"administration",'
    b'"affected_assertion_ids":[],"affected_evidence_ids":[],'
    b'"affected_fact_ids":[],"affected_grant_ids":[],"chain_identity":"acme",'
    b'"chain_kind":"realm","classification_transition":null,'
    b'"command_digest":null,'
    b'"correlation_id":"88888888-8888-4888-8888-888888888888",'
    b'"credential_verifier_id":"aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"event_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
    b'"evidence_digest":null,"evidence_reference":null,'
    b'"grant_id":"eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee","idempotency_key":null,'
    b'"mutation_id":null,"outcome":"allow",'
    b'"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"principal_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"reason_code":"principal_created",'
    b'"recorded_at":"2026-08-05T10:11:12.123456Z",'
    b'"replay_of_mutation_id":null,'
    b'"requested_scope":{"realm":"acme","segments":[]},'
    b'"safe_request_fingerprint":null,"schema":"cairn.audit/v1","sequence":1,'
    b'"source_scope":null,"target_scope":null,"trust_transition":null}'
)
_DATA_GOLDEN_BYTES = _ADMINISTRATION_GOLDEN_BYTES.replace(
    b'"action_kind":"administration"', b'"action_kind":"data"'
)


def test_realm_draft_administration_action_kind_matches_pre_change_bytes() -> None:
    """The generalised builder, given ``action_kind=ActionKind.ADMINISTRATION``
    (what every administration call site now passes explicitly), must
    reproduce the pre-generalisation builder's canonical bytes exactly —
    anchored against the recovered 3d92625 output, not against itself."""
    draft = _realm_draft(ActionKind.ADMINISTRATION)
    actual = canonical_audit_bytes(_wrap_event(draft))
    assert actual == _ADMINISTRATION_GOLDEN_BYTES


def test_realm_draft_data_action_kind_matches_golden_bytes() -> None:
    """A DATA-tagged draft, built from identical inputs bar action_kind, must
    match the golden bytes with only the action_kind field substituted — at
    byte level, not via a dict comparison that discards key ordering."""
    draft = _realm_draft(ActionKind.DATA)
    actual = canonical_audit_bytes(_wrap_event(draft))
    assert actual == _DATA_GOLDEN_BYTES


# --- denial generalisation (Task 7) ------------------------------------------

# Recovered the same way as _ADMINISTRATION_GOLDEN_BYTES above: the output of
# the pre-generalisation ``denial`` (which hardcoded
# ``ActionKind.ADMINISTRATION``) for the fixed inputs ``_denial`` uses,
# captured before the parameter was introduced. ``_DENIAL_DATA_GOLDEN_BYTES``
# substitutes only the action_kind field, so the pair proves a data-plane
# denial differs from an administration one in that field and nowhere else.
_DENIAL_ADMINISTRATION_GOLDEN_BYTES = (
    b'{"action_code":"create-principal","action_kind":"administration",'
    b'"affected_assertion_ids":[],"affected_evidence_ids":[],'
    b'"affected_fact_ids":[],"affected_grant_ids":[],"chain_identity":"acme",'
    b'"chain_kind":"realm","classification_transition":null,'
    b'"command_digest":null,'
    b'"correlation_id":"88888888-8888-4888-8888-888888888888",'
    b'"credential_verifier_id":"aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"event_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
    b'"evidence_digest":null,"evidence_reference":null,'
    b'"grant_id":"eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee","idempotency_key":null,'
    b'"mutation_id":null,"outcome":"deny",'
    b'"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"principal_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"reason_code":"label_exists",'
    b'"recorded_at":"2026-08-05T10:11:12.123456Z",'
    b'"replay_of_mutation_id":null,'
    b'"requested_scope":{"realm":"acme","segments":[]},'
    b'"safe_request_fingerprint":null,"schema":"cairn.audit/v1","sequence":1,'
    b'"source_scope":null,"target_scope":null,"trust_transition":null}'
)
_DENIAL_DATA_GOLDEN_BYTES = _DENIAL_ADMINISTRATION_GOLDEN_BYTES.replace(
    b'"action_kind":"administration"', b'"action_kind":"data"'
)

# Recovered identically from the pre-generalisation ``instance_denial_draft``.
_INSTANCE_ADMINISTRATION_GOLDEN_BYTES = (
    b'{"action_code":"create-principal","action_kind":"administration",'
    b'"affected_assertion_ids":[],"affected_evidence_ids":[],'
    b'"affected_fact_ids":[],"affected_grant_ids":[],'
    b'"chain_identity":"11111111-1111-4111-8111-111111111111",'
    b'"chain_kind":"instance","classification_transition":null,'
    b'"command_digest":null,'
    b'"correlation_id":"88888888-8888-4888-8888-888888888888",'
    b'"credential_verifier_id":"aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"event_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
    b'"evidence_digest":null,"evidence_reference":null,'
    b'"grant_id":null,"idempotency_key":null,'
    b'"mutation_id":null,"outcome":"deny",'
    b'"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"principal_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"reason_code":"realm_not_found",'
    b'"recorded_at":"2026-08-05T10:11:12.123456Z",'
    b'"replay_of_mutation_id":null,"requested_scope":null,'
    b'"safe_request_fingerprint":'
    b'"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",'
    b'"schema":"cairn.audit/v1","sequence":1,'
    b'"source_scope":null,"target_scope":null,"trust_transition":null}'
)
_INSTANCE_DATA_GOLDEN_BYTES = _INSTANCE_ADMINISTRATION_GOLDEN_BYTES.replace(
    b'"action_kind":"administration"', b'"action_kind":"data"'
)


def _denial(action_kind: ActionKind) -> MutationRejection:
    return gate.denial(
        FailureCode.AUTHORISATION_DENIED,
        gate.AUTHORISATION_DENIED_MESSAGE,
        _CORRELATION_ID,
        realm_id=_REALM,
        actor=gate.Actor(
            principal_id=_MANAGER_ID, credential_id=_MANAGER_CREDENTIAL_ID
        ),
        grant_id=_MANAGER_GRANT_ID,
        action_kind=action_kind,
        action_code="create-principal",
        requested_scope=Scope(_REALM, ()),
        reason_code="label_exists",
    )


def _instance_draft(action_kind: ActionKind) -> AuditDraft:
    return gate.instance_denial_draft(
        str(_INSTANCE_ID),
        gate.Actor(principal_id=_MANAGER_ID, credential_id=_MANAGER_CREDENTIAL_ID),
        "create-principal",
        "realm_not_found",
        _CORRELATION_ID,
        action_kind=action_kind,
        safe_request_fingerprint=bytes(range(32)),
    )


def test_realm_draft_carries_custody_identities_and_evidence_fields() -> None:
    """A data-plane allow event names what it touched; the identity sets and
    the evidence pair are inputs to the builder, not fields it can only ever
    leave empty."""
    assertion_id = UUID("20000000-0000-4000-8000-000000000000")
    fact_id = UUID("20000001-0000-4000-8000-000000000000")
    evidence_id = UUID("20000002-0000-4000-8000-000000000000")
    draft = gate.realm_draft(
        realm_id=_REALM,
        actor=gate.Actor(
            principal_id=_MANAGER_ID, credential_id=_MANAGER_CREDENTIAL_ID
        ),
        grant_id=_MANAGER_GRANT_ID,
        action_kind=ActionKind.DATA,
        action_code="ingest",
        requested_scope=Scope(_REALM, ()),
        outcome=Outcome.ALLOW,
        reason_code="assertion_ingested",
        correlation_id=_CORRELATION_ID,
        affected_assertion_ids=(assertion_id,),
        affected_fact_ids=(fact_id,),
        affected_evidence_ids=(evidence_id,),
        evidence_reference=evidence_id,
        evidence_digest=bytes(range(32)),
    )

    assert draft.affected_assertion_ids == (assertion_id,)
    assert draft.affected_fact_ids == (fact_id,)
    assert draft.affected_evidence_ids == (evidence_id,)
    assert draft.evidence_reference == evidence_id
    assert draft.evidence_digest == bytes(range(32))


def test_realm_draft_custody_fields_default_to_empty() -> None:
    draft = _realm_draft(ActionKind.ADMINISTRATION)
    assert draft.affected_assertion_ids == ()
    assert draft.affected_fact_ids == ()
    assert draft.affected_evidence_ids == ()
    assert draft.evidence_reference is None
    assert draft.evidence_digest is None


def test_denial_administration_action_kind_matches_pre_change_bytes() -> None:
    rejection = _denial(ActionKind.ADMINISTRATION)
    actual = canonical_audit_bytes(_wrap_event(rejection.denial_draft))
    assert actual == _DENIAL_ADMINISTRATION_GOLDEN_BYTES


def test_denial_data_action_kind_tags_the_event_as_data() -> None:
    """The first data-plane command to deny itself inside its own transaction
    must not have its audit event filed as administration."""
    rejection = _denial(ActionKind.DATA)
    actual = canonical_audit_bytes(_wrap_event(rejection.denial_draft))
    assert actual == _DENIAL_DATA_GOLDEN_BYTES


def test_denial_failure_is_unchanged_by_the_action_kind() -> None:
    rejection = _denial(ActionKind.DATA)
    assert rejection.failure.code is FailureCode.AUTHORISATION_DENIED
    assert rejection.failure.safe_message == gate.AUTHORISATION_DENIED_MESSAGE
    assert rejection.failure.retry is RetryClass.NEVER


def test_instance_denial_draft_administration_matches_pre_change_bytes() -> None:
    actual = canonical_audit_bytes(
        _wrap_event(_instance_draft(ActionKind.ADMINISTRATION))
    )
    assert actual == _INSTANCE_ADMINISTRATION_GOLDEN_BYTES


def test_instance_denial_draft_data_action_kind_tags_the_event_as_data() -> None:
    """The unknown-realm fallback is reached by data commands too, so it can
    no more hardcode administration than ``denial`` can."""
    actual = canonical_audit_bytes(_wrap_event(_instance_draft(ActionKind.DATA)))
    assert actual == _INSTANCE_DATA_GOLDEN_BYTES


# --- grant lookup (extracted, previously private) ----------------------------


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=_INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _seed_catalogue(data_path: Path) -> None:
    migrate_catalogue(_config(data_path), lambda: _NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (_REALM, _TS)
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (_REALM, bytes(32)),
        )
        connection.commit()


def _insert_principal(data_path: Path, principal_id: UUID | str, *, label: str) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(principal_id), PrincipalKind.HUMAN.value, label, _TS),
        )
        connection.commit()


def _insert_realm(data_path: Path, realm_id: str) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (realm_id, _TS)
        )
        connection.commit()


def _insert_grant(
    data_path: Path,
    *,
    grant_id: UUID,
    principal_id: UUID,
    realm_id: str = _REALM,
    operations: list[str] | None = None,
    expires_at: str | None = None,
) -> None:
    """A realm-root grant. ``delegable_operations`` is derived rather than
    passed: ``ck_grants_delegable_operations_presence`` requires it to be
    non-null exactly when ``operations`` names ``grant-manage``, so deriving
    it keeps every caller's row schema-legal without a second argument that
    has to be kept in step."""
    values = operations or [GrantOperation.GRANT_MANAGE.value]
    delegable = (
        json.dumps([GrantOperation.AUDIT_READ.value])
        if GrantOperation.GRANT_MANAGE.value in values
        else None
    )
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
                "[]",
                json.dumps(sorted(values)),
                "restricted",
                json.dumps(["public", "internal", "restricted"]),
                delegable,
                None,
                expires_at,
                _TS,
            ),
        )
        connection.commit()


def _revoke_grant(data_path: Path, grant_id: UUID, revoked_by: UUID) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grant_revocations "
            "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
            (str(grant_id), _TS, str(revoked_by), "superseded"),
        )
        connection.commit()


def _insert_credential(data_path: Path, credential_id: UUID, principal_id: str) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(credential_id), principal_id, bytes(32), _TS),
        )
        connection.commit()


def _seed_manager(data_path: Path) -> None:
    """A principal with a live realm-root grant-manage grant — mirrors
    tests/administration/test_commands.py's ``_seed_manager`` (duplicated
    per the no-shared-fixture convention)."""
    _insert_principal(data_path, _MANAGER_ID, label="manager")
    _insert_grant(data_path, grant_id=_MANAGER_GRANT_ID, principal_id=_MANAGER_ID)


def test_grants_for_principal_returns_only_that_principals_realm_grants(
    tmp_path: Path,
) -> None:
    """The same principal holds a grant in a second realm. Seeding one realm
    would leave the realm predicate exercised only positively — it would
    return the right single grant whether or not the predicate was there."""
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_realm(tmp_path, _OTHER_REALM)
    _insert_grant(
        tmp_path,
        grant_id=_OTHER_REALM_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_OTHER_REALM,
    )
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        grants = gate.grants_for_principal(fetch, _MANAGER_ID, _REALM)
    assert [g.grant_id for g in grants] == [_MANAGER_GRANT_ID]
    assert grants[0].principal_id == _MANAGER_ID
    assert grants[0].realm_id == _REALM
    assert GrantOperation.GRANT_MANAGE in grants[0].operations
    assert grants[0].revoked is False


def test_grants_for_principal_any_realm_returns_both_realms(tmp_path: Path) -> None:
    """The baseline for the case above: the second grant is real, live and
    readable, so its absence there is the realm predicate's doing and not a
    seeding accident."""
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_realm(tmp_path, _OTHER_REALM)
    _insert_grant(
        tmp_path,
        grant_id=_OTHER_REALM_GRANT_ID,
        principal_id=_MANAGER_ID,
        realm_id=_OTHER_REALM,
    )
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        grants = gate.grants_for_principal_any_realm(fetch, _MANAGER_ID)
    assert sorted(str(g.grant_id) for g in grants) == sorted(
        [str(_MANAGER_GRANT_ID), str(_OTHER_REALM_GRANT_ID)]
    )


def test_grants_for_principal_returns_nothing_for_unrelated_principal(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    _insert_principal(tmp_path, _OUTSIDER_ID, label="outsider")
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        grants = gate.grants_for_principal(fetch, _OUTSIDER_ID, _REALM)
    assert grants == ()


def test_root_grant_manage_finds_the_live_realm_root_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_manager(tmp_path)
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        manager = gate.root_grant_manage(fetch, _MANAGER_ID, _REALM, _NOW)
    assert manager is not None
    assert manager.grant_id == _MANAGER_GRANT_ID


def test_root_grant_manage_returns_none_without_a_grant_manage_grant(
    tmp_path: Path,
) -> None:
    """A grant the principal genuinely holds, live and at the realm root,
    naming every operation *except* ``grant-manage``. Seeding no grant at all
    would leave ``find_authorising_grant``'s operation predicate unexercised:
    an empty set has nothing to reject."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _OUTSIDER_ID, label="outsider")
    _insert_grant(
        tmp_path,
        grant_id=_OUTSIDER_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        operations=[
            GrantOperation.RETRIEVE.value,
            GrantOperation.INGEST.value,
            GrantOperation.PROMOTE.value,
            GrantOperation.INVALIDATE.value,
            GrantOperation.AUDIT_READ.value,
        ],
    )
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        manager = gate.root_grant_manage(fetch, _OUTSIDER_ID, _REALM, _NOW)
    assert manager is None


def test_root_grant_manage_finds_that_same_grant_once_it_manages(
    tmp_path: Path,
) -> None:
    """The baseline for the case above: identical seeding but for the
    ``operations`` column, so the missing ``grant-manage`` is the sole reason
    the other returns ``None``."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _OUTSIDER_ID, label="outsider")
    _insert_grant(
        tmp_path,
        grant_id=_OUTSIDER_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        operations=[
            GrantOperation.RETRIEVE.value,
            GrantOperation.INGEST.value,
            GrantOperation.PROMOTE.value,
            GrantOperation.INVALIDATE.value,
            GrantOperation.AUDIT_READ.value,
            GrantOperation.GRANT_MANAGE.value,
        ],
    )
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        manager = gate.root_grant_manage(fetch, _OUTSIDER_ID, _REALM, _NOW)
    assert manager is not None
    assert manager.grant_id == _OUTSIDER_GRANT_ID


def test_root_grant_manage_ignores_an_expired_grant_manage_grant(
    tmp_path: Path,
) -> None:
    """Liveness, not merely holding. ``_seed_manager``'s grant never expires
    and is never revoked, so neither half of ``is_live`` was ever negatively
    exercised through this lookup."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _OUTSIDER_ID, label="outsider")
    _insert_grant(
        tmp_path,
        grant_id=_EXPIRED_GRANT_ID,
        principal_id=_OUTSIDER_ID,
        expires_at=_TS,
    )
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        manager = gate.root_grant_manage(
            fetch, _OUTSIDER_ID, _REALM, _NOW + timedelta(seconds=1)
        )
    assert manager is None


def test_root_grant_manage_ignores_a_revoked_grant_manage_grant(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _OUTSIDER_ID, label="outsider")
    _insert_grant(tmp_path, grant_id=_REVOKED_GRANT_ID, principal_id=_OUTSIDER_ID)
    _revoke_grant(tmp_path, _REVOKED_GRANT_ID, _OUTSIDER_ID)
    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        manager = gate.root_grant_manage(fetch, _OUTSIDER_ID, _REALM, _NOW)
    assert manager is None


def test_credential_principal_refuses_an_unparseable_stored_identity(
    tmp_path: Path,
) -> None:
    """``credentials.principal_id`` carries no CHECK of its own, only a
    foreign key to ``principals.principal_id`` — the same shape-only GLOB
    ``_stored_grant_uuid`` guards. Without a reader guard ``UUID()`` raises a
    bare ``ValueError`` with no code and no audit reason."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MALFORMED_PRINCIPAL_ID, label="drifted")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, _MALFORMED_PRINCIPAL_ID)

    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        with pytest.raises(AuditValueError) as caught:
            gate.credential_principal(fetch, _MANAGER_CREDENTIAL_ID)
    assert caught.value.code == "credential_uuid_malformed"


def test_credential_principal_returns_a_parseable_stored_identity(
    tmp_path: Path,
) -> None:
    """The baseline: identical seeding but for the credential's stored
    identity."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _MALFORMED_PRINCIPAL_ID, label="drifted")
    _insert_principal(tmp_path, _MANAGER_ID, label="manager")
    _insert_credential(tmp_path, _MANAGER_CREDENTIAL_ID, str(_MANAGER_ID))

    with read_connection(tmp_path) as connection:
        fetch = gate.fetch_from(connection)
        principal_id = gate.credential_principal(fetch, _MANAGER_CREDENTIAL_ID)
    assert principal_id == _MANAGER_ID


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC), False),
        (datetime(1000, 1, 1, tzinfo=UTC), True),
        (datetime(1000, 1, 1, tzinfo=timezone(timedelta(hours=1))), False),
        (datetime(999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-1))), True),
        (datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))), False),
    ],
)
def test_expiry_and_preparation_preserve_utc_year_boundary(
    value: datetime, accepted: bool
) -> None:
    from cairn.authority.session_codec import canonical

    assert gate.validate_expiry(value) == (None if accepted else "invalid_expiry")
    if accepted:
        assert canonical(value) == b'"1000-01-01T00:00:00.000000Z"'
    else:
        with pytest.raises(ValueError, match="invalid_session_timestamp"):
            canonical(value)
