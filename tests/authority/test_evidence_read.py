"""Exact custody reads do not depend on semantic indexing or fact trust."""

import hashlib
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from test_retrieval import (
    _CORRELATION_ID,
    _EVIDENCE_PAYLOAD,
    _REALM,
    _SCOPE,
    _TASK,
    _agent_actor,
    _authority,
    _ingest_with_evidence,
    _ScriptedAttic,
    _seed_agent,
    _seed_catalogue,
)

from cairn.authority.evidence_read import EvidenceReadResult, ReadEvidence
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.transactions import FailureCode, Rejected
from cairn.evidence.adapter import (
    AtticAdapter,
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
)


class Attic(_ScriptedAttic):
    def __init__(self, result: object = FetchedPayload(_EVIDENCE_PAYLOAD)) -> None:
        super().__init__()
        self.result = result
        self.fetched: list[UUID] = []

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        self.fetched.append(evidence_id)
        if isinstance(self.result, Exception):
            raise self.result
        return cast(FetchedPayload | PayloadAbsent | PayloadCorrupt, self.result)


def real_authority(path: Path, attic: AtticAdapter) -> CairnAuthority:
    authority = _authority(path)
    authority._attic = attic
    return authority


def seed(path: Path) -> UUID:
    _seed_catalogue(path)
    _seed_agent(path)
    return _ingest_with_evidence(path, bodies=("source claim",))[1]


def read(
    path: Path, evidence_id: UUID, attic: Attic, scope: Scope = _SCOPE
) -> EvidenceReadResult | Rejected:
    return _authority(path, attic=attic).read_evidence(
        _agent_actor(), ReadEvidence(scope, evidence_id), correlation_id=_CORRELATION_ID
    )


def test_exact_custody_read_with_no_index_and_pending_outbox(tmp_path: Path) -> None:
    evidence_id = seed(tmp_path)
    result = read(tmp_path, evidence_id, Attic())
    assert isinstance(result, EvidenceReadResult)
    assert result.payload.encode("utf-8") == _EVIDENCE_PAYLOAD
    assert result.sha256 == hashlib.sha256(_EVIDENCE_PAYLOAD).hexdigest()
    assert result.byte_length == len(_EVIDENCE_PAYLOAD)
    assert result.media_type == "text/plain; charset=utf-8"


def test_inherited_ancestor_read(tmp_path: Path) -> None:
    evidence_id = seed(tmp_path)
    assert isinstance(
        read(tmp_path, evidence_id, Attic(), Scope(_REALM, _SCOPE.segments + (_TASK,))),
        EvidenceReadResult,
    )


@pytest.mark.parametrize(
    "result,code",
    [
        (PayloadAbsent(), "evidence_pending"),
        (PayloadCorrupt(), "evidence_corrupt"),
        (FetchedPayload(b"wrong"), "evidence_corrupt"),
        (FetchedPayload(cast(bytes, "not bytes")), "evidence_corrupt"),
        (None, "evidence_corrupt"),
        (RuntimeError("private exception"), "dependency_unavailable"),
    ],
)
def test_payload_failures(tmp_path: Path, result: object, code: str) -> None:
    evidence_id = seed(tmp_path)
    outcome = read(tmp_path, evidence_id, Attic(result))
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code.value == code


def test_unknown_does_not_fetch(tmp_path: Path) -> None:
    seed(tmp_path)
    attic = Attic()
    outcome = read(tmp_path, UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"), attic)
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    assert not attic.fetched


def edit(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    from cairn.catalogue.sqlite import _open_write_connection

    with _open_write_connection(path, create=False) as connection:
        # Deliberately construct hostile/corrupt catalogue fixtures.
        connection.execute("DROP TRIGGER IF EXISTS trg_grants_no_update")
        connection.execute("DROP TRIGGER IF EXISTS trg_evidence_records_no_update")
        connection.execute(sql, parameters)
        connection.commit()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE grants SET expires_at = '2026-08-01T00:00:00.000000Z'",
        "UPDATE grants SET operations = '[\"ingest\"]'",
    ],
)
def test_live_retrieve_grant_required_before_fetch(tmp_path: Path, change: str) -> None:
    evidence_id = seed(tmp_path)
    edit(tmp_path, change)
    attic = Attic()
    outcome = read(tmp_path, evidence_id, attic)
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.AUTHORISATION_DENIED
    assert not attic.fetched


@pytest.mark.parametrize(
    "change",
    [
        'UPDATE evidence_records SET scope_segments = \'[{"id":"job-2","kind":"job"}]\'',
        'UPDATE evidence_records SET scope_segments = \'[{"id":"job-1","kind":"job"},{"id":"task-1","kind":"task"}]\'',
        "UPDATE grants SET read_clearance = 'public'",
    ],
)
def test_hidden_evidence_is_not_found_without_fetch(
    tmp_path: Path, change: str
) -> None:
    evidence_id = seed(tmp_path)
    edit(tmp_path, change)
    attic = Attic()
    outcome = read(tmp_path, evidence_id, attic)
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code is FailureCode.NOT_FOUND
    assert not attic.fetched


