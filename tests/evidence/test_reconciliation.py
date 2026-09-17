import hashlib
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

from cairn.catalogue.audit import Classification, ScopeSegment
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection
from cairn.evidence.adapter import (
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.evidence.attic import SqliteAttic
from cairn.evidence.reconciliation import (
    DisclosedEvidence,
    ScopeDirection,
    reconcile_evidence,
)
from cairn.operations.metrics import Metrics
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import LogEvent, configure_logging

_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
_REALM = "acme"
_OTHER_REALM = "other"
_JOB1 = ScopeSegment(kind="job", identifier="job-1")
_JOB2 = ScopeSegment(kind="job", identifier="job-2")
_RUN1 = ScopeSegment(kind="run", identifier="run-1")
_REQUEST_SEGMENTS = (_JOB1,)
_REQUEST_CLEARANCE = Classification.INTERNAL

_REQUEST_SCOPE_ID = UUID("00000000-0000-4000-8000-000000000001")
_ANCESTOR_ID = UUID("00000000-0000-4000-8000-000000000002")
_SIBLING_ID = UUID("00000000-0000-4000-8000-000000000003")
_DESCENDANT_ID = UUID("00000000-0000-4000-8000-000000000004")
_OTHER_REALM_ID = UUID("00000000-0000-4000-8000-000000000005")
_HIGHER_CLASSIFICATION_ID = UUID("00000000-0000-4000-8000-000000000006")
_UNKNOWN_ID = UUID("00000000-0000-4000-8000-0000000000ff")


class _HostileAttic:
    """A test-local hostile adapter (I-69): delegates to a real
    ``SqliteAttic`` for legitimate reads, but returns tampered bytes for one
    designated identity — modelling a compromised or lying Attic backend.
    ``search`` is unused by ``reconcile_evidence`` and left unimplemented."""

    def __init__(self, data_path: Path, *, tampered: dict[UUID, bytes]) -> None:
        self._real = SqliteAttic(data_path)
        self._tampered = tampered

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise NotImplementedError

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        if evidence_id in self._tampered:
            return FetchedPayload(payload=self._tampered[evidence_id])
        return self._real.fetch(evidence_id)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _CorruptReportingAttic:
    """Reports every fetch as ``PayloadCorrupt`` regardless of identity —
    the adapter's own corruption signal, distinct from a computed digest
    mismatch, but per P-15/P-18 handled identically."""

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise NotImplementedError

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        return PayloadCorrupt()

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _RaisingFetchAttic:
    """Delegates to a real ``SqliteAttic`` except for one designated
    identity, whose ``fetch`` raises — modelling an Attic infrastructure
    failure (P-14: adapter infrastructure failures raise) reachable through
    the real ``SqliteAttic`` when its file is missing or corrupt."""

    def __init__(self, data_path: Path, *, raising: frozenset[UUID]) -> None:
        self._real = SqliteAttic(data_path)
        self._raising = raising

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise NotImplementedError

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        if evidence_id in self._raising:
            raise RuntimeError("attic unreachable")
        return self._real.fetch(evidence_id)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _WrongTypeAttic:
    """Delegates to a real ``SqliteAttic`` except for one designated
    identity, whose ``fetch`` returns a ``FetchedPayload`` carrying a
    non-``bytes`` payload — a hostile adapter breaking I-69's own result
    contract, not just lying about content."""

    def __init__(self, data_path: Path, *, wrong_type: frozenset[UUID]) -> None:
        self._real = SqliteAttic(data_path)
        self._wrong_type = wrong_type

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise NotImplementedError

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        if evidence_id in self._wrong_type:
            return FetchedPayload(payload="not bytes")  # type: ignore[arg-type]
        return self._real.fetch(evidence_id)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


class _UnknownResultAttic:
    """Delegates to a real ``SqliteAttic`` except for one designated identity,
    whose ``fetch`` returns a value that is none of the three result types at
    all.

    ``AtticAdapter`` is a ``Protocol``, so nothing enforces its return union
    at runtime. This is one step beyond ``_WrongTypeAttic``, which at least
    returns the right wrapper: here there is no ``.payload`` to read, so a
    reconciler that assumed the union would raise ``AttributeError`` — not
    the ``TypeError`` the non-bytes case is guarded against."""

    def __init__(self, data_path: Path, *, unknown: frozenset[UUID]) -> None:
        self._real = SqliteAttic(data_path)
        self._unknown = unknown

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt:
        raise NotImplementedError

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt:
        if evidence_id in self._unknown:
            return b"bare bytes, not a result value"  # type: ignore[return-value]
        return self._real.fetch(evidence_id)

    def search(self, query: str, limit: int) -> tuple[UUID, ...]:
        raise NotImplementedError


def _canonical_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=_INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _seed_catalogue(
    data_path: Path, *, realms: tuple[str, ...] = (_REALM, _OTHER_REALM)
) -> None:
    migrate_catalogue(_config(data_path), lambda: _NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for realm_id in realms:
            connection.execute(
                "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
                (realm_id, _canonical_ts(_NOW)),
            )
            connection.execute(
                "INSERT INTO audit_heads "
                "(chain_kind, chain_identity, last_sequence, last_hash) "
                "VALUES ('realm', ?, 0, ?)",
                (realm_id, bytes(32)),
            )
        connection.commit()


def _segments_column(segments: tuple[ScopeSegment, ...]) -> str:
    return json.dumps(
        [{"id": segment.identifier, "kind": segment.kind} for segment in segments],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _insert_evidence_record(
    data_path: Path,
    evidence_id: UUID,
    *,
    realm_id: str,
    classification: Classification,
    payload_digest: bytes,
    segments: tuple[ScopeSegment, ...] | None = None,
    scope_segments_json: str | None = None,
) -> None:
    """Inserts a catalogue evidence record directly, bypassing ingest
    entirely — reconciliation only ever reads this table, so its fixtures
    need no assertion, grant or principal machinery. Uses the external-
    custody form (no assertion_id) purely to satisfy
    ck_evidence_records_custody_form without a fake assertions row;
    reconcile_evidence reads realm/scope/classification/digest and never
    looks at custody form at all."""
    stored_segments_json = (
        scope_segments_json
        if scope_segments_json is not None
        else _segments_column(segments if segments is not None else ())
    )
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
            "classification, payload_digest, assertion_id, payload_length, "
            "external_uri, recorded_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(evidence_id),
                realm_id,
                stored_segments_json,
                classification.value,
                payload_digest,
                f"https://example.test/evidence/{evidence_id}",
                _canonical_ts(_NOW),
            ),
        )
        connection.commit()


def _digest(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def _seed_matrix(data_path: Path) -> None:
    """Catalogue evidence records at: the request scope, an ancestor, a
    sibling, a descendant, another realm, and a higher classification —
    exactly the fixture Task 10's brief specifies for the disclosure
    matrix."""
    _insert_evidence_record(
        data_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"request scope payload"),
        segments=_REQUEST_SEGMENTS,
    )
    _insert_evidence_record(
        data_path,
        _ANCESTOR_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"ancestor payload"),
        segments=(),
    )
    _insert_evidence_record(
        data_path,
        _SIBLING_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"sibling payload"),
        segments=(_JOB2,),
    )
    _insert_evidence_record(
        data_path,
        _DESCENDANT_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"descendant payload"),
        segments=(_JOB1, _RUN1),
    )
    _insert_evidence_record(
        data_path,
        _OTHER_REALM_ID,
        realm_id=_OTHER_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"other realm payload"),
        segments=_REQUEST_SEGMENTS,
    )
    _insert_evidence_record(
        data_path,
        _HIGHER_CLASSIFICATION_ID,
        realm_id=_REALM,
        classification=Classification.RESTRICTED,
        payload_digest=_digest(b"restricted payload"),
        segments=_REQUEST_SEGMENTS,
    )
    # Every record gets a real, digest-matching payload in Attic — not just
    # the two expected to be disclosed. Otherwise an excluded record is
    # excluded by PayloadAbsent regardless of whether the realm/scope/
    # clearance check that's supposed to exclude it even exists: the point
    # of this fixture is that each of the four exclusion reasons is the
    # *sole* reason its record is excluded, so a mutant deleting any one of
    # them is provably caught rather than masked by a coincidentally-absent
    # payload.
    attic = SqliteAttic(data_path)
    assert attic.store(_REQUEST_SCOPE_ID, b"request scope payload") == PayloadStored()
    assert attic.store(_ANCESTOR_ID, b"ancestor payload") == PayloadStored()
    assert attic.store(_SIBLING_ID, b"sibling payload") == PayloadStored()
    assert attic.store(_DESCENDANT_ID, b"descendant payload") == PayloadStored()
    assert attic.store(_OTHER_REALM_ID, b"other realm payload") == PayloadStored()
    assert (
        attic.store(_HIGHER_CLASSIFICATION_ID, b"restricted payload") == PayloadStored()
    )


