import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import workspace  # noqa: E402


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "src"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "uv.lock").write_text("lock\n")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one", cwd=repo)
    rev = git("rev-parse", "HEAD", cwd=repo)
    (repo / "later.txt").write_text("later\n")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two", cwd=repo)
    return repo, rev


def test_bare_and_clone_pin_the_revision(
    tmp_path: Path, source: tuple[Path, str]
) -> None:
    repo, rev = source
    bare = tmp_path / "origin.git"
    workspace.make_bare(repo, rev, bare)
    clone = workspace.clone_for(bare, tmp_path / "val", rev)
    assert git("rev-parse", "HEAD", cwd=clone) == rev
    assert not (clone / "later.txt").exists()
    assert workspace.remotes(clone) == [str(bare)]


def test_bare_refuses_unknown_revision(
    tmp_path: Path, source: tuple[Path, str]
) -> None:
    repo, _ = source
    with pytest.raises(subprocess.CalledProcessError):
        workspace.make_bare(repo, "0" * 40, tmp_path / "origin.git")


def test_pins_are_exact() -> None:
    assert workspace.DEEPDIFF_REV == "79e4379278b1cfdd8e9e3dccac364956908e8989"
    assert workspace.DEEPDIFF_URL == "https://github.com/qlustered/deepdiff"


def test_venv_has_every_extra_and_not_the_project_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> None:
        seen.append(argv)

    monkeypatch.setattr(subprocess, "run", fake_run)
    (tmp_path / "uv.lock").write_text("lock\n")
    workspace.build_venv(tmp_path, tmp_path / "venv")
    argv = seen[0]
    # The suite imports click, pytz, numpy...; and an installed project would shadow
    # each agent's own checkout outside pytest's rootdir.
    assert (
        "--all-extras" in argv and "--no-install-project" in argv and "--locked" in argv
    )
