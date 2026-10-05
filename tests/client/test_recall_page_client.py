"""Ordered recall pages: client methods, strict validation and the CLI command."""

import copy
import io
import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
from test_arrival_briefing import count_bytes, fact
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import NoRead, cli, invoke, profile

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import MemoryClient, RecallFailure
from cairn.client.validation import validate_recall, validate_recall_page

_SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
_CURSOR = "A" * 43
_CREATED = "2026-10-05T00:00:00.000000Z"
_EXPIRES = "2026-10-05T00:05:00.000000Z"
_EVIDENCE = "66666666-6666-4666-8666-666666666666"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _hits(result: Any) -> list[Mapping[str, object]]:
    return list(cast(tuple[Mapping[str, object], ...], result.data["hits"]))


def _client(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(http, scope=_SCOPE, classification=Classification.INTERNAL)


# --- live ASGI round trips -------------------------------------------------


@pytest.mark.anyio
async def test_client_pages_through_newest_results(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        saved = []
        for i in range(3):
            instance.clock.now += timedelta(seconds=1)
            saved.append(await api.remember(f"deploy note {i}"))
        http.headers["Authorization"] = f"Bearer {token}"
        client = _client(http)
        first = await client.recall_page("deploy", order="newest", limit=2)
        assert first.data["facts_remaining"] is True
        assert first.data["ordering"] == {
            "order": "newest",
            "time_basis": "source",
            "policy": "memory-order/v1",
        }
        assert first.data["snapshot_expires_at"] is not None
        rest = await client.continue_recall(str(first.data["next_cursor"]), limit=2)
        assert rest.data["next_cursor"] is None
        assert rest.data["facts_remaining"] is False
    pages = _hits(first) + _hits(rest)
    assert [hit["fact_id"] for hit in pages] == [
        item["result"]["fact_ids"][0] for item in reversed(saved)
    ]
    assert all(
        hit["source_time_status"] == "unavailable"
        and hit["observed_at"] is None
        and hit["ordering_time_basis"] == "source"
        for hit in pages
    )
    assert first.content_role == "untrusted-data"


@pytest.mark.anyio
async def test_source_time_orders_available_first_and_validates_strictly(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        old = await api.remember(
            "release fact old", observed_at="2020-01-01T00:00:00.000000Z"
        )
        bare = await api.remember("release fact bare")
        new = await api.remember(
            "release fact new", observed_at="2021-01-01T00:00:00.000000Z"
        )
        http.headers["Authorization"] = f"Bearer {token}"
        client = _client(http)
        oldest = await client.recall_page("release", order="oldest")
        recorded = await client.recall_page(
            "release", order="newest", time_basis="recorded"
        )
        relevance = await client.recall_page("release")
    ids = {
        name: item["result"]["fact_ids"][0]
        for name, item in (("old", old), ("bare", bare), ("new", new))
    }
    hits = _hits(oldest)
    assert [hit["fact_id"] for hit in hits] == [ids["old"], ids["new"], ids["bare"]]
    assert [hit["observed_at"] for hit in hits] == [
        "2020-01-01T00:00:00.000000Z",
        "2021-01-01T00:00:00.000000Z",
        None,
    ]
    assert [hit["source_time_status"] for hit in hits] == [
        "available",
        "available",
        "unavailable",
    ]
    # Memory-API assertions cite no Cairn-held evidence record.
    assert all(hit["source_evidence_id"] is None for hit in hits)
    assert oldest.data["next_cursor"] is None
    assert oldest.data["snapshot_expires_at"] is None
    assert oldest.data["selection_complete"] is True
    assert cast(Mapping[str, object], recorded.data["ordering"])["time_basis"] == (
        "recorded"
    )
    assert all(hit["ordering_time_basis"] == "recorded" for hit in _hits(recorded))
    # Relevance omits time_basis on the wire; the server refuses it otherwise.
    assert relevance.data["ordering"] == {
        "order": "relevance",
        "time_basis": None,
        "policy": "memory-order/v1",
    }
    assert all(hit["ordering_time_basis"] is None for hit in _hits(relevance))


@pytest.mark.anyio
async def test_ingested_origin_carries_source_evidence_id(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        ingested = await http.post(
            "/v1/ingest",
            json={
                "scope": {
                    "realm": "acme",
                    "segments": [{"kind": "repository", "identifier": "cairn"}],
                },
                "classification": "internal",
                "source_type": "agent-claim",
                "facts": [{"body": "ingested decision"}],
                "observed_at": "2022-01-01T00:00:00.000000Z",
                "evidence_payload": "conversation excerpt",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "88888888-8888-4888-8888-888888888888",
            },
        )
        assert ingested.status_code == 200, ingested.text
        http.headers["Authorization"] = f"Bearer {token}"
        page = await _client(http).recall_page("ingested", order="newest")
    result = ingested.json()["result"]
    (hit,) = _hits(page)
    assert hit["fact_id"] == result["fact_ids"][0]
    assert hit["source_evidence_id"] == result["evidence_id"] is not None
    assert hit["observed_at"] == "2022-01-01T00:00:00.000000Z"


@pytest.mark.anyio
async def test_page_budget_failure_carries_minimum(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        await memory_support.Api(http, token).remember("budget " + "y" * 2000)
        http.headers["Authorization"] = f"Bearer {token}"
        client = _client(http)
        with pytest.raises(RecallFailure) as raised:
            await client.recall_page("budget", budget=10)
    failure = raised.value.failure
    assert raised.value.operation == "recall-page"
    assert (failure.code, failure.retry, failure.status_code) == (
        "invalid_request",
        "never",
        400,
    )
    assert failure.correlation_id is not None
    detail = dict(failure.detail or ())
    assert detail["reason"] == "page_budget_too_small"
    assert int(detail["minimum_budget"]) > 10
    assert failure.detail == tuple(sorted(detail.items()))


@pytest.mark.anyio
async def test_unknown_cursor_is_continuation_unavailable(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        with pytest.raises(RecallFailure) as raised:
            await _client(http).continue_recall(_CURSOR)
    assert raised.value.operation == "recall-page"
    assert raised.value.failure.code == "invalid_request"
    assert raised.value.failure.detail == (("reason", "continuation_unavailable"),)


# --- wire requests and failure parsing --------------------------------------


def _page(*hits: dict[str, Any], **changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "hits": list(hits),
        "disagreements": [],
        "resolutions": [],
        "budget_consumed": 0,
        "budget_exhausted": False,
        "policy": "lexical-age/v1",
        "semantic_degraded": True,
        "ordering": {
            "order": "relevance",
            "time_basis": None,
            "policy": "memory-order/v1",
        },
        "snapshot_created_at": _CREATED,
        "snapshot_expires_at": None,
        "next_cursor": None,
        "facts_remaining": False,
        "context_incomplete": False,
        "selection_complete": True,
        **changes,
    }
    document["budget_consumed"] = count_bytes(document)
    return document


def _paged_fact(**changes: Any) -> dict[str, Any]:
    paged: dict[str, Any] = {
        "observed_at": None,
        "source_time_status": "unavailable",
        "ordering_time_basis": None,
        "source_evidence_id": None,
    }
    return fact(**{**paged, **changes})


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.bodies: list[dict[str, object]] = []
        self.paths: list[str] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.bodies.append(json.loads(request.content))
        if self.response is not None:
            return self.response
        ordering: dict[str, object] = {
            "order": "relevance",
            "time_basis": None,
            "policy": "memory-order/v1",
        }
        if "order" in self.bodies[-1] and self.bodies[-1]["order"] != "relevance":
            ordering = {
                "order": self.bodies[-1]["order"],
                "time_basis": self.bodies[-1].get("time_basis", "source"),
                "policy": "memory-order/v1",
            }
        return httpx.Response(200, json=_page(ordering=ordering))


@pytest.mark.anyio
async def test_request_bodies_omit_unset_fields_and_continuation_is_exact() -> None:
    recorder = Recorder()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(recorder)
    ) as http:
        client = _client(http)
        await client.recall_page("deploy")
        await client.recall_page("deploy", order="newest")
        await client.recall_page("deploy", order="oldest", time_basis="recorded")
        await client.continue_recall(_CURSOR, budget=99, limit=3)
    scope = {
        "realm": "acme",
        "segments": [{"kind": "repository", "identifier": "cairn"}],
    }
    base = {"scope": scope, "query": "deploy", "relevant_only": True}
    assert recorder.paths == ["/memory/v1/recall-page"] * 4
    assert recorder.bodies == [
        {**base, "order": "relevance", "budget": 16384, "limit": 20},
        {**base, "order": "newest", "budget": 16384, "limit": 20},
        {
            **base,
            "order": "oldest",
            "time_basis": "recorded",
            "budget": 16384,
            "limit": 20,
        },
        {"scope": scope, "cursor": _CURSOR, "budget": 99, "limit": 3},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call,code",
    [
        ({"query": "x", "time_basis": "source"}, "invalid_order"),
        (
            {"query": "x", "order": "relevance", "time_basis": "recorded"},
            "invalid_order",
        ),
        ({"query": "x", "order": "newest", "time_basis": "custody"}, "invalid_order"),
        ({"query": "x", "order": "random"}, "invalid_order"),
        ({"query": "x", "order": ["newest"]}, "invalid_order"),
        ({"query": "x", "relevant_only": 1}, "invalid_relevance_filter"),
        ({"query": ""}, "invalid_query"),
        ({"query": "\ud800"}, "invalid_query"),
        ({"query": "é" * 4097}, "invalid_query"),
        ({"query": "x", "budget": 0}, "invalid_budget"),
        ({"query": "x", "budget": 1048577}, "invalid_budget"),
        ({"query": "x", "limit": 0}, "invalid_limit"),
        ({"query": "x", "limit": 101}, "invalid_limit"),
        ({"query": "x", "limit": True}, "invalid_limit"),
        ({"cursor": "short"}, "invalid_cursor"),
        ({"cursor": "A" * 42 + "="}, "invalid_cursor"),
        ({"cursor": _CURSOR, "limit": 0}, "invalid_limit"),
        ({"cursor": _CURSOR, "budget": 0}, "invalid_budget"),
    ],
)
async def test_local_refusals_precede_io(call: dict[str, Any], code: str) -> None:
    recorder = Recorder()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(recorder)
    ) as http:
        client = _client(http)
        with pytest.raises(RecallFailure) as raised:
            if "cursor" in call:
                arguments = dict(call)
                await client.continue_recall(arguments.pop("cursor"), **arguments)
            else:
                arguments = dict(call)
                await client.recall_page(arguments.pop("query"), **arguments)
    assert raised.value.operation == "recall-page"
    assert raised.value.failure.code == code
    assert recorder.paths == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        # Legacy envelope: other recall-page refusals keep FailureEnvelope.
        {
            "failure": {
                "code": "invalid_request",
                "message": "PRIVATE prose",
                "retry": "never",
                "correlation_id": "77777777-7777-4777-8777-777777777777",
            }
        },
        # Malformed operation-local detail is not trusted.
        {
            "failure": {
                "code": "invalid_request",
                "message": "PRIVATE prose",
                "retry": "never",
                "correlation_id": "77777777-7777-4777-8777-777777777777",
                "detail": {"reason": "page_budget_too_small", "minimum_budget": 0},
            }
        },
        {
            "failure": {
                "code": "invalid_request",
                "message": "PRIVATE prose",
                "retry": "never",
                "correlation_id": "77777777-7777-4777-8777-777777777777",
                "detail": {"reason": "continuation_unavailable", "extra": 1},
            }
        },
    ],
)
async def test_failures_without_valid_page_detail_keep_safe_metadata(
    body: dict[str, object],
) -> None:
    recorder = Recorder(httpx.Response(400, json=body))
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(recorder)
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).recall_page("x")
    failure = raised.value.failure
    assert raised.value.operation == "recall-page"
    assert failure.code == "invalid_request" and failure.detail is None
    assert failure.correlation_id == "77777777-7777-4777-8777-777777777777"
    assert "PRIVATE" not in failure.message


@pytest.mark.anyio
async def test_page_detail_needs_closed_failure_vocabulary() -> None:
    body = {
        "failure": {
            "code": "invented_code",
            "message": "m",
            "retry": "never",
            "correlation_id": "77777777-7777-4777-8777-777777777777",
            "detail": {"reason": "continuation_unavailable"},
        }
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body)),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).continue_recall(_CURSOR)
    assert raised.value.failure.code == "http_error"
    assert raised.value.failure.detail is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code,retry",
    [
        (403, "invalid_request", "never"),
        (400, "authorisation_denied", "never"),
        (400, "invalid_request", "after-delay"),
    ],
)
async def test_page_detail_only_on_the_documented_refusal(
    status: int, code: str, retry: str
) -> None:
    body = {
        "failure": {
            "code": code,
            "message": "m",
            "retry": retry,
            "correlation_id": "77777777-7777-4777-8777-777777777777",
            "detail": {"reason": "page_budget_too_small", "minimum_budget": 9},
        }
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).recall_page("x")
    failure = raised.value.failure
    assert (failure.code, failure.retry, failure.status_code) == (code, retry, status)
    assert failure.detail is None


