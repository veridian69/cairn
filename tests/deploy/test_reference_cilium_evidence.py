"""Read-only Cilium supplement evidence for the completed Task 11 run."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from scripts.reference_acceptance_state import (  # noqa: E402
    HarnessRefusal,
    build_cilium_supplement,
    cilium_image_evidence,
)

SUPPLEMENT = REPOSITORY / "scripts" / "reference-cilium-evidence"
TARGET_REVISION = "0de93bd1492beda56143bc45b155fccf7cd47caf"
COLLECTOR_REVISION = "c" * 40
RUN_ID = "task11-0de93bd1492b-01"
AGENT_DIGEST = "sha256:0df5b2750b64c49843aba1d649e9eaf61467cb0645ad3171db6f6962c095ac92"
OPERATOR_DIGEST = (
    "sha256:0db4ca4e06969d8904ee036617795d0e9c3228cf7b8d902ba74fc2bb98d2d665"
)
ENVOY_DIGEST = "sha256:767101fb8a5e38f055778cb43b7aa8eed80450b37f8121effac3d9de9e06dc99"
EXPECTED_IMAGES = {
    "agent": f"quay.io/cilium/cilium:v1.19.6@{AGENT_DIGEST}",
    "operator": f"quay.io/cilium/operator-generic:v1.19.6@{OPERATOR_DIGEST}",
    "envoy": (
        "quay.io/cilium/cilium-envoy:"
        f"v1.36.9-1782267392-edeb3f2af56c37c407efa1f63f0b32f595399bbc@{ENVOY_DIGEST}"
    ),
}


def _workload(
    kind: str, name: str, container: str, image: str, label: str
) -> dict[str, Any]:
    status: dict[str, Any] = {"observedGeneration": 7}
    spec: dict[str, Any] = {
        "selector": {"matchLabels": {"app": label}},
        "template": {
            "metadata": {"labels": {"app": label}},
            "spec": {"containers": [{"name": container, "image": image}]},
        },
    }
    if kind == "DaemonSet":
        status.update(
            {
                "desiredNumberScheduled": 1,
                "currentNumberScheduled": 1,
                "updatedNumberScheduled": 1,
                "numberAvailable": 1,
                "numberReady": 1,
            }
        )
    else:
        spec["replicas"] = 1
        status.update(
            {
                "replicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "readyReplicas": 1,
            }
        )
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            "uid": f"uid-{name}",
            "generation": 7,
        },
        "spec": spec,
        "status": status,
    }


def _owner(
    kind: str, name: str, uid: str, *, controller: bool = True
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "name": name,
        "uid": uid,
        "controller": controller,
    }


def _pod(
    name: str,
    container: str,
    label: str,
    digest: str,
    owner: dict[str, Any],
    extra_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            "uid": f"uid-{name}",
            "labels": {"app": label, **(extra_labels or {})},
            "ownerReferences": [owner],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": container,
                    # The live failure mode: this field may be digest-only.
                    "image": digest,
                    "imageID": f"quay.io/cilium/runtime@{digest}",
                    "ready": True,
                }
            ],
        },
    }


def _operator_replicaset(
    name: str, uid: str, template_hash: str, *, replicas: int
) -> dict[str, Any]:
    labels = {"app": "operator", "pod-template-hash": template_hash}
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            "uid": uid,
            "generation": 3,
            "labels": dict(labels),
            "ownerReferences": [
                _owner("Deployment", "cilium-operator", "uid-cilium-operator")
            ],
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": dict(labels)},
            "template": {"metadata": {"labels": dict(labels)}},
        },
        "status": {
            "observedGeneration": 3,
            "replicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "fullyLabeledReplicas": replicas,
        },
    }


@pytest.fixture
def live_inventory() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    workloads = [
        _workload(
            "DaemonSet", "cilium", "cilium-agent", EXPECTED_IMAGES["agent"], "agent"
        ),
        _workload(
            "Deployment",
            "cilium-operator",
            "cilium-operator",
            EXPECTED_IMAGES["operator"],
            "operator",
        ),
        _workload(
            "DaemonSet",
            "cilium-envoy",
            "cilium-envoy",
            EXPECTED_IMAGES["envoy"],
            "envoy",
        ),
    ]
    replicasets = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            _operator_replicaset(
                "cilium-operator-7d8f9",
                "uid-cilium-operator-rs",
                "7d8f9c6b5d",
                replicas=1,
            ),
            _operator_replicaset(
                "cilium-operator-64c74",
                "uid-cilium-operator-rs-old",
                "64c74b5998",
                replicas=0,
            ),
        ],
    }
    pods = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            _pod(
                "cilium-node",
                "cilium-agent",
                "agent",
                AGENT_DIGEST,
                _owner("DaemonSet", "cilium", "uid-cilium"),
            ),
            _pod(
                "cilium-operator-a",
                "cilium-operator",
                "operator",
                OPERATOR_DIGEST,
                _owner("ReplicaSet", "cilium-operator-7d8f9", "uid-cilium-operator-rs"),
                {"pod-template-hash": "7d8f9c6b5d"},
            ),
            _pod(
                "cilium-envoy-node",
                "cilium-envoy",
                "envoy",
                ENVOY_DIGEST,
                _owner("DaemonSet", "cilium-envoy", "uid-cilium-envoy"),
            ),
        ],
    }
    return workloads, replicasets, pods


def _primary() -> bytes:
    return (
        json.dumps(
            {
                "status": "passed",
                "stage": "complete",
                "run_id": RUN_ID,
                "repository_revision": TARGET_REVISION,
                "image_revision": TARGET_REVISION,
                "cilium_chart": "cilium-1.19.6",
                "cilium_images": [],
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _helm() -> list[dict[str, Any]]:
    return [
        {
            "name": "cilium",
            "namespace": "kube-system",
            "revision": "1",
            "status": "deployed",
            "chart": "cilium-1.19.6",
            "app_version": "1.19.6",
        }
    ]


def test_digest_only_status_image_still_records_exact_three_identities(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
) -> None:
    workloads, replicasets, pods = live_inventory
    evidence = cilium_image_evidence(workloads, replicasets, pods, EXPECTED_IMAGES)
    assert [
        (item["workload_kind"], item["workload_name"], item["container_name"])
        for item in evidence
    ] == [
        ("DaemonSet", "cilium", "cilium-agent"),
        ("Deployment", "cilium-operator", "cilium-operator"),
        ("DaemonSet", "cilium-envoy", "cilium-envoy"),
    ]
    assert [item["runtime_image_digest"] for item in evidence] == [
        AGENT_DIGEST,
        OPERATOR_DIGEST,
        ENVOY_DIGEST,
    ]
    assert all("@sha256:" in item["spec_image"] for item in evidence)
    assert all(item["namespace"] == "kube-system" for item in evidence)
    assert all(item["desired_pods"] == 1 for item in evidence)
    assert [item["pod_uids"] for item in evidence] == [
        ["uid-cilium-node"],
        ["uid-cilium-operator-a"],
        ["uid-cilium-envoy-node"],
    ]
    assert evidence[0]["controller_chain"] == [
        {"kind": "DaemonSet", "name": "cilium", "uid": "uid-cilium"},
        {"kind": "Pod", "name": "cilium-node", "uid": "uid-cilium-node"},
    ]
    assert evidence[1]["controller_chain"] == [
        {
            "kind": "Deployment",
            "name": "cilium-operator",
            "uid": "uid-cilium-operator",
        },
        {
            "kind": "ReplicaSet",
            "name": "cilium-operator-7d8f9",
            "uid": "uid-cilium-operator-rs",
        },
        {
            "kind": "Pod",
            "name": "cilium-operator-a",
            "uid": "uid-cilium-operator-a",
        },
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda workloads, pods: workloads.pop(), "missing Cilium workload"),
        (
            lambda workloads, pods: workloads.append(workloads[0]),
            "duplicate Cilium workload",
        ),
        (
            lambda workloads, pods: workloads[1]["metadata"].update(uid="uid-cilium"),
            "ambiguous UID identity",
        ),
        (
            lambda workloads, pods: workloads[0]["spec"]["template"]["spec"][
                "containers"
            ][0].update(image="quay.io/cilium/cilium:v1.19.6"),
            "mutable or non-digest",
        ),
        (
            lambda workloads, pods: pods["items"][0]["status"]["containerStatuses"][
                0
            ].update(imageID="quay.io/cilium/cilium:v1.19.6"),
            "runtime image ID has no OCI digest",
        ),
        (
            lambda workloads, pods: pods["items"][0]["status"]["containerStatuses"][
                0
            ].update(imageID=f"quay.io/cilium/cilium@{OPERATOR_DIGEST}"),
            "does not match",
        ),
        (
            lambda workloads, pods: pods["items"].append(pods["items"][0]),
            "ambiguous Cilium pods",
        ),
    ],
)
def test_image_inventory_refuses_incomplete_or_ambiguous_identity(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
    mutation: Any,
    message: str,
) -> None:
    workloads, replicasets, pods = live_inventory
    mutation(workloads, pods)
    with pytest.raises(HarnessRefusal, match=message):
        cilium_image_evidence(workloads, replicasets, pods, EXPECTED_IMAGES)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"].pop(
                "ownerReferences"
            ),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(uid="wrong"),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(kind="Deployment"),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(name="wrong"),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(controller=False),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"].update(
                namespace="other"
            ),
            "wrong namespace or is deleting",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"].update(
                deletionTimestamp="2026-08-17T18:01:00Z"
            ),
            "wrong namespace or is deleting",
        ),
        (
            lambda workloads, replicasets, pods: workloads[0]["metadata"].update(
                deletionTimestamp="2026-08-17T18:01:00Z"
            ),
            "wrong namespace or is deleting",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0][
                "metadata"
            ].pop("ownerReferences"),
            "ReplicaSet controller owner",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(uid="wrong"),
            "ReplicaSet controller owner",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(name="wrong"),
            "ReplicaSet controller owner",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(kind="DaemonSet"),
            "ReplicaSet controller owner",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["metadata"][
                "ownerReferences"
            ][0].update(controller=False),
            "ReplicaSet controller owner",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0][
                "metadata"
            ].update(namespace="other"),
            "ReplicaSet.*wrong namespace or is deleting",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0][
                "metadata"
            ].update(deletionTimestamp="2026-08-17T18:01:00Z"),
            "ReplicaSet.*wrong namespace or is deleting",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0][
                "status"
            ].update(readyReplicas=0),
            "current ReplicaSet",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0][
                "status"
            ].update(observedGeneration=2),
            "generation is not observed",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ]["matchLabels"].pop("app"),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ]["matchLabels"].update(app="changed"),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ]["matchLabels"].update(unsafe="addition"),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ]["matchLabels"].pop("pod-template-hash"),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ]["matchLabels"].update(**{"pod-template-hash": "BAD_HASH"}),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["metadata"][
                "labels"
            ].update(**{"pod-template-hash": "wronghash"}),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "template"
            ]["metadata"]["labels"].update(**{"pod-template-hash": "wronghash"}),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][0]["spec"][
                "selector"
            ].update(matchExpressions=[{"key": "unsafe", "operator": "Exists"}]),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][1]["metadata"][
                "labels"
            ].update(**{"pod-template-hash": "wronghash"}),
            "does not match its ReplicaSet selector",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"][1]["spec"][
                "selector"
            ]["matchLabels"].update(unsafe="old-addition"),
            "selector relationship",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][1]["metadata"][
                "ownerReferences"
            ][0].update(name="stale-name"),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][1]["metadata"][
                "ownerReferences"
            ][0].update(uid="stale-rs"),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][1]["metadata"][
                "ownerReferences"
            ][0].update(controller=False),
            "controller owner",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][1]["status"][
                "conditions"
            ][0].update(status="False"),
            "not uniquely ready",
        ),
        (
            lambda workloads, replicasets, pods: workloads[1]["status"].update(
                readyReplicas=0
            ),
            "not fully current and ready",
        ),
        (
            lambda workloads, replicasets, pods: replicasets["items"].append(
                {
                    **replicasets["items"][0],
                    "metadata": {
                        **replicasets["items"][0]["metadata"],
                        "name": "cilium-operator-duplicate",
                        "uid": "uid-cilium-operator-rs-duplicate",
                    },
                }
            ),
            "current ReplicaSet",
        ),
        (
            lambda workloads, replicasets, pods: pods["items"][0]["metadata"][
                "ownerReferences"
            ].append(_owner("DaemonSet", "cilium", "uid-cilium")),
            "controller owner",
        ),
    ],
)
def test_image_inventory_refuses_non_current_controller_chain(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
    mutation: Any,
    message: str,
) -> None:
    workloads, replicasets, pods = live_inventory
    mutation(workloads, replicasets, pods)
    with pytest.raises(HarnessRefusal, match=message):
        cilium_image_evidence(workloads, replicasets, pods, EXPECTED_IMAGES)


def test_supplement_is_bound_to_primary_chart_target_and_collector(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
) -> None:
    primary = _primary()
    workloads, replicasets, pods = live_inventory
    supplement = build_cilium_supplement(
        primary,
        primary_sha256=hashlib.sha256(primary).hexdigest(),
        run_id=RUN_ID,
        target_revision=TARGET_REVISION,
        collector_revision=COLLECTOR_REVISION,
        helm_releases=_helm(),
        workloads=workloads,
        replicasets=replicasets,
        pods=pods,
        expected_chart="cilium-1.19.6",
        expected_images=EXPECTED_IMAGES,
        collected_at="2026-08-17T18:00:00Z",
    )
    assert supplement["status"] == "passed"
    assert supplement["primary_report_sha256"] == hashlib.sha256(primary).hexdigest()
    assert supplement["run_id"] == RUN_ID
    assert supplement["target_revision"] == TARGET_REVISION
    assert supplement["collector_revision"] == COLLECTOR_REVISION
    assert supplement["chart"] == {
        "release_name": "cilium",
        "namespace": "kube-system",
        "revision": "1",
        "status": "deployed",
        "chart": "cilium-1.19.6",
        "app_version": "1.19.6",
    }
    assert len(supplement["images"]) == 3


@pytest.mark.parametrize(
    ("primary_change", "argument_change", "message"),
    [
        ({"status": "failed"}, {}, "primary report is not passed/complete"),
        ({"stage": "recovery"}, {}, "primary report is not passed/complete"),
        ({"run_id": "other"}, {}, "primary report run ID mismatch"),
        ({"repository_revision": "d" * 40}, {}, "primary report revision mismatch"),
        ({"image_revision": "d" * 40}, {}, "primary image revision mismatch"),
        ({"cilium_chart": "cilium-1.19.5"}, {}, "primary Cilium chart mismatch"),
        (
            {"cilium_images": [{"unreviewed": True}]},
            {},
            "primary Cilium gap is not empty",
        ),
        ({}, {"primary_sha256": "f" * 64}, "primary report SHA-256 mismatch"),
    ],
)
def test_supplement_refuses_wrong_primary_identity(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
    primary_change: dict[str, Any],
    argument_change: dict[str, str],
    message: str,
) -> None:
    primary_document = json.loads(_primary())
    primary_document.update(primary_change)
    primary = (json.dumps(primary_document, sort_keys=True) + "\n").encode()
    workloads, replicasets, pods = live_inventory
    arguments = {
        "primary_sha256": hashlib.sha256(primary).hexdigest(),
        **argument_change,
    }
    with pytest.raises(HarnessRefusal, match=message):
        build_cilium_supplement(
            primary,
            **arguments,
            run_id=RUN_ID,
            target_revision=TARGET_REVISION,
            collector_revision=COLLECTOR_REVISION,
            helm_releases=_helm(),
            workloads=workloads,
            replicasets=replicasets,
            pods=pods,
            expected_chart="cilium-1.19.6",
            expected_images=EXPECTED_IMAGES,
            collected_at="2026-08-17T18:00:00Z",
        )


@pytest.mark.parametrize(
    ("releases", "message"),
    [
        ([], "missing or ambiguous"),
        (_helm() + _helm(), "missing or ambiguous"),
        ([{**_helm()[0], "chart": "cilium-1.19.5"}], "malformed or mismatched"),
        ([{**_helm()[0], "status": "failed"}], "malformed or mismatched"),
    ],
)
def test_supplement_refuses_missing_duplicate_or_mismatched_chart(
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
    releases: list[dict[str, Any]],
    message: str,
) -> None:
    primary = _primary()
    workloads, replicasets, pods = live_inventory
    with pytest.raises(HarnessRefusal, match=message):
        build_cilium_supplement(
            primary,
            primary_sha256=hashlib.sha256(primary).hexdigest(),
            run_id=RUN_ID,
            target_revision=TARGET_REVISION,
            collector_revision=COLLECTOR_REVISION,
            helm_releases=releases,
            workloads=workloads,
            replicasets=replicasets,
            pods=pods,
            expected_chart="cilium-1.19.6",
            expected_images=EXPECTED_IMAGES,
            collected_at="2026-08-17T18:00:00Z",
        )


def test_read_only_cli_uses_exact_kubectl_and_helm_surface_and_refuses_overwrite(
    tmp_path: Path,
    live_inventory: tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]],
) -> None:
    workloads, replicasets, pods = live_inventory
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name, payload in zip(("agent", "operator", "envoy"), workloads, strict=True):
        (fixtures / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    (fixtures / "pods.json").write_text(json.dumps(pods), encoding="utf-8")
    (fixtures / "replicasets.json").write_text(
        json.dumps(replicasets), encoding="utf-8"
    )
    (fixtures / "helm.json").write_text(json.dumps(_helm()), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.jsonl"
    fake_kubectl = fake_bin / "kubectl"
    fake_kubectl.write_text(
        """#!/bin/sh
