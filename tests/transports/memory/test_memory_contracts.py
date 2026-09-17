import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from cairn.transports.memory.contracts import (
    MANIFEST_NAME,
    OPENAPI_NAME,
    manifest_document,
    openapi_document,
    packaged_bytes,
    render_document,
)

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("operation", ["propose", "proposal-accept", "proposal-reject"])
def test_i27_proposal_key_schemas_match_variant_not_version(operation: str) -> None:
    document = openapi_document()
    header = next(
        p
        for p in document["paths"][f"/memory/v1/{operation}"]["post"]["parameters"]
        if p["name"] == "Idempotency-Key"
    )
    tool = next(t for t in manifest_document()["tools"] if t["name"] == operation)
    for schema in (
        header["schema"],
        tool["inputSchema"]["properties"]["idempotency_key"],
    ):
        validator = Draft202012Validator(schema)
        for version in "0123456789abcdef":
            for variant in "0123456789abcdef":
                key = f"22222222-2222-{version}222-{variant}222-222222222222"
                assert validator.is_valid(key) == (variant in "89ab"), key
        key = "abcdefab-cdef-5abc-8abc-abcdefabcdef"
        for bad in (
            key.upper(),
            "{" + key + "}",
            "urn:uuid:" + key,
            key.replace("-", ""),
            key + " ",
            key + "\n",
        ):
            assert not validator.is_valid(bad), bad


def test_i27_schema_specialisation_does_not_mutate_shared_v1_keys() -> None:
    from cairn.transports.mcp.server import build_tool
    from cairn.transports.memory.operations import OPERATIONS
    from cairn.transports.rest.v1.openapi import _path_item

    # Every non-proposal operation must still use precisely its shared builder.
    document = openapi_document()
    tools = {t["name"]: t for t in manifest_document()["tools"]}
    for entry in OPERATIONS:
        if entry.mutation and entry.tool not in {
            "propose",
            "proposal-accept",
            "proposal-reject",
        }:
            assert document["paths"][entry.path] == _path_item(entry)
            assert tools[entry.tool]["inputSchema"] == build_tool(entry).inputSchema


@pytest.mark.parametrize("name,mcp", [(OPENAPI_NAME, False), (MANIFEST_NAME, True)])
def test_separate_contracts_are_packaged_and_pinned(name: str, mcp: bool) -> None:
    document = render_document(mcp=mcp).encode()
    assert (ROOT / "contracts" / name).read_bytes() == document
    assert packaged_bytes(name) == document
    assert (ROOT / "contracts" / (name + ".sha256")).read_text().split()[
        0
    ] == hashlib.sha256(document).hexdigest()


def _refs(node: Any, root: Any) -> None:
    if isinstance(node, dict):
        if "$ref" in node:
            pointer = node["$ref"]
            assert pointer.startswith("#/")
            target = root
            for component in pointer[2:].split("/"):
                target = target[component]
        for value in node.values():
            _refs(value, root)
    elif isinstance(node, list):
        for value in node:
            _refs(value, root)


def test_all_contract_references_resolve_and_tool_schemas_are_closed() -> None:
    document = openapi_document()
    _refs(document, document)
    assert set(document["paths"]) == {
        f"/memory/v1/{name}"
        for name in (
            "diagnose",
            "remember",
            "recall",
            "history",
            "suggest",
            "propose",
            "proposal-list",
            "proposal-read",
            "proposal-accept",
            "proposal-reject",
            "disagree",
            "resolve",
            "correct",
            "session-open",
            "turn-begin",
            "turn-prepare",
            "turn-commit",
            "turn-abandon",
            "session-read",
            "visit-issue",
            "visit-acknowledge",
        )
    }
    for tool in manifest_document()["tools"]:
        for schema in ("inputSchema", "outputSchema"):
            value = tool[schema]
            Draft202012Validator.check_schema(value)
            _refs(value, value)
            assert value["additionalProperties"] is False
    recall = next(t for t in manifest_document()["tools"] if t["name"] == "recall")
    assert recall["outputSchema"]["properties"]["hits"]["items"]["$ref"].endswith(
        "/MemoryFactBody"
    )
    assert "cairn.memory/v1" in json.dumps(document)


@pytest.mark.parametrize("missing", [OPENAPI_NAME, MANIFEST_NAME])
def test_missing_packaged_memory_contract_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    from memory_support import Instance

    from cairn.runtime import composition

    instance = Instance(tmp_path)
    original = packaged_bytes

    def unavailable(name: str) -> bytes:
        if name == missing:
            raise FileNotFoundError("Missing packaged memory contract")
        return original(name)

    monkeypatch.setattr(composition, "packaged_bytes", unavailable)
    with pytest.raises(FileNotFoundError):
        instance.application()
