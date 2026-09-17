"""The subscribed host wrapper admits one exact turn without ambient state."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest

from cairn.client import chat_host
from cairn.client.chat_host import ChatActor, ChatHostError
from cairn.client.chat_host import _run_once as run_turn
from cairn.client.host_task import HostTaskError
from cairn.client.profiles import MemoryProfile, load_profile
from cairn.client.turn_receipts import TurnMemory
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DiagnosticPermissions,
    RecalledMemory,
    freeze_object,
)


def test_assessment_query_preserves_utf8_boundaries() -> None:
    assert chat_host._assessment_query("Café".encode()) == "Café"
    query = chat_host._assessment_query(("é" * 5000).encode())
    assert len(query.encode()) <= 8192
    assert query.endswith("é")


def test_host_assessment_retries_only_a_transient_semantic_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": False, "facts": []},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(
        chat_host, "_ASSESSMENT_RECALL_RETRY_SECONDS", 0.0, raising=False
    )
    context = json.loads(
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    )
    assert len(calls) == 2
    assert context["data"]["semantic_degraded"] is False


def test_host_assessment_stops_after_three_degraded_recalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    with pytest.raises(ChatHostError, match="^host_assessment_context_unavailable$"):
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    assert len(calls) == 3
    assert delays == [1.0, 1.0]


def test_host_assessment_recovers_once_from_budget_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": True, "semantic_degraded": False, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": False, "facts": []},
            ],
            calls,
        ),
    )

    context = json.loads(
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    )
    assert context["data"]["budget_exhausted"] is False
    assert calls == [16384, 65536]


def test_host_assessment_fails_after_one_exhausted_budget_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": True, "semantic_degraded": False, "facts": []},
                {"budget_exhausted": True, "semantic_degraded": False, "facts": []},
            ],
            calls,
        ),
    )

    with pytest.raises(ChatHostError, match="^host_assessment_context_unavailable$"):
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    assert calls == [16384, 65536]


def test_host_assessment_fails_if_budget_recovery_is_semantically_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="read-success",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": True, "semantic_degraded": False, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
            ],
            calls,
        ),
    )

    with pytest.raises(ChatHostError, match="^host_assessment_context_unavailable$"):
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    assert calls == [16384, 65536]


def test_host_assessment_recovers_after_semantic_retries_exhaust_the_first_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(
            actor,
            profile,
            [
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": True, "facts": []},
                {"budget_exhausted": True, "semantic_degraded": False, "facts": []},
                {"budget_exhausted": False, "semantic_degraded": False, "facts": []},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(
        chat_host, "_ASSESSMENT_RECALL_RETRY_SECONDS", 0.0, raising=False
    )

    context = json.loads(
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    )
    assert context["data"]["budget_exhausted"] is False
    assert context["data"]["semantic_degraded"] is False
    assert calls == [16384, 16384, 16384, 65536]


def test_host_assessment_does_not_retry_malformed_recall_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="haiku",
    )
    profile = load_profile(actor.profile_path)
    calls: list[int] = []
    monkeypatch.setattr(
        chat_host,
        "MemoryClient",
        _assessment_client(actor, profile, [{}], calls),
    )

    with pytest.raises(ChatHostError, match="^host_assessment_context_unavailable$"):
        asyncio.run(chat_host._load_assessment_context(actor, profile, "journey"))
    assert calls == [16384]


def _profile(tmp_path: Path, *, session_id: UUID | None = None) -> Path:
    credential = tmp_path / "cairn-token"
    credential.write_text(f"cairn1.{uuid4()}.{'A' * 43}\n")
    credential.chmod(0o600)
    profile = tmp_path / "profile.json"
    document: dict[str, object] = {
        "schema": "cairn.memory-profile/v1",
        "endpoint": "http://127.0.0.1:18423",
        "expected_instance_id": str(uuid4()),
        "scope": {
            "realm": "everyday",
            "segments": [{"kind": "job", "identifier": "chat-host-test"}],
        },
        "classification": "internal",
        "credential_file": str(credential),
        "session_id": str(session_id or uuid4()),
    }
    profile.write_text(json.dumps(document))
    return profile


def _auth(tmp_path: Path, provider: str, *, expired: bool = False) -> Path:
    path = tmp_path / f"{provider}-auth.json"
    if provider == "codex":
        value: dict[str, object] = {
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
            },
        }
    else:
        value = {
            "claudeAiOauth": {
                "accessToken": "oauth-access",
                "refreshToken": "oauth-refresh",
                "expiresAt": 1 if expired else 4_102_444_800_000,
            }
        }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def _dedicated_actor(tmp_path: Path, provider: str, model: str) -> ChatActor:
    state = tmp_path / (provider + "-dedicated")
    state.mkdir(mode=0o700)
    seeded = _auth(state, provider)
    auth = state / ("auth.json" if provider == "codex" else ".credentials.json")
    seeded.replace(auth)
    return ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=auth,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model=model,
        auth_state_dir=state,
    )


def _assessment_client(
    actor: ChatActor,
    profile: MemoryProfile,
    packets: list[dict[str, object]],
    calls: list[int],
) -> type[object]:
    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def diagnose(self, **kwargs: object) -> ConnectionDiagnostics:
            return ConnectionDiagnostics(
                ConnectionStatus.READY,
                instance_id=profile.expected_instance_id,
                principal_id=actor.expected_principal,
                scope=profile.scope,
                classification=profile.classification,
                permissions=DiagnosticPermissions(True, True, False, True),
                permission_basis="current_grants_only",
            )

        async def recall(self, *args: object, **kwargs: object) -> RecalledMemory:
            packet = packets[len(calls)]
            budget = kwargs.get("budget")
            if type(budget) is not int:
                raise AssertionError("assessment recall must set a byte budget")
            calls.append(budget)
            return RecalledMemory(freeze_object(packet))

    return FakeClient


def _fake_host(tmp_path: Path) -> Path:
    path = tmp_path / "fake-host"
    path.write_text(
        f"#!{sys.executable}\n"
        + r"""import json, os, stat, sys
