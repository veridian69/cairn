"""Private full-coverage fact units: bounded manifests and atomic publication.

The catalogue owns meaning and admission. Stored bindings are integrity metadata,
not authentication of externally replaced vectors or manifests.
"""

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from graphiti_core.driver.driver import GraphDriver

from cairn.projection.adapter import ProjectedFactState
from cairn.projection.fact_chunks import (
    EmbeddedLocalFact,
    UnitSpan,
    chunk_spans,
    embed_local,
)
from cairn.projection.fact_vectors import FactRepresentation, FactVectorError, _ready
from cairn.projection.semantic_evidence import (
    GRADE_LIMIT,
    PARTITION_LIMIT,
    SEARCH_POLICY,
    local_body_fingerprint,
    local_representation_recipe,
    local_representation_sha256,
)

_RECIPE = local_representation_recipe()
_HEX = re.compile(r"[0-9a-f]{64}\Z")
LOCAL_CUTOFF = 0.60


def policy_metadata(enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "representation": {
            **local_representation_recipe(),
            "sha256": local_representation_sha256(),
        }
        if enabled
        else None,
        "search": {
            "version": SEARCH_POLICY,
            "cutoff": LOCAL_CUTOFF,
            "comparison": "strictly greater than",
            "score": "max (1 + cosine)/2 over distinct unit vectors",
            "fact_limit_per_partition": GRADE_LIMIT,
            "partition_limit": PARTITION_LIMIT,
        }
        if enabled
        else None,
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _vector(value: object) -> list[float]:
    if type(value) is not list or len(value) != _RECIPE["dimension"]:
        raise FactVectorError()
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise FactVectorError()
    norm = math.hypot(*value)
    if not math.isfinite(norm) or abs(norm - 1.0) > 1e-5:
        raise FactVectorError()
    return list(value)


def _manifest(
    state: ProjectedFactState, group: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(group) is not str or not _HEX.fullmatch(group):
        raise FactVectorError()
    spans = chunk_spans(state.body)
    raw = state.body.encode("utf-8")
    identity = str(state.fact_id)
    fingerprint = local_body_fingerprint(state.body)
    recipe = local_representation_sha256()
    children: list[dict[str, Any]] = []
    for span in spans:
        if span.slot < len(children):
            continue
        text = raw[span.start : span.end]
        children.append(
            {
                "uuid": identity,
                "group_id": group,
                "representation": recipe,
                "fingerprint": fingerprint,
                "slot": span.slot,
                "dim": 1024,
                "input_bytes": len(text),
                "input_sha256": hashlib.sha256(text).hexdigest(),
                "key": _hash([identity, group, recipe, fingerprint, span.slot]),
            }
        )
    header = {
        "uuid": identity,
        "group_id": group,
        "representation": recipe,
        "fingerprint": fingerprint,
        "body_bytes": len(raw),
        "dim": 1024,
        "complete": True,
        "occurrence_count": len(spans),
        "vector_count": len(children),
        "starts": [s.start for s in spans],
        "ends": [s.end for s in spans],
        "slots": [s.slot for s in spans],
        "vector_keys": [c["key"] for c in children],
    }
    return header, children


def build_record(
    state: ProjectedFactState, group: str, values: EmbeddedLocalFact
) -> dict[str, Any]:
    header, children = _manifest(state, group)
    if type(values) is not EmbeddedLocalFact or type(values.spans) is not tuple:
        raise FactVectorError()
    if len(values.spans) != header["occurrence_count"]:
        raise FactVectorError()
    for index, span in enumerate(values.spans):
        if type(span) is not UnitSpan or any(
            type(x) is not int for x in (span.start, span.end, span.slot)
        ):
            raise FactVectorError()
        if (span.start, span.end, span.slot) != (
            header["starts"][index],
            header["ends"][index],
            header["slots"][index],
        ):
            raise FactVectorError()
    if type(values.vectors) is not tuple or len(values.vectors) != len(children):
        raise FactVectorError()
    for child, vector in zip(children, values.vectors, strict=True):
        child["embedding"] = _vector(vector)
    return {
        "header": header,
        "children": children,
        "pooled": {
            "uuid": str(state.fact_id),
            "group_id": group,
            "dim": 1024,
            "embedding": _vector(values.pooled),
        },
    }


# Exact fact identity in the already-selected partition; remove corrupt/obsolete
# private records too. All writes, including v1, are one engine statement.
_PUBLISH = """
OPTIONAL MATCH (h:CairnFactChunkSet {uuid:$uuid})
WITH collect(h) AS headers
OPTIONAL MATCH (c:CairnFactChunk {uuid:$uuid})
WITH headers, collect(c) AS children
OPTIONAL MATCH (v:CairnFactVector {uuid:$uuid})
WITH headers, children, collect(v) AS pooled
FOREACH (c IN children | DETACH DELETE c)
FOREACH (h IN headers | DETACH DELETE h)
FOREACH (v IN pooled | DETACH DELETE v)
CREATE (h:CairnFactChunkSet) SET h = $record.header
WITH h
UNWIND $record.children AS value
CREATE (c:CairnFactChunk) SET c = value
CREATE (h)-[:CAIRN_UNIT]->(c)
WITH DISTINCT h
CREATE (v:CairnFactVector) SET v = $record.pooled
RETURN h.uuid AS uuid
"""


async def publish(
    driver: GraphDriver,
    state: ProjectedFactState,
    group: str,
    values: EmbeddedLocalFact,
    representation: FactRepresentation,
) -> None:
    record = build_record(state, group, values)
    if representation.dimension != 1024 or representation.model != _RECIPE["model"]:
        raise FactVectorError()
    record["pooled"].update(
        representation=representation.metadata()["sha256"],
        fingerprint=representation.fingerprint(state.body),
    )
    rows, _, _ = await driver.execute_query(
        _PUBLISH, uuid=str(state.fact_id), record=record
    )
    if rows != [{"uuid": str(state.fact_id)}]:
        raise FactVectorError("fact_vector_rebuild_required")


def _integer(expression: str) -> str:
    return f"toLower(typeOf({expression})) = 'integer'"


def _hexadecimal(expression: str) -> str:
    return (
        f"toLower(typeOf({expression})) = 'string' AND size({expression}) = 64 "
        f"AND size(string.matchRegEx({expression},'^[0-9a-f]{{64}}$')) = 1"
    )


def _bounded_integer(expression: str, maximum: int) -> str:
    # CASE is inside the dangerous operand: an outer AND/CASE does not
    # prevent Falkor's eager range/arithmetic evaluation on null/corrupt data.
    return (
        f"CASE WHEN {_integer(expression)} AND {expression} >= 1 "
        f"AND {expression} <= {maximum} THEN {expression} ELSE 0 END"
    )


_GATHER = """
MATCH (e:Episodic)
OPTIONAL MATCH (h:CairnFactChunkSet {uuid:e.uuid})
OPTIONAL MATCH (h)-[r]->(linked)
WITH e, h, count(r) AS outgoing
OPTIONAL MATCH (c:CairnFactChunk {uuid:e.uuid})
OPTIONAL MATCH (h)-[unit_link]->(c)
WITH e, h, outgoing, c, count(unit_link) AS total,
     count(CASE WHEN type(unit_link) = 'CAIRN_UNIT' THEN unit_link ELSE null END) AS units
ORDER BY c.slot
WITH e, h, outgoing, collect(c) AS children,
     collect({total:total, units:units}) AS relationships
WITH e, collect({header:h, outgoing:outgoing, relationships:relationships, children:children}) AS versions
WITH e, versions, head(versions).header AS h,
     head(versions).outgoing AS outgoing,
     head(versions).relationships AS relationships,
     head(versions).children AS children
"""
_GATHER += (
    "WITH e, versions, h, outgoing, relationships, children, "
    + _bounded_integer("h.occurrence_count", 2048)
    + " AS occurrences, "
    + _bounded_integer("h.vector_count", 2048)
    + " AS vectors, "
    + _bounded_integer("h.body_bytes", 65536)
    + " AS body_bytes\n"
)

_VALID = " AND ".join(
    [
        "size(versions) = 1",
        "e.group_id = $group_id AND e.source_description = 'cairn.fact.projected'",
        "h.uuid = e.uuid AND h.group_id = $group_id AND h.representation = $representation",
        "toLower(typeOf(h.complete)) = 'boolean' AND h.complete = true",
        _hexadecimal("h.fingerprint"),
        _integer("h.dim") + " AND h.dim = 1024",
        _integer("h.body_bytes") + " AND h.body_bytes >= 1 AND h.body_bytes <= 65536",
        _integer("h.occurrence_count")
        + " AND h.occurrence_count >= 1 AND h.occurrence_count <= 2048 AND h.occurrence_count <= ceil(body_bytes / 32.0)",
        _integer("h.vector_count")
        + " AND h.vector_count >= 1 AND h.vector_count <= h.occurrence_count",
        "size(h.starts) = h.occurrence_count AND size(h.ends) = h.occurrence_count AND size(h.slots) = h.occurrence_count",
        "size(h.vector_keys) = h.vector_count AND size(children) = h.vector_count AND outgoing = h.vector_count AND size(relationships) = h.vector_count",
        "h.starts[0] = 0 AND h.ends[occurrences-1] = h.body_bytes",
        "all(i IN range(0,occurrences-1) WHERE "
        + " AND ".join(
            [
                _integer("h.starts[i]"),
                _integer("h.ends[i]"),
                _integer("h.slots[i]"),
                "h.starts[i] >= 0 AND h.ends[i] <= h.body_bytes",
                "h.ends[i]-h.starts[i] >= 1 AND h.ends[i]-h.starts[i] <= 512",
                "(i = occurrences-1 OR (h.ends[i]-h.starts[i] >= 32 AND h.ends[i] = h.starts[i+1]))",
                "h.slots[i] >= 0 AND h.slots[i] < h.vector_count",
                "children[h.slots[i]].input_bytes = h.ends[i]-h.starts[i]",
            ]
        )
        + ")",
        "reduce(first=[], slot IN h.slots | CASE WHEN slot IN first THEN first ELSE first+[slot] END) = range(0,vectors-1)",
        "all(i IN range(0,vectors-1) WHERE "
        + " AND ".join(
            [
                _integer("children[i].slot") + " AND children[i].slot = i",
                "children[i].uuid = e.uuid AND children[i].group_id = $group_id",
                "children[i].representation = h.representation AND children[i].fingerprint = h.fingerprint",
                _integer("children[i].dim") + " AND children[i].dim = 1024",
                _integer("children[i].input_bytes")
                + " AND children[i].input_bytes >= 1 AND children[i].input_bytes <= 512",
                _hexadecimal("children[i].input_sha256"),
                _hexadecimal("children[i].key"),
                "children[i].key = h.vector_keys[i]",
                "relationships[i].total = 1 AND relationships[i].units = 1",
                "size(children[i].embedding) = 1024",
                # Multiplication rejects non-numeric values; null and non-finite
                # results fail the comparison. Non-negative squares near one
                # also bound every component without a separate traversal.
                "abs(sqrt(reduce(norm=0.0, x IN children[i].embedding | norm+(1.0*x)*x))-1.0) <= 0.00001",
            ]
        )
        + ")",
    ]
)

# Preflight needs only partition validity, not scores or candidate envelopes.
# Count invalid rows without grouping so an empty partition is healthy. Keep
# the gathered domain and all validation (including child-slot ordering) intact.
_COVERAGE_ONLY = (
    _GATHER
    + "WITH coalesce("
    + _VALID
    + ", false) AS valid\n"
    + "RETURN count(CASE WHEN valid THEN null ELSE 1 END) = 0 AS coverage_valid\n"
)


# Shared predicate; coverage is retained even for zero hits. Never select chunks
# before validating all retained facts. Scores examine distinct children once.
_SEARCH = (
    _GATHER
    + "WITH e, h, children, coalesce("
    + _VALID
    + ", false) AS valid\n"
    + """
WITH e, h, valid, CASE WHEN valid AND $scoring THEN
     [c IN children | (2-vec.cosineDistance(vecf32(CASE WHEN valid THEN c.embedding ELSE $query_vector END),vecf32($query_vector)))/2]
     ELSE [] END AS scores
WITH e.uuid AS uuid, h.fingerprint AS fingerprint, valid,
     reduce(best=-1.0, score IN scores | CASE WHEN score > best THEN score ELSE best END) AS score
ORDER BY score DESC, uuid ASC
WITH collect({uuid:uuid, fingerprint:fingerprint, valid:valid, score:score}) AS rows
WITH rows, [row IN rows WHERE row.valid AND row.score > $cutoff] AS eligible
RETURN all(row IN rows WHERE row.valid) AS coverage_valid,
       size(eligible) AS eligible_count,
       [row IN eligible | {uuid:row.uuid, fingerprint:row.fingerprint, score:row.score}][..$limit] AS grades
"""
)


_REUSE = """
MATCH (h:CairnFactChunkSet {uuid:$uuid})
OPTIONAL MATCH (h)-[r]->(linked)
WITH h, count(r) AS outgoing
OPTIONAL MATCH (v:CairnFactVector {uuid:$uuid})
WITH h, outgoing, collect(v) AS pooled
OPTIONAL MATCH (c:CairnFactChunk {uuid:$uuid})
OPTIONAL MATCH (h)-[unit_link]->(c)
WITH h, outgoing, pooled, c, count(unit_link) AS total,
     count(CASE WHEN type(unit_link) = 'CAIRN_UNIT' THEN unit_link ELSE null END) AS units
ORDER BY c.slot
WITH h, outgoing, pooled, collect(properties(c)) AS children,
     collect({total:total, units:units}) AS relationships
RETURN properties(h) AS header, outgoing, children, relationships,
       [v IN pooled | properties(v)] AS pooled
"""


def _same(actual: object, expected: object) -> bool:
    # JSON distinguishes integer/float/bool, unlike Python numeric equality.
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
        expected, sort_keys=True, allow_nan=False
    )


class FactUnitIndex:
    def __init__(self, representation: FactRepresentation) -> None:
        if representation.dimension != 1024 or representation.model != _RECIPE["model"]:
            raise FactVectorError("fact_local_unavailable")
        self.representation = representation

    def _reusable(self, rows: Any, state: ProjectedFactState, group: str) -> bool:
        try:
            if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
                return False
            header, expected_children = _manifest(state, group)
            row = rows[0]
            children, pooled = row["children"], row["pooled"]
            if (
                type(row["header"]) not in (dict, OrderedDict)
                or not _same(row["header"], header)
                or type(children) is not list
                or len(children) != len(expected_children)
                or type(pooled) is not list
                or len(pooled) != 1
            ):
                return False
            relationships = row["relationships"]
            if (
                type(row["outgoing"]) is not int
                or row["outgoing"] != len(expected_children)
                or type(relationships) is not list
                or len(relationships) != len(expected_children)
                or any(
                    type(item) not in (dict, OrderedDict)
                    or not _same(item, {"total": 1, "units": 1})
                    for item in relationships
                )
            ):
                return False
            for actual, expected in zip(children, expected_children, strict=True):
                if type(actual) not in (dict, OrderedDict) or not _same(
                    {k: v for k, v in actual.items() if k != "embedding"}, expected
                ):
                    return False
                _vector(actual["embedding"])
            expected_pool = {
                "uuid": str(state.fact_id),
                "group_id": group,
                "dim": 1024,
                "representation": self.representation.metadata()["sha256"],
                "fingerprint": self.representation.fingerprint(state.body),
            }
            if type(pooled[0]) not in (dict, OrderedDict) or not _same(
                {k: v for k, v in pooled[0].items() if k != "embedding"}, expected_pool
            ):
                return False
            _vector(pooled[0]["embedding"])
            return True
        except (KeyError, TypeError, ValueError, FactVectorError):
            return False

    async def ensure(
        self, driver: GraphDriver, group: str, states: Sequence[ProjectedFactState]
    ) -> None:
        if not states:
            return
        await _ready(driver)
        for label in ("CairnFactChunkSet", "CairnFactChunk", "CairnFactVector"):
            await driver.execute_query(f"CREATE INDEX FOR (n:{label}) ON (n.uuid)")
        for state in states:
            rows, _, _ = await driver.execute_query(_REUSE, uuid=str(state.fact_id))
            if self._reusable(rows, state, group):
                continue
            values = await embed_local(state.body, self.representation)
            await publish(driver, state, group, values, self.representation)

    async def _read(
        self, driver: GraphDriver, group: str, vector: list[float] | None
    ) -> tuple[int, tuple[tuple[str, str, float], ...]]:
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _SEARCH,
            group_id=group,
            representation=local_representation_sha256(),
            scoring=vector is not None,
            query_vector=vector if vector is not None else [1.0] + [0.0] * 1023,
            cutoff=LOCAL_CUTOFF,
            limit=GRADE_LIMIT,
        )
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            raise FactVectorError("fact_vector_rebuild_required")
        row = rows[0]
        count, grades = row.get("eligible_count"), row.get("grades")
        if (
            row.get("coverage_valid") is not True
            or type(count) is not int
            or not 0 <= count <= 2**63 - 1
            or type(grades) is not list
            or len(grades) != min(count, GRADE_LIMIT)
        ):
            raise FactVectorError("fact_vector_rebuild_required")
        hits = []
        seen = set()
        for grade in grades:
            if type(grade) not in (dict, OrderedDict):
                raise FactVectorError("fact_vector_rebuild_required")
            identity, fingerprint, score = (
                grade.get("uuid"),
                grade.get("fingerprint"),
                grade.get("score"),
            )
            if (
                type(identity) is not str
                or len(identity) != 36
                or identity in seen
                or type(fingerprint) is not str
                or not _HEX.fullmatch(fingerprint)
                or type(score) is not float
                or not math.isfinite(score)
                or not LOCAL_CUTOFF < score <= 1.0 + 1e-6
            ):
                raise FactVectorError("fact_vector_rebuild_required")
            try:
                if str(UUID(identity)) != identity:
                    raise ValueError
            except ValueError:
                raise FactVectorError("fact_vector_rebuild_required") from None
            seen.add(identity)
            hits.append((identity, fingerprint, min(score, 1.0)))
        if hits != sorted(hits, key=lambda hit: (-hit[2], hit[0])):
            raise FactVectorError("fact_vector_rebuild_required")
        return count, tuple(hits)

    async def preflight(self, driver: GraphDriver, group: str) -> None:
        await _ready(driver)
        rows, _, _ = await driver.execute_query(
            _COVERAGE_ONLY,
            group_id=group,
            representation=local_representation_sha256(),
        )
        if (
            type(rows) is not list
            or len(rows) != 1
            or type(rows[0]) is not dict
            or set(rows[0]) != {"coverage_valid"}
            or rows[0]["coverage_valid"] is not True
        ):
            raise FactVectorError("fact_vector_rebuild_required")

    async def search(
        self, driver: GraphDriver, group: str, vector: list[float]
    ) -> tuple[int, tuple[tuple[str, str, float], ...]]:
        return await self._read(driver, group, _vector(vector))
