from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_retrieval import (
    _CORRELATION_ID,
    _DATA_GRANT_ID,
    _EVIDENCE_PAYLOAD,
    _SCOPE,
    _agent_actor,
    _authority,
    _ingest_with_evidence,
    _last_realm_event,
    _rows,
    _ScriptedAttic,
    _seed_agent,
    _seed_catalogue,
)

from cairn.authority.evidence_window import (
    EvidenceWindow,
    EvidenceWindowResult,
    InvalidOffset,
    WindowMode,
    offset_window,
    query_window,
    window_cost,
)
from cairn.authority.literal_locator import TooManyTerms
from cairn.authority.memory_page_types import PageBudgetTooSmall, PageRejected
from cairn.catalogue.audit import Outcome
from cairn.catalogue.transactions import FailureCode, Rejected

_ID = UUID(int=7, version=4)


def _q(
    text: str, query: str, budget: int = 16384
) -> EvidenceWindowResult | PageBudgetTooSmall:
    data = text.encode()
    return query_window(
        text,
        evidence_id=_ID,
        sha256="0" * 64,
        byte_length=len(data),
        query=query,
        budget=budget,
    )


def _o(
    text: str, start: int, budget: int = 16384
) -> EvidenceWindowResult | PageBudgetTooSmall:
    data = text.encode()
    return offset_window(
        text, data, evidence_id=_ID, sha256="0" * 64, start=start, budget=budget
    )


def _ok(result: EvidenceWindowResult | PageBudgetTooSmall) -> EvidenceWindowResult:
    assert isinstance(result, EvidenceWindowResult)
    return result


def _text(result: EvidenceWindowResult) -> str:
    assert result.text is not None
    return result.text


def _span(result: EvidenceWindowResult) -> tuple[int, int]:
    assert result.start_byte is not None and result.end_byte is not None
    return result.start_byte, result.end_byte


def test_query_window_takes_two_lines_before_and_six_from_anchor() -> None:
    text = "".join(f"line {i}\n" for i in range(20))
    result = _ok(_q(text, "line 10"))
    assert result.mode is WindowMode.QUERY and result.match_found is True
    assert result.text == "".join(f"line {i}\n" for i in range(8, 16))
    assert result.prefix_omitted and result.suffix_omitted
    assert result.next_start_byte == result.end_byte
    assert result.match_start_byte is not None and result.match_end_byte is not None
    assert text.encode()[result.match_start_byte : result.match_end_byte] == b"line 10"
    assert result.budget_consumed == window_cost(result)


def test_query_offsets_are_original_utf8_bytes() -> None:
    text = "été\nDie Straße liegt\nend\n"
    payload = text.encode()
    result = _ok(_q(text, "STRASSE"))
    assert result.match_start_byte is not None and result.match_end_byte is not None
    assert payload[result.match_start_byte : result.match_end_byte] == "Straße".encode()
    start, end = _span(result)
    assert payload[start:end].decode() == result.text == text
    assert result.prefix_omitted is False and result.suffix_omitted is False
    assert result.next_start_byte is None


def test_budget_trims_preceding_then_following_but_keeps_the_match() -> None:
    text = "a" * 500 + "NEEDLE" + "b" * 500
    full = _ok(_q(text, "needle"))
    tight = _ok(_q(text, "needle", budget=window_cost(full) - 400))
    assert "NEEDLE" in _text(tight)
    # Preceding context goes first: the following context is untouched.
    assert tight.start_byte is not None and full.start_byte is not None
    assert tight.start_byte > full.start_byte and tight.end_byte == full.end_byte
    assert tight.budget_consumed <= window_cost(full) - 400
    tighter = _ok(_q(text, "needle", budget=window_cost(full) - 700))
    assert _text(tighter).startswith("NEEDLE")
    assert tighter.end_byte is not None and full.end_byte is not None
    assert tighter.end_byte < full.end_byte


def test_match_that_cannot_fit_is_refused_with_minimum() -> None:
    text = "x" * 50 + " needle " + "y" * 50
    expected = window_cost(
        EvidenceWindowResult(
            evidence_id=_ID,
            mode=WindowMode.QUERY,
            text="needle",
            start_byte=51,
            end_byte=57,
            sha256="0" * 64,
            byte_length=len(text.encode()),
            match_found=True,
            match_start_byte=51,
            match_end_byte=57,
            prefix_omitted=True,
            suffix_omitted=True,
            next_start_byte=57,
        )
    )
    refused = _q(text, "needle", budget=20)
    assert isinstance(refused, PageBudgetTooSmall)
    assert refused.reason == "page_budget_too_small"
    assert refused.minimum_budget == expected
    exact = _ok(_q(text, "needle", budget=expected))
    assert exact.text == "needle" and exact.budget_consumed == expected
    short = _q(text, "needle", budget=expected - 1)
    assert isinstance(short, PageBudgetTooSmall) and short.minimum_budget == expected


