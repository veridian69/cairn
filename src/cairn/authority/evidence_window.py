"""Bounded, byte-exact windows inside one verified immutable evidence payload.

Not dialogue parsing: no JSON interpretation, no speaker identity, no joins
across evidence records. Budgets count the canonical record bytes (the record
without ``budget_consumed``). Window edges are Unicode scalar indices into the
decoded text, so no window ever splits a UTF-8 sequence.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from cairn.authority.evidence_read import _read_verified
from cairn.authority.gate import INVALID_REQUEST_MESSAGE, Actor, realm_draft
from cairn.authority.literal_locator import MAX_TERMS, TooManyTerms, locate
from cairn.authority.memory import _json
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_page_types import PageBudgetTooSmall, PageRejected
from cairn.authority.retrieval import MAX_BUDGET_BYTES
from cairn.catalogue.audit import ActionKind, Outcome, Scope
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
    RetryClass,
    StableFailure,
)
from cairn.evidence.adapter import AtticAdapter
from cairn.operations.metrics import Metrics
from cairn.runtime.logging import SafeLogger
from cairn.screening import SecretScreen, first_finding

ACTION = "memory-evidence-window"
DEFAULT_BUDGET = 16384
MAX_QUERY_BYTES = 8192
MAX_START_BYTE = 1_048_576
_BEFORE_LINES = 2
_FROM_ANCHOR_LINES = 6


class WindowMode(StrEnum):
    QUERY = "query"
    OFFSET = "offset"


class InvalidOffset(Exception):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    scope: Scope
    evidence_id: UUID
    budget: int = DEFAULT_BUDGET
    query: str | None = None
    start: int | None = None


@dataclass(frozen=True, slots=True)
class EvidenceWindowResult:
    evidence_id: UUID
    mode: WindowMode
    text: str | None
    start_byte: int | None
    end_byte: int | None
    sha256: str
    byte_length: int
    match_found: bool | None
    match_start_byte: int | None
    match_end_byte: int | None
    prefix_omitted: bool | None
    suffix_omitted: bool | None
    next_start_byte: int | None
    budget_consumed: int = 0


def window_cost(result: EvidenceWindowResult) -> int:
    document = memory_value(result)
    assert isinstance(document, dict)
    del document["budget_consumed"]
    return len(_json(document))


def _bytes(text: str, index: int) -> int:
    return len(text[:index].encode())


def _finish(result: EvidenceWindowResult) -> EvidenceWindowResult:
    return replace(result, budget_consumed=window_cost(result))


def _within(
    result: EvidenceWindowResult, budget: int
) -> EvidenceWindowResult | PageBudgetTooSmall:
    if result.budget_consumed <= budget:
        return result
    return PageBudgetTooSmall(result.budget_consumed)


def _largest(low: int, high: int, fits: Callable[[int], bool]) -> int:
    """Largest value in [low, high] satisfying a monotone-decreasing predicate.

    ``low`` must fit. The search is safe only because cost is non-decreasing
    over the searched range. Extending a window's start backwards adds at least
    one text byte per scalar and saves at most one ``start_byte`` digit, and
    ``prefix_omitted`` false is longer than true. Extending its end forwards
    never shrinks the ``end_byte`` digits while a suffix remains. The one break
    is the end reaching the payload end: ``next_start_byte`` becomes null and
    ``suffix_omitted`` false, which can make that window cheaper by one byte at
    seven-digit offsets. So both builders test the end-of-payload extent before
    searching, and include it when reporting a minimum budget. Over the range
    left to search, the end-of-payload extent then never fits.
    """
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def query_window(
    text: str,
    *,
    evidence_id: UUID,
    sha256: str,
    byte_length: int,
    query: str,
    budget: int,
) -> EvidenceWindowResult | PageBudgetTooSmall:
    span = locate(text, query)
    if span is None:
        return _within(
            _finish(
                EvidenceWindowResult(
                    evidence_id,
                    WindowMode.QUERY,
                    None,
                    None,
                    None,
                    sha256,
                    byte_length,
                    False,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ),
            budget,
        )
    match_start, match_end = span
    anchor = text.rfind("\n", 0, match_start) + 1
    start = anchor
    for _ in range(_BEFORE_LINES):
        if start == 0:
            break
        start = text.rfind("\n", 0, start - 1) + 1
    end = anchor
    for _ in range(_FROM_ANCHOR_LINES):
        newline = text.find("\n", end)
        if newline == -1:
            end = len(text)
            break
        end = newline + 1
    # A literal match spanning more lines stays indivisible.
    end = max(end, match_end)
    match_start_byte = _bytes(text, match_start)
    match_end_byte = _bytes(text, match_end)

    def build(lo: int, hi: int) -> EvidenceWindowResult:
        start_byte, end_byte = _bytes(text, lo), _bytes(text, hi)
        suffix = hi < len(text)
        return _finish(
            EvidenceWindowResult(
                evidence_id,
                WindowMode.QUERY,
                text[lo:hi],
                start_byte,
                end_byte,
                sha256,
                byte_length,
                True,
                match_start_byte,
                match_end_byte,
                lo > 0,
                suffix,
                end_byte if suffix else None,
            )
        )

    def fits(lo: int, hi: int) -> bool:
        return build(lo, hi).budget_consumed <= budget

    if fits(start, end):
        return build(start, end)
    if fits(match_start, end):
        # Trim preceding context first: keep the most scalars before the match.
        kept = _largest(0, match_start - start, lambda k: fits(match_start - k, end))
        return build(match_start - kept, end)
    minimal = build(match_start, match_end)
    if minimal.budget_consumed > budget:
        # A window reaching the payload end may undercut the match-only one.
        cheapest = minimal.budget_consumed
        if end == len(text):
            cheapest = min(cheapest, build(match_start, end).budget_consumed)
        return PageBudgetTooSmall(cheapest)
    return build(
        match_start, _largest(match_end, end, lambda hi: fits(match_start, hi))
    )


def offset_window(
    text: str,
    payload: bytes,
    *,
    evidence_id: UUID,
    sha256: str,
    start: int,
    budget: int,
) -> EvidenceWindowResult | PageBudgetTooSmall:
    if type(start) is not int or not 0 <= start <= len(payload):
        raise InvalidOffset
    try:
        lo = len(payload[:start].decode("utf-8"))
    except UnicodeDecodeError:
        raise InvalidOffset from None

    def build(hi: int) -> EvidenceWindowResult:
        end_byte = start + len(text[lo:hi].encode())
        suffix = hi < len(text)
        return _finish(
            EvidenceWindowResult(
                evidence_id,
                WindowMode.OFFSET,
                text[lo:hi],
                start,
                end_byte,
                sha256,
                len(payload),
                None,
                None,
                None,
                start > 0,
                suffix,
                end_byte if suffix else None,
            )
        )

    def fits(hi: int) -> bool:
        return build(hi).budget_consumed <= budget

    if lo == len(text):
        return _within(build(lo), budget)
    whole = build(len(text))
    if whole.budget_consumed <= budget:
        return whole
    smallest = build(lo + 1)
    if smallest.budget_consumed > budget:
        # The window reaching the payload end may undercut the one-scalar one.
        return PageBudgetTooSmall(min(smallest.budget_consumed, whole.budget_consumed))
    return build(_largest(lo + 1, len(text), fits))


def evidence_window(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    command: EvidenceWindow,
    *,
    correlation_id: UUID,
    clock: Callable[[], datetime],
    enabled: bool,
    attic: AtticAdapter | None,
    screen: SecretScreen,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> EvidenceWindowResult | Rejected | PageRejected:
    """Authorise, fetch and verify the whole payload, then cut one window.

    Request refusals (budget, offset range, query shape, secret screen) are
    decided inside the shared read after authorisation and before the
    ``enabled`` check, the catalogue row and any Attic I/O. Locating and
    window building run after the read connection has closed and outside
    any lock; only the final audit append takes the writer gate.
    """

    def admit() -> tuple[str, FailureCode] | None:
        query, start, budget = command.query, command.start, command.budget
        if type(budget) is not int or not 1 <= budget <= MAX_BUDGET_BYTES:
            return "invalid_budget", FailureCode.INVALID_REQUEST
        if query is not None and start is not None:
            return "invalid_query", FailureCode.INVALID_REQUEST
        if start is not None and (
            type(start) is not int or not 0 <= start <= MAX_START_BYTE
        ):
            return "invalid_offset", FailureCode.INVALID_REQUEST
        if query is not None:
            if (
                type(query) is not str
                or not query.strip()
                or len(query.encode()) > MAX_QUERY_BYTES
                # The locator's own term rule, decided before any Attic I/O.
                or len(set(query.strip().casefold().split())) > MAX_TERMS
            ):
                return "invalid_query", FailureCode.INVALID_REQUEST
            if first_finding(screen, (("query", query),)) is not None:
                return "memory_secret_rejected", FailureCode.SECRET_REJECTED
        return None

    verified = _read_verified(
        data_path,
        transactions,
        actor,
        command.scope,
        command.evidence_id,
        action_code=ACTION,
        correlation_id=correlation_id,
        clock=clock,
        enabled=enabled,
        attic=attic,
        metrics=metrics,
        logger=logger,
        admit=admit,
    )
    if isinstance(verified, Rejected):
        return verified

    def refuse(reason: str) -> Rejected:
        return transactions.reject(
            realm_draft(
                realm_id=command.scope.realm,
                actor=actor,
                grant_id=verified.grant_id,
                action_kind=ActionKind.DATA,
                action_code=ACTION,
                requested_scope=command.scope,
                outcome=Outcome.DENY,
                reason_code=reason,
                correlation_id=correlation_id,
            ),
            StableFailure(
                code=FailureCode.INVALID_REQUEST,
                safe_message=INVALID_REQUEST_MESSAGE,
                correlation_id=correlation_id,
                retry=RetryClass.NEVER,
            ),
        )

    try:
        if command.query is not None:
            result = query_window(
                verified.text,
                evidence_id=command.evidence_id,
                sha256=verified.digest_hex,
                byte_length=verified.length,
                query=command.query,
                budget=command.budget,
            )
        else:
            # Strict UTF-8 decoding round-trips, so this is the verified payload.
            payload = verified.text.encode()
            result = offset_window(
                verified.text,
                payload,
                evidence_id=command.evidence_id,
                sha256=verified.digest_hex,
                start=command.start or 0,
                budget=command.budget,
            )
    except InvalidOffset:
        return refuse("invalid_offset")
    except TooManyTerms:
        return refuse("invalid_query")
    if isinstance(result, PageBudgetTooSmall):
        return PageRejected(refuse("page_budget_too_small").failure, result)
    transactions.append_audit(
        realm_draft(
            realm_id=command.scope.realm,
            actor=actor,
            grant_id=verified.grant_id,
            action_kind=ActionKind.DATA,
            action_code=ACTION,
            requested_scope=command.scope,
            outcome=Outcome.ALLOW,
            reason_code="evidence_window_completed",
            correlation_id=correlation_id,
            affected_evidence_ids=(command.evidence_id,),
        )
    )
    return result
