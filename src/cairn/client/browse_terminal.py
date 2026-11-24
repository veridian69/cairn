"""Thin curses adapter and MemoryClient backend for ``cairn-memory browse``.

All state and drawing decisions live in ``cairn.client.browse``. This module
maps keys, draws pre-sanitised lines and calls the existing bounded client
operations: recall-page, history, evidence-window, proposal-list and
proposal-read for the read-only proposal view, suggest and diagnose for the
read-only suggestion and connection views, the explicit proposal path
(propose, then proposal-accept) for promotion, correct, disagree and
proposal-reject for the other confirmed writes, and the everyday CLI's own
session sequence (DurableMemorySession and the turn calls) for status,
arrive, acknowledge-visit, remember, resume and abandon.
"""

from __future__ import annotations

import curses
import locale
import os
import shlex
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from cairn.catalogue.audit import Classification, Scope
from cairn.client.browse import (
    SESSION_ATTEMPTS,
    AbandonAttempt,
    AcknowledgementAttempt,
    ArrivalAttempt,
    BrowseController,
    BrowseFailure,
    CorrectionAttempt,
    DisagreementAttempt,
    Fact,
    Line,
    Page,
    PromotionAttempt,
    PromotionOutcome,
    RememberAttempt,
    ResumeAttempt,
    SessionAttempt,
    WriteAttempt,
    WriteOutcome,
    remember_document,
    session_input,
)
from cairn.client.cli_input import command_key
from cairn.client.durable_session import DurableMemorySession
from cairn.client.errors import MemoryOperationFailure
from cairn.client.proposal_types import ProposalPage, ProposalSnapshot
from cairn.client.session_types import (
    DurableSessionFailure,
    DurableTurnResult,
    SessionOperationResult,
    SessionSnapshot,
)
from cairn.client.session_validation import require_preparation_binding
from cairn.client.suggestion_types import SuggestedMemory
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    PersistenceReceipt,
    PersistenceStatus,
)

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
PROPOSAL_LIMIT = 50
SUGGEST_BUDGET = 16384
SUGGEST_LIMIT = 8


def _failure(error: MemoryOperationFailure) -> BrowseFailure:
    detail = dict(error.failure.detail or ())
    reason = detail.get("reason")
    return BrowseFailure(
        error.operation, error.failure.code, reason if type(reason) is str else None
    )


