"""Installed reference examples and real SDK proposal effects, not host discovery."""

import hashlib
import io
import json
import os
import re
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, RFC_4122, UUID, uuid5

import anyio
import pytest
from host_workflow_fixture import build_cli_runtime
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from test_arrival_briefing import memory_support as memory_support
from test_host_workflow_bridge import private_staging_umask as private_staging_umask
from test_host_workflow_bridge import stage_bridge
from test_memory_cli import inventory, profile
from test_memory_cli_process import server

from cairn.client.cli_input import parse
from cairn.client.host_installation import install_host_workflow

ROOT = Path(__file__).resolve().parents[2]
PROPOSALS = frozenset(
    {"propose", "proposal-list", "proposal-read", "proposal-accept", "proposal-reject"}
)


def proposal_example(skill: str) -> dict[str, Any]:
    examples = [
        json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", skill, re.DOTALL)
    ]
    proposals = [
        value for value in examples if type(value) is dict and "proposal_id" in value
    ]
    assert len(proposals) == 1, (
        "reference must provide one usable proposal-input example"
    )
    return proposals[0]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_skill_reference_example_matches_actual_cli_and_v5_keys(provider: str) -> None:
    skill = (ROOT / "integrations" / provider / "cairn-memory/SKILL.md").read_text()
    example = proposal_example(skill)
    parsed = parse("propose", io.BytesIO(json.dumps(example).encode()))
    assert parsed.idempotency_key is not None
    assert (
        parsed.idempotency_key.variant == RFC_4122
        and parsed.idempotency_key.version == 5
    )
    assert parsed.proposal_id == UUID(example["proposal_id"])
    assert parsed.target_scope is not None
    assert not {"scope", "expected_instance_id", "session_id"} & example.keys()


@pytest.mark.anyio
@pytest.mark.parametrize("command", sorted(PROPOSALS))
async def test_sdk_inventory_and_per_caller_proposal_refusal(command: str) -> None:
    # Real SDK stdio; denied calls must not reach any subprocess runner.
    program = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "import anyio; import scripts.host_workflow_bridge as b; "
        "b.run_cli=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not launch')); "
        f"anyio.run(b.serve, b.BridgeConfig('codex', '0'*64, frozenset({{'{command}'}})))"
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-I", "-c", program], env={}
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == ["read_installed_skill", "run_daily_cli"]
            admitted = tools[1].inputSchema["properties"]["argv"]["enum"]
            assert admitted == [
                ["--help"],
                [command, "--help"],
                ["--profile", "/cli/profile.json", command],
            ]
            for denied in PROPOSALS - {command}:
                for argv in (
                    [denied, "--help"],
                    ["--profile", "/cli/profile.json", denied],
                ):
                    response = await client.call_tool(
                        "run_daily_cli", {"argv": argv, "stdin": ""}
                    )
                    assert response.isError
                    assert isinstance(response.content[0], types.TextContent)
                    assert (
                        json.loads(response.content[0].text)["error"]
                        == "invalid_request"
                    )
            response = await client.call_tool(
                "run_daily_cli",
                {
                    "argv": ["--profile", "/private/other-profile", command],
                    "stdin": "{}",
                },
            )
            assert response.isError and "private" not in response.model_dump_json()


