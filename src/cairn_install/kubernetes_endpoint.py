"""Bounded port-forward ownership, readiness and conservative process recovery."""

from __future__ import annotations

import os
import selectors
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from cairn_install.core import (
    Context,
    InstallError,
    defer_spawn_signals,
    owned_child_running,
    stop_command_group,
    stop_process_group,
)


class EndpointProcess:
    def __init__(
        self,
        ctx: Context,
        record: dict[str, Any],
        *,
        local_port: int | None = None,
        remote_port: int = 8000,
    ) -> None:
        self.ctx = ctx
        self.record = record
        self._local_port = local_port
        self.remote_port = remote_port
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def local_port(self) -> int:
        return self.ctx.port if self._local_port is None else self._local_port

    def open(self, argv: list[str]) -> None:
        self.close()
        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", self.local_port))
            except OSError as error:
                raise InstallError(
                    "Local port-forward port is already occupied"
                ) from error
        self.ctx.note("$ " + shlex.join(argv))
        try:
            with defer_spawn_signals() as cancelled:
                self.process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            if cancelled:
                raise KeyboardInterrupt
            process = self.process
            fields = (
                Path("/proc/" + str(process.pid) + "/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()
            )
            self.record["endpoint"] = {
                "pid": process.pid,
                "start_time": fields[19],
                "cmdline": Path("/proc/" + str(process.pid) + "/cmdline")
                .read_bytes()
                .hex(),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "argv": argv,
            }
            self.ctx.save()
            assert process.stdout is not None
            expected = (
                "Forwarding from 127.0.0.1:"
                + str(self.local_port)
                + " -> "
                + str(self.remote_port)
            ).encode()
            output = b""
            deadline = time.monotonic() + 20
            with selectors.DefaultSelector() as poller:
                poller.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not owned_child_running(process):
                        raise InstallError(
                            "kubectl port-forward exited before readiness"
                        )
                    if poller.select(timeout=0.1):
                        output += os.read(process.stdout.fileno(), 4096)
                        if len(output) > 65536:
                            raise InstallError(
                                "kubectl port-forward exceeded output limit"
                            )
                        if expected in output:
                            with socket.create_connection(
                                ("127.0.0.1", self.local_port), timeout=2
                            ):
                                # A script launcher can still be crossing the
                                # shebang exec boundary when Popen returns.
                                # Readiness proves the final command is now
                                # running, so journal that stable identity for
                                # safe recovery by a resumed installer.
                                stable_fields = (
                                    Path("/proc/" + str(process.pid) + "/stat")
                                    .read_text()
                                    .rsplit(")", 1)[1]
                                    .split()
                                )
                                endpoint = self.record["endpoint"]
                                endpoint["start_time"] = stable_fields[19]
                                endpoint["cmdline"] = (
                                    Path("/proc/" + str(process.pid) + "/cmdline")
                                    .read_bytes()
                                    .hex()
                                )
                                self.ctx.save()
                                return
            raise InstallError(
                "kubectl port-forward did not become ready within 20 seconds"
            )
        except (OSError, KeyboardInterrupt, InstallError):
            self.close()
            raise InstallError(
                "kubectl port-forward failed; its process was stopped"
            ) from None

    def close(self) -> None:
        if self.process is not None:
            if self.process.returncode is None:
                with defer_spawn_signals():
                    stop_command_group(self.process)
            else:
                # poll()/wait() reaps the leader. Its original identity can no
                # longer authorise a signal because the process-group ID may
                # have been reused; clear only after proving the group absent.
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    raise InstallError(
                        "Cannot verify reaped endpoint process group identity"
                    ) from error
                else:
                    raise InstallError(
                        "Endpoint process group survives its reaped leader; identity cannot be verified"
                    )
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None
        elif self.record.get("endpoint"):
            record = self.record["endpoint"]
            pid = record.get("pid")
            if type(pid) is not int or pid <= 1:
                raise InstallError("Recorded port-forward process identity is invalid")
            process = Path("/proc") / str(pid)
            try:
                stat = (process / "stat").read_text().rsplit(")", 1)[1].split()
                boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                if (
                    record.get("boot_id") != boot
                    or record.get("start_time") != stat[19]
                    or record.get("cmdline") != (process / "cmdline").read_bytes().hex()
                    or process.stat().st_uid != os.getuid()
                    or os.getpgid(pid) != pid
                ):
                    raise InstallError(
                        "Recorded port-forward process identity changed; refusing to signal"
                    )
                with defer_spawn_signals():
                    stop_process_group(pid)
            except (FileNotFoundError, ProcessLookupError):
                # A dead/reaped leader does not imply that its group is empty.
                # With no leader identity, a surviving group cannot be safely
                # attributed to this endpoint: retain the journal and refuse.
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    raise InstallError(
                        "Cannot verify recorded endpoint process group identity"
                    ) from error
                else:
                    raise InstallError(
                        "Recorded endpoint process group survives its leader; identity cannot be verified"
                    )
        if "endpoint" in self.record:
            self.record.pop("endpoint")
            self.ctx.save()
