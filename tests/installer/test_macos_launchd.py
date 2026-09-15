"""LaunchAgent ownership uses loaded configuration, never a diagnostic PID."""

import json
import os
import plistlib
from pathlib import Path
from typing import Any

import pytest

from cairn_install import launchd
from cairn_install.core import Context, InstallError, open_context


@pytest.mark.parametrize("architecture", ["arm64", "x86_64"])
def test_actual_print_fixture_preserves_top_level_identity(architecture: str) -> None:
    text = (
        Path(__file__).parent
        / "fixtures"
        / f"launchctl-print-macos26-{architecture}.txt"
    ).read_text()
    header, fields = launchd.parse_print(text)
    assert header.startswith("gui/501/invalid.example.cairn.probe.")
    assert fields["program"] == "/bin/sleep"
    assert fields["arguments"] == ["/bin/sleep", "300"]
    assert fields["type"] == "LaunchAgent"


@pytest.mark.parametrize("mutation", ["duplicate", "truncated", "nested"])
def test_ambiguous_print_refused(mutation: str) -> None:
    text = "gui/501/job = {\n\tprogram = /bin/sleep\n}\n"
    if mutation == "duplicate":
        text = text.replace("\tprogram", "\tprogram = /other\n\tprogram")
    elif mutation == "truncated":
        text = text[:-2]
    else:
        text = text.replace("\tprogram", "\tunexpected = {\n\t\tprogram").replace(
            "}\n", "\t}\n}\n"
        )
    with pytest.raises(InstallError):
        launchd.parse_print(text)


def make_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Context:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(launchd, "account_home", lambda: home)
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "mode": "native",
            "port": 18000,
            "semantic": False,
            "source": str(tmp_path),
            "source_fingerprint": "test",
        },
    )


def test_profile_clears_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent = launchd.DarwinLaunchAgent(ctx)
        profile = plistlib.loads(agent.contents.encode())
        assert profile["Program"] == "/usr/bin/env"
        assert profile["ProgramArguments"][:2] == ["/usr/bin/env", "-i"]
        assert "PYTHONNOUSERSITE=1" in profile["ProgramArguments"]
        assert profile["Label"].endswith(ctx.instance_id)
        assert profile["Umask"] == 63


