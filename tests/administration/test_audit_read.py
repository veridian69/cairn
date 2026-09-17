import itertools
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cairn.administration.audit_read import (
    AuditEventPage,
    ReadAuditEvents,
    read_audit_events,
)
from cairn.administration.commands import Actor
from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    ChainKind,
    Classification,
    Outcome,
    Scope,
    ScopeSegment,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME, _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_REALM = "acme"
_OTHER_REALM = "other-realm"

_READER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_READER_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_READER_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_OUTSIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OUTSIDER_CREDENTIAL_ID = UUID("bbbbbbbb-cccc-4bbb-8bbb-bbbbbbbbbbbb")

_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")

_REPO = ScopeSegment(kind="repository", identifier="acme-repo")
_OTHER_REPO = ScopeSegment(kind="repository", identifier="other-repo")
_BRANCH = ScopeSegment(kind="branch", identifier="main")


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


def _transactions(data_path: Path, *, now: datetime = _NOW) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: now,
        uuid_factory=_uuid_seq(0x30000000),
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
    data_path: Path, credential_id: UUID, principal_id: UUID
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(credential_id), str(principal_id), b"\x00" * 32, _TS),
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
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.AUDIT_READ}),
    expires_at: str | None = None,
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
                Classification.RESTRICTED.value,
                _set_json(
                    frozenset(
                        {
                            Classification.PUBLIC.value,
                            Classification.INTERNAL.value,
                            Classification.RESTRICTED.value,
                        }
                    )
                ),
                None,
                None,
                expires_at,
                _TS,
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


def _seed_reader(
    data_path: Path,
    *,
    segments: tuple[ScopeSegment, ...] = (),
    grant_id: UUID = _READER_GRANT_ID,
) -> None:
    _insert_principal(data_path, _READER_ID, label="reader")
    _insert_credential(data_path, _READER_CREDENTIAL_ID, _READER_ID)
    _insert_grant(
        data_path, grant_id=grant_id, principal_id=_READER_ID, segments=segments
    )


def _seed_outsider(data_path: Path) -> None:
    _insert_principal(data_path, _OUTSIDER_ID, label="outsider")
    _insert_credential(data_path, _OUTSIDER_CREDENTIAL_ID, _OUTSIDER_ID)


def _reader_actor() -> Actor:
    return Actor(principal_id=_READER_ID, credential_id=_READER_CREDENTIAL_ID)


def _outsider_actor() -> Actor:
    return Actor(principal_id=_OUTSIDER_ID, credential_id=_OUTSIDER_CREDENTIAL_ID)


def _draft(
    *,
    chain_kind: ChainKind = ChainKind.REALM,
    chain_identity: str = _REALM,
    action_code: str = "seed-event",
    reason_code: str = "seeded",
    requested_scope: Scope | None = None,
    source_scope: Scope | None = None,
    target_scope: Scope | None = None,
) -> AuditDraft:
    return AuditDraft(
        chain_kind=chain_kind,
        chain_identity=chain_identity,
        principal_id=None,
        credential_verifier_id=None,
        grant_id=None,
        action_kind=ActionKind.DATA,
        action_code=action_code,
        source_scope=source_scope,
        requested_scope=requested_scope,
        target_scope=target_scope,
        outcome=Outcome.ALLOW,
        reason_code=reason_code,
        affected_assertion_ids=(),
        affected_fact_ids=(),
        affected_evidence_ids=(),
        affected_grant_ids=(),
        classification_transition=None,
        trust_transition=None,
        evidence_reference=None,
        evidence_digest=None,
        correlation_id=_CORRELATION_ID,
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=None,
    )


