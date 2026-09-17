import hashlib
import itertools
import json
import sqlite3
import threading
from _thread import LockType
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from cairn.authority import mutations
from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.grants import is_scope_prefix
from cairn.authority.mutations import (
    AssertionIngested,
    CairnAuthority,
    ExternalEvidenceReference,
    FactsInvalidated,
    FactsPromoted,
    IngestAssertion,
    InvalidateFacts,
    PromoteFacts,
    _segment_documents,
)
from cairn.catalogue.audit import (
    AuditDraft,
    ChainKind,
    Classification,
    Scope,
    ScopeSegment,
    TrustClass,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    read_connection,
)
from cairn.catalogue.transactions import (
    CatalogueTransactionError,
    CatalogueTransactions,
    CommitAmbiguity,
    Committed,
    FailureCode,
    FailureDetail,
    MutationOutcome,
    MutationReceipt,
    Rejected,
    Replayed,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.screening import POLICY_VERSION, SecretFinding, SecretScreen

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_EXPIRED_TS = "2026-08-01T00:00:00.000000Z"
# Workload grants must carry an expiry (schema trigger), and an agent is a
# workload; every live fixture grant therefore expires well after _NOW.
_FUTURE_TS = "2027-01-01T00:00:00.000000Z"
_REALM = "acme"

_JOB = ScopeSegment(kind="job", identifier="job-1")
_SIBLING_JOB = ScopeSegment(kind="job", identifier="job-2")
_SCOPE = Scope(_REALM, (_JOB,))

_AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_AGENT_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_OUTSIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OUTSIDER_CREDENTIAL_ID = UUID("bbbbbbbb-cccc-4bbb-8bbb-bbbbbbbbbbbb")
_INGEST_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_PROMOTE_GRANT_ID = UUID("eeeeeeee-2222-4eee-8eee-eeeeeeeeeeee")
_REPLACEMENT_GRANT_ID = UUID("eeeeeeee-3333-4eee-8eee-eeeeeeeeeeee")
_HOSTILE_GRANT_ID = UUID("eeeeeeee-6666-4eee-8eee-eeeeeeeeeeee")

_IDEMPOTENCY_KEY = UUID("77777777-7777-4777-8777-777777777777")
_OTHER_KEY = UUID("77777777-8888-4777-8777-777777777777")
_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")

# Every table a custody mutation can write. Named once so an atomicity check
# cannot quietly omit one, and so Tasks 8 and 9 inherit the full list.
_CUSTODY_TABLES = (
    "assertions",
    "facts",
    "fact_invalidations",
    "evidence_records",
    "evidence_outbox",
    "projection_outbox",
)

_ALL_CLASSIFICATIONS = frozenset(
    {Classification.PUBLIC, Classification.INTERNAL, Classification.RESTRICTED}
)


# --- harness -----------------------------------------------------------------


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


def _uuid_descending(start: int) -> Callable[[], UUID]:
    """Hands out identities in *decreasing* string order, so a receipt that
    merely preserved assignment order would fail the sorted-by-str check."""
    counter = itertools.count(start, -1)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def _transactions(
    data_path: Path,
    *,
    now: datetime = _NOW,
) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: now,
        uuid_factory=_uuid_seq(0x10000000),
    )


def _authority(
    data_path: Path,
    *,
    now: datetime = _NOW,
    uuid_factory: Callable[[], UUID] | None = None,
    exact_evidence_enabled: bool = True,
    transactions: CatalogueTransactions | None = None,
    screen: SecretScreen | None = None,
) -> CairnAuthority:
    return CairnAuthority(
        data_path,
        transactions or _transactions(data_path, now=now),
        clock=lambda: now,
        uuid_factory=uuid_factory or _uuid_seq(0x20000000),
        exact_evidence_enabled=exact_evidence_enabled,
        screen=screen or SecretScreen(),
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
    principal_id: UUID | str,
    *,
    kind: PrincipalKind = PrincipalKind.WORKLOAD,
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
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(credential_id), str(principal_id), bytes(32), _TS),
        )
        connection.commit()


def _json_column(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _insert_grant(
    data_path: Path,
    *,
    grant_id: UUID,
    principal_id: UUID = _AGENT_ID,
    realm_id: str = _REALM,
    segments: tuple[ScopeSegment, ...] = (_JOB,),
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.INGEST}),
    read_clearance: Classification = Classification.RESTRICTED,
    write_classifications: frozenset[Classification] = _ALL_CLASSIFICATIONS,
    expires_at: str | None = _FUTURE_TS,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(grant_id),
                str(principal_id),
                realm_id,
                _json_column([{"id": s.identifier, "kind": s.kind} for s in segments]),
                _json_column(sorted(op.value for op in operations)),
                read_clearance.value,
                _json_column(sorted(c.value for c in write_classifications)),
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
            "(grant_id, revoked_at, revoked_by, reason_code) VALUES (?, ?, NULL, ?)",
            (str(grant_id), _TS, "superseded"),
        )
        connection.commit()


def _seed_ingester(
    data_path: Path,
    *,
    segments: tuple[ScopeSegment, ...] = (_JOB,),
    write_classifications: frozenset[Classification] = _ALL_CLASSIFICATIONS,
    expires_at: str = _FUTURE_TS,
) -> None:
    """A workload principal holding one live ``ingest`` grant."""
    _insert_principal(data_path, _AGENT_ID, label="agent")
    _insert_credential(data_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_grant(
        data_path,
        grant_id=_INGEST_GRANT_ID,
        segments=segments,
        write_classifications=write_classifications,
        expires_at=expires_at,
    )


def _seed_promote_grant(
    data_path: Path,
    *,
    segments: tuple[ScopeSegment, ...] = (_JOB,),
) -> None:
    _insert_grant(
        data_path,
        grant_id=_PROMOTE_GRANT_ID,
        segments=segments,
        operations=frozenset({GrantOperation.PROMOTE}),
    )


def _seed_outsider(data_path: Path) -> None:
    _insert_principal(data_path, _OUTSIDER_ID, label="outsider")
    _insert_credential(data_path, _OUTSIDER_CREDENTIAL_ID, _OUTSIDER_ID)


def _agent_actor() -> Actor:
    return Actor(principal_id=_AGENT_ID, credential_id=_AGENT_CREDENTIAL_ID)


def _outsider_actor() -> Actor:
    return Actor(principal_id=_OUTSIDER_ID, credential_id=_OUTSIDER_CREDENTIAL_ID)


def _connect(data_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(data_path / CATALOGUE_FILENAME)


def _rows(data_path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = _connect(data_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _audit_rows(data_path: Path) -> list[tuple[object, ...]]:
    return _rows(
        data_path,
        "SELECT chain_kind, action_kind, action_code, outcome, reason_code "
        "FROM audit_events ORDER BY chain_kind, sequence",
    )


def _command(
    *,
    scope: Scope = _SCOPE,
    classification: Classification = Classification.INTERNAL,
    source_type: SourceType = SourceType.AGENT_CLAIM,
    facts: tuple[FactDraft, ...] | None = None,
    requested_trust: TrustClass = TrustClass.CANDIDATE,
    observed_at: datetime | None = None,
    metadata: str | None = None,
    evidence_payload: bytes | None = None,
) -> IngestAssertion:
    return IngestAssertion(
        scope=scope,
        classification=classification,
        source_type=source_type,
        facts=facts if facts is not None else (_draft("the build is green"),),
        requested_trust=requested_trust,
        observed_at=observed_at,
        metadata=metadata,
        evidence_payload=evidence_payload,
    )


def _draft(body: str) -> FactDraft:
    return FactDraft(body=body, valid_from=None, valid_to=None)


def _ingest(
    authority: CairnAuthority,
    command: IngestAssertion,
    *,
    actor: Actor | None = None,
    idempotency_key: UUID = _IDEMPOTENCY_KEY,
) -> object:
    return authority.ingest(
        actor or _agent_actor(),
        command,
        idempotency_key=idempotency_key,
        correlation_id=_CORRELATION_ID,
    )


# === Step 1: authorisation ===================================================


def test_ingest_denied_without_any_ingest_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(), actor=_outsider_actor())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "ingest_grant_not_held")
    ]


def test_an_unauthorised_ingest_receipt_discloses_no_realm_chain_state(
    tmp_path: Path,
) -> None:
    """The reason the outer-gate denial is on the instance chain.

    ``Rejected`` hands the caller its ``audit_receipt``, and that receipt
    carries ``chain_identity`` and ``sequence``. On the realm chain those are
    the realm's name and the exact number of events recorded in it — read by
    an actor holding no grant there at all. Appending would also advance that
    sequence, letting such an actor perturb a chain it cannot read, and
    repeated denials would meter the realm's genuine write traffic.
    """
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    # A real event on the realm chain first, so its sequence is past zero and
    # the assertion below distinguishes "instance chain" from "realm chain
    # that happens to be empty".
    _seed_ingester(tmp_path)
    assert isinstance(_ingest(_authority(tmp_path), _command()), Committed)

    outcome = _ingest(_authority(tmp_path), _command(), actor=_outsider_actor())

    assert isinstance(outcome, Rejected)
    assert outcome.audit_receipt.chain_kind is ChainKind.INSTANCE
    assert outcome.audit_receipt.chain_identity != _REALM
    assert outcome.audit_receipt.chain_identity == str(_INSTANCE_ID)


def test_an_unauthorised_ingest_refusal_carries_a_realm_fingerprint(
    tmp_path: Path,
) -> None:
    """Correlatable without disclosure, as for promotion: realm auditors never
    see this event, so the fingerprint is what ties a reported correlation id
    back to the request. It is the realm and nothing else — the same value
    ingest's other two instance-chain refusals use, so a malformed scope, an
    unknown realm and an unauthorised request in one realm all correlate
    together and none of them publishes the scope path."""
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(), actor=_outsider_actor())

    assert isinstance(outcome, Rejected)
    assert (
        _only_event(tmp_path)["safe_request_fingerprint"]
        == hashlib.sha256(_REALM.encode()).hexdigest()
    )


def test_ingest_denied_when_the_ingest_grant_has_expired(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path, expires_at=_EXPIRED_TS)

    outcome = _ingest(_authority(tmp_path), _command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "ingest_grant_not_held")
    ]


def test_ingest_denied_when_the_ingest_grant_is_revoked(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _revoke_grant_row(tmp_path, _INGEST_GRANT_ID)

    outcome = _ingest(_authority(tmp_path), _command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "ingest_grant_not_held")
    ]


def test_ingest_denied_when_the_grant_scope_does_not_cover_the_request(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path, segments=(_SIBLING_JOB,))

    outcome = _ingest(_authority(tmp_path), _command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "ingest_grant_not_held")
    ]


def test_ingest_denied_for_a_classification_outside_the_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(
        tmp_path,
        write_classifications=frozenset(
            {Classification.PUBLIC, Classification.INTERNAL}
        ),
    )

    outcome = _ingest(
        _authority(tmp_path), _command(classification=Classification.RESTRICTED)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "classification_not_writable")
    ]


def test_validated_trust_denied_without_a_promote_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(requested_trust=TrustClass.VALIDATED, evidence_payload=b"proof"),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "validated_requires_promote")
    ]


def test_validated_trust_denied_when_the_promote_grant_is_at_a_sibling_scope(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _seed_promote_grant(tmp_path, segments=(_SIBLING_JOB,))

    outcome = _ingest(
        _authority(tmp_path),
        _command(requested_trust=TrustClass.VALIDATED, evidence_payload=b"proof"),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "validated_requires_promote")
    ]


def test_validated_trust_without_a_payload_is_an_invalid_request(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _seed_promote_grant(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(requested_trust=TrustClass.VALIDATED)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "validated_requires_payload")
    ]


def test_failed_approach_trust_needs_only_the_ingest_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(requested_trust=TrustClass.FAILED_APPROACH)
    )

    assert isinstance(outcome, Committed)
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "allow", "assertion_ingested")
    ]


def test_unknown_realm_is_not_found_on_the_instance_chain(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(scope=Scope("ghost", (_JOB,))))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "realm_not_found")
    ]


def test_replay_after_the_authorising_grant_is_revoked_is_freshly_denied(
    tmp_path: Path,
) -> None:
    """I-44: a replay must be authorised afresh, never served from the
    idempotency record after the grant behind it has gone."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)
    command = _command()

    first = _ingest(authority, command)
    assert isinstance(first, Committed)

    _revoke_grant_row(tmp_path, _INGEST_GRANT_ID)
    second = _ingest(authority, command)

    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.AUTHORISATION_DENIED
    # _audit_rows orders by (chain_kind, sequence); the fresh denial goes on
    # the instance chain, and "instance" sorts before "realm", so the denial
    # leads despite being the later event.
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "ingest_grant_not_held"),
        ("realm", "data", "ingest", "allow", "assertion_ingested"),
    ]


def test_denials_write_no_custody_rows(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)

    _ingest(_authority(tmp_path), _command(), actor=_outsider_actor())

    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_records") == [(0,)]


# === Step 2: durable custody =================================================

_PAYLOAD = b"cairn-evidence-payload-marker: build log tail"
_BODY = "distinctive-fact-body-marker: the build is green"
_OTHER_BODY = "distinctive-fact-body-marker: the tests are green"
_DEEP_SCOPE = Scope(_REALM, (_JOB, ScopeSegment(kind="step", identifier="step-3")))


def _restart_query(data_path: Path, sql: str) -> list[tuple[object, ...]]:
    """Reads through a brand-new ``CatalogueTransactions`` over the same
    directory — a genuine reopen of the catalogue file, not a second read on
    the connection that wrote it (I-25)."""
    transactions = CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=_uuid_seq(0x30000000),
    )
    return list(transactions.execute_compound(lambda t: t.query(sql)))


def _allow_event(data_path: Path) -> dict[str, object]:
    rows = _rows(
        data_path,
        "SELECT canonical_event FROM audit_events WHERE outcome = 'allow' "
        "ORDER BY sequence",
    )
    assert len(rows) == 1
    return cast(dict[str, object], json.loads(cast(bytes, rows[0][0])))


def _payload_ingest() -> IngestAssertion:
    return _command(
        scope=_DEEP_SCOPE,
        facts=(_draft(_BODY), _draft(_OTHER_BODY)),
        evidence_payload=_PAYLOAD,
    )


def test_payload_bearing_ingest_writes_the_assertion_row(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert _rows(
        tmp_path,
        "SELECT assertion_id, realm_id, classification, source_type, "
        "principal_id, observed_at, metadata, recorded_at FROM assertions",
    ) == [
        (
            str(outcome.value.assertion_id),
            _REALM,
            "internal",
            "agent-claim",
            str(_AGENT_ID),
            None,
            None,
            _TS,
        )
    ]


def test_payload_bearing_ingest_writes_one_fact_row_per_draft(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assertion_id = str(outcome.value.assertion_id)
    assert _rows(
        tmp_path,
        "SELECT body, trust, classification, realm_id, assertion_id, "
        "derived_from, promoted_by, evidence_id, valid_from, valid_to, "
        "recorded_at FROM facts ORDER BY body",
    ) == [
        (
            _BODY,
            "candidate",
            "internal",
            _REALM,
            assertion_id,
            None,
            None,
            None,
            None,
            None,
            _TS,
        ),
        (
            _OTHER_BODY,
            "candidate",
            "internal",
            _REALM,
            assertion_id,
            None,
            None,
            None,
            None,
            None,
            _TS,
        ),
    ]


def test_ingest_writes_scope_segments_in_canonical_sorted_key_form(
    tmp_path: Path,
) -> None:
    """Migration 0003's ``= json(...)`` CHECK pins minification only, never
    key order, so nothing below this layer can catch a scope path stored with
    ``kind`` before ``id``. Every custody table that carries a scope path is
    pinned here against the exact bytes ``json.dumps(..., sort_keys=True,
    separators=(",", ":"), ensure_ascii=False)`` produces for the audit
    scope-document segment shape."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    canonical = json.dumps(
        [{"id": "job-1", "kind": "job"}, {"id": "step-3", "kind": "step"}],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert canonical == '[{"id":"job-1","kind":"job"},{"id":"step-3","kind":"step"}]'
    # The sort must be doing work, not agreeing by accident. _segment_documents
    # is written kind-first on purpose so the canonical form depends on
    # sorting; without this, reordering it back to alphabetical would disarm
    # the assertion above silently, and a tidy or a merge could do exactly
    # that.
    assert (
        json.dumps(_segment_documents(_DEEP_SCOPE.segments), separators=(",", ":"))
        != canonical
    )
    stored = (
        _rows(tmp_path, "SELECT scope_segments FROM assertions")
        + _rows(tmp_path, "SELECT scope_segments FROM facts")
        + _rows(tmp_path, "SELECT scope_segments FROM evidence_records")
    )
    assert len(stored) == 4
    assert {row[0] for row in stored} == {canonical}


