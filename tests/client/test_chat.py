"""Managed input is the only automatic source of a submitted turn."""

import io
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from cairn.client import chat
from cairn.client.chat import ChatConfigError, console, load_config, session_actors
from cairn.client.chat_host import ChatActor, ChatHostError, ChatTurn
from cairn.client.profiles import load_profile
from cairn.client.turn_receipts import TurnMemory


def actors(tmp_path: Path) -> dict[str, ChatActor]:
    instance = str(uuid4())
    result = {}
    for name in ("val", "spike"):
        state = tmp_path / (name + "-state")
        state.mkdir(mode=0o700)
        auth = state / ("auth.json" if name == "val" else ".credentials.json")
        auth.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": "fixture-access",
                        "refresh_token": "fixture-refresh",
                    },
                    "claudeAiOauth": {
                        "accessToken": "fixture-access",
                        "refreshToken": "fixture-refresh",
                        "expiresAt": 4102444800000,
                    },
                }
            )
        )
        auth.chmod(0o600)
        path = tmp_path / (name + ".json")
        path.write_text(
            json.dumps(
                {
                    "schema": "cairn.memory-profile/v1",
                    "endpoint": "http://127.0.0.1:18423",
                    "expected_instance_id": instance,
                    "scope": {
                        "realm": "everyday",
                        "segments": [{"kind": "job", "identifier": "chat"}],
                    },
                    "classification": "internal",
                    "credential_file": str(tmp_path / "token"),
                    "session_id": str(uuid4()),
                }
            )
        )
        result[name] = ChatActor(
            "codex" if name == "val" else "claude",
            Path("/usr/bin/true"),
            auth,
            path,
            uuid4(),
            "test",
            auth_state_dir=state,
        )
    return result


def test_two_turns_switch_actor_and_never_admit_model_reply(tmp_path: Path) -> None:
    configured = actors(tmp_path)
    calls: list[tuple[ChatActor, bytes]] = []

    def turn(actor: ChatActor, message: bytes) -> ChatTurn:
        calls.append((actor, message))
        return ChatTurn(
            "Unsaved reply: invented booking confirmation.\x1b[2J",
            TurnMemory("none"),
            completion="complete",
        )

    output = io.StringIO()
    status = console(
        configured,
        io.BytesIO(
            "Café — 修理.\r\nKeep eight.\n/send\n/spike\nDo not book.\n/send\n/quit\n".encode()
        ),
        output,
        invoke=turn,
    )
    assert status == 0
    assert calls == [
        (configured["val"], "Café — 修理.\r\nKeep eight.\n".encode()),
        (configured["spike"], b"Do not book.\n"),
    ]
    assert "\x1b" not in output.getvalue()
    assert "saved" in output.getvalue().lower()


@pytest.mark.parametrize(
    "data",
    [
        b"draft without send",
        b"/send\n/quit\n",
        b"\n/send\n/quit\n",
        b"draft\n/cancel\n/quit\n",
    ],
)
def test_unsent_empty_and_cancelled_input_never_launch(
    tmp_path: Path, data: bytes
) -> None:
    def turn(actor: ChatActor, task: bytes) -> str:
        pytest.fail("unsent input launched a model")

    assert console(actors(tmp_path), io.BytesIO(data), io.StringIO(), invoke=turn) == 0


def test_literal_commands_and_draft_actor_command_are_preserved(tmp_path: Path) -> None:
    messages: list[bytes] = []

    def turn(actor: ChatActor, message: bytes) -> ChatTurn:
        messages.append(message)
        return ChatTurn("ok", TurnMemory("none"), completion="complete")

    console(
        actors(tmp_path),
        io.BytesIO(b"/literal /send\r\n/spike\n/send\n/quit\n"),
        io.StringIO(),
        invoke=turn,
    )
    assert messages == [b"/send\r\n/spike\n"]


def test_oversized_line_is_drained_and_requires_cancel(tmp_path: Path) -> None:
    messages: list[bytes] = []

    def turn(actor: ChatActor, message: bytes) -> ChatTurn:
        messages.append(message)
        return ChatTurn("ok", TurnMemory("none"), completion="complete")

    data = b"x" * 100000 + b"\n/send\nignored\n/send\n/cancel\nok\n/send\n/quit\n"
    console(actors(tmp_path), io.BytesIO(data), io.StringIO(), invoke=turn)
    assert messages == [b"ok\n"]


def test_failure_stops_before_next_turn_and_warns_about_custody(tmp_path: Path) -> None:
    calls = 0

    def turn(actor: ChatActor, message: bytes) -> ChatTurn:
        nonlocal calls
        calls += 1
        raise ChatHostError("host_failed")

    output = io.StringIO()
    assert (
        console(
            actors(tmp_path),
            io.BytesIO(b"one\n/send\ntwo\n/send\n"),
            output,
            invoke=turn,
        )
        == 1
    )
    assert calls == 1
    assert "read-back" in output.getvalue()


