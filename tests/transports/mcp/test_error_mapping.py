"""Task 9: the I-88 error split at `/v1/mcp` (P-56).

The decision divides every answer this endpoint can give into two
channels, and the tests here hold the division rather than any single
message.

A **JSON-RPC error object** is reserved for protocol faults — no Cairn
operation was identified, so there is nothing to audit and no authority
outcome to report. A **tool result** carries every outcome of an
identified operation, success and failure alike. The two are asserted
against each other: the failure vocabulary is walked in full to show that
no member of it escapes into the error channel, and each protocol fault is
driven over the wire to show that none of them arrives as a tool result.

Two claims here are about the *pinned SDK* rather than about Cairn, and
both are stated as such because they are what forced Task 9's shape:

- The SDK's ``@server.call_tool()`` decorator cannot emit an error object.
  Its wrapper ends in a blanket ``except Exception`` that returns
  ``_make_error_result(str(e))``
  (``mcp/server/lowlevel/server.py:589-590``), so an unknown tool name
  discovered at dispatch is a tool result no matter what is raised. Cairn
  therefore refuses it at the P-53 wrapper, and
  ``test_an_unknown_tool_name_is_an_error_object_and_names_nothing``
  is what holds that it still reaches the caller in I-88's shape.
- The SDK answers a non-conforming frame with
  ``f"Validation error: {str(e)}"`` (``streamable_http.py:499-507``), a
  Pydantic error string with the caller's own input interpolated into it.
  ``test_a_non_conforming_frame_echoes_none_of_the_callers_content``
  holds that Cairn's screen refuses the frame first, so that string is
  never reached.

Module-local fixtures for the reason ``test_mount_auth.py`` gives: the
``tests`` tree has no package markers, so this package cannot carry a
``conftest.py`` without mypy refusing a second module of that name. Named
``test_error_mapping`` rather than ``test_errors`` for the same reason —
``tests/transports/rest/v1/test_errors.py`` already holds that basename.
"""

import ast
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse
from mcp.types import INVALID_PARAMS as JSONRPC_INVALID_PARAMS
from mcp.types import INVALID_REQUEST as JSONRPC_INVALID_REQUEST
from mcp.types import METHOD_NOT_FOUND as JSONRPC_METHOD_NOT_FOUND
from mcp.types import PARSE_ERROR as JSONRPC_PARSE_ERROR

from cairn.authority.credentials import mint_token
from cairn.authority.gate import INVALID_REQUEST_MESSAGE
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import FailureCode, RetryClass, StableFailure
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.mcp.server import (
    ADMITTED_METHODS,
    TOOL_NAMES,
    failure_result,
    jsonrpc_error,
)
from cairn.transports.v1.parsing import MAX_REQUEST_BYTES, WireRejection
from cairn.transports.v1.wire import (
    RULE_BODY_TOO_LARGE,
    RULE_INVALID_CONTENT_TYPE,
    RULE_METHOD_NOT_ALLOWED,
    failure_envelope,
)

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
CORRELATION_ID = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "cairn"
MCP_SOURCE_ROOT = SOURCE_ROOT / "transports" / "mcp"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_config(tmp_path: Path) -> CairnConfig:
    data_path = tmp_path / "data"
    credentials_path = tmp_path / "credentials"
    data_path.mkdir()
    credentials_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=credentials_path),
    )
    migrate_catalogue(config, lambda: NOW)
    return config


def credential(data_path: Path) -> str:
    """One valid bearer credential, with no grant.

    No grant is needed anywhere in this module: every case here is refused
    before an operation is identified, and a case that reached the
    authority model would be testing something other than the error split.
    """
    timestamp = canonical_timestamp(NOW)
    credential_id = UUID(f"{next(count(1)):08x}-6666-4666-8666-666666666666")
    minted = mint_token(credential_id, lambda size: bytes(range(size)))
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(credential_id), str(PRINCIPAL_ID), minted.verifier, timestamp, None),
        )
        connection.commit()
    return f"Bearer {minted.text}"


