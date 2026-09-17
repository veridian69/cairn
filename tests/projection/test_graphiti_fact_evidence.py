"""Optional advice uses the existing adapter instance, never a second stack."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.driver.driver import GraphDriver

from cairn.projection.fact_vectors import FactVectorError
from cairn.projection.graphiti import GraphitiIndex
from cairn.projection.semantic_evidence import (
    SEARCH_POLICY,
    SemanticEvidence,
    SemanticEvidenceError,
    local_representation_sha256,
    query_sha256,
)


@pytest.mark.parametrize(
    ("model", "dimension", "enabled"),
    [
        ("text-embedding-3-small", 1024, True),
        ("text-embedding-3-small", 1, False),
        ("text-embedding-3-small", 2, False),
        ("text-embedding-3-small", 1536, False),
        ("text-embedding-3-small", True, False),
        ("text-embedding-3-small", 1024.0, False),
        ("other", 1024, False),
    ],
)
def test_accessor_is_side_effect_free_model_and_exact_dimension_gate(
    model: str, dimension: Any, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = object.__new__(GraphitiIndex)
    monkeypatch.setattr(
        index,
        "_graphiti",
        SimpleNamespace(
            embedder=SimpleNamespace(
                config=SimpleNamespace(embedding_model=model, embedding_dim=dimension)
            )
        ),
        raising=False,
    )
    assert index.memory_evidence_source() is (index if enabled else None)


def test_accessor_preserves_subclass_dynamic_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bounded(GraphitiIndex):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> SemanticEvidence:
            raise SemanticEvidenceError()

    index = object.__new__(Bounded)
    monkeypatch.setattr(
        index,
        "_graphiti",
        SimpleNamespace(
            embedder=SimpleNamespace(
                config=SimpleNamespace(
                    embedding_model="text-embedding-3-small", embedding_dim=1024
                )
            )
        ),
        raising=False,
    )
    source = index.memory_evidence_source()
    assert source is index
    with pytest.raises(SemanticEvidenceError):
        source.search_with_evidence("test", 1, ("acme\n[]",))


@pytest.fixture
def controlled(monkeypatch: pytest.MonkeyPatch) -> tuple[GraphitiIndex, list[str], Any]:
    events: list[str] = []

    class Driver:
        def clone(self, database: str) -> Any:
            return self

    class Embedder:
        config = SimpleNamespace(
            embedding_model="text-embedding-3-small", embedding_dim=1024
        )

        async def create_batch(self, texts: list[str]) -> list[list[float]]:
            events.append("embed")
            assert texts == ["plain query"]
            return [[1.0] + [0.0] * 1023]

    embedder = Embedder()

    async def graph_search(*args: Any, **kwargs: Any) -> Any:
        events.append("graph")
        return SimpleNamespace(
            episodes=[],
            edges=[SimpleNamespace(episodes=[str(UUID(int=i)) for i in range(1, 301)])],
        )

    index = object.__new__(GraphitiIndex)
    monkeypatch.setattr(
        index,
        "_graphiti",
        SimpleNamespace(embedder=embedder, search_with_vector=graph_search),
        raising=False,
    )
    monkeypatch.setattr(index, "_driver", Driver(), raising=False)
    monkeypatch.setattr(index, "_search_bound", asyncio.Semaphore(2), raising=False)
    monkeypatch.setattr(
        index, "_init_owner", SimpleNamespace(admit=lambda: None), raising=False
    )
    monkeypatch.setattr(index, "_search_driver_leases", {}, raising=False)
    monkeypatch.setattr(
        index, "_call", lambda coroutine, **kwargs: asyncio.run(coroutine)
    )

    class Legacy:
        representation = index._fact_vector_index().representation

        async def preflight(self, *args: Any) -> None:
            events.append("legacy-preflight")

        async def search(self, *args: Any) -> list[str]:
            events.append("legacy-search")
            return [str(UUID(int=301))]

    class Local:
        fail = False

        async def preflight(self, *args: Any) -> None:
            events.append("local-preflight")

        async def search(self, *args: Any) -> Any:
            events.append("local-search")
            if self.fail:
                raise FactVectorError("fact_vector_rebuild_required")
            return 1, ((str(UUID(int=302)), "a" * 64, 0.75),)

    local = Local()
    monkeypatch.setattr(index, "_fact_vector_index", lambda: Legacy())
    monkeypatch.setattr(index, "_fact_unit_index", lambda: local, raising=False)
    return index, events, local


def test_graded_search_reuses_query_and_preserves_unbounded_legacy_union(
    controlled: Any,
) -> None:
    index, events, _ = controlled
    result = index.search_with_evidence("plain\nquery", 1, ("acme\n[]",))
    assert result.candidate_ids == tuple(UUID(int=i) for i in range(1, 303))
    assert result.query_sha256 == query_sha256("plain\nquery")
    assert result.representation_sha256 == local_representation_sha256()
    assert result.search_policy == SEARCH_POLICY
    assert result.partitions[0].eligible_count == 1
    assert result.partitions[0].grades[0].fact_id == UUID(int=302)
    assert events == [
        "legacy-preflight",
        "local-preflight",
        "embed",
        "graph",
        "legacy-search",
        "local-search",
    ]


def test_legacy_search_never_requires_local_coverage(controlled: Any) -> None:
    index, events, local = controlled
    local.fail = True
    assert len(index.search("plain query", 1, ("acme\n[]",))) == 301
    assert not any(event.startswith("local") for event in events)


def test_blank_advice_is_benign_without_coverage_claim(controlled: Any) -> None:
    index, events, _ = controlled
    result = index.search_with_evidence(" \n\t", 1, ("acme\n[]",))
    assert result.partitions == result.candidate_ids == ()
    assert events == []


def test_failed_local_coverage_returns_no_partial_advice_or_retry(
    controlled: Any,
) -> None:
    index, events, local = controlled
    local.fail = True
    with pytest.raises(SemanticEvidenceError):
        index.search_with_evidence("plain query", 1, ("acme\n[]",))
    assert events.count("embed") == events.count("graph") == 1


@pytest.mark.parametrize("dimension", [2, 1024, 1536])
def test_delivery_uses_same_gate_and_only_one_representation_path(
    dimension: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = object.__new__(GraphitiIndex)
    monkeypatch.setattr(
        index,
        "_graphiti",
        SimpleNamespace(
            embedder=SimpleNamespace(
                config=SimpleNamespace(
                    embedding_model="text-embedding-3-small", embedding_dim=dimension
                )
            )
        ),
        raising=False,
    )
    events = []

    class Index:
        def __init__(self, name: str) -> None:
            self.name = name

        async def ensure(self, *args: Any) -> None:
            events.append(self.name)

    monkeypatch.setattr(index, "_fact_vector_index", lambda: Index("legacy"))
    monkeypatch.setattr(index, "_fact_unit_index", lambda: Index("local-plus-pooled"))
    asyncio.run(
        index._ensure_fact_vectors(cast(GraphDriver, SimpleNamespace()), "a" * 64, [])
    )
    assert events == (["local-plus-pooled"] if dimension == 1024 else ["legacy"])


def test_metadata_keeps_v1_and_reports_actual_enabled_policy(controlled: Any) -> None:
    index, events, _ = controlled
    metadata = index.index_policy_metadata()
    assert metadata["representation"]["version"] == "cairn.fact-vector/v1"
    local = metadata["local_evidence"]
    assert local["enabled"] is True
    assert local["representation"]["sha256"] == local_representation_sha256()
    assert local["search"] == {
        "version": SEARCH_POLICY,
        "cutoff": 0.60,
        "comparison": "strictly greater than",
        "score": "max (1 + cosine)/2 over distinct unit vectors",
        "fact_limit_per_partition": 256,
        "partition_limit": 17,
    }
    assert events == []


def test_metadata_non1024_keeps_only_v1(controlled: Any) -> None:
    index, _, _ = controlled
    index._graphiti.embedder.config.embedding_dim = 2
    assert index.index_policy_metadata()["local_evidence"] == {
        "enabled": False,
        "representation": None,
        "search": None,
    }
