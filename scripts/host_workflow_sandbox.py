"""Bounded native-Linux filesystem prerequisite, NOT a provider launch gate.

Controller-only API: stage reviewed dependency closures in five exact slots,
seal once, then run_host. An immutable bridge inside that jail calls run_cli.
Never expose these functions or staging paths directly as model tools. The
bridge must separately validate its fixed profile/command grammar. Effective
host tool inventory remains UNPROVEN; no providers or OAuth discovery here.

The trusted parent owns staging and must not mutate it during a run. Read-only
mounts protect against jailed writers, not a hostile same-UID parent. /usr is
the fixed system runtime dependency; no automatic dependency discovery occurs.
Networking is explicitly shared, including loopback: this is NOT a firewall.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

MANAGED_POLICY_PATHS = tuple(
    Path(path)
    for path in (
        "/etc/codex/managed_config.toml",
        "/etc/codex/requirements.toml",
        "/etc/claude-code/managed-settings.json",
        "/etc/claude-code/managed-settings.d",
    )
)
_SLOTS = ("host-runtime", "cli-runtime", "work", "host-config", "cli-config")
# CLI command/renderer contracts are byte limits including JSON escaping/newline.
_CLI_STDIN_LIMIT = 1048576
_CLI_STDOUT_LIMIT = 1048576
_CLI_STDERR_LIMIT = 65536
# Whole-host prompt/events have a separate finite lifetime budget. This does not
# promise an entire future subscribed workflow fits in one capture.
_HOST_STDIN_LIMIT = 65536
_HOST_STDOUT_LIMIT = 8388608
_HOST_STDERR_LIMIT = 262144
# Fixed per-invocation bridge transport limits, not caller-overridable budgets.
STDIO_INPUT_LIMIT = 8388608
STDIO_OUTPUT_LIMIT = 8388608
STDIO_STDERR_LIMIT = 262144
STDIO_MAX_SECONDS = 60.0
_STDIO_BUFFER = 65536


class SandboxFailure(RuntimeError):
    """Fixed code only; never attach subprocess output, inputs or source paths."""


@dataclass(frozen=True)
class Manifest:
    """Controller-owned staging identity; fingerprint is suitable for evidence."""

    root: Path = field(repr=False)
    fingerprint: str


@dataclass(frozen=True)
class Capture:
    """In-memory inert bytes; callers must sanitise before reporting/persistence."""

    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    input_bytes: int


@dataclass(frozen=True)
class StdioResult:
    """Orderly transport only, not protocol/session acceptance; no retained bytes."""

    input_bytes: int
    output_bytes: int
    stderr_bytes: int


def _policy_refusal() -> None:
    try:
        for path in MANAGED_POLICY_PATHS:
            # lexists semantics: dangling managed-policy links also refuse.
            if path.exists() or path.is_symlink():
                raise SandboxFailure("managed_policy_requires_review")
    except OSError:
        raise SandboxFailure("managed_policy_requires_review") from None


def _fingerprint(root: Path) -> str:
    try:
        if (
            sys.platform != "linux"
            or not root.is_absolute()
            or root.resolve(strict=True) != root
            or not root.is_relative_to("/tmp")
            or root == Path("/tmp")
            or root.stat().st_uid != os.getuid()
            or root.stat().st_mode & 0o077
            or {p.name for p in root.iterdir()} != set(_SLOTS)
        ):
            raise ValueError
        digest = hashlib.sha256(b"cairn-host-sandbox-manifest/v2\0")
        count = 0
        total = 0
        for slot in _SLOTS:
            directory = root / slot
            if not directory.is_dir():
                raise ValueError
            for path in (directory, *sorted(directory.rglob("*"))):
                info = path.lstat()
                count += 1
                if (
                    count > 8192
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o022
                    or path.resolve(strict=True) != path
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
                ):
                    raise ValueError
                content_hash = None
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > 1024 * 1024 * 1024:
                        raise ValueError
                    content_digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        while chunk := stream.read(65536):
                            content_digest.update(chunk)
                    content_hash = content_digest.hexdigest()
                # Content cannot impersonate record separators or metadata.
                # Fixed-schema canonical JSON plus explicit byte-length framing
                # also distinguishes files, directories and escaped path names.
                record = json.dumps(
                    [str(path.relative_to(root)), info.st_mode, content_hash],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
                digest.update(len(record).to_bytes(8, "big"))
                digest.update(record)
        for slot in ("host-runtime", "cli-runtime"):
            executable = root / slot / "entry"
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError
        return digest.hexdigest()
    except (OSError, ValueError, RuntimeError):
        raise SandboxFailure("sandbox_source_invalid") from None


def seal(root: Path) -> Manifest:
    """Refuse managed policy BEFORE accessing disposable staging; pin its bytes.

    root must be a canonical private directory under native /tmp, containing
    exactly host-runtime, cli-runtime, work, host-config and cli-config. All
    entries must be owned regular files/directories, without symlinks/hardlinks
    or group/other write access. Both runtime slots require executable `entry`.
    work contains the installed native skill layout and manifest; host-config
    contains explicit dummy config in prerequisite tests. No ambient discovery.
    """
    _policy_refusal()
    return Manifest(root, _fingerprint(root))


def _command(
    mounts: tuple[tuple[Path, str], ...], host: bool, argv: tuple[str, ...]
) -> list[str]:
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--as-pid-1",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    # Fixed merged-/usr loader compatibility; no host /etc, /tmp or home bind.
    for name in ("bin", "sbin", "lib", "lib64"):
        command.extend(("--symlink", f"usr/{name}", f"/{name}"))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/scratch",
            "--dir",
            "/scratch/home",
        )
    )
    if host:
        command.extend(("--tmpfs", "/host"))
    for source, destination in mounts:
        command.extend(("--ro-bind", str(source), destination))
    environment = {
        "HOME": "/scratch/home",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": "/scratch/config",
        "XDG_CACHE_HOME": "/scratch/cache",
        "XDG_DATA_HOME": "/scratch/data",
        "XDG_STATE_HOME": "/scratch/state",
    }
    if host:
        environment.update(
            CODEX_HOME="/host/config",
            CLAUDE_CONFIG_DIR="/host/config",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
    for key, value in environment.items():
        command.extend(("--setenv", key, value))
    command.extend(
        (
            "--chdir",
            "/work" if host else "/scratch",
            "--remount-ro",
            "/",
            "--",
            "/runtime/host/entry" if host else "/runtime/cli/entry",
            *argv,
        )
    )
    return command


def _spawn(command: list[str]) -> subprocess.Popen[bytes]:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pipesize=4096,
            env={},
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise SandboxFailure("sandbox_launch_failed") from None
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    return process


def _reap(process: subprocess.Popen[bytes]) -> None:
    # Kill the bwrap monitor, not just the payload's old process group.
    # die-with-parent kills payload PID 1; Linux then kills every descendant
    # in that PID namespace, even setsid children and nested CLI namespaces.
    if process.poll() is None:
        process.kill()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise SandboxFailure("sandbox_cleanup_failed") from None
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None
            stream.close()


def _capture(
    command: list[str],
    stdin: bytes,
    deadline: float,
    *,
    stdout_limit: int,
    stderr_limit: int,
) -> Capture:
    started = time.monotonic()
    process = _spawn(command)
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    out, err = bytearray(), bytearray()
    sent = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, label in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            if stdin:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            else:
                process.stdin.close()
            while selector.get_map() or process.poll() is None:
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    raise SandboxFailure("sandbox_timeout")
                for key, _ in selector.select(min(remaining, 0.05)):
                    if key.data == "in":
                        try:
                            sent += os.write(key.fd, stdin[sent : sent + 4096])
                        except BrokenPipeError:
                            raise SandboxFailure("sandbox_input_closed") from None
                        if sent == len(stdin):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = out if key.data == "out" else err
                        limit = stdout_limit if key.data == "out" else stderr_limit
                        if len(target) + len(chunk) > limit:
                            raise SandboxFailure("sandbox_output_limit")
                        target.extend(chunk)
            return Capture(process.wait(), bytes(out), bytes(err), len(stdin))
    finally:
        _reap(process)


def _validate_request(
    argv: tuple[str, ...],
    stdin: bytes,
    deadline: float,
    *,
    input_limit: int,
) -> None:
    if not isinstance(stdin, bytes) or len(stdin) > input_limit:
        raise SandboxFailure("sandbox_input_limit")
    if (
        not isinstance(argv, tuple)
        or len(argv) > 64
        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
    ):
        raise SandboxFailure("sandbox_argv_invalid")
    try:
        if sum(len(arg.encode()) for arg in argv) > 8192:
            raise SandboxFailure("sandbox_argv_invalid")
    except UnicodeError:
        raise SandboxFailure("sandbox_argv_invalid") from None
    if not math.isfinite(deadline) or not 0 < deadline <= 60:
        raise SandboxFailure("sandbox_deadline_invalid")


def _host_command(manifest: Manifest, argv: tuple[str, ...]) -> list[str]:
    if _fingerprint(manifest.root) != manifest.fingerprint:
        raise SandboxFailure("sandbox_source_changed")
    destinations = ("/runtime/host", "/runtime/cli", "/work", "/host/config", "/cli")
    mounts = tuple(
        (manifest.root / slot, dest)
        for slot, dest in zip(_SLOTS, destinations, strict=True)
    )
    return _command(mounts, True, argv)


def run_host(
    manifest: Manifest,
    argv: tuple[str, ...] = (),
    *,
    stdin: bytes = b"",
    deadline: float = 30,
) -> Capture:
    """Pinned host/bridge: 64 KiB input, 8 MiB events, separate 256 KiB stderr.

    Bounds cover the whole process lifetime; this grants no live gate.
    """
    _policy_refusal()
    _validate_request(argv, stdin, deadline, input_limit=_HOST_STDIN_LIMIT)
    return _capture(
        _host_command(manifest, argv),
        stdin,
        deadline,
        stdout_limit=_HOST_STDOUT_LIMIT,
        stderr_limit=_HOST_STDERR_LIMIT,
    )


def _stdio_descriptors(input_fd: int, output_fd: int) -> None:
    try:
        identities = []
        for fd, access in ((input_fd, os.O_RDONLY), (output_fd, os.O_WRONLY)):
            if type(fd) is not int or fd < 0:
                raise ValueError
            metadata = os.fstat(fd)
            if (
                not stat.S_ISFIFO(metadata.st_mode)
                or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != access
            ):
                raise ValueError
            identities.append((metadata.st_dev, metadata.st_ino))
        if identities[0] == identities[1]:
            raise ValueError
    except (OSError, ValueError, OverflowError):
        raise SandboxFailure("sandbox_stdio_invalid") from None


def _stdio_time(deadline: float, cancel: Event | None) -> float:
    if cancel is not None and cancel.is_set():
        raise SandboxFailure("sandbox_cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SandboxFailure("sandbox_timeout")
    return remaining


def _pump_stdio(
    process: subprocess.Popen[bytes],
    input_fd: int,
    output_fd: int,
    deadline: float,
    cancel: Event | None,
) -> StdioResult:
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    child_input = process.stdin.fileno()
    child_output = process.stdout.fileno()
    child_error = process.stderr.fileno()
    for fd in (child_input, child_output, child_error):
        os.set_blocking(fd, False)
    pending_input, pending_output = bytearray(), bytearray()
    input_eof = output_eof = error_eof = False
    input_bytes = output_bytes = stderr_bytes = 0
    with selectors.DefaultSelector() as selector:

        def watch(fd: int, events: int, label: str) -> None:
            try:
                key = selector.get_key(fd)
            except KeyError:
                if events:
                    selector.register(fd, events, label)
            else:
                if not events:
                    selector.unregister(fd)
                elif key.events != events:
                    selector.modify(fd, events, label)

        while True:
            remaining = _stdio_time(deadline, cancel)
            if input_eof and not pending_input and not process.stdin.closed:
                watch(child_input, 0, "child-input")
                process.stdin.close()
            watch(
                input_fd,
                selectors.EVENT_READ if not input_eof and not pending_input else 0,
                "input",
            )
            if not process.stdin.closed:
                watch(
                    child_input,
                    selectors.EVENT_WRITE if pending_input else 0,
                    "child-input",
                )
            watch(
                child_output,
                selectors.EVENT_READ if not output_eof and not pending_output else 0,
                "child-output",
            )
            watch(output_fd, selectors.EVENT_WRITE if pending_output else 0, "output")
            watch(child_error, selectors.EVENT_READ if not error_eof else 0, "stderr")
            for key, _ in selector.select(min(remaining, 0.05)):
                _stdio_time(deadline, cancel)
                try:
                    if key.data == "child-input":
                        try:
                            sent = os.write(key.fd, pending_input)
                        except BrokenPipeError:
                            raise SandboxFailure("sandbox_input_closed") from None
                        del pending_input[:sent]
                    elif key.data == "output":
                        try:
                            sent = os.write(key.fd, pending_output)
                        except BrokenPipeError:
                            raise SandboxFailure("sandbox_output_closed") from None
                        del pending_output[:sent]
                    elif key.data == "input":
                        chunk = os.read(
                            key.fd,
                            min(_STDIO_BUFFER, STDIO_INPUT_LIMIT - input_bytes + 1),
                        )
                        input_bytes += len(chunk)
                        if input_bytes > STDIO_INPUT_LIMIT:
                            raise SandboxFailure("sandbox_input_limit")
                        pending_input.extend(chunk)
                        input_eof = not chunk
                    elif key.data == "child-output":
                        chunk = os.read(
                            key.fd,
                            min(_STDIO_BUFFER, STDIO_OUTPUT_LIMIT - output_bytes + 1),
                        )
                        output_bytes += len(chunk)
                        if output_bytes > STDIO_OUTPUT_LIMIT:
                            raise SandboxFailure("sandbox_output_limit")
                        pending_output.extend(chunk)
                        output_eof = not chunk
                    else:
                        chunk = os.read(
                            key.fd,
                            min(_STDIO_BUFFER, STDIO_STDERR_LIMIT - stderr_bytes + 1),
                        )
                        stderr_bytes += len(chunk)
                        if stderr_bytes > STDIO_STDERR_LIMIT:
                            raise SandboxFailure("sandbox_output_limit")
                        error_eof = not chunk
                except BlockingIOError:
                    continue
            returncode = process.poll()
            if returncode is not None:
                if returncode != 0:
                    raise SandboxFailure("sandbox_child_failed")
                if pending_input:
                    raise SandboxFailure("sandbox_input_closed")
                if not input_eof:
                    raise SandboxFailure("sandbox_premature_eof")
                if output_eof and error_eof and not pending_output:
                    return StdioResult(input_bytes, output_bytes, stderr_bytes)


def serve_host_stdio(
    manifest: Manifest,
    input_fd: int,
    output_fd: int,
    deadline: float,
    *,
    cancel: Event | None = None,
) -> StdioResult:
    """Relay one fixed jailed entry, with an absolute time.monotonic deadline.

    Controller-only, no argv/env/endpoint overrides. At entry the deadline must
    be finite, in the future and at most 60s away; validation and draining spend
    that same deadline. Fixed cumulative 8 MiB input/output and 256 KiB stderr;
    at most 64 KiB pending per direction, with backpressure, no truncation/retry.
    Stderr is counted and discarded. Bytes already delivered cannot be undone.

    The two different pipes are BORROWED EXCLUSIVELY: do not close, use or change
    their open-file-description flags concurrently (including through aliases).
    Blocking flags are restored, and originals stay open on return/error. The
    controller closes them afterwards to propagate EOF/failure to its peer.
    This call alone owns its fresh bwrap monitor/process group/PID namespace;
    every exit reaps it, allowing at most a further two seconds for cleanup.
    Set cancel from another thread and join this call before releasing staging.

    Success means input EOF, all accepted bytes delivered, both child outputs
    drained, exit zero and completed reap. It does NOT certify complete MCP
    frames, no pending requests, receipts or native terminal success: controller
    and bridge own those checks. Controller must atomically admit this call ONCE
    per invocation, even after failure; never reset the deadline/budget on restart.
    No native host/account access or service/session admission is implemented.
    """
    _policy_refusal()
    started = time.monotonic()
    if (
        type(deadline) not in (int, float)
        or not started < deadline <= started + STDIO_MAX_SECONDS
    ):
        raise SandboxFailure("sandbox_deadline_invalid")
    _validate_request((), b"", deadline - started, input_limit=STDIO_INPUT_LIMIT)
    if cancel is not None and not isinstance(cancel, Event):
        raise SandboxFailure("sandbox_cancel_invalid")
    _stdio_descriptors(input_fd, output_fd)
    _stdio_time(deadline, cancel)
    command = _host_command(manifest, ())
    _stdio_time(deadline, cancel)
    old_input, old_output = os.get_blocking(input_fd), os.get_blocking(output_fd)
    process = None
    try:
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)
        process = _spawn(command)
        return _pump_stdio(process, input_fd, output_fd, deadline, cancel)
    except OSError:
        raise SandboxFailure("sandbox_stdio_failed") from None
    finally:
        try:
            if process is not None:
                _reap(process)
        finally:
            os.set_blocking(input_fd, old_input)
            os.set_blocking(output_fd, old_output)


def run_cli(
    argv: tuple[str, ...] = (), *, stdin: bytes = b"", deadline: float = 30
) -> Capture:
    """For the immutable bridge INSIDE run_host only, after protocol validation.

    No caller-supplied source, executable, environment or destination. Parent
    policy refusal already ran before hiding /etc. The CLI sees neither host
    runtime/work nor /host; only its immutable runtime and synthetic profile.
    Per command: 1 MiB stdin, 1 MiB stdout, separate 64 KiB stderr, all in bytes.
    """
    _validate_request(argv, stdin, deadline, input_limit=_CLI_STDIN_LIMIT)
    mounts = ((Path("/runtime/cli"), "/runtime/cli"), (Path("/cli"), "/cli"))
    if any(not path.is_dir() or path.resolve() != path for path, _ in mounts):
        raise SandboxFailure("sandbox_cli_context_invalid")
    return _capture(
        _command(mounts, False, argv),
        stdin,
        deadline,
        stdout_limit=_CLI_STDOUT_LIMIT,
        stderr_limit=_CLI_STDERR_LIMIT,
    )