from pathlib import Path

args = sys.argv[1:]
provider = "codex" if args and args[0] == "exec" else "claude"
model = args[args.index("--model") + 1]
if provider == "codex":
    configs = [args[index + 1] for index, value in enumerate(args) if value == "-c"]
    encoded = next(value.split("=", 1)[1] for value in configs if value.startswith("mcp_servers.cairn_test.args="))
    mcp_args = json.loads(encoded)
    workflow = json.loads(next(value.split("=", 1)[1] for value in configs if value.startswith("developer_instructions=")))
    enabled = json.loads(next(value.split("=", 1)[1] for value in configs if value.startswith("mcp_servers.cairn_test.enabled_tools=")))
else:
    mcp_path = Path(args[args.index("--mcp-config") + 1])
    config = json.loads(mcp_path.read_text())
    assert list(config["mcpServers"]) == ["cairn_test"]
    mcp_args = config["mcpServers"]["cairn_test"]["args"]
    assert "--append-system-prompt" not in args
    workflow = args[args.index("--system-prompt") + 1]
    enabled = args[args.index("--allowedTools") + 1]

source_path = Path(mcp_args[mcp_args.index("--sources-file") + 1])
source = json.loads(source_path.read_text())
task = sys.stdin.buffer.read()
context_path = (
    Path(mcp_args[mcp_args.index("--assessment-context-file") + 1])
    if "--assessment-context-file" in mcp_args else None
)
if context_path is not None:
    context_text = context_path.read_text()
    if any(context_text in argument for argument in args) or context_text in workflow:
        raise SystemExit(17)
home = Path(os.environ["HOME"])
files = sorted(str(item.relative_to(home)) for item in home.rglob("*") if item.is_file())
auth_name = ".codex/auth.json" if provider == "codex" else ".claude/.credentials.json"
auth_path = home / auth_name
auth_mode = stat.S_IMODE(auth_path.stat().st_mode)
auth_document = json.loads(auth_path.read_text())
oauth = auth_document.get("claudeAiOauth", auth_document.get("tokens", {}))
refresh_key = "refreshToken" if provider == "claude" else "refresh_token"
refresh_seen = oauth.get(refresh_key)
if model.startswith("rotate"):
    oauth[refresh_key] = refresh_seen + "-rotated"
    if provider == "claude":
        oauth["expiresAt"] = 4_102_444_800_000
    auth_path.write_text(json.dumps(auth_document))
    auth_path.chmod(0o600)
if model.startswith("invalidate-auth"):
    auth_path.write_text("{}")
    auth_path.chmod(0o600)
    if model.endswith("fail"):
        print("OAuth refresh token expired", file=sys.stderr)
        raise SystemExit(9)
payload = {
    "provider": provider,
    "workflow": workflow,
    "read_only": "--read-only" in mcp_args,
    "task_hex": task.hex(),
    "source_body": source["sources"][0]["body"],
    "source_id": source["sources"][0]["source_id"],
    "source_mode": stat.S_IMODE(source_path.stat().st_mode),
    "home": str(home),
    "files": files,
    "auth_mode": auth_mode,
    "refresh_seen": refresh_seen,
    "enabled": enabled,
    "builtins": args[args.index("--tools") + 1] if provider == "claude" else None,
    "sole_mcp": provider == "codex" or list(config["mcpServers"]) == ["cairn_test"],
    "workflow_mentions_source": "sources" in workflow,
    "memory_role": workflow.startswith("You are a scoped memory conversation agent."),
    "task_absent_from_argv": task.decode() not in args,
    "api_key_absent": "OPENAI_API_KEY" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ,
}
if model.startswith("read-") and model != "read-no-tools":
    packets = [("check", {"status": "ok", "result": {"status": "ready"}})]
    if context_path is None:
        packets.append(("recall", {"status": "ok", "result": {"data": {"budget_exhausted": False, "semantic_degraded": model == "read-degraded"}}}))
    else:
        packets.append(("sources", {"status": "ok", "result": {"host_assessment_context": json.loads(context_path.read_text())}}))
    for index, (name, packet) in enumerate(packets):
        identity = "tool-" + str(index)
        if provider == "codex":
            print(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cairn_test", "tool": name, "status": "completed", "result": {"structured_content": packet}}}))
        else:
            print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": identity, "name": "mcp__cairn_test__" + name}]}}))
            print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": identity, "content": json.dumps(packet)}]}}))
if model.startswith(("write-error", "prose-error", "arrival-partial")):
    packet = {"status": "unconfirmed", "mapping": None, "remember_receipt": None,
              "correction_receipt": None, "link_verified": False,
              "error": {"code": "receipt_journal_unavailable"}}
    if model.startswith("arrival-partial"):
        packet.update({"status": "partial", "stage": "arrival", "result": {"visit": None}, "error": {"code": "visit_unconfirmed"}})
    if model.startswith("prose-error"):
        if provider == "codex":
            event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(packet)}}
        else:
            event = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(packet)}]}}
    elif provider == "codex":
        event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": {"structured_content": packet}}}
    else:
        event = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": json.dumps(packet)}]}}
    print(json.dumps(event))
