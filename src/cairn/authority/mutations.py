"""CairnAuthority: the data-plane custody commands.

The pipeline is frozen: authenticate (the caller's job — an ``Actor`` is
already resolved by the time a command arrives here), authorise, validate
values and limits, then commit custody. Authorisation is evaluated twice, and
that is not redundancy to be factored away. The **outer** evaluation runs on a
read-only connection before ``mutate_idempotent`` is entered at all, because
``mutate_idempotent`` serves a stored ``Replayed`` result without ever
invoking the mutation callback: authorising only inside the callback would let
a replay of a stale idempotency key skip re-authorisation entirely, violating
I-44. The **inner** evaluation runs inside the write-locked transaction and is
authoritative, closing the check-then-act gap (P-07) that would otherwise let
a concurrent writer revoke the authorising grant between the outer read and
the write.

Both evaluations, and every temporal semantic of the mutation, use a single
``effective_at`` captured once per request. That is what makes the pair
meaningful rather than flaky: a grant cannot expire *between* the two checks,
so the only thing the inner check can observe is a genuine concurrent
revocation, and the audit timestamps stay deterministic.

The separate memory surface opts into an additional fresh-clock grant check
under the transaction lock, before either a new write or idempotent replay.
This leaves v1's accepted clock, screening and replay behaviour unchanged,
including the custody records' request-time timestamps.

``CairnAuthority`` holds no Attic adapter (P-14). A disabled exact-evidence
store is represented by absence — the single ``exact_evidence_enabled`` flag —
not by a null adapter object. External evidence references and promotion
remain available when it is false; only payload-bearing ingest is refused.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation
from cairn.authority.custody import (
    BATCH_TOO_LARGE,
    DUPLICATE_IDENTITY,
    EMPTY_BATCH,
    MAX_BATCH_FACTS,
    AssertionRecord,
    CustodyValueError,
    EvidenceRecord,
    ExactEvidence,
    ExternalEvidence,
    FactDraft,
    FactRecord,
    IngestedProvenance,
    InvalidationRecord,
    PromotedProvenance,
    SourceType,
    _validate_tz_aware,
    validate_reason,
)
from cairn.authority.gate import (
    AUTHORISATION_DENIED_MESSAGE as _AUTHORISATION_DENIED_MESSAGE,
)
from cairn.authority.gate import INVALID_REQUEST_MESSAGE as _INVALID_REQUEST_MESSAGE
from cairn.authority.gate import NOT_FOUND_MESSAGE as _NOT_FOUND_MESSAGE
from cairn.authority.gate import SECRET_REJECTED_MESSAGE as _SECRET_REJECTED_MESSAGE
from cairn.authority.gate import Actor as Actor
from cairn.authority.gate import Fetch as _Fetch
from cairn.authority.gate import denial as _denial
from cairn.authority.gate import fetch_from as _fetch_from
from cairn.authority.gate import grants_for_principal as _grants_for_principal
from cairn.authority.gate import instance_denial_draft as _instance_denial_draft
from cairn.authority.gate import instance_id as _instance_id
from cairn.authority.gate import realm_draft as _realm_draft
from cairn.authority.gate import realm_exists as _realm_exists
from cairn.authority.grants import (
    GrantRecord,
    find_authorising_grant,
    is_live,
    is_scope_prefix,
)
from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    AuditValueError,
    Classification,
    ClassificationTransition,
    Outcome,
    Scope,
    ScopeSegment,
    TrustClass,
    TrustTransition,
)
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    FailureDetail,
    MutationOutcome,
    MutationReceipt,
    MutationRejection,
    Rejected,
    RetryClass,
    StableFailure,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.evidence.adapter import AtticAdapter
from cairn.operations.metrics import Metrics
from cairn.projection.adapter import IndexAdapter
from cairn.runtime.logging import SafeLogger
from cairn.screening import (
    POLICY_VERSION,
    SecretFinding,
    SecretScreen,
    audit_reason_code,
    first_finding,
    normalise_for_screening,
)

if TYPE_CHECKING:
    from cairn.authority.retrieval import RetrievalResult, Retrieve

_AUTHORITY_SCHEMA = "cairn.authority/v1"
_ASSERTION_RESULT_SCHEMA = "cairn.authority.assertion/v1"
_PROMOTION_RESULT_SCHEMA = "cairn.authority.promotion/v1"
_INVALIDATION_RESULT_SCHEMA = "cairn.authority.invalidation/v1"
_INGEST = "ingest"
_PROMOTE = "promote"
_PROPOSAL_ACCEPT = "memory-proposal-accept"
_INVALIDATE = "invalidate"


# --- commands and results (closed, frozen; I-66) -----------------------------


@dataclass(frozen=True, slots=True)
class IngestAssertion:
    scope: Scope
    classification: Classification
    source_type: SourceType
    facts: tuple[FactDraft, ...]
    # Defaulted from here down: the field order is the published contract, so
    # the trailing three take their natural defaults rather than being
    # reordered ahead of requested_trust.
    requested_trust: TrustClass = TrustClass.CANDIDATE
    observed_at: datetime | None = None
    metadata: str | None = None
    evidence_payload: bytes | None = None


@dataclass(frozen=True, slots=True)
class AssertionIngested:
    """Content-free receipt: identities only, never a body or a payload."""

    assertion_id: UUID
    fact_ids: tuple[UUID, ...]
    evidence_id: UUID | None


@dataclass(frozen=True, slots=True)
class ExternalEvidenceReference:
    """Evidence held outside Cairn. Both fields are caller-attested: Cairn
    never fetches the URI and never sees the bytes the digest names, so this
    creates an external-custody evidence record and touches Attic not at
    all."""

    external_uri: str
    payload_digest: bytes


@dataclass(frozen=True, slots=True)
class PromoteFacts:
    fact_ids: tuple[UUID, ...]
    evidence: UUID | ExternalEvidenceReference
    target_scope: Scope | None
    target_classification: Classification | None
    reason: str


@dataclass(frozen=True, slots=True)
class FactsPromoted:
    """Content-free receipt. ``promotions`` pairs are in command order, which
    is deliberately *not* the sorted order the audit event's
    ``affected_fact_ids`` uses — the receipt answers "what did each of my
    sources become", the audit event answers "what did this mutation touch"."""

    promotions: tuple[tuple[UUID, UUID], ...]
    evidence_id: UUID


@dataclass(frozen=True, slots=True)
class ProposalAcceptanceContext:
    """Trusted internal composition, never request-selected callbacks.

    The guard adds proposal policy before replay lookup. The record callback
    binds the actual publication to a unique decision in the same transaction;
    it runs only on fresh success. Neither callback replaces normal authority.
    Raise ``MutationRejection`` for an audited refusal and atomic rollback.

    Optional stored-input validation runs under the writer lock before normal
    reauthorisation decodes stored values. It receives the actual actor,
    command, proposal identity and key to reject a mismatched composition.
    It grants no authority;
    normal reauthorisation still precedes the guard. None preserves the
    original seam's ordering and all legacy callers' behaviour.
    """

    proposal_id: UUID
    guard: Callable[[_GuardedTransaction], None]
    record: Callable[[_MutationTransaction, FactsPromoted], None]
    validate_stored: (
        Callable[[_GuardedTransaction, Actor, PromoteFacts, UUID, UUID], None] | None
    ) = None


@dataclass(frozen=True, slots=True)
class InvalidateFacts:
    fact_ids: tuple[UUID, ...]
    reason: str
    superseded_by: UUID | None


@dataclass(frozen=True, slots=True)
class FactsInvalidated:
    """Content-free receipt: identities only, sorted by string, plus the one
    ``invalidated_at`` instant captured for the whole batch."""

    fact_ids: tuple[UUID, ...]
    invalidated_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingEvidence:
    """The evidence record and the outbox work that carries its payload,
    allocated together so the two can never drift apart."""

    record: EvidenceRecord
    work_id: UUID
    payload: bytes
    digest: bytes


