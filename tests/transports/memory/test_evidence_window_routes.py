"""evidence-window over REST and MCP: windows, wire refusals and page failures."""

import json
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.evidence.attic import SqliteAttic
from cairn.evidence.delivery import deliver_evidence_outbox

UNKNOWN = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _deliver(instance: Instance) -> None:
    """Stand in for the Attic delivery loop, deterministically."""
    deliver_evidence_outbox(
        CatalogueTransactions(
            instance.data_path,
            writer_gate=threading.Lock(),
            clock=instance.clock,
            uuid_factory=uuid4,
        ),
        SqliteAttic(instance.data_path),
        clock=instance.clock,
    )


def _audit_rows(instance: Instance) -> list[tuple[str, str, str, dict[str, Any]]]:
    with read_connection(instance.data_path) as connection:
        rows = connection.execute(
            "SELECT action_code, outcome, reason_code, canonical_event FROM audit_events"
        ).fetchall()
    return [(a, o, r, json.loads(e)) for a, o, r, e in rows]


async def _saved_evidence(api: Api, instance: Instance, excerpt: str) -> str:
    saved = await api.remember("The build uses port 8123.", evidence_payload=excerpt)
    assert saved["outcome"] == "committed", saved
    _deliver(instance)
    evidence_id = saved["result"]["evidence_id"]
    assert isinstance(evidence_id, str)
    return evidence_id


