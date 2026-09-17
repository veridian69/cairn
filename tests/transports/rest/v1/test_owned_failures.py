"""The owned-5xx passage: I-73's 503/421/500 bodies must survive the
foundation middleware, which otherwise replaces any unapproved 5xx with
the generic internal failure. `failure_response(..., request=...)` stamps
server-side scope state; the middleware passes a marked 5xx and still
replaces an unmarked one (Operator, 7 August 2026)."""

from io import StringIO
from uuid import UUID

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cairn.catalogue.transactions import FailureCode, RetryClass, StableFailure
from cairn.operations.metrics import Metrics
from cairn.runtime.logging import configure_logging
from cairn.transports.rest.middleware import FoundationMiddleware
from cairn.transports.rest.v1.errors import failure_response

CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_application() -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        FoundationMiddleware,
        logger=configure_logging(StringIO()),
        metrics=Metrics(),
    )

    @application.post("/owned")
    async def owned(request: Request) -> Response:
        failure = StableFailure(
            code=FailureCode.DEPENDENCY_UNAVAILABLE,
            safe_message="A dependency is unavailable.",
            correlation_id=CORRELATION_ID,
            retry=RetryClass.AFTER_DELAY,
        )
        return failure_response(failure, request=request)

    @application.post("/unowned")
    async def unowned(request: Request) -> Response:
        return JSONResponse({"raw": "unapproved"}, status_code=503)

    return application


async def post(application: FastAPI, path: str) -> tuple[int, dict[str, object], str]:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    return (
        response.status_code,
        response.json(),
        response.headers.get("Retry-After", ""),
    )


@pytest.mark.anyio
async def test_a_marked_503_passes_the_middleware_with_retry_after() -> None:
    status, body, retry_after = await post(make_application(), "/owned")

    assert status == 503
    assert retry_after == "1"
    failure = body["failure"]
    assert type(failure) is dict
    assert failure["code"] == "dependency_unavailable"
    assert failure["retry"] == "after-delay"


@pytest.mark.anyio
async def test_an_unmarked_5xx_is_still_replaced_by_the_generic_failure() -> None:
    status, body, _ = await post(make_application(), "/unowned")

    assert status == 500
    failure = body["failure"]
    assert type(failure) is dict
    assert failure["code"] == "internal_error"
    assert "raw" not in body
