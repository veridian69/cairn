"""Task 8: the three read tool handlers and the boundary addressing
screen (I-85, I-87, P-50).

``test_a_secret_bearing_scope_prefix_leaves_no_trace_anywhere`` is I-87's
named proof and closes the amended I-74's stated residual. It is written
against ``read-audit-events`` because that is the one adapter-fronted
operation the custody screen does not cover, so the mutation paths
passing does not satisfy it.

Module-local fixtures for the reason ``test_calls.py`` records: ``tests``
has no package markers, so this package cannot carry a ``conftest.py``
without mypy refusing a second module of that name.
"""

import hashlib
import json
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cairn import __version__
from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.projection.delivery import deliver_projection_outbox
from cairn.projection.memory import MemoryIndex
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.manifest import packaged_manifest_bytes
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.rest.v1.openapi import packaged_contract_bytes
from cairn.transports.v1.wire import (
    RULE_UNKNOWN_FIELD,
)

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
GRANT_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

REALM = "acme"
REPO = {"kind": "repo", "identifier": "cairn"}
SCOPE: dict[str, object] = {"realm": REALM, "segments": [REPO]}

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# See tests/transports/v1/test_auth.py for this literal's provenance: a
# published example key, so the corpus carries no real credential.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_RULE = "cairn.secret/v1/upstream/AWSKeyDetector"

KEY_ONE = "aaaaaaaa-1111-4111-8111-111111111111"

Issue = Callable[[], str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_config(tmp_path: Path) -> CairnConfig:
    data_path = tmp_path / "data"
    credentials_path = tmp_path / "credentials"
    data_path.mkdir()
    credentials_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=credentials_path),
    )
    migrate_catalogue(config, lambda: NOW)
    return config


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def granted_credential(data_path: Path, *, operations: list[str]) -> Issue:
    """One principal with one grant over ``acme/repo:cairn`` carrying
    exactly the named operations, and credentials against it on demand."""
    timestamp = canonical_timestamp(NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (REALM, timestamp),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                _canonical([{"id": REPO["identifier"], "kind": REPO["kind"]}]),
                _canonical(sorted(operations)),
                "restricted",
                _canonical(["internal", "public", "restricted"]),
                None,
                None,
                canonical_timestamp(FUTURE),
                timestamp,
            ),
        )
        connection.commit()
    ordinals = count(1)

    def issue() -> str:
        credential_id = UUID(f"{next(ordinals):08x}-6666-4666-8666-666666666666")
        minted = mint_token(credential_id, lambda size: bytes(range(size)))
        with _open_write_connection(data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(credential_id),
                    str(PRINCIPAL_ID),
                    minted.verifier,
                    timestamp,
                    None,
                ),
            )
            connection.commit()
        return f"Bearer {minted.text}"

    return issue