def _audit_rows(data_path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return connection.execute(
            "SELECT chain_kind, chain_identity, sequence, outcome, reason_code "
            "FROM audit_events ORDER BY chain_kind, chain_identity, sequence"
        ).fetchall()
    finally:
        connection.close()


def _command(
    *,
    realm_id: str = _REALM,
    scope_prefix: tuple[ScopeSegment, ...] = (_REPO,),
    after_sequence: int = 0,
    limit: int = 100,
) -> ReadAuditEvents:
    return ReadAuditEvents(
        realm_id=realm_id,
        scope_prefix=scope_prefix,
        after_sequence=after_sequence,
        limit=limit,
    )


# === Step 1: candidate matching and scope isolation ==========================


def test_prefix_grant_returns_prefix_and_descendant_excludes_root_sibling_and_cross_realm(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    root_receipt = transactions.append_audit(_draft(requested_scope=Scope(_REALM, ())))
    prefix_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, (_REPO,)))
    )
    descendant_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, (_REPO, _BRANCH)))
    )
    transactions.append_audit(_draft(requested_scope=Scope(_REALM, (_OTHER_REPO,))))
    transactions.append_audit(
        _draft(
            chain_identity=_OTHER_REALM, requested_scope=Scope(_OTHER_REALM, (_REPO,))
        )
    )

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    sequences = [event.sequence for event in outcome.events]
    assert sequences == [prefix_receipt.sequence, descendant_receipt.sequence]
    assert root_receipt.sequence not in sequences
    assert outcome.next_after_sequence is None


def test_every_non_null_scope_must_be_within_prefix(tmp_path: Path) -> None:
    """P-03: an event matches only when EVERY non-null scope (source,
    requested, target) is at or below the prefix -- one out-of-scope role is
    enough to exclude it, even when another role matches."""
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    target_outside_prefix = transactions.append_audit(
        _draft(
            requested_scope=Scope(_REALM, (_REPO,)),
            target_scope=Scope(_REALM, (_OTHER_REPO,)),
        )
    )
    source_outside_prefix = transactions.append_audit(
        _draft(
            source_scope=Scope(_REALM, (_OTHER_REPO,)),
            requested_scope=Scope(_REALM, (_REPO,)),
        )
    )
    all_scopes_within_prefix = transactions.append_audit(
        _draft(
            source_scope=Scope(_REALM, (_REPO,)),
            requested_scope=Scope(_REALM, (_REPO,)),
            target_scope=Scope(_REALM, (_REPO, _BRANCH)),
        )
    )

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    sequences = [event.sequence for event in outcome.events]
    assert sequences == [all_scopes_within_prefix.sequence]
    assert target_outside_prefix.sequence not in sequences
    assert source_outside_prefix.sequence not in sequences


def test_root_prefix_grant_sees_all_events_including_all_null_scope(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=())
    transactions = _transactions(tmp_path)

    root_receipt = transactions.append_audit(_draft(requested_scope=Scope(_REALM, ())))
    prefix_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, (_REPO,)))
    )
    null_scope_receipt = transactions.append_audit(_draft())

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=()),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    sequences = [event.sequence for event in outcome.events]
    assert sequences == [
        root_receipt.sequence,
        prefix_receipt.sequence,
        null_scope_receipt.sequence,
    ]


def test_all_null_scope_event_absent_for_non_root_prefix(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    transactions.append_audit(_draft())
    prefix_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, (_REPO,)))
    )

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    assert [event.sequence for event in outcome.events] == [prefix_receipt.sequence]


def test_instance_chain_events_never_returned_for_any_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=())
    transactions = _transactions(tmp_path)

    transactions.append_audit(
        _draft(chain_kind=ChainKind.INSTANCE, chain_identity=str(_INSTANCE_ID))
    )
    prefix_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, ()))
    )

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=()),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    assert [event.sequence for event in outcome.events] == [prefix_receipt.sequence]
    assert all(event.draft.chain_kind is ChainKind.REALM for event in outcome.events)


