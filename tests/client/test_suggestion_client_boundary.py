"""Hostile suggestion servers cannot turn evidence into trusted or mutable data."""

import copy
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cairn.catalogue.audit import Classification, Scope
from cairn.client.errors import MemoryOperationFailure
from cairn.client.memory import MemoryClient

INSTANCE = UUID("11111111-1111-4111-8111-111111111111")
FACT = "22222222-2222-4222-8222-222222222222"
AUTHOR = "33333333-3333-4333-8333-333333333333"
BODY = "The build uses port 8123."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def diagnosis() -> dict[str, Any]:
    return {
        "instance_id": str(INSTANCE),
        "principal_id": AUTHOR,
        "principal_kind": "workload",
        "product_version": "0.1.0",
        "contract_identity": "cairn.memory/v1",
        "contract_digest": "a" * 64,
        "mcp_contract_digest": "b" * 64,
        "scope": {"realm": "acme", "segments": []},
        "classification": "internal",
        "permissions": {
            "retrieve": True,
            "ingest": False,
            "promote": False,
            "invalidate": False,
        },
        "evaluated_at": "2026-09-09T12:00:00.000000Z",
        "permission_basis": "current_grants_only",
    }


def packet() -> dict[str, Any]:
    fact = {
        "fact_id": FACT,
        "body": BODY,
        "scope": {"realm": "acme", "segments": []},
        "classification": "restricted",
        "trust": "candidate",
        "assertion_id": AUTHOR,
        "derived_from": None,
        "promoted_by": None,
        "evidence_id": None,
        "valid_from": None,
        "valid_to": None,
        "recorded_at": "2026-09-09T12:00:00.000000Z",
        "invalidated_at": None,
        "source_principal_id": AUTHOR,
        "source_type": "agent-claim",
        "relevance_score": 1.0,
        "has_disagreement": False,
        "disagreement_context_incomplete": False,
    }
    return account(
        {
            "items": [
                {
                    "kind": "exact_duplicate",
                    "facts": [fact],
                    "reason": "The current fact body is byte-identical; attribution and validity still matter.",
                    "match_basis": "exact_body",
                    "corrections": [],
                    "disagreements": [],
                }
            ],
            "budget_consumed": 0,
            "budget_exhausted": False,
            "semantic_degraded": True,
            "policy": "lexical-age/v1: distinct token overlap + 0.5 semantic membership + 0.25/(1+age_days/30); semantic limit 256; relevant_only: score > 0.25; no age cutoff or reinforcement",
        }
    )


