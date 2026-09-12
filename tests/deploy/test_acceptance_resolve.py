"""The hermetic provider-name observation used by kind acceptance."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPOSITORY = Path(__file__).resolve().parents[2]
RESOLVER = REPOSITORY / "tests" / "acceptance" / "kind" / "resolve.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location("resolve", RESOLVER)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


resolve = _load()


def _resolve(host: str, expected: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RESOLVER), host, expected, "443"],
        text=True,
        capture_output=True,
        check=False,
    )


def test_the_expected_resolved_address_is_matched() -> None:
    result = _resolve("127.0.0.1", "127.0.0.1")
    assert result.returncode == 0
    assert result.stdout == "matched\n"


def test_another_resolved_address_is_not_accepted() -> None:
    result = _resolve("127.0.0.1", "192.0.2.1")
    assert result.returncode == 1
    assert result.stdout == "mismatched\n"


def test_a_stale_answer_resets_dns_convergence() -> None:
    assert resolve.converges((True, True, False, True, True), required=3) is False


def test_only_the_required_consecutive_answers_converge() -> None:
    assert resolve.converges((False, True, True, True), required=3) is True