class CairnAuthority:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        exact_evidence_enabled: bool,
        screen: SecretScreen,
        retrieval_index_enabled: bool = True,
        index: IndexAdapter | None = None,
        attic: AtticAdapter | None = None,
        metrics: Metrics | None = None,
        logger: SafeLogger | None = None,
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._exact_evidence_enabled = exact_evidence_enabled
        self._screen = screen
        # P-39: with no index configured there is no deliverer, so queueing
        # projection work would grow the outbox without bound in every
        # graphiti-disabled deployment. The catalogue is authoritative and
        # ``rebuild-index`` re-projects from it, so nothing is lost by not
        # queueing. Defaulted true because that is slice 4's accepted
        # behaviour, which every existing caller is entitled to keep.
        self._retrieval_index_enabled = retrieval_index_enabled
        # P-14's rule applied to the index: a disabled retrieval index is
        # represented by absence, not by a null adapter object. The flag
        # above gates outbox writes; the adapter's absence gates retrieve.
        # Composition supplies both from the one graphiti.enabled setting.
        self._index = index
        # P-43's second retrieval modality. Held here and not in the flag
        # above because a mutation still must not touch Attic: P-14 keeps
        # payload-bearing ingest behind ``exact_evidence_enabled`` and the
        # outbox, and this adapter is read-only, used by ``retrieve`` alone.
        self._attic = attic
        # Retrieval's P-47 observability seam only — the mutation commands
        # record outcomes in the audit chain, not in operational metrics.
        self._metrics = metrics
        self._logger = logger

    def _projection_work_ids(self, count: int) -> tuple[UUID, ...]:
        """P-39: no index, no queued work — and no identities minted for
        work that will never exist."""
        if not self._retrieval_index_enabled:
            return ()
        return tuple(self._uuid_factory() for _ in range(count))

    def ingest(
        self,
        actor: Actor,
        command: IngestAssertion,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
        reauthorise_at_commit: bool = False,
        commit_guard: Callable[[_GuardedTransaction], None] | None = None,
    ) -> MutationOutcome[AssertionIngested]:
        """Ingest with an optional internal, transaction-scoped policy guard.

        Supplying ``commit_guard`` also enables fresh normal ingest
        reauthorisation before the guard, for both writes and replays. The
        guard may query the transaction and raise ``MutationRejection``; it
        must not acquire another writer lock or perform external work.
        Omission preserves the existing v1 timing and replay semantics.
        """
        effective_at = self._clock()
        scope = command.scope

        scope_failure = _scope_shape_failure(scope)
        if scope_failure is not None:
            return self._reject_instance(
                actor,
                correlation_id,
                scope_failure,
                action_code=_INGEST,
                fingerprint=_realm_fingerprint(scope.realm),
            )

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if not _realm_exists(fetch, scope.realm):
                return self._reject_unknown_realm(
                    fetch, actor, scope, correlation_id, action_code=_INGEST
                )
            grants = _sorted_grants(fetch, actor.principal_id, scope.realm)
            authorising = _authorisation_failure(command, grants, effective_at)
            if isinstance(authorising, str):
                # On the instance chain, not the realm's, by the same rule
                # promotion and invalidation already follow: a refusal decided
                # before the actor is proven to have standing may not touch a
                # realm chain. ``Rejected`` carries its ``audit_receipt``,
                # whose ``chain_identity`` and ``sequence`` would otherwise
                # hand an unauthorised actor the realm chain's name and its
                # exact length — and appending would advance that sequence,
                # letting it perturb, and meter, a chain it cannot read. The
                # realm chain resumes below, where the grant is settled.
                #
                # All three of _authorisation_failure's reasons come here
                # together, including the two where the actor does hold a
                # covering ingest grant: I-67 makes them one public
                # ``authorisation_denied``, and ``chain_kind`` travels back on
                # the receipt, so splitting them by chain would rebuild in the
                # receipt the very oracle the shared failure code denies.
                #
                # fetch=: the read connection is already open, so the refusal
                # does not open a second.
                return self._reject_instance(
                    actor,
                    correlation_id,
                    authorising,
                    action_code=_INGEST,
                    code=FailureCode.AUTHORISATION_DENIED,
                    message=_AUTHORISATION_DENIED_MESSAGE,
                    fetch=fetch,
                    fingerprint=_realm_fingerprint(scope.realm),
                )

        grant_id = authorising.grant_id
        limit_failure = _limit_failure(command, self._exact_evidence_enabled)
        if limit_failure is not None:
            return self._deny(
                actor,
                scope,
                grant_id,
                correlation_id,
                FailureCode.INVALID_REQUEST,
                _INVALID_REQUEST_MESSAGE,
                limit_failure,
                action_code=_INGEST,
            )

        # Deterministic identity assignment: assertion, then facts in draft
        # order, then evidence, then work rows. The receipt's fact_ids are
        # sorted by string, which is a different order — assignment order and
        # receipt order are not the same thing.
        payload = command.evidence_payload
        try:
            assertion_id = self._uuid_factory()
            fact_ids = tuple(self._uuid_factory() for _ in command.facts)
            assertion, facts = _ingest_records(
                actor, command, effective_at, assertion_id, fact_ids
            )
            evidence = (
                None
                if payload is None
                else _pending_evidence(
                    self._uuid_factory(),
                    self._uuid_factory(),
                    assertion_id,
                    command,
                    payload,
                    effective_at,
                )
            )
            projection_work_ids = self._projection_work_ids(len(command.facts))
        # AuditValueError as well as CustodyValueError: custody delegates scope
        # shape to Scope itself, so its violations arrive under a different
        # type. The command's own scope is already settled by
        # _scope_shape_failure above; this stays because record construction
        # is the shared shape for promotion and invalidation, whose scopes
        # come from stored rows rather than from a validated command.
        except (CustodyValueError, AuditValueError) as error:
            return self._deny(
                actor,
                scope,
                grant_id,
                correlation_id,
                FailureCode.INVALID_REQUEST,
                _INVALID_REQUEST_MESSAGE,
                error.code,
                action_code=_INGEST,
            )

        receipt = AssertionIngested(
            assertion_id=assertion_id,
            fact_ids=tuple(sorted(fact_ids, key=str)),
            evidence_id=None if evidence is None else evidence.record.evidence_id,
        )
        draft = _realm_draft(
            realm_id=scope.realm,
            actor=actor,
            grant_id=grant_id,
            action_kind=ActionKind.DATA,
            action_code=_INGEST,
            requested_scope=scope,
            outcome=Outcome.ALLOW,
            reason_code="assertion_ingested",
            correlation_id=correlation_id,
            affected_assertion_ids=(assertion_id,),
            affected_fact_ids=receipt.fact_ids,
            affected_evidence_ids=(
                () if evidence is None else (evidence.record.evidence_id,)
            ),
            evidence_reference=receipt.evidence_id,
            evidence_digest=None if evidence is None else evidence.digest,
        )

        def mutation(transaction: _MutationTransaction) -> AssertionIngested:
            _revalidate(
                transaction.query,
                actor,
                command,
                authorising,
                effective_at,
                correlation_id,
                action_code=_INGEST,
            )
            # After value validation (I-74) and after the in-transaction
            # revalidation, inside the mutation callback — the placement P-26
            # originally approved, restored by Operator's ruling of 7 August 2026.
            # ``mutate_idempotent`` never runs this callback for a replay or a
            # key conflict, so a committed write's retry is served from the
            # idempotency record rather than re-screened under a later policy,
            # a key conflict is named for what it is, and a principal revoked
            # since the outer check gets ``authorisation_denied`` with no
            # screening verdict — three contracts the pre-gate placement
            # silently dropped. The screened values all sit on the frozen
            # command, so nothing can change under the scan; the cost is the
            # scan running under the writer gate, bounded by I-30's request
            # ceiling at roughly a second.
            finding = _screen_ingest_payload(command, self._screen)
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=scope.realm,
                    actor=actor,
                    grant_id=grant_id,
                    action_code=_INGEST,
                    requested_scope=scope,
                )
            _insert_assertion(transaction, assertion)
            if evidence is not None:
                _insert_evidence(transaction, evidence.record)
                _insert_evidence_outbox(transaction, evidence, effective_at)
            for fact in facts:
                _insert_fact(transaction, fact)
            if projection_work_ids:
                for work_id, fact in zip(projection_work_ids, facts, strict=True):
                    _insert_projection_outbox(
                        transaction,
                        work_id,
                        fact.fact_id,
                        effective_at,
                        kind="fact-ingested",
                    )
            return receipt

        def reauthorise(transaction: _GuardedTransaction) -> None:
            _revalidate(
                transaction.query,
                actor,
                command,
                authorising,
                self._clock(),
                correlation_id,
                action_code=_INGEST,
            )
            if commit_guard is not None:
                commit_guard(transaction)

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation=_INGEST,
            idempotency_key=idempotency_key,
            command_digest=_ingest_digest(command),
            result_schema=_ASSERTION_RESULT_SCHEMA,
            mutation=mutation,
            encode=_encode_assertion_ingested,
            decode=_decode_assertion_ingested,
            restate_identities=_restate_assertion_identities,
            reauthorise=reauthorise
            if reauthorise_at_commit or commit_guard is not None
            else None,
        )

    def promote(
        self,
        actor: Actor,
        command: PromoteFacts,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
        acceptance: ProposalAcceptanceContext | None = None,
    ) -> MutationOutcome[FactsPromoted]:
        """Derive a validated fact from each source, with evidence provenance.

        Nothing about a source is edited: promotion writes new rows and leaves
        the originals byte-identical, because rows are immutable and a failed
        approach preserved as a failure is the point of the trust vocabulary.

        The check order is deliberate and pinned by tests. Structural failures
        precede authorisation failures throughout — a descendant target is
        refused as ``target_not_ancestor`` whether or not the actor holds
        promote there, and a classification lowering as ``classification_
        lowered`` whether or not they could write the class they asked for.
        That is what makes the public outcomes deterministic rather than a
        function of the actor's grants. It does not invert the frozen
        authorise-then-validate pipeline: that order governs the coarse
        phases, and the target simply cannot be authorised before it has been
        resolved, which requires reading the sources first.
        """
        if acceptance is not None and (
            not isinstance(acceptance, ProposalAcceptanceContext)
            or not isinstance(acceptance.proposal_id, UUID)
            or not callable(acceptance.guard)
            or not callable(acceptance.record)
        ):
            raise ValueError("invalid proposal acceptance context")
        effective_at = self._clock()

        # Caller-supplied scope shape settled before the scope is used
        # anywhere, including in the event recording its own refusal.
        if command.target_scope is not None:
            shape_failure = _scope_shape_failure(command.target_scope)
            if shape_failure is not None:
                return self._reject_instance(
                    actor,
                    correlation_id,
                    shape_failure,
                    action_code=_PROMOTE,
                    fingerprint=_promote_fingerprint(command),
                )
        limit_failure = _promote_limit_failure(command)
        if limit_failure is not None:
            return self._reject_instance(
                actor,
                correlation_id,
                limit_failure,
                action_code=_PROMOTE,
                fingerprint=_promote_fingerprint(command),
            )

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            grants = _GrantCache(fetch, actor.principal_id)
            # From here to the homogeneity check every refusal is filed off
            # the realm chain: see _reject_instance for why I-67 leaves no
            # alternative.
            try:
                sources = _load_sources(fetch, command.fact_ids)
                visibility = _source_visibility(
                    command.fact_ids, sources, grants, effective_at
                )
            except (CustodyValueError, AuditValueError) as error:
                # A stored scope column the schema accepts but the value layer
                # refuses. Promotion is the first command to build records
                # from stored rows rather than caller input, so this is the
                # first place that can happen.
                # fetch= throughout this block: the read connection is already
                # open, and _reject_instance takes one precisely so a refusal
                # decided here does not open a second.
                return self._reject_instance(
                    actor,
                    correlation_id,
                    error.code,
                    action_code=_PROMOTE,
                    fetch=fetch,
                    fingerprint=_promote_fingerprint(command),
                )
            if isinstance(visibility, str):
                return self._reject_instance(
                    actor,
                    correlation_id,
                    visibility,
                    action_code=_PROMOTE,
                    code=FailureCode.AUTHORISATION_DENIED,
                    message=_AUTHORISATION_DENIED_MESSAGE,
                    fetch=fetch,
                    fingerprint=_promote_fingerprint(command),
                )
            if _heterogeneous_batch(command.fact_ids, sources):
                # A heterogeneous batch may span two realms, so there is no
                # single realm the event could honestly name.
                return self._reject_instance(
                    actor,
                    correlation_id,
                    "heterogeneous_batch",
                    action_code=_PROMOTE,
                    fetch=fetch,
                    fingerprint=_promote_fingerprint(command),
                )

            # Every source is now proven visible and shares one scope, one
            # classification and one trust class, so naming the realm
            # discloses nothing the actor could not already learn.
            first = sources[command.fact_ids[0]]
            source_scope = first.scope
            retrieve = visibility

            lifecycle_failure = _lifecycle_failure(command.fact_ids, sources)
            if lifecycle_failure is not None:
                return self._deny_promotion(
                    actor, source_scope, correlation_id, lifecycle_failure
                )

            target_scope = (
                source_scope if command.target_scope is None else command.target_scope
            )
            target_classification = (
                first.classification
                if command.target_classification is None
                else command.target_classification
            )
            structural_failure = _target_shape_failure(
                source_scope, first.classification, target_scope, target_classification
            )
            if structural_failure is not None:
                return self._deny_promotion(
                    actor, source_scope, correlation_id, structural_failure
                )
            promote_grant = _target_authorisation(
                grants.for_realm(target_scope.realm),
                target_scope,
                target_classification,
                effective_at,
            )
            if isinstance(promote_grant, str):
                return self._deny_promotion(
                    actor,
                    source_scope,
                    correlation_id,
                    promote_grant,
                    code=FailureCode.AUTHORISATION_DENIED,
                    message=_AUTHORISATION_DENIED_MESSAGE,
                )

            located: _PromotionEvidence | None = None
            if isinstance(command.evidence, UUID):
                try:
                    found = _named_evidence(
                        fetch, command.evidence, source_scope, retrieve
                    )
                except (CustodyValueError, AuditValueError) as error:
                    return self._deny_promotion(
                        actor, source_scope, correlation_id, error.code
                    )
                if isinstance(found, str):
                    return self._deny_promotion(
                        actor,
                        source_scope,
                        correlation_id,
                        found,
                        code=FailureCode.AUTHORISATION_DENIED,
                        message=_AUTHORISATION_DENIED_MESSAGE,
                    )
                located = found

        # Deterministic identity assignment: the evidence record when the
        # command carries one inline, then derived facts in command order,
        # then projection work rows.
        try:
            evidence = (
                located
                if located is not None
                else _external_evidence(
                    self._uuid_factory(),
                    cast(ExternalEvidenceReference, command.evidence),
                    target_scope,
                    target_classification,
                    effective_at,
                )
            )
            derived_ids = tuple(self._uuid_factory() for _ in command.fact_ids)
            derived = tuple(
                _promoted_fact(
                    derived_id,
                    sources[fact_id],
                    actor,
                    target_scope,
                    target_classification,
                    evidence.evidence_id,
                    effective_at,
                )
                for derived_id, fact_id in zip(
                    derived_ids, command.fact_ids, strict=True
                )
            )
            projection_work_ids = self._projection_work_ids(len(command.fact_ids))
        except (CustodyValueError, AuditValueError) as error:
            return self._deny_promotion(actor, source_scope, correlation_id, error.code)

        receipt = FactsPromoted(
            promotions=tuple(zip(command.fact_ids, derived_ids, strict=True)),
            evidence_id=evidence.evidence_id,
        )
        draft = _realm_draft(
            realm_id=source_scope.realm,
            actor=actor,
            grant_id=promote_grant.grant_id,
            action_kind=ActionKind.DATA,
            action_code=_PROMOTE,
            requested_scope=None,
            outcome=Outcome.ALLOW,
            reason_code="facts_promoted",
            correlation_id=correlation_id,
            # The derived facts only, sorted. Promotion writes no source row:
            # facts are immutable, and the mutation below inserts derived
            # facts and their projection work and touches nothing else. A
            # source is read and cited, not changed, so listing it here would
            # make affected_* mean "named by the command" — at which point it
            # stops distinguishing what an operator must re-examine after the
            # event from what merely appeared in it. The source-to-derived
            # pairing is not lost: it is the mutation result's own content
            # (``FactsPromoted.promotions``), and ``source_scope`` and
            # ``evidence_reference`` are on this event already. Sorted, so
            # the ordering still differs from the receipt's command-order
            # pairs, on purpose.
            affected_fact_ids=tuple(sorted(derived_ids, key=str)),
            affected_evidence_ids=(evidence.evidence_id,),
            evidence_reference=evidence.evidence_id,
            evidence_digest=evidence.digest,
            source_scope=source_scope,
            target_scope=target_scope,
            classification_transition=ClassificationTransition(
                previous=first.classification, current=target_classification
            ),
            trust_transition=TrustTransition(
                previous=first.trust, current=TrustClass.VALIDATED
            ),
        )

        locked_at = effective_at

        def reauthorise(transaction: _GuardedTransaction) -> None:
            nonlocal locked_at
            assert acceptance is not None
            if acceptance.validate_stored is not None:
                acceptance.validate_stored(
                    transaction, actor, command, acceptance.proposal_id, idempotency_key
                )
            # Validation may take time; grants must be current afterwards.
            locked_at = self._clock()
            _reauthorise_proposal_promotion(
                transaction.query,
                actor,
                command,
                sources,
                source_scope,
                target_scope,
                target_classification,
                retrieve,
                promote_grant,
                locked_at,
                correlation_id,
            )
            acceptance.guard(transaction)

        def mutation(transaction: _MutationTransaction) -> FactsPromoted:
            _revalidate_promotion(
                transaction.query,
                actor,
                command.fact_ids,
                source_scope,
                target_scope,
                retrieve,
                promote_grant,
                locked_at,
                correlation_id,
            )
            # See ingest for the in-callback placement. The grant is named
            # here where ``_deny_promotion`` leaves it ``None``: the promote
            # grant is settled by this point and is the one the eventual allow
            # event would carry, so there is no second, differently-scoped
            # grant to confuse it with.
            finding = first_finding(self._screen, _promote_screened_fields(command))
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=source_scope.realm,
                    actor=actor,
                    grant_id=promote_grant.grant_id,
                    action_code=_PROMOTE,
                    requested_scope=source_scope,
                )
            if evidence.record is not None:
                _insert_evidence(transaction, evidence.record)
            for fact in derived:
                _insert_fact(transaction, fact)
            if projection_work_ids:
                for work_id, fact in zip(projection_work_ids, derived, strict=True):
                    _insert_projection_outbox(
                        transaction,
                        work_id,
                        fact.fact_id,
                        effective_at,
                        kind="fact-promoted",
                    )
            if acceptance is not None:
                acceptance.record(transaction, receipt)
            return receipt

        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation=_PROMOTE if acceptance is None else _PROPOSAL_ACCEPT,
            idempotency_key=idempotency_key,
            command_digest=(
                _promote_digest(command)
                if acceptance is None
                else _proposal_accept_digest(command, acceptance.proposal_id)
            ),
            result_schema=_PROMOTION_RESULT_SCHEMA,
            mutation=mutation,
            encode=_encode_facts_promoted,
            decode=_decode_facts_promoted,
            restate_identities=_restate_promotion_identities,
            reauthorise=None if acceptance is None else reauthorise,
        )

    def invalidate(
        self,
        actor: Actor,
        command: InvalidateFacts,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
        reauthorise_at_commit: bool = False,
        expected_scope: Scope | None = None,
    ) -> MutationOutcome[FactsInvalidated]:
        """End each named fact's belief validity by appending one immutable
        ``fact_invalidations`` row per fact. Rows are never edited; belief
        liveness is derived from the presence of the row, never stored.

        Batch shape is settled first, exactly as for promotion: it is pure
        command shape, needs no read connection, and its refusals go on the
        instance chain unconditionally.

        Unlike promotion, invalidation has only one relevant grant operation
        — there is no separate ``retrieve`` to prove visibility ahead of it.
        So the ``invalidate`` grant has to do that job as well as its own: it
        is checked per fact, against that fact's own stored scope, *before*
        homogeneity is evaluated. Checking it once against the batch's shared
        scope only after homogeneity would leak, via ``heterogeneous_batch``
        versus ``invalidate_grant_not_held``, that identities named by an
        actor with no standing at all nonetheless exist and span more than
        one scope — the same class of leak I-67 forbids for ``fact_unknown``.
        """
        effective_at = self._clock()
        limit_failure = _invalidate_limit_failure(command)
        if limit_failure is not None:
            return self._reject_instance(
                actor,
                correlation_id,
                limit_failure,
                action_code=_INVALIDATE,
                fingerprint=_fact_batch_fingerprint(command.fact_ids),
            )

        if expected_scope is not None and (
            type(expected_scope) is not Scope
            or _scope_shape_failure(expected_scope) is not None
            or any(
                type(identity) is not UUID or identity.version != 4
                for identity in command.fact_ids
            )
            or (
                command.superseded_by is not None
                and (
                    type(command.superseded_by) is not UUID
                    or command.superseded_by.version != 4
                )
            )
        ):
            return self._reject_instance(
                actor, correlation_id, "invalid_scope", action_code=_INVALIDATE
            )

        with read_connection(self._data_path) as connection:
            fetch = _fetch_from(connection)
            if expected_scope is not None and not _scoped_invalidation_allowed(
                fetch, actor, command, expected_scope, effective_at
            ):
                return self._reject_instance(
                    actor,
                    correlation_id,
                    "scoped_correction_denied",
                    action_code=_INVALIDATE,
                    code=FailureCode.AUTHORISATION_DENIED,
                    message=_AUTHORISATION_DENIED_MESSAGE,
                    fetch=fetch,
                )
            grants = _GrantCache(fetch, actor.principal_id)
            try:
                sources = _load_sources(fetch, command.fact_ids)
                visibility = _invalidate_visibility(
                    command.fact_ids, sources, grants, effective_at
                )
            except (CustodyValueError, AuditValueError) as error:
                return self._reject_instance(
                    actor,
                    correlation_id,
                    error.code,
                    action_code=_INVALIDATE,
                    fetch=fetch,
                    fingerprint=_fact_batch_fingerprint(command.fact_ids),
                )
            if isinstance(visibility, str):
                return self._reject_instance(
                    actor,
                    correlation_id,
                    visibility,
                    action_code=_INVALIDATE,
                    code=FailureCode.AUTHORISATION_DENIED,
                    message=_AUTHORISATION_DENIED_MESSAGE,
                    fetch=fetch,
                    fingerprint=_fact_batch_fingerprint(command.fact_ids),
                )
            if _heterogeneous_scope(command.fact_ids, sources):
                # P-13's invalidate binding is narrower than promotion's: the
                # batch shares one stored scope, not necessarily one
                # classification or trust class. May span two realms, so
                # there is no single realm the event could honestly name.
                return self._reject_instance(
                    actor,
                    correlation_id,
                    "heterogeneous_batch",
                    action_code=_INVALIDATE,
                    fetch=fetch,
                    fingerprint=_fact_batch_fingerprint(command.fact_ids),
                )

            # Every fact is now proven to exist, to be covered by a live
            # invalidate grant, and to share one stored scope — so naming the
            # realm discloses nothing the actor could not already learn.
            shared_scope = sources[command.fact_ids[0]].scope
            invalidate_grant = visibility

            if command.superseded_by is not None:
                superseded_failure = _superseded_by_failure(
                    fetch, command.superseded_by, shared_scope.realm
                )
                if superseded_failure is not None:
                    return self._deny_invalidate(
                        actor,
                        shared_scope,
                        invalidate_grant.grant_id,
                        correlation_id,
                        superseded_failure,
                    )

        # Deterministic identity assignment: one InvalidationRecord per fact
        # in command order, then one projection work row per fact — no new
        # fact_invalidations identity is minted, since fact_id is the row's
        # own primary key and it is always caller-supplied.
        try:
            records = tuple(
                _invalidation_record(fact_id, actor, command, effective_at)
                for fact_id in command.fact_ids
            )
            projection_work_ids = self._projection_work_ids(len(command.fact_ids))
        except CustodyValueError as error:
            return self._deny_invalidate(
                actor,
                shared_scope,
                invalidate_grant.grant_id,
                correlation_id,
                error.code,
            )

        receipt = FactsInvalidated(
            fact_ids=tuple(sorted(command.fact_ids, key=str)),
            invalidated_at=effective_at,
        )
        draft = _realm_draft(
            realm_id=shared_scope.realm,
            actor=actor,
            grant_id=invalidate_grant.grant_id,
            action_kind=ActionKind.DATA,
            action_code=_INVALIDATE,
            requested_scope=shared_scope,
            outcome=Outcome.ALLOW,
            reason_code="facts_invalidated",
            correlation_id=correlation_id,
            affected_fact_ids=receipt.fact_ids,
            # P-16: invalidate carries neither evidence field. Left absent.
        )

        def mutation(transaction: _MutationTransaction) -> FactsInvalidated:
            _revalidate_invalidate(
                transaction.query,
                actor,
                command.fact_ids,
                shared_scope,
                invalidate_grant,
                effective_at,
                correlation_id,
            )
            # See ingest for the in-callback placement.
            finding = first_finding(self._screen, (("reason", command.reason),))
            if finding is not None:
                raise _secret_denial(
                    finding,
                    correlation_id,
                    realm_id=shared_scope.realm,
                    actor=actor,
                    grant_id=invalidate_grant.grant_id,
                    action_code=_INVALIDATE,
                    requested_scope=shared_scope,
                )
            for record in records:
                _insert_invalidation(transaction, record)
            if projection_work_ids:
                for work_id, fact_id in zip(
                    projection_work_ids, command.fact_ids, strict=True
                ):
                    _insert_projection_outbox(
                        transaction,
                        work_id,
                        fact_id,
                        effective_at,
                        kind="fact-invalidated",
                    )
            return receipt

        def reauthorise(transaction: _GuardedTransaction) -> None:
            now = self._clock()
            if expected_scope is not None and not _scoped_invalidation_allowed(
                transaction.query, actor, command, expected_scope, now
            ):
                raise MutationRejection(
                    StableFailure(
                        FailureCode.AUTHORISATION_DENIED,
                        _AUTHORISATION_DENIED_MESSAGE,
                        correlation_id,
                        RetryClass.NEVER,
                    ),
                    _instance_denial_draft(
                        _instance_id(transaction.query),
                        actor,
                        _INVALIDATE,
                        "scoped_correction_denied",
                        correlation_id,
                        action_kind=ActionKind.DATA,
                    ),
                )
            _revalidate_invalidate(
                transaction.query,
                actor,
                command.fact_ids,
                shared_scope,
                invalidate_grant,
                now,
                correlation_id,
                check_fact_state=False,
            )

        # No restate_identities: unlike ingest and promotion, invalidation
        # mints no identity of its own. fact_invalidations.fact_id is the
        # row's primary key and is always the caller-supplied identity from
        # command.fact_ids, which survives a replay unchanged — the draft
        # above is already correct on the replay path without rewriting.
        return self._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation=_INVALIDATE,
            idempotency_key=idempotency_key,
            command_digest=_invalidate_digest(command),
            result_schema=_INVALIDATION_RESULT_SCHEMA,
            mutation=mutation,
            encode=_encode_facts_invalidated,
            decode=_decode_facts_invalidated,
            reauthorise=reauthorise
            if reauthorise_at_commit or expected_scope is not None
            else None,
        )

    def retrieve(
        self,
        actor: Actor,
        command: "Retrieve",
        *,
        correlation_id: UUID,
    ) -> "RetrievalResult | Rejected":
        """The fourth authority operation (I-77): reconciled retrieval.

        A read, so no idempotency key and no writer gate — supplying a key
        is the transport layer's ``invalid_request``, per the read-route
        rule. The pipeline lives in ``cairn.authority.retrieval``.
        """
        # Imported at call time: retrieval imports this module's stored-row
        # readers, so a top-level import here would be a cycle.
        from cairn.authority import retrieval

        return retrieval.retrieve(
            self._data_path,
            self._transactions,
            actor,
            command,
            index=self._index,
            screen=self._screen,
            correlation_id=correlation_id,
            clock=self._clock,
            attic=self._attic,
            metrics=self._metrics,
            logger=self._logger,
        )

    # --- outer-gate rejection helpers (bypass mutate_idempotent entirely) --

    def _reject(
        self,
        draft: AuditDraft,
        code: FailureCode,
        message: str,
        correlation_id: UUID,
    ) -> Rejected:
        failure = StableFailure(
            code=code,
            safe_message=message,
            correlation_id=correlation_id,
            retry=RetryClass.NEVER,
        )
        return self._transactions.reject(draft, failure)

    def _reject_instance(
        self,
        actor: Actor,
        correlation_id: UUID,
        reason_code: str,
        *,
        action_code: str,
        code: FailureCode = FailureCode.INVALID_REQUEST,
        message: str = _INVALID_REQUEST_MESSAGE,
        fetch: _Fetch | None = None,
        fingerprint: bytes | None = None,
    ) -> Rejected:
        """A refusal with no realm chain to record it on.

        One rule reaches here: no refusal decided before the actor is proven
        to have standing may be recorded on a realm chain. A request whose
        shape was rejected before a realm was consulted; an unknown realm; an
        ingest whose actor holds no grant covering the scope; and — for
        promotion and invalidation — every refusal decided before the named
        facts are proven visible. The last is not a convenience.
        ``PromoteFacts`` names its sources by identity and carries no realm,
        so an unknown identity has no realm chain by construction, while a
        source outside the actor's authority has one that must not be
        disclosed. I-67 requires the two to be publicly
        indistinguishable, and ``Rejected`` carries its ``audit_receipt``,
        chain and all — so the instance chain is the only chain the pair can
        share.

        ``fingerprint`` keeps such an event correlatable without disclosing
        what it was about, which is the trade the audit record should make
        when the alternative is telling a realm's auditors nothing at all.
        """
        if fetch is None:
            with read_connection(self._data_path) as connection:
                identity = _instance_id(_fetch_from(connection))
        else:
            identity = _instance_id(fetch)
        draft = _instance_denial_draft(
            identity,
            actor,
            action_code,
            reason_code,
            correlation_id,
            action_kind=ActionKind.DATA,
            safe_request_fingerprint=fingerprint,
        )
        return self._reject(draft, code, message, correlation_id)

    def _reject_unknown_realm(
        self,
        fetch: _Fetch,
        actor: Actor,
        scope: Scope,
        correlation_id: UUID,
        *,
        action_code: str,
    ) -> Rejected:
        return self._reject_instance(
            actor,
            correlation_id,
            "realm_not_found",
            action_code=action_code,
            code=FailureCode.NOT_FOUND,
            message=_NOT_FOUND_MESSAGE,
            fetch=fetch,
            fingerprint=_realm_fingerprint(scope.realm),
        )

    def _deny_promotion(
        self,
        actor: Actor,
        source_scope: Scope,
        correlation_id: UUID,
        reason_code: str,
        *,
        code: FailureCode = FailureCode.INVALID_REQUEST,
        message: str = _INVALID_REQUEST_MESSAGE,
    ) -> Rejected:
        """A promote refusal on the source realm chain, reached only once
        every source is proven visible.

        ``grant_id`` stays ``None`` throughout: the promote grant is not known
        for the structural refusals, and naming the retrieve grant on a
        refusal about the target would be misleading.
        """
        return self._deny(
            actor,
            source_scope,
            None,
            correlation_id,
            code,
            message,
            reason_code,
            action_code=_PROMOTE,
        )

    def _deny_invalidate(
        self,
        actor: Actor,
        shared_scope: Scope,
        grant_id: UUID,
        correlation_id: UUID,
        reason_code: str,
        *,
        code: FailureCode = FailureCode.AUTHORISATION_DENIED,
        message: str = _AUTHORISATION_DENIED_MESSAGE,
    ) -> Rejected:
        """An invalidate refusal on the shared scope's realm chain, reached
        only once every fact is proven visible, invalidate-authorised and
        homogeneous.

        Unlike ``_deny_promotion``, ``grant_id`` is named: invalidation has
        only one relevant grant, it is already settled by the time this is
        called, and it is the same grant the eventual allow event would
        record — there is no second, differently-scoped grant it could be
        confused with.
        """
        return self._deny(
            actor,
            shared_scope,
            grant_id,
            correlation_id,
            code,
            message,
            reason_code,
            action_code=_INVALIDATE,
        )

    def _deny(
        self,
        actor: Actor,
        scope: Scope,
        grant_id: UUID | None,
        correlation_id: UUID,
        code: FailureCode,
        message: str,
        reason_code: str,
        *,
        action_code: str,
    ) -> Rejected:
        draft = _realm_draft(
            realm_id=scope.realm,
            actor=actor,
            grant_id=grant_id,
            action_kind=ActionKind.DATA,
            action_code=action_code,
            requested_scope=scope,
            outcome=Outcome.DENY,
            reason_code=reason_code,
            correlation_id=correlation_id,
        )
        return self._reject(draft, code, message, correlation_id)


