#!/usr/bin/env python3
"""Create the client-cutover workload principals, grants and credentials.

Operator script for the client cutover design. Runs only against an instance
Operator names, with his own credential, and writes one credential file per label.
Idempotency keys are deterministic so a rerun replays rather than duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

NAMESPACE = uuid.UUID("7e1b9d3a-5c2f-4e8a-9b61-2f0c4d7a8e55")
GRANT_OPERATIONS = ["ingest", "retrieve"]


def key(realm: str, label: str, step: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{realm}:{label}:{step}"))


def plan(
    realm: str, labels: list[str], expires_at: str, *, reissue: int = 0
) -> list[dict[str, Any]]:
    credential_step = f"credential:reissue:{reissue}" if reissue > 0 else "credential"
    steps: list[dict[str, Any]] = []
    for label in labels:
        steps.append(
            {
                "label": label,
                "operation": "create-principal",
                "idempotency_key": key(realm, label, "principal"),
                "body": {"realm_id": realm, "kind": "workload", "label": label},
            }
        )
        steps.append(
            {
                "label": label,
                "operation": "create-grant",
                "idempotency_key": key(realm, label, "grant"),
                "body": {
                    "realm_id": realm,
                    "grant": {
                        "principal_id": None,
                        "realm_id": realm,
                        "segments": [],
                        "operations": list(GRANT_OPERATIONS),
                        "read_clearance": "internal",
                        "write_classifications": ["internal"],
                        "expires_at": expires_at,
                    },
                },
            }
        )
        steps.append(
            {
                "label": label,
                "operation": "issue-credential",
                "idempotency_key": key(realm, label, credential_step),
                "body": {
                    "realm_id": realm,
                    "principal_id": None,
                    "expires_at": expires_at,
                },
            }
        )
    return steps


def post(
    client: httpx.Client, operation: str, body: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    response = client.post(
        f"/v1/{operation}", json=body, headers={"Idempotency-Key": idempotency_key}
    )
    if response.status_code >= 400:
        raise SystemExit(f"{operation} failed with status {response.status_code}")
    payload: dict[str, Any] = response.json()
    return payload


def check_base_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise SystemExit("--base-url is not a valid URL") from None
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise SystemExit("--base-url must not contain userinfo, query or fragment")
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and host in ("127.0.0.1", "::1"):
        return
    raise SystemExit("--base-url must be https, or http on 127.0.0.1 or ::1")


def check_target(target: Path, label: str) -> bool:
    """Refuse a symlink or non-regular file at the target; return True if one exists."""
    existed = target.is_symlink() or target.exists()
    if existed and (target.is_symlink() or not target.is_file()):
        raise SystemExit(
            f"refusing to write credential for {label}: target is a symlink or not a regular file"
        )
    return existed


def write_credential(target: Path, label: str, plaintext: str) -> bool:
    """Write the plaintext at mode 0600; return True if a file was overwritten.

    The plaintext is issued once and cannot be fetched again, so an existing
    file is never truncated in place: the new token goes to a private temporary
    file in the same directory, is synced, then atomically replaces the target.
    A failed write leaves the old credential usable.
    """
    existed = check_target(target, label)
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    except OSError:
        raise SystemExit(
            f"refusing to write credential for {label}: cannot open target safely"
        ) from None
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(plaintext + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return existed


# Cairn's canonical token shape (cairn.authority.credentials.TOKEN_PATTERN).
TOKEN_PATTERN = re.compile(
    r"cairn1\.([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.[A-Za-z0-9_-]{43}"
)


def holds_credential(client: httpx.Client, target: Path, credential_id: str) -> bool:
    """True if the file holds a token for this credential id that Cairn accepts.

    The id is public, so a matching id proves nothing about the secret; the
    token must also authenticate against the read-only /v1/instance.
    """
    try:
        candidate = target.read_bytes().decode("utf-8").strip()
    except UnicodeDecodeError:
        return False
    match = TOKEN_PATTERN.fullmatch(candidate)
    if match is None or match.group(1) != credential_id:
        return False
    response = client.get(
        "/v1/instance", headers={"Authorization": f"Bearer {candidate}"}
    )
    if response.status_code == 200:
        return True
    if response.status_code == 401:
        return False
    raise SystemExit(f"instance check failed with status {response.status_code}")


def main(argv: list[str], transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--reissue", type=int, default=0)
    parser.add_argument("--label", action="append", dest="labels", required=True)
    args = parser.parse_args(argv)
    check_base_url(args.base_url)
    labels = args.labels
    token = args.credential_file.read_text(encoding="utf-8").strip()
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.output_dir, 0o700)
    for (
        label
    ) in labels:  # refuse before the first POST so no credential is issued and lost
        check_target(args.output_dir / label, label)
    principal_ids: dict[str, str] = {}
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for step in plan(args.realm, labels, args.expires_at, reissue=args.reissue):
            label, operation, body = step["label"], step["operation"], step["body"]
            if operation == "create-grant":
                body["grant"]["principal_id"] = principal_ids[label]
            elif operation == "issue-credential":
                body["principal_id"] = principal_ids[label]
            result = post(client, operation, body, step["idempotency_key"])
            outcome = result.get("outcome")
            if operation == "create-principal":
                principal_ids[label] = result["result"]["principal_id"]
                print(
                    json.dumps(
                        {
                            "label": label,
                            "step": operation,
                            "outcome": outcome,
                            "principal_id": principal_ids[label],
                        }
                    )
                )
            elif operation == "create-grant":
                print(
                    json.dumps(
                        {
                            "label": label,
                            "step": operation,
                            "outcome": outcome,
                            "grant_id": result["result"]["grant_id"],
                        }
                    )
                )
            else:
                plaintext = result["result"].get("plaintext")
                target = args.output_dir / label
                if plaintext:
                    line = {
                        "label": label,
                        "step": operation,
                        "outcome": outcome,
                        "written": str(target),
                    }
                    if write_credential(target, label, plaintext):
                        line["overwritten"] = True
                    print(json.dumps(line))
                elif target.exists() and holds_credential(
                    client, target, result["result"]["credential_id"]
                ):
                    print(
                        json.dumps(
                            {
                                "label": label,
                                "step": operation,
                                "outcome": outcome,
                                "written": None,
                                "note": "replayed; existing file kept",
                            }
                        )
                    )
                else:
                    print(
                        json.dumps(
                            {
                                "label": label,
                                "step": operation,
                                "outcome": outcome,
                                "written": None,
                                "note": "credential already issued and not in the file; revoke it, then rerun with --reissue <next number> for this label",
                            }
                        ),
                        file=sys.stderr,
                    )
                    return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
