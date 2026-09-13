"""Pure safety and evidence rules for the Task 11a reference posture run."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
STATE = REPOSITORY / "scripts" / "reference_posture_state.py"
HARNESS = REPOSITORY / "scripts" / "reference-posture-acceptance"
LAUNCHER = REPOSITORY / "scripts" / "reference-posture-launch"
UV_FETCHER = REPOSITORY / "scripts" / "fetch-uv"
TASK11A_RUNBOOK = (
    REPOSITORY / "docs" / "runbooks" / "runbook-reference-task11a-posture.md"
)
KUBERNETES_RECOVERY_RUNBOOK = (
    REPOSITORY / "docs" / "runbooks" / "runbook-kubernetes-recovery.md"
)
_FAKE_HARNESS_ROOT: Path | None = None

# This literal fixture deliberately does not derive itself from the helper.
# Removing a required live observation must make this suite fail.
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

EXPECTED_CHECK_RESULTS = {
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

REVISION = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = "task11a-0123456-01"
NAMESPACE = f"cairn-{RUN_ID}"
TASK11_REVISION = "0de93bd1492beda56143bc45b155fccf7cd47caf"
TASK11_RUN_ID = "task11-0de93bd1492b-01"
TASK11_PRIMARY_SHA256 = (
    "e8b489f8b67e038cc00dc4b2cc807f4a16ba21457d1a668b9e36c33d59361606"
)
TASK11_CILIUM_SHA256 = (
    "eb5c9251fc8e994b030cf7fc8c5da2aa7e3f5224c8f44390fb046c8270480da0"
)
FAKE_CAIRN_CONFIG = b'{"architecture":"amd64","os":"linux"}'
CAIRN_CONFIG_DIGEST = "sha256:" + hashlib.sha256(FAKE_CAIRN_CONFIG).hexdigest()
FALKORDB_DIGEST = (
    "sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62"
)
RETRIEVAL_RENDER_SHA256 = (
    "a7b89311eba09e14efa403aad933c8084cc14e523e2364d1a7f4507619408602"
)
GATEWAY_RENDER_SHA256 = (
    "e08d3c0bbe343e1c662b75f6cb10aea43fa1475059bf9a7a39ce89d662afe8f1"
)

GATEWAY_PRE_INVENTORY = [
    {
        "kind": kind,
        "name": name,
        "namespace": "cairn-egress",
        "owner": "task11",
        "run_id": TASK11_RUN_ID,
        "revision": TASK11_REVISION,
    }
    for kind, name in (
        ("ConfigMap", "egress-gateway-config"),
        ("Service", "cairn-egress-gateway"),
        ("Deployment", "cairn-egress-gateway"),
        ("NetworkPolicy", "egress-gateway-allow-cairn-ingress"),
        ("NetworkPolicy", "egress-gateway-allow-egress"),
        ("NetworkPolicy", "egress-gateway-default-deny"),
    )
]
GATEWAY_POST_INVENTORY = [
    *[
        {
            **item,
            **(
                {"owner": "task11a", "run_id": RUN_ID, "revision": REVISION}
                if (item["kind"], item["name"])
                == ("NetworkPolicy", "egress-gateway-allow-egress")
                else {}
            ),
        }
        for item in GATEWAY_PRE_INVENTORY
    ],
    {
        "kind": "CiliumNetworkPolicy",
        "name": "egress-gateway-deny-cluster-https",
        "namespace": "cairn-egress",
        "owner": "task11a",
        "run_id": RUN_ID,
        "revision": REVISION,
    },
]

TEMPORARY_CLEANUP_HELPERS = (
    "bootstrap Pod",
    "probe Pod",
    "control Pod",
    "control NetworkPolicy",
    "listener Pod",
    "listener Service",
)

EXPECTED_DURABLE_STEPS = (
    "gateway-prestate-recheck",
    "gateway-apply",
    "gateway-poststate",
    "namespace-apply",
    "secret-apply",
    "instance-apply",
    "bootstrap-scale-down",
    "bootstrap-cairn-stop",
    "bootstrap-pod-apply",
    "bootstrap-pod-ready",
    "bootstrap-catalogue-migrate",
    "bootstrap-command-exec",
    "bootstrap-pod-delete",
    "bootstrap-scale-up",
    "bootstrap-cairn-rollout",
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


def _runbook_shell_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line == "```bash":
            assert current is None
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current) + "\n")
            current = None
        elif current is not None:
            current.append(line)
    assert current is None
    return blocks


def _initialise_git_repository(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "source").write_text("reviewed\n", encoding="utf-8")
    subprocess.run(["git", "add", "source"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Val",
            "-c",
            "user.email=reviewer@example.invalid",
            "commit",
            "-q",
            "-m",
            "reviewed",
        ],
        cwd=path,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_retrieval_comment_and_committed_render_digest_are_current() -> None:
    source = (
        REPOSITORY / "deploy/kustomize/overlays/kubernetes-retrieval/configmap.yaml"
    ).read_text(encoding="utf-8")
    render_path = REPOSITORY / "deploy/kustomize/rendered/kubernetes-retrieval.yaml"
    rendered = render_path.read_text(encoding="utf-8")
    digest_record = render_path.with_suffix(".yaml.sha256").read_text(encoding="utf-8")

    assert "With graphiti disabled nothing reads it" not in source
    assert "Retrieval reads its provider and index credentials here" in source
    assert "Retrieval reads its provider and index credentials here" in rendered
    assert digest_record == (
        f"{hashlib.sha256(render_path.read_bytes()).hexdigest()}  "
        "kubernetes-retrieval.yaml\n"
    )


def _load_state() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_posture_state", STATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state() -> ModuleType:
    return _load_state()


@pytest.fixture
def protected_pvs() -> list[dict[str, object]]:
    return [
        {
            "metadata": {"name": "cairn-local-pv1"},
            "spec": {
                "local": {"path": "/var/lib/cairn/acceptance/cairn"},
                "claimRef": {
                    "namespace": "cairn-target-acceptance",
                    "name": "cairn-data-cairn-0",
                },
            },
        },
        {
            "metadata": {"name": "cairn-local-pv2"},
            "spec": {
                "local": {"path": "/var/lib/cairn/acceptance/falkordb"},
                "claimRef": {
                    "namespace": "cairn-target-acceptance",
                    "name": "falkordb-data-falkordb-0",
                },
            },
        },
    ]


@pytest.fixture
def available_pvs() -> list[dict[str, object]]:
    return [
        {
            "metadata": {"name": "cairn-local-pv3"},
            "status": {"phase": "Available"},
            "spec": {
                "accessModes": ["ReadWriteOncePod"],
                "capacity": {"storage": "10Gi"},
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "cairn-local",
                "local": {"path": "/mnt/cairn-local/pv3"},
            },
        },
        {
            "metadata": {"name": "cairn-local-pv4"},
            "status": {"phase": "Available"},
            "spec": {
                "accessModes": ["ReadWriteOncePod"],
                "capacity": {"storage": "10Gi"},
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "cairn-local",
                "local": {"path": "/mnt/cairn-local/pv4"},
            },
        },
    ]


def _cilium_images() -> list[dict[str, object]]:
    identities = (
        (
            "DaemonSet",
            "cilium",
            "cilium-agent",
            "quay.io/cilium/cilium:v1.19.6@sha256:"
            "0df5b2750b64c49843aba1d649e9eaf61467cb0645ad3171db6f6962c095ac92",
            "cilium-abc",
        ),
        (
            "Deployment",
            "cilium-operator",
            "cilium-operator",
            "quay.io/cilium/operator-generic:v1.19.6@sha256:"
            "0db4ca4e06969d8904ee036617795d0e9c3228cf7b8d902ba74fc2bb98d2d665",
            "cilium-operator-abcde-x",
        ),
        (
            "DaemonSet",
            "cilium-envoy",
            "cilium-envoy",
            "quay.io/cilium/cilium-envoy:v1.36.9-1782267392-edeb3f2af56c37c407efa1f63f0b32f595399bbc@sha256:"
            "767101fb8a5e38f055778cb43b7aa8eed80450b37f8121effac3d9de9e06dc99",
            "cilium-envoy-abc",
        ),
    )
    result: list[dict[str, object]] = []
    for kind, name, container, image, pod in identities:
        digest = image.rsplit("@", 1)[1]
        chain = [{"kind": kind, "name": name, "uid": f"{name}-uid"}]
        if kind == "Deployment":
            chain.append(
                {
                    "kind": "ReplicaSet",
                    "name": "cilium-operator-abcde",
                    "uid": "operator-rs-uid",
                }
            )
        chain.append({"kind": "Pod", "name": pod, "uid": f"{pod}-uid"})
        result.append(
            {
                "workload_kind": kind,
                "workload_name": name,
                "workload_uid": f"{name}-uid",
                "namespace": "kube-system",
                "container_name": container,
                "spec_image": image,
                "runtime_image_id": f"containerd://{digest}",
                "runtime_image_digest": digest,
                "pod_names": [pod],
                "pod_uids": [f"{pod}-uid"],
                "desired_pods": 1,
                "selector_labels": {"app": name},
                "controller_chain": chain,
            }
        )
    return result


def _runtime_expectations() -> list[dict[str, object]]:
    return [
        {
            "workload": "cairn",
            "container": "cairn",
            "mount_path": "/var/lib/cairn",
            "uid": 65532,
            "primary_gid": 0,
            "supplementary_gids": [65532],
            "fs_group": 65532,
            "image_reference": "docker.io/library/cairn:task11a",
        },
        {
            "workload": "falkordb",
            "container": "falkordb",
            "mount_path": "/var/lib/falkordb/data",
            "uid": 10001,
            "primary_gid": 0,
            "supplementary_gids": [10001],
            "fs_group": 10001,
            "image_reference": (
                f"docker.io/falkordb/falkordb:v4.20.4@{FALKORDB_DIGEST}"
            ),
        },
    ]


def _preflight_identities() -> dict[str, object]:
    return {
        "target_identity": {
            "host": {
                "hostname": "reference",
                "distribution": "Fedora Linux 44",
                "selinux": "Enforcing",
            },
            "kubernetes": {
                "server_version": "v1.35.0",
                "node": "reference",
                "node_ready": True,
                "container_runtime": "containerd://2.3.3",
            },
            "cilium": {"chart": "cilium-1.19.6", "images": _cilium_images()},
        },
        "storage_identity": {
            "storage_class": "cairn-local",
            "provisioner": "kubernetes.io/no-provisioner",
            "volume_binding_mode": "WaitForFirstConsumer",
        },
        "render_identities": [
            {
                "name": "kubernetes-retrieval",
                "path": "deploy/kustomize/rendered/kubernetes-retrieval.yaml",
                "sha256": RETRIEVAL_RENDER_SHA256,
            },
            {
                "name": "egress-gateway-reference",
                "path": "deploy/kustomize/rendered/egress-gateway-reference.yaml",
                "sha256": GATEWAY_RENDER_SHA256,
            },
        ],
        "image_identity": {
            "reference": "docker.io/library/cairn:task11a",
            "source_revision": REVISION,
            "local_image_id": "sha256:" + "5" * 64,
            "target_digest": "sha256:" + "6" * 64,
            "platform_manifest_digest": "sha256:" + "7" * 64,
            "config_digest": CAIRN_CONFIG_DIGEST,
            "expected_runtime_digest": CAIRN_CONFIG_DIGEST,
        },
        "accepted_task11_reports": [
            {
                "name": "primary",
                "path": "/evidence/task11-primary.json",
                "sha256": TASK11_PRIMARY_SHA256,
            },
            {
                "name": "cilium-supplement",
                "path": "/evidence/task11-cilium.json",
                "sha256": TASK11_CILIUM_SHA256,
            },
        ],
        "credential_sources": [
            {
                "name": "openai-api-key",
                "path": "/secrets/openai-api-key",
                "mode": "600",
                "owner": "root",
                "group": "root",
                "non_empty": True,
            },
            {
                "name": "falkordb-password",
                "path": "/secrets/falkordb-password",
                "mode": "600",
                "owner": "root",
                "group": "root",
                "non_empty": True,
            },
        ],
        "tool_inventory": [
            {"name": name, "path": f"/usr/bin/{name}"}
            for name in (
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
        ],
        "runtime_expectations": _runtime_expectations(),
        "gateway_inventory": copy.deepcopy(GATEWAY_PRE_INVENTORY),
    }


@pytest.fixture
def preflight_args(
    protected_pvs: list[dict[str, object]], available_pvs: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "revision": REVISION,
        "namespace": NAMESPACE,
        "owner": "task11a",
        "run_id": RUN_ID,
        "protected_pvs": protected_pvs,
        "available_pvs": available_pvs,
        "cluster_cidrs": {
            "pod": "10.244.0.0/16",
            "service": "10.96.0.0/12",
            "node": "192.168.50.50/32",
        },
        "cilium_crd_present": True,
        **_preflight_identities(),
    }


def _preflight(state: ModuleType, args: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], state.validate_posture_preflight(**args))


def _checks() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "outcome": "passed",
            "expected": {"result": EXPECTED_CHECK_RESULTS[name]},
            "observed": {"result": EXPECTED_CHECK_RESULTS[name]},
        }
        for name in REQUIRED_CHECKS
    ]


def _pv_observations() -> list[dict[str, object]]:
    return [
        {
            "workload": "cairn",
            "pv_name": "cairn-local-pv3",
            "host_path": "/mnt/cairn-local/pv3",
            "mount_path": "/var/lib/cairn",
            "pre_mount": {"uid": 0, "gid": 0, "mode": "0755"},
            "post_mount": {
                "uid": 0,
                "gid": 65532,
                "mode": "2770",
                "effective_group_writable": True,
            },
        },
        {
            "workload": "falkordb",
            "pv_name": "cairn-local-pv4",
            "host_path": "/mnt/cairn-local/pv4",
            "mount_path": "/var/lib/falkordb/data",
            "pre_mount": {"uid": 0, "gid": 0, "mode": "0755"},
            "post_mount": {
                "uid": 0,
                "gid": 10001,
                "mode": "2770",
                "effective_group_writable": True,
            },
        },
    ]


def _runtime_observations() -> list[dict[str, object]]:
    return [
        {
            "workload": "cairn",
            "pod": "cairn-0",
            "container": "cairn",
            "admitted": {
                "run_as_non_root": True,
                "run_as_user": None,
                "run_as_group": None,
                "fs_group": 65532,
                "fs_group_change_policy": "OnRootMismatch",
            },
            "runtime": {
                "uid": 65532,
                "primary_gid": 0,
                "supplementary_gids": [65532],
            },
            "spec_image": "docker.io/library/cairn:task11a",
            "runtime_image_id": f"containerd://{CAIRN_CONFIG_DIGEST}",
            "runtime_digest": CAIRN_CONFIG_DIGEST,
        },
        {
            "workload": "falkordb",
            "pod": "falkordb-0",
            "container": "falkordb",
            "admitted": {
                "run_as_non_root": True,
                "run_as_user": 10001,
                "run_as_group": 0,
                "fs_group": 10001,
                "fs_group_change_policy": "OnRootMismatch",
            },
            "runtime": {
                "uid": 10001,
                "primary_gid": 0,
                "supplementary_gids": [10001],
            },
            "spec_image": (f"docker.io/falkordb/falkordb:v4.20.4@{FALKORDB_DIGEST}"),
            "runtime_image_id": f"containerd://{FALKORDB_DIGEST}",
            "runtime_digest": FALKORDB_DIGEST,
        },
    ]


def _network_observations() -> list[dict[str, object]]:
    positive: dict[str, tuple[str, str, int]] = {
        "dns": ("cairn-egress/task11a-probe", "api.openai.com", 53),
        "external-https": (
            "cairn-egress/task11a-probe",
            "api.openai.com",
            443,
        ),
        "provider-delivery": (
            f"{NAMESPACE}/cairn-0",
            "api.openai.com-via-cairn-egress",
            443,
        ),
    }
    result: list[dict[str, object]] = []
    for name, (source, destination, port) in positive.items():
        observed = EXPECTED_CHECK_RESULTS[name]
        result.append(
            {
                "name": name,
                "source": source,
                "destination": destination,
                "port": port,
                "expected": observed,
                "observed": observed,
                "positive_control": None,
            }
        )
    negative = {
        "pod-https-denied": "10.244.1.20",
        "service-https-denied": "kubernetes.default.svc",
        "node-https-denied": "192.168.50.50",
        "squid-denied": "blocked.example.invalid",
    }
    for name, destination in negative.items():
        source = (
            f"{NAMESPACE}/cairn-0"
            if name == "squid-denied"
            else "cairn-egress/task11a-probe"
        )
        control_source = (
            f"{NAMESPACE}/cairn-0"
            if name == "squid-denied"
            else f"{NAMESPACE}/task11a-control"
        )
        result.append(
            {
                "name": name,
                "source": source,
                "destination": destination,
                "port": 443,
                "expected": "blocked",
                "observed": "blocked",
                "positive_control": {
                    "source": control_source,
                    "destination": (
                        "api.openai.com" if name == "squid-denied" else destination
                    ),
                    "port": 443,
                    "expected": "connected",
                    "observed": "connected",
                },
            }
        )
    return result


def _label_inventory() -> list[dict[str, str]]:
    return [
        {
            "namespace": "cairn-target-acceptance",
            "instance_label": "cairn-target-acceptance",
            "owner": "task11",
            "status": "administrative",
        },
        {
            "namespace": NAMESPACE,
            "instance_label": NAMESPACE,
            "owner": "task11a",
            "status": "administrative",
        },
    ]


def _report_args(preflight: dict[str, object]) -> dict[str, object]:
    return {
        "revision": REVISION,
        "run_id": RUN_ID,
        "preflight": preflight,
        "checks": _checks(),
        "pv_observations": _pv_observations(),
        "runtime_observations": _runtime_observations(),
        "network_observations": _network_observations(),
        "label_inventory": _label_inventory(),
        "gateway_observations": {
            "pre": copy.deepcopy(GATEWAY_PRE_INVENTORY),
            "post": copy.deepcopy(GATEWAY_POST_INVENTORY),
        },
    }


def _report(state: ModuleType, preflight: dict[str, object]) -> dict[str, object]:
    return cast(
        dict[str, object], state.build_posture_report(**_report_args(preflight))
    )


@contextmanager
def _fake_harness_environment(tmp_path: Path) -> Iterator[None]:
    global _FAKE_HARNESS_ROOT
    previous = _FAKE_HARNESS_ROOT
    _FAKE_HARNESS_ROOT = tmp_path
    try:
        yield
    finally:
        _FAKE_HARNESS_ROOT = previous


def _fake_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _install_fake_commands(root: Path) -> None:
    binary = root / "bin"
    binary.mkdir()
    # Expose only the real utilities this harness needs. Inheriting PATH lets a
    # disabled fake command fall through to an installed host tool.
    for command in (
        "bash",
        "cat",
        "dirname",
        "grep",
        "jq",
        "mkdir",
        "mktemp",
        "rm",
        "sed",
        "seq",
        "setsid",
        "sleep",
        "sort",
        "tail",
        "timeout",
        "wc",
    ):
        executable = shutil.which(command)
        assert executable is not None, f"test harness requires {command}"
        (binary / command).symlink_to(executable)
    _fake_executable(
        binary / "python3",
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
    )
    oci = root / "oci"
    oci.mkdir()
    config = FAKE_CAIRN_CONFIG
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    (oci / config_digest.removeprefix("sha256:")).write_bytes(config)
    (oci / manifest_digest.removeprefix("sha256:")).write_bytes(manifest)
    (root / "oci-target-digest").write_text(manifest_digest, encoding="utf-8")
    _fake_executable(
        binary / "id",
        '#!/bin/sh\n[ "${1:-}" = -u ] && printf \'0\\n\' || exec /usr/bin/id "$@"\n',
    )
    _fake_executable(
        binary / "awk",
        "#!/bin/sh\n"
        'for argument do [ "$argument" = /etc/os-release ] && '
        "{ printf 'Fedora Linux 44\\n'; exit 0; }; done\n"
        'exec /usr/bin/awk "$@"\n',
    )
    _fake_executable(binary / "hostname", "#!/bin/sh\nprintf 'reference\\n'\n")
    _fake_executable(
        binary / "getenforce",
        "#!/bin/sh\n"
        'if [ "${FAKE_KUBE_FIXTURE:-}" = selinux-drift ]; then '
        "printf 'Permissive\\n'; else printf 'Enforcing\\n'; fi\n",
    )
    _fake_executable(
        binary / "ip",
        "#!/bin/sh\n"
        "[ \"$*\" = '-j -4 address show' ] || exit 1\n"
        "printf '%s\\n' "
        '\'[{"ifname":"eth0","addr_info":[{"family":"inet","local":"192.168.50.50"}]}]\'\n',
    )
    _fake_executable(
        binary / "firewall-cmd",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["FAKE_KUBE_ROOT"])
with (root / "firewall.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
marker = root / "firewall-rule-active"
if args == ["--state"]:
    print("not running" if os.environ["FAKE_KUBE_FIXTURE"] == "firewalld-inactive" else "running")
elif args == ["--get-zone-of-interface=eth0"]:
    print("no zone" if os.environ["FAKE_KUBE_FIXTURE"] == "firewalld-no-zone" else "public")
elif any(argument.startswith("--query-rich-rule=") for argument in args):
    raise SystemExit(
        0
        if marker.exists()
        or os.environ["FAKE_KUBE_FIXTURE"] == "firewalld-rule-preexisting"
        else 1
    )
elif any(argument.startswith("--add-rich-rule=") for argument in args):
    if marker.exists():
        raise SystemExit(1)
    marker.write_text("active", encoding="utf-8")
elif any(argument.startswith("--remove-rich-rule=") for argument in args):
    if not marker.exists():
        raise SystemExit(1)
    marker.unlink()
else:
    raise SystemExit(1)
""",
    )
    _fake_executable(
        binary / "stat",
        "#!/bin/sh\n"
        'printf "stat %s\\n" "$*" >>"$FAKE_KUBE_ROOT/fs.log"\n'
        'case "$*" in *"%d:%i"*) case "$*" in '
        "*acceptance/cairn*) printf '1:11\\n' ;; *acceptance/falkordb*) printf '1:12\\n' ;; "
        '*cairn-local/pv3*) if [ "${FAKE_KUBE_FIXTURE:-}" = inode-alias ]; then '
        "printf '1:11\\n'; else printf '1:13\\n'; fi ;; *) printf '1:14\\n' ;; esac ;; "
        '*openai-api-key*|*falkordb-password*|*client-token*) printf "600 root:root\\n" ;; '
        "*) printf '755 root:root\\n' ;; esac\n",
    )
    _fake_executable(
        binary / "realpath",
        "#!/bin/sh\n"
        "for path do :; done\n"
        'printf "realpath %s\\n" "$path" >>"$FAKE_KUBE_ROOT/fs.log"\n'
        'if [ "${FAKE_KUBE_FIXTURE:-}" = path-alias ] && '
        '[ "$path" = /mnt/cairn-local/pv3 ]; then '
        "printf '/var/lib/cairn/acceptance/cairn\\n'; else printf '%s\\n' \"$path\"; fi\n",
    )
    _fake_executable(
        binary / "sha256sum",
        "#!/bin/sh\n"
        'if [ "${1:-}" = -c ]; then exit 0; fi\n'
        'case "${1:-}" in\n'
        "*task11-primary.json) value=e8b489f8b67e038cc00dc4b2cc807f4a16ba21457d1a668b9e36c33d59361606 ;;\n"
        '*task11-cilium.json) if [ "${FAKE_KUBE_FIXTURE:-}" = stale-task11-report ]; then '
        "value=0000000000000000000000000000000000000000000000000000000000000000; "
        "else value=eb5c9251fc8e994b030cf7fc8c5da2aa7e3f5224c8f44390fb046c8270480da0; fi ;;\n"
        "*kubernetes-retrieval.yaml) value=a7b89311eba09e14efa403aad933c8084cc14e523e2364d1a7f4507619408602 ;;\n"
        "*egress-gateway-reference.yaml) value=e08d3c0bbe343e1c662b75f6cb10aea43fa1475059bf9a7a39ce89d662afe8f1 ;;\n"
        "*) value=a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989 ;;\n"
        "esac\n"
        'printf \'%s  %s\\n\' "$value" "${1:--}"\n',
    )
    _fake_executable(
        binary / "socat",
        "#!/bin/sh\n"
        'printf "started %s\\n" "$$" >>"$FAKE_KUBE_ROOT/socat.log"\n'
        "stopping=0\n"
        'trap \'[ "$stopping" = 1 ] && exit 0; stopping=1; '
        'printf "stopped\\\\n" >>"$FAKE_KUBE_ROOT/socat.log"; exit 0\' TERM INT\n'
        "while :; do /usr/bin/sleep 1; done\n",
    )
    _fake_executable(
        binary / "ss",
        "#!/bin/sh\n"
        'if [ "${FAKE_KUBE_FIXTURE:-}" = node-port-in-use ]; then '
        "printf 'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*\\n'; fi\n",
    )
    _fake_executable(
        binary / "docker",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '*org.opencontainers.image.revision*) if [ "${FAKE_KUBE_FIXTURE:-}" = stale-image-revision ]; then '
        "printf '0de93bd1492beda56143bc45b155fccf7cd47caf\\n'; else "
        "printf '0123456789abcdef0123456789abcdef01234567\\n'; fi ;;\n"
        "*--format*Id*) printf 'sha256:5555555555555555555555555555555555555555555555555555555555555555\\n' ;;\n"
        "*) exit 0 ;;\n"
        "esac\n",
    )
    _fake_executable(
        binary / "ctr",
        r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["FAKE_KUBE_ROOT"])
