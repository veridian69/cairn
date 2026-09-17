"""Private publication admission, before any DB/provider work."""

import asyncio
import importlib
from collections import OrderedDict, UserDict
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.embedder.openai import OpenAIEmbedder

from cairn.catalogue.audit import Classification, TrustClass
from cairn.projection.adapter import ProjectedFactState
from cairn.projection.fact_chunks import EmbeddedLocalFact, UnitSpan
from cairn.projection.fact_vectors import FactRepresentation, FactVectorError
from cairn.projection.semantic_evidence import local_representation_sha256

GROUP = "a" * 64


def parsed_transport(value: Any) -> Any:
    """Exercise the installed parser, including nested properties/map values."""

    def wire(item: Any) -> list[Any]:
        if type(item) is dict:
            pairs = []
            for key, child in item.items():
                pairs.extend([key, wire(child)])
            return [10, pairs]
        if type(item) is list:
            return [6, [wire(child) for child in item]]
        if type(item) is bool:
            return [4, "true" if item else "false"]
        if type(item) is int:
            return [3, str(item)]
        if type(item) is float:
            return [5, str(item)]
        if type(item) is str:
            return [2, item]
        raise AssertionError("unsupported synthetic wire value")

    return importlib.import_module("falkordb.query_result").parse_scalar(
        wire(value), None
    )


def fact(body: str = "a" * 31 + "\n") -> ProjectedFactState:
    return ProjectedFactState(
        UUID("11111111-1111-4111-8111-111111111111"),
        "acme\n[]",
        body,
        "acme",
        (),
        Classification.INTERNAL,
        TrustClass.VALIDATED,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        None,
        None,
    )


def unit() -> list[float]:
    return [1.0] + [0.0] * 1023


def result() -> EmbeddedLocalFact:
    return EmbeddedLocalFact((UnitSpan(0, 32, 0),), (unit(),), unit())


def representation() -> FactRepresentation:
    return FactRepresentation(
        cast(
            OpenAIEmbedder,
            SimpleNamespace(
                config=SimpleNamespace(
                    embedding_model="text-embedding-3-small", embedding_dim=1024
                )
            ),
        )
    )


def test_record_retains_duplicate_occurrences_without_duplicate_vector_children() -> (
    None
):
    from cairn.projection.fact_unit_index import build_record

    state = fact(("a" * 31 + "\n") * 2)
    values = EmbeddedLocalFact(
        (UnitSpan(0, 32, 0), UnitSpan(32, 64, 0)), (unit(),), unit()
    )
    record = build_record(state, GROUP, values)
    assert record["header"]["starts"] == [0, 32]
    assert record["header"]["ends"] == [32, 64]
    assert record["header"]["slots"] == [0, 0]
    assert len(record["children"]) == 1
    assert record["children"][0]["input_bytes"] == 32


@pytest.mark.parametrize(
    "spans",
    [
        (UnitSpan(False, 32, 0),),
        (UnitSpan(cast(Any, 0.0), 32, 0),),
        (UnitSpan(0, True, 0),),
        (UnitSpan(0, 32, False),),
        (UnitSpan(0, 32, cast(Any, 0.0)),),
        (UnitSpan(1, 32, 0),),
        (UnitSpan(0, 31, 0),),
        (UnitSpan(0, 32, 1),),
        (UnitSpan(0, 32, 0), UnitSpan(0, 32, 0)),
    ],
    ids=[
        "bool-start",
        "float-start",
        "bool-end",
        "bool-slot",
        "float-slot",
        "gap",
        "missing-tail",
        "unused-slot",
        "duplicate-span",
    ],
)
def test_manifest_requires_exact_nonbool_integers_and_body_reconstruction(
    spans: Any,
) -> None:
    from cairn.projection.fact_unit_index import build_record

    with pytest.raises(FactVectorError):
        build_record(fact(), GROUP, replace(result(), spans=spans))


@pytest.mark.parametrize(
    "vector",
    [
        [],
        [1.0],
        [0.0] * 1024,
        [float("nan")] * 1024,
        [True] * 1024,
        [2.0] + [0.0] * 1023,
    ],
    ids=["empty", "dimension", "zero", "nan", "bool", "nonunit"],
)
def test_unusable_vector_rejected_before_publication(vector: Any) -> None:
    from cairn.projection.fact_unit_index import build_record

    with pytest.raises(FactVectorError):
        build_record(fact(), GROUP, replace(result(), vectors=(vector,)))
    with pytest.raises(FactVectorError):
        build_record(fact(), GROUP, replace(result(), pooled=vector))


def test_poison_payload_makes_no_publication_query() -> None:
    from cairn.projection.fact_unit_index import publish

    class Driver:
        calls = 0

        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            self.calls += 1
            return [], None, None

    driver = Driver()
    with pytest.raises(FactVectorError):
        asyncio.run(
            publish(
                cast(GraphDriver, driver),
                fact(),
                GROUP,
                replace(result(), vectors=([float("nan")] * 1024,)),
                representation(),
            )
        )
    assert driver.calls == 0


