"""Actual installed command and restarted server retain exact custody identities."""

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import checkpoint, profile

from cairn.catalogue.sqlite import read_connection

DRIVER = Path(__file__).with_name("memory_cli_process_driver.py")
COMMAND = Path(sys.executable).with_name("cairn-memory")


@contextmanager
def server(
    instance: Any, listener: socket.socket, crash: str = ""
) -> Iterator[subprocess.Popen[str]]:
    assert DRIVER.exists(), "CLI restart driver missing"
    packet = {
        "config": instance.config.model_dump(mode="json"),
        "now": instance.clock().isoformat(),
        "fd": listener.fileno(),
        "crash": crash,
    }
    process = subprocess.Popen(
        [sys.executable, str(DRIVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(listener.fileno(),),
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(packet) + "\n")
    process.stdin.close()
    try:
        with httpx.Client(timeout=0.2, trust_env=False) as http:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, "synthetic server exited early"
                try:
                    if (
                        http.get(
                            f"http://127.0.0.1:{listener.getsockname()[1]}/health/ready"
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.02)
            else:
                pytest.fail("synthetic startup timeout")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


def command(path: Path, name: str, value: object) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(DRIVER), "client", "--profile", str(path), name],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert "Traceback" not in completed.stderr
    return completed.returncode, json.loads(completed.stderr or completed.stdout)


@pytest.mark.parametrize(
    "crash", ["turn-begin", "turn-prepare", "ingest", "turn-commit"]
)
def test_actual_cli_restart_custody_and_no_regeneration(
    tmp_path: Path, memory_support: ModuleType, crash: str
) -> None:
    instance = memory_support.Instance(tmp_path, attic=False)
    _, token = instance.add_actor()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        path = profile(
            tmp_path, instance, token, f"http://127.0.0.1:{listener.getsockname()[1]}"
        )
        original_profile = path.read_bytes()
        value = checkpoint()
        with server(instance, listener, crash) as doomed:
            code, failed = command(path, "remember", value)
            assert code == (4 if crash == "turn-begin" else 3), failed
            assert doomed.wait(timeout=5) == 73
        with read_connection(instance.data_path) as con:
            gap_ids = [
                row[0]
                for row in con.execute("SELECT fact_id FROM facts ORDER BY fact_id")
            ]
            terminals = con.execute(
                "SELECT count(*) FROM memory_session_terminals"
            ).fetchone()[0]
            assert len(gap_ids) == (1 if crash in {"ingest", "turn-commit"} else 0)
            assert terminals == (1 if crash == "turn-commit" else 0)
        with server(instance, listener):
            code, state = command(path, "status", {"turn_id": value["turn_id"]})
            assert code == 0
            assert (
                state["result"]["state"]
                == {
                    "turn-begin": "started",
                    "turn-prepare": "prepared",
                    "ingest": "prepared",
                    "turn-commit": "committed",
                }[crash]
            )
            code, recovered = command(path, "resume", {"turn_id": value["turn_id"]})
            if crash == "turn-begin":
                assert code == 3 and recovered["result"]["completed_turn"] is None
                code, recovered = command(path, "remember", value)
            assert code == 0 and recovered["result"]["state"] == "committed", recovered
            assert (
                recovered["result"]["completed_turn"]["response"] == value["response"]
            )
            receipt = recovered["result"]["persistence"]
            if gap_ids:
                assert receipt["result"]["fact_ids"] == gap_ids
        with server(instance, listener):
            code, again = command(path, "resume", {"turn_id": value["turn_id"]})
            assert code == 0 and again["result"]["persistence"] == receipt
            assert (
                again["result"]["completed_turn"]
                == recovered["result"]["completed_turn"]
            )
        with read_connection(instance.data_path) as con:
            assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
        assert path.read_bytes() == original_profile


@pytest.mark.parametrize(
    "name",
    [
        "check",
        "arrive",
        "recall",
        "acknowledge-visit",
        "remember",
        "status",
        "resume",
        "abandon",
        "history",
        "correct",
        "suggest",
    ],
)
def test_installed_help_without_profile_or_stdin(name: str) -> None:
    result = subprocess.run(
        [str(COMMAND), name, "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and b"usage:" in result.stdout and not result.stderr
