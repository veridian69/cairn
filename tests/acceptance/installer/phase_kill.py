#!/usr/bin/env python3
"""Interrupt an upgrade only after its journal reaches the requested phase."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def phase_matches(journal: object, phase: str, old_id: str) -> bool:
    if not isinstance(journal, dict):
        return False
    return (
        journal.get("id") not in (None, old_id)
        and journal.get("direction") == "upgrade"
        and journal.get("phase") == phase
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("phase")
    parser.add_argument("old_id")
    parser.add_argument("pid", type=int)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    if args.pid <= 0:
        parser.error("PID must be positive")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error("--timeout must be finite and nonnegative")
    return args


def _journal(path: Path) -> dict[str, Any]:
    try:
        garden = json.loads(path.read_text()).get("garden") or {}
        upgrade = garden.get("upgrade") or {}
        return upgrade if isinstance(upgrade, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    deadline = time.monotonic() + args.timeout
    seen: object = None
    while True:
        journal = _journal(args.state)
        seen = journal.get("phase")
        if phase_matches(journal, args.phase, args.old_id):
            try:
                os.kill(args.pid, signal.SIGTERM)
            except ProcessLookupError:
                print("exited")
                return 1
            print("killed")
            return 0
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            print(f"exited (last {seen})")
            return 1
        if time.monotonic() >= deadline:
            print(f"timeout (last {seen})")
            return 1
        time.sleep(min(0.002, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
