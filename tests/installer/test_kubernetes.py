"""Installer behaviour at the external kubectl boundary, without a live cluster."""

from __future__ import annotations

import base64
import copy
import json
import shlex
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from cairn_install.core import Context, InstallError

IMAGE = "registry.example/cairn@sha256:" + "a" * 64
LABELS = {
    "cairn.example.invalid/instance-id": "11111111-1111-4111-8111-111111111111",
    "cairn.example.invalid/run-id": "22222222-2222-4222-8222-222222222222",
}


class Cluster(Context):
    """Keep real protected file/state handling; replace only external commands."""

    def __init__(self, tmp_path: Path, *, semantic: bool = False) -> None:
        directory = tmp_path / "journal"
        directory.mkdir(mode=0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        super().__init__(
            directory,
            {
                "name": "alpha",
                "mode": "kubernetes",
                "port": port,
                "semantic": semantic,
                "instance_id": LABELS["cairn.example.invalid/instance-id"],
                "run_id": LABELS["cairn.example.invalid/run-id"],
                "source": str(Path(__file__).resolve().parents[2]),
                "resources": {},
                "owned_files": {},
                "steps": {},
                "receipts": {},
                "kubernetes": {
                    "context": "test-context",
                    "namespace": "alpha",
                    "image": IMAGE,
                    "storage_class": "csi-test",
                    "preloaded_image": False,
                    "image_policy": "Always",
                },
            },
            -1,
        )
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.objects: dict[str, dict[str, Any]] = {}
        self.server = "https://test.invalid:6443"
        self.ca = "test-ca"
        self.namespace = {
            "metadata": {
                "uid": "namespace-uid",
                "labels": {"cairn.example.invalid/instance": "alpha"},
            }
        }
        self.nodes: list[dict[str, Any]] = [
            {
                "metadata": {
                    "name": "node1",
                    "uid": "uid-node1",
                    "labels": {
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                    },
                },
                "spec": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        ]
        self.uid_counts: dict[str, int] = {}
        self.uv_version = "uv 0.12.14"
        self.driver = "csi.example"
        self.allowed = True
        self.denied_permissions: set[tuple[str, str, str]] = set()
        self.fail_probe = False
        self.fail_rollout = False
        self.probe_failure_message: str | None = None
        self.falkordb_probe_mutation: str | None = None
        if semantic:
            self.state["kubernetes"]["falkordb_receipt"] = {
                "schema_version": 1,
                "image": "cairn.local/falkordb-runtime@sha256:" + "b" * 64,
                "archive_sha256": "c" * 64,
                "nodes": [{"name": "node1", "uid": "uid-node1"}],
            }
            provider = self.root / "credentials" / "openai-api-key"
            self.write_file(provider, "provider-secret-value\n", secret=True)
            self.state["provider_key_file"] = str(provider)

    def put(self, document: dict[str, Any]) -> None:
        doc = copy.deepcopy(document)
        key = doc["kind"].lower() + "/" + doc["metadata"]["name"]
        self.uid_counts[key] = self.uid_counts.get(key, 0) + 1
        suffix = "" if self.uid_counts[key] == 1 else "-" + str(self.uid_counts[key])
        doc["metadata"].setdefault(
            "uid", "uid-" + doc["kind"].lower() + "-" + doc["metadata"]["name"] + suffix
        )
        doc["metadata"]["resourceVersion"] = "123"
        if doc["kind"] == "Pod":
            terms = (
                doc.get("spec", {})
                .get("affinity", {})
                .get("nodeAffinity", {})
                .get("requiredDuringSchedulingIgnoredDuringExecution", {})
                .get("nodeSelectorTerms", [])
            )
            if terms and terms[0].get("matchFields"):
                doc["spec"]["nodeName"] = terms[0]["matchFields"][0]["values"][0]
            doc["status"] = {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": container["name"],
                        "imageID": container["image"],
                        "state": {"terminated": {"exitCode": 0}},
                    }
                    for container in doc.get("spec", {}).get("containers", [])
                ],
            }
            if doc["metadata"]["name"].startswith("falkordb-cache-"):
                if self.falkordb_probe_mutation == "image":
                    doc["spec"]["containers"][0]["image"] = "mutated.example/image:tag"
                elif self.falkordb_probe_mutation == "pull-policy":
                    doc["spec"]["containers"][0]["imagePullPolicy"] = "Always"
                elif self.falkordb_probe_mutation == "node":
                    doc["spec"]["nodeName"] = "other-node"
                elif self.falkordb_probe_mutation == "image-id":
                    doc["status"]["containerStatuses"][0]["imageID"] = (
                        "docker-pullable://wrong.example/image@sha256:" + "d" * 64
                    )
            if (self.fail_probe or self.probe_failure_message) and doc["metadata"][
                "name"
            ].startswith("cairn-probe-"):
                doc["status"] = {
                    "phase": "Failed",
                    "containerStatuses": [
                        {
                            "name": "probe",
                            "state": {
                                "terminated": {
                                    "exitCode": 1,
                                    "reason": "Error",
                                    "message": self.probe_failure_message
                                    or "probe failed",
                                }
                            },
                        }
                    ],
                }
        self.objects[key] = doc
        if doc["kind"] == "StatefulSet":
            for claim in doc["spec"]["volumeClaimTemplates"]:
                name = claim["metadata"]["name"] + "-" + doc["metadata"]["name"] + "-0"
                if "persistentvolumeclaim/" + name not in self.objects:
                    self.put(
                        {
                            "apiVersion": "v1",
                            "kind": "PersistentVolumeClaim",
                            "metadata": {
                                "name": name,
                                "labels": claim["metadata"]["labels"],
                            },
                            "spec": claim["spec"],
                        }
                    )

    def command(self, argv: Sequence[str], **kwargs: Any) -> str:
        args = list(argv)
        self.calls.append((args, kwargs))
        self.snapshots.append(copy.deepcopy(self.state))
        if args == ["uv", "--version"]:
            return self.uv_version
        if args[:2] == ["uv", "run"]:
            # The surrounding test command already uses the locked environment.
            # Exercise the real JSON/YAML subprocess boundary without syncing a
            # second runtime or touching the checkout's environment.
            result: subprocess.CompletedProcess[bytes] = subprocess.run(
                [sys.executable, "-m", "cairn_install.kubernetes_assets"],
                input=kwargs["stdin_data"],
                capture_output=True,
                cwd=kwargs["cwd"],
                timeout=20,
            )
            if result.returncode:
                raise InstallError(
                    "Kubernetes asset helper failed: " + result.stderr.decode()
                )
            return result.stdout.decode()
        # Explicit binding is checked for every invocation, not only happy-path calls.
        assert args[:3] == [
            "kubectl",
            "--context",
            self.state["kubernetes"]["context"],
        ]
        if "--namespace" in args:
            assert args[args.index("--namespace") + 1] == "alpha"
            args = args[args.index("--namespace") + 2 :]
        else:
            args = args[3:]
        if args[:2] == ["version", "--client"]:
            return json.dumps({"clientVersion": {"gitVersion": "v1.35.0"}})
        if args[:2] == ["config", "view"]:
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": self.server,
                                "certificate-authority-data": self.ca,
                            }
                        }
                    ]
                }
            )
        if args[:2] == ["get", "namespace"]:
            return json.dumps(self.namespace)
        if args[:2] == ["get", "nodes"]:
            return json.dumps({"items": self.nodes})
        if args[:2] == ["get", "node"]:
            return json.dumps(self.nodes[0])
        if args[:2] == ["get", "storageclass"]:
            return json.dumps({"provisioner": self.driver})
        if args[:2] == ["get", "csidriver"]:
            return json.dumps({"metadata": {"name": self.driver}})
        if args[:2] == ["auth", "can-i"]:
            # Real can-i parses resource/name independently of --subresource.
            resource = args[3].split("/", 1)[0]
            subresource = next(
                (
                    arg.split("=", 1)[1]
                    for arg in args
                    if arg.startswith("--subresource=")
                ),
                "",
            )
            allowed = (
                self.allowed
                and (args[2], resource, subresource) not in self.denied_permissions
            )
            return "yes\n" if allowed else "no\n"
        if args[:2] == ["create", "secret"]:
            data = {}
            for arg in args:
                if arg.startswith("--from-file="):
                    name, path = arg.removeprefix("--from-file=").split("=", 1)
                    data[name] = base64.b64encode(Path(path).read_bytes()).decode()
            return json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "cairn-credentials"},
                    "data": data,
                }
            )
        if args[:2] == ["create", "-f"]:
            for document in yaml.safe_load_all(kwargs["stdin_data"]):
                key = document["kind"].lower() + "/" + document["metadata"]["name"]
                if key in self.objects:
                    raise InstallError("AlreadyExists")
                self.put(document)
            return ""
        if args[0] == "get":
            obj = self.objects.get(args[1].lower())
            return json.dumps(obj) if obj else ""
        if args[:2] == ["delete", "--raw"]:
            uid = json.loads(kwargs["stdin_data"])["preconditions"]["uid"]
            for key, obj in list(self.objects.items()):
                if obj["metadata"]["uid"] == uid:
                    del self.objects[key]
            return "{}"
        if args[0] == "wait" and any("Succeeded" in arg for arg in args):
            if self.fail_probe:
                raise InstallError("probe failed")
            return ""
        if args[:2] == ["wait", "--for=delete"]:
            raise AssertionError("Deletion must use exact GET, not List/Watch")
        if args[0] == "scale" and "--replicas=0" in args:
            self.objects.pop("pod/" + args[1].split("/", 1)[1] + "-0", None)
            return ""
        if args[0] == "patch":
            document = self.objects[args[1]]
            patch = json.loads(args[args.index("-p") + 1])
            for operation in patch:
                parent = document
                parts = operation["path"].strip("/").split("/")
                for part in parts[:-1]:
                    parent = parent[part]
                if operation["op"] == "test":
                    if parent[parts[-1]] != operation["value"]:
                        raise InstallError("JSON patch test failed")
                else:
                    parent[parts[-1]] = copy.deepcopy(operation["value"])
            return ""
        if args[0] == "rollout" and self.fail_rollout:
            raise InstallError("Command failed with exit 1")
        if args[0] in {"wait", "rollout", "scale", "patch"}:
            return ""
        raise AssertionError(f"unexpected kubectl command: {args}")


