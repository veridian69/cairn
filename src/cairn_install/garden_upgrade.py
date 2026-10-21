"""Journalled Garden release changes; runtime work belongs to the adapters."""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from . import garden, workflow
from .core import Context, InstallError, open_context, open_read_context, read_owned

_COLD_COPY_LIMIT = 8 * 1024**3
_PHASES = (
    "recorded",
    "staged",
    "checked",
    "stopped",
    "copied",
    "swapped",
    "started",
    "verified",
    "complete",
)


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise InstallError(f"Garden version must be numeric and dotted: {value}")
    numbers = tuple(int(part) for part in value.split("."))
    # 1.2 and 1.2.0 denote the same numeric version.
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers = numbers[:-1]
    return numbers


def _mode(ctx: Context) -> None:
    if ctx.mode == "kubernetes":
        raise InstallError(
            "Kubernetes garden-upgrade arrives in a later release", "invalid_arguments"
        )
    if ctx.mode == "garden-server":
        raise InstallError(
            "garden-server garden-upgrade arrives in a later release",
            "invalid_arguments",
        )
    if ctx.mode not in {"native", "docker"} or platform.system() != "Linux":
        raise InstallError("Garden upgrade supports native and docker on Linux")


def _preconditions(ctx: Context) -> dict[str, Any]:
    _mode(ctx)
    record = ctx.state.get("garden")
    if not isinstance(record, dict):
        raise InstallError("Garden is not managed by this installation")
    journal = record.get("upgrade")
    # Only an unfinished journal explains a failed status; a complete one
    # predates whatever failed since.
    if ctx.state["status"] != "verified" and not (
        ctx.state["status"] in {"failed", "interrupted"}
        and isinstance(journal, dict)
        and journal.get("phase") != "complete"
    ):
        raise InstallError(
            "Garden upgrade requires a verified installation; run resume first"
        )
    if workflow._garden_add_incomplete(record) or "remove" in record:
        raise InstallError("Garden add or remove is incomplete; finish it first")
    backup = record.get("backup")
    if isinstance(backup, dict) and backup.get("phase") != "complete":
        raise InstallError(
            f"Garden backup is in progress; run garden-backup --name {ctx.name} again"
        )
    if journal is not None and journal.get("phase") not in _PHASES:
        raise InstallError("Recorded Garden upgrade phase is invalid")
    return record


def _generation(ctx: Context) -> dict[str, str]:
    record = ctx.state["garden"]
    verified = record.get("verification", {})
    result = {
        name: verified.get(name, {}).get("generation")
        for name in record["options"]["participants"]
    }
    if not result or any(
        not isinstance(value, str) or not value for value in result.values()
    ):
        raise InstallError(
            "Garden verification did not return every participant generation"
        )
    return result


def _verify(ctx: Context, adapter: workflow.GardenAdapter, expected: Any) -> None:
    workflow._verify_garden(ctx, adapter)
    if expected is not None and _generation(ctx) != expected:
        raise InstallError("Garden generation changed across the release change")


def _current_artefact(ctx: Context) -> dict[str, str]:
    if ctx.mode == "native":
        binary = ctx.root / "garden" / "bin" / "garden"
        expected = ctx.state["owned_files"].get(str(binary))
        observed = hashlib.sha256(read_owned(binary, 128 * 1024 * 1024)).hexdigest()
        if expected != observed or stat.S_IMODE(binary.stat().st_mode) != 0o755:
            raise InstallError("Owned Garden binary changed")
        return {"binary_sha256": observed}
    receipt = ctx.state["resources"].get("docker", {}).get("garden_image", {})
    image, identifier = receipt.get("reference"), receipt.get("id")
    if (
        not isinstance(image, str)
        or not image
        or not isinstance(identifier, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier)
    ):
        raise InstallError("Owned Garden image receipt is missing or invalid")
    return {"image": image, "id": identifier}


def _source(ctx: Context, adapter: workflow.Adapter) -> dict[str, Any]:
    from .cli import _assert_cairn_source

    _assert_cairn_source(ctx, ctx.source)
    before = _current_artefact(ctx)
    if ctx.mode == "docker":
        from .docker import Backend

        # Resume may rebuild a legacy image; back-fill needs the existing identity.
        cast(Backend, adapter).validate_ownership(require_garden_image=True)
    else:
        adapter.validate_ownership()
    if _current_artefact(ctx) != before:
        raise InstallError("Owned Garden artefact changed during validation")
    recorded = ctx.state["garden"].get("source")
    if recorded is None:
        if ctx.state.get("source_fingerprint_scope") == "excluding_garden":
            raise InstallError(
                "Legacy Garden record has no full Cairn fingerprint proof"
            )
        return {
            "path": str(ctx.source),
            "garden_fingerprint": garden.garden_fingerprint(ctx.source),
            "version": garden.source_version(ctx.source),
            "artefact": before,
        }
    if recorded.get("artefact", before) != before:
        raise InstallError("Garden artefact differs from its recorded source")
    return {**recorded, "artefact": before}


