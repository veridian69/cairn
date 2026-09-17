"""Module-level proof of the I-77 ``Retrieve`` command (tasks 5 and 6).

Everything here drives the pipeline through ``CairnAuthority.retrieve``
with a scripted index adapter: the adapter is told exactly which candidate
identities to return, so every test states which facts the index *claims*
and asserts what the catalogue actually discloses — the I-79 filters, the
I-82 ordering and budget (including their hypothesis property proofs), the
I-83 ``index_pending`` gate and the P-47 ``stale_index`` signal. The
mutation helpers clear the projection outbox afterwards, standing in for a
drained deliverer, so their facts read as indexed; the I-83 tests pass
``deliver=False`` to leave genuine lag behind. The hostile-adapter matrix
through the composed REST application is conformance's (P-46), not this
file's.
"""

import hashlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import (
    FactDraft,
    IngestedProvenance,
    PromotedProvenance,
    SourceType,
)
from cairn.authority.gate import Actor
from cairn.authority.mutations import (
    CairnAuthority,
    ExternalEvidenceReference,
    IngestAssertion,
    InvalidateFacts,
    PromoteFacts,
)
from cairn.authority.retrieval import (
    MAX_BUDGET_BYTES,
    MAX_QUERY_BYTES,
    RetrievalResult,
    Retrieve,
    RetrievedFact,
    _admitted,
    _assemble,
)
from cairn.catalogue.audit import (
    AuditEvent,
    ChainKind,
    Classification,
    Scope,
    ScopeSegment,
    TrustClass,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
)
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    FailureCode,
    Rejected,
    RetryClass,
)
from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.evidence.attic import AtticStorageError
from cairn.operations.metrics import Metrics
from cairn.projection.adapter import (
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.partition import canonical_partition, canonical_segments_json
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import SafeLogger, configure_logging
from cairn.screening import ALL_RULES, POLICY_VERSION, SecretScreen, audit_reason_code

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_EXPIRED_TS = "2026-08-01T00:00:00.000000Z"
_FUTURE_TS = "2027-01-01T00:00:00.000000Z"
_REALM = "acme"
_OTHER_REALM = "beta"

_JOB = ScopeSegment(kind="job", identifier="job-1")
_SIBLING_JOB = ScopeSegment(kind="job", identifier="job-2")
_TASK = ScopeSegment(kind="task", identifier="task-1")
_SCOPE = Scope(_REALM, (_JOB,))

_AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_AGENT_CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
_OUTSIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OUTSIDER_CREDENTIAL_ID = UUID("bbbbbbbb-cccc-4bbb-8bbb-bbbbbbbbbbbb")
_DATA_GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
_SECOND_GRANT_ID = UUID("eeeeeeee-2222-4eee-8eee-eeeeeeeeeeee")

_CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")

_ALL_CLASSIFICATIONS = frozenset(
    {Classification.PUBLIC, Classification.INTERNAL, Classification.RESTRICTED}
)
_DATA_OPERATIONS = frozenset(
    {
        GrantOperation.RETRIEVE,
        GrantOperation.INGEST,
        GrantOperation.PROMOTE,
        GrantOperation.INVALIDATE,
    }
)

_PEM_QUERY = "-----BEGIN RSA PRIVATE KEY-----"

# An identity the catalogue has never seen, for lagging-candidate cases.
_UNKNOWN_FACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_UNKNOWN_EVIDENCE_ID = UUID("dddddddd-eeee-4ddd-8ddd-dddddddddddd")

# The one payload every seeded assertion carries, so a scripted Attic can
# answer any fetch with bytes whose digest matches the catalogue's record.
_EVIDENCE_PAYLOAD = b"deterministic test evidence"


# --- harness -----------------------------------------------------------------


class _ScriptedIndex:
    """Returns exactly the candidates a test scripts, and proves the serving
    path never projects or clears."""

    def __init__(self, results: tuple[UUID, ...] = ()) -> None:
        self.results = results
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        raise AssertionError("the serving path must never project")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        self.calls.append((query, limit, partition_keys))
        return self.results

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        raise AssertionError("the serving path must never clear")


class _ScriptedAttic:
    """Returns exactly the evidence identities a test scripts, and answers
    every fetch with the one payload the seeded assertions carry — so the
    digest check passes and the scope, clearance and mapping rules are what
    the test is actually exercising. ``store`` is never reached: retrieval
    holds Attic read-only."""

    def __init__(
        self,
        results: tuple[UUID, ...] = (),
        *,
        payload: bytes = _EVIDENCE_PAYLOAD,
        search_raises: bool = False,
    ) -> None:
        self.results = results
        self.payload = payload
        self.search_raises = search_raises
        self.calls: list[tuple[str, int]] = []

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise AssertionError("retrieval must never write to Attic")

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        return FetchedPayload(payload=self.payload)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        self.calls.append((query, limit))
        if self.search_raises:
            raise AtticStorageError("attic_search_failed")
        return self.results


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


_KEY_COUNTER = itertools.count(1)


def _next_key() -> UUID:
    return UUID(f"{next(_KEY_COUNTER):08x}-9999-4999-8999-999999999999")


# Each authority takes its own disjoint window of deterministic identities:
# the command factory starts at the window base and the transactions factory
# halfway up, so no two authorities in one test can mint the same event_id.
_UUID_SEEDS = itertools.count(0x20000000, 0x00100000)


def _authority(
    data_path: Path,
    *,
    now: datetime = _NOW,
    index: _ScriptedIndex | None = None,
    attic: _ScriptedAttic | None = None,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> CairnAuthority:
    uuid_seed = next(_UUID_SEEDS)
    return CairnAuthority(
        data_path,
        CatalogueTransactions(
            data_path,
            writer_gate=threading.Lock(),
            clock=lambda: now,
            uuid_factory=_uuid_seq(uuid_seed + 0x00080000),
        ),
        clock=lambda: now,
        uuid_factory=_uuid_seq(uuid_seed),
        exact_evidence_enabled=True,
        screen=SecretScreen(),
        index=index,
        attic=attic,
        metrics=metrics,
        logger=logger,
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
    label: str,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(principal_id), PrincipalKind.WORKLOAD.value, label, _TS),
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
    grant_id: UUID = _DATA_GRANT_ID,
    principal_id: UUID = _AGENT_ID,
    realm_id: str = _REALM,
    segments: tuple[ScopeSegment, ...] = (_JOB,),
    operations: frozenset[GrantOperation] = _DATA_OPERATIONS,
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


def _seed_agent(
    data_path: Path,
    *,
    segments: tuple[ScopeSegment, ...] = (_JOB,),
    read_clearance: Classification = Classification.RESTRICTED,
    expires_at: str | None = _FUTURE_TS,
) -> None:
    """A workload principal holding one live grant carrying all four data
    operations, ``retrieve`` included."""
    _insert_principal(data_path, _AGENT_ID, label="agent")
    _insert_credential(data_path, _AGENT_CREDENTIAL_ID, _AGENT_ID)
    _insert_grant(
        data_path,
        segments=segments,
        read_clearance=read_clearance,
        expires_at=expires_at,
    )


def _seed_outsider(data_path: Path) -> None:
    _insert_principal(data_path, _OUTSIDER_ID, label="outsider")
    _insert_credential(data_path, _OUTSIDER_CREDENTIAL_ID, _OUTSIDER_ID)


def _agent_actor() -> Actor:
    return Actor(principal_id=_AGENT_ID, credential_id=_AGENT_CREDENTIAL_ID)


def _outsider_actor() -> Actor:
    return Actor(principal_id=_OUTSIDER_ID, credential_id=_OUTSIDER_CREDENTIAL_ID)


def _rows(data_path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
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


def _last_realm_event(data_path: Path) -> AuditEvent:
    rows = _rows(
        data_path,
        "SELECT canonical_event FROM audit_events "
        "WHERE chain_kind = 'realm' ORDER BY sequence",
    )
    return parse_canonical_audit_bytes(cast(bytes, rows[-1][0]))


def _mark_projection_delivered(data_path: Path) -> None:
    """What a drained deliverer leaves behind: no undelivered rows. The
    mutation helpers below end with this so their facts read as indexed —
    the I-83 tests that need genuine lag skip the helpers' cleanup."""
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projection_outbox")
        connection.commit()


def _ingest_facts(
    data_path: Path,
    *,
    bodies: tuple[str, ...],
    now: datetime = _NOW,
    scope: Scope = _SCOPE,
    classification: Classification = Classification.INTERNAL,
    requested_trust: TrustClass = TrustClass.VALIDATED,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    deliver: bool = True,
) -> tuple[UUID, ...]:
    """Facts written through the real ingest path, returned in the receipt's
    sorted order. ``validated`` by default — the retrieval default the tests
    mostly exercise — which requires the evidence payload the shared grant's
    ``promote`` operation makes lawful."""
    command = IngestAssertion(
        scope=scope,
        classification=classification,
        source_type=SourceType.AGENT_CLAIM,
        facts=tuple(
            FactDraft(body=body, valid_from=valid_from, valid_to=valid_to)
            for body in bodies
        ),
        requested_trust=requested_trust,
        evidence_payload=(
            _EVIDENCE_PAYLOAD if requested_trust is TrustClass.VALIDATED else None
        ),
    )
    outcome = _authority(data_path, now=now).ingest(
        _agent_actor(),
        command,
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    if deliver:
        _mark_projection_delivered(data_path)
    return outcome.value.fact_ids


def _ingest_with_evidence(
    data_path: Path,
    *,
    bodies: tuple[str, ...],
    scope: Scope = _SCOPE,
    classification: Classification = Classification.INTERNAL,
    now: datetime = _NOW,
) -> tuple[tuple[UUID, ...], UUID]:
    """As ``_ingest_facts``, but also returns the exact-evidence identity
    the assertion created — the identity a P-43 Attic candidate names."""
    command = IngestAssertion(
        scope=scope,
        classification=classification,
        source_type=SourceType.AGENT_CLAIM,
        facts=tuple(
            FactDraft(body=body, valid_from=None, valid_to=None) for body in bodies
        ),
        requested_trust=TrustClass.VALIDATED,
        evidence_payload=_EVIDENCE_PAYLOAD,
    )
    outcome = _authority(data_path, now=now).ingest(
        _agent_actor(),
        command,
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    _mark_projection_delivered(data_path)
    assert outcome.value.evidence_id is not None
    return outcome.value.fact_ids, outcome.value.evidence_id


def _invalidate_fact(
    data_path: Path, fact_id: UUID, *, now: datetime, deliver: bool = True
) -> None:
    outcome = _authority(data_path, now=now).invalidate(
        _agent_actor(),
        InvalidateFacts(
            fact_ids=(fact_id,), reason="superseded in test", superseded_by=None
        ),
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    if deliver:
        _mark_projection_delivered(data_path)


def _retrieve(
    data_path: Path,
    command: Retrieve,
    *,
    index: _ScriptedIndex | None,
    now: datetime = _NOW,
    actor: Actor | None = None,
    attic: _ScriptedAttic | None = None,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> RetrievalResult | Rejected:
    return _authority(
        data_path, now=now, index=index, attic=attic, metrics=metrics, logger=logger
    ).retrieve(actor or _agent_actor(), command, correlation_id=_CORRELATION_ID)


def _command(
    *,
    scope: Scope = _SCOPE,
    query: str = "how did the build go",
    budget: int = 4096,
    trust_filters: frozenset[TrustClass] = frozenset(),
    as_of: datetime | None = None,
) -> Retrieve:
    return Retrieve(
        scope=scope,
        query=query,
        budget=budget,
        trust_filters=trust_filters,
        as_of=as_of,
    )


# === Authorisation and shape =================================================


def test_retrieve_denied_without_a_retrieve_grant(tmp_path: Path) -> None:
    """The instance chain, by the data-plane rule: an actor with no standing
    may not read — or meter — a realm chain through its own refusal."""
    _seed_catalogue(tmp_path)
    _seed_outsider(tmp_path)
    index = _ScriptedIndex()

    outcome = _retrieve(tmp_path, _command(), index=index)

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert outcome.audit_receipt.chain_kind is ChainKind.INSTANCE
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "retrieve", "deny", "retrieve_grant_not_held")
    ]
    assert index.calls == []


def test_retrieve_denied_for_an_expired_grant(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, expires_at=_EXPIRED_TS)

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_a_grant_on_an_ancestor_scope_authorises_a_descendant_request(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())

    outcome = _retrieve(
        tmp_path,
        _command(scope=Scope(_REALM, (_JOB, _TASK))),
        index=_ScriptedIndex(),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_a_grant_on_a_sibling_scope_does_not_authorise(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=(_SIBLING_JOB,))

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED


def test_retrieve_in_an_unknown_realm_is_not_found(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)

    outcome = _retrieve(
        tmp_path, _command(scope=Scope("ghost", (_JOB,))), index=_ScriptedIndex()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "retrieve", "deny", "realm_not_found")
    ]


def test_a_malformed_scope_is_settled_before_anything_else(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    scope = Scope(_REALM, (_JOB,))
    object.__setattr__(scope, "segments", (_JOB,) * 17)

    outcome = _retrieve(tmp_path, _command(scope=scope), index=_ScriptedIndex())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("instance", "data", "retrieve", "deny", "invalid_scope")
    ]


# === The disabled posture (P-48) =============================================


def test_retrieve_without_an_index_is_retrieval_disabled(tmp_path: Path) -> None:
    """``invalid_request``, never a 503: no retry will ever help, which is
    exactly what the after-delay class would falsely promise."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)

    outcome = _retrieve(tmp_path, _command(), index=None)

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "retrieve", "deny", "retrieval_disabled")
    ]


# === Value and limit validation ==============================================


def test_query_bounds_are_enforced_in_bytes(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    index = _ScriptedIndex()

    empty = _retrieve(tmp_path, _command(query=""), index=index)
    assert isinstance(empty, Rejected)
    assert empty.failure.code is FailureCode.INVALID_REQUEST

    # 4097 two-byte characters: 4097 code points, 8194 bytes — the bound is
    # bytes after encoding (I-30), not characters.
    oversize = _retrieve(tmp_path, _command(query="é" * 4097), index=index)
    assert isinstance(oversize, Rejected)
    assert oversize.failure.code is FailureCode.INVALID_REQUEST

    at_bound = _retrieve(tmp_path, _command(query="x" * MAX_QUERY_BYTES), index=index)
    assert isinstance(at_bound, RetrievalResult)

    assert [row[4] for row in _audit_rows(tmp_path) if row[3] == "deny"] == [
        "invalid_query",
        "query_too_large",
    ]
    assert len(index.calls) == 1


def test_budget_bounds_are_enforced(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    index = _ScriptedIndex()

    for budget in (0, -1, MAX_BUDGET_BYTES + 1):
        outcome = _retrieve(tmp_path, _command(budget=budget), index=index)
        assert isinstance(outcome, Rejected)
        assert outcome.failure.code is FailureCode.INVALID_REQUEST

    assert index.calls == []
    assert {row[4] for row in _audit_rows(tmp_path)} == {"invalid_budget"}


def test_a_boolean_budget_is_refused(tmp_path: Path) -> None:
    """``True`` is an ``int`` to the interpreter and must not be one here."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)

    outcome = _retrieve(tmp_path, _command(budget=True), index=_ScriptedIndex())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST


def test_a_non_trust_class_filter_member_is_refused(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    filters = cast(frozenset[TrustClass], frozenset({"validated"}))

    outcome = _retrieve(
        tmp_path, _command(trust_filters=filters), index=_ScriptedIndex()
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "retrieve", "deny", "invalid_trust_filter")
    ]


def test_a_naive_as_of_is_refused(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)

    outcome = _retrieve(
        tmp_path,
        _command(as_of=datetime(2026, 8, 5, 10, 0, 0)),  # noqa: DTZ001 — the point
        index=_ScriptedIndex(),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INVALID_REQUEST
    assert _audit_rows(tmp_path) == [
        ("realm", "data", "retrieve", "deny", "invalid_timestamp")
    ]


# === Query screening (I-31) ==================================================


def test_a_secret_bearing_query_is_rejected_before_the_index_is_consulted(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    index = _ScriptedIndex()

    outcome = _retrieve(tmp_path, _command(query=_PEM_QUERY), index=index)

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.SECRET_REJECTED
    assert outcome.failure.detail is not None
    assert outcome.failure.detail.policy == POLICY_VERSION
    assert outcome.failure.detail.rule in ALL_RULES
    assert outcome.failure.detail.field_path == "query"
    assert index.calls == []
    assert _audit_rows(tmp_path) == [
        (
            "realm",
            "data",
            "retrieve",
            "deny",
            audit_reason_code(outcome.failure.detail.rule),
        )
    ]


def test_no_query_text_reaches_the_audit_chain(tmp_path: Path) -> None:
    """The query is screened caller content (I-31): the allow event records
    scope, grant, disclosed identities and the filter fingerprint — never
    the query, on any outcome."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    needle = "zebra-needle-of-unusual-distinction"

    outcome = _retrieve(tmp_path, _command(query=needle), index=_ScriptedIndex())

    assert isinstance(outcome, RetrievalResult)
    for row in _rows(tmp_path, "SELECT canonical_event FROM audit_events"):
        assert needle.encode() not in cast(bytes, row[0])


# === Fan-out =================================================================


def test_the_index_is_asked_for_the_ancestry_chain_root_through_self(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    index = _ScriptedIndex()
    scope = Scope(_REALM, (_JOB, _TASK))

    outcome = _retrieve(
        tmp_path, _command(scope=scope, query="anything", budget=512), index=index
    )

    assert isinstance(outcome, RetrievalResult)
    assert index.calls == [
        (
            "anything",
            512,
            (
                canonical_partition(_REALM, canonical_segments_json(())),
                canonical_partition(_REALM, canonical_segments_json((_JOB,))),
                canonical_partition(_REALM, canonical_segments_json((_JOB, _TASK))),
            ),
        )
    ]


# === The catalogue read path =================================================


def test_a_reconciled_candidate_is_disclosed_with_its_stored_fields(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("the deploy pipeline is green",))

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex((fact_id,)), now=_NOW
    )

    assert isinstance(outcome, RetrievalResult)
    assert len(outcome.hits) == 1
    hit = outcome.hits[0]
    assert hit.fact_id == fact_id
    assert hit.body == "the deploy pipeline is green"
    assert hit.scope == _SCOPE
    assert hit.classification is Classification.INTERNAL
    assert hit.trust is TrustClass.VALIDATED
    assert isinstance(hit.provenance, IngestedProvenance)
    assert hit.valid_from is None
    assert hit.valid_to is None
    assert hit.recorded_at == _NOW
    assert hit.invalidated_at is None
    assert outcome.budget_consumed == len(hit.body.encode("utf-8"))
    assert outcome.budget_exhausted is False


def test_a_promoted_fact_disclosed_with_its_promotion_provenance(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (source_id,) = _ingest_facts(
        tmp_path, bodies=("observed once",), requested_trust=TrustClass.CANDIDATE
    )
    promote = _authority(tmp_path).promote(
        _agent_actor(),
        PromoteFacts(
            fact_ids=(source_id,),
            evidence=ExternalEvidenceReference(
                external_uri="https://ci.example.test/run/1",
                payload_digest=bytes(32),
            ),
            target_scope=None,
            target_classification=None,
            reason="verified against the run log",
        ),
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(promote, Committed)
    _mark_projection_delivered(tmp_path)
    ((_, derived_id),) = promote.value.promotions

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex((derived_id,)))

    assert isinstance(outcome, RetrievalResult)
    assert len(outcome.hits) == 1
    provenance = outcome.hits[0].provenance
    assert isinstance(provenance, PromotedProvenance)
    assert provenance.derived_from == source_id
    assert provenance.promoted_by == _AGENT_ID
    assert provenance.evidence_id == promote.value.evidence_id


def test_an_unknown_candidate_is_discarded_without_a_public_failure(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("known",))

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex((_UNKNOWN_FACT_ID, fact_id))
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [fact_id]


def test_a_candidate_whose_stored_row_the_value_layer_refuses_is_discarded(
    tmp_path: Path,
) -> None:
    """Migration 0003 pins ``scope_segments`` to minified JSON arrays but
    cannot express that each element is well-formed, so ``[{"id":1,"kind":7}]``
    is a row SQLite accepts and the value layer refuses. Such a candidate
    cannot be reconciled and is discarded without a public failure; offline
    verification (I-48), not retrieval, owns corruption reporting."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (good_fact,) = _ingest_facts(tmp_path, bodies=("intact fact",))
    hostile_fact = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    rows = _rows(tmp_path, "SELECT assertion_id FROM assertions")
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(hostile_fact),
                _REALM,
                '[{"id":1,"kind":7}]',
                "hostile body",
                "validated",
                "internal",
                cast(str, rows[0][0]),
                _TS,
            ),
        )
        connection.commit()

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex((hostile_fact, good_fact))
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [good_fact]


def test_sibling_and_descendant_scope_facts_are_invisible(tmp_path: Path) -> None:
    """SCOPE-01's inherited-ancestor rule, enforced against the index's own
    claims: a request at ``job-1`` sees realm-root and own-scope facts, and
    neither a sibling's nor a descendant's, whatever the adapter returns."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    (root_fact,) = _ingest_facts(
        tmp_path, bodies=("root fact",), scope=Scope(_REALM, ())
    )
    (own_fact,) = _ingest_facts(tmp_path, bodies=("own fact",))
    (sibling_fact,) = _ingest_facts(
        tmp_path, bodies=("sibling fact",), scope=Scope(_REALM, (_SIBLING_JOB,))
    )
    (descendant_fact,) = _ingest_facts(
        tmp_path, bodies=("descendant fact",), scope=Scope(_REALM, (_JOB, _TASK))
    )

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((root_fact, own_fact, sibling_fact, descendant_fact)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert {hit.fact_id for hit in outcome.hits} == {root_fact, own_fact}


def test_a_cross_realm_candidate_is_invisible(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_agent(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=_SECOND_GRANT_ID,
        realm_id=_OTHER_REALM,
        segments=(_JOB,),
    )
    (acme_fact,) = _ingest_facts(tmp_path, bodies=("acme fact",))

    outcome = _retrieve(
        tmp_path,
        _command(scope=Scope(_OTHER_REALM, (_JOB,))),
        index=_ScriptedIndex((acme_fact,)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_trust_defaults_to_validated_and_widens_only_by_explicit_filter(
    tmp_path: Path,
) -> None:
    """TRUST-01 and TRUST-02's shape at module level: candidate and
    failed-approach facts exist and are indexed, and appear only when the
    request names their trust class."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (validated,) = _ingest_facts(tmp_path, bodies=("validated fact",))
    (candidate,) = _ingest_facts(
        tmp_path, bodies=("candidate fact",), requested_trust=TrustClass.CANDIDATE
    )
    (failed,) = _ingest_facts(
        tmp_path,
        bodies=("failed approach fact",),
        requested_trust=TrustClass.FAILED_APPROACH,
    )
    index = _ScriptedIndex((validated, candidate, failed))

    default = _retrieve(tmp_path, _command(), index=index)
    assert isinstance(default, RetrievalResult)
    assert {hit.fact_id for hit in default.hits} == {validated}

    widened = _retrieve(
        tmp_path,
        _command(
            trust_filters=frozenset({TrustClass.CANDIDATE, TrustClass.FAILED_APPROACH})
        ),
        index=index,
    )
    assert isinstance(widened, RetrievalResult)
    assert {hit.fact_id for hit in widened.hits} == {candidate, failed}


def test_the_classification_ceiling_is_the_maximum_across_covering_grants(
    tmp_path: Path,
) -> None:
    """I-80: the ceiling is derived server-side from the live ``retrieve``
    grants covering the request scope — an internal-clearance grant alone
    hides a restricted fact, and a second covering grant with restricted
    clearance admits it, with no caller-supplied field anywhere."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, read_clearance=Classification.INTERNAL)
    (fact_id,) = _ingest_facts(
        tmp_path,
        bodies=("restricted fact",),
        classification=Classification.RESTRICTED,
    )
    index = _ScriptedIndex((fact_id,))

    hidden = _retrieve(tmp_path, _command(), index=index)
    assert isinstance(hidden, RetrievalResult)
    assert hidden.hits == ()

    _insert_grant(
        tmp_path,
        grant_id=_SECOND_GRANT_ID,
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.RESTRICTED,
    )
    admitted = _retrieve(tmp_path, _command(), index=index)
    assert isinstance(admitted, RetrievalResult)
    assert [hit.fact_id for hit in admitted.hits] == [fact_id]


def test_a_fact_recorded_after_as_of_is_invisible(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("late fact",), now=_NOW)

    outcome = _retrieve(
        tmp_path,
        _command(as_of=_NOW - timedelta(hours=1)),
        index=_ScriptedIndex((fact_id,)),
        now=_NOW + timedelta(hours=1),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_world_validity_is_the_half_open_window(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    valid_from = _NOW + timedelta(hours=1)
    valid_to = _NOW + timedelta(hours=2)
    (fact_id,) = _ingest_facts(
        tmp_path,
        bodies=("windowed fact",),
        now=_NOW,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    index = _ScriptedIndex((fact_id,))
    query_now = _NOW + timedelta(hours=3)

    before = _retrieve(tmp_path, _command(as_of=_NOW), index=index, now=query_now)
    inside = _retrieve(tmp_path, _command(as_of=valid_from), index=index, now=query_now)
    at_end = _retrieve(tmp_path, _command(as_of=valid_to), index=index, now=query_now)

    assert isinstance(before, RetrievalResult) and before.hits == ()
    assert isinstance(inside, RetrievalResult)
    assert [hit.fact_id for hit in inside.hits] == [fact_id]
    assert isinstance(at_end, RetrievalResult) and at_end.hits == ()


def test_point_in_time_recall_of_an_invalidated_fact(tmp_path: Path) -> None:
    """I-81: invisible at and after its invalidation — including at the
    default query-time ``as_of`` — and visible to an earlier ``as_of``.

    The earlier read discloses no ``invalidated_at``: P-42 admits the field
    only where the invalidation is visible at ``as_of``, and this one was
    recorded an hour after the instant the caller asked about. History
    reads as history, which means it does not read as the present.
    """
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    invalidated_at = _NOW + timedelta(hours=1)
    query_now = _NOW + timedelta(hours=2)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("belief since ended",), now=_NOW)
    _invalidate_fact(tmp_path, fact_id, now=invalidated_at)
    index = _ScriptedIndex((fact_id,))

    at_query_time = _retrieve(tmp_path, _command(), index=index, now=query_now)
    at_invalidation = _retrieve(
        tmp_path, _command(as_of=invalidated_at), index=index, now=query_now
    )
    before = _retrieve(tmp_path, _command(as_of=_NOW), index=index, now=query_now)

    assert isinstance(at_query_time, RetrievalResult)
    assert at_query_time.hits == ()
    assert isinstance(at_invalidation, RetrievalResult)
    assert at_invalidation.hits == ()
    assert isinstance(before, RetrievalResult)
    assert [hit.fact_id for hit in before.hits] == [fact_id]
    assert before.hits[0].invalidated_at is None


def test_a_historical_hit_never_discloses_a_later_invalidation(
    tmp_path: Path,
) -> None:
    """P-42: a point-in-time read learns nothing recorded after the instant
    it asked about.

    The catalogue holds the invalidation throughout — the same fact is
    correctly withheld from a query at or after it — so what this pins is
    the disclosure boundary rather than the filter: the row exists, the
    filter reads it, and the wire does not carry it.
    """
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    invalidated_at = _NOW + timedelta(hours=1)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("retracted later",), now=_NOW)
    _invalidate_fact(tmp_path, fact_id, now=invalidated_at)
    index = _ScriptedIndex((fact_id,))

    # Every as_of strictly before the invalidation, including the last
    # instant that still precedes it.
    for as_of in (_NOW, invalidated_at - timedelta(microseconds=1)):
        result = _retrieve(
            tmp_path,
            _command(as_of=as_of),
            index=index,
            now=invalidated_at + timedelta(hours=1),
        )

        assert isinstance(result, RetrievalResult)
        assert [hit.fact_id for hit in result.hits] == [fact_id]
        assert result.hits[0].invalidated_at is None


# === Ordering, budget and duplicates =========================================


def test_hits_are_ordered_by_recorded_at_ascending(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    times = [_NOW + timedelta(minutes=offset) for offset in (0, 1, 2)]
    facts = [
        _ingest_facts(tmp_path, bodies=(f"fact {n}",), now=times[n])[0]
        for n in range(3)
    ]

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((facts[2], facts[0], facts[1])),
        now=_NOW + timedelta(hours=1),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == facts


def test_same_instant_hits_tie_break_on_fact_identity(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    # One assertion, two facts: identical recorded_at by construction. The
    # receipt is sorted by string identity — the I-82 tiebreak order.
    fact_ids = _ingest_facts(tmp_path, bodies=("first body", "second body"))

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex(tuple(reversed(fact_ids)))
    )

    assert isinstance(outcome, RetrievalResult)
    assert tuple(hit.fact_id for hit in outcome.hits) == fact_ids


def test_budget_assembly_stops_at_the_first_fact_that_does_not_fit(
    tmp_path: Path,
) -> None:
    """I-82: whole facts in deterministic order until the *next* would not
    fit — a later, smaller fact is not smuggled past the one that stopped
    assembly, or the result would depend on body sizes rather than order."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    times = [_NOW + timedelta(minutes=offset) for offset in (0, 1, 2)]
    first = _ingest_facts(tmp_path, bodies=("a" * 10,), now=times[0])[0]
    second = _ingest_facts(tmp_path, bodies=("b" * 20,), now=times[1])[0]
    third = _ingest_facts(tmp_path, bodies=("c" * 5,), now=times[2])[0]
    index = _ScriptedIndex((first, second, third))
    query_now = _NOW + timedelta(hours=1)

    truncated = _retrieve(tmp_path, _command(budget=16), index=index, now=query_now)
    assert isinstance(truncated, RetrievalResult)
    assert [hit.fact_id for hit in truncated.hits] == [first]
    assert truncated.budget_consumed == 10
    assert truncated.budget_exhausted is True

    exact = _retrieve(tmp_path, _command(budget=35), index=index, now=query_now)
    assert isinstance(exact, RetrievalResult)
    assert [hit.fact_id for hit in exact.hits] == [first, second, third]
    assert exact.budget_consumed == 35
    assert exact.budget_exhausted is False


def test_the_budget_counts_encoded_bytes_and_never_splits_a_fact(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    # Two characters, four UTF-8 bytes: a character-counting budget would
    # disclose it under a budget of three.
    (fact_id,) = _ingest_facts(tmp_path, bodies=("éé",))

    outcome = _retrieve(tmp_path, _command(budget=3), index=_ScriptedIndex((fact_id,)))

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()
    assert outcome.budget_consumed == 0
    assert outcome.budget_exhausted is True


def test_duplicate_candidates_collapse_to_one_hit(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("once only",))

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex((fact_id, fact_id, fact_id))
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [fact_id]
    assert outcome.budget_consumed == len("once only")


# === The read's own audit event ==============================================


def test_a_successful_retrieve_appends_the_allow_event_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The read-audit-events precedent on the data plane: one durable allow
    event naming scope, grant and the disclosed identities, a fingerprint of
    the filter parameters — and no idempotency record, because a read has no
    idempotency key to record."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    fact_ids = _ingest_facts(tmp_path, bodies=("first body", "second body"))
    as_of = _NOW + timedelta(minutes=30)
    command = _command(
        trust_filters=frozenset({TrustClass.VALIDATED}), as_of=as_of, budget=4096
    )
    idempotency_rows_before = _rows(
        tmp_path, "SELECT COUNT(*) FROM idempotency_records"
    )

    outcome = _retrieve(
        tmp_path,
        command,
        index=_ScriptedIndex(tuple(reversed(fact_ids))),
        now=_NOW + timedelta(hours=1),
    )

    assert isinstance(outcome, RetrievalResult)
    event = _last_realm_event(tmp_path)
    assert event.draft.action_code == "retrieve"
    assert event.draft.reason_code == "retrieval_completed"
    assert event.draft.grant_id == _DATA_GRANT_ID
    assert event.draft.requested_scope == _SCOPE
    assert event.draft.affected_fact_ids == fact_ids
    assert event.draft.idempotency_key is None
    expected_fingerprint = hashlib.sha256(
        b"cairn.retrieve.request/v1\x00"
        + json.dumps(
            {
                "as_of": "2026-08-05T10:41:12.123456Z",
                "budget": 4096,
                "trust_filters": ["validated"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()
    assert event.draft.safe_request_fingerprint == expected_fingerprint
    rows = _rows(tmp_path, "SELECT COUNT(*) FROM idempotency_records")
    assert rows == idempotency_rows_before


# === index_pending (I-83) ====================================================


def test_undelivered_relevant_projection_work_is_index_pending(
    tmp_path: Path,
) -> None:
    """The one public retrieval failure in the after-delay class: the index
    is known to be behind and a retry after the deliverer catches up will
    genuinely help. The lagging index is never even asked."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    _ingest_facts(tmp_path, bodies=("not yet projected",), deliver=False)
    index = _ScriptedIndex()

    outcome = _retrieve(tmp_path, _command(), index=index)

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INDEX_PENDING
    assert outcome.failure.retry is RetryClass.AFTER_DELAY
    assert index.calls == []
    assert [row for row in _audit_rows(tmp_path) if row[2] == "retrieve"] == [
        ("realm", "data", "retrieve", "deny", "index_pending")
    ]


def test_pending_work_for_a_sibling_scope_does_not_block(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    (own_fact,) = _ingest_facts(tmp_path, bodies=("own fact",))
    _ingest_facts(
        tmp_path,
        bodies=("sibling fact",),
        scope=Scope(_REALM, (_SIBLING_JOB,)),
        deliver=False,
    )

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex((own_fact,)))

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [own_fact]


def test_pending_ancestor_scope_work_blocks_a_descendant_request(
    tmp_path: Path,
) -> None:
    """A realm-root fact is a candidate for every request in the realm
    (SCOPE-01), so undelivered root work means every descendant's recall
    has a known gap."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    _ingest_facts(
        tmp_path, bodies=("root fact",), scope=Scope(_REALM, ()), deliver=False
    )

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex())

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INDEX_PENDING


def test_pending_work_created_after_as_of_does_not_block(tmp_path: Path) -> None:
    """A row queued after the moment the caller asks about cannot make that
    moment's answer stale: I-83 bounds the check at or before ``as_of``."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (old_fact,) = _ingest_facts(tmp_path, bodies=("old fact",), now=_NOW)
    late = _NOW + timedelta(hours=1)
    _ingest_facts(tmp_path, bodies=("late fact",), now=late, deliver=False)

    outcome = _retrieve(
        tmp_path,
        _command(as_of=_NOW + timedelta(minutes=30)),
        index=_ScriptedIndex((old_fact,)),
        now=late + timedelta(minutes=1),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [old_fact]


def test_an_undelivered_invalidation_also_blocks(tmp_path: Path) -> None:
    """Both projection kinds matter: an undelivered invalidation means the
    index still serves belief the catalogue has ended."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("soon retracted",))
    _invalidate_fact(tmp_path, fact_id, now=_NOW + timedelta(minutes=5), deliver=False)

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((fact_id,)),
        now=_NOW + timedelta(minutes=10),
    )

    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.INDEX_PENDING


# === stale_index observability (P-47) ========================================


def test_an_unknown_candidate_raises_the_stale_index_signal(tmp_path: Path) -> None:
    """Operator signal, never a wire failure: the metric increments, the log
    event carries only the candidate UUID and the correlation id, and the
    response is served normally from the surviving candidates."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("intact fact",))
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((_UNKNOWN_FACT_ID, fact_id)),
        metrics=metrics,
        logger=logger,
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [fact_id]
    assert b"cairn_stale_index_total 1.0" in metrics.render()[0]
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "candidate_id": str(_UNKNOWN_FACT_ID),
        "correlation_id": str(_CORRELATION_ID),
        "event": "stale_index_candidate",
        "time": payload["time"],
    }


def test_discarded_but_present_candidates_raise_no_signal(tmp_path: Path) -> None:
    """P-47's line: lagging, invalidated, out-of-scope and trust-filtered
    candidates are ordinary I-79 discards — no metric, no log, nothing a
    caller or operator could mistake for corruption."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    (sibling_fact,) = _ingest_facts(
        tmp_path, bodies=("sibling fact",), scope=Scope(_REALM, (_SIBLING_JOB,))
    )
    (candidate_fact,) = _ingest_facts(
        tmp_path, bodies=("candidate fact",), requested_trust=TrustClass.CANDIDATE
    )
    (invalidated_fact,) = _ingest_facts(tmp_path, bodies=("ended fact",))
    _invalidate_fact(tmp_path, invalidated_fact, now=_NOW + timedelta(minutes=1))
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((sibling_fact, candidate_fact, invalidated_fact)),
        now=_NOW + timedelta(minutes=2),
        metrics=metrics,
        logger=logger,
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()
    assert b"cairn_stale_index_total 0.0" in metrics.render()[0]
    assert stream.getvalue() == ""


def test_a_value_layer_refused_row_is_not_the_stale_index_signal(
    tmp_path: Path,
) -> None:
    """Its identity exists, so what it evidences is catalogue corruption,
    which offline verification (I-48) owns — not index corruption."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    _ingest_facts(tmp_path, bodies=("anchor fact",))
    hostile_fact = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    rows = _rows(tmp_path, "SELECT assertion_id FROM assertions")
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(hostile_fact),
                _REALM,
                '[{"id":1,"kind":7}]',
                "hostile body",
                "validated",
                "internal",
                cast(str, rows[0][0]),
                _TS,
            ),
        )
        connection.commit()
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((hostile_fact,)),
        metrics=metrics,
        logger=logger,
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()
    assert b"cairn_stale_index_total 0.0" in metrics.render()[0]
    assert stream.getvalue() == ""


# === Determinism =============================================================


def test_the_result_is_independent_of_the_adapter_candidate_order(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    times = [_NOW + timedelta(minutes=offset) for offset in (0, 1, 2)]
    facts = [
        _ingest_facts(tmp_path, bodies=(f"fact number {n}",), now=times[n])[0]
        for n in range(3)
    ]
    query_now = _NOW + timedelta(hours=1)
    command = _command(budget=30)

    first = _retrieve(
        tmp_path,
        command,
        index=_ScriptedIndex((facts[2], facts[0], facts[1])),
        now=query_now,
    )
    second = _retrieve(
        tmp_path,
        command,
        index=_ScriptedIndex((facts[1], facts[2], facts[0], facts[0])),
        now=query_now,
    )

    assert isinstance(first, RetrievalResult)
    assert isinstance(second, RetrievalResult)
    assert first == second


# === Property proofs (I-79 narrowing, I-82 assembly) =========================

_PROPERTY_INSTANTS = st.datetimes(
    min_value=datetime(2024, 1, 1),
    max_value=datetime(2030, 1, 1),
    timezones=st.just(UTC),
)
_PROPERTY_SEGMENTS = st.lists(
    st.sampled_from((_JOB, _SIBLING_JOB, _TASK)), max_size=3
).map(tuple)


@st.composite
def _property_facts(draw: st.DrawFn) -> RetrievedFact:
    recorded_at = draw(_PROPERTY_INSTANTS)
    return RetrievedFact(
        fact_id=draw(st.uuids(version=4)),
        body=draw(st.text(min_size=1, max_size=24)),
        scope=Scope(_REALM, draw(_PROPERTY_SEGMENTS)),
        classification=draw(st.sampled_from(Classification)),
        trust=draw(st.sampled_from(TrustClass)),
        provenance=IngestedProvenance(assertion_id=draw(st.uuids(version=4))),
        valid_from=draw(st.none() | _PROPERTY_INSTANTS),
        valid_to=draw(st.none() | _PROPERTY_INSTANTS),
        recorded_at=recorded_at,
        invalidated_at=draw(st.none() | _PROPERTY_INSTANTS),
    )


@given(
    fact=_property_facts(),
    request_segments=_PROPERTY_SEGMENTS,
    as_of=_PROPERTY_INSTANTS,
    narrow_trust=st.frozensets(st.sampled_from(TrustClass)),
    extra_trust=st.frozensets(st.sampled_from(TrustClass)),
    narrow_ceiling=st.integers(min_value=0, max_value=2),
    ceiling_raise=st.integers(min_value=0, max_value=2),
)
def test_no_filter_ever_widens_output(
    fact: RetrievedFact,
    request_segments: tuple[ScopeSegment, ...],
    as_of: datetime,
    narrow_trust: frozenset[TrustClass],
    extra_trust: frozenset[TrustClass],
    narrow_ceiling: int,
    ceiling_raise: int,
) -> None:
    """Trust filters and the clearance ceiling only ever narrow: a fact
    admitted under the stricter configuration is admitted under every
    weaker one, so no filter combination can surface a fact that a
    stricter request would have withheld."""
    scope = Scope(_REALM, request_segments)
    wide_trust = narrow_trust | extra_trust
    wide_ceiling = min(narrow_ceiling + ceiling_raise, 2)

    if _admitted(fact, scope, narrow_ceiling, narrow_trust, as_of):
        assert _admitted(fact, scope, wide_ceiling, wide_trust, as_of)


@given(
    facts=st.lists(_property_facts(), max_size=12, unique_by=lambda fact: fact.fact_id),
    budget=st.integers(min_value=1, max_value=120),
)
def test_budget_assembly_is_deterministic_and_never_splits_a_fact(
    facts: list[RetrievedFact], budget: int
) -> None:
    """I-82 as properties: assembly is a pure function of the admitted set
    and the budget — input order is irrelevant — its hits are exactly a
    prefix of the deterministic order, every disclosed body is counted
    whole, the total never exceeds the budget, and the exhausted flag is
    exactly "something admitted was withheld for budget"."""
    hits, consumed, exhausted = _assemble(facts, budget)
    again = _assemble(list(reversed(facts)), budget)

    assert (hits, consumed, exhausted) == again
    ordered = sorted(facts, key=lambda fact: (fact.recorded_at, str(fact.fact_id)))
    assert list(hits) == ordered[: len(hits)]
    assert consumed == sum(len(hit.body.encode("utf-8")) for hit in hits)
    assert consumed <= budget
    assert exhausted == (len(hits) < len(facts))
    if exhausted:
        blocking = ordered[len(hits)]
        assert consumed + len(blocking.body.encode("utf-8")) > budget


# === The Attic retrieval modality (P-43) =====================================


def test_evidence_01_an_attic_hit_maps_to_the_facts_of_its_assertion(
    tmp_path: Path,
) -> None:
    """`EVIDENCE-01`: an authorised exact-evidence hit at the requested
    scope is returned as the facts of its assertion, with their catalogue
    provenance — and never as payload bytes, which I-77's closed hit shape
    has no field for."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    fact_ids, evidence_id = _ingest_with_evidence(
        tmp_path, bodies=("evidence-backed claim",)
    )
    attic = _ScriptedAttic((evidence_id,))

    outcome = _retrieve(tmp_path, _command(), index=_ScriptedIndex(), attic=attic)

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == list(fact_ids)
    assert outcome.hits[0].body == "evidence-backed claim"
    assert isinstance(outcome.hits[0].provenance, IngestedProvenance)
    assert attic.calls == [("how did the build go", 4096)]


def test_evidence_01_reaches_evidence_at_an_ancestor_scope(tmp_path: Path) -> None:
    """The amended P-43 direction: evidence recorded at an ancestor of the
    request scope is in reach, exactly as an ancestor-scope fact is under
    SCOPE-01. The pre-amendment (audit-read) direction would discard it."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, segments=())
    fact_ids, evidence_id = _ingest_with_evidence(
        tmp_path, bodies=("root-scope claim",), scope=Scope(_REALM, ())
    )

    outcome = _retrieve(
        tmp_path,
        _command(scope=Scope(_REALM, (_JOB, _TASK))),
        index=_ScriptedIndex(),
        attic=_ScriptedAttic((evidence_id,)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == list(fact_ids)


def test_evidence_02_sibling_descendant_and_cross_realm_hits_are_discarded(
    tmp_path: Path,
) -> None:
    """`EVIDENCE-02`, all three directions at once, with the adapter naming
    every one of them."""
    _seed_catalogue(tmp_path, realms=(_REALM, _OTHER_REALM))
    _seed_agent(tmp_path, segments=())
    _insert_grant(
        tmp_path, grant_id=_SECOND_GRANT_ID, realm_id=_OTHER_REALM, segments=()
    )
    _, sibling_evidence = _ingest_with_evidence(
        tmp_path, bodies=("sibling claim",), scope=Scope(_REALM, (_SIBLING_JOB,))
    )
    _, descendant_evidence = _ingest_with_evidence(
        tmp_path, bodies=("descendant claim",), scope=Scope(_REALM, (_JOB, _TASK))
    )
    _, cross_realm_evidence = _ingest_with_evidence(
        tmp_path, bodies=("other realm claim",), scope=Scope(_OTHER_REALM, ())
    )

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex(),
        attic=_ScriptedAttic(
            (sibling_evidence, descendant_evidence, cross_realm_evidence)
        ),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_evidence_03_unknown_and_above_clearance_hits_are_discarded(
    tmp_path: Path,
) -> None:
    """`EVIDENCE-03`: an identity the catalogue never held and a record
    above the derived ceiling are both discarded, with no public failure
    distinguishing either from an ordinary empty result."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path, read_clearance=Classification.INTERNAL)
    _, restricted_evidence = _ingest_with_evidence(
        tmp_path,
        bodies=("restricted claim",),
        classification=Classification.RESTRICTED,
    )

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex(),
        attic=_ScriptedAttic((_UNKNOWN_EVIDENCE_ID, restricted_evidence)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_an_attic_candidate_whose_payload_digest_mismatches_is_discarded(
    tmp_path: Path,
) -> None:
    """I-69's digest check still stands in front of the mapping: bytes that
    are not what the catalogue recorded disclose nothing, and raise the
    evidence digest-mismatch signal rather than a retrieval failure."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    _, evidence_id = _ingest_with_evidence(tmp_path, bodies=("claim",))
    metrics = Metrics()

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex(),
        attic=_ScriptedAttic((evidence_id,), payload=b"not the recorded bytes"),
        metrics=metrics,
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()
    assert b"cairn_evidence_digest_mismatch_total 1.0" in metrics.render()[0]


def test_attic_facts_face_the_identical_fact_filters(tmp_path: Path) -> None:
    """P-43's "each of which passes the identical I-79/I-80/I-81 fact
    filters": reaching a fact through evidence is not a way around trust or
    point-in-time visibility. The same assertion's facts are visible before
    invalidation and invisible after it, through the Attic path alone."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    fact_ids, evidence_id = _ingest_with_evidence(tmp_path, bodies=("ended claim",))
    invalidated_at = _NOW + timedelta(hours=1)
    _invalidate_fact(tmp_path, fact_ids[0], now=invalidated_at)
    attic = _ScriptedAttic((evidence_id,))
    query_now = invalidated_at + timedelta(hours=1)

    after = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex(), attic=attic, now=query_now
    )
    before = _retrieve(
        tmp_path,
        _command(as_of=_NOW),
        index=_ScriptedIndex(),
        attic=attic,
        now=query_now,
    )

    assert isinstance(after, RetrievalResult)
    assert after.hits == ()
    assert isinstance(before, RetrievalResult)
    assert [hit.fact_id for hit in before.hits] == list(fact_ids)


def test_a_fact_reached_by_both_modalities_is_disclosed_once(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    fact_ids, evidence_id = _ingest_with_evidence(tmp_path, bodies=("one claim",))

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex(fact_ids),
        attic=_ScriptedAttic((evidence_id,)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == list(fact_ids)
    assert outcome.budget_consumed == len("one claim")


def test_the_two_modalities_are_merged_before_ordering_and_budget(
    tmp_path: Path,
) -> None:
    """I-82 governs the merged set, not each modality's share of it: an
    Attic-reached fact recorded first sorts first, and the budget is spent
    in that single order."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    early_facts, early_evidence = _ingest_with_evidence(
        tmp_path, bodies=("aaaa",), now=_NOW
    )
    (late_fact,) = _ingest_facts(
        tmp_path, bodies=("bbbb",), now=_NOW + timedelta(minutes=1)
    )

    outcome = _retrieve(
        tmp_path,
        _command(budget=6),
        index=_ScriptedIndex((late_fact,)),
        attic=_ScriptedAttic((early_evidence,)),
        now=_NOW + timedelta(hours=1),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == list(early_facts)
    assert outcome.budget_exhausted is True


def test_a_failing_attic_search_narrows_recall_without_failing_the_request(
    tmp_path: Path,
) -> None:
    """An Attic outage must not fail a retrieval the index can already
    answer: the index modality's hits are served unchanged."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("index-reachable fact",))

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((fact_id,)),
        attic=_ScriptedAttic(search_raises=True),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [fact_id]


def test_a_disabled_attic_is_simply_not_consulted(tmp_path: Path) -> None:
    """P-14's absence-not-null-object rule: with exact evidence disabled
    there is no adapter, retrieval runs the index modality alone, and the
    evidence-backed facts remain reachable through the index."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    fact_ids, _evidence_id = _ingest_with_evidence(tmp_path, bodies=("a claim",))

    outcome = _retrieve(
        tmp_path, _command(), index=_ScriptedIndex(fact_ids), attic=None
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == list(fact_ids)


def test_external_custody_evidence_maps_to_no_facts(tmp_path: Path) -> None:
    """A reconciled record with no assertion — external-custody evidence a
    hostile adapter may name — maps to nothing and is dropped, rather than
    reaching for facts that do not exist."""
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (source_id,) = _ingest_facts(
        tmp_path, bodies=("observed once",), requested_trust=TrustClass.CANDIDATE
    )
    promote = _authority(tmp_path).promote(
        _agent_actor(),
        PromoteFacts(
            fact_ids=(source_id,),
            evidence=ExternalEvidenceReference(
                external_uri="https://ci.example.test/run/2",
                payload_digest=hashlib.sha256(_EVIDENCE_PAYLOAD).digest(),
            ),
            target_scope=None,
            target_classification=None,
            reason="verified against the run log",
        ),
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(promote, Committed)
    _mark_projection_delivered(tmp_path)

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex(),
        attic=_ScriptedAttic((promote.value.evidence_id,)),
    )

    assert isinstance(outcome, RetrievalResult)
    assert outcome.hits == ()


def test_an_attic_returning_nothing_adds_nothing(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    (fact_id,) = _ingest_facts(tmp_path, bodies=("index-only fact",))

    outcome = _retrieve(
        tmp_path,
        _command(),
        index=_ScriptedIndex((fact_id,)),
        attic=_ScriptedAttic(()),
    )

    assert isinstance(outcome, RetrievalResult)
    assert [hit.fact_id for hit in outcome.hits] == [fact_id]
