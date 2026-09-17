"""Task 5: mount-layer authentication at `/v1/mcp` (I-88, P-54).

Two claims are under test and they are different in kind.

The first is that MCP's authentication *is* REST's. P-54 shares
``authenticate_request`` itself rather than restating it, so what has to
be proved is not that each denial behaves correctly — ``test_auth.py``
under ``tests/transports/v1/`` already proves that of the function — but
that the MCP endpoint routes every request through it and renders the
result the way I-88 fixes: 401 with ``WWW-Authenticate: Bearer`` and the
I-26 envelope on the HTTP response, not a JSON-RPC error object. The
public bodies are therefore compared byte for byte against the REST
transport's, on the same running application.

The second is ordering. Authentication runs before admission, so an
unauthenticated caller sending a frame Cairn would otherwise refuse gets
the 401 and learns nothing about whether the frame was well formed. That
one cannot be inherited from anywhere: it is a property of this wrapper's
sequence, and the only thing that holds it is a test that sends a bad
frame with no credential.

The audit-chain assertions are here for the same reason the absence
assertions are: I-88 says the protocol methods append nothing, and "no
audit event" is a claim that decays silently if nothing holds it.

Named for the mount rather than ``test_auth.py``: mypy resolves test
modules by basename, ``tests`` has no package markers, and
``tests/transports/v1/test_auth.py`` already holds that name.
"""

import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, closing
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

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.framing import MOUNT_ACTION_CODE, MOUNT_ACTION_KIND
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.v1.parsing import MAX_REQUEST_BYTES

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

# The credential factory's shape: this test needs a valid credential and a
# revoked one at the same time, the revoked case being a denial only if
# the caller could otherwise have succeeded.
IssueCredential = Callable[..., str]

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

