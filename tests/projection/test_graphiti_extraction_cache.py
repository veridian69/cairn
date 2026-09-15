"""Tests for the pinned bulk-extraction caching seam (P-90 seam 1)."""

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.prompts.models import Message
from graphiti_core.utils.maintenance import edge_operations
from pydantic import BaseModel

import cairn.projection.graphiti as graphiti_module
import cairn.projection.graphiti_bulk as graphiti_bulk_module
from cairn.projection import graphiti_extraction_cache as seam
from cairn.projection.graphiti import BoundedFalkorDriver
from cairn.runtime.logging import CacheKind, configure_logging


class _FakeStore:
    def __init__(self) -> None:
        # R3: rows are fact-owned, mirroring the real store's composite
        # (cache_key, fact_id) primary key. A keyless read serves the
        # lowest fact_id deterministically, as the real store does.
        self.rows: dict[tuple[str, str], str] = {}
        self.embeddings: dict[str, str] = {}
        self.put_embedding_many_calls: list[list[tuple[str, str]]] = []
        # Which cache each caller declared itself to be (R4 follow-up).
        self.kinds: list[CacheKind | None] = []

    def get(
        self,
        cache_key: str,
        fact_id: str | None = None,
        *,
        kind: CacheKind | None = None,
    ) -> str | None:
        self.kinds.append(kind)
        if fact_id is not None:
            return self.rows.get((cache_key, fact_id))
        owners = sorted(fact for key, fact in self.rows if key == cache_key)
        if not owners:
            return None
        return self.rows[(cache_key, owners[0])]

    def put(
        self,
        cache_key: str,
        fact_id: str,
        payload: str,
        *,
        kind: CacheKind | None = None,
    ) -> None:
        self.kinds.append(kind)
        self.rows.setdefault((cache_key, fact_id), payload)

    def put_many(self, rows: list[tuple[str, str, str]]) -> None:
        for cache_key, fact_id, payload in rows:
            self.put(cache_key, fact_id, payload)

    def get_embedding(self, cache_key: str) -> str | None:
        return self.embeddings.get(cache_key)

    def put_embedding(self, cache_key: str, payload: str) -> None:
        self.put_embedding_many([(cache_key, payload)])

    def put_embedding_many(self, rows: list[tuple[str, str]]) -> None:
        self.put_embedding_many_calls.append(list(rows))
        for cache_key, payload in rows:
            self.embeddings.setdefault(cache_key, payload)

    def _repair_embedding_many(self, rows: list[tuple[str, str, str]]) -> None:
        for key, observed, replacement in rows:
            if key not in self.embeddings or self.embeddings[key] == observed:
                self.embeddings[key] = replacement


class _FakeLLMClient(LLMClient):
    """A real LLMClient subclass, not a duck type.

    ``edge_operations._extract_edge_timestamps`` (and
    ``install_edge_timestamp_caching``'s wrapper around it) is typed
    against the ABC, so driving the real function needs a genuine
    subclass rather than an ``Any``-typed local at each call site — the
    same device as ``_RecordingClient`` in
    ``test_graphiti_edge_batch.py:645``.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        super().__init__(config=None)
        self._responses = list(responses) if responses is not None else []
        self.calls: list[Any] = []

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        raise AssertionError(
            "the fake answers at generate_response, not the retry path"
        )

    def _get_model_for_size(self, model_size: ModelSize) -> str:
        return "fake-medium" if model_size is ModelSize.medium else "fake-small"

    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        *,
        attribute_extraction: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(messages)
        return self._responses.pop(0)


def _episode(name: str = "ep", content: str | None = None) -> EpisodicNode:
    return EpisodicNode(
        name=name,
        group_id="g",
        source=EpisodeType.text,
        source_description="test",
        # The cache key is content-addressed (episode_sha), not
        # name-addressed, so distinct episodes need distinct content by
        # default or they collide on the same cache key.
        content=content if content is not None else f"content for {name}",
        valid_at=datetime.now(UTC),
    )


def _clients() -> Any:
    return SimpleNamespace(llm_client=_FakeLLMClient())


def _node(name: str) -> EntityNode:
    return EntityNode(name=name, group_id="g")


def _edge(source: str, target: str, fact: str) -> EntityEdge:
    return EntityEdge(
        group_id="g",
        source_node_uuid=source,
        target_node_uuid=target,
        created_at=datetime.now(UTC),
        name="RELATES",
        fact=fact,
    )


class _Recorder:
    """Records extract_nodes_and_edges_bulk calls, one distinguishable pair per episode."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[EpisodicNode, list[EpisodicNode]]]] = []

    async def __call__(
        self,
        clients: Any,
        episode_context: list[tuple[EpisodicNode, list[EpisodicNode]]],
        *,
        edge_type_map: dict[tuple[str, str], list[str]],
        edge_types: dict[str, type] | None = None,
        entity_types: dict[str, type] | None = None,
        excluded_entity_types: list[str] | None = None,
        custom_extraction_instructions: str | None = None,
    ) -> tuple[list[list[EntityNode]], list[list[EntityEdge]]]:
        self.calls.append(list(episode_context))
        nodes_bulk = []
        edges_bulk = []
        for episode, _ in episode_context:
            node = _node(f"node-{episode.name}")
            nodes_bulk.append([node])
            edges_bulk.append([_edge(node.uuid, node.uuid, f"fact-{episode.name}")])
        return nodes_bulk, edges_bulk


