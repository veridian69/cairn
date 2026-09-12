"""Cairn Attic evidence custody: the adapter protocol, its real SQLite-backed
implementation, outbox delivery and catalogue reconciliation."""

from cairn.evidence.adapter import (
    AtticAdapter,
    FetchedPayload,
    PayloadAbsent,
    PayloadCorrupt,
    PayloadStored,
)
from cairn.evidence.attic import ATTIC_FILENAME, AtticStorageError, SqliteAttic
from cairn.evidence.delivery import DeliveryReport, deliver_evidence_outbox
from cairn.evidence.reconciliation import (
    DisclosedEvidence,
    ScopeDirection,
    reconcile_evidence,
)

__all__ = [
    "ATTIC_FILENAME",
    "AtticAdapter",
    "AtticStorageError",
    "DeliveryReport",
    "DisclosedEvidence",
    "FetchedPayload",
    "PayloadAbsent",
    "PayloadCorrupt",
    "PayloadStored",
    "ScopeDirection",
    "SqliteAttic",
    "deliver_evidence_outbox",
    "reconcile_evidence",
]
