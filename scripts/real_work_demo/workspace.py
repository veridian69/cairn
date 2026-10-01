"""Pinned target repository, the local bare origin and per-actor clones."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

DEEPDIFF_URL = "https://github.com/qlustered/deepdiff"
DEEPDIFF_REV = "79e4379278b1cfdd8e9e3dccac364956908e8989"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
    ).strip()


def make_bare(source: Path, rev: str, bare: Path) -> None:
    """Create the only remote agents may use, with `main` at exactly `rev`."""
    _git("cat-file", "-e", f"{rev}^{{commit}}", cwd=source)
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("push", "-q", str(bare), f"{rev}:refs/heads/main", cwd=source)


def clone_for(bare: Path, dest: Path, rev: str) -> Path:
    _git("clone", "-q", str(bare), str(dest))
    _git("checkout", "-q", "-B", "main", rev, cwd=dest)
    return dest


def remotes(checkout: Path) -> list[str]:
    urls = _git("remote", "-v", cwd=checkout).splitlines()
    return sorted({line.split()[1] for line in urls})


def build_venv(checkout: Path, venv: Path) -> str:
    """Build deepdiff's locked test venv; return the uv.lock sha256."""
    subprocess.run(
        # Every extra: the suite imports click, pytz, numpy and more. Not the project:
        # an installed deepdiff would shadow each agent's own checkout.
        ["uv", "sync", "--locked", "--all-extras", "--no-install-project"],
        cwd=checkout,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
        check=True,
    )
    return hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()