def test_the_key_is_the_accepted_material() -> None:
    episode = _episode(content="hello world")
    llm_client = _FakeLLMClient()
    entity_types = None
    edge_types = None
    edge_type_map: dict[tuple[str, str], list[str]] = {
        ("Entity", "Entity"): ["RELATES"]
    }
    instructions = "be terse"

    material = {
        "episode_sha": hashlib.sha256(episode.content.encode("utf-8")).hexdigest(),
        "graphiti_core": seam._GRAPHITI_CORE_VERSION,
        "llm_model": "fake-medium",
        "small_model": "fake-small",
        "entity_types": "null",
        "edge_type_map": hashlib.sha256(
            json.dumps(
                {"Entity|Entity": ["RELATES"]},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "instructions": instructions,
    }
    expected = hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()

    assert (
        seam.extraction_cache_key(
            episode, llm_client, entity_types, edge_types, edge_type_map, instructions
        )
        == expected
    )


@pytest.mark.anyio
async def test_a_changed_previous_episode_window_still_hits_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The previous-episode window enters the pinned library's extraction
    # prompt but has no field in the accepted key, so the same episode
    # under a different window is served the cached extraction rather
    # than re-extracted. That is the accepted key's consequence, named
    # as a known gap in migration 0007's comment; pin it as behaviour so
    # a future key change has to come here and say so.
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episode = _episode("a")

    for window in ([], [_episode("earlier")]):
        nodes_bulk, _ = await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            [(episode, window)],
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
        )
        assert nodes_bulk[0][0].name == "node-a"

    assert len(recorder.calls) == 1
    assert len(store.rows) == 1


def test_serialisation_round_trips_through_the_library_models() -> None:
    node = _node("Alice")
    edge = _edge(node.uuid, node.uuid, "Alice likes Bob")
    payload = seam.serialise_extraction([node], [edge])
    result = seam.deserialise_extraction(payload)
    assert result is not None
    nodes, edges = result
    assert nodes[0].uuid == node.uuid
    assert nodes[0].name == node.name
    assert edges[0].fact == edge.fact
    assert edges[0].source_node_uuid == edge.source_node_uuid
    assert edges[0].target_node_uuid == edge.target_node_uuid


def test_a_corrupt_payload_deserialises_to_none() -> None:
    assert seam.deserialise_extraction("not json") is None
    assert seam.deserialise_extraction(json.dumps({"nodes": "wrong shape"})) is None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_a_full_miss_calls_through_and_populates_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), []),
        (_episode("b"), []),
    ]
    nodes_bulk, edges_bulk = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        episodes,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]) == 2
    assert len(store.rows) == 2
    assert nodes_bulk[0][0].name == "node-a"
    assert nodes_bulk[1][0].name == "node-b"
    assert edges_bulk[0][0].fact == "fact-a"
    assert edges_bulk[1][0].fact == "fact-b"


@pytest.mark.anyio
async def test_a_full_hit_skips_the_library_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), []),
        (_episode("b"), []),
    ]
    keys = [
        seam.extraction_cache_key(episode, _FakeLLMClient(), None, None, {}, None)
        for episode, _ in episodes
    ]
    for key, (episode, _) in zip(keys, episodes, strict=True):
        node = _node(f"cached-{episode.name}")
        edge = _edge(node.uuid, node.uuid, f"cached-fact-{episode.name}")
        store.put(key, str(episode.uuid), seam.serialise_extraction([node], [edge]))

    nodes_bulk, edges_bulk = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        episodes,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert recorder.calls == []
    assert nodes_bulk[0][0].name == "cached-a"
    assert nodes_bulk[1][0].name == "cached-b"
    assert edges_bulk[0][0].fact == "cached-fact-a"
    assert edges_bulk[1][0].fact == "cached-fact-b"


@pytest.mark.anyio
async def test_a_mixed_batch_calls_through_only_the_misses_and_merges_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), []),
        (_episode("b"), []),
        (_episode("c"), []),
    ]
    middle_key = seam.extraction_cache_key(
        episodes[1][0], _FakeLLMClient(), None, None, {}, None
    )
    cached_node = _node("cached-b")
    cached_edge = _edge(cached_node.uuid, cached_node.uuid, "cached-fact-b")
    store.put(
        middle_key,
        str(episodes[1][0].uuid),
        seam.serialise_extraction([cached_node], [cached_edge]),
    )

    nodes_bulk, edges_bulk = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        episodes,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(recorder.calls) == 1
    called_names = [episode.name for episode, _ in recorder.calls[0]]
    assert called_names == ["a", "c"]

    assert len(nodes_bulk) == 3
    assert len(edges_bulk) == 3
    assert nodes_bulk[0][0].name == "node-a"
    assert nodes_bulk[1][0].name == "cached-b"
    assert nodes_bulk[2][0].name == "node-c"
    assert edges_bulk[0][0].fact == "fact-a"
    assert edges_bulk[1][0].fact == "cached-fact-b"
    assert edges_bulk[2][0].fact == "fact-c"
    assert len(store.rows) == 3