if model.endswith("fail"):
    receipt = {
        "status": "partial",
        "stage": "mapping_readback",
        "remember_receipt": {
            "fact_id": "10000000-0000-4000-8000-000000000001",
            "mutation_id": "20000000-0000-4000-8000-000000000002",
            "body": "must not escape",
        },
    }
    if provider == "codex":
        event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": {"structured_content": receipt}}}
    else:
        event = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": json.dumps(receipt)}]}}
    print(json.dumps(event))
    raise SystemExit(9)
if model == "malformed":
    print("not-json")
    raise SystemExit(0)
if model.startswith("read-"):
    if provider == "codex":
        schema_path = Path(args[args.index("--output-schema") + 1])
        assert stat.S_IMODE(schema_path.stat().st_mode) == 0o600
        schema = json.loads(schema_path.read_text())
        assert enabled == ["check", "sources", "recall", "history"]
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
        assert enabled == "mcp__cairn_test__check,mcp__cairn_test__sources,mcp__cairn_test__recall,mcp__cairn_test__history"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["status", "issues"]
    assert "--read-only" in mcp_args
    if context_path is not None:
        assert stat.S_IMODE(context_path.stat().st_mode) == 0o600
    assert source["sources"][0]["body"].encode() == task
    payload = {"status": "complete", "issues": []}
if provider == "codex":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
else:
    print(json.dumps({"type": "system", "subtype": "init"}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(payload), **({"structured_output": payload} if model.startswith("read-") else {})}))
