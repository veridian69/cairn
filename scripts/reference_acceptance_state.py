#!/usr/bin/env python3
"""Fail-closed state and evidence rules for the reference acceptance harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OWNER = "cairn.example.invalid/acceptance-owner"
RUN = "cairn.example.invalid/acceptance-run"
REVISION = "cairn.example.invalid/acceptance-revision"
EXPECTED_OWNER = "task11"
EXPECTED_MOUNTS = {
    f"/mnt/cairn-local/pv{number}": f"/dev/sd{letter}"
    for number, letter in enumerate("bcde", start=1)
}
KIND_TO_RESOURCE = {
    "ConfigMap": "configmap",
    "Deployment": "deployment",
    "NetworkPolicy": "networkpolicy",
    "PersistentVolumeClaim": "persistentvolumeclaim",
    "Pod": "pod",
    "Secret": "secret",
    "Service": "service",
    "ServiceAccount": "serviceaccount",
    "StatefulSet": "statefulset",
}
# Controller-owned children and Service discovery objects are not rendered,
# but they are mutable members of the exact run inventory and must not become
# an ownership loophole on a resumed run.
MUTABLE_NAMESPACED_RESOURCES = tuple(KIND_TO_RESOURCE.values()) + (
    "replicaset",
    "controllerrevision",
    "endpoints",
    "endpointslice",
)
I95_GAP = (
    "no cross-node policy on one node, and OpenShift unclaimed until its suite "
    "passes on a real cluster."
)
CILIUM_TARGETS = (
    ("agent", "DaemonSet", "cilium", "cilium-agent"),
    ("operator", "Deployment", "cilium-operator", "cilium-operator"),
    ("envoy", "DaemonSet", "cilium-envoy", "cilium-envoy"),
)


class HarnessRefusal(ValueError):
    """The target state is ambiguous or unsafe to mutate."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_REGISTRY = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?")
_PATH_COMPONENT = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*")


def canonical_image_reference(reference: str) -> str:
    """Normalise a Docker familiar reference to the CRI/containerd form."""
    if not reference or reference != reference.strip() or "://" in reference:
        raise HarnessRefusal("image reference is malformed")
    if reference.count("@") > 1:
        raise HarnessRefusal("image reference is malformed")
    named, separator, digest = reference.partition("@")
    if separator and not _DIGEST.fullmatch(digest):
        raise HarnessRefusal("image reference digest is not canonical sha256")

    final_component = named.rsplit("/", 1)[-1]
    if ":" in final_component:
        image_name, tag = named.rsplit(":", 1)
        if not _TAG.fullmatch(tag):
            raise HarnessRefusal("image reference tag is malformed")
        tag_suffix = f":{tag}"
    else:
        image_name = named
        tag_suffix = "" if separator else ":latest"
    components = image_name.split("/")
    if not components or any(not component for component in components):
        raise HarnessRefusal("image reference name is malformed")
    if len(components) == 1:
        registry = "docker.io"
        path = ["library", components[0]]
    elif "." in components[0] or ":" in components[0] or components[0] == "localhost":
        registry = components[0]
        path = components[1:]
    else:
        registry = "docker.io"
        path = components
    if not _REGISTRY.fullmatch(registry) or any(
        not _PATH_COMPONENT.fullmatch(component) for component in path
    ):
        raise HarnessRefusal("image reference name is malformed")
    digest_suffix = f"@{digest}" if separator else ""
    return f"{registry}/{'/'.join(path)}{tag_suffix}{digest_suffix}"


def resolve_imported_image(
    requested: str, images: list[dict[str, str]]
) -> dict[str, str]:
    """Select one exact canonical imported reference and its OCI target digest."""
    references = [
        reference
        for image in images
        if isinstance((reference := image.get("reference")), str)
    ]
    canonical = resolve_imported_reference(requested, references)
    image = next(image for image in images if image.get("reference") == canonical)
    target_digest = image.get("target_digest")
    if not isinstance(target_digest, str) or not _DIGEST.fullmatch(target_digest):
        raise HarnessRefusal("canonical imported image lacks an OCI target digest")
    if "@" in canonical and canonical.rsplit("@", 1)[1] != target_digest:
        raise HarnessRefusal("imported target digest does not match requested digest")
    return {"reference": canonical, "target_digest": target_digest}


def resolve_imported_reference(requested: str, references: list[str]) -> str:
    """Select the one exact canonical reference from ``ctr images list -q``."""
    canonical = canonical_image_reference(requested)
    matches: list[str] = []
    for reference in references:
        try:
            candidate = canonical_image_reference(reference)
        except HarnessRefusal:
            continue
        if candidate == canonical:
            matches.append(reference)
    if len(matches) > 1:
        raise HarnessRefusal(f"imported image reference is ambiguous for {canonical}")
    if not matches or matches[0] != canonical:
        raise HarnessRefusal(f"canonical imported image is absent: {canonical}")
    return canonical


_CTR_IMAGE_HEADER = ("REF", "TYPE", "DIGEST", "SIZE", "PLATFORMS", "LABELS")
_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}


def containerd_target_digest(reference: str, table: str) -> str:
    """Parse the stable leading columns of a containerd 2.3 image table."""
    if canonical_image_reference(reference) != reference:
        raise HarnessRefusal("containerd image reference is not canonical")
    lines = table.splitlines()
    if not lines or tuple(lines[0].split()) != _CTR_IMAGE_HEADER:
        raise HarnessRefusal("containerd image table header is unexpected")
    if len(lines) != 2:
        raise HarnessRefusal("containerd image table lacks one exact data row")
    fields = lines[1].split()
    if len(fields) < 6 or fields[0] != reference:
        raise HarnessRefusal("containerd image table row is malformed or mismatched")
    digest = fields[2]
    if not _DIGEST.fullmatch(digest):
        raise HarnessRefusal("containerd image table target digest is malformed")
    if "@" in reference and reference.rsplit("@", 1)[1] != digest:
        raise HarnessRefusal("imported target digest does not match requested digest")
    return digest


