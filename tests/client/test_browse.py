"""Terminal-free controller tests for ``cairn-memory browse``."""

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment, TrustClass
from cairn.client.browse import (
    AbandonAttempt,
    AcknowledgementAttempt,
    ArrivalAttempt,
    BrowseController,
    BrowseFailure,
    CorrectionAttempt,
    DisagreementAttempt,
    Fact,
    Page,
    PromotionAttempt,
    PromotionOutcome,
    RejectionAttempt,
    RememberAttempt,
    ResumeAttempt,
    WriteAttempt,
    WriteOutcome,
    clip,
    sanitise,
    wrap,
)
from cairn.client.browse_terminal import (
    MemoryBrowseBackend,
    key_name,
    recovery_packet,
)
from cairn.client.errors import FailureMetadata, MemoryOperationFailure
from cairn.client.proposal_types import (
    ProposalDecision,
    ProposalPage,
    ProposalSnapshot,
)
from cairn.client.suggestion_types import SuggestedMemory
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DiagnosticPermissions,
)

SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
ROOT = Scope("acme", ())
SCOPE_JSON = {
    "realm": "acme",
    "segments": ({"kind": "repository", "identifier": "cairn"},),
}
EVIDENCE = "99999999-9999-4999-8999-999999999999"


def mode(c: BrowseController) -> str:
    """Read state freshly; awaited keys change it (defeats mypy narrowing)."""
    return str(c.mode)


def status(c: BrowseController) -> str:
    assert c.outcome is not None
    return str(c.outcome.status)


def last(backend: "Fake") -> tuple[object, ...]:
    return tuple(backend.calls[-1])


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def fact(
    n: int,
    *,
    trust: str = "candidate",
    body: str | None = None,
    evidence: str | None = EVIDENCE,
    scope: object = SCOPE_JSON,
) -> dict[str, object]:
    return {
        "fact_id": uid(n),
        "body": f"fact number {n}\nsecond line" if body is None else body,
        "trust": trust,
        "classification": "internal",
        "scope": scope,
        "source_type": "agent",
        "source_principal_id": uid(900),
        "recorded_at": f"2026-09-{(n % 28) + 1:02d}T12:00:00.000000Z",
        "observed_at": None,
        "valid_from": None,
        "valid_to": None,
        "invalidated_at": None,
        "derived_from": None,
        "promoted_by": None,
        "evidence_id": None,
        "has_disagreement": False,
        "disagreement_context_incomplete": False,
        "source_evidence_id": evidence,
    }


def proposal(
    n: int,
    *,
    state: Literal["pending", "accepted", "rejected"] = "pending",
    reason: str | None = None,
    target: Scope = SCOPE,
) -> ProposalSnapshot:
    decision = (
        None
        if state == "pending"
        else ProposalDecision(
            state,
            UUID(uid(800)),
            datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
            UUID(uid(801)),
            None if state == "accepted" else "Declined",
            None,
            None,
        )
    )
    return ProposalSnapshot(
        proposal_id=UUID(f"bbbbbbbb-0000-4000-8000-{n:012x}"),
        scope=SCOPE,
        source_fact_id=UUID(uid(n)),
        source_trust=TrustClass.CANDIDATE,
        source_invalidated=False,
        target_scope=target,
        classification=Classification.INTERNAL,
        reason=f"proposal reason {n}\nsecond reason line" if reason is None else reason,
        proposed_by=UUID(uid(900)),
        recorded_at=datetime(2026, 10, (n % 28) + 1, 8, 15, tzinfo=UTC),
        mutation_id=UUID(uid(700 + n)),
        state=state,
        decision=decision,
    )


DIGEST = "ab" * 32
MCP_DIGEST = "cd" * 32
EVALUATED = datetime(2026, 11, 3, 14, 5, tzinfo=UTC)
SAME_CANDIDATE = "Retrieval found a related candidate; equivalence is unverified."
CORRECTION_REASON = (
    "Recorded correction history; inspect both attributed claims before acting."
)


def diagnosis(
    *,
    retrieve: bool = True,
    ingest: bool = True,
    promote: bool = True,
    invalidate: bool = False,
) -> ConnectionDiagnostics:
    return ConnectionDiagnostics(
        ConnectionStatus.READY,
        instance_id=UUID(uid(600)),
        product_version="0.11.4",
        contract_identity="cairn-memory/v1",
        contract_digest=DIGEST,
        mcp_contract_digest=MCP_DIGEST,
        principal_id=UUID(uid(900)),
        principal_kind="human",
        scope=SCOPE,
        classification=Classification.INTERNAL,
        permissions=DiagnosticPermissions(retrieve, ingest, promote, invalidate),
        evaluated_at=EVALUATED,
        permission_basis="current_grants_only",
    )


def duplicate(root: int, other: int, body: str | None = None) -> dict[str, object]:
    return {
        "kind": "possible_duplicate",
        "match_basis": "retrieval_candidate",
        "reason": SAME_CANDIDATE,
        "facts": (fact(root), fact(other, body=body)),
        "corrections": (),
        "disagreements": (),
    }


def corrected(root: int, by: int, reason: str = "Measured again") -> dict[str, object]:
    return {
        "kind": "possible_correction",
        "match_basis": "recorded_correction",
        "reason": CORRECTION_REASON,
        "facts": (fact(root), fact(by)),
        "corrections": (
            {
                "fact_id": uid(root),
                "superseded_by": uid(by),
                "principal_id": uid(5),
                "reason": reason,
                "invalidated_at": "2026-09-10T12:00:00.000000Z",
            },
        ),
        "disagreements": (),
    }


def suggested(*items: Any, exhausted: bool = False) -> SuggestedMemory:
    return SuggestedMemory(tuple(items), 10 * len(items), exhausted, False, None)


class Fake:
    def __init__(self, facts: list[dict[str, object]], size: int = 3) -> None:
        self.facts, self.size = facts, size
        self.calls: list[tuple[object, ...]] = []
        self.outcomes: list[PromotionOutcome] = []
        self.write_outcomes: list[WriteOutcome] = []
        self.expired = False
        self.proposal_items: list[ProposalSnapshot] = []
        self.proposal_size = 2
        self.list_failure: BrowseFailure | None = None
        self.read_failure: BrowseFailure | None = None
        self.read_override: ProposalSnapshot | None = None
        self.suggestion_result = suggested()
        self.suggest_failure: BrowseFailure | None = None
        self.diagnosis = diagnosis()
        self.diagnose_failure: BrowseFailure | None = None
        self.snapshot: Any = session_snapshot()
        self.status_failure: BrowseFailure | None = None

    async def session_status(self, turn_id: UUID | None) -> Any:
        self.calls.append(
            ("session_status",) if turn_id is None else ("session_status", turn_id)
        )
        if self.status_failure is not None:
            raise self.status_failure
        return self.snapshot

    async def suggest(self, fact_id: UUID) -> SuggestedMemory:
        self.calls.append(("suggest", fact_id))
        if self.suggest_failure is not None:
            raise self.suggest_failure
        return self.suggestion_result

    async def diagnose(self) -> ConnectionDiagnostics:
        self.calls.append(("diagnose",))
        if self.diagnose_failure is not None:
            raise self.diagnose_failure
        return self.diagnosis

    async def proposals(self, after: UUID | None) -> ProposalPage:
        self.calls.append(("proposals", after))
        if self.list_failure is not None:
            raise self.list_failure
        items = sorted(self.proposal_items, key=lambda p: str(p.proposal_id))
        if after is not None:
            items = [p for p in items if str(p.proposal_id) > str(after)]
        chunk = items[: self.proposal_size]
        more = len(items) > self.proposal_size
        return ProposalPage(tuple(chunk), chunk[-1].proposal_id if more else None)

    async def proposal(self, proposal_id: UUID) -> ProposalSnapshot:
        self.calls.append(("proposal", proposal_id))
        if self.read_failure is not None:
            raise self.read_failure
        if self.read_override is not None:
            return self.read_override
        return next(p for p in self.proposal_items if p.proposal_id == proposal_id)

    async def page(self, query: str | None, cursor: str | None) -> Page:
        self.calls.append(("page", query, cursor))
        if cursor is not None and self.expired:
            raise BrowseFailure(
                "recall-page", "invalid_request", "continuation_unavailable"
            )
        start = 0 if cursor is None else int(cursor)
        items = [f for f in self.facts if query is None or query in str(f["body"])]
        chunk = items[start : start + self.size]
        following = start + self.size
        return Page(
            tuple(chunk), str(following) if following < len(items) else None, True
        )

    async def history(self, fact_id: UUID) -> Fact:
        self.calls.append(("history", fact_id))
        return {
            "facts": (fact(1), fact(77, trust="validated", body="derived copy")),
            "corrections": (
                {
                    "fact_id": uid(1),
                    "superseded_by": None,
                    "principal_id": uid(5),
                    "reason": "Corrected\x1b[31m",
                    "invalidated_at": "2026-09-10T12:00:00.000000Z",
                },
            ),
            "disagreements": (),
            "resolutions": (),
            "budget_consumed": 1,
            "budget_exhausted": False,
        }

    async def evidence(self, evidence_id: UUID, start: int) -> Fact:
        self.calls.append(("evidence", evidence_id, start))
        text = ["first window\x07 text", "second window"][0 if start == 0 else 1]
        end = start + len(text)
        return {
            "evidence_id": str(evidence_id),
            "text": text,
            "start_byte": start,
            "end_byte": end,
            "byte_length": 40,
            "sha256": "0" * 64,
            "next_start_byte": end if start == 0 else None,
        }

    async def promote(self, attempt: PromotionAttempt) -> PromotionOutcome:
        self.calls.append(("promote", attempt))
        return self.outcomes.pop(0)

    async def write(self, attempt: WriteAttempt) -> WriteOutcome:
        self.calls.append(("write", attempt))
        return self.write_outcomes.pop(0)


def ids() -> Iterator[UUID]:
    n = 0
    while True:
        n += 1
        yield UUID(f"aaaaaaaa-0000-4000-8000-{n:012x}")


def controller(
    facts: list[dict[str, object]],
    *,
    may_promote: bool = True,
    size: int = 3,
    connection: ConnectionDiagnostics | None = None,
) -> tuple[BrowseController, Fake]:
    backend = Fake(facts, size)
    generator = ids()
    result = BrowseController(
        backend,
        scope=SCOPE,
        may_promote=may_promote,
        connection=connection,
        identities=lambda: next(generator),
    )
    result.resize(12, 100)
    return result, backend


def screen(c: BrowseController) -> str:
    return "\n".join(line.text for line in c.render())


async def keys(c: BrowseController, *names: str) -> None:
    for name in names:
        await c.handle(name)


async def promote_to_confirm(c: BrowseController, reason: str = "Checked") -> None:
    await c.handle("p")
    assert mode(c) == "input" and c.input_kind == "reason"
    await keys(c, *reason, "enter")
    assert mode(c) == "confirm"


@pytest.mark.anyio
async def test_navigation_pages_through_the_cursor() -> None:
    c, backend = controller([fact(n) for n in range(1, 11)])
    await c.start()
    assert len(c.rows) == 3 and c.cursor == "3"
    assert "[C] 2026-09-02 12:00 fact number 1" in screen(c)
    assert "second line" not in screen(c)
    await keys(c, "j", "j", "down")
    assert c.selected == 3 and len(c.rows) == 6
    assert backend.calls[-1] == ("page", None, "3")
    await c.handle("k")
    assert c.selected == 2
    await c.handle("pgdn")
    assert c.selected == 9 and len(c.rows) == 10
    await c.handle("pgup")
    assert c.selected == 0
    await c.handle("G")
    assert c.selected == 9 and c.cursor is None and len(c.rows) == 10
    rendered = c.render()
    assert any(
        line.style == "selected" and "number 10" in line.text for line in rendered
    )
    await c.handle("g")
    assert c.selected == 0 and c.top == 0
    await c.handle("q")
    assert c.done


@pytest.mark.anyio
async def test_filter_is_client_side_and_fetches_past_hidden_rows() -> None:
    facts = [fact(n) for n in range(1, 8)] + [fact(8, trust="validated")]
    c, backend = controller(facts)
    await c.start()
    await c.handle("f")
    assert c.filter == "candidate" and all(r["trust"] == "candidate" for r in c.view)
    await c.handle("f")
    assert c.filter == "validated"
    assert [r["fact_id"] for r in c.view] == [uid(8)]
    assert all(call[1] is None for call in backend.calls if call[0] == "page")
    assert "filter: validated" in screen(c)
    await c.handle("f")
    assert c.filter == "all" and len(c.view) == len(c.rows)