def test_valid_publication_is_one_statement_including_pooled_and_local() -> None:
    from cairn.projection.fact_unit_index import publish

    class Driver:
        calls: list[dict[str, Any]]

        def __init__(self) -> None:
            self.calls = []

        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return [{"uuid": str(fact().fact_id)}], None, None

    driver = Driver()
    asyncio.run(
        publish(cast(GraphDriver, driver), fact(), GROUP, result(), representation())
    )
    assert len(driver.calls) == 1
    assert len(driver.calls[0]["record"]["children"]) == 1
    assert driver.calls[0]["record"]["pooled"]["embedding"] == unit()


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"coverage_valid": False, "eligible_count": 0, "grades": []},
        {"coverage_valid": 1, "eligible_count": 0, "grades": []},
        {"coverage_valid": True, "eligible_count": True, "grades": []},
        {"coverage_valid": True, "eligible_count": 1.0, "grades": []},
        {"coverage_valid": True, "eligible_count": 0, "grades": [{"uuid": "invalid"}]},
    ],
    ids=[
        "missing",
        "incomplete",
        "bool-coverage",
        "bool-count",
        "float-count",
        "bad-grade",
    ],
)
def test_bad_coverage_envelope_never_returns_candidates(row: Any) -> None:
    from cairn.projection.fact_unit_index import FactUnitIndex

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            return [row], None, None

    with pytest.raises(FactVectorError):
        asyncio.run(
            FactUnitIndex(representation()).search(
                cast(GraphDriver, Driver()), GROUP, unit()
            )
        )


def test_zero_hits_return_complete_empty_envelope() -> None:
    from cairn.projection.fact_unit_index import FactUnitIndex

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            return (
                [{"coverage_valid": True, "eligible_count": 0, "grades": []}],
                None,
                None,
            )

    assert asyncio.run(
        FactUnitIndex(representation()).search(
            cast(GraphDriver, Driver()), GROUP, unit()
        )
    ) == (0, ())


def test_preflight_dispatches_only_coverage_after_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.projection import fact_unit_index as module

    events = []

    async def ready(driver: GraphDriver) -> None:
        events.append("ready")

    monkeypatch.setattr(module, "_ready", ready)

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            assert events == ["ready"]
            events.append("query")
            assert query == (
                module._GATHER
                + "WITH coalesce("
                + module._VALID
                + ", false) AS valid\n"
                + "RETURN count(CASE WHEN valid THEN null ELSE 1 END) = 0 AS coverage_valid\n"
            )
            assert kwargs == {
                "group_id": GROUP,
                "representation": local_representation_sha256(),
            }
            tail = query[len(module._GATHER) :]
            for forbidden in ("ORDER BY", "collect(", "cosine", "grades", "$limit"):
                assert forbidden not in tail
            return [{"coverage_valid": True}], None, None

    assert (
        asyncio.run(
            module.FactUnitIndex(representation()).preflight(
                cast(GraphDriver, Driver()), GROUP
            )
        )
        is None
    )
    assert events == ["ready", "query"]


def test_validation_query_shape_uses_reviewed_single_component_pass() -> None:
    from cairn.projection import fact_unit_index as module

    promoted_norm = "reduce(norm=0.0, x IN children[i].embedding | norm+(1.0*x)*x)"
    assert "all(x IN children[i].embedding WHERE" not in module._VALID
    assert promoted_norm in module._VALID
    assert module._VALID.count("x IN children[i].embedding") == 1
    assert module._COVERAGE_ONLY.count(module._VALID) == 1
    assert module._SEARCH.count(module._VALID) == 1


def test_hexadecimal_uses_reviewed_fixed_native_pattern() -> None:
    from cairn.projection import fact_unit_index as module

    expected = (
        "toLower(typeOf(value)) = 'string' AND size(value) = 64 "
        "AND size(string.matchRegEx(value,'^[0-9a-f]{64}$')) = 1"
    )
    assert module._hexadecimal("value") == expected
    assert module._VALID.count("string.matchRegEx(") == 3
    assert "all(j IN range(0,63) WHERE substring(" not in module._VALID


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        (),
        [],
        [{"coverage_valid": True}, {"coverage_valid": True}],
        [None],
        [UserDict({"coverage_valid": True})],
        [OrderedDict(coverage_valid=True)],
        [{}],
        [{"coverage_valid": False}],
        [{"coverage_valid": None}],
        [{"coverage_valid": 1}],
        [{"coverage_valid": "true"}],
        [{"coverage_valid": True, "grades": []}],
        [{"coverage_valid": True, "eligible_count": 0, "grades": []}],
    ],
)
def test_preflight_refuses_nonexact_coverage_envelope(rows: Any) -> None:
    from cairn.projection.fact_unit_index import FactUnitIndex

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            return rows, None, None

    with pytest.raises(FactVectorError, match="fact_vector_rebuild_required"):
        asyncio.run(
            FactUnitIndex(representation()).preflight(
                cast(GraphDriver, Driver()), GROUP
            )
        )