class MemoryBrowseBackend:
    """Calls the profile-bound MemoryClient; ``dispatched`` records request
    paths sent on the wire, filled by the caller's HTTP request hook."""

    def __init__(
        self,
        client: MemoryClient,
        dispatched: set[str],
        *,
        session_id: UUID | None = None,
    ) -> None:
        self._client, self._dispatched = client, dispatched
        self._session_id = session_id

    def _session(self) -> DurableMemorySession:
        # A fresh durable session per command, as each CLI invocation has.
        if self._session_id is None:
            raise BrowseFailure("input", "session_id_required")
        return DurableMemorySession(self._client, session_id=self._session_id)

    async def session_status(self, turn_id: UUID | None) -> SessionSnapshot:
        """status, with or without turn_id: session-read only; it never
        opens a session."""
        session = self._session()
        try:
            return await session.status(turn_id)
        except MemoryOperationFailure as error:
            raise _failure(error) from None

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

    async def proposals(self, after: UUID | None) -> ProposalPage:
        try:
            return await self._client.proposal_list(limit=PROPOSAL_LIMIT, after=after)
        except MemoryOperationFailure as error:
            raise _failure(error) from None

    async def proposal(self, proposal_id: UUID) -> ProposalSnapshot:
        try:
            return await self._client.proposal_read(proposal_id)
        except MemoryOperationFailure as error:
            raise _failure(error) from None

    async def suggest(self, fact_id: UUID) -> SuggestedMemory:
        """Read-only evidence for one fact; nothing here acts on a suggestion."""
        try:
            return await self._client.suggest(
                fact_ids=(fact_id,), budget=SUGGEST_BUDGET, limit=SUGGEST_LIMIT
            )
        except MemoryOperationFailure as error:
            raise _failure(error) from None

    async def diagnose(self) -> ConnectionDiagnostics:
        """The same expected-instance diagnosis as ``check``; not ready fails."""
        client = self._client
        result = await client.diagnose(expected_instance_id=client.expected_instance_id)
        if result.status is not ConnectionStatus.READY:
            code = (
                result.failure.code
                if result.failure is not None
                else "connection_refused"
            )
            raise BrowseFailure("diagnose", code)
        return result

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

    async def write(self, attempt: WriteAttempt) -> WriteOutcome:
        """One correct, disagree or proposal-reject with the attempt's own key.

        Exit-class mapping is the everyday CLI's own ``failure_exit``, given
        the same dispatch evidence the CLI records: whether this request's
        path was sent on the wire. That makes correct unknown on every
        uncertain code (the CLI does not track its dispatch), and disagree and
        proposal-reject unknown only once their own request was sent; a
        failure in proposal-reject's diagnose preflight is therefore unsent.
        """
        from cairn.client.cli import failure_exit

        if isinstance(attempt, SESSION_ATTEMPTS):
            return await self._session_write(attempt)
        client = self._client
        path = f"/memory/v1/{attempt.operation}"
        self._dispatched.clear()
        try:
            if isinstance(attempt, CorrectionAttempt):
                receipt = await client.correct(
                    (attempt.fact_id,),
                    reason=attempt.reason,
                    superseded_by=attempt.superseded_by,
                    idempotency_key=attempt.key,
                )
                return _persisted(attempt.operation, receipt, "invalidated_at")
            if isinstance(attempt, DisagreementAttempt):
                receipt = await client.disagree(
                    attempt.left_fact_id,
                    attempt.right_fact_id,
                    reason=attempt.reason,
                    idempotency_key=attempt.key,
                )
                return _persisted(attempt.operation, receipt, "relationship_id")
            rejected = await client.proposal_reject(
                attempt.proposal_id, reason=attempt.reason, idempotency_key=attempt.key
            )
        except MemoryOperationFailure as error:
            sent = path in self._dispatched
            code = failure_exit(error, "unconfirmed", proposal_dispatched=sent)
            status: Literal["refused", "failed", "uncertain"] = (
                "uncertain" if code == 3 else "failed" if code == 4 else "refused"
            )
            return WriteOutcome(status, error.operation, error.failure.code)
        except (OSError, ValueError, TypeError, RuntimeError):
            # Not expected from the client; never claim an outcome it hides.
            return WriteOutcome(
                "uncertain" if path in self._dispatched else "failed",
                attempt.operation,
                "operational_failure",
            )
        if rejected.result.proposal_id != attempt.proposal_id:
            return WriteOutcome("uncertain", attempt.operation, "invalid_response")
        return WriteOutcome(
            "recorded",
            attempt.operation,
            receipt_outcome=rejected.outcome,
            mutation_id=_field(rejected.mutation_receipt, "mutation_id"),
            audit_event_id=_field(rejected.audit_receipt, "event_id"),
            audit_recorded_at=_field(rejected.audit_receipt, "recorded_at"),
        )

    async def _session_write(self, attempt: SessionAttempt) -> WriteOutcome:
        """The CLI's own session sequence (cli.py execute, SESSION_COMMANDS),
        its stage tracking, failure_exit and recovery values."""
        session = self._session()
        session_id = self._session_id
        assert session_id is not None
        client = self._client
        self._dispatched.clear()
        stage = "unconfirmed"
        try:
            if isinstance(attempt, ArrivalAttempt):
                value = session_input("arrive", {"query": attempt.query})
                if value is None:
                    return WriteOutcome("refused", "input", "invalid_input")
                opened = await session.open()
                stage = opened.snapshot.state
                arrival = await session.arrive(
                    value.query,
                    budget=value.budget,
                    history_fact_ids=value.history_fact_ids,
                )
                snapshot = arrival.visit.snapshot
                return WriteOutcome(
                    "recorded",
                    "arrive",
                    receipt_outcome=arrival.visit.outcome,
                    mutation_id=_field(arrival.visit.mutation_receipt, "mutation_id"),
                    stage=snapshot.state,
                    visit_id=None
                    if snapshot.visit_id is None
                    else str(snapshot.visit_id),
                    arrival=arrival,
                    snapshot=snapshot,
                )
            if isinstance(attempt, AcknowledgementAttempt):
                return _operation(
                    attempt.operation,
                    await session.acknowledge_visit(attempt.visit_id),
                )
            if isinstance(attempt, AbandonAttempt):
                return _operation(
                    attempt.operation,
                    await session.abandon(attempt.turn_id, reason=attempt.reason),
                )
            if isinstance(attempt, ResumeAttempt):
                return _turn(attempt.operation, await session.resume(attempt.turn_id))
            value = session_input("remember", remember_document(attempt))
            if value is None or value.turn is None:
                return WriteOutcome("refused", "input", "invalid_input")
            opened = await session.open()
            stage = opened.snapshot.state
            begun = await client.begin_turn(
                session_id,
                attempt.turn_id,
                attempt_id=attempt.attempt_id,
                replaces_turn_id=None,
                idempotency_key=command_key(
                    "begin", session_id, attempt.turn_id, attempt.attempt_id
                ),
            )
            stage = begun.snapshot.state
            prepared = await client.prepare_turn(
                session_id,
                attempt.turn_id,
                value.turn,
                attempt_id=attempt.attempt_id,
                idempotency_key=command_key(
                    "prepare", session_id, attempt.turn_id, attempt.attempt_id
                ),
            )
            stage = prepared.snapshot.state
            result = await session.resume(attempt.turn_id)
            require_preparation_binding(prepared.snapshot, result.snapshot)
            return _turn(attempt.operation, result)
        except MemoryOperationFailure as error:
            return self._session_failure(attempt, error, stage)
        except (OSError, ValueError, TypeError, RuntimeError, AssertionError):
            # The CLI's exit 4 class; never claim an outcome it hides.
            sent = bool(self._dispatched) and not isinstance(attempt, ArrivalAttempt)
            return WriteOutcome(
                "uncertain" if sent else "failed",
                attempt.operation,
                "operational_failure",
                stage=stage,
                recovery=_STATUS_THEN_RESUME if sent else None,
            )

    def _session_failure(
        self, attempt: SessionAttempt, error: MemoryOperationFailure, stage: str
    ) -> WriteOutcome:
        from cairn.client.cli import failure_exit

        if (
            isinstance(error, DurableSessionFailure)
            and error.last_confirmed_stage != "unconfirmed"
        ):
            stage = error.last_confirmed_stage
        code = failure_exit(error, stage)
        recovery: str | None = None
        if (
            attempt.operation == "remember"
            and stage in {"open", "started"}
            and error.operation in {"turn-begin", "turn-prepare"}
            and code in {3, 4}
        ):
            recovery = "resubmit_identical_checkpoint_same_identities"
        elif code == 3:
            recovery = _STATUS_THEN_RESUME
        elif (
            code == 4
            and isinstance(attempt, (AcknowledgementAttempt, AbandonAttempt))
            and f"/memory/v1/{error.operation}" in self._dispatched
        ):
            # The CLI exits 4 here with no recovery value. The mutation was
            # sent and its effect is unknown, so the browser keeps it as
            # unresolved; its server-derived key makes a resubmission exact.
            recovery = _BROWSE_RECOVERY[attempt.operation]
        status: Literal["refused", "failed", "uncertain"] = (
            "uncertain"
            if recovery is not None
            else "refused"
            if code == 2
            else "failed"
        )
        return WriteOutcome(
            status, error.operation, error.failure.code, stage=stage, recovery=recovery
        )


