"""Linux process identity and detached-launch helpers for native installs."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from .core import InstallError, defer_spawn_signals, secure_directory

_MAX_RECEIPT_BYTES = 16 * 1024
# A direct caller can stop its own detached child without finalising a live
# Popen; the normal helper CLI exits and leaves supervision to the receipt.
_spawned_processes: dict[int, subprocess.Popen[bytes]] = {}


class _HelperInterrupted(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_time: int
    uid: int
    executable: str
    argv: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "uid": self.uid,
            "executable": self.executable,
            "argv": list(self.argv),
        }

    @classmethod
    def from_json(cls, value: object) -> ProcessIdentity:
        if not isinstance(value, Mapping):
            raise InstallError("Native process receipt is malformed")
        pid = value.get("pid")
        start_time = value.get("start_time")
        uid = value.get("uid")
        executable = value.get("executable")
        argv = value.get("argv")
        if (
            type(pid) is not int
            or pid <= 0
            or type(start_time) is not int
            or start_time <= 0
            or type(uid) is not int
            or uid < 0
            or not isinstance(executable, str)
            or not executable.startswith("/")
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise InstallError("Native process receipt is malformed")
        return cls(pid, start_time, uid, executable, tuple(argv))


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read the kernel identity fields needed to rule out PID reuse."""
    if type(pid) is not int or pid <= 0:
        return None
    directory = Path("/proc") / str(pid)
    try:
        stat_text = (directory / "stat").read_text(encoding="utf-8")
        command_line = (directory / "cmdline").read_bytes()
        status = (directory / "status").read_text(encoding="utf-8")
        executable = os.readlink(directory / "exe")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as error:
        raise InstallError(f"Cannot inspect native process {pid}: {error}") from error

    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise InstallError(f"Cannot parse native process identity for PID {pid}")
    try:
        start_time = int(fields[19])
    except ValueError as error:
        raise InstallError(
            f"Cannot parse native process identity for PID {pid}"
        ) from error
    uid_line = next(
        (line for line in status.splitlines() if line.startswith("Uid:")), ""
    )
    uid_fields = uid_line.split()
    if len(uid_fields) < 2 or not uid_fields[1].isdigit():
        raise InstallError(f"Cannot parse native process owner for PID {pid}")
    argv = tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in command_line.rstrip(b"\0").split(b"\0")
        if item
    )
    if not argv:
        raise InstallError(f"Native process {pid} has no inspectable command line")
    return ProcessIdentity(pid, start_time, int(uid_fields[1]), executable, argv)


def spawn_detached(
    receipt_path: Path,
    log_path: Path,
    argv: Sequence[str],
    *,
    cwd: Path,
) -> ProcessIdentity:
    """Start one detached process and durably publish its exact identity."""
    _prune_spawned_processes()
    command = [str(item) for item in argv]
    if not command or any(not item or "\x00" in item for item in command):
        raise InstallError("Detached native command is invalid")
    secure_directory(receipt_path.parent, private=True)
    secure_directory(log_path.parent, private=True)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise InstallError(f"Refusing to replace process receipt: {receipt_path}")
    log_fd = os.open(
        log_path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    process: subprocess.Popen[bytes] | None = None
    published = False
    try:
        if not stat.S_ISREG(os.fstat(log_fd).st_mode):
            raise InstallError(f"Native service log is not a regular file: {log_path}")
        with defer_spawn_signals() as received:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
        if received:
            raise KeyboardInterrupt
        identity = process_identity(process.pid)
        if identity is None:
            raise InstallError("Native process exited before its identity was recorded")
        if identity.uid != os.getuid() or identity.argv != tuple(command):
            raise InstallError(
                "Started native process does not match the requested identity"
            )
        if identity.executable != os.path.realpath(command[0]):
            raise InstallError(
                "Started native process executable does not match launch intent"
            )
        _create_receipt(receipt_path, identity)
        published = True
        _spawned_processes[process.pid] = process
    except BaseException as error:
        if process is not None and not published:
            with defer_spawn_signals():
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        if isinstance(error, OSError):
            raise InstallError(
                f"Could not start native process: {error.strerror}"
            ) from error
        raise
    finally:
        os.close(log_fd)
    return identity


def load_process_receipt(path: Path) -> ProcessIdentity:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise InstallError(f"Process receipt is not owner-private: {path}")
            raw = stream.read(_MAX_RECEIPT_BYTES + 1)
    except OSError as error:
        raise InstallError(
            f"Cannot safely read process receipt {path}: {error}"
        ) from error
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise InstallError(f"Process receipt is too large: {path}")
    try:
        value: Any = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"Process receipt is malformed: {path}") from error
    return ProcessIdentity.from_json(value)


def stop_process(identity: ProcessIdentity, *, timeout: float = 30.0) -> bool:
    """Signal a process only after a pidfd and all recorded fields agree."""
    try:
        pid_fd = os.pidfd_open(identity.pid)
    except ProcessLookupError:
        _reap_if_child(identity.pid)
        return False
    try:
        observed = process_identity(identity.pid)
        if observed is None:
            _reap_if_child(identity.pid)
            return False
        if observed != identity:
            raise InstallError(
                "Native process identity changed; refusing to signal a reused PID"
            )
        signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
        poller = select.poll()
        poller.register(pid_fd, select.POLLIN)
        if not poller.poll(max(0, round(timeout * 1000))):
            signal.pidfd_send_signal(pid_fd, signal.SIGKILL)
            poller.poll(5000)
        _reap_if_child(identity.pid)
        return True
    except ProcessLookupError:
        _reap_if_child(identity.pid)
        return False
    finally:
        os.close(pid_fd)


def _create_receipt(path: Path, identity: ProcessIdentity) -> None:
    data = (json.dumps(identity.to_json(), sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".process-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise InstallError(
                f"Process receipt appeared during launch: {path}"
            ) from error
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def _reap_if_child(pid: int) -> None:
    process = _spawned_processes.pop(pid, None)
    if process is not None:
        try:
            process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            _spawned_processes[pid] = process
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _prune_spawned_processes() -> None:
    for pid, process in tuple(_spawned_processes.items()):
        if process.poll() is not None:
            _spawned_processes.pop(pid, None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    spawn = commands.add_parser("spawn", allow_abbrev=False)
    spawn.add_argument("--receipt", type=Path, required=True)
    spawn.add_argument("--log", type=Path, required=True)
    spawn.add_argument("--cwd", type=Path, required=True)
    spawn.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    previous_handlers = {
        selected: signal.getsignal(selected)
        for selected in (signal.SIGTERM, signal.SIGHUP)
    }

    def interrupt(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        raise _HelperInterrupted

    for selected in previous_handlers:
        signal.signal(selected, interrupt)
    try:
        identity = spawn_detached(
            arguments.receipt,
            arguments.log,
            command,
            cwd=arguments.cwd,
        )
    except (_HelperInterrupted, KeyboardInterrupt):
        print("Detached launch interrupted before completion", file=sys.stderr)
        return 130
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        for selected, previous in previous_handlers.items():
            signal.signal(selected, previous)
    print(json.dumps(identity.to_json(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
