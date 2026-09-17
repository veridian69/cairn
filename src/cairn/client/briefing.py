"""Bounded arrival context, kept separate from host or model instructions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import RFC_4122, UUID

from cairn.authority.retrieval import MAX_BUDGET_BYTES
from cairn.catalogue.sqlite import parse_timestamp
from cairn.client.errors import FailureMetadata, MemoryOperationFailure
from cairn.client.memory import MemoryClient
from cairn.client.types import FrozenJSONObject, RecalledMemory, _validate_timestamp

_MAX_SUMMARY_FACTS = 4
_MAX_EXCERPT_CHARACTERS = 160
_MAX_HISTORY_CALLS = 4
_MAX_EXPLICIT_ANCHORS = 64


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A record in ``recall`` or a zero-based ``history:N`` packet."""

    packet: str
    collection: Literal["hits", "facts", "corrections", "disagreements", "resolutions"]
    index: int


@dataclass(frozen=True, slots=True)
class CurrentFactSummary:
    """A literal source excerpt, never a derived task status or instruction."""

    fact_id: UUID
    excerpt: str
    source_principal_id: UUID | None
    source_type: str | None
    trust: str
    has_disagreement: bool
    disagreement_context_incomplete: bool
    reference: EvidenceReference


@dataclass(frozen=True, slots=True)
class ArrivalFailure:
    operation: Literal["recall", "history"]
    fact_id: UUID | None
    failure: FailureMetadata


@dataclass(frozen=True, slots=True)
class ArrivalBriefing:
    """Selected-memory coverage only, not an exhaustive change feed.

    ``changes`` references records strictly newer than ``since`` by default.
    ``include_boundary`` also includes equal timestamps for conservative durable
    visits. Facts use recorded_at (not validity); corrections use invalidated_at;
    disagreements and resolutions use recorded_at. When a correction record is
    absent, a fact's newly visible invalidated_at also yields a fact reference,
    without inventing a reason. The same fact may occur in
    several packets: each disclosure counts towards the total byte budget.

    Summary excerpts remain untrusted source text. Complete bodies, provenance,
    scopes, classifications and evidence identities live in the immutable
    packets. Missing history or a missing reason never proves no history exists.
    """

    since: datetime | None
    summary: tuple[CurrentFactSummary, ...]
    recall: RecalledMemory | None
    history: tuple[RecalledMemory, ...]
    history_fact_ids: tuple[UUID, ...]
    changes: tuple[EvidenceReference, ...]
    warnings: tuple[str, ...]
    failures: tuple[ArrivalFailure, ...]
    omitted_history_fact_ids: tuple[UUID, ...]
    omitted_summary_count: int
    budget: int
    budget_consumed: int
    budget_exhausted: bool
    source: Literal["cairn-memory/v1"] = "cairn-memory/v1"
    content_role: Literal["untrusted-data"] = "untrusted-data"
    coverage: Literal["selected-memory-only"] = "selected-memory-only"
    recall_calls: int = 1
    read_call_limit: Literal[6] = 6
    include_boundary: bool = False

    @property
    def previous_visit_known(self) -> bool:
        return self.since is not None


def _records(packet: RecalledMemory, field: str) -> tuple[FrozenJSONObject, ...]:
    # MemoryClient validates the complete wire packet before freezing it.
    return cast(tuple[FrozenJSONObject, ...], packet.data[field])


def _summary(fact: FrozenJSONObject, index: int) -> CurrentFactSummary:
    text = cast(str, fact["body"])
    # Bound processing as well as output; do not scan a full large body to
    # produce a short excerpt. Flatten whitespace without interpreting prose.
    excerpt = " ".join(text[:_MAX_EXCERPT_CHARACTERS].split())
    if len(text) > _MAX_EXCERPT_CHARACTERS:
        excerpt = excerpt[: _MAX_EXCERPT_CHARACTERS - 1] + "…"
    principal = cast(str | None, fact["source_principal_id"])
    return CurrentFactSummary(
        UUID(cast(str, fact["fact_id"])),
        excerpt,
        UUID(principal) if principal is not None else None,
        cast(str | None, fact["source_type"]),
        cast(str, fact["trust"]),
        cast(bool, fact["has_disagreement"]),
        cast(bool, fact["disagreement_context_incomplete"]),
        EvidenceReference("recall", "hits", index),
    )


