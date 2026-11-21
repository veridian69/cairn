"""Thin curses adapter and MemoryClient backend for ``cairn-memory browse``.

All state and drawing decisions live in ``cairn.client.browse``. This module
maps keys, draws pre-sanitised lines and calls the existing bounded client
operations: recall-page, history, evidence-window, and the explicit proposal
path (propose, then proposal-accept) for promotion.
"""

from __future__ import annotations

import curses
import locale
import os
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from cairn.catalogue.audit import Classification
from cairn.client.browse import (
    BrowseController,
    BrowseFailure,
    Fact,
    Line,
    Page,
    PromotionAttempt,
    PromotionOutcome,
)
from cairn.client.errors import MemoryOperationFailure

if TYPE_CHECKING:
    from cairn.client.memory import MemoryClient

# recall-page requires a non-empty query. With relevant_only false and a
# chronological order, the query neither selects nor ranks: every current,
# readable fact at the profile scope and its ancestors is listed.
LISTING_QUERY = "*"
PAGE_LIMIT = 50
PAGE_BUDGET = 65536
HISTORY_BUDGET = 65536
WINDOW_BUDGET = 16384


def _failure(error: MemoryOperationFailure) -> BrowseFailure:
    detail = dict(error.failure.detail or ())
    reason = detail.get("reason")
    return BrowseFailure(
        error.operation, error.failure.code, reason if type(reason) is str else None
    )


class MemoryBrowseBackend:
    """Calls the profile-bound MemoryClient; ``dispatched`` records request
    paths sent on the wire, filled by the caller's HTTP request hook."""

    def __init__(self, client: MemoryClient, dispatched: set[str]) -> None:
        self._client, self._dispatched = client, dispatched

    async def _page(self, query: str | None, cursor: str | None, budget: int) -> Fact:
        client = self._client
        if cursor is not None:
            result = await client.continue_recall(
                cursor, budget=budget, limit=PAGE_LIMIT
            )
        elif query is not None:
            result = await client.recall_page(
                query,
                order="relevance",
                relevant_only=True,
                budget=budget,
                limit=PAGE_LIMIT,
            )
        else:
            result = await client.recall_page(
                LISTING_QUERY,
                order="newest",
                time_basis="recorded",
                relevant_only=False,
                budget=budget,
                limit=PAGE_LIMIT,
            )
        return result.data

    async def page(self, query: str | None, cursor: str | None) -> Page:
        try:
            try:
                data = await self._page(query, cursor, PAGE_BUDGET)
            except MemoryOperationFailure as error:
                detail = dict(error.failure.detail or ())
                minimum = detail.get("minimum_budget")
                if (
                    detail.get("reason") != "page_budget_too_small"
                    or type(minimum) is not int
                ):
                    raise
                data = await self._page(query, cursor, minimum)
        except MemoryOperationFailure as error:
            raise _failure(error) from None
        hits = data["hits"]
        cursor_value = data["next_cursor"]
        assert isinstance(hits, tuple)
        return Page(
            tuple(hits),
            cursor_value if type(cursor_value) is str else None,
            data["selection_complete"] is True,
        )

    async def history(self, fact_id: UUID) -> Fact:
        try:
            return (await self._client.history(fact_id, budget=HISTORY_BUDGET)).data
        except MemoryOperationFailure as error:
            raise _failure(error) from None

    async def evidence(self, evidence_id: UUID, start: int) -> Fact:
        try:
            return (
                await self._client.evidence_window(
                    evidence_id, start=start, budget=WINDOW_BUDGET
                )
            ).data
        except MemoryOperationFailure as error:
            raise _failure(error) from None

    async def promote(self, attempt: PromotionAttempt) -> PromotionOutcome:
        """Propose at the profile scope, then accept with the source evidence.

        Both keys are the attempt's own, so a resubmission replays rather than
        duplicates. Exit-class mapping is the everyday CLI's own.
        """
        from cairn.client.cli import failure_exit

        client = self._client
        self._dispatched.clear()
        recorded = False
        try:
            await client.propose(
                attempt.proposal_id,
                source_fact_id=attempt.source_fact_id,
                target_scope=client.scope,
                reason=attempt.reason,
                idempotency_key=attempt.propose_key,
            )
            recorded = True
            accepted = await client.proposal_accept(
                attempt.proposal_id,
                evidence_id=attempt.evidence_id,
                target_classification=Classification(attempt.classification),
                idempotency_key=attempt.accept_key,
            )
        except MemoryOperationFailure as error:
            if (
                recorded
                and error.operation == "proposal-read"
                and "/memory/v1/proposal-accept" not in self._dispatched
            ):
                # The proposal committed and only the acceptance preflight read
                # failed: no promotion request was sent, so the outcome is known.
                return PromotionOutcome(
                    "failed",
                    "proposal-accept",
                    error.failure.code,
                    proposal_recorded=True,
                )
            sent = f"/memory/v1/{error.operation}" in self._dispatched
            code = failure_exit(error, "unconfirmed", proposal_dispatched=sent)
            status: Literal["refused", "failed", "uncertain"] = (
                "uncertain" if code == 3 else "failed" if code == 4 else "refused"
            )
            return PromotionOutcome(
                status, error.operation, error.failure.code, proposal_recorded=recorded
            )
        source, derived = accepted.result.promotions[0]
        if source != attempt.source_fact_id:
            return PromotionOutcome(
                "uncertain",
                "proposal-accept",
                "invalid_response",
                proposal_recorded=True,
            )
        return PromotionOutcome(
            "promoted",
            "proposal-accept",
            derived_fact_id=derived,
            receipt_outcome=accepted.outcome,
            proposal_recorded=True,
        )


