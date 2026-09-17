"""History must enforce host scope and bound hostile transport responses."""

import gzip
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import UUID

import httpx
import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import MemoryClient, RecallFailure

FACT = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
AUTHOR = "33333333-3333-4333-8333-333333333333"
LINK = "44444444-4444-4444-8444-444444444444"
SCOPE = {"realm": "acme", "segments": [{"kind": "project", "identifier": "cairn"}]}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def memory(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(
        http,
        scope=Scope("acme", (ScopeSegment("project", "cairn"),)),
        classification=Classification.PUBLIC,
    )


def packet() -> dict[str, Any]:
    def fact(identity: str) -> dict[str, Any]:
        return {
            "fact_id": identity,
            "body": "A restricted observation.",
            "scope": SCOPE,
            "classification": "restricted",
            "trust": "candidate",
            "assertion_id": AUTHOR,
            "derived_from": None,
            "promoted_by": None,
            "evidence_id": None,
            "valid_from": None,
            "valid_to": None,
            "recorded_at": "2026-09-09T12:00:00.000000Z",
            "invalidated_at": None,
            "source_principal_id": AUTHOR,
            "source_type": "agent-claim",
            "relevance_score": 1.0,
            "has_disagreement": True,
            "disagreement_context_incomplete": False,
        }

    return {
        "facts": [fact(FACT), fact(SECOND)],
        "corrections": [],
        "disagreements": [
            {
                "relationship_id": LINK,
                "left_fact_id": FACT,
                "right_fact_id": SECOND,
                "principal_id": AUTHOR,
                "scope": SCOPE,
                "classification": "restricted",
                "reason": "Independent measurements.",
                "recorded_at": "2026-09-09T12:00:00.000000Z",
            }
        ],
        "resolutions": [
            {
                "relationship_id": "55555555-5555-4555-8555-555555555555",
                "disagreement_id": LINK,
                "selected_fact_id": SECOND,
                "evidence_id": AUTHOR,
                "principal_id": AUTHOR,
                "scope": SCOPE,
                "classification": "restricted",
                "reason": "Verified measurement.",
                "recorded_at": "2026-09-09T12:00:00.000000Z",
            }
        ],
        "budget_consumed": 0,
        "budget_exhausted": False,
    }


def account(document: dict[str, Any]) -> int:
    size = sum(
        len(
            json.dumps(
                record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        for field in ("facts", "corrections", "disagreements", "resolutions")
        for record in document[field]
    )
    document["budget_consumed"] = size
    return size


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scope",
    [
        {"realm": "other", "segments": []},
        {"realm": "acme", "segments": [{"kind": "project", "identifier": "sibling"}]},
        {
            "realm": "acme",
            "segments": [*SCOPE["segments"], {"kind": "run", "identifier": "child"}],
        },
    ],
)
@pytest.mark.parametrize("field", ["all", "facts", "disagreements", "resolutions"])
async def test_history_rejects_outside_host_scope(
    scope: dict[str, Any], field: str
) -> None:
    document = packet()
    if field == "facts":
        # The first fact is admissible: every record, not just the first, matters.
        document["disagreements"] = []
        document["resolutions"] = []
        for record in document["facts"]:
            record["has_disagreement"] = False
        document["facts"][1]["scope"] = scope
    else:
        for collection in (
            ("facts", "disagreements", "resolutions") if field == "all" else (field,)
        ):
            for record in document[collection]:
                record["scope"] = scope
    account(document)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await memory(http).history(UUID(FACT))
    assert caught.value.operation == "history"
    assert caught.value.failure.code == "invalid_response"
    assert "restricted observation" not in repr(caught.value.failure)


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [SCOPE, {"realm": "acme", "segments": []}])
async def test_history_accepts_exact_and_ancestor_records_above_write_classification(
    scope: dict[str, Any],
) -> None:
    document = packet()
    for field in ("facts", "disagreements", "resolutions"):
        for record in document[field]:
            record["scope"] = scope
    budget = account(document)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document)),
    ) as http:
        result = await memory(http).history(UUID(FACT), budget=budget)
    facts = result.data["facts"]
    assert isinstance(facts, tuple) and len(facts) == 2
    assert isinstance(facts[0], Mapping)
    assert facts[0]["classification"] == "restricted"


@pytest.mark.anyio
async def test_history_deep_json_is_safe_recall_failure() -> None:
    raw = b'{"facts":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}"
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await memory(http).history(UUID(FACT))
    assert caught.value.operation == "history"
    assert caught.value.failure.code == "invalid_response"


class OversizedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.delivered = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(2048):
            self.delivered += 1024
            yield b"x" * 1024

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_history_bounds_stream_before_buffering_entire_hostile_body() -> None:
    stream = OversizedStream()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await memory(http).history(UUID(FACT), budget=1)
    assert caught.value.failure.code == "invalid_response"
    assert stream.delivered < 16384
    assert stream.closed


@pytest.mark.anyio
async def test_history_wire_expansion_does_not_truncate_valid_records() -> None:
    document = packet()
    document["facts"][0]["body"] = "é🪨" * 1000
    budget = account(document)
    raw = json.dumps(document, ensure_ascii=True, indent=2).encode()
    assert len(raw) > budget
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
    ) as http:
        result = await memory(http).history(UUID(FACT), budget=budget)
    facts = result.data["facts"]
    assert isinstance(facts, tuple) and isinstance(facts[0], Mapping)
    assert facts[0]["body"] == "é🪨" * 1000


@pytest.mark.anyio
async def test_history_refuses_compression_before_decoder_or_stream_consumption() -> (
    None
):
    compressed = gzip.compress(b"x" * (2 * 1024 * 1024))
    consumed: list[bool] = []
    closed: list[bool] = []
    requests: list[httpx.Request] = []

    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            consumed.append(True)
            yield compressed

        async def aclose(self) -> None:
            closed.append(True)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=CompressedStream()
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await memory(http).history(UUID(FACT), budget=1)
    assert caught.value.failure.code == "invalid_response"
    assert not consumed
    assert closed
    assert requests[0].headers["Accept-Encoding"] == "identity"
