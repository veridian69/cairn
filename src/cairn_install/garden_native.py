"""Garden's native unit, using the installer's existing ownership journal."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from .core import Context, InstallError, read_owned, secure_directory
from .destruction import check_instance_tree
from .garden import source_version
from .native import (
    _UNIT_RESOURCE,
    Backend,
    _fsync_directory,
    _systemd_quote,
    _unit_directory,
)

# Executables are recorded with this digest limit; the file-read cap is smaller.
_BINARY_LIMIT = 128 * 1024 * 1024


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
        self.binary = ctx.root / "garden" / "bin" / "garden"

    def _service_name(self) -> str:
        return f"cairn-install-{self.ctx.name}-garden.service"

    def _rollback_notice(self) -> str:
        return f"Rollback retained Garden configuration and data under {self.parent.root / 'garden'}."

    def preflight(self) -> None:
        self.validate_ownership()
        unit = _unit_directory() / self._service_name()
        if unit.exists() or unit.is_symlink():
            self.parent.check_file(unit)
        self._validate_loaded_unit(require_expected=False, record_vendor_policy=True)
        if self.binary.exists() or self.binary.is_symlink():
            self._check_binary()
        else:
            self.parent.command(
                ["go", "version"], cwd=self.parent.source / "garden", env=self._go_env()
            )
        if not self.is_running():
            self._require_listener()

    def _require_listener(self) -> None:
        from .garden import listen_address, require_listener_available

        options = self.parent.state["garden"]["options"]
        port = int(options["port"])
        # Legacy options have no listen_address; keep the wildcard probe as before.
        if "listen_address" in options:
            require_listener_available(
                port, wildcard=True, address=listen_address(options)
            )
        else:
            require_listener_available(port, wildcard=True)

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
        from .garden import gateway_config, listen, retention, write_json_config

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
            listen=listen(options, options["port"]),
            cairn_url=f"http://127.0.0.1:{self.parent.port}/memory/v1/diagnose",
        )
        # Stream retention is top-level in host.json; legacy installs omit it.
        document: dict[str, Any] = {"gateway": config}
        stream = retention(options, None)
        if stream is not None:
            document["stream"] = stream
        write_json_config(self.parent, self.config_path, document)

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
                    hashlib.sha256(read_owned(self.binary, _BINARY_LIMIT)).hexdigest()
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
            output = Path(temporary) / "garden"
            self._build_binary(self.parent.source, output)
            digest = hashlib.sha256(read_owned(output, _BINARY_LIMIT)).hexdigest()
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

    def _build_binary(self, source: Path, output: Path) -> None:
        version = source_version(source)
        self.parent.command(
            [
                "go",
                "build",
                "-trimpath",
                "-mod=readonly",
                "-ldflags",
                f"-s -w -X github.com/veridian69/cairn/garden/cmd.version={version}",
                "-o",
                str(output),
                ".",
            ],
            cwd=source / "garden",
            env=self._go_env(),
            timeout=900,
        )

    def _upgrade(self) -> dict[str, Any]:
        upgrade: dict[str, Any] = self.parent.state["garden"].get("upgrade", {})
        self._component(upgrade.get("id"))
        return upgrade

    @staticmethod
    def _component(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
        ):
            raise InstallError("Invalid Garden operation or snapshot id")
        return value

    @staticmethod
    def _digest(path: Path) -> str:
        """Stream large copies without following links or opening special files."""
        secure_directory(path.parent)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise InstallError(f"Expected an owned regular file: {path}")
                return hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as error:
            raise InstallError(f"Cannot safely read Garden file: {path}") from error

    def _publish(self, output: Path, dest: Path, *, mode: int = 0o600) -> None:
        digest = self._digest(output)
        key = str(dest)
        intents = self.parent.state.setdefault("file_intents", {})
        if key in intents and intents[key] != digest:
            raise InstallError(
                f"Garden output differs from interrupted publication: {dest}"
            )
        intents[key] = digest
        self.parent.save()
        output.chmod(mode)
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(output, dest, follow_symlinks=False)
        _fsync_directory(dest.parent)
        self.parent.state["owned_files"][key] = digest
        self.parent.save()

    def garden_stage(self, source: Path) -> dict[str, str]:
        upgrade = self._upgrade()
        staged = self.binary.parent / f".garden-{upgrade['id']}"
        secure_directory(staged.parent, private=True)
        if staged.exists() or staged.is_symlink():
            self._verify_owned(staged)
        else:
            with tempfile.TemporaryDirectory(
                prefix=".build-", dir=staged.parent
            ) as temp:
                output = Path(temp) / "garden"
                self._build_binary(source, output)
                output.chmod(0o755)
                self._check_version(output, source)
                self._publish(output, staged, mode=0o755)
        self._check_version(staged, source)
        return {"binary_sha256": self._digest(staged)}

    def _check_version(self, binary: Path, source: Path) -> None:
        version = self.parent.command([str(binary), "--version"]).strip()
        if version != f"garden version {source_version(source)}":
            raise InstallError("Staged Garden executable reports the wrong version")

    @staticmethod
    def _tree(path: Path) -> list[Path]:
        if not path.exists() or path.is_symlink():
            raise InstallError(f"Garden tree is missing or is a symlink: {path}")
        secure_directory(path, private=True)

        def refuse(error: OSError) -> None:
            raise InstallError(
                f"Cannot inspect Garden tree: {error.filename}"
            ) from error

        result = [path]
        for current, directories, files in os.walk(
            path, followlinks=False, onerror=refuse
        ):
            directories.sort()
            for name in sorted([*directories, *files]):
                entry = Path(current) / name
                info = entry.lstat()
                if info.st_uid != os.getuid() or not (
                    stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                ):
                    raise InstallError(
                        f"Garden tree has a non-regular or foreign member: {entry}"
                    )
                result.append(entry)
        return result

    @staticmethod
    def _copy_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
        members = archive.getmembers()
        seen: set[str] = set()
        for member in members:
            path = Path(member.name)
            if (
                not (member.isdir() or member.isfile())
                or path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "data"
                or str(path) != member.name.rstrip("/")
                or member.name in seen
            ):
                raise InstallError(f"Unsafe Garden cold-copy member: {member.name}")
            seen.add(member.name)
        if not any(
            member.name == "data/state.db" and member.isfile() for member in members
        ):
            raise InstallError("Garden cold copy is missing data/state.db")
        return members

    def garden_copy(self, dest: Path) -> None:
        upgrade = self._upgrade()
        expected = self.parent.root / "garden-upgrades" / upgrade["id"] / "data.tar"
        if dest != expected:
            raise InstallError(
                "Garden cold-copy destination does not match the upgrade"
            )
        secure_directory(dest.parent, private=True)
        if dest.exists() or dest.is_symlink():
            self._verify_owned(dest)
            with tarfile.open(dest, "r:") as archive:
                self._copy_members(archive)
            # Publication may have landed without its directory sync.
            _fsync_directory(dest.parent)
            return
        entries = self._tree(self.data_path)
        with tempfile.TemporaryDirectory(prefix=".copy-", dir=dest.parent) as temp:
            output = Path(temp) / "data.tar"
            with tarfile.open(output, "w", dereference=True) as archive:
                for path in entries:
                    archive.add(
                        path,
                        arcname=str(path.relative_to(self.data_path.parent)),
                        recursive=False,
                    )
            with tarfile.open(output, "r:") as archive:
                self._copy_members(archive)
            self._publish(output, dest)

    def garden_swap(self, artefact: dict[str, str]) -> None:
        upgrade = self._upgrade()
        target = artefact.get("binary_sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", target):
            raise InstallError("Invalid Garden binary digest")
        previous = self.binary.with_name("garden.previous")
        staged = self.binary.with_name(f".garden-{upgrade['id']}")
        replacements = self.parent.state.setdefault("file_replacements", {})
        key = str(self.binary)
        pending = replacements.get(key)
        if pending is not None:
            observed = self._digest(self.binary)
            if observed not in {pending["old"], pending["new"]}:
                raise InstallError("Interrupted Garden swap left unexpected content")
            if stat.S_IMODE(self.binary.stat().st_mode) != 0o755:
                raise InstallError("Garden executable permissions changed")
            if observed == pending["new"]:
                self._verify_owned(previous)
                if self._digest(previous) != pending["old"]:
                    raise InstallError(
                        "Interrupted Garden swap lost the previous release"
                    )
                _fsync_directory(self.binary.parent)
                self.parent.state["owned_files"][key] = observed
                self.parent.state.setdefault("file_intents", {})[key] = observed
                del replacements[key]
                self._forget(str(staged))
                pending = None
            elif target == pending["old"]:
                # An abort before rename needs no binary mutation.
                del replacements[key]
                self.parent.save()
                return
            elif target != pending["new"]:
                raise InstallError(
                    "Garden swap target differs from the pending replacement"
                )
        if pending is None:
            self._verify_owned(self.binary)
            old = self._digest(self.binary)
            if old == target:
                return
            pending = {"old": old, "new": target}
            # Prove the target and any previous release before recording intent.
            candidates = [
                path
                for path in (staged, previous)
                if path.exists() or path.is_symlink()
            ]
            for path in candidates:
                self._verify_owned(path)
            if not any(self._digest(path) == target for path in candidates):
                raise InstallError("Requested Garden release is not staged or previous")
            replacements[key] = pending
            self.parent.save()
        self._verify_owned(self.binary, pending_old=pending["old"])
        for path in (staged, previous):
            if path.exists() or path.is_symlink():
                self._verify_owned(path)
        if not staged.exists() or self._digest(staged) != target:
            if not previous.exists() or self._digest(previous) != target:
                raise InstallError("Requested Garden release disappeared")
            if staged.exists():
                staged.unlink()
                self._forget(str(staged))
            self.parent.state.setdefault("file_intents", {})[str(staged)] = target
            self.parent.save()
            os.link(previous, staged, follow_symlinks=False)
            _fsync_directory(staged.parent)
            self.parent.state["owned_files"][str(staged)] = target
            self.parent.save()
        if previous.exists() and self._digest(previous) != pending["old"]:
            previous.unlink()
            _fsync_directory(previous.parent)
            self._forget(str(previous))
        if not previous.exists():
            self.parent.state["owned_files"].pop(str(previous), None)
            self.parent.state.setdefault("file_intents", {})[str(previous)] = pending[
                "old"
            ]
            self.parent.save()
            os.link(self.binary, previous, follow_symlinks=False)
            _fsync_directory(previous.parent)
        self.parent.state["owned_files"][str(previous)] = pending["old"]
        self.parent.save()
        os.replace(staged, self.binary)
        _fsync_directory(self.binary.parent)
        self.parent.state["owned_files"][key] = target
        self.parent.state.setdefault("file_intents", {})[key] = target
        del replacements[key]
        self._forget(str(staged))

    def garden_restore_data(self, copy: Path) -> None:
        upgrade = self._upgrade()
        self._verify_owned(copy)
        receipt = upgrade.get("cold_copy", {})
        if (
            receipt.get("sha256") != self._digest(copy)
            or receipt.get("bytes") != copy.stat().st_size
        ):
            raise InstallError("Garden cold copy differs from the upgrade receipt")
        aside = self.data_path.with_name(f"data.pre-restore-{upgrade['id']}")
        if upgrade.get("aside") not in (None, str(aside)):
            raise InstallError("Garden restore aside path differs from the upgrade")
        with tarfile.open(copy, "r:") as archive:
            members = self._copy_members(archive)
            with tempfile.TemporaryDirectory(
                prefix=".restore-", dir=self.data_path.parent
            ) as temp:
                restored = Path(temp) / "data"
                secure_directory(restored, private=True)
                for member in members:
                    dest = Path(temp) / member.name
                    if member.isdir():
                        secure_directory(dest, private=True)
                    else:
                        secure_directory(dest.parent, private=True)
                        stream = archive.extractfile(member)
                        assert stream is not None
                        with stream, dest.open("xb") as output:
                            os.fchmod(output.fileno(), 0o600)
                            shutil.copyfileobj(stream, output)
                            output.flush()
                            os.fsync(output.fileno())
                for path in reversed(self._tree(restored)):
                    if path.is_dir():
                        _fsync_directory(path)
                if aside.exists() or aside.is_symlink():
                    if upgrade.get("aside") != str(aside):
                        raise InstallError("Garden restore aside directory is unowned")
                    self._tree(aside)
                    if self.data_path.exists() or self.data_path.is_symlink():
                        # Publication landed before its engine phase was saved.
                        if self._tree_digests(self.data_path) != self._tree_digests(
                            restored
                        ):
                            raise InstallError(
                                "Garden data changed after interrupted restore"
                            )
                        _fsync_directory(self.data_path.parent)
                        return
                else:
                    self._tree(self.data_path)
                    upgrade["aside"] = str(aside)
                    self.parent.save()
                    os.rename(self.data_path, aside)
                    _fsync_directory(aside.parent)
                os.rename(restored, self.data_path)
                _fsync_directory(self.data_path.parent)

    def _tree_digests(self, root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): self._digest(path)
            if path.is_file()
            else "directory"
            for path in self._tree(root)
        }

    def garden_snapshot(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name):
            raise InstallError("Invalid Garden snapshot name")
        self._verify_owned(self.binary)
        self._verify_owned(self.config_path)
        root = self.data_path.parent / "backups"
        secure_directory(root, private=True)
        # Snapshot ids are <UTC stamp>-<name>; a bare suffix would let "daily"
        # match "<stamp>-weekly-daily".
        candidates = [
            path.name
            for path in root.iterdir()
            if re.fullmatch(rf"\d{{8}}T\d{{6}}Z-{re.escape(name)}", path.name)
        ]
        if len(candidates) > 1:
            raise InstallError("Garden snapshot recovery is ambiguous")
        snapshots = self.ctx.state["resources"].setdefault("snapshots", {})
        receipt = snapshots.get(name)
        if receipt is None:
            receipt = {}
            snapshots[name] = receipt
            self.parent.save()
        if "id" not in receipt:
            if not candidates:
                output = self.parent.command(
                    [
                        str(self.binary),
                        "snapshot",
                        "create",
                        "--host-config",
                        str(self.config_path),
                        "--backup-dir",
                        str(root),
                        name,
                    ],
                    private=True,
                )
                match = re.fullmatch(r"snapshot created: (\S+) \([^\n]+\)\s*", output)
                if match is None:
                    raise InstallError("Garden snapshot returned an invalid receipt")
                candidates = [self._component(match[1])]
            if len(candidates) != 1:
                raise InstallError("Garden snapshot recovery is ambiguous")
            snapshot_id = candidates[0]
            if not re.fullmatch(
                r"[0-9]{8}T[0-9]{6}Z-" + re.escape(name) + r"(?:-[0-9]+)?", snapshot_id
            ):
                raise InstallError("Garden snapshot id does not match its name")
            receipt["files"] = self._tree_digests(root / snapshot_id)
            receipt["id"] = snapshot_id
            self.parent.save()
        self._snapshot_receipt(receipt["id"])
        return str(receipt["id"])

    def _snapshot_receipt(self, snapshot_id: str) -> tuple[Path, dict[str, Any]]:
        self._component(snapshot_id)
        snapshots = self.ctx.state["resources"].get("snapshots", {})
        receipt = next(
            (item for item in snapshots.values() if item.get("id") == snapshot_id), None
        )
        if receipt is None:
            raise InstallError("Garden snapshot is not owned")
        path = self.data_path.parent / "backups" / snapshot_id
        if path.exists() or path.is_symlink():
            observed = self._tree_digests(path)
            if receipt.get("deleting"):
                valid = all(
                    receipt["files"].get(key) == digest
                    for key, digest in observed.items()
                )
            else:
                valid = observed == receipt["files"]
            if not valid:
                raise InstallError("Garden snapshot changed")
        return path, receipt

    def garden_export(self, snapshot_id: str, dest: Path) -> None:
        source, receipt = self._snapshot_receipt(snapshot_id)
        if receipt.get("deleting"):
            raise InstallError("Garden snapshot deletion is in progress")
        if not source.exists():
            raise InstallError("Garden snapshot disappeared")
        secure_directory(dest, private=True)
        if source == dest or source in dest.parents or dest in source.parents:
            raise InstallError("Garden snapshot export overlaps its source")
        for path in self._tree(source):
            target = dest / path.relative_to(source)
            if path.is_dir():
                secure_directory(target, private=True)
            elif target.exists() or target.is_symlink():
                self._verify_owned(target)
                if self._digest(target) != self._digest(path):
                    raise InstallError("Garden snapshot export differs from its source")
            else:
                with tempfile.TemporaryDirectory(
                    prefix=".export-", dir=target.parent
                ) as temp:
                    output = Path(temp) / "file"
                    shutil.copyfile(path, output, follow_symlinks=False)
                    self._publish(output, target)

    def garden_prune(self, keep: Sequence[dict[str, str]]) -> None:
        retained = {item.get("binary_sha256") for item in keep}
        for key in sorted(self._owned_keys()):
            path = Path(key)
            if path.parent != self.binary.parent or not (
                path.name.startswith(".garden-") or path.name == "garden.previous"
            ):
                continue
            if path.exists() or path.is_symlink():
                self._verify_owned(path)
                if self._digest(path) in retained:
                    continue
                path.unlink()
                _fsync_directory(path.parent)
            self._forget(key)

    def garden_snapshot_delete(self, snapshot_id: str) -> None:
        path, receipt = self._snapshot_receipt(snapshot_id)
        if path.exists():
            check_instance_tree(self.parent)
            receipt["deleting"] = True
            self.parent.save()
            shutil.rmtree(path)
            _fsync_directory(path.parent)
        # Retain the receipt so a retry distinguishes absent from unowned.
        receipt["deleted"] = True
        self.parent.save()

    def garden_start(self) -> None:
        self._check_binary()
        self.start()

    def _check_binary(self) -> None:
        expected = self.parent.state["owned_files"].get(str(self.binary))
        actual = hashlib.sha256(read_owned(self.binary, _BINARY_LIMIT)).hexdigest()
        if expected is None or actual != expected:
            raise InstallError("Garden executable is unowned or changed")

    def garden_stop(self) -> None:
        self.stop()

    def garden_open_endpoint(self) -> None:
        pass

    def garden_close_endpoint(self) -> None:
        pass

    def garden_attach(self) -> None:
        """Check-only: fail before any change if garden_prepare cannot succeed.

        Garden is its own user unit, so Cairn's runtime needs no change here.
        """
        source = self.parent.source / "garden"
        if not source.is_dir() or source.is_symlink():
            raise InstallError(f"Garden source directory is missing: {source}")
        unit = _unit_directory() / self._service_name()
        if unit.exists() or unit.is_symlink():
            self._verify_owned(unit)
        if self.binary.exists() or self.binary.is_symlink():
            self._verify_owned(self.binary)
        else:
            self.parent.command(["go", "version"], cwd=source, env=self._go_env())
        state = self.parent.command(
            ["systemctl", "--user", "is-system-running"],
            cwd=self.parent.directory,
            allowed=(0, 1),
        ).strip()
        if state not in {"running", "degraded"}:
            raise InstallError("Garden needs a running systemd user manager")
        # A foreign unit loaded under Garden's name would make prepare refuse,
        # whether or not the expected unit file exists yet.
        self._validate_loaded_unit(require_expected=False)
        receipt = self.ctx.state["resources"].get(_UNIT_RESOURCE)
        if receipt is None or receipt.get("status") == "removed":
            self._require_listener()
        self.parent.note("Garden needs no Cairn attachment; prerequisites verified.")

    def garden_detach(self, *, delete_data: bool) -> None:
        """Remove Garden's unit, binary, configuration and copies; Cairn is untouched.

        Owned files are proved unchanged before the unit stops, so a refusal
        leaves Garden running. Absent files are already done, so rerunning an
        interrupted detach resumes it. The data directory stays unless
        delete_data is set.
        """
        unit = self._blitz_unit_inventory()
        paths = self._detach_paths()
        for path in paths:
            if path.exists() or path.is_symlink():
                self._verify_owned(path)
        if delete_data:
            self._data_delete_paths()
        if unit is not None:
            self._blitz_unit(*unit)
        for path in paths:
            if path.exists() or path.is_symlink():
                self._verify_owned(path)
                path.unlink()
                _fsync_directory(path.parent)
            self._forget(str(path))
        if delete_data:
            self._delete_data()
        action = "deleted" if delete_data else "kept"
        self.parent.note(
            f"Detached Garden runtime; {action} data under {self.data_path}."
        )

    def _verify_owned(self, path: Path, *, pending_old: str | None = None) -> None:
        """Read-only proof that path is the owned file the journal records.

        Unlike Context.check_file this promotes nothing, streams large files,
        and checks the modes the installer publishes (0755 binaries, 0644 unit).
        """
        key = str(path)
        if path.is_symlink():
            raise InstallError(f"Owned file is a symlink: {path}")
        if pending_old is None and key in self.parent.state.get(
            "file_replacements", {}
        ):
            raise InstallError(
                f"Owned file has an interrupted replacement; resume the operation: {path}"
            )
        intents = self.parent.state.get("file_intents", {})
        expected = (
            pending_old or self.parent.state["owned_files"].get(key) or intents.get(key)
        )
        if expected is None:
            raise InstallError(f"File is unowned: {path}")
        if self._digest(path) != expected:
            raise InstallError(f"Owned file changed; refusing mutation: {path}")
        mode = 0o755 if path.parent == self.binary.parent else None
        if path == _unit_directory() / self._service_name():
            mode = 0o644
        if mode is not None and stat.S_IMODE(path.stat().st_mode) != mode:
            raise InstallError(f"Owned file permissions changed: {path}")

    def _owned_keys(self) -> set[str]:
        return set(self.parent.state["owned_files"]) | set(
            self.parent.state.get("file_intents", {})
        )

    def _detach_paths(self) -> list[Path]:
        """Installer-owned Garden files, excluding the data directory."""
        root = self.parent.root / "garden"
        credentials = self.parent.root / "credentials" / "garden"
        unit = _unit_directory() / self._service_name()
        selected: list[Path] = []
        for key in sorted(self._owned_keys()):
            path = Path(key)
            if (
                path == unit
                or path.is_relative_to(credentials)
                or (
                    path.is_relative_to(root)
                    and not path.is_relative_to(self.data_path)
                    and not path.is_relative_to(root / "backups")
                    and not any(
                        part.startswith("data.pre-restore-")
                        for part in path.relative_to(root).parts
                    )
                    and path != self.binary.with_name("garden.previous")
                )
            ):
                selected.append(path)
        return selected

    def _forget(self, key: str) -> None:
        self.parent.state["owned_files"].pop(key, None)
        self.parent.state.get("file_intents", {}).pop(key, None)
        self.parent.save()

    def _data_delete_paths(self) -> list[Path]:
        """Prove all data deletion roots before detach stops the runtime."""
        garden_root = self.data_path.parent
        paths = [
            self.data_path,
            self.parent.root / "garden-upgrades",
            garden_root / "backups",
        ]
        if garden_root.exists():
            secure_directory(garden_root)
            paths.extend(garden_root.glob("data.pre-restore-*"))
        previous = self.binary.with_name("garden.previous")
        if previous.exists() or previous.is_symlink():
            self._verify_owned(previous)
        # Validate every root before deleting any of them.
        check_instance_tree(self.parent)
        for path in paths:
            if path.exists() or path.is_symlink():
                secure_directory(path, private=True)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise InstallError("This platform cannot safely remove Garden data")
        return paths

    def _delete_data(self) -> None:
        """Remove Garden data and retained upgrade copies, never following links."""
        paths = self._data_delete_paths()
        previous = self.binary.with_name("garden.previous")
        keys = [
            key
            for key in self._owned_keys()
            if any(Path(key).is_relative_to(path) for path in paths)
            or Path(key) == previous
        ]
        for path in paths:
            if path.exists():
                shutil.rmtree(path)
                _fsync_directory(path.parent)
        if previous.exists():
            previous.unlink()
            _fsync_directory(previous.parent)
        for key in keys:
            self._forget(key)
