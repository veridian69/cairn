#!/usr/bin/env python3
"""Verify an already committed synthetic fact or exact evidence; never ingest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

BODY = "The example repository uses a locked dependency set."
RETRY_CODES = {"index_pending", "stale_index", "dependency_unavailable"}
TOKEN_PATTERN = re.compile(
    r"cairn1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}\.[A-Za-z0-9_-]{43}"
)
MAX_RESPONSE_BYTES = 1048576
MAX_CREDENTIAL_BYTES = 1024
REQUEST = {
    "scope": {
        "realm": "local",
        "segments": [{"kind": "repository", "identifier": "example"}],
    },
    "query": "locked dependency set",
    "budget": 65536,
    "trust_filters": ["candidate"],
}


class VerificationError(Exception):
    """A bounded, operator-readable installation check refusal."""


def endpoint(base_url: str) -> str:
    try:
        value = urlsplit(base_url)
        port = value.port
    except ValueError as error:
        raise VerificationError("invalid endpoint URL") from error
    if (
        any(ord(character) <= 32 or ord(character) == 127 for character in base_url)
        or value.scheme != "http"
        or value.hostname not in {"127.0.0.1", "::1"}
        or value.username is not None
        or value.password is not None
        or value.path not in {"", "/"}
        or value.query
        or value.fragment
    ):
        raise VerificationError(
            "use a numeric-loopback HTTP base URL without credentials or a path"
        )
    if port is not None and port == 0:
        raise VerificationError("endpoint port must be from 1 to 65535")
    return base_url.rstrip("/") + "/v1/retrieve"


def read_credential(path: Path) -> str:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            ):
                raise VerificationError(
                    "credential must be an owned regular file with mode 0400 or 0600"
                )
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise VerificationError("credential file does not contain one Cairn token")
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        token = raw.decode("ascii")
        if TOKEN_PATTERN.fullmatch(token) is None:
            raise VerificationError("credential file does not contain one Cairn token")
        return token
    except (OSError, UnicodeError) as error:
        raise VerificationError(
            "cannot read a safe credential file; check path, ownership and permissions"
        ) from error


def request(
    base_url: str,
    token: str,
    seconds: float,
    *,
    operation: str = "retrieve",
    request_body: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Curl enforces a whole-request timeout; its default config/proxy are disabled."""
    url = endpoint(base_url)
    if operation not in {"retrieve", "read-evidence"}:
        raise VerificationError("unsupported verification operation")
    url = url.removesuffix("retrieve") + operation
    response_limit = (
        6 * 1_048_576 + 4096 if operation == "read-evidence" else MAX_RESPONSE_BYTES
    )
    with tempfile.TemporaryDirectory(prefix="cairn-retrieval-check-") as directory:
        root = Path(directory)
        for name in ("request", "headers", "body"):
            fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        (root / "request").write_text(
            json.dumps(REQUEST if request_body is None else request_body)
        )
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--disable",
                    "--silent",
                    "--show-error",
                    "--config",
                    "-",
                    "--noproxy",
                    "*",
                    "--proto",
                    "=http",
                    "--max-redirs",
                    "0",
                    "--max-time",
                    str(seconds),
                    "--connect-timeout",
                    str(min(5, seconds)),
                    "--max-filesize",
                    str(response_limit),
                    "--request",
                    "POST",
                    "--header",
                    "Content-Type: application/json",
                    "--data-binary",
                    "@" + str(root / "request"),
                    "--dump-header",
                    str(root / "headers"),
                    "--output",
                    str(root / "body"),
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                input=f'header = "Authorization: Bearer {token}"\n'.encode(),
                capture_output=True,
                timeout=seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise VerificationError(
                "retrieval request timed out; no ingest was repeated"
            ) from error
        except OSError as error:
            raise VerificationError(
                "curl could not run; check the installation prerequisites"
            ) from error
        if result.returncode:
            detail = result.stderr.decode(errors="replace").replace(token, "[redacted]")
            raise VerificationError(
                "retrieval transport failed; no ingest was repeated: "
                + json.dumps(detail[:4096])
            )
        try:
            status = int(result.stdout)
        except ValueError as error:
            raise VerificationError("curl returned an invalid HTTP status") from error
        headers: dict[str, str] = {}
        for line in (root / "headers").read_text(errors="replace").splitlines():
            if line.startswith("HTTP/"):
                headers = {}
            elif ":" in line:
                key, value = line.split(":", 1)
                lowered = key.lower()
                stripped = value.strip()
                if lowered in headers:
                    headers[lowered] += ", " + stripped
                else:
                    headers[lowered] = stripped
        body = (root / "body").read_bytes()
        if len(body) > response_limit:
            raise VerificationError(
                "retrieval response exceeded the verification size limit"
            )
        return status, headers, body