# --- the custody secret denial (I-74, P-26) ----------------------------------


def _secret_denial(
    finding: SecretFinding,
    correlation_id: UUID,
    *,
    realm_id: str,
    actor: Actor,
    grant_id: UUID | None,
    action_code: str,
    requested_scope: Scope,
) -> MutationRejection:
    """A custody-screen refusal, raised from inside the mutation callback.

    The event's ``reason_code`` names the rule and nothing else; the field
    path travels back to the caller in ``detail`` and never into the chain,
    per P-26. ``requested_scope`` is recorded as usual, unlike a boundary
    denial, because the scope is not the suspect content here — the boundary
    screen has already cleared it.
    """
    return _denial(
        FailureCode.SECRET_REJECTED,
        _SECRET_REJECTED_MESSAGE,
        correlation_id,
        realm_id=realm_id,
        actor=actor,
        grant_id=grant_id,
        action_kind=ActionKind.DATA,
        action_code=action_code,
        requested_scope=requested_scope,
        reason_code=audit_reason_code(finding.rule),
        detail=FailureDetail(
            policy=POLICY_VERSION,
            rule=finding.rule,
            field_path=finding.field_path,
        ),
    )


# --- request shape -----------------------------------------------------------


def _scope_shape_failure(scope: Scope) -> str | None:
    """Re-runs ``Scope``'s own validation over the command's scope before that
    scope is used anywhere.

    Everything downstream assumes a well-formed ``Scope`` — including the
    audit scope index, which stores one row per segment and bounds
    ``segment_kind``/``segment_id`` in SQL. A scope that reached the command
    with its construction-time validation bypassed would otherwise blow up
    while *recording its own denial*, turning a typed refusal into a raw
    constraint violation: exactly the failure mode durable denials exist to
    prevent. So the shape is settled first, and its rejection goes on the
    instance chain, which carries no scope at all.

    Both loops are needed. ``Scope`` validates the realm, the tuple type, the
    length bound and that every member *is* a ``ScopeSegment`` — it does not
    look inside one, because segment content is ``ScopeSegment``'s own
    invariant and a segment mutated after construction has already bypassed
    it. Reconstructing each segment is what re-establishes that. The
    ``Scope`` call must come first: it is what makes the attribute reads
    below safe from ``AttributeError``.
    """
    try:
        Scope(realm=scope.realm, segments=scope.segments)
        for segment in scope.segments:
            ScopeSegment(kind=segment.kind, identifier=segment.identifier)
    except AuditValueError as error:
        return error.code
    return None


