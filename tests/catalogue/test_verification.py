import hashlib
import itertools
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import (
    CairnAuthority,
    ExternalEvidenceReference,
    IngestAssertion,
    InvalidateFacts,
    PromoteFacts,
)
from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    ChainKind,
    Classification,
    Outcome,
    Scope,
    ScopeSegment,
    TrustClass,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    MutationReceipt,
)
from cairn.catalogue.verification import (
    _CLASSIFICATIONS,
    _GRANT_OPERATIONS,
    VerificationError,
    VerificationReport,
    _rebuild_scope_index,
    verify_catalogue,
)
from cairn.evidence.attic import SqliteAttic
from cairn.evidence.delivery import deliver_evidence_outbox
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.screening import SecretScreen

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

_MANAGER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MANAGER_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_WORKLOAD_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_WORKLOAD_CREDENTIAL_ID = UUID("cccccccc-dddd-4ccc-8ccc-cccccccccccc")
_MANAGER_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_DELEGATED_GRANT_ID = UUID("eeeeeeee-2222-4eee-8eee-eeeeeeeeeeee")
_WORKLOAD_GRANT_ID = UUID("eeeeeeee-3333-4eee-8eee-eeeeeeeeeeee")
_NONEXISTENT_ID = UUID("99999999-9999-4999-8999-999999999999")
_TAMPER_ID = UUID("10000000-0000-4000-8000-000000000099")

# Identities the custody fixture mints, pinned here so a tamper statement can
# name a row rather than a subquery. _seed_custody_catalogue asserts each one
# against the real receipt: if the seeding ever drifts, that assertion fails
# loudly rather than leaving every tamper case quietly aimed at nothing.
_ASSERTION_ID = UUID("30001000-0000-4000-8000-000000000000")
_SOURCE_FACT_ID = UUID("30001001-0000-4000-8000-000000000000")
_EXACT_EVIDENCE_ID = UUID("30001002-0000-4000-8000-000000000000")
_EXTERNAL_EVIDENCE_ID = UUID("40001000-0000-4000-8000-000000000000")
_PROMOTED_FACT_ID = UUID("40001001-0000-4000-8000-000000000000")
_OTHER_ASSERTION_ID = UUID("20000000-0000-4000-8000-000000000001")
_OTHER_FACT_ID = UUID("20000000-0000-4000-8000-000000000002")
_SECOND_TAMPER_ID = UUID("10000000-0000-4000-8000-000000000098")
# Passes every CHECK in migration 0003 (length 36, GLOB-legal, charset limited
# to hex digits and dashes) and still raises ValueError from UUID(), because
# the GLOB's `?` wildcards accept a dash where a hex digit belongs.
_MALFORMED_UUID = "--------------4----8----------------"

_CUSTODY_AGENT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_CUSTODY_CREDENTIAL_ID = UUID("dddddddd-eeee-4ddd-8ddd-dddddddddddd")
_CUSTODY_GRANT_ID = UUID("eeeeeeee-4444-4eee-8eee-eeeeeeeeeeee")
_CUSTODY_SCOPE = Scope(
    realm="local", segments=(ScopeSegment(kind="job", identifier="job-1"),)
)
_EVIDENCE_PAYLOAD = b"the build log"
_EXTERNAL_URI = "https://example.test/evidence/build-log"


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _values[T](values: list[T]) -> Iterator[T]:
    yield from values


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


def _seed_catalogue(data_path: Path) -> None:
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(
        "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
        ("local", "2026-08-05T12:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', 'local', 0, ?)",
        (bytes(32),),
    )
    connection.commit()
    connection.close()
    ids = _values(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
            UUID("88888888-8888-4888-8888-888888888888"),
            UUID("99999999-9999-4999-8999-999999999999"),
        ]
    )
    times = _values([NOW + timedelta(seconds=index) for index in range(1, 5)])
    store = CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: next(times),
        uuid_factory=lambda: next(ids),
    )
    store.append_audit(_draft("first_check"))
    store.append_audit(_draft("second_check"))
    realm = replace(
        _draft("realm_check"),
        chain_kind=ChainKind.REALM,
        chain_identity="local",
        source_scope=Scope(realm="local", segments=()),
        requested_scope=Scope(
            realm="local",
            segments=(ScopeSegment(kind="job", identifier="job/1"),),
        ),
    )
    store.append_audit(realm)
    store.mutate_idempotent(
        _draft("mutation_check"),
        principal_id=UUID("66666666-6666-4666-8666-666666666666"),
        operation="catalogue-check",
        idempotency_key=UUID("77777777-7777-5777-8777-777777777777"),
        command_digest=bytes.fromhex("11" * 32),
        result_schema="cairn.result/test-v1",
        mutation=lambda _transaction: "checked",
        encode=_encode_result,
        decode=_decode_result,
    )


