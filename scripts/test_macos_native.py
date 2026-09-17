#!/usr/bin/env python3
"""Exercise a real per-user macOS native installation and LaunchAgent."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

import test_macos_foreground as shared

from cairn_install.core import InstallError

LAUNCH_AGENT_SCHEMA = "cairn.install.launch-agent/v1"
LAUNCH_AGENT_RESOURCE = "native_launch_agent"
LAUNCH_AGENT_FIELDS = {
    "schema",
    "uid",
    "domain",
    "label",
    "path",
    "plist_sha256",
    "status",
}


def account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def native_command(
    source: Path,
    state_root: Path,
    name: str,
    operation: str,
    *,
    port: int | None = None,
) -> list[str]:
    if operation not in {"install", "resume", "rollback", "blitz"}:
        shared.fail(f"unsupported native acceptance operation: {operation}")
    command = [
        sys.executable,
        str(source / "cairn-install"),
        operation,
        "--non-interactive",
        "--name",
        name,
        "--state-root",
        str(state_root),
    ]
    if operation == "install":
        if port is None:
            shared.fail("native install command requires a port")
        command.extend(
            [
                "--source",
                str(source),
                "--mode",
                "native",
                "--port",
                str(port),
            ]
        )
    elif operation == "resume":
        command.extend(["--source", str(source)])
    elif operation == "blitz":
        command.append("--yes")
    return command


def _open_log(path: Path) -> BinaryIO:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    return os.fdopen(descriptor, "wb", buffering=0)


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_command(command: list[str], log_path: Path, *, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        shared.fail("native acceptance deadline expired before installer command")
    with _open_log(log_path) as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=shared.installer_environment(),
            start_new_session=True,
            close_fds=True,
        )
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _stop_group(process)
            raise shared.AcceptanceFailure(
                f"command exceeded the native acceptance deadline: {command[2]}"
            ) from error
    if returncode != 0:
        shared.fail(
            f"native installer {command[2]} exited {returncode}:\n"
            + shared._redacted_tail(log_path)
        )


def _token(state_path: Path) -> str:
    value = shared.private_text(
        state_path.parent / "instance/credentials/admin.token", maximum=1024
    ).rstrip("\n")
    if shared.TOKEN.fullmatch(value) is None:
        shared.fail("native installer credential has an invalid format")
    return value


def wait_serving(
    state_path: Path, port: int, *, deadline: float
) -> tuple[dict[str, Any], str]:
    last = "state not created"
    while time.monotonic() < deadline:
        if state_path.exists():
            state = shared.read_state(state_path)
            last = f"state={state.get('status')!r}"
            if state.get("status") == "verified":
                token = _token(state_path)
                try:
                    ready = shared._http(port, "/health/ready")
                    identity = shared._http(port, "/v1/instance", token=token)
                except shared.AcceptanceFailure:
                    time.sleep(0.25)
                    continue
                if ready.status != 200 or ready.body.get("status") not in {
                    "ok",
                    "ready",
                }:
                    time.sleep(0.25)
                    continue
                if (
                    identity.status != 200
                    or identity.body.get("contract_identity") != "cairn/v1"
                    or identity.body.get("instance_id") != state.get("instance_id")
                ):
                    shared.fail("native service identity differs from installer state")
                return state, token
        time.sleep(0.25)
    shared.fail(f"native service did not become verified and reachable: {last}")


def verify_native_state(state: dict[str, Any]) -> None:
    if state.get("mode") != "native" or state.get("semantic") is not False:
        shared.fail("acceptance instance is not graph-disabled native mode")
    steps = state.get("steps")
    if not isinstance(steps, dict) or any(
        steps.get(name) != "complete"
        for name in ("preflight", "prepare", "bootstrap", "start", "verify", "restart")
    ):
        shared.fail("native installer did not complete every verification stage")
    receipts = state.get("receipts")
    if (
        not isinstance(receipts, dict)
        or not isinstance(receipts.get("ingest"), dict)
        or not isinstance(receipts.get("attic"), dict)
        or receipts["attic"].get("status") != "verified"
    ):
        shared.fail("native installer did not retain complete custody receipts")


def launch_agent_receipt(
    state: dict[str, Any], *, expected_status: str
) -> dict[str, object]:
    resources = state.get("resources")
    receipt = (
        resources.get(LAUNCH_AGENT_RESOURCE) if isinstance(resources, dict) else None
    )
    if not isinstance(receipt, dict) or set(receipt) != LAUNCH_AGENT_FIELDS:
        shared.fail("native launch agent receipt is missing or malformed")
    instance_id = state.get("instance_id")
    try:
        canonical_instance = str(UUID(instance_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise shared.AcceptanceFailure(
            "installer state has a malformed identity"
        ) from error
    uid = os.getuid()
    label = f"invalid.example.cairn.{canonical_instance}"
    path = account_home() / "Library" / "LaunchAgents" / f"{label}.plist"
    digest = receipt.get("plist_sha256")
    if (
        receipt.get("schema") != LAUNCH_AGENT_SCHEMA
        or receipt.get("uid") != uid
        or receipt.get("domain") != f"gui/{uid}"
        or receipt.get("label") != label
        or receipt.get("path") != str(path)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or receipt.get("status") != expected_status
    ):
        shared.fail("native launch agent receipt does not match this instance")
    if expected_status in {"materialised", "loaded", "stopped"}:
        try:
            info = path.lstat()
        except OSError as error:
            raise shared.AcceptanceFailure(
                "loaded launch agent plist is missing"
            ) from error
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o644
            or shared.file_sha256(path) != digest
        ):
            shared.fail("loaded launch agent plist failed ownership or digest checks")
    elif expected_status == "removed" and (path.exists() or path.is_symlink()):
        shared.fail("removed launch agent receipt still has a plist")
    return {field: receipt[field] for field in sorted(LAUNCH_AGENT_FIELDS)}


def restart_native_adapter(state_root: Path, name: str) -> None:
    from cairn_install.core import open_context
    from cairn_install.native import Backend

    with open_context(state_root, name) as ctx:
        adapter = Backend(ctx)
        try:
            adapter.validate_ownership()
            adapter.restart()
        finally:
            adapter.close()


def stop_native_adapter(state_root: Path, name: str) -> None:
    from cairn_install.core import open_context
    from cairn_install.native import Backend

    with open_context(state_root, name) as ctx:
        adapter = Backend(ctx)
        try:
            adapter.validate_ownership()
            adapter.stop()
        finally:
            adapter.close()


def start_native_adapter(state_root: Path, name: str) -> None:
    from cairn_install.core import open_context
    from cairn_install.native import Backend

    with open_context(state_root, name) as ctx:
        adapter = Backend(ctx)
        try:
            adapter.validate_ownership()
            adapter.start()
        finally:
            adapter.close()


def validate_native_adapter(state_root: Path, name: str) -> None:
    from cairn_install.core import open_context
    from cairn_install.native import Backend

    with open_context(state_root, name) as ctx:
        adapter = Backend(ctx)
        try:
            adapter.validate_ownership()
        finally:
            adapter.close()


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.acceptance-{uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _launchctl(
    arguments: list[str],
    *,
    deadline: float,
    allowed: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    remaining = min(15.0, deadline - time.monotonic())
    if remaining <= 0:
        shared.fail("native acceptance deadline expired before launchctl command")
    try:
        completed = subprocess.run(
            ["/bin/launchctl", *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=shared.installer_environment(),
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as error:
        raise shared.AcceptanceFailure("bounded launchctl probe timed out") from error
    if len(completed.stdout) > shared.MAX_RESPONSE or len(completed.stderr) > 4096:
        shared.fail("launchctl probe output exceeded its acceptance bound")
    if completed.returncode not in allowed:
        shared.fail(
            f"launchctl {arguments[0]} returned {completed.returncode}: "
            + completed.stderr.decode("utf-8", errors="replace")[-1000:]
        )
    return completed


def _target_loaded(domain: str, label: str, *, deadline: float) -> bool:
    domain_result = _launchctl(["print", domain], deadline=deadline)
    if not domain_result.stdout.strip() or domain_result.stderr:
        shared.fail("current-user launchd domain could not be established")
    completed = _launchctl(
        ["print", f"{domain}/{label}"],
        deadline=deadline,
        allowed=frozenset({0, 113}),
    )
    if completed.returncode == 0:
        if not completed.stdout.strip() or completed.stderr:
            shared.fail("loaded LaunchAgent observation was malformed")
        return True
    expected = (
        f'Bad request.\nCould not find service "{label}" in domain for user gui: '
        f"{os.getuid()}\n"
    ).encode()
    if completed.stdout or completed.stderr != expected:
        shared.fail("LaunchAgent absence response was not the verified macOS profile")
    repeated = _launchctl(["print", domain], deadline=deadline)
    if not repeated.stdout.strip() or repeated.stderr:
        shared.fail("current-user launchd domain disappeared during observation")
    return False


def _wait_target_absent(
    domain: str, label: str, *, deadline: float, operation: str
) -> None:
    limit = min(deadline, time.monotonic() + 15)
    while time.monotonic() < limit:
        if not _target_loaded(domain, label, deadline=limit):
            return
        time.sleep(0.05)
    shared.fail(f"LaunchAgent remained loaded after {operation}")


def foreign_loaded_variants(
    expected_profile: dict[str, object], path: Path
) -> list[tuple[str, dict[str, object], Path]]:
    executable = dict(expected_profile)
    executable["Program"] = "/bin/sleep"
    arguments = dict(expected_profile)
    expected_arguments = arguments.get("ProgramArguments")
    if not isinstance(expected_arguments, list):
        shared.fail("owned launch agent plist has malformed arguments")
    arguments["ProgramArguments"] = [*expected_arguments, "--foreign-acceptance"]
    environment = dict(expected_profile)
    environment["EnvironmentVariables"] = {"CAIRN_NATIVE_ACCEPTANCE_FOREIGN": "1"}
    restart_policy = dict(expected_profile)
    restart_policy["KeepAlive"] = False
    alternate_directory = path.parent / f"cairn-native-acceptance-{uuid4().hex}"
    return [
        ("executable", executable, path),
        ("arguments", arguments, path),
        ("environment", environment, path),
        ("restart-policy", restart_policy, path),
        ("path", dict(expected_profile), alternate_directory / path.name),
    ]


def bootstrap_foreign_variant(
    domain: str, path: Path, variant: str, *, deadline: float
) -> None:
    try:
        _launchctl(["bootstrap", domain, str(path)], deadline=deadline)
    except shared.AcceptanceFailure as error:
        raise shared.AcceptanceFailure(
            f"foreign {variant} bootstrap failed: {error}"
        ) from error


def foreign_loaded_definition_control(
    state_root: Path,
    name: str,
    state_path: Path,
    receipt: dict[str, object],
    *,
    deadline: float,
) -> dict[str, object]:
    stop_native_adapter(state_root, name)
    stopped_state = shared.read_state(state_path)
    stopped = launch_agent_receipt(stopped_state, expected_status="stopped")
    path = Path(str(stopped["path"]))
    expected = path.read_bytes()
    try:
        expected_profile = plistlib.loads(expected)
    except plistlib.InvalidFileException as error:
        raise shared.AcceptanceFailure(
            "owned launch agent plist is malformed"
        ) from error
    if not isinstance(expected_profile, dict):
        shared.fail("owned launch agent plist is not a dictionary")
    domain = str(receipt["domain"])
    label = str(receipt["label"])
    target = f"{domain}/{label}"
    variants = foreign_loaded_variants(expected_profile, path)

    foreign_loaded = False
    active_variant = "unknown"
    try:
        for active_variant, profile, source_path in variants:
            foreign = plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=True)
            if source_path.parent != path.parent:
                source_path.parent.mkdir(mode=0o700)
            _atomic_replace(source_path, foreign)
            bootstrap_foreign_variant(
                domain, source_path, active_variant, deadline=deadline
            )
            foreign_loaded = True
            if source_path == path:
                _atomic_replace(path, expected)
            try:
                validate_native_adapter(state_root, name)
            except InstallError as error:
                expected_refusal = str(error).startswith(
                    "Refusing foreign loaded LaunchAgent"
                ) or (
                    active_variant == "restart-policy"
                    and str(error) == "Unexpected LaunchAgent restart properties"
                )
                if not expected_refusal:
                    raise
            else:
                shared.fail(
                    f"native adapter validation accepted foreign {active_variant}"
                )
            if not _target_loaded(domain, label, deadline=deadline):
                shared.fail(
                    f"validation booted out the refused foreign {active_variant}"
                )
            try:
                start_native_adapter(state_root, name)
            except InstallError as error:
                expected_refusal = str(error).startswith(
                    "Refusing foreign loaded LaunchAgent"
                ) or (
                    active_variant == "restart-policy"
                    and str(error) == "Unexpected LaunchAgent restart properties"
                )
                if not expected_refusal:
                    raise
            else:
                shared.fail(f"native adapter start accepted foreign {active_variant}")
            if not _target_loaded(domain, label, deadline=deadline):
                shared.fail(f"start booted out the refused foreign {active_variant}")
            _launchctl(["bootout", target], deadline=deadline)
            foreign_loaded = False
            _wait_target_absent(
                domain,
                label,
                deadline=deadline,
                operation=f"owned {active_variant} cleanup",
            )
            if source_path != path:
                source_path.unlink()
                source_path.parent.rmdir()
    finally:
        _atomic_replace(path, expected)
        if foreign_loaded:
            _launchctl(
                ["bootout", target],
                deadline=deadline,
                allowed=frozenset({0, 113}),
            )
            _wait_target_absent(
                domain,
                label,
                deadline=deadline,
                operation=f"failed {active_variant} cleanup",
            )
        for _, _, source_path in variants:
            if source_path != path and (
                source_path.exists() or source_path.is_symlink()
            ):
                source_path.unlink()
            if source_path.parent != path.parent and source_path.parent.exists():
                source_path.parent.rmdir()
    start_native_adapter(state_root, name)
    loaded_state = shared.read_state(state_path)
    return launch_agent_receipt(loaded_state, expected_status="loaded")


def lease_wait_control(
    state_root: Path,
    name: str,
    state_path: Path,
    receipt: dict[str, object],
    work_root: Path,
    *,
    deadline: float,
) -> None:
    from cairn_install.core import open_context
    from cairn_install.launchd import DarwinLaunchAgent

    stop_native_adapter(state_root, name)
    stopped_state = shared.read_state(state_path)
    launch_agent_receipt(stopped_state, expected_status="stopped")
    lock_path = state_path.parent / "instance/data/.cairn-instance.lock"
    started = work_root / "launchd-lease-started"
    terminating = work_root / "launchd-lease-terminating"
    release = work_root / "launchd-lease-release"
    fixture_script = work_root / "launchd-lease-fixture.py"
    fixture_source = """from __future__ import annotations
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