def _changes(
    packet: RecalledMemory,
    name: str,
    since: datetime | None,
    include_boundary: bool = False,
) -> tuple[EvidenceReference, ...]:
    if since is None:
        return ()

    def changed(value: str) -> bool:
        timestamp = parse_timestamp(value)
        return timestamp >= since if include_boundary else timestamp > since

    fields: tuple[
        Literal["hits", "facts", "corrections", "disagreements", "resolutions"], ...
    ]
    fields = (
        ("hits", "disagreements", "resolutions")
        if name == "recall"
        else ("facts", "corrections", "disagreements", "resolutions")
    )
    corrected = (
        {record["fact_id"] for record in _records(packet, "corrections")}
        if name != "recall"
        else set()
    )
    return tuple(
        EvidenceReference(name, field, index)
        for field in fields
        for index, record in enumerate(_records(packet, field))
        if changed(
            cast(
                str,
                record["invalidated_at" if field == "corrections" else "recorded_at"],
            )
        )
        or (
            field in {"hits", "facts"}
            and record["fact_id"] not in corrected
            and record["invalidated_at"] is not None
            and changed(cast(str, record["invalidated_at"]))
        )
    )


async def build_arrival_briefing(
    client: MemoryClient,
    query: str,
    *,
    since: datetime | None = None,
    history_fact_ids: tuple[UUID, ...] = (),
    budget: int = 16384,
    include_boundary: bool = False,
) -> ArrivalBriefing:
    """Read current context and at most four histories under one record budget.

    Recall may spend half the budget (at least one byte). Histories divide the
    actual remainder equally among outstanding anchors, carrying unused bytes
    forward. Explicit unique anchors take precedence over the selected current
    hits. At most 64 explicit anchors are accepted, before scanning or deduping.
    An empty, exhausted recall with no explicit anchors may retry once using the
    full unused budget: at most two recalls plus four histories (six reads).
    No returned record bytes are discarded to fund this retry. The host supplies
    and retains the previous-visit timestamp. Reads are not one atomic snapshot;
    disclosed invalidation removes a stale summary but preserves raw evidence.
    """
    _validate_timestamp(since, "since")
    if type(include_boundary) is not bool:
        raise ValueError("invalid_boundary_mode")
    if type(budget) is not int or not 1 <= budget <= MAX_BUDGET_BYTES:
        raise ValueError("invalid_budget")
    if type(history_fact_ids) is not tuple:
        raise ValueError("history_fact_ids_must_be_uuid4_tuple")
    if len(history_fact_ids) > _MAX_EXPLICIT_ANCHORS:
        raise ValueError("too_many_history_fact_ids")
    if any(
        type(identity) is not UUID
        or identity.version != 4
        or identity.variant != RFC_4122
        for identity in history_fact_ids
    ):
        raise ValueError("history_fact_ids_must_be_uuid4_tuple")

    warnings = ["selected_memory_only_not_exhaustive"]
    if since is None:
        warnings.append("previous_visit_unknown")
    failures: list[ArrivalFailure] = []
    recalled: RecalledMemory | None = None
    summaries: tuple[CurrentFactSummary, ...] = ()
    changes: list[EvidenceReference] = []
    histories: list[RecalledMemory] = []
    expanded: list[UUID] = []
    consumed = 0
    exhausted = False
    omitted_summary = 0
    recall_calls = 1
    try:
        recalled = await client.recall(
            query, budget=max(1, budget // 2), relevant_only=True
        )
    except MemoryOperationFailure as error:
        failures.append(ArrivalFailure("recall", None, error.failure))
    if (
        recalled is not None
        and not history_fact_ids
        and not _records(recalled, "hits")
        and recalled.data["budget_consumed"] == 0
        and recalled.data["budget_exhausted"]
    ):
        warnings.extend(("recall_budget_exhausted", "recall_full_budget_retry"))
        if recalled.data["semantic_degraded"]:
            warnings.append("semantic_degraded")
        recall_calls += 1
        try:
            recalled = await client.recall(query, budget=budget, relevant_only=True)
        except MemoryOperationFailure as error:
            failures.append(ArrivalFailure("recall", None, error.failure))
    if recalled is not None:
        consumed = cast(int, recalled.data["budget_consumed"])
        exhausted = cast(bool, recalled.data["budget_exhausted"])
        if exhausted:
            warnings.append("recall_budget_exhausted")
        if recalled.data["semantic_degraded"]:
            warnings.append("semantic_degraded")
        hits = _records(recalled, "hits")
        summaries = tuple(
            _summary(fact, index)
            for index, fact in enumerate(hits[:_MAX_SUMMARY_FACTS])
        )
        omitted_summary = max(0, len(hits) - len(summaries))
        if omitted_summary:
            warnings.append("summary_omitted")
        changes.extend(_changes(recalled, "recall", since, include_boundary))
    anchors = (
        tuple(dict.fromkeys(history_fact_ids))
        if history_fact_ids
        else tuple(item.fact_id for item in summaries)
    )
    selected = anchors[:_MAX_HISTORY_CALLS]
    omitted = list(anchors[_MAX_HISTORY_CALLS:])
    for index, identity in enumerate(selected):
        remaining = budget - consumed
        if remaining == 0:
            omitted.extend(selected[index:])
            exhausted = True
            break
        allocation = max(1, remaining // (len(selected) - index))
        try:
            packet = await client.history(identity, budget=allocation)
        except MemoryOperationFailure as error:
            failures.append(ArrivalFailure("history", identity, error.failure))
            omitted.append(identity)
            continue
        consumed += cast(int, packet.data["budget_consumed"])
        if packet.data["budget_exhausted"]:
            exhausted = True
            warnings.append("history_budget_exhausted")
        changes.extend(
            _changes(packet, f"history:{len(histories)}", since, include_boundary)
        )
        histories.append(packet)
        expanded.append(identity)
        corrected = {fact["fact_id"] for fact in _records(packet, "corrections")}
        if any(
            fact["invalidated_at"] is not None and fact["fact_id"] not in corrected
            for fact in _records(packet, "facts")
        ):
            warnings.append("correction_context_unavailable")
    if omitted:
        warnings.append("history_omitted")
    invalidated: set[UUID] = set()
    for packet in (() if recalled is None else (recalled,)) + tuple(histories):
        field = "hits" if packet is recalled else "facts"
        invalidated.update(
            UUID(cast(str, fact["fact_id"]))
            for fact in _records(packet, field)
            if fact["invalidated_at"] is not None
        )
        if any(
            fact["disagreement_context_incomplete"] for fact in _records(packet, field)
        ):
            warnings.append("disagreement_context_incomplete")
    current_summaries = tuple(
        item for item in summaries if item.fact_id not in invalidated
    )
    if len(current_summaries) != len(summaries):
        warnings.extend(("stale_recall_context", "summary_omitted"))
        omitted_summary += len(summaries) - len(current_summaries)
    return ArrivalBriefing(
        since=since,
        summary=current_summaries,
        recall=recalled,
        history=tuple(histories),
        history_fact_ids=tuple(expanded),
        changes=tuple(changes),
        warnings=tuple(dict.fromkeys(warnings)),
        failures=tuple(failures),
        omitted_history_fact_ids=tuple(omitted),
        omitted_summary_count=omitted_summary,
        budget=budget,
        budget_consumed=consumed,
        budget_exhausted=exhausted,
        recall_calls=recall_calls,
        include_boundary=include_boundary,
    )