def _segments_json(segments: list[dict[str, str]]) -> str:
    return json.dumps(
        segments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _set_json(values: list[str]) -> str:
    return json.dumps(
        sorted(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _row(template: str, baseline: dict[str, object], **changes: object) -> str:
    """One INSERT differing from a proven-clean baseline in named columns
    only. Used by the authority and custody tamper cases alike, so it sits
    above both."""
    return template.format(**{**baseline, **changes})


def _seed_authority_catalogue(data_path: Path) -> None:
    """A realm with a manager (grant-manage + delegable data grant), a
    delegated grant, a workload with a live grant, and one revocation of
    each kind — all mutually consistent."""
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(
        "INSERT INTO realms(realm_id, created_at) VALUES (?, ?)",
        ("local", "2026-08-05T12:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', 'local', 0, ?)",
        (bytes(32),),
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, 'human', 'manager', ?)",
        (str(_MANAGER_ID), "2026-08-05T12:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, 'workload', 'worker', ?)",
        (str(_WORKLOAD_ID), "2026-08-05T12:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO credentials "
        "(credential_id, principal_id, verifier, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (
            str(_MANAGER_CREDENTIAL_ID),
            str(_MANAGER_ID),
            hashlib.sha256(b"manager").digest(),
            "2026-08-05T12:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO credentials "
        "(credential_id, principal_id, verifier, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(_WORKLOAD_CREDENTIAL_ID),
            str(_WORKLOAD_ID),
            hashlib.sha256(b"worker").digest(),
            "2026-08-05T12:00:00.000000Z",
            "2027-08-05T12:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, "
        "delegable_operations, issued_by, expires_at, created_at) "
        "VALUES (?, ?, 'local', ?, ?, 'restricted', ?, ?, NULL, NULL, ?)",
        (
            str(_MANAGER_GRANT_ID),
            str(_MANAGER_ID),
            _segments_json([]),
            _set_json(["grant-manage"]),
            _set_json(["public", "internal", "restricted"]),
            _set_json(["retrieve", "audit-read"]),
            "2026-08-05T12:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, "
        "delegable_operations, issued_by, expires_at, created_at) "
        "VALUES (?, ?, 'local', ?, ?, 'internal', ?, NULL, ?, NULL, ?)",
        (
            str(_DELEGATED_GRANT_ID),
            str(_MANAGER_ID),
            _segments_json([{"id": "acme-repo", "kind": "repository"}]),
            _set_json(["retrieve"]),
            _set_json(["public"]),
            str(_MANAGER_ID),
            "2026-08-05T12:00:01.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, "
        "delegable_operations, issued_by, expires_at, created_at) "
        "VALUES (?, ?, 'local', ?, ?, 'public', ?, NULL, ?, ?, ?)",
        (
            str(_WORKLOAD_GRANT_ID),
            str(_WORKLOAD_ID),
            _segments_json([]),
            _set_json(["retrieve"]),
            _set_json(["public"]),
            str(_MANAGER_ID),
            "2027-08-05T12:00:00.000000Z",
            "2026-08-05T12:00:02.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO credential_revocations "
        "(credential_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
        (
            str(_WORKLOAD_CREDENTIAL_ID),
            "2026-08-05T12:00:03.000000Z",
            str(_MANAGER_ID),
            "rotated",
        ),
    )
    connection.execute(
        "INSERT INTO grant_revocations "
        "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, ?, ?)",
        (
            str(_DELEGATED_GRANT_ID),
            "2026-08-05T12:00:04.000000Z",
            str(_MANAGER_ID),
            "superseded",
        ),
    )
    connection.commit()
    connection.close()


def _uuid_seq(start: int) -> Callable[[], UUID]:
    counter = itertools.count(start)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def _custody_authority(data_path: Path, seed: int, now: datetime) -> CairnAuthority:
    """One authority per mutation, each with its own identity sequences.

    Sharing a ``uuid_factory`` across two mutations is not an option: a second
    authority built from the same seed restarts the audit ``event_id``
    sequence where the first began and collides on the very uniqueness
    ``_verify_audit`` checks.
    """
    return CairnAuthority(
        data_path,
        CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=lambda: now,
            uuid_factory=_uuid_seq(seed),
        ),
        clock=lambda: now,
        uuid_factory=_uuid_seq(seed + 0x1000),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )


def _seed_custody_catalogue(data_path: Path) -> None:
    """One full custody lifecycle written through ``CairnAuthority``: an
    ingest carrying an exact-evidence payload, a promotion taking inline
    external evidence, and an invalidation of the source superseded by the
    promoted fact.

    Nothing here writes a custody row directly. The intact case is worth
    little if the fixture and the verifier are two readings of the schema
    rather than the writer and the verifier disagreeing where they should.
    """
    config = _config(data_path)
    migrate_catalogue(config, lambda: NOW)
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(
        "INSERT INTO realms(realm_id, created_at) VALUES ('local', ?)",
        ("2026-08-05T12:00:00.000000Z",),
    )
    connection.execute(
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', 'local', 0, ?)",
        (bytes(32),),
    )
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, ?, 'agent', ?)",
        (
            str(_CUSTODY_AGENT_ID),
            PrincipalKind.WORKLOAD.value,
            "2026-08-05T12:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO credentials "
        "(credential_id, principal_id, verifier, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (
            str(_CUSTODY_CREDENTIAL_ID),
            str(_CUSTODY_AGENT_ID),
            hashlib.sha256(b"agent").digest(),
            "2026-08-05T12:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, "
        "delegable_operations, issued_by, expires_at, created_at) "
        "VALUES (?, ?, 'local', ?, ?, 'restricted', ?, NULL, NULL, ?, ?)",
        (
            str(_CUSTODY_GRANT_ID),
            str(_CUSTODY_AGENT_ID),
            _segments_json([{"id": "job-1", "kind": "job"}]),
            _set_json(
                [
                    GrantOperation.INGEST.value,
                    GrantOperation.INVALIDATE.value,
                    GrantOperation.PROMOTE.value,
                    GrantOperation.RETRIEVE.value,
                ]
            ),
            _set_json([classification.value for classification in Classification]),
            "2027-08-05T12:00:00.000000Z",
            "2026-08-05T12:00:00.000000Z",
        ),
    )
    connection.commit()
    connection.close()

    actor = Actor(principal_id=_CUSTODY_AGENT_ID, credential_id=_CUSTODY_CREDENTIAL_ID)
    ingested = _custody_authority(data_path, 0x30000000, NOW).ingest(
        actor,
        IngestAssertion(
            scope=_CUSTODY_SCOPE,
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=(
                FactDraft(body="the build is green", valid_from=None, valid_to=None),
            ),
            requested_trust=TrustClass.CANDIDATE,
            metadata='{"run":"1"}',
            evidence_payload=_EVIDENCE_PAYLOAD,
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000001"),
        correlation_id=UUID("88888888-8888-4888-8888-888888888888"),
    )
    assert isinstance(ingested, Committed)
    source_fact_id = ingested.value.fact_ids[0]
    assert (
        ingested.value.assertion_id,
        source_fact_id,
        ingested.value.evidence_id,
    ) == (
        _ASSERTION_ID,
        _SOURCE_FACT_ID,
        _EXACT_EVIDENCE_ID,
    )

    promoted = _custody_authority(
        data_path, 0x40000000, NOW + timedelta(seconds=1)
    ).promote(
        actor,
        PromoteFacts(
            fact_ids=(source_fact_id,),
            evidence=ExternalEvidenceReference(
                external_uri=_EXTERNAL_URI,
                payload_digest=hashlib.sha256(_EVIDENCE_PAYLOAD).digest(),
            ),
            target_scope=None,
            target_classification=None,
            reason="the verifier re-ran the build",
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000002"),
        correlation_id=UUID("88888888-8888-4888-8888-888888888888"),
    )
    assert isinstance(promoted, Committed)
    promoted_fact_id = promoted.value.promotions[0][1]
    assert (promoted_fact_id, promoted.value.evidence_id) == (
        _PROMOTED_FACT_ID,
        _EXTERNAL_EVIDENCE_ID,
    )

    invalidated = _custody_authority(
        data_path, 0x50000000, NOW + timedelta(seconds=2)
    ).invalidate(
        actor,
        InvalidateFacts(
            fact_ids=(source_fact_id,),
            reason="superseded by the promoted fact",
            superseded_by=promoted_fact_id,
        ),
        idempotency_key=UUID("70000000-0000-4000-8000-000000000003"),
        correlation_id=UUID("88888888-8888-4888-8888-888888888888"),
    )
    assert isinstance(invalidated, Committed)


def _copy_catalogue(source: Path, target: Path) -> CairnConfig:
    target.mkdir()
    source_connection = sqlite3.connect(source / CATALOGUE_FILENAME)
    target_connection = sqlite3.connect(target / CATALOGUE_FILENAME)
    source_connection.backup(target_connection)
    target_connection.close()
    source_connection.close()
    os.chmod(target / CATALOGUE_FILENAME, 0o660)
    return _config(target)


@pytest.fixture
def copied_catalogue(tmp_path: Path) -> Callable[[str], CairnConfig]:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalogue(source)

    def copy(name: str) -> CairnConfig:
        return _copy_catalogue(source, tmp_path / name)

    return copy


@pytest.fixture
def copied_authority_catalogue(tmp_path: Path) -> Callable[[str], CairnConfig]:
    source = tmp_path / "authority-source"
    source.mkdir()
    _seed_authority_catalogue(source)

    def copy(name: str) -> CairnConfig:
        return _copy_catalogue(source, tmp_path / name)

    return copy


@pytest.fixture
def copied_custody_catalogue(tmp_path: Path) -> Callable[[str], CairnConfig]:
    source = tmp_path / "custody-source"
    source.mkdir()
    _seed_custody_catalogue(source)

    def copy(name: str) -> CairnConfig:
        return _copy_catalogue(source, tmp_path / name)

    return copy


def _write(config: CairnConfig, *statements: str, ignore_checks: bool = False) -> None:
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    if ignore_checks:
        connection.execute("PRAGMA ignore_check_constraints = 1")
    restore: list[str] = []
    replacement_triggers = {
        statement.split()[2]
        for statement in statements
        if statement.startswith("CREATE TRIGGER ")
    }
    for statement in statements:
        if statement.startswith("DROP TRIGGER "):
            trigger = statement.removeprefix("DROP TRIGGER ")
            row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
                (trigger,),
            ).fetchone()
            assert row is not None
            if trigger not in replacement_triggers:
                restore.append(row[0])
        connection.execute(statement)
    for statement in restore:
        connection.execute(statement)
    connection.commit()
    connection.close()


def _write_with_foreign_keys_off(config: CairnConfig, *statements: str) -> None:
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    connection.execute("PRAGMA foreign_keys = OFF")
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()


def _assert_failure(config: CairnConfig, code: str) -> None:
    with pytest.raises(VerificationError) as caught:
        verify_catalogue(config)
    assert caught.value.code == code


def test_intact_catalogue_verifies_without_repair(tmp_path: Path) -> None:
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)

    assert verify_catalogue(config) == VerificationReport(
        schema_version=13,
        instance_id=INSTANCE_ID,
        realm_count=0,
        event_count=0,
        idempotency_count=0,
        principal_count=0,
        credential_count=0,
        grant_count=0,
        assertion_count=0,
        fact_count=0,
        invalidation_count=0,
        evidence_count=0,
        evidence_outbox_depth=0,
        projection_outbox_depth=0,
    )


def test_populated_catalogue_reports_verified_inventory(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    assert verify_catalogue(copied_catalogue("intact")) == VerificationReport(
        schema_version=13,
        instance_id=INSTANCE_ID,
        realm_count=1,
        event_count=4,
        idempotency_count=1,
        principal_count=0,
        credential_count=0,
        grant_count=0,
        assertion_count=0,
        fact_count=0,
        invalidation_count=0,
        evidence_count=0,
        evidence_outbox_depth=0,
        projection_outbox_depth=0,
    )


@pytest.mark.parametrize(
    ("name", "statements", "code"),
    [
        (
            "event-mutation",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "UPDATE audit_events SET action_code = 'altered' "
                "WHERE chain_kind = 'instance' AND sequence = 1",
            ),
            "audit_projection_invalid",
        ),
        (
            "event-deletion",
            (
                "DROP TRIGGER trg_audit_events_no_delete",
                "DELETE FROM audit_events WHERE chain_kind = 'instance' "
                "AND sequence = 2",
            ),
            "audit_chain_invalid",
        ),
        (
            "event-insertion",
            (
                "INSERT INTO audit_events SELECT chain_kind, chain_identity, 99, "
                "'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', recorded_at, "
                "previous_hash, event_hash, action_kind, action_code, outcome, "
                "reason_code, canonical_event FROM audit_events "
                "WHERE chain_kind = 'realm' AND sequence = 1",
            ),
            "audit_event_invalid",
        ),
        (
            "sequence-change",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "UPDATE audit_events SET sequence = 9 "
                "WHERE chain_kind = 'instance' AND sequence = 2",
            ),
            "audit_projection_invalid",
        ),
        (
            "previous-hash-change",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "UPDATE audit_events SET previous_hash = zeroblob(32) "
                "WHERE chain_kind = 'instance' AND sequence = 2",
            ),
            "audit_projection_invalid",
        ),
        (
            "reordering",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "UPDATE audit_events SET canonical_event = "
                "(SELECT canonical_event FROM audit_events "
                "WHERE chain_kind = 'instance' AND sequence = 1) "
                "WHERE chain_kind = 'instance' AND sequence = 2",
            ),
            "audit_event_invalid",
        ),
        (
            "stale-head",
            ("UPDATE audit_heads SET last_sequence = 1 WHERE chain_kind = 'instance'",),
            "audit_head_invalid",
        ),
        (
            "fabricated-head",
            (
                "UPDATE audit_heads SET last_hash = x'22222222222222222222"
                "22222222222222222222222222222222222222222222' "
                "WHERE chain_kind = 'realm'",
            ),
            "audit_head_invalid",
        ),
        (
            "canonical-byte-change",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "UPDATE audit_events SET canonical_event = "
                "CAST(canonical_event || x'20' AS BLOB) "
                "WHERE chain_kind = 'realm'",
            ),
            "audit_event_invalid",
        ),
        (
            "scope-index-drift",
            (
                "DELETE FROM audit_scope_index WHERE chain_kind = 'realm' "
                "AND role = 'source'",
            ),
            "scope_index_invalid",
        ),
        (
            "metadata",
            (
                "DROP TRIGGER trg_catalogue_metadata_no_update",
                "UPDATE catalogue_metadata SET instance_id = "
                "'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'",
            ),
            "instance_mismatch",
        ),
        (
            "migration",
            (
                "DROP TRIGGER trg_schema_migrations_no_update",
                "UPDATE schema_migrations SET sql_sha256 = zeroblob(32)",
            ),
            "migration_history_invalid",
        ),
        (
            "idempotency-result",
            (
                "DROP TRIGGER trg_idempotency_records_no_update",
                "UPDATE idempotency_records SET result_bytes = x'7b7d'",
            ),
            "idempotency_invalid",
        ),
        (
            "schema-drift",
            ("DROP INDEX ix_audit_events_recorded_at",),
            "catalogue_schema_invalid",
        ),
    ],
)
def test_tampering_is_detected_without_repair(
    copied_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
    code: str,
) -> None:
    config = copied_catalogue(name)
    _write(config, *statements)

    _assert_failure(config, code)


@pytest.mark.parametrize(
    ("name", "statements"),
    [
        (
            "same-name-index-drift",
            (
                "DROP INDEX ix_audit_events_recorded_at",
                "CREATE INDEX ix_audit_events_recorded_at ON audit_events(action_code)",
            ),
        ),
        (
            "same-name-trigger-drift",
            (
                "DROP TRIGGER trg_audit_events_no_update",
                "CREATE TRIGGER trg_audit_events_no_update "
                "BEFORE UPDATE ON audit_events BEGIN SELECT 1; END",
            ),
        ),
    ],
)
def test_same_name_schema_definition_tampering_is_detected(
    copied_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
) -> None:
    config = copied_catalogue(name)
    _write(config, *statements)

    _assert_failure(config, "catalogue_schema_invalid")


def test_event_failures_precede_projection_failures_globally(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_catalogue("verification-order")
    _write(
        config,
        "INSERT INTO audit_scope_index "
        "(chain_kind, chain_identity, sequence, role, ordinal) "
        f"VALUES ('instance', '{INSTANCE_ID}', 1, 'source', -1)",
        "DROP TRIGGER trg_audit_events_no_update",
        "UPDATE audit_events SET canonical_event = "
        "CAST(canonical_event || x'20' AS BLOB) "
        "WHERE chain_kind = 'realm'",
    )

    _assert_failure(config, "audit_event_invalid")


@pytest.mark.parametrize(
    ("pragma", "value", "code"),
    [
        ("application_id", 7, "application_id_mismatch"),
        ("user_version", 14, "schema_version_mismatch"),
    ],
)
def test_header_tampering_is_detected(
    copied_catalogue: Callable[[str], CairnConfig],
    pragma: str,
    value: int,
    code: str,
) -> None:
    config = copied_catalogue(pragma)
    _write(config, f"PRAGMA {pragma} = {value}")

    _assert_failure(config, code)


def test_foreign_key_corruption_is_detected_first(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_catalogue("foreign-key")
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO audit_scope_index "
        "(chain_kind, chain_identity, sequence, role, ordinal) "
        "VALUES ('realm', 'local', 88, 'source', -1)"
    )
    connection.commit()
    connection.close()

    _assert_failure(config, "foreign_key_failed")


def test_instance_mismatch_is_distinct_and_safe(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_catalogue("instance")
    wrong = config.model_copy(
        update={"instance_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")}
    )

    _assert_failure(wrong, "instance_mismatch")


def test_scope_index_rebuild_requires_an_intact_authoritative_chain(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_catalogue("rebuild")
    _write(
        config,
        "DELETE FROM audit_scope_index WHERE chain_kind = 'realm' AND role = 'source'",
    )

    _assert_failure(config, "scope_index_invalid")
    _rebuild_scope_index(config)
    assert verify_catalogue(config).event_count == 4

    _write(
        config,
        "DROP TRIGGER trg_audit_events_no_update",
        "UPDATE audit_events SET event_hash = zeroblob(32) WHERE chain_kind = 'realm'",
        "DELETE FROM audit_scope_index WHERE chain_kind = 'realm'",
    )
    with pytest.raises(VerificationError):
        _rebuild_scope_index(config)


def test_result_digest_tamper_is_detected_even_for_canonical_json(
    copied_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_catalogue("result-digest")
    replacement = b'{"mutation_receipt":{},"value":"altered"}'
    _write(
        config,
        "DROP TRIGGER trg_idempotency_records_no_update",
        "UPDATE idempotency_records SET "
        f"result_bytes = x'{replacement.hex()}', "
        f"result_digest = x'{hashlib.sha256(replacement).hexdigest()}'",
    )

    _assert_failure(config, "idempotency_invalid")


def test_intact_authority_catalogue_reports_verified_inventory(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    assert verify_catalogue(
        copied_authority_catalogue("authority-intact")
    ) == VerificationReport(
        schema_version=13,
        instance_id=INSTANCE_ID,
        realm_count=1,
        event_count=0,
        idempotency_count=0,
        principal_count=2,
        credential_count=2,
        grant_count=3,
        assertion_count=0,
        fact_count=0,
        invalidation_count=0,
        evidence_count=0,
        evidence_outbox_depth=0,
        projection_outbox_depth=0,
    )


@pytest.mark.parametrize("length", [31, 33])
def test_authority_verifier_length_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    length: int,
) -> None:
    config = copied_authority_catalogue(f"verifier-length-{length}")
    _write(
        config,
        "INSERT INTO credentials (credential_id, principal_id, verifier, "
        "created_at, expires_at) VALUES "
        f"('{_TAMPER_ID}', '{_MANAGER_ID}', zeroblob({length}), "
        "'2026-08-05T12:00:05.000000Z', NULL)",
        ignore_checks=True,
    )

    _assert_failure(config, "authority_verifier_invalid")


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "orphan-credential-principal",
            "INSERT INTO credentials (credential_id, principal_id, verifier, "
            "created_at, expires_at) VALUES "
            f"('{_TAMPER_ID}', '{_NONEXISTENT_ID}', zeroblob(32), "
            "'2026-08-05T12:00:05.000000Z', NULL)",
        ),
        (
            "orphan-grant-principal",
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) VALUES "
            f"('{_TAMPER_ID}', '{_NONEXISTENT_ID}', 'local', '[]', "
            "'[\"retrieve\"]', 'public', '[\"public\"]', NULL, NULL, "
            "NULL, '2026-08-05T12:00:05.000000Z')",
        ),
        (
            "orphan-grant-realm",
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) VALUES "
            f"('{_TAMPER_ID}', '{_MANAGER_ID}', 'nonexistent-realm', '[]', "
            "'[\"retrieve\"]', 'public', '[\"public\"]', NULL, NULL, "
            "NULL, '2026-08-05T12:00:05.000000Z')",
        ),
        (
            "orphan-credential-revocation",
            "INSERT INTO credential_revocations "
            "(credential_id, revoked_at, revoked_by, reason_code) VALUES "
            f"('{_NONEXISTENT_ID}', '2026-08-05T12:00:05.000000Z', "
            f"'{_MANAGER_ID}', 'rotated')",
        ),
        (
            "orphan-grant-revocation",
            "INSERT INTO grant_revocations "
            "(grant_id, revoked_at, revoked_by, reason_code) VALUES "
            f"('{_NONEXISTENT_ID}', '2026-08-05T12:00:05.000000Z', "
            f"'{_MANAGER_ID}', 'superseded')",
        ),
    ],
)
def test_authority_orphan_rows_are_caught_by_the_foreign_key_phase(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
) -> None:
    config = copied_authority_catalogue(name)
    _write_with_foreign_keys_off(config, statement)

    _assert_failure(config, "foreign_key_failed")


