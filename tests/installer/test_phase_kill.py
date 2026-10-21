from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "acceptance/installer/phase_kill.py"
_spec = importlib.util.spec_from_file_location("phase_kill", _path)
assert _spec is not None and _spec.loader is not None
phase_kill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phase_kill)


@pytest.mark.parametrize(
    "journal",
    [
        {"id": "new", "direction": "upgrade", "phase": "copied"},
        {"id": "new", "direction": "upgrade", "phase": "copied", "other": 1},
    ],
)
def test_phase_match_accepts_new_upgrade_at_phase(journal: dict[str, str]) -> None:
    assert phase_kill.phase_matches(journal, "copied", "old")


@pytest.mark.parametrize(
    "journal",
    [
        None,
        [],
        {"id": None, "direction": "upgrade", "phase": "copied"},
        {"id": "old", "direction": "upgrade", "phase": "copied"},
        {"id": "new", "direction": "rollback", "phase": "copied"},
        {"id": "new", "direction": "upgrade", "phase": "started"},
    ],
)
def test_phase_match_rejects_negative_controls(journal: object) -> None:
    assert not phase_kill.phase_matches(journal, "copied", "old")


@pytest.mark.parametrize("value", ["-1", "inf", "nan"])
def test_parser_rejects_invalid_timeout(value: str) -> None:
    with pytest.raises(SystemExit):
        phase_kill.parse_args(["state", "copied", "old", "123", "--timeout", value])


def test_parser_rejects_nonpositive_pid() -> None:
    with pytest.raises(SystemExit):
        phase_kill.parse_args(["state", "copied", "old", "0"])


def test_parser_accepts_zero_timeout() -> None:
    assert (
        phase_kill.parse_args(
            ["state", "copied", "old", "123", "--timeout", "0"]
        ).timeout
        == 0
    )