@pytest.mark.anyio
async def test_search_and_empty_search_return_to_newest_listing() -> None:
    c, backend = controller([fact(1, body="alpha"), fact(2, body="beta")])
    await c.start()
    await keys(c, "/", *"beta", "enter")
    assert backend.calls[-1] == ("page", "beta", None)
    assert [r["fact_id"] for r in c.rows] == [uid(2)]
    assert "search: beta" in screen(c)
    await c.handle("/")
    assert c.input_text == "beta"
    await keys(c, *["backspace"] * 4, " ", "enter")
    assert last(backend) == ("page", None, None) and c.query is None
    assert "newest first" in screen(c)
    await keys(c, "/", "x", "esc")
    assert mode(c) == "list" and c.query is None


@pytest.mark.anyio
async def test_expired_continuation_says_refresh() -> None:
    c, backend = controller([fact(n) for n in range(1, 8)])
    await c.start()
    backend.expired = True
    await keys(c, "j", "j", "j")
    assert c.cursor is None and "press r" in c.message
    backend.expired = False
    await c.handle("r")
    assert c.cursor == "3" and c.selected == 0


@pytest.mark.anyio
async def test_detail_history_and_paged_evidence() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await c.handle("enter")
    assert mode(c) == "detail" and backend.calls[-1] == ("history", UUID(uid(1)))
    c.resize(60, 100)
    text = screen(c)
    for expected in (
        f"fact_id: {uid(1)}",
        "trust: candidate",
        "classification: internal",
        "scope: acme/repository:cairn",
        "source_type: agent",
        f"source principal: {uid(900)}",
        "recorded_at: 2026-09-02",
        "valid_from: -",
        "invalidated_at: -",
        "derived_from: -",
        f"source evidence: {EVIDENCE}",
        "p promote",
    ):
        assert expected in text, expected
    assert "fact number 1" in text and "second line" in text
    assert f"fact {uid(77)} validated" in text
    assert "correction of" in text and "Corrected\\x1b[31m" in text
    await c.handle("e")
    assert mode(c) == "evidence"
    assert "first window\\x07 text" in screen(c)
    await c.handle("n")
    assert last(backend)[2] == len("first window\x07 text")
    assert "second window" in screen(c)
    await c.handle("n")
    assert c.message == "end of evidence"
    await c.handle("b")
    assert last(backend)[2] == 0
    await c.handle("esc")
    assert mode(c) == "detail"
    await c.handle("backspace")
    assert mode(c) == "list"


@pytest.mark.anyio
async def test_failed_back_read_keeps_evidence_history() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await c.handle("enter")
    await c.handle("e")
    await c.handle("n")
    assert "second window" in screen(c)
    original = backend.evidence

    async def failing(evidence_id: UUID, start: int) -> Fact:
        raise BrowseFailure("evidence-window", "transport_error", None)

    backend.evidence = failing  # type: ignore[method-assign]
    await c.handle("b")
    assert c.message == "evidence-window failed: transport_error"
    assert "second window" in screen(c)
    backend.evidence = original  # type: ignore[method-assign]
    await c.handle("b")
    assert last(backend)[2] == 0
    assert "first window" in screen(c)


@pytest.mark.anyio
async def test_no_promote_permission_is_never_offered() -> None:
    c, backend = controller([fact(1)], may_promote=False)
    await c.start()
    assert "p promote" not in screen(c)
    await c.handle("p")
    assert mode(c) == "list"
    assert c.message == "promotion not permitted for this profile"
    await keys(c, "enter", "p")
    assert c.message == "promotion not permitted for this profile"
    assert "p promote" not in screen(c)
    assert not any(call[0] == "promote" for call in backend.calls)


@pytest.mark.anyio
async def test_validated_ancestor_and_unevidenced_facts_are_refused() -> None:
    root = {"realm": "acme", "segments": ()}
    c, backend = controller(
        [fact(1, trust="validated"), fact(2, scope=root), fact(3, evidence=None)]
    )
    await c.start()
    assert "p promote" not in screen(c)
    await c.handle("p")
    assert mode(c) == "list" and "only candidate facts" in c.message
    await keys(c, "j", "p")
    assert "exact scope" in c.message and "acme" in c.message
    assert "^ fact number 2" in screen(c)
    await keys(c, "j", "p")
    assert "no source evidence" in c.message
    assert "external evidence reference" in c.message
    assert not any(call[0] == "promote" for call in backend.calls)


@pytest.mark.anyio
async def test_reason_is_required_and_cancel_sends_nothing() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await c.handle("p")
    await c.handle("enter")
    assert mode(c) == "input" and c.message == "a reason is required"
    await keys(c, " ", " ", "enter")
    assert mode(c) == "input" and c.message == "a reason is required"
    await c.handle("esc")
    assert mode(c) == "list"
    await promote_to_confirm(c)
    text = screen(c)
    c.resize(40, 100)
    text = screen(c)
    assert f"source fact: {uid(1)} (left unchanged)" in text
    assert "target scope: acme/repository:cairn (same scope)" in text
    assert "classification: internal (inherited)" in text
    assert f"evidence: {EVIDENCE} (the fact's own source evidence)" in text
    assert "Checked" in text and "Press y" in text
    await c.handle("n")
    assert mode(c) == "list" and "nothing was sent" in c.message
    assert not any(call[0] == "promote" for call in backend.calls)


@pytest.mark.anyio
async def test_confirmed_promotion_reports_derived_fact_and_refreshes() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await c.handle("enter")
    await promote_to_confirm(c, "Verified against the build log")
    derived = UUID(uid(500))
    backend.outcomes.append(
        PromotionOutcome(
            "promoted",
            "proposal-accept",
            derived_fact_id=derived,
            receipt_outcome="committed",
            proposal_recorded=True,
        )
    )
    await c.handle("y")
    attempt = backend.calls[-1][1]
    assert isinstance(attempt, PromotionAttempt)
    assert attempt.source_fact_id == UUID(uid(1))
    assert attempt.evidence_id == UUID(EVIDENCE)
    assert attempt.classification == "internal"
    assert attempt.reason == "Verified against the build log"
    assert len({attempt.proposal_id, attempt.propose_key, attempt.accept_key}) == 3
    assert mode(c) == "outcome"
    assert f"new validated fact: {derived}" in screen(c)
    assert "receipt outcome: committed" in screen(c)
    await c.handle("x")
    assert mode(c) == "list" and last(backend) == ("page", None, None)
    assert not c.unresolved


@pytest.mark.anyio
async def test_uncertain_promotion_keeps_identities_and_resubmits_identically() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await promote_to_confirm(c, "Secret-free reason")
    backend.outcomes.append(
        PromotionOutcome(
            "uncertain", "proposal-accept", "transport_error", proposal_recorded=True
        )
    )
    await c.handle("y")
    c.resize(40, 100)
    text = screen(c)
    assert "outcome unknown" in text and "Nothing is reported as promoted" in text
    first = backend.calls[-1][1]
    assert isinstance(first, PromotionAttempt)
    for identity in (first.proposal_id, first.propose_key, first.accept_key):
        assert str(identity) in text
    assert "new validated fact" not in text
    packet = recovery_packet(c)
    assert packet is not None
    assert "Secret-free reason" not in json.dumps(packet)
    assert packet["promotions"][0]["proposal_accept_idempotency_key"] == str(  # type: ignore[index]
        first.accept_key
    )
    backend.outcomes.append(
        PromotionOutcome(
            "promoted",
            "proposal-accept",
            derived_fact_id=UUID(uid(501)),
            receipt_outcome="replayed",
            proposal_recorded=True,
        )
    )
    await c.handle("R")
    assert backend.calls[-1] == ("promote", first)
    assert "receipt outcome: replayed" in screen(c)
    assert recovery_packet(c) is None


@pytest.mark.anyio
async def test_refused_and_unsent_outcomes() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await promote_to_confirm(c)
    backend.outcomes.append(
        PromotionOutcome(
            "refused", "proposal-accept", "authorisation_denied", proposal_recorded=True
        )
    )
    await c.handle("y")
    c.resize(40, 100)
    text = screen(c)
    assert "refused by Cairn (proposal-accept: authorisation_denied)" in text
    assert "remains pending" in text and "R resubmits" not in text
    await c.handle("R")
    assert mode(c) == "list" and recovery_packet(c) is None
    await promote_to_confirm(c)
    backend.outcomes.append(
        PromotionOutcome("failed", "propose", "transport_unavailable")
    )
    await c.handle("y")
    assert "request was not sent" in screen(c) and "R resubmits" in screen(c)
    assert recovery_packet(c) is None


@pytest.mark.anyio
async def test_untrusted_text_is_escaped_before_drawing() -> None:
    hostile = "\x1b[2J\x1b]0;owned\x07\rbad‮evil\u0085 x\x00"
    c, _ = controller([fact(1, body=hostile)])
    await c.start()
    await c.handle("enter")
    c.resize(40, 200)
    for line in c.render():
        assert all(ch.isprintable() for ch in line.text), line
    text = screen(c)
    assert "\\x1b[2J\\x1b]0;owned\\x07\\x0dbad\\u202eevil\\x85\\u2028x\\x00" in text


def test_sanitise_wrap_and_clip_use_terminal_cells() -> None:
    assert sanitise("a\tb\x7fc") == "a b\\x7fc"
    assert clip("日本語abc", 5) == "日本"
    assert wrap("one two three", 7) == ["one two", "three"]
    assert wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert wrap("x\ny", 10) == ["x", "y"]


def test_key_names() -> None:
    import curses

    assert key_name(curses.KEY_DOWN) == "down"
    assert key_name("\n") == "enter" and key_name("\x1b") == "esc"
    assert key_name("\x7f") == "backspace" and key_name("\x03") == "quit"
    assert key_name("é") == "é" and key_name("\x01") is None


@pytest.mark.anyio
async def test_cli_refuses_without_terminal_before_profile(tmp_path: Path) -> None:
    from cairn.client.cli import execute

    output, error = io.BytesIO(), io.BytesIO()
    code = await execute(
        ["--profile", str(tmp_path / "missing"), "browse"],
        stdin=io.BytesIO(b""),
        stdout=output,
        stderr=error,
    )
    assert code == 2 and output.getvalue() == b""
    packet: dict[str, Any] = json.loads(error.getvalue())
    assert packet["command"] == "browse"
    assert packet["result"]["error"] == {
        "code": "terminal_required",
        "operation": "input",
    }


async def _unknown(c: BrowseController, backend: Fake) -> PromotionAttempt:
    await c.start()
    await promote_to_confirm(c, "Original reason")
    backend.outcomes.append(
        PromotionOutcome(
            "uncertain", "proposal-accept", "transport_error", proposal_recorded=True
        )
    )
    await c.handle("y")
    attempt = c.attempt
    assert attempt is not None and attempt.proposal_id in c.unresolved
    return attempt


@pytest.mark.anyio
@pytest.mark.parametrize(
    "retry",
    [
        PromotionOutcome("failed", "propose", "transport_unavailable"),
        PromotionOutcome("refused", "propose", "authorisation_denied"),
    ],
    ids=["preflight-failure", "refusal"],
)
async def test_failed_or_refused_retry_keeps_unknown_outcome(
    retry: PromotionOutcome,
) -> None:
    c, backend = controller([fact(1), fact(2)])
    first = await _unknown(c, backend)
    backend.outcomes.append(retry)
    await c.handle("R")
    assert last(backend) == ("promote", first)
    c.resize(40, 100)
    text = screen(c)
    assert "Retry did not complete" in text and "still unknown" in text
    assert str(first.accept_key) in text and "R resubmits" in text
    packet: Any = recovery_packet(c)
    assert packet["error"] == {
        "code": "transport_error",
        "operation": "proposal-accept",
    }
    # Leaving the screen and every list transition keep the unknown attempt.
    await keys(c, "esc", "j", "f", "f", "f", "r", "/", *"fact", "enter")
    await keys(c, "/", "enter", "g", "G", "enter", "esc")
    assert first.proposal_id in c.unresolved
    promotes = len([call for call in backend.calls if call[0] == "promote"])
    # A new promotion on another fact is refused and reopens the unknown one.
    await keys(c, "g", "j", "p")
    assert mode(c) == "outcome" and c.attempt == first
    assert "resolve it before starting another" in screen(c)
    assert len([call for call in backend.calls if call[0] == "promote"]) == promotes
    await c.handle("esc")
    packet = recovery_packet(c)
    assert packet["promotions"][0]["proposal_id"] == str(first.proposal_id)
    # Only a receipt for this exact attempt resolves it.
    await c.handle("p")
    backend.outcomes.append(
        PromotionOutcome(
            "promoted",
            "proposal-accept",
            derived_fact_id=UUID(uid(600)),
            receipt_outcome="replayed",
            proposal_recorded=True,
        )
    )
    await c.handle("R")
    assert last(backend) == ("promote", first)
    assert recovery_packet(c) is None


