"""Bounded bridge tests. Fake runners below prove protocol handling only."""

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import anyio
import anyio.lowlevel
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import checkpoint, profile
from test_memory_cli_process import server

from cairn.catalogue.sqlite import read_connection
from cairn.client.host_installation import install_host_workflow

ROOT = Path(__file__).resolve().parents[2]
jsonschema = importlib.import_module("jsonschema")


@pytest.fixture(autouse=True)
def private_staging_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def bridge() -> Any:
    name = "scripts.host_workflow_bridge"
    assert importlib.util.find_spec(name) is not None, "bounded bridge missing"
    return importlib.import_module(name)


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"\xff", b"{} trailing"]
)
def test_strict_decoder_never_releases_invalid_input(bridge: Any, raw: bytes) -> None:
    with pytest.raises(bridge.BridgeFailure, match="^invalid_message$"):
        bridge.decode_json(raw)


def test_raw_limit_precedes_json_decode(bridge: Any) -> None:
    with pytest.raises(bridge.BridgeFailure, match="^message_limit$"):
        bridge.decode_message(b" " * (bridge.MAX_MESSAGE_BYTES + 1))


def test_config_rejects_unbounded_or_input_controlled_authority(bridge: Any) -> None:
    for value in (
        {"provider": "codex", "skill_sha256": "0" * 64, "allowed_commands": []},
        {"provider": "other", "skill_sha256": "0" * 64, "allowed_commands": ["check"]},
        {
            "provider": "codex",
            "skill_sha256": "0" * 64,
            "allowed_commands": ["proposal-*"],
        },
        {"provider": "codex", "skill_sha256": "bad", "allowed_commands": ["check"]},
        {
            "provider": "codex",
            "skill_sha256": "0" * 64,
            "allowed_commands": ["check"],
            "env": {},
        },
    ):
        with pytest.raises(bridge.BridgeFailure, match="^invalid_configuration$"):
            bridge.BridgeConfig.from_value(value)


