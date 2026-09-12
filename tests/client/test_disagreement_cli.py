"""Explicit disagreement, never implicit conflict resolution."""

import hashlib
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import (
    Boundary,
    BrokenOutput,
    InternalErrorAfterEffect,
    inventory,
    invoke,
    profile,
)

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.client import MemoryClient
from cairn.client.cli_input import FIELDS, parse
from cairn.client.errors import MemoryOperationFailure
from cairn.client.types import PersistenceStatus

LEFT = UUID("11111111-1111-4111-8111-111111111111")
RIGHT = UUID("22222222-2222-4222-8222-222222222222")
KEY = UUID("33333333-3333-5333-8333-333333333333")
RELATIONSHIP = "44444444-4444-4444-8444-444444444444"
SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
REASON = "Mesuré 雪"
# Golden sha256 of the actual authority _json envelope, independently reconstructed
# from its source: UTF-8, ensure_ascii=False, sorted keys, compact separators,
# schema cairn.memory/v1, operation memory-disagree, dataclass identifier naming.
DIGEST = "60886a94b14a26fcca06ddf293ab4d73e897b5a2fa231836cdd75e3a631697aa"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def required_client(client: MemoryClient) -> Any:
    method = getattr(client, "disagree", None)
    assert callable(method), "G1: MemoryClient.disagree is not exposed"
    return method


def command() -> dict[str, Any]:
    return dict(
        left_fact_id=str(LEFT),
        right_fact_id=str(RIGHT),
        reason=REASON,
        idempotency_key=str(KEY),
    )


def receipt() -> dict[str, Any]:
    return {
        "outcome": "committed",
        "result": {"relationship_id": RELATIONSHIP},
        "mutation_receipt": {
            "mutation_id": "55555555-5555-4555-8555-555555555555",
            "command_digest": DIGEST,
        },
        "audit_receipt": {
            "event_id": "66666666-6666-4666-8666-666666666666",
            "chain_kind": "realm",
            "chain_identity": "acme",
            "sequence": 1,
            "recorded_at": "2026-09-09T12:00:00.000000Z",
            "event_hash": "a" * 64,
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["committed", "replayed"])
async def test_disagree_binds_exact_command_and_detaches_receipt(outcome: str) -> None:
    packet = {**receipt(), "outcome": outcome}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=packet)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as http:
        client = MemoryClient(http, scope=SCOPE, classification=Classification.INTERNAL)
        result = await required_client(client)(
            LEFT, RIGHT, reason=REASON, idempotency_key=KEY
        )
    assert result.status is PersistenceStatus(outcome) and result.idempotency_key == KEY
    assert result.result == {"relationship_id": RELATIONSHIP}
    assert len(requests) == 1 and requests[0].url.path == "/memory/v1/disagree"
    assert requests[0].headers["Idempotency-Key"] == str(KEY)
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert json.loads(requests[0].content) == {
        "scope": {
            "realm": "acme",
            "segments": [{"kind": "repository", "identifier": "cairn"}],
        },
        "left_fact_id": str(LEFT),
        "right_fact_id": str(RIGHT),
        "classification": "internal",
        "reason": REASON,
    }
    packet["result"]["relationship_id"] = str(LEFT)
    assert result.result["relationship_id"] == RELATIONSHIP


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        "digest",
        "realm",
        "chain",
        "sequence-bool",
        "timestamp",
        "timestamp-noncanonical",
        "relationship",
        "mutation-id",
        "event-id",
        "event-hash",
        "utf8",
        "surrogate",
        "extra",
        "outcome",
        "duplicate",
        "nonfinite",
    ],
)
async def test_disagree_refuses_unbound_or_malformed_success(bad: str) -> None:
    packet = receipt()
    if bad == "digest":
        packet["mutation_receipt"]["command_digest"] = "b" * 64
    elif bad == "realm":
        packet["audit_receipt"]["chain_identity"] = "other"
    elif bad == "chain":
        packet["audit_receipt"]["chain_kind"] = "instance"
    elif bad == "sequence-bool":
        packet["audit_receipt"]["sequence"] = True
    elif bad == "timestamp":
        packet["audit_receipt"]["recorded_at"] = "yesterday"
    elif bad == "timestamp-noncanonical":
        packet["audit_receipt"]["recorded_at"] = "2026-09-09T12:00:00Z"
    elif bad == "relationship":
        packet["result"]["relationship_id"] = "not-an-id"
    elif bad == "mutation-id":
        packet["mutation_receipt"]["mutation_id"] = str(KEY)
    elif bad == "event-id":
        packet["audit_receipt"]["event_id"] = str(KEY)
    elif bad == "event-hash":
        packet["audit_receipt"]["event_hash"] = "A" * 64
    elif bad == "surrogate":
        packet["audit_receipt"]["chain_identity"] = "\ud800"
    elif bad == "extra":
        packet["private"] = "private-server-prose"
    elif bad == "outcome":
        packet["outcome"] = "skipped"
    raw = json.dumps(packet).encode()
    if bad == "duplicate":
        raw = raw.replace(
            b'"outcome": "committed"', b'"outcome":"committed","outcome":"committed"'
        )
    elif bad == "nonfinite":
        raw = raw.replace(b'"sequence": 1', b'"sequence": NaN')
    elif bad == "utf8":
        raw = b"\xff"
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw)),
    ) as http:
        client = MemoryClient(http, scope=SCOPE, classification=Classification.INTERNAL)
        method = required_client(client)
        with pytest.raises(MemoryOperationFailure) as error:
            await method(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)
        assert (
            error.value.operation == "disagree"
            and error.value.failure.code == "invalid_response"
        )
        assert "private" not in repr(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["oversized-success", "oversized-error", "compressed"])
