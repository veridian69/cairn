from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def _acceptance() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "test_macos_foreground.py"
    spec = importlib.util.spec_from_file_location("macos_foreground_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acceptance = _acceptance()


class _Clock:
    def __init__(self, wall: datetime, monotonic: float = 10.0) -> None:
        self.wall = wall
        self.monotonic = monotonic
        self.sleeps: list[float] = []

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds


def test_fact_visibility_barrier_waits_past_backstepped_wall_clock() -> None:
    recorded_at = datetime(2026, 9, 15, 13, 19, 36, 5_935, tzinfo=UTC)
    clock = _Clock(datetime(2026, 9, 15, 13, 19, 35, 971_127, tzinfo=UTC))

    acceptance.wait_for_fact_visibility(
        recorded_at,
        deadline=11.0,
        wall_now=clock.wall_now,
        monotonic_now=clock.monotonic_now,
        sleep=clock.sleep,
    )

    assert sum(clock.sleeps) == pytest.approx(0.134808)
    assert clock.wall >= recorded_at + timedelta(milliseconds=100)


def test_fact_visibility_barrier_obeys_acceptance_deadline() -> None:
    recorded_at = datetime(2026, 9, 15, 13, 19, 36, tzinfo=UTC)
    clock = _Clock(datetime(2026, 9, 15, 13, 19, 35, tzinfo=UTC))

    with pytest.raises(
        acceptance.AcceptanceFailure,
        match="deadline expired while waiting for remembered facts",
    ):
        acceptance.wait_for_fact_visibility(
            recorded_at,
            deadline=10.02,
            wall_now=clock.wall_now,
            monotonic_now=clock.monotonic_now,
            sleep=clock.sleep,
        )

    assert sum(clock.sleeps) == pytest.approx(0.02)
