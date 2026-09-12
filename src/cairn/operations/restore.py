"""P-66: the restore procedure, the other half of the I-17 pair.

``cairn restore`` targets a **replacement** deployment: it refuses a data
directory that is not empty (``lost+found`` alone tolerated) and never
merges into live state. The bundle's instance identity must equal the
configured one — restore retains identity, and there is no rebind
(I-41). The data-directory lease is acquired before anything is created
in the directory, so a concurrently started ``serve`` cannot open a
half-restored catalogue; every member's SHA-256 is verified against
``manifest.json`` **before** anything is installed, so a refusal names
the member and never partial-installs. Each member is read **once**:
streamed into the data directory under a staging name with the digest
computed over the bytes as they land, then linked into its final name
only after every member's digest has passed. Hashing the bundle and
then reading it again to install — even from a held descriptor — would
verify one version of the bytes and install whatever the inode holds by
the second read.

Quiescence — the original deployment stopped before this candidate opens
the catalogue — is an operational rule the runbook owns; the lease
cannot enforce it across PVCs and pretending otherwise would be a false
guarantee. What is enforced here: the install lands with I-51's file
discipline, and the same internal verification ``serve`` runs (I-48)
must pass — plus the Attic's own integrity checks when the bundle
carries that member — before the command reports success. A verification
failure leaves the installed files in place for diagnosis: the target is
a disposable empty volume, so the recovery is deletion and retry, and
the command's error output carries that as ``recovery``.
"""

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard
from uuid import UUID

from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CatalogueStorageError,
    parse_timestamp,
    read_connection,
)
from cairn.catalogue.verification import (
    VerificationReport,
    _verify_catalogue_locked,
)
from cairn.evidence.attic import ATTIC_FILENAME
from cairn.operations.backup import (
    BACKUP_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    AuditChainHead,
    BundleMember,
)
from cairn.runtime.config import CairnConfig
from cairn.runtime.lease import _LOCK_NAME, DataDirectoryLease, LeaseError

# The only directory entry a fresh volume legitimately carries (P-66).
_TOLERATED_ENTRIES = frozenset({"lost+found"})
# The same set once the lease is held, since acquiring it creates the
# lock file — the one entry restore itself is entitled to have added.
_LEASED_ENTRIES = _TOLERATED_ENTRIES | {_LOCK_NAME}
# The only names a manifest may install, so a hostile manifest cannot
# name a path and reach outside the data directory.
_MEMBER_NAMES = frozenset({CATALOGUE_FILENAME, ATTIC_FILENAME})
# Members are streamed into the data directory under this suffix and
# only linked to their final names once every digest has passed.
_STAGING_SUFFIX = ".restoring"
_SHA256_HEX = 64


class RestoreError(Exception):
    """Closed vocabulary of restore refusal codes.

    Codes: ``bundle_unreadable``, ``manifest_invalid``,
    ``instance_mismatch``, ``data_directory_unavailable``,
    ``data_directory_not_empty``, ``digest_mismatch`` (naming the
    member), ``install_failed``, ``attic_integrity_failed``,
    ``audit_boundary_mismatch``, ``audit_boundary_unreadable``,
    ``lease_release_failed``.
    """

    def __init__(
        self,
        code: str,
        member: str | None = None,
        *,
        recovery_required: bool = False,
    ) -> None:
        self.code = code
        self.member = member
        self.recovery_required = recovery_required
        detail = f"restore error: {code}"
        if member is not None:
            detail = f"{detail} ({member})"
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    instance_id: UUID
    bundle_path: Path
    audit_boundary: tuple[AuditChainHead, ...]
    members: tuple[BundleMember, ...]
    report: VerificationReport


