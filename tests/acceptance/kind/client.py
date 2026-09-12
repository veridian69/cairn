"""The in-cluster `/v1` round trip, discharged from a client pod.

This is the slice 5 task 12 checkbox Operator moved here on 8 August 2026:
start an instance from the published image inside a kind cluster,
authenticate, ingest and read back the audit event through `/v1` — from
another pod, over the Service, through the ingress the operator opened
and nothing wider.

It reads the credential from a file and never from `argv`, because a
container's command line is readable from the host and the audit trail
this proves is worth nothing if proving it leaks the token. It also
never prints the token, including in a failure message.

Standard library only: the request shapes are the ones
`scripts/smoke-image` already sends with curl, and the Cairn image ships
no curl.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

TIMEOUT_SECONDS = 15.0


class RoundTripError(Exception):
    """A refusal, a wrong status or an answer that did not contain what
    was ingested. The message is safe to print."""


class DeniedError(RoundTripError):
    """The instance answered, and said no.

    Separate from its parent because P-68's credential cross-check
    predicts a *status* — I-26's 401 — and "the request did not work"
    would be satisfied by a timeout, a wrong URL or an instance that was
    never running.
    """

    def __init__(self, path: str, status: int, detail: str) -> None:
        super().__init__(f"{path} returned HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _post(
    base_url: str,
    path: str,
    token: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise DeniedError(path, error.code, detail) from None
    except OSError as error:
        raise RoundTripError(f"{path} did not complete: {error}") from None
    answer: dict[str, Any] = json.loads(body)
    return answer


def _ingest(base_url: str, token: str, marker: str, *, exact: bool = False) -> str:
    payload: dict[str, Any] = {
        "scope": {"realm": "local", "segments": []},
        "classification": "internal",
        "source_type": "human",
        "facts": [{"body": marker}],
    }
    if exact:
        # Different text from the fact body gives the restore rehearsal two
        # independent pre-backup values to read. Store attribution is proved
        # separately by direct Attic and FalkorDB probes in the harness.
        payload["evidence_payload"] = f"attic evidence {marker}"
    answer = _post(
        base_url,
        "/v1/ingest",
        token,
        payload,
        {"Idempotency-Key": str(uuid.uuid4())},
    )
    receipt = answer.get("mutation_receipt") or {}
    mutation_id = receipt.get("mutation_id")
    if not isinstance(mutation_id, str) or not mutation_id:
        raise RoundTripError("/v1/ingest response carried no mutation_id")
    return mutation_id


def _read_audit_events(base_url: str, token: str) -> list[Any]:
    answer = _post(
        base_url,
        "/v1/read-audit-events",
        token,
        {
            "realm_id": "local",
            "scope_prefix": [],
            "after_sequence": 0,
            "limit": 500,
        },
    )
    events = answer.get("events")
    if not isinstance(events, list):
        raise RoundTripError("/v1/read-audit-events returned no events array")
    return events


def _carries_ingest(events: list[Any], mutation_id: str) -> bool:
    return any(
        event.get("mutation_id") == mutation_id and event.get("action_code") == "ingest"
        for event in events
    )


def _require_audit_event(base_url: str, token: str, mutation_id: str) -> None:
    if not _carries_ingest(_read_audit_events(base_url, token), mutation_id):
        raise RoundTripError(f"no ingest audit event for mutation {mutation_id}")


def _require_hit(answer: dict[str, Any], marker: str, surface: str) -> None:
    hits = answer.get("hits")
    if not isinstance(hits, list) or not any(
        isinstance(hit, dict) and hit.get("body") == marker for hit in hits
    ):
        raise RoundTripError(f"{surface} retrieve did not return the restored fact")


def _require_restored_rest(base_url: str, token: str, marker: str) -> None:
    fts_phrase = marker.replace('"', '""')
    request = {
        "scope": {"realm": "local", "segments": []},
        "query": f'"attic evidence {fts_phrase}"',
        "budget": 65536,
        "trust_filters": ["candidate"],
    }
    _require_hit(_post(base_url, "/v1/retrieve", token, request), marker, "REST")


def _require_restored_mcp(base_url: str, token: str, marker: str) -> None:
    request = {
        "scope": {"realm": "local", "segments": []},
        "query": marker,
        "budget": 65536,
        "trust_filters": ["candidate"],
    }
    frame = _post(
        base_url,
        "/v1/mcp",
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "retrieve", "arguments": request},
        },
        {"Accept": "application/json"},
    )
    result = frame.get("result")
    if (
        not isinstance(result, dict)
        or result.get("isError") is not False
        or not isinstance(result.get("structuredContent"), dict)
    ):
        raise RoundTripError("MCP retrieve returned no successful structured result")
    _require_hit(result["structuredContent"], marker, "MCP")


def _require_restored_reads(base_url: str, token: str, marker: str) -> None:
    _require_restored_rest(base_url, token, marker)
    _require_restored_mcp(base_url, token, marker)


def _wait_for_fixture_read(base_url: str, token: str, marker: str) -> None:
    for _ in range(120):
        try:
            _require_restored_rest(base_url, token, marker)
            return
        except RoundTripError:
            time.sleep(0.25)
    raise RoundTripError("the restore fixture did not become readable")


def main(argv: list[str]) -> int:
    mode, base_url, subject, token_path = argv[1], argv[2], argv[3], argv[4]
    with open(token_path, encoding="utf-8") as handle:
        token = handle.read().strip()
    try:
        if mode == "absent":
            # P-68's fixture cross-check. The read has to *succeed* and
            # then not contain the other instance's mutation: a refusal
            # printed as absence would be an isolation claim nothing
            # established.
            present = _carries_ingest(_read_audit_events(base_url, token), subject)
            print("present" if present else "absent", flush=True)
            return 1 if present else 0
        if mode == "denied":
            # P-68's credential cross-check, and the reason it prints a
            # status rather than a verdict: the prediction is I-26's 401,
            # and every other answer — including success — is the run's
            # to fail on.
            try:
                _read_audit_events(base_url, token)
            except DeniedError as denial:
                print(f"http-{denial.status}", flush=True)
            else:
                print("accepted", flush=True)
            return 0
        if mode == "ingest":
            mutation_id = _ingest(base_url, token, subject)
        elif mode == "restore-fixture":
            mutation_id = _ingest(base_url, token, subject, exact=True)
            _wait_for_fixture_read(base_url, token, subject)
        elif mode == "audit":
            mutation_id = subject
        elif mode in {"restored-rest", "restored-mcp"}:
            if len(argv) != 6:
                raise RoundTripError(
                    "restored mode requires the pre-backup mutation id"
                )
            mutation_id = argv[5]
            if mode == "restored-rest":
                _require_restored_rest(base_url, token, subject)
            else:
                _require_restored_mcp(base_url, token, subject)
        else:
            raise RoundTripError(f"unknown mode {mode}")
        _require_audit_event(base_url, token, mutation_id)
    except RoundTripError as error:
        print(f"failed {error}", flush=True)
        return 1
    print(f"ok {mutation_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
