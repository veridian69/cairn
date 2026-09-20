"""Small, dependency-free terminal styling for installer output."""

from __future__ import annotations

import os

_STYLES = {
    "stage": "\033[1;36m",
    "command": "\033[2m",
    "success": "\033[1;32m",
    "warning": "\033[33m",
    "error": "\033[1;31m",
}
_RESET = "\033[0m"


def paint(text: str, kind: str, *, fd: int = 1) -> str:
    """Return restrained ANSI styling when the selected stream is a terminal."""
    style = _STYLES.get(kind)
    if (
        style is None
        or not os.isatty(fd)
        or "NO_COLOR" in os.environ
        or os.environ.get("TERM") == "dumb"
    ):
        return text
    return f"{style}{text}{_RESET}"


def features_label(semantic: bool, garden: bool) -> str:
    """One operator-facing feature summary shared by status, listing and summaries."""
    label = "Attic plus semantic search" if semantic else "Attic only"
    if garden:
        label += "; Garden"
    return label