@asynccontextmanager
async def running(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client


async def post(
    client: AsyncClient,
    token: str,
    *,
    content: str,
    content_type: str = "application/json",
) -> HTTPXResponse:
    headers = {**MCP_HEADERS, "Content-Type": content_type, "Authorization": token}
    return await client.post(MOUNT_PATH, content=content, headers=headers)


def audit_events(data_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(data_path / CATALOGUE_FILENAME) as connection:
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def assert_error_object(
    response: HTTPXResponse,
    *,
    rule: str,
    code: int,
    request_id: str | int | None = None,
) -> None:
    """I-88's protocol-fault shape, whole.

    Every part is asserted rather than only the rule: the ``data`` member
    carrying the same envelope REST would have carried is the half of P-56
    that a implementation could silently drop while still returning
    something that looks like an error object.
    """
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    # JSON-RPC 2.0 §5: the caller's own identifier where the refused
    # frame validated as a Request, since it is then perfectly
    # determinable — the pinned SDK's ``JSONRPCError`` model refuses
    # ``id: null`` outright, so discarding a known identifier makes the
    # refusal unroutable by the client it answers (Val's gate review,
    # finding 1). Null only where §5 requires it: a frame refused before
    # a Request was identified.
    assert body["id"] == request_id
    error = body["error"]
    assert error["code"] == code
    assert error["message"] == INVALID_REQUEST_MESSAGE
    failure = error["data"]["failure"]
    assert failure["code"] == "invalid_request"
    assert failure["detail"]["rule"] == rule
    assert UUID(failure["correlation_id"])
    # A tool result is the other channel. Neither key may appear.
    assert "result" not in body
    assert "isError" not in body


# --------------------------------------------------------------------------
# The failure vocabulary stays in the tool-result channel
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(FailureCode))
def test_every_failure_code_renders_as_a_tool_result(code: FailureCode) -> None:
    """P-56: "every outcome of an identified operation is a tool result".

    Walked over the enumeration rather than over a list of codes this
    module chose, so a twelfth failure code added later is covered here on
    the day it is added rather than the day someone remembers.

    The shape asserted is I-88's in full: ``isError``, one canonical-JSON
    text block, and no ``structuredContent`` — the absence being the part
    that makes the SDK return the result rather than replacing it with its
    own error text (``mcp/server/lowlevel/server.py:566-570``).
    """
    result = failure_result(
        failure_envelope(
            StableFailure(
                code=code,
                safe_message="refused",
                correlation_id=CORRELATION_ID,
                retry=RetryClass.NEVER,
            )
        )
    )

    assert result.isError is True
    assert result.structuredContent is None
    (block,) = result.content
    assert block.type == "text"
    payload = json.loads(block.text)
    assert payload["failure"]["code"] == code.value
    assert payload["failure"]["correlation_id"] == str(CORRELATION_ID)


def test_the_failure_vocabulary_is_absent_from_the_error_object_mapping() -> None:
    """The other direction of the same split, and the one a reader is more
    likely to doubt: no failure code is reachable through the protocol-fault
    mapping at all.

    ``jsonrpc_error`` maps a ``WireRejection`` — an admission or screen
    refusal — and every envelope it builds is ``invalid_request``. A
    failure code arriving here would mean an identified operation's outcome
    had been routed into the wrong channel.
    """
    rejection = WireRejection(400, RULE_METHOD_NOT_ALLOWED, "method")
    error = jsonrpc_error(rejection, CORRELATION_ID, request_id=None)

    data = error["error"]
    assert type(data) is dict
    failure = data["data"]["failure"]
    assert failure["code"] == FailureCode.INVALID_REQUEST.value
    assert {code.value for code in FailureCode} - {failure["code"]}


# --------------------------------------------------------------------------
# Protocol faults stay in the error-object channel
# --------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("frame", "rule", "code", "request_id"),
    [
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "resources/list"}',
            "method_not_allowed",
            JSONRPC_METHOD_NOT_FOUND,
            1,
            id="unknown-method",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
            ' "params": {"name": "no-such-tool", "arguments": {}}}',
            "invalid_value",
            JSONRPC_INVALID_PARAMS,
            1,
            id="unknown-tool-name",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
            ' "params": {"name": "instance", "arguments": 42}}',
            "invalid_value",
            JSONRPC_INVALID_PARAMS,
            1,
            id="arguments-not-an-object",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
            ' "params": {"name": "instance", "arguments": null}}',
            "invalid_value",
            JSONRPC_INVALID_PARAMS,
            1,
            id="arguments-explicit-null",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "initialize",'
            ' "params": {"protocolVersion": true}}',
            "invalid_value",
            JSONRPC_INVALID_PARAMS,
            1,
            id="method-specific-params",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}',
            "method_not_allowed",
            JSONRPC_METHOD_NOT_FOUND,
            1,
            id="notification-method-as-request",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "method": "ping"}',
            "method_not_allowed",
            JSONRPC_METHOD_NOT_FOUND,
            None,
            id="request-method-as-notification",
        ),
        pytest.param(
            '[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]',
            "body_not_object",
            JSONRPC_INVALID_REQUEST,
            None,
            id="batch",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": ',
            "malformed_json",
            JSONRPC_PARSE_ERROR,
            None,
            id="unparseable-frame",
        ),
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "result": {}}',
            "missing_field",
            JSONRPC_INVALID_REQUEST,
            None,
            id="non-conforming-frame",
        ),
    ],
)
async def test_every_protocol_fault_is_a_json_rpc_error_object(
    tmp_path: Path,
    frame: str,
    rule: str,
    code: int,
    request_id: int | None,
) -> None:
    """P-56's list, each driven over the wire on a running application.

    All five faults the decision names are here — an unparseable frame, a
    non-conforming one, an unknown method, an unknown tool name and a
    non-object ``arguments``, an explicit ``null`` being present and not
    an object rather than absent (Val's gate review, finding 3) — plus a
    batch, which is refused as ``body_not_object`` because a JSON array
    is not an object and ``admit_body`` says so before the SDK's
    last-value-wins parse ever runs, plus the three ways a frame can miss
    the SDK's own method-specific validation: malformed protocol params,
    and an admitted method on the wrong frame kind in either direction.

    The expected identifier rides with each case: the caller's own where
    the frame validated as a Request, null where JSON-RPC 2.0 §5 requires
    it — including the notification-kind case, which has none to echo.

    Authenticated, because an unauthenticated caller is answered 401 by
    P-54 and would prove nothing about the split.
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)

    async with running(build_application(config)) as client:
        response = await post(client, token, content=frame)

    assert_error_object(response, rule=rule, code=code, request_id=request_id)


@pytest.mark.anyio
async def test_an_unknown_tool_name_is_an_error_object_and_names_nothing(
    tmp_path: Path,
) -> None:
    """The fault the SDK cannot render, moved here from ``test_calls.py``.

    Two claims. The shape is I-88's — an error object, because no Cairn
    operation was identified — which the decorator's blanket
    ``except Exception`` (``mcp/server/lowlevel/server.py:589-590``) makes
    impossible to produce at the dispatch, hence the P-53 wrapper. And the
    invented name is echoed nowhere: ``params.name`` is caller-authored
    text, so the answer names the field path and never its value.
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "no-such-tool", "arguments": {}},
    }

    async with running(build_application(config)) as client:
        response = await post(client, token, content=json.dumps(frame))

    assert_error_object(
        response, rule="invalid_value", code=JSONRPC_INVALID_PARAMS, request_id=1
    )
    assert response.json()["error"]["data"]["failure"]["detail"]["field_path"] == (
        "params.name"
    )
    assert "no-such-tool" not in response.text
    # No operation was identified, so there is nothing to audit.
    assert audit_events(config.paths.data) == []


