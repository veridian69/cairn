import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import RFC_4122, UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cairn.operations.metrics import Metrics
from cairn.runtime.logging import (
    LogEvent,
    Operation,
    OutcomeCode,
    SafeLogger,
    Transport,
)

_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_STACK_BASENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,127}\Z")
_OPERATION_BY_ROUTE_NAME = {operation.value: operation for operation in Operation}

# Set on ASGI scope state by the ``/v1`` failure renderer
# (``transports.rest.v1.errors``) and readable only by in-process code, so
# it is not caller-forgeable. A marked 5xx is an owned I-73 body — 503 with
# ``Retry-After``, 421, an adapter-rendered 500 — and passes through
# instead of being replaced by the generic internal failure (Operator,
# 7 August 2026; the byte-exact approval list cannot cover bodies carrying
# fresh correlation identifiers).
OWNED_FAILURE_STATE_KEY = "cairn_owned_failure"

# The same signalling channel, for P-57's two labels. The MCP endpoint is
# a single ASGI route serving eleven operations, so the route-name lookup
# below cannot label it and the adapter supplies the resolved values
# instead. In-process only, like the key above, so neither is forgeable by
# a caller.
#
# ``TRANSPORT_STATE_KEY`` is written once at the mount's entry rather than
# at tool resolution: a request refused before a tool is identified — an
# authentication denial, a protocol fault — is still an MCP request, and
# labelling it ``rest`` because it failed early would put MCP's refusals
# in REST's series. ``OPERATION_STATE_KEY`` is written later, when the
# tool is actually known.
OPERATION_STATE_KEY = "cairn_operation"
TRANSPORT_STATE_KEY = "cairn_transport"

# ``OUTCOME_STATE_KEY`` exists for the same reason as the operation key,
# one level further in: the status line cannot carry an MCP tool's
# outcome. Every identified tool answers HTTP 200 and puts the outcome
# inside the result (I-88), so an authorisation denial, a secret
# rejection and an internal error all reach this middleware as 200 and
# would be counted as successes — the whole MCP failure series filed
# under ``outcome_code="success"``. The adapter therefore signals the
# outcome it resolved, and this middleware prefers it over the status.
#
# The alternative — decoding the buffered MCP result here — was rejected:
# it would make the one transport-neutral middleware parse one
# transport's body, and it would read the answer back out of a document
# this layer has no business understanding.
OUTCOME_STATE_KEY = "cairn_outcome"


@dataclass(frozen=True, slots=True)
class _OwnedResponseStart:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]


@dataclass(frozen=True, slots=True)
class _OwnedResponseBody:
    body: bytes
    more_body: bool
    more_body_present: bool


type _OwnedResponseMessage = _OwnedResponseStart | _OwnedResponseBody
type _SealedResponse = tuple[_OwnedResponseMessage, ...]

_APPROVED_UNAVAILABLE_RESPONSES = {
    Operation.HEALTH_STARTUP: (
        _OwnedResponseStart(
            status=503,
            headers=(
                (b"content-length", b"21"),
                (b"content-type", b"application/json"),
            ),
        ),
        _OwnedResponseBody(
            body=b'{"status":"starting"}',
            more_body=False,
            more_body_present=False,
        ),
    ),
    Operation.HEALTH_READY: (
        _OwnedResponseStart(
            status=503,
            headers=(
                (b"content-length", b"22"),
                (b"content-type", b"application/json"),
            ),
        ),
        _OwnedResponseBody(
            body=b'{"status":"not-ready"}',
            more_body=False,
            more_body_present=False,
        ),
    ),
}


