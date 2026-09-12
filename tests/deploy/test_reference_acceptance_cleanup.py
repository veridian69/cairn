"""Fail-closed tests for the prepared-stage namespace cleanup guard."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
CLEANUP = REPOSITORY / "scripts" / "reference_acceptance_cleanup.py"
NAMESPACE = "cairn-target-acceptance"
GATEWAY_NAMESPACE = "cairn-egress"
ENDPOINTS_DEPRECATION_WARNING = (
    "Warning: v1 Endpoints is deprecated in v1.33+; use "
    "discovery.k8s.io/v1 EndpointSlice"
)
RUN_ID = "task11-prior-01"
REVISION = "a" * 40
OWNER_LABELS = {
    "cairn.example.invalid/acceptance-owner": "task11",
    "cairn.example.invalid/acceptance-run": RUN_ID,
    "cairn.example.invalid/acceptance-revision": REVISION,
}
RESOURCES = (
    "configmaps",
    "cronjobs.batch",
    "daemonsets.apps",
    "endpoints",
    "jobs.batch",
    "replicationcontrollers",
    "serviceaccounts",
    "widgets.example.test",
)


def test_cleanup_guard_is_executable() -> None:
    assert CLEANUP.stat().st_mode & 0o100


def _key(*arguments: str) -> str:
    return json.dumps(list(arguments), separators=(",", ":"))


def _object(
    api_version: str, kind: str, name: str, *, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": OWNER_LABELS if labels is None else labels,
        },
    }


def _list(*items: dict[str, Any]) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)})


def _prepared_fixture(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    report = tmp_path / "failed-report.json"
    report.write_text(
        json.dumps(
            {
                "status": "failed",
                "stage": "prepared",
                "run_id": RUN_ID,
                "repository_revision": REVISION,
            }
        ),
        encoding="utf-8",
    )
    report_digest = hashlib.sha256(report.read_bytes()).hexdigest()
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "uid": "namespace-uid-17",
            "labels": OWNER_LABELS,
        },
    }
    pvs = [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": f"cairn-local-pv{number}"},
            "spec": {
                "storageClassName": "cairn-local",
                "persistentVolumeReclaimPolicy": "Retain",
                "local": {"path": f"/mnt/cairn-local/pv{number}"},
            },
            "status": {"phase": "Available"},
        }
        for number in range(1, 5)
    ]
    responses: dict[str, Any] = {
        _key("get", "namespace", NAMESPACE, "-o", "json"): {
            "stdout": json.dumps(namespace)
        },
        _key(
            "get",
            "configmap",
            "acceptance-state",
            "-n",
            NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ): {"stdout": ""},
        _key(
            "get",
            "namespace",
            GATEWAY_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ): {"stdout": ""},
        _key("get", "pv", "-o", "json"): {"stdout": _list(*pvs)},
        _key("api-resources", "--verbs=list", "--namespaced=true", "-o", "name"): {
            "stdout": "\n".join(RESOURCES) + "\n"
        },
        _key("get", "configmaps", "-n", NAMESPACE, "-o", "json"): {
            "stdout": _list(
                _object("v1", "ConfigMap", "acceptance-scripts"),
                _object("v1", "ConfigMap", "kube-root-ca.crt"),
            )
        },
        _key("get", "serviceaccounts", "-n", NAMESPACE, "-o", "json"): {
            "stdout": _list(_object("v1", "ServiceAccount", "default"))
        },
        _key("delete", "namespace", NAMESPACE, "--wait=false"): {
            "stdout": f'namespace "{NAMESPACE}" deleted\n'
        },
        _key(
            "wait",
            "--for=delete",
            f"namespace/{NAMESPACE}",
            "--timeout=180s",
        ): {"stdout": ""},
    }
    for resource in RESOURCES:
        responses.setdefault(
            _key("get", resource, "-n", NAMESPACE, "-o", "json"),
            {"stdout": _list()},
        )
    return report, report_digest, responses


def _install_fake_kubectl(tmp_path: Path, responses: dict[str, Any]) -> Path:
    fixture = tmp_path / "kubectl-fixture.json"
    fixture.write_text(json.dumps(responses), encoding="utf-8")
    log = tmp_path / "kubectl-log.jsonl"
    executable = tmp_path / "kubectl"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as stream:
    responses = json.load(stream)
key = json.dumps(arguments, separators=(",", ":"))
response = responses.get(key)
if response is None:
    print("unexpected fake kubectl command: " + key, file=sys.stderr)
    raise SystemExit(97)
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
raise SystemExit(response.get("returncode", 0))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return log


def _run_cleanup(
    tmp_path: Path,
    report: Path,
    report_digest: str,
    responses: dict[str, Any],
    *,
    run_id: str = RUN_ID,
    revision: str = REVISION,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    log = _install_fake_kubectl(tmp_path, responses)
    result = subprocess.run(
        [
            sys.executable,
            str(CLEANUP),
            "--report",
            str(report),
            "--report-sha256",
            report_digest,
            "--run-id",
            run_id,
            "--revision",
            revision,
            "--delete",
        ],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "FAKE_KUBECTL_FIXTURE": str(tmp_path / "kubectl-fixture.json"),
            "FAKE_KUBECTL_LOG": str(log),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    commands = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, commands


@pytest.mark.parametrize(
    ("resource", "api_version", "kind"),
    [
        ("jobs.batch", "batch/v1", "Job"),
        ("cronjobs.batch", "batch/v1", "CronJob"),
        ("daemonsets.apps", "apps/v1", "DaemonSet"),
        ("replicationcontrollers", "v1", "ReplicationController"),
        ("widgets.example.test", "example.test/v1", "Widget"),
    ],
)
def test_cleanup_refuses_every_discovered_unexpected_resource(
    tmp_path: Path, resource: str, api_version: str, kind: str
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    responses[_key("get", resource, "-n", NAMESPACE, "-o", "json")] = {
        "stdout": _list(_object(api_version, kind, "unexpected"))
    }

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert "unexpected namespaced object" in result.stderr
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


@pytest.mark.parametrize("failure", ["discovery", "list"])
def test_cleanup_refuses_discovery_or_list_failure(
    tmp_path: Path, failure: str
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    key = (
        _key("api-resources", "--verbs=list", "--namespaced=true", "-o", "name")
        if failure == "discovery"
        else _key("get", "jobs.batch", "-n", NAMESPACE, "-o", "json")
    )
    responses[key] = {"returncode": 1, "stderr": "synthetic API failure\n"}

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert "kubectl" in result.stderr
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


def test_cleanup_allows_only_exact_endpoints_deprecation_warning(
    tmp_path: Path,
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    responses[_key("get", "endpoints", "-n", NAMESPACE, "-o", "json")] = {
        "stdout": _list(),
        "stderr": ENDPOINTS_DEPRECATION_WARNING + "\n",
    }

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 0, result.stderr
    assert ["delete", "namespace", NAMESPACE, "--wait=false"] in commands


def test_cleanup_warning_does_not_hide_an_unexpected_endpoint(
    tmp_path: Path,
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    responses[_key("get", "endpoints", "-n", NAMESPACE, "-o", "json")] = {
        "stdout": _list(_object("v1", "Endpoints", "unexpected")),
        "stderr": ENDPOINTS_DEPRECATION_WARNING + "\n",
    }

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert "unexpected namespaced object" in result.stderr
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


@pytest.mark.parametrize(
    ("resource", "stderr"),
    [
        (
            "endpoints",
            ENDPOINTS_DEPRECATION_WARNING + "\nsynthetic extra diagnostic\n",
        ),
        ("configmaps", ENDPOINTS_DEPRECATION_WARNING + "\n"),
        (
            "endpoints",
            "Warning: v1 Endpoints is deprecated; use discovery.k8s.io/v1 "
            "EndpointSlice\n",
        ),
    ],
)
def test_cleanup_refuses_non_exact_or_misplaced_warning(
    tmp_path: Path, resource: str, stderr: str
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    responses[_key("get", resource, "-n", NAMESPACE, "-o", "json")]["stderr"] = stderr

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert "kubectl" in result.stderr
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


@pytest.mark.parametrize(
    "response",
    [
        {
            "stdout": _list(),
            "stderr": ENDPOINTS_DEPRECATION_WARNING + "\n",
            "returncode": 1,
        },
        {
            "stdout": "not JSON",
            "stderr": ENDPOINTS_DEPRECATION_WARNING + "\n",
        },
    ],
)
def test_cleanup_refuses_failed_or_malformed_endpoints_list(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    responses[_key("get", "endpoints", "-n", NAMESPACE, "-o", "json")] = response

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


@pytest.mark.parametrize(
    "mismatch",
    [
        "report-run",
        "report-stage",
        "report-sha",
        "namespace-label",
        "object-label",
    ],
)
def test_cleanup_refuses_mismatched_report_or_namespace_identity(
    tmp_path: Path, mismatch: str
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    if mismatch in {"report-run", "report-stage"}:
        payload = json.loads(report.read_text(encoding="utf-8"))
        if mismatch == "report-run":
            payload["run_id"] = "different-run"
        else:
            payload["stage"] = "bring-up"
        report.write_text(json.dumps(payload), encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
    elif mismatch == "report-sha":
        digest = "b" * 64
    elif mismatch == "namespace-label":
        namespace = json.loads(
            responses[_key("get", "namespace", NAMESPACE, "-o", "json")]["stdout"]
        )
        namespace["metadata"]["labels"]["cairn.example.invalid/acceptance-run"] = (
            "different-run"
        )
        responses[_key("get", "namespace", NAMESPACE, "-o", "json")]["stdout"] = (
            json.dumps(namespace)
        )
    else:
        key = _key("get", "serviceaccounts", "-n", NAMESPACE, "-o", "json")
        serviceaccounts = json.loads(responses[key]["stdout"])
        serviceaccounts["items"][0]["metadata"]["labels"] = {}
        responses[key]["stdout"] = json.dumps(serviceaccounts)

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


def test_cleanup_refuses_a_missing_preparatory_object(tmp_path: Path) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    key = _key("get", "configmaps", "-n", NAMESPACE, "-o", "json")
    configmaps = json.loads(responses[key]["stdout"])
    configmaps["items"] = [
        item
        for item in configmaps["items"]
        if item["metadata"]["name"] != "acceptance-scripts"
    ]
    responses[key]["stdout"] = json.dumps(configmaps)

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert "exact expected object set" in result.stderr
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


@pytest.mark.parametrize("unsafe", ["phase", "gateway", "pv-claim"])
def test_cleanup_refuses_phase_gateway_or_claimed_pv(
    tmp_path: Path, unsafe: str
) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)
    if unsafe == "phase":
        responses[
            _key(
                "get",
                "configmap",
                "acceptance-state",
                "-n",
                NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            )
        ] = {"stdout": json.dumps(_object("v1", "ConfigMap", "acceptance-state"))}
    elif unsafe == "gateway":
        responses[
            _key(
                "get",
                "namespace",
                GATEWAY_NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            )
        ] = {
            "stdout": json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": GATEWAY_NAMESPACE, "labels": OWNER_LABELS},
                }
            )
        }
    else:
        pv_key = _key("get", "pv", "-o", "json")
        pvs = json.loads(responses[pv_key]["stdout"])
        pvs["items"][0]["status"]["phase"] = "Bound"
        pvs["items"][0]["spec"]["claimRef"] = {
            "namespace": NAMESPACE,
            "name": "data-cairn-0",
        }
        responses[pv_key]["stdout"] = json.dumps(pvs)

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 2
    assert not any(command[:2] == ["delete", "namespace"] for command in commands)


def test_cleanup_exact_success_command_sequence(tmp_path: Path) -> None:
    report, digest, responses = _prepared_fixture(tmp_path)

    result, commands = _run_cleanup(tmp_path, report, digest, responses)

    assert result.returncode == 0, result.stderr
    expected = [
        ["get", "namespace", NAMESPACE, "-o", "json"],
        [
            "get",
            "configmap",
            "acceptance-state",
            "-n",
            NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ],
        [
            "get",
            "namespace",
            GATEWAY_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ],
        ["get", "pv", "-o", "json"],
        ["api-resources", "--verbs=list", "--namespaced=true", "-o", "name"],
        *[
            ["get", resource, "-n", NAMESPACE, "-o", "json"]
            for resource in sorted(RESOURCES)
        ],
        ["delete", "namespace", NAMESPACE, "--wait=false"],
        [
            "wait",
            "--for=delete",
            f"namespace/{NAMESPACE}",
            "--timeout=180s",
        ],
    ]
    assert commands == expected
    assert report.exists()
    assert hashlib.sha256(report.read_bytes()).hexdigest() == digest
