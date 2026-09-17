"""Real bwrap canaries using fresh files and harmless Python stand-ins only."""

import importlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cairn.catalogue.audit import Classification, Scope
from cairn.client.cli_input import parse, preparation_size
from cairn.client.profiles import MemoryProfile
from cairn.client.rendering import render_result

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def private_staging_umask() -> Iterator[None]:
    # Catalogue/attic setup leaves 0007 in a reused pytest worker. Staged
    # fixtures and later test writes must not inherit group-write permission.
    # Scope this to our tests and restore the worker's previous policy.
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def sandbox() -> Any:
    name = "scripts.host_workflow_sandbox"
    assert importlib.util.find_spec(name) is not None, "sandbox is not implemented"
    return importlib.import_module(name)


def entry(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/python3 -I\n" + textwrap.dedent(body))
    path.chmod(0o700)


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    root = tmp_path / "stage"
    root.mkdir(mode=0o700)
    for name in ("host-runtime", "cli-runtime", "work", "host-config", "cli-config"):
        (root / name).mkdir(mode=0o700)
    entry(root / "host-runtime/entry", "print('host-help')")
    entry(root / "cli-runtime/entry", "print('cli-help')")
    (root / "work/.agents/skills/cairn-memory").mkdir(parents=True)
    (root / "work/.agents/skills/cairn-memory/SKILL.md").write_text("dummy-skill")
    (root / "host-config/config.json").write_text("{}")
    (root / "host-config/oauth.json").write_text("dummy-oauth")
    (root / "cli-config/profile.json").write_text("dummy-profile")
    (root / "cli-config/credential").write_text("dummy-scoped-credential")
    return root


@pytest.mark.host_isolation
def test_actual_allowed_read_immutable_paths_and_private_scratch(
    sandbox: Any, stage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("dummy-outside")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-do-not-inherit")
    entry(
        stage / "host-runtime/entry",
        f"""
        import os
        from pathlib import Path
        assert Path('/work/.agents/skills/cairn-memory/SKILL.md').read_text() == 'dummy-skill'
        assert Path('/host/config/oauth.json').read_text() == 'dummy-oauth'
        assert 'OPENAI_API_KEY' not in os.environ
        assert os.environ['HOME'] == '/scratch/home'
        for path in ['/runtime/host/entry', '/runtime/cli/entry',
                     '/work/.agents/skills/cairn-memory/SKILL.md',
                     '/host/config/config.json', '/cli/profile.json']:
            try:
                Path(path).write_text('replaced')
            except OSError:
                pass
            else:
                raise AssertionError('immutable_write')
        for path in [{str(outside)!r}, '/scratch/../../' + {str(outside)!r}.lstrip('/'),
                     '/proc/1/root' + {str(outside)!r}, '/mnt', '/sys', '/run', '/etc/passwd']:
            assert not Path(path).exists(), 'outside_visible'
        Path('/tmp/link').symlink_to({str(outside)!r})
        assert not Path('/tmp/link').exists()
        for fd in Path('/proc/self/fd').iterdir():
            try:
                assert os.readlink(fd) != {str(outside)!r}, 'descriptor_leaked'
            except FileNotFoundError:
                pass
        assert not Path('/proc/{os.getpid()}/root' + {str(outside)!r}).exists()
        assert os.stat('/proc/self/ns/net').st_ino == {os.stat("/proc/self/ns/net").st_ino}
        for path in [{str(outside)!r}, '/tmp/link']:
            try:
                Path(path).write_text('escaped')
            except OSError:
                pass
            else:
                raise AssertionError('outside_write')
        Path('/scratch/probe').write_text('private')
        Path('/tmp/probe').write_text('private')
        print('canaries-ok')
    """,
    )
    with outside.open() as descriptor:
        os.set_inheritable(descriptor.fileno(), True)
        result = sandbox.run_host(sandbox.seal(stage))
    assert result.stdout == b"canaries-ok\n"
    assert result.returncode == 0
    assert outside.read_text() == "dummy-outside"
    assert not (stage / "probe").exists()


@pytest.mark.host_isolation
def test_nested_cli_excludes_oauth_server_state_and_host_runtime(
    sandbox: Any, stage: Path, tmp_path: Path
) -> None:
    (tmp_path / "server-state").write_text("dummy-server")
    shutil.copyfile(sandbox.__file__, stage / "host-runtime/sandbox.py")
    entry(
        stage / "cli-runtime/entry",
        f"""
        import os
        from pathlib import Path
        assert Path('/cli/profile.json').read_text() == 'dummy-profile'
        assert Path('/cli/credential').read_text() == 'dummy-scoped-credential'
        for path in ['/host', '/runtime/host', '/work', {str(tmp_path / "server-state")!r},
                     '/proc/1/root/host/config/oauth.json']:
            assert not Path(path).exists(), 'outer_authority_visible'
        for path in ['/cli/profile.json', '/runtime/cli/entry']:
            try:
                Path(path).write_text('replace')
            except OSError:
                pass
            else:
                raise AssertionError('mutable_cli')
        assert 'CODEX_HOME' not in os.environ
        assert 'CLAUDE_CONFIG_DIR' not in os.environ
        print('cli-help')
    """,
    )
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        sys.path.insert(0, '/runtime/host')
        from sandbox import run_cli
        result = run_cli(('--help',))
        assert result.returncode == 0
        sys.stdout.buffer.write(result.stdout)
    """,
    )
    assert sandbox.run_host(sandbox.seal(stage)).stdout == b"cli-help\n"


@pytest.mark.parametrize(
    "kind",
    ["symlink", "parent-symlink", "hardlink", "fifo", "missing", "extra", "dotdot"],
)
def test_source_escape_refused(
    sandbox: Any, stage: Path, tmp_path: Path, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("dummy")
    if kind == "symlink":
        (stage / "work/escape").symlink_to(outside)
    elif kind == "parent-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(stage, target_is_directory=True)
        stage = alias
    elif kind == "hardlink":
        os.link(outside, stage / "work/escape")
    elif kind == "fifo":
        os.mkfifo(stage / "work/fifo")
    elif kind == "missing":
        (stage / "cli-runtime/entry").unlink()
    elif kind == "extra":
        (stage / "unexpected").mkdir()
    else:
        stage = stage / "work/.."
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_source_invalid$"):
        sandbox.seal(stage)


def test_sealed_source_substitution_refused(sandbox: Any, stage: Path) -> None:
    manifest = sandbox.seal(stage)
    (stage / "cli-config/profile.json").write_text("changed")
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_source_changed$"):
        sandbox.run_host(manifest)


def test_seal_rejects_file_record_folded_into_another_file(
    sandbox: Any, stage: Path
) -> None:
    first, second = stage / "work/a", stage / "work/b"
    first.write_bytes(b"alpha")
    second.write_bytes(b"beta")
    manifest = sandbox.seal(stage)
    first.write_bytes(
        b"alpha\0work/b\0" + str(second.stat().st_mode).encode() + b"\0beta"
    )
    second.unlink()
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_source_changed$"):
        sandbox.run_host(manifest)
    assert sandbox.seal(stage).fingerprint != manifest.fingerprint


def nested_capture(sandbox: Any, stage: Path, body: str, *, stdin: bytes = b"") -> Any:
    """Only synthetic data and controller-written Python cross this test bridge."""
    shutil.copyfile(sandbox.__file__, stage / "host-runtime/sandbox.py")
    (stage / "host-runtime/input").write_bytes(stdin)
    entry(stage / "cli-runtime/entry", body)
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, '/runtime/host')
        from sandbox import run_cli, SandboxFailure
        try:
            result = run_cli(stdin=Path('/runtime/host/input').read_bytes())
        except SandboxFailure as exc:
            print('error:' + str(exc))
        else:
            assert result.returncode == 0
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
        """,
    )
    result = sandbox.run_host(sandbox.seal(stage))
    assert result.returncode == 0
    return result


@pytest.mark.parametrize("character", ["x", "é"])
@pytest.mark.host_isolation
def test_maximum_checkpoint_content_and_json_escaping_reach_cli(
    sandbox: Any, stage: Path, character: str
) -> None:
    document = {
        "turn_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "response": character * (32768 // len(character.encode())),
        "observations": [
            {
                "body": str(i)
                + character * (4095 // len(character.encode()))
                + "x" * (4095 % len(character.encode()))
            }
            for i in range(8)
        ],
    }
    payload = json.dumps(document, separators=(",", ":")).encode()
    value = parse("remember", io.BytesIO(payload))
    identity = UUID("11111111-1111-4111-8111-111111111111")
    profile = MemoryProfile(
        "http://127.0.0.1:1",
        identity,
        Scope("synthetic", ()),
        Classification.INTERNAL,
        Path("/never-opened"),
        identity,
    )
    preparation_size(value, profile, identity)
    assert 65536 < len(payload) < 1048576
    result = nested_capture(
        sandbox,
        stage,
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())",
        stdin=payload,
    )
    assert result.stdout == payload


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_cli_stdin_exact_limit_and_one_byte_over(
    sandbox: Any, stage: Path, extra: int
) -> None:
    result = nested_capture(
        sandbox,
        stage,
        "import sys\nprint(len(sys.stdin.buffer.read()))",
        stdin=b"x" * (1048576 + extra),
    )
    assert result.stdout == (
        b"1048576\n" if not extra else b"error:sandbox_input_limit\n"
    )


@pytest.mark.parametrize(
    "body",
    ["x" * 300000, "\x01" * 50000, "x" * (1048576 - 77)],
    ids=["review-packet", "escaped-packet", "renderer-maximum"],
)
@pytest.mark.host_isolation
def test_renderer_accepted_packet_reaches_stdout_without_stderr_using_its_budget(
    sandbox: Any, stage: Path, body: str
) -> None:
    payload = render_result("recall", {"body": body})
    (stage / "cli-runtime/output").write_bytes(payload)
    result = nested_capture(
        sandbox,
        stage,
        """
        import sys
        from pathlib import Path
        sys.stdout.buffer.write(Path('/runtime/cli/output').read_bytes())
        sys.stderr.buffer.write(b'e' * 65536)
    """,
    )
    assert result.stdout == payload
    assert result.stderr == b"e" * 65536


@pytest.mark.parametrize("stream,limit", [(1, 1048576), (2, 65536)])
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_cli_stream_exact_limits_and_one_byte_over(
    sandbox: Any, stage: Path, stream: int, limit: int, extra: int
) -> None:
    result = nested_capture(
        sandbox,
        stage,
        f"""
        import sys
        stream = sys.stdout if {stream} == 1 else sys.stderr
        stream.buffer.write(b'x' * {limit + extra})
    """,
    )
    if extra:
        assert result.stdout == b"error:sandbox_output_limit\n"
        assert result.stderr == b""
    else:
        assert result.stdout == (b"x" * limit if stream == 1 else b"")
        assert result.stderr == (b"x" * limit if stream == 2 else b"")


@pytest.mark.parametrize("stream,limit", [(1, 8388608), (2, 262144)])
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.host_isolation
def test_whole_host_event_limits_are_separate_and_finite(
    sandbox: Any, stage: Path, stream: int, limit: int, extra: int
) -> None:
    entry(
        stage / "host-runtime/entry",
        f"""
        import sys
        stream = sys.stdout if {stream} == 1 else sys.stderr
        stream.buffer.write(b'x' * {limit + extra})
    """,
    )
    if extra:
        with pytest.raises(sandbox.SandboxFailure, match="^sandbox_output_limit$"):
            sandbox.run_host(sandbox.seal(stage))
    else:
        result = sandbox.run_host(sandbox.seal(stage))
        assert result.returncode == 0
        assert result.stdout == (b"x" * limit if stream == 1 else b"")
        assert result.stderr == (b"x" * limit if stream == 2 else b"")


def test_policy_refusal_precedes_source_access(
    sandbox: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "managed-policy"
    policy.symlink_to(tmp_path / "missing")
    monkeypatch.setattr(sandbox, "MANAGED_POLICY_PATHS", (policy,))
    with pytest.raises(
        sandbox.SandboxFailure, match="^managed_policy_requires_review$"
    ):
        sandbox.seal(tmp_path / "missing-stage")


@pytest.mark.parametrize("stream", [1, 2])
@pytest.mark.host_isolation
def test_output_capped_without_payload_in_error(
    sandbox: Any, stage: Path, stream: int
) -> None:
    entry(
        stage / "host-runtime/entry",
        f"""
        import os
        while True:
            os.write({stream}, b'dummy-payload' * 4096)
    """,
    )
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_output_limit$"):
        sandbox.run_host(sandbox.seal(stage))


@pytest.mark.host_isolation
def test_stdin_bounds_and_metacharacters_are_data(sandbox: Any, stage: Path) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        sys.stdout.buffer.write(sys.stdin.buffer.read())
    """,
    )
    manifest = sandbox.seal(stage)
    payload = b"$(touch /tmp/escape); `false`\n"
    assert sandbox.run_host(manifest, stdin=payload).stdout == payload
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_input_limit$"):
        sandbox.run_host(manifest, stdin=b"x" * (65536 + 1))


@pytest.mark.host_isolation
def test_closed_stdin_is_not_successful_delivery(sandbox: Any, stage: Path) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import os, time
        os.close(0)
        time.sleep(0.1)
    """,
    )
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_input_closed$"):
        sandbox.run_host(sandbox.seal(stage), stdin=b"x" * 65536)


@pytest.mark.parametrize("deadline", [0, -1, 61, float("nan"), float("inf")])
def test_invalid_deadline_refused(sandbox: Any, stage: Path, deadline: float) -> None:
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_deadline_invalid$"):
        sandbox.run_host(sandbox.seal(stage), deadline=deadline)


@pytest.mark.parametrize("argv", [("bad\0arg",), ("x" * 8193,), ("x",) * 65])
def test_invalid_argv_refused(sandbox: Any, stage: Path, argv: tuple[str, ...]) -> None:
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_argv_invalid$"):
        sandbox.run_host(sandbox.seal(stage), argv)


@pytest.mark.host_isolation
def test_nonzero_exit_and_inert_capture(sandbox: Any, stage: Path) -> None:
    entry(
        stage / "host-runtime/entry",
        """
        import sys
        print('dummy-output')
        print('dummy-error', file=sys.stderr)
        sys.exit(7)
    """,
    )
    result = sandbox.run_host(sandbox.seal(stage))
    assert result.returncode == 7
    assert result.stdout == b"dummy-output\n"
    assert result.stderr == b"dummy-error\n"
    assert "dummy" not in repr(result)


def test_parent_policy_rechecked_at_launch(
    sandbox: Any, stage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = sandbox.seal(stage)
    policy = tmp_path / "managed"
    policy.mkdir()
    monkeypatch.setattr(sandbox, "MANAGED_POLICY_PATHS", (policy,))
    with pytest.raises(
        sandbox.SandboxFailure, match="^managed_policy_requires_review$"
    ):
        sandbox.run_host(manifest)


@pytest.mark.host_isolation
def test_cleanup_failure_does_not_disclose_argv(
    sandbox: Any, stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = subprocess.Popen.wait

    def wait(process: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        result = original(process, timeout)
        if timeout == 2:
            raise subprocess.TimeoutExpired(["dummy-sensitive-argument"], timeout)
        return result

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(sandbox.SandboxFailure, match="^sandbox_cleanup_failed$"):
        sandbox.run_host(sandbox.seal(stage))


@pytest.mark.parametrize(
    "mode", ["timeout", "leader-exit", "descendant-pipes", "nested-timeout"]
)
@pytest.mark.host_isolation
def test_pid_namespace_cleanup_with_new_session(
    sandbox: Any, stage: Path, mode: str
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        port = receiver.getsockname()[1]
        body = f"""
            import os, socket, time
            child = os.fork()
            if child == 0:
                os.setsid()
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                while True:
                    s.sendto(b'alive', ('127.0.0.1', {port}))
                    time.sleep(0.01)
            time.sleep(0.1)
            if {mode!r} == 'leader-exit':
                os._exit(0)
            if {mode!r} == 'descendant-pipes':
                os.close(1)
                os.close(2)
            time.sleep(60)
        """
        target = (
            "cli-runtime/entry" if mode == "nested-timeout" else "host-runtime/entry"
        )
        entry(stage / target, body)
        if mode == "nested-timeout":
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
        started = time.monotonic()
        if mode == "leader-exit":
            assert sandbox.run_host(sandbox.seal(stage), deadline=1).returncode == 0
        else:
            with pytest.raises(sandbox.SandboxFailure, match="^sandbox_timeout$"):
                sandbox.run_host(sandbox.seal(stage), deadline=0.4)
        assert time.monotonic() - started < 3
        receiver.settimeout(0.2)
        received = 0
        while True:
            try:
                receiver.recv(128)
                received += 1
            except TimeoutError:
                break
            assert time.monotonic() - started < 3, "descendant survived"
        assert received > 0, "canary never ran"
        with pytest.raises(TimeoutError):
            receiver.recv(128)