@pytest.mark.anyio
async def test_response_ordering_must_match_the_initial_request() -> None:
    wrong = httpx.Response(200, json=_page())
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(Recorder(wrong)),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).recall_page("x", order="newest")
    assert raised.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_legacy_recall_failure_still_names_recall() -> None:
    body = {
        "failure": {
            "code": "invalid_request",
            "message": "m",
            "retry": "never",
            "correlation_id": "77777777-7777-4777-8777-777777777777",
        }
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body)),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).recall("x")
    assert raised.value.operation == "recall" and raised.value.failure.detail is None


# --- validator --------------------------------------------------------------


def test_validator_rejects_extra_keys_and_wrong_budget() -> None:
    packet: dict[str, object] = {
        "hits": [],
        "disagreements": [],
        "resolutions": [],
        "budget_consumed": 0,
        "budget_exhausted": False,
        "policy": "p",
        "semantic_degraded": False,
        "ordering": {
            "order": "relevance",
            "time_basis": None,
            "policy": "memory-order/v1",
        },
        "snapshot_created_at": _CREATED,
        "snapshot_expires_at": None,
        "next_cursor": None,
        "facts_remaining": False,
        "context_incomplete": False,
        "selection_complete": True,
    }
    assert validate_recall_page(packet, budget=1, limit=1) == packet
    with pytest.raises(ValueError):
        validate_recall_page({**packet, "extra": 1}, budget=1, limit=1)
    with pytest.raises(ValueError):
        validate_recall_page({**packet, "budget_consumed": 2}, budget=1, limit=1)
    with pytest.raises(ValueError):
        validate_recall_page({**packet, "next_cursor": "short"}, budget=1, limit=1)