def test_pagination_walks_all_matching_events_in_ascending_order(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    receipts = [
        transactions.append_audit(_draft(requested_scope=Scope(_REALM, (_REPO,))))
        for _ in range(3)
    ]

    page_one = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,), limit=2),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )
    assert isinstance(page_one, AuditEventPage)
    assert [event.sequence for event in page_one.events] == [
        receipts[0].sequence,
        receipts[1].sequence,
    ]
    assert page_one.next_after_sequence == receipts[1].sequence

    page_two = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(
            scope_prefix=(_REPO,),
            after_sequence=page_one.next_after_sequence,
            limit=2,
        ),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )
    assert isinstance(page_two, AuditEventPage)
    # page_one's own audit-read allow event (sequence receipts[2] + 1) has the
    # same requested_scope as the query prefix, so it is itself a legitimate
    # candidate for this second, same-prefix read — intended per the brief.
    own_allow_sequence = receipts[2].sequence + 1
    assert [event.sequence for event in page_two.events] == [
        receipts[2].sequence,
        own_allow_sequence,
    ]
    assert page_two.next_after_sequence is None


# === Step 2: reconciliation, allow-event durability and denial ==============


def test_drifted_scope_index_row_cannot_disclose_sibling_event(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    sibling_receipt = transactions.append_audit(
        _draft(requested_scope=Scope(_REALM, (_OTHER_REPO,)))
    )

    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE audit_scope_index SET segment_id = ? "
            "WHERE chain_kind = 'realm' AND chain_identity = ? AND sequence = ? "
            "AND role = 'requested' AND ordinal = 0",
            (_REPO.identifier, _REALM, sibling_receipt.sequence),
        )
        assert updated.rowcount == 1
        connection.commit()

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    assert sibling_receipt.sequence not in [event.sequence for event in outcome.events]


def test_successful_read_appends_allow_event_and_is_not_double_counted(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)
    seeded = transactions.append_audit(_draft(requested_scope=Scope(_REALM, (_REPO,))))

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    assert [event.sequence for event in outcome.events] == [seeded.sequence]

    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        row = connection.execute(
            "SELECT sequence, action_kind, action_code, outcome, reason_code "
            "FROM audit_events WHERE chain_kind = 'realm' AND chain_identity = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (_REALM,),
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        seeded.sequence + 1,
        "administration",
        "audit-read",
        "allow",
        "audit_read_completed",
    )


def test_missing_grant_is_denied_and_audited(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    transactions = _transactions(tmp_path)

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _outsider_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", _REALM, 1, "deny", "audit_read_grant_not_held")
    ]


def test_revoked_grant_is_denied_and_audited(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    _revoke_grant_row(tmp_path, _READER_GRANT_ID)
    transactions = _transactions(tmp_path)

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", _REALM, 1, "deny", "audit_read_grant_not_held")
    ]


def test_expired_grant_is_denied(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _READER_ID, label="reader")
    _insert_credential(tmp_path, _READER_CREDENTIAL_ID, _READER_ID)
    _insert_grant(
        tmp_path,
        grant_id=_READER_GRANT_ID,
        principal_id=_READER_ID,
        segments=(_REPO,),
        expires_at=_TS,
    )
    transactions = _transactions(tmp_path, now=_NOW + timedelta(seconds=1))

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW + timedelta(seconds=1),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_unknown_realm_is_not_found_with_instance_chain_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(realm_id="ghost-realm", scope_prefix=()),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    rows = _audit_rows(tmp_path)
    assert rows == [("instance", str(_INSTANCE_ID), 1, "deny", "realm_not_found")]


def test_invalid_limit_is_rejected_as_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,), limit=0),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    rows = _audit_rows(tmp_path)
    assert rows == [("instance", str(_INSTANCE_ID), 1, "deny", "invalid_limit")]


def test_returned_events_are_parsed_audit_events(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_reader(tmp_path, segments=(_REPO,))
    transactions = _transactions(tmp_path)
    transactions.append_audit(_draft(requested_scope=Scope(_REALM, (_REPO,))))

    outcome = read_audit_events(
        tmp_path,
        transactions,
        _reader_actor(),
        _command(scope_prefix=(_REPO,)),
        correlation_id=_CORRELATION_ID,
        clock=lambda: _NOW,
    )

    assert isinstance(outcome, AuditEventPage)
    assert len(outcome.events) == 1
    event = outcome.events[0]
    assert event.draft.action_code == "seed-event"
    assert event.draft.requested_scope == Scope(_REALM, (_REPO,))