_GRANT_INSERT = (
    "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
    "operations, read_clearance, write_classifications, "
    "delegable_operations, issued_by, expires_at, created_at) VALUES "
    "('{grant_id}', '{principal_id}', 'local', '{scope_segments}', "
    "'{operations}', '{read_clearance}', '{write_classifications}', "
    "{delegable_operations}, NULL, NULL, '{created_at}')"
)


@pytest.mark.parametrize(
    ("name", "operations", "delegable_operations"),
    [
        ("present-without-grant-manage", '["retrieve"]', "'[\"retrieve\"]'"),
        ("missing-when-grant-manage-present", '["grant-manage"]', "NULL"),
        (
            "contains-grant-manage",
            '["grant-manage"]',
            "'[\"grant-manage\"]'",
        ),
    ],
)
def test_authority_delegable_operations_inconsistency_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    operations: str,
    delegable_operations: str,
) -> None:
    config = copied_authority_catalogue(name)
    _write(
        config,
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_MANAGER_ID,
            scope_segments="[]",
            operations=operations,
            read_clearance="public",
            write_classifications='["public"]',
            delegable_operations=delegable_operations,
            created_at="2026-08-05T12:00:05.000000Z",
        ),
        ignore_checks=True,
    )

    _assert_failure(config, "authority_delegable_inconsistent")


def test_authority_workload_grant_without_expiry_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_authority_catalogue("workload-expiry-missing")
    _write(
        config,
        "DROP TRIGGER trg_grants_workload_expiry",
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_WORKLOAD_ID,
            scope_segments="[]",
            operations='["retrieve"]',
            read_clearance="public",
            write_classifications='["public"]',
            delegable_operations="NULL",
            created_at="2026-08-05T12:00:05.000000Z",
        ),
    )

    _assert_failure(config, "authority_workload_expiry_missing")


@pytest.mark.parametrize(
    ("name", "scope_segments", "operations", "write_classifications"),
    [
        (
            "scope-segments-unsorted-keys",
            '[{"kind":"repository","id":"acme-repo"}]',
            '["retrieve"]',
            '["public"]',
        ),
        (
            "scope-segments-spaced",
            '[{"id": "acme-repo", "kind": "repository"}]',
            '["retrieve"]',
            '["public"]',
        ),
        (
            "scope-segments-duplicate-key",
            '[{"id":"x","id":"y","kind":"repository"}]',
            '["retrieve"]',
            '["public"]',
        ),
        (
            "operations-unsorted",
            "[]",
            '["retrieve","ingest"]',
            '["public"]',
        ),
        (
            "write-classifications-spaced",
            "[]",
            '["retrieve"]',
            '["public", "internal"]',
        ),
    ],
)
def test_authority_non_canonical_json_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    scope_segments: str,
    operations: str,
    write_classifications: str,
) -> None:
    config = copied_authority_catalogue(name)
    _write(
        config,
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_MANAGER_ID,
            scope_segments=scope_segments,
            operations=operations,
            read_clearance="public",
            write_classifications=write_classifications,
            delegable_operations="NULL",
            created_at="2026-08-05T12:00:05.000000Z",
        ),
    )

    _assert_failure(config, "authority_json_not_canonical")


