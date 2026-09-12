"""Task 4: raw-frame admission at `/v1/mcp` (I-86, I-88, P-53).

This module mirrors ``tests/transports/v1/test_parsing.py`` clause for
clause. That is the point of it: I-86 says every I-71 admission rule
applies to the MCP endpoint, and I-86 further says "a scenario asserting a
rule identity asserts the same string on both transports". A rule that
REST refuses and MCP does not — or refuses under a different identity — is
the exact divergence Task 13's conformance run exists to catch, and
catching it here is cheaper.

Each refusal asserts three things: the rule identity (the same constant
its REST counterpart asserts), the JSON-RPC code I-88 maps it to, and
that the SDK was never called. The last is what makes this a boundary
rather than a validation: a refused frame must not reach a third-party
parser at all.

The wrapper is driven at the ASGI level rather than over HTTP because
several clauses cannot be expressed through a client — a ``Content-Length``
that lies about the body, invalid UTF-8, a body delivered in several
chunks. ``test_parsing.py`` builds its requests the same way and for the
same reason.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from cairn.authority.credentials import mint_token
from cairn.authority.gate import INVALID_REQUEST_MESSAGE, Actor
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.catalogue.transactions import Rejected
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.framing import (
    FRAME_REJECTION_STATUS,
    FrameAdmission,
)
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.mcp.server import JSONRPC_CODE_BY_RULE
from cairn.transports.v1.parsing import MAX_REQUEST_BYTES
from cairn.transports.v1.wire import (
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_MALFORMED_JSON,
)

_JSON_CONTENT_TYPE = [(b"content-type", b"application/json")]

# A frame the Task 9 screen admits, for the tests below whose subject is
# *admission* rather than the screen. ``screen_frame`` now runs inside the
# same refusal as ``admit_body``, so a stand-in ``{}`` no longer reaches
# downstream and would make these assert the wrong thing for the wrong
# reason. ``ping`` because it is the shortest admitted method that carries
# no parameters of its own.
_WELL_FORMED_FRAME = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'

CORRELATION_ID = UUID("22222222-2222-4222-8222-222222222222")
INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
CREDENTIAL_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_config(tmp_path: Path) -> CairnConfig:
    """A minimal instance for the two end-to-end cases at the foot of this
    module. Local rather than shared with ``test_mount.py``: this package
    cannot carry a ``conftest.py``, because ``tests`` has no package
    markers and mypy refuses a second module of that name."""
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


def seed_bearer(data_path: Path) -> str:
    """One principal and one credential, as the ``Authorization`` value a
    valid caller presents. P-54 puts authentication in front of admission,
    so the two end-to-end cases cannot reach admission without one."""
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    timestamp = canonical_timestamp(NOW)
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
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, timestamp, None),
        )
        connection.commit()
    return f"Bearer {minted.text}"


def authenticated(headers: Headers, correlation_id: UUID) -> Actor | Rejected:
    """P-54's step, stubbed to succeed.

    Admission is what this module pins, and it runs only for a caller who
    has already authenticated; a real credential in every case here would
    add a catalogue to tests that need none and would prove nothing about
    the rule being asserted. What the stub cannot show — that
    authentication runs *before* admission — is pinned in
    ``test_auth.py``, where it belongs.
    """
    return Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID)


class Downstream:
    """Stands in for the SDK's session manager.

    Records whether it was called at all — which is the assertion that
    makes a refusal a boundary — and what body it saw through the replayed
    receive channel.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.body: bytes | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.body = await Request(scope, receive).body()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"admitted": true}'})


class Socket:
    """A receive channel that counts how often it is pulled.

    ``chunks`` is delivered one ASGI message per element, so a test can
    put a body on the wire the way a real client does and still assert
    that it left the socket exactly once.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.pulls = 0

    async def __call__(self) -> Message:
        self.pulls += 1
        if not self._chunks:
            return {"type": "http.disconnect"}
        body = self._chunks.pop(0)
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(self._chunks),
        }


class Wire:
    """What the caller got back: the status and the decoded body."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[bytes, bytes] = {}
        self.chunks = bytearray()

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message["headers"])
        elif message["type"] == "http.response.body":
            self.chunks.extend(message.get("body", b""))

    def json(self) -> dict[str, Any]:
        decoded: dict[str, Any] = json.loads(bytes(self.chunks))
        return decoded


