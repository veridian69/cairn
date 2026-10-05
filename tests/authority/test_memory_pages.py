"""Initial recall-page selection and page assembly over real catalogues."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import test_retrieval as support
from test_memory import _memory, _seed
from test_retrieval import (
    _AGENT_ID,
    _CORRELATION_ID,
    _DATA_GRANT_ID,
    _NOW,
    _OUTSIDER_ID,
    _SCOPE,
    _SECOND_GRANT_ID,
    _TASK,
    _agent_actor,
    _authority,
    _ingest_facts,
    _ingest_with_evidence,
    _insert_grant,
    _last_realm_event,
    _outsider_actor,
    _seed_outsider,
)
from test_semantic_ranking import (
    NOW as GRADED_NOW,
)
from test_semantic_ranking import (
    QUERY,
    EvidenceSource,
    corrupt,
    graded_memory,
    packet,
    required_module,
    seed_state,
)
from test_source_time import _ingest, _promote_with_restricted_evidence

from cairn.authority.gate import Actor
from cairn.authority.memory import _json
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_page_types import (
    ContinuationUnavailable,
    Order,
    PageBudgetTooSmall,
    PageRejected,
    RecallContinue,
    RecallPage,
    RecallPageResult,
    TimeBasis,
)
from cairn.authority.memory_pages import CairnMemoryPages
from cairn.authority.memory_types import (
    GRADED_POLICY,
    RELEVANT_POLICY,
    SEMANTIC_UNAVAILABLE,
    Disagree,
    Recall,
    RecallResult,
)
from cairn.authority.mutations import InvalidateFacts, PromoteFacts
from cairn.authority.recall_snapshots import TTL, Snapshot, SnapshotStore
from cairn.catalogue.audit import Classification, Outcome, Scope
from cairn.catalogue.sqlite import _open_write_connection, read_connection
from cairn.catalogue.transactions import (
    Committed,
    FailureCode,
    Rejected,
    RetryClass,
)

# Grants are created at _NOW, so fixtures ingest at or after _NOW and read at _AT.
_AT = _NOW + timedelta(days=1)


def _pages(
    path: Path, store: SnapshotStore | None = None, *, now: datetime = _AT
) -> CairnMemoryPages:
    return CairnMemoryPages(_memory(path, now=now), store or SnapshotStore())


def _page(
    path: Path,
    command: RecallPage,
    store: SnapshotStore | None = None,
    *,
    now: datetime = _AT,
) -> RecallPageResult:
    result = _pages(path, store, now=now).recall_page(
        _agent_actor(), command, correlation_id=_CORRELATION_ID
    )
    assert isinstance(result, RecallPageResult), result
    return result


def _ids(result: RecallPageResult) -> list[UUID]:
    return [hit.memory.fact.fact_id for hit in result.hits]


def test_relevance_matches_legacy_order_and_excludes_recency_only(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    old = _ingest_facts(tmp_path, bodies=("quartz calibration approach",))[0]
    later = _NOW + timedelta(days=100)
    _ingest_facts(tmp_path, bodies=("unrelated lunch",), now=later)
    result = _page(tmp_path, RecallPage(_SCOPE, "quartz calibration"), now=later)
    assert _ids(result) == [old]
    assert result.ordering.time_basis is None
    assert result.hits[0].ordering_time_basis is None


def test_ungraded_relevance_matches_legacy_order_and_scores(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("alpha", "alpha beta", "tie one", "tie two"))
    _ingest_facts(tmp_path, bodies=("alpha beta gamma",), now=_NOW + timedelta(hours=1))
    _ingest_facts(tmp_path, bodies=("unrelated",), now=_NOW + timedelta(hours=2))
    query = "alpha beta gamma tie"
    legacy = _memory(tmp_path, now=_AT).recall(
        _agent_actor(),
        Recall(_SCOPE, query, relevant_only=False),
        correlation_id=_CORRELATION_ID,
    )
    result = _page(tmp_path, RecallPage(_SCOPE, query, relevant_only=False))
    assert isinstance(legacy, RecallResult) and len(legacy.hits) == 6
    assert _ids(result) == [h.fact.fact_id for h in legacy.hits]
    assert [h.memory for h in result.hits] == list(legacy.hits)
    assert result.policy == legacy.policy


def test_newest_selects_from_the_whole_match_set_before_limits(tmp_path: Path) -> None:
    _seed(tmp_path)
    for day in range(30):
        _ingest_facts(
            tmp_path, bodies=(f"deploy note {day}",), now=_NOW + timedelta(days=day)
        )
    newest = _ingest_facts(
        tmp_path, bodies=("deploy note newest",), now=_NOW + timedelta(days=40)
    )[0]
    at = _NOW + timedelta(days=41)
    result = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=3),
        now=at,
    )
    assert _ids(result)[0] == newest and len(result.hits) == 3
    assert result.facts_remaining and result.next_cursor is not None


def test_source_order_uses_observed_time_not_import_time(tmp_path: Path) -> None:
    _seed(tmp_path)
    ancient, _ = _ingest(
        tmp_path, "release plan alpha", observed_at=_NOW - timedelta(days=900)
    )
    recent, _ = _ingest(
        tmp_path, "release plan beta", observed_at=_NOW - timedelta(days=2)
    )
    undated, _ = _ingest(tmp_path, "release plan gamma", observed_at=None)
    result = _page(tmp_path, RecallPage(_SCOPE, "release plan", Order.NEWEST))
    assert _ids(result) == [recent, ancient, undated]
    assert [h.source_time_status.value for h in result.hits] == [
        "available",
        "available",
        "unavailable",
    ]


def test_whole_record_prefix_stops_and_never_skips(tmp_path: Path) -> None:
    _seed(tmp_path)
    small = _ingest_facts(
        tmp_path, bodies=("budget x",), now=_NOW + timedelta(minutes=2)
    )[0]
    _ingest_facts(
        tmp_path, bodies=("budget " + "y" * 3000,), now=_NOW + timedelta(minutes=1)
    )
    _ingest_facts(tmp_path, bodies=("budget z",), now=_NOW)
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "budget", Order.NEWEST, TimeBasis.RECORDED, budget=1500),
    )
    assert _ids(first) == [small]
    assert first.budget_exhausted and first.facts_remaining and first.next_cursor


def test_first_record_too_large_is_refused_with_exact_minimum(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("budget " + "y" * 3000,))
    store = SnapshotStore()
    result = _pages(tmp_path, store).recall_page(
        _agent_actor(),
        RecallPage(_SCOPE, "budget", budget=200),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, PageRejected)
    assert isinstance(result.detail, PageBudgetTooSmall)
    assert result.failure.code.value == "invalid_request"
    assert 200 < result.detail.minimum_budget <= 1_048_576
    assert store.usage() == (0, 0)
    retry = _page(
        tmp_path, RecallPage(_SCOPE, "budget", budget=result.detail.minimum_budget)
    )
    assert len(retry.hits) == 1


def test_single_page_result_allocates_no_snapshot(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("solo memory",))
    store = SnapshotStore()
    result = _page(tmp_path, RecallPage(_SCOPE, "solo"), store)
    assert result.next_cursor is None and result.snapshot_expires_at is None
    assert not result.facts_remaining and store.usage() == (0, 0)
    assert result.snapshot_created_at == _AT


def test_budget_consumed_is_canonical_sum_of_disclosed_records(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("ünïcödé memory", "plain memory"))
    result = _page(tmp_path, RecallPage(_SCOPE, "memory"))
    disclosed = [*result.hits, *result.disagreements, *result.resolutions]
    assert result.budget_consumed == sum(len(_json(memory_value(r))) for r in disclosed)


def test_explicit_time_basis_with_relevance_is_refused(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _pages(tmp_path).recall_page(
        _agent_actor(),
        RecallPage(_SCOPE, "x", Order.RELEVANCE, TimeBasis.SOURCE),
        correlation_id=_CORRELATION_ID,
    )
    assert (
        isinstance(result, Rejected) and result.failure.code.value == "invalid_request"
    )


def test_newest_spans_every_ancestor_partition(tmp_path: Path) -> None:
    _seed(tmp_path)
    root = Scope(_SCOPE.realm, ())
    _ingest_facts(tmp_path, bodies=("deploy root old",), scope=root)
    _ingest_facts(tmp_path, bodies=("deploy job mid",), now=_NOW + timedelta(minutes=1))
    newest = _ingest_facts(
        tmp_path,
        bodies=("deploy root new",),
        scope=root,
        now=_NOW + timedelta(minutes=2),
    )[0]
    result = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=1),
    )
    assert _ids(result) == [newest]


def test_disagreement_across_a_page_boundary_is_flagged_not_dropped(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left = _ingest_facts(
        tmp_path, bodies=("port is 8123",), now=_NOW + timedelta(minutes=1)
    )[0]
    right = _ingest_facts(tmp_path, bodies=("port is 9000",))[0]
    # disagree needs both facts recorded at or before its clock (F3).
    outcome = _memory(tmp_path, now=_AT).disagree(
        _agent_actor(),
        Disagree(_SCOPE, left, right, Classification.INTERNAL, "ports differ"),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    split = _page(
        tmp_path,
        RecallPage(_SCOPE, "port", Order.NEWEST, TimeBasis.RECORDED, limit=1),
    )
    assert split.hits[0].memory.has_disagreement
    assert split.hits[0].memory.disagreement_context_incomplete
    assert split.context_incomplete and split.disagreements == ()
    together = _page(
        tmp_path,
        RecallPage(_SCOPE, "port", Order.NEWEST, TimeBasis.RECORDED, limit=2),
    )
    assert len(together.disagreements) == 1 and not together.context_incomplete


def test_audit_failure_releases_the_unpublished_snapshot(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("audit a", "audit b", "audit c"))
    store = SnapshotStore()
    pages = _pages(tmp_path, store)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    pages._memory._audit_read = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        pages.recall_page(
            _agent_actor(),
            RecallPage(_SCOPE, "audit", limit=1),
            correlation_id=_CORRELATION_ID,
        )
    assert store.usage() == (0, 0)


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_bounds_are_refused(tmp_path: Path, limit: int) -> None:
    _seed(tmp_path)
    result = _pages(tmp_path).recall_page(
        _agent_actor(),
        RecallPage(_SCOPE, "x", limit=limit),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)


def test_selection_horizon_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.authority.memory_pages as pages

    monkeypatch.setattr(pages, "SNAPSHOT_CAPACITY", 3)
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=tuple(f"horizon {i}" for i in range(5)))
    store = SnapshotStore()
    result = _page(tmp_path, RecallPage(_SCOPE, "horizon", limit=2), store)
    assert not result.selection_complete
    assert result.next_cursor is not None
    located = store.resolve(result.next_cursor, _AT)
    assert located is not None
    snapshot, position = located
    assert position == 2
    assert len(snapshot.fact_ids) == len(snapshot.scores) == 3
    assert len(snapshot.source_available) == 3


def test_published_snapshot_freezes_scores_and_source_projection(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    ancient, _ = _ingest(
        tmp_path, "release plan alpha", observed_at=_NOW - timedelta(days=900)
    )
    recent, _ = _ingest(
        tmp_path, "release plan beta", observed_at=_NOW - timedelta(days=2)
    )
    undated, _ = _ingest(tmp_path, "release plan gamma", observed_at=None)
    store = SnapshotStore()
    result = _page(
        tmp_path, RecallPage(_SCOPE, "release plan", Order.NEWEST, limit=1), store
    )
    assert result.next_cursor is not None and result.selection_complete
    assert result.snapshot_created_at == _AT
    assert result.snapshot_expires_at == _AT + TTL
    located = store.resolve(result.next_cursor, _AT)
    assert located is not None
    snapshot, position = located
    assert position == 1
    assert (
        snapshot.created_at == _AT and snapshot.binding.time_basis is TimeBasis.SOURCE
    )
    assert snapshot.fact_ids == (recent, ancient, undated)
    assert snapshot.source_available == (True, True, False)
    assert snapshot.scores[0] == result.hits[0].memory.relevance_score


def test_recall_page_always_opens_one_read_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.authority.memory_pages as pages

    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("snapshot view",))
    statements: list[str] = []
    opened: list[sqlite3.Connection] = []

    @contextmanager
    def tracked(path: Path) -> Iterator[sqlite3.Connection]:
        with read_connection(path) as connection:
            opened.append(connection)
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(pages, "read_connection", tracked)
    service = _pages(tmp_path)
    assert service._memory._semantic_evidence is None
    result = service.recall_page(
        _agent_actor(), RecallPage(_SCOPE, "snapshot"), correlation_id=_CORRELATION_ID
    )
    assert isinstance(result, RecallPageResult)
    assert len(opened) == 1 and statements[0] == "BEGIN"
    assert statements.count("BEGIN") == 1


@pytest.mark.parametrize("kind", ["distinct", "tie"])
def test_graded_relevance_matches_legacy_order_and_frozen_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    if kind == "tie":
        # Equal integer units: legacy breaks the tie by recency, then UUID.
        grades = document.partitions[1].grades
        document = replace(
            document,
            partitions=(
                document.partitions[0],
                replace(
                    document.partitions[1],
                    grades=(
                        replace(grades[0], score=0.8000001),
                        replace(grades[1], score=0.8000002),
                    ),
                ),
            ),
        )
    source = EvidenceSource(document)
    service = graded_memory(tmp_path, source)
    legacy = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, relevant_only=False, budget=65536),
        correlation_id=support._CORRELATION_ID,
    )
    result = CairnMemoryPages(service, SnapshotStore()).recall_page(
        support._agent_actor(),
        RecallPage(support._SCOPE, QUERY, relevant_only=False, budget=65536),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(legacy, RecallResult) and isinstance(result, RecallPageResult)
    assert _ids(result) == [h.fact.fact_id for h in legacy.hits]
    assert _ids(result) == ([old, recent] if kind == "distinct" else [recent, old])
    assert [h.memory for h in result.hits] == list(legacy.hits)
    assert result.policy == GRADED_POLICY and not result.semantic_degraded
    assert source.calls == 2


@pytest.mark.parametrize("failure", ["timeout", "coverage", "eligible-body"])
def test_semantic_failure_keeps_marked_lexical_fallback_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    if failure == "eligible-body":
        stale = replace(document.partitions[1].grades[0], fingerprint="0" * 64)
        document = replace(
            document,
            partitions=(
                document.partitions[0],
                replace(
                    document.partitions[1],
                    grades=(stale, document.partitions[1].grades[1]),
                ),
            ),
        )
    elif failure == "coverage":
        document = corrupt(document, failure)

    class FailingSource(EvidenceSource):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            found = super().search_with_evidence(query, limit, partition_keys)
            if failure == "timeout":
                raise TimeoutError("private provider explanation")
            return found

    class NoLegacy(support._ScriptedIndex):
        attempts = 0

        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            self.attempts += 1
            return (old, recent)

    source = FailingSource(document)
    legacy = NoLegacy()
    service = graded_memory(tmp_path, source)
    service._index = legacy
    command = RecallPage(support._SCOPE, QUERY, relevant_only=False)
    result = CairnMemoryPages(service, SnapshotStore()).recall_page(
        support._agent_actor(), command, correlation_id=support._CORRELATION_ID
    )
    baseline = CairnMemoryPages(
        _memory(tmp_path, now=GRADED_NOW), SnapshotStore()
    ).recall_page(
        support._agent_actor(), command, correlation_id=support._CORRELATION_ID
    )
    assert isinstance(result, RecallPageResult)
    assert isinstance(baseline, RecallPageResult)
    assert result.hits == baseline.hits
    assert result.budget_consumed == baseline.budget_consumed
    assert not baseline.semantic_degraded
    assert result.semantic_degraded
    assert result.policy == baseline.policy + SEMANTIC_UNAVAILABLE
    assert "private" not in repr(result)
    assert source.calls == 1 and legacy.attempts == 0


def test_relevant_only_fallback_policy_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")

    class Timeout(EvidenceSource):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            super().search_with_evidence(query, limit, partition_keys)
            raise TimeoutError

    service = graded_memory(tmp_path, Timeout(packet(api, old, recent)))
    result = CairnMemoryPages(service, SnapshotStore()).recall_page(
        support._agent_actor(),
        RecallPage(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallPageResult)
    assert result.policy == RELEVANT_POLICY + SEMANTIC_UNAVAILABLE
    assert result.semantic_degraded


def _continue(
    path: Path,
    store: SnapshotStore,
    cursor: str | None,
    *,
    budget: int = 16384,
    limit: int = 20,
    actor: Actor | None = None,
    scope: Scope = _SCOPE,
    now: datetime = _AT,
) -> RecallPageResult | Rejected | PageRejected:
    assert cursor is not None
    return _pages(path, store, now=now).recall_continue(
        actor or _agent_actor(),
        RecallContinue(scope, cursor, budget, limit),
        correlation_id=_CORRELATION_ID,
    )


def _deploy_notes(path: Path, count: int = 7) -> list[UUID]:
    return [
        _ingest_facts(
            path, bodies=(f"deploy note {i}",), now=_NOW + timedelta(minutes=i)
        )[0]
        for i in range(count)
    ]


def test_continuation_request_carries_exactly_scope_cursor_budget_limit() -> None:
    assert [f.name for f in fields(RecallContinue)] == [
        "scope",
        "cursor",
        "budget",
        "limit",
    ]


def test_pages_traverse_snapshot_without_loss_or_duplication(tmp_path: Path) -> None:
    _seed(tmp_path)
    expected = list(reversed(_deploy_notes(tmp_path)))
    store = SnapshotStore()
    page = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=3),
        store,
    )
    seen = _ids(page)
    while page.next_cursor is not None:
        result = _continue(tmp_path, store, page.next_cursor, limit=3)
        assert isinstance(result, RecallPageResult)
        page = result
        seen += _ids(page)
    assert seen == expected and not page.facts_remaining


def test_replayed_cursor_returns_same_page_and_interned_token(tmp_path: Path) -> None:
    _seed(tmp_path)
    _deploy_notes(tmp_path)
    store = SnapshotStore()
    first = _page(tmp_path, RecallPage(_SCOPE, "deploy", limit=2), store)
    a = _continue(tmp_path, store, first.next_cursor, limit=2)
    usage = store.usage()
    b = _continue(tmp_path, store, first.next_cursor, limit=2)
    assert isinstance(a, RecallPageResult) and isinstance(b, RecallPageResult)
    assert a.next_cursor is not None
    assert _ids(a) == _ids(b) and a.next_cursor == b.next_cursor
    assert store.usage() == usage


def test_invalidated_fact_is_omitted_on_continuation(tmp_path: Path) -> None:
    _seed(tmp_path)
    notes = list(reversed(_deploy_notes(tmp_path, 4)))
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=2),
        store,
    )
    invalidated = _authority(tmp_path, now=_AT).invalidate(
        _agent_actor(),
        InvalidateFacts((notes[2],), "wrong", None),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(invalidated, Committed)
    rest = _continue(tmp_path, store, first.next_cursor, limit=2)
    assert isinstance(rest, RecallPageResult)
    assert _ids(rest) == [notes[3]] and rest.next_cursor is None
    assert not rest.facts_remaining
    disclosed = [*rest.hits, *rest.disagreements, *rest.resolutions]
    assert rest.budget_consumed == sum(len(_json(memory_value(r))) for r in disclosed)


def test_foreign_wrong_scope_and_invented_cursors_are_indistinguishable(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _seed_outsider(tmp_path)
    _insert_grant(tmp_path, grant_id=_SECOND_GRANT_ID, principal_id=_OUTSIDER_ID)
    _deploy_notes(tmp_path)
    store = SnapshotStore()
    cursor = _page(tmp_path, RecallPage(_SCOPE, "deploy", limit=2), store).next_cursor
    refusals = [
        _continue(tmp_path, store, cursor, actor=_outsider_actor()),
        _continue(
            tmp_path,
            store,
            cursor,
            scope=Scope(_SCOPE.realm, (*_SCOPE.segments, _TASK)),
        ),
        _continue(tmp_path, store, "A" * 43),
        _continue(tmp_path, store, cursor, now=_AT + timedelta(seconds=301)),
    ]
    for refusal in refusals:
        assert isinstance(refusal, PageRejected)
        assert refusal.detail == ContinuationUnavailable()
        assert refusal.failure.code.value == "invalid_request"
        assert isinstance(refusals[0], PageRejected)
        assert refusal.failure.safe_message == refusals[0].failure.safe_message


def _downgrade_clearance(path: Path) -> None:
    with _open_write_connection(path, create=False) as connection:
        connection.execute(
            "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
            (
                str(_DATA_GRANT_ID),
                "2026-08-05T10:11:12.123456Z",
                str(_AGENT_ID),
                "test",
            ),
        )
        connection.commit()
    _insert_grant(
        path,
        grant_id=_SECOND_GRANT_ID,
        segments=(),
        read_clearance=Classification.INTERNAL,
    )


def test_source_order_continuation_refused_after_clearance_change(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _promote_with_restricted_evidence(tmp_path, _NOW - timedelta(days=5))
    _deploy_notes(tmp_path, 3)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(
            _SCOPE, "promotable deploy", Order.OLDEST, relevant_only=False, limit=1
        ),
        store,
    )
    assert first.next_cursor is not None
    _downgrade_clearance(tmp_path)
    refused = _continue(tmp_path, store, first.next_cursor)
    assert isinstance(refused, PageRejected)
    assert refused.detail == ContinuationUnavailable()


def test_ceiling_change_refuses_before_the_changed_fact_is_reached(
    tmp_path: Path,
) -> None:
    """R5: the ceiling binding refuses on its own; the availability bits only
    cover facts a page actually projects."""
    _seed(tmp_path)
    for day in (10, 9, 8):
        _ingest(tmp_path, f"deploy {day}", observed_at=_NOW - timedelta(days=day))
    _promote_with_restricted_evidence(tmp_path, _NOW - timedelta(days=5))
    store = SnapshotStore()
    query = RecallPage(
        _SCOPE, "promotable deploy", Order.OLDEST, relevant_only=False, limit=1
    )
    first = _page(tmp_path, query, store)
    assert first.next_cursor is not None
    unchanged = _continue(tmp_path, store, first.next_cursor, limit=1)
    assert isinstance(unchanged, RecallPageResult)
    _downgrade_clearance(tmp_path)
    refused = _continue(tmp_path, store, first.next_cursor, limit=1)
    assert isinstance(refused, PageRejected)
    assert refused.detail == ContinuationUnavailable()


def test_source_order_continuation_refused_when_clock_steps_back(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _deploy_notes(tmp_path, 3)
    store = SnapshotStore()
    first = _page(tmp_path, RecallPage(_SCOPE, "deploy", Order.OLDEST, limit=1), store)
    refused = _continue(
        tmp_path, store, first.next_cursor, now=_AT - timedelta(seconds=1)
    )
    assert isinstance(refused, PageRejected)
    assert refused.detail == ContinuationUnavailable()


def test_source_order_continuation_refused_when_an_origin_becomes_available(
    tmp_path: Path,
) -> None:
    """R5/F1 (both reviews): promotion admits named evidence without checking its
    recorded_at, so a promoted fact can cite evidence recorded after itself. At
    creation the origin is unavailable; later it becomes available. The spec refuses
    either direction of change, so the stored availability bit must trip."""
    _seed(tmp_path)
    t1, t2, t3, t4 = (_NOW + timedelta(minutes=m) for m in (1, 2, 3, 4))
    source, _ = _ingest(
        tmp_path, "release plan source", observed_at=_NOW - timedelta(days=3)
    )
    _, evidence = _ingest_with_evidence(tmp_path, bodies=("later proof",), now=t3)
    _ingest_facts(tmp_path, bodies=("release plan filler",), now=t1)
    promoted = _authority(tmp_path, now=t2).promote(
        _agent_actor(),
        PromoteFacts(
            (source,),
            evidence,
            Scope(_SCOPE.realm, ()),
            Classification.INTERNAL,
            "published",
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(promoted, Committed)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "release plan", Order.OLDEST, limit=1),
        store,
        now=t2,
    )
    assert first.next_cursor is not None
    refused = _continue(tmp_path, store, first.next_cursor, now=t4)
    assert isinstance(refused, PageRejected)
    assert refused.detail == ContinuationUnavailable()


def test_recorded_order_continuation_survives_clearance_change(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _deploy_notes(tmp_path, 4)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=2),
        store,
    )
    _downgrade_clearance(tmp_path)
    assert isinstance(_continue(tmp_path, store, first.next_cursor), RecallPageResult)


def test_too_small_continuation_keeps_cursor_usable(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("budget small",), now=_NOW + timedelta(minutes=1))
    _ingest_facts(tmp_path, bodies=("budget " + "y" * 3000,))
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "budget", Order.NEWEST, TimeBasis.RECORDED, limit=1),
        store,
    )
    small = _continue(tmp_path, store, first.next_cursor, budget=300)
    assert isinstance(small, PageRejected)
    assert isinstance(small.detail, PageBudgetTooSmall)
    retry = _continue(
        tmp_path, store, first.next_cursor, budget=small.detail.minimum_budget
    )
    assert isinstance(retry, RecallPageResult) and len(retry.hits) == 1


def test_continuation_never_calls_the_semantic_provider(tmp_path: Path) -> None:
    _seed(tmp_path)
    _deploy_notes(tmp_path)
    store = SnapshotStore()
    pages = _pages(tmp_path, store)
    first = pages.recall_page(
        _agent_actor(),
        RecallPage(_SCOPE, "deploy", limit=2),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(first, RecallPageResult) and first.next_cursor is not None

    class Exploding:
        def search_with_evidence(self, *args: object) -> object:
            raise AssertionError("continuation must not call the provider")

    pages._memory._semantic_evidence = Exploding()  # type: ignore[assignment]
    result = pages.recall_continue(
        _agent_actor(),
        RecallContinue(_SCOPE, first.next_cursor),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallPageResult)


def test_continuation_returns_frozen_scores_never_recomputed(tmp_path: Path) -> None:
    """R10: ungraded scores decay with age, so a recomputation would differ."""
    _seed(tmp_path)
    _deploy_notes(tmp_path, 4)
    store = SnapshotStore()
    first = _page(tmp_path, RecallPage(_SCOPE, "deploy", limit=2), store)
    assert first.next_cursor is not None
    located = store.resolve(first.next_cursor, _AT)
    assert located is not None
    snapshot, position = located
    later = _AT + timedelta(seconds=200)
    rest = _continue(tmp_path, store, first.next_cursor, now=later)
    assert isinstance(rest, RecallPageResult) and len(rest.hits) == 2
    frozen = [h.memory.relevance_score for h in rest.hits]
    assert frozen == list(snapshot.scores[position:])
    fresh = _page(tmp_path, RecallPage(_SCOPE, "deploy", limit=4), now=later)
    recomputed = {h.memory.fact.fact_id: h.memory.relevance_score for h in fresh.hits}
    assert all(recomputed[i] != s for i, s in zip(_ids(rest), frozen, strict=True))


class _VanishingStore(SnapshotStore):
    """Loses the snapshot just before the next position token is interned."""

    def token_for(self, snapshot: Snapshot, position: int, now: datetime) -> str | None:
        self.discard(snapshot)
        return super().token_for(snapshot, position, now)


def test_continuation_without_internable_cursor_never_claims_completion(
    tmp_path: Path,
) -> None:
    """R12: the page is returned, still reporting facts remaining."""
    _seed(tmp_path)
    _deploy_notes(tmp_path, 5)
    store = _VanishingStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=2),
        store,
    )
    result = _continue(tmp_path, store, first.next_cursor, limit=2)
    assert isinstance(result, RecallPageResult) and len(result.hits) == 2
    assert result.facts_remaining and result.next_cursor is None
    assert store.usage() == (0, 0)


def test_snapshot_capacity_refusal_is_audited_and_allocates_nothing(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _deploy_notes(tmp_path, 3)
    store = SnapshotStore(per_process=0)
    result = _pages(tmp_path, store).recall_page(
        _agent_actor(),
        RecallPage(_SCOPE, "deploy", limit=1),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected) and not isinstance(result, PageRejected)
    assert result.failure.code is FailureCode.DEPENDENCY_UNAVAILABLE
    assert result.failure.retry is RetryClass.AFTER_DELAY
    assert result.failure.correlation_id == _CORRELATION_ID
    event = _last_realm_event(tmp_path).draft
    assert event.action_code == "memory-recall-page"
    assert event.outcome is Outcome.DENY
    assert event.reason_code == "recall_page_capacity"
    assert event.requested_scope == _SCOPE
    assert store.usage() == (0, 0)


def _budget_notes(path: Path) -> tuple[UUID, UUID, UUID]:
    """Newest first under recorded order: small visible, large restricted, then a
    mid-sized visible fact."""
    mid = _ingest_facts(path, bodies=("budget " + "z" * 600,))[0]
    large = _ingest_facts(
        path,
        bodies=("budget " + "y" * 3000,),
        now=_NOW + timedelta(minutes=1),
        classification=Classification.RESTRICTED,
    )[0]
    small = _ingest_facts(path, bodies=("budget x",), now=_NOW + timedelta(minutes=2))[
        0
    ]
    return small, large, mid


def _hide(path: Path, facts: tuple[UUID, ...], how: str) -> None:
    """Hide restricted facts by a clearance downgrade, or invalidate them."""
    if how == "clearance":
        _downgrade_clearance(path)
        return
    invalidated = _authority(path, now=_AT).invalidate(
        _agent_actor(),
        InvalidateFacts(facts, "wrong", None),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(invalidated, Committed)


@pytest.mark.parametrize("how", ["clearance", "invalidated"])
def test_hidden_next_fact_does_not_set_minimum_budget(tmp_path: Path, how: str) -> None:
    _seed(tmp_path)
    small, large, mid = _budget_notes(tmp_path)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "budget", Order.NEWEST, TimeBasis.RECORDED, limit=1),
        store,
    )
    assert _ids(first) == [small] and first.next_cursor is not None
    visible = _continue(tmp_path, store, first.next_cursor, budget=300)
    assert isinstance(visible, PageRejected)
    assert isinstance(visible.detail, PageBudgetTooSmall)
    assert visible.detail.minimum_budget > 3000  # the large fact, while visible
    _hide(tmp_path, (large,), how)
    hidden = _continue(tmp_path, store, first.next_cursor, budget=300)
    assert isinstance(hidden, PageRejected)
    assert isinstance(hidden.detail, PageBudgetTooSmall)
    minimum = hidden.detail.minimum_budget
    retry = _continue(tmp_path, store, first.next_cursor, budget=minimum)
    assert isinstance(retry, RecallPageResult) and _ids(retry) == [mid]
    assert minimum == len(_json(memory_value(retry.hits[0]))) < 3000


def _ordered_notes(path: Path) -> list[UUID]:
    """Newest first under recorded order: two visible, then two restricted."""
    return list(
        reversed(
            [
                _ingest_facts(
                    path,
                    bodies=(f"deploy note {i}",),
                    now=_NOW + timedelta(minutes=i),
                    classification=(
                        Classification.RESTRICTED if i < 2 else Classification.INTERNAL
                    ),
                )[0]
                for i in range(4)
            ]
        )
    )


@pytest.mark.parametrize("how", ["clearance", "invalidated"])
def test_hidden_facts_after_the_limit_do_not_count_as_remaining(
    tmp_path: Path, how: str
) -> None:
    _seed(tmp_path)
    notes = _ordered_notes(tmp_path)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=1),
        store,
    )
    assert _ids(first) == notes[:1] and first.next_cursor is not None
    before = _continue(tmp_path, store, first.next_cursor, limit=1)
    assert isinstance(before, RecallPageResult) and before.facts_remaining
    _hide(tmp_path, tuple(notes[2:]), how)
    rest = _continue(tmp_path, store, first.next_cursor, limit=1)
    assert isinstance(rest, RecallPageResult) and _ids(rest) == [notes[1]]
    assert not rest.facts_remaining and rest.next_cursor is None


@pytest.mark.parametrize("how", ["clearance", "invalidated"])
@pytest.mark.parametrize("budget", [1, 16384])
def test_continuation_over_only_hidden_facts_is_complete(
    tmp_path: Path, how: str, budget: int
) -> None:
    _seed(tmp_path)
    notes = _ordered_notes(tmp_path)
    store = SnapshotStore()
    first = _page(
        tmp_path,
        RecallPage(_SCOPE, "deploy", Order.NEWEST, TimeBasis.RECORDED, limit=2),
        store,
    )
    assert _ids(first) == notes[:2] and first.next_cursor is not None
    _hide(tmp_path, tuple(notes[2:]), how)
    rest = _continue(tmp_path, store, first.next_cursor, budget=budget)
    assert isinstance(rest, RecallPageResult)
    assert rest.hits == () and rest.budget_consumed == 0
    assert not rest.facts_remaining and rest.next_cursor is None