_STATUS_THEN_RESUME = "status_then_resume_without_regeneration"
# Browser-only recovery values, for the cases the CLI reports as exit 4.
_BROWSE_RECOVERY = {
    "acknowledge-visit": "resubmit_identical_acknowledge-visit_same_visit_id",
    "abandon": "resubmit_identical_abandon_same_turn_id_and_reason",
}


def _operation(
    operation: str, result: SessionOperationResult, stage: str | None = None
) -> WriteOutcome:
    """An operational receipt (visit, acknowledgement, abandonment); never a
    saved claim."""
    return WriteOutcome(
        "recorded",
        operation,
        receipt_outcome=result.outcome,
        mutation_id=_field(result.mutation_receipt, "mutation_id"),
        audit_event_id=_field(result.audit_receipt, "event_id"),
        audit_recorded_at=_field(result.audit_receipt, "recorded_at"),
        stage=result.snapshot.state if stage is None else stage,
        snapshot=result.snapshot,
    )


def _turn(operation: str, result: DurableTurnResult) -> WriteOutcome:
    """The CLI's exit mapping: committed/skipped 0, prepared/interrupted 3,
    abandoned 2. Saved needs a committed custody receipt with its result."""
    state, snapshot = result.state, result.snapshot
    persistence = result.persistence
    if state == "committed":
        if (
            persistence is None
            or persistence.status is not PersistenceStatus.COMMITTED
            or persistence.result is None
            or persistence.mutation_receipt is None
        ):
            return WriteOutcome(
                "uncertain",
                operation,
                "invalid_response",
                stage=snapshot.state,
                recovery=_STATUS_THEN_RESUME,
            )
        fact_ids = persistence.result.get("fact_ids")
        return WriteOutcome(
            "saved",
            operation,
            receipt_outcome=persistence.status.value,
            mutation_id=_field(persistence.mutation_receipt, "mutation_id"),
            audit_event_id=_field(persistence.audit_receipt, "event_id"),
            audit_recorded_at=_field(persistence.audit_receipt, "recorded_at"),
            stage=snapshot.state,
            fact_ids=tuple(str(i) for i in fact_ids)
            if isinstance(fact_ids, tuple | list)
            else (),
            custody_key=None
            if persistence.idempotency_key is None
            else str(persistence.idempotency_key),
            snapshot=snapshot,
        )
    if state in {"skipped", "abandoned"}:
        return WriteOutcome(state, operation, stage=snapshot.state, snapshot=snapshot)
    # prepared or interrupted: exit 3, nothing saved.
    return WriteOutcome(
        state,
        operation,
        stage=snapshot.state,
        recovery=_STATUS_THEN_RESUME
        if state == "prepared"
        else "resubmit_identical_checkpoint_same_identities",
        snapshot=snapshot,
    )


