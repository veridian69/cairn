"""Exact server-side backup-child liveness evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "backup_child.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "kind_acceptance_backup_child", SCRIPT
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "backup-child.json"
    marker.write_text(
        json.dumps(
            {
                "run_id": "run-17",
                "revision": "a" * 40,
                "pid": 41,
                "proc_start_ticks": "9876",
                "command": [
                    "cairn",
                    "backup",
                    "--config",
                    "/etc/cairn/config.yaml",
                    "--output",
                    "/var/lib/cairn/backups",
                ],
            }
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)
    process = tmp_path / "proc" / "41"
    process.mkdir(parents=True)
    (process / "stat").write_text(
        "41 (cairn worker) S " + "0 " * 18 + "9876 0 0\n", encoding="utf-8"
    )
    (process / "cmdline").write_bytes(
        b"/usr/bin/python\0/usr/local/bin/cairn\0backup\0--config\0"
        b"/etc/cairn/config.yaml\0--output\0/var/lib/cairn/backups\0"
    )
    return marker, tmp_path / "proc"


def test_exact_marked_child_alive_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _load()
    marker, proc_root = _fixture(tmp_path)
    monkeypatch.setattr(child.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(child.time, "time_ns", lambda: 1_700)

    evidence = child.require_exact_child_alive(
        marker, "run-17", "a" * 40, proc_root=proc_root
    )

    assert evidence == {
        "acceptance_event": "backup-child-alive",
        "pid": 41,
        "proc_start_ticks": "9876",
        "timestamp_ns": 1_700,
    }


def test_wrapper_alive_but_exact_child_dead_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _load()
    marker, proc_root = _fixture(tmp_path)

    def dead(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(child.os, "kill", dead)
    with pytest.raises(
        child.ChildEvidenceError, match="exact backup child is not alive"
    ):
        child.require_exact_child_alive(marker, "run-17", "a" * 40, proc_root=proc_root)


def test_zombie_exact_child_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _load()
    marker, proc_root = _fixture(tmp_path)
    (proc_root / "41" / "stat").write_text(
        "41 (cairn worker) Z " + "0 " * 18 + "9876 0 0\n", encoding="utf-8"
    )
    monkeypatch.setattr(child.os, "kill", lambda pid, signal: None)

    with pytest.raises(child.ChildEvidenceError, match="zombie"):
        child.require_exact_child_alive(marker, "run-17", "a" * 40, proc_root=proc_root)


def test_child_dying_during_final_observation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _load()
    marker, proc_root = _fixture(tmp_path)
    identities = iter([("S", "9876"), ("Z", "9876")])
    monkeypatch.setattr(child, "_process_identity", lambda _path: next(identities))
    monkeypatch.setattr(child.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(child.time, "time_ns", lambda: 1_700)

    with pytest.raises(child.ChildEvidenceError, match="died during observation"):
        child.require_exact_child_alive(marker, "run-17", "a" * 40, proc_root=proc_root)