@pytest.mark.anyio
async def test_long_reason_confirmation_scrolls_with_pinned_prompt() -> None:
    c, backend = controller([fact(1)])
    c.resize(24, 80)
    await c.start()
    await promote_to_confirm(c, "word " * 160)
    first = c.render()
    assert len(first) == 24
    assert first[-2].text.startswith("Press y to promote")
    assert "proposal-accept key" not in screen(c)
    for name in ("pgdn", "down", "j", "k", "G"):
        await c.handle(name)
        assert mode(c) == "confirm", name
        assert c.render()[-2].text.startswith("Press y to promote")
    attempt = c.attempt
    assert attempt is not None
    assert f"proposal-accept key: {attempt.accept_key}" in screen(c)
    await c.handle("x")
    assert mode(c) == "confirm"
    assert not any(call[0] == "promote" for call in backend.calls)
    await c.handle("n")
    assert mode(c) == "list" and "nothing was sent" in c.message


@pytest.mark.anyio
async def test_small_outcome_screen_scrolls_to_recovery_identities() -> None:
    c, backend = controller([fact(1)])
    attempt = await _unknown(c, backend)
    c.resize(8, 60)
    assert "R resubmits" in c.render()[-2].text
    assert str(attempt.accept_key) not in screen(c)
    await c.handle("G")
    assert mode(c) == "outcome"
    assert str(attempt.accept_key) in screen(c)
    assert "R resubmits" in c.render()[-2].text


def proposal_calls(backend: Fake) -> list[tuple[object, ...]]:
    return [call for call in backend.calls if call[0] in {"proposals", "proposal"}]


@pytest.mark.anyio
async def test_proposals_view_lists_pending_only_and_returns() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [
        proposal(1),
        proposal(2, state="accepted"),
        proposal(3, target=ROOT),
    ]
    backend.proposal_size = 5
    await c.start()
    assert "P proposals" in screen(c)
    await c.handle("P")
    assert mode(c) == "proposals" and last(backend) == ("proposals", None)
    text = screen(c)
    assert "Pending proposals acme/repository:cairn  2/3 loaded" in text
    assert "not a snapshot" in text
    assert (
        f"2026-10-02 08:15 pending -> acme/repository:cairn source {uid(1)[:8]}: "
        "proposal reason 1" in text
    )
    assert "-> acme source" in text and "proposal reason 2" not in text
    assert "second reason line" not in text
    await c.handle("j")
    assert c.proposal_selected == 1
    await c.handle("j")
    assert c.proposal_selected == 1
    await keys(c, "g", "G")
    assert c.proposal_selected == 1
    await c.handle("q")
    assert mode(c) == "list" and not c.done and c.selected == 0
    await keys(c, "P", "esc")
    assert mode(c) == "list"
    await keys(c, "P", "backspace")
    assert mode(c) == "list"
    # Read-only: only proposal-list and proposal-read were called.
    assert {call[0] for call in backend.calls} == {"page", "proposals"}


@pytest.mark.anyio
async def test_empty_proposals_view() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(1, state="rejected")]
    await c.start()
    await c.handle("P")
    text = screen(c)
    assert "(no pending proposals)" in text and "0/1 loaded" in text
    await c.handle("enter")
    assert mode(c) == "proposals"
    assert not any(call[0] == "proposal" for call in backend.calls)


@pytest.mark.anyio
async def test_proposals_continue_past_loaded_rows_and_refresh() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(n) for n in range(1, 6)]
    await c.start()
    await c.handle("P")
    assert len(c.proposal_rows) == 2 and c.proposal_cursor is not None
    assert "2/2+ loaded" in screen(c)
    await keys(c, "j", "j")
    assert c.proposal_selected == 2 and len(c.proposal_rows) == 4
    assert last(backend) == ("proposals", backend.proposal_items[1].proposal_id)
    await c.handle("G")
    assert c.proposal_selected == 4 and c.proposal_cursor is None
    assert "5/5 loaded" in screen(c)
    backend.proposal_items.append(proposal(6))
    await c.handle("r")
    assert last(backend) == ("proposals", None)
    assert c.proposal_selected == 0 and len(c.proposal_rows) == 2
    await c.handle("G")
    assert len(c.pending) == 6


@pytest.mark.anyio
async def test_proposals_skip_decided_pages_automatically() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(n, state="accepted") for n in range(1, 5)]
    backend.proposal_items.append(proposal(5))
    await c.start()
    await c.handle("P")
    assert [p.proposal_id for p in c.pending] == [backend.proposal_items[4].proposal_id]
    assert len([call for call in backend.calls if call[0] == "proposals"]) == 3


@pytest.mark.anyio
async def test_proposal_detail_opens_scrolls_and_leaves() -> None:
    c, backend = controller([fact(1)])
    item = proposal(1, reason="word " * 120)
    backend.proposal_items = [item]
    await c.start()
    await keys(c, "P", "enter")
    assert mode(c) == "proposal" and last(backend) == ("proposal", item.proposal_id)
    first = screen(c)
    assert "Proposal detail (read-only)" in first
    assert f"proposal_id: {item.proposal_id}" in first
    assert "j/k scroll" in first
    c.resize(60, 100)
    text = screen(c)
    for expected in (
        "state: pending",
        "scope: acme/repository:cairn",
        "target_scope: acme/repository:cairn",
        f"source_fact_id: {uid(1)}",
        "source trust: candidate   source invalidated: False",
        "classification: internal",
        f"proposed_by: {uid(900)}",
        "recorded_at: 2026-10-02T08:15:00+00:00",
        f"mutation_id: {uid(701)}",
        "Reason (untrusted text):",
        "Decision: none recorded",
    ):
        assert expected in text, expected
    c.resize(12, 100)
    await c.handle("G")
    assert c.scroll > 0 and "Decision: none recorded" in screen(c)
    await c.handle("g")
    assert c.scroll == 0
    await c.handle("q")
    assert mode(c) == "proposals" and c.proposal is None
    await keys(c, "enter", "esc")
    assert mode(c) == "proposals"
    await keys(c, "enter", "backspace")
    assert mode(c) == "proposals"


@pytest.mark.anyio
async def test_proposal_detail_shows_a_decision_made_since_listing() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(1)]
    backend.read_override = proposal(1, state="rejected")
    await c.start()
    await keys(c, "P", "enter")
    c.resize(60, 100)
    text = screen(c)
    assert "state: rejected" in text and "Decision:" in text
    assert f"decided_by: {uid(800)}" in text and "Declined" in text
    assert "promoted_fact_id: -" in text


@pytest.mark.anyio
async def test_refused_proposal_list_is_shown_as_unavailable() -> None:
    c, backend = controller([fact(1)])
    backend.list_failure = BrowseFailure("proposal-list", "authorisation_denied")
    await c.start()
    await c.handle("P")
    assert mode(c) == "proposals"
    assert c.message == "proposal-list failed: authorisation_denied"
    text = screen(c)
    assert "Pending proposals acme/repository:cairn  unavailable" in text
    assert "loaded" not in text.split("\n")[0]
    assert "(proposals unavailable: proposal-list failed: authorisation_denied)" in text
    await keys(c, "j", "G", "enter")
    assert not any(call[0] == "proposal" for call in backend.calls)
    backend.list_failure = None
    backend.proposal_items = [proposal(1)]
    await c.handle("r")
    assert c.proposal_error is None and "1/1 loaded" in screen(c)


@pytest.mark.anyio
async def test_failed_continuation_marks_listing_incomplete() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(n) for n in range(1, 6)]
    await c.start()
    await c.handle("P")
    backend.list_failure = BrowseFailure("proposal-list", "authorisation_denied")
    await keys(c, "j", "j")
    assert (
        c.message
        == "proposal-list failed: authorisation_denied; press r to start again"
    )
    assert c.proposal_cursor is None and c.proposal_selected == 1
    assert "2/2 loaded  listing incomplete  by id, not a snapshot" in screen(c)


@pytest.mark.anyio
async def test_failed_proposal_read_stays_on_the_list() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(1)]
    backend.read_failure = BrowseFailure("proposal-read", "transport_error")
    await c.start()
    await keys(c, "P", "enter")
    assert mode(c) == "proposals" and c.proposal is None
    assert c.message == "proposal-read failed: transport_error"
    assert "Proposal detail" not in screen(c)
    assert "proposal-read failed: transport_error" in screen(c)


@pytest.mark.anyio
async def test_hostile_proposal_text_is_escaped() -> None:
    hostile = "\x1b[2J\x1b]0;owned\x07\rbad‮evil\u0085 x\x00"
    escaped = "\\x1b[2J\\x1b]0;owned\\x07\\x0dbad\\u202eevil\\x85\\u2028x\\x00"
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(1, reason=hostile)]
    backend.read_override = proposal(1, state="rejected", reason=hostile)
    await c.start()
    await c.handle("P")
    c.resize(40, 200)
    for line in c.render():
        assert all(ch.isprintable() for ch in line.text), line
    assert escaped in screen(c)
    await c.handle("enter")
    assert mode(c) == "proposal"
    for line in c.render():
        assert all(ch.isprintable() for ch in line.text), line
    assert escaped in screen(c)


@pytest.mark.anyio
async def test_footers_fit_eighty_columns() -> None:
    c, backend = controller([fact(1)])
    backend.proposal_items = [proposal(1)]
    c.resize(24, 80)
    await c.start()
    assert c.render()[-1].text.endswith("p promote  P proposals  q quit")
    await c.handle("P")
    assert c.render()[-1].text.endswith("Esc back")
    await c.handle("enter")
    assert c.render()[-1].text.endswith("Esc back")
    await keys(c, "esc", "esc")
    footer = c.render()[-1].text
    assert footer == c._footer() and len(footer) <= 79
    assert "s suggest  c connection  S session  p promote" in footer
    backend.suggestion_result = suggested(duplicate(1, 2))
    await c.handle("s")
    assert c.render()[-1].text == "j/k scroll  g/G top/end  Esc back"
    await keys(c, "esc", "enter")
    footer = c.render()[-1].text
    assert footer == c._footer() and footer.endswith("s suggest  Esc back")
    await keys(c, "esc", "c")
    assert c.render()[-1].text == "j/k scroll  g/G top/end  r re-read  Esc back"


@pytest.mark.anyio
async def test_list_footer_shows_every_binding_on_a_wide_terminal() -> None:
    c, _ = controller([fact(1)])
    c.resize(24, 120)
    await c.start()
    assert c.render()[-1].text == (
        "Enter open  / search  f filter  r refresh  s suggest  c connection  "
        "S session  p promote  P proposals  q quit"
    )


def read_only(backend: Fake) -> None:
    allowed = {"page", "history", "evidence", "suggest", "diagnose"}
    assert {call[0] for call in backend.calls} <= allowed | {"proposals", "proposal"}


@pytest.mark.anyio
async def test_suggestions_open_from_list_and_detail_and_leave() -> None:
    c, backend = controller([fact(1), fact(2)])
    backend.suggestion_result = suggested(duplicate(1, 9), corrected(1, 8))
    await c.start()
    await c.handle("s")
    assert mode(c) == "suggestions" and last(backend) == ("suggest", UUID(uid(1)))
    c.resize(60, 200)
    text = screen(c)
    for expected in (
        f"Suggestions for {uid(1)} (read-only, untrusted evidence)",
        "the browser never acts on them",
        "budget consumed: 20   budget exhausted: False   semantic degraded: False",
        "recall policy: -",
        "1. possible_duplicate (basis: retrieval_candidate)",
        SAME_CANDIDATE,
        f"fact {uid(9)} candidate at acme/repository:cairn recorded",
        "body (untrusted text):",
        "fact number 9",
        "2. possible_correction (basis: recorded_correction)",
        f"correction of {uid(1)} superseded_by {uid(8)} by {uid(5)}",
        "reason (untrusted text):",
        "Measured again",
    ):
        assert expected in text, expected
    await c.handle("q")
    assert mode(c) == "list" and not c.done and c.fact is None
    assert c.suggestions is None
    await keys(c, "j", "s")
    assert last(backend) == ("suggest", UUID(uid(2)))
    await c.handle("backspace")
    assert mode(c) == "list" and c.selected == 1
    await keys(c, "k", "enter")
    assert mode(c) == "detail"
    await c.handle("s")
    assert mode(c) == "suggestions" and last(backend) == ("suggest", UUID(uid(1)))
    await c.handle("esc")
    assert mode(c) == "detail" and c.fact is not None
    assert c.fact["fact_id"] == uid(1)
    read_only(backend)


