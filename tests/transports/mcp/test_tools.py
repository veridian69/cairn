"""Task 6: the eleven tools as a caller meets them (I-84, I-85, P-55).

The schemas are asserted against the shared wire models rather than
transcribed, because a transcription would agree with a schema that had
drifted from the model it claims to generate from. What *is* written out
here is the surface itself — the eleven names and the mutation split —
since that is the thing the table exists to hold still.

``tools/list`` is exercised over the wire as well as read from the
registry: an inventory that exists in Python but never reaches the
protocol response would satisfy every unit assertion and no caller.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.mcp.server import TOOLS, input_schema, output_schema
from cairn.transports.rest.v1.openapi import render_document
from cairn.transports.v1.operations import OPERATIONS
from cairn.transports.v1.responses import AUDIT_EVENT_SCHEMA

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
CREDENTIAL_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
PROTOCOL_REVISION = "2025-11-25"

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_REVISION,
        "capabilities": {},
        "clientInfo": {"name": "conformance", "version": "0"},
    },
}

# I-84's surface, verbatim: hyphenated, unprefixed, one per operation.
TOOL_NAMES = {
    "ingest",
    "promote",
    "invalidate",
    "read-evidence",
    "retrieve",
    "create-principal",
    "issue-credential",
    "revoke-credential",
    "create-grant",
    "revoke-grant",
    "read-audit-events",
    "instance",
}
MUTATION_TOOLS = TOOL_NAMES - {
    "read-evidence",
    "retrieve",
    "read-audit-events",
    "instance",
}


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


def seed_bearer(data_path: Path) -> str:
    """``test_mount.py``'s recipe: one principal and one credential written
    directly, since a credential row is immutable once written and this
    package cannot carry a shared ``conftest.py``."""
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    timestamp = canonical_timestamp(NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, timestamp, None),
        )
        connection.commit()
    return f"Bearer {minted.text}"


async def listed_tools(tmp_path: Path) -> Any:
    """``tools/list`` over the protocol, behind the P-54 bearer check."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            for body in (
                INITIALIZE,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ):
                response = await client.post(
                    MOUNT_PATH,
                    content=json.dumps(body),
                    headers={**MCP_HEADERS, "Authorization": bearer},
                )
    return response.json()["result"]["tools"]


@pytest.mark.anyio
async def test_the_advertised_surface_is_exactly_the_eleven_tools(
    tmp_path: Path,
) -> None:
    """I-84: eleven tools, named with the operation vocabulary verbatim and
    unprefixed. Attic gets none, and neither does any other internal
    adapter."""
    tools = await listed_tools(tmp_path)

    assert {tool["name"] for tool in tools} == TOOL_NAMES
    assert len(tools) == 12


@pytest.mark.anyio
async def test_every_advertised_tool_declares_both_schemas(
    tmp_path: Path,
) -> None:
    """I-85: a tool's arguments are the request body and its result is the
    I-72 envelope, both declared rather than described in prose."""
    tools = await listed_tools(tmp_path)

    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        assert tool["description"]


@pytest.mark.anyio
async def test_the_advertised_schemas_are_the_generated_ones(
    tmp_path: Path,
) -> None:
    """The wire response carries the registry unchanged, so the assertions
    below — which read the registry — are assertions about what a caller
    receives."""
    tools = {tool["name"]: tool for tool in await listed_tools(tmp_path)}

    for entry in OPERATIONS:
        assert tools[entry.tool]["inputSchema"] == input_schema(entry)
        assert tools[entry.tool]["outputSchema"] == output_schema(entry)


