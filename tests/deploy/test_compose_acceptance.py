"""Host-independent guards for the P-69 Compose acceptance report."""

import copy
import importlib.machinery
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY / "scripts" / "compose-acceptance"


def _load_harness() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("compose_acceptance", str(HARNESS))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _complete_report(harness: ModuleType) -> dict[str, Any]:
    checks = [
        {
            "name": name,
            "expected": {"result": "expected"},
            "observed": {"result": "observed"},
            "outcome": "passed",
        }
        for name in harness.REQUIRED_CHECKS
    ]
    probe_window = "network-window-1"
    positive = next(
        check for check in checks if check["name"] == "cross-project-positive-control"
    )
    positive["observed"] = {
        "probe_window": probe_window,
        "source_network": "cairn-b_default",
        "target_address": "172.31.0.3",
        "target_port": 8000,
        "http_status": 200,
    }
    refusal = next(
        check for check in checks if check["name"] == "cross-project-refusal"
    )
    refusal["observed"] = {
        "probe_window": probe_window,
        "source_network": "cairn-a_default",
        "target_address": "172.31.0.3",
        "target_port": 8000,
        "exit_code": 1,
    }
    return {
        "status": "observed-pass",
        "stage": "compose-production",
        "source_revision": "b19fe3268c95c0eab18dfabb930866d86ffd5314",
        "identities": {
            "host": "reference",
            "docker_engine": "29.7.2",
            "docker_compose": "v5.4.0",
            "cairn_image": "cairn:v0.7.0-rc.6",
            "cairn_image_id": "sha256:base",
            "cairn_upgrade_image": "cairn:v0.7.0-rc.6-acceptance-upgrade",
            "cairn_upgrade_image_id": "sha256:upgrade",
            "falkordb_image": "falkordb/falkordb:v4.20.2@sha256:digest",
        },
        "checks": checks,
        "boundaries": list(harness.REQUIRED_BOUNDARIES),
    }


# P-69's suite enumeration, written out rather than derived, because
# every other guard in this file builds its fixture from the harness's own
# REQUIRED_CHECKS and so proves the validator's mechanism without ever
# proving the set is right. Dropping a check from the run and from that
# tuple would otherwise keep this suite green and still emit
# `observed-pass`. Each group names the P-69 clause it discharges; adding a
# check to the harness means adding it here, on purpose.
P69_REQUIRED_CHECKS = (
    # "packaged startup" — the project validates and first boot completes.
    "compose-config:cairn-a",
    "compose-config:cairn-b",
    "compose-config:cairn-retrieval",
    "first-boot:cairn-a",
    "first-boot:cairn-b",
    "first-boot:cairn-retrieval",
    # "all three health endpoints".
    "health-live:cairn-a",
    "health-startup:cairn-a",
    "health-ready:cairn-a",
    "health-live:cairn-b",
    "health-startup:cairn-b",
    "health-ready:cairn-b",
    # "authenticated REST and MCP round trips".
    "authenticated-rest-instance:cairn-a",
    "authenticated-rest-instance:cairn-b",
    "authenticated-mcp-initialize:cairn-a",
    "authenticated-mcp-tools-list:cairn-a",
    "authenticated-mcp-instance:cairn-a",
    "authenticated-mcp-initialize:cairn-b",
    "authenticated-mcp-tools-list:cairn-b",
    "authenticated-mcp-instance:cairn-b",
    "authenticated-rest-ingest:cairn-a",
    "authenticated-rest-ingest:cairn-b",
    # "persistence across restart".
    "persistence-across-restart:cairn-a",
    "persistence-across-restart:cairn-b",
    # "duplicate-process lock exclusion proven by a second container".
    "duplicate-process-lock-exclusion:cairn-a",
    "duplicate-process-lock-exclusion:cairn-b",
    # "termination within the 60-second grace".
    "bounded-termination:cairn-a",
    "bounded-termination:cairn-b",
    # "a backup/restore round trip via docker exec/docker cp".
    "backup-live:cairn-a",
    "backup-bundle-copied:cairn-a",
    "backup-member-digests:cairn-a",
    "restore-bundle-copied:cairn-a",
    "restore-round-trip:cairn-a",
    # "an image-replacement upgrade rehearsal".
    "image-replacement-upgrade:cairn-a",
    # "two-project separation ... and cross-project connection refusal
    # proven by an attempted connection", with the positive control the
    # 16 August 2026 review required of every negative.
    "two-project-networks-distinct",
    "two-project-volumes-distinct",
    "cross-project-positive-control",
    "cross-project-refusal",
    # The retrieval-enabled composition on the production target.
    "retrieval-falkordb-init",
    "retrieval-falkordb-health",
    "retrieval-cairn-ready",
    "retrieval-authenticated-rest",
    "retrieval-authenticated-mcp",
)