@pytest.mark.parametrize("phase", ["ready", "query"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_preflight_propagates_readiness_query_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, phase: str, failure: type[BaseException]
) -> None:
    from cairn.projection import fact_unit_index as module

    error = failure("controlled failure")

    async def ready(driver: GraphDriver) -> None:
        if phase == "ready":
            raise error

    monkeypatch.setattr(module, "_ready", ready)

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            assert phase == "query", "query ran after failed readiness"
            raise error

    with pytest.raises(failure) as caught:
        asyncio.run(
            module.FactUnitIndex(representation()).preflight(
                cast(GraphDriver, Driver()), GROUP
            )
        )
    assert caught.value is error


@pytest.mark.parametrize("pinned_parser", [False, True], ids=["dict", "pinned-parser"])
def test_ensure_validates_stored_exact_body_before_reusing_without_embedding(
    monkeypatch: pytest.MonkeyPatch,
    pinned_parser: bool,
) -> None:
    from cairn.projection import fact_unit_index as module

    rep = representation()
    record = module.build_record(fact(), GROUP, result())
    record["pooled"].update(
        representation=rep.metadata()["sha256"],
        fingerprint=rep.fingerprint(fact().body),
    )
    if pinned_parser:
        record = parsed_transport(record)
        assert type(record["children"][0]) is OrderedDict

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            if query.startswith("CREATE INDEX"):
                return [], None, None
            return (
                [
                    {
                        "header": record["header"],
                        "children": record["children"],
                        "outgoing": 1,
                        "relationships": parsed_transport([{"total": 1, "units": 1}])
                        if pinned_parser
                        else [{"total": 1, "units": 1}],
                        "pooled": [record["pooled"]],
                    }
                ],
                None,
                None,
            )

    async def forbidden(*args: Any) -> Any:
        raise AssertionError("complete record must not reembed")

    monkeypatch.setattr(module, "embed_local", forbidden, raising=False)
    asyncio.run(
        module.FactUnitIndex(rep).ensure(cast(GraphDriver, Driver()), GROUP, [fact()])
    )


def test_search_accepts_actual_pinned_parser_grade() -> None:
    from cairn.projection.fact_unit_index import FactUnitIndex

    grade = parsed_transport(
        {"uuid": str(fact().fact_id), "fingerprint": "a" * 64, "score": 1.0}
    )
    assert type(grade) is OrderedDict

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            return (
                [{"coverage_valid": True, "eligible_count": 1, "grades": [grade]}],
                None,
                None,
            )

    assert asyncio.run(
        FactUnitIndex(representation()).search(
            cast(GraphDriver, Driver()), GROUP, unit()
        )
    ) == (1, ((str(fact().fact_id), "a" * 64, 1.0),))


@pytest.mark.parametrize(
    ("outgoing", "relationships"),
    [
        (0, [{"total": 0, "units": 0}]),
        (2, [{"total": 1, "units": 1}]),
        (1, [{"total": 2, "units": 2}]),
        (1, [{"total": 1, "units": 0}]),
        (1, [{"total": 0, "units": 0}]),
        (True, [{"total": 1, "units": 1}]),
        (1, [{"total": True, "units": 1}]),
        (1, [{"total": 1, "units": 1.0}]),
        (1, []),
    ],
    ids=[
        "orphan",
        "extra-target",
        "duplicate",
        "wrong-kind",
        "foreign-link",
        "bool-outgoing",
        "bool-total",
        "float-units",
        "missing-counts",
    ],
)
def test_reuse_rejects_invalid_relationship_counts(
    outgoing: Any, relationships: Any
) -> None:
    from cairn.projection import fact_unit_index as module

    rep = representation()
    record = module.build_record(fact(), GROUP, result())
    record["pooled"].update(
        representation=rep.metadata()["sha256"],
        fingerprint=rep.fingerprint(fact().body),
    )
    row = {
        "header": record["header"],
        "children": record["children"],
        "pooled": [record["pooled"]],
        "outgoing": outgoing,
        "relationships": parsed_transport(relationships),
    }
    assert not module.FactUnitIndex(rep)._reusable([row], fact(), GROUP)


class DictSubclass(dict[str, Any]):
    pass


class OrderedSubclass(OrderedDict[str, Any]):
    pass


@pytest.mark.parametrize("container", [DictSubclass, OrderedSubclass, UserDict])
def test_search_rejects_unapproved_mapping_transport(container: Any) -> None:
    from cairn.projection.fact_unit_index import FactUnitIndex

    grade = container(uuid=str(fact().fact_id), fingerprint="a" * 64, score=1.0)

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            return (
                [{"coverage_valid": True, "eligible_count": 1, "grades": [grade]}],
                None,
                None,
            )

    with pytest.raises(FactVectorError):
        asyncio.run(
            FactUnitIndex(representation()).search(
                cast(GraphDriver, Driver()), GROUP, unit()
            )
        )
