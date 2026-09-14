"""Offline lifecycle checks and recoverable, one-time credential capture."""

from __future__ import annotations

import json
from typing import Any, Protocol

from cairn_install.core import TOKEN, Context, InstallError


class Lifecycle(Protocol):
    def lifecycle_argv(self, operation: str) -> list[str]: ...


def document(raw: str, operation: str, instance_id: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise InstallError(
            f"{operation} returned invalid JSON; inspect the retained log"
        ) from error
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise InstallError(f"{operation} did not report status ok")
    if value.get("instance_id") != instance_id:
        raise InstallError(f"{operation} identity differs from the recorded instance")
    if operation != "check-config" and value.get("operation") != operation:
        raise InstallError(f"Unexpected {operation} response")
    return value


def bootstrap(ctx: Context, backend: Lifecycle) -> None:
    """Caller has stopped its own service before taking the catalogue lease."""
    for operation in ("check-config", "migrate"):
        document(
            ctx.command(backend.lifecycle_argv(operation)), operation, ctx.instance_id
        )
    report = document(
        ctx.command(backend.lifecycle_argv("verify")), "verify", ctx.instance_id
    )
    capture = ctx.root / "credentials" / "bootstrap.json"
    recorded_capture = ctx.state.get("bootstrap_intent")
    if recorded_capture:
        from pathlib import Path

        candidate = Path(recorded_capture)
        if (
            candidate.parent != capture.parent
            or not candidate.name.startswith("bootstrap")
            or candidate.suffix != ".json"
        ):
            raise InstallError("Invalid recorded bootstrap capture path")
        capture = candidate
    credential = ctx.root / "credentials" / "admin.token"
    if credential.exists() or credential.is_symlink():
        ctx.check_file(credential)
        token = ctx.read_secret(credential)
        if not TOKEN.fullmatch(token) or report.get("realm_count") != 1:
            raise InstallError(
                "Recorded credential/catalogue is inconsistent; inspect before resuming"
            )
        capture_key = str(capture.absolute())
        capture_owned = capture_key in ctx.state["owned_files"] or capture_key in (
            ctx.state.get("file_intents", {})
        )
        if not recorded_capture and not capture_owned:
            raise InstallError(
                "Unowned bootstrap capture; refusing to trust credential"
            )
        if not capture.exists() and not capture.is_symlink():
            raise InstallError(
                "Recorded bootstrap capture is missing; files are preserved for recovery",
                "needs_credential_recovery",
            )
        if capture_owned:
            ctx.check_file(capture)
        try:
            captured = document(ctx.read_secret(capture), "bootstrap", ctx.instance_id)
        except InstallError:
            raise InstallError(
                "Recorded bootstrap capture is inconsistent; files are preserved for recovery",
                "needs_credential_recovery",
            ) from None
        if captured.get("realm_id") != "local" or captured.get("token") != token:
            raise InstallError(
                "Recorded bootstrap capture and credential disagree; files are preserved for recovery",
                "needs_credential_recovery",
            )
        return
    payload: dict[str, Any] | None = None
    if capture.exists() or capture.is_symlink():
        if (
            not ctx.state.get("bootstrap_intent")
            and str(capture) not in ctx.state["owned_files"]
        ):
            raise InstallError(
                "Unowned bootstrap capture; refusing to adopt credentials"
            )
        try:
            payload = document(ctx.read_secret(capture), "bootstrap", ctx.instance_id)
        except InstallError:
            if report.get("realm_count") != 0:
                raise InstallError(
                    "Bootstrap may have committed but no complete credential capture survives. "
                    "Data is preserved; follow the documented offline credential recovery procedure.",
                    "needs_credential_recovery",
                ) from None
            # Keep the original failed capture for diagnosis; a distinct attempt is safe
            # only after an offline, matching-instance proof of an empty catalogue.
            attempt = int(ctx.state.get("bootstrap_attempt", 0)) + 1
            ctx.state["bootstrap_attempt"] = attempt
            capture = capture.with_name(f"bootstrap-{attempt}.json")
    if payload is None:
        if type(report.get("realm_count")) is not int or report["realm_count"] != 0:
            raise InstallError(
                "The catalogue already has a realm but no retained administrator credential. "
                "No bootstrap was attempted. Use the documented credential recovery procedure.",
                "needs_credential_recovery",
            )
        ctx.state["bootstrap_intent"] = str(capture)
        ctx.save()
        ctx.command(
            backend.lifecycle_argv("bootstrap")
            + ["--realm", "local", "--label", "local-administrator"],
            private=True,
            stdout_path=capture,
        )
        payload = document(ctx.read_secret(capture), "bootstrap", ctx.instance_id)
    captured_token = payload.get("token")
    if (
        payload.get("realm_id") != "local"
        or not isinstance(captured_token, str)
        or not TOKEN.fullmatch(captured_token)
    ):
        raise InstallError(
            "Bootstrap capture has no valid local administrator credential",
            "needs_credential_recovery",
        )
    ctx.add_secret(captured_token)
    ctx.write_file(credential, captured_token + "\n", secret=True)
    ctx.state["bootstrap_complete"] = True
    ctx.save()
    # Credential value remains exclusively in protected files, never state or logs.