target = (root / "oci-target-digest").read_text()
source_reference = os.environ["IMAGE"]
reference = (
    source_reference
    if "/" in source_reference
    else "docker.io/library/" + source_reference
)
if args[-3:] == ["images", "list", "-q"]:
    print(reference)
elif len(args) >= 3 and args[-3:-1] == ["images", "list"]:
    print("REF TYPE DIGEST SIZE PLATFORMS LABELS")
    print(f"{reference} application/vnd.oci.image.manifest.v1+json {target} 1B linux/amd64 -")
elif len(args) >= 3 and args[-3:-1] == ["content", "get"]:
    digest = args[-1].removeprefix("sha256:")
    sys.stdout.buffer.write((root / "oci" / digest).read_bytes())
else:
    raise SystemExit(1)
""",
    )
    _fake_executable(
        binary / "git",
        """#!/bin/sh
case "$1 $2" in
  "rev-parse HEAD") printf '0123456789abcdef0123456789abcdef01234567\\n' ;;
  "diff --quiet"|"diff --cached") exit 0 ;;
  "status --porcelain=v1") exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _fake_executable(
        binary / "helm",
        '#!/bin/sh\nprintf \'%s\\n\' \'[{"name":"cilium","chart":"cilium-1.19.6"}]\'\n',
    )
    _fake_executable(
        binary / "kubectl",
        r"""#!/usr/bin/env python3
import base64
import json
import os
import re
import socket
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["FAKE_KUBE_ROOT"])
with (root / "kubectl.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
def applied_objects(payload):
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        values = []
        for document in re.split(r"^---\s*$", payload, flags=re.MULTILINE):
            kind = re.search(r"^kind:\s*(\S+)", document, re.MULTILINE)
            metadata = re.search(r"^metadata:\s*$([\s\S]*?)(?=^\S|\Z)", document, re.MULTILINE)
            if not kind or not metadata:
                continue
            block = metadata.group(1)
            name = re.search(r"^\s+name:\s*(\S+)", block, re.MULTILINE)
            namespace = re.search(r"^\s+namespace:\s*(\S+)", block, re.MULTILINE)
            if name:
                values.append({"kind": kind.group(1), "name": name.group(1),
                               "namespace": namespace.group(1) if namespace else "default"})
        return values
    items = value.get("items", []) if isinstance(value, dict) and value.get("kind") == "List" else [value]
    return [{"kind": item.get("kind"), "name": item.get("metadata", {}).get("name"),
             "namespace": item.get("metadata", {}).get("namespace", "default")}
            for item in items if isinstance(item, dict)]

if args[:1] == ["apply"] and "-f" in args:
    source = args[args.index("-f") + 1]
    payload = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    with (root / "applied-payloads.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"payload": payload}) + "\n")
    objects = applied_objects(payload)
    with (root / "applied.jsonl").open("a", encoding="utf-8") as stream:
        for item in objects:
            stream.write(json.dumps(item, sort_keys=True) + "\n")
    gateway_apply = any(
        item["name"] == "egress-gateway-deny-cluster-https" for item in objects
    )
    fixture = os.environ["FAKE_KUBE_FIXTURE"]
    if gateway_apply:
        marker = (
            "gateway-partial"
            if fixture == "gateway-partial-failure"
            else "gateway-applied"
        )
        (root / marker).write_text("true", encoding="utf-8")
        if fixture in {"gateway-partial-failure", "gateway-recovery-query-failure"}:
            (root / "failure-triggered").write_text("true", encoding="utf-8")
            raise SystemExit(1)
    if any(item["kind"] == "Namespace" and item["name"] == "cairn-task11a-0123456-01" for item in objects):
        (root / "namespace-applied").write_text("true", encoding="utf-8")
    if fixture == "bootstrap-apply-failure" and any(item["name"] == "task11a-bootstrap" for item in objects):
        raise SystemExit(1)
    if fixture == "probe-apply-failure" and any(item["name"] == "task11a-control" for item in objects):
        raise SystemExit(1)

fixture = os.environ["FAKE_KUBE_FIXTURE"]
protected = [
    {
        "metadata": {"name": "cairn-local-pv1"},
        "status": {"phase": "Bound"},
        "spec": {
            "storageClassName": "cairn-local",
            "persistentVolumeReclaimPolicy": "Retain",
            "local": {"path": "/var/lib/cairn/acceptance/cairn"},
            "claimRef": {
                "namespace": "cairn-target-acceptance",
                "name": "data-cairn-0", "uid": "pvc-cairn-uid",
            },
        },
    },
    {
        "metadata": {"name": "cairn-local-pv2"},
        "status": {"phase": "Bound"},
        "spec": {
            "storageClassName": "cairn-local",
            "persistentVolumeReclaimPolicy": "Retain",
            "local": {"path": "/var/lib/cairn/acceptance/falkordb"},
            "claimRef": {
                "namespace": "cairn-target-acceptance",
                "name": "data-falkordb-0", "uid": "pvc-falkordb-uid",
            },
        },
    },
]
protected_pvcs = [
    {"metadata": {"name": "data-cairn-0", "uid": "pvc-cairn-uid"},
     "spec": {"volumeName": "cairn-local-pv1"}, "status": {"phase": "Bound"}},
    {"metadata": {"name": "data-falkordb-0", "uid": "pvc-falkordb-uid"},
     "spec": {"volumeName": "cairn-local-pv2"}, "status": {"phase": "Bound"}},
]
if fixture == "protected-pvc-drift":
    protected_pvcs[0]["metadata"]["uid"] = "drifted-uid"
available = [
    {
        "metadata": {"name": "cairn-local-pv3"},
        "status": {"phase": "Available"},
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "capacity": {"storage": "10Gi"},
            "storageClassName": "cairn-local",
            "persistentVolumeReclaimPolicy": "Retain",
            "local": {"path": "/mnt/cairn-local/pv3"},
        },
    },
    {
        "metadata": {"name": "cairn-local-pv4"},
        "status": {"phase": "Available"},
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "capacity": {"storage": "10Gi"},
            "storageClassName": "cairn-local",
            "persistentVolumeReclaimPolicy": "Retain",
            "local": {"path": "/mnt/cairn-local/pv4"},
        },
    },
]
if fixture == "protected-pv":
    available[0] = protected[0]

joined = " ".join(args)
images = {
    "cilium-agent": "quay.io/cilium/cilium:v1.19.6@sha256:0df5b2750b64c49843aba1d649e9eaf61467cb0645ad3171db6f6962c095ac92",
    "cilium-operator": "quay.io/cilium/operator-generic:v1.19.6@sha256:0db4ca4e06969d8904ee036617795d0e9c3228cf7b8d902ba74fc2bb98d2d665",
    "cilium-envoy": "quay.io/cilium/cilium-envoy:v1.36.9-1782267392-edeb3f2af56c37c407efa1f63f0b32f595399bbc@sha256:767101fb8a5e38f055778cb43b7aa8eed80450b37f8121effac3d9de9e06dc99",
}
def workload(kind, name, container):
    labels = {"app": name}
    status = {"observedGeneration": 1}
    if kind == "DaemonSet":
        status.update({key: 1 for key in ("desiredNumberScheduled", "currentNumberScheduled",
                                           "updatedNumberScheduled", "numberAvailable", "numberReady")})
    else:
        status.update({key: 1 for key in ("replicas", "updatedReplicas", "availableReplicas", "readyReplicas")})
    if fixture == "stale-cilium" and name == "cilium":
        status["numberReady"] = 0
    return {"apiVersion": "apps/v1", "kind": kind,
            "metadata": {"name": name, "namespace": "kube-system", "uid": name + "-uid", "generation": 1},
            "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels},
                                  "spec": {"containers": [{"name": container, "image": images[container]}]}}},
            "status": status}
def owner(kind, name, uid):
    return [{"apiVersion": "apps/v1", "kind": kind, "name": name, "uid": uid, "controller": True}]
replicaset = {"apiVersion": "apps/v1", "kind": "ReplicaSet",
    "metadata": {"name": "cilium-operator-abcde", "namespace": "kube-system",
                 "uid": "operator-rs-uid", "generation": 1,
                 "labels": {"app": "cilium-operator", "pod-template-hash": "abcde"},
                 "ownerReferences": owner("Deployment", "cilium-operator", "cilium-operator-uid")},
    "spec": {"replicas": 1,
             "selector": {"matchLabels": {"app": "cilium-operator", "pod-template-hash": "abcde"}},
             "template": {"metadata": {"labels": {"app": "cilium-operator", "pod-template-hash": "abcde"}}}},
    "status": {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1,
               "availableReplicas": 1, "fullyLabeledReplicas": 1}}
def cilium_pod(name, container, owner_kind, owner_name, owner_uid, labels):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "kube-system", "uid": name + "-uid",
                         "labels": labels, "ownerReferences": owner(owner_kind, owner_name, owner_uid)},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": container, "ready": True,
                           "imageID": "containerd://" + images[container]}]}}
cilium_pods = [
    cilium_pod("cilium-abc", "cilium-agent", "DaemonSet", "cilium", "cilium-uid", {"app": "cilium"}),
    cilium_pod("cilium-operator-abcde-x", "cilium-operator", "ReplicaSet", "cilium-operator-abcde", "operator-rs-uid", {"app": "cilium-operator", "pod-template-hash": "abcde"}),
    cilium_pod("cilium-envoy-abc", "cilium-envoy", "DaemonSet", "cilium-envoy", "cilium-envoy-uid", {"app": "cilium-envoy"}),
]
temporary_resources = {
    ("pod", "cairn-task11a-0123456-01", "task11a-bootstrap"),
    ("pod", "cairn-egress", "task11a-probe"),
    ("pod", "cairn-task11a-0123456-01", "task11a-control"),
    ("networkpolicy", "cairn-task11a-0123456-01", "task11a-control-to-cairn"),
    ("pod", "cairn-task11a-0123456-01", "task11a-listener"),
    ("service", "cairn-task11a-0123456-01", "task11a-listener"),
}
def temporary_identity(arguments):
    if len(arguments) < 5 or "-n" not in arguments:
        return None
    return (arguments[1], arguments[arguments.index("-n") + 1], arguments[arguments.index("-n") + 2])
def temporary_marker(identity):
    return root / ("deleted-" + "-".join(identity))
identity = temporary_identity(args)
if args[:1] == ["delete"] and identity in temporary_resources:
    if fixture == "cleanup-delete-failure" and identity[2] == "task11a-control":
        raise SystemExit(1)
    temporary_marker(identity).write_text("deleted", encoding="utf-8")
    print("ok")
    raise SystemExit(0)
elif args[:1] == ["get"] and identity in temporary_resources and "-o" in args and "name" in args:
    if not temporary_marker(identity).exists():
        print(f"{identity[0]}/{identity[2]}")
    raise SystemExit(0)
if args[:2] == ["delete", "pod"]:
    for workload in ("cairn-0", "falkordb-0"):
        if workload in args:
            generation = root / f"{workload}.generation"
            current = int(generation.read_text()) if generation.exists() else 1
            generation.write_text(str(current + 1), encoding="utf-8")
if args[:1] == ["version"]:
    print(json.dumps({
        "clientVersion": {"gitVersion": "v1.35.0"},
        "serverVersion": {"gitVersion": "v1.35.0"},
    }))
elif args[:3] == ["get", "node", "reference"]:
    print(json.dumps({
        "metadata": {"name": "reference"},
        "spec": {"podCIDR": (
            "203.0.113.0/24" if fixture == "cidr-drift" else "10.244.1.0/24"
        )},
        "status": {
            "addresses": [{"type": "InternalIP", "address": "192.168.50.50"}],
            "conditions": [{"type": "Ready", "status": "True"}],
            "nodeInfo": {"containerRuntimeVersion": "containerd://2.3.3"},
        },
    }))
elif args[:3] == ["get", "storageclass", "cairn-local"]:
    print(json.dumps({
        "metadata": {"name": "cairn-local"},
        "provisioner": (
            "example.invalid/dynamic" if fixture == "storage-drift"
            else "kubernetes.io/no-provisioner"
        ),
        "volumeBindingMode": "WaitForFirstConsumer",
    }))
elif args[:3] == ["get", "configmap", "kubeadm-config"]:
    print(json.dumps({"data": {"ClusterConfiguration": "networking:\n  podSubnet: 10.244.0.0/16\n  serviceSubnet: 10.96.0.0/12\n"}}))
elif args[:3] == ["get", "namespace", "cairn-target-acceptance"]:
    print(json.dumps({
        "metadata": {
            "name": "cairn-target-acceptance",
            "labels": {
                "cairn.example.invalid/instance": "cairn-target-acceptance",
                "cairn.example.invalid/acceptance-owner": "task11",
                "cairn.example.invalid/acceptance-run": "task11-0de93bd1492b-01",
                "cairn.example.invalid/acceptance-revision": "0de93bd1492beda56143bc45b155fccf7cd47caf",
            },
        }
    }))
elif args[:3] == ["get", "namespace", "cairn-egress"]:
    print(json.dumps({
        "metadata": {
            "name": "cairn-egress",
            "labels": {
                "cairn.example.invalid/acceptance-owner": "task11",
                "cairn.example.invalid/acceptance-run": "task11-0de93bd1492b-01",
                "cairn.example.invalid/acceptance-revision": "0de93bd1492beda56143bc45b155fccf7cd47caf",
            },
        }
    }))
elif args[:3] == ["get", "namespace", "cairn-task11a-0123456-01"]:
    if fixture == "gateway-recovery-query-failure" and (root / "failure-triggered").exists():
        raise SystemExit(1)
    if (root / "namespace-applied").exists():
        print(json.dumps({"metadata": {"name": "cairn-task11a-0123456-01"}}))
    else:
        raise SystemExit(1)
elif args[:2] == ["get", "namespaces"]:
    print(json.dumps({"items": [
        {"metadata": {"name": "cairn-target-acceptance", "labels": {
            "cairn.example.invalid/instance": "cairn-target-acceptance",
            "cairn.example.invalid/acceptance-owner": "task11",
        }}},
        {"metadata": {"name": "cairn-task11a-0123456-01", "labels": {
            "cairn.example.invalid/instance": "cairn-task11a-0123456-01",
            "cairn.example.invalid/posture-owner": "task11a",
        }}},
    ]}))
elif args[:2] == ["get", "pod"] and "{.metadata.uid}" in joined:
    workload = "cairn-0" if "cairn-0" in args else "falkordb-0"
    generation = root / f"{workload}.generation"
    value = generation.read_text(encoding="utf-8") if generation.exists() else "1"
    print(f"{workload}-uid-{value}")
elif args[:2] == ["get", "pod"] and "{.status.podIP}" in joined:
    print("10.244.1.20")
elif args[:2] == ["get", "pod"] and "-o json" in joined:
    workload_name = next(
        (name for name in ("cairn-0", "falkordb-0") if name in args), None
    )
    if workload_name is None:
        print(json.dumps({"metadata": {"name": args[-3]}}))
    else:
        workload_name = workload_name.removesuffix("-0")
        if workload_name == "cairn":
            security = {
                "runAsNonRoot": True,
                "fsGroup": 65532,
                "fsGroupChangePolicy": "OnRootMismatch",
            }
            source_image = os.environ["IMAGE"]
            image = (
                source_image
                if "/" in source_image
                else "docker.io/library/" + source_image
            )
            image_id = "containerd://" + os.environ["FAKE_CAIRN_CONFIG_DIGEST"]
        else:
            security = {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 0,
                "fsGroup": 10001,
                "fsGroupChangePolicy": "OnRootMismatch",
            }
            image = "docker.io/falkordb/falkordb:v4.20.4@sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62"
            image_id = "containerd://sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62"
        print(json.dumps({
            "metadata": {"name": workload_name + "-0"},
            "spec": {
                "securityContext": security,
                "containers": [{"name": workload_name, "image": image}],
            },
            "status": {"containerStatuses": [{
                "name": workload_name, "imageID": image_id,
            }]},
        }))
elif args[:2] == ["get", "pv"]:
    if fixture == "gateway-recovery-query-failure" and (root / "failure-triggered").exists():
        raise SystemExit(1)
    print(json.dumps({"items": protected + available}))
elif args[:2] == ["get", "pvc"]:
    print(json.dumps({"items": protected_pvcs}))
elif args[:3] == ["get", "crd", "ciliumnetworkpolicies.cilium.io"]:
    print(json.dumps({"metadata": {"name": "ciliumnetworkpolicies.cilium.io"}}))
elif args[:3] == ["get", "daemonset", "cilium"]:
    print(json.dumps(workload("DaemonSet", "cilium", "cilium-agent")))
elif args[:3] == ["get", "deployment", "cilium-operator"]:
    print(json.dumps(workload("Deployment", "cilium-operator", "cilium-operator")))
elif args[:3] == ["get", "daemonset", "cilium-envoy"]:
    print(json.dumps(workload("DaemonSet", "cilium-envoy", "cilium-envoy")))
elif args[:2] == ["get", "replicasets"]:
    print(json.dumps({"items": [replicaset]}))
elif args[:2] == ["get", "pods"] and "--namespace" in args:
    print(json.dumps({"items": cilium_pods}))
elif args[:2] == ["get", "deployment,service,configmap,networkpolicy,ciliumnetworkpolicy"]:
    if fixture == "gateway-recovery-query-failure" and (root / "failure-triggered").exists():
        raise SystemExit(1)
    identities = [
        ("ConfigMap", "egress-gateway-config"),
        ("Service", "cairn-egress-gateway"),
        ("Deployment", "cairn-egress-gateway"),
        ("NetworkPolicy", "egress-gateway-allow-cairn-ingress"),
        ("NetworkPolicy", "egress-gateway-allow-egress"),
        ("NetworkPolicy", "egress-gateway-default-deny"),
    ]
    if (root / "gateway-applied").exists() or fixture == "unowned-cnp":
        identities.append(("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https"))
    if fixture == "missing-gateway-object":
        identities.pop()
    if fixture == "extra-gateway-object":
        identities.append(("Service", "unexpected-gateway-service"))
    items = []
    for kind, name in identities:
        task11a = kind == "CiliumNetworkPolicy" or (
            (root / "gateway-applied").exists()
            and (kind, name) == ("NetworkPolicy", "egress-gateway-allow-egress")
        )
        labels = {
            "cairn.example.invalid/acceptance-owner": (
                "other" if fixture == "unowned-cnp" and task11a
                else "task11a" if task11a else "task11"
            ),
            "cairn.example.invalid/acceptance-run": (
                "task11a-0123456-01" if task11a else "task11-0de93bd1492b-01"
            ),
            "cairn.example.invalid/acceptance-revision": (
                "0123456789abcdef0123456789abcdef01234567"
                if task11a else "0de93bd1492beda56143bc45b155fccf7cd47caf"
            ),
        }
        items.append({"kind": kind, "metadata": {
            "name": name, "namespace": "cairn-egress", "labels": labels,
        }})
    root_ca_labels = {}
    if fixture == "labelled-kube-root-ca":
        root_ca_labels = {
            "cairn.example.invalid/acceptance-owner": "task11",
            "cairn.example.invalid/acceptance-run": "task11-0de93bd1492b-01",
            "cairn.example.invalid/acceptance-revision": "0de93bd1492beda56143bc45b155fccf7cd47caf",
        }
    items.append({"kind": "ConfigMap", "metadata": {
        "name": "kube-root-ca.crt", "namespace": "cairn-egress", "labels": root_ca_labels,
    }})
    print(json.dumps({"items": items}))
elif args[:2] == ["get", "pods"] and "kube-system" in args:
    print(json.dumps({"items": [{"spec": {"containers": [
        {"image": "quay.io/cilium/cilium:v1.19.6@sha256:0df5b2750b64c49843aba1d649e9eaf61467cb0645ad3171db6f6962c095ac92"},
        {"image": "quay.io/cilium/operator-generic:v1.19.6@sha256:0db4ca4e06969d8904ee036617795d0e9c3228cf7b8d902ba74fc2bb98d2d665"},
        {"image": "quay.io/cilium/cilium-envoy:v1.36.9-1782267392-edeb3f2af56c37c407efa1f63f0b32f595399bbc@sha256:767101fb8a5e38f055778cb43b7aa8eed80450b37f8121effac3d9de9e06dc99"},
    ]}}]}))
elif args[:2] == ["kustomize", "--load-restrictor"]:
    print((Path.cwd() / "deploy/kustomize/rendered/kubernetes-retrieval.yaml").read_text())
elif args[:1] in (["apply"], ["delete"], ["wait"], ["rollout"], ["label"]):
    print("ok")
elif args[:3] == ["create", "secret", "generic"]:
    token = sys.stdin.read()
    print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {},
                      "data": {"token": base64.b64encode(token.encode()).decode()}}))
elif args[:1] == ["exec"]:
    stdin_payload = sys.stdin.read() if "-i" in args else ""
    if "cairn migrate" in joined:
        if fixture == "bootstrap-migration-failure":
            raise SystemExit(3)
        print(json.dumps({"status": "ok", "operation": "migrate"}))
    elif "cairn bootstrap" in joined:
        if fixture == "bootstrap-exec-timeout":
            raise SystemExit(124)
        print(json.dumps({"token": os.environ["FAKE_BOOTSTRAP_TOKEN"]}))
    elif " ingest http://cairn:8000 " in f" {joined} ":
        print("ok fake-mutation")
    elif "sqlite_sequence" in joined and "projection_outbox" in joined:
        high_water = root / "projection-high-water-reads"
        reads = int(high_water.read_text()) if high_water.exists() else 0
        high_water.write_text(str(reads + 1), encoding="utf-8")
        print("0" if reads == 0 else "1")
    elif "SELECT COUNT(*) FROM projection_outbox" in joined:
        print("0")
    elif "GRAPH.RO_QUERY" in joined and "cairn.fact.projected" in joined:
        print("OK")
        print("task11a-provider-0123456789abcdef0123456789abcdef01234567")
    elif "stat -c" in joined and "cairn-0" in joined:
        print("2770 0:65532")
    elif "stat -c" in joined and "falkordb-0" in joined:
        print("2770 0:10001")
    elif "task11a-network-positive" in joined:
        if "socket.create_connection" not in stdin_payload:
            raise SystemExit(1)
        print(
            "blocked"
            if fixture == "node-positive-failure" and "192.168.50.50" in args
            else "connected"
        )
    elif "task11a-network-negative" in joined:
        if "socket.create_connection" not in stdin_payload:
            raise SystemExit(1)
        if fixture == "negative-dns-failure" and "kubernetes.default.svc" in args:
            print("resolution-failed")
            raise SystemExit(2)
        print("blocked")
    elif "task11a-observe:cairn-runtime" in joined:
        print("65532:0:0 65532")
    elif "task11a-observe:cairn-wal" in joined:
        print("wal")
    elif "task11a-observe:cairn-lease" in joined:
        print("locked")
    elif "task11a-observe:cairn-restart" in joined:
        print("replaced")
    elif "task11a-observe:cairn-integrity" in joined:
        print("ok")
    elif "task11a-observe:falkordb-runtime" in joined:
        print("10001:0:0 10001")
    elif "task11a-observe:falkordb-auth" in joined:
        if ("NOAUTH Authentication required." not in joined
                or "auth=$(awk" not in joined
                or joined.index("NOAUTH Authentication required.") > joined.index("auth=$(awk")):
            raise SystemExit(1)
        print("noauth-refused-then-authenticated")
    elif "task11a-observe:falkordb-restart" in joined:
        print("persisted")
    elif "task11a-observe:retrieval-composition" in joined:
        print("enabled")
    elif "socket.getaddrinfo" in joined:
        code_index = len(args) - 1 - args[::-1].index("-c")
        code = args[code_index + 1]
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        try:
            exec(code, {})
        finally:
            socket.getaddrinfo = original
    else:
        print("success")
elif args[:1] == ["get"]:
    print("success")
else:
    print("ok")
""",
    )