printf '%s\n' \"$*\" >>\"$FAKE_COMMAND_LOG\"
[ \"${FAKE_KUBECTL_MALFORMED:-0}\" != 1 ] || { printf '{'; exit 0; }
case \"$*\" in
  'get daemonset cilium --namespace kube-system --output json') file=agent.json ;;
  'get deployment cilium-operator --namespace kube-system --output json') file=operator.json ;;
  'get daemonset cilium-envoy --namespace kube-system --output json') file=envoy.json ;;
  'get replicasets --namespace kube-system --output json') file=replicasets.json ;;
  'get pods --namespace kube-system --output json') file=pods.json ;;
  *) exit 64 ;;
esac
exec /bin/cat \"$FAKE_FIXTURES/$file\"
""",
        encoding="utf-8",
    )
    fake_helm = fake_bin / "helm"
    fake_helm.write_text(
        """#!/bin/sh
printf 'helm %s\n' \"$*\" >>\"$FAKE_COMMAND_LOG\"
[ \"${FAKE_HELM_DIAGNOSTIC:-0}\" != 1 ] || { printf 'warning\n' >&2; exit 0; }
[ \"$*\" = 'list --namespace kube-system --output json' ] || exit 64
exec /bin/cat \"$FAKE_FIXTURES/helm.json\"
""",
        encoding="utf-8",
    )
    for executable in (fake_kubectl, fake_helm):
        executable.chmod(0o755)
    collector = tmp_path / "collector"
    (collector / "scripts").mkdir(parents=True)
    (collector / "deploy").mkdir()
    for relative in (
        Path(".gitignore"),
        Path("scripts/reference-cilium-evidence"),
        Path("scripts/reference_acceptance_state.py"),
        Path("deploy/images.lock"),
    ):
        shutil.copy2(REPOSITORY / relative, collector / relative)
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=collector, check=True)
    subprocess.run(["/usr/bin/git", "add", "."], cwd=collector, check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.name=Val",
            "-c",
            "user.email=reviewer@example.invalid",
            "commit",
            "-qm",
            "collector fixture",
        ],
        cwd=collector,
        check=True,
    )
    collector_revision = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=collector,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    primary = tmp_path / "primary.json"
    primary.write_bytes(_primary())
    output = tmp_path / "supplement.json"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_FIXTURES": str(fixtures),
    }
    command = [
        str(collector / "scripts" / "reference-cilium-evidence"),
        "--primary-report",
        str(primary),
        "--primary-sha256",
        hashlib.sha256(_primary()).hexdigest(),
        "--run-id",
        RUN_ID,
        "--target-revision",
        TARGET_REVISION,
        "--output",
        str(output),
    ]
    result = subprocess.run(
        command,
        cwd=collector,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Cilium supplement written to {output}\n"
    assert result.stderr == ""
    supplement = json.loads(output.read_bytes())
    assert supplement["collector_revision"] == collector_revision
    assert len(supplement["images"]) == 3
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "helm list --namespace kube-system --output json",
        "get daemonset cilium --namespace kube-system --output json",
        "get deployment cilium-operator --namespace kube-system --output json",
        "get daemonset cilium-envoy --namespace kube-system --output json",
        "get replicasets --namespace kube-system --output json",
        "get pods --namespace kube-system --output json",
    ]
    before = output.read_bytes()
    repeated = subprocess.run(
        command,
        cwd=collector,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode == 2
    assert "already exists" in repeated.stderr
    assert output.read_bytes() == before

    diagnostic_output = tmp_path / "diagnostic.json"
    diagnostic_command = [*command[:-1], str(diagnostic_output)]
    diagnostic = subprocess.run(
        diagnostic_command,
        cwd=collector,
        env={**environment, "FAKE_HELM_DIAGNOSTIC": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert diagnostic.returncode == 2
    assert "failed or emitted diagnostics: helm" in diagnostic.stderr
    assert not diagnostic_output.exists()

    malformed_output = tmp_path / "malformed.json"
    malformed_command = [*command[:-1], str(malformed_output)]
    malformed = subprocess.run(
        malformed_command,
        cwd=collector,
        env={**environment, "FAKE_KUBECTL_MALFORMED": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert malformed.returncode == 2
    assert "returned malformed JSON: kubectl" in malformed.stderr
    assert not malformed_output.exists()

    commands_before_untracked = command_log.read_bytes()
    (collector / "scripts" / "json.py").write_text(
        "raise RuntimeError('must never import')\n", encoding="utf-8"
    )
    untracked_output = tmp_path / "untracked.json"
    untracked_command = [*command[:-1], str(untracked_output)]
    untracked = subprocess.run(
        untracked_command,
        cwd=collector,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert untracked.returncode == 2
    assert "not clean, including untracked files" in untracked.stderr
    assert "must never import" not in untracked.stderr
    assert not untracked_output.exists()
    assert command_log.read_bytes() == commands_before_untracked

    (collector / "scripts" / "json.py").unlink()
    shadow_source = tmp_path / "shadow_json.py"
    shadow_source.write_text(
        "raise RuntimeError('ignored shadow must never import')\n", encoding="utf-8"
    )
    py_compile.compile(
        str(shadow_source),
        cfile=str(collector / "scripts" / "json.pyc"),
        doraise=True,
    )
    assert (
        subprocess.run(
            [
                "/usr/bin/git",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=collector,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == ""
    )
    ignored_output = tmp_path / "ignored.json"
    ignored_command = [*command[:-1], str(ignored_output)]
    ignored = subprocess.run(
        ignored_command,
        cwd=collector,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 2
    assert "ignored files under scripts" in ignored.stderr
    assert "ignored shadow must never import" not in ignored.stderr
    assert not ignored_output.exists()
    assert command_log.read_bytes() == commands_before_untracked