def loaded_text(agent: launchd.DarwinLaunchAgent) -> str:
    fields: dict[str, Any] = {
        "path": str(agent.path),
        "type": "LaunchAgent",
        "state": "xpcproxy",
        "program": agent.argv[0],
        "arguments": agent.argv,
        "working directory": str(agent.root),
        "stdout path": str(agent.log),
        "stderr path": str(agent.log),
        "domain": f"{agent.domain} [100002]",
        "umask": "77",
        "exit timeout": "60",
        "minimum runtime": "10",
        "spawn type": "daemon (3)",
        "environment": ["OSLogRateLimit => 64", f"XPC_SERVICE_NAME => {agent.label}"],
        "properties": "keepalive | runatload | system service | tle system",
    }
    lines = [f"{agent.target} = {{"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines += [f"\t{key} = {{", *[f"\t\t{item}" for item in value], "\t}"]
        else:
            lines.append(f"\t{key} = {value}")
    return "\n".join([*lines, "}", ""])


class LaunchdHost:
    def __init__(self, agent: launchd.DarwinLaunchAgent) -> None:
        self.agent = agent
        self.loaded: str | None = None
        self.calls: list[tuple[str, ...]] = []
        self.after: str | None = None

    def command(self, *args: str) -> dict[str, Any]:
        self.calls.append(args)
        if args == ("manageruid",):
            return {"code": 0, "stdout": f"{self.agent.uid}\n", "stderr": ""}
        if args == ("print", self.agent.domain):
            return {"code": 0, "stdout": "GUI domain exists\n", "stderr": ""}
        if args == ("print", self.agent.target):
            if self.loaded is None:
                return {
                    "code": 113,
                    "stdout": "",
                    "stderr": f'Bad request.\nCould not find service "{self.agent.label}" in domain for user gui: {self.agent.uid}\n',
                }
            return {"code": 0, "stdout": self.loaded, "stderr": ""}
        if args[0] == "bootstrap":
            assert args == ("bootstrap", self.agent.domain, str(self.agent.path))
            self.loaded = loaded_text(self.agent)
        elif args == ("bootout", self.agent.target):
            self.loaded = None
        else:
            pytest.fail(f"Unexpected command: {args}")
        if self.after == args[0]:
            self.after = None
            raise InstallError("injected after external mutation")
        return {"code": 0, "stdout": "", "stderr": ""}


def agent_host(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> tuple[launchd.DarwinLaunchAgent, LaunchdHost]:
    agent = launchd.DarwinLaunchAgent(ctx)
    host = LaunchdHost(agent)
    monkeypatch.setattr(agent, "_command", host.command)
    monkeypatch.setattr(ctx, "command", lambda *args, **kwargs: "OK\n")
    return agent, host


def lease_file(ctx: Context) -> Path:
    data = ctx.root / "data"
    data.mkdir(exist_ok=True)
    path = data / ".cairn-instance.lock"
    path.write_text(ctx.instance_id + "\n")
    path.chmod(0o660)
    return path


def test_lifecycle_preserves_owned_data_and_recreates_removed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        assert agent.is_running()  # xpcproxy is loaded; HTTP readiness is separate.
        assert ctx.state["resources"][launchd.RESOURCE]["status"] == "loaded"
        agent.stop()
        assert host.loaded is None and agent.path.exists() and agent._lease is None
        agent.start()
        agent.restart()
        agent.remove()
        assert not agent.path.exists() and host.loaded is None
        assert agent._lease is not None
        agent.start()  # releases removal lease before launching again
        assert agent._lease is None and agent.path.exists()
        agent.remove()
        agent.close()


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("program = /usr/bin/env", "program = /bin/sh"),
        ("\t\t-i", "\t\t-iBAD"),
        ("exit timeout = 60", "exit timeout = 61"),
        ("minimum runtime = 10", "minimum runtime = 1"),
        ("OSLogRateLimit => 64", "CAIRN_CONFIG => /other"),
        ("keepalive | runatload", "runatload"),
    ],
)
def test_foreign_loaded_configuration_never_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, replacement: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        assert host.loaded is not None
        host.loaded = host.loaded.replace(field, replacement)
        host.calls.clear()
        for action in (agent.validate_ownership, agent.start, agent.stop, agent.remove):
            with pytest.raises(InstallError, match="foreign|properties"):
                action()
        assert not any(args[0] in {"bootstrap", "bootout"} for args in host.calls)
        assert agent.path.exists()


@pytest.mark.parametrize("operation", ["bootstrap", "bootout"])
def test_external_mutation_interruption_reconciles_by_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        if operation == "bootout":
            agent.start()
        host.after = operation
        action = agent.start if operation == "bootstrap" else agent.stop
        with pytest.raises(InstallError, match="injected"):
            action()
        action()
        assert [args[0] for args in host.calls].count(operation) == 1
        if operation == "bootstrap":
            agent.stop()


@pytest.mark.parametrize(
    "code,stderr", [(0, ""), (113, "permission denied"), (5, "I/O error")]
)
def test_empty_or_denied_observation_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stderr: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        real = host.command
        monkeypatch.setattr(
            agent,
            "_command",
            lambda *args: (
                {"code": code, "stdout": "", "stderr": stderr}
                if args == ("print", agent.target)
                else real(*args)
            ),
        )
        with pytest.raises(InstallError):
            agent.validate_ownership()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "mode"])
def test_unsafe_log_refused_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        if kind == "fifo":
            os.mkfifo(agent.log, 0o600)
        elif kind == "symlink":
            agent.log.symlink_to(ctx.root / "elsewhere")
        else:
            agent.log.write_text("")
            agent.log.chmod(0o644)
        with pytest.raises(InstallError, match="log"):
            agent.start()
        assert not any(args[0] == "bootstrap" for args in host.calls)


