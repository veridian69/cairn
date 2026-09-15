"""Unsupported Mac features fail before asking for provider credentials."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn_install import cli
from cairn_install.core import open_context


def test_mac_native_semantic_refuses_before_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "platform", SimpleNamespace(system=lambda: "Darwin"), raising=False
    )
    state = tmp_path / "state"
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "native",
                "--semantic",
                "--name",
                "mac",
                "--port",
                "18231",
                "--state-root",
                str(state),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert "macOS native supports Attic only" in capsys.readouterr().err
    assert not state.exists()


def test_mac_native_interactive_features_need_no_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "platform", SimpleNamespace(system=lambda: "Darwin"), raising=False
    )

    def refuse_input(prompt: str) -> str:
        raise AssertionError("Unsupported features should not be offered")

    monkeypatch.setattr("builtins.input", refuse_input)
    assert cli._prompt_semantic("native", False) is False


def test_mac_resume_refuses_semantics_before_reading_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    (source / "src/cairn").mkdir(parents=True)
    (source / "src/cairn/__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    (source / "uv.lock").write_text("version = 1\n")
    state_root = tmp_path / "state"
    with open_context(
        state_root,
        "mac",
        create={
            "mode": "native",
            "port": 18231,
            "semantic": True,
            "source": str(source),
            "source_fingerprint": cli.source_fingerprint(source),
        },
    ):
        pass
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Darwin"))

    def refuse_provider(*args: object) -> None:
        raise AssertionError("Provider preparation must remain uncalled")

    monkeypatch.setattr(cli, "_prepare_provider_key", refuse_provider)
    assert (
        cli.main(
            [
                "resume",
                "--name",
                "mac",
                "--state-root",
                str(state_root),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert "macOS native supports Attic only" in capsys.readouterr().err