def _run_fake_harness(
    mode: str,
    fixture: str,
    *,
    include_pythonpath: bool = True,
    image: str = "cairn:task11a",
) -> int:
    assert _FAKE_HARNESS_ROOT is not None
    root = _FAKE_HARNESS_ROOT
    if not (root / "bin").exists():
        _install_fake_commands(root)
    (root / "bin" / "socat").chmod(0o600 if fixture == "missing-socat" else 0o755)
    for marker in root.glob("deleted-*"):
        marker.unlink()
    (root / "kubectl.jsonl").write_text("", encoding="utf-8")
    for name in ("openai-api-key", "falkordb-password", "client-token"):
        value = (
            ""
            if fixture == "empty-openai" and name == "openai-api-key"
            else f"fake-{name}\n"
        )
        (root / name).write_text(value, encoding="utf-8")
    (root / "task11-primary.json").write_text("accepted primary\n", encoding="utf-8")
    (root / "task11-cilium.json").write_text("accepted supplement\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PATH": str(root / "bin"),
            "KUBECTL": str(root / "bin" / "kubectl"),
            "FAKE_KUBE_ROOT": str(root),
            "FAKE_KUBE_FIXTURE": fixture,
            "FAKE_BOOTSTRAP_TOKEN": "cairn1.fake-bootstrap-secret-value",
            "FAKE_CAIRN_CONFIG_DIGEST": CAIRN_CONFIG_DIGEST,
            "IMAGE": image,
            "REFERENCE_POSTURE_RUN_ID": "task11a-0123456-01",
            "REFERENCE_POSTURE_REPORT": str(root / "report.json"),
            "REFERENCE_POSTURE_OPENAI_API_KEY_FILE": str(root / "openai-api-key"),
            "REFERENCE_POSTURE_FALKORDB_PASSWORD_FILE": str(root / "falkordb-password"),
            "REFERENCE_POSTURE_TASK11_PRIMARY_REPORT": str(
                root / "task11-primary.json"
            ),
            "REFERENCE_POSTURE_TASK11_CILIUM_REPORT": str(root / "task11-cilium.json"),
        }
    )
    if include_pythonpath:
        environment["PYTHONPATH"] = str(REPOSITORY / "src")
    completed = subprocess.run(
        [str(HARNESS), mode],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    (root / "harness.stdout").write_text(completed.stdout, encoding="utf-8")
    (root / "harness.stderr").write_text(completed.stderr, encoding="utf-8")
    return completed.returncode


def _fake_kubectl_applied_paths() -> list[str]:
    assert _FAKE_HARNESS_ROOT is not None
    assert _run_fake_harness("run", fixture="safe") == 0, (
        _FAKE_HARNESS_ROOT / "harness.stderr"
    ).read_text(encoding="utf-8")
    calls = [
        json.loads(line)
        for line in (_FAKE_HARNESS_ROOT / "kubectl.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    protected = (
        "cairn-target-acceptance",
        "cairn-local-pv1",
        "cairn-local-pv2",
    )
    for call in calls:
        if call and call[0] in {"apply", "delete", "chmod", "chown"}:
            assert not any(value in " ".join(call) for value in protected)
    return [
        call[call.index("-f") + 1]
        for call in calls
        if call[:1] == ["apply"]
        and "-f" in call
        and "egress-gateway" in call[call.index("-f") + 1]
    ]


def test_required_check_set_is_the_task11a_suite(state: ModuleType) -> None:
    assert state.REQUIRED_CHECKS == REQUIRED_CHECKS


def test_preflight_selects_only_the_two_unclaimed_task11a_pvs(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    result = _preflight(state, preflight_args)

    assert result["selected_pvs"] == ["cairn-local-pv3", "cairn-local-pv4"]


@pytest.mark.parametrize(
    "path",
    [
        "/.",
        "/tmp/..",
        "/mnt/cairn-local/pv3/.",
    ],
)
def test_preflight_refuses_noncanonical_or_duplicate_pv_path_aliases(
    state: ModuleType, preflight_args: dict[str, object], path: str
) -> None:
    args = copy.deepcopy(preflight_args)
    pvs = cast(list[dict[str, object]], args["available_pvs"])
    spec = cast(dict[str, object], pvs[1]["spec"])
    local = cast(dict[str, object], spec["local"])
    local["path"] = path

    with pytest.raises(state.PostureRefusal, match="canonical"):
        _preflight(state, args)


def test_preflight_refuses_an_alias_of_a_protected_pv_path(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    args = copy.deepcopy(preflight_args)
    pvs = cast(list[dict[str, object]], args["available_pvs"])
    spec = cast(dict[str, object], pvs[0]["spec"])
    local = cast(dict[str, object], spec["local"])
    local["path"] = "/var/lib/cairn/acceptance/cairn/."

    with pytest.raises(state.PostureRefusal, match="canonical"):
        _preflight(state, args)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda args: args["available_pvs"][0]["spec"].__setitem__(
                "claimRef", {"namespace": "other", "name": "pvc"}
            ),
            "claim",
        ),
        (
            lambda args: args["available_pvs"][0]["spec"].__setitem__(
                "persistentVolumeReclaimPolicy", "Delete"
            ),
            "Retain",
        ),
        (
            lambda args: args["available_pvs"][0]["spec"].__setitem__(
                "accessModes", ["ReadWriteOnce"]
            ),
            "ReadWriteOncePod",
        ),
        (
            lambda args: args["available_pvs"][0]["spec"]["capacity"].__setitem__(
                "storage", "9Gi"
            ),
            "capacity",
        ),
        (
            lambda args: args["available_pvs"][0]["metadata"].__setitem__(
                "name", "cairn-local-pv1"
            ),
            "protected",
        ),
        (
            lambda args: args["cluster_cidrs"].__setitem__("pod", "203.0.113.0/24"),
            "CIDR",
        ),
        (lambda args: args.__setitem__("cilium_crd_present", False), "Cilium"),
    ],
)
def test_preflight_refuses_unsafe_target_drift(
    state: ModuleType, preflight_args: dict[str, object], change: object, message: str
) -> None:
    args = copy.deepcopy(preflight_args)
    change(args)  # type: ignore[operator]

    with pytest.raises(state.PostureRefusal, match=message):
        _preflight(state, args)


def _replace_path(value: object, path: tuple[object, ...], replacement: object) -> None:
    current = value
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    current[path[-1]] = replacement  # type: ignore[index]


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (("target_identity", "host", "distribution"), "Fedora Linux 45", "host"),
        (("target_identity", "host", "selinux"), "Permissive", "SELinux"),
        (("target_identity", "kubernetes", "server_version"), "v1.36.0", "Kubernetes"),
        (("target_identity", "kubernetes", "node_ready"), False, "Ready"),
        (("target_identity", "cilium", "chart"), "cilium-1.20.0", "Cilium"),
        (("target_identity", "cilium", "images"), [], "Cilium"),
        (("storage_identity", "provisioner"), "example.invalid/dynamic", "provisioner"),
        (("storage_identity", "volume_binding_mode"), "Immediate", "binding"),
        (("render_identities",), [], "render"),
        (("render_identities", 0, "sha256"), "0" * 64, "render"),
        (("image_identity", "source_revision"), TASK11_REVISION, "image revision"),
        (("image_identity", "reference"), "docker.io/library/cairn:latest", "image"),
        (("image_identity", "config_digest"), "sha256:" + "8" * 64, "runtime"),
        (("accepted_task11_reports",), [], "Task 11"),
        (("accepted_task11_reports", 0, "sha256"), "0" * 64, "Task 11"),
        (("credential_sources", 0, "non_empty"), False, "credential"),
        (("credential_sources", 1, "mode"), "644", "credential"),
        (("tool_inventory",), [], "tool"),
        (("runtime_expectations", 1, "mount_path"), "/var/lib/falkordb", "runtime"),
        (("gateway_inventory",), copy.deepcopy(GATEWAY_POST_INVENTORY), "gateway"),
        (("gateway_inventory", 0, "owner"), "task11a", "gateway"),
    ],
)
def test_preflight_refuses_empty_stale_or_mismatched_bound_identities(
    state: ModuleType,
    preflight_args: dict[str, object],
    path: tuple[object, ...],
    replacement: object,
    message: str,
) -> None:
    args = copy.deepcopy(preflight_args)
    _replace_path(args, path, replacement)

    with pytest.raises(state.PostureRefusal, match=message):
        _preflight(state, args)


