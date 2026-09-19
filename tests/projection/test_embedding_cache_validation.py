"""Validation and persistent recovery through the real SQLite cache seam."""

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

from cairn.catalogue import extraction_cache as cache_module
from cairn.catalogue.extraction_cache import ExtractionCacheStore
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import read_connection
from cairn.projection import graphiti_extraction_cache as seam
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import configure_logging


def _store(tmp_path: Path) -> ExtractionCacheStore:
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=tmp_path, credentials=tmp_path / "credentials"),
    )
    migrate_catalogue(config, lambda: datetime(2026, 9, 10, tzinfo=UTC))
    return ExtractionCacheStore(tmp_path, writer_gate=threading.Lock())


class _Provider:
    """Only the network boundary is replaced; Graphiti's embedder stays real."""

    def __init__(self, vectors: Any = None) -> None:
        self.vectors = [[3.0, 4.0]] if vectors is None else vectors
        self.calls: list[tuple[Any, str]] = []
        self.before_response: Callable[[], None] = lambda: None
        self.embeddings = self

    async def create(self, *, input: Any, model: str) -> Any:
        self.calls.append((input, model))
        self.before_response()
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=vector) for vector in self.vectors]
        )


def _inner(provider: _Provider) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        config=OpenAIEmbedderConfig(embedding_dim=2), client=cast(Any, provider)
    )


def _key(text: str) -> str:
    return seam.embedding_cache_key("text-embedding-3-small", 2, text)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "poison",
    [
        "not json",
        "null",
        "1",
        "{}",
        "[]",
        "[1]",
        "[1,2,3]",
        "[true,1]",
        '["3",4]',
        "[NaN,1]",
        "[Infinity,1]",
        "[-Infinity,1]",
        "[0,-0.0]",
        "[1.7e308,1.7e308]",
        "[" + "9" * 400 + ",1]",
    ],
)
async def test_poison_is_repaired_and_a_reopened_store_hits(
    tmp_path: Path, poison: str
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("fact"), poison)
    provider = _Provider()
    assert await seam.build_caching_embedder(store, _inner(provider)).create(
        "fact"
    ) == [3.0, 4.0]
    reopened = ExtractionCacheStore(tmp_path, writer_gate=threading.Lock())
    assert await seam.build_caching_embedder(reopened, _inner(provider)).create(
        ["fact"]
    ) == [3.0, 4.0]
    assert provider.calls == [(["fact"], "text-embedding-3-small")]
    assert json.loads(reopened.get_embedding(_key("fact")) or "null") == [3.0, 4.0]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["cached", "disabled", "query"])
@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[3, 4], [5, 6]],
        [[1]],
        [[True, 1]],
        [["3", 4]],
        [[float("nan"), 1]],
        [[float("inf"), 1]],
        [[0, 0]],
        [[1.7e308, 1.7e308]],
    ],
)
async def test_invalid_single_text_response_never_publishes(
    tmp_path: Path, mode: str, single: bool, vectors: Any
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("fact"), "[0,0]")
    provider = _Provider(vectors)
    wrapper = seam.build_caching_embedder(
        None if mode == "disabled" else store, _inner(provider)
    )

    async def invoke() -> None:
        if single:
            await wrapper.create("fact")
        else:
            await wrapper.create_batch(["fact"])

    with pytest.raises(ValueError, match="^embedding_(vector|batch)_invalid$"):
        if mode == "query":
            with seam.search_embedding_passthrough():
                await invoke()
        else:
            await invoke()
    assert store.get_embedding(_key("fact")) == "[0,0]"
    assert provider.calls == [(["fact"], "text-embedding-3-small")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "vectors", [[[3, 4], [0, 0]], [[3, 4]], [[3, 4], [5, 6], [7, 8]]]
)
async def test_whole_invalid_batch_preserves_poison_hits_and_absent_rows(
    tmp_path: Path, vectors: Any
) -> None:
    store = _store(tmp_path)
    store.put_embedding_many([(_key("poison"), "[0,0]"), (_key("hit"), "[6,8]")])
    provider = _Provider(vectors)
    with pytest.raises(ValueError, match="^embedding_(vector|batch)_invalid$"):
        await seam.build_caching_embedder(store, _inner(provider)).create_batch(
            ["hit", "absent", "poison"]
        )
    assert store.get_embedding(_key("hit")) == "[6,8]"
    assert store.get_embedding(_key("poison")) == "[0,0]"
    assert store.get_embedding(_key("absent")) is None
    assert provider.calls == [(["absent", "poison"], "text-embedding-3-small")]


