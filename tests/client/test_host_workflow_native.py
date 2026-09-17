"""Provider-free controller tests: fixed synthetic pipe peer, actual jailed entry."""

import errno
import hashlib
import importlib
import importlib.util
import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_host_workflow_sandbox import entry
from test_host_workflow_sandbox import private_staging_umask as private_staging_umask
from test_host_workflow_sandbox import sandbox as sandbox
from test_host_workflow_sandbox import stage as stage
from test_host_workflow_stdio import eof, receive, receive_message, send_message
from test_host_workflow_stdio import real_bridge_stage as real_bridge_stage
from test_memory_cli import checkpoint, profile
from test_memory_cli_process import server


def native() -> Any:
    assert importlib.util.find_spec("scripts.host_workflow_native"), (
        "controller missing"
    )
    return importlib.import_module("scripts.host_workflow_native")


@contextmanager
def running(invocation: Any) -> Iterator[Any]:
    incoming, send = os.pipe()
    receive_fd, outgoing = os.pipe()
    state = SimpleNamespace(send=send, receive=receive_fd, result=None, error=None)
    cancel = threading.Event()
    state.cancel = cancel

    def launch() -> None:
        try:
            state.result = invocation.launch(incoming, outgoing, cancel=cancel)
        except BaseException as error:
            state.error = error
        finally:
            os.close(incoming)
            os.close(outgoing)

    state.thread = threading.Thread(target=launch)
    state.thread.start()
    try:
        yield state
    finally:
        cancel.set()
        state.thread.join(4)
        for fd in (state.send, state.receive):
            if fd is not None:
                os.close(fd)
        assert not state.thread.is_alive()


def done(state: Any) -> Any:
    state.thread.join(4)
    assert not state.thread.is_alive()
    return state.error


def invocation(sandbox: Any, stage: Path, tmp_path: Path, seconds: float = 5) -> Any:
    return native().Invocation(
        sandbox.seal(stage), tmp_path / "admission", deadline=time.monotonic() + seconds
    )


def launch_in_spawned_process(controller: Any, incoming: Any, outgoing: Any) -> None:
    controller.launch(incoming.detach(), outgoing.detach())


