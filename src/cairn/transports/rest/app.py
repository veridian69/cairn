from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from cairn.operations.health import render_live, render_ready, render_startup
from cairn.operations.metrics import Metrics
from cairn.runtime.logging import Operation
from cairn.runtime.status import RuntimeStatus


def register_foundation_routes(
    application: FastAPI,
    *,
    status: RuntimeStatus,
    metrics: Metrics,
) -> None:
    @application.get("/health/live", name=Operation.HEALTH_LIVE.value)
    async def health_live() -> Response:
        status_code, body = render_live(await status.snapshot())
        return JSONResponse(body, status_code=status_code)

    @application.get("/health/startup", name=Operation.HEALTH_STARTUP.value)
    async def health_startup() -> Response:
        status_code, body = render_startup(await status.snapshot())
        return JSONResponse(body, status_code=status_code)

    @application.get("/health/ready", name=Operation.HEALTH_READY.value)
    async def health_ready() -> Response:
        status_code, body = render_ready(await status.snapshot())
        return JSONResponse(body, status_code=status_code)

    @application.get("/metrics", name=Operation.METRICS.value)
    async def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(body, headers={"Content-Type": content_type})
