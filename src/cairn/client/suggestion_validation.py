"""Validate closed suggestion evidence using the accepted fact/history contract."""

from typing import cast
from uuid import UUID

from cairn.authority.memory_types import (
    GRADED_RELEVANT_POLICY,
    RELEVANT_POLICY,
    SEMANTIC_UNAVAILABLE,
)
from cairn.catalogue.audit import Scope
from cairn.client.suggestion_types import SuggestedMemory
from cairn.client.types import freeze_object
from cairn.client.validation import (
    _canonical_size,
    _fact,
    _list,
    _object,
    _text,
    _uuid,
    validate_history,
)

_RESULT_FIELDS = frozenset(
    {"items", "budget_consumed", "budget_exhausted", "semantic_degraded", "policy"}
)
_ITEM_FIELDS = frozenset(
    {"kind", "facts", "reason", "match_basis", "corrections", "disagreements"}
)
_BASES = {
    "exact_duplicate": "exact_body",
    "possible_duplicate": "retrieval_candidate",
    "possible_correction": "recorded_correction",
    "related_disagreement": "recorded_disagreement",
}
_REASONS = {
    "exact_duplicate": {
        "The current fact body is byte-identical; attribution and validity still matter."
    },
    "possible_duplicate": {
        "Retrieval found a related candidate; equivalence is unverified.",
        "Selected evidence is historical or not currently valid; candidate equivalence is unverified.",
    },
    "possible_correction": {
        "Recorded correction history; inspect both attributed claims before acting."
    },
    "related_disagreement": {
        "An attributed disagreement is recorded; no claim is automatically preferred."
    },
}


def validate_input(
    observation: str | None, fact_ids: tuple[UUID, ...], budget: int, limit: int
) -> None:
    if (
        type(budget) is not int
        or not 1 <= budget <= 65536
        or type(limit) is not int
        or not 1 <= limit <= 16
    ):
        raise ValueError("invalid_bounds")
    if type(fact_ids) is not tuple or len(fact_ids) > 8:
        raise ValueError("invalid_identities")
    for identity in fact_ids:
        if type(identity) is not UUID:
            raise ValueError("invalid_identity")
        _uuid(str(identity))
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("duplicate_identity")
    if observation is None:
        if not fact_ids:
            raise ValueError("missing_input")
    elif (
        type(observation) is not str
        or fact_ids
        or not 1 <= len(observation.encode("utf-8")) <= 4096
    ):
        raise ValueError("invalid_observation")


def _evidence(item: dict[str, object], scope: Scope) -> list[dict[str, object]]:
    from cairn.client.memory import _validate_history_scopes

    facts = cast(list[dict[str, object]], _list(item["facts"]))
    for fact in facts:
        _fact(fact)
    # Authority selects items from larger recall/history packets. The flags
    # describe that source packet, not this item's relationship subset. Check
    # original fact flags first, then adapt only a detached validation packet's
    # omission flags to reuse history's full record/endpoint validators. Returned
    # evidence retains every original field unchanged.
    adapted = [
        dict(fact, disagreement_context_incomplete=bool(fact["has_disagreement"]))
        for fact in facts
    ]
    history = {
        "facts": adapted,
        "corrections": _list(item["corrections"]),
        "disagreements": _list(item["disagreements"]),
        "resolutions": [],
        "budget_exhausted": False,
    }
    history["budget_consumed"] = sum(
        _canonical_size(record)
        for key in ("facts", "corrections", "disagreements")
        for record in cast(list[object], history[key])
    )
    validate_history(history, budget=65536)
    _validate_history_scopes(history, scope)
    return facts