@pytest.mark.host_isolation
def test_admission_is_single_use_even_after_bridge_crash(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(stage / "host-runtime/entry", "raise SystemExit(7)")
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "sandbox_child_failed"
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "native_launcher_already_used"


@pytest.mark.host_isolation
def test_concurrent_launcher_cannot_start_second_bridge(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as first:
        until = time.monotonic() + 2
        while not (tmp_path / "admission/used").exists() and time.monotonic() < until:
            time.sleep(0.005)
        with running(controller) as second:
            eof(second)
            assert receive(second) == b""
            assert str(done(second)) == "native_launcher_already_used"
        eof(first)
        assert receive(first) == b""
        assert done(first) is None
    with pytest.raises(native().ControllerFailure, match="native_terminal_missing"):
        controller.finish(None)


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"jsonrpc":', "native_partial_frame"),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', "native_pending_request"),
    ],
)
@pytest.mark.host_isolation
def test_eof_does_not_certify_incomplete_protocol(
    sandbox: Any, stage: Path, tmp_path: Path, raw: bytes, code: str
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        os.write(state.send, raw)
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == code


@pytest.mark.host_isolation
def test_complete_exchange_still_requires_terminal_and_receipts(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import sys, json
        for line in sys.stdin:
            value = json.loads(line)
            print(json.dumps({'jsonrpc':'2.0','id':value['id'],'result':{}}), flush=True)
    """,
    )
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        send_message(state, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert receive_message(state) == {"jsonrpc": "2.0", "id": 1, "result": {}}
        eof(state)
        assert receive(state) == b""
        assert done(state) is None
    with pytest.raises(native().ControllerFailure, match="native_receipts_missing"):
        controller.finish(native().Terminal(0, "complete", ()))


def test_original_absolute_deadline_expires_without_retry(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(stage / "host-runtime/entry", "import time\ntime.sleep(10)")
    controller = invocation(sandbox, stage, tmp_path, seconds=0.25)
    time.sleep(0.3)
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "native_deadline"
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "native_launcher_already_used"


def initialise(state: Any) -> None:
    send_message(
        state,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "fixed-synthetic-peer", "version": "1"},
            },
        },
    )
    assert "result" in receive_message(state)
    send_message(state, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def call(
    state: Any, identity: int, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    send_message(
        state,
        {
            "jsonrpc": "2.0",
            "id": identity,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    response = receive_message(state)
    assert response["id"] == identity
    return response


def daily(state: Any, identity: int, command: str, body: object = None) -> Any:
    response = call(
        state,
        identity,
        "run_daily_cli",
        {
            "argv": ["--profile", "/cli/profile.json", command],
            "stdin": "" if body is None else json.dumps(body),
        },
    )
    assert not response["result"]["isError"], response
    packet = json.loads(response["result"]["content"][0]["text"])
    assert packet["exit_code"] == 0
    return json.loads(packet["stdout"])["result"]


@pytest.mark.host_isolation
def test_real_scoped_checkpoint_receipt_and_recovery(
    sandbox: Any,
    real_bridge_stage: Path,
    tmp_path: Path,
    memory_support: ModuleType,
) -> None:
    root = real_bridge_stage
    instance = memory_support.Instance(tmp_path / "instance", attic=False)
    scope = [
        {"kind": "repository", "identifier": "cairn"},
        {"kind": "composite-run", "identifier": str(uuid4())},
        {"kind": "job", "identifier": "synthetic-pipe"},
    ]
    principal, token = instance.add_actor(
        operations=[],
        segments=scope,
        read_clearance="internal",
    )
    # The fixture actor starts with no operations. Add a separate immutable,
    # internal-only synthetic grant using the existing fixture catalogue.
    from cairn.catalogue.sqlite import _open_write_connection, read_connection

    with _open_write_connection(instance.data_path, create=False) as con:
        con.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
            "operations, read_clearance, write_classifications, delegable_operations, "
            "issued_by, expires_at, created_at) "
            "SELECT ?, principal_id, realm_id, scope_segments, ?, read_clearance, ?, "
            "delegable_operations, issued_by, expires_at, created_at FROM grants "
            "WHERE principal_id = ?",
            (str(uuid4()), '["ingest","retrieve"]', '["internal"]', str(principal)),
        )
        con.commit()
    skill = root / "work/.agents/skills/cairn-memory/SKILL.md"
    (root / "host-config/bridge.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "skill_sha256": hashlib.sha256(skill.read_bytes()).hexdigest(),
                "allowed_commands": [
                    "check",
                    "arrive",
                    "acknowledge-visit",
                    "recall",
                    "remember",
                    "status",
                    "resume",
                ],
            }
        )
    )
    value = checkpoint()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        supplied = profile(
            tmp_path, instance, token, f"http://127.0.0.1:{listener.getsockname()[1]}"
        )
        config = json.loads(supplied.read_bytes())
        config["scope"]["segments"] = scope
        config["credential_file"] = "credential"
        (root / "cli-config/profile.json").write_text(json.dumps(config))
        (root / "cli-config/profile.json").chmod(0o600)
        (root / "cli-config/credential").write_text(token)
        (root / "cli-config/credential").chmod(0o600)
        with server(instance, listener):
            controller = native().Invocation(
                sandbox.seal(root),
                tmp_path / "admission",
                deadline=time.monotonic() + 60,
            )
            with running(controller) as state:
                initialise(state)
                send_message(state, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                assert {
                    tool["name"] for tool in receive_message(state)["result"]["tools"]
                } == {
                    "read_installed_skill",
                    "run_daily_cli",
                }
                loaded = call(
                    state,
                    3,
                    "read_installed_skill",
                    {
                        "path": "/work/.agents/skills/cairn-memory/SKILL.md",
                    },
                )
                assert (
                    json.loads(loaded["result"]["content"][0]["text"])["text"].encode()
                    == skill.read_bytes()
                )
                help_response = call(
                    state, 4, "run_daily_cli", {"argv": ["--help"], "stdin": ""}
                )
                assert (
                    "usage:"
                    in json.loads(help_response["result"]["content"][0]["text"])[
                        "stdout"
                    ]
                )
                daily(state, 5, "check")
                saved = daily(state, 6, "remember", value)
                resumed = daily(state, 7, "resume", {"turn_id": value["turn_id"]})
                assert saved["state"] == resumed["state"] == "committed"
                assert saved["persistence"] == resumed["persistence"]
                receipt_ids = tuple(saved["persistence"]["result"]["fact_ids"])
                eof(state)
                assert receive(state) == b""
                assert done(state) is None
            result = controller.finish(
                native().Terminal(0, "synthetic checkpoint complete", receipt_ids)
            )
            assert result.receipt_ids == receipt_ids
            assert token not in repr(result)
            assert "The build uses" not in repr(result)
            assert result.input_bytes > 0 and result.output_bytes > len(
                skill.read_bytes()
            )
            with read_connection(instance.data_path) as con:
                assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
            # Same exact scope and profile/turn, explicitly new controller invocation.
            recovery = native().Invocation(
                sandbox.seal(root),
                tmp_path / "recovery",
                deadline=time.monotonic() + 60,
            )
            with running(recovery) as state:
                initialise(state)
                replay = daily(state, 2, "resume", {"turn_id": value["turn_id"]})
                assert replay["persistence"] == saved["persistence"]
                eof(state)
                assert receive(state) == b""
                assert done(state) is None
            assert (
                recovery.finish(
                    native().Terminal(0, "recovered", receipt_ids)
                ).receipt_ids
                == receipt_ids
            )
            # Altered requested scope does not alter the server-side grant.
            for segment in (1, 2):
                denied_config = json.loads(json.dumps(config))
                denied_config["scope"]["segments"][segment]["identifier"] = str(uuid4())
                with httpx.Client(trust_env=False, timeout=2) as http:
                    diagnosis = http.post(
                        config["endpoint"] + "/memory/v1/diagnose",
                        headers={"Authorization": "Bearer " + token},
                        json={
                            "scope": denied_config["scope"],
                            "classification": "internal",
                        },
                    )
                    assert diagnosis.status_code == 200
                    assert not any(diagnosis.json()["permissions"].values())
                (root / "cli-config/profile.json").write_text(json.dumps(denied_config))
                denied = native().Invocation(
                    sandbox.seal(root),
                    tmp_path / f"denied-{segment}",
                    deadline=time.monotonic() + 60,
                )
                with running(denied) as state:
                    initialise(state)
                    response = call(
                        state,
                        2,
                        "run_daily_cli",
                        {
                            "argv": ["--profile", "/cli/profile.json", "resume"],
                            "stdin": json.dumps({"turn_id": value["turn_id"]}),
                        },
                    )
                    assert response["result"]["isError"]
                    packet = json.loads(response["result"]["content"][0]["text"])
                    assert (
                        json.loads(packet["stderr"])["result"]["error"]["code"]
                        == "connection_refused"
                    )
                    eof(state)
                    assert receive(state) == b""
                    assert done(state) is None
                with pytest.raises(
                    native().ControllerFailure, match="native_receipts_missing"
                ):
                    denied.finish(native().Terminal(0, "not accepted", receipt_ids))


@pytest.mark.parametrize("invalid", [10**400, float("inf"), float("nan"), -1, True])
def test_invalid_deadlines_have_fixed_refusal(
    sandbox: Any, stage: Path, tmp_path: Path, invalid: object
) -> None:
    with pytest.raises(native().ControllerFailure, match="native_descriptor_invalid"):
        native().Invocation(
            sandbox.seal(stage), tmp_path / "admission", deadline=invalid
        )


@pytest.mark.parametrize("invalid", [-1, 10**400, True])
def test_invalid_pipe_is_consumed_and_fixed_refusal(
    sandbox: Any, stage: Path, tmp_path: Path, invalid: object
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(native().ControllerFailure, match="native_pipe_invalid"):
            controller.launch(invalid, write_fd)
        with pytest.raises(
            native().ControllerFailure, match="native_launcher_already_used"
        ):
            controller.launch(read_fd, write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_source_mutation_refuses_without_reset(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    (stage / "cli-config/profile.json").write_text("changed synthetic scope")
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "sandbox_source_changed"
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "native_launcher_already_used"


@pytest.mark.parametrize("stop", ["eof", "cancel", "deadline"])
@pytest.mark.host_isolation
def test_active_nested_cli_cleanup_leaves_independent_sibling(
    sandbox: Any, stage: Path, tmp_path: Path, stop: str
) -> None:
    shutil.copyfile(sandbox.__file__, stage / "host-runtime/sandbox.py")
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        sys.path.insert(0, '/runtime/host')
        from sandbox import run_cli
        sys.stdin.buffer.readline()
        run_cli(deadline=10)
    """,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        entry(
            stage / "cli-runtime/entry",
            f"""
            import os, socket, time
            if os.fork() == 0:
                os.setsid()
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                while True:
                    sender.sendto(b'alive', ('127.0.0.1', {receiver.getsockname()[1]}))
                    time.sleep(.01)
            time.sleep(20)
        """,
        )
        sibling = subprocess.Popen(
            ["/usr/bin/sleep", "20"], start_new_session=True, env={}
        )
        try:
            controller = invocation(sandbox, stage, tmp_path, seconds=1.5)
            with running(controller) as state:
                send_message(state, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
                receiver.settimeout(1)
                assert receiver.recv(16) == b"alive"
                if stop == "eof":
                    eof(state)
                elif stop == "cancel":
                    state.cancel.set()
                assert str(done(state)) in {
                    "native_pending_request",
                    "native_cancelled",
                    "native_deadline",
                    "sandbox_timeout",
                }
            receiver.settimeout(0.15)
            until = time.monotonic() + 2
            while True:
                try:
                    receiver.recv(16)
                except TimeoutError:
                    break
                assert time.monotonic() < until, "owned descendant survived"
            with pytest.raises(TimeoutError):
                receiver.recv(16)
            assert sibling.poll() is None
            with pytest.raises(
                native().ControllerFailure, match="native_session_incomplete"
            ):
                controller.finish(native().Terminal(0, "invalid success", ()))
        finally:
            sibling.kill()
            sibling.wait(timeout=2)


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"jsonrpc":', "native_partial_frame"),
        (b'{"jsonrpc":"2.0","id":9,"result":{}}\n', "native_unexpected_response"),
        (b'{"jsonrpc":"2.0","id":1,"result":{}}\n' * 2, "native_unexpected_response"),
    ],
)
@pytest.mark.host_isolation
def test_bad_output_cannot_become_transport_success(
    sandbox: Any, stage: Path, tmp_path: Path, raw: bytes, code: str
) -> None:
    entry(
        stage / "host-runtime/entry",
        f"""
        import sys, os
        sys.stdin.buffer.read() if {code == "native_partial_frame"} else sys.stdin.buffer.readline()
        os.write(1, {raw!r})
        sys.stdin.buffer.read()
    """,
    )
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        if code == "native_partial_frame":
            eof(state)
        else:
            send_message(state, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        receive(state)
        assert str(done(state)) == code


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_cumulative_input_limit_is_not_reset_by_frames(
    sandbox: Any, stage: Path, tmp_path: Path, extra: int
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    prefix = b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{"data":"'
    suffix = b'"}}\n'
    size = 4194304
    packet = prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix
    raw = packet * 2 + b" " * extra
    with running(controller) as state:

        def write() -> None:
            try:
                offset = 0
                while offset < len(raw):
                    offset += os.write(state.send, raw[offset : offset + 65536])
            except BrokenPipeError:
                pass
            finally:
                eof(state)

        writer = threading.Thread(target=write)
        writer.start()
        assert receive(state) == b""
        writer.join(3)
        assert not writer.is_alive()
        if extra:
            assert str(done(state)) == "native_input_limit"
        else:
            assert done(state) is None
            assert state.result.input_bytes == 8388608


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_output_cumulative_bound_preserves_server_notifications(
    sandbox: Any, stage: Path, tmp_path: Path, extra: int
) -> None:
    entry(
        stage / "host-runtime/entry",
        f"""
        import sys, os
        sys.stdin.buffer.read()
        prefix = b'{{"jsonrpc":"2.0","method":"notifications/message","params":{{"data":"'
        suffix = b'"}}}}\\n'
        packet = prefix + b'x' * (4194304 - len(prefix) - len(suffix)) + suffix
        raw = packet * 2 + b' ' * {extra}
        offset = 0
        while offset < len(raw):
            offset += os.write(1, raw[offset:offset+65536])
    """,
    )
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        eof(state)
        output = receive(state)
        if extra:
            assert str(done(state)) in {"native_output_limit", "sandbox_output_limit"}
        else:
            assert done(state) is None
            assert len(output) == state.result.output_bytes == 8388608


@pytest.mark.host_isolation
def test_process_crash_keeps_admission_consumed(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    """Process-local locking alone would reopen this invocation after a crash."""
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    incoming, send = os.pipe()
    receive_fd, outgoing = os.pipe()
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=launch_in_spawned_process,
        args=(
            controller,
            multiprocessing.reduction.DupFd(incoming),
            multiprocessing.reduction.DupFd(outgoing),
        ),
    )
    child.start()
    try:
        until = time.monotonic() + 2
        while not (tmp_path / "admission/used").exists() and time.monotonic() < until:
            time.sleep(0.005)
        assert (tmp_path / "admission/used").is_file()
        child.kill()
        child.join()
        with running(controller) as state:
            eof(state)
            assert receive(state) == b""
            assert str(done(state)) == "native_launcher_already_used"
    finally:
        if child.is_alive():
            child.kill()
            child.join()
        for fd in (incoming, send, receive_fd, outgoing):
            os.close(fd)


@pytest.mark.host_isolation
def test_stage_changed_during_session_fails_after_drain(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import sys, json
        for line in sys.stdin:
            value = json.loads(line)
            print(json.dumps({'jsonrpc':'2.0','id':value['id'],'result':{}}), flush=True)
    """,
    )
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        send_message(state, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert receive_message(state)["id"] == 1
        (stage / "work/.agents/skills/cairn-memory/SKILL.md").write_text("changed")
        eof(state)
        assert receive(state) == b""
        assert str(done(state)) == "native_source_changed"


@pytest.mark.parametrize(
    "result,code",
    [
        ("x" * 4096, "native_receipts_missing"),
        ("x" * 4097, "native_terminal_invalid"),
        ("é" * 2049, "native_terminal_invalid"),
        ("\ud800", "native_terminal_invalid"),
        ("", "native_terminal_invalid"),
    ],
)
@pytest.mark.host_isolation
def test_terminal_text_bound_and_no_failed_finish_retry(
    sandbox: Any, stage: Path, tmp_path: Path, result: str, code: str
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        assert done(state) is None
    with pytest.raises(native().ControllerFailure, match=code):
        controller.finish(native().Terminal(0, result, ()))
    with pytest.raises(native().ControllerFailure, match="native_session_incomplete"):
        controller.finish(native().Terminal(0, "changed terminal", ()))


def test_wrong_pipe_access_and_invalid_cancel_never_launch(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    incoming, send = os.pipe()
    receive_fd, outgoing = os.pipe()
    try:
        for number, (read_fd, write_fd, cancel, code) in enumerate(
            [
                (send, outgoing, None, "native_pipe_invalid"),
                (incoming, receive_fd, None, "native_pipe_invalid"),
                (incoming, outgoing, object(), "native_cancel_invalid"),
            ]
        ):
            controller = native().Invocation(
                sandbox.seal(stage),
                tmp_path / f"admit-{number}",
                deadline=time.monotonic() + 5,
            )
            with pytest.raises(native().ControllerFailure, match=code):
                controller.launch(read_fd, write_fd, cancel=cancel)
            assert os.get_blocking(read_fd) and os.get_blocking(write_fd)
    finally:
        for fd in (incoming, send, receive_fd, outgoing):
            os.close(fd)


@pytest.mark.parametrize("drain", [True, False])
@pytest.mark.host_isolation
def test_slow_pipe_peer_backpressure_has_no_loss_or_budget_restart(
    sandbox: Any, stage: Path, tmp_path: Path, drain: bool
) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import sys, json
        for line in sys.stdin:
            request = json.loads(line)
            print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'text':'x' * 1048576}}), flush=True)
    """,
    )
    controller = invocation(sandbox, stage, tmp_path, seconds=3 if drain else 0.4)
    expected = (
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"text": "x" * 1048576}}
        ).encode()
        + b"\n"
    )
    with running(controller) as state:
        send_message(state, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        time.sleep(0.1)
        if drain:
            assert state.thread.is_alive()
            assert receive(state, len(expected)) == expected
            eof(state)
            assert receive(state) == b""
            assert done(state) is None
            assert state.result.output_bytes == len(expected)
        else:
            assert str(done(state)) in {"native_deadline", "sandbox_timeout"}


@pytest.mark.host_isolation
def test_simultaneous_admission_has_one_winner(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    controller = invocation(sandbox, stage, tmp_path)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def contender() -> None:
        incoming, send = os.pipe()
        receive_fd, outgoing = os.pipe()
        os.close(send)
        try:
            barrier.wait(timeout=2)
            controller.launch(incoming, outgoing)
            outcomes.append("drained")
        except native().ControllerFailure as error:
            outcomes.append(str(error))
        finally:
            for fd in (incoming, receive_fd, outgoing):
                os.close(fd)

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(4)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["drained", "native_launcher_already_used"]


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_stderr_is_bounded_discarded_and_never_replayed(
    sandbox: Any, stage: Path, tmp_path: Path, extra: int
) -> None:
    entry(
        stage / "host-runtime/entry",
        f"""
        import sys, os
        sys.stdin.buffer.read()
        raw = b'x' * (262144 + {extra})
        while raw:
            count = os.write(2, raw)
            raw = raw[count:]
    """,
    )
    controller = invocation(sandbox, stage, tmp_path)
    with running(controller) as state:
        eof(state)
        assert receive(state) == b""
        if extra:
            assert str(done(state)) == "sandbox_output_limit"
        else:
            assert done(state) is None
            assert state.result.stderr_bytes == 262144
            assert "xxx" not in repr(state.result)


@pytest.mark.parametrize("kind", ["symlink", "traversal", "in-stage", "existing"])
def test_descriptor_cannot_retarget_or_reopen_admission(
    sandbox: Any, stage: Path, tmp_path: Path, kind: str
) -> None:
    if kind == "symlink":
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        path = tmp_path / "alias/admission"
    elif kind == "traversal":
        path = tmp_path / "stage/../admission"
    elif kind == "in-stage":
        path = stage / "admission"
    else:
        path = tmp_path / "admission"
        path.mkdir()
    with pytest.raises(native().ControllerFailure, match="native_descriptor_invalid"):
        native().Invocation(sandbox.seal(stage), path, deadline=time.monotonic() + 5)


@pytest.mark.parametrize("fault", ["pipe1", "pipe2", "flags1", "flags2", "start"])
def test_partial_relay_setup_closes_every_owned_fd_once(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    owned: list[int] = []
    closes: list[int] = []
    real_pipe, real_flags, real_close = os.pipe, os.get_blocking, os.close
    calls = {"pipe": 0, "flags": 0}

    def pipe() -> tuple[int, int]:
        calls["pipe"] += 1
        if fault == f"pipe{calls['pipe']}":
            raise OSError(errno.EMFILE, "injected allocation failure")
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def flags(fd: int) -> bool:
        calls["flags"] += 1
        if fault == f"flags{calls['flags']}":
            raise OSError(errno.EIO, "injected flags failure")
        return real_flags(fd)

    def start(thread: threading.Thread) -> None:
        raise RuntimeError("injected thread start failure")

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "get_blocking", flags)
            patch.setattr(native().os, "close", close)
            patch.setattr(native().Thread, "start", start)
            with pytest.raises(Exception) as failure:
                controller.launch(borrowed[0], borrowed[3])
        # Assert resource effects before the error spelling, so RED exposes leaks.
        assert sorted(closes) == sorted(owned)
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert isinstance(failure.value, native().ControllerFailure)
        assert str(failure.value) == "native_stdio_failed"
        assert all(real_flags(fd) for fd in borrowed)
        with pytest.raises(
            native().ControllerFailure, match="native_launcher_already_used"
        ):
            controller.launch(borrowed[0], borrowed[3])
        with pytest.raises(
            native().ControllerFailure, match="native_session_incomplete"
        ):
            controller.finish(None)
    finally:
        # Test hygiene for RED only: close actual still-open descriptors, not
        # descriptors already released by a close that subsequently reported EIO.
        for fd in (*owned, *borrowed):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)


@pytest.mark.parametrize(
    "fault",
    [
        "send-close",
        "receive-close",
        "bridge-input-close",
        "bridge-output-close",
        "input-restore",
        "output-restore",
        "multiple",
    ],
)
def test_cleanup_fault_attempts_all_remaining_obligations_once(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    owned: list[int] = []
    closes: list[int] = []
    events: list[str] = []
    real_pipe, real_close, real_set = os.pipe, os.close, os.set_blocking
    real_alive = threading.Thread.is_alive

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)
        if len(owned) == 4:
            failures = {
                "send-close": [owned[1]],
                "receive-close": [owned[2]],
                "bridge-input-close": [owned[0]],
                "bridge-output-close": [owned[3]],
                "multiple": [owned[0], owned[1]],
            }.get(fault, [])
            if fd in failures:
                raise OSError(errno.EIO, "injected close error after release")

    def flags(fd: int, blocking: bool) -> None:
        if blocking and fd in (borrowed[0], borrowed[3]):
            events.append("input-restore" if fd == borrowed[0] else "output-restore")
            if (fault in ("input-restore", "multiple") and fd == borrowed[0]) or (
                fault == "output-restore" and fd == borrowed[3]
            ):
                raise OSError(errno.EIO, "injected restoration failure")
        real_set(fd, blocking)

    def bridge(*args: Any, cancel: threading.Event) -> Any:
        assert cancel.wait(2)
        return native().StdioResult(0, 0, 0)

    def relay_fault(*args: Any) -> Any:
        raise OSError(errno.EIO, "injected relay failure")

    def alive(thread: threading.Thread) -> bool:
        if thread.name == "synthetic-cairn-bridge":
            events.append("liveness")
        return real_alive(thread)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "close", close)
            patch.setattr(native().os, "set_blocking", flags)
            patch.setattr(native(), "serve_host_stdio", bridge)
            patch.setattr(native().select, "select", relay_fault)
            patch.setattr(native().Thread, "is_alive", alive)
            with pytest.raises(Exception) as failure:
                controller.launch(borrowed[0], borrowed[3])
        assert sorted(closes) == sorted(owned)
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert events.count("input-restore") == events.count("output-restore") == 1
        assert events[-1] == "liveness"
        assert os.get_blocking(borrowed[0]) == (
            fault not in ("input-restore", "multiple")
        )
        assert os.get_blocking(borrowed[3]) == (fault != "output-restore")
        assert isinstance(failure.value, native().ControllerFailure)
        assert str(failure.value) == "native_cleanup_failed"
        assert all(os.fstat(fd) for fd in borrowed)
        with pytest.raises(
            native().ControllerFailure, match="native_launcher_already_used"
        ):
            controller.launch(borrowed[0], borrowed[3])
        with pytest.raises(
            native().ControllerFailure, match="native_session_incomplete"
        ):
            controller.finish(None)
    finally:
        for fd in (*owned, *borrowed):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)


