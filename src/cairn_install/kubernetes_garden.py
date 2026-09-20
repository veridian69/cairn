"""Optional Garden resources and the guarded, resumable pod-template transition."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from cairn_install.core import InstallError, read_owned
from cairn_install.kubernetes_endpoint import EndpointProcess

if TYPE_CHECKING:
    from cairn_install.kubernetes import Backend


GARDEN_LABEL = "cairn.example.invalid/garden"
GARDEN_OBJECTS = frozenset(
    {
        "persistentvolumeclaim/garden-data",
        "configmap/garden-config",
        "secret/garden-tls",
        "service/garden",
        "networkpolicy/garden-ingress",
    }
)


def normal_template(template: dict[str, Any]) -> dict[str, Any]:
    """Remove only known API defaults and the installer's restart annotation."""
    value = deepcopy(template)
    metadata = value.setdefault("metadata", {})
    if metadata.get("creationTimestamp") is None:
        metadata.pop("creationTimestamp", None)
    annotations = metadata.get("annotations", {})
    annotations.pop("kubectl.kubernetes.io/restartedAt", None)
    if not annotations:
        metadata.pop("annotations", None)
    spec = value["spec"]
    defaults = {
        "restartPolicy": "Always",
        "dnsPolicy": "ClusterFirst",
        "schedulerName": "default-scheduler",
        "enableServiceLinks": True,
        "serviceAccount": spec.get("serviceAccountName"),
    }
    for key, default in defaults.items():
        if spec.get(key) == default:
            spec.pop(key, None)
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        for key, default in {
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
            "resources": {},
        }.items():
            if container.get(key) == default:
                container.pop(key, None)
        # Kubernetes defaults every probe separately, including the newly added
        # Garden TCP probes. Remove exact defaults on both sides of comparison;
        # changed thresholds, handlers and HTTP schemes remain visible as drift.
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container.get(name)
            if probe is None:
                continue
            for key, default in {
                "timeoutSeconds": 1,
                "periodSeconds": 10,
                "successThreshold": 1,
                "failureThreshold": 3,
            }.items():
                if probe.get(key) == default:
                    probe.pop(key)
            if probe.get("httpGet", {}).get("scheme") == "HTTP":
                probe["httpGet"].pop("scheme")
        for port in container.get("ports", []):
            if port.get("protocol") == "TCP":
                port.pop("protocol", None)
    for volume in spec.get("volumes", []):
        for key in ("configMap", "secret"):
            if volume.get(key, {}).get("defaultMode") == 0o644:
                volume[key].pop("defaultMode")
    return value


def sidecar_template(
    original: dict[str, Any], options: dict[str, Any], image_policy: str
) -> dict[str, Any]:
    template = deepcopy(original)
    template["metadata"].setdefault("labels", {})[GARDEN_LABEL] = "enabled"
    spec = template["spec"]
    # Match the exact-image/PVC preflight probe. Repeated recursive fsGroup
    # fixup would broaden Garden's private 0600 lock/state files on each restart.
    spec.setdefault("securityContext", {}).update(
        runAsUser=65532,
        runAsGroup=65532,
        fsGroup=65532,
        fsGroupChangePolicy="OnRootMismatch",
    )
    port = options["port"]
    spec["containers"].append(
        {
            "name": "garden",
            "image": options["image"],
            "imagePullPolicy": image_policy,
            "command": [
                "/usr/local/bin/a2a",
                "host",
                "--config",
                "/etc/garden/host.json",
            ],
            "ports": [{"name": "garden", "containerPort": port, "protocol": "TCP"}],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {"name": "garden-data", "mountPath": "/var/lib/garden"},
                {"name": "garden-config", "mountPath": "/etc/garden", "readOnly": True},
                {
                    "name": "garden-tls",
                    "mountPath": "/etc/garden-tls",
                    "readOnly": True,
                },
            ],
            "startupProbe": {
                "tcpSocket": {"port": "garden"},
                "periodSeconds": 2,
                "failureThreshold": 60,
            },
            "readinessProbe": {"tcpSocket": {"port": "garden"}, "periodSeconds": 5},
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"memory": "512Mi"},
            },
        }
    )
    spec["volumes"].extend(
        [
            {
                "name": "garden-data",
                "persistentVolumeClaim": {"claimName": "garden-data"},
            },
            {"name": "garden-config", "configMap": {"name": "garden-config"}},
            {
                "name": "garden-tls",
                "secret": {"secretName": "garden-tls", "defaultMode": 0o440},
            },
        ]
    )
    return template


