"""Slice 9 Task 3: resumable application of a migration plan over ``/v1``.

Synthetic fixtures only.  Bodies stay deliberately conspicuous so a failure
cannot be mistaken for an assertion about any legacy record.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import (
    ASGITransport,
    AsyncClient,
    ConnectError,
    MockTransport,
    Request,
    Response,
)

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn_migrate.__main__ import _CHECKOUT_ROOT, _refuse, main
from cairn_migrate.apply import ApplyError, apply_plan, read_plan
from cairn_migrate.mapping import (
    MIGRATION_NAMESPACE,
    OPERATIONS_FILENAME,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    RECONCILIATIONS_FILENAME,
    REJECTIONS_FILENAME,
    canonical_json,
    idempotency_key,
)

INSTANCE_ID_TEXT = "11111111-1111-4111-8111-111111111111"


def _operation(legacy_id: str, body: str) -> dict[str, object]:
    return {
        "operation": "ingest",
        "store": "graph-episode",
        "legacy_id": legacy_id,
        "idempotency_key": idempotency_key("graph-episode", legacy_id),
        "request": {
            "scope": {"realm": "cairn", "segments": []},
            "classification": "internal",
            "source_type": "agent-claim",
            "facts": [{"body": body}],
            "requested_trust": "candidate",
        },
    }


def _write_plan(
    path: Path,
    operations: list[dict[str, object]],
    *,
    rejections: list[dict[str, object]] | None = None,
) -> Path:
    path.mkdir()
    files: list[dict[str, object]] = []
    for filename, records in (
        (OPERATIONS_FILENAME, operations),
        (REJECTIONS_FILENAME, rejections if rejections is not None else []),
        (RECONCILIATIONS_FILENAME, []),
    ):
        raw = b"".join(
            (canonical_json(record) + "\n").encode("utf-8") for record in records
        )
        (path / filename).write_bytes(raw)
        files.append(
            {
                "filename": filename,
                "record_count": len(records),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    manifest = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_bundle": {"label": "synthetic", "manifest_sha256": "0" * 64},
        "target": {
            "realm": "cairn",
            "segments": [],
            "classification": "internal",
        },
        "idempotency_namespace": str(MIGRATION_NAMESPACE),
        "enumerations": {
            "filename": "enumerations.json",
            "bytes": 3,
            "sha256": hashlib.sha256(b"{}\n").hexdigest(),
        },
        "files": files,
        "counts": {},
    }
    (path / "enumerations.json").write_bytes(b"{}\n")
    (path / PLAN_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _in_process_instance(
    tmp_path: Path, *, operations: list[str]
) -> tuple[FastAPI, str]:
    """Build the real Task 3 transport seam with one root-granted workload."""
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    data = tmp_path / "data"
    credentials = tmp_path / "credentials"
    data.mkdir(parents=True)
    credentials.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
    )
    migrate_catalogue(config, lambda: now)
    principal_id = "22222222-2222-4222-8222-222222222222"
    credential_id = UUID("33333333-3333-4333-8333-333333333333")
    token = mint_token(credential_id, lambda count: bytes(range(count)))
    timestamp = canonical_timestamp(now)
    expires = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
    with _open_write_connection(data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES ('cairn', ?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', 'cairn', 0, ?)",
            (bytes(32),),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'workload', 'migration-test', ?)",
            (principal_id, timestamp),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(credential_id), principal_id, token.verifier, timestamp, expires),
        )
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES ('55555555-5555-4555-8555-555555555555', ?, 'cairn', "
            "'[]', ?, 'internal', '[\"internal\"]', NULL, NULL, ?, ?)",
            (principal_id, canonical_json(sorted(operations)), expires, timestamp),
        )
        connection.commit()
    return build_application(config), token.text


@pytest.mark.anyio
async def test_apply_posts_canonical_plan_lines_and_writes_identity_receipts(
    tmp_path: Path,
) -> None:
    """Removing the POST or receipt append must lose either the migration or
    its restart boundary; both are required observable effects."""
    operations = [
        _operation("legacy-one", "SYNTHETIC-APPLY-BODY-ONE"),
        _operation("legacy-two", "SYNTHETIC-APPLY-BODY-TWO"),
    ]
    plan = _write_plan(tmp_path / "plan", operations)
    requests: list[Request] = []

    def respond(request: Request) -> Response:
        requests.append(request)
        ordinal = len(requests)
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": f"{ordinal:08d}-1111-4111-8111-111111111111",
                    "fact_ids": [f"{ordinal:08d}-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
                "mutation_receipt": {
                    "mutation_id": f"{ordinal:08d}-3333-4333-8333-333333333333",
                    "command_digest": "a" * 64,
                },
                "audit_receipt": {
                    "event_id": f"{ordinal:08d}-4444-4444-8444-444444444444",
                    "chain_kind": "realm",
                    "chain_identity": "cairn",
                    "sequence": ordinal,
                    "recorded_at": "2026-08-21T12:00:00.000000Z",
                    "event_hash": "b" * 64,
                },
            },
        )

    receipts = tmp_path / "receipts.jsonl"
    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=receipts,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert summary.planned == 2
    assert summary.applied == 2
    assert summary.resumed == 0
    assert [request.url.path for request in requests] == ["/v1/ingest"] * 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [
        str(operation["idempotency_key"]) for operation in operations
    ]
    assert all(
        request.headers["Authorization"] == "Bearer synthetic-token"
        for request in requests
    )
    assert [request.content for request in requests] == [
        canonical_json(operation["request"]).encode("utf-8") for operation in operations
    ]
    written = [
        json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()
    ]
    assert written == [
        {
            "instance_id": INSTANCE_ID_TEXT,
            "operation": "ingest",
            "assertion_id": "00000001-1111-4111-8111-111111111111",
            "fact_ids": ["00000001-2222-4222-8222-222222222222"],
            "idempotency_key": operations[0]["idempotency_key"],
            "legacy_id": "legacy-one",
            "store": "graph-episode",
        },
        {
            "instance_id": INSTANCE_ID_TEXT,
            "operation": "ingest",
            "assertion_id": "00000002-1111-4111-8111-111111111111",
            "fact_ids": ["00000002-2222-4222-8222-222222222222"],
            "idempotency_key": operations[1]["idempotency_key"],
            "legacy_id": "legacy-two",
            "store": "graph-episode",
        },
    ]


@pytest.mark.anyio
async def test_apply_resumes_after_the_last_durable_receipt(tmp_path: Path) -> None:
    """A mid-run restart must not send an already-receipted operation again."""
    operations = [
        _operation("legacy-one", "SYNTHETIC-RESUME-BODY-ONE"),
        _operation("legacy-two", "SYNTHETIC-RESUME-BODY-TWO"),
    ]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        canonical_json(
            {
                "instance_id": INSTANCE_ID_TEXT,
                "operation": "ingest",
                "assertion_id": "00000001-1111-4111-8111-111111111111",
                "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                "idempotency_key": operations[0]["idempotency_key"],
                "legacy_id": "legacy-one",
                "store": "graph-episode",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    requested_keys: list[str] = []

    def respond(request: Request) -> Response:
        requested_keys.append(request.headers["Idempotency-Key"])
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000002-1111-4111-8111-111111111111",
                    "fact_ids": ["00000002-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=receipts,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert summary.applied == 1
    assert summary.resumed == 1
    assert requested_keys == [operations[1]["idempotency_key"]]
    assert len(receipts.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.anyio
async def test_apply_rejects_a_noncanonical_plan_idempotency_uuid(
    tmp_path: Path,
) -> None:
    operation = _operation("legacy-one", "SYNTHETIC-NONCANONICAL-KEY")
    operation["idempotency_key"] = str(operation["idempotency_key"]).upper()
    plan = _write_plan(tmp_path / "plan", [operation])

    async with AsyncClient(base_url="https://cairn.invalid") as client:
        with pytest.raises(ApplyError) as error:
            await apply_plan(
                plan_path=plan,
                receipts_path=tmp_path / "receipts.jsonl",
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert error.value.code == "plan_value_invalid"


def test_plan_reader_refuses_duplicate_identity_triples(tmp_path: Path) -> None:
    operation = _operation("legacy-one", "SYNTHETIC-DUPLICATE-PLAN")
    plan = _write_plan(tmp_path / "plan", [operation, operation])

    with pytest.raises(ApplyError) as error:
        read_plan(plan)

    assert error.value.code == "plan_value_invalid"


@pytest.mark.anyio
async def test_apply_refuses_receipts_bound_to_another_instance(
    tmp_path: Path,
) -> None:
    operation = _operation("legacy-one", "SYNTHETIC-INSTANCE-BOUND-RECEIPT")
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        canonical_json(
            {
                "instance_id": INSTANCE_ID_TEXT,
                "operation": "ingest",
                "assertion_id": "00000001-1111-4111-8111-111111111111",
                "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                "idempotency_key": operation["idempotency_key"],
                "legacy_id": "legacy-one",
                "store": "graph-episode",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    requests: list[Request] = []

    def unexpected_request(request: Request) -> Response:
        requests.append(request)
        return Response(500)

    async with AsyncClient(
        transport=MockTransport(unexpected_request),
        base_url="https://cairn.invalid",
    ) as client:
        with pytest.raises(ApplyError) as error:
            await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance="99999999-9999-4999-8999-999999999999",
                client=client,
                credential="synthetic-token",
            )

    assert error.value.code == "receipt_instance_mismatch"
    assert requests == []


@pytest.mark.anyio
async def test_apply_rejects_non_v4_cairn_identities_in_a_receipt(
    tmp_path: Path,
) -> None:
    operation = _operation("legacy-one", "SYNTHETIC-NON-V4-RECEIPT")
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        canonical_json(
            {
                "instance_id": INSTANCE_ID_TEXT,
                "operation": "ingest",
                "assertion_id": "00000001-1111-5111-8111-111111111111",
                "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                "idempotency_key": operation["idempotency_key"],
                "legacy_id": "legacy-one",
                "store": "graph-episode",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async with AsyncClient(base_url="https://cairn.invalid") as client:
        with pytest.raises(ApplyError) as error:
            await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert error.value.code == "receipt_value_invalid"


@pytest.mark.anyio
async def test_apply_retries_only_a_typed_after_delay_failure(tmp_path: Path) -> None:
    """Removing the typed-retry branch must turn a recoverable server reply
    into an incomplete migration; retrying more than the bound is equally a
    bug."""
    operation = _operation("retry-me", "SYNTHETIC-RETRY-BODY")
    plan = _write_plan(tmp_path / "plan", [operation])
    attempts = 0
    delays: list[float] = []

    def respond(request: Request) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return Response(
                503,
                headers={"Retry-After": "2"},
                json={
                    "failure": {
                        "code": "dependency_unavailable",
                        "message": "temporarily unavailable",
                        "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "retry": "after-delay",
                    }
                },
            )
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000001-1111-4111-8111-111111111111",
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=tmp_path / "receipts.jsonl",
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
            sleep=sleep,
            max_attempts=3,
        )

    assert summary.applied == 1
    assert attempts == 3
    assert delays == [2.0, 2.0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "failure", "headers", "outer_extra"),
    [
        (
            503,
            {
                "code": "internal_error",
                "message": "wrong retry class",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "2"},
            {},
        ),
        (
            503,
            {
                "code": "dependency_unavailable",
                "message": "fractional retry delay",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "2.5"},
            {},
        ),
        (
            503,
            {
                "code": "dependency_unavailable",
                "message": "scientific retry delay",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "1e3"},
            {},
        ),
        (
            500,
            {
                "code": "dependency_unavailable",
                "message": "wrong status",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "2"},
            {},
        ),
        (
            503,
            {
                "code": "dependency_unavailable",
                "message": "missing retry header",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {},
            {},
        ),
        (
            503,
            {
                "code": "dependency_unavailable",
                "message": "extra outer field",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "2"},
            {"result": {}},
        ),
        (
            503,
            {
                "code": [],
                "message": "non-string code",
                "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "retry": "after-delay",
            },
            {"Retry-After": "2"},
            {},
        ),
    ],
)
async def test_apply_does_not_retry_a_mismatched_after_delay_envelope(
    tmp_path: Path,
    status: int,
    failure: dict[str, object],
    headers: dict[str, str],
    outer_extra: dict[str, object],
) -> None:
    plan = _write_plan(
        tmp_path / "plan", [_operation("retry-mismatch", "SYNTHETIC-RETRY")]
    )
    attempts = 0

    def respond(request: Request) -> Response:
        nonlocal attempts
        attempts += 1
        return Response(
            status,
            headers=headers,
            json={"failure": failure, **outer_extra},
        )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(ApplyError):
            await apply_plan(
                plan_path=plan,
                receipts_path=tmp_path / "receipts.jsonl",
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert attempts == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation_name", "assertion_id"),
    [
        ("ingest", None),
        ("invalidate", "00000001-1111-4111-8111-111111111111"),
    ],
)
async def test_apply_rejects_an_impossible_operation_receipt_pairing(
    tmp_path: Path,
    operation_name: str,
    assertion_id: str | None,
) -> None:
    operation = _operation("legacy-one", "SYNTHETIC-IMPOSSIBLE-RECEIPT")
    operation["operation"] = operation_name
    if operation_name == "invalidate":
        operation["request"] = {
            "fact_ids": ["00000001-2222-4222-8222-222222222222"],
            "reason": "legacy contradiction ruled for carry-over",
        }
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        canonical_json(
            {
                "instance_id": INSTANCE_ID_TEXT,
                "operation": operation_name,
                "assertion_id": assertion_id,
                "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                "idempotency_key": operation["idempotency_key"],
                "legacy_id": "legacy-one",
                "store": "graph-episode",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async with AsyncClient(base_url="https://cairn.invalid") as client:
        with pytest.raises(ApplyError) as error:
            await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert error.value.code == "receipt_value_invalid"


@pytest.mark.anyio
async def test_apply_surfaces_the_servers_stable_refusal_code(tmp_path: Path) -> None:
    """Replacing the server's stable code with status text would hide the
    missing-promote-grant failure Task 3 is required to expose."""
    plan = _write_plan(
        tmp_path / "plan", [_operation("denied", "SYNTHETIC-DENIED-BODY")]
    )
    attempts = 0

    def respond(request: Request) -> Response:
        nonlocal attempts
        attempts += 1
        return Response(
            403,
            json={
                "failure": {
                    "code": "authorisation_denied",
                    "message": "request denied",
                    "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "retry": "never",
                }
            },
        )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(ApplyError) as error:
            await apply_plan(
                plan_path=plan,
                receipts_path=tmp_path / "receipts.jsonl",
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert error.value.code == "request_failed"
    assert error.value.detail == "authorisation_denied"
    assert attempts == 1
    assert not (tmp_path / "receipts.jsonl").exists()


@pytest.mark.anyio
async def test_validated_ingest_without_promote_grant_surfaces_real_refusal(
    tmp_path: Path,
) -> None:
    """The real authority boundary, not a mock: validated-at-ingest must not
    be weakened merely because the caller is the migration workload."""
    operation = _operation("validated", "SYNTHETIC-VALIDATED-BODY")
    request = operation["request"]
    assert isinstance(request, dict)
    request["source_type"] = "human"
    request["requested_trust"] = "validated"
    request["evidence_payload"] = '{"synthetic":true}'
    plan = _write_plan(tmp_path / "plan", [operation])
    application, token = _in_process_instance(
        tmp_path / "instance", operations=["ingest"]
    )

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://cairn"
        ) as client:
            with pytest.raises(ApplyError) as error:
                await apply_plan(
                    plan_path=plan,
                    receipts_path=tmp_path / "receipts.jsonl",
                    expected_instance=INSTANCE_ID_TEXT,
                    client=client,
                    credential=token,
                )

    assert error.value.code == "request_failed"
    assert error.value.detail == "authorisation_denied"
    assert not (tmp_path / "receipts.jsonl").exists()


@pytest.mark.anyio
async def test_mid_run_abort_keeps_a_resume_boundary_without_duplicates(
    tmp_path: Path,
) -> None:
    """Deleting the first durable receipt would resend the committed call;
    writing the second one before success would silently skip work."""
    operations = [
        _operation("legacy-one", "SYNTHETIC-ABORT-BODY-ONE"),
        _operation("legacy-two", "SYNTHETIC-ABORT-BODY-TWO"),
    ]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts = tmp_path / "receipts.jsonl"
    first_run = 0

    def abort_after_one(request: Request) -> Response:
        nonlocal first_run
        first_run += 1
        if first_run == 2:
            return Response(
                500,
                json={
                    "failure": {
                        "code": "internal_error",
                        "message": "request failed",
                        "correlation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "retry": "never",
                    }
                },
            )
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000001-1111-4111-8111-111111111111",
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    async with AsyncClient(
        transport=MockTransport(abort_after_one), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(ApplyError):
            await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
            )

    assert len(receipts.read_text(encoding="utf-8").splitlines()) == 1
    resumed_keys: list[str] = []

    def finish(request: Request) -> Response:
        resumed_keys.append(request.headers["Idempotency-Key"])
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000002-1111-4111-8111-111111111111",
                    "fact_ids": ["00000002-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    async with AsyncClient(
        transport=MockTransport(finish), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=receipts,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert summary.resumed == 1
    assert summary.applied == 1
    assert resumed_keys == [operations[1]["idempotency_key"]]
    assert len(receipts.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.anyio
async def test_apply_never_sends_a_rejection_record(tmp_path: Path) -> None:
    """Reading any file except ``operations.jsonl`` as calls would turn a
    counted rejection into a forbidden migration write."""
    operation = _operation("admitted", "SYNTHETIC-ADMITTED-BODY")
    plan = _write_plan(
        tmp_path / "plan",
        [operation],
        rejections=[
            {
                "store": "graph-episode",
                "legacy_id": "rejected",
                "rule": "secret_screen",
                "detail": "body",
            }
        ],
    )
    calls = 0

    def respond(request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000001-1111-4111-8111-111111111111",
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        await apply_plan(
            plan_path=plan,
            receipts_path=tmp_path / "receipts.jsonl",
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert calls == 1


@pytest.mark.anyio
async def test_apply_accepts_a_ruled_invalidation_plan_line(tmp_path: Path) -> None:
    operation = _operation("invalidate-one", "unused")
    operation["operation"] = "invalidate"
    operation["request"] = {
        "fact_ids": ["00000001-2222-4222-8222-222222222222"],
        "reason": "legacy contradiction ruled for carry-over",
    }
    plan = _write_plan(tmp_path / "plan", [operation])
    requests: list[Request] = []

    def respond(request: Request) -> Response:
        requests.append(request)
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "invalidated_at": "2026-08-21T12:00:00.000000Z",
                },
            },
        )

    receipts = tmp_path / "receipts.jsonl"
    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=receipts,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert summary.applied == 1
    assert requests[0].url.path == "/v1/invalidate"
    assert json.loads(receipts.read_text(encoding="utf-8")) == {
        "instance_id": INSTANCE_ID_TEXT,
        "operation": "invalidate",
        "assertion_id": None,
        "fact_ids": ["00000001-2222-4222-8222-222222222222"],
        "idempotency_key": operation["idempotency_key"],
        "legacy_id": "invalidate-one",
        "store": "graph-episode",
    }


@pytest.mark.anyio
async def test_apply_checkpoints_ingest_and_invalidate_for_one_legacy_record(
    tmp_path: Path,
) -> None:
    ingest = _operation("same-legacy", "SYNTHETIC-MIXED-BODY")
    invalidate = dict(ingest)
    invalidate["operation"] = "invalidate"
    invalidate["request"] = {
        "fact_ids": ["00000001-2222-4222-8222-222222222222"],
        "reason": "legacy contradiction ruled for carry-over",
    }
    plan = _write_plan(tmp_path / "plan", [ingest, invalidate])
    paths: list[str] = []

    def respond(request: Request) -> Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/ingest":
            return Response(
                200,
                json={
                    "outcome": "committed",
                    "result": {
                        "assertion_id": "00000001-1111-4111-8111-111111111111",
                        "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                        "evidence_id": None,
                    },
                },
            )
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "invalidated_at": "2026-08-21T12:00:00.000000Z",
                },
            },
        )

    receipts = tmp_path / "receipts.jsonl"
    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await apply_plan(
            plan_path=plan,
            receipts_path=receipts,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
        )

    assert summary.applied == 2
    assert summary.resumed == 0
    assert paths == ["/v1/ingest", "/v1/invalidate"]
    assert [
        json.loads(line)["operation"]
        for line in receipts.read_text(encoding="utf-8").splitlines()
    ] == ["ingest", "invalidate"]


def test_apply_cli_requires_an_explicit_instance_and_credential_file(
    tmp_path: Path,
) -> None:
    plan = _write_plan(
        tmp_path / "plan",
        [_operation("legacy-one", "SYNTHETIC-CLI-BODY")],
    )

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "apply",
                "--plan",
                str(plan),
                "--receipts",
                str(tmp_path / "receipts.jsonl"),
            ]
        )

    assert caught.value.code == 2


@pytest.mark.parametrize(
    "instance", ["https://cairn.invalid", "http://127.0.0.1", "http://[::1]"]
)
def test_apply_cli_reads_the_credential_file_without_disclosing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    instance: str,
) -> None:
    plan = _write_plan(
        tmp_path / "plan",
        [_operation("legacy-one", "SYNTHETIC-CLI-BODY")],
    )
    receipts = tmp_path / "receipts.jsonl"
    credential = tmp_path / "credential"
    credential.write_text("synthetic-secret-token\n", encoding="utf-8")
    calls: list[Request] = []

    def respond(request: Request) -> Response:
        calls.append(request)
        if request.url.path == "/v1/instance":
            return Response(200, json={"instance_id": INSTANCE_ID_TEXT})
        return Response(
            200,
            json={
                "outcome": "committed",
                "result": {
                    "assertion_id": "00000001-1111-4111-8111-111111111111",
                    "fact_ids": ["00000001-2222-4222-8222-222222222222"],
                    "evidence_id": None,
                },
            },
        )

    exit_code = main(
        [
            "apply",
            "--instance",
            instance,
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(credential),
            "--plan",
            str(plan),
            "--receipts",
            str(receipts),
        ],
        client_factory=lambda instance: AsyncClient(
            transport=MockTransport(respond), base_url=instance
        ),
    )

    assert exit_code == 0
    assert [call.url.path for call in calls] == ["/v1/instance", "/v1/ingest"]
    assert calls[1].url.path == "/v1/ingest"
    assert all(
        call.headers["Authorization"] == "Bearer synthetic-secret-token"
        for call in calls
    )
    output = capsys.readouterr()
    assert "synthetic-secret-token" not in output.out
    assert "synthetic-secret-token" not in output.err
    assert json.loads(output.out) == {
        "applied": 1,
        "planned": 1,
        "receipts_path": str(receipts.resolve()),
        "resumed": 0,
        "status": "ok",
    }


@pytest.mark.parametrize(
    "instance",
    [
        "cairn.invalid",
        "http://cairn.invalid",
        "http://localhost",
        "https://user:secret@cairn.invalid",
        "https://cairn.invalid/v1",
        "https://cairn.invalid?target=other",
    ],
)
def test_apply_cli_refuses_ambiguous_or_credentialled_instance_urls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    instance: str,
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("synthetic-token\n", encoding="utf-8")

    exit_code = main(
        [
            "apply",
            "--instance",
            instance,
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(credential),
            "--plan",
            str(tmp_path / "plan"),
            "--receipts",
            str(tmp_path / "receipts.jsonl"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "instance_url_invalid",
        "status": "error",
    }


def test_verify_cli_refuses_an_unreadable_credential_before_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "verify",
            "--instance",
            "https://cairn.invalid",
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(tmp_path / "absent"),
            "--plan",
            str(tmp_path / "plan"),
            "--receipts",
            str(tmp_path / "receipts.jsonl"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "credential_unreadable",
        "status": "error",
    }


def test_apply_cli_refuses_migration_paths_inside_the_checkout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("synthetic-token\n", encoding="utf-8")

    exit_code = main(
        [
            "apply",
            "--instance",
            "https://cairn.invalid",
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(credential),
            "--plan",
            str(_CHECKOUT_ROOT / "private-plan"),
            "--receipts",
            str(tmp_path / "receipts.jsonl"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "migration_path_inside_checkout",
        "status": "error",
    }


def test_apply_cli_refuses_an_unexpected_instance_before_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _write_plan(
        tmp_path / "plan",
        [_operation("legacy-one", "SYNTHETIC-CLI-BODY")],
    )
    credential = tmp_path / "credential"
    credential.write_text("synthetic-token\n", encoding="utf-8")
    calls: list[str] = []

    def respond(request: Request) -> Response:
        calls.append(request.url.path)
        return Response(
            200,
            json={"instance_id": "99999999-9999-4999-8999-999999999999"},
        )

    exit_code = main(
        [
            "apply",
            "--instance",
            "https://cairn.invalid",
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(credential),
            "--plan",
            str(plan),
            "--receipts",
            str(tmp_path / "receipts.jsonl"),
        ],
        client_factory=lambda instance: AsyncClient(
            transport=MockTransport(respond), base_url=instance
        ),
    )

    assert exit_code == 2
    assert calls == ["/v1/instance"]
    assert json.loads(capsys.readouterr().err) == {
        "code": "instance_mismatch",
        "status": "error",
    }


def test_cli_missing_arguments_use_the_json_refusal_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "apply",
                "--plan",
                str(tmp_path / "plan"),
                "--receipts",
                str(tmp_path / "receipts.jsonl"),
            ]
        )

    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "arguments_invalid",
        "status": "error",
    }


def test_cli_refusal_codes_are_closed() -> None:
    with pytest.raises(ValueError, match="unknown CLI error code"):
        _refuse("synthetic_unregistered_code")


def test_apply_cli_distinguishes_transport_failure_from_server_refusal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("synthetic-token\n", encoding="utf-8")

    def fail_transport(request: Request) -> Response:
        raise ConnectError("synthetic transport failure", request=request)

    exit_code = main(
        [
            "apply",
            "--instance",
            "https://cairn.invalid",
            "--expected-instance",
            INSTANCE_ID_TEXT,
            "--credential-file",
            str(credential),
            "--plan",
            str(tmp_path / "plan"),
            "--receipts",
            str(tmp_path / "receipts.jsonl"),
        ],
        client_factory=lambda instance: AsyncClient(
            transport=MockTransport(fail_transport), base_url=instance
        ),
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "transport_failed",
        "status": "error",
    }
