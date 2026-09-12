import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from cairn.runtime.lease import DataDirectoryLease, LeaseError
from cairn.runtime.status import RuntimeStatus, StatusSnapshot

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
CONTENDER_ID = UUID("22222222-2222-4222-8222-222222222222")
CONTENDER = """\
import sys
from pathlib import Path
from uuid import UUID

from cairn.runtime.lease import DataDirectoryLease, LeaseError

lease = DataDirectoryLease(Path(sys.argv[1]), UUID(sys.argv[2]))
try:
    lease.acquire()
except LeaseError as error:
    assert error.__cause__ is None
    assert error.__context__ is None
    print(error.code)
    raise SystemExit(1)
else:
    print("acquired")
    lease.release()
"""


def run_contender(data_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CONTENDER, str(data_path), str(CONTENDER_ID)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_lease_rejects_missing_data_directory(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-data"
    lease = DataDirectoryLease(missing_path, INSTANCE_ID)

    with pytest.raises(LeaseError) as raised:
        lease.acquire()

    assert raised.value.code == "data_unavailable"
    assert str(missing_path) not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_lease_is_exclusive_until_release(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lease = DataDirectoryLease(data_path, INSTANCE_ID)
    lease.acquire()

    try:
        contender = run_contender(data_path)
    finally:
        lease.release()

    assert contender.returncode == 1
    assert contender.stdout == "already_locked\n"
    assert contender.stderr == ""


def test_lease_records_only_instance_identity(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lease = DataDirectoryLease(data_path, INSTANCE_ID)

    lease.acquire()
    try:
        lock_path = data_path / ".cairn-instance.lock"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o660
        assert lock_path.read_bytes() == (b"11111111-1111-4111-8111-111111111111\n")
    finally:
        lease.release()


def test_lease_truncates_existing_lock_with_portable_mode(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lock_path = data_path / ".cairn-instance.lock"
    lock_path.write_text("stale-data-that-must-not-remain", encoding="utf-8")
    lock_path.chmod(0o660)
    lease = DataDirectoryLease(data_path, INSTANCE_ID)

    lease.acquire()
    try:
        assert lock_path.read_bytes() == (b"11111111-1111-4111-8111-111111111111\n")
    finally:
        lease.release()


def test_lease_rejects_existing_lock_with_non_portable_mode(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lock_path = data_path / ".cairn-instance.lock"
    lock_path.write_text("stale", encoding="utf-8")
    lock_path.chmod(0o600)
    lease = DataDirectoryLease(data_path, INSTANCE_ID)

    with pytest.raises(LeaseError) as raised:
        lease.acquire()

    assert raised.value.code == "data_unavailable"
    assert lock_path.read_text(encoding="utf-8") == "stale"


def test_lease_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    target = tmp_path / "target"
    target.write_text("must-remain", encoding="utf-8")
    (data_path / ".cairn-instance.lock").symlink_to(target)
    lease = DataDirectoryLease(data_path, INSTANCE_ID)

    with pytest.raises(LeaseError) as raised:
        lease.acquire()

    assert raised.value.code == "data_unavailable"
    assert target.read_text(encoding="utf-8") == "must-remain"


def test_lease_rejects_non_regular_lock_target(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    os.mkfifo(data_path / ".cairn-instance.lock")
    lease = DataDirectoryLease(data_path, INSTANCE_ID)

    with pytest.raises(LeaseError) as raised:
        lease.acquire()

    assert raised.value.code == "data_unavailable"


def test_failed_initialisation_closes_and_releases_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    real_fsync = os.fsync

    def fail_fsync(file_descriptor: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(LeaseError) as raised:
        DataDirectoryLease(data_path, INSTANCE_ID).acquire()
    assert raised.value.code == "data_unavailable"

    monkeypatch.setattr(os, "fsync", real_fsync)
    replacement = DataDirectoryLease(data_path, INSTANCE_ID)
    replacement.acquire()
    replacement.release()


def test_failed_creator_mode_does_not_publish_poisoned_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    real_fchmod = os.fchmod

    def fail_fchmod(file_descriptor: int, mode: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "fchmod", fail_fchmod)
    with pytest.raises(LeaseError) as raised:
        DataDirectoryLease(data_path, INSTANCE_ID).acquire()
    assert raised.value.code == "data_unavailable"
    assert list(data_path.iterdir()) == []

    monkeypatch.setattr(os, "fchmod", real_fchmod)
    replacement = DataDirectoryLease(data_path, INSTANCE_ID)
    replacement.acquire()
    replacement.release()


def test_release_allows_reacquisition(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lease = DataDirectoryLease(data_path, INSTANCE_ID)
    lease.acquire()
    lease.release()

    contender = run_contender(data_path)

    assert contender.returncode == 0
    assert contender.stdout == "acquired\n"
    assert contender.stderr == ""


def test_release_is_idempotent(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    lease = DataDirectoryLease(data_path, INSTANCE_ID)
    lease.acquire()

    lease.release()
    lease.release()

    assert run_contender(data_path).returncode == 0


def test_async_context_owns_and_releases_lease(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()

    async def exercise() -> tuple[int, int]:
        async with DataDirectoryLease(data_path, INSTANCE_ID):
            owned = run_contender(data_path).returncode
        released = run_contender(data_path).returncode
        return owned, released

    assert asyncio.run(exercise()) == (1, 0)


def test_status_starts_not_started_and_not_ready() -> None:
    async def exercise() -> StatusSnapshot:
        return await RuntimeStatus().snapshot()

    snapshot = asyncio.run(exercise())

    assert snapshot == StatusSnapshot(
        live=True,
        started=False,
        ready=False,
        stopping=False,
    )


def test_stopping_always_clears_readiness() -> None:
    async def exercise() -> StatusSnapshot:
        status = RuntimeStatus()
        await status.mark_started()
        await status.mark_ready()
        await status.mark_stopping()
        return await status.snapshot()

    snapshot = asyncio.run(exercise())

    assert snapshot.stopping
    assert not snapshot.ready


@pytest.mark.parametrize("stopping", [False, True])
def test_mark_ready_rejects_invalid_lifecycle_state(stopping: bool) -> None:
    async def exercise() -> None:
        status = RuntimeStatus()
        if stopping:
            await status.mark_started()
            await status.mark_stopping()
        with pytest.raises(RuntimeError):
            await status.mark_ready()

    asyncio.run(exercise())
