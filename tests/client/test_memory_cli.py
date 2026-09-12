"""The daily command preserves actual custody, context and read-only operations."""

import importlib
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_durable_session import ForgeTerminal

from cairn.catalogue.sqlite import read_connection


def cli() -> Any:
    assert importlib.util.find_spec("cairn.client.cli") is not None, "daily CLI missing"
    return importlib.import_module("cairn.client.cli")


@pytest.mark.anyio
async def test_http_policy_stable_keys_and_inert_content(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        document = json.loads(path.read_text())
        document["session_id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        path.write_text(json.dumps(document))
        marker = tmp_path / "must-not-execute"
        value = {
            **checkpoint(),
            "turn_id": "11111111-1111-4111-8111-111111111111",
            "attempt_id": "22222222-2222-4222-8222-222222222222",
            "response": f"$(touch {marker}) `touch {marker}`",
        }
        original = httpx.AsyncClient
        options: list[dict[str, Any]] = []

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            options.append(kwargs)
            return original(*args, **kwargs)

        monkeypatch.setattr(cli().httpx, "AsyncClient", factory)
        transport = Boundary(http._transport)
        code, result = await invoke(path, "remember", value, transport)
        assert code == 0, result
        assert len(options) == 1
        assert (
            options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
        )
        timeout = options[0]["timeout"]
        assert all(
            0 < getattr(timeout, name) <= 10
            for name in ("connect", "read", "write", "pool")
        )
        assert transport.keys["/memory/v1/turn-begin"] == [
            "1328e3b2-5eed-5e91-82cd-22bfebde94f6"
        ]
        assert transport.keys["/memory/v1/turn-prepare"] == [
            "fbea2b97-25e3-5928-b597-1da51f24db02"
        ]
        assert (
            result["result"]["persistence"]["idempotency_key"]
            == "6b7614be-4a0f-5a9b-b62a-af867bfd5832"
        )
        assert not marker.exists()
        assert result["result"]["completed_turn"]["response"] == value["response"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command,body",
    [
        ("recall", {"query": "é" * 4097}),
        ("recall", {"query": "x", "budget": 0}),
        ("recall", {"query": "x", "budget": 1048577}),
        ("recall", {"query": "x", "relevant_only": 1}),
        ("status", {"turn_id": None}),
        ("status", b" " * 1048577),
        ("history", {"fact_id": "11111111-1111-5111-8111-111111111111"}),
        (
            "arrive",
            {
                "query": "x",
                "history_fact_ids": ["11111111-1111-4111-8111-111111111111"] * 2,
            },
        ),
        ("suggest", {"fact_ids": ["11111111-1111-4111-8111-111111111111"] * 2}),
        ("suggest", {"observation": "x", "limit": 17}),
        ("suggest", {"observation": "x", "budget": None}),
        ("suggest", {"observation": "x", "limit": None}),
        (
            "correct",
            {
                "fact_ids": [],
                "reason": "x",
                "idempotency_key": "11111111-1111-4111-8111-111111111111",
            },
        ),
    ],
)
async def test_additional_strict_boundaries(command: str, body: object) -> None:
    code, result = await invoke(Path("/nonexistent-profile"), command, body)
    assert code == 2 and result["result"]["error"]["code"] in {
        "invalid_input",
        "input_too_large",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["session-read", "suggest", "recall", "history"])
async def test_read_response_loss_is_operational_not_custody(
    tmp_path: Path, memory_support: ModuleType, operation: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        code, saved = await invoke(path, "remember", checkpoint(), http._transport)
        assert code == 0
        fact_id = saved["result"]["persistence"]["result"]["fact_ids"][0]
        command, body = {
            "session-read": ("resume", {"turn_id": saved["result"]["turn_id"]}),
            "suggest": ("suggest", {"observation": "port"}),
            "recall": ("recall", {"query": "port"}),
            "history": ("history", {"fact_id": fact_id}),
        }[operation]
        before = inventory(instance)
        code, failed = await invoke(
            path, command, body, Boundary(http._transport, operation)
        )
        assert code == 4 and failed["result"]["last_confirmed_stage"] == "unconfirmed"
        assert inventory(instance) == before


@pytest.mark.anyio
async def test_remember_binds_held_preparation_across_resume(
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        transport = ForgeTerminal(http._transport, "response", forge_preflight_after=0)
        code, failed = await invoke(path, "remember", checkpoint(), transport)
        assert code == 3 and failed["result"]["last_confirmed_stage"] == "prepared"
        assert failed["result"]["error"]["code"] == "invalid_response"


@pytest.mark.anyio
async def test_help_uses_command_not_profile_filename() -> None:
    out = io.BytesIO()
    code = await cli().execute(
        ["--profile", "remember", "suggest", "--help"],
        stdin=NoRead(),
        stdout=out,
        stderr=io.BytesIO(),
    )
    assert code == 0 and b"Exactly one of observation" in out.getvalue()


@pytest.mark.anyio
async def test_skipped_abandoned_and_explicit_replacement_states(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        skipped = {**checkpoint(), "response": "", "observations": []}
        code, result = await invoke(path, "remember", skipped, http._transport)
        assert code == 0 and result["result"]["state"] == "skipped"
        assert result["result"]["persistence"]["idempotency_key"] is None
        code, result = await invoke(
            path, "resume", {"turn_id": skipped["turn_id"]}, http._transport
        )
        assert code == 0 and result["result"]["state"] == "skipped"
        lost = checkpoint()
        await invoke(path, "remember", lost, Boundary(http._transport, "turn-begin"))
        code, result = await invoke(
            path,
            "abandon",
            {"turn_id": lost["turn_id"], "reason": "Lost output"},
            http._transport,
        )
        assert code == 0 and result["result"]["snapshot"]["state"] == "abandoned"
        code, _ = await invoke(
            path,
            "abandon",
            {"turn_id": lost["turn_id"], "reason": "Changed reason"},
            http._transport,
        )
        assert code == 2
        code, _ = await invoke(
            path, "resume", {"turn_id": lost["turn_id"]}, http._transport
        )
        assert code == 2
        code, state = await invoke(
            path, "status", {"turn_id": lost["turn_id"]}, http._transport
        )
        assert code == 0 and state["result"]["state"] == "abandoned"
        replacement = {**checkpoint(), "replaces_turn_id": lost["turn_id"]}
        code, result = await invoke(path, "remember", replacement, http._transport)
        assert (
            code == 0
            and result["result"]["snapshot"]["replaces_turn_id"] == lost["turn_id"]
        )


@pytest.mark.anyio
async def test_selected_suggestion_preserves_complete_recorded_evidence_without_writes(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        api = memory_support.Api(http, token)
        first = (await api.remember("The build uses port 8123."))["result"]["fact_ids"][
            0
        ]
        second = (await api.remember("The build uses port 8123."))["result"][
            "fact_ids"
        ][0]
        code, _ = await invoke(
            path,
            "correct",
            {
                "fact_ids": [first],
                "superseded_by": second,
                "reason": "Fresh measurement",
                "idempotency_key": str(uuid4()),
            },
            http._transport,
        )
        assert code == 0
        expected = await api.call(
            "suggest",
            {
                "scope": memory_support.SCOPE,
                "expected_instance_id": str(instance.config.instance_id),
                "fact_ids": [first],
                "budget": 65536,
            },
        )
        before = inventory(instance)
        transport = Boundary(http._transport)
        code, result = await invoke(
            path, "suggest", {"fact_ids": [first], "budget": 65536}, transport
        )
        assert code == 0, result
        assert result["result"] == {
            **expected,
            "source": "cairn-memory/v1",
            "content_role": "untrusted-data",
        }
        assert any(item["corrections"] for item in result["result"]["items"])
        assert inventory(instance) == before
        assert set(transport.paths) == {"/memory/v1/diagnose", "/memory/v1/suggest"}
        assert transport.keys["/memory/v1/suggest"] == [""]


def profile(root: Path, instance: Any, token: str, endpoint: str) -> Path:
    credential = root / "cli-token"
    credential.write_text(token)
    credential.chmod(0o600)
    path = root / "cli-profile.json"
    path.write_text(
        json.dumps(
            {
                "schema": "cairn.memory-profile/v1",
                "endpoint": endpoint,
                "expected_instance_id": str(instance.config.instance_id),
                "scope": {
                    "realm": "acme",
                    "segments": [{"kind": "repository", "identifier": "cairn"}],
                },
                "classification": "internal",
                "credential_file": "cli-token",
                "session_id": str(uuid4()),
            }
        )
    )
    return path


def checkpoint() -> dict[str, Any]:
    return {
        "turn_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "response": "Exact completed output.",
        "observations": [{"body": "The build uses port 8123."}],
    }


async def invoke(
    path: Path,
    command: str,
    body: object = None,
    transport: httpx.AsyncBaseTransport | None = None,
    stdout: io.BytesIO | None = None,
) -> tuple[int, dict[str, Any]]:
    output = stdout if stdout is not None else io.BytesIO()
    error = io.BytesIO()
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    code = await cli().execute(
        ["--profile", str(path), command],
        stdin=io.BytesIO(data),
        stdout=output,
        stderr=error,
        transport=transport,
    )
    raw = error.getvalue() or output.getvalue()
    return code, json.loads(raw)


def inventory(instance: Any) -> dict[str, list[tuple[Any, ...]]]:
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
async def test_cli_actual_custody_replay_conflict_and_read_only_suggestion(
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        value = checkpoint()
        code, saved = await invoke(path, "remember", value, http._transport)
        assert code == 0, saved
        assert saved["result"]["state"] == "committed"
        receipt = saved["result"]["persistence"]
        ids = receipt["result"]["fact_ids"]
        assert len(ids) == 1
        code, replay = await invoke(path, "remember", value, http._transport)
        assert code == 0 and replay["result"]["persistence"] == receipt
        code, conflict = await invoke(
            path, "remember", {**value, "response": "changed"}, http._transport
        )
        assert (
            code == 2 and conflict["result"]["error"]["code"] == "idempotency_conflict"
        )
        before = inventory(instance)
        code, suggested = await invoke(
            path,
            "suggest",
            {"observation": "The build uses port 8123."},
            http._transport,
        )
        assert code == 0, suggested
        result = suggested["result"]
        assert result["content_role"] == "untrusted-data"
        item = next(
            item for item in result["items"] if item["kind"] == "exact_duplicate"
        )
        assert item["facts"][0]["fact_id"] == ids[0]
        assert item["facts"][0]["trust"] == "candidate"
        assert item["facts"][0]["source_principal_id"]
        assert set(item) == {
            "kind",
            "facts",
            "reason",
            "match_basis",
            "corrections",
            "disagreements",
        }
        assert inventory(instance) == before


@pytest.mark.anyio
async def test_arrival_explicit_ack_topic_recall_and_correction(
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        code, arrived = await invoke(path, "arrive", {"query": "port"}, http._transport)
        assert code == 0, arrived
        visit = arrived["result"]["visit"]["snapshot"]
        assert visit["acknowledged_watermark"] == 0
        before = inventory(instance)
        code, _ = await invoke(
            path, "recall", {"query": "another topic"}, http._transport
        )
        assert code == 0 and inventory(instance) == before
        code, ack = await invoke(
            path, "acknowledge-visit", {"visit_id": visit["visit_id"]}, http._transport
        )
        assert (
            code == 0
            and ack["result"]["snapshot"]["acknowledged_watermark"]
            == visit["visit_watermark"]
        )
        code, saved = await invoke(path, "remember", checkpoint(), http._transport)
        assert code == 0
        ids = saved["result"]["persistence"]["result"]["fact_ids"]
        code, corrected = await invoke(
            path,
            "correct",
            {
                "fact_ids": ids,
                "reason": "Measurement corrected",
                "idempotency_key": str(uuid4()),
            },
            http._transport,
        )
        assert code == 0, corrected
        code, history = await invoke(
            path, "history", {"fact_id": ids[0]}, http._transport
        )
        assert code == 0 and history["result"]["data"]["corrections"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command,body",
    [
        ("recall", {"query": "secret", "budget": True}),
        ("recall", {"query": None}),
        ("recall", {"query": "x", "scope": {}}),
        ("recall", b'{"query":"a","query":"b"}'),
        ("recall", b'{"query":"a","budget":NaN}'),
        ("recall", b"\xff"),
        ("recall", b"[]"),
        ("recall", b""),
        ("suggest", {"observation": "x", "fact_ids": []}),
        ("suggest", {"observation": "x", "idempotency_key": "secret"}),
        ("suggest", {"observation": "é" * 2049}),
        ("suggest", {"fact_ids": []}),
        ("suggest", {"observation": "x", "limit": True}),
        ("suggest", {"observation": "x", "budget": 65537}),
        ("suggest", {"observation": None}),
        ("remember", {"turn_id": "bad"}),
    ],
)
async def test_strict_inputs_before_any_network(command: str, body: object) -> None:
    code, result = await invoke(Path("/nonexistent-profile"), command, body)
    assert code == 2
    assert result["result"]["error"]["code"] == "invalid_input"
    assert "secret" not in json.dumps(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "delta",
    [
        {"response": "é" * 16385},
        {"observations": [{"body": ""}]},
        {"observations": [{"body": "x", "trust": "canonical"}]},
        {"observations": [{"body": "x", "valid_from": "2026-09-10T00:00:00Z"}]},
        {
            "observations": [
                {
                    "body": "x",
                    "valid_from": "2026-09-11T00:00:00.000000Z",
                    "valid_to": "2026-09-10T00:00:00.000000Z",
                }
            ]
        },
        {
            "observations": [
                {"body": "x", "observed_at": "2026-09-10T00:00:00.000000Z"},
                {"body": "y"},
            ]
        },
        {"observations": [{"body": "x"}] * 9},
        {"attempt_id": None},
    ],
)
async def test_bad_checkpoint_never_opens_session(
    tmp_path: Path, memory_support: ModuleType, delta: dict[str, Any]
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        before = inventory(instance)
        code, _ = await invoke(
            path, "remember", {**checkpoint(), **delta}, http._transport
        )
        assert code == 2 and inventory(instance) == before


class NoRead(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise AssertionError("stdin must not be read")

    def isatty(self) -> bool:
        return True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command",
    [
        None,
        "check",
        "arrive",
        "recall",
        "acknowledge-visit",
        "remember",
        "status",
        "resume",
        "abandon",
        "history",
        "correct",
        "suggest",
    ],
)
async def test_help_is_standalone(command: str | None) -> None:
    out = io.BytesIO()
    code = await cli().execute(
        ([command] if command else []) + ["--help"],
        stdin=NoRead(),
        stdout=out,
        stderr=io.BytesIO(),
    )
    assert code == 0 and b"usage:" in out.getvalue()
    if command == "suggest":
        assert all(
            field in out.getvalue()
            for field in (
                b"observation",
                b"fact_ids",
                b"budget",
                b"limit",
                b"4096",
                b"65536",
            )
        )


@pytest.mark.anyio
async def test_preflight_failure_is_not_uncertain_save(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    path = profile(tmp_path, instance, token, "http://127.0.0.1:1")
    for command, body in [
        ("remember", checkpoint()),
        ("suggest", {"observation": "x"}),
    ]:
        code, result = await invoke(path, command, body)
        assert code == 4 and result["result"]["last_confirmed_stage"] == "unconfirmed"


class Boundary(httpx.AsyncBaseTransport):
    def __init__(
        self, upstream: httpx.AsyncBaseTransport, lose: str | None = None
    ) -> None:
        self.upstream, self.lose = upstream, lose
        self.paths: list[str] = []
        self.keys: dict[str, list[str]] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.keys.setdefault(request.url.path, []).append(
            request.headers.get("Idempotency-Key", "")
        )
        response = await self.upstream.handle_async_request(request)
        await response.aread()
        if request.url.path.endswith("/" + str(self.lose)):
            self.lose = None
            raise httpx.ReadError("private server prose must not escape")
        return response


class InternalErrorAfterEffect(Boundary):
    """Keep real ASGI effects; replace only a successful acknowledgement."""

    def __init__(self, upstream: httpx.AsyncBaseTransport, operation: str) -> None:
        super().__init__(upstream)
        self.operation = operation

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if request.url.path.endswith("/" + self.operation):
            assert response.status_code == 200
            return httpx.Response(
                500,
                json={
                    "failure": {
                        "code": "internal_error",
                        "retry": "same-request",
                        "correlation_id": "11111111-1111-4111-8111-111111111111",
                        "message": "private internal server details",
                    }
                },
            )
        return response


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command,operation,body",
    [
        ("check", "diagnose", {}),
        ("recall", "recall", {"query": "port"}),
    ],
)
async def test_internal_error_reads_are_operational(
    tmp_path: Path,
    memory_support: ModuleType,
    command: str,
    operation: str,
    body: object,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        before = inventory(instance)
        code, result = await invoke(
            path, command, body, InternalErrorAfterEffect(http._transport, operation)
        )
        assert result["result"]["error"]["code"] == "internal_error"
        assert code == 4
        assert "private" not in json.dumps(result)
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["internal_error", "lost_response"])
async def test_uncertain_sessionless_correction_guides_exact_replay(
    tmp_path: Path,
    memory_support: ModuleType,
    failure: str,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        document = json.loads(path.read_text())
        del document["session_id"]
        path.write_text(json.dumps(document))
        api = memory_support.Api(http, token)
        fact_id = (await api.remember("The build uses port 8123."))["result"][
            "fact_ids"
        ][0]
        body = {
            "fact_ids": [fact_id],
            "reason": "Measurement corrected",
            "idempotency_key": str(uuid4()),
        }
        transport = (
            InternalErrorAfterEffect(http._transport, "correct")
            if failure == "internal_error"
            else Boundary(http._transport, "correct")
        )
        code, result = await invoke(path, "correct", body, transport)
        assert code == 3, result
        assert result["result"]["last_confirmed_stage"] == "unconfirmed"
        assert (
            result["result"]["recovery"]
            == "resubmit_identical_correction_same_idempotency_key_and_fields"
        )
        assert "private" not in json.dumps(result)
        assert transport.paths.count("/memory/v1/correct") == 1
        before_replay = inventory(instance)
        replay_transport = Boundary(http._transport)
        code, replay = await invoke(path, "correct", body, replay_transport)
        assert code == 0 and replay["result"]["status"] == "replayed", replay
        assert inventory(instance) == before_replay
        assert set(replay_transport.paths) == {
            "/memory/v1/diagnose",
            "/memory/v1/correct",
        }
        assert (
            transport.keys["/memory/v1/correct"]
            == replay_transport.keys["/memory/v1/correct"]
            == [body["idempotency_key"]]
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "boundary,want,stage",
    [
        ("turn-begin", 4, "open"),
        ("turn-prepare", 3, "started"),
        ("turn-commit", 3, "prepared"),
    ],
)
async def test_lost_acknowledgement_retains_stage_and_exact_recovery(
    tmp_path: Path,
    memory_support: ModuleType,
    boundary: str,
    want: int,
    stage: str,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        transport = Boundary(http._transport, boundary)
        value = checkpoint()
        code, failed = await invoke(path, "remember", value, transport)
        assert code == want and failed["result"]["last_confirmed_stage"] == stage
        assert "private" not in json.dumps(failed)
        assert transport.paths.count("/memory/v1/" + boundary) == 1
        code, state = await invoke(
            path, "status", {"turn_id": value["turn_id"]}, transport
        )
        assert code == 0
        assert (
            state["result"]["state"]
            == {
                "turn-begin": "started",
                "turn-prepare": "prepared",
                "turn-commit": "committed",
            }[boundary]
        )
        if boundary == "turn-begin":
            assert (
                failed["result"]["recovery"]
                == "resubmit_identical_checkpoint_same_identities"
            )
            code, interrupted = await invoke(
                path, "resume", {"turn_id": value["turn_id"]}, transport
            )
            assert code == 3 and interrupted["result"]["state"] == "interrupted"
            assert interrupted["result"]["completed_turn"] is None
        code, recovered = await invoke(path, "remember", value, transport)
        assert code == 0 and recovered["result"]["state"] == "committed"
        for op in ("turn-begin", "turn-prepare"):
            assert len(set(transport.keys["/memory/v1/" + op])) == 1
        assert (
            transport.keys["/memory/v1/turn-begin"][0]
            != transport.keys["/memory/v1/turn-prepare"][0]
        )
        with read_connection(instance.data_path) as con:
            assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 1


class BrokenOutput(io.BytesIO):
    def write(self, data: Any) -> int:
        raise BrokenPipeError("private output path")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command,want_stage", [("arrive", "open"), ("remember", "committed")]
)
async def test_broken_output_keeps_custody_and_never_acknowledges(
    tmp_path: Path,
    memory_support: ModuleType,
    command: str,
    want_stage: str,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        transport = Boundary(http._transport)
        body = checkpoint() if command == "remember" else {"query": "port"}
        code, result = await invoke(path, command, body, transport, BrokenOutput())
        assert code == 4 and result["result"]["last_confirmed_stage"] == want_stage
        assert result["result"]["error"]["code"] == "output_failure"
        assert "private" not in json.dumps(result)
        assert "/memory/v1/visit-acknowledge" not in transport.paths


@pytest.mark.anyio
async def test_prepare_then_unreadable_resume_keeps_known_preparation(
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()

    class FailRead(Boundary):
        prepared = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self.prepared and request.url.path.endswith("/session-read"):
                raise httpx.ReadError("lost read")
            response = await super().handle_async_request(request)
            if request.url.path.endswith("/turn-prepare"):
                self.prepared = True
            return response

    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        code, result = await invoke(
            path, "remember", checkpoint(), FailRead(http._transport)
        )
        assert code == 3 and result["result"]["last_confirmed_stage"] == "prepared"


@pytest.mark.anyio
async def test_instance_refusal_precedes_content_and_missing_session_is_local(
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        document = json.loads(path.read_text())
        document["expected_instance_id"] = str(uuid4())
        path.write_text(json.dumps(document))
        transport = Boundary(http._transport)
        before = inventory(instance)
        code, _ = await invoke(path, "remember", checkpoint(), transport)
        assert code == 2 and transport.paths == ["/memory/v1/diagnose"]
        assert inventory(instance) == before
        del document["session_id"]
        path.write_text(json.dumps(document))
        transport.paths.clear()
        code, result = await invoke(path, "status", b"", transport)
        assert code == 2 and result["result"]["error"]["code"] == "session_id_required"
        assert transport.paths == []


@pytest.mark.anyio
async def test_check_and_interactive_status_never_read_stdin(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        await invoke(path, "arrive", {"query": "port"}, http._transport)
        for command in ("check", "status"):
            assert (
                await cli().execute(
                    ["--profile", str(path), command],
                    stdin=NoRead(),
                    stdout=io.BytesIO(),
                    stderr=io.BytesIO(),
                    transport=http._transport,
                )
                == 0
            )


@pytest.mark.anyio
async def test_complete_envelope_limit_before_session_open(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    path = profile(tmp_path, instance, token, "http://127.0.0.1:1")
    value = checkpoint()
    value["response"] = "\x01" * 32768
    code, result = await invoke(path, "remember", value)
    assert code == 2 and result["result"]["error"]["code"] == "invalid_input"


@pytest.mark.anyio
async def test_argv_errors_do_not_echo_sensitive_unknown_arguments() -> None:
    out, err = io.BytesIO(), io.BytesIO()
    code = await cli().execute(
        ["--profile", "secret", "recall", "private-query"],
        stdin=NoRead(),
        stdout=out,
        stderr=err,
    )
    assert (
        code == 2
        and b"private-query" not in err.getvalue()
        and b"secret" not in err.getvalue()
    )