def backend(ctx: Cluster) -> Any:
    from cairn_install.kubernetes import Backend

    return Backend(ctx)


def prepared(tmp_path: Path, *, semantic: bool = False) -> tuple[Cluster, Any]:
    ctx = Cluster(tmp_path, semantic=semantic)
    adapter = backend(ctx)
    adapter.preflight()
    adapter.prepare()
    return ctx, adapter


def mutations(ctx: Cluster) -> list[list[str]]:
    return [
        args
        for args, _ in ctx.calls
        if any(
            verb in args
            for verb in ("create", "delete", "scale", "restart", "apply", "replace")
        )
    ]


def test_rollout_failure_reports_the_exact_failing_container_state(
    tmp_path: Path,
) -> None:
    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    ctx.fail_rollout = True
    ctx.objects["pod/cairn-0"] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "cairn-0"},
        "status": {
            "phase": "Pending",
            "initContainerStatuses": [
                {
                    "name": "migrate",
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off restarting failed container",
                        }
                    },
                    "lastState": {
                        "terminated": {
                            "reason": "Error",
                            "exitCode": 3,
                            "message": "untrusted provider-secret-value",
                        }
                    },
                }
            ],
        },
    }

    with pytest.raises(InstallError) as failure:
        adapter._rollout("cairn")  # noqa: SLF001

    message = str(failure.value)
    assert "pod/cairn-0" in message
    assert "init container migrate" in message
    assert "CrashLoopBackOff" in message
    assert "last terminated Error (exit 3)" in message
    assert "untrusted" not in message
    assert "provider-secret-value" not in message


