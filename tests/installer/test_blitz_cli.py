from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cairn_install import cli, workflow
from cairn_install.core import InstallError, open_context


def _recorded_installation(state_root: Path, name: str = "demo") -> None:
    with open_context(
        state_root,
        name,
        create={
            "source": str(state_root / "source-that-may-be-gone"),
            "mode": "native",
            "port": 8123,
            "semantic": True,
        },
    ):
        pass


def _destructive_result(state_root: Path) -> dict[str, Any]:
    return {
        "name": "demo",
        "status": "deleted",
        "endpoint": "http://127.0.0.1:8123",
        "state": str(state_root / "demo" / "state.json"),
        "log": str(state_root / "demo" / "commands.log"),
        "credential_file": str(
            state_root / "demo" / "instance" / "credentials" / "admin.token"
        ),
        "deleted": {"private_state": True},
    }


def test_interactive_blitz_requires_the_exact_instance_name_before_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "not-demo")
    monkeypatch.setattr(
        workflow,
        "blitz_install",
        lambda _ctx: pytest.fail("blitz ran without exact confirmation"),
        raising=False,
    )

    exit_code = cli.main(["blitz", "--name", "demo", "--state-root", str(state_root)])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "IRREVERSIBLE" in output.out
    assert "permanently deletes" in output.out
    assert "not secure erasure" in output.out
    assert "nothing was deleted" in output.err
    assert (state_root / "demo" / "state.json").exists()


def test_interactive_blitz_runs_after_exact_confirmation_without_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "demo")
    monkeypatch.setattr(
        cli,
        "source_fingerprint",
        lambda _source: pytest.fail("blitz must not fingerprint the source"),
    )
    seen: list[tuple[str, bool]] = []

    def blitz_install(ctx: object) -> dict[str, str]:
        seen.append(
            (
                ctx.name,  # type: ignore[attr-defined]
                ctx.verbose,  # type: ignore[attr-defined]
            )
        )
        return {"name": "demo", "status": "deleted"}

    monkeypatch.setattr(workflow, "blitz_install", blitz_install, raising=False)

    assert cli.main(["blitz", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert seen == [("demo", False)]


def test_non_interactive_blitz_requires_yes_without_reading_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"read stdin: {prompt}")
    )
    monkeypatch.setattr(
        workflow,
        "blitz_install",
        lambda _ctx: pytest.fail("blitz ran without --yes"),
        raising=False,
    )

    exit_code = cli.main(
        [
            "blitz",
            "--non-interactive",
            "--name",
            "demo",
            "--state-root",
            str(state_root),
        ]
    )

    assert exit_code == 2
    assert "--yes is required" in capsys.readouterr().err
    assert (state_root / "demo" / "state.json").exists()


def test_yes_bypasses_interactive_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"read stdin: {prompt}")
    )
    called: list[str] = []

    def blitz_install(ctx: object) -> dict[str, str]:
        name = ctx.name  # type: ignore[attr-defined]
        called.append(name)
        return {"name": name, "status": "deleted"}

    monkeypatch.setattr(
        workflow,
        "blitz_install",
        blitz_install,
        raising=False,
    )

    assert (
        cli.main(
            [
                "blitz",
                "--yes",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
            ]
        )
        == 0
    )
    assert called == ["demo"]


def test_confirmed_blitz_of_missing_state_fails_without_creating_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(
        workflow,
        "blitz_install",
        lambda _ctx: pytest.fail("missing state reached the workflow"),
        raising=False,
    )

    exit_code = cli.main(
        [
            "blitz",
            "--non-interactive",
            "--yes",
            "--name",
            "missing",
            "--state-root",
            str(state_root),
        ]
    )

    assert exit_code == 2
    assert "No recorded installation named missing" in capsys.readouterr().err
    assert not (state_root / "missing").exists()


def test_blitz_default_result_omits_stale_paths_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    result = _destructive_result(state_root)
    monkeypatch.setattr(workflow, "blitz_install", lambda _ctx: result, raising=False)

    assert (
        cli.main(
            [
                "blitz",
                "--non-interactive",
                "--yes",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Blitz complete" in output
    assert "Name: demo" in output
    assert "Status: deleted" in output
    assert "Endpoint:" not in output
    assert "State:" not in output
    assert "Log:" not in output
    assert "Credential:" not in output


def test_verbose_blitz_prints_full_result_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)
    result = _destructive_result(state_root)
    monkeypatch.setattr(workflow, "blitz_install", lambda _ctx: result, raising=False)

    assert (
        cli.main(
            [
                "blitz",
                "--non-interactive",
                "--yes",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
                "--verbose",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    document = json.loads(output[output.index("{") :])
    assert document == result


def test_failed_blitz_tells_operator_to_retry_blitz_and_retains_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    _recorded_installation(state_root)

    def fail(_ctx: object) -> None:
        raise InstallError("injected deletion failure", "deletion_failed")

    monkeypatch.setattr(workflow, "blitz_install", fail, raising=False)

    exit_code = cli.main(
        [
            "blitz",
            "--non-interactive",
            "--yes",
            "--name",
            "demo",
            "--state-root",
            str(state_root),
        ]
    )

    error = capsys.readouterr().err
    assert exit_code == 2
    assert "Recovery information retained; retry blitz with the same name." in error
    assert "resume" not in error.lower()
    assert (state_root / "demo" / "state.json").exists()
