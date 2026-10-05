"""The conversation adapter's read-only source_window tool (evidence-window)."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_conversation_adapter import SOURCE_ID, adapter
from test_evidence_window_client import deliver_evidence

import cairn.client.conversation as conversation
from cairn.client.errors import FailureMetadata, RecallFailure
from cairn.client.memory import MemoryClient

SOURCE = "user: what is the Copper Finch capacity?\nassistant: eight is confirmed.\n"
EVIDENCE = "66666666-6666-4666-8666-666666666666"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _too_small(minimum: int) -> RecallFailure:
    return RecallFailure(
        "evidence-window",
        FailureMetadata(
            code="invalid_request",
            message="m",
            retry="never",
            correlation_id=None,
            status_code=400,
            detail=(("minimum_budget", minimum), ("reason", "page_budget_too_small")),
        ),
    )


@pytest.mark.anyio
async def test_source_window_reads_a_recalled_facts_source_evidence(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        writer = adapter(http, instance, principal, source_body=SOURCE)
        saved = await writer.call(
            "remember",
            {
                "body": "Copper Finch capacity is eight.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert saved["status"] == "verified", saved
        deliver_evidence(instance)
        reader = adapter(http, instance, principal, source_body=SOURCE, read_only=True)
        recalled = await reader.call("recall", {"query": "Copper Finch capacity"})
        assert recalled["status"] == "ok", recalled
        evidence_id = recalled["result"]["data"]["hits"][0]["source_evidence_id"]
        assert evidence_id is not None
        found = await reader.call(
            "source_window", {"evidence_id": evidence_id, "query": "EIGHT is"}
        )
        later = await reader.call(
            "source_window", {"evidence_id": evidence_id, "start": 10}
        )
    assert found["status"] == "ok", found
    data = found["result"]["data"]
    assert found["result"]["content_role"] == "untrusted-data"
    assert data["match_found"] is True and data["mode"] == "query"
    assert "eight is confirmed" in data["text"]
    assert "Copper Finch capacity" in data["text"]
    assert later["status"] == "ok", later
    assert later["result"]["data"]["mode"] == "offset"
    assert later["result"]["data"]["start_byte"] == 10


@pytest.mark.anyio
async def test_source_window_fails_closed_on_an_unsupported_server(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: False)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        paths: list[str] = []

        async def record(request: httpx.Request) -> None:
            paths.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await adapter(http, instance, principal).call(
            "source_window", {"evidence_id": EVIDENCE, "query": "eight"}
        )
    assert result["error"] == {"code": "source_window_unsupported"}
    assert result["status"] == "unconfirmed"
    assert "/memory/v1/evidence-window" not in paths


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": "eight"},
        {"evidence_id": EVIDENCE, "query": "eight", "start": 0},
        {"evidence_id": "66666666-6666-1666-8666-666666666666"},
        {"evidence_id": EVIDENCE.replace("6", "A")},
        {"evidence_id": EVIDENCE, "query": ""},
        {"evidence_id": EVIDENCE, "query": "   "},
        {"evidence_id": EVIDENCE, "query": "é" * 4097},
        {"evidence_id": EVIDENCE, "start": -1},
        {"evidence_id": EVIDENCE, "start": 1_048_577},
        {"evidence_id": EVIDENCE, "start": True},
        {"evidence_id": EVIDENCE, "budget": 6},
    ],
)
async def test_source_window_rejects_invalid_shapes_before_any_request(
    tmp_path: Path, memory_support: ModuleType, arguments: dict[str, object]
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        paths: list[str] = []

        async def record(request: httpx.Request) -> None:
            paths.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await adapter(http, instance, principal).call(
            "source_window", arguments
        )
    assert result["status"] == "rejected"
    assert result["error"] == {"code": "invalid_input"}
    assert paths == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "minimums,budgets,status",
    [
        # Retry exactly once at a stated minimum above the fixed read size.
        ([20000], [16384, 20000], "ok"),
        ([20000, 30000], [16384, 20000], "unconfirmed"),
        # No retry when the stated minimum would not enlarge the read.
        ([16384], [16384], "unconfirmed"),
    ],
)
async def test_source_window_retry_gate(
    tmp_path: Path,
    memory_support: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    minimums: list[int],
    budgets: list[int],
    status: str,
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        seen: list[int] = []
        pending = list(minimums)

        async def fake(self: MemoryClient, evidence_id: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["budget"])
            assert kwargs["query"] == "eight" and kwargs["start"] is None
            if pending:
                raise _too_small(pending.pop(0))
            return None

        monkeypatch.setattr(MemoryClient, "evidence_window", fake)
        monkeypatch.setattr(conversation, "plain", lambda value: {"stub": True})
        result = await adapter(http, instance, principal).call(
            "source_window", {"evidence_id": EVIDENCE, "query": "eight"}
        )
    assert seen == budgets
    assert result["status"] == status
    if status != "ok":
        assert result["error"] == {
            "code": "invalid_request",
            "operation": "evidence-window",
        }
