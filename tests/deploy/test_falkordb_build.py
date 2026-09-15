"""The derivative recipe refuses altered inputs before touching Docker."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[2] / "deploy/falkordb"


@pytest.mark.parametrize(
    "input_file", ["Dockerfile.server", "run.sh", "gen-certs.sh", "LICENSE.txt"]
)
def test_changed_vendor_input_refuses_before_docker(
    tmp_path: Path, input_file: str
) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    shutil.copy2(RECIPE / "build.sh", recipe / "build.sh")
    shutil.copytree(RECIPE / "upstream", recipe / "upstream")
    with (recipe / "upstream" / input_file).open("ab") as source:
        source.write(b"\nchanged vendor input\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "docker-called"
    fake_docker = binary / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf called > "$DOCKER_MARKER"\nexit 99\n')
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "DOCKER_MARKER": str(marker),
    }

    result = subprocess.run(
        ["bash", str(recipe / "build.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"{input_file}: FAILED" in result.stdout
    assert not marker.exists()
