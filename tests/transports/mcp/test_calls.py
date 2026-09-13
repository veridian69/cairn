"""Task 7: the eight mutation tool handlers (I-85, P-55).

Three tests exist because of the pinned SDK rather than because of Cairn:
P-55 names three traps in ``mcp/server/lowlevel/server.py``, each of
which silently breaks a decision if the handler is written the obvious
way. One test per trap, each asserting the Cairn answer *and* the absence
of the SDK's. Where a claim is "MCP behaves as REST does", the REST body
is fetched from the same running application rather than transcribed.

Module-local fixtures for the reason ``test_mount_auth.py`` records:
``tests`` has no package markers, so this package cannot carry a
``conftest.py`` without mypy refusing a second module of that name.
"""

import json
import sqlite3
from collections.abc import AsyncIterator, Callable
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
from mcp.server.lowlevel import Server

from cairn.authority.credentials import mint_token
from cairn.authority.gate import INTERNAL_ERROR_MESSAGE
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.mcp.server import (
    INSTANCE_TOOL,
    MUTATION_ACTIONS,
    READ_ACTIONS,
)
from cairn.transports.v1.operations import OPERATIONS
from cairn.transports.v1.wire import (
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
    RULE_INVALID_VALUE,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
)

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
GRANT_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

REALM = "acme"
REPO = {"kind": "repo", "identifier": "cairn"}
SCOPE: dict[str, object] = {"realm": REALM, "segments": [REPO]}
# A scope the grant does not reach, for the authorisation-denied case.
OTHER_SCOPE: dict[str, object] = {
    "realm": REALM,
    "segments": [{"kind": "repo", "identifier": "elsewhere"}],
}

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"

KEY_ONE = "aaaaaaaa-1111-4111-8111-111111111111"
KEY_TWO = "bbbbbbbb-2222-4222-8222-222222222222"

Issue = Callable[[], str]


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


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def granted_credential(data_path: Path) -> Issue:
    """One principal with one grant over ``acme/repo:cairn``, and
    credentials against it on demand."""
    timestamp = canonical_timestamp(NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (REALM, timestamp),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                _canonical([{"id": REPO["identifier"], "kind": REPO["kind"]}]),
                _canonical(["ingest", "invalidate", "promote"]),
                "restricted",
                _canonical(["internal", "public", "restricted"]),
                None,
                None,
                canonical_timestamp(FUTURE),
                timestamp,
            ),
        )
        connection.commit()
    ordinals = count(1)

    def issue() -> str:
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
            connection.commit()
        return f"Bearer {minted.text}"

    return issue


