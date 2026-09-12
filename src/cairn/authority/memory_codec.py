"""Canonical disclosed memory records shared by authority budgeting and transports."""

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import cast
from uuid import UUID

from cairn.authority.custody import IngestedProvenance, PromotedProvenance
from cairn.authority.memory_types import MemoryFact
from cairn.catalogue.sqlite import canonical_timestamp


def memory_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, MemoryFact):
        fact = cast(dict[str, object], memory_value(value.fact))
        del fact["provenance"]
        provenance = value.fact.provenance
        ingested = provenance if isinstance(provenance, IngestedProvenance) else None
        promoted = provenance if isinstance(provenance, PromotedProvenance) else None
        return {
            **fact,
            "assertion_id": None if ingested is None else str(ingested.assertion_id),
            "derived_from": None if promoted is None else str(promoted.derived_from),
            "promoted_by": None if promoted is None else str(promoted.promoted_by),
            "evidence_id": None if promoted is None else str(promoted.evidence_id),
            "source_principal_id": memory_value(value.source_principal_id),
            "source_type": memory_value(value.source_type),
            "relevance_score": value.relevance_score,
            "has_disagreement": value.has_disagreement,
            "disagreement_context_incomplete": value.disagreement_context_incomplete,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: memory_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [memory_value(item) for item in value]
    return value
