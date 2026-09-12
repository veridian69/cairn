"""Explicit-source proposal authority. Publication always uses normal promote.

No owner/name policy: current retrieve, ingest and promote grants govern the
respective operations. One SQLite decision slot fences every reviewer/key.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from uuid import RFC_4122, UUID

from cairn.authority import proposal_codec as stored
from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation
from cairn.authority.custody import CustodyValueError, validate_reason
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
from cairn.authority.mutations import (
    CairnAuthority,
    FactsPromoted,
    PromoteFacts,
    ProposalAcceptanceContext,
    _named_evidence,
    _proposal_accept_digest,
    _scope_shape_failure,
    _segments_column,
    _sorted_grants,
    _SourceFact,
    _target_authorisation,
    _target_shape_failure,
)
from cairn.authority.proposal_codec import StoredProposal as _Proposal
from cairn.authority.proposal_codec import decode, digest, encode
from cairn.authority.proposal_types import (
    AcceptProposal,
    ListProposals,
    ProposalCommand,
    ProposalDecision,
    ProposalPage,
    ProposalRecorded,
    ProposalSnapshot,
    ProposeMemory,
    ReadProposal,
    RejectProposal,
)
from cairn.authority.retrieval import _covering_retrieve_grants
from cairn.catalogue.audit import (
    ActionKind,
    AuditValueError,
    Classification,
    Outcome,
    Scope,
)
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    CompoundTransaction,
    FailureCode,
    MutationOutcome,
    MutationRejection,
    Rejected,
    RetryClass,
    StableFailure,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.screening import SecretScreen, first_finding

__all__ = [
    "AcceptProposal",
    "CairnProposals",
    "ListProposals",
    "ProposeMemory",
    "ReadProposal",
    "RejectProposal",
]

_ACTIONS = {
    ProposeMemory: "memory-propose",
    ReadProposal: "memory-proposal-read",
    ListProposals: "memory-proposal-list",
    AcceptProposal: "memory-proposal-accept",
    RejectProposal: "memory-proposal-reject",
}


def _decode_stored[**P, T](
    decoder: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Translate only failures from an explicitly selected stored-value decoder.

    Do not add these built-in exceptions to the whole-operation audit guard:
    unrelated authority/control-flow bugs must still escape visibly.
    """
    try:
        return decoder(*args, **kwargs)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise CustodyValueError("invalid_proposal_record") from None


def _load(fetch: Fetch, proposal_id: UUID) -> _Proposal | None:
    rows = fetch(
        f"SELECT {stored.PROPOSAL_COLUMNS} FROM memory_proposals WHERE proposal_id=?",
        (str(proposal_id),),
    )
    if not rows:
        return None
    return stored.decode_proposal(rows[0])


def _load_decision(fetch: Fetch, proposal_id: UUID) -> stored.StoredDecision | None:
    rows = fetch(
        f"SELECT {stored.DECISION_COLUMNS} FROM memory_proposal_decisions WHERE proposal_id=?",
        (str(proposal_id),),
    )
    return None if not rows else stored.decode_decision(rows[0])