@asynccontextmanager
async def running(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client


async def call(
    client: AsyncClient,
    token: str,
    tool: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    """One ``tools/call`` frame, returning the ``CallToolResult``.

    The JSON-RPC envelope is unwrapped and asserted to be a success:
    P-56 puts every outcome of an identified operation in the tool result,
    so an error frame would fail an unrelated-looking assertion later.
    """
    response = await client.post(
        MOUNT_PATH,
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ),
        headers={**MCP_HEADERS, "Authorization": token},
    )
    assert response.status_code == 200, response.text
    frame = response.json()
    assert "error" not in frame, frame
    result: dict[str, Any] = frame["result"]
    return result


def failure_of(result: dict[str, Any]) -> dict[str, Any]:
    """I-88's failure shape, unpacked and checked as it is unpacked.

    ``structuredContent`` is absent rather than null: the SDK would have
    replaced a structureless result with its own error text had the
    handler not built the result object itself (P-55).
    """
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert len(result["content"]) == 1
    body: dict[str, Any] = json.loads(result["content"][0]["text"])
    failure: dict[str, Any] = body["failure"]
    return failure


def structured(result: dict[str, Any]) -> dict[str, Any]:
    """I-85's success shape: the structured object, plus the assertion
    that the single text block is that object serialised."""
    assert result["isError"] is False
    assert len(result["content"]) == 1
    content = result["content"][0]
    assert content["type"] == "text"
    payload: dict[str, Any] = result["structuredContent"]
    assert json.loads(content["text"]) == payload
    return payload


def catalogue_bytes(data_path: Path) -> bytes:
    return (data_path / CATALOGUE_FILENAME).read_bytes()


def rows(data_path: Path, table: str) -> int:
    with sqlite3.connect(data_path / CATALOGUE_FILENAME) as connection:
        count_row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    total: int = count_row[0]
    return total


def audit_action_codes(data_path: Path) -> list[str]:
    with sqlite3.connect(data_path / CATALOGUE_FILENAME) as connection:
        found = connection.execute(
            "SELECT canonical_event FROM audit_events ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0])["action_code"] for row in found]


def ingest_arguments(key: str, scope: dict[str, object] = SCOPE) -> dict[str, object]:
    return {
        "scope": scope,
        "classification": "internal",
        "source_type": "human",
        "requested_trust": "candidate",
        "facts": [
            {"body": "the gate is an anyio lock", "valid_from": None, "valid_to": None}
        ],
        "observed_at": None,
        "metadata": None,
        "evidence_payload": None,
        "idempotency_key": key,
    }


@pytest.mark.anyio
async def test_a_mutation_commits_and_returns_the_i72_envelope(tmp_path: Path) -> None:
    """The whole path, end to end: a tool call reaches the authority, the
    catalogue changes, and the caller gets the I-72 envelope."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()

    async with running(build_application(config)) as client:
        result = await call(client, token, "ingest", ingest_arguments(KEY_ONE))

    payload = structured(result)
    assert payload["outcome"] == "committed"
    assert len(payload["result"]["fact_ids"]) == 1
    assert UUID(payload["mutation_receipt"]["mutation_id"])
    assert payload["audit_receipt"]["chain_kind"] == "realm"
    assert rows(config.paths.data, "facts") == 1
    assert rows(config.paths.data, "assertions") == 1
    assert audit_action_codes(config.paths.data) == ["ingest"]


@pytest.mark.anyio
async def test_replaying_the_key_returns_the_original_receipt(tmp_path: Path) -> None:
    """I-27 through the argument rather than the header: the second call
    is a replay, not a second commit."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()

    async with running(build_application(config)) as client:
        first = structured(
            await call(client, token, "ingest", ingest_arguments(KEY_ONE))
        )
        second = structured(
            await call(client, token, "ingest", ingest_arguments(KEY_ONE))
        )

    assert first["outcome"] == "committed"
    assert second["outcome"] == "replayed"
    assert second["result"] == first["result"]
    assert second["mutation_receipt"] == first["mutation_receipt"]
    assert rows(config.paths.data, "facts") == 1


@pytest.mark.anyio
async def test_an_unknown_argument_is_unknown_field_and_not_jsonschema_prose(
    tmp_path: Path,
) -> None:
    """P-55's first SDK trap.

    With the SDK's input validation left on, this call returns prose with
    no rule identity, no field path and the caller's own key quoted into a
    string I-32 governs. The absence assertions are the point.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()
    arguments = {**ingest_arguments(KEY_ONE), "surprise": "extra"}

    async with running(build_application(config)) as client:
        result = await call(client, token, "ingest", arguments)

    failure = failure_of(result)
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"field_path": "surprise", "rule": RULE_UNKNOWN_FIELD}
    text = result["content"][0]["text"]
    assert "Input validation error" not in text
    assert "jsonschema" not in text
    # Nothing committed: the refusal is before the application call.
    assert rows(config.paths.data, "facts") == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutate", "rule", "field_path"),
    [
        pytest.param(
            lambda arguments: {
                name: value
                for name, value in arguments.items()
                if name != "idempotency_key"
            },
            RULE_IDEMPOTENCY_KEY_MISSING,
            "idempotency_key",
            id="missing",
        ),
        pytest.param(
            lambda arguments: {**arguments, "idempotency_key": KEY_ONE.upper()},
            RULE_IDEMPOTENCY_KEY_MALFORMED,
            "idempotency_key",
            id="uppercase",
        ),
        pytest.param(
            lambda arguments: {**arguments, "idempotency_key": 7},
            RULE_IDEMPOTENCY_KEY_MALFORMED,
            "idempotency_key",
            id="not-a-string",
        ),
        pytest.param(
            lambda arguments: {**arguments, "classification": "nonsense"},
            RULE_INVALID_VALUE,
            "classification",
            id="closed-vocabulary",
        ),
        pytest.param(
            lambda arguments: {
                **arguments,
                "facts": [
                    {"body": "x", "valid_from": "not a timestamp", "valid_to": None}
                ],
            },
            RULE_INVALID_VALUE,
            "facts[0].valid_from",
            id="nested-field-path",
        ),
        pytest.param(
            lambda arguments: {
                name: value for name, value in arguments.items() if name != "scope"
            },
            RULE_MISSING_FIELD,
            "scope",
            id="missing-scope",
        ),
    ],
)
async def test_a_malformed_argument_carries_its_rule_and_field_path(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], dict[str, object]],
    rule: str,
    field_path: str,
) -> None:
    """I-85's key handling and I-71's model. The key's rule identities are
    the strings ``require_idempotency_key`` raises for the header."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()

    async with running(build_application(config)) as client:
        result = await call(client, token, "ingest", mutate(ingest_arguments(KEY_ONE)))

    failure = failure_of(result)
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {"field_path": field_path, "rule": rule}
    assert rows(config.paths.data, "facts") == 0


@pytest.mark.anyio
async def test_a_stable_failure_is_an_error_result_the_sdk_does_not_replace(
    tmp_path: Path,
) -> None:
    """P-55's second SDK trap.

    By inspection of ``mcp/server/lowlevel/server.py:566-570`` the SDK
    replaces a result carrying no structured content with its own "Output
    validation error" text — which is the exact shape I-88 requires of a
    failure. What proves the handler short-circuited it is that the text
    block is Cairn's envelope rather than the SDK's sentence.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()

    async with running(build_application(config)) as client:
        result = await call(
            client, token, "ingest", ingest_arguments(KEY_ONE, OTHER_SCOPE)
        )

    failure = failure_of(result)
    assert failure["code"] == "authorisation_denied"
    assert failure["retry"] == "never"
    assert "Output validation error" not in result["content"][0]["text"]
    assert rows(config.paths.data, "facts") == 0
    # The denial is on the chain, as it is over REST: a refusal is an
    # event, not a silence.
    assert audit_action_codes(config.paths.data) == ["ingest"]


@pytest.mark.anyio
async def test_a_raising_inner_call_is_the_safe_internal_error_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-55's third SDK trap.

    By inspection of ``mcp/server/lowlevel/server.py:589-590`` an escaping
    exception becomes ``_make_error_result(str(e))``. This one quotes a
    scope path and a fact body, so the assertion that neither reaches the
    caller is the assertion that the handler is total.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()
    leak = "acme/repo:cairn the gate is an anyio lock"

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError(leak)

    application = build_application(config)
    monkeypatch.setattr(
        "cairn.authority.mutations.CairnAuthority.ingest", explode, raising=True
    )

    async with running(application) as client:
        result = await call(client, token, "ingest", ingest_arguments(KEY_ONE))

    failure = failure_of(result)
    assert failure["code"] == "internal_error"
    assert failure["message"] == INTERNAL_ERROR_MESSAGE
    assert failure["retry"] == "never"
    assert "detail" not in failure
    text = result["content"][0]["text"]
    assert leak not in text
    assert "RuntimeError" not in text


@pytest.mark.anyio
async def test_a_failing_preamble_is_the_safe_envelope_not_the_sdks_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-55's totality rule has no preamble exemption (Val's gate review
    of ``5b6a5ec``, finding 4).

    The request-context and correlation-state lookups ran before the
    catch-all, on the invariant that the middleware always stamps them —
    true of every built application, which is exactly the kind of claim
    the guard exists not to depend on. Here the invariant is broken at
    its root: ``Server.request_context`` itself raises, as it genuinely
    does outside a request context, carrying a string no caller may see.
    The answer must be the safe envelope under the nil correlation
    identifier — no request identity survived, and the envelope says so —
    not the SDK's stringification of the exception.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()
    leak = "context lookup leaked acme/repo:cairn"

    def explode(self: object) -> object:
        raise LookupError(leak)

    application = build_application(config)
    monkeypatch.setattr(Server, "request_context", property(explode), raising=True)

    async with running(application) as client:
        result = await call(client, token, "instance", {})

    failure = failure_of(result)
    assert failure["code"] == "internal_error"
    assert failure["correlation_id"] == str(UUID(int=0))
    text = result["content"][0]["text"]
    assert leak not in text
    assert "LookupError" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool", sorted(entry.tool for entry in OPERATIONS if not entry.mutation)
)
async def test_the_idempotency_key_is_forbidden_on_every_read_tool(
    tmp_path: Path, tool: str
) -> None:
    """I-85's read clause, with REST's rule identity for the header.

    On all three read tools, because the rule is the adapter's and is
    derived from the shared table's mutation flag, so a twelfth read
    inherits it.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()

    async with running(build_application(config)) as client:
        result = await call(client, token, tool, {"idempotency_key": KEY_ONE})

    failure = failure_of(result)
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {
        "field_path": "idempotency_key",
        "rule": RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    }


@pytest.mark.anyio
async def test_the_mcp_failure_body_is_the_rest_failure_body(tmp_path: Path) -> None:
    """I-86 and I-90: one refusal, described once. Both transports are
    driven on one running instance and their public bodies compared, minus
    the correlation identifier I-90 exempts."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    mcp_token = issue()
    rest_token = issue()
    arguments = {**ingest_arguments(KEY_ONE), "surprise": "extra"}
    body = {
        name: value for name, value in arguments.items() if name != "idempotency_key"
    }

    async with running(build_application(config)) as client:
        mcp_result = await call(client, mcp_token, "ingest", arguments)
        rest: HTTPXResponse = await client.post(
            "/v1/ingest",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": rest_token,
                "Idempotency-Key": KEY_TWO,
            },
        )

    assert rest.status_code == 400
    mcp_failure = json.loads(mcp_result["content"][0]["text"])["failure"]
    rest_failure = rest.json()["failure"]
    del mcp_failure["correlation_id"], rest_failure["correlation_id"]
    assert mcp_failure == rest_failure


@pytest.mark.anyio
async def test_a_secret_in_an_addressing_field_is_screened_at_the_boundary(
    tmp_path: Path,
) -> None:
    """I-87 and P-27 on the MCP mutation path.

    The screen is shared, so what this holds is that the handler *calls*
    it where REST calls it: after validation, so the field path names
    something a caller recognises, and before the application, so nothing
    lands.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)
    token = issue()
    hostile: dict[str, object] = {
        "realm": REALM,
        "segments": [{"kind": "repo", "identifier": AWS_EXAMPLE_KEY}],
    }

    async with running(build_application(config)) as client:
        result = await call(client, token, "ingest", ingest_arguments(KEY_ONE, hostile))

    failure = failure_of(result)
    assert failure["code"] == "secret_rejected"
    assert failure["detail"] == {
        "policy": "cairn.secret/v1",
        "rule": "cairn.secret/v1/upstream/AWSKeyDetector",
        "field_path": "scope.segments[0].identifier",
    }
    assert rows(config.paths.data, "facts") == 0
    assert rows(config.paths.data, "assertions") == 0
    assert AWS_EXAMPLE_KEY.encode() not in catalogue_bytes(config.paths.data)
    assert AWS_EXAMPLE_KEY not in result["content"][0]["text"]


# The unknown-tool-name case moved to ``test_errors.py`` with Task 9: it
# is a P-56 protocol fault answered by a JSON-RPC error object before the
# SDK is reached, not an outcome of an identified operation, so it is no
# longer a ``tools/call`` result this module can unwrap.


def test_the_audit_identity_is_the_rest_routes() -> None:
    """The eight mutations' audit identity, transcribed from the literals
    ``rest/v1/routes.py`` passes rather than read from the table under
    test. A divergence here is false provenance on the hash chain, not a
    wire difference a caller could report."""
    assert MUTATION_ACTIONS == {
        "ingest": ("ingest", ActionKind.DATA),
        "promote": ("promote", ActionKind.DATA),
        "invalidate": ("invalidate", ActionKind.DATA),
        "create-principal": ("create-principal", ActionKind.ADMINISTRATION),
        "issue-credential": ("issue-credential", ActionKind.ADMINISTRATION),
        "revoke-credential": ("revoke-credential", ActionKind.ADMINISTRATION),
        "create-grant": ("create-grant", ActionKind.ADMINISTRATION),
        "revoke-grant": ("revoke-grant", ActionKind.ADMINISTRATION),
    }
    assert set(MUTATION_ACTIONS) == {
        entry.tool for entry in OPERATIONS if entry.mutation
    }


def test_the_read_audit_identity_is_the_rest_routes() -> None:
    """The same claim for the two reads that screen addressing.

    ``audit-read`` is deliberately not the tool name: ``routes.py:298``
    passes it, so recording ``read-audit-events`` would put a second
    spelling of one operation on the chain. The three reads together are
    checked against the table, so a twelfth cannot be added silently.
    """
    assert READ_ACTIONS == {
        "read-audit-events": ("audit-read", ActionKind.ADMINISTRATION),
        "read-evidence": ("read-evidence", ActionKind.DATA),
        "retrieve": ("retrieve", ActionKind.DATA),
    }
    assert set(READ_ACTIONS) | {INSTANCE_TOOL} == {
        entry.tool for entry in OPERATIONS if not entry.mutation
    }