def test_invalid_utf8_refused_without_losing_next_valid_turn(tmp_path: Path) -> None:
    messages: list[bytes] = []

    def turn(actor: ChatActor, message: bytes) -> ChatTurn:
        messages.append(message)
        return ChatTurn("ok", TurnMemory("none"), completion="complete")

    console(
        actors(tmp_path),
        io.BytesIO(b"\xff\n/send\nok\n/send\n"),
        io.StringIO(),
        invoke=turn,
    )
    assert messages == [b"ok\n"]


def test_each_console_gets_distinct_sessions_and_cleans_profiles(
    tmp_path: Path,
) -> None:
    configured = actors(tmp_path)
    with session_actors(configured) as first:
        paths = [a.profile_path for a in first.values()]
        ids = [load_profile(p).session_id for p in paths]
        assert all(p.stat().st_mode & 0o777 == 0o400 for p in paths)
        assert all(
            load_profile(first[n].profile_path).scope
            == load_profile(configured[n].profile_path).scope
            for n in configured
        )
        assert all(first[n].auth_file == configured[n].auth_file for n in configured)
    assert all(not p.exists() for p in paths)
    with session_actors(configured) as second:
        ids.extend(load_profile(a.profile_path).session_id for a in second.values())
    assert len(set(ids)) == 4
    assert all(a.auth_file.exists() for a in configured.values())


def write_config(tmp_path: Path, configured: dict[str, ChatActor]) -> Path:
    data: dict[str, object] = {"schema": "cairn.chat/v1"}
    for name, actor in configured.items():
        data[name] = {
            "executable": str(actor.executable),
            "auth_file": str(actor.auth_file),
            "auth_state_dir": str(actor.auth_state_dir),
            "profile_path": str(actor.profile_path),
            "expected_principal": str(actor.expected_principal),
            "model": actor.model,
        }
    path = tmp_path / "chat.json"
    path.write_text(json.dumps(data))
    return path


def test_load_explicit_config_and_reject_scope_mismatch(tmp_path: Path) -> None:
    configured = actors(tmp_path)
    path = write_config(tmp_path, configured)
    assert load_config(path) == configured
    profile = configured["spike"].profile_path
    data = json.loads(profile.read_text())
    data["scope"]["realm"] = "another"
    profile.write_text(json.dumps(data))
    with pytest.raises(ChatConfigError):
        load_config(path)


@pytest.mark.parametrize("mutation", ["extra", "relative", "duplicate", "principal"])
def test_unsafe_config_is_rejected(tmp_path: Path, mutation: str) -> None:
    path = write_config(tmp_path, actors(tmp_path))
    data = json.loads(path.read_text())
    if mutation == "extra":
        data["shell"] = "run something"
    elif mutation == "relative":
        data["val"]["auth_file"] = "auth.json"
    elif mutation == "principal":
        data["spike"]["expected_principal"] = data["val"]["expected_principal"]
    else:
        path.write_text('{"schema":"cairn.chat/v1",' + path.read_text()[1:])
        with pytest.raises(ChatConfigError):
            load_config(path)
        return
    path.write_text(json.dumps(data))
    with pytest.raises(ChatConfigError):
        load_config(path)


def test_deep_invalid_config_has_a_closed_error(tmp_path: Path) -> None:
    path = tmp_path / "nested.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ChatConfigError):
        load_config(path)


def test_config_refuses_normal_provider_state_without_reading_auth(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path, actors(tmp_path))
    data = json.loads(path.read_text())
    shared = Path.home() / ".codex"
    data["val"]["auth_state_dir"] = str(shared)
    data["val"]["auth_file"] = str(shared / "auth.json")
    path.write_text(json.dumps(data))

    with pytest.raises(ChatConfigError):
        load_config(path)


@pytest.mark.parametrize(
    ("arguments", "code", "expected"),
    [
        (
            ["cairn-chat", "--config", "/tmp/chat.json"],
            "host_auth_locked",
            "Authentication state is in use",
        ),
        (
            ["cairn-chat", "--config", "/tmp/chat.json", "--recover-auth"],
            "host_auth_recovery_required",
            "Dedicated authentication recovery required",
        ),
    ],
)
def test_cli_reports_dedicated_auth_lifecycle_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: str,
    expected: str,
) -> None:
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(chat, "load_config", lambda path: {})

    @contextmanager
    def blocked(actors: dict[str, ChatActor]) -> Iterator[dict[str, ChatActor]]:
        raise ChatHostError(code)
        yield {}

    if "--recover-auth" in arguments:
        monkeypatch.setattr(
            chat,
            "recover_actor_auth",
            lambda actors: (_ for _ in ()).throw(ChatHostError(code)),
        )
    else:
        monkeypatch.setattr(chat, "session_actors", blocked)
    with pytest.raises(SystemExit) as raised:
        chat.run()
    assert raised.value.code == 2
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("host_timeout", "host_timeout"),
        ("host_authentication_failed", "host_authentication_failed"),
        ("secret-provider-message", "host_failed"),
    ],
)
def test_failure_displays_closed_reason_and_elapsed_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, expected: str
) -> None:
    clock = iter([100.0, 340.0])
    monkeypatch.setattr("cairn.client.chat.time.monotonic", lambda: next(clock))

    def fail(actor: ChatActor, task: bytes) -> str:
        raise ChatHostError(code)

    output = io.StringIO()
    assert (
        console(actors(tmp_path), io.BytesIO(b"hello\n/send\n"), output, invoke=fail)
        == 1
    )
    assert f"Reason: {expected}; elapsed: 240.0 s" in output.getvalue()
    assert "secret-provider-message" not in output.getvalue()