@asynccontextmanager
async def running(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client


async def call(
    client: AsyncClient,
    token: str,
    tool: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    """One ``tools/call`` frame, returning the ``CallToolResult``.

    The JSON-RPC envelope is unwrapped and asserted to be a success for
    ``test_calls.py``'s reason: P-56 puts every outcome of an identified
    operation in the tool result, so an error object here would fail an
    unrelated-looking assertion further down.
    """
    response = await client.post(
        MOUNT_PATH,
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ),
        headers={**MCP_HEADERS, "Authorization": token},
    )
    assert response.status_code == 200, response.text
    frame = response.json()
    assert "error" not in frame, frame
    result: dict[str, Any] = frame["result"]
    return result


def failure_of(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert len(result["content"]) == 1
    body: dict[str, Any] = json.loads(result["content"][0]["text"])
    failure: dict[str, Any] = body["failure"]
    return failure


def structured(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False
    assert len(result["content"]) == 1
    content = result["content"][0]
    assert content["type"] == "text"
    payload: dict[str, Any] = result["structuredContent"]
    assert json.loads(content["text"]) == payload
    return payload


def ingest_arguments(key: str, body_text: str) -> dict[str, object]:
    return {
        "scope": SCOPE,
        "classification": "internal",
        "source_type": "human",
        "requested_trust": "candidate",
        "facts": [{"body": body_text, "valid_from": None, "valid_to": None}],
        "observed_at": None,
        "metadata": None,
        "evidence_payload": None,
        "idempotency_key": key,
    }


def audit_arguments(
    scope_prefix: list[dict[str, str]], **overrides: object
) -> dict[str, object]:
    return {"realm_id": REALM, "scope_prefix": scope_prefix, **overrides}


def events(data_path: Path, chain_kind: str) -> list[dict[str, Any]]:
    with sqlite3.connect(data_path / CATALOGUE_FILENAME) as connection:
        found = connection.execute(
            "SELECT canonical_event FROM audit_events "
            "WHERE chain_kind = ? ORDER BY sequence",
            (chain_kind,),
        ).fetchall()
    return [json.loads(row[0]) for row in found]


@pytest.mark.anyio
async def test_the_audit_read_returns_the_bare_body_and_appends_its_own_event(
    tmp_path: Path,
) -> None:
    """The bare I-72 read body, and the self-appending property that makes
    this the one read holding the P-29 gate."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["ingest", "audit-read"])
    token = issue()

    async with running(build_application(config)) as client:
        ingested = await call(
            client, token, "ingest", ingest_arguments(KEY_ONE, "the gate is a lock")
        )
        assert ingested["isError"] is False
        result = await call(client, token, "read-audit-events", audit_arguments([REPO]))

    payload = structured(result)
    assert set(payload) == {"events", "next_after_sequence"}
    assert [event["action_code"] for event in payload["events"]] == ["ingest"]
    assert all(event["schema"] == "cairn.audit/v1" for event in payload["events"])
    # The read's own event is on the chain, behind the page it returned.
    assert [event["action_code"] for event in events(config.paths.data, "realm")] == [
        "ingest",
        "audit-read",
    ]


@pytest.mark.anyio
async def test_a_fact_round_trips_through_the_retrieve_tool(tmp_path: Path) -> None:
    """P-44 through the second transport: the tool reaches the pipeline
    ``test_retrieve_route.py`` proves and answers with the same bare
    body."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["ingest", "retrieve"])
    token = issue()
    index = MemoryIndex()

    async with running(build_application(config, index_adapter=index)) as client:
        ingested = await call(
            client, token, "ingest", ingest_arguments(KEY_ONE, "the build is green")
        )
        fact_id = structured(ingested)["result"]["fact_ids"][0]
        # Stand in for the delivery loop, deterministically: this test is
        # about the tool, and the loop has its own lifecycle tests.
        deliver_projection_outbox(
            CatalogueTransactions(
                config.paths.data,
                writer_gate=threading.Lock(),
                clock=lambda: NOW,
                uuid_factory=uuid4,
            ),
            index,
            clock=lambda: NOW,
        )
        result = await call(
            client,
            token,
            "retrieve",
            {
                "scope": SCOPE,
                "query": "build",
                "budget": 4096,
                "trust_filters": ["candidate"],
            },
        )

    payload = structured(result)
    assert set(payload) == {"hits", "budget_consumed", "budget_exhausted"}
    assert [hit["fact_id"] for hit in payload["hits"]] == [fact_id]
    assert payload["hits"][0]["body"] == "the build is green"


@pytest.mark.anyio
async def test_the_instance_tool_reports_what_the_instance_route_reports(
    tmp_path: Path,
) -> None:
    """I-90's equivalence on the one operation where it is exact.

    ``instance`` is deterministic and appends nothing, so the surfaces
    compare field for field rather than minus the per-request differences
    a mutation receipt carries.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["audit-read"])
    mcp_token = issue()
    rest_token = issue()

    async with running(build_application(config)) as client:
        result = await call(client, mcp_token, "instance", {})
        rest = await client.get("/v1/instance", headers={"Authorization": rest_token})

    assert rest.status_code == 200
    payload = structured(result)
    assert payload == rest.json()
    assert payload["instance_id"] == str(INSTANCE_ID)
    assert payload["product_version"] == __version__
    assert payload["contract_identity"] == "cairn/v1"
    assert payload["contract_digest"] == sha256(packaged_contract_bytes()).hexdigest()
    # I-89: both digests, on both transports. The field-for-field equality
    # above is what proves the second one is not an MCP-only addition.
    assert (
        payload["mcp_contract_digest"] == sha256(packaged_manifest_bytes()).hexdigest()
    )


@pytest.mark.anyio
async def test_an_argument_to_the_instance_tool_is_unknown_field(
    tmp_path: Path,
) -> None:
    """P-55's first mechanic on the one tool with no request model: the
    refusal is Cairn's closed rule identity, not jsonschema prose."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["audit-read"])
    token = issue()

    async with running(build_application(config)) as client:
        result = await call(client, token, "instance", {"realm_id": REALM})

    failure = failure_of(result)
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"field_path": "realm_id", "rule": RULE_UNKNOWN_FIELD}
    assert "Input validation error" not in result["content"][0]["text"]


@pytest.mark.anyio
async def test_a_secret_bearing_scope_prefix_leaves_no_trace_anywhere(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """I-87's named proof, and the close of the amended I-74's residual.

    Until this handler screened its addressing fields, a secret-bearing
    ``scope_prefix`` reached the chain durably and irrevocably. Every
    clause the decision names is asserted below, including that the field
    path is REST's with no ``arguments.`` prefix.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["audit-read"])
    token = issue()
    hostile = {"kind": "repo", "identifier": AWS_EXAMPLE_KEY}

    async with running(build_application(config)) as client:
        result = await call(
            client, token, "read-audit-events", audit_arguments([hostile])
        )
        metrics = await client.get("/metrics")

    failure = failure_of(result)
    assert failure["code"] == "secret_rejected"
    assert failure["detail"] == {
        "policy": "cairn.secret/v1",
        "rule": AWS_RULE,
        "field_path": "scope_prefix[0].identifier",
    }
    # Nothing reached the realm chain: the screen ran before the
    # application, so the read never happened and appended no event.
    assert events(config.paths.data, "realm") == []
    (denial,) = events(config.paths.data, "instance")
    assert denial["action_code"] == "audit-read"
    assert denial["reason_code"] == "secret_upstream_awskeydetector"
    assert denial["requested_scope"] is None
    assert denial["source_scope"] is None
    assert denial["target_scope"] is None
    assert denial["principal_id"] == str(PRINCIPAL_ID)
    assert denial["safe_request_fingerprint"] == (
        hashlib.sha256(AWS_EXAMPLE_KEY.encode()).hexdigest()
    )
    captured = capsys.readouterr()
    catalogue = (config.paths.data / CATALOGUE_FILENAME).read_bytes()
    assert AWS_EXAMPLE_KEY.encode() not in catalogue
    assert AWS_EXAMPLE_KEY not in json.dumps(result)
    assert AWS_EXAMPLE_KEY not in captured.out
    assert AWS_EXAMPLE_KEY not in captured.err
    assert metrics.status_code == 200
    assert AWS_EXAMPLE_KEY not in metrics.text


@pytest.mark.anyio
async def test_a_secret_bearing_scope_on_retrieve_is_screened_too(
    tmp_path: Path,
) -> None:
    """The other argument-carrying read, on the same shared screen: the
    query is custody-screened below the adapter (I-31), but the addressing
    fields are the boundary's alone."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data, operations=["retrieve"])
    token = issue()
    hostile: dict[str, object] = {
        "realm": REALM,
        "segments": [{"kind": "repo", "identifier": AWS_EXAMPLE_KEY}],
    }

    async with running(build_application(config)) as client:
        result = await call(
            client,
            token,
            "retrieve",
            {"scope": hostile, "query": "build", "budget": 4096},
        )

    failure = failure_of(result)
    assert failure["code"] == "secret_rejected"
    assert failure["detail"] == {
        "policy": "cairn.secret/v1",
        "rule": AWS_RULE,
        "field_path": "scope.segments[0].identifier",
    }