def test_authority_delegable_operations_non_canonical_json_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_authority_catalogue("delegable-non-canonical")
    _write(
        config,
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_MANAGER_ID,
            scope_segments="[]",
            operations='["grant-manage"]',
            read_clearance="public",
            write_classifications='["public"]',
            delegable_operations='\'["retrieve","audit-read"]\'',
            created_at="2026-08-05T12:00:05.000000Z",
        ),
    )

    _assert_failure(config, "authority_json_not_canonical")


def test_authority_invalid_principal_label_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_authority_catalogue("invalid-label")
    _write(
        config,
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        f"VALUES ('{_TAMPER_ID}', 'human', 'Bad_Label', "
        "'2026-08-05T12:00:05.000000Z')",
        ignore_checks=True,
    )

    _assert_failure(config, "authority_value_invalid")


def test_authority_invalid_timestamp_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_authority_catalogue("invalid-timestamp")
    _write(
        config,
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_MANAGER_ID,
            scope_segments="[]",
            operations='["retrieve"]',
            read_clearance="public",
            write_classifications='["public"]',
            delegable_operations="NULL",
            created_at="2026-08-05 12:00:05.000000Z",
        ),
        ignore_checks=True,
    )

    _assert_failure(config, "authority_value_invalid")


def test_authority_invalid_clearance_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_authority_catalogue("invalid-clearance")
    _write(
        config,
        _GRANT_INSERT.format(
            grant_id=_TAMPER_ID,
            principal_id=_MANAGER_ID,
            scope_segments="[]",
            operations='["retrieve"]',
            read_clearance="top-secret",
            write_classifications='["public"]',
            delegable_operations="NULL",
            created_at="2026-08-05T12:00:05.000000Z",
        ),
        ignore_checks=True,
    )

    _assert_failure(config, "authority_value_invalid")


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "credential-revocation-before-created",
            "INSERT INTO credential_revocations "
            "(credential_id, revoked_at, revoked_by, reason_code) VALUES "
            f"('{_MANAGER_CREDENTIAL_ID}', '2025-01-01T00:00:00.000000Z', "
            f"'{_MANAGER_ID}', 'rotated')",
        ),
        (
            "grant-revocation-before-created",
            "INSERT INTO grant_revocations "
            "(grant_id, revoked_at, revoked_by, reason_code) VALUES "
            f"('{_MANAGER_GRANT_ID}', '2025-01-01T00:00:00.000000Z', "
            f"'{_MANAGER_ID}', 'superseded')",
        ),
    ],
)
def test_authority_revocation_before_target_created_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
) -> None:
    config = copied_authority_catalogue(name)
    _write(config, statement)

    _assert_failure(config, "authority_revocation_inconsistent")


def test_restated_grant_operations_match_the_live_enum() -> None:
    """``_GRANT_OPERATIONS`` is restated rather than imported, so nothing but
    this test stops it drifting from ``GrantOperation``.

    The drift that matters is the *additive* one: a new member would leave
    every freshly issued grant naming an operation this phase does not know,
    so ``cairn verify`` would start refusing healthy catalogues — a false
    positive, which for a verification tool is worse than the false negative
    the membership check was added to close. Tests may import across layers
    freely, so pinning it costs nothing the restatement was protecting.
    """
    assert set(_GRANT_OPERATIONS) == {operation.value for operation in GrantOperation}


def test_restated_classifications_match_the_live_enum() -> None:
    assert set(_CLASSIFICATIONS) == {
        classification.value for classification in Classification
    }


# --- credential rows the schema admits and no command can read ---------------

_MALFORMED_CREDENTIAL_ID = "--------------4----9----------------"

_TAMPER_CREDENTIAL = (
    "INSERT INTO credentials "
    "(credential_id, principal_id, verifier, created_at, expires_at) VALUES "
    "('{credential_id}', '{principal_id}', zeroblob(32), '{created_at}', NULL)"
)
_INTACT_CREDENTIAL: dict[str, object] = {
    "credential_id": _TAMPER_ID,
    "principal_id": _MANAGER_ID,
    "created_at": "2026-08-05T12:00:05.000000Z",
}

# The principals row the principal_id case needs: credentials.principal_id
# carries a foreign key, so without a real parent the foreign-key phase would
# fail first and mask the check under test. Note that _verify_principals
# deliberately does not validate this row's own identity — see its sibling
# docstring — which is exactly why the credentials phase has to.
_MALFORMED_CREDENTIAL_PRINCIPAL = (
    "INSERT INTO principals (principal_id, kind, label, created_at) VALUES "
    "('--------------4----8----------------', 'human', 'adrift', "
    "'2026-08-05T12:00:05.000000Z')"
)


@pytest.mark.parametrize(
    ("name", "statements"),
    [
        ("credential", (_row(_TAMPER_CREDENTIAL, _INTACT_CREDENTIAL),)),
        (
            "credential-with-malformed-principal",
            (
                _MALFORMED_CREDENTIAL_PRINCIPAL,
                _row(_TAMPER_CREDENTIAL, _INTACT_CREDENTIAL),
            ),
        ),
    ],
)
def test_authority_credential_baselines_verify_cleanly(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
) -> None:
    config = copied_authority_catalogue(f"credential-baseline-{name}")
    _write(config, *statements)

    verify_catalogue(config)


@pytest.mark.parametrize(
    ("name", "statements"),
    [
        # ck_credentials_credential_id is the same shape-only GLOB the grant
        # columns carry, so SQLite stores this and UUID() raises on it.
        (
            "credential-id-unparseable",
            (
                _row(
                    _TAMPER_CREDENTIAL,
                    _INTACT_CREDENTIAL,
                    credential_id=_MALFORMED_CREDENTIAL_ID,
                ),
            ),
        ),
        # credentials.principal_id carries no CHECK of its own at all. This is
        # the row that makes CredentialAuthenticator.authenticate refuse every
        # request with credential_uuid_malformed.
        (
            "principal-id-unparseable",
            (
                _MALFORMED_CREDENTIAL_PRINCIPAL,
                _row(
                    _TAMPER_CREDENTIAL,
                    _INTACT_CREDENTIAL,
                    principal_id=_MALFORMED_UUID,
                ),
            ),
        ),
    ],
)
def test_authority_credential_value_tampering_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
) -> None:
    """The same argument as the grant cases below, one table over: an
    operator whose every authenticated request fails with
    ``credential_uuid_malformed`` must not be told the catalogue is clean."""
    config = copied_authority_catalogue(f"credential-{name}")
    _write(config, *statements)

    _assert_failure(config, "authority_value_invalid")


# --- grant rows the schema admits and no command can read --------------------
#
# Each case below plants one grant row differing from _INTACT_GRANT in exactly
# one column, and every baseline is proven clean by
# test_authority_grant_baselines_verify_cleanly — a case whose row would have
# failed for a second reason proves nothing about the check it names.

_AUTHORITY_LATER = "2026-08-05T12:00:05.000000Z"

_TAMPER_GRANT = (
    "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
    "operations, read_clearance, write_classifications, delegable_operations, "
    "issued_by, expires_at, created_at) VALUES ('{grant_id}', "
    "'{principal_id}', 'local', {scope_segments}, {operations}, "
    "'{read_clearance}', {write_classifications}, {delegable_operations}, "
    "{issued_by}, {expires_at}, '{created_at}')"
)
_INTACT_GRANT: dict[str, object] = {
    "grant_id": _TAMPER_ID,
    "principal_id": _MANAGER_ID,
    "scope_segments": "'[]'",
    "operations": "'[\"retrieve\"]'",
    "read_clearance": "public",
    "write_classifications": "'[\"public\"]'",
    "delegable_operations": "NULL",
    "issued_by": "NULL",
    "expires_at": "NULL",
    "created_at": _AUTHORITY_LATER,
}

# A principal whose identity passes migration 0002's shape-only CHECK and
# still cannot be parsed. The two cases pointing a grant column at it need a
# real principals row, since principal_id and issued_by both carry a foreign
# key — without one the foreign-key phase would fail first and mask the check
# under test. Deliberately 'human': a workload principal holding a grant with
# no expires_at trips authority_workload_expiry_missing instead.
_MALFORMED_GRANT_PRINCIPAL = (
    "INSERT INTO principals (principal_id, kind, label, created_at) VALUES "
    f"('{_MALFORMED_UUID}', 'human', 'drifted', '{_AUTHORITY_LATER}')"
)

_SEVENTEEN_SEGMENTS = _segments_json(
    [{"id": f"job-{index:02d}", "kind": "job"} for index in range(17)]
)


@pytest.mark.parametrize(
    ("name", "statements"),
    [
        ("grant", (_row(_TAMPER_GRANT, _INTACT_GRANT),)),
        (
            "grant-with-malformed-principal",
            (_MALFORMED_GRANT_PRINCIPAL, _row(_TAMPER_GRANT, _INTACT_GRANT)),
        ),
    ],
)
def test_authority_grant_baselines_verify_cleanly(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
) -> None:
    """The baseline grant row, and the malformed-principal row two cases
    need, must both pass verification untouched — otherwise every case built
    on them would keep failing after its check was deleted."""
    config = copied_authority_catalogue(f"authority-baseline-{name}")
    _write(config, *statements)

    verify_catalogue(config)