def test_match_near_end_of_a_one_mebibyte_line() -> None:
    text = "z" * (1_048_576 - 20) + " target end"
    result = _ok(_q(text, "target", budget=4096))
    assert result.match_found and "target" in _text(result)
    assert result.budget_consumed <= 4096


def test_no_match_is_a_metadata_only_variant() -> None:
    result = _ok(_q("abc", "absent"))
    assert result.match_found is False and result.text is None
    assert result.start_byte is None and result.end_byte is None
    assert result.match_start_byte is None and result.match_end_byte is None
    assert result.prefix_omitted is None and result.suffix_omitted is None
    assert result.next_start_byte is None and result.budget_consumed == window_cost(
        result
    )
    assert result.sha256 == "0" * 64 and result.byte_length == 3


def test_no_match_metadata_still_needs_budget() -> None:
    cost = _ok(_q("abc", "absent")).budget_consumed
    refused = _q("abc", "absent", budget=cost - 1)
    assert isinstance(refused, PageBudgetTooSmall) and refused.minimum_budget == cost
    assert isinstance(_q("abc", "absent", budget=cost), EvidenceWindowResult)


def test_window_edges_never_split_a_scalar() -> None:
    text = "é" * 100
    payload = text.encode()
    # A budget that leaves exactly one spare byte: a byte-oriented builder would
    # spend it on half of the next two-byte scalar.
    some = _ok(_o(text, 0, budget=window_cost(_ok(_o(text, 200))) + 41))
    budget = some.budget_consumed + 1
    result = _ok(_o(text, 0, budget=budget))
    assert budget - result.budget_consumed == 1
    start, end = _span(result)
    assert 0 < end < len(payload) and (end - start) % 2 == 0
    assert payload[start:end].decode("utf-8") == result.text
    # Query mode trims context on scalar edges too.
    query_text = "é" * 60 + " needle " + "é" * 60
    full = _ok(_q(query_text, "needle"))
    for spare in (1, 3, 5):
        trimmed = _ok(
            _q(query_text, "needle", budget=full.budget_consumed - 40 - spare)
        )
        low, high = _span(trimmed)
        assert query_text.encode()[low:high].decode("utf-8") == trimmed.text


def test_offset_mid_scalar_and_out_of_range_are_invalid() -> None:
    with pytest.raises(InvalidOffset):
        _o("é", 1)
    with pytest.raises(InvalidOffset):
        _o("é", 3)
    with pytest.raises(InvalidOffset):
        _o("é", -1)


def test_offset_budget_must_hold_one_scalar() -> None:
    refused = _o("€abc", 0, budget=1)
    assert isinstance(refused, PageBudgetTooSmall)
    exact = _ok(_o("€abc", 0, budget=refused.minimum_budget))
    assert exact.text == "€" and exact.next_start_byte == 3
    assert isinstance(
        _o("€abc", 0, budget=refused.minimum_budget - 1), PageBudgetTooSmall
    )


def test_offset_minimum_counts_a_cheaper_window_reaching_eof() -> None:
    # At seven-digit offsets the EOF window (null continuation) is one byte
    # cheaper than a one-scalar window that leaves a suffix.
    text = "a" * 1_000_010
    start = len(text) - 2
    refused = _o(text, start, budget=1)
    assert isinstance(refused, PageBudgetTooSmall)
    exact = _ok(_o(text, start, budget=refused.minimum_budget))
    assert exact.text == "aa" and exact.next_start_byte is None
    assert exact.budget_consumed == refused.minimum_budget
    short = _o(text, start, budget=refused.minimum_budget - 1)
    assert isinstance(short, PageBudgetTooSmall)
    assert short.minimum_budget == refused.minimum_budget


def test_query_minimum_counts_a_cheaper_window_reaching_eof() -> None:
    text = "a" * 1_000_000 + " needle" + "x"
    refused = _q(text, "needle", budget=1)
    assert isinstance(refused, PageBudgetTooSmall)
    exact = _ok(_q(text, "needle", budget=refused.minimum_budget))
    assert exact.text == "needlex" and exact.next_start_byte is None
    assert exact.budget_consumed == refused.minimum_budget
    short = _q(text, "needle", budget=refused.minimum_budget - 1)
    assert isinstance(short, PageBudgetTooSmall)
    assert short.minimum_budget == refused.minimum_budget