async def test_disagree_bounds_stream_before_decode(mode: str) -> None:
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if mode == "compressed":
                pytest.fail("compressed stream must be refused without iteration")
            yield b" " * 16385
            pytest.fail("overflow must stop iteration")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                500 if mode == "oversized-error" else 200,
                stream=Body(),
                headers={"Content-Encoding": "gzip"} if mode == "compressed" else {},
            )
        ),
    ) as http:
        method = required_client(
            MemoryClient(http, scope=SCOPE, classification=Classification.INTERNAL)
        )
        with pytest.raises(MemoryOperationFailure) as error:
            await method(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)
        assert error.value.failure.code == "invalid_response"


def test_disagree_cli_accepts_explicit_v5_key_without_authority_overrides() -> None:
    assert "disagree" in FIELDS, "G1: daily disagree input is not exposed"
    value = parse("disagree", io.BytesIO(json.dumps(command()).encode()))
    assert (
        getattr(value, "left_fact_id", None) == LEFT
        and getattr(value, "right_fact_id", None) == RIGHT
    )
    assert value.idempotency_key == KEY and value.reason == REASON


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        "missing-key",
        "non-rfc-key",
        "fact-v5",
        "reason",
        "scope",
        "classification",
        "duplicate",
        "nonfinite",
    ],
)
async def test_disagree_bad_input_precedes_credentials(
    tmp_path: Path, bad: str
) -> None:
    assert "disagree" in FIELDS, "G1: daily disagree input is not exposed"
    body = command()
    if bad == "missing-key":
        body.pop("idempotency_key")
    elif bad == "non-rfc-key":
        body["idempotency_key"] = "33333333-3333-5333-3333-333333333333"
    elif bad == "fact-v5":
        body["left_fact_id"] = str(KEY)
    elif bad == "reason":
        body["reason"] = "é" * 2049
    elif bad in {"scope", "classification"}:
        body[bad] = "private-authority-override"
    raw = json.dumps(body).encode()
    if bad == "duplicate":
        raw = b'{"reason":"x",' + raw[1:]
    elif bad == "nonfinite":
        raw = b'{"private":Infinity,' + raw[1:]
    code, result = await invoke(tmp_path / "missing-private-profile", "disagree", raw)
    assert code == 2 and result["result"]["error"] == {
        "operation": "input",
        "code": "invalid_input",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["normal", "lost-response", "grant-loss"])
