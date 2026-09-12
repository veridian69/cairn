"""Content-free client failures which retain only safe transport metadata."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from cairn.client.types import DurableObservation, ModelTurn


@dataclass(frozen=True, slots=True)
class FailureMetadata:
    code: str
    message: str
    retry: str
    correlation_id: str | None
    status_code: int | None


class MemoryOperationFailure(Exception):
    """A recall or remember failure without request, credential, or body data."""

    __slots__ = ("failure", "operation")

    def __init__(self, operation: str, failure: FailureMetadata) -> None:
        self.operation = operation
        self.failure = failure
        super().__init__(f"memory {operation} failed: {failure.code}")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(operation={self.operation!r}, "
            f"code={self.failure.code!r}, status_code={self.failure.status_code!r})"
        )


class RecallFailure(MemoryOperationFailure):
    pass


class RememberFailure(MemoryOperationFailure):
    pass


class PersistenceFailure(Exception):
    """A completed model turn whose exact durable output was not confirmed."""

    __slots__ = (
        "_session_id",
        "_retry_context",
        "completed_turn",
        "failure",
        "idempotency_key",
        "turn_id",
    )

    def __init__(
        self,
        *,
        session_id: UUID,
        retry_context: object,
        turn_id: UUID,
        idempotency_key: UUID,
        completed_turn: ModelTurn,
        failure: FailureMetadata,
    ) -> None:
        self._session_id = session_id
        self._retry_context = retry_context
        self.turn_id = turn_id
        self.idempotency_key = idempotency_key
        self.completed_turn = completed_turn
        self.failure = failure
        super().__init__(f"turn persistence failed: {failure.code}")

    @property
    def response(self) -> str:
        return self.completed_turn.response

    @property
    def observations(self) -> tuple[DurableObservation, ...]:
        return self.completed_turn.observations

    def __repr__(self) -> str:
        return (
            f"PersistenceFailure(turn_id={self.turn_id!r}, "
            f"idempotency_key={self.idempotency_key!r}, "
            f"code={self.failure.code!r}, "
            f"status_code={self.failure.status_code!r})"
        )


class PersistenceConflict(PersistenceFailure):
    """A turn identity was reused for a different persistence payload."""

    def __init__(
        self,
        *,
        session_id: UUID,
        retry_context: object,
        turn_id: UUID,
        idempotency_key: UUID,
        completed_turn: ModelTurn,
    ) -> None:
        super().__init__(
            session_id=session_id,
            retry_context=retry_context,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            completed_turn=completed_turn,
            failure=FailureMetadata(
                code="idempotency_conflict",
                message="Turn identity was reused with different observations.",
                retry="never",
                correlation_id=None,
                status_code=None,
            ),
        )