def test_payload_bearing_ingest_writes_the_exact_custody_evidence_record(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id is not None
    assert _rows(
        tmp_path,
        "SELECT evidence_id, realm_id, classification, payload_digest, "
        "assertion_id, payload_length, external_uri, recorded_at "
        "FROM evidence_records",
    ) == [
        (
            str(outcome.value.evidence_id),
            _REALM,
            "internal",
            hashlib.sha256(_PAYLOAD).digest(),
            str(outcome.value.assertion_id),
            len(_PAYLOAD),
            None,
            _TS,
        )
    ]


def test_payload_bearing_ingest_writes_the_store_payload_outbox_row(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert _rows(
        tmp_path,
        "SELECT kind, evidence_id, mutation_id, payload, created_at, attempts, "
        "last_attempt_at, last_failure_code FROM evidence_outbox",
    ) == [
        (
            "store-payload",
            str(outcome.value.evidence_id),
            str(outcome.mutation_receipt.mutation_id),
            _PAYLOAD,
            _TS,
            0,
            None,
            None,
        )
    ]


def test_ingest_writes_one_fact_ingested_projection_row_per_fact(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    mutation_id = str(outcome.mutation_receipt.mutation_id)
    assert _rows(
        tmp_path,
        "SELECT kind, fact_id, mutation_id, created_at, attempts "
        "FROM projection_outbox ORDER BY fact_id",
    ) == [
        ("fact-ingested", str(fact_id), mutation_id, _TS, 0)
        for fact_id in outcome.value.fact_ids
    ]


def test_allow_audit_event_names_every_identity_it_touched(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id is not None
    event = _allow_event(tmp_path)
    assert event["affected_assertion_ids"] == [str(outcome.value.assertion_id)]
    assert event["affected_fact_ids"] == [
        str(fact_id) for fact_id in outcome.value.fact_ids
    ]
    assert event["affected_fact_ids"] == sorted(
        cast(list[str], event["affected_fact_ids"])
    )
    assert event["affected_evidence_ids"] == [str(outcome.value.evidence_id)]
    assert event["evidence_reference"] == str(outcome.value.evidence_id)
    assert event["evidence_digest"] == hashlib.sha256(_PAYLOAD).hexdigest()
    assert event["requested_scope"] == {
        "realm": _REALM,
        "segments": [
            {"id": "job-1", "kind": "job"},
            {"id": "step-3", "kind": "step"},
        ],
    }


def test_custody_survives_a_restart(tmp_path: Path) -> None:
    """I-25: every row written by the commit is visible to a fresh
    ``CatalogueTransactions`` opening the same directory again."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id is not None
    assert _restart_query(tmp_path, "SELECT assertion_id FROM assertions") == [
        (str(outcome.value.assertion_id),)
    ]
    assert _restart_query(tmp_path, "SELECT fact_id FROM facts ORDER BY fact_id") == [
        (str(fact_id),) for fact_id in outcome.value.fact_ids
    ]
    assert _restart_query(tmp_path, "SELECT evidence_id FROM evidence_records") == [
        (str(outcome.value.evidence_id),)
    ]
    assert _restart_query(tmp_path, "SELECT payload FROM evidence_outbox") == [
        (_PAYLOAD,)
    ]
    assert _restart_query(tmp_path, "SELECT count(*) FROM projection_outbox") == [(2,)]
    assert _restart_query(
        tmp_path, "SELECT count(*) FROM audit_events WHERE outcome = 'allow'"
    ) == [(1,)]


def test_payload_free_ingest_creates_no_evidence_record_or_outbox_row(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(facts=(_draft(_BODY),)))

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id is None
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_records") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_outbox") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(1,)]
    event = _allow_event(tmp_path)
    assert event["affected_evidence_ids"] == []
    assert event["evidence_reference"] is None
    assert event["evidence_digest"] is None


def test_payload_bearing_ingest_is_refused_when_exact_evidence_is_disabled(
    tmp_path: Path,
) -> None:
    """I-69/I-16: a disabled Attic is absence, so there is nowhere for the
    payload to go — refuse before custody rather than write a half-assertion."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path, exact_evidence_enabled=False), _payload_ingest()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "evidence_disabled")
    ]
    for table in _CUSTODY_TABLES:
        assert _rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_payload_free_ingest_is_unaffected_when_exact_evidence_is_disabled(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path, exact_evidence_enabled=False),
        _command(facts=(_draft(_BODY),)),
    )

    assert isinstance(outcome, Committed)
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]


# === Step 3: atomicity, validation and idempotency ===========================


class _GrantRaceTransactions(CatalogueTransactions):
    """Changes the actor's grants in the window between the outer gate and the
    mutation transaction — what a concurrent writer would do, and the only
    thing the in-transaction re-evaluation exists to catch (P-07).

    With ``replacement`` set, a second live ingest grant covering the same
    scope is inserted before the first is revoked, so the re-evaluation finds
    a *different* authorising grant rather than none — the branch that must
    still fail closed, because the audit draft already names the grant the
    outer gate found.

    ``interfere`` replaces that built-in grant edit with an arbitrary one, so
    the same window can be used for any concurrent write — promotion races its
    own grants and races a concurrent invalidation of a source fact.
    """

    def __init__(
        self,
        data_path: Path,
        *,
        writer_gate: LockType,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        replacement: UUID | None = None,
        interfere: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__(
            data_path,
            writer_gate=writer_gate,
            clock=clock,
            uuid_factory=uuid_factory,
        )
        self._replacement = replacement
        self._interfere = interfere

    def mutate_idempotent[T](
        self,
        draft: AuditDraft,
        *,
        principal_id: UUID,
        operation: str,
        idempotency_key: UUID,
        command_digest: bytes,
        result_schema: str,
        mutation: Callable[[_MutationTransaction], T],
        encode: Callable[[T, MutationReceipt], bytes],
        decode: Callable[[bytes], tuple[T, MutationReceipt]],
        restate_identities: Callable[[AuditDraft, T], AuditDraft] | None = None,
        reauthorise: Callable[[_GuardedTransaction], None] | None = None,
    ) -> MutationOutcome[T]:
        if self._interfere is not None:
            self._interfere(self._data_path)
        else:
            if self._replacement is not None:
                _insert_grant(self._data_path, grant_id=self._replacement)
            _revoke_grant_row(self._data_path, _INGEST_GRANT_ID)
        return super().mutate_idempotent(
            draft,
            principal_id=principal_id,
            operation=operation,
            idempotency_key=idempotency_key,
            command_digest=command_digest,
            result_schema=result_schema,
            mutation=mutation,
            encode=encode,
            decode=decode,
            restate_identities=restate_identities,
            reauthorise=reauthorise,
        )


def _unvalidated_draft(body: str) -> FactDraft:
    """A draft carrying a body the value layer would have refused, built by
    bypassing ``__post_init__``. It is the only way to reach the command
    layer's own re-validation, which is what stands between a hostile value
    and a raw ``sqlite3.IntegrityError`` surfacing to a caller."""
    draft = FactDraft(body="placeholder", valid_from=None, valid_to=None)
    object.__setattr__(draft, "body", body)
    return draft


def _unvalidated_scope(segments: tuple[object, ...]) -> Scope:
    """A scope whose segments bypassed ``Scope.__post_init__``. Such a scope
    cannot be allowed to reach an audit draft: the denial event recording the
    refusal would itself fail against the audit scope index, so the shape has
    to be caught before any draft exists."""
    scope = Scope(_REALM, (_JOB,))
    object.__setattr__(scope, "segments", segments)
    return scope


def _over_long_segments() -> tuple[ScopeSegment, ...]:
    return (_JOB,) + tuple(
        ScopeSegment(kind="step", identifier=f"step-{n}") for n in range(16)
    )


def _catalogue_bytes(data_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(data_path.iterdir())
        if path.is_file() and path.name.startswith(CATALOGUE_FILENAME)
    )


def _catalogue_tables(connection: sqlite3.Connection) -> list[str]:
    """Every application table, read from ``sqlite_master`` rather than
    listed by hand, so a table a later task adds is scanned automatically
    instead of being silently skipped."""
    return [
        cast(str, row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _column_occurrences(data_path: Path, needle: bytes) -> set[tuple[str, str]]:
    """Every ``(table, column)`` in the whole catalogue holding a value that
    contains ``needle`` — an exhaustive scan of the schema, not of the
    columns the test happens to know about."""
    connection = _connect(data_path)
    try:
        found: set[tuple[str, str]] = set()
        for table in _catalogue_tables(connection):
            columns = [
                cast(str, row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for column in columns:
                for (value,) in connection.execute(f'SELECT "{column}" FROM "{table}"'):
                    raw = value.encode() if isinstance(value, str) else value
                    if isinstance(raw, bytes) and needle in raw:
                        found.add((table, column))
                        break
        return found
    finally:
        connection.close()


def test_a_hundred_fact_batch_is_accepted(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=tuple(_draft(f"fact {n}") for n in range(100))),
    )

    assert isinstance(outcome, Committed)
    assert len(outcome.value.fact_ids) == 100
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(100,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(100,)]


def test_a_hundred_and_one_fact_batch_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=tuple(_draft(f"fact {n}") for n in range(101))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "batch_too_large")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(0,)]


def test_an_empty_batch_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(facts=()))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [("realm", "data", "ingest", "deny", "empty_batch")]


def test_one_oversize_body_rejects_the_whole_batch_with_no_partial_custody(
    tmp_path: Path,
) -> None:
    """I-66: a batch is one atomic custody decision. The valid drafts around
    the bad one must leave nothing behind."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        # Payload-bearing, so the evidence_outbox check below is a real
        # assertion rather than a vacuous one on a command that would never
        # have written an outbox row.
        _command(
            facts=(
                _draft(_BODY),
                _unvalidated_draft("x" * 65537),
                _draft(_OTHER_BODY),
            ),
            evidence_payload=_PAYLOAD,
        ),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "invalid_body")
    ]
    for table in _CUSTODY_TABLES:
        assert _rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_a_custody_value_violation_reuses_its_typed_code_as_the_audit_reason(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(metadata="not json at all"))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "invalid_metadata")
    ]


def test_an_over_long_scope_is_a_typed_denial_not_an_escaping_error(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(scope=_unvalidated_scope(_over_long_segments()))
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope")
    ]


def test_a_scope_shape_refusal_carries_the_realm_fingerprint(tmp_path: Path) -> None:
    """Ingest's scope-shape refusal was the only instance-chain data event in
    the slice carrying no fingerprint. Promotion and invalidation fingerprint
    their batches at every call site, and the unknown-realm refusal on this
    very command already fingerprints the realm — so without this one, a
    realm's auditors are told nothing at all about a request refused before
    the realm was read.

    The realm and not the scope: the segments are precisely what was refused
    as malformed, and hashing them would make the fingerprint a function of
    the value the event must not publish."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(scope=_unvalidated_scope(_over_long_segments()))
    )

    assert isinstance(outcome, Rejected)
    assert (
        _only_event(tmp_path)["safe_request_fingerprint"]
        == hashlib.sha256(_REALM.encode()).hexdigest()
    )


def test_an_unknown_realm_refusal_carries_the_same_fingerprint(
    tmp_path: Path,
) -> None:
    """Both of ingest's instance-chain refusals name the same realm, so they
    correlate — which is the whole point of fingerprinting either."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(scope=Scope("nonexistent", (_JOB,)))
    )

    assert isinstance(outcome, Rejected)
    assert (
        _only_event(tmp_path)["safe_request_fingerprint"]
        == hashlib.sha256(b"nonexistent").hexdigest()
    )


def test_a_non_string_realm_refusal_carries_no_fingerprint(tmp_path: Path) -> None:
    """There is no realm to fingerprint when the realm is not a string, and
    the event says so rather than inventing one. Hashing it unconditionally
    would raise ``AttributeError`` while recording the very denial that
    exists to contain the malformed value."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    scope = Scope(_REALM, (_JOB,))
    object.__setattr__(scope, "realm", 7)

    outcome = _ingest(_authority(tmp_path), _command(scope=scope))

    assert isinstance(outcome, Rejected)
    assert _only_event(tmp_path)["safe_request_fingerprint"] is None


def _tampered_segment(
    *, kind: object = "job", identifier: object = "job-1"
) -> ScopeSegment:
    """A segment whose ``kind``/``identifier`` bypassed
    ``ScopeSegment.__post_init__``. Reconstructing the enclosing ``Scope``
    does not re-check either field — ``Scope`` validates only the realm, the
    tuple type, the length bound and that each member *is* a
    ``ScopeSegment`` — so segment content needs its own guard."""
    segment = ScopeSegment(kind="job", identifier="job-1")
    object.__setattr__(segment, "kind", kind)
    object.__setattr__(segment, "identifier", identifier)
    return segment


def test_a_segment_identifier_with_hostile_content_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(identifier="bad id!"),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_id")
    ]


def test_an_over_long_segment_identifier_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(identifier="j" * 300),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_id")
    ]


def test_a_segment_kind_with_hostile_content_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(kind="BAD"),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_kind")
    ]


def test_a_segment_holding_a_non_string_kind_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """A non-``str`` field would raise ``TypeError`` from inside ``re`` — not
    an ``AuditValueError``, so it carries no code, maps to no audit reason and
    escapes with no durable event at all. Same failure mode as hostile
    content, different exception class."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(kind=7),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_kind")
    ]


def test_a_segment_holding_a_non_string_identifier_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(identifier=1),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_id")
    ]


def test_a_segment_holding_a_none_identifier_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_tampered_segment(identifier=None),))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope_segment_id")
    ]


def test_a_non_string_realm_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    scope = Scope(_REALM, (_JOB,))
    object.__setattr__(scope, "realm", 7)

    outcome = _ingest(_authority(tmp_path), _command(scope=scope))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_realm")
    ]


def test_a_scope_holding_a_non_segment_is_a_typed_denial(tmp_path: Path) -> None:
    """The guard covers segment *content*, not merely how many there are: a
    non-``ScopeSegment`` in the tuple would otherwise reach the audit scope
    index and raise while recording the denial."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(scope=_unvalidated_scope((_JOB, "not-a-segment"))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "ingest", "deny", "invalid_scope")
    ]


def test_the_in_transaction_evaluation_catches_a_grant_revoked_mid_request(
    tmp_path: Path,
) -> None:
    """The outer gate passed; only the authoritative in-transaction
    re-evaluation can see the revocation, and its denial must be filed as a
    data event, not an administration one."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=_uuid_seq(0x10000000),
    )

    outcome = _ingest(
        _authority(tmp_path, transactions=transactions), _payload_ingest()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "ingest_grant_not_held")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(0,)]


def test_the_in_transaction_evaluation_fails_closed_on_a_swapped_grant(
    tmp_path: Path,
) -> None:
    """A concurrent writer swaps the authorising grant for a different one
    that would also authorise. The request must still be refused: the audit
    draft already names the grant the outer gate found, and committing under
    a grant that no longer authorises would make that a false record. The
    closed ingest vocabulary reports this as ``ingest_grant_not_held``, so the
    swap is deliberately indistinguishable from an outright revocation even in
    the audit reason."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=_uuid_seq(0x10000000),
        replacement=_REPLACEMENT_GRANT_ID,
    )

    outcome = _ingest(
        _authority(tmp_path, transactions=transactions), _payload_ingest()
    )

    # The replacement really does authorise on its own — without this the test
    # would pass for the wrong reason, having exercised the no-grant branch.
    assert _rows(
        tmp_path,
        "SELECT count(*) FROM grants g WHERE NOT EXISTS "
        "(SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.grant_id)",
    ) == [(1,)]
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "ingest_grant_not_held")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(0,)]


def test_ingests_in_transaction_evaluation_rolls_back_on_a_hostile_grant_row(
    tmp_path: Path,
) -> None:
    """A concurrent writer plants a second, hostile grant row for the same
    principal and realm — the same shape-without-meaning defect
    gate._row_to_grant's typed readers close for the outer gate (§5 of the
    Task 9 report), reachable here in ingest's own in-transaction
    re-derivation. The refusal must roll back and produce a durable denial
    through the normal MutationRejection route, not merely convert one
    exception type into another that still escapes."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=_uuid_seq(0x10000000),
        interfere=lambda path: _insert_grant_raw(
            path, grant_id=_HOSTILE_GRANT_ID, operations='["not-a-real-op"]'
        ),
    )

    outcome = _ingest(
        _authority(tmp_path, transactions=transactions), _payload_ingest()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "grant_enum_malformed")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_records") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_outbox") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]


def test_both_authorisation_evaluations_use_one_captured_instant(
    tmp_path: Path,
) -> None:
    """The clock advances an hour on every call after the first. A grant
    expiring half an hour from now stays live for the whole request only if
    ``effective_at`` was captured exactly once; a second ``clock()`` for the
    in-transaction evaluation would see it expired and deny."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path, expires_at="2026-08-05T10:41:12.123456Z")
    instants = iter([_NOW] + [_NOW + timedelta(hours=1)] * 32)
    authority = CairnAuthority(
        tmp_path,
        _transactions(tmp_path),
        clock=lambda: next(instants),
        uuid_factory=_uuid_seq(0x20000000),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )

    outcome = _ingest(authority, _payload_ingest())

    assert isinstance(outcome, Committed)
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "allow", "assertion_ingested")
    ]
    # A stray second clock() reaching recorded_at would store _NOW + 1h here
    # rather than the captured instant.
    assert _rows(tmp_path, "SELECT recorded_at FROM assertions") == [(_TS,)]
    assert _rows(tmp_path, "SELECT DISTINCT recorded_at FROM facts") == [(_TS,)]
    assert _rows(tmp_path, "SELECT recorded_at FROM evidence_records") == [(_TS,)]


def test_the_request_reads_its_clock_exactly_once(tmp_path: Path) -> None:
    """The rule itself, not one of its consequences: every temporal semantic
    of the mutation comes from a single capture, so the authority's clock is
    read once per request and never again."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    reads = 0

    def counting_clock() -> datetime:
        nonlocal reads
        reads += 1
        return _NOW

    authority = CairnAuthority(
        tmp_path,
        _transactions(tmp_path),
        clock=counting_clock,
        uuid_factory=_uuid_seq(0x20000000),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )

    outcome = _ingest(authority, _payload_ingest())

    assert isinstance(outcome, Committed)
    assert reads == 1


def test_identity_assignment_follows_assertion_then_drafts_then_evidence(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert outcome.value.assertion_id == UUID("20000000-0000-4000-8000-000000000000")
    assert _rows(tmp_path, "SELECT fact_id FROM facts ORDER BY rowid") == [
        ("20000001-0000-4000-8000-000000000000",),
        ("20000002-0000-4000-8000-000000000000",),
    ]
    assert _rows(tmp_path, "SELECT body FROM facts ORDER BY rowid") == [
        (_BODY,),
        (_OTHER_BODY,),
    ]
    assert outcome.value.evidence_id == UUID("20000003-0000-4000-8000-000000000000")


def test_receipt_fact_ids_are_sorted_by_string_not_by_assignment_order(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    outcome = _ingest(
        _authority(tmp_path, uuid_factory=_uuid_descending(0x2000000F)),
        _command(facts=(_draft("a"), _draft("b"), _draft("c"))),
    )

    assert isinstance(outcome, Committed)
    assigned = (
        UUID("2000000e-0000-4000-8000-000000000000"),
        UUID("2000000d-0000-4000-8000-000000000000"),
        UUID("2000000c-0000-4000-8000-000000000000"),
    )
    assert outcome.value.fact_ids != assigned
    assert outcome.value.fact_ids == tuple(sorted(assigned, key=str))


def test_an_identical_replay_returns_the_original_receipt(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)
    command = _payload_ingest()

    first = _ingest(authority, command)
    second = _ingest(authority, command)

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    # Reconstructed by the decode path from stored bytes, not handed back from
    # memory: the type and every identity must survive the round trip.
    assert isinstance(second.value, AssertionIngested)
    assert second.value == first.value
    assert second.mutation_receipt == first.mutation_receipt
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "allow", "assertion_ingested"),
        ("realm", "data", "ingest", "allow", "idempotent_replay"),
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(1,)]


def test_the_same_key_with_a_different_command_is_an_idempotency_conflict(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)

    first = _ingest(authority, _command(facts=(_draft(_BODY),)))
    second = _ingest(authority, _command(facts=(_draft(_OTHER_BODY),)))

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    assert _audit_rows(tmp_path)[1] == (
        "realm",
        "data",
        "ingest",
        "deny",
        "idempotency_conflict",
    )


def test_a_conflicting_key_on_a_payload_bearing_ingest_is_a_clean_conflict(
    tmp_path: Path,
) -> None:
    """The payload-free case above cannot reach this. An allow draft that
    creates evidence carries both P-16 fields, and ``mutate_idempotent`` turns
    that same draft into the conflict denial — so unless it clears them,
    ``AuditDraft`` refuses to be built and a stable conflict escapes as a raw
    ``AuditValueError`` instead. Promotion made this unavoidable, because its
    allow event must always carry both."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)

    first = _ingest(authority, _payload_ingest())
    second = _ingest(
        authority, _command(facts=(_draft(_OTHER_BODY),), evidence_payload=_PAYLOAD)
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    conflict = json.loads(
        cast(
            bytes,
            _rows(
                tmp_path,
                "SELECT canonical_event FROM audit_events WHERE outcome = 'deny'",
            )[0][0],
        )
    )
    assert conflict["evidence_reference"] is None
    assert conflict["evidence_digest"] is None


def test_a_different_key_with_the_same_command_ingests_again(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)

    first = _ingest(authority, _command(facts=(_draft(_BODY),)))
    second = _ingest(
        authority, _command(facts=(_draft(_BODY),)), idempotency_key=_OTHER_KEY
    )

    assert isinstance(first, Committed)
    assert isinstance(second, Committed)
    assert first.value.assertion_id != second.value.assertion_id


def test_the_column_scan_covers_every_custody_table(tmp_path: Path) -> None:
    """Guards the two scans below from passing vacuously: a scan that
    enumerated nothing would satisfy any "appears nowhere else" assertion."""
    _seed_catalogue(tmp_path)
    connection = _connect(tmp_path)
    try:
        tables = set(_catalogue_tables(connection))
    finally:
        connection.close()

    assert {
        "assertions",
        "facts",
        "fact_invalidations",
        "evidence_records",
        "evidence_outbox",
        "projection_outbox",
        "audit_events",
        "audit_scope_index",
        "idempotency_records",
    } <= tables


def test_a_fact_body_is_stored_in_no_column_but_facts_body(tmp_path: Path) -> None:
    """The slice 3 plaintext-custody analogue. Literal absence cannot be
    asserted — ``facts.body`` is exactly where a body is supposed to live —
    so the binding assertion is exhaustive by column: every table, every
    column, and the body is found in one place only."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert _column_occurrences(tmp_path, _BODY.encode()) == {("facts", "body")}
    assert _column_occurrences(tmp_path, _OTHER_BODY.encode()) == {("facts", "body")}


def test_an_evidence_payload_is_stored_in_no_column_but_the_evidence_outbox(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert _column_occurrences(tmp_path, _PAYLOAD) == {("evidence_outbox", "payload")}


def test_neither_a_body_nor_a_payload_reaches_a_receipt_or_an_audit_event(
    tmp_path: Path,
) -> None:
    """The two surfaces the constraint cares about most, asserted directly and
    over every row rather than a chosen one."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    result_bytes = cast(
        bytes, _rows(tmp_path, "SELECT result_bytes FROM idempotency_records")[0][0]
    )
    surfaces = [result_bytes] + [
        cast(bytes, event)
        for (event,) in _rows(tmp_path, "SELECT canonical_event FROM audit_events")
    ]
    assert len(surfaces) > 1
    for surface in surfaces:
        assert _BODY.encode() not in surface
        assert _OTHER_BODY.encode() not in surface
        assert _PAYLOAD not in surface