async def _raw(
    http: AsyncClient, token: str, transport: str, arguments: bytes
) -> dict[str, Any]:
    """Send hand-encoded JSON arguments (a lone surrogate escape survives)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if transport == "rest":
        response = await http.post(
            "/memory/v1/evidence-window", content=arguments, headers=headers
        )
        result = response.json()
    else:
        frame = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
            b'{"name":"evidence-window","arguments":' + arguments + b"}}"
        )
        response = await http.post("/memory/v1/mcp", content=frame, headers=headers)
        result = json.loads(response.json()["result"]["content"][0]["text"])
    assert isinstance(result, dict)
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_recall_page_source_evidence_reaches_evidence_window(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        excerpt = "user: please use port 8123\nassistant: noted\n"
        saved = await _saved_evidence(api, instance, excerpt)
        page = await api.call("recall-page", {"scope": SCOPE, "query": "port"})
        evidence_id = page["hits"][0]["source_evidence_id"]
        assert evidence_id == saved
        window = await api.call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": evidence_id, "query": "port 8123"},
        )
    assert window["match_found"] is True and "port 8123" in window["text"]
    assert window["byte_length"] == len(excerpt.encode())
    assert window["mode"] == "query" and window["evidence_id"] == evidence_id
    start, end = window["match_start_byte"], window["match_end_byte"]
    assert excerpt.encode()[start:end] == b"port 8123"
    assert window["text"] == excerpt and window["next_start_byte"] is None
    events = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-evidence-window", "allow", "evidence_window_completed")
    ]
    assert len(events) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_offset_windows_continue_to_the_end_without_loss(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    excerpt = "".join(f"line {n}: Grüezi café\n" for n in range(40))
    async with serve(instance) as http:
        api = Api(http, token, transport)
        evidence_id = await _saved_evidence(api, instance, excerpt)
        pieces: list[str] = []
        body: dict[str, Any] = {
            "scope": SCOPE,
            "evidence_id": evidence_id,
            "budget": 400,
        }
        for _ in range(50):
            window = await api.call("evidence-window", body)
            assert window["mode"] == "offset" and window["match_found"] is None
            assert window["budget_consumed"] <= 400
            pieces.append(window["text"])
            if window["next_start_byte"] is None:
                break
            body = {**body, "start": window["next_start_byte"]}
        else:
            raise AssertionError("offset paging did not finish")
    assert "".join(pieces) == excerpt and len(pieces) > 1


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_query_without_match_is_the_metadata_only_variant(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        evidence_id = await _saved_evidence(api, instance, "nothing relevant\n")
        window = await api.call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": evidence_id, "query": "zebra"},
        )
    assert window["match_found"] is False
    for name in (
        "text",
        "start_byte",
        "end_byte",
        "match_start_byte",
        "match_end_byte",
        "prefix_omitted",
        "suffix_omitted",
        "next_start_byte",
    ):
        assert window[name] is None


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_too_small_budget_uses_operation_local_detail_and_is_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        evidence_id = await _saved_evidence(api, instance, "port 8123\n")
        refused = await api.call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": evidence_id, "query": "port", "budget": 10},
        )
    failure = refused["failure"]
    assert set(failure) == {"code", "message", "retry", "correlation_id", "detail"}
    assert failure["code"] == "invalid_request" and failure["retry"] == "never"
    detail = failure["detail"]
    assert set(detail) == {"reason", "minimum_budget"}
    assert detail["reason"] == "page_budget_too_small"
    assert 10 < detail["minimum_budget"] <= 1_048_576
    denials = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-evidence-window", "deny", "page_budget_too_small")
    ]
    assert [e["correlation_id"] for e in denials] == [failure["correlation_id"]]


@pytest.mark.anyio
async def test_rest_page_failure_status_is_400(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        evidence_id = await _saved_evidence(Api(http, token), instance, "port 8123\n")
        response = await http.post(
            "/memory/v1/evidence-window",
            json={"scope": SCOPE, "evidence_id": evidence_id, "budget": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400
    assert response.json()["failure"]["detail"]["reason"] == "page_budget_too_small"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_unknown_evidence_keeps_the_legacy_envelope(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "evidence-window", {"scope": SCOPE, "evidence_id": UNKNOWN, "query": "x"}
        )
    assert refused["failure"]["code"] == "not_found"
    assert "detail" not in refused["failure"]


@pytest.mark.anyio
async def test_evidence_window_query_and_start_together_is_rejected(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token)
        refused = await api.call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": UNKNOWN, "query": "x", "start": 0},
        )
        assert refused["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "body,rule,field",
    [
        ({"evidence_id": UNKNOWN, "query": "x", "start": 0}, "invalid_value", "start"),
        ({"query": "x"}, "missing_field", "evidence_id"),
        ({"evidence_id": UNKNOWN.replace("1", "A")}, "invalid_value", "evidence_id"),
        ({"evidence_id": "not-a-uuid"}, "invalid_value", "evidence_id"),
        ({"evidence_id": UNKNOWN, "query": ""}, "invalid_value", "query"),
        ({"evidence_id": UNKNOWN, "start": -1}, "invalid_value", "start"),
        ({"evidence_id": UNKNOWN, "start": 1_048_577}, "invalid_value", "start"),
        ({"evidence_id": UNKNOWN, "start": True}, "invalid_value", "start"),
        ({"evidence_id": UNKNOWN, "budget": 0}, "invalid_value", "budget"),
        ({"evidence_id": UNKNOWN, "budget": 1_048_577}, "invalid_value", "budget"),
        ({"evidence_id": UNKNOWN, "cursor": "A" * 43}, "unknown_field", "cursor"),
    ],
)
async def test_evidence_window_wire_rejections(
    tmp_path: Path, transport: str, body: dict[str, Any], rule: str, field: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "evidence-window", {"scope": SCOPE, **body}
        )
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == rule
    assert refused["failure"]["detail"]["field_path"] == field


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_query_that_is_not_utf8_encodable_is_a_wire_rejection(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    arguments = json.dumps(
        {"scope": SCOPE, "evidence_id": UNKNOWN, "query": "port"}
    ).replace('"port"', '"port \\ud800"')
    async with serve(instance) as http:
        refused = await _raw(http, token, transport, arguments.encode())
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == "invalid_value"
    assert refused["failure"]["detail"]["field_path"] == "query"
    rows = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-evidence-window", "deny", "invalid_request")
    ]
    assert [event["principal_id"] for event in rows] == [str(principal)]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_wire_rejection_of_evidence_window_is_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": UNKNOWN, "query": "x", "start": 0},
        )
    rows = [
        event
        for action, outcome, reason, event in _audit_rows(instance)
        if (action, outcome, reason)
        == ("memory-evidence-window", "deny", "invalid_request")
    ]
    assert len(rows) == 1
    event = rows[0]
    assert event["correlation_id"] == refused["failure"]["correlation_id"]
    assert event["principal_id"] == str(principal)
    assert event["requested_scope"] is None
    assert event["safe_request_fingerprint"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_evidence_window_rejects_idempotency_keys(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": UNKNOWN, "query": "x"},
            key=str(uuid4()),
        )
    assert refused["failure"]["code"] == "invalid_request"
    assert refused["failure"]["detail"]["rule"] == "idempotency_key_forbidden"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_evidence_window_needs_retrieve(tmp_path: Path, transport: str) -> None:
    instance = Instance(tmp_path)
    _, writer = instance.add_actor(operations=["ingest"])
    async with serve(instance) as http:
        evidence_id = await _saved_evidence(
            Api(http, writer, transport), instance, "port 8123\n"
        )
        refused = await Api(http, writer, transport).call(
            "evidence-window",
            {"scope": SCOPE, "evidence_id": evidence_id, "query": "port"},
        )
    assert refused["failure"]["code"] == "authorisation_denied"
    assert "detail" not in refused["failure"]
