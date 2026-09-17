import hashlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    AuditEvent,
    AuditValueError,
    ChainKind,
    Outcome,
    Scope,
    ScopeRole,
    ScopeSegment,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.catalogue.transactions import (
    AuditReceipt,
    CatalogueContention,
    CatalogueTransactionError,
    CatalogueTransactions,
    CommitAmbiguity,
    Committed,
    CompoundTransaction,
    FailureCode,
    MutationReceipt,
    MutationRejection,
    Rejected,
    Replayed,
    RetryClass,
    StableFailure,
    _MutationTransaction,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _draft(reason_code: str) -> AuditDraft:
    return AuditDraft(
        chain_kind=ChainKind.INSTANCE,
        chain_identity=str(INSTANCE_ID),
        principal_id=None,
        credential_verifier_id=None,
        grant_id=None,
        action_kind=ActionKind.SYSTEM,
        action_code="catalogue-check",
        source_scope=None,
        requested_scope=None,
        target_scope=None,
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
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=None,
    )


def _values[T](values: list[T]) -> Iterator[T]:
    yield from values


def _add_realm_head(data_path: Path, realm: str) -> None:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(
        "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
        (realm, "2026-08-05T12:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', ?, 0, ?)",
        (realm, bytes(32)),
    )
    connection.commit()
    connection.close()


def _encode_result(value: str, receipt: MutationReceipt) -> bytes:
    return json.dumps(
        {
            "mutation_receipt": {
                "command_digest": receipt.command_digest.hex(),
                "mutation_id": str(receipt.mutation_id),
            },
            "value": value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _decode_result(value: bytes) -> tuple[str, MutationReceipt]:
    document = json.loads(value)
    return (
        document["value"],
        MutationReceipt(
            mutation_id=UUID(document["mutation_receipt"]["mutation_id"]),
            command_digest=bytes.fromhex(
                document["mutation_receipt"]["command_digest"]
            ),
        ),
    )


def _mutate(
    store: CatalogueTransactions,
    draft: AuditDraft,
    mutation: Callable[[_MutationTransaction], str],
    *,
    command_digest: bytes = bytes.fromhex("11" * 32),
) -> Committed[str] | Replayed[str] | Rejected:
    return store.mutate_idempotent(
        draft,
        principal_id=UUID("66666666-6666-4666-8666-666666666666"),
        operation="realm-bootstrap",
        idempotency_key=UUID("77777777-7777-4777-8777-777777777777"),
        command_digest=command_digest,
        result_schema="cairn.result/test-v1",
        mutation=mutation,
        encode=_encode_result,
        decode=_decode_result,
    )


def test_append_advances_instance_chain_with_canonical_events(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    times = _values([NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    event_ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: next(times),
        uuid_factory=lambda: next(event_ids),
    )

    first = store.append_audit(_draft("first_check"))
    second = store.append_audit(_draft("second_check"))

    assert first.sequence == 1
    assert second.sequence == 2
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        rows = connection.execute(
            "SELECT sequence, previous_hash, event_hash, canonical_event "
            "FROM audit_events ORDER BY sequence"
        ).fetchall()
        assert rows[0][1] == bytes(32)
        assert rows[1][1] == rows[0][2]
        assert parse_canonical_audit_bytes(rows[0][3]).sequence == 1
        assert parse_canonical_audit_bytes(rows[1][3]).sequence == 2
        assert connection.execute(
            "SELECT last_sequence, last_hash FROM audit_heads "
            "WHERE chain_kind = 'instance' AND chain_identity = ?",
            (str(INSTANCE_ID),),
        ).fetchone() == (2, rows[1][2])


def test_realm_chains_advance_independently(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "alpha")
    _add_realm_head(tmp_path, "beta")
    event_ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(event_ids),
    )
    alpha = replace(
        _draft("alpha_check"),
        chain_kind=ChainKind.REALM,
        chain_identity="alpha",
    )
    beta = replace(
        _draft("beta_check"),
        chain_kind=ChainKind.REALM,
        chain_identity="beta",
    )

    assert store.append_audit(alpha).sequence == 1
    assert store.append_audit(beta).sequence == 1
    assert store.append_audit(alpha).sequence == 2


def test_concurrent_appends_are_serialised_by_supplied_writer_gate(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    ids = _values(
        [UUID(f"{index:08x}-0000-4000-8000-000000000000") for index in range(1, 9)]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(
            executor.map(
                store.append_audit,
                [_draft(f"check_{index}") for index in range(1, 9)],
            )
        )

    assert sorted(receipt.sequence for receipt in receipts) == list(range(1, 9))


def test_scope_projection_distinguishes_absent_root_and_segments(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    event_ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(event_ids),
    )
    realm_draft = replace(
        _draft("scope_check"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
        source_scope=Scope(realm="local", segments=()),
        requested_scope=Scope(
            realm="local",
            segments=(ScopeSegment(kind="job", identifier="job/1"),),
        ),
    )

    store.append_audit(realm_draft)
    store.append_audit(replace(realm_draft, source_scope=None, requested_scope=None))

    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT sequence, role, ordinal, segment_kind, segment_id "
            "FROM audit_scope_index ORDER BY sequence, role, ordinal"
        ).fetchall() == [
            (1, "requested", 0, "job", "job/1"),
            (1, "source", -1, None, None),
        ]


def test_stale_head_compare_and_set_rolls_back_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    original = store._insert_scope
    sabotaged = False

    def sabotage(
        connection: sqlite3.Connection,
        event: AuditEvent,
        role: ScopeRole,
        scope: Scope | None,
    ) -> None:
        nonlocal sabotaged
        original(connection, event, role, scope)
        if not sabotaged:
            sabotaged = True
            connection.execute(
                "UPDATE audit_heads SET last_sequence = 99 "
                "WHERE chain_kind = 'instance' AND chain_identity = ?",
                (str(INSTANCE_ID),),
            )

    monkeypatch.setattr(store, "_insert_scope", sabotage)

    with pytest.raises(CatalogueTransactionError) as caught:
        store.append_audit(_draft("stale_head"))

    assert caught.value.code == "stale_audit_head"
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT last_sequence FROM audit_heads WHERE chain_kind = 'instance'"
        ).fetchone() == (0,)


def test_idempotent_mutation_commits_and_replays_exact_result(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
    )
    calls = 0

    def mutation(transaction: _MutationTransaction) -> str:
        nonlocal calls
        calls += 1
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("created", "2026-08-05T12:00:01.000000Z"),
        )
        return "created"

    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
        action_kind=ActionKind.ADMINISTRATION,
        action_code="realm-bootstrap",
    )
    committed = _mutate(store, draft, mutation)
    restarted = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=2),
        uuid_factory=lambda: next(ids),
    )
    replayed = _mutate(restarted, draft, mutation)

    assert isinstance(committed, Committed)
    assert isinstance(replayed, Replayed)
    assert committed.value == replayed.value == "created"
    assert committed.mutation_receipt == replayed.mutation_receipt
    assert committed.audit_receipt.sequence == 1
    assert replayed.audit_receipt.sequence == 2
    assert calls == 1
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM realms WHERE realm_id = 'created'"
        ).fetchone() == (1,)


def test_changed_command_is_audited_idempotency_conflict(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )
    principal_id = UUID("66666666-6666-4666-8666-666666666666")
    key = UUID("77777777-7777-4777-8777-777777777777")

    store.mutate_idempotent(
        draft,
        principal_id=principal_id,
        operation="realm-bootstrap",
        idempotency_key=key,
        command_digest=bytes.fromhex("11" * 32),
        result_schema="cairn.result/test-v1",
        mutation=lambda _transaction: "created",
        encode=_encode_result,
        decode=_decode_result,
    )
    conflict = store.mutate_idempotent(
        draft,
        principal_id=principal_id,
        operation="realm-bootstrap",
        idempotency_key=key,
        command_digest=bytes.fromhex("22" * 32),
        result_schema="cairn.result/test-v1",
        mutation=lambda _transaction: "must-not-run",
        encode=_encode_result,
        decode=_decode_result,
    )

    assert isinstance(conflict, Rejected)
    assert conflict.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    assert conflict.audit_receipt.sequence == 2


def test_mutation_failure_rolls_back_domain_audit_and_idempotency(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    def fail(transaction: _MutationTransaction) -> str:
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("residue", "2026-08-05T12:00:01.000000Z"),
        )
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError):
        store.mutate_idempotent(
            draft,
            principal_id=UUID("66666666-6666-4666-8666-666666666666"),
            operation="realm-bootstrap",
            idempotency_key=UUID("77777777-7777-4777-8777-777777777777"),
            command_digest=bytes.fromhex("11" * 32),
            result_schema="cairn.result/test-v1",
            mutation=fail,
            encode=_encode_result,
            decode=_decode_result,
        )

    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM realms WHERE realm_id = 'residue'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (0,)


