"""Foreground ownership is the live parent handle, never a saved PID."""

import fcntl
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from cairn_install import core
from cairn_install.core import Context, InstallError, open_context
from cairn_install.foreground import ForegroundProcess


def create(tmp_path: Path) -> Context:
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "mode": "disposable",
            "port": 18000,
            "semantic": False,
            "source": str(tmp_path),
            "source_fingerprint": "test",
        },
    )


def test_foreground_restart_reaps_child_and_inherits_instance_lock(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        argv = [sys.executable, "-c", "import time;time.sleep(60)"]
        owner.start(argv, ctx.root / "service.log")
        first = owner.process
        assert first is not None and owner.running()
        # The child keeps the same flock alive even after the parent's copy closes.
        os.close(ctx._lock)
        ctx._lock = -1
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(ctx.directory / "installer.lock"),
            ],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0
        owner.stop()
        assert first.returncode is not None
        assert ctx.state["resources"]["native_foreground"]["status"] == "stopped"


def test_stale_foreground_state_never_supplies_signal_authority(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        ctx.state["resources"]["native_foreground"] = {
            "generation": 1,
            "status": "running",
        }
        owner = ForegroundProcess(ctx)
        with pytest.raises(InstallError, match="without this parent"):
            owner.stop()
        assert ctx.state["resources"]["native_foreground"]["status"] == "running"


def test_interruption_during_spawn_still_registers_and_cleans_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = real(*args, **kwargs)
        children.append(child)
        os.kill(os.getpid(), signal.SIGTERM)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        with pytest.raises(KeyboardInterrupt):
            owner.start(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                ctx.root / "service.log",
            )
        assert children[0].returncode is not None
        assert ctx.state["resources"]["native_foreground"]["status"] == "stopped"


def test_exited_leader_remains_unreaped_until_group_cleanup(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        owner.start([sys.executable, "-c", "pass"], ctx.root / "service.log")
        child = owner.process
        assert child is not None
        deadline = time.monotonic() + 3
        while owner.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child.returncode is None
        with pytest.raises(InstallError, match="exited unexpectedly"):
            owner.wait()
        owner.stop()
        assert child.returncode == 0


@pytest.mark.parametrize(
    "text", ["", "1 2 UNKNOWN\n", "1 -2 S\n", "1 2 S\n1 2 S\n", "1 2 S extra\n"]
)
def test_darwin_inventory_refuses_ambiguous_or_malformed_output(text: str) -> None:
    with pytest.raises(InstallError, match="inventory"):
        core._parse_darwin_processes(text.encode())


def test_darwin_inventory_distinguishes_live_zombie_and_group_members() -> None:
    assert core._parse_darwin_processes(b"  1 1 Ss\n 12 10 Z+\n 13 10 R<\n") == {
        1: (1, "S"),
        12: (10, "Z"),
        13: (10, "R"),
    }


def test_restart_reaps_before_new_generation(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        argv = [sys.executable, "-c", "import time;time.sleep(60)"]
        try:
            owner.start(argv, ctx.root / "service.log")
            first = owner.process
            owner.stop()
            assert first is not None and first.returncode is not None
            owner.start(argv, ctx.root / "service.log")
            assert owner.process is not first and owner.running()
            assert ctx.state["resources"]["native_foreground"]["generation"] == 2
        finally:
            owner.close()


def test_repeated_interruptions_cannot_break_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        owner.start(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            ctx.root / "service.log",
        )
        child = owner.process
        real = core._group_has_live_members
        calls = []

        def interrupted_inventory(group: int) -> bool:
            calls.append(group)
            os.kill(os.getpid(), signal.SIGINT)
            return real(group)

        monkeypatch.setattr(core, "_group_has_live_members", interrupted_inventory)
        owner.close()
        assert len(calls) >= 2
        assert child is not None and child.returncode is not None


def test_foreground_cleanup_kills_term_resistant_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    child_code = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    )
    parent_code = "import subprocess,sys,time,signal,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
    with create(tmp_path) as ctx:
        owner = ForegroundProcess(ctx)
        owner.start(
            [sys.executable, "-c", parent_code, str(marker), child_code],
            ctx.root / "service.log",
        )
        leader = owner.process
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
        finally:
            owner.close()
        assert leader is not None and leader.returncode == -signal.SIGKILL
        assert not core._group_has_live_members(leader.pid)


def test_darwin_inventory_uses_fixed_tool_and_rejects_excess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = subprocess.Popen

    def fake_ps(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert argv == ["/bin/ps", "-axo", "pid=,pgid=,stat="]
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return real(
            [sys.executable, "-c", "import sys;sys.stdout.write('1 1 S\\n'*200000)"],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_ps)
    with pytest.raises(InstallError, match="exceeds limit"):
        core._darwin_processes()


def test_darwin_failed_fullsync_preserves_previous_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")
    calls: list[int] = []

    def failed_barrier(fd: int, command: int) -> int:
        calls.append(command)
        raise OSError("unsupported")

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(fcntl, "fcntl", failed_barrier)
    with pytest.raises(InstallError, match="full file sync failed"):
        core.atomic_write(target, b"new")
    assert target.read_bytes() == b"previous"
    assert calls == [51]


@pytest.mark.parametrize("status", ["H", "?s"])
def test_darwin_mach_states_remain_live(status: str) -> None:
    assert core._parse_darwin_processes(f"1 1 {status}\n".encode()) == {
        1: (1, status[0])
    }


def test_foreground_fifo_log_refused_without_blocking(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        log = ctx.root / "service.log"
        os.mkfifo(log, 0o600)
        owner = ForegroundProcess(ctx)
        previous = signal.signal(
            signal.SIGALRM,
            lambda *_: (_ for _ in ()).throw(AssertionError("FIFO open blocked")),
        )
        signal.alarm(3)
        try:
            with pytest.raises(InstallError, match="safely open"):
                owner.start([sys.executable, "-c", "pass"], log)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        assert owner.process is None
        assert ctx.state["resources"]["native_foreground"]["status"] == "stopped"


def test_command_signal_after_reap_never_signals_reused_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_wait = subprocess.Popen.wait

    def wait(child: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> int:
        result = real_wait(child, *args, **kwargs)
        if child.args == [sys.executable, "-c", "pass"]:
            os.kill(os.getpid(), signal.SIGINT)
        return result

    def unsafe_stop(child: subprocess.Popen[bytes]) -> None:
        pytest.fail("A reaped child's process group cannot be signalled")

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(core, "stop_command_group", unsafe_stop)
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="interrupted at completion") as caught:
            ctx.command([sys.executable, "-c", "pass"])
        assert caught.value.code == "interrupted"


def test_command_signal_during_observation_cleans_unreaped_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_stop = core.stop_command_group

    def observe(child: subprocess.Popen[bytes]) -> bool:
        children.append(child)
        assert child.returncode is None
        raise KeyboardInterrupt

    def stop(child: subprocess.Popen[bytes]) -> None:
        assert child.returncode is None
        real_stop(child)

    monkeypatch.setattr(core, "owned_child_running", observe)
    monkeypatch.setattr(core, "stop_command_group", stop)
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="interrupted or timed out"):
            ctx.command([sys.executable, "-c", "import time;time.sleep(60)"])
    assert children[0].returncode is not None


@pytest.mark.parametrize("denied_signal", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("inventory", ["zombie", "live", "unavailable"])
def test_darwin_group_permission_requires_proven_quiescence(
    denied_signal: int, inventory: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True
    )
    real_killpg = os.killpg
    calls: list[int] = []

    def deny(group: int, number: int) -> None:
        assert group == child.pid
        calls.append(number)
        if number == denied_signal:
            raise PermissionError(1, "Operation not permitted")

    def processes() -> dict[int, tuple[int, str]]:
        if inventory == "unavailable":
            raise InstallError("Darwin inventory unavailable", "cleanup_failed")
        return {child.pid: (child.pid, "Z" if inventory == "zombie" else "S")}

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(core, "_darwin_processes", processes)
    monkeypatch.setattr(os, "killpg", deny)
    try:
        if inventory == "zombie":
            core._signal_command_group(child.pid, denied_signal)
        else:
            with pytest.raises(InstallError) as error:
                core._signal_command_group(child.pid, denied_signal)
            assert error.value.code == "cleanup_failed"
        assert calls == [denied_signal]
        assert child.returncode is None
    finally:
        real_killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_darwin_zombie_permission_does_not_prevent_owned_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    deadline = time.monotonic() + 5
    try:
        while core.owned_child_running(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not core.owned_child_running(child)
        assert child.returncode is None
        calls: list[int] = []

        def deny(group: int, number: int) -> None:
            assert group == child.pid and child.returncode is None
            calls.append(number)
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            core, "_darwin_processes", lambda: {child.pid: (child.pid, "Z")}
        )
        monkeypatch.setattr(os, "killpg", deny)
        core.stop_command_group(child)
        assert calls == [signal.SIGTERM, signal.SIGKILL]
        assert child.returncode == 0
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=5)
