"""Task 10: ``rebuild-index`` (P-45).

The programme's "derived state rebuilds from the catalogue after deletion"
checkbox. The load-bearing test is the equivalence one: project, destroy
the index, rebuild, and prove retrieval answers identically — not that the
rebuild ran, but that what it produced is indistinguishable from what
incremental delivery produced.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority, IngestAssertion, InvalidateFacts
from cairn.authority.retrieval import RetrievalResult, Retrieve
from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    FailureCode,
    Rejected,
    RetryClass,
)
from cairn.projection.adapter import (
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.delivery import deliver_projection_outbox
from cairn.projection.memory import MemoryIndex
from cairn.projection.partition import canonical_segments_json
from cairn.projection.rebuild import RebuildReport, rebuild_index
from cairn.runtime.cli import main
from cairn.runtime.config import (
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)
from cairn.runtime.logging import LogEvent, configure_logging
from cairn.screening import SecretScreen

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
JOB = ScopeSegment(kind="job", identifier="job-1")
PRINCIPAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")


class _RefusingIndex:
    """Projects nothing and says so — the typed domain refusal, so the
    rebuild counts a failure rather than crashing."""

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return ProjectionFailed(code="index_refused")

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


def _config(data_path: Path, *, graphiti: bool = True) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
        graphiti=GraphitiConfig(enabled=graphiti),
    )


def _seed(config: CairnConfig) -> None:
    config.paths.credentials.mkdir(parents=True, exist_ok=True)
    migrate_catalogue(config, lambda: NOW)
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (REALM, TS)
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'workload', 'rebuilder', ?)",
            (str(PRINCIPAL_ID), TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), bytes(32), TS),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, "
            "delegable_operations, issued_by, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'restricted', ?, NULL, NULL, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                json.dumps(
                    [{"id": JOB.identifier, "kind": JOB.kind}],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                '["ingest","invalidate","promote","retrieve"]',
                '["internal","public","restricted"]',
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()


def _transactions(data_path: Path, *, now: datetime = NOW) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: now,
        uuid_factory=uuid4,
    )


def _authority(
    data_path: Path, *, now: datetime = NOW, index: object = None
) -> CairnAuthority:
    return CairnAuthority(
        data_path,
        _transactions(data_path, now=now),
        lambda: now,
        uuid4,
        exact_evidence_enabled=False,
        screen=SecretScreen(),
        index=cast(MemoryIndex | None, index),
    )


def _ingest(data_path: Path, body: str, *, now: datetime = NOW) -> UUID:
    outcome = _authority(data_path, now=now).ingest(
        Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
        IngestAssertion(
            scope=Scope(REALM, (JOB,)),
            classification=Classification.INTERNAL,
            source_type=SourceType.AGENT_CLAIM,
            facts=(FactDraft(body=body, valid_from=None, valid_to=None),),
            requested_trust=TrustClass.CANDIDATE,
        ),
        idempotency_key=uuid4(),
        correlation_id=CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    return outcome.value.fact_ids[0]


def _invalidate(data_path: Path, fact_id: UUID, *, now: datetime) -> None:
    outcome = _authority(data_path, now=now).invalidate(
        Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
        InvalidateFacts(fact_ids=(fact_id,), reason="superseded", superseded_by=None),
        idempotency_key=uuid4(),
        correlation_id=CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)


def _drain(data_path: Path) -> MemoryIndex:
    index = MemoryIndex()
    deliver_projection_outbox(
        _transactions(data_path), index, clock=lambda: NOW, limit=1000
    )
    return index


def _retrieve(
    data_path: Path,
    index: MemoryIndex,
    *,
    query: str = "fact",
    as_of: datetime | None = None,
    now: datetime,
) -> RetrievalResult:
    outcome = _authority(data_path, now=now, index=index).retrieve(
        Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
        Retrieve(
            scope=Scope(REALM, (JOB,)),
            query=query,
            budget=1_000_000,
            trust_filters=frozenset({TrustClass.CANDIDATE}),
            as_of=as_of,
        ),
        correlation_id=CORRELATION_ID,
    )
    assert isinstance(outcome, RetrievalResult)
    return outcome


def _outbox_rows(data_path: Path) -> list[tuple[str, str, str | None, str]]:
    """Kind, fact, mutation and stamp for every row, oldest first — the four
    columns the amended I-68 makes load-bearing for a rebuild row."""
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return cast(
            list[tuple[str, str, str | None, str]],
            connection.execute(
                "SELECT kind, fact_id, mutation_id, created_at "
                "FROM projection_outbox ORDER BY created_at, fact_id"
            ).fetchall(),
        )
    finally:
        connection.close()


def _depth(data_path: Path) -> int:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        return cast(
            int,
            connection.execute("SELECT COUNT(*) FROM projection_outbox").fetchone()[0],
        )
    finally:
        connection.close()


# --- the equivalence proof ----------------------------------------------------


def test_a_rebuilt_index_answers_identically_to_an_incrementally_built_one(
    tmp_path: Path,
) -> None:
    """The programme checkbox. Project three facts incrementally, record
    what retrieval says, destroy the index entirely, rebuild from the
    catalogue, and require the same answer — including for the fact whose
    belief has since ended, which is where a naive rebuild diverges."""
    config = _config(tmp_path)
    _seed(config)
    first = _ingest(tmp_path, "the first fact", now=NOW)
    _ingest(tmp_path, "the second fact", now=NOW + timedelta(minutes=1))
    _ingest(tmp_path, "the third fact", now=NOW + timedelta(minutes=2))
    _invalidate(tmp_path, first, now=NOW + timedelta(minutes=3))
    query_now = NOW + timedelta(hours=1)
    incremental = _drain(tmp_path)
    before = _retrieve(tmp_path, incremental, now=query_now)
    historical_before = _retrieve(tmp_path, incremental, as_of=NOW, now=query_now)

    rebuilt = MemoryIndex()
    report = rebuild_index(
        tmp_path, _transactions(tmp_path), rebuilt, uuid_factory=uuid4
    )

    assert report.projected == 3
    assert report.failed == 0
    assert report.unreadable == 0
    after = _retrieve(tmp_path, rebuilt, now=query_now)
    historical_after = _retrieve(tmp_path, rebuilt, as_of=NOW, now=query_now)
    assert after == before
    assert historical_after == historical_before
    # And the invalidated fact really is the interesting case: visible
    # historically, invisible now, from a freshly rebuilt index.
    assert [hit.fact_id for hit in historical_after.hits] == [first]
    assert first not in {hit.fact_id for hit in after.hits}


def test_the_rebuild_clears_what_the_index_held_first(tmp_path: Path) -> None:
    """A fact deleted from the catalogue is impossible (facts are
    immutable), but an index holding entries from another instance's data,
    or from a partition since renamed, must not survive a rebuild."""
    config = _config(tmp_path)
    _seed(config)
    _ingest(tmp_path, "the only fact")
    index = _drain(tmp_path)
    index.project(
        ProjectedFactState(
            fact_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            partition_key="a stale partition",
            body="an entry from somewhere else",
            realm_id=REALM,
            segments=(),
            classification=Classification.INTERNAL,
            trust=TrustClass.CANDIDATE,
            recorded_at=NOW,
            valid_from=None,
            valid_to=None,
            invalidated_at=None,
        )
    )

    rebuild_index(tmp_path, _transactions(tmp_path), index, uuid_factory=uuid4)

    assert index.search("somewhere else", 10, ("a stale partition",)) == ()


def test_pending_outbox_rows_are_superseded_by_the_enqueue(tmp_path: Path) -> None:
    """A pending custody row is replaced, not added to. Two rows for one
    fact would leave the custody row behind after re-projection deleted the
    rebuild row, and I-83 would then refuse that scope for ever — a rebuild
    that permanently disabled retrieval for the facts it had just
    restored."""
    config = _config(tmp_path)
    _seed(config)
    _ingest(tmp_path, "never delivered")
    assert _depth(tmp_path) == 1

    report = rebuild_index(
        tmp_path, _transactions(tmp_path), MemoryIndex(), uuid_factory=uuid4
    )

    assert report.projected == 1
    assert report.superseded_rows == 1
    assert _depth(tmp_path) == 0


def test_a_refusing_index_is_counted_rather_than_raised(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(config)
    _ingest(tmp_path, "one")
    _ingest(tmp_path, "two", now=NOW + timedelta(minutes=1))

    report = rebuild_index(
        tmp_path, _transactions(tmp_path), _RefusingIndex(), uuid_factory=uuid4
    )

    assert report.projected == 0
    assert report.failed == 2


def test_a_failed_rebuild_of_a_drained_fact_cannot_be_served_as_complete(
    tmp_path: Path,
) -> None:
    """The case the first remediation missed, and the reason the enqueue
    comes before the clear.

    A fact delivered long ago has **no** outbox row, so an approach that
    merely preserved existing rows preserved nothing here: after
    ``clear(None)`` destroyed its index entry and re-projection failed, the
    catalogue held the fact, the index did not, and the outbox said the
    index was up to date. Retrieval would then have served a result that
    was silently short — and its own silent-discard discipline guarantees
    the caller could not distinguish that from an honest empty answer.

    So the proof is not that a row survives; it is that the system refuses
    to answer. Both the present and a historical ``as_of`` are checked: the
    rebuild rows carry their fact's ``recorded_at``, and stamping them with
    the rebuild's own clock would leave exactly the historical read
    unprotected while the current one looked fine.
    """
    config = _config(tmp_path)
    _seed(config)
    fact_id = _ingest(tmp_path, "delivered long ago", now=NOW)
    _drain(tmp_path)
    assert _depth(tmp_path) == 0

    report = rebuild_index(
        tmp_path, _transactions(tmp_path), _RefusingIndex(), uuid_factory=uuid4
    )

    assert report.failed == 1
    assert report.projected == 0
    # The fact is owed again, durably, and says so in its own past.
    assert _depth(tmp_path) == 1
    assert _outbox_rows(tmp_path) == [("fact-rebuild", str(fact_id), None, TS)]

    query_now = NOW + timedelta(hours=1)
    for as_of in (None, NOW):
        outcome = _authority(tmp_path, now=query_now, index=MemoryIndex()).retrieve(
            Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
            Retrieve(
                scope=Scope(REALM, (JOB,)),
                query="delivered",
                budget=1_000_000,
                trust_filters=frozenset({TrustClass.CANDIDATE}),
                as_of=as_of,
            ),
            correlation_id=CORRELATION_ID,
        )

        assert isinstance(outcome, Rejected)
        assert outcome.failure.code is FailureCode.INDEX_PENDING
        assert outcome.failure.retry is RetryClass.AFTER_DELAY


def test_the_delivery_loop_heals_a_partially_rebuilt_index(tmp_path: Path) -> None:
    """The other half of the recovery: the surviving rows are ordinary work
    the existing deliverer drains, so an index left short by a failed
    rebuild repairs itself when an instance next serves, with no operator
    and no second rebuild."""
    config = _config(tmp_path)
    _seed(config)
    _ingest(tmp_path, "delivered long ago", now=NOW)
    _drain(tmp_path)
    rebuild_index(
        tmp_path, _transactions(tmp_path), _RefusingIndex(), uuid_factory=uuid4
    )
    assert _depth(tmp_path) == 1

    healed = _drain(tmp_path)

    assert _depth(tmp_path) == 0
    assert [
        hit.body for hit in _retrieve(tmp_path, healed, query="delivered", now=NOW).hits
    ] == ["delivered long ago"]


def test_an_unreadable_fact_keeps_its_rebuild_row(tmp_path: Path) -> None:
    """The other partial outcome. A row the value layer refuses is not a
    projection failure, but it is still a fact the index does not hold, so
    it keeps its marker for the same reason."""
    config = _config(tmp_path)
    _seed(config)
    good = _ingest(tmp_path, "intact")
    _drain(tmp_path)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) "
            "SELECT 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', realm_id, "
            "'[{\"id\":1,\"kind\":7}]', 'hostile', trust, classification, "
            "assertion_id, recorded_at FROM facts WHERE fact_id = ?",
            (str(good),),
        )
        connection.commit()

    report = rebuild_index(
        tmp_path, _transactions(tmp_path), MemoryIndex(), uuid_factory=uuid4
    )

    assert report.unreadable == 1
    assert report.projected == 1
    # Exactly one row left, and it belongs to the fact that was not stored.
    assert _outbox_rows(tmp_path) == [
        ("fact-rebuild", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", None, TS)
    ]


def test_the_enqueue_stamps_each_row_with_its_fact_recorded_at(
    tmp_path: Path,
) -> None:
    """I-83 compares ``created_at`` against the request's ``as_of``, so the
    column has to say when the index started owing the fact, not when the
    rebuild noticed. Stamped with the rebuild's clock, every one of these
    rows would sort after any historical query and protect none of them."""
    config = _config(tmp_path)
    _seed(config)
    later = NOW + timedelta(hours=3)
    _ingest(tmp_path, "early", now=NOW)
    _ingest(tmp_path, "late", now=later)
    _drain(tmp_path)

    rebuild_index(
        tmp_path, _transactions(tmp_path), _RefusingIndex(), uuid_factory=uuid4
    )

    assert [row[3] for row in _outbox_rows(tmp_path)] == [
        TS,
        canonical_timestamp(later),
    ]


def test_an_unreadable_row_is_counted_separately(tmp_path: Path) -> None:
    """Catalogue corruption is not a projection failure: ``verify`` is what
    diagnoses it, and the rebuild only has to refuse to guess."""
    config = _config(tmp_path)
    _seed(config)
    good = _ingest(tmp_path, "intact")
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) "
            "SELECT 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', realm_id, "
            "'[{\"id\":1,\"kind\":7}]', 'hostile', trust, classification, "
            "assertion_id, recorded_at FROM facts WHERE fact_id = ?",
            (str(good),),
        )
        connection.commit()

    report = rebuild_index(
        tmp_path, _transactions(tmp_path), MemoryIndex(), uuid_factory=uuid4
    )

    assert report.projected == 1
    assert report.unreadable == 1
    assert report.failed == 0


def test_the_rebuild_pages_through_a_batch_boundary(tmp_path: Path) -> None:
    """Paging is not decoration: a catalogue needing a rebuild is large,
    and a boundary bug would silently drop everything past the first page."""
    config = _config(tmp_path)
    _seed(config)
    for offset in range(5):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))

    report = rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        MemoryIndex(),
        uuid_factory=uuid4,
        batch_size=2,
    )

    assert report.projected == 5


# --- chunking through project_many (P-82) --------------------------------------


class BulkRecordingIndex:
    """A local copy of the Task 4 delivery-test fake, standalone so this
    file reads without importing ``test_projection_delivery.py``. Records
    every call so tests can assert which path ran."""

    def __init__(self) -> None:
        self.single: list[ProjectedFactState] = []
        self.bulk: list[tuple[ProjectedFactState, ...]] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.single.append(state)
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk.append(states)
        return tuple(FactProjected() for _ in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None


class BulkBrokenIndex(BulkRecordingIndex):
    """Bulk always breaks; per-fact works — the demotion case."""

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self.bulk.append(states)
        raise RuntimeError("bulk path down")


def _insert_fact(
    tmp_path: Path,
    *,
    fact_id: UUID,
    body: str,
    scope_segments: str,
    based_on: UUID,
    recorded_at: str,
) -> None:
    """A raw second fact sharing ``based_on``'s assertion, trust and
    classification but its own scope — the same technique
    ``test_an_unreadable_fact_keeps_its_rebuild_row`` uses to plant a row
    ``rebuild_index`` reads directly from the catalogue, here with a valid
    scope so the fact projects rather than failing to parse."""
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) "
            "SELECT ?, realm_id, ?, ?, trust, classification, assertion_id, ? "
            "FROM facts WHERE fact_id = ?",
            (str(fact_id), scope_segments, body, recorded_at, str(based_on)),
        )
        connection.commit()


def test_a_chunked_rebuild_projects_through_project_many_and_discharges(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed(config)
    for offset in range(5):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))
    index = BulkRecordingIndex()

    report = rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        index,
        uuid_factory=uuid4,
        chunk_size=2,
    )

    assert report.projected == 5
    assert report.failed == 0
    assert _depth(tmp_path) == 0
    assert [len(states) for states in index.bulk] == [2, 2, 1]
    assert index.single == []


def test_a_broken_bulk_rebuild_demotes_to_the_per_fact_loop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(config)
    for offset in range(3):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))
    index = BulkBrokenIndex()

    report = rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        index,
        uuid_factory=uuid4,
        chunk_size=3,
    )

    assert report.projected == 3
    assert report.failed == 0
    assert _depth(tmp_path) == 0
    assert len(index.single) == 3


def test_a_broken_bulk_rebuild_emits_the_bulk_demoted_event(tmp_path: Path) -> None:
    """P-82 gate-4 ruling (25 August 2026): the rebuild loop's demotion is
    the same silent slowness as delivery's and emits the same event."""
    config = _config(tmp_path)
    _seed(config)
    for offset in range(3):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))
    stream = StringIO()
    logger = configure_logging(stream)

    rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        BulkBrokenIndex(),
        uuid_factory=uuid4,
        chunk_size=3,
        logger=logger,
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    assert demoted[0]["chunk_size"] == 3
    assert demoted[0]["exception_type"] == "RuntimeError"
    assert demoted[0]["failure_shape"] == "adapter_raised"


