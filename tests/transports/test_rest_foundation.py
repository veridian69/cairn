import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import RFC_4122, UUID, uuid1, uuid4

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import cairn.runtime.composition as composition_module
from cairn.authority.gate import INTERNAL_ERROR_MESSAGE
from cairn.catalogue.audit import ActionKind, AuditDraft, ChainKind, Outcome
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.lease import LeaseError
from cairn.runtime.logging import Operation, configure_logging
from cairn.transports.rest.middleware import FoundationMiddleware

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
CALLER_CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SENTINEL = "super-secret-value"
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


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


@pytest.mark.anyio
async def test_live_and_startup_are_green_after_lifespan(tmp_path: Path) -> None:
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            live = await client.get("/health/live")
            startup = await client.get("/health/startup")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert startup.status_code == 200
    assert startup.json() == {"status": "started"}


@pytest.mark.anyio
async def test_ready_is_503_before_authority_and_green_after_verification(
    tmp_path: Path,
) -> None:
    application = build_application(make_config(tmp_path))

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        before_startup = await client.get("/health/ready")

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            after_startup = await client.get("/health/ready")
            metrics = await client.get("/metrics")

    assert before_startup.status_code == 503
    assert before_startup.json() == {"status": "not-ready"}
    assert after_startup.status_code == 200
    assert after_startup.json() == {"status": "ready"}
    assert 'operation="health_ready",outcome_code="unavailable"' in metrics.text
    assert 'operation="health_ready",outcome_code="success"' in metrics.text


@pytest.mark.anyio
async def test_response_has_canonical_correlation_uuid(tmp_path: Path) -> None:
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/health/live")

    correlation_id = response.headers["X-Correlation-ID"]
    parsed = UUID(correlation_id)
    assert str(parsed) == correlation_id
    assert parsed.version == 4


@pytest.mark.anyio
async def test_valid_caller_correlation_uuid_is_adopted(tmp_path: Path) -> None:
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/health/live",
                headers={"X-Correlation-ID": str(CALLER_CORRELATION_ID)},
            )

    assert response.headers["X-Correlation-ID"] == str(CALLER_CORRELATION_ID)


@pytest.mark.anyio
async def test_invalid_caller_correlation_uuid_is_replaced(tmp_path: Path) -> None:
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/health/live",
                headers={"X-Correlation-ID": "not-a-canonical-uuid"},
            )

    correlation_id = response.headers["X-Correlation-ID"]
    parsed = UUID(correlation_id)
    assert correlation_id != "not-a-canonical-uuid"
    assert str(parsed) == correlation_id
    assert parsed.version == 4


@pytest.mark.anyio
@pytest.mark.parametrize(
    "submitted",
    [
        pytest.param(str(uuid1()), id="version-1"),
        pytest.param("00000000-0000-4000-0000-000000000000", id="reserved-ncs-variant"),
    ],
)
async def test_a_canonical_but_not_version_4_correlation_uuid_is_replaced(
    tmp_path: Path, submitted: str
) -> None:
    """I-32 adopts a caller-supplied UUIDv4 and ignores anything else. The
    check has to match ``audit._validate_uuid`` on both halves, version
    and variant: the domain validates every correlation identifier that
    way, so a canonical UUID of any other shape used to be adopted here
    and then refused there — 500 on every route, and on the
    unauthenticated path the durable denial event was never appended at
    all, so one header suppressed the audit record of a failed
    authentication. Found by the Task 10 correctness review.
    """
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/health/live", headers={"X-Correlation-ID": submitted}
            )

    correlation_id = response.headers["X-Correlation-ID"]
    parsed = UUID(correlation_id)
    assert correlation_id != submitted
    assert parsed.version == 4
    assert parsed.variant == RFC_4122


@pytest.mark.anyio
async def test_metrics_is_prometheus_text_and_contains_no_identity(
    tmp_path: Path,
) -> None:
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/metrics")

    assert response.status_code == 200
    assert (
        response.headers["content-type"] == "text/plain; version=1.0.0; charset=utf-8"
    )
    assert str(INSTANCE_ID) not in response.text