def make_scope(
    headers: list[tuple[bytes, bytes]],
    *,
    body_length: int,
    declared_length: int | bytes | None = None,
    method: str = "POST",
) -> Scope:
    """An ASGI scope for `/v1/mcp`, with the scope state the foundation
    middleware would have put there.

    ``declared_length`` lets a test lie about ``Content-Length`` relative
    to what the socket actually delivers, numerically or as a value that
    is not decimal at all.
    """
    length = body_length if declared_length is None else declared_length
    if type(length) is int:
        length = str(length).encode("ascii")
    return {
        "type": "http",
        "method": method,
        "path": "/v1/mcp",
        "query_string": b"",
        "headers": [*headers, (b"content-length", length)],
        "state": {"correlation_id": CORRELATION_ID},
    }


async def admit(
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] = _JSON_CONTENT_TYPE,
    *,
    declared_length: int | bytes | None = None,
) -> tuple[Wire, Downstream, Socket]:
    """Runs one request through the wrapper and reports all three sides."""
    downstream = Downstream()
    socket = Socket(chunks)
    wire = Wire()
    scope = make_scope(
        headers,
        body_length=sum(len(chunk) for chunk in chunks),
        declared_length=declared_length,
    )
    await FrameAdmission(downstream, authenticate=authenticated)(scope, socket, wire)
    return wire, downstream, socket


def assert_refuses(
    wire: Wire,
    downstream: Downstream,
    *,
    rule: str,
    code: int,
    field_path: str = "body",
) -> None:
    """I-88's refusal shape, whole. The envelope under ``data`` is
    asserted field by field rather than by its ``rule`` alone: I-88
    requires all of it to be identical to what REST would have returned,
    so a drift in the message, the retry class or the correlation
    identifier matters as much as one in the identity."""
    assert downstream.calls == 0
    assert wire.status == FRAME_REJECTION_STATUS
    body = wire.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == code
    assert body["error"]["message"] == INVALID_REQUEST_MESSAGE
    assert body["error"]["data"] == {
        "failure": {
            "code": "invalid_request",
            "message": INVALID_REQUEST_MESSAGE,
            "retry": "never",
            "correlation_id": str(CORRELATION_ID),
            "detail": {"field_path": field_path, "rule": rule},
        }
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method",
    [
        pytest.param("GET", id="get"),
        pytest.param("DELETE", id="delete"),
        pytest.param("PUT", id="put"),
        pytest.param("PATCH", id="patch"),
    ],
)
async def test_every_method_but_post_is_refused_before_the_body_is_read(
    method: str,
) -> None:
    """P-53's first step, which the wrapper owns along with the other
    three. ``GET`` is the SDK's server-to-client stream and ``DELETE`` its
    session termination; I-84 has neither.

    The refusal is the 405 body the verb-named `/v1` routes give, with the
    ``Allow`` header RFC 9110 §10.2.1 requires, rather than an I-88 error
    object: a wrong method carries no JSON-RPC frame to answer in kind.
    That it happens before a byte is read is the ordering P-53 fixes —
    the socket is never pulled.
    """
    frame = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
    downstream = Downstream()
    socket = Socket([frame])
    wire = Wire()
    scope = make_scope(_JSON_CONTENT_TYPE, body_length=len(frame), method=method)
    await FrameAdmission(downstream, authenticate=authenticated)(scope, socket, wire)

    assert downstream.calls == 0
    assert socket.pulls == 0
    assert wire.status == 405
    assert wire.headers[b"allow"] == b"POST"
    failure = wire.json()["failure"]
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"field_path": "method", "rule": "method_not_allowed"}