@pytest.mark.anyio
async def test_empty_suggestions_say_so() -> None:
    c, backend = controller([fact(1)])
    await c.start()
    await c.handle("s")
    text = screen(c)
    assert "(no suggestions returned; an empty result does not prove" in text
    assert "budget consumed: 0" in text and "unavailable" not in text


@pytest.mark.anyio
async def test_failed_suggestions_are_not_shown_as_empty() -> None:
    c, backend = controller([fact(1)])
    backend.suggest_failure = BrowseFailure("suggest", "authorisation_denied")
    await c.start()
    await c.handle("s")
    assert mode(c) == "suggestions"
    assert c.message == "suggest failed: authorisation_denied"
    text = screen(c)
    assert "(suggestions unavailable: suggest failed: authorisation_denied)" in text
    assert "no suggestions" not in text and "budget consumed" not in text
    await c.handle("esc")
    assert mode(c) == "list" and c.suggestions_error is None


@pytest.mark.anyio
async def test_suggestions_scroll() -> None:
    c, backend = controller([fact(1)])
    backend.suggestion_result = suggested(
        *(duplicate(1, n) for n in range(10, 16)), exhausted=True
    )
    await c.start()
    await c.handle("s")
    total = len(c._suggestion_lines())
    assert total > c.body_height
    await c.handle("j")
    assert c.scroll == 1
    await c.handle("G")
    assert c.scroll == total - c.body_height
    assert "(suggestion budget exhausted; more may exist)" in screen(c)
    await c.handle("g")
    assert c.scroll == 0
    assert len(backend.calls) == 2


@pytest.mark.anyio
async def test_hostile_suggestion_text_is_escaped() -> None:
    hostile = "\x1b[2J\x1b]0;owned\x07\rbad‮evil\u0085 x\x00"
    escaped = "\\x1b[2J\\x1b]0;owned\\x07\\x0dbad\\u202eevil\\x85\\u2028x\\x00"
    c, backend = controller([fact(1)])
    backend.suggestion_result = suggested(
        duplicate(1, 2, body=hostile), corrected(1, 3, reason=hostile)
    )
    await c.start()
    await c.handle("s")
    c.resize(60, 200)
    for line in c.render():
        assert all(ch.isprintable() for ch in line.text), line
    assert screen(c).count(escaped) == 2


@pytest.mark.anyio
async def test_connection_view_shows_check_result_and_re_reads() -> None:
    c, backend = controller([fact(1)], connection=diagnosis())
    backend.diagnosis = diagnosis(promote=False)
    await c.start()
    assert "p promote" in screen(c)
    await c.handle("c")
    assert mode(c) == "connection" and last(backend) == ("diagnose",)
    c.resize(60, 100)
    text = screen(c)
    for expected in (
        "Connection and grants (read-only)",
        "status: ready",
        "evaluated_at (server): 2026-11-03T14:05:00+00:00",
        f"instance_id: {uid(600)}",
        "product_version: 0.11.4",
        "contract_identity: cairn-memory/v1",
        f"contract_digest: {DIGEST}",
        f"mcp_contract_digest: {MCP_DIGEST}",
        f"principal_id: {uid(900)}   principal_kind: human",
        "scope: acme/repository:cairn",
        "classification: internal",
        "permission_basis: current_grants_only",
        "retrieve: yes",
        "ingest: yes",
        "promote: no",
        "invalidate: no",
        "s suggestions and P proposals need retrieve: available",
        "p promotion needs ingest and promote: unavailable",
    ):
        assert expected in text, expected
    assert "Bearer" not in text and "token" not in text.lower()
    # The re-read replaced the start-time grants, as promotion now shows.
    await c.handle("esc")
    assert mode(c) == "list" and "p promote" not in screen(c)
    await c.handle("p")
    assert c.message == "promotion not permitted for this profile"
    await c.handle("c")
    backend.diagnosis = diagnosis()
    await c.handle("r")
    assert [call for call in backend.calls if call[0] == "diagnose"] == [
        ("diagnose",)
    ] * 3
    assert "p promotion needs ingest and promote: available" in screen(c)
    await c.handle("q")
    assert mode(c) == "list" and not c.done and "p promote" in screen(c)
    read_only(backend)


@pytest.mark.anyio
async def test_failed_connection_re_read_keeps_the_last_result() -> None:
    c, backend = controller([fact(1)], connection=diagnosis())
    backend.diagnose_failure = BrowseFailure("diagnose", "transport_error")
    await c.start()
    await c.handle("c")
    assert c.message == "diagnose failed: transport_error"
    c.resize(60, 100)
    text = screen(c)
    assert "re-read failed: diagnose failed: transport_error" in text
    assert "Showing the last successful diagnosis" in text
    assert f"instance_id: {uid(600)}" in text and "evaluated_at (server): 2026" in text
    assert c.may_promote
    backend.diagnose_failure = None
    await c.handle("r")
    assert c.connection_error is None and "re-read failed" not in screen(c)

    bare, failing = controller([fact(1)])
    failing.diagnose_failure = BrowseFailure("diagnose", "instance_mismatch")
    await bare.start()
    await bare.handle("c")
    text = screen(bare)
    assert "re-read failed: diagnose failed: instance_mismatch" in text
    assert "(no diagnosis available; press r to read it again)" in text
    assert "status:" not in text


@pytest.mark.anyio
async def test_missing_retrieve_marks_reads_unavailable() -> None:
    c, backend = controller([fact(1)], connection=diagnosis(retrieve=False))
    backend.diagnosis = diagnosis(retrieve=False)
    await c.start()
    text = screen(c)
    assert "s suggest" not in text and "P proposals" not in text
    assert "c connection" in text
    await c.handle("s")
    assert mode(c) == "list"
    assert c.message == "suggestions not permitted for this profile"
    await c.handle("P")
    assert mode(c) == "list"
    assert c.message == "proposals not permitted for this profile"
    await c.handle("enter")
    assert "s suggest" not in screen(c)
    await c.handle("s")
    assert mode(c) == "detail"
    assert c.message == "suggestions not permitted for this profile"
    await keys(c, "esc", "c")
    c.resize(60, 100)
    text = screen(c)
    assert "retrieve: no" in text
    assert "s suggestions and P proposals need retrieve: unavailable" in text
    assert not any(call[0] in {"suggest", "proposals"} for call in backend.calls)
    read_only(backend)


# Writes: correct, disagree and proposal-reject ------------------------------

KEY1 = "aaaaaaaa-0000-4000-8000-000000000001"
WRITER = diagnosis(invalidate=True)
OPERATIONS = ("correct", "disagree", "proposal-reject")
HOSTILE = "\x1b[2J\x1b]0;owned\x07\rbad‮evil\u0085 x\x00"
ESCAPED = "\\x1b[2J\\x1b]0;owned\\x07\\x0dbad\\u202eevil\\x85\\u2028x\\x00"
RECORDED_AT = "2026-11-24T10:00:00.000000Z"


def writer(
    facts: list[dict[str, object]], *, size: int = 3
) -> tuple[BrowseController, Fake]:
    return controller(facts, connection=WRITER, size=size)


def writes(backend: Fake) -> list[WriteAttempt]:
    return [
        call[1]
        for call in backend.calls
        if call[0] == "write"
        and isinstance(
            call[1], CorrectionAttempt | DisagreementAttempt | RejectionAttempt
        )
    ]


def recorded(
    operation: str,
    outcome: str = "committed",
    *,
    invalidated_at: str | None = None,
    relationship_id: str | None = None,
) -> WriteOutcome:
    return WriteOutcome(
        "recorded",
        operation,
        receipt_outcome=outcome,
        mutation_id=uid(310),
        audit_event_id=uid(311),
        audit_recorded_at=RECORDED_AT,
        invalidated_at=invalidated_at,
        relationship_id=relationship_id,
    )


async def reason_to_confirm(c: BrowseController, key: str, reason: str) -> None:
    await c.handle(key)
    assert mode(c) == "input" and c.input_kind == "reason", c.message
    await keys(c, *reason, "enter")
    assert mode(c) == "confirm", c.message


async def to_confirm(c: BrowseController, backend: Fake, operation: str) -> None:
    """correct acts on fact 1; disagree pairs marked fact 2 with fact 1."""
    await c.start()
    if operation == "proposal-reject":
        backend.proposal_items = [proposal(1)]
        await c.handle("P")
        await reason_to_confirm(c, "x", "Not wanted")
    else:
        await keys(c, "j", "m", "k")
        await reason_to_confirm(c, "x" if operation == "correct" else "d", "Not wanted")


@pytest.mark.anyio
async def test_correction_confirms_what_is_sent_and_reports_the_receipt() -> None:
    c, backend = writer([fact(1), fact(2)])
    await c.start()
    assert "x correct" in c._footer()
    await reason_to_confirm(c, "x", "Measured again")
    c.resize(40, 120)
    text = screen(c)
    for expected in (
        f"Confirm: correct (invalidate) {uid(1)}",
        "operation: correct",
        f"fact to invalidate: {uid(1)}",
        "first line (untrusted text): fact number 1",
        "superseded_by: none (no replacement named)",
        "scope: acme/repository:cairn (the profile's exact scope)",
        "Measured again",
        f"idempotency key: {KEY1}",
        "Press y to correct; Esc or n cancels.",
    ):
        assert expected in text, expected
    assert "second line" not in text
    # Scrolling keys never send.
    await keys(c, "j", "k", "G", "g", "x", "R")
    assert mode(c) == "confirm" and not writes(backend)
    backend.write_outcomes.append(recorded("correct", invalidated_at=RECORDED_AT))
    await c.handle("y")
    assert writes(backend) == [
        CorrectionAttempt(
            UUID(uid(1)), "fact number 1", None, None, "Measured again", UUID(KEY1)
        )
    ]
    text = screen(c)
    for expected in (
        f"Correction of {uid(1)}",
        "Corrected.",
        f"invalidated fact: {uid(1)}",
        "superseded_by: -",
        f"invalidated_at: {RECORDED_AT}",
        "receipt outcome: committed",
        f"mutation_id: {uid(310)}",
        f"audit event: {uid(311)} recorded {RECORDED_AT}",
    ):
        assert expected in text, expected
    assert c.render()[-2].text == "Esc returns."
    before = len(backend.calls)
    await c.handle("esc")
    assert mode(c) == "list" and c.message == "corrected; listing refreshed"
    assert backend.calls[before] == ("page", None, None)
    assert not c.unresolved_writes and recovery_packet(c) is None


@pytest.mark.anyio
async def test_supersession_uses_only_the_marked_fact() -> None:
    c, backend = writer([fact(1), fact(2, body="replacement body\nmore")])
    await c.start()
    assert "X supersede" not in c._footer()
    await c.handle("X")
    assert c.message == "mark the replacement fact with m first; X supersedes by it"
    await keys(c, "j", "m")
    assert c.message == f"marked {uid(2)}"
    assert "* [C]" in screen(c) and "* marked 00000002" in screen(c)
    await c.handle("X")
    assert mode(c) == "list"
    assert c.message == "the marked fact is this fact; mark a different replacement"
    # x never uses the mark, even when one is set.
    await c.handle("k")
    await reason_to_confirm(c, "x", "Plain")
    assert "superseded_by: none" in screen(c) and uid(2) not in screen(c)
    await c.handle("n")
    await c.handle("enter")
    assert f"Fact detail  marked: {uid(2)}" in screen(c)
    assert "X supersede" in c._footer()
    await reason_to_confirm(c, "X", "Replaced")
    c.resize(40, 120)
    text = screen(c)
    assert f"fact to invalidate: {uid(1)}" in text
    assert f"superseded_by (the marked fact): {uid(2)}" in text
    assert "first line (untrusted text): replacement body" in text
    assert "more" not in text
    backend.write_outcomes.append(recorded("correct"))
    await c.handle("y")
    [attempt] = writes(backend)
    assert isinstance(attempt, CorrectionAttempt)
    assert attempt.superseded_by == UUID(uid(2)) and attempt.fact_id == UUID(uid(1))
    assert f"superseded_by: {uid(2)}" in screen(c)
    await c.handle("esc")
    assert mode(c) == "list" and c.marked is None


