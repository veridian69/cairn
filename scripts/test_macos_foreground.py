#!/usr/bin/env python3
"""Exercise the real foreground disposable installer and memory API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Never
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

MAX_RESPONSE = 2 * 1024 * 1024
FACT_VISIBILITY_MARGIN = timedelta(milliseconds=100)
TOKEN = re.compile(r"cairn1\.[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}")
SCOPE: dict[str, object] = {
    "realm": "local",
    "segments": [{"kind": "repository", "identifier": "example"}],
}


class AcceptanceFailure(RuntimeError):
    """The foreground installation did not satisfy its acceptance contract."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class RunningInstaller:
    process: subprocess.Popen[bytes]
    log_path: Path
    log: BinaryIO

    def close_log(self) -> None:
        if not self.log.closed:
            self.log.close()


def fail(message: str) -> Never:
    raise AcceptanceFailure(message)


def private_text(path: Path, *, maximum: int) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            ):
                fail(f"expected an owner-private regular file: {path}")
            raw = stream.read(maximum + 1)
    except OSError as error:
        raise AcceptanceFailure(f"cannot safely read {path}: {error}") from error
    if len(raw) > maximum:
        fail(f"private file exceeds the acceptance limit: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeError as error:
        raise AcceptanceFailure(f"private file is not UTF-8: {path}") from error


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(private_text(path, maximum=2 * 1024 * 1024))
    except json.JSONDecodeError as error:
        raise AcceptanceFailure(f"installer state is malformed: {path}") from error
    if not isinstance(value, dict):
        fail("installer state is not an object")
    return value


def _redacted_tail(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        return "installer log unavailable"
    value = TOKEN.sub("[redacted credential]", value)
    value = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[redacted provider key]", value)
    return value


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def installer_environment() -> dict[str, str]:
    blocked = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith("CAIRN_")
    }
    environment.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
    return environment


def spawn_installer(command: list[str], log_path: Path) -> RunningInstaller:
    descriptor = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    log = os.fdopen(descriptor, "wb", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=installer_environment(),
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        log.close()
        raise
    return RunningInstaller(process, log_path, log)


def stop_installer(installer: RunningInstaller, *, expected: bool) -> None:
    process = installer.process
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        returncode = process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        installer.close_log()
        fail("installer did not finish its foreground cleanup after Ctrl-C")
    installer.close_log()
    if expected and returncode != 0:
        fail(
            f"foreground installer exited {returncode} after Ctrl-C:\n"
            + _redacted_tail(installer.log_path)
        )


def _http(
    port: int,
    path: str,
    *,
    token: str = "",
    body: dict[str, object] | None = None,
    key: str | None = None,
) -> Response:
    if not path.startswith("/") or "?" in path or "#" in path:
        fail("acceptance HTTP path is invalid")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method="GET" if data is None else "POST",
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=10
        ) as response:
            status = response.status
            response_headers = {
                name.lower(): value for name, value in response.headers.items()
            }
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        with error:
            status = error.code
            response_headers = {
                name.lower(): value for name, value in error.headers.items()
            }
            raw = error.read(MAX_RESPONSE + 1)
    except (URLError, TimeoutError, OSError) as error:
        raise AcceptanceFailure(f"HTTP request to {path} failed") from error
    if len(raw) > MAX_RESPONSE:
        fail(f"HTTP response from {path} exceeded {MAX_RESPONSE} bytes")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise AcceptanceFailure(
            f"HTTP {status} from {path} was not a JSON document"
        ) from error
    if not isinstance(document, dict):
        fail(f"HTTP {status} from {path} was not a JSON object")
    return Response(status, response_headers, document)


def success(
    port: int,
    path: str,
    token: str,
    body: dict[str, object],
    *,
    key: str | None = None,
) -> dict[str, Any]:
    response = _http(port, path, token=token, body=body, key=key)
    if response.status != 200:
        code = response.body.get("failure", {}).get("code", "unknown")
        fail(f"{path} returned HTTP {response.status} ({code})")
    return response.body


def mutation(
    port: int, path: str, token: str, body: dict[str, object]
) -> dict[str, Any]:
    document = success(port, path, token, body, key=str(uuid4()))
    if (
        document.get("outcome") not in {"committed", "replayed"}
        or not isinstance(document.get("mutation_receipt"), dict)
        or not isinstance(document.get("audit_receipt"), dict)
        or not isinstance(document.get("result"), dict)
    ):
        fail(f"{path} did not return a complete custody receipt")
    return document


def wait_for_fact_visibility(
    recorded_at: datetime,
    *,
    deadline: float,
    wall_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    target = recorded_at + FACT_VISIBILITY_MARGIN
    while (remaining := (target - wall_now()).total_seconds()) > 0:
        available = deadline - monotonic_now()
        if available <= 0:
            fail("acceptance deadline expired while waiting for remembered facts")
        sleep(min(remaining, available))


def _recorded_at(value: object) -> datetime:
    if not isinstance(value, str):
        fail("history returned a fact without a recorded_at timestamp")
    try:
        recorded_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AcceptanceFailure("history returned a malformed recorded_at") from error
    if recorded_at.tzinfo is None:
        fail("history returned a naive recorded_at timestamp")
    return recorded_at.astimezone(UTC)


def remembered(
    port: int, token: str, body: str, evidence_payload: str
) -> tuple[str, str, datetime]:
    document = mutation(
        port,
        "/memory/v1/remember",
        token,
        {
            "scope": SCOPE,
            "classification": "internal",
            "facts": [{"body": body}],
            "evidence_payload": evidence_payload,
        },
    )
    result = document["result"]
    assert isinstance(result, dict)
    fact_ids = result.get("fact_ids")
    evidence_id = result.get("evidence_id")
    if not isinstance(fact_ids, list) or len(fact_ids) != 1:
        fail("remember did not return exactly one fact identity")
    try:
        fact_id = str(UUID(fact_ids[0]))
        evidence = str(UUID(evidence_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise AcceptanceFailure("remember returned malformed identities") from error
    history = success(
        port,
        "/memory/v1/history",
        token,
        {"scope": SCOPE, "fact_id": fact_id},
    )
    matching = [
        item
        for item in history.get("facts", [])
        if isinstance(item, dict)
        and item.get("fact_id") == fact_id
        and item.get("body") == body
    ]
    if len(matching) != 1:
        fail("history did not prove the remembered fact ID/body mapping")
    recorded_at = _recorded_at(matching[0].get("recorded_at"))
    return fact_id, evidence, recorded_at


def exact_evidence(
    port: int, token: str, evidence_id: str, expected: str, *, deadline: float
) -> None:
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    last = "no response"
    while time.monotonic() < deadline:
        response = _http(
            port,
            "/v1/read-evidence",
            token=token,
            body={"scope": SCOPE, "evidence_id": evidence_id},
        )
        if response.status == 200:
            document = response.body
            if (
                document.get("evidence_id") != evidence_id
                or document.get("payload") != expected
                or document.get("sha256") != digest
                or document.get("byte_length") != len(expected.encode("utf-8"))
                or document.get("media_type") != "text/plain; charset=utf-8"
            ):
                fail(
                    "Attic evidence differed by ID, bytes, digest, length or media type"
                )
            return
        failure = response.body.get("failure")
        code = failure.get("code") if isinstance(failure, dict) else None
        last = f"HTTP {response.status} ({code or 'unknown'})"
        if response.status != 503 or code not in {
            "evidence_pending",
            "dependency_unavailable",
        }:
            fail(f"exact evidence read refused: {last}")
        time.sleep(0.5)
    fail(f"exact evidence did not become readable: {last}")


def recall(port: int, token: str, marker: str) -> dict[str, Any]:
    document = success(
        port,
        "/memory/v1/recall",
        token,
        {"scope": SCOPE, "query": marker, "budget": 65536},
    )
    if document.get("semantic_degraded") is not False:
        fail(
            "graph-disabled catalogue recall unexpectedly reported semantic degradation"
        )
    return document


def bodies(document: dict[str, Any]) -> dict[str, str]:
    return {
        item["fact_id"]: item["body"]
        for item in document.get("hits", [])
        if isinstance(item, dict)
        and isinstance(item.get("fact_id"), str)
        and isinstance(item.get("body"), str)
    }


def wait_verified(
    installer: RunningInstaller,
    state_path: Path,
    port: int,
    *,
    deadline: float,
) -> tuple[dict[str, Any], str]:
    last = "state not created"
    while time.monotonic() < deadline:
        if installer.process.poll() is not None:
            installer.close_log()
            fail(
                f"installer exited {installer.process.returncode} before holding the server:\n"
                + _redacted_tail(installer.log_path)
            )
        if state_path.exists():
            state = read_state(state_path)
            last = f"state={state.get('status')!r}"
            if state.get("status") == "verified":
                credential = state_path.parent / "instance/credentials/admin.token"
                token = private_text(credential, maximum=1024).rstrip("\n")
                if TOKEN.fullmatch(token) is None:
                    fail("installer credential has an invalid format")
                try:
                    ready = _http(port, "/health/ready")
                    identity = _http(port, "/v1/instance", token=token)
                except AcceptanceFailure:
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
                    fail("held server identity differs from installer state")
                if installer.process.poll() is not None:
                    fail("installer exited instead of holding the verified server")
                return state, token
        time.sleep(0.25)
    fail(f"installer did not reach verified foreground hold: {last}")


def wait_stopped(port: int, *, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            _http(port, "/health/ready")
        except AcceptanceFailure:
            return
        time.sleep(0.1)
    fail("Cairn remained reachable after foreground installer shutdown")


def installer_command(
    source: Path,
    state_root: Path,
    name: str,
    *,
    port: int | None = None,
    resume: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(source / "cairn-install"),
        "resume" if resume else "install",
        "--non-interactive",
        "--name",
        name,
        "--state-root",
        str(state_root),
        "--source",
        str(source),
        "--keep-running",
    ]
    if not resume:
        assert port is not None
        command.extend(["--mode", "disposable", "--port", str(port)])
    return command


def verify_installer_state(state: dict[str, Any]) -> None:
    if state.get("mode") != "disposable" or state.get("semantic") is not False:
        fail("acceptance instance is not graph-disabled disposable mode")
    steps = state.get("steps")
    if not isinstance(steps, dict) or any(
        steps.get(name) != "complete"
        for name in ("preflight", "prepare", "bootstrap", "start", "verify", "restart")
    ):
        fail("installer did not retain every required verification stage")
    receipts = state.get("receipts")
    if (
        not isinstance(receipts, dict)
        or not isinstance(receipts.get("ingest"), dict)
        or not isinstance(receipts.get("attic"), dict)
        or receipts["attic"].get("status") != "verified"
    ):
        fail("installer did not retain its exact Attic verification receipt")


def run_acceptance(source: Path, work_root: Path, *, seconds: float) -> dict[str, Any]:
    name = "macos-foreground"
    state_root = work_root / "state"
    state_root.mkdir(mode=0o700)
    state_path = state_root / name / "state.json"
    port = unused_port()
    deadline = time.monotonic() + seconds
    marker = "macos-foreground-" + uuid4().hex
    old_body = f"{marker}: the retained batch size is 32."
    new_body = f"{marker}: the retained batch size is 64."
    old_evidence = f"{marker} measured 32 items.\nExact first evidence line.\n"
    new_evidence = f"{marker} measured 64 items.\nExact second evidence line.\n"
    installers: list[RunningInstaller] = []
    try:
        first = spawn_installer(
            installer_command(source, state_root, name, port=port),
            work_root / "install.log",
        )
        installers.append(first)
        first_state, token = wait_verified(first, state_path, port, deadline=deadline)
        verify_installer_state(first_state)

        old_id, old_evidence_id, old_recorded_at = remembered(
            port, token, old_body, old_evidence
        )
        new_id, new_evidence_id, new_recorded_at = remembered(
            port, token, new_body, new_evidence
        )
        exact_evidence(port, token, old_evidence_id, old_evidence, deadline=deadline)
        exact_evidence(port, token, new_evidence_id, new_evidence, deadline=deadline)
        wait_for_fact_visibility(
            max(old_recorded_at, new_recorded_at), deadline=deadline
        )

        disagreement = mutation(
            port,
            "/memory/v1/disagree",
            token,
            {
                "scope": SCOPE,
                "left_fact_id": old_id,
                "right_fact_id": new_id,
                "classification": "internal",
                "reason": "Acceptance records competing measurements.",
            },
        )
        relationship_id = disagreement["result"].get("relationship_id")
        try:
            UUID(relationship_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise AcceptanceFailure("disagree returned a malformed identity") from error
        before = recall(port, token, marker)
        before_bodies = bodies(before)
        if (
            before_bodies.get(old_id) != old_body
            or before_bodies.get(new_id) != new_body
        ):
            fail("fresh recall did not return both disagreeing facts")
        if not any(
            item.get("relationship_id") == relationship_id
            for item in before.get("disagreements", [])
            if isinstance(item, dict)
        ):
            fail("fresh recall omitted the committed disagreement")

        mutation(
            port,
            "/memory/v1/correct",
            token,
            {
                "scope": SCOPE,
                "fact_ids": [old_id],
                "reason": "Acceptance selects the retained 64-item measurement.",
                "superseded_by": new_id,
            },
        )
        history = success(
            port,
            "/memory/v1/history",
            token,
            {"scope": SCOPE, "fact_id": old_id},
        )
        if not any(
            item.get("fact_id") == old_id and item.get("superseded_by") == new_id
            for item in history.get("corrections", [])
            if isinstance(item, dict)
        ):
            fail("history did not prove the correction replacement link")
        after = recall(port, token, marker)
        after_bodies = bodies(after)
        if after_bodies.get(new_id) != new_body or old_id in after_bodies:
            fail("fresh recall did not apply the committed correction")

        stop_installer(first, expected=True)
        wait_stopped(port, deadline=min(deadline, time.monotonic() + 15))

        second = spawn_installer(
            installer_command(source, state_root, name, resume=True),
            work_root / "resume.log",
        )
        installers.append(second)
        resumed_state, resumed_token = wait_verified(
            second, state_path, port, deadline=deadline
        )
        verify_installer_state(resumed_state)
        if resumed_state.get("instance_id") != first_state.get("instance_id"):
            fail("resume changed the retained Cairn instance identity")
        if resumed_token != token:
            fail("resume changed the retained administrator credential")
        source_fingerprint = resumed_state.get("source_fingerprint")
        if (
            not isinstance(source_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is None
            or source_fingerprint != first_state.get("source_fingerprint")
        ):
            fail("resume changed or malformed the tested source fingerprint")

        persisted = recall(port, resumed_token, marker)
        persisted_bodies = bodies(persisted)
        if persisted_bodies.get(new_id) != new_body or old_id in persisted_bodies:
            fail("fresh post-restart recall did not retain the correction")
        persisted_history = success(
            port,
            "/memory/v1/history",
            resumed_token,
            {"scope": SCOPE, "fact_id": old_id},
        )
        if not any(
            item.get("fact_id") == old_id and item.get("superseded_by") == new_id
            for item in persisted_history.get("corrections", [])
            if isinstance(item, dict)
        ):
            fail("fresh post-restart history omitted the correction")
        exact_evidence(
            port, resumed_token, old_evidence_id, old_evidence, deadline=deadline
        )
        exact_evidence(
            port, resumed_token, new_evidence_id, new_evidence, deadline=deadline
        )
        stop_installer(second, expected=True)
        wait_stopped(port, deadline=min(deadline, time.monotonic() + 15))

        return {
            "schema": "cairn.macos-foreground-acceptance/v1",
            "status": "verified",
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "source_fingerprint": source_fingerprint,
            "uv_lock_sha256": file_sha256(source / "uv.lock"),
            "mode": "disposable",
            "semantic": False,
            "instance_id": resumed_state["instance_id"],
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
            },
            "foreground_cycles": 2,
        }
    finally:
        for installer in reversed(installers):
            if installer.process.poll() is None:
                stop_installer(installer, expected=False)
            else:
                installer.close_log()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--deadline", type=float, default=900)
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().absolute()
    if not 60 <= args.deadline <= 1200:
        print("acceptance failed: --deadline must be 60–1200 seconds", file=sys.stderr)
        return 2
    if not (source / "cairn-install").is_file() or not (source / "uv.lock").is_file():
        print("acceptance failed: --source is not a Cairn checkout", file=sys.stderr)
        return 2
    temporary = args.work_root is None
    work_root = (
        Path(tempfile.mkdtemp(prefix="cairn-macos-foreground-"))
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
    report: dict[str, Any]
    succeeded = False
    try:
        report = run_acceptance(source, work_root, seconds=args.deadline)
        succeeded = True
    except (AcceptanceFailure, OSError, subprocess.SubprocessError) as error:
        report = {
            "schema": "cairn.macos-foreground-acceptance/v1",
            "status": "failed",
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "failure": TOKEN.sub("[redacted credential]", str(error)),
            "work_root": str(work_root),
        }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if succeeded:
        print(json.dumps(report, sort_keys=True))
        if not args.keep_work:
            shutil.rmtree(work_root)
        return 0
    print("acceptance failed; see " + str(args.report), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
