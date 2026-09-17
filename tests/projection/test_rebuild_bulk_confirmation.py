"""Bulk confirmation contracts using real disposable SQLite transactions."""

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from test_rebuild import (
    CORRELATION_ID,
    CREDENTIAL_ID,
    JOB,
    NOW,
    PRINCIPAL_ID,
    REALM,
    _authority,
    _config,
    _ingest,
    _retrieve,
    _seed,
    _write_config_file,
)

from cairn.authority.gate import Actor
from cairn.authority.retrieval import Retrieve
from cairn.catalogue.audit import Scope, TrustClass
from cairn.catalogue.sqlite import _open_write_connection, read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactionError,
    CatalogueTransactions,
    CommitAmbiguity,
    FailureCode,
    Rejected,
)
from cairn.projection.adapter import FactProjected, ProjectedFactState, ProjectionFailed
from cairn.projection.memory import MemoryIndex
from cairn.projection.rebuild import rebuild_index
from cairn.runtime.cli import main


def _seed_facts(path: Path, count: int) -> None:
    _seed(_config(path))
    base = _ingest(path, "fact")
    with _open_write_connection(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for number in range(1, count):
            connection.execute(
                "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
                "classification, assertion_id, recorded_at) SELECT ?, realm_id, "
                "scope_segments, body, trust, classification, assertion_id, recorded_at "
                "FROM facts WHERE fact_id = ?",
                (str(UUID(int=number, version=4)), str(base)),
            )
        connection.commit()


def _owed(path: Path) -> set[str]:
    with read_connection(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT fact_id FROM projection_outbox WHERE kind = 'fact-rebuild'"
            )
        }


class CommitProbe:
    def __init__(
        self,
        failure: type[BaseException] | None = None,
        *,
        persist_first: bool = False,
        fail_group: int = 1,
    ) -> None:
        self.previous: set[str] | None = None
        self.groups: list[set[str]] = []
        self.failure = failure
        self.persist_first = persist_first
        self.fail_group = fail_group

    def __call__(self, connection: sqlite3.Connection) -> None:
        current = {
            row[0]
            for row in connection.execute(
                "SELECT fact_id FROM projection_outbox WHERE kind = 'fact-rebuild'"
            )
        }
        if self.previous is not None:
            self.groups.append(self.previous - current)
            if self.failure is not None and len(self.groups) == self.fail_group:
                if self.persist_first:
                    connection.commit()
                raise self.failure()
        connection.commit()
        self.previous = current


class ResultsIndex(MemoryIndex):
    def __init__(self, path: Path, gate: threading.Lock, failures: set[int]) -> None:
        super().__init__()
        self.path = path
        self.gate = gate
        self.failures = failures
        self.seen: list[str] = []
        self.single: list[str] = []
        self.bulk_sizes: list[int] = []

    def _outside_gate(self) -> None:
        assert self.gate.acquire(blocking=False), "adapter called under writer gate"
        self.gate.release()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        self._outside_gate()
        assert _owed(self.path), "enqueue must commit before clear"
        super().clear(partition_keys)

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self._outside_gate()
        self.single.append(str(state.fact_id))
        return super().project(state)

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        self._outside_gate()
        self.bulk_sizes.append(len(states))
        results: list[FactProjected | ProjectionFailed] = []
        for state in states:
            position = len(self.seen)
            self.seen.append(str(state.fact_id))
            results.append(
                ProjectionFailed("fixture_failure")
                if position in self.failures
                else MemoryIndex.project(self, state)
            )
        return tuple(results)


@pytest.mark.parametrize(
    ("failures", "sizes"),
    [
        (set(), [3, 2]),
        ({0, 2}, [1, 2]),
        ({0, 1, 2, 3}, [1]),
        ({0, 1, 2, 3, 4}, []),
    ],
)
def test_valid_response_confirms_only_successes_in_one_transaction_per_chunk(
    tmp_path: Path, failures: set[int], sizes: list[int]
) -> None:
    _seed_facts(tmp_path, 5)
    gate = threading.Lock()
    probe = CommitProbe()
    transactions = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    index = ResultsIndex(tmp_path, gate, failures)
    report = rebuild_index(
        tmp_path, transactions, index, uuid_factory=uuid4, chunk_size=3
    )
    assert [len(group) for group in probe.groups] == sizes
    assert report.projected == 5 - len(failures)
    assert report.failed == len(failures)
    assert report.unreadable == 0
    assert _owed(tmp_path) == {index.seen[position] for position in failures}
    assert index.single == []