def test_offset_at_eof_is_an_empty_final_window() -> None:
    result = _ok(_o("abc", 3))
    assert result.mode is WindowMode.OFFSET
    assert result.text == "" and result.start_byte == result.end_byte == 3
    assert result.prefix_omitted is True and result.suffix_omitted is False
    assert result.next_start_byte is None and result.match_found is None
    empty = _ok(_o("", 0))
    assert empty.text == "" and empty.prefix_omitted is False


def test_offset_paging_makes_progress_to_eof() -> None:
    text = "abcdefghij" * 50
    start, seen = 0, ""
    budget = window_cost(_ok(_o(text, len(text)))) + 30
    while True:
        result = _ok(_o(text, start, budget=budget))
        assert result.start_byte == start
        seen += _text(result)
        if result.next_start_byte is None:
            break
        assert result.next_start_byte > start
        start = result.next_start_byte
    assert seen == text


# --- the audited authority operation ---------------------------------------

_ACTION = "memory-evidence-window"


def _seeded(tmp_path: Path) -> UUID:
    _seed_catalogue(tmp_path)
    _seed_agent(tmp_path)
    _, evidence = _ingest_with_evidence(tmp_path, bodies=("fact",))
    return evidence


def _call(
    tmp_path: Path,
    command: EvidenceWindow,
    attic: _ScriptedAttic | None = None,
    *,
    enabled: bool = True,
) -> EvidenceWindowResult | Rejected | PageRejected:
    authority = _authority(tmp_path, attic=attic or _ScriptedAttic())
    authority._exact_evidence_enabled = enabled
    return authority.evidence_window(
        _agent_actor(), command, correlation_id=_CORRELATION_ID
    )


def _assert_denied(tmp_path: Path, reason: str) -> None:
    event = _last_realm_event(tmp_path)
    assert event.draft.action_code == _ACTION
    assert event.draft.outcome is Outcome.DENY
    assert event.draft.reason_code == reason
    assert event.draft.grant_id == _DATA_GRANT_ID
    assert event.draft.requested_scope == _SCOPE


def test_query_window_reads_verified_payload_and_audits(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, query="TEST"), attic)
    assert isinstance(result, EvidenceWindowResult)
    assert result.text == _EVIDENCE_PAYLOAD.decode() and result.match_found
    assert result.mode is WindowMode.QUERY
    assert (result.match_start_byte, result.match_end_byte) == (14, 18)
    assert result.byte_length == len(_EVIDENCE_PAYLOAD)
    assert attic.fetches == [evidence] and attic.calls == []
    event = _last_realm_event(tmp_path)
    assert event.draft.action_code == _ACTION
    assert event.draft.outcome is Outcome.ALLOW
    assert event.draft.reason_code == "evidence_window_completed"
    assert event.draft.grant_id == _DATA_GRANT_ID
    assert event.draft.affected_evidence_ids == (evidence,)


def test_query_without_a_match_is_a_metadata_only_result(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, query="absent"))
    assert isinstance(result, EvidenceWindowResult)
    assert result.match_found is False and result.text is None
    assert _last_realm_event(tmp_path).draft.reason_code == "evidence_window_completed"


def test_offset_window_starts_at_the_byte_offset(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, start=14))
    assert isinstance(result, EvidenceWindowResult)
    assert result.mode is WindowMode.OFFSET
    assert result.text == _EVIDENCE_PAYLOAD[14:].decode()
    assert (result.start_byte, result.end_byte) == (14, len(_EVIDENCE_PAYLOAD))
    default = _call(tmp_path, EvidenceWindow(_SCOPE, evidence))
    assert isinstance(default, EvidenceWindowResult)
    assert default.start_byte == 0 and default.text == _EVIDENCE_PAYLOAD.decode()


def test_offset_beyond_the_payload_is_refused_after_the_read(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    start = len(_EVIDENCE_PAYLOAD) + 1
    result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, start=start), attic)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    assert attic.fetches == [evidence]
    _assert_denied(tmp_path, "invalid_offset")


