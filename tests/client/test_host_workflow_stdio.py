"""Actual jailed byte transport with synthetic pipes; no native agent or provider."""

import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_host_workflow_sandbox import entry
from test_host_workflow_sandbox import private_staging_umask as private_staging_umask
from test_host_workflow_sandbox import sandbox as sandbox
from test_host_workflow_sandbox import stage as stage


@contextmanager
def relay(
    sandbox: Any, manifest: Any, *, seconds: float = 5, blocking: bool = True
) -> Iterator[Any]:
    serve = sandbox.serve_host_stdio
    incoming, send = os.pipe()
    receive, outgoing = os.pipe()
    os.set_blocking(incoming, blocking)
    os.set_blocking(outgoing, blocking)
    cancel = threading.Event()
    state = SimpleNamespace(send=send, receive=receive, result=None, error=None)

    def serve_thread() -> None:
        try:
            state.result = serve(
                manifest, incoming, outgoing, time.monotonic() + seconds, cancel=cancel
            )
        except BaseException as error:
            state.error = error
        finally:
            state.flags_restored = (
                os.get_blocking(incoming) == blocking
                and os.get_blocking(outgoing) == blocking
            )
            os.close(incoming)
            os.close(outgoing)

    thread = threading.Thread(target=serve_thread)
    state.cancel = cancel
    state.thread = thread
    thread.start()
    try:
        yield state
    finally:
        cancel.set()
        thread.join(3)
        for fd in (state.send, state.receive):
            if fd is not None:
                os.close(fd)
        assert not thread.is_alive(), "owned relay did not join"


def eof(state: Any) -> None:
    os.close(state.send)
    state.send = None


def receive(state: Any, size: int | None = None) -> bytes:
    output = bytearray()
    until = time.monotonic() + 6
    while size is None or len(output) < size:
        assert select.select([state.receive], [], [], max(0, until - time.monotonic()))[
            0
        ], ("relay read timed out", state.error)
        chunk = os.read(state.receive, 65536 if size is None else size - len(output))
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


def finished(state: Any, code: str | None = None) -> Any:
    state.thread.join(3)
    assert not state.thread.is_alive()
    assert state.flags_restored
    if code is None:
        assert state.error is None, state.error
        return state.result
    assert type(state.error).__name__ == "SandboxFailure"
    assert str(state.error) == code
    return None