def test_preflight_failure_reports_no_provider_launch(tmp_path: Path) -> None:
    def fail(actor: ChatActor, task: bytes) -> str:
        raise ChatHostError("memory_connection_unavailable")

    output = io.StringIO()
    assert (
        console(actors(tmp_path), io.BytesIO(b"hello\n/send\n"), output, invoke=fail)
        == 1
    )
    assert "No agent launched; no turn writes were attempted." in output.getvalue()
    assert "writes may have committed" not in output.getvalue()


@pytest.mark.parametrize(
    ("status", "expected", "exit_code"),
    [
        ("none", "No facts saved from this turn", 0),
        ("verified", "1 saved fact verified by read-back", 0),
        ("partial", "Memory persistence is partial", 1),
        ("unknown", "Memory persistence is unknown", 1),
    ],
)
def test_trusted_memory_receipt_is_separate_from_prose(
    tmp_path: Path,
    status: Literal["none", "verified", "partial", "unknown"],
    expected: str,
    exit_code: int,
) -> None:
    from cairn.client.chat_host import ChatTurn
    from cairn.client.turn_receipts import TurnMemory

    fact = "10000000-0000-4000-8000-000000000001"
    memory = TurnMemory(
        status=status,
        fact_ids=(fact,) if status != "none" else (),
        attempted=0 if status == "none" else 1,
    )
    output = io.StringIO()
    calls = []

    def turn(actor: ChatActor, task: bytes) -> ChatTurn:
        calls.append(task)
        return ChatTurn(
            response="I saved absolutely everything.",
            memory=memory,
            completion="complete",
        )

    assert (
        console(
            actors(tmp_path), io.BytesIO(b"hello\n/send\n/quit\n"), output, invoke=turn
        )
        == exit_code
    )
    assert expected in output.getvalue()
    assert output.getvalue().index(expected) > output.getvalue().index(
        "I saved absolutely everything."
    )
    assert calls == [b"hello\n"]
    if exit_code:
        assert fact in output.getvalue()
        assert "Stopped without retry" in output.getvalue()


def test_plain_response_cannot_claim_verified_memory(tmp_path: Path) -> None:
    output = io.StringIO()
    calls = []

    def turn(actor: ChatActor, task: bytes) -> str:
        calls.append(task)
        return "Memory verified: saved everything"

    assert (
        console(
            actors(tmp_path),
            io.BytesIO(b"one\n/send\ntwo\n/send\n"),
            output,
            invoke=turn,
        )
        == 1
    )
    assert "Memory persistence is unknown" in output.getvalue()
    assert calls == [b"one\n"]


@pytest.mark.parametrize("completion", ["unchecked", "incomplete"])
def test_semantic_check_cannot_be_replaced_by_storage_receipt(
    tmp_path: Path, completion: Literal["unchecked", "incomplete"]
) -> None:
    output = io.StringIO()
    calls = []

    def turn(actor: ChatActor, task: bytes) -> ChatTurn:
        calls.append(task)
        return ChatTurn(
            "Saved.", TurnMemory("verified", ("known",), 1), completion=completion
        )

    assert (
        console(
            actors(tmp_path),
            io.BytesIO(b"first\n/send\nsecond\n/send\n"),
            output,
            invoke=turn,
        )
        == 1
    )
    assert calls == [b"first\n"]
    assert "1 saved fact verified by read-back" in output.getvalue()
    assert "Memory check: incomplete" in output.getvalue()


def test_explicit_assessment_model_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, actors(tmp_path))
    config = json.loads(path.read_text())
    config["spike"]["assessment_model"] = "sonnet"
    path.write_text(json.dumps(config))
    assert load_config(path)["spike"].assessment_model == "sonnet"
    config["spike"]["assessment_model"] = "--unsafe argument"
    path.write_text(json.dumps(config))
    with pytest.raises(ChatConfigError):
        load_config(path)
