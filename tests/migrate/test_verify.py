"""Slice 9 Task 3: verification through Cairn's ordinary ``/v1`` surface."""

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.catalogue.verification import verify_catalogue
from cairn.projection.delivery import deliver_projection_outbox
from cairn.projection.memory import MemoryIndex
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, GraphitiConfig, HttpConfig, PathConfig
from cairn_migrate.__main__ import main
from cairn_migrate.apply import apply_plan
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
from cairn_migrate.verify import VerifyError, verify_plan

INSTANCE_ID_TEXT = "11111111-1111-4111-8111-111111111111"


def _operation(ordinal: int) -> dict[str, object]:
    legacy_id = f"legacy-{ordinal}"
    return {
        "operation": "ingest",
        "store": "graph-episode",
        "legacy_id": legacy_id,
        "idempotency_key": idempotency_key("graph-episode", legacy_id),
        "request": {
            "scope": {"realm": "cairn", "segments": []},
            "classification": "internal",
            "source_type": "agent-claim",
            "facts": [{"body": f"SYNTHETIC-VERIFY-BODY-{ordinal}"}],
            "requested_trust": "candidate",
        },
    }


def _write_plan(path: Path, operations: list[dict[str, object]]) -> Path:
    path.mkdir()
    files: list[dict[str, object]] = []
    for filename, records in (
        (OPERATIONS_FILENAME, operations),
        (REJECTIONS_FILENAME, []),
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
    enumerations = b"{}\n"
    (path / "enumerations.json").write_bytes(enumerations)
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
            "bytes": len(enumerations),
            "sha256": hashlib.sha256(enumerations).hexdigest(),
        },
        "files": files,
        "counts": {},
    }
    (path / PLAN_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _write_receipts(
    path: Path, operations: list[dict[str, object]]
) -> list[dict[str, object]]:
    receipts = [
        {
            "instance_id": INSTANCE_ID_TEXT,
            "operation": operation["operation"],
            "store": operation["store"],
            "legacy_id": operation["legacy_id"],
            "idempotency_key": operation["idempotency_key"],
            "assertion_id": f"{ordinal:08d}-1111-4111-8111-111111111111",
            "fact_ids": [f"{ordinal:08d}-2222-4222-8222-222222222222"],
        }
        for ordinal, operation in enumerate(operations, start=1)
    ]
    path.write_text(
        "".join(canonical_json(receipt) + "\n" for receipt in receipts),
        encoding="utf-8",
    )
    return receipts


def _single_operation_responder(
    receipt: dict[str, object],
    *,
    replay_outcome: str = "replayed",
    include_replay_audit: bool = True,
    retrieval_fact_id: str | None = None,
    retrieval_body: str = "SYNTHETIC-VERIFY-BODY-1",
    pending_retrievals: int = 0,
) -> tuple[Callable[[Request], Response], dict[str, int | bool]]:
    state: dict[str, int | bool] = {"retrieval_attempts": 0, "replayed": False}
    fact_ids = cast(list[str], receipt["fact_ids"])

    def audit_event(*, replay: bool) -> dict[str, object]:
        return {
            "action_kind": "data",
            "action_code": "ingest",
            "outcome": "allow",
            "reason_code": "idempotent_replay" if replay else "assertion_ingested",
            "idempotency_key": receipt["idempotency_key"],
            "affected_assertion_ids": [receipt["assertion_id"]],
            "affected_fact_ids": fact_ids,
            "replay_of_mutation_id": (
                "00000001-3333-4333-8333-333333333333" if replay else None
            ),
        }

    def respond(request: Request) -> Response:
        if request.url.path == "/v1/retrieve":
            state["retrieval_attempts"] = state["retrieval_attempts"] + 1
            if state["retrieval_attempts"] <= pending_retrievals:
                return Response(
                    503,
                    headers={"Retry-After": "0"},
                    json={
                        "failure": {
                            "code": "index_pending",
                            "message": "projection has not caught up",
                            "correlation_id": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                            "retry": "after-delay",
                        }
                    },
                )
            return Response(
                200,
                json={
                    "hits": [
                        {
                            "fact_id": retrieval_fact_id or fact_ids[0],
                            "assertion_id": receipt["assertion_id"],
                            "body": retrieval_body,
                        }
                    ]
                },
            )
        if request.url.path == "/v1/ingest":
            state["replayed"] = True
            return Response(
                200,
                json={
                    "outcome": replay_outcome,
                    "result": {
                        "assertion_id": receipt["assertion_id"],
                        "fact_ids": fact_ids,
                        "evidence_id": None,
                    },
                },
            )
        assert request.url.path == "/v1/read-audit-events"
        events = [audit_event(replay=False)]
        if state["replayed"] and include_replay_audit:
            events.append(audit_event(replay=True))
        return Response(200, json={"events": events, "next_after_sequence": None})

    return respond, state


def _in_process_instance(
    tmp_path: Path,
    *,
    projection_enabled: bool = True,
) -> tuple[CairnConfig, MemoryIndex, FastAPI, str]:
    """Build the real P-76 seam with separate data and audit root grants."""
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
        graphiti=GraphitiConfig(enabled=projection_enabled),
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
        for grant_id, operations, writes in (
            (
                "55555555-5555-4555-8555-555555555555",
                ["ingest", "invalidate", "promote", "retrieve"],
                ["internal"],
            ),
            (
                "66666666-6666-4666-8666-666666666666",
                ["audit-read"],
                [],
            ),
        ):
            connection.execute(
                "INSERT INTO grants (grant_id, principal_id, realm_id, "
                "scope_segments, operations, read_clearance, "
                "write_classifications, delegable_operations, issued_by, "
                "expires_at, created_at) "
                "VALUES (?, ?, 'cairn', '[]', ?, 'internal', ?, NULL, NULL, ?, ?)",
                (
                    grant_id,
                    principal_id,
                    canonical_json(sorted(operations)),
                    canonical_json(sorted(writes)),
                    expires,
                    timestamp,
                ),
            )
        connection.commit()
    index = MemoryIndex()
    return (
        config,
        index,
        build_application(config, index_adapter=index if projection_enabled else None),
        token.text,
    )


def _drain(config: CairnConfig, index: MemoryIndex) -> None:
    deliver_projection_outbox(
        CatalogueTransactions(
            config.paths.data,
            writer_gate=threading.Lock(),
            clock=lambda: datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC),
            uuid_factory=uuid4,
        ),
        index,
        clock=lambda: datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC),
    )