def _ctr_bytes(*arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["ctr", "--namespace", "k8s.io", *arguments],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise HarnessRefusal("containerd image query failed") from error
    if result.returncode != 0 or result.stderr:
        raise HarnessRefusal("containerd image query failed or emitted diagnostics")
    return result.stdout


def _ctr(*arguments: str) -> str:
    try:
        return _ctr_bytes(*arguments).decode("utf-8")
    except UnicodeDecodeError as error:
        raise HarnessRefusal(
            "containerd image query returned non-UTF-8 text"
        ) from error


def resolve_containerd_image(requested: str) -> dict[str, str]:
    """Resolve an imported image through the ctr v2.3 quiet and table surfaces."""
    quiet = _ctr("images", "list", "-q")
    references = quiet.splitlines()
    if not references:
        raise HarnessRefusal("containerd quiet image inventory is empty")
    reference = resolve_imported_reference(requested, references)
    # Canonical reference grammar excludes whitespace, comma, equals and quotes,
    # so the exact name filter cannot be extended with containerd filter syntax.
    table = _ctr("images", "list", f"name=={reference}")
    return {
        "reference": reference,
        "target_digest": containerd_target_digest(reference, table),
    }


def _verified_content_json(digest: str, content: bytes) -> dict[str, Any]:
    if not _DIGEST.fullmatch(digest):
        raise HarnessRefusal("OCI content digest is malformed")
    observed = "sha256:" + hashlib.sha256(content).hexdigest()
    if observed != digest:
        raise HarnessRefusal("OCI content hash does not match requested digest")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessRefusal("OCI content is not valid JSON") from error
    if not isinstance(payload, dict):
        raise HarnessRefusal("OCI content is not a JSON object")
    return payload


def _require_schema(payload: dict[str, Any], media_types: set[str], kind: str) -> str:
    schema_version = payload.get("schemaVersion")
    if type(schema_version) is not int or schema_version != 2:
        raise HarnessRefusal(f"{kind} has an unsupported schema version")
    media_type = payload.get("mediaType")
    if not isinstance(media_type, str) or media_type not in media_types:
        raise HarnessRefusal(f"{kind} has an unsupported media type")
    return media_type


def _descriptor(
    descriptor: Any, media_types: set[str], kind: str
) -> tuple[str, str, int]:
    if not isinstance(descriptor, dict):
        raise HarnessRefusal(f"{kind} descriptor is malformed")
    media_type = descriptor.get("mediaType")
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(media_type, str) or media_type not in media_types:
        raise HarnessRefusal(f"{kind} descriptor has an unsupported media type")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise HarnessRefusal(f"{kind} descriptor digest is malformed")
    if type(size) is not int or size < 0:
        raise HarnessRefusal(f"{kind} descriptor size is malformed")
    return media_type, digest, size


def _platform_manifest_descriptor(index: dict[str, Any]) -> tuple[str, str, int]:
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise HarnessRefusal("OCI index manifest list is malformed")
    candidates: list[tuple[str, str, int]] = []
    for descriptor in manifests:
        media_type, digest, size = _descriptor(
            descriptor, _MANIFEST_MEDIA_TYPES, "OCI manifest"
        )
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            raise HarnessRefusal("OCI manifest platform is malformed")
        operating_system = platform.get("os")
        architecture = platform.get("architecture")
        if not isinstance(operating_system, str) or not isinstance(architecture, str):
            raise HarnessRefusal("OCI manifest platform is malformed")
        if operating_system == "linux" and architecture == "amd64":
            if platform.get("variant") not in {None, ""}:
                raise HarnessRefusal("linux/amd64 manifest has an unexpected variant")
            candidates.append((media_type, digest, size))
    if len(candidates) != 1:
        raise HarnessRefusal("OCI index lacks one exact linux/amd64 manifest")
    return candidates[0]


def _verified_descriptor_json(
    digest: str, size: int, content: bytes, kind: str
) -> dict[str, Any]:
    if len(content) != size:
        raise HarnessRefusal(f"{kind} content length does not match descriptor size")
    return _verified_content_json(digest, content)


def oci_provenance(
    target_digest: str, fetch_content: Callable[[str], bytes]
) -> dict[str, str]:
    """Prove target/index, linux/amd64 manifest and runtime config identity."""
    target = _verified_content_json(target_digest, fetch_content(target_digest))
    target_media = target.get("mediaType")
    if target_media in _INDEX_MEDIA_TYPES:
        _require_schema(target, _INDEX_MEDIA_TYPES, "OCI index")
        (
            expected_manifest_media,
            manifest_digest,
            manifest_size,
        ) = _platform_manifest_descriptor(target)
        manifest = _verified_descriptor_json(
            manifest_digest,
            manifest_size,
            fetch_content(manifest_digest),
            "OCI manifest",
        )
    elif target_media in _MANIFEST_MEDIA_TYPES:
        expected_manifest_media = str(target_media)
        manifest_digest = target_digest
        # A direct target manifest has no parent descriptor. ctr's table exposes
        # only a human-formatted size, so the raw target digest is authoritative.
        manifest = target
    else:
        raise HarnessRefusal("OCI target has an unsupported media type")
    manifest_media = _require_schema(manifest, _MANIFEST_MEDIA_TYPES, "OCI manifest")
    if manifest_media != expected_manifest_media:
        raise HarnessRefusal("OCI manifest media type differs from its descriptor")
    _config_media, config_digest, config_size = _descriptor(
        manifest.get("config"), _CONFIG_MEDIA_TYPES, "OCI config"
    )
    config = _verified_descriptor_json(
        config_digest,
        config_size,
        fetch_content(config_digest),
        "OCI config",
    )
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise HarnessRefusal("OCI config is not linux/amd64")
    return {
        "target_digest": target_digest,
        "platform_manifest_digest": manifest_digest,
        "config_digest": config_digest,
    }


def resolve_oci_provenance(requested: str) -> dict[str, str]:
    """Resolve the canonical image and its verified content provenance chain."""
    resolved = resolve_containerd_image(requested)
    provenance = oci_provenance(
        resolved["target_digest"],
        lambda digest: _ctr_bytes("content", "get", digest),
    )
    return {"reference": resolved["reference"], **provenance}


def runtime_image_digest(image_id: str) -> str:
    """Extract the terminal OCI manifest digest from a CRI image ID."""
    match = re.search(r"sha256:[0-9a-f]{64}$", image_id)
    if not match:
        raise HarnessRefusal("runtime image ID has no OCI digest")
    return match.group(0)


def runtime_config_digest(image_id: str, expected_config_digest: str) -> str:
    """Require the CRI image ID to identify the verified OCI config blob."""
    if not _DIGEST.fullmatch(expected_config_digest):
        raise HarnessRefusal("expected OCI config digest is malformed")
    observed = runtime_image_digest(image_id)
    if observed != expected_config_digest:
        raise HarnessRefusal("runtime image ID does not match OCI config digest")
    return observed


def _digest_qualified_reference(reference: Any, description: str) -> str:
    if (
        not isinstance(reference, str)
        or canonical_image_reference(reference) != reference
    ):
        raise HarnessRefusal(f"{description} is malformed")
    _name, separator, digest = reference.rpartition("@")
    if not separator or not _DIGEST.fullmatch(digest):
        raise HarnessRefusal(f"{description} is mutable or non-digest-qualified")
    return digest


def _required_cilium_images(expected_images: dict[str, str]) -> dict[str, str]:
    required = {key for key, *_rest in CILIUM_TARGETS}
    if set(expected_images) != required:
        raise HarnessRefusal("expected Cilium image identity set is incomplete")
    for key, reference in expected_images.items():
        _digest_qualified_reference(reference, f"expected Cilium {key} image")
    return expected_images


def _selector_matches(labels: Any, selector: dict[str, str]) -> bool:
    return isinstance(labels, dict) and all(
        labels.get(key) == value for key, value in selector.items()
    )


def _cilium_desired_pods(workload: dict[str, Any], kind: str) -> int:
    metadata = workload.get("metadata")
    spec = workload.get("spec")
    status = workload.get("status")
    if (
        not isinstance(metadata, dict)
        or not isinstance(spec, dict)
        or not isinstance(status, dict)
    ):
        raise HarnessRefusal("Cilium workload shape is malformed")
    generation = metadata.get("generation")
    if type(generation) is not int or status.get("observedGeneration") != generation:
        raise HarnessRefusal("Cilium workload generation is not observed")
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled")
        ready_fields = (
            "currentNumberScheduled",
            "updatedNumberScheduled",
            "numberAvailable",
            "numberReady",
        )
    else:
        desired = spec.get("replicas")
        ready_fields = (
            "replicas",
            "updatedReplicas",
            "availableReplicas",
            "readyReplicas",
        )
    if (
        type(desired) is not int
        or desired < 1
        or any(status.get(field) != desired for field in ready_fields)
    ):
        raise HarnessRefusal("Cilium workload is not fully current and ready")
    return desired


def validate_cilium_report_images(images: list[dict[str, Any]]) -> None:
    """Require the exact three immutable spec/runtime Cilium identities."""
    expected = {
        (kind, name, container) for _key, kind, name, container in CILIUM_TARGETS
    }
    observed: set[tuple[str, str, str]] = set()
    workload_uids: set[str] = set()
    if not isinstance(images, list):
        raise HarnessRefusal("report Cilium image identities are malformed")
    for item in images:
        if not isinstance(item, dict):
            raise HarnessRefusal("report Cilium image identity is malformed")
        identity = (
            item.get("workload_kind"),
            item.get("workload_name"),
            item.get("container_name"),
        )
        if (
            not all(isinstance(value, str) for value in identity)
            or identity not in expected
        ):
            raise HarnessRefusal("report Cilium image identity is unexpected")
        typed_identity = (str(identity[0]), str(identity[1]), str(identity[2]))
        if typed_identity in observed:
            raise HarnessRefusal("report Cilium image identity is duplicate")
        observed.add(typed_identity)
        workload_uid = item.get("workload_uid")
        if (
            not isinstance(workload_uid, str)
            or not workload_uid
            or workload_uid in workload_uids
        ):
            raise HarnessRefusal("report Cilium workload UID is malformed")
        workload_uids.add(workload_uid)
        spec_digest = _digest_qualified_reference(
            item.get("spec_image"), "Cilium spec image"
        )
        runtime_id = item.get("runtime_image_id")
        if not isinstance(runtime_id, str):
            raise HarnessRefusal("Cilium runtime image ID is malformed")
        runtime_digest = runtime_image_digest(runtime_id)
        if (
            item.get("runtime_image_digest") != runtime_digest
            or runtime_digest != spec_digest
        ):
            raise HarnessRefusal(
                "Cilium runtime image identity does not match its spec image"
            )
        pod_names = item.get("pod_names")
        pod_uids = item.get("pod_uids")
        if (
            not isinstance(pod_names, list)
            or len(pod_names) != 1
            or not all(isinstance(name, str) and name for name in pod_names)
            or len(set(pod_names)) != len(pod_names)
            or not isinstance(pod_uids, list)
            or len(pod_uids) != len(pod_names)
            or not all(isinstance(uid, str) and uid for uid in pod_uids)
            or len(set(pod_uids)) != len(pod_uids)
        ):
            raise HarnessRefusal("Cilium image identity has ambiguous Pod identities")
        selector = item.get("selector_labels")
        if (
            item.get("namespace") != "kube-system"
            or item.get("desired_pods") != 1
            or not isinstance(selector, dict)
            or not selector
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in selector.items()
            )
        ):
            raise HarnessRefusal("Cilium image identity has malformed association")
        expected_chain = [
            {"kind": typed_identity[0], "name": typed_identity[1], "uid": workload_uid}
        ]
        chain = item.get("controller_chain")
        if typed_identity[0] == "Deployment":
            if (
                not isinstance(chain, list)
                or len(chain) != 3
                or not isinstance(chain[1], dict)
                or chain[1].get("kind") != "ReplicaSet"
                or not isinstance(chain[1].get("name"), str)
                or not chain[1].get("name")
                or not isinstance(chain[1].get("uid"), str)
                or not chain[1].get("uid")
            ):
                raise HarnessRefusal("Cilium controller chain is malformed")
            expected_chain.append(chain[1])
        expected_chain.append({"kind": "Pod", "name": pod_names[0], "uid": pod_uids[0]})
        if chain != expected_chain:
            raise HarnessRefusal("Cilium controller chain is malformed")
    if observed != expected:
        raise HarnessRefusal("report lacks exact Cilium image identities")


