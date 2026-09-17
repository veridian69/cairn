"""Deterministic, separately packaged memory contracts; v1 stays frozen."""

import json
from importlib import resources
from typing import Any

from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from cairn.catalogue.transactions import FailureCode
from cairn.transports.memory.operations import OPERATIONS, PROPOSAL_TOOL_NAMES
from cairn.transports.memory.server import PROPOSAL_KEY_SCHEMA, build_memory_tool
from cairn.transports.rest.v1.openapi import _path_item, _success_name
from cairn.transports.v1.wire import FailureEnvelope

OPENAPI_NAME = "cairn-memory-openapi-v1.json"
MANIFEST_NAME = "cairn-memory-mcp-tools-v1.json"


def packaged_bytes(name: str) -> bytes:
    if name not in (OPENAPI_NAME, MANIFEST_NAME):
        raise ValueError("Unknown memory contract")
    return resources.files("cairn").joinpath("contracts", name).read_bytes()


def openapi_document() -> dict[str, Any]:
    models: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (FailureEnvelope, "serialization")
    ]
    for entry in OPERATIONS:
        assert entry.request is not None
        models.extend([(entry.request, "validation"), (entry.success, "serialization")])
    keys, definitions = models_json_schema(
        models, ref_template="#/components/schemas/{model}"
    )
    renames = {
        str(keys[(entry.success, "serialization")]["$ref"]).rsplit("/", 1)[
            1
        ]: _success_name(entry)
        for entry in OPERATIONS
        if entry.mutation
    }
    schemas = {
        renames.get(name, name): schema for name, schema in definitions["$defs"].items()
    }
    paths: dict[str, Any] = {entry.path: _path_item(entry) for entry in OPERATIONS}
    for entry in OPERATIONS:
        if entry.tool in PROPOSAL_TOOL_NAMES and entry.mutation:
            for parameter in paths[entry.path]["post"]["parameters"]:
                if parameter["name"] == "Idempotency-Key":
                    parameter["schema"] = dict(PROPOSAL_KEY_SCHEMA)
                    parameter["description"] = PROPOSAL_KEY_SCHEMA["description"]
    return {
        "openapi": "3.1.0",
        "info": {"title": "Cairn shared memory", "version": "cairn.memory/v1"},
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        },
    }


def manifest_document() -> dict[str, Any]:
    return {
        "contractIdentity": "cairn.memory/v1",
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "tools": [
            build_memory_tool(entry).model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            for entry in OPERATIONS
        ],
        "failureCodes": [code.value for code in FailureCode],
    }


def render_document(*, mcp: bool = False) -> str:
    return (
        json.dumps(
            manifest_document() if mcp else openapi_document(),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    import sys

    print(render_document(mcp="--mcp" in sys.argv[1:]), end="")