@pytest.mark.anyio
async def test_verify_compares_retrieval_replays_and_audit_identities(
    tmp_path: Path,
) -> None:
    """Removing any one of the three checks must make the summary incomplete:
    a receipt alone does not prove retrievability, replay, or durable audit."""
    operations = [_operation(1), _operation(2)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts_path = tmp_path / "receipts.jsonl"
    receipts = _write_receipts(receipts_path, operations)
    calls: list[Request] = []
    replayed_keys: set[str] = set()

    def respond(request: Request) -> Response:
        calls.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/retrieve":
            ordinal = 1 if body["query"].endswith("-1") else 2
            receipt = receipts[ordinal - 1]
            fact_ids = cast(list[str], receipt["fact_ids"])
            return Response(
                200,
                json={
                    "hits": [
                        {
                            "fact_id": fact_ids[0],
                            "assertion_id": receipt["assertion_id"],
                            "body": f"SYNTHETIC-VERIFY-BODY-{ordinal}",
                        }
                    ],
                    "budget_consumed": 23,
                    "budget_exhausted": False,
                },
            )
        if request.url.path == "/v1/ingest":
            key = request.headers["Idempotency-Key"]
            replayed_keys.add(key)
            receipt = next(
                value for value in receipts if value["idempotency_key"] == key
            )
            return Response(
                200,
                json={
                    "outcome": "replayed",
                    "result": {
                        "assertion_id": receipt["assertion_id"],
                        "fact_ids": receipt["fact_ids"],
                        "evidence_id": None,
                    },
                },
            )
        assert request.url.path == "/v1/read-audit-events"
        events = []
        for ordinal, receipt in enumerate(receipts, start=1):
            replay_states = [False]
            if receipt["idempotency_key"] in replayed_keys:
                replay_states.append(True)
            for replayed in replay_states:
                events.append(
                    {
                        "action_kind": "data",
                        "action_code": "ingest",
                        "outcome": "allow",
                        "reason_code": (
                            "idempotent_replay" if replayed else "assertion_ingested"
                        ),
                        "idempotency_key": receipt["idempotency_key"],
                        "affected_assertion_ids": [receipt["assertion_id"]],
                        "affected_fact_ids": receipt["fact_ids"],
                        "replay_of_mutation_id": (
                            f"{ordinal:08d}-3333-4333-8333-333333333333"
                            if replayed
                            else None
                        ),
                    }
                )
        return Response(200, json={"events": events, "next_after_sequence": None})

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await verify_plan(
            plan_path=plan,
            receipts_path=receipts_path,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
            sample_size=2,
        )

    assert summary.receipts == 2
    assert summary.retrieved == 2
    assert summary.replayed == 2
    assert summary.audited == 2
    assert [request.url.path for request in calls] == [
        "/v1/retrieve",
        "/v1/retrieve",
        "/v1/read-audit-events",
        "/v1/ingest",
        "/v1/ingest",
        "/v1/read-audit-events",
    ]
    assert all(
        "Idempotency-Key" not in request.headers
        for request in (calls[0], calls[1], calls[2], calls[-1])
    )


@pytest.mark.anyio
async def test_verify_rejects_duplicate_commit_audit_events(tmp_path: Path) -> None:
    operation = _operation(1)
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, [operation])[0]
    fact_ids = cast(list[str], receipt["fact_ids"])

    def respond(request: Request) -> Response:
        if request.url.path == "/v1/retrieve":
            return Response(
                200,
                json={
                    "hits": [
                        {
                            "fact_id": fact_ids[0],
                            "assertion_id": receipt["assertion_id"],
                            "body": "SYNTHETIC-VERIFY-BODY-1",
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/read-audit-events"
        event = {
            "action_kind": "data",
            "action_code": "ingest",
            "outcome": "allow",
            "reason_code": "assertion_ingested",
            "idempotency_key": receipt["idempotency_key"],
            "affected_assertion_ids": [receipt["assertion_id"]],
            "affected_fact_ids": receipt["fact_ids"],
            "replay_of_mutation_id": None,
        }
        return Response(
            200,
            json={"events": [event, event], "next_after_sequence": None},
        )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(VerifyError, match="audit_mismatch"):
            await verify_plan(
                plan_path=plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
                sample_size=1,
            )


@pytest.mark.anyio
async def test_verify_refuses_a_committed_replay_outcome(tmp_path: Path) -> None:
    operation = _operation(1)
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, [operation])[0]
    respond, _state = _single_operation_responder(receipt, replay_outcome="committed")

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(VerifyError) as error:
            await verify_plan(
                plan_path=plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
                sample_size=1,
            )

    assert error.value.code == "replay_failed"


@pytest.mark.anyio
async def test_verify_refuses_a_missing_replay_audit_delta(tmp_path: Path) -> None:
    operation = _operation(1)
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, [operation])[0]
    respond, _state = _single_operation_responder(receipt, include_replay_audit=False)

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(VerifyError) as error:
            await verify_plan(
                plan_path=plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
                sample_size=1,
            )

    assert error.value.code == "audit_mismatch"


@pytest.mark.anyio
@pytest.mark.parametrize("mismatch", ["fact_id", "body"])
async def test_verify_refuses_a_retrieval_hit_with_wrong_identity_or_body(
    tmp_path: Path, mismatch: str
) -> None:
    operation = _operation(1)
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, [operation])[0]
    respond, _state = _single_operation_responder(
        receipt,
        retrieval_fact_id=(
            "99999999-2222-4222-8222-222222222222" if mismatch == "fact_id" else None
        ),
        retrieval_body=(
            "SYNTHETIC-WRONG-RETRIEVAL-BODY"
            if mismatch == "body"
            else "SYNTHETIC-VERIFY-BODY-1"
        ),
    )

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        with pytest.raises(VerifyError) as error:
            await verify_plan(
                plan_path=plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
                sample_size=1,
            )

    assert error.value.code == "retrieve_mismatch"


@pytest.mark.anyio
async def test_verify_refuses_a_receipts_file_missing_a_plan_line(
    tmp_path: Path,
) -> None:
    operations = [_operation(1), _operation(2)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts_path = tmp_path / "receipts.jsonl"
    _write_receipts(receipts_path, operations[:1])

    async with AsyncClient(base_url="https://cairn.invalid") as client:
        with pytest.raises(VerifyError) as error:
            await verify_plan(
                plan_path=plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential="synthetic-token",
                sample_size=1,
            )

    assert error.value.code == "receipt_missing"


@pytest.mark.anyio
async def test_verify_retries_index_pending_during_retrieval_sampling(
    tmp_path: Path,
) -> None:
    operation = _operation(1)
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, [operation])[0]
    respond, state = _single_operation_responder(receipt, pending_retrievals=1)
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await verify_plan(
            plan_path=plan,
            receipts_path=receipts_path,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
            sample_size=1,
            sleep=record_delay,
        )

    assert summary.retrieved == 1
    assert state["retrieval_attempts"] == 2
    assert delays == [0.0]


@pytest.mark.anyio
async def test_verify_replays_a_ruled_invalidation_without_retrieval(
    tmp_path: Path,
) -> None:
    operation = _operation(1)
    operation["operation"] = "invalidate"
    invalidated_fact_ids = ["00000001-2222-4222-8222-222222222222"]
    operation["request"] = {
        "fact_ids": invalidated_fact_ids,
        "reason": "legacy contradiction ruled for carry-over",
    }
    plan = _write_plan(tmp_path / "plan", [operation])
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = {
        "instance_id": INSTANCE_ID_TEXT,
        "operation": "invalidate",
        "store": operation["store"],
        "legacy_id": operation["legacy_id"],
        "idempotency_key": operation["idempotency_key"],
        "assertion_id": None,
        "fact_ids": invalidated_fact_ids,
    }
    receipts_path.write_text(canonical_json(receipt) + "\n", encoding="utf-8")
    replayed = False
    calls: list[str] = []

    def respond(request: Request) -> Response:
        nonlocal replayed
        calls.append(request.url.path)
        if request.url.path == "/v1/invalidate":
            replayed = True
            return Response(
                200,
                json={
                    "outcome": "replayed",
                    "result": {
                        "fact_ids": receipt["fact_ids"],
                        "invalidated_at": "2026-08-21T12:00:00.000000Z",
                    },
                },
            )
        assert request.url.path == "/v1/read-audit-events"
        reasons = ["facts_invalidated"]
        if replayed:
            reasons.append("idempotent_replay")
        events = [
            {
                "action_kind": "data",
                "action_code": "invalidate",
                "outcome": "allow",
                "reason_code": reason,
                "idempotency_key": receipt["idempotency_key"],
                "affected_assertion_ids": [],
                "affected_fact_ids": receipt["fact_ids"],
                "replay_of_mutation_id": (
                    "00000001-3333-4333-8333-333333333333"
                    if reason == "idempotent_replay"
                    else None
                ),
            }
            for reason in reasons
        ]
        return Response(200, json={"events": events, "next_after_sequence": None})

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await verify_plan(
            plan_path=plan,
            receipts_path=receipts_path,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
            sample_size=1,
        )

    assert summary.retrieved == 0
    assert summary.replayed == 1
    assert summary.audited == 1
    assert calls == [
        "/v1/read-audit-events",
        "/v1/invalidate",
        "/v1/read-audit-events",
    ]


@pytest.mark.anyio
async def test_verify_pairs_mixed_operations_for_one_legacy_record(
    tmp_path: Path,
) -> None:
    ingest = _operation(1)
    invalidate = dict(ingest)
    invalidate["operation"] = "invalidate"
    fact_ids = ["00000001-2222-4222-8222-222222222222"]
    invalidate["request"] = {
        "fact_ids": fact_ids,
        "reason": "legacy contradiction ruled for carry-over",
    }
    plan = _write_plan(tmp_path / "plan", [ingest, invalidate])
    receipts = [
        {
            "instance_id": INSTANCE_ID_TEXT,
            "operation": "ingest",
            "store": ingest["store"],
            "legacy_id": ingest["legacy_id"],
            "idempotency_key": ingest["idempotency_key"],
            "assertion_id": "00000001-1111-4111-8111-111111111111",
            "fact_ids": fact_ids,
        },
        {
            "instance_id": INSTANCE_ID_TEXT,
            "operation": "invalidate",
            "store": invalidate["store"],
            "legacy_id": invalidate["legacy_id"],
            "idempotency_key": invalidate["idempotency_key"],
            "assertion_id": None,
            "fact_ids": fact_ids,
        },
    ]
    receipts_path = tmp_path / "receipts.jsonl"
    receipts_path.write_text(
        "".join(canonical_json(receipt) + "\n" for receipt in receipts),
        encoding="utf-8",
    )
    replayed: set[str] = set()

    def audit_event(operation: str, *, replay: bool) -> dict[str, object]:
        receipt = receipts[0 if operation == "ingest" else 1]
        return {
            "action_kind": "data",
            "action_code": operation,
            "outcome": "allow",
            "reason_code": (
                "idempotent_replay"
                if replay
                else (
                    "assertion_ingested"
                    if operation == "ingest"
                    else "facts_invalidated"
                )
            ),
            "idempotency_key": receipt["idempotency_key"],
            "affected_assertion_ids": (
                [receipt["assertion_id"]] if operation == "ingest" else []
            ),
            "affected_fact_ids": fact_ids,
            "replay_of_mutation_id": (
                "00000001-3333-4333-8333-333333333333" if replay else None
            ),
        }

    def respond(request: Request) -> Response:
        if request.url.path == "/v1/retrieve":
            return Response(
                200,
                json={
                    "hits": [
                        {
                            "fact_id": fact_ids[0],
                            "assertion_id": receipts[0]["assertion_id"],
                            "body": "SYNTHETIC-VERIFY-BODY-1",
                        }
                    ]
                },
            )
        if request.url.path in {"/v1/ingest", "/v1/invalidate"}:
            operation = request.url.path.removeprefix("/v1/")
            replayed.add(operation)
            receipt = receipts[0 if operation == "ingest" else 1]
            result: dict[str, object] = {"fact_ids": fact_ids}
            if operation == "ingest":
                result.update(assertion_id=receipt["assertion_id"], evidence_id=None)
            else:
                result["invalidated_at"] = "2026-08-21T12:00:00.000000Z"
            return Response(200, json={"outcome": "replayed", "result": result})
        events = [
            audit_event(operation, replay=False)
            for operation in ("ingest", "invalidate")
        ]
        events.extend(
            audit_event(operation, replay=True) for operation in sorted(replayed)
        )
        return Response(200, json={"events": events, "next_after_sequence": None})

    async with AsyncClient(
        transport=MockTransport(respond), base_url="https://cairn.invalid"
    ) as client:
        summary = await verify_plan(
            plan_path=plan,
            receipts_path=receipts_path,
            expected_instance=INSTANCE_ID_TEXT,
            client=client,
            credential="synthetic-token",
            sample_size=2,
        )

    assert summary.receipts == 2
    assert summary.retrieved == 0
    assert summary.replayed == 2
    assert summary.audited == 2


@pytest.mark.anyio
async def test_full_plan_applies_and_verifies_through_the_real_app(
    tmp_path: Path,
) -> None:
    """The Task 3 acceptance seam: real authentication, both root grants,
    custody, retrieval, idempotent replay and audit are exercised together."""
    operations = [_operation(1), _operation(2)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts = tmp_path / "receipts.jsonl"
    config, index, application, token = _in_process_instance(tmp_path / "instance")

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://cairn"
        ) as client:
            applied = await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
            )
            _drain(config, index)
            with _open_write_connection(config.paths.data, create=False) as connection:
                assertions_before_replay = connection.execute(
                    "SELECT count(*) FROM assertions"
                ).fetchone()[0]
            verified = await verify_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
                sample_size=2,
            )

    assert applied.applied == 2
    assert applied.resumed == 0
    assert verified.receipts == 2
    assert verified.retrieved == 2
    assert verified.replayed == 2
    assert verified.audited == 2
    catalogue = verify_catalogue(config)
    assert assertions_before_replay == 2
    assert catalogue.assertion_count == assertions_before_replay
    assert catalogue.idempotency_count == 2