def test_started_thread_keeps_ownership_when_start_reports_failure(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    owned: list[int] = []
    closes: list[int] = []
    threads: list[threading.Thread] = []
    real_pipe, real_close, real_start = os.pipe, os.close, threading.Thread.start

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)

    def start(thread: threading.Thread) -> None:
        threads.append(thread)
        real_start(thread)
        raise RuntimeError("injected failure after actual start")

    def bridge(*args: Any, cancel: threading.Event) -> Any:
        assert cancel.wait(2)
        return native().StdioResult(0, 0, 0)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "close", close)
            patch.setattr(native().Thread, "start", start)
            patch.setattr(native(), "serve_host_stdio", bridge)
            with pytest.raises(Exception) as failure:
                controller.launch(borrowed[0], borrowed[3])
        assert not any(thread.is_alive() for thread in threads)
        assert sorted(closes) == sorted(owned)
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert isinstance(failure.value, native().ControllerFailure)
        assert str(failure.value) == "native_stdio_failed"
        assert all(os.get_blocking(fd) for fd in borrowed)
    finally:
        for thread in threads:
            thread.join(3)
        for fd in (*owned, *borrowed):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)


def test_half_close_error_never_recloses_reused_descriptor(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    sentinel = os.open("/dev/null", os.O_RDONLY)
    owned: list[int] = []
    closes: list[int] = []
    real_pipe, real_close = os.pipe, os.close
    os.close(borrowed[1])

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)
        if len(owned) == 4 and fd == owned[1]:
            os.dup2(sentinel, fd)
            raise OSError(errno.EIO, "injected error after close and descriptor reuse")

    def bridge(*args: Any, cancel: threading.Event) -> Any:
        assert cancel.wait(2)
        return native().StdioResult(0, 0, 0)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "close", close)
            patch.setattr(native(), "serve_host_stdio", bridge)
            with pytest.raises(Exception) as failure:
                controller.launch(borrowed[0], borrowed[3])
        assert sorted(closes) == sorted(owned)
        assert os.fstat(owned[1]) == os.fstat(sentinel)
        for fd in owned[0:1] + owned[2:]:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert isinstance(failure.value, native().ControllerFailure)
        assert str(failure.value) == "native_cleanup_failed"
        assert os.get_blocking(borrowed[0]) and os.get_blocking(borrowed[3])
        with pytest.raises(
            native().ControllerFailure, match="native_launcher_already_used"
        ):
            controller.launch(borrowed[0], borrowed[3])
        with pytest.raises(
            native().ControllerFailure, match="native_session_incomplete"
        ):
            controller.finish(None)
    finally:
        for fd in set((*owned, borrowed[0], borrowed[2], borrowed[3], sentinel)):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)


