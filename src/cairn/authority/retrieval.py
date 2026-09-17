"""Reconciled retrieval: the I-77 ``Retrieve`` command and its P-42 pipeline.

The pipeline order is frozen and mirrors I-66's seam order: authenticate
(the caller's job — an ``Actor`` arrives resolved), authorise, validate
values and limits, screen the query, fan out to the index and — when
exact evidence is enabled — to Attic, reconcile every candidate against
the authoritative catalogue, then order and budget. Neither adapter is
trusted: ``IndexAdapter.search`` returns bare fact identities and
``AtticAdapter.search`` bare evidence identities, and nothing is disclosed
until the catalogue confirms the fact exists, sits on the request scope's
ancestry chain, passes the trust filters, fits under the derived
classification ceiling and is visible on both I-81 temporal axes. A
hostile or stale adapter is therefore a recall problem, never a
disclosure problem — the two modalities widen reach and share one gate.

Unlike the three mutation commands there is no idempotency key and no
writer gate (I-27's read rule, the P-28 ``read-audit-events`` precedent):
the pipeline takes read connections only, and the single write is the
allow event recording the read. That event records the requested scope,
the authorising grant, the disclosed fact identities (the counts) and a
fingerprint of the request's filter parameters — never the query text,
which is screened caller content under I-31 and appears in no stored row,
log or audit byte.

Two conditions the discards deliberately do not cover. A projection outbox
holding undelivered rows relevant to the request — rows whose fact sits on
the request scope's ancestry chain, queued at or before ``as_of`` — is a
known-behind index where a retry genuinely helps, so it is the one public
retrieval failure with the after-delay retry class (I-83 ``index_pending``).
And a candidate naming an identity the catalogue has never held is evidence
of index corruption rather than lag, so it increments the ``stale_index``
metric and emits a safe log event carrying only the candidate UUID and the
correlation id (P-47) — an operator signal, never a wire failure, and never
a probe a caller could distinguish from an ordinary discard. A candidate
whose stored row the value layer refuses is *not* that signal: its identity
exists, so what it evidences is catalogue corruption, which offline
verification (I-48) owns; it is discarded silently like every other
unreconcilable candidate.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation
from cairn.authority.custody import (
    CustodyValueError,
    FactProvenance,
    IngestedProvenance,
    PromotedProvenance,
)
from cairn.authority.gate import (
    AUTHORISATION_DENIED_MESSAGE as _AUTHORISATION_DENIED_MESSAGE,
)
from cairn.authority.gate import INVALID_REQUEST_MESSAGE as _INVALID_REQUEST_MESSAGE
from cairn.authority.gate import NOT_FOUND_MESSAGE as _NOT_FOUND_MESSAGE
from cairn.authority.gate import SECRET_REJECTED_MESSAGE as _SECRET_REJECTED_MESSAGE
from cairn.authority.gate import Actor as Actor
from cairn.authority.gate import Fetch as _Fetch
from cairn.authority.gate import fetch_from as _fetch_from
from cairn.authority.gate import instance_denial_draft as _instance_denial_draft
from cairn.authority.gate import instance_id as _instance_id
from cairn.authority.gate import realm_draft as _realm_draft
from cairn.authority.gate import realm_exists as _realm_exists
from cairn.authority.grants import GrantRecord, is_live, is_scope_prefix
from cairn.authority.mutations import (
    _realm_fingerprint,
    _scope_shape_failure,
    _sorted_grants,
    _stored_scope,
    _stored_timestamp,
)
from cairn.catalogue.audit import (
    ActionKind,
    AuditValueError,
    Classification,
    Outcome,
    Scope,
    TrustClass,
)
from cairn.catalogue.sqlite import canonical_timestamp, read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    FailureDetail,
    Rejected,
    RetryClass,
    StableFailure,
)
from cairn.evidence.adapter import AtticAdapter
from cairn.evidence.reconciliation import ScopeDirection, reconcile_evidence
from cairn.operations.metrics import Metrics
from cairn.projection.adapter import IndexAdapter
from cairn.projection.partition import canonical_partition, canonical_segments_json
from cairn.runtime.logging import LogEvent, SafeLogger
from cairn.screening import (
    POLICY_VERSION,
    SecretScreen,
    audit_reason_code,
    first_finding,
)

_RETRIEVE = "retrieve"

# I-30: the retrieval query is opaque UTF-8, 1 byte to 8 KiB after encoding.
MAX_QUERY_BYTES = 8192
# P-42: the budget is a positive integer of bytes, bounded by the I-30
# assertion ceiling — sixteen maximal 64 KiB fact bodies. An unbounded wire
# integer is not acceptable, and a larger budget could never be consumed.
MAX_BUDGET_BYTES = 1_048_576

# I-80: empty trust filters mean validated only, per spec §6.4. Candidate
# and failed-approach facts are returned only when named explicitly.
_DEFAULT_TRUST_FILTERS = frozenset({TrustClass.VALIDATED})

# The derived ceiling travels as an order for comparison against stored
# classifications; evidence reconciliation takes a Classification, so this
# inverts CLEARANCE_ORDER once rather than at each call.
_CLEARANCE_BY_ORDER = {order: value for value, order in CLEARANCE_ORDER.items()}

# Domain separation for the request fingerprint, by the partition-key
# derivation's precedent: another Cairn digest over the same bytes can
# never share this namespace.
_FINGERPRINT_DOMAIN = b"cairn.retrieve.request/v1\x00"

# I-83's honest 503: the index is known to be behind and a retry after the
# deliverer catches up will genuinely help — the only retrieval failure in
# the after-delay class.
_INDEX_PENDING_MESSAGE = "The retrieval index is not yet consistent with the catalogue."


# --- command and result (closed, frozen; I-77) -------------------------------


@dataclass(frozen=True, slots=True)
class Retrieve:
    scope: Scope
    query: str
    budget: int
    trust_filters: frozenset[TrustClass] = frozenset()
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetrievedFact:
    """Exactly the I-67 stored fields, nothing invented for the wire.

    ``provenance`` is the stored columns in their two lawful forms:
    ``IngestedProvenance`` for ingested facts, ``PromotedProvenance`` for
    promoted ones.

    ``invalidated_at`` is present only where an invalidation row exists
    **and is visible at the request's** ``as_of`` (P-42), which in v0.1
    means it is never present on a hit. I-79 discards any fact whose
    invalidation the request can see, so a surviving hit's invalidation
    is always in the request's future, and disclosing it would let a
    point-in-time read learn something recorded after the instant it
    asked about. The field stays in the closed hit shape because I-77
    froze that shape as the I-67 stored fields; a later version that
    checks an invalidation's own scope and clearance before disclosing it
    would fill this in without changing the wire.
    """

    fact_id: UUID
    body: str
    scope: Scope
    classification: Classification
    trust: TrustClass
    provenance: FactProvenance
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    invalidated_at: datetime | None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """``budget_exhausted`` means assembly stopped because the next fact in
    the deterministic order would not fit — "there was more", as distinct
    from "that is everything" (I-82)."""

    hits: tuple[RetrievedFact, ...]
    budget_consumed: int
    budget_exhausted: bool


# --- the pipeline ------------------------------------------------------------


def retrieve(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    command: Retrieve,
    *,
    index: IndexAdapter | None,
    screen: SecretScreen,
    correlation_id: UUID,
    clock: Callable[[], datetime],
    attic: AtticAdapter | None = None,
    metrics: Metrics | None = None,
    logger: SafeLogger | None = None,
) -> RetrievalResult | Rejected:
    effective_at = clock()
    scope = command.scope

    # Caller-supplied scope shape settled before the scope is used anywhere,
    # including in the event recording its own refusal — the mutation
    # commands' rule, applied to the read.
    shape_failure = _scope_shape_failure(scope)
    if shape_failure is not None:
        return _reject_instance(
            data_path,
            transactions,
            actor,
            correlation_id,
            shape_failure,
            fingerprint=_realm_fingerprint(scope.realm),
        )

    with read_connection(data_path) as connection:
        fetch = _fetch_from(connection)
        if not _realm_exists(fetch, scope.realm):
            return _reject_instance(
                data_path,
                transactions,
                actor,
                correlation_id,
                "realm_not_found",
                code=FailureCode.NOT_FOUND,
                message=_NOT_FOUND_MESSAGE,
                fetch=fetch,
                fingerprint=_realm_fingerprint(scope.realm),
            )

        # I-77: a live retrieve grant whose scope covers the request scope
        # under is_scope_prefix. Every covering grant is kept, not just the
        # first: I-80's classification ceiling is the maximum read_clearance
        # across them, while the first in the deterministic order is the one
        # the audit event names — the same one every evaluation of identical
        # state would name.
        grants = _sorted_grants(fetch, actor.principal_id, scope.realm)
        covering = _covering_retrieve_grants(grants, scope, effective_at)
        if not covering:
            # Instance chain, by the data-plane rule the mutation commands
            # follow: a refusal decided before the actor is proven to have
            # standing may not touch (or meter) a realm chain.
            return _reject_instance(
                data_path,
                transactions,
                actor,
                correlation_id,
                "retrieve_grant_not_held",
                code=FailureCode.AUTHORISATION_DENIED,
                message=_AUTHORISATION_DENIED_MESSAGE,
                fetch=fetch,
                fingerprint=_realm_fingerprint(scope.realm),
            )
        authorising = covering[0]
        ceiling = max(CLEARANCE_ORDER[grant.read_clearance] for grant in covering)

        value_failure = _value_failure(command, index_enabled=index is not None)
        if value_failure is not None:
            return _deny(
                transactions,
                actor,
                scope,
                authorising.grant_id,
                correlation_id,
                value_failure,
            )
        # The value checks above prove index is present past this point.
        assert index is not None

        # I-31: the query is screened caller content — screened here, after
        # value validation bounds the scan and before any remote call.
        finding = first_finding(screen, (("query", command.query),))
        if finding is not None:
            return _screen_denial(
                transactions,
                finding.rule,
                finding.field_path,
                actor,
                scope,
                authorising.grant_id,
                correlation_id,
            )

        as_of = command.as_of if command.as_of is not None else effective_at
        trust_filters = (
            command.trust_filters if command.trust_filters else _DEFAULT_TRUST_FILTERS
        )

        # I-83: undelivered projection work relevant to this request means
        # the index is known to be behind and a retry will genuinely help —
        # checked after the screen (P-42's order) and before the fan-out, so
        # a lagging index is never even asked.
        if _index_pending(fetch, scope, as_of):
            return _deny(
                transactions,
                actor,
                scope,
                authorising.grant_id,
                correlation_id,
                "index_pending",
                code=FailureCode.INDEX_PENDING,
                message=_INDEX_PENDING_MESSAGE,
                retry=RetryClass.AFTER_DELAY,
            )

        # P-42: fan out with the partition keys of the request scope's
        # ancestry chain, the realm root through the request scope itself.
        # The limit is the budget: a disclosed fact costs at least one body
        # byte, so no query can disclose more than ``budget`` facts — a
        # deterministic bound owed nothing by the adapter.
        candidates = index.search(
            command.query, command.budget, _ancestry_partitions(scope)
        )
        unique = tuple(dict.fromkeys(candidates))
        stored, present = _load_candidates(fetch, unique)

        # P-43: the second retrieval modality. Attic returns evidence
        # identities; each is reconciled by the frozen I-69 rule and then
        # mapped through the catalogue to the facts of its assertion, which
        # face the identical I-79/I-80/I-81 filters below. Payload bytes
        # never reach the wire — I-77's hit shape is the stored fact fields,
        # and I-82 counts fact bodies — so what the modality contributes is
        # reach, not a second kind of disclosure.
        if attic is not None:
            attic_facts = _attic_candidates(
                data_path,
                fetch,
                attic,
                scope=scope,
                query=command.query,
                limit=command.budget,
                ceiling=ceiling,
                metrics=metrics,
                logger=logger,
            )
            # Deduplicated against what the index modality already loaded: a
            # fact both modalities reach is one hit costing its body once.
            from_index = {fact.fact_id for fact in stored}
            stored = stored + tuple(
                fact for fact in attic_facts if fact.fact_id not in from_index
            )

    # P-47: an identity the catalogue has never held is evidence of index
    # corruption, not lag — an operator signal, never a wire failure, and
    # the served response is unchanged. Emitted per unique candidate in the
    # adapter's own order, carrying only the UUID and the correlation id.
    for candidate in unique:
        if candidate not in present:
            if metrics is not None:
                metrics.observe_stale_index()
            if logger is not None:
                logger.emit(
                    LogEvent.STALE_INDEX_CANDIDATE,
                    transport=None,
                    candidate_id=candidate,
                    correlation_id=correlation_id,
                )

    admitted = [
        _disclosed(fact, as_of)
        for fact in stored
        if _admitted(fact, scope, ceiling, trust_filters, as_of)
    ]
    hits, consumed, exhausted = _assemble(admitted, command.budget)

    allow_draft = replace(
        _realm_draft(
            realm_id=scope.realm,
            actor=actor,
            grant_id=authorising.grant_id,
            action_kind=ActionKind.DATA,
            action_code=_RETRIEVE,
            requested_scope=scope,
            outcome=Outcome.ALLOW,
            reason_code="retrieval_completed",
            correlation_id=correlation_id,
            affected_fact_ids=tuple(sorted((hit.fact_id for hit in hits), key=str)),
        ),
        # The filters, correlatably but without disclosure: the closed I-54
        # event value has no field that could carry them in the clear, and
        # the fingerprint is the trade the record makes when the alternative
        # is recording nothing. Never the query (I-31).
        safe_request_fingerprint=_request_fingerprint(command),
    )
    transactions.append_audit(allow_draft)

    return RetrievalResult(
        hits=hits, budget_consumed=consumed, budget_exhausted=exhausted
    )


# --- authorisation and validation --------------------------------------------


def _covering_retrieve_grants(
    grants: Sequence[GrantRecord], scope: Scope, at: datetime
) -> tuple[GrantRecord, ...]:
    return tuple(
        grant
        for grant in grants
        if grant.realm_id == scope.realm
        and GrantOperation.RETRIEVE in grant.operations
        and is_scope_prefix(grant.segments, scope.segments)
        and is_live(grant, at)
    )


def _value_failure(command: Retrieve, *, index_enabled: bool) -> str | None:
    # P-48: with no index configured, refused before any work — the honest
    # answer, because no retry will ever help, which is exactly what a 503
    # would falsely promise. Mirrors I-69's evidence_disabled.
    if not index_enabled:
        return "retrieval_disabled"
    if type(command.query) is not str or not command.query:
        return "invalid_query"
    if len(command.query.encode("utf-8")) > MAX_QUERY_BYTES:
        return "query_too_large"
    if (
        type(command.budget) is not int
        or command.budget < 1
        or command.budget > MAX_BUDGET_BYTES
    ):
        return "invalid_budget"
    if type(command.trust_filters) is not frozenset or any(
        type(value) is not TrustClass for value in command.trust_filters
    ):
        return "invalid_trust_filter"
    if command.as_of is not None and (
        type(command.as_of) is not datetime
        or command.as_of.tzinfo is None
        or command.as_of.utcoffset() is None
    ):
        return "invalid_timestamp"
    return None


def _ancestry_partitions(scope: Scope) -> tuple[str, ...]:
    return tuple(
        canonical_partition(
            scope.realm, canonical_segments_json(scope.segments[:depth])
        )
        for depth in range(len(scope.segments) + 1)
    )


def _index_pending(fetch: _Fetch, scope: Scope, as_of: datetime) -> bool:
    """I-83's trigger: an undelivered projection outbox row whose fact sits
    on the request scope's ancestry chain, queued at or before ``as_of``.

    A relevant fact's stored ``scope_segments`` can only be one of the
    ancestry chain's canonical encodings — the same bytes the mutation
    writer stores and ``canonical_segments_json`` produces — so the match
    is an exact ``IN`` over at most seventeen values, not a prefix parse.
    Canonical timestamps order lexicographically, so the ``created_at``
    comparison is a plain string comparison. All four outbox kinds matter
    and none is special-cased: an undelivered ``fact-ingested`` or
    ``fact-promoted`` row is a recall gap, an undelivered
    ``fact-invalidated`` row means the index still serves belief the
    catalogue has ended, and a surviving ``fact-rebuild`` row means a
    rebuild cleared the index and could not put that fact back (I-68 and
    I-83, both amended 9 August 2026). The last is why this check is the
    recovery path and not merely a lag warning: such a row is stamped with
    its fact's ``recorded_at``, so it withholds exactly the requests whose
    ``as_of`` could have seen the missing fact, historical ones included.
    """
    ancestry = tuple(
        canonical_segments_json(scope.segments[:depth])
        for depth in range(len(scope.segments) + 1)
    )
    placeholders = ",".join("?" * len(ancestry))
    rows = fetch(
        "SELECT 1 FROM projection_outbox JOIN facts "
        "ON facts.fact_id = projection_outbox.fact_id "
        "WHERE projection_outbox.created_at <= ? AND facts.realm_id = ? "
        f"AND facts.scope_segments IN ({placeholders}) LIMIT 1",
        [canonical_timestamp(as_of), scope.realm, *ancestry],
    )
    return bool(rows)


def _attic_candidates(
    data_path: Path,
    fetch: _Fetch,
    attic: AtticAdapter,
    *,
    scope: Scope,
    query: str,
    limit: int,
    ceiling: int,
    metrics: Metrics | None,
    logger: SafeLogger | None,
) -> tuple[RetrievedFact, ...]:
    """The P-43 modality: Attic candidates reconciled as evidence, then
    mapped to the facts of their assertions.

    The evidence gate is ``AT_OR_ABOVE`` per P-43 as amended 9 August 2026:
    evidence at the request scope or an ancestor is in reach (`EVIDENCE-01`),
    descendant, sibling and cross-realm evidence is not (`EVIDENCE-02`), and
    unknown, stale and above-clearance candidates are discarded by the same
    I-69 checks that serve `EVIDENCE-03`. Running it in the same direction
    as the I-79 fact filters is what makes the two gates compose to the
    ancestry chain rather than intersect to the request scope alone.

    The ceiling passed to reconciliation is the derived I-80 ceiling, so
    evidence is never disclosed — even transiently, even as a mapping step —
    above what the caller's grants admit. Each mapped fact then faces the
    identical fact filters at the call site: two independent enforcement
    points, exactly as I-80 pins for classification.

    A reconciled record with no assertion — external-custody evidence, which
    P-43's "only exact evidence is in Attic" does not cover, since a hostile
    adapter may name any identity it likes — maps to no facts and is simply
    dropped. Its payload never reaches the wire either way.
    """
    try:
        candidates = attic.search(query, limit)
    except Exception:
        # An Attic outage narrows recall; it must not fail a retrieval the
        # index modality can already answer. The evidence path logs its own
        # failures per candidate; a failed search has no candidate to name.
        return ()
    if not candidates:
        return ()
    disclosed = reconcile_evidence(
        data_path,
        attic,
        realm_id=scope.realm,
        segments=scope.segments,
        read_clearance=_CLEARANCE_BY_ORDER[ceiling],
        candidates=candidates,
        scope_direction=ScopeDirection.AT_OR_ABOVE,
        metrics=metrics,
        logger=logger,
    )
    if not disclosed:
        return ()
    fact_ids = _assertion_fact_ids(
        fetch, tuple(evidence.evidence_id for evidence in disclosed)
    )
    # The present-set is the index modality's stale-index signal (P-47) and
    # means nothing here: these identities came from the catalogue's own
    # join a moment ago, so absence would be a race, not corruption.
    facts, _ = _load_candidates(fetch, fact_ids)
    return facts


def _assertion_fact_ids(
    fetch: _Fetch, evidence_ids: tuple[UUID, ...]
) -> tuple[UUID, ...]:
    """``evidence_records.assertion_id`` → ``facts.assertion_id``, in a
    deterministic order. Ordering here is courtesy only — I-82 re-orders
    every admitted fact by ``recorded_at`` before assembly — but a stable
    input keeps the pipeline's intermediate values reproducible."""
    placeholders = ",".join("?" * len(evidence_ids))
    rows = fetch(
        "SELECT facts.fact_id FROM facts JOIN evidence_records "
        "ON evidence_records.assertion_id = facts.assertion_id "
        f"WHERE evidence_records.evidence_id IN ({placeholders}) "
        "ORDER BY facts.fact_id",
        [str(evidence_id) for evidence_id in evidence_ids],
    )
    return tuple(UUID(cast(str, row[0])) for row in rows)