@pytest.mark.host_isolation
def test_duplex_before_eof_then_complete_drain(sandbox: Any, stage: Path) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import os
        os.write(1, b'ready')
        while chunk := os.read(0, 4096):
            os.write(1, chunk)
        os.write(1, b'drained')
        os.write(2, b'private diagnostic')
    """,
    )
    with relay(sandbox, sandbox.seal(stage)) as state:
        assert receive(state, 5) == b"ready"
        # The transport must not interpret cancellation/framing or shell bytes.
        chunks = [b"\x00\xff", b'{"method":"notifications/cancelled"}\n', b"$(false)"]
        for chunk in chunks:
            os.write(state.send, chunk)
            assert receive(state, len(chunk)) == chunk
        eof(state)
        assert receive(state) == b"drained"
        result = finished(state)
    assert result.input_bytes == sum(map(len, chunks))
    assert result.output_bytes == result.input_bytes + 12
    assert result.stderr_bytes == 18
    assert "private" not in repr(result)


@pytest.mark.host_isolation
def test_stream_uses_only_fixed_mounts_environment_and_entry(
    sandbox: Any, stage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside-synthetic"
    outside.write_text("not mounted")
    monkeypatch.setenv("UNTRUSTED_AMBIENT", "not inherited")
    entry(
        stage / "host-runtime/entry",
        f"""
        import os,sys
        from pathlib import Path
        assert sys.argv == ['/runtime/host/entry']
        assert dict(os.environ) == {{
            'HOME':'/scratch/home','PATH':'/usr/bin','LANG':'C.UTF-8',
            'LC_ALL':'C.UTF-8','TERM':'dumb','TMPDIR':'/tmp',
            'XDG_CONFIG_HOME':'/scratch/config','XDG_CACHE_HOME':'/scratch/cache',
            'XDG_DATA_HOME':'/scratch/data','XDG_STATE_HOME':'/scratch/state',
            'CODEX_HOME':'/host/config','CLAUDE_CONFIG_DIR':'/host/config',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
            'PWD':'/work',
        }}
        assert os.getcwd() == '/work'
        assert not Path({str(outside)!r}).exists()
        for path in ['/runtime/host/entry','/runtime/cli/entry',
                     '/work/.agents/skills/cairn-memory/SKILL.md',
                     '/host/config/config.json','/cli/profile.json']:
            assert Path(path).is_file()
            try: Path(path).write_text('replace')
            except OSError: pass
            else: raise AssertionError('writable mount')
        for fd in Path('/proc/self/fd').iterdir():
            try: assert os.readlink(fd) != {str(outside)!r}
            except FileNotFoundError: pass
        sys.stdin.buffer.read()
        os.write(1,b'boundaries-ok')
    """,
    )
    with outside.open() as extra:
        os.set_inheritable(extra.fileno(), True)
        with relay(sandbox, sandbox.seal(stage)) as state:
            eof(state)
            assert receive(state) == b"boundaries-ok"
            finished(state)
    assert outside.read_text() == "not mounted"


@pytest.mark.parametrize(
    "direction,limit", [("input", 8388608), ("output", 8388608), ("stderr", 262144)]
)
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_cumulative_exact_limits_and_one_byte_over(
    sandbox: Any, stage: Path, direction: str, limit: int, extra: int
) -> None:
    body = "import os\n"
    if direction == "input":
        body += "while os.read(0,4096): pass\n"
    else:
        body += f"while os.read(0,4096): pass\nfd={1 if direction == 'output' else 2}\n"
        body += f"left={limit + extra}\nwhile left:\n n=os.write(fd,b'x'*min(4096,left)); left-=n\n"
    entry(stage / "host-runtime/entry", body)
    with relay(sandbox, sandbox.seal(stage)) as state:
        if direction == "input":
            remaining = limit + extra
            while remaining:
                remaining -= os.write(state.send, b"x" * min(4096, remaining))
        eof(state)
        output = receive(state)
        result = finished(
            state,
            ("sandbox_input_limit" if direction == "input" else "sandbox_output_limit")
            if extra
            else None,
        )
        assert len(output) <= (limit if direction == "output" else 0)
        if not extra:
            assert (
                getattr(
                    result,
                    {
                        "input": "input_bytes",
                        "output": "output_bytes",
                        "stderr": "stderr_bytes",
                    }[direction],
                )
                == limit
            )


@pytest.mark.parametrize(
    "kind", ["nonzero", "early-exit", "closed-input", "blocked-output", "broken-output"]
)
@pytest.mark.host_isolation
def test_failures_never_become_orderly_eof(
    sandbox: Any, stage: Path, kind: str
) -> None:
    programs = {
        "nonzero": "import sys\nsys.stdin.buffer.read(); sys.exit(7)",
        "early-exit": "pass",
        "closed-input": "import os,time\nos.close(0); os.write(1,b'ready'); time.sleep(.1)",
        "blocked-output": "import os\nwhile True: os.write(1,b'x'*4096)",
        "broken-output": "import os\nos.read(0,1); os.write(1,b'private')",
    }
    entry(stage / "host-runtime/entry", programs[kind])
    with relay(sandbox, sandbox.seal(stage), seconds=0.5) as state:
        if kind == "nonzero":
            eof(state)
        elif kind == "closed-input":
            assert receive(state, 5) == b"ready"
            # bwrap's monitor also holds the pipe: force queued delivery past
            # that pipe's capacity, rather than claiming five accepted bytes
            # prove payload consumption.
            os.write(state.send, b"x" * 65536)
        elif kind == "broken-output":
            os.close(state.receive)
            state.receive = None
            os.write(state.send, b"x")
            eof(state)
        finished(
            state,
            {
                "nonzero": "sandbox_child_failed",
                "early-exit": "sandbox_premature_eof",
                "closed-input": "sandbox_input_closed",
                "blocked-output": "sandbox_timeout",
                "broken-output": "sandbox_output_closed",
            }[kind],
        )


@pytest.mark.host_isolation
def test_slow_consumer_backpressures_input_and_recovers_without_loss(
    sandbox: Any, stage: Path
) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import os
        os.write(1,b'ready')
        while chunk := os.read(0,4096):
            os.write(1,chunk)
    """,
    )
    payload = bytes(range(256)) * 8192
    sent = threading.Event()
    failures: list[BaseException] = []
    with relay(sandbox, sandbox.seal(stage)) as state:
        assert receive(state, 5) == b"ready"

        def producer() -> None:
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(state.send, payload[offset : offset + 65536])
                eof(state)
                sent.set()
            except BaseException as error:
                failures.append(error)

        writer = threading.Thread(target=producer)
        writer.start()
        try:
            assert not sent.wait(0.1), "relay swallowed input behind a blocked consumer"
            assert receive(state) == payload
            result = finished(state)
        finally:
            state.cancel.set()
            writer.join(3)
            assert not writer.is_alive()
        assert not failures
        assert sent.is_set()
    assert result.input_bytes == len(payload)
    assert result.output_bytes == len(payload) + 5


