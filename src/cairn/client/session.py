"""Recall-before-model and remember-after-model turn orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from cairn.authority.retrieval import MAX_QUERY_BYTES
from cairn.catalogue.sqlite import canonical_timestamp
from cairn.client.briefing import ArrivalBriefing, build_arrival_briefing
from cairn.client.errors import (
    FailureMetadata,
    PersistenceConflict,
    PersistenceFailure,
    RememberFailure,
)
from cairn.client.memory import MemoryClient
from cairn.client.progress import PersistencePhase, PersistenceProgress
from cairn.client.types import (
    ModelCallback,
    ModelTurn,
    PersistenceReceipt,
    PersistenceStatus,
    TurnInput,
    TurnResult,
)
from cairn.session_identity import (
    MEMORY_REMEMBER_NAMESPACE as MEMORY_REMEMBER_NAMESPACE,
)
from cairn.session_identity import remember_key


def _idempotency_key(session_id: UUID, turn_id: UUID) -> UUID:
    return remember_key(session_id, turn_id)


def _payload_digest(turn: ModelTurn) -> bytes:
    facts = [
        {
            "body": item.body,
            "valid_from": (
                None
                if item.valid_from is None
                else canonical_timestamp(item.valid_from)
            ),
            "valid_to": (
                None if item.valid_to is None else canonical_timestamp(item.valid_to)
            ),
            "observed_at": (
                None
                if item.observed_at is None
                else canonical_timestamp(item.observed_at)
            ),
        }
        for item in turn.observations
    ]
    canonical = json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()


def _recall_query(user_input: str) -> str:
    """Retain cues from both ends within memory/v1's UTF-8 query limit."""
    safe = user_input.encode("utf-8", errors="replace").decode("utf-8")
    raw = safe.encode("utf-8")
    if len(raw) <= MAX_QUERY_BYTES:
        return safe
    marker = b"\n...\n"
    head_bytes = (MAX_QUERY_BYTES - len(marker)) // 2
    tail_bytes = MAX_QUERY_BYTES - len(marker) - head_bytes
    head = raw[:head_bytes].decode("utf-8", errors="ignore")
    tail = raw[-tail_bytes:].decode("utf-8", errors="ignore")
    return head + marker.decode("ascii") + tail


