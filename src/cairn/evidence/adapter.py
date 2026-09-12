"""Attic adapter protocol (I-69): the closed three-operation contract for
exact evidence payload custody, and its frozen result values.

Domain outcomes are always returned as one of these values, never raised:
``store`` succeeds or reports a same-identity byte mismatch; ``fetch``
returns the payload, its absence, or a digest mismatch; ``search`` returns
candidate identities only. Adapter infrastructure failures raise instead —
see ``cairn.evidence.attic.AtticStorageError`` for the real implementation.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PayloadStored:
    """``store`` succeeded: either a new payload was written, or an
    identical payload was re-stored under the same identity (idempotent
    no-op)."""


@dataclass(frozen=True, slots=True)
class PayloadCorrupt:
    """The identity is bound to bytes other than those in play: on
    ``store``, a genuine identity collision with different bytes; on
    ``fetch``, the stored payload no longer matches its digest. The
    originally stored bytes are never overwritten."""


@dataclass(frozen=True, slots=True)
class FetchedPayload:
    payload: bytes


@dataclass(frozen=True, slots=True)
class PayloadAbsent:
    """No payload is stored under this identity."""


class AtticAdapter(Protocol):
    """Closed three-operation protocol. Attic never authorises anything —
    every result is a candidate for the caller to reconcile against the
    catalogue."""

    def store(
        self, evidence_id: UUID, payload: bytes
    ) -> PayloadStored | PayloadCorrupt: ...

    def fetch(
        self, evidence_id: UUID
    ) -> FetchedPayload | PayloadAbsent | PayloadCorrupt: ...

    def search(self, query: str, limit: int) -> tuple[UUID, ...]: ...
