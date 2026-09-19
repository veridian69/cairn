"""Task 7: the three data routes end-to-end against a real catalogue.

The slice-applicable §6.6 rows proven at the wire here: `MUT-01`–`MUT-10`
mechanics, `SCOPE-06`–`SCOPE-08` and `TRUST-04`/`TRUST-05`, plus batch
atomicity, the failed-approach ingest path and both evidence forms of
promotion. Module-level semantics were accepted with slice 4; these tests
pin their REST mapping, not re-derive them.
"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid1

import pytest
from asgi_lifespan import LifespanManager
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
from cairn.runtime.config import AtticConfig, CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
OUTSIDER_ID = UUID("44444444-4444-4444-8444-444444444444")
OUTSIDER_CREDENTIAL_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
REALM = "acme"
# See tests/transports/v1/test_auth.py for the provenance of this literal.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"

REPO = {"kind": "repository", "identifier": "acme-repo"}
BRANCH = {"kind": "branch", "identifier": "main"}


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
        # Exact evidence on: the payload-bearing ingest path is part of the
        # matrix. Delivery is not wired (Task 12), so outbox rows simply
        # accumulate, which is the documented at-least-once posture.
        attic=AtticConfig(enabled=True),
    )


def _seed_identity(
    connection: sqlite3.Connection,
    principal_id: UUID,
    credential_id: UUID,
    verifier: bytes,
    label: str,
) -> None:
    connection.execute(
        "INSERT INTO principals (principal_id, kind, label, created_at) "
        "VALUES (?, ?, ?, ?)",
        (str(principal_id), "workload", label, TS),
    )
    connection.execute(
        "INSERT INTO credentials "
        "(credential_id, principal_id, verifier, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(credential_id), str(principal_id), verifier, TS, None),
    )


def add_grant(
    config: CairnConfig,
    grant_id: UUID,
    *,
    principal_id: UUID = PRINCIPAL_ID,
    segments: list[dict[str, str]],
    operations: list[str],
    read_clearance: str = "restricted",
    write_classifications: list[str] | None = None,
) -> None:
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                str(grant_id),
                str(principal_id),
                REALM,
                _canonical(
                    [{"id": s["identifier"], "kind": s["kind"]} for s in segments]
                ),
                _canonical(sorted(operations)),
                read_clearance,
                _canonical(
                    sorted(
                        write_classifications or ["internal", "public", "restricted"]
                    )
                ),
                FUTURE_TS,
                TS,
            ),
        )
        connection.commit()


def seed(config: CairnConfig) -> tuple[str, str]:
    """Seeds the realm, two principals with credentials, and one
    full-capability grant at the repository segment for the first —
    returning (worker token, outsider token). The outsider has no grants
    until a test adds one."""
    migrate_catalogue(config, lambda: NOW)
    worker = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    outsider = mint_token(OUTSIDER_CREDENTIAL_ID, lambda count: bytes(count))
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
        _seed_identity(connection, PRINCIPAL_ID, CREDENTIAL_ID, worker.verifier, "w")
        _seed_identity(
            connection, OUTSIDER_ID, OUTSIDER_CREDENTIAL_ID, outsider.verifier, "o"
        )
        connection.commit()
    add_grant(
        config,
        UUID("66666666-6666-4666-8666-666666666666"),
        segments=[REPO],
        operations=["ingest", "retrieve", "promote", "invalidate"],
    )
    return worker.text, outsider.text


class Api:
    """A thin driver: every call is a real POST through the composed app."""

    def __init__(self, client: AsyncClient, token: str) -> None:
        self._client = client
        self._token = token
        self._counter = 0

    def _key(self) -> str:
        self._counter += 1
        return f"{self._counter:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> HTTPXResponse:
        return await self._client.post(
            path,
            content=json.dumps(body),
            headers={
                "Authorization": f"Bearer {token or self._token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key or self._key(),
            },
        )

    async def ingest(
        self,
        *,
        segments: list[dict[str, str]],
        bodies: list[str],
        classification: str = "internal",
        trust: str = "candidate",
        payload: str | None = None,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> HTTPXResponse:
        body: dict[str, object] = {
            "scope": {"realm": REALM, "segments": segments},
            "classification": classification,
            "source_type": "agent-claim",
            "facts": [{"body": text} for text in bodies],
            "requested_trust": trust,
        }
        if payload is not None:
            body["evidence_payload"] = payload
        return await self.post(
            "/v1/ingest", body, token=token, idempotency_key=idempotency_key
        )


EXTERNAL_EVIDENCE = {
    "external_uri": "https://ci.example/run/42",
    "payload_digest": "ab" * 32,
}


def fact_rows(config: CairnConfig) -> list[tuple[str, str, int]]:
    with (
        closing(sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        return connection.execute(
            "SELECT f.fact_id, f.trust, "
            "EXISTS (SELECT 1 FROM fact_invalidations i "
            "WHERE i.fact_id = f.fact_id) "
            "FROM facts f ORDER BY f.fact_id"
        ).fetchall()


@pytest.mark.anyio
async def test_the_data_route_matrix(tmp_path: Path) -> None:
    """One app, one catalogue, the full positive flow: MUT-01/02/03/04,
    both evidence forms, the failed-approach path, and invalidation with
    history preserved."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)

            # MUT-01a: authorised ingest, two facts, one payload.
            ingested = await api.ingest(
                segments=[REPO],
                bodies=["kaniko builds the image", "argo syncs the manifests"],
                payload='{"run": 42}',
            )
            assert ingested.status_code == 200
            result = ingested.json()["result"]
            fact_ids = result["fact_ids"]
            evidence_id = result["evidence_id"]
            assert evidence_id is not None

            # MUT-02: replay creates no duplicates (proven for ingest in
            # Task 6; here for promote below).

            # MUT-03 + evidence-id form: same-scope promotion.
            promoted = await api.post(
                "/v1/promote",
                {
                    "fact_ids": [fact_ids[0]],
                    "evidence": {"evidence_id": evidence_id},
                    "reason": "verified by the pipeline run",
                },
                idempotency_key="99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert promoted.status_code == 200
            envelope = promoted.json()
            assert envelope["outcome"] == "committed"
            pair = envelope["result"]["promotions"][0]
            assert pair["source_fact_id"] == fact_ids[0]
            derived = pair["derived_fact_id"]
            assert derived != fact_ids[0]

            # MUT-02 for promote: identical receipt, replayed outcome.
            replayed = await api.post(
                "/v1/promote",
                {
                    "fact_ids": [fact_ids[0]],
                    "evidence": {"evidence_id": evidence_id},
                    "reason": "verified by the pipeline run",
                },
                idempotency_key="99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert replayed.json()["outcome"] == "replayed"
            assert replayed.json()["mutation_receipt"] == envelope["mutation_receipt"]

            # MUT-04 + external form: ancestor promotion from a deeper
            # scope to the repository ancestor.
            deeper = await api.ingest(
                segments=[REPO, BRANCH], bodies=["the branch builds green"]
            )
            assert deeper.status_code == 200
            ancestor = await api.post(
                "/v1/promote",
                {
                    "fact_ids": [deeper.json()["result"]["fact_ids"][0]],
                    "evidence": EXTERNAL_EVIDENCE,
                    "target_scope": {"realm": REALM, "segments": [REPO]},
                    "reason": "holds at the repository level",
                },
            )
            assert ancestor.status_code == 200

            # Failed-approach ingest is a first-class path.
            failed = await api.ingest(
                segments=[REPO],
                bodies=["direct docker builds hit the rate limit"],
                trust="failed-approach",
            )
            assert failed.status_code == 200

            # MUT-01b: invalidation ends belief validity without deleting
            # history, and replays idempotently.
            invalidated = await api.post(
                "/v1/invalidate",
                {
                    "fact_ids": [fact_ids[1]],
                    "reason": "superseded by the argo migration",
                },
                idempotency_key="88888888-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert invalidated.status_code == 200
            assert len(invalidated.json()["result"]["invalidated_at"]) == 27
            again = await api.post(
                "/v1/invalidate",
                {
                    "fact_ids": [fact_ids[1]],
                    "reason": "superseded by the argo migration",
                },
                idempotency_key="88888888-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert again.json()["outcome"] == "replayed"

    rows = fact_rows(config)
    # 2 initial + 1 deeper + 1 failed-approach + 2 derived (one per promotion)
    assert len(rows) == 6
    by_id = {row[0]: row for row in rows}
    assert by_id[fact_ids[1]][2] == 1  # invalidated, still present
    assert by_id[derived][1] == "validated"
    assert by_id[fact_ids[0]][1] == "candidate"  # MUT-03: source unchanged


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("grant_operations", "body_override", "status", "code"),
    [
        pytest.param(
            ["promote"],
            {},
            403,
            "authorisation_denied",
            id="mut-06-no-retrieve",
        ),
        pytest.param(
            ["retrieve"],
            {},
            403,
            "authorisation_denied",
            id="mut-07-no-promote",
        ),
        pytest.param(
            ["retrieve", "promote"],
            {"target_scope": {"realm": REALM, "segments": [REPO, BRANCH]}},
            400,
            "invalid_request",
            id="mut-08-descendant",
        ),
        pytest.param(
            ["retrieve", "promote"],
            {
                "target_scope": {
                    "realm": REALM,
                    "segments": [{"kind": "repository", "identifier": "other"}],
                }
            },
            400,
            "invalid_request",
            id="mut-08-sibling",
        ),
        pytest.param(
            ["retrieve", "promote"],
            {"target_scope": {"realm": "elsewhere", "segments": []}},
            400,
            "invalid_request",
            id="mut-08-cross-realm",
        ),
        pytest.param(
            ["retrieve", "promote"],
            {"target_classification": "restricted"},
            403,
            "authorisation_denied",
            id="mut-09-raise-outside-write",
        ),
        pytest.param(
            ["retrieve", "promote"],
            {"target_classification": "public"},
            400,
            "invalid_request",
            id="mut-10-lowering",
        ),
    ],
)
async def test_promotion_denials_map_to_their_fixed_statuses(
    tmp_path: Path,
    grant_operations: list[str],
    body_override: dict[str, object],
    status: int,
    code: str,
) -> None:
    """MUT-06 through MUT-10 at the wire, each from a fresh catalogue: the
    outsider principal gets exactly the named grant, and the denial maps
    through the I-73 table."""
    config = make_config(tmp_path)
    worker, outsider = seed(config)
    add_grant(
        config,
        UUID("77777777-7777-4777-8777-777777777777"),
        principal_id=OUTSIDER_ID,
        segments=[REPO],
        operations=grant_operations,
        write_classifications=["internal"],
    )
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            ingested = await api.ingest(segments=[REPO], bodies=["a fact"])
            fact_id = ingested.json()["result"]["fact_ids"][0]
            body: dict[str, object] = {
                "fact_ids": [fact_id],
                "evidence": EXTERNAL_EVIDENCE,
                "reason": "attempted promotion",
                **body_override,
            }
            response = await api.post("/v1/promote", body, token=outsider)

    assert response.status_code == status
    assert response.json()["failure"]["code"] == code


@pytest.mark.anyio
async def test_promotion_naming_unknown_evidence_is_denied_coarsely(
    tmp_path: Path,
) -> None:
    """MUT-05 at the wire: an evidence identity that does not exist answers
    the same coarse denial as one outside the actor's authority — the
    slice 3 disclosure rule I-67 carries forward."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            ingested = await api.ingest(segments=[REPO], bodies=["a fact"])
            response = await api.post(
                "/v1/promote",
                {
                    "fact_ids": [ingested.json()["result"]["fact_ids"][0]],
                    "evidence": {"evidence_id": "aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
                    "reason": "no such evidence",
                },
            )

    assert response.status_code == 403
    assert response.json()["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
async def test_realm_root_writes_need_an_explicit_root_grant(
    tmp_path: Path,
) -> None:
    """SCOPE-06: the repository-segment grant does not cover the realm
    root; an explicit empty-segments grant does."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            denied = await api.ingest(segments=[], bodies=["a realm-root fact"])
            assert denied.status_code == 403

            add_grant(
                config,
                UUID("99999999-9999-4999-8999-999999999999"),
                segments=[],
                operations=["ingest"],
            )
            allowed = await api.ingest(segments=[], bodies=["a realm-root fact"])
            assert allowed.status_code == 200


@pytest.mark.anyio
async def test_scope_grammar_and_depth_bounds_hold_at_the_wire(
    tmp_path: Path,
) -> None:
    """SCOPE-07 and SCOPE-08: malformed segment text is refused before the
    application, sixteen segments are accepted, seventeen are refused."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    add_grant(
        config,
        UUID("99999999-9999-4999-8999-999999999999"),
        segments=[REPO],
        operations=["ingest"],
    )
    application = build_application(config)

    def deep_segments(depth: int) -> list[dict[str, str]]:
        return [REPO] + [
            {"kind": "path", "identifier": f"level-{index}"}
            for index in range(depth - 1)
        ]

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            malformed = await api.ingest(
                segments=[{"kind": "Repo!", "identifier": "x"}],
                bodies=["a fact"],
            )
            assert malformed.status_code == 400
            assert malformed.json()["failure"]["detail"] == {
                "field_path": "scope",
                "rule": "invalid_value",
            }

            sixteen = await api.ingest(
                segments=deep_segments(16), bodies=["a deep fact"]
            )
            assert sixteen.status_code == 200

            seventeen = await api.ingest(
                segments=deep_segments(17), bodies=["a deeper fact"]
            )
            assert seventeen.status_code == 400
            assert seventeen.json()["failure"]["detail"]["field_path"] == "scope"


@pytest.mark.anyio
async def test_trust_and_classification_vocabularies_hold_at_the_wire(
    tmp_path: Path,
) -> None:
    """TRUST-04 (missing classification) and TRUST-05 (custom
    classification), named for the conformance mapping."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            missing = await api.post(
                "/v1/ingest",
                {
                    "scope": {"realm": REALM, "segments": [REPO]},
                    "source_type": "agent-claim",
                    "facts": [{"body": "a fact"}],
                },
            )
            custom = await api.ingest(
                segments=[REPO], bodies=["a fact"], classification="magenta"
            )

    assert missing.status_code == 400
    assert missing.json()["failure"]["detail"] == {
        "field_path": "classification",
        "rule": "missing_field",
    }
    assert custom.status_code == 400
    assert custom.json()["failure"]["detail"] == {
        "field_path": "classification",
        "rule": "invalid_value",
    }


@pytest.mark.anyio
async def test_a_rejected_batch_leaves_no_partial_custody(tmp_path: Path) -> None:
    """Batch atomicity at the wire: a secret in the second fact refuses the
    whole batch — no assertion, fact, evidence or outbox row survives."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            response = await api.ingest(
                segments=[REPO],
                bodies=["a clean fact", f"password={AWS_EXAMPLE_KEY}"],
            )

    assert response.status_code == 400
    assert response.json()["failure"]["code"] == "secret_rejected"
    with (
        closing(sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        for table in ("assertions", "facts", "evidence_records", "evidence_outbox"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    catalogue_bytes = (config.paths.data / CATALOGUE_FILENAME).read_bytes()
    assert AWS_EXAMPLE_KEY.encode() not in catalogue_bytes


@pytest.mark.anyio
async def test_a_malformed_payload_digest_names_the_field(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            response = await Api(client, worker).post(
                "/v1/promote",
                {
                    "fact_ids": [],
                    "evidence": {
                        "external_uri": "https://ci.example/run/42",
                        "payload_digest": "AB" * 32,
                    },
                    "reason": "r",
                },
            )

    assert response.status_code == 400
    assert response.json()["failure"]["detail"] == {
        "field_path": "evidence.payload_digest",
        "rule": "invalid_value",
    }


@pytest.mark.anyio
async def test_a_mixed_evidence_shape_names_the_field_without_class_names(
    tmp_path: Path,
) -> None:
    """The union filter: a body carrying fields of both evidence forms is
    refused with a field path free of Pydantic member-class names."""
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, worker)
            response = await api.post(
                "/v1/promote",
                {
                    "fact_ids": [],
                    "evidence": {
                        "evidence_id": "e",
                        "external_uri": "u",
                    },
                    "reason": "r",
                },
            )

    assert response.status_code == 400
    detail = response.json()["failure"]["detail"]
    assert detail["field_path"] == "evidence.external_uri"
    assert "EvidenceIdBody" not in json.dumps(response.json())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "field_path"),
    [
        pytest.param(
            {"fact_ids": ["NOT-A-UUID"], "reason": "r"},
            "fact_ids[0]",
            id="invalidate-fact-id",
        ),
        pytest.param(
            {"fact_ids": [], "reason": "r", "superseded_by": "3FA85F64"},
            "superseded_by",
            id="superseded-by",
        ),
        pytest.param(
            {
                "fact_ids": ["3FA85F64-5717-4562-B3FC-2C963F66AFA6"],
                "reason": "r",
            },
            "fact_ids[0]",
            id="uppercase-uuid",
        ),
    ],
)
async def test_invalidate_identity_refusals_name_the_field(
    tmp_path: Path,
    body: dict[str, object],
    field_path: str,
) -> None:
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            response = await Api(client, worker).post("/v1/invalidate", body)

    assert response.status_code == 400
    assert response.json()["failure"]["detail"] == {
        "field_path": field_path,
        "rule": "invalid_value",
    }


@pytest.mark.anyio
async def test_a_non_version_4_correlation_header_cannot_suppress_a_denial(
    tmp_path: Path,
) -> None:
    """The audit-suppression half of the Task 10 correctness finding,
    end-to-end through the composed stack.

    The middleware adopted any canonical UUID while ``AuditDraft``
    requires version 4, so a caller sending a UUIDv1 in
    ``X-Correlation-ID`` got a 500 out of the draft construction — and on
    the unauthenticated path the denial event was never appended, so one
    header erased the audit record of a failed authentication. Both paths
    are pinned: a fresh identifier is substituted, and the instance chain
    is written either way.
    """
    config = make_config(tmp_path)
    worker, _ = seed(config)
    application = build_application(config)
    submitted = str(uuid1())
    body = json.dumps(
        {
            "scope": {"realm": REALM, "segments": [REPO]},
            "classification": "internal",
            "source_type": "agent-claim",
            "facts": [{"body": "a fact"}],
        }
    )

    def headers(token: str, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": key,
            "X-Correlation-ID": submitted,
        }

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            denied = await client.post(
                "/v1/ingest",
                content=body,
                headers=headers(
                    "not-a-real-token", "3fa85f64-5717-4562-b3fc-2c963f66afa6"
                ),
            )
            accepted = await client.post(
                "/v1/ingest",
                content=body,
                headers=headers(worker, "3fa85f64-5717-4562-b3fc-2c963f66afb7"),
            )

    assert denied.status_code == 401
    assert denied.json()["failure"]["code"] == "authentication_failed"
    assert accepted.status_code == 200
    for response in (denied, accepted):
        adopted = UUID(response.headers["X-Correlation-ID"])
        assert adopted.version == 4
        assert str(adopted) != submitted
    # The denial is durable, which is exactly what the defect destroyed.
    with (
        closing(sqlite3.connect(config.paths.data / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        denials = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE chain_kind = 'instance'"
        ).fetchone()[0]
    assert denials == 1