# --- authorisation -----------------------------------------------------------


def _sorted_grants(
    fetch: _Fetch, principal_id: UUID, realm_id: str
) -> tuple[GrantRecord, ...]:
    # grants_for_principal carries no ORDER BY, so the outer and in-transaction
    # evaluations must impose one themselves or they could pick different
    # authorising grants from identical state.
    return tuple(
        sorted(
            _grants_for_principal(fetch, principal_id, realm_id),
            key=lambda grant: str(grant.grant_id),
        )
    )


def _authorisation_failure(
    command: IngestAssertion,
    grants: Sequence[GrantRecord],
    effective_at: datetime,
) -> GrantRecord | str:
    """The authorising ingest grant, or the precise audit reason it was
    refused. I-67: every reason here surfaces as the same public
    ``authorisation_denied``."""
    scope = command.scope
    authorising = find_authorising_grant(
        grants,
        realm_id=scope.realm,
        segments=scope.segments,
        operation=GrantOperation.INGEST,
        at=effective_at,
    )
    if authorising is None:
        return "ingest_grant_not_held"
    if command.classification not in authorising.write_classifications:
        return "classification_not_writable"
    if command.requested_trust is TrustClass.VALIDATED and (
        find_authorising_grant(
            grants,
            realm_id=scope.realm,
            segments=scope.segments,
            operation=GrantOperation.PROMOTE,
            at=effective_at,
        )
        is None
    ):
        return "validated_requires_promote"
    return authorising


def _reauthorise(
    current: GrantRecord | str | None,
    authorising: GrantRecord,
    changed_reason: str,
) -> str | None:
    """``None`` when the same grant still authorises, else the audit reason.

    Fails closed on any change: a *different* authorising grant is refused as
    well as none at all, because the audit event about to be appended already
    names the grant the outer gate found, and an event naming a grant that no
    longer authorises would be a false record. Each operation's reason
    vocabulary is closed, so the swap and the outright revocation deliberately
    report the same ``changed_reason``. A spurious denial here is safe to
    retry — denials are never cached as idempotency results.

    Shared by every command's in-transaction re-evaluation so the rule is
    stated once: ``current`` arrives as a grant, as an already-decided reason
    (ingest, whose re-evaluation folds classification and trust checks in), or
    as ``None`` (promotion, which re-derives one grant at a time).
    """
    if current is None:
        return changed_reason
    if isinstance(current, str):
        return current
    if current.grant_id != authorising.grant_id:
        return changed_reason
    return None


def _revalidate(
    fetch: _Fetch,
    actor: Actor,
    command: IngestAssertion,
    authorising: GrantRecord,
    effective_at: datetime,
    correlation_id: UUID,
    *,
    action_code: str,
) -> None:
    """The authoritative authorisation evaluation for ingest, under the write
    lock. See ``_reauthorise`` for why it fails closed on a swapped grant.

    The grant re-derivation is wrapped against a hostile stored grant row —
    see ``_revalidate_invalidate`` for why this in-transaction window needs
    the same guard the outer gate has via ``gate._row_to_grant``'s typed
    readers, closing it uniformly across all three commands rather than
    leaving it asymmetric.
    """
    try:
        grants = _sorted_grants(fetch, actor.principal_id, command.scope.realm)
    except AuditValueError as error:
        raise _denial(
            FailureCode.INVALID_REQUEST,
            _INVALID_REQUEST_MESSAGE,
            correlation_id,
            realm_id=command.scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=action_code,
            requested_scope=command.scope,
            reason_code=error.code,
        ) from error
    reason = _reauthorise(
        _authorisation_failure(command, grants, effective_at),
        authorising,
        "ingest_grant_not_held",
    )
    if reason is None:
        return
    raise _denial(
        FailureCode.AUTHORISATION_DENIED,
        _AUTHORISATION_DENIED_MESSAGE,
        correlation_id,
        realm_id=command.scope.realm,
        actor=actor,
        grant_id=None,
        action_kind=ActionKind.DATA,
        action_code=action_code,
        requested_scope=command.scope,
        reason_code=reason,
    )


# --- stored custody rows -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SourceFact:
    """A fact as stored, rebuilt for promotion to derive from."""

    fact_id: UUID
    scope: Scope
    body: str
    trust: TrustClass
    classification: Classification
    valid_from: datetime | None
    valid_to: datetime | None
    # No invalidation flag: it would be read before the writer gate is taken
    # and so could only ever be stale. _revalidate_promotion reads it inside
    # the transaction, which is the only place the answer is authoritative.


def _stored_scope(realm_id: str, scope_segments: str) -> Scope:
    """The single reader of a ``scope_segments`` column, mirroring
    ``_segments_column`` as its single writer.

    Migration 0003 constrains these columns to minified JSON arrays and
    nothing more: it cannot express that each element is an object with
    exactly ``kind`` and ``id``, nor that their contents are well-formed. So
    a row the schema accepts can still carry ``[{"kind": 7, "id": 1}]``, and
    reconstructing ``ScopeSegment`` is what refuses it — as a typed
    ``AuditValueError`` carrying a code, rather than a ``TypeError`` from
    inside ``re`` or a raw ``sqlite3.IntegrityError`` raised while recording
    the very denial that was supposed to contain it.
    """
    documents = json.loads(scope_segments)
    if type(documents) is not list:
        raise AuditValueError("invalid_scope")
    segments: list[ScopeSegment] = []
    for document in documents:
        if type(document) is not dict or set(document) != {"kind", "id"}:
            raise AuditValueError("invalid_scope")
        segments.append(ScopeSegment(kind=document["kind"], identifier=document["id"]))
    return Scope(realm=realm_id, segments=tuple(segments))


def _stored_timestamp(value: str) -> datetime:
    """The single reader of a stored custody timestamp, and the companion to
    ``_stored_scope``.

    ``ck_facts_valid_from`` and its siblings pin the *shape* — 27 characters
    matching the canonical GLOB — but nothing checks that the instant exists,
    so ``2026-13-45T99:99:99.000000Z`` is a row SQLite accepts.
    ``parse_timestamp`` answers that with ``CatalogueStorageError``, which is
    neither a ``CustodyValueError`` nor an ``AuditValueError`` and so would
    escape the command layer's typed-denial handling entirely: a raw exception
    to the caller and no durable event, which is the failure mode durable
    denials exist to prevent. Converting here keeps the caller's ``except``
    tuple the single place that decides what a bad stored value means, and
    reuses the storage layer's own code as the audit reason rather than
    inventing vocabulary.
    """
    try:
        return parse_timestamp(value)
    except CatalogueStorageError as error:
        raise CustodyValueError(error.code) from error