@pytest.mark.anyio
async def test_disagreement_pairs_the_marked_and_the_selected_fact() -> None:
    c, backend = writer([fact(1), fact(2, body="other claim")])
    await c.start()
    assert "d disagree" not in c._footer()
    await c.handle("d")
    assert c.message == "mark the other fact with m first; d disagrees with it"
    await keys(c, "j", "m", "m")
    assert c.marked is None and c.message == "mark cleared"
    await keys(c, "m", "k")
    assert "d disagree" in c._footer()
    await reason_to_confirm(c, "d", "Ports differ")
    c.resize(40, 120)
    text = screen(c)
    for expected in (
        f"Confirm: disagreement between {uid(2)} and {uid(1)}",
        "operation: disagree",
        f"left fact (the marked fact): {uid(2)}",
        "first line (untrusted text): other claim",
        f"right fact: {uid(1)}",
        "first line (untrusted text): fact number 1",
        "scope: acme/repository:cairn   classification: internal",
        "Ports differ",
        "Neither fact is changed, invalidated or re-trusted",
        f"idempotency key: {KEY1}",
        "Press y to record the disagreement; Esc or n cancels.",
    ):
        assert expected in text, expected
    backend.write_outcomes.append(recorded("disagree", relationship_id=uid(320)))
    await c.handle("y")
    assert writes(backend) == [
        DisagreementAttempt(
            UUID(uid(2)),
            "other claim",
            UUID(uid(1)),
            "fact number 1",
            "Ports differ",
            UUID(KEY1),
        )
    ]
    text = screen(c)
    assert "Disagreement recorded." in text and f"relationship: {uid(320)}" in text
    assert f"left fact: {uid(2)} (unchanged)" in text
    assert "receipt outcome: committed" in text
    await c.handle("esc")
    assert mode(c) == "list" and c.marked is None
    assert c.message == "disagreement recorded; listing refreshed"


@pytest.mark.anyio
async def test_rejection_rereads_reports_the_receipt_and_refreshes() -> None:
    c, backend = writer([fact(1)])
    first = proposal(1, reason="Please promote\nsecond reason line")
    backend.proposal_items = [first, proposal(2)]
    backend.proposal_size = 5
    await c.start()
    await c.handle("P")
    assert c._footer() == (
        "j/k move  PgUp/PgDn  g/G  Enter open  r refresh  x reject  Esc back"
    )
    await reason_to_confirm(c, "x", "Duplicate of another")
    assert last(backend) == ("proposal", first.proposal_id)
    c.resize(40, 120)
    text = screen(c)
    for expected in (
        f"Confirm: reject proposal {first.proposal_id}",
        "operation: proposal-reject",
        f"proposal: {first.proposal_id} (re-read just now: pending)",
        "proposal reason, first line (untrusted text): Please promote",
        f"source fact: {uid(1)} (left unchanged)",
        "target scope: acme/repository:cairn",
        "rejection reason:",
        "Duplicate of another",
        f"idempotency key: {KEY1}",
        "Press y to reject the proposal; Esc or n cancels.",
    ):
        assert expected in text, expected
    assert "second reason line" not in text
    backend.write_outcomes.append(recorded("proposal-reject"))
    await c.handle("y")
    assert writes(backend) == [
        RejectionAttempt(
            first.proposal_id,
            "Please promote",
            UUID(uid(1)),
            SCOPE,
            "Duplicate of another",
            UUID(KEY1),
        )
    ]
    text = screen(c)
    assert "Proposal rejected." in text and "receipt outcome: committed" in text
    backend.proposal_items[0] = proposal(1, state="rejected")
    await c.handle("esc")
    assert mode(c) == "proposals"
    assert c.message == "proposal rejected; proposals refreshed"
    assert [p.proposal_id for p in c.pending] == [proposal(2).proposal_id]
    # From the detail view, and cancelling there returns to it.
    await c.handle("enter")
    assert c._footer() == "j/k scroll  g/G top/end  x reject  Esc back"
    await reason_to_confirm(c, "x", "No")
    await c.handle("esc")
    assert mode(c) == "proposal" and c.proposal is not None
    assert c.message == "rejection cancelled; nothing was sent"
    assert len(writes(backend)) == 1


@pytest.mark.anyio
async def test_rejection_of_a_proposal_decided_meanwhile_sends_nothing() -> None:
    c, backend = writer([fact(1)])
    backend.proposal_items = [proposal(1)]
    await c.start()
    await keys(c, "P", "enter")
    backend.read_override = proposal(1, state="accepted")
    await c.handle("x")
    await keys(c, *"Too late", "enter")
    assert mode(c) == "proposal"
    assert c.message == "proposal is no longer pending (now accepted); nothing was sent"
    assert c.proposal is not None and c.proposal.state == "accepted"
    assert "x reject" not in c._footer()
    await c.handle("x")
    assert c.message == "this proposal is accepted; only a pending one can be rejected"
    # The stale list row is re-read too.
    await c.handle("esc")
    await c.handle("x")
    await keys(c, *"Too late", "enter")
    assert mode(c) == "proposals"
    assert c.message.endswith("nothing was sent; press r to refresh")
    backend.read_failure = BrowseFailure("proposal-read", "transport_error")
    await c.handle("x")
    await keys(c, *"Again", "enter")
    assert mode(c) == "input"
    assert c.message == "proposal-read failed: transport_error; nothing was sent"
    assert not writes(backend)


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["x", "X", "d"])
async def test_fact_writes_cancelled_at_reason_or_confirm_send_nothing(
    key: str,
) -> None:
    c, backend = writer([fact(1), fact(2)])
    await c.start()
    await keys(c, "j", "m", "k", key)
    assert mode(c) == "input"
    await keys(c, *"Half typed", "esc")
    assert mode(c) == "list" and c.message == "cancelled; nothing was sent"
    await reason_to_confirm(c, key, "Typed")
    await c.handle("n")
    assert mode(c) == "list" and c.message.endswith("cancelled; nothing was sent")
    await c.handle("enter")
    await reason_to_confirm(c, key, "Typed")
    await c.handle("esc")
    assert mode(c) == "detail" and c.message.endswith("cancelled; nothing was sent")
    assert not writes(backend) and recovery_packet(c) is None


@pytest.mark.anyio
async def test_rejection_cancelled_at_reason_sends_nothing() -> None:
    c, backend = writer([fact(1)])
    backend.proposal_items = [proposal(1)]
    await c.start()
    await keys(c, "P", "x", *"Half", "esc")
    assert mode(c) == "proposals" and c.message == "cancelled; nothing was sent"
    assert not writes(backend)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_definite_refusal_changes_nothing_and_is_not_resent(
    operation: str,
) -> None:
    c, backend = writer([fact(1), fact(2)])
    await to_confirm(c, backend, operation)
    backend.write_outcomes.append(
        WriteOutcome("refused", operation, "authorisation_denied")
    )
    await c.handle("y")
    c.resize(40, 120)
    text = screen(c)
    assert f"refused by Cairn ({operation}: authorisation_denied)" in text
    assert "Nothing was changed." in text and "unknown" not in text
    assert c.render()[-2].text == "Esc returns."
    await c.handle("R")
    assert mode(c) != "outcome" and len(writes(backend)) == 1
    assert not c.unresolved_writes and recovery_packet(c) is None


@pytest.mark.anyio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_unsent_write_says_so_and_resubmits_the_same_key(
    operation: str,
) -> None:
    c, backend = writer([fact(1), fact(2)])
    await to_confirm(c, backend, operation)
    backend.write_outcomes.append(
        WriteOutcome("failed", operation, "transport_unavailable")
    )
    await c.handle("y")
    c.resize(40, 120)
    text = screen(c)
    assert f"not completed ({operation}: transport_unavailable)" in text
    assert f"The {operation} request was not sent; nothing was changed." in text
    assert "R resubmits" in c.render()[-2].text and recovery_packet(c) is None
    backend.write_outcomes.append(recorded(operation))
    await c.handle("R")
    first, again = writes(backend)
    assert again is first and "receipt outcome: committed" in screen(c)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_unknown_outcome_keeps_the_key_and_retries_identically(
    operation: str,
) -> None:
    c, backend = writer([fact(1), fact(2)])
    await to_confirm(c, backend, operation)
    backend.write_outcomes.append(
        WriteOutcome("uncertain", operation, "transport_error")
    )
    await c.handle("y")
    [first] = writes(backend)
    c.resize(60, 120)
    text = screen(c)
    assert f"outcome unknown ({operation}: transport_error)" in text
    assert "nothing is reported as failed" in text
    for claim in (
        "Corrected.",
        "Disagreement recorded.",
        "Proposal rejected.",
        "refused",
        "not completed",
        "was not sent",
    ):
        assert claim not in text, claim
    assert f"idempotency_key: {first.key}" in text and "Not wanted" in text
    assert "R resubmits identically" in c.render()[-2].text
    assert first.key in c.unresolved_writes
    # Leaving keeps it; any new write, or a promotion, reopens it instead.
    await c.handle("esc")
    await c.handle("x")
    assert mode(c) == "outcome" and c.write is first
    assert "an earlier" in screen(c) and "resolve it before starting another" in (
        screen(c)
    )
    if operation != "proposal-reject":
        await keys(c, "esc", "p")
        assert mode(c) == "outcome" and c.write is first and c.attempt is None
    assert len(writes(backend)) == 1
    # A refused retry proves nothing about the first dispatch.
    backend.write_outcomes.append(
        WriteOutcome("refused", operation, "authorisation_denied")
    )
    await c.handle("R")
    assert writes(backend) == [first, first]
    text = screen(c)
    assert "Retry did not complete" in text and "still unknown" in text
    assert first.key in c.unresolved_writes and recovery_packet(c) is not None
    assert "R resubmits" in c.render()[-2].text
    backend.write_outcomes.append(recorded(operation, "replayed"))
    await c.handle("R")
    assert writes(backend) == [first, first, first]
    assert "receipt outcome: replayed" in screen(c)
    assert not c.unresolved_writes and recovery_packet(c) is None


@pytest.mark.anyio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_recovery_packet_names_the_exact_command_and_stdin(
    operation: str,
) -> None:
    from cairn.client.cli_input import FIELDS

    c, backend = writer([fact(1), fact(2)])
    await to_confirm(c, backend, operation)
    backend.write_outcomes.append(
        WriteOutcome("uncertain", operation, "internal_error")
    )
    await c.handle("y")
    [first] = writes(backend)
    packet: Any = recovery_packet(c, "/tmp/browse profile.json")
    assert "Not wanted" not in json.dumps(packet)
    assert packet["error"] == {"code": "internal_error", "operation": operation}
    assert packet["last_confirmed_stage"] == "unconfirmed"
    assert "promotions" not in packet
    name = {
        "correct": "corrections",
        "disagree": "disagreements",
        "proposal-reject": "proposal_rejections",
    }[operation]
    [entry] = packet[name]
    recovery = {
        "correct": "resubmit_identical_correction_same_idempotency_key_and_fields",
        "disagree": "resubmit_identical_disagree_same_idempotency_key_and_fields",
        "proposal-reject": (
            "resubmit_identical_proposal-reject_same_idempotency_key_and_fields"
        ),
    }[operation]
    assert entry["recovery"] == packet["recovery"] == recovery
    assert entry["command"] == (
        f"cairn-memory --profile '/tmp/browse profile.json' {operation}"
    )
    stdin = entry["stdin"]
    required, optional = FIELDS[operation]
    assert required <= set(stdin) <= required | optional
    assert stdin["idempotency_key"] == str(first.key)
    assert stdin["reason"] == "as entered; not repeated here"
    if operation == "correct":
        assert stdin["fact_ids"] == [uid(1)] and stdin["superseded_by"] is None
    elif operation == "disagree":
        assert (stdin["left_fact_id"], stdin["right_fact_id"]) == (uid(2), uid(1))
    else:
        assert stdin["proposal_id"] == str(proposal(1).proposal_id)
    default: Any = recovery_packet(c)
    assert default[name][0]["command"].endswith(f"--profile PATH {operation}")