@pytest.mark.anyio
async def test_stage_a_verify_skips_only_retrieval_when_projection_is_disabled(
    tmp_path: Path,
) -> None:
    operations = [_operation(1), _operation(2)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts = tmp_path / "receipts.jsonl"
    config, _index, application, token = _in_process_instance(
        tmp_path / "instance", projection_enabled=False
    )

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://cairn"
        ) as client:
            applied = await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
            )
            with _open_write_connection(config.paths.data, create=False) as connection:
                assertions_before_replay = connection.execute(
                    "SELECT count(*) FROM assertions"
                ).fetchone()[0]
            verified = await verify_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
                sample_size=2,
                skip_retrieval=True,
            )

    assert applied.applied == 2
    assert verified.receipts == 2
    assert verified.retrieved == 0
    assert verified.replayed == 2
    assert verified.audited == 2
    assert assertions_before_replay == 2
    assert verify_catalogue(config).assertion_count == assertions_before_replay


@pytest.mark.anyio
async def test_stage_a_verify_without_skip_refuses_disabled_retrieval(
    tmp_path: Path,
) -> None:
    operations = [_operation(1)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts = tmp_path / "receipts.jsonl"
    _config, _index, application, token = _in_process_instance(
        tmp_path / "instance", projection_enabled=False
    )

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://cairn"
        ) as client:
            await apply_plan(
                plan_path=plan,
                receipts_path=receipts,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
            )
            with pytest.raises(VerifyError) as error:
                await verify_plan(
                    plan_path=plan,
                    receipts_path=receipts,
                    expected_instance=INSTANCE_ID_TEXT,
                    client=client,
                    credential=token,
                    sample_size=1,
                )

    assert error.value.code == "retrieve_failed"


