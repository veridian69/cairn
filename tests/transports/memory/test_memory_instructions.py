"""#108: the memory MCP server tells clients its conventions."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from memory_support import REALM, Instance, serve

from cairn.runtime.config import McpConfig
from cairn.transports.memory.contracts import MANIFEST_NAME, packaged_bytes
from cairn.transports.memory.instructions import (
    REALM_PLACEHOLDER,
    SCOPE_DESCRIPTION,
    TEMPLATE,
    connector_instructions,
)

DOC = Path(__file__).parents[3] / "docs/operations/browser-connectors.md"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _doc_block() -> str:
    section = DOC.read_text().split("## Connector instructions\n", 1)[1]
    match = re.search(r"^```text\n(.*?)\n```$", section, re.DOTALL | re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_the_connector_doc_carries_the_server_text() -> None:
    # The private doc names the deployed realm; the public one keeps the
    # placeholder. Either way the rest of the block is the server's text.
    block = _doc_block()
    realm = re.search(r'"realm": "([^"]+)"', block)
    assert realm is not None
    assert block == TEMPLATE.replace(REALM_PLACEHOLDER, realm.group(1))


def test_instructions_name_the_configured_realm_only() -> None:
    assert connector_instructions(None) is None
    text = connector_instructions("acme")
    assert text is not None
    assert '{"realm": "acme", "segments": []}' in text
    assert REALM_PLACEHOLDER not in text


def test_every_scope_parameter_in_the_manifest_carries_the_hint() -> None:
    tools = json.loads(packaged_bytes(MANIFEST_NAME))["tools"]
    scoped = [t for t in tools if "scope" in t["inputSchema"]["properties"]]
    assert scoped
    for tool in scoped:
        assert tool["inputSchema"]["properties"]["scope"]["description"] == (
            SCOPE_DESCRIPTION
        )


async def _initialize(instance: Instance) -> dict[str, Any]:
    _, token = instance.add_actor()
    async with serve(instance) as http:
        response = await http.post(
            "/memory/v1/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
    result = response.json()["result"]
    assert isinstance(result, dict)
    return result


@pytest.mark.anyio
async def test_initialize_returns_the_instructions_for_the_configured_realm(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    instance.config = instance.config.model_copy(
        update={"mcp": McpConfig(instructions_realm=REALM)}
    )

    result = await _initialize(instance)

    assert result["instructions"] == connector_instructions(REALM)


@pytest.mark.anyio
async def test_initialize_sends_no_instructions_without_a_realm(
    tmp_path: Path,
) -> None:
    result = await _initialize(Instance(tmp_path))

    assert "instructions" not in result