def test_runtime_expectations_come_from_committed_dockerfile_and_render(
    state: ModuleType,
) -> None:
    observed = state.runtime_expectations_from_files(
        REPOSITORY / "Dockerfile",
        REPOSITORY / "deploy/kustomize/rendered/kubernetes-retrieval.yaml",
        "docker.io/library/cairn:task11a",
    )

    assert observed == _runtime_expectations()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", "0123456"),
        ("run_id", "task11a-not-a-revision"),
        ("namespace", "cairn-task11a-other-run"),
        ("owner", "other-owner"),
    ],
)
def test_preflight_refuses_malformed_identifiers(
    state: ModuleType, preflight_args: dict[str, object], field: str, value: str
) -> None:
    args = copy.deepcopy(preflight_args)
    args[field] = value

    with pytest.raises(state.PostureRefusal):
        _preflight(state, args)


def test_preflight_refuses_a_run_id_unbound_to_its_full_revision(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    args = copy.deepcopy(preflight_args)
    args["run_id"] = "task11a-abcdef0-01"
    args["namespace"] = "cairn-task11a-abcdef0-01"

    with pytest.raises(state.PostureRefusal, match="revision"):
        _preflight(state, args)


def test_preflight_refuses_a_digest_image_reference_until_renderer_supports_it(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    args = copy.deepcopy(preflight_args)
    reference = "docker.io/library/cairn@" + ("sha256:" + "1" * 64)
    image_identity = cast(dict[str, object], args["image_identity"])
    runtime_expectations = cast(list[dict[str, object]], args["runtime_expectations"])
    image_identity["reference"] = reference
    runtime_expectations[0]["image_reference"] = reference

    with pytest.raises(state.PostureRefusal, match="tag"):
        _preflight(state, args)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda checks: checks.append(
            {
                "name": "invented-check",
                "outcome": "passed",
                "expected": {"result": "success"},
                "observed": {"result": "success"},
            }
        ),
        lambda checks: checks.append(copy.deepcopy(checks[0])),
        lambda checks: checks.__delitem__(-1),
        lambda checks: checks[0].__setitem__("outcome", "failed"),
        lambda checks: checks[0].__setitem__("self_declared", True),
    ],
)
def test_report_refuses_incomplete_or_unobserved_check_claims(
    state: ModuleType, preflight_args: dict[str, object], mutation: object
) -> None:
    checks = _checks()
    mutation(checks)  # type: ignore[operator]
    arguments = _report_args(_preflight(state, preflight_args))
    arguments["checks"] = checks

    with pytest.raises(state.PostureRefusal):
        state.build_posture_report(**arguments)


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("pv_observations", lambda value: value.clear()),
        ("pv_observations", lambda value: value.append(copy.deepcopy(value[0]))),
        (
            "pv_observations",
            lambda value: value[1].__setitem__("mount_path", "/var/lib/falkordb"),
        ),
        (
            "pv_observations",
            lambda value: value[0]["post_mount"].__setitem__(
                "effective_group_writable", False
            ),
        ),
        ("runtime_observations", lambda value: value.clear()),
        (
            "runtime_observations",
            lambda value: value[0]["runtime"].__setitem__("primary_gid", 65532),
        ),
        (
            "runtime_observations",
            lambda value: value[0].__setitem__("runtime_digest", "sha256:" + "9" * 64),
        ),
        (
            "runtime_observations",
            lambda value: value[1].__setitem__(
                "spec_image",
                f"registry.example.invalid/falkordb:v4.20.4@{FALKORDB_DIGEST}",
            ),
        ),
        ("network_observations", lambda value: value.pop()),
        (
            "network_observations",
            lambda value: value[3].__setitem__("positive_control", None),
        ),
        (
            "network_observations",
            lambda value: value[0].__setitem__("name", "invented-attempt"),
        ),
        (
            "network_observations",
            lambda value: value[0].__setitem__("source", "arbitrary/source"),
        ),
        (
            "network_observations",
            lambda value: (
                value[3].__setitem__("destination", "203.0.113.10"),
                value[3]["positive_control"].__setitem__("destination", "203.0.113.10"),
            ),
        ),
        (
            "network_observations",
            lambda value: value[-1]["positive_control"].__setitem__(
                "source", f"{NAMESPACE}/task11a-control"
            ),
        ),
        ("label_inventory", lambda value: value.clear()),
        (
            "label_inventory",
            lambda value: value[1].__setitem__("status", "enforced"),
        ),
        (
            "gateway_observations",
            lambda value: value.__setitem__("post", copy.deepcopy(value["pre"])),
        ),
        (
            "gateway_observations",
            lambda value: value["post"][-1].__setitem__("owner", "task11"),
        ),
    ],
)
def test_report_refuses_missing_duplicate_or_cross_mismatched_observations(
    state: ModuleType,
    preflight_args: dict[str, object],
    field: str,
    mutation: object,
) -> None:
    arguments = _report_args(_preflight(state, preflight_args))
    mutation(arguments[field])  # type: ignore[operator]

    with pytest.raises(state.PostureRefusal):
        state.build_posture_report(**arguments)