@pytest.mark.parametrize(
    ("name", "statements", "ignore_checks"),
    [
        # The three UUID columns. ck_grants_grant_id and its siblings bound
        # length and character class without pinning the dashes to the
        # canonical positions, so SQLite stores these happily and UUID()
        # raises on them — no bypass needed.
        (
            "grant-id-unparseable",
            (_row(_TAMPER_GRANT, _INTACT_GRANT, grant_id=_MALFORMED_UUID),),
            False,
        ),
        (
            "principal-id-unparseable",
            (
                _MALFORMED_GRANT_PRINCIPAL,
                _row(_TAMPER_GRANT, _INTACT_GRANT, principal_id=_MALFORMED_UUID),
            ),
            False,
        ),
        (
            "issued-by-unparseable",
            (
                _MALFORMED_GRANT_PRINCIPAL,
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    issued_by=f"'{_MALFORMED_UUID}'",
                ),
            ),
            False,
        ),
        # The three enum-valued JSON columns. Their CHECKs assert valid JSON
        # and array type and nothing about the elements, so a spelling
        # outside the closed set is a row every constraint admits.
        (
            "operation-outside-the-closed-set",
            (
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    operations="'[\"not-a-real-op\"]'",
                ),
            ),
            False,
        ),
        (
            "write-classification-outside-the-closed-set",
            (
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    write_classifications="'[\"top-secret\"]'",
                ),
            ),
            False,
        ),
        (
            # operations must name grant-manage for a non-null
            # delegable_operations to satisfy the presence CHECK, so this row
            # differs from the baseline in both — but only the delegable
            # spelling is outside the closed set.
            "delegable-operation-outside-the-closed-set",
            (
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    operations="'[\"grant-manage\"]'",
                    delegable_operations="'[\"not-a-real-op\"]'",
                ),
            ),
            False,
        ),
        # scope_segments. ck_grants_scope_segments pins array shape and
        # length; the elements are unconstrained, so both of these are stored
        # without complaint and refused only when a grant is read.
        (
            "scope-segments-not-objects",
            (_row(_TAMPER_GRANT, _INTACT_GRANT, scope_segments="'[1,2,3]'"),),
            False,
        ),
        (
            "scope-segment-fields-not-strings",
            (
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    scope_segments='\'[{"id":1,"kind":7}]\'',
                ),
            ),
            False,
        ),
        # The one case the schema does refuse, kept because the bound is
        # re-derived here rather than trusted: a catalogue restored from a
        # laxer schema carries rows its CHECKs would now reject. Seventeen
        # otherwise well-formed segments, so the length is the sole fault.
        (
            "scope-segments-over-the-bound",
            (
                _row(
                    _TAMPER_GRANT,
                    _INTACT_GRANT,
                    scope_segments=f"'{_SEVENTEEN_SEGMENTS}'",
                ),
            ),
            True,
        ),
    ],
)
def test_authority_grant_value_tampering_is_detected(
    copied_authority_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
    ignore_checks: bool,
) -> None:
    """An operator whose catalogue holds one of these rows sees every
    authorising command fail with ``invalid_request``. Before this, ``cairn
    verify`` told them the catalogue was clean."""
    config = copied_authority_catalogue(name)
    _write(config, *statements, ignore_checks=ignore_checks)

    _assert_failure(config, "authority_value_invalid")


def test_intact_custody_catalogue_reports_verified_inventory(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    assert verify_catalogue(
        copied_custody_catalogue("custody-intact")
    ) == VerificationReport(
        schema_version=13,
        instance_id=INSTANCE_ID,
        realm_count=1,
        event_count=3,
        idempotency_count=3,
        principal_count=1,
        credential_count=1,
        grant_count=1,
        assertion_count=1,
        fact_count=2,
        invalidation_count=1,
        evidence_count=2,
        evidence_outbox_depth=1,
        projection_outbox_depth=3,
    )


def _extra_id(index: int) -> UUID:
    return UUID(f"{0x60000000 + index:08x}-0000-4000-8000-000000000000")


def test_every_custody_count_reports_its_own_table(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    """Six counts, six distinct values, so no pair can be swapped unseen.

    The intact fixture reports 1, 2, 1, 2, 1, 3 — three collisions — which
    leaves a report that read ``fact_count`` from the evidence tally, or any
    other permutation among the equal values, indistinguishable from a correct
    one. Rows are added here until all six differ.
    """
    config = copied_custody_catalogue("custody-distinct-counts")
    extra_evidence = [
        _row(
            _TAMPER_EVIDENCE,
            _INTACT_EVIDENCE,
            evidence_id=_extra_id(index),
            external_uri=f"'https://example.test/evidence/{index}'",
        )
        for index in range(2)
    ]
    extra_evidence_outbox = [
        _row(
            _TAMPER_EVIDENCE_OUTBOX,
            _INTACT_EVIDENCE_OUTBOX,
            work_id=_extra_id(10 + index),
        )
        for index in range(4)
    ]
    extra_projection_outbox = [
        _row(
            _TAMPER_PROJECTION_OUTBOX,
            _INTACT_PROJECTION_OUTBOX,
            work_id=_extra_id(20 + index),
        )
        for index in range(3)
    ]
    _write(
        config,
        _row(_TAMPER_ASSERTION, _INTACT_ASSERTION),
        _row(_TAMPER_FACT, _INTACT_FACT),
        *extra_evidence,
        *extra_evidence_outbox,
        *extra_projection_outbox,
    )

    report = verify_catalogue(config)

    assert (
        report.assertion_count,
        report.fact_count,
        report.invalidation_count,
        report.evidence_count,
        report.evidence_outbox_depth,
        report.projection_outbox_depth,
    ) == (2, 3, 1, 4, 5, 6)


def test_evidence_outbox_depth_drops_after_a_delivery_run(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_custody_catalogue("custody-delivered")
    report = deliver_evidence_outbox(
        CatalogueTransactions(
            config.paths.data,
            writer_gate=threading.Lock(),
            clock=lambda: NOW,
            uuid_factory=lambda: _TAMPER_ID,
        ),
        SqliteAttic(config.paths.data),
        clock=lambda: NOW,
    )

    assert (report.delivered, report.failed, report.remaining) == (1, 0, 0)
    verified = verify_catalogue(config)
    assert verified.evidence_outbox_depth == 0
    # Delivery drains payloads; it must not touch the durable record or the
    # content-free projection queue.
    assert verified.evidence_count == 2
    assert verified.projection_outbox_depth == 3


# --- custody tamper kit ------------------------------------------------------
#
# Every custody tamper case is one baseline row plus exactly one changed
# field. The baselines are themselves verified clean by
# test_custody_tamper_baselines_verify_cleanly below, which is what makes each
# case's single fault the sole reason its verification fails — a check proven
# by a fixture that would have failed anyway proves nothing.

_SEGMENTS = '\'[{"id":"job-1","kind":"job"}]\''
_LATER = "2026-08-05T12:00:05.000000Z"

_TAMPER_ASSERTION = (
    "INSERT INTO assertions (assertion_id, realm_id, scope_segments, "
    "classification, source_type, principal_id, observed_at, metadata, "
    "recorded_at) VALUES ('{assertion_id}', '{realm_id}', {scope_segments}, "
    "'{classification}', '{source_type}', '{principal_id}', {observed_at}, "
    "{metadata}, '{recorded_at}')"
)
_INTACT_ASSERTION: dict[str, object] = {
    "assertion_id": _TAMPER_ID,
    "realm_id": "local",
    "scope_segments": _SEGMENTS,
    "classification": "internal",
    "source_type": "agent-claim",
    "principal_id": _CUSTODY_AGENT_ID,
    "observed_at": "NULL",
    "metadata": "NULL",
    "recorded_at": _LATER,
}

_TAMPER_FACT = (
    "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
    "classification, assertion_id, derived_from, promoted_by, evidence_id, "
    "valid_from, valid_to, recorded_at) VALUES ('{fact_id}', '{realm_id}', "
    "{scope_segments}, {body}, '{trust}', '{classification}', {assertion_id}, "
    "{derived_from}, {promoted_by}, {evidence_id}, {valid_from}, {valid_to}, "
    "'{recorded_at}')"
)
_INTACT_FACT: dict[str, object] = {
    "fact_id": _TAMPER_ID,
    "realm_id": "local",
    "scope_segments": _SEGMENTS,
    "body": "'a stored body'",
    "trust": "candidate",
    "classification": "internal",
    "assertion_id": f"'{_ASSERTION_ID}'",
    "derived_from": "NULL",
    "promoted_by": "NULL",
    "evidence_id": "NULL",
    "valid_from": "NULL",
    "valid_to": "NULL",
    "recorded_at": _LATER,
}

_INTACT_PROMOTED_FACT: dict[str, object] = {
    **_INTACT_FACT,
    "trust": "validated",
    "assertion_id": "NULL",
    "derived_from": f"'{_SOURCE_FACT_ID}'",
    "promoted_by": f"'{_CUSTODY_AGENT_ID}'",
    "evidence_id": f"'{_EXACT_EVIDENCE_ID}'",
}

_TAMPER_EVIDENCE = (
    "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
    "classification, payload_digest, assertion_id, payload_length, "
    "external_uri, recorded_at) VALUES ('{evidence_id}', '{realm_id}', "
    "{scope_segments}, '{classification}', {payload_digest}, {assertion_id}, "
    "{payload_length}, {external_uri}, '{recorded_at}')"
)
_INTACT_EVIDENCE: dict[str, object] = {
    "evidence_id": _TAMPER_ID,
    "realm_id": "local",
    "scope_segments": _SEGMENTS,
    "classification": "internal",
    "payload_digest": "zeroblob(32)",
    "assertion_id": "NULL",
    "payload_length": "NULL",
    "external_uri": "'https://example.test/evidence/extra'",
    "recorded_at": _LATER,
}

_INTACT_EXACT_EVIDENCE: dict[str, object] = {
    **_INTACT_EVIDENCE,
    "assertion_id": f"'{_ASSERTION_ID}'",
    "payload_length": str(len(_EVIDENCE_PAYLOAD)),
    "external_uri": "NULL",
}

# An outbox row that agrees with the exact-custody evidence record in every
# way the verifier checks, so a case need only disturb the one field it is
# about.
_TAMPER_EVIDENCE_OUTBOX = (
    "INSERT INTO evidence_outbox (work_id, kind, evidence_id, mutation_id, "
    "payload, created_at, attempts) VALUES ('{work_id}', '{kind}', "
    "'{evidence_id}', {mutation_id}, {payload}, '{created_at}', {attempts})"
)
_INTACT_EVIDENCE_OUTBOX: dict[str, object] = {
    "work_id": _TAMPER_ID,
    "kind": "store-payload",
    "evidence_id": _EXACT_EVIDENCE_ID,
    "mutation_id": "(SELECT mutation_id FROM projection_outbox "
    "WHERE kind = 'fact-ingested')",
    "payload": f"x'{_EVIDENCE_PAYLOAD.hex()}'",
    "created_at": _LATER,
    "attempts": "0",
}

_TAMPER_PROJECTION_OUTBOX = (
    "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
    "created_at, attempts) VALUES ('{work_id}', '{kind}', '{fact_id}', "
    "{mutation_id}, '{created_at}', {attempts})"
)
_INTACT_PROJECTION_OUTBOX: dict[str, object] = {
    "work_id": _TAMPER_ID,
    "kind": "fact-ingested",
    "fact_id": _SOURCE_FACT_ID,
    "mutation_id": "(SELECT mutation_id FROM projection_outbox "
    "WHERE kind = 'fact-promoted')",
    "created_at": _LATER,
    "attempts": "0",
}

_TAMPER_INVALIDATION = (
    "INSERT INTO fact_invalidations (fact_id, invalidated_at, principal_id, "
    "superseded_by, reason) VALUES ('{fact_id}', '{invalidated_at}', "
    "'{principal_id}', {superseded_by}, {reason})"
)
_INTACT_INVALIDATION: dict[str, object] = {
    "fact_id": _PROMOTED_FACT_ID,
    "invalidated_at": _LATER,
    "principal_id": _CUSTODY_AGENT_ID,
    "superseded_by": "NULL",
    "reason": "'no longer believed'",
}

# A principal whose identity passes migration 0002's shape CHECK and still
# cannot be parsed — needed by any case pointing a custody column at it,
# since those columns carry a foreign key to a real principals row.
_MALFORMED_PRINCIPAL = (
    "INSERT INTO principals (principal_id, kind, label, created_at) VALUES "
    f"('{_MALFORMED_UUID}', 'workload', 'drifted', '{_LATER}')"
)


def _add_other_realm(config: CairnConfig) -> None:
    """A second realm holding one assertion and one fact, both internally
    consistent — the counterparty every cross-realm reference case needs.

    The ``audit_heads`` row is not optional scenery: ``_verify_heads`` requires
    one chain per realm, and without it that phase would fail first and hide
    whichever custody check the case is actually about.
    """
    _write(
        config,
        f"INSERT INTO realms (realm_id, created_at) VALUES ('other', '{_LATER}')",
        "INSERT INTO audit_heads "
        "(chain_kind, chain_identity, last_sequence, last_hash) "
        "VALUES ('realm', 'other', 0, zeroblob(32))",
        _row(
            _TAMPER_ASSERTION,
            _INTACT_ASSERTION,
            assertion_id=_OTHER_ASSERTION_ID,
            realm_id="other",
        ),
        _row(
            _TAMPER_FACT,
            _INTACT_FACT,
            fact_id=_OTHER_FACT_ID,
            realm_id="other",
            assertion_id=f"'{_OTHER_ASSERTION_ID}'",
        ),
    )


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        ("assertion", _row(_TAMPER_ASSERTION, _INTACT_ASSERTION)),
        ("fact", _row(_TAMPER_FACT, _INTACT_FACT)),
        ("promoted-fact", _row(_TAMPER_FACT, _INTACT_PROMOTED_FACT)),
        ("evidence", _row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE)),
        ("exact-evidence", _row(_TAMPER_EVIDENCE, _INTACT_EXACT_EVIDENCE)),
        ("evidence-outbox", _row(_TAMPER_EVIDENCE_OUTBOX, _INTACT_EVIDENCE_OUTBOX)),
        (
            "projection-outbox",
            _row(_TAMPER_PROJECTION_OUTBOX, _INTACT_PROJECTION_OUTBOX),
        ),
        ("invalidation", _row(_TAMPER_INVALIDATION, _INTACT_INVALIDATION)),
    ],
)
def test_custody_tamper_baselines_verify_cleanly(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
) -> None:
    """Each baseline row must pass verification untouched.

    Without this, a case built on a baseline that was already invalid for a
    second reason would keep passing after its check was deleted — which is
    exactly how three authority checks shipped unproven in Task 10.
    """
    config = copied_custody_catalogue(f"custody-baseline-{name}")
    _write(config, statement)

    verify_catalogue(config)


