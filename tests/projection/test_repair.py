"""#204 (I-99): targeted repair of retrieval-index coverage.

Facts go through the real authority into a migrated catalogue, so the outbox
rows and ``recorded_at`` stamps are the ones production writes. The index is a
fake whose coverage audit answers from a table the test sets: a torn or absent
graph is staged, not grown. The engine tests in ``test_fact_*_db.py`` check
what the real gates say about the same shapes.
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority, IngestAssertion
from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions, Committed
from cairn.projection.adapter import (
    CoverageAudit,
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.delivery import deliver_projection_outbox
from cairn.projection.fact_vectors import FactVectorError, audit_rows
from cairn.projection.memory import MemoryIndex
from cairn.projection.partition import canonical_partition, canonical_segments_json
from cairn.projection.repair import RepairReport, coverage_auditor, repair_index
from cairn.runtime.cli import main
from cairn.runtime.config import (
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)
from cairn.runtime.lease import DataDirectoryLease
from cairn.runtime.logging import LogEvent, configure_logging
from cairn.screening import SecretScreen

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
JOB = ScopeSegment(kind="job", identifier="job-1")
PARTITION = canonical_partition(REALM, canonical_segments_json((JOB,)))
UNSCOPED = canonical_partition(REALM, canonical_segments_json(()))
PRINCIPAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREDENTIAL_ID = UUID("aaaaaaaa-bbbb-4aaa-8aaa-aaaaaaaaaaaa")
GRANT_ID = UUID("eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee")
CORRELATION_ID = UUID("88888888-8888-4888-8888-888888888888")
STRAY = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EMPTY_AUDIT = CoverageAudit(frozenset(), frozenset(), 0)


class _Auditing(MemoryIndex):
    """A MemoryIndex that also audits coverage from a table the test sets.

    A partition missing from the table is audited as holding nothing, so every
    catalogue fact in it is owed. ``projected`` records each fact ``project``
    is asked for; ``refuse`` makes every projection a typed refusal.
    ``verified`` records every ``set_coverage_verified`` call, in order.
    """

    def __init__(
        self, audits: dict[str, CoverageAudit] | None = None, *, refuse: bool = False
    ) -> None:
        super().__init__()
        self.audits = audits or {}
        self.refuse = refuse
        self.audited: list[str] = []
        self.projected: list[UUID] = []
        self.verified: list[bool] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.projected.append(state.fact_id)
        if self.refuse:
            return ProjectionFailed(code="index_refused")
        return super().project(state)

    def set_coverage_verified(self, verified: bool) -> None:
        self.verified.append(verified)

    def coverage_audit(self, partition_key: str) -> CoverageAudit:
        self.audited.append(partition_key)
        return self.audits.get(partition_key, EMPTY_AUDIT)


def _audit(
    *,
    present: tuple[UUID, ...] = (),
    defective: tuple[UUID, ...] = (),
    unidentified: int = 0,
) -> CoverageAudit:
    return CoverageAudit(
        frozenset(str(identity) for identity in present),
        frozenset(str(identity) for identity in defective),
        unidentified,
    )


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
            "VALUES (?, 'workload', 'repairer', ?)",
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
            "VALUES (?, ?, ?, '[]', ?, 'restricted', ?, NULL, NULL, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
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


def _authority(data_path: Path, *, now: datetime = NOW) -> CairnAuthority:
    return CairnAuthority(
        data_path,
        _transactions(data_path, now=now),
        lambda: now,
        uuid4,
        exact_evidence_enabled=False,
        screen=SecretScreen(),
    )


def _ingest(
    data_path: Path,
    body: str,
    *,
    now: datetime = NOW,
    segments: tuple[ScopeSegment, ...] = (JOB,),
) -> UUID:
    outcome = _authority(data_path, now=now).ingest(
        Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
        IngestAssertion(
            scope=Scope(REALM, segments),
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


def _drain(data_path: Path, index: MemoryIndex | None = None) -> MemoryIndex:
    """Delivers every pending row, so the facts left behind have no row."""
    target = index if index is not None else MemoryIndex()
    deliver_projection_outbox(
        _transactions(data_path), target, clock=lambda: NOW, limit=1000
    )
    return target


def _repair(
    data_path: Path,
    index: _Auditing,
    *,
    cap: int | None = 50,
    dry_run: bool = False,
    now: datetime = NOW,
    logger: Any = None,
) -> RepairReport:
    return repair_index(
        _transactions(data_path, now=now),
        index,
        uuid_factory=uuid4,
        cap=cap,
        dry_run=dry_run,
        logger=logger,
    )


def _outbox_rows(data_path: Path) -> list[tuple[str, str, str | None, str]]:
    """Kind, fact, mutation and stamp for every row, oldest first."""
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


def _recorded_at(data_path: Path, fact_id: UUID) -> str:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    try:
        row = connection.execute(
            "SELECT recorded_at FROM facts WHERE fact_id = ?", (str(fact_id),)
        ).fetchone()
        return cast(str, row[0])
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


class _RecordingLogger:
    """Stands in for SafeLogger, keeping what each event was emitted with."""

    def __init__(self) -> None:
        self.events: list[tuple[LogEvent, int, dict[str, Any]]] = []

    def emit(
        self, event: LogEvent, /, *, level: int = logging.INFO, **fields: Any
    ) -> None:
        self.events.append((event, level, fields))


# --- what the repair owes -----------------------------------------------------


@pytest.mark.parametrize("shape", ["torn", "absent"])
def test_an_owed_fact_gets_one_rebuild_row_stamped_with_its_recorded_at(
    tmp_path: Path, shape: str
) -> None:
    """Torn: present but refused. Absent: not in the graph at all. Either way
    the row is ``fact-rebuild`` with no mutation, and its stamp is the fact's
    own ``recorded_at``, not the repair's clock (I-83)."""
    _seed(_config(tmp_path))
    fact = _ingest(tmp_path, "owed fact")
    _drain(tmp_path)
    audit = (
        _audit(present=(fact,), defective=(fact,)) if shape == "torn" else EMPTY_AUDIT
    )
    index = _Auditing({PARTITION: audit})

    report = _repair(tmp_path, index, now=NOW + timedelta(hours=1))

    assert report == RepairReport(targets=1, enqueued=1, unrepairable=0, over_cap=False)
    assert _recorded_at(tmp_path, fact) == TS
    assert _outbox_rows(tmp_path) == [("fact-rebuild", str(fact), None, TS)]


