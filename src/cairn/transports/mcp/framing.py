"""Authentication and raw-frame admission for `/v1/mcp` — Cairn's
boundary, then the SDK's.

I-86 puts every I-71 admission rule on the raw request bytes **before**
the SDK parses the frame, and the ordering is forced rather than
preferred: the pinned SDK reads the body under no size cap and parses it
with a bare ``json.loads`` (``streamable_http.py:490-493``), so handed
the frame first it would make I-71's duplicate-key rule last-value-wins
and leave I-30's 2 MiB ceiling unenforced.

This is P-53 and P-54's ASGI wrapper, holding their order in one
``__call__``: refuse a wrong method, authenticate, admit the bytes
through the shared ``admit_body``, screen the frame, then the SDK.
Authentication comes first because an unauthenticated caller must not
learn which of its frames Cairn considers well formed. The screen
(``server.screen_frame``) refuses every protocol fault I-88 names on
this side of the SDK, and ``server.jsonrpc_error`` is the single mapping
this module renders — see those functions for the SDK mechanics that
force the placement.

The body is parsed twice as a consequence. The second parse cannot
disagree with the first, because a document the two would read
differently has already been refused.
"""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import anyio.to_thread
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cairn.authority.credentials import CredentialAuthenticator
from cairn.authority.gate import Actor
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Rejected,
    StableFailure,
)
from cairn.transports.mcp.server import (
    ACTOR_STATE_KEY,
    TOOL_NAMES,
    frame_request_id,
    jsonrpc_error,
    screen_frame,
)
from cairn.transports.v1.auth import authenticate_request
from cairn.transports.v1.parsing import WireRejection, admit_body
from cairn.transports.v1.wire import (
    RULE_METHOD_NOT_ALLOWED,
    FailureEnvelope,
    failure_envelope,
    invalid_request_envelope,
)

# The endpoint's three HTTP statuses, as literals. I-88 keeps the I-73
# status table REST's, and P-50 rejects the cross-transport import that
# would reach it: this transport owns two statuses and names them, rather
# than borrowing a renderer whose table it must not consult.
#
# The frame rejection is 400 because an admission refusal reaches the
# caller as a JSON-RPC error object, and 400 is what the SDK itself gives
# a frame it could not accept (``streamable_http.py:496``). REST's refined
# 413 and 415 have no counterpart here.
FRAME_REJECTION_STATUS = 400
AUTHENTICATION_DENIAL_STATUS = 401
METHOD_REFUSAL_STATUS = 405


# No tool, and so no I-70 operation, is known when a denial is written:
# P-54 authenticates before the frame is parsed. The instance-chain event
# names the endpoint instead, in ``audit.py:28``'s kebab-case shape and
# alongside the bootstrap's own non-operation codes. Borrowing an
# operation's code would claim a request against an operation that was
# never identified — false provenance on the chain.
MOUNT_ACTION_CODE = "mcp-frame"

# SYSTEM because the transport refused a caller: neither DATA nor
# ADMINISTRATION.
MOUNT_ACTION_KIND = ActionKind.SYSTEM

# The authentication step, bound at composition and called per request. A
# callable rather than the three dependencies, so this module holds P-54's
# *order* while ``auth.py`` keeps sole ownership of what authentication
# means.
Authenticate = Callable[[Headers, UUID], Actor | Rejected]


def bearer_authentication(
    *,
    authenticator: CredentialAuthenticator,
    transactions: CatalogueTransactions,
    data_path: Path,
) -> Authenticate:
    """P-54's authentication step: ``authenticate_request``, unchanged.

    `AUTH-02`, `AUTH-03` and `AUTH-04` hold on MCP because they hold on
    the one implementation both transports call; the coarse public
    outcome, the private reason code and the safe request fingerprint are
    inherited with it.
    """

    def authenticate(headers: Headers, correlation_id: UUID) -> Actor | Rejected:
        return authenticate_request(
            headers,
            authenticator=authenticator,
            transactions=transactions,
            data_path=data_path,
            action_code=MOUNT_ACTION_CODE,
            action_kind=MOUNT_ACTION_KIND,
            correlation_id=correlation_id,
        )

    return authenticate


def _rendered(
    envelope: FailureEnvelope, status: int, headers: dict[str, str]
) -> JSONResponse:
    """One I-72 envelope as an HTTP response, on this transport's terms."""
    return JSONResponse(
        envelope.model_dump(mode="json", exclude_none=True),
        status_code=status,
        headers=headers,
    )


def authentication_denial_response(failure: StableFailure) -> JSONResponse:
    """I-88's one HTTP-layer failure: 401 with the I-26 envelope.

    The body is the shared constructor's, so the two transports cannot
    describe one denial two ways; the status and the challenge are this
    endpoint's own. ``authenticate_request`` denies as
    ``authentication_failed`` and nothing else, which is what makes a
    single status honest here. Cairn implements no part of the MCP
    authorization specification — I-24 excludes OAuth — so the challenge
    carries no ``resource_metadata`` parameter.
    """
    return _rendered(
        failure_envelope(failure),
        AUTHENTICATION_DENIAL_STATUS,
        {"WWW-Authenticate": "Bearer"},
    )


def method_refusal_response(correlation_id: UUID) -> JSONResponse:
    """P-53's wrong-method answer: the ``invalid_request`` envelope at 405.

    A wrong method carries no JSON-RPC frame to answer in kind, and I-88
    maps the 405 rule to this endpoint's own refusal of ``GET`` and
    ``DELETE`` rather than into the JSON-RPC channel. RFC 9110 §10.2.1
    makes ``Allow`` mandatory.
    """
    return _rendered(
        invalid_request_envelope(
            field_path="method",
            rule=RULE_METHOD_NOT_ALLOWED,
            correlation_id=correlation_id,
        ),
        METHOD_REFUSAL_STATUS,
        {"Allow": "POST"},
    )


