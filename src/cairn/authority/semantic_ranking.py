"""Catalogue-owned validation and deterministic integer semantic ranking.

Shape/query/resource checks precede catalogue admission. Body binding follows
admission; excluded identities cannot turn a hidden stale binding into a signal.
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cairn.authority.retrieval import RetrievedFact
from cairn.projection import semantic_evidence as evidence
from cairn.projection.partition import canonical_partition, canonical_segments_json


def units(*, lexical: int, grade: float | None, member: bool) -> int:
    if type(lexical) is not int or lexical < 0 or type(member) is not bool:
        raise ValueError("invalid_rank_input")
    if grade is not None:
        if type(grade) is not float or not math.isfinite(grade) or not 0 <= grade <= 1:
            raise ValueError("invalid_grade")
        bonus = math.floor(grade * 1_000_000 + 0.5)
    else:
        bonus = 500_000 if member else 0
    return lexical * 1_000_000 + bonus


def _hash(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def validate(
    value: object, query: str, partitions: tuple[str, ...]
) -> evidence.SemanticEvidence:
    """Reject hostile containers before iteration and metadata before hashing."""
    if type(value) is not evidence.SemanticEvidence:
        raise evidence.SemanticEvidenceError()
    if (
        not _hash(value.query_sha256)
        or value.query_sha256 != evidence.query_sha256(query)
        or not _hash(value.representation_sha256)
        or value.representation_sha256 != evidence.local_representation_sha256()
        or type(value.search_policy) is not str
        or value.search_policy != evidence.SEARCH_POLICY
        or type(value.candidate_ids) is not tuple
        or type(value.partitions) is not tuple
        or any(type(identity) is not UUID for identity in value.candidate_ids)
    ):
        raise evidence.SemanticEvidenceError()
    if not query.strip():
        if value.partitions or value.candidate_ids:
            raise evidence.SemanticEvidenceError()
        return value
    if not 1 <= len(value.partitions) == len(partitions) <= evidence.PARTITION_LIMIT:
        raise evidence.SemanticEvidenceError()
    max_length = max(map(len, partitions))

    def partition_key(key: object) -> bool:
        return type(key) is str and len(key) <= max_length and key in partitions

    seen_partitions: set[str] = set()
    # Count/container validation for every partition before processing grades.
    for partition in value.partitions:
        if (
            type(partition) is not evidence.PartitionGrades
            or not partition_key(partition.partition_key)
            or partition.partition_key in seen_partitions
            or partition.coverage_valid is not True
            or type(partition.eligible_count) is not int
            or not 0 <= partition.eligible_count <= 2**63 - 1
            or type(partition.grades) is not tuple
            or len(partition.grades)
            != min(partition.eligible_count, evidence.GRADE_LIMIT)
        ):
            raise evidence.SemanticEvidenceError()
        seen_partitions.add(partition.partition_key)
    seen: set[UUID] = set()
    for partition in value.partitions:
        for grade in partition.grades:
            if (
                type(grade) is not evidence.FactGrade
                or type(grade.fact_id) is not UUID
                or grade.fact_id in seen
                or not partition_key(grade.partition_key)
                or grade.partition_key != partition.partition_key
                or not _hash(grade.fingerprint)
                or type(grade.score) is not float
                or not math.isfinite(grade.score)
                or not 0.60 < grade.score <= 1
            ):
                raise evidence.SemanticEvidenceError()
            seen.add(grade.fact_id)
    return value


def reconcile(
    value: evidence.SemanticEvidence, admitted: dict[UUID, RetrievedFact]
) -> dict[UUID, float]:
    members = set(value.candidate_ids)
    grades: dict[UUID, float] = {}
    for partition in value.partitions:
        for grade in partition.grades:
            fact = admitted.get(grade.fact_id)
            if fact is None:
                continue  # R1: no body/partition binding checks for excluded IDs.
            if (
                fact.fact_id not in members
                or grade.partition_key
                != canonical_partition(
                    fact.scope.realm, canonical_segments_json(fact.scope.segments)
                )
                or grade.fingerprint != evidence.local_body_fingerprint(fact.body)
            ):
                raise evidence.SemanticEvidenceError()
            grades[fact.fact_id] = grade.score
    return grades


@dataclass(frozen=True, slots=True)
class Rank:
    units: int
    key: tuple[int, int, str]
    relevant: bool


def rank(fact: RetrievedFact, query: str, grade: float | None, member: bool) -> Rank:
    lexical = len(
        set(re.findall(r"\w+", fact.body.casefold()))
        & set(re.findall(r"\w+", query.casefold()))
    )
    value = units(lexical=lexical, grade=grade, member=member)
    age = fact.recorded_at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    micros = (age.days * 86400 + age.seconds) * 1_000_000 + age.microseconds
    return Rank(value, (-value, -micros, str(fact.fact_id)), lexical > 0 or member)