def test_the_catalogue_file_holds_one_copy_of_each_body_and_payload(
    tmp_path: Path,
) -> None:
    """Coarse secondary check on the raw file bytes, below the schema the
    column scan reads through. The count is exactly one only because every
    connection here is context-managed: SQLite checkpoints and removes the
    ``-wal`` sidecar when the last one closes cleanly, so no committed page
    survives in two files. It is a corroborating check, not the binding
    oracle — the column scan above is."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _payload_ingest())

    assert isinstance(outcome, Committed)
    assert sorted(
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith(CATALOGUE_FILENAME)
    ) == [CATALOGUE_FILENAME]
    catalogue = _catalogue_bytes(tmp_path)
    assert catalogue.count(_BODY.encode()) == 1
    assert catalogue.count(_OTHER_BODY.encode()) == 1
    assert catalogue.count(_PAYLOAD) == 1


def test_the_command_digest_is_sha256_of_the_p20_document(tmp_path: Path) -> None:
    """The digest document is pinned as a literal here, not recomputed from
    the code under test: a change to its field set, ordering or value forms
    must break this test rather than silently redefine command identity."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    command = _command(
        scope=_DEEP_SCOPE,
        classification=Classification.RESTRICTED,
        source_type=SourceType.VERIFIED_CHECK,
        facts=(
            FactDraft(
                body=_BODY,
                valid_from=datetime(2026, 8, 1, tzinfo=UTC),
                valid_to=datetime(2026, 9, 1, tzinfo=UTC),
            ),
        ),
        observed_at=datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        metadata='{"run":"r-7"}',
        evidence_payload=_PAYLOAD,
    )

    outcome = _ingest(_authority(tmp_path), command)

    expected_document = (
        b'{"classification":"restricted","command":"ingest",'
        b'"facts":[{"body":"' + _BODY.encode() + b'",'
        b'"valid_from":"2026-08-01T00:00:00.000000Z",'
        b'"valid_to":"2026-09-01T00:00:00.000000Z"}],'
        b'"metadata":{"run":"r-7"},'
        b'"observed_at":"2026-08-05T09:00:00.000000Z",'
        b'"payload_sha256":"' + hashlib.sha256(_PAYLOAD).hexdigest().encode() + b'",'
        b'"requested_trust":"candidate",'
        b'"schema":"cairn.authority/v1",'
        b'"scope":{"realm":"acme","segments":['
        b'{"id":"job-1","kind":"job"},{"id":"step-3","kind":"step"}]},'
        b'"source_type":"verified-check"}'
    )
    assert isinstance(outcome, Committed)
    assert (
        outcome.mutation_receipt.command_digest
        == hashlib.sha256(expected_document).digest()
    )
    assert (
        _allow_event(tmp_path)["command_digest"]
        == hashlib.sha256(expected_document).hexdigest()
    )


def test_an_empty_payload_is_refused_as_an_invalid_payload(tmp_path: Path) -> None:
    """``ExactEvidence`` is what bounds the payload, so its typed code is the
    audit reason — the storage layer's BLOB length CHECK never has to fire."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(evidence_payload=b""))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "invalid_payload")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_outbox") == [(0,)]


def test_an_oversize_payload_is_refused_as_an_invalid_payload(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(evidence_payload=b"x" * 1048577))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "invalid_payload")
    ]


# === Task 8, Step 1: promotion authorisation =================================
#
# Where a promote denial is filed is load-bearing, not incidental. A
# PromoteFacts names its sources by identity alone and carries no realm, so
# until every source is proven visible there is no realm the event could
# honestly name — and naming one would disclose the realm of a fact the actor
# may not be allowed to see. I-67 forces the point: an unknown source identity
# and a source outside the actor's authority must be publicly identical, and
# Rejected carries its audit_receipt, chain and all. Since the unknown case has
# no realm by construction, the instance chain is the only chain the pair can
# share. Everything decided before sources are proven visible and homogeneous
# therefore lands on the instance chain; everything after lands on the source
# realm chain, where the realm discloses nothing the actor could not already
# learn.

_RETRIEVE_GRANT_ID = UUID("eeeeeeee-4444-4eee-8eee-eeeeeeeeeeee")
_SOURCE_ASSERTION_ID = UUID("cccccccc-0000-4ccc-8ccc-cccccccccccc")
_SOURCE_FACT_ID = UUID("cccccccc-1111-4ccc-8ccc-cccccccccccc")
_SECOND_FACT_ID = UUID("cccccccc-2222-4ccc-8ccc-cccccccccccc")
_UNKNOWN_FACT_ID = UUID("cccccccc-9999-4ccc-8ccc-cccccccccccc")
_NAMED_EVIDENCE_ID = UUID("dddddddd-1111-4ddd-8ddd-dddddddddddd")
_UNKNOWN_EVIDENCE_ID = UUID("dddddddd-9999-4ddd-8ddd-dddddddddddd")

_STEP = ScopeSegment(kind="step", identifier="step-3")
_SOURCE_SCOPE = _DEEP_SCOPE  # acme / job-1 / step-3
_PARENT_SCOPE = _SCOPE  # acme / job-1
_ROOT_SCOPE = Scope(_REALM, ())
_CHILD_SCOPE = Scope(_REALM, (_JOB, _STEP, ScopeSegment(kind="task", identifier="t-1")))
_SIBLING_SCOPE = Scope(_REALM, (_SIBLING_JOB,))
_OTHER_REALM = "beta"
_CROSS_REALM_SCOPE = Scope(_OTHER_REALM, (_JOB,))

_EXTERNAL_URI = "https://attic.example/evidence/7"
_EXTERNAL_DIGEST = hashlib.sha256(b"caller-attested external evidence").digest()
_EVIDENCE_DIGEST = hashlib.sha256(_PAYLOAD).digest()
_REASON = "promotion-reason-marker: verified by an independent check"


def _insert_assertion_row(
    data_path: Path,
    *,
    assertion_id: UUID = _SOURCE_ASSERTION_ID,
    scope: Scope = _SOURCE_SCOPE,
    classification: Classification = Classification.INTERNAL,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO assertions (assertion_id, realm_id, scope_segments, "
            "classification, source_type, principal_id, observed_at, metadata, "
            "recorded_at) VALUES (?, ?, ?, ?, 'agent-claim', ?, NULL, NULL, ?)",
            (
                str(assertion_id),
                scope.realm,
                _json_column(
                    [{"id": s.identifier, "kind": s.kind} for s in scope.segments]
                ),
                classification.value,
                str(_AGENT_ID),
                _TS,
            ),
        )
        connection.commit()


def _insert_fact_row(
    data_path: Path,
    fact_id: UUID = _SOURCE_FACT_ID,
    *,
    scope: Scope = _SOURCE_SCOPE,
    body: str = _BODY,
    trust: TrustClass = TrustClass.CANDIDATE,
    classification: Classification = Classification.INTERNAL,
    valid_from: str | None = None,
    valid_to: str | None = None,
    assertion_id: UUID = _SOURCE_ASSERTION_ID,
    scope_segments: str | None = None,
) -> None:
    """A stored source fact, written directly rather than ingested.

    ``scope_segments`` overrides the canonical column so a row the schema
    accepts but the value layer must refuse can be planted deliberately.
    """
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, derived_from, promoted_by, evidence_id, "
            "valid_from, valid_to, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
            (
                str(fact_id),
                scope.realm,
                scope_segments
                if scope_segments is not None
                else _json_column(
                    [{"id": s.identifier, "kind": s.kind} for s in scope.segments]
                ),
                body,
                trust.value,
                classification.value,
                str(assertion_id),
                valid_from,
                valid_to,
                _TS,
            ),
        )
        connection.commit()


def _insert_evidence_row(
    data_path: Path,
    evidence_id: UUID = _NAMED_EVIDENCE_ID,
    *,
    scope: Scope = _SOURCE_SCOPE,
    classification: Classification = Classification.INTERNAL,
    assertion_id: UUID = _SOURCE_ASSERTION_ID,
    scope_segments: str | None = None,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
            "classification, payload_digest, assertion_id, payload_length, "
            "external_uri, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                str(evidence_id),
                scope.realm,
                scope_segments
                if scope_segments is not None
                else _json_column(
                    [{"id": s.identifier, "kind": s.kind} for s in scope.segments]
                ),
                classification.value,
                _EVIDENCE_DIGEST,
                str(assertion_id),
                len(_PAYLOAD),
                _TS,
            ),
        )
        connection.commit()


def _invalidate_fact_row(data_path: Path, fact_id: UUID = _SOURCE_FACT_ID) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO fact_invalidations "
            "(fact_id, invalidated_at, principal_id, superseded_by, reason) "
            "VALUES (?, ?, ?, NULL, 'superseded')",
            (str(fact_id), _TS, str(_AGENT_ID)),
        )
        connection.commit()


def _seed_promoter(
    data_path: Path,
    *,
    retrieve_segments: tuple[ScopeSegment, ...] = (_JOB,),
    promote_segments: tuple[ScopeSegment, ...] = (_JOB,),
    read_clearance: Classification = Classification.RESTRICTED,
    write_classifications: frozenset[Classification] = _ALL_CLASSIFICATIONS,
) -> None:
    """A workload principal holding one live ``retrieve`` grant and one live
    ``promote`` grant. They are separate rows so a test can weaken exactly one
    of the two authorisations promotion needs."""
    _insert_principal(data_path, _AGENT_ID, label="agent")
    _insert_credential(data_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_grant(
        data_path,
        grant_id=_RETRIEVE_GRANT_ID,
        segments=retrieve_segments,
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=read_clearance,
    )
    _insert_grant(
        data_path,
        grant_id=_PROMOTE_GRANT_ID,
        segments=promote_segments,
        operations=frozenset({GrantOperation.PROMOTE}),
        read_clearance=read_clearance,
        write_classifications=write_classifications,
    )


def _seed_promotable(
    data_path: Path,
    *,
    realms: tuple[str, ...] = (_REALM,),
    trust: TrustClass = TrustClass.CANDIDATE,
    classification: Classification = Classification.INTERNAL,
    evidence_classification: Classification | None = None,
    retrieve_segments: tuple[ScopeSegment, ...] = (_JOB,),
    promote_segments: tuple[ScopeSegment, ...] = (_JOB,),
    read_clearance: Classification = Classification.RESTRICTED,
    write_classifications: frozenset[Classification] = _ALL_CLASSIFICATIONS,
) -> None:
    """The whole happy-path fixture: catalogue, promoter, one source fact and
    one named evidence record, all at ``_SOURCE_SCOPE``."""
    _seed_catalogue(data_path, realms=realms)
    _seed_promoter(
        data_path,
        retrieve_segments=retrieve_segments,
        promote_segments=promote_segments,
        read_clearance=read_clearance,
        write_classifications=write_classifications,
    )
    _insert_assertion_row(data_path, classification=classification)
    _insert_fact_row(data_path, trust=trust, classification=classification)
    _insert_evidence_row(
        data_path,
        classification=classification
        if evidence_classification is None
        else evidence_classification,
    )


def _promote_command(
    *,
    fact_ids: tuple[UUID, ...] = (_SOURCE_FACT_ID,),
    evidence: UUID | ExternalEvidenceReference = _NAMED_EVIDENCE_ID,
    target_scope: Scope | None = None,
    target_classification: Classification | None = None,
    reason: str = _REASON,
) -> PromoteFacts:
    return PromoteFacts(
        fact_ids=fact_ids,
        evidence=evidence,
        target_scope=target_scope,
        target_classification=target_classification,
        reason=reason,
    )


def _promote(
    authority: CairnAuthority,
    command: PromoteFacts,
    *,
    actor: Actor | None = None,
    idempotency_key: UUID = _IDEMPOTENCY_KEY,
) -> object:
    return authority.promote(
        actor or _agent_actor(),
        command,
        idempotency_key=idempotency_key,
        correlation_id=_CORRELATION_ID,
    )


def _full_fact_rows(data_path: Path) -> list[tuple[object, ...]]:
    """Every column of every fact row, in insertion order — the only honest
    way to assert a source row is untouched. A status column would not be
    evidence: promotion is supposed to leave no mark on its source at all."""
    return _rows(data_path, "SELECT * FROM facts ORDER BY rowid")


def _derived_row(data_path: Path, derived_id: UUID) -> tuple[object, ...]:
    return _rows(
        data_path,
        "SELECT realm_id, scope_segments, body, trust, classification, "
        "assertion_id, derived_from, promoted_by, evidence_id, valid_from, "
        f"valid_to, recorded_at FROM facts WHERE fact_id = '{derived_id}'",
    )[0]


# --- MUT-03: same-scope promotion --------------------------------------------


def test_same_scope_promotion_derives_a_validated_fact_with_promoted_provenance(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, FactsPromoted)
    (source_id, derived_id) = outcome.value.promotions[0]
    assert source_id == _SOURCE_FACT_ID
    assert _derived_row(tmp_path, derived_id) == (
        _REALM,
        _json_column(
            [{"id": "job-1", "kind": "job"}, {"id": "step-3", "kind": "step"}]
        ),
        _BODY,
        "validated",
        "internal",
        None,
        str(_SOURCE_FACT_ID),
        str(_AGENT_ID),
        str(_NAMED_EVIDENCE_ID),
        None,
        None,
        _TS,
    )
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "allow", "facts_promoted")
    ]


def test_same_scope_promotion_leaves_the_source_row_byte_identical(
    tmp_path: Path,
) -> None:
    """Rows are immutable and promotion derives rather than edits. Compared
    over the full row before and after, not over a status column."""
    _seed_promotable(tmp_path)
    before = _full_fact_rows(tmp_path)
    assert len(before) == 1

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    after = _full_fact_rows(tmp_path)
    assert len(after) == 2
    assert after[0] == before[0]


# --- MUT-04: ancestor-scope promotion ----------------------------------------


def test_ancestor_scope_promotion_writes_the_derived_fact_at_the_target_scope(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(target_scope=_PARENT_SCOPE)
    )

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[:5] == (
        _REALM,
        _json_column([{"id": "job-1", "kind": "job"}]),
        _BODY,
        "validated",
        "internal",
    )


def test_ancestor_scope_promotion_leaves_the_source_row_byte_identical(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)
    before = _full_fact_rows(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(target_scope=_PARENT_SCOPE)
    )

    assert isinstance(outcome, Committed)
    assert _full_fact_rows(tmp_path)[0] == before[0]


# --- MUT-05: evidence identity -----------------------------------------------


def test_an_unknown_evidence_identity_is_a_coarse_authorisation_denial(
    tmp_path: Path,
) -> None:
    """A structurally absent evidence field is impossible by type, so the
    slice 4 form of MUT-05 is an evidence identity that names nothing."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(evidence=_UNKNOWN_EVIDENCE_ID)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "evidence_unknown")
    ]


# --- MUT-06: source visibility -----------------------------------------------


def test_a_source_without_a_covering_retrieve_grant_is_denied(tmp_path: Path) -> None:
    _seed_promotable(tmp_path, retrieve_segments=(_SIBLING_JOB,))

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "source_retrieve_denied")
    ]


def test_a_source_above_the_actors_read_clearance_is_denied(tmp_path: Path) -> None:
    _seed_promotable(
        tmp_path,
        classification=Classification.RESTRICTED,
        read_clearance=Classification.INTERNAL,
    )

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "source_clearance_exceeded")
    ]


# --- MUT-07: target authorisation --------------------------------------------


def test_a_target_without_a_covering_promote_grant_is_denied(tmp_path: Path) -> None:
    _seed_promotable(tmp_path, promote_segments=(_SIBLING_JOB,))

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_promote_denied")
    ]


def test_a_realm_root_target_requires_a_root_prefix_promote_grant(
    tmp_path: Path,
) -> None:
    """The SCOPE-06 analogue: a grant at job-1 does not reach the realm root,
    because the grant's own segments must prefix the target's."""
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command(target_scope=_ROOT_SCOPE))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_promote_denied")
    ]


def test_a_realm_root_target_succeeds_with_a_root_prefix_promote_grant(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path, promote_segments=())

    outcome = _promote(_authority(tmp_path), _promote_command(target_scope=_ROOT_SCOPE))

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[1] == "[]"


# --- MUT-08: ancestor-only widening ------------------------------------------