def test_the_registered_routes_are_exactly_the_expected_set(tmp_path: Path) -> None:
    application = build_application(make_config(tmp_path))

    paths = {route.path for route in application.routes if isinstance(route, Route)}

    assert paths == {
        "/health/live",
        "/health/startup",
        "/health/ready",
        "/metrics",
        # Slice 5 Tasks 6-9: the complete I-70 route map. P-32's formal
        # inventory proof lives in tests/transports/rest/v1/test_openapi.py,
        # which checks the same set against the generated artefact from
        # the other side; this one holds the literal.
        "/v1/ingest",
        "/v1/promote",
        "/v1/invalidate",
        "/v1/create-principal",
        "/v1/issue-credential",
        "/v1/revoke-credential",
        "/v1/create-grant",
        "/v1/revoke-grant",
        "/v1/read-audit-events",
        "/v1/retrieve",
        "/v1/instance",
        # Slice 7 Task 3: the I-84 MCP transport. One route inside `/v1`
        # that is not a verb-named operation route — I-84 qualifies I-70
        # in that one respect and no other, adding no operation. Listed
        # here because it is genuinely served; the count that holds it to
        # "no new operation" is in tests/transports/mcp/test_mount.py,
        # against the eleven the OpenAPI artefact publishes.
        "/v1/mcp",
        # The separate memory surface, including its explicit slash refusal.
        "/memory/v1/diagnose",
        "/memory/v1/remember",
        "/memory/v1/recall",
        "/memory/v1/history",
        "/memory/v1/disagree",
        "/memory/v1/resolve",
        "/memory/v1/correct",
        "/memory/v1/suggest",
        "/memory/v1/propose",
        "/memory/v1/proposal-read",
        "/memory/v1/proposal-list",
        "/memory/v1/proposal-accept",
        "/memory/v1/proposal-reject",
        "/memory/v1/session-open",
        "/memory/v1/session-read",
        "/memory/v1/turn-begin",
        "/memory/v1/turn-prepare",
        "/memory/v1/turn-commit",
        "/memory/v1/turn-abandon",
        "/memory/v1/visit-issue",
        "/memory/v1/visit-acknowledge",
        "/memory/v1/mcp",
        "/memory/v1/mcp/",
    }


# ``test_no_mcp_module_or_dependency_is_declared`` stood here until slice 7
# Task 2 (10 August 2026). It asserted that no module named for MCP and no
# MCP dependency existed — the programme's no-speculative-implementation
# constraint, made mechanical while MCP was still several slices away.
#
# Its obligation is discharged rather than abandoned: slice 7 is the slice
# that adds the transport, P-49 pins ``mcp==1.29.0`` on Operator's ruling of
# 10 August 2026, and a guard asserting the absence of a thing the plan
# now requires cannot be satisfied and should not be weakened into
# vacuity. What replaces it is narrower and still enforced —
# ``tests/transports/mcp/test_import_boundary.py`` bounds which SDK
# modules Cairn may reach, and the route-inventory assertion above remains
# the live guard on the served surface growing without a decision behind
# it.


def test_outbox_gauges_render_with_both_queue_labels_and_zero_is_not_absent() -> None:
    metrics = Metrics()

    metrics.set_outbox_state(OutboxQueue.EVIDENCE, depth=3, oldest_age_seconds=12.5)
    metrics.set_outbox_state(OutboxQueue.PROJECTION, depth=0, oldest_age_seconds=0)

    rendered, _ = metrics.render()
    text = rendered.decode("utf-8")
    assert 'cairn_outbox_depth{queue="evidence"} 3.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 12.5' in text
    # A gauge series prometheus_client never had `.labels(...).set()` called
    # for is missing from this text entirely, not present reading 0 — so an
    # explicitly zeroed series must still show up as its own line. This is
    # what distinguishes "set to zero" from "never set" (the empty-queue
    # case chose zero, deliberately, over an absent series).
    assert 'cairn_outbox_depth{queue="projection"} 0.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="projection"} 0.0' in text


def test_set_outbox_state_rejects_a_non_enum_queue() -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        metrics.set_outbox_state(cast(Any, "evidence"), depth=0, oldest_age_seconds=0.0)


@pytest.mark.parametrize("depth", [True, 1.5, -1])
def test_set_outbox_state_rejects_invalid_depth(depth: object) -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        metrics.set_outbox_state(
            OutboxQueue.EVIDENCE, depth=cast(Any, depth), oldest_age_seconds=0.0
        )


