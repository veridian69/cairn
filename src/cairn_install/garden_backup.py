"""Resumable, verified Garden host-layout archives."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
import stat
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .core import (
    Context,
    InstallError,
    atomic_write,
    open_context,
    open_read_context,
    read_owned,
    secure_directory,
    sync_regular_file,
)

if TYPE_CHECKING:
    from .workflow import GardenAdapter


def _archive_tools() -> ModuleType:
    # Use the verifier shipped with this installer, not an older source record:
    # the source tree's copy, or the one the wheel bundles.
    here = Path(__file__).resolve()
    candidates = (here.parents[2] / "scripts" / "backup", here.parent / "backup_tools")
    found = [path for path in candidates if (path / "garden_common.py").is_file()]
    if not found:
        raise InstallError("Garden archive verifier is missing from this installer")
    directory = str(found[0])
    sys.path.insert(0, directory)
    try:
        return importlib.import_module("garden_common")
    finally:
        sys.path.remove(directory)


def _check(ctx: Context, *, output: Path | None = None) -> None:
    if ctx.mode == "kubernetes":
        raise InstallError(
            "Kubernetes garden-backup arrives in a later release", "invalid_arguments"
        )
    if ctx.mode == "garden-server":
        if output is not None:
            raise InstallError(
                "--output is not valid for garden-server; backups use backup_hook.export_dir",
                "invalid_arguments",
            )
        if not ctx.state["server"]["backup_hook"]:
            raise InstallError(
                "garden-server backup requires backup_hook", "invalid_arguments"
            )
        return
    if ctx.mode not in {"native", "docker"}:
        raise InstallError(
            "Garden backup needs native or docker mode", "invalid_arguments"
        )
    garden = ctx.state.get("garden")
    if not isinstance(garden, dict):
        raise InstallError("No managed Garden installation to back up")
    for operation in ("upgrade", "add", "remove"):
        pending = garden.get(operation)
        if isinstance(pending, dict) and pending.get("phase") != "complete":
            command = "resume" if operation == "add" else f"garden-{operation}"
            raise InstallError(
                f"Garden {operation} is in progress; run {command} --name {ctx.name} again"
            )
    backup = garden.get("backup")
    recovering = isinstance(backup, dict) and (
        backup.get("phase") != "complete" or backup.get("retry_complete")
    )
    if not recovering and ctx.state["status"] != "verified":
        raise InstallError(
            f"Garden backup needs a verified installation; run resume --name {ctx.name} first"
        )
    source = garden.get("source")
    if isinstance(source, dict):
        _require_host_config(source.get("version"))
    # Garden installed with Cairn has no source record yet; _backup proves it.
    label = _label(ctx.name)
    if not re.fullmatch(r"[a-z0-9]{1,16}", label):
        raise InstallError(
            f"Garden backup cannot form a label from {ctx.name!r}: {label!r} is not "
            "1–16 letters/digits after removing other characters",
            "invalid_arguments",
        )


def _label(name: str) -> str:
    # Snapshot IDs use "-" as their separator, so the label keeps only [a-z0-9].
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _generations(ctx: Context, adapter: GardenAdapter) -> dict[str, str]:
    from . import garden

    try:
        adapter.garden_open_endpoint()
        garden.verify(ctx)
    finally:
        adapter.garden_close_endpoint()
    verified = ctx.state["garden"].get("verification", {})
    generations = {name: status.get("generation") for name, status in verified.items()}
    if not generations or any(
        not isinstance(value, str) or not value for value in generations.values()
    ):
        raise InstallError("Garden verification has no generation")
    return generations


def _deployment(ctx: Context) -> dict[str, Path]:
    from .native import _unit_directory

    root = ctx.root / "garden"
    files = {
        name: root / name for name in ("host.json", "tls/server.crt", "tls/server.key")
    }
    if (root / "tls/ca.crt").exists() or (root / "tls/ca.crt").is_symlink():
        files["tls/ca.crt"] = root / "tls/ca.crt"
    if ctx.mode == "native":
        name = f"cairn-install-{ctx.name}-garden.service"
        files[f"unit/{name}"] = _unit_directory() / name
    else:
        files["compose.yaml"] = ctx.root / "compose.yaml"
    for path in files.values():
        ctx.check_file(path)
    return files


def _digest(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise InstallError(
                "Garden archive must be an owned regular file with mode 0600"
            )
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_archive(archive: Path, backup: dict[str, Any], tools: ModuleType) -> str:
    digest = _digest(archive)
    read_owned(archive.with_suffix(".tar.sha256"))
    doc = tools.verify_garden(archive.parent, archive.stem)
    if (
        doc["id"] != backup["snapshot_id"]
        or doc.get("config_file") != "host.json"
        or (backup["sha256"] is not None and digest != backup["sha256"])
    ):
        raise InstallError("Garden archive differs from the backup journal")
    return digest


def _publish(source: Path, destination: Path) -> None:
    os.link(source, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _archive(
    ctx: Context, adapter: GardenAdapter, backup: dict[str, Any], tools: ModuleType
) -> str:
    archive = Path(backup["archive"])
    sidecar = archive.with_suffix(".tar.sha256")
    # The digest is saved before publication, so a retry can recognise its tar.
    if archive.exists() or archive.is_symlink():
        if backup["sha256"] is None or _digest(archive) != backup["sha256"]:
            raise InstallError("Garden archive differs from the backup journal")
        if not (sidecar.exists() or sidecar.is_symlink()):
            with tempfile.TemporaryDirectory(
                prefix=".garden-checksum-", dir=archive.parent
            ) as temporary:
                staged_sidecar = Path(temporary) / sidecar.name
                atomic_write(
                    staged_sidecar, f"{backup['sha256']}  {archive.name}\n".encode()
                )
                _publish(staged_sidecar, sidecar)
        return _verify_archive(archive, backup, tools)
    if sidecar.exists() or sidecar.is_symlink():
        raise InstallError("Garden checksum differs from the backup journal")
    with tempfile.TemporaryDirectory(
        prefix=".garden-export-", dir=archive.parent
    ) as temporary:
        stage = Path(temporary)
        snapshot = stage / backup["snapshot_id"]
        snapshot.mkdir(mode=0o700)
        adapter.garden_export(backup["snapshot_id"], snapshot)
        members = {backup["snapshot_id"]: snapshot, **_deployment(ctx)}
        for path in sorted(snapshot.rglob("*")):
            members[
                f"{backup['snapshot_id']}/{path.relative_to(snapshot).as_posix()}"
            ] = path
        staged_tar = stage / archive.name
        with staged_tar.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            with tarfile.open(fileobj=output, mode="w") as tar:
                for member, path in members.items():
                    info = path.lstat()
                    if info.st_uid != os.getuid() or not (
                        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                    ):
                        raise InstallError(f"Unsafe Garden archive member: {path}")
                    entry = tar.gettarinfo(str(path), arcname=member)
                    if not (entry.isdir() or entry.isfile()):
                        raise InstallError(f"Unsafe Garden archive member: {path}")
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mode = 0o700 if entry.isdir() else 0o600
                    if entry.isdir():
                        tar.addfile(entry)
                    else:
                        with path.open("rb") as stream:
                            tar.addfile(entry, stream)
            output.flush()
            sync_regular_file(output.fileno())
        digest = _digest(staged_tar)
        checksum = f"{digest}  {archive.name}\n".encode()
        atomic_write(staged_tar.with_suffix(".tar.sha256"), checksum)
        _verify_archive(staged_tar, {**backup, "sha256": None}, tools)
        backup["sha256"] = digest
        ctx.save()
        # Exclusive publication cannot replace an unrelated tar or checksum.
        _publish(staged_tar, archive)
        _publish(staged_tar.with_suffix(".tar.sha256"), sidecar)
    return _verify_archive(archive, backup, tools)


def _require_host_config(version: object) -> None:
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?",
        version if isinstance(version, str) else "",
    )
    if not match or tuple(map(int, match.groups())) < (0, 8, 0):
        raise InstallError(
            "Garden backup requires --host-config support (0.8.0); run garden-upgrade first"
        )


def _backup(ctx: Context, output: Path) -> dict[str, Any]:
    from . import workflow

    tools = _archive_tools()
    garden = ctx.state["garden"]
    backup = garden.get("backup")
    adapter = workflow.backend(ctx)
    finishing = False
    recovery_required = False
    active = backup is not None and (
        backup["phase"] != "complete" or backup.get("retry_complete")
    )
    try:
        hooks = workflow._garden_adapter(ctx, adapter)
        if backup is None or (
            backup["phase"] == "complete" and not backup.get("retry_complete")
        ):
            if garden.get("source") is None:
                from .garden_upgrade import _source

                # As garden-upgrade's back-fill: the running Garden must
                # provably come from this Cairn source.
                _require_host_config(_source(ctx, adapter)["version"])
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            archive = output / f"garden-backup-{stamp}.tar"
            if (
                (backup is not None and backup["id"] == stamp)
                or any(item["id"] == stamp for item in garden.get("backups", []))
                or archive.exists()
                or archive.is_symlink()
                or archive.with_suffix(".tar.sha256").exists()
                or archive.with_suffix(".tar.sha256").is_symlink()
            ):
                raise InstallError(
                    "Garden archive already exists for this second; try again later"
                )
            backup = {
                "id": stamp,
                "phase": "recorded",
                "snapshot_id": None,
                "archive": str(archive),
                "sha256": None,
                "generations": None,
            }
            garden["backup"] = backup
            active = True
            ctx.save()

        def phase(value: str) -> None:
            backup["phase"] = value
            ctx.save()

        def verify_generation() -> None:
            if _generations(ctx, hooks) != backup["generations"]:
                raise InstallError(
                    "Garden generation changed during backup; retained snapshot was not deleted"
                )

        if backup["phase"] == "recorded":
            _deployment(ctx)
            if backup["generations"] is None:
                backup["generations"] = _generations(ctx, hooks)
                ctx.save()
        if backup["phase"] in {"recorded", "stopped"}:
            recovery_required = True
            hooks.garden_stop()
            phase("stopped")
        if backup["phase"] == "stopped":
            snapshot_name = f"{_label(ctx.name)}-{backup['id'].lower()}"
            snapshot_id = hooks.garden_snapshot(snapshot_name)
            match = tools.LABELLED_SNAPSHOT.fullmatch(snapshot_id)
            if (
                not match
                or match["label"] != _label(ctx.name)
                or match["export_stamp"] != backup["id"].lower()
            ):
                raise InstallError("Garden snapshot ID differs from the backup request")
            backup["snapshot_id"] = snapshot_id
            phase("snapshotted")
        if backup["phase"] in {"snapshotted", "started", "archived", "complete"}:
            recovery_required = True
            hooks.garden_start()
            verify_generation()
            recovery_required = False
            if backup["phase"] == "snapshotted":
                phase("started")
        if backup["phase"] == "started":
            backup["sha256"] = _archive(ctx, hooks, backup, tools)
            phase("archived")
        if backup["phase"] == "archived":
            archive = Path(backup["archive"])
            _verify_archive(archive, backup, tools)
            hooks.garden_snapshot_delete(backup["snapshot_id"])
            backup["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            history = [
                item for item in garden.get("backups", []) if item["id"] != backup["id"]
            ]
            garden["backups"] = [*history, dict(backup)][-10:]
            finishing = True
            backup["phase"] = "complete"
            garden["backups"][-1]["phase"] = "complete"
        finishing = True
        backup.pop("retry_complete", None)
        ctx.state["status"] = "verified"
        ctx.state.pop("last_error", None)
        ctx.save()
        ctx.note(f"Garden backup archive: {backup['archive']}")
        return {
            "name": ctx.name,
            "status": ctx.state["status"],
            "archive": backup["archive"],
            "sha256": backup["sha256"],
        }
    except (Exception, KeyboardInterrupt) as error:
        if not active:
            raise
        interrupted = (
            isinstance(error, KeyboardInterrupt)
            or getattr(error, "code", None) == "interrupted"
        )
        if finishing:
            backup["retry_complete"] = True
        recovery = ""
        if recovery_required:
            try:
                hooks.garden_start()
            except (Exception, KeyboardInterrupt) as recovery_error:
                recovery += f"; Garden restart failed: {recovery_error}"
            try:
                verify_generation()
            except (Exception, KeyboardInterrupt) as recovery_error:
                recovery += f"; Garden recovery verification failed: {recovery_error}"
        ctx.state["status"] = "interrupted" if interrupted else "failed"
        advice = f"run garden-backup --name {ctx.name} again"
        ctx.state["last_error"] = ctx.redact(f"{error}{recovery}; {advice}")
        ctx.save()
        if recovery:
            ctx.note(ctx.redact(recovery.lstrip("; ")))
        ctx.note(advice)
        if isinstance(error, (InstallError, KeyboardInterrupt)):
            raise
        raise InstallError(f"Garden backup failed; {advice}") from error
    finally:
        workflow._close_adapter(adapter)


def run(args: argparse.Namespace, state_root: Path) -> int | dict[str, Any]:
    """Confirm the outage, then continue the durable backup journal."""
    from .cli import _absolute_safe_path, _confirm_garden_change, _required

    name = _required(
        args.name, "--name", "Installation name: ", non_interactive=args.non_interactive
    )
    with open_read_context(state_root, name) as ctx:
        _check(ctx, output=args.output)
    with open_context(state_root, name) as ctx:
        ctx.verbose = args.verbose
        args.transcript_path = ctx.directory / "commands.log"
        _check(ctx, output=args.output)
        if ctx.mode == "garden-server":
            from . import garden_server

            _confirm_garden_change(
                operation="garden-backup",
                announcements=[
                    "Garden will be unavailable while its stopped data is snapshotted."
                ],
                non_interactive=args.non_interactive,
                yes=args.yes,
            )
            return garden_server.backup(ctx)
        backup = ctx.state["garden"].get("backup")
        pending = backup is not None and (
            backup.get("phase") != "complete" or backup.get("retry_complete", False)
        )
        default = ctx.directory / "garden-backups"
        output = _absolute_safe_path(
            args.output or (Path(backup["archive"]).parent if pending else default),
            purpose="Garden backup output",
        )
        if pending and output != Path(backup["archive"]).parent:
            raise InstallError(
                "Garden backup is in progress; rerun with the original --output"
            )
        announcements = [
            "Garden will be unavailable while its stopped data is snapshotted."
        ]
        if output == default:
            announcements.append(
                f"Backups under {default} are deleted by blitz; copy them elsewhere to retain them."
            )
        _confirm_garden_change(
            operation="garden-backup",
            announcements=announcements,
            non_interactive=args.non_interactive,
            yes=args.yes,
        )
        secure_directory(output, private=True)
        return _backup(ctx, output)
