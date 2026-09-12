"""The MCP tool registry: the eleven I-84 tools, generated (I-85, P-55).

The registry is a projection of the shared operation table, not a second
inventory: a tool's name, input schema and output schema are the
operation's own, so a tool cannot describe an argument the REST route
would reject or a result the authority never returns. The one
hand-written argument is `idempotency_key` (I-27, I-85: an argument on
the eight mutations, forbidden on the three reads, a header over REST).
Each `register_calls` handler runs its REST route's sequence over the
same shared translation and the same P-29 writer gate.

P-56's protocol-fault half also lives here: `screen_frame` refuses every
fault I-88 names on the admitted frame, and `jsonrpc_error` maps any of
them — and every I-71 admission refusal — to a JSON-RPC error object.
Both are called by the P-53 wrapper, *before* the SDK parses the frame,
because the SDK cannot express the split: its `@server.call_tool()`
wrapper converts every handler exception into a tool result
(`mcp/server/lowlevel/server.py:589-590`), so the error object I-88
requires for an unidentified operation can only be produced on Cairn's
side of the SDK. `ACTOR_STATE_KEY` lives here rather than in
`framing.py` because the wrapper imports this module and the two cannot
import each other.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import anyio
import anyio.to_thread
from mcp.server.lowlevel import Server
from mcp.types import INVALID_PARAMS as JSONRPC_INVALID_PARAMS
from mcp.types import INVALID_REQUEST as JSONRPC_INVALID_REQUEST
from mcp.types import METHOD_NOT_FOUND as JSONRPC_METHOD_NOT_FOUND
from mcp.types import PARSE_ERROR as JSONRPC_PARSE_ERROR
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    InitializedNotification,
    InitializeRequest,
    JSONRPCNotification,
    JSONRPCRequest,
    ListToolsRequest,
    PingRequest,
    TextContent,
    Tool,
)
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.json_schema import JsonSchemaMode, models_json_schema
from starlette.requests import Request

from cairn.administration.audit_read import read_audit_events
from cairn.administration.commands import CairnAdministration
from cairn.authority.gate import (
    INTERNAL_ERROR_MESSAGE,
    INVALID_REQUEST_MESSAGE,
    Actor,
)
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.transactions import (
    CatalogueContention,
    CatalogueTransactions,
    FailureCode,
    MutationOutcome,
    Rejected,
    RetryClass,
    StableFailure,
    contention_failure,
)
from cairn.runtime.logging import Operation, OutcomeCode
from cairn.screening import SecretScreen
from cairn.transports.rest.middleware import (
    OPERATION_STATE_KEY,
    OUTCOME_STATE_KEY,
)
from cairn.transports.v1.auth import screen_addressing
from cairn.transports.v1.operations import OPERATIONS, OperationEntry
from cairn.transports.v1.parsing import WireRejection, validation_rejection
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
from cairn.transports.v1.responses import (
    AUDIT_EVENT_REF,
    AUDIT_EVENT_SCHEMA,
    AUDIT_EVENT_SCHEMA_NAME,
    InstanceResult,
)
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
from cairn.transports.v1.wire import (
    CONTRACT_IDENTITY,
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_INVALID_VALUE,
    RULE_MALFORMED_JSON,
    RULE_METHOD_NOT_ALLOWED,
    RULE_MISSING_FIELD,
    FailureEnvelope,
    WireModel,
    encode_uuid,
    failure_envelope,
    invalid_request_envelope,
)

# Where the authenticated ``Actor`` is left for the tool handlers. A
# handler that authenticated again would read the catalogue twice and
# could append a second denial for one request.
#
# Here rather than in ``framing.py``, which writes it, because Task 9 made
# the wrapper import this module for ``screen_frame`` and ``jsonrpc_error``
# and the two must not import each other.
ACTOR_STATE_KEY = "actor"

# The I-27 key as MCP presents it. The wording is the OpenAPI header
# parameter's, because it is the same rule wearing a different transport's
# clothes: same canonical spelling, same replay behaviour.
IDEMPOTENCY_KEY_ARGUMENT = "idempotency_key"

_IDEMPOTENCY_KEY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "A canonical lowercase hyphenated UUID. Replaying one returns the "
        "original receipt with outcome 'replayed'."
    ),
}

# ``instance`` takes no arguments (P-55), spelled as the *strict* empty
# object: the one operation with no arguments must not be the one that
# quietly accepts anything.
_NO_ARGUMENTS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_DEFS_TEMPLATE = "#/$defs/{model}"


def _self_contained(model: type[BaseModel], mode: JsonSchemaMode) -> dict[str, Any]:
    """One model's schema with every reference resolvable inside it.

    A tool schema travels alone, with no surrounding document to resolve a
    reference against. Hence ``models_json_schema`` rather than
    ``model_json_schema``, which raises ``KeyError`` on a reference it did
    not generate — the hand-authored audit document — and hence carrying
    that document in ``$defs`` when, and only when, it is referenced.
    """
    key_map, definitions = models_json_schema(
        [(model, mode)], ref_template=_DEFS_TEMPLATE
    )
    name = str(key_map[(model, mode)]["$ref"]).rsplit("/", 1)[1]
    defs: dict[str, Any] = dict(definitions["$defs"])
    schema: dict[str, Any] = defs.pop(name)
    if AUDIT_EVENT_REF in json.dumps(schema):
        defs[AUDIT_EVENT_SCHEMA_NAME] = AUDIT_EVENT_SCHEMA
    return {**schema, "$defs": defs} if defs else schema


def input_schema(entry: OperationEntry) -> dict[str, Any]:
    """The tool's arguments: the I-71 request body, plus the I-27 key on a
    mutation."""
    if entry.request is None:
        return dict(_NO_ARGUMENTS)
    schema = _self_contained(entry.request, "validation")
    if not entry.mutation:
        return schema
    # A copy per tool rather than a shared mutation of the model's own
    # schema dictionary, which Pydantic caches and hands out again.
    properties = {
        **schema["properties"],
        IDEMPOTENCY_KEY_ARGUMENT: dict(_IDEMPOTENCY_KEY_SCHEMA),
    }
    required = [*schema.get("required", []), IDEMPOTENCY_KEY_ARGUMENT]
    return {**schema, "properties": properties, "required": required}


def output_schema(entry: OperationEntry) -> dict[str, Any]:
    """The tool's result: the I-72 envelope for a mutation, the bare result
    body for a read — the same two shapes the REST 200 carries.

    Serialisation mode, because this describes what Cairn emits rather than
    what it would accept.
    """
    return _self_contained(entry.success, "serialization")


def build_tool(entry: OperationEntry) -> Tool:
    return Tool(
        name=entry.tool,
        description=entry.summary,
        inputSchema=input_schema(entry),
        outputSchema=output_schema(entry),
    )


# Built once at import: the schemas are a pure function of the frozen
# models, so a per-request rebuild would return the same eleven objects at
# a cost paid on every ``tools/list``.
TOOLS: tuple[Tool, ...] = tuple(build_tool(entry) for entry in OPERATIONS)


def register_tools(server: Server) -> None:
    """Advertises the eleven tools on the low-level server."""

    # The SDK's registration decorators carry no annotations, so strict
    # mypy cannot see through them; narrow codes rather than a module-wide
    # override, as ``mount.py`` does for the same reason.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        return list(TOOLS)


# The names ``tools/call`` may address, read from the advertised registry
# rather than restated: a tool the server does not advertise cannot be one
# the screen admits, and the two cannot drift.
TOOL_NAMES: frozenset[str] = frozenset(tool.name for tool in TOOLS)

TOOLS_CALL_METHOD = "tools/call"

# P-57's operation label, per tool. Derived rather than transcribed: I-84
# names the tools with the operation vocabulary verbatim, differing only
# in that wire names are kebab-case and ``Operation`` members are
# snake_case. A tool with no matching member raises here, at import, which
# is a broken build rather than an unlabelled metric discovered in
# production.
OPERATION_BY_TOOL: dict[str, Operation] = {
    tool.name: Operation(tool.name.replace("-", "_")) for tool in TOOLS
}

# The methods Cairn serves. The three protocol methods are I-88's,
# ``tools/call`` is the surface itself, and ``notifications/initialized``
# is the handshake's second half — I-84 excludes *server-initiated*
# notifications, and refusing this client-initiated one would refuse the
# ``initialize`` sequence of every conforming client. Anything else is an
# unknown method.
ADMITTED_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        TOOLS_CALL_METHOD,
        "notifications/initialized",
    }
)

# A frame Cairn will consider at all: a Request or a Notification. The
# SDK's own models, so the validation this performs and the validation the
# SDK performs on the same bytes cannot disagree.
#
# Narrower than the SDK's ``JSONRPCMessage`` by one deliberate step: a
# Response or an Error frame is refused here, where the SDK would answer
# 202. Cairn never sends a request — I-84 admits no server-initiated
# messages — so a client response answers nothing, identifies no method
# and reaches no operation.
_CLIENT_FRAME: TypeAdapter[JSONRPCRequest | JSONRPCNotification] = TypeAdapter(
    JSONRPCRequest | JSONRPCNotification
)

# The frame whose identifier a refusal may echo. A Request alone: a
# Notification carries none by definition.
_REQUEST_FRAME: TypeAdapter[JSONRPCRequest] = TypeAdapter(JSONRPCRequest)

# Each admitted method's own SDK model, split by the frame kind that may
# carry it. The SDK validates an incoming Request against
# ``types.ClientRequest`` (``mcp/shared/session.py:362``) and a
# Notification against ``types.ClientNotification`` (``session.py:399``),
# unions whose members discriminate by their ``method`` literal — so
# validating the admitted method's member here is validating what the SDK
# will validate, and a frame this screen passes cannot fail there. That
# matters because the SDK's failure is not silent: see ``screen_frame``.
#
# Two maps rather than one because the kinds do not overlap and the SDK
# branches on the kind first: ``ping`` exists only as a request,
# ``notifications/initialized`` only as a notification. A method carried
# by the wrong kind — ``initialize`` without an identifier, the
# initialized notification with one — would pass a single method-keyed
# map and then fail the SDK's kind-specific union, which is the leak the
# maps exist to close.
_REQUEST_MODEL_BY_METHOD: dict[str, type[BaseModel]] = {
    "initialize": InitializeRequest,
    "ping": PingRequest,
    "tools/list": ListToolsRequest,
    TOOLS_CALL_METHOD: CallToolRequest,
}

_NOTIFICATION_MODEL_BY_METHOD: dict[str, type[BaseModel]] = {
    "notifications/initialized": InitializedNotification,
}


def frame_request_id(document: dict[str, object]) -> str | int | None:
    """The identifier a refusal echoes, or ``None`` where JSON-RPC 2.0 §5
    requires null.

    §5 reserves null for the server that *cannot determine* the request
    identifier — a parse error, or a frame that is not a conforming
    Request object. A frame that validates as a Request has a perfectly
    determinable identifier, and discarding it makes the refusal
    unroutable by the very client it answers: the pinned SDK's
    ``JSONRPCError`` model types ``id`` as ``str | int``
    (``mcp/types.py``), so an error object carrying ``id: null`` in reply
    to an identified request fails the client's own frame validation
    (Val's gate review of ``5b6a5ec``, 11 August 2026).
    """
    try:
        return _REQUEST_FRAME.validate_python(document).id
    except ValidationError:
        return None


def screen_frame(
    document: dict[str, object], *, tool_names: frozenset[str] = TOOL_NAMES
) -> None:
    """P-56's protocol faults on an admitted frame, or ``WireRejection``.

    Called by the P-53 wrapper after ``admit_body`` and before the SDK,
    which is the only place these can be refused as JSON-RPC error
    objects: the SDK's ``@server.call_tool()`` wrapper converts every
    exception a handler raises into a tool result
    (``mcp/server/lowlevel/server.py:589-590``), so an unknown tool name
    discovered at dispatch can no longer be given the shape I-88 requires.

    Screening the frame here also closes two leaks in the pinned SDK, and
    the second is why the screen validates per method rather than stopping
    at the frame shape. By inspection of ``streamable_http.py:499-507`` a
    frame that fails ``JSONRPCMessage`` validation is answered with
    ``f"Validation error: {str(e)}"`` — a Pydantic error string with the
    offending input values interpolated into it, unbounded up to the 2 MiB
    cap. And by inspection of ``mcp/shared/session.py:362-383``, a frame
    that passes as a Request but fails the SDK's *method-specific*
    ``ClientRequest`` validation is logged at warning level through the
    same interpolated-values string — an I-32 leak into the operational
    log that no wire assertion can see (Val's gate review of ``5b6a5ec``,
    11 August 2026). The notification branch is worse still: by
    inspection of ``session.py:428-432`` a failed ``ClientNotification``
    validation logs the *entire frame* — ``f"... Message was:
    {message.message.root}"`` — at the same level. ``validation_rejection``
    reads only the error's type and location, never its input, so every
    such frame is refused here with a rule identity and a field path and
    nothing of the caller's content.
    """
    try:
        frame = _CLIENT_FRAME.validate_python(document)
    except ValidationError as error:
        raise validation_rejection(error) from None

    method = frame.method
    if method not in ADMITTED_METHODS:
        raise WireRejection(400, RULE_METHOD_NOT_ALLOWED, "method")

    # The kind ruling is the parsed frame's, not a hand check on ``id``:
    # ``isinstance`` on the union member is exactly how the SDK branches
    # (``session.py:360`` and ``:397``), so the two cannot classify one
    # frame differently. An admitted method on the wrong kind does not
    # exist on that channel and is refused as the unknown method it is
    # there — reaching the SDK instead, it would fail the kind's union
    # and be logged with the caller's content.
    models = (
        _REQUEST_MODEL_BY_METHOD
        if isinstance(frame, JSONRPCRequest)
        else _NOTIFICATION_MODEL_BY_METHOD
    )
    if (model := models.get(method)) is None:
        raise WireRejection(400, RULE_METHOD_NOT_ALLOWED, "method")

    if method == TOOLS_CALL_METHOD:
        params = document.get("params")
        if params is None:
            raise WireRejection(400, RULE_MISSING_FIELD, "params")
        if type(params) is not dict:
            raise WireRejection(400, RULE_INVALID_VALUE, "params")

        name = params.get("name")
        if name is None:
            raise WireRejection(400, RULE_MISSING_FIELD, "params.name")
        if type(name) is not str or name not in tool_names:
            raise WireRejection(400, RULE_INVALID_VALUE, "params.name")

        # Absent is admitted: the SDK reads ``req.params.arguments or {}``
        # (``server.py:530``), and the eight mutations still refuse the
        # missing I-27 key at their own handler. *Present* and not an
        # object is P-56's named fault — and an explicit ``null`` is
        # present, not absent. The previous ``is not None`` guard
        # conflated the two and admitted it (Val's gate review, finding 3).
        if "arguments" in params and type(params["arguments"]) is not dict:
            raise WireRejection(400, RULE_INVALID_VALUE, "params.arguments")

    # The admitted method's own SDK model, so the kind-specific validation
    # the SDK runs next cannot fail — and therefore cannot log. After the
    # hand checks above, which own the pinned rule identities and field
    # paths for the faults I-88 names; what remains for this to catch is
    # the long tail the hand checks never see, ``_meta`` and the protocol
    # params of the four non-tool methods.
    #
    # One fixed refusal rather than ``validation_rejection``'s reading of
    # the error, deliberately: the SDK's field names are camelCase, and
    # I-71's field-path vocabulary is snake_case — its rendering drops
    # anything CapWords as a union tag, so ``protocolVersion`` would reach
    # the caller as the mangled ``params.str``. No Cairn operation was
    # identified and no conforming client sends these params malformed;
    # naming the params object whole keeps the contract's path vocabulary
    # closed and the refusal honest.
    try:
        model.model_validate(document)
    except ValidationError:
        raise WireRejection(400, RULE_INVALID_VALUE, "params") from None


# The I-71 admission rules ``admit_body`` raises, by fault class. The
# split is JSON-RPC 2.0 §5.1's: ``-32700`` could not be parsed, ``-32600``
# parsed but is not a Request object. Duplicate keys and non-finite
# literals are parse failures because Cairn's hooks refuse them inside
# ``json.loads``.
JSONRPC_CODE_BY_RULE: dict[str, int] = {
    RULE_INVALID_CONTENT_TYPE: JSONRPC_INVALID_REQUEST,
    RULE_BODY_TOO_LARGE: JSONRPC_INVALID_REQUEST,
    RULE_INVALID_ENCODING: JSONRPC_PARSE_ERROR,
    RULE_MALFORMED_JSON: JSONRPC_PARSE_ERROR,
    RULE_DUPLICATE_JSON_KEY: JSONRPC_PARSE_ERROR,
    RULE_BODY_NOT_OBJECT: JSONRPC_INVALID_REQUEST,
}

_PARAMS_FIELD_ROOT = "params"


def jsonrpc_error(
    rejection: WireRejection,
    correlation_id: UUID,
    *,
    request_id: str | int | None,
) -> dict[str, object]:
    """P-56's single mapping function: any protocol fault at this endpoint
    as a JSON-RPC error object.

    ``data`` is the identical ``invalid_request`` envelope REST returns,
    built by the shared constructor, so a caller reading either transport
    reads one rule identity and one field path for one fault.

    The code is the SDK's standard constant for the *fault class*, never
    reinterpreted per operation (I-88). Three classes reach here, and the
    field path is what distinguishes the last two: an admission rule is
    keyed by name above, an unrecognised method is ``METHOD_NOT_FOUND``,
    and anything naming a ``params`` field is ``INVALID_PARAMS``. Whatever
    remains describes a frame that is not a conforming Request object,
    which is ``INVALID_REQUEST``.

    ``request_id`` is ``frame_request_id``'s ruling and is required rather
    than defaulted, for P-57's reason: a default of ``None`` here would
    silently discard every identified caller's identifier again the first
    time a call site forgot it. It is null exactly when JSON-RPC 2.0 §5
    requires null — a frame refused before a Request was identified — and
    the caller's own otherwise. The pinned SDK sends ``"server-error"``
    (``streamable_http.py:339``), which a client correlating by identifier
    could collide with a real request; I-88 fixes the *code* to the SDK's
    standard one, not the envelope.
    """
    if (code := JSONRPC_CODE_BY_RULE.get(rejection.rule)) is None:
        if rejection.rule == RULE_METHOD_NOT_ALLOWED:
            code = JSONRPC_METHOD_NOT_FOUND
        elif rejection.field_path.split(".", 1)[0] == _PARAMS_FIELD_ROOT:
            code = JSONRPC_INVALID_PARAMS
        else:
            code = JSONRPC_INVALID_REQUEST
    envelope = invalid_request_envelope(
        field_path=rejection.field_path,
        rule=rejection.rule,
        correlation_id=correlation_id,
    )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": INVALID_REQUEST_MESSAGE,
            "data": envelope.model_dump(mode="json", exclude_none=True),
        },
    }


# ``idempotency_key_duplicated`` — REST's rule for two headers — has no
# MCP counterpart: a JSON object cannot carry one key twice past
# ``admit_body``'s duplicate-key refusal (I-71). The rule stays in the
# closed vocabulary because it is REST's, not because this transport can
# reach it.
#
# The 400s the ``WireRejection``s below carry are inert here: I-88 keeps
# the I-73 status table out of this transport and nothing on the MCP path
# reads ``.status``. What is used is the rule identity and field path.
def _idempotency_key(arguments: dict[str, object]) -> tuple[UUID, dict[str, object]]:
    """I-85: the key is an argument on a mutation, and is not part of the
    request body the shared model validates.

    The round-trip comparison is ``require_idempotency_key``'s: uppercase,
    braced, URN-prefixed and unhyphenated spellings all parse as UUIDs but
    do not reproduce themselves, so all are ``idempotency_key_malformed``.
    """
    if IDEMPOTENCY_KEY_ARGUMENT not in arguments:
        raise WireRejection(400, RULE_IDEMPOTENCY_KEY_MISSING, IDEMPOTENCY_KEY_ARGUMENT)
    submitted = arguments[IDEMPOTENCY_KEY_ARGUMENT]
    body = {
        name: value
        for name, value in arguments.items()
        if name != IDEMPOTENCY_KEY_ARGUMENT
    }
    if type(submitted) is not str:
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_MALFORMED, IDEMPOTENCY_KEY_ARGUMENT
        )
    try:
        parsed = UUID(submitted)
        if str(parsed) != submitted:
            raise ValueError
    except ValueError:
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_MALFORMED, IDEMPOTENCY_KEY_ARGUMENT
        ) from None
    return parsed, body


def forbid_idempotency_key(arguments: dict[str, object]) -> None:
    """I-27's read rule as I-85 states it for MCP: the key is forbidden on
    the three read tools, with the rule identity REST uses for the header.

    The schemas refuse it too, but a schema refusal is the SDK's to make
    and P-55 takes input validation away from the SDK precisely so that
    the rule identity is Cairn's. Called by all three read handlers ahead
    of their model validation, the order ``routes.py`` gives the same two
    rules.
    """
    if IDEMPOTENCY_KEY_ARGUMENT in arguments:
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_FORBIDDEN, IDEMPOTENCY_KEY_ARGUMENT
        )


# P-57's outcome dimension, for a transport whose status line cannot
# carry it. An identified tool answers HTTP 200 whatever it decided
# (I-88), so the middleware's status reading would file every MCP failure
# — an authorisation denial, a secret rejection, an internal error — in
# the success series. This table is what the adapter signals instead.
#
# It must agree with REST's answer for every code, and
# ``test_the_two_transports_label_one_failure_code_alike`` asserts exactly
# that against ``STATUS_BY_FAILURE_CODE``, one code at a time. The table
# is not imported from REST and does not consult it: I-88 keeps the I-73
# status table REST's alone, and P-50 rejects the cross-transport import.
# Two mappings that must agree, with a test that fails when they stop, is
# the shape this constraint allows.
#
# ``internal_error`` maps to ``unavailable`` rather than to the
# identically named ``OutcomeCode.INTERNAL_ERROR``, which reads wrong
# until you see why: REST reaches it through a 500, and 500 is
# ``unavailable`` in the coarse observability vocabulary. Splitting one
# failure across two series by transport is the confusion this dimension
# exists to prevent. ``OutcomeCode.INTERNAL_ERROR`` stays what it already
# was — the middleware's own label for a response it could not complete.
OUTCOME_BY_FAILURE_CODE: dict[FailureCode, OutcomeCode] = {
    FailureCode.INVALID_REQUEST: OutcomeCode.INVALID_REQUEST,
    FailureCode.AUTHENTICATION_FAILED: OutcomeCode.INVALID_REQUEST,
    FailureCode.AUTHORISATION_DENIED: OutcomeCode.INVALID_REQUEST,
    FailureCode.SECRET_REJECTED: OutcomeCode.INVALID_REQUEST,
    FailureCode.NOT_FOUND: OutcomeCode.INVALID_REQUEST,
    FailureCode.IDEMPOTENCY_CONFLICT: OutcomeCode.INVALID_REQUEST,
    FailureCode.INDEX_PENDING: OutcomeCode.UNAVAILABLE,
    FailureCode.STALE_INDEX: OutcomeCode.UNAVAILABLE,
    FailureCode.DEPENDENCY_UNAVAILABLE: OutcomeCode.UNAVAILABLE,
    FailureCode.INSTANCE_MISMATCH: OutcomeCode.INVALID_REQUEST,
    FailureCode.INTERNAL_ERROR: OutcomeCode.UNAVAILABLE,
}


def outcome_of(result: CallToolResult) -> OutcomeCode:
    """The observability label for a finished tool call.

    Read back off the result the caller receives rather than threaded
    out of each handler beside it: the eleven handlers reach this dispatch
    by a dozen paths, and a label passed alongside the result is a second
    thing every one of them must remember to get right. The text block is
    the disclosed document (I-85), so reading it is reading what the
    caller was told — not a second source of truth about the outcome.
    """
    if not result.isError:
        return OutcomeCode.SUCCESS
    block = result.content[0]
    assert type(block) is TextContent
    document = json.loads(block.text)
    return OUTCOME_BY_FAILURE_CODE[FailureCode(document["failure"]["code"])]


def _canonical_json(payload: object) -> str:
    """The text block's serialisation, and never prose (I-85)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def success_result(body: WireModel) -> CallToolResult:
    """I-85's success shape: the I-72 object as ``structuredContent``, and
    exactly one text block carrying the same object serialised, for
    clients that ignore structured output.

    A fully constructed ``CallToolResult`` rather than a dict, for P-55's
    reason: the SDK's normalisation path cannot express the failure shape
    below, so both shapes are built here and the handler short-circuits it
    either way.
    """
    payload = body.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=_canonical_json(payload))],
        structuredContent=payload,
        isError=False,
    )