def test_checks_are_not_accepted_when_expected_and_observed_repeat_arbitrary_data(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    arguments = _report_args(_preflight(state, preflight_args))
    checks = cast(list[dict[str, object]], arguments["checks"])
    checks[0]["expected"] = {"result": "arbitrary"}
    checks[0]["observed"] = {"result": "arbitrary"}

    with pytest.raises(state.PostureRefusal, match="expected result"):
        state.build_posture_report(**arguments)


def test_report_is_exclusive_and_complete(
    state: ModuleType, preflight_args: dict[str, object], tmp_path: Path
) -> None:
    report = _report(state, _preflight(state, preflight_args))
    path = tmp_path / "report.json"

    state.write_report_exclusive(path, report)

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "passed"
    with pytest.raises(FileExistsError):
        state.write_report_exclusive(path, report)


def _cleanup_results() -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "result": "absent",
        }
        for kind, namespace, name in (
            ("Pod", NAMESPACE, "task11a-bootstrap"),
            ("Pod", "cairn-egress", "task11a-probe"),
            ("Pod", NAMESPACE, "task11a-control"),
            ("NetworkPolicy", NAMESPACE, "task11a-control-to-cairn"),
            ("Pod", NAMESPACE, "task11a-listener"),
            ("Service", NAMESPACE, "task11a-listener"),
        )
    ]


