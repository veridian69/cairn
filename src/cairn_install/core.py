"""Durable run ownership, secret-free teaching output and bounded commands."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import selectors
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO
from uuid import uuid4

NAME = re.compile(r"[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?")
# uv variables that select where the runtime lives or which interpreter runs
# it; the installer owns both, so they never come from the shell.
_OWNED_UV_VARIABLES = frozenset(
    {
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
        "UV_SYSTEM_PYTHON",
        "UV_NO_SYNC",
    }
)
TOKEN = re.compile(r"cairn1\.[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}")
MAX_OUTPUT = 2 * 1024 * 1024


class InstallError(Exception):
    def __init__(self, message: str, code: str = "installation_failed") -> None:
        self.code = code
        super().__init__(message)


def sync_regular_file(fd: int) -> None:
    """Flush installer publications through Darwin's drive-cache barrier."""
    os.fsync(fd)
    if platform.system() == "Darwin":
        # Apple XNU bsd/sys/fcntl.h defines F_FULLFSYNC as 51.
        # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/fcntl.h
        try:
            fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))
        except OSError as error:
            raise InstallError(
                "macOS full file sync failed", "durability_failed"
            ) from error


@contextmanager
def defer_spawn_signals() -> Iterator[list[int]]:
    """Acquire the child handle before delivering a cancellation exception."""
    received: list[int] = []
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.getsignal(number)
            signal.signal(number, lambda signum, frame: received.append(signum))
    try:
        yield received
    finally:
        for restored_number, handler in previous.items():
            signal.signal(restored_number, handler)


def _group_has_live_members(group: int) -> bool:
    if platform.system() == "Darwin":
        return any(
            pgid == group and status != "Z"
            for pgid, status in _darwin_processes().values()
        )
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            value = (entry / "stat").read_text()
            fields = value[value.rfind(")") + 2 :].split()
            if len(fields) > 2 and int(fields[2]) == group and fields[0] != "Z":
                return True
        except (OSError, ValueError):
            continue
    return False


def _parse_darwin_processes(raw: bytes) -> dict[int, tuple[int, str]]:
    # State flags documented by Apple's adv_cmds/ps/ps.1 (including legacy A/S).
    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise InstallError(
            "Invalid Darwin process inventory", "cleanup_failed"
        ) from error
    result: dict[int, tuple[int, str]] = {}
    for line in text.splitlines():
        match = re.fullmatch(
            r"\s*([0-9]+)\s+([0-9]+)\s+([HRISTUZ?])[<>AELNSs+VWX]*\s*", line
        )
        if match is None or int(match[1]) in result:
            raise InstallError("Invalid Darwin process inventory", "cleanup_failed")
        result[int(match[1])] = (int(match[2]), match[3])
    if not result:
        raise InstallError("Empty Darwin process inventory", "cleanup_failed")
    return result


def _darwin_processes() -> dict[int, tuple[int, str]]:
    """Read bounded kernel process metadata; never execute a PATH-selected ps."""
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,pgid=,stat="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            close_fds=True,
        )
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + 3
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise InstallError(
                        "Darwin process inventory timed out", "cleanup_failed"
                    )
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                output.extend(block)
                if len(output) > 1024 * 1024:
                    raise InstallError(
                        "Darwin process inventory exceeds limit", "cleanup_failed"
                    )
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise InstallError("Darwin process inventory failed", "cleanup_failed")
        return _parse_darwin_processes(bytes(output))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(
            "Darwin process inventory unavailable", "cleanup_failed"
        ) from error
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
                process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()


def owned_child_running(process: subprocess.Popen[bytes]) -> bool:
    """Observe without reaping: the child PID must keep its group ID pinned."""
    if process.returncode is not None:
        return False
    if platform.system() == "Darwin":
        record = _darwin_processes().get(process.pid)
        return record is not None and record[1] != "Z"
    try:
        value = (Path("/proc") / str(process.pid) / "stat").read_text()
        return value[value.rfind(")") + 2 :].split()[0] != "Z"
    except FileNotFoundError:
        return False


def stop_command_group(process: subprocess.Popen[bytes]) -> None:
    """Cancel the whole command tree, including children ignoring SIGTERM."""
    with defer_spawn_signals():
        if process.returncode is not None:
            raise InstallError(
                "Cannot signal an already reaped command group", "cleanup_failed"
            )
        stop_process_group(process.pid)
        process.wait(timeout=5)


