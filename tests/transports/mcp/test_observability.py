"""Task 10: transport-dimensioned observability (P-57, I-84).

I-84 adds one low-cardinality `transport` dimension and requires the MCP
adapter to supply the operation label, because the mount is a single ASGI
route serving eleven operations. P-57 pins *how*: the foundation
middleware stays the single emission point and the adapter signals to it
through ASGI scope state, so correlation identity, duration measurement
and the safe-log shape are inherited whole rather than reimplemented on a
second path.

Three claims are held here, and the third is the one that decides whether
the dimension is worth having.

- **One request, one record.** A second emitter would be the obvious way
  to label MCP, and it would double-count every request. Asserted by
  counting `request_completed` events rather than by inspecting them.
- **The labels are right on both surfaces.** The same `ingest`, over MCP
  and over REST, differing in exactly one label.
- **A refusal is still labelled.** An MCP request that never resolves a
  tool — an authentication denial, a protocol fault — is still an MCP
  request. If the transport were written at tool resolution, as the
  shortest reading of P-57 suggests, every MCP refusal would be filed
  under REST. The mount writes it on entry instead, and
  `test_an_mcp_refusal_is_still_labelled_mcp` is what holds that.

The safe-log rules are re-asserted rather than assumed: I-32 keeps scope
paths, fact bodies, tool arguments and credentials out of the log line and
the metric, and adding a dimension is exactly the sort of change that
could carry one in. Task 13's leak sweep re-proves it across the corpus;
this is the per-request guard.

Module-local fixtures for the reason `test_mount_auth.py` gives: the
`tests` tree has no package markers, so this package cannot carry a
`conftest.py` without mypy refusing a second module of that name.
"""

import json
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

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import FailureCode
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.mount import MOUNT_PATH

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
GRANT_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

# Deliberately not ``acme``/``cairn`` as the sibling modules use. These
# values double as leak sentinels below, and ``cairn`` appears in every
# metric name — an assertion that it is absent from the exposition would
# fail on the product's own prefix, and one that tolerated it would prove
# nothing. A sentinel must be a string that cannot legitimately occur.
REALM = "zephyr"
REPO = {"kind": "repo", "identifier": "quicksilver"}
SCOPE: dict[str, object] = {"realm": REALM, "segments": [REPO]}
FACT_BODY = "the gate is an anyio lock"

KEY_MCP = "aaaaaaaa-1111-4111-8111-111111111111"
KEY_REST = "bbbbbbbb-2222-4222-8222-222222222222"

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

