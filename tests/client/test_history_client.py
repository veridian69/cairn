"""History uses the host's scope and the same bounded HTTP boundary as recall."""

from uuid import UUID

import httpx
import pytest

from cairn.authority.retrieval import MAX_BUDGET_BYTES
from cairn.catalogue.audit import Classification, Scope
from cairn.client import MemoryClient, RecallFailure


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def client(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(
        http, scope=Scope("acme", ()), classification=Classification.INTERNAL
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fact_id,budget",
    [
        ("not-a-uuid", 100),
        (UUID(int=0), 100),
        (UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), True),
        (UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), 0),
        (UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), MAX_BUDGET_BYTES + 1),
    ],
)
async def test_history_rejects_invalid_uuid_and_budget_before_http(
    fact_id: UUID, budget: int
) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid history input reached HTTP")

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(unexpected)
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await client(http).history(fact_id, budget=budget)
    assert caught.value.operation == "history"


@pytest.mark.anyio
async def test_history_does_not_follow_redirects_or_accept_malformed_json() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(307, headers={"Location": "https://other.invalid"}),
            httpx.Response(200, content=b"{"),
        ]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
    ) as http:
        for _ in range(2):
            with pytest.raises(RecallFailure) as caught:
                await client(http).history(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
            assert caught.value.operation == "history"
    assert [str(r.url) for r in requests] == [
        "https://cairn.invalid/memory/v1/history"
    ] * 2


@pytest.mark.anyio
async def test_history_rejects_changed_destination_before_http() -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Changed destination reached HTTP")

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(unexpected)
    ) as http:
        memory = client(http)
        http.base_url = "https://other.invalid"
        with pytest.raises(RecallFailure) as caught:
            await memory.history(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert caught.value.failure.code == "client_context_changed"
