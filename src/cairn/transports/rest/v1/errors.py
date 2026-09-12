"""The total, fixed I-73 mapping from application failures to HTTP.

The table never varies by operation and never reinterprets an outcome
(I-26): the adapter renders the ``StableFailure`` it was handed, adds the
transport headers I-73 names, and nothing else. Transport-level admission
refusals arrive as ``WireRejection`` carrying their own refined status
(413/415/405) and keep the ``invalid_request`` body vocabulary.
"""

from uuid import UUID

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from cairn.authority.gate import NOT_FOUND_MESSAGE
from cairn.catalogue.transactions import (
    CatalogueContention,
    FailureCode,
    RetryClass,
    StableFailure,
    contention_failure,
)
from cairn.transports.rest.middleware import OWNED_FAILURE_STATE_KEY
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.paths import MCP_MOUNT_PATH
from cairn.transports.v1.wire import (
    RULE_METHOD_NOT_ALLOWED,
    FailureEnvelope,
    failure_envelope,
    invalid_request_envelope,
)

STATUS_BY_FAILURE_CODE: dict[FailureCode, int] = {
    FailureCode.INVALID_REQUEST: 400,
    FailureCode.AUTHENTICATION_FAILED: 401,
    FailureCode.AUTHORISATION_DENIED: 403,
    FailureCode.SECRET_REJECTED: 400,
    FailureCode.NOT_FOUND: 404,
    FailureCode.IDEMPOTENCY_CONFLICT: 409,
    FailureCode.INDEX_PENDING: 503,
    FailureCode.STALE_INDEX: 503,
    FailureCode.DEPENDENCY_UNAVAILABLE: 503,
    FailureCode.INSTANCE_MISMATCH: 421,
    FailureCode.INTERNAL_ERROR: 500,
}

# Every 503 carries Retry-After because those codes are the I-26
# after-delay retry class. Nothing today can estimate real recovery time,
# so the value is a pinned constant at the writer-gate contention scale —
# a maximum screened request holds the gate for roughly a second (Operator,
# 7 August 2026).
RETRY_AFTER_SECONDS = 1

_RETRY_AFTER_CODES = frozenset(
    {
        FailureCode.INDEX_PENDING,
        FailureCode.STALE_INDEX,
        FailureCode.DEPENDENCY_UNAVAILABLE,
    }
)


def register_boundary_handlers(application: FastAPI) -> None:
    """I-70's two out-of-route behaviours: an unknown path returns the
    fixed ``not_found`` failure, a known path with a wrong method returns
    ``invalid_request`` with 405.

    Neither reaches a route handler, so neither can be rendered by one —
    without these, both fall through to Starlette's ``{"detail": ...}``,
    which is not the I-26 envelope any caller of this API is entitled to
    expect. Task 4 carried the 405 wiring to Task 6; Task 6 did not pick
    it up, and the gap was found by the Task 10 correctness review, which
    is late: the contract Task 10 publishes asserts both behaviours.

    Both handlers are scoped to the ``/v1`` namespace (post-acceptance
    review, 8 August 2026): the I-26 envelope is the ``/v1`` data
    contract, and imposing it on the foundation surface — health probes
    and ``/metrics``, whose consumers are kubelets and scrapers, not
    ``/v1`` callers — rewrote responses that surface never promised.
    Outside ``/v1`` the framework default answers, exactly as it did
    before these handlers existed.

    They live here rather than beside the foundation routes because this
    module owns the I-73 rendering and the dependency runs ``v1`` →
    foundation; putting them in ``app.py`` would invert it. Handlers are
    registered per status code, so nothing else that might raise an
    ``HTTPException`` is quietly reinterpreted.
    """

    async def not_found(request: Request, exception: Exception) -> Response:
        if not _is_v1_path(request) and isinstance(exception, HTTPException):
            return await http_exception_handler(request, exception)
        return failure_response(
            StableFailure(
                code=FailureCode.NOT_FOUND,
                safe_message=NOT_FOUND_MESSAGE,
                correlation_id=request.state.correlation_id,
                retry=RetryClass.NEVER,
            ),
            request=request,
        )

    async def method_not_allowed(request: Request, exception: Exception) -> Response:
        if not _is_v1_path(request) and isinstance(exception, HTTPException):
            return await http_exception_handler(request, exception)
        # The shape Task 4 fixed for it: an admission refusal carrying the
        # invalid_request body under its own refined status. The Allow
        # header Starlette computed from the route table is preserved
        # (post-acceptance review, 8 August 2026): RFC 9110 §10.2.1 makes
        # it mandatory on every 405, and I-73's header table governs the
        # failure vocabulary, not the headers HTTP itself requires.
        response = wire_rejection_response(
            WireRejection(405, RULE_METHOD_NOT_ALLOWED, "method"),
            request.state.correlation_id,
        )
        if isinstance(exception, HTTPException) and exception.headers:
            allow = exception.headers.get("Allow")
            if allow is not None:
                response.headers["Allow"] = allow
        return response

    async def catalogue_contention(request: Request, exception: Exception) -> Response:
        # P-65's barrier makes the I-49 busy-timeout mapping reachable on
        # any route that writes the catalogue, all of which live under
        # ``/v1``. It is rendered here, at the boundary, rather than
        # per-route: no ``Rejected`` exists to hand a route handler,
        # because the denial append needs the very lock that was
        # contended. ``failure_response`` marks the failure owned, so the
        # 503 with ``Retry-After`` passes the foundation middleware
        # instead of being replaced by the generic 500 — the exact
        # substitution this failure must never suffer.
        return failure_response(
            contention_failure(request.state.correlation_id),
            request=request,
        )

    application.add_exception_handler(404, not_found)
    application.add_exception_handler(405, method_not_allowed)
    application.add_exception_handler(CatalogueContention, catalogue_contention)


