"""The ``/v1`` router (I-70) — HTTP orchestration, and only that.

Every mutation route runs the same sequence the Task 4/5 stubs fixed:
authenticate (headers only, before the body is read), admit the body,
extract the idempotency key, validate the strict wire model, translate to
the frozen command — and only then, under the P-29 writer gate, screen
addressing and invoke the application. The gate is an ``anyio.Lock`` held
across exactly the synchronous catalogue section, which runs in a worker
thread; it is never held while awaiting the client, so a slow sender
cannot starve writers. Authentication's rare denial append serialises on
the catalogue's own writer gate instead — taking the request-level gate
before the body is read would hand exactly that starvation to every
caller.

The wire↔command translation the sequence names moved to
``cairn.transports.v1.translation`` in slice 7's Task 7, unchanged: the
MCP tool handlers must produce the same commands from the same models and
render the same results, and I-90 makes a second copy of that mapping a
way for the two surfaces to disagree about which field a caller got
wrong. What remains here is HTTP — the request/response shape, the
``Idempotency-Key`` header, and the ``JSONResponse`` that carries a
rendered body.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

import anyio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from cairn.administration.audit_read import AuditEventPage, read_audit_events
from cairn.administration.commands import CairnAdministration
from cairn.authority.credentials import CredentialAuthenticator
from cairn.authority.gate import Actor
from cairn.authority.mutations import CairnAuthority
from cairn.authority.retrieval import RetrievalResult
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    MutationOutcome,
    Rejected,
    Replayed,
)
from cairn.runtime.logging import Operation
from cairn.screening import SecretScreen
from cairn.transports.rest.v1.errors import failure_response, wire_rejection_response
from cairn.transports.rest.v1.parsing import (
    forbid_idempotency_key,
    require_idempotency_key,
)
from cairn.transports.v1.auth import authenticate_request, screen_addressing
from cairn.transports.v1.parsing import WireRejection, admit_body
from cairn.transports.v1.requests import (
    CreateGrantRequest,
    CreatePrincipalRequest,
    IngestRequest,
    InvalidateRequest,
    IssueCredentialRequest,
    PromoteRequest,
    ReadAuditEventsRequest,
    RetrieveRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
)
from cairn.transports.v1.responses import InstanceResult
from cairn.transports.v1.translation import (
    create_grant_command,
    create_grant_result,
    create_principal_command,
    create_principal_result,
    ingest_command,
    ingest_result,
    invalidate_command,
    invalidate_result,
    issue_credential_command,
    issue_credential_result,
    promote_command,
    promote_result,
    read_audit_events_command,
    read_audit_events_result,
    retrieve_command,
    retrieve_result,
    revoke_credential_command,
    revoke_credential_result,
    revoke_grant_command,
    revoke_grant_result,
    success_envelope,
    validated,
)
from cairn.transports.v1.wire import CONTRACT_IDENTITY, WireModel, encode_uuid


def register_v1_routes(
    application: FastAPI,
    *,
    authenticator: CredentialAuthenticator,
    authority: CairnAuthority,
    administration: CairnAdministration,
    transactions: CatalogueTransactions,
    screen: SecretScreen,
    data_path: Path,
    write_gate: anyio.Lock,
    instance_id: UUID,
    product_version: str,
    contract_digest: str,
    mcp_contract_digest: str,
    clock: Callable[[], datetime],
) -> None:
    async def authenticate(
        request: Request, *, action_code: str, action_kind: ActionKind
    ) -> Actor | Rejected:
        correlation_id: UUID = request.state.correlation_id

        def run() -> Actor | Rejected:
            return authenticate_request(
                request.headers,
                authenticator=authenticator,
                transactions=transactions,
                data_path=data_path,
                action_code=action_code,
                action_kind=action_kind,
                correlation_id=correlation_id,
            )

        return await anyio.to_thread.run_sync(run)

    async def run_mutation[CommandT, ValueT, ResultT: WireModel](
        request: Request,
        *,
        action_code: str,
        action_kind: ActionKind,
        translate: Callable[[dict[str, object]], CommandT],
        invoke: Callable[[Actor, CommandT, UUID, UUID], MutationOutcome[ValueT]],
        render: Callable[[ValueT], ResultT],
    ) -> Response:
        correlation_id: UUID = request.state.correlation_id
        outcome = await authenticate(
            request, action_code=action_code, action_kind=action_kind
        )
        if isinstance(outcome, Rejected):
            return failure_response(outcome.failure, request=request)
        try:
            body = await admit_body(request)
            idempotency_key = require_idempotency_key(request.headers)
            command = translate(body)
        except WireRejection as rejection:
            return wire_rejection_response(rejection, correlation_id)

        def execute() -> MutationOutcome[ValueT]:
            denied = screen_addressing(
                body,
                screen=screen,
                actor=outcome,
                transactions=transactions,
                data_path=data_path,
                action_code=action_code,
                action_kind=action_kind,
                correlation_id=correlation_id,
            )
            if denied is not None:
                return denied
            return invoke(outcome, command, idempotency_key, correlation_id)

        async with write_gate:
            result = await anyio.to_thread.run_sync(execute)
        if isinstance(result, Rejected):
            return failure_response(result.failure, request=request)
        return _success(result, render(result.value))

    @application.post("/v1/ingest", name=Operation.INGEST.value)
    async def ingest(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="ingest",
            action_kind=ActionKind.DATA,
            translate=lambda body: ingest_command(validated(IngestRequest, body)),
            invoke=lambda actor, command, key, cid: authority.ingest(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=ingest_result,
        )

    @application.post("/v1/promote", name=Operation.PROMOTE.value)
    async def promote(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="promote",
            action_kind=ActionKind.DATA,
            translate=lambda body: promote_command(validated(PromoteRequest, body)),
            invoke=lambda actor, command, key, cid: authority.promote(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=promote_result,
        )

    @application.post("/v1/invalidate", name=Operation.INVALIDATE.value)
    async def invalidate(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="invalidate",
            action_kind=ActionKind.DATA,
            translate=lambda body: invalidate_command(
                validated(InvalidateRequest, body)
            ),
            invoke=lambda actor, command, key, cid: authority.invalidate(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=invalidate_result,
        )

    @application.post("/v1/create-principal", name=Operation.CREATE_PRINCIPAL.value)
    async def create_principal(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="create-principal",
            action_kind=ActionKind.ADMINISTRATION,
            translate=lambda body: create_principal_command(
                validated(CreatePrincipalRequest, body)
            ),
            invoke=lambda actor, command, key, cid: administration.create_principal(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=create_principal_result,
        )

    @application.post("/v1/issue-credential", name=Operation.ISSUE_CREDENTIAL.value)
    async def issue_credential(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="issue-credential",
            action_kind=ActionKind.ADMINISTRATION,
            translate=lambda body: issue_credential_command(
                validated(IssueCredentialRequest, body)
            ),
            invoke=lambda actor, command, key, cid: administration.issue_credential(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=issue_credential_result,
        )

    @application.post("/v1/revoke-credential", name=Operation.REVOKE_CREDENTIAL.value)
    async def revoke_credential(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="revoke-credential",
            action_kind=ActionKind.ADMINISTRATION,
            translate=lambda body: revoke_credential_command(
                validated(RevokeCredentialRequest, body)
            ),
            invoke=lambda actor, command, key, cid: administration.revoke_credential(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=revoke_credential_result,
        )

    @application.post("/v1/create-grant", name=Operation.CREATE_GRANT.value)
    async def create_grant(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="create-grant",
            action_kind=ActionKind.ADMINISTRATION,
            translate=lambda body: create_grant_command(
                validated(CreateGrantRequest, body)
            ),
            invoke=lambda actor, command, key, cid: administration.create_grant(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=create_grant_result,
        )

    @application.post("/v1/revoke-grant", name=Operation.REVOKE_GRANT.value)
    async def revoke_grant(request: Request) -> Response:
        return await run_mutation(
            request,
            action_code="revoke-grant",
            action_kind=ActionKind.ADMINISTRATION,
            translate=lambda body: revoke_grant_command(
                validated(RevokeGrantRequest, body)
            ),
            invoke=lambda actor, command, key, cid: administration.revoke_grant(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            render=revoke_grant_result,
        )

    @application.post("/v1/read-audit-events", name=Operation.READ_AUDIT_EVENTS.value)
    async def read_audit(request: Request) -> Response:
        """The audit read: a ``POST`` despite being a read because scope
        paths must never appear in a URL (I-70). P-28 forbids the
        idempotency key here, and the read runs under the P-29 gate
        because a successful read appends its own audit event."""
        correlation_id: UUID = request.state.correlation_id
        outcome = await authenticate(
            request,
            action_code="audit-read",
            action_kind=ActionKind.ADMINISTRATION,
        )
        if isinstance(outcome, Rejected):
            return failure_response(outcome.failure, request=request)
        try:
            body = await admit_body(request)
            forbid_idempotency_key(request.headers)
            command = read_audit_events_command(validated(ReadAuditEventsRequest, body))
        except WireRejection as rejection:
            return wire_rejection_response(rejection, correlation_id)

        def execute() -> AuditEventPage | Rejected:
            denied = screen_addressing(
                body,
                screen=screen,
                actor=outcome,
                transactions=transactions,
                data_path=data_path,
                action_code="audit-read",
                action_kind=ActionKind.ADMINISTRATION,
                correlation_id=correlation_id,
            )
            if denied is not None:
                return denied
            return read_audit_events(
                data_path,
                transactions,
                outcome,
                command,
                correlation_id=correlation_id,
                clock=clock,
            )

        async with write_gate:
            page = await anyio.to_thread.run_sync(execute)
        if isinstance(page, Rejected):
            return failure_response(page.failure, request=request)
        return JSONResponse(read_audit_events_result(page).model_dump(mode="json"))

    @application.post("/v1/retrieve", name=Operation.RETRIEVE.value)
    async def retrieve(request: Request) -> Response:
        """P-44: reconciled retrieval. A ``POST`` for the same reason the
        audit read is — scope paths and the query must never appear in a
        URL (I-70) — with no idempotency key (I-27's read rule) and the
        bare result body the other two read routes return.

        Unlike the audit read this does *not* take the P-29 write gate. It
        appends one allow event, so it does write; but a read that queued
        behind every other read would serialise the route retrieval exists
        to make fast, and the append already serialises on the inner
        writer gate where it must. The audit read's gate is inherited from
        its own P-29 ruling and is not a precedent this has to follow.
        """
        correlation_id: UUID = request.state.correlation_id
        outcome = await authenticate(
            request,
            action_code="retrieve",
            action_kind=ActionKind.DATA,
        )
        if isinstance(outcome, Rejected):
            return failure_response(outcome.failure, request=request)
        try:
            body = await admit_body(request)
            forbid_idempotency_key(request.headers)
            command = retrieve_command(validated(RetrieveRequest, body))
        except WireRejection as rejection:
            return wire_rejection_response(rejection, correlation_id)

        def execute() -> RetrievalResult | Rejected:
            # The addressing screen covers the scope, as on every route;
            # the query is screened inside the pipeline (I-31), after value
            # validation bounds it and before any remote call.
            denied = screen_addressing(
                body,
                screen=screen,
                actor=outcome,
                transactions=transactions,
                data_path=data_path,
                action_code="retrieve",
                action_kind=ActionKind.DATA,
                correlation_id=correlation_id,
            )
            if denied is not None:
                return denied
            return authority.retrieve(outcome, command, correlation_id=correlation_id)

        result = await anyio.to_thread.run_sync(execute)
        if isinstance(result, Rejected):
            return failure_response(result.failure, request=request)
        return JSONResponse(retrieve_result(result).model_dump(mode="json"))

    @application.get("/v1/instance", name=Operation.INSTANCE.value)
    async def instance(request: Request) -> Response:
        """I-70: authenticated, no caller input; reports the I-29 contract
        identity, product version and the SHA-256 of each packaged
        artefact — the OpenAPI document and the I-89 MCP manifest — both
        computed once at startup through the composition contract seam."""
        correlation_id: UUID = request.state.correlation_id
        outcome = await authenticate(
            request, action_code="instance", action_kind=ActionKind.SYSTEM
        )
        if isinstance(outcome, Rejected):
            return failure_response(outcome.failure, request=request)
        try:
            forbid_idempotency_key(request.headers)
        except WireRejection as rejection:
            return wire_rejection_response(rejection, correlation_id)
        result = InstanceResult(
            instance_id=encode_uuid(instance_id),
            product_version=product_version,
            contract_identity=CONTRACT_IDENTITY,
            contract_digest=contract_digest,
            mcp_contract_digest=mcp_contract_digest,
        )
        return JSONResponse(result.model_dump(mode="json"))


def _success[ResultT: WireModel, ValueT](
    result: Committed[ValueT] | Replayed[ValueT],
    body: ResultT,
) -> JSONResponse:
    """The I-72 envelope, carried by HTTP. The envelope itself is
    ``transports.v1.translation``'s, because MCP returns the identical
    object as ``structuredContent`` (I-85); only the ``JSONResponse``
    around it is REST's."""
    return JSONResponse(success_envelope(result, body).model_dump(mode="json"))
