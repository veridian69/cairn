"""Opt-in cue filtering over real catalogue facts; scripted index is not semantics."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from test_memory import _disagree, _memory, _seed
from test_retrieval import (
    _CORRELATION_ID,
    _NOW,
    _SCOPE,
    _SIBLING_JOB,
    _agent_actor,
    _ingest_facts,
    _ScriptedIndex,
)

from cairn.authority.memory_types import Recall, RecallResult
from cairn.catalogue.audit import Scope
from cairn.catalogue.transactions import Committed, FailureCode, Rejected


def test_opt_in_removes_same_scope_clutter_but_default_remains_unchanged(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    target = _ingest_facts(tmp_path, bodies=("quartz calibration baseline",))[0]
    clutter = _ingest_facts(
        tmp_path,
        bodies=tuple(f"maintenance rota cupboard {i}" for i in range(24)),
    )
    service = _memory(tmp_path)
    baseline = service.recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", budget=65536),
        correlation_id=_CORRELATION_ID,
    )
    filtered = service.recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", budget=65536, relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    explicit_false = service.recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", budget=65536, relevant_only=False),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(baseline, RecallResult)
    assert isinstance(filtered, RecallResult)
    assert baseline == explicit_false
    assert {hit.fact.fact_id for hit in baseline.hits} == {target, *clutter}
    assert baseline.hits[0].fact.fact_id == target
    assert all(hit.relevance_score == 0.25 for hit in baseline.hits[1:])
    assert [hit.fact.fact_id for hit in filtered.hits] == [target]
    assert filtered.hits[0] == baseline.hits[0]
    assert filtered.budget_consumed < baseline.budget_consumed
    assert not filtered.budget_exhausted
    assert filtered.policy != baseline.policy


@pytest.mark.parametrize(
    "query", ["unconnected nebula", "automobile servicing cadence", " !!! "]
)
def test_no_cue_returns_zero_without_claiming_budget_exhaustion(
    tmp_path: Path,
    query: str,
) -> None:
    _seed(tmp_path)
    _ingest_facts(
        tmp_path,
        bodies=("Vehicle maintenance occurs fortnightly.",)
        + tuple(f"cupboard rota {i}" for i in range(24)),
    )
    result = _memory(tmp_path).recall(
        _agent_actor(),
        Recall(_SCOPE, query, budget=1, relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert result.hits == ()
    assert result.disagreements == ()
    assert result.resolutions == ()
    assert result.budget_consumed == 0
    assert not result.budget_exhausted


def test_old_strong_cue_survives_filter_and_small_budget(tmp_path: Path) -> None:
    _seed(tmp_path)
    old = _ingest_facts(tmp_path, bodies=("amber calibration baseline",))[0]
    now = _NOW + timedelta(days=100)
    _ingest_facts(
        tmp_path,
        bodies=tuple(f"current maintenance rota {i}" for i in range(24)),
        now=now,
    )
    result = _memory(tmp_path, now=now).recall(
        _agent_actor(),
        Recall(_SCOPE, "AMBER calibration", budget=1000, relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [old]
    assert result.hits[0].fact.recorded_at == _NOW
    assert 2 < result.hits[0].relevance_score < 2.25
    assert not result.budget_exhausted


def test_controlled_index_membership_retains_old_no_overlap_match_and_reconciles_scope(
    tmp_path: Path,
) -> None:
    """This proves membership handling, not real semantic retrieval quality."""
    _seed(tmp_path)
    target = _ingest_facts(
        tmp_path, bodies=("Vehicle maintenance occurs fortnightly.",)
    )[0]
    sibling = _ingest_facts(
        tmp_path,
        bodies=("Unrelated sibling material",),
        scope=Scope(_SCOPE.realm, (_SIBLING_JOB,)),
    )[0]
    now = _NOW + timedelta(days=100)
    _ingest_facts(
        tmp_path,
        bodies=tuple(f"cupboard rota {i}" for i in range(24)),
        now=now,
    )
    service = _memory(tmp_path, now=now)
    service._index = _ScriptedIndex((sibling, UUID(int=999), target))
    result = service.recall(
        _agent_actor(),
        Recall(_SCOPE, "automobile servicing cadence", relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [target]
    assert 0.5 < result.hits[0].relevance_score < 0.75
    assert not result.semantic_degraded


def test_index_failure_keeps_lexical_cue_and_drops_uncued_fallback(
    tmp_path: Path,
) -> None:
    class UnavailableIndex(_ScriptedIndex):
        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            raise RuntimeError("synthetic unavailable index")

    _seed(tmp_path)
    target = _ingest_facts(tmp_path, bodies=("quartz calibration",))[0]
    _ingest_facts(tmp_path, bodies=("unrelated cupboard rota",))
    service = _memory(tmp_path)
    service._index = UnavailableIndex()
    result = service.recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [target]
    assert result.semantic_degraded


def test_filtered_disagreement_endpoint_still_reports_missing_context(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left = _ingest_facts(tmp_path, bodies=("archive retention seven days",))[0]
    right = _ingest_facts(tmp_path, bodies=("keep records for thirty days",))[0]
    assert isinstance(_disagree(tmp_path, left, right), Committed)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        Recall(_SCOPE, "archive retention", relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [left]
    assert result.hits[0].has_disagreement
    assert result.hits[0].disagreement_context_incomplete
    assert result.disagreements == ()


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_relevant_only_requires_an_actual_boolean(tmp_path: Path, value: Any) -> None:
    _seed(tmp_path)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", relevant_only=value),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST


def test_empty_query_is_still_rejected_with_filter_enabled(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        Recall(_SCOPE, "", relevant_only=True),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
