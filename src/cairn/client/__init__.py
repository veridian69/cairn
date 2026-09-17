"""Provider-neutral automatic client for Cairn's versioned memory surface."""

from cairn.client.briefing import (
    ArrivalBriefing,
    ArrivalFailure,
    CurrentFactSummary,
    EvidenceReference,
    build_arrival_briefing,
)
from cairn.client.durable_session import DurableMemorySession
from cairn.client.errors import (
    FailureMetadata,
    MemoryOperationFailure,
    PersistenceConflict,
    PersistenceFailure,
    RecallFailure,
    RememberFailure,
)
from cairn.client.memory import MemoryClient
from cairn.client.progress import PersistencePhase, PersistenceProgress
from cairn.client.session import MEMORY_REMEMBER_NAMESPACE, MemorySession
from cairn.client.session_types import (
    DurableArrival,
    DurableProgress,
    DurableSessionFailure,
    DurableStage,
    DurableTurnResult,
    SessionOperationResult,
    SessionSnapshot,
)
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DiagnosticPermissions,
    DurableObservation,
    FrozenJSON,
    FrozenJSONObject,
    ModelCallback,
    ModelTurn,
    PersistenceReceipt,
    PersistenceStatus,
    RecalledMemory,
    TurnInput,
    TurnResult,
)

__all__ = [
    "MEMORY_REMEMBER_NAMESPACE",
    "ArrivalBriefing",
    "ArrivalFailure",
    "ConnectionDiagnostics",
    "ConnectionStatus",
    "CurrentFactSummary",
    "DiagnosticPermissions",
    "DurableObservation",
    "DurableMemorySession",
    "DurableArrival",
    "DurableProgress",
    "DurableSessionFailure",
    "DurableStage",
    "DurableTurnResult",
    "SessionOperationResult",
    "SessionSnapshot",
    "EvidenceReference",
    "FailureMetadata",
    "FrozenJSON",
    "FrozenJSONObject",
    "MemoryClient",
    "MemoryOperationFailure",
    "MemorySession",
    "ModelCallback",
    "ModelTurn",
    "PersistenceConflict",
    "PersistenceFailure",
    "PersistencePhase",
    "PersistenceProgress",
    "PersistenceReceipt",
    "PersistenceStatus",
    "RecallFailure",
    "RecalledMemory",
    "RememberFailure",
    "TurnInput",
    "TurnResult",
    "build_arrival_briefing",
]
