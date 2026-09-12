"""P-65: the coordinated backup bundle and its mutation barrier.

``cairn backup`` runs as a second process beside the live server (I-35)
and deliberately does not take the data-directory lease — that is
``serve``'s ownership guard, not a reader's. The barrier is a
``BEGIN IMMEDIATE`` transaction on the live catalogue: every Cairn writer
runs ``BEGIN IMMEDIATE`` under the 5,000 ms busy timeout (I-49), so while
the barrier is held no mutation can commit anywhere — REST, MCP, the
delivery loop's delivered-marking — and the catalogue, the audit heads
and both outboxes are copied from one instant.

Member order is pinned — catalogue first, then Attic — so the Attic
member can only ever be *ahead* of the catalogue's outbox state, never
behind it: an in-flight delivery may land an Attic row whose
delivered-mark was barred, and restore's reconciliation then finds a
pending outbox entry whose payload already exists, where redelivery is
idempotent by identity. The one direction is stated so nobody "fixes"
the order. Credentials and FalkorDB state are excluded — the FalkorDB
index is derived, and deleting it is a recovery procedure completed by
``cairn rebuild-index`` (I-17, I-91).
"""

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cairn.catalogue.sqlite import (
    APPLICATION_ID,
    CATALOGUE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import _begin_immediate
from cairn.evidence.attic import ATTIC_FILENAME
from cairn.runtime.config import CairnConfig

BACKUP_SCHEMA_VERSION = "cairn.backup/v1"
MANIFEST_FILENAME = "manifest.json"
_BUNDLE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """Closed vocabulary of backup refusal codes.

    Codes: ``output_unavailable``, ``bundle_exists``,
    ``instance_mismatch``, ``audit_boundary_invalid``,
    ``member_copy_failed``, ``manifest_write_failed``.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"backup error: {code}")


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    chain_kind: str
    chain_identity: str
    sequence: int
    head_digest: str


@dataclass(frozen=True, slots=True)
class BundleMember:
    name: str
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupResult:
    instance_id: UUID
    bundle_path: Path
    created_at: datetime
    barrier_ms: float
    audit_boundary: tuple[AuditChainHead, ...]
    members: tuple[BundleMember, ...]


def create_backup(
    config: CairnConfig,
    output_path: Path,
    *,
    clock: Callable[[], datetime],
) -> BackupResult:
    """The P-65 procedure: barrier, boundary read, pinned-order copies,
    barrier release, digests, manifest.

    Raises ``CatalogueContention`` when another writer outlasts the busy
    timeout while the barrier is being taken — the same typed retryable
    failure the barrier inflicts on writers, in the other direction.
    """
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError("output_unavailable") from error

    created_at = clock()
    bundle_path: Path
    with _open_write_connection(config.paths.data, create=False) as connection:
        _begin_immediate(connection)
        barrier_started = time.monotonic()
        try:
            instance_id = _verified_identity(connection, config.instance_id)
            boundary = _read_audit_boundary(connection)
            bundle_path = _create_bundle_directory(output_path, instance_id, created_at)
            member_names = [CATALOGUE_FILENAME]
            _copy_member(
                config.paths.data / CATALOGUE_FILENAME,
                bundle_path / CATALOGUE_FILENAME,
            )
            attic_path = config.paths.data / ATTIC_FILENAME
            # Enabled-but-absent is a real state, not an error: the Attic
            # file is created lazily on first store, so an instance that
            # has never held exact evidence has nothing to copy and the
            # bundle honestly carries no Attic member.
            if config.attic.enabled and attic_path.exists():
                _copy_member(attic_path, bundle_path / ATTIC_FILENAME)
                member_names.append(ATTIC_FILENAME)
        finally:
            connection.execute("ROLLBACK")
            barrier_ms = (time.monotonic() - barrier_started) * 1000

    members = tuple(_measured_member(bundle_path / name, name) for name in member_names)
    _write_manifest(
        bundle_path,
        instance_id=instance_id,
        created_at=created_at,
        boundary=boundary,
        members=members,
    )
    _fsync_directory(bundle_path)
    _fsync_directory(output_path)
    return BackupResult(
        instance_id=instance_id,
        bundle_path=bundle_path,
        created_at=created_at,
        barrier_ms=barrier_ms,
        audit_boundary=boundary,
        members=members,
    )


def _verified_identity(
    connection: sqlite3.Connection,
    configured_id: UUID,
) -> UUID:
    """The catalogue must be the current schema and the configured
    instance before a bundle names it. The mismatch refusal mirrors
    ``verification._verify_identity``: a backup of somebody else's data
    directory is exactly the confusion I-41's retained identity exists to
    prevent."""
    identity = (
        connection.execute("PRAGMA application_id").fetchone(),
        connection.execute("PRAGMA user_version").fetchone(),
    )
    if identity != ((APPLICATION_ID,), (CURRENT_SCHEMA_VERSION,)):
        raise CatalogueStorageError("catalogue_identity_mismatch")
    metadata = connection.execute(
        "SELECT instance_id FROM catalogue_metadata"
    ).fetchall()
    if len(metadata) != 1 or metadata[0][0] != str(configured_id):
        raise BackupError("instance_mismatch")
    return configured_id


def _read_audit_boundary(
    connection: sqlite3.Connection,
) -> tuple[AuditChainHead, ...]:
    rows = connection.execute(
        "SELECT chain_kind, chain_identity, last_sequence, last_hash "
        "FROM audit_heads ORDER BY chain_kind, chain_identity"
    ).fetchall()
    boundary: list[AuditChainHead] = []
    for chain_kind, chain_identity, sequence, head in rows:
        if (
            type(chain_kind) is not str
            or type(chain_identity) is not str
            or type(sequence) is not int
            or type(head) is not bytes
            or len(head) != 32
        ):
            raise BackupError("audit_boundary_invalid")
        boundary.append(
            AuditChainHead(
                chain_kind=chain_kind,
                chain_identity=chain_identity,
                sequence=sequence,
                head_digest=head.hex(),
            )
        )
    return tuple(boundary)


def _create_bundle_directory(
    output_path: Path,
    instance_id: UUID,
    created_at: datetime,
) -> Path:
    timestamp = created_at.astimezone(UTC).strftime(_BUNDLE_TIMESTAMP_FORMAT)
    bundle_path = output_path / f"cairn-backup-{instance_id}-{timestamp}"
    try:
        bundle_path.mkdir()
    except FileExistsError as error:
        raise BackupError("bundle_exists") from error
    except OSError as error:
        raise BackupError("output_unavailable") from error
    return bundle_path


def _copy_member(source_path: Path, target_path: Path) -> None:
    """One member through SQLite's online backup API, from its own read
    connection. WAL readers are unaffected by the barrier, and with no
    commits possible the copy completes in one pass."""
    try:
        source = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro&nofollow=1",
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
    except sqlite3.Error as error:
        raise BackupError("member_copy_failed") from error
    try:
        target = sqlite3.connect(target_path, timeout=5.0, isolation_level=None)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as error:
        raise BackupError("member_copy_failed") from error
    finally:
        source.close()
    try:
        os.chmod(target_path, 0o660)
        _fsync_file(target_path)
    except OSError as error:
        raise BackupError("member_copy_failed") from error


def _measured_member(path: Path, name: str) -> BundleMember:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as member:
        while chunk := member.read(1 << 20):
            digest.update(chunk)
            byte_count += len(chunk)
    return BundleMember(
        name=name,
        byte_count=byte_count,
        sha256=digest.hexdigest(),
    )


def _write_manifest(
    bundle_path: Path,
    *,
    instance_id: UUID,
    created_at: datetime,
    boundary: tuple[AuditChainHead, ...],
    members: tuple[BundleMember, ...],
) -> None:
    payload = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "instance_id": str(instance_id),
        "created_at": canonical_timestamp(created_at),
        "catalogue_schema_version": CURRENT_SCHEMA_VERSION,
        "audit_boundary": [
            {
                "chain_kind": head.chain_kind,
                "chain_identity": head.chain_identity,
                "sequence": head.sequence,
                "head_digest": head.head_digest,
            }
            for head in boundary
        ],
        "members": [
            {
                "name": member.name,
                "bytes": member.byte_count,
                "sha256": member.sha256,
            }
            for member in members
        ],
    }
    manifest_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    manifest_path = bundle_path / MANIFEST_FILENAME
    try:
        fd = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o660,
        )
        try:
            if os.write(fd, manifest_bytes) != len(manifest_bytes):
                raise BackupError("manifest_write_failed")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        raise BackupError("manifest_write_failed") from error


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