def failure_result(envelope: FailureEnvelope) -> CallToolResult:
    """I-88's failure shape: ``isError``, one canonical-JSON text block,
    and **no** ``structuredContent``.

    The absence is load-bearing. By inspection of
    ``mcp/server/lowlevel/server.py:566-570`` the SDK replaces a result
    with no structured content by its own error text whenever the tool
    declares an ``outputSchema``, which all eleven do; returning the
    result object directly short-circuits that at ``:546-547``.
    """
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=_canonical_json(
                    envelope.model_dump(mode="json", exclude_none=True)
                ),
            )
        ],
        structuredContent=None,
        isError=True,
    )


def rejection_result(rejection: WireRejection, correlation_id: UUID) -> CallToolResult:
    """An I-71 refusal inside an identified operation.

    P-56: every outcome of an identified operation is a tool result, not a
    JSON-RPC error object. The body is the shared constructor's, so the
    rule identity and field path are the strings REST returns.
    """
    return failure_result(
        invalid_request_envelope(
            field_path=rejection.field_path,
            rule=rejection.rule,
            correlation_id=correlation_id,
        )
    )


# What ``call_tool`` names when the correlation stamp itself could not be
# read. The nil UUID rather than a fresh one: a minted identifier would
# claim a request identity that was never established, where nil is
# greppable and plainly means "none survived". Reaching it is a broken
# build being reported safely, not a request outcome.
NIL_CORRELATION_ID = UUID(int=0)


