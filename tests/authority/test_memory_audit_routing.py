"""Memory denials become realm attributable only after live retrieve standing."""

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from test_memory import _memory, _seed
from test_retrieval import (
    _CORRELATION_ID,
    _NOW,
    _SCOPE,
    _SECOND_GRANT_ID,
    _agent_actor,
    _ingest_with_evidence,
    _insert_grant,
    _rows,
    _seed_catalogue,
)

from cairn.authority import memory
from cairn.authority.credentials import GrantOperation
from cairn.catalogue.audit import (
    ChainKind,
    Classification,
    Outcome,
    Scope,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    FailureCode,
    Rejected,
)
from cairn.screening import SecretScreen

_SECRET = "-----BEGIN RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("case", "reason", "code"),
    [
        ("recall-budget", "invalid_budget", FailureCode.INVALID_REQUEST),
        ("history-budget", "invalid_budget", FailureCode.INVALID_REQUEST),
        ("query", "invalid_query", FailureCode.INVALID_REQUEST),
        ("secret-query", "memory_secret_rejected", FailureCode.SECRET_REJECTED),
        (
            "history-missing",
            "memory_authorisation_denied",
            FailureCode.AUTHORISATION_DENIED,
        ),
        ("reason", "invalid_reason", FailureCode.INVALID_REQUEST),
        ("secret-reason", "memory_secret_rejected", FailureCode.SECRET_REJECTED),
        ("endpoints", "memory_authorisation_denied", FailureCode.AUTHORISATION_DENIED),
        ("resolution", "memory_authorisation_denied", FailureCode.AUTHORISATION_DENIED),
        ("evidence", "memory_authorisation_denied", FailureCode.AUTHORISATION_DENIED),
        ("selection", "invalid_selection", FailureCode.INVALID_REQUEST),
    ],
)
def test_post_standing_denial_enters_realm_chain(
    tmp_path: Path, case: str, reason: str, code: FailureCode
) -> None:
    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(
        tmp_path, bodies=("left memory", "right memory")
    )
    service = _memory(tmp_path)
    actor = _agent_actor()
    correlation = uuid4()
    result: object
    if case in {"recall-budget", "query", "secret-query"}:
        result = service.recall(
            actor,
            memory.Recall(
                _SCOPE,
                _SECRET
                if case == "secret-query"
                else ""
                if case == "query"
                else "memory",
                0 if case == "recall-budget" else 16384,
            ),
            correlation_id=correlation,
        )
    elif case.startswith("history"):
        result = service.history(
            actor,
            memory.History(_SCOPE, uuid4(), 0 if case == "history-budget" else 16384),
            correlation_id=correlation,
        )
    elif case in {"reason", "secret-reason", "endpoints"}:
        result = service.disagree(
            actor,
            memory.Disagree(
                _SCOPE,
                left,
                left if case == "endpoints" else right,
                Classification.INTERNAL,
                _SECRET if case == "secret-reason" else "",
            ),
            idempotency_key=uuid4(),
            correlation_id=correlation,
        )
    else:
        disagreement = service.disagree(
            actor,
            memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different"),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(disagreement, Committed)
        result = service.resolve(
            actor,
            memory.Resolve(
                _SCOPE,
                uuid4() if case == "resolution" else disagreement.value.relationship_id,
                uuid4() if case == "evidence" else evidence,
                uuid4() if case == "selection" else left,
                "resolution explanation",
            ),
            idempotency_key=uuid4(),
            correlation_id=correlation,
        )
    assert isinstance(result, Rejected)
    assert result.failure.code == code
    events = [
        parse_canonical_audit_bytes(cast(bytes, row[0])).draft
        for row in _rows(tmp_path, "SELECT canonical_event FROM audit_events")
    ]
    matching = [event for event in events if event.correlation_id == correlation]
    assert len(matching) == 1
    event = matching[0]
    assert event.chain_kind == ChainKind.REALM
    assert event.chain_identity == _SCOPE.realm
    assert event.requested_scope == _SCOPE
    assert event.principal_id == actor.principal_id
    assert event.credential_verifier_id == actor.credential_id
    assert event.outcome == Outcome.DENY
    assert event.reason_code == reason
    assert event.affected_fact_ids == ()
    assert event.affected_evidence_ids == ()
    assert _SECRET not in repr(_rows(tmp_path, "SELECT * FROM audit_events"))
    assert "resolution explanation" not in repr(events)


@pytest.mark.parametrize("scope", [_SCOPE, Scope("unknown", ())])
def test_without_standing_denial_stays_on_instance_chain(
    tmp_path: Path, scope: Scope
) -> None:
    _seed_catalogue(tmp_path)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        memory.Recall(scope, _SECRET, 0),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code == FailureCode.AUTHORISATION_DENIED
    rows = _rows(tmp_path, "SELECT canonical_event FROM audit_events")
    assert len(rows) == 1
    event = parse_canonical_audit_bytes(cast(bytes, rows[0][0])).draft
    assert event.chain_kind == ChainKind.INSTANCE
    assert event.requested_scope is None
    assert _SECRET not in repr(rows)


@pytest.mark.parametrize("keep_retrieve", [False, True])
def test_writer_lock_rechecks_standing_for_denial_routing(
    tmp_path: Path, keep_retrieve: bool
) -> None:
    _seed(tmp_path)
    (left, right), _ = _ingest_with_evidence(
        tmp_path, bodies=("left memory", "right memory")
    )
    if keep_retrieve:
        _insert_grant(
            tmp_path,
            grant_id=_SECOND_GRANT_ID,
            operations=frozenset({GrantOperation.RETRIEVE}),
            expires_at="2029-01-01T00:00:00.000000Z",
        )
    later = _NOW + timedelta(days=365)
    times = iter((_NOW, later))
    service = memory.CairnMemory(
        tmp_path,
        CatalogueTransactions(
            tmp_path,
            writer_gate=threading.Lock(),
            clock=lambda: later,
            uuid_factory=uuid4,
        ),
        lambda: next(times),
        uuid4,
        SecretScreen(),
    )
    result = service.disagree(
        _agent_actor(),
        memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different"),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
    )
    assert isinstance(result, Rejected)
    assert result.failure.code == FailureCode.AUTHORISATION_DENIED
    assert result.audit_receipt.chain_kind == (
        ChainKind.REALM if keep_retrieve else ChainKind.INSTANCE
    )
    assert _rows(tmp_path, "SELECT * FROM memory_disagreements") == []
    assert (
        _rows(
            tmp_path,
            "SELECT * FROM idempotency_records WHERE operation = 'memory-disagree'",
        )
        == []
    )


def test_denial_audit_failure_propagates_without_a_response(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = _rows(tmp_path, "SELECT * FROM audit_events")
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        _memory(tmp_path).recall(
            _agent_actor(),
            memory.Recall(_SCOPE, "memory", 0),
            correlation_id=uuid4(),
        )
    assert _rows(tmp_path, "SELECT * FROM audit_events") == before