def validate_suggestions(
    value: object,
    *,
    scope: Scope,
    observation: str | None,
    fact_ids: tuple[UUID, ...],
    budget: int,
    limit: int,
) -> SuggestedMemory:
    document = _object(value, _RESULT_FIELDS)
    items = [_object(raw, _ITEM_FIELDS) for raw in _list(document["items"])]
    consumed = document["budget_consumed"]
    if type(consumed) is not int or not 0 <= consumed <= budget or len(items) > limit:
        raise ValueError("invalid_budget")
    if (
        type(document["budget_exhausted"]) is not bool
        or type(document["semantic_degraded"]) is not bool
    ):
        raise ValueError("invalid_flags")
    policy = _text(document["policy"], nullable=True)
    if policy is not None and policy not in (
        RELEVANT_POLICY,
        GRADED_RELEVANT_POLICY,
        RELEVANT_POLICY + SEMANTIC_UNAVAILABLE,
    ):
        raise ValueError("invalid_source_policy")
    # Housekeeping accumulates degradation across selected roots, while policy
    # describes the last recall. A healthy last policy cannot clear that flag.
    if (
        policy == RELEVANT_POLICY + SEMANTIC_UNAVAILABLE
        and not document["semantic_degraded"]
    ):
        raise ValueError("missing_degradation")
    if observation is not None and policy is None:
        raise ValueError("missing_recall_policy")
    if policy is None and document["semantic_degraded"]:
        raise ValueError("degradation_without_recall")
    roots = {str(identity) for identity in fact_ids}
    seen: set[tuple[object, ...]] = set()
    for item in items:
        kind = _text(item["kind"])
        basis = _text(item["match_basis"])
        reason = _text(item["reason"])
        if kind not in _BASES or basis != _BASES[kind] or reason not in _REASONS[kind]:
            raise ValueError("invalid_kind_basis_reason")
        if observation is not None and reason == (
            "Selected evidence is historical or not currently valid; candidate equivalence is unverified."
        ):
            raise ValueError("historical_reason_without_selected_root")
        facts = _evidence(item, scope)
        identities = tuple(cast(str, fact["fact_id"]) for fact in facts)
        corrections = cast(list[dict[str, object]], item["corrections"])
        disagreements = cast(list[dict[str, object]], item["disagreements"])
        if kind in {"exact_duplicate", "possible_duplicate"}:
            if (
                corrections
                or disagreements
                or len(facts) != (1 if observation is not None else 2)
                or policy is None
            ):
                raise ValueError("invalid_comparison")
            if observation is None and identities[0] not in roots:
                raise ValueError("wrong_comparison_root")
            if (
                observation is None
                and facts[0]["invalidated_at"] is not None
                and reason
                != (
                    "Selected evidence is historical or not currently valid; candidate equivalence is unverified."
                )
            ):
                raise ValueError("missing_historical_root_reason")
            if facts[-1]["invalidated_at"] is not None:
                raise ValueError("historical_candidate")
            if kind == "exact_duplicate" and (
                any(fact["invalidated_at"] is not None for fact in facts)
                or facts[-1]["body"]
                != (observation if observation is not None else facts[0]["body"])
            ):
                raise ValueError("false_exact_match")
        elif kind == "possible_correction":
            if (
                observation is not None
                or disagreements
                or len(corrections) != 1
                or len(facts) != 2
            ):
                raise ValueError("invalid_correction")
            correction = corrections[0]
            if identities != (correction["fact_id"], correction["superseded_by"]):
                raise ValueError("wrong_correction_endpoints")
        else:
            if corrections or len(disagreements) != 1 or len(facts) != 2:
                raise ValueError("invalid_disagreement")
            link = disagreements[0]
            if identities != (link["left_fact_id"], link["right_fact_id"]):
                raise ValueError("wrong_disagreement_endpoints")
        identity = (
            kind,
            identities,
            tuple(c["fact_id"] for c in corrections),
            tuple(d["relationship_id"] for d in disagreements),
        )
        if identity in seen:
            raise ValueError("duplicate_suggestion")
        seen.add(identity)
    if consumed != sum(_canonical_size(item) for item in items):
        raise ValueError("invalid_budget_consumed")
    return SuggestedMemory(
        tuple(freeze_object(item) for item in items),
        consumed,
        document["budget_exhausted"],
        document["semantic_degraded"],
        policy,
    )
