"""Pure optional memory-recall advice, never admission or authenticated knowledge.

DTOs describe the wire between an explicitly selected source and authority.
Authority must validate even instances of these frozen types before using advice.
No provider, index driver or authority implementation is imported here.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

SEARCH_POLICY = "cairn.fact-local-search/v5"
GRADE_LIMIT = 256
PARTITION_LIMIT = 17


class SemanticEvidenceError(Exception):
    """Closed input-free failure, including translated source failures."""

    def __init__(self) -> None:
        super().__init__("semantic_evidence_invalid")


@dataclass(frozen=True, slots=True)
class FactGrade:
    fact_id: UUID
    partition_key: str
    fingerprint: str
    score: float


@dataclass(frozen=True, slots=True)
class PartitionGrades:
    partition_key: str
    coverage_valid: bool
    eligible_count: int
    grades: tuple[FactGrade, ...]


@dataclass(frozen=True, slots=True)
class SemanticEvidence:
    query_sha256: str
    representation_sha256: str
    search_policy: str
    candidate_ids: tuple[UUID, ...]
    partitions: tuple[PartitionGrades, ...]


class SemanticEvidenceSource(Protocol):
    def search_with_evidence(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> SemanticEvidence: ...


def local_representation_recipe() -> dict[str, str | int]:
    """Fresh recipe value; changing it changes the representation identity."""
    return {
        "version": "cairn.fact-local-vector/v5",
        "model": "text-embedding-3-small",
        "dimension": 1024,
        "dimension_truncation": "leading dimensions",
        "encoding": "UTF-8 strict; no normalisation; retain all whitespace",
        "unit_min_bytes": 32,
        "unit_max_bytes": 512,
        "line_boundaries": "CRLF as one delimiter; lone CR; lone LF; boundary after delimiter",
        "sentence_boundaries": ".!?,;: and U+3002 U+FF01 U+FF1F; boundary after punctuation only before whitespace or body end",
        "whitespace": "U+0009..000D U+001C..001F U+0020 U+0085 U+00A0 U+1680 U+2000..200A U+2028 U+2029 U+202F U+205F U+3000",
        "split": "earliest boundary >= start+32 and <= start+512; else greatest scalar boundary <= start+512; body end is boundary; final tail <32 allowed; no overlap",
        "dedup": "per fact exact full UTF-8 text; first occurrence slot order; retain all occurrence spans",
        "max_body_bytes": 65536,
        "max_unit_refs": 2048,
        "max_vectors": 2048,
        "max_embedded_bytes": 65536,
        "base_chunk_bytes": 2048,
        "max_base_chunks": 33,
        "max_total_embedded_bytes": 131072,
        "batch_texts": 32,
        "batch_bytes": 16384,
        "vector": "L2 unit; float32 quantisation",
        "pool": "local distinct units only; legacy bases only, unchanged cairn.fact-vector/v1",
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _utf8(value: str, limit: int) -> bytes:
    if type(value) is not str or len(value) > limit:
        raise SemanticEvidenceError()
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeError:
        raise SemanticEvidenceError() from None
    if len(raw) > limit:
        raise SemanticEvidenceError()
    return raw


def local_representation_sha256() -> str:
    return _hash(local_representation_recipe())


def local_body_fingerprint(body: str) -> str:
    return _hash(
        {
            "representation": local_representation_sha256(),
            "body_sha256": hashlib.sha256(_utf8(body, 65536)).hexdigest(),
        }
    )


def query_sha256(query: str) -> str:
    raw = _utf8(query, 8192)
    return hashlib.sha256(raw.replace(b"\n", b" ")).hexdigest()