def test_corrupt_payload_is_refused_before_any_window(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    result = _call(
        tmp_path,
        EvidenceWindow(_SCOPE, evidence, query="x"),
        _ScriptedAttic(payload=b"tampered"),
    )
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.EVIDENCE_CORRUPT


@pytest.mark.parametrize("enabled", [True, False])
def test_secret_query_is_refused_before_attic(tmp_path: Path, enabled: bool) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    query = "-----BEGIN RSA PRIVATE KEY-----"
    command = EvidenceWindow(_SCOPE, evidence, query=query)
    result = _call(tmp_path, command, attic, enabled=enabled)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.SECRET_REJECTED
    assert attic.fetches == []
    _assert_denied(tmp_path, "memory_secret_rejected")


@pytest.mark.parametrize(
    "query",
    [
        "",
        " \t\n ",
        "x" * 8193,
        " ".join(f"t{i}" for i in range(33)),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_invalid_query_is_refused_before_attic(
    tmp_path: Path, query: str, enabled: bool
) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    command = EvidenceWindow(_SCOPE, evidence, query=query)
    result = _call(tmp_path, command, attic, enabled=enabled)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    assert attic.fetches == []
    _assert_denied(tmp_path, "invalid_query")


def test_query_limits_are_inclusive(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    for query in ("x" * 8192, " ".join(f"t{i}" for i in range(32)) + " T0"):
        result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, query=query))
        assert isinstance(result, EvidenceWindowResult)


def test_query_and_start_together_are_invalid(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    command = EvidenceWindow(_SCOPE, evidence, query="x", start=0)
    result = _call(tmp_path, command, attic)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    assert attic.fetches == []
    _assert_denied(tmp_path, "invalid_query")


@pytest.mark.parametrize(
    ("budget", "start", "reason"),
    [
        (0, None, "invalid_budget"),
        (1_048_577, None, "invalid_budget"),
        (16384, -1, "invalid_offset"),
        (16384, 1_048_577, "invalid_offset"),
    ],
)
def test_out_of_range_parameters_are_refused_before_attic(
    tmp_path: Path, budget: int, start: int | None, reason: str
) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    command = EvidenceWindow(_SCOPE, evidence, budget=budget, start=start)
    result = _call(tmp_path, command, attic, enabled=False)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    assert attic.fetches == []
    _assert_denied(tmp_path, reason)


def test_disabled_evidence_is_refused_without_attic(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    attic = _ScriptedAttic()
    command = EvidenceWindow(_SCOPE, evidence, query="deterministic")
    result = _call(tmp_path, command, attic, enabled=False)
    assert isinstance(result, Rejected)
    assert attic.fetches == []
    _assert_denied(tmp_path, "evidence_disabled")


def test_budget_too_small_uses_page_rejection(tmp_path: Path) -> None:
    evidence = _seeded(tmp_path)
    expected = query_window(
        _EVIDENCE_PAYLOAD.decode(),
        evidence_id=evidence,
        sha256="0" * 64,
        byte_length=len(_EVIDENCE_PAYLOAD),
        query="deterministic",
        budget=5,
    )
    assert isinstance(expected, PageBudgetTooSmall)
    command = EvidenceWindow(_SCOPE, evidence, budget=5, query="deterministic")
    result = _call(tmp_path, command)
    assert isinstance(result, PageRejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    assert result.detail == PageBudgetTooSmall(expected.minimum_budget)
    _assert_denied(tmp_path, "page_budget_too_small")
    fits = EvidenceWindow(
        _SCOPE, evidence, budget=expected.minimum_budget, query="deterministic"
    )
    assert isinstance(_call(tmp_path, fits), EvidenceWindowResult)


def test_unknown_evidence_is_not_found(tmp_path: Path) -> None:
    _seeded(tmp_path)
    attic = _ScriptedAttic()
    unknown = _call(tmp_path, EvidenceWindow(_SCOPE, uuid4(), query="x"), attic)
    assert isinstance(unknown, Rejected)
    assert unknown.failure.code is FailureCode.NOT_FOUND
    assert attic.fetches == []
    codes = [r[0] for r in _rows(tmp_path, "SELECT reason_code FROM audit_events")]
    assert "evidence_not_found" in codes


def test_locator_term_refusal_is_an_invalid_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission applies the locator's term rule; this guards the two drifting."""
    evidence = _seeded(tmp_path)

    def too_many(text: str, query: str) -> tuple[int, int] | None:
        raise TooManyTerms

    monkeypatch.setattr("cairn.authority.evidence_window.locate", too_many)
    result = _call(tmp_path, EvidenceWindow(_SCOPE, evidence, query="x"))
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.INVALID_REQUEST
    _assert_denied(tmp_path, "invalid_query")
