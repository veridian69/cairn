"""Conversation writes enforce evidence, identities and correction recovery."""

import asyncio
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_arrival_briefing import memory_support as memory_support

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import DurableObservation, MemoryClient, RememberFailure
from cairn.client.conversation_sources import HostSource, create_source_bundle
from cairn.client.profiles import MemoryProfile
from cairn.client.turn_receipts import ReceiptJournal

SOURCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SECOND_SOURCE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SESSION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
HOST_TASK = "Copper Finch: capacity was estimated at twelve; eight is confirmed. Propose staffing."


def adapter_module() -> Any:
    assert importlib.util.find_spec("cairn.client.conversation") is not None, (
        "verified conversation adapter is missing"
    )
    return importlib.import_module("cairn.client.conversation")


def adapter(
    http: httpx.AsyncClient,
    instance: Any,
    principal: UUID,
    *,
    source_body: str = HOST_TASK,
    receipt_path: Path | None = None,
    read_only: bool = False,
    assessment_context: dict[str, object] | None = None,
) -> Any:
    profile = MemoryProfile(
        endpoint=str(http.base_url).rstrip("/"),
        expected_instance_id=instance.config.instance_id,
        scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
        classification=Classification.INTERNAL,
        credential_file=Path("/unused"),
        session_id=SESSION_ID,
    )
    client = MemoryClient(
        http,
        scope=profile.scope,
        classification=profile.classification,
        expected_instance_id=profile.expected_instance_id,
    )
    sources = create_source_bundle(
        profile,
        expected_principal=principal,
        sources=(
            HostSource(SOURCE_ID, source_body),
            HostSource(SECOND_SOURCE_ID, "An independent host task."),
        ),
    )
    receipts = None
    if receipt_path is not None:
        ReceiptJournal.create(receipt_path, sources)
        receipts = ReceiptJournal.open(receipt_path, sources)
    return adapter_module().ConversationAdapter(
        client,
        profile=profile,
        expected_principal=principal,
        sources=sources,
        receipts=receipts,
        read_only=read_only,
        assessment_context=assessment_context,
    )


def test_uuid_constraints_are_visible_without_narrowing_runtime_contract() -> None:
    module = adapter_module()
    expected = {
        ("remember", "source_id"): module.CANONICAL_UUID_PATTERN,
        ("remember", "idempotency_key"): module.CANONICAL_UUID_PATTERN,
        ("replace", "fact_id"): module.CANONICAL_UUID4_PATTERN,
        ("replace", "source_id"): module.CANONICAL_UUID_PATTERN,
        ("replace", "idempotency_key"): module.CANONICAL_UUID_PATTERN,
        ("history", "fact_id"): module.CANONICAL_UUID4_PATTERN,
        ("acknowledge_visit", "visit_id"): module.CANONICAL_UUID_PATTERN,
    }
    for (tool, field), pattern in expected.items():
        field_schema = module.INPUTS[tool].model_json_schema()["properties"][field]
        assert field_schema["type"] == "string"
        assert field_schema["format"] == "uuid"
        assert field_schema["pattern"] == pattern
        assert field_schema["minLength"] == field_schema["maxLength"] == 36
        validator = Draft202012Validator(
            {"type": "object", "properties": {field: field_schema}}
        )
        valid = (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            if field == "fact_id"
            else "aaaaaaaa-aaaa-faaa-8aaa-aaaaaaaaaaaa"
        )
        assert validator.is_valid({field: valid})
        for invalid in (
            valid.upper(),
            "{" + valid + "}",
            valid + "\n",
            valid[:19] + "7" + valid[20:],
            "not-a-uuid",
        ):
            assert not validator.is_valid({field: invalid})
        if field == "fact_id":
            assert not validator.is_valid(
                {field: "aaaaaaaa-aaaa-faaa-8aaa-aaaaaaaaaaaa"}
            )

    general = "aaaaaaaa-aaaa-faaa-8aaa-aaaaaaaaaaaa"
    assert (
        module.Remember.model_validate(
            {"body": "Candidate.", "source_id": general, "idempotency_key": general}
        ).source_id
        == general
    )
    assert module.Acknowledge.model_validate({"visit_id": general}).visit_id == general
    with pytest.raises(module.ValidationError):
        module.History.model_validate({"fact_id": general})


