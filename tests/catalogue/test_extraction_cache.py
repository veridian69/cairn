"""Tests for the P-90 projection cache store."""

import json
import logging
import sqlite3
import threading
from _thread import LockType
from contextlib import closing
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from cairn.catalogue.extraction_cache import ExtractionCacheStore
from cairn.catalogue.migration import migrate_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import CacheKind, configure_logging

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _store(tmp_path: Path) -> ExtractionCacheStore:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    return ExtractionCacheStore(
        tmp_path, writer_gate=threading.Lock(), clock=lambda: NOW
    )


def test_a_missing_key_reads_as_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get("absent") is None
    assert store.get_embedding("absent") is None


def test_a_put_row_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("key-1", "fact-1", '{"nodes": []}')
    assert store.get("key-1") == '{"nodes": []}'


def test_put_many_stores_every_row_in_one_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_many(
        [
            ("key-a", "fact-a", "payload-a"),
            ("key-b", "fact-b", "payload-b"),
        ]
    )
    assert store.get("key-a") == "payload-a"
    assert store.get("key-b") == "payload-b"


def test_put_many_with_no_rows_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_many([])


def test_a_repeated_put_keeps_the_first_row(tmp_path: Path) -> None:
    # Cache values are derivations of immutable inputs: two writers with
    # the same key computed the same class of answer, so first-write-wins
    # is correct and avoids churn under concurrent projection.
    store = _store(tmp_path)
    store.put("key-1", "fact-1", "first")
    store.put("key-1", "fact-1", "second")
    assert store.get("key-1") == "first"


def test_get_with_no_fact_id_ignores_the_row_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("key-1", "fact-1", "payload")
    assert store.get("key-1") == "payload"


def test_get_with_a_matching_fact_id_hits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("key-1", "fact-1", "payload")
    assert store.get("key-1", "fact-1") == "payload"


def test_two_facts_with_identical_bodies_both_store_rows(tmp_path: Path) -> None:
    # R3: the accepted key is content-scoped, so two distinct facts with
    # byte-identical bodies share a cache_key. Storage is fact-owned —
    # PRIMARY KEY (cache_key, fact_id) — so both facts keep their own
    # episode-owned payloads instead of the first permanently occupying
    # the key.
    store = _store(tmp_path)
    store.put("key-1", "fact-1", "payload-1")
    store.put("key-1", "fact-2", "payload-2")
    assert store.get("key-1", "fact-1") == "payload-1"
    assert store.get("key-1", "fact-2") == "payload-2"


def test_get_with_no_fact_id_reads_deterministically(tmp_path: Path) -> None:
    # R3: the shared timestamp path reads by cache_key alone; with
    # fact-owned rows that read must be deterministic — lowest fact_id —
    # not whichever row the engine happens to return.
    store = _store(tmp_path)
    store.put("key-1", "fact-b", "payload-b")
    store.put("key-1", "fact-a", "payload-a")
    assert store.get("key-1") == "payload-a"


def test_get_with_a_different_fact_id_is_a_miss(tmp_path: Path) -> None:
    # C1: the key is content-scoped, so two facts with identical bodies
    # can share a row; the stored fact_id is what tells them apart on
    # read.
    store = _store(tmp_path)
    store.put("key-1", "fact-1", "payload")
    assert store.get("key-1", "fact-2") is None


def test_embedding_rows_are_separate_from_extraction_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_embedding("key-1", "[0.25, -0.5]")
    assert store.get_embedding("key-1") == "[0.25, -0.5]"
    assert store.get("key-1") is None


def test_put_embedding_many_stores_every_row_in_one_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding_many(
        [
            ("key-a", "[0.1]"),
            ("key-b", "[0.2]"),
        ]
    )
    assert store.get_embedding("key-a") == "[0.1]"
    assert store.get_embedding("key-b") == "[0.2]"


def test_put_embedding_many_with_no_rows_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_embedding_many([])


def test_put_embedding_many_keeps_the_first_row_on_repeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_embedding_many([("key-a", "first")])
    store.put_embedding_many([("key-a", "second")])
    assert store.get_embedding("key-a") == "first"


def test_a_read_against_a_broken_catalogue_is_a_miss(tmp_path: Path) -> None:
    # Failure posture: reads never raise — a cache problem must not take
    # down the live path. No catalogue exists at this path at all.
    store = ExtractionCacheStore(tmp_path / "nowhere", writer_gate=threading.Lock())
    assert store.get("key-1") is None
    assert store.get_embedding("key-1") is None


