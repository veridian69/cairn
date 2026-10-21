"""Shared, visible installation stages and preserving lifecycle recovery."""

from __future__ import annotations

import platform
import shlex
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from cairn_install.bootstrap import bootstrap
from cairn_install.core import Context, InstallError
from cairn_install.output import features_label
from cairn_install.verification import ingest, ready, validate_ingest, verify_reads

STAGES = (
    ("preflight", "Check prerequisites and ownership"),
    ("garden_files", "Prepare Garden files and participant credentials"),
    ("garden_reconfigure", "Attach Garden to the running Cairn installation"),
    ("prepare", "Prepare runtime and configuration"),
    ("bootstrap", "Verify catalogue and retain administrator credential"),
    ("start", "Start Cairn"),
    ("verify", "Verify identity and exact saved data"),
    ("garden_prepare", "Prepare Garden runtime and configuration"),
    ("garden_start", "Start Garden"),
    ("garden_verify", "Verify Garden endpoint and participant bindings"),
    ("garden_restart", "Restart Garden and verify the same bindings"),
    ("restart", "Restart and verify the same saved data"),
    ("stop", "Stop the disposable Cairn process and retain its data"),
    ("rollback", "Stop Cairn and preserve its data"),
    ("garden_rollback", "Stop Garden and preserve its data"),
    ("garden_revoke", "Revoke Garden participant credentials and grants"),
    ("garden_detach", "Detach Garden runtime and preserve requested data"),
    ("garden_stage", "Build and check the new Garden release"),
    ("garden_copy", "Stop Garden and take a cold copy of its data"),
    ("garden_swap", "Switch Garden to the selected release, keeping the other"),
    ("garden_restore_data", "Restore Garden data from the cold copy"),
    ("garden_snapshot", "Stop Garden and take a validated snapshot"),
    ("garden_archive", "Write and verify the Garden backup archive"),
    ("server_account", "Create Garden service account"),
    ("server_install", "Install Garden server files"),
    ("server_hook", "Install Garden backup hook"),
    ("garden_enable", "Enable Garden services"),
)