async def test_suggestion_explicit_disagreement_history_and_replay(
    tmp_path: Path, memory_support: ModuleType, mode: str
) -> None:
    assert callable(getattr(MemoryClient, "disagree", None)), (
        "G1: missing disagreement client"
    )
    assert "disagree" in FIELDS, "G1: missing disagreement CLI"
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        left = (await api.remember("Batch size is 32."))["result"]["fact_ids"][0]
        right = (await api.remember("Batch size is 64."))["result"]["fact_ids"][0]
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        config = json.loads(path.read_text())
        config.pop("session_id")
        path.write_text(json.dumps(config))
        before = inventory(instance)
        code, suggestion = await invoke(
            path, "suggest", {"observation": "Batch size is 32."}, http._transport
        )
        assert code == 0 and suggestion["result"]["items"]
        assert inventory(instance) == before
        if mode == "grant-loss":
            with _open_write_connection(instance.data_path, create=False) as connection:
                connection.execute(
                    "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                    (canonical_timestamp(instance.clock()), str(principal)),
                )
                connection.commit()
            before = inventory(instance)
        body = {**command(), "left_fact_id": left, "right_fact_id": right}
        transport = Boundary(
            http._transport, "disagree" if mode == "lost-response" else None
        )
        code, result = await invoke(path, "disagree", body, transport)
        if mode == "grant-loss":
            assert (
                code == 2 and result["result"]["error"]["code"] == "connection_refused"
            )
            assert inventory(instance) == before
            return
        if mode == "lost-response":
            assert (
                code == 3
                and result["result"]["recovery"]
                == "resubmit_identical_disagree_same_idempotency_key_and_fields"
            )
        else:
            assert code == 0 and result["result"]["status"] == "committed"
        code, replay = await invoke(path, "disagree", body, transport)
        assert code == 0 and replay["result"]["status"] == "replayed"
        relationship = replay["result"]["result"]["relationship_id"]
        after = inventory(instance)
        assert len(after["memory_disagreements"]) == 1
        assert (
            after["facts"] == before["facts"]
            and after["fact_invalidations"] == before["fact_invalidations"]
        )
        code, history = await invoke(
            path, "history", {"fact_id": left}, http._transport
        )
        assert code == 0
        assert (
            history["result"]["data"]["disagreements"][0]["relationship_id"]
            == relationship
        )
        assert history["result"]["data"]["disagreements"][0]["reason"] == REASON
        assert history["result"]["data"]["disagreements"][0]["principal_id"] == str(
            principal
        )
        assert inventory(instance) == after
        code, conflict = await invoke(
            path, "disagree", {**body, "reason": "Changed reason"}, http._transport
        )
        assert (
            code == 2 and conflict["result"]["error"]["code"] == "idempotency_conflict"
        )
        assert inventory(instance) == after