def _cold_copy_size(root: Path) -> tuple[int, int]:
    """Return the data size and the size of native garden_copy's archive."""
    # Serialise each header as garden_copy's writer would (PAX extensions
    # included), so the archive limit is checked before any downtime.
    writer = tarfile.open(fileobj=io.BytesIO(), mode="w", dereference=True)
    size, archive = 0, 2 * tarfile.BLOCKSIZE
    paths = [root]
    for directory, directories, files in os.walk(root, followlinks=False):
        paths.extend(Path(directory) / name for name in directories + files)
    for path in paths:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise InstallError("Garden data contains a non-regular member")
        member = writer.gettarinfo(path, arcname=str(path.relative_to(root.parent)))
        archive += len(member.tobuf(writer.format, writer.encoding, writer.errors))
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
            archive += -(-info.st_size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    return size, -(-archive // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


def _check_space(ctx: Context, adapter: workflow.GardenAdapter) -> None:
    if ctx.mode == "docker":
        # Only garden_copy can measure the volume. Its backend must check
        # capacity before creating the archive; the engine never probes Docker.
        return
    size, archive = _cold_copy_size(ctx.root / "garden" / "data")
    if archive > _COLD_COPY_LIMIT:
        raise InstallError(
            "Garden data exceeds the 8 GiB cold-copy limit; upgrade cannot proceed"
        )
    if shutil.disk_usage(ctx.root).free < size * 2:
        raise InstallError(
            "Garden upgrade needs free space of at least twice the data size"
        )


def _cold_copy(path: Path) -> dict[str, Any]:
    """Validate a stopped-data archive without extracting any member."""
    from .core import secure_directory

    secure_directory(path.parent, private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > _COLD_COPY_LIMIT
        ):
            raise InstallError(
                "Garden cold copy must be an owned regular file of at most 8 GiB"
            )
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
        stream.seek(0)
        found = False
        names: set[str] = set()
        try:
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                for member in archive:
                    name = PurePosixPath(member.name)
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or not name.parts
                        or name.parts[0] != "data"
                        or not (member.isfile() or member.isdir())
                        or str(name) in names
                    ):
                        raise InstallError("Garden cold copy contains an unsafe member")
                    names.add(str(name))
                    found |= str(name) == "data/state.db" and member.isfile()
        except tarfile.TarError as error:
            raise InstallError("Garden cold copy is not a valid tar archive") from error
        if not found:
            raise InstallError("Garden cold copy has no data/state.db")
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "bytes": info.st_size,
        "taken_at": workflow._utc_timestamp(),
    }


def _check_copy(journal: dict[str, Any]) -> Path:
    recorded = journal["cold_copy"]
    path = Path(recorded["path"])
    observed = _cold_copy(path)
    if any(observed[key] != recorded[key] for key in ("sha256", "bytes")):
        raise InstallError("Recorded Garden cold copy changed")
    return path


def _check_staged_source(target: dict[str, Any], path: Path) -> None:
    # The receipt must describe what the build read, before and after it.
    if (
        garden.garden_fingerprint(path) != target["garden_fingerprint"]
        or garden.source_version(path) != target["version"]
    ):
        raise InstallError(
            "Garden --source changed while staging; rerun with the recorded checkout unchanged"
        )


def _phase(ctx: Context, journal: dict[str, Any], phase: str) -> None:
    journal["phase"] = phase
    if phase == "started":
        journal["new_started"] = True
    ctx.save()


