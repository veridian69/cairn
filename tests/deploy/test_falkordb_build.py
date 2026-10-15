"""The source recipe pins inputs and refuses dirty source before Docker."""

import argparse
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[2] / "deploy/falkordb"


def test_current_source_pins_and_built_records() -> None:
    build = (RECIPE / "build.sh").read_text()
    dockerfile = (RECIPE / "Dockerfile.source").read_text()
    provenance = json.loads((RECIPE / "provenance.json").read_text())
    commit = "2811bc690c37558b345c0f8491c05424729e6953"
    redis_sha = "b9ffee226b5eecdba98a679260dad764b2a4ebd90dce4ad5ac9e9f3eef9c02b3"
    assert f"falkor_commit={commit}" in build
    assert "redis_version=8.10.2" in build
    assert f"redis_sha={redis_sha}" in build
    assert "cpu_features_commit=438a66e41807cd73e0c403966041b358f5eafc68" in build
    assert "ARG REDIS_VERSION=8.10.2" in dockerfile
    assert f'org.opencontainers.image.revision="{commit}"' in dockerfile
    assert (
        'org.opencontainers.image.version="redis-8.10.2_falkordb-4.20.7"' in dockerfile
    )
    assert provenance["upstream_commit"] == commit
    assert provenance["redis_source_sha256"] == redis_sha
    assert provenance["build_source_state"] == "built_and_accepted"
    assert provenance["module_recompiled"] is True
    for field in ("module_sha256", "image_index", "platform_manifest", "image_config"):
        assert re.fullmatch(r"(sha256:)?[0-9a-f]{64}", provenance[field])
    assert provenance["scanner"]["fixable_high_critical"] == 0
    release = json.loads((RECIPE / "release.json").read_text())
    assert release["local_image"] == "cairn-local/falkordb-server:v4.20.7"
    assert re.fullmatch(r"[0-9a-f]{64}", release["archive_sha256"])
    assert (
        json.loads((RECIPE / "source-release.json").read_text())["artifact_role"]
        == "historical_only"
    )


def test_historical_bundle_refuses_current_provenance_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "historical_source_bundle", RECIPE / "prepare_source_bundle.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("external command called")
    )
    with pytest.raises(module.BundleError, match="immutable earlier release"):
        module.prepare(
            argparse.Namespace(
                distribution_dir=RECIPE,
                falkordb_checkout=tmp_path,
                runtime_image="unused",
            )
        )


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


@pytest.mark.parametrize("jobs", ["0", "11", "08", "18446744073709551617"])
def test_invalid_job_limit_refuses_before_external_commands(
    tmp_path: Path, jobs: str
) -> None:
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
        "CAIRN_BUILD_JOBS": jobs,
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
  *" rev-parse HEAD") printf '%s\n' 2811bc690c37558b345c0f8491c05424729e6953 ;;
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