def retry_delay(value: str | None, *, now: float | None = None) -> float:
    if value is None:
        raise VerificationError("retryable response has no Retry-After header")
    if re.fullmatch(r"[0-9]+", value):
        delay = float(value)
    else:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                raise ValueError("date has no timezone")
            delay = max(0, parsed.timestamp() - (time.time() if now is None else now))
        except (ValueError, TypeError, OverflowError) as error:
            raise VerificationError(
                "invalid Retry-After; inspect the service/proxy response"
            ) from error
    if not math.isfinite(delay):
        raise VerificationError("invalid Retry-After")
    return delay


def response_context(
    status: int,
    headers: dict[str, str],
    token: str,
    failure: dict[str, object] | None = None,
) -> str:
    def redacted(value: object) -> object:
        if isinstance(value, str):
            return value.replace(token, "[redacted]")
        return value

    context: dict[str, object] = {
        "http_status": status,
        "retry_after": redacted(headers.get("retry-after")),
        "correlation_header": redacted(headers.get("x-correlation-id")),
        "www_authenticate": redacted(headers.get("www-authenticate")),
    }
    if failure is not None:
        rendered: dict[str, object] = {
            name: redacted(failure[name])
            for name in ("code", "message", "retry", "correlation_id")
        }
        detail = failure.get("detail")
        if isinstance(detail, dict):
            rendered_detail = {
                name: redacted(detail[name])
                for name in ("policy", "rule", "field_path")
                if isinstance(detail.get(name), str)
            }
            if rendered_detail:
                rendered["detail"] = rendered_detail
        context["failure"] = rendered
    return json.dumps(context, separators=(",", ":"))


def failure_body(body: object) -> dict[str, object]:
    if not isinstance(body, dict) or not isinstance(body.get("failure"), dict):
        raise VerificationError("malformed failure envelope")
    failure = body["failure"]
    assert isinstance(failure, dict)
    if any(
        not isinstance(failure.get(name), str)
        for name in ("code", "message", "retry", "correlation_id")
    ):
        raise VerificationError("malformed failure envelope")
    return failure


