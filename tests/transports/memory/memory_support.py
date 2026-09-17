"""Disposable local catalogue and real ASGI driver for memory acceptance."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import AtticConfig, CairnConfig, HttpConfig, PathConfig

REALM = "acme"
REPO = {"kind": "repository", "identifier": "cairn"}
SCOPE: dict[str, object] = {"realm": REALM, "segments": [REPO]}
NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Instance:
    def __init__(self, root: Path, *, attic: bool = True) -> None:
        data, credentials = root / "data", root / "credentials"
        data.mkdir(parents=True)
        credentials.mkdir()
        self.clock = Clock()
        self.config = CairnConfig(
            schema_version="cairn.config/v1",
            instance_id=uuid4(),
            mode="test",
            http=HttpConfig(host="127.0.0.1", port=8000),
            paths=PathConfig(data=data, credentials=credentials),
            attic=AtticConfig(enabled=attic),
        )
        migrate_catalogue(self.config, self.clock)
        with _open_write_connection(data, create=False) as con:
            con.execute(
                "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
                (REALM, canonical_timestamp(self.clock())),
            )
            con.execute(
                "INSERT INTO audit_heads (chain_kind, chain_identity, last_sequence, last_hash) VALUES ('realm', ?, 0, ?)",
                (REALM, bytes(32)),
            )
            con.commit()

    @property
    def data_path(self) -> Path:
        return self.config.paths.data

    def add_actor(
        self,
        *,
        operations: list[str] | None = None,
        segments: list[dict[str, str]] | None = None,
        read_clearance: str = "restricted",
        label: str | None = None,
    ) -> tuple[UUID, str]:
        principal, credential, grant = uuid4(), uuid4(), uuid4()
        minted = mint_token(credential, lambda count: bytes(range(count)))
        ts = canonical_timestamp(self.clock())
        with _open_write_connection(self.data_path, create=False) as con:
            con.execute(
                "INSERT INTO principals (principal_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
                (
                    str(principal),
                    "workload",
                    "actor-" + str(principal) if label is None else label,
                    ts,
                ),
            )
            con.execute(
                "INSERT INTO credentials (credential_id, principal_id, verifier, created_at, expires_at) VALUES (?, ?, ?, ?, NULL)",
                (str(credential), str(principal), minted.verifier, ts),
            )
            con.execute(
                "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, operations, read_clearance, write_classifications, delegable_operations, issued_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    str(grant),
                    str(principal),
                    REALM,
                    json.dumps(
                        [
                            {"id": s["identifier"], "kind": s["kind"]}
                            for s in ([REPO] if segments is None else segments)
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        sorted(
                            ["ingest", "retrieve", "promote", "invalidate"]
                            if operations is None
                            else operations
                        ),
                        separators=(",", ":"),
                    ),
                    read_clearance,
                    '["internal","public","restricted"]',
                    canonical_timestamp(datetime(2040, 1, 1, tzinfo=UTC)),
                    ts,
                ),
            )
            con.commit()
        return principal, minted.text

    def application(self) -> FastAPI:
        return build_application(self.config, clock=self.clock)


@asynccontextmanager
async def serve(instance: Instance) -> AsyncIterator[AsyncClient]:
    application = instance.application()
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://127.0.0.1"
        ) as http:
            yield http


class Api:
    def __init__(self, http: AsyncClient, token: str, transport: str = "rest") -> None:
        self.http, self.token, self.transport = http, token, transport

    async def call(
        self, name: str, body: dict[str, Any], *, key: str | None = None
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.transport == "rest":
            if key is not None:
                headers["Idempotency-Key"] = key
            response = await self.http.post(
                f"/memory/v1/{name}", json=body, headers=headers
            )
            result = response.json()
        else:
            arguments = dict(body)
            if key is not None:
                arguments["idempotency_key"] = key
            response = await self.http.post(
                "/memory/v1/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            frame = response.json()
            if "error" in frame:
                result = frame["error"]["data"]
            else:
                result = json.loads(frame["result"]["content"][0]["text"])
        assert isinstance(result, dict)
        return result

    async def remember(
        self, body: str, *, key: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        return await self.call(
            "remember",
            {
                "scope": SCOPE,
                "classification": "internal",
                "facts": [{"body": body}],
                **extra,
            },
            key=str(uuid4()) if key is None else key,
        )
