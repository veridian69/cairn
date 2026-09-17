"""The MCP Streamable HTTP endpoint, mounted inside the `/v1` surface.

I-84 puts MCP at ``/v1/mcp``, inside `/v1` on Operator's ruling of 10 August
2026, so that I-70's "outside ``/v1`` only the health probes and
``/metrics`` exist" stays literally true.

The transport posture is P-52's, and each part refuses something the SDK
would otherwise offer:

- **Stateless.** No session between requests, so every request carries
  its own authentication and there is no session identifier to steal,
  resume or reap.
- **JSON responses.** Cairn never initiates a message (I-84 admits no
  notifications, progress, subscriptions or logging), and in SSE mode the
  SDK would demand callers `Accept: text/event-stream` for a stream that
  can never carry anything (``streamable_http.py:449-469``).
- **POST only, at exactly this path**, registered as a ``Route`` rather
  than a ``Mount`` — see ``McpTransport``.

The endpoint owns its refusals rather than falling through to the `/v1`
boundary handlers (I-88), and they all live in ``framing.py``, which this
module wraps the SDK in: the only way to the SDK is through Cairn's
boundary. It requires a bearer credential on every request and serves the
eleven tools ``server.py`` projects from the shared operation table.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

import anyio
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Message, Receive, Scope, Send

from cairn.administration.commands import CairnAdministration
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.runtime.logging import Transport
from cairn.screening import SecretScreen
from cairn.transports.mcp.framing import Authenticate, FrameAdmission
from cairn.transports.mcp.server import TOOL_NAMES, register_calls, register_tools
from cairn.transports.rest.middleware import TRANSPORT_STATE_KEY
from cairn.transports.v1.paths import MCP_MOUNT_PATH

# The mount's route name. Deliberately not a member of ``Operation``: the
# foundation middleware resolves its operation label from the route name,
# and one ASGI mount serving eleven tools cannot be labelled by its route.
# Task 10 wired P-57's answer — the adapter writes the resolved operation
# into scope state and the middleware prefers it — so this name still
# fails the route lookup, which is now the correct fallback rather than a
# gap.
MOUNT_ROUTE_NAME = "mcp_transport"

# Re-exported from the shared `/v1` package, which owns it so that the
# REST boundary handlers can exclude this path without importing the MCP
# adapter that renders its refusals through them.
MOUNT_PATH = MCP_MOUNT_PATH

SERVER_NAME = "drystane-cairn"


def build_mcp_server(
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
) -> Server:
    """The low-level MCP server carrying the eleven I-84 tools.

    Every argument is the identical object ``register_v1_routes``
    receives, and that identity is the point. ``write_gate`` is the
    application's single P-29 lock: two locks would let an MCP mutation
    and a REST mutation enter the catalogue section together, which no
    test of either transport alone could see. The last three are the
    ``instance`` values, resolved once at startup — two transports
    reporting two contract digests for one process is the divergence I-90
    forbids, and I-89 makes that two digests apiece rather than one.
    """
    server: Server = Server(SERVER_NAME)
    register_tools(server)
    register_calls(
        server,
        authority=authority,
        administration=administration,
        transactions=transactions,
        screen=screen,
        data_path=data_path,
        write_gate=write_gate,
        clock=clock,
        instance_id=instance_id,
        product_version=product_version,
        contract_digest=contract_digest,
        mcp_contract_digest=mcp_contract_digest,
    )
    return server


def build_session_manager(server: Server) -> StreamableHTTPSessionManager:
    """P-52's transport posture, in the SDK's own terms."""
    return StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        stateless=True,
    )


@asynccontextmanager
async def running_session_manager(
    manager: StreamableHTTPSessionManager,
) -> AsyncIterator[None]:
    """Holds the manager's task group open for the application's lifetime.

    The SDK requires this and permits it exactly once per instance
    (``streamable_http_manager.py:130-138``), which is why the manager is
    built per application rather than shared.
    """
    async with manager.run():
        yield


def _normalised(send: Send) -> Send:
    """Coerces the SDK's response-start status to a plain ``int``.

    The foundation middleware's buffer accepts a start message only when
    ``type(status) is int`` (``middleware.py:189``), and the SDK sends
    ``HTTPStatus.OK`` — an ``IntEnum``, so every MCP response was answered
    500 by the middleware's fallback. Adapting the dependency is this
    adapter's job; relaxing a reviewed invariant that guards every
    transport is not.
    """

    async def normalised_send(message: Message) -> None:
        if message.get("type") == "http.response.start":
            status = message.get("status")
            if type(status) is not int and isinstance(status, int):
                message = {**message, "status": int(status)}
        await send(message)

    return normalised_send


class McpTransport:
    """The endpoint's ASGI application: Cairn's refusals, then its
    admission boundary, then the SDK.

    A class rather than a closure, and registered as a ``Route`` rather
    than a ``Mount``, both for the same reason — the endpoint is exactly
    one path. A Starlette ``Mount`` matches by prefix and its pattern
    requires a following separator, so ``/v1/mcp`` itself does not match
    and the router answers 307 to ``/v1/mcp/``: the trailing slash would
    become the canonical spelling of an endpoint I-84 fixed without one.
    A ``Route`` matches the literal path, and Starlette treats a
    non-function endpoint as a raw ASGI application, which is what the
    SDK's session manager needs.
    """

    def __init__(
        self,
        manager: StreamableHTTPSessionManager,
        *,
        authenticate: Authenticate,
        tool_names: frozenset[str] = TOOL_NAMES,
    ) -> None:
        # Built once around the session manager rather than consulted per
        # request by a caller who could forget to: all three refusals are
        # the wrapper's, in P-53 and P-54's order.
        self._admitted = FrameAdmission(
            manager.handle_request, authenticate=authenticate, tool_names=tool_names
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # P-57's transport label, written before anything this endpoint
        # serves can fail. Unconditional and first: an authentication
        # denial or a protocol fault never resolves a tool, and labelling
        # those ``rest`` because they failed early would file MCP's
        # refusals under REST's series — the one confusion the dimension
        # exists to prevent.
        scope["state"][TRANSPORT_STATE_KEY] = Transport.MCP
        await self._admitted(scope, receive, _normalised(send))
