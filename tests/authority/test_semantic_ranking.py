"""Task 1 graded authority mechanics; no embedding-quality claim.

No numerical half-ties, representation dimensions or provider quality assertions.
The planned local_representation_sha256() supplies the shared recipe identity;
these tests do not independently choose a representation dimension.
Missing seams fail at explicit capability assertions, not collection imports.
"""

import hashlib
import importlib
import importlib.util
import inspect
import itertools
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
import test_retrieval as support
from test_memory import _memory

from cairn.authority.memory import CairnMemory
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_types import Recall, RecallResult
from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import Rejected
from cairn.projection.partition import canonical_partition, canonical_segments_json
from cairn.projection.semantic_evidence import SemanticEvidenceSource

QUERY = "orbital instrument"
OLD_BODY = "quartz calibration baseline"
NEW_BODY = "cupboard maintenance rota"
EXCLUDED_BODY = "private excluded specimen"
NOW = support._NOW + timedelta(days=100)
PARTITION = canonical_partition(
    support._SCOPE.realm, canonical_segments_json(support._SCOPE.segments)
)
PARTITIONS = (canonical_partition(support._SCOPE.realm, "[]"), PARTITION)


def required_module(name: str) -> ModuleType:
    assert importlib.util.find_spec(name) is not None, f"RED Task 1: missing {name}"
    return importlib.import_module(name)


