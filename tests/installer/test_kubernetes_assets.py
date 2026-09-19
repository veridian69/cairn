from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from cairn_install.kubernetes_assets import asset_envelope, render_holder, render_site

ROOT = Path(__file__).parents[2]
KUBERNETES_NAMESPACE = "cairn-install-namespace"
INSTANCE_NAME = "cairn-alpha"
INSTANCE_ID = "11111111-1111-4111-8111-111111111111"
OWNER_LABELS = {
    "io.cairn.install.instance": INSTANCE_ID,
    "io.cairn.install.run": "22222222-2222-4222-8222-222222222222",
}


def _source(name: str) -> str:
    return (ROOT / "deploy" / "kustomize" / "rendered" / name).read_text(
        encoding="utf-8"
    )


def _documents(rendered: str) -> list[dict[str, Any]]:
    return [document for document in yaml.safe_load_all(rendered) if document]


def _named(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def _instance_label_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = (
            [value["app.kubernetes.io/instance"]]
            if "app.kubernetes.io/instance" in value
            else []
        )
        return values + [
            label for child in value.values() for label in _instance_label_values(child)
        ]
    if isinstance(value, list):
        return [label for child in value for label in _instance_label_values(child)]
    return []


def test_render_site_sets_instance_values_and_keeps_claims_after_statefulset_deletion() -> (
    None
):
    """Omitting any instance substitution could bind a site to another Cairn."""
    documents = _documents(
        render_site(
            _source("kubernetes.yaml"),
            namespace=KUBERNETES_NAMESPACE,
            instance_name=INSTANCE_NAME,
            instance_id=INSTANCE_ID,
            image="registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            storage_class="fast-rwop",
            semantic=False,
            owner_labels=OWNER_LABELS,
        )
    )

    assert {
        (document["kind"], document["metadata"]["name"]) for document in documents
    } == {
        ("ServiceAccount", "cairn"),
        ("ConfigMap", "cairn-config"),
        ("Service", "cairn"),
        ("StatefulSet", "cairn"),
        ("NetworkPolicy", "cairn-allow-dns-egress"),
        ("NetworkPolicy", "cairn-default-deny"),
    }
    assert {document["metadata"]["namespace"] for document in documents} == {
        KUBERNETES_NAMESPACE
    }
    assert all(
        document["metadata"]["labels"] | OWNER_LABELS == document["metadata"]["labels"]
        for document in documents
    )

    configuration = yaml.safe_load(
        _named(documents, "ConfigMap", "cairn-config")["data"]["config.yaml"]
    )
    assert configuration["instance_id"] == INSTANCE_ID

    cairn = _named(documents, "StatefulSet", "cairn")
    assert cairn["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert cairn["spec"]["template"]["spec"]["initContainers"][0]["image"] == (
        "registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert cairn["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == (
        "fast-rwop"
    )
    assert cairn["spec"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    assert (
        cairn["spec"]["selector"]["matchLabels"]["app.kubernetes.io/instance"]
        == INSTANCE_NAME
    )
    assert (
        _named(documents, "Service", "cairn")["spec"]["selector"][
            "app.kubernetes.io/instance"
        ]
        == INSTANCE_NAME
    )
    assert (
        _named(documents, "NetworkPolicy", "cairn-default-deny")["spec"]["podSelector"][
            "matchLabels"
        ]["app.kubernetes.io/instance"]
        == INSTANCE_NAME
    )
    assert set(_instance_label_values(documents)) == {INSTANCE_NAME}
    assert not any(document["kind"] == "Secret" for document in documents)


def test_attic_only_asset_envelope_makes_cairn_volume_writable() -> None:
    """The guided Attic-only pod must be able to initialise a fresh CSI volume."""
    output = asset_envelope(
        raw=_source("kubernetes.yaml"),
        namespace=KUBERNETES_NAMESPACE,
        instance_name=INSTANCE_NAME,
        instance_id=INSTANCE_ID,
        image="registry.example/cairn@sha256:" + "a" * 64,
        storage_class="fast-rwop",
        semantic=False,
        owner_labels=OWNER_LABELS,
        image_policy="Always",
    )

    cairn = _named(output["documents"], "StatefulSet", "cairn")
    assert cairn["spec"]["template"]["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "fsGroup": 65532,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert (
        output["holder_document"]["spec"]["securityContext"]
        == cairn["spec"]["template"]["spec"]["securityContext"]
    )


def test_render_site_keeps_the_shared_gateway_peer_and_semantic_inventory() -> None:
    """Rewriting the gateway peer would silently widen or break semantic egress."""
    source = _documents(_source("kubernetes-retrieval.yaml"))
    expected_gateway_peer = deepcopy(
        _named(source, "NetworkPolicy", "cairn-allow-gateway-egress")["spec"]["egress"][
            0
        ]["to"]
    )

    documents = _documents(
        render_site(
            _source("kubernetes-retrieval.yaml"),
            namespace=KUBERNETES_NAMESPACE,
            instance_name=INSTANCE_NAME,
            instance_id=INSTANCE_ID,
            image="registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            storage_class="fast-rwop",
            semantic=True,
            owner_labels=OWNER_LABELS,
        )
    )

    assert {
        (document["kind"], document["metadata"]["name"]) for document in documents
    } == {
        ("ServiceAccount", "cairn"),
        ("ConfigMap", "cairn-config"),
        ("Service", "cairn"),
        ("Service", "falkordb"),
        ("StatefulSet", "cairn"),
        ("StatefulSet", "falkordb"),
        ("NetworkPolicy", "cairn-allow-dns-egress"),
        ("NetworkPolicy", "cairn-allow-gateway-egress"),
        ("NetworkPolicy", "cairn-allow-index-egress"),
        ("NetworkPolicy", "cairn-default-deny"),
        ("NetworkPolicy", "falkordb-allow-cairn-ingress"),
        ("NetworkPolicy", "falkordb-default-deny"),
    }
    gateway = _named(documents, "NetworkPolicy", "cairn-allow-gateway-egress")
    assert gateway["spec"]["egress"][0]["to"] == expected_gateway_peer
    assert set(_instance_label_values(documents)) == {INSTANCE_NAME}
    cairn = _named(documents, "StatefulSet", "cairn")
    assert cairn["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert cairn["spec"]["template"]["spec"]["initContainers"][0]["image"] == (
        "registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert {
        stateful_set["metadata"]["name"]: stateful_set["spec"]["volumeClaimTemplates"][
            0
        ]["spec"]["storageClassName"]
        for stateful_set in documents
        if stateful_set["kind"] == "StatefulSet"
    } == {"cairn": "fast-rwop", "falkordb": "fast-rwop"}
    assert all(
        stateful_set["spec"]["persistentVolumeClaimRetentionPolicy"]
        == {"whenDeleted": "Retain", "whenScaled": "Retain"}
        for stateful_set in documents
        if stateful_set["kind"] == "StatefulSet"
    )


def test_local_falkordb_receipt_changes_only_the_falkordb_workload() -> None:
    falkordb_image = "registry.example/falkordb@sha256:" + "b" * 64
    output = asset_envelope(
        raw=_source("kubernetes-retrieval.yaml"),
        namespace=KUBERNETES_NAMESPACE,
        instance_name=INSTANCE_NAME,
        instance_id=INSTANCE_ID,
        image="registry.example/cairn@sha256:" + "a" * 64,
        storage_class="fast-rwop",
        semantic=True,
        owner_labels=OWNER_LABELS,
        image_policy="Always",
        falkordb_receipt={
            "schema_version": 1,
            "image": falkordb_image,
            "archive_sha256": "c" * 64,
            "nodes": [
                {"name": "worker-a", "uid": "uid-a"},
                {"name": "worker-b", "uid": "uid-b"},
            ],
        },
    )
    documents = output["documents"]
    cairn = _named(documents, "StatefulSet", "cairn")["spec"]["template"]["spec"]
    falkordb = _named(documents, "StatefulSet", "falkordb")["spec"]["template"]["spec"]

    assert cairn["containers"][0]["imagePullPolicy"] == "Always"
    assert "affinity" not in cairn
    assert falkordb["containers"][0]["image"] == falkordb_image
    assert falkordb["containers"][0]["imagePullPolicy"] == "Never"
    assert falkordb["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"] == [
        {
            "matchFields": [
                {
                    "key": "metadata.name",
                    "operator": "In",
                    "values": ["worker-a"],
                }
            ]
        },
        {
            "matchFields": [
                {
                    "key": "metadata.name",
                    "operator": "In",
                    "values": ["worker-b"],
                }
            ]
        },
    ]


def test_render_site_refuses_an_unrecognised_rendered_resource() -> None:
    """Accepting a new overlay object without review would exceed the installer boundary."""
    documents = _documents(_source("kubernetes.yaml"))
    documents.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "unreviewed"},
            "data": {"value": "unexpected"},
        }
    )

    with pytest.raises(ValueError, match="unexpected rendered Kubernetes shape"):
        render_site(
            yaml.safe_dump_all(documents, sort_keys=False),
            namespace=KUBERNETES_NAMESPACE,
            instance_name=INSTANCE_NAME,
            instance_id=INSTANCE_ID,
            image="registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            storage_class="fast-rwop",
            semantic=False,
            owner_labels=OWNER_LABELS,
        )


