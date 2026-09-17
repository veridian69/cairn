"""Strict validation for decoded memory/v1 success documents."""

from __future__ import annotations

import json
import math
import re
from typing import cast
from uuid import RFC_4122, UUID

from cairn.authority.credentials import CLEARANCE_ORDER
from cairn.authority.custody import SourceType
from cairn.catalogue.audit import (
    AuditValueError,
    Classification,
    Scope,
    ScopeSegment,
    TrustClass,
)
from cairn.catalogue.sqlite import CatalogueStorageError, parse_timestamp

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FACT_FIELDS = frozenset(
    {
        "assertion_id",
        "body",
        "classification",
        "derived_from",
        "disagreement_context_incomplete",
        "evidence_id",
        "fact_id",
        "has_disagreement",
        "invalidated_at",
        "promoted_by",
        "recorded_at",
        "relevance_score",
        "scope",
        "source_principal_id",
        "source_type",
        "trust",
        "valid_from",
        "valid_to",
    }
)
_DISAGREEMENT_FIELDS = frozenset(
    {
        "classification",
        "left_fact_id",
        "principal_id",
        "reason",
        "recorded_at",
        "relationship_id",
        "right_fact_id",
        "scope",
    }
)
_RESOLUTION_FIELDS = frozenset(
    {
        "classification",
        "disagreement_id",
        "evidence_id",
        "principal_id",
        "reason",
        "recorded_at",
        "relationship_id",
        "scope",
        "selected_fact_id",
    }
)
_RECALL_FIELDS = frozenset(
    {
        "budget_consumed",
        "budget_exhausted",
        "disagreements",
        "hits",
        "policy",
        "resolutions",
        "semantic_degraded",
    }
)


def _canonical_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("not_object")
    result = cast(dict[str, object], value)
    if frozenset(result) != fields:
        raise ValueError("object_fields")
    return result


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError("not_list")
    return cast(list[object], value)


def _text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ValueError("not_text")
    text = value
    text.encode("utf-8")
    return text


def _uuid(value: object, *, nullable: bool = False) -> str | None:
    text = _text(value, nullable=nullable)
    if text is None:
        return None
    try:
        parsed = UUID(text)
    except ValueError:
        raise ValueError("invalid_uuid") from None
    if parsed.version != 4 or parsed.variant != RFC_4122 or str(parsed) != text:
        raise ValueError("invalid_uuid")
    return text


def _timestamp(value: object, *, nullable: bool = False) -> str | None:
    text = _text(value, nullable=nullable)
    if text is None:
        return None
    if len(text) != 27 or not text.endswith("Z"):
        raise ValueError("invalid_timestamp")
    try:
        parse_timestamp(text)
    except CatalogueStorageError:
        raise ValueError("invalid_timestamp") from None
    return text


def _scope(value: object) -> None:
    document = _object(value, frozenset({"realm", "segments"}))
    realm = _text(document["realm"])
    segments: list[ScopeSegment] = []
    for raw in _list(document["segments"]):
        segment = _object(raw, frozenset({"identifier", "kind"}))
        kind = _text(segment["kind"])
        identifier = _text(segment["identifier"])
        assert kind is not None and identifier is not None
        segments.append(ScopeSegment(kind, identifier))
    assert realm is not None
    try:
        Scope(realm, tuple(segments))
    except AuditValueError:
        raise ValueError("invalid_scope") from None


def _member(value: object, members: type[Classification] | type[TrustClass]) -> str:
    text = _text(value)
    assert text is not None
    try:
        members(text)
    except ValueError:
        raise ValueError("invalid_vocabulary") from None
    return text


def _source_type(value: object) -> str | None:
    text = _text(value, nullable=True)
    if text is None:
        return None
    try:
        SourceType(text)
    except ValueError:
        raise ValueError("invalid_source_type") from None
    return text