def test_absolute_deadline_includes_fingerprint_before_any_launch(
    sandbox: Any, stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = sandbox.seal(stage)
    fingerprint = sandbox._fingerprint

    def slow(root: Path) -> str:
        value: str = fingerprint(root)
        time.sleep(0.1)
        return value

    monkeypatch.setattr(sandbox, "_fingerprint", slow)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("expired launch")
    )
    with relay(sandbox, manifest, seconds=0.05) as state:
        finished(state, "sandbox_timeout")


def test_refusals_precede_launch_and_preserve_borrowed_descriptors(
    sandbox: Any, stage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = sandbox.seal(stage)
    incoming, send = os.pipe()
    receive_fd, outgoing = os.pipe()

    def never_launch(*args: Any, **kwargs: Any) -> None:
        pytest.fail("refusal reached process launch")

    monkeypatch.setattr(subprocess, "Popen", never_launch)
    try:
        for deadline in (
            10**400,
            float("nan"),
            float("inf"),
            time.monotonic() - 1,
            time.monotonic() + 61,
        ):
            with pytest.raises(
                sandbox.SandboxFailure, match="^sandbox_deadline_invalid$"
            ):
                sandbox.serve_host_stdio(manifest, incoming, outgoing, deadline)
        with pytest.raises(sandbox.SandboxFailure, match="^sandbox_stdio_invalid$"):
            sandbox.serve_host_stdio(manifest, outgoing, incoming, time.monotonic() + 1)
        for read_fd, write_fd in ((10**400, outgoing), (incoming, 10**400)):
            with pytest.raises(sandbox.SandboxFailure, match="^sandbox_stdio_invalid$"):
                sandbox.serve_host_stdio(
                    manifest, read_fd, write_fd, time.monotonic() + 1
                )
            assert os.get_blocking(incoming) and os.get_blocking(outgoing)
        (stage / "cli-config/profile.json").write_text("changed")
        with pytest.raises(sandbox.SandboxFailure, match="^sandbox_source_changed$"):
            sandbox.serve_host_stdio(manifest, incoming, outgoing, time.monotonic() + 1)
        policy = tmp_path / "managed"
        policy.mkdir()
        monkeypatch.setattr(sandbox, "MANAGED_POLICY_PATHS", (policy,))
        with pytest.raises(
            sandbox.SandboxFailure, match="^managed_policy_requires_review$"
        ):
            sandbox.serve_host_stdio(manifest, incoming, outgoing, time.monotonic() + 1)
        assert os.get_blocking(incoming) and os.get_blocking(outgoing)
    finally:
        for fd in (incoming, outgoing, send, receive_fd):
            os.close(fd)


@pytest.mark.host_isolation
def test_initially_nonblocking_pipes_remain_nonblocking(
    sandbox: Any, stage: Path
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    with relay(sandbox, sandbox.seal(stage), blocking=False) as state:
        eof(state)
        assert receive(state) == b""
        finished(state)


@pytest.mark.parametrize("stop", ["cancel", "eof-timeout"])
@pytest.mark.host_isolation
def test_cancel_reaps_nested_namespace_but_not_independent_sibling(
    sandbox: Any, stage: Path, stop: str
) -> None:
    shutil.copyfile(sandbox.__file__, stage / "host-runtime/sandbox.py")
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        sys.path.insert(0, '/runtime/host')
        from sandbox import run_cli
        run_cli(deadline=60)
    """,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        entry(
            stage / "cli-runtime/entry",
            f"""
            import os,socket,time
            if os.fork() == 0:
                os.setsid()
                s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                while True:
                    s.sendto(b'alive',('127.0.0.1',{receiver.getsockname()[1]}))
                    time.sleep(0.01)
            time.sleep(60)
        """,
        )
        sibling = subprocess.Popen(["/usr/bin/sleep", "10"], start_new_session=True)
        try:
            with relay(sandbox, sandbox.seal(stage), seconds=1) as state:
                receiver.settimeout(2)
                assert receiver.recv(128) == b"alive"
                if stop == "cancel":
                    state.cancel.set()
                    finished(state, "sandbox_cancelled")
                else:
                    eof(state)
                    finished(state, "sandbox_timeout")
            receiver.settimeout(0.15)
            until = time.monotonic() + 2
            while True:
                try:
                    receiver.recv(128)
                except TimeoutError:
                    break
                assert time.monotonic() < until, "nested descendant survived"
            with pytest.raises(TimeoutError):
                receiver.recv(128)
            assert sibling.poll() is None
        finally:
            sibling.kill()
            sibling.wait(timeout=2)


@pytest.mark.host_isolation
def test_cleanup_failure_is_fixed_and_restores_borrowed_flags(
    sandbox: Any, stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry(stage / "host-runtime/entry", "import sys\nsys.stdin.buffer.read()")
    original = subprocess.Popen.wait

    def failed_wait(
        process: subprocess.Popen[bytes], timeout: float | None = None
    ) -> int:
        value = original(process, timeout)
        if timeout == 2:
            raise subprocess.TimeoutExpired(["private-command"], timeout)
        return value

    monkeypatch.setattr(subprocess.Popen, "wait", failed_wait)
    with relay(sandbox, sandbox.seal(stage)) as state:
        eof(state)
        assert receive(state) == b""
        finished(state, "sandbox_cleanup_failed")


@pytest.fixture(scope="module")
def real_bridge_stage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from host_workflow_fixture import build_cli_runtime
    from test_host_workflow_bridge import stage_bridge

    from cairn.client.host_installation import install_host_workflow

    root = tmp_path_factory.mktemp("streaming-bridge")
    stage_bridge(root)
    build_cli_runtime(root / "cli-runtime")
    (root / "host-runtime/entry").write_bytes(
        (root / "host-runtime/bridge-entry").read_bytes()
    )
    assets = root / "cli-runtime/site-packages/cairn/host_workflows"
    install_host_workflow("codex", root / "work", assets_root=assets, apply=True)
    skill = root / "work/.agents/skills/cairn-memory/SKILL.md"
    (root / "host-config/bridge.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "skill_sha256": hashlib.sha256(skill.read_bytes()).hexdigest(),
                "allowed_commands": ["check"],
            }
        )
    )
    # Module fixtures precede the per-test private umask fixture.
    (root / "host-config/bridge.json").chmod(0o600)
    return root


def send_message(state: Any, value: dict[str, Any]) -> None:
    raw = json.dumps(value).encode() + b"\n"
    offset = 0
    while offset < len(raw):
        offset += os.write(state.send, raw[offset:])


def receive_message(state: Any) -> dict[str, Any]:
    raw = bytearray()
    while len(raw) <= 1048576:
        chunk = receive(state, 1)
        assert chunk, ("bridge closed before frame", state.error)
        raw.extend(chunk)
        if chunk == b"\n":
            result: dict[str, Any] = json.loads(raw)
            return result
    pytest.fail("unexpected unbounded test response")


@pytest.mark.host_isolation
def test_actual_sdk_skill_and_nested_wheel_help_over_streaming_pipes(
    sandbox: Any, real_bridge_stage: Path
) -> None:
    skill = (
        real_bridge_stage / "work/.agents/skills/cairn-memory/SKILL.md"
    ).read_bytes()
    with relay(sandbox, sandbox.seal(real_bridge_stage), seconds=20) as state:
        send_message(
            state,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "synthetic-streaming-client",
                        "version": "1",
                    },
                },
            },
        )
        assert receive_message(state)["id"] == 1
        send_message(state, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send_message(state, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = receive_message(state)
        assert tools["id"] == 2
        assert {tool["name"] for tool in tools["result"]["tools"]} == {
            "read_installed_skill",
            "run_daily_cli",
        }
        send_message(
            state,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "read_installed_skill",
                    "arguments": {"path": "/work/.agents/skills/cairn-memory/SKILL.md"},
                },
            },
        )
        answer = receive_message(state)
        assert answer["id"] == 3 and not answer["result"]["isError"]
        loaded = json.loads(answer["result"]["content"][0]["text"])
        assert loaded["sha256"] == hashlib.sha256(skill).hexdigest()
        assert loaded["text"].encode() == skill
        send_message(
            state,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "run_daily_cli",
                    "arguments": {"argv": ["--help"], "stdin": ""},
                },
            },
        )
        answer = receive_message(state)
        assert answer["id"] == 4 and not answer["result"]["isError"]
        help_result = json.loads(answer["result"]["content"][0]["text"])
        assert help_result["exit_code"] == 0 and "usage:" in help_result["stdout"]
        eof(state)
        assert receive(state) == b"", "unsolicited or duplicate response"
        result = finished(state)
    assert result.input_bytes > 0 and result.output_bytes > len(skill)
    assert result.stderr_bytes == 0


@pytest.mark.host_isolation
def test_partial_frame_eof_preserves_bridge_protocol_refusal(
    sandbox: Any, real_bridge_stage: Path
) -> None:
    with relay(sandbox, sandbox.seal(real_bridge_stage), seconds=15) as state:
        os.write(state.send, b'{"jsonrpc":')
        eof(state)
        answer = receive_message(state)
        assert answer["error"]["code"] == -32600
        assert receive(state) == b""
        finished(state)
    # Transport EOF is not protocol acceptance: the controller must reject the
    # actual error frame above, even when the bridge closes with exit zero.
