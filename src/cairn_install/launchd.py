"""Owned current-user LaunchAgents with recoverable service and data teardown."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import plistlib
import pwd
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from .core import Context, InstallError, read_owned

RESOURCE = "native_launch_agent"
INTENT = "native_launch_agent_intent"
SCHEMA = "cairn.install.launch-agent/v1"
_LIMIT = 1024 * 1024
# The wrapper remains inside Context.command's owned process group. Its children
# never start a new session. Return stderr/status explicitly rather than treating
# an arbitrary empty stdout as proof that a launchd service does not exist.
_CAPTURE = r"""
import json, os, subprocess, sys, tempfile, time
with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
    p = subprocess.Popen(['/bin/launchctl', *sys.argv[2:]], stdout=out, stderr=err,
        stdin=subprocess.DEVNULL, env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
    try:
        deadline = time.monotonic() + float(sys.argv[1])
        while p.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError('launchctl timed out')
            if max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > 1048576:
                raise RuntimeError('launchctl output exceeded limit')
            time.sleep(.05)
        out.seek(0); err.seek(0)
        stdout = out.read(1048577); stderr = err.read(1048577)
        if max(len(stdout), len(stderr)) > 1048576:
            raise RuntimeError('launchctl output exceeded limit')
        print(json.dumps({'code':p.returncode,'stdout':stdout.decode(),'stderr':stderr.decode()}))
    finally:
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait(timeout=2)
"""
_BLOCKS = {
    "arguments",
    "inherited environment",
    "default environment",
    "environment",
    "resource coalition",
    "jetsam coalition",
}

# Diagnostic scalar names observed in the Intel and ARM macOS 26 fixtures.
_SCALARS = {
    "jetsam thread limit",
    "proxy started suspended",
    "jetsam memory limit (inactive)",
    "stderr path",
    "path",
    "exit timeout",
    "execs",
    "started suspended",
    "working directory",
    "cpumon",
    "last exit code",
    "type",
    "jetsam memory limit (active)",
    "runs",
    "spawn type",
    "asid",
    "domain",
    "checked allocations flags",
    "forks",
    "jetsamproperties category",
    "properties",
    "trampolined",
    "jetsam priority",
    "active count",
    "checked allocations",
    "program",
    "minimum runtime",
    "checked allocations reason",
    "state",
    "umask",
    "initialized",
    "pid",
    "stdout path",
    "immediate reason",
}


def account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def parse_print(text: str) -> tuple[str, dict[str, str | list[str]]]:
    """Parse only the bounded macOS 26 layout captured in our real fixtures."""
    if len(text.encode()) > _LIMIT or "\r" in text or "\x00" in text:
        raise InstallError("Invalid launchctl print output")
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].endswith(" = {") or lines[-1] != "}":
        raise InstallError("Incomplete launchctl print output")
    header = lines[0][:-4]
    fields: dict[str, str | list[str]] = {}
    block: str | None = None
    for line in lines[1:-1]:
        if not line:
            continue
        if block is not None:
            if line == "\t}":
                block = None
                continue
            if not line.startswith("\t\t") or line.startswith("\t\t\t"):
                raise InstallError("Ambiguous launchctl nested output")
            value = line[2:]
            if not value or any(c in value for c in "{}\\"):
                raise InstallError("Unsupported launchctl escaping or nested block")
            values = fields[block]
            assert isinstance(values, list)
            values.append(value)
            continue
        if not line.startswith("\t") or line.startswith("\t\t"):
            raise InstallError("Ambiguous launchctl field indentation")
        key, separator, value = line[1:].partition(" = ")
        if not separator or not key or key in fields:
            raise InstallError("Duplicate or invalid launchctl field")
        if value == "{":
            if key not in _BLOCKS:
                raise InstallError("Unsupported launchctl configuration block")
            fields[key] = []
            block = key
        else:
            if key not in _SCALARS:
                raise InstallError(f"Unsupported launchctl field: {key}")
            if not value or any(c in value for c in "{}\\"):
                raise InstallError("Unsupported launchctl scalar")
            fields[key] = value
    if block is not None:
        raise InstallError("Incomplete launchctl configuration block")
    return header, fields


class DarwinLaunchAgent:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.uid = os.getuid()
        self.home = account_home()
        self.root = ctx.root.resolve()
        self.label = f"invalid.example.cairn.{UUID(ctx.instance_id)}"
        self.domain = f"gui/{self.uid}"
        self.target = f"{self.domain}/{self.label}"
        self.path = self.home / "Library" / "LaunchAgents" / f"{self.label}.plist"
        self.log = self.root / "service.log"
        self.argv = [
            "/usr/bin/env",
            "-i",
            f"HOME={self.home}",
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONNOUSERSITE=1",
            "PYTHONUNBUFFERED=1",
            str(self.root / "runtime/bin/python"),
            str(self.root / "runtime/bin/cairn"),
            "serve",
            "--config",
            str(self.root / "config.yaml"),
        ]
        if any(any(c in value for c in "\n\r\t\x00{}\\") for value in self.argv):
            raise InstallError(
                "LaunchAgent paths contain unsupported control or escape characters"
            )
        profile = {
            "Label": self.label,
            "Program": self.argv[0],
            "ProgramArguments": self.argv,
            "WorkingDirectory": str(self.root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "Umask": 63,
            "ExitTimeOut": 60,
            "StandardOutPath": str(self.log),
            "StandardErrorPath": str(self.log),
        }
        self.contents = plistlib.dumps(profile, sort_keys=True).decode()
        self.identity: dict[str, Any] = {
            "schema": SCHEMA,
            "uid": self.uid,
            "domain": self.domain,
            "label": self.label,
            "path": str(self.path),
            "plist_sha256": hashlib.sha256(self.contents.encode()).hexdigest(),
        }
        self._lease: int | None = None
        self._observation_deadline: float | None = None

    def _command(self, *args: str) -> dict[str, Any]:
        timeout = 80.0
        if self._observation_deadline is not None:
            timeout = min(timeout, self._observation_deadline - time.monotonic())
            if timeout <= 0:
                raise InstallError(
                    "LaunchAgent remains loaded after bootout", "cleanup_failed"
                )
        try:
            raw = self.ctx.command(
                [sys.executable, "-c", _CAPTURE, str(min(70.0, timeout)), *args],
                cwd=self.ctx.directory,
                timeout=timeout,
                private=True,
            )
        except InstallError as error:
            if self._observation_deadline is not None:
                raise InstallError(
                    "Cannot complete LaunchAgent shutdown observation", "cleanup_failed"
                ) from error
            raise
        try:
            result = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise InstallError("Invalid launchctl command envelope") from error
        if (
            not isinstance(result, dict)
            or set(result) != {"code", "stdout", "stderr"}
            or type(result["code"]) is not int
            or not isinstance(result["stdout"], str)
            or not isinstance(result["stderr"], str)
            or max(len(result["stdout"].encode()), len(result["stderr"].encode()))
            > _LIMIT
        ):
            raise InstallError("Invalid launchctl command result")
        return result

    def _domain(self) -> None:
        result = self._command("print", self.domain)
        if result["code"] != 0 or not result["stdout"].strip() or result["stderr"]:
            raise InstallError("A current-user macOS GUI launchd domain is required")

    def _observe(self) -> dict[str, str | list[str]] | None:
        self._domain()
        result = self._command("print", self.target)
        if result == {
            "code": 113,
            "stdout": "",
            "stderr": f'Bad request.\nCould not find service "{self.label}" in domain for user gui: {self.uid}\n',
        }:
            self._domain()
            return None
        if result["code"] != 0 or result["stderr"]:
            raise InstallError("Cannot establish loaded LaunchAgent identity")
        header, fields = parse_print(result["stdout"])
        expected: dict[str, str | list[str]] = {
            "path": str(self.path),
            "type": "LaunchAgent",
            "program": self.argv[0],
            "arguments": self.argv,
            "working directory": str(self.root),
            "stdout path": str(self.log),
            "stderr path": str(self.log),
            "umask": "77",
            "exit timeout": "60",
            "minimum runtime": "10",
            "spawn type": "daemon (3)",
        }
        if header != self.target:
            raise InstallError("Refusing foreign loaded LaunchAgent target")
        for key, value in expected.items():
            if fields.get(key) != value:
                raise InstallError(f"Refusing foreign loaded LaunchAgent field: {key}")
        environment = fields.get("environment")
        if not isinstance(environment, list) or sorted(environment) != sorted(
            ["OSLogRateLimit => 64", f"XPC_SERVICE_NAME => {self.label}"]
        ):
            raise InstallError("Refusing foreign loaded LaunchAgent environment")
        domain = fields.get("domain")
        properties = fields.get("properties")
        if (
            not isinstance(domain, str)
            or re.fullmatch(re.escape(self.domain) + r" \[[0-9]+\]", domain) is None
            or not isinstance(properties, str)
        ):
            raise InstallError("Invalid loaded LaunchAgent domain or properties")
        flags = properties.split(" | ")
        if (
            len(flags) != len(set(flags))
            or not {"keepalive", "runatload"} <= set(flags)
            or set(flags)
            - {
                "keepalive",
                "runatload",
                "inferred program",
                "system service",
                "tle system",
            }
        ):
            raise InstallError("Unexpected LaunchAgent restart properties")
        return fields

    def _receipt(self) -> dict[str, Any] | None:
        resources = self.ctx.state["resources"]
        receipt = resources.get(RESOURCE)
        attempted = resources.get("native_launch_agent_bootstrap_attempted")
        if attempted is not None and attempted is not True:
            raise InstallError("Invalid LaunchAgent bootstrap history")
        if receipt is not None and (
            not isinstance(receipt, dict)
            or set(receipt) != set(self.identity) | {"status"}
            or any(
                receipt.get(k) != v or type(receipt.get(k)) is not type(v)
                for k, v in self.identity.items()
            )
            or receipt.get("status")
            not in {"materialised", "loaded", "stopped", "removed"}
        ):
            raise InstallError("LaunchAgent receipt does not match this installation")
        intent = resources.get(INTENT)
        if intent is not None and (
            not isinstance(intent, dict)
            or set(intent) != set(self.identity) | {"operation", "phase"}
            or any(intent.get(k) != v for k, v in self.identity.items())
            or (intent.get("operation"), intent.get("phase"))
            not in {
                ("start", "publish_pending"),
                ("start", "bootstrap_pending"),
                ("stop", "bootout_pending"),
                ("remove", "bootout_pending"),
                ("remove", "unlink_pending"),
            }
        ):
            raise InstallError("LaunchAgent lifecycle intent is malformed")
        return receipt

    def _intent(self, operation: str, phase: str) -> None:
        self.ctx.state["resources"][INTENT] = {
            **self.identity,
            "operation": operation,
            "phase": phase,
        }
        self.ctx.save()

    def _status(self, status: str) -> None:
        self.ctx.state["resources"][RESOURCE] = {**self.identity, "status": status}
        self.ctx.save()

    def _parents(self, *, create: bool = False) -> None:
        # secure_directory validates shape, but launchd publication also needs
        # ownership and write-permission checks on each account-local parent.
        for parent in (self.home, self.home / "Library", self.path.parent):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                if not create:
                    return
                try:
                    parent.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise InstallError(f"Unsafe LaunchAgent parent: {parent}")

    def _file(self) -> bool:
        self._parents()
        if not self.path.exists() and not self.path.is_symlink():
            return False
        self.ctx.check_file(self.path)
        if (
            read_owned(self.path) != self.contents.encode()
            or stat.S_IMODE(self.path.lstat().st_mode) != 0o644
        ):
            raise InstallError("Owned LaunchAgent plist changed")
        return True

    def validate_ownership(self) -> None:
        receipt = self._receipt()
        exists = self._file()
        observed = self._observe()
        intent = self.ctx.state["resources"].get(INTENT)
        if observed is not None and (
            not exists or (receipt is None and intent is None)
        ):
            raise InstallError("Refusing unowned loaded LaunchAgent")
        if receipt is not None:
            if receipt["status"] == "removed" and (exists or observed is not None):
                raise InstallError("Removed LaunchAgent was replaced")
            if (
                not exists
                and receipt["status"] != "removed"
                and not (
                    intent is not None
                    and intent["operation"] == "remove"
                    and intent["phase"] == "unlink_pending"
                )
            ):
                raise InstallError("Owned LaunchAgent plist disappeared")

    def preflight(self) -> None:
        if os.geteuid() == 0:
            raise InstallError("LaunchAgents require the current non-root user")
        manager = self._command("manageruid")
        if manager != {"code": 0, "stdout": f"{self.uid}\n", "stderr": ""}:
            raise InstallError("launchd manager does not match current user")
        self.validate_ownership()

    def is_running(self) -> bool:
        self.validate_ownership()
        return self._observe() is not None

    def _log(self) -> None:
        fd = -1
        try:
            fd = os.open(
                self.log,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.uid
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise InstallError(
                    "LaunchAgent log must be an owned private regular file"
                )
        except OSError as error:
            raise InstallError("Cannot safely open LaunchAgent log") from error
        finally:
            if fd >= 0:
                os.close(fd)

    def start(self) -> None:
        self.close()
        self.validate_ownership()
        if self._observe() is not None:
            self._status("loaded")
            return
        self._log()
        self._parents(create=True)
        self._intent("start", "publish_pending")
        self.ctx.write_file(self.path, self.contents, mode=0o644)
        self._status("materialised")
        self.ctx.command(
            ["/usr/bin/plutil", "-lint", str(self.path)], cwd=self.ctx.directory
        )
        self.ctx.state["resources"]["native_launch_agent_bootstrap_attempted"] = True
        self._intent("start", "bootstrap_pending")
        self._file()
        if self._observe() is not None:
            raise InstallError("LaunchAgent appeared before bootstrap")
        result = self._command("bootstrap", self.domain, str(self.path))
        if result["code"] != 0:
            raise InstallError(
                "LaunchAgent bootstrap failed; preserve state and inspect launchd policy"
            )
        if self._observe() is None:
            raise InstallError("LaunchAgent did not remain loaded after bootstrap")
        self._status("loaded")

    def _partial_deletion(self) -> bool:
        journal = self.ctx.directory.parent / f".{self.ctx.name}.blitz.json"
        if not journal.exists() and not journal.is_symlink():
            return False
        try:
            saved = json.loads(read_owned(journal))
        except (ValueError, OSError) as error:
            raise InstallError(
                "Invalid LaunchAgent deletion recovery journal"
            ) from error
        return (
            isinstance(saved, dict)
            and saved.get("schema") == 1
            and saved.get("owner_uid") == self.uid
            and saved.get("name") == self.ctx.name
            and saved.get("instance_id") == self.ctx.instance_id
            and saved.get("run_id") == self.ctx.state["run_id"]
            and saved.get("status") == "blitzing"
            and saved.get("blitz_phase") == "resources_removed"
            and isinstance(saved.get("resources"), dict)
            and saved["resources"].get(RESOURCE)
            == {**self.identity, "status": "removed"}
        )

    def _quiesce(self, *, removal: bool, never_started: bool) -> None:
        if self._lease is not None:
            return
        path = self.root / "data/.cairn-instance.lock"
        deadline = time.monotonic() + 65
        while True:
            fd = -1
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self.uid
                    or stat.S_IMODE(info.st_mode) != 0o660
                ):
                    raise InstallError("Unsafe Cairn data lease")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # The runtime truncates then writes while holding this flock.
                # Read identity only after acquisition, never during that window.
                if os.read(fd, 128) != f"{self.ctx.instance_id}\n".encode():
                    raise InstallError("Cairn data lease identity changed")
                actual = path.lstat()
                if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                    raise InstallError("Cairn data lease pathname changed")
                self._lease = fd
                return
            except FileNotFoundError as error:
                if fd < 0 and (never_started or (removal and self._partial_deletion())):
                    return
                raise InstallError("Recorded Cairn data lease disappeared") from error
            except OSError as error:
                if (
                    error.errno not in {errno.EAGAIN, errno.EACCES}
                    or time.monotonic() >= deadline
                ):
                    raise InstallError(
                        "Cairn data lease did not become available", "cleanup_failed"
                    ) from error
            finally:
                if fd >= 0 and self._lease != fd:
                    os.close(fd)
            time.sleep(0.05)

    def _unload(self, *, removal: bool) -> bool:
        self.validate_ownership()
        receipt = self._receipt()
        intent = self.ctx.state["resources"].get(INTENT)
        observed = self._observe()
        never_started = (
            observed is None
            and self.ctx.state["resources"].get(
                "native_launch_agent_bootstrap_attempted"
            )
            is not True
            and (receipt is None or receipt["status"] in {"materialised", "removed"})
            and (
                intent is None
                or intent["phase"] in {"publish_pending", "unlink_pending"}
            )
        )
        if observed is not None:
            # A login may have loaded the plist before our bootstrap receipt.
            # Persist that fact with the bootout intent before removing the job.
            self.ctx.state["resources"]["native_launch_agent_bootstrap_attempted"] = (
                True
            )
            self._intent("remove" if removal else "stop", "bootout_pending")
            self._file()
            self._observe()
            result = self._command("bootout", self.target)
            if result["code"] != 0:
                raise InstallError("LaunchAgent bootout failed; state retained")
        self._observation_deadline = time.monotonic() + 65
        try:
            while self._observe() is not None:
                if time.monotonic() >= self._observation_deadline:
                    raise InstallError(
                        "LaunchAgent remains loaded after bootout", "cleanup_failed"
                    )
                time.sleep(0.1)
        finally:
            self._observation_deadline = None
        return never_started

    def stop(self) -> None:
        never_started = self._unload(removal=False)
        try:
            self._quiesce(removal=False, never_started=never_started)
            if self._receipt() is not None:
                self._status("stopped")
        finally:
            self.close()

    def restart(self) -> None:
        self.stop()
        self.start()

    def remove(self) -> None:
        never_started = self._unload(removal=True)
        if self.path.exists() or self.path.is_symlink():
            self._intent("remove", "unlink_pending")
            self._file()
            self.path.unlink()
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._quiesce(removal=True, never_started=never_started)
        self._status("removed")

    def close(self) -> None:
        if self._lease is not None:
            descriptor, self._lease = self._lease, None
            os.close(descriptor)
