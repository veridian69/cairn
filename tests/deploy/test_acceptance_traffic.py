"""Task 9 live-traffic behaviour around the P-65 mutation barrier."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "traffic.py"


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    specification = importlib.util.spec_from_file_location(
        "kind_acceptance_traffic", SCRIPT
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_a_typed_503_retries_the_same_mutation_instead_of_stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing the 503 retry must stop before the first mutation completes."""
    traffic = _load(monkeypatch)
    token = tmp_path / "token"
    token.write_text("cairn1.token\n", encoding="utf-8")
    calls: list[str] = []

    def ingest(_base_url: str, _token: str, marker: str) -> str:
        calls.append(marker)
        if len(calls) == 1:
            raise traffic.client.DeniedError(
                "/v1/ingest",
                503,
                '{"failure":{"code":"dependency_unavailable","retry":"after-delay"}}',
            )
        if len(calls) == 3:
            raise traffic.client.RoundTripError("stop the test loop")
        return "mutation"

    monkeypatch.setattr(traffic.client, "_ingest", ingest)
    monkeypatch.setattr(traffic.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(traffic.time, "time_ns", lambda: 1_500)

    assert traffic.main(["traffic.py", "http://cairn", str(token), "live"]) == 1
    assert calls == ["live 0", "live 0", "live 1"]
    assert capsys.readouterr().out.splitlines() == [
        "ready",
        "mutations 1 1500",
        "failed after 1: stop the test loop",
    ]


def test_an_untyped_503_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A status alone must not turn an unrelated refusal into a retry loop."""
    traffic = _load(monkeypatch)
    token = tmp_path / "token"
    token.write_text("cairn1.token\n", encoding="utf-8")
    calls = 0

    def ingest(_base_url: str, _token: str, _marker: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise traffic.client.RoundTripError("retried an untyped refusal")
        raise traffic.client.DeniedError(
            "/v1/ingest",
            503,
            '{"failure":{"code":"internal_error","retry":"never"}}',
        )

    monkeypatch.setattr(traffic.client, "_ingest", ingest)
    monkeypatch.setattr(traffic.time, "sleep", lambda _seconds: None)

    assert traffic.main(["traffic.py", "http://cairn", str(token), "live"]) == 1
    assert calls == 1
    assert "dependency_unavailable" not in capsys.readouterr().out