@pytest.mark.anyio
async def test_writes_need_known_grants() -> None:
    # Without a diagnosis nothing is offered, as for promotion.
    c, backend = controller([fact(1), fact(2)])
    backend.proposal_items = [proposal(1)]
    await c.start()
    footer = c._footer()
    for part in ("m mark", "x correct", "X supersede", "d disagree"):
        assert part not in footer
    await c.handle("x")
    assert c.message == "correction not permitted for this profile"
    await c.handle("d")
    assert c.message == "disagreement not permitted for this profile"
    await c.handle("m")
    assert c.marked is None and "neither is permitted" in c.message
    await c.handle("P")
    assert "x reject" not in c._footer()
    await c.handle("x")
    assert c.message == "proposal rejection not permitted for this profile"
    await c.handle("enter")
    assert "x reject" not in c._footer()
    await c.handle("x")
    assert c.message == "proposal rejection not permitted for this profile"
    assert not writes(backend)

    # Missing invalidate hides correction but not disagreement.
    c, backend = controller([fact(1), fact(2)], connection=diagnosis())
    await c.start()
    await keys(c, "j", "m", "k")
    footer = c._footer()
    assert "d disagree" in footer and "x correct" not in footer
    assert "X supersede" not in footer
    for key in ("x", "X"):
        await c.handle(key)
        assert c.message == "correction not permitted for this profile"
    # Missing ingest hides disagreement.
    c, backend = controller(
        [fact(1), fact(2)], connection=diagnosis(ingest=False, invalidate=True)
    )
    await c.start()
    await keys(c, "j", "m", "k")
    assert "d disagree" not in c._footer() and "x correct" in c._footer()
    await c.handle("d")
    assert c.message == "disagreement not permitted for this profile"
    assert not writes(backend)


@pytest.mark.anyio
async def test_connection_view_lists_the_write_grants_and_re_reads() -> None:
    c, backend = writer([fact(1)])
    backend.diagnosis = diagnosis(invalidate=False, ingest=False)
    await c.start()
    await c.handle("c")
    c.resize(60, 160)
    text = screen(c)
    assert "x and X correct need invalidate: unavailable" in text
    assert "d disagree needs retrieve and ingest: unavailable" in text
    assert (
        "x reject (proposals) needs retrieve, and promote at the proposal's "
        "target scope, which Cairn checks: available"
    ) in text
    await c.handle("esc")
    assert "x correct" not in c._footer() and "m mark" not in c._footer()
    backend.diagnosis = WRITER
    await keys(c, "c", "esc")
    assert "x correct" in c._footer()


@pytest.mark.anyio
async def test_write_input_validation() -> None:
    v1 = {**fact(3), "fact_id": "00000003-0000-1000-8000-000000000000"}
    gone = {**fact(5), "invalidated_at": RECORDED_AT}
    c, backend = writer(
        [fact(1), fact(2), v1, fact(4, scope={"realm": "acme", "segments": ()}), gone],
        size=10,
    )
    await c.start()
    await c.handle("x")
    await c.handle("enter")
    assert mode(c) == "input" and c.message == "a reason is required"
    await keys(c, " ", " ", "enter")
    assert mode(c) == "input" and c.message == "a reason is required"
    await c.handle("\x07")
    assert c.input_text == "  "
    # The input refuses anything past 4096 UTF-8 bytes; so does submission.
    await keys(c, "backspace", "backspace", *("é" * 2048), "é", "a")
    assert c.input_text == "é" * 2048
    c.input_text = "a" * 4097
    await c.handle("enter")
    assert mode(c) == "input"
    assert c.message == "the reason must be 1..4096 UTF-8 bytes"
    c.input_text = "é" * 2048
    await c.handle("enter")
    assert mode(c) == "confirm"
    await c.handle("n")
    # A second fact that is not a canonical UUIDv4, or is the same fact.
    await keys(c, "j", "j", "m", "g")
    await c.handle("X")
    assert c.message == (
        "the marked fact ID is not a canonical UUIDv4; Cairn would refuse it"
    )
    await c.handle("d")
    assert c.message == (
        "the marked fact ID is not a canonical UUIDv4; Cairn would refuse it"
    )
    await keys(c, "m", "d")
    assert c.message == "the marked fact is this fact; mark a different fact"
    # Ancestor-scope and invalidated facts.
    await keys(c, "j", "j", "j", "x")
    assert c.message == (
        "correction is limited to facts at the profile's exact scope; "
        "the fact is at acme"
    )
    await keys(c, "m", "g")
    assert "d disagree" not in c._footer()
    await c.handle("d")
    assert c.message == (
        "disagreement is limited to facts at the profile's exact scope; "
        "the marked fact is at acme"
    )
    await keys(c, "G", "x")
    assert c.message == "this fact is already invalidated"
    await keys(c, "g", "j", "m", "g", "X")
    assert mode(c) == "input"
    await c.handle("esc")
    await keys(c, "G", "m", "g", "X")
    assert c.message == "the marked replacement is itself invalidated"
    assert not writes(backend)


@pytest.mark.anyio
async def test_unknown_promotion_blocks_writes_too() -> None:
    c, backend = writer([fact(1), fact(2)])
    first = await _unknown(c, backend)
    await keys(c, "esc", "j", "m", "x")
    assert mode(c) == "outcome" and c.attempt == first and c.write is None
    assert "an earlier promotion outcome is unknown" in screen(c)
    assert not writes(backend)


@pytest.mark.anyio
async def test_hostile_text_is_escaped_on_write_screens() -> None:
    c, backend = writer([fact(1, body=HOSTILE), fact(2, body=HOSTILE)])
    backend.proposal_items = [proposal(1, reason=HOSTILE)]
    await c.start()
    await keys(c, "j", "m", "k")

    def inert() -> None:
        c.resize(40, 200)
        for line in c.render():
            assert all(ch.isprintable() for ch in line.text), line
        assert ESCAPED in screen(c)

    for key in ("x", "X", "d"):
        await reason_to_confirm(c, key, "Reason")
        inert()
        await c.handle("n")
    await c.handle("enter")
    inert()
    await keys(c, "esc", "P")
    await reason_to_confirm(c, "x", "Reason")
    inert()
    backend.write_outcomes.append(
        WriteOutcome("uncertain", "proposal-reject", "transport_error")
    )
    await c.handle("y")
    for line in c.render():
        assert all(ch.isprintable() for ch in line.text), line


@pytest.mark.anyio
async def test_write_footers_fit_eighty_columns() -> None:
    c, backend = writer([fact(1), fact(2)])
    backend.proposal_items = [proposal(1)]
    c.resize(24, 80)
    await c.start()

    def fits(*parts: str) -> None:
        footer = c.render()[-1].text
        assert footer == c._footer() and len(footer) <= 79, footer
        for part in parts:
            assert part in footer, (part, footer)

    fits("m mark", "x correct", "p promote", "P proposals", "q quit")
    await keys(c, "j", "m", "k")
    fits("x correct", "X supersede", "d disagree", "p promote", "q quit")
    await c.handle("enter")
    fits("e evidence", "x correct", "X supersede", "d disagree", "s suggest")
    await c.handle("e")
    fits("Esc back")
    await keys(c, "esc", "s")
    fits("Esc back")
    await keys(c, "esc", "x")
    fits("Enter submit")
    await keys(c, *"Why", "enter")
    fits("scroll")
    backend.write_outcomes.append(
        WriteOutcome("uncertain", "correct", "internal_error")
    )
    await c.handle("y")
    fits("scroll")
    backend.write_outcomes.append(recorded("correct"))
    await c.handle("R")
    await c.handle("esc")
    fits("q quit")
    await c.handle("P")
    fits("x reject", "Esc back")
    await c.handle("enter")
    fits("x reject", "Esc back")
    backend.diagnosis = WRITER
    await keys(c, "esc", "esc", "c")
    fits("r re-read")
    c.resize(24, 200)
    await keys(c, "esc", "j", "m", "k")
    assert c.render()[-1].text == (
        "Enter open  / search  f filter  r refresh  s suggest  c connection  "
        "S session  m mark  x correct  X supersede  d disagree  p promote  "
        "P proposals  q quit"
    )


class _WriteClient:
    """Stands in for MemoryClient: optionally dispatches, then fails or answers."""

    def __init__(
        self, dispatched: set[str], sent: bool, code: str | None, result: object
    ) -> None:
        self.dispatched, self.sent, self.code = dispatched, sent, code
        self.result = result

    async def _call(self, operation: str) -> Any:
        if self.sent:
            self.dispatched.add(f"/memory/v1/{operation}")
        if self.code is not None:
            raise MemoryOperationFailure(
                operation, FailureMetadata(self.code, "-", "never", None, None)
            )
        return self.result

    async def correct(self, *args: object, **kwargs: object) -> Any:
        return await self._call("correct")

    async def disagree(self, *args: object, **kwargs: object) -> Any:
        return await self._call("disagree")

    async def proposal_reject(self, *args: object, **kwargs: object) -> Any:
        return await self._call("proposal-reject")


ATTEMPTS: dict[str, WriteAttempt] = {
    "correct": CorrectionAttempt(UUID(uid(1)), "a", None, None, "Why", UUID(KEY1)),
    "disagree": DisagreementAttempt(
        UUID(uid(2)), "b", UUID(uid(1)), "a", "Why", UUID(KEY1)
    ),
    "proposal-reject": RejectionAttempt(
        proposal(1).proposal_id, "r", UUID(uid(1)), SCOPE, "Why", UUID(KEY1)
    ),
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "sent", "code", "expected"),
    [
        # cli.py failure_exit: correct is exit 3 on any uncertain code.
        ("correct", False, "transport_error", "uncertain"),
        ("correct", True, "internal_error", "uncertain"),
        ("correct", False, "upstream_access_denied", "uncertain"),
        ("correct", True, "authorisation_denied", "refused"),
        ("correct", False, "invalid_correction", "refused"),
        # disagree and proposal-reject: exit 3 only once their request was sent.
        ("disagree", False, "transport_error", "failed"),
        ("disagree", True, "transport_error", "uncertain"),
        ("disagree", True, "upstream_access_denied", "uncertain"),
        ("disagree", True, "authorisation_denied", "refused"),
        ("proposal-reject", False, "transport_error", "failed"),
        ("proposal-reject", True, "internal_error", "uncertain"),
        ("proposal-reject", True, "idempotency_conflict", "refused"),
    ],
)
async def test_backend_classifies_write_failures_as_the_cli_does(
    operation: str, sent: bool, code: str, expected: str
) -> None:
    dispatched = {"/memory/v1/stale"}
    backend = MemoryBrowseBackend(
        _WriteClient(dispatched, sent, code, None),  # type: ignore[arg-type]
        dispatched,
    )
    outcome = await backend.write(ATTEMPTS[operation])
    assert outcome == WriteOutcome(expected, operation, code)  # type: ignore[arg-type]
    assert "/memory/v1/stale" not in dispatched


@pytest.mark.anyio
async def test_backend_reports_receipts_and_distrusts_a_foreign_one() -> None:
    from cairn.client.proposal_types import ProposalMutation, ProposalRecorded
    from cairn.client.types import PersistenceReceipt, PersistenceStatus

    mutation = {"mutation_id": uid(310), "command_digest": DIGEST}
    audit = {"event_id": uid(311), "recorded_at": RECORDED_AT}
    receipt = PersistenceReceipt(
        PersistenceStatus.REPLAYED,
        UUID(KEY1),
        {"fact_ids": (uid(1),), "invalidated_at": RECORDED_AT},
        mutation,
        audit,
    )
    dispatched: set[str] = set()
    backend = MemoryBrowseBackend(
        _WriteClient(dispatched, True, None, receipt),  # type: ignore[arg-type]
        dispatched,
    )
    outcome = await backend.write(ATTEMPTS["correct"])
    assert outcome == recorded("correct", "replayed", invalidated_at=RECORDED_AT)
    skipped = PersistenceReceipt(PersistenceStatus.SKIPPED, UUID(KEY1))
    backend = MemoryBrowseBackend(
        _WriteClient(dispatched, True, None, skipped),  # type: ignore[arg-type]
        dispatched,
    )
    outcome = await backend.write(ATTEMPTS["disagree"])
    assert outcome == WriteOutcome("uncertain", "disagree", "invalid_response")
    foreign = ProposalMutation(
        "committed", ProposalRecorded(proposal(2).proposal_id), mutation, audit
    )
    backend = MemoryBrowseBackend(
        _WriteClient(dispatched, True, None, foreign),  # type: ignore[arg-type]
        dispatched,
    )
    outcome = await backend.write(ATTEMPTS["proposal-reject"])
    assert outcome == WriteOutcome("uncertain", "proposal-reject", "invalid_response")


