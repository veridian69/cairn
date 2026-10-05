from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_conversation_adapter import SOURCE_ID, adapter

import cairn.client.conversation as conversation
import cairn.runtime.composition as composition
from cairn.client.conversation import supports_pages
from cairn.client.errors import FailureMetadata, RecallFailure
from cairn.client.memory import MemoryClient

CURSOR = "A" * 43


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.7.13", True),
        ("0.7.13rc1", True),
        ("0.8.0", True),
        ("1.0.0", True),
        ("0.7.12", False),
        ("0.7", False),
        ("dev", False),
    ],
)
def test_supports_pages_parses_product_version(version: str, expected: bool) -> None:
    assert supports_pages({"product_version": version}) is expected


def test_supports_pages_treats_missing_or_non_string_version_as_unsupported() -> None:
    assert supports_pages({}) is False
    assert supports_pages({"product_version": 7}) is False


@pytest.mark.anyio
async def test_adapter_recall_defaults_to_relevant_recall_page(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The in-repo server still reports 0.7.12 (no version bump in this work).
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        result = await adapter(http, instance, principal).call(
            "recall", {"query": "anything"}
        )
        assert result["status"] == "ok", result
        assert result["ordering"] == "recall-page"


@pytest.mark.anyio
async def test_adapter_falls_back_only_for_default_relevance(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: False)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        fallback = await service.call("recall", {"query": "x"})
        assert fallback["status"] == "ok", fallback
        assert fallback["ordering"] == "legacy-fallback"
        explicit = await service.call("recall", {"query": "x", "order": "relevance"})
        assert explicit["ordering"] == "legacy-fallback"
        for arguments in (
            {"query": "x", "order": "newest"},
            {"query": "x", "order": "oldest"},
            {"cursor": CURSOR},
        ):
            refused = await service.call("recall", arguments)
            assert refused["error"] == {"code": "ordering_unsupported"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": "x", "order": "sideways"}, {"query": "x", "cursor": "short"}],
)
async def test_adapter_recall_rejects_invalid_shapes_as_invalid_input(
    tmp_path: Path, memory_support: ModuleType, arguments: dict[str, object]
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        result = await adapter(http, instance, principal).call("recall", arguments)
        assert result["status"] == "rejected"
        assert result["error"] == {"code": "invalid_input"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [{"cursor": CURSOR, "query": "x"}, {"cursor": CURSOR, "order": "newest"}],
)
async def test_adapter_cursor_requires_no_query_or_order(
    tmp_path: Path, memory_support: ModuleType, arguments: dict[str, object]
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        result = await adapter(http, instance, principal).call("recall", arguments)
        assert result["status"] == "rejected"
        assert result["error"] == {"code": "cursor_requires_no_query"}


def _too_small(minimum: int) -> RecallFailure:
    return RecallFailure(
        "recall-page",
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
async def test_adapter_retries_once_with_minimum_budget(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        real = MemoryClient.recall_page
        budgets: list[int] = []

        async def flaky(self: MemoryClient, query: str, **kwargs: Any) -> Any:
            budgets.append(kwargs["budget"])
            if len(budgets) == 1:
                raise _too_small(20000)
            return await real(self, query, **kwargs)

        monkeypatch.setattr(MemoryClient, "recall_page", flaky)
        result = await service.call("recall", {"query": "x", "order": "newest"})
        assert result["status"] == "ok", result
        assert budgets == [16384, 20000]


@pytest.mark.anyio
async def test_adapter_retries_only_once_then_surfaces_closed_code(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        budgets: list[int] = []

        async def always(self: MemoryClient, query: str, **kwargs: Any) -> Any:
            budgets.append(kwargs["budget"])
            raise _too_small(20000)

        monkeypatch.setattr(MemoryClient, "recall_page", always)
        result = await service.call("recall", {"query": "x"})
        assert budgets == [16384, 20000]
        assert result["error"] == {
            "code": "invalid_request",
            "operation": "recall-page",
        }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("minimum", "expected"), [(65536, [16384, 65536]), (65537, [16384])]
)
async def test_adapter_retry_is_capped_at_four_read_budgets(
    tmp_path: Path,
    memory_support: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    minimum: int,
    expected: list[int],
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        budgets: list[int] = []

        async def always(self: MemoryClient, query: str, **kwargs: Any) -> Any:
            budgets.append(kwargs["budget"])
            raise _too_small(minimum)

        monkeypatch.setattr(MemoryClient, "recall_page", always)
        result = await service.call("recall", {"query": "x"})
        assert budgets == expected
        assert result["error"] == {
            "code": "invalid_request",
            "operation": "recall-page",
        }


@pytest.mark.anyio
async def test_adapter_does_not_retry_continuation_unavailable(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        calls: list[int] = []

        async def gone(self: MemoryClient, query: str, **kwargs: Any) -> Any:
            calls.append(kwargs["budget"])
            raise RecallFailure(
                "recall-page",
                FailureMetadata(
                    code="invalid_request",
                    message="m",
                    retry="never",
                    correlation_id=None,
                    status_code=400,
                    detail=(("reason", "continuation_unavailable"),),
                ),
            )

        monkeypatch.setattr(MemoryClient, "recall_page", gone)
        result = await service.call("recall", {"query": "x"})
        assert calls == [16384]
        assert result["error"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_adapter_continues_with_cursor_only(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        seen: list[tuple[str, int]] = []

        async def fake(self: MemoryClient, cursor: str, **kwargs: Any) -> Any:
            seen.append((cursor, kwargs["budget"]))
            return None

        monkeypatch.setattr(MemoryClient, "continue_recall", fake)
        monkeypatch.setattr(conversation, "plain", lambda value: {"stub": True})
        result = await service.call("recall", {"cursor": CURSOR})
        assert result == {
            "status": "ok",
            "result": {"stub": True},
            "ordering": "recall-page",
        }
        assert seen == [(CURSOR, 16384)]


@pytest.mark.anyio
async def test_adapter_continuation_retries_once_with_minimum_budget(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        seen: list[tuple[str, int]] = []
        page = object()

        async def flaky(self: MemoryClient, cursor: str, **kwargs: Any) -> Any:
            seen.append((cursor, kwargs["budget"]))
            if len(seen) == 1:
                raise _too_small(20000)
            return page

        monkeypatch.setattr(MemoryClient, "continue_recall", flaky)
        monkeypatch.setattr(
            conversation, "plain", lambda value: {"page": value is page}
        )
        result = await service.call("recall", {"cursor": CURSOR})
        assert result == {
            "status": "ok",
            "result": {"page": True},
            "ordering": "recall-page",
        }
        assert seen == [(CURSOR, 16384), (CURSOR, 20000)]


@pytest.mark.anyio
async def test_adapter_continuation_retries_only_once_then_surfaces_closed_code(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        budgets: list[int] = []

        async def always(self: MemoryClient, cursor: str, **kwargs: Any) -> Any:
            budgets.append(kwargs["budget"])
            raise _too_small(20000)

        monkeypatch.setattr(MemoryClient, "continue_recall", always)
        result = await service.call("recall", {"cursor": CURSOR})
        assert budgets == [16384, 20000]
        assert result["error"] == {
            "code": "invalid_request",
            "operation": "recall-page",
        }


@pytest.mark.anyio
async def test_adapter_retry_requires_minimum_above_read_budget(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        budgets: list[int] = []

        async def small(self: MemoryClient, query: str, **kwargs: Any) -> Any:
            budgets.append(kwargs["budget"])
            raise _too_small(16384)

        monkeypatch.setattr(MemoryClient, "recall_page", small)
        result = await service.call("recall", {"query": "x"})
        assert budgets == [16384]
        assert result["error"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_adapter_default_relevance_sends_no_time_basis(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        bodies: list[dict[str, Any]] = []

        async def record(request: httpx.Request) -> None:
            if request.url.path.endswith("/recall-page"):
                bodies.append(json.loads(request.content))

        http.event_hooks["request"].append(record)
        result = await adapter(http, instance, principal).call(
            "recall", {"query": "anything"}
        )
        assert result["status"] == "ok", result
        assert len(bodies) == 1
        assert "time_basis" not in bodies[0]
        assert bodies[0]["relevant_only"] is True
        assert bodies[0]["order"] == "relevance"


@pytest.mark.anyio
async def test_adapter_uses_served_product_version_without_patching(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(composition, "__version__", "0.7.13")
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        checked = await service.call("check", {})
        assert checked["result"]["product_version"] == "0.7.13"
        result = await service.call("recall", {"query": "anything"})
        assert result["status"] == "ok", result
        assert result["ordering"] == "recall-page"


@pytest.mark.anyio
async def test_adapter_pages_through_cursor_without_loss_or_duplicates(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation, "supports_pages", lambda diagnostic: True)
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        saved: set[str] = set()
        for number in range(22):
            made = await service.call(
                "remember",
                {
                    "body": f"Copper Finch capacity is {number}.",
                    "source_id": str(SOURCE_ID),
                    "idempotency_key": str(uuid4()),
                },
            )
            assert made["status"] == "verified", made
            saved.add(made["mapping"]["fact_id"])
        seen: list[str] = []
        arguments: dict[str, object] = {"query": "Copper Finch capacity"}
        for _ in range(5):
            page = await service.call("recall", arguments)
            assert page["status"] == "ok", page
            data = page["result"]["data"]
            seen.extend(hit["fact_id"] for hit in data["hits"])
            cursor = data["next_cursor"]
            if cursor is None:
                break
            arguments = {"cursor": cursor}
        else:
            raise AssertionError("paging did not finish")
        assert len(seen) == len(set(seen))
        assert set(seen) == saved