def _load_sources(fetch: _Fetch, fact_ids: tuple[UUID, ...]) -> dict[UUID, _SourceFact]:
    # Only the scope and the two timestamps are converted through guards. The
    # rest are backed by migration 0003 CHECK constraints strong enough to make
    # the conversion total: fact_id is pinned to the UUID4 GLOB, and trust and
    # classification to their exact enum spellings. scope_segments and the
    # timestamps are different in kind — their CHECKs constrain shape without
    # constraining meaning, so those are the two that need reading guards.
    placeholders = ",".join("?" * len(fact_ids))
    rows = fetch(
        "SELECT fact_id, realm_id, scope_segments, body, trust, classification, "
        f"valid_from, valid_to FROM facts WHERE fact_id IN ({placeholders})",
        [str(fact_id) for fact_id in fact_ids],
    )
    sources: dict[UUID, _SourceFact] = {}
    for row in rows:
        (
            fact_id,
            realm_id,
            scope_segments,
            body,
            trust,
            classification,
            valid_from,
            valid_to,
        ) = cast(tuple[str, str, str, str, str, str, str | None, str | None], row)
        sources[UUID(fact_id)] = _SourceFact(
            fact_id=UUID(fact_id),
            scope=_stored_scope(realm_id, scope_segments),
            body=body,
            trust=TrustClass(trust),
            classification=Classification(classification),
            valid_from=None if valid_from is None else _stored_timestamp(valid_from),
            valid_to=None if valid_to is None else _stored_timestamp(valid_to),
        )
    return sources


class _GrantCache:
    """One principal's grants, per realm, read once.

    A promotion's sources may name several realms before homogeneity settles
    the question, and every evaluation of the same state must pick the same
    authorising grant — so the ordering ``_sorted_grants`` imposes has to be
    shared rather than re-derived per lookup.
    """

    def __init__(self, fetch: _Fetch, principal_id: UUID) -> None:
        self._fetch = fetch
        self._principal_id = principal_id
        self._by_realm: dict[str, tuple[GrantRecord, ...]] = {}

    def for_realm(self, realm_id: str) -> tuple[GrantRecord, ...]:
        if realm_id not in self._by_realm:
            self._by_realm[realm_id] = _sorted_grants(
                self._fetch, self._principal_id, realm_id
            )
        return self._by_realm[realm_id]


# --- promotion authorisation -------------------------------------------------


def _source_visibility(
    fact_ids: tuple[UUID, ...],
    sources: dict[UUID, _SourceFact],
    grants: _GrantCache,
    effective_at: datetime,
) -> GrantRecord | str:
    """The ``retrieve`` grant that makes every source visible, or the audit
    reason the batch is refused.

    I-67: an unknown identity and an identity outside the actor's authority
    are publicly identical and differ only here, in the reason.
    """
    retrieve: GrantRecord | None = None
    for fact_id in fact_ids:
        source = sources.get(fact_id)
        if source is None:
            return "fact_unknown"
        retrieve = find_authorising_grant(
            grants.for_realm(source.scope.realm),
            realm_id=source.scope.realm,
            segments=source.scope.segments,
            operation=GrantOperation.RETRIEVE,
            at=effective_at,
        )
        if retrieve is None:
            return "source_retrieve_denied"
        if (
            CLEARANCE_ORDER[source.classification]
            > CLEARANCE_ORDER[retrieve.read_clearance]
        ):
            return "source_clearance_exceeded"
    # fact_ids is non-empty: _promote_limit_failure has already run.
    assert retrieve is not None
    return retrieve


def _heterogeneous_scope(
    fact_ids: tuple[UUID, ...], sources: dict[UUID, _SourceFact]
) -> bool:
    """P-13's scope binding, shared by promotion and invalidation.
    Invalidation binds only this dimension; promotion widens it with
    classification and trust in ``_heterogeneous_batch`` below."""
    first_scope = sources[fact_ids[0]].scope
    return any(sources[fact_id].scope != first_scope for fact_id in fact_ids[1:])


def _heterogeneous_batch(
    fact_ids: tuple[UUID, ...], sources: dict[UUID, _SourceFact]
) -> bool:
    """P-13: one stored scope, one classification and one trust class across
    the batch, so the event carries one authoritative scope and one
    classification and trust transition rather than several."""
    first = sources[fact_ids[0]]
    return _heterogeneous_scope(fact_ids, sources) or any(
        sources[fact_id].classification != first.classification
        or sources[fact_id].trust != first.trust
        for fact_id in fact_ids[1:]
    )


def _lifecycle_failure(
    fact_ids: tuple[UUID, ...], sources: dict[UUID, _SourceFact]
) -> str | None:
    """I-67: a failed approach is preserved as a failure, never laundered into
    a validated belief.

    ``source_invalidated`` deliberately does **not** live here. It is a data
    precondition rather than an authorisation check, and this runs on replays
    too — so refusing here would deny the replay of a promotion that already
    committed, while the derived validated fact it asks about sits in the
    catalogue. Invalidating a *source* does not invalidate what was derived
    from it. Worse, that denial invites the one response that does real
    damage: retrying under a fresh idempotency key and promoting twice.

    The line is drawn where the plan draws it — grants are freshly evaluated
    on every replay (I-44), data preconditions are checked on first execution
    only. So invalidation is checked once, inside the mutation transaction,
    which a replay never enters.

    A failed approach needs no such care: trust is immutable, so a source that
    was promotable cannot become a failed approach afterwards, and this can
    never fire on the replay of a promotion that succeeded.
    """
    for fact_id in fact_ids:
        if sources[fact_id].trust is TrustClass.FAILED_APPROACH:
            return "failed_approach_source"
    return None


def _target_shape_failure(
    source_scope: Scope,
    source_classification: Classification,
    target_scope: Scope,
    target_classification: Classification,
) -> str | None:
    """Structural target checks: what the caller asked for, judged without
    reference to what they are allowed to do.

    Ancestor-only widening means the target is the source scope or a prefix of
    it in the same realm. Descendant, sibling and cross-realm targets are all
    ``target_not_ancestor``, which is an ``invalid_request`` — publicly
    distinguishable from an authorisation failure, and deliberately so. The
    cross-realm case is settled here, which is why promotion never looks up a
    target realm and so never needs the slice 3 ``realm_not_found`` path.
    """
    if target_scope.realm != source_scope.realm or not is_scope_prefix(
        target_scope.segments, source_scope.segments
    ):
        return "target_not_ancestor"
    if CLEARANCE_ORDER[target_classification] < CLEARANCE_ORDER[source_classification]:
        return "classification_lowered"
    return None


def _target_authorisation(
    grants: Sequence[GrantRecord],
    target_scope: Scope,
    target_classification: Classification,
    effective_at: datetime,
) -> GrantRecord | str:
    """The ``promote`` grant authorising the write, or the reason it was
    refused.

    ``write_classifications`` is checked against the resolved target class
    unconditionally, not only when the promotion raises it: a promotion writes
    a new fact, and a promoter holding ``{public}`` must not be able to write
    a ``restricted`` fact merely because the class was inherited unchanged.
    """
    promote_grant = find_authorising_grant(
        grants,
        realm_id=target_scope.realm,
        segments=target_scope.segments,
        operation=GrantOperation.PROMOTE,
        at=effective_at,
    )
    if promote_grant is None:
        return "target_promote_denied"
    if target_classification not in promote_grant.write_classifications:
        return "classification_not_writable"
    return promote_grant


def _reauthorise_proposal_promotion(
    fetch: _Fetch,
    actor: Actor,
    command: PromoteFacts,
    sources: dict[UUID, _SourceFact],
    source_scope: Scope,
    target_scope: Scope,
    target_classification: Classification,
    retrieve: GrantRecord,
    promote_grant: GrantRecord,
    effective_at: datetime,
    correlation_id: UUID,
) -> None:
    """Fresh locked authority for acceptance and replay, excluding invalidation.

    Sources are immutable. Grants are read again with one fresh evaluation
    time, including clearance, writable classification and named evidence.
    Keep the originally selected grant binding for the promotion audit.
    """
    code = FailureCode.AUTHORISATION_DENIED
    message = _AUTHORISATION_DENIED_MESSAGE
    try:
        grants = _GrantCache(fetch, actor.principal_id)
        current_retrieve = _source_visibility(
            command.fact_ids, sources, grants, effective_at
        )
        reason = _reauthorise(current_retrieve, retrieve, "source_retrieve_denied")
        if reason is None:
            reason = _reauthorise(
                _target_authorisation(
                    grants.for_realm(target_scope.realm),
                    target_scope,
                    target_classification,
                    effective_at,
                ),
                promote_grant,
                "target_promote_denied",
            )
        if reason is None and isinstance(command.evidence, UUID):
            assert isinstance(current_retrieve, GrantRecord)
            evidence = _named_evidence(
                fetch, command.evidence, source_scope, current_retrieve
            )
            if isinstance(evidence, str):
                reason = evidence
    except (AuditValueError, CustodyValueError) as error:
        code = FailureCode.INVALID_REQUEST
        message = _INVALID_REQUEST_MESSAGE
        reason = error.code
    if reason is not None:
        raise _denial(
            code,
            message,
            correlation_id,
            realm_id=source_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_PROMOTE,
            requested_scope=source_scope,
            reason_code=reason,
        )


def _revalidate_promotion(
    fetch: _Fetch,
    actor: Actor,
    fact_ids: tuple[UUID, ...],
    source_scope: Scope,
    target_scope: Scope,
    retrieve: GrantRecord,
    promote_grant: GrantRecord,
    effective_at: datetime,
    correlation_id: UUID,
) -> None:
    """The authoritative evaluation, under the write lock.

    Both grants are re-derived, because promotion needs both and either can be
    revoked concurrently. Source *existence* is not re-checked — facts are
    immutable and never deleted, so there is nothing to race — but invalidation
    is, because ``fact_invalidations`` is additive and the outer read happened
    on a different connection before the writer gate was taken. That window is
    real, and a guard that can be raced is not a guard.

    The grant re-derivation is wrapped against a hostile stored grant row for
    the same reason — see ``_revalidate_invalidate``.
    """
    # Both grants live in one realm: the ancestor check has already refused
    # any target outside the source's realm.
    try:
        grants = _sorted_grants(fetch, actor.principal_id, source_scope.realm)
    except AuditValueError as error:
        raise _denial(
            FailureCode.INVALID_REQUEST,
            _INVALID_REQUEST_MESSAGE,
            correlation_id,
            realm_id=source_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_PROMOTE,
            requested_scope=source_scope,
            reason_code=error.code,
        ) from error
    reason = _reauthorise(
        find_authorising_grant(
            grants,
            realm_id=source_scope.realm,
            segments=source_scope.segments,
            operation=GrantOperation.RETRIEVE,
            at=effective_at,
        ),
        retrieve,
        "source_retrieve_denied",
    ) or _reauthorise(
        find_authorising_grant(
            grants,
            realm_id=target_scope.realm,
            segments=target_scope.segments,
            operation=GrantOperation.PROMOTE,
            at=effective_at,
        ),
        promote_grant,
        "target_promote_denied",
    )
    if reason is not None:
        raise _denial(
            FailureCode.AUTHORISATION_DENIED,
            _AUTHORISATION_DENIED_MESSAGE,
            correlation_id,
            realm_id=source_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_PROMOTE,
            requested_scope=source_scope,
            reason_code=reason,
        )
    placeholders = ",".join("?" * len(fact_ids))
    if fetch(
        f"SELECT 1 FROM fact_invalidations WHERE fact_id IN ({placeholders})",
        [str(fact_id) for fact_id in fact_ids],
    ):
        raise _denial(
            FailureCode.INVALID_REQUEST,
            _INVALID_REQUEST_MESSAGE,
            correlation_id,
            realm_id=source_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_PROMOTE,
            requested_scope=source_scope,
            reason_code="source_invalidated",
        )


# --- invalidation authorisation -----------------------------------------------


def _invalidate_visibility(
    fact_ids: tuple[UUID, ...],
    sources: dict[UUID, _SourceFact],
    grants: _GrantCache,
    effective_at: datetime,
) -> GrantRecord | str:
    """The ``invalidate`` grant proven to cover every named fact's own stored
    scope, or the audit reason the batch is refused — evaluated per fact,
    before homogeneity is known.

    This is the analogue of ``_source_visibility``, narrowed to one grant
    operation and no clearance dimension: invalidation does not read a fact's
    body, so there is nothing for a clearance check to guard. It cannot,
    however, be simplified to a single check against the batch's eventual
    shared scope, because that scope is not yet known to be shared — and,
    unlike promotion, there is no separate ``retrieve`` grant to establish
    visibility first. This check has to do that job as well as its own, so it
    must run per fact, exactly where promotion's ``retrieve`` check runs, and
    for the same reason.

    I-67: an unknown identity and an identity outside the actor's invalidate
    authority are publicly identical and differ only here, in the reason.
    """
    authorising: GrantRecord | None = None
    for fact_id in fact_ids:
        source = sources.get(fact_id)
        if source is None:
            return "fact_unknown"
        authorising = find_authorising_grant(
            grants.for_realm(source.scope.realm),
            realm_id=source.scope.realm,
            segments=source.scope.segments,
            operation=GrantOperation.INVALIDATE,
            at=effective_at,
        )
        if authorising is None:
            return "invalidate_grant_not_held"
    # fact_ids is non-empty: _invalidate_limit_failure has already run.
    assert authorising is not None
    return authorising


