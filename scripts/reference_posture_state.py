#!/usr/bin/env python3
"""Pure fail-closed validation for the Task 11a posture evidence boundary."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn, cast

import yaml

from cairn.screening import SecretScreen  # type: ignore[import-untyped]

_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from reference_acceptance_state import (  # noqa: E402
    HarnessRefusal,
    canonical_image_reference,
    runtime_config_digest,
    runtime_image_digest,
    validate_cilium_report_images,
)


class PostureRefusal(ValueError):
    """The target is ambiguous, unsafe, or cannot support the claimed posture."""


REQUIRED_CHECKS = (
    "preflight",
    "pv-handoff",
    "cairn-runtime",
    "cairn-wal",
    "cairn-lease",
    "cairn-restart",
    "cairn-integrity",
    "falkordb-runtime",
    "falkordb-auth",
    "falkordb-restart",
    "retrieval-composition",
    "provider-delivery",
    "dns",
    "external-https",
    "pod-https-denied",
    "service-https-denied",
    "node-https-denied",
    "squid-denied",
    "label-inventory",
    "probe-cleanup",
    "secret-scan",
)

_REVISION = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"task11a-[0-9a-f]{7,12}-[0-9]{2}")
_CLUSTER_CIDR_KEYS = frozenset(("pod", "service", "node"))
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPOSITORY = Path(__file__).resolve().parents[1]


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"{name} is required")
    return value


_EXPECTED_HOST = _required_environment("REFERENCE_EXPECTED_HOST")
_EXPECTED_NODE = _required_environment("REFERENCE_NODE_NAME")
_BASELINE_NAMESPACE = _required_environment("REFERENCE_BASELINE_NAMESPACE")
_GATEWAY_NAMESPACE = _required_environment("REFERENCE_GATEWAY_NAMESPACE")
_STORAGE_CLASS = _required_environment("REFERENCE_STORAGE_CLASS")
_TASK11_REVISION = _required_environment("REFERENCE_BASELINE_REVISION")
_TASK11_RUN_ID = _required_environment("REFERENCE_BASELINE_RUN_ID")
_TASK11_REPORT_HASHES = {
    "primary": _required_environment("REFERENCE_BASELINE_PRIMARY_SHA256"),
    "cilium-supplement": _required_environment("REFERENCE_BASELINE_CILIUM_SHA256"),
}
_RENDER_PATHS = {
    "kubernetes-retrieval": "deploy/kustomize/rendered/kubernetes-retrieval.yaml",
    "egress-gateway-reference": "deploy/kustomize/rendered/egress-gateway-reference.yaml",
}
_REQUIRED_TOOLS = frozenset(
    (
        "awk",
        "cat",
        "ctr",
        "dirname",
        "docker",
        "firewall-cmd",
        "getenforce",
        "git",
        "grep",
        "helm",
        "ip",
        "jq",
        "kubectl",
        "mkdir",
        "mktemp",
        "posture-python",
        "realpath",
        "rm",
        "sed",
        "seq",
        "setsid",
        "sha256sum",
        "sleep",
        "socat",
        "sort",
        "ss",
        "stat",
        "tail",
        "timeout",
        "wc",
    )
)
_EXPECTED_CHECK_RESULTS = {
    "preflight": "validated",
    "pv-handoff": "group-writable",
    "cairn-runtime": "65532:0:0 65532",
    "cairn-wal": "wal",
    "cairn-lease": "locked",
    "cairn-restart": "replaced",
    "cairn-integrity": "ok",
    "falkordb-runtime": "10001:0:0 10001",
    "falkordb-auth": "noauth-refused-then-authenticated",
    "falkordb-restart": "persisted",
    "retrieval-composition": "enabled",
    "provider-delivery": "delivered",
    "dns": "resolved",
    "external-https": "connected",
    "pod-https-denied": "blocked",
    "service-https-denied": "blocked",
    "node-https-denied": "blocked",
    "squid-denied": "blocked",
    "label-inventory": "complete",
    "probe-cleanup": "complete",
    "secret-scan": "clean",
}
_FAILURE_PHASES = frozenset(
    (
        "gateway-apply",
        "gateway-applied",
        "gateway-poststate",
        "namespace-apply",
        "secret-apply",
        "instance-apply",
        "bootstrap",
        "bootstrap-scale-down",
        "bootstrap-cairn-stop",
        "bootstrap-pod-apply",
        "bootstrap-pod-ready",
        "bootstrap-catalogue-migrate",
        "bootstrap-command-exec",
        "bootstrap-pod-delete",
        "bootstrap-scale-up",
        "bootstrap-cairn-rollout",
        "posture-proof",
        "posture-rollout",
        "pv-handoff",
        "cairn-runtime",
        "cairn-catalogue",
        "cairn-restart",
        "falkordb-runtime",
        "falkordb-auth",
        "falkordb-restart",
        "retrieval-composition",
        "probe-apply",
        "provider-delivery",
        "network-posture",
        "label-inventory",
        "temporary-cleanup",
        "secret-screen",
        "success-report",
    )
)
_GATEWAY_OBJECTS = (
    ("ConfigMap", "egress-gateway-config"),
    ("Service", "cairn-egress-gateway"),
    ("Deployment", "cairn-egress-gateway"),
    ("NetworkPolicy", "egress-gateway-allow-cairn-ingress"),
    ("NetworkPolicy", "egress-gateway-allow-egress"),
    ("NetworkPolicy", "egress-gateway-default-deny"),
)
_RENDERED_EXCLUSIONS = cast(
    tuple[ipaddress.IPv4Network, ...],
    tuple(
        ipaddress.ip_network(cidr)
        for cidr in (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "169.254.0.0/16",
        )
    ),
)


def _refuse(message: str) -> NoReturn:
    raise PostureRefusal(message)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _refuse(f"{field} must be an object")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        _refuse(f"{field} must be an array")
    return value


def _mapping_list(value: object, field: str) -> list[dict[str, object]]:
    return [
        _mapping(item, f"{field}[{index}]")
        for index, item in enumerate(_list(value, field))
    ]


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _refuse(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        _refuse(f"{field} must be an integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        _refuse(f"{field} must be a boolean")
    return value


def _exact_keys(value: dict[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        _refuse(f"{field} has an unexpected schema")


def _digest(value: object, field: str) -> str:
    result = _string(value, field)
    if _DIGEST.fullmatch(result) is None:
        _refuse(f"{field} must be a canonical sha256 digest")
    return result


def _sha256(value: object, field: str) -> str:
    result = _string(value, field)
    if _SHA256.fullmatch(result) is None:
        _refuse(f"{field} must be a lowercase SHA-256")
    return result


def _lock_values() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (
        (_REPOSITORY / "deploy/images.lock").read_text(encoding="utf-8").splitlines()
    ):
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def _validate_identifiers(
    *, revision: object, namespace: object, owner: object, run_id: object
) -> tuple[str, str, str, str]:
    revision_text = _string(revision, "revision")
    run_text = _string(run_id, "run_id")
    namespace_text = _string(namespace, "namespace")
    owner_text = _string(owner, "owner")
    if _REVISION.fullmatch(revision_text) is None:
        _refuse("revision must be a full lowercase Git SHA-1")
    if _RUN_ID.fullmatch(run_text) is None:
        _refuse("run_id is malformed")
    revision_prefix = run_text.removeprefix("task11a-").rsplit("-", 1)[0]
    if not revision_text.startswith(revision_prefix):
        _refuse("run_id does not identify the supplied revision")
    if namespace_text != f"cairn-{run_text}":
        _refuse("namespace does not match run_id")
    if owner_text != "task11a":
        _refuse("owner must be task11a")
    return revision_text, namespace_text, owner_text, run_text


def _canonical_absolute_path(path: str, field: str) -> str:
    """Return the lexical POSIX canonical path without filesystem resolution."""

    if not path.startswith("/"):
        _refuse(f"{field} must be an absolute non-root path")
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    canonical = "/" + "/".join(parts)
    if path != canonical:
        _refuse(f"{field} must be a canonical absolute path")
    if canonical == "/":
        _refuse(f"{field} must be an absolute non-root path")
    return canonical


def _pv_identity(value: object, field: str) -> dict[str, str]:
    pv = _mapping(value, field)
    metadata = _mapping(pv.get("metadata"), f"{field}.metadata")
    spec = _mapping(pv.get("spec"), f"{field}.spec")
    local = _mapping(spec.get("local"), f"{field}.spec.local")
    name = _string(metadata.get("name"), f"{field}.metadata.name")
    path = _string(local.get("path"), f"{field}.spec.local.path")
    return {
        "name": name,
        "path": _canonical_absolute_path(path, f"{field}.spec.local.path"),
    }


def _candidate_pv(value: object, field: str) -> dict[str, object] | None:
    pv = _mapping(value, field)
    identity = _pv_identity(pv, field)
    status = _mapping(pv.get("status"), f"{field}.status")
    spec = _mapping(pv.get("spec"), f"{field}.spec")
    if (
        status.get("phase") != "Available"
        or spec.get("persistentVolumeReclaimPolicy") != "Retain"
        or spec.get("storageClassName") != _STORAGE_CLASS
        or spec.get("claimRef") is not None
    ):
        return None
    if spec.get("accessModes") != ["ReadWriteOncePod"]:
        _refuse(f"{field} must offer exactly ReadWriteOncePod")
    capacity = _mapping(spec.get("capacity"), f"{field}.spec.capacity")
    storage = _string(capacity.get("storage"), f"{field}.spec.capacity.storage")
    match = re.fullmatch(
        r"(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<suffix>[KMGTPE]i|[kKMGTPE]|m|[eE][+-]?[0-9]+)?",
        storage,
    )
    if match is None:
        _refuse(f"{field} storage capacity is malformed")
    multipliers = {
        "": Decimal(1),
        "m": Decimal("0.001"),
        "k": Decimal(1000),
        "K": Decimal(1000),
        "M": Decimal(1000) ** 2,
        "G": Decimal(1000) ** 3,
        "T": Decimal(1000) ** 4,
        "P": Decimal(1000) ** 5,
        "E": Decimal(1000) ** 6,
        "Ki": Decimal(1024),
        "Mi": Decimal(1024) ** 2,
        "Gi": Decimal(1024) ** 3,
        "Ti": Decimal(1024) ** 4,
        "Pi": Decimal(1024) ** 5,
        "Ei": Decimal(1024) ** 6,
    }
    suffix = match.group("suffix") or ""
    try:
        multiplier = (
            Decimal(10) ** int(suffix[1:])
            if suffix.startswith(("e", "E"))
            else multipliers[suffix]
        )
        storage_bytes = Decimal(match.group("number")) * multiplier
    except (InvalidOperation, KeyError, ValueError, OverflowError) as error:
        raise PostureRefusal(f"{field} storage capacity is malformed") from error
    if storage_bytes < Decimal(10) * (Decimal(1024) ** 3):
        _refuse(f"{field} storage capacity is smaller than the 10Gi claim")
    return {
        **identity,
        "access_modes": ["ReadWriteOncePod"],
        "capacity": storage,
    }


def _validate_cluster_cidrs(cluster_cidrs: object) -> dict[str, str]:
    cidrs = _mapping(cluster_cidrs, "cluster_cidrs")
    if set(cidrs) != _CLUSTER_CIDR_KEYS:
        _refuse("cluster_cidrs must contain exactly pod, service and node")
    result: dict[str, str] = {}
    for name in sorted(_CLUSTER_CIDR_KEYS):
        value = _string(cidrs[name], f"cluster_cidrs.{name}")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as error:
            raise PostureRefusal(f"cluster CIDR {name} is malformed") from error
        if not isinstance(network, ipaddress.IPv4Network) or str(network) != value:
            _refuse(f"cluster CIDR {name} must be canonical IPv4")
        if not any(network.subnet_of(exclusion) for exclusion in _RENDERED_EXCLUSIONS):
            _refuse(f"cluster CIDR {name} is outside the rendered exclusions")
        result[name] = value
    return result


def runtime_expectations_from_files(
    dockerfile: Path, rendered_manifest: Path, cairn_image: str
) -> list[dict[str, object]]:
    """Derive the two workload identities from committed executable material."""

    try:
        docker_text = dockerfile.read_text(encoding="utf-8")
        documents = list(
            yaml.safe_load_all(rendered_manifest.read_text(encoding="utf-8"))
        )
        canonical_cairn = canonical_image_reference(cairn_image)
    except (OSError, UnicodeError, yaml.YAMLError, HarnessRefusal) as error:
        raise PostureRefusal("runtime source material is unreadable") from error
    users = [
        match.group(1)
        for line in docker_text.splitlines()
        if (match := re.fullmatch(r"\s*USER\s+(\S+)\s*", line)) is not None
    ]
    if not users or users[-1] != "65532:0":
        _refuse("Cairn Dockerfile runtime identity is not 65532:0")
    statefulsets = {
        document.get("metadata", {}).get("name"): document
        for document in documents
        if isinstance(document, dict) and document.get("kind") == "StatefulSet"
    }
    if set(statefulsets) != {"cairn", "falkordb"}:
        _refuse("retrieval render lacks the exact runtime workloads")

    result: list[dict[str, object]] = []
    expected = {
        "cairn": {"uid": 65532, "gid": 0, "fs_group": 65532},
        "falkordb": {"uid": 10001, "gid": 0, "fs_group": 10001},
    }
    for workload in ("cairn", "falkordb"):
        statefulset = statefulsets[workload]
        pod_spec = statefulset.get("spec", {}).get("template", {}).get("spec", {})
        containers = pod_spec.get("containers")
        matches = (
            [item for item in containers if item.get("name") == workload]
            if isinstance(containers, list)
            else []
        )
        if len(matches) != 1:
            _refuse(f"runtime render has no unique {workload} container")
        container = matches[0]
        mounts = container.get("volumeMounts")
        data_mounts = (
            [item for item in mounts if item.get("name") == "data"]
            if isinstance(mounts, list)
            else []
        )
        if len(data_mounts) != 1:
            _refuse(f"runtime render has no unique {workload} data mount")
        security = pod_spec.get("securityContext")
        if not isinstance(security, dict):
            _refuse(f"runtime render lacks {workload} security context")
        identity = expected[workload]
        if (
            security.get("runAsNonRoot") is not True
            or security.get("fsGroup") != identity["fs_group"]
            or security.get("fsGroupChangePolicy") != "OnRootMismatch"
        ):
            _refuse(f"runtime render has unsafe {workload} group handoff")
        if workload == "cairn":
            if (
                security.get("runAsUser") is not None
                or security.get("runAsGroup") is not None
            ):
                _refuse("Cairn render must inherit the Dockerfile identity")
            image = canonical_cairn
        else:
            if security.get("runAsUser") != 10001 or security.get("runAsGroup") != 0:
                _refuse("FalkorDB render runtime identity is not 10001:0")
            try:
                image = canonical_image_reference(
                    _string(container.get("image"), "FalkorDB image")
                )
            except HarnessRefusal as error:
                raise PostureRefusal("FalkorDB image is malformed") from error
        result.append(
            {
                "workload": workload,
                "container": workload,
                "mount_path": _string(
                    data_mounts[0].get("mountPath"), f"{workload} mount path"
                ),
                "uid": identity["uid"],
                "primary_gid": identity["gid"],
                "supplementary_gids": [identity["fs_group"]],
                "fs_group": identity["fs_group"],
                "image_reference": image,
            }
        )
    return result


def _validate_target_identity(value: object) -> dict[str, object]:
    target = _mapping(value, "target_identity")
    _exact_keys(target, {"host", "kubernetes", "cilium"}, "target_identity")
    host = _mapping(target["host"], "target_identity.host")
    _exact_keys(host, {"hostname", "distribution", "selinux"}, "target_identity.host")
    if host != {
        "hostname": _EXPECTED_HOST,
        "distribution": "Fedora Linux 44",
        "selinux": "Enforcing",
    }:
        _refuse("target host or SELinux identity drifted")
    kubernetes = _mapping(target["kubernetes"], "target_identity.kubernetes")
    _exact_keys(
        kubernetes,
        {"server_version", "node", "node_ready", "container_runtime"},
        "target_identity.kubernetes",
    )
    locks = _lock_values()
    if (
        kubernetes.get("server_version") != locks.get("TARGET_KUBERNETES_VERSION")
        or kubernetes.get("node") != _EXPECTED_NODE
        or kubernetes.get("node_ready") is not True
        or not _string(
            kubernetes.get("container_runtime"),
            "target_identity.kubernetes.container_runtime",
        ).startswith("containerd://")
    ):
        _refuse("Kubernetes target identity or node Ready state drifted")
    cilium = _mapping(target["cilium"], "target_identity.cilium")
    _exact_keys(cilium, {"chart", "images"}, "target_identity.cilium")
    if (
        cilium.get("chart")
        != f"cilium-{locks['TARGET_CILIUM_VERSION'].removeprefix('v')}"
    ):
        _refuse("Cilium chart identity drifted")
    images = _mapping_list(cilium.get("images"), "target_identity.cilium.images")
    try:
        validate_cilium_report_images(images)
    except HarnessRefusal as error:
        raise PostureRefusal(f"Cilium image identity refused: {error}") from error
    expected_images = {
        "cilium-agent": locks["TARGET_CILIUM_AGENT_IMAGE"],
        "cilium-operator": locks["TARGET_CILIUM_OPERATOR_IMAGE"],
        "cilium-envoy": locks["TARGET_CILIUM_ENVOY_IMAGE"],
    }
    for item in images:
        container_name = item.get("container_name")
        if not isinstance(container_name, str) or item.get(
            "spec_image"
        ) != expected_images.get(container_name):
            _refuse("Cilium image pin drifted")
    return target


def _validate_storage_identity(value: object) -> dict[str, object]:
    storage = _mapping(value, "storage_identity")
    _exact_keys(
        storage,
        {"storage_class", "provisioner", "volume_binding_mode"},
        "storage_identity",
    )
    if storage != {
        "storage_class": _STORAGE_CLASS,
        "provisioner": "kubernetes.io/no-provisioner",
        "volume_binding_mode": "WaitForFirstConsumer",
    }:
        _refuse("storage class provisioner or binding mode drifted")
    return storage


def _validate_render_identities(value: object) -> list[dict[str, object]]:
    items = _list(value, "render_identities")
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        render = _mapping(item, f"render_identities[{index}]")
        _exact_keys(render, {"name", "path", "sha256"}, f"render_identities[{index}]")
        name = _string(render["name"], f"render_identities[{index}].name")
        path = _string(render["path"], f"render_identities[{index}].path")
        digest = _sha256(render["sha256"], f"render_identities[{index}].sha256")
        if name in names or _RENDER_PATHS.get(name) != path:
            _refuse("render identity is missing, duplicate or unexpected")
        try:
            observed = hashlib.sha256((_REPOSITORY / path).read_bytes()).hexdigest()
        except OSError as error:
            raise PostureRefusal("render identity source is unreadable") from error
        if observed != digest:
            _refuse("render digest does not match committed material")
        names.add(name)
        result.append(render)
    if names != set(_RENDER_PATHS):
        _refuse("render identities must contain the exact two committed renders")
    return result


def _validate_image_identity(value: object, revision: str) -> dict[str, object]:
    image = _mapping(value, "image_identity")
    _exact_keys(
        image,
        {
            "reference",
            "source_revision",
            "local_image_id",
            "target_digest",
            "platform_manifest_digest",
            "config_digest",
            "expected_runtime_digest",
        },
        "image_identity",
    )
    reference = _string(image["reference"], "image_identity.reference")
    try:
        canonical = canonical_image_reference(reference)
    except HarnessRefusal as error:
        raise PostureRefusal("image reference is malformed") from error
    if canonical != reference or ":latest" in reference or "@" in reference:
        _refuse("image reference must be a canonical non-latest tag for the run")
    if image.get("source_revision") != revision:
        _refuse("image revision does not match source revision")
    _digest(image.get("local_image_id"), "image_identity.local_image_id")
    _digest(image.get("target_digest"), "image_identity.target_digest")
    _digest(
        image.get("platform_manifest_digest"),
        "image_identity.platform_manifest_digest",
    )
    config = _digest(image.get("config_digest"), "image_identity.config_digest")
    runtime = _digest(
        image.get("expected_runtime_digest"),
        "image_identity.expected_runtime_digest",
    )
    if runtime != config:
        _refuse("image runtime digest is not the verified OCI config digest")
    return image


def _validate_task11_reports(value: object) -> list[dict[str, object]]:
    items = _list(value, "accepted_task11_reports")
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        report = _mapping(item, f"accepted_task11_reports[{index}]")
        _exact_keys(
            report, {"name", "path", "sha256"}, f"accepted_task11_reports[{index}]"
        )
        name = _string(report["name"], f"accepted_task11_reports[{index}].name")
        path = _canonical_absolute_path(
            _string(report["path"], f"accepted_task11_reports[{index}].path"),
            f"accepted_task11_reports[{index}].path",
        )
        digest = _sha256(report["sha256"], f"accepted_task11_reports[{index}].sha256")
        if name in names or _TASK11_REPORT_HASHES.get(name) != digest:
            _refuse("accepted Task 11 report identity drifted")
        names.add(name)
        result.append({"name": name, "path": path, "sha256": digest})
    if names != set(_TASK11_REPORT_HASHES):
        _refuse(
            "accepted Task 11 reports must contain the exact primary and supplement"
        )
    return result


def _validate_credentials(value: object) -> list[dict[str, object]]:
    items = _list(value, "credential_sources")
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        credential = _mapping(item, f"credential_sources[{index}]")
        _exact_keys(
            credential,
            {"name", "path", "mode", "owner", "group", "non_empty"},
            f"credential_sources[{index}]",
        )
        name = _string(credential["name"], f"credential_sources[{index}].name")
        path = _canonical_absolute_path(
            _string(credential["path"], f"credential_sources[{index}].path"),
            f"credential_sources[{index}].path",
        )
        if (
            name in names
            or name not in {"openai-api-key", "falkordb-password"}
            or credential.get("mode") != "600"
            or credential.get("owner") != "root"
            or credential.get("group") != "root"
            or credential.get("non_empty") is not True
        ):
            _refuse("credential source identity, permission or content is unsafe")
        names.add(name)
        result.append({**credential, "path": path})
    if names != {"openai-api-key", "falkordb-password"}:
        _refuse("credential sources must contain the exact two required files")
    return result


def _validate_tools(value: object) -> list[dict[str, object]]:
    items = _list(value, "tool_inventory")
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        tool = _mapping(item, f"tool_inventory[{index}]")
        _exact_keys(tool, {"name", "path"}, f"tool_inventory[{index}]")
        name = _string(tool["name"], f"tool_inventory[{index}].name")
        path = _canonical_absolute_path(
            _string(tool["path"], f"tool_inventory[{index}].path"),
            f"tool_inventory[{index}].path",
        )
        if name in names or name not in _REQUIRED_TOOLS:
            _refuse("tool inventory is missing, duplicate or unexpected")
        names.add(name)
        result.append({"name": name, "path": path})
    if names != set(_REQUIRED_TOOLS):
        _refuse("tool inventory does not contain every required late tool")
    return result


def _gateway_inventory(
    value: object, *, revision: str, run_id: str, post: bool
) -> list[dict[str, object]]:
    items = _list(value, "gateway_inventory")
    expected = {
        (kind, name): ("task11", _TASK11_RUN_ID, _TASK11_REVISION)
        for kind, name in _GATEWAY_OBJECTS
    }
    if post:
        expected[("NetworkPolicy", "egress-gateway-allow-egress")] = (
            "task11a",
            run_id,
            revision,
        )
        expected[("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https")] = (
            "task11a",
            run_id,
            revision,
        )
    result: list[dict[str, object]] = []
    observed: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        identity = _mapping(item, f"gateway_inventory[{index}]")
        _exact_keys(
            identity,
            {"kind", "name", "namespace", "owner", "run_id", "revision"},
            f"gateway_inventory[{index}]",
        )
        key = (
            _string(identity["kind"], f"gateway_inventory[{index}].kind"),
            _string(identity["name"], f"gateway_inventory[{index}].name"),
        )
        if (
            key in observed
            or key not in expected
            or identity.get("namespace") != _GATEWAY_NAMESPACE
            or (
                identity.get("owner"),
                identity.get("run_id"),
                identity.get("revision"),
            )
            != expected[key]
        ):
            _refuse("gateway inventory identity or ownership drifted")
        observed.add(key)
        result.append(identity)
    if observed != set(expected):
        phase = "post" if post else "pre"
        _refuse(f"gateway {phase}-state is not the exact expected object set")
    return result


def _validate_runtime_expectations(
    value: object, image_reference: str
) -> list[dict[str, object]]:
    expected = runtime_expectations_from_files(
        _REPOSITORY / "Dockerfile",
        _REPOSITORY / _RENDER_PATHS["kubernetes-retrieval"],
        image_reference,
    )
    if value != expected:
        _refuse("runtime expectations differ from the committed Dockerfile or render")
    return expected


def validate_posture_preflight(
    *,
    revision: object,
    namespace: object,
    owner: object,
    run_id: object,
    protected_pvs: object,
    available_pvs: object,
    cluster_cidrs: object,
    cilium_crd_present: object,
    target_identity: object,
    storage_identity: object,
    render_identities: object,
    image_identity: object,
    accepted_task11_reports: object,
    credential_sources: object,
    tool_inventory: object,
    runtime_expectations: object,
    gateway_inventory: object,
) -> dict[str, object]:
    """Validate read-only target inventory before Task 11a can mutate it."""

    revision_text, namespace_text, owner_text, run_text = _validate_identifiers(
        revision=revision, namespace=namespace, owner=owner, run_id=run_id
    )
    if cilium_crd_present is not True:
        _refuse("CiliumNetworkPolicy CRD is absent")
    protected = [
        _pv_identity(value, f"protected_pvs[{index}]")
        for index, value in enumerate(_list(protected_pvs, "protected_pvs"))
    ]
    protected_names = {pv["name"] for pv in protected}
    protected_paths = {pv["path"] for pv in protected}
    if len(protected_names) != len(protected) or len(protected_paths) != len(protected):
        _refuse("protected PV identities are ambiguous")

    candidates: list[dict[str, object]] = []
    for index, value in enumerate(_list(available_pvs, "available_pvs")):
        identity = _pv_identity(value, f"available_pvs[{index}]")
        if identity["name"] in protected_names or identity["path"] in protected_paths:
            _refuse("available inventory includes a protected PV identity")
        candidate = _candidate_pv(value, f"available_pvs[{index}]")
        if candidate is not None:
            candidates.append(candidate)
    names = [pv["name"] for pv in candidates]
    paths = [pv["path"] for pv in candidates]
    if len(candidates) != 2 or len(set(names)) != 2 or len(set(paths)) != 2:
        _refuse(
            "exactly two unique unclaimed Available Retain reference PVs are required"
        )
    selected = sorted(candidates, key=lambda pv: str(pv["name"]))
    if [(pv["name"], pv["path"]) for pv in selected] != [
        ("cairn-local-pv3", "/mnt/cairn-local/pv3"),
        ("cairn-local-pv4", "/mnt/cairn-local/pv4"),
    ]:
        _refuse("selected PV identities do not match the Task 11a storage assignment")
    cidrs = _validate_cluster_cidrs(cluster_cidrs)
    validated_target = _validate_target_identity(target_identity)
    validated_storage = _validate_storage_identity(storage_identity)
    validated_renders = _validate_render_identities(render_identities)
    validated_image = _validate_image_identity(image_identity, revision_text)
    validated_reports = _validate_task11_reports(accepted_task11_reports)
    validated_credentials = _validate_credentials(credential_sources)
    validated_tools = _validate_tools(tool_inventory)
    validated_runtime = _validate_runtime_expectations(
        runtime_expectations,
        _string(validated_image["reference"], "image_identity.reference"),
    )
    validated_gateway = _gateway_inventory(
        gateway_inventory, revision=revision_text, run_id=run_text, post=False
    )
    return {
        "revision": revision_text,
        "namespace": namespace_text,
        "owner": owner_text,
        "run_id": run_text,
        "protected_pvs": sorted(protected, key=lambda pv: pv["name"]),
        "selected_pvs": [pv["name"] for pv in selected],
        "selected_pv_paths": [pv["path"] for pv in selected],
        "selected_pv_identities": selected,
        "cluster_cidrs": cidrs,
        "cilium_crd_present": True,
        "target_identity": validated_target,
        "storage_identity": validated_storage,
        "render_identities": validated_renders,
        "image_identity": validated_image,
        "accepted_task11_reports": validated_reports,
        "credential_sources": validated_credentials,
        "tool_inventory": validated_tools,
        "runtime_expectations": validated_runtime,
        "gateway_inventory": validated_gateway,
    }


def _validate_preflight_for_report(
    preflight: object, revision: str, run_id: str
) -> dict[str, object]:
    value = _mapping(preflight, "preflight")
    required = {
        "revision",
        "namespace",
        "owner",
        "run_id",
        "protected_pvs",
        "selected_pvs",
        "selected_pv_paths",
        "selected_pv_identities",
        "cluster_cidrs",
        "cilium_crd_present",
        "target_identity",
        "storage_identity",
        "render_identities",
        "image_identity",
        "accepted_task11_reports",
        "credential_sources",
        "tool_inventory",
        "runtime_expectations",
        "gateway_inventory",
    }
    if set(value) != required:
        _refuse("preflight has an unexpected schema")
    validated = validate_posture_preflight(
        revision=value["revision"],
        namespace=value["namespace"],
        owner=value["owner"],
        run_id=value["run_id"],
        protected_pvs=[
            {
                "metadata": {"name": item["name"]},
                "spec": {"local": {"path": item["path"]}},
            }
            for item in _list(value["protected_pvs"], "preflight.protected_pvs")
            if isinstance(item, dict)
        ],
        available_pvs=[
            {
                "metadata": {"name": item["name"]},
                "status": {"phase": "Available"},
                "spec": {
                    "accessModes": item["access_modes"],
                    "capacity": {"storage": item["capacity"]},
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": _STORAGE_CLASS,
                    "local": {"path": item["path"]},
                },
            }
            for item in _list(
                value["selected_pv_identities"], "preflight.selected_pv_identities"
            )
            if isinstance(item, dict)
        ],
        cluster_cidrs=value["cluster_cidrs"],
        cilium_crd_present=value["cilium_crd_present"],
        target_identity=value["target_identity"],
        storage_identity=value["storage_identity"],
        render_identities=value["render_identities"],
        image_identity=value["image_identity"],
        accepted_task11_reports=value["accepted_task11_reports"],
        credential_sources=value["credential_sources"],
        tool_inventory=value["tool_inventory"],
        runtime_expectations=value["runtime_expectations"],
        gateway_inventory=value["gateway_inventory"],
    )
    if (
        validated != value
        or revision != validated["revision"]
        or run_id != validated["run_id"]
    ):
        _refuse("preflight is not the bound validated result")
    return value


def _validate_checks(checks: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, value in enumerate(_list(checks, "checks")):
        check = _mapping(value, f"checks[{index}]")
        if set(check) != {"name", "expected", "observed", "outcome"}:
            _refuse(f"checks[{index}] has an unexpected schema")
        name = _string(check["name"], f"checks[{index}].name")
        expected = _mapping(check["expected"], f"checks[{index}].expected")
        observed = _mapping(check["observed"], f"checks[{index}].observed")
        if name in names:
            _refuse(f"duplicate check {name}")
        if check["outcome"] != "passed":
            _refuse(f"check {name} did not pass")
        expected_result = _EXPECTED_CHECK_RESULTS.get(name)
        if expected != {"result": expected_result}:
            _refuse(f"check {name} has the wrong expected result")
        if observed != expected:
            _refuse(f"check {name} is self-declared rather than observed")
        names.add(name)
        result.append(check)
    if names != set(REQUIRED_CHECKS):
        _refuse("checks must contain every required name exactly once")
    return result


def _validate_mount_observation(
    value: object, field: str, *, pre: bool, expected_gid: int
) -> dict[str, object]:
    mount = _mapping(value, field)
    expected_keys = {"uid", "gid", "mode"}
    if not pre:
        expected_keys.add("effective_group_writable")
    _exact_keys(mount, expected_keys, field)
    uid = _integer(mount["uid"], f"{field}.uid")
    gid = _integer(mount["gid"], f"{field}.gid")
    mode = _string(mount["mode"], f"{field}.mode")
    if re.fullmatch(r"0?[0-7]{3,4}", mode) is None:
        _refuse(f"{field}.mode is not canonical POSIX octal")
    parsed_mode = int(mode, 8)
    if pre:
        if (uid, gid, mode) != (0, 0, "0755"):
            _refuse("PV pre-mount state must be root:root 0755")
    elif (
        uid != 0
        or gid != expected_gid
        or parsed_mode & 0o020 == 0
        or mount.get("effective_group_writable") is not True
    ):
        _refuse("PV post-mount state lacks the exact effective group handoff")
    return mount


def _validate_pv_observations(
    value: object, preflight: dict[str, object]
) -> list[dict[str, object]]:
    items = _list(value, "pv_observations")
    expected = {
        "cairn": (
            "cairn-local-pv3",
            "/mnt/cairn-local/pv3",
            "/var/lib/cairn",
            65532,
        ),
        "falkordb": (
            "cairn-local-pv4",
            "/mnt/cairn-local/pv4",
            "/var/lib/falkordb/data",
            10001,
        ),
    }
    selected = {
        (item["name"], item["path"])
        for item in _mapping_list(
            preflight["selected_pv_identities"], "preflight.selected_pv_identities"
        )
    }
    result: list[dict[str, object]] = []
    workloads: set[str] = set()
    for index, item in enumerate(items):
        observation = _mapping(item, f"pv_observations[{index}]")
        _exact_keys(
            observation,
            {
                "workload",
                "pv_name",
                "host_path",
                "mount_path",
                "pre_mount",
                "post_mount",
            },
            f"pv_observations[{index}]",
        )
        workload = _string(
            observation["workload"], f"pv_observations[{index}].workload"
        )
        if workload in workloads or workload not in expected:
            _refuse("PV observations are missing, duplicate or unexpected")
        pv_name, host_path, mount_path, gid = expected[workload]
        if (
            observation.get("pv_name") != pv_name
            or observation.get("host_path") != host_path
            or observation.get("mount_path") != mount_path
            or (pv_name, host_path) not in selected
        ):
            _refuse("PV observation does not cross-bind the selected volume and mount")
        _validate_mount_observation(
            observation["pre_mount"],
            f"pv_observations[{index}].pre_mount",
            pre=True,
            expected_gid=0,
        )
        _validate_mount_observation(
            observation["post_mount"],
            f"pv_observations[{index}].post_mount",
            pre=False,
            expected_gid=gid,
        )
        workloads.add(workload)
        result.append(observation)
    if workloads != set(expected):
        _refuse("PV observations must contain exactly Cairn and FalkorDB")
    return result


def _validate_runtime_observations(
    value: object, preflight: dict[str, object]
) -> list[dict[str, object]]:
    items = _list(value, "runtime_observations")
    expectations = {
        item["workload"]: item
        for item in _mapping_list(
            preflight["runtime_expectations"], "preflight.runtime_expectations"
        )
    }
    image_identity = _mapping(preflight["image_identity"], "preflight.image_identity")
    result: list[dict[str, object]] = []
    workloads: set[str] = set()
    for index, item in enumerate(items):
        observation = _mapping(item, f"runtime_observations[{index}]")
        _exact_keys(
            observation,
            {
                "workload",
                "pod",
                "container",
                "admitted",
                "runtime",
                "spec_image",
                "runtime_image_id",
                "runtime_digest",
            },
            f"runtime_observations[{index}]",
        )
        workload = _string(
            observation["workload"], f"runtime_observations[{index}].workload"
        )
        if workload in workloads or workload not in expectations:
            _refuse("runtime observations are missing, duplicate or unexpected")
        expected = expectations[workload]
        admitted = _mapping(
            observation["admitted"], f"runtime_observations[{index}].admitted"
        )
        _exact_keys(
            admitted,
            {
                "run_as_non_root",
                "run_as_user",
                "run_as_group",
                "fs_group",
                "fs_group_change_policy",
            },
            f"runtime_observations[{index}].admitted",
        )
        expected_admitted = {
            "run_as_non_root": True,
            "run_as_user": None if workload == "cairn" else 10001,
            "run_as_group": None if workload == "cairn" else 0,
            "fs_group": expected["fs_group"],
            "fs_group_change_policy": "OnRootMismatch",
        }
        runtime = _mapping(
            observation["runtime"], f"runtime_observations[{index}].runtime"
        )
        _exact_keys(
            runtime,
            {"uid", "primary_gid", "supplementary_gids"},
            f"runtime_observations[{index}].runtime",
        )
        expected_runtime = {
            "uid": expected["uid"],
            "primary_gid": expected["primary_gid"],
            "supplementary_gids": expected["supplementary_gids"],
        }
        try:
            observed_image = canonical_image_reference(
                _string(
                    observation["spec_image"],
                    f"runtime_observations[{index}].spec_image",
                )
            )
        except HarnessRefusal as error:
            raise PostureRefusal("runtime observed image is malformed") from error
        if (
            observation.get("pod") != f"{workload}-0"
            or observation.get("container") != expected["container"]
            or admitted != expected_admitted
            or runtime != expected_runtime
            or observed_image != expected["image_reference"]
        ):
            _refuse("runtime identity does not match admitted and effective identity")
        runtime_id = _string(
            observation["runtime_image_id"],
            f"runtime_observations[{index}].runtime_image_id",
        )
        runtime_digest_value = _digest(
            observation["runtime_digest"],
            f"runtime_observations[{index}].runtime_digest",
        )
        try:
            if workload == "cairn":
                verified = runtime_config_digest(
                    runtime_id,
                    _string(
                        image_identity["expected_runtime_digest"],
                        "preflight.image_identity.expected_runtime_digest",
                    ),
                )
            else:
                verified = runtime_image_digest(runtime_id)
                expected_falkor = _string(
                    expected["image_reference"], "FalkorDB image reference"
                ).rsplit("@", 1)[-1]
                if verified != expected_falkor:
                    _refuse("FalkorDB runtime digest differs from its pinned image")
        except HarnessRefusal as error:
            raise PostureRefusal(f"runtime image identity refused: {error}") from error
        if verified != runtime_digest_value:
            _refuse("runtime digest does not match runtime image ID")
        workloads.add(workload)
        result.append(observation)
    if workloads != {"cairn", "falkordb"}:
        _refuse("runtime observations must contain exactly Cairn and FalkorDB")
    return result


def _validate_network_observations(
    value: object, preflight: dict[str, object]
) -> list[dict[str, object]]:
    items = _list(value, "network_observations")
    namespace = _string(preflight["namespace"], "preflight.namespace")
    cidrs = _mapping(preflight["cluster_cidrs"], "preflight.cluster_cidrs")
    try:
        pod_network = ipaddress.ip_network(
            _string(cidrs["pod"], "preflight.cluster_cidrs.pod"), strict=True
        )
        node_network = ipaddress.ip_network(
            _string(cidrs["node"], "preflight.cluster_cidrs.node"), strict=True
        )
    except ValueError as error:
        raise PostureRefusal("bound network CIDR is malformed") from error
    gateway_source = f"{_GATEWAY_NAMESPACE}/task11a-probe"
    control_source = f"{namespace}/task11a-control"
    cairn_source = f"{namespace}/cairn-0"
    expected: dict[str, tuple[str, str | None, int, str | None, str | None]] = {
        "dns": (gateway_source, "api.openai.com", 53, None, None),
        "external-https": (gateway_source, "api.openai.com", 443, None, None),
        "provider-delivery": (
            cairn_source,
            "api.openai.com-via-cairn-egress",
            443,
            None,
            None,
        ),
        "pod-https-denied": (gateway_source, None, 443, control_source, None),
        "service-https-denied": (
            gateway_source,
            "kubernetes.default.svc",
            443,
            control_source,
            "kubernetes.default.svc",
        ),
        "node-https-denied": (
            gateway_source,
            str(node_network.network_address),
            443,
            control_source,
            str(node_network.network_address),
        ),
        "squid-denied": (
            cairn_source,
            "blocked.example.invalid",
            443,
            cairn_source,
            "api.openai.com",
        ),
    }
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        observation = _mapping(item, f"network_observations[{index}]")
        _exact_keys(
            observation,
            {
                "name",
                "source",
                "destination",
                "port",
                "expected",
                "observed",
                "positive_control",
            },
            f"network_observations[{index}]",
        )
        name = _string(observation["name"], f"network_observations[{index}].name")
        if name in names or name not in expected:
            _refuse("network observations are missing, duplicate or unexpected")
        expected_result = _EXPECTED_CHECK_RESULTS[name]
        source = _string(
            observation.get("source"), f"network_observations[{index}].source"
        )
        destination = _string(
            observation.get("destination"),
            f"network_observations[{index}].destination",
        )
        (
            expected_source,
            expected_destination,
            expected_port,
            control_owner,
            control_destination,
        ) = expected[name]
        if name == "pod-https-denied":
            try:
                pod_address = ipaddress.ip_address(destination)
            except ValueError as error:
                raise PostureRefusal("Pod denial destination is malformed") from error
            if (
                not isinstance(pod_address, ipaddress.IPv4Address)
                or not isinstance(pod_network, ipaddress.IPv4Network)
                or pod_address not in pod_network
                or pod_address
                in {pod_network.network_address, pod_network.broadcast_address}
            ):
                _refuse("Pod denial destination is outside the bound Pod CIDR")
            expected_destination = destination
            control_destination = destination
        if (
            observation.get("expected") != expected_result
            or observation.get("observed") != expected_result
            or source != expected_source
            or destination != expected_destination
            or observation.get("port") != expected_port
        ):
            _refuse("network observation did not produce its fixed expected result")
        control = observation.get("positive_control")
        if name.endswith("-denied"):
            control_value = _mapping(
                control, f"network_observations[{index}].positive_control"
            )
            _exact_keys(
                control_value,
                {"source", "destination", "port", "expected", "observed"},
                f"network_observations[{index}].positive_control",
            )
            if (
                control_value.get("source") != control_owner
                or control_value.get("destination") != control_destination
                or control_value.get("port") != 443
                or control_value.get("expected") != "connected"
                or control_value.get("observed") != "connected"
            ):
                _refuse("denied network observation lacks its exact positive control")
        elif control is not None:
            _refuse("positive network observation must not invent a control")
        names.add(name)
        result.append(observation)
    if names != set(expected):
        _refuse("network observations must contain every named attempt exactly once")
    return result


def _validate_label_inventory(
    value: object, preflight: dict[str, object]
) -> list[dict[str, object]]:
    items = _list(value, "label_inventory")
    expected = {
        _BASELINE_NAMESPACE: (
            _BASELINE_NAMESPACE,
            "task11",
        ),
        str(preflight["namespace"]): (str(preflight["namespace"]), "task11a"),
    }
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        observation = _mapping(item, f"label_inventory[{index}]")
        _exact_keys(
            observation,
            {"namespace", "instance_label", "owner", "status"},
            f"label_inventory[{index}]",
        )
        namespace = _string(
            observation["namespace"], f"label_inventory[{index}].namespace"
        )
        if (
            namespace in names
            or namespace not in expected
            or (
                observation.get("instance_label"),
                observation.get("owner"),
            )
            != expected[namespace]
            or observation.get("status") != "administrative"
        ):
            _refuse("label inventory is incomplete or is presented as enforcement")
        names.add(namespace)
        result.append(observation)
    if names != set(expected):
        _refuse("label inventory must contain the exact Task 11 and Task 11a grants")
    return result


def _validate_gateway_observations(
    value: object, preflight: dict[str, object], revision: str, run_id: str
) -> dict[str, object]:
    observations = _mapping(value, "gateway_observations")
    _exact_keys(observations, {"pre", "post"}, "gateway_observations")
    pre = _gateway_inventory(
        observations["pre"], revision=revision, run_id=run_id, post=False
    )
    post = _gateway_inventory(
        observations["post"], revision=revision, run_id=run_id, post=True
    )
    if pre != preflight["gateway_inventory"]:
        _refuse("gateway pre-state does not match the bound preflight inventory")
    return {"pre": pre, "post": post}


def build_posture_report(
    *,
    revision: object,
    run_id: object,
    preflight: object,
    checks: object,
    pv_observations: object,
    runtime_observations: object,
    network_observations: object,
    label_inventory: object,
    gateway_observations: object,
) -> dict[str, object]:
    """Build a complete observed-pass report or refuse the evidence claim."""

    revision_text, _, _, run_text = _validate_identifiers(
        revision=revision,
        namespace=f"cairn-{run_id}",
        owner="task11a",
        run_id=run_id,
    )
    report_preflight = _validate_preflight_for_report(
        preflight, revision_text, run_text
    )
    report_checks = _validate_checks(checks)
    return {
        "status": "passed",
        "revision": revision_text,
        "run_id": run_text,
        "preflight": report_preflight,
        "checks": report_checks,
        "pv_observations": _validate_pv_observations(pv_observations, report_preflight),
        "runtime_observations": _validate_runtime_observations(
            runtime_observations, report_preflight
        ),
        "network_observations": _validate_network_observations(
            network_observations, report_preflight
        ),
        "label_inventory": _validate_label_inventory(label_inventory, report_preflight),
        "gateway_observations": _validate_gateway_observations(
            gateway_observations,
            report_preflight,
            revision_text,
            run_text,
        ),
    }


def _validate_cleanup(value: object, namespace: str) -> dict[str, object]:
    cleanup = _mapping(value, "cleanup")
    _exact_keys(cleanup, {"attempted", "results"}, "cleanup")
    if cleanup.get("attempted") is not True:
        _refuse("failed report must record attempted cleanup")
    expected = {
        ("Pod", namespace, "task11a-bootstrap"),
        ("Pod", _GATEWAY_NAMESPACE, "task11a-probe"),
        ("Pod", namespace, "task11a-control"),
        ("NetworkPolicy", namespace, "task11a-control-to-cairn"),
        ("Pod", namespace, "task11a-listener"),
        ("Service", namespace, "task11a-listener"),
    }
    observed: set[tuple[str, str, str]] = set()
    results: list[dict[str, object]] = []
    for index, item in enumerate(_list(cleanup["results"], "cleanup.results")):
        result = _mapping(item, f"cleanup.results[{index}]")
        _exact_keys(
            result,
            {"kind", "namespace", "name", "result"},
            f"cleanup.results[{index}]",
        )
        identity = (
            _string(result["kind"], f"cleanup.results[{index}].kind"),
            _string(result["namespace"], f"cleanup.results[{index}].namespace"),
            _string(result["name"], f"cleanup.results[{index}].name"),
        )
        if (
            identity in observed
            or identity not in expected
            or result.get("result") not in {"removed", "absent", "failed"}
        ):
            _refuse("cleanup result is missing, duplicate or unexpected")
        observed.add(identity)
        results.append(result)
    if observed != expected:
        _refuse("cleanup results must cover every temporary object")
    return {"attempted": True, "results": results}


def _validate_recovery_inventory(
    value: object, preflight: dict[str, object], revision: str, run_id: str
) -> dict[str, object]:
    del revision, run_id
    recovery = _mapping(value, "recovery_inventory")
    _exact_keys(
        recovery,
        {"namespace", "selected_pvs", "gateway_inventory"},
        "recovery_inventory",
    )
    namespace = _mapping(recovery["namespace"], "recovery_inventory.namespace")
    _exact_keys(
        namespace,
        {"query_succeeded", "name", "exists"},
        "recovery_inventory.namespace",
    )
    namespace_query_succeeded = namespace.get("query_succeeded")
    if (
        namespace.get("name") != preflight["namespace"]
        or type(namespace_query_succeeded) is not bool
        or (namespace_query_succeeded and type(namespace.get("exists")) is not bool)
        or (not namespace_query_succeeded and namespace.get("exists") is not None)
    ):
        _refuse("recovery namespace identity is malformed")
    selected_pvs = _mapping(recovery["selected_pvs"], "recovery_inventory.selected_pvs")
    _exact_keys(
        selected_pvs,
        {"query_succeeded", "items"},
        "recovery_inventory.selected_pvs",
    )
    pvs_query_succeeded = selected_pvs.get("query_succeeded")
    if type(pvs_query_succeeded) is not bool:
        _refuse("recovery PV query status is malformed")
    selected_items = _list(
        selected_pvs["items"], "recovery_inventory.selected_pvs.items"
    )
    if not pvs_query_succeeded and selected_items:
        _refuse("an unavailable recovery PV query cannot claim observations")
    expected_pv_names = {
        item["name"]
        for item in _mapping_list(
            preflight["selected_pv_identities"], "preflight.selected_pv_identities"
        )
    }
    selected: set[str] = set()
    pvs: list[dict[str, object]] = []
    for index, item in enumerate(selected_items):
        field = f"recovery_inventory.selected_pvs.items[{index}]"
        pv = _mapping(item, field)
        _exact_keys(
            pv,
            {"name", "path", "phase", "claim_namespace", "claim_name"},
            field,
        )
        name = _string(pv.get("name"), f"{field}.name")
        path = _canonical_absolute_path(
            _string(pv.get("path"), f"{field}.path"), f"{field}.path"
        )
        if (
            name in selected
            or name not in expected_pv_names
            or pv.get("phase") not in {"Available", "Bound", "Released", "Failed"}
            or not isinstance(pv.get("claim_namespace"), (str, type(None)))
            or not isinstance(pv.get("claim_name"), (str, type(None)))
        ):
            _refuse("recovery PV inventory is malformed")
        selected.add(name)
        pvs.append({**pv, "path": path})

    gateway_observation = _mapping(
        recovery["gateway_inventory"], "recovery_inventory.gateway_inventory"
    )
    _exact_keys(
        gateway_observation,
        {"query_succeeded", "items"},
        "recovery_inventory.gateway_inventory",
    )
    gateway_query_succeeded = gateway_observation.get("query_succeeded")
    if type(gateway_query_succeeded) is not bool:
        _refuse("recovery gateway query status is malformed")
    gateway_items = _list(
        gateway_observation["items"], "recovery_inventory.gateway_inventory.items"
    )
    if not gateway_query_succeeded and gateway_items:
        _refuse("an unavailable recovery gateway query cannot claim observations")
    gateway: list[dict[str, object]] = []
    observed_gateway: set[tuple[str, str]] = set()
    for index, item in enumerate(gateway_items):
        field = f"recovery_inventory.gateway_inventory.items[{index}]"
        identity = _mapping(item, field)
        _exact_keys(
            identity,
            {"kind", "name", "namespace", "owner", "run_id", "revision"},
            field,
        )
        key = (
            _string(identity["kind"], f"{field}.kind"),
            _string(identity["name"], f"{field}.name"),
        )
        if key in observed_gateway or identity.get("namespace") != _GATEWAY_NAMESPACE:
            _refuse("recovery gateway inventory is malformed")
        for label in ("owner", "run_id", "revision"):
            _string(identity[label], f"{field}.{label}")
        observed_gateway.add(key)
        gateway.append(identity)
    return {
        "namespace": namespace,
        "selected_pvs": {
            "query_succeeded": pvs_query_succeeded,
            "items": pvs,
        },
        "gateway_inventory": {
            "query_succeeded": gateway_query_succeeded,
            "items": gateway,
        },
    }


def build_failed_posture_report(
    *,
    revision: object,
    run_id: object,
    phase: object,
    preflight: object,
    cleanup: object,
    recovery_inventory: object,
) -> dict[str, object]:
    """Build immutable failure evidence without converting it into success."""

    revision_text, namespace, _, run_text = _validate_identifiers(
        revision=revision,
        namespace=f"cairn-{run_id}",
        owner="task11a",
        run_id=run_id,
    )
    phase_text = _string(phase, "phase")
    if phase_text not in _FAILURE_PHASES:
        _refuse("failed report phase is not a harness mutation phase")
    report_preflight = _validate_preflight_for_report(
        preflight, revision_text, run_text
    )
    return {
        "status": "failed",
        "revision": revision_text,
        "run_id": run_text,
        "phase": phase_text,
        "preflight": report_preflight,
        "cleanup": _validate_cleanup(cleanup, namespace),
        "recovery_inventory": _validate_recovery_inventory(
            recovery_inventory, report_preflight, revision_text, run_text
        ),
    }


def _screen_strings(value: object, path: str = "report") -> None:
    if isinstance(value, str):
        if SecretScreen().screen(path, value):
            _refuse(f"secret-shaped content in {path}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _refuse(f"{path} contains a non-string JSON object key")
            _screen_strings(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _screen_strings(item, f"{path}[{index}]")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _refuse(f"{path} contains a non-finite JSON number")
        return
    _refuse(f"{path} contains a non-JSON value")


def failed_report_path(success_path: Path) -> Path:
    if success_path.suffix != ".json" or success_path.name.endswith(".failed.json"):
        _refuse("success report path must end in .json and not .failed.json")
    return success_path.with_name(f"{success_path.stem}.failed.json")


def report_outcome_path(success_path: Path) -> Path:
    """Return the run-scoped atomic outcome claim adjacent to both reports."""

    failed_report_path(success_path)
    return success_path.with_name(f"{success_path.stem}.outcome")


def write_report_exclusive(path: Path, report: dict[str, object]) -> Path:
    """Atomically claim one run outcome before writing its immutable report."""

    if not isinstance(path, Path):
        _refuse("report path must be a Path")
    report_value = _mapping(report, "report")
    _screen_strings(report_value)
    status = report_value.get("status")
    if status not in {"passed", "failed"}:
        _refuse("report status must be passed or failed")
    failure_path = failed_report_path(path)
    outcome_path = report_outcome_path(path)
    if path.exists() or failure_path.exists():
        raise FileExistsError("success or failed report already exists for this run")
    output_path = path if status == "passed" else failure_path
    try:
        payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as error:
        raise PostureRefusal("report is not JSON-serialisable") from error
    output_path.parent.mkdir(parents=True, exist_ok=True)
    claim = json.dumps({"status": status}, sort_keys=True) + "\n"
    descriptor = os.open(outcome_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(claim)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return output_path


def _stdin_object() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise ValueError("stdin must contain one JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("stdin must contain one JSON object")
    return value


def _emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) == 1 and arguments[0] == "validate-preflight":
            _emit(validate_posture_preflight(**_stdin_object()))
            return 0
        if len(arguments) == 1 and arguments[0] == "build-report":
            _emit(build_posture_report(**_stdin_object()))
            return 0
        if len(arguments) == 1 and arguments[0] == "build-failed-report":
            _emit(build_failed_posture_report(**_stdin_object()))
            return 0
        if len(arguments) == 4 and arguments[0] == "runtime-expectations":
            _emit(
                {
                    "runtime_expectations": runtime_expectations_from_files(
                        Path(arguments[1]), Path(arguments[2]), arguments[3]
                    )
                }
            )
            return 0
        if len(arguments) == 2 and arguments[0] == "write-report":
            path = Path(arguments[1])
            written = write_report_exclusive(path, _stdin_object())
            _emit({"path": str(written)})
            return 0
        raise ValueError(
            "usage: reference_posture_state.py "
            "{validate-preflight|build-report|build-failed-report|"
            "runtime-expectations DOCKERFILE RENDER IMAGE|write-report PATH}"
        )
    except PostureRefusal as error:
        print(error, file=sys.stderr)
        return 1
    except (FileExistsError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
