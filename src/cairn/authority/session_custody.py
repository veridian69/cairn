"""Normal ingest followed by fenced terminal recording, with replay reconciliation.

No lock spans these phases. The immutable preparation is checked again inside
each writer, including before normal ingest's idempotency lookup. Every caller
uses the original turn custody key irrespective of its session-operation key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, cast
from uuid import UUID

from cairn.authority.gate import Actor, Fetch, fetch_from, instance_id, realm_draft
from cairn.authority.mutations import AssertionIngested
from cairn.authority.session_codec import (
    canonical,
    decode,
    encode,
    future_ingest,
    observations_from,
)
from cairn.authority.session_types import CommitTurn, PrepareTurn, SessionSnapshot
from cairn.catalogue.audit import ActionKind, Classification, Outcome
from cairn.catalogue.sqlite import canonical_timestamp, read_connection
from cairn.catalogue.transactions import (
    Committed,
    FailureCode,
    MutationOutcome,
    MutationReceipt,
    MutationRejection,
    Rejected,
    Replayed,
    _GuardedTransaction,
    _MutationTransaction,
)
from cairn.session_identity import remember_key

if TYPE_CHECKING:
    from cairn.authority.sessions import CairnSessions


@dataclass(frozen=True, slots=True)
class _Preparation:
    command: PrepareTurn
    classification: Classification
    mutation_id: UUID
    payload: bytes
    digest: bytes


def _prepared(
    service: CairnSessions,
    fetch: Fetch,
    actor: Actor,
    command: CommitTurn,
    correlation_id: UUID,
) -> _Preparation:
    _, classification = service._access(
        fetch, actor, command, service._clock(), correlation_id
    )
    rows = fetch(
        "SELECT p.payload, p.payload_digest, p.mutation_id, t.attempt_id, o.command_digest, o.principal_id, o.operation "
        "FROM memory_session_preparations p JOIN memory_session_turns t USING(session_id, turn_id) "
        "JOIN memory_session_operations o ON o.mutation_id=p.mutation_id "
        "WHERE p.session_id=? AND p.turn_id=?",
        (str(command.session_id), str(command.turn_id)),
    )
    if not rows:
        raise service._refuse(
            fetch,
            actor,
            command,
            correlation_id,
            FailureCode.INVALID_REQUEST,
            "session_not_prepared",
            standing=True,
        )
    payload, digest, mid, attempt, operation_digest, owner, operation = cast(
        tuple[bytes, bytes, str, str, bytes, str, str], rows[0]
    )
    try:
        output = json.loads(payload)["command"]
        prepared = PrepareTurn(
            command.scope,
            command.session_id,
            command.turn_id,
            UUID(attempt),
            output["response"],
            observations_from(output["observations"]),
        )
        expected = canonical(
            {
                "schema": "cairn.session.preparation/v1",
                "instance_id": instance_id(fetch),
                "principal_id": actor.principal_id,
                "classification": classification,
                "operation": "session-prepare",
                "command": asdict(prepared),
            }
        )
        if (
            payload != expected
            or hashlib.sha256(payload).digest() != digest
            or operation_digest != digest
            or owner != str(actor.principal_id)
            or operation != "session-prepare"
            or fetch(
                "SELECT 1 FROM memory_session_abandonments WHERE session_id=? AND turn_id=?",
                (str(command.session_id), str(command.turn_id)),
            )
        ):
            raise ValueError("invalid_preparation")
        return _Preparation(prepared, classification, UUID(mid), payload, digest)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise service._refuse(
            fetch,
            actor,
            command,
            correlation_id,
            FailureCode.INVALID_REQUEST,
            "invalid_session_preparation",
            standing=True,
        ) from None


def commit_turn(
    service: CairnSessions,
    actor: Actor,
    command: CommitTurn,
    idempotency_key: UUID,
    correlation_id: UUID,
) -> MutationOutcome[SessionSnapshot]:
    try:
        with read_connection(service._data_path) as connection:
            fetch = fetch_from(connection)
            preparation = _prepared(service, fetch, actor, command, correlation_id)
            grant, _ = service._access(
                fetch, actor, command, service._clock(), correlation_id
            )

        def fence(transaction: _GuardedTransaction) -> None:
            current = _prepared(
                service, transaction.query, actor, command, correlation_id
            )
            if current != preparation:
                raise service._refuse(
                    transaction.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.IDEMPOTENCY_CONFLICT,
                    "session_conflict",
                    standing=True,
                )

        digest = hashlib.sha256(
            canonical(
                {
                    "schema": "cairn.session.commit/v1",
                    "operation": "session-commit",
                    "command": asdict(command),
                    "principal_id": actor.principal_id,
                    "preparation_mutation_id": preparation.mutation_id,
                    "preparation_digest": preparation.digest,
                }
            )
        ).digest()
        draft = realm_draft(
            realm_id=command.scope.realm,
            actor=actor,
            grant_id=grant.grant_id,
            action_kind=ActionKind.DATA,
            action_code="session-commit",
            requested_scope=command.scope,
            outcome=Outcome.ALLOW,
            reason_code="session_operation_recorded",
            correlation_id=correlation_id,
        )

        def claim_fence(transaction: _GuardedTransaction) -> None:
            fence(transaction)
            current, _ = service._access(
                transaction.query, actor, command, service._clock(), correlation_id
            )
            if current != grant:
                raise service._refuse(transaction.query, actor, command, correlation_id)
            # Includes terminals written before migration 0011, which have no
            # claim. Check inside this writer, never as an outer-only preflight.
            historical = transaction.query(
                "SELECT command_digest FROM idempotency_records WHERE principal_id=? AND operation='session-commit' AND idempotency_key=?",
                (str(actor.principal_id), str(idempotency_key)),
            )
            if historical and historical[0][0] != digest:
                raise service._refuse(
                    transaction.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.IDEMPOTENCY_CONFLICT,
                    "session_conflict",
                    standing=True,
                )

        def claim_write(transaction: _MutationTransaction) -> SessionSnapshot:
            transaction.execute(
                "INSERT INTO memory_session_commit_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(actor.principal_id),
                    "session-commit-claim",
                    str(idempotency_key),
                    digest,
                    str(command.session_id),
                    str(command.turn_id),
                    str(preparation.mutation_id),
                    preparation.digest,
                    str(transaction.mutation_id),
                ),
            )
            return service._snapshot(
                transaction.query,
                command,
                MutationReceipt(transaction.mutation_id, digest),
            )

        claim = service._transactions.mutate_idempotent(
            replace(draft, action_code="session-commit-claim"),
            principal_id=actor.principal_id,
            operation="session-commit-claim",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.session.snapshot/v1",
            mutation=claim_write,
            encode=encode,
            decode=decode,
            reauthorise=claim_fence,
        )
        if isinstance(claim, Rejected):
            return claim

        def custody_fence(transaction: _GuardedTransaction) -> None:
            claim_fence(transaction)
            rows = transaction.query(
                "SELECT command_digest, session_id, turn_id, preparation_mutation_id, preparation_digest, mutation_id "
                "FROM memory_session_commit_claims WHERE principal_id=? AND idempotency_key=?",
                (str(actor.principal_id), str(idempotency_key)),
            )
            if rows != (
                (
                    digest,
                    str(command.session_id),
                    str(command.turn_id),
                    str(preparation.mutation_id),
                    preparation.digest,
                    str(claim.mutation_receipt.mutation_id),
                ),
            ):
                raise service._refuse(
                    transaction.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.IDEMPOTENCY_CONFLICT,
                    "session_conflict",
                    standing=True,
                )

        # A committed claim permanently reserves this caller key even if the
        # process exits here. Its replay authorises no facts by itself.
        custody: Committed[AssertionIngested] | Replayed[AssertionIngested] | None = (
            None
        )
        key = remember_key(command.session_id, command.turn_id)
        if preparation.command.observations:
            result = service._authority.ingest(
                actor,
                future_ingest(preparation.command, preparation.classification),
                idempotency_key=key,
                correlation_id=correlation_id,
                reauthorise_at_commit=True,
                commit_guard=custody_fence,
            )
            if isinstance(result, Rejected):
                return result
            custody = result

        def record(transaction: _MutationTransaction) -> SessionSnapshot:
            if transaction.query(
                "SELECT 1 FROM memory_session_terminals WHERE session_id=? AND turn_id=?",
                (str(command.session_id), str(command.turn_id)),
            ):
                raise service._refuse(
                    transaction.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.IDEMPOTENCY_CONFLICT,
                    "session_conflict",
                    standing=True,
                )
            receipt = MutationReceipt(transaction.mutation_id, digest)
            snapshot = replace(
                service._snapshot(transaction.query, command, receipt),
                state="skipped" if custody is None else "committed",
                custody_result=None if custody is None else custody.value,
                custody_receipt=None if custody is None else custody.mutation_receipt,
                custody_audit_receipt=None
                if custody is None
                else custody.audit_receipt,
                custody_idempotency_key=None if custody is None else key,
            )
            transaction.execute(
                "INSERT INTO memory_session_terminals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(command.session_id),
                    str(command.turn_id),
                    str(preparation.mutation_id),
                    preparation.digest,
                    snapshot.state,
                    str(receipt.mutation_id),
                    str(actor.principal_id),
                    "session-commit",
                    str(idempotency_key),
                    digest,
                    canonical_timestamp(service._clock()),
                    encode(snapshot, receipt),
                    None
                    if custody is None
                    else str(custody.mutation_receipt.mutation_id),
                    None if custody is None else str(custody.audit_receipt.event_id),
                    None if custody is None else str(custody.value.assertion_id),
                ),
            )
            return snapshot

        return service._transactions.mutate_idempotent(
            draft,
            principal_id=actor.principal_id,
            operation="session-commit",
            idempotency_key=idempotency_key,
            command_digest=digest,
            result_schema="cairn.session.snapshot/v1",
            mutation=record,
            encode=encode,
            decode=decode,
            reauthorise=custody_fence,
        )
    except MutationRejection as error:
        return service._transactions.reject(error.denial_draft, error.failure)
