"""REST and MCP renderers for the recall-page/evidence-window failure packet.

Legacy FailureBody/StableFailure.detail stay closed; these operations carry
their own detail (models.PageFailureEnvelope), rendered identically here on
both transports.
"""

from mcp.types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import JSONResponse

from cairn.authority.memory_page_types import PageBudgetTooSmall, PageRejected
from cairn.transports.mcp.server import _canonical_json
from cairn.transports.memory.models import (
    ContinuationDetailBody,
    PageBudgetDetailBody,
    PageFailureBody,
    PageFailureEnvelope,
)
from cairn.transports.rest.middleware import OWNED_FAILURE_STATE_KEY
from cairn.transports.rest.v1.errors import (
    _RETRY_AFTER_CODES,
    RETRY_AFTER_SECONDS,
    STATUS_BY_FAILURE_CODE,
)


def page_failure_envelope(value: PageRejected) -> PageFailureEnvelope:
    detail = value.detail
    return PageFailureEnvelope(
        failure=PageFailureBody(
            code=value.failure.code.value,
            message=value.failure.safe_message,
            retry=value.failure.retry.value,
            correlation_id=str(value.failure.correlation_id),
            detail=PageBudgetDetailBody(
                reason="page_budget_too_small",
                minimum_budget=detail.minimum_budget,
            )
            if isinstance(detail, PageBudgetTooSmall)
            else ContinuationDetailBody(reason="continuation_unavailable"),
        )
    )


def page_failure_response(value: PageRejected, *, request: Request) -> JSONResponse:
    request.scope.setdefault("state", {})[OWNED_FAILURE_STATE_KEY] = True
    headers = (
        {"Retry-After": str(RETRY_AFTER_SECONDS)}
        if value.failure.code in _RETRY_AFTER_CODES
        else {}
    )
    return JSONResponse(
        page_failure_envelope(value).model_dump(mode="json", exclude_none=True),
        status_code=STATUS_BY_FAILURE_CODE[value.failure.code],
        headers=headers,
    )


def page_failure_result(value: PageRejected) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=_canonical_json(
                    page_failure_envelope(value).model_dump(
                        mode="json", exclude_none=True
                    )
                ),
            )
        ],
        structuredContent=None,
        isError=True,
    )
