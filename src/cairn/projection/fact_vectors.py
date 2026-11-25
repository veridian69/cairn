"""Private fact-body representation, independent of Graphiti extraction.

Only derived vectors cross this internal seam. Bodies, vectors and provider
errors never appear in its closed failures or policy metadata.
"""

import asyncio
import hashlib
import json
import math
import struct
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

from graphiti_core.driver.driver import GraphDriver
from graphiti_core.embedder.openai import OpenAIEmbedder

from cairn.projection.adapter import CoverageAudit, ProjectedFactState
from cairn.projection.models import EMBEDDING_MODEL

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
    # #281: set by the search that was refused while a projection overlapped
    # it. It selects the log event and nothing else.
    in_flight = False

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
            self.model != EMBEDDING_MODEL
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
# #204: the same per-episode verdict, one row each, so repair can name the
# facts the gate refuses. Derived from the gate text so the two cannot drift.
_COVERAGE_ROWS = (
    _COVERAGE_SEARCH.partition("WITH e.uuid AS uuid")[0]
    + "RETURN e.uuid AS uuid, valid\n"
)

# #318: the gate text restricted to named episodes, for the facts one
# projection wrote. Same predicate, so a fact it passes passes the gate.
_COVERAGE_NAMED = _COVERAGE_ROWS.replace(
    "MATCH (e:Episodic) WHERE e.group_id = $group_id",
    "MATCH (e:Episodic) WHERE e.uuid IN $uuids AND e.group_id = $group_id",
)
assert _COVERAGE_NAMED != _COVERAGE_ROWS
# #318: scoring for a partition whose coverage is already established (see
# ``PartitionVerdicts``). It proves nothing; for a covered partition whose
# counts agree it selects what ``_COVERAGE_SEARCH`` selects, in that order.
_TRUSTED_SEARCH = """
MATCH (v:CairnFactVector) WHERE v.group_id = $group_id
WITH v.uuid AS uuid,
     (2 - vec.cosineDistance(vecf32(v.embedding), vecf32($query_vector)))/2 AS score
WHERE score > $cutoff
WITH uuid, score ORDER BY score DESC, uuid ASC
RETURN collect(uuid)[..$limit] AS candidates
"""
# One row even for an empty graph: each aggregate has no grouping key.
PARTITION_COUNTS = """
OPTIONAL MATCH (e:Episodic)
WITH count(e) AS all_episodes, count(DISTINCT e.uuid) AS identities,
     count(CASE WHEN e.group_id = $group_id THEN 1 ELSE null END) AS episodes
OPTIONAL MATCH (v:CairnFactVector)
WITH all_episodes, identities, episodes, count(v) AS all_vectors,
     count(CASE WHEN v.group_id = $group_id THEN 1 ELSE null END) AS vectors
OPTIONAL MATCH (h:CairnFactChunkSet)
RETURN all_episodes, identities, episodes, all_vectors, vectors, count(h) AS all_headers,
       count(CASE WHEN h.group_id = $group_id THEN 1 ELSE null END) AS headers
"""


async def partition_counts(driver: GraphDriver, group_id: str) -> tuple[int, int, int]:
    """Episodes, fact vectors and chunk-set headers of this partition in the
    driver's graph. A record of any other partition there is a defect: the
    gates walk episodes and the trusted scans walk records, and only a graph
    holding nothing else lets equal counts stand for the same facts."""
    await _ready(driver)
    rows, _, _ = await driver.execute_query(PARTITION_COUNTS, group_id=group_id)
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise FactVectorError("fact_vector_rebuild_required")
    names = ("episodes", "vectors", "headers")
    counts = tuple(rows[0].get(name) for name in names)
    totals = tuple(rows[0].get("all_" + name) for name in names)
    if any(type(count) is not int or count < 0 for count in counts + totals):
        raise FactVectorError("fact_vector_rebuild_required")
    # Two episodes sharing an identity would each pass the gates on the one
    # record they share, and leave room for a record no episode owns.
    if counts != totals or rows[0].get("identities") != counts[0]:
        raise FactVectorError("fact_vector_rebuild_required")
    return cast(tuple[int, int, int], counts)


def counts_agree(counts: tuple[int, int, int], units: bool) -> bool:
    """One fact vector per episode and, where units are graded, one header.

    The gates walk episodes, so they cannot see a record no episode owns.
    Equal counts beside a passed gate make the trusted scans, which walk the
    records, select from exactly the facts the gate covered."""
    episodes, vectors, headers = counts
    return episodes == vectors and (not units or episodes == headers)