def test_audit_only_rejection_is_durable(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    failure = StableFailure(
        code=FailureCode.AUTHORISATION_DENIED,
        safe_message="The operation is not authorised.",
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        retry=RetryClass.NEVER,
    )

    rejected = store.reject(
        replace(
            _draft("authorisation_denied"),
            outcome=Outcome.DENY,
        ),
        failure,
    )

    assert rejected == Rejected(failure=failure, audit_receipt=rejected.audit_receipt)
    assert rejected.audit_receipt.sequence == 1


def test_replay_detects_corrupt_stored_result_after_restart(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
    )

    def mutation(_transaction: _MutationTransaction) -> str:
        return "created"

    _mutate(store, draft, mutation)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    connection.execute("DROP TRIGGER trg_idempotency_records_no_update")
    connection.execute(
        "UPDATE idempotency_records SET result_digest = ?",
        (bytes(32),),
    )
    connection.commit()
    connection.close()
    restarted = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=2),
        uuid_factory=lambda: next(ids),
    )

    with pytest.raises(CatalogueTransactionError) as caught:
        _mutate(restarted, draft, mutation)

    assert caught.value.code == "idempotency_record_corrupt"


def test_audit_event_guard_rejects_update_and_delete(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    store.append_audit(_draft("guard_check"))
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audit_events SET reason_code = 'changed'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_events")


def test_matching_idempotency_state_resolves_ambiguous_commit(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )

    def commit_then_lose_acknowledgement(connection: sqlite3.Connection) -> None:
        connection.commit()
        raise CommitAmbiguity

    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
        commit=commit_then_lose_acknowledgement,
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    outcome = _mutate(store, draft, lambda _transaction: "created")

    assert isinstance(outcome, Committed)
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (1,)


def test_absent_idempotency_state_records_error_after_ambiguous_commit(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
        ]
    )

    def lose_before_commit(connection: sqlite3.Connection) -> None:
        connection.rollback()
        raise CommitAmbiguity

    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
        commit=lose_before_commit,
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    with pytest.raises(CatalogueTransactionError) as caught:
        _mutate(store, draft, lambda _transaction: "created")

    assert caught.value.code == "commit_outcome_unknown"
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT outcome, reason_code FROM audit_events"
        ).fetchall() == [("error", "commit_ambiguous")]


