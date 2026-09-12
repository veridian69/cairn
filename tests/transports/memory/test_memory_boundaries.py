import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.authority.memory import CairnMemory
from cairn.catalogue.transactions import CatalogueContention
from cairn.transports.v1.parsing import MAX_REQUEST_BYTES


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/memory/v1/remember", "/memory/v1/mcp"])
async def test_authentication_precedes_body_consumption(
    tmp_path: Path, path: str
) -> None:
    instance = Instance(tmp_path)
    consumed = False

    async def hostile_body() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        raise AssertionError("Body was read before authentication")
        yield b""

    async with serve(instance) as http:
        response = await http.post(
            path, content=hostile_body(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401
        assert not consumed
        assert response.json()["failure"]["code"] == "authentication_failed"


@pytest.mark.anyio
async def test_exact_mounts_and_independent_tool_inventories(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with serve(instance) as http:
        for path, forbidden in [
            ("/v1/mcp", "remember"),
            ("/memory/v1/mcp", "create-grant"),
        ]:
            listed = await http.post(
                path,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            names = {tool["name"] for tool in listed.json()["result"]["tools"]}
            assert forbidden not in names
            if path.startswith("/memory"):
                assert names == {
                    "diagnose",
                    "remember",
                    "recall",
                    "history",
                    "suggest",
                    "propose",
                    "proposal-list",
                    "proposal-read",
                    "proposal-accept",
                    "proposal-reject",
                    "disagree",
                    "resolve",
                    "correct",
                    "session-open",
                    "turn-begin",
                    "turn-prepare",
                    "turn-commit",
                    "turn-abandon",
                    "session-read",
                    "visit-issue",
                    "visit-acknowledge",
                }
            response = await http.post(
                path,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": forbidden, "arguments": {}},
                },
            )
            assert "error" in response.json()
        for path in ["/memory/v1/missing", "/memory/v1/mcp/child", "/memory/v1/mcp/"]:
            response = await http.post(path, headers=headers)
            assert response.status_code == 404
            assert response.json()["failure"]["code"] == "not_found"
        response = await http.get("/memory/v1/recall", headers=headers)
        assert response.status_code == 405
        assert response.json()["failure"]["detail"]["rule"] == "method_not_allowed"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_strict_json_admission(tmp_path: Path, transport: str) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    path = "/memory/v1/recall" if transport == "rest" else "/memory/v1/mcp"
    async with serve(instance) as http:
        for raw, rule in [
            ("[]", "body_not_object"),
            ('{"scope":{},"scope":{}}', "duplicate_json_key"),
            ('{"secret":"do not echo"', "malformed_json"),
            (" " * (MAX_REQUEST_BYTES + 1), "body_too_large"),
        ]:
            response = await http.post(path, headers=headers, content=raw)
            document = response.json()
            failure = (
                document["failure"]
                if transport == "rest"
                else document["error"]["data"]["failure"]
            )
            assert failure["detail"]["rule"] == rule
            assert "do not echo" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_contention_and_internal_errors_are_safe_and_labelled(
    tmp_path: Path,
    transport: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        for exception, code, retry in [
            (CatalogueContention(), "dependency_unavailable", "after-delay"),
            (
                RuntimeError("secret payload must stay hidden"),
                "internal_error",
                "never",
            ),
        ]:

            def fail(*args: Any, error: Exception = exception, **kwargs: Any) -> Any:
                raise error

            monkeypatch.setattr(CairnMemory, "recall", fail)
            result = await api.call("recall", {"scope": SCOPE, "query": "safe"})
            assert result["failure"]["code"] == code, result
            assert result["failure"]["retry"] == retry, result
            assert "secret payload" not in str(result)
        metrics = (await http.get("/metrics")).text
        assert 'operation="memory_recall"' in metrics
        assert 'outcome_code="unavailable"' in metrics
    logs = capsys.readouterr().err
    assert token not in logs
    assert "secret payload" not in logs
    assert '"operation": "memory_recall"' in logs


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_hidden_relationships_do_not_disclose_identifiers(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, author_token = instance.add_actor()
    _, reader_token = instance.add_actor(read_clearance="internal")
    async with serve(instance) as http:
        author = Api(http, author_token, transport)
        visible = await author.remember("Public approach.")
        hidden = await author.remember(
            "Restricted approach.", classification="restricted"
        )
        left, right = visible["result"]["fact_ids"][0], hidden["result"]["fact_ids"][0]
        disagreement = await author.call(
            "disagree",
            {
                "scope": SCOPE,
                "left_fact_id": left,
                "right_fact_id": right,
                "classification": "restricted",
                "reason": "Restricted comparison.",
            },
            key=str(uuid4()),
        )
        relation = disagreement["result"]["relationship_id"]
        reader = Api(http, reader_token, transport)
        recall = await reader.call("recall", {"scope": SCOPE, "query": "approach"})
        history = await reader.call("history", {"scope": SCOPE, "fact_id": left})
        for result in (recall, history):
            assert result["disagreements"] == []
            assert right not in json.dumps(result)
            assert relation not in json.dumps(result)
            assert "Restricted" not in json.dumps(result)