def test_a_healthy_present_fact_gets_no_row(tmp_path: Path) -> None:
    _seed(_config(tmp_path))
    fact = _ingest(tmp_path, "healthy fact")
    _drain(tmp_path)
    stream = StringIO()
    index = _Auditing({PARTITION: _audit(present=(fact,))})

    report = _repair(tmp_path, index, logger=configure_logging(stream))

    assert report == RepairReport(targets=0, enqueued=0, unrepairable=0, over_cap=False)
    assert _depth(tmp_path) == 0
    assert stream.getvalue() == ""


def test_a_fact_with_a_pending_row_is_left_to_that_row_even_when_defective(
    tmp_path: Path,
) -> None:
    """The undrained ingest row re-projects the fact anyway. A second row
    would replay it twice for nothing."""
    _seed(_config(tmp_path))
    fact = _ingest(tmp_path, "pending fact")
    index = _Auditing({PARTITION: _audit(present=(fact,), defective=(fact,))})

    report = _repair(tmp_path, index)

    assert report == RepairReport(targets=0, enqueued=0, unrepairable=0, over_cap=False)
    assert [row[0] for row in _outbox_rows(tmp_path)] == ["fact-ingested"]


def test_affected_names_every_torn_or_absent_fact_including_ones_with_rows(
    tmp_path: Path,
) -> None:
    _seed(_config(tmp_path))
    healthy = _ingest(tmp_path, "healthy")
    absent = _ingest(tmp_path, "absent", now=NOW + timedelta(minutes=1))
    _drain(tmp_path)
    covered = _ingest(tmp_path, "covered", now=NOW + timedelta(minutes=2))
    index = _Auditing({PARTITION: _audit(present=(healthy,))})

    report = _repair(tmp_path, index)

    assert report.targets == 1
    assert report.affected == frozenset({str(absent), str(covered)})


def test_a_dry_run_counts_what_it_would_enqueue_and_writes_nothing(
    tmp_path: Path,
) -> None:
    _seed(_config(tmp_path))
    for number in range(3):
        _ingest(tmp_path, f"fact {number}", now=NOW + timedelta(minutes=number))
    _drain(tmp_path)
    index = _Auditing()

    report = _repair(tmp_path, index, dry_run=True)

    assert report == RepairReport(targets=3, enqueued=0, unrepairable=0, over_cap=False)
    assert _depth(tmp_path) == 0


