"""Pure model and controller for ``cairn-memory browse``.

Nothing here touches curses, the network or a clock. An injected
``BrowseBackend`` performs bounded reads and the explicit writes (promotion,
correct, disagree and proposal-reject, each after a reason and y; the session
commands arrive, acknowledge-visit, remember, resume and abandon, each after
y on a confirm screen; status is the only session read), and
``render`` returns already sanitised, width-clipped lines for a thin terminal
adapter. Memory text is untrusted data: every drawn line passes through
``sanitise``, which replaces control, format and separator characters with
visible escapes, so stored text can never drive the terminal.
"""

from __future__ import annotations

import io
import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from cairn.authority.custody import CustodyValueError, validate_reason
from cairn.catalogue.audit import Scope
from cairn.client.cli_input import Input, command_key, parse, preparation_size
from cairn.client.command_io import CommandInputError
from cairn.client.profiles import MemoryProfile
from cairn.client.proposal_types import ProposalPage, ProposalSnapshot
from cairn.client.session_types import DurableArrival, SessionSnapshot
from cairn.client.suggestion_types import SuggestedMemory
from cairn.client.types import ConnectionDiagnostics

type Fact = Mapping[str, object]
type Style = Literal["normal", "selected", "header", "status", "dim"]
type Mode = Literal[
    "list",
    "detail",
    "evidence",
    "input",
    "confirm",
    "outcome",
    "proposals",
    "proposal",
    "suggestions",
    "connection",
    "session",
]
type InputKind = Literal["search", "reason", "query", "observation", "response"]

MAX_QUERY_BYTES = 8192
MAX_REASON_BYTES = 4096
# remember's own limits (cli_input.completed_turn): 0..8 observations with a
# body of 1..4096 bytes each, and a response of 0..32768 bytes.
MAX_OBSERVATIONS = 8
MAX_BODY_BYTES = 4096
MAX_RESPONSE_BYTES = 32768
_INPUT_LIMITS: dict[str, int] = {
    "search": MAX_QUERY_BYTES,
    "reason": MAX_REASON_BYTES,
    "query": MAX_QUERY_BYTES,
    "observation": MAX_BODY_BYTES,
    "response": MAX_RESPONSE_BYTES,
}
# Bounded automatic continuation per keypress when a client-side trust filter
# hides most of a page; the snapshot itself is bounded by the server.
MAX_AUTO_PAGES = 10
FILTERS: tuple[str, ...] = ("all", "candidate", "validated")
_MARKERS = {"candidate": "[C]", "validated": "[V]", "failed-approach": "[F]"}
_ESCAPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def sanitise(text: str) -> str:
    """Replace every non-printing character with a visible, inert escape."""
    output: list[str] = []
    for character in text:
        if character == "\t":
            output.append(" ")
        elif unicodedata.category(character) in _ESCAPED:
            code = ord(character)
            output.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}")
        else:
            output.append(character)
    return "".join(output)


def cell_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def clip(text: str, width: int) -> str:
    used, output = 0, []
    for character in text:
        size = cell_width(character)
        if used + size > width:
            break
        output.append(character)
        used += size
    return "".join(output)


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap by terminal cells; overlong words are hard-broken."""
    width = max(1, width)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current, used = "", 0
        for word in sanitise(paragraph).split(" "):
            size = sum(cell_width(c) for c in word)
            gap = 1 if current else 0
            if used + gap + size <= width:
                current, used = current + (" " if gap else "") + word, used + gap + size
                continue
            if current:
                lines.append(current)
            current, used = "", 0
            while size > width:
                head = clip(word, width) or word[0]
                lines.append(head)
                word = word[len(head) :]
                size = sum(cell_width(c) for c in word)
            current, used = word, size
        lines.append(current)
    return lines


def scope_key(value: object) -> tuple[str, tuple[tuple[str, str], ...]]:
    if isinstance(value, Scope):
        return value.realm, tuple((s.kind, s.identifier) for s in value.segments)
    document = cast(Mapping[str, object], value)
    segments = cast(
        tuple[Mapping[str, str], ...] | list[Mapping[str, str]], document["segments"]
    )
    return cast(str, document["realm"]), tuple(
        (s["kind"], s["identifier"]) for s in segments
    )


def scope_label(value: object) -> str:
    realm, segments = scope_key(value)
    return "/".join([realm, *(f"{kind}:{identifier}" for kind, identifier in segments)])


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    style: Style = "normal"


@dataclass(frozen=True, slots=True)
class Page:
    hits: tuple[Fact, ...]
    next_cursor: str | None
    selection_complete: bool


class BrowseFailure(Exception):
    """Closed operation/code vocabulary only; never server prose or content."""

    def __init__(self, operation: str, code: str, reason: str | None = None) -> None:
        super().__init__(f"{operation}: {code}")
        self.operation, self.code, self.reason = operation, code, reason


@dataclass(frozen=True, slots=True)
class PromotionAttempt:
    """One explicit promotion; its identities are fixed for every resubmission."""

    source_fact_id: UUID
    evidence_id: UUID
    classification: str
    reason: str
    proposal_id: UUID
    propose_key: UUID
    accept_key: UUID


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """promoted: FactsPromoted receipt; refused: confirmed refusal (exit 2 class);
    failed: the named request was not dispatched (exit 4 class); uncertain: a
    dispatched mutation whose effect is unconfirmed (exit 3 class)."""

    status: Literal["promoted", "refused", "failed", "uncertain"]
    operation: str
    code: str | None = None
    derived_fact_id: UUID | None = None
    receipt_outcome: str | None = None
    proposal_recorded: bool = False


@dataclass(frozen=True, slots=True)
class CorrectionAttempt:
    """Invalidate one fact, optionally superseded by another; the idempotency
    key is fixed when the attempt is created and reused for every retry."""

    fact_id: UUID
    fact_line: str
    superseded_by: UUID | None
    superseded_line: str | None
    reason: str
    key: UUID
    operation: Literal["correct"] = "correct"


@dataclass(frozen=True, slots=True)
class DisagreementAttempt:
    """Record a disagreement between two distinct facts; fixed key."""

    left_fact_id: UUID
    left_line: str
    right_fact_id: UUID
    right_line: str
    reason: str
    key: UUID
    operation: Literal["disagree"] = "disagree"


@dataclass(frozen=True, slots=True)
class RejectionAttempt:
    """Reject one pending proposal, re-read just before confirmation; fixed key."""

    proposal_id: UUID
    proposal_line: str
    source_fact_id: UUID
    target_scope: Scope
    reason: str
    key: UUID
    operation: Literal["proposal-reject"] = "proposal-reject"


@dataclass(frozen=True, slots=True)
class ArrivalAttempt:
    """Open/replay the profile's session, issue a visit, read a briefing. The
    durable client draws a fresh visit key for every arrival, so there is no
    exact resubmission; an arrival never acknowledges its visit. ``key`` is a
    local identity only and is never sent."""

    query: str
    key: UUID
    operation: Literal["arrive"] = "arrive"


@dataclass(frozen=True, slots=True)
class AcknowledgementAttempt:
    """Acknowledge one visit; the durable client derives the key from it."""

    visit_id: UUID
    source: str
    operation: Literal["acknowledge-visit"] = "acknowledge-visit"

    @property
    def key(self) -> UUID:
        return self.visit_id


@dataclass(frozen=True, slots=True)
class RememberAttempt:
    """One completed checkpoint. turn_id and attempt_id are generated once,
    when the attempt is created, and reused for every exact resubmission."""

    turn_id: UUID
    attempt_id: UUID
    response: str
    observations: tuple[str, ...]
    operation: Literal["remember"] = "remember"

    @property
    def key(self) -> UUID:
        return self.turn_id


@dataclass(frozen=True, slots=True)
class ResumeAttempt:
    """Resume one turn: status, then commit if prepared; never regenerates."""

    turn_id: UUID
    source: str
    operation: Literal["resume"] = "resume"

    @property
    def key(self) -> UUID:
        return self.turn_id


@dataclass(frozen=True, slots=True)
class AbandonAttempt:
    """Abandon one unprepared started turn, with a reason."""

    turn_id: UUID
    source: str
    reason: str
    operation: Literal["abandon"] = "abandon"

    @property
    def key(self) -> UUID:
        return self.turn_id


type SessionAttempt = (
    ArrivalAttempt
    | AcknowledgementAttempt
    | RememberAttempt
    | ResumeAttempt
    | AbandonAttempt
)
SESSION_ATTEMPTS = (
    ArrivalAttempt,
    AcknowledgementAttempt,
    RememberAttempt,
    ResumeAttempt,
    AbandonAttempt,
)
type WriteAttempt = (
    CorrectionAttempt | DisagreementAttempt | RejectionAttempt | SessionAttempt
)


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """recorded: a receipt for this exact request; refused: confirmed refusal
    (exit 2 class), nothing changed; failed: the request was not dispatched
    (exit 4 class); uncertain: possibly dispatched, effect unknown (exit 3).

    Session commands add the CLI's own turn states: saved (state committed
    with a committed custody receipt, the only saved claim), skipped and
    abandoned (exit 0 and 2, known), prepared and interrupted (exit 3, not
    saved). For session commands ``failed`` means not completed: the
    request may have been sent, but it changes no facts and needs no
    recovery. ``recovery`` is the CLI's recovery value when it gives one."""

    status: Literal[
        "recorded",
        "refused",
        "failed",
        "uncertain",
        "saved",
        "skipped",
        "abandoned",
        "prepared",
        "interrupted",
    ]
    operation: str
    code: str | None = None
    receipt_outcome: str | None = None
    mutation_id: str | None = None
    audit_event_id: str | None = None
    audit_recorded_at: str | None = None
    invalidated_at: str | None = None
    relationship_id: str | None = None
    stage: str | None = None
    recovery: str | None = None
    fact_ids: tuple[str, ...] = ()
    custody_key: str | None = None
    visit_id: str | None = None
    arrival: DurableArrival | None = None
    snapshot: SessionSnapshot | None = None


# Outcomes that settle an attempt, and those that leave it to recover (the
# CLI's exit 3 class).
_SETTLED = frozenset({"recorded", "saved", "skipped", "abandoned"})
_OPEN = frozenset({"uncertain", "prepared", "interrupted"})


def session_input(command: str, document: dict[str, object]) -> Input | None:
    """The everyday CLI's own stdin validation for a session command."""
    try:
        return parse(command, io.BytesIO(json.dumps(document).encode()))
    except (CommandInputError, ValueError):
        return None


def remember_document(attempt: RememberAttempt) -> dict[str, object]:
    """The exact stdin JSON ``cairn-memory remember`` takes for this attempt."""
    return {
        "turn_id": str(attempt.turn_id),
        "attempt_id": str(attempt.attempt_id),
        "response": attempt.response,
        "observations": [{"body": body} for body in attempt.observations],
    }


# What each write is called in messages, prompts and the busy line.
_WRITE_NAMES = {
    "correct": "correction",
    "disagree": "disagreement",
    "proposal-reject": "rejection",
    "arrive": "arrival",
    "acknowledge-visit": "acknowledgement",
    "remember": "checkpoint",
    "resume": "resume",
    "abandon": "abandonment",
}
_WRITE_DONE = {
    "correct": "corrected",
    "disagree": "recorded",
    "proposal-reject": "rejected",
    "arrive": "briefed",
    "acknowledge-visit": "acknowledged",
    "remember": "saved",
    "resume": "saved",
    "abandon": "abandoned",
}


class BrowseBackend(Protocol):
    async def page(self, query: str | None, cursor: str | None) -> Page: ...

    async def history(self, fact_id: UUID) -> Fact: ...

    async def evidence(self, evidence_id: UUID, start: int) -> Fact: ...

    async def promote(self, attempt: PromotionAttempt) -> PromotionOutcome: ...

    async def write(self, attempt: WriteAttempt) -> WriteOutcome: ...

    async def proposals(self, after: UUID | None) -> ProposalPage: ...

    async def proposal(self, proposal_id: UUID) -> ProposalSnapshot: ...

    async def suggest(self, fact_id: UUID) -> SuggestedMemory: ...

    async def diagnose(self) -> ConnectionDiagnostics: ...

    async def session_status(self, turn_id: UUID | None) -> SessionSnapshot: ...


def _text(fact: Fact, name: str) -> str:
    value = fact.get(name)
    return "-" if value is None else str(value)


def _optional(value: object) -> str:
    return "-" if value is None else str(value)


def _first_line(body: object) -> str:
    text = body if type(body) is str else ""
    return text.split("\n", 1)[0]


def _wrapped(lines: list[Line], width: int) -> list[Line]:
    output: list[Line] = []
    for line in lines:
        text = sanitise(line.text)
        if sum(cell_width(c) for c in text) <= width:
            output.append(Line(text, line.style))
        else:
            output.extend(Line(part, line.style) for part in wrap(text, width))
    return output


def _fit(parts: list[str], optional: tuple[str, ...], width: int) -> str:
    """Join footer parts, leaving out optional ones in order until they fit."""
    parts = list(parts)
    for part in optional:
        if len("  ".join(parts)) <= width:
            break
        if part in parts:
            parts.remove(part)
    return "  ".join(parts)


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _uuid4(value: object) -> UUID | None:
    """The canonical UUIDv4 the CLI requires for a fact ID, else None."""
    try:
        identity = UUID(str(value))
    except ValueError:
        return None
    return identity if identity.version == 4 and str(identity) == value else None


