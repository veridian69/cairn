#!/usr/bin/env python3
"""Delete only an exactly empty, owned Task 11 prepared-stage namespace."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

NAMESPACE = "cairn-target-acceptance"
GATEWAY_NAMESPACE = "cairn-egress"
OWNER = "cairn.example.invalid/acceptance-owner"
RUN = "cairn.example.invalid/acceptance-run"
REVISION = "cairn.example.invalid/acceptance-revision"
EXPECTED_OWNER = "task11"
EXPECTED_OBJECTS = {
    ("configmaps", "v1", "ConfigMap", "acceptance-scripts"),
    ("configmaps", "v1", "ConfigMap", "kube-root-ca.crt"),
    ("serviceaccounts", "v1", "ServiceAccount", "default"),
}
EXPECTED_PVS = {
    f"cairn-local-pv{number}": f"/mnt/cairn-local/pv{number}" for number in range(1, 5)
}
RUN_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
GIT_REVISION = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RESOURCE = re.compile(r"[a-z0-9][a-z0-9.-]*")
ENDPOINTS_DEPRECATION_WARNING = (
    "Warning: v1 Endpoints is deprecated in v1.33+; use "
    "discovery.k8s.io/v1 EndpointSlice"
)
ENDPOINTS_LIST_ARGUMENTS = ("get", "endpoints", "-n", NAMESPACE, "-o", "json")


class CleanupRefusal(RuntimeError):
    """The supplied evidence or discovered target state is unsafe to delete."""


def _read_report(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not SHA256.fullmatch(expected_sha256):
        raise CleanupRefusal("report SHA-256 is malformed")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size > 1024 * 1024
        ):
            raise CleanupRefusal("report file identity is unsafe")
        content = stream.read()
    observed_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(observed_sha256, expected_sha256):
        raise CleanupRefusal("immutable report SHA-256 does not match")
    try:
        report = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CleanupRefusal("immutable report is not valid JSON") from error
    if not isinstance(report, dict):
        raise CleanupRefusal("immutable report is not a JSON object")
    return report


def _kubectl(*arguments: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["kubectl", *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise CleanupRefusal(f"kubectl {' '.join(arguments[:2])} failed") from error
    # kubectl 1.35 emits this fixed client-side warning for the otherwise valid
    # core/v1 Endpoints list.  Keep the exception tied to that exact command and
    # text; every server diagnostic and all other stderr remain a refusal.
    allowed_stderr = arguments == ENDPOINTS_LIST_ARGUMENTS and result.stderr in {
        ENDPOINTS_DEPRECATION_WARNING,
        ENDPOINTS_DEPRECATION_WARNING + "\n",
    }
    if result.returncode != 0 or (result.stderr and not allowed_stderr):
        raise CleanupRefusal(f"kubectl {' '.join(arguments[:2])} failed")
    return result.stdout


def _json_output(output: str, operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise CleanupRefusal(f"kubectl {operation} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise CleanupRefusal(f"kubectl {operation} returned a non-object")
    return payload


def _require_identity(item: dict[str, Any], run_id: str, revision: str) -> None:
    labels = item.get("metadata", {}).get("labels") or {}
    expected = {OWNER: EXPECTED_OWNER, RUN: run_id, REVISION: revision}
    if any(labels.get(key) != value for key, value in expected.items()):
        kind = item.get("kind", "Object")
        name = item.get("metadata", {}).get("name", "<unnamed>")
        raise CleanupRefusal(f"{kind}/{name} lacks exact prior-run ownership")


def _validate_report(report: dict[str, Any], run_id: str, revision: str) -> None:
    if (
        report.get("status") != "failed"
        or report.get("stage") != "prepared"
        or report.get("run_id") != run_id
        or report.get("repository_revision") != revision
    ):
        raise CleanupRefusal(
            "report is not the exact failed:prepared prior run and revision"
        )


def _validate_namespace(run_id: str, revision: str) -> None:
    namespace = _json_output(
        _kubectl("get", "namespace", NAMESPACE, "-o", "json"), "get namespace"
    )
    if namespace.get("metadata", {}).get("name") != NAMESPACE:
        raise CleanupRefusal("kubectl returned the wrong namespace")
    _require_identity(namespace, run_id, revision)


def _require_no_phase_or_gateway() -> None:
    phase = _kubectl(
        "get",
        "configmap",
        "acceptance-state",
        "-n",
        NAMESPACE,
        "--ignore-not-found",
        "-o",
        "json",
    )
    if phase.strip():
        raise CleanupRefusal("durable acceptance phase exists; use recovery")
    gateway = _kubectl(
        "get",
        "namespace",
        GATEWAY_NAMESPACE,
        "--ignore-not-found",
        "-o",
        "json",
    )
    if gateway.strip():
        raise CleanupRefusal("gateway namespace exists; use recovery")


def _validate_pvs() -> None:
    payload = _json_output(_kubectl("get", "pv", "-o", "json"), "get pv")
    items = payload.get("items")
    if not isinstance(items, list):
        raise CleanupRefusal("PV inventory has no item list")
    local_pvs = {
        item.get("metadata", {}).get("name"): item
        for item in items
        if isinstance(item, dict)
        and item.get("spec", {}).get("storageClassName") == "cairn-local"
    }
    if set(local_pvs) != set(EXPECTED_PVS):
        raise CleanupRefusal("the exact four cairn-local PVs are not present")
    for name, expected_path in EXPECTED_PVS.items():
        pv = local_pvs[name]
        spec = pv.get("spec", {})
        if (
            pv.get("status", {}).get("phase") != "Available"
            or spec.get("claimRef") is not None
            or spec.get("persistentVolumeReclaimPolicy") != "Retain"
            or spec.get("local", {}).get("path") != expected_path
        ):
            raise CleanupRefusal(f"PV {name} is not Available, unclaimed and Retain")


def _discover_resources() -> list[str]:
    output = _kubectl(
        "api-resources", "--verbs=list", "--namespaced=true", "-o", "name"
    )
    resources = [line.strip() for line in output.splitlines() if line.strip()]
    if (
        not resources
        or len(resources) != len(set(resources))
        or any(not RESOURCE.fullmatch(resource) for resource in resources)
        or not {"configmaps", "serviceaccounts"}.issubset(resources)
    ):
        raise CleanupRefusal("namespaced API discovery is empty or malformed")
    return sorted(resources)


def _inventory_all_namespaced_resources(
    resources: list[str], run_id: str, revision: str
) -> None:
    inventory: list[tuple[tuple[str, str, str, str], dict[str, Any]]] = []
    for resource in resources:
        payload = _json_output(
            _kubectl("get", resource, "-n", NAMESPACE, "-o", "json"),
            f"list {resource}",
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise CleanupRefusal(f"namespaced list for {resource} has no items")
        for item in items:
            if not isinstance(item, dict):
                raise CleanupRefusal(f"namespaced list for {resource} is malformed")
            metadata = item.get("metadata", {})
            identity = (
                resource,
                str(item.get("apiVersion", "")),
                str(item.get("kind", "")),
                str(metadata.get("name", "")),
            )
            if metadata.get("namespace") != NAMESPACE or not all(identity):
                raise CleanupRefusal(f"namespaced list for {resource} is malformed")
            inventory.append((identity, item))
    identities = [identity for identity, _item in inventory]
    if len(identities) != len(set(identities)):
        raise CleanupRefusal("namespaced inventory contains duplicate objects")
    unexpected = sorted(set(identities) - EXPECTED_OBJECTS)
    if unexpected:
        resource, _api_version, kind, name = unexpected[0]
        raise CleanupRefusal(
            f"unexpected namespaced object {resource}/{kind}/{name}; use recovery"
        )
    missing = sorted(EXPECTED_OBJECTS - set(identities))
    if missing:
        raise CleanupRefusal("prepared namespace lacks the exact expected object set")
    for _identity, item in inventory:
        _require_identity(item, run_id, revision)


def cleanup_prepared_run(
    report_path: Path,
    report_sha256: str,
    run_id: str,
    revision: str,
) -> None:
    if not RUN_ID.fullmatch(run_id) or not GIT_REVISION.fullmatch(revision):
        raise CleanupRefusal("prior run ID or revision is malformed")
    report = _read_report(report_path, report_sha256)
    _validate_report(report, run_id, revision)
    _validate_namespace(run_id, revision)
    _require_no_phase_or_gateway()
    _validate_pvs()
    resources = _discover_resources()
    _inventory_all_namespaced_resources(resources, run_id, revision)
    _kubectl("delete", "namespace", NAMESPACE, "--wait=false")
    _kubectl(
        "wait",
        "--for=delete",
        f"namespace/{NAMESPACE}",
        "--timeout=180s",
        timeout=210,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete one exact failed:prepared Task 11 namespace"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--delete", action="store_true", required=True)
    arguments = parser.parse_args()
    try:
        cleanup_prepared_run(
            arguments.report,
            arguments.report_sha256,
            arguments.run_id,
            arguments.revision,
        )
    except (CleanupRefusal, OSError) as error:
        print(f"reference prepared cleanup refused: {error}", file=sys.stderr)
        return 2
    print(f"deleted exact owned namespace {NAMESPACE}; immutable report preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