def restore_bundle(config: CairnConfig, bundle_path: Path) -> RestoreResult:
    """The P-66 procedure, in its pinned order: manifest and identity,
    empty-directory refusal, lease, digest verification, install,
    verification, boundary report."""
    manifest = _load_manifest(bundle_path)
    if manifest.instance_id != config.instance_id:
        raise RestoreError("instance_mismatch")
    _require_empty_data_directory(config.paths.data)

    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        # The pre-lease refusal cannot be the last word: between it and
        # the lease, anything may have written to the directory, and
        # ``O_EXCL`` only catches a collision on the two member names.
        # Repeating the check under the lease closes the window — from
        # here on the only writer entitled to the directory is this one.
        _require_empty_data_directory(
            config.paths.data,
            tolerated=_LEASED_ENTRIES,
            recovery_required=True,
        )
        _install_members(bundle_path, config.paths.data, manifest.members)
        try:
            _fsync_directory(config.paths.data)
        except OSError as error:
            raise RestoreError("install_failed") from error
        report = _verify_catalogue_locked(config)
        if _installed_heads(config.paths.data) != manifest.audit_boundary:
            raise RestoreError("audit_boundary_mismatch")
        if any(member.name == ATTIC_FILENAME for member in manifest.members):
            _verify_attic(config.paths.data / ATTIC_FILENAME)
    except BaseException:
        # The refusal in flight is the honest signal, and it already
        # carries its own recovery; a release failure on top of it must
        # not replace it with a lease error that carries none.
        try:
            lease.release()
        except LeaseError:
            pass
        raise
    try:
        lease.release()
    except LeaseError as error:
        # Post-install: the members are installed and verified, but the
        # command failed, and a retry would refuse the non-empty
        # directory — so this too carries the recovery instruction.
        raise RestoreError("lease_release_failed") from error
    return RestoreResult(
        instance_id=manifest.instance_id,
        bundle_path=bundle_path,
        audit_boundary=manifest.audit_boundary,
        members=manifest.members,
        report=report,
    )


@dataclass(frozen=True, slots=True)
class _Manifest:
    instance_id: UUID
    audit_boundary: tuple[AuditChainHead, ...]
    members: tuple[BundleMember, ...]


def _load_manifest(bundle_path: Path) -> _Manifest:
    if not bundle_path.is_dir():
        raise RestoreError("bundle_unreadable")
    try:
        raw = (bundle_path / MANIFEST_FILENAME).read_bytes()
    except OSError as error:
        raise RestoreError("bundle_unreadable") from error
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise RestoreError("manifest_invalid") from error
    if not isinstance(document, dict):
        raise RestoreError("manifest_invalid")
    if document.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise RestoreError("manifest_invalid")
    instance_raw = document.get("instance_id")
    if type(instance_raw) is not str:
        raise RestoreError("manifest_invalid")
    try:
        instance_id = UUID(instance_raw)
    except ValueError as error:
        raise RestoreError("manifest_invalid") from error
    _validate_provenance(document)
    return _Manifest(
        instance_id=instance_id,
        audit_boundary=_validated_boundary(document.get("audit_boundary")),
        members=_validated_members(document.get("members")),
    )


def _validate_provenance(document: dict[str, object]) -> None:
    """The two manifest fields P-65 writes and nothing else reads.

    A field no reader validates is decorative, and a decorative field is
    a trap: it looks like a guarantee. ``created_at`` must parse as a
    canonical timestamp, and ``catalogue_schema_version`` must be the
    schema this build packages — v0.1 restores a bundle, it does not
    migrate one. ``_verify_catalogue_locked`` would refuse a foreign
    schema later on its packaged-versus-applied history, but incidentally
    and after the members are installed; refusing here is the deliberate
    check, before anything is written. The strict ``int`` test is not
    ceremony: ``True == 1`` in Python, and a bool must not pass for a
    schema version.
    """
    created_at = document.get("created_at")
    if type(created_at) is not str:
        raise RestoreError("manifest_invalid")
    try:
        parse_timestamp(created_at)
    except CatalogueStorageError as error:
        raise RestoreError("manifest_invalid") from error
    schema_version = document.get("catalogue_schema_version")
    if type(schema_version) is not int or schema_version != CURRENT_SCHEMA_VERSION:
        raise RestoreError("manifest_invalid")