def _field(document: object, name: str) -> str | None:
    value = document.get(name) if isinstance(document, Mapping) else None
    return value if type(value) is str else None


def _persisted(operation: str, receipt: PersistenceReceipt, name: str) -> WriteOutcome:
    """A committed or replayed receipt for this exact request; anything else
    is not a receipt and stays unknown."""
    if receipt.status not in {PersistenceStatus.COMMITTED, PersistenceStatus.REPLAYED}:
        return WriteOutcome("uncertain", operation, "invalid_response")
    result = _field(receipt.result, name)
    return WriteOutcome(
        "recorded",
        operation,
        receipt_outcome=receipt.status.value,
        mutation_id=_field(receipt.mutation_receipt, "mutation_id"),
        audit_event_id=_field(receipt.audit_receipt, "event_id"),
        audit_recorded_at=_field(receipt.audit_receipt, "recorded_at"),
        invalidated_at=result if name == "invalidated_at" else None,
        relationship_id=result if name == "relationship_id" else None,
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


_NOT_REPEATED = "as entered; not repeated here"
_RECOVERY = {
    # The everyday CLI's own recovery values for these commands.
    "correct": "resubmit_identical_correction_same_idempotency_key_and_fields",
    "disagree": "resubmit_identical_disagree_same_idempotency_key_and_fields",
    "proposal-reject": "resubmit_identical_proposal-reject_same_idempotency_key_and_fields",
}
_LISTS = {
    "correct": "corrections",
    "disagree": "disagreements",
    "proposal-reject": "proposal_rejections",
    "remember": "checkpoints",
    "resume": "resumes",
    "abandon": "abandonments",
    "acknowledge-visit": "acknowledgements",
    "arrive": "arrivals",
}


def _scope_document(scope: Scope) -> dict[str, object]:
    return {
        "realm": scope.realm,
        "segments": [
            {"kind": s.kind, "identifier": s.identifier} for s in scope.segments
        ],
    }


def _session_recovery(
    write: SessionAttempt, outcome: WriteOutcome, profile: str
) -> dict[str, object]:
    """The command the CLI's recovery value names, with its stdin. Content
    (response, observations, reason) is left out and must be resubmitted
    unchanged; the identities are the attempt's own."""
    recovery = outcome.recovery or _STATUS_THEN_RESUME
    command: str
    stdin: dict[str, object]
    if isinstance(write, AcknowledgementAttempt):
        command, stdin = write.operation, {"visit_id": str(write.visit_id)}
    elif isinstance(write, AbandonAttempt):
        command = write.operation
        stdin = {"turn_id": str(write.turn_id), "reason": _NOT_REPEATED}
    elif isinstance(write, ArrivalAttempt):
        # No turn is known to the arrival; status reports it.
        command, stdin = "status", {}
    elif recovery == "resubmit_identical_checkpoint_same_identities" and isinstance(
        write, RememberAttempt
    ):
        command = write.operation
        stdin = {
            "turn_id": str(write.turn_id),
            "attempt_id": str(write.attempt_id),
            "response": _NOT_REPEATED,
            "observations": _NOT_REPEATED,
        }
    else:
        command, stdin = "resume", {"turn_id": str(write.key)}
    return {
        "operation": write.operation,
        "recovery": recovery,
        "last_confirmed_stage": outcome.stage or "unconfirmed",
        "command": f"cairn-memory --profile {shlex.quote(profile)} {command}",
        "stdin": stdin,
    }


def _write_recovery(
    write: WriteAttempt, outcome: WriteOutcome, profile: str
) -> dict[str, object]:
    """The exact command and stdin for an identical resubmission; the reason
    is left out, as for promotion, and must be the same text."""
    if isinstance(write, SESSION_ATTEMPTS):
        return _session_recovery(write, outcome, profile)
    stdin: dict[str, object]
    if isinstance(write, CorrectionAttempt):
        stdin = {
            "fact_ids": [str(write.fact_id)],
            "reason": _NOT_REPEATED,
            "idempotency_key": str(write.key),
            "superseded_by": None
            if write.superseded_by is None
            else str(write.superseded_by),
        }
    elif isinstance(write, DisagreementAttempt):
        stdin = {
            "left_fact_id": str(write.left_fact_id),
            "right_fact_id": str(write.right_fact_id),
            "reason": _NOT_REPEATED,
            "idempotency_key": str(write.key),
        }
    else:
        stdin = {
            "proposal_id": str(write.proposal_id),
            "reason": _NOT_REPEATED,
            "idempotency_key": str(write.key),
        }
    return {
        "operation": write.operation,
        "recovery": _RECOVERY[write.operation],
        "command": f"cairn-memory --profile {shlex.quote(profile)} {write.operation}",
        "stdin": stdin,
    }


def recovery_packet(
    controller: BrowseController, profile: str = "PATH"
) -> dict[str, object] | None:
    """Content-free identities for every write left unconfirmed at exit."""
    promotions = list(controller.unresolved.values())
    writes = list(controller.unresolved_writes.values())
    if not promotions and not writes:
        return None
    scope = controller.scope
    if writes:
        latest, outcome = writes[-1]
        recovery = _RECOVERY.get(latest.operation) or (
            outcome.recovery or _STATUS_THEN_RESUME
        )
        error = {
            "code": outcome.code or "outcome_unknown",
            "operation": outcome.operation,
        }
    else:
        _, last = promotions[-1]
        recovery = "resubmit_identical_propose_then_proposal-accept_same_idempotency_keys_and_fields"
        error = {"code": last.code or "outcome_unknown", "operation": last.operation}
    packet: dict[str, object] = {
        "error": error,
        "last_confirmed_stage": "unconfirmed",
        "recovery": recovery,
    }
    if promotions:
        packet["promotions"] = [
            {
                "proposal_id": str(attempt.proposal_id),
                "source_fact_id": str(attempt.source_fact_id),
                "target_scope": _scope_document(scope),
                "evidence_id": str(attempt.evidence_id),
                "target_classification": attempt.classification,
                "propose_idempotency_key": str(attempt.propose_key),
                "proposal_accept_idempotency_key": str(attempt.accept_key),
                "reason": _NOT_REPEATED,
            }
            for attempt, _ in promotions
        ]
    for operation, name in _LISTS.items():
        entries = [
            _write_recovery(write, outcome, profile)
            for write, outcome in writes
            if write.operation == operation
        ]
        if entries:
            packet[name] = entries
    return packet
