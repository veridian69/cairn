"""Private full-coverage clause units and embedding reuse; no provider setup."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import cast

from cairn.projection.fact_vectors import (
    FactRepresentation,
    FactVectorError,
    quantize_unit,
)
from cairn.projection.semantic_evidence import local_representation_recipe

_RECIPE = local_representation_recipe()
_PUNCTUATION = frozenset(".!?。！？,;:")
# Fixed recipe whitespace, independent of Python's evolving Unicode database.
_WHITESPACE = frozenset(
    (
        *range(0x09, 0x0E),
        *range(0x1C, 0x21),
        0x85,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )
)


@dataclass(frozen=True, slots=True)
class UnitSpan:
    start: int
    end: int
    slot: int


def _construct(body: str) -> tuple[tuple[UnitSpan, ...], tuple[str, ...]]:
    maximum = cast(int, _RECIPE["max_body_bytes"])
    if type(body) is not str or len(body) > maximum:
        raise FactVectorError()
    try:
        raw = body.encode("utf-8", "strict")
    except UnicodeError:
        raise FactVectorError() from None
    if not 1 <= len(raw) <= maximum:
        raise FactVectorError()

    scalars = [0]
    for character in body:
        scalars.append(scalars[-1] + len(character.encode("utf-8")))
    ends = {len(raw)}
    for index, character in enumerate(body):
        following = body[index + 1] if index + 1 < len(body) else None
        if character == "\r":
            ends.add(scalars[index + (2 if following == "\n" else 1)])
        elif character == "\n":
            if index == 0 or body[index - 1] != "\r":
                ends.add(scalars[index + 1])
        elif character in _PUNCTUATION and (
            following is None or ord(following) in _WHITESPACE
        ):
            ends.add(scalars[index + 1])
    boundaries = sorted(ends)
    floor = cast(int, _RECIPE["unit_min_bytes"])
    ceiling = cast(int, _RECIPE["unit_max_bytes"])
    spans = []
    slots: dict[str, int] = {}
    start = 0
    while start < len(raw):
        choice = bisect_left(boundaries, start + floor)
        if len(raw) - start < floor:
            end = len(raw)
        elif choice < len(boundaries) and boundaries[choice] <= start + ceiling:
            end = boundaries[choice]
        else:
            end = scalars[bisect_right(scalars, min(start + ceiling, len(raw))) - 1]
        text = raw[start:end].decode("utf-8")
        slot = slots.setdefault(text, len(slots))
        spans.append(UnitSpan(start, end, slot))
        start = end
    if len(spans) > cast(int, _RECIPE["max_unit_refs"]) or len(slots) > cast(
        int, _RECIPE["max_vectors"]
    ):
        raise FactVectorError()
    return tuple(spans), tuple(slots)


def chunk_spans(body: str) -> tuple[UnitSpan, ...]:
    """All exact byte occurrences, with first-occurrence distinct-text slots."""
    return _construct(body)[0]


@dataclass(frozen=True, slots=True)
class EmbeddedLocalFact:
    spans: tuple[UnitSpan, ...]
    vectors: tuple[list[float], ...]
    pooled: list[float]


async def embed_local(
    body: str, representation: FactRepresentation
) -> EmbeddedLocalFact:
    if representation.dimension != _RECIPE["dimension"]:
        raise FactVectorError("fact_local_unavailable")
    spans, texts = _construct(body)
    # The unchanged v1 implementation streams bases once, in its original order.
    # No result is publishable until every local batch has also been admitted.
    pooled = await representation.embed_text(body)
    vectors = tuple(
        [
            quantize_unit(unit, representation.dimension)
            async for unit in representation.embed_chunks(texts)
        ]
    )
    return EmbeddedLocalFact(spans, vectors, pooled)
