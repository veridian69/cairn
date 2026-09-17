"""Durable orchestration uses real custody and never regenerates recovery."""

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support

import cairn.client as client_api
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import Committed, Replayed
from cairn.client import (
    DurableMemorySession,
    DurableObservation,
    DurableSessionFailure,
    MemoryClient,
    MemoryOperationFailure,
    ModelCallback,
    ModelTurn,
    TurnInput,
)


def bound_client(http: httpx.AsyncClient, instance_id: UUID) -> MemoryClient:
    return MemoryClient(
        http,
        scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
        classification=Classification.INTERNAL,
        expected_instance_id=instance_id,
    )


class Intercept(httpx.AsyncBaseTransport):
    """Exercise real ASGI effects, then lose or damage only the response."""

    def __init__(self, upstream: httpx.AsyncBaseTransport) -> None:
        self.upstream = upstream
        self.lose: str | None = None
        self.damage: Callable[[dict[str, Any]], object] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.upstream.handle_async_request(request)
        await response.aread()
        if request.url.path.endswith("/" + str(self.lose)):
            self.lose = None
            raise httpx.ReadError("synthetic lost acknowledgement")
        if request.url.path.endswith("/session-read") and self.damage is not None:
            data = response.json()
            self.damage(data)
            return httpx.Response(200, json=data)
        return response