def test_other_realm_counterparty_verifies_cleanly(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    config = copied_custody_catalogue("custody-other-realm")
    _add_other_realm(config)

    assert verify_catalogue(config).fact_count == 3


@pytest.mark.parametrize(
    ("name", "statements", "ignore_checks"),
    [
        # Needs no bypass at all: the canonical-UUID CHECK is shape-only, so
        # SQLite stores this happily and UUID() raises on it.
        (
            "assertion-identity-unparseable",
            (_row(_TAMPER_ASSERTION, _INTACT_ASSERTION, assertion_id=_MALFORMED_UUID),),
            False,
        ),
        (
            "assertion-principal-unparseable",
            (
                _MALFORMED_PRINCIPAL,
                _row(
                    _TAMPER_ASSERTION, _INTACT_ASSERTION, principal_id=_MALFORMED_UUID
                ),
            ),
            False,
        ),
        (
            "fact-identity-unparseable",
            (_row(_TAMPER_FACT, _INTACT_FACT, fact_id=_MALFORMED_UUID),),
            False,
        ),
        (
            "fact-promoted-by-unparseable",
            (
                _MALFORMED_PRINCIPAL,
                _row(
                    _TAMPER_FACT,
                    _INTACT_PROMOTED_FACT,
                    promoted_by=f"'{_MALFORMED_UUID}'",
                ),
            ),
            False,
        ),
        (
            "evidence-identity-unparseable",
            (_row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, evidence_id=_MALFORMED_UUID),),
            False,
        ),
        (
            "evidence-outbox-work-id-unparseable",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    work_id=_MALFORMED_UUID,
                ),
            ),
            False,
        ),
        (
            "projection-outbox-work-id-unparseable",
            (
                _row(
                    _TAMPER_PROJECTION_OUTBOX,
                    _INTACT_PROJECTION_OUTBOX,
                    work_id=_MALFORMED_UUID,
                ),
            ),
            False,
        ),
        (
            "invalidation-principal-unparseable",
            (
                _MALFORMED_PRINCIPAL,
                _row(
                    _TAMPER_INVALIDATION,
                    _INTACT_INVALIDATION,
                    principal_id=_MALFORMED_UUID,
                ),
            ),
            False,
        ),
        # Timestamps: the CHECK pins 27 characters and a separator layout, but
        # its negative class admits a separator where a digit belongs, so a
        # value naming no real instant passes it.
        (
            "assertion-recorded-at-names-no-instant",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    recorded_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        (
            "assertion-observed-at-names-no-instant",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    observed_at="'----------T--:--:--.------Z'",
                ),
            ),
            False,
        ),
        (
            "fact-valid-from-names-no-instant",
            (
                _row(
                    _TAMPER_FACT,
                    _INTACT_FACT,
                    valid_from="'9999-99-99T99:99:99.999999Z'",
                ),
            ),
            False,
        ),
        (
            "fact-valid-to-names-no-instant",
            (
                _row(
                    _TAMPER_FACT,
                    _INTACT_FACT,
                    valid_to="'9999-99-99T99:99:99.999999Z'",
                ),
            ),
            False,
        ),
        (
            "fact-recorded-at-names-no-instant",
            (
                _row(
                    _TAMPER_FACT,
                    _INTACT_FACT,
                    recorded_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        (
            "evidence-recorded-at-names-no-instant",
            (
                _row(
                    _TAMPER_EVIDENCE,
                    _INTACT_EVIDENCE,
                    recorded_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        (
            "evidence-outbox-created-at-names-no-instant",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    created_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        (
            "projection-outbox-created-at-names-no-instant",
            (
                _row(
                    _TAMPER_PROJECTION_OUTBOX,
                    _INTACT_PROJECTION_OUTBOX,
                    created_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        (
            "evidence-outbox-last-attempt-at-names-no-instant",
            (
                "UPDATE evidence_outbox SET last_attempt_at = "
                "'9999-99-99T99:99:99.999999Z'",
            ),
            False,
        ),
        (
            "projection-outbox-last-attempt-at-names-no-instant",
            (
                "UPDATE projection_outbox SET last_attempt_at = "
                "'9999-99-99T99:99:99.999999Z'",
            ),
            False,
        ),
        (
            "invalidation-invalidated-at-names-no-instant",
            (
                _row(
                    _TAMPER_INVALIDATION,
                    _INTACT_INVALIDATION,
                    invalidated_at="9999-99-99T99:99:99.999999Z",
                ),
            ),
            False,
        ),
        # A canonical JSON array is not yet a scope path: these two are
        # perfectly canonical bytes carrying no segment at all, which the
        # reading guards in mutations.py and reconciliation.py already refuse
        # and name this phase as the authority on.
        (
            "scope-segments-not-segment-documents",
            (_row(_TAMPER_ASSERTION, _INTACT_ASSERTION, scope_segments="'[1,2,3]'"),),
            False,
        ),
        (
            "scope-segments-segment-fields-not-strings",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    scope_segments='\'[{"id":1,"kind":"job"}]\'',
                ),
            ),
            False,
        ),
        # The exact value reconciliation.py's _stored_segments names as the
        # row "the schema accepts" while deferring the authoritative verdict
        # to this phase. Canonical bytes, so only the element rule can catch
        # it.
        (
            "scope-segments-the-value-reconciliation-defers-on",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    scope_segments='\'[{"id":1,"kind":7}]\'',
                ),
            ),
            False,
        ),
        # The key-set clause needs cases of its own. Both of these carry
        # canonical bytes and the right element type, so neither the
        # canonicality check nor ScopeSegment construction would refuse them —
        # only the key set does. The second is also a robustness case: without
        # the clause, reading document["id"] would throw a bare KeyError out
        # of the verifier.
        (
            "scope-segments-segment-with-an-extra-key",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    scope_segments='\'[{"id":"job-1","kind":"job","note":"x"}]\'',
                ),
            ),
            False,
        ),
        (
            "scope-segments-segment-missing-its-id",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    scope_segments='\'[{"kind":"job"}]\'',
                ),
            ),
            False,
        ),
        # Canonical JSON that is not an array at all, and an array longer than
        # the 16-segment bound. Both CHECKs are total, so both need the
        # bypass — the same policy this file applies to enums and octet
        # bounds, now applied here too.
        # A scalar, deliberately, not an object. An object's keys iterate as
        # strings and are refused by the key-set clause instead, so an object
        # would leave the array check itself unpinned — the first sweep of
        # this round proved exactly that. A number is the value that only the
        # array check can refuse: without it, len() raises TypeError and the
        # verifier degrades to a generic failure.
        (
            "scope-segments-not-an-array",
            (_row(_TAMPER_ASSERTION, _INTACT_ASSERTION, scope_segments="'5'"),),
            True,
        ),
        (
            "scope-segments-above-the-sixteen-segment-bound",
            (
                _row(
                    _TAMPER_ASSERTION,
                    _INTACT_ASSERTION,
                    scope_segments="'"
                    + json.dumps(
                        [{"id": f"job-{index}", "kind": "job"} for index in range(17)],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "'",
                ),
            ),
            True,
        ),
        # Octet bounds, not character bounds. Each of these is within the
        # bound when counted in characters and over it when counted in UTF-8
        # octets, which is the only thing that distinguishes
        # length(CAST(col AS BLOB)) from length(col).
        (
            "fact-body-above-bound-in-octets-not-characters",
            (_row(_TAMPER_FACT, _INTACT_FACT, body="'" + "あ" * 30000 + "'"),),
            True,
        ),
        (
            "invalidation-reason-above-bound-in-octets-not-characters",
            (
                _row(
                    _TAMPER_INVALIDATION,
                    _INTACT_INVALIDATION,
                    reason="'" + "あ" * 2000 + "'",
                ),
            ),
            True,
        ),
        # Each custody table carrying a scope path needs its own case: the
        # phases call the same guard, but a deletion mutant kills one call
        # site at a time, and the first sweep of this kit proved the facts and
        # evidence call sites unpinned when only the assertions one was
        # covered.
        (
            "fact-scope-segments-not-segment-documents",
            (_row(_TAMPER_FACT, _INTACT_FACT, scope_segments="'[1,2,3]'"),),
            False,
        ),
        (
            "evidence-scope-segments-not-segment-documents",
            (_row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, scope_segments="'[1,2,3]'"),),
            False,
        ),
        # Enum columns. Each CHECK is a full IN-list and does constrain
        # meaning, so these are reachable only with checks lifted — a
        # catalogue restored from a pre-STRICT schema is the case they cover.
        (
            "assertion-classification-unknown",
            (_row(_TAMPER_ASSERTION, _INTACT_ASSERTION, classification="top-secret"),),
            True,
        ),
        (
            "assertion-source-type-unknown",
            (_row(_TAMPER_ASSERTION, _INTACT_ASSERTION, source_type="rumour"),),
            True,
        ),
        (
            "fact-trust-unknown",
            (_row(_TAMPER_FACT, _INTACT_FACT, trust="gospel"),),
            True,
        ),
        (
            "fact-classification-unknown",
            (_row(_TAMPER_FACT, _INTACT_FACT, classification="top-secret"),),
            True,
        ),
        (
            "evidence-classification-unknown",
            (_row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, classification="top-secret"),),
            True,
        ),
        (
            "evidence-outbox-kind-unknown",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    kind="delete-payload",
                ),
            ),
            True,
        ),
        (
            "projection-outbox-kind-unknown",
            (
                _row(
                    _TAMPER_PROJECTION_OUTBOX,
                    _INTACT_PROJECTION_OUTBOX,
                    kind="fact-forgotten",
                ),
            ),
            True,
        ),
        # Bounds. Every one is a real octet bound in the schema, so these too
        # need the checks lifted to reach the verifier.
        (
            "evidence-digest-thirty-one-octets",
            (_row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, payload_digest="zeroblob(31)"),),
            True,
        ),
        (
            "evidence-payload-length-below-bound",
            (_row(_TAMPER_EVIDENCE, _INTACT_EXACT_EVIDENCE, payload_length="0"),),
            True,
        ),
        (
            "evidence-payload-length-above-bound",
            (_row(_TAMPER_EVIDENCE, _INTACT_EXACT_EVIDENCE, payload_length="1048577"),),
            True,
        ),
        (
            "evidence-external-uri-below-bound",
            (_row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, external_uri="''"),),
            True,
        ),
        (
            "evidence-external-uri-above-bound",
            (
                _row(
                    _TAMPER_EVIDENCE,
                    _INTACT_EVIDENCE,
                    external_uri="'https://example.test/" + "a" * 2030 + "'",
                ),
            ),
            True,
        ),
        (
            "fact-body-below-bound",
            (_row(_TAMPER_FACT, _INTACT_FACT, body="''"),),
            True,
        ),
        (
            "invalidation-reason-below-bound",
            (_row(_TAMPER_INVALIDATION, _INTACT_INVALIDATION, reason="''"),),
            True,
        ),
    ],
)
def test_custody_value_tampering_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
    ignore_checks: bool,
) -> None:
    config = copied_custody_catalogue(f"custody-value-{name}")
    _write(config, *statements, ignore_checks=ignore_checks)

    _assert_failure(config, "custody_value_invalid")


