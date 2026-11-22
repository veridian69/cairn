"""garden-upgrade hooks for --mode garden-server; the engine owns the journal.

Root installs only release files from the installer's stage, inside root-owned
directories. Garden data never passes through root: the Garden account streams
its cold copy to the installer and restores it into its own home (Sol r1 P1-1).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import garden, garden_server, sudo
from .core import (
    Context,
    InstallError,
    _fsync_directory,
    atomic_write,
    read_owned,
    secure_directory,
)

EMPTY = hashlib.sha256(b"").hexdigest()
BINARY = "/usr/local/bin/garden"


def release_key(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def release_artefact(installed: dict[str, str]) -> dict[str, str]:
    files = {
        path: value for path, value in installed.items() if sudo.release_path(path)
    }
    if not sudo.RELEASE_FIXED <= set(files):
        raise InstallError("Installed Garden release is incomplete")
    return {"binary_sha256": files[BINARY], "release_sha256": release_key(files)}


def _mode(path: str) -> int:
    return 0o755 if path.startswith("/usr/local/bin/") else 0o644


class ServerGarden:
    """GardenAdapter for system units installed through the fixed sudo table."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.server = garden_server._require(ctx)
        # S11 records list eight tools; every later sudo argv needs all eleven.
        garden_server.record_tools(ctx)

    @property
    def releases(self) -> Path:
        return self.ctx.root / "server-releases"

    def close(self) -> None:
        pass

    def _upgrade(self) -> dict[str, Any]:
        upgrade: dict[str, Any] = self.ctx.state["garden"].get("upgrade", {})
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", str(upgrade.get("id", ""))):
            raise InstallError("Garden upgrade journal is missing or invalid")
        return upgrade

    def validate_ownership(self) -> None:
        garden_server._installed(self.ctx)
        for kind in ("group", "user"):
            record = garden_server._account(self.ctx, kind)
            if record is None:
                raise InstallError(f"Recorded Garden {kind} disappeared")
            garden_server._validate_account(self.ctx, kind, record)

    def garden_preflight(self) -> None:
        """Checks before the journal exists, while Garden still runs."""
        ctx, server = self.ctx, self.server
        # Old snippets lack the S12 rows: write the new one before using them.
        garden_server.write_snippet(ctx)
        sudo.check_policy(ctx)
        if server["backup_hook"]:
            script = sudo.stage_root(ctx) / sudo.BACKUP_SCRIPT.lstrip("/")
            content = read_owned(script)
            if (
                hashlib.sha256(content).hexdigest()
                != server["installed"].get(sudo.BACKUP_SCRIPT)
                or f"if [ -e {sudo.FLAG} ]; then".encode() not in content
            ):
                raise InstallError(
                    "The installed garden-backup hook predates the upgrade guard "
                    f"({sudo.FLAG}); upgrade this Garden by hand as the guided "
                    "installation guide describes"
                )
        if sudo.digest(ctx, sudo.FLAG) is not None and "upgrade_flag" not in server:
            raise InstallError(
                f"{sudo.FLAG} exists but this installation did not create it; "
                "finish the other upgrade and remove the flag first"
            )
        if sudo.DATA_PATHS[0] not in sudo.data_present(ctx):
            raise InstallError("Garden data directory is missing")
        size = sudo.run(ctx, "data_size").split("\t", 1)[0].strip()
        if not size.isdigit():
            raise InstallError("Could not measure the Garden data directory")
        if int(size) > sudo.COPY_LIMIT:
            raise InstallError(
                "Garden data exceeds the 8 GiB cold-copy limit; upgrade cannot proceed"
            )
        if shutil.disk_usage(ctx.root).free < int(size) * 2:
            raise InstallError(
                "Garden upgrade needs free space of at least twice the data size"
            )

    # Releases are installer-owned trees of the release files only.

    def _write_release(self, key: str, payload: dict[str, bytes]) -> None:
        secure_directory(self.releases, private=True)
        temporary = self.releases / f".tmp-{key}"
        if temporary.exists() or temporary.is_symlink():
            secure_directory(temporary, private=True)
            shutil.rmtree(temporary)
        secure_directory(temporary, private=True)
        for path, data in payload.items():
            target = temporary / path.lstrip("/")
            for parent in reversed(target.parents):
                if parent.is_relative_to(temporary):
                    secure_directory(parent, private=True)
            atomic_write(target, data, _mode(path))
        dest = self.releases / key
        if dest.exists() or dest.is_symlink():
            # Callers publish only unrecorded keys: this is an interrupted
            # publication whose record was never saved. Replace it.
            secure_directory(dest, private=True)
            shutil.rmtree(dest)
        os.rename(temporary, dest)
        _fsync_directory(self.releases)

    def _release(self, key: str) -> dict[str, bytes]:
        recorded = self.server.get("releases", {}).get(key)
        if recorded is None:
            raise InstallError("Requested Garden release is not recorded")
        root = self.releases / key
        secure_directory(root, private=True)
        payload = {}
        for path, expected in recorded["files"].items():
            data = read_owned(root / path.lstrip("/"), sudo.BINARY_LIMIT)
            if hashlib.sha256(data).hexdigest() != expected:
                raise InstallError(f"Retained Garden release changed: {path}")
            payload[path] = data
        return payload

    def _check_version(self, binary: Path, version: str) -> None:
        reported = self.ctx.command([str(binary), "--version"]).strip()
        if reported != f"garden version {version}":
            raise InstallError("Garden executable reports the wrong version")

    def garden_stage(self, source: Path) -> dict[str, str]:
        ctx, server = self.ctx, self.server
        upgrade = self._upgrade()
        releases = server.setdefault("releases", {})
        current = {
            path: value
            for path, value in server["installed"].items()
            if sudo.release_path(path)
        }
        key = release_key(current)
        if key not in releases:
            # Retain the running release from the pinned stage for rollback.
            if garden_server._stage_hashes(ctx) != server["staged"] or any(
                server["staged"].get(path) != value for path, value in current.items()
            ):
                raise InstallError(
                    "Staged server files differ from the installed release"
                )
            stage = sudo.stage_root(ctx)
            self._write_release(
                key,
                {
                    path: read_owned(stage / path.lstrip("/"), sudo.BINARY_LIMIT)
                    for path in current
                },
            )
            releases[key] = {"version": upgrade["from"]["version"], "files": current}
            ctx.save()
        version = garden.source_version(source)
        with tempfile.TemporaryDirectory(prefix=".build-", dir=self.releases) as temp:
            root = Path(temp)
            garden_server.build_release(ctx, source, root)
            manifest = garden._document(read_owned(root / sudo.MANIFEST.lstrip("/")))
            paths = ["/" + relative for relative in manifest["files"]]
            paths.append(sudo.MANIFEST)
            if not all(sudo.release_path(path) for path in paths) or not (
                sudo.RELEASE_FIXED <= set(paths)
            ):
                raise InstallError("Built Garden release has unexpected files")
            payload = {
                path: read_owned(root / path.lstrip("/"), sudo.BINARY_LIMIT)
                for path in sorted(paths)
            }
        files = {
            path: hashlib.sha256(data).hexdigest() for path, data in payload.items()
        }
        target = release_key(files)
        if target not in releases:
            self._write_release(target, payload)
        self._check_version(self.releases / target / BINARY.lstrip("/"), version)
        releases[target] = {"version": version, "source": str(source), "files": files}
        ctx.save()
        # The target's deploy rows are now policy rows: prove them granted.
        garden_server.write_snippet(ctx)
        sudo.check_policy(ctx)
        return {"binary_sha256": files[BINARY], "release_sha256": target}

    def _restore_owned(self, upgrade: dict[str, Any]) -> None:
        aside, restored = sudo.DATA_PATHS[1:]
        present = sudo.data_present(self.ctx)
        if upgrade.get("aside") is None and (
            aside in present or (restored in present and "restore" not in upgrade)
        ):
            raise InstallError(
                f"{aside} or {restored} exists and is not this restore's; move it "
                "away as the Garden account, then run garden-upgrade again"
            )

    def garden_stop(self) -> None:
        ctx, server = self.ctx, self.server
        upgrade = self._upgrade()
        if upgrade["direction"] == "rollback" and upgrade["data_choice"] == "restore":
            self._restore_owned(upgrade)
        # Flag first: a hook that starts later exits; one already running shows
        # as active below (A3). /etc/garden is root-owned, so this survives reboot.
        server["upgrade_flag"] = "set"
        ctx.save()
        sudo.run(ctx, "flag_set")
        if sudo.digest(ctx, sudo.FLAG) != EMPTY:
            raise InstallError("Garden upgrade flag publication did not match")
        if server["backup_hook"]:
            state = ctx.command(
                [server["tools"]["systemctl"], "is-active", "garden-backup.service"],
                allowed=(0, 1, 2, 3, 4),
            ).strip()
            if state not in {"inactive", "failed"}:
                raise InstallError(
                    f"garden-backup.service is {state or 'unknown'}; wait for it to "
                    "finish, then run garden-upgrade again"
                )
        # Disabled units stay down across a reboot mid-swap or mid-restore (P1-3).
        garden_server._units(ctx, "stop")
        garden_server._units(ctx, "disable")

    def garden_start(self) -> None:
        garden_server._units(self.ctx, "start")
        garden_server._units(self.ctx, "enable")

    def garden_open_endpoint(self) -> None:
        pass

    def garden_close_endpoint(self) -> None:
        pass

    def garden_copy(self, dest: Path) -> None:
        ctx = self.ctx
        upgrade = self._upgrade()
        if dest != ctx.root / "garden-upgrades" / upgrade["id"] / "data.tar":
            raise InstallError(
                "Garden cold-copy destination does not match the upgrade"
            )
        secure_directory(dest.parent, private=True)
        key = str(dest)
        if dest.exists() or dest.is_symlink():
            # Published already; the engine validates it against its receipt.
            if key not in ctx.state["owned_files"]:
                # Linked before its receipt was saved: adopt it only if it
                # matches the intent recorded before publication.
                intent = ctx.state.get("file_intents", {}).get(key)
                fd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if (
                    intent is None
                    or digest != intent
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                ):
                    raise InstallError(f"Garden cold copy is unowned: {dest}")
                _fsync_directory(dest.parent)
                ctx.state["owned_files"][key] = digest
                ctx.save()
            return
        user = garden_server._account(ctx, "user")
        group = garden_server._account(ctx, "group")
        if user is None or group is None:
            raise InstallError("Recorded Garden account disappeared")
        with tempfile.TemporaryDirectory(prefix=".copy-", dir=dest.parent) as temp:
            output = Path(temp) / "data.tar"
            sudo.run(ctx, "data_take", stdout_path=output)
            try:
                with tarfile.open(output, "r:") as archive:
                    for member in archive:
                        # A2: nothing the Garden account does not own may enter
                        # a restore, even though only that account extracts it.
                        if member.uid != user.pw_uid or member.gid != group.gr_gid:
                            raise InstallError(
                                f"Garden cold copy has a foreign member: {member.name}"
                            )
            except tarfile.TarError as error:
                raise InstallError("Garden cold copy is not a valid tar") from error
            with output.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.fsync(stream.fileno())
            ctx.state.setdefault("file_intents", {})[key] = digest
            ctx.save()
            os.link(output, dest, follow_symlinks=False)
            _fsync_directory(dest.parent)
        ctx.state["owned_files"][key] = digest
        ctx.save()

    def garden_swap(self, artefact: dict[str, str]) -> None:
        ctx, server = self.ctx, self.server
        key = artefact.get("release_sha256", "")
        if (
            key not in server.get("releases", {})
            and not server.get("replacements")
            and release_artefact(server["installed"]) == artefact
        ):
            # An abort before the first stage retained anything: the installed
            # release is already the target, and nothing was changed.
            return
        payload = self._release(key)
        target = server["releases"][key]["files"]
        installed, staged = server["installed"], server["staged"]
        replacements = server.setdefault("replacements", {})
        stage = sudo.stage_root(ctx)
        writes = sorted(
            path
            for path in target
            if installed.get(path) != target[path] or path in replacements
        )
        # As install-server: the ownership manifest lands after its files.
        writes.sort(key=lambda path: path == sudo.MANIFEST)
        for path in writes:
            staged_path = stage / path.lstrip("/")
            if staged_path.exists() or staged_path.is_symlink():
                read_owned(staged_path, sudo.BINARY_LIMIT)
                staged_path.unlink()
            secure_directory(staged_path.parent, private=True)
            atomic_write(staged_path, payload[path], _mode(path))
            staged[path] = target[path]
            replacements[path] = {"old": installed.get(path), "new": target[path]}
            ctx.save()
            sudo.run(ctx, "file", path=path)
            if sudo.digest(ctx, path) != target[path]:
                raise InstallError(f"Server file publication did not match: {path}")
            installed[path] = target[path]
            del replacements[path]
            ctx.save()
        dropped = {
            path for path in [*installed, *replacements] if sudo.release_path(path)
        } - set(target)
        for path in sorted(dropped, reverse=True):
            replacements[path] = {"old": installed.get(path), "new": None}
            ctx.save()
            sudo.run(ctx, "rm", path=path)
            if sudo.digest(ctx, path) is not None:
                raise InstallError(f"Server file removal did not land: {path}")
            staged_path = stage / path.lstrip("/")
            if staged_path.exists() or staged_path.is_symlink():
                read_owned(staged_path, sudo.BINARY_LIMIT)
                staged_path.unlink()
            installed.pop(path, None)
            staged.pop(path, None)
            del replacements[path]
            ctx.save()
        sudo.run(ctx, "reload")
        self._check_version(Path(BINARY), server["releases"][key]["version"])

    def _extract(self, copy: Path, upgrade: dict[str, Any]) -> None:
        upgrade["restore"] = "extracting"
        self.ctx.save()
        sudo.run(self.ctx, "data_stage")
        sudo.run(self.ctx, "data_extract", stdin_path=copy)
        # Exit 1 (a difference) fails the command, as any other error does.
        sudo.run(self.ctx, "data_compare", stdin_path=copy)

    def garden_restore_data(self, copy: Path) -> None:
        ctx = self.ctx
        upgrade = self._upgrade()
        data, aside, restored = sudo.DATA_PATHS
        self._restore_owned(upgrade)
        present = sudo.data_present(ctx)
        if aside in present and data in present:
            if restored in present:
                raise InstallError(
                    f"Garden restore found {data}, {aside} and {restored} together; "
                    "inspect them by hand"
                )
            return
        if shutil.disk_usage("/var/lib/garden").free < upgrade["cold_copy"]["bytes"]:
            raise InstallError("Garden restore needs free space for the cold copy")
        if aside in present:
            # Moved aside before an interruption; re-extracting is idempotent.
            self._extract(copy, upgrade)
        else:
            if data not in present:
                raise InstallError(
                    f"Garden data is missing; refusing to restore: {data}"
                )
            self._extract(copy, upgrade)
            upgrade["aside"] = aside
            ctx.save()
            sudo.run(ctx, "data_aside")
        sudo.run(ctx, "data_place")

    def garden_prune(self, keep: Sequence[dict[str, str]]) -> None:
        retained = {item.get("release_sha256") for item in keep if item}
        releases = self.server.get("releases", {})
        if self.releases.exists():
            secure_directory(self.releases, private=True)
            for entry in sorted(self.releases.iterdir()):
                if entry.name in retained:
                    continue
                secure_directory(entry, private=True)
                shutil.rmtree(entry)
            _fsync_directory(self.releases)
        for key in [key for key in releases if key not in retained]:
            del releases[key]
        self.ctx.save()

    def garden_upgrade_closed(self) -> None:
        sudo.run(self.ctx, "flag_clear")
        if sudo.digest(self.ctx, sudo.FLAG) is not None:
            raise InstallError("Garden upgrade flag removal did not land")
        self.server.pop("upgrade_flag", None)
        self.ctx.save()