_KEYS = {
    curses.KEY_UP: "up",
    curses.KEY_DOWN: "down",
    curses.KEY_NPAGE: "pgdn",
    curses.KEY_PPAGE: "pgup",
    curses.KEY_HOME: "home",
    curses.KEY_END: "end",
    curses.KEY_ENTER: "enter",
    curses.KEY_BACKSPACE: "backspace",
    curses.KEY_RESIZE: "resize",
}


def key_name(key: int | str) -> str | None:
    if isinstance(key, int):
        return _KEYS.get(key)
    if key in {"\n", "\r"}:
        return "enter"
    if key == "\x1b":
        return "esc"
    if key in {"\x7f", "\x08"}:
        return "backspace"
    if key in {"\x03", "\x04"}:
        return "quit"
    if len(key) == 1 and key.isprintable():
        return key
    return None


def _draw(screen: curses.window, lines: list[Line]) -> None:
    styles = {
        "normal": curses.A_NORMAL,
        "selected": curses.A_REVERSE,
        "header": curses.A_BOLD,
        "status": curses.A_BOLD,
        "dim": curses.A_DIM,
    }
    screen.erase()
    for row, line in enumerate(lines):
        try:
            screen.addstr(row, 0, line.text, styles[line.style])
        except curses.error:
            pass
    screen.refresh()


async def run_terminal(controller: BrowseController) -> None:
    """Own the terminal for the session and always restore it."""
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        screen = curses.initscr()
    except curses.error:
        raise RuntimeError("terminal_unavailable") from None
    try:
        curses.noecho()
        curses.raw()
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        def redraw() -> None:
            height, width = screen.getmaxyx()
            controller.resize(height, width)
            _draw(screen, controller.render())

        controller.on_busy = redraw
        await controller.start()
        while not controller.done:
            redraw()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            name = key_name(key)
            if name is not None and name != "resize":
                await controller.handle(name)
    except curses.error:
        raise RuntimeError("terminal_failure") from None
    finally:
        try:
            screen.keypad(False)
            curses.noraw()
            curses.echo()
        finally:
            curses.endwin()


def recovery_packet(controller: BrowseController) -> dict[str, object] | None:
    """Content-free identities for every promotion left unconfirmed at exit."""
    if not controller.unresolved:
        return None
    attempts = list(controller.unresolved.values())
    _, last = attempts[-1]
    scope = controller.scope
    return {
        "error": {"code": last.code or "outcome_unknown", "operation": last.operation},
        "last_confirmed_stage": "unconfirmed",
        "recovery": "resubmit_identical_propose_then_proposal-accept_same_idempotency_keys_and_fields",
        "promotions": [
            {
                "proposal_id": str(attempt.proposal_id),
                "source_fact_id": str(attempt.source_fact_id),
                "target_scope": {
                    "realm": scope.realm,
                    "segments": [
                        {"kind": s.kind, "identifier": s.identifier}
                        for s in scope.segments
                    ],
                },
                "evidence_id": str(attempt.evidence_id),
                "target_classification": attempt.classification,
                "propose_idempotency_key": str(attempt.propose_key),
                "proposal_accept_idempotency_key": str(attempt.accept_key),
                "reason": "as entered; not repeated here",
            }
            for attempt, _ in attempts
        ],
    }
