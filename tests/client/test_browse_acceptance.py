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
from uuid import UUID

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
from cairn.client.types import ConnectionStatus

SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
COMMAND = Path(sys.executable).with_name("cairn-memory")


@asynccontextmanager
async def browser(
    http: httpx.AsyncClient, token: str, instance: Any
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
        yield (
            BrowseController(
                MemoryBrowseBackend(client, dispatched),
                scope=SCOPE,
                may_promote=permissions.ingest and permissions.promote,
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
