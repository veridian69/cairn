"""Synthetic loopback server only: crash after real effects, before acknowledgement."""

import json
import os
import socket
import sys
from datetime import datetime
from typing import Any
from unittest.mock import patch

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.transactions import Committed, Replayed
from cairn.client.durable_session import DurableMemorySession
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig


class CrashAfterOperation:
    def __init__(self, app: ASGIApp, operation: str) -> None:
        self.app, self.operation = app, operation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def output(message: Message) -> None:
            if (
                self.operation
                and scope.get("path") == "/memory/v1/" + self.operation
                and message["type"] == "http.response.start"
                and message["status"] == 200
            ):
                os._exit(73)
            await send(message)

        await self.app(scope, receive, output)


def run() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "client":
        from importlib.metadata import distribution

        async def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("CLI must never invoke a model runner")

        patch.object(DurableMemorySession, "run_turn", forbidden).start()
        del sys.argv[1]
        entrypoint = next(
            e
            for e in distribution("drystane-cairn").entry_points
            if e.group == "console_scripts" and e.name == "cairn-memory"
        )
        entrypoint.load()()
        return
    packet = json.loads(sys.stdin.readline())
    config = CairnConfig.model_validate_json(json.dumps(packet["config"]))
    now = datetime.fromisoformat(packet["now"])

    if packet["crash"] == "ingest":
        original = CairnAuthority.ingest

        def ingest(self: CairnAuthority, *args: Any, **kwargs: Any) -> Any:
            result = original(self, *args, **kwargs)
            if kwargs.get("commit_guard") is not None and isinstance(
                result, (Committed, Replayed)
            ):
                os._exit(73)
            return result

        patch.object(CairnAuthority, "ingest", ingest).start()
    app = CrashAfterOperation(
        build_application(config, clock=lambda: now), packet["crash"]
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    server.run(sockets=[socket.socket(fileno=packet["fd"])])


if __name__ == "__main__":
    run()