def _superseded_by_failure(
    fetch: _Fetch, superseded_by: UUID, realm_id: str
) -> str | None:
    """``superseded_by`` must name an existing fact in the same realm as the
    batch being invalidated.

    By the time this runs, standing over the named facts is already proven,
    so the realm is legitimately known to the actor — the I-67 concern here
    is a *different* pair from the one above: a ``superseded_by`` that does
    not exist at all, and one that exists but in another realm, must produce
    byte-identical public denials. Both are simply ``superseded_by_unknown``.
    """
    rows = fetch("SELECT realm_id FROM facts WHERE fact_id = ?", (str(superseded_by),))
    if not rows or cast(str, rows[0][0]) != realm_id:
        return "superseded_by_unknown"
    return None


def _invalidation_record(
    fact_id: UUID,
    actor: Actor,
    command: InvalidateFacts,
    effective_at: datetime,
) -> InvalidationRecord:
    return InvalidationRecord(
        fact_id=fact_id,
        invalidated_at=effective_at,
        principal_id=actor.principal_id,
        superseded_by=command.superseded_by,
        reason=command.reason,
    )


def _revalidate_invalidate(
    fetch: _Fetch,
    actor: Actor,
    fact_ids: tuple[UUID, ...],
    shared_scope: Scope,
    invalidate_grant: GrantRecord,
    effective_at: datetime,
    correlation_id: UUID,
    *,
    check_fact_state: bool = True,
) -> None:
    """The authoritative evaluation, under the write lock.

    The invalidate grant is re-derived once, against the already-established
    shared scope — safe because homogeneity was already proven at the outer
    gate and facts are immutable, so re-deriving it per fact here would be
    pure redundancy, not a guard against anything that can change between the
    two evaluations.

    ``fact_already_invalidated`` is checked here and only here.
    ``fact_invalidations`` is additive and the outer read happened on a
    different connection before the writer gate was taken, so the window is
    real (P-07). It is a data precondition rather than an authorisation
    check, checked on first execution only — by analogy with Task 8's
    ``source_invalidated`` ruling — so a replay of an invalidation that
    already committed returns its original receipt rather than being denied.

    The grant re-derivation is wrapped for the same reason ``_load_sources``
    is at the outer gate: a grant row can be hostile in the same
    shape-without-meaning way a fact row can (see ``gate._row_to_grant``).
    ``_revalidate`` and ``_revalidate_promotion`` carry the identical guard,
    so the window is closed uniformly across all three commands rather than
    only where this task happened to be looking.
    """
    try:
        grants = _sorted_grants(fetch, actor.principal_id, shared_scope.realm)
    except AuditValueError as error:
        raise _denial(
            FailureCode.INVALID_REQUEST,
            _INVALID_REQUEST_MESSAGE,
            correlation_id,
            realm_id=shared_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_INVALIDATE,
            requested_scope=shared_scope,
            reason_code=error.code,
        ) from error
    reason = _reauthorise(
        find_authorising_grant(
            grants,
            realm_id=shared_scope.realm,
            segments=shared_scope.segments,
            operation=GrantOperation.INVALIDATE,
            at=effective_at,
        ),
        invalidate_grant,
        "invalidate_grant_not_held",
    )
    if reason is not None:
        raise _denial(
            FailureCode.AUTHORISATION_DENIED,
            _AUTHORISATION_DENIED_MESSAGE,
            correlation_id,
            realm_id=shared_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_INVALIDATE,
            requested_scope=shared_scope,
            reason_code=reason,
        )
    # A memory pre-replay guard checks authority, not the fresh-mutation
    # precondition: a successfully invalidated fact must remain replayable.
    if not check_fact_state:
        return
    placeholders = ",".join("?" * len(fact_ids))
    if fetch(
        f"SELECT 1 FROM fact_invalidations WHERE fact_id IN ({placeholders})",
        [str(fact_id) for fact_id in fact_ids],
    ):
        raise _denial(
            FailureCode.INVALID_REQUEST,
            _INVALID_REQUEST_MESSAGE,
            correlation_id,
            realm_id=shared_scope.realm,
            actor=actor,
            grant_id=None,
            action_kind=ActionKind.DATA,
            action_code=_INVALIDATE,
            requested_scope=shared_scope,
            reason_code="fact_already_invalidated",
        )


# --- promotion evidence and derived records ----------------------------------


@dataclass(frozen=True, slots=True)
class _PromotionEvidence:
    """``record`` is ``None`` when the command named evidence that already
    exists; only an inline external reference has a row to write."""

    evidence_id: UUID
    digest: bytes
    record: EvidenceRecord | None


def _named_evidence(
    fetch: _Fetch,
    evidence_id: UUID,
    source_scope: Scope,
    retrieve: GrantRecord,
) -> _PromotionEvidence | str:
    """Evidence named by identity must be in the same realm at the source
    scope or an ancestor of it, and within the actor's read clearance —
    otherwise a promotion could cite proof its author cannot see."""
    rows = fetch(
        "SELECT realm_id, scope_segments, classification, payload_digest "
        "FROM evidence_records WHERE evidence_id = ?",
        (str(evidence_id),),
    )
    if not rows:
        return "evidence_unknown"
    realm_id, scope_segments, classification, digest = cast(
        tuple[str, str, str, bytes], rows[0]
    )
    scope = _stored_scope(realm_id, scope_segments)
    if scope.realm != source_scope.realm or not is_scope_prefix(
        scope.segments, source_scope.segments
    ):
        return "evidence_scope_invalid"
    if (
        CLEARANCE_ORDER[Classification(classification)]
        > CLEARANCE_ORDER[retrieve.read_clearance]
    ):
        return "evidence_clearance_exceeded"
    return _PromotionEvidence(evidence_id=evidence_id, digest=digest, record=None)


def _external_evidence(
    evidence_id: UUID,
    reference: ExternalEvidenceReference,
    target_scope: Scope,
    target_classification: Classification,
    effective_at: datetime,
) -> _PromotionEvidence:
    """An inline reference creates an external-custody record at the target
    scope in the same transaction. Attic is not involved: Cairn never sees
    the bytes, so there is no payload to store and no outbox row to write."""
    custody = ExternalEvidence(
        external_uri=reference.external_uri,
        payload_digest=reference.payload_digest,
    )
    return _PromotionEvidence(
        evidence_id=evidence_id,
        digest=custody.payload_digest,
        record=EvidenceRecord(
            evidence_id=evidence_id,
            realm_id=target_scope.realm,
            segments=target_scope.segments,
            classification=target_classification,
            custody=custody,
            recorded_at=effective_at,
        ),
    )


def _promoted_fact(
    fact_id: UUID,
    source: _SourceFact,
    actor: Actor,
    target_scope: Scope,
    target_classification: Classification,
    evidence_id: UUID,
    effective_at: datetime,
) -> FactRecord:
    # The declared validity window is inherited, not restated: the derived
    # fact asserts the same thing over the same period, on better authority.
    return FactRecord(
        fact_id=fact_id,
        realm_id=target_scope.realm,
        segments=target_scope.segments,
        body=source.body,
        trust=TrustClass.VALIDATED,
        classification=target_classification,
        provenance=PromotedProvenance(
            derived_from=source.fact_id,
            promoted_by=actor.principal_id,
            evidence_id=evidence_id,
        ),
        valid_from=source.valid_from,
        valid_to=source.valid_to,
        recorded_at=effective_at,
    )


# --- limits and values -------------------------------------------------------


# I-96's fixed placeholder. Angle brackets sit outside every candidate
# charset in the policy, so the substitution can neither read as part of a
# longer token nor bridge two runs into one.
_ATTESTED_DIGEST_PLACEHOLDER = "<attested-digest>"

# The boundary for a standalone occurrence (Operator's F1 ruling on the I-96
# review, 23 August 2026). A digest flanked by any of these characters is
# part of a larger value — a constructed credential, a longer hex run — and
# is not itself the attested digest, so it stays unmasked and every rule
# still judges the full value. The class is the union of the policy's value
# charsets (the entropy candidate, authorization-header value, provider-token
# and hex classes), so the guard can never be narrower than the patterns it
# protects; the envelope's own digests sit quote-delimited in canonical JSON
# and are untouched by it.
_TOKEN_CHARSET = r"[A-Za-z0-9+/=_%.~-]"


def _ingest_screened_fields(command: IngestAssertion) -> Iterator[tuple[str, str]]:
    """The caller-authored text on an ingest, in the command's field order.

    A generator rather than a tuple, because ``first_finding`` stops at the
    first dirty field and the evidence payload is up to a mebibyte: a body
    that fails must not pay to decode a payload nobody will screen.

    Field paths are the command's own names, not the REST wire's — MCP reaches
    this same seam in slice 7 and must name the same fields.

    The payload is bytes and the screen takes text, so the decode is itself a
    policy choice. UTF-8 with replacement keeps text screenable and lets
    binary collapse harmlessly; ``latin-1`` would never raise but would mangle
    multi-byte UTF-8 into mojibake and defeat the P-36 fold on every non-ASCII
    payload. Stated residual: a UTF-16 or otherwise re-encoded payload evades,
    which is the policy module's standing position on determined encoders
    rather than a new hole. The fold is on a discarded copy, so I-30's
    prohibition on normalising stored content is untouched — exact evidence
    still keeps the bytes it arrived with.

    I-96: the metadata and payload renderings are screened with every
    *standalone* occurrence of an *attested digest* — the lowercase 64-hex
    SHA-256 of a sibling fact body in this command — replaced by a fixed
    placeholder, on the screening copy only (the same I-30 posture as the
    fold). A provenance envelope honestly quoting its own body's digest —
    the migration's ``content_sha256`` in metadata, repeated in the P-74
    verbatim-row payload — is otherwise exactly what ``HexHighEntropyString``
    exists to catch. The exemption is semantic rather than syntactic: a
    string is masked only if it *is* the SHA-256 of a body that is itself
    fully screened, so smuggling a secret through it requires a preimage, and
    there is no allowlist, baseline or per-principal bypass — I-31's posture
    holds. Standalone is load-bearing (Operator's F1 ruling on the review of this
    change): an occurrence flanked by ``_TOKEN_CHARSET`` characters is part
    of a larger value — a credential constructed around a known digest needs
    no preimage — and masking inside it would destroy a pattern finding that
    fired before this decision existed, so such occurrences stay unmasked and
    the full value is judged. Fact bodies are
    never masked, because the digest attests the body and the body's own text
    must stay fully screened; metadata member keys are not masked, because
    the ruling names the canonical string, the member values and the payload,
    and a key is never the envelope's digest carrier. The digests are
    computed only after the body yields, so a dirty body still stops the scan
    before paying for the hashing or the payload decode.
    """
    for index, draft in enumerate(command.facts):
        yield f"facts[{index}].body", draft.body
    digests = tuple(
        hashlib.sha256(draft.body.encode("utf-8")).hexdigest()
        for draft in command.facts
    )
    attested = (
        re.compile(
            f"(?<!{_TOKEN_CHARSET})(?:" + "|".join(digests) + f")(?!{_TOKEN_CHARSET})"
        )
        if digests
        else None
    )

    def masked(text: str) -> str:
        # The boundary decision is made over the policy's own fold (the
        # re-review finding over ``db8b510``): a format separator or a
        # fullwidth prefix must not make an embedded digest look standalone
        # to a raw-text boundary the screen would then never see. The fold
        # is idempotent, so the screen's own pass leaves this copy
        # unchanged; stored content is untouched either way (I-30).
        if attested is None:
            return text
        return attested.sub(_ATTESTED_DIGEST_PLACEHOLDER, normalise_for_screening(text))

    if command.metadata is not None:
        yield "metadata", masked(command.metadata)
        for path, text in _metadata_members(command.metadata):
            yield path, text if path.endswith(".key") else masked(text)
    if command.evidence_payload is not None:
        yield (
            "evidence_payload",
            masked(command.evidence_payload.decode("utf-8", "replace")),
        )


def _metadata_members(canonical: str) -> Iterator[tuple[str, str]]:
    """Every string key and string value inside canonical metadata, each
    screened on its own after the canonical string as a whole.

    The whole string alone is not sufficient, and the Task 11 leak sweep
    proved it rather than argued it: wrapped as ``{"note":"<secret>"}`` a
    corpus positive that trips ``HexHighEntropyString`` as a fact body
    trips nothing at all, and seven further rules lose their finding, so
    an ingest that is refused with the secret in ``facts[0].body`` commits
    it durably in ``metadata``. The cause is tokenisation: the pinned
    detectors read text, and JSON quoting, escaping and key punctuation
    join the secret to its delimiters. Screening the members restores
    every corpus verdict; the canonical string stays screened because it
    is what is stored, so no existing finding is lost.

    Paths are positional over the canonical (sorted-key) order —
    ``metadata[0].key`` — never the key text itself. A key may be the
    secret, and a field path travels back to the caller and into the safe
    log; naming the key there would be the disclosure the screen exists to
    prevent.
    """
    yield from _json_strings(cast(object, json.loads(canonical)), "metadata")


