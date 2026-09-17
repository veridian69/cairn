"""The Task 4 fail-before-custody proof.

A test-only stub route runs the exact admission sequence a real ``/v1``
mutation route will run — ``admit_body``, ``require_idempotency_key``,
strict model validation — and records every call that reaches the
"application". Each rejection case then asserts two things: the refined
status and rule on the wire, and that the application was never invoked,
which is the obligation I-71 states for unknown fields, duplicate keys,
oversize bodies and malformed idempotency keys.
"""

from uuid import UUID

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse
from pydantic import ValidationError

from cairn.transports.rest.v1.errors import wire_rejection_response
from cairn.transports.rest.v1.parsing import require_idempotency_key
from cairn.transports.v1.parsing import (
    MAX_REQUEST_BYTES,
    WireRejection,
    admit_body,
    validation_rejection,
)
from cairn.transports.v1.wire import (
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
    RULE_INVALID_CONTENT_TYPE,
    RULE_UNKNOWN_FIELD,
    WireModel,
)

CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
IDEMPOTENCY_KEY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubRequestModel(WireModel):
    """Stands in for a per-route request model; two fields suffice to
    exercise strictness."""

    scope: str
    count: int


def make_stub_application() -> tuple[FastAPI, list[dict[str, object]]]:
    """A stub route wired exactly as a real mutation route will be: the
    admission sequence in front, the recording list standing in for the
    application pipeline."""
    application_calls: list[dict[str, object]] = []
    application = FastAPI()

    @application.post("/stub")
    async def stub(request: Request) -> Response:
        try:
            body = await admit_body(request)
            require_idempotency_key(request.headers)
            try:
                _StubRequestModel.model_validate(body)
            except ValidationError as error:
                raise validation_rejection(error) from error
        except WireRejection as rejection:
            return wire_rejection_response(rejection, CORRELATION_ID)
        application_calls.append(body)
        return JSONResponse({"ok": True})

    return application, application_calls


async def post_stub(
    application: FastAPI,
    *,
    content: bytes,
    headers: dict[str, str],
) -> HTTPXResponse:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.post("/stub", content=content, headers=headers)


def json_headers(*, idempotency_key: str | None = IDEMPOTENCY_KEY) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


@pytest.mark.anyio
async def test_a_valid_request_reaches_the_application_exactly_once() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "count": 1}',
        headers=json_headers(),
    )
    assert response.status_code == 200
    assert calls == [{"scope": "s", "count": 1}]


@pytest.mark.anyio
async def test_an_unknown_field_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "count": 1, "surplus": true}',
        headers=json_headers(),
    )
    assert response.status_code == 400
    detail = response.json()["failure"]["detail"]
    assert detail == {"field_path": "surplus", "rule": RULE_UNKNOWN_FIELD}
    assert calls == []


@pytest.mark.anyio
async def test_a_duplicate_json_key_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "scope": "t", "count": 1}',
        headers=json_headers(),
    )
    assert response.status_code == 400
    detail = response.json()["failure"]["detail"]
    assert detail == {"field_path": "scope", "rule": RULE_DUPLICATE_JSON_KEY}
    assert calls == []


@pytest.mark.anyio
async def test_an_oversize_body_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    padding = b"a" * MAX_REQUEST_BYTES
    response = await post_stub(
        application,
        content=b'{"scope": "' + padding + b'", "count": 1}',
        headers=json_headers(),
    )
    assert response.status_code == 413
    detail = response.json()["failure"]["detail"]
    assert detail == {"field_path": "body", "rule": RULE_BODY_TOO_LARGE}
    assert calls == []


@pytest.mark.anyio
async def test_a_malformed_idempotency_key_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "count": 1}',
        headers=json_headers(idempotency_key="NOT-CANONICAL"),
    )
    assert response.status_code == 400
    detail = response.json()["failure"]["detail"]
    assert detail == {
        "field_path": "Idempotency-Key",
        "rule": RULE_IDEMPOTENCY_KEY_MALFORMED,
    }
    assert calls == []


@pytest.mark.anyio
async def test_a_missing_idempotency_key_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "count": 1}',
        headers=json_headers(idempotency_key=None),
    )
    assert response.status_code == 400
    detail = response.json()["failure"]["detail"]
    assert detail == {
        "field_path": "Idempotency-Key",
        "rule": RULE_IDEMPOTENCY_KEY_MISSING,
    }
    assert calls == []


@pytest.mark.anyio
async def test_a_wrong_content_type_fails_before_any_application_call() -> None:
    application, calls = make_stub_application()
    response = await post_stub(
        application,
        content=b'{"scope": "s", "count": 1}',
        headers={
            "Content-Type": "text/plain",
            "Idempotency-Key": IDEMPOTENCY_KEY,
        },
    )
    assert response.status_code == 415
    detail = response.json()["failure"]["detail"]
    assert detail == {
        "field_path": "Content-Type",
        "rule": RULE_INVALID_CONTENT_TYPE,
    }
    assert calls == []
