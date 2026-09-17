"""Parent-owned disposable process groups; durable state never authorises a PID."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

from .core import (
    Context,
    InstallError,
    defer_spawn_signals,
    owned_child_running,
    stop_command_group,
)

RESOURCE = "native_foreground"


class ForegroundProcess:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.process: subprocess.Popen[bytes] | None = None

    def validate(self) -> None:
        if any(
            key in self.ctx.state["resources"]
            for key in ("native_process", "native_process_intent")
        ):
            raise InstallError(
                "Cannot adopt a detached process in foreground mode; use a new instance"
            )
        receipt = self.ctx.state["resources"].get(RESOURCE)
        if receipt is None:
            return
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"generation", "status"}
            or type(receipt["generation"]) is not int
            or receipt["generation"] <= 0
            or receipt["status"] not in {"spawning", "running", "stopped"}
        ):
            raise InstallError("Foreground lifecycle state is malformed")
        if receipt["status"] != "stopped" and self.process is None:
            raise InstallError(
                "Foreground process was active without this parent handle; refusing adoption or signalling"
            )

    def running(self) -> bool:
        self.validate()
        return self.process is not None and owned_child_running(self.process)

    def start(self, argv: list[str], log: Path) -> None:
        self.validate()
        if self.process is not None:
            if self.running():
                return
            self.stop()
        generation = (
            self.ctx.state["resources"].get(RESOURCE, {}).get("generation", 0) + 1
        )
        receipt = {"generation": generation, "status": "spawning"}
        self.ctx.state["resources"][RESOURCE] = receipt
        self.ctx.save()
        log_fd = -1
        try:
            try:
                log_fd = os.open(
                    log,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                    0o600,
                )
            except OSError as error:
                raise InstallError(
                    "Cannot safely open foreground service log"
                ) from error
            info = os.fstat(log_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise InstallError(
                    "Foreground service log must be an owned private regular file"
                )
            environment = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")
                if key in os.environ
            }
            environment["PYTHONUNBUFFERED"] = "1"
            with defer_spawn_signals() as cancelled:
                self.process = subprocess.Popen(
                    argv,
                    cwd=self.ctx.root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=log_fd,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(self.ctx.lock_fd,),
                )
            if cancelled:
                raise KeyboardInterrupt
            receipt["status"] = "running"
            self.ctx.save()
        except BaseException:
            if self.process is not None:
                self.stop()
            else:
                receipt["status"] = "stopped"
                self.ctx.save()
            raise
        finally:
            if log_fd >= 0:
                os.close(log_fd)

    def stop(self) -> None:
        self.validate()
        if self.process is None:
            return
        with defer_spawn_signals():
            stop_command_group(self.process)
            self.process = None
            self.ctx.state["resources"][RESOURCE]["status"] = "stopped"
            self.ctx.save()

    def close(self) -> None:
        # Refusing stale state must not become authority to stop an unknown child.
        if self.process is not None:
            self.stop()

    def wait(self) -> None:
        while self.running():
            time.sleep(0.1)
        raise InstallError("Foreground Cairn exited unexpectedly")