def frame_rejection_response(
    rejection: WireRejection,
    correlation_id: UUID,
    *,
    request_id: str | int | None,
) -> JSONResponse:
    """One protocol fault as an HTTP response carrying a JSON-RPC error
    object.

    The object itself is ``jsonrpc_error``'s — P-56's single mapping
    function, which owns the code, the safe message and the envelope for
    every fault at this endpoint. What is left here is the rendering: this
    module owns the endpoint's statuses, and the mapping owns the body.
    ``request_id`` is ``frame_request_id``'s ruling on the refused
    document, passed through untouched.
    """
    return JSONResponse(
        jsonrpc_error(rejection, correlation_id, request_id=request_id),
        status_code=FRAME_REJECTION_STATUS,
    )


def _recording(receive: Receive, buffer: bytearray) -> Receive:
    """Passes the receive channel through, keeping the body bytes.

    ``admit_body`` streams the request rather than returning its bytes —
    it must, since the 2 MiB cap has to bite before the whole body is in
    memory — so the buffer is filled from underneath it rather than by it.
    """

    async def recording_receive() -> Message:
        message = await receive()
        # Only ``http.request`` carries a body; a disconnect has no
        # ``body`` key and so contributes nothing, which is why this needs
        # no branch on the message type.
        buffer.extend(message.get("body", b""))
        return message

    return recording_receive


def _replaying(body: bytes) -> Receive:
    """The admitted bytes, as a receive channel the SDK can consume."""
    delivered = False

    async def replaying_receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return replaying_receive


class FrameAdmission:
    """P-53 and P-54's wrapper: refuse, authenticate, or admit the raw
    bytes, without the SDK.

    A wrapper around the application rather than a call inside
    ``McpTransport``, so that a refusal provably cannot reach the SDK: the
    wrapped application is called on exactly one branch, and no refusal is
    on it.
    """

    def __init__(
        self,
        application: ASGIApp,
        *,
        authenticate: Authenticate,
        tool_names: frozenset[str] = TOOL_NAMES,
    ) -> None:
        self._application = application
        self._authenticate = authenticate
        self._tool_names = tool_names

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refusal = self._method_refusal(scope)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        correlation_id: UUID = scope["state"]["correlation_id"]
        denial = await self._authentication_denial(scope, correlation_id)
        if denial is not None:
            await denial(scope, receive, send)
            return
        admitted = bytearray()
        document: dict[str, object] | None = None
        try:
            document = await admit_body(Request(scope, _recording(receive, admitted)))
            # P-56's protocol faults, in the same refusal as I-71's
            # admission rules because they are the same kind of answer: no
            # Cairn operation was identified, so there is nothing to audit
            # and a JSON-RPC error object is what the caller gets. Here
            # rather than at the tool dispatch because the SDK's decorator
            # cannot render one — see ``screen_frame``.
            screen_frame(document, tool_names=self._tool_names)
        except WireRejection as rejection:
            # An admission refusal has no document and so no identifier —
            # JSON-RPC 2.0 §5's null case. A screened document's is
            # ``frame_request_id``'s ruling: the caller's own identifier
            # where the frame validated as a Request, null otherwise.
            frame_refusal = frame_rejection_response(
                rejection,
                correlation_id,
                request_id=None if document is None else frame_request_id(document),
            )
            await frame_refusal(scope, receive, send)
            return
        await self._application(scope, _replaying(bytes(admitted)), send)

    async def _authentication_denial(
        self, scope: Scope, correlation_id: UUID
    ) -> Response | None:
        """P-54's step, before a byte of the body is read.

        I-88 keeps this one failure at the HTTP layer, so the answer is
        ``authentication_denial_response`` rather than a JSON-RPC error
        object. Off the event loop because ``authenticate_request`` reads
        the catalogue and appends its denial synchronously.

        Sitting here, the check covers ``initialize``, ``tools/list`` and
        ``ping`` as well as ``tools/call``, and a denial appends its
        instance-chain event whatever the frame would have asked for.
        I-88's amendment of 11 August 2026 settles that: its "no audit
        event" clause governs the three protocol methods once
        authenticated, not an authentication denial, which cannot know the
        method without parsing an unauthenticated caller's bytes.
        """
        outcome = await anyio.to_thread.run_sync(
            self._authenticate, Headers(scope=scope), correlation_id
        )
        # ``isinstance`` for the reason ``auth.py:109-114`` records: mypy
        # narrows only the positive arm of ``type() is``, and both arms of
        # this closed two-member union are consumed.
        if isinstance(outcome, Rejected):
            return authentication_denial_response(outcome.failure)
        # The SDK threads the Starlette request through to a tool handler,
        # so the ``Actor`` travels in the request's own scope state rather
        # than in a contextvar or a second authentication pass. Written on
        # the admitted branch only, so a handler that finds no actor has
        # been reached without authentication and must refuse to proceed.
        scope["state"][ACTOR_STATE_KEY] = outcome
        return None

    def _method_refusal(self, scope: Scope) -> Response | None:
        """P-53's first step, before a byte of the body is read.

        ``GET`` is the SDK's standalone server-to-client stream and
        ``DELETE`` its session termination; I-84 has neither, since a
        stateless server holds no session and never initiates a message.
        Only the method is checked: routing has already established the
        path, because the endpoint is a literal ``Route``.
        """
        if scope["method"] == "POST":
            return None
        return method_refusal_response(scope["state"]["correlation_id"])