def test_a_malformed_rebuild_answer_names_its_failure_shape(tmp_path: Path) -> None:
    """P-82 gate-4 ruling: the rebuild loop's demotion carries the same
    partition-free failure shape as delivery's. The rebuild path reaches
    the malformed branch with no exception at all, so the shape is the
    only thing distinguishing it from a size and nothing else."""
    config = _config(tmp_path)
    _seed(config)
    for offset in range(3):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))

    class ShortIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return (FactProjected(),)

    stream = StringIO()
    logger = configure_logging(stream)

    rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        ShortIndex(),
        uuid_factory=uuid4,
        chunk_size=3,
        logger=logger,
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    demoted = [
        event
        for event in events
        if event["event"] == LogEvent.PROJECTION_BULK_DEMOTED.value
    ]
    assert len(demoted) == 1
    assert demoted[0]["chunk_size"] == 3
    assert demoted[0].get("exception_type") is None
    assert demoted[0]["failure_shape"] == "answer_length_mismatch"


def test_an_invalid_rebuild_result_element_demotes_to_the_per_fact_loop(
    tmp_path: Path,
) -> None:
    """An aligned tuple carrying a non-explicit element is no per-state
    answer: the buffer demotes to the per-fact loop instead of counting
    the invalid element as a failure."""
    config = _config(tmp_path)
    _seed(config)
    for offset in range(3):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))

    class LyingIndex(BulkRecordingIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self.bulk.append(states)
            return tuple(cast(FactProjected, None) for _ in states)

    index = LyingIndex()

    report = rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        index,
        uuid_factory=uuid4,
        chunk_size=3,
    )

    assert report.projected == 3
    assert report.failed == 0
    assert _depth(tmp_path) == 0
    assert len(index.bulk) == 1 and len(index.single) == 3


