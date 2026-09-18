"""Strict, secret-free rendering of Cairn's published Kubernetes overlays."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from hashlib import sha256
from typing import Any

_INSTANCE_LABEL = "app.kubernetes.io/instance"
_INSTANCE_PLACEHOLDER = "REPLACE_WITH_PER_INSTANCE_UUID"
_RETAIN_PVCS = {"whenDeleted": "Retain", "whenScaled": "Retain"}
_SOURCE_DIGESTS = {
    False: "8e99fe9e92723a38eb9d4615b15aeac7dc10e45ada86525c1e1737891143e679",
    True: "ddbd719b01ffee28f0b2111be3255f4ed0ad6c5cecef7f024d5a2b7e870791bf",
}
_INVENTORIES = {
    False: {
        ("ServiceAccount", "cairn"),
        ("ConfigMap", "cairn-config"),
        ("Service", "cairn"),
        ("StatefulSet", "cairn"),
        ("NetworkPolicy", "cairn-allow-dns-egress"),
        ("NetworkPolicy", "cairn-default-deny"),
    },
    True: {
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
    },
}


def render_site(
    raw: str,
    *,
    namespace: str,
    instance_name: str,
    instance_id: str,
    image: str,
    storage_class: str,
    semantic: bool,
    owner_labels: dict[str, str],
) -> str:
    """Render exactly one known overlay with only documented instance changes."""
    import yaml

    if sha256(raw.encode()).hexdigest() != _SOURCE_DIGESTS[semantic]:
        raise ValueError("unexpected rendered Kubernetes shape")
    documents = _load_documents(raw)
    if _inventory(documents) != _INVENTORIES[semantic]:
        raise ValueError("unexpected rendered Kubernetes shape")

    for document in documents:
        metadata = _mapping(document, "metadata")
        labels = _mapping(metadata, "labels")
        metadata["namespace"] = namespace
        labels.update(owner_labels)
        _replace_instance_labels(document, instance_name)
        _label_nested_metadata(document, owner_labels)

    configuration = _named(documents, "ConfigMap", "cairn-config")
    data = _mapping(configuration, "data")
    config = data.get("config.yaml")
    if not isinstance(config, str) or config.count(_INSTANCE_PLACEHOLDER) != 1:
        raise ValueError("unexpected rendered Kubernetes shape")
    data["config.yaml"] = config.replace(_INSTANCE_PLACEHOLDER, instance_id)

    for stateful_set in _by_kind(documents, "StatefulSet"):
        spec = _mapping(stateful_set, "spec")
        template = _mapping(spec, "template")
        pod_spec = _mapping(template, "spec")
        if _mapping(stateful_set, "metadata").get("name") == "cairn":
            containers = _list(pod_spec, "containers")
            cairn_container = _container(containers, "cairn")
            cairn_container["image"] = image
            init_containers = _list(pod_spec, "initContainers")
            _container(init_containers, "migrate")["image"] = image
        claims = _list(spec, "volumeClaimTemplates")
        if len(claims) != 1:
            raise ValueError("unexpected rendered Kubernetes shape")
        claim_spec = _mapping(claims[0], "spec")
        if claim_spec.get("storageClassName") != "cairn-local":
            raise ValueError("unexpected rendered Kubernetes shape")
        claim_spec["storageClassName"] = storage_class
        spec["persistentVolumeClaimRetentionPolicy"] = deepcopy(_RETAIN_PVCS)

    if not semantic and len(_by_kind(documents, "StatefulSet")) != 1:
        raise ValueError("unexpected rendered Kubernetes shape")
    if any(document.get("kind") == "Secret" for document in documents):
        raise ValueError("unexpected rendered Kubernetes shape")
    return yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)


def render_holder(site: str, *, namespace: str, owner_labels: dict[str, str]) -> str:
    """Derive the exclusive lifecycle Pod from the rendered Cairn template."""
    import yaml

    documents = _load_documents(site)
    cairn = _named(documents, "StatefulSet", "cairn")
    metadata = _mapping(cairn, "metadata")
    if metadata.get("namespace") != namespace:
        raise ValueError("unexpected rendered Kubernetes shape")
    stateful_spec = _mapping(cairn, "spec")
    template = _mapping(stateful_spec, "template")
    template_metadata = _mapping(template, "metadata")
    template_labels = _mapping(template_metadata, "labels")
    pod_spec = deepcopy(_mapping(template, "spec"))
    containers = _list(pod_spec, "containers")
    source_container = deepcopy(_container(containers, "cairn"))
    mounts = _list(source_container, "volumeMounts")
    if not any(
        mount.get("name") == "data" for mount in mounts if isinstance(mount, dict)
    ):
        raise ValueError("unexpected rendered Kubernetes shape")
    claims = _list(stateful_spec, "volumeClaimTemplates")
    if len(claims) != 1 or _mapping(claims[0], "metadata").get("name") != "data":
        raise ValueError("unexpected rendered Kubernetes shape")

    source_container["name"] = "cairn-bootstrap"
    source_container["command"] = ["sleep", "infinity"]
    for field in ("args", "livenessProbe", "readinessProbe", "startupProbe"):
        source_container.pop(field, None)
    pod_spec["containers"] = [source_container]
    pod_spec.pop("initContainers", None)
    pod_spec["restartPolicy"] = "Never"
    volumes = _list(pod_spec, "volumes")
    # A lifecycle holder never hosts Garden or receives its data/TLS material.
    required_mounts = {mount["name"] for mount in mounts}
    volumes[:] = [volume for volume in volumes if volume.get("name") in required_mounts]
    template_labels = dict(template_labels)
    template_labels.pop("cairn.example.invalid/garden", None)
    if any(
        volume.get("name") == "data" for volume in volumes if isinstance(volume, dict)
    ):
        raise ValueError("unexpected rendered Kubernetes shape")
    volumes.append(
        {"name": "data", "persistentVolumeClaim": {"claimName": "data-cairn-0"}}
    )

    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "cairn-bootstrap",
                "namespace": namespace,
                "labels": template_labels | owner_labels,
            },
            "spec": pod_spec,
        },
        sort_keys=False,
    )


def _load_documents(raw: str) -> list[dict[str, Any]]:
    import yaml

    try:
        documents = [document for document in yaml.safe_load_all(raw) if document]
    except yaml.YAMLError as error:
        raise ValueError("unexpected rendered Kubernetes shape") from error
    if not documents or not all(isinstance(document, dict) for document in documents):
        raise ValueError("unexpected rendered Kubernetes shape")
    return documents


def _inventory(documents: list[dict[str, Any]]) -> set[tuple[str, str]]:
    inventory: set[tuple[str, str]] = set()
    for document in documents:
        kind = document.get("kind")
        metadata = document.get("metadata")
        if not isinstance(kind, str) or not isinstance(metadata, dict):
            raise ValueError("unexpected rendered Kubernetes shape")
        name = metadata.get("name")
        if not isinstance(name, str):
            raise ValueError("unexpected rendered Kubernetes shape")
        inventory.add((kind, name))
    if len(inventory) != len(documents):
        raise ValueError("unexpected rendered Kubernetes shape")
    return inventory


def _named(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and isinstance(document.get("metadata"), dict)
        and document["metadata"].get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError("unexpected rendered Kubernetes shape")
    return matches[0]


def _by_kind(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [document for document in documents if document.get("kind") == kind]


def _mapping(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise ValueError("unexpected rendered Kubernetes shape")
    return value


def _list(container: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = container.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("unexpected rendered Kubernetes shape")
    return value


def _container(containers: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [container for container in containers if container.get("name") == name]
    if len(matches) != 1:
        raise ValueError("unexpected rendered Kubernetes shape")
    return matches[0]


def _replace_instance_labels(value: Any, instance_name: str) -> None:
    if isinstance(value, dict):
        if _INSTANCE_LABEL in value:
            if value[_INSTANCE_LABEL] != "cairn":
                raise ValueError("unexpected rendered Kubernetes shape")
            value[_INSTANCE_LABEL] = instance_name
        for child in value.values():
            _replace_instance_labels(child, instance_name)
    elif isinstance(value, list):
        for child in value:
            _replace_instance_labels(child, instance_name)


def _label_nested_metadata(value: Any, owner_labels: dict[str, str]) -> None:
    if isinstance(value, dict):
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            labels = metadata.get("labels")
            if isinstance(labels, dict):
                labels.update(owner_labels)
        for child in value.values():
            _label_nested_metadata(child, owner_labels)
    elif isinstance(value, list):
        for child in value:
            _label_nested_metadata(child, owner_labels)


def site_inventory(semantic: bool) -> frozenset[str]:
    """Static ownership boundary, safe to import without project dependencies."""
    return frozenset(kind.lower() + "/" + name for kind, name in _INVENTORIES[semantic])


def asset_envelope(
    *,
    namespace: str,
    instance_name: str,
    instance_id: str,
    image: str,
    storage_class: str,
    semantic: bool,
    owner_labels: dict[str, str],
    image_policy: str,
    falkordb_receipt: dict[str, Any] | None = None,
    garden_enabled: bool = False,
    raw: str | None = None,
    site: str | None = None,
    holder: str | None = None,
) -> dict[str, Any]:
    """Locked-runtime boundary: JSON in/out, all YAML parsing stays here."""
    import yaml

    if image_policy not in {"Always", "IfNotPresent"}:
        raise ValueError("invalid image policy")
    falkordb_image: str | None = None
    falkordb_nodes: list[str] = []
    if falkordb_receipt is not None:
        if not semantic or not isinstance(falkordb_receipt, dict):
            raise ValueError("invalid FalkorDB receipt")
        falkordb_image = falkordb_receipt.get("image")
        node_records = falkordb_receipt.get("nodes")
        if (
            not isinstance(falkordb_image, str)
            or not isinstance(node_records, list)
            or not node_records
            or not all(
                isinstance(node, dict) and isinstance(node.get("name"), str)
                for node in node_records
            )
        ):
            raise ValueError("invalid FalkorDB receipt")
        falkordb_nodes = [node["name"] for node in node_records]
    if site is None:
        if raw is None:
            raise ValueError("source manifest is missing")
        site = render_site(
            raw,
            namespace=namespace,
            instance_name=instance_name,
            instance_id=instance_id,
            image=image,
            storage_class=storage_class,
            semantic=semantic,
            owner_labels=owner_labels,
        )
        documents = _load_documents(site)
        for document in _by_kind(documents, "StatefulSet"):
            pod = document["spec"]["template"]["spec"]
            pod["nodeSelector"] = {
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
            }
            if document["metadata"]["name"] == "cairn":
                pod.setdefault("securityContext", {}).update(
                    fsGroup=65532,
                    fsGroupChangePolicy="OnRootMismatch",
                )
                if garden_enabled:
                    pod["securityContext"].update(
                        runAsUser=65532,
                        runAsGroup=65532,
                    )
                for container in pod["containers"] + pod.get("initContainers", []):
                    container["imagePullPolicy"] = image_policy
            elif document["metadata"]["name"] == "falkordb":
                if falkordb_image is not None:
                    container = _container(pod["containers"], "falkordb")
                    container["image"] = falkordb_image
                    container["imagePullPolicy"] = "Never"
                    pod["affinity"] = {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchFields": [
                                            {
                                                "key": "metadata.name",
                                                "operator": "In",
                                                "values": [node],
                                            }
                                        ]
                                    }
                                    for node in falkordb_nodes
                                ]
                            }
                        }
                    }
        site = yaml.safe_dump_all(documents, sort_keys=False)
    else:
        documents = _load_documents(site)
    if _inventory(documents) != _INVENTORIES[semantic]:
        raise ValueError("retained site inventory differs")
    if falkordb_image is not None:
        falkordb = _named(documents, "StatefulSet", "falkordb")
        pod = _mapping(_mapping(_mapping(falkordb, "spec"), "template"), "spec")
        container = _container(_list(pod, "containers"), "falkordb")
        affinity = _mapping(_mapping(pod, "affinity"), "nodeAffinity")
        required = _mapping(affinity, "requiredDuringSchedulingIgnoredDuringExecution")
        if (
            container.get("image") != falkordb_image
            or container.get("imagePullPolicy") != "Never"
            or required.get("nodeSelectorTerms")
            != [
                {
                    "matchFields": [
                        {
                            "key": "metadata.name",
                            "operator": "In",
                            "values": [node],
                        }
                    ]
                }
                for node in falkordb_nodes
            ]
        ):
            raise ValueError("retained FalkorDB placement differs")
    for document in documents:
        metadata = _mapping(document, "metadata")
        if metadata.get("namespace") != namespace or any(
            _mapping(metadata, "labels").get(key) != value
            for key, value in owner_labels.items()
        ):
            raise ValueError("retained site ownership differs")
    expected_holder = render_holder(
        site, namespace=namespace, owner_labels=owner_labels
    )
    holder_document = _load_documents(expected_holder)[0]
    if holder is None:
        holder = expected_holder
    elif _load_documents(holder) != [holder_document]:
        raise ValueError("retained holder differs from the site template")
    return {
        "site": site,
        "holder": holder,
        "documents": documents,
        "holder_document": holder_document,
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        response = asset_envelope(**request)
    except (ValueError, TypeError, KeyError):
        print("Invalid Kubernetes asset request", file=sys.stderr)
        return 2
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