def _is_v1_path(request: Request) -> bool:
    """The `/v1` namespace these boundary handlers speak for, minus the
    MCP endpoint.

    I-84 keeps the ``not_found`` and 405 handlers "scoped to the
    verb-named routes", because ``/v1/mcp`` owns its own refusals per
    I-88 — a JSON-RPC caller reaching a sub-path or a wrong method is
    answered by the mount, in its own terms, not by the REST boundary.
    Without this exclusion the mount's 404 would be re-rendered here and
    the two transports would disagree about what they had refused.
    """
    path = request.url.path
    if path in (MCP_MOUNT_PATH, "/memory/v1/mcp"):
        return False
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ("/v1", "/memory/v1")
    )


def failure_response(
    failure: StableFailure,
    *,
    request: Request | None = None,
) -> JSONResponse:
    """Renders a ``StableFailure`` through the fixed table.

    The body is the shared ``/v1`` constructor's, for the reason
    ``wire_rejection_response`` gives below: I-72's rule that ``detail``
    survives only on ``secret_rejected`` and ``invalid_request`` is one
    rule, and MCP renders the same failures. What this function adds is
    the I-73 status and that table's headers, which do not cross over.

    When ``request``  is given, the render marks the request's ASGI scope
    state as carrying an owned failure, which is what lets an I-73 5xx —
    503 with ``Retry-After``, 421, an adapter-rendered 500 — pass the
    foundation middleware instead of being replaced by the generic
    internal failure. The marker is server-side state a caller cannot
    reach; route handlers pass ``request`` for exactly this reason.
    """
    if request is not None:
        request.scope.setdefault("state", {})[OWNED_FAILURE_STATE_KEY] = True
    return _render(
        status=STATUS_BY_FAILURE_CODE[failure.code],
        code=failure.code,
        envelope=failure_envelope(failure),
    )


def wire_rejection_response(
    rejection: WireRejection,
    correlation_id: UUID,
) -> JSONResponse:
    """Renders an admission refusal: the ``invalid_request`` body under the
    rejection's own refined status (I-73's 413/415/405 clause).

    The body itself comes from the shared ``/v1`` constructor rather than
    being built here, so that the MCP adapter's I-88 rendering of the same
    refusal cannot describe it differently (I-86). ``_render``'s header
    table is not lost by the change: it adds a header only for
    ``authentication_failed`` and the three 503 codes, and an admission
    refusal is neither.
    """
    return JSONResponse(
        invalid_request_envelope(
            field_path=rejection.field_path,
            rule=rejection.rule,
            correlation_id=correlation_id,
        ).model_dump(mode="json", exclude_none=True),
        status_code=rejection.status,
    )


def _render(
    *,
    status: int,
    code: FailureCode,
    envelope: FailureEnvelope,
) -> JSONResponse:
    headers: dict[str, str] = {}
    if code is FailureCode.AUTHENTICATION_FAILED:
        headers["WWW-Authenticate"] = "Bearer"
    if code in _RETRY_AFTER_CODES:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return JSONResponse(
        envelope.model_dump(mode="json", exclude_none=True),
        status_code=status,
        headers=headers,
    )
