"""Pure model and controller for ``cairn-memory browse``.

Nothing here touches curses, the network or a clock. An injected
``BrowseBackend`` performs bounded reads and the explicit promotion, and
``render`` returns already sanitised, width-clipped lines for a thin terminal
adapter. Memory text is untrusted data: every drawn line passes through
``sanitise``, which replaces control, format and separator characters with
visible escapes, so stored text can never drive the terminal.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from cairn.authority.custody import CustodyValueError, validate_reason
from cairn.catalogue.audit import Scope

type Fact = Mapping[str, object]
type Style = Literal["normal", "selected", "header", "status", "dim"]
type Mode = Literal["list", "detail", "evidence", "input", "confirm", "outcome"]

MAX_QUERY_BYTES = 8192
MAX_REASON_BYTES = 4096
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


class BrowseBackend(Protocol):
    async def page(self, query: str | None, cursor: str | None) -> Page: ...

    async def history(self, fact_id: UUID) -> Fact: ...

    async def evidence(self, evidence_id: UUID, start: int) -> Fact: ...

    async def promote(self, attempt: PromotionAttempt) -> PromotionOutcome: ...


def _text(fact: Fact, name: str) -> str:
    value = fact.get(name)
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


class BrowseController:
    """Terminal-independent state, key handling, paging and line rendering."""

    def __init__(
        self,
        backend: BrowseBackend,
        *,
        scope: Scope,
        may_promote: bool,
        identities: Callable[[], UUID] = uuid4,
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.may_promote = may_promote
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
        self.input_kind: Literal["search", "reason"] | None = None
        self.input_text = ""
        self.input_return: Mode = "list"
        self.attempt: PromotionAttempt | None = None
        self.attempt_return: Mode = "list"
        self.outcome: PromotionOutcome | None = None
        self.unresolved: dict[UUID, tuple[PromotionAttempt, PromotionOutcome]] = {}

    # Model helpers -------------------------------------------------------

    @property
    def view(self) -> list[Fact]:
        if self.filter == "all":
            return self.rows
        return [row for row in self.rows if row.get("trust") == self.filter]

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

    # Input ---------------------------------------------------------------

    def _start_input(self, kind: Literal["search", "reason"], text: str) -> None:
        self.input_return = self.mode
        self.mode, self.input_kind, self.input_text = "input", kind, text

    async def _input_key(self, key: str) -> None:
        limit = MAX_QUERY_BYTES if self.input_kind == "search" else MAX_REASON_BYTES
        if key == "esc":
            self.mode, self.input_kind = self.input_return, None
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
        if not text.strip():
            self.message = "a reason is required"
            return
        try:
            validate_reason(text)
        except CustodyValueError:
            self.message = "the reason must be 1..4096 UTF-8 bytes"
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

    def _begin_promotion(self, fact: Fact | None) -> None:
        if self.unresolved:
            # One unknown outcome at a time: reopen it rather than start another.
            attempt, outcome = list(self.unresolved.values())[-1]
            self.attempt_return = self.mode
            self.attempt, self.outcome = attempt, outcome
            self.mode, self.scroll = "outcome", 0
            self.message = (
                "an earlier promotion outcome is unknown; resolve it before "
                "starting another"
            )
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
            self._start_input("reason", "")

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
            self.mode, self.attempt = self.attempt_return, None
            if self.mode == "list":
                self.fact = None
            self.message = "promotion cancelled; nothing was sent"

    async def _execute(self) -> None:
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

    async def _outcome_key(self, key: str) -> None:
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
        return f"{marker} {recorded} {ancestor}{_first_line(fact.get('body'))}"

    def _header(self) -> str:
        if self.mode == "detail":
            return "Fact detail"
        if self.mode == "confirm" and self.attempt is not None:
            return f"Confirm: promote {self.attempt.source_fact_id} to validated"
        if self.mode == "outcome" and self.attempt is not None:
            return f"Promotion of {self.attempt.source_fact_id}"
        if self.mode == "evidence":
            return "Source evidence (untrusted text, not a fact)"
        listing = f"search: {self.query}" if self.query else "newest first"
        more = "+" if self.cursor is not None else ""
        partial = "" if self.selection_complete else "  selection incomplete"
        return (
            f"Cairn {scope_label(self.scope)}  {listing}  filter: {self.filter}  "
            f"{len(self.view)}/{len(self.rows)}{more} loaded{partial}"
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

    def _confirm_lines(self) -> list[Line]:
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
        if self.mode == "confirm":
            return "Press y to promote; Esc or n cancels."
        if self.mode == "outcome" and self.outcome is not None:
            hint = (
                "R resubmits identically (same keys); Esc returns."
                if self._retryable()
                else "Esc returns."
            )
            return hint
        return self.message

    def _footer(self) -> str:
        if self.mode == "input":
            return "Enter submit  Esc cancel  Backspace delete"
        if self.mode == "evidence":
            return "j/k scroll  n next window  b previous  Esc back"
        if self.mode == "detail":
            promote = "  p promote" if self.promotion_offered(self.fact) else ""
            evidence = (
                "  e evidence"
                if self.fact and self.fact.get("source_evidence_id")
                else ""
            )
            return f"j/k scroll  g/G top/end{evidence}{promote}  Esc back"
        if self.mode in {"confirm", "outcome"}:
            return "j/k PgUp/PgDn g/G scroll"
        promote = "  p promote" if self.promotion_offered(self._current()) else ""
        return f"j/k move  PgUp/PgDn  g/G  Enter open  / search  f filter  r refresh{promote}  q quit"

    def render(self) -> list[Line]:
        height, width = self.height, self.width
        body: list[Line]
        if self.mode == "input":
            label = (
                "Search (empty lists newest first): "
                if self.input_kind == "search"
                else "Promotion reason (required): "
            )
            body = [
                Line(label, "header"),
                *(Line(p) for p in wrap(self.input_text + "_", width)),
            ]
            body = body[-self.body_height :]
        elif self.mode in {"confirm", "outcome"}:
            body = self._screen_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "detail":
            body = self._detail_lines()[self.scroll : self.scroll + self.body_height]
        elif self.mode == "evidence":
            body = self._evidence_lines()[self.scroll : self.scroll + self.body_height]
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
