"""Task 9: ``POST /v1/retrieve`` end to end (P-44).

The pipeline's own behaviour is proven at module level in
``tests/authority/test_retrieval.py``; this file proves the *wire*: strict
parsing, the forbidden idempotency key, the bare result shape, the I-73
status mapping for each failure this route can produce, and that a real
hit round-trips through the composed application with the in-memory index
the composition builds for a test-mode instance.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.projection.memory import MemoryIndex
from cairn.runtime.composition import build_application
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
IDEMPOTENCY_KEY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
REPO = {"kind": "repository", "identifier": "acme-repo"}

_ALL_DATA_OPERATIONS = ["ingest", "invalidate", "promote", "retrieve"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_config(data_path: Path, *, graphiti: bool = True) -> CairnConfig:
    data = data_path / "data"
    credentials = data_path / "credentials"
    data.mkdir(exist_ok=True)
    credentials.mkdir(exist_ok=True)
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
        attic=AtticConfig(enabled=False),
        graphiti=GraphitiConfig(enabled=graphiti),
    )


def seed(
    config: CairnConfig,
    *,
    operations: list[str] | None = None,
    segments: list[dict[str, str]] | None = None,
) -> str:
    migrate_catalogue(config, lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)", (REALM, TS)
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'workload', 'retriever', ?)",
            (str(PRINCIPAL_ID), TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, TS),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'restricted', ?, NULL, NULL, ?, ?)",
            (
                "55555555-5555-4555-8555-555555555555",
                str(PRINCIPAL_ID),
                REALM,
                _canonical(
                    [
                        {"id": s["identifier"], "kind": s["kind"]}
                        for s in (segments if segments is not None else [])
                    ]
                ),
                _canonical(
                    sorted(
                        operations if operations is not None else _ALL_DATA_OPERATIONS
                    )
                ),
                _canonical(["internal", "public", "restricted"]),
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()
    return minted.text


def headers_for(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    built = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if idempotency_key is not None:
        built["Idempotency-Key"] = idempotency_key
    return built


async def retrieve(
    client: AsyncClient,
    token: str,
    *,
    body: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> HTTPXResponse:
    payload: dict[str, object] = {
        "scope": {"realm": REALM, "segments": []},
        "query": "build",
        "budget": 4096,
    }
    if body is not None:
        payload.update(body)
    return await client.post(
        "/v1/retrieve",
        content=json.dumps(payload),
        headers=headers_for(token, idempotency_key=idempotency_key),
    )


async def ingest(
    client: AsyncClient,
    token: str,
    *,
    body_text: str,
    key: str,
    segments: list[dict[str, str]] | None = None,
) -> HTTPXResponse:
    return await client.post(
        "/v1/ingest",
        content=json.dumps(
            {
                "scope": {
                    "realm": REALM,
                    "segments": segments if segments is not None else [],
                },
                "classification": "internal",
                "source_type": "agent-claim",
                "facts": [{"body": body_text}],
            }
        ),
        headers=headers_for(token, idempotency_key=key),
    )


@pytest.mark.anyio
async def test_an_ingested_fact_round_trips_through_retrieve(tmp_path: Path) -> None:
    """The whole slice in one request pair: ingest over ``/v1``, project
    through the composed index, retrieve it back with its stored fields."""
    config = make_config(tmp_path)
    token = seed(config)
    index = MemoryIndex()
    application = build_application(config, index_adapter=index)

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            ingested = await ingest(
                client, token, body_text="the build is green", key=IDEMPOTENCY_KEY
            )
            assert ingested.status_code == 200
            fact_id = ingested.json()["result"]["fact_ids"][0]
            assertion_id = ingested.json()["result"]["assertion_id"]
            # Stand in for the delivery loop, deterministically: this test
            # is about the route, and the loop has its own lifecycle tests.
            _drain(config, index)

            response = await retrieve(
                client, token, body={"trust_filters": ["candidate"]}
            )

    assert response.status_code == 200
    document: Any = response.json()
    # A read returns the bare result — no mutation envelope (I-77).
    assert set(document) == {"hits", "budget_consumed", "budget_exhausted"}
    assert len(document["hits"]) == 1
    hit = document["hits"][0]
    assert hit["fact_id"] == fact_id
    assert hit["body"] == "the build is green"
    assert hit["scope"] == {"realm": REALM, "segments": []}
    assert hit["classification"] == "internal"
    assert hit["trust"] == "candidate"
    assert hit["assertion_id"] == assertion_id
    assert hit["derived_from"] is None
    assert hit["promoted_by"] is None
    assert hit["evidence_id"] is None
    assert hit["recorded_at"].endswith("Z")
    assert hit["invalidated_at"] is None
    assert document["budget_consumed"] == len("the build is green")
    assert document["budget_exhausted"] is False


@pytest.mark.anyio
async def test_trust_filters_travel_and_default_to_validated(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    index = MemoryIndex()
    application = build_application(config, index_adapter=index)

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            assert (
                await ingest(
                    client, token, body_text="a candidate claim", key=IDEMPOTENCY_KEY
                )
            ).status_code == 200
            _drain(config, index)

            defaulted = await retrieve(client, token, body={"query": "candidate"})
            widened = await retrieve(
                client,
                token,
                body={"query": "candidate", "trust_filters": ["candidate"]},
            )

    assert defaulted.status_code == 200
    assert defaulted.json()["hits"] == []
    assert widened.status_code == 200
    assert len(widened.json()["hits"]) == 1


@pytest.mark.anyio
async def test_an_idempotency_key_is_refused(tmp_path: Path) -> None:
    """I-27's read rule at the wire, as on the other two read routes."""
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(client, token, idempotency_key=IDEMPOTENCY_KEY)

    assert response.status_code == 400
    assert response.json()["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_an_unauthenticated_retrieve_is_denied(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await client.post(
                "/v1/retrieve",
                content=json.dumps(
                    {
                        "scope": {"realm": REALM, "segments": []},
                        "query": "build",
                        "budget": 4096,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 401
    assert response.json()["failure"]["code"] == "authentication_failed"


@pytest.mark.anyio
async def test_retrieve_without_the_operation_is_denied(tmp_path: Path) -> None:
    """`AUTH-07` shaped: a token with every data operation *except*
    ``retrieve`` cannot read."""
    config = make_config(tmp_path)
    token = seed(config, operations=["ingest", "promote", "invalidate"])
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(client, token)

    assert response.status_code == 403
    assert response.json()["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"query": ""}, id="empty-query"),
        pytest.param({"budget": 0}, id="zero-budget"),
        pytest.param({"budget": 1048577}, id="budget-above-bound"),
        pytest.param({"as_of": "2026-08-07T12:00:00"}, id="naive-as-of"),
    ],
)
async def test_values_the_pipeline_refuses_are_invalid_request(
    tmp_path: Path, body: dict[str, object]
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(client, token, body=body)

    assert response.status_code == 400
    assert response.json()["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"trust_filters": ["speculative"]}, id="unknown-trust"),
        pytest.param(
            {"trust_filters": ["validated", "validated"]}, id="duplicate-trust"
        ),
        pytest.param({"budget": "4096"}, id="budget-as-string"),
        pytest.param({"unexpected": True}, id="unknown-field"),
        pytest.param({"query": None}, id="null-query"),
    ],
)
async def test_strict_parsing_refuses_malformed_bodies(
    tmp_path: Path, body: dict[str, object]
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(client, token, body=body)

    assert response.status_code == 400
    assert response.json()["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_a_secret_bearing_query_is_refused_at_the_wire(tmp_path: Path) -> None:
    """I-31: the query is screened caller content. The failure carries the
    I-72 bounded detail — policy, rule and field path — and never the
    matched text."""
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(
                client, token, body={"query": "-----BEGIN RSA PRIVATE KEY-----"}
            )

    # I-73 maps secret_rejected to 400, not 422: it is a refusal of the
    # request as written, and the caller learns which field from detail.
    assert response.status_code == 400
    failure = response.json()["failure"]
    assert failure["code"] == "secret_rejected"
    assert failure["detail"]["field_path"] == "query"
    assert "BEGIN RSA PRIVATE KEY" not in response.text


@pytest.mark.anyio
async def test_retrieval_is_refused_when_the_index_is_disabled(
    tmp_path: Path,
) -> None:
    """P-48's disabled posture at the wire: ``invalid_request``, never a
    503, because no retry will help."""
    config = make_config(tmp_path, graphiti=False)
    token = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            response = await retrieve(client, token)

    assert response.status_code == 400
    assert response.json()["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_undelivered_projection_work_is_a_503_with_retry_after(
    tmp_path: Path,
) -> None:
    """I-83 at the wire: the index is known to be behind, so the answer is
    the after-delay class with the header a client needs to honour it."""
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config, index_adapter=MemoryIndex())

    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://cairn") as client:
            assert (
                await ingest(
                    client, token, body_text="not yet indexed", key=IDEMPOTENCY_KEY
                )
            ).status_code == 200
            # Deliberately not drained.
            response = await retrieve(client, token)

    assert response.status_code == 503
    assert response.json()["failure"]["code"] == "index_pending"
    assert "Retry-After" in response.headers


def _drain(config: CairnConfig, index: MemoryIndex) -> None:
    """Runs the projection deliverer once, synchronously — the loop's job,
    done here without its timing so the route assertions stay exact."""
    import threading
    from uuid import uuid4

    from cairn.catalogue.transactions import CatalogueTransactions
    from cairn.projection.delivery import deliver_projection_outbox

    deliver_projection_outbox(
        CatalogueTransactions(
            config.paths.data,
            writer_gate=threading.Lock(),
            clock=lambda: NOW,
            uuid_factory=uuid4,
        ),
        index,
        clock=lambda: NOW,
    )