@pytest.mark.anyio
async def test_tracked_write_persists_intent_before_dispatch_and_verified_result(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    path = tmp_path / "receipts.json"
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal, receipt_path=path)
        observed = []

        async def inspect_intent(request: httpx.Request) -> None:
            if request.url.path.endswith("/remember"):
                observed.append(ReceiptJournal.summarise(path, service.sources))

        http.event_hooks["request"].append(inspect_intent)
        result = await service.call(
            "remember",
            {
                "body": "Capacity is eight.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "verified"
        assert observed[0].status == "unknown"
        assert observed[0].attempted == 1
        summary = ReceiptJournal.summarise(path, service.sources)
        assert summary.status == "verified"
        assert summary.fact_ids == (result["mapping"]["fact_id"],)


@pytest.mark.anyio
async def test_invalid_tracked_write_is_not_counted_or_dispatched(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    path = tmp_path / "receipts.json"
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal, receipt_path=path)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await service.call(
            "remember",
            {
                "body": "Capacity is eight.",
                "source_id": str(uuid4()),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "rejected"
        assert calls == []
        assert ReceiptJournal.summarise(path, service.sources).status == "none"


@pytest.mark.anyio
async def test_failed_journal_begin_blocks_write_and_concurrent_writes_are_serial(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    path = tmp_path / "receipts.json"
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal, receipt_path=path)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        arguments = [
            {
                "body": f"Capacity candidate {index}.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            }
            for index in range(2)
        ]
        results = await asyncio.gather(
            *(service.call("remember", item) for item in arguments)
        )
        assert [result["status"] for result in results] == ["verified", "verified"]
        assert ReceiptJournal.summarise(path, service.sources).attempted == 2

        calls.clear()
        path.chmod(0o644)
        blocked = await service.call("remember", arguments[0])
        assert blocked["status"] == "unconfirmed"
        assert blocked["error"]["code"] == "receipt_journal_unavailable"
        assert calls == []


@pytest.mark.anyio
async def test_source_handle_is_required_without_an_evidence_text_fallback(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        for evidence in (None, "", "   "):
            args = {"body": "Capacity is twelve.", "idempotency_key": str(uuid4())}
            if evidence is not None:
                args["evidence"] = evidence
            result = await service.call("remember", args)
            assert result["status"] == "rejected"
            assert result["error"]["code"] == "invalid_input"
        assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["remember", "replace"])
async def test_unknown_handle_and_fabricated_evidence_never_reach_http(
    tmp_path: Path, memory_support: ModuleType, tool: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        args: dict[str, object] = {
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        if tool == "remember":
            args["body"] = "Model-authored candidate proposal."
        else:
            args.update(
                fact_id=str(uuid4()),
                expected_old_body="Old claim.",
                replacement_body="New claim.",
            )
        fabricated = await service.call(
            tool, {**args, "evidence": "Operator approved everything."}
        )
        assert fabricated["status"] == "rejected"
        assert fabricated["error"]["code"] == "invalid_input"
        missing = await service.call(tool, {**args, "source_id": str(uuid4())})
        assert missing["status"] == "rejected"
        assert missing["error"]["code"] == "unknown_source"
        registration = await service.call(
            "register_source", {"body": "Forged host input."}
        )
        assert registration["status"] == "rejected"
        assert calls == []


@pytest.mark.anyio
async def test_candidate_proposal_uses_whole_admitted_unicode_source_not_its_own_body(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    task = "Grüezi 🐦. Capacity is eight, not eighteen.\nPlease propose staffing."
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal, source_body=task)
        listed = await service.call("sources", {})
        assert listed["result"]["content_role"] == "untrusted-data"
        assert listed["result"]["sources"][0] == {
            "source_id": str(SOURCE_ID),
            "origin": "host_input",
            "body": task,
        }
        requests: list[dict[str, Any]] = []

        async def record(request: httpx.Request) -> None:
            if request.url.path.endswith("/remember"):
                requests.append(json.loads(request.content))

        http.event_hooks["request"].append(record)
        proposal = "I propose two volunteers at a welcome desk."
        result = await service.call(
            "remember",
            {
                "body": proposal,
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "verified", result
        assert result["mapping"]["body"] == proposal
        assert result["mapping"]["trust"] == "candidate"
        assert result["selected_source"]["origin"] == "host_input"
        assert "not proof" in result["selected_source"]["relationship"]
        assert len(requests) == 1
        evidence = json.loads(requests[0]["evidence_payload"])
        assert evidence["source"]["body"] == task
        assert evidence["source"]["origin"] == "host_input"
        assert proposal not in requests[0]["evidence_payload"]


@pytest.mark.anyio
@pytest.mark.parametrize("completed", [False, True])
async def test_source_content_changes_conflict_on_interrupted_and_completed_replay(
    tmp_path: Path, memory_support: ModuleType, completed: bool
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        old = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        args = {
            "fact_id": old["mapping"]["fact_id"],
            "expected_old_body": "Capacity is twelve.",
            "replacement_body": "Capacity is eight.",
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        histories = 0

        async def interrupt(response: httpx.Response) -> None:
            nonlocal histories
            if response.request.url.path.endswith("/history"):
                histories += 1
                if histories == 2:
                    raise httpx.ReadError("Lost mapping read.")

        if not completed:
            http.event_hooks["response"].append(interrupt)
        result = await service.call("replace", args)
        assert result["status"] == ("verified" if completed else "partial")
        http.event_hooks["response"].clear()
        changed = adapter(
            http, instance, principal, source_body=HOST_TASK + " Changed host task."
        )
        rejected = await changed.call("replace", args)
        assert rejected["status"] != "verified"
        assert rejected["error"]["code"] == "idempotency_conflict", rejected
        resumed = await adapter(http, instance, principal).call("replace", args)
        assert resumed["status"] == "verified", resumed
        assert resumed["remember_receipt"]["status"] == "replayed"


@pytest.mark.anyio
async def test_adapter_constructor_requires_matching_sources_without_fallback(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        with pytest.raises(TypeError):
            adapter_module().ConversationAdapter(
                service.client, profile=service.profile, expected_principal=principal
            )
        with pytest.raises(ValueError):
            adapter_module().ConversationAdapter(
                service.client,
                profile=service.profile,
                expected_principal=principal,
                sources=None,
            )
        with pytest.raises(ValueError):
            adapter_module().ConversationAdapter(
                service.client,
                profile=service.profile,
                expected_principal=principal,
                sources=replace(service.sources, session_id=uuid4()),
            )


@pytest.mark.anyio
async def test_verified_write_and_replacement_survive_restart_and_replay(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        old = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert old["status"] == "verified", old
        assert old["mapping"]["body"] == "Capacity is twelve."
        old_id = old["mapping"]["fact_id"]
        args = {
            "fact_id": old_id,
            "expected_old_body": "Capacity is twelve.",
            "replacement_body": "Capacity is eight.",
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        changed = await service.call("replace", args)
        assert changed["status"] == "verified"
        assert changed["link_verified"] is True
        assert changed["mapping"]["body"] == "Capacity is eight."
        assert changed["correction_receipt"]["status"] == "committed"
        replay = await adapter(http, instance, principal).call("replace", args)
        assert replay["status"] == "verified"
        assert replay["mapping"] == changed["mapping"]
        assert replay["remember_receipt"]["status"] == "replayed"
        assert replay["correction_receipt"]["status"] == "replayed"
        history = await service.call("history", {"fact_id": old_id})
        corrections = history["result"]["data"]["corrections"]
        assert corrections[0]["superseded_by"] == changed["mapping"]["fact_id"]
        assert len(history["result"]["data"]["facts"]) == 2


@pytest.mark.anyio
async def test_pinned_principal_mismatch_sends_no_content(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, uuid4())
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await service.call(
            "remember",
            {
                "body": "Private observation.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "unconfirmed"
        assert result["error"]["code"] == "principal_mismatch"
        assert calls == ["/memory/v1/diagnose"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure_stage", ["remember", "new_readback", "correct", "link_readback"]
)
async def test_lost_substep_response_retains_receipts_and_same_root_recovers(
    tmp_path: Path, memory_support: ModuleType, failure_stage: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        old = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        args = {
            "fact_id": old["mapping"]["fact_id"],
            "expected_old_body": "Capacity is twelve.",
            "replacement_body": "Capacity is eight.",
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        histories = 0
        failed = False

        async def drop(response: httpx.Response) -> None:
            nonlocal histories, failed
            path = response.request.url.path.rsplit("/", 1)[-1]
            if path == "history":
                histories += 1
            target = (
                path
                if path in {"remember", "correct"}
                else (
                    "new_readback"
                    if path == "history" and histories == 2
                    else "link_readback"
                    if path == "history" and histories == 3
                    else ""
                )
            )
            if not failed and target == failure_stage:
                failed = True
                raise httpx.ReadError("SYNTHETIC secret that must not escape")

        http.event_hooks["response"].append(drop)
        result = await service.call("replace", args)
        assert failed
        assert result["status"] in {"partial", "unconfirmed"}
        assert result["stage"] == failure_stage
        assert result["link_verified"] is False
        assert (result["remember_receipt"] is not None) == (failure_stage != "remember")
        assert (result["correction_receipt"] is not None) == (
            failure_stage == "link_readback"
        )
        assert "SYNTHETIC secret" not in json.dumps(result)
        recovered = await adapter(http, instance, principal).call("replace", args)
        assert recovered["status"] == "verified", recovered
        assert recovered["remember_receipt"]["status"] == "replayed"
        assert recovered["link_verified"] is True
        history = await service.call("history", {"fact_id": args["fact_id"]})
        assert len(history["result"]["data"]["facts"]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize(
    "change", ["source_id", "expected_old_body", "idempotency_key"]
)
async def test_changed_recovery_arguments_never_claim_original_workflow_completed(
    tmp_path: Path, memory_support: ModuleType, completed: bool, change: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        old = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        args = {
            "fact_id": old["mapping"]["fact_id"],
            "expected_old_body": "Capacity is twelve.",
            "replacement_body": "Capacity is eight.",
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        histories = 0

        async def interrupt(response: httpx.Response) -> None:
            nonlocal histories
            if response.request.url.path.endswith("/history"):
                histories += 1
                if histories == 2:
                    raise httpx.ReadError("interrupted after saving")

        if not completed:
            http.event_hooks["response"].append(interrupt)
        original = await service.call("replace", args)
        assert original["status"] == ("verified" if completed else "partial")
        http.event_hooks["response"].clear()
        altered = dict(args)
        altered[change] = (
            str(SECOND_SOURCE_ID)
            if change == "source_id"
            else str(uuid4())
            if change == "idempotency_key"
            else "Changed input."
        )
        # A new root on an active old fact is explicitly a different operation,
        # not an identifiable retry; complete the original first to prevent it.
        if change == "idempotency_key" and not completed:
            assert (await service.call("replace", args))["status"] == "verified"
        changed = await service.call("replace", altered)
        assert changed["status"] != "verified"
        assert changed["link_verified"] is False
        restored = await service.call("replace", args)
        assert restored["status"] == "verified", restored


@pytest.mark.anyio
async def test_unexpected_post_commit_failure_preserves_confirmed_receipt(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)

        async def fail(response: httpx.Response) -> None:
            if response.request.url.path.endswith("/history"):
                raise RuntimeError("secret exception")

        http.event_hooks["response"].append(fail)
        result = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "partial"
        assert result["remember_receipt"]["status"] == "committed"
        assert result["error"] == {"code": "internal_error"}
        assert "secret exception" not in json.dumps(result)


@pytest.mark.anyio
async def test_ancestor_fact_cannot_be_replaced_from_child_profile(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http, scope=Scope("acme", ()), classification=Classification.INTERNAL
        )
        receipt = await client.remember(
            (DurableObservation("Ancestor fact."),), idempotency_key=uuid4()
        )
        assert receipt.result is not None
        identities = receipt.result["fact_ids"]
        assert isinstance(identities, tuple)
        service = adapter(http, instance, principal)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await service.call(
            "replace",
            {
                "fact_id": identities[0],
                "expected_old_body": "Ancestor fact.",
                "replacement_body": "Changed ancestor.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["error"]["code"] == "exact_scope_required"
        assert not any(path.endswith(("/remember", "/correct")) for path in calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    [
        "body",
        "classification",
        "scope",
        "source_principal_id",
        "assertion_id",
        "trust",
        "budget_exhausted",
    ],
)
async def test_unverifiable_readback_preserves_custody_without_claiming_mapping(
    tmp_path: Path, memory_support: ModuleType, field: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)

        async def alter(response: httpx.Response) -> None:
            if not response.request.url.path.endswith("/history"):
                return
            await response.aread()
            document = response.json()
            fact = document["facts"][0]
            if field == "budget_exhausted":
                document[field] = True
            else:
                fact[field] = {
                    "body": "Wrong body.",
                    "classification": "public",
                    "scope": {"realm": "acme", "segments": []},
                    "source_principal_id": str(uuid4()),
                    "assertion_id": str(uuid4()),
                    "trust": "failed-approach",
                }[field]
            document["budget_consumed"] = sum(
                len(
                    json.dumps(
                        record,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                )
                for key in ("facts", "corrections", "disagreements", "resolutions")
                for record in document[key]
            )
            response._content = json.dumps(document).encode()

        http.event_hooks["response"].append(alter)
        result = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert result["status"] == "partial"
        assert result["mapping"] is None
        assert result["remember_receipt"]["status"] == "committed"


@pytest.mark.anyio
async def test_competing_invalidation_leaves_partial_replacement_not_false_link(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        old = await service.call(
            "remember",
            {
                "body": "Capacity is twelve.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        old_id = UUID(old["mapping"]["fact_id"])
        competed = False

        async def compete(request: httpx.Request) -> None:
            nonlocal competed
            if request.url.path.endswith("/correct") and not competed:
                competed = True
                await service.client.correct(
                    (old_id,), reason="Separate withdrawal.", idempotency_key=uuid4()
                )

        http.event_hooks["request"].append(compete)
        result = await service.call(
            "replace",
            {
                "fact_id": str(old_id),
                "expected_old_body": "Capacity is twelve.",
                "replacement_body": "Capacity is eight.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        assert competed
        assert result["status"] == "partial"
        assert result["remember_receipt"] is not None
        assert result["correction_receipt"] is None
        assert result["link_verified"] is False
        history = await service.call("history", {"fact_id": str(old_id)})
        assert history["result"]["data"]["corrections"][0]["superseded_by"] is None


@pytest.mark.anyio
async def test_arrival_does_not_acknowledge_until_explicit_tool_call(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        arrival = await service.call("arrive", {"query": "current work"})
        assert arrival["status"] == "ok", arrival
        snapshot = arrival["result"]["visit"]["snapshot"]
        assert snapshot["acknowledged_at"] is None
        acknowledged = await service.call(
            "acknowledge_visit", {"visit_id": snapshot["visit_id"]}
        )
        assert acknowledged["status"] == "ok", acknowledged
        assert (
            acknowledged["result"]["snapshot"]["acknowledged_at"]
            == snapshot["visit_at"]
        )


@pytest.mark.anyio
async def test_instance_mismatch_rejects_content_and_model_cannot_override_context(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        original = adapter(http, instance, principal)
        profile = replace(original.profile, expected_instance_id=uuid4())
        client = MemoryClient(
            http,
            scope=profile.scope,
            classification=profile.classification,
            expected_instance_id=profile.expected_instance_id,
        )
        service = adapter_module().ConversationAdapter(
            client,
            profile=profile,
            expected_principal=principal,
            sources=create_source_bundle(
                profile,
                expected_principal=principal,
                sources=(HostSource(SOURCE_ID, HOST_TASK),),
            ),
        )
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        args = {
            "body": "Private fact.",
            "source_id": str(SOURCE_ID),
            "idempotency_key": str(uuid4()),
        }
        rejected = await service.call("remember", {**args, "scope": {}})
        assert rejected["status"] == "rejected"
        assert calls == []
        result = await service.call("remember", args)
        assert result["error"]["code"] == "connection_refused"
        assert calls == ["/memory/v1/diagnose"]


@pytest.mark.anyio
@pytest.mark.parametrize("evidence_id", [None, "not-a-uuid"])
async def test_evidence_bearing_client_rejects_missing_or_invalid_evidence_receipt(
    evidence_id: str | None,
) -> None:
    from test_memory_client import _remember_body

    document = _remember_body()
    receipt_result = document["result"]
    assert isinstance(receipt_result, dict)
    receipt_result["evidence_id"] = evidence_id

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as http:
        client = MemoryClient(
            http, scope=Scope("cairn", ()), classification=Classification.INTERNAL
        )
        with pytest.raises(RememberFailure):
            await client.remember(
                (DurableObservation("A fact."),),
                idempotency_key=uuid4(),
                evidence_payload="A source.",
            )


@pytest.mark.anyio
async def test_remember_receipt_is_bounded_before_decoding() -> None:
    from test_memory_client import _remember_body

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps(_remember_body()).encode() + b" " * 20000
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as http:
        client = MemoryClient(
            http, scope=Scope("cairn", ()), classification=Classification.INTERNAL
        )
        with pytest.raises(RememberFailure):
            await client.remember(
                (DurableObservation("A fact."),), idempotency_key=uuid4()
            )


@pytest.mark.anyio
async def test_arrival_with_failed_briefing_is_partial_and_keeps_visit_receipt(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)

        async def fail(request: httpx.Request) -> None:
            if request.url.path.endswith("/recall"):
                raise httpx.ReadError("Unavailable recall.")

        http.event_hooks["request"].append(fail)
        result = await service.call("arrive", {"query": "current work"})
        assert result["status"] == "partial"
        assert result["result"]["visit"]["snapshot"]["visit_id"]
        assert result["result"]["briefing"]["failures"]
        assert result["result"]["visit"]["snapshot"]["acknowledged_at"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["arrive", "recall", "history"])
async def test_conversation_read_budget_cannot_be_supplied_by_the_model(
    tmp_path: Path, memory_support: ModuleType, tool: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        args: dict[str, object] = (
            {"fact_id": str(uuid4())} if tool == "history" else {"query": "capacity"}
        )
        args["budget"] = 6
        validator = Draft202012Validator(
            adapter_module().INPUTS[tool].model_json_schema()
        )
        assert not validator.is_valid(args)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        result = await service.call(tool, args)
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "invalid_input"
        assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["arrive", "recall", "history"])
async def test_conversation_reads_retrieve_fixture_facts_with_fixed_byte_budget(
    tmp_path: Path, memory_support: ModuleType, tool: str
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(http, instance, principal)
        saved = await service.call(
            "remember",
            {
                "body": "Copper Finch capacity is eight.",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        fact_id = saved["mapping"]["fact_id"]
        budgets: list[int] = []

        async def record(request: httpx.Request) -> None:
            if request.url.path.endswith(("/recall", "/history")):
                budgets.append(json.loads(request.content)["budget"])

        http.event_hooks["request"].append(record)
        result = await service.call(
            tool,
            {"fact_id": fact_id}
            if tool == "history"
            else {"query": "Copper Finch capacity"},
        )
        assert result["status"] == "ok", result
        if tool == "arrive":
            facts = result["result"]["briefing"]["recall"]["data"]["hits"]
            # The existing briefing divides its aggregate byte budget between
            # recall and selected histories; each call is not the whole budget.
            assert result["result"]["briefing"]["budget"] == 16384
            assert budgets and all(1 <= budget <= 16384 for budget in budgets)
        else:
            facts = result["result"]["data"]["facts" if tool == "history" else "hits"]
            assert budgets == [16384]
        assert any(fact["fact_id"] == fact_id for fact in facts)


@pytest.mark.anyio
async def test_read_only_adapter_blocks_mutations_before_dispatch_or_journalling(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    path = tmp_path / "read-only-receipts.json"
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        writer = adapter(http, instance, principal)
        saved = await writer.call(
            "remember",
            {
                "body": "Existing work",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
        )
        service = adapter(http, instance, principal, receipt_path=path, read_only=True)
        calls: list[str] = []

        async def record(request: httpx.Request) -> None:
            calls.append(request.url.path)

        http.event_hooks["request"].append(record)
        mutations = {
            "remember": {
                "body": "A fact",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
            "replace": {
                "fact_id": str(uuid4()),
                "expected_old_body": "Old",
                "replacement_body": "New",
                "source_id": str(SOURCE_ID),
                "idempotency_key": str(uuid4()),
            },
            "arrive": {"query": "work"},
            "acknowledge_visit": {"visit_id": str(uuid4())},
            "future_mutation": {},
        }
        for name, arguments in mutations.items():
            result = await service.call(name, arguments)
            assert result["status"] == "rejected"
            assert result["error"] == {"code": "read_only_tool"}
        assert calls == []
        assert ReceiptJournal.summarise(path, service.sources).attempted == 0
        for name, arguments in {
            "check": {},
            "sources": {},
            "recall": {"query": "work"},
            "history": {"fact_id": saved["mapping"]["fact_id"]},
        }.items():
            result = await service.call(name, arguments)
            assert result["status"] == "ok", result
        assert calls


@pytest.mark.anyio
async def test_read_only_mcp_inventory_contains_only_nonmutating_tools(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    from mcp.types import ListToolsRequest, ListToolsResult

    from cairn.client.conversation_mcp import build_server

    instance = memory_support.Instance(tmp_path)
    principal, _ = instance.add_actor()
    async with memory_support.serve(instance) as http:
        service = adapter(http, instance, principal, read_only=True)
        server = build_server(service)
        result = await server.request_handlers[ListToolsRequest](ListToolsRequest())
        assert isinstance(result.root, ListToolsResult)
        assert {tool.name for tool in result.root.tools} == {
            "check",
            "sources",
            "recall",
            "history",
        }
        assert all(
            tool.annotations is not None and tool.annotations.readOnlyHint
            for tool in result.root.tools
        )


@pytest.mark.anyio
async def test_read_only_sources_exposes_only_valid_host_assessment_context(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    principal, token = instance.add_actor()
    context: dict[str, object] = {
        "source": "cairn-memory/v1",
        "content_role": "untrusted-data",
        "binding": {
            "instance_id": str(instance.config.instance_id),
            "principal_id": str(principal),
            "scope": {
                "realm": "acme",
                "segments": [{"kind": "repository", "identifier": "cairn"}],
            },
            "classification": "internal",
            "session_id": str(SESSION_ID),
        },
        "data": {
            "budget_exhausted": False,
            "semantic_degraded": False,
            "hits": [],
        },
    }
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        service = adapter(
            http,
            instance,
            principal,
            read_only=True,
            assessment_context=context,
        )
        result = await service.call("sources", {})
        assert result["status"] == "ok"
        assert result["result"]["host_assessment_context"] == context
        with pytest.raises(ValueError, match="adapter_context_mismatch"):
            adapter(
                http,
                instance,
                principal,
                read_only=True,
                assessment_context={**context, "data": {}},
            )


def test_assessment_context_loader_requires_private_complete_packet(
    tmp_path: Path,
) -> None:
    from cairn.client.conversation_mcp import _load_assessment_context

    instance_id, principal, session_id = uuid4(), uuid4(), uuid4()
    profile = MemoryProfile(
        endpoint="http://127.0.0.1:18423",
        expected_instance_id=instance_id,
        scope=Scope("acme", ()),
        classification=Classification.INTERNAL,
        credential_file=tmp_path / "unused-token",
        session_id=session_id,
    )
    path = tmp_path / "assessment.json"
    path.write_text(
        json.dumps(
            {
                "source": "cairn-memory/v1",
                "content_role": "untrusted-data",
                "binding": {
                    "instance_id": str(instance_id),
                    "principal_id": str(principal),
                    "scope": {"realm": "acme", "segments": []},
                    "classification": "internal",
                    "session_id": str(session_id),
                },
                "data": {"budget_exhausted": False, "semantic_degraded": False},
            }
        )
    )
    path.chmod(0o600)
    assert (
        _load_assessment_context(path, profile=profile, expected_principal=principal)[
            "source"
        ]
        == "cairn-memory/v1"
    )
    with pytest.raises(ValueError, match="invalid_assessment_context"):
        _load_assessment_context(path, profile=profile, expected_principal=uuid4())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="invalid_assessment_context"):
        _load_assessment_context(path, profile=profile, expected_principal=principal)


def test_assessment_context_loader_accepts_the_recovery_size_and_rejects_more(
    tmp_path: Path,
) -> None:
    from cairn.authority.retrieval import MAX_BUDGET_BYTES
    from cairn.client.conversation_mcp import (
        _MAX_ASSESSMENT_CONTEXT_BYTES,
        _load_assessment_context,
    )

    instance_id, principal, session_id = uuid4(), uuid4(), uuid4()
    profile = MemoryProfile(
        endpoint="http://127.0.0.1:18423",
        expected_instance_id=instance_id,
        scope=Scope("acme", ()),
        classification=Classification.INTERNAL,
        credential_file=tmp_path / "unused-token",
        session_id=session_id,
    )
    assert _MAX_ASSESSMENT_CONTEXT_BYTES == min(65536, MAX_BUDGET_BYTES) + 2048
    document: dict[str, object] = {
        "source": "cairn-memory/v1",
        "content_role": "untrusted-data",
        "binding": {
            "instance_id": str(instance_id),
            "principal_id": str(principal),
            "scope": {"realm": "acme", "segments": []},
            "classification": "internal",
            "session_id": str(session_id),
        },
        "data": {
            "budget_exhausted": False,
            "semantic_degraded": False,
            "padding": "",
        },
    }
    encoded = json.dumps(document, separators=(",", ":")).encode()
    document["data"] = {
        "budget_exhausted": False,
        "semantic_degraded": False,
        "padding": "x" * (_MAX_ASSESSMENT_CONTEXT_BYTES - len(encoded)),
    }
    encoded = json.dumps(document, separators=(",", ":")).encode()
    assert len(encoded) == _MAX_ASSESSMENT_CONTEXT_BYTES
    path = tmp_path / "assessment.json"
    path.write_bytes(encoded)
    path.chmod(0o600)
    assert (
        _load_assessment_context(path, profile=profile, expected_principal=principal)[
            "source"
        ]
        == "cairn-memory/v1"
    )
    path.write_bytes(encoded + b"x")
    with pytest.raises(ValueError, match="invalid_assessment_context"):
        _load_assessment_context(path, profile=profile, expected_principal=principal)


@pytest.mark.parametrize("read_only", [False, True])
def test_mcp_cli_passes_read_only_mode(
    monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    from cairn.client import conversation_mcp

    received: list[tuple[bool, Path | None]] = []

    async def fake_serve(
        *args: object,
        read_only: bool = False,
        assessment_context_path: Path | None = None,
    ) -> None:
        received.append((read_only, assessment_context_path))

    argv = [
        "cairn-conversation-mcp",
        "--profile",
        "/unused/profile.json",
        "--expected-principal",
        str(uuid4()),
        "--sources-file",
        "/unused/sources.json",
    ]
    if read_only:
        argv.append("--read-only")
    monkeypatch.setattr(conversation_mcp, "serve", fake_serve)
    monkeypatch.setattr("sys.argv", argv)
    conversation_mcp.run()
    assert received == [(read_only, None)]


def test_mcp_cli_passes_private_assessment_context_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.client import conversation_mcp

    received: list[tuple[bool, Path | None]] = []

    async def fake_serve(
        *args: object,
        read_only: bool = False,
        assessment_context_path: Path | None = None,
    ) -> None:
        received.append((read_only, assessment_context_path))

    context = Path("/tmp/private-assessment-context.json")
    monkeypatch.setattr(conversation_mcp, "serve", fake_serve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cairn-conversation-mcp",
            "--profile",
            "/unused/profile.json",
            "--expected-principal",
            str(uuid4()),
            "--sources-file",
            "/unused/sources.json",
            "--read-only",
            "--assessment-context-file",
            str(context),
        ],
    )
    conversation_mcp.run()
    assert received == [(True, context)]
