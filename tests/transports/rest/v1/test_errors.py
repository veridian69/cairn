import json
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from cairn.authority.gate import INVALID_REQUEST_MESSAGE, SECRET_REJECTED_MESSAGE
from cairn.catalogue.transactions import (
    FailureCode,
    FailureDetail,
    RetryClass,
    StableFailure,
)
from cairn.transports.rest.v1.errors import (
    RETRY_AFTER_SECONDS,
    STATUS_BY_FAILURE_CODE,
    failure_response,
    register_boundary_handlers,
    wire_rejection_response,
)
from cairn.transports.v1.parsing import WireRejection, validation_rejection
from cairn.transports.v1.wire import (
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_VALUE,
    RULE_METHOD_NOT_ALLOWED,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
    WireModel,
)

CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def body_of(response: JSONResponse) -> dict[str, object]:
    parsed = json.loads(bytes(response.body))
    assert type(parsed) is dict
    return parsed


def make_failure(
    code: FailureCode,
    *,
    detail: FailureDetail | None = None,
) -> StableFailure:
    return StableFailure(
        code=code,
        safe_message="A safe message.",
        correlation_id=CORRELATION_ID,
        retry=RetryClass.NEVER,
        detail=detail,
    )


def test_the_status_table_is_total_over_the_eleven_codes() -> None:
    assert set(STATUS_BY_FAILURE_CODE) == set(FailureCode)


def test_the_status_table_is_exactly_the_i73_table() -> None:
    assert STATUS_BY_FAILURE_CODE == {
        FailureCode.INVALID_REQUEST: 400,
        FailureCode.AUTHENTICATION_FAILED: 401,
        FailureCode.AUTHORISATION_DENIED: 403,
        FailureCode.SECRET_REJECTED: 400,
        FailureCode.NOT_FOUND: 404,
        FailureCode.IDEMPOTENCY_CONFLICT: 409,
        FailureCode.EVIDENCE_PENDING: 503,
        FailureCode.EVIDENCE_CORRUPT: 500,
        FailureCode.INDEX_PENDING: 503,
        FailureCode.STALE_INDEX: 503,
        FailureCode.DEPENDENCY_UNAVAILABLE: 503,
        FailureCode.INSTANCE_MISMATCH: 421,
        FailureCode.INTERNAL_ERROR: 500,
    }


@pytest.mark.parametrize("code", list(FailureCode))
def test_every_failure_renders_its_fixed_status_and_stable_body(
    code: FailureCode,
) -> None:
    response = failure_response(make_failure(code))
    assert response.status_code == STATUS_BY_FAILURE_CODE[code]
    body = body_of(response)
    assert body == {
        "failure": {
            "code": code.value,
            "message": "A safe message.",
            "retry": "never",
            "correlation_id": str(CORRELATION_ID),
        }
    }


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(FailureCode.INDEX_PENDING, id="index-pending"),
        pytest.param(FailureCode.STALE_INDEX, id="stale-index"),
        pytest.param(FailureCode.DEPENDENCY_UNAVAILABLE, id="dependency-unavailable"),
    ],
)
def test_every_503_carries_retry_after(code: FailureCode) -> None:
    response = failure_response(make_failure(code))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(code, id=code.value)
        for code in FailureCode
        if STATUS_BY_FAILURE_CODE[code] != 503
    ],
)
def test_no_other_failure_carries_retry_after(code: FailureCode) -> None:
    assert "Retry-After" not in failure_response(make_failure(code)).headers


def test_authentication_failed_carries_the_bearer_challenge() -> None:
    response = failure_response(make_failure(FailureCode.AUTHENTICATION_FAILED))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert (
        "WWW-Authenticate"
        not in failure_response(make_failure(FailureCode.AUTHORISATION_DENIED)).headers
    )


def test_a_secret_rejection_renders_the_three_field_detail() -> None:
    detail = FailureDetail(
        policy="cairn.secret/v1",
        rule="cairn.secret/v1/pem-block",
        field_path="facts[0].body",
    )
    failure = StableFailure(
        code=FailureCode.SECRET_REJECTED,
        safe_message=SECRET_REJECTED_MESSAGE,
        correlation_id=CORRELATION_ID,
        retry=RetryClass.NEVER,
        detail=detail,
    )
    body = body_of(failure_response(failure))
    failure_body = body["failure"]
    assert type(failure_body) is dict
    assert failure_body["detail"] == {
        "policy": "cairn.secret/v1",
        "rule": "cairn.secret/v1/pem-block",
        "field_path": "facts[0].body",
    }