def test_a_write_against_a_broken_catalogue_is_dropped(tmp_path: Path) -> None:
    store = ExtractionCacheStore(tmp_path / "nowhere", writer_gate=threading.Lock())
    store.put("key-1", "fact-1", "payload")
    store.put_many([("key-2", "fact-2", "payload")])
    store.put_embedding("key-3", "[]")


def test_a_failed_read_emits_only_a_safe_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # R4/I-32: cache diagnostics travel the safe stream as closed events;
    # no ordinary logger output, table name or exception text anywhere.
    stream = StringIO()
    logger = configure_logging(stream)
    store = ExtractionCacheStore(
        tmp_path / "nowhere", writer_gate=threading.Lock(), logger=logger
    )

    with caplog.at_level(logging.DEBUG):
        assert store.get("key-1") is None
        assert store.get_embedding("key-1") is None

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [
        (payload["event"], payload["cache_kind"], payload["cache_reason"])
        for payload in payloads
    ] == [
        ("projection_cache_miss", "extraction", "read_failed"),
        ("projection_cache_miss", "embedding", "read_failed"),
    ]
    assert "projection_extraction_cache" not in stream.getvalue()
    assert caplog.records == []


def test_a_dropped_write_emits_only_a_safe_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stream = StringIO()
    logger = configure_logging(stream)
    store = ExtractionCacheStore(
        tmp_path / "nowhere", writer_gate=threading.Lock(), logger=logger
    )

    with caplog.at_level(logging.DEBUG):
        store.put_many([("key-a", "fact-a", "pa"), ("key-b", "fact-b", "pb")])
        store.put_embedding("key-c", "[]")

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [
        (payload["event"], payload["cache_kind"], payload["cache_rows"])
        for payload in payloads
    ] == [
        ("projection_cache_write_dropped", "extraction", 2),
        ("projection_cache_write_dropped", "embedding", 1),
    ]
    assert caplog.records == []


def test_a_failure_reports_the_callers_cache_kind(tmp_path: Path) -> None:
    # R4 follow-up: seam 2 (edge timestamps) stores its rows in the
    # extraction table under a different key shape, so a kind derived
    # from the table alone would report every timestamp-cache failure as
    # `extraction` and `timestamps` could never appear on these events.
    # A caller that knows which cache it is names it.
    stream = StringIO()
    logger = configure_logging(stream)
    store = ExtractionCacheStore(
        tmp_path / "nowhere", writer_gate=threading.Lock(), logger=logger
    )

    assert store.get("key-1", kind=CacheKind.TIMESTAMPS) is None
    store.put("key-1", "fact-1", "payload", kind=CacheKind.TIMESTAMPS)

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(payload["event"], payload["cache_kind"]) for payload in payloads] == [
        ("projection_cache_miss", "timestamps"),
        ("projection_cache_write_dropped", "timestamps"),
    ]


def test_a_failure_falls_back_to_the_tables_own_kind(tmp_path: Path) -> None:
    # Seam 1 and seam 3 are the table's own cache: no override needed.
    stream = StringIO()
    logger = configure_logging(stream)
    store = ExtractionCacheStore(
        tmp_path / "nowhere", writer_gate=threading.Lock(), logger=logger
    )

    assert store.get("key-1") is None
    assert store.get_embedding("key-1") is None

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [payload["cache_kind"] for payload in payloads] == [
        "extraction",
        "embedding",
    ]


def test_a_store_without_a_logger_stays_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # No safe logger threaded (direct construction in tests): failure
    # posture is unchanged and nothing raw is emitted in its place.
    store = ExtractionCacheStore(tmp_path / "nowhere", writer_gate=threading.Lock())

    with caplog.at_level(logging.DEBUG):
        assert store.get("key-1") is None
        store.put("key-1", "fact-1", "payload")

    assert caplog.records == []


class _RecordingLock:
    """A duck-typed stand-in for ``threading.Lock``: ``_thread.LockType``
    is a C type and cannot be subclassed, so this is cast to it at each
    call site below — the store only ever uses it as a context manager
    (I3's writer_gate contract), which this satisfies structurally."""

    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def __enter__(self) -> "_RecordingLock":
        self.acquired += 1
        return self

    def __exit__(self, *args: object) -> None:
        self.released += 1


