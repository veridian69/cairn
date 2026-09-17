"""Turn receipt journals expose bounded write outcomes without storing content."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client.conversation_sources import (
    HostSource,
    SourceBundle,
    create_source_bundle,
)
from cairn.client.profiles import MemoryProfile
from cairn.client.turn_receipts import ReceiptJournal, ReceiptJournalError, TurnMemory


def bundle(body: str = "A private host task.") -> SourceBundle:
    profile = MemoryProfile(
        "http://127.0.0.1:8123",
        uuid4(),
        Scope("synthetic", (ScopeSegment("job", "receipts"),)),
        Classification.INTERNAL,
        Path("/unused"),
        uuid4(),
    )
    return create_source_bundle(
        profile,
        expected_principal=uuid4(),
        sources=(HostSource(uuid4(), body),),
    )


def test_unstarted_is_unknown_and_started_empty_is_none(tmp_path: Path) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    assert ReceiptJournal.summarise(path, sources) == TurnMemory(status="unknown")

    ReceiptJournal.open(path, sources)
    assert ReceiptJournal.summarise(path, sources) == TurnMemory(status="none")


def test_unfinished_attempt_is_unknown_and_finished_write_is_verified(
    tmp_path: Path,
) -> None:
    sources = bundle("SECRET source body")
    source_id = sources.sources[0].source_id
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    sequence = journal.begin("remember", source_id)
    assert ReceiptJournal.summarise(path, sources) == TurnMemory(
        status="unknown", attempted=1
    )

    fact_id = uuid4()
    journal.finish(
        sequence,
        {
            "status": "verified",
            "stage": "complete",
            "mapping": {"fact_id": str(fact_id), "body": "SECRET saved body"},
            "remember_receipt": {"body": "SECRET receipt body"},
        },
    )
    summary = ReceiptJournal.summarise(path, sources)
    assert summary == TurnMemory(
        status="verified", fact_ids=(str(fact_id),), attempted=1
    )
    assert summary.public() == {
        "status": "verified",
        "fact_ids": [str(fact_id)],
        "attempted": 1,
    }
    persisted = path.read_text()
    assert "SECRET" not in persisted


def test_mixed_outcomes_keep_verified_ids_with_conservative_status(
    tmp_path: Path,
) -> None:
    sources = bundle()
    source_id = sources.sources[0].source_id
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    verified_id = uuid4()
    first = journal.begin("remember", source_id)
    journal.finish(
        first,
        {"status": "verified", "mapping": {"fact_id": str(verified_id)}},
    )
    second = journal.begin("replace", source_id)
    journal.finish(second, {"status": "partial", "mapping": None})
    assert ReceiptJournal.summarise(path, sources) == TurnMemory(
        status="partial", fact_ids=(str(verified_id),), attempted=2
    )

    journal.begin("remember", source_id)
    assert ReceiptJournal.summarise(path, sources) == TurnMemory(
        status="unknown", fact_ids=(str(verified_id),), attempted=3
    )


@pytest.mark.parametrize("case", ["missing", "corrupt", "wrong_sources"])
def test_untrusted_or_mismatched_journal_is_unknown(tmp_path: Path, case: str) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    if case != "missing":
        ReceiptJournal.create(path, sources)
        ReceiptJournal.open(path, sources)
    if case == "corrupt":
        path.write_text("not json")
    selected = bundle() if case == "wrong_sources" else sources
    assert ReceiptJournal.summarise(path, selected).status == "unknown"


def test_journal_rejects_unknown_sources_and_unsafe_files(tmp_path: Path) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    with pytest.raises(ReceiptJournalError):
        journal.begin("remember", uuid4())
    with pytest.raises(ReceiptJournalError):
        journal.begin("recall", sources.sources[0].source_id)

    path.chmod(0o644)
    with pytest.raises(ReceiptJournalError):
        ReceiptJournal.open(path, sources)
    assert ReceiptJournal.summarise(path, sources).status == "unknown"


def test_finish_accepts_only_once_and_snapshot_is_strict(tmp_path: Path) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    sequence = journal.begin("remember", sources.sources[0].source_id)
    journal.finish(sequence, {"status": "rejected", "error": {"body": "secret"}})
    with pytest.raises(ReceiptJournalError):
        journal.finish(sequence, {"status": "verified"})
    document = json.loads(path.read_text())
    assert set(document["operations"][0]["outcome"]) == {"status", "fact_ids"}
    assert ReceiptJournal.summarise(path, sources).status == "partial"


def test_separate_open_instances_cannot_lose_concurrent_intents(tmp_path: Path) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    first = ReceiptJournal.open(path, sources)
    second = ReceiptJournal.open(path, sources)
    source_id = sources.sources[0].source_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(first.begin, "remember", source_id),
            pool.submit(second.begin, "replace", source_id),
        )
        sequences = sorted(future.result() for future in futures)
    assert sequences == [1, 2]
    assert ReceiptJournal.summarise(path, sources).attempted == 2


def test_failed_begin_is_sticky_unknown_for_limit_and_io(tmp_path: Path) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    source_id = sources.sources[0].source_id
    for _ in range(32):
        sequence = journal.begin("remember", source_id)
        journal.finish(sequence, {"status": "verified", "mapping": None})
    assert ReceiptJournal.summarise(path, sources).status == "verified"
    with pytest.raises(ReceiptJournalError):
        journal.begin("remember", source_id)
    assert ReceiptJournal.summarise(path, sources).status == "unknown"

    other_sources = bundle()
    other_path = tmp_path / "other.json"
    ReceiptJournal.create(other_path, other_sources)
    other = ReceiptJournal.open(other_path, other_sources)
    other_path.chmod(0o644)
    with pytest.raises(ReceiptJournalError):
        other.begin("remember", other_sources.sources[0].source_id)
    other_path.chmod(0o600)
    assert ReceiptJournal.summarise(other_path, other_sources).status == "unknown"
    with pytest.raises(ReceiptJournalError):
        other.begin("remember", other_sources.sources[0].source_id)
    assert ReceiptJournal.summarise(other_path, other_sources).status == "unknown"


def test_interrupted_poison_marker_is_itself_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = bundle()
    path = tmp_path / "receipts.json"
    ReceiptJournal.create(path, sources)
    journal = ReceiptJournal.open(path, sources)
    real_fsync = os.fsync

    def interrupt_poison(descriptor: int) -> None:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o400:
            raise OSError("synthetic marker failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", interrupt_poison)
    with pytest.raises(ReceiptJournalError):
        journal.begin("remember", sources.sources[0].source_id)
    assert ReceiptJournal.summarise(path, sources).status == "unknown"