def verify(
    base_url: str,
    token: str,
    fact_id: str,
    seconds: float,
    request_seconds: float,
    max_attempts: int,
    *,
    evidence_payload: bytes | None = None,
) -> dict[str, str]:
    endpoint(base_url)
    evidence_mode = evidence_payload is not None
    retry_codes = (
        {"evidence_pending", "dependency_unavailable"} if evidence_mode else RETRY_CODES
    )
    deadline = time.monotonic() + seconds
    last = "no HTTP response"
    attempts = 0
    while attempts < max_attempts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        if evidence_mode:
            status, headers, raw = request(
                base_url,
                token,
                min(request_seconds, remaining),
                operation="read-evidence",
                request_body={"scope": REQUEST["scope"], "evidence_id": fact_id},
            )
        else:
            status, headers, raw = request(
                base_url, token, min(request_seconds, remaining)
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise VerificationError(
                f"HTTP {status} returned malformed JSON; "
                + response_context(status, headers, token)
            ) from error
        if status == 200 and evidence_mode:
            assert evidence_payload is not None
            try:
                actual = body.get("payload") if isinstance(body, dict) else None
                if not isinstance(actual, str):
                    raise ValueError("payload is not text")
                actual_bytes = actual.encode("utf-8")
                digest = hashlib.sha256(evidence_payload).hexdigest()
                if (
                    body.get("evidence_id") != fact_id
                    or actual_bytes != evidence_payload
                    or body.get("sha256") != digest
                    or type(body.get("byte_length")) is not int
                    or body["byte_length"] != len(evidence_payload)
                    or body.get("media_type") != "text/plain; charset=utf-8"
                ):
                    raise ValueError("evidence differs")
            except (ValueError, UnicodeError) as error:
                raise VerificationError(
                    "evidence success did not match the saved bytes, ID, digest, length and media type"
                ) from error
            return {"status": "verified", "evidence_id": fact_id, "sha256": digest}
        if status == 200:
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("hits"), list)
                or type(body.get("budget_consumed")) is not int
                or body["budget_consumed"] < 0
                or type(body.get("budget_exhausted")) is not bool
                or any(
                    not isinstance(hit, dict)
                    or not isinstance(hit.get("fact_id"), str)
                    or not isinstance(hit.get("body"), str)
                    for hit in body["hits"]
                )
            ):
                raise VerificationError(
                    "malformed retrieval success: "
                    + response_context(status, headers, token)
                )
            if any(
                hit.get("fact_id") == fact_id and hit.get("body") == BODY
                for hit in body["hits"]
            ):
                return {"status": "verified", "fact_id": fact_id, "body": BODY}
            raise VerificationError(
                "HTTP 200 did not return the expected fact; check scope, trust, receipt and projection status. This is not classified as indexing delay."
            )
        try:
            failure = failure_body(body)
        except VerificationError as error:
            raise VerificationError(
                f"HTTP {status} returned a malformed failure envelope; "
                + response_context(status, headers, token)
            ) from error
        last = response_context(status, headers, token, failure)
        if (
            status != 503
            or failure.get("code") not in retry_codes
            or failure.get("retry") != "after-delay"
        ):
            raise VerificationError(
                f"HTTP {status} retrieval refused without retry: " + last
            )
        print(json.dumps({"waiting": json.loads(last)}), file=sys.stderr)
        try:
            delay = retry_delay(headers.get("retry-after"))
        except VerificationError as error:
            raise VerificationError(f"{error}; {last}") from error
        if delay >= deadline - time.monotonic():
            break
        if attempts >= max_attempts:
            raise VerificationError(
                "retrieval verification attempt limit exceeded; "
                + last
                + "; no ingest was repeated"
            )
        time.sleep(delay)
    raise VerificationError(
        "retrieval verification deadline exceeded; "
        + last
        + "; check /health/ready, service logs and adapter credentials, then retry this read-only check with the same fact ID"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--credential-file", required=True, type=Path)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--fact-id", type=UUID)
    identity.add_argument("--evidence-id", type=UUID)
    parser.add_argument(
        "--payload-file",
        type=Path,
        help="saved UTF-8 input for --evidence-id; never sent to the server",
    )
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--max-attempts", type=int, default=30)
    args = parser.parse_args()
    if not (0 < args.deadline <= 3600 and 0 < args.request_timeout <= args.deadline):
        parser.error(
            "use a finite deadline up to 3600 seconds and a positive request timeout no larger than it"
        )
    if not 1 <= args.max_attempts <= 1000:
        parser.error("use a max-attempts value from 1 to 1000")
    if (args.evidence_id is not None) != (args.payload_file is not None):
        parser.error("--evidence-id requires --payload-file; --fact-id forbids it")
    try:
        payload = None
        if args.payload_file is not None:
            with args.payload_file.open("rb") as stream:
                payload = stream.read(1_048_577)
            if not 1 <= len(payload) <= 1_048_576:
                raise VerificationError(
                    "expected evidence must be from 1 byte to 1 MiB"
                )
            payload.decode("utf-8")
        result = verify(
            args.base_url,
            read_credential(args.credential_file),
            str(args.evidence_id if args.evidence_id is not None else args.fact_id),
            args.deadline,
            args.request_timeout,
            args.max_attempts,
            evidence_payload=payload,
        )
    except (VerificationError, OSError, UnicodeError) as error:
        print(json.dumps({"verification_failed": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
