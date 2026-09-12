"""Task 3: the `/v1/mcp` endpoint's transport posture (I-84, P-52).

Every assertion here is a refusal or a handshake — no Cairn operation is
reachable yet, because no tool is registered until Task 6. What is pinned
is the shape of the surface: one endpoint, POST only, stateless, JSON
responses, and refusals that match the ones the verb-named `/v1` routes
already give rather than the SDK's defaults.

The refusals are tested rather than assumed. P-52 says so explicitly, and
the reason is that `GET` and `DELETE` are the two methods a caller can
reach without a valid frame: if the SDK's stateless mode ever stopped
refusing them, nothing else in the suite would notice.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse
from starlette.routing import Mount, Route

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import Operation
from cairn.transports.mcp.mount import MOUNT_PATH, MOUNT_ROUTE_NAME, SERVER_NAME

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
CREDENTIAL_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
# The revision the pinned SDK implements, recorded by P-49 and asserted
# here so an SDK bump that moves it fails loudly rather than silently
# renegotiating the contract the I-89 manifest will publish.
PROTOCOL_REVISION = "2025-11-25"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_REVISION,
        "capabilities": {},
        "clientInfo": {"name": "conformance", "version": "0"},
    },
}

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


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


def seed_bearer(data_path: Path) -> str:
    """One principal and one credential, as the ``Authorization`` value a
    valid caller presents.

    ``tests/transports/v1/test_auth.py``'s recipe — direct rows rather
    than the administration path, a credential row being immutable once
    written. Module-local for the reason ``make_config`` is: this package
    cannot carry a shared ``conftest.py``, because ``tests`` has no
    package markers and mypy refuses a second module of that name.
    """
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


async def call(
    client: AsyncClient, body: dict[str, object], bearer: str
) -> HTTPXResponse:
    """Every frame carries a credential from Task 5 onwards (P-54): the
    handshake is behind the same bearer check a tool call is."""
    return await client.post(
        MOUNT_PATH,
        content=json.dumps(body),
        headers={**MCP_HEADERS, "Authorization": bearer},
    )


def test_the_endpoint_is_registered_at_the_exact_v1_path(
    tmp_path: Path,
) -> None:
    """I-84's endpoint, registered as a literal ``Route``.

    No ``Mount`` may appear: a mount would match by prefix and redirect
    ``/v1/mcp`` to ``/v1/mcp/``, making the trailing slash the canonical
    spelling of a path ruled without one.
    """
    config = make_config(tmp_path)
    application = build_application(config)
    named = {
        route.path: route.name
        for route in application.routes
        if isinstance(route, Route) and route.path == MOUNT_PATH
    }
    assert named == {MOUNT_PATH: MOUNT_ROUTE_NAME}
    assert [route for route in application.routes if isinstance(route, Mount)] == []


def test_the_mount_name_is_not_an_operation_label() -> None:
    """P-57: one ASGI mount serves eleven tools, so the middleware's
    route-name lookup must not resolve it to an operation. Until Task 10
    supplies the resolved tool, the mount is unlabelled — which is
    correct — rather than labelled with whichever operation happened to
    share its name."""
    assert MOUNT_ROUTE_NAME not in {operation.value for operation in Operation}


@pytest.mark.anyio
async def test_initialize_negotiates_the_pinned_protocol_revision(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await call(client, INITIALIZE, bearer)

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["protocolVersion"] == PROTOCOL_REVISION
    assert body["result"]["serverInfo"]["name"] == SERVER_NAME


@pytest.mark.anyio
async def test_the_response_is_json_rather_than_an_event_stream(
    tmp_path: Path,
) -> None:
    """P-52's JSON-response posture. In SSE mode the SDK requires callers
    to accept ``text/event-stream`` for a stream Cairn can never fill,
    since I-84 admits no server-initiated message of any kind."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await call(client, INITIALIZE, bearer)

    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_no_session_identifier_is_issued(
    tmp_path: Path,
) -> None:
    """Stateless: nothing to resume, reap or steal, and every request
    carries its own authentication once Task 5 requires one."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await call(client, INITIALIZE, bearer)

    assert "mcp-session-id" not in response.headers


@pytest.mark.anyio
async def test_the_endpoint_advertises_the_tools_capability(
    tmp_path: Path,
) -> None:
    """The capability itself is the mount's concern; its eleven-tool
    inventory is ``test_tools.py``'s. Task 6 filled the list this test
    used to assert was empty — the capability was declared before the
    tools existed so that filling it was not a protocol change."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            initialised = await call(client, INITIALIZE, bearer)
            listed = await call(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                bearer,
            )

    capabilities = initialised.json()["result"]["capabilities"]
    assert "tools" in capabilities
    # I-84 implements no other capability: no resources, no prompts, no
    # sampling, no completions, no logging.
    assert set(capabilities) - {"experimental"} == {"tools"}
    assert listed.json()["result"]["tools"] != []


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
async def test_every_method_but_post_is_refused(tmp_path: Path, method: str) -> None:
    """I-84 has no server-to-client stream (GET) and no session to
    terminate (DELETE). Cairn owns the refusal rather than inheriting the
    SDK's, per I-88, so the answer is the same 405 body the verb-named
    routes give — with the Allow header RFC 9110 §10.2.1 requires."""
    config = make_config(tmp_path)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.request(method, MOUNT_PATH, headers=MCP_HEADERS)

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    body = response.json()
    assert body["failure"]["code"] == "invalid_request"
    assert body["failure"]["detail"]["rule"] == "method_not_allowed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param(f"{MOUNT_PATH}/anything", id="sub-path"),
        pytest.param(f"{MOUNT_PATH}/messages", id="sdk-style-sub-path"),
    ],
)
async def test_only_the_exact_mount_path_is_served(tmp_path: Path, path: str) -> None:
    """P-52: a Starlette mount matches by prefix, and the surface I-84
    fixed is one endpoint. A prefix that quietly accepts sub-paths is a
    larger surface than the one that was ruled.

    """
    config = make_config(tmp_path)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                path, content=json.dumps(INITIALIZE), headers=MCP_HEADERS
            )

    assert response.status_code == 404
    assert response.json()["failure"]["code"] == "not_found"