def test_writes_take_the_writer_gate(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    lock = _RecordingLock()
    store = ExtractionCacheStore(
        tmp_path, writer_gate=cast(LockType, lock), clock=lambda: NOW
    )

    store.put("key-1", "fact-1", "payload")
    assert (lock.acquired, lock.released) == (1, 1)

    store.put_many([("key-2", "fact-2", "payload")])
    assert (lock.acquired, lock.released) == (2, 2)

    store.put_embedding("key-3", "[]")
    assert (lock.acquired, lock.released) == (3, 3)

    store.put_embedding_many([("key-4", "[]"), ("key-5", "[]")])
    assert (lock.acquired, lock.released) == (4, 4)


def test_reads_do_not_take_the_writer_gate(tmp_path: Path) -> None:
    migrate_catalogue(_config(tmp_path), lambda: NOW)
    lock = _RecordingLock()
    store = ExtractionCacheStore(
        tmp_path, writer_gate=cast(LockType, lock), clock=lambda: NOW
    )

    store.get("absent")
    store.get_embedding("absent")

    assert (lock.acquired, lock.released) == (0, 0)


def test_construction_requires_a_writer_gate(tmp_path: Path) -> None:
    # R5: the store shares I-25's single in-process writer gate. An
    # optional gate was how the rebuild path ended up writing beside
    # CatalogueTransactions' lock instead of under it, so construction
    # without one refuses rather than silently running ungated.
    with pytest.raises(TypeError):
        cast(Any, ExtractionCacheStore)(tmp_path)


def test_embedding_repair_compares_exact_payload_and_updates_timestamp(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding_many([("same", "poison"), ("changed", "winner")])
    later = datetime(2026, 9, 10, 12, tzinfo=UTC)
    repairer = ExtractionCacheStore(
        tmp_path, writer_gate=threading.Lock(), clock=lambda: later
    )
    repairer._repair_embedding_many(
        [
            ("same", "poison", "replacement"),
            ("changed", "poison", "loser"),
            ("absent", "poison", "inserted"),
        ]
    )
    assert store.get_embedding("same") == "replacement"
    assert store.get_embedding("changed") == "winner"
    assert store.get_embedding("absent") == "inserted"
    with (
        closing(sqlite3.connect(tmp_path / "catalogue.sqlite3")) as connection,
        connection,
    ):
        rows = dict(
            connection.execute(
                "SELECT cache_key, created_at FROM projection_embedding_cache"
            )
        )
    assert rows["same"] == rows["absent"]
    assert rows["same"] != rows["changed"]
    # Ordinary insertion still cannot overwrite a repaired entry.
    store.put_embedding("same", "ordinary overwrite")
    assert store.get_embedding("same") == "replacement"


def test_embedding_repair_is_atomic_and_emits_only_safe_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_embedding_many([("first", "poison"), ("second", "poison")])
    stream = StringIO()
    repairer = ExtractionCacheStore(
        tmp_path, writer_gate=threading.Lock(), logger=configure_logging(stream)
    )
    with (
        closing(sqlite3.connect(tmp_path / "catalogue.sqlite3")) as connection,
        connection,
    ):
        connection.execute(
            "CREATE TRIGGER fail_second BEFORE UPDATE ON projection_embedding_cache "
            "WHEN NEW.cache_key = 'second' BEGIN SELECT RAISE(ABORT, 'private payload'); END"
        )
    repairer._repair_embedding_many(
        [("first", "poison", "valid"), ("second", "poison", "valid")]
    )
    assert store.get_embedding("first") == "poison"
    assert store.get_embedding("second") == "poison"
    event = json.loads(stream.getvalue())
    assert (event["event"], event["cache_kind"], event["cache_rows"]) == (
        "projection_cache_write_dropped",
        "embedding",
        2,
    )
    assert "private" not in stream.getvalue()


def test_embedding_repair_deduplicates_keys_before_the_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding("key", "poison")
    store._repair_embedding_many(
        [("key", "poison", "first"), ("key", "first", "second")]
    )
    assert store.get_embedding("key") == "first"


def test_embedding_repair_waits_for_writer_admission(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_embedding("key", "poison")
    entered = threading.Event()
    gate = threading.Lock()

    class Admission:
        def __enter__(self) -> None:
            entered.set()
            gate.acquire()

        def __exit__(self, *args: object) -> None:
            gate.release()

    repairer = ExtractionCacheStore(tmp_path, writer_gate=cast(LockType, Admission()))
    worker = threading.Thread(
        target=repairer._repair_embedding_many, args=([("key", "poison", "valid")],)
    )
    gate.acquire()
    try:
        worker.start()
        assert entered.wait(2)
        assert store.get_embedding("key") == "poison"
    finally:
        gate.release()
        worker.join(2)
    assert not worker.is_alive()
    assert store.get_embedding("key") == "valid"