def _signal_command_group(group: int, number: int) -> None:
    try:
        os.killpg(group, number)
    except (PermissionError, ProcessLookupError) as error:
        # Darwin killpg1 excludes zombies and can return EPERM for a group
        # whose unreaped leader is its only member. Never infer death from
        # that errno: require the bounded, strict Darwin inventory to prove it.
        if platform.system() == "Darwin":
            if not _group_has_live_members(group):
                return
        elif isinstance(error, ProcessLookupError):
            return
        raise InstallError(
            "Cannot signal command group; live processes may remain",
            "cleanup_failed",
        ) from error


def stop_process_group(group: int) -> None:
    """Stop a process group whose identity the caller has already verified."""
    _signal_command_group(group, signal.SIGTERM)
    deadline = time.monotonic() + 5
    # Do not reap the leader during the grace period: its PID pins the group ID.
    while _group_has_live_members(group) and time.monotonic() < deadline:
        time.sleep(0.05)
    _signal_command_group(group, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while _group_has_live_members(group) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _group_has_live_members(group):
        raise InstallError(
            "Command group did not stop after SIGKILL; inspect the host before resuming",
            "cleanup_failed",
        )


def secure_directory(path: Path, *, private: bool = False) -> None:
    """Reject symlink traversal; only create missing directories under our UID."""
    if not path.is_absolute():
        raise InstallError(f"Use an absolute path: {path}")
    for parent in [*reversed(path.parents), path]:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InstallError(f"Not a real directory (symlinks refused): {parent}")
    info = path.stat()
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise InstallError(f"Directory must be owned by you with mode 0700: {path}")


def read_owned(path: Path, limit: int = MAX_OUTPUT) -> bytes:
    secure_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise InstallError(f"Expected an owned regular file: {path}")
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise InstallError(f"File exceeds safe size limit: {path}")
            return value
    except OSError as error:
        raise InstallError(f"Cannot safely read {path}: {error.strerror}") from error


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_owned(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            sync_regular_file(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Context:
    def __init__(
        self,
        directory: Path,
        state: dict[str, Any],
        lock: int,
        *,
        prepare_root: bool = True,
    ) -> None:
        self.directory = directory
        self.state = state
        self.root = directory / "instance"
        self.source = Path(state["source"])
        self._lock = lock
        self._secrets: set[str] = set()
        self.verbose = False
        self._display_cwd: Path | None = None
        if prepare_root:
            secure_directory(self.root, private=True)

    @property
    def name(self) -> str:
        return str(self.state["name"])

    @property
    def mode(self) -> str:
        return str(self.state["mode"])

    @property
    def port(self) -> int:
        return int(self.state["port"])

    @property
    def semantic(self) -> bool:
        return bool(self.state["semantic"])

    @property
    def instance_id(self) -> str:
        if self.mode == "garden-server":
            return str(self.state["server"]["cairn"]["instance_id"])
        return str(self.state["instance_id"])

    @property
    def run_id(self) -> str:
        return str(self.state["run_id"])

    @property
    def lock_fd(self) -> int:
        """Share this open-file-description with an owned foreground child."""
        if self._lock < 0:
            raise InstallError("Installer lock is already closed")
        return self._lock

    def __enter__(self) -> Context:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._lock >= 0:
            os.close(self._lock)
            self._lock = -1

    def save(self) -> None:
        atomic_write(
            self.directory / "state.json",
            (json.dumps(self.state, indent=2, sort_keys=True) + "\n").encode(),
        )

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = TOKEN.sub("[redacted credential]", text)
        text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[redacted provider key]", text)
        return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", text)

    def note(
        self,
        message: str,
        *,
        detail: bool = False,
        kind: str | None = None,
        terminal_message: str | None = None,
    ) -> None:
        from cairn_install.output import paint

        # Child tools may force ANSI even though capture is not a terminal.
        # Strip their styling/hyperlinks before redaction; only paint() styles
        # our terminal output, and the on-disk journal remains plain text.
        def clean_text(value: str) -> str:
            plain = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", value)
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", plain)
            return self.redact(plain)

        clean = clean_text(message)
        path = self.directory / "commands.log"
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "a") as log:
            info = os.fstat(log.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise InstallError(
                    "Command log must be an owned regular file with mode 0600"
                )
            log.write(clean + "\n")
            log.flush()
        if detail and not self.verbose:
            return
        if terminal_message is not None:
            clean = clean_text(terminal_message)
        if kind is None:
            stripped = clean.lstrip()
            if stripped.startswith("==="):
                kind = "stage"
            elif stripped.startswith("[OK]") or '"status": "verified"' in stripped:
                kind = "success"
            elif stripped.startswith("$") or stripped.startswith("Running "):
                kind = "command"
            elif stripped.startswith("Installation stopped"):
                kind = "warning"
            else:
                kind = "plain"
        print(paint(clean, kind), flush=True)

    def read_secret(self, path: Path) -> str:
        raw = read_owned(path, 8192)
        if stat.S_IMODE(path.stat().st_mode) not in (0o400, 0o600):
            raise InstallError(f"Secret file needs mode 0400 or 0600: {path}")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeError as error:
            raise InstallError(f"Secret file is not UTF-8: {path}") from error
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise InstallError(f"Secret must be one non-empty line: {path}")
        self.add_secret(value)
        return value

    def write_file(
        self, path: Path, content: str, *, mode: int = 0o600, secret: bool = False
    ) -> None:
        path = path.absolute()
        digest = hashlib.sha256(content.encode()).hexdigest()
        key = str(path)
        owned = self.state["owned_files"]
        intents = self.state.setdefault("file_intents", {})
        self._refuse_pending_replacement(path)
        if secret:
            self.add_secret(content.strip())
        if path.exists() or path.is_symlink():
            observed = hashlib.sha256(read_owned(path)).hexdigest()
            expected = owned.get(key) or intents.get(key)
            if expected is None:
                raise InstallError(f"Refusing to overwrite unowned file: {path}")
            if observed != expected or observed != digest:
                raise InstallError(f"Owned file changed; preserve and inspect: {path}")
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise InstallError(f"Owned file permissions changed: {path}")
            owned[key] = digest
            self.save()
            return
        intents[key] = digest
        self.save()
        if secret:
            self.note(
                f"Write protected credential file {shlex.quote(key)}; contents omitted.",
                detail=True,
            )
        else:
            self.note(
                f"# Write {key} (mode {mode:04o})\ncat > {shlex.quote(key)} <<'CAIRN_FILE'\n{content.rstrip()}\nCAIRN_FILE",
                detail=True,
            )
        # Link publication cannot replace a file created between inspection and publish.
        secure_directory(path.parent)
        fd, temp = tempfile.mkstemp(prefix=".create-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content.encode())
                stream.flush()
                sync_regular_file(stream.fileno())
            os.link(temp, path, follow_symlinks=False)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError as error:
            raise InstallError(f"File appeared during creation: {path}") from error
        finally:
            os.unlink(temp)
        owned[key] = digest
        self.save()

    def _refuse_pending_replacement(self, path: Path) -> None:
        if str(path.absolute()) in self.state.get("file_replacements", {}):
            raise InstallError(
                f"Owned file has an interrupted replacement; resume the operation: {path}"
            )

    def pending_replacements(self) -> dict[str, dict[str, str]]:
        return {
            key: dict(entry)
            for key, entry in self.state.get("file_replacements", {}).items()
        }

    def replace_file(
        self, path: Path, content: str, *, mode: int = 0o600, secret: bool = False
    ) -> bool:
        path = Path(os.path.normpath(path.absolute()))
        # Overwriting is only safe where no other user can substitute content
        # between inspection and publish: inside the private instance tree.
        if path == self.root or not path.is_relative_to(self.root):
            raise InstallError(
                f"Replacement target must be inside the private instance tree: {path}"
            )
        secure_directory(self.root, private=True)
        secure_directory(path.parent)
        digest = hashlib.sha256(content.encode()).hexdigest()
        key = str(path)
        owned = self.state["owned_files"]
        intents = self.state.setdefault("file_intents", {})
        if secret:
            self.add_secret(content.strip())
        pending = self.state.get("file_replacements", {}).get(key)
        if pending is not None:
            observed = hashlib.sha256(read_owned(path)).hexdigest()
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise InstallError(f"Owned file permissions changed: {path}")
            if observed == pending["new"]:
                # The journaled publish landed; make its rename durable and adopt
                # it before any new request, so a restore can follow it.
                _fsync_directory(path.parent)
                owned[key] = pending["new"]
                del self.state["file_replacements"][key]
                self.save()
                if digest == pending["new"]:
                    return True
            elif observed != pending["old"]:
                raise InstallError(
                    f"Interrupted replacement left unexpected content; preserve and inspect: {path}"
                )
            elif observed == digest:
                # Publish never landed and the old content is what is requested.
                del self.state["file_replacements"][key]
                self.save()
                return False
            # Otherwise the publish never landed; the request supersedes the intent.
            pending = None
        expected = owned.get(key) or intents.get(key)
        if expected is None:
            raise InstallError(f"Refusing to replace unowned file: {path}")
        observed = hashlib.sha256(read_owned(path)).hexdigest()
        if observed != expected:
            raise InstallError(f"Owned file changed; refusing replacement: {path}")
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise InstallError(f"Owned file permissions changed: {path}")
        if observed == digest and pending is None:
            return False
        replacements = self.state.setdefault("file_replacements", {})
        replacements[key] = {"old": observed, "new": digest}
        self.save()
        if secret:
            self.note(
                f"Replace protected credential file {shlex.quote(key)}; contents omitted.",
                detail=True,
            )
        else:
            self.note(
                f"# Replace {key} (mode {mode:04o})\ncat > {shlex.quote(key)} <<'CAIRN_FILE'\n{content.rstrip()}\nCAIRN_FILE",
                detail=True,
            )
        secure_directory(path.parent)
        fd, temp = tempfile.mkstemp(prefix=".replace-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content.encode())
                stream.flush()
                sync_regular_file(stream.fileno())
            os.replace(temp, path)
            _fsync_directory(path.parent)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        owned[key] = digest
        del self.state["file_replacements"][key]
        self.save()
        return True

    def check_file(self, path: Path) -> None:
        self._refuse_pending_replacement(path)
        key = str(path.absolute())
        expected = self.state["owned_files"].get(key) or self.state.get(
            "file_intents", {}
        ).get(key)
        if expected is None:
            raise InstallError(f"File is unowned: {path}")
        if hashlib.sha256(read_owned(path)).hexdigest() != expected:
            raise InstallError(f"Owned file changed; refusing mutation: {path}")
        if key not in self.state["owned_files"]:
            self.state["owned_files"][key] = expected
            self.save()

    def command(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        private: bool = False,
        stdout_path: Path | None = None,
        stdout_limit: int = MAX_OUTPUT,
        stdin_data: bytes | None = None,
        stdin_path: Path | None = None,
        allowed: tuple[int, ...] = (0,),
    ) -> str:
        """Run a bounded command, optionally streaming protected file input.

        stdout_limit applies only to stdout_path; stderr and returned stdout
        retain MAX_OUTPUT. stdin_path streams an owned regular file without
        symlinks, requires private=True and is exclusive with stdin_data.
        """
        if (stdin_data is not None or stdin_path is not None) and not private:
            raise InstallError("Command stdin requires private output")
        if stdin_data is not None and stdin_path is not None:
            raise InstallError(
                "Command stdin_data and stdin_path are mutually exclusive"
            )
        if stdout_path is not None and (
            type(stdout_limit) is not int or stdout_limit <= 0
        ):
            raise InstallError("Command stdout_limit must be a positive integer")
        working = cwd or self.source
        command = [str(arg) for arg in argv]
        # Children inherit the operator's proxy and index configuration
        # (mirrors, offline caches) so fetches work on proxied hosts; only the
        # variables that would redirect the installer's own resources are
        # dropped. Loopback is exempt from any proxy because every readiness
        # and identity check targets 127.0.0.1.
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CAIRN_", "COMPOSE_")) and k not in _OWNED_UV_VARIABLES
        }
        for name in ("no_proxy", "NO_PROXY"):
            environment[name] = ",".join(
                entry
                for entry in (environment.get(name, ""), "127.0.0.1", "localhost")
                if entry
            )
        environment.update(env or {})
        prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in sorted((env or {}).items())
        )
        shown = f"{prefix} {shlex.join(command)}".strip()
        if stdout_path:
            shown += " > " + shlex.quote(str(stdout_path))
        kubectl_args = iter(command[1:])
        kubectl_verb = None
        for part in kubectl_args:
            if part in {"--context", "--namespace"}:
                next(kubectl_args, None)
            elif not part.startswith("-"):
                kubectl_verb = part
                break
        diagnostic = (
            (
                Path(command[0]).name == "docker"
                and any(
                    part in {"inspect", "ps", "ls", "info", "version"}
                    for part in command[1:]
                )
            )
            or (
                Path(command[0]).name == "systemctl"
                and any(
                    part == "show" or part.startswith("is-") for part in command[1:]
                )
            )
            or (
                Path(command[0]).name == "kubectl"
                and kubectl_verb
                in {"api-resources", "auth", "config", "get", "version", "wait"}
            )
        )
        long_command = len(shown) > 200 or "\n" in shown or "-c" in command
        hidden = (diagnostic or long_command) and not self.verbose
        display_cwd = Path(os.path.abspath(working))
        display = f"$ {shown}"
        if display_cwd != self._display_cwd:
            display = f"$ cd {shlex.quote(str(display_cwd))}\n{display}"
        self.note(
            f"\n$ (cd {shlex.quote(str(working))} && {shown})",
            detail=diagnostic or long_command,
            kind="command",
            terminal_message=display,
        )
        if not hidden:
            self._display_cwd = display_cwd
        # Validate file input before creating the capture, so a refused input
        # never leaves an exclusive capture behind to block a retry.
        input_file: BinaryIO | None = None
        if stdin_path is not None:
            secure_directory(stdin_path.parent)
            try:
                input_file = os.fdopen(
                    os.open(stdin_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
                    "rb",
                )
            except OSError as error:
                raise InstallError(
                    f"Expected an owned regular stdin file: {stdin_path}"
                ) from error
            info = os.fstat(input_file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                input_file.close()
                raise InstallError(
                    f"Expected an owned regular stdin file: {stdin_path}"
                )
        capture_fd: int | None = None
        if stdout_path:
            secure_directory(stdout_path.parent)
            try:
                capture_fd = os.open(
                    stdout_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as error:
                if input_file is not None:
                    input_file.close()
                raise InstallError(
                    f"Cannot exclusively create capture {stdout_path}"
                ) from error
        try:
            with (
                tempfile.TemporaryFile() as out,
                tempfile.TemporaryFile() as err,
                tempfile.TemporaryFile() as private_input,
                ExitStack() as inputs,
            ):
                input_stream: BinaryIO = private_input
                if stdin_data is not None:
                    private_input.write(stdin_data)
                    private_input.seek(0)
                elif input_file is not None:
                    input_stream = inputs.enter_context(input_file)
                process: subprocess.Popen[bytes] | None = None
                try:
                    with defer_spawn_signals() as cancelled:
                        process = subprocess.Popen(
                            command,
                            cwd=working,
                            env=environment,
                            stdin=input_stream
                            if stdin_data is not None or stdin_path is not None
                            else subprocess.DEVNULL,
                            stdout=capture_fd if capture_fd is not None else out,
                            stderr=err,
                            start_new_session=True,
                        )
                    if cancelled:
                        raise KeyboardInterrupt
                    started = time.monotonic()
                    deadline = started + timeout
                    next_update = started + 15
                    while owned_child_running(process):
                        if time.monotonic() >= next_update:
                            self.note(
                                f"# Command still running ({int(time.monotonic() - started)} seconds)."
                            )
                            next_update = time.monotonic() + 15
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(command, timeout)
                        output_fd = (
                            capture_fd if capture_fd is not None else out.fileno()
                        )
                        output_limit = (
                            stdout_limit if capture_fd is not None else MAX_OUTPUT
                        )
                        if (
                            os.fstat(err.fileno()).st_size > MAX_OUTPUT
                            or os.fstat(output_fd).st_size > output_limit
                        ):
                            raise InstallError(
                                "Command output exceeded the safe capture limit"
                            )
                        time.sleep(0.2)
                except (
                    subprocess.TimeoutExpired,
                    KeyboardInterrupt,
                    InstallError,
                ) as error:
                    if process is not None:
                        with defer_spawn_signals():
                            stop_command_group(process)
                    if isinstance(error, InstallError):
                        raise
                    raise InstallError(
                        "Command interrupted or timed out; state retained. Use resume.",
                        "interrupted",
                    ) from None
                # Cancellation cleanup must retain an unreaped leader. Once the
                # command has finished, reap under deferred signals outside that
                # cleanup region; its numeric process group is no longer ours.
                with defer_spawn_signals() as cancelled:
                    code = process.wait()
                if cancelled:
                    raise InstallError(
                        "Command interrupted at completion; state retained. Use resume.",
                        "interrupted",
                    )
                if capture_fd is not None:
                    if os.fstat(capture_fd).st_size > stdout_limit:
                        raise InstallError(
                            "Command output exceeded limit; inspect the stage before resuming."
                        )
                    os.fsync(capture_fd)
                    output = ""
                else:
                    out.seek(0)
                    output = out.read(MAX_OUTPUT + 1).decode(errors="replace")
                err.seek(0)
                errors = err.read(MAX_OUTPUT + 1).decode(errors="replace")
                if (
                    len(output.encode()) > MAX_OUTPUT
                    or len(errors.encode()) > MAX_OUTPUT
                ):
                    raise InstallError(
                        "Command output exceeded limit; inspect the stage before resuming."
                    )
                if not private:
                    if output:
                        self.note(output.rstrip(), detail=True, kind="plain")
                    if errors:
                        self.note(errors.rstrip(), detail=True, kind="plain")
                else:
                    self.note(
                        "Protected command output omitted from public log.", detail=True
                    )
                self.note(f"# exit {code}", detail=True)
                if code not in allowed:
                    if private or not self.verbose:
                        # A private command never shows stdout; its redacted
                        # stderr tail is the only diagnostic a tester gets.
                        # File input is arbitrary data redact() cannot know,
                        # so a command reading it shows no stderr at all.
                        excerpt = (
                            ""
                            if stdin_path is not None
                            else "\n".join(
                                self.redact(
                                    errors if private else errors or output
                                ).splitlines()[-8:]
                            )[-1600:]
                        )
                        self.note(
                            f"Command failed (exit {code})."
                            + (f"\n{excerpt}" if excerpt else ""),
                            kind="error",
                        )
                    raise InstallError(f"Command failed with exit {code}")
                return output
        except OSError as error:
            raise InstallError(
                f"Could not execute {command[0]}: {error.strerror}"
            ) from error
        finally:
            if input_file is not None:
                input_file.close()
            if capture_fd is not None:
                os.close(capture_fd)


@contextmanager
def state_root_guard(state_root: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Serialize final deletion against opening/recreating an instance directory."""
    secure_directory(state_root.absolute(), private=True)
    fd = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(
                fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError as error:
            raise InstallError(
                "Installer state cleanup is busy; retry shortly"
            ) from error
        yield
    finally:
        os.close(fd)


def open_context(
    state_root: Path, name: str, *, create: dict[str, Any] | None = None
) -> Context:
    with state_root_guard(state_root):
        return _open_context_locked(state_root, name, create=create)


def open_read_context(state_root: Path, name: str) -> Context:
    """Open immutable recorded state under a shared lock without touching disk."""
    if not NAME.fullmatch(name):
        raise InstallError(
            "Name needs 1–40 lowercase letters/digits/hyphens, beginning with a letter."
        )
    root = state_root.absolute()
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise InstallError(
            f"No recorded installation named {name} in {root}"
        ) from error
    lock = -1
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise InstallError(f"Directory must be owned by you with mode 0700: {root}")
        try:
            fcntl.flock(root_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(
                "Installer state cleanup is busy; retry shortly"
            ) from error
        directory = root / name
        journal = root / f".{name}.blitz.json"
        recovery: dict[str, Any] | None = None
        if journal.exists() or journal.is_symlink():
            if stat.S_IMODE(journal.lstat().st_mode) != 0o600:
                raise InstallError("Invalid blitz journal permissions")
            value = json.loads(read_owned(journal))
            if (
                not isinstance(value, dict)
                or value.get("schema") != 1
                or value.get("owner_uid") != os.getuid()
                or value.get("name") != name
                or value.get("status") != "blitzing"
                or value.get("blitz_phase") != "resources_removed"
            ):
                raise InstallError("Invalid blitz recovery journal")
            recovery = value
        if recovery is not None:
            # Final deletion is serialised on the state-root directory. The
            # instance lock may already have been removed by the interrupted
            # rmtree, so do not require or recreate it for read-only status.
            lock = os.dup(root_fd)
            if directory.exists() or directory.is_symlink():
                directory_fd = os.open(
                    directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    info = os.fstat(directory_fd)
                    if (
                        info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                    ):
                        raise InstallError(
                            f"Directory must be owned by you with mode 0700: {directory}"
                        )
                finally:
                    os.close(directory_fd)
                path = directory / "state.json"
                if path.exists() or path.is_symlink():
                    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                        raise InstallError(f"State must have mode 0600: {path}")
                    remaining = json.loads(read_owned(path))
                    if not isinstance(remaining, dict) or any(
                        remaining.get(key) != recovery.get(key)
                        for key in ("name", "run_id", "instance_id", "owner_uid")
                    ):
                        raise InstallError("Blitz journal and surviving state disagree")
        elif directory.exists() or directory.is_symlink():
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                info = os.fstat(directory_fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise InstallError(
                        f"Directory must be owned by you with mode 0700: {directory}"
                    )
                lock = os.open(
                    "installer.lock",
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            finally:
                os.close(directory_fd)
            lock_info = os.fstat(lock)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600
            ):
                raise InstallError(
                    "Installer lock must be an owned regular file with mode 0600"
                )
        else:
            raise InstallError(f"No recorded installation named {name} in {root}")
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(
                "another installer is using this instance; wait for it to finish"
            ) from error
        path = directory / "state.json"
        if recovery is not None:
            state = recovery
        else:
            if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                raise InstallError(f"State must have mode 0600: {path}")
            state = json.loads(read_owned(path))
            if (
                not isinstance(state, dict)
                or state.get("schema") != 1
                or state.get("owner_uid") != os.getuid()
                or state.get("name") != name
            ):
                raise InstallError("Unsupported or foreign installer state")
        context = Context(directory, state, lock, prepare_root=False)
        lock = -1
        return context
    except (OSError, ValueError) as error:
        raise InstallError("Cannot safely read recorded installer state") from error
    finally:
        if lock >= 0:
            os.close(lock)
        os.close(root_fd)


def _open_context_locked(
    state_root: Path, name: str, *, create: dict[str, Any] | None = None
) -> Context:
    if not NAME.fullmatch(name):
        raise InstallError(
            "Name needs 1–40 lowercase letters/digits/hyphens, beginning with a letter."
        )
    secure_directory(state_root.absolute(), private=True)
    directory = state_root.absolute() / name
    journal = state_root.absolute() / f".{name}.blitz.json"
    recovery = None
    if journal.exists() or journal.is_symlink():
        if create is not None:
            raise InstallError(
                "This name has an unfinished blitz; run blitz again first"
            )
        if stat.S_IMODE(journal.lstat().st_mode) != 0o600:
            raise InstallError("Invalid blitz journal permissions")
        recovery = json.loads(read_owned(journal))
        if (
            not isinstance(recovery, dict)
            or recovery.get("schema") != 1
            or recovery.get("owner_uid") != os.getuid()
            or recovery.get("name") != name
            or recovery.get("status") != "blitzing"
            or recovery.get("blitz_phase") != "resources_removed"
        ):
            raise InstallError("Invalid blitz recovery journal")
    if not directory.exists() and create is None and recovery is None:
        raise InstallError(f"No recorded installation named {name} in {state_root}")
    secure_directory(directory, private=True)
    lock = os.open(
        directory / "installer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    info = os.fstat(lock)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(lock)
        raise InstallError(
            "Installer lock must be an owned regular file with mode 0600"
        )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock)
        raise InstallError(
            "another installer is using this instance; wait for it to finish"
        ) from error
    try:
        path = directory / "state.json"
        if recovery is not None:
            if path.exists() or path.is_symlink():
                remaining = json.loads(read_owned(path))
                if not isinstance(remaining, dict) or any(
                    remaining.get(key) != recovery.get(key)
                    for key in ("name", "run_id", "instance_id", "owner_uid")
                ):
                    raise InstallError("Blitz journal and surviving state disagree")
            state = recovery
        elif path.exists() or path.is_symlink():
            if stat.S_IMODE(path.lstat().st_mode) != 0o600:
                raise InstallError(f"State must have mode 0600: {path}")
            state = json.loads(read_owned(path))
            if (
                not isinstance(state, dict)
                or state.get("schema") != 1
                or state.get("owner_uid") != os.getuid()
                or state.get("name") != name
            ):
                raise InstallError("Unsupported or foreign installer state")
        elif create is not None:
            state = dict(create)
            state.update(
                schema=1,
                owner_uid=os.getuid(),
                name=name,
                run_id=str(uuid4()),
                instance_id=str(uuid4()),
                status="planned",
                steps={},
                owned_files={},
                resources={},
                receipts={},
            )
        else:
            raise InstallError("Run record missing; cannot safely adopt existing files")
        ctx = Context(directory, state, lock)
        ctx.save()
        return ctx
    except BaseException:
        os.close(lock)
        raise