# --- the cap --------------------------------------------------------------------


def _three_absent_facts(data_path: Path) -> None:
    _seed(_config(data_path))
    for number in range(3):
        _ingest(data_path, f"fact {number}", now=NOW + timedelta(minutes=number))
    _drain(data_path)


def test_over_the_cap_nothing_is_enqueued_and_the_pass_says_so(
    tmp_path: Path,
) -> None:
    _three_absent_facts(tmp_path)
    logger = _RecordingLogger()

    report = _repair(tmp_path, _Auditing(), cap=2, logger=logger)

    assert report == RepairReport(targets=3, enqueued=0, unrepairable=0, over_cap=True)
    assert _depth(tmp_path) == 0
    assert logger.events == [
        (
            LogEvent.PROJECTION_REPAIR_OVER_CAP,
            logging.WARNING,
            {"transport": None, "remaining": 3},
        )
    ]


def test_the_cap_counts_what_is_owed_when_the_rows_are_written(
    tmp_path: Path,
) -> None:
    """Sol review P2: a fact that gains a row while the graph is audited is
    no longer owed, so the pass falls back under the cap and repairs the
    rest instead of refusing on its stale snapshot count."""
    _three_absent_facts(tmp_path)
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        (covered,) = connection.execute(
            "SELECT fact_id FROM facts ORDER BY recorded_at LIMIT 1"
        ).fetchone()
    finally:
        connection.close()

    class _MutatedDuringAudit(_Auditing):
        def coverage_audit(self, partition_key: str) -> CoverageAudit:
            _transactions(tmp_path).execute_compound(
                lambda tx: tx.execute(
                    "INSERT INTO projection_outbox "
                    "(work_id, kind, fact_id, mutation_id, created_at, attempts) "
                    "VALUES (?, 'fact-rebuild', ?, NULL, ?, 0)",
                    (str(uuid4()), covered, TS),
                )
            )
            return super().coverage_audit(partition_key)

    report = _repair(tmp_path, _MutatedDuringAudit(), cap=2)

    assert report == RepairReport(targets=2, enqueued=2, unrepairable=0, over_cap=False)
    assert _depth(tmp_path) == 3
    assert [row[1] for row in _outbox_rows(tmp_path)].count(covered) == 1


def test_a_cap_equal_to_the_targets_enqueues_them_all(tmp_path: Path) -> None:
    _three_absent_facts(tmp_path)

    report = _repair(tmp_path, _Auditing(), cap=3)

    assert report == RepairReport(targets=3, enqueued=3, unrepairable=0, over_cap=False)
    assert _depth(tmp_path) == 3


def test_no_cap_is_uncapped(tmp_path: Path) -> None:
    _three_absent_facts(tmp_path)
    logger = _RecordingLogger()

    report = _repair(tmp_path, _Auditing(), cap=None, logger=logger)

    assert report == RepairReport(targets=3, enqueued=3, unrepairable=0, over_cap=False)
    assert _depth(tmp_path) == 3
    assert logger.events == [
        (
            LogEvent.PROJECTION_REPAIR_ENQUEUED,
            logging.INFO,
            {"transport": None, "remaining": 3},
        )
    ]


# --- what no row can repair -----------------------------------------------------


def test_refusals_no_fact_explains_are_counted_logged_and_never_enqueued(
    tmp_path: Path,
) -> None:
    """A defective episode whose identity is not in the catalogue, and an
    episode whose identity is not a string, are both beyond a row's reach."""
    _seed(_config(tmp_path))
    fact = _ingest(tmp_path, "healthy fact")
    _drain(tmp_path)
    logger = _RecordingLogger()
    index = _Auditing(
        {PARTITION: _audit(present=(fact, STRAY), defective=(STRAY,), unidentified=2)}
    )

    report = _repair(tmp_path, index, logger=logger)

    assert report == RepairReport(targets=0, enqueued=0, unrepairable=3, over_cap=False)
    assert _depth(tmp_path) == 0
    assert logger.events == [
        (
            LogEvent.PROJECTION_REPAIR_UNREPAIRABLE,
            logging.WARNING,
            {"transport": None, "failed": 3},
        )
    ]


