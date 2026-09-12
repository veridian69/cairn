"""Bounded synthetic evaluation truth and scoring; never proof of provider use.

The live runner owns instance isolation, projection and provider provenance.
This module performs no I/O and makes no semantic-evidence claim. Expected
labels remain outside provider inputs. A successful score is necessary, not
sufficient, for accepting a real semantic run.
"""

import copy
import re
from datetime import UTC, datetime
from typing import Any

_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_INVARIANTS = {
    "forbidden_scope": "sibling",
    "trust": "candidate",
    "current_only": True,
    "projection_must_complete": True,
    "report_precision_and_recall_for_each_query": True,
    "no_corpus_specific_synonyms_or_expected_labels_in_provider_queries": True,
}


class CorpusError(ValueError):
    """Input-free error suitable for an evaluator's public failure envelope."""

    def __init__(self) -> None:
        super().__init__("invalid_corpus")


def _require(condition: bool) -> None:
    if not condition:
        raise CorpusError()


def _text(value: Any, maximum: int) -> None:
    _require(isinstance(value, str) and bool(value.strip()))
    try:
        _require(len(value.encode("utf-8")) <= maximum)
    except UnicodeError:
        raise CorpusError() from None


def _timestamp(value: Any) -> datetime:
    _text(value, 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise CorpusError() from None


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    _require(isinstance(value, dict))
    _require(required <= value.keys() <= required | (optional or set()))


def _labels(value: Any, known: set[str]) -> set[str]:
    _require(isinstance(value, list) and len(value) <= 128)
    _require(all(isinstance(item, str) for item in value))
    result = set(value)
    _require(len(result) == len(value) and result <= known)
    return result


def validate_corpus(document: Any) -> dict[str, Any]:
    """Return an independent validated copy of one small versioned corpus."""
    _object(
        document,
        {"schema", "description", "facts", "corrections", "queries", "invariants"},
    )
    _require(document["schema"] == "cairn.synthetic-memory-quality/v1")
    _text(document["description"], 2048)
    _require(document["invariants"] == _INVARIANTS)
    _require(
        all(
            type(document["invariants"][key]) is type(value)
            for key, value in _INVARIANTS.items()
        )
    )
    facts, queries, corrections = (
        document["facts"],
        document["queries"],
        document["corrections"],
    )
    _require(isinstance(facts, list) and 1 <= len(facts) <= 128)
    _require(isinstance(queries, list) and 1 <= len(queries) <= 128)
    _require(isinstance(corrections, list) and len(corrections) <= 128)
    by_label: dict[str, dict[str, Any]] = {}
    for fact in facts:
        _object(fact, {"label", "body", "recorded_at", "scope"})
        _text(fact["label"], 64)
        _require(
            _LABEL.fullmatch(fact["label"]) is not None
            and fact["label"] not in by_label
        )
        _text(fact["body"], 4096)
        _require(fact["scope"] in ("job", "sibling"))
        _timestamp(fact["recorded_at"])
        by_label[fact["label"]] = fact
    corrected: set[str] = set()
    for correction in corrections:
        _object(correction, {"source", "replacement", "reason", "recorded_at"})
        pair = _labels([correction["source"], correction["replacement"]], set(by_label))
        _require(len(pair) == 2 and correction["source"] not in corrected)
        corrected.add(correction["source"])
        _text(correction["reason"], 4096)
        at = _timestamp(correction["recorded_at"])
        _require(
            all(_timestamp(by_label[label]["recorded_at"]) <= at for label in pair)
        )
        _require(len({by_label[label]["scope"] for label in pair}) == 1)
    names: set[str] = set()
    splits: set[str] = set()
    for query in queries:
        _object(
            query,
            {"name", "split", "query", "relevant", "top_k", "zero_literal_overlap"},
            {"minimum_recall", "maximum_irrelevant", "require_top_one", "forbidden"},
        )
        _text(query["name"], 64)
        _require(
            _LABEL.fullmatch(query["name"]) is not None and query["name"] not in names
        )
        names.add(query["name"])
        _require(query["split"] in ("development", "held-out"))
        splits.add(query["split"])
        _text(query["query"], 4096)
        relevant = _labels(query["relevant"], set(by_label))
        forbidden = _labels(query.get("forbidden", []), set(by_label))
        _require(not relevant & forbidden and not relevant & corrected)
        _require(all(by_label[label]["scope"] == "job" for label in relevant))
        _require(type(query["top_k"]) is int and 1 <= query["top_k"] <= 32)
        _require(type(query["zero_literal_overlap"]) is bool)
        if relevant:
            minimum = query.get("minimum_recall")
            _require(type(minimum) in (int, float) and 0 < minimum <= 1)
        else:
            _require(
                query.get("maximum_irrelevant") == 0
                and type(query.get("maximum_irrelevant")) is int
            )
        if "minimum_recall" in query:
            minimum = query["minimum_recall"]
            _require(type(minimum) in (int, float) and 0 <= minimum <= 1)
        if "maximum_irrelevant" in query:
            maximum = query["maximum_irrelevant"]
            _require(type(maximum) is int and 0 <= maximum <= 128)
        if "require_top_one" in query:
            _require(
                isinstance(query["require_top_one"], str)
                and query["require_top_one"] in relevant
            )
        if query["zero_literal_overlap"]:
            _require(bool(relevant))
            words = set(re.findall(r"\w+", query["query"].casefold()))
            _require(
                all(
                    not words
                    & set(re.findall(r"\w+", by_label[label]["body"].casefold()))
                    for label in relevant
                )
            )
    _require(splits == {"development", "held-out"})
    return dict(copy.deepcopy(document))


def seed_events(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    """Order facts and corrections on one monotonic synthetic server clock."""
    events = [
        {
            "kind": kind,
            "recorded_at": _timestamp(value["recorded_at"]).isoformat(),
            "value": copy.deepcopy(value),
        }
        for kind, values in (
            ("fact", corpus["facts"]),
            ("correction", corpus["corrections"]),
        )
        for value in values
    ]
    return sorted(
        events, key=lambda event: (event["recorded_at"], event["kind"] != "fact")
    )


def measure(returned: list[str], relevant: list[str], *, top_k: int) -> dict[str, Any]:
    """Unique true positives; duplicate positions never inflate precision."""
    _require(type(top_k) is int and top_k > 0)
    truth, selected = set(relevant), returned[:top_k]
    true_positives = len(set(selected) & truth)
    first = next((i for i, label in enumerate(selected, 1) if label in truth), None)
    return {
        "precision_at_k": true_positives / len(selected) if selected else None,
        "recall_at_k": true_positives / len(truth) if truth else None,
        "reciprocal_rank_at_k": (1 / first if first else 0) if truth else None,
        "returned_count": len(returned),
        "irrelevant_at_k": sum(label not in truth for label in selected),
        "irrelevant_returned": sum(label not in truth for label in returned),
        "duplicate_returned": len(returned) - len(set(returned)),
    }


def assess_query(
    corpus: dict[str, Any],
    query: dict[str, Any],
    returned: list[str],
    *,
    projection_complete: bool,
    semantic_degraded: bool,
) -> dict[str, Any]:
    """Assess supplied observations; this cannot authenticate their provenance."""
    _require(
        isinstance(returned, list)
        and len(returned) <= 128
        and all(isinstance(label, str) for label in returned)
    )
    known = {fact["label"]: fact for fact in corpus["facts"]}
    metrics = measure(returned, query["relevant"], top_k=query["top_k"])
    failures: list[str] = []
    checks = {
        "projection_incomplete": projection_complete is True,
        "semantic_degraded": semantic_degraded is False,
        "unknown_label": all(label in known for label in returned),
        "hidden_scope": all(
            known[label]["scope"] == "job" for label in returned if label in known
        ),
        "duplicate_result": metrics["duplicate_returned"] == 0,
        "forbidden_fact": not set(returned) & set(query.get("forbidden", [])),
        "superseded_fact": not set(returned)
        & {correction["source"] for correction in corpus["corrections"]},
    }
    if query["relevant"]:
        checks["recall"] = metrics["recall_at_k"] >= query["minimum_recall"]
    if "require_top_one" in query:
        checks["top_one"] = bool(returned) and returned[0] == query["require_top_one"]
    if "maximum_irrelevant" in query:
        checks["irrelevant_clutter"] = (
            metrics["irrelevant_returned"] <= query["maximum_irrelevant"]
        )
    failures.extend(name for name, passed in checks.items() if not passed)
    return {
        "metrics": metrics,
        "expectations_met": not failures,
        "failed_expectations": failures,
    }
