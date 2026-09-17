"""The built wheel carries the exact reviewed host packages, not local guesses."""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_both_reviewed_host_workflows(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repository,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, "wheel build failed"
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        for provider in ("codex", "claude"):
            member = f"cairn/host_workflows/{provider}/cairn-memory/SKILL.md"
            assert member in wheel.namelist(), (
                "reviewed host workflow missing from wheel"
            )
            original = (
                repository / "integrations" / provider / "cairn-memory" / "SKILL.md"
            )
            assert (
                hashlib.sha256(wheel.read(member)).digest()
                == hashlib.sha256(original.read_bytes()).digest()
            )
        unpacked = tmp_path / "installed-wheel"
        wheel.extractall(unpacked)
    for provider, directory in (("codex", ".agents"), ("claude", ".claude")):
        destination = tmp_path / provider
        destination.mkdir()
        command = [
            sys.executable,
            "-m",
            "cairn.client.host_installation",
            "--provider",
            provider,
            "--destination",
            str(destination),
        ]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(unpacked),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        preview = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert preview.returncode == 0, "packaged installer could not locate its assets"
        assert json.loads(preview.stdout)["state"] == "preview"
        assert tuple(destination.iterdir()) == ()
        installed = subprocess.run(
            [*command, "--apply"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert installed.returncode == 0
        assert json.loads(installed.stdout)["state"] == "installed"
        assert (
            destination / directory / "skills" / "cairn-memory" / "SKILL.md"
        ).read_bytes() == (
            repository / "integrations" / provider / "cairn-memory" / "SKILL.md"
        ).read_bytes()

    # Install the actual console script, not an editable checkout entrypoint.
    # Dependencies remain the test interpreter's locked environment; this is
    # package/entrypoint evidence, not a clean-machine dependency-install test.
    target = tmp_path / "console-install"
    installation = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            "--no-deps",
            "--no-index",
            str(wheels[0]),
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert installation.returncode == 0, "wheel console installation failed"
    console = target / "bin" / "cairn-memory"
    assert console.is_file()
    console_environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(target),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cairn.client.cli; print(cairn.client.cli.__file__)",
        ],
        cwd=tmp_path,
        env=console_environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert imported.returncode == 0
    assert Path(imported.stdout.decode().strip()) == target / "cairn/client/cli.py"
    commands = (
        "check",
        "arrive",
        "recall",
        "acknowledge-visit",
        "remember",
        "status",
        "resume",
        "abandon",
        "history",
        "correct",
        "suggest",
        "propose",
        "proposal-list",
        "proposal-read",
        "proposal-accept",
        "proposal-reject",
    )
    for arguments in (("--help",), *((command, "--help") for command in commands)):
        help_result = subprocess.run(
            [str(console), *arguments],
            cwd=tmp_path,
            env=console_environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert help_result.returncode == 0, "installed command help failed"
        assert b"usage:" in help_result.stdout