def test_remove_never_started_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        agent.remove()
        agent.remove()
        assert ctx.state["resources"][launchd.RESOURCE]["status"] == "removed"
        assert not any(args[0] == "bootout" for args in host.calls)


def test_lease_identity_read_only_after_holder_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl
    import threading
    import time

    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        descriptor = os.open(path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.ftruncate(descriptor, 0)

        def finish_publication() -> None:
            time.sleep(0.1)
            os.write(descriptor, (ctx.instance_id + "\n").encode())
            os.close(descriptor)

        worker = threading.Thread(target=finish_publication)
        worker.start()
        try:
            agent._quiesce(removal=False, never_started=False)
            assert agent._lease is not None
            with path.open("r+") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            agent.close()
            worker.join(timeout=2)


@pytest.mark.parametrize("fault", ["uuid", "symlink", "mode", "missing"])
def test_bad_recorded_lease_refuses_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        if fault == "uuid":
            path.write_text("other\n")
        elif fault == "mode":
            path.chmod(0o600)
        else:
            path.unlink()
            if fault == "symlink":
                path.symlink_to(ctx.root / "other")
        with pytest.raises(InstallError):
            agent._quiesce(removal=False, never_started=False)
        assert agent._lease is None


def test_partial_deletion_journal_allows_already_removed_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        agent.start()
        agent.remove()
        agent.close()
        ctx.state.update(status="blitzing", blitz_phase="resources_removed")
        journal = ctx.directory.parent / f".{ctx.name}.blitz.json"
        journal.write_text(json.dumps(ctx.state))
        journal.chmod(0o600)
        path.unlink()
        agent.remove()
        assert agent._lease is None
        assert ctx.state["resources"][launchd.RESOURCE]["status"] == "removed"


def test_path_disappearance_after_flock_is_not_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        real = fcntl.flock

        def disappear(fd: int, operation: int) -> None:
            real(fd, operation)
            path.unlink()

        monkeypatch.setattr(fcntl, "flock", disappear)
        with pytest.raises(InstallError, match="disappeared"):
            agent._quiesce(removal=True, never_started=True)
        assert agent._lease is None


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("deletion_fails", [False, True])
def test_blitz_holds_lease_through_deletion_and_closes_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resumed: bool, deletion_fails: bool
) -> None:
    import fcntl
    from types import SimpleNamespace

    from cairn_install import destruction, workflow

    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        agent.start()
        if resumed:
            agent.remove()
            agent.close()
            ctx.state["blitz_phase"] = "resources_removed"
        adapter = SimpleNamespace(blitz=agent.remove, close=agent.close)
        monkeypatch.setattr(
            workflow, "platform", SimpleNamespace(system=lambda: "Darwin")
        )
        monkeypatch.setattr(workflow, "backend", lambda _: adapter)
        monkeypatch.setattr(destruction, "check_instance_tree", lambda _: None)
        visited: list[bool] = []

        def delete(context: Context) -> None:
            assert context is ctx and agent._lease is not None
            with path.open("r+") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            visited.append(True)
            if deletion_fails:
                raise InstallError("delete interrupted")

        monkeypatch.setattr(destruction, "remove_instance_files", delete)
        if deletion_fails:
            with pytest.raises(InstallError, match="delete interrupted"):
                workflow.blitz_install(ctx)
        else:
            workflow.blitz_install(ctx)
        assert visited == [True] and agent._lease is None


def test_darwin_native_backend_selects_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from cairn_install import native

    with make_context(tmp_path, monkeypatch) as ctx:
        calls: list[str] = []
        delegate = SimpleNamespace(
            **{
                name: (lambda name=name: calls.append(name))
                for name in ("start", "stop", "restart", "remove", "close")
            }
        )
        monkeypatch.setattr(
            native, "platform", SimpleNamespace(system=lambda: "Darwin")
        )
        monkeypatch.setattr(launchd, "DarwinLaunchAgent", lambda _: delegate)
        backend = native.Backend(ctx)
        backend.start()
        backend.stop()
        backend.restart()
        backend.rollback()
        backend.blitz()
        backend.close()
        assert calls == [
            "start",
            "stop",
            "restart",
            "remove",
            "close",
            "remove",
            "close",
        ]