class BrowseController:
    """Terminal-independent state, key handling, paging and line rendering."""

    def __init__(
        self,
        backend: BrowseBackend,
        *,
        scope: Scope,
        may_promote: bool,
        connection: ConnectionDiagnostics | None = None,
        identities: Callable[[], UUID] = uuid4,
        profile: MemoryProfile | None = None,
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.may_promote = may_promote
        # The session commands need the profile's session_id, as in the CLI.
        self.profile = profile
        self.session_id = None if profile is None else profile.session_id
        # The latest successful diagnosis; None means grants are not known here.
        self.connection = connection
        self.connection_error: str | None = None
        self._identities = identities
        self.on_busy: Callable[[], None] | None = None
        self.height, self.width = 24, 80
        self.done = False
        self.mode: Mode = "list"
        self.message = ""
        self.rows: list[Fact] = []
        self.cursor: str | None = None
        self.selection_complete = True
        self.query: str | None = None
        self.filter = "all"
        self.selected = 0
        self.top = 0
        self.fact: Fact | None = None
        self.history: Fact | None = None
        self.history_error: str | None = None
        self.scroll = 0
        self.window: Fact | None = None
        self.window_starts: list[int] = []
        self.input_kind: InputKind | None = None
        self.input_text = ""
        self.input_return: Mode = "list"
        self.attempt: PromotionAttempt | None = None
        self.attempt_return: Mode = "list"
        self.outcome: PromotionOutcome | None = None
        self.unresolved: dict[UUID, tuple[PromotionAttempt, PromotionOutcome]] = {}
        # correct, disagree and proposal-reject share the confirm and outcome
        # screens with promotion; only one attempt of either kind is active.
        self.reason_for: Literal[
            "promote", "correct", "disagree", "proposal-reject", "abandon"
        ]
        self.reason_for = "promote"
        self.marked: Fact | None = None
        self.draft_other: Fact | None = None
        self.draft_proposal: ProposalSnapshot | None = None
        self.write: WriteAttempt | None = None
        self.write_outcome: WriteOutcome | None = None
        self.unresolved_writes: dict[UUID, tuple[WriteAttempt, WriteOutcome]] = {}
        # Read-only proposal view; its own selection leaves the fact list intact.
        self.proposal_rows: list[ProposalSnapshot] = []
        self.proposal_cursor: UUID | None = None
        self.proposal_error: str | None = None
        self.proposal_incomplete = False
        self.proposal_selected = 0
        self.proposal_top = 0
        self.proposal: ProposalSnapshot | None = None
        # Read-only suggestions for one fact; never executed or persisted.
        self.suggest_fact: Fact | None = None
        self.suggestions: SuggestedMemory | None = None
        self.suggestions_error: str | None = None
        self.suggest_return: Mode = "list"
        # Session view: the last status read, the visit this browser's own
        # arrival issued (never acknowledged automatically), the turn of its
        # latest checkpoint, and the observations of a checkpoint being typed.
        self.session_status: SessionSnapshot | None = None
        self.session_error: str | None = None
        self.issued_visit: UUID | None = None
        self.session_turn: UUID | None = None
        self.draft_observations: list[str] = []
        self.draft_turn: tuple[UUID, str] | None = None

    # Model helpers -------------------------------------------------------

    @property
    def may_retrieve(self) -> bool:
        """Reads are offered unless a known diagnosis reports no retrieve grant;
        the server still checks every request."""
        permissions = None if self.connection is None else self.connection.permissions
        return permissions is None or permissions.retrieve

    # Writes, like promotion, are offered only when a known diagnosis reports
    # the grant; Cairn still checks every request again.

    @property
    def may_correct(self) -> bool:
        """correct needs invalidate at the fact's scope, which the browser
        limits to the profile's exact scope (where check evaluates it)."""
        permissions = None if self.connection is None else self.connection.permissions
        return permissions is not None and permissions.invalidate

    @property
    def may_disagree(self) -> bool:
        """disagree needs retrieve and ingest at the exact profile scope."""
        permissions = None if self.connection is None else self.connection.permissions
        return permissions is not None and permissions.retrieve and permissions.ingest

    @property
    def may_reject(self) -> bool:
        """proposal-reject needs retrieve (the client's own preflight). Cairn
        also checks promote at the proposal's target scope for the source's
        classification, which check does not evaluate; that is left to Cairn."""
        permissions = None if self.connection is None else self.connection.permissions
        return permissions is not None and permissions.retrieve

    @property
    def may_session_write(self) -> bool:
        """arrive, acknowledge-visit, remember, resume and abandon need a
        covering retrieve grant and an ingest grant for the profile
        classification at the exact scope (authority/sessions.py, _access);
        status needs retrieve only, so it follows may_retrieve."""
        permissions = None if self.connection is None else self.connection.permissions
        return (
            self.session_id is not None
            and permissions is not None
            and permissions.retrieve
            and permissions.ingest
        )

    @property
    def view(self) -> list[Fact]:
        if self.filter == "all":
            return self.rows
        return [row for row in self.rows if row.get("trust") == self.filter]

    @property
    def pending(self) -> list[ProposalSnapshot]:
        """proposal-list returns every state; the view shows pending only."""
        return [p for p in self.proposal_rows if p.state == "pending"]

    @property
    def body_height(self) -> int:
        return max(1, self.height - 3)

    def resize(self, height: int, width: int) -> None:
        self.height, self.width = max(4, height), max(20, width)

    def _busy(self, text: str) -> None:
        self.message = text
        if self.on_busy is not None:
            self.on_busy()

    def _current(self) -> Fact | None:
        if self.mode in {"detail", "evidence"} or self.input_kind == "reason":
            return self.fact
        view = self.view
        return view[self.selected] if 0 <= self.selected < len(view) else None

    # Loading -------------------------------------------------------------

    async def start(self) -> None:
        await self.reload()

    async def reload(self) -> None:
        self.rows, self.cursor, self.selected, self.top = [], None, 0, 0
        self.selection_complete = True
        self._busy("Loading...")
        self.message = ""
        try:
            page = await self.backend.page(self.query, None)
        except BrowseFailure as failure:
            self.message = f"recall-page failed: {failure.code}"
            return
        self._accept(page)
        await self._ensure(0)

    def _accept(self, page: Page) -> None:
        self.rows.extend(page.hits)
        self.cursor = page.next_cursor
        self.selection_complete = page.selection_complete

    async def _ensure(self, index: int, pages: int = MAX_AUTO_PAGES) -> None:
        fetched = 0
        while len(self.view) <= index and self.cursor is not None and fetched < pages:
            fetched += 1
            self._busy("Loading more...")
            self.message = ""
            try:
                page = await self.backend.page(None, self.cursor)
            except BrowseFailure as failure:
                self.cursor = None
                self.message = (
                    "listing snapshot expired; press r to start again"
                    if failure.reason == "continuation_unavailable"
                    else f"recall-page failed: {failure.code}; press r to start again"
                )
                return
            self._accept(page)

    async def reload_proposals(self) -> None:
        self.proposal_rows, self.proposal_cursor = [], None
        self.proposal_selected, self.proposal_top = 0, 0
        self.proposal_error, self.proposal_incomplete = None, False
        self._busy("Loading proposals...")
        self.message = ""
        try:
            page = await self.backend.proposals(None)
        except BrowseFailure as failure:
            self.proposal_error = failure.code
            self.message = f"proposal-list failed: {failure.code}"
            return
        self._accept_proposals(page)
        await self._ensure_proposals(0)

    def _accept_proposals(self, page: ProposalPage) -> None:
        self.proposal_rows.extend(page.items)
        self.proposal_cursor = page.next_cursor

    async def _ensure_proposals(self, index: int, pages: int = MAX_AUTO_PAGES) -> None:
        fetched = 0
        while (
            len(self.pending) <= index
            and self.proposal_cursor is not None
            and fetched < pages
        ):
            fetched += 1
            self._busy("Loading more proposals...")
            self.message = ""
            try:
                page = await self.backend.proposals(self.proposal_cursor)
            except BrowseFailure as failure:
                self.proposal_cursor, self.proposal_incomplete = None, True
                self.message = (
                    f"proposal-list failed: {failure.code}; press r to start again"
                )
                return
            self._accept_proposals(page)

    # Keys ----------------------------------------------------------------

    async def handle(self, key: str) -> None:
        if key == "quit":
            self.done = True
            return
        if self.mode == "input":
            await self._input_key(key)
        elif self.mode == "confirm":
            await self._confirm_key(key)
        elif self.mode == "outcome":
            await self._outcome_key(key)
        elif self.mode == "detail":
            await self._detail_key(key)
        elif self.mode == "evidence":
            await self._evidence_key(key)
        elif self.mode == "proposals":
            await self._proposals_key(key)
        elif self.mode == "proposal":
            self._proposal_key(key)
        elif self.mode == "suggestions":
            self._suggestions_key(key)
        elif self.mode == "connection":
            await self._connection_key(key)
        elif self.mode == "session":
            await self._session_key(key)
        else:
            await self._list_key(key)

    async def _list_key(self, key: str) -> None:
        self.message = ""
        if key in {"down", "j"}:
            await self._ensure(self.selected + 1)
            self.selected = min(self.selected + 1, max(0, len(self.view) - 1))
        elif key in {"up", "k"}:
            self.selected = max(0, self.selected - 1)
        elif key == "pgdn":
            await self._ensure(self.selected + self.body_height)
            self.selected = min(
                self.selected + self.body_height, max(0, len(self.view) - 1)
            )
        elif key == "pgup":
            self.selected = max(0, self.selected - self.body_height)
        elif key in {"g", "home"}:
            self.selected = 0
        elif key in {"G", "end"}:
            await self._ensure(len(self.rows) + 10**6, pages=MAX_AUTO_PAGES * 2)
            self.selected = max(0, len(self.view) - 1)
            if self.cursor is not None:
                self.message = "more facts remain; press G again to load further"
        elif key == "enter":
            fact = self._current()
            if fact is not None:
                await self._open_detail(fact)
        elif key == "/":
            self._start_input("search", self.query or "")
        elif key == "f":
            current = self._current()
            self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
            view = self.view
            self.selected = next((i for i, row in enumerate(view) if row is current), 0)
            await self._ensure(self.selected)
        elif key == "r":
            await self.reload()
        elif key == "p":
            self._begin_promotion(self._current())
        elif key in {"m", "x", "X", "d"}:
            self._fact_write_key(key, self._current())
        elif key == "P":
            if self.may_retrieve:
                self.mode = "proposals"
                await self.reload_proposals()
            else:
                self.message = "proposals not permitted for this profile"
        elif key == "s":
            await self._open_suggestions(self._current())
        elif key == "c":
            self.mode, self.scroll = "connection", 0
            await self._read_connection()
        elif key == "S":
            await self._open_session()
        elif key == "q":
            self.done = True
        self._follow()

    def _follow(self) -> None:
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + self.body_height:
            self.top = self.selected - self.body_height + 1

    async def _open_detail(self, fact: Fact) -> None:
        self.mode, self.fact, self.scroll = "detail", fact, 0
        self.history, self.history_error = None, None
        self._busy("Loading history...")
        self.message = ""
        try:
            self.history = await self.backend.history(UUID(str(fact["fact_id"])))
        except BrowseFailure as failure:
            self.history_error = failure.code

    def _scroll_key(self, key: str, total: int) -> bool:
        limit = max(0, total - self.body_height)
        moves = {
            "down": 1,
            "j": 1,
            "up": -1,
            "k": -1,
            "pgdn": self.body_height,
            " ": self.body_height,
            "pgup": -self.body_height,
        }
        if key in moves:
            self.scroll = min(limit, max(0, self.scroll + moves[key]))
        elif key in {"g", "home"}:
            self.scroll = 0
        elif key in {"G", "end"}:
            self.scroll = limit
        else:
            return False
        return True

    async def _detail_key(self, key: str) -> None:
        self.message = ""
        if self._scroll_key(key, len(self._detail_lines())):
            return
        assert self.fact is not None
        if key in {"esc", "backspace", "q"}:
            self.mode, self.fact = "list", None
        elif key == "e":
            evidence = self.fact.get("source_evidence_id")
            if evidence is None:
                self.message = "this fact has no source evidence"
            else:
                self.window_starts = []
                await self._open_window(UUID(str(evidence)), 0)
        elif key == "p":
            self._begin_promotion(self.fact)
        elif key in {"m", "x", "X", "d"}:
            self._fact_write_key(key, self.fact)
        elif key == "s":
            await self._open_suggestions(self.fact)

    async def _open_window(self, evidence_id: UUID, start: int) -> bool:
        self._busy("Loading evidence...")
        self.message = ""
        try:
            window = await self.backend.evidence(evidence_id, start)
        except BrowseFailure as failure:
            self.message = f"evidence-window failed: {failure.code}"
            return False
        self.mode, self.window, self.scroll = "evidence", window, 0
        self.window_starts.append(start)
        return True

    async def _evidence_key(self, key: str) -> None:
        self.message = ""
        assert self.window is not None
        if key in {"esc", "backspace", "q"}:
            self.mode, self.window, self.scroll = "detail", None, 0
            return
        evidence_id = UUID(str(self.window["evidence_id"]))
        if key == "n":
            following = self.window.get("next_start_byte")
            if type(following) is int:
                await self._open_window(evidence_id, following)
            else:
                self.message = "end of evidence"
        elif key == "b":
            if len(self.window_starts) > 1:
                # Drop the current and previous offsets only once the previous
                # window has loaded; a failed read leaves the history intact.
                saved = list(self.window_starts)
                del self.window_starts[-2:]
                if not await self._open_window(evidence_id, saved[-2]):
                    self.window_starts[:] = saved
            else:
                self.message = "start of evidence"
        else:
            self._scroll_key(key, len(self._evidence_lines()))

    # Proposals (read-only) -----------------------------------------------

    async def _proposals_key(self, key: str) -> None:
        self.message = ""
        if key in {"down", "j"}:
            await self._ensure_proposals(self.proposal_selected + 1)
            self.proposal_selected = min(
                self.proposal_selected + 1, max(0, len(self.pending) - 1)
            )
        elif key in {"up", "k"}:
            self.proposal_selected = max(0, self.proposal_selected - 1)
        elif key == "pgdn":
            await self._ensure_proposals(self.proposal_selected + self.body_height)
            self.proposal_selected = min(
                self.proposal_selected + self.body_height,
                max(0, len(self.pending) - 1),
            )
        elif key == "pgup":
            self.proposal_selected = max(0, self.proposal_selected - self.body_height)
        elif key in {"g", "home"}:
            self.proposal_selected = 0
        elif key in {"G", "end"}:
            await self._ensure_proposals(
                len(self.proposal_rows) + 10**6, pages=MAX_AUTO_PAGES * 2
            )
            self.proposal_selected = max(0, len(self.pending) - 1)
            if self.proposal_cursor is not None:
                self.message = "more proposals remain; press G again to load further"
        elif key == "enter":
            pending = self.pending
            if 0 <= self.proposal_selected < len(pending):
                await self._open_proposal(pending[self.proposal_selected])
        elif key == "r":
            await self.reload_proposals()
        elif key == "x":
            pending = self.pending
            self._begin_rejection(
                pending[self.proposal_selected]
                if 0 <= self.proposal_selected < len(pending)
                else None
            )
        elif key in {"esc", "backspace", "q"}:
            self.mode = "list"
        self._follow_proposals()

    def _follow_proposals(self) -> None:
        if self.proposal_selected < self.proposal_top:
            self.proposal_top = self.proposal_selected
        elif self.proposal_selected >= self.proposal_top + self.body_height:
            self.proposal_top = self.proposal_selected - self.body_height + 1

    async def _open_proposal(self, listed: ProposalSnapshot) -> None:
        self._busy("Loading proposal...")
        self.message = ""
        try:
            current = await self.backend.proposal(listed.proposal_id)
        except BrowseFailure as failure:
            self.message = f"proposal-read failed: {failure.code}"
            return
        self.mode, self.proposal, self.scroll = "proposal", current, 0

    def _proposal_key(self, key: str) -> None:
        self.message = ""
        if self._scroll_key(key, len(self._proposal_lines())):
            return
        if key == "x":
            self._begin_rejection(self.proposal)
        elif key in {"esc", "backspace", "q"}:
            self.mode, self.proposal, self.scroll = "proposals", None, 0

    # Suggestions and connection (read-only) ------------------------------

    async def _open_suggestions(self, fact: Fact | None) -> None:
        if fact is None:
            return
        if not self.may_retrieve:
            self.message = "suggestions not permitted for this profile"
            return
        self.suggest_return = self.mode
        self.mode, self.suggest_fact, self.scroll = "suggestions", fact, 0
        self.suggestions, self.suggestions_error = None, None
        self._busy("Loading suggestions...")
        self.message = ""
        try:
            self.suggestions = await self.backend.suggest(UUID(str(fact["fact_id"])))
        except BrowseFailure as failure:
            self.suggestions_error = failure.code
            self.message = f"suggest failed: {failure.code}"

    def _suggestions_key(self, key: str) -> None:
        self.message = ""
        if self._scroll_key(key, len(self._suggestion_lines())):
            return
        if key in {"esc", "backspace", "q"}:
            self.mode, self.scroll = self.suggest_return, 0
            self.suggest_fact, self.suggestions = None, None
            self.suggestions_error = None

    async def _read_connection(self) -> None:
        """Re-read the diagnosis; a failure keeps the last result and its grants."""
        self._busy("Checking connection...")
        self.message = ""
        try:
            connection = await self.backend.diagnose()
        except BrowseFailure as failure:
            self.connection_error = failure.code
            self.message = f"diagnose failed: {failure.code}"
            return
        self.connection, self.connection_error = connection, None
        permissions = connection.permissions
        self.may_promote = (
            permissions is not None and permissions.ingest and permissions.promote
        )

    async def _connection_key(self, key: str) -> None:
        self.message = ""
        if self._scroll_key(key, len(self._connection_lines())):
            return
        if key == "r":
            await self._read_connection()
        elif key in {"esc", "backspace", "q"}:
            self.mode, self.scroll = "list", 0

    # Session commands ----------------------------------------------------

    async def _open_session(self) -> None:
        self.mode, self.scroll = "session", 0
        self.session_status, self.session_error = None, None
        if self.session_id is not None:
            await self._read_status()

    async def _read_status(self) -> None:
        """status, the only session command sent without confirmation."""
        if self.session_id is None:
            return
        if not self.may_retrieve:
            self.session_status, self.session_error = None, None
            self.message = "status not permitted for this profile"
            return
        self._busy("Reading session status...")
        self.message = ""
        try:
            self.session_status = await self.backend.session_status(self._own_turn())
        except BrowseFailure as failure:
            # A failed read never leaves an older status looking current.
            self.session_status, self.session_error = None, failure.code
            self.message = f"status failed: {failure.code}"
            return
        self.session_error = None

    def _own_turn(self) -> UUID | None:
        """status reads a turn only when given its turn_id; without one it
        reports the session. This browser asks for its own latest turn."""
        own = self._known_turn(own=True)
        return None if own is None else own[0]

    def _known_turn(self, *, own: bool = False) -> tuple[UUID, str] | None:
        """The turn u and b act on: this browser's own unresolved or latest
        checkpoint first, else the turn the last status read reported."""
        for write, _ in reversed(list(self.unresolved_writes.values())):
            if isinstance(write, (RememberAttempt, ResumeAttempt, AbandonAttempt)):
                return write.turn_id, "this browser's unresolved checkpoint"
        if self.session_turn is not None:
            return self.session_turn, "this browser's latest checkpoint"
        if own:
            return None
        status = self.session_status
        if status is not None and status.turn_id is not None:
            return status.turn_id, "reported by status"
        return None

    def _known_visit(self) -> tuple[UUID, str] | None:
        if self.issued_visit is not None:
            return self.issued_visit, "issued by this browser's arrival"
        status = self.session_status
        if status is not None and status.visit_id is not None:
            return status.visit_id, "reported by status"
        return None

    def _session_problem(self) -> str | None:
        if self.session_id is None:
            return "session commands need a session_id in the profile"
        if not self.may_session_write:
            return "session commands not permitted for this profile"
        return None

    async def _session_key(self, key: str) -> None:
        self.message = ""
        if self._scroll_key(key, len(self._session_lines())):
            return
        if key in {"esc", "backspace", "q"}:
            self.mode, self.scroll = "list", 0
            return
        if self.session_id is None:
            return
        if key == "r":
            await self._read_status()
            return
        if key not in {"a", "v", "w", "u", "b"}:
            return
        problem = self._session_problem()
        if problem is not None:
            self.message = problem
            return
        self.attempt_return = "session"
        if key == "u":
            self._begin_resume()
            return
        if key == "b":
            self._begin_abandon()
            return
        if self._reopen_unresolved():
            return
        if key == "a":
            self._start_input("query", "")
        elif key == "w":
            self.draft_observations = []
            self._start_input("observation", "")
        else:
            visit = self._known_visit()
            if visit is None:
                self.message = "no visit known; arrive first or re-read status"
                return
            self._confirm(AcknowledgementAttempt(visit[0], visit[1]))

    def _begin_resume(self) -> None:
        # Resume and abandon are the recovery steps for an unresolved turn,
        # so an unknown outcome does not block them.
        turn = self._known_turn()
        if turn is None:
            self.message = "no turn known; remember first or re-read status"
            return
        self.attempt_return = "session"
        self._confirm(ResumeAttempt(turn[0], turn[1]))

    def _begin_abandon(self) -> None:
        turn = self._known_turn()
        if turn is None:
            self.message = "no turn known; remember first or re-read status"
            return
        self.draft_turn = turn
        self.reason_for, self.attempt, self.fact = "abandon", None, None
        self._start_input("reason", "")

    def _confirm(self, write: WriteAttempt) -> None:
        self.write, self.write_outcome, self.attempt = write, None, None
        self.mode, self.input_kind, self.message = "confirm", None, ""
        self.scroll = 0

    def _input_problem(self, command: str, document: dict[str, object]) -> str | None:
        """The CLI's own checks, before anything is sent."""
        value = session_input(command, document)
        if value is None:
            return "invalid input; nothing was sent"
        if command == "remember":
            connection, profile = self.connection, self.profile
            principal = None if connection is None else connection.principal_id
            if profile is None or principal is None:
                return "the connection is not known; nothing was sent"
            try:
                preparation_size(value, profile, principal)
            except CommandInputError:
                return (
                    "the checkpoint exceeds the 73728-byte preparation bound; "
                    "nothing was sent"
                )
        return None

    async def _submit_session_input(self, text: str) -> None:
        kind = self.input_kind
        if kind == "query":
            if not text:
                self.message = "a query is required"
                return
            problem = self._input_problem("arrive", {"query": text})
            if problem is not None:
                self.message = problem
                return
            self._confirm(ArrivalAttempt(text, self._identities()))
            return
        if kind == "observation":
            if text:
                self.draft_observations.append(text)
                self.input_text = ""
                count = len(self.draft_observations)
                if count < MAX_OBSERVATIONS:
                    self.message = f"observation {count} added"
                    return
            self.input_kind, self.input_text, self.message = "response", "", ""
            return
        observations = tuple(self.draft_observations)
        attempt = RememberAttempt(
            self._identities(), self._identities(), text, observations
        )
        problem = self._input_problem("remember", remember_document(attempt))
        if problem is not None:
            self.message = problem
            return
        self.draft_observations = []
        self._confirm(attempt)

    # Input ---------------------------------------------------------------

    def _start_input(self, kind: InputKind, text: str) -> None:
        self.input_return = self.mode
        self.mode, self.input_kind, self.input_text = "input", kind, text

    async def _input_key(self, key: str) -> None:
        limit = _INPUT_LIMITS[self.input_kind or "search"]
        if key == "esc":
            self.mode, self.input_kind = self.input_return, None
            self.draft_observations, self.draft_turn = [], None
            self.message = "cancelled; nothing was sent"
        elif key == "backspace":
            self.input_text = self.input_text[:-1]
        elif key == "enter":
            await self._submit_input()
        elif (
            len(key) == 1
            and key.isprintable()
            and len((self.input_text + key).encode("utf-8")) <= limit
        ):
            self.input_text += key

    async def _submit_input(self) -> None:
        text = self.input_text
        if self.input_kind == "search":
            self.mode, self.input_kind = "list", None
            self.fact = None
            self.query = text.strip() or None
            await self.reload()
            return
        if self.input_kind in {"query", "observation", "response"}:
            await self._submit_session_input(text)
            return
        if not text.strip():
            self.message = "a reason is required"
            return
        try:
            validate_reason(text)
        except CustodyValueError:
            self.message = "the reason must be 1..4096 UTF-8 bytes"
            return
        if self.reason_for != "promote":
            await self._prepare_write(text)
            return
        fact = self.fact
        assert fact is not None
        self.attempt = PromotionAttempt(
            source_fact_id=UUID(str(fact["fact_id"])),
            evidence_id=UUID(str(fact["source_evidence_id"])),
            classification=str(fact["classification"]),
            reason=text,
            proposal_id=self._identities(),
            propose_key=self._identities(),
            accept_key=self._identities(),
        )
        self.mode, self.input_kind, self.message = "confirm", None, ""
        self.scroll = 0

    # Promotion -----------------------------------------------------------

    def promotion_offered(self, fact: Fact | None) -> bool:
        return (
            self.may_promote
            and fact is not None
            and fact.get("trust") == "candidate"
            and fact.get("invalidated_at") is None
        )

    def _reopen_unresolved(self) -> bool:
        """One unknown outcome at a time, across every mutation: reopen it
        rather than start another."""
        if self.unresolved:
            attempt, outcome = list(self.unresolved.values())[-1]
            self.attempt_return = self.mode
            self.attempt, self.outcome = attempt, outcome
            self.write, self.write_outcome = None, None
            label = "promotion"
        elif self.unresolved_writes:
            write, written = list(self.unresolved_writes.values())[-1]
            self.attempt_return = self.mode
            self.write, self.write_outcome = write, written
            self.attempt, self.outcome = None, None
            label = _WRITE_NAMES[write.operation]
        else:
            return False
        self.mode, self.scroll = "outcome", 0
        self.message = (
            f"an earlier {label} outcome is unknown; resolve it before starting another"
        )
        return True

    def _begin_promotion(self, fact: Fact | None) -> None:
        if self._reopen_unresolved():
            return
        if fact is None:
            return
        if not self.may_promote:
            self.message = "promotion not permitted for this profile"
        elif fact.get("trust") != "candidate":
            self.message = f"only candidate facts can be promoted; this fact is {sanitise(_text(fact, 'trust'))}"
        elif fact.get("invalidated_at") is not None:
            self.message = "an invalidated fact cannot be promoted"
        elif scope_key(fact["scope"]) != scope_key(self.scope):
            self.message = (
                "promotion is limited to facts at the profile's exact scope; "
                f"this fact is at {sanitise(scope_label(fact['scope']))}"
            )
        elif fact.get("source_evidence_id") is None:
            self.message = (
                "this fact has no source evidence; the browser does not promote "
                "with an external evidence reference"
            )
        else:
            self.attempt_return = self.mode
            self.fact = fact
            self.message = ""
            self.reason_for, self.write = "promote", None
            self._start_input("reason", "")

    # correct, disagree and proposal-reject ---------------------------------

    def _exact(self, fact: Fact) -> bool:
        return scope_key(fact["scope"]) == scope_key(self.scope)

    def _same(self, left: Fact | None, right: Fact | None) -> bool:
        return (
            left is not None
            and right is not None
            and left.get("fact_id") == right.get("fact_id")
        )

    def correction_offered(self, fact: Fact | None) -> bool:
        return (
            self.may_correct
            and fact is not None
            and fact.get("invalidated_at") is None
            and self._exact(fact)
        )

    def supersession_offered(self, fact: Fact | None) -> bool:
        return (
            self.correction_offered(fact)
            and self.marked is not None
            and not self._same(fact, self.marked)
        )

    def disagreement_offered(self, fact: Fact | None) -> bool:
        marked = self.marked
        return (
            self.may_disagree
            and fact is not None
            and marked is not None
            and not self._same(fact, marked)
            and self._exact(fact)
            and self._exact(marked)
        )

    def rejection_offered(self, proposal: ProposalSnapshot | None) -> bool:
        return self.may_reject and proposal is not None and proposal.state == "pending"

    def _fact_write_key(self, key: str, fact: Fact | None) -> None:
        if fact is None:
            return
        if key == "m":
            self._mark(fact)
        elif key == "x":
            self._begin_correction(fact, supersede=False)
        elif key == "X":
            self._begin_correction(fact, supersede=True)
        else:
            self._begin_disagreement(fact)

    def _mark(self, fact: Fact) -> None:
        """The second fact for X and d is always one the human marked."""
        if not (self.may_correct or self.may_disagree):
            self.message = "marking serves correct and disagree; neither is permitted"
        elif self._same(fact, self.marked):
            self.marked = None
            self.message = "mark cleared"
        else:
            self.marked = fact
            self.message = f"marked {_text(fact, 'fact_id')}"

    def _fact_problem(self, fact: Fact, role: str, operation: str) -> str | None:
        if not self._exact(fact):
            return (
                f"{operation} is limited to facts at the profile's exact scope; "
                f"the {role} is at {sanitise(scope_label(fact['scope']))}"
            )
        if _uuid4(fact.get("fact_id")) is None:
            return f"the {role} ID is not a canonical UUIDv4; Cairn would refuse it"
        return None

    def _begin_correction(self, fact: Fact, *, supersede: bool) -> None:
        if self._reopen_unresolved():
            return
        other = self.marked if supersede else None
        if not self.may_correct:
            self.message = "correction not permitted for this profile"
            return
        if fact.get("invalidated_at") is not None:
            self.message = "this fact is already invalidated"
            return
        problem = self._fact_problem(fact, "fact", "correction")
        if problem is None and supersede:
            if other is None:
                problem = "mark the replacement fact with m first; X supersedes by it"
            elif self._same(fact, other):
                problem = "the marked fact is this fact; mark a different replacement"
            elif _uuid4(other.get("fact_id")) is None:
                problem = "the marked fact ID is not a canonical UUIDv4; Cairn would refuse it"
            elif other.get("invalidated_at") is not None:
                problem = "the marked replacement is itself invalidated"
        if problem is not None:
            self.message = problem
            return
        self._start_write("correct", fact, other)

    def _begin_disagreement(self, fact: Fact) -> None:
        if self._reopen_unresolved():
            return
        other = self.marked
        if not self.may_disagree:
            self.message = "disagreement not permitted for this profile"
            return
        if other is None:
            self.message = "mark the other fact with m first; d disagrees with it"
            return
        if self._same(fact, other):
            self.message = "the marked fact is this fact; mark a different fact"
            return
        problem = self._fact_problem(
            other, "marked fact", "disagreement"
        ) or self._fact_problem(fact, "fact", "disagreement")
        if problem is not None:
            self.message = problem
            return
        self._start_write("disagree", fact, other)

    def _start_write(
        self,
        operation: Literal["correct", "disagree"],
        fact: Fact,
        other: Fact | None,
    ) -> None:
        self.attempt_return = self.mode
        self.fact, self.draft_other, self.draft_proposal = fact, other, None
        self.message = ""
        self.reason_for, self.attempt = operation, None
        self._start_input("reason", "")

    def _begin_rejection(self, proposal: ProposalSnapshot | None) -> None:
        if self._reopen_unresolved():
            return
        if proposal is None:
            return
        if not self.may_reject:
            self.message = "proposal rejection not permitted for this profile"
            return
        if proposal.state != "pending":
            self.message = (
                f"this proposal is {proposal.state}; only a pending one can be rejected"
            )
            return
        self.attempt_return = self.mode
        self.draft_proposal, self.draft_other = proposal, None
        self.message = ""
        self.reason_for, self.attempt = "proposal-reject", None
        self._start_input("reason", "")

    async def _prepare_write(self, reason: str) -> None:
        """Build the attempt, generating its idempotency key exactly once."""
        write: WriteAttempt
        if self.reason_for == "abandon":
            turn = self.draft_turn
            assert turn is not None
            document: dict[str, object] = {"turn_id": str(turn[0]), "reason": reason}
            problem = self._input_problem("abandon", document)
            if problem is not None:
                self.message = problem
                return
            self.draft_turn = None
            self._confirm(AbandonAttempt(turn[0], turn[1], reason))
            return
        if self.reason_for == "proposal-reject":
            listed = self.draft_proposal
            assert listed is not None
            # Re-read so the confirm screen never offers a decided proposal.
            self._busy("Re-reading proposal...")
            self.message = ""
            try:
                current = await self.backend.proposal(listed.proposal_id)
            except BrowseFailure as failure:
                self.message = f"proposal-read failed: {failure.code}; nothing was sent"
                return
            if self.attempt_return == "proposal":
                self.proposal = current
            if current.proposal_id != listed.proposal_id or current.state != "pending":
                self.mode, self.input_kind = self.attempt_return, None
                self.draft_proposal = None
                self.message = (
                    f"proposal is no longer pending (now {current.state}); "
                    "nothing was sent"
                    + ("; press r to refresh" if self.mode == "proposals" else "")
                )
                return
            write = RejectionAttempt(
                proposal_id=current.proposal_id,
                proposal_line=_first_line(current.reason),
                source_fact_id=current.source_fact_id,
                target_scope=current.target_scope,
                reason=reason,
                key=self._identities(),
            )
        else:
            fact, other = self.fact, self.draft_other
            assert fact is not None
            fact_id = _uuid4(fact.get("fact_id"))
            assert fact_id is not None
            if self.reason_for == "correct":
                write = CorrectionAttempt(
                    fact_id=fact_id,
                    fact_line=_first_line(fact.get("body")),
                    superseded_by=None
                    if other is None
                    else _uuid4(other.get("fact_id")),
                    superseded_line=None
                    if other is None
                    else _first_line(other.get("body")),
                    reason=reason,
                    key=self._identities(),
                )
            else:
                assert other is not None
                left = _uuid4(other.get("fact_id"))
                assert left is not None
                write = DisagreementAttempt(
                    left_fact_id=left,
                    left_line=_first_line(other.get("body")),
                    right_fact_id=fact_id,
                    right_line=_first_line(fact.get("body")),
                    reason=reason,
                    key=self._identities(),
                )
        self.write, self.write_outcome, self.attempt = write, None, None
        self.mode, self.input_kind, self.message = "confirm", None, ""
        self.scroll = 0

    def _screen_lines(self) -> list[Line]:
        lines = (
            self._confirm_lines() if self.mode == "confirm" else self._outcome_lines()
        )
        if self.mode == "outcome" and self.message:
            lines = [Line(self.message, "status"), *lines]
        return _wrapped(lines, self.width - 1)

    async def _confirm_key(self, key: str) -> None:
        if key == "y":
            await self._execute()
        elif self._scroll_key(key, len(self._screen_lines())):
            return
        elif key in {"esc", "n", "q", "backspace"}:
            write = self.write
            self.mode, self.attempt, self.write = self.attempt_return, None, None
            if self.mode == "list":
                self.fact = None
            label = "promotion" if write is None else _WRITE_NAMES[write.operation]
            self.message = f"{label} cancelled; nothing was sent"

    async def _execute(self) -> None:
        if self.write is not None:
            await self._execute_write()
            return
        attempt = self.attempt
        assert attempt is not None
        self.mode, self.scroll = "outcome", 0
        self.outcome = None
        self._busy("Promoting...")
        try:
            outcome = await self.backend.promote(attempt)
        except BrowseFailure as failure:
            outcome = PromotionOutcome("failed", failure.operation, failure.code)
        self.message = ""
        self.outcome = outcome
        # Only a FactsPromoted receipt for this exact attempt resolves an
        # unknown outcome. A retry that fails or is refused before resending
        # proves nothing about the earlier dispatch, so the original stays.
        if outcome.status == "promoted":
            self.unresolved.pop(attempt.proposal_id, None)
        elif outcome.status == "uncertain":
            earlier = self.unresolved.get(attempt.proposal_id)
            self.unresolved[attempt.proposal_id] = (
                attempt,
                outcome if earlier is None else earlier[1],
            )

    async def _execute_write(self) -> None:
        write = self.write
        assert write is not None
        self.mode, self.scroll = "outcome", 0
        self.write_outcome = None
        self._busy(f"Sending {write.operation}...")
        try:
            outcome = await self.backend.write(write)
        except BrowseFailure as failure:
            outcome = WriteOutcome("failed", failure.operation, failure.code)
        self.message = ""
        self.write_outcome = outcome
        if isinstance(write, RememberAttempt):
            self.session_turn = write.turn_id
        if isinstance(write, ArrivalAttempt) and outcome.visit_id is not None:
            self.issued_visit = UUID(outcome.visit_id)
        if (
            isinstance(write, AcknowledgementAttempt)
            and outcome.status == "recorded"
            and write.visit_id == self.issued_visit
        ):
            self.issued_visit = None
        # As for promotion: only a receipt for this exact attempt resolves an
        # unknown outcome; a later refusal or unsent retry proves nothing.
        # A session turn is keyed by its turn_id, so resume or abandon of an
        # unresolved checkpoint settles it, and the original attempt is kept.
        if outcome.status in _SETTLED:
            self.unresolved_writes.pop(write.key, None)
        elif outcome.status in _OPEN:
            earlier = self.unresolved_writes.get(write.key)
            self.unresolved_writes[write.key] = (
                (write, outcome) if earlier is None else earlier
            )

    async def _session_outcome_key(self, key: str) -> None:
        outcome, write = self.write_outcome, self.write
        assert outcome is not None and write is not None
        if key == "R" and self._retryable():
            await self._execute()
            return
        if (
            key == "u"
            and isinstance(write, (RememberAttempt, ResumeAttempt))
            and write.key in self.unresolved_writes
        ):
            self._begin_resume()
            return
        if self._scroll_key(key, len(self._screen_lines())):
            return
        self.write, self.write_outcome = None, None
        self.mode, self.scroll = "session", 0
        if outcome.status == "saved":
            # New candidate facts: the listing must not stay stale.
            await self.reload()
        # Every session command may have changed state; re-read it.
        await self._read_status()
        if not self.message:
            self.message = (
                "saved; listing refreshed and status re-read"
                if outcome.status == "saved"
                else "status re-read"
            )

    async def _write_outcome_key(self, key: str) -> None:
        outcome, write = self.write_outcome, self.write
        if outcome is None or write is None:
            return
        if isinstance(write, SESSION_ATTEMPTS):
            await self._session_outcome_key(key)
            return
        if key == "R" and self._retryable():
            await self._execute()
            return
        if self._scroll_key(key, len(self._screen_lines())):
            return
        self.write, self.write_outcome = None, None
        if outcome.status == "recorded":
            # The views now show superseded state; reload rather than keep it.
            if write.operation == "proposal-reject":
                self.mode, self.proposal = "proposals", None
                await self.reload_proposals()
                self.message = "proposal rejected; proposals refreshed"
            else:
                self.mode, self.fact, self.marked = "list", None, None
                await self.reload()
                self.message = (
                    "corrected; listing refreshed"
                    if write.operation == "correct"
                    else "disagreement recorded; listing refreshed"
                )
            return
        self.mode = self.attempt_return
        if self.mode == "list":
            self.fact = None

    async def _outcome_key(self, key: str) -> None:
        if self.write is not None:
            await self._write_outcome_key(key)
            return
        outcome = self.outcome
        if outcome is None:
            return
        if key == "R" and self._retryable():
            await self._execute()
            return
        if self._scroll_key(key, len(self._screen_lines())):
            return
        promoted = outcome.status == "promoted"
        self.outcome = None
        if promoted:
            self.mode, self.fact, self.attempt = "list", None, None
            await self.reload()
            self.message = "promoted; listing refreshed"
            return
        self.mode = self.attempt_return
        if self.mode == "list":
            self.fact = None
        self.attempt = None

    def _retryable(self) -> bool:
        """An exact resubmission with the same keys; never after a definite
        refusal unless an earlier outcome for this attempt is still unknown."""
        write, written = self.write, self.write_outcome
        if isinstance(write, ArrivalAttempt):
            # Every arrival issues a fresh visit; nothing to resubmit exactly.
            return False
        if write is not None:
            return written is not None and (
                written.status in {"uncertain", "failed"}
                or write.key in self.unresolved_writes
            )
        outcome, attempt = self.outcome, self.attempt
        return (
            outcome is not None
            and attempt is not None
            and (
                outcome.status in {"uncertain", "failed"}
                or attempt.proposal_id in self.unresolved
            )
        )

    # Rendering -----------------------------------------------------------

    def _row(self, fact: Fact) -> str:
        marker = _MARKERS.get(str(fact.get("trust")), "[?]")
        recorded = _text(fact, "recorded_at")[:16].replace("T", " ")
        ancestor = "" if scope_key(fact["scope"]) == scope_key(self.scope) else "^ "
        marked = "* " if self._same(fact, self.marked) else ""
        return f"{marked}{marker} {recorded} {ancestor}{_first_line(fact.get('body'))}"

    def _write_header(self, write: WriteAttempt) -> str:
        confirm = self.mode == "confirm"
        if isinstance(write, SESSION_ATTEMPTS):
            return ("Confirm: " if confirm else "") + {
                "arrive": "arrive (open session, issue a visit)",
                "acknowledge-visit": "acknowledge-visit",
                "remember": "remember (save a checkpoint)",
                "resume": "resume a turn",
                "abandon": "abandon a turn",
            }[write.operation]
        if isinstance(write, CorrectionAttempt):
            return (
                f"Confirm: correct (invalidate) {write.fact_id}"
                if confirm
                else f"Correction of {write.fact_id}"
            )
        if isinstance(write, DisagreementAttempt):
            pair = f"{write.left_fact_id} and {write.right_fact_id}"
            return (
                f"Confirm: disagreement between {pair}"
                if confirm
                else f"Disagreement between {pair}"
            )
        return (
            f"Confirm: reject proposal {write.proposal_id}"
            if confirm
            else f"Rejection of proposal {write.proposal_id}"
        )

    def _header(self) -> str:
        marked = (
            "" if self.marked is None else f"  marked: {_text(self.marked, 'fact_id')}"
        )
        if self.mode in {"confirm", "outcome"} and self.write is not None:
            return self._write_header(self.write)
        if self.mode == "detail":
            return f"Fact detail{marked}"
        if self.mode == "confirm" and self.attempt is not None:
            return f"Confirm: promote {self.attempt.source_fact_id} to validated"
        if self.mode == "outcome" and self.attempt is not None:
            return f"Promotion of {self.attempt.source_fact_id}"
        if self.mode == "evidence":
            return "Source evidence (untrusted text, not a fact)"
        if self.mode == "proposal":
            return "Proposal detail (read-only)"
        if self.mode == "suggestions" and self.suggest_fact is not None:
            fact_id = _text(self.suggest_fact, "fact_id")
            return f"Suggestions for {fact_id} (read-only, untrusted evidence)"
        if self.mode == "connection":
            return "Connection and grants (read-only)"
        if self.mode == "session":
            if self.session_id is None:
                return "Session commands unavailable"
            return f"Session {self.session_id}"
        if self.mode == "proposals":
            label = f"Pending proposals {scope_label(self.scope)}"
            if self.proposal_error is not None:
                return f"{label}  unavailable"
            more = "+" if self.proposal_cursor is not None else ""
            incomplete = "  listing incomplete" if self.proposal_incomplete else ""
            # proposal-list pages by proposal ID and is not a snapshot.
            return (
                f"{label}  {len(self.pending)}/{len(self.proposal_rows)}{more} "
                f"loaded{incomplete}  by id, not a snapshot"
            )
        listing = f"search: {self.query}" if self.query else "newest first"
        more = "+" if self.cursor is not None else ""
        partial = "" if self.selection_complete else "  selection incomplete"
        return (
            f"Cairn {scope_label(self.scope)}  {listing}  filter: {self.filter}  "
            f"{len(self.view)}/{len(self.rows)}{more} loaded{partial}"
            + (
                ""
                if self.marked is None
                else f"  * marked {_text(self.marked, 'fact_id')[:8]}"
            )
        )

    def _detail_lines(self) -> list[Line]:
        fact = self.fact
        assert fact is not None
        width = self.width
        evidence = fact.get("source_evidence_id")
        lines = [
            Line(f"fact_id: {_text(fact, 'fact_id')}"),
            Line(
                f"trust: {_text(fact, 'trust')}   classification: {_text(fact, 'classification')}"
            ),
            Line(f"scope: {scope_label(fact['scope'])}"),
            Line(
                f"source_type: {_text(fact, 'source_type')}   source principal: {_text(fact, 'source_principal_id')}"
            ),
            Line(f"recorded_at: {_text(fact, 'recorded_at')}"),
            Line(f"observed_at (source claim): {_text(fact, 'observed_at')}"),
            Line(
                f"valid_from: {_text(fact, 'valid_from')}   valid_to: {_text(fact, 'valid_to')}"
            ),
            Line(f"invalidated_at: {_text(fact, 'invalidated_at')}"),
            Line(
                f"derived_from: {_text(fact, 'derived_from')}   promoted_by: {_text(fact, 'promoted_by')}"
            ),
            Line(f"promotion evidence: {_text(fact, 'evidence_id')}"),
            Line(
                "source evidence: "
                + ("none" if evidence is None else f"{evidence} (press e to open)")
            ),
            Line(
                f"has_disagreement: {_text(fact, 'has_disagreement')}   context incomplete: {_text(fact, 'disagreement_context_incomplete')}"
            ),
            Line(""),
            Line("Body (untrusted text):", "header"),
        ]
        body = fact.get("body")
        lines.extend(
            Line("  " + part)
            for part in wrap(body if type(body) is str else "", width - 2)
        )
        lines.append(Line(""))
        lines.append(Line("History and relationships:", "header"))
        lines.extend(self._history_lines(str(fact.get("fact_id"))))
        return _wrapped(lines, width - 1)

    def _history_lines(self, fact_id: str) -> list[Line]:
        if self.history_error is not None:
            return [Line(f"  history unavailable: {self.history_error}")]
        history = self.history
        if history is None:
            return [Line("  history not loaded")]
        lines: list[Line] = []
        for item in cast(tuple[Fact, ...], history.get("facts", ())):
            if str(item.get("fact_id")) == fact_id:
                continue
            lines.append(
                Line(
                    f"  fact {_text(item, 'fact_id')} {_text(item, 'trust')} "
                    f"recorded {_text(item, 'recorded_at')} invalidated {_text(item, 'invalidated_at')}: "
                    f"{_first_line(item.get('body'))}"
                )
            )
        for item in cast(tuple[Fact, ...], history.get("corrections", ())):
            lines.append(
                Line(
                    f"  correction of {_text(item, 'fact_id')} superseded_by {_text(item, 'superseded_by')} "
                    f"by {_text(item, 'principal_id')} at {_text(item, 'invalidated_at')}: {_first_line(item.get('reason'))}"
                )
            )
        for item in cast(tuple[Fact, ...], history.get("disagreements", ())):
            lines.append(
                Line(
                    f"  disagreement {_text(item, 'relationship_id')}: {_text(item, 'left_fact_id')} vs "
                    f"{_text(item, 'right_fact_id')} by {_text(item, 'principal_id')}: {_first_line(item.get('reason'))}"
                )
            )
        for item in cast(tuple[Fact, ...], history.get("resolutions", ())):
            details = ", ".join(
                f"{key}={_first_line(str(value))}"
                for key, value in sorted(item.items())
                if key != "scope"
            )
            lines.append(Line(f"  resolution: {details}"))
        if history.get("budget_exhausted") is True:
            lines.append(Line("  (history budget exhausted; more may exist)"))
        return lines or [Line("  no other history records disclosed")]

    def _evidence_lines(self) -> list[Line]:
        window = self.window
        assert window is not None
        lines = [
            Line(f"evidence_id: {_text(window, 'evidence_id')}"),
            Line(
                f"bytes {_text(window, 'start_byte')}..{_text(window, 'end_byte')} of {_text(window, 'byte_length')}"
                f"   sha256 {_text(window, 'sha256')}"
            ),
            Line(""),
        ]
        text = window.get("text")
        lines.extend(
            Line(part)
            for part in wrap(text if type(text) is str else "", self.width - 1)
        )
        return _wrapped(lines, self.width - 1)

    def _proposal_row(self, proposal: ProposalSnapshot) -> str:
        recorded = proposal.recorded_at.isoformat()[:16].replace("T", " ")
        invalidated = " (invalidated)" if proposal.source_invalidated else ""
        return (
            f"{recorded} {proposal.state} -> {scope_label(proposal.target_scope)} "
            f"source {str(proposal.source_fact_id)[:8]}{invalidated}: "
            f"{_first_line(proposal.reason)}"
        )

    def _proposal_lines(self) -> list[Line]:
        proposal = self.proposal
        assert proposal is not None
        width = self.width
        lines = [
            Line(f"proposal_id: {proposal.proposal_id}"),
            Line(f"state: {proposal.state}"),
            Line(f"scope: {scope_label(proposal.scope)}"),
            Line(f"target_scope: {scope_label(proposal.target_scope)}"),
            Line(f"source_fact_id: {proposal.source_fact_id}"),
            Line(
                f"source trust: {proposal.source_trust}   "
                f"source invalidated: {proposal.source_invalidated}"
            ),
            Line(f"classification: {proposal.classification}"),
            Line(f"proposed_by: {proposal.proposed_by}"),
            Line(f"recorded_at: {proposal.recorded_at.isoformat()}"),
            Line(f"mutation_id: {proposal.mutation_id}"),
            Line(""),
            Line("Reason (untrusted text):", "header"),
            *(Line("  " + part) for part in wrap(proposal.reason, width - 2)),
            Line(""),
        ]
        decision = proposal.decision
        if decision is None:
            lines.append(Line("Decision: none recorded"))
        else:
            lines.extend(
                [
                    Line("Decision:", "header"),
                    Line(f"  state: {decision.state}"),
                    Line(f"  decided_by: {decision.decided_by}"),
                    Line(f"  recorded_at: {decision.recorded_at.isoformat()}"),
                    Line(f"  mutation_id: {decision.mutation_id}"),
                    Line(f"  evidence_id: {_optional(decision.evidence_id)}"),
                    Line(f"  promoted_fact_id: {_optional(decision.promoted_fact_id)}"),
                ]
            )
            if decision.reason is not None:
                lines.append(Line("  reason (untrusted text):"))
                lines.extend(
                    Line("    " + part) for part in wrap(decision.reason, width - 4)
                )
        return _wrapped(lines, width - 1)

    def _suggestion_lines(self) -> list[Line]:
        width, result = self.width, self.suggestions
        lines = [Line("Suggestions are evidence only; the browser never acts on them.")]
        if self.suggestions_error is not None:
            lines.append(
                Line(
                    "(suggestions unavailable: suggest failed: "
                    f"{self.suggestions_error})"
                )
            )
            return _wrapped(lines, width - 1)
        if result is None:
            lines.append(Line("suggestions not loaded"))
            return _wrapped(lines, width - 1)
        lines.extend(
            [
                Line(
                    f"budget consumed: {result.budget_consumed}   "
                    f"budget exhausted: {result.budget_exhausted}   "
                    f"semantic degraded: {result.semantic_degraded}"
                ),
                Line(f"recall policy: {_optional(result.policy)}"),
                Line(""),
            ]
        )
        if not result.items:
            lines.append(
                Line(
                    "(no suggestions returned; an empty result does not prove "
                    "there are no duplicates)"
                )
            )
        for number, item in enumerate(result.items, 1):
            # kind, basis and reason are a closed vocabulary the client checks.
            lines.append(
                Line(
                    f"{number}. {_text(item, 'kind')} "
                    f"(basis: {_text(item, 'match_basis')})",
                    "header",
                )
            )
            lines.append(Line(f"  {_text(item, 'reason')}"))
            for evidence in cast(tuple[Fact, ...], item.get("facts", ())):
                lines.append(
                    Line(
                        f"  fact {_text(evidence, 'fact_id')} {_text(evidence, 'trust')} "
                        f"at {scope_label(evidence['scope'])} "
                        f"recorded {_text(evidence, 'recorded_at')} "
                        f"invalidated {_text(evidence, 'invalidated_at')}"
                    )
                )
                body = evidence.get("body")
                lines.append(Line("    body (untrusted text):"))
                lines.extend(
                    Line("      " + part)
                    for part in wrap(body if type(body) is str else "", width - 6)
                )
            for correction in cast(tuple[Fact, ...], item.get("corrections", ())):
                lines.append(
                    Line(
                        f"  correction of {_text(correction, 'fact_id')} "
                        f"superseded_by {_text(correction, 'superseded_by')} "
                        f"by {_text(correction, 'principal_id')} "
                        f"at {_text(correction, 'invalidated_at')}"
                    )
                )
                reason = correction.get("reason")
                lines.append(Line("    reason (untrusted text):"))
                lines.extend(
                    Line("      " + part)
                    for part in wrap(reason if type(reason) is str else "", width - 6)
                )
            for link in cast(tuple[Fact, ...], item.get("disagreements", ())):
                lines.append(
                    Line(
                        f"  disagreement {_text(link, 'relationship_id')}: "
                        f"{_text(link, 'left_fact_id')} vs "
                        f"{_text(link, 'right_fact_id')} "
                        f"by {_text(link, 'principal_id')}"
                    )
                )
                reason = link.get("reason")
                lines.append(Line("    reason (untrusted text):"))
                lines.extend(
                    Line("      " + part)
                    for part in wrap(reason if type(reason) is str else "", width - 6)
                )
            lines.append(Line(""))
        if result.budget_exhausted:
            lines.append(Line("(suggestion budget exhausted; more may exist)"))
        return _wrapped(lines, width - 1)

    def _connection_lines(self) -> list[Line]:
        """What ``check`` reports, never a credential or token."""
        lines: list[Line] = []
        connection = self.connection
        if self.connection_error is not None:
            lines.append(
                Line(f"re-read failed: diagnose failed: {self.connection_error}")
            )
            if connection is not None:
                lines.append(
                    Line(
                        "Showing the last successful diagnosis; grants may differ now."
                    )
                )
            lines.append(Line(""))
        if connection is None:
            lines.append(Line("(no diagnosis available; press r to read it again)"))
            return _wrapped(lines, self.width - 1)
        evaluated = connection.evaluated_at
        scope = connection.scope
        classification = connection.classification
        lines.extend(
            [
                Line(f"status: {connection.status.value}"),
                Line(
                    "evaluated_at (server): "
                    + ("-" if evaluated is None else evaluated.isoformat())
                ),
                Line(f"instance_id: {_optional(connection.instance_id)}"),
                Line(f"product_version: {_optional(connection.product_version)}"),
                Line(f"contract_identity: {_optional(connection.contract_identity)}"),
                Line(f"contract_digest: {_optional(connection.contract_digest)}"),
                Line(
                    f"mcp_contract_digest: {_optional(connection.mcp_contract_digest)}"
                ),
                Line(
                    f"principal_id: {_optional(connection.principal_id)}   "
                    f"principal_kind: {_optional(connection.principal_kind)}"
                ),
                Line("scope: " + ("-" if scope is None else scope_label(scope))),
                Line(
                    "classification: "
                    + ("-" if classification is None else classification.value)
                ),
                Line(f"permission_basis: {_optional(connection.permission_basis)}"),
                Line(""),
                Line("Permissions at this exact scope and classification:", "header"),
            ]
        )
        permissions = connection.permissions
        if permissions is None:
            lines.append(Line("  not reported"))
        else:
            lines.extend(
                [
                    Line(f"  retrieve: {_yes(permissions.retrieve)}"),
                    Line(f"  ingest: {_yes(permissions.ingest)}"),
                    Line(f"  promote: {_yes(permissions.promote)}"),
                    Line(f"  invalidate: {_yes(permissions.invalidate)}"),
                ]
            )
        reads = "available" if self.may_retrieve else "unavailable"
        promote = "available" if self.may_promote else "unavailable"
        lines.extend(
            [
                Line(""),
                Line("In this browser:", "header"),
                Line("  listing, search, history and evidence need retrieve"),
                Line(f"  s suggestions and P proposals need retrieve: {reads}"),
                Line(f"  p promotion needs ingest and promote: {promote}"),
                Line(
                    "  x and X correct need invalidate: "
                    + ("available" if self.may_correct else "unavailable")
                ),
                Line(
                    "  d disagree needs retrieve and ingest: "
                    + ("available" if self.may_disagree else "unavailable")
                ),
                Line(
                    "  x reject (proposals) needs retrieve, and promote at the "
                    "proposal's target scope, which Cairn checks: "
                    + ("available" if self.may_reject else "unavailable")
                ),
                Line(
                    "  S session: status needs retrieve; arrive, acknowledge-visit, "
                    "remember, resume and abandon need retrieve and ingest: "
                    + (
                        "unavailable (the profile has no session_id)"
                        if self.session_id is None
                        else "available"
                        if self.may_session_write
                        else "unavailable"
                    )
                ),
                Line(
                    "Grants can change at any time; Cairn checks every request again."
                ),
            ]
        )
        return _wrapped(lines, self.width - 1)

    # Session rendering ---------------------------------------------------

    def _session_lines(self) -> list[Line]:
        if self.session_id is None:
            return _wrapped(
                [
                    Line(
                        "Session commands are unavailable for this profile.", "header"
                    ),
                    Line(
                        "The profile has no session_id. cairn-memory refuses arrive, "
                        "acknowledge-visit, remember, status, resume and abandon "
                        "without one (session_id_required), and so does this "
                        "browser. Nothing was sent."
                    ),
                ],
                self.width - 1,
            )
        lines = [Line(f"session_id: {self.session_id} (from the profile)"), Line("")]
        status = self.session_status
        if self.session_error is not None:
            lines.append(
                Line(f"(status unavailable: status failed: {self.session_error})")
            )
        elif status is None:
            lines.append(Line("(status not read; press r to read it)"))
        else:
            lines.extend(self._status_lines(status))
        visit, turn = self._known_visit(), self._known_turn()
        allowed = self.may_session_write
        lines.extend(
            [
                Line(""),
                Line("In this browser:", "header"),
                Line(
                    "  visit for v: "
                    + ("none known" if visit is None else f"{visit[0]} ({visit[1]})")
                ),
                Line(
                    "  turn for u and b: "
                    + ("none known" if turn is None else f"{turn[0]} ({turn[1]})")
                ),
                Line(
                    "  r status needs retrieve: "
                    + ("available" if self.may_retrieve else "unavailable")
                ),
                Line(
                    "  a arrive, v acknowledge-visit, w remember, u resume and "
                    "b abandon need retrieve and ingest: "
                    + ("available" if allowed else "unavailable")
                ),
                Line(
                    "Each of them is sent only after y on a confirm screen. "
                    "Cairn checks every request again."
                ),
            ]
        )
        return _wrapped(lines, self.width - 1)

    def _status_lines(self, status: SessionSnapshot) -> list[Line]:
        """What status (session-read) reports; it opens nothing."""
        custody = status.custody_receipt is not None
        asked = self._own_turn()
        lines = [
            Line(
                "Status (session-read; reads only), "
                + (
                    "for the session (no turn_id given):"
                    if asked is None
                    else f"for turn {asked}:"
                ),
                "header",
            ),
            Line(f"  state: {status.state}"),
            Line(f"  turn_id: {_optional(status.turn_id)}"),
            Line(f"  attempt_id: {_optional(status.attempt_id)}"),
            Line(f"  replaces_turn_id: {_optional(status.replaces_turn_id)}"),
            Line(
                f"  turn_count: {status.turn_count}   "
                f"prepared_bytes: {status.prepared_bytes}"
            ),
            Line(f"  visit_id: {_optional(status.visit_id)}"),
            Line(
                f"  visit_watermark: {_optional(status.visit_watermark)}   visit_at: "
                + ("-" if status.visit_at is None else status.visit_at.isoformat())
            ),
            Line(
                f"  acknowledged_watermark: {status.acknowledged_watermark}   "
                "acknowledged_at: "
                + (
                    "-"
                    if status.acknowledged_at is None
                    else status.acknowledged_at.isoformat()
                )
            ),
            Line(
                "  committed custody receipt: "
                + (
                    f"present (key {_optional(status.custody_idempotency_key)})"
                    if custody
                    else "none"
                )
            ),
            Line(
                f"  stored observations: {len(status.observations)} "
                "(stored output is not fact custody)"
            ),
        ]
        if status.abandonment_reason is not None:
            lines.append(Line("  abandonment reason (untrusted text):"))
            lines.extend(
                Line("    " + part)
                for part in wrap(status.abandonment_reason, self.width - 4)
            )
        return lines

    def _session_confirm_lines(self, write: SessionAttempt) -> list[Line]:
        session = f"session_id: {self.session_id} (from the profile)"
        if isinstance(write, ArrivalAttempt):
            return [
                Line("Open or replay the session and issue a new visit?", "header"),
                Line(""),
                Line("operation: arrive"),
                Line(session),
                *self._reason_lines("query", write.query),
                Line("budget: 16384 (default)   history_fact_ids: none"),
                Line(""),
                Line(
                    "This opens or replays the session (session-open) and issues a "
                    "fresh visit (visit-issue): both change session state on Cairn. "
                    "Then it reads the briefing."
                ),
                Line(
                    "The visit is not acknowledged; that is a separate confirmed "
                    "step (v). A fresh visit key is drawn for every arrival, so "
                    "there is no exact resubmission."
                ),
            ]
        if isinstance(write, AcknowledgementAttempt):
            return [
                Line("Acknowledge this visit?", "header"),
                Line(""),
                Line("operation: acknowledge-visit"),
                Line(session),
                Line(f"visit_id: {write.visit_id} ({write.source})"),
                Line(""),
                Line(
                    "Acknowledge only after reading the briefing. This moves the "
                    "session's acknowledged checkpoint; it saves no facts."
                ),
                Line("idempotency key: derived by the client from the visit_id"),
            ]
        if isinstance(write, RememberAttempt):
            session_id = self.session_id
            assert session_id is not None
            begin, prepare = (
                command_key(step, session_id, write.turn_id, write.attempt_id)
                for step in ("begin", "prepare")
            )
            lines = [
                Line("Save these observations as a completed checkpoint?", "header"),
                Line(""),
                Line("operation: remember"),
                Line(session),
                Line(f"turn_id: {write.turn_id} (new)"),
                Line(f"attempt_id: {write.attempt_id} (new)"),
                Line("replaces_turn_id: none"),
                *self._checkpoint_content(write),
            ]
            lines.extend(
                [
                    Line(""),
                    Line(
                        "Sends session-open, turn-begin and turn-prepare, then "
                        "resume (session-read, and turn-commit if prepared)."
                    ),
                    Line(
                        "No observations: Cairn records the turn as skipped and "
                        "saves no facts."
                        if not write.observations
                        else "Saved observations become candidate facts at the "
                        "profile scope."
                    ),
                    Line(f"turn-begin key: {begin}"),
                    Line(f"turn-prepare key: {prepare}"),
                ]
            )
            return lines
        if isinstance(write, ResumeAttempt):
            return [
                Line("Resume this turn?", "header"),
                Line(""),
                Line("operation: resume"),
                Line(session),
                Line(f"turn_id: {write.turn_id} ({write.source})"),
                Line(""),
                Line(
                    "Reads the turn's status and, if its output is prepared, "
                    "commits it (turn-commit). It never regenerates or resubmits "
                    "output."
                ),
            ]
        return [
            Line("Abandon this turn?", "header"),
            Line(""),
            Line("operation: abandon"),
            Line(session),
            Line(f"turn_id: {write.turn_id} ({write.source})"),
            *self._reason_lines("reason", write.reason),
            Line(""),
            Line(
                "Fences an unprepared started turn; Cairn refuses it for prepared "
                "or completed output. It saves nothing and does not touch any "
                "visit. A later checkpoint may replace the turn."
            ),
            Line("idempotency key: derived by the client from the turn_id"),
        ]

    def _checkpoint_content(self, write: RememberAttempt) -> list[Line]:
        """The response and every observation in full. An unresolved
        checkpoint can only be resubmitted with exactly this content, so the
        outcome screen shows it as the confirm screen did."""
        size = len(write.response.encode("utf-8"))
        lines = [
            Line(f"response ({size} bytes):"),
            *(
                [Line("  (empty)")]
                if not write.response
                else [
                    Line("  " + part) for part in wrap(write.response, self.width - 2)
                ]
            ),
            Line(f"observations: {len(write.observations)}"),
        ]
        for number, body in enumerate(write.observations, 1):
            lines.append(Line(f"  {number}. body (no timestamps):"))
            lines.extend(Line("     " + part) for part in wrap(body, self.width - 5))
        return lines

    def _session_identities(self, write: SessionAttempt) -> list[Line]:
        lines = [
            Line(f"operation: {write.operation}"),
            Line(f"session_id: {self.session_id}"),
        ]
        if isinstance(write, AcknowledgementAttempt):
            lines.append(Line(f"visit_id: {write.visit_id}"))
        elif isinstance(write, RememberAttempt):
            lines.extend(
                [
                    Line(f"turn_id: {write.turn_id}"),
                    Line(f"attempt_id: {write.attempt_id}"),
                    Line("resubmit exactly this response and these observations:"),
                    *self._checkpoint_content(write),
                ]
            )
        elif isinstance(write, (ResumeAttempt, AbandonAttempt)):
            lines.append(Line(f"turn_id: {write.turn_id}"))
            if isinstance(write, AbandonAttempt):
                lines.extend(
                    self._reason_lines(
                        "reason (resubmit exactly this text)", write.reason
                    )
                )
        return lines

    def _briefing_lines(self, arrival: DurableArrival) -> list[Line]:
        briefing, width = arrival.briefing, self.width
        lines = [
            Line(
                "since: "
                + ("-" if briefing.since is None else briefing.since.isoformat())
            ),
            Line(
                "warnings: "
                + (", ".join(briefing.warnings) if briefing.warnings else "none")
            ),
        ]
        for failure in briefing.failures:
            lines.append(
                Line(
                    f"failure: {failure.operation} {_optional(failure.fact_id)}: "
                    f"{failure.failure.code}"
                )
            )
        lines.append(Line(""))
        lines.append(Line("Summary (selected memory, untrusted text):", "header"))
        if not briefing.summary:
            lines.append(Line("  (no summary entries; this does not prove none exist)"))
        for item in briefing.summary:
            lines.append(
                Line(
                    f"  fact {item.fact_id} {item.trust} by "
                    f"{_optional(item.source_principal_id)} "
                    f"({_optional(item.source_type)})"
                    + ("  has disagreement" if item.has_disagreement else "")
                )
            )
            lines.extend(Line("    " + part) for part in wrap(item.excerpt, width - 4))
        if briefing.omitted_summary_count:
            lines.append(
                Line(f"  ({briefing.omitted_summary_count} summary entries omitted)")
            )
        hits = () if briefing.recall is None else briefing.recall.data.get("hits", ())
        lines.extend(
            [
                Line(""),
                Line(
                    f"recall hits: {len(cast(tuple[object, ...], hits))}   "
                    f"history packets: {len(briefing.history)}   "
                    f"change references: {len(briefing.changes)}"
                ),
            ]
        )
        if briefing.omitted_history_fact_ids:
            lines.append(
                Line(
                    "omitted history: "
                    + ", ".join(str(i) for i in briefing.omitted_history_fact_ids)
                )
            )
        return lines

    def _turn_lines(self, write: SessionAttempt, outcome: WriteOutcome) -> list[Line]:
        """remember and resume, in the CLI's own state vocabulary."""
        status = outcome.status
        if status == "saved":
            return [
                Line("Saved: committed custody receipt.", "header"),
                Line(f"state: committed   receipt: {outcome.receipt_outcome}"),
                Line(
                    "fact_ids: "
                    + (", ".join(outcome.fact_ids) if outcome.fact_ids else "-")
                ),
                Line(f"custody idempotency key: {_optional(outcome.custody_key)}"),
                Line(f"mutation_id: {_optional(outcome.mutation_id)}"),
                Line(
                    "Saved facts are candidates; that they are searchable is not "
                    "confirmed here."
                ),
                *self._session_identities(write),
            ]
        if status == "skipped":
            return [
                Line("Skipped: no observations, so no facts were saved.", "header"),
                Line("state: skipped"),
                *self._session_identities(write),
            ]
        if status == "abandoned":
            return [
                Line(
                    "Abandoned: this turn was abandoned; nothing was saved.", "header"
                ),
                Line("state: abandoned"),
                *self._session_identities(write),
            ]
        if status == "prepared":
            return [
                Line(
                    "Prepared: Cairn holds the output, but fact custody is not "
                    "confirmed. Nothing is reported as saved.",
                    "header",
                ),
                Line("state: prepared"),
                Line("Press u to resume the same turn (commit). Recovery identities:"),
                *self._session_identities(write),
            ]
        return [
            Line(
                "Interrupted: the turn is started only and its output is not "
                "prepared. Nothing is saved.",
                "header",
            ),
            Line("state: interrupted (started)"),
            Line(
                "resume cannot recover lost output. Resubmit the identical "
                "checkpoint (R, from this browser's own checkpoint), or abandon "
                "the turn (b in the session view). Recovery identities:"
            ),
            *self._session_identities(write),
        ]

    def _session_outcome_lines(self, write: SessionAttempt) -> list[Line]:
        outcome = self.write_outcome
        if outcome is None:
            return [Line(f"Sending {write.operation}...")]
        failure = f"{outcome.operation}: {outcome.code}"
        name = _WRITE_NAMES[write.operation]
        title = name[0].upper() + name[1:]
        done = _WRITE_DONE[write.operation]
        stage = Line(f"last confirmed stage: {_optional(outcome.stage)}")
        if outcome.status not in _OPEN and write.key in self.unresolved_writes:
            return [
                Line(f"Retry did not complete ({failure}).", "header"),
                Line(
                    "The earlier outcome is still unknown; nothing is reported "
                    f"as {done}. Recovery identities:"
                ),
                *self._session_identities(write),
            ]
        if outcome.status in {
            "saved",
            "skipped",
            "abandoned",
            "prepared",
            "interrupted",
        }:
            return self._turn_lines(write, outcome)
        if outcome.status == "recorded":
            if isinstance(write, ArrivalAttempt):
                assert outcome.arrival is not None
                snapshot = outcome.arrival.visit.snapshot
                return [
                    Line("Briefing. The visit is NOT acknowledged.", "header"),
                    Line(
                        f"visit_id: {_optional(outcome.visit_id)}   visit_at: "
                        + (
                            "-"
                            if snapshot.visit_at is None
                            else snapshot.visit_at.isoformat()
                        )
                    ),
                    Line(f"visit receipt: {outcome.receipt_outcome}"),
                    Line(
                        "After reading, acknowledge it with v in the session view "
                        "(a separate confirmed step)."
                    ),
                    Line(""),
                    *self._briefing_lines(outcome.arrival),
                ]
            state = "-" if outcome.snapshot is None else outcome.snapshot.state
            claim = (
                f"Visit {write.visit_id} acknowledged."
                if isinstance(write, AcknowledgementAttempt)
                else f"Turn {write.key} abandoned."
            )
            return [
                Line(claim, "header"),
                Line(
                    f"Operational receipt ({outcome.receipt_outcome}); this saves no facts."
                ),
                Line(f"session state: {state}"),
                Line(f"mutation_id: {_optional(outcome.mutation_id)}"),
                Line(
                    f"audit event: {_optional(outcome.audit_event_id)} "
                    f"recorded {_optional(outcome.audit_recorded_at)}"
                ),
            ]
        if outcome.status == "refused":
            lines = [
                Line(f"{title} refused by Cairn ({failure}).", "header"),
                stage,
                Line(f"Nothing is reported as {done}."),
            ]
            if isinstance(write, ArrivalAttempt):
                lines.append(Line("No visit is acknowledged."))
            return lines
        if outcome.status == "failed":
            lines = [
                Line(f"{title} not completed ({failure}).", "header"),
                stage,
                Line(f"No receipt was returned; nothing is reported as {done}."),
            ]
            if isinstance(write, ArrivalAttempt):
                lines.append(
                    Line(
                        "A visit may have been issued; it is not acknowledged. "
                        "Press r in the session view to read the status."
                    )
                )
            else:
                lines.extend([Line(""), *self._session_identities(write)])
            return lines
        resume = isinstance(write, (RememberAttempt, ResumeAttempt))
        return [
            Line(f"{title} outcome unknown ({failure}).", "header"),
            Line("The request may have reached Cairn."),
            Line(f"Nothing is reported as {done}, and nothing is reported as failed."),
            stage,
            Line(f"recovery: {_optional(outcome.recovery)}"),
            Line("Recovery identities:"),
            *self._session_identities(write),
            Line(""),
            Line(
                "Press R to resubmit identically (same identities)"
                + ("; u resumes the same turn" if resume else "")
                + ". On quitting, the exact cairn-memory command and its stdin "
                "JSON are printed; content is not repeated there, so keep it."
            ),
        ]

    def _reason_lines(self, label: str, reason: str) -> list[Line]:
        return [
            Line(f"{label}:"),
            *(Line("  " + part) for part in wrap(reason, self.width - 2)),
        ]

    def _classification(self) -> str:
        connection = self.connection
        level = None if connection is None else connection.classification
        return "fixed by the profile" if level is None else level.value

    def _write_confirm_lines(self, write: WriteAttempt) -> list[Line]:
        """Everything that will be sent, IDs in full, before y."""
        if isinstance(write, SESSION_ATTEMPTS):
            return self._session_confirm_lines(write)
        scope = scope_label(self.scope)
        if isinstance(write, CorrectionAttempt):
            lines = [
                Line("Invalidate this fact? Its history is kept.", "header"),
                Line(""),
                Line("operation: correct"),
                Line(f"fact to invalidate: {write.fact_id}"),
                Line(f"  first line (untrusted text): {write.fact_line}"),
            ]
            if write.superseded_by is None:
                lines.append(Line("superseded_by: none (no replacement named)"))
            else:
                lines.extend(
                    [
                        Line(f"superseded_by (the marked fact): {write.superseded_by}"),
                        Line(f"  first line (untrusted text): {write.superseded_line}"),
                    ]
                )
            lines.extend(
                [
                    Line(f"scope: {scope} (the profile's exact scope)"),
                    *self._reason_lines("reason", write.reason),
                    Line(""),
                    Line(
                        "Cairn records the correction and the fact leaves the "
                        "current listing; the replacement, if any, is unchanged."
                    ),
                    Line(f"idempotency key: {write.key}"),
                ]
            )
            return lines
        if isinstance(write, DisagreementAttempt):
            return [
                Line("Record a disagreement between these two facts?", "header"),
                Line(""),
                Line("operation: disagree"),
                Line(f"left fact (the marked fact): {write.left_fact_id}"),
                Line(f"  first line (untrusted text): {write.left_line}"),
                Line(f"right fact: {write.right_fact_id}"),
                Line(f"  first line (untrusted text): {write.right_line}"),
                Line(f"scope: {scope}   classification: {self._classification()}"),
                *self._reason_lines("reason", write.reason),
                Line(""),
                Line(
                    "Neither fact is changed, invalidated or re-trusted; Cairn "
                    "records a disagreement between them."
                ),
                Line(f"idempotency key: {write.key}"),
            ]
        return [
            Line("Reject this pending proposal?", "header"),
            Line(""),
            Line("operation: proposal-reject"),
            Line(f"proposal: {write.proposal_id} (re-read just now: pending)"),
            Line(
                f"  proposal reason, first line (untrusted text): {write.proposal_line}"
            ),
            Line(f"source fact: {write.source_fact_id} (left unchanged)"),
            Line(f"target scope: {scope_label(write.target_scope)}"),
            Line(f"scope: {scope}"),
            *self._reason_lines("rejection reason", write.reason),
            Line(""),
            Line(
                "Cairn records the rejection; nothing is published and the source "
                "fact is not invalidated."
            ),
            Line(f"idempotency key: {write.key}"),
        ]

    def _write_identities(self, write: WriteAttempt) -> list[Line]:
        if isinstance(write, SESSION_ATTEMPTS):
            return self._session_identities(write)
        scope = scope_label(self.scope)
        if isinstance(write, CorrectionAttempt):
            lines = [
                Line("operation: correct"),
                Line(f"fact_ids: {write.fact_id}"),
                Line(f"superseded_by: {_optional(write.superseded_by)}"),
            ]
        elif isinstance(write, DisagreementAttempt):
            lines = [
                Line("operation: disagree"),
                Line(f"left_fact_id: {write.left_fact_id}"),
                Line(f"right_fact_id: {write.right_fact_id}"),
            ]
        else:
            lines = [
                Line("operation: proposal-reject"),
                Line(f"proposal_id: {write.proposal_id}"),
            ]
        return [
            *lines,
            Line(f"scope: {scope}"),
            Line(f"idempotency_key: {write.key}"),
            *self._reason_lines("reason (resubmit exactly this text)", write.reason),
        ]

    def _write_outcome_lines(self, write: WriteAttempt) -> list[Line]:
        if isinstance(write, SESSION_ATTEMPTS):
            return self._session_outcome_lines(write)
        outcome = self.write_outcome
        name = _WRITE_NAMES[write.operation]
        title = name[0].upper() + name[1:]
        if outcome is None:
            return [Line(f"Sending {write.operation}...")]
        failure = f"{outcome.operation}: {outcome.code}"
        done = _WRITE_DONE[write.operation]
        if outcome.status != "uncertain" and write.key in self.unresolved_writes:
            return [
                Line(f"Retry did not complete ({failure}).", "header"),
                Line(
                    "The earlier outcome is still unknown; nothing is reported "
                    f"as {done}. Recovery identities:"
                ),
                *self._write_identities(write),
            ]
        if outcome.status == "recorded":
            if isinstance(write, CorrectionAttempt):
                lines = [
                    Line("Corrected.", "header"),
                    Line(f"invalidated fact: {write.fact_id}"),
                    Line(f"superseded_by: {_optional(write.superseded_by)}"),
                    Line(f"invalidated_at: {_optional(outcome.invalidated_at)}"),
                ]
            elif isinstance(write, DisagreementAttempt):
                lines = [
                    Line("Disagreement recorded.", "header"),
                    Line(f"relationship: {_optional(outcome.relationship_id)}"),
                    Line(f"left fact: {write.left_fact_id} (unchanged)"),
                    Line(f"right fact: {write.right_fact_id} (unchanged)"),
                ]
            else:
                lines = [
                    Line("Proposal rejected.", "header"),
                    Line(f"proposal: {write.proposal_id}"),
                    Line(f"source fact: {write.source_fact_id} (unchanged)"),
                ]
            return [
                *lines,
                Line(f"receipt outcome: {outcome.receipt_outcome}"),
                Line(f"mutation_id: {_optional(outcome.mutation_id)}"),
                Line(
                    f"audit event: {_optional(outcome.audit_event_id)} "
                    f"recorded {_optional(outcome.audit_recorded_at)}"
                ),
                Line(f"idempotency key: {write.key}"),
            ]
        if outcome.status == "refused":
            return [
                Line(f"{title} refused by Cairn ({failure}).", "header"),
                Line("Nothing was changed."),
            ]
        if outcome.status == "failed":
            return [
                Line(f"{title} not completed ({failure}).", "header"),
                Line(
                    f"The {write.operation} request was not sent; nothing was changed."
                ),
                Line(""),
                *self._write_identities(write),
            ]
        return [
            Line(f"{title} outcome unknown ({failure}).", "header"),
            Line("The request may have reached Cairn."),
            Line(f"Nothing is reported as {done}, and nothing is reported as failed."),
            Line("Recovery identities:"),
            *self._write_identities(write),
            Line(""),
            Line(
                f"Press R to resubmit identically (same key). On quitting, the "
                f"cairn-memory {write.operation} command and its stdin JSON are "
                "printed; the reason is not repeated there, so keep it."
            ),
        ]

    def _confirm_lines(self) -> list[Line]:
        if self.write is not None:
            return self._write_confirm_lines(self.write)
        attempt = self.attempt
        assert attempt is not None
        return [
            Line("Promote this candidate to validated?", "header"),
            Line(""),
            Line(f"source fact: {attempt.source_fact_id} (left unchanged)"),
            Line(f"target scope: {scope_label(self.scope)} (same scope)"),
            Line(f"classification: {attempt.classification} (inherited)"),
            Line(f"evidence: {attempt.evidence_id} (the fact's own source evidence)"),
            Line("reason:"),
            *(Line("  " + part) for part in wrap(attempt.reason, self.width - 2)),
            Line(""),
            Line(
                "Cairn records proposal "
                f"{attempt.proposal_id} at this scope, then accepts it, deriving a "
                "new validated fact."
            ),
            Line(f"propose key: {attempt.propose_key}"),
            Line(f"proposal-accept key: {attempt.accept_key}"),
        ]

    def _outcome_lines(self) -> list[Line]:
        if self.write is not None:
            return self._write_outcome_lines(self.write)
        outcome, attempt = self.outcome, self.attempt
        if outcome is None or attempt is None:
            return [Line("Promoting...")]
        identities = [
            Line(f"proposal_id: {attempt.proposal_id}"),
            Line(f"source_fact_id: {attempt.source_fact_id}"),
            Line(f"target_scope: {scope_label(self.scope)}"),
            Line(f"evidence_id: {attempt.evidence_id}"),
            Line(f"target_classification: {attempt.classification}"),
            Line(f"propose idempotency_key: {attempt.propose_key}"),
            Line(f"proposal-accept idempotency_key: {attempt.accept_key}"),
        ]
        failure = f"{outcome.operation}: {outcome.code}"
        if outcome.status != "uncertain" and attempt.proposal_id in self.unresolved:
            return [
                Line(f"Retry did not complete ({failure}).", "header"),
                Line(
                    "The earlier outcome is still unknown; nothing is reported as "
                    "promoted. Recovery identities:"
                ),
                *identities,
            ]
        if outcome.status == "promoted":
            return [
                Line("Promoted.", "header"),
                Line(f"new validated fact: {outcome.derived_fact_id}"),
                Line(f"derived from: {attempt.source_fact_id} (unchanged)"),
                Line(f"evidence: {attempt.evidence_id}"),
                Line(f"proposal: {attempt.proposal_id}"),
                Line(f"receipt outcome: {outcome.receipt_outcome}"),
            ]
        if outcome.status == "refused":
            lines = [
                Line(f"Promotion refused by Cairn ({failure}).", "header"),
                Line("Nothing was promoted."),
            ]
            if outcome.proposal_recorded:
                lines.append(
                    Line(
                        f"Proposal {attempt.proposal_id} was recorded and remains pending."
                    )
                )
            return lines
        if outcome.status == "failed":
            return [
                Line(f"Promotion not completed ({failure}).", "header"),
                Line(
                    f"The {outcome.operation} request was not sent; nothing is reported as promoted."
                ),
                *(
                    [
                        Line(
                            f"Proposal {attempt.proposal_id} was recorded and remains pending."
                        )
                    ]
                    if outcome.proposal_recorded
                    else []
                ),
                Line(""),
                *identities,
            ]
        return [
            Line(f"Promotion outcome unknown ({failure}).", "header"),
            Line("Nothing is reported as promoted. Recovery identities:"),
            *identities,
            Line(""),
            Line(
                "Recover later if needed: propose then proposal-accept with these "
                "identities, keys and the same reason."
            ),
        ]

    def _prompt(self) -> str:
        """Pinned controls for confirm/outcome; otherwise the message line."""
        write = self.write
        if self.mode == "confirm" and write is not None:
            verb = {
                "correct": "correct",
                "disagree": "record the disagreement",
                "proposal-reject": "reject the proposal",
                "arrive": "open the session and issue a visit",
                "acknowledge-visit": "acknowledge the visit",
                "remember": "send the checkpoint",
                "resume": "resume the turn",
                "abandon": "abandon the turn",
            }[write.operation]
            return f"Press y to {verb}; Esc or n cancels."
        if self.mode == "confirm":
            return "Press y to promote; Esc or n cancels."
        if self.mode == "outcome" and (
            self.outcome is not None
            if write is None
            else self.write_outcome is not None
        ):
            hint = (
                "R resubmits identically (same keys); Esc returns."
                if self._retryable()
                else "Esc returns."
            )
            if (
                isinstance(write, (RememberAttempt, ResumeAttempt))
                and write.key in self.unresolved_writes
            ):
                hint = "R resubmits identically; u resumes the turn; Esc returns."
            return hint
        return self.message

    def _footer(self) -> str:
        if self.mode == "input":
            return "Enter submit  Esc cancel  Backspace delete"
        if self.mode == "evidence":
            return "j/k scroll  n next window  b previous  Esc back"
        if self.mode == "detail":
            fact = self.fact
            parts = ["j/k scroll", "g/G top/end"]
            if fact and fact.get("source_evidence_id"):
                parts.append("e evidence")
            parts.extend(self._fact_write_parts(fact))
            if self.promotion_offered(fact):
                parts.append("p promote")
            if self.may_retrieve:
                parts.append("s suggest")
            parts.append("Esc back")
            return _fit(parts, ("g/G top/end", "j/k scroll", "m mark"), self.width - 1)
        if self.mode in {"confirm", "outcome"}:
            return "j/k PgUp/PgDn g/G scroll"
        if self.mode == "proposal":
            reject = "  x reject" if self.rejection_offered(self.proposal) else ""
            return f"j/k scroll  g/G top/end{reject}  Esc back"
        if self.mode == "suggestions":
            return "j/k scroll  g/G top/end  Esc back"
        if self.mode == "connection":
            return "j/k scroll  g/G top/end  r re-read  Esc back"
        if self.mode == "session":
            if self.session_id is None:
                return "Esc back"
            parts = ["j/k scroll"]
            if self.may_retrieve:
                parts.append("r status")
            if self.may_session_write:
                parts.extend(["a arrive", "v ack visit", "w remember"])
                parts.extend(["u resume", "b abandon"])
            parts.append("Esc back")
            return _fit(parts, ("j/k scroll",), self.width - 1)
        if self.mode == "proposals":
            pending = self.pending
            selected = (
                pending[self.proposal_selected]
                if 0 <= self.proposal_selected < len(pending)
                else None
            )
            reject = "  x reject" if self.rejection_offered(selected) else ""
            return f"j/k move  PgUp/PgDn  g/G  Enter open  r refresh{reject}  Esc back"
        # Movement keys are omitted; on a narrow terminal the most conventional
        # bindings are left out too, so the footer fits (80 columns or more).
        current = self._current()
        parts = ["Enter open", "/ search", "f filter", "r refresh"]
        if self.may_retrieve and current is not None:
            parts.append("s suggest")
        parts.append("c connection")
        parts.append("S session")
        parts.extend(self._fact_write_parts(current))
        if self.promotion_offered(current):
            parts.append("p promote")
        if self.may_retrieve:
            parts.append("P proposals")
        parts.append("q quit")
        return _fit(
            parts,
            (
                "Enter open",
                "r refresh",
                "f filter",
                "/ search",
                "c connection",
                "S session",
                "s suggest",
            ),
            self.width - 1,
        )

    def _fact_write_parts(self, fact: Fact | None) -> list[str]:
        """Only the write keys this fact and the known grants allow."""
        parts: list[str] = []
        if fact is not None and (self.may_correct or self.may_disagree):
            parts.append("m mark")
        if self.correction_offered(fact):
            parts.append("x correct")
        if self.supersession_offered(fact):
            parts.append("X supersede")
        if self.disagreement_offered(fact):
            parts.append("d disagree")
        return parts

    def render(self) -> list[Line]:
        height, width = self.height, self.width
        body: list[Line]
        if self.mode == "input":
            count = len(self.draft_observations)
            label = (
                "Search (empty lists newest first): "
                if self.input_kind == "search"
                else "Arrival query (required, 1..8192 bytes; Esc cancels): "
                if self.input_kind == "query"
                else f"Observation {count + 1} of at most {MAX_OBSERVATIONS} "
                "(1..4096 bytes; Enter on an empty line ends; Esc cancels): "
                if self.input_kind == "observation"
                else f"Response for the checkpoint with {count} observations "
                "(may be empty, up to 32768 bytes; Esc cancels): "
                if self.input_kind == "response"
                else {
                    "promote": "Promotion reason (required): ",
                    "correct": "Correction reason (required; Esc cancels): ",
                    "disagree": "Disagreement reason (required; Esc cancels): ",
                    "proposal-reject": "Rejection reason (required; Esc cancels): ",
                    "abandon": "Abandonment reason (required; Esc cancels): ",
                }[self.reason_for]
            )
            body = [
                *(Line(p, "header") for p in wrap(label, width)),
                *(Line(p) for p in wrap(self.input_text + "_", width)),
            ]
            body = body[-self.body_height :]
        elif self.mode in {"confirm", "outcome"}:
            body = self._screen_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "detail":
            body = self._detail_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "evidence":
            body = self._evidence_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "proposal":
            body = self._proposal_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "suggestions":
            body = self._suggestion_lines()
            body = body[self.scroll : self.scroll + self.body_height]
        elif self.mode == "connection":
            body = self._connection_lines()
            body = body[self.scroll : self.scroll + self.body_height]
        elif self.mode == "session":
            body = self._session_lines()
            body = body[self.scroll : self.scroll + self.body_height]
        elif self.mode == "proposals":
            pending = self.pending
            self._follow_proposals()
            top = self.proposal_top
            body = [
                Line(
                    self._proposal_row(proposal),
                    "selected" if index == self.proposal_selected else "normal",
                )
                for index, proposal in enumerate(
                    pending[top : top + self.body_height], top
                )
            ]
            if self.proposal_error is not None:
                body = [
                    Line(
                        f"(proposals unavailable: proposal-list failed: {self.proposal_error})"
                    )
                ]
            elif not pending:
                body = [
                    Line(
                        "(no pending proposals loaded; press G to load further)"
                        if self.proposal_cursor is not None
                        else "(no pending proposals)"
                    )
                ]
        else:
            view = self.view
            self._follow()
            body = [
                Line(
                    self._row(fact), "selected" if index == self.selected else "normal"
                )
                for index, fact in enumerate(
                    view[self.top : self.top + self.body_height], self.top
                )
            ]
            if not view:
                body = [
                    Line("(no facts match the filter)" if self.rows else "(no facts)")
                ]
        lines = [Line(self._header(), "header"), *body[: self.body_height]]
        lines.extend(Line("") for _ in range(height - 2 - len(lines)))
        lines.append(Line(self._prompt(), "status"))
        lines.append(Line(self._footer(), "dim"))
        return [
            Line(clip(sanitise(line.text), width - 1), line.style)
            for line in lines[:height]
        ]