@pytest.mark.parametrize(
    "oldest_age_seconds",
    ["12", float("nan"), float("inf"), float("-inf"), -1.0, 10**400],
)
def test_set_outbox_state_rejects_invalid_age(oldest_age_seconds: object) -> None:
    metrics = Metrics()

    with pytest.raises(TypeError):
        metrics.set_outbox_state(
            OutboxQueue.EVIDENCE,
            depth=0,
            oldest_age_seconds=cast(Any, oldest_age_seconds),
        )


def test_evidence_digest_mismatch_counter_renders_and_increments() -> None:
    metrics = Metrics()

    metrics.observe_evidence_digest_mismatch()
    metrics.observe_evidence_digest_mismatch()

    rendered, _ = metrics.render()
    assert b"cairn_evidence_digest_mismatch_total 2.0" in rendered


def _canonical_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_evidence_outbox_row(data_path: Path, *, created_at: datetime) -> None:
    """Inserts one durable evidence_outbox row directly, bypassing ingest.

    This module needs only that a pending row exists, not how it got there —
    but startup verifies the catalogue before it samples the gauges, so the
    row must be one ingest could actually have produced. That means the
    exact-custody evidence form (the queue carries payload bytes, and only
    exact custody has any), a digest and length agreeing with the payload,
    the principal and assertion that form names, and one audit event carrying
    the same ``mutation_id``: P-17 gives the outbox column no foreign key
    precisely so that catalogue verification can cross-check it against the
    audit chain, so a fabricated mutation identity is itself detected
    corruption.
    """
    realm_id = "acme"
    principal_id = uuid4()
    assertion_id = uuid4()
    evidence_id = uuid4()
    mutation_id = uuid4()
    payload = b"payload"
    recorded_at = _canonical_ts(created_at)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO realms (realm_id, created_at) VALUES (?, ?)",
            (realm_id, recorded_at),
        )
        connection.execute(
            "INSERT OR IGNORE INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (realm_id, bytes(32)),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, 'workload', ?, ?)",
            # principals.label is unique and this helper is called more than
            # once per catalogue.
            (str(principal_id), f"agent-{principal_id.hex[:8]}", recorded_at),
        )
        connection.execute(
            "INSERT INTO assertions (assertion_id, realm_id, scope_segments, "
            "classification, source_type, principal_id, observed_at, metadata, "
            "recorded_at) VALUES (?, ?, '[]', 'internal', 'agent-claim', ?, "
            "NULL, NULL, ?)",
            (str(assertion_id), realm_id, str(principal_id), recorded_at),
        )
        connection.execute(
            "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
            "classification, payload_digest, assertion_id, payload_length, "
            "external_uri, recorded_at) "
            "VALUES (?, ?, '[]', 'internal', ?, ?, ?, NULL, ?)",
            (
                str(evidence_id),
                realm_id,
                hashlib.sha256(payload).digest(),
                str(assertion_id),
                len(payload),
                recorded_at,
            ),
        )
        connection.execute(
            "INSERT INTO evidence_outbox (work_id, kind, evidence_id, mutation_id, "
            "payload, created_at, attempts) "
            "VALUES (?, 'store-payload', ?, ?, ?, ?, 0)",
            (
                str(uuid4()),
                str(evidence_id),
                str(mutation_id),
                payload,
                recorded_at,
            ),
        )
        connection.commit()
    _append_mutation_event(data_path, mutation_id, created_at)


def _append_mutation_event(
    data_path: Path, mutation_id: UUID, recorded_at: datetime
) -> None:
    """The audit counterpart of the outbox row above, on the instance chain.

    A real mutation appends its event and its outbox rows in one transaction;
    this appends the event separately, which is enough for the cross-check
    since it asks only that the mutation identity appears in the chain.
    """
    CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: recorded_at,
        uuid_factory=uuid4,
    ).append_audit(
        AuditDraft(
            chain_kind=ChainKind.INSTANCE,
            chain_identity=str(INSTANCE_ID),
            principal_id=None,
            credential_verifier_id=None,
            grant_id=None,
            action_kind=ActionKind.SYSTEM,
            action_code="catalogue-check",
            source_scope=None,
            requested_scope=None,
            target_scope=None,
            outcome=Outcome.ALLOW,
            reason_code="seeded_outbox_row",
            affected_assertion_ids=(),
            affected_fact_ids=(),
            affected_evidence_ids=(),
            affected_grant_ids=(),
            classification_transition=None,
            trust_transition=None,
            evidence_reference=None,
            evidence_digest=None,
            correlation_id=CALLER_CORRELATION_ID,
            idempotency_key=None,
            mutation_id=mutation_id,
            command_digest=None,
            replay_of_mutation_id=None,
            safe_request_fingerprint=None,
        )
    )


