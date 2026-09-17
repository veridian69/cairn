import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from cairn_install.core import Context, InstallError, open_context


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


def test_state_preserves_identity_and_serialises_mutation(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        identity = ctx.instance_id
        with pytest.raises(InstallError, match="another installer"):
            create(tmp_path)
        ctx.state["steps"]["configure"] = "running"
        ctx.save()
    with open_context(tmp_path / "state", "demo") as resumed:
        assert resumed.instance_id == identity
        assert resumed.state["steps"]["configure"] == "running"


def test_quiet_commands_keep_diagnostics_in_plain_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        assert (
            ctx.command([sys.executable, "-c", "print('diagnostic-detail')"]).strip()
            == "diagnostic-detail"
        )
        assert "diagnostic-detail" not in capsys.readouterr().out
        assert "diagnostic-detail" in (ctx.directory / "commands.log").read_text()


def test_verbose_output_still_redacts_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        ctx.verbose = True
        ctx.add_secret("private-value")
        ctx.command(
            [
                sys.executable,
                "-c",
                "print('diagnostic-' + 'detail'); print('private-' + 'value')",
            ]
        )
        output = capsys.readouterr().out
        assert "diagnostic-detail" in output
        assert "private-value" not in output
        assert "[redacted]" in output


def test_quiet_failure_shows_bounded_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError):
            ctx.command(
                [
                    sys.executable,
                    "-c",
                    "import sys;print('useful failure',file=sys.stderr);sys.exit(7)",
                ]
            )
        assert "useful failure" in capsys.readouterr().out


def test_refuses_symlink_and_foreign_file(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.write_text("keep")
    with create(tmp_path) as ctx:
        path = ctx.root / "config.yaml"
        path.symlink_to(foreign)
        with pytest.raises(InstallError):
            ctx.write_file(path, "new")
        assert foreign.read_text() == "keep"
        path.unlink()
        path.write_text("new")
        with pytest.raises(InstallError, match="unowned"):
            ctx.write_file(path, "new")


def test_owned_file_reconciles_but_detects_drift(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "config.yaml"
        ctx.write_file(path, "original")
        ctx.write_file(path, "original")
        path.write_text("changed")
        with pytest.raises(InstallError, match="changed"):
            ctx.check_file(path)


def test_secret_capture_never_enters_log(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        ctx.add_secret("secret-value")
        ctx.command([sys.executable, "-c", "print('secret-' + 'value')"])
        capture = ctx.root / "credentials" / "capture.json"
        ctx.command(
            [sys.executable, "-c", "print('private-' + 'value')"],
            private=True,
            stdout_path=capture,
        )
        assert capture.read_text().strip() == "private-value"
        assert capture.stat().st_mode & 0o777 == 0o600
        log = (ctx.directory / "commands.log").read_text()
        assert "secret-value" not in log
        assert "private-value" not in log
        assert "[redacted]" in log


def test_failed_command_does_not_exit_parent_and_is_actionable(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="exit 7"):
            ctx.command([sys.executable, "-c", "raise SystemExit(7)"])
        assert (
            ctx.command([sys.executable, "-c", "print('still running')"]).strip()
            == "still running"
        )


def test_environment_overrides_are_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "victim")
    monkeypatch.setenv("CAIRN_IMAGE", "wrong")
    with create(tmp_path) as ctx:
        output = ctx.command(
            [
                sys.executable,
                "-c",
                "import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(('CAIRN_', 'COMPOSE_'))}))",
            ]
        )
        assert json.loads(output) == {}


def test_state_permissions_and_name_validation(tmp_path: Path) -> None:
    with pytest.raises(InstallError):
        open_context(tmp_path, "../bad", create={})
    with create(tmp_path) as ctx:
        assert ctx.directory.stat().st_mode & 0o777 == 0o700
        assert (ctx.directory / "state.json").stat().st_mode & 0o777 == 0o600
        assert ctx.state["owner_uid"] == os.getuid()


def test_file_publication_reconciles_durable_intent_before_validation(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "config.yaml"
        ctx.state["file_intents"] = {str(path): hashlib.sha256(b"owned").hexdigest()}
        ctx.save()
        path.write_text("owned")
        path.chmod(0o600)
        ctx.check_file(path)
        assert str(path) in ctx.state["owned_files"]


def test_timeout_stops_grandchild_that_ignores_term(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    child = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    )
    parent = "import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="timed out"):
            ctx.command([sys.executable, "-c", parent, str(pid_file), child], timeout=1)
    pid = int(pid_file.read_text())
    path = Path(f"/proc/{pid}/stat")
    assert not path.exists() or path.read_text().split(") ", 1)[1].startswith("Z ")


def test_signal_during_spawn_waits_for_handle_then_cleans_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def interrupted_spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real(*args, **kwargs)  # type: ignore[call-overload]
        children.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process  # type: ignore[no-any-return]

    monkeypatch.setattr("cairn_install.core.subprocess.Popen", interrupted_spawn)
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="interrupted"):
            ctx.command([sys.executable, "-c", "import time;time.sleep(60)"])
    assert children[0].poll() is not None


def test_forced_child_colour_is_removed_from_console_and_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        ctx.verbose = True
        ctx.add_secret("private-value")
        ctx.command(
            [
                sys.executable,
                "-c",
                "esc=chr(27); print(esc+']8;;https://example.invalid'+chr(7)"
                "+esc+'[31mprivate-value'+esc+'[0m'+esc+']8;;'+esc+chr(92))",
            ]
        )
        output = capsys.readouterr().out
        log = (ctx.directory / "commands.log").read_text()
        for text in (output, log):
            assert "\033" not in text
            assert "private-value" not in text
            assert "[redacted]" in text


def test_command_display_tracks_visible_directory_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shlex

    other = tmp_path / "other directory"
    other.mkdir()
    with create(tmp_path) as ctx:
        ctx.command(["/bin/pwd"])
        ctx.command(["/bin/pwd"])
        # Hidden commands must not change the operator's displayed directory.
        ctx.command([sys.executable, "-c", "pass"], cwd=other)
        ctx.command(["/bin/pwd"])
        ctx.command(["/bin/pwd"], cwd=other)
        ctx.command(["/bin/pwd"])
        output = capsys.readouterr().out
        assert output.count(f"$ cd {shlex.quote(str(tmp_path))}\n") == 2
        assert output.count(f"$ cd {shlex.quote(str(other))}\n") == 1
        assert output.count("$ /bin/pwd") == 5
        assert "(cd " not in output
        assert "full command in the log" not in output
        log = (ctx.directory / "commands.log").read_text()
        assert shlex.join([sys.executable, "-c", "pass"]) in log


def test_verbose_directory_changes_are_visible_and_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        ctx.verbose = True
        ctx.add_secret("secret-value")
        ctx.command([sys.executable, "-c", "pass"], env={"EXAMPLE": "secret-value"})
        ctx.command(["/bin/pwd"])
        output = capsys.readouterr().out
        assert output.count("$ cd ") == 1
        assert "(cd " not in output
        assert "secret-value" not in output
        assert "[redacted]" in output


def test_private_stdin_reaches_child_without_disclosing_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Catches stdin omission and accidental publication of secret input/output.
    with create(tmp_path) as ctx:
        payload = b"private-input-not-registered-for-redaction"
        result = ctx.command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            stdin_data=payload,
            private=True,
        )
        assert result == payload.decode()
        assert payload.decode() not in (ctx.directory / "commands.log").read_text()
        assert payload.decode() not in capsys.readouterr().out


def test_stdin_requires_private_output(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="private"):
            ctx.command([sys.executable, "-c", "pass"], stdin_data=b"secret")