class _SmallResponseBuffer:
    def __init__(self) -> None:
        self._messages: list[_OwnedResponseMessage] = []
        self._started = False
        self._terminal = False
        self._valid = True
        self._sealed = False
        self._protocol_violation = False

    async def send(self, message: Message) -> None:
        if self._sealed:
            self._protocol_violation = True
            return
        if not self._valid:
            return
        if type(message) is not dict:
            self._reject()
            return

        message_type: object = message.get("type")
        if type(message_type) is not str:
            self._reject()
            return
        if message_type == "http.response.start":
            self._accept_start(message)
            return
        if message_type == "http.response.body":
            self._accept_body(message)
            return
        self._reject()

    @property
    def is_complete(self) -> bool:
        return self._valid and self._started and self._terminal

    @property
    def protocol_violation(self) -> bool:
        return self._protocol_violation

    @property
    def status(self) -> int | None:
        if not self._messages:
            return None
        start = self._messages[0]
        if type(start) is not _OwnedResponseStart:
            return None
        return start.status

    def is_approved_unavailable(self, operation: Operation | None) -> bool:
        if operation is None:
            return False
        expected = _APPROVED_UNAVAILABLE_RESPONSES.get(operation)
        return expected is not None and tuple(self._messages) == expected

    def seal(self) -> _SealedResponse | None:
        if not self.is_complete:
            return None
        self._sealed = True
        return tuple(self._messages)

    async def replay(
        self,
        response: _SealedResponse,
        send: Send,
        correlation_id: UUID,
    ) -> None:
        for message in response:
            if isinstance(message, _OwnedResponseStart):
                headers = [
                    (bytes(name), bytes(value))
                    for name, value in message.headers
                    if name.lower() != b"x-correlation-id"
                ]
                headers.append(
                    (
                        b"x-correlation-id",
                        str(correlation_id).encode("ascii"),
                    )
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": message.status,
                        "headers": headers,
                    }
                )
                continue

            body_message: Message = {
                "type": "http.response.body",
                "body": bytes(message.body),
            }
            if message.more_body_present:
                body_message["more_body"] = message.more_body
            await send(body_message)

    def _accept_start(self, message: Message) -> None:
        if (
            self._started
            or self._terminal
            or set(message)
            != {
                "type",
                "status",
                "headers",
            }
        ):
            self._reject()
            return

        status: object = message.get("status")
        headers = _snapshot_headers(message.get("headers"))
        if type(status) is not int or not 100 <= status <= 599 or headers is None:
            self._reject()
            return

        self._messages.append(
            _OwnedResponseStart(
                status=status,
                headers=headers,
            )
        )
        self._started = True

    def _accept_body(self, message: Message) -> None:
        allowed_keys = {"type", "body", "more_body"}
        if (
            not self._started
            or self._terminal
            or not {"type", "body"} <= set(message) <= allowed_keys
        ):
            self._reject()
            return

        body: object = message.get("body")
        more_body_present = "more_body" in message
        more_body: object = message.get("more_body", False)
        if type(body) is not bytes or type(more_body) is not bool:
            self._reject()
            return

        self._messages.append(
            _OwnedResponseBody(
                body=bytes(body),
                more_body=more_body,
                more_body_present=more_body_present,
            )
        )
        if not more_body:
            self._terminal = True

    def _reject(self) -> None:
        self._protocol_violation = True
        self._messages.clear()
        self._valid = False


def _snapshot_headers(
    submitted: object,
) -> tuple[tuple[bytes, bytes], ...] | None:
    if type(submitted) is not list:
        return None
    owned: list[tuple[bytes, bytes]] = []
    for pair in submitted:
        if type(pair) is not tuple or len(pair) != 2:
            return None
        name, value = pair
        if type(name) is not bytes or type(value) is not bytes:
            return None
        owned.append((bytes(name), bytes(value)))
    return tuple(owned)


class FoundationMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        logger: SafeLogger,
        metrics: Metrics,
    ) -> None:
        self._app = app
        self._logger = logger
        self._metrics = metrics

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _correlation_id(scope)
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        started_at = time.monotonic()
        response_buffer = _SmallResponseBuffer()

        try:
            await self._app(scope, receive, response_buffer.send)
        except Exception as error:
            duration_ms = _duration_ms(started_at)
            # Both labels are read *after* the application ran: the MCP
            # mount writes them into scope state while it is serving, so
            # reading either before the call above would see nothing and
            # silently label every MCP request as an unlabelled REST one.
            operation = _operation(scope)
            transport = _transport(scope)
            self._record_request(
                correlation_id=correlation_id,
                operation=operation,
                transport=transport,
                outcome_code=OutcomeCode.INTERNAL_ERROR,
                duration_ms=duration_ms,
                error=error,
            )
            await _send_internal_failure(
                scope,
                receive,
                send,
                correlation_id,
            )
            return

        operation = _operation(scope)
        transport = _transport(scope)
        status_code = response_buffer.status
        owned_failure = state.get(OWNED_FAILURE_STATE_KEY) is True
        if _requires_internal_failure(response_buffer, operation, owned_failure):
            self._record_request(
                correlation_id=correlation_id,
                operation=operation,
                transport=transport,
                outcome_code=OutcomeCode.INTERNAL_ERROR,
                duration_ms=_duration_ms(started_at),
            )
            await _send_internal_failure(
                scope,
                receive,
                send,
                correlation_id,
            )
            return

        sealed_response = response_buffer.seal()
        if sealed_response is None:
            self._record_request(
                correlation_id=correlation_id,
                operation=operation,
                transport=transport,
                outcome_code=OutcomeCode.INTERNAL_ERROR,
                duration_ms=_duration_ms(started_at),
            )
            await _send_internal_failure(
                scope,
                receive,
                send,
                correlation_id,
            )
            return

        outcome_code = _resolved_outcome(scope, status_code)
        try:
            await response_buffer.replay(
                sealed_response,
                send,
                correlation_id,
            )
        except Exception as error:
            self._record_request(
                correlation_id=correlation_id,
                operation=operation,
                transport=transport,
                outcome_code=OutcomeCode.INTERNAL_ERROR,
                duration_ms=_duration_ms(started_at),
                error=error,
            )
            raise
        else:
            self._record_request(
                correlation_id=correlation_id,
                operation=operation,
                transport=transport,
                outcome_code=(
                    OutcomeCode.INTERNAL_ERROR
                    if response_buffer.protocol_violation
                    else outcome_code
                ),
                duration_ms=_duration_ms(started_at),
            )

    def _record_request(
        self,
        *,
        correlation_id: UUID,
        operation: Operation | None,
        transport: Transport,
        outcome_code: OutcomeCode,
        duration_ms: float,
        error: Exception | None = None,
    ) -> None:
        exception_type, stack_location = _safe_exception_details(error)
        self._logger.emit(
            LogEvent.REQUEST_COMPLETED,
            level=logging.ERROR if error is not None else logging.INFO,
            correlation_id=correlation_id,
            operation=operation,
            transport=transport,
            outcome_code=outcome_code,
            duration_ms=duration_ms,
            exception_type=exception_type,
            stack_location=stack_location,
        )
        if operation is not None:
            self._metrics.observe_request(
                operation,
                transport=transport,
                outcome_code=outcome_code,
                duration_ms=duration_ms,
            )


def _correlation_id(scope: Scope) -> UUID:
    """I-32: a caller-supplied UUIDv4 is adopted, anything else ignored in
    favour of a fresh one — never an error.

    The version check is not decoration. ``AuditDraft`` validates every
    correlation identifier as RFC 4122 version 4, so adopting a canonical
    UUID of any other version handed the domain a value it refuses: the
    draft construction raised, the request answered 500, and on the
    unauthenticated path the durable denial event was never appended at
    all. One header suppressed the audit record of a failed
    authentication — the attempts the chain most needs. Found by the Task
    10 correctness review and reproduced before this fix.
    """
    submitted = Headers(scope=scope).get("X-Correlation-ID")
    if submitted is not None:
        try:
            parsed = UUID(submitted)
        except ValueError:
            pass
        else:
            # Both halves, matching ``audit._validate_uuid`` exactly: the
            # version nibble alone leaves
            # ``00000000-0000-4000-0000-000000000000`` — version 4, variant
            # RESERVED_NCS — adopted here and refused there, which is the
            # same defect one nibble along.
            if (
                str(parsed) == submitted
                and parsed.version == 4
                and parsed.variant == RFC_4122
            ):
                return parsed
    return uuid4()


