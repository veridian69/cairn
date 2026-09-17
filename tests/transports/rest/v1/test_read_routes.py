"""Task 9: the two read routes end-to-end.

`POST /v1/read-audit-events` — pagination, the self-appending-read
property, `AUDIT-02`-shaped prefix confinement and `AUTH-07` for
``audit-read`` at the wire — and `GET /v1/instance` with the I-29
identity triple. P-28's read-route idempotency-key prohibition is proven
on both.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

from cairn import __version__
from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.transports.mcp.manifest import packaged_manifest_bytes
from cairn.transports.rest.v1.openapi import packaged_contract_bytes

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
IDEMPOTENCY_KEY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

REPO = {"kind": "repository", "identifier": "acme-repo"}
OTHER = {"kind": "repository", "identifier": "other-repo"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_config(data_path: Path) -> CairnConfig:
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
    )


def seed(
    config: CairnConfig,
    *,
    operations: list[str],
    segments: list[dict[str, str]],
) -> str:
    """Seeds the realm, one principal and one grant carrying exactly the
    given operations at the given segments, returning the bearer token."""
    migrate_catalogue(config, lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (REALM, TS),
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
            (str(PRINCIPAL_ID), "workload", "reader", TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, TS, None),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                "55555555-5555-4555-8555-555555555555",
                str(PRINCIPAL_ID),
                REALM,
                _canonical(
                    [{"id": s["identifier"], "kind": s["kind"]} for s in segments]
                ),
                _canonical(sorted(operations)),
                "restricted",
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


async def read_events(
    client: AsyncClient,
    token: str,
    *,
    scope_prefix: list[dict[str, str]],
    after_sequence: int = 0,
    limit: int | None = None,
    idempotency_key: str | None = None,
) -> HTTPXResponse:
    body: dict[str, object] = {
        "realm_id": REALM,
        "scope_prefix": scope_prefix,
        "after_sequence": after_sequence,
    }
    if limit is not None:
        body["limit"] = limit
    return await client.post(
        "/v1/read-audit-events",
        content=json.dumps(body),
        headers=headers_for(token, idempotency_key=idempotency_key),
    )


async def ingest(
    client: AsyncClient,
    token: str,
    *,
    segments: list[dict[str, str]],
    body_text: str,
    key: str,
) -> HTTPXResponse:
    return await client.post(
        "/v1/ingest",
        content=json.dumps(
            {
                "scope": {"realm": REALM, "segments": segments},
                "classification": "internal",
                "source_type": "agent-claim",
                "facts": [{"body": body_text}],
            }
        ),
        headers=headers_for(token, idempotency_key=key),
    )


@pytest.mark.anyio
async def test_the_audit_read_pages_and_appends_its_own_event(
    tmp_path: Path,
) -> None:
    """The read returns canonical event documents in sequence order, pages
    through ``after_sequence``, and each successful read appends its own
    ``allow`` event — the self-appending property the model description
    records."""
    config = make_config(tmp_path)
    token = seed(config, operations=["ingest", "audit-read"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            for index in range(3):
                response = await ingest(
                    client,
                    token,
                    segments=[REPO],
                    body_text=f"fact number {index}",
                    key=f"{index:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                )
                assert response.status_code == 200

            first = await read_events(client, token, scope_prefix=[REPO], limit=2)
            assert first.status_code == 200
            page = first.json()
            assert len(page["events"]) == 2
            assert page["next_after_sequence"] is not None
            first_docs = page["events"]
            assert all(doc["schema"] == "cairn.audit/v1" for doc in first_docs)
            assert all(doc["outcome"] == "allow" for doc in first_docs)

            second = await read_events(
                client,
                token,
                scope_prefix=[REPO],
                after_sequence=page["next_after_sequence"],
                limit=500,
            )
            assert second.status_code == 200
            remaining = second.json()["events"]
            # The three ingests plus the first read's own event, minus the
            # two already returned: the first read is visible to the second.
            sequences = [doc["sequence"] for doc in first_docs + remaining]
            assert sequences == sorted(sequences)
            actions = [doc["action_code"] for doc in first_docs + remaining]
            assert "audit-read" in actions


@pytest.mark.anyio
async def test_the_audit_read_is_confined_to_the_grant_prefix(
    tmp_path: Path,
) -> None:
    """`AUDIT-02`-shaped confinement at the wire: a reader granted at the
    repository prefix cannot read outside it, and events for a sibling
    scope never appear in an in-prefix read."""
    config = make_config(tmp_path)
    token = seed(
        config,
        operations=["ingest", "audit-read"],
        segments=[REPO],
    )
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            # This denial durably appends a realm-chain DENY event whose
            # requested_scope is the sibling — the sibling-scope event the
            # in-prefix read below must then exclude. Incidental seeding,
            # made explicit here so the assertion reads as exercised.
            outside = await read_events(client, token, scope_prefix=[OTHER])
            assert outside.status_code == 403
            assert outside.json()["failure"]["code"] == "authorisation_denied"

            ingested = await ingest(
                client,
                token,
                segments=[REPO],
                body_text="an in-prefix fact",
                key=IDEMPOTENCY_KEY,
            )
            assert ingested.status_code == 200
            inside = await read_events(client, token, scope_prefix=[REPO])
            assert inside.status_code == 200
            for doc in inside.json()["events"]:
                requested = doc["requested_scope"]
                if requested is not None:
                    identifiers = [segment["id"] for segment in requested["segments"]]
                    assert "other-repo" not in identifiers


@pytest.mark.anyio
async def test_audit_read_without_the_operation_is_denied(tmp_path: Path) -> None:
    """`AUTH-07` for ``audit-read``: a valid credential whose grant lacks
    the operation is denied coarsely."""
    config = make_config(tmp_path)
    token = seed(config, operations=["ingest"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            response = await read_events(client, token, scope_prefix=[REPO])

    assert response.status_code == 403
    assert response.json()["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
async def test_an_idempotency_key_on_either_read_route_is_refused(
    tmp_path: Path,
) -> None:
    """P-28 at the wire: the header is forbidden on both read routes."""
    config = make_config(tmp_path)
    token = seed(config, operations=["audit-read"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            on_audit_read = await read_events(
                client,
                token,
                scope_prefix=[REPO],
                idempotency_key=IDEMPOTENCY_KEY,
            )
            on_instance = await client.get(
                "/v1/instance",
                headers=headers_for(token, idempotency_key=IDEMPOTENCY_KEY),
            )

    for response in (on_audit_read, on_instance):
        assert response.status_code == 400
        assert response.json()["failure"]["detail"] == {
            "field_path": "Idempotency-Key",
            "rule": "idempotency_key_forbidden",
        }


@pytest.mark.anyio
async def test_an_unauthenticated_audit_read_is_denied(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed(config, operations=["audit-read"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            response = await client.post(
                "/v1/read-audit-events",
                content=json.dumps({"realm_id": REALM, "scope_prefix": [REPO]}),
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_a_secret_bearing_scope_prefix_is_screened(tmp_path: Path) -> None:
    """The boundary screen covers ``scope_prefix`` through the real stack
    — the amended I-74 obligation for this adapter's read path."""
    config = make_config(tmp_path)
    token = seed(config, operations=["audit-read"], segments=[REPO])
    application = build_application(config)
    # See tests/transports/v1/test_auth.py for this literal's provenance.
    hostile = {"kind": "repository", "identifier": "AKIAIOSFODNN7EXAMPLE"}

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            response = await read_events(client, token, scope_prefix=[hostile])

    assert response.status_code == 400
    failure = response.json()["failure"]
    assert failure["code"] == "secret_rejected"
    assert failure["detail"]["field_path"] == "scope_prefix[0].identifier"