lock, started, terminating, release = map(Path, sys.argv[1:])
descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
fcntl.flock(descriptor, fcntl.LOCK_EX)

def terminate(signum: int, frame: object) -> None:
    del signum, frame
    terminating.touch(exist_ok=False)
    while not release.exists():
        time.sleep(0.01)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
started.touch(exist_ok=False)
while True:
    time.sleep(0.1)
"""
    descriptor = os.open(
        fixture_script,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o700,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(fixture_source)
        stream.flush()
        os.fsync(stream.fileno())
    failures: list[Exception] = []
    domain = str(receipt["domain"])
    label = str(receipt["label"])
    target = f"{domain}/{label}"
    thread: threading.Thread | None = None
    with open_context(state_root, name) as ctx:
        agent = DarwinLaunchAgent(ctx)
        original_contents = agent.path.read_bytes()
        original_receipt = dict(ctx.state["resources"][LAUNCH_AGENT_RESOURCE])
        original_intent = ctx.state["resources"].get("native_launch_agent_intent")
        original_digest = ctx.state["owned_files"][str(agent.path)]
        profile = plistlib.loads(original_contents)
        argv = [
            sys.executable,
            str(fixture_script),
            str(lock_path),
            str(started),
            str(terminating),
            str(release),
        ]
        profile["Program"] = argv[0]
        profile["ProgramArguments"] = argv
        fixture_contents = plistlib.dumps(profile, sort_keys=True).decode()
        fixture_digest = hashlib.sha256(fixture_contents.encode()).hexdigest()
        agent.argv = argv
        agent.contents = fixture_contents
        agent.identity = {**agent.identity, "plist_sha256": fixture_digest}

        def stop() -> None:
            try:
                agent.stop()
            except Exception as error:
                failures.append(error)

        loaded = False
        try:
            _atomic_replace(agent.path, fixture_contents.encode())
            ctx.state["owned_files"][str(agent.path)] = fixture_digest
            ctx.state["resources"][LAUNCH_AGENT_RESOURCE] = {
                **agent.identity,
                "status": "stopped",
            }
            ctx.state["resources"].pop("native_launch_agent_intent", None)
            ctx.save()
            agent.start()
            loaded = True
            while not started.exists():
                if not _target_loaded(domain, label, deadline=deadline):
                    shared.fail(
                        "launchd lease fixture exited before acquiring the lease"
                    )
                if time.monotonic() >= deadline:
                    shared.fail("launchd lease fixture did not acquire the data lease")
                time.sleep(0.01)
            thread = threading.Thread(target=stop, name="native-launchd-stop")
            thread.start()
            while not terminating.exists():
                if not thread.is_alive():
                    shared.fail(
                        "native stop returned before the fixture handled SIGTERM"
                    )
                if time.monotonic() >= deadline:
                    shared.fail("launchd did not deliver SIGTERM to its owned fixture")
                time.sleep(0.01)
            probe = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                import fcntl

                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                else:
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    shared.fail(
                        "SIGTERM-delayed launchd fixture released its data lease"
                    )
            finally:
                os.close(probe)
            if not lock_path.parent.is_dir():
                shared.fail(
                    "native stop removed data while its launchd job was terminating"
                )
            time.sleep(0.25)
            if not thread.is_alive():
                shared.fail(
                    "native stop did not wait for its SIGTERM-delayed launchd job"
                )
            release.touch(exist_ok=False)
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                shared.fail("native stop did not finish after fixture termination")
            if failures:
                raise failures[0]
            loaded = False
            launch_agent_receipt(ctx.state, expected_status="stopped")
        finally:
            if not release.exists():
                release.touch()
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
            if loaded:
                _launchctl(
                    ["bootout", target],
                    deadline=max(deadline, time.monotonic() + 15),
                    allowed=frozenset({0, 113}),
                )
                _wait_target_absent(
                    domain,
                    label,
                    deadline=max(deadline, time.monotonic() + 15),
                    operation="failed lease-fixture cleanup",
                )
            agent.close()
            _atomic_replace(agent.path, original_contents)
            ctx.state["owned_files"][str(agent.path)] = original_digest
            ctx.state["resources"][LAUNCH_AGENT_RESOURCE] = original_receipt
            if original_intent is None:
                ctx.state["resources"].pop("native_launch_agent_intent", None)
            else:
                ctx.state["resources"]["native_launch_agent_intent"] = original_intent
            ctx.save()


def verify_memory(
    port: int,
    token: str,
    marker: str,
    old_id: str,
    old_body: str,
    new_id: str,
    new_body: str,
    relationship_id: str,
) -> None:
    recalled = shared.recall(port, token, marker)
    recalled_bodies = shared.bodies(recalled)
    if recalled_bodies.get(new_id) != new_body or old_id in recalled_bodies:
        shared.fail("fresh native recall did not retain the correction")
    history = shared.success(
        port,
        "/memory/v1/history",
        token,
        {"scope": shared.SCOPE, "fact_id": old_id},
    )
    if not any(
        item.get("fact_id") == old_id and item.get("superseded_by") == new_id
        for item in history.get("corrections", [])
        if isinstance(item, dict)
    ):
        shared.fail("fresh native history omitted the correction")
    if not any(
        item.get("relationship_id") == relationship_id
        for item in history.get("disagreements", [])
        if isinstance(item, dict)
    ):
        shared.fail("fresh native history omitted the disagreement")


def run_acceptance(source: Path, work_root: Path, *, seconds: float) -> dict[str, Any]:
    name = "macos-native"
    state_root = work_root / "state"
    state_root.mkdir(mode=0o700)
    state_path = state_root / name / "state.json"
    port = shared.unused_port()
    deadline = time.monotonic() + seconds
    marker = "macos-native-" + uuid4().hex
    old_body = f"{marker}: the retained batch size is 32."
    new_body = f"{marker}: the retained batch size is 64."
    old_evidence = f"{marker} measured 32 items.\nExact first evidence line.\n"
    new_evidence = f"{marker} measured 64 items.\nExact second evidence line.\n"
    plist_path: Path | None = None
    completed_blitz = False
    try:
        run_command(
            native_command(source, state_root, name, "install", port=port),
            work_root / "install.log",
            deadline=deadline,
        )
        initial_state, token = wait_serving(state_path, port, deadline=deadline)
        verify_native_state(initial_state)
        initial_agent = launch_agent_receipt(initial_state, expected_status="loaded")
        plist_path = Path(str(initial_agent["path"]))
        launchd_domain = str(initial_agent["domain"])
        launchd_label = str(initial_agent["label"])
        if not _target_loaded(launchd_domain, launchd_label, deadline=deadline):
            shared.fail("installed native LaunchAgent is not loaded")

        old_id, old_evidence_id, old_recorded_at = shared.remembered(
            port, token, old_body, old_evidence
        )
        new_id, new_evidence_id, new_recorded_at = shared.remembered(
            port, token, new_body, new_evidence
        )
        shared.exact_evidence(
            port, token, old_evidence_id, old_evidence, deadline=deadline
        )
        shared.exact_evidence(
            port, token, new_evidence_id, new_evidence, deadline=deadline
        )
        shared.wait_for_fact_visibility(
            max(old_recorded_at, new_recorded_at), deadline=deadline
        )
        disagreement = shared.mutation(
            port,
            "/memory/v1/disagree",
            token,
            {
                "scope": shared.SCOPE,
                "left_fact_id": old_id,
                "right_fact_id": new_id,
                "classification": "internal",
                "reason": "Acceptance records competing measurements.",
            },
        )
        relationship_value = disagreement["result"].get("relationship_id")
        try:
            relationship_id = str(UUID(relationship_value))
        except (TypeError, ValueError, AttributeError) as error:
            raise shared.AcceptanceFailure(
                "native disagree returned a malformed identity"
            ) from error
        before = shared.recall(port, token, marker)
        before_bodies = shared.bodies(before)
        if (
            before_bodies.get(old_id) != old_body
            or before_bodies.get(new_id) != new_body
        ):
            shared.fail("fresh native recall omitted a disagreement endpoint")
        if not any(
            item.get("relationship_id") == relationship_id
            for item in before.get("disagreements", [])
            if isinstance(item, dict)
        ):
            shared.fail("fresh native recall omitted the disagreement")
        shared.mutation(
            port,
            "/memory/v1/correct",
            token,
            {
                "scope": shared.SCOPE,
                "fact_ids": [old_id],
                "reason": "Acceptance selects the retained 64-item measurement.",
                "superseded_by": new_id,
            },
        )
        verify_memory(
            port,
            token,
            marker,
            old_id,
            old_body,
            new_id,
            new_body,
            relationship_id,
        )

        restart_native_adapter(state_root, name)
        restarted_state, restarted_token = wait_serving(
            state_path, port, deadline=deadline
        )
        if (
            restarted_state.get("instance_id") != initial_state.get("instance_id")
            or restarted_token != token
        ):
            shared.fail("native restart changed instance identity or credential")
        verify_memory(
            port,
            restarted_token,
            marker,
            old_id,
            old_body,
            new_id,
            new_body,
            relationship_id,
        )
        shared.exact_evidence(
            port,
            restarted_token,
            old_evidence_id,
            old_evidence,
            deadline=deadline,
        )
        shared.exact_evidence(
            port,
            restarted_token,
            new_evidence_id,
            new_evidence,
            deadline=deadline,
        )

        foreign_control_agent = foreign_loaded_definition_control(
            state_root,
            name,
            state_path,
            initial_agent,
            deadline=deadline,
        )
        foreign_control_state, foreign_control_token = wait_serving(
            state_path, port, deadline=deadline
        )
        if (
            foreign_control_state.get("instance_id") != initial_state.get("instance_id")
            or foreign_control_token != token
        ):
            shared.fail("foreign-profile control changed identity or credential")
        verify_memory(
            port,
            foreign_control_token,
            marker,
            old_id,
            old_body,
            new_id,
            new_body,
            relationship_id,
        )

        lease_wait_control(
            state_root,
            name,
            state_path,
            foreign_control_agent,
            work_root,
            deadline=deadline,
        )
        start_native_adapter(state_root, name)
        lease_control_state, lease_control_token = wait_serving(
            state_path, port, deadline=deadline
        )
        lease_control_agent = launch_agent_receipt(
            lease_control_state, expected_status="loaded"
        )
        if lease_control_token != token:
            shared.fail("lease-wait control changed the retained credential")
        verify_memory(
            port,
            lease_control_token,
            marker,
            old_id,
            old_body,
            new_id,
            new_body,
            relationship_id,
        )

        run_command(
            native_command(source, state_root, name, "rollback"),
            work_root / "rollback.log",
            deadline=deadline,
        )
        shared.wait_stopped(port, deadline=min(deadline, time.monotonic() + 15))
        rolled_back = shared.read_state(state_path)
        removed_agent = launch_agent_receipt(rolled_back, expected_status="removed")
        if _target_loaded(launchd_domain, launchd_label, deadline=deadline):
            shared.fail("native rollback retained the LaunchAgent job")
        if rolled_back.get("status") != "rolled_back":
            shared.fail("native rollback did not retain rolled-back state")
        if _token(state_path) != token:
            shared.fail("native rollback changed the retained credential")
        if not (state_path.parent / "instance/data").is_dir():
            shared.fail("native rollback removed retained data")

        run_command(
            native_command(source, state_root, name, "resume"),
            work_root / "resume.log",
            deadline=deadline,
        )
        resumed_state, resumed_token = wait_serving(state_path, port, deadline=deadline)
        resumed_agent = launch_agent_receipt(resumed_state, expected_status="loaded")
        if not _target_loaded(launchd_domain, launchd_label, deadline=deadline):
            shared.fail("native resume did not reload the LaunchAgent job")
        if (
            resumed_state.get("instance_id") != initial_state.get("instance_id")
            or resumed_token != token
        ):
            shared.fail("native resume changed instance identity or credential")
        verify_memory(
            port,
            resumed_token,
            marker,
            old_id,
            old_body,
            new_id,
            new_body,
            relationship_id,
        )
        shared.exact_evidence(
            port, resumed_token, old_evidence_id, old_evidence, deadline=deadline
        )
        shared.exact_evidence(
            port, resumed_token, new_evidence_id, new_evidence, deadline=deadline
        )

        source_fingerprint = resumed_state.get("source_fingerprint")
        if (
            not isinstance(source_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is None
            or source_fingerprint != initial_state.get("source_fingerprint")
        ):
            shared.fail("native resume changed or malformed the source fingerprint")

        run_command(
            native_command(source, state_root, name, "blitz"),
            work_root / "blitz.log",
            deadline=deadline,
        )
        completed_blitz = True
        shared.wait_stopped(port, deadline=min(deadline, time.monotonic() + 15))
        if state_path.parent.exists() or state_path.parent.is_symlink():
            shared.fail("native blitz retained the installer instance tree")
        if plist_path.exists() or plist_path.is_symlink():
            shared.fail("native blitz retained the launch agent plist")
        if _target_loaded(launchd_domain, launchd_label, deadline=deadline):
            shared.fail("native blitz retained the LaunchAgent job")

        return {
            "schema": "cairn.macos-native-acceptance/v1",
            "status": "verified",
            "system": platform.system(),
            "macos_version": platform.mac_ver()[0],
            "darwin_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "source_fingerprint": source_fingerprint,
            "uv_lock_sha256": shared.file_sha256(source / "uv.lock"),
            "mode": "native",
            "semantic": False,
            "instance_id": initial_state["instance_id"],
            "launchd": {
                "initial_loaded_receipt": initial_agent,
                "foreign_control_reloaded_receipt": foreign_control_agent,
                "lease_control_reloaded_receipt": lease_control_agent,
                "rollback_removed_receipt": removed_agent,
                "resumed_loaded_receipt": resumed_agent,
                "adapter_restart": "verified",
                "foreign_executable_refused_without_bootout": "verified",
                "foreign_arguments_refused_without_bootout": "verified",
                "foreign_environment_refused_without_bootout": "verified",
                "foreign_restart_policy_refused_without_bootout": "verified",
                "foreign_plist_path_refused_without_bootout": "verified",
                "stop_waited_for_sigterm_delayed_data_lease": "verified",
            },
            "lifecycle": {
                "installer_exited_service_reachable": "verified",
                "native_restart": "verified",
                "rollback_retained_data": "verified",
                "resume_reused_identity": "verified",
                "blitz_removed_instance": "verified",
            },
            "memory": {
                "remember": "verified",
                "disagree": "verified",
                "correct": "verified",
                "fresh_recall": "verified",
                "fresh_history": "verified",
            },
            "attic": {
                "exact_bytes_before_restart": "verified",
                "exact_bytes_after_restart": "verified",
                "exact_bytes_after_resume": "verified",
            },
        }
    finally:
        if not completed_blitz and state_path.exists():
            cleanup_log = work_root / "cleanup-blitz.log"
            if not cleanup_log.exists():
                try:
                    run_command(
                        native_command(source, state_root, name, "blitz"),
                        cleanup_log,
                        deadline=max(deadline, time.monotonic() + 30),
                    )
                except (
                    InstallError,
                    OSError,
                    subprocess.SubprocessError,
                    shared.AcceptanceFailure,
                ):
                    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--deadline", type=float, default=420)
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().absolute()
    if platform.system() != "Darwin":
        print(
            "acceptance failed: macOS native acceptance requires Darwin",
            file=sys.stderr,
        )
        return 2
    if not 60 <= args.deadline <= 900:
        print("acceptance failed: --deadline must be 60–900 seconds", file=sys.stderr)
        return 2
    if not (source / "cairn-install").is_file() or not (source / "uv.lock").is_file():
        print("acceptance failed: --source is not a Cairn checkout", file=sys.stderr)
        return 2
    temporary = args.work_root is None
    work_root = (
        Path(tempfile.mkdtemp(prefix="cairn-macos-native-"))
        if temporary
        else args.work_root.expanduser().absolute()
    )
    if not temporary:
        try:
            work_root.mkdir(mode=0o700)
        except FileExistsError:
            print("acceptance failed: --work-root must not exist", file=sys.stderr)
            return 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    succeeded = False
    try:
        report = run_acceptance(source, work_root, seconds=args.deadline)
        succeeded = True
    except (
        InstallError,
        shared.AcceptanceFailure,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        report = {
            "schema": "cairn.macos-native-acceptance/v1",
            "status": "failed",
            "system": platform.system(),
            "macos_version": platform.mac_ver()[0],
            "darwin_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "failure": shared.TOKEN.sub("[redacted credential]", str(error)),
            "work_root": str(work_root),
        }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(report, sort_keys=True), file=sys.stdout if succeeded else sys.stderr
    )
    if succeeded:
        if not args.keep_work:
            shutil.rmtree(work_root)
        return 0
    print("acceptance failed; see " + str(args.report), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