_HOSTILE_REALM = "'; DROP TABLE audit_events; --‮EVIL"


def _catalogue_bytes(data_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(data_path.iterdir())
        if path.is_file() and path.name.startswith(CATALOGUE_FILENAME)
    )


def test_unbindable_request_records_only_a_fingerprint_in_the_instance_chain(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    fingerprint = hashlib.sha256(_HOSTILE_REALM.encode()).digest()
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    draft = replace(
        _draft("unknown_realm"),
        outcome=Outcome.DENY,
        safe_request_fingerprint=fingerprint,
    )

    receipt = store.append_audit(draft)

    assert receipt.chain_kind is ChainKind.INSTANCE
    assert receipt.chain_identity == str(INSTANCE_ID)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    canonical = connection.execute(
        "SELECT canonical_event FROM audit_events"
    ).fetchone()[0]
    stored = parse_canonical_audit_bytes(canonical)
    assert stored.draft.safe_request_fingerprint == fingerprint
    assert stored.draft.source_scope is None
    assert stored.draft.requested_scope is None
    assert stored.draft.target_scope is None
    assert connection.execute("SELECT count(*) FROM realms").fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM audit_heads WHERE chain_kind = 'realm'"
    ).fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM audit_scope_index").fetchone() == (
        0,
    )
    connection.close()
    assert _HOSTILE_REALM.encode() not in _catalogue_bytes(tmp_path)


def test_hostile_realm_text_cannot_enter_instance_identity_or_scope() -> None:
    with pytest.raises(AuditValueError) as identity_error:
        replace(_draft("unknown_realm"), chain_identity=_HOSTILE_REALM)

    assert identity_error.value.code == "invalid_uuid"

    with pytest.raises(AuditValueError) as realm_error:
        Scope(realm=_HOSTILE_REALM, segments=())

    assert realm_error.value.code == "invalid_realm"

    with pytest.raises(AuditValueError) as scope_error:
        replace(
            _draft("unknown_realm"),
            requested_scope=Scope(realm="local", segments=()),
        )

    assert scope_error.value.code == "instance_scope_forbidden"


