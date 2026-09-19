import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import anyio
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

import cairn.runtime.composition as composition_module
from cairn.authority.credentials import mint_token
from cairn.authority.gate import Actor
from cairn.authority.mutations import (
    AssertionIngested,
    CairnAuthority,
    IngestAssertion,
)
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import MutationOutcome, _GuardedTransaction
from cairn.runtime.composition import ContractState, build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
GRANT_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
# See tests/transports/v1/test_auth.py for the provenance of this literal.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"

IDEMPOTENCY_KEY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

INGEST_BODY: dict[str, object] = {
    "scope": {
        "realm": REALM,
        "segments": [{"kind": "repository", "identifier": "acme-repo"}],
    },
    "classification": "internal",
    "source_type": "agent-claim",
    "facts": [{"body": "The deploy pipeline uses kaniko."}],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


def _canonical(value: object) -> str:
    """The catalogue's canonical JSON column form — the startup verifier
    refuses anything else."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def seed(config: CairnConfig) -> str:
    """Migrates and seeds a realm, principal, live credential and an ingest
    grant covering the test scope — the ``test_mutations.py`` recipe —
    returning the bearer token."""
    migrate_catalogue(config, lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    data_path = config.paths.data
    with _open_write_connection(data_path, create=False) as connection:
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
            (str(PRINCIPAL_ID), "workload", "worker", TS),
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
                str(GRANT_ID),
                str(PRINCIPAL_ID),
                REALM,
                _canonical([{"id": "acme-repo", "kind": "repository"}]),
                _canonical(["ingest"]),
                "restricted",
                _canonical(["internal", "public", "restricted"]),
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()
    return minted.text


def headers_for(
    token: str, *, idempotency_key: str | None = IDEMPOTENCY_KEY
) -> dict[str, str]:
    built = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if idempotency_key is not None:
        built["Idempotency-Key"] = idempotency_key
    return built


async def post_ingest(
    application: FastAPI,
    *,
    body: dict[str, object],
    headers: dict[str, str],
) -> HTTPXResponse:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/v1/ingest", content=json.dumps(body), headers=headers
        )


def realm_events(data_path: Path) -> list[dict[str, object]]:
    with (
        closing(sqlite3.connect(data_path / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events "
            "WHERE chain_kind = 'realm' ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


@pytest.mark.anyio
async def test_a_committed_ingest_returns_the_full_envelope(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        response = await post_ingest(
            application, body=INGEST_BODY, headers=headers_for(token)
        )

    assert response.status_code == 200
    envelope = response.json()
    assert envelope["outcome"] == "committed"
    result = envelope["result"]
    assert UUID(result["assertion_id"])
    assert len(result["fact_ids"]) == 1
    assert result["evidence_id"] is None
    assert len(envelope["mutation_receipt"]["command_digest"]) == 64
    audit = envelope["audit_receipt"]
    assert audit["chain_kind"] == "realm"
    assert audit["chain_identity"] == REALM
    assert len(audit["recorded_at"]) == 27
    events = realm_events(config.paths.data)
    assert events[-1]["outcome"] == "allow"


@pytest.mark.anyio
async def test_a_replay_returns_the_original_receipt_marked_replayed(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        first = await post_ingest(
            application, body=INGEST_BODY, headers=headers_for(token)
        )
        second = await post_ingest(
            application, body=INGEST_BODY, headers=headers_for(token)
        )

    assert first.status_code == 200
    assert second.status_code == 200
    committed = first.json()
    replayed = second.json()
    assert committed["outcome"] == "committed"
    assert replayed["outcome"] == "replayed"
    assert replayed["result"] == committed["result"]
    assert replayed["mutation_receipt"] == committed["mutation_receipt"]
    # A replay creates a fresh audit event: the envelope's receipt moves on.
    assert (
        replayed["audit_receipt"]["sequence"] > committed["audit_receipt"]["sequence"]
    )


@pytest.mark.anyio
async def test_an_ungranted_principal_is_denied_authorisation(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)
    body = dict(INGEST_BODY)
    body["scope"] = {
        "realm": REALM,
        "segments": [{"kind": "repository", "identifier": "other-repo"}],
    }

    async with LifespanManager(application):
        response = await post_ingest(application, body=body, headers=headers_for(token))

    assert response.status_code == 403
    assert response.json()["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
async def test_wire_admission_failures_hold_through_the_real_application(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)
    surplus = dict(INGEST_BODY)
    surplus["surprise"] = True

    async with LifespanManager(application):
        unknown_field = await post_ingest(
            application, body=surplus, headers=headers_for(token)
        )
        missing_key = await post_ingest(
            application,
            body=INGEST_BODY,
            headers=headers_for(token, idempotency_key=None),
        )
        unauthenticated = await post_ingest(application, body=INGEST_BODY, headers={})
        bad_classification = await post_ingest(
            application,
            body={**INGEST_BODY, "classification": "top-secret"},
            headers=headers_for(token),
        )

    assert unknown_field.status_code == 400
    assert unknown_field.json()["failure"]["detail"] == {
        "field_path": "surprise",
        "rule": "unknown_field",
    }
    assert missing_key.status_code == 400
    assert missing_key.json()["failure"]["detail"]["rule"] == "idempotency_key_missing"
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    assert bad_classification.status_code == 400
    assert bad_classification.json()["failure"]["detail"] == {
        "field_path": "classification",
        "rule": "invalid_value",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "field_path"),
    [
        pytest.param(
            {"scope": {"realm": "ACME!", "segments": []}},
            "scope",
            id="scope-shape",
        ),
        pytest.param(
            {
                "facts": [
                    {
                        "body": "b",
                        "valid_from": "2027-01-01T00:00:00+00:00",
                        "valid_to": "2026-01-01T00:00:00+00:00",
                    }
                ]
            },
            "facts[0]",
            id="validity-window",
        ),
        pytest.param(
            {"facts": [{"body": "b", "valid_from": "2026-01-01T00:00:00"}]},
            "facts[0].valid_from",
            id="naive-timestamp",
        ),
        pytest.param(
            {"observed_at": "yesterday"},
            "observed_at",
            id="unparseable-timestamp",
        ),
    ],
)
async def test_translation_refusals_name_the_field(
    tmp_path: Path,
    mutation: dict[str, object],
    field_path: str,
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        response = await post_ingest(
            application,
            body={**INGEST_BODY, **mutation},
            headers=headers_for(token),
        )

    assert response.status_code == 400
    assert response.json()["failure"]["detail"] == {
        "field_path": field_path,
        "rule": "invalid_value",
    }


@pytest.mark.anyio
async def test_a_secret_bearing_scope_is_rejected_through_the_real_stack(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)
    body = dict(INGEST_BODY)
    body["scope"] = {
        "realm": REALM,
        "segments": [{"kind": "repository", "identifier": AWS_EXAMPLE_KEY}],
    }

    async with LifespanManager(application):
        response = await post_ingest(application, body=body, headers=headers_for(token))

    assert response.status_code == 400
    failure = response.json()["failure"]
    assert failure["code"] == "secret_rejected"
    assert failure["detail"]["field_path"] == "scope.segments[0].identifier"
    catalogue_bytes = (config.paths.data / CATALOGUE_FILENAME).read_bytes()
    assert AWS_EXAMPLE_KEY.encode() not in catalogue_bytes


class _CountingAuthority(CairnAuthority):
    """Records how many ingests run concurrently; the P-29 gate must keep
    the peak at one. The sleep widens the window so an unserialised pair
    would actually overlap."""

    lock = threading.Lock()
    active = 0
    peak = 0

    def ingest(
        self,
        actor: Actor,
        command: IngestAssertion,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
        reauthorise_at_commit: bool = False,
        commit_guard: Callable[[_GuardedTransaction], None] | None = None,
    ) -> MutationOutcome[AssertionIngested]:
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            time.sleep(0.02)
            return super().ingest(
                actor,
                command,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                reauthorise_at_commit=reauthorise_at_commit,
                commit_guard=commit_guard,
            )
        finally:
            with cls.lock:
                cls.active -= 1


@pytest.mark.anyio
async def test_parallel_mutations_serialise_on_the_writer_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-29's concurrency proof: eight parallel ingests through the test
    client all succeed and never overlap inside the gated section. The
    gated section is one synchronous closure, so no transaction can span a
    remote await by construction."""
    monkeypatch.setattr(composition_module, "CairnAuthority", _CountingAuthority)
    _CountingAuthority.active = 0
    _CountingAuthority.peak = 0
    config = make_config(tmp_path)
    token = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            responses: list[HTTPXResponse] = []

            async def submit(index: int) -> None:
                key = f"{index:08x}-0000-4000-8000-000000000000"
                body = dict(INGEST_BODY)
                body["facts"] = [{"body": f"fact number {index}"}]
                response = await client.post(
                    "/v1/ingest",
                    content=json.dumps(body),
                    headers=headers_for(token, idempotency_key=key),
                )
                responses.append(response)

            async with anyio.create_task_group() as group:
                for index in range(8):
                    group.start_soon(submit, index)

    assert [response.status_code for response in responses] == [200] * 8
    assert _CountingAuthority.peak == 1


@pytest.mark.anyio
async def test_readiness_consults_the_contract_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-32 via Task 6's placeholder guard: a not-loaded contract keeps
    readiness at 503 even after a clean startup."""
    monkeypatch.setattr(
        composition_module,
        "_contract_state",
        lambda: ContractState(loaded=False, digest="0" * 64, mcp_digest="1" * 64),
    )
    config = make_config(tmp_path)
    seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            ready = await client.get("/health/ready")
            startup = await client.get("/health/startup")

    assert ready.status_code == 503
    assert startup.status_code == 200
