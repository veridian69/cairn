"""Catalogue-backed, owner-private session state and immutable preparation.

Only a Committed begin is permission to invoke a model. Replayed operations
describe historical results, never a new execution permit. The custody helper
consumes immutable preparation through normal ingest in a separate transaction.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import RFC_4122, UUID

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
from cairn.authority.grants import GrantRecord, find_authorising_grant
from cairn.authority.mutations import (
    CairnAuthority,
    _scope_shape_failure,
    _sorted_grants,
    _stored_scope,
    validate_ingest_payload,
)
from cairn.authority.retrieval import _covering_retrieve_grants
from cairn.authority.session_codec import (
    canonical,
    decode,
    encode,
    future_ingest,
    observations_from,
)
from cairn.authority.session_types import (
    AbandonTurn,
    AcknowledgeVisit,
    BeginTurn,
    CommitTurn,
    DurableObservation,
    IssueVisit,
    OpenSession,
    PrepareTurn,
    ReadSession,
    SessionCommand,
    SessionMutation,
    SessionSnapshot,
)
from cairn.catalogue.audit import ActionKind, AuditValueError, Classification, Outcome
from cairn.catalogue.sqlite import canonical_timestamp, parse_timestamp, read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    CompoundTransaction,
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
from cairn.projection.partition import canonical_segments_json
from cairn.screening import SecretScreen, first_finding

_ACTIONS = {
    OpenSession: "session-open",
    BeginTurn: "session-begin",
    PrepareTurn: "session-prepare",
    AbandonTurn: "session-abandon",
    IssueVisit: "session-issue-visit",
    AcknowledgeVisit: "session-acknowledge-visit",
    ReadSession: "session-read",
    CommitTurn: "session-commit",
}


class CairnSessions:
    def __init__(
        self,
        data_path: Path,
        transactions: CatalogueTransactions,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        screen: SecretScreen,
        authority: CairnAuthority,
    ) -> None:
        self._data_path = data_path
        self._transactions = transactions
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._screen = screen
        self._authority = authority

    def _refuse(
        self,
        fetch: Fetch,
        actor: Actor,
        command: SessionCommand,
        correlation_id: UUID,
        code: FailureCode = FailureCode.AUTHORISATION_DENIED,
        reason: str = "session_authorisation_denied",
        *,
        standing: bool = False,
    ) -> MutationRejection:
        messages = {
            FailureCode.AUTHORISATION_DENIED: "The requested operation is not authorised.",
            FailureCode.INVALID_REQUEST: "The request is invalid.",
            FailureCode.SECRET_REJECTED: "The request contains prohibited secret material.",
            FailureCode.IDEMPOTENCY_CONFLICT: "The request conflicts with an existing operation.",
        }
        action = _ACTIONS[type(command)]
        draft = (
            realm_draft(
                realm_id=command.scope.realm,
                actor=actor,
                grant_id=None,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=command.scope,
                outcome=Outcome.DENY,
                reason_code=reason,
                correlation_id=correlation_id,
            )
            if standing
            else instance_denial_draft(
                instance_id(fetch),
                actor,
                action,
                reason,
                correlation_id,
                action_kind=ActionKind.DATA,
            )
        )
        return MutationRejection(
            StableFailure(code, messages[code], correlation_id, RetryClass.NEVER), draft
        )

    def _access(
        self,
        fetch: Fetch,
        actor: Actor,
        command: SessionCommand,
        now: datetime,
        correlation_id: UUID,
    ) -> tuple[GrantRecord, Classification]:
        # Missing, hidden, mismatched and grant-denied sessions share one opaque
        # instance denial. Only validation after standing reaches a realm chain.
        if _scope_shape_failure(command.scope) is not None or not realm_exists(
            fetch, command.scope.realm
        ):
            raise self._refuse(fetch, actor, command, correlation_id)
        rows = fetch(
            "SELECT instance_id, principal_id, realm_id, scope_segments, classification FROM memory_sessions WHERE session_id = ?",
            (str(command.session_id),),
        )
        if rows:
            iid, owner, realm, segments, level = cast(
                tuple[str, str, str, str, str], rows[0]
            )
            if (
                iid != instance_id(fetch)
                or owner != str(actor.principal_id)
                or _stored_scope(realm, segments) != command.scope
            ):
                raise self._refuse(fetch, actor, command, correlation_id)
            classification = Classification(level)
        elif isinstance(command, OpenSession):
            classification = command.classification
        else:
            raise self._refuse(fetch, actor, command, correlation_id)
        try:
            grants = _sorted_grants(fetch, actor.principal_id, command.scope.realm)
        except (AuditValueError, CustodyValueError):
            raise self._refuse(fetch, actor, command, correlation_id) from None
        covering = _covering_retrieve_grants(grants, command.scope, now)
        if (
            not covering
            or type(classification) is not Classification
            or CLEARANCE_ORDER[classification]
            > max(CLEARANCE_ORDER[g.read_clearance] for g in covering)
        ):
            raise self._refuse(fetch, actor, command, correlation_id)
        grant = covering[0]
        if not isinstance(command, ReadSession):
            writable = find_authorising_grant(
                grants,
                realm_id=command.scope.realm,
                segments=command.scope.segments,
                operation=GrantOperation.INGEST,
                at=now,
            )
            if writable is None or classification not in writable.write_classifications:
                raise self._refuse(fetch, actor, command, correlation_id)
            grant = writable
        if (
            isinstance(command, OpenSession)
            and command.classification != classification
        ):
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.IDEMPOTENCY_CONFLICT,
                "session_conflict",
                standing=True,
            )
        return grant, classification

    def _payload(
        self,
        fetch: Fetch,
        actor: Actor,
        command: SessionMutation,
        classification: Classification,
        now: datetime,
        correlation_id: UUID,
    ) -> bytes:
        try:
            ids = [command.session_id]
            if isinstance(command, (BeginTurn, PrepareTurn, AbandonTurn)):
                ids.append(command.turn_id)
            if isinstance(command, (BeginTurn, PrepareTurn)):
                ids.append(command.attempt_id)
            if isinstance(command, BeginTurn) and command.replaces_turn_id is not None:
                ids.append(command.replaces_turn_id)
            if isinstance(command, AcknowledgeVisit):
                ids.append(command.visit_id)
            if any(
                type(identity) is not UUID or identity.variant != RFC_4122
                for identity in ids
            ):
                raise CustodyValueError("invalid_session_identity")
            fields: list[tuple[str, str]] = []
            if isinstance(command, PrepareTurn):
                if (
                    type(command.response) is not str
                    or len(command.response.encode()) > 32768
                ):
                    raise CustodyValueError("invalid_session_response")
                if (
                    type(command.observations) is not tuple
                    or len(command.observations) > 8
                    or any(
                        type(o) is not DurableObservation for o in command.observations
                    )
                ):
                    raise CustodyValueError("invalid_session_observations")
                if any(
                    type(o.body) is not str or len(o.body.encode()) > 4096
                    for o in command.observations
                ):
                    raise CustodyValueError("invalid_session_observations")
                fields.append(("response", command.response))
                if command.observations:
                    finding = validate_ingest_payload(
                        actor,
                        future_ingest(command, classification),
                        effective_at=now,
                        exact_evidence_enabled=self._authority._exact_evidence_enabled,
                        screen=self._screen,
                        observation_times=tuple(
                            o.observed_at for o in command.observations
                        ),
                    )
                    if finding is not None:
                        raise self._refuse(
                            fetch,
                            actor,
                            command,
                            correlation_id,
                            FailureCode.SECRET_REJECTED,
                            "session_secret_rejected",
                            standing=True,
                        )
            elif isinstance(command, AbandonTurn):
                validate_reason(command.reason)
                fields.append(("reason", command.reason))
            if first_finding(self._screen, fields) is not None:
                raise self._refuse(
                    fetch,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.SECRET_REJECTED,
                    "session_secret_rejected",
                    standing=True,
                )
            payload = canonical(
                {
                    "schema": "cairn.session.preparation/v1"
                    if isinstance(command, PrepareTurn)
                    else "cairn.session.command/v1",
                    "instance_id": instance_id(fetch),
                    "principal_id": actor.principal_id,
                    "classification": classification,
                    "operation": _ACTIONS[type(command)],
                    "command": asdict(command),
                }
            )
            if len(payload) > 73728:
                raise CustodyValueError("session_envelope_too_large")
            if isinstance(command, PrepareTurn) and command.observations:
                # Validate what recovery will actually ingest, as well as the
                # original values above. Same-zone datetime comparisons ignore
                # fold, so UTC encoding can expose an invalid validity window.
                persisted = replace(
                    command,
                    observations=observations_from(
                        json.loads(payload)["command"]["observations"]
                    ),
                )
                finding = validate_ingest_payload(
                    actor,
                    future_ingest(persisted, classification),
                    effective_at=now,
                    exact_evidence_enabled=self._authority._exact_evidence_enabled,
                    screen=self._screen,
                    observation_times=tuple(
                        o.observed_at for o in persisted.observations
                    ),
                )
                if finding is not None:
                    raise self._refuse(
                        fetch,
                        actor,
                        command,
                        correlation_id,
                        FailureCode.SECRET_REJECTED,
                        "session_secret_rejected",
                        standing=True,
                    )
            return payload
        except (CustodyValueError, AuditValueError) as error:
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.INVALID_REQUEST,
                error.code,
                standing=True,
            ) from None
        except (TypeError, ValueError, OverflowError):
            raise self._refuse(
                fetch,
                actor,
                command,
                correlation_id,
                FailureCode.INVALID_REQUEST,
                "invalid_session_value",
                standing=True,
            ) from None

    def _snapshot(
        self,
        fetch: Fetch,
        command: SessionCommand,
        receipt: MutationReceipt | None = None,
    ) -> SessionSnapshot:
        sid = str(command.session_id)
        iid, owner, realm, segments, level, mid = cast(
            tuple[str, str, str, str, str, str],
            fetch(
                "SELECT instance_id, principal_id, realm_id, scope_segments, classification, mutation_id FROM memory_sessions WHERE session_id = ?",
                (sid,),
            )[0],
        )
        state: SessionSnapshot = SessionSnapshot(
            command.session_id,
            UUID(iid),
            UUID(owner),
            _stored_scope(realm, segments),
            Classification(level),
            "open",
            MutationReceipt(UUID(mid), b""),
        )
        tid = (
            command.turn_id
            if isinstance(
                command, (ReadSession, BeginTurn, PrepareTurn, AbandonTurn, CommitTurn)
            )
            else None
        )
        if tid is not None:
            attempt, predecessor, mid = cast(
                tuple[str, str | None, str],
                fetch(
                    "SELECT attempt_id, replaces_turn_id, mutation_id FROM memory_session_turns WHERE session_id = ? AND turn_id = ?",
                    (sid, str(tid)),
                )[0],
            )
            state = replace(
                state,
                state="started",
                turn_id=tid,
                attempt_id=UUID(attempt),
                replaces_turn_id=None if predecessor is None else UUID(predecessor),
            )
            prepared = fetch(
                "SELECT payload, mutation_id FROM memory_session_preparations WHERE session_id = ? AND turn_id = ?",
                (sid, str(tid)),
            )
            abandoned = fetch(
                "SELECT reason, mutation_id FROM memory_session_abandonments WHERE session_id = ? AND turn_id = ?",
                (sid, str(tid)),
            )
            if prepared:
                payload, mid = cast(tuple[bytes, str], prepared[0])
                output = json.loads(payload)["command"]
                state = replace(
                    state,
                    state="prepared",
                    response=output["response"],
                    observations=observations_from(output["observations"]),
                )
            elif abandoned:
                reason, mid = cast(tuple[str, str], abandoned[0])
                state = replace(state, state="abandoned", abandonment_reason=reason)
            terminal = fetch(
                "SELECT result FROM memory_session_terminals WHERE session_id = ? AND turn_id = ?",
                (sid, str(tid)),
            )
            if terminal:
                state, terminal_receipt = decode(cast(bytes, terminal[0][0]))
                if receipt is None:
                    receipt = terminal_receipt
        if receipt is None:
            digest = cast(
                bytes,
                fetch(
                    "SELECT command_digest FROM memory_session_operations WHERE mutation_id = ?",
                    (mid,),
                )[0][0],
            )
            receipt = MutationReceipt(UUID(mid), digest)
        turns = cast(
            int,
            fetch(
                "SELECT count(*) FROM memory_session_turns WHERE session_id = ?", (sid,)
            )[0][0],
        )
        size = cast(
            int,
            fetch(
                "SELECT coalesce(sum(length(payload)), 0) FROM memory_session_preparations WHERE session_id = ?",
                (sid,),
            )[0][0],
        )
        ack = fetch(
            "SELECT v.watermark, v.issued_at FROM memory_session_acknowledgements a JOIN memory_session_visits v ON v.session_id = a.session_id AND v.visit_id = a.visit_id WHERE a.session_id = ? ORDER BY v.watermark DESC LIMIT 1",
            (sid,),
        )
        return replace(
            state,
            operational_receipt=receipt,
            turn_count=turns,
            prepared_bytes=size,
            acknowledged_watermark=cast(int, ack[0][0]) if ack else 0,
            acknowledged_at=parse_timestamp(cast(str, ack[0][1])) if ack else None,
        )

    def _write(
        self,
        transaction: _MutationTransaction,
        actor: Actor,
        command: SessionMutation,
        payload: bytes,
        now: datetime,
        correlation_id: UUID,
        classification: Classification,
        receipt: MutationReceipt,
    ) -> SessionSnapshot:
        sid = str(command.session_id)

        def conflict() -> MutationRejection:
            return self._refuse(
                transaction.query,
                actor,
                command,
                correlation_id,
                FailureCode.IDEMPOTENCY_CONFLICT,
                "session_conflict",
                standing=True,
            )

        if isinstance(command, OpenSession):
            if transaction.query(
                "SELECT 1 FROM memory_sessions WHERE session_id = ?", (sid,)
            ):
                raise conflict()
            transaction.execute(
                "INSERT INTO memory_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    str(instance_id(transaction.query)),
                    str(actor.principal_id),
                    command.scope.realm,
                    canonical_segments_json(command.scope.segments),
                    classification.value,
                    str(receipt.mutation_id),
                ),
            )
        elif isinstance(command, BeginTurn):
            if transaction.query(
                "SELECT 1 FROM memory_session_turns WHERE session_id = ? AND (turn_id = ? OR attempt_id = ?)",
                (sid, str(command.turn_id), str(command.attempt_id)),
            ):
                raise conflict()
            predecessor = command.replaces_turn_id
            if predecessor is not None:
                if not transaction.query(
                    "SELECT 1 FROM memory_session_abandonments WHERE session_id = ? AND turn_id = ?",
                    (sid, str(predecessor)),
                ) or transaction.query(
                    "SELECT 1 FROM memory_session_turns WHERE session_id = ? AND replaces_turn_id = ?",
                    (sid, str(predecessor)),
                ):
                    raise conflict()
            transaction.execute(
                "INSERT INTO memory_session_turns VALUES (?, ?, ?, ?, ?)",
                (
                    sid,
                    str(command.turn_id),
                    str(command.attempt_id),
                    None if predecessor is None else str(predecessor),
                    str(receipt.mutation_id),
                ),
            )
        elif isinstance(command, (PrepareTurn, AbandonTurn)):
            rows = transaction.query(
                "SELECT attempt_id FROM memory_session_turns WHERE session_id = ? AND turn_id = ?",
                (sid, str(command.turn_id)),
            )
            if not rows:
                raise self._refuse(transaction.query, actor, command, correlation_id)
            if isinstance(command, PrepareTurn) and rows[0][0] != str(
                command.attempt_id
            ):
                raise conflict()
            for table in ("memory_session_preparations", "memory_session_abandonments"):
                if transaction.query(
                    f"SELECT 1 FROM {table} WHERE session_id = ? AND turn_id = ?",
                    (sid, str(command.turn_id)),
                ):
                    raise conflict()
            if isinstance(command, PrepareTurn):
                transaction.execute(
                    "INSERT INTO memory_session_preparations VALUES (?, ?, ?, ?, ?)",
                    (
                        sid,
                        str(command.turn_id),
                        payload,
                        hashlib.sha256(payload).digest(),
                        str(receipt.mutation_id),
                    ),
                )
            else:
                transaction.execute(
                    "INSERT INTO memory_session_abandonments VALUES (?, ?, ?, ?)",
                    (
                        sid,
                        str(command.turn_id),
                        command.reason,
                        str(receipt.mutation_id),
                    ),
                )
        elif isinstance(command, IssueVisit):
            last = transaction.query(
                "SELECT watermark, issued_at FROM memory_session_visits WHERE session_id = ? ORDER BY watermark DESC LIMIT 1",
                (sid,),
            )
            watermark = cast(int, last[0][0]) + 1 if last else 1
            at = max(now, parse_timestamp(cast(str, last[0][1]))) if last else now
            visit = self._uuid_factory()
            transaction.execute(
                "INSERT INTO memory_session_visits VALUES (?, ?, ?, ?, ?)",
                (
                    sid,
                    str(visit),
                    watermark,
                    canonical_timestamp(at),
                    str(receipt.mutation_id),
                ),
            )
            return replace(
                self._snapshot(transaction.query, command, receipt),
                visit_id=visit,
                visit_watermark=watermark,
                visit_at=at,
            )
        else:
            if not transaction.query(
                "SELECT 1 FROM memory_session_visits WHERE session_id = ? AND visit_id = ?",
                (sid, str(command.visit_id)),
            ):
                raise self._refuse(
                    transaction.query,
                    actor,
                    command,
                    correlation_id,
                    FailureCode.INVALID_REQUEST,
                    "invalid_session_visit",
                    standing=True,
                )
            transaction.execute(
                "INSERT INTO memory_session_acknowledgements VALUES (?, ?, ?)",
                (sid, str(command.visit_id), str(receipt.mutation_id)),
            )
        return self._snapshot(transaction.query, command, receipt)

    def _mutate(
        self,
        actor: Actor,
        command: SessionMutation,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        action = _ACTIONS[type(command)]
        now = self._clock()
        try:
            with read_connection(self._data_path) as connection:
                fetch = fetch_from(connection)
                authorising, classification = self._access(
                    fetch, actor, command, now, correlation_id
                )
                payload = self._payload(
                    fetch, actor, command, classification, now, correlation_id
                )
            digest = hashlib.sha256(payload).digest()
            draft = realm_draft(
                realm_id=command.scope.realm,
                actor=actor,
                grant_id=authorising.grant_id,
                action_kind=ActionKind.DATA,
                action_code=action,
                requested_scope=command.scope,
                outcome=Outcome.ALLOW,
                reason_code="session_operation_recorded",
                correlation_id=correlation_id,
            )

            def reauthorise(transaction: _GuardedTransaction) -> None:
                nonlocal now
                now = self._clock()
                current = self._access(
                    transaction.query, actor, command, now, correlation_id
                )
                if current != (authorising, classification):
                    raise self._refuse(
                        transaction.query, actor, command, correlation_id
                    )

            def mutation(transaction: _MutationTransaction) -> SessionSnapshot:
                # Validation and state checks share the actual write transaction.
                self._payload(
                    transaction.query,
                    actor,
                    command,
                    classification,
                    now,
                    correlation_id,
                )
                receipt = MutationReceipt(transaction.mutation_id, digest)
                transaction.execute(
                    "INSERT INTO memory_session_operations VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(receipt.mutation_id),
                        str(actor.principal_id),
                        action,
                        str(idempotency_key),
                        digest,
                        canonical_timestamp(now),
                    ),
                )
                return self._write(
                    transaction,
                    actor,
                    command,
                    payload,
                    now,
                    correlation_id,
                    classification,
                    receipt,
                )

            return self._transactions.mutate_idempotent(
                draft,
                principal_id=actor.principal_id,
                operation=action,
                idempotency_key=idempotency_key,
                command_digest=digest,
                result_schema="cairn.session.snapshot/v1",
                mutation=mutation,
                encode=encode,
                decode=decode,
                reauthorise=reauthorise,
            )
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)

    def open(
        self,
        actor: Actor,
        command: OpenSession,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def begin(
        self,
        actor: Actor,
        command: BeginTurn,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def prepare(
        self,
        actor: Actor,
        command: PrepareTurn,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def abandon(
        self,
        actor: Actor,
        command: AbandonTurn,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def commit(
        self,
        actor: Actor,
        command: CommitTurn,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        from cairn.authority.session_custody import commit_turn

        return commit_turn(self, actor, command, idempotency_key, correlation_id)

    def issue_visit(
        self,
        actor: Actor,
        command: IssueVisit,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def acknowledge_visit(
        self,
        actor: Actor,
        command: AcknowledgeVisit,
        *,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome[SessionSnapshot]:
        return self._mutate(actor, command, idempotency_key, correlation_id)

    def read(
        self, actor: Actor, command: ReadSession, *, correlation_id: UUID
    ) -> SessionSnapshot | Rejected:
        def work(transaction: CompoundTransaction) -> SessionSnapshot:
            grant, _ = self._access(
                transaction.query, actor, command, self._clock(), correlation_id
            )
            if command.turn_id is not None and not transaction.query(
                "SELECT 1 FROM memory_session_turns WHERE session_id = ? AND turn_id = ?",
                (str(command.session_id), str(command.turn_id)),
            ):
                raise self._refuse(transaction.query, actor, command, correlation_id)
            result = self._snapshot(transaction.query, command)
            transaction.append(
                realm_draft(
                    realm_id=command.scope.realm,
                    actor=actor,
                    grant_id=grant.grant_id,
                    action_kind=ActionKind.DATA,
                    action_code="session-read",
                    requested_scope=command.scope,
                    outcome=Outcome.ALLOW,
                    reason_code="session_read",
                    correlation_id=correlation_id,
                )
            )
            return result

        try:
            return self._transactions.execute_compound(work)
        except MutationRejection as error:
            return self._transactions.reject(error.denial_draft, error.failure)