def test_the_default_rebuild_path_never_calls_project_many(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(config)
    for offset in range(3):
        _ingest(tmp_path, f"fact {offset}", now=NOW + timedelta(minutes=offset))
    index = BulkRecordingIndex()

    report = rebuild_index(tmp_path, _transactions(tmp_path), index, uuid_factory=uuid4)

    assert report.projected == 3
    assert index.bulk == []
    assert len(index.single) == 3


def test_rebuild_chunks_never_mix_partitions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(config)
    base = _ingest(tmp_path, "base fact", now=NOW)
    job_two = ScopeSegment(kind="job", identifier="job-2")
    scope_a = canonical_segments_json((JOB,))
    scope_b = canonical_segments_json((job_two,))
    for offset in range(3):
        _insert_fact(
            tmp_path,
            fact_id=uuid4(),
            body=f"partition a {offset}",
            scope_segments=scope_a,
            based_on=base,
            recorded_at=canonical_timestamp(NOW + timedelta(minutes=offset + 1)),
        )
    for offset in range(3):
        _insert_fact(
            tmp_path,
            fact_id=uuid4(),
            body=f"partition b {offset}",
            scope_segments=scope_b,
            based_on=base,
            recorded_at=canonical_timestamp(NOW + timedelta(minutes=offset + 10)),
        )
    index = BulkRecordingIndex()

    report = rebuild_index(
        tmp_path,
        _transactions(tmp_path),
        index,
        uuid_factory=uuid4,
        chunk_size=100,
    )

    assert report.projected == 7
    assert index.bulk != []
    for states in index.bulk:
        assert len({state.partition_key for state in states}) == 1


# --- the CLI command ----------------------------------------------------------


def test_the_cli_rebuilds_and_reports_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    _ingest(tmp_path, "cli fact")
    config_path = _write_config_file(tmp_path)

    exit_code = main(["rebuild-index", "--config", str(config_path)])

    assert exit_code == 0
    payload = json.loads(_stdout(capsys))
    assert payload["status"] == "ok"
    assert payload["projected"] == 1
    assert payload["failed"] == 0
    assert payload["superseded_rows"] == 1


def test_the_cli_reports_a_partial_rebuild_as_partial(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report is stubbed because what is under test is how the CLI
    renders an outcome, not how the rebuild reaches one: ``status`` must
    agree with the exit code on the one path where the index is left
    incomplete, and the counts must still reach the operator."""
    config = _config(tmp_path)
    _seed(config)
    config_path = _write_config_file(tmp_path)
    monkeypatch.setattr(
        "cairn.runtime.cli.rebuild_index",
        lambda *args, **kwargs: RebuildReport(
            projected=3, failed=1, unreadable=0, superseded_rows=0
        ),
    )

    exit_code = main(["rebuild-index", "--config", str(config_path)])

    assert exit_code == 4
    payload = json.loads(_stdout(capsys))
    assert payload["status"] == "partial"
    assert payload["projected"] == 3
    assert payload["failed"] == 1


def test_the_cli_refuses_when_the_index_is_disabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path, graphiti=False)
    _seed(config)
    config_path = _write_config_file(tmp_path, graphiti=False)

    exit_code = main(["rebuild-index", "--config", str(config_path)])

    assert exit_code == 4
    assert json.loads(_stderr(capsys))["code"] == "retrieval_index_disabled"


def test_the_cli_refuses_while_an_instance_holds_the_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P-45's exclusion: a rebuild racing a delivery loop would leave a
    state neither produced."""
    from cairn.runtime.lease import DataDirectoryLease

    config = _config(tmp_path)
    _seed(config)
    config_path = _write_config_file(tmp_path)
    holder = DataDirectoryLease(tmp_path, INSTANCE_ID)
    holder.acquire()
    try:
        exit_code = main(["rebuild-index", "--config", str(config_path)])
    finally:
        holder.release()

    assert exit_code == 4
    assert json.loads(_stderr(capsys))["code"] == "already_locked"


def _write_config_file(tmp_path: Path, *, graphiti: bool = True) -> Path:
    config_path = tmp_path / "cairn.yaml"
    config_path.write_text(
        "\n".join(
            (
                "schema_version: cairn.config/v1",
                f"instance_id: {INSTANCE_ID}",
                "mode: test",
                "http:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "paths:",
                f"  data: {tmp_path}",
                f"  credentials: {tmp_path / 'credentials'}",
                "graphiti:",
                f"  enabled: {'true' if graphiti else 'false'}",
                "",
            )
        )
    )
    return config_path


def _stdout(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err