"""
    )
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_run_turn_admits_exact_source_and_uses_only_isolated_cairn(
    tmp_path: Path, provider: str
) -> None:
    task = "Capacity is eight.\r\nCafé — 修理.\n".encode()
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    result = json.loads(run_turn(actor, task).response)

    assert bytes.fromhex(result["task_hex"]) == task
    assert result["source_body"].encode() == task
    assert result["source_mode"] == 0o400
    assert result["auth_mode"] == 0o600
    if provider == "codex":
        assert result["enabled"] == [
            "check",
            "sources",
            "recall",
            "history",
            "remember",
            "replace",
            "arrive",
            "acknowledge_visit",
        ]
    else:
        assert result["enabled"] == "mcp__cairn_test__*"
        assert result["builtins"] == ""
    assert result["sole_mcp"] is True
    assert result["workflow_mentions_source"] is True
    assert result["memory_role"] is True
    assert result["task_absent_from_argv"] is True
    assert result["api_key_absent"] is True
    expected_files = (
        [".codex/auth.json"]
        if provider == "codex"
        else [
            ".claude/.credentials.json",
            "mcp.json",
        ]
    )
    assert result["files"] == sorted(
        [*expected_files, "turn-receipts.json", ".turn-receipts.json.lock"]
    )
    assert not Path(result["home"]).exists()


def test_each_run_uses_a_distinct_source_handle(tmp_path: Path) -> None:
    actor = ChatActor(
        provider="codex",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "codex"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    first = json.loads(run_turn(actor, b"first").response)
    second = json.loads(run_turn(actor, b"second").response)

    assert first["source_id"] != second["source_id"]
    assert first["source_body"] == "first"
    assert second["source_body"] == "second"


@pytest.mark.parametrize(
    ("provider", "auth", "code"),
    [
        ("codex", {"OPENAI_API_KEY": "forbidden", "tokens": {}}, "invalid_host_auth"),
        ("claude", {"claudeAiOauth": {"expiresAt": 1}}, "invalid_host_auth"),
        (
            "claude",
            {
                "ANTHROPIC_API_KEY": "forbidden",
                "claudeAiOauth": {
                    "accessToken": "oauth-access",
                    "refreshToken": "oauth-refresh",
                    "expiresAt": 4_102_444_800_000,
                },
            },
            "invalid_host_auth",
        ),
        (
            "claude",
            {
                "claudeAiOauth": {
                    "accessToken": "oauth-access",
                    "refreshToken": "expired-refresh",
                    "expiresAt": 4_102_444_800_000,
                    "refreshTokenExpiresAt": 1,
                }
            },
            "invalid_host_auth",
        ),
        (
            "claude",
            {
                "claudeAiOauth": {
                    "accessToken": "oauth-access",
                    "refreshToken": "oauth-refresh",
                    "expiresAt": float("nan"),
                }
            },
            "invalid_host_auth",
        ),
    ],
)
def test_invalid_or_expired_subscription_auth_stops_before_launch(
    tmp_path: Path, provider: str, auth: object, code: str
) -> None:
    executable = _fake_host(tmp_path)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(auth))
    auth_path.chmod(0o600)
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=executable,
        auth_file=auth_path,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    with pytest.raises(ChatHostError, match=f"^{code}$"):
        run_turn(actor, b"must not launch")


def test_auth_file_must_be_private_and_explicit(tmp_path: Path) -> None:
    auth = _auth(tmp_path, "codex")
    auth.chmod(0o644)
    actor = ChatActor(
        provider="codex",
        executable=_fake_host(tmp_path),
        auth_file=auth,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    with pytest.raises(ChatHostError, match="^host_auth_unavailable$"):
        run_turn(actor, b"must not launch")


def test_ambiguous_duplicate_auth_fields_are_refused(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"OPENAI_API_KEY":"forbidden","OPENAI_API_KEY":null,'
        '"tokens":{"access_token":"oauth-access",'
        '"refresh_token":"oauth-refresh"}}'
    )
    auth.chmod(0o600)
    actor = ChatActor(
        provider="codex",
        executable=_fake_host(tmp_path),
        auth_file=auth,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    with pytest.raises(ChatHostError, match="^invalid_host_auth$"):
        run_turn(actor, b"must not launch")


def test_expired_claude_access_with_live_refresh_is_accepted(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-access",
                    "refreshToken": "live-refresh",
                    "expiresAt": 1,
                    "refreshTokenExpiresAt": 4_102_444_800_000,
                }
            }
        )
    )
    auth.chmod(0o600)
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=auth,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    result = json.loads(run_turn(actor, b"refreshable").response)

    assert result["provider"] == "claude"


def test_prepared_auth_preserves_refresh_rotation_across_turns(tmp_path: Path) -> None:
    source = _auth(tmp_path, "claude")
    original = source.read_bytes()
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=source,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="rotate",
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    destination = session_dir / "credentials.json"

    prepared = chat_host.prepare_actor_auth(actor, destination)
    first = json.loads(run_turn(prepared, b"first").response)
    second = json.loads(run_turn(prepared, b"second").response)

    assert prepared.auth_file == destination
    assert prepared.refresh_file == destination
    assert first["refresh_seen"] == "oauth-refresh"
    assert second["refresh_seen"] == "oauth-refresh-rotated"
    assert source.read_bytes() == original
    stored = json.loads(destination.read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == ("oauth-refresh-rotated-rotated")


def test_dedicated_auth_retains_rotation_across_fresh_consoles(tmp_path: Path) -> None:
    actor = _dedicated_actor(tmp_path, "claude", "rotate")

    first = json.loads(run_turn(actor, b"first").response)
    second = json.loads(run_turn(actor, b"second").response)

    assert first["refresh_seen"] == "oauth-refresh"
    assert second["refresh_seen"] == "oauth-refresh-rotated"
    stored = json.loads(actor.auth_file.read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == "oauth-refresh-rotated-rotated"
    assert not (actor.auth_state_dir / ".cairn-chat-auth-pending").exists()  # type: ignore[operator]


def test_dedicated_auth_lock_refuses_a_second_console(tmp_path: Path) -> None:
    actor = _dedicated_actor(tmp_path, "codex", "success")

    with chat_host.actor_auth_locks([actor]):
        with pytest.raises(ChatHostError, match="^host_auth_locked$"):
            with chat_host.actor_auth_locks([actor]):
                pass


def test_incomplete_dedicated_handoff_requires_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _dedicated_actor(tmp_path, "codex", "success")
    launched = 0

    def interrupted(*args: object, **kwargs: object) -> object:
        nonlocal launched
        launched += 1
        raise HostTaskError("host_timeout")

    monkeypatch.setattr(chat_host, "run_host_task", interrupted)
    with pytest.raises(ChatHostError, match="^host_timeout$"):
        run_turn(actor, b"may have committed")
    assert launched == 1
    assert (actor.auth_state_dir / ".cairn-chat-auth-pending").exists()  # type: ignore[operator]

    with pytest.raises(ChatHostError, match="^host_auth_recovery_required$"):
        run_turn(actor, b"must not retry")
    assert launched == 1


def test_pending_marker_is_durable_before_provider_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _dedicated_actor(tmp_path, "codex", "success")
    synced: list[str] = []
    original_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        synced.append(kind)
        original_fsync(descriptor)

    def interrupted(*args: object, **kwargs: object) -> object:
        assert synced[-2:] == ["file", "directory"]
        marker = actor.auth_state_dir / ".cairn-chat-auth-pending"  # type: ignore[operator]
        assert chat_host._pending_digest(marker)
        raise HostTaskError("host_timeout")

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(chat_host, "run_host_task", interrupted)
    with pytest.raises(ChatHostError, match="^host_timeout$"):
        run_turn(actor, b"interrupted")


def test_explicit_recovery_requires_changed_valid_dedicated_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _dedicated_actor(tmp_path, "codex", "success")
    monkeypatch.setattr(
        chat_host,
        "run_host_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(HostTaskError("host_timeout")),
    )
    with pytest.raises(ChatHostError, match="^host_timeout$"):
        run_turn(actor, b"interrupted")
    with pytest.raises(ChatHostError, match="^host_auth_recovery_required$"):
        chat_host.recover_actor_auth([actor])

    document = json.loads(actor.auth_file.read_text())
    document["tokens"]["refresh_token"] = "fresh-login-refresh"
    actor.auth_file.write_text(json.dumps(document))
    actor.auth_file.chmod(0o600)
    chat_host.recover_actor_auth([actor])
    assert not (actor.auth_state_dir / ".cairn-chat-auth-pending").exists()  # type: ignore[operator]


def test_recovery_skips_clean_actor_and_recovers_pending_actor(tmp_path: Path) -> None:
    val = _dedicated_actor(tmp_path, "codex", "success")
    spike = _dedicated_actor(tmp_path, "claude", "success")
    chat_host._begin_auth_handoff(spike, chat_host._read_auth(spike.auth_file))
    document = json.loads(spike.auth_file.read_text())
    document["claudeAiOauth"]["refreshToken"] = "fresh-login-refresh"
    spike.auth_file.write_text(json.dumps(document))
    spike.auth_file.chmod(0o600)

    chat_host.recover_actor_auth([val, spike])

    assert not (spike.auth_state_dir / ".cairn-chat-auth-pending").exists()  # type: ignore[operator]
    assert val.auth_file.exists()


def test_invalid_dedicated_refresh_preserves_previous_auth_and_marker(
    tmp_path: Path,
) -> None:
    actor = _dedicated_actor(tmp_path, "claude", "invalidate-auth-success")
    original = actor.auth_file.read_bytes()

    with pytest.raises(ChatHostError, match="^host_auth_refresh_failed$"):
        run_turn(actor, b"must fail closed")

    assert actor.auth_file.read_bytes() == original
    assert (actor.auth_state_dir / ".cairn-chat-auth-pending").exists()  # type: ignore[operator]


def test_failed_turn_still_preserves_valid_rotated_auth(tmp_path: Path) -> None:
    source = _auth(tmp_path, "claude")
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=source,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="rotate-fail",
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    destination = session_dir / "credentials.json"
    prepared = chat_host.prepare_actor_auth(actor, destination)

    with pytest.raises(ChatHostError, match="^host_failed$"):
        run_turn(prepared, b"may have committed")

    stored = json.loads(destination.read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == "oauth-refresh-rotated"


def test_provider_auth_failure_remains_primary_when_refresh_copy_is_invalid(
    tmp_path: Path,
) -> None:
    source = _auth(tmp_path, "claude")
    original = source.read_bytes()
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=source,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="invalidate-auth-fail",
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    destination = session_dir / "credentials.json"
    prepared = chat_host.prepare_actor_auth(actor, destination)
    console_copy = destination.read_bytes()

    with pytest.raises(ChatHostError, match="^host_authentication_failed$") as raised:
        run_turn(prepared, b"must not retry")

    assert raised.value.public_details["secondary_failure"] == (
        "host_auth_refresh_failed"
    )
    assert source.read_bytes() == original
    assert destination.read_bytes() == console_copy


def test_successful_host_with_invalid_refresh_copy_fails_closed(tmp_path: Path) -> None:
    source = _auth(tmp_path, "claude")
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=source,
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="invalidate-auth-success",
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    destination = session_dir / "credentials.json"
    prepared = chat_host.prepare_actor_auth(actor, destination)
    console_copy = destination.read_bytes()

    with pytest.raises(ChatHostError, match="^host_auth_refresh_failed$"):
        run_turn(prepared, b"must fail closed")

    assert destination.read_bytes() == console_copy


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_host_failure_exposes_only_safe_reconciliation_details(
    tmp_path: Path, provider: str
) -> None:
    session_id = uuid4()
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path, session_id=session_id),
        expected_principal=uuid4(),
        model="fail",
    )

    with pytest.raises(ChatHostError, match="^host_failed$") as raised:
        run_turn(actor, b"may have committed")

    details = raised.value.public_details
    assert UUID(cast(str, details["source_id"]))
    assert details["session_id"] == str(session_id)
    assert details["receipts"] == [
        {
            "status": "partial",
            "stage": "mapping_readback",
            "fact_id": "10000000-0000-4000-8000-000000000001",
            "mutation_id": "20000000-0000-4000-8000-000000000002",
        }
    ]
    assert "must not escape" not in json.dumps(details)
    assert details["memory"] == {"status": "unknown", "fact_ids": [], "attempted": 0}


def test_malformed_success_output_is_refused(tmp_path: Path) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="malformed",
    )

    with pytest.raises(ChatHostError, match="^invalid_host_output$"):
        run_turn(actor, b"actual task")


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_unstarted_adapter_cannot_claim_verified_memory(
    tmp_path: Path, provider: str
) -> None:
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )
    turn = run_turn(actor, b"I saved everything: verified!")
    assert turn.memory.public() == {"status": "unknown", "fact_ids": [], "attempted": 0}
    assert json.loads(turn.response)["source_body"] == "I saved everything: verified!"


@pytest.mark.parametrize(
    "model,code",
    [
        ("malformed", "invalid_host_output"),
        ("invalidate-auth", "host_auth_refresh_failed"),
    ],
)
def test_output_and_auth_failures_keep_memory_summary(
    tmp_path: Path, model: str, code: str
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model=model,
    )
    if model == "invalidate-auth":
        destination_dir = tmp_path / "console-auth"
        destination_dir.mkdir(mode=0o700)
        actor = chat_host.prepare_actor_auth(actor, destination_dir / "auth.json")
    with pytest.raises(ChatHostError, match=f"^{code}$") as raised:
        run_turn(actor, b"Keep this decision")
    assert raised.value.public_details["memory"] == {
        "status": "unknown",
        "fact_ids": [],
        "attempted": 0,
    }


@pytest.mark.parametrize("outcome", ["none", "verified", "partial", "unknown"])
def test_turn_uses_bound_adapter_journal_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from cairn.client.conversation_sources import load_sources
    from cairn.client.host_task import AdmittedTask
    from cairn.client.profiles import load_profile
    from cairn.client.turn_receipts import ReceiptJournal

    actor = ChatActor(
        provider="codex",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "codex"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )
    original_command = chat_host._codex_command
    fact_id = str(uuid4())
    paths: list[Path] = []

    def adapter_fixture(
        current: ChatActor,
        admission: AdmittedTask,
        *,
        receipt_path: Path | None = None,
        workflow: str | None = None,
        read_only: bool = False,
        output_schema: Path | None = None,
    ) -> list[str]:
        assert receipt_path is not None
        paths.append(receipt_path)
        sources = load_sources(
            admission.sources_path,
            profile=load_profile(current.profile_path),
            expected_principal=current.expected_principal,
        )
        journal = ReceiptJournal.open(receipt_path, sources)
        if outcome != "none":
            sequence = journal.begin("remember", sources.sources[0].source_id)
            if outcome != "unknown":
                journal.finish(
                    sequence, {"status": outcome, "mapping": {"fact_id": fact_id}}
                )
        return original_command(
            current,
            admission,
            receipt_path=receipt_path,
            workflow=workflow,
            read_only=read_only,
            output_schema=output_schema,
        )

    monkeypatch.setattr(chat_host, "_codex_command", adapter_fixture)
    turn = run_turn(actor, b"A durable decision")
    assert turn.memory.status == outcome
    assert turn.memory.attempted == (0 if outcome == "none" else 1)
    assert turn.memory.fact_ids == (
        (fact_id,) if outcome in {"verified", "partial"} else ()
    )
    assert paths and not paths[0].exists()


def test_timeout_keeps_incomplete_write_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.client.conversation_sources import load_sources
    from cairn.client.host_task import AdmittedTask, HostTaskError
    from cairn.client.profiles import load_profile
    from cairn.client.turn_receipts import ReceiptJournal

    actor = ChatActor(
        provider="codex",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "codex"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
    )

    def interrupted_adapter(
        current: ChatActor,
        admission: AdmittedTask,
        *,
        receipt_path: Path | None = None,
        workflow: str | None = None,
        read_only: bool = False,
        output_schema: Path | None = None,
    ) -> list[str]:
        assert receipt_path is not None
        sources = load_sources(
            admission.sources_path,
            profile=load_profile(current.profile_path),
            expected_principal=current.expected_principal,
        )
        journal = ReceiptJournal.open(receipt_path, sources)
        journal.begin("remember", sources.sources[0].source_id)
        raise HostTaskError("host_timeout")

    monkeypatch.setattr(chat_host, "_codex_command", interrupted_adapter)
    with pytest.raises(ChatHostError, match="^host_timeout$") as raised:
        run_turn(actor, b"A durable decision")
    assert raised.value.public_details["memory"] == {
        "status": "unknown",
        "fact_ids": [],
        "attempted": 1,
    }


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "model", ["write-error", "write-error-fail", "prose-error", "arrival-partial"]
)
def test_structured_write_failure_only_downgrades_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, model: str
) -> None:
    from cairn.client.conversation_sources import load_sources
    from cairn.client.host_task import AdmittedTask
    from cairn.client.profiles import load_profile
    from cairn.client.turn_receipts import ReceiptJournal

    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model=model,
    )
    original_args = chat_host._adapter_args
    fact_id = str(uuid4())

    def adapter_fixture(
        current: ChatActor,
        admission: AdmittedTask,
        *,
        receipt_path: Path | None = None,
        workflow: str | None = None,
        read_only: bool = False,
        output_schema: Path | None = None,
    ) -> list[str]:
        assert receipt_path is not None
        sources = load_sources(
            admission.sources_path,
            profile=load_profile(current.profile_path),
            expected_principal=current.expected_principal,
        )
        journal = ReceiptJournal.open(receipt_path, sources)
        sequence = journal.begin("remember", sources.sources[0].source_id)
        journal.finish(
            sequence, {"status": "verified", "mapping": {"fact_id": fact_id}}
        )
        return original_args(
            current, admission, receipt_path=receipt_path, read_only=read_only
        )

    monkeypatch.setattr(chat_host, "_adapter_args", adapter_fixture)
    if model.endswith("fail"):
        with pytest.raises(ChatHostError, match="^host_failed$") as raised:
            run_turn(actor, b"Prior saved decision, then a new decision")
        memory = raised.value.public_details["memory"]
    else:
        memory = run_turn(
            actor, b"Prior saved decision, then a new decision"
        ).memory.public()
    assert memory == {
        "status": "unknown" if model.startswith("write-error") else "verified",
        "fact_ids": [fact_id],
        "attempted": 1,
    }


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_read_only_phase_preserves_source_and_restricts_tools(
    tmp_path: Path, provider: str
) -> None:
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="read-success",
    )
    source_id = uuid4()
    task = "She's doing tax calculations.\r\nCafé\n".encode()
    result = run_turn(
        actor, task, source_id=source_id, workflow="Audit only.", read_only=True
    )
    assert json.loads(result.response) == {"status": "complete", "issues": []}
    # The executable itself checks the schema flag, private schema mode,
    # source bytes and restricted tool list before it emits its verdict.
    assert result.completion == "unchecked"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_trusted_assessment_context_is_private_and_read_from_sources(
    tmp_path: Path, provider: str
) -> None:
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="read-success",
    )
    context = json.dumps(
        {
            "source": "cairn-memory/v1",
            "content_role": "untrusted-data",
            "binding": {},
            "data": {
                "budget_exhausted": False,
                "semantic_degraded": False,
                "hits": [{"body": "private recall sentinel"}],
            },
        }
    )
    result = run_turn(
        actor,
        b"A harmless question",
        workflow="Assess without leaking context.",
        read_only=True,
        trusted_assessment_context=True,
        assessment_context=context,
    )
    assert json.loads(result.response) == {"status": "complete", "issues": []}


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("model", ["read-no-tools", "read-degraded"])
def test_read_only_phase_requires_successful_actual_reads(
    tmp_path: Path, provider: str, model: str
) -> None:
    actor = ChatActor(
        provider=cast(Literal["codex", "claude"], provider),
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, provider),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model=model,
    )
    with pytest.raises(ChatHostError, match="^host_read_evidence_unconfirmed$"):
        run_turn(actor, b"A task", read_only=True)


def _read_events(provider: str, packets: Sequence[tuple[str, object]]) -> bytes:
    events = []
    for index, (name, packet) in enumerate(packets):
        if provider == "codex":
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "cairn_test",
                        "tool": name,
                        "status": "completed",
                        "result": {"structured_content": packet},
                    },
                }
            )
        else:
            identity = f"tool-{index}"
            events.extend(
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": identity,
                                    "name": f"mcp__cairn_test__{name}",
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": identity,
                                    "content": json.dumps(packet),
                                }
                            ]
                        },
                    },
                ]
            )
    return "\n".join(json.dumps(event) for event in events).encode()


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("name", ["recall", "history"])
def test_read_evidence_accepts_only_complete_reads(provider: str, name: str) -> None:
    check = ("check", {"status": "ok", "result": {"status": "ready"}})
    read = (
        name,
        {
            "status": "ok",
            "result": {
                "data": {
                    "budget_exhausted": False,
                    "semantic_degraded": False,
                }
            },
        },
    )
    if name == "history":
        with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
            chat_host._require_read_evidence(
                provider, _read_events(provider, [check, read])
            )
    else:
        chat_host._require_read_evidence(
            provider, _read_events(provider, [check, read])
        )
    for packets in (
        [read, check],
        [check],
        [read],
        [check, read, (name, {"status": "partial"})],
    ):
        with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
            chat_host._require_read_evidence(provider, _read_events(provider, packets))
    for data in ({}, {"budget_exhausted": True, "semantic_degraded": False}):
        packet = {"status": "ok", "result": {"data": data}}
        with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
            chat_host._require_read_evidence(
                provider, _read_events(provider, [check, (name, packet)])
            )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_read_evidence_requires_host_context_from_sources(provider: str) -> None:
    check = ("check", {"status": "ok", "result": {"status": "ready"}})
    context = {
        "source": "cairn-memory/v1",
        "content_role": "untrusted-data",
        "binding": {},
        "data": {"budget_exhausted": False, "semantic_degraded": False},
    }
    chat_host._require_read_evidence(
        provider,
        _read_events(
            provider,
            [
                (check[0], check[1]),
                (
                    "sources",
                    {"status": "ok", "result": {"host_assessment_context": context}},
                ),
            ],
        ),
        require_assessment_context=True,
    )
    for bad in ({}, {"host_assessment_context": {**context, "data": {}}}):
        with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
            chat_host._require_read_evidence(
                provider,
                _read_events(
                    provider,
                    [
                        (check[0], check[1]),
                        ("sources", {"status": "ok", "result": bad}),
                    ],
                ),
                require_assessment_context=True,
            )

    # Claude may issue check and sources in one assistant message.  The adapter
    # authenticates before serving sources, so either completed-result order is
    # valid provided the host receives both verified observations.
    chat_host._require_read_evidence(
        provider,
        _read_events(
            provider,
            [
                (
                    "sources",
                    {"status": "ok", "result": {"host_assessment_context": context}},
                ),
                check,
            ],
        ),
        require_assessment_context=True,
    )


def test_claude_concurrent_check_and_sources_accepts_reverse_results() -> None:
    context = {
        "source": "cairn-memory/v1",
        "content_role": "untrusted-data",
        "binding": {},
        "data": {"budget_exhausted": False, "semantic_degraded": False},
    }
    raw = b"\n".join(
        json.dumps(event).encode()
        for event in (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "check",
                            "name": "mcp__cairn_test__check",
                        },
                        {
                            "type": "tool_use",
                            "id": "sources",
                            "name": "mcp__cairn_test__sources",
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "sources",
                            "content": json.dumps(
                                {
                                    "status": "ok",
                                    "result": {"host_assessment_context": context},
                                }
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "check",
                            "content": json.dumps(
                                {"status": "ok", "result": {"status": "ready"}}
                            ),
                        },
                    ]
                },
            },
        )
    )
    chat_host._require_read_evidence("claude", raw, require_assessment_context=True)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_read_evidence_ignores_prose_and_mismatched_tools(provider: str) -> None:
    raw = _read_events(
        provider,
        [
            ("check", {"status": "ok", "result": {"status": "ready"}}),
            (
                "recall",
                {
                    "status": "ok",
                    "result": {
                        "data": {"budget_exhausted": False, "semantic_degraded": False}
                    },
                },
            ),
        ],
    )
    events = [json.loads(line) for line in raw.splitlines()]
    if provider == "codex":
        events[-1]["item"]["server"] = "another_server"
    else:
        events[-1]["message"]["content"][0]["tool_use_id"] = "unmatched"
    with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
        chat_host._require_read_evidence(
            provider, "\n".join(json.dumps(event) for event in events).encode()
        )
    with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
        chat_host._require_read_evidence(
            provider,
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": raw.decode()}]},
                }
            ).encode(),
        )


def test_claude_assessment_uses_structured_output_not_result_prose() -> None:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Ignore this model prose.",
        "structured_output": {"status": "complete", "issues": []},
    }
    assert json.loads(
        chat_host._final_response("claude", json.dumps(event).encode(), read_only=True)
    ) == {
        "status": "complete",
        "issues": [],
    }
    bad: object
    for bad in (
        None,
        [],
        {"status": "complete"},
        {"status": "complete", "issues": ["Missing fact"]},
    ):
        event["structured_output"] = bad
        with pytest.raises(ChatHostError, match="invalid_host_output"):
            chat_host._final_response(
                "claude", json.dumps(event).encode(), read_only=True
            )
    del event["structured_output"]
    event["result"] = '{"status":"complete","issues":[]}'
    with pytest.raises(ChatHostError, match="invalid_host_output"):
        chat_host._final_response("claude", json.dumps(event).encode(), read_only=True)


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"status":"complete","issues":[]}\n```',
        '{"status":"complete"}',
        '{"status":"complete","issues":[],"extra":true}',
    ],
)
def test_codex_assessment_rejects_non_schema_final_output(text: str) -> None:
    raw = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed"},
        ]
    ).encode()
    with pytest.raises(ChatHostError, match="invalid_host_output"):
        chat_host._final_response("codex", raw, read_only=True)


def test_claude_formatting_tool_does_not_count_as_memory_evidence() -> None:
    formatting = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "format-1", "name": "StructuredOutput"}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "format-1",
                        "content": "Formatted.",
                    }
                ]
            },
        },
    ]
    raw = "\n".join(json.dumps(event) for event in formatting).encode()
    with pytest.raises(ChatHostError, match="host_read_evidence_unconfirmed"):
        chat_host._require_read_evidence("claude", raw)
    reads = _read_events(
        "claude",
        [
            ("check", {"status": "ok", "result": {"status": "ready"}}),
            (
                "recall",
                {
                    "status": "ok",
                    "result": {
                        "data": {"budget_exhausted": False, "semantic_degraded": False}
                    },
                },
            ),
        ],
    )
    chat_host._require_read_evidence("claude", reads + b"\n" + raw)


@pytest.mark.parametrize(
    "assessment_model", ["", "has space", "has\nnewline", "x" * 129, 42, False]
)
def test_invalid_assessment_model_stops_before_launch(
    tmp_path: Path, assessment_model: object
) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
        assessment_model=cast(str, assessment_model),
    )
    with pytest.raises(ChatHostError, match="^invalid_host_actor$"):
        run_turn(actor, b"Do not launch.")


def test_assessment_model_survives_auth_preparation(tmp_path: Path) -> None:
    actor = ChatActor(
        provider="claude",
        executable=_fake_host(tmp_path),
        auth_file=_auth(tmp_path, "claude"),
        profile_path=_profile(tmp_path),
        expected_principal=uuid4(),
        model="success",
        assessment_model="sonnet",
    )
    directory = tmp_path / "console-auth"
    directory.mkdir(mode=0o700)
    prepared = chat_host.prepare_actor_auth(actor, directory / "claude.json")
    assert prepared.assessment_model == "sonnet"
    assert prepared.model == "success"
    assert (
        json.loads(run_turn(prepared, b"Normal turn").response)["task_hex"]
        == b"Normal turn".hex()
    )
    assert chat_host.ChatTurn("Reply", TurnMemory("none")).completion_issues == ()