def _validated_members(value: object) -> tuple[BundleMember, ...]:
    if not isinstance(value, list) or not value:
        raise RestoreError("manifest_invalid")
    members: list[BundleMember] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise RestoreError("manifest_invalid")
        name = entry.get("name")
        byte_count = entry.get("bytes")
        digest = entry.get("sha256")
        # The name whitelist is the path-traversal guard: nothing outside
        # the two known member files is ever created from manifest data.
        if (
            name not in _MEMBER_NAMES
            or type(byte_count) is not int
            or byte_count < 0
            or not _hex_digest(digest)
        ):
            raise RestoreError("manifest_invalid")
        members.append(BundleMember(name=name, byte_count=byte_count, sha256=digest))
    names = [member.name for member in members]
    if len(set(names)) != len(names) or CATALOGUE_FILENAME not in names:
        raise RestoreError("manifest_invalid")
    return tuple(members)


def _validated_boundary(value: object) -> tuple[AuditChainHead, ...]:
    if not isinstance(value, list):
        raise RestoreError("manifest_invalid")
    boundary: list[AuditChainHead] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise RestoreError("manifest_invalid")
        chain_kind = entry.get("chain_kind")
        chain_identity = entry.get("chain_identity")
        sequence = entry.get("sequence")
        head_digest = entry.get("head_digest")
        if (
            type(chain_kind) is not str
            or type(chain_identity) is not str
            or type(sequence) is not int
            or sequence < 0
            or not _hex_digest(head_digest)
        ):
            raise RestoreError("manifest_invalid")
        boundary.append(
            AuditChainHead(
                chain_kind=chain_kind,
                chain_identity=chain_identity,
                sequence=sequence,
                head_digest=head_digest,
            )
        )
    return tuple(boundary)