def test_only_request_scope_and_descendant_are_disclosed(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_matrix(tmp_path)
    attic = SqliteAttic(tmp_path)
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(
            _REQUEST_SCOPE_ID,
            _ANCESTOR_ID,
            _SIBLING_ID,
            _DESCENDANT_ID,
            _OTHER_REALM_ID,
            _HIGHER_CLASSIFICATION_ID,
        ),
        logger=logger,
    )

    assert {item.evidence_id for item in disclosed} == {
        _REQUEST_SCOPE_ID,
        _DESCENDANT_ID,
    }
    assert len(disclosed) == 2
    # Every record now has a real payload in Attic (see _seed_matrix), so
    # nothing here is excluded by absence — realm, ancestry and clearance
    # are each the sole reason their record is excluded, and none of those
    # exclusions are even operator-visible (unlike a malformed record or a
    # digest mismatch, both logged elsewhere).
    assert stream.getvalue() == ""
    by_id = {item.evidence_id: item for item in disclosed}
    assert by_id[_REQUEST_SCOPE_ID] == DisclosedEvidence(
        evidence_id=_REQUEST_SCOPE_ID,
        payload=b"request scope payload",
        classification=Classification.INTERNAL,
        segments=_REQUEST_SEGMENTS,
    )
    assert by_id[_DESCENDANT_ID] == DisclosedEvidence(
        evidence_id=_DESCENDANT_ID,
        payload=b"descendant payload",
        classification=Classification.INTERNAL,
        segments=(_JOB1, _RUN1),
    )