def test_shared_recipe_and_unicode_bindings_have_independent_golden_bytes() -> None:
    api = required_module("cairn.projection.semantic_evidence")
    assert api.local_representation_recipe() == {
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
    assert api.SEARCH_POLICY == "cairn.fact-local-search/v5"
    assert (
        api.local_representation_sha256()
        == "8dc6d82b0e86630b9529e1735ea037d9583f55a99384f5558b49b022cabb1229"
    )
    assert (
        api.local_body_fingerprint("e\u0301雪\r\n")
        == "794ffb5b36f68e3bc9ce5c830e71fb2a79d387da46caec705d7f8e08756a4863"
    )
    assert (
        api.query_sha256("雪\r\nx")
        == "9895907f1c0dc7528e362be78e3eb77963ea36df7e7fb2fad91be1821a2e0a60"
    )
    recipe = api.local_representation_recipe()
    recipe.clear()
    assert api.local_representation_sha256() == (
        "8dc6d82b0e86630b9529e1735ea037d9583f55a99384f5558b49b022cabb1229"
    )
    assert api.local_body_fingerprint("é") != api.local_body_fingerprint("e\u0301")


@pytest.mark.parametrize(
    "helper,value",
    [
        ("query_sha256", "\ud800"),
        ("local_body_fingerprint", "\ud800"),
        ("query_sha256", "x" * 8193),
        ("local_body_fingerprint", "x" * 65537),
    ],
    ids=["query-unicode", "body-unicode", "query-overflow", "body-overflow"],
)
def test_shared_binding_rejects_invalid_input_without_echo(
    helper: str, value: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    with pytest.raises(api.SemanticEvidenceError) as error:
        getattr(api, helper)(value)
    assert str(error.value) == "semantic_evidence_invalid"


@pytest.mark.parametrize(
    "lexical,grade,member,want",
    [
        (0, 0.8, True, 800_000),
        (0, None, True, 500_000),
        (2, 0.7, True, 2_700_000),
        (0, None, False, 0),
    ],
    ids=["graded", "ungraded-member", "lexical-plus-grade", "neither"],
)
def test_units_preserves_lexical_and_distinct_semantic_evidence(
    lexical: int, grade: float | None, member: bool, want: int
) -> None:
    ranking = required_module("cairn.authority.semantic_ranking")
    actual = ranking.units(lexical=lexical, grade=grade, member=member)
    assert type(actual) is int and actual == want


@pytest.mark.parametrize(
    "grade",
    [True, False, 0, 1, "0.8", float("nan"), float("inf"), float("-inf"), -0.01, 1.01],
    ids=[
        "true",
        "false",
        "int-zero",
        "int-one",
        "string",
        "nan",
        "infinity",
        "negative-infinity",
        "below-range",
        "above-range",
    ],
)
def test_units_rejects_malformed_grade_instead_of_clamping(grade: Any) -> None:
    ranking = required_module("cairn.authority.semantic_ranking")
    with pytest.raises((TypeError, ValueError)):
        ranking.units(lexical=0, grade=grade, member=True)


def binding(representation: str, body: str) -> str:
    # Independently reconstruct the existing exact-body binding recipe; no vectors.
    return hashlib.sha256(
        json.dumps(
            {
                "representation": representation,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class EvidenceSource:
    """Only the external advice is scripted; admission/ranking/catalogue are real."""

    def __init__(self, packet: Any) -> None:
        self.packet = packet
        self.calls = 0

    def search_with_evidence(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> Any:
        assert (query, limit, partition_keys) == (QUERY, 256, PARTITIONS)
        self.calls += 1
        return self.packet


def packet(
    api: ModuleType,
    old: UUID,
    recent: UUID,
    excluded: UUID | None = None,
    *,
    stale: bool = False,
) -> Any:
    representation = api.local_representation_sha256()
    grades = [
        api.FactGrade(old, PARTITION, binding(representation, OLD_BODY), 0.8),
        api.FactGrade(recent, PARTITION, binding(representation, NEW_BODY), 0.61),
    ]
    if excluded is not None:
        grades.append(
            api.FactGrade(
                excluded,
                PARTITION,
                binding(
                    representation, "stale private specimen" if stale else EXCLUDED_BODY
                ),
                0.9,
            )
        )
    return api.SemanticEvidence(
        query_sha256=hashlib.sha256(QUERY.replace("\n", " ").encode()).hexdigest(),
        representation_sha256=representation,
        search_policy="cairn.fact-local-search/v5",
        candidate_ids=tuple(grade.fact_id for grade in grades),
        partitions=(
            api.PartitionGrades(PARTITIONS[0], True, 0, ()),
            api.PartitionGrades(PARTITION, True, len(grades), tuple(grades)),
        ),
    )


def graded_memory(path: Path, source: SemanticEvidenceSource) -> CairnMemory:
    assert "semantic_evidence" in inspect.signature(CairnMemory).parameters, (
        "RED Task 1: CairnMemory lacks explicit semantic_evidence injection"
    )
    existing = _memory(path, now=NOW)
    return CairnMemory(
        path,
        existing._transactions,
        existing._clock,
        existing._uuid_factory,
        existing._screen,
        semantic_evidence=source,
    )


def seed_state(
    path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> tuple[UUID, UUID, UUID | None]:
    # Isolated real catalogues have identical visible IDs/evidence/dates. Reset only
    # the existing test helper's ID generator, scoped to this setup invocation.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with monkeypatch.context() as scoped:
        scoped.setattr(support, "_UUID_SEEDS", itertools.count(0x70000000, 0x00100000))
        support._seed_catalogue(path, realms=(support._REALM, support._OTHER_REALM))
        support._seed_agent(path, segments=(), read_clearance=Classification.INTERNAL)
        support._insert_grant(
            path,
            grant_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            realm_id=support._OTHER_REALM,
            segments=(),
            read_clearance=Classification.INTERNAL,
        )
        old = support._ingest_facts(path, bodies=(OLD_BODY,))[0]
        recent = support._ingest_facts(path, bodies=(NEW_BODY,), now=NOW)[0]
        if state == "unknown":
            return old, recent, None
        scope = (
            Scope(support._OTHER_REALM, support._SCOPE.segments)
            if state == "foreign"
            else support._SCOPE
        )
        excluded = support._ingest_facts(
            path,
            bodies=(EXCLUDED_BODY,),
            scope=scope,
            classification=Classification.RESTRICTED
            if state == "hidden"
            else Classification.INTERNAL,
            valid_from=NOW + timedelta(days=1) if state == "future" else None,
            valid_to=NOW if state == "expired" else None,
        )[0]
        if state == "superseded":
            support._invalidate_fact(path, excluded, now=NOW)
        return old, recent, excluded


@pytest.mark.parametrize("candidate_limit", [None, 1], ids=["all", "one"])
def test_older_higher_grade_beats_newer_distractor_before_candidate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_limit: int | None
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    source = EvidenceSource(packet(api, old, recent))
    result = graded_memory(tmp_path, source).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, budget=65536),
        correlation_id=support._CORRELATION_ID,
        _candidate_limit=candidate_limit,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == (
        [old, recent] if candidate_limit is None else [old]
    )
    assert [hit.relevance_score for hit in result.hits] == (
        [0.8, 0.61] if candidate_limit is None else [0.8]
    )
    assert result.policy == "lexical-graded/v2" and not result.semantic_degraded
    assert source.calls == 1


@pytest.mark.parametrize(
    "stale", [False, True], ids=["matching-binding", "stale-binding"]
)
def test_r1_excluded_catalogue_state_cannot_change_visible_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    original_binding = api.local_body_fingerprint
    bound_bodies: list[str] = []

    def visible_binding(body: str) -> str:
        bound_bodies.append(body)
        return str(original_binding(body))

    monkeypatch.setattr(api, "local_body_fingerprint", visible_binding)
    fixed_packet: Any = None
    excluded_id: UUID | None = None
    expected: RecallResult | None = None
    expected_bytes: bytes | None = None
    for state in ("hidden", "foreign", "superseded", "unknown"):
        old, recent, excluded = seed_state(tmp_path / state, monkeypatch, state)
        if fixed_packet is None:
            assert excluded is not None
            excluded_id = excluded
            fixed_packet = packet(api, old, recent, excluded, stale=stale)
        else:
            assert excluded is None or excluded == excluded_id
        source = EvidenceSource(fixed_packet)  # Exactly the same advisory record.
        result = graded_memory(tmp_path / state, source).recall(
            support._agent_actor(),
            Recall(support._SCOPE, QUERY, budget=65536),
            correlation_id=support._CORRELATION_ID,
            _candidate_limit=1,
        )
        assert isinstance(result, RecallResult)
        assert [hit.fact.fact_id for hit in result.hits] == [old]
        assert result.hits[0].relevance_score == 0.8
        assert result.policy == "lexical-graded/v2" and not result.semantic_degraded
        assert result.budget_exhausted  # Two eligible visible facts, one slot.
        disclosed = json.dumps(
            memory_value(result),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert str(excluded_id).encode() not in disclosed
        assert EXCLUDED_BODY.encode() not in disclosed
        if expected is None:
            expected, expected_bytes = result, disclosed
        assert result == expected  # Includes scores, bodies, byte accounting, flags.
        assert disclosed == expected_bytes
        assert source.calls == 1
        assert EXCLUDED_BODY not in bound_bodies


def test_correction_during_search_cannot_mix_catalogue_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")

    class CorrectingSource(EvidenceSource):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            if not self.calls:
                support._invalidate_fact(tmp_path, old, now=NOW)
            return super().search_with_evidence(query, limit, partition_keys)

    service = graded_memory(tmp_path, CorrectingSource(packet(api, old, recent)))
    first = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    second = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(first, RecallResult) and isinstance(second, RecallResult)
    assert [hit.fact.fact_id for hit in first.hits] == [old, recent]
    assert [hit.fact.fact_id for hit in second.hits] == [recent]
    assert not first.semantic_degraded and not second.semantic_degraded


class HostileTuple(tuple[Any, ...]):
    def __iter__(self) -> Any:
        pytest.fail("container subclass must be rejected before iteration")

    def __len__(self) -> int:
        pytest.fail("container subclass must be rejected before length")


class HostileIterable:
    def __iter__(self) -> Any:
        pytest.fail("lazy advice must never be materialised")


BAD_ENVELOPES = [
    "query",
    "recipe",
    "policy",
    "candidate-string",
    "candidate-subclass",
    "candidate-lazy",
    "partitions-subclass",
    "partitions-lazy",
    "grades-subclass",
    "grades-lazy",
    "count-bool",
    "count-negative",
    "count-overflow",
    "count-mismatch",
    "coverage",
    "missing-partition",
    "duplicate-partition",
    "extra-partition",
    "partition-length",
    "partition-foreign",
    "partition-type",
    "duplicate-grade",
    "grade-bool",
    "grade-int",
    "grade-nan",
    "grade-infinity",
    "grade-negative",
    "grade-overflow",
    "grade-cutoff",
    "grade-id",
    "grade-partition",
    "fingerprint",
]


def corrupt(document: Any, case: str) -> Any:
    partition = document.partitions[1]
    grade = partition.grades[0]
    if case in {"query", "recipe", "policy"}:
        return replace(
            document,
            **{
                {
                    "query": "query_sha256",
                    "recipe": "representation_sha256",
                    "policy": "search_policy",
                }[case]: "a" * 64
            },
        )
    if case.startswith("candidate-"):
        values = {
            "candidate-string": (str(grade.fact_id),),
            "candidate-subclass": HostileTuple(document.candidate_ids),
            "candidate-lazy": HostileIterable(),
        }
        return replace(document, candidate_ids=values[case])
    if case.startswith("partitions-"):
        return replace(
            document,
            partitions=HostileTuple(document.partitions)
            if case.endswith("subclass")
            else HostileIterable(),
        )
    if case.startswith("grades-"):
        partition = replace(
            partition,
            grades=HostileTuple(partition.grades)
            if case.endswith("subclass")
            else HostileIterable(),
        )
    elif case.startswith("count-"):
        partition = replace(
            partition,
            eligible_count={
                "count-bool": True,
                "count-negative": -1,
                "count-overflow": 2**63,
                "count-mismatch": 1,
            }[case],
        )
    elif case == "coverage":
        partition = replace(partition, coverage_valid=False)
    elif case in {"missing-partition", "duplicate-partition", "extra-partition"}:
        values = {
            "missing-partition": (partition,),
            "duplicate-partition": (partition, partition),
            "extra-partition": document.partitions + (partition,),
        }
        return replace(document, partitions=values[case])
    elif case.startswith("partition-"):
        partition = replace(
            partition,
            partition_key={
                "partition-length": "x" * 100_000,
                "partition-foreign": "other",
                "partition-type": 1,
            }[case],
        )
    elif case == "duplicate-grade":
        partition = replace(partition, grades=(grade, grade))
    else:
        if case == "grade-id":
            grade = replace(grade, fact_id=str(grade.fact_id))
        elif case == "grade-partition":
            grade = replace(grade, partition_key=PARTITIONS[0])
        elif case == "fingerprint":
            grade = replace(grade, fingerprint="A" * 64)
        else:
            grade = replace(
                grade,
                score={
                    "grade-bool": True,
                    "grade-int": 1,
                    "grade-nan": float("nan"),
                    "grade-infinity": float("inf"),
                    "grade-negative": -0.1,
                    "grade-overflow": 1.1,
                    "grade-cutoff": 0.6,
                }[case],
            )
        partition = replace(partition, grades=(grade, partition.grades[1]))
    return replace(document, partitions=(document.partitions[0], partition))


@pytest.mark.parametrize("case", BAD_ENVELOPES)
def test_shape_and_resource_validation_precedes_catalogue_binding(case: str) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    ranking = required_module("cairn.authority.semantic_ranking")
    document = packet(api, UUID(int=1, version=4), UUID(int=2, version=4))
    with pytest.raises(api.SemanticEvidenceError):
        ranking.validate(corrupt(document, case), QUERY, PARTITIONS)


@pytest.mark.parametrize("version", [2, 3, 4], ids=["v2", "v3", "v4"])
def test_shared_v5_refuses_old_source_policy(version: int) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    ranking = required_module("cairn.authority.semantic_ranking")
    document = packet(api, UUID(int=1, version=4), UUID(int=2, version=4))
    with pytest.raises(api.SemanticEvidenceError, match="^semantic_evidence_invalid$"):
        ranking.validate(
            replace(document, search_policy=f"cairn.fact-local-search/v{version}"),
            QUERY,
            PARTITIONS,
        )


def test_shared_v5_source_envelope_is_accepted() -> None:
    api = required_module("cairn.projection.semantic_evidence")
    ranking = required_module("cairn.authority.semantic_ranking")
    document = packet(api, UUID(int=1, version=4), UUID(int=2, version=4))
    assert ranking.validate(document, QUERY, PARTITIONS) is document


def test_full_grade_bounds_and_int64_counts_do_not_impose_a_candidate_union_cap() -> (
    None
):
    api = required_module("cairn.projection.semantic_evidence")
    ranking = required_module("cairn.authority.semantic_ranking")
    partitions = tuple(f"acme\npartition-{i}" for i in range(17))
    candidate_ids = tuple(UUID(int=i + 1, version=4) for i in range(9000))
    document = api.SemanticEvidence(
        api.query_sha256(QUERY),
        api.local_representation_sha256(),
        api.SEARCH_POLICY,
        candidate_ids,
        tuple(
            api.PartitionGrades(
                key,
                True,
                2**63 - 1,
                tuple(
                    api.FactGrade(candidate_ids[p * 256 + i], key, "0" * 64, 0.8)
                    for i in range(256)
                ),
            )
            for p, key in enumerate(partitions)
        ),
    )
    assert ranking.validate(document, QUERY, partitions) is document
    with pytest.raises(FrozenInstanceError):
        document.search_policy = "changed"
    with pytest.raises(FrozenInstanceError):
        document.partitions[0].grades[0].score = 0.1
    overflow = replace(
        document.partitions[0],
        grades=document.partitions[0].grades + (document.partitions[1].grades[0],),
    )
    with pytest.raises(api.SemanticEvidenceError):
        ranking.validate(
            replace(document, partitions=(overflow, *document.partitions[1:])),
            QUERY,
            partitions,
        )


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "eligible-body",
        "eligible-partition",
        "not-member",
        "query",
        "coverage",
        "grade-bool",
    ],
)
def test_failed_enabled_source_discards_all_advice_and_never_calls_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    if failure in {"eligible-body", "eligible-partition"}:
        grade = document.partitions[1].grades[0]
        if failure == "eligible-body":
            grade = replace(grade, fingerprint="0" * 64)
            document = replace(
                document,
                partitions=(
                    document.partitions[0],
                    replace(
                        document.partitions[1],
                        grades=(grade, document.partitions[1].grades[1]),
                    ),
                ),
            )
        else:
            grade = replace(grade, partition_key=PARTITIONS[0])
            document = replace(
                document,
                partitions=(
                    replace(document.partitions[0], eligible_count=1, grades=(grade,)),
                    replace(
                        document.partitions[1],
                        eligible_count=1,
                        grades=(document.partitions[1].grades[1],),
                    ),
                ),
            )
    elif failure == "not-member":
        document = replace(document, candidate_ids=(recent,))
    elif failure != "timeout":
        document = corrupt(document, failure)

    class FailingSource(EvidenceSource):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            result = super().search_with_evidence(query, limit, partition_keys)
            if failure == "timeout":
                raise TimeoutError("private provider explanation")
            return result

    class NoLegacy(support._ScriptedIndex):
        attempts = 0

        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            self.attempts += 1
            return (old, recent)

    source = FailingSource(document)
    legacy = NoLegacy()
    service = graded_memory(tmp_path, source)
    service._index = legacy
    result = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    baseline = _memory(tmp_path, now=NOW).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult) and isinstance(baseline, RecallResult)
    assert result.hits == baseline.hits
    assert result.budget_consumed == baseline.budget_consumed
    assert result.budget_exhausted == baseline.budget_exhausted
    assert (
        result.semantic_degraded
        and result.policy == baseline.policy + "; semantic-unavailable"
    )
    assert "private" not in repr(result)
    assert source.calls == 1 and legacy.attempts == 0


@pytest.mark.parametrize("query", [QUERY, " \n "])
def test_healthy_empty_advice_is_not_failure_and_age_alone_is_not_relevant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    seed_state(tmp_path, monkeypatch, "unknown")
    document = api.SemanticEvidence(
        api.query_sha256(query),
        api.local_representation_sha256(),
        api.SEARCH_POLICY,
        (),
        tuple(api.PartitionGrades(p, True, 0, ()) for p in PARTITIONS)
        if query.strip()
        else (),
    )

    class EmptySource:
        def search_with_evidence(
            self, received: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            assert received == query
            return document

    service = graded_memory(tmp_path, EmptySource())
    result = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, query, relevant_only=True, budget=1),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert result.hits == () and result.budget_consumed == 0
    assert not result.semantic_degraded and not result.budget_exhausted


def test_budget_is_applied_to_whole_ranked_records_and_accounts_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    service = graded_memory(tmp_path, EvidenceSource(packet(api, old, recent)))
    full = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(full, RecallResult)
    size = len(
        json.dumps(
            memory_value(full.hits[0]),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )
    bounded = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, budget=size),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(bounded, RecallResult)
    assert bounded.hits == (full.hits[0],)
    assert bounded.budget_consumed == size and bounded.budget_exhausted


@pytest.mark.parametrize("kind", ["endpoint", "quantised", "uuid"])
def test_exact_ties_use_recency_then_uuid_without_changing_relevant_only(
    tmp_path: Path, kind: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    support._seed_catalogue(tmp_path)
    support._seed_agent(tmp_path, segments=())
    first_body = "orbital" if kind == "endpoint" else OLD_BODY
    old = support._ingest_facts(
        tmp_path, bodies=(first_body,), now=NOW if kind == "uuid" else support._NOW
    )[0]
    recent = support._ingest_facts(tmp_path, bodies=(NEW_BODY,), now=NOW)[0]
    document = packet(api, old, recent)
    grades = document.partitions[1].grades
    if kind == "endpoint":
        grades = (replace(grades[1], score=1.0),)
        document = replace(document, candidate_ids=(recent,))
    else:
        grades = (
            replace(grades[0], score=0.8000001),
            replace(grades[1], score=0.8000002),
        )
    document = replace(
        document,
        partitions=(
            document.partitions[0],
            replace(document.partitions[1], eligible_count=len(grades), grades=grades),
        ),
    )
    result = graded_memory(tmp_path, EvidenceSource(document)).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, relevant_only=True),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult) and not result.semantic_degraded
    assert [hit.fact.fact_id for hit in result.hits] == (
        sorted([old, recent], key=str) if kind == "uuid" else [recent, old]
    )
    assert [hit.relevance_score for hit in result.hits] == (
        [1.0, 1.0] if kind == "endpoint" else [0.8, 0.8]
    )


def test_ungraded_union_members_keep_half_bonus_and_absent_capability_is_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    document = replace(
        document,
        partitions=(
            document.partitions[0],
            replace(
                document.partitions[1],
                eligible_count=1,
                grades=(document.partitions[1].grades[0],),
            ),
        ),
    )
    result = graded_memory(tmp_path, EvidenceSource(document)).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, relevant_only=True),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.relevance_score for hit in result.hits] == [0.8, 0.5]
    legacy = _memory(tmp_path, now=NOW)
    index = support._ScriptedIndex((old, recent))
    legacy._index = index
    previous = legacy.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(previous, RecallResult)
    assert (
        previous.policy.startswith("lexical-age/v1") and not previous.semantic_degraded
    )
    assert [hit.fact.fact_id for hit in previous.hits] == [recent, old]
    assert len(index.calls) == 1


def test_trust_excluded_stale_grade_is_not_bound_or_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    document = replace(
        document,
        partitions=(
            document.partitions[0],
            replace(
                document.partitions[1],
                grades=tuple(
                    replace(grade, fingerprint="0" * 64)
                    for grade in document.partitions[1].grades
                ),
            ),
        ),
    )
    result = graded_memory(tmp_path, EvidenceSource(document)).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY, trust_filters=frozenset({TrustClass.CANDIDATE})),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert not result.semantic_degraded and result.hits == ()


