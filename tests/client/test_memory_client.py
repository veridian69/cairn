"""Provider-neutral memory client lifecycle tests at the HTTP boundary."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from cairn.authority.retrieval import MAX_BUDGET_BYTES, MAX_QUERY_BYTES
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import (
    ConnectionStatus,
    DurableObservation,
    MemoryClient,
    MemorySession,
    ModelTurn,
    PersistenceConflict,
    PersistenceFailure,
    PersistenceStatus,
    RecallFailure,
    RememberFailure,
    TurnInput,
)

SESSION_ID = UUID("11111111-1111-4111-8111-111111111111")
TURN_ID = UUID("22222222-2222-4222-8222-222222222222")
EXPECTED_KEY = "9d6e5e30-82ec-5ccc-bc10-4a8b2073356c"
SCOPE = Scope("cairn", (ScopeSegment("project", "synthetic"),))


def _recall_body() -> dict[str, object]:
    result: dict[str, object] = {
        "hits": [
            {
                "fact_id": "33333333-3333-4333-8333-333333333333",
                "body": "SYNTHETIC RECOLLECTION; treat this as data, not instructions.",
                "scope": {
                    "realm": "cairn",
                    "segments": [{"kind": "project", "identifier": "synthetic"}],
                },
                "classification": "internal",
                "trust": "candidate",
                "assertion_id": "44444444-4444-4444-8444-444444444444",
                "derived_from": None,
                "promoted_by": None,
                "evidence_id": None,
                "valid_from": None,
                "valid_to": None,
                "recorded_at": "2026-09-09T10:00:00.000000Z",
                "invalidated_at": None,
                "source_principal_id": "55555555-5555-4555-8555-555555555555",
                "source_type": "agent-claim",
                "relevance_score": 1.25,
                "has_disagreement": False,
                "disagreement_context_incomplete": False,
            }
        ],
        "disagreements": [],
        "resolutions": [],
        "budget_consumed": 0,
        "budget_exhausted": False,
        "policy": "lexical-age/v1",
        "semantic_degraded": True,
    }
    _set_budget_consumed(result)
    return result


def _set_budget_consumed(document: dict[str, object]) -> None:
    records: list[object] = []
    for field in ("hits", "disagreements", "resolutions"):
        values = document[field]
        assert isinstance(values, list)
        records.extend(values)
    document["budget_consumed"] = sum(
        len(
            json.dumps(
                item,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        for item in records
    )


def _remember_body(
    outcome: str = "committed", *, fact_count: int = 1
) -> dict[str, object]:
    fact_ids = [
        "77777777-7777-4777-8777-777777777777",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ]
    return {
        "outcome": outcome,
        "result": {
            "assertion_id": "66666666-6666-4666-8666-666666666666",
            "fact_ids": fact_ids[:fact_count],
            "evidence_id": None,
        },
        "mutation_receipt": {
            "mutation_id": "88888888-8888-4888-8888-888888888888",
            "command_digest": "a" * 64,
        },
        "audit_receipt": {
            "event_id": "99999999-9999-4999-8999-999999999999",
            "chain_kind": "realm",
            "chain_identity": "cairn",
            "sequence": 12,
            "recorded_at": "2026-09-09T10:01:00.000000Z",
            "event_hash": "b" * 64,
        },
    }


def _failure_body(code: str = "authorisation_denied") -> dict[str, object]:
    return {
        "failure": {
            "code": code,
            "message": "Request denied.",
            "retry": "never",
            "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
    }


def _client(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(
        http,
        scope=SCOPE,
        classification=Classification.INTERNAL,
    )


@pytest.mark.anyio
async def test_run_turn_recalls_before_callback_then_batches_host_owned_remember() -> (
    None
):
    """Changing order, widening host values, or splitting the write breaks this."""
    events: list[str] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        events.append(request.url.path)
        if request.url.path == "/memory/v1/recall":
            return httpx.Response(200, json=_recall_body())
        return httpx.Response(200, json=_remember_body(fact_count=2))

    async def model(turn_input: TurnInput) -> ModelTurn:
        events.append("callback")
        assert turn_input.user_input == "What do we know?"
        assert turn_input.recalled.source == "cairn-memory/v1"
        assert turn_input.recalled.content_role == "untrusted-data"
        assert turn_input.recalled.data["semantic_degraded"] is True
        hits = turn_input.recalled.data["hits"]
        assert isinstance(hits, tuple)
        first_hit = hits[0]
        assert isinstance(first_hit, Mapping)
        assert first_hit["trust"] == "candidate"
        assert first_hit["source_principal_id"] == (
            "55555555-5555-4555-8555-555555555555"
        )
        with pytest.raises(TypeError):
            turn_input.recalled.data["policy"] = "changed"  # type: ignore[index]
        with pytest.raises(TypeError):
            first_hit["trust"] = "validated"  # type: ignore[index]
        observed_at = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)
        return ModelTurn(
            response="A completed synthetic response.",
            observations=(
                DurableObservation("First durable fact", observed_at=observed_at),
                DurableObservation(
                    "Second durable fact",
                    valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                    valid_to=datetime(2026, 10, 1, tzinfo=UTC),
                    observed_at=observed_at,
                ),
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://cairn.invalid",
        headers={"Authorization": "Bearer SYNTHETIC-CREDENTIAL"},
        follow_redirects=True,
    ) as http:
        result = await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
            "What do we know?", model, turn_id=TURN_ID, budget=4096
        )

    assert events == ["/memory/v1/recall", "callback", "/memory/v1/remember"]
    assert result.response == "A completed synthetic response."
    assert result.persistence.status is PersistenceStatus.COMMITTED
    assert str(result.persistence.idempotency_key) == EXPECTED_KEY
    assert len(requests) == 2
    recall = json.loads(requests[0].content)
    assert recall == {
        "scope": {
            "realm": "cairn",
            "segments": [{"kind": "project", "identifier": "synthetic"}],
        },
        "query": "What do we know?",
        "budget": 4096,
    }
    remember = json.loads(requests[1].content)
    assert remember == {
        "scope": recall["scope"],
        "classification": "internal",
        "facts": [
            {
                "body": "First durable fact",
                "valid_from": None,
                "valid_to": None,
            },
            {
                "body": "Second durable fact",
                "valid_from": "2026-09-01T00:00:00.000000Z",
                "valid_to": "2026-10-01T00:00:00.000000Z",
            },
        ],
        "observed_at": "2026-09-09T10:00:00.000000Z",
    }
    assert "source_type" not in remember
    assert "requested_trust" not in remember
    assert requests[1].headers["Idempotency-Key"] == EXPECTED_KEY
    assert requests[1].headers["Authorization"] == "Bearer SYNTHETIC-CREDENTIAL"


@pytest.mark.anyio
async def test_empty_observation_selection_skips_remember() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_recall_body())

    async def model(_turn_input: TurnInput) -> ModelTurn:
        return ModelTurn(response="Nothing durable.")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        result = await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
            "Ephemeral request", model, turn_id=TURN_ID
        )

    assert paths == ["/memory/v1/recall"]
    assert result.persistence.status is PersistenceStatus.SKIPPED
    assert result.persistence.idempotency_key is None


@pytest.mark.anyio
async def test_callback_failure_prevents_remember() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_recall_body())

    async def model(_turn_input: TurnInput) -> ModelTurn:
        raise RuntimeError("synthetic provider failure")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RuntimeError, match="synthetic provider failure"):
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "query", model, turn_id=TURN_ID
            )

    assert paths == ["/memory/v1/recall"]


@pytest.mark.anyio
async def test_recall_denial_fails_before_callback_with_safe_metadata() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_failure_body())

    async def model(_turn_input: TurnInput) -> ModelTurn:
        nonlocal called
        called = True
        return ModelTurn("must not happen")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://cairn.invalid",
        headers={"Authorization": "Bearer NEVER-LEAK-THIS"},
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "PRIVATE QUERY TEXT", model, turn_id=TURN_ID
            )

    assert called is False
    assert caught.value.failure.code == "authorisation_denied"
    assert caught.value.failure.status_code == 403
    assert "PRIVATE QUERY TEXT" not in repr(caught.value)
    assert "NEVER-LEAK-THIS" not in repr(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["network", "malformed"])
async def test_recall_transport_or_malformed_success_never_claims_memory(
    mode: str,
) -> None:
    called = False

    def respond(request: httpx.Request) -> httpx.Response:
        if mode == "network":
            raise httpx.ConnectError("synthetic connection failure", request=request)
        return httpx.Response(200, json={"hits": []})

    async def model(_turn_input: TurnInput) -> ModelTurn:
        nonlocal called
        called = True
        return ModelTurn("must not happen")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "query", model, turn_id=TURN_ID
            )

    assert called is False
    assert caught.value.failure.code in {"transport_error", "invalid_response"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "override",
    [
        {"hits": [{}]},
        {"disagreements": [{}]},
        {"budget_consumed": -1},
        {"semantic_degraded": {"wrong": "type"}},
    ],
)
async def test_malformed_nested_recall_packet_is_rejected(
    override: dict[str, object],
) -> None:
    malformed = {**_recall_body(), **override}

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fact_id", "not-a-uuid"),
        ("relevance_score", float("nan")),
        ("source_type", None),
        ("has_disagreement", "yes"),
        ("disagreement_context_incomplete", True),
    ],
)
async def test_malformed_recalled_fact_values_are_rejected(
    field: str, value: object
) -> None:
    malformed = _recall_body()
    hits = malformed["hits"]
    assert isinstance(hits, list)
    hit = hits[0]
    assert isinstance(hit, dict)
    hit[field] = value

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(malformed, allow_nan=True).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_incomplete_disagreement_context_is_valid_when_explicitly_flagged() -> (
    None
):
    response = _recall_body()
    hits = response["hits"]
    assert isinstance(hits, list)
    hit = hits[0]
    assert isinstance(hit, dict)
    hit["has_disagreement"] = True
    hit["disagreement_context_incomplete"] = True
    _set_budget_consumed(response)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        recalled = await _client(http).recall("query")

    recalled_hits = recalled.data["hits"]
    assert isinstance(recalled_hits, tuple)
    recalled_hit = recalled_hits[0]
    assert isinstance(recalled_hit, Mapping)
    assert recalled_hit["has_disagreement"] is True
    assert recalled_hit["disagreement_context_incomplete"] is True


@pytest.mark.anyio
async def test_recall_rejects_budget_counter_that_omits_disclosed_records() -> None:
    response = _recall_body()
    response["budget_consumed"] = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_recall_rejects_unbounded_integer_score_as_invalid_response() -> None:
    response = _recall_body()
    hits = response["hits"]
    assert isinstance(hits, list)
    hit = hits[0]
    assert isinstance(hit, dict)
    hit["relevance_score"] = 10**400
    _set_budget_consumed(response)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_resolution_must_select_an_endpoint_of_its_disagreement() -> None:
    response = _recall_body()
    hits = response["hits"]
    assert isinstance(hits, list)
    first = hits[0]
    assert isinstance(first, dict)
    first["has_disagreement"] = True
    second = json.loads(json.dumps(first))
    second["fact_id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    third = json.loads(json.dumps(first))
    third["fact_id"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    third["has_disagreement"] = False
    hits.extend((second, third))
    response["disagreements"] = [
        {
            "relationship_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "scope": first["scope"],
            "left_fact_id": first["fact_id"],
            "right_fact_id": second["fact_id"],
            "classification": "internal",
            "principal_id": "55555555-5555-4555-8555-555555555555",
            "reason": "Synthetic conflict",
            "recorded_at": "2026-09-09T10:00:00.000000Z",
        }
    ]
    response["resolutions"] = [
        {
            "relationship_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "scope": first["scope"],
            "disagreement_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "evidence_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "selected_fact_id": third["fact_id"],
            "classification": "internal",
            "principal_id": "55555555-5555-4555-8555-555555555555",
            "reason": "Synthetic resolution",
            "recorded_at": "2026-09-09T10:00:00.000000Z",
        }
    ]
    _set_budget_consumed(response)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "budget", "code"),
    [
        ("", 1, "invalid_query"),
        ("q" * (MAX_QUERY_BYTES + 1), 1, "invalid_query"),
        ("query", 0, "invalid_budget"),
        ("query", MAX_BUDGET_BYTES + 1, "invalid_budget"),
    ],
)
async def test_direct_recall_rejects_invalid_inputs_before_http(
    query: str, budget: int, code: str
) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_recall_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall(query, budget=budget)

    assert caught.value.failure.code == code
    assert calls == 0


@pytest.mark.anyio
async def test_recall_relevance_is_explicit_strict_opt_in() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_recall_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        memory = _client(http)
        await memory.recall("query")
        await memory.recall("query", relevant_only=True)
        with pytest.raises(RecallFailure):
            await memory.recall("query", relevant_only=1)  # type: ignore[arg-type]
    assert "relevant_only" not in json.loads(requests[0].content)
    assert json.loads(requests[1].content)["relevant_only"] is True
    assert len(requests) == 2


def _diagnostic_body() -> dict[str, object]:
    return {
        "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "product_version": "0.1.0",
        "contract_identity": "cairn.memory/v1",
        "contract_digest": "b" * 64,
        "mcp_contract_digest": "c" * 64,
        "principal_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "principal_kind": "workload",
        "scope": {
            "realm": "cairn",
            "segments": [{"kind": "project", "identifier": "synthetic"}],
        },
        "classification": "internal",
        "permissions": {
            "retrieve": True,
            "ingest": True,
            "promote": False,
            "invalidate": False,
        },
        "evaluated_at": "2026-09-09T12:00:00.000000Z",
        "permission_basis": "current_grants_only",
    }


@pytest.mark.anyio
async def test_diagnose_checks_authenticated_instance_contract_and_expected_identity() -> (
    None
):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_diagnostic_body(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://cairn.invalid",
        headers={"Authorization": "Bearer NEVER-LEAK-THIS"},
    ) as http:
        result = await _client(http).diagnose(
            expected_instance_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            expected_contract_digest="b" * 64,
            expected_mcp_contract_digest="c" * 64,
        )

    assert result.status is ConnectionStatus.READY
    assert result.instance_id == UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert result.contract_identity == "cairn.memory/v1"
    assert result.product_version == "0.1.0"
    assert result.failure is None
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/memory/v1/diagnose"
    assert requests[0].headers["Authorization"] == "Bearer NEVER-LEAK-THIS"


@pytest.mark.anyio
async def test_diagnose_reports_instance_mismatch_without_exposing_credentials() -> (
    None
):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_diagnostic_body(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://cairn.invalid",
        headers={"Authorization": "Bearer SECRET-CREDENTIAL"},
    ) as http:
        result = await _client(http).diagnose(
            expected_instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        )

    assert result.status is ConnectionStatus.INSTANCE_MISMATCH
    assert result.failure is not None
    assert result.failure.code == "instance_mismatch"
    assert "SECRET-CREDENTIAL" not in repr(result)


@pytest.mark.anyio
async def test_diagnose_reports_authentication_and_transport_failures_safely() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic failure", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        result = await _client(http).diagnose()

    assert result.status is ConnectionStatus.UNREACHABLE
    assert result.failure is not None
    assert result.failure.code == "transport_error"


@pytest.mark.anyio
async def test_diagnose_reports_contract_mismatch_and_invalid_instance_documents() -> (
    None
):
    responses = iter(
        [
            _diagnostic_body(),
            {"instance_id": "not-an-instance"},
        ]
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        client = _client(http)
        incompatible = await client.diagnose(expected_contract_digest="d" * 64)
        malformed = await client.diagnose()

    assert incompatible.status is ConnectionStatus.INCOMPATIBLE
    assert incompatible.failure is not None
    assert incompatible.failure.code == "contract_mismatch"
    assert malformed.status is ConnectionStatus.INVALID_RESPONSE
    assert malformed.failure is not None
    assert malformed.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_authenticated_requests_do_not_follow_redirects() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(307, headers={"Location": "https://other.invalid/leak"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://cairn.invalid",
        follow_redirects=True,
    ) as http:
        with pytest.raises(RecallFailure) as caught:
            await _client(http).recall("query")

    assert paths == ["/memory/v1/recall"]
    assert caught.value.failure.status_code == 307


@pytest.mark.anyio
async def test_plain_http_is_limited_to_numeric_loopback() -> None:
    async with httpx.AsyncClient(base_url="http://cairn.invalid") as remote:
        with pytest.raises(ValueError, match="requires TLS"):
            _client(remote)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as loopback:
        assert _client(loopback).scope == SCOPE


@pytest.mark.anyio
async def test_persistence_failure_preserves_exact_turn_for_one_explicit_retry() -> (
    None
):
    callback_calls = 0
    remember_requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/memory/v1/recall":
            return httpx.Response(200, json=_recall_body())
        remember_requests.append(request)
        if len(remember_requests) == 1:
            raise httpx.ConnectError("synthetic lost response", request=request)
        return httpx.Response(200, json=_remember_body("replayed"))

    async def model(_turn_input: TurnInput) -> ModelTurn:
        nonlocal callback_calls
        callback_calls += 1
        return ModelTurn(
            response="The useful response survives.",
            observations=(DurableObservation("Exact durable body"),),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(PersistenceFailure) as caught:
            await session.run_turn("query", model, turn_id=TURN_ID)

        failure = caught.value
        assert failure.response == "The useful response survives."
        assert failure.observations == (DurableObservation("Exact durable body"),)
        assert str(failure.idempotency_key) == EXPECTED_KEY
        retried = await session.retry_persistence(failure)

    assert callback_calls == 1
    assert retried.response == "The useful response survives."
    assert retried.persistence.status is PersistenceStatus.REPLAYED
    assert len(remember_requests) == 2
    assert remember_requests[0].content == remember_requests[1].content
    assert (
        remember_requests[0].headers["Idempotency-Key"]
        == remember_requests[1].headers["Idempotency-Key"]
        == EXPECTED_KEY
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (403, _failure_body(), "authorisation_denied"),
        (200, {"outcome": "committed", "result": {}}, "invalid_response"),
    ],
)
async def test_unsuccessful_or_malformed_remember_is_never_reported_as_persisted(
    status: int,
    body: dict[str, object],
    expected_code: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/memory/v1/recall":
            return httpx.Response(200, json=_recall_body())
        return httpx.Response(status, json=body)

    async def model(_turn_input: TurnInput) -> ModelTurn:
        return ModelTurn("Completed", (DurableObservation("Durable"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(PersistenceFailure) as caught:
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "query", model, turn_id=TURN_ID
            )

    assert caught.value.response == "Completed"
    assert caught.value.failure.code == expected_code


@pytest.mark.anyio
async def test_invalid_remember_receipt_invariants_are_rejected() -> None:
    malformed = _remember_body()
    result = malformed["result"]
    mutation = malformed["mutation_receipt"]
    audit = malformed["audit_receipt"]
    assert isinstance(result, dict)
    assert isinstance(mutation, dict)
    assert isinstance(audit, dict)
    result.update(assertion_id="not-a-uuid", fact_ids=[])
    mutation.update(mutation_id="wrong", command_digest="wrong")
    audit.update(
        event_id="wrong",
        chain_kind="wrong",
        sequence=-1,
        recorded_at="wrong",
        event_hash="wrong",
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RememberFailure) as caught:
            await _client(http).remember(
                (DurableObservation("required fact"),), idempotency_key=TURN_ID
            )

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("result", "assertion_id", "not-a-uuid"),
        ("result", "fact_ids", []),
        ("result", "evidence_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("mutation_receipt", "mutation_id", "not-a-uuid"),
        ("mutation_receipt", "command_digest", "ABC"),
        ("audit_receipt", "event_id", "not-a-uuid"),
        ("audit_receipt", "chain_kind", "instance"),
        ("audit_receipt", "chain_identity", "another-realm"),
        ("audit_receipt", "sequence", 0),
        ("audit_receipt", "recorded_at", "not-a-timestamp"),
        ("audit_receipt", "event_hash", "xyz"),
    ],
)
async def test_each_malformed_remember_receipt_value_is_rejected(
    section: str, field: str, value: object
) -> None:
    malformed = _remember_body()
    target = malformed[section]
    assert isinstance(target, dict)
    target[field] = value

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(RememberFailure) as caught:
            await _client(http).remember(
                (DurableObservation("required fact"),), idempotency_key=TURN_ID
            )

    assert caught.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_invalid_batch_preserves_the_completed_response_without_writing() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_recall_body())

    async def model(_turn_input: TurnInput) -> ModelTurn:
        return ModelTurn(
            "Completed before selection failed.",
            (
                DurableObservation(
                    "first", observed_at=datetime(2026, 9, 9, tzinfo=UTC)
                ),
                DurableObservation(
                    "second", observed_at=datetime(2026, 9, 10, tzinfo=UTC)
                ),
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(PersistenceFailure) as caught:
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "query", model, turn_id=TURN_ID
            )

    assert paths == ["/memory/v1/recall"]
    assert caught.value.response == "Completed before selection failed."
    assert caught.value.failure.code == "invalid_observations"
    assert caught.value.failure.retry == "never"


@pytest.mark.anyio
async def test_reusing_turn_id_with_changed_output_is_a_visible_local_conflict() -> (
    None
):
    remember_count = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal remember_count
        remember_count += 1
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        await session.persist_turn(
            TURN_ID, ModelTurn("first", (DurableObservation("first fact"),))
        )
        with pytest.raises(PersistenceConflict) as caught:
            await session.persist_turn(
                TURN_ID, ModelTurn("changed", (DurableObservation("changed fact"),))
            )

    assert remember_count == 1
    assert isinstance(caught.value, PersistenceFailure)
    assert caught.value.response == "changed"
    assert caught.value.observations == (DurableObservation("changed fact"),)
    assert caught.value.failure.code == "idempotency_conflict"


@pytest.mark.anyio
async def test_retry_persistence_rejects_same_session_id_under_another_client() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("synthetic original failure", request=request)

    other_scope = Scope("cairn", (ScopeSegment("project", "other"),))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        original = MemorySession(
            MemoryClient(
                http,
                scope=SCOPE,
                classification=Classification.RESTRICTED,
            ),
            session_id=SESSION_ID,
        )
        changed = MemorySession(
            MemoryClient(
                http,
                scope=other_scope,
                classification=Classification.PUBLIC,
            ),
            session_id=SESSION_ID,
        )
        with pytest.raises(PersistenceFailure) as first:
            await original.persist_turn(
                TURN_ID,
                ModelTurn("completed", (DurableObservation("restricted fact"),)),
            )
        with pytest.raises(PersistenceFailure) as mismatch:
            await changed.retry_persistence(first.value)

    assert len(requests) == 1
    assert mismatch.value.response == "completed"
    assert mismatch.value.failure.code == "retry_context_mismatch"
    assert mismatch.value.failure.retry == "never"


@pytest.mark.anyio
async def test_retry_rejects_injected_http_destination_drift_before_sending() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/memory/v1/recall":
            return httpx.Response(200, json=_recall_body())
        raise httpx.ConnectError("synthetic original failure", request=request)

    async def model(_turn_input: TurnInput) -> ModelTurn:
        return ModelTurn("completed", (DurableObservation("durable"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(PersistenceFailure) as first:
            await session.run_turn("query", model, turn_id=TURN_ID)
        http.base_url = "https://changed.invalid"
        with pytest.raises(PersistenceFailure) as drift:
            await session.retry_persistence(first.value)

    assert [request.url.host for request in requests] == [
        "cairn.invalid",
        "cairn.invalid",
    ]
    assert drift.value.response == "completed"
    assert drift.value.observations == (DurableObservation("durable"),)
    assert drift.value.failure.code == "client_context_changed"
    assert drift.value.failure.retry == "never"


@pytest.mark.anyio
async def test_empty_output_cannot_replace_a_persisted_turn() -> None:
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        await session.persist_turn(
            TURN_ID, ModelTurn("first", (DurableObservation("durable"),))
        )
        with pytest.raises(PersistenceConflict) as caught:
            await session.persist_turn(TURN_ID, ModelTurn("changed to empty"))

    assert requests == 1
    assert caught.value.response == "changed to empty"


@pytest.mark.anyio
async def test_persisted_output_cannot_replace_an_empty_turn() -> None:
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        skipped = await session.persist_turn(TURN_ID, ModelTurn("first empty"))
        with pytest.raises(PersistenceConflict) as caught:
            await session.persist_turn(
                TURN_ID,
                ModelTurn("changed to durable", (DurableObservation("durable"),)),
            )

    assert skipped.status is PersistenceStatus.SKIPPED
    assert requests == 0
    assert caught.value.response == "changed to durable"


@pytest.mark.anyio
async def test_unencodable_observation_preserves_completed_response() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_recall_body())

    async def model(_turn_input: TurnInput) -> ModelTurn:
        return ModelTurn("useful completed response", (DurableObservation("\ud800"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        with pytest.raises(PersistenceFailure) as caught:
            await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
                "query", model, turn_id=TURN_ID
            )

    assert paths == ["/memory/v1/recall"]
    assert caught.value.response == "useful completed response"
    assert caught.value.observations == (DurableObservation("\ud800"),)
    assert caught.value.failure.code == "invalid_observations"


@pytest.mark.anyio
async def test_long_input_uses_bounded_recall_cue_but_full_callback_input() -> None:
    full_input = "A" * 5000 + " middle " + "Z" * 5000
    sent_queries: list[str] = []
    callback_inputs: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent_queries.append(body["query"])
        return httpx.Response(200, json=_recall_body())

    async def model(turn_input: TurnInput) -> ModelTurn:
        callback_inputs.append(turn_input.user_input)
        return ModelTurn("completed")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        await MemorySession(_client(http), session_id=SESSION_ID).run_turn(
            full_input, model, turn_id=TURN_ID
        )

    assert callback_inputs == [full_input]
    assert len(sent_queries) == 1
    assert len(sent_queries[0].encode("utf-8")) <= MAX_QUERY_BYTES
    assert sent_queries[0].startswith("A" * 100)
    assert sent_queries[0].endswith("Z" * 100)
