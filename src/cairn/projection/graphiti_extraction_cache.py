"""Pinned Graphiti bulk-extraction caching seam (P-90 seam 1).

Graphiti 0.30.2's ``extract_nodes_and_edges_bulk`` issues one provider
call per episode with no cache of its own: reprojecting the same
episode content re-extracts nodes and edges from scratch every time.
This module caches the library's own extraction output per episode in
the catalogue, keyed off the extraction inputs that vary independently
of the episode's content, including a call to ``_get_model_for_size`` —
a private method of the pinned library's ``LLMClient``
(``openai_base_client.py:111``); the compatibility guard below is what
licenses calling it. Delete this seam when the pinned dependency caches
extraction itself.

The key is content-scoped, not episode-scoped, and deliberately does
not cover every input the extraction prompt consumes. ``reference_time``
(the episode's ``valid_at``) enters the prompt but is not key material,
because a fact's ``reference_time`` is stable and reproduces identically
on rebuild. ``excluded_entity_types`` has no field in the accepted key,
so it bypasses the cache entirely rather than being served blind. Two
distinct facts with byte-identical bodies would otherwise collide on the
same key; ``cached_extract_nodes_and_edges_bulk`` excludes that by
verifying the stored row's ``fact_id`` on read, not by the key itself.

Three further prompt inputs are unkeyed and, unlike the two above, are
known gaps rather than settled choices. They do not share a rationale.
Two are inert only while Cairn calls ``add_episode_bulk`` without
``entity_types`` or ``edge_types``, both of which the library then
defaults to ``None``: ``_types_fingerprint`` covers entity-type field
names only, while the pinned library feeds each type's ``__doc__`` into
the extraction prompt, and ``edge_types`` reaches the edge prompt as
``fact_type_description`` but is neither key material nor a bypass
trigger. The third is **not** inert — the previous-episode window (the
three most recent same-partition episodes, read live from the graph)
enters both prompts and is not key material, so a hit can serve
extraction produced under a different window. It varies through failure
reordering, ``valid_at`` ties and chunk composition; the effect is
semantic drift on already nondeterministic model output rather than a
broken invariant, and it cannot arise under the default
``chunk_size`` of 1, which never reaches the bulk path. Configuring
custom types, or keying the window, needs these closed first — see
migration 0007's comment and the P-90 remediation handoff.

Seam 1's pinned shapes: the key (``extraction_cache_key``) is a sha256
of canonical JSON over ``episode_sha``, ``graphiti_core``, ``llm_model``,
``small_model``, ``entity_types``, ``edge_type_map`` and
``instructions``; the value (``serialise_extraction`` /
``deserialise_extraction``) is canonical JSON
``{"nodes": [...], "edges": [...]}``, a direct pass-through of each
library model's own ``model_dump(mode="json")`` / ``model_validate``.

This module also houses Seam 2 (``install_edge_timestamp_caching``,
P-90 task 4), which shares this cache's catalogue table under a
different key shape: a sha256 of canonical JSON over the discriminator
``kind: "edge_timestamps"``, ``fact_sha``, ``reference_time``,
``graphiti_core`` and ``small_model`` (this key is new design, approved
2026-08-29, unlike seam 1's locked key). Its value is canonical JSON
``{"valid_at": iso-or-null, "invalid_at": iso-or-null}``. Unlike seam
1, seam 2's installer mutates a process global —
``edge_operations._extract_edge_timestamps`` — rebinding it to a
caching wrapper; any test calling ``install_edge_timestamp_caching``
must restore that global afterwards, as this module's own autouse
fixture does.

Seam 3 (``build_caching_embedder``, P-90 task 5) caches the embedder
client itself, in a third catalogue table (``projection_embedding_cache``,
via ``_CacheStore.get_embedding``/``put_embedding``). Its key is a sha256
of canonical JSON over ``kind: "embedding"``, ``model``, ``dim`` and
``text_sha``; ``dim`` is new design, approved 2026-08-29 — the accepted
design's key was (model, text sha256) only, but ``OpenAIEmbedder.create``
truncates to ``config.embedding_dim``, so two dims produce different
valid answers from the same (model, text) pair. Its value is
``json.dumps`` of the float vector. Unlike seams 1 and 2, seam 3 is a
subclass (``_CachingEmbedder(OpenAIEmbedder)``) composed in by the
caller rather than a process-global patch, and it has a
``search_embedding_passthrough`` escape hatch: graphiti's own search
path must never see stale embeddings, so that context manager bypasses
both cache reads and writes for the duration of a search call.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from datetime import datetime
from importlib.metadata import version
from typing import Any, Protocol, cast

from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.openai import OpenAIEmbedder
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.extract_edges import EdgeTimestamps
from graphiti_core.utils.bulk_utils import extract_nodes_and_edges_bulk
from graphiti_core.utils.datetime_utils import ensure_utc
from graphiti_core.utils.maintenance import edge_operations
from pydantic import BaseModel

from cairn.runtime.logging import CacheKind, CacheReason, LogEvent, SafeLogger

_GRAPHITI_CORE_VERSION = version("graphiti-core")
_GRAPHITI_EXTRACTION_CACHE_COMPATIBILITY_VERSION = "0.30.2"


def _require_graphiti_extraction_cache_compatibility() -> None:
    if _GRAPHITI_CORE_VERSION != _GRAPHITI_EXTRACTION_CACHE_COMPATIBILITY_VERSION:
        raise RuntimeError("graphiti_extraction_cache_compatibility_version")


def _emit_miss(logger: SafeLogger | None, kind: CacheKind, reason: CacheReason) -> None:
    # R4/I-32: closed enums on the safe stream, or nothing at all — the
    # seam owns no ordinary logger for a raw fallback line to escape by.
    if logger is not None:
        logger.emit(
            LogEvent.PROJECTION_CACHE_MISS,
            level=logging.WARNING,
            transport=None,
            cache_kind=kind,
            cache_reason=reason,
        )


class _CacheStore(Protocol):
    """The private cache store methods, named structurally.

    Declared here rather than imported so this projection-layer module
    stays free of a dependency on ``cairn.catalogue``; composition
    passes the real ``ExtractionCacheStore`` through at the call site.
    """

    def get(
        self,
        cache_key: str,
        fact_id: str | None = None,
        *,
        kind: CacheKind | None = None,
    ) -> str | None: ...
    def put(
        self,
        cache_key: str,
        fact_id: str,
        payload: str,
        *,
        kind: CacheKind | None = None,
    ) -> None: ...
    def put_many(self, rows: list[tuple[str, str, str]]) -> None: ...
    def get_embedding(self, cache_key: str) -> str | None: ...
    def put_embedding(self, cache_key: str, payload: str) -> None: ...
    def put_embedding_many(self, rows: list[tuple[str, str]]) -> None: ...
    def _repair_embedding_many(self, rows: list[tuple[str, str, str]]) -> None: ...


def _types_fingerprint(types: dict[str, type[BaseModel]] | None) -> str:
    if types is None:
        return "null"
    material = {name: sorted(model.model_fields) for name, model in types.items()}
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _edge_type_map_fingerprint(edge_type_map: dict[tuple[str, str], list[str]]) -> str:
    material = {
        f"{source}|{target}": sorted(names)
        for (source, target), names in edge_type_map.items()
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extraction_cache_key(
    episode: EpisodicNode,
    llm_client: Any,
    entity_types: dict[str, type[BaseModel]] | None,
    edge_types: dict[str, type[BaseModel]] | None,
    edge_type_map: dict[tuple[str, str], list[str]],
    custom_extraction_instructions: str | None,
) -> str:
    # edge_types is accepted for interface parity with the call site but is
    # deliberately not key material: the accepted design's key has no field
    # for it (locked, see the P-90 extraction-cache proposal).
    del edge_types
    _require_graphiti_extraction_cache_compatibility()
    material = {
        "episode_sha": hashlib.sha256(episode.content.encode("utf-8")).hexdigest(),
        "graphiti_core": _GRAPHITI_CORE_VERSION,
        "llm_model": llm_client._get_model_for_size(ModelSize.medium),
        "small_model": llm_client._get_model_for_size(ModelSize.small),
        "entity_types": _types_fingerprint(entity_types),
        "edge_type_map": _edge_type_map_fingerprint(edge_type_map),
        "instructions": custom_extraction_instructions or "",
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialise_extraction(nodes: list[EntityNode], edges: list[EntityEdge]) -> str:
    payload = {
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "edges": [edge.model_dump(mode="json") for edge in edges],
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def deserialise_extraction(
    payload: str,
) -> tuple[list[EntityNode], list[EntityEdge]] | None:
    try:
        data = json.loads(payload)
        nodes = [EntityNode.model_validate(item) for item in data["nodes"]]
        edges = [EntityEdge.model_validate(item) for item in data["edges"]]
    except Exception:
        # ANY exception (malformed JSON, wrong shape, a pydantic
        # ValidationError) is a cache miss, not a caller-visible error.
        return None
    return nodes, edges


async def cached_extract_nodes_and_edges_bulk(
    store: _CacheStore | None,
    clients: GraphitiClients,
    episode_context: list[tuple[EpisodicNode, list[EpisodicNode]]],
    *,
    edge_type_map: dict[tuple[str, str], list[str]],
    edge_types: dict[str, type[BaseModel]] | None,
    entity_types: dict[str, type[BaseModel]] | None,
    excluded_entity_types: list[str] | None,
    custom_extraction_instructions: str | None,
    logger: SafeLogger | None = None,
) -> tuple[list[list[EntityNode]], list[list[EntityEdge]]]:
    if store is None or excluded_entity_types is not None:
        # excluded_entity_types has no field in the accepted key: rather
        # than serve a key blind to it, bypass the cache entirely.
        return await extract_nodes_and_edges_bulk(
            clients,
            episode_context,
            edge_type_map=edge_type_map,
            edge_types=edge_types,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=custom_extraction_instructions,
        )
    keys = [
        extraction_cache_key(
            episode,
            clients.llm_client,
            entity_types,
            edge_types,
            edge_type_map,
            custom_extraction_instructions,
        )
        for episode, _ in episode_context
    ]
    nodes_bulk: list[list[EntityNode] | None] = [None] * len(episode_context)
    edges_bulk: list[list[EntityEdge] | None] = [None] * len(episode_context)
    misses: list[int] = []
    for index, key in enumerate(keys):
        # C1: the key is content-scoped, so a row can be owned by a
        # different fact with byte-identical content. Verifying fact_id
        # on this read is what keeps that fact's identity — group_id,
        # episodes, uuids — from being replayed onto this episode.
        fact_id = str(episode_context[index][0].uuid)
        payload = await asyncio.to_thread(store.get, key, fact_id)
        cached = None if payload is None else deserialise_extraction(payload)
        if payload is not None and cached is None:
            _emit_miss(logger, CacheKind.EXTRACTION, CacheReason.PAYLOAD_INVALID)
        if cached is None:
            misses.append(index)
        else:
            nodes_bulk[index], edges_bulk[index] = cached
    if misses:
        miss_nodes, miss_edges = await extract_nodes_and_edges_bulk(
            clients,
            [episode_context[index] for index in misses],
            edge_type_map=edge_type_map,
            edge_types=edge_types,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=custom_extraction_instructions,
        )
        rows: list[tuple[str, str, str]] = []
        for position, index in enumerate(misses):
            nodes_bulk[index] = miss_nodes[position]
            edges_bulk[index] = miss_edges[position]
            rows.append(
                (
                    keys[index],
                    str(episode_context[index][0].uuid),
                    serialise_extraction(miss_nodes[position], miss_edges[position]),
                )
            )
        await asyncio.to_thread(store.put_many, rows)
    if logger is not None:
        logger.emit(
            LogEvent.PROJECTION_EXTRACTION_CACHE_BATCH,
            transport=None,
            cache_hits=len(episode_context) - len(misses),
            cache_misses=len(misses),
        )
    # Every position was set above, either from a cache hit or from the
    # miss merge — but that invariant is not visible to mypy through two
    # separate loops. An explicit assert converts a broken invariant into
    # a loud failure instead of the silent order/count corruption a bare
    # `if x is not None` filter would produce here.
    assert all(nodes is not None for nodes in nodes_bulk)
    assert all(edges is not None for edges in edges_bulk)
    return (
        cast(list[list[EntityNode]], nodes_bulk),
        cast(list[list[EntityEdge]], edges_bulk),
    )


def timestamps_cache_key(fact: str, reference_time: datetime, llm_client: Any) -> str:
    _require_graphiti_extraction_cache_compatibility()
    material = {
        "kind": "edge_timestamps",
        "fact_sha": hashlib.sha256(fact.encode("utf-8")).hexdigest(),
        "reference_time": reference_time.isoformat(),
        "graphiti_core": _GRAPHITI_CORE_VERSION,
        "small_model": llm_client._get_model_for_size(ModelSize.small),
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamps_to_payload(
    valid_at: datetime | None, invalid_at: datetime | None
) -> str:
    payload = {
        "valid_at": valid_at.isoformat() if valid_at is not None else None,
        "invalid_at": invalid_at.isoformat() if invalid_at is not None else None,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _timestamps_from_payload(
    payload: str,
) -> tuple[datetime | None, datetime | None] | None:
    try:
        data = json.loads(payload)
        valid_at = (
            datetime.fromisoformat(data["valid_at"])
            if data["valid_at"] is not None
            else None
        )
        invalid_at = (
            datetime.fromisoformat(data["invalid_at"])
            if data["invalid_at"] is not None
            else None
        )
    except Exception:
        # ANY exception (malformed JSON, wrong shape, an unparseable
        # timestamp string) is a cache miss, not a caller-visible error.
        return None
    return valid_at, invalid_at


# R1: the store (and its safe logger, R4) rides on the LLM client
# instance, never in the wrapper's closure — a process global that closed
# over the first store would route every later Graphiti instance's
# timestamps into the first instance's catalogue.
_TIMESTAMP_STORE_ATTRIBUTE = "_cairn_timestamp_cache_store"


def install_edge_timestamp_caching(
    store: _CacheStore, llm_client: Any, logger: SafeLogger | None = None
) -> None:
    _require_graphiti_extraction_cache_compatibility()
    setattr(llm_client, _TIMESTAMP_STORE_ATTRIBUTE, (store, logger))
    module: Any = edge_operations
    if getattr(module._extract_edge_timestamps, "_cairn_timestamp_cache", False):
        return
    original = module._extract_edge_timestamps

    async def cached_extract_edge_timestamps(
        llm_client: Any,
        edge: EntityEdge,
        episode: EpisodicNode | None,
    ) -> None:
        installed: tuple[_CacheStore, SafeLogger | None] | None = getattr(
            llm_client, _TIMESTAMP_STORE_ATTRIBUTE, None
        )
        if installed is None:
            # A client no installation touched: pure upstream behaviour.
            await original(llm_client, edge, episode)
            return
        store, logger = installed
        # Replicates the original's own guards before any cache work: an
        # edge that already has a timestamp, or a call with no usable
        # reference time, must behave exactly as the uncached original.
        if edge.valid_at is not None or edge.invalid_at is not None:
            return
        if episode is None or episode.valid_at is None:
            return
        key = timestamps_cache_key(edge.fact, episode.valid_at, llm_client)
        payload = await asyncio.to_thread(store.get, key, kind=CacheKind.TIMESTAMPS)
        if payload is not None:
            parsed = _timestamps_from_payload(payload)
            if parsed is not None:
                edge.valid_at, edge.invalid_at = parsed
                return
            _emit_miss(logger, CacheKind.TIMESTAMPS, CacheReason.PAYLOAD_INVALID)
        await _extract_and_cache_timestamps(
            store, llm_client, edge, episode, key, logger
        )

    wrapper: Any = cached_extract_edge_timestamps
    wrapper._cairn_timestamp_cache = True
    module._extract_edge_timestamps = wrapper


async def _extract_and_cache_timestamps(
    store: _CacheStore,
    llm_client: Any,
    edge: EntityEdge,
    episode: EpisodicNode,
    key: str,
    logger: SafeLogger | None,
) -> None:
    """The pinned library's own timestamp call, owned here (R2).

    Graphiti 0.30.2's ``_extract_edge_timestamps`` swallows provider,
    validation and parsing failures, leaving both fields null — a state
    indistinguishable from a valid "no timestamp" answer. Owning the call
    (guards, prompt, response model, model size and UTC normalisation
    matched to the upstream exactly) is what lets the seam cache only a
    completed extraction: a valid both-null answer is cacheable, any
    failure is logged safely and never cached, so the next call retries.
    The edge itself ends up exactly as the uncached upstream leaves it.
    No exception text, edge identifier, fact or timestamp reaches a log.
    """
    assert episode.valid_at is not None  # guarded by the caller
    context = {
        "fact": edge.fact,
        "reference_time": episode.valid_at.isoformat(),
    }
    try:
        llm_response = await llm_client.generate_response(
            prompt_library.extract_edges.extract_timestamps(context),
            response_model=EdgeTimestamps,
            model_size=ModelSize.small,
            prompt_name="extract_edges.extract_timestamps",
        )
        timestamps = EdgeTimestamps(**llm_response)
    except Exception:
        _emit_miss(logger, CacheKind.TIMESTAMPS, CacheReason.TIMESTAMP_CALL_FAILED)
        return
    completed = True
    if timestamps.valid_at:
        try:
            edge.valid_at = ensure_utc(
                datetime.fromisoformat(timestamps.valid_at.replace("Z", "+00:00"))
            )
        except ValueError:
            completed = False
    if timestamps.invalid_at:
        try:
            edge.invalid_at = ensure_utc(
                datetime.fromisoformat(timestamps.invalid_at.replace("Z", "+00:00"))
            )
        except ValueError:
            completed = False
    if not completed:
        _emit_miss(logger, CacheKind.TIMESTAMPS, CacheReason.TIMESTAMP_PARSE_FAILED)
        return
    await asyncio.to_thread(
        store.put,
        key,
        str(episode.uuid),
        _timestamps_to_payload(edge.valid_at, edge.invalid_at),
        kind=CacheKind.TIMESTAMPS,
    )


_SEARCH_PASSTHROUGH: ContextVar[bool] = ContextVar("_SEARCH_PASSTHROUGH", default=False)


@contextlib.contextmanager
def search_embedding_passthrough() -> Iterator[None]:
    # A ContextVar (rather than a plain module global) so the setting
    # propagates into tasks graphiti's own semaphore_gather fan-out
    # spawns during a search call, without a thread/task-unsafe global.
    token = _SEARCH_PASSTHROUGH.set(True)
    try:
        yield
    finally:
        _SEARCH_PASSTHROUGH.reset(token)


def embedding_cache_key(model: str, dim: int, text: str) -> str:
    material = {
        "kind": "embedding",
        "model": model,
        "dim": dim,
        "text_sha": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cacheable_text(
    input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]],
) -> str | None:
    # The only shapes projection uses (nodes.py:509, edges.py:291 in the
    # pinned graphiti_core); token iterables are search/library-internal
    # shapes this seam doesn't key for, so they bypass to the inner
    # embedder unchanged.
    if isinstance(input_data, str):
        return input_data
    if isinstance(input_data, list) and len(input_data) == 1:
        item = input_data[0]
        if isinstance(item, str):
            return item
    return None


def _payload_to_vector(payload: str, dim: int) -> list[float] | None:
    try:
        return _validate_vector(json.loads(payload), dim)
    except Exception:
        # ANY exception (malformed JSON, wrong shape) is a cache miss,
        # not a caller-visible error.
        return None


def _validate_vector(data: object, dim: int) -> list[float]:
    if not isinstance(data, list) or len(data) != dim:
        raise ValueError("embedding_vector_invalid")
    if not all(
        isinstance(item, int | float) and not isinstance(item, bool) for item in data
    ):
        raise ValueError("embedding_vector_invalid")
    try:
        vector = [float(item) for item in data]
    except (OverflowError, ValueError):
        raise ValueError("embedding_vector_invalid") from None
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("embedding_vector_invalid")
    # hypot avoids spurious overflow/underflow from squaring finite components.
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding_vector_invalid")
    return vector


def _validate_batch(data: object, count: int, dim: int) -> list[list[float]]:
    if not isinstance(data, list) or len(data) != count:
        raise ValueError("embedding_batch_invalid")
    return [_validate_vector(vector, dim) for vector in data]


class _CachingEmbedder(OpenAIEmbedder):
    def __init__(
        self,
        store: _CacheStore | None,
        inner: OpenAIEmbedder,
        logger: SafeLogger | None = None,
    ) -> None:
        # Same config and client as the inner embedder, purely so this
        # instance carries the same shape (isinstance(..., OpenAIEmbedder)
        # is checked by graphiti's own provider wiring). The miss path
        # below calls `inner` itself, polymorphically, rather than the
        # base class's own methods. Text uses the batch override so the
        # pinned create() cannot discard surplus results before validation.
        super().__init__(config=inner.config, client=inner.client)
        self._store = store
        self._inner = inner
        self._logger = logger

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        text = _cacheable_text(input_data)
        if text is None:
            # Preserve legacy multi-text/token iterable semantics and identity.
            # Only the returned scalar vector is observable: inner.create()
            # selects the first provider result. No raw count claim here.
            return _validate_vector(
                await self._inner.create(input_data), self.config.embedding_dim
            )
        return (await self.create_batch([text]))[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if self._store is None or _SEARCH_PASSTHROUGH.get():
            return _validate_batch(
                await self._inner.create_batch(input_data_list),
                len(input_data_list),
                self.config.embedding_dim,
            )
        keys = [
            embedding_cache_key(
                str(self.config.embedding_model), self.config.embedding_dim, text
            )
            for text in input_data_list
        ]
        # One read/recomputation/repair per distinct key; restore caller order
        # (including duplicate texts) only after the whole response is valid.
        texts = dict(zip(keys, input_data_list, strict=True))
        results: dict[str, list[float]] = {}
        misses: list[str] = []
        invalid: dict[str, str] = {}
        for key in texts:
            payload = await asyncio.to_thread(self._store.get_embedding, key)
            vector = (
                None
                if payload is None
                else _payload_to_vector(payload, self.config.embedding_dim)
            )
            if payload is not None and vector is None:
                invalid[key] = payload
                _emit_miss(
                    self._logger, CacheKind.EMBEDDING, CacheReason.PAYLOAD_INVALID
                )
            if vector is None:
                misses.append(key)
            else:
                results[key] = vector
        if misses:
            miss_vectors = _validate_batch(
                await self._inner.create_batch([texts[key] for key in misses]),
                len(misses),
                self.config.embedding_dim,
            )
            # No publication until every vector and the exact count pass.
            # Ordinary misses retain insert-ignore; only successfully read
            # invalid payloads authorise bounded compare-and-swap repair.
            rows: list[tuple[str, str]] = []
            repairs: list[tuple[str, str, str]] = []
            for key, vector in zip(misses, miss_vectors, strict=True):
                results[key] = vector
                payload = json.dumps(vector, allow_nan=False)
                if key in invalid:
                    repairs.append((key, invalid[key], payload))
                else:
                    rows.append((key, payload))
            if rows:
                await asyncio.to_thread(self._store.put_embedding_many, rows)
            if repairs:
                await asyncio.to_thread(self._store._repair_embedding_many, repairs)
        if self._logger is not None:
            miss_keys = set(misses)
            miss_count = sum(key in miss_keys for key in keys)
            self._logger.emit(
                LogEvent.PROJECTION_EMBEDDING_CACHE_BATCH,
                transport=None,
                cache_hits=len(input_data_list) - miss_count,
                cache_misses=miss_count,
            )
        return [results[key] for key in keys]


def build_caching_embedder(
    store: _CacheStore | None, inner: OpenAIEmbedder, logger: SafeLogger | None = None
) -> OpenAIEmbedder:
    # R6: this seam activates P-90 by itself, so it enforces the
    # compatibility guard itself rather than relying on a neighbouring
    # installer to have refused first.
    _require_graphiti_extraction_cache_compatibility()
    return _CachingEmbedder(store, inner, logger)


__all__ = [
    "build_caching_embedder",
    "cached_extract_nodes_and_edges_bulk",
    "deserialise_extraction",
    "embedding_cache_key",
    "extraction_cache_key",
    "install_edge_timestamp_caching",
    "search_embedding_passthrough",
    "serialise_extraction",
    "timestamps_cache_key",
]
