"""Exercise the installer on native temporary files, never host configuration."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.client.host_installation import InstallationError, install_host_workflow


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    for provider in ("codex", "claude"):
        package = root / provider / "cairn-memory"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: cairn-memory\n---\nSynthetic skill\n"
        )
    return root


@pytest.mark.parametrize(
    "provider,folder", [("codex", ".agents"), ("claude", ".claude")]
)
def test_preview_writes_nothing_and_apply_is_idempotent(
    tmp_path: Path, assets: Path, provider: str, folder: str
) -> None:
    destination = tmp_path / "host"
    destination.mkdir()
    preview = install_host_workflow(provider, destination, assets_root=assets)
    assert preview.state == "preview"
    assert list(destination.iterdir()) == []
    installed = install_host_workflow(
        provider, destination, assets_root=assets, apply=True
    )
    target = destination / folder / "skills" / "cairn-memory"
    assert installed.path == target
    assert installed.state == "installed"
    assert (target / "SKILL.md").read_bytes() == (
        assets / provider / "cairn-memory" / "SKILL.md"
    ).read_bytes()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()}
    again = install_host_workflow(provider, destination, assets_root=assets, apply=True)
    assert again.state == "unchanged"
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()
    }


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    "collision", ["directory", "file", "symlink", "modified", "extra", "manifest"]
)
def test_refuses_collisions_without_altering_them(
    tmp_path: Path, assets: Path, collision: str, apply: bool
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    target = host / ".agents" / "skills" / "cairn-memory"
    target.parent.mkdir(parents=True)
    if collision == "file":
        target.write_text("unrelated")
    elif collision == "symlink":
        target.symlink_to(assets, target_is_directory=True)
    elif collision == "directory":
        target.mkdir()
    else:
        install_host_workflow("codex", host, assets_root=assets, apply=True)
        if collision == "modified":
            (target / "SKILL.md").write_text("user edit")
        elif collision == "extra":
            (target / "personal.txt").write_text("keep")
        else:
            (target / ".cairn-install.json").write_text(json.dumps({"owner": "other"}))
    with pytest.raises(InstallationError):
        install_host_workflow("codex", host, assets_root=assets, apply=apply)
    assert target.exists()
    if collision == "modified":
        assert (target / "SKILL.md").read_text() == "user edit"
    if collision == "extra":
        assert (target / "personal.txt").read_text() == "keep"


@pytest.mark.parametrize("part", ["host", ".agents", "skills"])
def test_rejects_symlink_in_destination_chain(
    tmp_path: Path, assets: Path, part: str
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    host = tmp_path / "host"
    if part == "host":
        host.symlink_to(real, target_is_directory=True)
    else:
        host.mkdir()
        link = host / ".agents"
        if part == "skills":
            link.mkdir()
            link /= "skills"
        link.symlink_to(real, target_is_directory=True)
    with pytest.raises(InstallationError):
        install_host_workflow("codex", host, assets_root=assets, apply=True)
    assert list(real.iterdir()) == []


@pytest.mark.parametrize(
    "path",
    ["relative", "/", "/tmp", "/home", "/etc", "/usr", "/mnt/c/host", "/tmp/../host"],
)
def test_rejects_unsafe_destination(assets: Path, path: str) -> None:
    with pytest.raises(InstallationError):
        install_host_workflow("codex", Path(path), assets_root=assets)


@pytest.mark.parametrize("bad", ["symlink", "oversized", "invalid-utf8", "directory"])
def test_rejects_unsafe_asset(tmp_path: Path, assets: Path, bad: str) -> None:
    host = tmp_path / "host"
    host.mkdir()
    source = assets / "codex" / "cairn-memory" / "SKILL.md"
    source.unlink()
    if bad == "symlink":
        source.symlink_to(assets / "claude" / "cairn-memory" / "SKILL.md")
    elif bad == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"x" * 65537 if bad == "oversized" else b"\xff")
    with pytest.raises(InstallationError):
        install_host_workflow("codex", host, assets_root=assets, apply=True)
    assert list(host.iterdir()) == []


def test_unknown_provider_is_not_a_path(tmp_path: Path, assets: Path) -> None:
    with pytest.raises(InstallationError):
        install_host_workflow("../claude", tmp_path, assets_root=assets, apply=True)


def test_install_repository_assets(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "integrations"
    for provider in ("codex", "claude"):
        result = install_host_workflow(provider, tmp_path, assets_root=root, apply=True)
        assert (result.path / "SKILL.md").stat().st_size > 0


def test_standalone_command_requires_explicit_apply(
    tmp_path: Path, assets: Path
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    command = [
        sys.executable,
        "-m",
        "cairn.client.host_installation",
        "--provider",
        "codex",
        "--destination",
        str(host),
        "--assets-root",
        str(assets),
    ]
    preview = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(preview.stdout)["state"] == "preview"
    assert preview.stderr == ""
    assert list(host.iterdir()) == []
    applied = subprocess.run(
        command + ["--apply"], capture_output=True, text=True, check=True
    )
    assert json.loads(applied.stdout)["state"] == "installed"
    assert applied.stderr == ""
    (host / ".agents" / "skills" / "cairn-memory" / "SKILL.md").write_text(
        "do-not-leak-content"
    )
    collision = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert collision.returncode == 2
    assert json.loads(collision.stdout) == {"error": "installation_collision"}
    assert collision.stderr == ""


def test_source_directory_symlink_is_refused(tmp_path: Path, assets: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(assets, target_is_directory=True)
    host = tmp_path / "host"
    host.mkdir()
    with pytest.raises(InstallationError):
        install_host_workflow("codex", host, assets_root=alias, apply=True)
    assert list(host.iterdir()) == []


def test_oversized_owned_manifest_is_refused(tmp_path: Path, assets: Path) -> None:
    result = install_host_workflow("codex", tmp_path, assets_root=assets, apply=True)
    manifest = result.path / ".cairn-install.json"
    manifest.write_bytes(b"x" * 65537)
    with pytest.raises(InstallationError):
        install_host_workflow("codex", tmp_path, assets_root=assets, apply=True)
    assert manifest.stat().st_size == 65537
