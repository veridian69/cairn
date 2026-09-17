"""OpenAI 3 / HTTPX2 contract for Graphiti's shared provider transport."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest
from graphiti_core.driver import falkordb_driver
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.prompts.models import Message
from openai import (
    DEFAULT_TIMEOUT,
    APIConnectionError,
    APIStatusError,
)
from openai import DefaultAsyncHttpxClient as OpenAIDefaultAsyncHttpxClient
from pydantic import BaseModel

import cairn.projection.graphiti as graphiti_module


@pytest.fixture(autouse=True)
def _closed_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "OPENAI_ADMIN_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")


def test_all_provider_roles_share_one_httpx2_pool_with_the_accepted_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: dict[str, Any] = {}
    real_constructor = OpenAIDefaultAsyncHttpxClient

    def capture(**kwargs: Any) -> httpx2.AsyncClient:
        constructed.update(kwargs)
        return real_constructor(**kwargs)

    monkeypatch.setattr(graphiti_module, "DefaultAsyncHttpxClient", capture)

    shared, llm_client, embedder, reranker = graphiti_module._openai_provider_clients()
    try:
        limits = constructed["limits"]
        assert isinstance(limits, httpx2.Limits)
        assert limits.max_connections == 1000
        assert limits.max_keepalive_connections == 100
        assert limits.keepalive_expiry is None
        assert isinstance(shared._client, httpx2.AsyncClient)
        assert cast_pool(shared)._max_connections == 1000
        assert cast_pool(shared)._max_keepalive_connections == 100
        assert cast_pool(shared)._keepalive_expiry is None
        assert shared._client._trust_env is True
        assert shared.timeout == DEFAULT_TIMEOUT
        assert shared.max_retries == 2
        assert llm_client.client is shared
        assert embedder.client is shared
        assert reranker.client is shared
        assert llm_client.config.model == "gpt-5.4-nano"
        assert llm_client.config.small_model == "gpt-4.1-nano"
        assert llm_client.reasoning == "none"
        assert reranker.config.model == "gpt-4.1-nano"
    finally:
        asyncio.run(shared.close())

    asyncio.run(shared.close())
    assert shared._client.is_closed


def cast_pool(shared: Any) -> Any:
    return shared._client._transport._pool


class _Answer(BaseModel):
    answer: str


def _response(document: dict[str, Any], status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=document)


def _parsed_response(model: str) -> dict[str, Any]:
    return {
        "id": "resp_synthetic",
        "created_at": int(time.time()),
        "model": model,
        "object": "response",
        "output": [
            {
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"answer":"accepted"}',
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _chat_response(model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl_synthetic",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "True"},
                "logprobs": {
                    "content": [
                        {
                            "token": "True",
                            "bytes": [84, 114, 117, 101],
                            "logprob": -0.1,
                            "top_logprobs": [
                                {
                                    "token": "True",
                                    "bytes": [84, 114, 117, 101],
                                    "logprob": -0.1,
                                },
                                {
                                    "token": "False",
                                    "bytes": [70, 97, 108, 115, 101],
                                    "logprob": -2.3,
                                },
                            ],
                        }
                    ]
                },
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


def _clients_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> tuple[Any, Any, Any, Any]:
    real_constructor = OpenAIDefaultAsyncHttpxClient

    def construct(**kwargs: Any) -> httpx2.AsyncClient:
        return real_constructor(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(graphiti_module, "DefaultAsyncHttpxClient", construct)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://provider.invalid/v1")
    return graphiti_module._openai_provider_clients()


def test_all_three_graphiti_roles_parse_openai3_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    async def serve(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/responses":
            return _response(_parsed_response(body["model"]))
        if request.url.path == "/v1/embeddings":
            return _response(
                {
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [1.0] + [0.0] * 1023,
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            )
        if request.url.path == "/v1/chat/completions":
            return _response(_chat_response(body["model"]))
        return _response({"error": {"message": "unexpected path"}}, 404)

    shared, llm_client, embedder, reranker = _clients_with_transport(monkeypatch, serve)

    async def exercise() -> None:
        try:
            answer = await llm_client.generate_response(
                [Message(role="user", content="return the accepted answer")],
                _Answer,
            )
            embedding = await embedder.create("embed this")
            ranking = await reranker.rank("needle", ["needle passage"])
        finally:
            await shared.close()
        assert answer == {"answer": "accepted"}
        assert embedding == [1.0] + [0.0] * 1023
        assert ranking == [("needle passage", pytest.approx(math.exp(-0.1)))]

    asyncio.run(exercise())

    assert [path for path, _ in requests] == [
        "/v1/responses",
        "/v1/embeddings",
        "/v1/chat/completions",
    ]
    assert requests[0][1]["model"] == "gpt-5.4-nano"
    assert requests[0][1]["reasoning"] == {"effort": "none"}
    assert requests[2][1]["model"] == "gpt-4.1-nano"


@pytest.mark.parametrize(
    "failure",
    [429, 500, httpx2.ConnectTimeout, httpx2.ReadTimeout],
    ids=["rate-limit", "server-error", "connect-timeout", "read-timeout"],
)
def test_provider_retries_twice_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
    failure: int | type[httpx2.TimeoutException],
) -> None:
    attempts = 0

    async def serve(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            if isinstance(failure, int):
                return httpx2.Response(
                    failure,
                    headers={"Retry-After": "0"},
                    json={"error": {"message": "synthetic", "type": "test"}},
                )
            raise failure("synthetic timeout", request=request)
        return _response(
            {
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        )

    shared, _llm_client, embedder, _reranker = _clients_with_transport(
        monkeypatch, serve
    )

    async def exercise() -> Any:
        try:
            return await embedder.create("retry")
        finally:
            await shared.close()

    assert asyncio.run(exercise()) == [1.0]
    assert attempts == 3


@pytest.mark.parametrize("failure", [500, httpx2.ConnectTimeout])
def test_provider_surfaces_the_third_failure_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
    failure: int | type[httpx2.ConnectTimeout],
) -> None:
    attempts = 0

    async def serve(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if isinstance(failure, int):
            return httpx2.Response(
                failure,
                headers={"Retry-After": "0"},
                json={"error": {"message": "synthetic", "type": "test"}},
            )
        raise failure("synthetic timeout", request=request)

    shared, _llm_client, embedder, _reranker = _clients_with_transport(
        monkeypatch, serve
    )

    async def exercise() -> None:
        try:
            expected = (
                APIStatusError if isinstance(failure, int) else APIConnectionError
            )
            with pytest.raises(expected):
                await embedder.create("fail")
        finally:
            await shared.close()

    asyncio.run(exercise())
    assert attempts == 3
    assert shared._client.is_closed


def test_cancelling_an_outstanding_request_releases_the_pool_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        accepted = 0

        async def hold(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal accepted
            accepted += 1
            try:
                await reader.readuntil(b"\r\n\r\n")
                entered.set()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{port}/v1")
        shared, _llm_client, embedder, _reranker = (
            graphiti_module._openai_provider_clients()
        )
        try:
            request = asyncio.create_task(embedder.create("cancel"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            pool = cast_pool(shared)
            for _ in range(20):
                if not pool._connections and not pool._requests:
                    break
                await asyncio.sleep(0)
            assert pool._connections == []
            assert pool._requests == []
            assert accepted == 1

            await asyncio.wait_for(shared.close(), timeout=2)
            await asyncio.wait_for(shared.close(), timeout=2)
            assert shared._client.is_closed
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            if not shared._client.is_closed:
                await shared.close()

    asyncio.run(scenario())


def _proxy(
    records: list[tuple[str, str]],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            records.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            body = json.dumps(
                {
                    "object": "list",
                    "model": request["model"],
                    "data": [{"object": "embedding", "index": 0, "embedding": [1.0]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler contract
            records.append(("CONNECT", self.path))
            self.send_error(502)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_http_provider_traffic_honours_the_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple[str, str]] = []
    server, thread = _proxy(records)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://provider.invalid/v1")
    shared, _llm_client, embedder, _reranker = (
        graphiti_module._openai_provider_clients()
    )
    try:
        assert asyncio.run(embedder.create("proxy")) == [1.0]
    finally:
        asyncio.run(shared.close())
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert records == [("POST", "http://provider.invalid/v1/embeddings")]


def test_https_provider_egress_uses_connect_and_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple[str, str]] = []
    server, thread = _proxy(records)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{server.server_port}")
    shared, _llm_client, embedder, _reranker = (
        graphiti_module._openai_provider_clients()
    )
    try:
        with pytest.raises(APIConnectionError):
            asyncio.run(embedder.create("blocked"))
    finally:
        asyncio.run(shared.close())
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert records == [("CONNECT", "api.openai.com:443")] * 3


def test_graphiti_constructor_failure_closes_the_owned_openai3_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FalkorClient:
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1

    falkor = FalkorClient()
    providers: list[Any] = []
    real_provider_constructor = graphiti_module._openai_provider_clients

    async def build(_self: Any, _delete_existing: bool = False) -> None:
        return None

    def provider_clients() -> tuple[Any, Any, Any, Any]:
        result = real_provider_constructor()
        providers.append(result[0])
        return result

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic construction failure")

    monkeypatch.setattr(falkordb_driver, "FalkorDB", lambda **_kwargs: falkor)
    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", build)
    monkeypatch.setattr(graphiti_module, "_openai_provider_clients", provider_clients)
    monkeypatch.setattr(graphiti_module, "_construct_graphiti", fail)

    with pytest.raises(ValueError, match="synthetic construction failure"):
        graphiti_module.GraphitiIndex(host="offline.invalid", port=1)

    assert len(providers) == 1
    assert providers[0]._client.is_closed
    assert falkor.closes == 1