def _require_current_kube_system_object(
    item: dict[str, Any], kind: str, name: str
) -> dict[str, Any]:
    metadata = item.get("metadata")
    if (
        item.get("kind") != kind
        or not isinstance(metadata, dict)
        or metadata.get("name") != name
        or metadata.get("namespace") != "kube-system"
        or metadata.get("deletionTimestamp") is not None
    ):
        raise HarnessRefusal(
            f"Cilium {kind}/{name} is in the wrong namespace or is deleting"
        )
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise HarnessRefusal(f"Cilium {kind}/{name} has ambiguous UID identity")
    return metadata


def _require_controller_owner(
    metadata: dict[str, Any],
    expected_kind: str,
    expected_name: str,
    expected_uid: str,
    *,
    subject: str,
) -> None:
    references = metadata.get("ownerReferences")
    controllers = (
        [
            reference
            for reference in references
            if isinstance(reference, dict) and reference.get("controller") is True
        ]
        if isinstance(references, list)
        else []
    )
    expected = {
        "apiVersion": "apps/v1",
        "kind": expected_kind,
        "name": expected_name,
        "uid": expected_uid,
        "controller": True,
    }
    if len(controllers) != 1 or any(
        controllers[0].get(key) != value for key, value in expected.items()
    ):
        raise HarnessRefusal(f"Cilium {subject} has no exact controller owner")