class ResponseChunks(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b" " * 65536


class ForgeTerminal(Intercept):
    """Keep real fact custody but replace one known immutable turn field."""

    def __init__(
        self,
        upstream: httpx.AsyncBaseTransport,
        field: str,
        *,
        forge_preflight_after: int | None = None,
    ) -> None:
        super().__init__(upstream)
        self.field = field
        self.forged_id = str(uuid4())
        self.forge_preflight_after = forge_preflight_after
        self.prepared_reads = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        commit = request.url.path.endswith("/turn-commit")
        read = request.url.path.endswith("/session-read")
        if response.status_code == 200 and (commit or read):
            data = response.json()
            snapshot = data["result"] if commit else data
            if read:
                if (
                    snapshot["state"] != "prepared"
                    or self.forge_preflight_after is None
                ):
                    return response
                self.prepared_reads += 1
                if self.prepared_reads <= self.forge_preflight_after:
                    return response
            if self.field == "response":
                snapshot["response"] = "X" * len(snapshot["response"])
            else:
                snapshot[self.field] = self.forged_id
            if read:
                payload = {
                    "schema": "cairn.session.preparation/v1",
                    "instance_id": snapshot["instance_id"],
                    "principal_id": snapshot["principal_id"],
                    "classification": snapshot["classification"],
                    "operation": "session-prepare",
                    "command": {
                        key: snapshot[key]
                        for key in (
                            "scope",
                            "session_id",
                            "turn_id",
                            "attempt_id",
                            "response",
                            "observations",
                        )
                    },
                }
                # Keep the forged preflight self-consistent, so the durable
                # caller must compare against its earlier held preparation.
                snapshot["operational_receipt"]["command_digest"] = hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            return httpx.Response(200, json=data)
        return response


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["run", "resume"])
@pytest.mark.parametrize("retry", ["resume", "run", "run-recall-denied"])
async def test_post_ingest_authorisation_loss_stays_pending_with_real_stage(
    path: str,
    retry: str,
    tmp_path: Path,
    memory_support: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    original = CairnAuthority.ingest
    original_time = instance.clock.now
    calls = 0
    turn = ModelTurn("Known output", (DurableObservation("The port is 8123."),))

    def expire_after_ingest(self: CairnAuthority, *args: Any, **kwargs: Any) -> Any:
        outcome = original(self, *args, **kwargs)
        if kwargs.get("commit_guard") is not None and isinstance(
            outcome, (Committed, Replayed)
        ):
            instance.clock.now = original_time.replace(year=2041)
        return outcome

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return turn

    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = bound_client(http, instance.config.instance_id)
        session = DurableMemorySession(client, session_id=uuid4())
        await session.open()
        tid, attempt = uuid4(), uuid4()
        if path == "resume":
            await client.begin_turn(
                session._session_id, tid, attempt_id=attempt, idempotency_key=uuid4()
            )
            await client.prepare_turn(
                session._session_id,
                tid,
                turn,
                attempt_id=attempt,
                idempotency_key=uuid4(),
            )
        with monkeypatch.context() as patcher:
            patcher.setattr(CairnAuthority, "ingest", expire_after_ingest)
            with pytest.raises(DurableSessionFailure) as raised:
                if path == "run":
                    await session.run_turn(
                        "Which port?", callback, turn_id=tid, attempt_id=attempt
                    )
                else:
                    await session.resume(tid)
        with read_connection(instance.data_path) as con:
            fact_ids = [row[0] for row in con.execute("SELECT fact_id FROM facts")]
            assert len(fact_ids) == 1
            assert (
                con.execute("SELECT count(*) FROM memory_session_terminals").fetchone()[
                    0
                ]
                == 0
            )
        failure = raised.value
        assert failure.failure.code == "authorisation_denied"
        assert failure.state == "pending"
        assert failure.operation == "turn-commit"
        assert failure.last_confirmed_stage == "prepared"
        assert failure.completed_turn == turn
        progress = session.persistence_progress(tid)
        assert progress is not None and progress.phase == "pending"
        assert progress.operation == "turn-commit"
        assert progress.last_confirmed_stage == "prepared"
        original_recall = MemoryClient.recall

        async def expire_before_recall(
            self: MemoryClient, *args: Any, **kwargs: Any
        ) -> Any:
            instance.clock.now = original_time.replace(year=2041)
            return await original_recall(self, *args, **kwargs)

        with monkeypatch.context() as patcher:
            if retry == "run-recall-denied":
                # A successful session-level read must not replace the known
                # prepared turn stage with the enclosing session's open state.
                instance.clock.now = original_time
                patcher.setattr(MemoryClient, "recall", expire_before_recall)
            with pytest.raises(DurableSessionFailure) as unreadable:
                if retry == "resume":
                    await session.resume(tid)
                else:
                    await session.run_turn(
                        "Which port?", callback, turn_id=tid, attempt_id=attempt
                    )
        assert unreadable.value.state == "pending"
        assert unreadable.value.operation == (
            "recall" if retry == "run-recall-denied" else "session-read"
        )
        assert unreadable.value.last_confirmed_stage == "prepared"
        progress = session.persistence_progress(tid)
        assert progress is not None and progress.phase == "pending"
        assert progress.last_confirmed_stage == "prepared"
        instance.clock.now = original_time
        recovered = await session.resume(tid)
        assert recovered.completed_turn == turn
        assert (
            recovered.persistence is not None
            and recovered.persistence.result is not None
        )
        assert recovered.persistence.result["fact_ids"] == tuple(fact_ids)
        assert calls == (1 if path == "run" else 0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "operation,stage", [("session-read", "unconfirmed"), ("recall", "open")]
)
async def test_pre_generation_denial_keeps_actual_operation_and_confirmed_stage(
    operation: str, stage: str, tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    calls = 0

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn("Must not generate")

    class ExpireRequest(Intercept):
        armed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self.armed and request.url.path.endswith("/" + operation):
                instance.clock.now = instance.clock.now.replace(year=2041)
            return await super().handle_async_request(request)

    async with memory_support.serve(instance) as upstream:
        transport = ExpireRequest(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            session = DurableMemorySession(
                bound_client(http, instance.config.instance_id), session_id=uuid4()
            )
            await session.open()
            transport.armed = True
            tid = uuid4()
            with pytest.raises(DurableSessionFailure) as raised:
                await session.run_turn(
                    "A port", callback, turn_id=tid, attempt_id=uuid4()
                )
            assert raised.value.operation == operation
            assert raised.value.state == "failed"
            assert raised.value.last_confirmed_stage == stage
            assert raised.value.completed_turn is None
            assert calls == 0
            with read_connection(instance.data_path) as con:
                assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["run", "resume", "low-level", "run-forged-preflight", "resume-forged-preflight"],
)
@pytest.mark.parametrize("field", ["response", "attempt_id", "replaces_turn_id"])
async def test_terminal_cannot_replace_known_preparation_with_real_custody(
    path: str, field: str, tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    turn = ModelTurn("Known output", (DurableObservation("The port is 8123."),))
    calls = 0

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return turn

    async with memory_support.serve(instance) as upstream:
        transport = ForgeTerminal(
            upstream._transport,
            field,
            forge_preflight_after=(1 if path.startswith("resume") else 0)
            if path.endswith("forged-preflight")
            else None,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            sid, tid, attempt = uuid4(), uuid4(), uuid4()
            session = DurableMemorySession(client, session_id=sid)
            await session.open()
            if not path.startswith("run"):
                await client.begin_turn(
                    sid, tid, attempt_id=attempt, idempotency_key=uuid4()
                )
                await client.prepare_turn(
                    sid, tid, turn, attempt_id=attempt, idempotency_key=uuid4()
                )
            with pytest.raises(MemoryOperationFailure) as raised:
                if path.startswith("run"):
                    await session.run_turn(
                        "Which port?", callback, turn_id=tid, attempt_id=attempt
                    )
                elif path.startswith("resume"):
                    await session.resume(tid)
                else:
                    await client.commit_turn(sid, tid, idempotency_key=uuid4())
            assert raised.value.failure.code == "invalid_response"
            assert raised.value.operation == "turn-commit"
            if path != "low-level":
                assert isinstance(raised.value, DurableSessionFailure)
                assert raised.value.state == "pending"
                assert raised.value.completed_turn == turn
                assert raised.value.last_confirmed_stage == "prepared"
            with read_connection(instance.data_path) as con:
                assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
                assert (
                    con.execute(
                        "SELECT count(*) FROM memory_session_terminals"
                    ).fetchone()[0]
                    == 1
                )
            recovered = await session.resume(tid)
            assert recovered.completed_turn == turn
            assert recovered.attempt_id == attempt
            assert calls == (1 if path.startswith("run") else 0)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_prepared_recovery_preserves_exact_output_and_actual_fact_ids(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    assert hasattr(client_api, "DurableMemorySession"), "durable API missing"
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    sid, tid, attempt = uuid4(), uuid4(), uuid4()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        session = client_api.DurableMemorySession(client, session_id=sid)
        await session.open()
        await client.begin_turn(sid, tid, attempt_id=attempt, idempotency_key=uuid4())
        turn = ModelTurn(
            "Exact prepared output", (DurableObservation("The selected port is 8123."),)
        )
        await client.prepare_turn(
            sid, tid, turn, attempt_id=attempt, idempotency_key=uuid4()
        )
        resumed = await client_api.DurableMemorySession(client, session_id=sid).resume(
            tid
        )
        assert resumed.completed_turn == turn
        assert resumed.state == "committed"
        again = await session.resume(tid)
        assert again.persistence is not None
        assert resumed.persistence is not None
        assert again.persistence.result == resumed.persistence.result
        assert (
            again.persistence.mutation_receipt == resumed.persistence.mutation_receipt
        )


@pytest.mark.anyio
async def test_started_resume_and_begin_replay_never_invoke_callback(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    assert hasattr(client_api, "DurableMemorySession"), "durable API missing"
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    sid, tid, attempt = uuid4(), uuid4(), uuid4()
    calls = 0

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn("Empty is skipped")

    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        session = client_api.DurableMemorySession(client, session_id=sid)
        await session.open()
        await client.begin_turn(sid, tid, attempt_id=attempt, idempotency_key=uuid4())
        assert (await session.resume(tid)).state == "interrupted"
        assert calls == 0
        completed = await session.run_turn(
            "A port", callback, turn_id=uuid4(), attempt_id=uuid4()
        )
        assert completed.state == "skipped"
        assert calls == 1
        assert completed.persistence is not None
        assert completed.persistence.result is None


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["turn-begin", "turn-prepare", "turn-commit"])
async def test_lost_acknowledgement_never_regenerates(
    operation: str, tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    calls = 0

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn(
            "Exact output", (DurableObservation("The selected port is 8123."),)
        )

    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            session = DurableMemorySession(client, session_id=uuid4())
            await session.open()
            tid, attempt = uuid4(), uuid4()
            transport.lose = operation
            with pytest.raises(DurableSessionFailure) as failure:
                await session.run_turn(
                    "Which port?", callback, turn_id=tid, attempt_id=attempt
                )
            assert failure.value.state == "pending"
            progress = session.persistence_progress(tid)
            assert progress is not None and progress.phase == "pending"
            recovered = await DurableMemorySession(
                client, session_id=session._session_id
            ).resume(tid)
            assert calls == (0 if operation == "turn-begin" else 1)
            assert recovered.state == (
                "interrupted" if operation == "turn-begin" else "committed"
            )
            if operation != "turn-begin":
                assert recovered.completed_turn is not None
                assert recovered.completed_turn.response == "Exact output"
                assert recovered.persistence is not None
                assert recovered.persistence.result is not None
                fact_ids = recovered.persistence.result["fact_ids"]
                assert isinstance(fact_ids, tuple) and len(fact_ids) == 1


@pytest.mark.anyio
async def test_contenders_replay_and_cancellation_cannot_reauthorise_generation(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    calls = 0
    entered, finish = asyncio.Event(), asyncio.Event()

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return ModelTurn("Completed")

    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = bound_client(http, instance.config.instance_id)
        sid, tid, attempt = uuid4(), uuid4(), uuid4()
        first, second = (
            DurableMemorySession(client, session_id=sid),
            DurableMemorySession(client, session_id=sid),
        )
        await first.open()
        task = asyncio.create_task(
            first.run_turn("A port", callback, turn_id=tid, attempt_id=attempt)
        )
        await asyncio.wait_for(entered.wait(), 5)
        replay = await second.run_turn(
            "A port", callback, turn_id=tid, attempt_id=attempt
        )
        assert replay.state == "interrupted"
        assert calls == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        progress = first.persistence_progress(tid)
        assert progress is not None and progress.phase == "pending"
        assert (await second.resume(tid)).state == "interrupted"
        await second.abandon(tid, reason="Interrupted callback")
        finish.set()
        replaced = await second.run_turn(
            "New attempt",
            callback,
            turn_id=uuid4(),
            attempt_id=uuid4(),
            replaces_turn_id=tid,
        )
        assert replaced.state == "skipped"
        assert replaced.snapshot.replaces_turn_id == tid
        assert calls == 2


@pytest.mark.anyio
async def test_arrival_explicit_ack_equality_rollback_and_failed_read(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            session = DurableMemorySession(client, session_id=uuid4())
            await session.open()
            await client.remember(
                (DurableObservation("The port is 8123."),), idempotency_key=uuid4()
            )
            arrival = await session.arrive("port")
            assert (await session.status()).acknowledged_watermark == 0
            assert arrival.visit.snapshot.visit_id is not None
            await session.acknowledge_visit(arrival.visit.snapshot.visit_id)
            again = await session.arrive("port")
            assert again.briefing.include_boundary
            assert again.briefing.changes
            assert not again.clock_rollback
            assert again.briefing.coverage == "selected-memory-only"
            transport.lose = "recall"
            partial = await session.arrive("port")
            assert partial.briefing.failures
            assert (await session.status()).acknowledged_watermark == 1
            instance.clock.now -= timedelta(hours=1)
            backwards = await session.arrive("port")
            assert backwards.clock_rollback
            assert backwards.briefing.since is None
            assert "prior_checkpoint_in_future" in backwards.briefing.warnings
            assert "clock_rollback_changes_uncertain" in backwards.briefing.warnings
            assert (await session.status()).acknowledged_watermark == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,bad",
    [
        ("session_id", str(uuid4())),
        ("instance_id", str(uuid4())),
        ("principal_id", str(uuid4())),
        ("turn_id", str(uuid4())),
        ("attempt_id", "NOT-AN-ID"),
        ("classification", "public"),
        ("state", "started"),
        ("custody_receipt", {"mutation_id": str(uuid4()), "command_digest": "a" * 64}),
        ("custody_audit_receipt", {}),
        ("custody_result", {}),
        ("custody_idempotency_key", str(uuid4())),
        ("response", "x" * 32769),
        ("response", "changed"),
        ("turn_count", True),
        ("prepared_bytes", 1),
        ("acknowledged_at", "2026-01-01T00:00:00.000000Z"),
        ("visit_id", str(uuid4())),
        ("extra", "PRIVATE"),
        ("state", "abandoned"),
    ],
    ids=[
        "foreign-session",
        "foreign-instance",
        "foreign-principal",
        "foreign-turn",
        "malformed-attempt",
        "foreign-classification",
        "unexpected-started-state",
        "unexpected-custody-receipt",
        "empty-custody-audit",
        "empty-custody-result",
        "unexpected-custody-key",
        "oversized-response",
        "changed-response",
        "boolean-turn-count",
        "undersized-preparation",
        "unexpected-acknowledgement",
        "unexpected-visit",
        "extra-field",
        "unexpected-abandoned-state",
    ],
)
async def test_foreign_or_malformed_preparation_is_never_recovered(
    field: str, bad: object, tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            sid, tid, attempt = uuid4(), uuid4(), uuid4()
            await client.open_session(sid, idempotency_key=uuid4())
            await client.begin_turn(
                sid, tid, attempt_id=attempt, idempotency_key=uuid4()
            )
            await client.prepare_turn(
                sid,
                tid,
                ModelTurn("Exact output", (DurableObservation("A fact."),)),
                attempt_id=attempt,
                idempotency_key=uuid4(),
            )
            transport.damage = lambda data: data.update({field: copy.deepcopy(bad)})
            with pytest.raises(MemoryOperationFailure) as failure:
                await client.read_session(sid, turn_id=tid)
            assert failure.value.failure.code == "invalid_response"
            assert "PRIVATE" not in str(failure.value)


@pytest.mark.anyio
async def test_changed_committed_observations_cannot_borrow_a_custody_receipt(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            sid, tid, attempt = uuid4(), uuid4(), uuid4()
            await client.open_session(sid, idempotency_key=uuid4())
            await client.begin_turn(
                sid, tid, attempt_id=attempt, idempotency_key=uuid4()
            )
            await client.prepare_turn(
                sid,
                tid,
                ModelTurn("Exact output", (DurableObservation("The port is 8123."),)),
                attempt_id=attempt,
                idempotency_key=uuid4(),
            )
            await client.commit_turn(sid, tid, idempotency_key=uuid4())
            transport.damage = lambda data: data["observations"][0].update(
                {"body": "The port is 9000."}
            )
            with pytest.raises(MemoryOperationFailure):
                await client.read_session(sid, turn_id=tid)


@pytest.mark.anyio
async def test_incomplete_nested_observation_is_not_a_complete_snapshot(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            sid, tid, attempt = uuid4(), uuid4(), uuid4()
            await client.open_session(sid, idempotency_key=uuid4())
            await client.begin_turn(
                sid, tid, attempt_id=attempt, idempotency_key=uuid4()
            )
            await client.prepare_turn(
                sid,
                tid,
                ModelTurn("Exact output", (DurableObservation("A fact."),)),
                attempt_id=attempt,
                idempotency_key=uuid4(),
            )
            transport.damage = lambda data: data["observations"][0].pop("valid_from")
            with pytest.raises(MemoryOperationFailure):
                await client.read_session(sid, turn_id=tid)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["declared", "streamed", "compressed", "duplicate"])
async def test_snapshot_wire_is_bounded_before_decoding(
    mode: str, tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as real:

        async def fake(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/diagnose"):
                response = await real.post(
                    "/memory/v1/diagnose",
                    json=json.loads(request.content),
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert isinstance(response, httpx.Response)
                return response
            if mode == "declared":
                return httpx.Response(
                    200, headers={"Content-Length": "458753"}, content=b"{}"
                )
            if mode == "compressed":
                # Non-identity is refused even when an unknown codec is not decoded by HTTPX.
                return httpx.Response(
                    200, headers={"Content-Encoding": "unknown"}, content=b"{}"
                )
            if mode == "duplicate":
                return httpx.Response(200, content=b'{"state":"open","state":"open"}')
            return httpx.Response(200, stream=ResponseChunks())

        async with httpx.AsyncClient(
            base_url=real.base_url, transport=httpx.MockTransport(fake)
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            with pytest.raises(MemoryOperationFailure) as failure:
                await client.read_session(uuid4())
            assert failure.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_maximum_output_and_eight_observations_survive_bounded_recovery(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        client = bound_client(http, instance.config.instance_id)
        sid, tid, attempt = uuid4(), uuid4(), uuid4()
        await client.open_session(sid, idempotency_key=uuid4())
        await client.begin_turn(sid, tid, attempt_id=attempt, idempotency_key=uuid4())
        observation = DurableObservation(
            ("The measured port remains 8123. " * 140)[:4096]
        )
        output = ModelTurn("𐀀" * 8192, (observation,) * 8)
        await client.prepare_turn(
            sid, tid, output, attempt_id=attempt, idempotency_key=uuid4()
        )
        recovered = await DurableMemorySession(client, session_id=sid).resume(tid)
        assert recovered.completed_turn == output
        assert recovered.persistence is not None
        assert recovered.persistence.result is not None
        fact_ids = recovered.persistence.result["fact_ids"]
        assert isinstance(fact_ids, tuple) and len(fact_ids) == 8


@pytest.mark.anyio
async def test_invalid_abandonment_reason_becomes_safe_client_failure(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        transport = Intercept(upstream._transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            client = bound_client(http, instance.config.instance_id)
            sid, tid, attempt = uuid4(), uuid4(), uuid4()
            await client.open_session(sid, idempotency_key=uuid4())
            await client.begin_turn(
                sid, tid, attempt_id=attempt, idempotency_key=uuid4()
            )
            await client.abandon_turn(
                sid, tid, reason="Interrupted", idempotency_key=uuid4()
            )
            transport.damage = lambda data: data.update({"abandonment_reason": ""})
            with pytest.raises(MemoryOperationFailure) as failure:
                await client.read_session(sid, turn_id=tid)
            assert failure.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_fixed_client_classification_refuses_foreign_preparation_before_writing(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        internal = bound_client(http, instance.config.instance_id)
        restricted = MemoryClient(
            http,
            scope=internal.scope,
            classification=Classification.RESTRICTED,
            expected_instance_id=instance.config.instance_id,
        )
        sid, tid, attempt = uuid4(), uuid4(), uuid4()
        await restricted.open_session(sid, idempotency_key=uuid4())
        await restricted.begin_turn(
            sid, tid, attempt_id=attempt, idempotency_key=uuid4()
        )
        with pytest.raises(MemoryOperationFailure):
            await internal.prepare_turn(
                sid,
                tid,
                ModelTurn("Wrong classification"),
                attempt_id=attempt,
                idempotency_key=uuid4(),
            )
        assert (await restricted.read_session(sid, turn_id=tid)).state == "started"


@pytest.mark.anyio
async def test_invalid_callback_output_is_not_exposed_as_a_completed_model_turn(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()

    async def invalid_callback(value: TurnInput) -> dict[str, str]:
        return {"response": "Not a ModelTurn", "scope": "not-authority"}

    async with memory_support.serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        session = DurableMemorySession(
            bound_client(http, instance.config.instance_id), session_id=uuid4()
        )
        await session.open()
        tid = uuid4()
        with pytest.raises(DurableSessionFailure) as failure:
            await session.run_turn(
                "A port",
                cast(
                    ModelCallback, invalid_callback
                ),  # Deliberately violate callback contract.
                turn_id=tid,
                attempt_id=uuid4(),
            )
        assert failure.value.completed_turn is None
        assert (await session.resume(tid)).state == "interrupted"


@pytest.mark.anyio
async def test_wrong_instance_handshake_sends_no_preparation_content(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as upstream:
        sent: list[str] = []

        async def intercept(request: httpx.Request) -> None:
            sent.append(request.url.path)

        async with httpx.AsyncClient(
            transport=upstream._transport,
            base_url=upstream.base_url,
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"request": [intercept]},
        ) as http:
            client = MemoryClient(
                http,
                scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
                classification=Classification.INTERNAL,
                expected_instance_id=uuid4(),
            )
            with pytest.raises(MemoryOperationFailure) as refused:
                await client.prepare_turn(
                    uuid4(),
                    uuid4(),
                    ModelTurn("PRIVATE OUTPUT"),
                    attempt_id=uuid4(),
                    idempotency_key=uuid4(),
                )
            assert refused.value.failure.code == "instance_mismatch"
            assert sent == ["/memory/v1/diagnose"]