def test_validator_accepts_canonical_paged_hits() -> None:
    hit = _paged_fact(
        observed_at="2026-01-01T00:00:00.000000Z",
        source_time_status="available",
        ordering_time_basis="source",
        source_evidence_id=_EVIDENCE,
    )
    packet = _page(
        hit,
        ordering={
            "order": "newest",
            "time_basis": "source",
            "policy": "memory-order/v1",
        },
        next_cursor=_CURSOR,
        snapshot_expires_at=_EXPIRES,
        facts_remaining=True,
    )
    budget = int(packet["budget_consumed"])
    assert validate_recall_page(packet, budget=budget, limit=1) == packet
    # A legacy validator sees the four paged fields as unknown.
    with pytest.raises(ValueError):
        validate_recall({k: packet[k] for k in packet if k in _LEGACY}, budget=budget)


_LEGACY = frozenset(
    {
        "hits",
        "disagreements",
        "resolutions",
        "budget_consumed",
        "budget_exhausted",
        "policy",
        "semantic_degraded",
    }
)
_NEWEST = {"order": "newest", "time_basis": "source", "policy": "memory-order/v1"}


@pytest.mark.parametrize(
    "hit_changes,page_changes",
    [
        ({"source_time_status": "available"}, {}),
        ({"observed_at": "2026-01-01T00:00:00.000000Z"}, {}),
        (
            {"observed_at": "2026-01-01T00:00:00Z", "source_time_status": "available"},
            {},
        ),
        ({"source_time_status": "withheld"}, {}),
        ({"ordering_time_basis": "source"}, {}),
        ({}, {"ordering": _NEWEST}),
        ({"source_evidence_id": "not-a-uuid"}, {}),
        ({"source_evidence_id": "66666666-6666-1666-8666-666666666666"}, {}),
        ({"source_evidence_id": "66666666-6666-4666-8666-66666666666A"}, {}),
        ({"source_evidence_id": "{66666666-6666-4666-8666-666666666666}"}, {}),
        ({"extra": None}, {}),
        ({}, {"ordering": {**_NEWEST, "time_basis": None}}),
        (
            {},
            {
                "ordering": {
                    "order": "relevance",
                    "time_basis": "source",
                    "policy": "memory-order/v1",
                }
            },
        ),
        (
            {},
            {
                "ordering": {
                    "order": "relevance",
                    "time_basis": None,
                    "policy": "memory-order/v2",
                }
            },
        ),
        ({}, {"ordering": {"order": "relevance", "time_basis": None}}),
        (
            {},
            {
                "ordering": {
                    "order": ["relevance"],
                    "time_basis": None,
                    "policy": "memory-order/v1",
                }
            },
        ),
        (
            {},
            {
                "ordering": {
                    "order": "relevance",
                    "time_basis": [],
                    "policy": "memory-order/v1",
                }
            },
        ),
        ({}, {"next_cursor": _CURSOR, "snapshot_expires_at": _EXPIRES}),
        ({}, {"next_cursor": _CURSOR, "facts_remaining": True}),
        (
            {},
            {
                "next_cursor": "A" * 42 + "=",
                "facts_remaining": True,
                "snapshot_expires_at": _EXPIRES,
            },
        ),
        ({}, {"facts_remaining": 1}),
        ({}, {"context_incomplete": None}),
        ({}, {"selection_complete": "true"}),
        ({}, {"snapshot_created_at": None}),
        ({}, {"snapshot_expires_at": "2026-10-05"}),
    ],
)
def test_validator_rejects_inconsistent_pages(
    hit_changes: dict[str, Any], page_changes: dict[str, Any]
) -> None:
    hit = _paged_fact(**hit_changes)
    packet = _page(hit, **page_changes)
    with pytest.raises(ValueError):
        validate_recall_page(packet, budget=1048576, limit=20)


