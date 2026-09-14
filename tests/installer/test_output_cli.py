from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn_install import cli
from cairn_install.output import paint


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "src" / "cairn").mkdir(parents=True)
    (source / "src" / "cairn" / "__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    return source


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("stage", "\033[1;36mStage\033[0m"),
        ("command", "\033[2mStage\033[0m"),
        ("success", "\033[1;32mStage\033[0m"),
        ("warning", "\033[33mStage\033[0m"),
        ("error", "\033[1;31mStage\033[0m"),
        ("plain", "Stage"),
    ],
)
def test_paint_uses_restrained_styles_only_for_a_tty(
    kind: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.isatty", lambda fd: fd == 2)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert paint("Stage", kind, fd=2) == expected
    assert paint("Stage", kind, fd=1) == "Stage"


@pytest.mark.parametrize(("environment", "value"), [("NO_COLOR", ""), ("TERM", "dumb")])
def test_paint_respects_plain_output_controls(
    environment: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.isatty", lambda _fd: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv(environment, value)

    assert paint("Stage", "stage") == "Stage"


def _result(state_root: Path) -> dict[str, object]:
    directory = state_root / "demo"
    return {
        "name": "demo",
        "mode": "native",
        "status": "verified",
        "instance_id": "11111111-1111-4111-8111-111111111111",
        "features": "Attic only",
        "endpoint": "http://127.0.0.1:8000",
        "steps": {"preflight": "complete"},
        "state": str(directory / "state.json"),
        "log": str(directory / "commands.log"),
        "credential_file": str(directory / "instance" / "credentials" / "admin.token"),
        "note": "Recorded result; status does not probe the live service.",
    }


def test_install_default_prints_a_concise_operational_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import workflow

    state_root = tmp_path / "state"
    result = _result(state_root)
    monkeypatch.setattr(workflow, "run_install", lambda _ctx: result)

    exit_code = cli.main(
        [
            "--non-interactive",
            "--mode",
            "native",
            "--name",
            "demo",
            "--state-root",
            str(state_root),
        ],
        default_source=_source_tree(tmp_path),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Installation complete" in output
    assert "Endpoint: http://127.0.0.1:8000" in output
    assert f"State: {result['state']}" in output
    assert "Log:" not in output
    assert f"Credential: {result['credential_file']}" in output
    assert '"instance_id"' not in output
    assert '"steps"' not in output


def test_verbose_prints_the_full_result_and_reaches_the_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import workflow

    state_root = tmp_path / "state"
    result = _result(state_root)
    seen: list[bool] = []

    def run_install(ctx: object) -> dict[str, object]:
        seen.append(ctx.verbose)  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr(workflow, "run_install", run_install)

    exit_code = cli.main(
        [
            "--non-interactive",
            "--mode",
            "native",
            "--name",
            "demo",
            "--state-root",
            str(state_root),
            "--verbose",
        ],
        default_source=_source_tree(tmp_path),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert seen == [True]
    assert '"instance_id": "11111111-1111-4111-8111-111111111111"' in output
    assert '"steps": {' in output


def test_status_keeps_full_json_without_verbose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import workflow

    source = _source_tree(tmp_path)
    state_root = tmp_path / "state"
    result = _result(state_root)
    monkeypatch.setattr(workflow, "run_install", lambda _ctx: result)
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
    capsys.readouterr()
    monkeypatch.setattr(workflow, "status_install", lambda _ctx: result)

    assert cli.main(["status", "--name", "demo", "--state-root", str(state_root)]) == 0

    output = capsys.readouterr().out
    document = json.loads(output[output.index("{") :])
    assert document == result


@pytest.mark.parametrize("operation", ["status", "resume", "rollback"])
def test_verbose_reaches_every_reopened_context(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import workflow

    source = _source_tree(tmp_path)
    state_root = tmp_path / "state"
    result = _result(state_root)
    monkeypatch.setattr(workflow, "run_install", lambda _ctx: result)
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
    capsys.readouterr()
    seen: list[bool] = []

    def inspect(ctx: object) -> dict[str, object]:
        seen.append(ctx.verbose)  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr(workflow, "run_install", inspect)
    monkeypatch.setattr(workflow, "status_install", inspect)
    monkeypatch.setattr(workflow, "rollback_install", inspect)

    assert (
        cli.main(
            [
                operation,
                "--name",
                "demo",
                "--state-root",
                str(state_root),
                "--verbose",
            ]
        )
        == 0
    )
    assert seen == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_transcript_footer_is_last_and_file_notices_stay_in_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail: bool,
) -> None:
    from cairn_install import workflow
    from cairn_install.core import Context, InstallError

    root = tmp_path / "state"
    result = _result(root)

    def run(ctx: Context) -> dict[str, object]:
        ctx.write_file(ctx.root / "example.yaml", "example: content\n")
        if fail:
            raise InstallError("deliberate check failure")
        return result

    monkeypatch.setattr(workflow, "run_install", run)
    code = cli.main(
        [
            "--non-interactive",
            "--mode",
            "native",
            "--name",
            "demo",
            "--state-root",
            str(root),
        ],
        default_source=_source_tree(tmp_path),
    )
    captured = capsys.readouterr()
    assert code == (2 if fail else 0)
    transcript = root / "demo" / "commands.log"
    footer_stream = captured.err if fail else captured.out
    assert footer_stream.rstrip().endswith(f"Transcript: {transcript}")
    assert (captured.out + captured.err).count("Transcript:") == 1
    assert "example.yaml" not in captured.out
    assert "example: content" in transcript.read_text()


def test_ls_missing_root_does_not_create_state_or_transcript(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "missing"
    assert cli.main(["ls", "--state-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "No recorded installations" in output
    assert "Transcript:" not in output
    assert not root.exists()


def test_ls_displays_recorded_instance_without_rewriting_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install.core import open_context

    root = tmp_path / "state"
    with open_context(
        root,
        "notes",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8123,
            "semantic": False,
        },
    ) as ctx:
        state = ctx.directory / "state.json"
    before = (state.read_bytes(), state.stat().st_mtime_ns)
    assert cli.main(["ls", "--state-root", str(root)]) == 0
    output = capsys.readouterr().out
    for expected in ("notes", "native", "8123", "Attic only", "RECORDED STATUS"):
        assert expected in output
    assert "Transcript:" not in output
    assert (state.read_bytes(), state.stat().st_mtime_ns) == before
    assert not (root / "notes" / "commands.log").exists()
    assert cli.main(["ls", "--state-root", str(root), "--verbose"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "notes"


def test_unexpected_failure_sends_transcript_footer_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import workflow
    from cairn_install.core import Context

    root = tmp_path / "state"

    def fail(ctx: Context) -> None:
        ctx.note("Retained diagnostics", detail=True)
        raise ValueError("unexpected failure")

    monkeypatch.setattr(workflow, "run_install", fail)
    with pytest.raises(ValueError, match="unexpected failure"):
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--state-root",
                str(root),
            ],
            default_source=_source_tree(tmp_path),
        )
    captured = capsys.readouterr()
    assert "Transcript:" not in captured.out
    assert captured.err.rstrip().endswith(
        f"Transcript: {root / 'demo' / 'commands.log'}"
    )
