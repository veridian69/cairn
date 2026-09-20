#!/usr/bin/env python3
"""Extract a fenced shell block from a Markdown guide, verbatim, by heading.

Acceptance rehearsals execute the documentation instead of a copy of it, so a
procedure that drifts from its guide fails here rather than in a blind run.
A block is addressed by the heading it sits under and its 1-based index among
the ``sh``/``bash`` blocks of that section. A section ends at the next heading
of equal or higher level. ``--replace OLD NEW`` substitutes a documented
placeholder; every replacement must match, and ``--require-complete`` refuses
a block that still contains ``REPLACE_WITH_`` or ``YOUR_`` placeholders.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")
SHELL = frozenset({"sh", "bash"})
PLACEHOLDER = re.compile(r"REPLACE_WITH_|YOUR_")


@dataclass(frozen=True)
class Block:
    heading: str
    level: int
    index: int
    ordinal: int
    body: str


def shell_blocks(text: str) -> list[Block]:
    """Every sh/bash block with the nearest heading above it."""
    blocks: list[Block] = []
    heading, level, index, ordinal = "", 0, 0, 0
    fence_language: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if fence_language is not None:
            if FENCE.match(line) and line.strip() == "```":
                if fence_language in SHELL:
                    index += 1
                    ordinal += 1
                    blocks.append(
                        Block(heading, level, index, ordinal, "\n".join(body))
                    )
                fence_language, body = None, []
            else:
                body.append(line)
            continue
        if match := FENCE.match(line):
            fence_language = match.group(1)
            continue
        if match := HEADING.match(line):
            level, heading, index = len(match.group(1)), match.group(2), 0
    if fence_language is not None:
        raise ValueError("unterminated code fence")
    return blocks


def section_blocks(text: str, section: str) -> list[Block]:
    """Blocks under ``section`` up to the next heading of equal or higher level."""
    headings = [
        (number, len(match.group(1)))
        for number, line in enumerate(text.splitlines())
        if (match := HEADING.match(line)) and match.group(2) == section
    ]
    if len(headings) != 1:
        raise ValueError(
            f"{len(headings)} headings match {section!r}; expected exactly 1"
        )
    start, level = headings[0]
    lines = text.splitlines()
    end = len(lines)
    fenced = False
    for number in range(start + 1, len(lines)):
        if FENCE.match(lines[number]):
            fenced = not fenced or lines[number].strip() != "```"
            continue
        if fenced:
            continue
        if (match := HEADING.match(lines[number])) and len(match.group(1)) <= level:
            end = number
            break
    scoped = "\n".join(lines[start:end]) + "\n"
    return [
        Block(section, level, b.index, b.ordinal, b.body) for b in shell_blocks(scoped)
    ]


GUARD = re.compile(
    r"(\*(REPLACE_WITH_|YOUR_)\S*\*|!= *['\"]?(REPLACE_WITH_|YOUR_)"
    r"|(REPLACE_WITH_|YOUR_)[A-Z0-9_]*\*?\))"
)


def unreplaced_placeholder(body: str) -> str | None:
    """First line still carrying a placeholder value, ignoring the guards that test for one."""
    for line in body.splitlines():
        if PLACEHOLDER.search(line) and not GUARD.search(line):
            return line.strip()
    return None


def apply_replacements(body: str, replacements: list[list[str]]) -> str:
    for old, new in replacements:
        if not old:
            raise ValueError("replacement OLD must not be empty")
        if old not in body:
            raise ValueError(f"replacement not found in block: {old!r}")
        body = body.replace(old, new)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("guide", type=Path)
    parser.add_argument("--section", help="exact heading text the block sits under")
    parser.add_argument("--index", type=int, default=1, help="1-based block index")
    parser.add_argument(
        "--replace", action="append", default=[], nargs=2, metavar=("OLD", "NEW")
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="refuse a block that still contains REPLACE_WITH_ or YOUR_ placeholders",
    )
    parser.add_argument(
        "--list", action="store_true", help="list heading, index, ordinal"
    )
    args = parser.parse_args(argv)
    try:
        text = args.guide.read_text(encoding="utf-8")
        if args.list:
            for block in shell_blocks(text):
                first = block.body.splitlines()[0] if block.body else ""
                print(f"{block.heading}\t{block.index}\t{block.ordinal}\t{first}")
            return 0
        if args.section is None:
            raise ValueError("--section or --list is required")
        blocks = section_blocks(text, args.section)
        if not 1 <= args.index <= len(blocks):
            raise ValueError(
                f"section {args.section!r} has {len(blocks)} shell block(s); "
                f"index {args.index} is out of range"
            )
        body = apply_replacements(blocks[args.index - 1].body, args.replace)
        if args.require_complete and (line := unreplaced_placeholder(body)):
            raise ValueError(f"placeholder remains: {line}")
    except (OSError, ValueError) as error:
        print(f"doc_blocks: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
