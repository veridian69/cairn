import hashlib
import json
import sqlite3
from _thread import LockType
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from cairn.catalogue.audit import (
    AuditDraft,
    AuditEvent,
    ChainKind,
    Outcome,
    Scope,
    ScopeRole,
    canonical_audit_bytes,
    hash_audit_event,
)
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp


class CatalogueTransactionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"catalogue transaction error: {code}")


class CommitAmbiguity(Exception):
    """Injected or driver-reported loss of commit acknowledgement."""


class CatalogueContention(Exception):
    """A busy or locked timeout around the write transaction, typed (I-49).

    Every writer runs ``BEGIN IMMEDIATE`` under the 5,000 ms busy timeout;
    a contender that outlasts it — at the ``BEGIN``, at a statement, at
    the commit or at the rollback (see ``_write_transaction``) — surfaces
    as this rather than as a raw
    ``sqlite3.OperationalError``. The path first became reachable with the
    P-65 backup barrier — the in-process ``_writer_gate`` serialises every
    writer inside one server, so only a second process contending for the
    write lock can trip it. No denial audit event accompanies this
    failure, because appending one needs the very lock that was contended;
    the transports render ``contention_failure`` directly instead.
    """


CONTENTION_MESSAGE = "The catalogue is briefly unavailable for writes."

_BUSY_PRIMARY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


@contextmanager
def _write_transaction(data_path: Path) -> Iterator[sqlite3.Connection]:
    """A write connection whose busy/locked failures are I-49 contention,
    wherever inside the transaction they arise.

    ``BEGIN IMMEDIATE`` is where contention normally lands, but it is not
    the only place: a checkpointer or a second process can make a
    statement, the commit or even the rollback time out, and I-49's
    promise is about the busy timeout, not about one statement. Mapping
    only the ``BEGIN`` left those paths raising a raw
    ``sqlite3.OperationalError``, which the REST middleware renders as
    the 500 I-49 exists to eliminate. The primary result code still
    decides, and any other ``OperationalError`` still passes through
    untouched.

    ``_resolve_commit_ambiguity`` deliberately does not use this: an
    ambiguous commit's honest signal is ``commit_outcome_unknown``, not
    "retry after a delay".
    """
    with _open_write_connection(data_path, create=False) as connection:
        try:
            yield connection
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode & 0xFF in _BUSY_PRIMARY_CODES:
                raise CatalogueContention() from error
            raise


