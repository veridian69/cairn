"""Independent operation inventory: old v1 contracts never import this table."""

from cairn.runtime.logging import Operation
from cairn.transports.memory.models import (
    CorrectRequest,
    DiagnoseBody,
    DiagnoseRequest,
    DisagreeRequest,
    HistoryBody,
    HistoryRequest,
    RecallBody,
    RecallRequest,
    RelationshipResult,
    RememberRequest,
    ResolveRequest,
)
from cairn.transports.memory.proposal_models import (
    AcceptProposalRequest,
    ListProposalsRequest,
    ProposalPageBody,
    ProposalRecordedBody,
    ProposalSnapshotBody,
    ProposeRequest,
    ReadProposalRequest,
    RejectProposalRequest,
)
from cairn.transports.memory.session_models import (
    AbandonTurnRequest,
    AcknowledgeVisitRequest,
    BeginTurnRequest,
    OpenSessionRequest,
    PrepareTurnRequest,
    ReadSessionRequest,
    SessionRequest,
    SessionSnapshotBody,
    TurnRequest,
)
from cairn.transports.memory.suggestion_models import (
    SuggestionResultBody,
    SuggestRequest,
)
from cairn.transports.v1.operations import OperationEntry
from cairn.transports.v1.responses import IngestResult, InvalidateResult, PromoteResult
from cairn.transports.v1.wire import SuccessEnvelope