class MemorySession:
    """In-memory turn coordination; it owns no credential or durable queue."""

    __slots__ = (
        "_client",
        "_payloads",
        "_session_id",
        "_progress",
        "_started_turns",
        "_failures",
    )

    def __init__(self, client: MemoryClient, *, session_id: UUID) -> None:
        if type(client) is not MemoryClient:
            raise TypeError("client must be MemoryClient")
        if type(session_id) is not UUID:
            raise TypeError("session_id must be UUID")
        self._client = client
        self._session_id = session_id
        self._payloads: dict[UUID, bytes] = {}
        self._progress: dict[UUID, PersistenceProgress] = {}
        self._started_turns: set[UUID] = set()
        self._failures: dict[UUID, PersistenceFailure] = {}

    def persistence_progress(self, turn_id: UUID) -> PersistenceProgress | None:
        """Return current in-process custody state without initiating a request."""
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        return self._progress.get(turn_id)

    def persistence_failure(self, turn_id: UUID) -> PersistenceFailure | None:
        """Recover exact completed output after interruption in this process."""
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        return self._failures.get(turn_id)

    def _interrupted(
        self, turn_id: UUID, turn: ModelTurn, code: str = "persistence_interrupted"
    ) -> PersistenceFailure:
        return PersistenceFailure(
            session_id=self._session_id,
            retry_context=self._client._retry_context,
            turn_id=turn_id,
            idempotency_key=_idempotency_key(self._session_id, turn_id),
            completed_turn=turn,
            failure=FailureMetadata(
                code=code,
                message="Turn persistence was not confirmed.",
                retry="explicit",
                correlation_id=None,
                status_code=None,
            ),
        )

    async def arrive(
        self,
        query: str,
        *,
        since: datetime | None = None,
        history_fact_ids: tuple[UUID, ...] = (),
        budget: int = 16384,
    ) -> ArrivalBriefing:
        """Build a bounded briefing using this session's fixed memory context."""
        return await build_arrival_briefing(
            self._client,
            query,
            since=since,
            history_fact_ids=history_fact_ids,
            budget=budget,
        )

    async def run_turn(
        self,
        user_input: str,
        model_callback: ModelCallback,
        *,
        turn_id: UUID,
        budget: int = 16384,
        recall_query: str | None = None,
        relevant_only: bool = False,
    ) -> TurnResult:
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        if type(user_input) is not str:
            raise TypeError("user_input must be str")
        if type(relevant_only) is not bool:
            raise TypeError("relevant_only must be bool")
        if turn_id in self._started_turns or turn_id in self._progress:
            raise ValueError("turn already started; retry persistence separately")
        query = _recall_query(user_input) if recall_query is None else recall_query
        self._started_turns.add(turn_id)
        try:
            recalled = await self._client.recall(
                query, budget=budget, relevant_only=relevant_only
            )
        except BaseException:
            # No callback has begun, so retrying failed recall is still safe.
            self._started_turns.remove(turn_id)
            raise
        completed_turn = await model_callback(TurnInput(user_input, recalled))
        if type(completed_turn) is not ModelTurn:
            raise TypeError("model_callback must return ModelTurn")
        persistence = await self.persist_turn(turn_id, completed_turn)
        return TurnResult(completed_turn.response, persistence)

    async def persist_turn(self, turn_id: UUID, turn: ModelTurn) -> PersistenceReceipt:
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        if type(turn) is not ModelTurn:
            raise TypeError("turn must be ModelTurn")
        prior = self._progress.get(turn_id)
        if prior is not None and prior.phase is PersistencePhase.PROCESSING:
            raise self._interrupted(turn_id, turn, "persistence_in_progress")
        self._progress[turn_id] = PersistenceProgress(
            PersistencePhase.PROCESSING,
            receipt=None if prior is None else prior.receipt,
        )
        try:
            receipt = await self._persist_turn(turn_id, turn)
        except BaseException as error:
            # Cancellation must not leave a permanent in-progress indicator.
            # An attempted conflicting payload cannot erase a confirmed receipt.
            failure = (
                error
                if isinstance(error, PersistenceFailure)
                else self._interrupted(turn_id, turn)
            )
            if failure.failure.code in {"idempotency_conflict", "invalid_observations"}:
                # A rejected replacement must not strand the original completed
                # turn. The raised exception separately preserves rejected output.
                self._failures.setdefault(turn_id, failure)
            else:
                self._failures[turn_id] = failure
            if prior is not None and prior.phase in {
                PersistencePhase.SAVED,
                PersistencePhase.SKIPPED,
            }:
                self._progress[turn_id] = replace(
                    prior, failure_code=failure.failure.code
                )
            else:
                self._progress[turn_id] = PersistenceProgress(
                    PersistencePhase.FAILED, failure_code=failure.failure.code
                )
            if isinstance(error, Exception) and not isinstance(
                error, PersistenceFailure
            ):
                raise failure from None
            raise
        skipped = receipt.status is PersistenceStatus.SKIPPED
        self._progress[turn_id] = PersistenceProgress(
            PersistencePhase.SKIPPED if skipped else PersistencePhase.SAVED,
            receipt=receipt,
            searchability="not-applicable" if skipped else "unconfirmed",
        )
        self._failures.pop(turn_id, None)
        return receipt

    async def _persist_turn(self, turn_id: UUID, turn: ModelTurn) -> PersistenceReceipt:
        if type(turn_id) is not UUID:
            raise TypeError("turn_id must be UUID")
        if type(turn) is not ModelTurn:
            raise TypeError("turn must be ModelTurn")
        key = _idempotency_key(self._session_id, turn_id)
        try:
            digest = _payload_digest(turn)
        except (UnicodeError, ValueError):
            raise PersistenceFailure(
                session_id=self._session_id,
                retry_context=self._client._retry_context,
                turn_id=turn_id,
                idempotency_key=key,
                completed_turn=turn,
                failure=FailureMetadata(
                    code="invalid_observations",
                    message="Durable observations cannot form one remember batch.",
                    retry="never",
                    correlation_id=None,
                    status_code=None,
                ),
            ) from None
        prior = self._payloads.setdefault(turn_id, digest)
        if prior != digest:
            raise PersistenceConflict(
                session_id=self._session_id,
                retry_context=self._client._retry_context,
                turn_id=turn_id,
                idempotency_key=key,
                completed_turn=turn,
            )
        if not turn.observations:
            return await self._client.remember((), idempotency_key=key)
        try:
            return await self._client.remember(turn.observations, idempotency_key=key)
        except ValueError:
            raise PersistenceFailure(
                session_id=self._session_id,
                retry_context=self._client._retry_context,
                turn_id=turn_id,
                idempotency_key=key,
                completed_turn=turn,
                failure=FailureMetadata(
                    code="invalid_observations",
                    message="Durable observations cannot form one remember batch.",
                    retry="never",
                    correlation_id=None,
                    status_code=None,
                ),
            ) from None
        except RememberFailure as error:
            raise PersistenceFailure(
                session_id=self._session_id,
                retry_context=self._client._retry_context,
                turn_id=turn_id,
                idempotency_key=key,
                completed_turn=turn,
                failure=error.failure,
            ) from None

    async def retry_persistence(self, failure: PersistenceFailure) -> TurnResult:
        if not isinstance(failure, PersistenceFailure):
            raise TypeError("failure must be PersistenceFailure")
        if failure._session_id != self._session_id:
            raise ValueError("persistence failure belongs to another session")
        if failure._retry_context is not self._client._retry_context:
            raise PersistenceFailure(
                session_id=self._session_id,
                retry_context=failure._retry_context,
                turn_id=failure.turn_id,
                idempotency_key=failure.idempotency_key,
                completed_turn=failure.completed_turn,
                failure=FailureMetadata(
                    code="retry_context_mismatch",
                    message="Persistence retry used a different memory client.",
                    retry="never",
                    correlation_id=None,
                    status_code=None,
                ),
            )
        persistence = await self.persist_turn(failure.turn_id, failure.completed_turn)
        return TurnResult(failure.response, persistence)
