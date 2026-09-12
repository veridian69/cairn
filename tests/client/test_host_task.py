"""The trusted host path admits exactly the bytes sent to its child process."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client.host_task import AdmittedTask, HostTaskError, run_host_task
from cairn.client.profiles import MemoryProfile


def profile() -> MemoryProfile:
    return MemoryProfile(
        endpoint="http://127.0.0.1:18423",
        expected_instance_id=uuid4(),
        scope=Scope("everyday", (ScopeSegment("job", "source-test"),)),
        classification=Classification.INTERNAL,
        credential_file=Path("/unused-credential"),
        session_id=uuid4(),
    )


def test_child_receives_identical_unicode_task_and_source_is_removed() -> None:
    task = "Do not book anything.\r\nCapacity is eight. Café — 修理.\n".encode()
    source_id, principal = uuid4(), uuid4()
    paths: list[Path] = []

    def command(admission: AdmittedTask) -> list[str]:
        paths.append(admission.sources_path)
        assert admission.task_bytes == task
        assert admission.sources_path.stat().st_mode & 0o777 == 0o400
        assert admission.sources_path.parent.stat().st_mode & 0o777 == 0o700
        return [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; "
            "print(json.dumps({'input':sys.stdin.buffer.read().decode('utf-8'),"
            "'source':json.loads(Path(sys.argv[1]).read_text())}))",
            str(admission.sources_path),
        ]

    result = run_host_task(
        profile(),
        expected_principal=principal,
        source_id=source_id,
        task=task,
        command=command,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["input"].encode() == task
    source = output["source"]["sources"][0]
    assert source == {
        "source_id": str(source_id),
        "origin": "host_input",
        "body": task.decode(),
    }
    assert output["source"]["principal_id"] == str(principal)
    assert not paths[0].exists()
    assert not paths[0].parent.exists()


@pytest.mark.parametrize("task", [b"", b"\xff", b"x" * 16385])
def test_invalid_task_never_reaches_command_builder(task: bytes) -> None:
    def command(admission: AdmittedTask) -> list[str]:
        pytest.fail("invalid source reached the host")

    with pytest.raises(ValueError):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=task,
            command=command,
        )


def test_timeout_terminates_child_and_removes_source() -> None:
    paths: list[Path] = []

    def command(admission: AdmittedTask) -> list[str]:
        paths.append(admission.sources_path)
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    with pytest.raises(HostTaskError, match="^host_timeout$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=command,
            timeout=0.1,
        )
    assert not paths[0].parent.exists()


def test_launch_failure_removes_source_without_retry() -> None:
    paths: list[Path] = []

    def command(admission: AdmittedTask) -> list[str]:
        paths.append(admission.sources_path)
        return ["/no-such-cairn-test-host"]

    with pytest.raises(OSError):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=command,
        )
    assert len(paths) == 1
    assert not paths[0].parent.exists()


def test_host_failure_is_returned_once_without_retry() -> None:
    calls = 0

    def command(admission: AdmittedTask) -> list[str]:
        nonlocal calls
        calls += 1
        return [sys.executable, "-c", "import sys; sys.exit(17)"]

    result = run_host_task(
        profile(),
        expected_principal=uuid4(),
        source_id=uuid4(),
        task=b"Actual task",
        command=command,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 17
    assert calls == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr", "both"])
def test_combined_output_limit_closes_without_retry(stream: str) -> None:
    paths: list[Path] = []

    def command(admission: AdmittedTask) -> list[str]:
        paths.append(admission.sources_path)
        return [
            sys.executable,
            "-c",
            "import os; "
            + {
                "stdout": "os.write(1,b'x'*4097)",
                "stderr": "os.write(2,b'x'*4097)",
                "both": "os.write(1,b'x'*2048); os.write(2,b'y'*2049)",
            }[stream],
        ]

    with pytest.raises(HostTaskError, match="^host_output_limit$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=command,
            max_output_bytes=4096,
        )
    assert len(paths) == 1
    assert not paths[0].parent.exists()


def test_full_duplex_pumping_preserves_exact_input_and_both_outputs() -> None:
    task = ("é" * 8192).encode()
    result = run_host_task(
        profile(),
        expected_principal=uuid4(),
        source_id=uuid4(),
        task=task,
        command=lambda _: [
            sys.executable,
            "-c",
            "import os,sys; os.write(1,b'o'*65536); os.write(2,b'e'*65536); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        timeout=2,
        max_output_bytes=147456,
    )
    assert result.returncode == 0
    assert result.stdout == b"o" * 65536 + task
    assert result.stderr == b"e" * 65536


def test_nonconsuming_stdin_cannot_hide_output_overflow() -> None:
    started = time.monotonic()
    with pytest.raises(HostTaskError, match="^host_output_limit$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"x" * 16384,
            command=lambda _: [
                sys.executable,
                "-c",
                "import os,time; os.write(2,b'e'*65536); time.sleep(30)",
            ],
            timeout=2,
            max_output_bytes=1024,
        )
    assert time.monotonic() - started < 3


def test_default_output_cap_is_one_mebibyte() -> None:
    with pytest.raises(HostTaskError, match="^host_output_limit$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=lambda _: [
                sys.executable,
                "-c",
                "import os; os.write(1,b'x'*1048577)",
            ],
        )


def test_nonconsuming_stdin_and_closed_outputs_obey_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(HostTaskError, match="^host_timeout$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"x" * 16384,
            command=lambda _: [
                sys.executable,
                "-c",
                "import os,time; os.close(1); os.close(2); time.sleep(30)",
            ],
            timeout=0.1,
        )
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("captured", [True, False])
def test_timeout_kills_group_even_when_leader_exits_and_grandchild_ignores_term(
    tmp_path: Path,
    captured: bool,
) -> None:
    pid_path = tmp_path / "grandchild.pid"
    grandchild = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    child = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(30)"
    )
    try:
        with pytest.raises(HostTaskError, match="^host_timeout$"):
            run_host_task(
                profile(),
                expected_principal=uuid4(),
                source_id=uuid4(),
                task=b"Actual task",
                command=lambda _: [sys.executable, "-c", child],
                stdout=subprocess.PIPE if captured else subprocess.DEVNULL,
                stderr=subprocess.PIPE if captured else subprocess.DEVNULL,
                timeout=0.3,
            )
        pid = int(pid_path.read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{pid}/stat")
            try:
                state = stat.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("grandchild survived host timeout")
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_supplied_output_file_and_merged_stderr_remain_supported(
    tmp_path: Path,
) -> None:
    with (tmp_path / "output").open("w+b") as output:
        result = run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=lambda _: [
                sys.executable,
                "-c",
                "import os; os.write(1,b'out'); os.write(2,b'err')",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            max_output_bytes=1,
        )
        output.seek(0)
        assert output.read() == b"outerr"
    assert result.stdout is None and result.stderr is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_output_limit_refused_before_launch(limit: int) -> None:
    def command(_: AdmittedTask) -> list[str]:
        pytest.fail("invalid output limit reached command builder")

    with pytest.raises(HostTaskError, match="^invalid_host_output_limit$"):
        run_host_task(
            profile(),
            expected_principal=uuid4(),
            source_id=uuid4(),
            task=b"Actual task",
            command=command,
            max_output_bytes=limit,
        )