def test_rollout_failure_never_replays_untrusted_status_strings(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    ctx.fail_rollout = True
    canary = "provider-secret-value\n\x1b[2J"
    ctx.objects["pod/cairn-0"] = {
        "status": {
            "phase": canary,
            "containerStatuses": [
                {
                    "name": canary,
                    "state": {
                        "waiting": {"reason": canary, "message": canary},
                        "terminated": {
                            "reason": canary,
                            "message": canary,
                            "exitCode": 10**100,
                        },
                    },
                }
            ],
        }
    }

    with pytest.raises(InstallError) as failure:
        adapter._rollout("cairn")  # noqa: SLF001

    message = str(failure.value)
    assert "container unknown waiting; terminated" in message
    assert "provider-secret-value" not in message
    assert "\x1b" not in message
    assert str(10**100) not in message

    assert adapter._pod_failure_detail({"status": {"phase": canary}}) == (  # noqa: SLF001
        "status unavailable"
    )
    assert (
        adapter._pod_failure_detail(  # noqa: SLF001
            {
                "status": {
                    "phase": [],
                    "containerStatuses": [
                        {"name": [], "state": {"waiting": {"reason": []}}}
                    ],
                }
            }
        )
        == "container unknown waiting"
    )


def test_preflight_proves_exact_image_rwop_and_cleans_probe(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    adapter.preflight()
    assert adapter.runtime_python == sys.executable
    assert ctx.state["resources"]["kubernetes"]["api_server"] == ctx.server
    assert ctx.state["resources"]["kubernetes"]["namespace_uid"] == "namespace-uid"
    creates = [
        yaml.safe_load(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
    ]
    pod = next(doc for doc in creates if doc["kind"] == "Pod")
    pvc = next(doc for doc in creates if doc["kind"] == "PersistentVolumeClaim")
    assert pod["spec"]["containers"][0]["image"] == IMAGE
    assert pod["spec"]["containers"][0]["imagePullPolicy"] == "Always"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOncePod"]
    assert pvc["spec"]["storageClassName"] == "csi-test"
    assert not ctx.objects
    assert all(kw.get("timeout", 120) <= 360 for _, kw in ctx.calls)


@pytest.mark.parametrize(
    "changed",
    ["server", "namespace_uid", "namespace_label", "resource_uid", "resource_label"],
)
def test_drift_refuses_before_mutation(tmp_path: Path, changed: str) -> None:
    ctx, adapter = prepared(tmp_path)
    if changed == "server":
        ctx.server = "https://different.invalid"
    elif changed == "namespace_uid":
        ctx.namespace["metadata"]["uid"] = "recreated"
    elif changed == "namespace_label":
        ctx.namespace["metadata"]["labels"] = {}
    elif changed == "resource_uid":
        ctx.objects["service/cairn"]["metadata"]["uid"] = "recreated"
    else:
        ctx.objects["service/cairn"]["metadata"]["labels"] = {}
    ctx.calls.clear()
    with pytest.raises(InstallError):
        adapter.blitz()
    assert not mutations(ctx)


@pytest.mark.parametrize(
    "condition",
    [
        "no_ready_node",
        "multi_preloaded",
        "namespace_label",
        "no_csi",
        "permission",
        "conflict",
    ],
)
def test_preflight_refuses_missing_prerequisites(
    tmp_path: Path, condition: str
) -> None:
    ctx = Cluster(tmp_path)
    if condition == "no_ready_node":
        ctx.nodes[0]["status"]["conditions"][0]["status"] = "False"
    elif condition == "multi_preloaded":
        ctx.state["kubernetes"].update(
            preloaded_image=True, image_policy="IfNotPresent"
        )
        ctx.nodes *= 2
    elif condition == "namespace_label":
        ctx.namespace["metadata"]["labels"] = {}
    elif condition == "no_csi":
        ctx.driver = "kubernetes.io/no-provisioner"
    elif condition == "permission":
        ctx.allowed = False
    else:
        ctx.put(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "cairn", "labels": LABELS},
            }
        )
    with pytest.raises(InstallError):
        backend(ctx).preflight()
    assert not mutations(ctx)


def test_failed_probe_always_deletes_exact_probe_objects(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    ctx.fail_probe = True
    with pytest.raises(InstallError, match="probe failed"):
        backend(ctx).preflight()
    assert not ctx.objects
    assert (
        len([args for args, _ in ctx.calls if "--raw" in args and "delete" in args])
        == 2
    )


def test_failed_semantic_probe_reports_safe_gateway_remediation_without_waiting(
    tmp_path: Path,
) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.probe_failure_message = (
        "shared egress gateway cairn-egress-gateway.cairn-egress:3128 is not "
        "resolvable; install docs/operations/kubernetes-gateway.md"
    )

    with pytest.raises(InstallError, match="shared egress gateway"):
        backend(ctx).preflight()

    assert not any("wait" in args for args, _ in ctx.calls)
    assert not ctx.objects


def test_failed_probe_never_replays_untrusted_termination_message(
    tmp_path: Path,
) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.probe_failure_message = (
        "shared egress gateway cairn-egress-gateway.cairn-egress:3128 is not "
        "resolvable; install docs/operations/kubernetes-gateway.md provider-secret"
    )

    with pytest.raises(InstallError) as caught:
        backend(ctx).preflight()

    assert "provider-secret" not in str(caught.value)
    assert "Exact-image probe failed" in str(caught.value)


def test_prepare_protects_credentials_and_records_creation_before_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _ = prepared(tmp_path, semantic=True)
    secret = ctx.objects["secret/cairn-credentials"]
    assert secret["metadata"]["labels"].items() >= LABELS.items()
    assert (
        base64.b64decode(secret["data"]["openai-api-key"]) == b"provider-secret-value\n"
    )
    password = base64.b64decode(secret["data"]["falkordb-password"]).decode().strip()
    assert (
        base64.b64decode(secret["data"]["falkordb.conf"]).decode()
        == "requirepass " + password + "\n"
    )
    public = json.dumps(ctx.state) + "\n".join(" ".join(args) for args, _ in ctx.calls)
    public += (ctx.directory / "commands.log").read_text() + capsys.readouterr().out
    for path in ctx.root.glob("*.yaml"):
        public += path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
    for value in ("provider-secret-value", password, *secret["data"].values()):
        assert value not in public
    for (args, kw), state in zip(ctx.calls, ctx.snapshots, strict=True):
        if args[-3:] == ["create", "-f", "-"]:
            doc = yaml.safe_load(kw["stdin_data"])
            key = doc["kind"].lower() + "/" + doc["metadata"]["name"]
            assert state["resources"]["kubernetes"]["objects"][key]["intent"] is True
            assert kw["private"] is True
    assert (
        "persistentvolumeclaim/data-cairn-0"
        in ctx.state["resources"]["kubernetes"]["objects"]
    )
    assert all(
        obj.get("uid")
        for obj in ctx.state["resources"]["kubernetes"]["objects"].values()
        if obj["name"] in {"cairn", "data-cairn-0", "cairn-credentials"}
    )


def test_holder_exclusivity_and_lifecycle_commands(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path, semantic=True)
    ctx.put(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "cairn-0", "labels": LABELS},
        }
    )
    ctx.calls.clear()
    adapter.stop()
    commands = [args for args, _ in ctx.calls]
    scale = next(i for i, args in enumerate(commands) if "scale" in args)
    wait = next(i for i, args in enumerate(commands) if "pod/cairn-0" in args)
    create = next(
        i for i, args in enumerate(commands) if args[-3:] == ["create", "-f", "-"]
    )
    assert scale < wait < create
    assert not any(
        "statefulset/falkordb" in args and "scale" in args for args in commands
    )
    assert adapter.lifecycle_argv("migrate") == [
        "kubectl",
        "--context",
        "test-context",
        "--namespace",
        "alpha",
        "exec",
        "pod/cairn-bootstrap",
        "--",
        "cairn",
        "migrate",
        "--config",
        "/etc/cairn/config.yaml",
    ]
    ctx.calls.clear()
    adapter.start()
    commands = [args for args, _ in ctx.calls]
    delete = next(i for i, args in enumerate(commands) if "delete" in args)
    scale = next(i for i, args in enumerate(commands) if "scale" in args)
    assert delete < scale
    assert "pod/cairn-bootstrap" not in ctx.objects
    ctx.calls.clear()
    adapter.restart()
    assert any("patch" in args and "statefulset/cairn" in args for args, _ in ctx.calls)
    assert not any(
        "patch" in args and "statefulset/falkordb" in args for args, _ in ctx.calls
    )


def test_rollback_retains_data_and_blitz_is_retryable(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path, semantic=True)
    adapter.stop()
    adapter.rollback()
    assert "pod/cairn-bootstrap" not in ctx.objects
    assert "persistentvolumeclaim/data-cairn-0" in ctx.objects
    assert "secret/cairn-credentials" in ctx.objects
    ctx.calls.clear()
    adapter.blitz()
    assert not ctx.objects
    deletes = [(args, kw) for args, kw in ctx.calls if "delete" in args]
    assert deletes
    assert all(
        "/namespaces/alpha/" in args[args.index("--raw") + 1] for args, _ in deletes
    )
    assert all(
        json.loads(kw["stdin_data"])["preconditions"]["uid"] for _, kw in deletes
    )
    adapter.blitz()


def test_resume_accepts_only_recorded_creation_intent(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    record = ctx.state["resources"]["kubernetes"]["objects"]["service/cairn"]
    del record["uid"]  # command succeeded before receipt save
    record.update(phase="creating", intent=True)
    adapter.validate_ownership()
    assert record["uid"] == "uid-service-cairn"
    del record["uid"]
    record["intent"] = False
    with pytest.raises(InstallError):
        adapter.validate_ownership()


def test_immutable_options_rejected_by_backend(tmp_path: Path) -> None:
    ctx, _ = prepared(tmp_path)
    ctx.state["kubernetes"]["image"] = "registry.example/other@sha256:" + "b" * 64
    with pytest.raises(InstallError):
        backend(ctx)


def test_lost_password_is_not_regenerated(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path, semantic=True)
    (ctx.root / "credentials" / "falkordb-password").unlink()
    ctx.calls.clear()
    with pytest.raises(InstallError):
        adapter.prepare()
    assert not mutations(ctx)


def install_tunnel_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> None:
    import os

    binary = tmp_path / "kubectl"
    binary.write_text(
        "#!"
        + sys.executable
        + "\n"
        + (
            "raise SystemExit(7)\n"
            if fail
            else "import socket,sys,time\n"
            'port,remote=map(int,next(a for a in sys.argv if a.count(":")==1 and a.split(":")[0].isdigit()).split(":"))\n'
            's=socket.socket(); s.bind(("127.0.0.1",port)); s.listen()\n'
            'print("Forwarding from 127.0.0.1:"+str(port)+" -> "+str(remote),flush=True)\n'
            "time.sleep(300)\n"
        )
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])