def _current_deployment_replicaset(
    replicasets: list[dict[str, Any]],
    *,
    deployment_name: str,
    deployment_uid: str,
    selector: dict[str, str],
    desired: int,
) -> dict[str, Any]:
    candidates = [
        replicaset
        for replicaset in replicasets
        if replicaset.get("kind") == "ReplicaSet"
        and _selector_matches(replicaset.get("metadata", {}).get("labels"), selector)
    ]
    current: list[dict[str, Any]] = []
    for replicaset in candidates:
        metadata = replicaset.get("metadata", {})
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise HarnessRefusal("Cilium ReplicaSet identity is malformed")
        metadata = _require_current_kube_system_object(replicaset, "ReplicaSet", name)
        _require_controller_owner(
            metadata,
            "Deployment",
            deployment_name,
            deployment_uid,
            subject="ReplicaSet controller owner",
        )
        spec = replicaset.get("spec", {})
        generation = metadata.get("generation")
        status = replicaset.get("status", {})
        if (
            type(generation) is not int
            or status.get("observedGeneration") != generation
        ):
            raise HarnessRefusal("Cilium ReplicaSet generation is not observed")
        selector_block = spec.get("selector")
        replicaset_selector = (
            selector_block.get("matchLabels")
            if isinstance(selector_block, dict)
            else None
        )
        template_labels = spec.get("template", {}).get("metadata", {}).get("labels")
        extra_selector_keys = (
            set(replicaset_selector) - set(selector)
            if isinstance(replicaset_selector, dict)
            else set()
        )
        template_hash = (
            replicaset_selector.get("pod-template-hash")
            if isinstance(replicaset_selector, dict)
            else None
        )
        if (
            not isinstance(selector_block, dict)
            or selector_block.get("matchExpressions") not in (None, [])
            or not isinstance(replicaset_selector, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in replicaset_selector.items()
            )
            or not _selector_matches(replicaset_selector, selector)
            or extra_selector_keys != {"pod-template-hash"}
            or not isinstance(template_hash, str)
            or re.fullmatch(r"[a-z0-9]{5,63}", template_hash) is None
            or not _selector_matches(metadata.get("labels"), replicaset_selector)
            or not _selector_matches(template_labels, replicaset_selector)
        ):
            raise HarnessRefusal(
                "Cilium ReplicaSet has an unsafe selector relationship"
            )
        replicas = spec.get("replicas", 0)
        status_counts = (
            status.get("replicas", 0),
            status.get("readyReplicas", 0),
            status.get("availableReplicas", 0),
            status.get("fullyLabeledReplicas", 0),
        )
        if replicas == desired and all(count == desired for count in status_counts):
            current.append(replicaset)
        elif replicas != 0 or any(count != 0 for count in status_counts):
            raise HarnessRefusal("Cilium Deployment has a stale current ReplicaSet")
    if len(current) != 1:
        raise HarnessRefusal("Cilium Deployment has no unique current ReplicaSet")
    return current[0]


def cilium_image_evidence(
    workloads: list[dict[str, Any]],
    replicasets: dict[str, Any],
    pods: dict[str, Any],
    expected_images: dict[str, str],
) -> list[dict[str, Any]]:
    """Bind exact Cilium controller chains to ready Pods and runtime image IDs."""
    _required_cilium_images(expected_images)
    if not isinstance(workloads, list):
        raise HarnessRefusal("Cilium workload inventory is malformed")
    replicaset_items = (
        replicasets.get("items") if isinstance(replicasets, dict) else None
    )
    if not isinstance(replicaset_items, list) or not all(
        isinstance(replicaset, dict) for replicaset in replicaset_items
    ):
        raise HarnessRefusal("Cilium ReplicaSet inventory is malformed")
    pod_items = pods.get("items") if isinstance(pods, dict) else None
    if not isinstance(pod_items, list) or not all(
        isinstance(pod, dict) for pod in pod_items
    ):
        raise HarnessRefusal("Cilium Pod inventory is malformed")
    evidence: list[dict[str, Any]] = []
    selected_pod_uids: set[str] = set()
    selected_workload_uids: set[str] = set()
    for key, kind, name, container_name in CILIUM_TARGETS:
        matches = [
            workload
            for workload in workloads
            if workload.get("kind") == kind
            and workload.get("metadata", {}).get("name") == name
            and workload.get("metadata", {}).get("namespace") == "kube-system"
        ]
        if not matches:
            raise HarnessRefusal(f"missing Cilium workload {kind}/{name}")
        if len(matches) != 1:
            raise HarnessRefusal(f"duplicate Cilium workload {kind}/{name}")
        workload = matches[0]
        metadata = _require_current_kube_system_object(workload, kind, name)
        workload_uid = metadata.get("uid")
        if (
            not isinstance(workload_uid, str)
            or not workload_uid
            or workload_uid in selected_workload_uids
        ):
            raise HarnessRefusal(
                f"Cilium workload {kind}/{name} has ambiguous UID identity"
            )
        selected_workload_uids.add(workload_uid)
        desired = _cilium_desired_pods(workload, kind)
        containers = (
            workload.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers")
        )
        if not isinstance(containers, list):
            raise HarnessRefusal(
                f"Cilium workload {kind}/{name} has malformed containers"
            )
        selected_containers = [
            container
            for container in containers
            if isinstance(container, dict) and container.get("name") == container_name
        ]
        if len(selected_containers) != 1:
            raise HarnessRefusal(
                f"Cilium workload {kind}/{name} has ambiguous container identity"
            )
        spec_image = selected_containers[0].get("image")
        expected_image = expected_images[key]
        spec_digest = _digest_qualified_reference(
            spec_image, f"Cilium {key} spec image"
        )
        if spec_image != expected_image:
            raise HarnessRefusal(
                f"Cilium {key} spec image does not match its approved pin"
            )
        selector = workload.get("spec", {}).get("selector", {}).get("matchLabels")
        template_labels = (
            workload.get("spec", {})
            .get("template", {})
            .get("metadata", {})
            .get("labels")
        )
        if (
            not isinstance(selector, dict)
            or not selector
            or not all(
                isinstance(key_name, str) and isinstance(value, str)
                for key_name, value in selector.items()
            )
            or not _selector_matches(template_labels, selector)
        ):
            raise HarnessRefusal(
                f"Cilium workload {kind}/{name} has an ambiguous selector"
            )
        expected_pod_owner = (kind, name, workload_uid)
        expected_pod_selector = selector
        controller_chain_prefix = [{"kind": kind, "name": name, "uid": workload_uid}]
        if kind == "Deployment":
            current_replicaset = _current_deployment_replicaset(
                replicaset_items,
                deployment_name=name,
                deployment_uid=workload_uid,
                selector=selector,
                desired=desired,
            )
            replicaset_metadata = current_replicaset["metadata"]
            replicaset_name = replicaset_metadata["name"]
            replicaset_uid = replicaset_metadata["uid"]
            expected_pod_owner = ("ReplicaSet", replicaset_name, replicaset_uid)
            expected_pod_selector = current_replicaset["spec"]["selector"][
                "matchLabels"
            ]
            controller_chain_prefix.append(
                {
                    "kind": "ReplicaSet",
                    "name": replicaset_name,
                    "uid": replicaset_uid,
                }
            )
        selected_pods = [
            pod
            for pod in pod_items
            if _selector_matches(pod.get("metadata", {}).get("labels"), selector)
        ]
        if len(selected_pods) != desired:
            raise HarnessRefusal(f"ambiguous Cilium pods for {kind}/{name}")
        runtime_ids: set[str] = set()
        pod_names: list[str] = []
        pod_uids: list[str] = []
        controller_chains: list[list[dict[str, str]]] = []
        for pod in selected_pods:
            pod_metadata = pod.get("metadata", {})
            pod_name = pod_metadata.get("name")
            if not isinstance(pod_name, str) or not pod_name:
                raise HarnessRefusal(
                    f"Cilium Pod for {kind}/{name} is not uniquely ready"
                )
            metadata = _require_current_kube_system_object(pod, "Pod", pod_name)
            pod_uid = metadata.get("uid")
            _require_controller_owner(metadata, *expected_pod_owner, subject="Pod")
            if not _selector_matches(metadata.get("labels"), expected_pod_selector):
                raise HarnessRefusal(
                    f"Cilium Pod for {kind}/{name} does not match its ReplicaSet selector"
                )
            statuses = pod.get("status", {}).get("containerStatuses")
            conditions = pod.get("status", {}).get("conditions")
            if (
                not isinstance(pod_uid, str)
                or not pod_uid
                or pod_uid in selected_pod_uids
                or not isinstance(pod_name, str)
                or not pod_name
                or pod.get("status", {}).get("phase") != "Running"
                or not isinstance(conditions, list)
                or not any(
                    isinstance(condition, dict)
                    and condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                    for condition in conditions
                )
            ):
                raise HarnessRefusal(
                    f"Cilium Pod for {kind}/{name} is not uniquely ready"
                )
            selected_pod_uids.add(pod_uid)
            if not isinstance(statuses, list):
                raise HarnessRefusal(
                    f"Cilium Pod for {kind}/{name} has malformed statuses"
                )
            selected_statuses = [
                status
                for status in statuses
                if isinstance(status, dict) and status.get("name") == container_name
            ]
            if (
                len(selected_statuses) != 1
                or selected_statuses[0].get("ready") is not True
            ):
                raise HarnessRefusal(
                    f"Cilium Pod for {kind}/{name} has ambiguous runtime identity"
                )
            runtime_id = selected_statuses[0].get("imageID")
            if not isinstance(runtime_id, str):
                raise HarnessRefusal("Cilium runtime image ID is malformed")
            runtime_digest = runtime_image_digest(runtime_id)
            if runtime_digest != spec_digest:
                raise HarnessRefusal(
                    f"Cilium {key} runtime image does not match its spec image"
                )
            runtime_ids.add(runtime_id)
            pod_names.append(pod_name)
            pod_uids.append(pod_uid)
            controller_chains.append(
                [
                    *controller_chain_prefix,
                    {"kind": "Pod", "name": pod_name, "uid": pod_uid},
                ]
            )
        if len(runtime_ids) != 1:
            raise HarnessRefusal(f"Cilium {key} runtime image identity is ambiguous")
        if len(controller_chains) != 1:
            raise HarnessRefusal(f"Cilium {key} controller chain is ambiguous")
        evidence.append(
            {
                "workload_kind": kind,
                "workload_name": name,
                "workload_uid": workload_uid,
                "namespace": "kube-system",
                "container_name": container_name,
                "spec_image": spec_image,
                "runtime_image_id": next(iter(runtime_ids)),
                "runtime_image_digest": spec_digest,
                "pod_names": sorted(pod_names),
                "pod_uids": sorted(pod_uids),
                "desired_pods": desired,
                "selector_labels": dict(sorted(selector.items())),
                "controller_chain": controller_chains[0],
            }
        )
    validate_cilium_report_images(evidence)
    return evidence


