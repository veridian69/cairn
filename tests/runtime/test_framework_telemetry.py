"""Cairn's HTTP application never reports to an OpenTelemetry provider."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from opentelemetry import _logs, metrics, trace

import cairn.runtime.composition as composition
from cairn.catalogue.migration import migrate_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


class _RecordingProviders:
    """Stand in for an SDK another component configured in this process."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def get_tracer(self, name: str, *_: Any, **__: Any) -> trace.Tracer:
        self.requests.append("tracer")
        return trace.NoOpTracer()

    def get_meter(self, name: str, *_: Any, **__: Any) -> metrics.Meter:
        self.requests.append("meter")
        return metrics.NoOpMeter(name)

    def get_logger(self, name: str, *_: Any, **__: Any) -> _logs.Logger:
        self.requests.append("logger")
        return _logs.NoOpLogger(name)


def _configure_ambient_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingProviders:
    providers = _RecordingProviders()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: providers)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: providers)
    monkeypatch.setattr(_logs, "get_logger_provider", lambda: providers)
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    return providers


def _get(application: FastAPI, *paths: str) -> list[int]:
    async def exercise() -> list[int]:
        async with LifespanManager(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return [(await client.get(path)).status_code for path in paths]

    return asyncio.run(exercise())


def test_a_default_fastapi_application_reports_to_a_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control: without Cairn's setting, the framework uses the provider.
    providers = _configure_ambient_telemetry(monkeypatch)
    application = FastAPI()

    @application.get("/probe")
    async def probe() -> dict[str, str]:
        return {}

    assert _get(application, "/probe") == [200]
    assert providers.requests


def test_cairn_application_ignores_a_configured_provider_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = _configure_ambient_telemetry(monkeypatch)
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=tmp_path, credentials=tmp_path / "credentials"),
    )
    migrate_catalogue(config, lambda: NOW)
    application = composition.build_application(config)

    assert _get(application, "/health/ready", "/no-such-path?query=value") == [
        200,
        404,
    ]
    assert providers.requests == []


def _import_in_fresh_process(module: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "OTEL_PROPAGATORS": "tracecontext,not-installed",
        "OTEL_PYTHON_CONTEXT": "not-installed",
    }
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_framework_alone_fails_to_import_under_an_unavailable_propagator() -> None:
    # The control: the inherited variable is read while FastAPI is imported.
    result = _import_in_fresh_process("fastapi")

    assert result.returncode != 0
    assert "Propagator not-installed not found" in result.stderr


def test_cairn_imports_under_inherited_opentelemetry_configuration() -> None:
    result = _import_in_fresh_process("cairn.runtime.composition")

    assert result.returncode == 0, result.stderr
    assert "Failed to load context" not in result.stderr
    assert "Propagator" not in result.stderr