def test_a_descendant_target_is_an_invalid_request_though_promote_is_held_there(
    tmp_path: Path,
) -> None:
    """A promote grant covering the source necessarily covers everything below
    it, so the actor genuinely *could* write at the descendant — a premise
    asserted here rather than claimed. Narrowing is refused anyway, because
    ancestor-only widening is structural.

    Note what removing the ancestor check would do in this fixture: not turn
    this into a denial, but let the promotion **succeed**. That is a different
    failure from the one the sibling and cross-realm cases catch, where no
    promote grant covers the target and dropping the check yields
    ``target_promote_denied`` instead. Both are needed to pin the ordering.
    """
    _seed_promotable(tmp_path)
    assert is_scope_prefix((_JOB,), _CHILD_SCOPE.segments)

    outcome = _promote(
        _authority(tmp_path), _promote_command(target_scope=_CHILD_SCOPE)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_not_ancestor")
    ]


def test_a_sibling_target_is_an_invalid_request(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(target_scope=_SIBLING_SCOPE)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_not_ancestor")
    ]


def test_a_cross_realm_target_is_an_invalid_request(tmp_path: Path) -> None:
    """Same segments, different realm. Refused structurally, which is why
    promotion never needs the slice 3 ``realm_not_found`` path: the target
    realm is never looked up at all."""
    _seed_promotable(tmp_path, realms=(_REALM, _OTHER_REALM))

    outcome = _promote(
        _authority(tmp_path), _promote_command(target_scope=_CROSS_REALM_SCOPE)
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_not_ancestor")
    ]


# --- MUT-09 / MUT-10: classification transitions -----------------------------


def test_a_classification_raise_outside_write_classifications_is_denied(
    tmp_path: Path,
) -> None:
    _seed_promotable(
        tmp_path,
        write_classifications=frozenset(
            {Classification.PUBLIC, Classification.INTERNAL}
        ),
    )

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(target_classification=Classification.RESTRICTED),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "classification_not_writable")
    ]


def test_a_classification_lowering_promotion_is_an_invalid_request(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(target_classification=Classification.PUBLIC),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "classification_lowered")
    ]


def test_a_classification_lowering_is_refused_before_the_writability_check(
    tmp_path: Path,
) -> None:
    """The same field, two different public outcomes. The actor cannot write
    ``public`` either, so only the ordering decides which of the two this is:
    lowering is structural and comes first, so it must be ``invalid_request``
    and never ``classification_not_writable``."""
    _seed_promotable(
        tmp_path, write_classifications=frozenset({Classification.INTERNAL})
    )

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(target_classification=Classification.PUBLIC),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "classification_lowered")
    ]


def test_an_unchanged_classification_still_requires_write_classifications(
    tmp_path: Path,
) -> None:
    """A promotion writes a new fact, so ``write_classifications`` gates it
    even when the classification is inherited unchanged. Enforcing this only
    on a raise would let a promoter holding ``{public}`` write a ``restricted``
    fact purely because it was not a raise."""
    _seed_promotable(
        tmp_path,
        classification=Classification.RESTRICTED,
        write_classifications=frozenset({Classification.PUBLIC}),
    )

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "classification_not_writable")
    ]


def test_a_classification_raise_inside_write_classifications_succeeds(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(target_classification=Classification.RESTRICTED),
    )

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[4] == "restricted"


# === Task 8, Step 2: lifecycle, disclosure, batches and evidence =============


def _only_event(data_path: Path) -> dict[str, object]:
    rows = _rows(
        data_path, "SELECT canonical_event FROM audit_events ORDER BY sequence"
    )
    assert len(rows) == 1
    return cast(dict[str, object], json.loads(cast(bytes, rows[0][0])))


def _insert_fact_rows(data_path: Path, fact_ids: tuple[UUID, ...]) -> None:
    """Bulk source rows in one transaction, for the batch-bound tests."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, derived_from, promoted_by, evidence_id, "
            "valid_from, valid_to, recorded_at) "
            "VALUES (?, ?, ?, ?, 'candidate', 'internal', ?, NULL, NULL, NULL, "
            "NULL, NULL, ?)",
            [
                (
                    str(fact_id),
                    _REALM,
                    _json_column(
                        [
                            {"id": s.identifier, "kind": s.kind}
                            for s in _SOURCE_SCOPE.segments
                        ]
                    ),
                    f"bulk source {index}",
                    str(_SOURCE_ASSERTION_ID),
                    _TS,
                )
                for index, fact_id in enumerate(fact_ids)
            ],
        )
        connection.commit()


def _bulk_fact_ids(count: int) -> tuple[UUID, ...]:
    return tuple(
        UUID(f"cccccccc-{index:04x}-4ccc-8ccc-cccccccccccc") for index in range(count)
    )


# --- I-67 lifecycle refusals -------------------------------------------------


def test_a_failed_approach_source_is_an_invalid_request(tmp_path: Path) -> None:
    """A failure preserved as a failure is the point of the trust vocabulary;
    promoting one would launder it into a validated belief."""
    _seed_promotable(tmp_path, trust=TrustClass.FAILED_APPROACH)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "failed_approach_source")
    ]


def test_an_invalidated_source_is_an_invalid_request(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)
    _invalidate_fact_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "source_invalidated")
    ]


def test_a_promotion_replay_returns_the_original_receipt_after_its_source_is_invalidated(
    tmp_path: Path,
) -> None:
    """``source_invalidated`` is a data precondition, checked on first
    execution only.

    The promotion happened and the derived validated fact exists; invalidating
    the *source* does not invalidate what was derived from it. Denying the
    replay would report "denied" about a row sitting committed in the
    catalogue, and invite the one client response that does real damage —
    retrying under a fresh idempotency key and promoting twice.

    Checking it inside the mutation transaction is what produces this
    behaviour, because ``mutate_idempotent`` serves a stored result without
    ever entering that transaction. Move the check back out to the outer gate
    and this test fails.

    Grants remain freshly evaluated on every replay; see
    ``test_a_promotion_replay_after_the_promote_grant_is_revoked_is_freshly_denied``
    for the other half of that line.
    """
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)
    command = _promote_command()

    first = _promote(authority, command)
    assert isinstance(first, Committed)
    _invalidate_fact_row(tmp_path)
    second = _promote(authority, command)

    assert isinstance(second, Replayed)
    assert second.value == first.value
    assert second.mutation_receipt == first.mutation_receipt
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "allow", "facts_promoted"),
        ("realm", "data", "promote", "allow", "idempotent_replay"),
    ]
    # A replay reports what already happened; it does not happen again.
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(1,)]


def test_a_candidate_source_can_be_promoted(tmp_path: Path) -> None:
    _seed_promotable(tmp_path, trust=TrustClass.CANDIDATE)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert _only_event(tmp_path)["trust_transition"] == {
        "from": "candidate",
        "to": "validated",
    }


def test_a_validated_source_can_be_promoted(tmp_path: Path) -> None:
    """Re-promoting an already-validated fact on fresh evidence is legitimate:
    the derived fact is a new belief with a new evidence reference, not an
    edit of the old one."""
    _seed_promotable(tmp_path, trust=TrustClass.VALIDATED)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert _only_event(tmp_path)["trust_transition"] == {
        "from": "validated",
        "to": "validated",
    }


# --- I-67 disclosure ---------------------------------------------------------


def test_an_unknown_source_and_an_unauthorised_source_are_publicly_identical(
    tmp_path: Path,
) -> None:
    """The binding disclosure test. Two catalogues, the same command, the same
    named identity: in one the fact exists but the actor may not see it, in
    the other it does not exist at all. Both refusals must be publicly
    indistinguishable — which is why they share the instance chain, since the
    unknown case has no realm to file under.

    The assertion is exact rather than approximate: the two canonical audit
    events must be equal once ``reason_code`` is removed. That covers the
    chain, the sequence, the fingerprint and the failure together, and would
    fail if any of them leaked the difference.
    """
    unauthorised = tmp_path / "unauthorised"
    unknown = tmp_path / "unknown"
    unauthorised.mkdir()
    unknown.mkdir()
    _seed_promotable(unauthorised, retrieve_segments=(_SIBLING_JOB,))
    _seed_catalogue(unknown)
    _seed_promoter(unknown)

    denied = _promote(_authority(unauthorised), _promote_command())
    missing = _promote(_authority(unknown), _promote_command())

    assert isinstance(denied, Rejected)
    assert isinstance(missing, Rejected)
    assert denied.failure == missing.failure
    assert denied.audit_receipt.chain_kind is missing.audit_receipt.chain_kind
    assert denied.audit_receipt.chain_identity == missing.audit_receipt.chain_identity
    assert denied.audit_receipt.sequence == missing.audit_receipt.sequence

    denied_event = _only_event(unauthorised)
    missing_event = _only_event(unknown)
    assert denied_event["reason_code"] == "source_retrieve_denied"
    assert missing_event["reason_code"] == "fact_unknown"
    assert denied_event.pop("reason_code") != missing_event.pop("reason_code")
    assert denied_event == missing_event


def test_an_instance_chain_promote_refusal_carries_a_request_fingerprint(
    tmp_path: Path,
) -> None:
    """Correlatable without disclosure: realm auditors cannot see these events
    at all, so the fingerprint is what lets an operator tie a reported
    correlation id to the request that produced it."""
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert (
        _only_event(tmp_path)["safe_request_fingerprint"]
        == hashlib.sha256(
            json.dumps(
                [str(_SOURCE_FACT_ID)], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


# --- P-13 batch homogeneity and identity -------------------------------------


def test_heterogeneous_source_scopes_are_an_invalid_request(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_PARENT_SCOPE)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "heterogeneous_batch")
    ]


def test_heterogeneous_source_classifications_are_an_invalid_request(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)
    _insert_fact_row(
        tmp_path, _SECOND_FACT_ID, classification=Classification.RESTRICTED
    )

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "heterogeneous_batch")
    ]


def test_heterogeneous_source_trusts_are_an_invalid_request(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, trust=TrustClass.VALIDATED)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "heterogeneous_batch")
    ]


def test_a_homogeneous_two_source_batch_is_accepted(tmp_path: Path) -> None:
    """Guards the three above from passing for the wrong reason: a two-source
    batch is fine when scope, classification and trust all agree."""
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, body=_OTHER_BODY)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Committed)
    assert len(outcome.value.promotions) == 2
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(4,)]


def test_a_duplicate_source_identity_is_an_invalid_request(tmp_path: Path) -> None:
    """Unlike ingest, whose batch carries drafts with no identity to repeat,
    a promotion names its sources — so the same fact twice would derive two
    facts from one and put a duplicate into the audit identity set."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _SOURCE_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "duplicate_identity")
    ]


def test_an_empty_promotion_batch_is_rejected(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command(fact_ids=()))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "empty_batch")
    ]


def test_a_hundred_and_one_source_promotion_batch_is_rejected(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(fact_ids=_bulk_fact_ids(101))
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "batch_too_large")
    ]


def test_a_hundred_source_promotion_batch_is_accepted(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)
    fact_ids = _bulk_fact_ids(100)
    _insert_fact_rows(tmp_path, fact_ids)

    outcome = _promote(_authority(tmp_path), _promote_command(fact_ids=fact_ids))

    assert isinstance(outcome, Committed)
    assert len(outcome.value.promotions) == 100
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(100,)]


# --- evidence ----------------------------------------------------------------


def test_named_evidence_at_an_ancestor_of_the_source_scope_is_accepted(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_evidence_row(tmp_path, scope=_PARENT_SCOPE)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id == _NAMED_EVIDENCE_ID


def test_named_evidence_at_a_sibling_scope_is_denied(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_evidence_row(tmp_path, scope=_SIBLING_SCOPE)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "evidence_scope_invalid")
    ]


def test_named_evidence_at_a_descendant_scope_is_denied(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_evidence_row(tmp_path, scope=_CHILD_SCOPE)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "evidence_scope_invalid")
    ]


def test_named_evidence_in_another_realm_is_denied(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_evidence_row(tmp_path, scope=_CROSS_REALM_SCOPE)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "evidence_scope_invalid")
    ]


def test_named_evidence_above_the_actors_read_clearance_is_denied(
    tmp_path: Path,
) -> None:
    """The source is within clearance and the evidence is not — so this cannot
    pass by riding on the source check."""
    _seed_promotable(
        tmp_path,
        classification=Classification.INTERNAL,
        evidence_classification=Classification.RESTRICTED,
        read_clearance=Classification.INTERNAL,
    )

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "evidence_clearance_exceeded")
    ]


def _external_reference(
    *, uri: str = _EXTERNAL_URI, digest: bytes = _EXTERNAL_DIGEST
) -> ExternalEvidenceReference:
    return ExternalEvidenceReference(external_uri=uri, payload_digest=digest)


def test_an_inline_external_reference_creates_an_external_custody_record(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(evidence=_external_reference(), target_scope=_PARENT_SCOPE),
    )

    assert isinstance(outcome, Committed)
    assert _rows(
        tmp_path,
        "SELECT evidence_id, realm_id, scope_segments, classification, "
        "payload_digest, assertion_id, payload_length, external_uri, recorded_at "
        f"FROM evidence_records WHERE evidence_id = '{outcome.value.evidence_id}'",
    ) == [
        (
            str(outcome.value.evidence_id),
            _REALM,
            _json_column([{"id": "job-1", "kind": "job"}]),
            "internal",
            _EXTERNAL_DIGEST,
            None,
            None,
            _EXTERNAL_URI,
            _TS,
        )
    ]


def test_an_inline_external_reference_writes_no_evidence_outbox_row(
    tmp_path: Path,
) -> None:
    """P-14/I-16: Cairn never sees the referenced bytes, so there is no payload
    to hand to Attic and no store-payload work to enqueue."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(evidence=_external_reference())
    )

    assert isinstance(outcome, Committed)
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_outbox") == [(0,)]


def test_an_inline_external_reference_works_with_exact_evidence_disabled(
    tmp_path: Path,
) -> None:
    """Promotion stays available when the exact-evidence store is absent —
    only payload-bearing ingest is refused."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path, exact_evidence_enabled=False),
        _promote_command(evidence=_external_reference()),
    )

    assert isinstance(outcome, Committed)


def test_the_derived_fact_names_the_inline_evidence_record(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(evidence=_external_reference())
    )

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[8] == str(outcome.value.evidence_id)


def test_an_invalid_external_uri_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(evidence=_external_reference(uri="not a uri at all")),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_uri")
    ]


def test_an_invalid_external_payload_digest_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(evidence=_external_reference(digest=b"too short")),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_digest")
    ]


# --- inherited validity, reason and hostile stored rows ----------------------


def test_the_derived_fact_inherits_the_sources_validity_window(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(
        tmp_path,
        valid_from="2026-08-01T00:00:00.000000Z",
        valid_to="2026-09-01T00:00:00.000000Z",
    )
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[9:11] == (
        "2026-08-01T00:00:00.000000Z",
        "2026-09-01T00:00:00.000000Z",
    )


def test_the_derived_facts_recorded_at_is_the_single_captured_instant(
    tmp_path: Path,
) -> None:
    """A shared gap the round 2 review found on invalidation's receipt and
    noted promotion has no equivalent guard against either: ``_authority``'s
    constant clock makes a second ``self._clock()`` read unfalsifiable, so a
    mutant taking a fresh one for the derived fact's ``recorded_at`` would
    pass every existing test. An advancing clock, as ingest's own
    ``test_both_authorisation_evaluations_use_one_captured_instant`` uses,
    proves it: a stray second read would store ``_NOW + 1h`` here."""
    _seed_promotable(tmp_path)
    instants = iter([_NOW] + [_NOW + timedelta(hours=1)] * 32)
    authority = CairnAuthority(
        tmp_path,
        _transactions(tmp_path),
        clock=lambda: next(instants),
        uuid_factory=_uuid_seq(0x20000000),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )

    outcome = _promote(authority, _promote_command())

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _derived_row(tmp_path, derived_id)[11] == _TS


def test_an_over_long_promotion_reason_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command(reason="x" * 4097))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_reason")
    ]


def test_an_empty_promotion_reason_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command(reason=""))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_reason")
    ]


def test_a_hostile_segment_id_on_a_stored_source_row_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """Promotion is the first command to build records from stored rows rather
    than caller input, and migration 0003 constrains a ``scope_segments``
    column to a minified JSON array and nothing more. This row passes every
    SQL CHECK. Without the value layer's own guard it would reach the audit
    scope index and raise a raw constraint violation while recording the very
    denial meant to contain it."""
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, scope_segments='[{"id":"bad id!","kind":"job"}]')
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_scope_segment_id")
    ]


def test_a_non_string_segment_kind_on_a_stored_source_row_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """``[{"kind": 7, "id": 1}]`` parses to ints without anything having been
    tampered with, and would raise ``TypeError`` from inside ``re`` — no code,
    no audit reason, no durable event."""
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, scope_segments='[{"id":1,"kind":7}]')
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_scope_segment_kind")
    ]


def test_a_stored_source_scope_of_the_wrong_json_shape_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """The CHECK pins a JSON array; it cannot pin that each element is an
    object carrying exactly ``kind`` and ``id``."""
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, scope_segments="[1]")
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_scope")
    ]


def test_a_malformed_stored_valid_from_is_a_typed_denial(tmp_path: Path) -> None:
    """The same failure mode as a hostile stored scope, one column across.

    ``ck_facts_valid_from`` enforces only ``length = 27`` and a GLOB over
    ``[0-9TZ:.-]``, so a well-shaped but impossible instant is a legal stored
    row. ``parse_timestamp`` raises ``CatalogueStorageError``, which is
    neither a ``CustodyValueError`` nor an ``AuditValueError`` — so without a
    guard it escapes the command layer entirely, with no durable event and a
    raw exception reaching the caller.
    """
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, valid_from="2026-13-45T99:99:99.000000Z")
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "timestamp_malformed")
    ]


def test_a_malformed_stored_valid_to_is_a_typed_denial(tmp_path: Path) -> None:
    """``valid_to`` is a second call site, guarded separately."""
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, valid_to="2026-02-30T00:00:00.000000Z")
    _insert_evidence_row(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "timestamp_malformed")
    ]