@pytest.mark.anyio
async def test_a_cross_episode_content_collision_is_rejected_on_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # C1/R3: the key is content-scoped, so two distinct episodes with
    # byte-identical bodies hash to the same key. episode_a populates its
    # row; episode_b must MISS on its first read (no row owned by its
    # uuid) and call through rather than inherit episode_a's nodes and
    # edges — then keep its own fact-owned row beside episode_a's.
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episode_a = _episode("a", content="same body")
    episode_b = _episode("b", content="same body")
    assert episode_a.uuid != episode_b.uuid

    nodes_bulk_a, _ = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        [(episode_a, [])],
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 1
    assert nodes_bulk_a[0][0].name == "node-a"

    # episode_a still hits its own row.
    nodes_bulk_a_again, _ = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        [(episode_a, [])],
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 1
    assert nodes_bulk_a_again[0][0].name == "node-a"

    # episode_b hashes to the same key but is a different fact: MISS,
    # call through, do not inherit episode_a's identity.
    nodes_bulk_b, _ = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        [(episode_b, [])],
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 2
    assert nodes_bulk_b[0][0].name == "node-b"

    # R3: episode_b's call-through kept its own row beside episode_a's,
    # so a rebuild hits for both without another provider call.
    key = seam.extraction_cache_key(episode_a, _FakeLLMClient(), None, None, {}, None)
    assert len(store.rows) == 2
    for episode, name in ((episode_a, "node-a"), (episode_b, "node-b")):
        stored = seam.deserialise_extraction(store.rows[(key, str(episode.uuid))])
        assert stored is not None
        stored_nodes, _ = stored
        assert stored_nodes[0].name == name

    nodes_bulk_b_again, _ = await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        [(episode_b, [])],
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 2
    assert nodes_bulk_b_again[0][0].name == "node-b"


@pytest.mark.anyio
async def test_identical_bodies_both_populate_and_both_hit_on_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # R3 regression, against the real sqlite store: two facts with
    # byte-identical bodies both populate fact-owned rows, then both hit
    # on a rebuild without a provider call.
    from cairn.catalogue.extraction_cache import ExtractionCacheStore
    from cairn.catalogue.migration import migrate_catalogue
    from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=tmp_path, credentials=tmp_path / "credentials"),
    )
    migrate_catalogue(config, lambda: datetime.now(UTC))
    store = ExtractionCacheStore(tmp_path, writer_gate=threading.Lock())

    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    episode_a = _episode("a", content="same body")
    episode_b = _episode("b", content="same body")

    for episode in (episode_a, episode_b):
        await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            [(episode, [])],
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
        )
    assert len(recorder.calls) == 2

    # The rebuild: both facts hit their own rows, no provider call.
    for episode, name in ((episode_a, "node-a"), (episode_b, "node-b")):
        nodes_bulk, _ = await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            [(episode, [])],
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
        )
        assert nodes_bulk[0][0].name == name
    assert len(recorder.calls) == 2


@pytest.mark.anyio
async def test_a_corrupt_stored_payload_falls_through_the_bulk_wrapper(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # M2/deferred Task 3 item: no test previously drove a corrupt stored
    # payload through cached_extract_nodes_and_edges_bulk itself, only
    # through deserialise_extraction directly.
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episode = _episode("a")
    key = seam.extraction_cache_key(episode, _FakeLLMClient(), None, None, {}, None)
    store.put(key, str(episode.uuid), "not json")
    stream = StringIO()
    logger = configure_logging(stream)

    with caplog.at_level(logging.DEBUG):
        nodes_bulk, edges_bulk = await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            [(episode, [])],
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
            logger=logger,
        )

    assert len(recorder.calls) == 1
    assert nodes_bulk[0][0].name == "node-a"
    assert edges_bulk[0][0].fact == "fact-a"
    # R4: the invalid payload is a safe closed event, never a raw line.
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (events[0]["event"], events[0]["cache_kind"], events[0]["cache_reason"]) == (
        "projection_cache_miss",
        "extraction",
        "payload_invalid",
    )
    assert caplog.records == []


@pytest.mark.anyio
async def test_a_none_store_calls_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), []),
        (_episode("b"), []),
    ]
    nodes_bulk, edges_bulk = await seam.cached_extract_nodes_and_edges_bulk(
        None,
        _clients(),
        episodes,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]) == 2
    assert nodes_bulk[0][0].name == "node-a"
    assert nodes_bulk[1][0].name == "node-b"