@pytest.mark.anyio
async def test_a_non_conforming_frame_echoes_none_of_the_callers_content(
    tmp_path: Path,
) -> None:
    """The pinned SDK's leak, closed by screening the frame first.

    By inspection of ``streamable_http.py:499-507`` a frame that parses as
    JSON but fails ``JSONRPCMessage`` validation is answered with
    ``f"Validation error: {str(e)}"`` — a Pydantic error string carrying
    the offending input, unbounded up to the I-30 cap. Cairn bounds every
    other reflection of caller text to 128 characters
    (``parsing.py:60``), so the value asserted absent here is one that
    would be plainly visible if the screen were removed.
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)
    secret = "sk-live-should-never-be-echoed"
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "scope_prefix": secret})

    async with running(build_application(config)) as client:
        response = await post(client, token, content=frame)

    assert secret not in response.text
    assert "Validation error" not in response.text
    assert "error" in response.json()


@pytest.mark.anyio
async def test_a_method_specific_fault_leaks_nothing_into_any_log(
    tmp_path: Path,
) -> None:
    """The pinned SDK's second leak, closed by validating per method
    (Val's gate review of ``5b6a5ec``, finding 2).

    By inspection of ``mcp/shared/session.py:380-384`` a frame that
    passes generic JSON-RPC framing but fails the SDK's method-specific
    ``ClientRequest`` validation is logged at warning level as
    ``f"Failed to validate request: {e}"`` — a Pydantic error string with
    the caller's submitted values interpolated into it. The notification
    branch (``session.py:428-432``) logs the entire frame. Neither line
    reaches the wire, so this test captures the logging tree itself: the
    canary must appear in no record at any level, and the caller must get
    the I-88 refusal rather than the SDK's answer.
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)
    canary = "sk-live-should-never-be-logged"
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "initialize",
            "params": {"protocolVersion": {"leak": canary}},
        }
    )

    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = Capture(level=logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        async with running(build_application(config)) as client:
            response = await post(client, token, content=frame)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    for record in captured:
        try:
            rendered = record.getMessage()
        except TypeError:
            # The safe logger's own payloads are not format strings.
            rendered = str(record.msg)
        assert canary not in rendered
    assert canary not in response.text
    assert_error_object(
        response, rule="invalid_value", code=JSONRPC_INVALID_PARAMS, request_id=5
    )


@pytest.mark.anyio
async def test_a_string_identifier_is_echoed_as_itself(tmp_path: Path) -> None:
    """JSON-RPC 2.0 allows string identifiers, and the echo must not
    coerce one: a client correlating by identifier compares exactly."""
    config = make_config(tmp_path)
    token = credential(config.paths.data)
    frame = json.dumps({"jsonrpc": "2.0", "id": "req-17", "method": "resources/list"})

    async with running(build_application(config)) as client:
        response = await post(client, token, content=frame)

    assert_error_object(
        response,
        rule="method_not_allowed",
        code=JSONRPC_METHOD_NOT_FOUND,
        request_id="req-17",
    )


# --------------------------------------------------------------------------
# The three refined statuses, and the one HTTP-layer refusal
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_oversized_and_wrong_media_type_rules_arrive_under_invalid_request(
    tmp_path: Path,
) -> None:
    """I-88's 413 and 415 clause: the refined statuses do not cross over,
    but their *rule identities* do, under ``invalid_request``.

    There is no HTTP status on a JSON-RPC error object, which is the point
    — the caller is told which rule refused it, and ``retry`` rather than a
    status is what says whether to try again (I-26).
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)

    async with running(build_application(config)) as client:
        oversized = await post(client, token, content=" " * (MAX_REQUEST_BYTES + 1))
        wrong_media_type = await post(
            client,
            token,
            content='{"jsonrpc": "2.0", "id": 1, "method": "ping"}',
            content_type="text/plain",
        )

    assert_error_object(
        oversized, rule=RULE_BODY_TOO_LARGE, code=JSONRPC_INVALID_REQUEST
    )
    assert_error_object(
        wrong_media_type,
        rule=RULE_INVALID_CONTENT_TYPE,
        code=JSONRPC_INVALID_REQUEST,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_the_405_rule_is_this_endpoints_own_refusal(
    tmp_path: Path, method: str
) -> None:
    """I-88 maps the 405 rule "to the endpoint's own refusal of ``GET`` and
    ``DELETE``" rather than into the JSON-RPC channel.

    A wrong method carries no frame to answer in kind, so this one stays an
    HTTP response — and RFC 9110 §10.2.1 makes ``Allow`` mandatory on it.
    """
    config = make_config(tmp_path)

    async with running(build_application(config)) as client:
        response = await client.request(method, MOUNT_PATH, headers=MCP_HEADERS)

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    body = response.json()
    assert "jsonrpc" not in body
    assert body["failure"]["code"] == "invalid_request"
    assert body["failure"]["detail"]["rule"] == RULE_METHOD_NOT_ALLOWED


# --------------------------------------------------------------------------
# The I-73 status table does not cross over
# --------------------------------------------------------------------------


def imported_names(source: Path) -> set[str]:
    """Every name a file imports, by parsing rather than grepping — this
    module's own prose names the symbol under test."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if type(node) is ast.ImportFrom:
            names.update(alias.name for alias in node.names)
        elif type(node) is ast.Import:
            names.update(alias.name for alias in node.names)
    return names


def test_the_rest_status_table_is_imported_nowhere_under_the_mcp_adapter() -> None:
    """I-88's "the I-73 status table does not cross over", made mechanical.

    ``STATUS_BY_FAILURE_CODE`` is what turns a failure into an HTTP status,
    and there is no HTTP status on a tool result. An import of it under
    ``transports/mcp/`` would be the first step of rebuilding REST's
    rendering inside a transport that must not have one.

    ``RETRY_AFTER_SECONDS`` is asserted with it for the same reason: I-88
    says ``retry`` is the only retry signal here and no ``Retry-After``
    counterpart is invented.
    """
    sources = sorted(MCP_SOURCE_ROOT.rglob("*.py"))
    assert sources, f"no sources found under {MCP_SOURCE_ROOT}; the scan is vacuous"

    forbidden = {"STATUS_BY_FAILURE_CODE", "RETRY_AFTER_SECONDS"}
    offenders = {
        str(source.relative_to(MCP_SOURCE_ROOT)): sorted(found)
        for source in sources
        if (found := imported_names(source) & forbidden)
    }
    assert offenders == {}


def test_the_scan_detects_the_import_it_forbids(tmp_path: Path) -> None:
    """The detector's teeth. A scan that stopped detecting would pass the
    test above silently for the rest of the slice."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from cairn.transports.rest.v1.errors import STATUS_BY_FAILURE_CODE\n",
        encoding="utf-8",
    )

    assert "STATUS_BY_FAILURE_CODE" in imported_names(offender)


# --------------------------------------------------------------------------
# The admitted surface
# --------------------------------------------------------------------------


def test_the_admitted_methods_are_the_handshake_and_the_surface() -> None:
    """The allow-list, pinned so that widening it is a decision rather than
    an edit.

    ``notifications/initialized`` earns its place: I-84 excludes
    *server-initiated* notifications, and this one is the client's half of
    the ``initialize`` sequence. Refusing it would refuse every conforming
    client while the three protocol methods still appeared to work.
    """
    assert ADMITTED_METHODS == {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "notifications/initialized",
    }


def test_the_screened_tool_names_are_the_advertised_registry() -> None:
    """I-84's eleven, and the screen reads them from the registry rather
    than a second list — asserted here against the operation vocabulary
    transcribed by hand, which is the only way the two can be shown to
    agree."""
    assert TOOL_NAMES == {
        "ingest",
        "promote",
        "invalidate",
        "retrieve",
        "create-principal",
        "issue-credential",
        "revoke-credential",
        "create-grant",
        "revoke-grant",
        "read-audit-events",
        "instance",
    }


@pytest.mark.anyio
async def test_a_conforming_client_handshake_is_admitted_end_to_end(
    tmp_path: Path,
) -> None:
    """The screen's cost, held to zero for a client that behaves.

    The sequence a conforming client actually sends: ``initialize``, then
    the ``notifications/initialized`` acknowledgement, then ``tools/list``,
    then a ``tools/call``. Without this, an allow-list that omitted the
    notification would pass every other test in this module and break the
    first real client to connect.
    """
    config = make_config(tmp_path)
    token = credential(config.paths.data)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    instance = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "instance", "arguments": {}},
    }

    async with running(build_application(config)) as client:
        first = await post(client, token, content=json.dumps(initialize))
        second = await post(client, token, content=json.dumps(initialized))
        third = await post(client, token, content=json.dumps(tools_list))
        fourth = await post(client, token, content=json.dumps(instance))

    assert first.status_code == 200
    assert "error" not in first.json()
    # A notification has no identifier to answer, so the SDK accepts it
    # without a body.
    assert second.status_code == 202
    assert third.status_code == 200
    assert len(third.json()["result"]["tools"]) == len(TOOL_NAMES)
    assert fourth.status_code == 200
    assert fourth.json()["result"]["isError"] is False