@pytest.mark.anyio
async def test_real_verify_skips_current_retrieval_for_plan_invalidations(
    tmp_path: Path,
) -> None:
    ingest = _operation(1)
    ingest_plan = _write_plan(tmp_path / "ingest-plan", [ingest])
    receipts_path = tmp_path / "receipts.jsonl"
    config, index, application, token = _in_process_instance(tmp_path / "instance")

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://cairn"
        ) as client:
            await apply_plan(
                plan_path=ingest_plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
            )
            ingest_receipt = json.loads(
                receipts_path.read_text(encoding="utf-8").splitlines()[0]
            )
            fact_ids = cast(list[str], ingest_receipt["fact_ids"])
            invalidate = dict(ingest)
            invalidate["operation"] = "invalidate"
            invalidate["request"] = {
                "fact_ids": fact_ids,
                "reason": "legacy contradiction ruled for carry-over",
            }
            mixed_plan = _write_plan(tmp_path / "mixed-plan", [ingest, invalidate])
            applied = await apply_plan(
                plan_path=mixed_plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
            )
            _drain(config, index)
            verified = await verify_plan(
                plan_path=mixed_plan,
                receipts_path=receipts_path,
                expected_instance=INSTANCE_ID_TEXT,
                client=client,
                credential=token,
                sample_size=2,
            )

    assert applied.applied == 1
    assert applied.resumed == 1
    assert verified.receipts == 2
    assert verified.retrieved == 0
    assert verified.replayed == 2
    assert verified.audited == 2
    assert verify_catalogue(config).assertion_count == 1


