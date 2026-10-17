"""The currency check sees the self-built Redis and FalkorDB pins fall behind."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPOSITORY_ROOT / "scripts/check-runtime-currency"
# GitHub API responses recorded on 17 October 2026, trimmed to the fields read.
FIXTURES = REPOSITORY_ROOT / "tests/fixtures/runtime_currency"
REDIS_RELEASES = "repos_redis_redis_releases_per_page_100.json"


def _checker() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("runtime_currency", str(CHECKER))
    spec = importlib.util.spec_from_loader("runtime_currency", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _check(
    build_script: Path, fixtures: Path = FIXTURES
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--build-script",
            str(build_script),
            "--fixtures",
            str(fixtures),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _pins(tmp_path: Path, falkor_commit: str, redis_version: str) -> Path:
    build = tmp_path / "build.sh"
    build.write_text(f"falkor_commit={falkor_commit}\nredis_version={redis_version}\n")
    return build


def test_v0_7_14_pins_are_behind_patch_with_redis_security_fixes(
    tmp_path: Path,
) -> None:
    result = _check(
        _pins(tmp_path, "5ac6db8059013c9d74842c02b6a9f1a4858a6a1b", "8.6.3")
    )
    assert result.returncode == 1, result.stderr
    redis, falkordb = result.stdout.splitlines()
    assert redis.startswith("redis 8.6.3: behind-patch; 8.6.7 available;")
    assert "SECURITY in 8.6.5, 8.6.6, 8.6.7" in redis
    assert falkordb.startswith("falkordb v4.20.4: behind-patch; v4.20.7 available;")


def test_issue_118_pins_are_current() -> None:
    result = _check(REPOSITORY_ROOT / "deploy/falkordb/build.sh")
    assert result.returncode == 0, result.stderr
    redis, falkordb = result.stdout.splitlines()
    assert redis == "redis 8.10.2: current"
    assert falkordb.startswith("falkordb v4.20.7: newer-line-available;")
    assert "newer lines (information only): v4.22.1, v6.0.2" in falkordb


def test_untagged_falkordb_commit_fails_the_check(tmp_path: Path) -> None:
    result = _check(_pins(tmp_path, "0" * 40, "8.10.2"))
    assert result.returncode == 2
    assert "is not exactly one release tag" in result.stderr


def test_missing_pin_fails_the_check(tmp_path: Path) -> None:
    build = tmp_path / "build.sh"
    build.write_text("redis_version=8.10.2\n")
    result = _check(build)
    assert result.returncode == 2
    assert "pin missing" in result.stderr


def test_unhashed_security_release_is_still_reported(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    releases = json.loads((fixtures / REDIS_RELEASES).read_text())
    releases.insert(
        0,
        {
            "tag_name": "8.10.3",
            "draft": False,
            "prerelease": False,
            "body": "Upgrade urgency SECURITY: see notes.",
        },
    )
    (fixtures / REDIS_RELEASES).write_text(json.dumps(releases))
    result = _check(REPOSITORY_ROOT / "deploy/falkordb/build.sh", fixtures)
    redis = result.stdout.splitlines()[0]
    assert redis == (
        "redis 8.10.2: current; not yet in redis-hashes: 8.10.3; SECURITY in 8.10.3"
    )


def test_unexpected_failure_exits_2_not_behind_patch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _checker()

    def stalled(path: str, fixtures: Path | None) -> None:
        raise subprocess.TimeoutExpired(["gh", "api", path], 60)

    monkeypatch.setattr(checker, "fetch", stalled)
    assert checker.main([]) == 2
    assert "TimeoutExpired" in capsys.readouterr().err


def test_unauthenticated_gh_falls_back_to_public_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _checker()
    requested = []

    def unauthenticated(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 4, "", "gh auth login required")

    def public(request: object, timeout: int) -> io.BytesIO:
        requested.append(getattr(request, "full_url", ""))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(checker.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(checker.subprocess, "run", unauthenticated)
    monkeypatch.setattr(checker.urllib.request, "urlopen", public)
    assert checker.fetch("repos/redis/redis/releases", None) == {"ok": True}
    assert requested == ["https://api.github.com/repos/redis/redis/releases"]


def _fake_docker(tmp_path: Path, labels: dict[str, str]) -> dict[str, str]:
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(f"#!/bin/sh\nprintf '%s\\n' '{json.dumps(labels)}'\n")
    docker.chmod(0o755)
    return {"PATH": f"{docker.parent}:/usr/bin:/bin"}


def _check_container(
    tmp_path: Path, labels: dict[str, str], *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--container",
            "cairn-v1-falkordb-1",
            "--fixtures",
            str(FIXTURES),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_fake_docker(tmp_path, labels),
    )


def test_running_container_labels_supply_the_pins(tmp_path: Path) -> None:
    labels = {
        "org.opencontainers.image.revision": "5ac6db8059013c9d74842c02b6a9f1a4858a6a1b",
        "org.opencontainers.image.version": "redis-8.6.3_falkordb-4.20.4",
    }
    status = tmp_path / "status.json"
    result = _check_container(tmp_path, labels, "--status", str(status))
    assert result.returncode == 1, result.stderr
    written = json.loads(status.read_text())
    assert written["result"] == "behind-patch"
    assert written["exit"] == 1
    assert written["lines"] == result.stdout.splitlines()
    assert written["lines"][0].startswith("redis 8.6.3: behind-patch;")
    assert written["error"] == ""


def test_unlabelled_container_fails_and_says_so_in_status(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    result = _check_container(tmp_path, {}, "--status", str(status))
    assert result.returncode == 2
    written = json.loads(status.read_text())
    assert written["result"] == "failed"
    assert written["lines"] == []
    assert "image labels carry no Redis and FalkorDB pins" in written["error"]