def _json_strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    if type(value) is str:
        yield path, value
    elif type(value) is dict:
        # A JSON object key is a string by grammar, so no type test guards
        # this yield: ``json.loads`` cannot produce any other kind.
        for index, (key, member) in enumerate(cast(dict[str, object], value).items()):
            yield f"{path}[{index}].key", key
            yield from _json_strings(member, f"{path}[{index}].value")
    elif type(value) is list:
        for index, member in enumerate(cast(list[object], value)):
            yield from _json_strings(member, f"{path}[{index}]")


def _promote_screened_fields(command: PromoteFacts) -> Iterator[tuple[str, str]]:
    """The caller-authored text on a promotion, in the command's field order.

    ``evidence`` is a stored UUID or an external reference; only the latter
    carries caller text. Its ``payload_digest`` is not screened — I-74 exempts
    digest fields, a 64-character hex hash image making entropy screening of
    it vacuous, with the bounded residual that a caller may smuggle exactly 32
    secret bytes as a purported digest accepted and stated there.
    """
    # ``isinstance`` where this codebase otherwise writes ``type(x) is not Y``,
    # and deliberately: the acceptance path below decides what gets stored with
    # ``isinstance`` too, so an exact-type test here would be *narrower* than
    # the test that admits the value — a subclass would be stored as external
    # evidence with its URI never screened. The screen must never be narrower
    # than the seam it guards, so the two agree on the broader test.
    if isinstance(command.evidence, ExternalEvidenceReference):
        yield "evidence.external_uri", command.evidence.external_uri
    yield "reason", command.reason


def _limit_failure(
    command: IngestAssertion, exact_evidence_enabled: bool
) -> str | None:
    # P-13's heterogeneous_batch is not enforced here and is not an omission:
    # IngestAssertion carries one classification and one source_type for the
    # whole command, so a heterogeneous ingest batch cannot be expressed.
    # Promotion must enforce it across its source facts, and invalidation
    # across its stored scopes — neither inherits "not applicable" from here.
    if not command.facts:
        return EMPTY_BATCH
    if len(command.facts) > MAX_BATCH_FACTS:
        return BATCH_TOO_LARGE
    if command.requested_trust is TrustClass.VALIDATED and (
        command.evidence_payload is None
    ):
        return "validated_requires_payload"
    if command.evidence_payload is not None and not exact_evidence_enabled:
        return "evidence_disabled"
    return None


def _promote_limit_failure(command: PromoteFacts) -> str | None:
    """P-13's ``duplicate_identity`` is enforced here and was not for ingest,
    which is not an inconsistency: ``IngestAssertion`` carries drafts, which
    have no identity to repeat, while ``PromoteFacts`` carries caller-supplied
    identities. A repeated source would otherwise derive two facts from one
    and put a duplicate into the audit event's identity set."""
    if not command.fact_ids:
        return EMPTY_BATCH
    if len(command.fact_ids) > MAX_BATCH_FACTS:
        return BATCH_TOO_LARGE
    if len(set(command.fact_ids)) != len(command.fact_ids):
        return DUPLICATE_IDENTITY
    try:
        validate_reason(command.reason)
        if isinstance(command.evidence, ExternalEvidenceReference):
            # Settled here, as command shape, so that the P-20 digest below
            # never renders an unvalidated caller value.
            ExternalEvidence(
                external_uri=command.evidence.external_uri,
                payload_digest=command.evidence.payload_digest,
            )
    except CustodyValueError as error:
        return error.code
    return None


def _realm_fingerprint(realm: object) -> bytes | None:
    """Correlates ingest's instance-chain refusals — the realm and nothing
    else.

    Ingest is the one command that carries a scope, so the realm is the widest
    thing a refusal may fingerprint without publishing the scope path the
    actor may have no standing to name. All three of ingest's instance-chain
    refusals use it, so a malformed scope, an unknown realm and an
    unauthorised request in the same realm correlate to the same value.

    ``None`` for a non-string realm, which is not a hedge: ``Scope`` refuses
    one at construction, so reaching here with an ``int`` means the scope
    arrived with its validation bypassed and ``.encode()`` would raise
    ``AttributeError`` while recording the very denial that exists to contain
    it. There is no realm to fingerprint in that case, and the event should
    say so rather than invent one.
    """
    if type(realm) is not str:
        return None
    return hashlib.sha256(realm.encode()).digest()


def _fact_batch_fingerprint(fact_ids: tuple[UUID, ...]) -> bytes:
    """Correlates the instance-chain refusals without disclosing which facts
    were named — the identities are exactly what must not be published when
    the actor may have no standing to know they exist.

    Shared by promotion and invalidation, whose commands both name facts by
    identity only and carry no scope of their own.
    """
    return hashlib.sha256(
        _canonical_json([str(fact_id) for fact_id in fact_ids]).encode()
    ).digest()


def _promote_fingerprint(command: PromoteFacts) -> bytes:
    return _fact_batch_fingerprint(command.fact_ids)


def _scoped_invalidation_allowed(
    fetch: _Fetch,
    actor: Actor,
    command: InvalidateFacts,
    scope: Scope,
    now: datetime,
) -> bool:
    """Additional host boundary; ordinary invalidation authority remains mandatory."""
    try:
        grants = _sorted_grants(fetch, actor.principal_id, scope.realm)
        ceiling = max(
            (
                CLEARANCE_ORDER[grant.read_clearance]
                for grant in grants
                if GrantOperation.RETRIEVE in grant.operations
                and is_scope_prefix(grant.segments, scope.segments)
                and is_live(grant, now)
            ),
            default=-1,
        )
        if ceiling < 0:
            return False
        identities = set(command.fact_ids)
        if command.superseded_by is not None:
            identities.add(command.superseded_by)
        rows = fetch(
            "SELECT realm_id, scope_segments, classification, recorded_at FROM facts WHERE fact_id IN ("
            + ",".join("?" for _ in identities)
            + ")",
            tuple(str(identity) for identity in identities),
        )
        return len(rows) == len(identities) and all(
            _stored_scope(cast(str, row[0]), cast(str, row[1])) == scope
            and CLEARANCE_ORDER[Classification(cast(str, row[2]))] <= ceiling
            and _stored_timestamp(cast(str, row[3])) <= now
            for row in rows
        )
    except (AuditValueError, CustodyValueError, ValueError, TypeError):
        return False


def _invalidate_limit_failure(command: InvalidateFacts) -> str | None:
    """P-13's ``duplicate_identity`` and the I-30 batch bound, exactly as
    ``_promote_limit_failure`` enforces them for promotion: ``InvalidateFacts``
    also carries caller-supplied identities rather than drafts. Reason
    validation is settled here too, as command shape, so it never reaches
    ``InvalidationRecord`` construction unvalidated."""
    if not command.fact_ids:
        return EMPTY_BATCH
    if len(command.fact_ids) > MAX_BATCH_FACTS:
        return BATCH_TOO_LARGE
    if len(set(command.fact_ids)) != len(command.fact_ids):
        return DUPLICATE_IDENTITY
    try:
        validate_reason(command.reason)
    except CustodyValueError as error:
        return error.code
    return None


def validate_ingest_payload(
    actor: Actor,
    command: IngestAssertion,
    *,
    effective_at: datetime,
    exact_evidence_enabled: bool,
    screen: SecretScreen,
    observation_times: tuple[datetime | None, ...] | None = None,
) -> SecretFinding | None:
    """Validate a complete future custody payload without I/O or minted IDs.

    Uses ingest's limit checks, record builders and screening policy. Raises
    ``CustodyValueError``/``AuditValueError`` for invalid values; returns a
    content-free ``SecretFinding`` for prohibited material, or ``None`` when
    valid. The caller must handle both failure channels before preparation.

    An ingest command has one assertion-level ``observed_at``. Callers
    starting from per-observation values must pass ``observation_times``;
    its length and every UTC instant must agree with the command. Values
    must be aware datetimes or explicit ``None``. UTC conversion is only for
    comparison; no input timestamp is changed or silently discarded.

    This does not authorise, audit, reserve an idempotency key or take
    custody. A caller establishes current standing before exposing any
    validation verdict, and maps safe failures into its own audited outcome.
    Actual ingest retains its separate validation and screening phases so
    committed replays are not screened again under a later policy.
    """
    scope_failure = _scope_shape_failure(command.scope)
    if scope_failure is not None:
        raise AuditValueError(scope_failure)
    limit_failure = _limit_failure(command, exact_evidence_enabled)
    if limit_failure is not None:
        raise CustodyValueError(limit_failure)
    if observation_times is not None:
        if len(observation_times) != len(command.facts):
            raise CustodyValueError("invalid_validity")
        observed_at = _observation_instant(command.observed_at)
        if any(
            _observation_instant(value) != observed_at for value in observation_times
        ):
            raise CustodyValueError("invalid_validity")
    # Record identity is irrelevant to value validation. These placeholders
    # never leave this function, consume no UUID factory and cannot be stored.
    placeholder = UUID("00000000-0000-4000-8000-000000000000")
    _ingest_records(
        actor,
        command,
        effective_at,
        placeholder,
        (placeholder,) * len(command.facts),
    )
    if command.evidence_payload is not None:
        _pending_evidence(
            placeholder,
            placeholder,
            placeholder,
            command,
            command.evidence_payload,
            effective_at,
        )
    return _screen_ingest_payload(command, screen)


def _observation_instant(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    _validate_tz_aware(value)
    # Equality with the same tzinfo compares wall time, ignoring fold. UTC
    # preserves the distinction between the two occurrences of a DST hour.
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise CustodyValueError("invalid_validity") from None


def _screen_ingest_payload(
    command: IngestAssertion,
    screen: SecretScreen,
) -> SecretFinding | None:
    return first_finding(screen, _ingest_screened_fields(command))


def _ingest_records(
    actor: Actor,
    command: IngestAssertion,
    effective_at: datetime,
    assertion_id: UUID,
    fact_ids: tuple[UUID, ...],
) -> tuple[AssertionRecord, tuple[FactRecord, ...]]:
    return (
        _assertion_record(assertion_id, actor, command, effective_at),
        tuple(
            _fact_record(fact_id, assertion_id, command, draft, effective_at)
            for fact_id, draft in zip(fact_ids, command.facts, strict=True)
        ),
    )


def _assertion_record(
    assertion_id: UUID,
    actor: Actor,
    command: IngestAssertion,
    effective_at: datetime,
) -> AssertionRecord:
    return AssertionRecord(
        assertion_id=assertion_id,
        realm_id=command.scope.realm,
        segments=command.scope.segments,
        classification=command.classification,
        source_type=command.source_type,
        principal_id=actor.principal_id,
        observed_at=command.observed_at,
        metadata=command.metadata,
        recorded_at=effective_at,
    )


def _fact_record(
    fact_id: UUID,
    assertion_id: UUID,
    command: IngestAssertion,
    draft: FactDraft,
    effective_at: datetime,
) -> FactRecord:
    return FactRecord(
        fact_id=fact_id,
        realm_id=command.scope.realm,
        segments=command.scope.segments,
        body=draft.body,
        trust=command.requested_trust,
        classification=command.classification,
        provenance=IngestedProvenance(assertion_id=assertion_id),
        valid_from=draft.valid_from,
        valid_to=draft.valid_to,
        recorded_at=effective_at,
    )


def _pending_evidence(
    evidence_id: UUID,
    work_id: UUID,
    assertion_id: UUID,
    command: IngestAssertion,
    payload: bytes,
    effective_at: datetime,
) -> _PendingEvidence:
    # The digest is Cairn's own, taken over the bytes actually received —
    # never a value the caller supplies and Cairn trusts. ExactEvidence is
    # what bounds the payload length, so an empty or oversize payload is
    # refused here, as invalid_payload, rather than by the storage layer.
    digest = hashlib.sha256(payload).digest()
    return _PendingEvidence(
        record=EvidenceRecord(
            evidence_id=evidence_id,
            realm_id=command.scope.realm,
            segments=command.scope.segments,
            classification=command.classification,
            custody=ExactEvidence(
                assertion_id=assertion_id,
                payload_length=len(payload),
                payload_digest=digest,
            ),
            recorded_at=effective_at,
        ),
        work_id=work_id,
        payload=payload,
        digest=digest,
    )


# --- custody writes ----------------------------------------------------------


def _insert_assertion(
    transaction: _MutationTransaction, assertion: AssertionRecord
) -> None:
    transaction.execute(
        "INSERT INTO assertions (assertion_id, realm_id, scope_segments, "
        "classification, source_type, principal_id, observed_at, metadata, "
        "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(assertion.assertion_id),
            assertion.realm_id,
            _segments_column(assertion.segments),
            assertion.classification.value,
            assertion.source_type.value,
            str(assertion.principal_id),
            None
            if assertion.observed_at is None
            else canonical_timestamp(assertion.observed_at),
            assertion.metadata,
            canonical_timestamp(assertion.recorded_at),
        ),
    )


