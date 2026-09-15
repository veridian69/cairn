"""Evidence policy must come from the executing adapter, not evaluator defaults."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_evaluator_copies_actual_adapter_policy_and_labels_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.projection.graphiti import GraphitiIndex

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    live = importlib.import_module("semantic_memory_live")

    class Index(GraphitiIndex):
        def __init__(self) -> None:
            pass

        def index_policy_metadata(self) -> dict[str, Any]:
            return {"search": {"sha256": "executed-policy"}, "last_requested_limit": 7}

    assert live.index_policy_metadata(Index()) == {
        "search": {"sha256": "executed-policy"},
        "last_requested_limit": 7,
    }
    assert live.index_policy_metadata(object()) == {
        "available": False,
        "source": "controlled-index-not-semantic",
    }


@pytest.mark.parametrize("dimension", [1024, 1536, 1])
def test_real_policy_snapshot_uses_executing_dimension_and_limit(
    monkeypatch: pytest.MonkeyPatch, dimension: int
) -> None:
    from cairn.projection.graphiti import GraphitiIndex

    adapter: Any = GraphitiIndex.__new__(GraphitiIndex)
    adapter._graphiti = SimpleNamespace(
        embedder=SimpleNamespace(
            config=SimpleNamespace(
                embedding_model="text-embedding-3-small", embedding_dim=dimension
            )
        )
    )
    adapter._last_search_limit = 7
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    live = importlib.import_module("semantic_memory_live")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("policy snapshot must not query or invoke a provider")

    monkeypatch.setattr(adapter, "_call", forbidden)
    snapshot = live.index_policy_metadata(adapter)
    assert snapshot["representation"]["dimension"] == dimension
    assert snapshot["representation"]["version"] == "cairn.fact-vector/v1"
    assert snapshot["search"]["cutoff"] == 0.60
    assert snapshot["graphiti_search"]["limit"] == 7
    assert snapshot["libraries"]["graphiti-core"] == "0.30.2"
    assert snapshot["last_requested_limit"] == 7
    local = snapshot["local_evidence"]
    if dimension == 1024:
        assert local["enabled"] is True
        assert local["representation"]["version"] == "cairn.fact-local-vector/v5"
        assert (
            local["representation"]["sha256"]
            == "8dc6d82b0e86630b9529e1735ea037d9583f55a99384f5558b49b022cabb1229"
        )
        assert local["search"] == {
            "version": "cairn.fact-local-search/v5",
            "cutoff": 0.60,
            "comparison": "strictly greater than",
            "score": "max (1 + cosine)/2 over distinct unit vectors",
            "fact_limit_per_partition": 256,
            "partition_limit": 17,
        }
    else:
        assert local == {"enabled": False, "representation": None, "search": None}
