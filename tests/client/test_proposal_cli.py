"""Proposal commands preserve explicit identities and actual publication custody."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import Boundary, cli, inventory, invoke, profile
from test_proposal_client_boundary import snapshot

from cairn.client.cli_input import FIELDS, parse

PID = "11111111-1111-1111-1111-111111111111"
KEY = "22222222-2222-5222-8222-222222222222"
ROOT = {"realm": "acme", "segments": []}
COMMANDS = (
    "propose",
    "proposal-read",
    "proposal-list",
    "proposal-accept",
    "proposal-reject",
)


def proposal(source: str = PID) -> dict[str, Any]:
    return dict(
        proposal_id=PID,
        source_fact_id=source,
        target_scope=ROOT,
        reason="Reuse this knowledge",
        idempotency_key=KEY,
    )


def sessionless(path: Path) -> Path:
    document = json.loads(path.read_text())
    document.pop("session_id")
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize(
    "command,body",
    [
        ("propose", proposal()),
        ("proposal-read", {"proposal_id": PID}),
        ("proposal-list", {"limit": 100, "after": None}),
        (
            "proposal-accept",
            dict(
                proposal_id=PID,
                evidence_id=PID,
                target_classification="public",
                idempotency_key=KEY,
            ),
        ),
        ("proposal-reject", dict(proposal_id=PID, reason="No", idempotency_key=KEY)),
    ],
)
def test_proposal_reference_ids_use_canonical_uuid_without_rfc_restriction(
    command: str, body: dict[str, Any]
) -> None:
    value = parse(command, io.BytesIO(json.dumps(body).encode()))
    if command != "proposal-list":
        assert value.proposal_id == UUID(PID)
    else:
        assert value.limit == 100 and value.after is None


@pytest.mark.parametrize("field", ["proposal_id", "source_fact_id"])
def test_canonical_non_rfc_proposal_values(field: str) -> None:
    value = parse(
        "propose", io.BytesIO(json.dumps({**proposal(), field: PID}).encode())
    )
    assert getattr(value, field) == UUID(PID)


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["propose", "proposal-accept", "proposal-reject"])
@pytest.mark.parametrize(
    "key",
    [
        *(f"22222222-2222-5222-{n}222-222222222222" for n in "01234567cdef"),
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "ABCDEFAB-CDEF-5ABC-8ABC-ABCDEFABCDEF",
        "{" + KEY + "}",
        "urn:uuid:" + KEY,
        KEY.replace("-", ""),
        KEY + " ",
    ],
)
async def test_i27_cli_key_refusal_precedes_profile_and_network(
    tmp_path: Path, command: str, key: str
) -> None:
    body = proposal() if command == "propose" else dict(proposal_id=PID)
    if command == "proposal-accept":
        body.update(evidence_id=PID, target_classification="internal")
    elif command == "proposal-reject":
        body.update(reason="No")
    body["idempotency_key"] = key

    async def never(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid key reached network")

    output = io.BytesIO()
    code, result = await invoke(
        tmp_path / "missing-profile", command, body, httpx.MockTransport(never), output
    )
    assert code == 2 and output.getvalue() == b""
    assert result["result"]["error"] == {"code": "invalid_input", "operation": "input"}


@pytest.mark.parametrize("version", "0123456789abcdef")
@pytest.mark.parametrize("variant", "89ab")
def test_i27_cli_keys_are_version_unpinned(version: str, variant: str) -> None:
    key = f"22222222-2222-{version}222-{variant}222-222222222222"
    value = parse(
        "propose",
        io.BytesIO(json.dumps({**proposal(), "idempotency_key": key}).encode()),
    )
    assert value.idempotency_key == UUID(key)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command,body",
    [
        *(
            (command, {})
            for command in (
                "propose",
                "proposal-read",
                "proposal-accept",
                "proposal-reject",
            )
        ),
        *(
            (command, b'{"proposal_id":"private","proposal_id":"other"}')
            for command in COMMANDS
        ),
        *((command, b'{"private":NaN}') for command in COMMANDS),
        *((command, b'{"private":Infinity}') for command in COMMANDS),
        *((command, b"[]") for command in COMMANDS),
        *(
            ("propose", {**proposal(), field: value})
            for field, value in [
                ("reason", ""),
                ("reason", "é" * 2049),
                ("reason", None),
                ("reason", "\ud800"),
                ("proposal_id", 1),
                ("proposal_id", PID.upper().replace("1111", "AAAA", 1)),
                ("proposal_id", PID.replace("-", "")),
                ("idempotency_key", None),
                ("source_fact_id", "private"),
                ("scope", ROOT),
                ("trust", "validated"),
                ("target_scope", None),
                ("target_scope", {**ROOT, "extra": 1}),
                ("target_scope", {"realm": "Acme", "segments": []}),
                ("target_scope", {"realm": "a" * 64, "segments": []}),
                ("target_scope", {"realm": "acme", "segments": {}}),
                (
                    "target_scope",
                    {
                        "realm": "acme",
                        "segments": [{"kind": "repo", "identifier": "x"}] * 17,
                    },
                ),
                (
                    "target_scope",
                    {"realm": "acme", "segments": [{"kind": "repo", "id": "x"}]},
                ),
                (
                    "target_scope",
                    {
                        "realm": "acme",
                        "segments": [{"kind": "r" * 64, "identifier": "x"}],
                    },
                ),
                (
                    "target_scope",
                    {
                        "realm": "acme",
                        "segments": [{"kind": "repo", "identifier": "x" * 256}],
                    },
                ),
                (
                    "target_scope",
                    {
                        "realm": "acme",
                        "segments": [{"kind": "repo", "identifier": "é"}],
                    },
                ),
            ]
        ),
        *(
            ("proposal-list", body)
            for body in [
                {"limit": True},
                {"limit": 0},
                {"limit": 101},
                {"limit": None},
                {"after": "private"},
                {"idempotency_key": KEY},
            ]
        ),
        ("proposal-read", {"proposal_id": PID, "idempotency_key": KEY}),
        ("propose", {k: v for k, v in proposal().items() if k != "idempotency_key"}),
        ("proposal-reject", {"proposal_id": PID, "reason": "No"}),
        (
            "proposal-accept",
            {"proposal_id": PID, "evidence_id": PID, "target_classification": "public"},
        ),
        *(
            (
                "proposal-accept",
                dict(
                    proposal_id=PID,
                    evidence_id=PID,
                    idempotency_key=KEY,
                    target_classification=value,
                ),
            )
            for value in ["secret", None, 1, "PUBLIC"]
        ),
    ],
)
async def test_invalid_input_precedes_profile_and_network(
    tmp_path: Path, command: str, body: object
) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid input reached network")

    output = io.BytesIO()
    code, result = await invoke(
        tmp_path / "missing-private-profile",
        command,
        body,
        httpx.MockTransport(never),
        output,
    )
    assert code == 2 and output.getvalue() == b""
    assert result["result"]["error"] == {"code": "invalid_input", "operation": "input"}
    assert "private" not in json.dumps(result)


def test_exact_public_input_bounds_and_defaults() -> None:
    body = {
        **proposal(),
        "reason": "é" * 2048,
        "target_scope": {
            "realm": "r" * 63,
            "segments": [{"kind": "k" * 63, "identifier": "i" * 255}] * 16,
        },
    }
    assert (
        parse("propose", io.BytesIO(json.dumps(body).encode())).reason == body["reason"]
    )
    assert parse("proposal-list", io.BytesIO(b"{}")).limit == 50
    assert parse(
        "proposal-list", io.BytesIO(json.dumps({"after": PID, "limit": 1}).encode())
    ).after == UUID(PID)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "boundary", ["first-diagnose", "second-diagnose", "proposal-read", "proposal-list"]
)
async def test_preflight_and_read_loss_are_operational_not_mutation_recovery(
    tmp_path: Path, memory_support: ModuleType, boundary: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        seen: list[str] = []

        async def fail_boundary(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            seen.append(name)
            if (
                boundary == "first-diagnose"
                and len(seen) == 1
                or boundary == "second-diagnose"
                and len(seen) == 2
                or boundary == name
            ):
                raise httpx.ReadError("private network failure")
            return cast(
                httpx.Response, await http._transport.handle_async_request(request)
            )

        command = "proposal-list" if boundary == "proposal-list" else "proposal-accept"
        body = (
            {}
            if command == "proposal-list"
            else dict(
                proposal_id=PID,
                evidence_id=PID,
                target_classification="internal",
                idempotency_key=KEY,
            )
        )
        before = inventory(instance)
        code, failed = await invoke(
            path, command, body, httpx.MockTransport(fail_boundary)
        )
        assert code == 4, failed
        assert "recovery" not in failed["result"]
        assert "private" not in json.dumps(failed)
        assert "proposal-accept" not in seen
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize("command", COMMANDS)
async def test_profile_instance_mismatch_never_sends_proposal(
    tmp_path: Path, memory_support: ModuleType, command: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        document = json.loads(path.read_text())
        document["expected_instance_id"] = str(uuid4())
        path.write_text(json.dumps(document))
        body = {
            "propose": proposal(),
            "proposal-list": {},
            "proposal-read": {"proposal_id": PID},
            "proposal-reject": dict(proposal_id=PID, reason="No", idempotency_key=KEY),
            "proposal-accept": dict(
                proposal_id=PID,
                evidence_id=PID,
                target_classification="internal",
                idempotency_key=KEY,
            ),
        }[command]
        transport = Boundary(http._transport)
        before = inventory(instance)
        code, failed = await invoke(path, command, body, transport)
        assert code == 2, failed
        assert transport.paths == ["/memory/v1/diagnose"]
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize("human", [False, True])
async def test_legal_large_page_fails_output_without_partial_write_or_mutation(
    tmp_path: Path, memory_support: ModuleType, human: bool
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        scope = json.loads(path.read_text())["scope"]
        items = [
            {
                **snapshot(),
                "scope": scope,
                "proposal_id": str(UUID(int=i)),
                "reason": "\x01" * 4096,
            }
            for i in range(1, 101)
        ]
        page = {"items": items, "next_cursor": None}
        assert len(json.dumps(page).encode()) > 1048576
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("proposal-list"):
                return httpx.Response(200, json=page)
            assert request.url.path.endswith("diagnose")
            return cast(
                httpx.Response, await http._transport.handle_async_request(request)
            )

        output, error = io.BytesIO(), io.BytesIO()
        before = inventory(instance)
        code = await cli().execute(
            ["--profile", str(path), *(["--human"] if human else []), "proposal-list"],
            stdin=io.BytesIO(b'{"limit":100}'),
            stdout=output,
            stderr=error,
            transport=httpx.MockTransport(handler),
        )
        assert code == 4 and output.getvalue() == b""
        assert json.loads(error.getvalue())["result"]["error"] == {
            "operation": "output",
            "code": "output_failure",
        }
        assert seen == ["/memory/v1/diagnose"] * 2 + ["/memory/v1/proposal-list"]
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "evidence,published",
    [(None, None), (PID, None), (None, PID), (PID, PID)],
    ids=["both-hidden", "evidence-only", "publication-only", "both-readable"],
)
async def test_hidden_decision_references_stay_null(
    tmp_path: Path,
    memory_support: ModuleType,
    evidence: str | None,
    published: str | None,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        packet = snapshot()
        packet.update(
            proposal_id=PID,
            scope=json.loads(path.read_text())["scope"],
            state="accepted",
            decision=dict(
                state="accepted",
                decided_by=packet["proposed_by"],
                recorded_at=packet["recorded_at"],
                mutation_id=str(uuid4()),
                reason=None,
                evidence_id=evidence,
                promoted_fact_id=published,
            ),
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("proposal-read"):
                return httpx.Response(200, json=packet)
            assert request.url.path.endswith("diagnose")
            return cast(
                httpx.Response, await http._transport.handle_async_request(request)
            )

        code, result = await invoke(
            path, "proposal-read", {"proposal_id": PID}, httpx.MockTransport(handler)
        )
        assert code == 0, result
        assert result["result"]["decision"]["evidence_id"] == evidence
        assert result["result"]["decision"]["promoted_fact_id"] == published


def test_built_wheel_console_help(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repository,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stderr.decode()
    (wheel,) = tmp_path.glob("*.whl")
    target = tmp_path / "console"
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr.decode()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(target),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cairn.client.cli; print(cairn.client.cli.__file__)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert imported.returncode == 0
    assert Path(imported.stdout.decode().strip()) == target / "cairn/client/cli.py"
    for command in FIELDS:
        help_result = subprocess.run(
            [str(target / "bin/cairn-memory"), command, "--help"],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert help_result.returncode == 0 and not help_result.stderr
        text = help_result.stdout.decode()
        assert command in text and "canonical" in text
        assert "after may be null" in text
        assert (
            "Proposal reference IDs and cursors accept every canonical lowercase UUID version/variant."
            in text
        )
        if command in {"propose", "proposal-accept", "proposal-reject"}:
            assert "all original fields" in text and "SAME idempotency_key" in text
            description = text.split("Required keys:", 1)[0]
            assert (
                "Mutation keys require canonical lowercase hyphenated RFC 4122 variant UUIDs"
                in description
            )
            assert "version unrestricted, including UUIDv5" in description


@pytest.mark.anyio
async def test_proposal_stdin_limit_is_unchanged(tmp_path: Path) -> None:
    output = io.BytesIO()
    code, failed = await invoke(
        tmp_path / "unused", "propose", b" " * 1048577, stdout=output
    )
    assert code == 2 and output.getvalue() == b""
    assert failed["result"]["error"] == {
        "code": "input_too_large",
        "operation": "input",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["propose", "proposal-accept", "proposal-reject"])
async def test_output_failure_preserves_committed_stage_without_followup(
    tmp_path: Path, memory_support: ModuleType, command: str
) -> None:
    class BrokenOutput(io.BytesIO):
        def write(self, data: Any) -> int:
            raise OSError("private output failure")

    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        source = await memory_support.Api(http, token).remember(
            "Knowledge", evidence_payload="Evidence"
        )
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        body = proposal(source["result"]["fact_ids"][0])
        if command != "propose":
            assert (await invoke(path, "propose", body, http._transport))[0] == 0
            body = dict(proposal_id=PID, idempotency_key=str(uuid4()))
            if command == "proposal-accept":
                body.update(
                    evidence_id=source["result"]["evidence_id"],
                    target_classification="internal",
                )
            else:
                body["reason"] = "No"
        transport = Boundary(http._transport)
        before = len(inventory(instance)["facts"])
        code, result = await invoke(path, command, body, transport, BrokenOutput())
        assert code == 4, result
        assert result["result"] == {
            "error": {"code": "output_failure", "operation": "output"},
            "last_confirmed_stage": "committed",
        }
        assert transport.paths[-1] == "/memory/v1/" + command
        assert transport.paths.count("/memory/v1/" + command) == 1
        assert len(inventory(instance)["facts"]) == before + (
            command == "proposal-accept"
        )
        code, replayed = await invoke(path, command, body, http._transport)
        assert code == 0 and replayed["result"]["outcome"] == "replayed"


@pytest.mark.anyio
async def test_conflicting_replay_and_current_authority_refusals(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        source = await memory_support.Api(http, token).remember(
            "Knowledge", evidence_payload="Evidence"
        )
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        body = proposal(source["result"]["fact_ids"][0])
        assert (await invoke(path, "propose", body, http._transport))[0] == 0
        stable = inventory(instance)
        code, failed = await invoke(
            path, "propose", {**body, "reason": "Changed"}, http._transport
        )
        assert code == 2 and "recovery" not in failed["result"]
        assert failed["result"]["error"]["code"] == "idempotency_conflict"
        assert inventory(instance) == stable
        # Same source visibility, but no ancestor publication authority.
        _, narrow_token = instance.add_actor()
        (tmp_path / "cli-token").write_text(narrow_token)
        before = inventory(instance)
        accept = dict(
            proposal_id=PID,
            evidence_id=source["result"]["evidence_id"],
            target_classification="internal",
            idempotency_key=str(uuid4()),
        )
        code, refused = await invoke(path, "proposal-accept", accept, http._transport)
        assert code == 2 and "recovery" not in refused["result"]
        assert refused["result"]["error"]["code"] == "authorisation_denied"
        assert inventory(instance) == before


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["propose", "proposal-accept", "proposal-reject"])
@pytest.mark.parametrize("loss", ["transport", "internal"])
async def test_lost_mutation_requires_identical_named_replay(
    tmp_path: Path, memory_support: ModuleType, command: str, loss: str
) -> None:
    from test_memory_cli import InternalErrorAfterEffect

    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        source = await memory_support.Api(http, token).remember(
            "Knowledge", evidence_payload="Evidence"
        )
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        body = proposal(source["result"]["fact_ids"][0])
        if command != "propose":
            assert (await invoke(path, "propose", body, http._transport))[0] == 0
            body = dict(proposal_id=PID, idempotency_key=str(uuid4()))
            if command == "proposal-accept":
                body.update(
                    evidence_id=source["result"]["evidence_id"],
                    target_classification="internal",
                )
            else:
                body["reason"] = "Not appropriate"
        before = len(inventory(instance)["facts"])
        transport = (
            Boundary(http._transport, command)
            if loss == "transport"
            else InternalErrorAfterEffect(http._transport, command)
        )
        code, failed = await invoke(path, command, body, transport)
        assert code == 3, failed
        assert failed["result"] == {
            "error": {
                "operation": command,
                "code": "transport_error" if loss == "transport" else "internal_error",
            },
            "last_confirmed_stage": "unconfirmed",
            "recovery": f"resubmit_identical_{command}_same_idempotency_key_and_fields",
        }
        assert "private" not in json.dumps(failed)
        assert transport.paths.count("/memory/v1/" + command) == 1
        replay_transport = Boundary(http._transport)
        code, replay = await invoke(path, command, body, replay_transport)
        assert code == 0, replay
        assert replay["result"]["outcome"] == "replayed"
        assert transport.keys["/memory/v1/" + command] == [body["idempotency_key"]]
        assert replay_transport.keys["/memory/v1/" + command] == [
            body["idempotency_key"]
        ]
        assert len(inventory(instance)["facts"]) == before + (
            command == "proposal-accept"
        )


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["foreign", "descendant", "sibling"])
async def test_impossible_target_fails_before_network(
    tmp_path: Path, memory_support: ModuleType, target: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    path = sessionless(profile(tmp_path, instance, token, "http://127.0.0.1"))
    scope: dict[str, Any] = (
        {"realm": "other", "segments": []}
        if target == "foreign"
        else {
            "realm": "acme",
            "segments": [
                {
                    "kind": "repository",
                    "identifier": "other" if target == "sibling" else "cairn",
                }
            ],
        }
    )
    if target == "descendant":
        scope["segments"].append({"kind": "branch", "identifier": "main"})

    def never(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid target reached network")

    code, result = await invoke(
        path,
        "propose",
        {**proposal(), "target_scope": scope},
        httpx.MockTransport(never),
    )
    assert code == 2, result


@pytest.mark.anyio
async def test_cli_proposal_lifecycle_without_session(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        source = await api.remember("Source knowledge", evidence_payload="Evidence")
        path = sessionless(
            profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        )
        transport = Boundary(http._transport)
        body = proposal(source["result"]["fact_ids"][0])
        before = inventory(instance)
        code, recorded = await invoke(path, "propose", body, transport)
        assert code == 0, recorded
        assert recorded["result"]["result"] == {"proposal_id": PID}
        assert inventory(instance)["facts"] == before["facts"]
        stable = inventory(instance)
        for command, request in [
            ("proposal-read", {"proposal_id": PID}),
            ("proposal-list", {"after": None}),
        ]:
            code, read = await invoke(path, command, request, transport)
            assert code == 0, read
            assert inventory(instance) == stable
        accept = dict(
            proposal_id=PID,
            evidence_id=source["result"]["evidence_id"],
            target_classification="internal",
            idempotency_key=str(uuid4()),
        )
        code, accepted = await invoke(path, "proposal-accept", accept, transport)
        assert code == 0, accepted
        assert (
            accepted["result"]["result"]["promotions"][0][0] == body["source_fact_id"]
        )
        assert len(inventory(instance)["facts"]) == len(before["facts"]) + 1
        code, replayed = await invoke(path, "proposal-accept", accept, transport)
        assert code == 0, replayed
        assert replayed["result"]["outcome"] == "replayed"
        assert replayed["result"]["result"] == accepted["result"]["result"]
        assert len(inventory(instance)["facts"]) == len(before["facts"]) + 1
        other = {**body, "proposal_id": str(uuid4()), "idempotency_key": str(uuid4())}
        assert (await invoke(path, "propose", other, transport))[0] == 0
        reject = dict(
            proposal_id=other["proposal_id"],
            reason="Not appropriate",
            idempotency_key=str(uuid4()),
        )
        code, rejected = await invoke(path, "proposal-reject", reject, transport)
        assert code == 0, rejected
        assert rejected["result"]["result"] == {"proposal_id": other["proposal_id"]}
        assert len(inventory(instance)["facts"]) == len(before["facts"]) + 1
        assert not any("session" in p or "turn" in p for p in transport.paths)