@pytest.mark.anyio
async def test_a_json_object_frame_reaches_the_sdk_unchanged() -> None:
    frame = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
    wire, downstream, _ = await admit([frame])

    assert downstream.calls == 1
    assert downstream.body == frame
    assert wire.status == 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param(b"application/json", id="bare"),
        pytest.param(b"application/json; charset=utf-8", id="charset-lower"),
        pytest.param(b"application/json; charset=UTF-8", id="charset-upper"),
        pytest.param(b"Application/JSON", id="media-type-case"),
    ],
)
async def test_the_charset_parameter_and_case_variants_are_accepted(
    content_type: bytes,
) -> None:
    _, downstream, _ = await admit(
        [_WELL_FORMED_FRAME], [(b"content-type", content_type)]
    )

    assert downstream.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="absent"),
        pytest.param([(b"content-type", b"text/plain")], id="other-type"),
        pytest.param(
            [(b"content-type", b"application/json; charset=latin-1")],
            id="other-charset",
        ),
        pytest.param(
            [(b"content-type", b'application/json; charset="utf-8"')],
            id="quoted-charset",
        ),
        pytest.param(
            [(b"content-type", b"application/json; boundary=x")],
            id="other-parameter",
        ),
        pytest.param(
            [(b"content-type", b"application/json;")],
            id="trailing-semicolon",
        ),
        pytest.param(
            [(b"content-type", b"application/json; ")],
            id="semicolon-whitespace",
        ),
        pytest.param(
            [(b"content-type", b"application/json ;")],
            id="space-then-semicolon",
        ),
        pytest.param(
            [
                (b"content-type", b"application/json"),
                (b"content-type", b"application/json"),
            ],
            id="duplicated-header",
        ),
    ],
)
async def test_anything_but_json_content_type_is_refused(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """REST answers 415 here. I-88 says the refined statuses do not cross
    over, so what carries across is the rule identity, not the status."""
    wire, downstream, _ = await admit([b"{}"], headers)

    assert_refuses(
        wire,
        downstream,
        rule=RULE_INVALID_CONTENT_TYPE,
        code=JSONRPC_INVALID_REQUEST,
        field_path="Content-Type",
    )


@pytest.mark.anyio
async def test_a_declared_length_over_the_cap_is_refused_without_reading() -> None:
    wire, downstream, socket = await admit(
        [b"{}"], declared_length=MAX_REQUEST_BYTES + 1
    )

    assert_refuses(
        wire, downstream, rule=RULE_BODY_TOO_LARGE, code=JSONRPC_INVALID_REQUEST
    )
    assert socket.pulls == 0


@pytest.mark.anyio
async def test_a_non_decimal_declared_length_falls_through_to_the_stream() -> None:
    _, downstream, _ = await admit([_WELL_FORMED_FRAME], declared_length=b"junk")

    assert downstream.calls == 1


@pytest.mark.anyio
async def test_a_streamed_frame_over_the_cap_is_refused_despite_its_declaration() -> (
    None
):
    """I-30's ceiling is on what Cairn reads, so it is checked against the
    bytes as well as the declaration. Slice 5's P-26 noted that the custody
    seam carries no aggregate cap; on this transport there is nothing
    behind this check to catch an oversize frame."""
    oversize = b'{"padding": "' + b"a" * MAX_REQUEST_BYTES + b'"}'
    wire, downstream, _ = await admit([oversize], declared_length=2)

    assert_refuses(
        wire, downstream, rule=RULE_BODY_TOO_LARGE, code=JSONRPC_INVALID_REQUEST
    )


@pytest.mark.anyio
async def test_the_cap_counts_the_whole_frame_not_the_arguments() -> None:
    """I-86: "the cap is the whole frame, not the arguments object", so
    the JSON-RPC envelope's own bytes count towards it. A frame whose
    arguments sit under the ceiling but whose framing pushes it over is
    refused."""
    envelope = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": '
    arguments = b'{"padding": "' + b"a" * (MAX_REQUEST_BYTES - len(envelope)) + b'"}}'
    frame = envelope + arguments
    assert len(arguments) < MAX_REQUEST_BYTES < len(frame)
    wire, downstream, _ = await admit([frame])

    assert_refuses(
        wire, downstream, rule=RULE_BODY_TOO_LARGE, code=JSONRPC_INVALID_REQUEST
    )


@pytest.mark.anyio
async def test_a_frame_exactly_at_the_cap_is_admitted() -> None:
    # A real ``ping`` padded to the ceiling rather than a bare object: the
    # claim is that the cap admits the last permitted byte, and since
    # Task 9 a frame the screen would refuse cannot demonstrate that.
    prefix = b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"p": "'
    suffix = b'"}}'
    padding = b"a" * (MAX_REQUEST_BYTES - len(prefix) - len(suffix))
    frame = prefix + padding + suffix
    assert len(frame) == MAX_REQUEST_BYTES
    _, downstream, _ = await admit([frame])

    assert downstream.calls == 1
    assert downstream.body == frame


@pytest.mark.anyio
async def test_invalid_utf8_is_refused_before_parsing() -> None:
    wire, downstream, _ = await admit([b'{"a": "\xff\xfe"}'])

    assert_refuses(
        wire, downstream, rule=RULE_INVALID_ENCODING, code=JSONRPC_PARSE_ERROR
    )


@pytest.mark.anyio
async def test_malformed_json_is_refused() -> None:
    wire, downstream, _ = await admit([b'{"a": '])

    assert_refuses(wire, downstream, rule=RULE_MALFORMED_JSON, code=JSONRPC_PARSE_ERROR)


@pytest.mark.anyio
async def test_a_pathologically_nested_frame_is_refused_not_crashed() -> None:
    wire, downstream, _ = await admit([b"[" * 100_000])

    assert_refuses(wire, downstream, rule=RULE_MALFORMED_JSON, code=JSONRPC_PARSE_ERROR)


@pytest.mark.anyio
async def test_an_integer_over_the_digit_limit_is_refused_not_crashed() -> None:
    wire, downstream, _ = await admit([b'{"id": ' + b"9" * 5000 + b"}"])

    assert_refuses(wire, downstream, rule=RULE_MALFORMED_JSON, code=JSONRPC_PARSE_ERROR)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b'{"id": NaN}', id="nan"),
        pytest.param(b'{"id": Infinity}', id="infinity"),
        pytest.param(b'{"id": -Infinity}', id="negative-infinity"),
        pytest.param(b'{"params": {"b": [1, NaN]}}', id="nested-nan"),
    ],
)
async def test_non_finite_json_constants_are_refused_at_admission(
    frame: bytes,
) -> None:
    """The SDK parses with a bare ``json.loads``, which admits all three.
    Refusing them here is the whole reason admission runs first."""
    wire, downstream, _ = await admit([frame])

    assert_refuses(wire, downstream, rule=RULE_MALFORMED_JSON, code=JSONRPC_PARSE_ERROR)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b'{"method": "ping", "method": "tools/list"}', id="top-level"),
        pytest.param(
            b'{"params": {"scope": 1, "scope": 2}}',
            id="nested",
        ),
        pytest.param(
            b'{"params": {"facts": [{"scope": 1, "scope": 2}]}}',
            id="inside-array",
        ),
    ],
)
async def test_duplicate_json_keys_anywhere_are_refused(frame: bytes) -> None:
    """I-71's rule is document-wide, and the parameters of a tool call are
    part of the same document. Left to the SDK's parser every one of these
    would be last-value-wins."""
    wire, downstream, _ = await admit([frame])

    body = wire.json()
    assert downstream.calls == 0
    assert body["error"]["code"] == JSONRPC_PARSE_ERROR
    assert body["error"]["data"]["failure"]["detail"]["rule"] == (
        RULE_DUPLICATE_JSON_KEY
    )