def test_a_defect_is_unrepairable_in_a_partition_that_does_not_hold_its_fact(
    tmp_path: Path,
) -> None:
    """Each partition's audit is checked only against that partition's own
    catalogue facts, so a job fact named by the unscoped partition's audit is
    not repairable from there."""
    _seed(_config(tmp_path))
    job_fact = _ingest(tmp_path, "job fact")
    unscoped_fact = _ingest(tmp_path, "unscoped fact", segments=())
    _drain(tmp_path)
    index = _Auditing(
        {
            PARTITION: _audit(present=(job_fact,)),
            UNSCOPED: _audit(present=(unscoped_fact,), defective=(job_fact,)),
        }
    )

    report = _repair(tmp_path, index)

    assert report == RepairReport(targets=0, enqueued=0, unrepairable=1, over_cap=False)
    assert _depth(tmp_path) == 0


# --- end to end -------------------------------------------------------------------


def test_the_repair_reprojects_exactly_its_targets_and_the_outbox_drains(
    tmp_path: Path,
) -> None:
    _seed(_config(tmp_path))
    absent = _ingest(tmp_path, "absent fact")
    torn = _ingest(tmp_path, "torn fact")
    healthy = _ingest(tmp_path, "healthy fact")
    _drain(tmp_path)
    index = _Auditing({PARTITION: _audit(present=(torn, healthy), defective=(torn,))})
    _repair(tmp_path, index)

    delivered = deliver_projection_outbox(
        _transactions(tmp_path), index, clock=lambda: NOW, limit=1000
    )

    assert sorted(index.projected, key=str) == sorted((absent, torn), key=str)
    assert healthy not in index.projected
    assert delivered.delivered == 2
    assert delivered.remaining == 0
    assert _depth(tmp_path) == 0


def test_only_an_index_that_can_audit_is_repairable() -> None:
    assert coverage_auditor(MemoryIndex()) is None
    index = _Auditing()
    assert coverage_auditor(index) is index


def test_a_non_callable_audit_attribute_is_not_the_capability() -> None:
    class Impostor(MemoryIndex):
        coverage_audit = None

    assert coverage_auditor(Impostor()) is None


def test_an_index_that_cannot_fail_closed_is_not_repairable() -> None:
    """#204 (P1): repair without the verification gate could leave semantic
    search open while the graph is still unaudited, so an audit alone is not
    the capability. Both halves are required."""

    class AuditOnly(MemoryIndex):
        def coverage_audit(self, partition_key: str) -> CoverageAudit:
            return EMPTY_AUDIT

    class VerifyOnly(MemoryIndex):
        def set_coverage_verified(self, verified: bool) -> None:
            return None

    assert coverage_auditor(AuditOnly()) is None
    assert coverage_auditor(VerifyOnly()) is None


# --- the audit value --------------------------------------------------------------


def test_merge_unions_identities_and_keeps_the_larger_unidentified_count() -> None:
    """Both gates walk the same episodes, so an unidentified one is seen by
    each. Summing would count it twice."""
    vector = CoverageAudit(frozenset({"a"}), frozenset({"a"}), 1)
    unit = CoverageAudit(frozenset({"a", "b"}), frozenset(), 2)

    merged = vector.merge(unit)

    assert merged == CoverageAudit(frozenset({"a", "b"}), frozenset({"a"}), 2)
    assert unit.merge(vector) == merged


def test_audit_rows_marks_an_identity_defective_when_any_row_refuses_it() -> None:
    rows = [
        {"uuid": "a", "valid": True},
        {"uuid": "b", "valid": False},
        {"uuid": "a", "valid": False},
    ]

    assert audit_rows(rows) == CoverageAudit(
        frozenset({"a", "b"}), frozenset({"a", "b"}), 0
    )


def test_audit_rows_counts_non_string_identities_as_unidentified() -> None:
    rows: list[dict[str, Any]] = [
        {"uuid": None, "valid": False},
        {"uuid": 7, "valid": True},
        {"uuid": "a", "valid": True},
    ]

    assert audit_rows(rows) == CoverageAudit(frozenset({"a"}), frozenset(), 2)


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {"uuid": "a", "valid": True},
        (),
        [["a", True]],
        [{"uuid": "a"}],
        [{"valid": True}],
        [{"uuid": "a", "valid": True, "extra": 1}],
    ],
    ids=[
        "none",
        "mapping",
        "tuple",
        "list-row",
        "missing-valid",
        "missing-uuid",
        "extra-column",
    ],
)
def test_audit_rows_refuses_any_shape_it_does_not_know(rows: object) -> None:
    with pytest.raises(FactVectorError, match="fact_vector_rebuild_required"):
        audit_rows(rows)


# --- the CLI command ----------------------------------------------------------------


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