def test_unknown_realm_append_fails_closed_without_creating_authority_state(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    draft = replace(
        _draft("unknown_realm"),
        chain_kind=ChainKind.REALM,
        chain_identity="ghost",
        outcome=Outcome.DENY,
    )

    with pytest.raises(CatalogueTransactionError) as caught:
        store.append_audit(draft)

    assert caught.value.code == "audit_chain_unavailable"
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute("SELECT count(*) FROM realms").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT chain_kind, chain_identity, last_sequence FROM audit_heads"
        ).fetchall() == [("instance", str(INSTANCE_ID), 0)]


def _uuid_sequence() -> Callable[[], UUID]:
    counter = itertools.count(1)
    return lambda: UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")


def _encode_text_result(value: str, receipt: MutationReceipt) -> bytes:
    return json.dumps(
        {
            "mutation_receipt": {
                "command_digest": receipt.command_digest.hex(),
                "mutation_id": str(receipt.mutation_id),
            },
            "value": value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


_OPERATIONS = st.from_regex(r"[a-z][a-z0-9-]{0,20}[a-z0-9]", fullmatch=True)
_RESULT_TEXT = st.text(st.characters(exclude_categories=["Cs"]), max_size=40)


@settings(max_examples=25, deadline=None)
@given(
    principal_id=st.uuids(version=4),
    operation=_OPERATIONS,
    idempotency_key=st.uuids(version=4),
    command_digest=st.binary(min_size=32, max_size=32),
    value=_RESULT_TEXT,
)
def test_replay_returns_the_exact_committed_result_for_any_command(
    principal_id: UUID,
    operation: str,
    idempotency_key: UUID,
    command_digest: bytes,
    value: str,
) -> None:
    with TemporaryDirectory() as raw_path:
        data_path = Path(raw_path)
        migrate_catalogue(_config(data_path), lambda: NOW)
        store = CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=lambda: NOW + timedelta(seconds=1),
            uuid_factory=_uuid_sequence(),
        )

        def attempt(
            reason_code: str,
            digest: bytes,
        ) -> Committed[str] | Replayed[str] | Rejected:
            return store.mutate_idempotent(
                _draft(reason_code),
                principal_id=principal_id,
                operation=operation,
                idempotency_key=idempotency_key,
                command_digest=digest,
                result_schema="cairn.result/test-v1",
                mutation=lambda _transaction: value,
                encode=_encode_text_result,
                decode=_decode_result,
            )

        committed = attempt("first_write", command_digest)
        replayed = attempt("replayed_write", command_digest)

        assert isinstance(committed, Committed)
        assert isinstance(replayed, Replayed)
        assert committed.value == value
        assert replayed.value == value
        assert replayed.mutation_receipt == committed.mutation_receipt
        connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
        try:
            assert connection.execute(
                "SELECT count(*) FROM idempotency_records"
            ).fetchone() == (1,)
        finally:
            connection.close()


@settings(max_examples=25, deadline=None)
@given(
    principal_id=st.uuids(version=4),
    operation=_OPERATIONS,
    idempotency_key=st.uuids(version=4),
    first_digest=st.binary(min_size=32, max_size=32),
    second_digest=st.binary(min_size=32, max_size=32),
)
def test_reused_idempotency_key_with_another_command_always_conflicts(
    principal_id: UUID,
    operation: str,
    idempotency_key: UUID,
    first_digest: bytes,
    second_digest: bytes,
) -> None:
    assume(first_digest != second_digest)
    with TemporaryDirectory() as raw_path:
        data_path = Path(raw_path)
        migrate_catalogue(_config(data_path), lambda: NOW)
        store = CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=lambda: NOW + timedelta(seconds=1),
            uuid_factory=_uuid_sequence(),
        )

        def attempt(
            reason_code: str,
            digest: bytes,
        ) -> Committed[str] | Replayed[str] | Rejected:
            return store.mutate_idempotent(
                _draft(reason_code),
                principal_id=principal_id,
                operation=operation,
                idempotency_key=idempotency_key,
                command_digest=digest,
                result_schema="cairn.result/test-v1",
                mutation=lambda _transaction: "stored",
                encode=_encode_text_result,
                decode=_decode_result,
            )

        committed = attempt("first_write", first_digest)
        rejected = attempt("conflicting_write", second_digest)

        assert isinstance(committed, Committed)
        assert isinstance(rejected, Rejected)
        assert rejected.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
        assert rejected.failure.retry is RetryClass.NEVER
        connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
        try:
            assert connection.execute(
                "SELECT command_digest, mutation_id FROM idempotency_records"
            ).fetchall() == [
                (first_digest, str(committed.mutation_receipt.mutation_id))
            ]
        finally:
            connection.close()