@pytest.mark.anyio
async def test_concurrent_valid_payload_wins_over_observed_poison(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("fact"), "[0,0]")
    provider = _Provider()

    def concurrent_write() -> None:
        with (
            closing(sqlite3.connect(tmp_path / "catalogue.sqlite3")) as connection,
            connection,
        ):
            connection.execute(
                "UPDATE projection_embedding_cache SET payload = ? WHERE cache_key = ?",
                ("[6,8]", _key("fact")),
            )

    provider.before_response = concurrent_write
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    assert await wrapper.create("fact") == [3, 4]
    assert await wrapper.create("fact") == [6, 8]
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_repeated_keys_have_one_recomputation_and_consistent_results(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("fact"), "[0,0]")
    provider = _Provider()
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    assert await wrapper.create_batch(["fact", "fact"]) == [[3, 4], [3, 4]]
    assert provider.calls == [(["fact"], "text-embedding-3-small")]
    assert await wrapper.create_batch(["fact", "fact"]) == [[3, 4], [3, 4]]
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_dropped_repair_returns_valid_vector_but_does_not_claim_recovery(
    tmp_path: Path,
) -> None:
    _store(tmp_path).put_embedding(_key("fact"), "[0,0]")
    stream = StringIO()
    store = ExtractionCacheStore(
        tmp_path, writer_gate=threading.Lock(), logger=configure_logging(stream)
    )
    with (
        closing(sqlite3.connect(tmp_path / "catalogue.sqlite3")) as connection,
        connection,
    ):
        connection.execute(
            "CREATE TRIGGER fail_repair BEFORE UPDATE ON projection_embedding_cache "
            "BEGIN SELECT RAISE(ABORT, 'private failure text'); END"
        )
    provider = _Provider()
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    assert await wrapper.create("fact") == [3, 4]
    assert store.get_embedding(_key("fact")) == "[0,0]"
    assert await wrapper.create("fact") == [3, 4]
    assert len(provider.calls) == 2
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(e["event"], e["cache_kind"], e["cache_rows"]) for e in events] == [
        ("projection_cache_write_dropped", "embedding", 1),
        ("projection_cache_write_dropped", "embedding", 1),
    ]
    assert "private" not in stream.getvalue()


@pytest.mark.anyio
async def test_text_dispatch_uses_inner_batch_override_and_preserves_truncation() -> (
    None
):
    provider = _Provider([[3, 4, 99]])

    class Inner(OpenAIEmbedder):
        async def create(
            self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
        ) -> list[float]:
            pytest.fail("text must use the polymorphic batch path")

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            vectors = await super().create_batch(input_data_list)
            return [[-value for value in vector] for vector in vectors]

    inner = Inner(
        config=OpenAIEmbedderConfig(embedding_dim=2), client=cast(Any, provider)
    )
    wrapper = seam.build_caching_embedder(None, inner)
    assert isinstance(wrapper, OpenAIEmbedder)
    assert await wrapper.create(["text"]) == [-3, -4]
    assert provider.calls == [(["text"], "text-embedding-3-small")]


@pytest.mark.anyio
@pytest.mark.parametrize("input_data", [[1, 2], [[1], [2]], ["one", "two"], []])
async def test_legacy_noncacheable_input_keeps_first_result_semantics(
    tmp_path: Path, input_data: Any
) -> None:
    store = _store(tmp_path)
    provider = _Provider([[3, 4, 99], [5, 6, 99]])
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    assert await wrapper.create(input_data) == [3, 4]
    assert provider.calls[0][0] is input_data
    with (
        closing(sqlite3.connect(tmp_path / "catalogue.sqlite3")) as connection,
        connection,
    ):
        assert connection.execute(
            "SELECT count(*) FROM projection_embedding_cache"
        ).fetchone() == (0,)


@pytest.mark.anyio
async def test_legacy_iterable_is_not_consumed_and_its_result_is_validated() -> None:
    provider = _Provider([[0, 0]])
    tokens = iter([1, 2, 3])
    wrapper = seam.build_caching_embedder(None, _inner(provider))
    with pytest.raises(ValueError, match="^embedding_vector_invalid$"):
        await wrapper.create(tokens)
    assert provider.calls[0][0] is tokens
    assert list(tokens) == [1, 2, 3]


@pytest.mark.anyio
async def test_read_failure_is_unknown_and_cannot_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("fact"), "[6,8]")
    provider = _Provider()

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("private failure")

    monkeypatch.setattr(cache_module, "read_connection", unavailable)
    provider.before_response = lambda: monkeypatch.setattr(
        cache_module, "read_connection", read_connection
    )
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    assert await wrapper.create("fact") == [3, 4]
    assert store.get_embedding(_key("fact")) == "[6,8]"
    assert await wrapper.create("fact") == [6, 8]
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_query_passthrough_validates_without_reading_or_writing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put_embedding(_key("cached"), "[6,8]")
    provider = _Provider()
    wrapper = seam.build_caching_embedder(store, _inner(provider))
    with seam.search_embedding_passthrough():
        assert await wrapper.create("cached") == [3, 4]
        assert await wrapper.create_batch(["new"]) == [[3, 4]]
    assert await wrapper.create("cached") == [6, 8]
    assert store.get_embedding(_key("new")) is None
    assert provider.calls == [
        (["cached"], "text-embedding-3-small"),
        (["new"], "text-embedding-3-small"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("response", [None, 1, {}, [1], [[1, 2, 3]], [[10**400, 1]]])
async def test_invalid_polymorphic_batch_result_is_rejected(response: Any) -> None:
    class Inner(OpenAIEmbedder):
        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            return cast(list[list[float]], response)

    inner = Inner(
        config=OpenAIEmbedderConfig(embedding_dim=2), client=cast(Any, object())
    )
    with pytest.raises(ValueError, match="^embedding_(vector|batch)_invalid$"):
        await seam.build_caching_embedder(None, inner).create("text")
