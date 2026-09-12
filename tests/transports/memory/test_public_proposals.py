"""Public proposal lifecycle exercises the actual ASGI and catalogue boundary."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from asgi_lifespan import LifespanManager
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)
from cairn.client.errors import MemoryOperationFailure
from cairn.client.memory import MemoryClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def rows(instance: Instance, table: str) -> list[tuple[Any, ...]]:
    with read_connection(instance.data_path) as con:
        return [tuple(row) for row in con.execute(f'SELECT * FROM "{table}"')]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("operation", ["propose", "proposal-accept", "proposal-reject"])
async def test_i27_public_keys_refuse_safely_and_v5_replays(
    tmp_path: Path, transport: str, operation: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token, transport)
        source = (await api.remember("Synthetic source", evidence_payload="Measured"))[
            "result"
        ]
        # Proposal references deliberately remain canonical non-RFC UUIDs.
        pid = "11111111-1111-1111-1111-111111111111"
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        creation = {
            **context,
            "proposal_id": pid,
            "source_fact_id": source["fact_ids"][0],
            "target_scope": {"realm": "acme", "segments": []},
            "reason": "Reuse",
        }
        if operation != "propose":
            assert (await api.call("propose", creation, key=str(uuid4())))[
                "outcome"
            ] == "committed"
        body = creation if operation == "propose" else {**context, "proposal_id": pid}
        if operation == "proposal-accept":
            body.update(
                evidence_id=source["evidence_id"], target_classification="internal"
            )
        elif operation == "proposal-reject":
            body.update(reason="No")
        with read_connection(instance.data_path) as con:
            tables = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not row[0].startswith("audit_") and row[0] != "sqlite_sequence"
            ]
        before = {table: rows(instance, table) for table in tables}
        good_key = str(uuid5(NAMESPACE_URL, transport + operation))
        bad_keys = [
            *(f"22222222-2222-5222-{n}222-222222222222" for n in "01234567cdef"),
            "22222222-2222-2222-2222-222222222222",
            "00000000-0000-0000-0000-000000000000",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
            good_key.upper(),
            "{" + good_key + "}",
            "urn:uuid:" + good_key,
            good_key.replace("-", ""),
            good_key + " ",
        ]
        for key in bad_keys:
            audit_count = len(rows(instance, "audit_events"))
            result = await api.call(operation, body, key=key)
            assert result["failure"]["code"] == "invalid_request", result
            assert key not in json.dumps(result)
            assert {table: rows(instance, table) for table in tables} == before
            assert len(rows(instance, "audit_events")) == audit_count + 1
            with read_connection(instance.data_path) as con:
                event = json.loads(
                    con.execute(
                        "SELECT canonical_event FROM audit_events WHERE chain_kind='instance' ORDER BY sequence DESC LIMIT 1"
                    ).fetchone()[0]
                )
            assert event["outcome"] == "deny"
            assert len(event["safe_request_fingerprint"]) == 64
            assert event["action_code"] == f"memory-{operation}"
            assert event["idempotency_key"] is None and event["mutation_id"] is None
            assert key not in json.dumps(event)
        committed = await api.call(operation, body, key=good_key)
        assert committed["outcome"] == "committed"
        after = {table: rows(instance, table) for table in tables}
        replay = await api.call(operation, body, key=good_key)
        assert replay["outcome"] == "replayed"
        assert replay["mutation_receipt"] == committed["mutation_receipt"]
        assert {table: rows(instance, table) for table in tables} == after
        assert len(rows(instance, "facts")) == (
            2 if operation == "proposal-accept" else 1
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_worker_proposes_reviewer_reads_and_rejects_without_publication(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    author, token = instance.add_actor(operations=["ingest", "retrieve"])
    reviewer, reviewer_token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        worker, review = (
            Api(http, token, transport),
            Api(http, reviewer_token, transport),
        )
        source = (await worker.remember("Reusable observation"))["result"]["fact_ids"][
            0
        ]
        facts = rows(instance, "facts")
        pid, key = str(uuid4()), str(uuid4())
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        body = {
            **context,
            "proposal_id": pid,
            "source_fact_id": source,
            "target_scope": {"realm": "acme", "segments": []},
            "reason": "Reuse this finding",
        }
        created = await worker.call("propose", body, key=key)
        assert created.get("outcome") == "committed", created
        assert created["result"] == {"proposal_id": pid}
        replay = await worker.call("propose", body, key=key)
        assert replay["outcome"] == "replayed"
        assert replay["mutation_receipt"] == created["mutation_receipt"]
        page = await review.call("proposal-list", context)
        snapshot = await review.call("proposal-read", {**context, "proposal_id": pid})
        assert page == {"items": [snapshot], "next_cursor": None}
        assert snapshot["proposed_by"] == str(author)
        assert (
            snapshot["source_fact_id"] == source
            and snapshot["source_trust"] == "candidate"
        )
        assert snapshot["state"] == "pending" and snapshot["decision"] is None
        denied = await worker.call(
            "proposal-reject",
            {**context, "proposal_id": pid, "reason": "No"},
            key=str(uuid4()),
        )
        assert denied["failure"]["code"] == "authorisation_denied"
        rejected = await review.call(
            "proposal-reject",
            {**context, "proposal_id": pid, "reason": "Keep local"},
            key=str(uuid4()),
        )
        assert rejected["result"] == {"proposal_id": pid}
        final = await review.call("proposal-read", {**context, "proposal_id": pid})
        assert final["state"] == "rejected"
        assert final["decision"]["decided_by"] == str(reviewer)
        assert (
            final["decision"]["mutation_id"]
            == rejected["mutation_receipt"]["mutation_id"]
        )
        assert final["decision"]["evidence_id"] is None
        assert rows(instance, "facts") == facts
        assert len(rows(instance, "memory_proposal_decisions")) == 1


@pytest.mark.anyio
async def test_actual_client_rejection_receipt_and_audit(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        source = (await Api(http, token).remember("Reusable"))["result"]["fact_ids"][0]
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        pid, key = (
            uuid5(NAMESPACE_URL, "client-rejection"),
            uuid5(NAMESPACE_URL, "reject-key"),
        )
        await client.propose(
            pid,
            source_fact_id=UUID(source),
            target_scope=Scope("acme", ()),
            reason="Review é",
            idempotency_key=uuid5(NAMESPACE_URL, "propose-key"),
        )
        facts = rows(instance, "facts")
        rejected = await client.proposal_reject(
            pid, reason="Keep here é", idempotency_key=key
        )
        replay = await client.proposal_reject(
            pid, reason="Keep here é", idempotency_key=key
        )
        assert rejected.outcome == "committed" and replay.outcome == "replayed"
        assert rejected.result.proposal_id == pid and replay.result == rejected.result
        assert rejected.mutation_receipt == replay.mutation_receipt
        view = await client.proposal_read(pid)
        assert view.decision is not None and view.decision.reason == "Keep here é"
        assert (
            str(view.decision.mutation_id) == rejected.mutation_receipt["mutation_id"]
        )
        with read_connection(instance.data_path) as con:
            row = con.execute(
                "SELECT canonical_event FROM audit_events WHERE event_id=?",
                (rejected.audit_receipt["event_id"],),
            ).fetchone()
            assert row is not None
            audit = json.loads(row[0])
            assert audit["mutation_id"] == rejected.mutation_receipt["mutation_id"]
            assert (
                audit["command_digest"] == rejected.mutation_receipt["command_digest"]
            )
            assert audit["action_code"] == "memory-proposal-reject"
        assert rows(instance, "facts") == facts


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "delta",
    [
        {"limit": True},
        {"limit": 101},
        {"after": "BAD"},
        {"principal_id": "private-input"},
        {"expected_instance_id": "BAD"},
    ],
)
async def test_malformed_public_proposals_are_opaque_and_audited(
    tmp_path: Path, transport: str, delta: dict[str, object]
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        result = await Api(http, token, transport).call(
            "proposal-list",
            {
                "scope": SCOPE,
                "expected_instance_id": str(instance.config.instance_id),
                **delta,
            },
        )
        assert result.get("failure", {}).get("code") == "invalid_request", result
        assert "private-input" not in str(result)
        with read_connection(instance.data_path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM audit_events WHERE action_code='memory-proposal-list'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.anyio
async def test_actual_client_acceptance_and_explicit_replay(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token)
        source_result = (
            await api.remember(
                "Reusable observation", evidence_payload="Measured evidence"
            )
        )["result"]
        source, evidence = (
            UUID(source_result["fact_ids"][0]),
            UUID(source_result["evidence_id"]),
        )
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        pid, key = uuid4(), uuid4()
        made = await client.propose(
            pid,
            source_fact_id=source,
            target_scope=Scope("acme", ()),
            reason="Reuse this",
            idempotency_key=uuid4(),
        )
        assert made.result.proposal_id == pid and made.outcome == "committed"
        snapshot = await client.proposal_read(pid)
        assert snapshot.source_fact_id == source and snapshot.state == "pending"
        assert (await client.proposal_list()).items == (snapshot,)
        accepted = await client.proposal_accept(
            pid,
            evidence_id=evidence,
            target_classification=Classification.INTERNAL,
            idempotency_key=key,
        )
        assert accepted.outcome == "committed"
        assert accepted.result.evidence_id == evidence
        assert accepted.result.promotions[0][0] == source
        replay = await client.proposal_accept(
            pid,
            evidence_id=evidence,
            target_classification=Classification.INTERNAL,
            idempotency_key=key,
        )
        assert replay.outcome == "replayed" and replay.result == accepted.result
        assert replay.mutation_receipt == accepted.mutation_receipt
        final = await client.proposal_read(pid)
        assert (
            final.decision is not None
            and final.decision.promoted_fact_id == accepted.result.promotions[0][1]
        )
        assert len(rows(instance, "facts")) == 2
        assert len(rows(instance, "memory_proposal_decisions")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("other_action", ["proposal-accept", "proposal-reject"])
async def test_distinct_reviewer_keys_race_one_decision(
    tmp_path: Path, transport: str, other_action: str
) -> None:
    instance = Instance(tmp_path)
    _, worker_token = instance.add_actor(operations=["ingest", "retrieve"])
    _, first_token = instance.add_actor(segments=[])
    _, second_token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        worker = Api(http, worker_token, transport)
        source = (
            await worker.remember("Reusable observation", evidence_payload="Evidence")
        )["result"]
        fact = source["fact_ids"][0]
        pid = str(uuid4())
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "proposal_id": pid,
        }
        assert (
            await worker.call(
                "propose",
                {
                    **context,
                    "source_fact_id": fact,
                    "target_scope": {"realm": "acme", "segments": []},
                    "reason": "Reusable",
                },
                key=str(uuid4()),
            )
        )["outcome"] == "committed"
        accept = {
            **context,
            "evidence_id": source["evidence_id"],
            "target_classification": "internal",
        }
        assert (await worker.call("proposal-accept", accept, key=str(uuid4())))[
            "failure"
        ]["code"] == "authorisation_denied"
        source_before = rows(instance, "facts")[0]
        a, b = await asyncio.gather(
            Api(http, first_token, transport).call(
                "proposal-accept", accept, key=str(uuid4())
            ),
            Api(http, second_token, "mcp" if transport == "rest" else "rest").call(
                other_action,
                accept
                if other_action == "proposal-accept"
                else {**context, "reason": "Keep local"},
                key=str(uuid4()),
            ),
        )
        assert sorted([a.get("outcome", "refused"), b.get("outcome", "refused")]) == [
            "committed",
            "refused",
        ]
        failure = a if "failure" in a else b
        assert failure["failure"]["code"] == "idempotency_conflict"
        assert len(rows(instance, "memory_proposal_decisions")) == 1
        assert source_before in rows(instance, "facts")
        accepted = (
            a.get("result", {}).get("promotions")
            or b.get("result", {}).get("promotions")
        ) is not None
        assert len(rows(instance, "facts")) == (2 if accepted else 1)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_public_scope_grant_loss_and_publication_disclosure(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    worker, worker_token = instance.add_actor(operations=["ingest", "retrieve"])
    _, reviewer_token = instance.add_actor(segments=[])
    _, public_token = instance.add_actor(
        segments=[], read_clearance="internal", operations=["retrieve"]
    )
    async with serve(instance) as http:
        author, review, public = (
            Api(http, worker_token, transport),
            Api(http, reviewer_token, transport),
            Api(http, public_token, transport),
        )
        source = (
            await author.remember(
                "Restricted source",
                classification="restricted",
                evidence_payload="Evidence",
            )
        )["result"]
        pid, key = str(uuid4()), str(uuid4())
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        proposal = {
            **context,
            "proposal_id": pid,
            "source_fact_id": source["fact_ids"][0],
            "target_scope": {"realm": "acme", "segments": []},
            "reason": "Private discussion",
        }
        assert (await author.call("propose", proposal, key=key))[
            "outcome"
        ] == "committed"
        for api in (author, review):
            broad = await api.call(
                "proposal-list", {**context, "scope": {"realm": "acme", "segments": []}}
            )
            assert (
                broad.get("items") == []
                or broad.get("failure", {}).get("code") == "authorisation_denied"
            )
        hidden = await public.call("proposal-read", {**context, "proposal_id": pid})
        assert hidden["failure"]["code"] == "authorisation_denied" and pid not in str(
            hidden
        )
        wrong = await review.call(
            "proposal-read",
            {**context, "proposal_id": pid, "scope": {"realm": "acme", "segments": []}},
        )
        assert wrong["failure"]["code"] == "authorisation_denied"
        assert (await public.call("proposal-list", context))["items"] == []
        before = rows(instance, "memory_proposals")
        with _open_write_connection(instance.data_path, create=False) as con:
            con.execute(
                "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                (canonical_timestamp(instance.clock()), str(worker)),
            )
            con.commit()
        replay = await author.call("propose", proposal, key=key)
        assert replay["failure"]["code"] == "authorisation_denied"
        assert rows(instance, "memory_proposals") == before


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_public_changed_payload_and_legacy_promotion_domain(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token, transport)
        source = (await api.remember("Reusable", evidence_payload="Evidence"))["result"]
        fact, evidence, pid, key = (
            source["fact_ids"][0],
            source["evidence_id"],
            str(uuid4()),
            str(uuid4()),
        )
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "proposal_id": pid,
        }
        target = {"realm": "acme", "segments": []}
        proposed = {
            **context,
            "source_fact_id": fact,
            "target_scope": target,
            "reason": "Reusable",
        }
        assert (await api.call("propose", proposed, key=key))["outcome"] == "committed"
        assert (await api.call("propose", {**proposed, "reason": "Changed"}, key=key))[
            "failure"
        ]["code"] == "idempotency_conflict"
        promotion_key = str(uuid4())
        legacy = await http.post(
            "/v1/promote",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": promotion_key,
            },
            json={
                "fact_ids": [fact],
                "evidence": {"evidence_id": evidence},
                "target_scope": target,
                "target_classification": "internal",
                "reason": "Reusable",
            },
        )
        assert legacy.status_code == 200, legacy.json()
        accepted = await api.call(
            "proposal-accept",
            {**context, "evidence_id": evidence, "target_classification": "internal"},
            key=promotion_key,
        )
        assert accepted["outcome"] == "committed", accepted
        assert accepted["mutation_receipt"] != legacy.json()["mutation_receipt"]
        replay = await api.call(
            "proposal-accept",
            {**context, "evidence_id": evidence, "target_classification": "internal"},
            key=promotion_key,
        )
        assert (
            replay["outcome"] == "replayed" and replay["result"] == accepted["result"]
        )
        changed = await api.call(
            "proposal-accept",
            {**context, "evidence_id": evidence, "target_classification": "restricted"},
            key=promotion_key,
        )
        assert changed["failure"]["code"] == "idempotency_conflict"
        assert (
            len(rows(instance, "facts")) == 3
            and len(rows(instance, "memory_proposal_decisions")) == 1
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "case",
    [
        "missing_key",
        "malformed_key",
        "read_key",
        "instance",
        "reason_bytes",
        "scope",
        "classification_override",
        "trust_override",
        "author_override",
        "upper_uuid",
    ],
)
async def test_wire_refusals_precede_proposal_mutation(
    tmp_path: Path, transport: str, case: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        fact = (await api.remember("Source"))["result"]["fact_ids"][0]
        body: dict[str, Any] = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "proposal_id": str(uuid4()),
            "source_fact_id": fact,
            "target_scope": SCOPE,
            "reason": "Reuse",
        }
        key: str | None = str(uuid4())
        name = "propose"
        if case == "missing_key":
            key = None
        if case == "malformed_key":
            key = "not-a-key"
        if case == "read_key":
            name, body = (
                "proposal-list",
                {
                    "scope": SCOPE,
                    "expected_instance_id": str(instance.config.instance_id),
                },
            )
        if case == "instance":
            body["expected_instance_id"] = str(uuid4())
        if case == "reason_bytes":
            body["reason"] = "é" * 2049
        if case == "scope":
            body["scope"] = {
                "realm": "acme",
                "segments": [{"kind": "bad kind", "identifier": "x"}],
            }
        if case == "classification_override":
            body["classification"] = "public"
        if case == "trust_override":
            body["trust"] = "validated"
        if case == "author_override":
            body["proposed_by"] = str(uuid4())
        if case == "upper_uuid":
            body["proposal_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        result = await api.call(name, body, key=key)
        assert result.get("failure", {}).get("code") == (
            "authorisation_denied" if case == "instance" else "invalid_request"
        ), result
        assert rows(instance, "memory_proposals") == []
        assert rows(instance, "memory_proposal_decisions") == []
        assert len(rows(instance, "facts")) == 1


@pytest.mark.anyio
async def test_duplicate_json_and_read_only_tool_annotations(tmp_path: Path) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        response = await http.post(
            "/memory/v1/propose",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=b'{"scope":{},"scope":{}}',
        )
        assert response.status_code == 400
        with read_connection(instance.data_path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM audit_events WHERE action_code='memory-propose'"
                ).fetchone()[0]
                == 1
            )
        tools = await http.post(
            "/memory/v1/mcp",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        inventory = {tool["name"]: tool for tool in tools.json()["result"]["tools"]}
        for name in ("proposal-list", "proposal-read"):
            assert inventory[name]["annotations"]["readOnlyHint"] is True
        for name in ("propose", "proposal-accept", "proposal-reject"):
            assert not inventory[name].get("annotations", {}).get("readOnlyHint", False)


@pytest.mark.anyio
async def test_lost_acceptance_response_then_explicit_client_replay(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    application = instance.application()

    class LostResponse(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=application)
            self.lost = False
            self.calls: list[str] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls.append(request.url.path)
            response = await self.inner.handle_async_request(request)
            if request.url.path.endswith("proposal-accept") and not self.lost:
                self.lost = True
                await response.aclose()
                raise httpx.ReadError("synthetic lost acknowledgement")
            return response

    transport = LostResponse()
    async with (
        LifespanManager(application),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
    ):
        source = (
            await Api(http, token).remember("Reusable", evidence_payload="Evidence")
        )["result"]
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        pid, key = uuid4(), uuid4()
        await client.propose(
            pid,
            source_fact_id=UUID(source["fact_ids"][0]),
            target_scope=Scope("acme", ()),
            reason="Reusable",
            idempotency_key=uuid4(),
        )
        with pytest.raises(MemoryOperationFailure) as error:
            await client.proposal_accept(
                pid,
                evidence_id=UUID(source["evidence_id"]),
                target_classification=Classification.INTERNAL,
                idempotency_key=key,
            )
        assert error.value.failure.code == "transport_error"
        assert transport.calls.count("/memory/v1/proposal-accept") == 1
        assert (
            len(rows(instance, "facts")) == 2
            and len(rows(instance, "memory_proposal_decisions")) == 1
        )
        replay = await client.proposal_accept(
            pid,
            evidence_id=UUID(source["evidence_id"]),
            target_classification=Classification.INTERNAL,
            idempotency_key=key,
        )
        assert replay.outcome == "replayed"
        assert transport.calls.count("/memory/v1/proposal-accept") == 2
        assert (
            len(rows(instance, "facts")) == 2
            and len(rows(instance, "memory_proposal_decisions")) == 1
        )
        assert transport.calls[-1] == "/memory/v1/proposal-accept"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_publication_does_not_grant_private_discussion_and_replay_preserves_invalidation(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    public_actor, public_token = instance.add_actor(
        segments=[], operations=["retrieve"]
    )
    async with serve(instance) as http:
        api, public = Api(http, token, transport), Api(http, public_token, transport)
        source = (await api.remember("Reusable source", evidence_payload="Evidence"))[
            "result"
        ]
        pid, key = str(uuid4()), str(uuid4())
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
            "proposal_id": pid,
        }
        await api.call(
            "propose",
            {
                **context,
                "source_fact_id": source["fact_ids"][0],
                "target_scope": {"realm": "acme", "segments": []},
                "reason": "Private discussion",
            },
            key=str(uuid4()),
        )
        body = {
            **context,
            "evidence_id": source["evidence_id"],
            "target_classification": "internal",
        }
        accepted = await api.call("proposal-accept", body, key=key)
        published = accepted["result"]["promotions"][0]["derived_fact_id"]
        # Even broad inherited read rights do not make a proposal visible in
        # the publication's explicitly selected scope.
        recalled = await public.call(
            "recall", {"scope": {"realm": "acme", "segments": []}, "query": "Reusable"}
        )
        assert published in str(recalled)
        hidden = await public.call(
            "proposal-read", {**context, "scope": {"realm": "acme", "segments": []}}
        )
        assert hidden["failure"]["code"] == "authorisation_denied"
        assert "Private discussion" not in str(hidden)
        assert (
            await public.call(
                "proposal-list",
                {
                    "scope": {"realm": "acme", "segments": []},
                    "expected_instance_id": str(instance.config.instance_id),
                },
            )
        )["items"] == []
        invalidated = await api.call(
            "correct",
            {
                "scope": SCOPE,
                "fact_ids": source["fact_ids"],
                "reason": "Historical source",
            },
            key=str(uuid4()),
        )
        assert invalidated["outcome"] == "committed"
        replay = await api.call("proposal-accept", body, key=key)
        assert (
            replay["outcome"] == "replayed" and replay["result"] == accepted["result"]
        )
        view = await api.call("proposal-read", context)
        assert view["source_invalidated"] and view["state"] == "accepted"
        assert (
            len(rows(instance, "facts")) == 2
            and len(rows(instance, "fact_invalidations")) == 1
        )


@pytest.mark.anyio
async def test_client_success_survives_read_grant_loss_after_commit(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor(segments=[])
    application = instance.application()

    class RevokeAfterSuccess(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await httpx.ASGITransport(app=application).handle_async_request(
                request
            )
            if (
                request.url.path.endswith("proposal-accept")
                and response.status_code == 200
            ):
                with _open_write_connection(instance.data_path, create=False) as con:
                    con.execute(
                        "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                        (canonical_timestamp(instance.clock()), str(principal)),
                    )
                    con.commit()
            return response

    async with (
        LifespanManager(application),
        httpx.AsyncClient(
            transport=RevokeAfterSuccess(),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
    ):
        source = (
            await Api(http, token).remember("Reusable", evidence_payload="Evidence")
        )["result"]
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        pid = uuid4()
        await client.propose(
            pid,
            source_fact_id=UUID(source["fact_ids"][0]),
            target_scope=Scope("acme", ()),
            reason="Reusable",
            idempotency_key=uuid4(),
        )
        accepted = await client.proposal_accept(
            pid,
            evidence_id=UUID(source["evidence_id"]),
            target_classification=Classification.INTERNAL,
            idempotency_key=uuid4(),
        )
        assert accepted.outcome == "committed"
        assert len(rows(instance, "facts")) == 2
        with pytest.raises(MemoryOperationFailure):
            await client.proposal_read(pid)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_real_hundred_item_page_and_canonical_v5_replay(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        fact = (await api.remember("Reusable"))["result"]["fact_ids"][0]
        context = {
            "scope": SCOPE,
            "expected_instance_id": str(instance.config.instance_id),
        }
        ids = sorted(
            str(uuid5(NAMESPACE_URL, f"synthetic-proposal-{i}")) for i in range(101)
        )
        for pid in ids:
            body = {
                **context,
                "proposal_id": pid,
                "source_fact_id": fact,
                "target_scope": SCOPE,
                "reason": "é" * 2048,
            }
            key = str(uuid5(NAMESPACE_URL, pid))
            created = await api.call("propose", body, key=key)
            assert created["outcome"] == "committed", created
        replay = await api.call("propose", body, key=key)
        assert (
            replay["outcome"] == "replayed"
            and replay["mutation_receipt"] == created["mutation_receipt"]
        )
        page = await api.call("proposal-list", {**context, "limit": 100})
        assert [item["proposal_id"] for item in page["items"]] == ids[:100]
        assert page["next_cursor"] == ids[99]
        remainder = await api.call(
            "proposal-list", {**context, "limit": 100, "after": page["next_cursor"]}
        )
        assert [item["proposal_id"] for item in remainder["items"]] == ids[100:]
        assert remainder["next_cursor"] is None
        assert (
            len(rows(instance, "facts")) == 1
            and rows(instance, "memory_proposal_decisions") == []
        )