def test_an_invalid_request_detail_omits_the_policy_field() -> None:
    detail = FailureDetail(policy="", rule="some_rule", field_path="metadata")
    failure = make_failure(FailureCode.INVALID_REQUEST, detail=detail)
    body = body_of(failure_response(failure))
    failure_body = body["failure"]
    assert type(failure_body) is dict
    assert failure_body["detail"] == {
        "field_path": "metadata",
        "rule": "some_rule",
    }


def test_a_detail_on_an_unlicensed_code_is_not_rendered() -> None:
    detail = FailureDetail(policy="p", rule="r", field_path="f")
    body = body_of(failure_response(make_failure(FailureCode.NOT_FOUND, detail=detail)))
    failure_body = body["failure"]
    assert type(failure_body) is dict
    assert "detail" not in failure_body


@pytest.mark.parametrize(
    ("status", "rule", "field_path"),
    [
        pytest.param(413, RULE_BODY_TOO_LARGE, "body", id="cap"),
        pytest.param(415, RULE_INVALID_CONTENT_TYPE, "Content-Type", id="media-type"),
        pytest.param(405, RULE_METHOD_NOT_ALLOWED, "method", id="method"),
        pytest.param(400, RULE_DUPLICATE_JSON_KEY, "scope", id="duplicate-key"),
    ],
)
def test_a_wire_rejection_keeps_its_refined_status_and_names_its_rule(
    status: int,
    rule: str,
    field_path: str,
) -> None:
    response = wire_rejection_response(
        WireRejection(status, rule, field_path),
        CORRELATION_ID,
    )
    assert response.status_code == status
    assert body_of(response) == {
        "failure": {
            "code": "invalid_request",
            "message": INVALID_REQUEST_MESSAGE,
            "retry": "never",
            "correlation_id": str(CORRELATION_ID),
            "detail": {"field_path": field_path, "rule": rule},
        }
    }


class _ProbeFact(WireModel):
    body: str


class _ProbeRequest(WireModel):
    scope: str
    facts: list[_ProbeFact]


def validation_error_for(payload: dict[str, object]) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _ProbeRequest.model_validate(payload)
    return caught.value


def test_an_unknown_field_maps_to_the_unknown_field_rule() -> None:
    error = validation_error_for(
        {"scope": "s", "facts": [], "surplus": 1},
    )
    rejection = validation_rejection(error)
    assert rejection.status == 400
    assert rejection.rule == RULE_UNKNOWN_FIELD
    assert rejection.field_path == "surplus"


def test_a_missing_field_maps_to_the_missing_field_rule() -> None:
    rejection = validation_rejection(validation_error_for({"scope": "s"}))
    assert rejection.rule == RULE_MISSING_FIELD
    assert rejection.field_path == "facts"


def test_a_type_mismatch_maps_to_invalid_value_with_an_indexed_path() -> None:
    error = validation_error_for({"scope": "s", "facts": [{"body": 42}]})
    rejection = validation_rejection(error)
    assert rejection.rule == RULE_INVALID_VALUE
    assert rejection.field_path == "facts[0].body"


def test_a_model_level_error_with_an_empty_loc_falls_back_to_body() -> None:
    with pytest.raises(ValidationError) as caught:
        _ProbeRequest.model_validate(42)
    rejection = validation_rejection(caught.value)
    assert rejection.rule == RULE_INVALID_VALUE
    assert rejection.field_path == "body"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(RuntimeError("not http"), id="not-an-http-exception"),
        pytest.param(HTTPException(status_code=405), id="no-headers"),
        pytest.param(
            HTTPException(status_code=405, headers={"X-Other": "1"}),
            id="headers-without-allow",
        ),
    ],
)
async def test_the_405_handler_tolerates_an_exception_without_allow(
    exception: Exception,
) -> None:
    """The ``Allow`` preservation copies only what Starlette supplied: a
    405 raised without an ``HTTPException``, without headers, or without
    ``Allow`` still renders the envelope, with no invented header."""
    application = FastAPI()
    register_boundary_handlers(application)
    handler = application.exception_handlers[405]
    scope: dict[str, object] = {
        "type": "http",
        "method": "PUT",
        "path": "/v1/ingest",
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    request.state.correlation_id = CORRELATION_ID
    outcome = handler(request, exception)
    assert not isinstance(outcome, Response)
    response = await outcome
    assert isinstance(response, JSONResponse)
    assert response.status_code == 405
    assert "Allow" not in response.headers
    failure = body_of(response)["failure"]
    assert isinstance(failure, dict)
    assert failure["detail"] == {
        "field_path": "method",
        "rule": "method_not_allowed",
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