@pytest.mark.parametrize(
    "system,resource",
    [
        ("Linux", "native_launch_agent"),
        ("Linux", "native_launch_agent_intent"),
        ("Darwin", "native_unit"),
        ("Darwin", "native_unit_intent"),
    ],
)
def test_native_refuses_another_platform_service_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, resource: str
) -> None:
    from types import SimpleNamespace

    from cairn_install import native

    with make_context(tmp_path, monkeypatch) as ctx:
        ctx.state["resources"][resource] = {}
        monkeypatch.setattr(native, "platform", SimpleNamespace(system=lambda: system))
        with pytest.raises(InstallError, match="another platform"):
            native.Backend(ctx)


def test_structured_capture_retains_status_and_keeps_diagnostics_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent = launchd.DarwinLaunchAgent(ctx)
        expected = {"code": 113, "stdout": "", "stderr": "missing service"}
        seen: list[dict[str, Any]] = []

        def capture(argv: list[str], **kwargs: Any) -> str:
            assert argv[-2:] == ["print", agent.target]
            seen.append(kwargs)
            return json.dumps(expected)

        monkeypatch.setattr(ctx, "command", capture)
        assert agent._command("print", agent.target) == expected
        assert seen[0]["private"] is True and seen[0]["timeout"] == 80
        monkeypatch.setattr(
            ctx,
            "command",
            lambda *args, **kwargs: '{"code":true,"stdout":"","stderr":""}',
        )
        with pytest.raises(InstallError, match="result"):
            agent._command("print", agent.target)


def test_lease_contention_exhaustion_never_reports_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl
    from types import SimpleNamespace

    with make_context(tmp_path, monkeypatch) as ctx:
        agent, _ = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        agent.start()
        descriptor = os.open(path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        times = iter([0.0, 0.0, 70.0])
        monkeypatch.setattr(
            launchd,
            "time",
            SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None),
        )
        try:
            with pytest.raises(InstallError, match="did not become available"):
                agent.stop()
            assert ctx.state["resources"][launchd.RESOURCE]["status"] == "loaded"
            assert agent._lease is None
        finally:
            os.close(descriptor)


def test_unlink_interruption_reconciles_without_rebooting_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        real = Path.unlink

        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            real(path, *args, **kwargs)
            if path == agent.path:
                raise InstallError("interrupted after unlink")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", unlink)
            with pytest.raises(InstallError, match="interrupted after unlink"):
                agent.remove()
        agent.remove()
        assert not agent.path.exists() and agent._lease is not None
        assert [args[0] for args in host.calls].count("bootout") == 1
        agent.close()


@pytest.mark.parametrize("directory", ["Library", "LaunchAgents"])
@pytest.mark.parametrize("fault", ["group_write", "world_write", "foreign_owner"])
def test_unsafe_launchagent_parent_refuses_every_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str, fault: str
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        parent = agent.home / "Library"
        if directory == "LaunchAgents":
            parent /= "LaunchAgents"
        if fault == "foreign_owner":
            real = Path.lstat

            def foreign(path: Path) -> os.stat_result:
                info = real(path)
                if path == parent:
                    values = list(info)
                    values[4] = agent.uid + 1
                    return os.stat_result(values)
                return info

            monkeypatch.setattr(Path, "lstat", foreign)
        else:
            parent.chmod(0o770 if fault == "group_write" else 0o707)
        host.calls.clear()
        for action in (agent.validate_ownership, agent.start, agent.stop, agent.remove):
            with pytest.raises(InstallError, match="LaunchAgent parent"):
                action()
        assert not any(args[0] in {"bootstrap", "bootout"} for args in host.calls)
        assert agent.path.exists()
        parent.chmod(0o700)