def internal_error_result(correlation_id: UUID) -> CallToolResult:
    """The safe envelope for anything the adapter caught and cannot
    describe (P-55's totality rule).

    By inspection of ``mcp/server/lowlevel/server.py:589-590`` an escaping
    exception becomes ``_make_error_result(str(e))``, an I-32 leak from
    any exception whose text quotes a scope path, a fact body or a query.
    Nothing escapes, so nothing is stringified.
    """
    return failure_result(
        failure_envelope(
            StableFailure(
                code=FailureCode.INTERNAL_ERROR,
                safe_message=INTERNAL_ERROR_MESSAGE,
                correlation_id=correlation_id,
                retry=RetryClass.NEVER,
            )
        )
    )


# The audit identity of each mutation tool. These are the strings the
# hash chain records and I-90 requires them to be the REST route's: a
# divergence would be false provenance on the chain rather than a wire
# difference a caller could notice. Restated rather than read from the
# shared table, whose contents P-51 pinned to hold no transport
# mechanics; a test holds them against the literals ``routes.py`` passes.
MUTATION_ACTIONS: dict[str, tuple[str, ActionKind]] = {
    "ingest": ("ingest", ActionKind.DATA),
    "promote": ("promote", ActionKind.DATA),
    "invalidate": ("invalidate", ActionKind.DATA),
    "create-principal": ("create-principal", ActionKind.ADMINISTRATION),
    "issue-credential": ("issue-credential", ActionKind.ADMINISTRATION),
    "revoke-credential": ("revoke-credential", ActionKind.ADMINISTRATION),
    "create-grant": ("create-grant", ActionKind.ADMINISTRATION),
    "revoke-grant": ("revoke-grant", ActionKind.ADMINISTRATION),
}


