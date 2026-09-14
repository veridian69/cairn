"""Authenticate the instance and prove retained synthetic data, with no new retry keys."""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from cairn_install.core import MAX_OUTPUT, TOKEN, Context, InstallError, read_owned

PAYLOAD = "Cairn Attic check: café.\nExact second line.\n"
BODY = "The example repository uses a locked dependency set."


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def request(
    ctx: Context,
    endpoint: str,
    token: str = "",
    *,
    data: bytes | None = None,
    key: str = "",
) -> dict[str, Any]:
    url = f"http://127.0.0.1:{ctx.port}{endpoint}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = key
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            Request(url, data=data, headers=headers), timeout=10
        ) as response:
            raw = response.read(MAX_OUTPUT + 1)
            if response.status != 200 or len(raw) > MAX_OUTPUT:
                raise InstallError(f"Unexpected or oversized response from {endpoint}")
    except HTTPError as error:
        raise InstallError(
            f"{endpoint} returned HTTP {error.code}; retained state is unchanged"
        ) from None
    except (URLError, TimeoutError, OSError) as error:
        raise InstallError(
            f"{endpoint} connection failed; inspect service logs and use resume"
        ) from error
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise InstallError(f"{endpoint} returned malformed JSON") from error
    if not isinstance(value, dict):
        raise InstallError(f"{endpoint} returned an unexpected document")
    return value


def validate_instance(value: dict[str, Any], expected: str) -> None:
    if (
        value.get("contract_identity") != "cairn/v1"
        or value.get("instance_id") != expected
    ):
        raise InstallError(
            "Authenticated instance identity/contract differs from the installation record"
        )


def ready(ctx: Context) -> str:
    credential = ctx.root / "credentials" / "admin.token"
    ctx.check_file(credential)
    token = ctx.read_secret(credential)
    if not TOKEN.fullmatch(token):
        raise InstallError("Administrator credential has invalid format")
    curl_config = ctx.root / "credentials" / "curl.conf"
    ctx.write_file(
        curl_config, f'header = "Authorization: Bearer {token}"\n', secret=True
    )
    ctx.note(
        "# Wait up to 120 seconds for readiness, then check authenticated identity.\n"
        f"curl --disable --silent --show-error --fail --noproxy '*' --max-time 10 "
        f"http://127.0.0.1:{ctx.port}/health/ready"
    )
    deadline = time.monotonic() + 120
    last = "No readiness response"
    while time.monotonic() < deadline:
        try:
            response = request(ctx, "/health/ready")
            if response.get("status") not in {"ok", "ready"}:
                raise InstallError("Readiness has not reported ready")
            break
        except InstallError as error:
            last = str(error)
            time.sleep(2)
    else:
        raise InstallError(f"Readiness deadline exceeded: {last}")
    ctx.note(
        f"curl --disable --silent --show-error --fail --noproxy '*' --max-time 10 "
        f"--config {shlex.quote(str(curl_config))} http://127.0.0.1:{ctx.port}/v1/instance"
    )
    identity = request(ctx, "/v1/instance", token)
    validate_instance(identity, ctx.instance_id)
    project = tomllib.loads(read_owned(ctx.source / "pyproject.toml").decode())
    if identity.get("product_version") != project["project"]["version"]:
        raise InstallError(
            "Authenticated product version differs from this source distribution"
        )
    for field, filename in (
        ("contract_digest", "cairn-openapi-v1.json"),
        ("mcp_contract_digest", "cairn-mcp-tools-v1.json"),
    ):
        expected = hashlib.sha256(
            read_owned(ctx.source / "src" / "cairn" / "contracts" / filename)
        ).hexdigest()
        if identity.get(field) != expected:
            raise InstallError(
                f"Authenticated {field} differs from this source distribution"
            )
    ctx.state["receipts"]["instance"] = identity
    ctx.save()
    ctx.note(
        json.dumps(
            {
                "status": "verified",
                "check": "authenticated_instance",
                "instance_id": ctx.instance_id,
            }
        )
    )
    return token