@pytest.mark.anyio
async def test_the_trailing_slash_redirects_to_the_canonical_path(
    tmp_path: Path,
) -> None:
    """``/v1/mcp/`` is not a second endpoint: the router redirects it to
    the one canonical path, exactly as it does for every verb-named `/v1`
    route. This is the behaviour a ``Mount`` would have inverted, serving
    the slash form and redirecting the bare path to it.
    """
    config = make_config(tmp_path)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                f"{MOUNT_PATH}/", content=json.dumps(INITIALIZE), headers=MCP_HEADERS
            )

    assert response.status_code == 307
    assert response.headers["location"].endswith(MOUNT_PATH)


@pytest.mark.anyio
async def test_the_verb_named_routes_still_answer_their_own_boundary(
    tmp_path: Path,
) -> None:
    """The `_is_v1_path` narrowing must exclude the MCP mount without
    releasing anything else: an unknown `/v1` path still gets I-70's
    ``not_found`` envelope rather than the framework default."""
    config = make_config(tmp_path)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post("/v1/not-a-route", json={})

    assert response.status_code == 404
    assert response.json()["failure"]["code"] == "not_found"


def test_the_v1_route_inventory_is_unchanged_by_the_mount(
    tmp_path: Path,
) -> None:
    """Mounting a transport must not add, remove or rename an operation
    route. I-84 adds no operation; this is that claim, mechanised."""
    config = make_config(tmp_path)
    application = build_application(config)
    paths = {route.path for route in application.routes if isinstance(route, Route)}

    assert "/v1/ingest" in paths
    # Eleven operation routes plus the transport endpoint, which I-84
    # states adds no operation — the count is what holds it to that.
    assert len({path for path in paths if path.startswith("/v1/")}) == 12
