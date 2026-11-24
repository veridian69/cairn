"""Browse against a disposable local Cairn: real listing, detail and promotion."""

import errno
import fcntl
import os
import pty
import select
import socket
import struct
import subprocess
import sys
import termios
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_evidence_window_client import deliver_evidence
from test_memory_cli import profile
from test_memory_cli_process import server

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import read_connection
from cairn.client import MemoryClient
from cairn.client.browse import BrowseController
from cairn.client.browse_terminal import MemoryBrowseBackend
from cairn.client.profiles import MemoryProfile
from cairn.client.types import ConnectionStatus

SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
COMMAND = Path(sys.executable).with_name("cairn-memory")


@asynccontextmanager
async def browser(
    http: httpx.AsyncClient,
    token: str,
    instance: Any,
    session_id: UUID | None = None,
) -> AsyncIterator[tuple[BrowseController, list[str]]]:
    dispatched: set[str] = set()
    paths: list[str] = []

    async def hook(request: httpx.Request) -> None:
        dispatched.add(request.url.path)
        paths.append(request.url.path)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=http._transport,
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"request": [hook]},
    ) as authorised:
        client = MemoryClient(
            authorised,
            scope=SCOPE,
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )
        diagnostic = await client.diagnose(
            expected_instance_id=instance.config.instance_id
        )
        assert diagnostic.status is ConnectionStatus.READY
        assert diagnostic.permissions is not None
        permissions = diagnostic.permissions
        profile = MemoryProfile(
            "http://127.0.0.1",
            instance.config.instance_id,
            SCOPE,
            Classification.INTERNAL,
            Path("unused-token"),
            session_id,
        )
        yield (
            BrowseController(
                MemoryBrowseBackend(client, dispatched, session_id=session_id),
                scope=SCOPE,
                may_promote=permissions.ingest and permissions.promote,
                connection=diagnostic,
                profile=profile,
            ),
            paths,
        )


def mode(c: BrowseController) -> str:
    """Read state freshly; awaited keys change it (defeats mypy narrowing)."""
    return str(c.mode)


def status(c: BrowseController) -> str:
    assert c.outcome is not None
    return str(c.outcome.status)


def fact_row(instance: Any, fact_id: str) -> tuple[Any, ...]:
    with read_connection(instance.data_path) as con:
        return tuple(
            con.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        )


def fact_count(instance: Any) -> int:
    with read_connection(instance.data_path) as con:
        return int(con.execute("SELECT count(*) FROM facts").fetchone()[0])