def _recovery_inventory() -> dict[str, object]:
    return {
        "namespace": {
            "query_succeeded": True,
            "name": NAMESPACE,
            "exists": True,
        },
        "selected_pvs": {
            "query_succeeded": True,
            "items": [
                {
                    "name": "cairn-local-pv3",
                    "path": "/mnt/cairn-local/pv3",
                    "phase": "Bound",
                    "claim_namespace": NAMESPACE,
                    "claim_name": "data-cairn-0",
                },
                {
                    "name": "cairn-local-pv4",
                    "path": "/mnt/cairn-local/pv4",
                    "phase": "Bound",
                    "claim_namespace": NAMESPACE,
                    "claim_name": "data-falkordb-0",
                },
            ],
        },
        "gateway_inventory": {
            "query_succeeded": True,
            "items": copy.deepcopy(GATEWAY_POST_INVENTORY),
        },
    }


def _failed_report(
    state: ModuleType, preflight: dict[str, object]
) -> dict[str, object]:
    return cast(
        dict[str, object],
        state.build_failed_posture_report(
            revision=REVISION,
            run_id=RUN_ID,
            phase="gateway-applied",
            preflight=preflight,
            cleanup={"attempted": True, "results": _cleanup_results()},
            recovery_inventory=_recovery_inventory(),
        ),
    )


def test_failed_report_is_distinct_immutable_and_cannot_be_promoted(
    state: ModuleType, preflight_args: dict[str, object], tmp_path: Path
) -> None:
    preflight = _preflight(state, preflight_args)
    success = _report(state, preflight)
    failed = _failed_report(state, preflight)
    success_path = tmp_path / "task11a-run.json"

    written = state.write_report_exclusive(success_path, failed)

    assert written == tmp_path / "task11a-run.failed.json"
    assert not success_path.exists()
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "failed"
    with pytest.raises(FileExistsError):
        state.write_report_exclusive(success_path, failed)
    with pytest.raises(FileExistsError):
        state.write_report_exclusive(success_path, success)
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "failed"


def test_existing_success_report_also_blocks_failed_report(
    state: ModuleType, preflight_args: dict[str, object], tmp_path: Path
) -> None:
    preflight = _preflight(state, preflight_args)
    success_path = tmp_path / "task11a-run.json"
    state.write_report_exclusive(success_path, _report(state, preflight))

    with pytest.raises(FileExistsError):
        state.write_report_exclusive(success_path, _failed_report(state, preflight))
    assert not (tmp_path / "task11a-run.failed.json").exists()


