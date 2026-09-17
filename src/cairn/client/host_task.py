"""Trusted host admission: the child receives the exact task admitted as source.

This is a host-side utility, never an MCP tool. The caller supplies actual task
input and builds the host command with the explicit sources path. It does not
discover credentials, configure host permissions or attest human authorship.
The temporary document transports one invocation's input; it is not a local
memory or transcript archive. New user input requires a new host admission.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from uuid import UUID

from cairn.client.conversation_sources import HostSource, create_source_bundle
from cairn.client.profiles import MemoryProfile


class HostTaskError(ValueError):
    """Closed host failure codes without source, path or provider diagnostics."""


@dataclass(frozen=True, slots=True)
class AdmittedTask:
    task_bytes: bytes
    sources_path: Path


@contextmanager
def admitted_task(
    profile: MemoryProfile,
    *,
    expected_principal: UUID,
    source_id: UUID,
    task: bytes,
) -> Iterator[AdmittedTask]:
    """Admit one exact UTF-8 task for a host-controlled invocation lifetime."""
    if os.name != "posix" or type(task) is not bytes:
        raise HostTaskError("invalid_host_task")
    try:
        body = task.decode("utf-8")
    except UnicodeError:
        raise HostTaskError("invalid_host_task") from None
    bundle = create_source_bundle(
        profile,
        expected_principal=expected_principal,
        sources=(HostSource(source_id=source_id, body=body),),
    )
    document = json.dumps(
        bundle.to_document(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="cairn-host-task-", dir="/tmp") as temp:
        path = Path(temp) / "sources.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "wb") as target:
            target.write(document)
        yield AdmittedTask(task_bytes=task, sources_path=path)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Join this invocation's process group without launching another host."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader can exit on TERM while a grandchild ignores it. Always
        # finish the group, including when all its output was redirected.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _exchange(
    process: subprocess.Popen[bytes], task: bytes, deadline: float, limit: int
) -> tuple[bytes | None, bytes | None]:
    """Pump all owned pipes together, retaining at most the combined cap."""
    captured: dict[int, bytearray] = {}
    outputs = (process.stdout, process.stderr)
    total = 0
    written = 0
    with selectors.DefaultSelector() as selector:
        assert process.stdin is not None
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        for stream in outputs:
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                captured[stream.fileno()] = bytearray()
                selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostTaskError("host_timeout")
            for key, events in selector.select(remaining):
                if events & selectors.EVENT_WRITE:
                    try:
                        written += os.write(key.fd, task[written : written + 4096])
                    except BrokenPipeError:
                        written = len(task)
                    except BlockingIOError:
                        continue
                    if written == len(task):
                        selector.unregister(key.fd)
                        process.stdin.close()
                else:
                    try:
                        chunk = os.read(key.fd, min(65536, limit - total + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        total += len(chunk)
                        if total > limit:
                            raise HostTaskError("host_output_limit")
                        captured[key.fd].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostTaskError("host_timeout")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise HostTaskError("host_timeout") from None
    return (
        bytes(captured[outputs[0].fileno()]) if outputs[0] is not None else None,
        bytes(captured[outputs[1].fileno()]) if outputs[1] is not None else None,
    )


def run_host_task(
    profile: MemoryProfile,
    *,
    expected_principal: UUID,
    source_id: UUID,
    task: bytes,
    command: Callable[[AdmittedTask], Sequence[str]],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout: IO[bytes] | int | None = subprocess.PIPE,
    stderr: IO[bytes] | int | None = subprocess.PIPE,
    timeout: float = 240,
    max_output_bytes: int = 1048576,
) -> subprocess.CompletedProcess[bytes]:
    """Launch once and feed exactly the admitted task, cleaning up on every exit.

    ``command`` belongs to the trusted host integration. It must configure only
    authorised tools and pass ``sources_path`` to the conversation adapter. No
    task content is interpolated into shell code; no shell is used here.
    Captured stdout and stderr share ``max_output_bytes``; caller-owned output
    destinations retain subprocess semantics and are not captured or counted.
    The deadline covers concurrent stdin/output pumping and process completion.
    Every exit terminates the invocation's remaining process-group members.
    """
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
        raise HostTaskError("invalid_host_timeout")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise HostTaskError("invalid_host_output_limit")
    with admitted_task(
        profile, expected_principal=expected_principal, source_id=source_id, task=task
    ) as admission:
        arguments = command(admission)
        if (
            isinstance(arguments, (str, bytes))
            or not arguments
            or any(
                type(argument) is not str or "\0" in argument for argument in arguments
            )
        ):
            raise HostTaskError("invalid_host_command")
        args = list(arguments)
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            output, errors = _exchange(
                process, admission.task_bytes, deadline, max_output_bytes
            )
        finally:
            _stop(process)
        return subprocess.CompletedProcess(args, process.returncode, output, errors)