def test_mutation_transaction_query_sees_uncommitted_writes_and_expires(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )
    captured: list[tuple[tuple[object, ...], ...]] = []
    holder: list[_MutationTransaction] = []

    def mutation(transaction: _MutationTransaction) -> str:
        holder.append(transaction)
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("created", "2026-08-05T12:00:01.000000Z"),
        )
        captured.append(
            transaction.query(
                "SELECT realm_id FROM realms WHERE realm_id = ?",
                ("created",),
            )
        )
        return "created"

    _mutate(store, draft, mutation)

    assert captured == [(("created",),)]
    with pytest.raises(CatalogueTransactionError) as caught:
        holder[0].query("SELECT 1")
    assert caught.value.code == "transaction_expired"


def test_mutation_rejection_rolls_back_and_records_durable_denial(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    calls = 0
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=_uuid_sequence(),
    )
    draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )
    failure = StableFailure(
        code=FailureCode.AUTHORISATION_DENIED,
        safe_message="The operation is not authorised.",
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        retry=RetryClass.NEVER,
    )
    denial_draft = replace(
        draft,
        outcome=Outcome.DENY,
        reason_code="authorisation_denied",
    )

    def reject(transaction: _MutationTransaction) -> str:
        nonlocal calls
        calls += 1
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("residue", "2026-08-05T12:00:01.000000Z"),
        )
        raise MutationRejection(failure, denial_draft)

    outcome = _mutate(store, draft, reject)

    assert isinstance(outcome, Rejected)
    assert outcome.failure == failure
    assert outcome.audit_receipt.sequence == 1
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM realms WHERE realm_id = 'residue'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT outcome, reason_code FROM audit_events"
        ).fetchall() == [("deny", "authorisation_denied")]
        assert calls == 1

        retried = _mutate(store, draft, reject)

        assert isinstance(retried, Rejected)
        assert calls == 2, "a retried attempt with the same key must re-evaluate"
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            2,
        )
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (0,)