def revoke_reader(path: Path) -> None:
    with _open_write_connection(path, create=False) as connection:
        connection.execute(
            "INSERT INTO grant_revocations (grant_id, revoked_at, revoked_by, reason_code) SELECT grant_id, ?, ?, 'test_revocation' FROM grants WHERE realm_id = ?",
            (canonical_timestamp(NOW), str(support._AGENT_ID), support._REALM),
        )
        connection.commit()


def test_grant_revocation_during_search_applies_to_next_snapshot_not_response_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")

    class RevokingSource(EvidenceSource):
        def search_with_evidence(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> Any:
            revoke_reader(tmp_path)
            return super().search_with_evidence(query, limit, partition_keys)

    source = RevokingSource(packet(api, old, recent))
    service = graded_memory(tmp_path, source)
    first = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    second = service.recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(first, RecallResult) and len(first.hits) == 2
    assert not first.semantic_degraded
    assert (
        isinstance(second, Rejected)
        and second.failure.code.value == "authorisation_denied"
    )
    assert source.calls == 1


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "source-failure",
        "binding-failure",
        "refused",
        "bad-query",
        "unicode-error",
        "audit-failure",
    ],
)
def test_enabled_read_snapshot_closes_on_success_fallback_refusal_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import cairn.authority.memory as memory

    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    source = EvidenceSource(packet(api, old, recent))
    if mode == "refused":
        revoke_reader(tmp_path)
    if mode == "binding-failure":
        source.packet = replace(source.packet, candidate_ids=(recent,))
    if mode == "source-failure":
        source.packet = None
    service = graded_memory(tmp_path, source)
    opened: list[sqlite3.Connection] = []
    original = read_connection
    statements: list[str] = []

    @contextmanager
    def tracked(path: Path) -> Iterator[sqlite3.Connection]:
        with original(path) as connection:
            opened.append(connection)
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(memory, "read_connection", tracked)
    audit = service._audit_read

    def closed_before_audit(*args: Any, **kwargs: Any) -> None:
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        if mode == "audit-failure":
            raise RuntimeError("synthetic audit unavailable")
        audit(*args, **kwargs)

    monkeypatch.setattr(service, "_audit_read", closed_before_audit)
    query = (
        "" if mode == "bad-query" else "\ud800" if mode == "unicode-error" else QUERY
    )
    if mode in {"unicode-error", "audit-failure"}:
        with pytest.raises((UnicodeError, RuntimeError)):
            service.recall(
                support._agent_actor(),
                Recall(support._SCOPE, query),
                correlation_id=support._CORRELATION_ID,
            )
    else:
        result = service.recall(
            support._agent_actor(),
            Recall(support._SCOPE, query),
            correlation_id=support._CORRELATION_ID,
        )
        assert isinstance(
            result, Rejected if mode in {"bad-query", "refused"} else RecallResult
        )
    assert opened
    assert statements[0] == "BEGIN"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_overbound_excluded_advice_degrades_independently_of_catalogue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    expected: RecallResult | None = None
    fixed: Any = None
    for state in ("hidden", "foreign", "superseded", "unknown"):
        path = tmp_path / state
        old, recent, excluded = seed_state(path, monkeypatch, state)
        if fixed is None:
            assert excluded is not None
            fixed = packet(api, old, recent, excluded, stale=True)
            partition = fixed.partitions[1]
            fixed = replace(
                fixed,
                partitions=(
                    fixed.partitions[0],
                    replace(
                        partition,
                        eligible_count=257,
                        grades=(partition.grades[-1],) * 257,
                    ),
                ),
            )
        result = graded_memory(path, EvidenceSource(fixed)).recall(
            support._agent_actor(),
            Recall(support._SCOPE, QUERY),
            correlation_id=support._CORRELATION_ID,
        )
        assert isinstance(result, RecallResult)
        assert result.semantic_degraded and result.policy.endswith(
            "; semantic-unavailable"
        )
        if expected is None:
            expected = result
        assert result == expected


