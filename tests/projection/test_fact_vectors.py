"""Fact representation tests: content preservation and bounded provider work."""

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import pytest


class Embedder:
    def __init__(self, response: list[list[float]] | None = None) -> None:
        self.config = SimpleNamespace(
            embedding_model="text-embedding-3-small", embedding_dim=2
        )
        self.calls: list[list[str]] = []
        self.response = response

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return (
            self.response if self.response is not None else [[3.0, 4.0] for _ in texts]
        )


def representation(embedder: Any) -> Any:
    from cairn.projection.fact_vectors import FactRepresentation

    return FactRepresentation(embedder)


def test_chunks_preserve_exact_unicode_and_special_token_spellings() -> None:
    from cairn.projection.fact_vectors import utf8_chunks

    body = "a" * 2047 + "🙂e\u0301漢字<|endoftext|>\r\n" * 200
    chunks = list(utf8_chunks(body))
    assert chunks[0] == "a" * 2047
    assert "".join(chunks) == body
    assert b"".join(chunk.encode("utf-8") for chunk in chunks) == body.encode("utf-8")
    assert all(0 < len(chunk.encode("utf-8")) <= 2048 for chunk in chunks)
    assert all(chunk.encode("utf-8").decode("utf-8") == chunk for chunk in chunks)


def test_pool_uses_byte_weights_and_reuses_one_query_batch() -> None:
    embedder = Embedder([[1.0, 0.0], [0.0, 1.0]])
    rep = representation(embedder)
    vector = asyncio.run(rep.embed_text("a" * 2048 + "b" * 1024))
    assert vector == pytest.approx([2 / math.sqrt(5), 1 / math.sqrt(5)])
    assert len(embedder.calls) == 1


def test_maximum_body_is_streamed_in_bounded_batches() -> None:
    embedder = Embedder()
    vector = asyncio.run(representation(embedder).embed_text("x" * 65536))
    assert vector == pytest.approx([0.6, 0.8])
    assert len(embedder.calls) == 4
    assert all(
        len(call) <= 32 and sum(len(text.encode()) for text in call) <= 16384
        for call in embedder.calls
    )


@pytest.mark.parametrize(
    "response",
    [
        [],
        [[1.0]],
        [[1.0, 0.0], [1.0, 0.0]],
        [[float("nan"), 1.0]],
        [[0.0, 0.0]],
        [[True, 1.0]],
    ],
)
def test_malformed_provider_result_cannot_become_a_fact_vector(response: Any) -> None:
    from cairn.projection.fact_vectors import FactVectorError

    with pytest.raises(FactVectorError):
        asyncio.run(representation(Embedder(response)).embed_text("ordinary fact"))


def test_cutoff_metadata_changes_search_identity_not_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.projection import fact_vectors

    rep = representation(Embedder())
    before = fact_vectors.policy_metadata(rep)
    monkeypatch.setattr(fact_vectors, "FACT_CUTOFF", 0.7)
    after = fact_vectors.policy_metadata(rep)
    assert before["search"]["sha256"] != after["search"]["sha256"]
    assert before["representation"] == after["representation"]
    assert after["search"]["cutoff"] == 0.7


@pytest.mark.parametrize("position", [0, 32768, 65535], ids=["start", "middle", "end"])
def test_maximum_body_all_positions_contribute_without_truncation(
    position: int,
) -> None:
    class Controlled(Embedder):
        async def create_batch(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return [[1.0, 0.0] if "!" in text else [0.0, 1.0] for text in texts]

    body = "x" * position + "!" + "x" * (65535 - position)
    embedder = Controlled()
    vector = asyncio.run(representation(embedder).embed_text(body))
    assert "".join(text for batch in embedder.calls for text in batch) == body
    assert vector == pytest.approx([1 / math.sqrt(962), 31 / math.sqrt(962)])


def test_invalid_second_result_publishes_no_first_fact() -> None:
    from cairn.projection.fact_vectors import FactVectorError

    published = []

    async def run() -> None:
        async for result in representation(
            Embedder([[1.0, 0.0], [0.0, 0.0]])
        ).embed_many(["first", "second"]):
            published.append(result)

    with pytest.raises(FactVectorError):
        asyncio.run(run())
    assert published == []


def test_opposing_chunks_cannot_publish_a_zero_pooled_vector() -> None:
    from cairn.projection.fact_vectors import FactVectorError

    with pytest.raises(FactVectorError):
        asyncio.run(
            representation(Embedder([[1.0, 0.0], [-1.0, 0.0]])).embed_text("x" * 4096)
        )


def test_body_and_dimension_bind_representation_fingerprint() -> None:
    embedder = Embedder()
    old = representation(embedder)
    assert old.fingerprint("e\u0301") != old.fingerprint("é")
    embedder.config.embedding_dim = 3
    new = representation(embedder)
    assert old.metadata()["sha256"] != new.metadata()["sha256"]
    assert old.fingerprint("same") != new.fingerprint("same")


@pytest.mark.parametrize("text", ["", "\ud800"])
def test_invalid_text_fails_before_embedding(text: str) -> None:
    from cairn.projection.fact_vectors import FactVectorError

    embedder = Embedder()
    with pytest.raises(FactVectorError):
        asyncio.run(representation(embedder).embed_text(text))
    assert embedder.calls == []