class GardenDeployment:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.ctx = backend.ctx
        state = self.ctx.state.get("garden")
        self.options: dict[str, Any] | None = None
        if state is None:
            self.record: dict[str, Any] = {}
            return
        if not isinstance(state, dict) or not isinstance(state.get("options"), dict):
            raise InstallError("Invalid Garden options")
        self.options = deepcopy(state["options"])
        if (
            not isinstance(self.options.get("image"), str)
            or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", self.options["image"])
            or type(self.options.get("port")) is not int
            or not 1024 <= self.options["port"] <= 65535
            or self.options["port"] in {8000, self.ctx.port}
            or self.options.get("kubernetes_service_type", "ClusterIP")
            not in {"ClusterIP", "LoadBalancer"}
            or not isinstance(self.options.get("allowed_cidrs"), list)
        ):
            raise InstallError(
                "Invalid Kubernetes Garden image, port, Service type or ingress options"
            )
        try:
            for cidr in self.options["allowed_cidrs"]:
                if not isinstance(cidr, str):
                    raise ValueError("CIDR must be a string")
                ipaddress.ip_network(cidr, strict=True)
        except ValueError as error:
            raise InstallError("Invalid Garden ingress CIDR") from error
        self.record = backend.record.setdefault(
            "garden", {"options": deepcopy(self.options)}
        )
        if self.record.get("options") != self.options:
            raise InstallError("Recorded Garden options changed")
        self.endpoint = EndpointProcess(
            self.ctx,
            self.record,
            local_port=self.options["port"],
            remote_port=self.options["port"],
        )

    def metadata(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "namespace": self.backend.namespace,
            "labels": dict(self.backend.owner_labels),
        }

    def claim(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": self.metadata("garden-data"),
            "spec": {
                "accessModes": ["ReadWriteOncePod"],
                "storageClassName": self.backend.options["storage_class"],
                "resources": {"requests": {"storage": "10Gi"}},
                "volumeMode": "Filesystem",
            },
        }

    def prepare(self) -> None:
        if self.options is None:
            return
        from cairn_install.garden import gateway_config

        backend = self.backend
        backend.validate_ownership()
        backend._load_assets()
        port = self.options["port"]
        cfg = gateway_config(
            self.ctx,
            data_dir="/var/lib/garden/data",
            cert_file="/etc/garden-tls/tls.crt",
            key_file="/etc/garden-tls/tls.key",
            daemon_url_file="/var/lib/garden/run/daemon.url",
            listen=f"0.0.0.0:{port}",
            cairn_url="http://127.0.0.1:8000/memory/v1/diagnose",
        )
        host = {"gateway": cfg}
        content = json.dumps(host, sort_keys=True)
        tls = {}
        for target, name in (("tls.crt", "server.crt"), ("tls.key", "server.key")):
            path = self.ctx.root / "garden" / "tls" / name
            self.ctx.check_file(path)
            tls[target] = base64.b64encode(read_owned(path)).decode()
        # Config/TLS remain immutable for the same installation; a matching UID
        # alone never means changed configuration was applied successfully.
        fingerprint = hashlib.sha256(
            json.dumps({"host": host, "tls": tls}, sort_keys=True).encode()
        ).hexdigest()
        if self.record.get("configuration_digest", fingerprint) != fingerprint:
            raise InstallError(
                "Garden configuration changed; explicit replacement is required"
            )
        self.record["configuration_digest"] = fingerprint
        self.ctx.save()
        selector = {
            "app.kubernetes.io/name": "cairn",
            "app.kubernetes.io/instance": self.ctx.name,
            GARDEN_LABEL: "enabled",
        }
        service_spec: dict[str, Any] = {
            "type": self.options.get("kubernetes_service_type", "ClusterIP"),
            "selector": selector,
            "ports": [
                {
                    "name": "garden",
                    "port": port,
                    "targetPort": "garden",
                    "protocol": "TCP",
                }
            ],
        }
        cidrs = self.options["allowed_cidrs"]
        if service_spec["type"] == "LoadBalancer":
            service_spec["loadBalancerSourceRanges"] = cidrs
        documents = [
            self.claim(),
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": self.metadata("garden-tls"),
                "type": "kubernetes.io/tls",
                "data": tls,
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": self.metadata("garden-config"),
                "data": {"host.json": content},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": self.metadata("garden"),
                "spec": service_spec,
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": self.metadata("garden-ingress"),
                "spec": {
                    "podSelector": {"matchLabels": selector},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"ipBlock": {"cidr": cidr}} for cidr in cidrs],
                            "ports": [{"protocol": "TCP", "port": port}],
                        }
                    ]
                    if cidrs
                    else [],
                },
            },
        ]
        # Check every existing Garden document before creating any new one.
        for doc in documents:
            observed = backend.resources.get(backend._key(doc))
            if observed is not None:
                backend.resources.owned(backend._key(doc), observed)
                for key in ("data", "type"):
                    if key in doc and observed.get(key) != doc[key]:
                        raise InstallError(
                            "Garden resource configuration drift: " + backend._key(doc)
                        )
                if (
                    doc["kind"] == "NetworkPolicy"
                    and observed.get("spec") != doc["spec"]
                ):
                    raise InstallError("Garden network policy drift")
                if doc["kind"] == "Service":
                    actual = observed.get("spec", {})
                    for key in ("type", "selector", "loadBalancerSourceRanges"):
                        if actual.get(key) != doc["spec"].get(key):
                            raise InstallError("Garden service drift")
                    if [
                        {
                            key: p.get(key)
                            for key in ("name", "port", "targetPort", "protocol")
                        }
                        for p in actual.get("ports", [])
                    ] != service_spec["ports"]:
                        raise InstallError("Garden service ports drift")
        for doc in documents:
            backend.resources.create(doc)
        self.attach()

    def attach(self) -> None:
        if self.options is None:
            return
        backend = self.backend
        observed = backend.resources.get("statefulset/cairn")
        if observed is None:
            raise InstallError("Cairn must exist before attaching Garden")
        backend.resources.owned("statefulset/cairn", observed)
        current = observed["spec"]["template"]
        transition = self.record.get("transition")
        if transition is None:
            source = next(
                doc
                for doc in backend.documents
                if doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == "cairn"
            )
            if normal_template(current) != normal_template(source["spec"]["template"]):
                raise InstallError(
                    "Cairn pod template changed before Garden attachment"
                )
            transition = {
                "uid": observed["metadata"]["uid"],
                "before": deepcopy(current),
                "after": sidecar_template(
                    current,
                    self.options,
                    ("Never" if isinstance(backend.garden_receipt, dict) else "Always"),
                ),
                "phase": "pending",
            }
            self.record["transition"] = transition
            self.ctx.save()
        if transition.get("uid") != observed["metadata"]["uid"]:
            raise InstallError("Garden attachment StatefulSet UID changed")
        if normal_template(current) == normal_template(transition["after"]):
            transition["phase"] = "attached"
            self.ctx.save()
            return
        if transition.get("phase") != "pending" or normal_template(
            current
        ) != normal_template(transition["before"]):
            raise InstallError("Cairn pod template drift during Garden attachment")
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": transition["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": observed["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/template", "value": current},
            {"op": "replace", "path": "/spec/template", "value": transition["after"]},
        ]
        backend._run(
            "patch", "statefulset/cairn", "--type=json", "-p", json.dumps(patch)
        )
        changed = backend.resources.get("statefulset/cairn")
        if changed is None:
            raise InstallError("Garden workload disappeared after patch")
        backend.resources.owned("statefulset/cairn", changed)
        if normal_template(changed["spec"]["template"]) != normal_template(
            transition["after"]
        ):
            raise InstallError("Garden template patch was not observed")
        transition["phase"] = "attached"
        self.ctx.save()

    def start(self) -> None:
        if self.options is None:
            return
        self.prepare()
        self.backend._scale("cairn", 1)
        self.backend._rollout("cairn")

    def open_endpoint(self) -> None:
        if self.options is None:
            return
        self.backend.validate_ownership()
        port = self.options["port"]
        self.endpoint.open(
            self.backend._argv(
                "port-forward",
                "service/garden",
                f"{port}:{port}",
                "--address",
                "127.0.0.1",
            )
        )

    def close_endpoint(self) -> None:
        if self.options is not None:
            self.endpoint.close()
