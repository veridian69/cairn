"""The edge-dedup evaluator's corpus and scoring, without a provider."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluate_edge_dedup.py"


@pytest.fixture
def evaluator(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("edge_dedup_evaluator_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shipped_corpus_is_valid_and_mixed(evaluator: Any) -> None:
    items, digest = evaluator.load_corpus(evaluator.CORPUS)
    assert len(digest) == 64
    assert len({item["id"] for item in items}) == len(items) == 15
    assert 0 < sum(bool(item["duplicates"]) for item in items) < len(items)


def test_context_indexes_continuously(evaluator: Any) -> None:
    built = evaluator.context(
        {"existing": ["a", "b"], "invalidation": ["c"], "new": "d"}
    )
    assert [fact["idx"] for fact in built["existing_edges"]] == [0, 1]
    assert built["edge_invalidation_candidates"] == [{"idx": 2, "fact": "c"}]
    assert built["new_edge"] == "d"


def test_score_separates_false_missed_and_out_of_range(evaluator: Any) -> None:
    item = {
        "id": "x",
        "existing": ["a", "b"],
        "invalidation": ["c"],
        "duplicates": [0],
        "contradicted": [2],
    }
    exact = evaluator.score(item, {"duplicate_facts": [0], "contradicted_facts": [2]})
    assert exact["duplicates_correct"] and exact["contradictions_correct"]
    wrong = evaluator.score(item, {"duplicate_facts": [1], "contradicted_facts": []})
    assert not wrong["duplicates_correct"]
    assert (wrong["false_duplicates"], wrong["missed_duplicates"]) == (1, 1)
    assert wrong["contradictions_correct"] is False
    # Graphiti discards an idx outside EXISTING FACTS, so it is not a false duplicate.
    stray = evaluator.score(
        item, {"duplicate_facts": [0, 2], "contradicted_facts": [2]}
    )
    assert stray["duplicates_correct"]
    assert (stray["false_duplicates"], stray["out_of_range"]) == (0, 1)


def test_unscored_contradiction_stays_out_of_the_summary(evaluator: Any) -> None:
    item = {
        "id": "x",
        "existing": ["a"],
        "invalidation": [],
        "duplicates": [],
        "contradicted": None,
    }
    result = evaluator.score(item, {"duplicate_facts": [], "contradicted_facts": [0]})
    assert result["contradictions_correct"] is None
    summary = evaluator.summarise([[result]])
    assert summary["contradictions_scored"] == 0
    assert summary["duplicates_correct_per_repeat"] == [1]


def test_corpus_refuses_a_label_outside_its_list(
    evaluator: Any, tmp_path: Path
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        '{"schema": "cairn.edge-dedup-corpus.v1", "items": [{"id": "x", '
        '"existing": ["a"], "invalidation": [], "new": "b", '
        '"duplicates": [1], "contradicted": []}]}'
    )
    with pytest.raises(ValueError, match="corpus_invalid"):
        evaluator.load_corpus(corpus)


def test_run_drops_ambient_provider_settings_and_silences_logs(
    evaluator: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "key"
    key.write_text("k" * 32)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.invalid/v1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    seen: dict[str, Any] = {}

    async def measure(*_: Any) -> dict[str, Any]:
        seen["environment"] = dict(evaluator.os.environ)
        seen["logging_disabled"] = evaluator.logging.root.manager.disable
        raise RuntimeError("provider text that must not be printed")

    monkeypatch.setattr(evaluator, "measure", measure)
    monkeypatch.setattr(evaluator.os, "environ", dict(evaluator.os.environ))
    monkeypatch.setattr(
        evaluator.sys,
        "argv",
        ["x", "--provider-key-file", str(key), "--output", str(tmp_path / "out")],
    )
    previous = evaluator.logging.root.manager.disable
    try:
        assert evaluator.main() == 2
    finally:
        evaluator.logging.disable(previous)
    assert "OPENAI_BASE_URL" not in seen["environment"]
    assert "HTTPS_PROXY" not in seen["environment"]
    assert seen["environment"]["OPENAI_API_KEY"] == "k" * 32
    assert seen["logging_disabled"] >= evaluator.logging.CRITICAL