@pytest.mark.parametrize("state", ["future", "expired"])
def test_world_time_exclusion_precedes_stale_grade_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, excluded = seed_state(tmp_path, monkeypatch, state)
    result = graded_memory(
        tmp_path, EvidenceSource(packet(api, old, recent, excluded, stale=True))
    ).recall(
        support._agent_actor(),
        Recall(support._SCOPE, QUERY),
        correlation_id=support._CORRELATION_ID,
    )
    assert isinstance(result, RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [old, recent]
    assert not result.semantic_degraded


def test_grade_order_and_partition_order_do_not_replace_authority_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = required_module("cairn.projection.semantic_evidence")
    old, recent, _ = seed_state(tmp_path, monkeypatch, "unknown")
    document = packet(api, old, recent)
    reversed_packet = replace(
        document,
        candidate_ids=tuple(reversed(document.candidate_ids)),
        partitions=(
            replace(
                document.partitions[1],
                grades=tuple(reversed(document.partitions[1].grades)),
            ),
            document.partitions[0],
        ),
    )
    responses = [
        graded_memory(tmp_path, EvidenceSource(value)).recall(
            support._agent_actor(),
            Recall(support._SCOPE, QUERY),
            correlation_id=support._CORRELATION_ID,
        )
        for value in (document, reversed_packet)
    ]
    assert isinstance(responses[0], RecallResult)
    assert not responses[0].semantic_degraded
    assert responses[0] == responses[1]