# The same, for the two reads whose boundary denials name an operation.
# ``audit-read`` is deliberately not the tool name. ``instance`` is absent
# because it takes no arguments and so has no addressing field to deny
# over; its REST code belongs to authentication, which happens at the
# mount under ``mcp-frame`` (P-54).
READ_ACTIONS: dict[str, tuple[str, ActionKind]] = {
    "read-audit-events": ("audit-read", ActionKind.ADMINISTRATION),
    "retrieve": ("retrieve", ActionKind.DATA),
}

# The one read with no arguments, named because the dispatch branches on
# it.
INSTANCE_TOOL = "instance"


class _NoArguments(WireModel):
    """The empty object ``instance``'s input schema advertises, as a model.

    A model rather than a hand-written check, so that P-55's refusal of a
    stray argument comes from the one validator: ``extra="forbid"`` yields
    ``unknown_field`` with the offending name, exactly as it does for a
    stray argument to ``ingest``.
    """


@dataclass(frozen=True, slots=True)
class _Mutation:
    """One mutation tool's half of ``run_mutation``'s keyword arguments.

    ``routes.py`` keeps these generic because each route is its own call
    site; a table keyed by tool name cannot, since the eight entries have
    eight different command and value types. The erasure is confined to
    this dataclass and the runner below it.
    """

    translate: Callable[[dict[str, object]], Any]
    invoke: Callable[[Actor, Any, UUID, UUID], MutationOutcome[Any]]
    render: Callable[[Any], WireModel]