@pytest.mark.parametrize("version", list("0123456789abcdef"))
def test_disagree_cli_admits_every_rfc_key_version_and_exact_reason_cap(
    version: str,
) -> None:
    key = f"33333333-3333-{version}333-8333-333333333333"
    value = parse(
        "disagree",
        io.BytesIO(
            json.dumps(
                {**command(), "idempotency_key": key, "reason": "é" * 2048}
            ).encode()
        ),
    )
    assert str(value.idempotency_key) == key
    assert len(value.reason.encode()) == 4096


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    ["operation", "scope", "classification", "left_fact_id", "right_fact_id", "reason"],
)
async def test_receipt_digest_rejects_foreign_command_fields(field: str) -> None:
    body: dict[str, Any] = {
        "scope": {
            "realm": "acme",
            "segments": [{"kind": "repository", "identifier": "cairn"}],
        },
        "classification": "internal",
        "left_fact_id": str(LEFT),
        "right_fact_id": str(RIGHT),
        "reason": REASON,
    }
    envelope = {
        "schema": "cairn.memory/v1",
        "operation": "memory-disagree",
        "command": body,
    }
    if field == "operation":
        envelope[field] = "memory-correct"
    elif field == "scope":
        body[field] = {"realm": "acme", "segments": []}
    else:
        body[field] = {
            "classification": "public",
            "left_fact_id": str(RIGHT),
            "right_fact_id": str(LEFT),
            "reason": "Other",
        }[field]
    packet = receipt()
    packet["mutation_receipt"]["command_digest"] = hashlib.sha256(
        json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=packet)),
    ) as http:
        with pytest.raises(MemoryOperationFailure, match="invalid_response"):
            await MemoryClient(
                http, scope=SCOPE, classification=Classification.INTERNAL
            ).disagree(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        "same-facts",
        "fact-v5",
        "key-variant",
        "reason-empty",
        "reason-overflow",
        "reason-surrogate",
        "base-url",
    ],
)
async def test_local_disagreement_validation_never_sends(bad: str) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid request must not dispatch")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(forbidden)
    ) as http:
        client = MemoryClient(http, scope=SCOPE, classification=Classification.INTERNAL)
        left, right, key, reason = LEFT, RIGHT, KEY, REASON
        if bad == "same-facts":
            right = LEFT
        elif bad == "fact-v5":
            left = KEY
        elif bad == "key-variant":
            key = UUID("33333333-3333-5333-3333-333333333333")
        elif bad == "base-url":
            http.base_url = "http://127.0.0.2"
        else:
            reason = {
                "reason-empty": "",
                "reason-overflow": "é" * 2049,
                "reason-surrogate": "\ud800",
            }[bad]
        with pytest.raises(MemoryOperationFailure) as error:
            await client.disagree(left, right, reason=reason, idempotency_key=key)
        assert error.value.failure.code == (
            "client_context_changed" if bad == "base-url" else "invalid_request"
        )


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("overflow", [False, True], ids=["exact-cap", "overflow"])
async def test_disagreement_exact_response_byte_boundary(
    status: int, overflow: bool
) -> None:
    packet = (
        receipt()
        if status == 200
        else {
            "failure": {
                "code": "authorisation_denied",
                "message": "Denied",
                "retry": "never",
                "correlation_id": str(LEFT),
            }
        }
    )
    raw = json.dumps(packet).encode()
    raw += b" " * (16384 + int(overflow) - len(raw))
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=raw)),
    ) as http:
        client = MemoryClient(http, scope=SCOPE, classification=Classification.INTERNAL)
        if status == 200 and not overflow:
            assert (
                await client.disagree(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)
            ).status is PersistenceStatus.COMMITTED
        else:
            with pytest.raises(MemoryOperationFailure) as error:
                await client.disagree(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)
            assert error.value.failure.code == (
                "invalid_response" if overflow else "authorisation_denied"
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode", ["preflight", "wrong-instance", "internal", "malformed", "output"]
)
async def test_disagreement_recovery_is_dispatch_bound_and_never_reads_after_success(
    tmp_path: Path, memory_support: ModuleType, mode: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        left = (await api.remember("First claim"))["result"]["fact_ids"][0]
        right = (await api.remember("Second claim"))["result"]["fact_ids"][0]
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        config = json.loads(path.read_text())
        config.pop("session_id")
        if mode == "wrong-instance":
            config["expected_instance_id"] = str(LEFT)
        path.write_text(json.dumps(config))
        body = {**command(), "left_fact_id": left, "right_fact_id": right}
        before = inventory(instance)

        class Malformed(Boundary):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                response = await super().handle_async_request(request)
                if request.url.path.endswith("/disagree"):
                    assert response.status_code == 200
                    return httpx.Response(200, json={"private": "never disclose"})
                return response

        transport: Boundary
        if mode == "preflight":
            transport = Boundary(http._transport, "diagnose")
        elif mode == "internal":
            transport = InternalErrorAfterEffect(http._transport, "disagree")
        elif mode == "malformed":
            transport = Malformed(http._transport)
        else:
            transport = Boundary(http._transport)
        code, value = await invoke(
            path,
            "disagree",
            body,
            transport,
            stdout=BrokenOutput() if mode == "output" else None,
        )
        diagnostic = value["result"]
        assert "private" not in json.dumps(
            value
        ) and "never disclose" not in json.dumps(value)
        if mode in {"preflight", "wrong-instance"}:
            assert code == (4 if mode == "preflight" else 2)
            assert "recovery" not in diagnostic
            assert transport.paths == ["/memory/v1/diagnose"]
            assert inventory(instance) == before
            return
        assert transport.paths == ["/memory/v1/diagnose", "/memory/v1/disagree"]
        assert transport.keys["/memory/v1/disagree"] == [str(KEY)]
        assert code == (4 if mode == "output" else 3)
        assert diagnostic["last_confirmed_stage"] == (
            "committed" if mode == "output" else "unconfirmed"
        )
        if mode != "output":
            assert (
                diagnostic["recovery"]
                == "resubmit_identical_disagree_same_idempotency_key_and_fields"
            )
        committed = inventory(instance)
        assert len(committed["memory_disagreements"]) == 1
        code, replay = await invoke(path, "disagree", body, http._transport)
        assert code == 0 and replay["result"]["status"] == "replayed"
        assert inventory(instance) == committed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad", ["extra", "nonfinite", "surrogate", "utf8", "duplicate"]
)
async def test_malformed_failure_body_is_not_a_confirmed_refusal(bad: str) -> None:
    failure: dict[str, Any] = {
        "code": "authorisation_denied",
        "retry": "never",
        "correlation_id": str(LEFT),
        "message": "Safe refusal",
    }
    packet: dict[str, Any] = {"failure": failure}
    if bad == "extra":
        packet["private"] = "secret"
    elif bad == "nonfinite":
        failure["message"] = float("inf")
    elif bad == "surrogate":
        failure["message"] = "\ud800"
    raw = json.dumps(packet).encode()
    if bad == "utf8":
        raw = b"\xff"
    elif bad == "duplicate":
        raw = b'{"failure":{},' + raw[1:]
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, content=raw)),
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await MemoryClient(
                http, scope=SCOPE, classification=Classification.INTERNAL
            ).disagree(LEFT, RIGHT, reason=REASON, idempotency_key=KEY)
        assert error.value.failure.code == "invalid_response"
