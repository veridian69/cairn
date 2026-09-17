"""Quality measurements must expose misses, clutter and missing context honestly."""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluate_memory_recall.py"


def evaluator() -> Any:
    assert SCRIPT.is_file(), "The runnable recall evaluation has not been implemented"
    spec = importlib.util.spec_from_file_location("recall_evaluation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metrics_count_unique_relevance_and_penalise_duplicate_clutter() -> None:
    metrics = evaluator().measure(["noise", "a", "a", "b"], {"a", "b"}, k=3)
    assert metrics == {
        "k": 3,
        "relevant_count": 2,
        "returned_count": 4,
        "recall_at_k": 0.5,
        "first_relevant_rank": 2,
        "reciprocal_rank_at_k": 0.5,
        "irrelevant_at_k": 1,
        "irrelevant_returned": 1,
        "duplicate_returned": 1,
    }


def test_metrics_do_not_award_recall_for_a_miss_or_empty_relevance() -> None:
    measure = evaluator().measure
    late = measure(["noise", "more", "target"], {"target"}, k=2)
    assert late["recall_at_k"] == 0
    assert late["reciprocal_rank_at_k"] == 0
    assert late["first_relevant_rank"] == 3
    missing = measure([], {"target"}, k=3)
    assert missing["recall_at_k"] == 0
    assert missing["first_relevant_rank"] is None
    empty = measure(["noise"], set(), k=3)
    assert empty["recall_at_k"] is None
    assert empty["reciprocal_rank_at_k"] is None
    assert empty["irrelevant_returned"] == 1
    with pytest.raises(ValueError, match="positive"):
        measure([], set(), k=0)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    evaluator()
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=SCRIPT.parent.parent,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


def test_real_app_report_exposes_paraphrase_and_clutter_limits(
    report: dict[str, Any],
) -> None:
    assert report["retrieval_mode"] == "lexical-only"
    assert report["semantic_evidence"] is False
    assert report["provider_calls"] == 0
    assert report["synthetic"] is True
    assert report["same_scope_distractors"] == 24
    assert report["catalogue_verified"] is True
    scenarios = {
        row["name"]: row for row in report["scenarios"] if row["mode"] == "baseline"
    }
    literal = scenarios["literal"]
    assert literal["metrics"]["recall_at_k"] == 1
    assert literal["metrics"]["first_relevant_rank"] == 1
    paraphrase = scenarios["paraphrase_no_overlap"]
    assert paraphrase["literal_overlap"] == []
    assert paraphrase["metrics"]["recall_at_k"] == 0
    assert paraphrase["metrics"]["first_relevant_rank"] > 3
    assert paraphrase["expectations"]["relevant_in_top_k"] is False
    clutter = scenarios["unrelated_query"]
    assert clutter["metrics"]["returned_count"] > 24
    assert (
        clutter["metrics"]["irrelevant_returned"]
        == clutter["metrics"]["returned_count"]
    )
    assert clutter["expectations"]["no_irrelevant_results"] is False
    assert report["quality_expectations_met"] is False
    assert report["failed_expectations"] == [
        "baseline.paraphrase_no_overlap.relevant_in_top_k",
        "baseline.unrelated_query.no_irrelevant_results",
        "relevant_only.paraphrase_no_overlap.relevant_in_top_k",
    ]


@pytest.mark.parametrize("mode", ["baseline", "relevant_only"])
def test_correction_contradiction_and_age_outcomes(
    report: dict[str, Any],
    mode: str,
) -> None:
    rows = {row["name"]: row for row in report["scenarios"] if row["mode"] == mode}
    correction = rows["correction_history"]
    assert correction["metrics"]["recall_at_k"] == 1
    assert all(correction["expectations"].values())
    assert correction["history_labels"] == ["port_original", "port_corrected"]
    assert correction["history_correction_count"] == 1
    complete = rows["contradiction_context"]
    assert complete["metrics"]["recall_at_k"] == 1
    assert complete["disagreement_count"] == 1
    assert complete["resolution_count"] == 0
    assert all(complete["expectations"].values())
    limited = rows["contradiction_limited_context"]
    assert limited["disagreement_count"] == 0
    assert limited["budget_exhausted"] is True
    assert all(limited["expectations"].values())
    routine = rows["fading"]
    cued = rows["resurfacing"]
    assert routine["returned_labels"] == ["distractor_23"]
    assert cued["returned_labels"] == ["old_calibration"]
    assert routine["metrics"]["recall_at_k"] == 0
    assert cued["metrics"]["recall_at_k"] == 1
    assert all(routine["expectations"].values())
    assert all(cued["expectations"].values())


def test_filter_removes_uncued_clutter_without_inventing_paraphrase_recall(
    report: dict[str, Any],
) -> None:
    rows = {(row["mode"], row["name"]): row for row in report["scenarios"]}
    baseline = rows["baseline", "specific_literal"]
    filtered = rows["relevant_only", "specific_literal"]
    assert baseline["metrics"]["returned_count"] == 30
    assert baseline["metrics"]["irrelevant_returned"] == 29
    assert filtered["returned_labels"] == ["vehicle_schedule"]
    assert filtered["metrics"]["recall_at_k"] == 1
    assert filtered["metrics"]["reciprocal_rank_at_k"] == 1
    assert filtered["metrics"]["irrelevant_returned"] == 0
    assert filtered["budget_consumed"] < baseline["budget_consumed"]
    # The broad literal query still matches "maintenance" in all 24 distractors.
    # Cue filtering is not a semantic judgement about their usefulness.
    broad = rows["relevant_only", "literal"]
    assert broad["metrics"]["irrelevant_returned"] == 24
    unrelated = rows["relevant_only", "unrelated_query"]
    assert unrelated["returned_labels"] == []
    assert unrelated["budget_consumed"] == 0
    assert unrelated["budget_exhausted"] is False
    assert unrelated["expectations"]["no_irrelevant_results"] is True
    paraphrase = rows["relevant_only", "paraphrase_no_overlap"]
    assert paraphrase["literal_overlap"] == []
    assert paraphrase["returned_labels"] == []
    assert paraphrase["metrics"]["recall_at_k"] == 0
    assert paraphrase["metrics"]["first_relevant_rank"] is None
    assert paraphrase["expectations"]["relevant_in_top_k"] is False
    assert report["evaluation_modes"] == [
        {
            "name": "baseline",
            "relevant_only": False,
            "quality_expectations_met": False,
            "failed_expectations": [
                "baseline.paraphrase_no_overlap.relevant_in_top_k",
                "baseline.unrelated_query.no_irrelevant_results",
            ],
        },
        {
            "name": "relevant_only",
            "relevant_only": True,
            "quality_expectations_met": False,
            "failed_expectations": [
                "relevant_only.paraphrase_no_overlap.relevant_in_top_k",
            ],
        },
    ]


def test_modes_compare_the_same_queries_budgets_and_controlled_clock(
    report: dict[str, Any],
) -> None:
    baseline = {
        row["name"]: row for row in report["scenarios"] if row["mode"] == "baseline"
    }
    filtered = {
        row["name"]: row
        for row in report["scenarios"]
        if row["mode"] == "relevant_only"
    }
    assert len(baseline) == len(filtered) == 9
    assert baseline.keys() == filtered.keys()
    for name, row in baseline.items():
        assert row["relevant_only"] is False
        assert filtered[name]["relevant_only"] is True
        for field in ("query", "at", "budget_requested", "relevant_labels"):
            assert row[field] == filtered[name][field]


def test_report_metrics_and_budgets_are_derived_from_disclosed_packets(
    report: dict[str, Any],
) -> None:
    measure = evaluator().measure
    for row in report["scenarios"]:
        assert row["metrics"] == measure(
            row["returned_labels"], set(row["relevant_labels"]), k=3
        )
        assert row["budget_consumed"] <= row["budget_requested"]
        assert row["scope_matches"] is True
        assert row["candidate_only"] is True
    assert (
        report["timings"]["label"]
        == "descriptive local timing; not SLA or semantic benchmark"
    )
    assert len(report["timings"]["recall_ms"]) == len(report["scenarios"])
    assert all(value >= 0 for value in report["timings"]["recall_ms"])
    assert report["timings"]["total_ms"] >= sum(report["timings"]["recall_ms"])
    assert report["source_snapshot"]["unchanged_during_run"] is True
    assert report["controlled_clock"]["start"] == "2026-01-01T12:00:00+00:00"


def test_repeated_runs_have_identical_quality_results(report: dict[str, Any]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    again = json.loads(result.stdout)
    assert again["scenarios"] == report["scenarios"]
    assert again["failed_expectations"] == report["failed_expectations"]


def test_missing_contradiction_endpoints_are_quality_misses_not_invalid_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = evaluator()
    original_post = AsyncClient.post

    async def missing_endpoints(self: AsyncClient, url: Any, **kwargs: Any) -> Response:
        response = await original_post(self, url, **kwargs)
        if (
            url == "/memory/v1/recall"
            and kwargs["json"]["query"] == "archive retention duration"
            and response.status_code == 200
        ):
            packet = response.json()
            packet.update(
                hits=[],
                disagreements=[],
                resolutions=[],
                budget_consumed=0,
                budget_exhausted=False,
            )
            return Response(200, json=packet, request=response.request)
        return response

    monkeypatch.setattr(AsyncClient, "post", missing_endpoints)
    result = asyncio.run(module.evaluate_mode(tmp_path, relevant_only=True))
    rows = {row["name"]: row for row in result["scenarios"]}
    for name in ("contradiction_context", "contradiction_limited_context"):
        row = rows[name]
        assert row["budget_requested"] >= 1
        assert row["metrics"]["recall_at_k"] == 0
        assert row["expectations"]["both_accounts_in_top_k"] is False
        assert (
            f"relevant_only.{name}.both_accounts_in_top_k"
            in result["failed_expectations"]
        )
    assert result["catalogue_verified"] is True


@pytest.mark.parametrize(
    "fingerprint",
    ["evaluation_sha256", "lock_sha256", "git_head", "cairn_python_sha256"],
)
def test_fingerprint_drift_during_first_mode_refuses_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fingerprint: str
) -> None:
    module = evaluator()
    changed = False
    original_post = AsyncClient.post
    original_read = Path.read_bytes
    original_run = subprocess.run
    files = {
        "evaluation_sha256": SCRIPT,
        "lock_sha256": SCRIPT.parent.parent / "uv.lock",
        "cairn_python_sha256": SCRIPT.parent.parent / "src/cairn/__init__.py",
    }

    async def change_during_recall(
        self: AsyncClient, url: Any, **kwargs: Any
    ) -> Response:
        nonlocal changed
        response = await original_post(self, url, **kwargs)
        if url == "/memory/v1/recall":
            changed = True
        return response

    def changed_bytes(path: Path) -> bytes:
        data = original_read(path)
        if changed and path == files.get(fingerprint):
            return data + b"\n# synthetic fingerprint drift\n"
        return data

    def changed_git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if (
            changed
            and fingerprint == "git_head"
            and args[0] == ["git", "rev-parse", "HEAD"]
        ):
            return subprocess.CompletedProcess(args[0], 0, stdout="0" * 40)
        return original_run(*args, **kwargs)

    # Change only boundary observations, never the repository or Git metadata.
    monkeypatch.setattr(AsyncClient, "post", change_during_recall)
    monkeypatch.setattr(Path, "read_bytes", changed_bytes)
    monkeypatch.setattr(subprocess, "run", changed_git)
    with pytest.raises(RuntimeError, match="changed during evaluation"):
        asyncio.run(module.evaluate(tmp_path))