def test_a_malformed_caller_supplied_target_scope_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """The caller-supplied target scope, as distinct from a stored one. This
    is the earliest guard in the pipeline and the only one that runs before
    any read."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(
            target_scope=_unvalidated_scope((_tampered_segment(identifier="bad id!"),))
        ),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "promote", "deny", "invalid_scope_segment_id")
    ]


def test_a_malformed_target_scope_refusal_still_carries_a_fingerprint(
    tmp_path: Path,
) -> None:
    """Every instance-chain promote refusal is correlatable, not just the ones
    about source identities — realm auditors cannot see any of them."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(target_scope=_unvalidated_scope(_over_long_segments())),
    )

    assert isinstance(outcome, Rejected)
    assert (
        _only_event(tmp_path)["safe_request_fingerprint"]
        == hashlib.sha256(
            json.dumps(
                [str(_SOURCE_FACT_ID)], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def test_a_hostile_stored_scope_writes_no_custody_rows(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_promoter(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, scope_segments='[{"id":"bad id!","kind":"job"}]')

    _promote(_authority(tmp_path), _promote_command())

    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]


# === Task 8, Step 3: receipt, audit, atomicity and idempotency ===============


def _two_source_command() -> PromoteFacts:
    """Sources named in an order that is *not* their sorted order, so a
    receipt that quietly sorted its pairs would be caught."""
    assert str(_SECOND_FACT_ID) > str(_SOURCE_FACT_ID)
    return _promote_command(fact_ids=(_SECOND_FACT_ID, _SOURCE_FACT_ID))


def test_promotion_pairs_preserve_command_order(tmp_path: Path) -> None:
    """Two orderings in one operation. The receipt pairs each source with what
    it became, in the order the caller named them; the audit event lists every
    touched identity sorted. Derived identities are handed out in *descending*
    string order here, so neither ordering can agree with the other by
    accident."""
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, body=_OTHER_BODY)

    outcome = _promote(
        _authority(tmp_path, uuid_factory=_uuid_descending(0x2000000F)),
        _two_source_command(),
    )

    assert isinstance(outcome, Committed)
    (first_source, first_derived), (second_source, second_derived) = (
        outcome.value.promotions
    )
    assert (first_source, second_source) == (_SECOND_FACT_ID, _SOURCE_FACT_ID)
    assert str(first_derived) > str(second_derived)
    assert _derived_row(tmp_path, first_derived)[2] == _OTHER_BODY
    assert _derived_row(tmp_path, second_derived)[2] == _BODY


def test_the_audit_event_lists_the_derived_facts_sorted_and_not_the_sources(
    tmp_path: Path,
) -> None:
    """``affected_*`` names what the mutation changed, not what the command
    named. Promotion inserts derived facts and writes no source row, so the
    sources are cited by the mutation result and this event's ``source_scope``
    but are not ``affected``."""
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, body=_OTHER_BODY)

    outcome = _promote(
        _authority(tmp_path, uuid_factory=_uuid_descending(0x2000000F)),
        _two_source_command(),
    )

    assert isinstance(outcome, Committed)
    derived = [str(derived) for _, derived in outcome.value.promotions]
    sources = [str(source) for source, _ in outcome.value.promotions]
    affected = _only_event(tmp_path)["affected_fact_ids"]
    assert affected == sorted(derived)
    # The descending uuid_factory makes derived order differ from sorted
    # order, so the sort is proven rather than coincidental.
    assert affected != derived
    assert not set(affected) & set(sources)


def test_the_allow_event_records_both_scopes_and_both_transitions(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path, classification=Classification.INTERNAL)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(
            target_scope=_PARENT_SCOPE,
            target_classification=Classification.RESTRICTED,
        ),
    )

    assert isinstance(outcome, Committed)
    event = _only_event(tmp_path)
    assert event["source_scope"] == {
        "realm": _REALM,
        "segments": [{"id": "job-1", "kind": "job"}, {"id": "step-3", "kind": "step"}],
    }
    assert event["target_scope"] == {
        "realm": _REALM,
        "segments": [{"id": "job-1", "kind": "job"}],
    }
    assert event["classification_transition"] == {
        "from": "internal",
        "to": "restricted",
    }
    assert event["trust_transition"] == {"from": "candidate", "to": "validated"}
    assert event["requested_scope"] is None


def test_the_allow_event_carries_both_evidence_fields(tmp_path: Path) -> None:
    """P-16: an allow promote event requires both, and ``AuditDraft`` refuses
    one without the other."""
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    event = _only_event(tmp_path)
    assert event["evidence_reference"] == str(_NAMED_EVIDENCE_ID)
    assert event["evidence_digest"] == _EVIDENCE_DIGEST.hex()
    assert event["affected_evidence_ids"] == [str(_NAMED_EVIDENCE_ID)]
    assert event["grant_id"] == str(_PROMOTE_GRANT_ID)


def test_promotion_writes_one_fact_promoted_projection_row_per_derived_fact(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, body=_OTHER_BODY)

    outcome = _promote(_authority(tmp_path), _two_source_command())

    assert isinstance(outcome, Committed)
    mutation_id = str(outcome.mutation_receipt.mutation_id)
    assert _rows(
        tmp_path,
        "SELECT kind, fact_id, mutation_id, created_at, attempts "
        "FROM projection_outbox ORDER BY fact_id",
    ) == sorted(
        ("fact-promoted", str(derived), mutation_id, _TS, 0)
        for _, derived in outcome.value.promotions
    )


def test_one_unknown_source_rejects_the_whole_batch_with_no_partial_custody(
    tmp_path: Path,
) -> None:
    """I-66: a batch is one atomic custody decision."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(fact_ids=(_SOURCE_FACT_ID, _UNKNOWN_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    # Every custody table, checked against the seeded fixture rather than
    # against a hand-picked subset — the seeded counts are what "unchanged"
    # means here, since the fixture legitimately holds one of several of them.
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM evidence_records") == [(1,)]
    for table in ("fact_invalidations", "evidence_outbox", "projection_outbox"):
        assert _rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]
    assert set(_CUSTODY_TABLES) == {
        "assertions",
        "facts",
        "evidence_records",
        "fact_invalidations",
        "evidence_outbox",
        "projection_outbox",
    }


def test_promotion_identity_assignment_follows_evidence_then_facts_then_work(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(evidence=_external_reference())
    )

    assert isinstance(outcome, Committed)
    assert outcome.value.evidence_id == UUID("20000000-0000-4000-8000-000000000000")
    assert outcome.value.promotions[0][1] == UUID(
        "20000001-0000-4000-8000-000000000000"
    )
    assert _rows(tmp_path, "SELECT work_id FROM projection_outbox") == [
        ("20000002-0000-4000-8000-000000000000",)
    ]


def test_a_promotion_survives_a_restart(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path), _promote_command(evidence=_external_reference())
    )

    assert isinstance(outcome, Committed)
    (_, derived_id) = outcome.value.promotions[0]
    assert _restart_query(
        tmp_path, f"SELECT trust FROM facts WHERE fact_id = '{derived_id}'"
    ) == [("validated",)]
    assert _restart_query(
        tmp_path,
        f"SELECT external_uri FROM evidence_records "
        f"WHERE evidence_id = '{outcome.value.evidence_id}'",
    ) == [(_EXTERNAL_URI,)]


# --- P-07: the authoritative in-transaction evaluation -----------------------


def _revoke_promote_grant(data_path: Path) -> None:
    _revoke_grant_row(data_path, _PROMOTE_GRANT_ID)


def _swap_promote_grant(data_path: Path) -> None:
    _insert_grant(
        data_path,
        grant_id=_REPLACEMENT_GRANT_ID,
        operations=frozenset({GrantOperation.PROMOTE}),
    )
    _revoke_grant_row(data_path, _PROMOTE_GRANT_ID)


def _racing_authority(
    tmp_path: Path, interfere: Callable[[Path], None]
) -> CairnAuthority:
    return _authority(
        tmp_path,
        transactions=_GrantRaceTransactions(
            tmp_path,
            writer_gate=threading.Lock(),
            clock=lambda: _NOW,
            uuid_factory=_uuid_seq(0x10000000),
            interfere=interfere,
        ),
    )


def test_the_in_transaction_evaluation_catches_a_promote_grant_revoked_mid_request(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _racing_authority(tmp_path, _revoke_promote_grant), _promote_command()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_promote_denied")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]


def test_the_in_transaction_evaluation_catches_a_retrieve_grant_revoked_mid_request(
    tmp_path: Path,
) -> None:
    """Promotion needs both grants, so both must be re-derived under the write
    lock — re-checking only the one the event names would leave the other
    raceable."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _racing_authority(
            tmp_path, lambda path: _revoke_grant_row(path, _RETRIEVE_GRANT_ID)
        ),
        _promote_command(),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "source_retrieve_denied")
    ]


def test_the_in_transaction_evaluation_fails_closed_on_a_swapped_promote_grant(
    tmp_path: Path,
) -> None:
    """A concurrent writer swaps the promote grant for a different one that
    would also authorise. Still refused: the audit draft already names the
    grant the outer gate found. The closed vocabulary reports this as
    ``target_promote_denied``, so the swap is indistinguishable from an
    outright revocation even in the audit reason — no new code was invented
    for it."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _racing_authority(tmp_path, _swap_promote_grant), _promote_command()
    )

    # The replacement really does authorise on its own, so this is not the
    # no-grant branch wearing a different hat.
    assert _rows(
        tmp_path,
        "SELECT count(*) FROM grants g WHERE NOT EXISTS "
        "(SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.grant_id) "
        "AND g.operations LIKE '%promote%'",
    ) == [(1,)]
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "target_promote_denied")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]


def test_the_in_transaction_evaluation_catches_a_source_invalidated_mid_request(
    tmp_path: Path,
) -> None:
    """Invalidation is the only mutable fact about a source: rows are
    immutable, but ``fact_invalidations`` is additive and the outer read
    happens on a different connection before the writer gate is taken. A guard
    that can be raced is not a guard."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _racing_authority(tmp_path, _invalidate_fact_row), _promote_command()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "source_invalidated")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]


def test_promotes_in_transaction_evaluation_rolls_back_on_a_hostile_grant_row(
    tmp_path: Path,
) -> None:
    """The promotion sibling of ingest's equivalent test: a concurrent writer
    plants a second, hostile grant row for the same principal and realm,
    reached from _revalidate_promotion's own grant re-derivation. Must roll
    back through the normal MutationRejection route, with no derived fact,
    evidence record or outbox row surviving."""
    _seed_promotable(tmp_path)

    outcome = _promote(
        _racing_authority(
            tmp_path,
            lambda path: _insert_grant_raw(
                path, grant_id=_HOSTILE_GRANT_ID, operations='["not-a-real-op"]'
            ),
        ),
        _promote_command(),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "grant_enum_malformed")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]


# --- idempotency and content-freedom -----------------------------------------


def test_an_identical_promotion_replay_returns_the_original_receipt(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)
    command = _promote_command()

    first = _promote(authority, command)
    second = _promote(authority, command)

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    # Reconstructed by the decode path from stored bytes: the pair structure
    # and every identity must survive the round trip.
    assert isinstance(second.value, FactsPromoted)
    assert second.value == first.value
    assert second.mutation_receipt == first.mutation_receipt
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "allow", "facts_promoted"),
        ("realm", "data", "promote", "allow", "idempotent_replay"),
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]


def test_a_promotion_replay_after_the_promote_grant_is_revoked_is_freshly_denied(
    tmp_path: Path,
) -> None:
    """I-44: a replay is authorised afresh, never served from the idempotency
    record after the grant behind it has gone.

    The counterpart to
    ``test_a_promotion_replay_returns_the_original_receipt_after_its_source_is_invalidated``,
    and together they pin where the line falls: **authorisation** is
    re-evaluated on every replay, a **data precondition** is not. Keeping both
    is what proves the invalidation reversal did not weaken I-44.
    """
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)
    command = _promote_command()

    first = _promote(authority, command)
    assert isinstance(first, Committed)
    _revoke_grant_row(tmp_path, _PROMOTE_GRANT_ID)
    second = _promote(authority, command)

    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path)[1] == (
        "realm",
        "data",
        "promote",
        "deny",
        "target_promote_denied",
    )


def test_the_same_key_with_a_different_promotion_is_an_idempotency_conflict(
    tmp_path: Path,
) -> None:
    """The reason is part of the digest, so re-using a key with a different
    stated justification is a conflict rather than a silent replay."""
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)

    first = _promote(authority, _promote_command())
    second = _promote(authority, _promote_command(reason="a different reason"))

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.IDEMPOTENCY_CONFLICT


def test_promotion_result_bytes_are_content_free(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    surfaces = [
        cast(
            bytes, _rows(tmp_path, "SELECT result_bytes FROM idempotency_records")[0][0]
        )
    ] + [
        cast(bytes, event)
        for (event,) in _rows(tmp_path, "SELECT canonical_event FROM audit_events")
    ]
    assert len(surfaces) > 1
    for surface in surfaces:
        assert _BODY.encode() not in surface
        assert _REASON.encode() not in surface


def test_a_promotion_reason_is_stored_in_no_column_at_all(tmp_path: Path) -> None:
    """A design observation, pinned so it cannot change silently. Under the
    approved schema a promotion's stated justification has nowhere durable to
    go: ``facts`` has no reason column, ``evidence_records`` has none, and
    there is no promotions table. It survives only as an input to the P-20
    command digest, which is what makes the same key with a different reason
    an idempotency conflict. An invalidation's reason, by contrast, lands in
    ``fact_invalidations.reason``."""
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert _column_occurrences(tmp_path, _REASON.encode()) == set()


def test_a_promoted_body_is_stored_in_no_column_but_facts_body(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert _column_occurrences(tmp_path, _BODY.encode()) == {("facts", "body")}


def test_the_promote_command_digest_is_sha256_of_the_p20_document(
    tmp_path: Path,
) -> None:
    """The P-20 document is pinned as a literal, not recomputed from the code
    under test: a change to its field set, ordering or value forms must break
    this test rather than silently redefine command identity."""
    _seed_promotable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, body=_OTHER_BODY)
    command = _promote_command(
        fact_ids=(_SECOND_FACT_ID, _SOURCE_FACT_ID),
        evidence=_external_reference(),
        target_scope=_PARENT_SCOPE,
        target_classification=Classification.RESTRICTED,
    )

    outcome = _promote(_authority(tmp_path), command)

    expected_document = (
        b'{"command":"promote",'
        b'"evidence":{"external_uri":"' + _EXTERNAL_URI.encode() + b'",'
        b'"payload_digest":"' + _EXTERNAL_DIGEST.hex().encode() + b'"},'
        b'"fact_ids":["' + str(_SECOND_FACT_ID).encode() + b'",'
        b'"' + str(_SOURCE_FACT_ID).encode() + b'"],'
        b'"reason":"' + _REASON.encode() + b'",'
        b'"schema":"cairn.authority/v1",'
        b'"target_classification":"restricted",'
        b'"target_scope":{"realm":"acme","segments":['
        b'{"id":"job-1","kind":"job"}]}}'
    )
    assert isinstance(outcome, Committed)
    assert (
        outcome.mutation_receipt.command_digest
        == hashlib.sha256(expected_document).digest()
    )
    assert (
        _only_event(tmp_path)["command_digest"]
        == hashlib.sha256(expected_document).hexdigest()
    )


def test_the_promote_digest_renders_an_absent_target_as_null(tmp_path: Path) -> None:
    """The inherited case is a distinct document, not an omitted field: a
    command that names its target explicitly and one that inherits it must not
    collide on the same idempotency key."""
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    expected_document = (
        b'{"command":"promote",'
        b'"evidence":{"evidence_id":"' + str(_NAMED_EVIDENCE_ID).encode() + b'"},'
        b'"fact_ids":["' + str(_SOURCE_FACT_ID).encode() + b'"],'
        b'"reason":"' + _REASON.encode() + b'",'
        b'"schema":"cairn.authority/v1",'
        b'"target_classification":null,'
        b'"target_scope":null}'
    )
    assert isinstance(outcome, Committed)
    assert (
        outcome.mutation_receipt.command_digest
        == hashlib.sha256(expected_document).digest()
    )


def _events(data_path: Path) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(cast(bytes, row[0])))
        for row in _rows(
            data_path,
            "SELECT canonical_event FROM audit_events ORDER BY chain_kind, sequence",
        )
    ]


def _stored_identities(data_path: Path) -> tuple[set[object], set[object]]:
    return (
        {row[0] for row in _rows(data_path, "SELECT fact_id FROM facts")},
        {
            row[0]
            for row in _rows(data_path, "SELECT evidence_id FROM evidence_records")
        },
    )


def test_a_promotion_replay_event_names_only_identities_that_exist(
    tmp_path: Path,
) -> None:
    """The audit chain is the load-bearing artefact, so an event may not cite
    identities that were never created.

    A replay re-runs everything up to ``mutate_idempotent``, including minting
    fresh UUIDs for the evidence record and derived facts — none of which are
    written, because the mutation callback never runs. Carrying those into the
    replay event would make it a false record, and a P-16 evidence reference
    to a record that does not exist is the worst case of it. Inline external
    evidence is used here because its identity is minted rather than supplied,
    so the defect is visible in ``evidence_reference`` as well as in the fact
    identities.
    """
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)
    command = _promote_command(evidence=_external_reference())

    first = _promote(authority, command)
    second = _promote(authority, command)

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    allow, replay = _events(tmp_path)
    assert replay["reason_code"] == "idempotent_replay"
    stored_facts, stored_evidence = _stored_identities(tmp_path)
    assert set(cast(list[object], replay["affected_fact_ids"])) <= stored_facts
    assert set(cast(list[object], replay["affected_evidence_ids"])) <= stored_evidence
    assert replay["evidence_reference"] in stored_evidence
    # The honest identities are the originals, so the two events agree.
    assert replay["affected_fact_ids"] == allow["affected_fact_ids"]
    assert replay["affected_evidence_ids"] == allow["affected_evidence_ids"]
    assert replay["evidence_reference"] == allow["evidence_reference"]