@pytest.mark.anyio
async def test_fake_runner_protocol_preserves_actual_exit_and_typed_recovery(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.host_workflow_sandbox import Capture

    error = {
        "schema": "cairn.memory-command/v1",
        "command": "remember",
        "result": {
            "error": {"code": "transport_error", "operation": "turn-prepare"},
            "last_confirmed_stage": "started",
            "recovery": "resubmit_identical_checkpoint_same_identities",
        },
    }

    def runner(argv: tuple[str, ...], *, stdin: bytes, deadline: float) -> Capture:
        assert argv == ("--profile", "/cli/profile.json", "remember")
        assert stdin == b"$(touch sentinel)\n`whoami`"
        return Capture(3, b"", json.dumps(error).encode(), len(stdin))

    monkeypatch.setattr(bridge, "run_cli", runner)
    app = bridge.Bridge(bridge.BridgeConfig("codex", "0" * 64, frozenset({"remember"})))
    result = await app.call(
        "run_daily_cli",
        {
            "argv": ["--profile", "/cli/profile.json", "remember"],
            "stdin": "$(touch sentinel)\n`whoami`",
        },
    )
    value = json.loads(result.content[0].text)
    assert result.isError
    assert value["exit_code"] == 3
    assert json.loads(value["stderr"]) == error
    assert value["untrusted_data"] is True


@pytest.mark.anyio
async def test_fake_runner_protocol_suppresses_unrecognised_stderr(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.host_workflow_sandbox import Capture

    monkeypatch.setattr(
        bridge, "run_cli", lambda *a, **kw: Capture(4, b"", b"private diagnostic", 0)
    )
    app = bridge.Bridge(bridge.BridgeConfig("codex", "0" * 64, frozenset({"check"})))
    result = await app.call(
        "run_daily_cli",
        {"argv": ["--profile", "/cli/profile.json", "check"], "stdin": ""},
    )
    value = json.loads(result.content[0].text)
    assert result.isError
    assert value["exit_code"] == 4
    assert value["stderr_bytes"] == 18
    assert "private diagnostic" not in result.content[0].text
    assert value["error"] == "unrecognised_cli_output"


def test_regular_reader_complete_limit_and_path_refusals(
    bridge: Any, tmp_path: Path
) -> None:
    skill = tmp_path / "SKILL.md"
    raw = b"x" * 65536
    skill.write_bytes(raw)
    assert bridge.read_regular(str(skill), 65536) == raw
    skill.write_bytes(raw + b"x")
    with pytest.raises(bridge.BridgeFailure):
        bridge.read_regular(str(skill), 65536)
    skill.write_bytes(b"full\ntext\n")
    alias = tmp_path / "alias"
    alias.symlink_to(skill)
    directory_alias = tmp_path / "directory_alias"
    directory_alias.symlink_to(tmp_path, target_is_directory=True)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in (
        alias,
        directory_alias / "SKILL.md",
        fifo,
        tmp_path,
        tmp_path / "missing",
    ):
        with pytest.raises(bridge.BridgeFailure):
            bridge.read_regular(str(path), 65536)


def subprocess_parameters(extra: str = "") -> StdioServerParameters:
    # Fixed test harness, no provider or CLI launch. This exercises the actual
    # SDK subprocess transport; it makes no filesystem-confinement claim.
    program = (
        "import sys, anyio; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "from scripts.host_workflow_bridge import BridgeConfig, serve; "
        + extra
        + "anyio.run(serve, BridgeConfig('codex', '0'*64, frozenset({'check'})))"
    )
    return StdioServerParameters(
        command=sys.executable, args=["-I", "-c", program], env={}
    )


@pytest.mark.anyio
async def test_actual_sdk_stdio_initialization_closed_inventory_and_safe_refusal(
    bridge: Any,
) -> None:
    assert hasattr(bridge, "serve"), "bounded SDK transport missing"
    async with stdio_client(subprocess_parameters()) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "cairn-acceptance-bridge"
            inventory = (await client.list_tools()).tools
            assert [tool.name for tool in inventory] == [
                "read_installed_skill",
                "run_daily_cli",
            ]
            for tool in inventory:
                assert tool.inputSchema["additionalProperties"] is False
            assert inventory[1].annotations is not None
            assert inventory[1].annotations.readOnlyHint is False
            assert inventory[1].annotations.destructiveHint is True
            for argv in (
                ["--profile", "/host/secret", "check"],
                ["remember", "--help"],
                ["check", "--extra"],
            ):
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(
                        {"argv": argv, "stdin": ""}, inventory[1].inputSchema
                    )
            for arguments in (
                {"argv": ["--profile", "/host/secret", "check"], "stdin": ""},
                {"argv": ["--help"], "stdin": "", "env": {"secret": "canary"}},
                {"argv": ["--profile", "/cli/profile.json", "remember"], "stdin": ""},
            ):
                result = await client.call_tool("run_daily_cli", arguments)
                assert result.isError
                assert "canary" not in result.model_dump_json()
                assert "/host/secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","id":1,"method":"ping","id":2}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"secret":NaN}}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":[]}}\n',
        b"\xff\n",
        b"{} trailing\n",
    ],
)
def test_actual_stdio_malformed_messages_are_fixed_errors(
    bridge: Any, raw: bytes
) -> None:
    assert hasattr(bridge, "serve"), "bounded SDK transport missing"
    params = subprocess_parameters()
    process = subprocess.run(
        [params.command, *params.args],
        input=raw,
        capture_output=True,
        env={},
        timeout=5,
    )
    assert process.stderr == b""
    response = json.loads(process.stdout)
    assert response["error"]["message"] == "Invalid request"
    assert "secret" not in process.stdout.decode()