def test_render_holder_derives_a_safe_waiting_pod_from_the_rendered_cairn_template() -> (
    None
):
    """Dropping a template field would make lifecycle commands run unlike Cairn."""
    site = render_site(
        _source("kubernetes-retrieval.yaml"),
        namespace=KUBERNETES_NAMESPACE,
        instance_name=INSTANCE_NAME,
        instance_id=INSTANCE_ID,
        image="registry.example/cairn@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        storage_class="fast-rwop",
        semantic=True,
        owner_labels=OWNER_LABELS,
    )
    cairn = _named(_documents(site), "StatefulSet", "cairn")
    source_spec = cairn["spec"]["template"]["spec"]

    holder = yaml.safe_load(
        render_holder(
            site,
            namespace=KUBERNETES_NAMESPACE,
            owner_labels=OWNER_LABELS,
        )
    )
    container = holder["spec"]["containers"][0]

    assert holder["apiVersion"] == "v1"
    assert holder["kind"] == "Pod"
    assert holder["metadata"] == {
        "name": "cairn-bootstrap",
        "namespace": KUBERNETES_NAMESPACE,
        "labels": cairn["spec"]["template"]["metadata"]["labels"] | OWNER_LABELS,
    }
    assert holder["spec"]["serviceAccountName"] == source_spec["serviceAccountName"]
    assert holder["spec"]["securityContext"] == source_spec["securityContext"]
    assert holder["spec"]["restartPolicy"] == "Never"
    assert container["name"] == "cairn-bootstrap"
    assert container["image"] == source_spec["containers"][0]["image"]
    assert (
        container["securityContext"] == source_spec["containers"][0]["securityContext"]
    )
    assert container["env"] == source_spec["containers"][0]["env"]
    assert container["volumeMounts"] == source_spec["containers"][0]["volumeMounts"]
    assert container["command"] == ["sleep", "infinity"]
    assert "args" not in container
    assert (
        not {
            "livenessProbe",
            "readinessProbe",
            "startupProbe",
        }
        & container.keys()
    )
    assert "initContainers" not in holder["spec"]
    assert holder["spec"]["volumes"] == source_spec["volumes"] + [
        {
            "name": "data",
            "persistentVolumeClaim": {"claimName": "data-cairn-0"},
        }
    ]
    defined_volume_names = {volume["name"] for volume in holder["spec"]["volumes"]}
    assert {
        mount["name"] for mount in container["volumeMounts"]
    } <= defined_volume_names