def test_pre_ident_interruption_abandons_late_worker_without_touching_reused_fds(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widen the real Thread.start wait window before worker ident publication."""
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    sentinel = os.open("/dev/null", os.O_RDONLY)
    entered, release, completed = (threading.Event() for _ in range(3))
    owned: list[int] = []
    workers: list[threading.Thread] = []
    bridge_calls: list[tuple[int, int]] = []
    closes: list[int] = []
    real_pipe, real_close = os.pipe, os.close

    class PausedBootstrap(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            workers.append(self)

            def interrupted_wait(timeout: float | None = None) -> bool:
                assert entered.wait(2)
                raise KeyboardInterrupt("injected interruption in real start wait")

            cast(Any, self)._started.wait = interrupted_wait

        def _bootstrap_inner(self) -> None:
            entered.set()
            assert release.wait(2)
            try:
                cast(Any, super())._bootstrap_inner()
            finally:
                completed.set()

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)

    def bridge(
        manifest: Any,
        input_fd: int,
        output_fd: int,
        deadline: float,
        *,
        cancel: threading.Event,
    ) -> Any:
        bridge_calls.append((input_fd, output_fd))
        return native().StdioResult(0, 0, 0)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native(), "Thread", PausedBootstrap)
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "close", close)
            patch.setattr(native(), "serve_host_stdio", bridge)
            with pytest.raises(KeyboardInterrupt):
                controller.launch(borrowed[0], borrowed[3])
            assert workers[0].ident is None
            assert not completed.is_set()
            assert sorted(closes) == sorted(owned)
            for fd in owned:
                with pytest.raises(OSError):
                    os.fstat(fd)
            # Both former bridge endpoints now belong to unrelated sentinel use.
            os.dup2(sentinel, owned[0])
            os.dup2(sentinel, owned[3])
            release.set()
            assert completed.wait(2)
            workers[0].join(1)
            assert not workers[0].is_alive()
            assert bridge_calls == []
            assert sorted(closes) == sorted(owned), "late worker reclosed old endpoints"
            assert os.fstat(owned[0]) == os.fstat(sentinel)
            assert os.fstat(owned[3]) == os.fstat(sentinel)
            assert all(os.get_blocking(fd) for fd in borrowed)
            with pytest.raises(
                native().ControllerFailure, match="native_launcher_already_used"
            ):
                controller.launch(borrowed[0], borrowed[3])
            with pytest.raises(
                native().ControllerFailure, match="native_session_incomplete"
            ):
                controller.finish(None)
    finally:
        release.set()
        completed.wait(2)
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
        for fd in (*owned, *borrowed, sentinel):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)


def test_start_interruption_after_worker_claim_cancels_and_joins_owner(
    sandbox: Any,
    stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = invocation(sandbox, stage, tmp_path)
    borrowed = (*os.pipe(), *os.pipe())
    owned: list[int] = []
    closes: list[int] = []
    workers: list[threading.Thread] = []
    entered, cancelled = threading.Event(), threading.Event()
    real_pipe, real_close, real_start = os.pipe, os.close, threading.Thread.start

    def pipe() -> tuple[int, int]:
        pair = real_pipe()
        owned.extend(pair)
        return pair

    def close(fd: int) -> None:
        if fd in owned:
            closes.append(fd)
        real_close(fd)

    def start(thread: threading.Thread) -> None:
        workers.append(thread)
        real_start(thread)
        assert entered.wait(2)
        raise KeyboardInterrupt("injected interruption after worker took ownership")

    def bridge(*args: Any, cancel: threading.Event) -> Any:
        entered.set()
        assert cancel.wait(2)
        cancelled.set()
        return native().StdioResult(0, 0, 0)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(native().os, "pipe", pipe)
            patch.setattr(native().os, "close", close)
            patch.setattr(native().Thread, "start", start)
            patch.setattr(native(), "serve_host_stdio", bridge)
            with pytest.raises(KeyboardInterrupt):
                controller.launch(borrowed[0], borrowed[3])
        assert cancelled.is_set()
        assert not any(worker.is_alive() for worker in workers)
        assert sorted(closes) == sorted(owned)
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert all(os.get_blocking(fd) for fd in borrowed)
        with pytest.raises(
            native().ControllerFailure, match="native_launcher_already_used"
        ):
            controller.launch(borrowed[0], borrowed[3])
        with pytest.raises(
            native().ControllerFailure, match="native_session_incomplete"
        ):
            controller.finish(None)
    finally:
        for worker in workers:
            worker.join(3)
        for fd in (*owned, *borrowed):
            try:
                os.fstat(fd)
            except OSError:
                continue
            real_close(fd)