def account(value: dict[str, Any]) -> dict[str, Any]:
    value["budget_consumed"] = sum(
        len(
            json.dumps(
                item, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        for item in value["items"]
    )
    return value


def client(http: httpx.AsyncClient, *, bound: bool = True) -> MemoryClient:
    return MemoryClient(
        http,
        scope=Scope("acme", ()),
        classification=Classification.INTERNAL,
        expected_instance_id=INSTANCE if bound else None,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "scope",
        "basis",
        "body",
        "timestamp",
        "boolean_score",
        "infinite_score",
        "missing_field",
        "extra_field",
        "invalidated",
        "duplicate_fact",
        "count",
        "bytes",
        "bool_budget",
        "policy",
        "correction",
        "root",
        "historical_observation",
    ],
)
async def test_adversarial_suggestion_evidence_is_refused(case: str) -> None:
    value = packet()
    item = value["items"][0]
    fact = item["facts"][0]
    if case == "scope":
        fact["scope"]["realm"] = "sibling"
    elif case == "basis":
        item["match_basis"] = "recorded_correction"
    elif case == "body":
        fact["body"] = "different"
    elif case == "timestamp":
        fact["recorded_at"] = "yesterday"
    elif case == "boolean_score":
        fact["relevance_score"] = True
    elif case == "infinite_score":
        fact["relevance_score"] = float("inf")
    elif case == "missing_field":
        del fact["source_type"]
    elif case == "extra_field":
        item["automatic_action"] = "remember"
    elif case == "invalidated":
        fact["invalidated_at"] = fact["recorded_at"]
    elif case == "duplicate_fact":
        item["facts"].append(dict(fact))
    elif case == "count":
        value["items"] *= 9
    elif case == "policy":
        value["policy"] = None
    elif case == "correction":
        item.update(kind="possible_correction", match_basis="recorded_correction")
    elif case == "historical_observation":
        item.update(
            kind="possible_duplicate",
            match_basis="retrieval_candidate",
            reason="Selected evidence is historical or not currently valid; candidate equivalence is unverified.",
        )
    account(value)
    if case == "bytes":
        value["budget_consumed"] += 1
    elif case == "bool_budget":
        value["budget_consumed"] = True
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            content=json.dumps(
                diagnosis() if request.url.path.endswith("diagnose") else value
            ).encode(),
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            if case == "root":
                await client(http).suggest(fact_ids=(INSTANCE,))
            else:
                await client(http).suggest(observation=BODY)
        assert caught.value.failure.code == "invalid_response"
        assert BODY not in str(caught.value)
    assert calls == ["/memory/v1/diagnose", "/memory/v1/suggest"]


@pytest.mark.anyio
@pytest.mark.parametrize("root_state", ["invalidated", "current", "temporal"])
@pytest.mark.parametrize("historical_reason", [False, True])
async def test_selected_root_invalidation_requires_historical_reason(
    root_state: str, historical_reason: bool
) -> None:
    value = packet()
    item = value["items"][0]
    root = item["facts"][0]
    candidate = copy.deepcopy(root)
    candidate["fact_id"] = str(INSTANCE)
    item["facts"].append(candidate)
    if root_state == "invalidated":
        root["invalidated_at"] = root["recorded_at"]
    elif root_state == "temporal":
        root["valid_to"] = root["recorded_at"]
    reason = (
        "Selected evidence is historical or not currently valid; candidate equivalence is unverified."
        if historical_reason
        else "Retrieval found a related candidate; equivalence is unverified."
    )
    item.update(
        kind="possible_duplicate", match_basis="retrieval_candidate", reason=reason
    )
    account(value)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else value
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        if root_state == "invalidated" and not historical_reason:
            with pytest.raises(MemoryOperationFailure) as caught:
                await client(http).suggest(fact_ids=(UUID(FACT),))
            assert caught.value.failure.code == "invalid_response"
        else:
            result = await client(http).suggest(fact_ids=(UUID(FACT),))
            assert result.items[0]["reason"] == reason


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"observation": ""},
        {"observation": "é" * 2049},
        {"observation": BODY, "limit": True},
        {"observation": BODY, "budget": float("inf")},
        {"observation": BODY, "fact_ids": (INSTANCE,)},
        {"fact_ids": [INSTANCE]},
        {"fact_ids": (UUID(int=0),)},
        {"fact_ids": (INSTANCE, INSTANCE)},
    ],
)
async def test_invalid_input_never_reaches_http(kwargs: dict[str, Any]) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid suggestion reached HTTP")

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(unexpected)
    ) as http:
        with pytest.raises(MemoryOperationFailure):
            await client(http).suggest(**kwargs)


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(110):
            yield b" " * 4096


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "compressed",
        "stream",
        "length",
        "length_negative",
        "length_mismatch",
        "duplicate_keys",
        "redirect",
        "error_large",
        "error_secret",
    ],
)
async def test_response_caps_and_safe_failures(case: str) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("diagnose"):
            return httpx.Response(200, json=diagnosis())
        if case == "compressed":
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=b""
            )
        if case == "stream":
            return httpx.Response(200, stream=Chunks())
        if case == "length":
            return httpx.Response(
                200, headers={"Content-Length": "9999999"}, content=b"{}"
            )
        if case == "length_negative":
            return httpx.Response(200, headers={"Content-Length": "-1"}, content=b"{}")
        if case == "length_mismatch":
            return httpx.Response(200, headers={"Content-Length": "1"}, content=b"{}")
        if case == "duplicate_keys":
            return httpx.Response(200, content=b'{"items":[],"items":[]}')
        if case == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.invalid"})
        if case == "error_large":
            return httpx.Response(403, content=b"x" * 16385)
        return httpx.Response(
            403,
            json={
                "failure": {
                    "code": "authorisation_denied",
                    "message": "private-server-body",
                    "retry": "never",
                    "correlation_id": AUTHOR,
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
    ) as http:
        with pytest.raises(MemoryOperationFailure) as caught:
            await client(http).suggest(observation=BODY, budget=65536)
        assert "private-server-body" not in str(caught.value)
    assert calls == ["/memory/v1/diagnose", "/memory/v1/suggest"]


@pytest.mark.anyio
async def test_output_detached_frozen_and_no_followup_write() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet()
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        result = await client(http).suggest(observation=BODY)
        with pytest.raises(TypeError):
            result.items[0]["kind"] = "trusted"  # type: ignore[index]
    assert calls == ["/memory/v1/diagnose", "/memory/v1/suggest"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["unbound", "wrong_instance", "no_read", "changed_url"]
)
async def test_binding_and_read_diagnosis_precede_content(case: str) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert BODY not in request.content.decode()
        assert request.url.path == "/memory/v1/diagnose"
        value = diagnosis()
        if case == "wrong_instance":
            value["instance_id"] = AUTHOR
        if case == "no_read":
            value["permissions"].update(retrieve=False, ingest=True)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        memory = client(http, bound=case != "unbound")
        if case == "changed_url":
            http.base_url = "https://foreign.invalid"
        with pytest.raises(MemoryOperationFailure):
            await memory.suggest(observation=BODY)
    assert calls == (
        [] if case in {"unbound", "changed_url"} else ["/memory/v1/diagnose"]
    )


@pytest.mark.anyio
async def test_full_budget_unicode_escaping_and_readonly_request() -> None:
    value = packet()
    body = "é" * 2048
    value["items"][0]["facts"][0]["body"] = body
    value["items"] = [copy.deepcopy(value["items"][0]) for _ in range(12)]
    for item in value["items"]:
        item["facts"][0]["fact_id"] = str(uuid4())
    account(value)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("diagnose"):
            return httpx.Response(200, json=diagnosis())
        actual = json.loads(request.content)
        assert actual == {
            "scope": {"realm": "acme", "segments": []},
            "expected_instance_id": str(INSTANCE),
            "observation": body,
            "fact_ids": [],
            "budget": 65536,
            "limit": 16,
        }
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(
            200, content=json.dumps(value, ensure_ascii=True).encode()
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        result = await client(http).suggest(observation=body, budget=65536, limit=16)
    assert result.budget_consumed == value["budget_consumed"]
    assert 50000 < result.budget_consumed <= 65536


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["possible_correction", "related_disagreement"])
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "truncated",
        "foreign_endpoint",
        "reversed",
        "bad_time",
        "bad_principal",
        "missing_endpoint",
    ],
)
async def test_complete_attributed_relationship_records(
    kind: str, damage: str | None
) -> None:
    value = packet()
    item = value["items"][0]
    right = copy.deepcopy(item["facts"][0])
    right["fact_id"] = str(INSTANCE)
    right["body"] = "Updated build settings."
    item["facts"].append(right)
    item["kind"] = kind
    record: dict[str, Any]
    if kind == "possible_correction":
        item["match_basis"] = "recorded_correction"
        item["reason"] = (
            "Recorded correction history; inspect both attributed claims before acting."
        )
        item["facts"][0]["invalidated_at"] = "2026-09-09T12:00:00.000000Z"
        record = {
            "fact_id": FACT,
            "superseded_by": str(INSTANCE),
            "principal_id": AUTHOR,
            "reason": "Updated configuration.",
            "invalidated_at": "2026-09-09T12:00:00.000000Z",
        }
        item["corrections"] = [record]
        endpoint, timestamp = "superseded_by", "invalidated_at"
    else:
        item["match_basis"] = "recorded_disagreement"
        item["reason"] = (
            "An attributed disagreement is recorded; no claim is automatically preferred."
        )
        for fact in item["facts"]:
            fact["has_disagreement"] = True
        record = {
            "relationship_id": AUTHOR,
            "scope": {"realm": "acme", "segments": []},
            "left_fact_id": FACT,
            "right_fact_id": str(INSTANCE),
            "classification": "restricted",
            "principal_id": AUTHOR,
            "reason": "Different settings.",
            "recorded_at": "2026-09-09T12:00:00.000000Z",
        }
        item["disagreements"] = [record]
        endpoint, timestamp = "right_fact_id", "recorded_at"
    if damage == "truncated":
        del record["reason"]
    elif damage == "foreign_endpoint":
        record[endpoint] = AUTHOR
    elif damage == "reversed":
        item["facts"].reverse()
    elif damage == "bad_time":
        record[timestamp] = "yesterday"
    elif damage == "bad_principal":
        record["principal_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    elif damage == "missing_endpoint":
        item["facts"].pop()
    account(value)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else value
        )

    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(respond)
    ) as http:
        if damage is None:
            result = await client(http).suggest(fact_ids=(UUID(FACT),))
            assert result.items[0]["kind"] == kind
        else:
            with pytest.raises(MemoryOperationFailure):
                await client(http).suggest(fact_ids=(UUID(FACT),))