def test_execute_compound_commits_two_chained_events_and_domain_rows(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: next(ids),
    )
    first_draft = replace(
        _draft("first_event"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )
    second_draft = replace(
        _draft("second_event"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    def work(transaction: CompoundTransaction) -> tuple[AuditReceipt, AuditReceipt]:
        first = transaction.append(first_draft)
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("created", "2026-08-05T12:00:01.000000Z"),
        )
        assert transaction.query(
            "SELECT realm_id FROM realms WHERE realm_id = ?", ("created",)
        ) == (("created",),)
        second = transaction.append(second_draft)
        return first, second

    first, second = store.execute_compound(work)

    assert first.sequence == 1
    assert second.sequence == 2
    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            2,
        )
        assert connection.execute(
            "SELECT count(*) FROM realms WHERE realm_id = 'created'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT last_sequence FROM audit_heads WHERE chain_kind = 'realm' "
            "AND chain_identity = 'local'"
        ).fetchone() == (2,)


def test_execute_compound_failure_after_first_append_rolls_back_everything(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    first_draft = replace(
        _draft("first_event"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    def work(transaction: CompoundTransaction) -> None:
        transaction.append(first_draft)
        transaction.execute(
            "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
            ("residue", "2026-08-05T12:00:01.000000Z"),
        )
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError):
        store.execute_compound(work)

    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT count(*) FROM realms WHERE realm_id = 'residue'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT last_sequence FROM audit_heads WHERE chain_kind = 'realm' "
            "AND chain_identity = 'local'"
        ).fetchone() == (0,)


def test_execute_compound_transaction_expires_after_work_returns(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )
    holder: list[CompoundTransaction] = []

    def work(transaction: CompoundTransaction) -> None:
        holder.append(transaction)

    store.execute_compound(work)

    with pytest.raises(CatalogueTransactionError) as caught:
        holder[0].query("SELECT 1")
    assert caught.value.code == "transaction_expired"


def test_execute_compound_ambiguous_commit_fails_closed(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)

    def lose_after_commit(connection: sqlite3.Connection) -> None:
        connection.commit()
        raise CommitAmbiguity

    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
        commit=lose_after_commit,
    )

    def work(transaction: CompoundTransaction) -> None:
        transaction.append(_draft("first_event"))

    with pytest.raises(CatalogueTransactionError) as caught:
        store.execute_compound(work)

    assert caught.value.code == "commit_outcome_unknown"


def _captured_busy_error(data_path: Path) -> sqlite3.OperationalError:
    """A genuine ``SQLITE_BUSY``, contended out of SQLite itself.

    A Python-constructed ``OperationalError`` carries no
    ``sqlite_errorcode`` at all, so nothing synthetic can exercise the
    primary-result-code test the mapping makes.
    """
    holder = sqlite3.connect(
        data_path / CATALOGUE_FILENAME,
        timeout=0,
        isolation_level=None,
    )
    contender = sqlite3.connect(
        data_path / CATALOGUE_FILENAME,
        timeout=0,
        isolation_level=None,
    )
    try:
        holder.execute("BEGIN IMMEDIATE")
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            return error
        raise AssertionError("the contended BEGIN IMMEDIATE did not report busy")
    finally:
        holder.close()
        contender.close()


def test_a_busy_commit_or_statement_is_typed_contention(tmp_path: Path) -> None:
    """I-49 promises the busy timeout becomes a typed retryable failure,
    not that ``BEGIN IMMEDIATE`` does. A checkpointer or a second process
    can make a statement, the commit or the rollback time out; mapped
    only at the ``BEGIN``, those surfaced raw and the REST middleware
    rendered them as the 500 the decision exists to eliminate."""
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    _add_realm_head(tmp_path, "local")
    busy = _captured_busy_error(tmp_path)

    def busy_commit(connection: sqlite3.Connection) -> None:
        raise busy

    committing = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
        commit=busy_commit,
    )
    realm_draft = replace(
        _draft("realm_created"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
    )

    with pytest.raises(CatalogueContention):
        _mutate(committing, realm_draft, lambda _transaction: "created")

    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=lambda: UUID("44444444-4444-4444-8444-444444444444"),
    )

    def busy_statement(transaction: CompoundTransaction) -> None:
        raise busy

    with pytest.raises(CatalogueContention):
        store.execute_compound(busy_statement)


def test_a_non_contention_operational_error_stays_raw(tmp_path: Path) -> None:
    """The other half of the mapping: only the busy and locked primary
    codes are contention. Anything else is a defect, and labelling it
    retryable would mislead every caller."""
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW,
        uuid_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
    )

    def malformed(transaction: CompoundTransaction) -> None:
        transaction.execute("SELECT * FROM a_table_that_does_not_exist")

    with pytest.raises(sqlite3.OperationalError):
        store.execute_compound(malformed)


def test_writer_gate_serialises_compound_and_idempotent_mutations(
    tmp_path: Path,
) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    store = CatalogueTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW + timedelta(seconds=1),
        uuid_factory=_uuid_sequence(),
    )

    def run_compound(index: int) -> None:
        def work(transaction: CompoundTransaction) -> None:
            transaction.append(_draft(f"compound_first_{index}"))
            transaction.append(_draft(f"compound_second_{index}"))

        store.execute_compound(work)

    def run_idempotent(index: int) -> None:
        store.mutate_idempotent(
            _draft(f"idempotent_{index}"),
            principal_id=UUID("66666666-6666-4666-8666-666666666666"),
            operation="parallel-op",
            idempotency_key=UUID(f"{index:08x}-0000-4999-8000-000000000000"),
            command_digest=bytes.fromhex("11" * 32),
            result_schema="cairn.result/test-v1",
            mutation=lambda _transaction: "created",
            encode=_encode_result,
            decode=_decode_result,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_compound, index) for index in range(4)]
        futures += [executor.submit(run_idempotent, index) for index in range(4)]
        for future in futures:
            future.result()

    with closing(sqlite3.connect(tmp_path / CATALOGUE_FILENAME)) as connection:
        sequences = [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM audit_events WHERE chain_kind = 'instance' "
                "ORDER BY sequence"
            ).fetchall()
        ]
        assert sequences == list(range(1, 13))
