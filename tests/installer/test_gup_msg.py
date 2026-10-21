from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "acceptance/installer/gup_msg.py"
_spec = importlib.util.spec_from_file_location("gup_msg", _path)
assert _spec is not None and _spec.loader is not None
gup_msg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gup_msg)


@pytest.mark.parametrize("raw", ["generation", "send:a", "has:a_2-3"])
def test_validate_ops_accepts_supported_operations(raw: str) -> None:
    kind = raw.partition(":")[0]
    assert gup_msg.validate_ops([raw]) == [
        (kind, None if raw == "generation" else raw.partition(":")[2])
    ]


@pytest.mark.parametrize(
    "raw", ["", "has:", "send:../x", "send:a/b", "send:a b", "oops:x", "generation:x"]
)
def test_validate_ops_rejects_invalid_operations(raw: str) -> None:
    with pytest.raises(ValueError):
        gup_msg.validate_ops([raw])


@pytest.mark.parametrize("value", ["-1", "inf", "nan"])
def test_parser_rejects_invalid_lease_wait(value: str) -> None:
    with pytest.raises(SystemExit):
        gup_msg.parse_args(
            [
                "--garden",
                "g",
                "--bundle-dir",
                "b",
                "--store",
                "s",
                "--lease-wait",
                value,
                "generation",
            ]
        )


def test_parser_accepts_zero_lease_wait() -> None:
    assert (
        gup_msg.parse_args(
            [
                "--garden",
                "g",
                "--bundle-dir",
                "b",
                "--store",
                "s",
                "--lease-wait",
                "0",
                "generation",
            ]
        ).lease_wait
        == 0
    )


def test_startup_timeout_closes_the_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    closed = []

    class TimedOut:
        def __init__(self, *args: object) -> None:
            pass

        def start(self, binding: object) -> str:
            raise TimeoutError("simulated startup timeout")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(gup_msg, "Client", TimedOut)
    monkeypatch.setattr(gup_msg.rt, "seed_home", lambda *args: {})
    monkeypatch.setattr(gup_msg.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(TimeoutError, match="simulated startup timeout"):
        gup_msg.main(
            [
                "--garden",
                "unused",
                "--bundle-dir",
                str(tmp_path),
                "--store",
                str(tmp_path),
                "generation",
            ]
        )
    assert closed == [True]
