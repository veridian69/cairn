import hashlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

from cairn.authority.credentials import CredentialAuthenticator, mint_token
from cairn.authority.gate import Actor
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions, Rejected
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.screening import SecretScreen
from cairn.transports.rest.v1.errors import failure_response
from cairn.transports.v1.auth import authenticate_request, screen_addressing
from cairn.transports.v1.parsing import admit_body

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")
CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
UNKNOWN_CREDENTIAL_ID = UUID("44444444-4444-4444-8444-444444444444")
CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACTOR = Actor(principal_id=PRINCIPAL_ID, credential_id=CREDENTIAL_ID)
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
# The canonical AWS documentation example key — a synthetic positive that
# trips cairn.secret/v1/upstream/AWSKeyDetector (verified against the real
# screen before this file was written), never a real credential.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_RULE = "cairn.secret/v1/upstream/AWSKeyDetector"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def seed_credential(data_path: Path, *, expires_at: str | None = None) -> str:
    """Migrates a fresh catalogue and seeds one credential the fast way —
    direct rows, the ``test_credentials.py`` recipe — returning the bearer
    token text. Expiry is set at insert because credential rows are
    immutable once written."""
    migrate_catalogue(make_config(data_path), lambda: NOW)
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", TS),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, TS, expires_at),
        )
        connection.commit()
    return minted.text


def revoke_credential(data_path: Path) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credential_revocations "
            "(credential_id, revoked_at, revoked_by, reason_code) "
            "VALUES (?, ?, ?, ?)",
            (str(CREDENTIAL_ID), TS, str(PRINCIPAL_ID), "superseded"),
        )
        connection.commit()


def seed_expired_credential(data_path: Path) -> str:
    return seed_credential(
        data_path,
        expires_at=canonical_timestamp(datetime(2026, 8, 6, tzinfo=UTC)),
    )


def seed_revoked_credential(data_path: Path) -> str:
    token = seed_credential(data_path)
    revoke_credential(data_path)
    return token


def _uuid_seq(start: int) -> Callable[[], UUID]:
    counter = itertools.count(start)

    def factory() -> UUID:
        return UUID(f"{next(counter):08x}-0000-4000-8000-000000000000")

    return factory


def make_transactions(data_path: Path) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=lambda: NOW,
        uuid_factory=_uuid_seq(0x40000000),
    )