def test_unknown_and_duplicate_candidates_disclosed_exactly_once(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _seed_matrix(tmp_path)
    attic = SqliteAttic(tmp_path)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID, _REQUEST_SCOPE_ID, _UNKNOWN_ID),
    )

    assert len(disclosed) == 1
    assert disclosed[0].evidence_id == _REQUEST_SCOPE_ID


def test_digest_mismatch_from_a_lying_adapter_is_discarded_and_recorded(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"the real bytes"),
        segments=_REQUEST_SEGMENTS,
    )
    attic = _HostileAttic(tmp_path, tampered={_REQUEST_SCOPE_ID: b"not the real bytes"})
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID,),
        metrics=metrics,
        logger=logger,
    )

    assert disclosed == ()
    rendered, _ = metrics.render()
    assert b"cairn_evidence_digest_mismatch_total 1.0" in rendered
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == LogEvent.EVIDENCE_DIGEST_MISMATCH.value
    assert payload["evidence_id"] == str(_REQUEST_SCOPE_ID)
    assert "the real bytes" not in stream.getvalue()
    assert "not the real bytes" not in stream.getvalue()


def test_adapter_reported_payload_corrupt_is_also_recorded_as_digest_mismatch(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"the real bytes"),
        segments=_REQUEST_SEGMENTS,
    )
    metrics = Metrics()

    disclosed = reconcile_evidence(
        tmp_path,
        _CorruptReportingAttic(),
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID,),
        metrics=metrics,
    )

    assert disclosed == ()
    rendered, _ = metrics.render()
    assert b"cairn_evidence_digest_mismatch_total 1.0" in rendered