class Adapter(Protocol):
    @property
    def runtime_python(self) -> str | Path: ...
    def preflight(self) -> None: ...
    def prepare(self) -> None: ...
    def validate_ownership(self) -> None: ...
    def is_running(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def close(self) -> None: ...
    def wait_foreground(self) -> None: ...
    def rollback(self) -> None: ...
    def blitz(self) -> None: ...
    def lifecycle_argv(self, operation: str) -> list[str]: ...


class EndpointAdapter(Protocol):
    def open_endpoint(self) -> None: ...
    def close_endpoint(self) -> None: ...


class GardenAdapter(Protocol):
    def garden_attach(self) -> None: ...
    def garden_detach(self, *, delete_data: bool) -> None: ...
    def garden_prepare(self) -> None: ...
    def garden_start(self) -> None: ...
    def garden_stop(self) -> None: ...
    def garden_open_endpoint(self) -> None: ...
    def garden_close_endpoint(self) -> None: ...

    def garden_stage(self, source: Path) -> dict[str, str]:
        """Build and check source for the recorded upgrade id; return its artefact.

        Native returns {binary_sha256}, Docker {image, id}. The backend owns
        the staged binary/image; the engine owns upgrade phases and receipts.
        """
        ...

    def garden_copy(self, dest: Path) -> None:
        """Write stopped Garden data to an owned tar at dest; engine records its hash."""
        ...

    def garden_swap(self, artefact: dict[str, str]) -> None:
        """Select artefact, retaining the other release, using the upgrade journal.

        Native finds the staged/previous binary by its recorded digest and
        upgrade id; Docker selects the recorded image. Cairn stays unchanged.
        """
        ...

    def garden_restore_data(self, copy: Path) -> None:
        """Restore the validated cold-copy tar while Garden is stopped.

        Move current data aside using the recorded upgrade id and retain that
        path in the journal so retries reuse it. The engine owns phase changes.
        """
        ...

    def garden_snapshot(self, name: str) -> str:
        """Take a validated snapshot of stopped Garden; return its snapshot id.

        garden_snapshot(name) returns the existing snapshot whose id ends with
        -<name> if one exists, otherwise creates it; idempotent by name.
        """
        ...

    def garden_export(self, snapshot_id: str, dest: Path) -> None:
        """Export snapshot contents into owned dest directory for engine archiving."""
        ...

    def garden_prune(self, keep: Sequence[dict[str, str]]) -> None:
        """Delete owned Garden release artefacts not in keep; idempotent.

        Called after a completed upgrade with the current and previous
        artefacts. Only artefacts recorded as owned are removed; a missing
        one is not an error. The engine prunes cold copies itself.
        """
        ...

    def garden_snapshot_delete(self, snapshot_id: str) -> None:
        """Delete a staged snapshot after its archive verified; idempotent."""
        ...


def _garden_adapter(ctx: Context, adapter: Adapter) -> GardenAdapter:
    if ctx.mode == "native":
        from cairn_install.garden_native import GardenBackend

        # garden_attach and garden_detach land with the native Garden backend.
        return cast(GardenAdapter, GardenBackend(ctx))
    return cast(GardenAdapter, adapter)


def _garden_add_incomplete(garden: dict[str, Any]) -> bool:
    return (
        garden.get("origin") == "added"
        and garden.get("add", {}).get("phase") != "complete"
    )


def _garden_pending(ctx: Context) -> str | None:
    """Return recovery advice for an in-flight upgrade or backup, without writes."""
    garden = ctx.state.get("garden", {})
    for operation in ("upgrade", "backup"):
        recorded = garden.get(operation)
        if isinstance(recorded, dict) and recorded.get("phase") != "complete":
            return (
                f"Garden {operation} is in progress; "
                f"run garden-{operation} --name {ctx.name} again"
            )
    return None


def _verify_garden(ctx: Context, adapter: GardenAdapter) -> None:
    from cairn_install import garden

    try:
        adapter.garden_open_endpoint()
        # Kubernetes verifies through a port-forward bound to loopback; native
        # and Docker listeners may accept only their configured address.
        if ctx.mode == "kubernetes":
            garden.verify(ctx, connect_host="127.0.0.1")
        else:
            garden.verify(ctx)
    finally:
        adapter.garden_close_endpoint()
    garden.profiles(ctx)


def _restart_garden(ctx: Context, adapter: GardenAdapter) -> None:
    """Restart Garden alone; Cairn keeps its generation and the same bindings."""
    adapter.garden_stop()
    adapter.garden_start()
    _verify_garden(ctx, adapter)


def backend(ctx: Context, *, foreground: bool = False) -> Adapter:
    if ctx.mode == "docker":
        from cairn_install.docker import Backend

        return Backend(ctx)
    if ctx.mode == "kubernetes":
        from cairn_install.kubernetes import Backend as KubernetesBackend

        return KubernetesBackend(ctx)
    from cairn_install.native import Backend as NativeBackend

    return NativeBackend(ctx, foreground=foreground)


def _close_adapter(adapter: Adapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        close()


def stage(ctx: Context, key: str, action: Callable[[], object]) -> None:
    # Every persisted stage is part of the operator-facing protocol. Refuse a
    # programmer typo instead of leaking an internal identifier into output.
    title = dict(STAGES)[key]
    ctx.note(f"\n=== {title} ===")
    ctx.state["steps"][key] = "running"
    ctx.state["current_stage"] = key
    ctx.save()
    action()
    ctx.state["steps"][key] = "complete"
    ctx.save()
    ctx.note(f"[OK] {title}")


def status_install(ctx: Context) -> dict[str, Any]:
    if ctx.mode == "garden-server":
        from . import garden_server

        return garden_server.status(ctx)
    endpoint = f"http://127.0.0.1:{ctx.port}"
    if ctx.mode == "kubernetes":
        kubernetes = ctx.state.get("kubernetes")
        if not isinstance(kubernetes, dict):
            raise InstallError("Kubernetes transport record is missing")
        context = kubernetes.get("context")
        namespace = kubernetes.get("namespace")
        if (
            not isinstance(context, str)
            or not context
            or any(char.isspace() for char in context)
            or not isinstance(namespace, str)
            or not namespace
            or any(char.isspace() for char in namespace)
        ):
            raise InstallError("Kubernetes transport record is invalid")
        endpoint = shlex.join(
            [
                "kubectl",
                "--context",
                context,
                "--namespace",
                namespace,
                "port-forward",
                "service/cairn",
                f"{ctx.port}:8000",
            ]
        )
    result = {
        "name": ctx.name,
        "mode": ctx.mode,
        "status": ctx.state["status"],
        "instance_id": ctx.instance_id,
        "features": features_label(ctx.semantic, "garden" in ctx.state),
        "endpoint": endpoint,
        "steps": ctx.state["steps"],
        "state": str(ctx.directory / "state.json"),
        "log": str(ctx.directory / "commands.log"),
        "credential_file": str(ctx.root / "credentials" / "admin.token"),
        "note": "Recorded result; status does not probe the live service.",
    }
    if "garden" in ctx.state:
        garden = ctx.state["garden"]
        options = garden["options"]
        result["garden_endpoint"] = options["endpoint"]
        result["garden_profiles"] = str(ctx.root / "garden" / "profiles")
        result["garden"] = {
            "enabled": True,
            "origin": "added" if garden.get("origin") == "added" else "installed",
            "verification": ctx.state["steps"].get("garden_verify", "not_checked"),
            "data_policy": "Restart and rollback preserve Garden data; blitz deletes it.",
        }
        source = garden.get("source")
        if isinstance(source, dict) and "version" in source:
            result["garden"]["version"] = source["version"]
        previous = garden.get("previous")
        if isinstance(previous, dict):
            result["garden"]["previous_version"] = previous["version"]
        if isinstance(garden.get("upgrade"), dict):
            result["garden"]["upgrade_phase"] = garden["upgrade"]["phase"]
        upgrades = garden.get("upgrades", [])
        if upgrades:
            last = upgrades[-1]
            result["garden"]["last_upgrade"] = {
                "from": last["from_version"],
                "to": last["to_version"],
                "at": last["at"],
                "data_choice": last["data_choice"],
                "cold_copy": last["cold_copy"],
            }
        copies = {
            entry["cold_copy"]["path"]: entry["cold_copy"]
            for entry in upgrades
            if entry.get("cold_copy")
        }
        result["garden"]["cold_copies"] = list(copies.values())[-2:]
        if isinstance(garden.get("backup"), dict):
            result["garden"]["backup_phase"] = garden["backup"]["phase"]
        if garden.get("backups"):
            last_backup = garden["backups"][-1]
            result["garden"]["last_backup"] = {
                key: last_backup[key]
                for key in ("archive", "sha256", "at", "snapshot_id")
            }
        if isinstance(options.get("expires_at"), str):
            result["garden"]["expires_at"] = options["expires_at"]
            result["garden"]["days_left"] = _days_left(options["expires_at"])
        if "add" in garden:
            result["garden"]["add_phase"] = garden["add"]["phase"]
        if "remove" in garden:
            result["garden"]["remove_phase"] = garden["remove"]["phase"]
    pending = ctx.pending_replacements()
    if pending:
        result["pending_replacements"] = pending
    return result


def _days_left(expires_at: str) -> int:
    """Whole days until expiry; negative once the authority has closed."""
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return (expiry - datetime.now(UTC)).days


@contextmanager
def _verification_endpoint(ctx: Context, adapter: Adapter) -> Iterator[None]:
    if ctx.mode != "kubernetes":
        yield
        return
    endpoint = cast(EndpointAdapter, adapter)
    try:
        endpoint.open_endpoint()
        yield
    finally:
        endpoint.close_endpoint()


def run_install(ctx: Context, *, keep_running: bool = False) -> dict[str, Any]:
    if pending := _garden_pending(ctx):
        raise InstallError(pending)
    if keep_running and ctx.mode != "disposable":
        raise InstallError(
            "--keep-running requires disposable mode", "invalid_arguments"
        )
    if ctx.state["status"] == "blitzing":
        raise InstallError("This instance is being deleted; run blitz again to finish")
    adapter = backend(ctx, foreground=True) if keep_running else backend(ctx)
    try:
        return _run_install(ctx, adapter, keep_running=keep_running)
    finally:
        try:
            _close_adapter(adapter)
        except InstallError as error:
            if ctx.state["status"] == "verified":
                ctx.state["verified_recheck"] = True
            ctx.state["status"] = "failed"
            ctx.state["last_error"] = ctx.redact(str(error))
            ctx.save()
            raise


def _run_install(
    ctx: Context, adapter: Adapter, *, keep_running: bool
) -> dict[str, Any]:
    garden_adapter = _garden_adapter(ctx, adapter) if "garden" in ctx.state else None
    previously_verified = (
        ctx.state["status"] == "verified" or ctx.state.get("verified_recheck") is True
    )
    adding = garden_adapter is not None and _garden_add_incomplete(ctx.state["garden"])
    if adding and not previously_verified:
        raise InstallError("Garden add needs a verified installation; it cannot resume")
    if previously_verified and ctx.mode == "disposable" and not keep_running:
        adapter.validate_ownership()
        ctx.note(
            "This installation already passed its checks. Showing the retained result; no new data submitted."
        )
        return status_install(ctx)
    if previously_verified:
        ctx.state["verified_recheck"] = True
    ctx.state["status"] = "installing"
    ctx.save()
    try:

        def preflight() -> None:
            adapter.validate_ownership()
            adapter.preflight()
            if garden_adapter is not None and ctx.mode == "native":
                from cairn_install.garden_native import GardenBackend

                cast(GardenBackend, garden_adapter).preflight()

        stage(ctx, "preflight", preflight)
        if garden_adapter is not None:
            from cairn_install import garden

            stage(ctx, "garden_files", lambda: garden.prepare(ctx))
        if previously_verified:
            receipt = ctx.state["receipts"].get("ingest")
            if not isinstance(receipt, dict):
                raise InstallError(
                    "Verified installation has no retained ingest receipt"
                )
            validate_ingest(receipt)
            if adding and garden_adapter is not None:
                stage(ctx, "garden_reconfigure", garden_adapter.garden_attach)
            # Starting is idempotent and reconciles stopped persistent services.
            # Reuse the saved proof: never bootstrap or submit another write here.
            stage(ctx, "start", adapter.start)

            def verify_saved() -> None:
                with _verification_endpoint(ctx, adapter):
                    token = ready(ctx)
                    verify_reads(ctx, str(adapter.runtime_python), receipt)
                    if garden_adapter is not None:
                        garden.enrol(ctx, token)

            stage(ctx, "verify", verify_saved)
        else:
            stage(ctx, "prepare", adapter.prepare)

            def establish() -> None:
                adapter.validate_ownership()
                if garden_adapter is not None:
                    garden_adapter.garden_stop()
                if adapter.is_running():
                    adapter.stop()
                bootstrap(ctx, adapter)

            stage(ctx, "bootstrap", establish)
            stage(ctx, "start", adapter.start)

            def verify() -> None:
                with _verification_endpoint(ctx, adapter):
                    token = ready(ctx)
                    receipt = ingest(ctx, token)
                    verify_reads(ctx, str(adapter.runtime_python), receipt)
                    if garden_adapter is not None:
                        garden.enrol(ctx, token)

            stage(ctx, "verify", verify)

        if garden_adapter is not None:
            stage(ctx, "garden_prepare", garden_adapter.garden_prepare)
            stage(ctx, "garden_start", garden_adapter.garden_start)
            stage(ctx, "garden_verify", lambda: _verify_garden(ctx, garden_adapter))
            if adding:
                stage(
                    ctx, "garden_restart", lambda: _restart_garden(ctx, garden_adapter)
                )

        if not previously_verified:

            def restart() -> None:
                if garden_adapter is not None:
                    garden_adapter.garden_stop()
                adapter.restart()
                with _verification_endpoint(ctx, adapter):
                    ready(ctx)
                    verify_reads(
                        ctx,
                        str(adapter.runtime_python),
                        ctx.state["receipts"]["ingest"],
                    )
                if garden_adapter is not None:
                    garden_adapter.garden_start()
                    _verify_garden(ctx, garden_adapter)

            stage(ctx, "restart", restart)
            if ctx.mode == "disposable" and not keep_running:
                stage(ctx, "stop", adapter.stop)
                ctx.note(
                    "Disposable test finished and its process is stopped. Its data and credentials are retained for inspection."
                )
        if adding:
            ctx.state["garden"]["add"]["phase"] = "complete"
        ctx.state["status"] = "verified"
        ctx.state.pop("verified_recheck", None)
        ctx.state.pop("last_error", None)
        ctx.save()
        ctx.note(
            '"status": "verified" — authenticated identity, exact Attic bytes'
            + (", semantic retrieval" if ctx.semantic else "")
            + " and restart retention passed."
        )
        if keep_running:
            ctx.note(
                "Cairn is ready at " + f"http://127.0.0.1:{ctx.port}. "
                "Press Ctrl-C to stop; data and credentials will be retained."
            )
            try:
                adapter.wait_foreground()
            except KeyboardInterrupt:
                ctx.note("Stopping the verified foreground instance; data retained.")
        return status_install(ctx)
    except (InstallError, KeyboardInterrupt) as error:
        if ctx.state["status"] == "verified":
            ctx.state["verified_recheck"] = True
        ctx.state["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        ctx.state["last_error"] = ctx.redact(str(error))
        ctx.save()
        ctx.note(
            "Installation stopped; state, data and credentials are retained. Use resume with the same name, or rollback to stop owned services."
        )
        raise


def rollback_install(ctx: Context) -> dict[str, Any]:
    if pending := _garden_pending(ctx):
        raise InstallError(pending)
    if ctx.state["status"] == "blitzing":
        raise InstallError("This instance is being deleted; run blitz again to finish")
    if "remove" in ctx.state.get("garden", {}):
        raise InstallError(_garden_remove_pending(ctx))
    adapter = backend(ctx)
    try:
        adapter.validate_ownership()
        if "garden" in ctx.state and ctx.mode == "native":
            from cairn_install.garden_native import GardenBackend

            stage(ctx, "garden_rollback", GardenBackend(ctx).rollback)
        stage(ctx, "rollback", adapter.rollback)
        _close_adapter(adapter)
        # An incomplete Garden add resumes from its retained verified proof.
        if "garden" in ctx.state and _garden_add_incomplete(ctx.state["garden"]):
            ctx.state["verified_recheck"] = True
        else:
            ctx.state.pop("verified_recheck", None)
        ctx.state.pop("last_error", None)
        ctx.state["status"] = "rolled_back"
        ctx.save()
        ctx.note(
            "Rollback stopped owned services. Data, volumes, credentials and configuration are retained; resume can reuse them."
        )
        return status_install(ctx)
    except (InstallError, KeyboardInterrupt) as error:
        ctx.state["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        ctx.state["last_error"] = ctx.redact(str(error))
        ctx.save()
        raise


def _garden_remove_pending(ctx: Context) -> str:
    return f"Garden removal is in progress; run garden-remove --name {ctx.name} again"


def add_garden(ctx: Context, options: dict[str, Any]) -> dict[str, Any]:
    """Journal the Garden add before any side effect, then run its verified flow."""
    from cairn_install import garden

    if pending := _garden_pending(ctx):
        raise InstallError(pending)
    if ctx.state["status"] != "verified":
        raise InstallError("Garden add needs a verified installation; run resume first")
    if "garden" in ctx.state:
        raise InstallError("Garden is already managed")
    if ctx.mode not in {"native", "docker"}:
        raise InstallError("Garden add supports native and docker installations")
    ctx.state["garden"] = {
        "options": options,
        "origin": "added",
        "add": {"phase": "recorded"},
        "source": {
            "path": str(ctx.source),
            "garden_fingerprint": garden.garden_fingerprint(ctx.source),
            "version": garden.source_version(ctx.source),
        },
        "agents": _retained_principals(ctx, options),
    }
    ctx.save()
    return run_install(ctx)


def _retained_principals(
    ctx: Context, options: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Reuse principals a removed Garden left dormant; Cairn refuses their labels again.

    Removal revokes credentials and grants but keeps principals, so a re-add
    carries over only the create-principal operation. A committed receipt is
    reused as is; a request whose response was lost keeps its key, so Cairn
    replays the original creation. Enrolment then issues a fresh credential
    and grant.
    """
    agents: dict[str, dict[str, Any]] = {}
    for record in reversed(ctx.state.get("garden_history", [])):
        for name, agent in record.get("agents", {}).items():
            if name not in options["participants"] or name in agents:
                continue
            created = agent.get("operations", {}).get("create-principal")
            if isinstance(created, dict) and {"key", "body"} <= set(created):
                agents[name] = {"operations": {"create-principal": dict(created)}}
    return agents


def check_garden_remove(ctx: Context, *, delete_data: bool) -> None:
    """Refuse a garden-remove request without writing anything.

    delete_data may only move from kept to deleted, and only before detachment
    starts; from then on the backend's recorded choice is final.
    """
    if pending := _garden_pending(ctx):
        raise InstallError(pending)
    if ctx.state["status"] == "blitzing":
        raise InstallError("This instance is being deleted; run blitz again to finish")
    recorded = ctx.state.get("garden")
    if not isinstance(recorded, dict):
        raise InstallError("Garden is not managed by this installation")
    if ctx.mode not in {"native", "docker"}:
        raise InstallError("Garden removal supports native and docker installations")
    remove = recorded.get("remove")
    if remove is None:
        if _garden_add_incomplete(recorded):
            raise InstallError(
                f"Garden add is incomplete; run resume --name {ctx.name} first"
            )
        return
    if remove["delete_data"] and not delete_data:
        raise InstallError(
            "Garden removal already recorded --delete-data; rerun with --delete-data"
        )
    if delete_data and not remove["delete_data"] and remove["phase"] == "detached":
        raise InstallError(
            "Garden was already detached keeping its data; it cannot be deleted now"
        )
    if delete_data != remove["delete_data"] and remove["phase"] == "detaching":
        # The backend journals its own choice when detachment starts.
        raise InstallError(
            "Garden detachment already started "
            + ("deleting" if remove["delete_data"] else "keeping")
            + " its data; rerun garden-remove with the same choice"
        )


def remove_garden(ctx: Context, *, delete_data: bool) -> dict[str, Any]:
    """Stop Garden, revoke its grants, detach it, then archive its record.

    The recorded remove phase makes a rerun continue after any interruption.
    Revocation and detachment are idempotent, so their saved keys are reused.
    """
    from cairn_install import garden

    check_garden_remove(ctx, delete_data=delete_data)
    recorded: dict[str, Any] = ctx.state["garden"]
    remove = recorded.get("remove")
    if remove is None:
        remove = {"phase": "stopping", "delete_data": delete_data}
        recorded["remove"] = remove
    elif delete_data:
        remove["delete_data"] = True
    ctx.save()
    adapter = backend(ctx)
    try:
        garden_adapter = _garden_adapter(ctx, adapter)
        garden_adapter.garden_stop()
        stage(ctx, "start", adapter.start)
        token = ready(ctx)
        if remove["phase"] == "stopping":
            stage(ctx, "garden_revoke", lambda: garden.revoke(ctx, token))
            remove["phase"] = "revoked"
            ctx.save()
        if remove["phase"] == "revoked":
            remove["phase"] = "detaching"
            ctx.save()
        if remove["phase"] == "detaching":
            stage(
                ctx,
                "garden_detach",
                lambda: garden_adapter.garden_detach(
                    delete_data=bool(remove["delete_data"])
                ),
            )
            remove["phase"] = "detached"
            ctx.save()

        def verify_retained() -> None:
            receipt = ctx.state["receipts"].get("ingest")
            if not isinstance(receipt, dict):
                raise InstallError(
                    "Verified installation has no retained ingest receipt"
                )
            with _verification_endpoint(ctx, adapter):
                ready(ctx)
                verify_reads(ctx, str(adapter.runtime_python), receipt)

        stage(ctx, "verify", verify_retained)
        _close_adapter(adapter)
        archived = {**recorded, "removed_at": _utc_timestamp()}
        ctx.state.setdefault("garden_history", []).append(archived)
        del ctx.state["garden"]
        ctx.note(
            "Garden data was deleted as requested."
            if remove["delete_data"]
            else "Garden data was kept; --delete-data deletes it when removing."
        )
        ctx.state["status"] = "verified"
        ctx.state.pop("verified_recheck", None)
        ctx.state.pop("last_error", None)
        ctx.save()
        return status_install(ctx)
    except (InstallError, KeyboardInterrupt) as error:
        ctx.state["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        ctx.state["last_error"] = ctx.redact(str(error))
        ctx.save()
        raise


def _utc_timestamp() -> str:
    # Same shape as Garden's expires_at, so archived records read consistently.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def blitz_install(ctx: Context) -> dict[str, Any]:
    from cairn_install.destruction import check_instance_tree, remove_instance_files

    check_instance_tree(ctx)
    ctx.state["status"] = "blitzing"
    ctx.save()
    result = {"name": ctx.name, "instance_id": ctx.instance_id, "status": "deleted"}
    adapter = backend(ctx)
    try:
        if ctx.state.get("blitz_phase") != "resources_removed" or (
            ctx.mode == "native" and platform.system() == "Darwin"
        ):
            ctx.note("\n=== Remove owned services and storage permanently ===")
            # Darwin re-establishes service absence and the data lease even on
            # resumed storage deletion. Keep that lease through rmtree below.
            if "garden" in ctx.state and ctx.mode == "native":
                from cairn_install.garden_native import GardenBackend
                from cairn_install.native import Backend as NativeBackend

                garden_native = GardenBackend(ctx)
                cast(NativeBackend, adapter).validate_blitz_inventory()
                garden_native.validate_blitz_inventory()
                garden_native.blitz()
            adapter.blitz()
            ctx.state["blitz_phase"] = "resources_removed"
            ctx.save()
        ctx.note(f"Remove instance files, credentials, logs and state: {ctx.directory}")
        remove_instance_files(ctx)
        return result
    finally:
        _close_adapter(adapter)