@pytest.mark.anyio
async def test_startup_samples_both_outbox_queues_after_verification(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    older = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 8, 6, 12, 0, 10, tzinfo=UTC)
    sampled_at = datetime(2026, 8, 6, 12, 0, 30, tzinfo=UTC)
    _seed_evidence_outbox_row(config.paths.data, created_at=older)
    _seed_evidence_outbox_row(config.paths.data, created_at=newer)
    application = build_application(config, clock=lambda: sampled_at)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/metrics")

    text = response.text
    assert 'cairn_outbox_depth{queue="evidence"} 2.0' in text
    # The oldest row is 30 seconds behind the injected sample clock; the
    # newer row must not win — the gauge tracks the oldest, not the newest.
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 30.0' in text
    # projection_outbox has no rows and no deliverer anywhere in this slice
    # (P-18 non-goal), so startup sampling is the only thing that ever sets
    # its gauges — and the empty case must read as a set zero, not an
    # absent series (see the equivalent unit-level assertion above).
    assert 'cairn_outbox_depth{queue="projection"} 0.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="projection"} 0.0' in text


@pytest.mark.anyio
async def test_startup_survives_a_future_dated_outbox_row(tmp_path: Path) -> None:
    """A row written ahead of the sampling clock must not stop the service.

    ``set_outbox_state`` refuses a negative age with a ``TypeError``, and
    startup sampling is not inside the ``CatalogueStorageError`` guard that
    handles an unreadable ``created_at`` — so unclamped this aborts the
    lifespan and the service never becomes ready, over a gauge, on a
    catalogue with nothing wrong with it. A clock stepping backwards between
    the ingesting process's write and this read is enough to produce it.
    """
    config = make_config(tmp_path)
    created_at = datetime(2026, 8, 6, 12, 0, 30, tzinfo=UTC)
    sampled_at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
    _seed_evidence_outbox_row(config.paths.data, created_at=created_at)
    application = build_application(config, clock=lambda: sampled_at)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            ready = await client.get("/health/ready")
            response = await client.get("/metrics")

    assert ready.status_code == 200
    text = response.text
    assert 'cairn_outbox_depth{queue="evidence"} 1.0' in text
    assert 'cairn_outbox_oldest_age_seconds{queue="evidence"} 0.0' in text


@pytest.mark.anyio
async def test_second_application_cannot_start_on_same_data_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_stream = StringIO()
    test_logger = configure_logging(log_stream)
    monkeypatch.setattr(
        composition_module,
        "configure_logging",
        lambda stream: test_logger,
    )
    config = make_config(tmp_path)
    first_application = build_application(config)
    second_application = build_application(config)

    async with LifespanManager(first_application):
        with pytest.raises(LeaseError) as raised:
            async with LifespanManager(second_application):
                pass

    assert raised.value.code == "already_locked"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    startup_failure = [
        record for record in records if record["event"] == "runtime_start_failed"
    ]
    assert startup_failure == [
        {
            "event": "runtime_start_failed",
            "failure_code": "already_locked",
            "instance_id": str(INSTANCE_ID),
            "time": startup_failure[0]["time"],
        }
    ]
    assert str(tmp_path) not in log_stream.getvalue()