def _operation(scope: Scope) -> Operation | None:
    """The operation label, preferring what an adapter resolved (P-57).

    The scope-state value wins because the route name cannot be right for
    every surface: ``/v1/mcp`` is one route carrying eleven operations, so
    its adapter names the operation once it has resolved the tool. The
    route-name lookup remains for REST, where the route *is* the
    operation.
    """
    signalled = scope.get("state", {}).get(OPERATION_STATE_KEY)
    if type(signalled) is Operation:
        return signalled
    route = scope.get("route")
    route_name = getattr(route, "name", None)
    if type(route_name) is not str:
        return None
    return _OPERATION_BY_ROUTE_NAME.get(route_name)


def _transport(scope: Scope) -> Transport:
    """Which surface served the request.

    REST does not write the key — it is the surface every route but one
    belongs to — so its absence means REST. That is a fallback on the
    *resolution*, not the ``rest`` default on the *argument* P-57 forbids:
    the mount writes ``Transport.MCP`` unconditionally on entry, before
    anything it serves can fail, so no MCP outcome depends on a later
    write happening.
    """
    signalled = scope.get("state", {}).get(TRANSPORT_STATE_KEY)
    if type(signalled) is Transport:
        return signalled
    return Transport.REST


def _requires_internal_failure(
    response: _SmallResponseBuffer,
    operation: Operation | None,
    owned_failure: bool,
) -> bool:
    if not response.is_complete:
        return True
    status_code = response.status
    if status_code is None:
        return True
    if status_code < 500:
        return False
    if owned_failure:
        return False
    return not (status_code == 503 and response.is_approved_unavailable(operation))


def _outcome_code(status_code: int | None) -> OutcomeCode:
    if status_code is None or status_code >= 500:
        return OutcomeCode.UNAVAILABLE
    if status_code >= 400:
        return OutcomeCode.INVALID_REQUEST
    return OutcomeCode.SUCCESS


def _resolved_outcome(scope: Scope, status_code: int | None) -> OutcomeCode:
    """The outcome a request actually had, signalled where the status
    cannot carry it.

    The signal wins because the status line cannot be right for every
    surface: an identified MCP tool answers 200 whatever it decided, so
    reading the status alone files every MCP failure as a success. Where
    nothing was signalled — REST, and every MCP refusal decided before a
    tool was identified, which does carry a real 4xx — the status remains
    the answer.
    """
    signalled = scope.get("state", {}).get(OUTCOME_STATE_KEY)
    if type(signalled) is OutcomeCode:
        return signalled
    return _outcome_code(status_code)


def _duration_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000


async def _send_internal_failure(
    scope: Scope,
    receive: Receive,
    send: Send,
    correlation_id: UUID,
) -> None:
    async def send_with_correlation(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message)["X-Correlation-ID"] = str(correlation_id)
        await send(message)

    response = JSONResponse(
        {
            "failure": {
                "code": "internal_error",
                "message": "The request could not be completed.",
                "retry": "never",
                "correlation_id": str(correlation_id),
            }
        },
        status_code=500,
    )
    await response(scope, receive, send_with_correlation)


def _safe_exception_details(
    error: Exception | None,
) -> tuple[str | None, str | None]:
    if error is None:
        return None, None

    candidate_exception_type = type(error).__name__
    exception_type: str | None = (
        candidate_exception_type
        if _EXCEPTION_CLASS.fullmatch(candidate_exception_type) is not None
        else None
    )

    traceback = error.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    if traceback is None:
        return exception_type, None

    basename = Path(traceback.tb_frame.f_code.co_filename).name
    if _STACK_BASENAME.fullmatch(basename) is None:
        return exception_type, None
    return exception_type, f"{basename}:{traceback.tb_lineno}"