def test_a_payload_bearing_ingest_replay_event_names_only_identities_that_exist(
    tmp_path: Path,
) -> None:
    """The same defect on ingest, which has carried it since Task 7. The
    assertion, fact and evidence identities are all minted before
    ``mutate_idempotent`` and none are written on a replay."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)
    command = _payload_ingest()

    first = _ingest(authority, command)
    second = _ingest(authority, command)

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    allow, replay = _events(tmp_path)
    stored_facts, stored_evidence = _stored_identities(tmp_path)
    stored_assertions = {
        row[0] for row in _rows(tmp_path, "SELECT assertion_id FROM assertions")
    }
    assert set(cast(list[object], replay["affected_fact_ids"])) <= stored_facts
    assert set(cast(list[object], replay["affected_evidence_ids"])) <= stored_evidence
    assert (
        set(cast(list[object], replay["affected_assertion_ids"])) <= stored_assertions
    )
    assert replay["evidence_reference"] in stored_evidence
    assert replay["affected_fact_ids"] == allow["affected_fact_ids"]
    assert replay["affected_assertion_ids"] == allow["affected_assertion_ids"]
    assert replay["evidence_reference"] == allow["evidence_reference"]


def test_an_idempotency_conflict_event_claims_no_affected_identities(
    tmp_path: Path,
) -> None:
    """A conflict refuses a different command outright, so nothing was
    affected. The caller's draft describes the mutation that *would* have
    happened, and its freshly minted identities were never written."""
    _seed_promotable(tmp_path)
    authority = _authority(tmp_path)

    first = _promote(authority, _promote_command())
    second = _promote(authority, _promote_command(reason="a different reason"))

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    conflict = _events(tmp_path)[1]
    assert conflict["reason_code"] == "idempotency_conflict"
    assert conflict["affected_fact_ids"] == []
    assert conflict["affected_evidence_ids"] == []
    assert conflict["affected_assertion_ids"] == []


def _ambiguous_authority(tmp_path: Path) -> CairnAuthority:
    """A transaction store whose commit is lost *before* it lands, so the
    mutation's writes are rolled back and the idempotency record is absent —
    the branch that drives `_resolve_commit_ambiguity` to append its own
    ``error`` event rather than recognising a successful commit."""

    def lose_the_commit(_connection: sqlite3.Connection) -> None:
        raise CommitAmbiguity

    return _authority(
        tmp_path,
        transactions=CatalogueTransactions(
            tmp_path,
            writer_gate=threading.Lock(),
            clock=lambda: _NOW,
            uuid_factory=_uuid_seq(0x10000000),
            commit=lose_the_commit,
        ),
    )


def test_a_commit_ambiguity_event_clears_the_evidence_fields(tmp_path: Path) -> None:
    """The other half of the P-16 shared-machinery fix, which the conflict
    test does not reach. ``_resolve_commit_ambiguity`` turns the same allow
    draft into an ``error`` event, and P-16 permits evidence fields only on an
    allow event — so without clearing them ``AuditDraft`` refuses to be built
    and the ambiguity resolution crashes instead of recording anything."""
    _seed_promotable(tmp_path)

    with pytest.raises(CatalogueTransactionError):
        _promote(_ambiguous_authority(tmp_path), _promote_command())

    ambiguous = _events(tmp_path)[0]
    assert ambiguous["outcome"] == "error"
    assert ambiguous["reason_code"] == "commit_ambiguous"
    assert ambiguous["evidence_reference"] is None
    assert ambiguous["evidence_digest"] is None


def test_a_lost_commit_rolls_back_the_inline_evidence_record(tmp_path: Path) -> None:
    """Batch atomicity below the outer gate. Every other atomicity test here
    refuses before the transaction opens, so nothing has been written to roll
    back; this one fails after the inline evidence record and the derived
    facts are inserted, which is the only path that exercises the rollback."""
    _seed_promotable(tmp_path)

    with pytest.raises(CatalogueTransactionError):
        _promote(
            _ambiguous_authority(tmp_path),
            _promote_command(evidence=_external_reference()),
        )

    # Only the seeded row survives: the inline external-custody record went
    # with the rolled-back transaction.
    assert _rows(tmp_path, "SELECT evidence_id FROM evidence_records") == [
        (str(_NAMED_EVIDENCE_ID),)
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]


def test_the_promotion_result_schema_is_pinned(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command())

    assert isinstance(outcome, Committed)
    assert _rows(tmp_path, "SELECT result_schema FROM idempotency_records") == [
        ("cairn.authority.promotion/v1",)
    ]


# === Step 1: invalidation =====================================================

_INVALIDATE_REASON = "invalidation-reason-marker: verified by an independent check"
_INVALIDATE_GRANT_ID = UUID("eeeeeeee-5555-4eee-8eee-eeeeeeeeeeee")


def _invalidate_command(
    *,
    fact_ids: tuple[UUID, ...] = (_SOURCE_FACT_ID,),
    reason: str = _INVALIDATE_REASON,
    superseded_by: UUID | None = None,
) -> InvalidateFacts:
    return InvalidateFacts(
        fact_ids=fact_ids, reason=reason, superseded_by=superseded_by
    )


def _invalidate(
    authority: CairnAuthority,
    command: InvalidateFacts,
    *,
    actor: Actor | None = None,
    idempotency_key: UUID = _IDEMPOTENCY_KEY,
) -> object:
    return authority.invalidate(
        actor or _agent_actor(),
        command,
        idempotency_key=idempotency_key,
        correlation_id=_CORRELATION_ID,
    )


def _seed_invalidator(
    data_path: Path, *, segments: tuple[ScopeSegment, ...] = _SOURCE_SCOPE.segments
) -> None:
    """A workload principal holding one live ``invalidate`` grant."""
    _insert_principal(data_path, _AGENT_ID, label="agent")
    _insert_credential(data_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_grant(
        data_path,
        grant_id=_INVALIDATE_GRANT_ID,
        segments=segments,
        operations=frozenset({GrantOperation.INVALIDATE}),
    )


def _seed_invalidatable(
    data_path: Path,
    *,
    realms: tuple[str, ...] = (_REALM,),
    fact_id: UUID = _SOURCE_FACT_ID,
    scope: Scope = _SOURCE_SCOPE,
    trust: TrustClass = TrustClass.CANDIDATE,
    classification: Classification = Classification.INTERNAL,
) -> None:
    """The whole happy-path fixture: catalogue, invalidator, one fact at
    ``_SOURCE_SCOPE``."""
    _seed_catalogue(data_path, realms=realms)
    _seed_invalidator(data_path)
    _insert_assertion_row(data_path, classification=classification)
    _insert_fact_row(
        data_path, fact_id, scope=scope, trust=trust, classification=classification
    )


def _fact_invalidation_row(
    data_path: Path, fact_id: UUID = _SOURCE_FACT_ID
) -> tuple[object, ...]:
    return _rows(
        data_path,
        "SELECT fact_id, invalidated_at, principal_id, superseded_by, reason "
        f"FROM fact_invalidations WHERE fact_id = '{fact_id}'",
    )[0]


def test_a_duplicate_invalidation_identity_is_an_invalid_request(
    tmp_path: Path,
) -> None:
    """As for promotion: InvalidateFacts names its facts by identity, so the
    same fact twice would append two fact_invalidations rows for one fact —
    ck_fact_invalidations' PRIMARY KEY on fact_id would refuse the second
    insert as a raw constraint violation if this were not caught first."""
    _seed_catalogue(tmp_path)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SOURCE_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "duplicate_identity")
    ]


def test_an_empty_invalidation_batch_is_rejected(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command(fact_ids=()))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "empty_batch")
    ]


def test_a_hundred_and_one_fact_invalidation_batch_is_rejected(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)

    outcome = _invalidate(
        _authority(tmp_path), _invalidate_command(fact_ids=_bulk_fact_ids(101))
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "batch_too_large")
    ]


def test_an_over_long_invalidation_reason_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command(reason="x" * 4097))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalid_reason")
    ]


def test_an_empty_invalidation_reason_is_a_typed_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command(reason=""))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalid_reason")
    ]


# --- visibility: fact_unknown / invalidate_grant_not_held ---------------------


def test_invalidation_denied_without_any_invalidate_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalidate_grant_not_held")
    ]


def test_an_unknown_fact_identity_is_a_coarse_denial(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path)

    outcome = _invalidate(
        _authority(tmp_path), _invalidate_command(fact_ids=(_SOURCE_FACT_ID,))
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "fact_unknown")
    ]


def test_an_unknown_fact_and_an_unauthorised_fact_are_publicly_identical(
    tmp_path: Path,
) -> None:
    """The binding disclosure test, as for promotion: in one catalogue the
    fact exists but the actor's only invalidate grant sits at a sibling
    scope, in the other it does not exist at all. Both refusals must be
    publicly indistinguishable, which is why they share the instance chain —
    the unknown case has no realm to file under."""
    unauthorised = tmp_path / "unauthorised"
    unknown = tmp_path / "unknown"
    unauthorised.mkdir()
    unknown.mkdir()
    _seed_catalogue(unauthorised)
    _seed_invalidator(unauthorised, segments=(_SIBLING_JOB,))
    _insert_assertion_row(unauthorised)
    _insert_fact_row(unauthorised)
    _seed_catalogue(unknown)
    _seed_invalidator(unknown)

    denied = _invalidate(_authority(unauthorised), _invalidate_command())
    missing = _invalidate(_authority(unknown), _invalidate_command())

    assert isinstance(denied, Rejected)
    assert isinstance(missing, Rejected)
    assert denied.failure == missing.failure
    assert denied.audit_receipt.chain_kind is missing.audit_receipt.chain_kind
    assert denied.audit_receipt.chain_identity == missing.audit_receipt.chain_identity
    assert denied.audit_receipt.sequence == missing.audit_receipt.sequence

    denied_event = _only_event(unauthorised)
    missing_event = _only_event(unknown)
    assert denied_event["reason_code"] == "invalidate_grant_not_held"
    assert missing_event["reason_code"] == "fact_unknown"
    assert denied_event.pop("reason_code") != missing_event.pop("reason_code")
    assert denied_event == missing_event


def test_a_batch_with_no_invalidate_grant_naming_facts_in_different_scopes_is_refused_as_grant_not_held(  # noqa: E501
    tmp_path: Path,
) -> None:
    """The ordering pin the team lead asked for. An actor holding no
    invalidate grant at all, naming two real facts in different scopes, must
    receive ``invalidate_grant_not_held`` — not ``heterogeneous_batch``. The
    latter would tell an unauthorised actor that both identities exist and
    that they span more than one scope, which is exactly the disclosure I-67
    forbids. This only holds if the grant check runs per fact before
    homogeneity is evaluated; with the checks the other way round this test
    fails."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, _SOURCE_FACT_ID, scope=_SOURCE_SCOPE)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_PARENT_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalidate_grant_not_held")
    ]


def test_partially_authorised_batch_is_refused_as_grant_not_held(
    tmp_path: Path,
) -> None:
    """The IMPORTANT 2 coverage hole from round 2's review. The test above
    only proves the ordering for an actor holding *no* grant at all — under
    which the first fact already fails visibility, so the test would pass
    even if the loop stopped after one iteration. This is the case that
    actually needs every fact checked: the actor's grant covers the first
    fact's scope but not the second's, so only a per-fact loop that keeps
    going catches it as invalidate_grant_not_held rather than falling
    through to heterogeneous_batch, which would disclose that the second
    identity exists and sits elsewhere."""
    _seed_invalidatable(tmp_path)  # grant covers _SOURCE_SCOPE only
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_PARENT_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalidate_grant_not_held")
    ]


def test_an_unknown_fact_in_second_position_is_a_coarse_denial(tmp_path: Path) -> None:
    """The companion coverage hole: an unknown fact named *after* an
    authorised one must still be fact_unknown, not a raw KeyError from
    _heterogeneous_scope trying to look up an identity the visibility loop
    never confirmed exists — which is exactly what would happen if the loop
    stopped checking after the first fact passed."""
    _seed_invalidatable(tmp_path)  # only _SOURCE_FACT_ID exists

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "fact_unknown")
    ]


# --- P-13 homogeneity: stored scope only, narrower than promotion -------------


def test_heterogeneous_stored_scopes_are_an_invalid_request(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path, segments=())  # realm-root grant covers both
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, _SOURCE_FACT_ID, scope=_SOURCE_SCOPE)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_PARENT_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "heterogeneous_batch")
    ]


def test_a_homogeneous_batch_with_different_classification_and_trust_is_accepted(
    tmp_path: Path,
) -> None:
    """P-13 binds invalidation to stored scope only — narrower than
    promotion's three dimensions. Two facts at the same scope but different
    classification and trust must NOT be refused as heterogeneous."""
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(
        tmp_path,
        _SOURCE_FACT_ID,
        trust=TrustClass.CANDIDATE,
        classification=Classification.INTERNAL,
    )
    _insert_fact_row(
        tmp_path,
        _SECOND_FACT_ID,
        trust=TrustClass.VALIDATED,
        classification=Classification.RESTRICTED,
    )

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Committed)
    assert isinstance(outcome.value, FactsInvalidated)


# --- MUT-01's invalidation half: append-only, byte-identical fact rows -------


def test_invalidation_appends_an_immutable_row_and_leaves_the_fact_byte_identical(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    before = _full_fact_rows(tmp_path)
    assert len(before) == 1

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Committed)
    after = _full_fact_rows(tmp_path)
    assert len(after) == 1
    assert after[0] == before[0]
    assert _fact_invalidation_row(tmp_path) == (
        str(_SOURCE_FACT_ID),
        _TS,
        str(_AGENT_ID),
        None,
        _INVALIDATE_REASON,
    )


def test_the_allow_event_names_the_invalidate_grant(tmp_path: Path) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Committed)
    assert _only_event(tmp_path)["grant_id"] == str(_INVALIDATE_GRANT_ID)


def test_a_deny_invalidate_denial_also_names_the_invalidate_grant(
    tmp_path: Path,
) -> None:
    """Pins the divergence from _deny_promotion, which always leaves
    grant_id None: invalidation has only one relevant grant, already settled
    by the time _deny_invalidate is reachable (superseded_by_unknown is the
    one denial routed through it), and naming it is the acting principal's
    own grant — nothing about anyone else is disclosed.

    _revalidate_invalidate's own in-transaction denials (invalidate_grant_
    not_held on a race, fact_already_invalidated) are untouched by this
    ruling and still carry grant_id=None, matching _revalidate_promotion's
    established pattern for its own in-transaction denials — this test is
    about _deny_invalidate specifically, not every invalidate denial."""
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(superseded_by=UUID("cccccccc-9999-4ccc-8ccc-cccccccccccc")),
    )

    assert isinstance(outcome, Rejected)
    assert _only_event(tmp_path)["reason_code"] == "superseded_by_unknown"
    assert _only_event(tmp_path)["grant_id"] == str(_INVALIDATE_GRANT_ID)


# --- superseded_by --------------------------------------------------------


def test_superseded_by_naming_an_existing_fact_in_the_same_realm_succeeds(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(superseded_by=_SECOND_FACT_ID),
    )

    assert isinstance(outcome, Committed)
    assert _fact_invalidation_row(tmp_path)[3] == str(_SECOND_FACT_ID)


def test_superseded_by_unknown_and_in_another_realm_are_publicly_identical(
    tmp_path: Path,
) -> None:
    """The second I-67 disclosure pin. By this point standing over the named
    facts is already proven, so the realm is legitimately known — what must
    stay indistinguishable is a superseded_by that does not exist at all
    versus one that exists but in a different realm."""
    _OTHER_REALM = "acme-annex"
    unknown = tmp_path / "unknown"
    foreign = tmp_path / "foreign"
    unknown.mkdir()
    foreign.mkdir()
    _seed_invalidatable(unknown)
    _seed_invalidatable(foreign, realms=(_REALM, _OTHER_REALM))
    with _open_write_connection(foreign, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, derived_from, promoted_by, evidence_id, "
            "valid_from, valid_to, recorded_at) "
            "VALUES (?, ?, ?, ?, 'candidate', 'internal', ?, NULL, NULL, NULL, "
            "NULL, NULL, ?)",
            (
                str(_SECOND_FACT_ID),
                _OTHER_REALM,
                _json_column([{"id": s.identifier, "kind": s.kind} for s in (_JOB,)]),
                _BODY,
                str(_SOURCE_ASSERTION_ID),
                _TS,
            ),
        )
        connection.commit()

    missing = _invalidate(
        _authority(unknown), _invalidate_command(superseded_by=_SECOND_FACT_ID)
    )
    foreign_result = _invalidate(
        _authority(foreign), _invalidate_command(superseded_by=_SECOND_FACT_ID)
    )

    assert isinstance(missing, Rejected)
    assert isinstance(foreign_result, Rejected)
    assert missing.failure == foreign_result.failure
    assert missing.audit_receipt.chain_kind is foreign_result.audit_receipt.chain_kind
    assert (
        missing.audit_receipt.chain_identity
        == foreign_result.audit_receipt.chain_identity
    )
    missing_event = _only_event(unknown)
    foreign_event = _only_event(foreign)
    assert missing_event["reason_code"] == "superseded_by_unknown"
    assert foreign_event["reason_code"] == "superseded_by_unknown"
    assert missing_event == foreign_event


# --- fact_already_invalidated: a data precondition, checked once -------------


def test_an_already_invalidated_fact_is_an_invalid_request(tmp_path: Path) -> None:
    _seed_invalidatable(tmp_path)
    _invalidate_fact_row(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "fact_already_invalidated")
    ]


def test_fact_already_invalidated_rejects_the_whole_batch_with_no_partial_custody(
    tmp_path: Path,
) -> None:
    """I-66: a batch is one atomic custody decision. Checked against every
    custody table, as Task 8's equivalent does — not a hand-picked subset —
    with the seeded fixture's own counts as what "unchanged" means for the
    tables it legitimately populates."""
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)
    _invalidate_fact_row(tmp_path, _SOURCE_FACT_ID)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Rejected)
    assert _rows(tmp_path, "SELECT count(*) FROM assertions") == [(1,)]
    assert _rows(tmp_path, "SELECT count(*) FROM facts") == [(2,)]
    assert _rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(1,)]
    for table in ("evidence_records", "evidence_outbox", "projection_outbox"):
        assert _rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]
    assert set(_CUSTODY_TABLES) == {
        "assertions",
        "facts",
        "fact_invalidations",
        "evidence_records",
        "evidence_outbox",
        "projection_outbox",
    }


def test_an_invalidation_replay_returns_the_original_receipt(tmp_path: Path) -> None:
    """The replay-safety counterpart to the two tests above: replaying the
    identical request returns the original receipt rather than being denied
    as fact_already_invalidated, because that check runs only inside the
    mutation transaction, which a replay never enters (by analogy with
    Task 8's source_invalidated ruling, §10.3)."""
    _seed_invalidatable(tmp_path)
    authority = _authority(tmp_path)
    command = _invalidate_command()

    first = _invalidate(authority, command)
    second = _invalidate(authority, command)

    assert isinstance(first, Committed)
    assert isinstance(second, Replayed)
    assert second.value == first.value
    assert second.mutation_receipt == first.mutation_receipt
    assert _rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(1,)]
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "allow", "facts_invalidated"),
        ("realm", "data", "invalidate", "allow", "idempotent_replay"),
    ]


def test_replay_after_the_invalidate_grant_is_revoked_is_freshly_denied(
    tmp_path: Path,
) -> None:
    """I-44: a replay must be authorised afresh."""
    _seed_invalidatable(tmp_path)
    authority = _authority(tmp_path)
    command = _invalidate_command()

    first = _invalidate(authority, command)
    assert isinstance(first, Committed)

    _revoke_grant_row(tmp_path, _INVALIDATE_GRANT_ID)
    second = _invalidate(authority, command)

    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.AUTHORISATION_DENIED
    # _audit_rows orders by (chain_kind, sequence); "instance" sorts before
    # "realm" alphabetically, so the second (denied) event lists first even
    # though the allow happened first chronologically.
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalidate_grant_not_held"),
        ("realm", "data", "invalidate", "allow", "facts_invalidated"),
    ]


def test_the_same_key_with_a_different_invalidation_is_an_idempotency_conflict(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)
    authority = _authority(tmp_path)

    first = _invalidate(authority, _invalidate_command(fact_ids=(_SOURCE_FACT_ID,)))
    second = _invalidate(authority, _invalidate_command(fact_ids=(_SECOND_FACT_ID,)))

    assert isinstance(first, Committed)
    assert isinstance(second, Rejected)
    assert second.failure.code is FailureCode.IDEMPOTENCY_CONFLICT


# --- P-07: the authoritative in-transaction evaluation ------------------------


def _revoke_invalidate_grant(data_path: Path) -> None:
    _revoke_grant_row(data_path, _INVALIDATE_GRANT_ID)


