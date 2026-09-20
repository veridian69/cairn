"""Fenced shell blocks are extracted from documentation by heading, verbatim."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "doc_blocks.py"

GUIDE = """# Title

Intro block that must not count.

```sh
echo intro
```

## Build the thing

Text.

```sh
set -eu
kube_context='YOUR_CONTEXT'
echo "$kube_context"
```

```text
not shell
```

```bash
echo second
```

### Nested heading

```sh
echo nested
```

## Other section

```sh
echo other
```
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def guide(tmp_path: Path) -> Path:
    path = tmp_path / "guide.md"
    path.write_text(GUIDE)
    return path


def test_extracts_the_nth_shell_block_under_a_heading(guide: Path) -> None:
    first = _run(str(guide), "--section", "Build the thing")
    assert first.returncode == 0, first.stderr
    assert (
        first.stdout == "set -eu\nkube_context='YOUR_CONTEXT'\necho \"$kube_context\"\n"
    )

    second = _run(str(guide), "--section", "Build the thing", "--index", "2")
    assert second.returncode == 0, second.stderr
    assert second.stdout == "echo second\n"


def test_section_scope_ends_at_the_next_heading_of_equal_or_higher_level(
    guide: Path,
) -> None:
    nested = _run(str(guide), "--section", "Build the thing", "--index", "3")
    assert nested.returncode == 0, nested.stderr
    assert nested.stdout == "echo nested\n"

    beyond = _run(str(guide), "--section", "Build the thing", "--index", "4")
    assert beyond.returncode == 2
    assert "3 shell block(s)" in beyond.stderr

    other = _run(str(guide), "--section", "Nested heading")
    assert other.stdout == "echo nested\n"


def test_unknown_or_ambiguous_section_is_refused(guide: Path, tmp_path: Path) -> None:
    missing = _run(str(guide), "--section", "No such heading")
    assert missing.returncode == 2
    assert "No such heading" in missing.stderr

    duplicate = tmp_path / "duplicate.md"
    duplicate.write_text(
        "## Same\n\n```sh\necho a\n```\n\n## Same\n\n```sh\necho b\n```\n"
    )
    ambiguous = _run(str(duplicate), "--section", "Same")
    assert ambiguous.returncode == 2
    assert "2 headings" in ambiguous.stderr


def test_replacements_apply_and_leftover_placeholders_are_refused(
    guide: Path, tmp_path: Path
) -> None:
    replaced = _run(
        str(guide),
        "--section",
        "Build the thing",
        "--replace",
        "'YOUR_CONTEXT'",
        "reference",
    )
    assert replaced.returncode == 0, replaced.stderr
    assert replaced.stdout == 'set -eu\nkube_context=reference\necho "$kube_context"\n'

    leftover = _run(str(guide), "--section", "Build the thing", "--require-complete")
    assert leftover.returncode == 2
    assert "YOUR_CONTEXT" in leftover.stderr

    guarded = tmp_path / "guarded.md"
    guarded.write_text(
        "## Guarded\n\n```sh\n"
        "kube_context='REPLACE_WITH_KUBE_CONTEXT'\n"
        "case \"$kube_context\" in\n  ''|REPLACE_WITH_KUBE_CONTEXT) exit 2 ;;\n  *YOUR_*) exit 2 ;;\nesac\n"
        'test "$url" != REPLACE_WITH_URL || exit 2\n'
        "```\n"
    )
    complete = _run(
        str(guarded),
        "--section",
        "Guarded",
        "--require-complete",
        "--replace",
        "'REPLACE_WITH_KUBE_CONTEXT'",
        "reference",
    )
    assert complete.returncode == 0, complete.stderr
    assert "''|REPLACE_WITH_KUBE_CONTEXT) exit 2 ;;" in complete.stdout
    assert "*YOUR_*) exit 2 ;;" in complete.stdout

    unused = _run(
        str(guide),
        "--section",
        "Other section",
        "--replace",
        "'YOUR_CONTEXT'",
        "reference",
    )
    assert unused.returncode == 2
    assert "replacement not found" in unused.stderr


def test_list_prints_every_shell_block_with_its_heading_and_index(guide: Path) -> None:
    listing = _run(str(guide), "--list")
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.splitlines() == [
        "Title\t1\t1\techo intro",
        "Build the thing\t1\t2\tset -eu",
        "Build the thing\t2\t3\techo second",
        "Nested heading\t1\t4\techo nested",
        "Other section\t1\t5\techo other",
    ]