@pytest.mark.anyio
async def test_limit_bounds_are_refused_by_the_application(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    token = seed(config, operations=["audit-read"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            zero = await read_events(client, token, scope_prefix=[REPO], limit=0)
            excessive = await read_events(client, token, scope_prefix=[REPO], limit=501)

    for response in (zero, excessive):
        assert response.status_code == 400
        assert response.json()["failure"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_the_instance_route_reports_the_contract_identity(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    token = seed(config, operations=["audit-read"], segments=[REPO])
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            authenticated = await client.get(
                "/v1/instance", headers={"Authorization": f"Bearer {token}"}
            )
            unauthenticated = await client.get("/v1/instance")

    assert authenticated.status_code == 200
    body = authenticated.json()
    assert body["instance_id"] == str(INSTANCE_ID)
    assert body["product_version"] == __version__
    assert body["contract_identity"] == "cairn/v1"
    # I-76: the SHA-256 of the packaged artefact's bytes, not a value
    # derived from anything the request supplied.
    assert body["contract_digest"] == sha256(packaged_contract_bytes()).hexdigest()
    # I-89's added field, on the same terms: the SHA-256 of the packaged
    # manifest's bytes, and never a digest of the pair — ``contract_digest``
    # keeps its I-76 meaning so a consumer that pinned it is unaffected.
    assert body["mcp_contract_digest"] == sha256(packaged_manifest_bytes()).hexdigest()
    assert body["contract_digest"] != body["mcp_contract_digest"]
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
