"""Provider-free v5 units, exact deduplication and legacy pooling."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from graphiti_core.embedder.openai import OpenAIEmbedder

from cairn.projection.fact_chunks import chunk_spans, embed_local
from cairn.projection.fact_vectors import FactRepresentation, FactVectorError


class ControlledEmbedder:
    def __init__(self, dimension: int = 1024) -> None:
        self.config = SimpleNamespace(
            embedding_model="text-embedding-3-small", embedding_dim=dimension
        )
        self.inputs: list[list[str]] = []
        self.poison_at = 0

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.inputs.append(texts)
        vectors = [
            (([1.0, 0.0] if t.startswith("a") else [0.0, 1.0]) + [0.0] * 1534)[
                : self.config.embedding_dim
            ]
            for t in texts
        ]
        if self.poison_at == len(self.inputs):
            vectors[-1] = [float("nan")] * self.config.embedding_dim
        return vectors


def representation(e: ControlledEmbedder) -> FactRepresentation:
    return FactRepresentation(cast(OpenAIEmbedder, e))


def positions(body: str) -> list[tuple[int, int, int]]:
    return [(s.start, s.end, s.slot) for s in chunk_spans(body)]


def test_short_unicode_retains_whitespace_and_one_slot() -> None:
    body = "e\u0301🙂漢\r\n"
    assert positions(body) == [(0, len(body.encode()), 0)]


@pytest.mark.parametrize("mark", list(".!?。！？,;:"))
def test_clause_boundary_owns_mark_and_requires_lookahead(mark: str) -> None:
    first = "a" * 31 + mark
    end = len(first.encode())
    assert positions(first + " tail") == [(0, end, 0), (end, end + 5, 1)]
    assert positions(first + "tail") == [(0, end + 4, 0)]


@pytest.mark.parametrize(
    "space",
    [
        "\t",
        "\x1c",
        "\x85",
        "\xa0",
        "\u1680",
        "\u2000",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
    ],
    ids=lambda s: f"U+{ord(s):04X}",
)
def test_explicit_unicode_whitespace_lookahead(space: str) -> None:
    body = "a" * 31 + "," + space + "tail"
    assert positions(body) == [(0, 32, 0), (32, len(body.encode()), 1)]


@pytest.mark.parametrize("character", ["\u200b", "\ufeff", "\u180e"])
def test_unlisted_character_is_not_whitespace(character: str) -> None:
    body = "a" * 31 + "," + character + "tail"
    assert positions(body) == [(0, len(body.encode()), 0)]


def test_floor_packs_tiny_fragments_without_filling_ceiling() -> None:
    assert positions("a" * 15 + ". " + "b" * 15 + "; tail") == [(0, 33, 0), (33, 38, 1)]


@pytest.mark.parametrize("delimiter", ["\r\n", "\r", "\n"])
def test_line_boundary_owns_delimiter(delimiter: str) -> None:
    end = 31 + len(delimiter)
    assert positions("a" * 31 + delimiter + "tail") == [(0, end, 0), (end, end + 4, 1)]


def test_unicode_separator_alone_is_not_line_boundary() -> None:
    body = "a" * 32 + "\u2028tail"
    assert positions(body) == [(0, len(body.encode()), 0)]


def test_scalar_fallback_can_split_crlf_but_not_scalar() -> None:
    assert positions("a" * 511 + "\r\ntail") == [(0, 512, 0), (512, 517, 1)]
    assert positions("a" * 510 + "🙂tail") == [(0, 510, 0), (510, 518, 1)]


def test_slots_deduplicate_text_not_occurrences() -> None:
    a, b = "a" * 31 + "\n", "b" * 31 + "\n"
    assert positions(a + b + a) == [(0, 32, 0), (32, 64, 1), (64, 96, 0)]


@pytest.mark.parametrize(
    ("body", "count", "distinct"),
    [
        ("x" * 65536, 128, 1),
        ("🙂" * 16384, 128, 1),
        ("a\n" * 32768, 2048, 1),
        ("".join(f"{i:030d}.\n" for i in range(2048)), 2048, 2048),
    ],
    ids=["ascii", "four-byte", "repeated-lines", "distinct-lines"],
)
def test_maximal_body_full_coverage(body: str, count: int, distinct: int) -> None:
    raw = body.encode()
    spans = chunk_spans(body)
    assert len(spans) == count <= (len(raw) + 31) // 32 <= 2048
    assert len({s.slot for s in spans}) == distinct
    assert spans[0].start == 0 and spans[-1].end == 65536
    assert all(a.end == b.start for a, b in zip(spans[:-1], spans[1:], strict=True))
    assert all(32 <= s.end - s.start <= 512 for s in spans[:-1])
    assert 1 <= spans[-1].end - spans[-1].start <= 512
    assert b"".join(raw[s.start : s.end] for s in spans) == raw
    assert all(
        raw[s.start : s.end].decode().encode() == raw[s.start : s.end] for s in spans
    )


def test_whitespace_tail_is_never_dropped() -> None:
    assert positions("a" * 31 + ".\n") == [(0, 32, 0), (32, 33, 1)]


def test_whitespace_only_units_are_embedded_and_deduplicated_exactly() -> None:
    embedder = ControlledEmbedder()
    body = " " * 1025
    result = asyncio.run(embed_local(body, representation(embedder)))
    assert [(s.start, s.end, s.slot) for s in result.spans] == [
        (0, 512, 0),
        (512, 1024, 0),
        (1024, 1025, 1),
    ]
    assert len(result.vectors) == 2
    assert [t for batch in embedder.inputs for t in batch] == [body, " " * 512, " "]


@pytest.mark.parametrize(
    "body",
    ["", "x" * 65537, "bad\ud800", "🙂" * 16385],
    ids=["empty", "chars", "surrogate", "bytes"],
)
def test_invalid_body_refused(body: str) -> None:
    with pytest.raises(FactVectorError):
        chunk_spans(body)


def test_distinct_vectors_keep_exact_legacy_pool_without_base_reembedding() -> None:
    body = "a" * 2048 + "b" * (31 * 2048)
    embedder, legacy = ControlledEmbedder(), ControlledEmbedder()
    result = asyncio.run(embed_local(body, representation(embedder)))
    pooled = asyncio.run(representation(legacy).embed_text(body))
    assert result.pooled == pooled
    assert (1 + pooled[0]) / 2 < 0.60
    assert max((1 + v[0]) / 2 for v in result.vectors) == 1.0
    assert len(result.spans) == 128 and len(result.vectors) == 2
    assert [s.slot for s in result.spans] == [0] * 4 + [1] * 124
    sent = [t for batch in embedder.inputs for t in batch]
    bases = [t for batch in legacy.inputs for t in batch]
    assert sent[: len(bases)] == bases
    assert sent[len(bases) :] == ["a" * 512, "b" * 512]
    assert all(
        len(b) <= 32 and sum(len(t.encode()) for t in b) <= 16384
        for b in embedder.inputs
    )


def test_equal_vectors_do_not_merge_different_texts() -> None:
    result = asyncio.run(
        embed_local(
            "b" * 31 + "\n" + "c" * 31 + "\n", representation(ControlledEmbedder())
        )
    )
    assert len(result.vectors) == 2 and result.vectors[0] == result.vectors[1]
    assert [s.slot for s in result.spans] == [0, 1]


def test_late_poison_prevents_any_publishable_result() -> None:
    embedder = ControlledEmbedder()
    embedder.poison_at = 3
    with pytest.raises(FactVectorError):
        asyncio.run(
            embed_local(
                "".join(f"{i:030d}.\n" for i in range(70)), representation(embedder)
            )
        )


@pytest.mark.parametrize("dimension", [1, 2, 1536])
def test_non1024_refused_without_provider_work(dimension: int) -> None:
    embedder = ControlledEmbedder(dimension)
    rep = representation(embedder)
    assert rep.dimension == dimension
    with pytest.raises(FactVectorError, match="fact_local_unavailable"):
        asyncio.run(embed_local("ordinary text", rep))
    assert embedder.inputs == []


def test_batch_validation_precedes_first_unit() -> None:
    embedder = ControlledEmbedder()
    embedder.poison_at = 1

    async def run() -> None:
        values = representation(embedder).embed_chunks(["a" * 2048, "b"])
        with pytest.raises(FactVectorError):
            await anext(values)

    asyncio.run(run())
