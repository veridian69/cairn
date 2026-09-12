"""Client diagnostic handling distrusts everything except validated metadata."""

import json
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest

from cairn.catalogue.audit import Classification, Scope
from cairn.client import ConnectionStatus, MemoryClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def document() -> dict[str, object]:
    return {
        "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "principal_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "principal_kind": "workload",
        "product_version": "0.1.0",
        "contract_identity": "cairn.memory/v1",
        "contract_digest": "b" * 64,
        "mcp_contract_digest": "c" * 64,
        "scope": {"realm": "acme", "segments": []},
        "classification": "internal",
        "evaluated_at": "2026-09-09T12:00:00.000000Z",
        "permission_basis": "current_grants_only",
        "permissions": {
            "retrieve": True,
            "ingest": False,
            "promote": False,
            "invalidate": False,
        },
    }


def client(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(
        http, scope=Scope("acme", ()), classification=Classification.INTERNAL
    )


@pytest.mark.anyio
@pytest.mark.parametrize("compressed", [False, True])
async def test_diagnostics_bound_wire_reads_and_close_rejected_stream(
    compressed: bool,
) -> None:
    class Stream(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.reads += 1
            yield b" " * 16385
            self.reads += 1
            yield b"UNTRUSTED SERVER CONTENT"

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"} if compressed else {},
            stream=stream,
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        result = await client(http).diagnose()
    assert result.status is ConnectionStatus.INVALID_RESPONSE
    assert stream.reads == (0 if compressed else 1)
    assert stream.closed
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert "UNTRUSTED" not in repr(result)


@pytest.mark.anyio
async def test_diagnostics_use_memory_surface_and_bind_principal_permissions_to_request() -> (
    None
):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=document())

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        result = await client(http).diagnose(
            expected_contract_digest="b" * 64, expected_mcp_contract_digest="c" * 64
        )
    assert result.status is ConnectionStatus.READY
    assert result.principal_id == UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert result.permissions is not None
    assert result.permissions.retrieve is True
    assert result.permissions.ingest is False
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/memory/v1/diagnose"
    assert json.loads(requests[0].content) == {
        "scope": {"realm": "acme", "segments": []},
        "classification": "internal",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "delta",
    [
        {"scope": {"realm": "other", "segments": []}},
        {"classification": "public"},
        {"principal_id": "untrusted identity"},
        {"principal_kind": "admin"},
        {"product_version": "x" * 10000},
        {"product_version": "SECRET SERVER PROSE"},
        {
            "permissions": {
                "retrieve": 1,
                "ingest": False,
                "promote": False,
                "invalidate": False,
            }
        },
        {"contract_identity": "cairn/v1"},
        {"unexpected": "SECRET SERVER PROSE"},
        {"evaluated_at": "2026-99-09T12:00:00.000000Z"},
    ],
)
async def test_diagnostics_reject_wrong_scope_unbounded_or_malformed_metadata(
    delta: dict[str, object],
) -> None:
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={**document(), **delta})
        ),
    ) as http:
        result = await client(http).diagnose()
    assert result.status is ConnectionStatus.INVALID_RESPONSE
    assert "SECRET SERVER PROSE" not in repr(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        b'{"broken":',
        b"[]",
        b'{"principal_id":"first","principal_id":"second"}',
        b" " * 20000,
        b"\xff",
    ],
)
async def test_diagnostics_malformed_json_is_a_safe_result(content: bytes) -> None:
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
    ) as http:
        result = await client(http).diagnose()
    assert result.status is ConnectionStatus.INVALID_RESPONSE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "authentication_failed", "authentication_failed"),
        (403, "authorisation_denied", "authorisation_denied"),
        (500, "attacker-code", "invalid_response"),
    ],
)
async def test_diagnostic_failures_never_copy_untrusted_server_prose(
    status: int, code: str, expected: str
) -> None:
    payload = {
        "failure": {
            "code": code,
            "message": "SECRET SERVER PROSE",
            "retry": "never",
            "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)),
    ) as http:
        result = await client(http).diagnose()
    assert result.status.value == expected
    assert "SECRET SERVER PROSE" not in repr(result)
    assert "attacker-code" not in repr(result)


@pytest.mark.anyio
async def test_diagnostics_no_grants_is_authenticated_without_ready_claim() -> None:
    payload = {
        **document(),
        "permissions": dict.fromkeys(
            ("retrieve", "ingest", "promote", "invalidate"), False
        ),
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as http:
        result = await client(http).diagnose()
    assert result.status.value == "no_authorised_operations"
    assert result.principal_id == UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert result.failure is None
