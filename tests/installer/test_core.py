import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cairn_install.core import MAX_OUTPUT, Context, InstallError, open_context


def reopen(tmp_path: Path) -> Context:
    return open_context(tmp_path / "state", "demo")


REAL_REPLACE = os.replace


def publish_crashes(src: Any, dst: Any) -> None:
    # Only the replacement's publish crashes; state.json uses os.replace too.
    if Path(str(src)).name.startswith(".replace-"):
        raise RuntimeError("simulated crash")
    REAL_REPLACE(src, dst)


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


def test_command_capture_limit_is_configurable_only_for_stdout_path(
    tmp_path: Path,
) -> None:
    size = MAX_OUTPUT + 1
    argv = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x' * {size})"]
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="limit"):
            ctx.command(argv, stdout_path=ctx.root / "default.tar")
        capture = ctx.root / "large.tar"
        assert ctx.command(argv, stdout_path=capture, stdout_limit=size) == ""
        assert capture.stat().st_size == size
        with pytest.raises(InstallError, match="limit"):
            ctx.command(argv, stdout_limit=size)
        with pytest.raises(InstallError, match="limit"):
            ctx.command(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stderr.buffer.write(b'x' * {size})",
                ],
                stdout_path=ctx.root / "stderr.tar",
                stdout_limit=size,
            )


def test_command_capture_enforces_limit_after_a_fast_child_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install import core

    def already_exited(process: subprocess.Popen[bytes]) -> bool:
        # Observe exit without reaping, so the normal completion path still owns it.
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        return False

    monkeypatch.setattr(core, "owned_child_running", already_exited)
    with create(tmp_path) as ctx:
        with pytest.raises(InstallError, match="limit"):
            ctx.command(
                [sys.executable, "-c", "print('too much output')"],
                stdout_path=ctx.root / "short.tar",
                stdout_limit=4,
            )


def test_command_streams_private_stdin_file_without_logging_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with create(tmp_path) as ctx:
        data = b"private archive contents\x00" * 100000
        source = ctx.root / "input.tar"
        source.write_bytes(data)
        capture = ctx.root / "output.tar"
        assert (
            ctx.command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ],
                private=True,
                stdin_path=source,
                stdout_path=capture,
                stdout_limit=len(data),
            )
            == ""
        )
        assert capture.read_bytes() == data
        assert "private archive contents" not in capsys.readouterr().out
        assert (
            "private archive contents"
            not in (ctx.directory / "commands.log").read_text()
        )
        with pytest.raises(InstallError, match="exit 3"):
            ctx.command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.buffer.write(sys.stdin.buffer.read(64)); sys.exit(3)",
                ],
                private=True,
                stdin_path=source,
            )
        assert "private archive contents" not in capsys.readouterr().out
        assert (
            "private archive contents"
            not in (ctx.directory / "commands.log").read_text()
        )
        with pytest.raises(InstallError, match="private"):
            ctx.command([sys.executable], stdin_path=source)
        with pytest.raises(InstallError, match="both|exclusive"):
            ctx.command(
                [sys.executable], private=True, stdin_path=source, stdin_data=b"other"
            )


@pytest.mark.parametrize(
    "kind", ["symlink", "parent-symlink", "fifo", "directory", "missing"]
)
def test_command_stdin_path_refuses_unsafe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("unsafe stdin must be refused before spawning"),
    )
    with create(tmp_path) as ctx:
        source = ctx.root / "input.tar"
        if kind == "symlink":
            target = ctx.root / "real.tar"
            target.write_bytes(b"protected")
            source.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(source)
        elif kind == "directory":
            source.mkdir()
        elif kind == "parent-symlink":
            real = ctx.root / "real"
            real.mkdir()
            (real / "input.tar").write_bytes(b"protected")
            (ctx.root / "link").symlink_to(real, target_is_directory=True)
            source = ctx.root / "link" / "input.tar"
        capture = ctx.root / "output.tar"
        with pytest.raises(InstallError):
            ctx.command(
                [sys.executable, "-c", "raise Exception('must not run')"],
                private=True,
                stdin_path=source,
                stdout_path=capture,
            )
        # A refused input leaves no exclusive capture to block the retry.
        assert not capture.exists()


@pytest.mark.parametrize("limit", [0, -1, True])
def test_command_capture_refuses_invalid_limits(tmp_path: Path, limit: int) -> None:
    with create(tmp_path) as ctx:
        capture = ctx.root / "output.tar"
        with pytest.raises(InstallError, match="positive integer"):
            ctx.command([sys.executable], stdout_path=capture, stdout_limit=limit)
        assert not capture.exists()


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


def test_quiet_kubectl_reads_stay_in_the_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\nprintf 'synthetic pod inventory\\n'\n")
    kubectl.chmod(0o755)
    with create(tmp_path) as ctx:
        assert "synthetic pod inventory" in ctx.command(
            [str(kubectl), "--context", "test", "get", "pods", "-o", "json"]
        )
        assert capsys.readouterr().out == ""
        log = (ctx.directory / "commands.log").read_text()
        assert "kubectl --context test get pods -o json" in log
        assert "synthetic pod inventory" in log


