"""Terminal-free controller tests for ``cairn-memory browse``."""

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cairn.catalogue.audit import Scope, ScopeSegment
from cairn.client.browse import (
    BrowseController,
    BrowseFailure,
    Fact,
    Page,
    PromotionAttempt,
    PromotionOutcome,
    clip,
    sanitise,
    wrap,
)
from cairn.client.browse_terminal import key_name, recovery_packet

SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
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


class Fake:
    def __init__(self, facts: list[dict[str, object]], size: int = 3) -> None:
        self.facts, self.size = facts, size
        self.calls: list[tuple[object, ...]] = []
        self.outcomes: list[PromotionOutcome] = []
        self.expired = False

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


def ids() -> Iterator[UUID]:
    n = 0
    while True:
        n += 1
        yield UUID(f"aaaaaaaa-0000-4000-8000-{n:012x}")


def controller(
    facts: list[dict[str, object]], *, may_promote: bool = True, size: int = 3
) -> tuple[BrowseController, Fake]:
    backend = Fake(facts, size)
    generator = ids()
    result = BrowseController(
        backend,
        scope=SCOPE,
        may_promote=may_promote,
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