@pytest.mark.anyio
async def test_an_echoed_duplicate_key_is_bounded_in_the_field_path() -> None:
    """The field path echoes a caller-authored key name, so it is bounded
    on this transport by the same constant as on REST — one bound, in
    ``parsing.py``, rather than a second one here that could drift."""
    key = b"k" * 4096
    wire, downstream, _ = await admit([b'{"' + key + b'": 1, "' + key + b'": 2}'])

    assert_refuses(
        wire,
        downstream,
        rule=RULE_DUPLICATE_JSON_KEY,
        code=JSONRPC_PARSE_ERROR,
        field_path="k" * 128,
    )


@pytest.mark.anyio
async def test_the_same_key_in_sibling_objects_is_not_a_duplicate() -> None:
    # ``id`` at both levels: one key, two objects, no duplicate. Carried
    # on a real ``ping`` so the frame reaches downstream on its merits
    # rather than being refused by the Task 9 screen first.
    _, downstream, _ = await admit(
        [b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"id": 2}}']
    )

    assert downstream.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"42", id="number"),
        pytest.param(b'"text"', id="string"),
        pytest.param(b"null", id="null"),
    ],
)
async def test_a_non_object_frame_is_refused(frame: bytes) -> None:
    wire, downstream, _ = await admit([frame])

    assert_refuses(
        wire, downstream, rule=RULE_BODY_NOT_OBJECT, code=JSONRPC_INVALID_REQUEST
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"[]", id="empty-batch"),
        pytest.param(
            b'[{"jsonrpc": "2.0", "id": 1, "method": "ping"},'
            b' {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]',
            id="two-call-batch",
        ),
    ],
)
async def test_a_json_rpc_batch_is_refused(frame: bytes) -> None:
    """I-86 refuses batching outright: one frame carries one request, so
    the cap, the audit event, the idempotency key and the correlation
    identifier each keep a single referent. Pinned by its own test rather
    than left implicit in the non-object clause above, so a conformance
    scenario asserting it asserts something deliberate. The rule is
    ``body_not_object`` because that is what a top-level array is.
    """
    wire, downstream, _ = await admit([frame])

    assert_refuses(
        wire, downstream, rule=RULE_BODY_NOT_OBJECT, code=JSONRPC_INVALID_REQUEST
    )


