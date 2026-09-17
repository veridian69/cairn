"""Namespaced Kubernetes lifecycle with durable creation and UID ownership proofs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cairn_install.core import (
    Context,
    InstallError,
    read_owned,
    secure_directory,
)
from cairn_install.kubernetes_assets import site_inventory
from cairn_install.kubernetes_endpoint import EndpointProcess
from cairn_install.kubernetes_garden import GARDEN_OBJECTS, GardenDeployment
from cairn_install.kubernetes_resources import RESOURCE_APIS, ResourceJournal

_KUBERNETES_NODE = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")


def _falkordb_probe_name(node: str) -> str:
    return "falkordb-cache-" + hashlib.sha256(node.encode()).hexdigest()[:20]


class Backend:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        options = ctx.state.get("kubernetes")
        if not isinstance(options, dict):
            raise InstallError("Missing Kubernetes options")
        for name in ("context", "namespace", "storage_class", "image"):
            value = options.get(name)
            if (
                not isinstance(value, str)
                or not value
                or any(c.isspace() or ord(c) < 32 for c in value)
            ):
                raise InstallError("Invalid Kubernetes options")
        if not re.fullmatch(
            r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", options["namespace"]
        ):
            raise InstallError("Invalid Kubernetes namespace")
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", options["image"]):
            raise InstallError("Kubernetes image requires an immutable sha256 digest")
        if type(options.get("preloaded_image")) is not bool or options.get(
            "image_policy"
        ) != ("IfNotPresent" if options["preloaded_image"] else "Always"):
            raise InstallError("Invalid Kubernetes image policy")
        receipt = options.get("falkordb_receipt")
        if receipt is not None:
            if not ctx.semantic:
                raise InstallError("FalkorDB receipt requires semantic search")
            self._validate_falkordb_receipt(receipt)
        self.falkordb_receipt = deepcopy(receipt)
        self.options = dict(options)
        self.namespace = options["namespace"]
        self.prefix = ["kubectl", "--context", options["context"]]
        self.owner_labels = {
            "cairn.example.invalid/instance-id": ctx.instance_id,
            "cairn.example.invalid/run-id": ctx.run_id,
        }
        self.record = ctx.state.setdefault("resources", {}).setdefault(
            "kubernetes",
            {
                "options": dict(options),
                "owner_labels": self.owner_labels,
                "objects": {},
            },
        )
        if (
            self.record.get("options") != options
            or self.record.get("owner_labels") != self.owner_labels
        ):
            raise InstallError("Recorded Kubernetes options or ownership changed")
        if not isinstance(self.record.get("objects"), dict):
            raise InstallError("Invalid Kubernetes resource record")
        self.site_path = ctx.root / "kubernetes.yaml"
        self.holder_path = ctx.root / "kubernetes-holder.yaml"
        self.endpoint = EndpointProcess(ctx, self.record)
        self.garden = GardenDeployment(self)
        self.site = ""
        self.documents: list[dict[str, Any]] = []
        self.holder_document: dict[str, Any] = {}
        self.expected = set(site_inventory(ctx.semantic))
        self.expected.update(
            {
                "secret/cairn-credentials",
                "pod/cairn-bootstrap",
                "persistentvolumeclaim/data-cairn-0",
            }
        )
        self.probe_name = "cairn-probe-" + ctx.run_id.replace("-", "")[:24]
        self.expected.update(
            {"pod/" + self.probe_name, "persistentvolumeclaim/" + self.probe_name}
        )
        if ctx.semantic:
            self.expected.add("persistentvolumeclaim/data-falkordb-0")
        if isinstance(self.falkordb_receipt, dict):
            self.expected.update(
                "pod/" + _falkordb_probe_name(node["name"])
                for node in self.falkordb_receipt["nodes"]
            )
        if self.garden.options is not None:
            self.expected.update(GARDEN_OBJECTS)
        self.resources = ResourceJournal(
            ctx,
            record=self.record,
            expected=set(self.expected),
            namespace=self.namespace,
            owner_labels=self.owner_labels,
            command=self._run,
        )

    @staticmethod
    def _validate_falkordb_receipt(value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "image",
            "archive_sha256",
            "nodes",
        }:
            raise InstallError("Invalid Kubernetes FalkorDB receipt")
        nodes = value.get("nodes")
        image = value.get("image")
        archive = value.get("archive_sha256")
        if (
            type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or not isinstance(image, str)
            or re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image) is None
            or not isinstance(archive, str)
            or re.fullmatch(r"[0-9a-f]{64}", archive) is None
            or not isinstance(nodes, list)
            or not nodes
        ):
            raise InstallError("Invalid Kubernetes FalkorDB receipt")
        names: set[str] = set()
        uids: set[str] = set()
        for node in nodes:
            if (
                not isinstance(node, dict)
                or set(node) != {"name", "uid"}
                or not isinstance(node.get("name"), str)
                or _KUBERNETES_NODE.fullmatch(node["name"]) is None
                or not isinstance(node.get("uid"), str)
                or not node["uid"]
                or len(node["uid"]) > 128
                or any(
                    character.isspace() or ord(character) < 32
                    for character in node["uid"]
                )
                or node["name"] in names
                or node["uid"] in uids
            ):
                raise InstallError("Invalid Kubernetes FalkorDB receipt")
            names.add(node["name"])
            uids.add(node["uid"])

    @property
    def runtime_python(self) -> str:
        return sys.executable

    def _retained_assets(self) -> dict[str, str]:
        retained = {}
        for key, path in (("site", self.site_path), ("holder", self.holder_path)):
            if path.exists() or path.is_symlink():
                self.ctx.check_file(path)
                retained[key] = read_owned(path).decode()
            elif str(path) in self.ctx.state["owned_files"]:
                raise InstallError("Owned Kubernetes manifest is missing: " + str(path))
        return retained

    def _load_assets(self) -> None:
        retained = self._retained_assets()
        request: dict[str, Any] = {
            "namespace": self.namespace,
            "instance_name": self.ctx.name,
            "instance_id": self.ctx.instance_id,
            "image": self.options["image"],
            "storage_class": self.options["storage_class"],
            "semantic": self.ctx.semantic,
            "owner_labels": self.owner_labels,
            "image_policy": self.options["image_policy"],
            "garden_enabled": self.garden.options is not None,
            "falkordb_receipt": self.falkordb_receipt,
            **retained,
        }
        if "site" not in retained:
            name = (
                "kubernetes-retrieval.yaml" if self.ctx.semantic else "kubernetes.yaml"
            )
            request["raw"] = read_owned(
                self.ctx.source / "deploy/kustomize/rendered" / name
            ).decode()
        runtime = self.ctx.root / "kubernetes-runtime"
        secure_directory(runtime)
        output = self.ctx.command(
            [
                "uv",
                "run",
                "--locked",
                "--no-dev",
                "python",
                "-m",
                "cairn_install.kubernetes_assets",
            ],
            cwd=self.ctx.source,
            env={"UV_PROJECT_ENVIRONMENT": str(runtime)},
            stdin_data=json.dumps(request).encode(),
            private=True,
            timeout=900,
        )
        envelope = self._json(output)
        if (
            not isinstance(envelope.get("site"), str)
            or not isinstance(envelope.get("holder"), str)
            or not isinstance(envelope.get("documents"), list)
            or not all(isinstance(doc, dict) for doc in envelope["documents"])
            or not isinstance(envelope.get("holder_document"), dict)
            or {self._key(doc) for doc in envelope["documents"]}
            != site_inventory(self.ctx.semantic)
            or any(envelope.get(key) != value for key, value in retained.items())
        ):
            raise InstallError("Invalid Kubernetes asset helper response")
        self.ctx.write_file(self.site_path, envelope["site"])
        self.ctx.write_file(self.holder_path, envelope["holder"])
        self.site = envelope["site"]
        self.documents = envelope["documents"]
        self.holder_document = envelope["holder_document"]

    def _argv(self, *args: str, cluster: bool = False) -> list[str]:
        return (
            self.prefix
            + ([] if cluster else ["--namespace", self.namespace])
            + list(args)
        )

    def _run(
        self,
        *args: str,
        cluster: bool = False,
        timeout: float = 120,
        stdin: dict[str, Any] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"private": True, "timeout": timeout}
        if stdin is not None:
            kwargs["stdin_data"] = json.dumps(stdin).encode()
        return self.ctx.command(self._argv(*args, cluster=cluster), **kwargs)

    @staticmethod
    def _json(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except ValueError:
            raise InstallError("kubectl returned malformed JSON") from None
        if not isinstance(value, dict):
            raise InstallError("kubectl returned an unexpected document")
        return value

    @staticmethod
    def _key(doc: dict[str, Any]) -> str:
        return str(doc["kind"]).lower() + "/" + str(doc["metadata"]["name"])

    def _identity(self) -> None:
        value = self._json(
            self._run(
                "config",
                "view",
                "--minify",
                "--flatten",
                "--raw",
                "-o",
                "json",
                cluster=True,
            )
        )
        clusters = value.get("clusters", [])
        if len(clusters) != 1:
            raise InstallError("Selected Kubernetes context has no unique API server")
        server = clusters[0].get("cluster", {}).get("server")
        if (
            not isinstance(server, str)
            or urlsplit(server).scheme != "https"
            or not urlsplit(server).hostname
        ):
            raise InstallError("Kubernetes API server must use HTTPS")
        cluster = clusters[0]["cluster"]
        if cluster.get("insecure-skip-tls-verify"):
            raise InstallError("Kubernetes API server TLS verification is required")
        identity = hashlib.sha256(
            json.dumps(cluster, sort_keys=True).encode()
        ).hexdigest()
        if self.record.get("cluster_identity", identity) != identity:
            raise InstallError("Kubernetes API server TLS identity changed")
        namespace = self._json(
            self._run("get", "namespace", self.namespace, "-o", "json", cluster=True)
        )
        meta = namespace.get("metadata", {})
        uid = meta.get("uid")
        if (
            not uid
            or meta.get("labels", {}).get("cairn.example.invalid/instance")
            != self.ctx.name
        ):
            raise InstallError(
                "Namespace UID or administrator instance label is missing or changed"
            )
        for key, observed in (("api_server", server), ("namespace_uid", uid)):
            if self.record.get(key, observed) != observed:
                raise InstallError("Kubernetes API server or namespace UID changed")
        self.record.update(
            api_server=server, namespace_uid=uid, cluster_identity=identity
        )
        self.ctx.save()

    def validate_ownership(self) -> None:
        self._inventory()

    def _inventory(self, *, allow_absent: bool = False) -> None:
        self._retained_assets()
        self._identity()
        self.resources.inventory(allow_absent=allow_absent)

    def preflight(self) -> None:
        version = self.ctx.command(["uv", "--version"], cwd=self.ctx.directory).strip()
        if version.split()[:2] != ["uv", "0.12.14"]:
            raise InstallError(
                "uv 0.12.14 is required; found " + (version or "no version")
            )
        for name in ("pyproject.toml", "uv.lock"):
            read_owned(self.ctx.source / name)
        self._json(self._run("version", "--client", "-o", "json", cluster=True))
        self.validate_ownership()
        nodes = self._json(self._run("get", "nodes", "-o", "json", cluster=True)).get(
            "items", []
        )
        schedulable = [
            n
            for n in nodes
            if not n.get("spec", {}).get("unschedulable")
            and not any(
                t.get("effect") in {"NoSchedule", "NoExecute"}
                for t in n.get("spec", {}).get("taints", [])
            )
        ]
        ready = [
            n
            for n in schedulable
            if n.get("metadata", {}).get("labels", {}).get("kubernetes.io/os")
            == "linux"
            and n.get("metadata", {}).get("labels", {}).get("kubernetes.io/arch")
            == "amd64"
            and any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in n.get("status", {}).get("conditions", [])
            )
        ]
        if not ready:
            raise InstallError("A schedulable Ready linux/amd64 node is required")
        if isinstance(self.falkordb_receipt, dict):
            ready_by_name = {
                node.get("metadata", {}).get("name"): node for node in ready
            }
            for expected in self.falkordb_receipt["nodes"]:
                observed = ready_by_name.get(expected["name"])
                if (
                    observed is None
                    or observed.get("metadata", {}).get("uid") != expected["uid"]
                ):
                    raise InstallError(
                        "FalkorDB receipt node UID or eligibility changed: "
                        + expected["name"]
                    )
        if self.options["preloaded_image"]:
            if len(schedulable) != 1:
                raise InstallError(
                    "Preloaded image requires exactly one schedulable node"
                )
            images = ready[0].get("status", {}).get("images", [])
            if not any(
                self.options["image"] in item.get("names", []) for item in images
            ):
                raise InstallError(
                    "Exact preloaded digest is not recorded in the node image cache"
                )
            if self.garden.options is not None and not any(
                self.garden.options["image"] in item.get("names", []) for item in images
            ):
                raise InstallError(
                    "Exact preloaded Garden digest is not in the node image cache"
                )
        storage = self._json(
            self._run(
                "get",
                "storageclass",
                self.options["storage_class"],
                "-o",
                "json",
                cluster=True,
            )
        )
        driver = storage.get("provisioner")
        if (
            not isinstance(driver, str)
            or not driver
            or driver.startswith("kubernetes.io/")
        ):
            raise InstallError("A CSI StorageClass with RWOP support is required")
        registered = self._json(
            self._run("get", "csidriver", driver, "-o", "json", cluster=True)
        )
        if registered.get("metadata", {}).get("name") != driver:
            raise InstallError("StorageClass CSI driver is not registered")
        permissions = [
            (verb, plural, "")
            for _, plural in RESOURCE_APIS.values()
            for verb in ("get", "create", "delete")
        ]
        permissions += [
            ("patch", "statefulsets", ""),
            ("list", "statefulsets", ""),
            ("watch", "statefulsets", ""),
            ("list", "pods", ""),
            ("watch", "pods", ""),
            ("get", "statefulsets", "scale"),
            ("update", "statefulsets", "scale"),
            # Modern kubectl uses WebSocket GET, with SPDY POST fallback.
            ("get", "pods", "exec"),
            ("create", "pods", "exec"),
            ("get", "pods", "portforward"),
            ("create", "pods", "portforward"),
        ]
        for verb, resource, subresource in permissions:
            flags = ["--subresource=" + subresource] if subresource else []
            if self._run("auth", "can-i", verb, resource, *flags).strip() != "yes":
                raise InstallError(
                    "Kubernetes permission required: "
                    + verb
                    + " "
                    + resource
                    + ("/" + subresource if subresource else "")
                )
        self._probe()
        self._probe_falkordb_cache()

    def _probe(self) -> None:
        metadata = {
            "name": self.probe_name,
            "namespace": self.namespace,
            "labels": self.owner_labels
            | {
                "app.kubernetes.io/name": "cairn",
                "app.kubernetes.io/instance": self.ctx.name,
            },
        }
        claim = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": metadata,
            "spec": {
                "accessModes": ["ReadWriteOncePod"],
                "storageClassName": self.options["storage_class"],
                "resources": {"requests": {"storage": "1Mi"}},
            },
        }
        script = (
            "import pathlib,socket,subprocess; import cairn; "
            'subprocess.run(["cairn","--help"],check=True,timeout=20); '
            'pathlib.Path("/probe/check").write_text("rwop"); '
            'socket.getaddrinfo("kubernetes.default.svc.cluster.local",443); '
        )
        if self.ctx.semantic:
            script += (
                's=socket.create_connection(("cairn-egress-gateway.cairn-egress.svc.cluster.local",3128),10); '
                's.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\\r\\nHost: api.openai.com:443\\r\\n\\r\\n"); '
                'reply=s.recv(4096); s.close(); assert reply.split()[1]==b"200", "gateway CONNECT failed"; '
            )
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": metadata,
            "spec": {
                "restartPolicy": "Never",
                "activeDeadlineSeconds": 150,
                # Match NetworkPolicy peers, but never join the live Service.
                "readinessGates": [
                    {"conditionType": "cairn.example.invalid/probe-never-ready"}
                ],
                "automountServiceAccountToken": False,
                "nodeSelector": {
                    "kubernetes.io/os": "linux",
                    "kubernetes.io/arch": "amd64",
                },
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65532,
                    "fsGroup": 65532,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "probe",
                        "image": self.options["image"],
                        "imagePullPolicy": self.options["image_policy"],
                        "command": ["python", "-c", script],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [{"name": "probe", "mountPath": "/probe"}],
                    }
                ],
                "volumes": [
                    {
                        "name": "probe",
                        "persistentVolumeClaim": {"claimName": self.probe_name},
                    }
                ],
            },
        }
        if self.garden.options is not None:
            pod["spec"]["securityContext"]["fsGroupChangePolicy"] = "OnRootMismatch"
            pod["spec"]["containers"].append(
                {
                    "name": "garden-probe",
                    "image": self.garden.options["image"],
                    "imagePullPolicy": self.options["image_policy"],
                    "command": ["/usr/local/bin/a2a", "host", "--help"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "probe", "mountPath": "/var/lib/garden"}],
                }
            )
        keys = ["pod/" + self.probe_name, "persistentvolumeclaim/" + self.probe_name]
        # Clean an interrupted, owned probe before attempting it again.
        for key in keys:
            self.resources.delete(key)
        try:
            self.resources.create(claim)
            self.resources.create(pod)
            self._run(
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                keys[0],
                "--timeout=180s",
                timeout=190,
            )
            observed = self.resources.get(keys[0])
            if not observed:
                raise InstallError("Exact-image probe disappeared")
            self.resources.owned(keys[0], observed)
            statuses = observed.get("status", {}).get("containerStatuses", [])
            expected_images = {
                container["name"]: container["image"]
                for container in pod["spec"]["containers"]
            }
            if (
                {status.get("name") for status in statuses} != set(expected_images)
                or len(statuses) != len(expected_images)
                or any(
                    status.get("name") not in expected_images
                    or status.get("state", {}).get("terminated", {}).get("exitCode")
                    != 0
                    or expected_images[status["name"]].split("@", 1)[1]
                    not in status.get("imageID", "")
                    for status in statuses
                )
            ):
                raise InstallError(
                    "Exact image execution or RWOP/DNS/gateway probe failed"
                )
        finally:
            # Do not delete the PVC if the Pod could not be safely removed.
            for key in keys:
                self.resources.delete(key)

    def _probe_falkordb_cache(self) -> None:
        if not isinstance(self.falkordb_receipt, dict):
            return
        image = self.falkordb_receipt["image"]
        for node in self.falkordb_receipt["nodes"]:
            name = _falkordb_probe_name(node["name"])
            key = "pod/" + name
            metadata = {
                "name": name,
                "namespace": self.namespace,
                "labels": self.owner_labels
                | {
                    "app.kubernetes.io/name": "falkordb-cache-probe",
                    "app.kubernetes.io/instance": self.ctx.name,
                },
            }
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": metadata,
                "spec": {
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": 60,
                    "automountServiceAccountToken": False,
                    "nodeSelector": {
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                    },
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchFields": [
                                            {
                                                "key": "metadata.name",
                                                "operator": "In",
                                                "values": [node["name"]],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 0,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "falkordb-cache-probe",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "command": ["/bin/sh", "-c", "exit 0"],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            }
            self.resources.delete(key)
            try:
                self.resources.create(pod)
                self._run(
                    "wait",
                    "--for=jsonpath={.status.phase}=Succeeded",
                    key,
                    "--timeout=90s",
                    timeout=100,
                )
                observed = self.resources.get(key)
                if not observed:
                    raise InstallError("FalkorDB exact-image cache probe disappeared")
                self.resources.owned(key, observed)
                observed_spec = observed.get("spec", {})
                containers = observed_spec.get("containers", [])
                statuses = observed.get("status", {}).get("containerStatuses", [])
                if (
                    observed_spec.get("nodeName") != node["name"]
                    or len(containers) != 1
                    or containers[0].get("name") != "falkordb-cache-probe"
                    or containers[0].get("image") != image
                    or containers[0].get("imagePullPolicy") != "Never"
                    or len(statuses) != 1
                    or statuses[0].get("name") != "falkordb-cache-probe"
                    or not str(statuses[0].get("imageID", "")).endswith(
                        "@" + image.rsplit("@", 1)[1]
                    )
                    or statuses[0]
                    .get("state", {})
                    .get("terminated", {})
                    .get("exitCode")
                    != 0
                ):
                    raise InstallError("FalkorDB exact-image cache probe failed")
            finally:
                self.resources.delete(key)

    def _credentials(self) -> list[str]:
        if not self.ctx.semantic:
            return []
        provider = Path(str(self.ctx.state.get("provider_key_file", "")))
        expected = self.ctx.root / "credentials" / "openai-api-key"
        if provider != expected:
            raise InstallError("Semantic mode requires the owned copied provider key")
        self.ctx.check_file(provider)
        self.ctx.read_secret(provider)
        password_file = self.ctx.root / "credentials" / "falkordb-password"
        known = str(password_file) in self.ctx.state["owned_files"] or str(
            password_file
        ) in self.ctx.state.get("file_intents", {})
        if known or password_file.exists() or password_file.is_symlink():
            self.ctx.check_file(password_file)
            password = self.ctx.read_secret(password_file)
        else:
            password = secrets.token_hex(32)
            self.ctx.write_file(password_file, password + "\n", secret=True)
        config = self.ctx.root / "credentials" / "falkordb.conf"
        self.ctx.write_file(config, "requirepass " + password + "\n", secret=True)
        return [
            "--from-file=openai-api-key=" + str(provider),
            "--from-file=falkordb-password=" + str(password_file),
            "--from-file=falkordb.conf=" + str(config),
        ]

    def prepare(self) -> None:
        files = self._credentials()
        self.validate_ownership()
        self._load_assets()
        if self.garden.options is not None:
            self.resources.create(self.garden.claim())
        key = "secret/cairn-credentials"
        if not self.resources.get(key):
            secret = self._json(
                self._run(
                    "create",
                    "secret",
                    "generic",
                    "cairn-credentials",
                    *files,
                    "--dry-run=client",
                    "-o",
                    "json",
                )
            )
            secret["metadata"]["namespace"] = self.namespace
            secret["metadata"]["labels"] = self.owner_labels
            self.resources.create(secret)
        # Establish exact claim ownership before any controller can mount data.
        claims = []
        for doc in self.documents:
            if doc["kind"] == "StatefulSet":
                for template in doc["spec"]["volumeClaimTemplates"]:
                    metadata = deepcopy(template["metadata"])
                    metadata.update(
                        namespace=self.namespace,
                        name=metadata["name"] + "-" + doc["metadata"]["name"] + "-0",
                    )
                    claim = {
                        "apiVersion": "v1",
                        "kind": "PersistentVolumeClaim",
                        "metadata": metadata,
                        "spec": deepcopy(template["spec"]),
                    }
                    self.resources.create(claim)
                    claims.append(self._key(claim))
        for doc in self.documents:
            if doc["kind"] == "StatefulSet":
                for key in claims:
                    observed = self.resources.get(key)
                    if observed is None:
                        raise InstallError("Owned workload storage is missing: " + key)
                    self.resources.owned(key, observed)
            self.resources.create(doc)
        for name in ["falkordb", "cairn"] if self.ctx.semantic else ["cairn"]:
            self._rollout(name)
        self.validate_ownership()
        for key in self.expected:
            if key.startswith("persistentvolumeclaim/data-") and not self.record[
                "objects"
            ].get(key, {}).get("uid"):
                raise InstallError("Workload storage receipt is missing: " + key)
        self.record["prepared"] = True
        self.ctx.save()

    def _rollout(self, name: str) -> None:
        self._run(
            "rollout", "status", "statefulset/" + name, "--timeout=300s", timeout=310
        )

    def _scale(self, name: str, replicas: int) -> None:
        key = "statefulset/" + name
        observed = self.resources.get(key)
        if not observed:
            return
        self.resources.owned(key, observed)
        version = observed["metadata"].get("resourceVersion")
        if not version:
            raise InstallError("StatefulSet resource version is missing")
        self._run(
            "scale",
            key,
            "--replicas=" + str(replicas),
            "--resource-version=" + str(version),
        )

    def is_running(self) -> bool:
        self.validate_ownership()
        # A prepared workload needs holder reconciliation even after interruption
        # between scaling to zero and creating the exclusive lifecycle Pod.
        return self.resources.get("statefulset/cairn") is not None

    def stop(self) -> None:
        self.validate_ownership()
        self._scale("cairn", 0)
        self.resources.wait_deleted("pod/cairn-0")
        self._load_assets()
        self.resources.create(self.holder_document)
        self._run(
            "wait",
            "--for=condition=Ready",
            "pod/cairn-bootstrap",
            "--timeout=180s",
            timeout=190,
        )

    def lifecycle_argv(self, operation: str) -> list[str]:
        self.validate_ownership()
        if operation not in {"check-config", "migrate", "verify", "bootstrap"}:
            raise InstallError("Unsupported lifecycle operation")
        return self._argv(
            "exec",
            "pod/cairn-bootstrap",
            "--",
            "cairn",
            operation,
            "--config",
            "/etc/cairn/config.yaml",
        )

    def start(self) -> None:
        self.validate_ownership()
        self.resources.delete("pod/cairn-bootstrap")
        for name in ["falkordb", "cairn"] if self.ctx.semantic else ["cairn"]:
            self._scale(name, 1)
            self._rollout(name)

    def restart(self) -> None:
        self.validate_ownership()
        observed = self.resources.get("statefulset/cairn")
        if observed is None:
            raise InstallError("Cairn StatefulSet is missing")
        self.resources.owned("statefulset/cairn", observed)
        annotations = dict(
            observed["spec"]["template"]["metadata"].get("annotations", {})
        )
        annotations["kubectl.kubernetes.io/restartedAt"] = datetime.now(UTC).isoformat()
        patch = [
            {
                "op": "test",
                "path": "/metadata/uid",
                "value": observed["metadata"]["uid"],
            },
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": observed["metadata"]["resourceVersion"],
            },
            {
                "op": "add",
                "path": "/spec/template/metadata/annotations",
                "value": annotations,
            },
        ]
        self._run("patch", "statefulset/cairn", "--type=json", "-p", json.dumps(patch))
        self._rollout("cairn")

    def rollback(self) -> None:
        self.garden_close_endpoint()
        self.close_endpoint()
        self.validate_ownership()
        for name in ["cairn", "falkordb"] if self.ctx.semantic else ["cairn"]:
            self._scale(name, 0)
            self.resources.wait_deleted("pod/" + name + "-0")
        self.resources.delete("pod/cairn-bootstrap")

    def blitz(self) -> None:
        self.garden_close_endpoint()
        self.close_endpoint()
        # Full inventory validation precedes even the first destructive command.
        self._inventory(allow_absent=True)
        keys = list(self.record["objects"])
        keys.sort(
            key=lambda key: (
                0
                if key.startswith("pod/")
                else 1
                if key.startswith("statefulset/")
                else 3
                if key.startswith("persistentvolumeclaim/")
                else 2,
                key,
            )
        )
        for key in keys:
            self.resources.delete(key)

    def open_endpoint(self) -> None:
        self.close_endpoint()
        self.validate_ownership()
        self.endpoint.open(
            self._argv(
                "port-forward",
                "service/cairn",
                str(self.ctx.port) + ":8000",
                "--address",
                "127.0.0.1",
            )
        )

    def close_endpoint(self) -> None:
        self.endpoint.close()

    def close(self) -> None:
        """Persistent workloads remain available after the installer exits."""

    def wait_foreground(self) -> None:
        raise InstallError("Foreground mode requires a disposable installation")

    def garden_prepare(self) -> None:
        self.garden.prepare()

    def garden_start(self) -> None:
        self.garden.start()

    def garden_stop(self) -> None:
        # Garden shares Cairn's pod: ordinary stop/restart/rollback controls
        # both. Scaling here would leave restart's template patch at zero.
        self.garden_close_endpoint()

    def garden_open_endpoint(self) -> None:
        self.garden.open_endpoint()

    def garden_close_endpoint(self) -> None:
        self.garden.close_endpoint()