@pytest.mark.parametrize("interrupted", [False, True])
def test_observed_auto_loaded_job_permanently_requires_data_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        path = lease_file(ctx)
        agent.start()
        # Simulate login loading a published plist before the installer recorded
        # its own bootstrap: the observed job still rules out never-started.
        ctx.state["resources"].pop("native_launch_agent_bootstrap_attempted")
        agent._status("materialised")
        agent._intent("start", "publish_pending")
        path.unlink()
        if interrupted:
            host.after = "bootout"
        with pytest.raises(InstallError, match="injected|lease disappeared"):
            agent.remove()
        saved = json.loads((ctx.directory / "state.json").read_text())
        assert saved["resources"]["native_launch_agent_bootstrap_attempted"] is True
        with pytest.raises(InstallError, match="lease disappeared"):
            agent.remove()
        assert [args[0] for args in host.calls].count("bootout") == 1


def test_unknown_loaded_scalar_cannot_hide_configuration_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        assert host.loaded is not None
        host.loaded = host.loaded.replace(
            "\tprogram =", "\troot directory = /unowned\n\tprogram ="
        )
        host.calls.clear()
        with pytest.raises(InstallError, match="Unsupported launchctl field"):
            agent.stop()
        assert not any(args[0] == "bootout" for args in host.calls)


@pytest.mark.parametrize("outcome", ["absent", "timeout", "foreign"])
def test_bootout_waits_for_validated_drain_without_repeating_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from types import SimpleNamespace

    with make_context(tmp_path, monkeypatch) as ctx:
        agent, host = agent_host(ctx, monkeypatch)
        lease_file(ctx)
        agent.start()
        real = host.command
        draining = False
        observations = 0
        elapsed = 0.0

        def monotonic() -> float:
            nonlocal elapsed
            elapsed += 35.0 if outcome == "timeout" else 0.1
            return elapsed

        def command(*args: str) -> dict[str, Any]:
            nonlocal draining, observations
            if args == ("bootout", agent.target):
                host.calls.append(args)
                draining = True
                return {"code": 0, "stdout": "", "stderr": ""}
            if draining and args == ("print", agent.target):
                observations += 1
                if outcome == "absent" and observations == 3:
                    host.loaded = None
                elif outcome == "foreign" and observations == 2:
                    host.loaded = loaded_text(agent).replace(
                        "program = /usr/bin/env", "program = /bin/sh"
                    )
            return real(*args)

        monkeypatch.setattr(agent, "_command", command)
        monkeypatch.setattr(
            launchd, "time", SimpleNamespace(monotonic=monotonic, sleep=lambda _: None)
        )
        if outcome == "absent":
            agent.stop()
            assert observations == 3
            assert ctx.state["resources"][launchd.RESOURCE]["status"] == "stopped"
        else:
            with pytest.raises(InstallError, match="remains loaded|foreign"):
                agent.stop()
            assert ctx.state["resources"][launchd.RESOURCE]["status"] == "loaded"
        assert [args[0] for args in host.calls].count("bootout") == 1
        assert agent._lease is None


def test_shutdown_observation_passes_remaining_deadline_to_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    with make_context(tmp_path, monkeypatch) as ctx:
        agent = launchd.DarwinLaunchAgent(ctx)
        agent._observation_deadline = 100.0
        monkeypatch.setattr(launchd, "time", SimpleNamespace(monotonic=lambda: 90.0))
        calls: list[float] = []

        def capture(argv: list[str], **kwargs: Any) -> str:
            assert argv[3] == "10.0"
            calls.append(kwargs["timeout"])
            return json.dumps({"code": 0, "stdout": "domain", "stderr": ""})

        monkeypatch.setattr(ctx, "command", capture)
        agent._command("print", agent.domain)
        assert calls == [10.0]
        agent._observation_deadline = 89.0
        with pytest.raises(InstallError, match="remains loaded") as error:
            agent._command("print", agent.domain)
        assert error.value.code == "cleanup_failed" and calls == [10.0]