def test_endpoint_binds_loopback_and_cleans_process_and_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    install_tunnel_binary(tmp_path, monkeypatch)
    ctx, adapter = prepared(tmp_path)
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        ctx.state["port"] = port.getsockname()[1]
    adapter.open_endpoint()
    record = ctx.state["resources"]["kubernetes"]["endpoint"]
    assert record["argv"][-2:] == ["--address", "127.0.0.1"]
    assert record["pid"] > 0 and record["start_time"] and record["boot_id"]
    with socket.create_connection(("127.0.0.1", ctx.port), timeout=1):
        pass
    adapter.close_endpoint()
    assert "endpoint" not in ctx.state["resources"]["kubernetes"]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", ctx.port), timeout=0.2)


def test_endpoint_start_failure_does_not_leave_process_or_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_tunnel_binary(tmp_path, monkeypatch, fail=True)
    ctx, adapter = prepared(tmp_path)
    with pytest.raises(InstallError, match="port-forward"):
        adapter.open_endpoint()
    assert "endpoint" not in ctx.state["resources"]["kubernetes"]


def test_terminated_object_can_be_deleted_on_retry(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    ctx.objects["service/cairn"]["metadata"]["deletionTimestamp"] = (
        "2026-09-17T00:00:00Z"
    )
    adapter.blitz()
    assert not ctx.objects


def test_preloaded_cairn_uses_its_exact_pod_probe_while_garden_pulls_normally(
    garden_cluster: Cluster,
) -> None:
    """Node image inventory is advisory; only Cairn opts into the exception."""
    ctx = garden_cluster
    ctx.state["kubernetes"].update(preloaded_image=True, image_policy="IfNotPresent")
    adapter = backend(ctx)

    adapter.preflight()

    probe = next(
        json.loads(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
        and json.loads(kw["stdin_data"])["kind"] == "Pod"
    )
    assert {
        container["name"]: container["imagePullPolicy"]
        for container in probe["spec"]["containers"]
    } == {"probe": "IfNotPresent", "garden-probe": "Always"}

    adapter.prepare()
    adapter.garden_prepare()
    workload = ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"]
    assert {
        container["name"]: container["imagePullPolicy"]
        for container in workload["containers"]
    } == {"cairn": "IfNotPresent", "garden": "Always"}


def test_falkordb_receipt_rejects_replaced_or_ineligible_nodes(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.nodes[0]["metadata"]["uid"] = "replacement-uid"
    ctx.state["kubernetes"]["falkordb_receipt"] = {
        "schema_version": 1,
        "image": "registry.example/falkordb@sha256:" + "b" * 64,
        "archive_sha256": "c" * 64,
        "nodes": [{"name": "node1", "uid": "original-uid"}],
    }

    with pytest.raises(InstallError, match="UID or eligibility"):
        backend(ctx).preflight()


def test_falkordb_receipt_backend_rejects_boolean_schema_version(
    tmp_path: Path,
) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.state["kubernetes"]["falkordb_receipt"] = {
        "schema_version": True,
        "image": "registry.example/falkordb@sha256:" + "b" * 64,
        "archive_sha256": "c" * 64,
        "nodes": [{"name": "node1", "uid": "uid-node1"}],
    }

    with pytest.raises(InstallError, match="Invalid Kubernetes FalkorDB receipt"):
        backend(ctx)


def test_falkordb_receipt_probes_exact_never_pull_image_on_every_node(
    tmp_path: Path,
) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.nodes[0]["metadata"]["uid"] = "uid-node1"
    second = copy.deepcopy(ctx.nodes[0])
    second["metadata"].update(name="node2", uid="uid-node2")
    ctx.nodes.append(second)
    image = "registry.example/falkordb@sha256:" + "b" * 64
    ctx.state["kubernetes"]["falkordb_receipt"] = {
        "schema_version": 1,
        "image": image,
        "archive_sha256": "c" * 64,
        "nodes": [
            {"name": "node1", "uid": "uid-node1"},
            {"name": "node2", "uid": "uid-node2"},
        ],
    }

    adapter = backend(ctx)
    adapter.preflight()

    pods = [
        json.loads(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
        and json.loads(kw["stdin_data"])["kind"] == "Pod"
        and json.loads(kw["stdin_data"])["spec"]["containers"][0]["image"] == image
    ]
    assert len(pods) == 2
    assert {
        pod["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchFields"][0]["values"][0]
        for pod in pods
    } == {"node1", "node2"}
    assert all(
        pod["spec"]["containers"][0]["imagePullPolicy"] == "Never" for pod in pods
    )
    assert all(
        pod["spec"]["securityContext"]["runAsUser"] == 10001
        and pod["spec"]["securityContext"]["runAsGroup"] == 0
        for pod in pods
    )
    assert not any(key.startswith("pod/falkordb-cache-") for key in ctx.objects)


@pytest.mark.parametrize("mutation", ["image", "pull-policy", "node", "image-id"])
def test_falkordb_receipt_rejects_mutated_probe_attestation(
    tmp_path: Path, mutation: str
) -> None:
    ctx = Cluster(tmp_path, semantic=True)
    ctx.nodes[0]["metadata"]["uid"] = "uid-node1"
    ctx.state["kubernetes"]["falkordb_receipt"] = {
        "schema_version": 1,
        "image": "registry.example/falkordb@sha256:" + "b" * 64,
        "archive_sha256": "c" * 64,
        "nodes": [{"name": "node1", "uid": "uid-node1"}],
    }
    ctx.falkordb_probe_mutation = mutation

    with pytest.raises(InstallError, match="exact-image cache probe failed"):
        backend(ctx).preflight()
    assert not any(key.startswith("pod/falkordb-cache-") for key in ctx.objects)


def test_resumed_backend_cleans_verified_recorded_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_tunnel_binary(tmp_path, monkeypatch)
    ctx, adapter = prepared(tmp_path)
    adapter.open_endpoint()
    try:
        resumed = backend(ctx)
        resumed.close_endpoint()
        assert "endpoint" not in ctx.state["resources"]["kubernetes"]
        assert adapter.endpoint.process.wait(timeout=2) == -15
    finally:
        adapter.close_endpoint()


def test_resumed_backend_records_identity_after_launcher_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual-kubectl"
    install_tunnel_binary(tmp_path, monkeypatch)
    (tmp_path / "kubectl").rename(actual)
    wrapper = tmp_path / "kubectl"
    wrapper.write_text(
        "#!/bin/sh\nsleep 0.1\nexec " + shlex.quote(str(actual)) + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    ctx, adapter = prepared(tmp_path)
    adapter.open_endpoint()
    try:
        record = ctx.state["resources"]["kubernetes"]["endpoint"]
        current = (Path("/proc") / str(record["pid"]) / "cmdline").read_bytes().hex()
        assert record["cmdline"] == current
        resumed = backend(ctx)
        resumed.close_endpoint()
        assert "endpoint" not in ctx.state["resources"]["kubernetes"]
    finally:
        adapter.close_endpoint()


def test_tunnel_pid_reuse_is_not_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_tunnel_binary(tmp_path, monkeypatch)
    ctx, adapter = prepared(tmp_path)
    adapter.open_endpoint()
    try:
        ctx.state["resources"]["kubernetes"]["endpoint"]["start_time"] = "0"
        with pytest.raises(InstallError, match="identity"):
            backend(ctx).close_endpoint()
        assert adapter.endpoint.process.poll() is None
    finally:
        adapter.close_endpoint()


def test_lifecycle_revalidates_context_before_exec(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    adapter.stop()
    ctx.server = "https://changed.invalid"
    with pytest.raises(InstallError):
        adapter.lifecycle_argv("bootstrap")


def test_changed_ca_identity_is_refused(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    ctx.ca = "replacement-ca"
    with pytest.raises(InstallError):
        adapter.validate_ownership()


def test_restart_pins_the_resource_uid_before_changing_template(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    ctx.calls.clear()
    adapter.restart()
    patch_call = next((args, kw) for args, kw in ctx.calls if "patch" in args)
    args, _ = patch_call
    patch = json.loads(args[args.index("-p") + 1])
    assert patch[0] == {
        "op": "test",
        "path": "/metadata/uid",
        "value": "uid-statefulset-cairn",
    }
    assert patch[1] == {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "123",
    }
    assert patch[2]["path"] == "/spec/template/metadata/annotations"
    assert patch[2]["value"]["kubectl.kubernetes.io/restartedAt"]
    assert any("rollout" in args and "status" in args for args, _ in ctx.calls)


def test_restart_refuses_concurrent_annotation_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, adapter = prepared(tmp_path)
    original = ctx.command

    def concurrent(argv: Sequence[str], **kwargs: Any) -> str:
        if "patch" in argv and "statefulset/cairn" in argv:
            document = ctx.objects["statefulset/cairn"]
            document["metadata"]["resourceVersion"] = "124"
            document["spec"]["template"]["metadata"].setdefault("annotations", {})[
                "operator.example/change"
            ] = "preserve"
        return original(argv, **kwargs)

    monkeypatch.setattr(ctx, "command", concurrent)
    with pytest.raises(InstallError, match="JSON patch test failed"):
        adapter.restart()
    assert ctx.objects["statefulset/cairn"]["spec"]["template"]["metadata"][
        "annotations"
    ] == {"operator.example/change": "preserve"}


def test_missing_recorded_storage_refuses_start_but_blitz_can_finish(
    tmp_path: Path,
) -> None:
    ctx, adapter = prepared(tmp_path)
    del ctx.objects["persistentvolumeclaim/data-cairn-0"]
    ctx.calls.clear()
    with pytest.raises(InstallError, match="missing"):
        adapter.start()
    assert not mutations(ctx)
    adapter.blitz()
    assert not ctx.objects


def test_waiting_for_an_absent_pod_does_not_issue_a_failing_wait(
    tmp_path: Path,
) -> None:
    ctx, adapter = prepared(tmp_path)
    # A resumed scale-to-zero has no Cairn Pod left. kubectl wait may reject
    # an explicit name that no longer exists, so inspect absence first.
    ctx.calls.clear()
    adapter.stop()
    assert not any("wait" in args and "pod/cairn-0" in args for args, _ in ctx.calls)


def test_interrupted_secret_creation_is_recovered_without_regenerating_credentials(
    tmp_path: Path,
) -> None:
    ctx, adapter = prepared(tmp_path, semantic=True)
    old = copy.deepcopy(ctx.objects["secret/cairn-credentials"])
    receipt = ctx.state["resources"]["kubernetes"]["objects"][
        "secret/cairn-credentials"
    ]
    receipt.pop("uid")
    receipt.update(phase="creating", intent=True)
    ctx.calls.clear()
    backend(ctx).prepare()
    assert ctx.objects["secret/cairn-credentials"] == old
    assert not any("create" in args and "secret" in args for args, _ in ctx.calls)


def test_probe_never_becomes_a_service_endpoint(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    backend(ctx).preflight()
    pod = next(
        json.loads(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
        and json.loads(kw["stdin_data"])["kind"] == "Pod"
    )
    assert pod["spec"].get("readinessGates") == [
        {"conditionType": "cairn.example.invalid/probe-never-ready"}
    ]


@pytest.mark.parametrize("target", ["holder", "probe"])
def test_interrupted_delete_reconciles_durable_intent_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    if target == "holder":
        adapter.preflight()
        adapter.prepare()
        adapter.stop()
        key = "pod/cairn-bootstrap"
    else:
        key = "pod/" + adapter.probe_name
    original = ctx.command
    interrupted = False

    def interrupt_after_acceptance(argv: Sequence[str], **kwargs: Any) -> str:
        nonlocal interrupted
        if (
            "delete" in argv
            and "--raw" in argv
            and argv[list(argv).index("--raw") + 1].endswith("/" + key.split("/")[1])
            and "/pods/" in argv[list(argv).index("--raw") + 1]
            and not interrupted
        ):
            original(argv, **kwargs)
            interrupted = True
            raise KeyboardInterrupt
        return original(argv, **kwargs)

    monkeypatch.setattr(ctx, "command", interrupt_after_acceptance)
    with pytest.raises(KeyboardInterrupt):
        adapter.start() if target == "holder" else adapter.preflight()
    assert key not in ctx.objects
    # Reload the persisted journal, not merely the original in-memory dictionary.
    ctx.state = json.loads((ctx.directory / "state.json").read_text())
    resumed = backend(ctx)
    resumed.start() if target == "holder" else resumed.preflight()
    assert key not in ctx.objects


def test_completed_delete_never_adopts_same_label_replacement(tmp_path: Path) -> None:
    ctx, adapter = prepared(tmp_path)
    replacement = copy.deepcopy(ctx.objects["service/cairn"])
    adapter.blitz()
    replacement["metadata"]["uid"] = "replacement-service-uid"
    ctx.put(replacement)
    ctx.calls.clear()
    with pytest.raises(InstallError, match="ownership|UID"):
        backend(ctx).blitz()
    assert not mutations(ctx)
    assert ctx.objects["service/cairn"]["metadata"]["uid"] == "replacement-service-uid"


def test_deliberate_holder_recreation_uses_a_fresh_creation_transition(
    tmp_path: Path,
) -> None:
    ctx, adapter = prepared(tmp_path)
    adapter.stop()
    previous = ctx.objects["pod/cairn-bootstrap"]["metadata"]["uid"]
    adapter.start()
    adapter.stop()
    assert ctx.objects["pod/cairn-bootstrap"]["metadata"]["uid"] != previous
    adapter.validate_ownership()


def test_foreign_claim_appearing_after_inventory_is_never_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    adapter.preflight()
    original = ctx.command

    def claim_race(argv: Sequence[str], **kwargs: Any) -> str:
        result = original(argv, **kwargs)
        if (
            list(argv)[-3:] == ["create", "-f", "-"]
            and json.loads(kwargs["stdin_data"])["kind"] == "Secret"
        ):
            ctx.put(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "data-cairn-0",
                        "uid": "foreign-pvc-uid",
                        "labels": {},
                    },
                    "spec": {"accessModes": ["ReadWriteOncePod"]},
                }
            )
        return result

    monkeypatch.setattr(ctx, "command", claim_race)
    with pytest.raises(InstallError, match="ownership|UID"):
        adapter.prepare()
    assert "statefulset/cairn" not in ctx.objects
    assert not any("rollout" in args for args, _ in ctx.calls)
    assert (
        ctx.objects["persistentvolumeclaim/data-cairn-0"]["metadata"]["uid"]
        == "foreign-pvc-uid"
    )


def test_exact_owned_claims_have_receipts_before_any_workload_creation(
    tmp_path: Path,
) -> None:
    ctx, _ = prepared(tmp_path, semantic=True)
    claim_creates: set[str] = set()
    for (args, kw), state in zip(ctx.calls, ctx.snapshots, strict=True):
        if args[-3:] != ["create", "-f", "-"]:
            continue
        doc = json.loads(kw["stdin_data"])
        if doc["kind"] == "PersistentVolumeClaim" and doc["metadata"][
            "name"
        ].startswith("data-"):
            claim_creates.add(doc["metadata"]["name"])
            assert doc["spec"]["accessModes"] == ["ReadWriteOncePod"]
            assert doc["spec"]["storageClassName"] == "csi-test"
            assert doc["metadata"]["namespace"] == "alpha"
            assert doc["metadata"]["labels"].items() >= LABELS.items()
        if doc["kind"] == "StatefulSet":
            for name in ("data-cairn-0", "data-falkordb-0"):
                receipt = state["resources"]["kubernetes"]["objects"][
                    "persistentvolumeclaim/" + name
                ]
                assert receipt.get("uid") and receipt["phase"] == "live"
    assert claim_creates == {"data-cairn-0", "data-falkordb-0"}


@pytest.mark.parametrize(
    "permission",
    [
        ("create", "pods", "exec"),
        ("get", "pods", "exec"),
        ("create", "pods", "portforward"),
        ("get", "pods", "portforward"),
        ("get", "statefulsets", "scale"),
        ("update", "statefulsets", "scale"),
        ("list", "pods", ""),
        ("list", "statefulsets", ""),
    ],
)
def test_preflight_denies_required_subresource_or_list_before_mutation(
    tmp_path: Path, permission: tuple[str, str, str]
) -> None:
    ctx = Cluster(tmp_path)
    ctx.denied_permissions.add(permission)
    with pytest.raises(InstallError, match="permission"):
        backend(ctx).preflight()
    assert not any(args[-3:] == ["create", "-f", "-"] for args, _ in ctx.calls)


def test_preflight_does_not_require_unused_pod_log_access(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    ctx.denied_permissions.add(("get", "pods", "log"))
    backend(ctx).preflight()
    assert not any("--subresource=log" in args for args, _ in ctx.calls)


def test_recovered_endpoint_retains_journal_when_dead_leader_has_live_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import signal
    import socket
    import time

    install_tunnel_binary(tmp_path, monkeypatch)
    ctx, adapter = prepared(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ctx.state["port"] = sock.getsockname()[1]
    ready = tmp_path / "child-ready"
    leave = tmp_path / "leader-exit"
    child_pid = tmp_path / "child-pid"
    child = (
        "import pathlib,socket,time; "
        f's=socket.socket(); s.bind(("127.0.0.1",{ctx.port})); s.listen(); '
        f'pathlib.Path({str(ready)!r}).write_text("ready"); time.sleep(300)'
    )
    (tmp_path / "kubectl").write_text(
        "#!" + sys.executable + "\nimport pathlib,subprocess,sys,time\n"
        f'p=subprocess.Popen([sys.executable,"-c",{child!r}])\n'
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        f'print("Forwarding from 127.0.0.1:{ctx.port} -> 8000",flush=True)\n'
        f"while not pathlib.Path({str(leave)!r}).exists(): time.sleep(0.01)\n"
    )
    adapter.open_endpoint()
    try:
        before = copy.deepcopy(ctx.state["resources"]["kubernetes"]["endpoint"])
        leave.touch()
        assert adapter.endpoint.process.wait(timeout=3) == 0
        with pytest.raises(InstallError, match="group|identity"):
            backend(ctx).close_endpoint()
        assert ctx.state["resources"]["kubernetes"]["endpoint"] == before
        with socket.create_connection(("127.0.0.1", ctx.port), timeout=1):
            pass  # Refusal must not kill an unverified surviving process group.
    finally:
        os.kill(int(child_pid.read_text()), signal.SIGKILL)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(adapter.endpoint.process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        adapter.close_endpoint()


def test_recovered_endpoint_clears_a_fully_absent_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import signal

    install_tunnel_binary(tmp_path, monkeypatch)
    ctx, adapter = prepared(tmp_path)
    adapter.open_endpoint()
    try:
        os.killpg(adapter.endpoint.process.pid, signal.SIGTERM)
        adapter.endpoint.process.wait(timeout=3)
        backend(ctx).close_endpoint()
        assert "endpoint" not in ctx.state["resources"]["kubernetes"]
    finally:
        adapter.close_endpoint()


class DeletionClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.elapsed += seconds


@pytest.mark.parametrize(
    "key",
    [
        "serviceaccount/cairn",
        "configmap/cairn-config",
        "service/cairn",
        "secret/cairn-credentials",
        "pod/cairn-bootstrap",
        "persistentvolumeclaim/data-cairn-0",
        "statefulset/cairn",
        "networkpolicy/cairn-default-deny",
    ],
)
def test_deletion_polling_uses_only_exact_get_until_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    from cairn_install import kubernetes_resources

    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    clock = DeletionClock()
    monkeypatch.setattr(kubernetes_resources, "time", clock, raising=False)
    ctx.objects[key] = {
        "metadata": {
            "name": key.split("/")[1],
            "uid": "terminating",
            "labels": LABELS,
            "deletionTimestamp": "2026-09-17T00:00:00Z",
        }
    }
    original = ctx.command
    reads = 0

    def eventual_absence(argv: Sequence[str], **kwargs: Any) -> str:
        nonlocal reads
        if "wait" in argv:
            raise InstallError("list/watch permission was not granted")
        if "get" in argv and key in argv:
            reads += 1
            if reads == 3:
                ctx.objects.pop(key)
        return original(argv, **kwargs)

    monkeypatch.setattr(ctx, "command", eventual_absence)
    adapter.resources.wait_deleted(key)
    assert reads == 3
    assert 0 < clock.elapsed < 180
    assert all(
        args[5:] == ["get", key, "--ignore-not-found", "-o", "json"]
        for args, _ in ctx.calls
    )
    assert all(0 < kw["timeout"] <= 30 for _, kw in ctx.calls)


def test_deletion_polling_times_out_with_each_get_inside_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install import kubernetes_resources

    ctx = Cluster(tmp_path)
    adapter = backend(ctx)
    clock = DeletionClock()
    monkeypatch.setattr(kubernetes_resources, "time", clock, raising=False)
    key = "persistentvolumeclaim/data-cairn-0"
    ctx.objects[key] = {"metadata": {"name": "data-cairn-0", "uid": "terminating"}}
    original = ctx.command

    def slow_get(argv: Sequence[str], **kwargs: Any) -> str:
        assert "wait" not in argv
        assert 0 < kwargs["timeout"] <= min(30, 180 - clock.elapsed)
        clock.elapsed += kwargs["timeout"]
        return original(argv, **kwargs)

    monkeypatch.setattr(ctx, "command", slow_get)
    with pytest.raises(InstallError, match="Timed out"):
        adapter.resources.wait_deleted(key)
    assert clock.elapsed == 180
    assert len(ctx.calls) <= 6
    assert key in ctx.objects


def test_kubernetes_constructor_and_launcher_need_only_standard_library(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    state = {
        "name": "alpha",
        "mode": "kubernetes",
        "port": 18080,
        "semantic": False,
        "instance_id": LABELS["cairn.example.invalid/instance-id"],
        "run_id": LABELS["cairn.example.invalid/run-id"],
        "source": str(root),
        "resources": {},
        "owned_files": {},
        "kubernetes": {
            "context": "test-context",
            "namespace": "alpha",
            "image": IMAGE,
            "storage_class": "csi-test",
            "image_policy": "Always",
            "preloaded_image": False,
        },
    }
    code = (
        "import json,sys; from pathlib import Path; "
        "from cairn_install.core import Context; from cairn_install.workflow import backend; "
        f"ctx=Context(Path({str(tmp_path)!r}),json.loads({json.dumps(state)!r}),-1); "
        'adapter=backend(ctx); assert "statefulset/cairn" in adapter.expected; '
        'assert "yaml" not in sys.modules; '
        'import cairn_install.native, cairn_install.docker; print("stdlib-only")'
    )
    environment = os.environ | {"PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib-only"
    result = subprocess.run(
        [sys.executable, "-S", str(root / "cairn-install"), "--help"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "kubernetes" in result.stdout


def test_kubernetes_preflight_rejects_wrong_uv_before_probe(tmp_path: Path) -> None:
    ctx = Cluster(tmp_path)
    ctx.uv_version = "uv 0.11.0"
    with pytest.raises(InstallError, match="uv 0.12.14"):
        backend(ctx).preflight()
    assert not any(args[-3:] == ["create", "-f", "-"] for args, _ in ctx.calls)


def test_kubernetes_prepare_uses_locked_helper_and_checks_retained_files(
    tmp_path: Path,
) -> None:
    ctx, adapter = prepared(tmp_path)
    helper_calls = [(args, kw) for args, kw in ctx.calls if args[:2] == ["uv", "run"]]
    assert helper_calls
    args, kw = helper_calls[0]
    assert args[:6] == ["uv", "run", "--locked", "--no-dev", "python", "-m"]
    assert args[6] == "cairn_install.kubernetes_assets"
    assert kw["cwd"] == ctx.source
    assert kw["env"]["UV_PROJECT_ENVIRONMENT"] == str(ctx.root / "kubernetes-runtime")
    site = adapter.site_path.read_text()
    holder = adapter.holder_path.read_text()
    ctx.calls.clear()
    backend(ctx).prepare()
    request = json.loads(
        next(kw["stdin_data"] for args, kw in ctx.calls if args[:2] == ["uv", "run"])
    )
    assert request["site"] == site and request["holder"] == holder
    adapter.holder_path.write_text(holder + "\n# changed\n")
    ctx.calls.clear()
    with pytest.raises(InstallError, match="changed"):
        backend(ctx).stop()
    assert not any(
        "scale" in args or args[:2] == ["uv", "run"] for args, _ in ctx.calls
    )


@pytest.fixture
def garden_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cluster:
    """Only the independently tested enrolment boundary is replaced here."""
    import types

    ctx = Cluster(tmp_path)
    ctx.state["garden"] = {
        "options": {
            "endpoint": "https://garden.example.invalid:9443/mcp",
            "port": 9443,
            "image": "registry.example/garden@sha256:" + "b" * 64,
            "kubernetes_service_type": "ClusterIP",
            "allowed_cidrs": ["192.0.2.0/24"],
        }
    }
    ctx.write_file(ctx.root / "garden/tls/server.crt", "synthetic certificate")
    ctx.write_file(
        ctx.root / "garden/tls/server.key", "synthetic private key", secret=True
    )
    module = types.ModuleType("cairn_install.garden")

    def gateway_config(context: Context, **kwargs: Any) -> dict[str, Any]:
        kwargs["auth"] = {
            "instance_id": context.instance_id,
            "endpoint": kwargs.pop("cairn_url"),
            "scope": {"realm": "test", "segments": []},
            "classification": "internal",
        }
        kwargs["tls_cert_file"] = kwargs.pop("cert_file")
        kwargs["tls_key_file"] = kwargs.pop("key_file")
        kwargs["principals"] = {"33333333-3333-4333-8333-333333333333": "val"}
        return kwargs

    def require_listener_available(port: int, *, wildcard: bool) -> None:
        del port, wildcard

    module.gateway_config = gateway_config  # type: ignore[attr-defined]
    module.require_listener_available = require_listener_available  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cairn_install.garden", module)
    return ctx


def test_garden_initial_prepare_does_not_start_before_enrolment(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.preflight()
    adapter.prepare()
    assert "persistentvolumeclaim/garden-data" in ctx.objects
    assert "secret/garden-tls" not in ctx.objects
    assert [
        c["name"]
        for c in ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"][
            "containers"
        ]
    ] == ["cairn"]
    probes = [
        yaml.safe_load(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
    ]
    pod = next(p for p in probes if p["kind"] == "Pod")
    assert (
        pod["spec"]["containers"][1]["image"] == ctx.state["garden"]["options"]["image"]
    )
    assert pod["spec"]["containers"][1]["command"] == [
        "/usr/local/bin/a2a",
        "host",
        "--help",
    ]


def test_garden_attachment_binds_tls_loopback_auth_and_owned_storage(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    adapter.garden_start()
    pod = ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["cairn", "garden"]
    assert pod["securityContext"]["fsGroup"] == 65532
    garden = pod["containers"][1]
    assert garden["ports"][0]["containerPort"] == 9443
    assert garden["command"] == [
        "/usr/local/bin/a2a",
        "host",
        "--config",
        "/etc/garden/host.json",
    ]
    assert {m["name"] for m in garden["volumeMounts"]} == {
        "garden-data",
        "garden-config",
        "garden-tls",
    }
    cfg = json.loads(ctx.objects["configmap/garden-config"]["data"]["host.json"])
    assert (
        cfg["gateway"]["auth"]["endpoint"] == "http://127.0.0.1:8000/memory/v1/diagnose"
    )
    assert cfg["gateway"]["listen"] == "0.0.0.0:9443"
    assert cfg["gateway"]["daemon_url_file"] == "/var/lib/garden/run/daemon.url"
    assert ctx.objects["persistentvolumeclaim/garden-data"]["spec"]["accessModes"] == [
        "ReadWriteOncePod"
    ]
    assert ctx.objects["service/garden"]["spec"]["type"] == "ClusterIP"
    ingress = ctx.objects["networkpolicy/garden-ingress"]["spec"]["ingress"]
    assert ingress == [
        {
            "from": [{"ipBlock": {"cidr": "192.0.2.0/24"}}],
            "ports": [{"protocol": "TCP", "port": 9443}],
        }
    ]
    before = copy.deepcopy(ctx.objects)
    backend(ctx).garden_prepare()
    assert ctx.objects == before
    assert "synthetic private key" not in json.dumps(ctx.state)


def test_garden_resume_after_patch_acceptance_before_response(
    garden_cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    original = ctx.command
    failed = False

    def ambiguous(argv: Sequence[str], **kwargs: Any) -> str:
        nonlocal failed
        result = original(argv, **kwargs)
        if "patch" in argv and not failed:
            failed = True
            raise InstallError("response lost")
        return result

    monkeypatch.setattr(ctx, "command", ambiguous)
    with pytest.raises(InstallError, match="response lost"):
        adapter.garden_prepare()
    assert adapter.garden.record["transition"]["phase"] == "pending"
    backend(ctx).garden_prepare()
    assert adapter.garden.record["transition"]["phase"] == "attached"
    assert sum("patch" in args for args, _ in ctx.calls) == 1


def test_garden_template_drift_is_not_overwritten(garden_cluster: Cluster) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    template = ctx.objects["statefulset/cairn"]["spec"]["template"]
    template["spec"]["containers"][0]["image"] = "foreign-image"
    with pytest.raises(InstallError, match="template changed"):
        adapter.garden_prepare()
    assert template["spec"]["containers"][0]["image"] == "foreign-image"
    assert not any("patch" in args for args, _ in ctx.calls)


def test_garden_holder_is_isolated_and_rollback_retains_data(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    adapter.stop()
    holder = ctx.objects["pod/cairn-bootstrap"]
    assert [c["name"] for c in holder["spec"]["containers"]] == ["cairn-bootstrap"]
    assert not any(v["name"].startswith("garden") for v in holder["spec"]["volumes"])
    assert "cairn.example.invalid/garden" not in holder["metadata"]["labels"]
    adapter.start()
    adapter.restart()
    adapter.garden_start()
    adapter.rollback()
    assert "persistentvolumeclaim/garden-data" in ctx.objects
    assert "secret/garden-tls" in ctx.objects
    assert "pod/cairn-bootstrap" not in ctx.objects


def test_garden_replaced_pvc_blocks_blitz_before_mutation(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    ctx.objects["persistentvolumeclaim/garden-data"]["metadata"]["uid"] = "replacement"
    before = len(ctx.calls)
    with pytest.raises(InstallError, match="UID or ownership conflict"):
        adapter.blitz()
    assert not any("delete" in args for args, _ in ctx.calls[before:])


def test_garden_configuration_drift_is_refused(garden_cluster: Cluster) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    ctx.objects["configmap/garden-config"]["data"]["host.json"] = "{}"
    with pytest.raises(InstallError, match="configuration drift"):
        adapter.garden_prepare()


def test_garden_blitz_deletes_exact_resources_and_is_repeatable(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    adapter.blitz()
    assert not ctx.objects
    adapter.blitz()


def test_garden_options_cannot_change_on_resume(garden_cluster: Cluster) -> None:
    ctx = garden_cluster
    backend(ctx)
    ctx.state["garden"]["options"]["port"] = 8443
    with pytest.raises(InstallError, match="Garden options changed"):
        backend(ctx)


def test_garden_endpoint_uses_configured_port_and_separate_record(
    garden_cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = garden_cluster
    install_tunnel_binary(tmp_path, monkeypatch)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        ctx.state["garden"]["options"]["port"] = listener.getsockname()[1]
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    adapter.garden_open_endpoint()
    try:
        record = ctx.state["resources"]["kubernetes"]
        assert "endpoint" not in record
        receipt = record["garden"]["endpoint"]
        port = ctx.state["garden"]["options"]["port"]
        assert f"{port}:{port}" in receipt["argv"]
        assert receipt["argv"][-2:] == ["--address", "127.0.0.1"]
    finally:
        adapter.garden_close_endpoint()
    assert "endpoint" not in record["garden"]


def test_garden_stop_does_not_scale_shared_pod_before_restart(
    garden_cluster: Cluster,
) -> None:
    adapter = backend(garden_cluster)
    adapter.prepare()
    adapter.garden_prepare()
    garden_cluster.calls.clear()
    adapter.garden_stop()
    assert not any("scale" in args for args, _ in garden_cluster.calls)


def test_garden_rejects_collision_with_cairn_container_port(
    garden_cluster: Cluster,
) -> None:
    garden_cluster.state["garden"]["options"]["port"] = 8000
    with pytest.raises(InstallError, match="Garden image, port"):
        backend(garden_cluster)


def test_garden_nested_options_do_not_alias_durable_snapshot(
    garden_cluster: Cluster,
) -> None:
    backend(garden_cluster)
    garden_cluster.state["garden"]["options"]["allowed_cidrs"].append("198.51.100.0/24")
    with pytest.raises(InstallError, match="Garden options changed"):
        backend(garden_cluster)


def _api_default_probes(template: dict[str, Any]) -> None:
    """Defaults observed on the dedicated Kubernetes acceptance StatefulSet."""
    for container in template["spec"]["containers"]:
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container.get(name)
            if probe is None:
                continue
            for key, value in {
                "timeoutSeconds": 1,
                "periodSeconds": 10,
                "successThreshold": 1,
                "failureThreshold": 3,
            }.items():
                probe.setdefault(key, value)
            if "httpGet" in probe:
                probe["httpGet"].setdefault("scheme", "HTTP")


def test_garden_attachment_accepts_api_probe_defaults_before_and_after_patch(
    garden_cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    _api_default_probes(ctx.objects["statefulset/cairn"]["spec"]["template"])
    original = ctx.command

    def api_defaults(argv: Sequence[str], **kwargs: Any) -> str:
        result = original(argv, **kwargs)
        if "patch" in argv:
            _api_default_probes(ctx.objects["statefulset/cairn"]["spec"]["template"])
        return result

    monkeypatch.setattr(ctx, "command", api_defaults)
    adapter.garden_prepare()
    assert adapter.garden.record["transition"]["phase"] == "attached"
    backend(ctx).garden_prepare()
    assert sum("patch" in args for args, _ in ctx.calls) == 1
    probes = ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"]["containers"]
    assert probes[1]["readinessProbe"]["timeoutSeconds"] == 1


@pytest.mark.parametrize(
    "changed", ["timeoutSeconds", "failureThreshold", "scheme", "path"]
)
def test_garden_attachment_rejects_nondefault_probe_drift(
    garden_cluster: Cluster, changed: str
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    template = ctx.objects["statefulset/cairn"]["spec"]["template"]
    _api_default_probes(template)
    probe = template["spec"]["containers"][0]["readinessProbe"]
    if changed in {"scheme", "path"}:
        probe["httpGet"][changed] = "HTTPS" if changed == "scheme" else "/wrong"
    else:
        probe[changed] = 9
    with pytest.raises(InstallError, match="template changed"):
        adapter.garden_prepare()
    assert not any("patch" in args for args, _ in ctx.calls)


def test_garden_mount_policy_preserves_private_runtime_modes_on_restart(
    garden_cluster: Cluster,
) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.preflight()
    adapter.prepare()
    pod_spec = ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert (
        adapter.holder_document["spec"]["securityContext"]["fsGroupChangePolicy"]
        == "OnRootMismatch"
    )
    created = [
        yaml.safe_load(kw["stdin_data"])
        for args, kw in ctx.calls
        if args[-3:] == ["create", "-f", "-"]
    ]
    probe = next(doc for doc in created if doc["kind"] == "Pod")
    assert probe["spec"]["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    adapter.garden_prepare()
    adapter.restart()
    adapter.garden_start()
    pod_spec = ctx.objects["statefulset/cairn"]["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert pod_spec["securityContext"]["runAsUser"] == 65532
    assert pod_spec["securityContext"]["fsGroup"] == 65532


def test_garden_permission_policy_drift_is_not_ignored(garden_cluster: Cluster) -> None:
    ctx = garden_cluster
    adapter = backend(ctx)
    adapter.prepare()
    adapter.garden_prepare()
    template = ctx.objects["statefulset/cairn"]["spec"]["template"]
    template["spec"]["securityContext"]["fsGroupChangePolicy"] = "Always"
    ctx.calls.clear()
    with pytest.raises(InstallError, match="template drift"):
        adapter.garden_prepare()
    assert not any("patch" in args for args, _ in ctx.calls)