@pytest.mark.anyio
async def test_promoter_promotes_candidate_and_agent_is_not_offered(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    operator, operator_token = instance.add_actor(segments=[], label="synthetic-human")
    _, agent_token = instance.add_actor(
        operations=["ingest", "retrieve"], segments=[], label="synthetic-agent"
    )
    async with memory_support.serve(instance) as http:
        agent = memory_support.Api(http, agent_token)
        plain = await agent.remember("A fact saved without source text")
        assert plain["outcome"] == "committed", plain
        instance.clock.now = instance.clock.now.replace(minute=5)
        source = await agent.remember(
            "The build uses port 8123.\x1b[2J",
            evidence_payload="Port 8123 per build log",
        )
        source_id = source["result"]["fact_ids"][0]
        deliver_evidence(instance)
        source_before = fact_row(instance, source_id)
        count_before = fact_count(instance)

        async with browser(http, agent_token, instance) as (c, paths):
            await c.start()
            assert [r["fact_id"] for r in c.rows][:2] == [
                source_id,
                plain["result"]["fact_ids"][0],
            ]
            assert "p promote" not in "\n".join(line.text for line in c.render())
            await c.handle("p")
            assert c.message == "promotion not permitted for this profile"
            await c.handle("enter")
            await c.handle("p")
            assert c.message == "promotion not permitted for this profile"
            assert not any("propos" in path for path in paths)

        async with browser(http, operator_token, instance) as (c, paths):
            await c.start()
            assert c.view[0]["fact_id"] == source_id
            assert c.view[0]["trust"] == "candidate"
            evidence = c.view[0]["source_evidence_id"]
            assert evidence is not None
            assert "\\x1b[2J" in c.render()[1].text
            await c.handle("j")
            await c.handle("p")
            assert "no source evidence" in c.message and mode(c) == "list"
            await c.handle("k")
            await c.handle("enter")
            assert mode(c) == "detail" and c.history is not None
            await c.handle("e")
            assert mode(c) == "evidence" and c.window is not None
            assert c.window["text"] == "Port 8123 per build log"
            await c.handle("esc")
            await c.handle("p")
            for key in "Confirmed in build log":
                await c.handle(key)
            await c.handle("enter")
            assert mode(c) == "confirm"
            instance.clock.now = instance.clock.now.replace(minute=10)
            await c.handle("y")
            outcome = c.outcome
            assert outcome is not None and outcome.status == "promoted", outcome
            assert outcome.receipt_outcome == "committed"
            derived = str(outcome.derived_fact_id)
            assert "/memory/v1/propose" in paths
            assert "/memory/v1/proposal-accept" in paths
            await c.handle("x")
            assert mode(c) == "list"
            top = dict(c.view[0])
            assert top["fact_id"] == derived and top["trust"] == "validated"
            assert c.view[0]["derived_from"] == source_id
            assert c.view[0]["promoted_by"] == str(operator)
            assert c.view[0]["evidence_id"] == evidence
            await c.handle("f")
            assert source_id in [r["fact_id"] for r in c.view]
            await c.handle("f")
            assert [r["fact_id"] for r in c.view] == [derived]
            await c.handle("p")
            assert "only candidate facts" in c.message

        assert fact_count(instance) == count_before + 1
        assert fact_row(instance, source_id) == source_before
        with read_connection(instance.data_path) as con:
            trust, derived_from, promoted_by, scope = con.execute(
                "SELECT trust, derived_from, promoted_by, scope_segments FROM facts WHERE fact_id = ?",
                (derived,),
            ).fetchone()
        assert (trust, derived_from, promoted_by) == (
            "validated",
            source_id,
            str(operator),
        )
        assert scope == fact_row(instance, source_id)[2]


@pytest.mark.anyio
async def test_uncertain_acceptance_is_not_reported_and_replays(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        source = await api.remember("Losable promotion", evidence_payload="Evidence")
        source_id = source["result"]["fact_ids"][0]
        upstream = http._transport
        lose = {"/memory/v1/proposal-accept"}

        class Lossy(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                response: httpx.Response = await upstream.handle_async_request(request)
                await response.aread()
                if request.url.path in lose:
                    lose.clear()
                    raise httpx.ReadError("acknowledgement lost")
                return response

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1", transport=Lossy()
        ) as lossy:
            async with browser(lossy, token, instance) as (c, _):
                await c.start()
                await c.handle("p")
                for key in "Reviewed":
                    await c.handle(key)
                await c.handle("enter")
                await c.handle("y")
                assert c.outcome is not None and c.outcome.status == "uncertain"
                assert c.outcome.operation == "proposal-accept"
                assert c.unresolved
                after_loss = fact_count(instance)
                await c.handle("R")
                assert c.outcome is not None and status(c) == "promoted"
                assert c.outcome.receipt_outcome == "replayed"
                assert not c.unresolved
                assert fact_count(instance) == after_loss
                assert c.attempt is not None
                assert c.attempt.source_fact_id == UUID(source_id)


@pytest.mark.anyio
async def test_lost_preflight_read_after_proposal_is_not_unknown(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        await api.remember("Preflight promotion", evidence_payload="Evidence")
        upstream = http._transport
        lose = {"/memory/v1/proposal-read"}

        class Lossy(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                response: httpx.Response = await upstream.handle_async_request(request)
                await response.aread()
                if request.url.path in lose:
                    lose.clear()
                    raise httpx.ReadError("read reply lost")
                return response

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1", transport=Lossy()
        ) as lossy:
            async with browser(lossy, token, instance) as (c, _):
                await c.start()
                await c.handle("p")
                for key in "Reviewed":
                    await c.handle(key)
                await c.handle("enter")
                await c.handle("y")
                assert c.outcome is not None and c.outcome.status == "failed"
                assert c.outcome.operation == "proposal-accept"
                assert c.outcome.proposal_recorded
                assert not c.unresolved
                before = fact_count(instance)
                await c.handle("R")
                assert c.outcome is not None and status(c) == "promoted"
                assert fact_count(instance) == before + 1
                assert not c.unresolved


@pytest.mark.anyio
async def test_suggestions_and_connection_are_read_only(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    agent, token = instance.add_actor(
        operations=["ingest", "retrieve"], segments=[], label="synthetic-agent"
    )
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        first = await api.remember("The build uses port 8123.")
        instance.clock.now = instance.clock.now.replace(minute=5)
        await api.remember("The build uses port 8123.")
        count = fact_count(instance)
        async with browser(http, token, instance) as (c, paths):
            await c.start()
            del paths[:]
            await c.handle("j")
            assert c.view[c.selected]["fact_id"] == first["result"]["fact_ids"][0]
            await c.handle("s")
            assert mode(c) == "suggestions", c.message
            assert c.suggestions_error is None and c.suggestions is not None
            text = "\n".join(line.text for line in c.render())
            assert "Suggestions for" in text and "suggest failed" not in text
            assert "1. exact_duplicate (basis: exact_body)" in text
            await c.handle("esc")
            await c.handle("c")
            assert mode(c) == "connection" and c.connection_error is None
            c.resize(60, 100)
            text = "\n".join(line.text for line in c.render())
            assert f"principal_id: {agent}" in text
            assert f"instance_id: {instance.config.instance_id}" in text
            assert "retrieve: yes" in text and "promote: no" in text
            assert "p promotion needs ingest and promote: unavailable" in text
            assert token not in text
            assert set(paths) == {"/memory/v1/diagnose", "/memory/v1/suggest"}
        assert fact_count(instance) == count


@asynccontextmanager
async def reader(
    http: httpx.AsyncClient, token: str, instance: Any
) -> AsyncIterator[MemoryClient]:
    """A separate client for reading the store back after a browser write."""
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=http._transport,
        headers={"Authorization": f"Bearer {token}"},
    ) as authorised:
        yield MemoryClient(
            authorised,
            scope=SCOPE,
            classification=Classification.INTERNAL,
            expected_instance_id=instance.config.instance_id,
        )


def lossy(http: httpx.AsyncClient, path: str) -> httpx.AsyncClient:
    """Deliver the first request to ``path`` but lose its acknowledgement."""
    upstream = http._transport
    lose = {path}

    class Lossy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response: httpx.Response = await upstream.handle_async_request(request)
            await response.aread()
            if request.url.path in lose:
                lose.clear()
                raise httpx.ReadError("acknowledgement lost")
            return response

    return httpx.AsyncClient(base_url="http://127.0.0.1", transport=Lossy())


async def type_reason(c: BrowseController, key: str, reason: str) -> None:
    await c.handle(key)
    for character in reason:
        await c.handle(character)
    await c.handle("enter")
    assert mode(c) == "confirm", c.message


@pytest.mark.anyio
async def test_real_correction_supersedes_and_an_exact_retry_replays(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        old = (await api.remember("The build uses port 8123."))["result"]
        instance.clock.now = instance.clock.now.replace(minute=5)
        new = (await api.remember("The build uses port 8124."))["result"]
        old_id, new_id = old["fact_ids"][0], new["fact_ids"][0]
        async with lossy(http, "/memory/v1/correct") as lost:
            async with browser(lost, token, instance) as (c, paths):
                await c.start()
                assert [r["fact_id"] for r in c.view] == [new_id, old_id]
                await c.handle("m")
                await c.handle("j")
                await type_reason(c, "X", "Port changed in the build")
                assert "/memory/v1/correct" not in paths
                instance.clock.now = instance.clock.now.replace(minute=10)
                await c.handle("y")
                assert c.write_outcome is not None
                assert c.write_outcome.status == "uncertain", c.write_outcome
                assert c.unresolved_writes
                first = c.write
                await c.handle("R")
                assert c.write is first
                outcome = c.write_outcome
                assert outcome is not None and outcome.status == "recorded", outcome
                assert outcome.receipt_outcome == "replayed"
                assert not c.unresolved_writes
                await c.handle("esc")
                assert mode(c) == "list" and c.message == "corrected; listing refreshed"
                assert [r["fact_id"] for r in c.view] == [new_id]
        async with reader(http, token, instance) as client:
            history = (await client.history(UUID(old_id))).data
        corrections: list[Any] = list(history["corrections"])  # type: ignore[arg-type]
        assert len(corrections) == 1
        assert corrections[0]["fact_id"] == old_id
        assert corrections[0]["superseded_by"] == new_id
        assert corrections[0]["reason"] == "Port changed in the build"


@pytest.mark.anyio
async def test_real_disagreement_is_recorded_between_the_two_facts(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        left = (await api.remember("Deploys happen on Tuesdays."))["result"]
        instance.clock.now = instance.clock.now.replace(minute=5)
        right = (await api.remember("Deploys happen on Thursdays."))["result"]
        left_id, right_id = left["fact_ids"][0], right["fact_ids"][0]
        count = fact_count(instance)
        async with browser(http, token, instance) as (c, paths):
            await c.start()
            await c.handle("j")
            await c.handle("m")
            await c.handle("k")
            await type_reason(c, "d", "The two days conflict")
            assert "/memory/v1/disagree" not in paths
            await c.handle("y")
            outcome = c.write_outcome
            assert outcome is not None and outcome.status == "recorded", outcome
            assert outcome.receipt_outcome == "committed"
            relationship = outcome.relationship_id
            assert relationship is not None
            await c.handle("esc")
            assert c.message == "disagreement recorded; listing refreshed"
            assert all(r["has_disagreement"] is True for r in c.view)
        assert fact_count(instance) == count
        async with reader(http, token, instance) as client:
            history = (await client.history(UUID(right_id))).data
        links: list[Any] = list(history["disagreements"])  # type: ignore[arg-type]
        [link] = links
        assert link["relationship_id"] == relationship
        assert (link["left_fact_id"], link["right_fact_id"]) == (left_id, right_id)
        assert link["reason"] == "The two days conflict"


@pytest.mark.anyio
async def test_real_proposal_rejection_is_recorded_and_leaves_the_source(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        source_id = (await api.remember("A claim proposed for promotion"))["result"][
            "fact_ids"
        ][0]
        proposal_id = UUID("cccccccc-0000-4000-8000-000000000001")
        async with reader(http, token, instance) as client:
            await client.propose(
                proposal_id,
                source_fact_id=UUID(source_id),
                target_scope=SCOPE,
                reason="Worth keeping",
                idempotency_key=UUID("cccccccc-0000-4000-8000-000000000002"),
            )
        source_before = fact_row(instance, source_id)
        async with browser(http, token, instance) as (c, paths):
            await c.start()
            await c.handle("P")
            assert [p.proposal_id for p in c.pending] == [proposal_id]
            await c.handle("enter")
            await type_reason(c, "x", "Not durable enough")
            assert "/memory/v1/proposal-reject" not in paths
            await c.handle("y")
            outcome = c.write_outcome
            assert outcome is not None and outcome.status == "recorded", outcome
            assert outcome.receipt_outcome == "committed"
            await c.handle("esc")
            assert mode(c) == "proposals" and not c.pending
        assert fact_row(instance, source_id) == source_before
        async with reader(http, token, instance) as client:
            current = await client.proposal_read(proposal_id)
        assert current.state == "rejected" and current.decision is not None
        assert current.decision.reason == "Not durable enough"
        assert current.decision.promoted_fact_id is None


async def keys(c: BrowseController, *names: str) -> None:
    for name in names:
        await c.handle(name)


@pytest.mark.anyio
async def test_real_arrive_then_an_explicit_acknowledge_visit(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        api = memory_support.Api(http, token)
        await api.remember("The build uses port 8123.")
        async with browser(http, token, instance, uuid4()) as (c, paths):
            await c.start()
            await keys(c, "S", "a", *"port", "enter")
            assert mode(c) == "confirm", c.message
            # Only status was read on opening the view; nothing was opened.
            assert (
                set(paths) & {"/memory/v1/session-open", "/memory/v1/visit-issue"}
                == set()
            )
            assert "/memory/v1/session-read" in paths
            await c.handle("y")
            outcome = c.write_outcome
            assert outcome is not None and outcome.status == "recorded", outcome
            assert outcome.visit_id is not None and outcome.arrival is not None
            text = "\n".join(line.text for line in c._screen_lines())
            assert "Briefing. The visit is NOT acknowledged." in text
            assert "The build uses port 8123." in text
            assert "/memory/v1/visit-acknowledge" not in paths
            await c.handle("esc")
            status = c.session_status
            # status (no turn_id) does not report the visit: only the
            # browser's own arrival makes it known for v.
            assert status is not None and status.visit_id is None
            assert status.acknowledged_at is None
            assert status.acknowledged_watermark == 0
            assert str(c.issued_visit) == outcome.visit_id
            await c.handle("v")
            assert mode(c) == "confirm" and "/memory/v1/visit-acknowledge" not in paths
            await c.handle("y")
            acknowledged = c.write_outcome
            assert acknowledged is not None and acknowledged.status == "recorded"
            assert paths.count("/memory/v1/visit-acknowledge") == 1
            snapshot = acknowledged.snapshot
            assert snapshot is not None and snapshot.acknowledged_at is not None
            assert snapshot.acknowledged_watermark == 1
            await c.handle("esc")
            # status reports the acknowledgement, though never the visit ID.
            status = c.session_status
            assert status is not None and status.acknowledged_watermark == 1
            assert status.acknowledged_at == snapshot.acknowledged_at
            assert c.issued_visit is None and not c.unresolved_writes


@pytest.mark.anyio
async def test_real_remember_is_saved_and_visible_through_the_client(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        async with browser(http, token, instance, uuid4()) as (c, paths):
            await c.start()
            assert c.view == []
            await keys(c, "S", "w", *"The deploy uses port 9000.", "enter", "enter")
            await keys(c, *"Measured", "enter")
            assert mode(c) == "confirm", c.message
            assert "/memory/v1/turn-begin" not in paths
            await c.handle("y")
            outcome = c.write_outcome
            assert outcome is not None and outcome.status == "saved", outcome
            [fact_id] = outcome.fact_ids
            assert "Saved: committed custody receipt." in "\n".join(
                line.text for line in c._screen_lines()
            )
            await c.handle("esc")
            assert c.message == "saved; listing refreshed and status re-read"
            assert [row["fact_id"] for row in c.view] == [fact_id]
            status = c.session_status
            assert status is not None and status.state == "committed"
        async with reader(http, token, instance) as client:
            history = (await client.history(UUID(fact_id))).data
        facts: list[Any] = list(history["facts"])  # type: ignore[arg-type]
        assert [f["body"] for f in facts if f["fact_id"] == fact_id] == [
            "The deploy uses port 9000."
        ]
        assert fact_row(instance, fact_id)[0] == fact_id


@pytest.mark.anyio
async def test_real_abandon_of_an_unknown_checkpoint(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with memory_support.serve(instance) as http:
        async with lossy(http, "/memory/v1/turn-begin") as lost:
            async with browser(lost, token, instance, uuid4()) as (c, paths):
                await c.start()
                await keys(c, "S", "w", *"Lost output", "enter", "enter", "enter")
                await c.handle("y")
                outcome = c.write_outcome
                assert outcome is not None and outcome.status == "uncertain", outcome
                assert outcome.recovery == (
                    "resubmit_identical_checkpoint_same_identities"
                )
                attempt = c.write
                assert attempt is not None and c.unresolved_writes
                assert "/memory/v1/turn-prepare" not in paths
                await keys(c, "esc", "b")
                assert mode(c) == "input"
                await keys(c, *"Output was lost", "enter")
                assert mode(c) == "confirm"
                assert "/memory/v1/turn-abandon" not in paths
                await c.handle("y")
                abandoned = c.write_outcome
                assert abandoned is not None and abandoned.status == "recorded"
                assert abandoned.snapshot is not None
                assert abandoned.snapshot.state == "abandoned"
                assert abandoned.snapshot.turn_id == attempt.key
                assert not c.unresolved_writes
                await c.handle("esc")
                status = c.session_status
                assert status is not None and status.state == "abandoned"


def _read_until(fd: int, needle: bytes, seen: bytearray, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while needle not in seen:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for {needle!r}"
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            seen.extend(chunk)
    assert needle in seen, needle


def test_pty_smoke_lists_opens_and_quits(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor(operations=["ingest", "retrieve"], segments=[])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        path = profile(tmp_path, instance, token, endpoint)
        with server(instance, listener):
            with httpx.Client(base_url=endpoint, trust_env=False) as http:
                saved = http.post(
                    "/memory/v1/remember",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": "33333333-3333-4333-8333-333333333333",
                    },
                    json={
                        "scope": memory_support.SCOPE,
                        "classification": "internal",
                        "facts": [{"body": "Smoke fact visible in the terminal"}],
                    },
                )
                assert saved.status_code == 200, saved.text
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            process = subprocess.Popen(
                [str(COMMAND), "--profile", str(path), "browse"],
                stdin=slave,
                stdout=slave,
                stderr=subprocess.PIPE,
                env={**os.environ, "TERM": "xterm"},
                close_fds=True,
            )
            os.close(slave)
            seen = bytearray()
            try:
                _read_until(master, b"Smoke fact visible", seen)
                os.write(master, b"p")
                _read_until(master, b"promotion not permitted", seen)
                os.write(master, b"\r")
                _read_until(master, b"fact_id:", seen)
                os.write(master, b"\x1b")
                time.sleep(0.2)
                os.write(master, b"q")
                assert process.wait(timeout=20) == 0
                assert process.stderr is not None
                assert process.stderr.read() == b""
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()
                os.close(master)
