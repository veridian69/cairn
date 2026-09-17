"""Bounded read composition inside CairnMemory's existing authority boundary.

Bounds apply to admitted provenance/history work and returned records, not the
existing recall catalogue scan. No mutation capability is used by this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import RFC_4122, UUID

from cairn.authority.gate import Actor, fetch_from
from cairn.authority.housekeeping_types import Suggest, Suggestion, SuggestionResult
from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_types import History, MemoryFact, Recall
from cairn.authority.retrieval import (
    _admitted,
    _covering_retrieve_grants,
    _load_candidates,
)
from cairn.catalogue.sqlite import CatalogueStorageError, read_connection
from cairn.catalogue.transactions import FailureCode, MutationRejection, Rejected

if TYPE_CHECKING:
    from cairn.authority.memory import CairnMemory

READ_BYTES = 8192
ROOT_LIMIT = 8
RECORD_LIMIT = 16
WORK_BYTES = ROOT_LIMIT * 2 * READ_BYTES
ACTION = "memory-suggest"


def _valid(command: Suggest) -> bool:
    if (
        type(command.budget) is not int
        or not 1 <= command.budget <= 65536
        or type(command.limit) is not int
        or not 1 <= command.limit <= 16
        or type(command.fact_ids) is not tuple
        or len(command.fact_ids) > ROOT_LIMIT
        or any(
            type(identity) is not UUID
            or identity.variant != RFC_4122
            or identity.version != 4
            for identity in command.fact_ids
        )
        or len(set(command.fact_ids)) != len(command.fact_ids)
    ):
        return False
    if command.observation is None:
        return bool(command.fact_ids)
    if command.fact_ids or type(command.observation) is not str:
        return False
    try:
        return 1 <= len(command.observation.encode("utf-8")) <= 4096
    except UnicodeEncodeError:
        return False


def suggest(
    memory: CairnMemory, actor: Actor, command: Suggest, *, correlation_id: UUID
) -> SuggestionResult | Rejected:
    # Local import keeps the helper within the same component without a cycle.
    from cairn.authority.memory import _ALL_TRUST, _can_read, _json, _visible

    try:
        now = memory._clock()
        with read_connection(memory._data_path) as connection:
            fetch = fetch_from(connection)
            _, initial_ceiling = memory._access(
                fetch, actor, command.scope, now, correlation_id, ACTION
            )
            if not _valid(command):
                raise memory._refuse(
                    fetch,
                    actor,
                    correlation_id,
                    ACTION,
                    FailureCode.INVALID_REQUEST,
                    "invalid_suggestion",
                    scope=command.scope,
                )
            if command.observation is not None:
                memory._screen_text(
                    fetch,
                    actor,
                    command.observation,
                    correlation_id,
                    ACTION,
                    scope=command.scope,
                )
            roots, _ = _load_candidates(fetch, command.fact_ids)
            if len(roots) != len(command.fact_ids) or any(
                not _can_read(fact, command.scope, initial_ceiling, now)
                for fact in roots
            ):
                raise memory._refuse(fetch, actor, correlation_id, ACTION)
            roots_by_id = {fact.fact_id: fact for fact in roots}

        candidates: list[Suggestion] = []
        observed: dict[UUID, MemoryFact] = {}
        query_roots: dict[UUID, MemoryFact] = {}
        exhausted = False
        degraded = False
        policy: str | None = None
        work_bytes = 0
        queries: list[tuple[str, UUID | None]] = []
        if command.observation is not None:
            queries.append((command.observation, None))
        for identity in command.fact_ids:
            history = memory.history(
                actor,
                History(command.scope, identity, READ_BYTES),
                correlation_id=correlation_id,
                _record_limit=RECORD_LIMIT,
            )
            if isinstance(history, Rejected):
                return history
            work_bytes += history.budget_consumed
            exhausted |= history.budget_exhausted
            facts = {fact.fact.fact_id: fact for fact in history.facts}
            observed.update(facts)
            if identity in facts:
                query_roots[identity] = facts[identity]
                queries.append((roots_by_id[identity].body, identity))
            else:
                exhausted = True
            for correction in history.corrections:
                replacement = correction.superseded_by
                if (
                    replacement is not None
                    and correction.fact_id in facts
                    and replacement in facts
                ):
                    candidates.append(
                        Suggestion(
                            "possible_correction",
                            (facts[correction.fact_id], facts[replacement]),
                            "Recorded correction history; inspect both attributed claims before acting.",
                            "recorded_correction",
                            (correction,),
                        )
                    )
            for disagreement in history.disagreements:
                if (
                    disagreement.left_fact_id in facts
                    and disagreement.right_fact_id in facts
                ):
                    candidates.append(
                        Suggestion(
                            "related_disagreement",
                            (
                                facts[disagreement.left_fact_id],
                                facts[disagreement.right_fact_id],
                            ),
                            "An attributed disagreement is recorded; no claim is automatically preferred.",
                            "recorded_disagreement",
                            disagreements=(disagreement,),
                        )
                    )

        for query, root_id in queries:
            recalled = memory.recall(
                actor,
                Recall(command.scope, query, READ_BYTES, relevant_only=True),
                correlation_id=correlation_id,
                _candidate_limit=RECORD_LIMIT,
                _include_relationships=True,
            )
            if isinstance(recalled, Rejected):
                return recalled
            work_bytes += recalled.budget_consumed
            exhausted |= recalled.budget_exhausted
            degraded |= recalled.semantic_degraded or memory._index is None
            policy = recalled.policy
            recalled_facts = {hit.fact.fact_id: hit for hit in recalled.hits}
            for disagreement in recalled.disagreements:
                if (
                    disagreement.left_fact_id in recalled_facts
                    and disagreement.right_fact_id in recalled_facts
                ):
                    candidates.append(
                        Suggestion(
                            "related_disagreement",
                            (
                                recalled_facts[disagreement.left_fact_id],
                                recalled_facts[disagreement.right_fact_id],
                            ),
                            "An attributed disagreement is recorded; no claim is automatically preferred.",
                            "recorded_disagreement",
                            disagreements=(disagreement,),
                        )
                    )
            for hit in recalled.hits:
                observed[hit.fact.fact_id] = hit
                if hit.fact.fact_id == root_id:
                    continue
                historical_root = root_id is not None and not _admitted(
                    roots_by_id[root_id],
                    command.scope,
                    initial_ceiling,
                    _ALL_TRUST,
                    now,
                )
                exact = hit.fact.body == query and not historical_root
                endpoints = (hit,) if root_id is None else (query_roots[root_id], hit)
                candidates.append(
                    Suggestion(
                        "exact_duplicate" if exact else "possible_duplicate",
                        endpoints,
                        "The current fact body is byte-identical; attribution and validity still matter."
                        if exact
                        else "Selected evidence is historical or not currently valid; candidate equivalence is unverified."
                        if historical_root
                        else "Retrieval found a related candidate; equivalence is unverified.",
                        "exact_body" if exact else "retrieval_candidate",
                    )
                )

        # Every actual read is charged, even when evidence is later deduplicated.
        assert work_bytes <= WORK_BYTES
        selected: list[Suggestion] = []
        seen: set[bytes] = set()
        consumed = 0
        for candidate in candidates:
            # Relevance scores can differ across root queries; identity dedup is
            # independent of those scores and has no authority significance.
            item_identity = _json(
                (
                    candidate.kind,
                    tuple(str(f.fact.fact_id) for f in candidate.facts),
                    tuple(str(c.fact_id) for c in candidate.corrections),
                    tuple(str(d.relationship_id) for d in candidate.disagreements),
                )
            )
            if item_identity in seen:
                continue
            seen.add(item_identity)
            size = len(_json(memory_value(candidate)))
            if len(selected) >= command.limit or consumed + size > command.budget:
                exhausted = True
                continue
            selected.append(candidate)
            consumed += size

        # Final current-authority check covers buffered evidence too: otherwise
        # omission flags could retain information from a now-hidden candidate.
        now = memory._clock()
        with read_connection(memory._data_path) as connection:
            fetch = fetch_from(connection)
            grants, ceiling = memory._access(
                fetch, actor, command.scope, now, correlation_id, ACTION
            )
            if ceiling != initial_ceiling:
                raise memory._refuse(fetch, actor, correlation_id, ACTION)
            loaded, _ = _load_candidates(
                fetch, tuple(dict.fromkeys((*command.fact_ids, *observed)))
            )
            current = {fact.fact_id: fact for fact in loaded}
            if len(current) != len(set(command.fact_ids) | set(observed)):
                raise memory._refuse(fetch, actor, correlation_id, ACTION)
            for identity, fact in current.items():
                if not _can_read(fact, command.scope, ceiling, now):
                    raise memory._refuse(fetch, actor, correlation_id, ACTION)
                if identity in roots_by_id and fact != roots_by_id[identity]:
                    raise memory._refuse(fetch, actor, correlation_id, ACTION)
                if identity in observed:
                    fresh = memory._fact(fetch, fact, command.scope, ceiling, now)
                    prior = observed[identity]
                    if (fresh.fact, fresh.source_principal_id, fresh.source_type) != (
                        prior.fact,
                        prior.source_principal_id,
                        prior.source_type,
                    ):
                        raise memory._refuse(fetch, actor, correlation_id, ACTION)
            for candidate in selected:
                if candidate.kind in ("exact_duplicate", "possible_duplicate") and any(
                    not _admitted(
                        current[f.fact.fact_id], command.scope, ceiling, _ALL_TRUST, now
                    )
                    for f in (
                        candidate.facts
                        if candidate.kind == "exact_duplicate"
                        else candidate.facts[-1:]
                    )
                ):
                    raise memory._refuse(fetch, actor, correlation_id, ACTION)
                if any(
                    not _visible(
                        link.scope, link.classification, command.scope, ceiling
                    )
                    or link.recorded_at > now
                    for link in candidate.disagreements
                ):
                    raise memory._refuse(fetch, actor, correlation_id, ACTION)
            grant = _covering_retrieve_grants(grants, command.scope, now)[0]
        memory._audit_read(
            actor,
            command.scope,
            grant.grant_id,
            correlation_id,
            ACTION,
            tuple(
                dict.fromkeys(f.fact.fact_id for item in selected for f in item.facts)
            ),
        )
        return SuggestionResult(tuple(selected), consumed, exhausted, degraded, policy)
    except MutationRejection as error:
        return memory._reject(error)
    except CatalogueStorageError:
        # Stored-value corruption must not turn into raw content-bearing errors.
        with read_connection(memory._data_path) as connection:
            refusal = memory._refuse(
                fetch_from(connection), actor, correlation_id, ACTION
            )
        return memory._reject(refusal)