def test_validator_enforces_limit_and_paged_record_accounting() -> None:
    first = _paged_fact()
    second = _paged_fact(fact_id="77777777-7777-4777-8777-777777777777")
    packet = _page(first, second)
    validate_recall_page(packet, budget=1048576, limit=2)
    with pytest.raises(ValueError):
        validate_recall_page(packet, budget=1048576, limit=1)
    # Accounting covers the whole paged record, not only legacy fields.
    legacy_only = copy.deepcopy(packet)
    legacy_only["budget_consumed"] = count_bytes(
        {"hits": [{k: v for k, v in first.items() if k in fact()}] * 2}
    )
    with pytest.raises(ValueError):
        validate_recall_page(legacy_only, budget=1048576, limit=2)
    # A legacy (unpaged) hit is refused in a page.
    with pytest.raises(ValueError):
        validate_recall_page(_page(fact()), budget=1048576, limit=20)


# --- CLI --------------------------------------------------------------------


class Capture(httpx.AsyncBaseTransport):
    def __init__(self, upstream: httpx.AsyncBaseTransport) -> None:
        self.upstream = upstream
        self.pages: list[dict[str, object]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/memory/v1/recall-page":
            self.pages.append(json.loads(request.content))
        return await self.upstream.handle_async_request(request)


@pytest.mark.anyio
async def test_cli_recall_page_round_trip_and_budget_refusal(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        for i in range(3):
            instance.clock.now += timedelta(seconds=1)
            await api.remember(f"deploy note {i} " + "z" * 600)
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        transport = Capture(http._transport)
        code, first = await invoke(
            path,
            "recall-page",
            {"query": "deploy", "order": "newest", "limit": 2},
            transport,
        )
        assert code == 0, first
        assert first["command"] == "recall-page"
        data = first["result"]["data"]
        assert data["facts_remaining"] is True and len(data["hits"]) == 2
        code, rest = await invoke(
            path, "recall-page", {"cursor": data["next_cursor"]}, transport
        )
        assert code == 0, rest
        assert rest["result"]["data"]["next_cursor"] is None
        assert len(rest["result"]["data"]["hits"]) == 1
        code, default = await invoke(
            path, "recall-page", {"query": "deploy"}, transport
        )
        assert code == 0, default
        code, refused = await invoke(
            path, "recall-page", {"query": "deploy", "budget": 10}, transport
        )
    scope = {
        "realm": "acme",
        "segments": [{"kind": "repository", "identifier": "cairn"}],
    }
    # Command defaults are this command's own: limit 20, relevant_only true.
    assert transport.pages[:3] == [
        {
            "scope": scope,
            "query": "deploy",
            "order": "newest",
            "relevant_only": True,
            "budget": 16384,
            "limit": 2,
        },
        {"scope": scope, "cursor": data["next_cursor"], "budget": 16384, "limit": 20},
        {
            "scope": scope,
            "query": "deploy",
            "order": "relevance",
            "relevant_only": True,
            "budget": 16384,
            "limit": 20,
        },
    ]
    assert code == 2
    error = refused["result"]["error"]
    assert error["code"] == "invalid_request" and error["operation"] == "recall-page"
    assert error["detail"]["reason"] == "page_budget_too_small"
    assert error["detail"]["minimum_budget"] > 600
    assert set(error) == {"code", "operation", "detail"}


@pytest.mark.anyio
async def test_cli_continuation_unavailable_exits_two(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        code, refused = await invoke(
            path, "recall-page", {"cursor": _CURSOR}, http._transport
        )
    assert code == 2
    assert refused["result"]["error"] == {
        "code": "invalid_request",
        "operation": "recall-page",
        "detail": {"reason": "continuation_unavailable"},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": "x", "cursor": _CURSOR},
        {"cursor": _CURSOR, "order": "newest"},
        {"cursor": _CURSOR, "relevant_only": True},
        {"cursor": _CURSOR, "time_basis": "source"},
        {"cursor": "short"},
        {"cursor": None},
        {"query": None},
        {"query": ""},
        {"query": "é" * 4097},
        {"query": "x", "time_basis": "source"},
        {"query": "x", "order": "relevance", "time_basis": "recorded"},
        {"query": "x", "order": "newest", "time_basis": None},
        {"query": "x", "order": "newest", "time_basis": "custody"},
        {"query": "x", "order": "random"},
        {"query": "x", "order": None},
        {"query": "x", "relevant_only": 1},
        {"query": "x", "relevant_only": None},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 101},
        {"query": "x", "budget": 0},
        {"query": "x", "budget": 1048577},
        {"query": "x", "trust_filters": []},
        {"query": "x", "scope": {}},
    ],
)
async def test_cli_recall_page_strict_inputs_before_any_network(
    body: dict[str, object],
) -> None:
    code, result = await invoke(Path("/nonexistent-profile"), "recall-page", body)
    assert code == 2
    assert result["result"]["error"]["code"] == "invalid_input"


@pytest.mark.anyio
async def test_cli_recall_page_help_is_standalone() -> None:
    out = io.BytesIO()
    code = await cli().execute(
        ["recall-page", "--help"], stdin=NoRead(), stdout=out, stderr=io.BytesIO()
    )
    text = out.getvalue()
    assert code == 0 and b"usage:" in text
    assert all(
        field in text
        for field in (b"order", b"time_basis", b"relevant_only", b"cursor", b"limit")
    )