def _swap_invalidate_grant(data_path: Path) -> None:
    _insert_grant(
        data_path,
        grant_id=_REPLACEMENT_GRANT_ID,
        segments=_SOURCE_SCOPE.segments,
        operations=frozenset({GrantOperation.INVALIDATE}),
    )
    _revoke_grant_row(data_path, _INVALIDATE_GRANT_ID)


def test_the_in_transaction_evaluation_catches_an_invalidate_grant_revoked_mid_request(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _racing_authority(tmp_path, _revoke_invalidate_grant), _invalidate_command()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "invalidate_grant_not_held")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(0,)]


def test_the_in_transaction_evaluation_fails_closed_on_a_swapped_invalidate_grant(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _racing_authority(tmp_path, _swap_invalidate_grant), _invalidate_command()
    )

    assert _rows(
        tmp_path,
        "SELECT count(*) FROM grants g WHERE NOT EXISTS "
        "(SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.grant_id) "
        "AND g.operations LIKE '%invalidate%'",
    ) == [(1,)]
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "invalidate_grant_not_held")
    ]


def test_the_in_transaction_evaluation_catches_a_concurrent_invalidation_mid_request(
    tmp_path: Path,
) -> None:
    """The P-07 window Task 8 flagged and left for this task to make
    reachable: fact_invalidations is additive and the outer read happens on a
    different connection before the writer gate is taken."""
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _racing_authority(tmp_path, _invalidate_fact_row), _invalidate_command()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "fact_already_invalidated")
    ]
    # Only the interfering write's row survives; ours was never appended.
    assert _rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(1,)]


def test_invalidates_in_transaction_evaluation_rolls_back_on_a_hostile_grant_row(
    tmp_path: Path,
) -> None:
    """The invalidate sibling of the ingest and promote versions of this
    test: a concurrent writer plants a second, hostile grant row for the
    same principal and realm, reached from _revalidate_invalidate's own
    grant re-derivation rather than the outer gate's."""
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _racing_authority(
            tmp_path,
            lambda path: _insert_grant_raw(
                path, grant_id=_HOSTILE_GRANT_ID, operations='["not-a-real-op"]'
            ),
        ),
        _invalidate_command(),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "grant_enum_malformed")
    ]
    assert _rows(tmp_path, "SELECT count(*) FROM fact_invalidations") == [(0,)]
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(0,)]


# --- batch bound, projection outbox, receipt shape ----------------------------


def test_a_hundred_fact_invalidation_batch_is_accepted(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path, segments=())
    _insert_assertion_row(tmp_path)
    fact_ids = _bulk_fact_ids(100)
    _insert_fact_rows(tmp_path, fact_ids)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command(fact_ids=fact_ids))

    assert isinstance(outcome, Committed)
    assert len(outcome.value.fact_ids) == 100
    assert _rows(tmp_path, "SELECT count(*) FROM projection_outbox") == [(100,)]


def test_invalidation_writes_one_fact_invalidated_projection_row_per_fact(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SOURCE_FACT_ID, _SECOND_FACT_ID)),
    )

    assert isinstance(outcome, Committed)
    assert _rows(
        tmp_path, "SELECT kind, fact_id FROM projection_outbox ORDER BY fact_id"
    ) == [
        ("fact-invalidated", str(_SOURCE_FACT_ID)),
        ("fact-invalidated", str(_SECOND_FACT_ID)),
    ]


def test_the_receipt_carries_sorted_identities_and_one_shared_invalidated_at(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)

    # Command order is descending; the receipt must still come back sorted.
    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(fact_ids=(_SECOND_FACT_ID, _SOURCE_FACT_ID)),
    )

    assert isinstance(outcome, Committed)
    assert outcome.value.fact_ids == tuple(
        sorted((_SOURCE_FACT_ID, _SECOND_FACT_ID), key=str)
    )
    assert outcome.value.invalidated_at == _NOW


def test_the_shared_invalidated_at_is_the_single_captured_instant(
    tmp_path: Path,
) -> None:
    """The Minor item promoted in round 2's review: with ``_authority``'s
    constant clock, the assertion above (``invalidated_at == _NOW``) cannot
    distinguish the captured ``effective_at`` from a fresh ``self._clock()``
    read — both equal ``_NOW``. A mutant taking a fresh read for the
    receipt's ``invalidated_at`` survived every prior test. An advancing
    clock, as ingest's own equivalent test uses, closes that: a stray second
    read would return ``_NOW + 1h`` here instead."""
    _seed_invalidatable(tmp_path)
    instants = iter([_NOW] + [_NOW + timedelta(hours=1)] * 32)
    authority = CairnAuthority(
        tmp_path,
        _transactions(tmp_path),
        clock=lambda: next(instants),
        uuid_factory=_uuid_seq(0x20000000),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )

    outcome = _invalidate(authority, _invalidate_command())

    assert isinstance(outcome, Committed)
    assert outcome.value.invalidated_at == _NOW
    assert _rows(tmp_path, "SELECT invalidated_at FROM fact_invalidations") == [(_TS,)]


# --- P-20 digest and result schema --------------------------------------------


def test_the_invalidate_command_digest_is_sha256_of_the_p20_document(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(superseded_by=None),
    )

    assert isinstance(outcome, Committed)
    expected = hashlib.sha256(
        json.dumps(
            {
                "command": "invalidate",
                "schema": "cairn.authority/v1",
                "fact_ids": [str(_SOURCE_FACT_ID)],
                "reason": _INVALIDATE_REASON,
                "superseded_by": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()
    assert outcome.mutation_receipt.command_digest == expected
    assert _only_event(tmp_path)["command_digest"] == expected.hex()


def test_the_invalidate_digest_renders_a_present_superseded_by(
    tmp_path: Path,
) -> None:
    """The companion to the test above: a digest that rendered
    ``superseded_by`` as ``null`` regardless of the command would let a
    different command replay under the same idempotency key."""
    _seed_invalidatable(tmp_path)
    _insert_fact_row(tmp_path, _SECOND_FACT_ID, scope=_SOURCE_SCOPE)

    outcome = _invalidate(
        _authority(tmp_path),
        _invalidate_command(superseded_by=_SECOND_FACT_ID),
    )

    assert isinstance(outcome, Committed)
    expected = hashlib.sha256(
        json.dumps(
            {
                "command": "invalidate",
                "schema": "cairn.authority/v1",
                "fact_ids": [str(_SOURCE_FACT_ID)],
                "reason": _INVALIDATE_REASON,
                "superseded_by": str(_SECOND_FACT_ID),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()
    assert outcome.mutation_receipt.command_digest == expected


def test_the_invalidation_result_schema_is_pinned(tmp_path: Path) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Committed)
    assert _rows(tmp_path, "SELECT result_schema FROM idempotency_records") == [
        ("cairn.authority.invalidation/v1",)
    ]


# --- hostile stored fact rows, reached via invalidate too ---------------------


def test_a_hostile_stored_fact_scope_reached_via_invalidate_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """_load_sources is inherited verbatim from promotion; this pins that its
    guard fires on invalidation's own path too, not only promotion's."""
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, scope_segments="[1]")

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalid_scope")
    ]


def test_a_malformed_stored_valid_from_reached_via_invalidate_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_invalidator(tmp_path)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path, valid_from="2026-13-45T99:99:99.000000Z")

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "timestamp_malformed")
    ]


# --- gate.py: hostile stored grant rows, reached from every command ----------


def _insert_grant_raw(
    data_path: Path,
    *,
    grant_id: UUID | str,
    scope_segments: str = '[{"kind":"job","id":"job-1"}]',
    operations: str = '["invalidate"]',
    write_classifications: str = '["public","internal","restricted"]',
    issued_by: str | None = None,
    expires_at: str = _FUTURE_TS,
    created_at: str = _TS,
) -> None:
    """Plants a grant row with caller-supplied column values, bypassing
    ``_insert_grant``'s canonical encoding — needed to reach a row the schema
    accepts but the value layer must refuse. Grants are immutable
    (``trg_grants_no_update``), so a hostile value can only be reached via a
    fresh insert, never an update of the seeded grant. ``grant_id`` accepts a
    raw string so a hostile-but-schema-legal identifier can be planted too,
    not only a hostile-but-schema-legal *value* of an otherwise well-formed
    identifier."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                str(grant_id),
                str(_AGENT_ID),
                _REALM,
                scope_segments,
                operations,
                "restricted",
                write_classifications,
                issued_by,
                expires_at,
                created_at,
            ),
        )
        connection.commit()


def test_a_hostile_stored_grant_scope_segment_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """gate._row_to_grant read scope_segments unguarded before this task: a
    row of shape ``[1]`` raised a raw TypeError from subscripting an int, with
    no code and no durable event, reached from every command that loads
    grants — not only invalidation's own."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(tmp_path, grant_id=_INVALIDATE_GRANT_ID, scope_segments="[1]")

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "invalid_scope")
    ]


def test_a_malformed_stored_grant_expires_at_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """The defect routed in from Task 8's review: gate._row_to_grant called
    parse_timestamp on grants.expires_at/created_at with no guard, and
    ck_grants_expires_at/ck_grants_created_at are shape-only GLOBs — the same
    gap _stored_timestamp closes for facts."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(
        tmp_path,
        grant_id=_INVALIDATE_GRANT_ID,
        expires_at="2026-13-45T99:99:99.000000Z",
    )

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "timestamp_malformed")
    ]


def test_a_malformed_stored_grant_created_at_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(
        tmp_path,
        grant_id=_INVALIDATE_GRANT_ID,
        created_at="2026-13-45T99:99:99.000000Z",
    )

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "timestamp_malformed")
    ]


def test_a_malformed_stored_grant_operations_entry_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """A second sibling of the routed-in defect, found during the
    shape-without-meaning audit and fixed on the team lead's ruling:
    ck_grants_operations constrains valid-JSON-array shape only, not that
    each element is one of the closed GrantOperation spellings, so
    GrantOperation(value) raised a raw, uncoded ValueError. Probed directly
    against grants_for_principal before fixing:
    PROBE[grantops.RAISED]: ValueError 'not-a-real-op' is not a valid
    GrantOperation."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(
        tmp_path,
        grant_id=_INVALIDATE_GRANT_ID,
        operations='["not-a-real-op"]',
    )

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "grant_enum_malformed")
    ]


_MALFORMED_UUID = "0000000--0000-4000-8000-000000000000"


def test_a_malformed_stored_grant_id_is_a_typed_denial(tmp_path: Path) -> None:
    """CRITICAL 1 from round 2's review: ck_grants_grant_id's GLOB is
    shape-only, not total. ``?`` matches any character and the negative
    class ``NOT GLOB '*[^0-9a-f-]*'`` permits ``-`` anywhere, so a dash can
    sit in the wrong position and still pass. ``_MALFORMED_UUID`` is 36
    characters of only hex digits and dashes; ``UUID(value)`` raises a raw,
    uncoded ``ValueError`` on it regardless. Probed directly:
    PROBE[grant_id.RAISED]: ValueError badly formed hexadecimal UUID
    string."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(tmp_path, grant_id=_MALFORMED_UUID)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "grant_uuid_malformed")
    ]


def test_a_malformed_stored_grant_issued_by_is_a_typed_denial(tmp_path: Path) -> None:
    """The ``issued_by`` sibling. Reachable only because a hostile
    ``issued_by`` must satisfy ``fk_grants_issued_by``, and
    ``principals.principal_id`` has the identical shape-only GLOB gap — so
    the same class of hostile row is planted on the *referenced* principal
    first, then cited by the grant's foreign key."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_principal(tmp_path, _MALFORMED_UUID, label="hostile-issuer")
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(
        tmp_path, grant_id=_INVALIDATE_GRANT_ID, issued_by=_MALFORMED_UUID
    )

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "grant_uuid_malformed")
    ]


def test_a_malformed_stored_grant_write_classifications_entry_is_a_typed_denial(
    tmp_path: Path,
) -> None:
    """The write_classifications sibling of the test above — same gap, same
    shared code, a different enum type (Classification rather than
    GrantOperation)."""
    _seed_catalogue(tmp_path)
    _insert_principal(tmp_path, _AGENT_ID, label="agent")
    _insert_credential(tmp_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_assertion_row(tmp_path)
    _insert_fact_row(tmp_path)
    _insert_grant_raw(
        tmp_path,
        grant_id=_INVALIDATE_GRANT_ID,
        write_classifications='["not-a-classification"]',
    )

    outcome = _invalidate(_authority(tmp_path), _invalidate_command())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "invalidate", "deny", "grant_enum_malformed")
    ]


# === Final review: two mutants the suite let live =============================


def _seed_for_promote_refusal(data_path: Path) -> None:
    _seed_catalogue(data_path)
    _seed_promoter(data_path)


def _call_promote_refusal(data_path: Path) -> object:
    """Refused at the visibility check — inside the outer read block, with a
    live ``fetch`` in hand."""
    return _promote(_authority(data_path), _promote_command())


def _seed_for_invalidate_refusal(data_path: Path) -> None:
    _seed_catalogue(data_path)
    _seed_invalidator(data_path)


def _call_invalidate_refusal(data_path: Path) -> object:
    return _invalidate(_authority(data_path), _invalidate_command())


def _seed_for_unknown_realm_refusal(data_path: Path) -> None:
    _seed_catalogue(data_path)
    _seed_ingester(data_path)


def _call_unknown_realm_refusal(data_path: Path) -> object:
    return _ingest(_authority(data_path), _command(scope=Scope("nonexistent", (_JOB,))))


@pytest.mark.parametrize(
    ("name", "seed", "call"),
    [
        ("promote", _seed_for_promote_refusal, _call_promote_refusal),
        ("invalidate", _seed_for_invalidate_refusal, _call_invalidate_refusal),
        (
            "unknown-realm",
            _seed_for_unknown_realm_refusal,
            _call_unknown_realm_refusal,
        ),
    ],
)
def test_an_instance_refusal_reuses_the_already_open_read_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    seed: Callable[[Path], None],
    call: Callable[[Path], object],
) -> None:
    """``_reject_instance`` takes a ``fetch`` precisely so a refusal decided
    inside the outer read block does not open a second connection to read one
    row of catalogue metadata.

    Dropping ``fetch=fetch`` at those call sites changes no outcome, no
    failure and no audit event — SQLite is happy to hand out a second reader
    — so every other assertion in this file keeps passing. That is what makes
    the omission invisible, and why the connection count has to be asserted
    directly rather than inferred from a result.
    """
    seed(tmp_path)
    opened = 0

    def counting(data_path: Path) -> AbstractContextManager[sqlite3.Connection]:
        nonlocal opened
        opened += 1
        return read_connection(data_path)

    monkeypatch.setattr(mutations, "read_connection", counting)

    outcome = call(tmp_path)

    assert isinstance(outcome, Rejected)
    assert opened == 1


def test_a_commit_ambiguity_event_keeps_the_identity_sets(tmp_path: Path) -> None:
    """The deliberate asymmetry with ``_replay_or_reject``'s conflict draft,
    which clears all four sets three lines away in the same module.

    A conflict refuses a *different* command outright, so nothing was
    affected. An ambiguous commit is the opposite case: the transaction may or
    may not have landed, and the whole purpose of the ``error`` event is to
    tell an operator which identities they now have to go and check. Clearing
    them would leave the one event about an unknown outcome asserting that
    nothing was touched.

    The set it keeps is the allow draft's, so it names the derived identity
    and not the source — which is the right answer for exactly the reason
    this event exists. An operator here is reconciling *what may or may not
    now exist*. The derived fact is precisely that. The source is not: it was
    there before, it is there now, and promotion would not have written it on
    either outcome, so naming it would send the operator to look at the one
    row the ambiguity cannot have touched.

    Pinned because clearing them leaves the suite green, and next to the
    evidence-field clearing directly above it reads like an oversight rather
    than a decision.
    """
    _seed_promotable(tmp_path)

    with pytest.raises(CatalogueTransactionError):
        _promote(_ambiguous_authority(tmp_path), _promote_command())

    ambiguous = _events(tmp_path)[0]
    assert ambiguous["reason_code"] == "commit_ambiguous"
    fact_ids = cast(list[str], ambiguous["affected_fact_ids"])
    assert str(_SOURCE_FACT_ID) not in fact_ids
    assert len(fact_ids) == 1
    assert ambiguous["affected_evidence_ids"] == [str(_NAMED_EVIDENCE_ID)]


# === The custody secret screen (I-74, P-26) =================================
#
# Every literal below was run through the real ``SecretScreen`` before being
# written here, per the Task 2 lesson: a hand-authored credential that does not
# actually trip its own rule turns a red test green for the wrong reason.

_PEM_BODY = (
    "the deploy key is\n"
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA\n"
    "-----END RSA PRIVATE KEY-----"
)
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
# Canonical JSON, because ``_validate_metadata`` re-serialises and compares:
# keys sorted, no spaces. A hand-written literal in any other shape fails as
# ``invalid_metadata`` before the screen ever sees it.
_SECRET_METADATA = '{"aws_key":"AKIAIOSFODNN7EXAMPLE","runner":"ci"}'
_SECRET_URI = "https://ci:AKIAIOSFODNN7EXAMPLE@artifacts.internal/build/42.log"
_PEM_RULE = f"{POLICY_VERSION}/pem-block"
_AWS_RULE = f"{POLICY_VERSION}/upstream/AWSKeyDetector"
# A synthetic digest the Task 11 leak sweep found evading the canonical
# metadata string: the pinned detector extracts quoted strings from text,
# and JSON escaping of the caller's own quotes (``\"``) hides this one
# entirely, so the same content refused in a fact body committed in
# metadata. Screened as a member value it trips its rule again.
_ESCAPED_HEX_VALUE = (
    'digest "3f8a1c9e7b5d2046f1a3c9e7b5d2048a6c4e2d0b8f7a5931e6c4a2d08b7f59e13"'
)
_ESCAPED_HEX_METADATA = json.dumps(
    {"note": _ESCAPED_HEX_VALUE}, sort_keys=True, separators=(",", ":")
)
_HEX_RULE = f"{POLICY_VERSION}/upstream/HexHighEntropyString"


def _custody_rows(data_path: Path) -> dict[str, int]:
    """Every table a rejected mutation must not have written, counted.

    Inherits ``_CUSTODY_TABLES`` rather than relisting it — that constant
    exists precisely so a later check cannot quietly omit a table — and adds
    ``idempotency_records``, which is not custody but which I-74 requires to
    be absent too: a cached rejection would let a retry of the same
    secret-bearing request skip the screen.
    """
    return {
        table: cast(int, _rows(data_path, f"SELECT count(*) FROM {table}")[0][0])
        for table in _CUSTODY_TABLES + ("idempotency_records",)
    }


def test_ingest_rejects_a_secret_in_a_fact_body(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(facts=(_draft(_PEM_BODY),)))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_PEM_RULE, field_path="facts[0].body"
    )
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "secret_pem_block")
    ]


def test_ingest_rejects_a_secret_in_the_canonical_metadata(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(metadata=_SECRET_METADATA))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_AWS_RULE, field_path="metadata"
    )
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "secret_upstream_awskeydetector")
    ]


