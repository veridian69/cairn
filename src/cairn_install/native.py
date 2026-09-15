"""Disposable and persistent native installer backends."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import socket
import stat
import sys
from pathlib import Path
from typing import Any

from .core import Context, InstallError, read_owned, secure_directory
from .native_index import (
    ProcessIdentity,
    load_process_receipt,
    process_identity,
    stop_process,
)

_UV_VERSION = "0.12.14"
_PROCESS_RESOURCE = "native_process"
_PROCESS_STATUS = "native_process_status"
_UNIT_RESOURCE = "native_unit"
_INDEX_LABEL = "invalid.example.cairn.install-instance"
_VENDOR_DROPIN_POLICY = "native_vendor_dropin_policy"
_INDEX_PORT_RESOURCE = "native_index_port"
_MAX_VENDOR_DROPIN_BYTES = 1024 * 1024


def _unit_directory() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _vendor_dropin_directory() -> Path:
    return Path("/usr/lib/systemd/user/service.d")


class Backend:
    def __init__(self, ctx: Context, *, foreground: bool = False) -> None:
        if ctx.mode not in {"disposable", "native"}:
            raise InstallError(f"Native backend cannot install mode {ctx.mode}")
        self.ctx = ctx
        if ctx.mode == "native":
            system = platform.system()
            keys = ctx.state["resources"]
            if (
                system == "Linux"
                and any(key.startswith("native_launch_agent") for key in keys)
            ) or (
                system == "Darwin"
                and any(key.startswith("native_unit") for key in keys)
            ):
                raise InstallError(
                    "Native service manager receipt belongs to another platform"
                )
        if foreground and ctx.mode != "disposable":
            raise InstallError("Foreground execution requires disposable mode")
        self.foreground = ctx.mode == "disposable" and (
            foreground
            or platform.system() == "Darwin"
            or "native_foreground" in ctx.state["resources"]
        )
        from .foreground import ForegroundProcess

        self._foreground = ForegroundProcess(ctx) if self.foreground else None
        from .launchd import DarwinLaunchAgent

        self._launch_agent = (
            DarwinLaunchAgent(ctx)
            if platform.system() == "Darwin" and ctx.mode == "native"
            else None
        )
        self.config_path = ctx.root / "config.yaml"
        self.data_path = ctx.root / "data"
        self.credentials_path = ctx.root / "credentials"
        self.runtime_path = ctx.root / "runtime"
        self.service_log = ctx.root / "service.log"
        self._index = _NativeIndex(ctx) if ctx.semantic else None

    @property
    def runtime_python(self) -> Path:
        return self.runtime_path / "bin" / "python"

    def preflight(self) -> None:
        system = platform.system()
        if system not in {"Linux", "Darwin"}:
            raise InstallError("Native installation requires Linux or macOS")
        if system == "Darwin" and self.ctx.semantic:
            raise InstallError(
                "macOS installation currently supports catalogue and Attic only"
            )
        if not (3, 12) <= sys.version_info[:2] <= (3, 14):
            raise InstallError("The installer needs host Python 3.12–3.14")
        if os.geteuid() == 0:
            raise InstallError(
                "Run the native installer as its dedicated non-root user"
            )
        architecture = self.ctx.command(["uname", "-m"], cwd=self.ctx.directory).strip()
        if architecture not in (
            {"x86_64", "arm64"} if system == "Darwin" else {"x86_64"}
        ):
            raise InstallError(
                "Unsupported native architecture; Linux requires x86_64, macOS requires x86_64 or arm64"
            )
        uv_version = self.ctx.command(
            ["uv", "--version"], cwd=self.ctx.directory
        ).strip()
        uv_fields = uv_version.split()
        if uv_fields[:2] != ["uv", _UV_VERSION]:
            raise InstallError(
                f"uv {_UV_VERSION} is required; found {uv_version or 'no version'}"
            )
        for required in ("pyproject.toml", "uv.lock"):
            path = self.ctx.source / required
            try:
                info = path.lstat()
            except OSError as error:
                raise InstallError(f"Installer source is missing {path}") from error
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise InstallError(f"Installer source file is not regular: {path}")
        if self._launch_agent is not None:
            self._launch_agent.preflight()
        elif self.ctx.mode == "native":
            state = self.ctx.command(
                ["systemctl", "--user", "is-system-running"],
                cwd=self.ctx.directory,
                allowed=(0, 1),
            ).strip()
            if state not in {"running", "degraded"}:
                raise InstallError(
                    "A running systemd user manager is required for persistent native mode"
                )
            self._validate_loaded_unit(
                require_expected=False, record_vendor_policy=True
            )
        if self._index is not None:
            self._index.preflight()
        if not self.is_running():
            self._require_available_port()

    def prepare(self) -> None:
        for directory in (self.data_path, self.credentials_path):
            secure_directory(directory, private=True)
        self.ctx.state["resources"].setdefault(
            "native_runtime",
            {"path": str(self.runtime_path), "python": "3.14", "uv": _UV_VERSION},
        )
        self.ctx.save()
        self.ctx.command(
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--no-editable",
                "--python",
                "3.14",
            ],
            cwd=self.ctx.source,
            env={"UV_PROJECT_ENVIRONMENT": str(self.runtime_path)},
            timeout=900,
        )
        if self._index is not None:
            self._index.ensure_port()
        self.ctx.write_file(self.config_path, self._configuration())
        if self._index is not None:
            self._index.prepare()

    def validate_ownership(self) -> None:
        if self._foreground is not None:
            self._foreground.validate()
        if self.config_path.exists() or self.config_path.is_symlink():
            self.ctx.check_file(self.config_path)
        elif self._file_was_recorded(self.config_path):
            raise InstallError("Owned native configuration disappeared")
        if self._launch_agent is not None:
            self._launch_agent.validate_ownership()
        elif self.ctx.mode == "native":
            receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
            if receipt is not None:
                unit_path, _ = self._validated_unit_receipt(receipt)
                status = receipt.get("status")
                if unit_path.exists() or unit_path.is_symlink():
                    self.ctx.check_file(unit_path)
                    self._validate_loaded_unit(
                        require_expected=status != "materialised"
                    )
                elif status != "removed" and not self._unit_delete_is_reconcilable(
                    unit_path
                ):
                    raise InstallError(
                        "Owned native unit disappeared; refusing adoption"
                    )
        if self._index is not None:
            self._index.validate_ownership()

    def is_running(self) -> bool:
        if self._launch_agent is not None:
            return self._launch_agent.is_running()
        if self._foreground is not None:
            return self._foreground.running()
        if self.ctx.mode == "native":
            receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
            if receipt is None or receipt.get("status") == "removed":
                return False
            _, service = self._validated_unit_receipt(receipt)
            self.validate_ownership()
            state = self.ctx.command(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property",
                    "ActiveState",
                    "--value",
                    service,
                ],
                cwd=self.ctx.directory,
                allowed=(0, 1, 3, 4),
            ).strip()
            return state == "active"
        self._reconcile_process_intent()
        receipt = self._current_process()
        if receipt is None:
            return False
        observed = process_identity(receipt.pid)
        if observed == receipt:
            self.ctx.state["resources"][_PROCESS_STATUS] = "running"
            self.ctx.save()
            return True
        status = "stopped" if observed is None else "identity_mismatch"
        self.ctx.state["resources"][_PROCESS_STATUS] = status
        self.ctx.save()
        return False

    def start(self) -> None:
        if self._launch_agent is not None:
            self._launch_agent.start()
            return
        if self._index is not None:
            self._index.start()
        if self._foreground is not None:
            if not self._foreground.running():
                self._require_available_port()
            self._foreground.start(
                [
                    str(self.runtime_python),
                    str(self.runtime_path / "bin" / "cairn"),
                    "serve",
                    "--config",
                    str(self.config_path),
                ],
                self.service_log,
            )
        elif self.ctx.mode == "native":
            self._start_unit()
        else:
            self._start_process()

    def stop(self) -> None:
        if self._launch_agent is not None:
            self._launch_agent.stop()
            return
        if self._foreground is not None:
            self._foreground.stop()
            return
        if self.ctx.mode == "native":
            receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
            if receipt is None or receipt.get("status") == "removed":
                return
            _, service = self._validated_unit_receipt(receipt)
            self.validate_ownership()
            self.ctx.state["resources"]["native_stop_intent"] = True
            self.ctx.save()
            self.ctx.command(
                ["systemctl", "--user", "stop", service], cwd=self.ctx.directory
            )
            receipt["status"] = "stopped"
            self.ctx.save()
            return
        self._reconcile_process_intent()
        receipt = self._current_process()
        if receipt is None:
            return
        observed = process_identity(receipt.pid)
        if observed is not None and observed != receipt:
            self.ctx.state["resources"][_PROCESS_STATUS] = "identity_mismatch"
            self.ctx.save()
            raise InstallError(
                "Native process identity changed; refusing to signal a reused PID"
            )
        self.ctx.state["resources"]["native_stop_intent"] = receipt.to_json()
        self.ctx.save()
        if observed is not None:
            self.ctx.note(
                f"Stop proved-owned disposable process PID {receipt.pid} by pidfd identity."
            )
            stop_process(receipt)
        self.ctx.state["resources"][_PROCESS_STATUS] = "stopped"
        self.ctx.save()

    def restart(self) -> None:
        if self._launch_agent is not None:
            self._launch_agent.restart()
            return
        if self.ctx.mode == "native":
            receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
            if receipt is None or receipt.get("status") == "removed":
                self.start()
                return
            _, service = self._validated_unit_receipt(receipt)
            self.validate_ownership()
            self.ctx.state["resources"]["native_restart_intent"] = True
            self.ctx.save()
            self.ctx.command(
                ["systemctl", "--user", "restart", service], cwd=self.ctx.directory
            )
            receipt["status"] = "enabled"
            self.ctx.save()
            return
        self.stop()
        self.start()

    def close(self) -> None:
        if self._launch_agent is not None:
            self._launch_agent.close()
        if self._foreground is not None:
            self._foreground.close()

    def wait_foreground(self) -> None:
        if self._foreground is None:
            raise InstallError(
                "Foreground waiting requires a parent-owned disposable process"
            )
        self._foreground.wait()

    def rollback(self) -> None:
        if self._launch_agent is not None:
            try:
                self._launch_agent.remove()
            finally:
                self._launch_agent.close()
        elif self.ctx.mode == "native":
            receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
            if receipt is not None and receipt.get("status") != "removed":
                unit_path, service = self._validated_unit_receipt(receipt)
                if not unit_path.exists() and self._unit_delete_is_reconcilable(
                    unit_path
                ):
                    self.ctx.command(
                        ["systemctl", "--user", "daemon-reload"],
                        cwd=self.ctx.directory,
                    )
                    receipt["status"] = "removed"
                    self.ctx.state["resources"]["native_unit_delete_intent"][
                        "phase"
                    ] = "complete"
                    self.ctx.save()
                else:
                    self.validate_ownership()
                    self.ctx.state["resources"]["native_unit_delete_intent"] = {
                        "path": str(unit_path),
                        "service": service,
                        "phase": "disable_pending",
                    }
                    self.ctx.save()
                    self.ctx.command(
                        ["systemctl", "--user", "disable", "--now", service],
                        cwd=self.ctx.directory,
                        allowed=(0, 1, 3, 4),
                    )
                    self.ctx.state["resources"]["native_unit_delete_intent"][
                        "phase"
                    ] = "unlink_pending"
                    self.ctx.save()
                    self.ctx.check_file(unit_path)
                    unit_path.unlink()
                    _fsync_directory(unit_path.parent)
                    self.ctx.command(
                        ["systemctl", "--user", "daemon-reload"],
                        cwd=self.ctx.directory,
                    )
                    receipt["status"] = "removed"
                    self.ctx.state["resources"]["native_unit_delete_intent"][
                        "phase"
                    ] = "complete"
                    self.ctx.save()
        else:
            self.stop()
        if self._index is not None:
            self._index.rollback()
        self.ctx.note(
            f"Rollback retained configuration, data and credentials under {self.ctx.root}."
        )

    def blitz(self) -> None:
        """Remove all proved-owned native runtime resources and semantic data."""
        resources = self.ctx.state["resources"]
        if self._launch_agent is not None:
            self._launch_agent.remove()
            resources["native_blitz"] = "complete"
            self.ctx.save()
            return
        resources["native_blitz_intent"] = "remove_all_owned_resources"
        self.ctx.save()

        # Inventory every Docker survivor before stopping the service. A foreign
        # replacement therefore blocks the destructive sequence at its outset.
        index_inventory = (
            self._index._blitz_inventory() if self._index is not None else None
        )
        unit = self._blitz_unit_inventory() if self.ctx.mode == "native" else None

        if self.ctx.mode == "native":
            if unit is not None:
                self._blitz_unit(*unit)
        else:
            self.stop()
            receipt = self._current_process() if self._foreground is None else None
            if receipt is not None and process_identity(receipt.pid) == receipt:
                raise InstallError(
                    "Owned disposable process remains after blitz; refusing data deletion"
                )

        if self._index is not None and index_inventory is not None:
            self._index.blitz(index_inventory)
        resources["native_blitz"] = "complete"
        self.ctx.save()

    def _blitz_unit_inventory(
        self,
    ) -> tuple[dict[str, object], Path, str] | None:
        resources = self.ctx.state["resources"]
        unit_path = _unit_directory() / self._service_name()
        expected_intent = {
            "path": str(unit_path),
            "service": self._service_name(),
        }
        intent = resources.get("native_unit_intent")
        if intent is not None and intent != expected_intent:
            raise InstallError("Native unit creation intent is malformed")

        receipt = resources.get(_UNIT_RESOURCE)
        if receipt is None:
            if intent != expected_intent:
                return None
            if not unit_path.exists() and not unit_path.is_symlink():
                return None
            self.ctx.check_file(unit_path)
            receipt = {**expected_intent, "status": "materialised"}
            resources[_UNIT_RESOURCE] = receipt
            self.ctx.save()
        self._validated_unit_receipt(receipt)
        status = receipt.get("status")
        if not isinstance(status, str):
            raise InstallError("Native unit ownership receipt is malformed")
        if status == "removed":
            if unit_path.exists() or unit_path.is_symlink():
                raise InstallError("Removed native unit path was replaced")
            self._require_unit_stopped(self._service_name())
            return None
        if unit_path.exists() or unit_path.is_symlink():
            self.ctx.check_file(unit_path)
            self._validate_loaded_unit(require_expected=status != "materialised")
        elif not self._unit_delete_is_reconcilable(unit_path):
            raise InstallError("Owned native unit disappeared; refusing adoption")
        return receipt, unit_path, self._service_name()

    def _blitz_unit(
        self, receipt: dict[str, object], unit_path: Path, service: str
    ) -> None:
        resources = self.ctx.state["resources"]
        if not unit_path.exists() and not unit_path.is_symlink():
            if not self._unit_delete_is_reconcilable(unit_path):
                raise InstallError("Owned native unit disappeared; refusing adoption")
            # The owned path was already unlinked. Do not disable the service name
            # again: a new foreign fragment could have claimed it meanwhile.
            self._require_unit_stopped(service)
            self.ctx.command(
                ["systemctl", "--user", "daemon-reload"], cwd=self.ctx.directory
            )
            receipt["status"] = "removed"
            resources["native_unit_delete_intent"]["phase"] = "complete"
            self.ctx.save()
            return
        resources["native_unit_delete_intent"] = {
            "path": str(unit_path),
            "service": service,
            "phase": "disable_pending",
        }
        self.ctx.save()
        self.ctx.command(
            ["systemctl", "--user", "disable", "--now", service],
            cwd=self.ctx.directory,
            allowed=(0, 1, 3, 4),
        )
        self._require_unit_stopped(service)
        resources["native_unit_delete_intent"]["phase"] = "unlink_pending"
        self.ctx.save()
        if unit_path.exists() or unit_path.is_symlink():
            self.ctx.check_file(unit_path)
            unit_path.unlink()
            _fsync_directory(unit_path.parent)
        self.ctx.command(
            ["systemctl", "--user", "daemon-reload"], cwd=self.ctx.directory
        )
        receipt["status"] = "removed"
        resources["native_unit_delete_intent"]["phase"] = "complete"
        self.ctx.save()

    def _require_unit_stopped(self, service: str) -> None:
        raw = self.ctx.command(
            [
                "systemctl",
                "--user",
                "show",
                "--property",
                "LoadState",
                "--property",
                "ActiveState",
                service,
            ],
            cwd=self.ctx.directory,
            allowed=(0, 1, 3, 4),
        ).strip()
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in fields:
                raise InstallError(
                    f"Could not prove native service stopped before blitz: {service}"
                )
            fields[key] = value
        if set(fields) != {"LoadState", "ActiveState"} or fields["ActiveState"] not in {
            "inactive",
            "failed",
        }:
            raise InstallError(
                f"Owned native service did not stop before blitz: {service} "
                f"({fields.get('ActiveState', 'unknown')})"
            )

    def lifecycle_argv(self, operation: str) -> list[str]:
        if operation not in {"check-config", "migrate", "verify", "bootstrap"}:
            raise InstallError(f"Unsupported native lifecycle operation: {operation}")
        return [
            str(self.runtime_path / "bin" / "cairn"),
            operation,
            "--config",
            str(self.config_path),
        ]

    def _configuration(self) -> str:
        graphiti = "  enabled: false\n"
        if self.ctx.semantic:
            if self._index is None:
                raise InstallError("Semantic native index is unavailable")
            graphiti = (
                f"  enabled: true\n  host: 127.0.0.1\n  port: {self._index.port}\n"
            )
        return (
            "schema_version: cairn.config/v1\n"
            f"instance_id: {self.ctx.instance_id}\n"
            "mode: production\n"
            "http:\n"
            "  host: 127.0.0.1\n"
            f"  port: {self.ctx.port}\n"
            "paths:\n"
            f"  data: {_yaml_string(self.data_path)}\n"
            f"  credentials: {_yaml_string(self.credentials_path)}\n"
            "attic:\n"
            "  enabled: true\n"
            "graphiti:\n"
            f"{graphiti}"
        )

    def _start_unit(self) -> None:
        unit_path = _unit_directory() / self._service_name()
        secure_directory(unit_path.parent)
        self._validate_loaded_unit(require_expected=False)
        content = self._unit_contents()
        receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
        if receipt is not None:
            recorded_path, recorded_service = self._validated_unit_receipt(receipt)
            if recorded_path != unit_path or recorded_service != self._service_name():
                raise InstallError(
                    "Recorded native unit does not match this installation"
                )
        self.ctx.state["resources"]["native_unit_intent"] = {
            "path": str(unit_path),
            "service": self._service_name(),
        }
        self.ctx.save()
        self.ctx.write_file(unit_path, content, mode=0o644)
        receipt = {
            "path": str(unit_path),
            "service": self._service_name(),
            "status": "materialised",
        }
        self.ctx.state["resources"][_UNIT_RESOURCE] = receipt
        self.ctx.save()
        self.ctx.command(
            ["systemd-analyze", "--user", "verify", str(unit_path)],
            cwd=self.ctx.directory,
        )
        self.ctx.command(
            ["systemctl", "--user", "daemon-reload"], cwd=self.ctx.directory
        )
        self._validate_loaded_unit(require_expected=True)
        self.ctx.command(
            ["systemctl", "--user", "enable", "--now", self._service_name()],
            cwd=self.ctx.directory,
        )
        receipt["status"] = "enabled"
        self.ctx.save()

    def _start_process(self) -> None:
        if self._reconcile_process_intent():
            return
        current = self._current_process()
        if current is not None and process_identity(current.pid) == current:
            self.ctx.state["resources"][_PROCESS_STATUS] = "running"
            self.ctx.save()
            return
        generation = (
            int(self.ctx.state["resources"].get("native_process_generation", 0)) + 1
        )
        receipt_path = self.ctx.root / f"process-{generation}.json"
        argv = [
            str(self.runtime_python),
            str(self.runtime_path / "bin" / "cairn"),
            "serve",
            "--config",
            str(self.config_path),
        ]
        intent = {
            "generation": generation,
            "receipt_path": str(receipt_path),
            "argv": argv,
        }
        self.ctx.state["resources"]["native_process_intent"] = intent
        self.ctx.save()
        helper_argv = [
            sys.executable,
            "-m",
            "cairn_install.native_index",
            "spawn",
            "--receipt",
            str(receipt_path),
            "--log",
            str(self.service_log),
            "--cwd",
            str(self.ctx.root),
            "--",
            *argv,
        ]
        self.ctx.command(
            helper_argv,
            cwd=self.ctx.source,
            env={"PYTHONPATH": str(self.ctx.source / "src")},
        )
        identity = load_process_receipt(receipt_path)
        if identity.uid != os.getuid() or identity.argv != tuple(argv):
            raise InstallError("Detached process receipt does not match launch intent")
        self.ctx.state["resources"][_PROCESS_RESOURCE] = identity.to_json()
        self.ctx.state["resources"]["native_process_generation"] = generation
        self.ctx.state["resources"][_PROCESS_STATUS] = "running"
        self.ctx.save()

    def _reconcile_process_intent(self) -> bool:
        value = self.ctx.state["resources"].get("native_process_intent")
        if value is None:
            return False
        if not isinstance(value, dict):
            raise InstallError("Disposable process launch intent is malformed")
        generation = value.get("generation")
        receipt_value = value.get("receipt_path")
        argv = value.get("argv")
        if (
            type(generation) is not int
            or generation <= 0
            or receipt_value != str(self.ctx.root / f"process-{generation}.json")
            or not isinstance(argv, list)
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise InstallError("Disposable process launch intent is malformed")
        receipt_path = Path(receipt_value)
        if not receipt_path.exists() and not receipt_path.is_symlink():
            return False
        identity = load_process_receipt(receipt_path)
        if identity.uid != os.getuid() or identity.argv != tuple(argv):
            raise InstallError("Detached process receipt does not match launch intent")
        observed = process_identity(identity.pid)
        if observed != identity:
            self.ctx.state["resources"][_PROCESS_RESOURCE] = identity.to_json()
            self.ctx.state["resources"]["native_process_generation"] = generation
            self.ctx.state["resources"][_PROCESS_STATUS] = (
                "stopped" if observed is None else "identity_mismatch"
            )
            self.ctx.save()
            return False
        self.ctx.state["resources"][_PROCESS_RESOURCE] = identity.to_json()
        self.ctx.state["resources"]["native_process_generation"] = generation
        self.ctx.state["resources"][_PROCESS_STATUS] = "running"
        self.ctx.save()
        return True

    def _current_process(self) -> ProcessIdentity | None:
        receipt = self.ctx.state["resources"].get(_PROCESS_RESOURCE)
        if receipt is None:
            return None
        return ProcessIdentity.from_json(receipt)

    def _service_name(self) -> str:
        return f"cairn-install-{self.ctx.name}.service"

    def _unit_contents(self) -> str:
        executable = _systemd_quote(str(self.runtime_path / "bin" / "cairn"))
        config = _systemd_quote(str(self.config_path))
        return (
            "[Unit]\n"
            f"Description=Cairn guided installation {self.ctx.name}\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={executable} serve --config {config}\n"
            "Restart=on-failure\n"
            "RestartSec=5s\n"
            "TimeoutStopSec=60s\n"
            "UMask=0077\n"
            "NoNewPrivileges=true\n"
            "PrivateTmp=true\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _validated_unit_receipt(self, value: object) -> tuple[Path, str]:
        if not isinstance(value, dict):
            raise InstallError("Native unit ownership receipt is malformed")
        expected_path = _unit_directory() / self._service_name()
        if value.get("path") != str(expected_path):
            raise InstallError("Native unit ownership path does not match")
        if value.get("service") != self._service_name():
            raise InstallError("Native unit ownership name does not match")
        return expected_path, self._service_name()

    def _file_was_recorded(self, path: Path) -> bool:
        key = str(path.absolute())
        return key in self.ctx.state["owned_files"] or key in self.ctx.state.get(
            "file_intents", {}
        )

    def _validate_loaded_unit(
        self, *, require_expected: bool, record_vendor_policy: bool = False
    ) -> None:
        service = self._service_name()
        expected = str(_unit_directory() / service)
        fragment = self.ctx.command(
            [
                "systemctl",
                "--user",
                "show",
                "--property",
                "FragmentPath",
                "--value",
                service,
            ],
            cwd=self.ctx.directory,
            allowed=(0, 1, 3, 4),
        ).strip()
        dropins = self.ctx.command(
            [
                "systemctl",
                "--user",
                "show",
                "--property",
                "DropInPaths",
                "--value",
                service,
            ],
            cwd=self.ctx.directory,
            allowed=(0, 1, 3, 4),
        ).strip()
        self._validate_vendor_dropins(
            dropins.split(),
            record_policy=record_vendor_policy,
            unit_loaded=bool(fragment),
        )
        if fragment and fragment != expected:
            raise InstallError(f"Refusing foreign loaded native unit {service}")
        if require_expected and fragment != expected:
            raise InstallError(f"Owned native unit is not loaded from {expected}")

    def _validate_vendor_dropins(
        self, paths: list[str], *, record_policy: bool, unit_loaded: bool
    ) -> None:
        vendor_directory = _vendor_dropin_directory()
        observed_files: list[dict[str, str]] = []
        for value in sorted(paths):
            path = Path(value)
            if (
                not path.is_absolute()
                or path.parent != vendor_directory
                or path.suffix != ".conf"
            ):
                raise InstallError(
                    f"Refusing foreign drop-in for native unit {self._service_name()}: {path}"
                )
            digest = _vendor_dropin_digest(path)
            observed_files.append({"path": str(path), "sha256": digest})
        resources = self.ctx.state["resources"]
        recorded = resources.get(_VENDOR_DROPIN_POLICY)
        if record_policy:
            expected: dict[str, object] = {
                "schema": 1,
                "files": _vendor_dropin_inventory(vendor_directory),
            }
            if recorded is None:
                resources[_VENDOR_DROPIN_POLICY] = expected
                self.ctx.save()
                recorded = expected
            elif recorded != expected:
                raise InstallError(
                    f"Native vendor drop-in policy changed for {self._service_name()}"
                )
        if recorded is None:
            if observed_files:
                raise InstallError(
                    "Refusing unrecorded vendor drop-in policy for native unit "
                    f"{self._service_name()}"
                )
            return
        if (
            not isinstance(recorded, dict)
            or recorded.get("schema") != 1
            or not isinstance(recorded.get("files"), list)
        ):
            raise InstallError("Native vendor drop-in policy receipt is malformed")
        expected_files: list[dict[str, str]] = []
        for item in recorded["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "sha256"}
                or not isinstance(item.get("path"), str)
                or Path(item["path"]).parent != vendor_directory
                or not isinstance(item.get("sha256"), str)
                or len(item["sha256"]) != 64
                or any(
                    character not in "0123456789abcdef" for character in item["sha256"]
                )
            ):
                raise InstallError("Native vendor drop-in policy receipt is malformed")
            expected_files.append({"path": item["path"], "sha256": item["sha256"]})
        if expected_files != sorted(expected_files, key=lambda item: item["path"]):
            raise InstallError("Native vendor drop-in policy receipt is malformed")
        if unit_loaded:
            matches = observed_files == expected_files
        else:
            matches = all(item in expected_files for item in observed_files)
        if not matches:
            raise InstallError(
                f"Native vendor drop-in policy changed for {self._service_name()}"
            )
        if record_policy:
            for item in expected_files:
                self.ctx.note(
                    "Accepted system vendor user-service drop-in "
                    f"{item['path']} with SHA256 {item['sha256']}."
                )

    def _unit_delete_is_reconcilable(self, unit_path: Path) -> bool:
        value = self.ctx.state["resources"].get("native_unit_delete_intent")
        return (
            isinstance(value, dict)
            and value.get("path") == str(unit_path)
            and value.get("service") == self._service_name()
            and value.get("phase") == "unlink_pending"
        )

    def _require_available_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.ctx.port))
        except OSError as error:
            raise InstallError(
                f"Loopback port {self.ctx.port} is already in use by another process"
            ) from error
        finally:
            probe.close()


class _NativeIndex:
    """Own one optional FalkorDB container while preserving its volumes."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.container = f"cairn-install-{ctx.name}-falkordb"
        self.data_volume = f"cairn-install-{ctx.name}-falkordb-data"
        self.config_volume = f"cairn-install-{ctx.name}-falkordb-config"
        self.password_path = ctx.root / "credentials" / "falkordb-password"
        self.provider_path = ctx.root / "credentials" / "openai-api-key"
        self.index_config_path = ctx.root / "credentials" / "falkordb.conf"

    def preflight(self) -> None:
        version = self.ctx.command(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            cwd=self.ctx.directory,
        ).strip()
        if _version_tuple(version) < (25, 0):
            raise InstallError(
                f"Docker Engine 25 or newer is required; found {version}"
            )
        image = self._locked_image()
        self.ctx.state["resources"]["native_index_image"] = image
        self.ctx.save()
        self.ensure_port()

    @property
    def port(self) -> int:
        value = self.ctx.state["resources"].get(_INDEX_PORT_RESOURCE)
        if type(value) is not int or not 1 <= value <= 65535:
            raise InstallError("Native semantic index port receipt is malformed")
        return value

    def ensure_port(self) -> None:
        resources = self.ctx.state["resources"]
        value = resources.get(_INDEX_PORT_RESOURCE)
        if value is None:
            legacy = self._legacy_config_port()
            value = legacy if legacy is not None else self._allocate_loopback_port()
            resources[_INDEX_PORT_RESOURCE] = value
            self.ctx.save()
            self.ctx.note(
                f"Selected and recorded loopback port {value} for the semantic index."
            )
        port = self.port
        value = self._owned_running_container()
        if value is not None:
            self._validate_port_mapping(value, port)
            return
        self._require_loopback_port_available(port)

    def prepare(self) -> None:
        configured_provider = self.ctx.state.get("provider_key_file")
        if configured_provider != str(self.provider_path):
            raise InstallError(
                "Semantic native mode needs its protected provider copy at "
                f"{self.provider_path}"
            )
        self.ctx.read_secret(self.provider_path)
        password_present = (
            self.password_path.exists() or self.password_path.is_symlink()
        )
        password_recorded = str(self.password_path.absolute()) in self.ctx.state.get(
            "owned_files", {}
        ) or str(self.password_path.absolute()) in self.ctx.state.get(
            "file_intents", {}
        )
        if password_recorded and not password_present:
            raise InstallError(
                f"Previously recorded FalkorDB credential is missing: {self.password_path}"
            )
        if password_present:
            self.ctx.check_file(self.password_path)
            password = self.ctx.read_secret(self.password_path)
        else:
            password = secrets.token_hex(32)
            self.ctx.write_file(
                self.password_path,
                password + "\n",
                secret=True,
            )
        self.ctx.write_file(
            self.index_config_path,
            f"requirepass {password}\n",
            secret=True,
        )
        self._ensure_volume(self.data_volume, "data")
        self._ensure_volume(self.config_volume, "config")
        self._prepare_volumes()
        self._ensure_container()

    def start(self) -> None:
        """Start the retained index without preparing volumes or credentials."""
        self.validate_ownership()
        receipt = self.ctx.state["resources"].get("native_index_container")
        if receipt is None:
            raise InstallError(
                "Native semantic index has no retained container receipt"
            )
        self.ctx.command(["docker", "start", self.container], cwd=self.ctx.directory)
        receipt["status"] = "running"
        self.ctx.save()

    def validate_ownership(self) -> None:
        resources = self.ctx.state["resources"]
        port = None if resources.get(_INDEX_PORT_RESOURCE) is None else self.port
        for kind, name in (
            ("data", self.data_volume),
            ("config", self.config_volume),
        ):
            receipt = resources.get(f"native_index_{kind}_volume")
            if receipt is not None:
                if receipt.get("name") != name:
                    raise InstallError("Native index volume ownership receipt changed")
                value = self._inspect("volume", name)
                if value is None:
                    raise InstallError(f"Owned native index volume disappeared: {name}")
                self._check_label(value, name)
        container_receipt = resources.get("native_index_container")
        if container_receipt is not None:
            if container_receipt.get("name") != self.container:
                raise InstallError("Native index container ownership receipt changed")
            value = self._inspect("container", self.container)
            if value is None:
                raise InstallError(
                    f"Owned native index container disappeared: {self.container}"
                )
            self._check_label(value, self.container)
            expected_port = port
            if expected_port is None:
                expected_port = self._legacy_config_port()
            if expected_port is None:
                raise InstallError("Owned native semantic index port disappeared")
            self._validate_port_mapping(value, expected_port)

    def rollback(self) -> None:
        self._rollback_init_survivor()
        resources = self.ctx.state["resources"]
        receipt = resources.get("native_index_container")
        if receipt is None:
            intent = resources.get("native_index_container_intent")
            expected = {"name": self.container, "label": self.ctx.instance_id}
            if intent is not None and intent != expected:
                raise InstallError("Native index container intent is malformed")
            if intent == expected:
                value = self._inspect("container", self.container)
                if value is not None:
                    self._check_label(value, self.container)
                    receipt = {**expected, "status": "running"}
                    resources["native_index_container"] = receipt
                    self.ctx.save()
        if receipt is not None:
            self._require_label("container", self.container)
            resources["native_index_rollback_intent"] = True
            self.ctx.save()
            self.ctx.command(
                ["docker", "stop", "--time", "30", self.container],
                cwd=self.ctx.directory,
            )
            receipt["status"] = "stopped"
            self.ctx.save()
            self.ctx.note(
                "Native semantic rollback retained the FalkorDB container and both volumes."
            )

    def blitz(
        self,
        inventory: tuple[str | None, str | None, list[tuple[str, str]]] | None = None,
    ) -> None:
        """Remove the proved-owned FalkorDB containers and data volumes."""
        init_name, container_name, volumes = inventory or self._blitz_inventory()
        resources = self.ctx.state["resources"]
        resources["native_index_blitz_intent"] = "remove_container_and_volumes"
        self.ctx.save()
        for name, key in (
            (init_name, "native_index_volume_init_blitz"),
            (container_name, "native_index_container_blitz"),
        ):
            if name is None:
                continue
            self.ctx.command(["docker", "rm", "--force", name], cwd=self.ctx.directory)
            if self._blitz_inspect("container", name) is not None:
                raise InstallError(f"Owned native index container remains: {name}")
            resources[key] = "removed"
            self.ctx.save()
        for name, key in volumes:
            # No --force: attached or otherwise shared volumes must survive.
            self.ctx.command(["docker", "volume", "rm", name], cwd=self.ctx.directory)
            if self._blitz_inspect("volume", name) is not None:
                raise InstallError(f"Owned native index volume remains: {name}")
            resources[key] = "removed"
            self.ctx.save()
        resources["native_index_blitz"] = "complete"
        self.ctx.save()

    def _blitz_inventory(
        self,
    ) -> tuple[str | None, str | None, list[tuple[str, str]]]:
        """Resolve recorded creation intents without requiring survivors to exist."""
        resources = self.ctx.state["resources"]
        init_name = f"{self.container}-init-{self.ctx.run_id[:8]}"
        init_expected = {"name": init_name, "label": self.ctx.instance_id}
        init_intent = resources.get("native_index_volume_init_intent")
        if init_intent is not None and init_intent != init_expected:
            raise InstallError("Native index initialiser intent is malformed")
        init_survivor = (
            self._blitz_inspect("container", init_name) if init_intent else None
        )
        if init_survivor is not None:
            self._check_label(init_survivor, init_name)

        container_expected = {"name": self.container, "label": self.ctx.instance_id}
        container_receipt = resources.get("native_index_container")
        container_intent = resources.get("native_index_container_intent")
        if container_intent is not None and not self._blitz_container_record_matches(
            container_intent, container_expected
        ):
            raise InstallError("Native index container intent is malformed")
        if container_receipt is not None and not self._blitz_container_record_matches(
            container_receipt, container_expected
        ):
            raise InstallError("Native index container ownership receipt changed")
        container_authorised = (
            container_receipt is not None or container_intent is not None
        )
        container_value = (
            self._blitz_inspect("container", self.container)
            if container_authorised
            else None
        )
        if container_value is not None:
            self._check_label(container_value, self.container)

        volume_survivors: list[tuple[str, str]] = []
        for kind, name in (
            ("data", self.data_volume),
            ("config", self.config_volume),
        ):
            key = f"native_index_{kind}_volume"
            expected = {"name": name, "label": self.ctx.instance_id}
            receipt = resources.get(key)
            intent = resources.get(f"{key}_intent")
            if intent is not None and intent != expected:
                raise InstallError("Native index volume intent is malformed")
            if receipt is not None and receipt != expected:
                raise InstallError("Native index volume ownership receipt changed")
            authorised = receipt is not None or intent is not None
            value = self._blitz_inspect("volume", name) if authorised else None
            if value is not None:
                self._check_label(value, name)
                volume_survivors.append((name, f"{key}_blitz"))

        return (
            init_name if init_survivor is not None else None,
            self.container if container_value is not None else None,
            volume_survivors,
        )

    @staticmethod
    def _blitz_container_record_matches(
        value: object, expected: dict[str, str]
    ) -> bool:
        if not isinstance(value, dict):
            return False
        keys = set(value)
        if keys not in ({"name", "label"}, {"name", "label", "status"}):
            return False
        if (
            value.get("name") != expected["name"]
            or value.get("label") != expected["label"]
        ):
            return False
        return "status" not in value or value.get("status") in {"running", "stopped"}

    def _blitz_inspect(self, kind: str, name: str) -> dict[str, Any] | None:
        if kind == "container":
            listed = self.ctx.command(
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{name}$",
                ],
                cwd=self.ctx.directory,
            )
            inspect_argv = ["docker", "inspect", name]
        elif kind == "volume":
            listed = self.ctx.command(
                [
                    "docker",
                    "volume",
                    "ls",
                    "--quiet",
                    "--filter",
                    f"name=^{name}$",
                ],
                cwd=self.ctx.directory,
            )
            inspect_argv = ["docker", "volume", "inspect", name]
        else:
            raise InstallError(f"Unsupported Docker ownership kind: {kind}")
        identifiers = [line for line in listed.splitlines() if line]
        if not identifiers:
            return None
        if len(identifiers) != 1:
            raise InstallError(f"Docker returned ambiguous ownership data for {name}")
        raw = self.ctx.command(inspect_argv, cwd=self.ctx.directory).strip()
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as error:
            raise InstallError(
                f"Docker returned malformed ownership data for {name}"
            ) from error
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
        ):
            raise InstallError(f"Docker returned ambiguous ownership data for {name}")
        return values[0]

    def _rollback_init_survivor(self) -> None:
        resources = self.ctx.state["resources"]
        intent = resources.get("native_index_volume_init_intent")
        if intent is None:
            return
        init_name = f"{self.container}-init-{self.ctx.run_id[:8]}"
        expected = {"name": init_name, "label": self.ctx.instance_id}
        if intent != expected:
            raise InstallError("Native index initialiser intent is malformed")
        value = self._inspect("container", init_name)
        if value is not None:
            self._check_label(value, init_name)
            self.ctx.command(
                ["docker", "rm", "--force", init_name],
                cwd=self.ctx.directory,
            )
        resources["native_index_volume_init"] = "rolled_back"
        self.ctx.save()

    def _locked_image(self) -> str:
        path = self.ctx.source / "deploy" / "images.lock"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise InstallError(
                f"Cannot read locked infrastructure images: {path}"
            ) from error
        values = [
            line.split("=", 1)[1]
            for line in lines
            if line.startswith("FALKORDB_IMAGE=")
        ]
        if len(values) != 1 or "@sha256:" not in values[0]:
            raise InstallError("FALKORDB_IMAGE must be exactly digest-pinned")
        return values[0]

    def _ensure_volume(self, name: str, kind: str) -> None:
        key = f"native_index_{kind}_volume"
        existing = self._inspect("volume", name)
        intent = {"name": name, "label": self.ctx.instance_id}
        if existing is not None:
            if (
                self.ctx.state["resources"].get(key) != intent
                and self.ctx.state["resources"].get(f"{key}_intent") != intent
            ):
                raise InstallError(f"Refusing existing foreign Docker volume: {name}")
            self._check_label(existing, name)
            self.ctx.state["resources"][key] = intent
            self.ctx.save()
            return
        if self.ctx.state["resources"].get(key) is not None:
            raise InstallError(f"Owned native index volume disappeared: {name}")
        self.ctx.state["resources"][f"{key}_intent"] = intent
        self.ctx.save()
        self.ctx.command(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"{_INDEX_LABEL}={self.ctx.instance_id}",
                name,
            ],
            cwd=self.ctx.directory,
        )
        self.ctx.state["resources"][key] = intent
        self.ctx.save()

    def _prepare_volumes(self) -> None:
        key = "native_index_volume_init"
        if self.ctx.state["resources"].get(key) == "complete":
            return
        init_name = f"{self.container}-init-{self.ctx.run_id[:8]}"
        intent = {"name": init_name, "label": self.ctx.instance_id}
        existing = self._inspect("container", init_name)
        if existing is not None:
            if (
                self.ctx.state["resources"].get("native_index_volume_init_intent")
                != intent
            ):
                raise InstallError(
                    f"Refusing existing foreign Docker container: {init_name}"
                )
            self._check_label(existing, init_name)
            self.ctx.command(
                ["docker", "rm", "--force", init_name],
                cwd=self.ctx.directory,
            )
        image = self._locked_image()
        self.ctx.state["resources"]["native_index_volume_init_intent"] = intent
        self.ctx.state["resources"][key] = "running"
        self.ctx.save()
        created = False
        try:
            self.ctx.command(
                [
                    "docker",
                    "create",
                    "--name",
                    init_name,
                    "--label",
                    f"{_INDEX_LABEL}={self.ctx.instance_id}",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--mount",
                    f"type=volume,source={self.config_volume},target=/config",
                    "--mount",
                    f"type=volume,source={self.data_volume},target=/var/lib/falkordb/data",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    # docker cp preserves the host operator ownership. The init
                    # process has CAP_CHOWN but deliberately lacks CAP_FOWNER,
                    # so first make UID 0 the owner before tightening the mode.
                    "chown 0:0 /config/cairn.conf.new && "
                    "chmod 0400 /config/cairn.conf.new && "
                    "chown 10001:0 /config/cairn.conf.new /var/lib/falkordb/data && "
                    "mv /config/cairn.conf.new /config/cairn.conf",
                ],
                cwd=self.ctx.directory,
            )
            created = True
            self.ctx.command(
                [
                    "docker",
                    "cp",
                    str(self.index_config_path),
                    f"{init_name}:/config/cairn.conf.new",
                ],
                cwd=self.ctx.directory,
            )
            self.ctx.command(
                ["docker", "start", "--attach", init_name],
                cwd=self.ctx.directory,
            )
        finally:
            survivor = self._inspect("container", init_name) if not created else None
            if survivor is not None:
                self._check_label(survivor, init_name)
            if created or survivor is not None:
                self.ctx.command(
                    ["docker", "rm", init_name],
                    cwd=self.ctx.directory,
                    allowed=(0, 1),
                )
        self.ctx.state["resources"][key] = "complete"
        self.ctx.save()

    def _ensure_container(self) -> None:
        port = self.port
        existing = self._inspect("container", self.container)
        receipt = {"name": self.container, "label": self.ctx.instance_id}
        recorded = self.ctx.state["resources"].get("native_index_container")
        intent = self.ctx.state["resources"].get("native_index_container_intent")
        if existing is not None:
            if recorded is None and intent != receipt:
                raise InstallError(
                    f"Refusing existing foreign Docker container: {self.container}"
                )
            self._check_label(existing, self.container)
            self._validate_port_mapping(existing, port)
            self.ctx.command(
                ["docker", "start", self.container],
                cwd=self.ctx.directory,
                allowed=(0, 1),
            )
            receipt["status"] = "running"
            self.ctx.state["resources"]["native_index_container"] = receipt
            self.ctx.save()
            return
        self.ctx.state["resources"]["native_index_container_intent"] = receipt
        self.ctx.save()
        self.ctx.command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.container,
                "--hostname",
                "falkordb",
                "--label",
                f"{_INDEX_LABEL}={self.ctx.instance_id}",
                "--restart",
                "unless-stopped",
                "--stop-timeout",
                "30",
                "--user",
                "10001:0",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--publish",
                f"127.0.0.1:{port}:6379",
                "--env",
                "REDIS_ARGS=/etc/falkordb/cairn.conf",
                "--env",
                "BROWSER=0",
                "--env",
                "TLS=0",
                "--env",
                "FALKORDB_ARGS=MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000",
                "--mount",
                f"type=volume,source={self.config_volume},target=/etc/falkordb,readonly",
                "--mount",
                f"type=volume,source={self.data_volume},target=/var/lib/falkordb/data",
                self._locked_image(),
            ],
            cwd=self.ctx.directory,
        )
        receipt["status"] = "running"
        self.ctx.state["resources"]["native_index_container"] = receipt
        self.ctx.save()

    def _legacy_config_port(self) -> int | None:
        path = self.ctx.root / "config.yaml"
        if not path.exists() and not path.is_symlink():
            return None
        self.ctx.check_file(path)
        try:
            content = read_owned(path, limit=64 * 1024).decode("utf-8")
        except UnicodeError as error:
            raise InstallError("Owned native configuration is not UTF-8") from error
        legacy = "graphiti:\n  enabled: true\n  host: 127.0.0.1\n  port: 16379\n"
        if legacy not in content:
            raise InstallError(
                "Owned semantic configuration has no recorded index port"
            )
        return 16379

    def _allocate_loopback_port(self) -> int:
        for _ in range(16):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            finally:
                probe.close()
            if port != self.ctx.port:
                return port
        raise InstallError("Could not allocate a loopback port for the semantic index")

    def _require_loopback_port_available(self, port: int) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise InstallError(
                f"Semantic index loopback port {port} is already in use by another process"
            ) from error
        finally:
            probe.close()

    def _owned_running_container(self) -> dict[str, Any] | None:
        resources = self.ctx.state["resources"]
        expected = {"name": self.container, "label": self.ctx.instance_id}
        receipt = resources.get("native_index_container")
        intent = resources.get("native_index_container_intent")
        if receipt is None and intent != expected:
            return None
        if receipt is not None and (
            not isinstance(receipt, dict)
            or receipt.get("name") != self.container
            or receipt.get("label") != self.ctx.instance_id
        ):
            raise InstallError("Native index container ownership receipt changed")
        value = self._inspect("container", self.container)
        if value is None:
            return None
        self._check_label(value, self.container)
        state = value.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            return None
        return value

    def _validate_port_mapping(self, value: dict[str, Any], port: int) -> None:
        expected = [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
        host_config = value.get("HostConfig")
        bindings = (
            host_config.get("PortBindings") if isinstance(host_config, dict) else None
        )
        if not isinstance(bindings, dict) or bindings.get("6379/tcp") != expected:
            raise InstallError(
                f"Native semantic container port mapping changed for {self.container}"
            )
        state = value.get("State")
        if isinstance(state, dict) and state.get("Running") is True:
            network = value.get("NetworkSettings")
            mappings = network.get("Ports") if isinstance(network, dict) else None
            if not isinstance(mappings, dict) or mappings.get("6379/tcp") != expected:
                raise InstallError(
                    "Running native semantic container port mapping changed for "
                    f"{self.container}"
                )

    def _inspect(self, kind: str, name: str) -> dict[str, Any] | None:
        argv = ["docker"]
        if kind == "volume":
            argv.append("volume")
        argv.extend(["inspect", name])
        output = self.ctx.command(argv, cwd=self.ctx.directory, allowed=(0, 1)).strip()
        if not output:
            return None
        try:
            values = json.loads(output)
        except json.JSONDecodeError as error:
            raise InstallError(
                f"Docker returned malformed ownership data for {name}"
            ) from error
        if values == []:
            return None
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
        ):
            raise InstallError(f"Docker returned ambiguous ownership data for {name}")
        return values[0]

    def _require_label(self, kind: str, name: str) -> None:
        value = self._inspect(kind, name)
        if value is None:
            raise InstallError(f"Owned Docker {kind} disappeared: {name}")
        self._check_label(value, name)

    def _check_label(self, value: dict[str, Any], name: str) -> None:
        labels = value.get("Labels")
        if labels is None:
            config = value.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
        if (
            not isinstance(labels, dict)
            or labels.get(_INDEX_LABEL) != self.ctx.instance_id
        ):
            raise InstallError(f"Refusing foreign Docker resource: {name}")