def test_missing_without_outbox_is_unavailable(tmp_path: Path) -> None:
    evidence_id = seed(tmp_path)
    edit(tmp_path, "DELETE FROM evidence_outbox")
    result = read(tmp_path, evidence_id, Attic(PayloadAbsent()))
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.DEPENDENCY_UNAVAILABLE


def test_catalogue_length_is_verified(tmp_path: Path) -> None:
    evidence_id = seed(tmp_path)
    edit(tmp_path, "UPDATE evidence_records SET payload_length = payload_length + 1")
    result = read(tmp_path, evidence_id, Attic())
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "evidence_corrupt"


@pytest.mark.parametrize("payload", [b"\xff"])
def test_invalid_text_is_corrupt_even_if_digest_matches(
    tmp_path: Path, payload: bytes
) -> None:
    evidence_id = seed(tmp_path)
    edit(
        tmp_path,
        "UPDATE evidence_records SET payload_digest = ?, payload_length = ?",
        (hashlib.sha256(payload).digest(), len(payload)),
    )
    result = read(tmp_path, evidence_id, Attic(FetchedPayload(payload)))
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "evidence_corrupt"


def test_reopened_real_attic_preserves_exact_utf8(tmp_path: Path) -> None:
    from cairn.evidence.attic import SqliteAttic

    evidence_id = seed(tmp_path)
    SqliteAttic(tmp_path).store(evidence_id, _EVIDENCE_PAYLOAD)
    result = real_authority(tmp_path, SqliteAttic(tmp_path)).read_evidence(
        _agent_actor(),
        ReadEvidence(_SCOPE, evidence_id),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, EvidenceReadResult)
    assert result.payload.encode() == _EVIDENCE_PAYLOAD


def test_revoked_grant_refuses_read(tmp_path: Path) -> None:
    from test_retrieval import _DATA_GRANT_ID, _TS

    evidence_id = seed(tmp_path)
    edit(
        tmp_path,
        "INSERT INTO grant_revocations VALUES (?, ?, NULL, 'test_revocation')",
        (str(_DATA_GRANT_ID), _TS),
    )
    attic = Attic()
    result = read(tmp_path, evidence_id, attic)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED
    assert not attic.fetched


def test_disabled_is_checked_after_grant(tmp_path: Path) -> None:
    evidence_id = seed(tmp_path)
    authority = _authority(tmp_path, attic=Attic())
    authority._exact_evidence_enabled = False
    result = authority.read_evidence(
        _agent_actor(),
        ReadEvidence(_SCOPE, evidence_id),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    edit(tmp_path, "UPDATE grants SET operations = '[\"ingest\"]'")
    result = authority.read_evidence(
        _agent_actor(),
        ReadEvidence(_SCOPE, evidence_id),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED


def test_audit_failure_prevents_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.catalogue.transactions import CatalogueTransactions

    evidence_id = seed(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(CatalogueTransactions, "append_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        read(tmp_path, evidence_id, Attic())


def test_audit_contains_identity_without_payload(tmp_path: Path) -> None:
    from test_retrieval import _last_realm_event

    evidence_id = seed(tmp_path)
    result = read(tmp_path, evidence_id, Attic())
    assert isinstance(result, EvidenceReadResult)
    event = _last_realm_event(tmp_path)
    assert event.draft.action_code == "read-evidence"
    assert event.draft.affected_evidence_ids == (evidence_id,)


@pytest.mark.parametrize(
    "payload", ["café\r\nline\n e\u0301".encode(), b"known\x00payload"]
)
def test_candidate_source_roundtrip(tmp_path: Path, payload: bytes) -> None:
    from test_retrieval import _next_key

    from cairn.authority.custody import FactDraft, SourceType
    from cairn.authority.mutations import IngestAssertion
    from cairn.catalogue.transactions import Committed
    from cairn.evidence.attic import SqliteAttic

    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    receipt = _authority(tmp_path).ingest(
        _agent_actor(),
        IngestAssertion(
            scope=_SCOPE,
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=(
                FactDraft(body="candidate fixture", valid_from=None, valid_to=None),
            ),
            evidence_payload=payload,
        ),
        idempotency_key=_next_key(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(receipt, Committed)
    evidence_id = receipt.value.evidence_id
    assert evidence_id is not None
    SqliteAttic(tmp_path).store(evidence_id, payload)
    result = real_authority(tmp_path, SqliteAttic(tmp_path)).read_evidence(
        _agent_actor(),
        ReadEvidence(_SCOPE, evidence_id),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, EvidenceReadResult)
    assert result.payload.encode() == payload
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.byte_length == len(payload)


@pytest.mark.parametrize("external", [False, True])
def test_cross_realm_and_external_reference_never_fetch(
    tmp_path: Path, external: bool
) -> None:
    from test_retrieval import _add_realm

    evidence_id = seed(tmp_path)
    if external:
        edit(
            tmp_path,
            "UPDATE evidence_records SET assertion_id = NULL, payload_length = NULL, external_uri = 'https://example.invalid/evidence'",
        )
    else:
        _add_realm(tmp_path, "other")
        edit(tmp_path, "UPDATE evidence_records SET realm_id = 'other'")
    attic = Attic()
    result = read(tmp_path, evidence_id, attic)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.NOT_FOUND
    assert not attic.fetched
