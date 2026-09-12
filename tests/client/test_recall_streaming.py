"""Recall must bound untrusted wire data before buffering and decoding."""

import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import cast

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_client import (
    _client,
    _failure_body,
    _recall_body,
    _set_budget_consumed,
)

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import MemoryClient, RecallFailure


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["policy", "body"])
async def test_duplicate_keys_in_otherwise_valid_packet_are_rejected(
    field: str,
) -> None:
    body = json.dumps(_recall_body()).encode()
    marker = ('"' + field + '":').encode()
    body = body.replace(marker, marker + b'"PRIVATE SHADOW", ' + marker, 1)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Chunks(body))
        ),
    ) as http:
        with pytest.raises(RecallFailure):
            await _client(http).recall("query")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "br"},
        {"Content-Length": "-1"},
        {"Content-Length": "+1"},
        {"Content-Length": "1, 1"},
        {"Content-Length": " 1"},
        {"Content-Length": "1.0"},
        {"Content-Length": "99999999999999999999"},
        {"Content-Length": "4104"},
    ],
)
async def test_invalid_headers_refused_before_iteration(
    headers: dict[str, str],
) -> None:
    stream = Chunks(b"PRIVATE BODY")
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, stream=stream)
        ),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("PRIVATE QUERY", budget=1)
    assert stream.read == 0
    assert stream.closed
    assert caught.value.operation == "recall"
    assert caught.value.failure.code == "invalid_response"
    assert "PRIVATE" not in repr(caught.value.failure)


@pytest.mark.anyio
@pytest.mark.parametrize("status,cap", [(200, 4103), (403, 16384)])
async def test_cumulative_overflow_stops_before_next_chunk(
    status: int, cap: int
) -> None:
    stream = Chunks(b" " * cap, b"!", b"PRIVATE UNREAD")
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=stream)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query", budget=1)
    assert stream.read == 2
    assert stream.closed
    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize("status,cap", [(200, 4103), (403, 16384)])
async def test_exact_wire_limit_is_inclusive(status: int, cap: int) -> None:
    document = _recall_body() if status == 200 else _failure_body()
    if status == 200:
        document["hits"] = []
        document["budget_consumed"] = 0
    body = json.dumps(document).encode()
    stream = Chunks(body, b" " * (cap - len(body)))
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=stream)),
    ) as http:
        if status == 200:
            result = await _client(http).recall("query", budget=1)
            assert result.data["hits"] == ()
        else:
            with pytest.raises(RecallFailure) as caught:
                await _client(http).recall("query", budget=1)
            assert caught.value.failure.code == "authorisation_denied"
    assert stream.closed


@pytest.mark.anyio
async def test_partial_transport_failure_closes_without_echo() -> None:
    class Interrupted(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"PRIVATE":'
            raise httpx.ReadError("PRIVATE TRANSPORT")

    stream = Interrupted()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")
    assert stream.closed
    assert "PRIVATE" not in repr(caught.value.failure)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        b'{"hits":[],"hits":[]}',
        b'{"policy":NaN}',
        b'{"policy":Infinity}',
        b'{"policy":-Infinity}',
        b'{"policy":1e9999}',
        b"[" * 1500 + b"]" * 1500,
        b"\xff",
        b"{} trailing",
        b'{"policy":"\\ud800"}',
    ],
)
async def test_malformed_decoder_is_safe(body: bytes) -> None:
    stream = Chunks(body)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("PRIVATE QUERY")
    assert caught.value.failure.code == "invalid_response"
    assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("ascii_only", [False, True])
async def test_exact_unicode_budget_and_request_contract(ascii_only: bool) -> None:
    document = _recall_body()
    hit = cast(list[dict[str, object]], document["hits"])[0]
    hit["body"] = 'é雪😀\\"\n' * 100
    _set_budget_consumed(document)
    budget = cast(int, document["budget_consumed"])
    body = json.dumps(document, ensure_ascii=ascii_only).encode()
    stream = Chunks(*(body[i : i + 7] for i in range(0, len(body), 7)))
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Length": str(len(body))}, stream=stream
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(serve)
    ) as http:
        result = await _client(http).recall(
            "PRIVATE QUERY", budget=budget, relevant_only=True
        )
    assert result.content_role == "untrusted-data"
    hits = cast(tuple[Mapping[str, object], ...], result.data["hits"])
    assert hits[0]["body"] == hit["body"]
    assert result.data["budget_consumed"] == budget
    assert stream.closed
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert json.loads(requests[0].content)["relevant_only"] is True
    assert "PRIVATE QUERY" not in str(requests[0].url)
    assert "PRIVATE QUERY" not in str(requests[0].headers)


@pytest.mark.anyio
@pytest.mark.parametrize("length_delta", [-1, 1])
async def test_content_length_must_match_actual_body(length_delta: int) -> None:
    body = json.dumps(_recall_body()).encode()
    stream = Chunks(body)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"Content-Length": str(len(body) + length_delta)},
                stream=stream,
            )
        ),
    ) as http:
        with pytest.raises(RecallFailure):
            await _client(http).recall("query")
    assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 307])
async def test_failure_never_echoes_body_or_follows_redirect(status: int) -> None:
    document = _failure_body()
    cast(dict[str, object], document["failure"])["message"] = "PRIVATE BODY"
    body = json.dumps(document).encode()
    stream = Chunks(body)
    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, headers={"Location": "https://other.invalid/PRIVATE"}, stream=stream
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(serve),
        follow_redirects=True,
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("PRIVATE QUERY")
    assert calls == 1
    assert stream.closed
    assert caught.value.failure.status_code == status
    assert "PRIVATE" not in repr(caught.value.failure)
    assert caught.value.failure.code == "authorisation_denied"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"PRIVATE plain text",
        b"\xff",
        b"null",
        b"{}",
        b'{"failure":{"message":"PRIVATE prose"}}',
        b'{"failure":{},"failure":{}}',
        b'{"failure":{"code":NaN}}',
        b'{"failure":{"code":1e9999}}',
    ],
)
async def test_non200_malformed_body_preserves_http_error_fallback(body: bytes) -> None:
    stream = Chunks(body)
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(503, stream=stream)),
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("PRIVATE query")
    assert caught.value.operation == "recall"
    assert caught.value.failure.code == "http_error"
    assert caught.value.failure.status_code == 503
    assert caught.value.failure.retry == "never"
    assert caught.value.failure.correlation_id is None
    assert "PRIVATE" not in repr(caught.value.failure)
    assert stream.closed


@pytest.mark.anyio
async def test_actual_asgi_recall_roundtrip(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        saved = await api.remember("Synthetic Unicode measurement 雪")
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
        )
        result = await client.recall("measurement", relevant_only=True)
        exact = await client.recall(
            "measurement", budget=cast(int, result.data["budget_consumed"])
        )
    hits = cast(tuple[Mapping[str, object], ...], exact.data["hits"])
    assert hits[0]["fact_id"] == saved["result"]["fact_ids"][0]
    assert hits[0]["body"] == "Synthetic Unicode measurement 雪"
    assert exact.content_role == "untrusted-data"
