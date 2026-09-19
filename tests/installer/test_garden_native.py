from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import cli, native
from cairn_install.core import Context, InstallError, open_context
from cairn_install.garden_native import GardenBackend


@pytest.mark.parametrize("mode", ["native", "docker", "kubernetes"])
def test_macos_refuses_new_garden_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Darwin"))
    state = tmp_path / "state"
    assert (
        cli.main(
            [
                "install",
                "--mode",
                mode,
                "--garden-config",
                str(tmp_path / "garden.json"),
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
    assert (
        "Garden is not supported on macOS; install Garden on Linux."
        in capsys.readouterr().err
    )
    assert not state.exists()


@pytest.mark.parametrize("mode", ["native", "docker", "kubernetes"])
def test_macos_refuses_retained_garden_before_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
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
            "mode": mode,
            "port": 18231,
            "semantic": False,
            "source": str(source),
            "source_fingerprint": cli.source_fingerprint(source),
            "garden": {"options": {"port": 8443}},
        },
    ):
        pass
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(
        "cairn_install.workflow.run_install",
        lambda ctx: pytest.fail("workflow must not run"),
    )
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
    assert (
        "Garden is not supported on macOS; install Garden on Linux."
        in capsys.readouterr().err
    )


def context(tmp_path: Path) -> Context:
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
            "garden": {"options": {"port": 8443}},
        },
    )


def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    units = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: units)
    commands: list[list[str]] = []

    def command(self: Context, argv: list[str], **kwargs: Any) -> str:
        commands.append(argv)
        if "FragmentPath" in argv:
            path = units / argv[-1]
            return str(path) if path.exists() else ""
        if "ActiveState" in argv:
            return "inactive"
        return ""

    monkeypatch.setattr(Context, "command", command)
    return commands


def test_garden_unit_uses_separate_receipts_and_preserves_data_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        original = {"service": "cairn-install-demo.service", "status": "enabled"}
        ctx.state["resources"]["native_unit"] = original.copy()
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        data = ctx.root / "garden" / "data" / "retained"
        ctx.write_file(data, "message history\n")
        backend.garden_start()
        unit = tmp_path / "units" / "cairn-install-demo-garden.service"
        assert "PartOf=cairn-install-demo.service" in unit.read_text()
        assert " host --config " in unit.read_text()
        backend.rollback()
        assert not unit.exists()
        assert data.read_text() == "message history\n"
        assert ctx.state["resources"]["native_unit"] == original
        assert (
            ctx.state["resources"]["garden_native"]["native_unit"]["status"]
            == "removed"
        )
        assert not any(argv[-1] == "cairn-install-demo.service" for argv in commands)


def test_changed_garden_unit_blocks_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        backend.garden_start()
        unit = tmp_path / "units" / "cairn-install-demo-garden.service"
        unit.write_text("foreign replacement\n")
        with pytest.raises(InstallError, match="changed"):
            backend.blitz()
        assert unit.exists()


def test_changed_garden_executable_blocks_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        backend.binary.write_text("replacement\n")
        with pytest.raises(InstallError, match="executable.*changed"):
            backend.garden_start()
        assert commands == []


def test_foreign_cairn_unit_blocks_blitz_before_garden_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install.workflow import blitz_install

    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        backend.garden_start()
        cairn_unit = tmp_path / "units" / "cairn-install-demo.service"
        ctx.write_file(cairn_unit, "owned Cairn unit\n", mode=0o644)
        ctx.state["resources"]["native_unit"] = {
            "path": str(cairn_unit),
            "service": cairn_unit.name,
            "status": "enabled",
        }
        cairn_unit.write_text("foreign replacement\n")
        commands.clear()
        with pytest.raises(InstallError, match="changed"):
            blitz_install(ctx)
        assert (tmp_path / "units" / "cairn-install-demo-garden.service").exists()
        assert not any("stop" in argv or "disable" in argv for argv in commands)
