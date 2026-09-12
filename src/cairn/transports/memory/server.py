"""Memory-only MCP inventory, with safe errors and the shared dispatcher."""

from typing import cast
from uuid import UUID

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, Tool, ToolAnnotations
from starlette.requests import Request

from cairn.authority.gate import Actor
from cairn.catalogue.transactions import (
    CatalogueContention,
    Rejected,
    contention_failure,
)
from cairn.runtime.logging import OutcomeCode
from cairn.transports.mcp.server import (
    ACTOR_STATE_KEY,
    _idempotency_key,
    build_tool,
    failure_result,
    forbid_idempotency_key,
    internal_error_result,
    outcome_of,
    rejection_result,
    success_result,
)
from cairn.transports.memory.diagnostic_audit import argument_fingerprint
from cairn.transports.memory.dispatch import MemoryDispatch
from cairn.transports.memory.operations import (
    BY_TOOL,
    OPERATIONS,
    PROPOSAL_TOOL_NAMES,
    SESSION_TOOL_NAMES,
)
from cairn.transports.rest.middleware import OPERATION_STATE_KEY, OUTCOME_STATE_KEY
from cairn.transports.v1.operations import OperationEntry
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.wire import failure_envelope

MOUNT_PATH = "/memory/v1/mcp"

# I-27: mutation identity, not a Cairn-assigned UUIDv4 object reference.
PROPOSAL_KEY_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "minLength": 36,
    "maxLength": 36,
    "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    "description": (
        "A canonical lowercase hyphenated RFC 4122 variant UUID; version is "
        "not restricted (UUIDv5 is valid). Replaying one returns the original "
        "receipt with outcome 'replayed'."
    ),
}


def build_memory_tool(entry: OperationEntry) -> Tool:
    tool = build_tool(entry)
    if entry.tool in PROPOSAL_TOOL_NAMES and entry.mutation:
        tool.inputSchema["properties"]["idempotency_key"] = dict(PROPOSAL_KEY_SCHEMA)
    if entry.tool in {"suggest", "proposal-list", "proposal-read"}:
        tool.annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    return tool


TOOLS = tuple(build_memory_tool(entry) for entry in OPERATIONS)


def build_server(dispatch: MemoryDispatch) -> Server:
    server: Server = Server("cairn-memory")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        return list(TOOLS)

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
        cid = UUID(int=0)
        signal: dict[str, object] | None = None
        try:
            state = cast(Request, server.request_context.request).scope["state"]
            signal = state
            cid = state["correlation_id"]
            actor: Actor = state[ACTOR_STATE_KEY]
            entry = BY_TOOL[name]
            state[OPERATION_STATE_KEY] = entry.operation
            try:
                key = None
                if entry.mutation:
                    key, body = _idempotency_key(arguments)
                else:
                    forbid_idempotency_key(arguments)
                    body = arguments
                value = await dispatch.run(name, body, actor, key, cid)
                result = (
                    failure_result(failure_envelope(value.failure))
                    if isinstance(value, Rejected)
                    else success_result(value)
                )
            except WireRejection as rejection:
                if name in PROPOSAL_TOOL_NAMES:
                    await dispatch.audit_proposal_rejection(
                        actor, cid, argument_fingerprint(arguments), name
                    )
                if name == "suggest":
                    await dispatch.audit_suggestion_rejection(
                        actor, cid, argument_fingerprint(arguments)
                    )
                if name == "diagnose":
                    await dispatch.audit_diagnostic_rejection(
                        actor, cid, argument_fingerprint(arguments)
                    )
                elif name in SESSION_TOOL_NAMES:
                    await dispatch.audit_session_rejection(
                        actor, cid, argument_fingerprint(arguments), name
                    )
                result = rejection_result(rejection, cid)
            state[OUTCOME_STATE_KEY] = outcome_of(result)
            return result
        except CatalogueContention:
            if signal is not None:
                signal[OUTCOME_STATE_KEY] = OutcomeCode.UNAVAILABLE
            return failure_result(failure_envelope(contention_failure(cid)))
        except Exception:
            if signal is not None:
                signal[OUTCOME_STATE_KEY] = OutcomeCode.UNAVAILABLE
            return internal_error_result(cid)

    return server