@pytest.mark.anyio
async def test_unexpected_failure_is_fixed_and_observability_is_safe() -> None:
    log_stream = StringIO()
    logger = configure_logging(log_stream)
    metrics = Metrics()
    application = FastAPI()
    application.add_middleware(
        FoundationMiddleware,
        logger=logger,
        metrics=metrics,
    )

    @application.post("/explode", name=Operation.HEALTH_LIVE.value)
    async def explode(request: Request) -> None:
        assert request.state.correlation_id == CALLER_CORRELATION_ID
        raise RuntimeError(f"{SENTINEL} /private/{SENTINEL} config={SENTINEL}")

    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/explode?query={SENTINEL}",
            headers={
                "X-Correlation-ID": str(CALLER_CORRELATION_ID),
                "Authorization": f"Bearer {SENTINEL}",
            },
            content=f"body={SENTINEL}",
        )

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == str(CALLER_CORRELATION_ID)
    assert response.json() == {
        "failure": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "retry": "never",
            "correlation_id": str(CALLER_CORRELATION_ID),
        }
    }
    assert SENTINEL not in response.text

    lines = log_stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "request_completed"
    assert payload["operation"] == "health_live"
    assert payload["outcome_code"] == "internal_error"
    assert payload["correlation_id"] == str(CALLER_CORRELATION_ID)
    assert payload["exception_type"] == "RuntimeError"
    assert payload["stack_location"].startswith("test_rest_foundation.py:")
    assert SENTINEL not in log_stream.getvalue()

    rendered_metrics, _ = metrics.render()
    assert (
        'operation="health_live",outcome_code="internal_error"'
        in rendered_metrics.decode("utf-8")
    )


@pytest.mark.anyio
async def test_handled_internal_response_is_replaced_without_leaking() -> None:
    log_stream = StringIO()
    logger = configure_logging(log_stream)
    metrics = Metrics()
    application = FastAPI()
    application.add_middleware(
        FoundationMiddleware,
        logger=logger,
        metrics=metrics,
    )

    @application.get("/handled", name=Operation.HEALTH_LIVE.value)
    async def handled_failure() -> Response:
        return PlainTextResponse(
            SENTINEL,
            status_code=500,
            headers={"X-Downstream-Secret": SENTINEL},
        )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/handled",
            headers={"X-Correlation-ID": str(CALLER_CORRELATION_ID)},
        )

    assert_fixed_internal_failure(response)
    assert "X-Downstream-Secret" not in response.headers
    assert SENTINEL not in response.text
    assert_safe_internal_observability(log_stream, metrics)


@pytest.mark.anyio
async def test_failure_after_response_start_is_replaced_without_leaking() -> None:
    class StartedThenFailedResponse(Response):
        async def __call__(
            self,
            scope: Scope,
            receive: Receive,
            send: Send,
        ) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"x-downstream-secret", SENTINEL.encode("ascii")),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": SENTINEL.encode("ascii"),
                    "more_body": True,
                }
            )
            raise RuntimeError(f"exception-message={SENTINEL}")

    log_stream = StringIO()
    logger = configure_logging(log_stream)
    metrics = Metrics()
    application = FastAPI()
    application.add_middleware(
        FoundationMiddleware,
        logger=logger,
        metrics=metrics,
    )

    @application.get("/late-failure", name=Operation.HEALTH_LIVE.value)
    async def late_failure() -> Response:
        return StartedThenFailedResponse()

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/late-failure",
            headers={"X-Correlation-ID": str(CALLER_CORRELATION_ID)},
        )

    assert_fixed_internal_failure(response)
    assert "X-Downstream-Secret" not in response.headers
    assert SENTINEL not in response.text
    assert_safe_internal_observability(
        log_stream,
        metrics,
        exception_type="RuntimeError",
    )


def assert_fixed_internal_failure(response: HTTPXResponse) -> None:
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Correlation-ID"] == str(CALLER_CORRELATION_ID)
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.json() == {
        "failure": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "retry": "never",
            "correlation_id": str(CALLER_CORRELATION_ID),
        }
    }


def assert_safe_internal_observability(
    log_stream: StringIO,
    metrics: Metrics,
    *,
    operation: Operation = Operation.HEALTH_LIVE,
    exception_type: str | None = None,
) -> None:
    lines = log_stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "request_completed"
    assert payload["operation"] == operation.value
    assert payload["outcome_code"] == "internal_error"
    assert payload["correlation_id"] == str(CALLER_CORRELATION_ID)
    if exception_type is None:
        assert "exception_type" not in payload
        assert "stack_location" not in payload
    else:
        assert payload["exception_type"] == exception_type
        assert payload["stack_location"].startswith("test_rest_foundation.py:")
    assert SENTINEL not in log_stream.getvalue()

    rendered_metrics, _ = metrics.render()
    assert (
        f'operation="{operation.value}",outcome_code="internal_error"'
        in rendered_metrics.decode("utf-8")
    )


