"""Shared, visible installation stages and preserving lifecycle recovery."""

from __future__ import annotations

import platform
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from cairn_install.bootstrap import bootstrap
from cairn_install.core import Context, InstallError
from cairn_install.output import features_label
from cairn_install.verification import ingest, ready, validate_ingest, verify_reads

STAGES = (
    ("preflight", "Check prerequisites and ownership"),
    ("garden_files", "Prepare Garden files and participant credentials"),
    ("prepare", "Prepare runtime and configuration"),
    ("bootstrap", "Verify catalogue and retain administrator credential"),
    ("start", "Start Cairn"),
    ("verify", "Verify identity and exact saved data"),
    ("garden_prepare", "Prepare Garden runtime and configuration"),
    ("garden_start", "Start Garden"),
    ("garden_verify", "Verify Garden endpoint and participant bindings"),
    ("restart", "Restart and verify the same saved data"),
    ("stop", "Stop the disposable Cairn process and retain its data"),
    ("rollback", "Stop Cairn and preserve its data"),
    ("garden_rollback", "Stop Garden and preserve its data"),
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
    def garden_prepare(self) -> None: ...
    def garden_start(self) -> None: ...
    def garden_stop(self) -> None: ...
    def garden_open_endpoint(self) -> None: ...
    def garden_close_endpoint(self) -> None: ...


def _garden_adapter(ctx: Context, adapter: Adapter) -> GardenAdapter:
    if ctx.mode == "native":
        from cairn_install.garden_native import GardenBackend

        return GardenBackend(ctx)
    return cast(GardenAdapter, adapter)


def _verify_garden(ctx: Context, adapter: GardenAdapter) -> None:
    from cairn_install import garden

    try:
        adapter.garden_open_endpoint()
        garden.verify(ctx)
    finally:
        adapter.garden_close_endpoint()
    garden.profiles(ctx)


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
        result["garden_endpoint"] = ctx.state["garden"]["options"]["endpoint"]
        result["garden_profiles"] = str(ctx.root / "garden" / "profiles")
        result["garden"] = {
            "enabled": True,
            "verification": ctx.state["steps"].get("garden_verify", "not_checked"),
            "data_policy": "Restart and rollback preserve Garden data; blitz deletes it.",
        }
    return result


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
    try:
        adapter.validate_ownership()
        if "garden" in ctx.state and ctx.mode == "native":
            from cairn_install.garden_native import GardenBackend

            stage(ctx, "garden_rollback", GardenBackend(ctx).rollback)
        stage(ctx, "rollback", adapter.rollback)
        _close_adapter(adapter)
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
