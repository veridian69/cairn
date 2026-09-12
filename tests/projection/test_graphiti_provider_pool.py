"""Socket-level tests for Graphiti provider-pool ownership and expiry."""

import asyncio

import httpx
import pytest
from graphiti_core.llm_client.config import ModelSize
from openai import DefaultAsyncHttpxClient

from cairn.projection.graphiti import _openai_provider_clients


def test_provider_clients_route_language_work_to_nano_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing Cairn's explicit routing must expose Graphiti's larger default."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    shared, llm, _embedder, reranker = _openai_provider_clients()
    try:
        assert llm._get_model_for_size(ModelSize.medium) == "gpt-5.4-nano"
        assert llm._get_model_for_size(ModelSize.small) == "gpt-4.1-nano"
        assert llm._resolve_reasoning_effort("gpt-5.4-nano", llm.reasoning) == "none"
        assert reranker.config.model == "gpt-4.1-nano"
    finally:
        asyncio.run(shared.close())


async def _accepted_connections(client: httpx.AsyncClient, idle_seconds: float) -> int:
    accepted = 0
    writers: set[asyncio.StreamWriter] = set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal accepted
        accepted += 1
        writers.add(writer)
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: keep-alive\r\n\r\nOK"
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writers.discard(writer)
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with asyncio.timeout(5):
            first = await client.get(f"http://127.0.0.1:{port}/first")
            assert first.status_code == 200
            await asyncio.sleep(idle_seconds)
            second = await client.get(f"http://127.0.0.1:{port}/second")
            assert second.status_code == 200
    finally:
        for writer in tuple(writers):
            writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for writer in tuple(writers)),
            return_exceptions=True,
        )
        server.close()
        await server.wait_closed()
    return accepted


def test_provider_pool_reuses_one_socket_after_an_idle_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring any finite expiry must turn the production count into two."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    async def exercise() -> tuple[int, int]:
        expiring_control = DefaultAsyncHttpxClient(
            limits=httpx.Limits(
                max_connections=1000,
                max_keepalive_connections=100,
                keepalive_expiry=0.05,
            )
        )
        try:
            control_connections = await _accepted_connections(
                expiring_control,
                0.15,
            )
        finally:
            await expiring_control.aclose()

        shared, _llm, _embedder, _reranker = _openai_provider_clients()
        try:
            production_connections = await _accepted_connections(
                shared._client,
                0.15,
            )
        finally:
            await shared.close()
        return control_connections, production_connections

    control_connections, production_connections = asyncio.run(exercise())

    assert control_connections == 2
    assert production_connections == 1