@pytest.mark.anyio
async def test_arbitrary_health_503_is_replaced_without_leaking() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"x-downstream-secret", SENTINEL.encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": SENTINEL.encode("ascii"),
            }
        )

    messages, log_stream, metrics = await run_foundation_middleware(
        downstream,
        operation=Operation.HEALTH_READY,
    )

    assert_fixed_asgi_internal_failure(messages)
    assert SENTINEL not in repr(messages)
    assert_safe_internal_observability(
        log_stream,
        metrics,
        operation=Operation.HEALTH_READY,
    )


@pytest.mark.anyio
async def test_mutating_sent_message_dictionary_cannot_bypass_500() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        start: Message = {
            "type": "http.response.start",
            "status": 500,
            "headers": [(b"content-type", b"text/plain")],
        }
        await send(start)
        start["status"] = 200
        await send(
            {
                "type": "http.response.body",
                "body": SENTINEL.encode("ascii"),
            }
        )

    messages, log_stream, metrics = await run_foundation_middleware(downstream)

    assert_fixed_asgi_internal_failure(messages)
    assert SENTINEL not in repr(messages)
    assert_safe_internal_observability(log_stream, metrics)


@pytest.mark.anyio
async def test_mutating_sent_header_collection_cannot_change_replay() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"content-type", b"text/plain")]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": headers,
            }
        )
        headers.append((b"x-downstream-secret", SENTINEL.encode("ascii")))
        await send(
            {
                "type": "http.response.body",
                "body": b"safe",
            }
        )

    messages, log_stream, metrics = await run_foundation_middleware(downstream)

    status, headers, body = unpack_asgi_response(messages)
    assert status == 200
    assert headers["content-type"] == "text/plain"
    assert headers["x-correlation-id"] == str(CALLER_CORRELATION_ID)
    assert "x-downstream-secret" not in headers
    assert body == b"safe"
    assert SENTINEL not in repr(messages)
    assert_success_observability(log_stream, metrics)


@pytest.mark.anyio
async def test_incomplete_more_body_response_is_replaced() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": SENTINEL.encode("ascii"),
                "more_body": True,
            }
        )

    messages, log_stream, metrics = await run_foundation_middleware(downstream)

    assert_fixed_asgi_internal_failure(messages)
    assert SENTINEL not in repr(messages)
    assert_safe_internal_observability(log_stream, metrics)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "downstream_messages",
    [
        pytest.param(
            [
                {
                    "type": "http.response.body",
                    "body": SENTINEL.encode("ascii"),
                },
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                },
            ],
            id="body-before-start",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": ((b"content-type", b"text/plain"),),
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                },
            ],
            id="non-list-headers",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/plain"]],
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                },
            ],
            id="non-tuple-header-pair",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                },
                {
                    "type": "http.response.body",
                    "body": SENTINEL,
                },
            ],
            id="non-bytes-body",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                    "more_body": 0,
                },
            ],
            id="non-boolean-more-body",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                },
                {
                    "type": "http.response.trailers",
                    "headers": [(b"x-downstream-secret", SENTINEL.encode("ascii"))],
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                },
            ],
            id="unexpected-message-type",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                },
                {
                    "type": "http.response.body",
                    "body": b"safe",
                },
                {
                    "type": "http.response.body",
                    "body": SENTINEL.encode("ascii"),
                },
            ],
            id="message-after-terminal",
        ),
        pytest.param(
            [
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            ],
            id="missing-terminal-body",
        ),
    ],
)
async def test_malformed_completed_response_is_replaced(
    downstream_messages: list[dict[str, object]],
) -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        for message in downstream_messages:
            await send(cast(Message, message))

    messages, log_stream, metrics = await run_foundation_middleware(downstream)

    assert_fixed_asgi_internal_failure(messages)
    assert SENTINEL not in repr(messages)
    assert_safe_internal_observability(log_stream, metrics)


@pytest.mark.anyio
async def test_replay_failure_is_never_observed_as_success() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"safe",
            }
        )

    log_stream = StringIO()
    metrics = Metrics()
    send_attempts = 0

    async def failing_send(message: Message) -> None:
        nonlocal send_attempts
        send_attempts += 1
        raise RuntimeError(f"send-failure={SENTINEL}")

    with pytest.raises(RuntimeError):
        await invoke_foundation_middleware(
            downstream,
            failing_send,
            log_stream,
            metrics,
        )

    assert send_attempts == 1
    assert_safe_internal_observability(
        log_stream,
        metrics,
        exception_type="RuntimeError",
    )
    assert 'outcome_code="success"' not in log_stream.getvalue()
    rendered_metrics, _ = metrics.render()
    assert (
        'operation="health_live",outcome_code="success"'
        not in rendered_metrics.decode("utf-8")
    )