def validate_ingest(value: dict[str, Any]) -> None:
    try:
        if value.get("outcome") not in {"committed", "replayed"}:
            raise ValueError("not committed")
        if not isinstance(value["mutation_receipt"], dict) or not isinstance(
            value["audit_receipt"], dict
        ):
            raise ValueError("missing receipts")
        result = value["result"]
        UUID(result["evidence_id"])
        UUID(result["assertion_id"])
        if not isinstance(result["fact_ids"], list) or len(result["fact_ids"]) != 1:
            raise ValueError("invalid fact list")
        UUID(result["fact_ids"][0])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise InstallError(
            "Ingest response did not prove the expected committed write; same request/key retained"
        ) from error


def ingest(ctx: Context, token: str) -> dict[str, Any]:
    receipts = ctx.state["receipts"]
    if "ingest" in receipts:
        saved: dict[str, Any] = receipts["ingest"]
        validate_ingest(saved)
        return saved
    if "ingest_key" not in ctx.state:
        ctx.state["ingest_key"] = str(uuid4())
        ctx.save()
    key = str(ctx.state["ingest_key"])
    payload_path = ctx.root / "checks" / "payload.txt"
    request_path = ctx.root / "checks" / "ingest.json"
    ctx.write_file(payload_path, PAYLOAD)
    data = (
        json.dumps(
            {
                "scope": {
                    "realm": "local",
                    "segments": [{"kind": "repository", "identifier": "example"}],
                },
                "classification": "internal",
                "source_type": "human",
                "facts": [{"body": BODY}],
                "evidence_payload": PAYLOAD,
            },
            sort_keys=True,
        )
        + "\n"
    )
    ctx.write_file(request_path, data)
    ctx.note(
        "# Submit one synthetic check. An uncertain response is resumed with this same key and request.\n"
        f"curl --disable --silent --show-error --fail-with-body --noproxy '*' --max-time 10 "
        f"--config {shlex.quote(str(ctx.root / 'credentials' / 'curl.conf'))} "
        f"--header 'Content-Type: application/json' --header 'Idempotency-Key: {key}' "
        f"--data-binary @{shlex.quote(str(request_path))} http://127.0.0.1:{ctx.port}/v1/ingest"
    )
    response = request(ctx, "/v1/ingest", token, data=read_owned(request_path), key=key)
    validate_ingest(response)
    receipts["ingest"] = response
    ctx.save()
    return response


def verify_reads(ctx: Context, python: str, receipt: dict[str, Any]) -> None:
    helper = ctx.source / "scripts" / "verify-retrieval.py"
    common = [
        python,
        str(helper),
        "--base-url",
        f"http://127.0.0.1:{ctx.port}",
        "--credential-file",
        str(ctx.root / "credentials" / "admin.token"),
        "--deadline",
        "120",
        "--request-timeout",
        "10",
        "--max-attempts",
        "30",
    ]
    checks = [
        (
            "attic",
            "--evidence-id",
            receipt["result"]["evidence_id"],
            ["--payload-file", str(ctx.root / "checks" / "payload.txt")],
        )
    ]
    if ctx.semantic:
        checks.append(("semantic", "--fact-id", receipt["result"]["fact_ids"][0], []))
    for name, flag, identity, extra in checks:
        raw = ctx.command(common + [flag, identity] + extra, timeout=135)
        try:
            value = json.loads(raw)
            field = "evidence_id" if name == "attic" else "fact_id"
            if value.get("status") != "verified" or value.get(field) != identity:
                raise ValueError("unexpected proof")
        except (ValueError, AttributeError) as error:
            raise InstallError(
                f"{name} helper did not report the expected verified result"
            ) from error
        ctx.state["receipts"][name] = value
        ctx.save()
        ctx.note(json.dumps(value))