def test_the_required_check_set_is_the_p69_suite() -> None:
    harness = _load_harness()

    assert harness.REQUIRED_CHECKS == P69_REQUIRED_CHECKS


def test_the_host_operating_system_is_read_from_the_host(tmp_path: Path) -> None:
    """I-95 pins the target host's OS and I-14's claim rule expects the
    record to name it. An unreadable file is recorded as absent rather
    than guessed."""
    harness = _load_harness()
    release = tmp_path / "os-release"
    release.write_text(
        'NAME="Fedora Linux"\nPRETTY_NAME="Fedora Linux 44 (Server Edition)"\n',
        encoding="utf-8",
    )

    assert harness._host_os(release) == "Fedora Linux 44 (Server Edition)"
    assert harness._host_os(tmp_path / "absent") == ""


def test_a_complete_literal_report_is_accepted() -> None:
    harness = _load_harness()
    harness.validate_report(_complete_report(harness))


def test_a_missing_required_check_is_refused() -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    report["checks"] = report["checks"][:-1]

    with pytest.raises(harness.EvidenceError, match="missing required checks"):
        harness.validate_report(report)


def test_an_identical_upgrade_image_is_refused() -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    report["identities"]["cairn_upgrade_image_id"] = report["identities"][
        "cairn_image_id"
    ]

    with pytest.raises(harness.EvidenceError, match="upgrade image ID"):
        harness.validate_report(report)


@pytest.mark.parametrize("field", ["probe_window", "target_address", "target_port"])
def test_the_refusal_must_match_its_positive_control(field: str) -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    refusal = next(
        check for check in report["checks"] if check["name"] == "cross-project-refusal"
    )
    refusal["observed"][field] = "wrong"

    with pytest.raises(harness.EvidenceError, match="positive control"):
        harness.validate_report(report)


def test_retrieval_evidence_cannot_be_dropped() -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    report["checks"] = [
        check
        for check in report["checks"]
        if check["name"] != "retrieval-falkordb-health"
    ]

    with pytest.raises(harness.EvidenceError, match="missing required checks"):
        harness.validate_report(report)


def test_a_failed_observation_cannot_claim_observed_pass() -> None:
    harness = _load_harness()
    report = copy.deepcopy(_complete_report(harness))
    report["checks"][0]["outcome"] = "failed"

    with pytest.raises(harness.EvidenceError, match="non-passing checks"):
        harness.validate_report(report)


def test_the_evidence_boundaries_are_required() -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    report["boundaries"] = report["boundaries"][:-1]

    with pytest.raises(harness.EvidenceError, match="missing required boundaries"):
        harness.validate_report(report)


def test_instance_configuration_selects_retrieval_explicitly() -> None:
    harness = _load_harness()

    base = harness.instance_config("11111111-1111-4111-8111-111111111111", False)
    retrieval = harness.instance_config("77777777-7777-4777-8777-777777777777", True)

    assert "instance_id: 11111111-1111-4111-8111-111111111111" in base
    assert "graphiti:\n  enabled: false\n" in base
    assert "instance_id: 77777777-7777-4777-8777-777777777777" in retrieval
    assert "graphiti:\n  enabled: true\n  host: falkordb\n  port: 6379\n" in retrieval


