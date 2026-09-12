"""Fresh server and client processes recover the same actual custody IDs."""

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
from uuid import uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support

from cairn.catalogue.sqlite import read_connection

WORKER = Path(__file__).with_name("session_process_worker.py")


@contextmanager
def server(
    instance: Any, listener: socket.socket, *, crash: bool = False
) -> Iterator[subprocess.Popen[str]]:
    packet = {
        "config": instance.config.model_dump(mode="json"),
        "now": instance.clock().isoformat(),
        "fd": listener.fileno(),
        "crash_after_ingest": crash,
    }
    process = subprocess.Popen(
        [sys.executable, str(WORKER), "server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(listener.fileno(),),
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(packet) + "\n")
    process.stdin.close()
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=0.2, trust_env=False) as http:
            while time.monotonic() < deadline:
                assert process.poll() is None, (
                    process.stderr.read()
                    if process.stderr
                    else "synthetic server exited before startup"
                )
                try:
                    if http.get(endpoint + "/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.02)
            else:
                pytest.fail("synthetic server did not become ready")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def invoke(mode: str, packet: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(WORKER), mode],
        input=json.dumps(packet) + "\n",
        text=True,
        capture_output=True,
        timeout=25,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("crash_gap", [False, True])
def test_fresh_server_and_client_resume_without_callback(
    tmp_path: Path, memory_support: ModuleType, crash_gap: bool
) -> None:
    instance = memory_support.Instance(tmp_path, attic=False)
    _, token = instance.add_actor()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        packet = {
            "endpoint": f"http://127.0.0.1:{listener.getsockname()[1]}",
            "token": token,
            "instance_id": str(instance.config.instance_id),
            "session_id": str(uuid4()),
            "turn_id": str(uuid4()),
            "attempt_id": str(uuid4()),
        }
        with server(instance, listener) as first:
            prepared = invoke("prepare", packet)
            assert prepared == {
                "state": "prepared",
                "response": "Exact output across processes.",
                "callbacks": 1,
            }
            first_pid = first.pid
        gap_ids = None
        if crash_gap:
            with server(instance, listener, crash=True) as doomed:
                interrupted = invoke("resume", packet)
                assert interrupted["callbacks"] == 0
                assert interrupted["state"] == "pending"
                assert doomed.wait(timeout=5) == 73
            with read_connection(instance.data_path) as connection:
                gap_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT fact_id FROM facts ORDER BY fact_id"
                    )
                ]
                assert len(gap_ids) == 1
                assert (
                    connection.execute(
                        "SELECT count(*) FROM memory_session_terminals"
                    ).fetchone()[0]
                    == 0
                )
        with server(instance, listener) as second:
            assert second.pid != first_pid
            recovered = invoke("resume", packet)
            assert recovered["state"] == "committed"
            assert recovered["callbacks"] == 0
            assert recovered["response"] == prepared["response"]
            if gap_ids is not None:
                assert recovered["fact_ids"] == gap_ids
        with server(instance, listener):
            again = invoke("resume", packet)
            assert again == recovered
        with read_connection(instance.data_path) as connection:
            assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