def _yaml_string(value: Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _systemd_quote(value: str) -> str:
    if not value.startswith("/") or "\x00" in value or "\n" in value or "\r" in value:
        raise InstallError("systemd paths must be absolute single-line values")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _version_tuple(value: str) -> tuple[int, int]:
    pieces = value.split(".", 2)
    if len(pieces) < 2 or not pieces[0].isdigit() or not pieces[1].isdigit():
        raise InstallError(
            f"Cannot parse Docker Engine version: {value or 'empty output'}"
        )
    return int(pieces[0]), int(pieces[1])


def _vendor_dropin_digest(path: Path) -> str:
    try:
        directory_info = path.parent.lstat()
        path_info = path.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(
            directory_info.st_mode
        ):
            raise InstallError(
                f"System vendor drop-in directory is not a real directory: {path.parent}"
            )
        if (
            not stat.S_ISREG(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or path_info.st_uid != 0
            or path_info.st_mode & 0o022
        ):
            raise InstallError(
                f"System vendor drop-in is not a protected root-owned file: {path}"
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            opened_info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_uid != 0
                or opened_info.st_mode & 0o022
                or (opened_info.st_dev, opened_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
            ):
                raise InstallError(
                    f"System vendor drop-in changed while it was inspected: {path}"
                )
            content = stream.read(_MAX_VENDOR_DROPIN_BYTES + 1)
    except OSError as error:
        raise InstallError(
            f"Cannot safely inspect system vendor drop-in {path}: {error}"
        ) from error
    if len(content) > _MAX_VENDOR_DROPIN_BYTES:
        raise InstallError(f"System vendor drop-in is too large: {path}")
    return hashlib.sha256(content).hexdigest()


def _vendor_dropin_inventory(directory: Path) -> list[dict[str, str]]:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise InstallError(
            f"Cannot inspect system vendor drop-in directory {directory}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
    ):
        raise InstallError(
            f"System vendor drop-in directory is not protected: {directory}"
        )
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".conf")
    except OSError as error:
        raise InstallError(
            f"Cannot list system vendor drop-in directory {directory}: {error}"
        ) from error
    return [
        {"path": str(path), "sha256": _vendor_dropin_digest(path)} for path in paths
    ]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