def test_instance_environment_contains_paths_but_no_credentials() -> None:
    harness = _load_harness()

    document = harness.instance_environment(
        "cairn-a",
        18080,
        Path("/acceptance/a/config.yaml"),
        Path("/acceptance/a/credentials"),
        None,
    )

    assert document == (
        "COMPOSE_PROJECT_NAME=cairn-a\n"
        "CAIRN_HOST_PORT=18080\n"
        "CAIRN_CONFIG_FILE=/acceptance/a/config.yaml\n"
        "CAIRN_CREDENTIALS_DIR=/acceptance/a/credentials\n"
    )
    assert "PASSWORD" not in document.upper()
    assert "TOKEN" not in document.upper()


def test_report_is_validated_before_it_is_written(tmp_path: Path) -> None:
    harness = _load_harness()
    report = _complete_report(harness)
    path = tmp_path / "report.json"

    harness.write_report(path, report)

    assert json.loads(path.read_text(encoding="utf-8")) == report
    report["checks"][0]["outcome"] = "failed"
    with pytest.raises(harness.EvidenceError, match="non-passing checks"):
        harness.write_report(path, report)
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "observed-pass"


def test_the_bootstrap_principal_label_satisfies_the_catalogue_constraint() -> None:
    harness = _load_harness()

    assert re.fullmatch(r"[a-z][a-z0-9-]{0,62}", harness.BOOTSTRAP_LABEL)


def test_restore_enters_through_a_mounted_transfer_volume() -> None:
    harness = _load_harness()

    plan = harness.restore_transfer_plan("cairn-t10-a")

    assert plan == {
        "volume": "cairn-t10-a_restore-input",
        "container": "cairn-t10-a-restore-transfer",
        "copy_target": "/restore-input/restore-bundle",
        "helper_bundle": "/restore-input/restore-bundle",
        "helper_mount_read_only": True,
    }


def test_upgrade_migration_refuses_a_running_service_state() -> None:
    harness = _load_harness()

    with pytest.raises(harness.AcceptanceError, match="stopped service"):
        harness.require_upgrade_service_stopped({"status": "running", "exit_code": 0})

    harness.require_upgrade_service_stopped({"status": "exited", "exit_code": 0})


@pytest.mark.parametrize("alteration", ["none", "renamed", "missing_schema"])
def test_live_mcp_advertisement_matches_the_reviewed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alteration: str
) -> None:
    harness = _load_harness()
    tools = json.loads((REPOSITORY / "contracts/cairn-mcp-tools-v1.json").read_text())[
        "tools"
    ]
    if alteration == "renamed":
        tools[-1]["name"] = "unreviewed-tool"
    elif alteration == "missing_schema":
        tools[-1].pop("inputSchema")
    instance = harness.Instance(
        "fixture", "fixture", "fixture-id", 12345, tmp_path, False
    )
    runner = harness.Runner.__new__(harness.Runner)
    runner.recorder = harness.Recorder()

    def reply(url: str, *, token: str, payload: dict[str, Any]) -> tuple[int, Any]:
        result: dict[str, Any]
        if payload["method"] == "initialize":
            result = {"protocolVersion": harness.MCP_PROTOCOL}
        elif payload["method"] == "tools/list":
            result = {"tools": tools}
        else:
            result = {"structuredContent": {"instance_id": "fixture-id"}}
        return 200, {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    monkeypatch.setattr(harness, "_http_json", reply)
    if alteration == "none":
        runner._mcp_round_trip(instance, "synthetic")
    else:
        with pytest.raises(
            harness.AcceptanceError, match="authenticated-mcp-tools-list"
        ):
            runner._mcp_round_trip(instance, "synthetic")