def _stub_index(monkeypatch: pytest.MonkeyPatch, index: MemoryIndex) -> None:
    monkeypatch.setattr(
        "cairn.runtime.cli._default_index", lambda *args, **kwargs: (index, None)
    )


def test_the_cli_dry_run_reports_targets_and_writes_no_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(_config(tmp_path))
    _ingest(tmp_path, "cli fact")
    _drain(tmp_path)
    _stub_index(monkeypatch, _Auditing())
    config_path = _write_config_file(tmp_path)

    exit_code = main(["repair-index", "--config", str(config_path), "--dry-run"])

    # A dry run that owes work is partial by the same rule as a real one.
    assert exit_code == 4
    assert json.loads(_stdout(capsys)) == {
        "affected": 1,
        "delivered": 0,
        "dry_run": True,
        "enqueued": 0,
        "instance_id": str(INSTANCE_ID),
        "remaining": 0,
        "status": "partial",
        "targets": 1,
        "unrepairable": 0,
    }
    assert _depth(tmp_path) == 0


def test_the_cli_dry_run_counts_a_fact_already_left_to_its_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an interrupted repair the missing fact still has its row, so no
    new row is owed, but coverage is not restored: the dry run is partial."""
    _seed(_config(tmp_path))
    _ingest(tmp_path, "queued fact")
    _stub_index(monkeypatch, _Auditing())
    config_path = _write_config_file(tmp_path)

    exit_code = main(["repair-index", "--config", str(config_path), "--dry-run"])

    assert exit_code == 4
    payload = json.loads(_stdout(capsys))
    assert payload["status"] == "partial"
    assert payload["affected"] == 1
    assert payload["targets"] == 0
    assert payload["enqueued"] == 0
    assert _depth(tmp_path) == 1


def test_the_cli_repairs_drains_and_exits_ok(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(_config(tmp_path))
    fact = _ingest(tmp_path, "cli fact")
    _drain(tmp_path)
    index = _Auditing()
    _stub_index(monkeypatch, index)
    config_path = _write_config_file(tmp_path)

    exit_code = main(["repair-index", "--config", str(config_path)])

    assert exit_code == 0
    payload = json.loads(_stdout(capsys))
    assert payload == {
        "affected": 1,
        "delivered": 1,
        "dry_run": False,
        "enqueued": 1,
        "instance_id": str(INSTANCE_ID),
        "remaining": 0,
        "status": "ok",
        "targets": 1,
        "unrepairable": 0,
    }
    assert index.projected == [fact]
    assert _depth(tmp_path) == 0


def test_the_cli_reports_a_leftover_as_partial(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusing index leaves the repair row in place, so the command says
    the work is not done rather than reporting a repair it did not make."""
    _seed(_config(tmp_path))
    _ingest(tmp_path, "cli fact")
    _drain(tmp_path)
    _stub_index(monkeypatch, _Auditing(refuse=True))
    config_path = _write_config_file(tmp_path)

    exit_code = main(["repair-index", "--config", str(config_path)])

    assert exit_code == 4
    payload = json.loads(_stdout(capsys))
    assert payload["status"] == "partial"
    assert payload["enqueued"] == 1
    assert payload["delivered"] == 0
    assert payload["remaining"] == 1
    assert _depth(tmp_path) == 1


def test_the_cli_refuses_an_index_that_cannot_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(_config(tmp_path))
    _stub_index(monkeypatch, MemoryIndex())
    config_path = _write_config_file(tmp_path)

    exit_code = main(["repair-index", "--config", str(config_path)])

    assert exit_code == 4
    assert json.loads(_stderr(capsys))["code"] == "repair_unsupported"


def test_the_cli_refuses_when_the_index_is_disabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_config(tmp_path, graphiti=False))
    config_path = _write_config_file(tmp_path, graphiti=False)

    exit_code = main(["repair-index", "--config", str(config_path)])

    assert exit_code == 4
    assert json.loads(_stderr(capsys))["code"] == "retrieval_index_disabled"


def test_the_cli_refuses_while_an_instance_holds_the_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_config(tmp_path))
    config_path = _write_config_file(tmp_path)
    holder = DataDirectoryLease(tmp_path, INSTANCE_ID)
    holder.acquire()
    try:
        exit_code = main(["repair-index", "--config", str(config_path)])
    finally:
        holder.release()

    assert exit_code == 4
    assert json.loads(_stderr(capsys))["code"] == "already_locked"