def _validate_primary_report(
    primary_bytes: bytes,
    *,
    primary_sha256: str,
    run_id: str,
    target_revision: str,
    expected_chart: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", primary_sha256):
        raise HarnessRefusal("primary report SHA-256 is malformed")
    if hashlib.sha256(primary_bytes).hexdigest() != primary_sha256:
        raise HarnessRefusal("primary report SHA-256 mismatch")
    try:
        primary = json.loads(primary_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessRefusal("primary report is not valid JSON") from error
    if not isinstance(primary, dict):
        raise HarnessRefusal("primary report is not a JSON object")
    if primary.get("status") != "passed" or primary.get("stage") != "complete":
        raise HarnessRefusal("primary report is not passed/complete")
    if primary.get("run_id") != run_id:
        raise HarnessRefusal("primary report run ID mismatch")
    if primary.get("repository_revision") != target_revision:
        raise HarnessRefusal("primary report revision mismatch")
    if primary.get("image_revision") != target_revision:
        raise HarnessRefusal("primary image revision mismatch")
    if primary.get("cilium_chart") != expected_chart:
        raise HarnessRefusal("primary Cilium chart mismatch")
    if primary.get("cilium_images") != []:
        raise HarnessRefusal("primary Cilium gap is not empty")
    return primary


def _cilium_chart_evidence(
    releases: list[dict[str, Any]], expected_chart: str
) -> dict[str, str]:
    matches = [
        release
        for release in releases
        if isinstance(release, dict) and release.get("name") == "cilium"
    ]
    if len(matches) != 1:
        raise HarnessRefusal("live Cilium Helm release is missing or ambiguous")
    release = matches[0]
    revision = release.get("revision")
    if type(revision) is int:
        revision = str(revision)
    required = {
        "release_name": release.get("name"),
        "namespace": release.get("namespace"),
        "revision": revision,
        "status": release.get("status"),
        "chart": release.get("chart"),
        "app_version": release.get("app_version"),
    }
    if (
        required["namespace"] != "kube-system"
        or required["status"] != "deployed"
        or required["chart"] != expected_chart
        or not isinstance(required["revision"], str)
        or not required["revision"].isdigit()
        or not isinstance(required["app_version"], str)
        or not required["app_version"]
    ):
        raise HarnessRefusal("live Cilium Helm identity is malformed or mismatched")
    return {key: str(value) for key, value in required.items()}


def build_cilium_supplement(
    primary_bytes: bytes,
    *,
    primary_sha256: str,
    run_id: str,
    target_revision: str,
    collector_revision: str,
    helm_releases: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    replicasets: dict[str, Any],
    pods: dict[str, Any],
    expected_chart: str,
    expected_images: dict[str, str],
    collected_at: str,
) -> dict[str, Any]:
    """Build the immutable read-only supplement for one reviewed report."""
    _validate_primary_report(
        primary_bytes,
        primary_sha256=primary_sha256,
        run_id=run_id,
        target_revision=target_revision,
        expected_chart=expected_chart,
    )
    if not re.fullmatch(r"[0-9a-f]{40}", collector_revision):
        raise HarnessRefusal("collector revision is malformed")
    return {
        "schema": "cairn.reference-cilium-evidence/v1",
        "status": "passed",
        "collected_at": collected_at,
        "primary_report_sha256": primary_sha256,
        "run_id": run_id,
        "target_revision": target_revision,
        "collector_revision": collector_revision,
        "chart": _cilium_chart_evidence(helm_releases, expected_chart),
        "images": cilium_image_evidence(workloads, replicasets, pods, expected_images),
    }


def _checked_json_command(arguments: list[str]) -> Any:
    try:
        result = subprocess.run(arguments, capture_output=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise HarnessRefusal(f"read-only command failed: {arguments[0]}") from error
    if result.returncode != 0 or result.stderr:
        raise HarnessRefusal(
            f"read-only command failed or emitted diagnostics: {arguments[0]}"
        )
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessRefusal(
            f"read-only command returned malformed JSON: {arguments[0]}"
        ) from error


def _live_cilium_inventory() -> tuple[
    list[dict[str, Any]], dict[str, Any], dict[str, Any]
]:
    workloads = [
        _checked_json_command(
            [
                "kubectl",
                "get",
                "daemonset" if kind == "DaemonSet" else "deployment",
                name,
                "--namespace",
                "kube-system",
                "--output",
                "json",
            ]
        )
        for _key, kind, name, _container in CILIUM_TARGETS
    ]
    replicasets = _checked_json_command(
        [
            "kubectl",
            "get",
            "replicasets",
            "--namespace",
            "kube-system",
            "--output",
            "json",
        ]
    )
    pods = _checked_json_command(
        [
            "kubectl",
            "get",
            "pods",
            "--namespace",
            "kube-system",
            "--output",
            "json",
        ]
    )
    if (
        not all(isinstance(workload, dict) for workload in workloads)
        or not isinstance(replicasets, dict)
        or not isinstance(pods, dict)
    ):
        raise HarnessRefusal("live Cilium inventory is malformed")
    return workloads, replicasets, pods


def _locked_cilium_identities(
    path: Path = Path("deploy/images.lock"),
) -> tuple[str, dict[str, str]]:
    wanted = {
        "TARGET_CILIUM_VERSION",
        "TARGET_CILIUM_AGENT_IMAGE",
        "TARGET_CILIUM_OPERATOR_IMAGE",
        "TARGET_CILIUM_ENVOY_IMAGE",
    }
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise HarnessRefusal("Cilium image lock is unreadable") from error
    for line in lines:
        key, separator, value = line.partition("=")
        if key in wanted:
            if not separator or key in values or not value:
                raise HarnessRefusal("Cilium image lock is malformed or duplicate")
            values[key] = value
    if set(values) != wanted or not values["TARGET_CILIUM_VERSION"].startswith("v"):
        raise HarnessRefusal("Cilium image lock is incomplete")
    images = {
        "agent": values["TARGET_CILIUM_AGENT_IMAGE"],
        "operator": values["TARGET_CILIUM_OPERATOR_IMAGE"],
        "envoy": values["TARGET_CILIUM_ENVOY_IMAGE"],
    }
    _required_cilium_images(images)
    return f"cilium-{values['TARGET_CILIUM_VERSION'][1:]}", images


def _identity(item: dict[str, Any]) -> str:
    metadata = item.get("metadata", {})
    namespace = metadata.get("namespace")
    prefix = f"{namespace}/" if namespace else ""
    return f"{item.get('kind', 'Object')}/{prefix}{metadata.get('name', '<unnamed>')}"


def validate_inventory_ownership(
    inventory: list[dict[str, Any]], run_id: str, repository_revision: str
) -> None:
    """Require every already-present mutable object to belong to this exact run."""
    expected = {OWNER: EXPECTED_OWNER, RUN: run_id, REVISION: repository_revision}
    for item in inventory:
        labels = item.get("metadata", {}).get("labels") or {}
        if any(labels.get(key) != value for key, value in expected.items()):
            raise HarnessRefusal(f"unexpected or unowned {_identity(item)}")


def stamp_targets_for_rendered_objects(
    objects: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Map every exact rendered kind to its ownership-stamping target."""
    targets: list[tuple[str, str]] = []
    for item in objects:
        kind = item.get("kind")
        if kind == "Namespace":
            continue
        if not isinstance(kind, str):
            raise HarnessRefusal(f"unsupported rendered kind {kind!r}")
        resource = KIND_TO_RESOURCE.get(kind)
        name = item.get("metadata", {}).get("name")
        if not resource or not isinstance(name, str) or not name:
            raise HarnessRefusal(f"unsupported or unnamed rendered kind {kind!r}")
        targets.append((resource, name))
    return targets


def invocation_action(mode: str, *, report_exists: bool, durable_phase: str) -> str:
    if mode == "preflight":
        return "preflight-only"
    if mode != "run":
        raise HarnessRefusal(f"unknown invocation mode {mode}")
    if report_exists:
        raise HarnessRefusal("immutable evidence already exists; choose a new run ID")
    if durable_phase in {"bootstrap-started", "recovery-started", "complete"}:
        raise HarnessRefusal(
            f"phase {durable_phase} requires the manual recovery route; do not resume automatically"
        )
    if durable_phase not in {"", "prepared", "bootstrap-complete"}:
        raise HarnessRefusal(f"unknown durable phase {durable_phase}")
    return "start"


def build_report(
    *,
    status: str,
    stage: str,
    run_id: str,
    repository_revision: str,
    image_revision: str,
    image_digest: str,
    preflight: dict[str, str],
    rendered_manifest_digests: list[dict[str, str]],
    workload_images: list[dict[str, str]],
    cilium_images: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    backup_barrier_ms: int | float | None,
    cairn_image: str = "",
    cairn_local_image_id: str = "",
    cairn_target_digest: str = "",
    cairn_platform_manifest_digest: str = "",
    cairn_config_digest: str = "",
    cairn_runtime_image_id: str = "",
    backup_command_started_ns: int | None = None,
    backup_mutation_completed_ns: int | None = None,
    backup_child_pid: int | None = None,
    backup_child_start_ticks: str | None = None,
    backup_child_alive_observed_ns: int | None = None,
    backup_command_finished_ns: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    if image_revision != repository_revision:
        raise HarnessRefusal(
            f"image revision {image_revision!r} does not match repository revision {repository_revision}"
        )
    required = (
        "distribution",
        "kubernetes_version",
        "container_runtime",
        "storage_class",
        "cni",
        "cilium_chart",
    )
    missing = [name for name in required if not preflight.get(name)]
    if missing:
        raise HarnessRefusal(f"report lacks preflight values: {', '.join(missing)}")
    if status == "passed":
        validate_cilium_report_images(cilium_images)
    return {
        "status": status,
        "stage": stage,
        "run_id": run_id,
        "repository_revision": repository_revision,
        "image_revision": image_revision,
        "cairn_image": cairn_image,
        "cairn_image_digest": image_digest,
        "cairn_local_image_id": cairn_local_image_id,
        "cairn_target_digest": cairn_target_digest,
        "cairn_platform_manifest_digest": cairn_platform_manifest_digest,
        "cairn_config_digest": cairn_config_digest,
        "cairn_runtime_image_id": cairn_runtime_image_id,
        **{name: preflight[name] for name in required},
        "rendered_manifest_digests": rendered_manifest_digests,
        "workload_images": workload_images,
        "cilium_images": cilium_images,
        "backup_barrier_ms": backup_barrier_ms,
        "backup_command_started_ns": backup_command_started_ns,
        "backup_mutation_completed_ns": backup_mutation_completed_ns,
        "backup_child_pid": backup_child_pid,
        "backup_child_start_ticks": backup_child_start_ticks,
        "backup_child_alive_observed_ns": backup_child_alive_observed_ns,
        "backup_command_finished_ns": backup_command_finished_ns,
        "backup_overlap_scope": (
            "a mutation completed before a later observation proved the exact "
            "server-side cairn backup child was still alive; this does not prove "
            "overlap with the narrower SQLite barrier"
        ),
        "checks": checks,
        "gaps": [I95_GAP],
        "task_11_observations": [
            "The f1418ad live attempt observed a PostHog upload through the "
            "gateway refused with HTTP 403; this is fail-closed egress evidence, "
            "not a provider-success claim."
        ],
        "task_11a_status": "pending",
        "task_11a_observations": [
            "The target run records volume ownership; it does not close Task 11a.",
            "Retrieval enablement remains a multi-step operator action.",
            "The gateway NetworkPolicy still permits 443 without a cluster-CIDR exclusion.",
            "The instance namespace label remains a grant governed outside these artefacts.",
        ],
    }


def write_report_exclusive(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def service_selector_patch(namespace: str) -> list[dict[str, Any]]:
    return [
        {
            "op": "replace",
            "path": "/spec/selector",
            "value": {
                "app.kubernetes.io/name": "cairn",
                "app.kubernetes.io/instance": namespace,
                "cairn.example.invalid/recovery": "restored",
            },
        }
    ]


def bootstrap_secret_command(namespace: str) -> list[str]:
    """Return the credential-safe client command; the value is stdin only."""
    return [
        "kubectl",
        "create",
        "secret",
        "generic",
        "acceptance-client",
        "-n",
        namespace,
        "--from-file=token=/dev/stdin",
        "--dry-run=client",
        "-o",
        "json",
    ]


def endpoint_candidate_converged(
    observations: list[list[str]], candidate_ip: str, consecutive: int = 3
) -> bool:
    if consecutive < 1 or len(observations) < consecutive:
        return False
    return all(addresses == [candidate_ip] for addresses in observations[-consecutive:])


def rwop_refusal_is_current(pod: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    uid = pod.get("metadata", {}).get("uid")
    status = pod.get("status", {})
    if not uid or status.get("phase") != "Pending":
        return False
    scheduled_refusal = any(
        condition.get("type") == "PodScheduled"
        and condition.get("status") == "False"
        and condition.get("reason") == "Unschedulable"
        and "ReadWriteOncePod access mode already in-use"
        in condition.get("message", "")
        for condition in status.get("conditions", [])
    )
    current_event = any(
        event.get("involvedObject", {}).get("uid") == uid
        and "ReadWriteOncePod access mode already in-use" in event.get("message", "")
        for event in events
    )
    return scheduled_refusal and current_event


def _node_is_reference(pv: dict[str, Any]) -> bool:
    expressions = (
        pv.get("spec", {})
        .get("nodeAffinity", {})
        .get("required", {})
        .get("nodeSelectorTerms", [{}])[0]
        .get("matchExpressions", [])
    )
    return any(
        item.get("key") == "kubernetes.io/hostname"
        and item.get("operator") == "In"
        and item.get("values") == ["reference"]
        for item in expressions
    )


def validate_storage_inventory(
    mounts: list[dict[str, Any]], pvs: list[dict[str, Any]]
) -> None:
    actual_mounts = {
        mount.get("target"): mount.get("source")
        for mount in mounts
        if mount.get("fstype") == "xfs" and mount.get("size_bytes") == 20 * 1024**3
    }
    if actual_mounts != EXPECTED_MOUNTS:
        raise HarnessRefusal("mounted storage is not exactly /dev/sdb-sde, XFS, 20 GiB")
    if len(pvs) != 4:
        raise HarnessRefusal("expected exactly four cairn-local PVs")
    paths: set[str] = set()
    for pv in pvs:
        spec = pv.get("spec", {})
        name = pv.get("metadata", {}).get("name", "<unnamed>")
        valid = (
            spec.get("storageClassName") == "cairn-local"
            and spec.get("volumeMode") == "Filesystem"
            and spec.get("accessModes") == ["ReadWriteOncePod"]
            and spec.get("persistentVolumeReclaimPolicy") == "Retain"
            and spec.get("capacity", {}).get("storage") == "20Gi"
            and pv.get("status", {}).get("phase") in {"Available", "Bound"}
            and _node_is_reference(pv)
        )
        path = spec.get("local", {}).get("path")
        if not valid or path not in EXPECTED_MOUNTS:
            raise HarnessRefusal(f"invalid real-disc PV {name}")
        paths.add(path)
    if paths != set(EXPECTED_MOUNTS):
        raise HarnessRefusal("PV paths do not exactly cover pv1-pv4")


def claim_bindings(
    claims: list[dict[str, Any]], pvs: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    by_name = {pv.get("metadata", {}).get("name"): pv for pv in pvs}
    result: dict[str, dict[str, str]] = {}
    for claim in claims:
        name = claim.get("metadata", {}).get("name", "<unnamed>")
        pv_name = claim.get("spec", {}).get("volumeName")
        if claim.get("status", {}).get("phase") != "Bound" or pv_name not in by_name:
            raise HarnessRefusal(f"claim {name} is not bound to an inventoried PV")
        pv = by_name[pv_name]
        policy = pv.get("spec", {}).get("persistentVolumeReclaimPolicy")
        path = pv.get("spec", {}).get("local", {}).get("path")
        if policy != "Retain" or path not in EXPECTED_MOUNTS:
            raise HarnessRefusal(f"claim {name} lacks a real-disc Retain binding")
        result[name] = {"pv": pv_name, "path": path, "reclaim_policy": policy}
    return result


def backup_command_overlap(
    *,
    backup_started_ns: int,
    backup_finished_ns: int,
    wrapper_observed_running: bool,
    exact_child_observed_alive: bool,
    child_alive_observed_ns: int | None,
    mutation_completed_ns: list[int],
) -> int:
    """Return a mutation completion observed inside the backup command window."""
    if not wrapper_observed_running:
        raise HarnessRefusal("backup wrapper was not observed running")
    if not exact_child_observed_alive or child_alive_observed_ns is None:
        raise HarnessRefusal("exact backup child was not alive after the mutation")
    if backup_finished_ns <= backup_started_ns:
        raise HarnessRefusal("backup command timestamps are invalid")
    if not backup_started_ns <= child_alive_observed_ns <= backup_finished_ns:
        raise HarnessRefusal(
            "exact-child liveness timestamp is outside the backup window"
        )
    overlap = [
        timestamp
        for timestamp in mutation_completed_ns
        if backup_started_ns <= timestamp <= child_alive_observed_ns
    ]
    if not overlap:
        raise HarnessRefusal("no mutation completed during the backup command")
    return min(overlap)


_TRANSITIONS = {
    ("prepared", "bootstrap-started"),
    ("bootstrap-started", "bootstrap-complete"),
    ("bootstrap-complete", "recovery-started"),
    ("recovery-started", "complete"),
}


def recovery_transition(current: str, requested: str) -> str:
    if (current, requested) not in _TRANSITIONS:
        raise HarnessRefusal(
            f"unsafe {current or '<empty>'} -> {requested} transition; use the manual recovery route"
        )
    return requested


def duplicate_process_refusal(exit_code: int, output: str) -> str:
    """Require one failed-lifecycle event and kubectl's exact exit diagnostic."""
    if type(exit_code) is not int or exit_code != 3:
        raise HarnessRefusal("duplicate Cairn process did not exit with status 3")
    lines = output.splitlines()
    if len(lines) != 2:
        raise HarnessRefusal("duplicate Cairn process output is ambiguous")
    if lines[1] != "command terminated with exit code 3":
        raise HarnessRefusal("duplicate Cairn process kubectl diagnostic is unexpected")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise HarnessRefusal("duplicate Cairn process output is not JSON") from error
    if not isinstance(payload, dict) or (
        payload.get("event") != "runtime_start_failed"
        or payload.get("failure_code") != "already_locked"
    ):
        raise HarnessRefusal("duplicate Cairn process lacks lock-refusal evidence")
    return "already_locked"


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    ownership = subparsers.add_parser("validate-ownership")
    ownership.add_argument("run_id")
    ownership.add_argument("revision")
    invocation = subparsers.add_parser("invocation-action")
    invocation.add_argument("mode")
    invocation.add_argument("report_exists", choices=("true", "false"))
    invocation.add_argument("phase")
    transition = subparsers.add_parser("transition")
    transition.add_argument("current")
    transition.add_argument("requested")
    report = subparsers.add_parser("write-report")
    report.add_argument("path", type=Path)
    subparsers.add_parser("build-report")
    subparsers.add_parser("extract-bootstrap-token")
    subparsers.add_parser("rwop-current")
    subparsers.add_parser("validate-storage")
    subparsers.add_parser("claim-bindings")
    subparsers.add_parser("mutable-resources")
    subparsers.add_parser("validate-backup-overlap")
    canonical = subparsers.add_parser("canonical-image-reference")
    canonical.add_argument("reference")
    imported = subparsers.add_parser("resolve-imported-image")
    imported.add_argument("requested")
    containerd = subparsers.add_parser("resolve-containerd-image")
    containerd.add_argument("requested")
    provenance = subparsers.add_parser("resolve-oci-provenance")
    provenance.add_argument("requested")
    runtime = subparsers.add_parser("runtime-image-digest")
    runtime.add_argument("image_id")
    runtime_config = subparsers.add_parser("runtime-config-digest")
    runtime_config.add_argument("image_id")
    runtime_config.add_argument("expected_config_digest")
    duplicate = subparsers.add_parser("duplicate-process-refusal")
    duplicate.add_argument("exit_code", type=int)
    subparsers.add_parser("collect-cilium-images")
    supplement = subparsers.add_parser("write-cilium-supplement")
    supplement.add_argument("--primary-report", type=Path, required=True)
    supplement.add_argument("--primary-sha256", required=True)
    supplement.add_argument("--run-id", required=True)
    supplement.add_argument("--target-revision", required=True)
    supplement.add_argument("--collector-revision", required=True)
    supplement.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-ownership":
            validate_inventory_ownership(
                json.load(sys.stdin), args.run_id, args.revision
            )
        elif args.command == "invocation-action":
            print(
                invocation_action(
                    args.mode,
                    report_exists=args.report_exists == "true",
                    durable_phase=args.phase,
                )
            )
        elif args.command == "transition":
            print(recovery_transition(args.current, args.requested))
        elif args.command == "write-report":
            write_report_exclusive(args.path, json.load(sys.stdin))
        elif args.command == "build-report":
            json.dump(build_report(**json.load(sys.stdin)), sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "extract-bootstrap-token":
            tokens = []
            for line in sys.stdin:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = payload.get("token")
                if payload.get("operation") == "bootstrap" and isinstance(token, str):
                    tokens.append(token)
            if len(tokens) != 1 or not tokens[0].startswith("cairn1."):
                raise HarnessRefusal("bootstrap returned no unique credential")
            sys.stdout.write(tokens[0])
        elif args.command == "rwop-current":
            payload = json.load(sys.stdin)
            if not rwop_refusal_is_current(payload["pod"], payload["events"]):
                raise HarnessRefusal("current Pod lacks UID-bound RWOP refusal")
        elif args.command == "validate-storage":
            payload = json.load(sys.stdin)
            validate_storage_inventory(payload["mounts"], payload["pvs"])
        elif args.command == "claim-bindings":
            payload = json.load(sys.stdin)
            json.dump(claim_bindings(payload["claims"], payload["pvs"]), sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "mutable-resources":
            print(",".join(MUTABLE_NAMESPACED_RESOURCES))
        elif args.command == "validate-backup-overlap":
            payload = json.load(sys.stdin)
            backup_command_overlap(**payload)
            print("mutation-completed-during-backup-command")
        elif args.command == "canonical-image-reference":
            print(canonical_image_reference(args.reference))
        elif args.command == "resolve-imported-image":
            payload = json.load(sys.stdin)
            if not isinstance(payload, list) or not all(
                isinstance(image, dict) for image in payload
            ):
                raise HarnessRefusal("containerd image inventory is malformed")
            json.dump(resolve_imported_image(args.requested, payload), sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "resolve-containerd-image":
            json.dump(resolve_containerd_image(args.requested), sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "resolve-oci-provenance":
            json.dump(resolve_oci_provenance(args.requested), sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "runtime-image-digest":
            print(runtime_image_digest(args.image_id))
        elif args.command == "runtime-config-digest":
            print(runtime_config_digest(args.image_id, args.expected_config_digest))
        elif args.command == "duplicate-process-refusal":
            print(duplicate_process_refusal(args.exit_code, sys.stdin.read()))
        elif args.command == "collect-cilium-images":
            _chart, expected_images = _locked_cilium_identities()
            workloads, replicasets, pods = _live_cilium_inventory()
            json.dump(
                cilium_image_evidence(workloads, replicasets, pods, expected_images),
                sys.stdout,
            )
            sys.stdout.write("\n")
        elif args.command == "write-cilium-supplement":
            if args.output.exists():
                raise HarnessRefusal("Cilium supplement output already exists")
            try:
                primary_bytes = args.primary_report.read_bytes()
            except OSError as error:
                raise HarnessRefusal("primary report is unreadable") from error
            expected_chart, expected_images = _locked_cilium_identities()
            _validate_primary_report(
                primary_bytes,
                primary_sha256=args.primary_sha256,
                run_id=args.run_id,
                target_revision=args.target_revision,
                expected_chart=expected_chart,
            )
            releases = _checked_json_command(
                [
                    "helm",
                    "list",
                    "--namespace",
                    "kube-system",
                    "--output",
                    "json",
                ]
            )
            if not isinstance(releases, list):
                raise HarnessRefusal("live Helm inventory is malformed")
            workloads, replicasets, pods = _live_cilium_inventory()
            collected_at = (
                datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            )
            supplement_report = build_cilium_supplement(
                primary_bytes,
                primary_sha256=args.primary_sha256,
                run_id=args.run_id,
                target_revision=args.target_revision,
                collector_revision=args.collector_revision,
                helm_releases=releases,
                workloads=workloads,
                replicasets=replicasets,
                pods=pods,
                expected_chart=expected_chart,
                expected_images=expected_images,
                collected_at=collected_at,
            )
            try:
                write_report_exclusive(args.output, supplement_report)
            except FileExistsError as error:
                raise HarnessRefusal(
                    "Cilium supplement output already exists"
                ) from error
            print(f"Cilium supplement written to {args.output}")
    except (HarnessRefusal, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
