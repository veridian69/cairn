"""Private, bounded metadata receipts for one managed conversation turn.

The journal records write intent and verified identifiers, never source or fact
content. It is observability for the trusted host and is not a transcript,
retry store, or semantic guarantee that a model saved useful context.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, cast
from uuid import RFC_4122, UUID, uuid4

from cairn.client.conversation_sources import SourceBundle
from cairn.client.diagnostics import _unique_object

SCHEMA = "cairn.turn-receipts/v1"
MAX_BYTES = 65536
MAX_OPERATIONS = 32
_OUTCOME_STATUSES = {"verified", "partial", "unconfirmed", "rejected"}
_LOCK_SCHEMA = "cairn.turn-receipts-lock/v1"


class ReceiptJournalError(ValueError):
    """Closed journal failure without paths, content, or raw exceptions."""

    def __init__(self, code: str = "receipt_journal_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TurnMemory:
    status: Literal["none", "verified", "partial", "unknown"]
    fact_ids: tuple[str, ...] = ()
    attempted: int = 0

    def public(self) -> dict[str, object]:
        return {
            "status": self.status,
            "fact_ids": list(self.fact_ids),
            "attempted": self.attempted,
        }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _context(sources: SourceBundle) -> dict[str, object]:
    if type(sources) is not SourceBundle:
        raise ReceiptJournalError("receipt_context_mismatch")
    document = sources.to_document()
    return {
        "instance_id": str(sources.instance_id),
        "principal_id": str(sources.principal_id),
        "scope": document["scope"],
        "classification": sources.classification.value,
        "session_id": str(sources.session_id),
        "source_ids": [str(source.source_id) for source in sources.sources],
        "source_digest": hashlib.sha256(_canonical(document)).hexdigest(),
    }


def _parent(path: Path) -> tuple[int, str]:
    directory: int | None = None
    try:
        if os.name != "posix" or not isinstance(path, Path) or not path.is_absolute():
            raise ReceiptJournalError()
        if len(path.parts) > 1 and path.parts[1] == "mnt":
            raise ReceiptJournalError()
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            next_directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReceiptJournalError()
        return directory, path.name
    except ReceiptJournalError:
        if directory is not None:
            os.close(directory)
        raise
    except OSError:
        if directory is not None:
            os.close(directory)
        raise ReceiptJournalError() from None


def _read(path: Path) -> dict[str, object]:
    directory, name = _parent(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ReceiptJournalError()
        data = bytearray()
        while len(data) <= MAX_BYTES:
            chunk = os.read(descriptor, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise ReceiptJournalError()
        value = json.loads(
            bytes(data).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ReceiptJournalError()),
        )
        if type(value) is not dict:
            raise ReceiptJournalError()
        return cast(dict[str, object], value)
    except ReceiptJournalError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ReceiptJournalError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _write(path: Path, document: dict[str, object], *, create: bool = False) -> None:
    data = _canonical(document)
    if len(data) > MAX_BYTES:
        raise ReceiptJournalError("receipt_journal_full")
    directory, name = _parent(path)
    temporary = f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        if create:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        else:
            # Validate the current target before replacing it in the private dir.
            current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(current)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ReceiptJournalError()
            finally:
                os.close(current)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if not create:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except ReceiptJournalError:
        raise
    except OSError:
        raise ReceiptJournalError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not create:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _lock_document(pending: bool) -> bytes:
    return _canonical({"schema": _LOCK_SCHEMA, "pending_begin": pending})


def _read_pending(target: IO[bytes]) -> bool:
    target.seek(0)
    try:
        document = json.loads(
            target.read(MAX_BYTES + 1).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ReceiptJournalError()),
        )
        if (
            type(document) is not dict
            or set(document) != {"schema", "pending_begin"}
            or document["schema"] != _LOCK_SCHEMA
            or type(document["pending_begin"]) is not bool
        ):
            raise ReceiptJournalError()
        return document["pending_begin"]
    except ReceiptJournalError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReceiptJournalError() from None


def _set_pending(target: IO[bytes], pending: bool) -> None:
    data = _lock_document(pending)
    # Make an interrupted marker update self-invalidating before touching its
    # bytes. Only a fully flushed marker is restored to the accepted 0600 mode.
    os.fchmod(target.fileno(), 0o400)
    os.fsync(target.fileno())
    target.seek(0)
    target.truncate()
    target.write(data)
    target.flush()
    os.fsync(target.fileno())
    os.fchmod(target.fileno(), 0o600)
    os.fsync(target.fileno())


@contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[IO[bytes]]:
    directory, name = _parent(_lock_path(path))
    descriptor: int | None = None
    target: IO[bytes] | None = None
    try:
        for attempt in range(200):
            try:
                descriptor = os.open(
                    name,
                    (os.O_RDWR if exclusive else os.O_RDONLY) | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                break
            except PermissionError:
                # A writer poisons the marker mode while holding flock. Give
                # that bounded critical section time to restore the valid mode.
                if attempt == 199:
                    raise
                time.sleep(0.001)
        assert descriptor is not None
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ReceiptJournalError()
        target = os.fdopen(descriptor, "r+b" if exclusive else "rb", closefd=False)
        _read_pending(target)
        yield target
    except ReceiptJournalError:
        raise
    except OSError:
        raise ReceiptJournalError() from None
    finally:
        if target is not None:
            target.close()
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(directory)


def _create_lock(path: Path) -> None:
    lock = _lock_path(path)
    directory, name = _parent(lock)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        data = _lock_document(False)
        os.write(descriptor, data)
        os.fsync(descriptor)
        os.fsync(directory)
    except OSError:
        raise ReceiptJournalError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _uuid(value: object, *, v4: bool = False) -> str:
    if type(value) is not str:
        raise ReceiptJournalError()
    try:
        identity = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ReceiptJournalError() from None
    if (
        str(identity) != value
        or identity.variant != RFC_4122
        or (v4 and identity.version != 4)
    ):
        raise ReceiptJournalError()
    return value


def _validate(document: dict[str, object], sources: SourceBundle) -> None:
    if set(document) != {"schema", "context", "adapter_started", "operations"}:
        raise ReceiptJournalError()
    if document["schema"] != SCHEMA or type(document["adapter_started"]) is not bool:
        raise ReceiptJournalError()
    if document["context"] != _context(sources):
        raise ReceiptJournalError("receipt_context_mismatch")
    operations = document["operations"]
    if type(operations) is not list or len(operations) > MAX_OPERATIONS:
        raise ReceiptJournalError()
    source_ids = {str(source.source_id) for source in sources.sources}
    for index, raw in enumerate(operations, 1):
        if type(raw) is not dict or set(raw) != {
            "sequence",
            "operation",
            "source_id",
            "outcome",
        }:
            raise ReceiptJournalError()
        operation = cast(dict[str, object], raw)
        if (
            operation["sequence"] != index
            or operation["operation"] not in {"remember", "replace"}
            or _uuid(operation["source_id"]) not in source_ids
        ):
            raise ReceiptJournalError()
        outcome = operation["outcome"]
        if outcome is None:
            continue
        if type(outcome) is not dict or set(outcome) != {"status", "fact_ids"}:
            raise ReceiptJournalError()
        outcome = cast(dict[str, object], outcome)
        if (
            outcome["status"] not in _OUTCOME_STATUSES
            or type(outcome["fact_ids"]) is not list
        ):
            raise ReceiptJournalError()
        for fact_id in cast(list[object], outcome["fact_ids"]):
            _uuid(fact_id, v4=True)


def _safe_outcome(result: dict[str, object]) -> dict[str, object]:
    if type(result) is not dict or result.get("status") not in _OUTCOME_STATUSES:
        raise ReceiptJournalError("invalid_receipt_result")
    fact_ids: list[str] = []
    mapping = result.get("mapping")
    if type(mapping) is dict and "fact_id" in mapping:
        fact_ids.append(_uuid(mapping["fact_id"], v4=True))
    return {"status": result["status"], "fact_ids": fact_ids}


class ReceiptJournal:
    def __init__(self, path: Path, sources: SourceBundle) -> None:
        self._path = path
        self._sources = sources
        self._lock = threading.Lock()

    def _bound_to(self, sources: SourceBundle) -> bool:
        """Confirm an adapter uses the exact admitted context, without content."""
        try:
            return _context(self._sources) == _context(sources)
        except ReceiptJournalError:
            return False

    @classmethod
    def create(cls, path: Path, sources: SourceBundle) -> ReceiptJournal:
        document: dict[str, object] = {
            "schema": SCHEMA,
            "context": _context(sources),
            "adapter_started": False,
            "operations": [],
        }
        _create_lock(path)
        try:
            _write(path, document, create=True)
        except ReceiptJournalError:
            try:
                _lock_path(path).unlink()
            except OSError:
                pass
            raise
        return cls(path, sources)

    @classmethod
    def open(cls, path: Path, sources: SourceBundle) -> ReceiptJournal:
        journal = cls(path, sources)
        with journal._lock:
            with _locked(path, exclusive=True):
                document = _read(path)
                _validate(document, sources)
                document["adapter_started"] = True
                _write(path, document)
        return journal

    @staticmethod
    def summarise(path: Path, sources: SourceBundle) -> TurnMemory:
        try:
            with _locked(path, exclusive=False) as lock:
                pending = _read_pending(lock)
                document = _read(path)
                _validate(document, sources)
                if document["adapter_started"] is not True:
                    return TurnMemory(status="unknown")
                operations = cast(list[dict[str, object]], document["operations"])
                if not operations:
                    return TurnMemory(status="unknown" if pending else "none")
                fact_ids: list[str] = []
                unfinished = pending
                partial = False
                for operation in operations:
                    outcome = cast(dict[str, object] | None, operation["outcome"])
                    if outcome is None:
                        unfinished = True
                        continue
                    if outcome["status"] != "verified":
                        partial = True
                    for fact_id in cast(list[str], outcome["fact_ids"]):
                        if fact_id not in fact_ids:
                            fact_ids.append(fact_id)
                status: Literal["verified", "partial", "unknown"]
                status = (
                    "unknown" if unfinished else "partial" if partial else "verified"
                )
                return TurnMemory(status, tuple(fact_ids), len(operations))
        except (ReceiptJournalError, OSError, ValueError, TypeError):
            return TurnMemory(status="unknown")

    def begin(self, operation: str, source_id: UUID) -> int:
        if operation not in {"remember", "replace"} or type(source_id) is not UUID:
            raise ReceiptJournalError("invalid_receipt_operation")
        if source_id not in {source.source_id for source in self._sources.sources}:
            raise ReceiptJournalError("receipt_context_mismatch")
        with self._lock:
            with _locked(self._path, exclusive=True) as lock:
                if _read_pending(lock):
                    raise ReceiptJournalError()
                _set_pending(lock, True)
                document = _read(self._path)
                _validate(document, self._sources)
                if document["adapter_started"] is not True:
                    raise ReceiptJournalError()
                operations = cast(list[dict[str, object]], document["operations"])
                if len(operations) >= MAX_OPERATIONS:
                    raise ReceiptJournalError("receipt_journal_full")
                sequence = len(operations) + 1
                operations.append(
                    {
                        "sequence": sequence,
                        "operation": operation,
                        "source_id": str(source_id),
                        "outcome": None,
                    }
                )
                _write(self._path, document)
                _set_pending(lock, False)
                return sequence

    def finish(self, sequence: int, result: dict[str, object]) -> None:
        if type(sequence) is not int or sequence <= 0:
            raise ReceiptJournalError("invalid_receipt_operation")
        outcome = _safe_outcome(result)
        with self._lock:
            with _locked(self._path, exclusive=True):
                document = _read(self._path)
                _validate(document, self._sources)
                operations = cast(list[dict[str, object]], document["operations"])
                if (
                    sequence > len(operations)
                    or operations[sequence - 1]["outcome"] is not None
                ):
                    raise ReceiptJournalError("invalid_receipt_operation")
                operations[sequence - 1]["outcome"] = outcome
                _write(self._path, document)
