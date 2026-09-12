import asyncio
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager

import cairn.runtime.composition as composition
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import CATALOGUE_FILENAME, CURRENT_SCHEMA_VERSION
from cairn.catalogue.verification import (
    VerificationError,
    VerificationReport,
    _verify_catalogue_locked,
)
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.lease import DataDirectoryLease

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _config(data_path: Path, instance_id: UUID = INSTANCE_ID) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=instance_id,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _set_pragma(data_path: Path, pragma: str, value: int) -> None:
    connection = sqlite3.connect(data_path / CATALOGUE_FILENAME)
    connection.execute(f"PRAGMA {pragma} = {value}")
    connection.close()


def test_exact_catalogue_becomes_ready_and_keeps_verified_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    application = composition.build_application(config)

    async def exercise() -> tuple[int, dict[str, str], VerificationReport, bool]:
        async with LifespanManager(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get("/health/ready")
            state = application.state.catalogue_state
            return response.status_code, response.json(), state.report, state.closed

    status_code, body, report, closed = asyncio.run(exercise())

    assert status_code == 200
    assert body == {"status": "ready"}
    assert report.instance_id == INSTANCE_ID
    assert not closed
    assert application.state.catalogue_state.closed


def test_catalogue_verification_runs_outside_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    loop_thread = threading.get_ident()
    verification_threads: list[int] = []
    real_verify = _verify_catalogue_locked

    def observe(candidate: CairnConfig) -> VerificationReport:
        verification_threads.append(threading.get_ident())
        return real_verify(candidate)

    monkeypatch.setattr(composition, "_verify_catalogue_locked", observe)
    application = composition.build_application(config)

    async def exercise() -> None:
        async with LifespanManager(application):
            pass

    asyncio.run(exercise())

    assert verification_threads
    assert verification_threads[0] != loop_thread


def test_absent_catalogue_fails_startup_and_releases_lease(tmp_path: Path) -> None:
    config = _config(tmp_path)
    application = composition.build_application(config)

    async def exercise() -> None:
        with pytest.raises(VerificationError) as caught:
            async with LifespanManager(application):
                pass
        assert caught.value.code == "catalogue_unavailable"

    asyncio.run(exercise())
    replacement = DataDirectoryLease(tmp_path, INSTANCE_ID)
    replacement.acquire()
    replacement.release()


@pytest.mark.parametrize(
    ("name", "tamper", "instance_id", "code"),
    [
        (
            "behind",
            lambda path: _set_pragma(path, "user_version", 0),
            INSTANCE_ID,
            "schema_version_mismatch",
        ),
        (
            "ahead",
            lambda path: _set_pragma(path, "user_version", CURRENT_SCHEMA_VERSION + 1),
            INSTANCE_ID,
            "schema_version_mismatch",
        ),
        (
            "corrupt",
            lambda path: _set_pragma(path, "application_id", 7),
            INSTANCE_ID,
            "application_id_mismatch",
        ),
        (
            "mismatch",
            lambda _path: None,
            UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            "instance_mismatch",
        ),
    ],
)
def test_non_exact_catalogue_never_starts(
    tmp_path: Path,
    name: str,
    tamper: Callable[[Path], None],
    instance_id: UUID,
    code: str,
) -> None:
    data_path = tmp_path / name
    data_path.mkdir()
    canonical = _config(data_path)
    migrate_catalogue(canonical, lambda: NOW)
    tamper(data_path)
    application = composition.build_application(_config(data_path, instance_id))

    async def exercise() -> None:
        with pytest.raises(VerificationError) as caught:
            async with LifespanManager(application):
                pass
        assert caught.value.code == code

    asyncio.run(exercise())


def test_shutdown_closes_catalogue_state_before_releasing_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    events: list[str] = []
    application = composition.build_application(config)

    class ObservedLease:
        def acquire(self) -> None:
            events.append("lease-acquired")

        def release(self) -> None:
            assert application.state.catalogue_state.closed
            events.append("lease-released")

    monkeypatch.setattr(
        composition,
        "DataDirectoryLease",
        lambda _path, _instance_id: ObservedLease(),
    )
    application = composition.build_application(config)

    async def exercise() -> None:
        async with LifespanManager(application):
            events.append("running")

    asyncio.run(exercise())

    assert events == ["lease-acquired", "running", "lease-released"]
