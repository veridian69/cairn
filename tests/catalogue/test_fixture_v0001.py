import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from cairn.catalogue.sqlite import APPLICATION_ID

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v0001"
_FIXTURE_DB = _FIXTURE_DIR / "catalogue.sqlite3"
_FIXTURE_MANIFEST = _FIXTURE_DIR / "fixture.json"
_EXPECTED_INSTANCE_ID = UUID("9d3f5a72-4c81-4f2e-9b6a-2e7c8d1f0a35")
_EXPECTED_USER_VERSION = 1


def _fixture_metadata() -> dict[str, str]:
    return cast(dict[str, str], json.loads(_FIXTURE_MANIFEST.read_text()))


@pytest.fixture
def copied_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "catalogue.sqlite3"
    shutil.copy(_FIXTURE_DB, target)
    return target


def test_fixture_manifest_records_source_commit_and_instance_id() -> None:
    metadata = _fixture_metadata()
    assert metadata["source_commit"] == "c7e36ce5a2e94d40d5176423ae6bc1776debdb97"
    assert UUID(metadata["instance_id"]) == _EXPECTED_INSTANCE_ID


def test_fixture_opens_read_only(copied_fixture: Path) -> None:
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        assert connection.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO catalogue_metadata DEFAULT VALUES")
    finally:
        connection.close()


def test_fixture_application_id_and_schema_version(copied_fixture: Path) -> None:
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    assert application_id == (APPLICATION_ID,)
    assert user_version == (_EXPECTED_USER_VERSION,)


def test_fixture_instance_id_matches_manifest(copied_fixture: Path) -> None:
    metadata = _fixture_metadata()
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT instance_id FROM catalogue_metadata"
        ).fetchone()
    finally:
        connection.close()
    assert row == (metadata["instance_id"],)


def test_fixture_digest_matches_manifest(copied_fixture: Path) -> None:
    metadata = _fixture_metadata()
    digest = hashlib.sha256(copied_fixture.read_bytes()).hexdigest()
    assert digest == metadata["fixture_sha256"]