@pytest.mark.anyio
async def test_skill_exact_full_digest_utf8_and_size_protocol(
    bridge: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Narrow reader integration: redirect only the fixed path open to a synthetic
    # local file. The real jailed native path is exercised by the E2E fixture.
    skill = tmp_path / "SKILL.md"
    raw = ("complete reviewed skill\n雪\n" * 100).encode()
    skill.write_bytes(raw)
    original_reader = bridge.read_regular

    def reader(path: str, limit: int) -> bytes:
        assert path == "/work/.agents/skills/cairn-memory/SKILL.md"
        value = original_reader(str(skill), limit)
        assert isinstance(value, bytes)
        return value

    monkeypatch.setattr(bridge, "read_regular", reader)
    app = bridge.Bridge(
        bridge.BridgeConfig(
            "codex", hashlib.sha256(raw).hexdigest(), frozenset({"check"})
        )
    )
    request = {"path": "/work/.agents/skills/cairn-memory/SKILL.md"}
    result = await app.call("read_installed_skill", request)
    assert not result.isError
    assert json.loads(result.content[0].text)["text"].encode() == raw
    skill.write_bytes(raw + b"changed")
    assert (await app.call("read_installed_skill", request)).isError
    for malformed in (b"\xff", b"x" * 65537):
        skill.write_bytes(malformed)
        app = bridge.Bridge(
            bridge.BridgeConfig(
                "codex", hashlib.sha256(malformed).hexdigest(), frozenset({"check"})
            )
        )
        assert (await app.call("read_installed_skill", request)).isError
    assert (await app.call("read_installed_skill", {"path": "/host/secret"})).isError


@pytest.mark.anyio
async def test_sdk_stdio_tool_call_lifetime_limit(bridge: Any) -> None:
    async with stdio_client(subprocess_parameters()) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            for _ in range(bridge.MAX_TOOL_CALLS):
                assert (
                    await client.call_tool(
                        "run_daily_cli", {"argv": ["forbidden"], "stdin": ""}
                    )
                ).isError
            with pytest.raises(McpError, match="Invalid request"):
                await client.call_tool(
                    "run_daily_cli", {"argv": ["forbidden"], "stdin": ""}
                )


@pytest.mark.anyio
async def test_sdk_stdio_fake_runner_concurrency_refuses_before_dispatch(
    bridge: Any,
) -> None:
    # Narrow transport test only: controlled slow runner, no CLI/custody claim.
    setup = (
        "import time; import scripts.host_workflow_bridge as b; "
        "from scripts.host_workflow_sandbox import Capture; "
        "b.run_cli=lambda *a, **kw: (time.sleep(0.5), Capture(0,b'help',b'',0))[1]; "
    )
    async with stdio_client(subprocess_parameters(setup)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            answers: list[object] = []

            async def help_call() -> None:
                answers.append(
                    await client.call_tool(
                        "run_daily_cli", {"argv": ["--help"], "stdin": ""}
                    )
                )

            async with anyio.create_task_group() as group:
                group.start_soon(help_call)
                await anyio.sleep(0.1)
                with pytest.raises(McpError, match="Invalid request"):
                    await client.send_ping()
            assert len(answers) == 1


@pytest.mark.anyio
async def test_raw_pipe_exact_bound_and_overflow_without_newline(bridge: Any) -> None:
    for extra in (0, 1):
        read_fd, write_fd = os.pipe()
        raw = b" " * (bridge.MAX_MESSAGE_BYTES + extra)

        async def writer(
            fd: int = write_fd, data: bytes = raw + (b"\n" if not extra else b"")
        ) -> None:
            try:
                await anyio.to_thread.run_sync(_write_all, fd, data)
            finally:
                os.close(fd)

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(writer)
                reader = bridge.RawLines(read_fd)
                if extra:
                    with pytest.raises(bridge.BridgeFailure, match="message_limit"):
                        await reader.read()
                else:
                    assert await reader.read() == raw
        finally:
            os.close(read_fd)


def _write_all(fd: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        offset += os.write(fd, raw[offset : offset + 65536])


def test_malformed_stdout_preserves_recognised_committed_diagnostic(
    bridge: Any,
) -> None:
    from scripts.host_workflow_protocol import validate_daily_request
    from scripts.host_workflow_sandbox import Capture

    error = {
        "schema": "cairn.memory-command/v1",
        "command": "remember",
        "result": {
            "error": {"code": "output_failure", "operation": "output"},
            "last_confirmed_stage": "committed",
        },
    }
    invocation = validate_daily_request(
        {"argv": ["--profile", "/cli/profile.json", "remember"], "stdin": "{}"},
        allowed_commands=frozenset({"remember"}),
    )
    result = bridge.cli_result(
        Capture(4, b"{truncated", json.dumps(error).encode(), 2), invocation
    )
    value = json.loads(result.content[0].text)
    assert result.isError
    assert value["exit_code"] == 4
    assert json.loads(value["stderr"]) == error
    assert "truncated" not in result.content[0].text


@pytest.mark.parametrize(
    "change",
    [
        {"recovery": "private-secret"},
        {"last_confirmed_stage": "private-secret"},
        {"error": {"code": "private-secret", "operation": "output"}},
        {"extra": "private-secret"},
    ],
)
def test_diagnostic_recognition_is_closed_not_just_json(
    bridge: Any, change: dict[str, Any]
) -> None:
    result = {
        "error": {"code": "output_failure", "operation": "output"},
        "last_confirmed_stage": "committed",
    }
    result.update(change)
    raw = json.dumps(
        {"schema": "cairn.memory-command/v1", "command": "remember", "result": result}
    ).encode()
    assert not bridge.recognised_error(raw, "remember")


def test_actual_stdio_overflow_and_message_flood_are_finite(bridge: Any) -> None:
    params = subprocess_parameters()
    for raw in (
        b" " * (bridge.MAX_MESSAGE_BYTES + 1),
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        * (bridge.MAX_MESSAGES + 1),
    ):
        process = subprocess.run(
            [params.command, *params.args],
            input=raw,
            capture_output=True,
            env={},
            timeout=5,
        )
        assert process.returncode == 0
        assert process.stderr == b""
        response = json.loads(process.stdout)
        assert response["error"]["message"] == "Invalid request"


@pytest.mark.anyio
async def test_fake_runner_input_exact_limit_and_no_followup(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.host_workflow_sandbox import Capture

    launches: list[int] = []

    def runner(argv: tuple[str, ...], *, stdin: bytes, deadline: float) -> Capture:
        launches.append(len(stdin))
        return Capture(2, b"", b"", len(stdin))

    monkeypatch.setattr(bridge, "run_cli", runner)
    app = bridge.Bridge(bridge.BridgeConfig("codex", "0" * 64, frozenset({"remember"})))
    for size in (1048576, 1048577):
        result = await app.call(
            "run_daily_cli",
            {
                "argv": ["--profile", "/cli/profile.json", "remember"],
                "stdin": "x" * size,
            },
        )
        assert result.isError
    await anyio.lowlevel.checkpoint()
    assert launches == [1048576]


def test_fake_capture_output_bound_and_complete_untrusted_data(bridge: Any) -> None:
    from scripts.host_workflow_protocol import validate_daily_request
    from scripts.host_workflow_sandbox import Capture

    invocation = validate_daily_request(
        {"argv": ["--help"], "stdin": ""}, allowed_commands=frozenset({"check"})
    )
    for size in (1048576, 1048577):
        result = bridge.cli_result(Capture(0, b"x" * size, b"", 0), invocation)
        value = json.loads(result.content[0].text)
        assert value["stdout_bytes"] == size
        assert value["untrusted_data"] is True
        if size == 1048576:
            assert not result.isError
            assert value["stdout"] == "x" * size
        else:
            assert result.isError
            assert "stdout" not in value


@pytest.mark.anyio
async def test_eof_cancels_session_without_work(
    bridge: Any,
) -> None:
    read_fd, write_fd = os.pipe()
    output_read, output_write = os.pipe()
    config = bridge.BridgeConfig("codex", "0" * 64, frozenset({"check"}))
    os.close(write_fd)
    try:
        with anyio.fail_after(2):
            await bridge.serve(config, read_fd, output_write)
    finally:
        os.close(read_fd)
        os.close(output_write)
        os.close(output_read)


@pytest.mark.anyio
async def test_broken_output_closes_transport_without_protocol_traceback(
    bridge: Any,
) -> None:
    read_fd, write_fd = os.pipe()
    output_read, output_write = os.pipe()
    os.close(output_read)
    os.write(write_fd, b"invalid-json\n")
    os.close(write_fd)
    try:
        with anyio.fail_after(2), pytest.raises(ExceptionGroup) as failure:
            await bridge.serve(
                bridge.BridgeConfig("codex", "0" * 64, frozenset({"check"})),
                read_fd,
                output_write,
            )
        assert all(
            isinstance(error, BrokenPipeError) for error in failure.value.exceptions
        )
    finally:
        os.close(read_fd)
        os.close(output_write)


@pytest.mark.anyio
async def test_idle_transport_has_controller_lifetime_deadline(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    output_read, output_write = os.pipe()
    monkeypatch.setattr(bridge, "SESSION_DEADLINE", 0.05)
    try:
        with anyio.fail_after(2):
            await bridge.serve(
                bridge.BridgeConfig("codex", "0" * 64, frozenset({"check"})),
                read_fd,
                output_write,
            )
    finally:
        for descriptor in (read_fd, write_fd, output_read, output_write):
            os.close(descriptor)


def test_json_escape_worst_case_fits_raw_budget(bridge: Any) -> None:
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "run_daily_cli",
                "arguments": {
                    "argv": ["--profile", "/cli/profile.json", "remember"],
                    "stdin": "\x01" * 1048576,
                },
            },
        }
    ).encode()
    assert (
        bridge.decode_message(raw).root.params["arguments"]["stdin"] == "\x01" * 1048576
    )


@pytest.mark.parametrize(
    "code",
    [
        "connection_refused",
        "invalid_arguments",
        "expected_instance_required",
        "client_context_changed",
        "invalid_preparation",
    ],
)
def test_recognised_cli_local_failure_vocabulary(bridge: Any, code: str) -> None:
    raw = json.dumps(
        {
            "schema": "cairn.memory-command/v1",
            "command": "remember",
            "result": {
                "error": {"code": code, "operation": "input"},
                "last_confirmed_stage": "unconfirmed",
            },
        }
    ).encode()
    assert bridge.recognised_error(raw, "remember")


@pytest.mark.parametrize(
    "command", ["propose", "proposal-accept", "proposal-reject", "disagree"]
)
def test_proposal_recovery_diagnostics_are_closed_and_preserved(
    bridge: Any, command: str
) -> None:
    from scripts.host_workflow_protocol import validate_daily_request
    from scripts.host_workflow_sandbox import Capture

    invocation = validate_daily_request(
        {"argv": ["--profile", "/cli/profile.json", command], "stdin": "{}"},
        allowed_commands=frozenset({command}),
    )
    error: dict[str, Any] = {
        "schema": "cairn.memory-command/v1",
        "command": command,
        "result": {
            "error": {"code": "transport_error", "operation": command},
            "last_confirmed_stage": "unconfirmed",
            "recovery": f"resubmit_identical_{command}_same_idempotency_key_and_fields",
        },
    }
    raw = json.dumps(error).encode()
    result = bridge.cli_result(Capture(3, b"", raw, 2), invocation)
    value = json.loads(result.content[0].text)
    assert result.isError and value["exit_code"] == 3
    assert json.loads(value["stderr"]) == error
    error["result"]["recovery"] += "_private-secret"
    refused = bridge.cli_result(
        Capture(3, b"", json.dumps(error).encode(), 2), invocation
    )
    assert refused.isError and "private-secret" not in refused.model_dump_json()


# Controller-owned input to this test driver is a finite synthetic scenario.
# This driver is not shipped or callable through the bridge's tool interface.
SDK_DRIVER = r"""
import json, sys, hashlib
from pathlib import Path
sys.path.insert(0, '/runtime/cli/site-packages')
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

scenario = json.load(sys.stdin)
async def exercise():
    summary = []
    async with stdio_client(StdioServerParameters(command='/runtime/host/bridge-entry', env={})) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == ['read_installed_skill', 'run_daily_cli']
            skill_path = '/work/' + ('.agents' if scenario['provider'] == 'codex' else '.claude') + '/skills/cairn-memory/SKILL.md'
            skill = await client.call_tool('read_installed_skill', {'path': skill_path})
            assert not skill.isError
            skill_data = json.loads(skill.content[0].text)
            assert skill_data['text'].encode() == Path(skill_path).read_bytes()
            assert skill_data['sha256'] == scenario['skill_sha256']
            assert hashlib.sha256(skill_data['text'].encode()).hexdigest() == scenario['skill_sha256']
            for item in scenario['requests']:
                called = await client.call_tool('run_daily_cli', {'argv': item['argv'], 'stdin': item['stdin']})
                value = json.loads(called.content[0].text)
                assert value['source'] == 'cli'
                assert value['exit_code'] == item['exit_code']
                assert called.isError == (item['exit_code'] != 0)
                assert value['input_bytes'] == len(item['stdin'].encode())
                assert value['untrusted_data'] is True
                safe = {'command': item['command'], 'exit_code': value['exit_code'],
                        'input_bytes': value['input_bytes'], 'stdout_bytes': value['stdout_bytes'],
                        'stderr_bytes': value['stderr_bytes']}
                if item['command'] == 'help':
                    assert 'usage:' in value['stdout']
                    assert not value['stderr']
                elif item['exit_code'] != 0:
                    error = json.loads(value['stderr'])['result']
                    assert error['error']['code'] == item['error_code']
                    if 'stage' in item:
                        assert error['last_confirmed_stage'] == item['stage']
                    if 'recovery' in item:
                        assert error['recovery'] == item['recovery']
                    safe['diagnostic'] = error
                else:
                    result = json.loads(value['stdout'])['result']
                    if item['command'] == 'check':
                        assert result['principal_id'] == scenario['principal_id']
                        assert result['instance_id'] == scenario['instance_id']
                    if item['command'] in ('remember', 'resume'):
                        assert result['state'] == 'committed'
                        assert result['completed_turn']['response'] == scenario['completed_response']
                        safe['receipt'] = result['persistence']
                    if item['command'] == 'status':
                        assert result['state'] == item.get('state', 'committed')
                    if item['command'] in ('propose', 'proposal-accept', 'proposal-reject'):
                        safe['proposal_receipt'] = result
                    if item['command'] == 'proposal-read':
                        safe['proposal_id'] = result['proposal_id']
                        safe['proposal_state'] = result['state']
                        safe['decision'] = result['decision']
                    if item['command'] == 'proposal-list':
                        safe['proposal_ids'] = [p['proposal_id'] for p in result['items']]
                        safe['next_cursor'] = result['next_cursor']
                    if item['command'] == 'disagree':
                        safe['disagreement_receipt'] = result
                    if item['command'] == 'suggest':
                        safe['suggestions'] = result['items']
                    if item['command'] == 'history':
                        safe['disagreements'] = result['data']['disagreements']
                summary.append(safe)
    print(json.dumps({'skill_sha256': scenario['skill_sha256'], 'commands': summary}))
anyio.run(exercise)
"""


def stage_bridge(stage: Path) -> None:
    for name in ("host-runtime", "host-config", "cli-config", "work"):
        (stage / name).mkdir(mode=0o700)
    modules = stage / "host-runtime/scripts"
    modules.mkdir(mode=0o700)
    for name in (
        "host_workflow_bridge.py",
        "host_workflow_protocol.py",
        "host_workflow_sandbox.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, modules / name)
        (modules / name).chmod(0o600)
    entry = stage / "host-runtime/entry"
    entry.write_text("#!/usr/bin/python3 -I\n" + textwrap.dedent(SDK_DRIVER))
    entry.chmod(0o700)
    bridge_entry = stage / "host-runtime/bridge-entry"
    bridge_entry.write_text(
        "#!/usr/bin/python3 -I\n"
        "import sys\n"
        "sys.path.insert(0, '/runtime/cli/site-packages')\n"
        "sys.path.insert(0, '/runtime/host')\n"
        "from scripts.host_workflow_bridge import main\n"
        "main()\n"
    )
    bridge_entry.chmod(0o700)


CANCEL_DRIVER = r"""
import json, select, subprocess, sys, time
command = json.load(sys.stdin)
process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env={})
def send(value):
    process.stdin.write(json.dumps(value).encode() + b'\n')
    process.stdin.flush()
def receive():
    assert select.select([process.stdout], [], [], 3)[0], 'response timeout'
    line = process.stdout.readline()
    return json.loads(line) if line else None
send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
    'protocolVersion':'2025-11-25','capabilities':{},
    'clientInfo':{'name':'cancellation-regression','version':'1'}}})
assert 'result' in receive()
send({'jsonrpc':'2.0','method':'notifications/initialized'})
def help_call(identity):
    send({'jsonrpc':'2.0','id':identity,'method':'tools/call','params':{
        'name':'run_daily_cli','arguments':{'argv':['--help'],'stdin':''}}})
help_call(2)
time.sleep(.2)
send({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':2}})
cancel = receive()
assert cancel['id'] == 2 and 'error' in cancel
# The bounded runner must finish before the next command; the cancellation
# response itself does not prove cleanup or any storage outcome.
time.sleep(1.1)
alive = process.poll() is None
ping = allowed = False
if alive:
    send({'jsonrpc':'2.0','id':3,'method':'ping'})
    answer = receive()
    ping = bool(answer and answer['id'] == 3 and 'result' in answer)
    if ping:
        help_call(4)
        answer = receive()
        assert answer['id'] == 4
        result = answer['result']
        value = json.loads(result['content'][0]['text'])
        allowed = not result.get('isError', False) and value['exit_code'] == 0 and value['stdout'].strip() == 'help'
process.stdin.close()
process.wait(timeout=3)
stderr = process.stderr.read()
print(json.dumps({'alive_after_cancel':alive,'ping':ping,'allowed_call':allowed,
                  'bridge_exit':process.returncode,'stderr_bytes':len(stderr)}))
"""


def assert_cancel_session_survives(raw: bytes) -> None:
    assert json.loads(raw) == {
        "alive_after_cancel": True,
        "ping": True,
        "allowed_call": True,
        "bridge_exit": 0,
        "stderr_bytes": 0,
    }


@pytest.mark.parametrize("failure", ["none", "sandbox", "unexpected"])
def test_active_sdk_cancel_fake_runner_preserves_session(failure: str) -> None:
    # Narrow protocol/lifetime regression, explicitly not actual CLI evidence.
    setup = textwrap.dedent(f"""
        import time
        import scripts.host_workflow_bridge as b
        calls = 0
        def runner(*args, **kwargs):
            global calls
            calls += 1
            time.sleep(.7)
            if calls == 1 and {failure!r} == 'sandbox':
                raise b.SandboxFailure('sandbox_timeout')
            if calls == 1 and {failure!r} == 'unexpected':
                raise RuntimeError('synthetic failure')
            return b.Capture(0, b'help', b'', 0)
        b.run_cli = runner
    """)
    parameters = subprocess_parameters(setup)
    result = subprocess.run(
        [sys.executable, "-I", "-c", CANCEL_DRIVER],
        input=json.dumps([parameters.command, *parameters.args]).encode(),
        capture_output=True,
        env={},
        timeout=10,
    )
    assert result.returncode == 0 and result.stderr == b""
    assert_cancel_session_survives(result.stdout)


@pytest.mark.host_isolation
def test_active_sdk_cancel_jailed_main_preserves_session(tmp_path: Path) -> None:
    from host_workflow_fixture import build_cli_runtime

    from scripts.host_workflow_sandbox import run_host, seal

    stage = tmp_path / "cancel-stage"
    stage.mkdir(mode=0o700)
    stage_bridge(stage)
    build_cli_runtime(stage / "cli-runtime")
    # Synthetic delayed CLI stand-in isolates real nested jail cleanup. The
    # actual main()/SDK/protocol/run_cli paths are unchanged. No custody claim.
    (stage / "cli-runtime/entry").write_text(
        "#!/usr/bin/python3 -I\nimport time\ntime.sleep(.7)\nprint('help')\n"
    )
    (stage / "host-runtime/entry").write_text("#!/usr/bin/python3 -I\n" + CANCEL_DRIVER)
    (stage / "host-config/bridge.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "skill_sha256": "0" * 64,
                "allowed_commands": ["check"],
            }
        )
    )
    result = run_host(seal(stage), stdin=b'["/runtime/host/bridge-entry"]', deadline=10)
    assert result.returncode == 0 and result.stderr == b""
    assert_cancel_session_survives(result.stdout)


@pytest.mark.host_isolation
def test_actual_jailed_sdk_bridge_and_nested_wheel_cli(
    tmp_path: Path, memory_support: ModuleType, bridge: Any
) -> None:
    # Run only after Main releases its immutable production runtime fixture.
    # This is scripted SDK acceptance, not Codex/Claude native host discovery.
    builder = importlib.import_module("host_workflow_fixture")
    from scripts.host_workflow_sandbox import run_host, seal

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    stage_bridge(stage)
    runtime = builder.build_cli_runtime(stage / "cli-runtime")
    assert runtime.file_count < 8100
    assert len(runtime.wheel_sha256) == 64
    instance = memory_support.Instance(tmp_path / "instance", attic=False)
    segments = [
        {"kind": "repository", "identifier": "cairn"},
        {"kind": "composite-run", "identifier": str(uuid4())},
        {"kind": "job", "identifier": "bridge-test"},
    ]
    principal, token = instance.add_actor(
        operations=["ingest", "retrieve"], segments=segments, read_clearance="internal"
    )
    # Synthetic catalogue setup changes this worker's umask to 0007. Restore
    # private staging before creating any subsequently sealed config/skill files.
    os.umask(0o077)
    receipts: list[str] = []
    fingerprints: list[str] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        supplied = profile(
            tmp_path, instance, token, f"http://127.0.0.1:{listener.getsockname()[1]}"
        )
        config = json.loads(supplied.read_bytes())
        config["credential_file"] = "credential"
        config["scope"]["segments"] = segments
        (stage / "cli-config/profile.json").write_text(json.dumps(config))
        (stage / "cli-config/credential").write_text(token)
        with server(instance, listener):
            for provider in ("codex", "claude"):
                installation = install_host_workflow(
                    provider,
                    stage / "work",
                    assets_root=ROOT / "integrations",
                    apply=True,
                )
                digest = hashlib.sha256(
                    (installation.path / "SKILL.md").read_bytes()
                ).hexdigest()
                (stage / "host-config/bridge.json").write_text(
                    json.dumps(
                        {
                            "provider": provider,
                            "skill_sha256": digest,
                            "allowed_commands": sorted(bridge.COMMANDS),
                        }
                    )
                )
                value = checkpoint()
                value["response"] = (
                    "Exact completed output.\n$(touch /scratch/not-executed) `whoami` 雪"
                )
                requests: list[dict[str, Any]] = [
                    {"command": "help", "argv": ["--help"], "stdin": "", "exit_code": 0}
                ]
                requests.extend(
                    {
                        "command": "help",
                        "argv": [command, "--help"],
                        "stdin": "",
                        "exit_code": 0,
                    }
                    for command in sorted(bridge.COMMANDS)
                )
                for command, content, code, error in (
                    ("check", "", 0, None),
                    ("remember", "{invalid-json", 2, "invalid_input"),
                    ("remember", json.dumps(value), 0, None),
                    ("status", json.dumps({"turn_id": value["turn_id"]}), 0, None),
                    ("resume", json.dumps({"turn_id": value["turn_id"]}), 0, None),
                    (
                        "correct",
                        json.dumps(
                            {
                                "fact_ids": [str(uuid4())],
                                "reason": "synthetic refusal",
                                "idempotency_key": str(uuid4()),
                            }
                        ),
                        2,
                        "authorisation_denied",
                    ),
                ):
                    requests.append(
                        {
                            "command": command,
                            "argv": ["--profile", "/cli/profile.json", command],
                            "stdin": content,
                            "exit_code": code,
                            "error_code": error,
                        }
                    )
                scenario = {
                    "provider": provider,
                    "skill_sha256": digest,
                    "principal_id": str(principal),
                    "instance_id": str(instance.config.instance_id),
                    "completed_response": value["response"],
                    "requests": requests,
                }
                manifest = seal(stage)
                fingerprints.append(manifest.fingerprint)
                result = run_host(
                    manifest, stdin=json.dumps(scenario).encode(), deadline=60
                )
                assert result.returncode == 0, "scripted SDK/bridge acceptance failed"
                assert result.stderr == b""
                summary = json.loads(result.stdout)
                assert summary["skill_sha256"] == digest
                assert len(summary["commands"]) == 1 + len(bridge.COMMANDS) + 6
                saved = [
                    item["receipt"] for item in summary["commands"] if "receipt" in item
                ]
                assert len(saved) == 2
                assert saved[0]["result"]["fact_ids"] == saved[1]["result"]["fact_ids"]
                receipts.extend(saved[0]["result"]["fact_ids"])
        # A real server crash after prepare loses its acknowledgement. A fresh
        # bridge and CLI subsequently recover the same prepared completed output.
        (stage / "host-config/bridge.json").write_text(
            json.dumps(
                {
                    "provider": "codex",
                    "skill_sha256": digest,
                    "allowed_commands": ["remember", "status", "resume"],
                }
            )
        )
        lost = checkpoint()
        lost["response"] = "Completed before the synthetic lost acknowledgement."
        scenario.update(
            provider="codex",
            completed_response=lost["response"],
            requests=[
                {
                    "command": "remember",
                    "argv": ["--profile", "/cli/profile.json", "remember"],
                    "stdin": json.dumps(lost),
                    "exit_code": 3,
                    "error_code": "transport_error",
                    "stage": "started",
                    "recovery": "resubmit_identical_checkpoint_same_identities",
                }
            ],
        )
        with server(instance, listener, "turn-prepare") as doomed:
            manifest = seal(stage)
            fingerprints.append(manifest.fingerprint)
            uncertain = run_host(
                manifest, stdin=json.dumps(scenario).encode(), deadline=60
            )
            assert uncertain.returncode == 0, "lost-acknowledgement SDK path failed"
            assert uncertain.stderr == b""
            assert doomed.wait(timeout=5) == 73
        scenario["requests"] = [
            {
                "command": command,
                "argv": ["--profile", "/cli/profile.json", command],
                "stdin": json.dumps({"turn_id": lost["turn_id"]}),
                "exit_code": 0,
                "state": "prepared",
            }
            for command in ("status", "resume")
        ]
        with server(instance, listener):
            manifest = seal(stage)
            fingerprints.append(manifest.fingerprint)
            recovered = run_host(
                manifest, stdin=json.dumps(scenario).encode(), deadline=60
            )
            assert recovered.returncode == 0, "fresh SDK/CLI recovery failed"
            assert recovered.stderr == b""
            summary = json.loads(recovered.stdout)
            receipts.extend(summary["commands"][1]["receipt"]["result"]["fact_ids"])
    with read_connection(instance.data_path) as connection:
        actual = [
            row[0]
            for row in connection.execute("SELECT fact_id FROM facts ORDER BY fact_id")
        ]
        assert actual == sorted(receipts)
        assert len(actual) == 3
    # Safe reproducibility evidence only: no request bodies, raw CLI output,
    # transcripts, profile data or synthetic bearer credentials are emitted.
    print(
        json.dumps(
            {
                "bridge_runtime_evidence": {
                    "wheel_sha256": runtime.wheel_sha256,
                    "requirements_sha256": runtime.requirements_sha256,
                    "runtime_file_count": runtime.file_count,
                    "manifests": fingerprints,
                    "outer_jails": 4,
                    "bridge_processes": 4,
                    "nested_cli_jails": 2 * (1 + len(bridge.COMMANDS) + 6) + 3,
                    "synthetic_server_processes": 3,
                    "provider_processes": 0,
                    "independently_matched_fact_count": len(actual),
                }
            },
            sort_keys=True,
        )
    )