def test_adapter_fetch_raising_discards_only_that_candidate(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"payload A"),
        segments=_REQUEST_SEGMENTS,
    )
    second_id = UUID("00000000-0000-4000-8000-000000000007")
    _insert_evidence_record(
        tmp_path,
        second_id,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"payload B"),
        segments=_REQUEST_SEGMENTS,
    )
    real_attic = SqliteAttic(tmp_path)
    assert real_attic.store(second_id, b"payload B") == PayloadStored()
    attic = _RaisingFetchAttic(tmp_path, raising=frozenset({_REQUEST_SCOPE_ID}))
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID, second_id),
        logger=logger,
    )

    # The raising candidate is discarded; the legitimate one behind it in
    # the same batch must not be denied along with it.
    assert len(disclosed) == 1
    assert disclosed[0].evidence_id == second_id
    lines = stream.getvalue().splitlines()
    events = [json.loads(line) for line in lines]
    failed_events = [
        e for e in events if e["event"] == LogEvent.EVIDENCE_FETCH_FAILED.value
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["evidence_id"] == str(_REQUEST_SCOPE_ID)


def test_adapter_returning_non_bytes_payload_is_discarded_not_a_crash(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"payload A"),
        segments=_REQUEST_SEGMENTS,
    )
    attic = _WrongTypeAttic(tmp_path, wrong_type=frozenset({_REQUEST_SCOPE_ID}))
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID,),
        logger=logger,
    )

    assert disclosed == ()
    lines = stream.getvalue().splitlines()
    events = [json.loads(line) for line in lines]
    assert any(e["event"] == LogEvent.EVIDENCE_FETCH_FAILED.value for e in events)


def test_adapter_returning_a_value_outside_the_contract_discards_only_it(
    tmp_path: Path,
) -> None:
    """A result that is none of the three types is a failed fetch, not an
    answer.

    Nothing enforces ``AtticAdapter``'s return union at runtime, so the
    reconciler cannot infer ``FetchedPayload`` from "not absent and not
    corrupt". Reading ``.payload`` off such a value raises ``AttributeError``,
    which is not the guarded ``TypeError``, so it would escape and deny the
    whole batch — the one outcome ruling 5 forbids. The legitimate candidate
    behind it must still be disclosed.
    """
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"payload A"),
        segments=_REQUEST_SEGMENTS,
    )
    second_id = UUID("00000000-0000-4000-8000-000000000007")
    _insert_evidence_record(
        tmp_path,
        second_id,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"payload B"),
        segments=_REQUEST_SEGMENTS,
    )
    assert SqliteAttic(tmp_path).store(second_id, b"payload B") == PayloadStored()
    attic = _UnknownResultAttic(tmp_path, unknown=frozenset({_REQUEST_SCOPE_ID}))
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID, second_id),
        logger=logger,
    )

    assert len(disclosed) == 1
    assert disclosed[0].evidence_id == second_id
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed_events = [
        e for e in events if e["event"] == LogEvent.EVIDENCE_FETCH_FAILED.value
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["evidence_id"] == str(_REQUEST_SCOPE_ID)


def test_payload_absent_is_discarded_silently(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _insert_evidence_record(
        tmp_path,
        _REQUEST_SCOPE_ID,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"never delivered"),
        segments=_REQUEST_SEGMENTS,
    )
    attic = SqliteAttic(tmp_path)  # nothing stored under _REQUEST_SCOPE_ID
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID,),
        metrics=metrics,
        logger=logger,
    )

    assert disclosed == ()
    rendered, _ = metrics.render()
    assert b"cairn_evidence_digest_mismatch_total 1.0" not in rendered
    assert stream.getvalue() == ""