def _assemble(
    admitted: list[RetrievedFact], budget: int
) -> tuple[tuple[RetrievedFact, ...], int, bool]:
    """I-82 ordering and budgeting: ``recorded_at`` ascending with the
    fact-identity tiebreak, then whole facts appended until the next in that
    order would exceed the remaining budget — assembly stops there, so a
    later, smaller fact is never smuggled past the one that stopped it, and
    no fact body is ever split. The exhausted flag is exactly "assembly
    stopped for budget"."""
    ordered = sorted(admitted, key=lambda fact: (fact.recorded_at, str(fact.fact_id)))
    hits: list[RetrievedFact] = []
    consumed = 0
    exhausted = False
    for fact in ordered:
        cost = len(fact.body.encode("utf-8"))
        if consumed + cost > budget:
            exhausted = True
            break
        hits.append(fact)
        consumed += cost
    return tuple(hits), consumed, exhausted


def _request_fingerprint(command: Retrieve) -> bytes:
    document = {
        "budget": command.budget,
        "trust_filters": sorted(value.value for value in command.trust_filters),
        "as_of": (
            None if command.as_of is None else canonical_timestamp(command.as_of)
        ),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(_FINGERPRINT_DOMAIN + encoded.encode("utf-8")).digest()


# --- the catalogue read path -------------------------------------------------

# SQLite bounds host parameters per statement; chunking keeps the candidate
# reads far under every deployed limit.
_SELECT_CHUNK = 400


def _load_candidates(
    fetch: _Fetch, fact_ids: tuple[UUID, ...]
) -> tuple[tuple[RetrievedFact, ...], frozenset[UUID]]:
    """The stored state of every candidate the catalogue knows, and the set
    of identities it holds at all.

    The set is what separates P-47's two cases: an identity outside it has
    never been in the catalogue — the stale-index signal — while a row the
    value layer refuses is *present* but unreconcilable, so it is discarded
    silently and left to offline verification (I-48), which owns catalogue
    corruption reporting.
    """
    loaded: list[RetrievedFact] = []
    present: set[UUID] = set()
    for start in range(0, len(fact_ids), _SELECT_CHUNK):
        chunk = fact_ids[start : start + _SELECT_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        parameters = [str(fact_id) for fact_id in chunk]
        invalidations: dict[str, str] = {
            cast(str, row[0]): cast(str, row[1])
            for row in fetch(
                "SELECT fact_id, invalidated_at FROM fact_invalidations "
                f"WHERE fact_id IN ({placeholders})",
                parameters,
            )
        }
        rows = fetch(
            "SELECT fact_id, realm_id, scope_segments, body, trust, "
            "classification, assertion_id, derived_from, promoted_by, "
            "evidence_id, valid_from, valid_to, recorded_at "
            f"FROM facts WHERE fact_id IN ({placeholders})",
            parameters,
        )
        for row in rows:
            present.add(UUID(cast(str, row[0])))
            try:
                loaded.append(_stored_fact(row, invalidations))
            except (CustodyValueError, AuditValueError):
                continue
    return tuple(loaded), frozenset(present)


def _stored_fact(
    row: tuple[object, ...], invalidations: dict[str, str]
) -> RetrievedFact:
    (
        fact_id,
        realm_id,
        scope_segments,
        body,
        trust,
        classification,
        assertion_id,
        derived_from,
        promoted_by,
        evidence_id,
        valid_from,
        valid_to,
        recorded_at,
    ) = cast(
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str,
        ],
        row,
    )
    # Only the scope and the timestamps go through reading guards; the rest
    # are made total by migration 0003's CHECK constraints, exactly as
    # promotion's _load_sources establishes. ck_facts_provenance_form pins
    # the two lawful column forms, so the branch below is exhaustive.
    provenance: FactProvenance
    if assertion_id is not None:
        provenance = IngestedProvenance(assertion_id=UUID(assertion_id))
    else:
        provenance = PromotedProvenance(
            derived_from=UUID(cast(str, derived_from)),
            promoted_by=UUID(cast(str, promoted_by)),
            evidence_id=UUID(cast(str, evidence_id)),
        )
    invalidated_at = invalidations.get(fact_id)
    return RetrievedFact(
        fact_id=UUID(fact_id),
        body=body,
        scope=_stored_scope(realm_id, scope_segments),
        classification=Classification(classification),
        trust=TrustClass(trust),
        provenance=provenance,
        valid_from=None if valid_from is None else _stored_timestamp(valid_from),
        valid_to=None if valid_to is None else _stored_timestamp(valid_to),
        recorded_at=_stored_timestamp(recorded_at),
        invalidated_at=(
            None if invalidated_at is None else _stored_timestamp(invalidated_at)
        ),
    )


def _admitted(
    fact: RetrievedFact,
    scope: Scope,
    ceiling: int,
    trust_filters: frozenset[TrustClass],
    as_of: datetime,
) -> bool:
    """The I-79 fact filters: ancestry, trust, ceiling and both temporal
    axes. Every refusal is a silent discard — no public failure discloses
    what an index candidate turned out to be."""
    if fact.scope.realm != scope.realm:
        return False
    # SCOPE-01's inherited-ancestor rule: a fact is visible at its own scope
    # and every descendant, so the fact's scope must prefix the request's.
    if not is_scope_prefix(fact.scope.segments, scope.segments):
        return False
    if fact.trust not in trust_filters:
        return False
    if CLEARANCE_ORDER[fact.classification] > ceiling:
        return False
    # I-81 world validity: as_of within [valid_from, valid_to), null-open.
    if fact.valid_from is not None and as_of < fact.valid_from:
        return False
    if fact.valid_to is not None and as_of >= fact.valid_to:
        return False
    # I-81 belief validity: recorded at or before as_of, not yet invalidated.
    if as_of < fact.recorded_at:
        return False
    if fact.invalidated_at is not None and as_of >= fact.invalidated_at:
        return False
    return True


def _disclosed(fact: RetrievedFact, as_of: datetime) -> RetrievedFact:
    """P-42: hide an invalidation the request's ``as_of`` cannot see.

    Stated as the visibility predicate rather than as an unconditional
    null, even though ``_admitted`` above guarantees every survivor falls
    into the hidden case: the rule is "disclose what was believed at
    ``as_of``", and writing the rule rather than its current consequence
    keeps this correct if the filters ever change. The fact keeps its real
    invalidation state until here, because that is what ``_admitted``
    filters on — only the disclosed copy forgets it.
    """
    if fact.invalidated_at is not None and as_of < fact.invalidated_at:
        return replace(fact, invalidated_at=None)
    return fact


# --- denial helpers ----------------------------------------------------------


def _reject_instance(
    data_path: Path,
    transactions: CatalogueTransactions,
    actor: Actor,
    correlation_id: UUID,
    reason_code: str,
    *,
    code: FailureCode = FailureCode.INVALID_REQUEST,
    message: str = _INVALID_REQUEST_MESSAGE,
    fetch: _Fetch | None = None,
    fingerprint: bytes | None = None,
) -> Rejected:
    """A refusal with no realm chain to record it on, by the mutation
    commands' rule: no refusal decided before the actor is proven to have
    standing may touch — or meter — a realm chain."""
    if fetch is None:
        with read_connection(data_path) as connection:
            identity = _instance_id(_fetch_from(connection))
    else:
        identity = _instance_id(fetch)
    draft = _instance_denial_draft(
        identity,
        actor,
        _RETRIEVE,
        reason_code,
        correlation_id,
        action_kind=ActionKind.DATA,
        safe_request_fingerprint=fingerprint,
    )
    failure = StableFailure(
        code=code,
        safe_message=message,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
    )
    return transactions.reject(draft, failure)


def _deny(
    transactions: CatalogueTransactions,
    actor: Actor,
    scope: Scope,
    grant_id: UUID,
    correlation_id: UUID,
    reason_code: str,
    *,
    code: FailureCode = FailureCode.INVALID_REQUEST,
    message: str = _INVALID_REQUEST_MESSAGE,
    retry: RetryClass = RetryClass.NEVER,
) -> Rejected:
    draft = _realm_draft(
        realm_id=scope.realm,
        actor=actor,
        grant_id=grant_id,
        action_kind=ActionKind.DATA,
        action_code=_RETRIEVE,
        requested_scope=scope,
        outcome=Outcome.DENY,
        reason_code=reason_code,
        correlation_id=correlation_id,
    )
    failure = StableFailure(
        code=code,
        safe_message=message,
        correlation_id=correlation_id,
        retry=retry,
    )
    return transactions.reject(draft, failure)


def _screen_denial(
    transactions: CatalogueTransactions,
    rule: str,
    field_path: str,
    actor: Actor,
    scope: Scope,
    grant_id: UUID,
    correlation_id: UUID,
) -> Rejected:
    """The retrieval secret denial, by the custody commands' I-74 shape: the
    event's reason_code names the rule and nothing else; the field path
    travels back to the caller in ``detail`` and never into the chain."""
    draft = _realm_draft(
        realm_id=scope.realm,
        actor=actor,
        grant_id=grant_id,
        action_kind=ActionKind.DATA,
        action_code=_RETRIEVE,
        requested_scope=scope,
        outcome=Outcome.DENY,
        reason_code=audit_reason_code(rule),
        correlation_id=correlation_id,
    )
    failure = StableFailure(
        code=FailureCode.SECRET_REJECTED,
        safe_message=_SECRET_REJECTED_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
        detail=FailureDetail(policy=POLICY_VERSION, rule=rule, field_path=field_path),
    )
    return transactions.reject(draft, failure)