def instance_events(data_path: Path) -> list[dict[str, object]]:
    with (
        closing(sqlite3.connect(data_path / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events "
            "WHERE chain_kind = 'instance' ORDER BY sequence"
        ).fetchall()
    parsed: list[dict[str, object]] = []
    for row in rows:
        document = json.loads(row[0])
        assert type(document) is dict
        parsed.append(document)
    return parsed


def make_stub_application(
    data_path: Path,
) -> tuple[FastAPI, list[dict[str, object]]]:
    """A stub route wired exactly as a real mutation route will be: the
    P-27 sequence — authenticate, admit, screen addressing — in front of a
    recording list standing in for the application pipeline."""
    authenticator = CredentialAuthenticator(data_path, lambda: NOW)
    transactions = make_transactions(data_path)
    screen = SecretScreen()
    application_calls: list[dict[str, object]] = []
    application = FastAPI()

    @application.post("/stub")
    async def stub(request: Request) -> Response:
        outcome = authenticate_request(
            request.headers,
            authenticator=authenticator,
            transactions=transactions,
            data_path=data_path,
            action_code="ingest",
            action_kind=ActionKind.DATA,
            correlation_id=CORRELATION_ID,
        )
        if isinstance(outcome, Rejected):
            return failure_response(outcome.failure)
        body = await admit_body(request)
        rejected = screen_addressing(
            body,
            screen=screen,
            actor=outcome,
            transactions=transactions,
            data_path=data_path,
            action_code="ingest",
            action_kind=ActionKind.DATA,
            correlation_id=CORRELATION_ID,
        )
        if rejected is not None:
            return failure_response(rejected.failure)
        application_calls.append(body)
        return JSONResponse({"ok": True})

    return application, application_calls


async def post_stub(
    application: FastAPI,
    *,
    body: dict[str, object],
    headers: dict[str, str],
) -> HTTPXResponse:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.post("/stub", json=body, headers=headers)


CLEAN_BODY: dict[str, object] = {
    "scope": {
        "realm": "acme",
        "segments": [{"kind": "repository", "identifier": "acme-repo"}],
    }
}


@pytest.mark.anyio
async def test_a_valid_bearer_token_reaches_the_application(tmp_path: Path) -> None:
    token = seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)

    response = await post_stub(
        application,
        body=CLEAN_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert calls == [CLEAN_BODY]
    assert instance_events(tmp_path) == []


@pytest.mark.anyio
async def test_an_absent_credential_is_denied_with_a_durable_event(
    tmp_path: Path,
) -> None:
    """`AUTH-02` at the HTTP layer."""
    seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)

    response = await post_stub(application, body=CLEAN_BODY, headers={})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    failure = response.json()["failure"]
    assert failure["code"] == "authentication_failed"
    assert "detail" not in failure
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == "authorization_header_missing"
    assert event["principal_id"] is None
    assert event["credential_verifier_id"] is None
    assert event["safe_request_fingerprint"] is None
    assert event["outcome"] == "deny"
    assert event["correlation_id"] == str(CORRELATION_ID)


@pytest.mark.anyio
async def test_bad_credentials_are_publicly_indistinguishable(
    tmp_path: Path,
) -> None:
    """`AUTH-03` at the HTTP layer: malformed token, unknown credential and
    wrong secret answer with byte-identical public bodies; the precise
    reasons exist only on the private instance chain."""
    token = seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)
    unknown = token.replace(str(CREDENTIAL_ID), str(UNKNOWN_CREDENTIAL_ID))
    wrong_secret = mint_token(CREDENTIAL_ID, lambda count: bytes(count)).text

    bodies = []
    for presented in ("garbage", unknown, wrong_secret):
        response = await post_stub(
            application,
            body=CLEAN_BODY,
            headers={"Authorization": f"Bearer {presented}"},
        )
        assert response.status_code == 401
        bodies.append(response.json())

    assert bodies[0] == bodies[1] == bodies[2]
    assert calls == []
    events = instance_events(tmp_path)
    assert [event["reason_code"] for event in events] == [
        "malformed_token",
        "unknown_credential",
        "verifier_mismatch",
    ]
    for event, presented in zip(
        events, ("garbage", unknown, wrong_secret), strict=True
    ):
        expected = hashlib.sha256(f"Bearer {presented}".encode()).hexdigest()
        assert event["safe_request_fingerprint"] == expected
        assert event["principal_id"] is None
        assert event["credential_verifier_id"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("seed", "reason_code"),
    [
        pytest.param(seed_expired_credential, "credential_expired", id="expired"),
        pytest.param(seed_revoked_credential, "credential_is_revoked", id="revoked"),
    ],
)
async def test_expired_and_revoked_credentials_are_denied_coarsely(
    tmp_path: Path,
    seed: Callable[[Path], str],
    reason_code: str,
) -> None:
    token = seed(tmp_path)
    application, calls = make_stub_application(tmp_path)

    response = await post_stub(
        application,
        body=CLEAN_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["failure"]["code"] == "authentication_failed"
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == reason_code


@pytest.mark.anyio
async def test_a_duplicated_authorization_header_is_denied(tmp_path: Path) -> None:
    token = seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/stub",
            json=CLEAN_BODY,
            headers=[
                ("Authorization", f"Bearer {token}"),
                ("Authorization", f"Bearer {token}"),
            ],
        )

    assert response.status_code == 401
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == "authorization_header_duplicated"
    joined = f"Bearer {token}\nBearer {token}"
    expected = hashlib.sha256(joined.encode()).hexdigest()
    assert event["safe_request_fingerprint"] == expected


@pytest.mark.anyio
async def test_a_non_bearer_scheme_is_denied(tmp_path: Path) -> None:
    seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)

    response = await post_stub(
        application,
        body=CLEAN_BODY,
        headers={"Authorization": "Basic am9uOmh1bnRlcjI="},
    )

    assert response.status_code == 401
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == "authorization_scheme_unsupported"


