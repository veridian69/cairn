"""Every file the wheel force-includes reaches the Cairn image build."""

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ignored(path: str, rules: list[str]) -> bool:
    """Docker semantics: the last matching rule wins; '!' re-includes."""
    ignored = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        pattern = pattern.rstrip("/")
        parts = path.split("/")
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
            ignored = not negate
    return ignored


def test_force_included_files_are_copied_before_the_project_sync() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    rules = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    sync = lines.index("RUN uv sync --frozen --no-dev --no-editable")
    copied = [
        word
        for line in lines[:sync]
        if line.startswith("COPY ") and "--from=" not in line
        for word in line.split()[1:-1]
    ]
    assert sources
    for source in sources:
        assert not _ignored(source, rules), f"{source} is outside the build context"
        assert any(
            source == item or source.startswith(item.rstrip("/") + "/")
            for item in copied
        ), f"{source} is not copied before the project sync"


def test_dockerignore_matcher_follows_last_rule() -> None:
    rules = ["scripts/", "!scripts/backup/a.py"]
    assert _ignored("scripts/backup/b.py", rules)
    assert not _ignored("scripts/backup/a.py", rules)
