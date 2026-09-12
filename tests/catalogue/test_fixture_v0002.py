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

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v0002"
_FIXTURE_DB = _FIXTURE_DIR / "catalogue.sqlite3"
_FIXTURE_MANIFEST = _FIXTURE_DIR / "fixture.json"
_EXPECTED_INSTANCE_ID = UUID("6e0f4a9c-3d21-4b87-9c5a-1f2e3d4c5b6a")
_EXPECTED_USER_VERSION = 2
_EXPECTED_REALM_ID = "local"
_EXPECTED_EVENT_COUNT = 2


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
    assert metadata["source_commit"] == "3d9262569ae5e2ef12421143464e5e7f72a11826"
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
    """The frozen events are the only historical audit content the suite has,
    and nothing had ever parsed them.

    P-16 tightened ``AuditDraft.__post_init__``, and
    ``parse_canonical_audit_bytes`` builds an ``AuditDraft`` — so the parser
    was tightened against events written before the rule existed, and the
    only check that it still accepts them was performed by hand. This is that
    check, automated: parse, re-encode byte-identically, and re-derive the
    stored ``event_hash`` from the parsed event rather than from the stored
    bytes, so a parser that silently dropped or defaulted a field could not
    reproduce it.
    """
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


def test_fixture_bootstrap_seed_content(copied_fixture: Path) -> None:
    connection = sqlite3.connect(f"{copied_fixture.as_uri()}?mode=ro", uri=True)
    try:
        realms = connection.execute("SELECT realm_id FROM realms").fetchall()
        assert realms == [(_EXPECTED_REALM_ID,)]

        principals = connection.execute(
            "SELECT principal_id, label FROM principals"
        ).fetchall()
        assert len(principals) == 1
        principal_id, label = principals[0]
        assert label == "operator"

        credentials = connection.execute(
            "SELECT principal_id FROM credentials"
        ).fetchall()
        assert credentials == [(principal_id,)]

        grants = connection.execute(
            "SELECT principal_id, realm_id FROM grants"
        ).fetchall()
        assert grants == [(principal_id, _EXPECTED_REALM_ID)] * 3

        sequences = connection.execute(
            "SELECT sequence FROM audit_events "
            "WHERE chain_kind = 'realm' AND chain_identity = ? "
            "ORDER BY sequence",
            (_EXPECTED_REALM_ID,),
        ).fetchall()
        assert sequences == [(1,), (2,)]
    finally:
        connection.close()
