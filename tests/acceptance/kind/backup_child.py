"""Verify that the exact marked server-side backup child is still alive."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any


class ChildEvidenceError(RuntimeError):
    """The target-local marker does not identify a live exact child."""


EXPECTED_COMMAND = [
    "cairn",
    "backup",
    "--config",
    "/etc/cairn/config.yaml",
    "--output",
    "/var/lib/cairn/backups",
]


def _read_marker(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_size > 4096
        ):
            raise ChildEvidenceError("backup child marker ownership or mode is unsafe")
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ChildEvidenceError("backup child marker is not an object")
    return payload


def _process_identity(stat_path: Path) -> tuple[str, str]:
    try:
        suffix = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return suffix[0], suffix[19]
    except (FileNotFoundError, IndexError) as error:
        raise ChildEvidenceError("exact backup child is not alive") from error


def require_exact_child_alive(
    marker_path: Path,
    expected_run_id: str,
    expected_revision: str,
    *,
    proc_root: Path = Path("/proc"),
) -> dict[str, int | str]:
    marker = _read_marker(marker_path)
    if (
        marker.get("run_id") != expected_run_id
        or marker.get("revision") != expected_revision
    ):
        raise ChildEvidenceError("backup child marker has the wrong run identity")
    pid = marker.get("pid")
    expected_ticks = marker.get("proc_start_ticks")
    expected_command = marker.get("command")
    if (
        not isinstance(pid, int)
        or pid <= 1
        or not isinstance(expected_ticks, str)
        or expected_command != EXPECTED_COMMAND
    ):
        raise ChildEvidenceError("backup child marker is malformed")

    process = proc_root / str(pid)
    state, start_ticks = _process_identity(process / "stat")
    if state == "Z":
        raise ChildEvidenceError("exact backup child is a zombie")
    if start_ticks != expected_ticks:
        raise ChildEvidenceError("backup child PID was reused")
    try:
        os.kill(pid, 0)
        command = [
            argument.decode("utf-8")
            for argument in (process / "cmdline").read_bytes().split(b"\0")
            if argument
        ]
    except (ProcessLookupError, FileNotFoundError, UnicodeDecodeError) as error:
        raise ChildEvidenceError("exact backup child is not alive") from error
    candidate = command[-len(expected_command) :]
    if (
        len(candidate) != len(expected_command)
        or Path(candidate[0]).name != expected_command[0]
        or candidate[1:] != expected_command[1:]
    ):
        raise ChildEvidenceError("live PID is not the exact marked backup child")
    # This is a conservative upper bound: the final identity/state read occurs
    # after the timestamp. A race may therefore refuse valid evidence, but it
    # cannot claim the child was alive after a mutation when it was already dead.
    observed_ns = time.time_ns()
    final_state, final_start_ticks = _process_identity(process / "stat")
    if final_state == "Z" or final_start_ticks != expected_ticks:
        raise ChildEvidenceError("exact backup child died during observation")
    return {
        "acceptance_event": "backup-child-alive",
        "pid": pid,
        "proc_start_ticks": expected_ticks,
        "timestamp_ns": observed_ns,
    }


def main(argv: list[str]) -> int:
    try:
        evidence = require_exact_child_alive(Path(argv[1]), argv[2], argv[3])
    except (ChildEvidenceError, IndexError, json.JSONDecodeError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