INITIALIZE: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "conformance", "version": "0"},
    },
}
TOOLS_CALL: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "instance", "arguments": {}},
}
TOOLS_LIST: dict[str, object] = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
PING: dict[str, object] = {"jsonrpc": "2.0", "id": 4, "method": "ping"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_config(tmp_path: Path) -> CairnConfig:
    """A migrated instance holding no credential. Module-local because
    ``tests`` has no package markers, so this package cannot carry a
    ``conftest.py`` without mypy refusing a second module of that name."""
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


def credential_factory(data_path: Path) -> IssueCredential:
    """Writes the one principal, then issues distinct credentials against
    it on demand — valid, or revoked at the moment of issue."""
    timestamp = canonical_timestamp(NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.commit()
    ordinals = count(1)

    def issue(*, revoked: bool = False) -> str:
        credential_id = UUID(f"{next(ordinals):08x}-6666-4666-8666-666666666666")
        minted = mint_token(credential_id, lambda size: bytes(range(size)))
        with _open_write_connection(data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(credential_id),
                    str(PRINCIPAL_ID),
                    minted.verifier,
                    timestamp,
                    None,
                ),
            )
            if revoked:
                connection.execute(
                    "INSERT INTO credential_revocations "
                    "(credential_id, revoked_at, revoked_by, reason_code) "
                    "VALUES (?, ?, ?, ?)",
                    (str(credential_id), timestamp, str(PRINCIPAL_ID), "superseded"),
                )
            connection.commit()
        return f"Bearer {minted.text}"

    return issue


@asynccontextmanager
async def running(application: FastAPI) -> AsyncIterator[AsyncClient]:
    """The application, started once.

    Once per test rather than once per request: the runtime holds a lease
    and its status is one-way, so a second lifespan over the same
    application fails before a second request could be made.
    """
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client


async def post(
    client: AsyncClient,
    *,
    content: str,
    authorization: tuple[str, ...] = (),
) -> HTTPXResponse:
    """One frame at the mount.

    ``authorization`` is a tuple rather than a string so a test can
    present the header twice — the duplicated-credential case cannot be
    expressed with a header mapping.
    """
    headers = [*MCP_HEADERS.items(), *(("Authorization", v) for v in authorization)]
    return await client.post(MOUNT_PATH, content=content, headers=headers)


def audit_events(data_path: Path) -> list[dict[str, Any]]:
    """Every event on every chain, in write order.

    Deliberately not filtered to the instance chain: the absence claims
    below are that *nothing* was appended anywhere, and a filter would
    make a realm-chain write invisible to them.
    """
    with (
        closing(sqlite3.connect(data_path / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events ORDER BY sequence"
        ).fetchall()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        document = json.loads(row[0])
        assert type(document) is dict
        parsed.append(document)
    return parsed


def assert_denied(response: HTTPXResponse) -> None:
    """I-88's authentication answer: an HTTP failure, not a JSON-RPC one.

    The absence of ``jsonrpc`` in the body is asserted because it is the
    thing that could plausibly drift — an implementer reaching for the
    error object every other refusal at this endpoint uses. I-88 keeps
    authentication at the HTTP layer precisely so that a client without a
    credential is answered in the protocol it has actually reached.
    """
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    body = response.json()
    assert "jsonrpc" not in body
    failure = body["failure"]
    assert failure["code"] == "authentication_failed"
    assert failure["retry"] == "never"
    # I-26's coarse outcome: no detail, on any denial, for any reason.
    assert "detail" not in failure
    assert UUID(failure["correlation_id"])


def bad_credentials(issue: IssueCredential) -> dict[str, tuple[tuple[str, ...], str]]:
    """Every way a credential can fail, with the private reason code
    ``auth.py`` records for it.

    The public answers are identical — that is `AUTH-03` — so the reason
    codes are what prove the endpoint reached the real authenticator
    rather than short-circuiting on a check of its own.
    """
    valid = issue()
    return {
        "missing": ((), "authorization_header_missing"),
        "duplicated": ((valid, valid), "authorization_header_duplicated"),
        "wrong-scheme": (
            ("Basic " + valid.removeprefix("Bearer "),),
            "authorization_scheme_unsupported",
        ),
        "malformed": (("Bearer not-a-token",), "malformed_token"),
        "revoked": ((issue(revoked=True),), "credential_is_revoked"),
    }


def credential_cases() -> Iterator[Any]:
    for key in ("missing", "duplicated", "wrong-scheme", "malformed", "revoked"):
        for frame, frame_id in ((TOOLS_CALL, "tools-call"), (INITIALIZE, "initialize")):
            yield pytest.param(key, frame, id=f"{key}-{frame_id}")


@pytest.mark.anyio
@pytest.mark.parametrize(("key", "frame"), list(credential_cases()))
async def test_every_bad_credential_is_refused_on_a_tool_call_and_the_handshake(
    tmp_path: Path,
    key: str,
    frame: dict[str, object],
) -> None:
    """P-54's coverage claim: the check sits at the HTTP layer of the
    mount, so it covers the protocol handshake exactly as it covers a tool
    call. Both frames are sent for every credential fault because the
    handshake is the one an unauthenticated client actually reaches
    first — if authentication were wired into the tool dispatch instead,
    every assertion here would still pass on ``tools/call`` and fail on
    ``initialize``.

    The denied ``initialize`` appends an event by I-88's amendment of
    11 August 2026: the decision's "no audit event" clause governs the
    protocol methods once authenticated, not a denial, which cannot know
    the method without parsing an unauthenticated caller's bytes.
    """
    config = make_config(tmp_path)
    presented, reason_code = bad_credentials(credential_factory(config.paths.data))[key]

    async with running(build_application(config)) as client:
        response = await post(
            client, content=json.dumps(frame), authorization=presented
        )

    assert_denied(response)
    (event,) = audit_events(config.paths.data)
    assert event["reason_code"] == reason_code
    assert event["outcome"] == "deny"
    assert event["chain_kind"] == "instance"
    # An unverified caller claim is not an identity: the event names no
    # principal and no credential, whatever the caller presented.
    assert event["principal_id"] is None
    assert event["credential_verifier_id"] is None
    # P-54 authenticates before the frame is parsed, so no operation is
    # known and the event says so rather than borrowing an operation's
    # code from the tool the frame happened to name.
    assert event["action_code"] == MOUNT_ACTION_CODE
    assert event["action_kind"] == MOUNT_ACTION_KIND.value
    assert event["correlation_id"] == response.json()["failure"]["correlation_id"]


@pytest.mark.anyio
async def test_the_denial_fingerprints_what_was_presented(
    tmp_path: Path,
) -> None:
    """The correlation handle I-53 gives an operator: repeated
    presentations of one bad token hash alike, and the token itself never
    enters the event. Inherited from ``authenticate_request``, asserted
    here because "inherited" is a claim about wiring."""
    presented = "Bearer not-a-token"

    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        for _ in range(2):
            await post(client, content=json.dumps(PING), authorization=(presented,))

    first, second = audit_events(config.paths.data)
    expected = hashlib.sha256(presented.encode()).hexdigest()
    assert first["safe_request_fingerprint"] == expected
    assert second["safe_request_fingerprint"] == expected


@pytest.mark.anyio
async def test_an_absent_credential_fingerprints_nothing(
    tmp_path: Path,
) -> None:
    """There is nothing to fingerprint, so the field is absent rather than
    a hash of the empty string — which would collide with every other
    caller who presented nothing."""
    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        await post(client, content=json.dumps(PING))

    (event,) = audit_events(config.paths.data)
    assert event["safe_request_fingerprint"] is None


@pytest.mark.anyio
async def test_a_malformed_frame_from_an_unauthenticated_caller_is_a_401(
    tmp_path: Path,
) -> None:
    """P-54's ordering clause, and the only test that can hold it.

    The frame carries a duplicate key, which admission refuses with
    ``duplicate_json_key`` under a JSON-RPC error object. Presented
    without a credential it must instead be a 401 that says nothing about
    the frame: an unauthenticated caller must not be able to learn which
    of its frames Cairn considers well formed. Reverse the two steps and
    this is the only assertion in the suite that changes.
    """
    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        response = await post(
            client, content='{"jsonrpc": "2.0", "id": 1, "id": 2, "method": "ping"}'
        )

    assert_denied(response)
    assert "duplicate_json_key" not in response.text
    (event,) = audit_events(config.paths.data)
    assert event["reason_code"] == "authorization_header_missing"


@pytest.mark.anyio
async def test_an_oversized_frame_from_an_unauthenticated_caller_is_a_401(
    tmp_path: Path,
) -> None:
    """The same ordering, at the other end of admission: the I-30 ceiling
    is a cheap check and refusing on it first would be the tempting
    optimisation. It would also tell an unauthenticated caller what
    Cairn's limit is."""
    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        response = await post(client, content=" " * (MAX_REQUEST_BYTES + 1))

    assert_denied(response)
    assert "body_too_large" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(INITIALIZE, id="initialize"),
        pytest.param(TOOLS_LIST, id="tools-list"),
        pytest.param(PING, id="ping"),
    ],
)
async def test_an_authenticated_protocol_method_appends_no_audit_event(
    tmp_path: Path,
    frame: dict[str, object],
) -> None:
    """I-88: the three protocol methods are the transport handshake, not a
    request against the authority model, so no Cairn operation was
    attempted and there is nothing to audit.

    The residual this leaves is stated in that decision rather than
    discovered here: an unauthenticated handshake probe is visible in the
    safe operational log and in the metrics, and not on the audit chain.
    """
    config = make_config(tmp_path)
    bearer = credential_factory(config.paths.data)()
    async with running(build_application(config)) as client:
        response = await post(
            client, content=json.dumps(frame), authorization=(bearer,)
        )

    assert response.status_code == 200
    assert "error" not in response.json()
    assert audit_events(config.paths.data) == []


@pytest.mark.anyio
async def test_an_unknown_method_from_an_authenticated_caller_appends_nothing(
    tmp_path: Path,
) -> None:
    """The failure half of the same rule. A method Cairn does not
    implement is a protocol fault under I-88 — no operation was
    identified — so it is a JSON-RPC error object and the chain stays
    empty."""
    frame = {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}

    config = make_config(tmp_path)
    bearer = credential_factory(config.paths.data)()
    async with running(build_application(config)) as client:
        response = await post(
            client, content=json.dumps(frame), authorization=(bearer,)
        )

    assert "error" in response.json()
    assert audit_events(config.paths.data) == []


@pytest.mark.anyio
async def test_a_wrong_method_is_refused_before_authentication(
    tmp_path: Path,
) -> None:
    """P-53's first step stays first. A ``GET`` carries no frame and no
    credential is required to say so, and refusing it before the
    authenticator runs is what stops an unauthenticated caller appending
    to a durable, irrevocable chain by looping over a method Cairn does
    not serve.
    """
    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        response = await client.get(MOUNT_PATH, headers=MCP_HEADERS)

    assert response.status_code == 405
    assert response.json()["failure"]["detail"]["rule"] == "method_not_allowed"
    assert audit_events(config.paths.data) == []


@pytest.mark.anyio
async def test_the_public_denial_is_identical_to_the_rest_transports(
    tmp_path: Path,
) -> None:
    """I-88 says the MCP 401 carries "the I-26 ``authentication_failed``
    envelope", which is the REST body and not a variant of it. Compared on
    the same application rather than against a literal, so the two cannot
    drift together into agreeing about something wrong.

    Only the correlation identifier differs, being per request.
    """
    config = make_config(tmp_path)
    async with running(build_application(config)) as client:
        mcp = await post(client, content=json.dumps(PING))
        # ``instance`` because I-70 makes it the one operation that takes
        # no caller input at all: nothing but the missing credential can
        # be what the two transports refuse.
        rest = await client.get("/v1/instance")

    assert mcp.status_code == rest.status_code == 401
    assert mcp.headers["WWW-Authenticate"] == rest.headers["WWW-Authenticate"]
    mcp_failure = mcp.json()["failure"]
    rest_failure = rest.json()["failure"]
    assert mcp_failure.pop("correlation_id") != rest_failure.pop("correlation_id")
    assert mcp_failure == rest_failure