@pytest.mark.anyio
async def test_late_send_during_replay_cannot_truncate_sealed_response() -> None:
    replay_started = asyncio.Event()
    late_send_finished = asyncio.Event()
    late_task: asyncio.Task[None] | None = None
    messages: list[dict[str, object]] = []
    log_stream = StringIO()
    metrics = Metrics()

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal late_task
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"safe",
            }
        )

        async def send_after_terminal() -> None:
            try:
                await replay_started.wait()
                await send(
                    {
                        "type": "http.response.body",
                        "body": SENTINEL.encode("ascii"),
                    }
                )
            finally:
                late_send_finished.set()

        late_task = asyncio.create_task(send_after_terminal())

    async def yielding_send(message: Message) -> None:
        captured: dict[str, object] = dict(message)
        headers = captured.get("headers")
        if type(headers) is list:
            captured["headers"] = list(headers)
        messages.append(captured)
        if message["type"] == "http.response.start":
            replay_started.set()
            await late_send_finished.wait()

    await invoke_foundation_middleware(
        downstream,
        yielding_send,
        log_stream,
        metrics,
    )
    assert late_task is not None
    await late_task

    payload = json.loads(log_stream.getvalue())
    assert (
        [message.get("type") for message in messages],
        payload["outcome_code"],
    ) == (
        ["http.response.start", "http.response.body"],
        "internal_error",
    )
    status, headers, body = unpack_asgi_response(messages)
    assert status == 200
    assert headers["content-type"] == "text/plain"
    assert headers["x-correlation-id"] == str(CALLER_CORRELATION_ID)
    assert body == b"safe"
    assert SENTINEL not in repr(messages)
    assert_safe_internal_observability(log_stream, metrics)
    rendered_metrics, _ = metrics.render()
    assert (
        'operation="health_live",outcome_code="success"'
        not in rendered_metrics.decode("utf-8")
    )


async def run_foundation_middleware(
    downstream: ASGIApp,
    *,
    operation: Operation = Operation.HEALTH_LIVE,
) -> tuple[list[dict[str, object]], StringIO, Metrics]:
    messages: list[dict[str, object]] = []
    log_stream = StringIO()
    metrics = Metrics()

    async def capture(message: Message) -> None:
        captured: dict[str, object] = dict(message)
        headers = captured.get("headers")
        if type(headers) is list:
            captured["headers"] = list(headers)
        messages.append(captured)

    await invoke_foundation_middleware(
        downstream,
        capture,
        log_stream,
        metrics,
        operation=operation,
    )
    return messages, log_stream, metrics


async def invoke_foundation_middleware(
    downstream: ASGIApp,
    send: Send,
    log_stream: StringIO,
    metrics: Metrics,
    *,
    operation: Operation = Operation.HEALTH_LIVE,
) -> None:
    middleware = FoundationMiddleware(
        downstream,
        logger=configure_logging(log_stream),
        metrics=metrics,
    )
    scope = make_http_scope(operation)
    await middleware(scope, receive_empty_request, send)


def make_http_scope(operation: Operation) -> Scope:
    async def unused_endpoint(request: Request) -> None:
        raise AssertionError("route endpoint must not run")

    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-correlation-id", str(CALLER_CORRELATION_ID).encode("ascii"))],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "state": {},
        "route": Route("/test", unused_endpoint, name=operation.value),
    }


async def receive_empty_request() -> Message:
    return {
        "type": "http.request",
        "body": b"",
        "more_body": False,
    }


def assert_fixed_asgi_internal_failure(
    messages: list[dict[str, object]],
) -> None:
    status, headers, body = unpack_asgi_response(messages)
    assert status == 500
    assert headers["content-type"] == "application/json"
    assert headers["x-correlation-id"] == str(CALLER_CORRELATION_ID)
    assert int(headers["content-length"]) == len(body)
    assert json.loads(body) == {
        "failure": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "retry": "never",
            "correlation_id": str(CALLER_CORRELATION_ID),
        }
    }