def test_malformed_stored_scope_is_discarded_and_logged_as_unreadable(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    malformed_id = UUID("00000000-0000-4000-8000-0000000000aa")
    # Migration 0003's CHECK pins minified-JSON-array shape only, not that
    # each element is a well-formed {kind, id} object of strings: this row
    # is schema-legal but semantically meaningless, exactly the reading-
    # guard gap ScopeSegment.__post_init__ exists to close.
    _insert_evidence_record(
        tmp_path,
        malformed_id,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"irrelevant"),
        scope_segments_json='[{"id":1,"kind":"job"}]',
    )
    metrics = Metrics()
    stream = StringIO()
    logger = configure_logging(stream)
    attic = SqliteAttic(tmp_path)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(malformed_id,),
        metrics=metrics,
        logger=logger,
    )

    assert disclosed == ()
    rendered, _ = metrics.render()
    assert b"cairn_evidence_digest_mismatch_total 1.0" not in rendered
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == LogEvent.EVIDENCE_RECORD_UNREADABLE.value
    assert payload["evidence_id"] == str(malformed_id)


def test_stored_scope_element_missing_expected_keys_is_also_unreadable(
    tmp_path: Path,
) -> None:
    """A distinct malformed shape from the one above: a schema-legal array
    element whose keys aren't exactly ``{kind, id}`` (here, ``id`` is
    missing outright), hitting ``_stored_segments``'s own key-set guard
    rather than ``ScopeSegment.__post_init__``'s."""
    _seed_catalogue(tmp_path)
    malformed_id = UUID("00000000-0000-4000-8000-0000000000bb")
    _insert_evidence_record(
        tmp_path,
        malformed_id,
        realm_id=_REALM,
        classification=Classification.INTERNAL,
        payload_digest=_digest(b"irrelevant"),
        scope_segments_json='[{"kind":"job"}]',
    )
    stream = StringIO()
    logger = configure_logging(stream)
    attic = SqliteAttic(tmp_path)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(malformed_id,),
        logger=logger,
    )

    assert disclosed == ()
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == LogEvent.EVIDENCE_RECORD_UNREADABLE.value
    assert payload["evidence_id"] == str(malformed_id)


def test_no_metrics_or_logger_is_a_valid_call(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    _seed_matrix(tmp_path)
    attic = SqliteAttic(tmp_path)

    disclosed = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=(_REQUEST_SCOPE_ID,),
    )

    assert len(disclosed) == 1


def test_the_scope_direction_selects_which_ancestry_rule_applies(
    tmp_path: Path,
) -> None:
    """P-43 as amended 9 August 2026. I-69 fixes *that* a candidate's scope
    must satisfy the request's ancestry rule; which direction that is
    belongs to the caller. Audit-style reads want records at the request
    scope or below (the default this module was built with); retrieval
    wants records at the request scope or above, so its evidence gate runs
    in the same direction as the fact filters it feeds. One record at an
    ancestor scope and one at a descendant scope prove the two directions
    are genuinely opposed rather than one being a relaxation of the other.
    """
    _seed_catalogue(tmp_path)
    _seed_matrix(tmp_path)
    attic = SqliteAttic(tmp_path)
    candidates = (_ANCESTOR_ID, _DESCENDANT_ID)

    at_or_below = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=candidates,
    )
    at_or_above = reconcile_evidence(
        tmp_path,
        attic,
        realm_id=_REALM,
        segments=_REQUEST_SEGMENTS,
        read_clearance=_REQUEST_CLEARANCE,
        candidates=candidates,
        scope_direction=ScopeDirection.AT_OR_ABOVE,
    )

    assert [item.evidence_id for item in at_or_below] == [_DESCENDANT_ID]
    assert [item.evidence_id for item in at_or_above] == [_ANCESTOR_ID]