def _fact(value: object) -> tuple[str, bool, bool]:
    fact = _object(value, _FACT_FIELDS)
    identity = _uuid(fact["fact_id"])
    assert identity is not None
    body = _text(fact["body"])
    assert body is not None
    _scope(fact["scope"])
    _member(fact["classification"], Classification)
    _member(fact["trust"], TrustClass)

    assertion = _uuid(fact["assertion_id"], nullable=True)
    promoted = tuple(
        _uuid(fact[field], nullable=True)
        for field in ("derived_from", "promoted_by", "evidence_id")
    )
    if assertion is not None and any(item is not None for item in promoted):
        raise ValueError("mixed_provenance")
    if (
        assertion is None
        and any(item is not None for item in promoted)
        and any(item is None for item in promoted)
    ):
        raise ValueError("partial_provenance")

    _timestamp(fact["valid_from"], nullable=True)
    _timestamp(fact["valid_to"], nullable=True)
    _timestamp(fact["recorded_at"])
    _timestamp(fact["invalidated_at"], nullable=True)
    principal = _uuid(fact["source_principal_id"], nullable=True)
    source_type = _source_type(fact["source_type"])
    if (principal is None) != (source_type is None):
        raise ValueError("partial_source")

    score = fact["relevance_score"]
    if type(score) not in {int, float}:
        raise ValueError("invalid_relevance_score")
    try:
        number = float(cast(int | float, score))
    except OverflowError:
        raise ValueError("invalid_relevance_score") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid_relevance_score")
    has_disagreement = fact["has_disagreement"]
    context_incomplete = fact["disagreement_context_incomplete"]
    if type(has_disagreement) is not bool or type(context_incomplete) is not bool:
        raise ValueError("invalid_disagreement_flags")
    if context_incomplete and not has_disagreement:
        raise ValueError("invalid_disagreement_flags")
    return identity, has_disagreement, context_incomplete


def _disagreement(value: object) -> tuple[str, str, str]:
    link = _object(value, _DISAGREEMENT_FIELDS)
    identity = _uuid(link["relationship_id"])
    left = _uuid(link["left_fact_id"])
    right = _uuid(link["right_fact_id"])
    _scope(link["scope"])
    _member(link["classification"], Classification)
    _uuid(link["principal_id"])
    _text(link["reason"])
    _timestamp(link["recorded_at"])
    assert identity is not None and left is not None and right is not None
    if left == right:
        raise ValueError("self_disagreement")
    return identity, left, right


def _resolution(value: object) -> tuple[str, str, str | None]:
    resolution = _object(value, _RESOLUTION_FIELDS)
    identity = _uuid(resolution["relationship_id"])
    disagreement = _uuid(resolution["disagreement_id"])
    selected = _uuid(resolution["selected_fact_id"], nullable=True)
    _uuid(resolution["evidence_id"])
    _scope(resolution["scope"])
    _member(resolution["classification"], Classification)
    _uuid(resolution["principal_id"])
    _text(resolution["reason"])
    _timestamp(resolution["recorded_at"])
    assert identity is not None and disagreement is not None
    return identity, disagreement, selected