def _insert_fact(transaction: _MutationTransaction, fact: FactRecord) -> None:
    # The two provenance forms are exclusive, and migration 0003's
    # ck_facts_provenance_form CHECK enforces exactly that shape in SQL: an
    # ingested fact names its assertion and nothing else, a promoted fact
    # names its source, promoter and evidence and no assertion.
    provenance = fact.provenance
    if isinstance(provenance, IngestedProvenance):
        lineage: tuple[str | None, ...] = (
            str(provenance.assertion_id),
            None,
            None,
            None,
        )
    else:
        lineage = (
            None,
            str(provenance.derived_from),
            str(provenance.promoted_by),
            str(provenance.evidence_id),
        )
    transaction.execute(
        "INSERT INTO facts (fact_id, realm_id, scope_segments, body, trust, "
        "classification, assertion_id, derived_from, promoted_by, evidence_id, "
        "valid_from, valid_to, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(fact.fact_id),
            fact.realm_id,
            _segments_column(fact.segments),
            fact.body,
            fact.trust.value,
            fact.classification.value,
            *lineage,
            None if fact.valid_from is None else canonical_timestamp(fact.valid_from),
            None if fact.valid_to is None else canonical_timestamp(fact.valid_to),
            canonical_timestamp(fact.recorded_at),
        ),
    )


def _insert_invalidation(
    transaction: _MutationTransaction, record: InvalidationRecord
) -> None:
    # fact_id is the row's primary key: one invalidation per fact, and
    # trg_fact_invalidations_no_update backs the "never edited" rule in SQL.
    transaction.execute(
        "INSERT INTO fact_invalidations (fact_id, invalidated_at, principal_id, "
        "superseded_by, reason) VALUES (?, ?, ?, ?, ?)",
        (
            str(record.fact_id),
            canonical_timestamp(record.invalidated_at),
            str(record.principal_id),
            None if record.superseded_by is None else str(record.superseded_by),
            record.reason,
        ),
    )


def _insert_evidence(
    transaction: _MutationTransaction, evidence: EvidenceRecord
) -> None:
    # Exact custody means Cairn holds the bytes and vouches for the digest it
    # took over them; external custody means it holds neither and records what
    # the caller attested. ck_evidence_records_custody_form pins the pair.
    custody = evidence.custody
    if isinstance(custody, ExactEvidence):
        form: tuple[object, ...] = (
            str(custody.assertion_id),
            custody.payload_length,
            None,
        )
    else:
        form = (None, None, custody.external_uri)
    transaction.execute(
        "INSERT INTO evidence_records (evidence_id, realm_id, scope_segments, "
        "classification, payload_digest, assertion_id, payload_length, "
        "external_uri, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(evidence.evidence_id),
            evidence.realm_id,
            _segments_column(evidence.segments),
            evidence.classification.value,
            custody.payload_digest,
            *form,
            canonical_timestamp(evidence.recorded_at),
        ),
    )


def _insert_evidence_outbox(
    transaction: _MutationTransaction,
    evidence: _PendingEvidence,
    effective_at: datetime,
) -> None:
    # The only place in the catalogue the exact payload bytes ever live, and
    # only until Attic confirms custody.
    transaction.execute(
        "INSERT INTO evidence_outbox (work_id, kind, evidence_id, mutation_id, "
        "payload, created_at, attempts) "
        "VALUES (?, 'store-payload', ?, ?, ?, ?, 0)",
        (
            str(evidence.work_id),
            str(evidence.record.evidence_id),
            str(transaction.mutation_id),
            evidence.payload,
            canonical_timestamp(effective_at),
        ),
    )


def _insert_projection_outbox(
    transaction: _MutationTransaction,
    work_id: UUID,
    fact_id: UUID,
    effective_at: datetime,
    *,
    kind: str,
) -> None:
    # Content-free per I-68: the identity of the affected fact and nothing
    # about what it says. P-17 fixes the kind vocabulary at fact-ingested,
    # fact-promoted and fact-invalidated, one row per affected fact.
    transaction.execute(
        "INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id, "
        "created_at, attempts) VALUES (?, ?, ?, ?, ?, 0)",
        (
            str(work_id),
            kind,
            str(fact_id),
            str(transaction.mutation_id),
            canonical_timestamp(effective_at),
        ),
    )


# --- canonical JSON, digests and result bytes --------------------------------


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _segment_documents(
    segments: tuple[ScopeSegment, ...],
) -> list[dict[str, str]]:
    # Written kind-first deliberately, against the canonical output order.
    # The canonical form is produced by sorting, not by insertion order, and
    # building these already alphabetical would let a lost ``sort_keys=True``
    # emit correct bytes anyway — making the invariant untestable at exactly
    # the layer where the schema's ``= json(...)`` CHECK cannot help.
    return [{"kind": segment.kind, "id": segment.identifier} for segment in segments]


def _segments_column(segments: tuple[ScopeSegment, ...]) -> str:
    """The canonical stored form of a scope path.

    Migration 0003's ``= json(...)`` CHECK pins minification only; key order
    is an application invariant, so every writer of a ``scope_segments``
    column must go through here.
    """
    return _canonical_json(_segment_documents(segments))


def _scope_document(scope: Scope) -> dict[str, object]:
    return {"realm": scope.realm, "segments": _segment_documents(scope.segments)}


def _ingest_digest(command: IngestAssertion) -> bytes:
    document: dict[str, object] = {
        "command": "ingest",
        "schema": _AUTHORITY_SCHEMA,
        "scope": _scope_document(command.scope),
        "classification": command.classification.value,
        "source_type": command.source_type.value,
        "requested_trust": command.requested_trust.value,
        "observed_at": None
        if command.observed_at is None
        else canonical_timestamp(command.observed_at),
        "metadata": None if command.metadata is None else json.loads(command.metadata),
        "facts": [
            {
                "body": draft.body,
                "valid_from": None
                if draft.valid_from is None
                else canonical_timestamp(draft.valid_from),
                "valid_to": None
                if draft.valid_to is None
                else canonical_timestamp(draft.valid_to),
            }
            for draft in command.facts
        ],
        "payload_sha256": None
        if command.evidence_payload is None
        else hashlib.sha256(command.evidence_payload).hexdigest(),
    }
    return hashlib.sha256(_canonical_json(document).encode()).digest()


def _promote_digest(command: PromoteFacts) -> bytes:
    evidence = command.evidence
    document: dict[str, object] = {
        "command": "promote",
        "schema": _AUTHORITY_SCHEMA,
        # Order-preserved: the pairing of source to derived fact is part of
        # what the caller asked for, so a reordered batch is a different
        # command rather than a replay of the same one.
        "fact_ids": [str(fact_id) for fact_id in command.fact_ids],
        "evidence": (
            {"evidence_id": str(evidence)}
            if isinstance(evidence, UUID)
            else {
                "external_uri": evidence.external_uri,
                "payload_digest": evidence.payload_digest.hex(),
            }
        ),
        "target_scope": (
            None
            if command.target_scope is None
            else _scope_document(command.target_scope)
        ),
        "target_classification": (
            None
            if command.target_classification is None
            else command.target_classification.value
        ),
        "reason": command.reason,
    }
    return hashlib.sha256(_canonical_json(document).encode()).digest()


def _proposal_accept_digest(command: PromoteFacts, proposal_id: UUID) -> bytes:
    document = {
        "command": _PROPOSAL_ACCEPT,
        "proposal_id": str(proposal_id),
        "promotion_digest": _promote_digest(command).hex(),
    }
    return hashlib.sha256(_canonical_json(document).encode()).digest()


def _receipt_document(receipt: MutationReceipt) -> dict[str, object]:
    return {
        "command_digest": receipt.command_digest.hex(),
        "mutation_id": str(receipt.mutation_id),
    }


def _receipt_from_document(document: dict[str, object]) -> MutationReceipt:
    return MutationReceipt(
        mutation_id=UUID(cast(str, document["mutation_id"])),
        command_digest=bytes.fromhex(cast(str, document["command_digest"])),
    )


def _encode_assertion_ingested(
    value: AssertionIngested, receipt: MutationReceipt
) -> bytes:
    # Identities only: a fact body or an evidence payload in these bytes would
    # be durable plaintext custody outside the one table that is meant to hold
    # it.
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "assertion_id": str(value.assertion_id),
                "fact_ids": [str(fact_id) for fact_id in value.fact_ids],
                "evidence_id": None
                if value.evidence_id is None
                else str(value.evidence_id),
            },
        }
    ).encode()


def _decode_assertion_ingested(
    data: bytes,
) -> tuple[AssertionIngested, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = AssertionIngested(
        assertion_id=UUID(result["assertion_id"]),
        fact_ids=tuple(UUID(fact_id) for fact_id in result["fact_ids"]),
        evidence_id=None
        if result["evidence_id"] is None
        else UUID(result["evidence_id"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _restate_assertion_identities(
    draft: AuditDraft, value: AssertionIngested
) -> AuditDraft:
    """Identity fields for an ingest *replay* event, taken from the stored
    result rather than from the identities this request minted and never
    wrote. ``evidence_digest`` needs no restating: it is derived from the
    command's own payload, and a replay only happens when the command digest
    matches, so it is already the original value."""
    return replace(
        draft,
        affected_assertion_ids=(value.assertion_id,),
        affected_fact_ids=value.fact_ids,
        affected_evidence_ids=(
            () if value.evidence_id is None else (value.evidence_id,)
        ),
        evidence_reference=value.evidence_id,
    )


def _restate_promotion_identities(
    draft: AuditDraft, value: FactsPromoted
) -> AuditDraft:
    """As above for promotion. The derived fact identities and an inline
    evidence record's identity are minted per request, so a replay's freshly
    minted ones name rows that were never written; both are rebuilt from the
    receipt — sorted, and derived-only, as the allow event's are."""
    return replace(
        draft,
        affected_fact_ids=tuple(
            sorted({derived_id for _, derived_id in value.promotions}, key=str)
        ),
        affected_evidence_ids=(value.evidence_id,),
        evidence_reference=value.evidence_id,
    )


def _encode_facts_promoted(value: FactsPromoted, receipt: MutationReceipt) -> bytes:
    # Identities only, as for ingest: neither the promoted body nor the reason
    # that justified the promotion belongs in durable result bytes.
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "promotions": [
                    [str(source_id), str(derived_id)]
                    for source_id, derived_id in value.promotions
                ],
                "evidence_id": str(value.evidence_id),
            },
        }
    ).encode()


def _decode_facts_promoted(data: bytes) -> tuple[FactsPromoted, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = FactsPromoted(
        promotions=tuple(
            (UUID(source_id), UUID(derived_id))
            for source_id, derived_id in result["promotions"]
        ),
        evidence_id=UUID(result["evidence_id"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])


def _invalidate_digest(command: InvalidateFacts) -> bytes:
    document: dict[str, object] = {
        "command": "invalidate",
        "schema": _AUTHORITY_SCHEMA,
        # Order-preserved, as promotion's fact_ids are: a reordered batch is a
        # different command, not a replay of the same one.
        "fact_ids": [str(fact_id) for fact_id in command.fact_ids],
        "reason": command.reason,
        "superseded_by": (
            None if command.superseded_by is None else str(command.superseded_by)
        ),
    }
    return hashlib.sha256(_canonical_json(document).encode()).digest()


def _encode_facts_invalidated(
    value: FactsInvalidated, receipt: MutationReceipt
) -> bytes:
    # Identities and the one shared instant only, as for ingest and
    # promotion: nothing about what the facts said or why they were
    # invalidated belongs in durable result bytes.
    return _canonical_json(
        {
            "mutation_receipt": _receipt_document(receipt),
            "result": {
                "fact_ids": [str(fact_id) for fact_id in value.fact_ids],
                "invalidated_at": canonical_timestamp(value.invalidated_at),
            },
        }
    ).encode()


def _decode_facts_invalidated(data: bytes) -> tuple[FactsInvalidated, MutationReceipt]:
    document = json.loads(data)
    result = document["result"]
    value = FactsInvalidated(
        fact_ids=tuple(UUID(fact_id) for fact_id in result["fact_ids"]),
        invalidated_at=parse_timestamp(result["invalidated_at"]),
    )
    return value, _receipt_from_document(document["mutation_receipt"])