class CairnProposals:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
        screen: SecretScreen,
        authority: CairnAuthority,
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock
        self._screen = screen
        self._authority = authority

    def _refuse(
        self,
        fetch: Fetch,
        actor: Actor,
        command: ProposalCommand,
        correlation_id: UUID,
        code: FailureCode = FailureCode.AUTHORISATION_DENIED,
        reason: str = "proposal_authorisation_denied",
    ) -> MutationRejection:
        # Unknown, hidden, wrong-source and denied identities have identical
        # public failures and instance audit metadata. No fingerprint of input.
        messages = {
            FailureCode.AUTHORISATION_DENIED: "The requested operation is not authorised.",
            FailureCode.INVALID_REQUEST: "The request is invalid.",
            FailureCode.IDEMPOTENCY_CONFLICT: "The request conflicts with an existing operation.",
            FailureCode.SECRET_REJECTED: "The request contains prohibited secret material.",
        }
        return MutationRejection(
            StableFailure(code, messages[code], correlation_id, RetryClass.NEVER),
            instance_denial_draft(
                instance_id(fetch),
                actor,
                _ACTIONS[type(command)],
                reason,
                correlation_id,
                action_kind=ActionKind.DATA,
            ),
        )

    @contextmanager
    def _stored_values(
        self, fetch: Fetch, actor: Actor, command: ProposalCommand, correlation_id: UUID
    ) -> Iterator[None]:
        """Stored values accepted by SQLite still require semantic decoding.

        Keep decoding failures inside the audited authority boundary; callers
        must not receive the malformed value or learn which stored row failed.
        This guard encloses reads/decoding, never opening or committing storage.
        """
        try:
            yield
        except (CustodyValueError, AuditValueError, CatalogueStorageError):
            raise self._refuse(fetch, actor, command, correlation_id) from None

    def _scope_access(
        self,
        fetch: Fetch,
        actor: Actor,
        command: ProposalCommand,
        now: datetime,
        correlation_id: UUID,
    ) -> tuple[tuple[GrantRecord, ...], int]:
        if _scope_shape_failure(command.scope) is not None or not realm_exists(
            fetch, command.scope.realm
        ):
            raise self._refuse(fetch, actor, command, correlation_id)
        grants = _decode_stored(
            _sorted_grants, fetch, actor.principal_id, command.scope.realm
        )
        covering = _covering_retrieve_grants(grants, command.scope, now)
        if not covering:
            raise self._refuse(fetch, actor, command, correlation_id)
        return grants, max(CLEARANCE_ORDER[g.read_clearance] for g in covering)

    def _access(
        self,
        fetch: Fetch,
        actor: Actor,
        command: ProposeMemory | ReadProposal | AcceptProposal | RejectProposal,
        now: datetime,
        correlation_id: UUID,
    ) -> tuple[_SourceFact, _Proposal | None, GrantRecord]:
        with self._stored_values(fetch, actor, command, correlation_id):
            if type(command.proposal_id) is not UUID or (
                isinstance(command, ProposeMemory)
                and type(command.source_fact_id) is not UUID
            ):
                raise self._refuse(
                    fetch,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.INVALID_REQUEST,
                    "invalid_identity",
                )
            grants, ceiling = self._scope_access(
                fetch, actor, command, now, correlation_id
            )
            p = _load(fetch, command.proposal_id)
            source_id = (
                command.source_fact_id
                if isinstance(command, ProposeMemory)
                else (None if p is None else p.source_fact_id)
            )
            sources = (
                {} if source_id is None else stored.load_sources(fetch, (source_id,))
            )
            source = sources.get(source_id) if source_id is not None else None
            if (
                source is None
                or source.scope != command.scope
                or CLEARANCE_ORDER[source.classification] > ceiling
            ):
                raise self._refuse(fetch, actor, command, correlation_id)
            if p is not None and (
                p.scope != command.scope
                or p.source_fact_id != source_id
                or p.classification != source.classification
            ):
                raise self._refuse(fetch, actor, command, correlation_id)
            if p is not None:
                stored.validate_history(fetch, p, _load_decision(fetch, p.proposal_id))
            grant = next(
                g
                for g in _covering_retrieve_grants(grants, command.scope, now)
                if CLEARANCE_ORDER[g.read_clearance]
                >= CLEARANCE_ORDER[source.classification]
            )
            if isinstance(command, ProposeMemory):
                writable = find_authorising_grant(
                    grants,
                    realm_id=command.scope.realm,
                    segments=command.scope.segments,
                    operation=GrantOperation.INGEST,
                    at=now,
                )
                if (
                    writable is None
                    or source.classification not in writable.write_classifications
                ):
                    raise self._refuse(fetch, actor, command, correlation_id)
                grant = writable
                if (
                    _scope_shape_failure(command.target_scope) is not None
                    or _target_shape_failure(
                        source.scope,
                        source.classification,
                        command.target_scope,
                        source.classification,
                    )
                    is not None
                ):
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.INVALID_REQUEST,
                        "invalid_target",
                    )
            elif isinstance(command, AcceptProposal):
                if (
                    type(command.evidence_id) is not UUID
                    or type(command.target_classification) is not Classification
                ):
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.INVALID_REQUEST,
                        "invalid_acceptance",
                    )
                # Reuse promotion's evidence closure, with one opaque refusal.
                # This is repeated by the proposal guard and normal promotion.
                stored.validate_evidence(fetch, command.evidence_id)
                if isinstance(
                    _decode_stored(
                        _named_evidence, fetch, command.evidence_id, source.scope, grant
                    ),
                    str,
                ):
                    raise self._refuse(fetch, actor, command, correlation_id)
            elif isinstance(command, RejectProposal):
                assert p is not None
                target = _target_authorisation(
                    grants, p.target_scope, source.classification, now
                )
                if isinstance(target, str):
                    raise self._refuse(fetch, actor, command, correlation_id)
                grant = target
            return source, p, grant

    def _screen_command(
        self,
        fetch: Fetch,
        actor: Actor,
        command: ProposeMemory | RejectProposal,
        correlation_id: UUID,
    ) -> None:
        try:
            validate_reason(command.reason)
        except CustodyValueError:
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.INVALID_REQUEST,
                "invalid_reason",
            ) from None
        fields = [("reason", command.reason)]
        # Scopes enter durable custody and authoritative audit, too.
        scopes = (
            (command.scope, command.target_scope)
            if isinstance(command, ProposeMemory)
            else (command.scope,)
        )
        for scope in scopes:
            fields.append(("realm", scope.realm))
            fields.extend(("scope", segment.identifier) for segment in scope.segments)
            fields.extend(("kind", segment.kind) for segment in scope.segments)
        if first_finding(self._screen, fields) is not None:
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.SECRET_REJECTED,
                "secret_rejected",
            )

    def _binding(
        self,
        fetch: Fetch,
        actor: Actor,
        command: ProposeMemory | RejectProposal | AcceptProposal,
        key: UUID,
        command_digest: bytes,
        correlation_id: UUID,
    ) -> None:
        """Validate the exact committed operation/result before any replay.

        A pending proposal is not itself a permit to replay an unrelated key.
        A decided proposal can only replay its own principal/key/command/result.
        """
        action = _ACTIONS[type(command)]
        with self._stored_values(fetch, actor, command, correlation_id):
            proposal = _load(fetch, command.proposal_id)
            decision = _load_decision(fetch, command.proposal_id)
            if proposal is not None:
                stored.validate_history(fetch, proposal, decision)
        bound = proposal if isinstance(command, ProposeMemory) else decision
        record = fetch(
            "SELECT 1 FROM idempotency_records WHERE principal_id=? AND operation=? AND idempotency_key=?",
            (str(actor.principal_id), action, str(key)),
        )
        if bound is None and not record:
            return
        valid = False
        if bound is not None and record:
            binding = bound.binding
            valid = (
                binding.principal_id,
                binding.operation,
                binding.idempotency_key,
                binding.command_digest,
            ) == (actor.principal_id, action, key, command_digest)
        if not valid:
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.IDEMPOTENCY_CONFLICT,
                "proposal_conflict",
            )

    def _mutate(
        self,
        actor: Actor,
        command: ProposeMemory | RejectProposal,
        key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[ProposalRecorded]:
        now = self._clock()
        try:
            with read_connection(self._data_path) as connection:
                fetch = fetch_from(connection)
                if type(key) is not UUID or key.variant != RFC_4122:
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.INVALID_REQUEST,
                        "invalid_idempotency_key",
                    )
                source, _, grant = self._access(
                    fetch, actor, command, now, correlation_id
                )
                self._screen_command(fetch, actor, command, correlation_id)
            command_digest = digest(command)
            action = _ACTIONS[type(command)]
            draft = realm_draft(
                realm_id=command.scope.realm,
                actor=actor,
                grant_id=grant.grant_id,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=command.scope,
                outcome=Outcome.ALLOW,
                reason_code="proposal_recorded",
                correlation_id=correlation_id,
            )

            def guard(tx: _GuardedTransaction) -> None:
                nonlocal now
                now = self._clock()
                current_source, _, current_grant = self._access(
                    tx.query, actor, command, now, correlation_id
                )
                if (source, grant) != (current_source, current_grant):
                    raise self._refuse(tx.query, actor, command, correlation_id)
                self._binding(
                    tx.query, actor, command, key, command_digest, correlation_id
                )

            def mutation(tx: _MutationTransaction) -> ProposalRecorded:
                self._screen_command(tx.query, actor, command, correlation_id)
                try:
                    if isinstance(command, ProposeMemory):
                        tx.execute(
                            "INSERT INTO memory_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                str(command.proposal_id),
                                str(command.source_fact_id),
                                command.scope.realm,
                                _segments_column(command.scope.segments),
                                _segments_column(command.target_scope.segments),
                                source.classification.value,
                                command.reason,
                                str(actor.principal_id),
                                action,
                                str(key),
                                command_digest,
                                str(tx.mutation_id),
                                canonical_timestamp(now),
                            ),
                        )
                    else:
                        tx.execute(
                            "INSERT INTO memory_proposal_decisions VALUES (?, 'rejected', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                            (
                                str(command.proposal_id),
                                str(actor.principal_id),
                                action,
                                str(key),
                                command_digest,
                                str(tx.mutation_id),
                                canonical_timestamp(now),
                                command.reason,
                            ),
                        )
                except sqlite3.IntegrityError:
                    raise self._refuse(
                        tx.query,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.IDEMPOTENCY_CONFLICT,
                        "proposal_conflict",
                    ) from None
                return ProposalRecorded(command.proposal_id)

            return self._transactions.mutate_idempotent(
                draft,
                principal_id=actor.principal_id,
                operation=action,
                idempotency_key=key,
                command_digest=command_digest,
                result_schema="cairn.proposal.recorded/v1",
                mutation=mutation,
                encode=encode,
                decode=decode,
                reauthorise=guard,
            )
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)

    def propose(
        self,
        actor: Actor,
        command: ProposeMemory,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[ProposalRecorded]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def reject(
        self,
        actor: Actor,
        command: RejectProposal,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[ProposalRecorded]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def accept(
        self,
        actor: Actor,
        command: AcceptProposal,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[FactsPromoted]:
        now = self._clock()
        try:
            with read_connection(self._data_path) as connection:
                fetch = fetch_from(connection)
                if (
                    type(idempotency_key) is not UUID
                    or idempotency_key.variant != RFC_4122
                ):
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.INVALID_REQUEST,
                        "invalid_idempotency_key",
                    )
                source, p, _ = self._access(fetch, actor, command, now, correlation_id)
                if (
                    type(command.evidence_id) is not UUID
                    or type(command.target_classification) is not Classification
                ):
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.INVALID_REQUEST,
                        "invalid_acceptance",
                    )
                assert p is not None
                promotion = PromoteFacts(
                    (p.source_fact_id,),
                    command.evidence_id,
                    p.target_scope,
                    command.target_classification,
                    p.reason,
                )
            command_digest = _proposal_accept_digest(promotion, command.proposal_id)

            def validate_stored(
                tx: _GuardedTransaction,
                actual_actor: Actor,
                actual: PromoteFacts,
                actual_proposal_id: UUID,
                actual_key: UUID,
            ) -> None:
                nonlocal now
                now = self._clock()
                if (actual_actor, actual, actual_proposal_id, actual_key) != (
                    actor,
                    promotion,
                    command.proposal_id,
                    idempotency_key,
                ):
                    raise self._refuse(tx.query, actor, command, correlation_id)
                current_source, current_p, _ = self._access(
                    tx.query, actor, command, now, correlation_id
                )
                if (source, p) != (current_source, current_p):
                    raise self._refuse(tx.query, actor, command, correlation_id)

            def guard(tx: _GuardedTransaction) -> None:
                self._binding(
                    tx.query,
                    actor,
                    command,
                    idempotency_key,
                    command_digest,
                    correlation_id,
                )

            def record(tx: _MutationTransaction, result: FactsPromoted) -> None:
                if (
                    len(result.promotions) != 1
                    or result.promotions[0][0] != source.fact_id
                    or result.evidence_id != command.evidence_id
                ):
                    raise self._refuse(
                        tx.query,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.IDEMPOTENCY_CONFLICT,
                        "proposal_conflict",
                    )
                try:
                    tx.execute(
                        "INSERT INTO memory_proposal_decisions VALUES (?, 'accepted', ?, 'memory-proposal-accept', ?, ?, ?, ?, NULL, ?, ?, ?)",
                        (
                            str(command.proposal_id),
                            str(actor.principal_id),
                            str(idempotency_key),
                            command_digest,
                            str(tx.mutation_id),
                            canonical_timestamp(now),
                            str(result.evidence_id),
                            str(result.promotions[0][1]),
                            command.target_classification.value,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise self._refuse(
                        tx.query,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.IDEMPOTENCY_CONFLICT,
                        "proposal_conflict",
                    ) from None

            return self._authority.promote(
                actor,
                promotion,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                acceptance=ProposalAcceptanceContext(
                    command.proposal_id, guard, record, validate_stored
                ),
            )
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)

    def _visible_reference(
        self,
        fetch: Fetch,
        actor: Actor,
        identity: UUID,
        table: Literal["facts", "evidence_records"],
        request: Scope,
        now: datetime,
    ) -> UUID | None:
        column = "fact_id" if table == "facts" else "evidence_id"
        rows = fetch(
            f"SELECT realm_id, scope_segments, classification FROM {table} WHERE {column}=?",
            (str(identity),),
        )
        if not rows:
            return None
        realm, segments, level = cast(tuple[str, str, str], rows[0])
        scope = stored.scope(realm, segments)
        if scope.realm != request.realm or not is_scope_prefix(
            scope.segments, request.segments
        ):
            return None
        grants = _covering_retrieve_grants(
            _decode_stored(_sorted_grants, fetch, actor.principal_id, request.realm),
            request,
            now,
        )
        return (
            identity
            if any(
                CLEARANCE_ORDER[g.read_clearance]
                >= CLEARANCE_ORDER[stored.classification(level)]
                for g in grants
            )
            else None
        )

    def _snapshot(
        self,
        fetch: Fetch,
        actor: Actor,
        p: _Proposal,
        source: _SourceFact,
        now: datetime,
    ) -> ProposalSnapshot:
        record = _load_decision(fetch, p.proposal_id)
        stored.validate_history(fetch, p, record)
        decision = None
        state: Literal["pending", "accepted", "rejected"] = "pending"
        if record is not None:
            state = record.state
            decision = ProposalDecision(
                state,
                record.binding.principal_id,
                record.binding.recorded_at,
                record.binding.mutation_id,
                record.reason,
                None
                if record.evidence_id is None
                else self._visible_reference(
                    fetch,
                    actor,
                    record.evidence_id,
                    "evidence_records",
                    p.scope,
                    now,
                ),
                None
                if record.promoted_fact_id is None
                else self._visible_reference(
                    fetch, actor, record.promoted_fact_id, "facts", p.scope, now
                ),
            )
        invalidated = bool(
            fetch(
                "SELECT 1 FROM fact_invalidations WHERE fact_id=?",
                (str(source.fact_id),),
            )
        )
        return ProposalSnapshot(
            p.proposal_id,
            p.scope,
            p.source_fact_id,
            source.trust,
            invalidated,
            p.target_scope,
            p.classification,
            p.reason,
            p.binding.principal_id,
            p.binding.recorded_at,
            p.binding.mutation_id,
            state,
            decision,
        )

    def read(
        self, actor: Actor, command: ReadProposal, *, correlation_id: UUID
    ) -> ProposalSnapshot | Rejected:
        def work(tx: CompoundTransaction) -> ProposalSnapshot:
            now = self._clock()
            source, p, grant = self._access(
                tx.query, actor, command, now, correlation_id
            )
            assert p is not None
            with self._stored_values(tx.query, actor, command, correlation_id):
                result = self._snapshot(tx.query, actor, p, source, now)
            tx.append(
                realm_draft(
                    realm_id=command.scope.realm,
                    actor=actor,
                    grant_id=grant.grant_id,
                    action_kind=ActionKind.DATA,
                    action_code=_ACTIONS[type(command)],
                    requested_scope=command.scope,
                    outcome=Outcome.ALLOW,
                    reason_code="proposal_read",
                    correlation_id=correlation_id,
                )
            )
            return result

        try:
            return self._transactions.execute_compound(work)
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)

    def list(
        self, actor: Actor, command: ListProposals, *, correlation_id: UUID
    ) -> ProposalPage | Rejected:
        def work(tx: CompoundTransaction) -> ProposalPage:
            now = self._clock()
            with self._stored_values(tx.query, actor, command, correlation_id):
                grants, ceiling = self._scope_access(
                    tx.query, actor, command, now, correlation_id
                )
            if (
                type(command.limit) is not int
                or not 1 <= command.limit <= 100
                or (command.after is not None and type(command.after) is not UUID)
            ):
                raise self._refuse(
                    tx.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.INVALID_REQUEST,
                    "invalid_page",
                )
            if command.after is not None:
                # Never accept an invented/hidden cursor to establish an offset.
                try:
                    self._access(
                        tx.query,
                        actor,
                        ReadProposal(command.scope, command.after),
                        now,
                        correlation_id,
                    )
                except MutationRejection:
                    raise self._refuse(
                        tx.query, actor, command, correlation_id
                    ) from None
            levels = tuple(
                c.value for c in Classification if CLEARANCE_ORDER[c] <= ceiling
            )
            slots = ",".join("?" for _ in levels)
            rows = tx.query(
                "SELECT p.proposal_id FROM memory_proposals p JOIN facts f ON f.fact_id=p.source_fact_id "
                "WHERE p.realm_id=? AND p.scope_segments=? AND f.realm_id=p.realm_id AND f.scope_segments=p.scope_segments "
                f"AND f.classification=p.classification AND p.classification IN ({slots}) "
                "AND p.proposal_id>? ORDER BY p.proposal_id LIMIT ?",
                (
                    command.scope.realm,
                    _segments_column(command.scope.segments),
                    *levels,
                    "" if command.after is None else str(command.after),
                    command.limit + 1,
                ),
            )
            items: list[ProposalSnapshot] = []
            with self._stored_values(tx.query, actor, command, correlation_id):
                # The extra eligible row affects the cursor too, so it must
                # cross the same boundary before influencing the response.
                for (identity,) in rows:
                    p = _load(tx.query, stored.identity(identity))
                    if p is None:
                        raise stored.invalid()
                    source = stored.load_sources(tx.query, (p.source_fact_id,)).get(
                        p.source_fact_id
                    )
                    if source is None:
                        raise stored.invalid()
                    items.append(self._snapshot(tx.query, actor, p, source, now))
            tx.append(
                realm_draft(
                    realm_id=command.scope.realm,
                    actor=actor,
                    grant_id=_covering_retrieve_grants(grants, command.scope, now)[
                        0
                    ].grant_id,
                    action_kind=ActionKind.DATA,
                    action_code=_ACTIONS[type(command)],
                    requested_scope=command.scope,
                    outcome=Outcome.ALLOW,
                    reason_code="proposals_listed",
                    correlation_id=correlation_id,
                )
            )
            return ProposalPage(
                tuple(items[: command.limit]),
                items[command.limit - 1].proposal_id
                if len(rows) > command.limit
                else None,
            )

        try:
            return self._transactions.execute_compound(work)
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)
