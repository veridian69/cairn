"""recall-page over REST and MCP: pages, continuation and operation-local failures."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.authority.recall_snapshots import SnapshotStore
from cairn.catalogue.sqlite import read_connection


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _audit_rows(instance: Instance) -> list[tuple[str, str, str, dict[str, Any]]]:
    with read_connection(instance.data_path) as connection:
        rows = connection.execute(
            "SELECT action_code, outcome, reason_code, canonical_event FROM audit_events"
        ).fetchall()
    return [(a, o, r, json.loads(e)) for a, o, r, e in rows]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_recall_page_round_trip_and_continuation(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        for i in range(3):
            assert (await api.remember(f"deploy note {i}"))["outcome"] == "committed"
        first = await api.call(
            "recall-page",
            {"scope": SCOPE, "query": "deploy", "order": "newest", "limit": 2},
        )
        assert first["ordering"] == {
            "order": "newest",
            "time_basis": "source",
            "policy": "memory-order/v1",
        }
        assert len(first["hits"]) == 2 and first["facts_remaining"] is True
        hit = first["hits"][0]
        assert {
            "observed_at",
            "source_time_status",
            "ordering_time_basis",
            "source_evidence_id",
        } <= set(hit)
        rest = await api.call(
            "recall-page", {"scope": SCOPE, "cursor": first["next_cursor"], "limit": 2}
        )
        assert len(rest["hits"]) == 1 and rest["next_cursor"] is None
        seen = {h["fact_id"] for h in first["hits"]} | {
            h["fact_id"] for h in rest["hits"]
        }
        assert len(seen) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_too_small_budget_uses_operation_local_detail_and_is_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        await api.remember("budget " + "y" * 2000)
        refused = await api.call(
            "recall-page", {"scope": SCOPE, "query": "budget", "budget": 10}
        )
    failure = refused["failure"]
    assert set(failure) == {"code", "message", "retry", "correlation_id", "detail"}
    assert failure["code"] == "invalid_request" and failure["retry"] == "never"
    detail = failure["detail"]
    assert set(detail) == {"reason", "minimum_budget"}
    assert detail["reason"] == "page_budget_too_small"
    assert type(detail["minimum_budget"]) is int
    assert 10 < detail["minimum_budget"] <= 1_048_576
    denials = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-recall-page", "deny", "page_budget_too_small")
    ]
    assert [e["correlation_id"] for e in denials] == [failure["correlation_id"]]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_unknown_cursor_is_continuation_unavailable_and_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "recall-page", {"scope": SCOPE, "cursor": "A" * 43}
        )
    failure = refused["failure"]
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"reason": "continuation_unavailable"}
    denials = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-recall-page", "deny", "continuation_unavailable")
    ]
    assert [e["correlation_id"] for e in denials] == [failure["correlation_id"]]


@pytest.mark.anyio
async def test_rest_page_failure_status_is_400(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        response = await http.post(
            "/memory/v1/recall-page",
            json={"scope": SCOPE, "cursor": "A" * 43},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400
    assert "retry-after" not in response.headers


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_other_refusals_keep_the_legacy_envelope(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(operations=["ingest"])
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "recall-page", {"scope": SCOPE, "query": "anything"}
        )
    assert refused["failure"]["code"] == "authorisation_denied"
    assert "detail" not in refused["failure"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "body,rule,field",
    [
        ({"cursor": "A" * 43, "query": "x"}, "unknown_field", "query"),
        ({"cursor": "A" * 43, "order": "newest"}, "unknown_field", "order"),
        ({"query": "x", "time_basis": "source"}, "invalid_value", "time_basis"),
        ({"query": "x", "time_basis": None}, "invalid_value", "time_basis"),
        (
            {"query": "x", "order": "relevance", "time_basis": "recorded"},
            "invalid_value",
            "time_basis",
        ),
        ({}, "missing_field", "query"),
        ({"query": "x", "limit": 0}, "invalid_value", "limit"),
        ({"query": "x", "order": "random"}, "invalid_value", "order"),
        ({"cursor": "short"}, "invalid_value", "cursor"),
    ],
)
async def test_recall_page_wire_rejections(
    tmp_path: Path, transport: str, body: dict[str, Any], rule: str, field: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "recall-page", {"scope": SCOPE, **body}
        )
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == rule
    assert refused["failure"]["detail"]["field_path"] == field


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_wire_rejection_of_recall_page_is_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "recall-page", {"scope": SCOPE, "query": "x", "limit": 0}
        )
    rows = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-recall-page", "deny", "invalid_request")
    ]
    assert len(rows) == 1
    event = rows[0]
    assert event["correlation_id"] == refused["failure"]["correlation_id"]
    assert event["principal_id"] == str(principal)
    assert event["requested_scope"] is None
    assert event["safe_request_fingerprint"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("operation", ["recall-page", "recall"])
async def test_query_that_is_not_utf8_encodable_is_a_wire_rejection(
    tmp_path: Path, transport: str, operation: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    # Hand-encoded so the lone surrogate escape survives to the server.
    arguments = (
        json.dumps({"scope": SCOPE, "query": "port"})
        .replace('"port"', '"port \\ud800"')
        .encode()
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with serve(instance) as http:
        if transport == "rest":
            response = await http.post(
                f"/memory/v1/{operation}", content=arguments, headers=headers
            )
            refused = response.json()
        else:
            frame = (
                b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                b'{"name":"'
                + operation.encode()
                + b'","arguments":'
                + arguments
                + b"}}"
            )
            response = await http.post("/memory/v1/mcp", content=frame, headers=headers)
            refused = json.loads(response.json()["result"]["content"][0]["text"])
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == "invalid_value"
    assert refused["failure"]["detail"]["field_path"] == "query"
    rows = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == (f"memory-{operation}", "deny", "invalid_request")
    ]
    assert [event["principal_id"] for event in rows] == [str(principal)]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_recall_page_rejects_idempotency_keys(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "recall-page", {"scope": SCOPE, "query": "x"}, key=str(uuid4())
        )
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == "idempotency_key_forbidden"


@pytest.mark.anyio
async def test_shutdown_closes_the_snapshot_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[SnapshotStore] = []

    class Recording(SnapshotStore):
        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr("cairn.runtime.composition.SnapshotStore", Recording)
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token)
        for i in range(2):
            await api.remember(f"shutdown note {i}")
        page = await api.call(
            "recall-page", {"scope": SCOPE, "query": "shutdown", "limit": 1}
        )
        assert page["next_cursor"] is not None
        assert closed == []
    assert len(closed) == 1
    assert closed[0].usage() == (0, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_snapshot_capacity_is_dependency_unavailable_after_delay(
    tmp_path: Path, transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Full(SnapshotStore):
        def __init__(self) -> None:
            super().__init__(per_process=0)

    monkeypatch.setattr("cairn.runtime.composition.SnapshotStore", Full)
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    body = {"scope": SCOPE, "query": "capacity", "limit": 1}
    async with serve(instance) as http:
        api = Api(http, token, transport)
        for i in range(2):
            await api.remember(f"capacity note {i}")
        if transport == "rest":
            response = await http.post(
                "/memory/v1/recall-page",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "1"
            refused = response.json()
        else:
            refused = await api.call("recall-page", body)
    failure = refused["failure"]
    assert failure["code"] == "dependency_unavailable"
    assert failure["retry"] == "after-delay"
    assert "detail" not in failure
    denials = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-recall-page", "deny", "recall_page_capacity")
    ]
    assert [e["correlation_id"] for e in denials] == [failure["correlation_id"]]