@pytest.mark.parametrize(
    ("name", "statement", "ignore_checks"),
    [
        (
            "scope-segments-unsorted-keys",
            _row(
                _TAMPER_ASSERTION,
                _INTACT_ASSERTION,
                scope_segments='\'[{"kind":"job","id":"job-1"}]\'',
            ),
            False,
        ),
        (
            "scope-segments-spaced",
            _row(
                _TAMPER_ASSERTION,
                _INTACT_ASSERTION,
                scope_segments='\'[{"id": "job-1", "kind": "job"}]\'',
            ),
            True,
        ),
        # The strongest case in this kit, and the only one needing no bypass
        # and no malformed shape: json_valid() returns 1 for an invalid UTF-8
        # byte inside a JSON string literal, so the column holds octets the
        # driver refuses to decode. Selecting it as TEXT raises inside sqlite3
        # before any check could run; reading it as a BLOB keeps the verdict
        # here.
        (
            "scope-segments-undecodable-octets",
            _row(
                _TAMPER_ASSERTION,
                _INTACT_ASSERTION,
                scope_segments="'[{\"id\":\"' || CAST(x'ff' AS TEXT) "
                '|| \'","kind":"job"}]\'',
            ),
            False,
        ),
        (
            "metadata-unsorted-keys",
            _row(_TAMPER_ASSERTION, _INTACT_ASSERTION, metadata='\'{"b":1,"a":2}\''),
            False,
        ),
        (
            "metadata-spaced",
            _row(_TAMPER_ASSERTION, _INTACT_ASSERTION, metadata="'{\"a\": 1}'"),
            False,
        ),
        (
            "metadata-undecodable-octets",
            _row(
                _TAMPER_ASSERTION,
                _INTACT_ASSERTION,
                metadata="'{\"k\":\"' || CAST(x'ff' AS TEXT) || '\"}'",
            ),
            False,
        ),
    ],
)
def test_custody_non_canonical_json_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
    ignore_checks: bool,
) -> None:
    config = copied_custody_catalogue(f"custody-json-{name}")
    _write(config, statement, ignore_checks=ignore_checks)

    _assert_failure(config, "custody_json_not_canonical")