def _forward(
    ctx: Context, adapter: workflow.GardenAdapter, journal: dict[str, Any]
) -> None:
    if journal["phase"] == "recorded":
        _phase(ctx, journal, "staged")
    if journal["phase"] == "staged":

        def build() -> None:
            source = Path(journal["to"]["path"])
            # Output from an attempt that never recorded its artefact is
            # unvalidated: discard it so this attempt builds what it checks.
            record = ctx.state["garden"]
            adapter.garden_prune(
                keep=[
                    item["artefact"]
                    for item in (record.get("source"), record.get("previous"))
                    if item and item.get("artefact")
                ]
            )
            _check_staged_source(journal["to"], source)
            artefact = adapter.garden_stage(source)
            _check_staged_source(journal["to"], source)
            journal["to"]["artefact"] = artefact
            ctx.save()

        workflow.stage(ctx, "garden_stage", build)
        _phase(ctx, journal, "checked")
    if journal["phase"] == "checked":
        workflow.stage(
            ctx,
            "garden_verify",
            lambda: _verify(ctx, adapter, journal["generation_before"]),
        )
        _phase(ctx, journal, "stopped")
    if journal["phase"] == "stopped":
        adapter.garden_stop()
        _phase(ctx, journal, "copied")
    if journal["phase"] == "copied":
        dest = ctx.root / "garden-upgrades" / journal["id"] / "data.tar"
        from .core import secure_directory

        secure_directory(dest.parent, private=True)

        def take_copy() -> None:
            adapter.garden_copy(dest)
            receipt = _cold_copy(dest)
            if journal["cold_copy"] is not None:
                _check_copy(journal)
            else:
                journal["cold_copy"] = receipt
                ctx.save()

        workflow.stage(ctx, "garden_copy", take_copy)
        _phase(ctx, journal, "swapped")
    _select_and_verify(ctx, adapter, journal)


def _select_and_verify(
    ctx: Context, adapter: workflow.GardenAdapter, journal: dict[str, Any]
) -> None:
    if journal["phase"] == "swapped":
        # The Docker hooks validate the live image against the recorded source,
        # so publish the new source only once the backend has recorded the swap.
        workflow.stage(
            ctx, "garden_swap", lambda: adapter.garden_swap(journal["to"]["artefact"])
        )
        ctx.state["garden"]["source"] = copy.deepcopy(journal["to"])
        ctx.save()
        _phase(ctx, journal, "started")
    if journal["phase"] == "started":
        workflow.stage(ctx, "garden_start", adapter.garden_start)
        _phase(ctx, journal, "verified")
    if journal["phase"] == "verified":
        if not journal.get("restart_started"):
            workflow.stage(
                ctx,
                "garden_verify",
                lambda: _verify(ctx, adapter, journal["generation_before"]),
            )
            journal["restart_started"] = True
            ctx.save()
        workflow.stage(
            ctx, "garden_restart", lambda: workflow._restart_garden(ctx, adapter)
        )
        if _generation(ctx) != journal["generation_before"]:
            raise InstallError("Garden generation changed across restart")


def _rollback(
    ctx: Context, adapter: workflow.GardenAdapter, journal: dict[str, Any]
) -> None:
    if journal["phase"] == "recorded":
        _phase(ctx, journal, "stopped")
    if journal["phase"] == "stopped":
        adapter.garden_stop()
        _phase(ctx, journal, "copied")
    if journal["phase"] == "copied":
        if journal["data_choice"] == "restore":
            path = _check_copy(journal)
            workflow.stage(
                ctx, "garden_restore_data", lambda: adapter.garden_restore_data(path)
            )
        _phase(ctx, journal, "swapped")
    _select_and_verify(ctx, adapter, journal)


def _remove_copy(
    ctx: Context, path: Path, *, receipt: dict[str, Any] | None = None
) -> None:
    """Remove an owned cold copy and its records, including interrupted captures."""
    from .core import _fsync_directory, secure_directory

    root = ctx.root / "garden-upgrades"
    if ".." in path.parts:
        raise InstallError(f"Garden cold-copy path is not normalised: {path}")
    if path.name != "data.tar" or path.parent.parent != root:
        raise InstallError(
            "Recorded Garden cold-copy path is outside the upgrades tree"
        )
    for directory in (root, path.parent):
        if directory.exists() or directory.is_symlink():
            secure_directory(directory, private=True)
    key = str(path)
    owned = ctx.state["owned_files"]
    intents = ctx.state.get("file_intents", {})
    captures = (
        ctx.state.get("resources", {}).get("docker", {}).get("garden_captures", {})
    )
    capture = captures.get(key, {})
    if path.exists() or path.is_symlink():
        expected = [
            digest
            for digest in (owned.get(key), intents.get(key), capture.get("sha256"))
            if digest is not None
        ]
        if not expected and capture != {"intent": "capture"}:
            raise InstallError(f"Garden cold copy is unowned: {path}")
        if receipt is not None:
            observed = _cold_copy(path)
            digest, size = observed["sha256"], observed["bytes"]
            if digest != receipt["sha256"] or size != receipt["bytes"]:
                raise InstallError("Refusing to prune a changed Garden cold copy")
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise InstallError(f"Expected an owned regular file: {path}")
                if ctx.mode == "docker" and stat.S_IMODE(info.st_mode) != 0o600:
                    raise InstallError(f"Garden capture must have mode 0600: {path}")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = info.st_size
        if any(value != digest for value in expected) or (
            capture.get("sha256") is not None and capture.get("bytes") != size
        ):
            raise InstallError(f"Owned Garden cold copy changed: {path}")
        path.unlink()
    # Also on a retry that finds the file gone: the unlink may be unsynced.
    if path.parent.is_dir():
        _fsync_directory(path.parent)
    for table in (owned, intents, captures):
        table.pop(key, None)
    ctx.save()