# Session commands -----------------------------------------------------------

SESSION = UUID("cccccccc-0000-4000-8000-000000000001")
VISIT = UUID("dddddddd-0000-4000-8000-000000000001")
TURN = UUID("eeeeeeee-0000-4000-8000-000000000001")
NOT_REPEATED = "as entered; not repeated here"
VISIT_AT = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


def session_snapshot(
    state: Literal[
        "open", "started", "prepared", "abandoned", "committed", "skipped"
    ] = "open",
    *,
    turn: UUID | None = TURN,
    visit: UUID | None = VISIT,
    reason: str | None = None,
) -> Any:
    from cairn.client.session_types import SessionSnapshot

    return SessionSnapshot(
        session_id=SESSION,
        instance_id=UUID(uid(600)),
        principal_id=UUID(uid(900)),
        scope=SCOPE,
        classification=Classification.INTERNAL,
        state=state,
        operational_receipt={},
        custody_receipt=None,
        turn_id=turn,
        attempt_id=None if turn is None else UUID(uid(21)),
        replaces_turn_id=None,
        response=None,
        observations=(),
        abandonment_reason=reason,
        acknowledged_watermark=0,
        acknowledged_at=None,
        turn_count=0 if turn is None else 1,
        prepared_bytes=0,
        visit_id=visit,
        visit_watermark=None if visit is None else 1,
        visit_at=None if visit is None else VISIT_AT,
        custody_result=None,
        custody_audit_receipt=None,
        custody_idempotency_key=None,
    )


def arrival(excerpt: str = "The build uses port 8123.") -> Any:
    from cairn.client.briefing import (
        ArrivalBriefing,
        CurrentFactSummary,
        EvidenceReference,
    )
    from cairn.client.session_types import DurableArrival, SessionOperationResult

    briefing = ArrivalBriefing(
        since=None,
        summary=(
            CurrentFactSummary(
                UUID(uid(1)),
                excerpt,
                UUID(uid(900)),
                "agent",
                "candidate",
                False,
                False,
                EvidenceReference("recall", "hits", 0),
            ),
        ),
        recall=None,
        history=(),
        history_fact_ids=(),
        changes=(),
        warnings=("previous_visit_unknown",),
        failures=(),
        omitted_history_fact_ids=(),
        omitted_summary_count=0,
        budget=16384,
        budget_consumed=10,
        budget_exhausted=False,
    )
    visit = SessionOperationResult(
        "committed", session_snapshot(visit=VISIT), {"mutation_id": uid(400)}, {}
    )
    return DurableArrival(briefing, visit, False)


def session_profile(session: UUID | None = SESSION) -> Any:
    from cairn.client.profiles import MemoryProfile

    return MemoryProfile(
        "http://127.0.0.1",
        UUID(uid(600)),
        SCOPE,
        Classification.INTERNAL,
        Path("token"),
        session,
    )


SessionFake = Fake


def sessioned(
    *,
    session: UUID | None = SESSION,
    connection: ConnectionDiagnostics | None = None,
) -> tuple[BrowseController, SessionFake]:
    backend = SessionFake([fact(1), fact(2)])
    generator = ids()
    result = BrowseController(
        backend,
        scope=SCOPE,
        may_promote=False,
        connection=diagnosis() if connection is None else connection,
        identities=lambda: next(generator),
        profile=session_profile(session),
    )
    result.resize(40, 100)
    return result, backend


def sent(backend: Fake) -> list[Any]:
    return [call[1] for call in backend.calls if call[0] == "write"]


async def typed(c: BrowseController, text: str) -> None:
    await keys(c, *text, "enter")


async def remember_to_confirm(
    c: BrowseController, bodies: tuple[str, ...], response: str = "Done"
) -> None:
    await c.handle("w")
    for body in bodies:
        await typed(c, body)
    await c.handle("enter")
    assert c.input_kind == "response", c.message
    await typed(c, response)
    assert mode(c) == "confirm", c.message


def saved(operation: str = "remember") -> WriteOutcome:
    return WriteOutcome(
        "saved",
        operation,
        receipt_outcome="committed",
        mutation_id=uid(410),
        stage="committed",
        fact_ids=(uid(31),),
        custody_key=uid(411),
    )


@pytest.mark.anyio
async def test_session_view_without_session_id_offers_nothing() -> None:
    c, backend = sessioned(session=None)
    await c.start()
    assert "S session" in c._footer()
    await c.handle("S")
    assert mode(c) == "session"
    text = screen(c)
    assert "Session commands are unavailable for this profile." in text
    assert "session_id_required" in text and "Nothing was sent." in text
    assert c.render()[-1].text == "Esc back"
    await keys(c, "r", "a", "v", "w", "u", "b")
    assert mode(c) == "session"
    assert {call[0] for call in backend.calls} == {"page"}
    await c.handle("esc")
    assert mode(c) == "list"


@pytest.mark.anyio
async def test_session_view_reads_status_on_opening_and_on_r() -> None:
    c, backend = sessioned()
    c.resize(40, 80)
    await c.start()
    backend.snapshot = session_snapshot("started", reason=HOSTILE)
    await c.handle("S")
    assert backend.calls[-1] == ("session_status",)
    text = screen(c)
    assert f"Session {SESSION}" in text and "state: started" in text
    assert f"turn_id: {TURN}" in text and f"visit_id: {VISIT}" in text
    assert "committed custody receipt: none" in text
    assert f"visit for v: {VISIT} (reported by status)" in text
    assert f"turn for u and b: {TURN} (reported by status)" in text
    assert "\x1b" not in text and "\\x1b[2J" in text
    footer = c.render()[-1].text
    assert footer == c._footer() and len(footer) <= 79, footer
    for part in ("r status", "a arrive", "v ack visit", "w remember", "u resume"):
        assert part in footer
    assert footer.endswith("b abandon  Esc back")
    backend.status_failure = BrowseFailure("session-read", "transport_error")
    await c.handle("r")
    assert [call for call in backend.calls if call[0] == "session_status"] == [
        ("session_status",)
    ] * 2
    text = screen(c)
    assert "status failed: transport_error" in text and "state: started" not in text
    assert sent(backend) == []


@pytest.mark.anyio
async def test_session_writes_need_retrieve_and_ingest() -> None:
    c, backend = sessioned(connection=diagnosis(ingest=False))
    await c.start()
    await c.handle("S")
    assert backend.calls[-1] == ("session_status",)
    assert "a arrive" not in c._footer() and "r status" in c._footer()
    for key in ("a", "v", "w", "u", "b"):
        await c.handle(key)
        assert mode(c) == "session"
        assert c.message == "session commands not permitted for this profile"
    assert sent(backend) == []
    c, backend = sessioned(connection=diagnosis(retrieve=False))
    await c.start()
    await c.handle("S")
    assert ("session_status",) not in backend.calls
    assert c.message == "status not permitted for this profile"
    assert c._footer() == "j/k scroll  Esc back"
    c.connection = None
    assert not c.may_session_write and c.may_retrieve
    await c.handle("r")
    assert backend.calls[-1] == ("session_status",)
    await c.handle("a")
    assert c.message == "session commands not permitted for this profile"
    await keys(c, "esc", "c")
    assert "S session: status needs retrieve" in screen(c)


@pytest.mark.anyio
async def test_arrive_confirms_shows_the_briefing_and_never_acknowledges() -> None:
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", "a")
    assert mode(c) == "input" and c.input_kind == "query"
    await c.handle("enter")
    assert c.message == "a query is required" and mode(c) == "input"
    await typed(c, "current work")
    assert mode(c) == "confirm" and sent(backend) == []
    text = screen(c)
    assert "Open or replay the session and issue a new visit?" in text
    assert "opens or replays the session (session-open)" in text
    assert "issues a fresh visit (visit-issue)" in text
    assert "The visit is not acknowledged" in text and "current work" in text
    assert (
        c._prompt()
        == "Press y to open the session and issue a visit; Esc or n cancels."
    )
    backend.write_outcomes.append(
        WriteOutcome(
            "recorded",
            "arrive",
            receipt_outcome="committed",
            visit_id=str(VISIT),
            arrival=arrival(HOSTILE),
        )
    )
    await c.handle("y")
    [first] = sent(backend)
    assert first == ArrivalAttempt("current work", UUID(KEY1))
    text = screen(c)
    assert "Briefing. The visit is NOT acknowledged." in text
    assert f"visit_id: {VISIT}" in text and "previous_visit_unknown" in text
    assert "\x1b" not in text and "\\x1b[2J" in text
    assert not c._retryable() and c._prompt() == "Esc returns."
    await c.handle("esc")
    assert mode(c) == "session" and backend.calls[-1] == ("session_status",)
    assert len(sent(backend)) == 1
    assert f"visit for v: {VISIT} (issued by this browser's arrival)" in screen(c)
    await c.handle("v")
    assert mode(c) == "confirm" and len(sent(backend)) == 1
    assert f"visit_id: {VISIT} (issued by this browser's arrival)" in screen(c)
    backend.write_outcomes.append(
        WriteOutcome(
            "recorded",
            "acknowledge-visit",
            receipt_outcome="committed",
            snapshot=session_snapshot(),
        )
    )
    await c.handle("y")
    assert sent(backend)[-1] == AcknowledgementAttempt(
        VISIT, "issued by this browser's arrival"
    )
    text = screen(c)
    assert f"Visit {VISIT} acknowledged." in text
    assert "Operational receipt (committed); this saves no facts." in text
    assert "saved" not in text.replace("saves no facts", "")
    await c.handle("esc")
    assert c.issued_visit is None and not c.unresolved_writes


@pytest.mark.anyio
async def test_failed_arrival_says_a_visit_may_exist_unacknowledged() -> None:
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", "a", *"work", "enter")
    backend.write_outcomes.append(
        WriteOutcome("failed", "recall", "transport_error", stage="open")
    )
    await c.handle("y")
    text = screen(c)
    assert "Arrival not completed (recall: transport_error)." in text
    assert "A visit may have been issued; it is not acknowledged." in text
    assert not c._retryable() and not c.unresolved_writes
    backend.write_outcomes.append(
        WriteOutcome("refused", "session-open", "authorisation_denied")
    )
    await keys(c, "esc", "a", *"work", "enter", "y")
    assert "Arrival refused by Cairn (session-open: authorisation_denied)." in screen(c)
    assert recovery_packet(c) is None


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["a", "w", "v", "u", "b"])
async def test_session_commands_cancelled_send_nothing(key: str) -> None:
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", key)
    if mode(c) == "input":
        await c.handle("esc")
        assert mode(c) == "session" and c.message == "cancelled; nothing was sent"
        await c.handle(key)
        if key in {"a", "b"}:
            await typed(c, "why")
        elif key == "w":
            await keys(c, "enter", "enter")
    assert mode(c) == "confirm", c.message
    await c.handle("n")
    assert mode(c) == "session" and c.message.endswith("cancelled; nothing was sent")
    assert sent(backend) == []


@pytest.mark.anyio
async def test_remember_confirms_everything_and_reports_only_a_custody_receipt() -> (
    None
):
    from cairn.client.cli_input import command_key

    c, backend = sessioned()
    await c.start()
    await c.handle("S")
    await remember_to_confirm(c, ("The build uses port 8123.", "CI runs nightly."))
    attempt = c.write
    assert isinstance(attempt, RememberAttempt)
    assert (attempt.turn_id, attempt.attempt_id) == (
        UUID(KEY1),
        UUID("aaaaaaaa-0000-4000-8000-000000000002"),
    )
    text = screen(c)
    for part in (
        f"turn_id: {KEY1} (new)",
        "attempt_id: aaaaaaaa-0000-4000-8000-000000000002 (new)",
        "response (4 bytes):",
        "  Done",
        "observations: 2",
        "The build uses port 8123.",
        "CI runs nightly.",
        f"turn-begin key: {command_key('begin', SESSION, attempt.turn_id, attempt.attempt_id)}",
        f"turn-prepare key: {command_key('prepare', SESSION, attempt.turn_id, attempt.attempt_id)}",
    ):
        assert part in text, part
    assert sent(backend) == []
    backend.write_outcomes.append(saved())
    await c.handle("y")
    assert sent(backend) == [attempt]
    text = screen(c)
    assert "Saved: committed custody receipt." in text
    assert (
        f"fact_ids: {uid(31)}" in text
        and f"custody idempotency key: {uid(411)}" in text
    )
    pages = len([call for call in backend.calls if call[0] == "page"])
    await c.handle("esc")
    assert len([call for call in backend.calls if call[0] == "page"]) == pages + 1
    # status is now read for this browser's own turn.
    assert backend.calls[-1] == ("session_status", attempt.turn_id)
    assert mode(c) == "session"
    assert c.message == "saved; listing refreshed and status re-read"
    assert f"turn for u and b: {KEY1} (this browser's latest checkpoint)" in screen(c)


