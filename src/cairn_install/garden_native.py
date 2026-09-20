"""Garden's native unit, using the installer's existing ownership journal."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from .core import Context, InstallError, read_owned, secure_directory
from .garden import source_version
from .native import Backend, _systemd_quote, _unit_directory


class _UnitContext:
    """Share file ownership, but keep the two units' resource receipts separate.

    File operations and persistence remain on the real context. The native unit
    implementation only mutates its resources mapping, which is a direct view
    of the Garden resource journal in the parent state.
    """

    def __init__(self, parent: Context) -> None:
        self.parent = parent
        self.state = dict(parent.state)
        self.state["resources"] = parent.state["resources"].setdefault(
            "garden_native", {}
        )
        self.semantic = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.parent, name)


class GardenBackend(Backend):
    def __init__(self, ctx: Context) -> None:
        if ctx.mode != "native":
            raise InstallError("Garden native unit requires native mode")
        self.parent = ctx
        super().__init__(cast(Context, _UnitContext(ctx)))
        self.config_path = ctx.root / "garden" / "host.json"
        self.data_path = ctx.root / "garden" / "data"
        self.binary = ctx.root / "garden" / "bin" / "a2a"

    def _service_name(self) -> str:
        return f"cairn-install-{self.ctx.name}-garden.service"

    def _rollback_notice(self) -> str:
        return f"Rollback retained Garden configuration and data under {self.parent.root / 'garden'}."

    def preflight(self) -> None:
        from .garden import require_listener_available

        self.validate_ownership()
        unit = _unit_directory() / self._service_name()
        if unit.exists() or unit.is_symlink():
            self.parent.check_file(unit)
        self._validate_loaded_unit(require_expected=False, record_vendor_policy=True)
        if self.binary.exists() or self.binary.is_symlink():
            self._check_binary()
        else:
            self.parent.command(
                ["go", "version"], cwd=self.parent.source / "a2a", env=self._go_env()
            )
        if not self.is_running():
            require_listener_available(
                int(self.parent.state["garden"]["options"]["port"]), wildcard=True
            )

    def _unit_contents(self) -> str:
        cairn = f"cairn-install-{self.ctx.name}.service"
        return (
            "[Unit]\n"
            f"Description=Garden for Cairn installation {self.ctx.name}\n"
            f"Requires={cairn}\nAfter={cairn}\nPartOf={cairn}\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={_systemd_quote(str(self.binary))} host --config {_systemd_quote(str(self.config_path))}\n"
            "Restart=on-failure\nRestartSec=5s\nTimeoutStopSec=60s\n"
            "UMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    def garden_prepare(self) -> None:
        from .garden import gateway_config, write_json_config

        self.validate_ownership()
        self._validate_loaded_unit(require_expected=False, record_vendor_policy=True)
        for directory in (
            self.data_path,
            self.binary.parent,
            self.config_path.parent / "run",
        ):
            secure_directory(directory, private=True)
        self._prepare_binary()
        options = self.parent.state["garden"]["options"]
        config = gateway_config(
            self.parent,
            data_dir=str(self.data_path),
            cert_file=str(self.config_path.parent / "tls" / "server.crt"),
            key_file=str(self.config_path.parent / "tls" / "server.key"),
            daemon_url_file=str(self.config_path.parent / "run" / "daemon.url"),
            listen=f"0.0.0.0:{options['port']}",
            cairn_url=f"http://127.0.0.1:{self.parent.port}/memory/v1/diagnose",
        )
        write_json_config(self.parent, self.config_path, {"gateway": config})

    def _go_env(self) -> dict[str, str]:
        """Keep every Go side effect inside the instance tree blitz removes.

        Build and module caches are relocated so a 0555 module cache cannot
        block deletion; XDG_CONFIG_HOME keeps Go's own telemetry counters and
        configuration out of the operator's home directory.
        """
        garden_root = self.binary.parents[1]
        return {
            "CGO_ENABLED": "0",
            "GOCACHE": str(garden_root / "go-build"),
            "GOMODCACHE": str(garden_root / "go-mod"),
            "GOFLAGS": "-modcacherw",
            "XDG_CONFIG_HOME": str(garden_root / "go-config"),
        }

    def _prepare_binary(self) -> None:
        key = str(self.binary)
        owned = self.parent.state["owned_files"]
        intents = self.parent.state.setdefault("file_intents", {})
        if self.binary.exists() or self.binary.is_symlink():
            if key not in owned and key in intents:
                if (
                    hashlib.sha256(
                        read_owned(self.binary, 128 * 1024 * 1024)
                    ).hexdigest()
                    != intents[key]
                ):
                    raise InstallError("Interrupted Garden binary publication changed")
                owned[key] = intents[key]
                self.parent.save()
            self._check_binary()
            if self.binary.stat().st_mode & 0o777 != 0o755:
                raise InstallError("Garden executable permissions changed")
            return
        if key in owned:
            raise InstallError("Owned Garden executable disappeared")
        with tempfile.TemporaryDirectory(
            prefix=".build-", dir=self.binary.parent
        ) as temporary:
            output = Path(temporary) / "a2a"
            version = source_version(self.parent.source)
            # Same flags as a2a/Dockerfile and a2a/Makefile, so the binary
            # reports the release version. Go's caches stay inside the
            # instance tree, writable, so blitz's rmtree removes them.
            self.parent.command(
                [
                    "go",
                    "build",
                    "-trimpath",
                    "-mod=readonly",
                    "-ldflags",
                    f"-s -w -X github.com/veridian69/cairn/a2a/cmd.version={version}",
                    "-o",
                    str(output),
                    ".",
                ],
                cwd=self.parent.source / "a2a",
                env=self._go_env(),
                timeout=900,
            )
            digest = hashlib.sha256(read_owned(output, 128 * 1024 * 1024)).hexdigest()
            if key in intents and intents[key] != digest:
                raise InstallError("Garden build differs from interrupted publication")
            intents[key] = digest
            self.parent.save()
            output.chmod(0o755)
            with output.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(output, self.binary, follow_symlinks=False)
            directory = os.open(self.binary.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            owned[key] = digest
            self.parent.save()

    def garden_start(self) -> None:
        self._check_binary()
        self.start()

    def _check_binary(self) -> None:
        expected = self.parent.state["owned_files"].get(str(self.binary))
        actual = hashlib.sha256(read_owned(self.binary, 128 * 1024 * 1024)).hexdigest()
        if expected is None or actual != expected:
            raise InstallError("Garden executable is unowned or changed")

    def garden_stop(self) -> None:
        self.stop()

    def garden_open_endpoint(self) -> None:
        pass

    def garden_close_endpoint(self) -> None:
        pass
