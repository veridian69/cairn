"""Private fact-body representation, independent of Graphiti extraction.

Only derived vectors cross this internal seam. Bodies, vectors and provider
errors never appear in its closed failures or policy metadata.
"""

import asyncio
import hashlib
import json
import math
import struct
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from graphiti_core.driver.driver import GraphDriver
from graphiti_core.embedder.openai import OpenAIEmbedder

from cairn.projection.adapter import ProjectedFactState

CHUNK_BYTES = 2048
BATCH_BYTES = 16384
BATCH_TEXTS = 32
FACT_CUTOFF = 0.60


def policy_metadata(representation: "FactRepresentation") -> dict[str, Any]:
    search = {
        "version": "cairn.fact-search/v1",
        "cutoff": FACT_CUTOFF,
        "comparison": "strictly greater than",
        "score": "(1 + cosine)/2; exact partition scan",
        "falkor_cosine": "(2 - vec.cosineDistance(vecf32(fact), vecf32(query)))/2",
        "limit": "per partition, before candidate union",
        "ties": "score descending, UUID ascending",
        "union": "episode BM25/RRF, extracted edge episodes, fact vectors; stable deduplication; no global truncation",
        "query": "blank returns empty without coverage assertion; otherwise replace LF with space; uncached create_batch once across partitions",
        "coverage": "preflight then single-statement coverage and candidates; fail closed across requested partitions",
    }
    return {
        "representation": representation.metadata(),
        "search": {**search, "sha256": _hash(search)},
    }


class FactVectorError(Exception):
    def __init__(self, code: str = "fact_vector_invalid") -> None:
        self.code = code
        super().__init__(code)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def utf8_chunks(text: str) -> Iterator[str]:
    start, size = 0, 0
    for position, character in enumerate(text):
        try:
            length = len(character.encode("utf-8", "strict"))
        except UnicodeError:
            raise FactVectorError() from None
        if size + length > CHUNK_BYTES:
            yield text[start:position]
            start, size = position, 0
        size += length
    if size:
        yield text[start:]
    else:
        raise FactVectorError()