def _prune_copies(ctx: Context, history: list[dict[str, Any]]) -> None:

    copies = {
        entry["cold_copy"]["path"]: entry["cold_copy"]
        for entry in history
        if entry.get("cold_copy")
    }
    retained = set(list(copies)[-2:])
    for entry in history:
        receipt = entry.get("cold_copy")
        if not receipt or receipt["path"] in retained:
            continue
        path = Path(receipt["path"])
        _remove_copy(ctx, path, receipt=receipt)
        if path.parent.exists():
            path.parent.rmdir()


def _finish(
    ctx: Context, adapter: workflow.GardenAdapter, journal: dict[str, Any]
) -> dict[str, Any]:
    record = ctx.state["garden"]
    record["source"] = copy.deepcopy(journal["to"])
    if journal["direction"] == "upgrade":
        record["previous"] = copy.deepcopy(journal["from"])
    else:
        record.pop("previous", None)
    history = record.setdefault("upgrades", [])
    if not any(entry["id"] == journal["id"] for entry in history):
        history.append(
            {
                "id": journal["id"],
                "direction": journal["direction"],
                "from_version": journal["from"]["version"],
                "to_version": journal["to"]["version"],
                "at": workflow._utc_timestamp(),
                "cold_copy": copy.deepcopy(journal["cold_copy"]),
                "data_choice": journal["data_choice"],
            }
        )
    ctx.save()
    keep = [record["source"]["artefact"]]
    if record.get("previous"):
        keep.append(record["previous"]["artefact"])
    adapter.garden_prune(keep=keep)
    _prune_copies(ctx, history)
    ctx.state["status"] = "verified"
    ctx.state.pop("last_error", None)
    # Publish the terminal journal with all bookkeeping, never before it.
    journal["phase"] = "complete"
    ctx.save()
    return workflow.status_install(ctx)