def request(command: str, body: object, *, code: int = 0) -> dict[str, Any]:
    return {
        "command": command,
        "argv": ["--profile", "/cli/profile.json", command],
        "stdin": json.dumps(body),
        "exit_code": code,
    }


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.host_isolation
def test_built_skill_sdk_lifecycle_and_real_lost_publication_replay(
    tmp_path: Path, memory_support: ModuleType, provider: str
) -> None:
    from scripts.host_workflow_sandbox import run_host, seal

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    stage_bridge(stage)
    runtime = build_cli_runtime(stage / "cli-runtime")
    assert runtime.file_count < 8100
    assets = stage / "cli-runtime/site-packages/cairn/host_workflows"
    installation = install_host_workflow(
        provider, stage / "work", assets_root=assets, apply=True
    )
    raw = (installation.path / "SKILL.md").read_bytes()
    assert (
        raw == (ROOT / "integrations" / provider / "cairn-memory/SKILL.md").read_bytes()
    )
    digest = hashlib.sha256(raw).hexdigest()
    body = proposal_example(raw.decode())
    instance = memory_support.Instance(tmp_path / "instance")
    principal, token = instance.add_actor(segments=[])
    _, narrow_token = instance.add_actor(
        operations=["retrieve"], read_clearance="internal"
    )

    async def seed() -> dict[str, Any]:
        async with memory_support.serve(instance) as http:
            result = await memory_support.Api(http, token).remember(
                "Synthetic reusable knowledge",
                evidence_payload="Synthetic publication evidence",
            )
            assert isinstance(result, dict)
            return result

    source = anyio.run(seed)["result"]
    body["source_fact_id"] = source["fact_ids"][0]
    pid = body["proposal_id"]

    def key(name: str) -> str:
        return str(
            uuid5(NAMESPACE_URL, f"cairn-synthetic-host-proposal:{provider}:{name}")
        )

    accept = dict(
        proposal_id=pid,
        evidence_id=source["evidence_id"],
        target_classification="restricted",
        idempotency_key=key("accept"),
    )
    other = {
        **body,
        "proposal_id": key("rejected-proposal"),
        "idempotency_key": key("other-proposal"),
    }
    reject = dict(
        proposal_id=other["proposal_id"],
        reason="Not for publication",
        idempotency_key=key("reject"),
    )
    os.umask(0o077)
    (stage / "host-config/bridge.json").write_text(
        json.dumps(
            {
                "provider": provider,
                "skill_sha256": digest,
                "allowed_commands": sorted(PROPOSALS),
            }
        )
    )
    fingerprints: list[str] = []
    commands: list[str] = []
    scenario: dict[str, Any] = {
        "provider": provider,
        "skill_sha256": digest,
        "principal_id": str(principal),
        "instance_id": str(instance.config.instance_id),
    }

    def exercise(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scenario["requests"] = requests
        manifest = seal(stage)
        fingerprints.append(manifest.fingerprint)
        result = run_host(manifest, stdin=json.dumps(scenario).encode(), deadline=60)
        assert result.returncode == 0, "synthetic SDK proposal scenario failed"
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
        (stage / "cli-config/credential").write_text(token)
        initial = inventory(instance)
        with server(instance, listener):
            recorded = exercise(
                [
                    *[
                        {
                            "command": "help",
                            "argv": [name, "--help"],
                            "stdin": "",
                            "exit_code": 0,
                        }
                        for name in sorted(PROPOSALS)
                    ],
                    request("propose", body),
                    request("propose", other),
                    request("proposal-reject", reject),
                ]
            )
            for item in recorded[-3:]:
                receipt = item["proposal_receipt"]
                assert receipt["outcome"] == "committed"
                assert set(receipt["result"]) == {"proposal_id"}
            assert inventory(instance)["facts"] == initial["facts"]
            before_reads = inventory(instance)
            reads = exercise(
                [
                    request("proposal-read", {"proposal_id": pid}),
                    request("proposal-list", {"limit": 100, "after": None}),
                ]
            )
            assert (
                reads[0]["proposal_state"] == "pending" and reads[0]["decision"] is None
            )
            assert reads[1]["proposal_ids"] == sorted([pid, other["proposal_id"]])
            assert reads[1]["next_cursor"] is None
            assert inventory(instance) == before_reads

        lost_request = {
            **request("proposal-accept", accept, code=3),
            "error_code": "transport_error",
            "stage": "unconfirmed",
            "recovery": "resubmit_identical_proposal-accept_same_idempotency_key_and_fields",
        }
        with server(instance, listener, "proposal-accept") as doomed:
            lost = exercise([lost_request])[0]
            assert doomed.wait(timeout=5) == 73
            assert lost["diagnostic"] == {
                "error": {"code": "transport_error", "operation": "proposal-accept"},
                "last_confirmed_stage": "unconfirmed",
                "recovery": lost_request["recovery"],
            }
        published = inventory(instance)
        assert len(published["facts"]) == len(initial["facts"]) + 1
        with server(instance, listener):
            replay = exercise([request("proposal-accept", accept)])[0][
                "proposal_receipt"
            ]
            assert replay["outcome"] == "replayed"
            assert replay["result"]["evidence_id"] == source["evidence_id"]
            assert replay["result"]["promotions"][0][0] == source["fact_ids"][0]
            assert len(replay["result"]["promotions"]) == 1
            assert inventory(instance) == published
            # Fixed staging is resealed; only the controller switches to a disposable
            # reader that can read the internal source proposal, not the restricted publication.
            (stage / "cli-config/credential").write_text(narrow_token)
            before_reads = inventory(instance)
            reads = exercise(
                [
                    request("proposal-read", {"proposal_id": pid}),
                    request("proposal-list", {}),
                ]
            )
            assert reads[0]["proposal_state"] == "accepted"
            assert reads[0]["decision"]["promoted_fact_id"] is None
            assert inventory(instance) == before_reads

    print(
        json.dumps(
            {
                "proposal_host_evidence": {
                    "provider_asset": provider,
                    "skill_sha256": digest,
                    "wheel_sha256": runtime.wheel_sha256,
                    "requirements_sha256": runtime.requirements_sha256,
                    "protocol_sha256": hashlib.sha256(
                        (
                            stage / "host-runtime/scripts/host_workflow_protocol.py"
                        ).read_bytes()
                    ).hexdigest(),
                    "bridge_sha256": hashlib.sha256(
                        (
                            stage / "host-runtime/scripts/host_workflow_bridge.py"
                        ).read_bytes()
                    ).hexdigest(),
                    "manifests": fingerprints,
                    "commands": commands,
                    "publication_count": 1,
                    "provider_processes": 0,
                }
            },
            sort_keys=True,
        )
    )