def test_undecodable_octets_do_not_mask_another_problem(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    """A row of undecodable octets must not stop the verifier finding the rest.

    Reading these columns as TEXT would raise inside the sqlite3 driver at the
    fetch — before a single row is examined — so one such row would collapse
    the whole run into the generic ``catalogue_invalid`` and hide every other
    problem in the catalogue behind it. A tool whose job is finding corruption
    must not be stoppable by the corruption it is looking for.

    Here the undecodable row sorts *after* an unrelated non-canonical
    ``metadata`` value. The verifier must report the metadata.

    The earlier fault is deliberately one whose code differs from the code a
    failed read produces. Reading the column as TEXT raises ``sqlite3.Error``
    at the fetch, which this phase's own guard converts to
    ``custody_value_invalid`` — so an earlier fault carrying *that* code would
    let the test pass whether the masking was fixed or not.
    """
    config = copied_custody_catalogue("custody-undecodable-does-not-mask")
    _write(
        config,
        _row(
            _TAMPER_ASSERTION,
            _INTACT_ASSERTION,
            assertion_id=_SECOND_TAMPER_ID,
            metadata='\'{"b":1,"a":2}\'',
        ),
        _row(
            _TAMPER_ASSERTION,
            _INTACT_ASSERTION,
            assertion_id=_TAMPER_ID,
            scope_segments="'[{\"id\":\"' || CAST(x'ff' AS TEXT) "
            '|| \'","kind":"job"}]\'',
        ),
    )

    # The premise, pinned rather than asserted: the driver really does refuse
    # this column as TEXT, and really does hand over the raw octets as a BLOB.
    # If a future change reads these columns as TEXT again, this fails here
    # rather than silently degrading detection to catalogue_invalid.
    connection = sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "SELECT scope_segments FROM assertions ORDER BY assertion_id"
            ).fetchall()
        assert (
            b"\xff"
            in connection.execute(
                "SELECT CAST(scope_segments AS BLOB) FROM assertions "
                "WHERE assertion_id = ?",
                (str(_TAMPER_ID),),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    _assert_failure(config, "custody_json_not_canonical")


def test_an_unreadable_body_does_not_stop_a_later_phase(
    copied_custody_catalogue: Callable[[str], CairnConfig],
) -> None:
    """A fact body of undecodable octets is not an error, and must not stop
    the verifier reaching a fault two phases later.

    This is the property ruling 3 exists for, and it is stronger than
    asserting the right code for a bad row. ``facts.body`` is the right column
    to prove it on: an undecodable octet there is not itself corruption — the
    body is measured, never read — so the run must simply carry on. One octet
    satisfies ``ck_facts_body``, so no bypass is needed.

    Select ``body`` instead of measuring it and the fetch raises inside the
    driver, the phase's own guard turns that into ``custody_value_invalid``
    for the whole table, and the cross-realm reference below is never reached.
    """
    config = copied_custody_catalogue("custody-unreadable-body")
    _add_other_realm(config)
    _write(
        config,
        _row(_TAMPER_FACT, _INTACT_FACT, body="CAST(x'ff' AS TEXT)"),
        _row(
            _TAMPER_INVALIDATION,
            _INTACT_INVALIDATION,
            superseded_by=f"'{_OTHER_FACT_ID}'",
        ),
    )

    _assert_failure(config, "custody_reference_invalid")


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        (
            "both-forms",
            {
                "derived_from": f"'{_SOURCE_FACT_ID}'",
                "promoted_by": f"'{_CUSTODY_AGENT_ID}'",
                "evidence_id": f"'{_EXACT_EVIDENCE_ID}'",
            },
        ),
        ("neither-form", {"assertion_id": "NULL"}),
        ("promoted-without-evidence", {**_INTACT_PROMOTED_FACT, "evidence_id": "NULL"}),
    ],
)
def test_custody_fact_provenance_form_tampering_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    changes: dict[str, object],
) -> None:
    config = copied_custody_catalogue(f"custody-provenance-{name}")
    _write(config, _row(_TAMPER_FACT, _INTACT_FACT, **changes), ignore_checks=True)

    _assert_failure(config, "custody_provenance_invalid")


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        (
            "both-forms",
            {
                "assertion_id": f"'{_ASSERTION_ID}'",
                "payload_length": str(len(_EVIDENCE_PAYLOAD)),
            },
        ),
        ("neither-form", {"external_uri": "NULL"}),
        (
            "exact-without-payload-length",
            {"assertion_id": f"'{_ASSERTION_ID}'", "external_uri": "NULL"},
        ),
    ],
)
def test_custody_evidence_custody_form_tampering_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    changes: dict[str, object],
) -> None:
    config = copied_custody_catalogue(f"custody-form-{name}")
    _write(
        config, _row(_TAMPER_EVIDENCE, _INTACT_EVIDENCE, **changes), ignore_checks=True
    )

    _assert_failure(config, "custody_custody_form_invalid")


@pytest.mark.parametrize(
    ("name", "statement", "ignore_checks"),
    [
        (
            "validity-window-inverted",
            _row(
                _TAMPER_FACT,
                _INTACT_FACT,
                valid_from="'2026-08-05T13:00:00.000000Z'",
                valid_to="'2026-08-05T12:00:00.000000Z'",
            ),
            True,
        ),
        (
            "validity-window-empty",
            _row(
                _TAMPER_FACT,
                _INTACT_FACT,
                valid_from="'2026-08-05T12:00:00.000000Z'",
                valid_to="'2026-08-05T12:00:00.000000Z'",
            ),
            True,
        ),
        # No CHECK can compare two tables, so this one needs no bypass.
        (
            "invalidated-before-the-fact-was-recorded",
            _row(
                _TAMPER_INVALIDATION,
                _INTACT_INVALIDATION,
                invalidated_at="2026-08-05T12:00:00.000000Z",
            ),
            False,
        ),
    ],
)
def test_custody_temporal_tampering_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
    ignore_checks: bool,
) -> None:
    config = copied_custody_catalogue(f"custody-temporal-{name}")
    _write(config, statement, ignore_checks=ignore_checks)

    _assert_failure(config, "custody_temporal_invalid")


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "invalidation-superseded-by-another-realm",
            _row(
                _TAMPER_INVALIDATION,
                _INTACT_INVALIDATION,
                superseded_by=f"'{_OTHER_FACT_ID}'",
            ),
        ),
        (
            "fact-asserted-in-another-realm",
            _row(_TAMPER_FACT, _INTACT_FACT, assertion_id=f"'{_OTHER_ASSERTION_ID}'"),
        ),
        (
            "fact-derived-from-another-realm",
            _row(
                _TAMPER_FACT,
                _INTACT_PROMOTED_FACT,
                derived_from=f"'{_OTHER_FACT_ID}'",
            ),
        ),
        (
            "fact-evidenced-from-another-realm",
            _row(
                _TAMPER_FACT,
                _INTACT_PROMOTED_FACT,
                realm_id="other",
                scope_segments=_SEGMENTS,
                derived_from=f"'{_OTHER_FACT_ID}'",
            ),
        ),
        (
            "evidence-asserted-in-another-realm",
            _row(
                _TAMPER_EVIDENCE,
                _INTACT_EXACT_EVIDENCE,
                assertion_id=f"'{_OTHER_ASSERTION_ID}'",
            ),
        ),
    ],
)
def test_custody_cross_realm_references_are_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
) -> None:
    """Every one of these is foreign-key clean: the row it names exists, and
    only its realm is wrong. No CHECK and no foreign key can express that,
    and scope isolation is the one invariant that cannot afford the gap."""
    config = copied_custody_catalogue(f"custody-reference-{name}")
    _add_other_realm(config)
    _write(config, statement)

    _assert_failure(config, "custody_reference_invalid")


@pytest.mark.parametrize(
    ("name", "statements", "ignore_checks"),
    [
        (
            "payload-disagreeing-with-its-record",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    payload="x'00'",
                ),
            ),
            False,
        ),
        # A same-length substitution, which is the whole point of holding a
        # digest: the length clause cannot tell these bytes from the real
        # ones, so only the digest comparison fails this case.
        (
            "payload-of-the-right-length-but-the-wrong-bytes",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    payload=f"x'{b'THE BUILD LOG'.hex()}'",
                ),
            ),
            False,
        ),
        # Digest agrees — the fixture attests the same payload for both
        # records — so the external record's absent payload_length is the sole
        # reason this fails. The queue exists only to carry exact bytes.
        (
            "payload-queued-against-an-external-custody-record",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    evidence_id=_EXTERNAL_EVIDENCE_ID,
                ),
            ),
            False,
        ),
        (
            "evidence-mutation-absent-from-the-audit-chain",
            (
                _row(
                    _TAMPER_EVIDENCE_OUTBOX,
                    _INTACT_EVIDENCE_OUTBOX,
                    mutation_id=f"'{_NONEXISTENT_ID}'",
                ),
            ),
            False,
        ),
        (
            "projection-mutation-absent-from-the-audit-chain",
            (
                _row(
                    _TAMPER_PROJECTION_OUTBOX,
                    _INTACT_PROJECTION_OUTBOX,
                    mutation_id=f"'{_NONEXISTENT_ID}'",
                ),
            ),
            False,
        ),
        (
            "evidence-attempts-negative",
            ("UPDATE evidence_outbox SET attempts = -1",),
            True,
        ),
        (
            "projection-attempts-negative",
            ("UPDATE projection_outbox SET attempts = -1",),
            True,
        ),
    ],
)
def test_custody_outbox_tampering_is_detected(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statements: tuple[str, ...],
    ignore_checks: bool,
) -> None:
    config = copied_custody_catalogue(f"custody-outbox-{name}")
    _write(config, *statements, ignore_checks=ignore_checks)

    _assert_failure(config, "custody_outbox_invalid")


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "invalidation-for-a-missing-fact",
            _row(_TAMPER_INVALIDATION, _INTACT_INVALIDATION, fact_id=_NONEXISTENT_ID),
        ),
        (
            "invalidation-superseded-by-a-missing-fact",
            _row(
                _TAMPER_INVALIDATION,
                _INTACT_INVALIDATION,
                superseded_by=f"'{_NONEXISTENT_ID}'",
            ),
        ),
        (
            "orphan-evidence-outbox-row",
            _row(
                _TAMPER_EVIDENCE_OUTBOX,
                _INTACT_EVIDENCE_OUTBOX,
                evidence_id=_NONEXISTENT_ID,
            ),
        ),
        (
            "orphan-projection-outbox-row",
            _row(
                _TAMPER_PROJECTION_OUTBOX,
                _INTACT_PROJECTION_OUTBOX,
                fact_id=_NONEXISTENT_ID,
            ),
        ),
    ],
)
def test_custody_orphan_rows_are_caught_by_the_foreign_key_phase(
    copied_custody_catalogue: Callable[[str], CairnConfig],
    name: str,
    statement: str,
) -> None:
    """A dangling custody reference is the foreign-key phase's to report, not
    the custody phase's.

    ``_verify_custody`` deliberately holds no duplicate of these: a second
    check of the same thing could not be proven by deleting it, because
    ``PRAGMA foreign_key_check`` would fail the catalogue either way. Pinning
    the codes here is what makes that division a decision rather than an
    omission.
    """
    config = copied_custody_catalogue(f"custody-orphan-{name}")
    _write_with_foreign_keys_off(config, statement)

    _assert_failure(config, "foreign_key_failed")
