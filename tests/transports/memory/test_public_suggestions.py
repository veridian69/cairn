"""Real REST/MCP suggestions must disclose complete evidence without custody."""

import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)
from cairn.client.memory import MemoryClient
from cairn.projection.memory import MemoryIndex
from cairn.runtime.composition import build_application


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def inventory(instance: Instance) -> dict[str, list[tuple[Any, ...]]]:
    with read_connection(instance.data_path) as con:
        names = [
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            name: [tuple(row) for row in con.execute(f'SELECT * FROM "{name}"')]
            for name in names
            if not name.startswith("audit_") and name != "sqlite_sequence"
        }


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_suggest_actual_evidence_budget_and_no_mutation(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        first = (await api.remember("The build uses port 8123."))["result"]["fact_ids"][
            0
        ]
        second = (await api.remember("The build uses port 8123."))["result"][
            "fact_ids"
        ][0]
        before = inventory(instance)
        request = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "fact_ids": [first],
        }
        result = await api.call("suggest", request)
        assert "items" in result, result
        exact = next(
            item for item in result["items"] if item["kind"] == "exact_duplicate"
        )
        assert [fact["fact_id"] for fact in exact["facts"]] == [first, second]
        assert exact["match_basis"] == "exact_body"
        assert all(fact["source_principal_id"] for fact in exact["facts"])
        size = sum(
            len(
                json.dumps(
                    item, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            for item in result["items"]
        )
        assert result["budget_consumed"] == size
        omitted = await api.call("suggest", {**request, "budget": size - 1})
        assert omitted["items"] == [] and omitted["budget_exhausted"]
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "delta",
    [
        {"observation": ""},
        {"observation": "é" * 2049},
        {"limit": True},
        {"limit": 17},
        {"budget": 0},
        {"budget": 65537},
        {"fact_ids": ["bad"]},
        {"principal_id": "private-input"},
        {"trust": "canonical"},
        {"classification": "public"},
        {"promotion": True},
        {"expected_instance_id": "invalid"},
    ],
)
async def test_strict_suggestion_request_refusals(
    tmp_path: Path, transport: str, delta: dict[str, object]
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        result = await Api(http, token, transport).call(
            "suggest",
            {
                "scope": SCOPE,
                "expected_instance_id": str(instance.config.instance_id),
                "observation": "private-input",
                **delta,
            },
        )
        assert result.get("failure", {}).get("code") == "invalid_request", result
        assert "private-input" not in str(result)
        with read_connection(instance.data_path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM audit_events WHERE action_code='memory-suggest'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.anyio
async def test_client_real_asgi_suggest_roundtrip(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token)
        identity = (await api.remember("The build uses port 8123."))["result"][
            "fact_ids"
        ][0]
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        result = await client.suggest(observation="The build uses port 8123.")
        facts = result.items[0]["facts"]
        assert isinstance(facts, tuple) and isinstance(facts[0], Mapping)
        assert facts[0]["fact_id"] == identity
        assert result.items[0]["kind"] == "exact_duplicate"
        assert result.budget_consumed > 0
        assert result.policy is not None
        empty = await client.suggest(observation="The build uses port 8123.", budget=1)
        assert empty.items == () and empty.budget_exhausted


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_recorded_disagreement_and_historical_correction_are_complete(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    author, token = instance.add_actor()
    _, second_token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        left = (await api.remember("Build uses port 8123."))["result"]["fact_ids"][0]
        right = (
            await Api(http, second_token, transport).remember("Build uses port 8124.")
        )["result"]["fact_ids"][0]
        link = await api.call(
            "disagree",
            {
                "scope": SCOPE,
                "left_fact_id": left,
                "right_fact_id": right,
                "classification": "internal",
                "reason": "Separate observed configurations.",
            },
            key=str(uuid4()),
        )
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        observation = await api.call(
            "suggest", {**context, "observation": "Build uses port"}
        )
        item = next(
            item
            for item in observation["items"]
            if item["kind"] == "related_disagreement"
        )
        assert [fact["fact_id"] for fact in item["facts"]] == [left, right]
        assert (
            item["disagreements"][0]["relationship_id"]
            == link["result"]["relationship_id"]
        )
        assert item["disagreements"][0]["principal_id"] == str(author)
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        validated = await client.suggest(observation="Build uses port")
        assert any(item["kind"] == "related_disagreement" for item in validated.items)
        corrected = await api.call(
            "correct",
            {
                "fact_ids": [left],
                "superseded_by": right,
                "reason": "Updated configuration.",
            },
            key=str(uuid4()),
        )
        assert corrected["outcome"] == "committed"
        result = await api.call("suggest", {**context, "fact_ids": [left]})
        correction = next(
            item for item in result["items"] if item["kind"] == "possible_correction"
        )
        assert correction["corrections"][0]["reason"] == "Updated configuration."
        assert [fact["fact_id"] for fact in correction["facts"]] == [left, right]
        assert correction["facts"][0]["invalidated_at"] is not None
        comparison = next(
            item for item in result["items"] if item["kind"] == "possible_duplicate"
        )
        assert comparison["facts"][0]["fact_id"] == left
        assert comparison["facts"][0]["invalidated_at"] is not None
        assert not any(item["kind"] == "exact_duplicate" for item in result["items"])
        validated_history = await client.suggest(fact_ids=(UUID(left),))
        assert any(
            item["kind"] == "possible_correction" for item in validated_history.items
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_hidden_siblings_grant_loss_secret_and_instance_refusal(
    tmp_path: Path, transport: str, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = Instance(tmp_path)
    owner, token = instance.add_actor()
    _, sibling_token = instance.add_actor(
        segments=[{"kind": "repository", "identifier": "sibling"}]
    )
    async with serve(instance) as http:
        api = Api(http, token, transport)
        visible = (await api.remember("Build uses port 8123."))["result"]["fact_ids"][0]
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        body = {**context, "observation": "Build uses port 8123."}
        before = await api.call("suggest", body)
        hidden = (
            await Api(http, sibling_token, transport).remember(
                "Build uses port 8123.",
                scope={
                    "realm": "acme",
                    "segments": [{"kind": "repository", "identifier": "sibling"}],
                },
            )
        )["result"]["fact_ids"][0]
        after = await api.call("suggest", body)
        assert after == before and hidden not in str(after)
        batch = await api.call(
            "suggest", {**context, "fact_ids": [visible, hidden], "budget": 1}
        )
        assert batch["failure"]["code"] == "authorisation_denied"
        assert hidden not in str(batch) and visible not in str(batch)
        wrong = await api.call(
            "suggest", {**body, "expected_instance_id": str(uuid4())}
        )
        assert wrong["failure"]["code"] == "authorisation_denied"
        secret = await api.call(
            "suggest", {**body, "observation": "AKIAIOSFODNN7EXAMPLE"}
        )
        assert secret["failure"]["code"] == "secret_rejected"
        assert "AKIAIOSFODNN7EXAMPLE" not in str(secret)
        keyed = await api.call("suggest", body, key=str(uuid4()))
        assert keyed["failure"]["code"] == "invalid_request"
        with _open_write_connection(instance.data_path, create=False) as con:
            con.execute(
                "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                (canonical_timestamp(instance.clock()), str(owner)),
            )
            con.commit()
        denied = await api.call("suggest", body)
        assert denied["failure"]["code"] == "authorisation_denied"
        assert visible not in str(denied)
    logs = capsys.readouterr().err
    assert (
        "Build uses port" not in logs
        and token not in logs
        and "AKIAIOSFODNN7EXAMPLE" not in logs
    )
    assert '"operation": "memory_suggest"' in logs


@pytest.mark.anyio
async def test_suggestion_transport_parity_and_strict_read_annotation(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token)
        await api.remember("Build uses port 8123.")
        body = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "observation": "Build uses port 8123.",
        }
        assert await api.call("suggest", body) == await Api(http, token, "mcp").call(
            "suggest", body
        )
        response = await http.post(
            "/memory/v1/mcp",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        tool = next(
            tool
            for tool in response.json()["result"]["tools"]
            if tool["name"] == "suggest"
        )
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["additionalProperties"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [b'{"scope":{},"scope":{}}', b'{"observation":"private-input"', b" " * 1048577],
)
async def test_wire_body_refusal_is_audited_without_content(
    tmp_path: Path, raw: bytes
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        response = await http.post(
            "/memory/v1/suggest",
            content=raw,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code in {400, 413}
        assert "private-input" not in response.text
        with read_connection(instance.data_path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM audit_events WHERE action_code='memory-suggest'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_controlled_index_candidates_and_revocation_during_read(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()

    class ControlledIndex(MemoryIndex):
        identities: tuple[UUID, ...] = ()
        revoke = False

        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            assert query == "motorcar" and limit == 256 and partition_keys
            if self.revoke:
                with _open_write_connection(instance.data_path, create=False) as con:
                    con.execute(
                        "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                        (canonical_timestamp(instance.clock()), str(principal)),
                    )
                    con.commit()
            return self.identities

    index = ControlledIndex()
    app = build_application(instance.config, clock=instance.clock, index_adapter=index)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http,
    ):
        api = Api(http, token, transport)
        identity = (await api.remember("automobile engine"))["result"]["fact_ids"][0]
        index.identities = (UUID(identity),)
        body = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "observation": "motorcar",
        }
        found = await api.call("suggest", body)
        assert found["items"][0]["kind"] == "possible_duplicate"
        assert found["items"][0]["match_basis"] == "retrieval_candidate"
        assert found["semantic_degraded"] is False
        index.revoke = True
        refused = await api.call("suggest", body)
        assert refused["failure"]["code"] == "authorisation_denied"
        assert identity not in str(refused) and "automobile" not in str(refused)


@pytest.mark.anyio
async def test_old_current_ancestor_and_no_recall_policy_are_accepted(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token)
        root_scope = {"realm": "acme", "segments": []}
        old = (await api.remember("The port is 8123.", scope=root_scope))["result"][
            "fact_ids"
        ][0]
        large = (await api.remember("z" * 8192))["result"]["fact_ids"][0]
        instance.clock.now += timedelta(days=100)
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        found = await client.suggest(observation="The port is 8123.")
        assert found.items[0]["kind"] == "exact_duplicate"
        assert old in str(found.items)
        omitted = await client.suggest(fact_ids=(UUID(large),))
        assert omitted.items == () and omitted.policy is None
        assert omitted.budget_exhausted and not omitted.semantic_degraded