def _rollback_journal(
    record: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    old = record.get("upgrade")
    if not isinstance(old, dict):
        raise InstallError("Garden has no retained upgrade to roll back")
    choice = "restore" if args.restore_data else "keep" if args.keep_data else None
    if old["direction"] == "rollback":
        if old["phase"] == "complete":
            return old
        if choice is not None and old["data_choice"] != choice:
            raise InstallError(
                "Garden rollback already recorded a different data choice"
            )
        return old
    if old["new_started"] and choice is None:
        raise InstallError(
            "Rollback requires --restore-data or --keep-data: restoring discards messages "
            "since the upgrade; keeping data can break later backups with the old binary.",
            "data_choice_required",
        )
    target = old["from"]
    if target is None:
        raise InstallError("Garden has no previous release to roll back to")
    if not old["new_started"]:
        choice = None
    return {
        "id": str(uuid4()),
        "direction": "rollback",
        "from": copy.deepcopy(old["to"]),
        "to": copy.deepcopy(target),
        "phase": "recorded",
        "generation_before": copy.deepcopy(old["generation_before"]),
        "cold_copy": copy.deepcopy(old["cold_copy"]),
        "new_started": False,
        "data_choice": choice,
        "aside": None,
    }


def run(args: argparse.Namespace, state_root: Path) -> int | dict[str, Any]:
    from .cli import (
        _assert_cairn_source,
        _confirm_garden_change,
        _required,
        source_fingerprint,
        validate_source,
    )

    name = _required(
        args.name, "--name", "Installation name: ", non_interactive=args.non_interactive
    )
    with open_read_context(state_root, name) as readonly:
        _mode(readonly)
    with open_context(state_root, name) as ctx:
        ctx.verbose = args.verbose
        args.transcript_path = ctx.directory / "commands.log"
        record = _preconditions(ctx)
        journal: dict[str, Any] = record.get("upgrade", {})
        pending = bool(journal) and journal["phase"] != "complete"
        if args.rollback:
            if args.source is not None:
                raise InstallError(
                    "--source is not valid with --rollback", "invalid_arguments"
                )
            journal = _rollback_journal(record, args)
        else:
            if pending and journal["direction"] == "rollback":
                raise InstallError(
                    f"Garden rollback is in progress; run garden-upgrade --name {name} --rollback again"
                )
            source = validate_source(
                Path(
                    _required(
                        args.source,
                        "--source",
                        "Garden source checkout: ",
                        non_interactive=args.non_interactive,
                    )
                ).resolve()
            )
            if source == ctx.source.resolve():
                raise InstallError(
                    "Garden --source must use a separate checkout from the recorded Cairn install source"
                )
            target = {
                "path": str(source),
                "garden_fingerprint": garden.garden_fingerprint(source),
                "version": garden.source_version(source),
            }
            _version(target["version"])
            if pending and any(target[key] != journal["to"][key] for key in target):
                raise InstallError(
                    "Garden upgrade already recorded a different --source; rerun with the recorded checkout unchanged"
                )
        _assert_cairn_source(ctx, ctx.source)
        if not pending and not args.rollback:
            garden.require_unexpired(record["options"])
            current = record.get("source")
            current_fingerprint = (
                current["garden_fingerprint"]
                if current
                else garden.garden_fingerprint(ctx.source)
            )
            if current_fingerprint == target["garden_fingerprint"]:
                return workflow.status_install(ctx)
            old_version = (
                garden.source_version(ctx.source)
                if current is None
                else current["version"]
            )
            if _version(target["version"]) < _version(old_version):
                raise InstallError(
                    "Garden downgrade is refused; use --rollback for the retained release"
                )
        announcements = [
            "Garden will stop for a cold copy and release change; Cairn stays running."
        ]
        if (
            not args.rollback
            and not pending
            and _version(target["version"]) == _version(old_version)
        ):
            announcements.append(
                f"Rebuilding Garden {target['version']} from a different fingerprint."
            )
        _confirm_garden_change(
            operation="garden-upgrade",
            announcements=announcements,
            non_interactive=args.non_interactive,
            yes=args.yes,
        )
        owns_journal = pending or args.rollback
        adapter: workflow.Adapter | None = None
        try:
            adapter = workflow.backend(ctx)
            garden_adapter = workflow._garden_adapter(ctx, adapter)
            if args.rollback:
                old = record["upgrade"]
                if old["direction"] == "upgrade" and old["cold_copy"] is None:
                    # Keep the old id durable until its unpublished copy is gone.
                    _remove_copy(
                        ctx, ctx.root / "garden-upgrades" / old["id"] / "data.tar"
                    )
            if not args.rollback and not pending:
                current = _source(ctx, adapter)
                try:
                    _verify(ctx, garden_adapter, None)
                except InstallError as error:
                    raise InstallError(
                        "Garden cannot be verified; run resume first"
                    ) from error
                _check_space(ctx, garden_adapter)
                journal = {
                    "id": str(uuid4()),
                    "direction": "upgrade",
                    "from": current,
                    "to": target,
                    "phase": "recorded",
                    "generation_before": _generation(ctx),
                    "cold_copy": None,
                    "new_started": False,
                    "data_choice": None,
                    "aside": None,
                }
                record["source"] = copy.deepcopy(current)
                if ctx.state.get("source_fingerprint_scope") != "excluding_garden":
                    ctx.state["cairn_fingerprint"] = source_fingerprint(
                        ctx.source, exclude_garden=True
                    )
                    ctx.state["source_fingerprint_scope"] = "excluding_garden"
            record["upgrade"] = journal
            owns_journal = True
            ctx.save()
            action = _rollback if journal["direction"] == "rollback" else _forward
            action(ctx, garden_adapter, journal)
            return _finish(ctx, garden_adapter, journal)
        except (InstallError, KeyboardInterrupt, OSError) as error:
            if owns_journal:
                ctx.state["status"] = (
                    "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
                )
                ctx.state["last_error"] = ctx.redact(str(error))
                ctx.save()
                ctx.note(
                    f"Garden upgrade did not finish; run garden-upgrade --name {name} again"
                )
            if isinstance(error, OSError):
                raise InstallError(
                    f"Garden upgrade filesystem operation failed: {error.strerror}"
                ) from error
            raise
        finally:
            if adapter is not None:
                workflow._close_adapter(adapter)
