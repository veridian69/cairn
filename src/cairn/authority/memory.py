"""Attributed shared memory with current-authority history and immutable opinions.

Legacy ranking: distinct Unicode word overlap, a 0.5 semantic membership bonus,
and a recency bonus in (0, 0.25]. Optional graded evidence instead uses integer
lexical/semantic units, then recency and UUID ties (lexical-graded/v2). A graded
request pins a catalogue read view before grants and body checks; it does not
promise current-at-response authority or a cross-store transaction. There is no age cutoff,
usage reinforcement or mutation on recall. Catalogue candidates remain available
when an optional semantic index is absent, behind, or returns opaque UUID order.
Opt-in relevant_only recall excludes recency-only matches before budget admission;
lexical cues and semantic membership remain eligible regardless of age.
Budgets count the sum of canonical JSON bytes of disclosed records (not envelope
punctuation). Facts are admitted before bounded relationship context; exact
per-fact flags disclose visible disagreements and omissions without vetoing facts.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, fields, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.authority import semantic_ranking
from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation
from cairn.authority.custody import (
    CustodyValueError,
    FactProvenance,
    IngestedProvenance,
    PromotedProvenance,
    SourceType,
    validate_reason,
)
from cairn.authority.gate import (
    Actor,
    Fetch,
    fetch_from,
    instance_denial_draft,
    instance_id,
    realm_draft,
    realm_exists,
)
from cairn.authority.grants import GrantRecord, find_authorising_grant, is_scope_prefix
from cairn.authority.housekeeping_types import Suggest, SuggestionResult
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_types import (
    GRADED_POLICY,
    GRADED_RELEVANT_POLICY,
    MAX_HISTORY_RECORDS,
    POLICY,
    RELEVANT_POLICY,
    SEMANTIC_UNAVAILABLE,
    Disagree,
    History,
    MemoryCorrection,
    MemoryDisagreement,
    MemoryFact,
    MemoryFactRecord,
    MemoryHistory,
    MemoryResolution,
    Recall,
    RecallResult,
    RelationshipRecorded,
    Resolve,
)
from cairn.authority.mutations import (
    _scope_shape_failure,
    _sorted_grants,
    _stored_scope,
)
from cairn.authority.retrieval import (
    MAX_BUDGET_BYTES,
    MAX_QUERY_BYTES,
    RetrievedFact,
    _admitted,
    _ancestry_partitions,
    _covering_retrieve_grants,
    _load_candidates,
)
from cairn.catalogue.audit import (
    ActionKind,
    AuditValueError,
    Classification,
    Outcome,
    Scope,
    TrustClass,
)
from cairn.catalogue.sqlite import canonical_timestamp, parse_timestamp, read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    MutationOutcome,
    MutationReceipt,
    MutationRejection,
    Rejected,
    RetryClass,
    StableFailure,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.projection.adapter import IndexAdapter
from cairn.projection.partition import canonical_segments_json
from cairn.projection.semantic_evidence import SemanticEvidence, SemanticEvidenceSource
from cairn.screening import SecretScreen, first_finding

__all__ = [
    "CairnMemory",
    "Disagree",
    "History",
    "MemoryCorrection",
    "MemoryDisagreement",
    "MemoryFact",
    "MemoryFactRecord",
    "MemoryHistory",
    "MemoryResolution",
    "Recall",
    "RecallResult",
    "RelationshipRecorded",
    "Resolve",
]
_ALL_TRUST = frozenset(TrustClass)


def _default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(type(value).__name__)


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        default=_default,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _size(
    value: MemoryFact | MemoryCorrection | MemoryDisagreement | MemoryResolution,
) -> int:
    return len(_json(memory_value(value)))


def _score(fact: RetrievedFact, now: datetime, query: str, semantic: bool) -> float:
    age_days = max(0.0, (now - fact.recorded_at).total_seconds() / 86400)
    words = set(re.findall(r"\w+", fact.body.casefold()))
    cues = set(re.findall(r"\w+", query.casefold()))
    return len(words & cues) + (0.5 if semantic else 0.0) + 0.25 / (1 + age_days / 30)


def _visible(
    scope: Scope, classification: Classification, request: Scope, ceiling: int
) -> bool:
    return (
        scope.realm == request.realm
        and is_scope_prefix(scope.segments, request.segments)
        and CLEARANCE_ORDER[classification] <= ceiling
    )


def _can_read(fact: RetrievedFact, scope: Scope, ceiling: int, now: datetime) -> bool:
    return fact.recorded_at <= now and _visible(
        fact.scope, fact.classification, scope, ceiling
    )


def _evidence(
    fetch: Fetch, identity: UUID, scope: Scope, ceiling: int, now: datetime
) -> Classification | None:
    rows = fetch(
        "SELECT realm_id, scope_segments, classification, recorded_at FROM evidence_records WHERE evidence_id = ?",
        (str(identity),),
    )
    if not rows:
        return None
    realm, segments, classification, recorded = cast(tuple[str, str, str, str], rows[0])
    level = Classification(classification)
    return (
        level
        if _visible(_stored_scope(realm, segments), level, scope, ceiling)
        and parse_timestamp(recorded) <= now
        else None
    )


def _history_pairs(
    fetch: Fetch, identity: UUID, scope: Scope, ceiling: int, now: datetime
) -> tuple[tuple[object, ...], ...]:
    """Apply disclosure before the traversal limit, so hidden fan-in cannot
    change the public exhaustion signal or displace an authorised neighbour."""
    ancestry = tuple(
        canonical_segments_json(scope.segments[:n])
        for n in range(len(scope.segments) + 1)
    )
    levels = tuple(
        level.value for level in Classification if CLEARANCE_ORDER[level] <= ceiling
    )
    scope_slots = ",".join("?" for _ in ancestry)
    level_slots = ",".join("?" for _ in levels)
    parameters: tuple[object, ...] = (
        scope.realm,
        *ancestry,
        *levels,
        canonical_timestamp(now),
    )

    def visible(alias: str) -> str:
        return f"{alias}.realm_id = ? AND {alias}.scope_segments IN ({scope_slots}) AND {alias}.classification IN ({level_slots}) AND {alias}.recorded_at <= ?"

    return fetch(
        f"SELECT i.fact_id, i.superseded_by FROM fact_invalidations i JOIN facts a ON a.fact_id = i.fact_id LEFT JOIN facts b ON b.fact_id = i.superseded_by "
        f"WHERE (i.fact_id = ? OR i.superseded_by = ?) AND i.invalidated_at <= ? AND {visible('a')} AND (i.superseded_by IS NULL OR ({visible('b')})) "
        "UNION "
        f"SELECT d.left_fact_id, d.right_fact_id FROM memory_disagreements d JOIN facts a ON a.fact_id = d.left_fact_id JOIN facts b ON b.fact_id = d.right_fact_id "
        f"WHERE (d.left_fact_id = ? OR d.right_fact_id = ?) AND {visible('d')} AND {visible('a')} AND {visible('b')} "
        "ORDER BY 1, 2 LIMIT ?",
        (
            str(identity),
            str(identity),
            canonical_timestamp(now),
            *parameters,
            *parameters,
            str(identity),
            str(identity),
            *parameters,
            *parameters,
            *parameters,
            MAX_HISTORY_RECORDS + 1,
        ),
    )


def _flag_context(
    fetch: Fetch,
    facts: list[MemoryFact],
    scope: Scope,
    ceiling: int,
    now: datetime,
    links: list[MemoryDisagreement],
    resolutions: list[MemoryResolution],
) -> list[MemoryFact]:
    """Exact visibility-filtered existential checks, returning no relationship
    bodies. Discovery is bounded separately; duplicates cannot enlarge Python
    materialisation here. SQLite may still scan matching index entries."""
    ancestry = tuple(
        canonical_segments_json(scope.segments[:n])
        for n in range(len(scope.segments) + 1)
    )
    levels = tuple(
        level.value for level in Classification if CLEARANCE_ORDER[level] <= ceiling
    )
    scope_slots = ",".join("?" for _ in ancestry)
    level_slots = ",".join("?" for _ in levels)
    visibility_parameters: tuple[object, ...] = (
        scope.realm,
        *ancestry,
        *levels,
        canonical_timestamp(now),
    )

    def visible(alias: str) -> str:
        return f"{alias}.realm_id = ? AND {alias}.scope_segments IN ({scope_slots}) AND {alias}.classification IN ({level_slots}) AND {alias}.recorded_at <= ?"

    link_ids = tuple(str(link.relationship_id) for link in links)
    resolution_ids = tuple(
        str(resolution.relationship_id) for resolution in resolutions
    )
    missing_link = (
        "1"
        if not link_ids
        else "d.relationship_id NOT IN (" + ",".join("?" for _ in link_ids) + ")"
    )
    missing_resolution = (
        "1"
        if not resolution_ids
        else "r.relationship_id NOT IN (" + ",".join("?" for _ in resolution_ids) + ")"
    )
    base = (
        "SELECT 1 FROM memory_disagreements d JOIN facts a ON a.fact_id = d.left_fact_id JOIN facts b ON b.fact_id = d.right_fact_id "
        f"WHERE (d.left_fact_id = ? OR d.right_fact_id = ?) AND {visible('d')} AND {visible('a')} AND {visible('b')}"
    )
    omitted = (
        f" AND ({missing_link} OR EXISTS ("
        "SELECT 1 FROM memory_resolutions r JOIN evidence_records e ON e.evidence_id = r.evidence_id "
        f"WHERE r.disagreement_id = d.relationship_id AND {missing_resolution} "
        f"AND r.classification IN ({level_slots}) AND r.recorded_at <= ? AND {visible('e')} LIMIT 1))"
    )
    result: list[MemoryFact] = []
    for fact in facts:
        identity = str(fact.fact.fact_id)
        parameters = (
            identity,
            identity,
            *visibility_parameters,
            *visibility_parameters,
            *visibility_parameters,
        )
        has_disagreement = bool(fetch(base + " LIMIT 1", parameters))
        incomplete = has_disagreement and bool(
            fetch(
                base + omitted + " LIMIT 1",
                (
                    *parameters,
                    *link_ids,
                    *resolution_ids,
                    *levels,
                    canonical_timestamp(now),
                    *visibility_parameters,
                ),
            )
        )
        result.append(
            replace(
                fact,
                has_disagreement=has_disagreement,
                disagreement_context_incomplete=incomplete,
            )
        )
    return result


class CairnMemory:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        screen: SecretScreen,
        index: IndexAdapter | None = None,
        *,
        semantic_evidence: SemanticEvidenceSource | None = None,
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._screen = screen
        self._index = index
        self._semantic_evidence = semantic_evidence

    def _refuse(
        self,
        fetch: Fetch,
        actor: Actor,
        correlation_id: UUID,
        action: str,
        code: FailureCode = FailureCode.AUTHORISATION_DENIED,
        reason: str = "memory_authorisation_denied",
        *,
        scope: Scope | None = None,
    ) -> MutationRejection:
        # Supply scope only after _access establishes live retrieve standing
        # in this snapshot. No request bodies or hidden object IDs enter denials.
        messages = {
            FailureCode.AUTHORISATION_DENIED: "The requested operation is not authorised.",
            FailureCode.INVALID_REQUEST: "The request is invalid.",
            FailureCode.SECRET_REJECTED: "The request contains prohibited secret material.",
        }
        return MutationRejection(
            StableFailure(code, messages[code], correlation_id, RetryClass.NEVER),
            realm_draft(
                realm_id=scope.realm,
                actor=actor,
                grant_id=None,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=scope,
                outcome=Outcome.DENY,
                reason_code=reason,
                correlation_id=correlation_id,
            )
            if scope is not None
            else instance_denial_draft(
                instance_id(fetch),
                actor,
                action,
                reason,
                correlation_id,
                action_kind=ActionKind.DATA,
            ),
        )

    def _access(
        self,
        fetch: Fetch,
        actor: Actor,
        scope: Scope,
        now: datetime,
        correlation_id: UUID,
        action: str,
    ) -> tuple[tuple[GrantRecord, ...], int]:
        if _scope_shape_failure(scope) is not None:
            raise self._refuse(
                fetch,
                actor,
                correlation_id,
                action,
                FailureCode.INVALID_REQUEST,
                "invalid_scope",
            )
        if not realm_exists(fetch, scope.realm):
            raise self._refuse(fetch, actor, correlation_id, action)
        try:
            grants = _sorted_grants(fetch, actor.principal_id, scope.realm)
        except (AuditValueError, CustodyValueError):
            raise self._refuse(fetch, actor, correlation_id, action) from None
        covering = _covering_retrieve_grants(grants, scope, now)
        if not covering:
            raise self._refuse(fetch, actor, correlation_id, action)
        return grants, max(CLEARANCE_ORDER[g.read_clearance] for g in covering)

    def _reject(self, error: MutationRejection) -> Rejected:
        return self._transactions.reject(error.denial_draft, error.failure)

    def _budget(
        self,
        fetch: Fetch,
        actor: Actor,
        budget: int,
        correlation_id: UUID,
        action: str,
        *,
        scope: Scope,
    ) -> None:
        if type(budget) is not int or not 1 <= budget <= MAX_BUDGET_BYTES:
            raise self._refuse(
                fetch,
                actor,
                correlation_id,
                action,
                FailureCode.INVALID_REQUEST,
                "invalid_budget",
                scope=scope,
            )

    def _screen_text(
        self,
        fetch: Fetch,
        actor: Actor,
        text: str,
        correlation_id: UUID,
        action: str,
        *,
        scope: Scope,
    ) -> None:
        if (
            first_finding(
                self._screen,
                (("query" if action == "memory-recall" else "reason", text),),
            )
            is not None
        ):
            raise self._refuse(
                fetch,
                actor,
                correlation_id,
                action,
                FailureCode.SECRET_REJECTED,
                "memory_secret_rejected",
                scope=scope,
            )

    def _fact(
        self,
        fetch: Fetch,
        fact: RetrievedFact,
        scope: Scope,
        ceiling: int,
        now: datetime,
        query: str = "",
        semantic: bool = False,
        *,
        relevance_score: float | None = None,
    ) -> MemoryFact:
        principal = None
        source_type = None
        provenance: FactProvenance | None = fact.provenance
        source = fact
        seen: set[UUID] = set()
        while isinstance(source.provenance, PromotedProvenance):
            link = source.provenance
            if source.fact_id in seen or len(seen) >= MAX_HISTORY_RECORDS:
                break
            seen.add(source.fact_id)
            loaded, _ = _load_candidates(fetch, (link.derived_from,))
            if (
                not loaded
                or not _can_read(loaded[0], scope, ceiling, now)
                or _evidence(fetch, link.evidence_id, scope, ceiling, now) is None
            ):
                provenance = None
                break
            source = loaded[0]
        if isinstance(source.provenance, IngestedProvenance):
            rows = fetch(
                "SELECT principal_id, source_type, realm_id, scope_segments, classification, recorded_at FROM assertions WHERE assertion_id = ?",
                (str(source.provenance.assertion_id),),
            )
            if rows:
                owner, kind, realm, segments, classification, recorded = cast(
                    tuple[str, str, str, str, str, str], rows[0]
                )
                if (
                    _visible(
                        _stored_scope(realm, segments),
                        Classification(classification),
                        scope,
                        ceiling,
                    )
                    and parse_timestamp(recorded) <= now
                ):
                    principal, source_type = UUID(owner), SourceType(kind)
                else:
                    provenance = None
        score = (
            _score(fact, now, query, semantic)
            if relevance_score is None
            else relevance_score
        )
        values = {field.name: getattr(fact, field.name) for field in fields(fact)}
        values["provenance"] = provenance
        if fact.invalidated_at is not None and fact.invalidated_at > now:
            values["invalidated_at"] = None
        return MemoryFact(MemoryFactRecord(**values), principal, source_type, score)

    def _links(
        self,
        fetch: Fetch,
        facts: dict[UUID, RetrievedFact],
        scope: Scope,
        ceiling: int,
        now: datetime,
        limit: int = MAX_HISTORY_RECORDS,
    ) -> tuple[list[MemoryDisagreement], list[MemoryResolution], bool]:
        disagreements: list[MemoryDisagreement] = []
        resolutions: list[MemoryResolution] = []
        if not facts:
            return disagreements, resolutions, False
        ids = tuple(str(identity) for identity in facts)
        placeholders = ",".join("?" for _ in ids)
        identity_clause = f" AND left_fact_id IN ({placeholders}) AND right_fact_id IN ({placeholders})"
        ancestry = tuple(
            canonical_segments_json(scope.segments[:n])
            for n in range(len(scope.segments) + 1)
        )
        ancestry_slots = ",".join("?" for _ in ancestry)
        levels = tuple(
            level.value for level in Classification if CLEARANCE_ORDER[level] <= ceiling
        )
        level_slots = ",".join("?" for _ in levels)
        bound = " LIMIT ?"
        bounds = (limit + 1,)
        for row in fetch(
            f"SELECT relationship_id, realm_id, scope_segments, left_fact_id, right_fact_id, classification, principal_id, reason, recorded_at FROM memory_disagreements WHERE realm_id = ? {identity_clause} AND classification IN ({level_slots}) AND recorded_at <= ? ORDER BY relationship_id"
            + bound,
            (scope.realm, *ids, *ids, *levels, canonical_timestamp(now), *bounds),
        ):
            (
                identity,
                realm,
                segments,
                left,
                right,
                classification,
                owner,
                reason,
                recorded,
            ) = cast(tuple[str, str, str, str, str, str, str, str, str], row)
            link_scope = _stored_scope(realm, segments)
            if (
                UUID(left) not in facts
                or UUID(right) not in facts
                or not _visible(
                    link_scope, Classification(classification), scope, ceiling
                )
                or parse_timestamp(recorded) > now
            ):
                continue
            disagreement = MemoryDisagreement(
                UUID(identity),
                link_scope,
                UUID(left),
                UUID(right),
                Classification(classification),
                UUID(owner),
                reason,
                parse_timestamp(recorded),
            )
            if len(disagreements) + len(resolutions) >= limit:
                return disagreements, resolutions, True
            disagreements.append(disagreement)
            for resolution in fetch(
                f"SELECT r.relationship_id, r.evidence_id, r.selected_fact_id, r.classification, r.principal_id, r.reason, r.recorded_at FROM memory_resolutions r JOIN evidence_records e ON e.evidence_id = r.evidence_id WHERE r.disagreement_id = ? AND r.classification IN ({level_slots}) AND r.recorded_at <= ? AND e.realm_id = ? AND e.scope_segments IN ({ancestry_slots}) AND e.classification IN ({level_slots}) AND e.recorded_at <= ? ORDER BY r.relationship_id"
                + bound,
                (
                    identity,
                    *levels,
                    canonical_timestamp(now),
                    scope.realm,
                    *ancestry,
                    *levels,
                    canonical_timestamp(now),
                    *bounds,
                ),
            ):
                rid, evidence, selected, level, principal, why, at = cast(
                    tuple[str, str, str | None, str, str, str, str], resolution
                )
                if (
                    CLEARANCE_ORDER[Classification(level)] > ceiling
                    or parse_timestamp(at) > now
                    or _evidence(fetch, UUID(evidence), scope, ceiling, now) is None
                ):
                    continue
                if len(disagreements) + len(resolutions) >= limit:
                    return disagreements, resolutions, True
                resolutions.append(
                    MemoryResolution(
                        UUID(rid),
                        link_scope,
                        UUID(identity),
                        UUID(evidence),
                        None if selected is None else UUID(selected),
                        Classification(level),
                        UUID(principal),
                        why,
                        parse_timestamp(at),
                    )
                )
        return disagreements, resolutions, False

    def recall(
        self,
        actor: Actor,
        command: Recall,
        *,
        correlation_id: UUID,
        _candidate_limit: int | None = None,
        _include_relationships: bool = True,
    ) -> RecallResult | Rejected:
        # Private housekeeping bounds: public defaults retain the original path.
        if _candidate_limit is not None and (
            type(_candidate_limit) is not int or not 1 <= _candidate_limit <= 16
        ):
            raise ValueError("invalid internal candidate limit")
        if type(_include_relationships) is not bool:
            raise ValueError("invalid internal relationship option")
        now = self._clock()
        action = "memory-recall"
        try:
            with read_connection(self._data_path) as connection:
                if self._semantic_evidence is not None:
                    # One coherent catalogue view from grants through disclosure.
                    # Connection closure ends this read transaction before auditing.
                    connection.execute("BEGIN")
                fetch = fetch_from(connection)
                grants, ceiling = self._access(
                    fetch, actor, command.scope, now, correlation_id, action
                )
                self._budget(
                    fetch,
                    actor,
                    command.budget,
                    correlation_id,
                    action,
                    scope=command.scope,
                )
                if (
                    type(command.query) is not str
                    or not command.query
                    or len(command.query.encode()) > MAX_QUERY_BYTES
                    or type(command.trust_filters) is not frozenset
                    or any(type(t) is not TrustClass for t in command.trust_filters)
                    or type(command.relevant_only) is not bool
                ):
                    raise self._refuse(
                        fetch,
                        actor,
                        correlation_id,
                        action,
                        FailureCode.INVALID_REQUEST,
                        "invalid_query",
                        scope=command.scope,
                    )
                self._screen_text(
                    fetch,
                    actor,
                    command.query,
                    correlation_id,
                    action,
                    scope=command.scope,
                )
                # Query every ancestral partition, never a recency-limited prefix.
                # Only identities are materialised here; the shared loader chunks SQL.
                segments = tuple(
                    canonical_segments_json(command.scope.segments[:n])
                    for n in range(len(command.scope.segments) + 1)
                )
                placeholders = ",".join("?" for _ in segments)
                ids = tuple(
                    UUID(cast(str, row[0]))
                    for row in fetch(
                        f"SELECT fact_id FROM facts WHERE realm_id = ? AND scope_segments IN ({placeholders})",
                        (command.scope.realm, *segments),
                    )
                )
                semantic_ids: set[UUID] = set()
                semantic_degraded = False
                advice: SemanticEvidence | None = None
                catalogue_ids = ids
                if self._semantic_evidence is not None:
                    try:
                        partitions = _ancestry_partitions(command.scope)
                        advice = semantic_ranking.validate(
                            self._semantic_evidence.search_with_evidence(
                                command.query, 256, partitions
                            ),
                            command.query,
                            partitions,
                        )
                        semantic_ids.update(advice.candidate_ids)
                    except Exception:
                        semantic_degraded = True
                elif self._index is not None:
                    try:
                        semantic_ids.update(
                            self._index.search(
                                command.query, 256, _ancestry_partitions(command.scope)
                            )
                        )
                    except Exception:
                        # The external accelerator cannot make catalogue memory unavailable.
                        semantic_degraded = True
                grade_ids = (
                    ()
                    if advice is None
                    else tuple(g.fact_id for p in advice.partitions for g in p.grades)
                )
                ids = tuple(dict.fromkeys((*ids, *semantic_ids, *grade_ids)))
                loaded, _ = _load_candidates(fetch, ids)
                admitted = {
                    f.fact_id: f
                    for f in loaded
                    if _admitted(
                        f,
                        command.scope,
                        ceiling,
                        _ALL_TRUST,
                        now,
                    )
                }
                allowed = {
                    identity
                    for identity, f in admitted.items()
                    if not command.trust_filters or f.trust in command.trust_filters
                }
                candidate_exhausted = False
                grades: dict[UUID, float] = {}
                if advice is not None:
                    try:
                        grades = semantic_ranking.reconcile(
                            advice,
                            {identity: admitted[identity] for identity in allowed},
                        )
                    except Exception:
                        advice = None
                        semantic_ids.clear()
                        semantic_degraded = True
                        allowed.intersection_update(catalogue_ids)
                graded = advice is not None
                ranks = (
                    {
                        identity: semantic_ranking.rank(
                            admitted[identity],
                            command.query,
                            grades.get(identity),
                            identity in semantic_ids,
                        )
                        for identity in allowed
                    }
                    if graded
                    else {}
                )
                if graded:
                    ranked_ids = sorted(
                        (
                            identity
                            for identity in allowed
                            if not command.relevant_only or ranks[identity].relevant
                        ),
                        key=lambda identity: ranks[identity].key,
                    )
                    if _candidate_limit is not None:
                        candidate_exhausted = len(ranked_ids) > _candidate_limit
                        ranked_ids = ranked_ids[:_candidate_limit]
                    allowed = set(ranked_ids)
                elif _candidate_limit is not None:
                    eligible = sorted(
                        (
                            (
                                _score(f, now, command.query, identity in semantic_ids),
                                identity,
                            )
                            for identity, f in admitted.items()
                            if identity in allowed
                        ),
                        key=lambda item: (-item[0], str(item[1])),
                    )
                    if command.relevant_only:
                        eligible = [item for item in eligible if item[0] > 0.25]
                    candidate_exhausted = len(eligible) > _candidate_limit
                    allowed = {identity for _, identity in eligible[:_candidate_limit]}
                ranked = sorted(
                    (
                        self._fact(
                            fetch,
                            f,
                            command.scope,
                            ceiling,
                            now,
                            command.query,
                            f.fact_id in semantic_ids,
                            relevance_score=ranks[identity].units / 1_000_000
                            if graded
                            else None,
                        )
                        for identity, f in admitted.items()
                        if identity in allowed
                    ),
                    key=lambda f: (
                        ranks[f.fact.fact_id].key
                        if graded
                        else (-f.relevance_score, str(f.fact.fact_id))
                    ),
                )
                hits: list[MemoryFact] = []
                consumed = 0
                exhausted = candidate_exhausted
                # Both flags start false, the longer JSON boolean representation.
                # Admission reserves their maximum cost; final bytes are recomputed.
                for hit in ranked:
                    # Recency contributes at most 0.25; a lexical cue contributes
                    # at least 1 and semantic membership 0.5, even for old facts.
                    if (
                        command.relevant_only
                        and not graded
                        and hit.relevance_score <= 0.25
                    ):
                        continue
                    if consumed + _size(hit) > command.budget:
                        exhausted = True
                        continue
                    hits.append(hit)
                    consumed += _size(hit)
                selected = {
                    hit.fact.fact_id: admitted[hit.fact.fact_id] for hit in hits
                }
                if _include_relationships:
                    links, resolutions, context_limited = self._links(
                        fetch,
                        selected,
                        command.scope,
                        ceiling,
                        now,
                        limit=MAX_HISTORY_RECORDS,
                    )
                else:
                    links, resolutions, context_limited = [], [], False
                exhausted |= context_limited
                disclosed_links: list[MemoryDisagreement] = []
                disclosed_resolutions: list[MemoryResolution] = []
                for link in links:
                    if consumed + _size(link) <= command.budget:
                        disclosed_links.append(link)
                        consumed += _size(link)
                    else:
                        exhausted = True
                disclosed_link_ids = {link.relationship_id for link in disclosed_links}
                for resolution in resolutions:
                    if resolution.disagreement_id not in disclosed_link_ids:
                        continue
                    if consumed + _size(resolution) <= command.budget:
                        disclosed_resolutions.append(resolution)
                        consumed += _size(resolution)
                    else:
                        exhausted = True
                # Bounded existential flags remain truthful even when the private
                # caller opts out of relationship-body expansion.
                hits = _flag_context(
                    fetch,
                    hits,
                    command.scope,
                    ceiling,
                    now,
                    disclosed_links,
                    disclosed_resolutions,
                )
                consumed = (
                    sum(_size(hit) for hit in hits)
                    + sum(_size(link) for link in disclosed_links)
                    + sum(_size(resolution) for resolution in disclosed_resolutions)
                )
                policy = RELEVANT_POLICY if command.relevant_only else POLICY
                if graded:
                    policy = (
                        GRADED_RELEVANT_POLICY
                        if command.relevant_only
                        else GRADED_POLICY
                    )
                elif self._semantic_evidence is not None and semantic_degraded:
                    policy += SEMANTIC_UNAVAILABLE
                result = RecallResult(
                    tuple(hits),
                    consumed,
                    exhausted,
                    policy,
                    tuple(disclosed_links),
                    tuple(disclosed_resolutions),
                    semantic_degraded,
                )
                authorising = _covering_retrieve_grants(grants, command.scope, now)[0]
            self._audit_read(
                actor,
                command.scope,
                authorising.grant_id,
                correlation_id,
                action,
                tuple(h.fact.fact_id for h in result.hits),
            )
            return result
        except MutationRejection as error:
            return self._reject(error)

    def _audit_read(
        self,
        actor: Actor,
        scope: Scope,
        grant_id: UUID,
        correlation_id: UUID,
        action: str,
        fact_ids: tuple[UUID, ...],
    ) -> None:
        self._transactions.append_audit(
            realm_draft(
                realm_id=scope.realm,
                actor=actor,
                grant_id=grant_id,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=scope,
                outcome=Outcome.ALLOW,
                reason_code="memory_read_completed",
                correlation_id=correlation_id,
                affected_fact_ids=tuple(sorted(fact_ids, key=str)),
            )
        )

    def history(
        self,
        actor: Actor,
        command: History,
        *,
        correlation_id: UUID,
        _record_limit: int = MAX_HISTORY_RECORDS,
    ) -> MemoryHistory | Rejected:
        if (
            type(_record_limit) is not int
            or not 1 <= _record_limit <= MAX_HISTORY_RECORDS
        ):
            raise ValueError("invalid internal history limit")
        now = self._clock()
        action = "memory-history"
        try:
            with read_connection(self._data_path) as connection:
                fetch = fetch_from(connection)
                grants, ceiling = self._access(
                    fetch, actor, command.scope, now, correlation_id, action
                )
                self._budget(
                    fetch,
                    actor,
                    command.budget,
                    correlation_id,
                    action,
                    scope=command.scope,
                )
                loaded, _ = _load_candidates(fetch, (command.fact_id,))
                if not loaded or not _can_read(loaded[0], command.scope, ceiling, now):
                    raise self._refuse(
                        fetch, actor, correlation_id, action, scope=command.scope
                    )
                pending = [command.fact_id]
                seen: set[UUID] = set()
                visible: dict[UUID, RetrievedFact] = {}
                corrections: list[MemoryCorrection] = []
                exhausted = False
                while pending and len(seen) < _record_limit:
                    identity = pending.pop(0)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    loaded, _ = _load_candidates(fetch, (identity,))
                    if not loaded or not _can_read(
                        loaded[0], command.scope, ceiling, now
                    ):
                        continue
                    visible[identity] = loaded[0]
                    rows = _history_pairs(fetch, identity, command.scope, ceiling, now)
                    for row in rows[:_record_limit]:
                        for value in row:
                            if value is not None:
                                neighbour = UUID(cast(str, value))
                                if neighbour not in seen and neighbour not in pending:
                                    pending.append(neighbour)
                    if len(rows) > _record_limit:
                        exhausted = True
                exhausted |= bool(pending)
                for identity in visible:
                    rows = fetch(
                        "SELECT superseded_by, principal_id, reason, invalidated_at FROM fact_invalidations WHERE fact_id = ?",
                        (str(identity),),
                    )
                    if not rows:
                        continue
                    replacement, owner, reason, at = cast(
                        tuple[str | None, str, str, str], rows[0]
                    )
                    if parse_timestamp(at) <= now and (
                        replacement is None or UUID(replacement) in visible
                    ):
                        corrections.append(
                            MemoryCorrection(
                                identity,
                                None if replacement is None else UUID(replacement),
                                UUID(owner),
                                reason,
                                parse_timestamp(at),
                            )
                        )
                remaining = _record_limit - len(visible)
                if len(corrections) > remaining:
                    exhausted = True
                    corrections = corrections[:remaining]
                links, resolutions, links_exhausted = self._links(
                    fetch,
                    visible,
                    command.scope,
                    ceiling,
                    now,
                    limit=remaining - len(corrections),
                )
                exhausted |= links_exhausted
                facts = [
                    self._fact(fetch, fact, command.scope, ceiling, now)
                    for fact in visible.values()
                ]
                # Root first, then stable traversal. Edges are disclosed only if all
                # named endpoints survived the byte budget.
                chosen: list[MemoryFact] = []
                consumed = 0
                for fact in facts:
                    cost = _size(fact)
                    if consumed + cost > command.budget:
                        exhausted = True
                        continue
                    chosen.append(fact)
                    consumed += cost
                chosen_ids = {f.fact.fact_id for f in chosen}
                kept_corrections: list[MemoryCorrection] = []
                kept_links: list[MemoryDisagreement] = []
                kept_resolutions: list[MemoryResolution] = []
                for correction in corrections:
                    if correction.fact_id in chosen_ids and (
                        correction.superseded_by is None
                        or correction.superseded_by in chosen_ids
                    ):
                        if consumed + _size(correction) <= command.budget:
                            kept_corrections.append(correction)
                            consumed += _size(correction)
                        else:
                            exhausted = True
                for link in links:
                    if (
                        link.left_fact_id in chosen_ids
                        and link.right_fact_id in chosen_ids
                    ):
                        if consumed + _size(link) <= command.budget:
                            kept_links.append(link)
                            consumed += _size(link)
                        else:
                            exhausted = True
                kept_ids = {link.relationship_id for link in kept_links}
                for resolution in resolutions:
                    if resolution.disagreement_id in kept_ids:
                        if consumed + _size(resolution) <= command.budget:
                            kept_resolutions.append(resolution)
                            consumed += _size(resolution)
                        else:
                            exhausted = True
                chosen = _flag_context(
                    fetch,
                    chosen,
                    command.scope,
                    ceiling,
                    now,
                    kept_links,
                    kept_resolutions,
                )
                consumed = (
                    sum(_size(hit) for hit in chosen)
                    + sum(_size(correction) for correction in kept_corrections)
                    + sum(_size(link) for link in kept_links)
                    + sum(_size(resolution) for resolution in kept_resolutions)
                )
                result = MemoryHistory(
                    tuple(chosen),
                    tuple(kept_corrections),
                    tuple(kept_links),
                    tuple(kept_resolutions),
                    consumed,
                    exhausted,
                )
                authorising = _covering_retrieve_grants(grants, command.scope, now)[0]
            self._audit_read(
                actor,
                command.scope,
                authorising.grant_id,
                correlation_id,
                action,
                tuple(f.fact.fact_id for f in result.facts),
            )
            return result
        except MutationRejection as error:
            return self._reject(error)

    def suggest(
        self, actor: Actor, command: Suggest, *, correlation_id: UUID
    ) -> SuggestionResult | Rejected:
        from cairn.authority.housekeeping import suggest

        return suggest(self, actor, command, correlation_id=correlation_id)

    def _mutation_access(
        self,
        fetch: Fetch,
        actor: Actor,
        command: Disagree | Resolve,
        now: datetime,
        correlation_id: UUID,
        action: str,
    ) -> tuple[GrantRecord, tuple[UUID, UUID], Classification]:
        grants, ceiling = self._access(
            fetch, actor, command.scope, now, correlation_id, action
        )
        if isinstance(command, Disagree):
            endpoints = (command.left_fact_id, command.right_fact_id)
            classification = command.classification
            operation = GrantOperation.INGEST
        else:
            rows = fetch(
                "SELECT left_fact_id, right_fact_id, classification, realm_id, scope_segments FROM memory_disagreements WHERE relationship_id = ?",
                (str(command.disagreement_id),),
            )
            if not rows:
                raise self._refuse(
                    fetch, actor, correlation_id, action, scope=command.scope
                )
            left, right, level, realm, segments = cast(
                tuple[str, str, str, str, str], rows[0]
            )
            endpoints = (UUID(left), UUID(right))
            classification = Classification(level)
            if (
                _stored_scope(realm, segments) != command.scope
                or CLEARANCE_ORDER[classification] > ceiling
            ):
                raise self._refuse(
                    fetch, actor, correlation_id, action, scope=command.scope
                )
            evidence_level = _evidence(
                fetch, command.evidence_id, command.scope, ceiling, now
            )
            if evidence_level is None:
                raise self._refuse(
                    fetch, actor, correlation_id, action, scope=command.scope
                )
            classification = max(
                (classification, evidence_level), key=CLEARANCE_ORDER.__getitem__
            )
            if (
                command.selected_fact_id is not None
                and command.selected_fact_id not in endpoints
            ):
                raise self._refuse(
                    fetch,
                    actor,
                    correlation_id,
                    action,
                    FailureCode.INVALID_REQUEST,
                    "invalid_selection",
                    scope=command.scope,
                )
            operation = GrantOperation.PROMOTE
        authorising = find_authorising_grant(
            grants,
            realm_id=command.scope.realm,
            segments=command.scope.segments,
            operation=operation,
            at=now,
        )
        loaded, _ = _load_candidates(fetch, endpoints)
        if (
            authorising is None
            or len(loaded) != 2
            or endpoints[0] == endpoints[1]
            or any(
                f.scope != command.scope
                or not _can_read(f, command.scope, ceiling, now)
                for f in loaded
            )
        ):
            raise self._refuse(
                fetch, actor, correlation_id, action, scope=command.scope
            )
        if (
            classification not in authorising.write_classifications
            or CLEARANCE_ORDER[classification] > ceiling
            or any(
                CLEARANCE_ORDER[f.classification] > CLEARANCE_ORDER[classification]
                for f in loaded
            )
        ):
            raise self._refuse(
                fetch, actor, correlation_id, action, scope=command.scope
            )
        return authorising, endpoints, classification

    def disagree(
        self,
        actor: Actor,
        command: Disagree,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[RelationshipRecorded]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def resolve(
        self,
        actor: Actor,
        command: Resolve,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[RelationshipRecorded]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def _mutate(
        self,
        actor: Actor,
        command: Disagree | Resolve,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[RelationshipRecorded]:
        now = self._clock()
        action = (
            "memory-disagree" if isinstance(command, Disagree) else "memory-resolve"
        )
        try:
            with read_connection(self._data_path) as connection:
                fetch = fetch_from(connection)
                authorising, endpoints, classification = self._mutation_access(
                    fetch, actor, command, now, correlation_id, action
                )
                try:
                    validate_reason(command.reason)
                except CustodyValueError:
                    raise self._refuse(
                        fetch,
                        actor,
                        correlation_id,
                        action,
                        FailureCode.INVALID_REQUEST,
                        "invalid_reason",
                        scope=command.scope,
                    ) from None
            identity = self._uuid_factory()
            draft = realm_draft(
                realm_id=command.scope.realm,
                actor=actor,
                grant_id=authorising.grant_id,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=command.scope,
                outcome=Outcome.ALLOW,
                reason_code="memory_relationship_recorded",
                correlation_id=correlation_id,
                affected_fact_ids=tuple(sorted(endpoints, key=str)),
            )

            def reauthorise(transaction: _GuardedTransaction) -> None:
                nonlocal now
                # Queueing behind the writer lock can outlive a grant. This one
                # fresh instant governs both reauthorisation and stored mutation time.
                now = self._clock()
                current = self._mutation_access(
                    transaction.query, actor, command, now, correlation_id, action
                )
                if current != (authorising, endpoints, classification):
                    raise self._refuse(
                        transaction.query,
                        actor,
                        correlation_id,
                        action,
                        scope=command.scope,
                    )

            def mutation(transaction: _MutationTransaction) -> RelationshipRecorded:
                self._screen_text(
                    transaction.query,
                    actor,
                    command.reason,
                    correlation_id,
                    action,
                    scope=command.scope,
                )
                if isinstance(command, Disagree):
                    transaction.execute(
                        "INSERT INTO memory_disagreements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(identity),
                            command.scope.realm,
                            canonical_segments_json(command.scope.segments),
                            str(command.left_fact_id),
                            str(command.right_fact_id),
                            classification.value,
                            str(actor.principal_id),
                            command.reason,
                            canonical_timestamp(now),
                            str(transaction.mutation_id),
                        ),
                    )
                else:
                    transaction.execute(
                        "INSERT INTO memory_resolutions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(identity),
                            str(command.disagreement_id),
                            str(command.evidence_id),
                            None
                            if command.selected_fact_id is None
                            else str(command.selected_fact_id),
                            classification.value,
                            str(actor.principal_id),
                            command.reason,
                            canonical_timestamp(now),
                            str(transaction.mutation_id),
                        ),
                    )
                return RelationshipRecorded(identity)

            return self._transactions.mutate_idempotent(
                draft,
                principal_id=actor.principal_id,
                operation=action,
                idempotency_key=idempotency_key,
                command_digest=hashlib.sha256(
                    _json(
                        {
                            "schema": "cairn.memory/v1",
                            "operation": action,
                            "command": asdict(command),
                        }
                    )
                ).digest(),
                result_schema="cairn.memory.relationship/v1",
                mutation=mutation,
                encode=_encode,
                decode=_decode,
                reauthorise=reauthorise,
            )
        except MutationRejection as error:
            return self._reject(error)


def _encode(value: RelationshipRecorded, receipt: MutationReceipt) -> bytes:
    return _json(
        {
            "relationship_id": str(value.relationship_id),
            "mutation_receipt": {
                "mutation_id": str(receipt.mutation_id),
                "command_digest": receipt.command_digest.hex(),
            },
        }
    )


def _decode(data: bytes) -> tuple[RelationshipRecorded, MutationReceipt]:
    doc = json.loads(data)
    return RelationshipRecorded(UUID(doc["relationship_id"])), MutationReceipt(
        UUID(doc["mutation_receipt"]["mutation_id"]),
        bytes.fromhex(doc["mutation_receipt"]["command_digest"]),
    )