def validate_recall(value: object, *, budget: int) -> dict[str, object]:
    """Validate the complete current recall packet without discarding fields."""
    document = _object(value, _RECALL_FIELDS)
    consumed = document["budget_consumed"]
    if type(consumed) is not int or not 0 <= consumed <= budget:
        raise ValueError("invalid_budget_consumed")
    if type(document["budget_exhausted"]) is not bool:
        raise ValueError("invalid_budget_exhausted")
    policy = _text(document["policy"])
    if policy == "":
        raise ValueError("invalid_policy")
    if type(document["semantic_degraded"]) is not bool:
        raise ValueError("invalid_semantic_degraded")

    facts = [_fact(item) for item in _list(document["hits"])]
    fact_ids = {identity for identity, _, _ in facts}
    if len(fact_ids) != len(facts):
        raise ValueError("duplicate_fact")

    disagreement_values = _list(document["disagreements"])
    disagreements = [_disagreement(item) for item in disagreement_values]
    disagreement_ids = {identity for identity, _, _ in disagreements}
    if len(disagreement_ids) != len(disagreements):
        raise ValueError("duplicate_disagreement")
    displayed_by_fact: set[str] = set()
    for _, left, right in disagreements:
        if left not in fact_ids or right not in fact_ids:
            raise ValueError("undisclosed_disagreement_endpoint")
        displayed_by_fact.update((left, right))

    resolution_values = _list(document["resolutions"])
    resolutions = [_resolution(item) for item in resolution_values]
    resolution_ids = {identity for identity, _, _ in resolutions}
    if len(resolution_ids) != len(resolutions):
        raise ValueError("duplicate_resolution")
    if disagreement_ids & resolution_ids:
        raise ValueError("duplicate_relationship")
    disagreement_endpoints = {
        identity: frozenset((left, right)) for identity, left, right in disagreements
    }
    for _, disagreement, selected in resolutions:
        if disagreement not in disagreement_ids:
            raise ValueError("undisclosed_resolution_disagreement")
        if (
            selected is not None
            and selected not in disagreement_endpoints[disagreement]
        ):
            raise ValueError("undisclosed_selected_fact")

    for identity, has_disagreement, context_incomplete in facts:
        displayed = identity in displayed_by_fact
        if displayed and not has_disagreement:
            raise ValueError("missing_disagreement_flag")
        if has_disagreement and not displayed and not context_incomplete:
            raise ValueError("unexplained_disagreement_flag")
    disclosed_size = sum(_canonical_size(item) for item in _list(document["hits"]))
    disclosed_size += sum(_canonical_size(item) for item in disagreement_values)
    disclosed_size += sum(_canonical_size(item) for item in resolution_values)
    if consumed != disclosed_size:
        raise ValueError("invalid_budget_consumed")
    return document


def validate_history(value: object, *, budget: int) -> dict[str, object]:
    """Validate all disclosed history, including reasons and their endpoints.

    An invalidated fact need not have a correction record: current authority
    can withhold its replacement and reason, and the byte budget can omit it.
    A disclosed correction, however, must name only disclosed endpoints.
    """
    document = _object(
        value,
        frozenset(
            {
                "facts",
                "corrections",
                "disagreements",
                "resolutions",
                "budget_consumed",
                "budget_exhausted",
            }
        ),
    )
    consumed = document["budget_consumed"]
    if type(consumed) is not int or not 0 <= consumed <= budget:
        raise ValueError("invalid_budget_consumed")
    corrections = _list(document["corrections"])
    correction_fields = frozenset(
        {"fact_id", "superseded_by", "principal_id", "reason", "invalidated_at"}
    )
    checked = [_object(item, correction_fields) for item in corrections]
    correction_size = sum(_canonical_size(item) for item in checked)
    # Recall's primitives also enforce duplicate identities, relationship
    # endpoints, provenance, flags and canonical disclosed-record accounting.
    validate_recall(
        {
            "hits": document["facts"],
            "disagreements": document["disagreements"],
            "resolutions": document["resolutions"],
            "budget_consumed": consumed - correction_size,
            "budget_exhausted": document["budget_exhausted"],
            "policy": "history",
            "semantic_degraded": False,
        },
        budget=budget,
    )
    facts = {
        item["fact_id"]: item
        for raw in _list(document["facts"])
        for item in [_object(raw, _FACT_FIELDS)]
    }
    disagreements = {
        item["relationship_id"]: item
        for raw in _list(document["disagreements"])
        for item in [_object(raw, _DISAGREEMENT_FIELDS)]
    }
    for link in disagreements.values():
        for endpoint in ("left_fact_id", "right_fact_id"):
            fact = facts[link[endpoint]]
            if link["scope"] != fact["scope"]:
                raise ValueError("inconsistent_disagreement_scope")
            if (
                CLEARANCE_ORDER[Classification(cast(str, link["classification"]))]
                < (CLEARANCE_ORDER[Classification(cast(str, fact["classification"]))])
            ):
                raise ValueError("inconsistent_disagreement_classification")
    for raw in _list(document["resolutions"]):
        resolution = _object(raw, _RESOLUTION_FIELDS)
        link = disagreements[resolution["disagreement_id"]]
        if resolution["scope"] != link["scope"]:
            raise ValueError("inconsistent_resolution_scope")
        if (
            CLEARANCE_ORDER[Classification(cast(str, resolution["classification"]))]
            < (CLEARANCE_ORDER[Classification(cast(str, link["classification"]))])
        ):
            raise ValueError("inconsistent_resolution_classification")
    corrected: set[str] = set()
    for correction in checked:
        identity = _uuid(correction["fact_id"])
        replacement = _uuid(correction["superseded_by"], nullable=True)
        _uuid(correction["principal_id"])
        _text(correction["reason"])
        invalidated_at = _timestamp(correction["invalidated_at"])
        assert identity is not None
        if identity in corrected:
            raise ValueError("duplicate_correction")
        corrected.add(identity)
        if identity not in facts or (
            replacement is not None and replacement not in facts
        ):
            raise ValueError("undisclosed_correction_endpoint")
        if facts[identity]["invalidated_at"] != invalidated_at:
            raise ValueError("inconsistent_correction_timestamp")
    return document


