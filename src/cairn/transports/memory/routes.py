"""Authenticated REST adapter for the separately versioned memory surface."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import anyio
from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cairn.authority.credentials import CredentialAuthenticator
from cairn.authority.gate import Actor
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.transactions import Rejected
from cairn.transports.memory.diagnostic_audit import fingerprinted_request
from cairn.transports.memory.dispatch import MemoryDispatch
from cairn.transports.memory.operations import (
    OPERATIONS,
    PROPOSAL_TOOL_NAMES,
    SESSION_TOOL_NAMES,
)
from cairn.transports.rest.v1.errors import failure_response, wire_rejection_response
from cairn.transports.rest.v1.parsing import (
    forbid_idempotency_key,
    require_idempotency_key,
)
from cairn.transports.v1.auth import authenticate_request
from cairn.transports.v1.operations import OperationEntry
from cairn.transports.v1.parsing import WireRejection, admit_body


def register_routes(
    application: FastAPI,
    *,
    dispatch: MemoryDispatch,
    authenticator: CredentialAuthenticator,
) -> None:
    def endpoint(entry: OperationEntry) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            cid: UUID = request.state.correlation_id

            def authenticate() -> Actor | Rejected:
                return authenticate_request(
                    request.headers,
                    authenticator=authenticator,
                    transactions=dispatch.transactions,
                    data_path=dispatch.data_path,
                    action_code=f"memory-{entry.tool}",
                    action_kind=ActionKind.DATA,
                    correlation_id=cid,
                )

            actor = await anyio.to_thread.run_sync(authenticate)
            if isinstance(actor, Rejected):
                return failure_response(actor.failure, request=request)
            admission_request = request
            fingerprint = None
            if (
                entry.tool in {"diagnose", "suggest"}
                or entry.tool in SESSION_TOOL_NAMES
                or entry.tool in PROPOSAL_TOOL_NAMES
            ):
                admission_request, fingerprint = fingerprinted_request(request)
            try:
                body = await admit_body(admission_request)
                key = None
                if entry.mutation:
                    key = require_idempotency_key(request.headers)
                else:
                    forbid_idempotency_key(request.headers)
                result = await dispatch.run(entry.tool, body, actor, key, cid)
            except WireRejection as rejection:
                if entry.tool in PROPOSAL_TOOL_NAMES:
                    assert fingerprint is not None
                    await dispatch.audit_proposal_rejection(
                        actor, cid, fingerprint.digest(), entry.tool
                    )
                if entry.tool == "suggest":
                    assert fingerprint is not None
                    await dispatch.audit_suggestion_rejection(
                        actor, cid, fingerprint.digest()
                    )
                if entry.tool == "diagnose":
                    assert fingerprint is not None
                    await dispatch.audit_diagnostic_rejection(
                        actor, cid, fingerprint.digest()
                    )
                elif entry.tool in SESSION_TOOL_NAMES:
                    assert fingerprint is not None
                    await dispatch.audit_session_rejection(
                        actor, cid, fingerprint.digest(), entry.tool
                    )
                return wire_rejection_response(rejection, cid)
            if isinstance(result, Rejected):
                return failure_response(result.failure, request=request)
            return JSONResponse(result.model_dump(mode="json"))

        return handle

    for entry in OPERATIONS:
        application.add_api_route(
            entry.path, endpoint(entry), methods=["POST"], name=entry.operation.value
        )


async def refuse_mcp_slash(request: Request) -> Response:
    """Keep the memory MCP spelling exact without changing old v1 redirects."""
    raise HTTPException(status_code=404)