def test_report_and_outcome_claim_are_both_fsynced(
    state: ModuleType,
    preflight_args: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _preflight(state, preflight_args)
    calls: list[int] = []
    original = state.os.fsync

    def observed_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(state.os, "fsync", observed_fsync)
    state.write_report_exclusive(
        tmp_path / "task11a-run.json", _report(state, preflight)
    )

    assert len(calls) == 2


def test_report_accepts_kubernetes_short_falkordb_image_reference(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    preflight = _preflight(state, preflight_args)
    arguments = _report_args(preflight)
    observations = cast(list[dict[str, object]], arguments["runtime_observations"])
    observations[1]["spec_image"] = f"falkordb/falkordb:v4.20.4@{FALKORDB_DIGEST}"

    report = state.build_posture_report(**arguments)

    assert (
        report["runtime_observations"][1]["spec_image"] == observations[1]["spec_image"]
    )


def test_concurrent_report_writers_claim_exactly_one_outcome(
    state: ModuleType,
    preflight_args: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _preflight(state, preflight_args)
    success_path = tmp_path / "task11a-run.json"
    reports = (_report(state, preflight), _failed_report(state, preflight))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    opens = 0
    original_open = state.os.open

    def coordinated_open(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal opens
        with lock:
            opens += 1
            should_wait = opens <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return cast(int, original_open(path, flags, mode))

    monkeypatch.setattr(state.os, "open", coordinated_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(state.write_report_exclusive, success_path, report)
            for report in reports
        ]
    results = [future.exception() for future in futures]

    assert sum(error is None for error in results) == 1
    assert sum(isinstance(error, FileExistsError) for error in results) == 1
    assert (
        success_path.exists() + (tmp_path / "task11a-run.failed.json").exists()
    ) == 1
    assert (tmp_path / "task11a-run.outcome").is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("phase", ""),
        lambda value: value.__setitem__("phase", "invented-phase"),
        lambda value: value["cleanup"].__setitem__("attempted", False),
        lambda value: value["cleanup"].__setitem__("results", []),
        lambda value: value.__setitem__("recovery_inventory", {}),
    ],
)
def test_failed_report_refuses_incomplete_failure_evidence(
    state: ModuleType,
    preflight_args: dict[str, object],
    mutation: object,
) -> None:
    preflight = _preflight(state, preflight_args)
    arguments = {
        "revision": REVISION,
        "run_id": RUN_ID,
        "phase": "gateway-applied",
        "preflight": preflight,
        "cleanup": {"attempted": True, "results": _cleanup_results()},
        "recovery_inventory": _recovery_inventory(),
    }
    mutation(arguments)  # type: ignore[operator]

    with pytest.raises(state.PostureRefusal):
        state.build_failed_posture_report(**arguments)


def test_failed_report_accepts_partial_typed_recovery_inventory(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    preflight = _preflight(state, preflight_args)
    recovery = _recovery_inventory()
    recovery["selected_pvs"]["items"] = recovery["selected_pvs"]["items"][:1]  # type: ignore[index]
    recovery["gateway_inventory"]["items"] = GATEWAY_POST_INVENTORY[:4]  # type: ignore[index]

    failed = state.build_failed_posture_report(
        revision=REVISION,
        run_id=RUN_ID,
        phase="gateway-apply",
        preflight=preflight,
        cleanup={"attempted": True, "results": _cleanup_results()},
        recovery_inventory=recovery,
    )

    assert failed["recovery_inventory"] == recovery


def test_failed_report_records_unavailable_recovery_queries_without_guessing(
    state: ModuleType, preflight_args: dict[str, object]
) -> None:
    preflight = _preflight(state, preflight_args)
    recovery = {
        "namespace": {
            "query_succeeded": False,
            "name": NAMESPACE,
            "exists": None,
        },
        "selected_pvs": {"query_succeeded": False, "items": []},
        "gateway_inventory": {"query_succeeded": False, "items": []},
    }

    failed = state.build_failed_posture_report(
        revision=REVISION,
        run_id=RUN_ID,
        phase="gateway-apply",
        preflight=preflight,
        cleanup={"attempted": True, "results": _cleanup_results()},
        recovery_inventory=recovery,
    )

    assert failed["recovery_inventory"] == recovery


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["namespace"].__setitem__("exists", None),
        lambda value: value["selected_pvs"].__setitem__("items", [object()]),
        lambda value: value["gateway_inventory"].__setitem__(
            "items", [*GATEWAY_PRE_INVENTORY, GATEWAY_PRE_INVENTORY[0]]
        ),
    ],
)
def test_failed_report_refuses_malformed_recovery_observations(
    state: ModuleType,
    preflight_args: dict[str, object],
    mutation: object,
) -> None:
    preflight = _preflight(state, preflight_args)
    recovery = _recovery_inventory()
    mutation(recovery)  # type: ignore[operator]

    with pytest.raises(state.PostureRefusal):
        state.build_failed_posture_report(
            revision=REVISION,
            run_id=RUN_ID,
            phase="gateway-apply",
            preflight=preflight,
            cleanup={"attempted": True, "results": _cleanup_results()},
            recovery_inventory=recovery,
        )


def test_report_refuses_secret_shaped_content_before_writing(
    state: ModuleType, preflight_args: dict[str, object], tmp_path: Path
) -> None:
    report = _report(state, _preflight(state, preflight_args))
    report["network_observations"] = [{"token": "sk-abcdefghijklmnopqrstuvwxyz"}]
    path = tmp_path / "report.json"

    with pytest.raises(state.PostureRefusal, match="secret"):
        state.write_report_exclusive(path, report)
    assert not path.exists()


def test_report_refuses_non_json_content_before_writing(
    state: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "report.json"

    with pytest.raises(state.PostureRefusal, match="JSON"):
        state.write_report_exclusive(path, {"number": float("nan")})
    assert not path.exists()


def test_cli_validates_stdin_and_emits_one_json_object(
    preflight_args: dict[str, object],
) -> None:
    completed = subprocess.run(
        [sys.executable, str(STATE), "validate-preflight"],
        input=json.dumps(preflight_args),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["selected_pvs"] == [
        "cairn-local-pv3",
        "cairn-local-pv4",
    ]


def test_cli_refusal_has_the_required_exit_code(
    preflight_args: dict[str, object],
) -> None:
    preflight_args["cilium_crd_present"] = False
    completed = subprocess.run(
        [sys.executable, str(STATE), "validate-preflight"],
        input=json.dumps(preflight_args),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1


@pytest.mark.parametrize(
    "fixture",
    [
        "protected-pv",
        "protected-pvc-drift",
        "cidr-drift",
        "unowned-cnp",
        "missing-gateway-object",
        "extra-gateway-object",
        "stale-cilium",
        "selinux-drift",
        "storage-drift",
        "stale-image-revision",
        "stale-task11-report",
        "empty-openai",
        "missing-socat",
        "firewalld-inactive",
        "firewalld-no-zone",
        "firewalld-rule-preexisting",
        "node-port-in-use",
        "path-alias",
        "inode-alias",
    ],
)
def test_preflight_refuses_before_apply_on_protected_or_target_drift(
    tmp_path: Path, fixture: str
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("preflight", fixture=fixture) == 1
        calls = [
            json.loads(line)
            for line in (tmp_path / "kubectl.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    assert not any(call and call[0] in {"apply", "delete"} for call in calls)


def test_missing_socat_is_isolated_from_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    marker = tmp_path / "host-socat-called"
    _fake_executable(host_bin / "socat", f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n")
    monkeypatch.setenv("PATH", f"{host_bin}:{os.environ['PATH']}")
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("preflight", fixture="missing-socat") == 1
        assert (
            "required tool is not an absolute executable: socat"
            in (tmp_path / "harness.stderr").read_text()
        )
        assert _run_fake_harness("preflight", fixture="safe") == 0
    assert not marker.exists()


def test_gateway_is_rechecked_as_six_objects_then_proved_as_seven(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    inventory_queries = [
        index
        for index, call in enumerate(calls)
        if call[:2]
        == ["get", "deployment,service,configmap,networkpolicy,ciliumnetworkpolicy"]
    ]
    first_apply = next(
        index for index, call in enumerate(calls) if call[:1] == ["apply"]
    )

    assert len(inventory_queries) == 3
    assert (
        inventory_queries[0] < inventory_queries[1] < first_apply < inventory_queries[2]
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["gateway_observations"] == {
        "pre": GATEWAY_PRE_INVENTORY,
        "post": GATEWAY_POST_INVENTORY,
    }


def test_narrowed_gateway_policy_records_task11a_provenance(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    payloads = [
        json.loads(line)["payload"]
        for line in (tmp_path / "applied-payloads.jsonl").read_text().splitlines()
    ]
    gateway_payload = next(
        payload
        for payload in payloads
        if "egress-gateway-deny-cluster-https" in payload
    )
    documents = {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(gateway_payload)
    }
    task11a = {
        ("NetworkPolicy", "egress-gateway-allow-egress"),
        ("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https"),
    }
    for identity, document in documents.items():
        labels = document["metadata"]["labels"]
        if identity in task11a:
            assert labels["cairn.example.invalid/acceptance-owner"] == "task11a"
            assert labels["cairn.example.invalid/acceptance-run"] == RUN_ID
            assert labels["cairn.example.invalid/acceptance-revision"] == REVISION
        else:
            assert labels["cairn.example.invalid/acceptance-owner"] == "task11"
            assert labels["cairn.example.invalid/acceptance-run"] == TASK11_RUN_ID
            assert (
                labels["cairn.example.invalid/acceptance-revision"] == TASK11_REVISION
            )


def test_preflight_allows_the_kubernetes_generated_labelled_root_ca_configmap(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("preflight", fixture="labelled-kube-root-ca") == 0


def test_success_report_cross_binds_runtime_storage_and_target_identities(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert report["pv_observations"] == _pv_observations()
    assert report["runtime_observations"] == _runtime_observations()
    assert sorted(
        report["network_observations"],
        key=lambda item: cast(str, item["name"]),
    ) == sorted(_network_observations(), key=lambda item: cast(str, item["name"]))
    assert report["preflight"]["target_identity"]["host"] == {
        "hostname": "reference",
        "distribution": "Fedora Linux 44",
        "selinux": "Enforcing",
    }
    assert report["preflight"]["accepted_task11_reports"] == [
        {
            "name": "primary",
            "path": str(tmp_path / "task11-primary.json"),
            "sha256": TASK11_PRIMARY_SHA256,
        },
        {
            "name": "cilium-supplement",
            "path": str(tmp_path / "task11-cilium.json"),
            "sha256": TASK11_CILIUM_SHA256,
        },
    ]


def test_falkordb_proof_executes_exact_noauth_refusal_before_authentication(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    command = next(
        " ".join(call) for call in calls if "task11a-observe:falkordb-auth" in call
    )

    assert command.index("NOAUTH Authentication required.") < command.index(
        "auth=$(awk"
    )
    checks = {
        item["name"]: item
        for item in json.loads((tmp_path / "report.json").read_text())["checks"]
    }
    assert checks["falkordb-auth"]["observed"] == {
        "result": "noauth-refused-then-authenticated"
    }


def test_run_applies_target_gateway_render_not_generic_render(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        applied = _fake_kubectl_applied_paths()

    assert len(applied) == 1
    assert applied[0].endswith("/egress-gateway-reference-owned.yaml")


def test_registry_qualified_cairn_image_reaches_both_probe_pods(tmp_path: Path) -> None:
    image = "registry.example/cairn:v0.1.0-reviewed"
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe", image=image) == 0

    payloads = [
        json.loads(line)["payload"]
        for line in (tmp_path / "applied-payloads.jsonl").read_text().splitlines()
    ]
    probe_payloads = [
        payload
        for payload in payloads
        if "task11a-listener" in payload or "task11a-probe" in payload
    ]
    assert len(probe_payloads) == 2
    assert all(f"image: {image}" in payload for payload in probe_payloads)


def test_cleanup_removes_only_run_owned_probe_and_listener(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
        calls = [
            json.loads(line)
            for line in (tmp_path / "kubectl.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    deletes = [
        call
        for call in calls
        if call[:1] == ["delete"] and "--ignore-not-found" in call
    ]
    assert deletes == [
        [
            "delete",
            "pod",
            "-n",
            "cairn-egress",
            "task11a-probe",
            "--ignore-not-found",
            "--wait=true",
        ],
        [
            "delete",
            "pod",
            "-n",
            "cairn-task11a-0123456-01",
            "task11a-control",
            "--ignore-not-found",
            "--wait=true",
        ],
        [
            "delete",
            "networkpolicy",
            "-n",
            "cairn-task11a-0123456-01",
            "task11a-control-to-cairn",
            "--ignore-not-found",
            "--wait=true",
        ],
        [
            "delete",
            "pod",
            "-n",
            "cairn-task11a-0123456-01",
            "task11a-listener",
            "--ignore-not-found",
            "--wait=true",
        ],
        [
            "delete",
            "service",
            "-n",
            "cairn-task11a-0123456-01",
            "task11a-listener",
            "--ignore-not-found",
            "--wait=true",
        ],
    ]


def test_all_waited_kubernetes_deletions_allow_the_documented_termination_bound() -> (
    None
):
    harness = HARNESS.read_text(encoding="utf-8")

    assert 'bounded 30s "$kubectl" delete' not in harness
    assert (
        'bounded 120s "$kubectl" delete "$resource" -n "$object_namespace" "$name" \\\n'
        "    --ignore-not-found --wait=true"
    ) in harness
    for pod in ("task11a-bootstrap", "cairn-0", "falkordb-0"):
        assert (
            f'bounded 120s "$kubectl" delete pod -n "$namespace" {pod} --wait=true'
        ) in harness
    assert (
        'bounded 30s "$kubectl" exec -n "$namespace" task11a-bootstrap -- \\\n'
        "    cairn bootstrap"
    ) not in harness
    assert (
        'bounded 120s "$kubectl" exec -n "$namespace" task11a-bootstrap -- \\\n'
        "    cairn bootstrap"
    ) in harness


def test_bootstrap_failure_records_exact_durable_step(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="bootstrap-exec-timeout") == 4

    failed = json.loads((tmp_path / "report.failed.json").read_text(encoding="utf-8"))
    assert failed["phase"] == "bootstrap-command-exec"
    progress = [
        json.loads(line)
        for line in (tmp_path / "report.progress.jsonl").read_text().splitlines()
    ]
    assert progress[-2]["step"] == "bootstrap-command-exec"
    assert progress[-2]["status"] == "started"
    assert progress[-1] == {
        "timestamp": progress[-1]["timestamp"],
        "revision": REVISION,
        "run_id": RUN_ID,
        "step": "bootstrap-command-exec",
        "status": "failed",
        "exit_code": 4,
    }


def test_bootstrap_migrates_the_catalogue_before_minting_a_credential(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    migration_index = next(
        index
        for index, call in enumerate(calls)
        if "cairn migrate --config /etc/cairn/config.yaml" in " ".join(call)
    )
    bootstrap_index = next(
        index
        for index, call in enumerate(calls)
        if "cairn bootstrap --config /etc/cairn/config.yaml" in " ".join(call)
    )

    assert migration_index < bootstrap_index


def test_bootstrap_migration_failure_records_its_exact_durable_step(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="bootstrap-migration-failure") == 4

    failed = json.loads((tmp_path / "report.failed.json").read_text(encoding="utf-8"))
    assert failed["phase"] == "bootstrap-catalogue-migrate"


def test_rendered_posture_instance_identity_is_a_catalogue_valid_uuid4(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    payloads = [
        json.loads(line)["payload"]
        for line in (tmp_path / "applied-payloads.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    instance_payload = next(
        payload for payload in payloads if "kind: StatefulSet" in payload
    )
    instance_id = re.search(
        r"^\s*instance_id: ([0-9a-f-]+)$", instance_payload, re.MULTILINE
    )

    assert instance_id is not None
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        instance_id.group(1),
    )


def test_every_slow_or_mutating_live_stage_has_a_named_durable_step() -> None:
    harness = HARNESS.read_text(encoding="utf-8")

    for step in EXPECTED_DURABLE_STEPS:
        assert f"begin_step {step}" in harness


def test_detached_launcher_preserves_outcome_and_removes_sources(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "reference-posture-launch"
    launcher.write_bytes(LAUNCHER.read_bytes())
    launcher.chmod(0o755)
    harness = scripts / "reference-posture-acceptance"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'{"status":"passed"}\\n\' >"$REFERENCE_POSTURE_REPORT"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    report = repository / "build" / "evidence.json"
    report.parent.mkdir()
    openai = repository / "openai"
    falkordb = repository / "falkordb"
    openai.write_text("secret-a", encoding="utf-8")
    falkordb.write_text("secret-b", encoding="utf-8")
    openai.chmod(0o600)
    falkordb.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        {
            "REFERENCE_POSTURE_REPORT": str(report),
            "REFERENCE_POSTURE_OPENAI_API_KEY_FILE": str(openai),
            "REFERENCE_POSTURE_FALKORDB_PASSWORD_FILE": str(falkordb),
        }
    )

    completed = subprocess.run(
        [str(launcher)],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    for _ in range(100):
        if report.exists() and not openai.exists() and not falkordb.exists():
            break
        threading.Event().wait(0.02)
    assert json.loads(report.read_text(encoding="utf-8")) == {"status": "passed"}
    assert not openai.exists()
    assert not falkordb.exists()
    assert (repository / "build" / "evidence.live.log").is_file()


def test_cleanup_failure_refuses_success_evidence_and_records_failed_cleanup(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="cleanup-delete-failure") == 1

    assert not (tmp_path / "report.json").exists()
    failed = json.loads((tmp_path / "report.failed.json").read_text(encoding="utf-8"))
    result = next(
        item
        for item in failed["cleanup"]["results"]
        if item
        == {
            "kind": "Pod",
            "namespace": NAMESPACE,
            "name": "task11a-control",
            "result": "failed",
        }
    )
    assert result["result"] == "failed"


def test_harness_uses_locked_repository_python_without_pythonpath(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        _install_fake_commands(tmp_path)
        _fake_executable(
            tmp_path / "bin" / "python3",
            "#!/bin/sh\ncase \"${1:-}\" in *'/bin/kubectl'|*'/bin/ctr'|*'/bin/firewall-cmd') exec /usr/bin/python3 \"$@\" ;; esac\nexit 97\n",
        )
        assert (
            _run_fake_harness("preflight", fixture="safe", include_pythonpath=False)
            == 0
        )


def test_existing_atomic_outcome_claim_refuses_before_target_preflight(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        (tmp_path / "report.outcome").write_text(
            '{"status":"failed"}\n', encoding="utf-8"
        )
        assert _run_fake_harness("preflight", fixture="safe") == 1

    assert (tmp_path / "kubectl.jsonl").read_text(encoding="utf-8") == ""


def test_fetch_uv_extracts_the_dockerfile_pinned_binary_without_overwrite(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_uv = tmp_path / "fake-uv"
    _fake_executable(fake_uv, "#!/bin/sh\nprintf 'uv 0.12.0 (test)\\n'\n")
    _fake_executable(
        binary / "docker",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  create) printf 'pinned-uv-container\\n' ;;\n"
        '  cp) cp "$FAKE_UV" "$3" ;;\n'
        "  rm) exit 0 ;;\n"
        "  *) exit 97 ;;\n"
        "esac\n",
    )
    destination = tmp_path / "tools" / "uv"
    environment = os.environ | {
        "PATH": f"{binary}:{os.environ['PATH']}",
        "FAKE_UV": str(fake_uv),
        "UV_DESTINATION": str(destination),
    }

    completed = subprocess.run(
        [str(UV_FETCHER)],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert destination.read_text(encoding="utf-8") == fake_uv.read_text(
        encoding="utf-8"
    )
    repeated = subprocess.run(
        [str(UV_FETCHER)],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode != 0


def test_denials_have_positive_controls_and_secret_values_never_escape(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
        calls = [
            json.loads(line)
            for line in (tmp_path / "kubectl.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    markers = [
        marker
        for call in calls
        for marker in call
        if marker in {"task11a-network-positive", "task11a-network-negative"}
    ]
    assert markers.count("task11a-network-negative") == 4
    for index, marker in enumerate(markers):
        if marker == "task11a-network-negative":
            assert index > 0 and markers[index - 1] == "task11a-network-positive"

    escaped = b"\n".join(
        path.read_bytes()
        for path in (
            tmp_path / "kubectl.jsonl",
            tmp_path / "harness.stdout",
            tmp_path / "harness.stderr",
            tmp_path / "report.json",
        )
    )
    assert b"fake-openai-api-key" not in escaped
    assert b"fake-falkordb-password" not in escaped


def test_listener_fixture_is_explicitly_bound_to_the_run_namespace() -> None:
    listener = (
        REPOSITORY / "tests/acceptance/reference/task11a-listener-pod.yaml"
    ).read_text(encoding="utf-8")

    assert listener.count("namespace: REPLACE_TASK11A_NAMESPACE") == 2


def test_listener_service_selects_only_the_listener_pod() -> None:
    documents = list(
        yaml.safe_load_all(
            (
                REPOSITORY / "tests/acceptance/reference/task11a-listener-pod.yaml"
            ).read_text(encoding="utf-8")
        )
    )
    pod = next(document for document in documents if document["kind"] == "Pod")
    service = next(document for document in documents if document["kind"] == "Service")

    selector = service["spec"]["selector"]
    assert selector == {"app.kubernetes.io/name": "task11a-listener"}
    assert all(
        pod["metadata"]["labels"].get(key) == value for key, value in selector.items()
    )


@pytest.mark.parametrize(
    "fixture",
    (
        "task11a-listener-pod.yaml",
        "task11a-probe-pod.yaml",
    ),
)
def test_task11a_probe_commands_are_all_yaml_strings(fixture: str) -> None:
    documents = yaml.safe_load_all(
        (REPOSITORY / "tests/acceptance/reference" / fixture).read_text(
            encoding="utf-8"
        )
    )
    for document in documents:
        if document.get("kind") != "Pod":
            continue
        for container in document["spec"]["containers"]:
            for field in ("command",):
                if field in container:
                    assert all(isinstance(value, str) for value in container[field])
            for probe_name in ("readinessProbe", "livenessProbe", "startupProbe"):
                command = container.get(probe_name, {}).get("exec", {}).get("command")
                if command is not None:
                    assert all(isinstance(value, str) for value in command)


def test_fake_parses_listener_and_probe_objects_in_their_exact_namespaces(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0
    applied = [
        json.loads(line)
        for line in (tmp_path / "applied.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    temporary = {
        (item["kind"], item["name"]): item["namespace"]
        for item in applied
        if item["name"]
        in {
            "task11a-listener",
            "task11a-probe",
            "task11a-control",
            "task11a-control-to-cairn",
        }
    }
    assert temporary == {
        ("Pod", "task11a-listener"): "cairn-task11a-0123456-01",
        ("Service", "task11a-listener"): "cairn-task11a-0123456-01",
        ("Pod", "task11a-probe"): "cairn-egress",
        ("Pod", "task11a-control"): "cairn-task11a-0123456-01",
        ("NetworkPolicy", "task11a-control-to-cairn"): "cairn-task11a-0123456-01",
    }


def test_dns_observation_records_the_probe_command_actual_output() -> None:
    harness = HARNESS.read_text(encoding="utf-8")

    assert 'socket.getaddrinfo("api.openai.com", 443); print("resolved")' in harness


def test_dns_probe_actual_embedded_python_output_reaches_the_report(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    checks = {
        item["name"]: item
        for item in json.loads((tmp_path / "report.json").read_text())["checks"]
    }
    assert checks["dns"]["expected"] == {"result": "resolved"}
    assert checks["dns"]["observed"] == {"result": "resolved"}


def test_network_probes_stream_their_embedded_programs_to_kubernetes(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    probe_calls = [
        call
        for call in calls
        if "task11a-network-positive" in call or "task11a-network-negative" in call
    ]
    assert probe_calls
    assert all("-i" in call for call in probe_calls)


def test_probe_waits_for_running_and_public_egress_before_provider(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    probe_wait = next(
        call for call in calls if call[:1] == ["wait"] and "pod/task11a-probe" in call
    )
    assert "--for=jsonpath={.status.phase}=Running" in probe_wait
    assert "--for=condition=Ready=false" not in probe_wait
    public_probe = next(
        index
        for index, call in enumerate(calls)
        if "task11a-network-positive" in call and "api.openai.com" in call
    )
    provider = next(
        index
        for index, call in enumerate(calls)
        if " ingest http://cairn:8000 " in f" {' '.join(call)} "
    )
    assert public_probe < provider


def test_retrieval_delivery_binds_config_queue_and_completed_marker(
    tmp_path: Path,
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    composition = next(
        " ".join(call)
        for call in calls
        if "task11a-observe:retrieval-composition" in call
    )
    assert "yaml.safe_load" in composition
    assert "graphiti" in composition
    high_water = [
        index
        for index, call in enumerate(calls)
        if "sqlite_sequence" in " ".join(call) and "projection_outbox" in " ".join(call)
    ]
    provider = next(
        index
        for index, call in enumerate(calls)
        if " ingest http://cairn:8000 " in f" {' '.join(call)} "
    )
    completed_marker = next(
        index
        for index, call in enumerate(calls)
        if "GRAPH.RO_QUERY" in " ".join(call)
        and "cairn.fact.projected" in " ".join(call)
    )
    assert len(high_water) == 2
    assert high_water[0] < provider < high_water[1] < completed_marker


def test_network_denial_refuses_dns_resolution_failure(tmp_path: Path) -> None:
    harness = HARNESS.read_text(encoding="utf-8")
    assert "except socket.gaierror:" in harness
    assert 'print("resolution-failed")' in harness

    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="negative-dns-failure") == 1


def test_runbook_formats_the_gateway_grant_label_as_code() -> None:
    runbook = (
        REPOSITORY / "docs/runbooks/runbook-reference-task11a-posture.md"
    ).read_text(encoding="utf-8")

    assert "`cairn.example.invalid/instance` is a grant" in runbook


def test_runbook_names_gateway_prestate_recovery_prerequisite() -> None:
    runbook = (
        REPOSITORY / "docs/runbooks/runbook-reference-task11a-posture.md"
    ).read_text(encoding="utf-8")

    assert "restore the exact accepted Task 11 gateway pre-state" in runbook
    assert "before allocating a new Task 11a run ID" in runbook


def test_node_probe_owns_one_timeout_bounded_firewalld_rule(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "firewall.jsonl").read_text().splitlines()
    ]
    rule = (
        'rule family="ipv4" source address="10.244.0.0/16" '
        'destination address="192.168.50.50/32" '
        'port port="443" protocol="tcp" accept'
    )
    assert ["--state"] in calls
    assert ["--get-zone-of-interface=eth0"] in calls
    assert ["--zone=public", f"--add-rich-rule={rule}", "--timeout=300"] in calls
    assert ["--zone=public", f"--remove-rich-rule={rule}"] in calls
    add = calls.index(["--zone=public", f"--add-rich-rule={rule}", "--timeout=300"])
    remove = calls.index(["--zone=public", f"--remove-rich-rule={rule}"])
    assert (
        sum(
            add < index < remove
            and call == ["--zone=public", f"--query-rich-rule={rule}"]
            for index, call in enumerate(calls)
        )
        >= 2
    )
    assert not (tmp_path / "firewall-rule-active").exists()


def test_preflight_only_reads_firewalld_state(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("preflight", fixture="safe") == 0

    calls = [
        json.loads(line)
        for line in (tmp_path / "firewall.jsonl").read_text().splitlines()
    ]
    assert not any(
        any(
            argument.startswith("--add-rich-rule=")
            or argument.startswith("--remove-rich-rule=")
            for argument in call
        )
        for call in calls
    )


def test_failed_node_probe_removes_firewalld_rule_and_listener(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="node-positive-failure") == 1

    assert not (tmp_path / "firewall-rule-active").exists()
    listener_events = (tmp_path / "socat.log").read_text().splitlines()
    assert len(listener_events) == 2
    assert listener_events[0].startswith("started ")
    assert listener_events[1] == "stopped"


def test_node_pod_subnet_contained_by_cluster_cidr_is_accepted(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("preflight", fixture="safe") == 0


@pytest.mark.parametrize(
    "fixture",
    [
        "gateway-partial-failure",
        "bootstrap-apply-failure",
        "probe-apply-failure",
    ],
)
def test_partial_apply_failure_attempts_idempotent_cleanup_of_every_probe(
    tmp_path: Path, fixture: str
) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture=fixture) == 1
    calls = [
        json.loads(line)
        for line in (tmp_path / "kubectl.jsonl").read_text().splitlines()
    ]
    cleaned = {
        (call[1], call[call.index("-n") + 1], call[call.index("-n") + 2])
        for call in calls
        if call[:1] == ["delete"] and "--ignore-not-found" in call
    }
    expected_cleaned = {
        ("pod", "cairn-task11a-0123456-01", "task11a-bootstrap"),
        ("pod", "cairn-egress", "task11a-probe"),
        ("pod", "cairn-task11a-0123456-01", "task11a-control"),
        ("networkpolicy", "cairn-task11a-0123456-01", "task11a-control-to-cairn"),
        ("pod", "cairn-task11a-0123456-01", "task11a-listener"),
        ("service", "cairn-task11a-0123456-01", "task11a-listener"),
    }
    if fixture == "probe-apply-failure":
        expected_cleaned.remove(
            ("pod", "cairn-task11a-0123456-01", "task11a-bootstrap")
        )
    assert cleaned == expected_cleaned
    failed_path = tmp_path / "report.failed.json"
    failed = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["run_id"] == RUN_ID
    assert failed["phase"]
    assert failed["cleanup"]["attempted"] is True
    assert len(failed["cleanup"]["results"]) == 6
    assert failed["recovery_inventory"]["selected_pvs"]["query_succeeded"] is True
    assert {
        item["name"] for item in failed["recovery_inventory"]["selected_pvs"]["items"]
    } == {"cairn-local-pv3", "cairn-local-pv4"}
    assert failed["recovery_inventory"]["gateway_inventory"]["query_succeeded"] is True
    gateway_items = failed["recovery_inventory"]["gateway_inventory"]["items"]
    gateway_keys = sorted((item["kind"], item["name"]) for item in gateway_items)
    assert gateway_keys in (
        sorted((item["kind"], item["name"]) for item in GATEWAY_PRE_INVENTORY),
        sorted((item["kind"], item["name"]) for item in GATEWAY_POST_INVENTORY),
    )
    if len(gateway_items) == len(GATEWAY_POST_INVENTORY):
        assert sorted(
            gateway_items, key=lambda item: (item["kind"], item["name"])
        ) == sorted(
            GATEWAY_POST_INVENTORY,
            key=lambda item: (item["kind"], item["name"]),
        )


def test_failed_report_survives_unavailable_recovery_queries(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="gateway-recovery-query-failure") == 1

    failed = json.loads((tmp_path / "report.failed.json").read_text(encoding="utf-8"))
    recovery = failed["recovery_inventory"]
    assert recovery == {
        "namespace": {
            "query_succeeded": False,
            "name": NAMESPACE,
            "exists": None,
        },
        "selected_pvs": {"query_succeeded": False, "items": []},
        "gateway_inventory": {"query_succeeded": False, "items": []},
    }


def test_failed_run_id_refuses_rerun_and_cannot_become_success(tmp_path: Path) -> None:
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="bootstrap-apply-failure") == 1
        failed_before = (tmp_path / "report.failed.json").read_bytes()
        assert _run_fake_harness("run", fixture="safe") == 1

    assert (tmp_path / "report.failed.json").read_bytes() == failed_before
    assert not (tmp_path / "report.json").exists()


def test_generated_bootstrap_credential_is_absent_from_all_fake_artefacts(
    tmp_path: Path,
) -> None:
    token = b"cairn1.fake-bootstrap-secret-value"
    with _fake_harness_environment(tmp_path):
        assert _run_fake_harness("run", fixture="safe") == 0

    assert all(
        token not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