def validate_remember_success(
    value: object, *, fact_count: int, realm: str, evidence_supplied: bool = False
) -> dict[str, object]:
    """Validate a remember receipt strongly enough to claim persistence."""
    document = _object(
        value,
        frozenset({"audit_receipt", "mutation_receipt", "outcome", "result"}),
    )
    if document["outcome"] not in {"committed", "replayed"}:
        raise ValueError("invalid_outcome")

    result = _object(
        document["result"], frozenset({"assertion_id", "evidence_id", "fact_ids"})
    )
    _uuid(result["assertion_id"])
    facts = [_uuid(item) for item in _list(result["fact_ids"])]
    if len(facts) != fact_count or len(set(facts)) != len(facts):
        raise ValueError("invalid_fact_receipts")
    if evidence_supplied:
        _uuid(result["evidence_id"])
    elif result["evidence_id"] is not None:
        raise ValueError("unexpected_evidence")

    return _validate_receipts(document, realm)


def validate_correction_success(
    value: object,
    *,
    fact_ids: tuple[UUID, ...],
    realm: str,
) -> dict[str, object]:
    document = _object(
        value, frozenset({"audit_receipt", "mutation_receipt", "outcome", "result"})
    )
    if document["outcome"] not in {"committed", "replayed"}:
        raise ValueError("invalid_outcome")
    result = _object(document["result"], frozenset({"fact_ids", "invalidated_at"}))
    identities = [_uuid(item) for item in _list(result["fact_ids"])]
    if len(identities) != len(fact_ids) or set(identities) != {
        str(identity) for identity in fact_ids
    }:
        raise ValueError("invalid_fact_receipts")
    _timestamp(result["invalidated_at"])
    return _validate_receipts(document, realm)


def _validate_receipts(document: dict[str, object], realm: str) -> dict[str, object]:

    mutation = _object(
        document["mutation_receipt"],
        frozenset({"command_digest", "mutation_id"}),
    )
    _uuid(mutation["mutation_id"])
    command_digest = _text(mutation["command_digest"])
    if command_digest is None or _DIGEST.fullmatch(command_digest) is None:
        raise ValueError("invalid_command_digest")

    audit = _object(
        document["audit_receipt"],
        frozenset(
            {
                "chain_identity",
                "chain_kind",
                "event_hash",
                "event_id",
                "recorded_at",
                "sequence",
            }
        ),
    )
    _uuid(audit["event_id"])
    if audit["chain_kind"] != "realm" or audit["chain_identity"] != realm:
        raise ValueError("invalid_audit_chain")
    sequence = audit["sequence"]
    if type(sequence) is not int or sequence < 1:
        raise ValueError("invalid_audit_sequence")
    _timestamp(audit["recorded_at"])
    event_hash = _text(audit["event_hash"])
    if event_hash is None or _DIGEST.fullmatch(event_hash) is None:
        raise ValueError("invalid_event_hash")
    return document
