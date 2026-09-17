"""Opt-in catalogue recovery: only a fresh begin permits one model callback."""

from dataclasses import replace
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from cairn.catalogue.sqlite import parse_timestamp
from cairn.client.briefing import build_arrival_briefing
from cairn.client.errors import FailureMetadata, MemoryOperationFailure
from cairn.client.memory import MemoryClient
from cairn.client.session import _recall_query
from cairn.client.session_types import (
    DurableArrival,
    DurableProgress,
    DurableSessionFailure,
    DurableStage,
    DurableTurnResult,
    SessionOperationResult,
    SessionSnapshot,
)
from cairn.client.session_validation import require_preparation_binding
from cairn.client.types import (
    ConnectionStatus,
    ModelCallback,
    ModelTurn,
    PersistenceReceipt,
    PersistenceStatus,
    TurnInput,
)
from cairn.session_identity import MEMORY_REMEMBER_NAMESPACE


class DurableMemorySession:
    """No local persistence, credential copy, callback replay or automatic ack.

    Call open before using a new session. Keep the session and turn identities
    in host configuration/caller input; all recovery content stays in Cairn.
    """

    def __init__(self, client: MemoryClient, *, session_id: UUID) -> None:
        if type(client) is not MemoryClient or type(session_id) is not UUID:
            raise TypeError("durable session requires MemoryClient and UUID")
        if client.expected_instance_id is None:
            raise ValueError("expected_instance_required")
        self._client = client
        self._session_id = session_id
        self._progress: dict[UUID, DurableProgress] = {}

    def _key(self, operation: str, identity: UUID | None = None) -> UUID:
        return uuid5(
            MEMORY_REMEMBER_NAMESPACE,
            f"durable:{operation}:{self._session_id}:{identity}",
        )

    def persistence_progress(self, turn_id: UUID) -> DurableProgress | None:
        return self._progress.get(turn_id)

    async def open(self) -> SessionOperationResult:
        return await self._client.open_session(
            self._session_id, idempotency_key=self._key("open")
        )

    async def status(self, turn_id: UUID | None = None) -> SessionSnapshot:
        return await self._client.read_session(self._session_id, turn_id=turn_id)

    async def abandon(self, turn_id: UUID, *, reason: str) -> SessionOperationResult:
        return await self._client.abandon_turn(
            self._session_id,
            turn_id,
            reason=reason,
            idempotency_key=self._key("abandon", turn_id),
        )

    def _result(self, snapshot: SessionSnapshot) -> DurableTurnResult:
        assert snapshot.turn_id is not None and snapshot.attempt_id is not None
        assert snapshot.state != "open"
        state = "interrupted" if snapshot.state == "started" else snapshot.state
        completed = (
            None
            if snapshot.response is None
            else ModelTurn(snapshot.response, snapshot.observations)
        )
        receipt = None
        if state == "committed":
            receipt = PersistenceReceipt(
                PersistenceStatus.COMMITTED,
                snapshot.custody_idempotency_key,
                snapshot.custody_result,
                snapshot.custody_receipt,
                snapshot.custody_audit_receipt,
            )
        elif state == "skipped":
            receipt = PersistenceReceipt(PersistenceStatus.SKIPPED, None)
        self._progress[snapshot.turn_id] = DurableProgress(
            state,
            searchability="not-applicable" if state == "skipped" else "unconfirmed",
            last_confirmed_stage=snapshot.state,
        )
        return DurableTurnResult(
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.attempt_id,
            state,
            completed,
            receipt,
            snapshot,
        )

    def _failure(
        self,
        turn_id: UUID,
        error: BaseException,
        completed: ModelTurn | None,
        operation: str,
        stage: DurableStage,
    ) -> DurableSessionFailure:
        if isinstance(error, DurableSessionFailure):
            return error
        metadata = (
            error.failure
            if isinstance(error, MemoryOperationFailure)
            else FailureMetadata(
                "session_interrupted",
                "The turn outcome is unconfirmed.",
                "explicit",
                None,
                None,
            )
        )
        # A commit can ingest successfully and then fail fresh authority checks
        # before terminal recording. Even a definite denial cannot prove that
        # custody was not established. Retain prepared uncertainty on retries.
        pending = (
            operation == "turn-commit"
            or stage == "prepared"
            or metadata.code
            in {
                "transport_error",
                "transport_unavailable",
                "invalid_response",
                "http_error",
                "dependency_unavailable",
                "commit_outcome_unknown",
                "session_interrupted",
            }
        )
        state: Literal["pending", "failed"] = "pending" if pending else "failed"
        actual_operation = (
            error.operation if isinstance(error, MemoryOperationFailure) else operation
        )
        self._progress[turn_id] = DurableProgress(
            state, metadata.code, operation=actual_operation, last_confirmed_stage=stage
        )
        return DurableSessionFailure(
            actual_operation,
            metadata,
            session_id=self._session_id,
            turn_id=turn_id,
            state=state,
            completed_turn=completed,
            last_confirmed_stage=stage,
        )

    async def run_turn(
        self,
        user_input: str,
        model_callback: ModelCallback,
        *,
        turn_id: UUID,
        attempt_id: UUID,
        replaces_turn_id: UUID | None = None,
        budget: int = 16384,
        recall_query: str | None = None,
        relevant_only: bool = False,
    ) -> DurableTurnResult:
        if (
            type(turn_id) is not UUID
            or type(attempt_id) is not UUID
            or type(user_input) is not str
            or not callable(model_callback)
        ):
            raise TypeError("invalid_turn_arguments")
        completed: ModelTurn | None = None
        previous = self._progress.get(turn_id)
        stage: DurableStage = (
            previous.last_confirmed_stage if previous else "unconfirmed"
        )
        operation = "session-read"
        self._progress[turn_id] = DurableProgress(
            "processing", operation=operation, last_confirmed_stage=stage
        )
        try:
            # Check expected identity before even sending the recall query.
            await self.status()
            # A session-level read cannot supersede a known turn checkpoint.
            if stage == "unconfirmed":
                stage = "open"
            operation = "recall"
            recalled = await self._client.recall(
                _recall_query(user_input) if recall_query is None else recall_query,
                budget=budget,
                relevant_only=relevant_only,
            )
            operation = "turn-begin"
            begin = await self._client.begin_turn(
                self._session_id,
                turn_id,
                attempt_id=attempt_id,
                replaces_turn_id=replaces_turn_id,
                idempotency_key=self._key("begin", attempt_id),
            )
            if begin.outcome != "committed":
                return await self.resume(turn_id)
            stage = "started"
            operation = "model-callback"
            candidate = await model_callback(TurnInput(user_input, recalled))
            if type(candidate) is not ModelTurn:
                raise TypeError("model_callback must return ModelTurn")
            completed = candidate
            operation = "turn-prepare"
            prepared = await self._client.prepare_turn(
                self._session_id,
                turn_id,
                completed,
                attempt_id=attempt_id,
                idempotency_key=self._key("prepare", turn_id),
            )
            stage = "prepared"
            operation = "turn-commit"
            committed = await self._client.commit_turn(
                self._session_id, turn_id, idempotency_key=self._key("commit", turn_id)
            )
            require_preparation_binding(prepared.snapshot, committed.snapshot)
            return self._result(committed.snapshot)
        except BaseException as error:
            failure = self._failure(turn_id, error, completed, operation, stage)
            if not isinstance(error, Exception):
                raise
            raise failure from None

    async def resume(self, turn_id: UUID) -> DurableTurnResult:
        """Recover immutable output; deliberately accepts no callback."""
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        completed: ModelTurn | None = None
        previous = self._progress.get(turn_id)
        stage: DurableStage = (
            previous.last_confirmed_stage if previous else "unconfirmed"
        )
        operation = "session-read"
        try:
            snapshot = await self.status(turn_id)
            stage = snapshot.state
            if snapshot.state == "prepared":
                assert snapshot.response is not None
                completed = ModelTurn(snapshot.response, snapshot.observations)
                operation = "turn-commit"
                result = await self._client.commit_turn(
                    self._session_id,
                    turn_id,
                    idempotency_key=self._key("commit", turn_id),
                )
                require_preparation_binding(snapshot, result.snapshot)
                snapshot = result.snapshot
            return self._result(snapshot)
        except BaseException as error:
            failure = self._failure(turn_id, error, completed, operation, stage)
            if not isinstance(error, Exception):
                raise
            raise failure from None

    async def arrive(
        self,
        query: str,
        *,
        history_fact_ids: tuple[UUID, ...] = (),
        budget: int = 16384,
    ) -> DurableArrival:
        visit = await self._client.issue_visit(
            self._session_id, idempotency_key=uuid4()
        )
        snapshot = visit.snapshot
        assert snapshot.visit_at is not None
        diagnostics = await self._client.diagnose(
            expected_instance_id=self._client.expected_instance_id
        )
        if (
            diagnostics.status is not ConnectionStatus.READY
            or diagnostics.evaluated_at is None
        ):
            raise MemoryOperationFailure(
                "arrive",
                diagnostics.failure
                or FailureMetadata(
                    "invalid_response",
                    "Cairn returned invalid visit timing.",
                    "never",
                    None,
                    None,
                ),
            )
        future_checkpoint = snapshot.acknowledged_at is not None and (
            snapshot.acknowledged_at > snapshot.visit_at
            or snapshot.acknowledged_at > diagnostics.evaluated_at
        )
        rollback = future_checkpoint or (
            min(
                diagnostics.evaluated_at,
                parse_timestamp(cast(str, visit.audit_receipt["recorded_at"])),
            )
            < snapshot.visit_at
        )
        briefing = await build_arrival_briefing(
            self._client,
            query,
            since=None if rollback else snapshot.acknowledged_at,
            history_fact_ids=history_fact_ids,
            budget=budget,
            include_boundary=True,
        )
        if rollback:
            briefing = replace(
                briefing,
                warnings=(*briefing.warnings, "clock_rollback_changes_uncertain"),
            )
        if future_checkpoint:
            briefing = replace(
                briefing, warnings=(*briefing.warnings, "prior_checkpoint_in_future")
            )
        return DurableArrival(briefing, visit, rollback)

    async def acknowledge_visit(self, visit_id: UUID) -> SessionOperationResult:
        return await self._client.acknowledge_visit(
            self._session_id,
            visit_id,
            idempotency_key=self._key("acknowledge", visit_id),
        )