@dataclass(slots=True)
class _Verdict:
    verified: bool = False
    units: bool = False
    counts: tuple[int, int, int] | None = None
    epoch: int = 0
    writers: int = 0
    base: bool = False
    base_units: bool = False
    failed: bool = False


class PartitionVerdicts:
    """#318: which partitions have passed their coverage gates since the
    last write, so a read need not walk the partition again to prove it.

    A verdict is established by a gated read that passed, and only if no
    write was running at any point of it. A write withdraws it for as long as it runs; when
    the last overlapping write ends it is restored only if the partition
    held a verdict before, every one of those writes succeeded and the
    facts they wrote passed the same gates by name. Anything else leaves the
    partition unverified, and the next read proves it in full as before.

    What this gives up is detection, at the next read, of a record damaged
    behind this process's back between writes. The stored counts catch a
    lost or added record on the next read; the start-up audit (I-99) and
    every gated read still see the rest. Thread-safe: reads and deliveries
    arrive on different threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._verdicts: dict[str, _Verdict] = {}
        # One clock for every epoch, so no two events share a value. A
        # partition with no entry stands at ``_floor``, the last drop of all.
        self._clock = 0
        self._floor = 0

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def _of(self, group: str) -> _Verdict:
        """The entry for a partition that was written to or holds a verdict.
        Reads alone never create one: a caller may name any partition, and
        an entry per name asked for would grow without bound."""
        verdict = self._verdicts.get(group)
        if verdict is None:
            verdict = self._verdicts[group] = _Verdict(epoch=self._floor)
        return verdict

    def _epoch(self, group: str) -> int:
        verdict = self._verdicts.get(group)
        return self._floor if verdict is None else verdict.epoch

    def epoch(self, group: str) -> int:
        with self._lock:
            return self._epoch(group)

    def held(self, group: str, units: bool) -> tuple[int, tuple[int, int, int]] | None:
        """The epoch and counts of a verdict that covers this read."""
        with self._lock:
            verdict = self._verdicts.get(group)
            if (
                verdict is None
                or not verdict.verified
                or verdict.counts is None
                or (units and not verdict.units)
            ):
                return None
            return verdict.epoch, verdict.counts

    def unchanged(self, group: str, epoch: int) -> bool:
        with self._lock:
            verdict = self._verdicts.get(group)
            return verdict is not None and verdict.verified and verdict.epoch == epoch

    def establish(
        self, group: str, epoch: int, counts: tuple[int, int, int], units: bool
    ) -> None:
        """Record a passed gated read, unless a write or a drop intervened.

        An empty partition gets no verdict: its walk costs nothing, and a
        verdict for every empty partition a caller names would be unbounded."""
        with self._lock:
            if (
                self._epoch(group) != epoch
                or counts[0] == 0
                or not counts_agree(counts, units)
            ):
                return
            verdict = self._of(group)
            if verdict.writers:
                return
            if verdict.verified and verdict.units and not units:
                return  # Keep the wider proof already held.
            verdict.verified, verdict.units, verdict.counts = True, units, counts

    def drop(self, group: str | None = None) -> None:
        """Withdraw one verdict, or all of them; overlapping writes cannot
        restore what was dropped."""
        with self._lock:
            if group is not None:
                targets = [self._verdicts[group]] if group in self._verdicts else []
            else:
                # Entries with a write in progress keep their bookkeeping;
                # the rest are forgotten and stand at the new floor.
                self._floor = self._tick()
                self._verdicts = {
                    name: verdict
                    for name, verdict in self._verdicts.items()
                    if verdict.writers
                }
                targets = list(self._verdicts.values())
            for verdict in targets:
                verdict.verified = False
                verdict.failed = True
                verdict.epoch = self._tick()

    def begin_write(self, group: str) -> bool:
        """Withdraw the verdict for the write; whether it is restorable."""
        with self._lock:
            verdict = self._of(group)
            if verdict.writers == 0:
                verdict.base, verdict.base_units = verdict.verified, verdict.units
                verdict.failed = False
            verdict.writers += 1
            verdict.verified = False
            verdict.epoch = self._tick()
            return verdict.base and not verdict.failed

    def abandon_write(self, group: str) -> None:
        """A write whose work may still be running after its call gave up
        (timeout, shutdown). Its slot is never released: the partition holds
        no verdict again in this process and every read of it is gated."""
        with self._lock:
            verdict = self._of(group)
            verdict.verified = False
            verdict.failed = True
            verdict.epoch = self._tick()

    def end_write(
        self, group: str, counts: tuple[int, int, int] | None, units: bool
    ) -> None:
        """``counts`` is ``None`` unless the write succeeded and what it
        wrote passed the gates by name; ``units`` says the unit gate was
        among them."""
        with self._lock:
            verdict = self._of(group)
            verdict.writers -= 1
            # A gated read that took its epoch while this write ran saw a
            # partition in motion: it must establish nothing either.
            verdict.epoch = self._tick()
            proven_units = verdict.base_units and units
            if counts is None or not counts_agree(counts, proven_units):
                verdict.failed = True
            if not units:
                verdict.base_units = False
            if verdict.writers == 0 and verdict.base and not verdict.failed:
                assert counts is not None
                verdict.verified = True
                verdict.units, verdict.counts = verdict.base_units, counts


def named_rows_valid(rows: object, identities: Sequence[str]) -> bool:
    """Whether per-episode verdict rows name exactly ``identities``, all valid."""
    if type(rows) is not list or len(rows) != len(set(identities)):
        return False
    seen: set[str] = set()
    for row in rows:
        if (
            type(row) is not dict
            or set(row) != {"uuid", "valid"}
            or type(row["uuid"]) is not str
            or row["valid"] is not True
        ):
            return False
        seen.add(row["uuid"])
    return seen == set(identities)


class FactVectorIndex:
    def __init__(self, representation: FactRepresentation) -> None:
        self.representation = representation

    async def verify(
        self, driver: GraphDriver, group_id: str, identities: Sequence[str]
    ) -> bool:
        """#318: the coverage gate's verdict for these episodes alone."""
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _COVERAGE_NAMED,
            uuids=list(identities),
            group_id=group_id,
            representation=self.representation.metadata()["sha256"],
            dimension=self.representation.dimension,
        )
        return named_rows_valid(rows, identities)

    async def trusted_search(
        self, driver: GraphDriver, group_id: str, vector: list[float], limit: int
    ) -> list[str]:
        """#318: candidates without the coverage walk. Only for a partition
        whose verdict the caller holds."""
        _unit(vector, self.representation.dimension)
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _TRUSTED_SEARCH,
            group_id=group_id,
            query_vector=vector,
            cutoff=FACT_CUTOFF,
            limit=limit,
        )
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            raise FactVectorError("fact_vector_rebuild_required")
        return _candidates(rows[0].get("candidates"), limit)

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
        return _candidates(rows[0].get("candidates"), limit)

    async def preflight(self, driver: GraphDriver, group_id: str) -> None:
        await self._read(driver, group_id, None, 0)

    async def audit(self, driver: GraphDriver, group_id: str) -> CoverageAudit:
        """Every episode identity this gate sees, with its verdict."""
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _COVERAGE_ROWS,
            group_id=group_id,
            representation=self.representation.metadata()["sha256"],
            dimension=self.representation.dimension,
        )
        return audit_rows(rows)

    async def search(
        self, driver: GraphDriver, group_id: str, vector: list[float], limit: int
    ) -> list[str]:
        _unit(vector, self.representation.dimension)
        return await self._read(driver, group_id, vector, limit)


def _candidates(candidates: object, limit: int) -> list[str]:
    if (
        not isinstance(candidates, list)
        or len(candidates) > limit
        or any(not isinstance(value, str) for value in candidates)
    ):
        raise FactVectorError("fact_vector_rebuild_required")
    return candidates


def audit_rows(rows: object) -> CoverageAudit:
    """Fold ``(uuid, valid)`` rows; any invalid row marks its identity."""
    if type(rows) is not list:
        raise FactVectorError("fact_vector_rebuild_required")
    present: set[str] = set()
    defective: set[str] = set()
    unidentified = 0
    for row in rows:
        if type(row) is not dict or set(row) != {"uuid", "valid"}:
            raise FactVectorError("fact_vector_rebuild_required")
        identity = row["uuid"]
        if type(identity) is not str:
            unidentified += 1
            continue
        present.add(identity)
        if row["valid"] is not True:
            defective.add(identity)
    return CoverageAudit(frozenset(present), frozenset(defective), unidentified)


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