def test_ingest_screens_each_metadata_member_not_only_the_whole_string(
    tmp_path: Path,
) -> None:
    """The canonical string alone is not sufficient, proven by content it
    misses: escaped quotes hide this digest from every rule when the whole
    document is screened, and the member screen catches it. The field path
    is positional — a key may itself be the secret, and a path travels back
    to the caller and into the safe log."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    assert SecretScreen().screen("metadata", _ESCAPED_HEX_METADATA) == ()

    outcome = _ingest(_authority(tmp_path), _command(metadata=_ESCAPED_HEX_METADATA))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_HEX_RULE, field_path="metadata[0].value"
    )
    assert _custody_rows(tmp_path) == {
        table: 0 for table in _CUSTODY_TABLES + ("idempotency_records",)
    }
    assert _ESCAPED_HEX_VALUE.encode() not in _catalogue_bytes(tmp_path)


def test_ingest_screens_a_secret_bearing_metadata_key(tmp_path: Path) -> None:
    """A key is caller-authored text like any other, and its field path
    names its position rather than its text."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    metadata = json.dumps(
        {_ESCAPED_HEX_VALUE: "ci"}, sort_keys=True, separators=(",", ":")
    )

    outcome = _ingest(_authority(tmp_path), _command(metadata=metadata))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_HEX_RULE, field_path="metadata[0].key"
    )


def test_ingest_rejects_a_secret_in_the_decoded_evidence_payload(
    tmp_path: Path,
) -> None:
    """EVIDENCE-04 at module level: the payload is the only field that reaches
    Attic, and it is screened before the outbox row that would carry it."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path), _command(evidence_payload=_AWS_KEY.encode())
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_AWS_RULE, field_path="evidence_payload"
    )


def test_a_binary_evidence_payload_is_screened_rather_than_skipped(
    tmp_path: Path,
) -> None:
    """The decode is a policy choice, not a detail. These bytes are not valid
    UTF-8 — a strict decode raises on them — so any implementation that
    decoded strictly and gave up on failure would let the key straight
    through. ``errors="replace"`` damages only the undecodable bytes and
    leaves the surrounding ASCII screenable."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    payload = b"\x89PNG\r\n\x1a\n\xff\xfe" + _AWS_KEY.encode() + b"\xc3\x28"
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")

    outcome = _ingest(_authority(tmp_path), _command(evidence_payload=payload))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED


# === I-96: attested-digest masking at the custody screen seam ===============
#
# The migration envelope quotes the SHA-256 of its own fact body in ingest
# metadata and repeats it in the verbatim-row evidence payload; a quoted
# 64-hex string is precisely what ``HexHighEntropyString`` exists to catch.
# I-96 masks every literal occurrence of an *attested* digest — the SHA-256
# of a sibling fact body in the same command — on the screening copy only.
# Every literal below was run through the real ``SecretScreen``.

_ATTESTED_BODY = "the deploy pipeline uses kaniko"
_ATTESTED_DIGEST = hashlib.sha256(_ATTESTED_BODY.encode("utf-8")).hexdigest()
_ATTESTED_METADATA = json.dumps(
    {"content_sha256": _ATTESTED_DIGEST}, sort_keys=True, separators=(",", ":")
)


def test_ingest_admits_the_attested_digest_of_a_fact_body_in_metadata(
    tmp_path: Path,
) -> None:
    """The guard proves the canonical metadata string trips the hex rule on
    its own, so the admission below is the mask's doing and not a quiet
    policy change."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    assert _rule_identities_of(
        SecretScreen().screen("metadata", _ATTESTED_METADATA)
    ) == {_HEX_RULE}

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=_ATTESTED_METADATA),
    )

    assert isinstance(outcome, Committed)
    # The mask is a screening copy only (I-30): the stored metadata keeps
    # the digest it arrived with.
    assert _ATTESTED_DIGEST.encode() in _catalogue_bytes(tmp_path)


def test_a_near_miss_of_the_attested_digest_is_still_rejected(
    tmp_path: Path,
) -> None:
    """One flipped hex character is not the digest of any fact body, so the
    hex rule keeps its finding."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    near_miss = _ATTESTED_DIGEST[:-1] + ("0" if _ATTESTED_DIGEST[-1] != "0" else "1")
    metadata = json.dumps(
        {"content_sha256": near_miss}, sort_keys=True, separators=(",", ":")
    )

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_HEX_RULE, field_path="metadata"
    )


def test_a_digest_attested_by_no_fact_body_is_not_masked(tmp_path: Path) -> None:
    """The exemption is semantic: a 64-hex string is masked only when it is
    the SHA-256 of a sibling fact body in the same command."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    foreign = hashlib.sha256(b"a body this command does not carry").hexdigest()
    metadata = json.dumps(
        {"content_sha256": foreign}, sort_keys=True, separators=(",", ":")
    )

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_HEX_RULE, field_path="metadata"
    )


def test_a_fact_body_is_never_masked(tmp_path: Path) -> None:
    """A digest quoted inside a fact body is that body's own content, and the
    body is what the digests attest — masking there would let the attested
    value hide a finding in the field it came from."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    quoting_body = f'the access token is "{_ATTESTED_DIGEST}"'

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY), _draft(quoting_body))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION,
        rule=f"{POLICY_VERSION}/contextual-entropy",
        field_path="facts[1].body",
    )


def test_the_attested_digest_is_masked_in_a_metadata_member_value(
    tmp_path: Path,
) -> None:
    """The escaped-quote shape from the Task 11 leak sweep: invisible to the
    whole-string screen, caught at the member — so the member's own copy
    must be masked too."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    member_value = f'digest "{_ATTESTED_DIGEST}"'
    metadata = json.dumps({"note": member_value}, sort_keys=True, separators=(",", ":"))
    assert SecretScreen().screen("metadata", metadata) == ()
    assert _rule_identities_of(SecretScreen().screen("m", member_value)) == {_HEX_RULE}

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Committed)


def test_a_digest_embedded_in_a_token_shaped_value_is_not_masked(
    tmp_path: Path,
) -> None:
    """Operator's F1 ruling on the I-96 review, 23 August 2026: masking is
    standalone-occurrence only. A credential constructed around a known
    digest — chosen after the body, no preimage required — must keep its
    pattern finding; substring replacement would have destroyed it."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    metadata = json.dumps(
        {"key": f"sk-{_ATTESTED_DIGEST}"}, sort_keys=True, separators=(",", ":")
    )
    assert _rule_identities_of(SecretScreen().screen("metadata", metadata)) == {
        f"{POLICY_VERSION}/provider-token"
    }

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION,
        rule=f"{POLICY_VERSION}/provider-token",
        field_path="metadata",
    )


def test_a_format_separator_does_not_make_an_embedded_digest_standalone(
    tmp_path: Path,
) -> None:
    """Re-review blocking finding over ``db8b510``: the boundary decision
    must use the same normalised rendering the screen judges. A U+200B
    between ``sk-`` and the digest is stripped by the P-36 fold, joining
    the credential the raw-text boundary saw as two values."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    value = f"sk-​{_ATTESTED_DIGEST}"
    metadata = json.dumps(
        {"key": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert _rule_identities_of(SecretScreen().screen("m", value)) == {
        f"{POLICY_VERSION}/provider-token"
    }

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION,
        rule=f"{POLICY_VERSION}/provider-token",
        field_path="metadata",
    )


def test_a_fullwidth_token_prefix_does_not_make_a_digest_standalone(
    tmp_path: Path,
) -> None:
    """The sibling bypass: a fullwidth ``ｓｋ－`` folds to ``sk-`` under
    NFKC, so the digest the raw-text boundary judged standalone is the tail
    of a constructed credential on the screened rendering."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    value = f"ｓｋ－{_ATTESTED_DIGEST}"
    metadata = json.dumps(
        {"key": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert _rule_identities_of(SecretScreen().screen("m", value)) == {
        f"{POLICY_VERSION}/provider-token"
    }

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION,
        rule=f"{POLICY_VERSION}/provider-token",
        field_path="metadata",
    )


def test_the_seam_verdict_matches_the_screen_across_a_composition_boundary(
    tmp_path: Path,
) -> None:
    """Closure-review finding over ``c9ccda2``: stripping U+200B exposes a
    composition boundary (A + U+030A composes to Å), so a fold that
    normalises before stripping is not its own fixed point and the seam's
    pre-folded copy was screened over a rendering the policy never judged.
    On the stable fold there is one rendering: the direct screen and the
    admission verdict must name the same finding."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    value = "client_secretA\u200b\u030a: 0011223344556677"
    metadata = json.dumps(
        {"note": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert _rule_identities_of(SecretScreen().screen("metadata", metadata)) == {
        f"{POLICY_VERSION}/contextual-entropy"
    }

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_ATTESTED_BODY),), metadata=metadata),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION,
        rule=f"{POLICY_VERSION}/contextual-entropy",
        field_path="metadata",
    )


def test_only_standalone_digest_occurrences_are_masked() -> None:
    """The boundary at the generator: an occurrence flanked by a
    token-charset character is part of a larger value and is not the
    attested digest; a quote- or whitespace-delimited occurrence is."""
    payload = json.dumps(
        {
            "content": _ATTESTED_BODY,
            "content_sha256": _ATTESTED_DIGEST,
            "joined": f"deadbeef{_ATTESTED_DIGEST}",
            "prefixed": f"sk-{_ATTESTED_DIGEST}",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    command = _command(
        facts=(_draft(_ATTESTED_BODY),),
        evidence_payload=payload,
    )

    fields = dict(mutations._ingest_screened_fields(command))

    screened_payload = fields["evidence_payload"]
    assert f"deadbeef{_ATTESTED_DIGEST}" in screened_payload
    assert f"sk-{_ATTESTED_DIGEST}" in screened_payload
    assert f'"content_sha256":"{_ATTESTED_DIGEST}"' not in screened_payload


def test_the_seam_masks_the_screening_copies_and_nothing_else() -> None:
    """The masking map, pinned at the generator: the canonical metadata
    string, each metadata member value and the decoded evidence payload are
    masked; fact bodies and member keys are not."""
    quoting_body = f"the marker was {_ATTESTED_DIGEST}"
    metadata = json.dumps(
        {_ATTESTED_DIGEST: "ci", "content_sha256": _ATTESTED_DIGEST},
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = json.dumps(
        {"content": _ATTESTED_BODY, "content_sha256": _ATTESTED_DIGEST},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    command = _command(
        facts=(_draft(_ATTESTED_BODY), _draft(quoting_body)),
        metadata=metadata,
        evidence_payload=payload,
    )

    fields = dict(mutations._ingest_screened_fields(command))

    assert _ATTESTED_DIGEST in fields["facts[1].body"]
    assert _ATTESTED_DIGEST not in fields["metadata"]
    assert fields["metadata[0].key"] == _ATTESTED_DIGEST
    assert _ATTESTED_DIGEST not in fields["metadata[1].value"]
    assert _ATTESTED_DIGEST not in fields["evidence_payload"]


def _rule_identities_of(findings: tuple[SecretFinding, ...]) -> set[str]:
    return {finding.rule for finding in findings}


def test_a_secret_rejected_ingest_writes_no_custody_outbox_or_idempotency_row(
    tmp_path: Path,
) -> None:
    """TRUST-06 and EVIDENCE-04 together: rejected rather than classified, and
    rejected before anything durable exists to carry it onward. The denial
    audit event is the only row the request is allowed to leave behind."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    command = _command(
        facts=(_draft(_PEM_BODY),),
        metadata=_SECRET_METADATA,
        evidence_payload=_AWS_KEY.encode(),
    )

    assert isinstance(_ingest(_authority(tmp_path), command), Rejected)

    assert _custody_rows(tmp_path) == {
        "assertions": 0,
        "facts": 0,
        "fact_invalidations": 0,
        "evidence_records": 0,
        "evidence_outbox": 0,
        "projection_outbox": 0,
        "idempotency_records": 0,
    }


def test_no_screened_secret_reaches_the_catalogue_bytes(tmp_path: Path) -> None:
    """Row counts prove nothing was inserted; this proves nothing was written.
    The denial event is appended after the rejection, and an event that
    quoted the offending field would leak through the audit chain the screen
    exists to protect."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    assert isinstance(
        _ingest(_authority(tmp_path), _command(facts=(_draft(_PEM_BODY),))), Rejected
    )

    assert b"PRIVATE KEY" not in _catalogue_bytes(tmp_path)


def test_a_secret_denial_event_names_the_rule_but_never_the_field_path(
    tmp_path: Path,
) -> None:
    """P-26 splits the disclosure deliberately: the closed ``cairn.audit/v1``
    value has no field for either, so the rule rides in ``reason_code`` and
    the field path travels only to the caller. An event carrying the field
    path would be a second, unversioned schema."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(facts=(_draft(_PEM_BODY),)))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail is not None
    event = _events(tmp_path)[-1]
    assert event["reason_code"] == "secret_pem_block"
    assert "facts[0].body" not in json.dumps(event)


def test_the_screen_names_the_first_dirty_field_in_command_order(
    tmp_path: Path,
) -> None:
    """Field order is behaviour, not incidental iteration order: the caller is
    told which field to redact first, and a reordering would silently change
    the answer for every multi-field rejection."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_PEM_BODY),), metadata=_SECRET_METADATA),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail is not None
    assert outcome.failure.detail.field_path == "facts[0].body"


def test_a_secret_in_a_later_fact_body_is_named_by_its_own_index(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)

    outcome = _ingest(
        _authority(tmp_path),
        _command(facts=(_draft(_BODY), _draft(_OTHER_BODY), _draft(_PEM_BODY))),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail is not None
    assert outcome.failure.detail.field_path == "facts[2].body"


def test_ingest_screens_only_after_value_validation(tmp_path: Path) -> None:
    """I-74 orders the screen after value validation, and the order is
    observable on a single field: metadata that is both over-length and
    secret-bearing is ``invalid_request``, not ``secret_rejected``. Screening
    first would answer a question about content before the request was known
    to be well-formed at all — and would spend the scan doing it."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    oversized = f'{{"aws_key":"{_AWS_KEY}","pad":"{"x" * 65536}"}}'

    outcome = _ingest(_authority(tmp_path), _command(metadata=oversized))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert outcome.failure.detail is None


def test_a_clean_ingest_carries_no_failure_detail(tmp_path: Path) -> None:
    """The detail is the bounded disclosure I-72 licenses on two failures
    only. Nothing else may grow one by accident."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    _seed_outsider(tmp_path)

    outcome = _ingest(_authority(tmp_path), _command(), actor=_outsider_actor())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.detail is None


class _RejectEverythingScreen(SecretScreen):
    """Stands in for a later ``cairn.secret`` version whose widened vocabulary
    now matches text it once passed: every screened field yields a finding."""

    def screen(self, field_path: str, text: str) -> tuple[SecretFinding, ...]:
        return (SecretFinding(rule=_PEM_RULE, field_path=field_path),)


def test_a_committed_ingest_replays_under_a_stricter_screen(tmp_path: Path) -> None:
    """The in-callback placement means ``mutate_idempotent`` settles a replay
    before the screen can run — the same guarantee the outer gates already
    document for their own checks. A client that committed a write and lost
    the ack must get its ``Replayed`` receipt back, not ``secret_rejected``
    from a policy that tightened in between: that answer says the write never
    happened while its rows sit in custody, and invites a redacted duplicate
    under a fresh key."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    command = _command()
    assert isinstance(_ingest(_authority(tmp_path), command), Committed)

    outcome = _ingest(_authority(tmp_path, screen=_RejectEverythingScreen()), command)

    assert isinstance(outcome, Replayed)


def test_key_reuse_with_a_secret_bearing_command_is_a_conflict(
    tmp_path: Path,
) -> None:
    """Idempotency-conflict detection precedes the screen: reusing a key with
    a different command is ``idempotency_conflict`` even when the new command
    carries a secret, and no screening verdict — with its I-72 ``detail`` —
    is handed out for a request that was never eligible to run."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    authority = _authority(tmp_path)
    assert isinstance(_ingest(authority, _command()), Committed)

    outcome = _ingest(authority, _command(facts=(_draft(_PEM_BODY),)))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.IDEMPOTENCY_CONFLICT
    assert outcome.failure.detail is None


def test_a_grant_revoked_mid_request_is_denied_before_the_screen_speaks(
    tmp_path: Path,
) -> None:
    """The third contract the in-callback placement restores, and the reason
    the screen sits *after* ``_revalidate`` rather than merely inside the
    callback. A caller authorised at the outer read and revoked before the
    transaction has no standing, so it learns nothing about the policy: not
    the rule, not the field path, and no ``secret_<rule>`` event naming its
    revoked grant on the realm chain. Screening first would answer a question
    about content for a principal already proven unauthorised — the
    disclosure the pre-gate placement made reachable."""
    _seed_catalogue(tmp_path)
    _seed_ingester(tmp_path)
    transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=_uuid_seq(0x10000000),
    )

    outcome = _ingest(
        _authority(tmp_path, transactions=transactions),
        _command(facts=(_draft(_PEM_BODY),)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert outcome.failure.detail is None
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "ingest", "deny", "ingest_grant_not_held")
    ]


def test_promote_rejects_a_secret_in_the_reason(tmp_path: Path) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(_authority(tmp_path), _promote_command(reason=_PEM_BODY))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_PEM_RULE, field_path="reason"
    )
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "promote", "deny", "secret_pem_block")
    ]


def test_promote_rejects_a_secret_in_an_external_evidence_uri(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)

    outcome = _promote(
        _authority(tmp_path),
        _promote_command(evidence=_external_reference(uri=_SECRET_URI)),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail is not None
    assert outcome.failure.detail.field_path == "evidence.external_uri"


def test_a_secret_rejected_promotion_writes_no_derived_fact_or_outbox_row(
    tmp_path: Path,
) -> None:
    _seed_promotable(tmp_path)
    before = _custody_rows(tmp_path)

    assert isinstance(
        _promote(_authority(tmp_path), _promote_command(reason=_PEM_BODY)), Rejected
    )

    assert _custody_rows(tmp_path) == before
    assert b"PRIVATE KEY" not in _catalogue_bytes(tmp_path)


def test_invalidate_rejects_a_secret_in_the_reason(tmp_path: Path) -> None:
    _seed_invalidatable(tmp_path)

    outcome = _invalidate(_authority(tmp_path), _invalidate_command(reason=_PEM_BODY))

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail == FailureDetail(
        policy=POLICY_VERSION, rule=_PEM_RULE, field_path="reason"
    )
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "invalidate", "deny", "secret_pem_block")
    ]


def test_a_secret_rejected_invalidation_writes_no_invalidation_row(
    tmp_path: Path,
) -> None:
    _seed_invalidatable(tmp_path)
    before = _custody_rows(tmp_path)

    assert isinstance(
        _invalidate(_authority(tmp_path), _invalidate_command(reason=_PEM_BODY)),
        Rejected,
    )

    assert _custody_rows(tmp_path) == before
    assert b"PRIVATE KEY" not in _catalogue_bytes(tmp_path)