@pytest.mark.parametrize(("count", "sizes"), [(501, [500, 1]), (1001, [500, 500, 1])])
def test_library_chunk_above_configured_limit_splits_confirmation_only(
    tmp_path: Path, count: int, sizes: list[int]
) -> None:
    _seed_facts(tmp_path, count)
    gate = threading.Lock()
    probe = CommitProbe()
    index = ResultsIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    report = rebuild_index(tmp_path, store, index, uuid_factory=uuid4, chunk_size=count)
    assert report.projected == count
    assert [len(group) for group in probe.groups] == sizes
    assert set.union(*probe.groups) == set(index.seen)
    assert index.bulk_sizes == [count]
    assert not _owed(tmp_path)


@pytest.mark.parametrize("mode", ["default", "raised", "malformed"])
def test_default_and_demotion_keep_individual_transactions(
    tmp_path: Path, mode: str
) -> None:
    _seed_facts(tmp_path, 3)
    gate = threading.Lock()
    probe = CommitProbe()

    class DemotingIndex(ResultsIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            self._outside_gate()
            assert mode != "default", "default must not call bulk"
            if mode == "raised":
                raise RuntimeError("fixture bulk failure")
            # A tempting success prefix does not authorise any confirmation.
            return cast(
                tuple[FactProjected | ProjectionFailed, ...],
                (FactProjected(), None, FactProjected()),
            )

    index = DemotingIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    report = rebuild_index(
        tmp_path,
        store,
        index,
        uuid_factory=uuid4,
        chunk_size=1 if mode == "default" else 3,
    )
    assert report.projected == 3
    assert len(index.single) == 3
    assert [len(group) for group in probe.groups] == [1, 1, 1]


def test_unreadable_fact_retains_obligation_alongside_bulk_successes(
    tmp_path: Path,
) -> None:
    _seed_facts(tmp_path, 3)
    bad_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(
            "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, recorded_at) SELECT ?, realm_id, "
            '\'[{"id":1,"kind":7}]\', body, trust, classification, assertion_id, recorded_at '
            "FROM facts LIMIT 1",
            (bad_id,),
        )
    gate = threading.Lock()
    probe = CommitProbe()
    index = ResultsIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    report = rebuild_index(tmp_path, store, index, uuid_factory=uuid4, chunk_size=3)
    assert (report.projected, report.failed, report.unreadable) == (3, 0, 1)
    assert [len(group) for group in probe.groups] == [3]
    assert _owed(tmp_path) == {bad_id}


def test_large_library_interruption_can_retain_more_than_500_successes(
    tmp_path: Path,
) -> None:
    _seed_facts(tmp_path, 1001)
    gate = threading.Lock()
    probe = CommitProbe(KeyboardInterrupt, fail_group=2)
    index = ResultsIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    with pytest.raises(KeyboardInterrupt):
        rebuild_index(tmp_path, store, index, uuid_factory=uuid4, chunk_size=1001)
    assert index.bulk_sizes == [1001]
    assert [len(group) for group in probe.groups] == [500, 500]
    assert _owed(tmp_path) == set(index.seen[500:])
    assert len(_owed(tmp_path)) == 501


def test_confirmation_preserves_exact_other_kind_row(tmp_path: Path) -> None:
    _seed_facts(tmp_path, 3)
    gate = threading.Lock()
    custody: list[tuple[object, ...]] = []

    class OtherKindIndex(ResultsIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            result = super().project_many(states)
            # Insert after enqueue's intentionally unchanged supersession.
            with _open_write_connection(tmp_path, create=False) as connection:
                connection.execute(
                    "INSERT INTO projection_outbox "
                    "(work_id, kind, fact_id, mutation_id, created_at, attempts) "
                    "VALUES (?, 'fact-ingested', ?, ?, '2026-08-08T12:00:00.000000Z', 7)",
                    (str(uuid4()), str(states[0].fact_id), str(uuid4())),
                )
                custody.extend(
                    connection.execute(
                        "SELECT * FROM projection_outbox WHERE kind = 'fact-ingested'"
                    ).fetchall()
                )
            return result

    index = OtherKindIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4
    )
    assert (
        rebuild_index(
            tmp_path, store, index, uuid_factory=uuid4, chunk_size=3
        ).projected
        == 3
    )
    with read_connection(tmp_path) as connection:
        assert (
            connection.execute("SELECT * FROM projection_outbox").fetchall() == custody
        )
    assert len(custody) == 1


@pytest.mark.parametrize("count", [3, 502])
def test_second_delete_failure_rolls_back_group_not_earlier_group(
    tmp_path: Path, count: int
) -> None:
    _seed_facts(tmp_path, count)
    gate = threading.Lock()
    probe = CommitProbe()

    class FailingDeleteIndex(ResultsIndex):
        def project_many(
            self, states: tuple[ProjectedFactState, ...]
        ) -> tuple[FactProjected | ProjectionFailed, ...]:
            result = super().project_many(states)
            target = states[501 if count > 500 else 1].fact_id
            with _open_write_connection(tmp_path, create=False) as connection:
                connection.execute(
                    "CREATE TRIGGER fixture_second_delete BEFORE DELETE ON projection_outbox "
                    f"WHEN OLD.fact_id = '{target}' AND OLD.kind = 'fact-rebuild' "
                    "BEGIN SELECT RAISE(ABORT, 'fixture_second_delete'); END"
                )
            return result

    index = FailingDeleteIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    with pytest.raises(sqlite3.IntegrityError, match="fixture_second_delete"):
        rebuild_index(tmp_path, store, index, uuid_factory=uuid4, chunk_size=count)
    confirmed = 500 if count > 500 else 0
    assert _owed(tmp_path) == set(index.seen[confirmed:])
    assert [len(group) for group in probe.groups] == ([500] if confirmed else [])
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("DROP TRIGGER fixture_second_delete")
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failure", [KeyboardInterrupt, CommitAmbiguity])
@pytest.mark.parametrize("persist_first", [False, True])
def test_commit_interruption_observes_actual_persistence_and_restart_heals(
    tmp_path: Path, failure: type[BaseException], persist_first: bool
) -> None:
    _seed_facts(tmp_path, 4)
    gate = threading.Lock()
    probe = CommitProbe(failure, persist_first=persist_first, fail_group=2)
    index = ResultsIndex(tmp_path, gate, set())
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    expected_error = (
        CatalogueTransactionError if failure is CommitAmbiguity else failure
    )
    with pytest.raises(expected_error) as caught:
        rebuild_index(tmp_path, store, index, uuid_factory=uuid4, chunk_size=2)
    if isinstance(caught.value, CatalogueTransactionError):
        assert caught.value.code == "commit_outcome_unknown"
    # First group really committed. Second group's exception is not evidence
    # that its DELETEs were rolled back: inspect through a fresh connection.
    assert _owed(tmp_path) == (set() if persist_first else set(index.seen[2:]))
    assert [len(group) for group in probe.groups] == [2, 2]
    if not persist_first:
        for as_of in (None, NOW):
            outcome = _authority(
                tmp_path, now=NOW + timedelta(hours=1), index=index
            ).retrieve(
                Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID),
                Retrieve(
                    scope=Scope(REALM, (JOB,)),
                    query="fact",
                    budget=1000000,
                    trust_filters=frozenset({TrustClass.CANDIDATE}),
                    as_of=as_of,
                ),
                correlation_id=CORRELATION_ID,
            )
            assert isinstance(outcome, Rejected)
            assert outcome.failure.code is FailureCode.INDEX_PENDING
    # A new transaction owner/serving deliverer, not another clear/rebuild.
    from cairn.projection.delivery import deliver_projection_outbox

    fresh_store = CatalogueTransactions(
        tmp_path, writer_gate=threading.Lock(), clock=lambda: NOW, uuid_factory=uuid4
    )
    deliver_projection_outbox(fresh_store, index, clock=lambda: NOW)
    assert not _owed(tmp_path)
    assert len(_retrieve(tmp_path, index, now=NOW + timedelta(hours=1)).hits) == 4


@pytest.mark.parametrize("persist_first", [False, True])
def test_real_cli_reports_ambiguous_confirmation_as_error_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    persist_first: bool,
) -> None:
    _seed_facts(tmp_path, 3)
    config_file = _write_config_file(tmp_path)
    config_file.write_text(config_file.read_text() + "\ndelivery:\n  chunk_size: 3\n")
    gate = threading.Lock()
    index = ResultsIndex(tmp_path, gate, set())
    probe = CommitProbe(CommitAmbiguity, persist_first=persist_first)
    store = CatalogueTransactions(
        tmp_path, writer_gate=gate, clock=lambda: NOW, uuid_factory=uuid4, commit=probe
    )
    monkeypatch.setattr(
        "cairn.runtime.cli._default_index", lambda *args, **kwargs: (index, None)
    )
    monkeypatch.setattr(
        "cairn.runtime.cli.CatalogueTransactions", lambda *args, **kwargs: store
    )
    assert main(["rebuild-index", "--config", str(config_file)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "catalogue_unavailable"
    assert _owed(tmp_path) == (set() if persist_first else set(index.seen))