def _unit(vector: object, dimension: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimension:
        raise FactVectorError()
    if any(
        type(value) not in (int, float) or not math.isfinite(value) for value in vector
    ):
        raise FactVectorError()
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise FactVectorError()
    return [value / norm for value in vector]


def quantize_unit(vector: list[float], dimension: int) -> list[float]:
    stored = [
        struct.unpack("!f", struct.pack("!f", value))[0]
        for value in _unit(vector, dimension)
    ]
    _unit(stored, dimension)
    return stored


class FactRepresentation:
    def __init__(self, embedder: OpenAIEmbedder) -> None:
        self.embedder = embedder
        self.model = str(embedder.config.embedding_model)
        self.dimension = embedder.config.embedding_dim
        if (
            self.model != "text-embedding-3-small"
            or type(self.dimension) is not int
            or not 1 <= self.dimension <= 1536
        ):
            raise FactVectorError("fact_vector_model_unsupported")

    def metadata(self) -> dict[str, Any]:
        recipe = {
            "version": "cairn.fact-vector/v1",
            "model": self.model,
            "dimension": self.dimension,
            "dimension_truncation": "leading dimensions",
            "encoding": "UTF-8 strict; no normalisation",
            "tokenizer": "none; utf8-byte-upper-bound/v1",
            "chunk_bytes": CHUNK_BYTES,
            "batch_bytes": BATCH_BYTES,
            "batch_texts": BATCH_TEXTS,
            "split": "greedy longest Unicode-scalar prefix; no overlap",
            "pool": "L2 each chunk; UTF-8-byte-weighted float64 mean; L2 pooled; float32 quantisation",
        }
        return {**recipe, "sha256": _hash(recipe)}

    def fingerprint(self, body: str) -> str:
        return _hash(
            {
                "representation": self.metadata()["sha256"],
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
        )

    async def embed_text(self, text: str) -> list[float]:
        return [vector async for _, vector in self.embed_many([text])][0]

    async def embed_many(
        self, texts: Sequence[str]
    ) -> AsyncIterator[tuple[int, list[float]]]:
        def entries() -> Iterator[tuple[int, str, bool]]:
            for index, text in enumerate(texts):
                chunks = iter(utf8_chunks(text))
                current = next(chunks)
                for following in chunks:
                    yield index, current, False
                    current = following
                yield index, current, True

        accumulator = [0.0] * self.dimension
        weight = 0
        async for index, text, final, vector in self._embed_entries(entries()):
            size = len(text.encode("utf-8"))
            for axis, value in enumerate(vector):
                accumulator[axis] += size * value
            weight += size
            if final:
                yield (
                    index,
                    quantize_unit(
                        [value / weight for value in accumulator], self.dimension
                    ),
                )
                accumulator, weight = [0.0] * self.dimension, 0

    async def embed_chunks(self, texts: Sequence[str]) -> AsyncIterator[list[float]]:
        """Validated units for existing base inputs and private bridge inputs."""
        entries = ((index, text, True) for index, text in enumerate(texts))
        async for _, _, _, vector in self._embed_entries(entries):
            yield vector

    async def _embed_entries(
        self, entries: Iterator[tuple[int, str, bool]]
    ) -> AsyncIterator[tuple[int, str, bool, list[float]]]:
        def batches() -> Iterator[list[tuple[int, str, bool]]]:
            batch: list[tuple[int, str, bool]] = []
            size = 0
            for entry in entries:
                length = len(entry[1].encode("utf-8"))
                if not 1 <= length <= CHUNK_BYTES:
                    raise FactVectorError()
                if batch and (len(batch) == BATCH_TEXTS or size + length > BATCH_BYTES):
                    yield batch
                    batch, size = [], 0
                batch.append(entry)
                size += length
            if batch:
                yield batch

        for batch in batches():
            vectors = await self.embedder.create_batch([text for _, text, _ in batch])
            if not isinstance(vectors, list) or len(vectors) != len(batch):
                raise FactVectorError()
            # Validate every result before yielding any completed fact in this batch.
            units = [_unit(vector, self.dimension) for vector in vectors]
            for (index, text, final), vector in zip(batch, units, strict=True):
                yield index, text, final, vector


# All retained episodes participate in coverage, before score/limit. The final
# aggregate has no grouping key, so even an empty graph returns one envelope.
# Store float32-quantised values as a numeric array: its actual length and shape
# remain inspectable before conversion to Falkor's cosine operand.
# Fingerprint syntax is required coverage metadata, not an authentication check
# against retained body content. Coercion makes string operands safe; equality
# with the original property still requires a string, not a coerced scalar.
_COVERAGE_SEARCH = """
MATCH (e:Episodic) WHERE e.group_id = $group_id
OPTIONAL MATCH (v:CairnFactVector {uuid:e.uuid})
WITH e, collect(v) AS versions
WITH e, versions, head(versions) AS v
WITH e, versions, v, toStringOrNull(v.fingerprint) AS fingerprint
WITH e, v, coalesce(
    size(versions) = 1 AND e.source_description = 'cairn.fact.projected'
    AND v.group_id = $group_id AND v.representation = $representation
    AND v.fingerprint = fingerprint AND size(fingerprint) = 64
    AND all(position IN range(0,63) WHERE substring(fingerprint,position,1)
        IN ['0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f'])
    AND v.dim = $dimension AND size(v.embedding) = $dimension
    AND all(x IN v.embedding WHERE x = x AND abs(x) <= 3.4028234663852886e38)
    AND reduce(norm = 0.0, x IN v.embedding | norm + x*x) > 0.0,
    false) AS valid
WITH e.uuid AS uuid, valid,
    CASE WHEN valid AND $scoring THEN
        (2 - vec.cosineDistance(vecf32(CASE WHEN valid THEN v.embedding ELSE $query_vector END), vecf32($query_vector)))/2
    ELSE -1.0 END AS score
ORDER BY score DESC, uuid ASC
WITH collect({uuid:uuid, valid:valid, score:score}) AS rows
RETURN all(row IN rows WHERE row.valid) AS coverage_valid,
       [row IN rows WHERE row.valid AND row.score > $cutoff | row.uuid][..$limit] AS candidates
"""
_COVERAGE_ONLY = (
    _COVERAGE_SEARCH.partition("WITH e.uuid AS uuid")[0]
    + """
WITH collect(valid) AS validity
RETURN all(value IN validity WHERE value) AS coverage_valid, [] AS candidates
"""
)


class FactVectorIndex:
    def __init__(self, representation: FactRepresentation) -> None:
        self.representation = representation

    async def ensure(
        self, driver: GraphDriver, group_id: str, states: Sequence[ProjectedFactState]
    ) -> None:
        if not states:
            return
        await _ready(driver)
        await driver.execute_query("CREATE INDEX FOR (v:CairnFactVector) ON (v.uuid)")
        rows, _, _ = await driver.execute_query(
            "MATCH (v:CairnFactVector) WHERE v.uuid IN $ids AND v.group_id = $group_id "
            "RETURN v.uuid AS uuid, v.fingerprint AS fingerprint, v.representation AS representation, v.embedding AS embedding, v.dim AS dimension",
            ids=[str(state.fact_id) for state in states],
            group_id=group_id,
        )
        stored: dict[str, Any] = {}
        for row in rows:
            if row["uuid"] in stored:
                raise FactVectorError("fact_vector_rebuild_required")
            stored[row["uuid"]] = row
        pending: list[ProjectedFactState] = []
        recipe = self.representation.metadata()["sha256"]
        for state in states:
            existing = stored.get(str(state.fact_id))
            if (
                existing is not None
                and existing["fingerprint"]
                == self.representation.fingerprint(state.body)
                and existing["representation"] == recipe
                and existing.get("dimension") == self.representation.dimension
            ):
                try:
                    _unit(existing["embedding"], self.representation.dimension)
                    continue
                except FactVectorError:
                    pass
            pending.append(state)
        async for position, vector in self.representation.embed_many(
            [state.body for state in pending]
        ):
            state = pending[position]
            await driver.execute_query(
                "MERGE (v:CairnFactVector {uuid:$uuid}) "
                "SET v.group_id=$group_id, v.representation=$representation, v.fingerprint=$fingerprint, "
                "v.dim=$dimension, v.embedding=$embedding RETURN v.uuid AS uuid",
                uuid=str(state.fact_id),
                group_id=group_id,
                representation=recipe,
                fingerprint=self.representation.fingerprint(state.body),
                dimension=self.representation.dimension,
                embedding=vector,
            )

    async def _read(
        self, driver: GraphDriver, group_id: str, vector: list[float] | None, limit: int
    ) -> list[str]:
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _COVERAGE_SEARCH if vector is not None else _COVERAGE_ONLY,
            group_id=group_id,
            representation=self.representation.metadata()["sha256"],
            dimension=self.representation.dimension,
            query_vector=vector if vector is not None else [],
            scoring=vector is not None,
            cutoff=FACT_CUTOFF,
            limit=limit,
        )
        if len(rows) != 1 or rows[0].get("coverage_valid") is not True:
            raise FactVectorError("fact_vector_rebuild_required")
        candidates = rows[0].get("candidates")
        if (
            not isinstance(candidates, list)
            or len(candidates) > limit
            or any(not isinstance(value, str) for value in candidates)
        ):
            raise FactVectorError("fact_vector_rebuild_required")
        return candidates

    async def preflight(self, driver: GraphDriver, group_id: str) -> None:
        await self._read(driver, group_id, None, 0)

    async def search(
        self, driver: GraphDriver, group_id: str, vector: list[float], limit: int
    ) -> list[str]:
        _unit(vector, self.representation.dimension)
        return await self._read(driver, group_id, vector, limit)


async def _ready(driver: GraphDriver) -> None:
    # Falkor clones start schema creation in a background task. Own its result
    # before using the partition, rather than leaving work behind at close.
    ready = getattr(driver, "wait_ready", None)
    if ready is not None:
        await ready()
        return
    task = getattr(driver, "_init_task", None)
    if task is not None:
        await asyncio.shield(task)
