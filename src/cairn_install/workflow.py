"""Shared, visible installation stages and preserving lifecycle recovery."""

from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from cairn_install.bootstrap import bootstrap
from cairn_install.core import Context, InstallError
from cairn_install.verification import ingest, ready, validate_ingest, verify_reads

STAGES = (
    ("preflight", "Check prerequisites and ownership"),
    ("prepare", "Prepare runtime and configuration"),
    ("bootstrap", "Verify catalogue and retain administrator credential"),
    ("start", "Start Cairn"),
    ("verify", "Verify identity and exact saved data"),
    ("restart", "Restart and verify the same saved data"),
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


def backend(ctx: Context, *, foreground: bool = False) -> Adapter:
    if ctx.mode == "docker":
        from cairn_install.docker import Backend

        return Backend(ctx)
    from cairn_install.native import Backend as NativeBackend

    return NativeBackend(ctx, foreground=foreground)


def stage(ctx: Context, key: str, action: Callable[[], object]) -> None:
    title = dict(STAGES).get(key, key)
    ctx.note(f"\n=== {title} ===")
    ctx.state["steps"][key] = "running"
    ctx.state["current_stage"] = key
    ctx.save()
    action()
    ctx.state["steps"][key] = "complete"
    ctx.save()
    ctx.note(f"[OK] {title}")


def status_install(ctx: Context) -> dict[str, Any]:
    return {
        "name": ctx.name,
        "mode": ctx.mode,
        "status": ctx.state["status"],
        "instance_id": ctx.instance_id,
        "features": "Attic plus semantic search" if ctx.semantic else "Attic only",
        "endpoint": f"http://127.0.0.1:{ctx.port}",
        "steps": ctx.state["steps"],
        "state": str(ctx.directory / "state.json"),
        "log": str(ctx.directory / "commands.log"),
        "credential_file": str(ctx.root / "credentials" / "admin.token"),
        "note": "Recorded result; status does not probe the live service.",
    }


def run_install(ctx: Context, *, keep_running: bool = False) -> dict[str, Any]:
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
            adapter.close()
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
    previously_verified = (
        ctx.state["status"] == "verified" or ctx.state.get("verified_recheck") is True
    )
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

        stage(ctx, "preflight", preflight)
        if previously_verified:
            receipt = ctx.state["receipts"].get("ingest")
            if not isinstance(receipt, dict):
                raise InstallError(
                    "Verified installation has no retained ingest receipt"
                )
            validate_ingest(receipt)
            # Starting is idempotent and reconciles stopped persistent services.
            # Reuse the saved proof: never bootstrap or submit another write here.
            stage(ctx, "start", adapter.start)

            def verify_saved() -> None:
                ready(ctx)
                verify_reads(ctx, str(adapter.runtime_python), receipt)

            stage(ctx, "verify", verify_saved)
        else:
            stage(ctx, "prepare", adapter.prepare)

            def establish() -> None:
                adapter.validate_ownership()
                if adapter.is_running():
                    adapter.stop()
                bootstrap(ctx, adapter)

            stage(ctx, "bootstrap", establish)
            stage(ctx, "start", adapter.start)

            def verify() -> None:
                token = ready(ctx)
                receipt = ingest(ctx, token)
                verify_reads(ctx, str(adapter.runtime_python), receipt)

            stage(ctx, "verify", verify)

            def restart() -> None:
                adapter.restart()
                ready(ctx)
                verify_reads(
                    ctx, str(adapter.runtime_python), ctx.state["receipts"]["ingest"]
                )

            stage(ctx, "restart", restart)
            if ctx.mode == "disposable" and not keep_running:
                stage(ctx, "stop", adapter.stop)
                ctx.note(
                    "Disposable test finished and its process is stopped. Its data and credentials are retained for inspection."
                )
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
    if ctx.state["status"] == "blitzing":
        raise InstallError("This instance is being deleted; run blitz again to finish")
    adapter = backend(ctx)
    adapter.validate_ownership()
    stage(ctx, "rollback", adapter.rollback)
    ctx.state.pop("verified_recheck", None)
    ctx.state["status"] = "rolled_back"
    ctx.save()
    ctx.note(
        "Rollback stopped owned services. Data, volumes, credentials and configuration are retained; resume can reuse them."
    )
    return status_install(ctx)


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
            adapter.blitz()
            ctx.state["blitz_phase"] = "resources_removed"
            ctx.save()
        ctx.note(f"Remove instance files, credentials, logs and state: {ctx.directory}")
        remove_instance_files(ctx)
        return result
    finally:
        adapter.close()