@pytest.mark.anyio
async def test_the_frame_is_read_from_the_socket_exactly_once() -> None:
    """P-53's replay clause. ``admit_body`` consumes the receive channel,
    and the SDK then calls ``await request.body()`` on the same scope; the
    admitted bytes are replayed so the SDK sees exactly what was admitted
    rather than an exhausted stream.
    """
    frame = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
    _, downstream, socket = await admit([frame])

    assert socket.pulls == 1
    assert downstream.body == frame


@pytest.mark.anyio
async def test_the_replayed_channel_ends_rather_than_repeating() -> None:
    """A channel that re-delivered its body would let a consumer polling
    for more read the same frame twice — the mirror image of the bug the
    replay exists to prevent."""
    pulled: list[Message] = []

    async def pull_twice(scope: Scope, receive: Receive, send: Send) -> None:
        pulled.append(await receive())
        pulled.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    frame = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
    scope = make_scope(_JSON_CONTENT_TYPE, body_length=len(frame))
    await FrameAdmission(pull_twice, authenticate=authenticated)(
        scope, Socket([frame]), Wire()
    )

    assert pulled[0] == {"type": "http.request", "body": frame, "more_body": False}
    assert pulled[1] == {"type": "http.disconnect"}


@pytest.mark.anyio
async def test_a_chunked_frame_is_replayed_whole() -> None:
    """Admitted across every message and replayed as one, so the SDK is
    never handed a partial frame nor left to pull the remainder."""
    chunks = [b'{"jsonrpc": "2.0", ', b'"id": 1, ', b'"method": "ping"}']
    _, downstream, socket = await admit(chunks)

    assert socket.pulls == len(chunks)
    assert downstream.body == b"".join(chunks)


@pytest.mark.anyio
async def test_the_endpoint_refuses_a_duplicate_key_frame_end_to_end(
    tmp_path: Path,
) -> None:
    """The wrapper, wired: everything above drives ``FrameAdmission``
    directly, so something must prove the SDK is reachable only through it
    and that the foundation middleware passes a refusal through rather
    than rewriting it. A duplicate key is the case that matters most,
    being the one the SDK's own parser would silently have accepted as
    last-value-wins.
    """
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    frame = b'{"jsonrpc": "2.0", "id": 1, "id": 2, "method": "ping"}'
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                MOUNT_PATH,
                content=frame,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "Authorization": bearer,
                },
            )

    assert response.status_code == FRAME_REJECTION_STATUS
    body = response.json()
    assert body["error"]["code"] == JSONRPC_PARSE_ERROR
    failure = body["error"]["data"]["failure"]
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"field_path": "id", "rule": RULE_DUPLICATE_JSON_KEY}
    # The correlation identifier is the one the middleware minted for this
    # request, not a value the adapter invented: it is a real UUID and it
    # is echoed on the response header the foundation surface already sets.
    assert UUID(failure["correlation_id"])


@pytest.mark.anyio
async def test_an_admitted_frame_still_reaches_the_sdk_end_to_end(
    tmp_path: Path,
) -> None:
    """The other half: admission must not have broken the handshake Task 3
    pinned. If the replay were wrong the SDK would see an empty or
    truncated body and answer its own parse error instead."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
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
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                MOUNT_PATH,
                content=json.dumps(initialize),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": bearer,
                },
            )

    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"


def test_every_admission_rule_has_a_pinned_json_rpc_code() -> None:
    """I-88 fixes the code to "the SDK's standard one for the fault
    class". This pins the whole map, so a rule added to admission without
    a decided code fails here rather than defaulting quietly."""
    assert JSONRPC_CODE_BY_RULE == {
        RULE_INVALID_CONTENT_TYPE: JSONRPC_INVALID_REQUEST,
        RULE_BODY_TOO_LARGE: JSONRPC_INVALID_REQUEST,
        RULE_BODY_NOT_OBJECT: JSONRPC_INVALID_REQUEST,
        RULE_INVALID_ENCODING: JSONRPC_PARSE_ERROR,
        RULE_MALFORMED_JSON: JSONRPC_PARSE_ERROR,
        RULE_DUPLICATE_JSON_KEY: JSONRPC_PARSE_ERROR,
    }
