"""The source recipe pins inputs and refuses dirty source before Docker."""

import os
import subprocess
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[2] / "deploy/falkordb"


def test_source_recipe_has_offline_compiles_and_runtime_dependency_check() -> None:
    dockerfile = (RECIPE / "Dockerfile.source").read_text()

    assert dockerfile.count("RUN --network=none") >= 2
    assert "FETCHCONTENT_SOURCE_DIR_CPU_FEATURES" in dockerfile
    assert "set(CXX_AVX OFF CACHE BOOL" in dockerfile
    assert "set(CXX_AVX512F OFF CACHE BOOL" in dockerfile
    assert "grep -Fx 'CXX_AVX:BOOL=OFF'" in dockerfile
    assert (
        "! grep -R --include=flags.make -- '-mavx' bin/linux-x64-release" in dockerfile
    )
    assert 'grep -F "not found"' in dockerfile
    assert "apt-get upgrade -y" in dockerfile
    assert "FALKORDB_DATA_PATH=/var/lib/falkordb/data" in dockerfile
    assert "/usr/share/licenses/cairn-falkordb" in dockerfile
    assert 'cd "/build/redis-${REDIS_VERSION}"' in dockerfile
    assert "ldd /usr/local/bin/redis-cli" in dockerfile
    assert 'ENTRYPOINT ["/var/lib/falkordb/bin/run.sh"]' in dockerfile


def test_invalid_job_limit_refuses_before_external_commands(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "docker-called"
    for name in ("docker", "git", "curl"):
        fake = binary / name
        fake.write_text('#!/bin/sh\nprintf called > "$COMMAND_MARKER"\nexit 99\n')
        fake.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "COMMAND_MARKER": str(marker),
        "CAIRN_BUILD_JOBS": "11",
    }

    result = subprocess.run(
        ["bash", str(RECIPE / "build.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "CAIRN_BUILD_JOBS must be an integer from 1 to 10" in result.stderr
    assert not marker.exists()


def test_build_forces_fresh_apt_layers() -> None:
    assert "docker build --no-cache" in (RECIPE / "build.sh").read_text()


def test_avx_guard_reaches_nested_vecsim_flags(tmp_path: Path) -> None:
    nested = tmp_path / "bin/linux-x64-release/search-static/VecSim/CMakeFiles/x"
    nested.mkdir(parents=True)
    flags = nested / "flags.make"
    flags.write_text("CXX_FLAGS = -O3 -mavx\n")
    guard = next(
        line.strip().removesuffix(" && \\")
        for line in (RECIPE / "Dockerfile.source").read_text().splitlines()
        if line.strip().startswith("! grep -R --include=flags.make")
    )
    result = subprocess.run(["bash", "-c", guard], cwd=tmp_path, check=False)
    assert result.returncode != 0
    flags.write_text("CXX_FLAGS = -O3 -msse\n")
    result = subprocess.run(["bash", "-c", guard], cwd=tmp_path, check=False)
    assert result.returncode == 0


def test_git_status_failure_refuses_before_docker(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "docker-called"
    fake_git = binary / "git"
    fake_git.write_text(
        r"""#!/bin/sh
if [ "$1" = clone ]; then eval "target=\${$#}"; mkdir -p "$target"; exit 0; fi
case "$*" in
  *" rev-parse HEAD") printf '%s\n' 5ac6db8059013c9d74842c02b6a9f1a4858a6a1b ;;
  *" submodule status --recursive") exit 0 ;;
  *" status --porcelain --untracked-files=all") exit 42 ;;
  *) exit 0 ;;
esac
"""
    )
    fake_git.chmod(0o755)
    fake_docker = binary / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf called > "$DOCKER_MARKER"\n')
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(RECIPE / "build.sh")],
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DOCKER_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 42
    assert not marker.exists()