Issue = Callable[[], str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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


def granted_credential(data_path: Path) -> Issue:
    """One principal with one grant over ``acme/repo:cairn``, and
    credentials against it on demand — ``test_calls.py``'s fixture."""
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


def ingest_body() -> dict[str, object]:
    return {
        "scope": SCOPE,
        "classification": "internal",
        "source_type": "human",
        "requested_trust": "candidate",
        "facts": [{"body": FACT_BODY, "valid_from": None, "valid_to": None}],
        "observed_at": None,
        "metadata": None,
        "evidence_payload": None,
    }


def request_records(captured: str) -> list[dict[str, Any]]:
    """Every ``request_completed`` line the safe logger wrote.

    Parsed from the real formatted output on stderr rather than from the
    payload object, because the claim is about what an operator actually
    receives — a field that never survived formatting would pass an
    assertion made one layer earlier.
    """
    records: list[dict[str, Any]] = []
    for line in captured.splitlines():
        if not line.startswith("{"):
            continue
        document = json.loads(line)
        if document.get("event") == "request_completed":
            records.append(document)
    return records


async def mcp_ingest(client: AsyncClient, token: str) -> None:
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "ingest",
            "arguments": {**ingest_body(), "idempotency_key": KEY_MCP},
        },
    }
    response = await client.post(
        MOUNT_PATH,
        content=json.dumps(frame),
        headers={**MCP_HEADERS, "Authorization": token},
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


async def rest_ingest(client: AsyncClient, token: str) -> None:
    response = await client.post(
        "/v1/ingest",
        content=json.dumps(ingest_body()),
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
            "Idempotency-Key": KEY_REST,
        },
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_an_mcp_ingest_labels_the_operation_and_the_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-84's requirement, end to end: the mount is one route, so without
    the adapter's signal this request would carry no operation at all.

    The count assertion is P-57's "the middleware stays the single
    emission point" made mechanical — a second emitter on the MCP side is
    the obvious implementation and it double-counts.
    """
    config = make_config(tmp_path)
    token = granted_credential(config.paths.data)()

    async with running(build_application(config)) as client:
        await mcp_ingest(client, token)

    records = request_records(capsys.readouterr().err)
    assert len(records) == 1
    assert records[0]["operation"] == "ingest"
    assert records[0]["transport"] == "mcp"
    assert records[0]["outcome_code"] == "success"


@pytest.mark.anyio
async def test_a_rest_ingest_labels_the_same_operation_on_the_other_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the pair. One operation, two surfaces, one label
    different — which is the whole point of the dimension: an MCP
    regression must not be invisible behind REST's traffic."""
    config = make_config(tmp_path)
    token = granted_credential(config.paths.data)()

    async with running(build_application(config)) as client:
        await rest_ingest(client, token)

    records = request_records(capsys.readouterr().err)
    assert len(records) == 1
    assert records[0]["operation"] == "ingest"
    assert records[0]["transport"] == "rest"


@pytest.mark.anyio
async def test_the_two_transports_differ_in_exactly_one_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compared against each other on one running instance rather than
    against literals, so the two cannot drift together into agreeing about
    something wrong.

    Timestamp, duration and correlation identity are per request and are
    dropped before the comparison; everything else must match, because the
    same operation over two surfaces differs in the transport and nothing
    else.
    """
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)

    async with running(build_application(config)) as client:
        await mcp_ingest(client, issue())
        await rest_ingest(client, issue())

    mcp_record, rest_record = request_records(capsys.readouterr().err)
    for record in (mcp_record, rest_record):
        record.pop("duration_ms")
        record.pop("correlation_id")
        record.pop("time")

    assert mcp_record.pop("transport") == "mcp"
    assert rest_record.pop("transport") == "rest"
    assert mcp_record == rest_record


@pytest.mark.anyio
async def test_an_mcp_refusal_is_still_labelled_mcp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case the shortest reading of P-57 would get wrong.

    An authentication denial and a protocol fault both fail before any
    tool is resolved, so an adapter that wrote the transport at tool
    resolution would leave them labelled ``rest`` — filing MCP's refusals
    under REST's series, which is the one confusion this dimension exists
    to prevent. The mount writes the label on entry, before anything it
    serves can fail.

    No operation is asserted because none was identified; that is the
    honest answer and the metric is correspondingly not emitted.
    """
    config = make_config(tmp_path)
    granted_credential(config.paths.data)

    async with running(build_application(config)) as client:
        unauthenticated = await client.post(
            MOUNT_PATH,
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers=MCP_HEADERS,
        )
        assert unauthenticated.status_code == 401

    records = request_records(capsys.readouterr().err)
    assert len(records) == 1
    assert records[0]["transport"] == "mcp"
    assert "operation" not in records[0]


@pytest.mark.anyio
async def test_the_metric_carries_both_transports_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dimension where it is actually consumed. Both series must exist
    and each must count one request; a shared series would satisfy a naive
    "the label is present" assertion while making the two surfaces
    indistinguishable."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)

    async with running(build_application(config)) as client:
        await mcp_ingest(client, issue())
        await rest_ingest(client, issue())
        metrics = await client.get("/metrics")

    capsys.readouterr()
    text = metrics.text
    assert (
        'cairn_http_requests_total{operation="ingest",outcome_code="success",'
        'transport="mcp"} 1.0' in text
    )
    assert (
        'cairn_http_requests_total{operation="ingest",outcome_code="success",'
        'transport="rest"} 1.0' in text
    )


@pytest.mark.anyio
async def test_neither_the_log_nor_the_metric_carries_caller_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-32, re-asserted at the point a new dimension was added.

    The scope path, the fact body, the tool argument object and the
    credential are each things the adapter now handles on the way to
    resolving a label, so each is a plausible thing to have carried into
    the record by accident. The realm and segment identifiers are
    asserted absent from the *log line*; ``ingest`` as an operation label
    is expected and is not what is being looked for.
    """
    config = make_config(tmp_path)
    token = granted_credential(config.paths.data)()

    async with running(build_application(config)) as client:
        await mcp_ingest(client, token)
        metrics = await client.get("/metrics")

    captured = capsys.readouterr().err
    # The first record is the ingest; scraping ``/metrics`` is itself a
    # request and logs one of its own.
    record = request_records(captured)[0]
    rendered = json.dumps(record)

    for secret in (FACT_BODY, REALM, str(REPO["identifier"]), token, KEY_MCP):
        assert secret not in rendered
        assert secret not in metrics.text
    # The whole argument object, not just its parts.
    assert "scope" not in rendered
    assert "arguments" not in rendered


# A scope the grant does not reach, and a body the secret policy refuses:
# two identified tool failures, which answer HTTP 200 with the outcome
# inside the result (I-88).
OTHER_SCOPE: dict[str, object] = {
    "realm": REALM,
    "segments": [{"kind": "repo", "identifier": "elsewhere"}],
}
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"


async def mcp_call(
    client: AsyncClient, token: str, tool: str, arguments: dict[str, object]
) -> dict[str, Any]:
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
    # The status is asserted to be 200 rather than merely read: it is the
    # premise of every assertion below. A failure that arrived as a 4xx
    # would be labelled correctly by the status alone, and would prove
    # nothing about the signal.
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


@pytest.mark.parametrize(
    ("label", "arguments", "expected_code", "expected_outcome"),
    [
        (
            "authorisation_denied",
            {"scope": OTHER_SCOPE},
            "authorisation_denied",
            "invalid_request",
        ),
        (
            "secret_rejected",
            {"facts": [{"body": f"the key is {AWS_EXAMPLE_KEY}"}]},
            "secret_rejected",
            "invalid_request",
        ),
    ],
)
@pytest.mark.anyio
async def test_an_identified_tool_failure_is_not_recorded_as_a_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
    arguments: dict[str, object],
    expected_code: str,
    expected_outcome: str,
) -> None:
    """The defect this signal closes, one failure class at a time.

    An identified tool answers HTTP 200 whatever it decided, so a
    middleware reading the status alone recorded every MCP failure —
    an authorisation denial, a secret rejection, an internal error — as
    ``outcome_code="success"``. The entire MCP failure series was
    invisible, in the log and in the metric alike, while the audit chain
    recorded the denials correctly: an observability defect, not a
    disclosure one, found by a bot review of PR #9 on 11 August 2026 and
    reproduced before it was touched.
    """
    config = make_config(tmp_path)
    token = granted_credential(config.paths.data)()

    async with running(build_application(config)) as client:
        result = await mcp_call(
            client,
            token,
            "ingest",
            {**ingest_body(), **arguments, "idempotency_key": KEY_MCP},
        )
        metrics = await client.get("/metrics")

    assert result["isError"] is True, label
    disclosed = json.loads(result["content"][0]["text"])
    assert disclosed["failure"]["code"] == expected_code, label

    record = request_records(capsys.readouterr().err)[0]
    assert record["operation"] == "ingest", label
    assert record["transport"] == "mcp", label
    assert record["outcome_code"] == expected_outcome, label
    assert (
        f'cairn_http_requests_total{{operation="ingest",'
        f'outcome_code="{expected_outcome}",transport="mcp"}} 1.0' in metrics.text
    ), label
    assert (
        'cairn_http_requests_total{operation="ingest",outcome_code="success",'
        'transport="mcp"}' not in metrics.text
    ), label


@pytest.mark.anyio
async def test_the_two_transports_label_one_failure_alike(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same denial over both surfaces, compared against each other
    rather than against a literal — the shape
    ``test_the_two_transports_differ_in_exactly_one_field`` uses for the
    success case, which is exactly the case that hid this defect."""
    config = make_config(tmp_path)
    issue = granted_credential(config.paths.data)

    async with running(build_application(config)) as client:
        await mcp_call(
            client,
            issue(),
            "ingest",
            {**ingest_body(), "scope": OTHER_SCOPE, "idempotency_key": KEY_MCP},
        )
        denied = await client.post(
            "/v1/ingest",
            content=json.dumps({**ingest_body(), "scope": OTHER_SCOPE}),
            headers={
                "Content-Type": "application/json",
                "Authorization": issue(),
                "Idempotency-Key": KEY_REST,
            },
        )
        assert denied.status_code == 403

    mcp_record, rest_record = request_records(capsys.readouterr().err)
    for record in (mcp_record, rest_record):
        record.pop("duration_ms")
        record.pop("correlation_id")
        record.pop("time")

    assert mcp_record.pop("transport") == "mcp"
    assert rest_record.pop("transport") == "rest"
    assert mcp_record == rest_record


def test_the_two_transports_label_one_failure_code_alike() -> None:
    """Every stable failure code, mapped both ways, compared.

    MCP cannot consult REST's status table — I-88 keeps it REST's alone
    and P-50 rejects the import — so the two mappings are separate by
    construction and this is what stops them drifting apart. Consulting
    both here is legitimate: a test may import what the source may not,
    and the whole point is to compare them.
    """
    from cairn.transports.mcp.server import OUTCOME_BY_FAILURE_CODE
    from cairn.transports.rest.middleware import _outcome_code
    from cairn.transports.rest.v1.errors import STATUS_BY_FAILURE_CODE

    assert set(OUTCOME_BY_FAILURE_CODE) == set(FailureCode)
    for code in FailureCode:
        assert OUTCOME_BY_FAILURE_CODE[code] == _outcome_code(
            STATUS_BY_FAILURE_CODE[code]
        ), code