@pytest.mark.parametrize("semantic", [False, True])
def test_locked_helper_returns_consistent_site_holder_and_documents(
    semantic: bool,
) -> None:
    import json
    import subprocess
    import sys

    request = {
        "raw": _source("kubernetes-retrieval.yaml" if semantic else "kubernetes.yaml"),
        "namespace": KUBERNETES_NAMESPACE,
        "instance_name": INSTANCE_NAME,
        "instance_id": INSTANCE_ID,
        "image": "registry.example/cairn@sha256:" + "a" * 64,
        "storage_class": "fast-rwop",
        "semantic": semantic,
        "owner_labels": OWNER_LABELS,
        "image_policy": "IfNotPresent",
    }
    result = subprocess.run(
        [sys.executable, "-m", "cairn_install.kubernetes_assets"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout, "The locked helper must emit its JSON contract"
    output = json.loads(result.stdout)
    assert output["documents"] == list(yaml.safe_load_all(output["site"]))
    assert output["holder_document"] == yaml.safe_load(output["holder"])
    assert output["holder_document"]["metadata"]["name"] == "cairn-bootstrap"
    pod = output["holder_document"]["spec"]
    assert pod["containers"][0]["imagePullPolicy"] == "IfNotPresent"
    assert pod["nodeSelector"] == {
        "kubernetes.io/os": "linux",
        "kubernetes.io/arch": "amd64",
    }
    # Existing protected text, including harmless presentation, is authoritative.
    request.pop("raw")
    request["site"] = output["site"] + "\n# retained presentation\n"
    request["holder"] = output["holder"]
    result = subprocess.run(
        [sys.executable, "-m", "cairn_install.kubernetes_assets"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    resumed = json.loads(result.stdout)
    assert resumed["site"] == request["site"]
    assert resumed["holder"] == request["holder"]
    assert resumed["documents"] == output["documents"]


def test_locked_helper_rejects_malformed_request_without_output() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "cairn_install.kubernetes_assets"],
        input='{"unexpected": true}',
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Invalid Kubernetes asset request" in result.stderr
