"""Synthetic semantic evaluation truth must fail closed before provider work."""

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests/fixtures/memory_quality/everyday-v1.json"


def evaluator() -> Any:
    path = ROOT / "scripts/semantic_memory_corpus.py"
    assert path.is_file(), "Semantic corpus validator is missing"
    spec = importlib.util.spec_from_file_location("semantic_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture() -> dict[str, Any]:
    value = json.loads(CORPUS.read_bytes())
    assert isinstance(value, dict)
    return value


def test_fixed_corpus_validates_without_provider_and_orders_events() -> None:
    module = evaluator()
    corpus = module.validate_corpus(fixture())
    events = module.seed_events(corpus)
    assert len(events) == 13
    assert [event["recorded_at"] for event in events] == sorted(
        event["recorded_at"] for event in events
    )
    correction = next(
        i for i, event in enumerate(events) if event["kind"] == "correction"
    )
    assert events[correction - 1]["value"]["label"] == "new_route"
    assert events[correction + 1]["value"]["label"] == "fan_inspection"


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["facts"].append(copy.deepcopy(c["facts"][0])),
        lambda c: c["queries"][0].update(relevant=["missing"]),
        lambda c: c["queries"][0].update(top_k=True),
        lambda c: c["queries"][0].update(minimum_recall=float("nan")),
        lambda c: c["queries"][0].update(minimum_recall=10**400),
        lambda c: c["queries"][0].update(query="cooling fan"),
        lambda c: c["queries"][0].update(require_top_one="entry_approval"),
        lambda c: c["queries"][0].update(forbidden=["thermal_response"]),
        lambda c: c["queries"][0].update(relevant=["sibling_thermal"]),
        lambda c: c["queries"][0].update(split="test-ish"),
        lambda c: c["queries"].append(copy.deepcopy(c["queries"][0])),
        lambda c: c["queries"].clear(),
        lambda c: c["facts"][0].update(recorded_at="2024-01-01T12:00:00"),
        lambda c: c["facts"][0].update(body="x" * 4097),
        lambda c: c["facts"][0].update(scope="productive"),
        lambda c: c["corrections"][0].update(source="missing"),
        lambda c: c["corrections"][0].update(recorded_at="2023-01-01T00:00:00Z"),
        lambda c: c["corrections"][0].update(replacement="old_route"),
        lambda c: c["invariants"].update(projection_must_complete=False),
        lambda c: c.update(unexpected="input"),
    ],
)
def test_invalid_corpus_refuses_safe_code(change: Any) -> None:
    module = evaluator()
    corpus = fixture()
    change(corpus)
    with pytest.raises(module.CorpusError) as caught:
        module.validate_corpus(corpus)
    assert str(caught.value) == "invalid_corpus"


def test_metrics_count_positions_not_duplicate_relevance() -> None:
    module = evaluator()
    result = module.measure(["noise", "a", "a", "b"], ["a", "b"], top_k=3)
    assert result == {
        "precision_at_k": 1 / 3,
        "recall_at_k": 1 / 2,
        "reciprocal_rank_at_k": 1 / 2,
        "returned_count": 4,
        "irrelevant_at_k": 1,
        "irrelevant_returned": 1,
        "duplicate_returned": 1,
    }
    empty = module.measure([], [], top_k=3)
    assert empty["precision_at_k"] is None
    assert empty["recall_at_k"] is None


def test_unknown_labels_pending_projection_and_degradation_never_pass() -> None:
    module = evaluator()
    corpus = module.validate_corpus(fixture())
    query = corpus["queries"][0]
    for returned, projected, degraded in [
        (["unknown"], True, False),
        (["thermal_response"], False, False),
        (["thermal_response"], True, True),
        (["thermal_response", "sibling_thermal"], True, False),
        (["thermal_response", "thermal_response"], True, False),
        (["thermal_response", "old_route"], True, False),
        (
            ["thermal_response", "fan_inspection", "warehouse_paint", "old_route"],
            True,
            False,
        ),
    ]:
        result = module.assess_query(
            corpus,
            query,
            returned,
            projection_complete=projected,
            semantic_degraded=degraded,
        )
        assert result["expectations_met"] is False
        assert result["failed_expectations"]
    result = module.assess_query(
        corpus,
        query,
        ["thermal_response"],
        projection_complete=True,
        semantic_degraded=False,
    )
    assert result["expectations_met"] is True
    assert "semantic_evidence" not in result  # Scoring alone is not live evidence.


def test_corrections_and_unrelated_clutter_are_explicit_failures() -> None:
    module = evaluator()
    corpus = module.validate_corpus(fixture())
    queries = {q["name"]: q for q in corpus["queries"]}
    for name, returned, expected in [
        ("corrected_current_route", ["new_route", "old_route"], "forbidden_fact"),
        ("unrelated_query", ["thermal_response"], "irrelevant_clutter"),
        ("literal_id", ["different_valve", "literal_identifier"], "top_one"),
    ]:
        result = module.assess_query(
            corpus,
            queries[name],
            returned,
            projection_complete=True,
            semantic_degraded=False,
        )
        assert result["expectations_met"] is False
        assert expected in result["failed_expectations"]