@pytest.mark.anyio
async def test_excluded_entity_types_bypass_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), []),
        (_episode("b"), []),
    ]
    # Pre-populate both keys; even so, the bypass must call through and
    # must not write to the store.
    for episode, _ in episodes:
        key = seam.extraction_cache_key(episode, _FakeLLMClient(), None, None, {}, None)
        store.put(key, str(episode.uuid), "should-not-be-read")

    await seam.cached_extract_nodes_and_edges_bulk(
        store,
        _clients(),
        episodes,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=["Skip"],
        custom_extraction_instructions=None,
    )
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]) == 2
    # No new rows were written by the bypass path.
    assert len(store.rows) == 2
    assert all(payload == "should-not-be-read" for payload in store.rows.values())


@pytest.mark.anyio
async def test_the_bulk_witness_logs_counts_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
    store = _FakeStore()
    episodes: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("sentinel-a", content="secret-a"), []),
        (_episode("sentinel-b", content="secret-b"), []),
        (_episode("sentinel-c", content="secret-c"), []),
    ]
    middle_key = seam.extraction_cache_key(
        episodes[1][0], _FakeLLMClient(), None, None, {}, None
    )
    cached_node = _node("cached")
    store.put(
        middle_key,
        str(episodes[1][0].uuid),
        seam.serialise_extraction(
            [cached_node], [_edge(cached_node.uuid, cached_node.uuid, "cached-fact")]
        ),
    )

    stream = StringIO()
    logger = configure_logging(stream)
    with caplog.at_level(logging.DEBUG):
        await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            episodes,
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
            logger=logger,
        )

    # R4: one safe witness event carrying counts only.
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [
        event
        for event in events
        if event["event"] == "projection_extraction_cache_batch"
    ] == [
        {
            "event": "projection_extraction_cache_batch",
            "cache_hits": 1,
            "cache_misses": 2,
            "time": events[-1]["time"],
        }
    ]
    assert "secret-a" not in stream.getvalue()
    assert "secret-b" not in stream.getvalue()
    assert "secret-c" not in stream.getvalue()
    assert "sentinel" not in stream.getvalue()
    assert caplog.records == []


def test_compatibility_guard_refuses_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seam, "_GRAPHITI_CORE_VERSION", "0.29.4")
    with pytest.raises(
        RuntimeError, match="graphiti_extraction_cache_compatibility_version"
    ):
        seam._require_graphiti_extraction_cache_compatibility()


@pytest.fixture(autouse=True)
def _restore_extract_edge_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    # Captures whatever _extract_edge_timestamps is at the START of this
    # test and restores exactly that object at teardown, regardless of
    # what the test (or install_edge_timestamp_caching) reassigns it to
    # meanwhile — the same device as test_graphiti_edge_batch.py's
    # _restore_prompt_library, and for the same xdist-safety reason.
    module: Any = edge_operations
    monkeypatch.setattr(
        edge_operations, "_extract_edge_timestamps", module._extract_edge_timestamps
    )


@pytest.mark.anyio
async def test_install_patches_and_is_idempotent() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient()
    seam.install_edge_timestamp_caching(store, llm_client)
    installed = edge_operations._extract_edge_timestamps
    assert getattr(installed, "_cairn_timestamp_cache", False) is True

    seam.install_edge_timestamp_caching(store, llm_client)

    assert edge_operations._extract_edge_timestamps is installed