OPERATIONS: tuple[OperationEntry, ...] = (
    OperationEntry(
        Operation.MEMORY_SUGGEST,
        "suggest",
        "post",
        "/memory/v1/suggest",
        "Read bounded attributed suggestions; no persistence or automatic follow-up action. Omissions and empty results do not prove absence of duplicates.",
        SuggestRequest,
        SuggestionResultBody,
        SuggestionResultBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_DIAGNOSE,
        "diagnose",
        "post",
        "/memory/v1/diagnose",
        "Inspect authenticated identity, memory contracts and current grants for exact scope/classification; not a promise of operation success.",
        DiagnoseRequest,
        DiagnoseBody,
        DiagnoseBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_REMEMBER,
        "remember",
        "post",
        "/memory/v1/remember",
        (
            "Save authenticated observations as candidate agent claims. Keep "
            "independently changeable details separate: changing capacity must "
            "not withdraw the schedule, venue or unfinished work. Prefer one "
            "fact per call when referring to individual facts later. For a "
            "conversation save, supply its relevant source excerpt in "
            "evidence_payload; never invent evidence or imply automatic "
            "transcript capture. A committed/replayed outcome with "
            "mutation_receipt and audit_receipt confirms candidate custody; "
            "there is no separate custody_receipt field. evidence_id concerns "
            "evidence, not fact custody. This does not confirm searchability. "
            "Returned fact_ids are an unordered set, NOT aligned with input "
            "order. Call history for the saved IDs and match each returned "
            "fact_id with its body before reporting labels or using an ID in "
            "a correction. If read-back is missing, report the mapping as "
            "unverified; never guess or write again to obtain another ID."
        ),
        RememberRequest,
        IngestResult,
        SuccessEnvelope[IngestResult],
        True,
    ),
    OperationEntry(
        Operation.MEMORY_RECALL,
        "recall",
        "post",
        "/memory/v1/recall",
        "Recall authorised, attributed memories with deterministic fading.",
        RecallRequest,
        RecallBody,
        RecallBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_HISTORY,
        "history",
        "post",
        "/memory/v1/history",
        (
            "Read bounded, currently authorised facts and correction history. "
            "Use returned fact objects to verify exact fact_id/body mappings "
            "after remember and before correct; do not infer mappings from "
            "array order. After a replacement, read the OLD fact's history "
            "and verify its corrections entry explicitly names the new "
            "superseded_by ID before claiming a replacement link. Preserve "
            "omissions and candidate attribution; a missing or partial result "
            "does not establish that no history exists."
        ),
        HistoryRequest,
        HistoryBody,
        HistoryBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_DISAGREE,
        "disagree",
        "post",
        "/memory/v1/disagree",
        "Record an attributed disagreement without changing either belief.",
        DisagreeRequest,
        RelationshipResult,
        SuccessEnvelope[RelationshipResult],
        True,
    ),
    OperationEntry(
        Operation.MEMORY_RESOLVE,
        "resolve",
        "post",
        "/memory/v1/resolve",
        "Record an evidence-backed judgement with promotion authority.",
        ResolveRequest,
        RelationshipResult,
        SuccessEnvelope[RelationshipResult],
        True,
    ),
    OperationEntry(
        Operation.MEMORY_CORRECT,
        "correct",
        "post",
        "/memory/v1/correct",
        (
            "Invalidate whole facts with current authority, retaining history. "
            "This does NOT edit selected words or create a replacement. For "
            "a correction with a replacement: read the old fact's exact body; "
            "remember the replacement with source evidence; verify the new "
            "ID/body by history; then set superseded_by to that verified new "
            "fact ID here. If the old fact is compound, preserve ALL still-valid "
            "details in the replacement or separately committed facts BEFORE "
            "invalidating it. Never drop date, time or venue when changing "
            "only capacity. Omitting superseded_by or setting null means "
            "withdrawal WITHOUT a replacement link; use that only for an "
            "intentional withdrawal. Finally read the old fact's history to "
            "verify the link. Report incomplete steps honestly; do not retry "
            "an uncertain mutation under a new key."
        ),
        CorrectRequest,
        InvalidateResult,
        SuccessEnvelope[InvalidateResult],
        True,
    ),
)
OPERATIONS += tuple(
    OperationEntry(
        operation,
        name,
        "post",
        f"/memory/v1/{name}",
        description,
        request,
        SessionSnapshotBody,
        SuccessEnvelope[SessionSnapshotBody] if mutation else SessionSnapshotBody,
        mutation,
    )
    for operation, name, request, mutation, description in (
        (
            Operation.MEMORY_SESSION_OPEN,
            "session-open",
            OpenSessionRequest,
            True,
            "Open an owner-private session with fixed scope and classification.",
        ),
        (
            Operation.MEMORY_TURN_BEGIN,
            "turn-begin",
            BeginTurnRequest,
            True,
            "Claim a turn attempt. Only a newly committed begin permits generation; replay does not.",
        ),
        (
            Operation.MEMORY_TURN_PREPARE,
            "turn-prepare",
            PrepareTurnRequest,
            True,
            "Prepare bounded immutable output; this is not fact custody.",
        ),
        (
            Operation.MEMORY_TURN_COMMIT,
            "turn-commit",
            TurnRequest,
            True,
            "Reconcile prepared observations with actual normal candidate custody.",
        ),
        (
            Operation.MEMORY_TURN_ABANDON,
            "turn-abandon",
            AbandonTurnRequest,
            True,
            "Explicitly fence an unprepared interrupted turn.",
        ),
        (
            Operation.MEMORY_SESSION_READ,
            "session-read",
            ReadSessionRequest,
            False,
            "Read owner-private session or turn state under current authority.",
        ),
        (
            Operation.MEMORY_VISIT_ISSUE,
            "visit-issue",
            SessionRequest,
            True,
            "Issue a selected-memory visit boundary before briefing reads.",
        ),
        (
            Operation.MEMORY_VISIT_ACKNOWLEDGE,
            "visit-acknowledge",
            AcknowledgeVisitRequest,
            True,
            "Explicitly acknowledge a consumed visit; never move progress backwards.",
        ),
    )
)
OPERATIONS += (
    OperationEntry(
        Operation.MEMORY_PROPOSE,
        "propose",
        "post",
        "/memory/v1/propose",
        "Record a source-scoped proposal; does not publish facts.",
        ProposeRequest,
        ProposalRecordedBody,
        SuccessEnvelope[ProposalRecordedBody],
        True,
    ),
    OperationEntry(
        Operation.MEMORY_PROPOSAL_LIST,
        "proposal-list",
        "post",
        "/memory/v1/proposal-list",
        "List currently readable proposals at one exact source. Pagination is not a snapshot.",
        ListProposalsRequest,
        ProposalPageBody,
        ProposalPageBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_PROPOSAL_READ,
        "proposal-read",
        "post",
        "/memory/v1/proposal-read",
        "Read a source-scoped proposal. Null references mean absent or unreadable.",
        ReadProposalRequest,
        ProposalSnapshotBody,
        ProposalSnapshotBody,
        False,
    ),
    OperationEntry(
        Operation.MEMORY_PROPOSAL_ACCEPT,
        "proposal-accept",
        "post",
        "/memory/v1/proposal-accept",
        "Explicitly publish through normal promotion and atomically record acceptance.",
        AcceptProposalRequest,
        PromoteResult,
        SuccessEnvelope[PromoteResult],
        True,
    ),
    OperationEntry(
        Operation.MEMORY_PROPOSAL_REJECT,
        "proposal-reject",
        "post",
        "/memory/v1/proposal-reject",
        "Record rejection without invalidating the source.",
        RejectProposalRequest,
        ProposalRecordedBody,
        SuccessEnvelope[ProposalRecordedBody],
        True,
    ),
)
PROPOSAL_TOOL_NAMES = frozenset(
    {"propose", "proposal-list", "proposal-read", "proposal-accept", "proposal-reject"}
)
TOOL_NAMES = frozenset(entry.tool for entry in OPERATIONS)
SESSION_TOOL_NAMES = frozenset(
    entry.tool for entry in OPERATIONS if entry.result is SessionSnapshotBody
)
BY_TOOL = {entry.tool: entry for entry in OPERATIONS}
