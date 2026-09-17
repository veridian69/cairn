import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from cairn.catalogue.audit import (
    canonical_audit_bytes,
    hash_audit_event,
    parse_canonical_audit_bytes,
)
from cairn.catalogue.sqlite import APPLICATION_ID

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v0003"
_FIXTURE_DB = _FIXTURE_DIR / "catalogue.sqlite3"
_FIXTURE_MANIFEST = _FIXTURE_DIR / "fixture.json"
_EXPECTED_INSTANCE_ID = UUID("5c1f8a2b-7d43-4e96-8b1a-2f3c4d5e6a7b")
_EXPECTED_USER_VERSION = 3
_EXPECTED_REALM_ID = "local"
# realm-genesis, realm-bootstrap, then one ingest event per assertion.
_EXPECTED_EVENT_COUNT = 5
_EXPECTED_OUTBOX_ROWS = 4


def _fixture_metadata() -> dict[str, str]:
    return cast(dict[str, str], json.loads(_FIXTURE_MANIFEST.read_text()))


@pytest.fixture
def copied_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "catalogue.sqlite3"
    shutil.copy(_FIXTURE_DB, target)
    target.chmod(0o660)
    return target


def test_fixture_manifest_records_source_commit_and_instance_id() -> None:
    metadata = _fixture_metadata()
    assert metadata["source_commit"] == "c27939ae4931b8b475599fe84683605f995f8b2a"
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


def test_fixture_events_reparse_reencode_and_rehash(copied_fixture: Path) -> None:
    """The same check v0001 and v0002 carry: the frozen events parse,
    re-encode byte-identically, and re-derive their stored hash from the
    parsed event rather than from the stored bytes."""
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT canonical_event, event_hash FROM audit_events "
            "ORDER BY chain_kind, chain_identity, sequence"
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == _EXPECTED_EVENT_COUNT
    for canonical_event, event_hash in rows:
        event = parse_canonical_audit_bytes(canonical_event)
        assert canonical_audit_bytes(event) == canonical_event
        assert hash_audit_event(event) == event_hash


def test_fixture_carries_the_pre_sequence_projection_outbox(
    copied_fixture: Path,
) -> None:
    """This fixture exists for migration 0004, so what matters is that it
    is a genuine slice 4/5 outbox: no ``sequence`` column, and rows whose
    ``created_at`` values do not by themselves determine an order — two
    of the four share a timestamp, so the backfill's ``work_id`` tiebreak
    is exercised rather than assumed."""
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(projection_outbox)"
            ).fetchall()
        ]
        rows = connection.execute(
            "SELECT created_at, work_id, kind FROM projection_outbox"
        ).fetchall()
    finally:
        connection.close()

    assert "sequence" not in columns
    assert len(rows) == _EXPECTED_OUTBOX_ROWS
    assert len({row[0] for row in rows}) == 3
    assert {row[2] for row in rows} == {"fact-ingested"}


def test_fixture_bootstrap_and_ingest_seed_content(copied_fixture: Path) -> None:
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT realm_id FROM realms").fetchall() == [
            (_EXPECTED_REALM_ID,)
        ]
        principals = connection.execute(
            "SELECT principal_id, label FROM principals"
        ).fetchall()
        assert len(principals) == 1
        principal_id, label = principals[0]
        assert label == "fixture"
        assert connection.execute(
            "SELECT principal_id FROM credentials"
        ).fetchall() == [(principal_id,)]
        assert (
            connection.execute("SELECT principal_id, realm_id FROM grants").fetchall()
            == [(principal_id, _EXPECTED_REALM_ID)] * 3
        )

        assert connection.execute("SELECT count(*) FROM assertions").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM facts").fetchone() == (4,)
        assert connection.execute(
            "SELECT count(*) FROM evidence_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM evidence_outbox"
        ).fetchone() == (0,)
    finally:
        connection.close()
