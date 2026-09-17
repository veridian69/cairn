"""Corrections carry fixed host scope and require a complete actual receipt."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_client import _client, _remember_body

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import MemoryClient
from cairn.client.errors import MemoryOperationFailure
from cairn.client.types import FrozenJSONObject, PersistenceStatus

FACT = UUID("77777777-7777-4777-8777-777777777777")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def response(outcome: str = "committed") -> dict[str, object]:
    body = _remember_body(outcome)
    body["result"] = {
        "fact_ids": [str(FACT)],
        "invalidated_at": "2026-09-09T12:00:00.000000Z",
    }
    return body


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["committed", "replayed"])
async def test_correct_uses_host_scope_and_detaches_validated_receipt(
    outcome: str,
) -> None:
    requests: list[httpx.Request] = []
    document = response(outcome)

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=document)

    key, replacement = uuid4(), uuid4()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(serve)
    ) as http:
        receipt = await _client(http).correct(
            (FACT,),
            reason="Measured again",
            superseded_by=replacement,
            idempotency_key=key,
        )
    assert receipt.status is PersistenceStatus(outcome)
    assert receipt.idempotency_key == key
    assert receipt.result is not None and receipt.result["fact_ids"] == (str(FACT),)
    assert receipt.mutation_receipt is not None and receipt.audit_receipt is not None
    assert requests[0].url.path == "/memory/v1/correct"
    assert requests[0].headers["Idempotency-Key"] == str(key)
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert json.loads(requests[0].content) == {
        "scope": {
            "realm": "cairn",
            "segments": [{"kind": "project", "identifier": "synthetic"}],
        },
        "fact_ids": [str(FACT)],
        "reason": "Measured again",
        "superseded_by": str(replacement),
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,bad",
    [
        ("result", {"fact_ids": [], "invalidated_at": "2026-09-09T12:00:00.000000Z"}),
        (
            "result",
            {
                "fact_ids": [str(uuid4())],
                "invalidated_at": "2026-09-09T12:00:00.000000Z",
            },
        ),
        ("result", {"fact_ids": [str(FACT)], "invalidated_at": "yesterday"}),
        ("audit_receipt", {}),
        ("mutation_receipt", {}),
        ("outcome", "saved"),
        ("extra", "PRIVATE SERVER PROSE"),
    ],
)
async def test_correct_refuses_invalid_receipts_without_trusting_prose(
    field: str, bad: object
) -> None:
    document = {**response(), field: bad}
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=document)
        ),
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            await _client(http).correct(
                (FACT,), reason="Correction", idempotency_key=uuid4()
            )
    assert caught.value.operation == "correct"
    assert caught.value.failure.code == "invalid_response"
    assert "PRIVATE" not in repr(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "facts,reason",
    [
        ((), "valid"),
        ((FACT, FACT), "valid"),
        ((FACT,), ""),
        ((FACT,), "x" * 4097),
        ((FACT,), "\ud800"),
    ],
    ids=["empty", "duplicate", "empty-reason", "oversized-reason", "invalid-unicode"],
)
async def test_correct_invalid_input_stops_before_network(
    facts: tuple[UUID, ...], reason: str
) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid correction must not start network I/O")

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(serve)
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            await _client(http).correct(facts, reason=reason, idempotency_key=uuid4())
    assert caught.value.failure.code == "invalid_correction"


@pytest.mark.anyio
@pytest.mark.parametrize("compressed", [False, True])
async def test_correction_wire_limit_stops_and_closes_stream(compressed: bool) -> None:
    class Stream(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.reads += 1
            yield b" " * 16385
            self.reads += 1
            yield b"PRIVATE SERVER BODY"

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                stream=stream,
                headers={"Content-Encoding": "gzip"} if compressed else {},
            )
        ),
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            await _client(http).correct(
                (FACT,), reason="Correction", idempotency_key=uuid4()
            )
    assert caught.value.failure.code == "invalid_response"
    assert stream.reads == (0 if compressed else 1) and stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("status", [302, 401, 403, 409, 500])
async def test_correction_failure_uses_safe_metadata_without_retry(status: int) -> None:
    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            text="PRIVATE SERVER ERROR",
            headers={"Location": "https://other.invalid"},
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(serve)
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            await _client(http).correct(
                (FACT,), reason="Correction", idempotency_key=uuid4()
            )
    assert calls == 1
    assert caught.value.failure.status_code == status
    assert "PRIVATE" not in repr(caught.value)


@pytest.mark.anyio
async def test_real_client_correction_retains_history_and_original_receipt(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        old = await api.remember("Synthetic old measurement")
        new = await api.remember("Synthetic corrected measurement")
        old_id, new_id = (
            UUID(old["result"]["fact_ids"][0]),
            UUID(new["result"]["fact_ids"][0]),
        )
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
        )
        key = uuid4()
        first = await client.correct(
            (old_id,),
            reason="Measured again",
            superseded_by=new_id,
            idempotency_key=key,
        )
        replay = await client.correct(
            (old_id,),
            reason="Measured again",
            superseded_by=new_id,
            idempotency_key=key,
        )
        assert (
            first.status is PersistenceStatus.COMMITTED
            and replay.status is PersistenceStatus.REPLAYED
        )
        assert (
            first.result == replay.result
            and first.mutation_receipt == replay.mutation_receipt
        )
        history = await client.history(old_id)
        facts = cast(tuple[FrozenJSONObject, ...], history.data["facts"])
        corrections = cast(tuple[FrozenJSONObject, ...], history.data["corrections"])
        assert {fact["fact_id"] for fact in facts} == {
            str(old_id),
            str(new_id),
        }
        assert corrections[0]["superseded_by"] == str(new_id)