@pytest.mark.parametrize("skip_retrieval", [False, True])
def test_verify_cli_reports_only_identity_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    skip_retrieval: bool,
) -> None:
    operations = [_operation(1)]
    plan = _write_plan(tmp_path / "plan", operations)
    receipts_path = tmp_path / "receipts.jsonl"
    receipt = _write_receipts(receipts_path, operations)[0]
    credential = tmp_path / "credential"
    credential.write_text("synthetic-secret-token\n", encoding="utf-8")
    replayed = False
    calls: list[str] = []

    def respond(request: Request) -> Response:
        nonlocal replayed
        calls.append(request.url.path)
        if request.url.path == "/v1/instance":
            return Response(200, json={"instance_id": INSTANCE_ID_TEXT})
        if request.url.path == "/v1/retrieve":
            fact_ids = cast(list[str], receipt["fact_ids"])
            return Response(
                200,
                json={
                    "hits": [
                        {
                            "fact_id": fact_ids[0],
                            "assertion_id": receipt["assertion_id"],
                            "body": "SYNTHETIC-VERIFY-BODY-1",
                        }
                    ],
                    "budget_consumed": 23,
                    "budget_exhausted": False,
                },
            )
        if request.url.path == "/v1/ingest":
            replayed = True
            return Response(
                200,
                json={
                    "outcome": "replayed",
                    "result": {
                        "assertion_id": receipt["assertion_id"],
                        "fact_ids": receipt["fact_ids"],
                        "evidence_id": None,
                    },
                },
            )
        return Response(
            200,
            json={
                "events": [
                    {
                        "action_kind": "data",
                        "action_code": "ingest",
                        "outcome": "allow",
                        "reason_code": reason,
                        "idempotency_key": receipt["idempotency_key"],
                        "affected_assertion_ids": [receipt["assertion_id"]],
                        "affected_fact_ids": receipt["fact_ids"],
                        "replay_of_mutation_id": (
                            "00000001-3333-4333-8333-333333333333"
                            if reason == "idempotent_replay"
                            else None
                        ),
                    }
                    for reason in (
                        ("assertion_ingested", "idempotent_replay")
                        if replayed
                        else ("assertion_ingested",)
                    )
                ],
                "next_after_sequence": None,
            },
        )

    arguments = [
        "verify",
        "--instance",
        "https://cairn.invalid",
        "--expected-instance",
        INSTANCE_ID_TEXT,
        "--credential-file",
        str(credential),
        "--plan",
        str(plan),
        "--receipts",
        str(receipts_path),
        "--sample-size",
        "1",
    ]
    if skip_retrieval:
        arguments.append("--skip-retrieval")
    exit_code = main(
        arguments,
        client_factory=lambda instance: AsyncClient(
            transport=MockTransport(respond), base_url=instance
        ),
    )

    assert exit_code == 0
    output = capsys.readouterr()
    assert "synthetic-secret-token" not in output.out
    assert "SYNTHETIC-VERIFY-BODY" not in output.out
    assert json.loads(output.out) == {
        "audited": 1,
        "receipts": 1,
        "replayed": 1,
        "retrieved": 0 if skip_retrieval else 1,
        "status": "ok",
    }
    assert ("/v1/retrieve" in calls) is not skip_retrieval
    assert calls.count("/v1/ingest") == 1
    assert calls.count("/v1/read-audit-events") == 2
