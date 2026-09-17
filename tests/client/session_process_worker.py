"""Subprocess-only synthetic test driver; never reads ambient Cairn config."""

import asyncio
import json
import os
import socket
import sys
from datetime import datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import uvicorn

from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.transactions import Committed, Replayed
from cairn.client import (
    DurableMemorySession,
    DurableObservation,
    DurableSessionFailure,
    MemoryClient,
    ModelTurn,
    TurnInput,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig


class StopBeforeCommit(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turn-commit"):
            raise httpx.ConnectError("synthetic interruption before commit")
        return await super().handle_async_request(request)


async def client(packet: dict[str, Any], mode: str) -> dict[str, object]:
    transport = StopBeforeCommit() if mode == "prepare" else httpx.AsyncHTTPTransport()
    calls = 0

    async def callback(value: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn(
            "Exact output across processes.",
            (DurableObservation("The durable port is 8123."),),
        )

    async with httpx.AsyncClient(
        base_url=packet["endpoint"],
        transport=transport,
        headers={"Authorization": "Bearer " + packet["token"]},
        timeout=10,
    ) as http:
        memory = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
            expected_instance_id=UUID(packet["instance_id"]),
        )
        session = DurableMemorySession(memory, session_id=UUID(packet["session_id"]))
        if mode == "prepare":
            await session.open()
            try:
                await session.run_turn(
                    "Which port?",
                    callback,
                    turn_id=UUID(packet["turn_id"]),
                    attempt_id=UUID(packet["attempt_id"]),
                )
            except DurableSessionFailure:
                state = await session.status(UUID(packet["turn_id"]))
                return {
                    "state": state.state,
                    "response": state.response,
                    "callbacks": calls,
                }
            raise AssertionError("commit should have been interrupted")
        try:
            result = await session.resume(UUID(packet["turn_id"]))
        except DurableSessionFailure as error:
            return {
                "state": error.state,
                "code": error.failure.code,
                "callbacks": calls,
            }
        assert result.persistence is not None and result.persistence.result is not None
        assert result.completed_turn is not None
        assert result.persistence.mutation_receipt is not None
        fact_ids = result.persistence.result["fact_ids"]
        assert isinstance(fact_ids, tuple)
        return {
            "state": result.state,
            "callbacks": calls,
            "response": result.completed_turn.response,
            "fact_ids": list(fact_ids),
            "mutation_id": result.persistence.mutation_receipt["mutation_id"],
        }


def run() -> None:
    packet = json.loads(sys.stdin.readline())
    if sys.argv[1] == "server":
        if packet["crash_after_ingest"]:
            original = CairnAuthority.ingest

            def ingest(self: CairnAuthority, *args: Any, **kwargs: Any) -> Any:
                outcome = original(self, *args, **kwargs)
                if kwargs.get("commit_guard") is not None and isinstance(
                    outcome, (Committed, Replayed)
                ):
                    os._exit(73)
                return outcome

            # This crash hook intentionally lasts for this server process only.
            patch.object(CairnAuthority, "ingest", ingest).start()
        config = CairnConfig.model_validate_json(json.dumps(packet["config"]))
        now = datetime.fromisoformat(packet["now"])
        app = build_application(config, clock=lambda: now)
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False)
        )
        server.run(sockets=[socket.socket(fileno=packet["fd"])])
    else:
        print(json.dumps(asyncio.run(client(packet, sys.argv[1]))))


if __name__ == "__main__":
    run()
