"""Real installed wheel/jailed SDK disagreement; no native host or provider."""

import hashlib
import io
import json
import os
import re
import socket
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest
from host_workflow_fixture import build_cli_runtime
from test_arrival_briefing import memory_support as memory_support
from test_host_proposals import request
from test_host_proposals import (
    test_sdk_inventory_and_per_caller_proposal_refusal as check_inventory,
)
from test_host_workflow_bridge import private_staging_umask as private_staging_umask
from test_host_workflow_bridge import stage_bridge
from test_memory_cli import inventory, profile
from test_memory_cli_process import server

from cairn.client.cli_input import parse
from cairn.client.host_installation import install_host_workflow

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = "resubmit_identical_disagree_same_idempotency_key_and_fields"


def example(skill: str) -> dict[str, Any]:
    values = [
        json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", skill, re.DOTALL)
    ]
    found = [value for value in values if "left_fact_id" in value]
    assert len(found) == 1, "one executable disagreement example required"
    return dict(found[0])


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_installed_reference_has_explicit_disagreement_example(provider: str) -> None:
    skill = (ROOT / "integrations" / provider / "cairn-memory/SKILL.md").read_text()
    value = parse("disagree", io.BytesIO(json.dumps(example(skill)).encode()))
    assert value.left_fact_id is not None and value.left_fact_id.version == 4
    assert value.right_fact_id is not None and value.right_fact_id.version == 4
    assert value.left_fact_id != value.right_fact_id
    assert value.idempotency_key is not None and value.idempotency_key.version == 5
    assert "same key and all original fields" in skill
    assert "not a correction, invalidation, trust change or resolution" in skill


@pytest.mark.anyio
async def test_sdk_disagreement_inventory_and_restrictions() -> None:
    await check_inventory("disagree")


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.host_isolation
def test_wheel_sdk_suggestion_confirmation_refusal_and_lost_response_replay(
    tmp_path: Path, memory_support: ModuleType, provider: str
) -> None:
    from scripts.host_workflow_sandbox import run_host, seal

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    stage_bridge(stage)
    runtime = build_cli_runtime(stage / "cli-runtime")
    installation = install_host_workflow(
        provider,
        stage / "work",
        assets_root=stage / "cli-runtime/site-packages/cairn/host_workflows",
        apply=True,
    )
    raw = (installation.path / "SKILL.md").read_bytes()
    assert (
        raw == (ROOT / "integrations" / provider / "cairn-memory/SKILL.md").read_bytes()
    )
    digest = hashlib.sha256(raw).hexdigest()
    body = example(raw.decode())
    instance = memory_support.Instance(tmp_path / "instance")
    principal, token = instance.add_actor()
    _, reader_token = instance.add_actor(operations=["retrieve"])

    async def seed() -> list[str]:
        async with memory_support.serve(instance) as http:
            api = memory_support.Api(http, token)
            return [
                (await api.remember(text))["result"]["fact_ids"][0]
                for text in ("Batch size is 32.", "Batch size is 64.")
            ]

    left, right = anyio.run(seed)
    body.update(left_fact_id=left, right_fact_id=right)
    os.umask(0o077)
    (stage / "host-config/bridge.json").write_text(
        json.dumps(
            {
                "provider": provider,
                "skill_sha256": digest,
                "allowed_commands": ["suggest", "disagree", "history"],
            }
        )
    )
    scenario: dict[str, Any] = {
        "provider": provider,
        "skill_sha256": digest,
        "principal_id": str(principal),
        "instance_id": str(instance.config.instance_id),
    }
    fingerprints: list[str] = []
    commands: list[str] = []

    def exercise(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        manifest = seal(stage)
        fingerprints.append(manifest.fingerprint)
        result = run_host(
            manifest,
            stdin=json.dumps({**scenario, "requests": requests}).encode(),
            deadline=60,
        )
        assert result.returncode == 0, "synthetic disagreement SDK scenario failed"
        assert result.stderr == b""
        summary = json.loads(result.stdout)
        assert summary["skill_sha256"] == digest
        commands.extend(item["command"] for item in summary["commands"])
        return list(summary["commands"])

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        supplied = profile(
            tmp_path, instance, token, f"http://127.0.0.1:{listener.getsockname()[1]}"
        )
        config = json.loads(supplied.read_bytes())
        config.pop("session_id")
        config["credential_file"] = "credential"
        (stage / "cli-config/profile.json").write_text(json.dumps(config))
        credential = stage / "cli-config/credential"
        credential.write_text(token)
        initial = inventory(instance)
        with server(instance, listener):
            suggested = exercise(
                [
                    {
                        "command": "help",
                        "argv": ["disagree", "--help"],
                        "stdin": "",
                        "exit_code": 0,
                    },
                    request("suggest", {"observation": "Batch size is 32."}),
                ]
            )
            assert suggested[-1]["suggestions"]
            assert inventory(instance) == initial  # No implicit disagreement.
            credential.write_text(reader_token)
            denied = exercise(
                [
                    {
                        **request("disagree", body, code=2),
                        "error_code": "authorisation_denied",
                        "stage": "unconfirmed",
                    }
                ]
            )[0]
            assert denied["diagnostic"]["error"]["operation"] == "disagree"
            assert inventory(instance) == initial
            credential.write_text(token)

        with server(instance, listener, "disagree") as doomed:
            lost = exercise(
                [
                    {
                        **request("disagree", body, code=3),
                        "error_code": "transport_error",
                        "stage": "unconfirmed",
                        "recovery": RECOVERY,
                    }
                ]
            )[0]
            assert doomed.wait(timeout=5) == 73
            assert lost["diagnostic"] == {
                "error": {"code": "transport_error", "operation": "disagree"},
                "last_confirmed_stage": "unconfirmed",
                "recovery": RECOVERY,
            }
        committed = inventory(instance)
        assert len(committed["memory_disagreements"]) == 1
        assert committed["facts"] == initial["facts"]
        assert committed["fact_invalidations"] == initial["fact_invalidations"]
        with server(instance, listener):
            replay, history = exercise(
                [request("disagree", body), request("history", {"fact_id": left})]
            )
            receipt = replay["disagreement_receipt"]
            assert receipt["status"] == "replayed"
            evidence = history["disagreements"][0]
            assert evidence["relationship_id"] == receipt["result"]["relationship_id"]
            assert evidence["reason"] == body["reason"]
            assert evidence["principal_id"] == str(principal)
            assert inventory(instance) == committed
    print(
        json.dumps(
            {
                "disagreement_host_evidence": {
                    "provider_asset": provider,
                    "skill_sha256": digest,
                    "wheel_sha256": runtime.wheel_sha256,
                    "requirements_sha256": runtime.requirements_sha256,
                    "manifests": fingerprints,
                    "commands": commands,
                    "relationship_count": 1,
                    "provider_processes": 0,
                }
            },
            sort_keys=True,
        )
    )