def test_kubectl_context_named_like_read_does_not_hide_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\nprintf 'deleted synthetic resource\\n'\n")
    kubectl.chmod(0o755)
    with create(tmp_path) as ctx:
        ctx.command(
            [
                str(kubectl),
                "--context",
                "get",
                "--namespace",
                "wait",
                "delete",
                "pod/demo",
            ]
        )
        output = capsys.readouterr().out
        assert "delete pod/demo" in output


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


def test_children_keep_proxy_and_index_configuration_but_not_owned_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxied or mirrored host must still fetch; loopback stays direct."""
    monkeypatch.setenv("https_proxy", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("no_proxy", "intranet.example.invalid")
    monkeypatch.setenv("UV_INDEX_URL", "https://mirror.example.invalid/simple")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "hijack"))
    monkeypatch.setenv("UV_PROJECT", str(tmp_path / "other-project"))
    monkeypatch.setenv("UV_SYSTEM_PYTHON", "1")
    monkeypatch.setenv("CAIRN_IMAGE", "wrong")
    with create(tmp_path) as ctx:
        output = ctx.command(
            [
                sys.executable,
                "-c",
                "import os,json;print(json.dumps({k:v for k,v in os.environ.items() "
                "if k.lower().endswith('_proxy') or k.startswith(('UV_', 'CAIRN_'))}))",
            ]
        )
    child = json.loads(output)
    assert child["https_proxy"] == "http://proxy.example.invalid:3128"
    assert child["HTTP_PROXY"] == "http://proxy.example.invalid:3128"
    assert child["UV_INDEX_URL"] == "https://mirror.example.invalid/simple"
    assert child["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
    assert child["no_proxy"] == "intranet.example.invalid,127.0.0.1,localhost"
    assert child["NO_PROXY"] == "127.0.0.1,localhost"
    assert "UV_PROJECT_ENVIRONMENT" not in child
    assert "UV_PROJECT" not in child
    assert "UV_SYSTEM_PYTHON" not in child
    assert "CAIRN_IMAGE" not in child


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


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_replace_unchanged_content_is_a_no_op(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "original")
        before = copy.deepcopy(ctx.state)
        assert ctx.replace_file(path, "original") is False
        assert ctx.state == before
        assert "file_replacements" not in ctx.state
        assert ctx.pending_replacements() == {}


def test_replace_updates_bytes_mode_ownership_and_clears_journal(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old", mode=0o640)
        assert ctx.replace_file(path, "new", mode=0o640) is True
        assert path.read_text() == "new"
        assert path.stat().st_mode & 0o777 == 0o640
        assert ctx.state["owned_files"][str(path)] == sha("new")
        assert ctx.pending_replacements() == {}
        assert ctx.state["file_replacements"] == {}
        assert not list(path.parent.glob(".replace-*"))
    with reopen(tmp_path) as resumed:
        assert resumed.state["owned_files"][str(path)] == sha("new")
        assert resumed.pending_replacements() == {}


def test_replace_refuses_unowned_file(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        path.write_text("foreign")
        path.chmod(0o600)
        with pytest.raises(InstallError, match="Refusing to replace unowned file"):
            ctx.replace_file(path, "new")
        assert path.read_text() == "foreign"
        assert ctx.pending_replacements() == {}


def test_replace_refuses_externally_modified_file(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "original")
        path.write_text("changed")
        with pytest.raises(
            InstallError, match="Owned file changed; refusing replacement"
        ):
            ctx.replace_file(path, "new")
        assert path.read_text() == "changed"
        assert ctx.pending_replacements() == {}


def test_replace_refuses_mode_drift(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "original")
        path.chmod(0o640)
        with pytest.raises(InstallError, match="permissions changed"):
            ctx.replace_file(path, "new")
        assert path.read_text() == "original"
        assert ctx.pending_replacements() == {}


def test_replace_refuses_symlink_and_leaves_target_alone(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.write_text("keep")
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "original")
        path.unlink()
        path.symlink_to(foreign)
        with pytest.raises(InstallError):
            ctx.replace_file(path, "new")
        assert foreign.read_text() == "keep"
        assert ctx.pending_replacements() == {}


def test_crash_before_publish_resumes_with_same_content(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    assert path.read_text() == "old"
    assert not list(path.parent.glob(".replace-*"))
    with reopen(tmp_path) as resumed:
        assert resumed.pending_replacements() == {
            str(path): {"old": sha("old"), "new": sha("new")}
        }
        assert resumed.replace_file(path, "new") is True
        assert path.read_text() == "new"
        assert resumed.state["owned_files"][str(path)] == sha("new")
        assert resumed.pending_replacements() == {}


def test_crash_after_publish_finalises_on_resume(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        real_save = Context.save
        saves: list[int] = []

        def crash_before_final_save(self: Context) -> None:
            saves.append(1)
            # First save journals the intent; the second follows the publish.
            if len(saves) == 2:
                raise RuntimeError("simulated crash")
            real_save(self)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Context, "save", crash_before_final_save)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    assert path.read_text() == "new"
    with reopen(tmp_path) as resumed:
        assert resumed.pending_replacements() == {
            str(path): {"old": sha("old"), "new": sha("new")}
        }
        assert resumed.replace_file(path, "new") is True
        assert resumed.state["owned_files"][str(path)] == sha("new")
        assert resumed.pending_replacements() == {}


def test_restore_after_unfinalised_publish_adopts_then_replaces(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        real_save = Context.save
        saves: list[int] = []

        def crash_before_final_save(self: Context) -> None:
            saves.append(1)
            if len(saves) == 2:
                raise RuntimeError("simulated crash")
            real_save(self)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Context, "save", crash_before_final_save)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    with reopen(tmp_path) as resumed:
        assert resumed.replace_file(path, "old") is True
        assert path.read_text() == "old"
        assert resumed.state["owned_files"][str(path)] == sha("old")
        assert resumed.pending_replacements() == {}


def test_request_for_old_content_after_failed_publish_clears_journal(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    with reopen(tmp_path) as resumed:
        assert resumed.replace_file(path, "old") is False
        assert path.read_text() == "old"
        assert resumed.pending_replacements() == {}
        resumed.check_file(path)


def test_resume_with_different_content_replaces_when_old_remains(
    tmp_path: Path,
) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "first")
    with reopen(tmp_path) as resumed:
        assert resumed.replace_file(path, "second") is True
        assert path.read_text() == "second"
        assert resumed.state["owned_files"][str(path)] == sha("second")
        assert resumed.pending_replacements() == {}


def test_resume_refuses_unexpected_third_content(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    path.write_text("surprise")
    path.chmod(0o600)
    with reopen(tmp_path) as resumed:
        with pytest.raises(InstallError, match="Interrupted replacement"):
            resumed.replace_file(path, "new")
        assert path.read_text() == "surprise"
        assert resumed.pending_replacements() == {
            str(path): {"old": sha("old"), "new": sha("new")}
        }


def test_pending_journal_blocks_check_and_write(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    with reopen(tmp_path) as resumed:
        with pytest.raises(InstallError, match="interrupted replacement"):
            resumed.check_file(path)
        with pytest.raises(InstallError, match="interrupted replacement"):
            resumed.write_file(path, "other")
        assert path.read_text() == "old"


def test_secret_replacement_content_never_enters_log(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "credentials.env"
        ctx.write_file(path, "placeholder")
        assert ctx.replace_file(path, "secret-value", secret=True) is True
        assert path.read_text() == "secret-value"
        log = (ctx.directory / "commands.log").read_text()
        assert "secret-value" not in log
        assert "Replace protected credential file" in log
        assert "contents omitted" in log


def test_cancelled_replacement_still_checks_permissions(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", publish_crashes)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    path.chmod(0o644)
    with reopen(tmp_path) as resumed:
        with pytest.raises(InstallError, match="permissions changed"):
            resumed.replace_file(path, "old")
        assert str(path) in resumed.pending_replacements()


def test_replace_refuses_targets_outside_private_tree(tmp_path: Path) -> None:
    with create(tmp_path) as ctx:
        outside = tmp_path / "shared" / "compose.yaml"
        outside.parent.mkdir(mode=0o777)
        outside.write_text("old")
        ctx.state["owned_files"][str(outside)] = sha("old")
        for target in (outside, ctx.root / ".." / "state.json", ctx.root):
            with pytest.raises(InstallError, match="private instance tree"):
                ctx.replace_file(target, "new")
        assert outside.read_text() == "old"
        assert ctx.pending_replacements() == {}


def test_adoption_makes_rename_durable_before_clearing_journal(
    tmp_path: Path,
) -> None:
    import cairn_install.core as core

    with create(tmp_path) as ctx:
        path = ctx.root / "compose.yaml"
        ctx.write_file(path, "old")
        real_save = Context.save
        saves: list[int] = []

        def crash_before_final_save(self: Context) -> None:
            saves.append(1)
            if len(saves) == 2:
                raise RuntimeError("simulated crash")
            real_save(self)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Context, "save", crash_before_final_save)
            with pytest.raises(RuntimeError, match="simulated crash"):
                ctx.replace_file(path, "new")
    events: list[str] = []
    real_fsync = core._fsync_directory

    def record_fsync(directory: Path) -> None:
        events.append(f"fsync:{directory}")
        real_fsync(directory)

    def record_save(self: Context) -> None:
        events.append(
            "save:" + ",".join(sorted(self.state.get("file_replacements", {})))
        )
        real_save(self)

    with reopen(tmp_path) as resumed:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(core, "_fsync_directory", record_fsync)
            patch.setattr(Context, "save", record_save)
            assert resumed.replace_file(path, "new") is True
    assert events[0] == f"fsync:{path.parent}"
    assert events[1] == "save:"