@pytest.mark.anyio
async def test_skipped_and_prepared_are_never_rendered_as_saved() -> None:
    c, backend = sessioned()
    await c.start()
    await c.handle("S")
    await remember_to_confirm(c, (), "")
    text = screen(c)
    assert "observations: 0" in text and "(empty)" in text
    assert "Cairn records the turn as skipped and saves no facts." in text
    backend.write_outcomes.append(WriteOutcome("skipped", "remember", stage="skipped"))
    await c.handle("y")
    text = screen(c)
    assert "Skipped: no observations, so no facts were saved." in text
    assert "Saved:" not in text and not c.unresolved_writes
    await c.handle("esc")
    await remember_to_confirm(c, ("Port 8123",))
    backend.write_outcomes.append(
        WriteOutcome(
            "prepared",
            "remember",
            stage="prepared",
            recovery="status_then_resume_without_regeneration",
        )
    )
    await c.handle("y")
    text = screen(c)
    assert "Prepared: Cairn holds the output, but fact custody is not" in text
    assert "Nothing is reported as saved." in text
    assert "Saved:" not in text and "saved;" not in c.message
    assert c._prompt() == "R resubmits identically; u resumes the turn; Esc returns."
    assert list(c.unresolved_writes) == [UUID("aaaaaaaa-0000-4000-8000-000000000003")]


@pytest.mark.anyio
async def test_unknown_checkpoint_retries_exactly_then_resumes_the_same_turn() -> None:
    c, backend = sessioned()
    await c.start()
    await c.handle("S")
    await remember_to_confirm(c, ("Port 8123",))
    backend.write_outcomes.append(
        WriteOutcome(
            "uncertain",
            "turn-prepare",
            "internal_error",
            stage="started",
            recovery="resubmit_identical_checkpoint_same_identities",
        )
    )
    await c.handle("y")
    text = screen(c)
    assert "Checkpoint outcome unknown (turn-prepare: internal_error)." in text
    assert "Nothing is reported as saved, and nothing is reported as failed." in text
    assert "recovery: resubmit_identical_checkpoint_same_identities" in text
    assert "Saved:" not in text
    # The content needed for an identical resubmission stays on the screen;
    # the confirm screen cannot be reopened.
    whole = "\n".join(line.text for line in c._screen_lines())
    assert "resubmit exactly this response and these observations:" in whole
    assert "Port 8123" in whole
    assert "Done" in whole
    backend.write_outcomes.append(
        WriteOutcome("failed", "session-open", "transport_error", stage="unconfirmed")
    )
    await c.handle("R")
    first, second = sent(backend)
    assert second is first
    assert "Retry did not complete (session-open: transport_error)." in screen(c)
    # A new arrival is blocked; resume and abandon remain the recovery steps.
    await keys(c, "esc", "a")
    assert mode(c) == "outcome" and "resolve it before starting another" in c.message
    await c.handle("u")
    assert mode(c) == "confirm"
    resume = c.write
    assert resume == ResumeAttempt(
        first.turn_id, "this browser's unresolved checkpoint"
    )
    backend.write_outcomes.append(saved("resume"))
    await c.handle("y")
    assert "Saved: committed custody receipt." in screen(c)
    assert not c.unresolved_writes and recovery_packet(c) is None


@pytest.mark.anyio
async def test_session_recovery_packet_names_the_exact_command_and_stdin() -> None:
    from cairn.client.cli_input import FIELDS

    c, backend = sessioned()
    await c.start()
    await c.handle("S")
    await remember_to_confirm(c, ("Secret body",), "Secret response")
    backend.write_outcomes.append(
        WriteOutcome(
            "uncertain",
            "turn-begin",
            "transport_error",
            stage="open",
            recovery="resubmit_identical_checkpoint_same_identities",
        )
    )
    await c.handle("y")
    attempt = sent(backend)[0]
    packet: Any = recovery_packet(c, "/tmp/browse profile.json")
    assert "Secret" not in json.dumps(packet)
    assert packet["error"] == {"code": "transport_error", "operation": "turn-begin"}
    assert packet["recovery"] == "resubmit_identical_checkpoint_same_identities"
    [entry] = packet["checkpoints"]
    assert entry == {
        "operation": "remember",
        "recovery": "resubmit_identical_checkpoint_same_identities",
        "last_confirmed_stage": "open",
        "command": "cairn-memory --profile '/tmp/browse profile.json' remember",
        "stdin": {
            "turn_id": str(attempt.turn_id),
            "attempt_id": str(attempt.attempt_id),
            "response": NOT_REPEATED,
            "observations": NOT_REPEATED,
        },
    }
    assert set(entry["stdin"]) == FIELDS["remember"][0]
    # resume of the same turn keeps the original checkpoint as the record.
    await c.handle("u")
    backend.write_outcomes.append(
        WriteOutcome(
            "uncertain",
            "turn-commit",
            "transport_error",
            stage="prepared",
            recovery="status_then_resume_without_regeneration",
        )
    )
    await c.handle("y")
    packet = recovery_packet(c)
    assert packet["checkpoints"][0]["command"].endswith("--profile PATH remember")
    assert "resumes" not in packet
    # A status-reported turn: the CLI's status-then-resume, resume command.
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", "u")
    backend.write_outcomes.append(
        WriteOutcome(
            "prepared",
            "resume",
            stage="prepared",
            recovery="status_then_resume_without_regeneration",
        )
    )
    await c.handle("y")
    packet = recovery_packet(c)
    assert packet["resumes"] == [
        {
            "operation": "resume",
            "recovery": "status_then_resume_without_regeneration",
            "last_confirmed_stage": "prepared",
            "command": "cairn-memory --profile PATH resume",
            "stdin": {"turn_id": str(TURN)},
        }
    ]
    assert packet["error"] == {"code": "outcome_unknown", "operation": "resume"}


@pytest.mark.anyio
async def test_abandon_needs_a_reason_and_confirmation() -> None:
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", "b")
    assert mode(c) == "input" and c.input_kind == "reason" and sent(backend) == []
    await c.handle("enter")
    assert c.message == "a reason is required" and mode(c) == "input"
    await typed(c, "Output was lost")
    assert mode(c) == "confirm" and sent(backend) == []
    text = screen(c)
    assert f"turn_id: {TURN} (reported by status)" in text
    assert "Fences an unprepared started turn" in text and "Output was lost" in text
    assert c._prompt() == "Press y to abandon the turn; Esc or n cancels."
    backend.write_outcomes.append(
        WriteOutcome("refused", "turn-abandon", "invalid_state", stage="unconfirmed")
    )
    await c.handle("y")
    assert sent(backend) == [
        AbandonAttempt(TURN, "reported by status", "Output was lost")
    ]
    text = screen(c)
    assert "Abandonment refused by Cairn (turn-abandon: invalid_state)." in text
    assert not c._retryable() and not c.unresolved_writes
    await keys(c, "esc", "b", *"Lost", "enter")
    backend.write_outcomes.append(
        WriteOutcome(
            "recorded",
            "abandon",
            receipt_outcome="committed",
            snapshot=session_snapshot("abandoned"),
        )
    )
    await c.handle("y")
    text = screen(c)
    assert f"Turn {TURN} abandoned." in text and "session state: abandoned" in text
    assert "this saves no facts" in text


@pytest.mark.anyio
async def test_unknown_acknowledgement_keeps_the_visit_for_an_exact_retry() -> None:
    c, backend = sessioned()
    await c.start()
    await keys(c, "S", "v")
    backend.write_outcomes.append(
        WriteOutcome(
            "uncertain",
            "visit-acknowledge",
            "transport_error",
            stage="unconfirmed",
            recovery="resubmit_identical_acknowledge-visit_same_visit_id",
        )
    )
    await c.handle("y")
    assert "Acknowledgement outcome unknown" in screen(c)
    packet: Any = recovery_packet(c)
    assert packet["acknowledgements"][0]["stdin"] == {"visit_id": str(VISIT)}
    assert packet["acknowledgements"][0]["command"].endswith("acknowledge-visit")
    backend.write_outcomes.append(
        WriteOutcome(
            "recorded",
            "acknowledge-visit",
            receipt_outcome="replayed",
            snapshot=session_snapshot(),
        )
    )
    await c.handle("R")
    first, second = sent(backend)
    assert first is second and recovery_packet(c) is None


@pytest.mark.anyio
async def test_session_input_validation_at_the_documented_limits() -> None:
    c, _ = sessioned()
    await c.start()
    await keys(c, "S", "a")
    c.input_text = "q" * 8191
    await keys(c, "é", "x")
    assert c.input_text == "q" * 8191 + "x"
    await keys(c, "esc", "w")
    c.input_text = "b" * 4095
    await keys(c, "1", "2")
    assert len(c.input_text.encode()) == 4096
    await c.handle("enter")
    assert c.draft_observations == ["b" * 4095 + "1"]
    for n in range(2, 9):
        await typed(c, f"observation {n}")
    # The eighth body ends the list by itself.
    assert c.input_kind == "response" and len(c.draft_observations) == 8
    c.input_text = "r" * 32767
    await keys(c, "s", "t")
    assert len(c.input_text) == 32768
    # Escaped quotes overrun the 73728-byte preparation bound.
    c.draft_observations = ['"' * 4096] * 8
    await c.handle("enter")
    assert mode(c) == "input"
    assert c.message.startswith("the checkpoint exceeds the 73728-byte")
    await c.handle("esc")
    assert mode(c) == "session" and c.draft_observations == []
    await keys(c, "b")
    c.input_text = "r" * 4096
    await keys(c, "x")
    assert len(c.input_text) == 4096


@pytest.mark.anyio
async def test_session_footers_fit_eighty_columns() -> None:
    c, backend = sessioned()
    c.resize(24, 80)
    await c.start()

    def fits(*parts: str) -> None:
        footer = c.render()[-1].text
        assert footer == c._footer() and len(footer) <= 79, footer
        for part in parts:
            assert part in footer, (part, footer)

    fits("S session", "q quit")
    await c.handle("S")
    fits("a arrive", "w remember", "b abandon", "Esc back")
    for key in ("a", "w", "b"):
        await c.handle(key)
        fits("Enter submit")
        await c.handle("esc")
    await keys(c, "a", *"q", "enter")
    fits("scroll")
    backend.write_outcomes.append(
        WriteOutcome("recorded", "arrive", visit_id=str(VISIT), arrival=arrival())
    )
    await c.handle("y")
    fits("scroll")


@pytest.mark.anyio
async def test_backend_turn_outcomes_follow_the_cli() -> None:
    from cairn.client.browse_terminal import _turn
    from cairn.client.session_types import DurableTurnResult
    from cairn.client.types import PersistenceReceipt, PersistenceStatus

    def result(state: Any, persistence: Any = None) -> Any:
        return DurableTurnResult(
            SESSION, TURN, UUID(uid(21)), state, None, persistence, session_snapshot()
        )

    committed = PersistenceReceipt(
        PersistenceStatus.COMMITTED,
        UUID(uid(411)),
        {"fact_ids": (uid(31),)},
        {"mutation_id": uid(410)},
        {"event_id": uid(412), "recorded_at": RECORDED_AT},
    )
    outcome = _turn("remember", result("committed", committed))
    assert outcome.status == "saved" and outcome.fact_ids == (uid(31),)
    assert outcome.custody_key == uid(411)
    # Committed without a custody result is not a saved claim.
    bare = PersistenceReceipt(PersistenceStatus.COMMITTED, None)
    assert _turn("remember", result("committed", bare)).status == "uncertain"
    skipped = PersistenceReceipt(PersistenceStatus.SKIPPED, None)
    assert _turn("remember", result("committed", skipped)).status == "uncertain"
    assert _turn("remember", result("skipped", skipped)).status == "skipped"
    assert _turn("resume", result("abandoned")).status == "abandoned"
    prepared = _turn("resume", result("prepared"))
    assert prepared.status == "prepared"
    assert prepared.recovery == "status_then_resume_without_regeneration"
    interrupted = _turn("remember", result("interrupted"))
    assert interrupted.recovery == "resubmit_identical_checkpoint_same_identities"