def _hex_digest(value: object) -> TypeGuard[str]:
    return (
        type(value) is str
        and len(value) == _SHA256_HEX
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_empty_data_directory(
    data_path: Path,
    *,
    tolerated: frozenset[str] | set[str] = _TOLERATED_ENTRIES,
    recovery_required: bool = False,
) -> None:
    try:
        entries = [entry.name for entry in data_path.iterdir()]
    except (OSError, NotADirectoryError) as error:
        raise RestoreError("data_directory_unavailable") from error
    if set(entries) - tolerated:
        raise RestoreError(
            "data_directory_not_empty",
            recovery_required=recovery_required,
        )


def _install_members(
    bundle_path: Path,
    data_path: Path,
    members: tuple[BundleMember, ...],
) -> None:
    """Stage every member, then link them into place — P-66's
    verify-all-before-install-any order, with the copy moved ahead of the
    verification so the digest can be computed over the copy.

    A refusal during staging unlinks every staging file, so the
    directory is back to holding at most the lease file. The link is the
    install: ``os.link`` refuses an occupied final name, preserving the
    no-clobber property ``O_EXCL`` used to provide.
    """
    staged: list[tuple[str, Path]] = []
    try:
        for member in members:
            staged.append((member.name, _stage_member(bundle_path, data_path, member)))
        for name, staging_path in staged:
            try:
                os.link(staging_path, data_path / name)
                os.unlink(staging_path)
            except OSError as error:
                raise RestoreError("install_failed", name) from error
    except BaseException:
        for _, staging_path in staged:
            _unlink_quietly(staging_path)
        raise


def _stage_member(bundle_path: Path, data_path: Path, member: BundleMember) -> Path:
    """One member, read once: streamed into the data directory under a
    staging name with the digest computed over the bytes as they land.
    Only regular files are accepted, and the manifest byte count bounds
    the read to one byte beyond the claimed size so a mismatch refuses
    before an oversized member can fill the target.

    Hashing the bundle member and then reading it again to install — by
    name or from a held descriptor — verifies one version of the bytes
    and installs whatever the inode holds at the second read: anyone
    able to write the bundle directory, an operator-selected sink
    outside Cairn's trust boundary (I-17), could mutate the member in
    between. Here there is no second read; the digest describes exactly
    the bytes the link will install. The staging file carries I-51's
    discipline from birth: mode ``0660``, ``O_EXCL``, fsync.
    """
    staging_path = data_path / f"{member.name}{_STAGING_SUFFIX}"
    try:
        source_fd = os.open(
            bundle_path / member.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RestoreError("digest_mismatch", member.name) from error
    digest = hashlib.sha256()
    byte_count = 0
    try:
        try:
            source_status = os.fstat(source_fd)
        except OSError as error:
            raise RestoreError("digest_mismatch", member.name) from error
        if not stat.S_ISREG(source_status.st_mode):
            raise RestoreError("digest_mismatch", member.name)

        # A staging-open failure creates nothing, so it cleans nothing:
        # an occupied staging name is refused, never unlinked.
        try:
            target_fd = os.open(
                staging_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o660,
            )
        except OSError as error:
            raise RestoreError("install_failed", member.name) from error
        try:
            while True:
                try:
                    remaining = member.byte_count - byte_count
                    chunk = os.read(source_fd, min(1 << 20, remaining + 1))
                except OSError as error:
                    raise RestoreError("digest_mismatch", member.name) from error
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise RestoreError("digest_mismatch", member.name)
                if os.write(target_fd, chunk) != len(chunk):
                    raise RestoreError("install_failed", member.name)
                digest.update(chunk)
                byte_count += len(chunk)
            os.fchmod(target_fd, 0o660)
            os.fsync(target_fd)
        except RestoreError:
            _unlink_quietly(staging_path)
            raise
        except OSError as error:
            _unlink_quietly(staging_path)
            raise RestoreError("install_failed", member.name) from error
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)
    if byte_count != member.byte_count or digest.hexdigest() != member.sha256:
        _unlink_quietly(staging_path)
        raise RestoreError("digest_mismatch", member.name)
    return staging_path


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _installed_heads(data_path: Path) -> tuple[AuditChainHead, ...]:
    """The installed catalogue's heads, for the declared-versus-actual
    boundary comparison — the read that makes "report the verified
    boundary" a verification rather than an echo of the manifest. A
    catalogue this read cannot open is a typed post-install refusal, not
    an escape: the members are already installed, and the caller's error
    output must still carry the recovery instruction."""
    try:
        with read_connection(data_path) as connection:
            rows = connection.execute(
                "SELECT chain_kind, chain_identity, last_sequence, last_hash "
                "FROM audit_heads ORDER BY chain_kind, chain_identity"
            ).fetchall()
    except (sqlite3.Error, CatalogueStorageError) as error:
        raise RestoreError("audit_boundary_unreadable") from error
    return tuple(
        AuditChainHead(
            chain_kind=chain_kind,
            chain_identity=chain_identity,
            sequence=sequence,
            head_digest=head.hex(),
        )
        for chain_kind, chain_identity, sequence, head in rows
    )


def _verify_attic(attic_path: Path) -> None:
    """The Attic member's own checks (P-66): SQLite integrity and the
    FTS5 index's integrity-check command. Opened read-write because the
    FTS check is issued through ``INSERT INTO payloads_fts(payloads_fts)``
    — a command, not a row; it modifies nothing."""
    try:
        connection = sqlite3.connect(
            f"{attic_path.as_uri()}?mode=rw&nofollow=1",
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
    except sqlite3.Error as error:
        raise RestoreError("attic_integrity_failed") from error
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RestoreError("attic_integrity_failed")
        connection.execute(
            "INSERT INTO payloads_fts(payloads_fts) VALUES ('integrity-check')"
        )
    except sqlite3.Error as error:
        raise RestoreError("attic_integrity_failed") from error
    finally:
        connection.close()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
