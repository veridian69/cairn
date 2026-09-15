from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn_install import cli
from cairn_install.core import InstallError, open_context


def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "src" / "cairn").mkdir(parents=True)
    (source / "src" / "cairn" / "__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    (source / "uv.lock").write_text("version = 1\n")
    return source


def install_workflow(monkeypatch: pytest.MonkeyPatch, **functions: object) -> None:
    from cairn_install import workflow

    defaults: dict[str, object] = {
        "run_install": lambda ctx: None,
        "status_install": lambda ctx: None,
        "rollback_install": lambda ctx: None,
    }
    defaults.update(functions)
    for name, function in defaults.items():
        monkeypatch.setattr(workflow, name, function)


def test_non_interactive_missing_required_values_never_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"read stdin: {prompt}")
    )

    result = cli.main(["--non-interactive", "--state-root", str(tmp_path)])

    assert result == 2
    assert "--mode is required in non-interactive mode" in capsys.readouterr().err


def test_interactive_wizard_uses_clear_feature_names_and_shows_plan_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    source = source_tree(tmp_path)
    answers = iter(["native", "demo", "8123", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def run_install(ctx: object) -> None:
        output = capsys.readouterr().out
        assert "Attic only" in output
        assert "Attic plus semantic search" in output
        assert "Configuration" in output
        assert "Mode: native" in output
        assert "Port: 8123" in output

    install_workflow(monkeypatch, run_install=run_install)

    assert (
        cli.main(["--state-root", str(tmp_path / "state")], default_source=source) == 0
    )


def test_disposable_is_always_attic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "disposable",
            "--name",
            "demo",
            "--semantic",
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "Disposable mode supports Attic only" in capsys.readouterr().err


def test_provider_key_flag_requires_semantic_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    key_file = tmp_path / "unused-key"
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "native",
            "--name",
            "demo",
            "--provider-key-file",
            str(key_file),
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "--provider-key-file requires --semantic" in capsys.readouterr().err
    assert not key_file.exists()


def test_semantic_setup_creates_protected_key_file_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    install_workflow(
        monkeypatch,
        run_install=lambda ctx: pytest.fail("workflow must wait for the key"),
    )
    arguments = [
        "--non-interactive",
        "--mode",
        "native",
        "--name",
        "demo",
        "--semantic",
        "--state-root",
        str(state_root),
    ]

    assert cli.main(arguments, default_source=source) == 2

    key_file = state_root / "demo" / "openai-api-key"
    state = json.loads((state_root / "demo" / "state.json").read_text())
    assert key_file.read_bytes() == b""
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert state["status"] == "planned"
    first_ids = state["run_id"], state["instance_id"]
    message = capsys.readouterr().err
    assert str(key_file) in message
    assert "edit the protected file, then rerun" in message.lower()

    key_file.write_text("sk-example-super-secret-value\n")
    os.chmod(key_file, 0o600)
    seen: dict[str, object] = {}

    def run_install(ctx: object) -> None:
        seen["ids"] = ctx.run_id, ctx.instance_id  # type: ignore[attr-defined]
        provider = Path(ctx.state["provider_key_file"])  # type: ignore[attr-defined]
        seen["provider"] = provider
        assert provider.read_text() == "sk-example-super-secret-value\n"
        assert stat.S_IMODE(provider.stat().st_mode) == 0o600

    install_workflow(monkeypatch, run_install=run_install)
    assert cli.main(arguments, default_source=source) == 0
    assert seen["ids"] == first_ids
    assert (
        seen["provider"]
        == state_root / "demo" / "instance" / "credentials" / "openai-api-key"
    )
    public = capsys.readouterr()
    logs = (state_root / "demo" / "commands.log").read_text()
    assert "sk-example-super-secret-value" not in public.out + public.err + logs

    key_file.unlink()
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert not key_file.exists()


def test_missing_explicit_provider_key_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    missing = tmp_path / "keys" / "openai"
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "docker",
            "--name",
            "demo",
            "--semantic",
            "--provider-key-file",
            str(missing),
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "Provider key file does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_interrupted_provider_copy_resumes_from_protected_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    create = {
        "mode": "native",
        "port": 8000,
        "semantic": True,
        "source": str(source),
        "source_fingerprint": cli.source_fingerprint(source),
    }
    with open_context(state_root, "demo", create=create) as ctx:
        input_path = ctx.directory / "openai-api-key"
        input_path.write_text("sk-example-interrupted-secret\n")
        os.chmod(input_path, 0o600)

        def interrupt(*args: object, **kwargs: object) -> None:
            raise InstallError("injected interruption", "interrupted")

        monkeypatch.setattr(ctx, "write_file", interrupt)
        with pytest.raises(InstallError, match="injected interruption"):
            cli._prepare_provider_key(ctx, None)
        assert "provider_key_file" not in ctx.state

    with open_context(state_root, "demo") as resumed:
        cli._prepare_provider_key(resumed, None)
        provider = resumed.root / "credentials" / "openai-api-key"
        assert resumed.state["provider_key_file"] == str(provider)
        assert provider.read_text() == "sk-example-interrupted-secret\n"


def test_source_fingerprint_is_content_based_and_ignores_evidence(
    tmp_path: Path,
) -> None:
    source = source_tree(tmp_path)
    (source / "README.md").write_text("Cairn\n")
    before = cli.source_fingerprint(source)
    (source / "docs" / "evidence").mkdir(parents=True)
    (source / "docs" / "evidence" / "run.log").write_text("changing output")

    assert cli.source_fingerprint(source) == before

    (source / "src" / "cairn" / "__init__.py").write_text("VERSION = 2\n")
    assert cli.source_fingerprint(source) != before

    changed_source = source_tree(tmp_path / "other")
    (changed_source / "README.md").write_text("Changed package metadata\n")
    assert cli.source_fingerprint(changed_source) != cli.source_fingerprint(
        source_tree(tmp_path / "baseline")
    )

    (source / "deploy").mkdir()
    (source / "deploy" / "images.lock").write_text("falkordb=first\n")
    image_fingerprint = cli.source_fingerprint(source)
    (source / "deploy" / "images.lock").write_text("falkordb=second\n")
    assert cli.source_fingerprint(source) != image_fingerprint


def test_source_fingerprint_refuses_symlinked_install_tree(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    real_package = source / "src" / "real-cairn"
    (source / "src" / "cairn").rename(real_package)
    (source / "src" / "cairn").symlink_to(real_package, target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        cli.source_fingerprint(source)


def test_resume_reuses_recorded_options_and_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    captured: list[tuple[str, str, str, int, bool]] = []

    def run_install(ctx: object) -> None:
        captured.append(
            (
                ctx.run_id,  # type: ignore[attr-defined]
                ctx.instance_id,  # type: ignore[attr-defined]
                ctx.mode,  # type: ignore[attr-defined]
                ctx.port,  # type: ignore[attr-defined]
                ctx.semantic,  # type: ignore[attr-defined]
            )
        )

    install_workflow(monkeypatch, run_install=run_install)
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "docker",
                "--name",
                "demo",
                "--port",
                "8123",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert captured == [captured[0], captured[0]]
    assert captured[0][2:] == ("docker", 8123, False)


@pytest.mark.parametrize("operation", ["status", "rollback"])
def test_recovery_operations_do_not_require_source(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    called: list[str] = []
    install_workflow(monkeypatch)
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    source.rename(tmp_path / "source-gone")
    install_workflow(
        monkeypatch,
        status_install=lambda ctx: called.append("status"),
        rollback_install=lambda ctx: called.append("rollback"),
    )

    assert cli.main([operation, "--name", "demo", "--state-root", str(state_root)]) == 0
    assert called == [operation]


def test_source_launcher_runs_without_an_installed_package(tmp_path: Path) -> None:
    launcher = Path(__file__).parents[2] / "cairn-install"
    result = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Guided Cairn installer" in result.stdout


def test_sigterm_stops_active_child_before_releasing_lock(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    child_pid_file = tmp_path / "child.pid"
    child_program = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    program = f"""
import sys
from pathlib import Path
from cairn_install import cli, workflow

def run_install(ctx):
    ctx.command([sys.executable, '-c', {child_program!r}], cwd=ctx.directory)

workflow.run_install = run_install
raise SystemExit(cli.main([
    '--non-interactive', '--mode', 'native', '--name', 'signal-test',
    '--state-root', {str(state_root)!r}
], default_source=Path({str(source)!r})))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[2],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert child_pid_file.exists(), process.communicate(timeout=1)
        child_pid = int(child_pid_file.read_text())

        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=10)

        assert process.returncode == 2
        assert "interrupted" in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        with open_context(state_root, "signal-test") as ctx:
            assert ctx.state["run_id"]
            assert ctx.state["instance_id"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_main_restores_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)
    previous = {
        number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)
    }

    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "restore",
                "--state-root",
                str(tmp_path / "state"),
            ],
            default_source=source,
        )
        == 0
    )

    assert {number: signal.getsignal(number) for number in previous} == previous


def test_main_outside_main_thread_does_not_install_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda *args: pytest.fail("signal handlers touched outside main thread"),
    )
    results: list[int] = []
    worker = threading.Thread(
        target=lambda: results.append(
            cli.main(
                [
                    "--non-interactive",
                    "--mode",
                    "native",
                    "--name",
                    "threaded",
                    "--state-root",
                    str(tmp_path / "thread-state"),
                ],
                default_source=source,
            )
        )
    )

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert results == [0]


@pytest.mark.parametrize("operation", ["status", "rollback", "blitz", "ls"])
def test_keep_running_rejects_non_install_operations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    assert cli.main([operation, "--keep-running", "--state-root", str(tmp_path)]) == 2
    assert "only valid with install or resume" in capsys.readouterr().err


def test_keep_running_install_passes_foreground_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    seen: list[bool] = []

    def run_install(ctx: object, *, keep_running: bool = False) -> None:
        seen.append(keep_running)

    install_workflow(monkeypatch, run_install=run_install)
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "disposable",
                "--name",
                "mac",
                "--port",
                "19234",
                "--source",
                str(source),
                "--state-root",
                str(tmp_path / "state"),
                "--non-interactive",
                "--keep-running",
            ]
        )
        == 0
    )
    assert seen == [True]


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_incompatible_keep_running_does_not_create_state_or_request_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    state = tmp_path / "state"
    assert (
        cli.main(
            [
                "install",
                "--mode",
                mode,
                "--semantic",
                "--keep-running",
                "--name",
                "wrong",
                "--port",
                "19234",
                "--state-root",
                str(state),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert "--keep-running requires disposable mode" in capsys.readouterr().err
    assert not state.exists()