@pytest.mark.anyio
async def test_every_denial_reason_is_publicly_identical(tmp_path: Path) -> None:
    """The full `AUTH-03` surface in one place: all eight denial reasons —
    three extraction failures and five authenticator reasons — answer with
    byte-identical public bodies, statuses and challenge headers, each from
    a fresh catalogue."""

    def case_missing(path: Path) -> list[tuple[str, str]]:
        seed_credential(path)
        return []

    def case_duplicated(path: Path) -> list[tuple[str, str]]:
        pair = ("Authorization", f"Bearer {seed_credential(path)}")
        return [pair, pair]

    def case_scheme(path: Path) -> list[tuple[str, str]]:
        seed_credential(path)
        return [("Authorization", "Basic am9uOmh1bnRlcjI=")]

    def case_malformed(path: Path) -> list[tuple[str, str]]:
        seed_credential(path)
        return [("Authorization", "Bearer garbage")]

    def case_unknown(path: Path) -> list[tuple[str, str]]:
        token = seed_credential(path)
        unknown = token.replace(str(CREDENTIAL_ID), str(UNKNOWN_CREDENTIAL_ID))
        return [("Authorization", f"Bearer {unknown}")]

    def case_mismatch(path: Path) -> list[tuple[str, str]]:
        seed_credential(path)
        wrong = mint_token(CREDENTIAL_ID, lambda count: bytes(count)).text
        return [("Authorization", f"Bearer {wrong}")]

    def case_expired(path: Path) -> list[tuple[str, str]]:
        return [("Authorization", f"Bearer {seed_expired_credential(path)}")]

    def case_revoked(path: Path) -> list[tuple[str, str]]:
        return [("Authorization", f"Bearer {seed_revoked_credential(path)}")]

    cases = (
        case_missing,
        case_duplicated,
        case_scheme,
        case_malformed,
        case_unknown,
        case_mismatch,
        case_expired,
        case_revoked,
    )
    bodies: list[dict[str, object]] = []
    for index, prepare in enumerate(cases):
        path = tmp_path / str(index)
        path.mkdir()
        headers = prepare(path)
        application, calls = make_stub_application(path)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post("/stub", json=CLEAN_BODY, headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert calls == []
        bodies.append(response.json())

    assert all(body == bodies[0] for body in bodies[1:])


@pytest.mark.anyio
async def test_a_secret_bearing_scope_identifier_is_rejected_durably(
    tmp_path: Path,
) -> None:
    """P-27's headline proof: the denial event carries a fingerprint and no
    scope fields, is durable before the response, and the secret text never
    reaches the catalogue bytes."""
    token = seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)
    body: dict[str, object] = {
        "scope": {
            "realm": "acme",
            "segments": [{"kind": "repository", "identifier": AWS_EXAMPLE_KEY}],
        }
    }

    response = await post_stub(
        application,
        body=body,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    failure = response.json()["failure"]
    assert failure["code"] == "secret_rejected"
    assert failure["detail"] == {
        "policy": "cairn.secret/v1",
        "rule": AWS_RULE,
        "field_path": "scope.segments[0].identifier",
    }
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == "secret_upstream_awskeydetector"
    assert event["requested_scope"] is None
    assert event["source_scope"] is None
    assert event["target_scope"] is None
    assert event["principal_id"] == str(PRINCIPAL_ID)
    assert event["credential_verifier_id"] == str(CREDENTIAL_ID)
    expected = hashlib.sha256(AWS_EXAMPLE_KEY.encode()).hexdigest()
    assert event["safe_request_fingerprint"] == expected
    catalogue_bytes = (tmp_path / CATALOGUE_FILENAME).read_bytes()
    assert AWS_EXAMPLE_KEY.encode() not in catalogue_bytes


@pytest.mark.anyio
async def test_an_unauthenticated_secret_bearing_request_gets_no_verdict(
    tmp_path: Path,
) -> None:
    """P-27's order: authentication first, so an unauthenticated caller
    receives ``authentication_failed`` with no screening verdict."""
    seed_credential(tmp_path)
    application, calls = make_stub_application(tmp_path)
    body: dict[str, object] = {
        "scope": {
            "realm": "acme",
            "segments": [{"kind": "repository", "identifier": AWS_EXAMPLE_KEY}],
        }
    }

    response = await post_stub(application, body=body, headers={})

    assert response.status_code == 401
    failure = response.json()["failure"]
    assert failure["code"] == "authentication_failed"
    assert "detail" not in failure
    assert calls == []
    (event,) = instance_events(tmp_path)
    assert event["reason_code"] == "authorization_header_missing"


@pytest.mark.parametrize(
    ("body", "field_path"),
    [
        pytest.param({"realm_id": AWS_EXAMPLE_KEY}, "realm_id", id="realm-id"),
        pytest.param(
            {"scope": {"realm": AWS_EXAMPLE_KEY, "segments": []}},
            "scope.realm",
            id="scope-realm",
        ),
        pytest.param(
            {"scope": {"realm": "acme", "segments": [{"kind": AWS_EXAMPLE_KEY}]}},
            "scope.segments[0].kind",
            id="segment-kind",
        ),
        pytest.param(
            {
                "target_scope": {
                    "segments": [{"kind": "r", "identifier": AWS_EXAMPLE_KEY}]
                }
            },
            "target_scope.segments[0].identifier",
            id="target-scope-segment",
        ),
        pytest.param(
            {"grant": {"realm_id": AWS_EXAMPLE_KEY}},
            "grant.realm_id",
            id="grant-realm",
        ),
        pytest.param(
            {"grant": {"segments": [{"identifier": AWS_EXAMPLE_KEY}]}},
            "grant.segments[0].identifier",
            id="grant-segment",
        ),
        pytest.param(
            {"scope_prefix": [{"kind": AWS_EXAMPLE_KEY}]},
            "scope_prefix[0].kind",
            id="scope-prefix",
        ),
    ],
)
def test_every_addressing_location_is_screened(
    tmp_path: Path,
    body: dict[str, object],
    field_path: str,
) -> None:
    seed_credential(tmp_path)
    transactions = make_transactions(tmp_path)

    rejected = screen_addressing(
        body,
        screen=SecretScreen(),
        actor=ACTOR,
        transactions=transactions,
        data_path=tmp_path,
        action_code="ingest",
        action_kind=ActionKind.DATA,
        correlation_id=CORRELATION_ID,
    )

    assert rejected is not None
    assert rejected.failure.detail is not None
    assert rejected.failure.detail.field_path == field_path
    assert rejected.failure.detail.rule == AWS_RULE


def test_the_first_hostile_field_in_documented_order_is_reported(
    tmp_path: Path,
) -> None:
    """Two hostile fields: the walk's fixed order decides which the detail
    names, and only one denial event is appended — the docstring's
    first-dirty-field claim, pinned."""
    seed_credential(tmp_path)
    body: dict[str, object] = {
        "realm_id": AWS_EXAMPLE_KEY,
        "scope": {
            "realm": "acme",
            "segments": [{"kind": "repository", "identifier": AWS_EXAMPLE_KEY}],
        },
    }

    rejected = screen_addressing(
        body,
        screen=SecretScreen(),
        actor=ACTOR,
        transactions=make_transactions(tmp_path),
        data_path=tmp_path,
        action_code="ingest",
        action_kind=ActionKind.DATA,
        correlation_id=CORRELATION_ID,
    )

    assert rejected is not None
    assert rejected.failure.detail is not None
    assert rejected.failure.detail.field_path == "realm_id"
    assert len(instance_events(tmp_path)) == 1


def test_mis_shaped_addressing_is_left_to_model_validation(tmp_path: Path) -> None:
    """A non-dict segment or non-list segments carries no screenable text;
    the screen skips it and model validation refuses it downstream."""
    seed_credential(tmp_path)
    body: dict[str, object] = {
        "scope": {"realm": "acme", "segments": ["not-a-segment", 7]},
        "target_scope": {"segments": "not-a-list"},
    }

    rejected = screen_addressing(
        body,
        screen=SecretScreen(),
        actor=ACTOR,
        transactions=make_transactions(tmp_path),
        data_path=tmp_path,
        action_code="ingest",
        action_kind=ActionKind.DATA,
        correlation_id=CORRELATION_ID,
    )

    assert rejected is None
    assert instance_events(tmp_path) == []


def test_clean_addressing_passes_without_an_event(tmp_path: Path) -> None:
    seed_credential(tmp_path)

    rejected = screen_addressing(
        dict(CLEAN_BODY),
        screen=SecretScreen(),
        actor=ACTOR,
        transactions=make_transactions(tmp_path),
        data_path=tmp_path,
        action_code="ingest",
        action_kind=ActionKind.DATA,
        correlation_id=CORRELATION_ID,
    )

    assert rejected is None
    assert instance_events(tmp_path) == []