def _begin_immediate(connection: sqlite3.Connection) -> None:
    """Opens the write transaction, mapping the busy/locked timeout to
    ``CatalogueContention``. The primary result code decides — extended
    codes such as ``SQLITE_BUSY_SNAPSHOT`` carry the primary in their low
    byte — and any other ``OperationalError`` stays raw: it is not
    contention and pretending it were retryable would mislabel a defect.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode & 0xFF in _BUSY_PRIMARY_CODES:
            raise CatalogueContention() from error
        raise


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    event_id: UUID
    chain_kind: ChainKind
    chain_identity: str
    sequence: int
    recorded_at: datetime
    event_hash: bytes


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    mutation_id: UUID
    command_digest: bytes


@dataclass(frozen=True, slots=True)
class Committed[T]:
    value: T
    mutation_receipt: MutationReceipt
    audit_receipt: AuditReceipt


@dataclass(frozen=True, slots=True)
class Replayed[T]:
    value: T
    mutation_receipt: MutationReceipt
    audit_receipt: AuditReceipt


class RetryClass(StrEnum):
    NEVER = "never"
    SAME_REQUEST = "same-request"
    AFTER_DELAY = "after-delay"


class FailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORISATION_DENIED = "authorisation_denied"
    SECRET_REJECTED = "secret_rejected"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INDEX_PENDING = "index_pending"
    STALE_INDEX = "stale_index"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INSTANCE_MISMATCH = "instance_mismatch"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """The bounded disclosure I-72 permits on a ``secret_rejected`` failure.

    Only a policy version, a rule identity and a field path — never the
    matched text, which ``SecretFinding`` does not carry either. It rides on
    the failure rather than on ``Rejected`` because I-72 makes ``detail`` part
    of the failure body, and it rides at all because the field path is
    deliberately absent from the denial audit event (P-26): without this the
    seam would know which field to name and have no way to say so.
    """

    policy: str
    rule: str
    field_path: str


@dataclass(frozen=True, slots=True)
class StableFailure:
    code: FailureCode
    safe_message: str
    correlation_id: UUID
    retry: RetryClass
    # Absent on every failure but the two I-72 licenses, which is why it is
    # defaulted rather than required: a caller reading ``detail`` on an
    # arbitrary failure would be reading a disclosure I-26 does not permit.
    detail: FailureDetail | None = None


def contention_failure(correlation_id: UUID) -> StableFailure:
    """The one rendering of ``CatalogueContention`` both transports share:
    ``dependency_unavailable`` with the after-delay retry class, exactly
    the I-49 promise P-65 discharges. Below ``StableFailure`` rather than
    beside its exception, because the return annotation is evaluated at
    definition time."""
    return StableFailure(
        code=FailureCode.DEPENDENCY_UNAVAILABLE,
        safe_message=CONTENTION_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.AFTER_DELAY,
    )


@dataclass(frozen=True, slots=True)
class Rejected:
    failure: StableFailure
    audit_receipt: AuditReceipt


type MutationOutcome[T] = Committed[T] | Replayed[T] | Rejected


class MutationRejection(Exception):
    """Raised by a mutation callback to deny its own request in place.

    The mutation's writes are rolled back; ``denial_draft`` is then appended
    durably in its own transaction and surfaces as ``Rejected``.

    This denial handling is specific to ``mutate_idempotent``: raised inside
    ``execute_compound``'s callback instead, it propagates raw with no denial
    event appended at all, since ``execute_compound`` has no ``except
    MutationRejection`` handler of its own. It is only meaningful when raised
    from within a ``mutate_idempotent`` mutation callback.
    """

    def __init__(self, failure: StableFailure, denial_draft: AuditDraft) -> None:
        self.failure = failure
        self.denial_draft = denial_draft
        super().__init__("mutation rejected")


class _GuardedTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise CatalogueTransactionError("transaction_expired")

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> None:
        self._require_active()
        self._connection.execute(sql, parameters)

    def query(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self._require_active()
        return tuple(self._connection.execute(sql, parameters).fetchall())

    def expire(self) -> None:
        self._active = False


class _MutationTransaction(_GuardedTransaction):
    """Carries the mutation identity the enclosing ``mutate_idempotent`` has
    already assigned, so a mutation can stamp it onto rows it writes —
    outbox work referencing the mutation that produced it (P-17) cannot be
    written after the fact, and the identity is fixed before the callback
    runs."""

    def __init__(self, connection: sqlite3.Connection, mutation_id: UUID) -> None:
        super().__init__(connection)
        self.mutation_id = mutation_id


class CompoundTransaction(_GuardedTransaction):
    def __init__(
        self,
        connection: sqlite3.Connection,
        append: Callable[[sqlite3.Connection, AuditDraft], AuditReceipt],
    ) -> None:
        super().__init__(connection)
        self._append = append

    def append(self, draft: AuditDraft) -> AuditReceipt:
        self._require_active()
        return self._append(self._connection, draft)


class CatalogueTransactions:
    def __init__(
        self,
        data_path: Path,
        *,
        writer_gate: LockType,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        commit: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self._data_path = data_path
        self._writer_gate = writer_gate
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._commit = _commit if commit is None else commit

    def append_audit(self, draft: AuditDraft) -> AuditReceipt:
        with self._writer_gate:
            with _write_transaction(self._data_path) as connection:
                return self._append_in_transaction(connection, draft)

    def reject(
        self,
        draft: AuditDraft,
        failure: StableFailure,
    ) -> Rejected:
        audit_receipt = self.append_audit(draft)
        return Rejected(failure=failure, audit_receipt=audit_receipt)

    def mutate_idempotent[T](
        self,
        draft: AuditDraft,
        *,
        principal_id: UUID,
        operation: str,
        idempotency_key: UUID,
        command_digest: bytes,
        result_schema: str,
        mutation: Callable[[_MutationTransaction], T],
        encode: Callable[[T, MutationReceipt], bytes],
        decode: Callable[[bytes], tuple[T, MutationReceipt]],
        restate_identities: Callable[[AuditDraft, T], AuditDraft] | None = None,
        reauthorise: Callable[[_GuardedTransaction], None] | None = None,
    ) -> MutationOutcome[T]:
        """``restate_identities`` rewrites the *replay* event's identity fields
        from the decoded stored result. Any caller whose draft names identities
        it minted for this request must supply it — see ``_replay_or_reject``
        for why omitting it produces a false audit record.

        ``reauthorise`` is an optional gate evaluated under the write lock
        before both fresh mutation and replay lookup. Memory/v1 uses this to
        close revocation races on replay; existing v1 callers retain their
        accepted outer-gate and mutation-callback behaviour unchanged.
        """
        with self._writer_gate:
            with _write_transaction(self._data_path) as connection:
                _begin_immediate(connection)
                try:
                    if reauthorise is not None:
                        guard = _GuardedTransaction(connection)
                        try:
                            reauthorise(guard)
                        finally:
                            guard.expire()
                    outcome = self._mutate_idempotent(
                        connection,
                        draft,
                        principal_id=principal_id,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        command_digest=command_digest,
                        result_schema=result_schema,
                        mutation=mutation,
                        encode=encode,
                        decode=decode,
                        restate_identities=restate_identities,
                    )
                    self._commit(connection)
                    return outcome
                except CommitAmbiguity:
                    connection.rollback()
                    return self._resolve_commit_ambiguity(
                        draft,
                        outcome,
                        principal_id=principal_id,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        command_digest=command_digest,
                    )
                except MutationRejection as rejection:
                    connection.rollback()
                    return Rejected(
                        failure=rejection.failure,
                        audit_receipt=self._append_in_transaction(
                            connection,
                            rejection.denial_draft,
                        ),
                    )
                except BaseException:
                    connection.rollback()
                    raise

    def execute_compound[T](
        self,
        work: Callable[[CompoundTransaction], T],
    ) -> T:
        with self._writer_gate:
            with _write_transaction(self._data_path) as connection:
                _begin_immediate(connection)
                transaction = CompoundTransaction(connection, self._append)
                try:
                    result = work(transaction)
                    self._commit(connection)
                    return result
                except CommitAmbiguity as error:
                    connection.rollback()
                    raise CatalogueTransactionError("commit_outcome_unknown") from error
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    transaction.expire()

    def _resolve_commit_ambiguity[T](
        self,
        draft: AuditDraft,
        outcome: MutationOutcome[T],
        *,
        principal_id: UUID,
        operation: str,
        idempotency_key: UUID,
        command_digest: bytes,
    ) -> MutationOutcome[T]:
        with _open_write_connection(self._data_path, create=False) as connection:
            if isinstance(outcome, Committed):
                row = connection.execute(
                    "SELECT command_digest, mutation_id, original_event_id "
                    "FROM idempotency_records WHERE principal_id = ? "
                    "AND operation = ? AND idempotency_key = ?",
                    (str(principal_id), operation, str(idempotency_key)),
                ).fetchone()
                if row == (
                    command_digest,
                    str(outcome.mutation_receipt.mutation_id),
                    str(outcome.audit_receipt.event_id),
                ):
                    return outcome
            _begin_immediate(connection)
            try:
                self._append(
                    connection,
                    replace(
                        draft,
                        principal_id=principal_id,
                        outcome=Outcome.ERROR,
                        reason_code="commit_ambiguous",
                        idempotency_key=idempotency_key,
                        mutation_id=None,
                        command_digest=command_digest,
                        replay_of_mutation_id=None,
                        # P-16 permits evidence fields only on a data-plane
                        # allow event; see _replay_or_reject for why turning
                        # an allow draft into a non-allow one must clear them.
                        evidence_reference=None,
                        evidence_digest=None,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        raise CatalogueTransactionError("commit_outcome_unknown")

    def _mutate_idempotent[T](
        self,
        connection: sqlite3.Connection,
        draft: AuditDraft,
        *,
        principal_id: UUID,
        operation: str,
        idempotency_key: UUID,
        command_digest: bytes,
        result_schema: str,
        mutation: Callable[[_MutationTransaction], T],
        encode: Callable[[T, MutationReceipt], bytes],
        decode: Callable[[bytes], tuple[T, MutationReceipt]],
        restate_identities: Callable[[AuditDraft, T], AuditDraft] | None,
    ) -> MutationOutcome[T]:
        existing = connection.execute(
            "SELECT command_digest, result_schema, result_bytes, result_digest, "
            "mutation_id FROM idempotency_records "
            "WHERE principal_id = ? AND operation = ? AND idempotency_key = ?",
            (str(principal_id), operation, str(idempotency_key)),
        ).fetchone()
        if existing is not None:
            return self._replay_or_reject(
                connection,
                draft,
                existing=existing,
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                command_digest=command_digest,
                result_schema=result_schema,
                decode=decode,
                restate_identities=restate_identities,
            )

        mutation_receipt = MutationReceipt(
            mutation_id=self._uuid_factory(),
            command_digest=command_digest,
        )
        effective_draft = replace(
            draft,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            mutation_id=mutation_receipt.mutation_id,
            command_digest=command_digest,
            replay_of_mutation_id=None,
        )
        transaction = _MutationTransaction(connection, mutation_receipt.mutation_id)
        try:
            value = mutation(transaction)
        finally:
            transaction.expire()
        result_bytes = encode(value, mutation_receipt)
        _validate_result_bytes(result_bytes)
        audit_receipt = self._append(connection, effective_draft)
        connection.execute(
            "INSERT INTO idempotency_records ("
            "principal_id, operation, idempotency_key, command_digest, "
            "result_schema, result_bytes, result_digest, mutation_id, "
            "original_event_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(principal_id),
                operation,
                str(idempotency_key),
                command_digest,
                result_schema,
                result_bytes,
                hashlib.sha256(result_bytes).digest(),
                str(mutation_receipt.mutation_id),
                str(audit_receipt.event_id),
                canonical_timestamp(audit_receipt.recorded_at),
            ),
        )
        return Committed(
            value=value,
            mutation_receipt=mutation_receipt,
            audit_receipt=audit_receipt,
        )

    def _replay_or_reject[T](
        self,
        connection: sqlite3.Connection,
        draft: AuditDraft,
        *,
        existing: tuple[object, ...],
        principal_id: UUID,
        idempotency_key: UUID,
        command_digest: bytes,
        result_schema: str,
        decode: Callable[[bytes], tuple[T, MutationReceipt]],
        restate_identities: Callable[[AuditDraft, T], AuditDraft] | None,
    ) -> Replayed[T] | Rejected:
        stored_digest, stored_schema, result_bytes, result_digest, mutation_id = (
            existing
        )
        if stored_digest != command_digest:
            failure = StableFailure(
                code=FailureCode.IDEMPOTENCY_CONFLICT,
                safe_message="The idempotency key was used for another request.",
                correlation_id=draft.correlation_id,
                retry=RetryClass.NEVER,
            )
            conflict_draft = replace(
                draft,
                principal_id=principal_id,
                outcome=Outcome.DENY,
                reason_code="idempotency_conflict",
                idempotency_key=idempotency_key,
                mutation_id=None,
                command_digest=command_digest,
                replay_of_mutation_id=None,
                # The caller's draft describes the mutation it *would* have
                # made, so a mutation that creates or cites evidence hands one
                # in with both P-16 fields set. Turning it into a denial
                # without clearing them would breach the rule that only a
                # data-plane allow event carries evidence fields, and
                # AuditDraft would refuse to be constructed at all — turning a
                # clean idempotency conflict into a raw AuditValueError.
                # Clearing is also the honest record: this event refuses a
                # different command, whose evidence is not this evidence.
                evidence_reference=None,
                evidence_digest=None,
                # Same reasoning for the identity sets: an audit event
                # records effect, not intent, and on a conflict nothing was
                # affected. Three of the four sets only ever hold identities
                # minted for this request; revoke-grant's names a real,
                # pre-existing row, so clearing it forgoes naming the
                # attempted target — accepted by P-35 (Operator, 6 August 2026):
                # the closed cairn.audit/v1 value has no field for an
                # attempted target, reopening I-54 is out of scope per P-26,
                # and
                # command_digest already identifies the refused command. The
                # residual forensic gap is confined to conflicts, which mean
                # a key was reused with a different command — overwhelmingly
                # a client defect; a hostile revocation attempt with a fresh
                # key still produces an ordinary deny event naming the grant.
                affected_assertion_ids=(),
                affected_fact_ids=(),
                affected_evidence_ids=(),
                affected_grant_ids=(),
            )
            return Rejected(
                failure=failure,
                audit_receipt=self._append(connection, conflict_draft),
            )
        if (
            stored_schema != result_schema
            or type(result_bytes) is not bytes
            or type(result_digest) is not bytes
            or hashlib.sha256(result_bytes).digest() != result_digest
            or type(mutation_id) is not str
        ):
            raise CatalogueTransactionError("idempotency_record_corrupt")
        value, mutation_receipt = decode(result_bytes)
        if (
            mutation_receipt.command_digest != command_digest
            or str(mutation_receipt.mutation_id) != mutation_id
        ):
            raise CatalogueTransactionError("idempotency_result_corrupt")
        replay_draft = replace(
            draft,
            principal_id=principal_id,
            reason_code="idempotent_replay",
            idempotency_key=idempotency_key,
            mutation_id=None,
            command_digest=command_digest,
            replay_of_mutation_id=mutation_receipt.mutation_id,
        )
        if restate_identities is not None:
            # The caller's draft describes the mutation this replay did *not*
            # perform. A replay re-runs everything up to this point, including
            # minting identities for the rows it would have written — and none
            # of those rows exist, because the mutation callback never ran. An
            # event citing them would be a false record, and the audit chain is
            # the load-bearing artefact.
            #
            # They cannot simply be cleared: a replay stays an *allow* event,
            # and P-16 requires both evidence fields on an allow promote event.
            # So they are restated from the decoded stored result, which is the
            # only surviving record of what actually happened — not a second
            # source of truth, but the original one.
            replay_draft = restate_identities(replay_draft, value)
        return Replayed(
            value=value,
            mutation_receipt=mutation_receipt,
            audit_receipt=self._append(connection, replay_draft),
        )

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        draft: AuditDraft,
    ) -> AuditReceipt:
        _begin_immediate(connection)
        try:
            receipt = self._append(connection, draft)
            connection.commit()
            return receipt
        except BaseException:
            connection.rollback()
            raise

    def _append(
        self,
        connection: sqlite3.Connection,
        draft: AuditDraft,
    ) -> AuditReceipt:
        head = connection.execute(
            "SELECT last_sequence, last_hash FROM audit_heads "
            "WHERE chain_kind = ? AND chain_identity = ?",
            (draft.chain_kind.value, draft.chain_identity),
        ).fetchone()
        if (
            not isinstance(head, tuple)
            or len(head) != 2
            or type(head[0]) is not int
            or type(head[1]) is not bytes
            or len(head[1]) != 32
        ):
            raise CatalogueTransactionError("audit_chain_unavailable")
        previous_sequence = head[0]
        previous_hash = head[1]
        event = AuditEvent(
            draft=draft,
            sequence=previous_sequence + 1,
            event_id=self._uuid_factory(),
            recorded_at=self._clock(),
            previous_hash=previous_hash,
        )
        canonical_event = canonical_audit_bytes(event)
        event_hash = hash_audit_event(event)
        connection.execute(
            "INSERT INTO audit_events ("
            "chain_kind, chain_identity, sequence, event_id, recorded_at, "
            "previous_hash, event_hash, action_kind, action_code, outcome, "
            "reason_code, canonical_event"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.chain_kind.value,
                draft.chain_identity,
                event.sequence,
                str(event.event_id),
                canonical_timestamp(event.recorded_at),
                event.previous_hash,
                event_hash,
                draft.action_kind.value,
                draft.action_code,
                draft.outcome.value,
                draft.reason_code,
                canonical_event,
            ),
        )
        self._insert_scope(
            connection,
            event,
            ScopeRole.SOURCE,
            draft.source_scope,
        )
        self._insert_scope(
            connection,
            event,
            ScopeRole.REQUESTED,
            draft.requested_scope,
        )
        self._insert_scope(
            connection,
            event,
            ScopeRole.TARGET,
            draft.target_scope,
        )
        self._verify_inserted_event(
            connection,
            event,
            canonical_event,
            event_hash,
        )
        updated = connection.execute(
            "UPDATE audit_heads SET last_sequence = ?, last_hash = ? "
            "WHERE chain_kind = ? AND chain_identity = ? "
            "AND last_sequence = ? AND last_hash = ?",
            (
                event.sequence,
                event_hash,
                draft.chain_kind.value,
                draft.chain_identity,
                previous_sequence,
                previous_hash,
            ),
        )
        if updated.rowcount != 1:
            raise CatalogueTransactionError("stale_audit_head")
        return AuditReceipt(
            event_id=event.event_id,
            chain_kind=draft.chain_kind,
            chain_identity=draft.chain_identity,
            sequence=event.sequence,
            recorded_at=event.recorded_at,
            event_hash=event_hash,
        )

    @staticmethod
    def _verify_inserted_event(
        connection: sqlite3.Connection,
        event: AuditEvent,
        canonical_event: bytes,
        event_hash: bytes,
    ) -> None:
        draft = event.draft
        row = connection.execute(
            "SELECT event_id, recorded_at, previous_hash, event_hash, "
            "action_kind, action_code, outcome, reason_code, canonical_event "
            "FROM audit_events WHERE chain_kind = ? AND chain_identity = ? "
            "AND sequence = ?",
            (draft.chain_kind.value, draft.chain_identity, event.sequence),
        ).fetchone()
        if row != (
            str(event.event_id),
            canonical_timestamp(event.recorded_at),
            event.previous_hash,
            event_hash,
            draft.action_kind.value,
            draft.action_code,
            draft.outcome.value,
            draft.reason_code,
            canonical_event,
        ):
            raise CatalogueTransactionError("audit_projection_mismatch")
        expected: list[tuple[str, int, str | None, str | None]] = []
        for role, scope in (
            (ScopeRole.SOURCE, draft.source_scope),
            (ScopeRole.REQUESTED, draft.requested_scope),
            (ScopeRole.TARGET, draft.target_scope),
        ):
            if scope is None:
                continue
            if not scope.segments:
                expected.append((role.value, -1, None, None))
            else:
                expected.extend(
                    (
                        role.value,
                        ordinal,
                        segment.kind,
                        segment.identifier,
                    )
                    for ordinal, segment in enumerate(scope.segments)
                )
        rows = connection.execute(
            "SELECT role, ordinal, segment_kind, segment_id "
            "FROM audit_scope_index WHERE chain_kind = ? "
            "AND chain_identity = ? AND sequence = ? "
            "ORDER BY role, ordinal",
            (draft.chain_kind.value, draft.chain_identity, event.sequence),
        ).fetchall()
        if rows != sorted(expected):
            raise CatalogueTransactionError("audit_projection_mismatch")

    @staticmethod
    def _insert_scope(
        connection: sqlite3.Connection,
        event: AuditEvent,
        role: ScopeRole,
        scope: Scope | None,
    ) -> None:
        if scope is None:
            return
        coordinates = (
            event.draft.chain_kind.value,
            event.draft.chain_identity,
            event.sequence,
            role.value,
        )
        if not scope.segments:
            connection.execute(
                "INSERT INTO audit_scope_index ("
                "chain_kind, chain_identity, sequence, role, ordinal, "
                "segment_kind, segment_id"
                ") VALUES (?, ?, ?, ?, -1, NULL, NULL)",
                coordinates,
            )
            return
        connection.executemany(
            "INSERT INTO audit_scope_index ("
            "chain_kind, chain_identity, sequence, role, ordinal, "
            "segment_kind, segment_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (*coordinates, ordinal, segment.kind, segment.identifier)
                for ordinal, segment in enumerate(scope.segments)
            ],
        )


def _commit(connection: sqlite3.Connection) -> None:
    connection.commit()


def _validate_result_bytes(value: bytes) -> None:
    if type(value) is not bytes:
        raise CatalogueTransactionError("invalid_result_bytes")
    try:
        document = json.loads(value.decode("utf-8", errors="strict"))
        canonical = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CatalogueTransactionError("invalid_result_bytes") from error
    if canonical != value:
        raise CatalogueTransactionError("invalid_result_bytes")