@dataclass(frozen=True, slots=True)
class _Read:
    """One argument-carrying read tool, in the same erased shape.

    ``gated`` is ``routes.py:340-351``'s ruling rather than a choice made
    here: the audit read takes the P-29 gate because a successful read
    appends its own event, while retrieval does not, since queueing every
    read would serialise the operation retrieval exists to make fast.
    """

    translate: Callable[[dict[str, object]], Any]
    invoke: Callable[[Actor, Any, UUID], Any]
    render: Callable[[Any], WireModel]
    gated: bool


ToolHandler = Callable[[dict[str, object], Actor, UUID], Awaitable[CallToolResult]]


def register_calls(
    server: Server,
    *,
    authority: CairnAuthority,
    administration: CairnAdministration,
    transactions: CatalogueTransactions,
    screen: SecretScreen,
    data_path: Path,
    write_gate: anyio.Lock,
    clock: Callable[[], datetime],
    instance_id: UUID,
    product_version: str,
    contract_digest: str,
    mcp_contract_digest: str,
) -> None:
    """Registers ``tools/call`` for the eleven I-84 operations.

    ``validate_input=False`` is P-55's first pinned mechanic. By
    inspection of ``mcp/server/lowlevel/server.py:534-538`` the SDK's
    default returns ``_make_error_result(f"Input validation error:
    {e.message}")`` — jsonschema prose with the offending value quoted
    into it, which is neither the I-72 envelope nor safe under I-32. Cairn
    validates with the same strict models REST validates with.
    """

    mutations: dict[str, _Mutation] = {
        "ingest": _Mutation(
            lambda body: ingest_command(validated(IngestRequest, body)),
            lambda actor, command, key, cid: authority.ingest(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            ingest_result,
        ),
        "promote": _Mutation(
            lambda body: promote_command(validated(PromoteRequest, body)),
            lambda actor, command, key, cid: authority.promote(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            promote_result,
        ),
        "invalidate": _Mutation(
            lambda body: invalidate_command(validated(InvalidateRequest, body)),
            lambda actor, command, key, cid: authority.invalidate(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            invalidate_result,
        ),
        "create-principal": _Mutation(
            lambda body: create_principal_command(
                validated(CreatePrincipalRequest, body)
            ),
            lambda actor, command, key, cid: administration.create_principal(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            create_principal_result,
        ),
        "issue-credential": _Mutation(
            lambda body: issue_credential_command(
                validated(IssueCredentialRequest, body)
            ),
            lambda actor, command, key, cid: administration.issue_credential(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            issue_credential_result,
        ),
        "revoke-credential": _Mutation(
            lambda body: revoke_credential_command(
                validated(RevokeCredentialRequest, body)
            ),
            lambda actor, command, key, cid: administration.revoke_credential(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            revoke_credential_result,
        ),
        "create-grant": _Mutation(
            lambda body: create_grant_command(validated(CreateGrantRequest, body)),
            lambda actor, command, key, cid: administration.create_grant(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            create_grant_result,
        ),
        "revoke-grant": _Mutation(
            lambda body: revoke_grant_command(validated(RevokeGrantRequest, body)),
            lambda actor, command, key, cid: administration.revoke_grant(
                actor, command, idempotency_key=key, correlation_id=cid
            ),
            revoke_grant_result,
        ),
    }

    async def run_mutation(
        name: str,
        mutation: _Mutation,
        arguments: dict[str, object],
        actor: Actor,
        correlation_id: UUID,
    ) -> CallToolResult:
        """``routes.py:189-230``'s sequence, with MCP at both ends.

        Order is I-87's and the REST route's exactly: the key and the
        model first, then — inside the writer gate, on the worker thread —
        the addressing screen, then the application. Screening after
        validation lets the screen name a field path the caller would
        recognise; screening inside the gate keeps a maximum screened
        request from being interleaved with a write.
        """
        action_code, action_kind = MUTATION_ACTIONS[name]
        try:
            idempotency_key, body = _idempotency_key(arguments)
            command = mutation.translate(body)
        except WireRejection as rejection:
            return rejection_result(rejection, correlation_id)

        def execute() -> MutationOutcome[Any]:
            denied = screen_addressing(
                body,
                screen=screen,
                actor=actor,
                transactions=transactions,
                data_path=data_path,
                action_code=action_code,
                action_kind=action_kind,
                correlation_id=correlation_id,
            )
            if denied is not None:
                return denied
            return mutation.invoke(actor, command, idempotency_key, correlation_id)

        async with write_gate:
            result = await anyio.to_thread.run_sync(execute)
        if isinstance(result, Rejected):
            return failure_result(failure_envelope(result.failure))
        # The I-72 envelope, not the bare result body: a mutation answers
        # inside it on both surfaces (I-85), and it is the shared
        # constructor's so the two cannot render one receipt two ways.
        return success_result(success_envelope(result, mutation.render(result.value)))

    reads: dict[str, _Read] = {
        "read-audit-events": _Read(
            lambda body: read_audit_events_command(
                validated(ReadAuditEventsRequest, body)
            ),
            lambda actor, command, cid: read_audit_events(
                data_path,
                transactions,
                actor,
                command,
                correlation_id=cid,
                clock=clock,
            ),
            read_audit_events_result,
            gated=True,
        ),
        "retrieve": _Read(
            lambda body: retrieve_command(validated(RetrieveRequest, body)),
            lambda actor, command, cid: authority.retrieve(
                actor, command, correlation_id=cid
            ),
            retrieve_result,
            gated=False,
        ),
    }

    async def run_read(
        name: str,
        read: _Read,
        arguments: dict[str, object],
        actor: Actor,
        correlation_id: UUID,
    ) -> CallToolResult:
        """``routes.py:289-336`` and ``:338-388``'s sequence, with MCP at
        both ends.

        I-87's order: refuse the I-27 key, validate and translate, screen
        every addressing field, then invoke. The arguments object *is* the
        body, so the field paths the screen names are REST's —
        ``scope_prefix[0].identifier``, not an ``arguments.``-prefixed
        variant. The answer is the bare result body: a read commits
        nothing, so it has no receipt to carry.
        """
        action_code, action_kind = READ_ACTIONS[name]
        try:
            forbid_idempotency_key(arguments)
            command = read.translate(arguments)
        except WireRejection as rejection:
            return rejection_result(rejection, correlation_id)

        def execute() -> Any:
            denied = screen_addressing(
                arguments,
                screen=screen,
                actor=actor,
                transactions=transactions,
                data_path=data_path,
                action_code=action_code,
                action_kind=action_kind,
                correlation_id=correlation_id,
            )
            if denied is not None:
                return denied
            return read.invoke(actor, command, correlation_id)

        if read.gated:
            async with write_gate:
                result = await anyio.to_thread.run_sync(execute)
        else:
            result = await anyio.to_thread.run_sync(execute)
        if isinstance(result, Rejected):
            return failure_result(failure_envelope(result.failure))
        return success_result(read.render(result))

    async def run_instance(
        arguments: dict[str, object], correlation_id: UUID
    ) -> CallToolResult:
        """``routes.py:390-413``, less the parts that were HTTP.

        No screen and no worker thread: no addressing fields to walk, and
        the four values were resolved once at startup. What remains is
        the two refusals a caller can provoke — the forbidden I-27 key and
        an argument on a tool that takes none.

        Both digests, per I-89: an MCP caller learning which OpenAPI
        document is answering it and not which manifest describes the
        tools it just called would be the one caller the added field
        exists for.
        """
        try:
            forbid_idempotency_key(arguments)
            validated(_NoArguments, arguments)
        except WireRejection as rejection:
            return rejection_result(rejection, correlation_id)
        return success_result(
            InstanceResult(
                instance_id=encode_uuid(instance_id),
                product_version=product_version,
                contract_identity=CONTRACT_IDENTITY,
                contract_digest=contract_digest,
                mcp_contract_digest=mcp_contract_digest,
            )
        )

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
        # P-55's totality rule admits no preamble: anything read before
        # the guard is a line the SDK stringifies when it raises
        # (``mcp/server/lowlevel/server.py:589-590``). The middleware
        # stamps the correlation identifier before any transport sees the
        # request, so the lookups below cannot fail on any built
        # application — but "cannot fail" was the claim the guard exists
        # to not depend on (Val's gate review, finding 4). Until the stamp
        # is read, the nil identifier is the honest one: no request
        # identity survived, and the envelope says so safely.
        correlation_id = NIL_CORRELATION_ID
        # Bound before the guard so the failure path can still label
        # itself. An assignment of a literal cannot raise, so this is not
        # the preamble P-55 forbids — the rule is about *reading* the
        # request, and nothing is read here.
        signal: dict[str, object] | None = None
        try:
            state = cast(Request, server.request_context.request).scope["state"]
            signal = state
            correlation_id = state["correlation_id"]
            # A tool call reaching here unauthenticated is a wiring fault,
            # not a caller error: P-54 authenticates every frame at the
            # mount. The KeyError lands in the guard below as
            # ``internal_error``, which discloses nothing.
            actor: Actor = state[ACTOR_STATE_KEY]
            # P-57: the mount is one ASGI route, so the operation label
            # cannot come from the route name. Written here, where the
            # tool is first known, and read by the foundation middleware
            # after this returns — the adapter signals rather than
            # emitting, so correlation identity, duration and the safe-log
            # shape stay the middleware's alone.
            state[OPERATION_STATE_KEY] = OPERATION_BY_TOOL[name]
            mutation = mutations.get(name)
            if mutation is not None:
                return _signalled(
                    state,
                    await run_mutation(
                        name, mutation, arguments, actor, correlation_id
                    ),
                )
            read = reads.get(name)
            if read is not None:
                return _signalled(
                    state,
                    await run_read(name, read, arguments, actor, correlation_id),
                )
            if name == INSTANCE_TOOL:
                return _signalled(state, await run_instance(arguments, correlation_id))
            return _signalled(state, _unrouted(correlation_id))
        except CatalogueContention:
            # The P-65 barrier's typed contention (I-49), rendered as the
            # ``dependency_unavailable`` failure this transport's outcome
            # table already carries. Ahead of the bare arm below, which
            # would otherwise funnel a declared retryable condition into
            # ``internal_error`` with the never retry class — the exact
            # transport disagreement I-86 forbids, since REST answers the
            # same contention with its owned 503.
            if signal is not None:
                signal[OUTCOME_STATE_KEY] = OutcomeCode.UNAVAILABLE
            return failure_result(failure_envelope(contention_failure(correlation_id)))
        except Exception:
            # P-55's totality rule. Deliberately bare: an exception this
            # adapter did not anticipate is exactly the one whose text
            # might quote a scope path or a fact body, and the SDK would
            # stringify it into the caller's result.
            #
            # The label is written here too, and not left to the status:
            # this answer is HTTP 200 like every other tool result, so an
            # unlabelled one would be counted a success — the exact defect
            # this signal exists to close, on the one path that means the
            # adapter has already gone wrong.
            if signal is not None:
                signal[OUTCOME_STATE_KEY] = OutcomeCode.UNAVAILABLE
            return internal_error_result(correlation_id)


def _signalled(state: dict[str, object], result: CallToolResult) -> CallToolResult:
    """Writes P-57's outcome label for a finished tool call, and returns
    the result unchanged.

    Wrapping every return rather than labelling once after the dispatch,
    because the dispatch returns from four places and a fifth added later
    would silently go unlabelled — and unlabelled means counted as a
    success, which is the failure mode this exists to prevent.
    """
    state[OUTCOME_STATE_KEY] = outcome_of(result)
    return result


def _unrouted(correlation_id: UUID) -> CallToolResult:
    """A name the registry does not advertise — unreachable since Task 9.

    ``screen_frame`` refuses an unknown tool name at the P-53 wrapper, so
    a name reaching the dispatch below has already been checked against
    ``TOOL_NAMES``. Kept as the safe fallback rather than deleted: the
    branch that would replace it is ``raise``, and the caller of last
    resort for a wiring fault at this seam must still be an envelope that
    discloses nothing, not the SDK stringifying an exception into a tool
    result.
    """
    return internal_error_result(correlation_id)