@pytest.mark.anyio
async def test_two_clients_route_to_their_own_stores() -> None:
    # R1: the process-global wrapper closes over no store — each
    # installation attaches the store to its own LLM client, and the
    # wrapper resolves the store from the client on every call, so
    # sequential and concurrent Graphiti instances stay isolated.
    store_a = _FakeStore()
    store_b = _FakeStore()
    client_a = _FakeLLMClient(
        responses=[{"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None}]
    )
    client_b = _FakeLLMClient(
        responses=[{"valid_at": "2027-06-07T08:09:10Z", "invalid_at": None}]
    )
    seam.install_edge_timestamp_caching(store_a, client_a)
    seam.install_edge_timestamp_caching(store_b, client_b)
    episode = _episode()

    edge_a = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(client_a, edge_a, episode)
    assert len(client_a.calls) == 1
    assert len(store_a.rows) == 1
    assert store_b.rows == {}

    # Interleaved: the same fact through client_b shares the cache key
    # (same small model) but must not see store_a's row — its own store
    # is empty, so it calls its own provider and writes its own row.
    edge_b = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(client_b, edge_b, episode)
    assert len(client_b.calls) == 1
    assert len(store_b.rows) == 1
    assert edge_b.valid_at != edge_a.valid_at

    # Each client still hits its own store.
    edge_a_again = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(client_a, edge_a_again, episode)
    assert len(client_a.calls) == 1
    assert edge_a_again.valid_at == edge_a.valid_at


@pytest.mark.anyio
async def test_a_client_without_a_store_passes_through_unchanged() -> None:
    # R1: after one client installs the global wrapper, a client with no
    # attached store must behave exactly as the uncached upstream — no
    # cache reads, no cache writes, a provider call every time.
    store = _FakeStore()
    seam.install_edge_timestamp_caching(store, _FakeLLMClient())
    plain_client = _FakeLLMClient(
        responses=[
            {"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None},
            {"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None},
        ]
    )
    episode = _episode()

    for _ in range(2):
        edge = _edge("source", "target", "A moved to Bern")
        await edge_operations._extract_edge_timestamps(plain_client, edge, episode)
        assert edge.valid_at is not None

    assert len(plain_client.calls) == 2
    assert store.rows == {}


@pytest.mark.anyio
async def test_a_timestamp_miss_calls_the_provider_and_stores_the_result() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient(
        responses=[{"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None}]
    )
    seam.install_edge_timestamp_caching(store, llm_client)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")

    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert len(llm_client.calls) == 1
    assert edge.valid_at is not None
    assert edge.invalid_at is None
    key = seam.timestamps_cache_key(edge.fact, episode.valid_at, llm_client)
    assert seam._timestamps_from_payload(store.rows[(key, str(episode.uuid))]) == (
        edge.valid_at,
        edge.invalid_at,
    )


@pytest.mark.anyio
async def test_a_timestamp_hit_skips_the_provider_and_sets_the_edge() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient()
    seam.install_edge_timestamp_caching(store, llm_client)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")
    key = seam.timestamps_cache_key(edge.fact, episode.valid_at, llm_client)
    valid_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    store.put(key, str(episode.uuid), seam._timestamps_to_payload(valid_at, None))

    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert llm_client.calls == []
    assert edge.valid_at == valid_at
    assert edge.invalid_at is None


@pytest.mark.anyio
async def test_a_both_null_result_is_cached_and_served() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient(responses=[{"valid_at": None, "invalid_at": None}])
    seam.install_edge_timestamp_caching(store, llm_client)
    episode = _episode()
    first_edge = _edge("source", "target", "no time is mentioned here")

    await edge_operations._extract_edge_timestamps(llm_client, first_edge, episode)

    assert len(llm_client.calls) == 1
    assert first_edge.valid_at is None
    assert first_edge.invalid_at is None

    second_edge = _edge("source", "target", "no time is mentioned here")
    await edge_operations._extract_edge_timestamps(llm_client, second_edge, episode)

    assert len(llm_client.calls) == 1
    assert second_edge.valid_at is None
    assert second_edge.invalid_at is None


@pytest.mark.anyio
async def test_the_timestamp_seam_names_its_own_cache_kind() -> None:
    # R4 follow-up: seam 2 shares seam 1's table, so it must declare its
    # own kind on every store call — otherwise a store-level read or
    # write failure during timestamp caching is reported to operators as
    # an extraction-cache failure.
    store = _FakeStore()
    llm_client = _FakeLLMClient(
        responses=[{"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None}]
    )
    seam.install_edge_timestamp_caching(store, llm_client)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")

    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert store.kinds == [CacheKind.TIMESTAMPS, CacheKind.TIMESTAMPS]


@pytest.mark.anyio
async def test_the_bulk_seam_leaves_the_cache_kind_to_the_table() -> None:
    recorder = _Recorder()
    store = _FakeStore()
    episode = _episode("a")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)
        await seam.cached_extract_nodes_and_edges_bulk(
            store,
            _clients(),
            [(episode, [])],
            edge_type_map={},
            edge_types=None,
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
        )

    assert store.kinds == [None, None]


@pytest.mark.anyio
async def test_a_transient_failure_is_not_cached_and_a_retry_caches() -> None:
    # R2: graphiti 0.30.2 swallows provider failures and leaves both
    # fields null. Caching that state would make a temporary failure
    # indistinguishable from a valid "no timestamp" answer and suppress
    # every future retry — so a failed call writes no row, and the next
    # successful call retries and caches.
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    llm_client = _FakeLLMClient(responses=[])  # the first call raises
    seam.install_edge_timestamp_caching(store, llm_client, logger=logger)
    episode = _episode()

    edge = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)
    assert edge.valid_at is None
    assert edge.invalid_at is None
    assert store.rows == {}
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (events[0]["event"], events[0]["cache_kind"], events[0]["cache_reason"]) == (
        "projection_cache_miss",
        "timestamps",
        "timestamp_call_failed",
    )

    llm_client._responses = [{"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None}]
    retry_edge = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(llm_client, retry_edge, episode)
    assert retry_edge.valid_at is not None
    assert len(store.rows) == 1

    cached_edge = _edge("source", "target", "A moved to Bern")
    await edge_operations._extract_edge_timestamps(llm_client, cached_edge, episode)
    assert cached_edge.valid_at == retry_edge.valid_at
    assert len(llm_client.calls) == 2


@pytest.mark.anyio
async def test_an_unparseable_timestamp_is_not_cached() -> None:
    # R2: a response whose timestamp string does not parse is a failure,
    # not a valid "no timestamp" answer — the edge is left exactly as the
    # uncached upstream leaves it, and nothing is cached.
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    llm_client = _FakeLLMClient(
        responses=[{"valid_at": "not-a-date", "invalid_at": None}]
    )
    seam.install_edge_timestamp_caching(store, llm_client, logger=logger)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")

    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert edge.valid_at is None
    assert edge.invalid_at is None
    assert store.rows == {}
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (events[0]["event"], events[0]["cache_kind"], events[0]["cache_reason"]) == (
        "projection_cache_miss",
        "timestamps",
        "timestamp_parse_failed",
    )


@pytest.mark.anyio
async def test_an_edge_with_timestamps_is_left_alone() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient()
    seam.install_edge_timestamp_caching(store, llm_client)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")
    edge.valid_at = datetime(2020, 1, 1, tzinfo=UTC)

    await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert llm_client.calls == []
    assert store.rows == {}
    assert store.embeddings == {}


@pytest.mark.anyio
async def test_a_missing_episode_bypasses_the_cache() -> None:
    store = _FakeStore()
    llm_client = _FakeLLMClient()
    seam.install_edge_timestamp_caching(store, llm_client)
    edge = _edge("source", "target", "A moved to Bern")

    await edge_operations._extract_edge_timestamps(llm_client, edge, None)

    assert llm_client.calls == []
    assert store.rows == {}


@pytest.mark.anyio
async def test_a_corrupt_timestamp_payload_falls_through_to_the_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    llm_client = _FakeLLMClient(
        responses=[{"valid_at": "2026-01-02T03:04:05Z", "invalid_at": None}]
    )
    seam.install_edge_timestamp_caching(store, llm_client, logger=logger)
    episode = _episode()
    edge = _edge("source", "target", "A moved to Bern")
    key = seam.timestamps_cache_key(edge.fact, episode.valid_at, llm_client)
    store.put(key, str(episode.uuid), "not json")

    with caplog.at_level(logging.DEBUG):
        await edge_operations._extract_edge_timestamps(llm_client, edge, episode)

    assert len(llm_client.calls) == 1
    assert edge.valid_at is not None
    assert edge.invalid_at is None
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (events[0]["event"], events[0]["cache_kind"], events[0]["cache_reason"]) == (
        "projection_cache_miss",
        "timestamps",
        "payload_invalid",
    )
    assert caplog.records == []


class _RecordingEmbedder(OpenAIEmbedder):
    """A real OpenAIEmbedder subclass, not a duck type.

    ``build_caching_embedder`` is isinstance-checked against
    ``OpenAIEmbedder`` by graphiti's own provider wiring, so the fake
    inner client must genuinely be one — the same device as
    ``_FakeLLMClient`` above. The client is a placeholder: every
    overridden method below never touches it, so constructing a real
    ``AsyncOpenAI`` here would be pure overhead.
    """

    def __init__(self, config: OpenAIEmbedderConfig | None = None) -> None:
        super().__init__(config=config, client=cast(Any, object()))
        self.create_calls: list[Any] = []
        self.create_batch_calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        # Honour the configured dimension, as the real embedder does —
        # the cache validates stored rows against it.
        return [float(len(text))] * self.config.embedding_dim

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        self.create_calls.append(input_data)
        if isinstance(input_data, str):
            return self._vector(input_data)
        if (
            isinstance(input_data, list)
            and input_data
            and isinstance(input_data[0], str)
        ):
            return self._vector(input_data[0])
        return [1.0] * self.config.embedding_dim

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        self.create_batch_calls.append(list(input_data_list))
        return [self._vector(text) for text in input_data_list]


def _embedder_config(
    model: str = "text-embedding-3-small", dim: int = 8
) -> OpenAIEmbedderConfig:
    return OpenAIEmbedderConfig(embedding_model=model, embedding_dim=dim)


def test_the_cache_store_protocol_is_private() -> None:
    # R7/I-21: the protocol exists only to keep the projection module
    # independent of the concrete catalogue class — an internal layering
    # device, not an exported port.
    assert not hasattr(seam, "CacheStore")
    assert "_CacheStore" not in seam.__all__


def test_build_caching_embedder_refuses_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R6: every exported construction seam that could activate P-90
    # against another Graphiti version enforces the compatibility guard
    # itself, not incidentally via a neighbouring seam.
    monkeypatch.setattr(seam, "_GRAPHITI_CORE_VERSION", "0.29.4")
    with pytest.raises(
        RuntimeError, match="graphiti_extraction_cache_compatibility_version"
    ):
        seam.build_caching_embedder(
            _FakeStore(), _RecordingEmbedder(_embedder_config())
        )


@pytest.mark.anyio
async def test_a_repeated_single_embedding_is_served_from_the_store() -> None:
    store = _FakeStore()
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner)
    assert isinstance(caching, OpenAIEmbedder)

    first = await caching.create("hello world")
    second = await caching.create("hello world")

    assert first == second
    assert inner.create_calls == []
    assert inner.create_batch_calls == [["hello world"]]
    assert len(store.embeddings) == 1


@pytest.mark.anyio
async def test_create_batch_calls_through_only_the_misses_in_order() -> None:
    store = _FakeStore()
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner)
    texts = ["alpha", "bravo", "charlie"]
    cached_vector = await inner.create(texts[1])
    key = seam.embedding_cache_key(
        str(inner.config.embedding_model), inner.config.embedding_dim, texts[1]
    )
    store.put_embedding(key, json.dumps(cached_vector))
    inner.create_calls.clear()

    result = await caching.create_batch(texts)

    assert len(inner.create_batch_calls) == 1
    assert inner.create_batch_calls[0] == ["alpha", "charlie"]
    assert result[0] == inner._vector("alpha")
    assert result[1] == cached_vector
    assert result[2] == inner._vector("charlie")


@pytest.mark.anyio
async def test_create_batch_writes_every_miss_in_one_store_call() -> None:
    # I4: one fsync'd transaction for the whole batch of misses, not one
    # per vector.
    store = _FakeStore()
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner)
    texts = ["alpha", "bravo", "charlie"]

    result = await caching.create_batch(texts)

    assert len(store.put_embedding_many_calls) == 1
    written = dict(store.put_embedding_many_calls[0])
    assert len(written) == 3
    for text, vector in zip(texts, result, strict=True):
        key = seam.embedding_cache_key(
            str(inner.config.embedding_model), inner.config.embedding_dim, text
        )
        assert key in written
        # Individually readable after the batch write.
        assert store.get_embedding(key) == written[key]
        assert json.loads(written[key]) == vector


@pytest.mark.anyio
async def test_the_embedding_key_separates_models_and_dims() -> None:
    store = _FakeStore()
    inner_a = _RecordingEmbedder(_embedder_config(model="model-a", dim=4))
    inner_b = _RecordingEmbedder(_embedder_config(model="model-b", dim=8))
    caching_a = seam.build_caching_embedder(store, inner_a)
    caching_b = seam.build_caching_embedder(store, inner_b)

    await caching_a.create("same text")
    await caching_b.create("same text")

    assert len(store.embeddings) == 2


@pytest.mark.anyio
async def test_search_passthrough_bypasses_reads_and_writes() -> None:
    store = _FakeStore()
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner)
    await caching.create("cached text")
    assert len(store.embeddings) == 1
    inner.create_batch_calls.clear()

    with seam.search_embedding_passthrough():
        await caching.create("cached text")

    assert inner.create_calls == []
    assert inner.create_batch_calls == [["cached text"]]
    assert len(store.embeddings) == 1


@pytest.mark.anyio
async def test_token_iterable_input_bypasses_the_cache() -> None:
    store = _FakeStore()
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner)

    await caching.create([1, 2, 3])

    assert len(inner.create_calls) == 1
    assert store.embeddings == {}


@pytest.mark.anyio
async def test_a_corrupt_embedding_payload_is_a_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner, logger=logger)
    key = seam.embedding_cache_key(
        str(inner.config.embedding_model), inner.config.embedding_dim, "broken"
    )
    store.put_embedding(key, "not json")

    with caplog.at_level(logging.DEBUG):
        result = await caching.create("broken")

    assert inner.create_calls == []
    assert inner.create_batch_calls == [["broken"]]
    assert result == inner._vector("broken")
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (events[0]["event"], events[0]["cache_kind"], events[0]["cache_reason"]) == (
        "projection_cache_miss",
        "embedding",
        "payload_invalid",
    )
    assert caplog.records == []


@pytest.mark.anyio
async def test_a_wrong_dimension_embedding_payload_is_a_miss() -> None:
    # A corrupt row that is still valid JSON — an empty or truncated
    # vector — must degrade to a miss like any other corruption, never
    # violate the embedder's fixed-dimension contract as a "hit".
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner, logger=logger)
    for text, payload in (("short", [1.0]), ("empty", [])):
        key = seam.embedding_cache_key(
            str(inner.config.embedding_model), inner.config.embedding_dim, text
        )
        store.put_embedding(key, json.dumps(payload))

    single = await caching.create("short")
    batch = await caching.create_batch(["empty"])

    assert inner.create_calls == []
    assert inner.create_batch_calls == [["short"], ["empty"]]
    assert single == inner._vector("short")
    assert batch == [inner._vector("empty")]
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    misses = [
        (event["cache_kind"], event["cache_reason"])
        for event in events
        if event["event"] == "projection_cache_miss"
    ]
    assert misses == [("embedding", "payload_invalid")] * 2


@pytest.mark.anyio
async def test_create_batch_emits_a_safe_witness() -> None:
    # R4: counts only, on the batch path.
    store = _FakeStore()
    stream = StringIO()
    logger = configure_logging(stream)
    inner = _RecordingEmbedder(_embedder_config())
    caching = seam.build_caching_embedder(store, inner, logger=logger)
    cached_vector = await inner.create("bravo")
    key = seam.embedding_cache_key(
        str(inner.config.embedding_model), inner.config.embedding_dim, "bravo"
    )
    store.put_embedding(key, json.dumps(cached_vector))

    await caching.create_batch(["alpha", "bravo", "charlie"])

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    witnesses = [
        event
        for event in events
        if event["event"] == "projection_embedding_cache_batch"
    ]
    assert len(witnesses) == 1
    assert witnesses[0]["cache_hits"] == 1
    assert witnesses[0]["cache_misses"] == 2
    assert "alpha" not in stream.getvalue()


def test_the_seam_owns_no_ordinary_logger() -> None:
    # R4: with no safe logger threaded, the seam is silent — there is no
    # module-level fallback logger for raw lines to escape through.
    assert not hasattr(seam, "_LOGGER")


# --- Task 6: wiring the seams into ``_CairnGraphiti``/``_construct_graphiti`` --


class _FakeFalkorDB:
    """The barest FalkorDB stand-in ``_construct_graphiti`` needs: nothing
    in these tests ever queries it, only ``Graphiti.__init__`` storing the
    driver."""

    def select_graph(self, name: str) -> Any:
        return SimpleNamespace()


@pytest.mark.anyio
async def test_the_bulk_override_passes_the_store_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(seam, "extract_nodes_and_edges_bulk", recorder)

    async def _stub_dedupe(
        *args: Any, **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return {}, {}

    monkeypatch.setattr(
        graphiti_bulk_module, "dedupe_nodes_bulk_incremental", _stub_dedupe
    )
    store = _FakeStore()
    instance = object.__new__(graphiti_module._CairnGraphiti)
    instance._cairn_extraction_cache = store
    instance.clients = _clients()
    episode_context: list[tuple[EpisodicNode, list[EpisodicNode]]] = [
        (_episode("a"), [])
    ]

    await instance._extract_and_dedupe_nodes_bulk(
        episode_context,
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
    )

    assert len(store.rows) == 1


@pytest.mark.anyio
async def test_the_bulk_override_preserves_the_request_scoped_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graphiti 0.30.2 isolates concurrent groups with a per-request
    driver in this bundle. Replacing it with the instance bundle would
    send extraction and dedupe reads to whichever graph the instance owns."""
    calls: list[tuple[str, object]] = []

    async def extract(
        store: object,
        clients: object,
        episode_context: object,
        **kwargs: object,
    ) -> tuple[list[list[EntityNode]], list[list[EntityEdge]]]:
        calls.append(("extract", clients))
        return [[]], [[]]

    async def dedupe(
        clients: object,
        extracted_nodes: object,
        episode_context: object,
        entity_types: object,
    ) -> tuple[dict[str, list[EntityNode]], dict[str, str]]:
        calls.append(("dedupe", clients))
        return {}, {}

    monkeypatch.setattr(graphiti_module, "cached_extract_nodes_and_edges_bulk", extract)
    monkeypatch.setattr(graphiti_bulk_module, "dedupe_nodes_bulk_incremental", dedupe)
    instance = object.__new__(graphiti_module._CairnGraphiti)
    instance._cairn_extraction_cache = None
    instance._cairn_safe_logger = None
    instance_clients = _clients()
    request_clients = _clients()
    instance.clients = instance_clients

    await instance._extract_and_dedupe_nodes_bulk(
        [(_episode("a"), [])],
        edge_type_map={},
        edge_types=None,
        entity_types=None,
        excluded_entity_types=None,
        clients=request_clients,
    )

    assert calls == [("extract", request_clients), ("dedupe", request_clients)]
    assert instance.clients is instance_clients


def test_construct_graphiti_installs_the_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        store = _FakeStore()
        graphiti = graphiti_module._construct_graphiti(
            BoundedFalkorDriver(falkor_db=_FakeFalkorDB(), concurrency_limit=1),
            llm_client,
            embedder,
            cross_encoder,
            edge_batch_size=1,
            extraction_cache=store,
        )
        assert graphiti._cairn_extraction_cache is store
        assert (
            getattr(
                edge_operations._extract_edge_timestamps,
                "_cairn_timestamp_cache",
                False,
            )
            is True
        )
        assert isinstance(graphiti.embedder, seam._CachingEmbedder)
    finally:
        asyncio.run(shared.close())


def test_construct_graphiti_without_a_store_leaves_extraction_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    shared, llm_client, embedder, cross_encoder = (
        graphiti_module._openai_provider_clients()
    )
    try:
        graphiti = graphiti_module._construct_graphiti(
            BoundedFalkorDriver(falkor_db=_FakeFalkorDB(), concurrency_limit=1),
            llm_client,
            embedder,
            cross_encoder,
            edge_batch_size=1,
        )
        assert graphiti._cairn_extraction_cache is None
        assert (
            getattr(
                edge_operations._extract_edge_timestamps,
                "_cairn_timestamp_cache",
                False,
            )
            is False
        )
        assert isinstance(graphiti.embedder, OpenAIEmbedder)
        assert graphiti.embedder.config is embedder.config
        assert graphiti.embedder.client is embedder.client
    finally:
        asyncio.run(shared.close())
