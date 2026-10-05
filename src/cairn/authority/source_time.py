"""Authorised source chronology for paged recall.

Mirrors CairnMemory._fact's provenance walk exactly, batched: an origin that
_fact would not attribute yields neither a time nor an evidence identity, and
missing and withheld times are indistinguishable.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from cairn.authority.custody import IngestedProvenance, PromotedProvenance
from cairn.authority.gate import Fetch
from cairn.authority.memory import _can_read, _visible
from cairn.authority.memory_page_types import SourceTimeStatus
from cairn.authority.memory_types import MAX_HISTORY_RECORDS
from cairn.authority.mutations import _stored_scope
from cairn.authority.retrieval import RetrievedFact, _load_candidates
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import parse_timestamp

_CHUNK = 500


@dataclass(frozen=True, slots=True)
class SourceProjection:
    observed_at: datetime | None
    evidence_id: UUID | None

    @property
    def status(self) -> SourceTimeStatus:
        return (
            SourceTimeStatus.AVAILABLE
            if self.observed_at is not None
            else SourceTimeStatus.UNAVAILABLE
        )


_UNAVAILABLE = SourceProjection(None, None)


def _chunks(values: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _rows(fetch: Fetch, prefix: str, ids: Iterable[str]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for chunk in _chunks(sorted(set(ids))):
        slots = ",".join("?" for _ in chunk)
        rows.extend(fetch(f"{prefix} IN ({slots})", tuple(chunk)))
    return rows


def _row_visible(
    where: tuple[str, str, str, str], scope: Scope, ceiling: int, now: datetime
) -> bool:
    realm, segments, classification, recorded = where
    return (
        _visible(
            _stored_scope(realm, segments),
            Classification(classification),
            scope,
            ceiling,
        )
        and parse_timestamp(recorded) <= now
    )


def project_sources(
    fetch: Fetch,
    facts: Mapping[UUID, RetrievedFact],
    scope: Scope,
    ceiling: int,
    now: datetime,
) -> dict[UUID, SourceProjection]:
    sources: dict[UUID, RetrievedFact] = dict(facts)
    seen: dict[UUID, set[UUID]] = {identity: set() for identity in facts}
    hidden: set[UUID] = set()
    while True:
        pending: dict[UUID, PromotedProvenance] = {}
        for identity, source in sources.items():
            if identity in hidden or not isinstance(
                source.provenance, PromotedProvenance
            ):
                continue
            if (
                source.fact_id in seen[identity]
                or len(seen[identity]) >= MAX_HISTORY_RECORDS
            ):
                hidden.add(identity)
                continue
            seen[identity].add(source.fact_id)
            pending[identity] = source.provenance
        if not pending:
            break
        loaded, _ = _load_candidates(
            fetch, tuple(dict.fromkeys(link.derived_from for link in pending.values()))
        )
        parents = {fact.fact_id: fact for fact in loaded}
        evidence = {
            cast(str, row[0]): cast(tuple[str, str, str, str, str], row)
            for row in _rows(
                fetch,
                "SELECT evidence_id, realm_id, scope_segments, classification, recorded_at "
                "FROM evidence_records WHERE evidence_id",
                (str(link.evidence_id) for link in pending.values()),
            )
        }
        for identity, link in pending.items():
            parent = parents.get(link.derived_from)
            proof = evidence.get(str(link.evidence_id))
            if (
                parent is None
                or not _can_read(parent, scope, ceiling, now)
                or proof is None
                or not _row_visible(proof[1:], scope, ceiling, now)
            ):
                hidden.add(identity)
            else:
                sources[identity] = parent
    origins = {
        identity: source.provenance.assertion_id
        for identity, source in sources.items()
        if identity not in hidden and isinstance(source.provenance, IngestedProvenance)
    }
    assertions = {
        cast(str, row[0]): cast(tuple[str, str, str, str, str, str | None], row)
        for row in _rows(
            fetch,
            "SELECT assertion_id, realm_id, scope_segments, classification, recorded_at, "
            "observed_at FROM assertions WHERE assertion_id",
            (str(a) for a in origins.values()),
        )
    }
    exact: dict[str, UUID] = {}
    for row in _rows(
        fetch,
        "SELECT assertion_id, evidence_id, realm_id, scope_segments, classification, "
        "recorded_at FROM evidence_records WHERE payload_length IS NOT NULL AND assertion_id",
        (str(a) for a in origins.values()),
    ):
        held = cast(tuple[str, str, str, str, str, str], row)
        assertion, evidence_id = held[0], held[1]
        if _row_visible(held[2:], scope, ceiling, now):
            candidate = UUID(evidence_id)
            if assertion not in exact or str(candidate) < str(exact[assertion]):
                exact[assertion] = candidate
    result: dict[UUID, SourceProjection] = {}
    for identity in facts:
        assertion_id = origins.get(identity)
        origin = None if assertion_id is None else assertions.get(str(assertion_id))
        if origin is None or not _row_visible(origin[1:5], scope, ceiling, now):
            result[identity] = _UNAVAILABLE
            continue
        observed = origin[5]
        result[identity] = SourceProjection(
            None if observed is None else parse_timestamp(observed),
            exact.get(str(assertion_id)),
        )
    return result