def unpack_asgi_response(
    messages: list[dict[str, object]],
) -> tuple[int, dict[str, str], bytes]:
    assert len(messages) == 2
    start, body_message = messages
    assert start.get("type") == "http.response.start"
    status = start.get("status")
    assert type(status) is int
    raw_headers = start.get("headers")
    assert type(raw_headers) is list
    headers: dict[str, str] = {}
    for pair in raw_headers:
        assert type(pair) is tuple
        assert len(pair) == 2
        name, value = pair
        assert type(name) is bytes
        assert type(value) is bytes
        headers[name.decode("ascii")] = value.decode("ascii")

    assert body_message.get("type") == "http.response.body"
    body = body_message.get("body")
    assert type(body) is bytes
    assert body_message.get("more_body", False) is False
    return status, headers, body


def assert_success_observability(
    log_stream: StringIO,
    metrics: Metrics,
) -> None:
    lines = log_stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["operation"] == "health_live"
    assert payload["outcome_code"] == "success"
    assert SENTINEL not in log_stream.getvalue()

    rendered_metrics, _ = metrics.render()
    assert 'operation="health_live",outcome_code="success"' in rendered_metrics.decode(
        "utf-8"
    )


@pytest.mark.anyio
async def test_an_unknown_path_answers_in_the_failure_envelope(
    tmp_path: Path,
) -> None:
    """I-70: an unknown path returns the fixed ``not_found`` failure.
    Without the boundary handlers this fell through to Starlette's
    ``{"detail": "Not Found"}`` — the right status carrying the wrong
    body, so a caller parsing the documented envelope got nothing.
    """
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/does-not-exist")

    assert response.status_code == 404
    failure = response.json()["failure"]
    assert failure["code"] == "not_found"
    assert failure["retry"] == "never"
    assert failure["correlation_id"] == response.headers["X-Correlation-ID"]
    # I-32: the path the caller asked for is not reflected back.
    assert "does-not-exist" not in response.text
    assert "detail" not in failure


@pytest.mark.anyio
async def test_a_wrong_method_answers_invalid_request_with_405(
    tmp_path: Path,
) -> None:
    """I-70: a known ``/v1`` path with a wrong method is
    ``invalid_request`` with 405, in the shape Task 4 fixed for it, and
    carries the ``Allow`` header RFC 9110 §10.2.1 mandates — previously
    dropped, restored by the post-acceptance review of 8 August 2026.
    """
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/ingest")

    assert response.status_code == 405
    failure = response.json()["failure"]
    assert failure["code"] == "invalid_request"
    assert failure["detail"] == {
        "field_path": "method",
        "rule": "method_not_allowed",
    }
    assert failure["correlation_id"] == response.headers["X-Correlation-ID"]
    assert "POST" in response.headers["Allow"]


@pytest.mark.anyio
async def test_the_v1_failure_contract_stops_at_the_v1_boundary(
    tmp_path: Path,
) -> None:
    """The boundary handlers are scoped to ``/v1`` (post-acceptance
    review, 8 August 2026): the foundation surface — health probes,
    ``/metrics``, and any path outside ``/v1`` — answers with the
    framework default, not the I-26 envelope its consumers never
    promised to parse. The 405 still carries ``Allow``.
    """
    application = build_application(make_config(tmp_path))

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            wrong_method = await client.post("/metrics")
            unknown_path = await client.get("/does-not-exist")

    assert wrong_method.status_code == 405
    assert "failure" not in wrong_method.json()
    assert "GET" in wrong_method.headers["Allow"]
    assert unknown_path.status_code == 404
    assert "failure" not in unknown_path.json()


def test_the_internal_error_message_is_one_message() -> None:
    """The foundation catch-all and the ``/v1`` adapters say the same
    thing when they caught something they cannot describe.

    ``authority/gate.py`` owns the safe-message surface, but the
    middleware cannot import it: the dependency runs ``/v1`` → foundation
    and never back, so the middleware spells its own body out and this
    holds the two equal. Slice 7's MCP adapter reads the constant for its
    ``internal_error`` tool result, which is how the text acquired a
    second reader and this test acquired a reason to exist.
    """
    assert INTERNAL_ERROR_MESSAGE == "The request could not be completed."