def test_instances_input_schema_is_the_empty_object() -> None:
    """P-55: ``instance`` takes no arguments. The strict empty object, so
    the one operation without arguments is not the one that accepts
    anything."""
    instance = next(tool for tool in TOOLS if tool.name == "instance")

    assert instance.inputSchema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_the_idempotency_key_is_an_argument_on_exactly_the_eight_mutations() -> None:
    """I-27's MCP clause and I-85. A read tool refuses the key by
    construction: it neither declares the property nor admits an unknown
    one, so the refusal is machine-readable in a way an absent header is
    not."""
    carrying = set()
    for tool in TOOLS:
        properties = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])
        assert tool.inputSchema["additionalProperties"] is False
        if "idempotency_key" in properties:
            assert "idempotency_key" in required
            carrying.add(tool.name)

    assert carrying == MUTATION_TOOLS


def test_no_tool_admits_a_credential_or_a_correlation_argument() -> None:
    """I-85: the credential travels through ``Authorization`` alone and
    correlation stays an HTTP concern. Because every input schema forbids
    unknown properties, this is a property of the frozen models rather
    than of adapter vigilance — but a model that grew such a field would
    publish it here first."""
    forbidden = {"token", "credential", "authorization", "correlation_id"}

    for tool in TOOLS:
        assert set(tool.inputSchema.get("properties", {})) & forbidden == set()


def test_every_tool_input_schema_is_the_shared_request_model() -> None:
    """The arguments object *is* the REST request body, field for field,
    with the one documented addition. Compared against the model rather
    than a transcription of it, so a drifting generator fails here."""
    for entry in OPERATIONS:
        if entry.request is None:
            continue
        declared = set(input_schema(entry)["properties"])
        model = set(entry.request.model_json_schema()["properties"])
        addition = {"idempotency_key"} if entry.mutation else set()

        assert declared == model | addition


def test_a_mutation_returns_the_envelope_and_a_read_returns_the_body() -> None:
    """I-85's two result shapes, which are the two the REST 200 carries."""
    for entry in OPERATIONS:
        schema = output_schema(entry)
        if entry.mutation:
            assert set(schema["properties"]) == {
                "outcome",
                "result",
                "mutation_receipt",
                "audit_receipt",
            }
        else:
            assert schema["title"] == entry.result.__name__


def test_every_advertised_schema_resolves_its_own_references() -> None:
    """A tool schema travels alone: the I-89 manifest publishes it with no
    surrounding document, so a reference that resolves only inside the
    OpenAPI components section would reach a client as a dangling one. The
    audit document is the live case — it is hand-authored rather than
    modelled, and the shared result model references it."""
    for tool in TOOLS:
        for schema in (tool.inputSchema, tool.outputSchema or {}):
            defined = {f"#/$defs/{name}" for name in schema.get("$defs", {})}

            assert set(_references(schema)) <= defined


def _references(node: object) -> list[str]:
    if type(node) is dict:
        found = [str(value) for key, value in node.items() if key == "$ref"]
        for value in node.values():
            found.extend(_references(value))
        return found
    if type(node) is list:
        return [reference for item in node for reference in _references(item)]
    return []


def test_the_audit_document_both_surfaces_publish_is_one_document() -> None:
    """I-72: the events are the canonical ``cairn.audit/v1`` bytes, not a
    per-transport re-modelling of them. The MCP tool carries the same
    schema the OpenAPI artefact publishes as a component, differing only
    in the reference namespace each document uses."""
    read = next(tool for tool in TOOLS if tool.name == "read-audit-events")
    published = json.loads(render_document())["components"]["schemas"]["AuditEvent"]
    carried = (read.outputSchema or {})["$defs"]["AuditEvent"]

    assert carried == AUDIT_EVENT_SCHEMA
    assert carried == published
    # Every other tool is free of a definition it never mentions.
    for tool in TOOLS:
        if tool.name != "read-audit-events":
            assert "AuditEvent" not in (tool.outputSchema or {}).get("$defs", {})


def test_the_registry_is_a_projection_of_the_operation_table() -> None:
    """P-51: the tools are one of three projections of one table, not a
    second inventory that happens to agree with it today."""
    assert [tool.name for tool in TOOLS] == [entry.tool for entry in OPERATIONS]
    assert [tool.description for tool in TOOLS] == [
        entry.summary for entry in OPERATIONS
    ]
