"""Host-visible custody state survives slow writes and uncertain failures."""

import asyncio

import httpx
import pytest
from test_memory_client import (
    SESSION_ID,
    TURN_ID,
    _client,
    _recall_body,
    _remember_body,
)

from cairn.client import (
    DurableObservation,
    MemorySession,
    ModelTurn,
    PersistenceConflict,
    PersistenceFailure,
    RecallFailure,
    TurnInput,
)
from cairn.client.progress import PersistencePhase


@pytest.mark.anyio
async def test_processing_becomes_saved_only_after_valid_receipt() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("recall"):
            return httpx.Response(200, json=_recall_body())
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_remember_body())

    async def model(_context: TurnInput) -> ModelTurn:
        return ModelTurn("Completed", (DurableObservation("Durable observation"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        assert session.persistence_progress(TURN_ID) is None
        task = asyncio.create_task(session.run_turn("query", model, turn_id=TURN_ID))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            progress = session.persistence_progress(TURN_ID)
            assert progress is not None
            assert progress.phase is PersistencePhase.PROCESSING
            assert progress.receipt is None
        finally:
            release.set()
            result = await task
        progress = session.persistence_progress(TURN_ID)
        assert progress is not None
        assert progress.phase is PersistencePhase.SAVED
        assert progress.receipt == result.persistence
        assert progress.searchability == "unconfirmed"


@pytest.mark.anyio
async def test_uncertain_write_exposes_failed_then_retry_saves_without_model_replay() -> (
    None
):
    writes = callbacks = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.url.path.endswith("recall"):
            return httpx.Response(200, json=_recall_body())
        writes += 1
        if writes == 1:
            raise httpx.ReadTimeout("PRIVATE transport text", request=request)
        return httpx.Response(200, json=_remember_body("replayed"))

    async def model(_context: TurnInput) -> ModelTurn:
        nonlocal callbacks
        callbacks += 1
        return ModelTurn("Completed", (DurableObservation("Durable observation"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(PersistenceFailure) as caught:
            await session.run_turn("query", model, turn_id=TURN_ID)
        progress = session.persistence_progress(TURN_ID)
        assert progress is not None
        assert progress.phase is PersistencePhase.FAILED
        assert progress.failure_code == "transport_error"
        assert progress.receipt is None
        assert "PRIVATE" not in repr(progress)
        await session.retry_persistence(caught.value)
        progress = session.persistence_progress(TURN_ID)
        assert progress is not None
        assert progress.phase is PersistencePhase.SAVED
    assert callbacks == 1
    assert writes == 2


@pytest.mark.anyio
async def test_invalid_turn_identity_is_rejected_before_model_side_effects() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append("http")
        return httpx.Response(200, json=_recall_body())

    async def model(_context: TurnInput) -> ModelTurn:
        calls.append("model")
        return ModelTurn("Completed")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(TypeError, match="turn_id"):
            await session.run_turn("query", model, turn_id="wrong")  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.anyio
async def test_no_observations_is_skipped_not_saved() -> None:
    async with httpx.AsyncClient(base_url="https://cairn.invalid") as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        await session.persist_turn(TURN_ID, ModelTurn("Nothing durable"))
        progress = session.persistence_progress(TURN_ID)
    assert progress is not None
    assert progress.phase is PersistencePhase.SKIPPED
    assert progress.searchability == "not-applicable"


@pytest.mark.anyio
async def test_repeating_failed_turn_cannot_repeat_model_side_effects() -> None:
    callbacks = 0

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("recall"):
            return httpx.Response(200, json=_recall_body())
        raise httpx.ReadTimeout("lost receipt", request=request)

    async def model(_context: TurnInput) -> ModelTurn:
        nonlocal callbacks
        callbacks += 1
        return ModelTurn("Completed", (DurableObservation("Durable"),))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(PersistenceFailure):
            await session.run_turn("query", model, turn_id=TURN_ID)
        with pytest.raises(ValueError, match="already started"):
            await session.run_turn("query", model, turn_id=TURN_ID)
    assert callbacks == 1


@pytest.mark.anyio
async def test_failed_recall_can_retry_before_callback_has_started() -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(200, json=_recall_body())

    async def model(_context: TurnInput) -> ModelTurn:
        return ModelTurn("Completed")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        with pytest.raises(RecallFailure):
            await session.run_turn("query", model, turn_id=TURN_ID)
        assert session.persistence_progress(TURN_ID) is None
        result = await session.run_turn("query", model, turn_id=TURN_ID)
    assert result.response == "Completed"
    assert requests == 2


@pytest.mark.anyio
async def test_conflicting_payload_preserves_confirmed_original_receipt() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        original = ModelTurn("Completed", (DurableObservation("original"),))
        receipt = await session.persist_turn(TURN_ID, original)
        with pytest.raises(PersistenceConflict):
            await session.persist_turn(
                TURN_ID, ModelTurn("Different", (DurableObservation("different"),))
            )
        progress = session.persistence_progress(TURN_ID)
    assert progress is not None
    assert progress.phase is PersistencePhase.SAVED
    assert progress.receipt == receipt


@pytest.mark.anyio
async def test_cancellation_cannot_leave_a_write_claiming_processing_forever() -> None:
    entered = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        task = asyncio.create_task(
            session.persist_turn(
                TURN_ID, ModelTurn("Completed", (DurableObservation("Durable"),))
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        progress = session.persistence_progress(TURN_ID)
        assert progress is not None
        assert progress.phase is PersistencePhase.FAILED
        assert progress.failure_code == "persistence_interrupted"
        failure = session.persistence_failure(TURN_ID)
        assert failure is not None
        assert failure.completed_turn == ModelTurn(
            "Completed", (DurableObservation("Durable"),)
        )


@pytest.mark.anyio
@pytest.mark.parametrize("bad_body", [None, "\ud800"])
async def test_failed_repeat_cannot_erase_a_confirmed_custody_receipt(
    bad_body: str | None,
) -> None:
    writes = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        writes += 1
        if writes > 1:
            raise httpx.ReadTimeout("lost replay receipt", request=request)
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        original = ModelTurn("Completed", (DurableObservation("original"),))
        receipt = await session.persist_turn(TURN_ID, original)
        attempted = (
            original
            if bad_body is None
            else ModelTurn("Changed", (DurableObservation(bad_body),))
        )
        with pytest.raises(PersistenceFailure):
            await session.persist_turn(TURN_ID, attempted)
        progress = session.persistence_progress(TURN_ID)
        assert progress is not None
        assert progress.phase is PersistencePhase.SAVED
        assert progress.receipt == receipt


@pytest.mark.anyio
async def test_concurrent_persistence_preserves_both_completed_outputs() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_remember_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        original = ModelTurn("First", (DurableObservation("original"),))
        second = ModelTurn("Second", (DurableObservation("different"),))
        task = asyncio.create_task(session.persist_turn(TURN_ID, original))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            with pytest.raises(PersistenceFailure) as caught:
                await session.persist_turn(TURN_ID, second)
            assert caught.value.completed_turn == second
        finally:
            release.set()
            await task


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", ["different", "\ud800"])
async def test_rejected_replacement_cannot_overwrite_recoverable_original(
    replacement: str,
) -> None:
    writes = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise httpx.ReadTimeout("uncertain custody", request=request)
        return httpx.Response(200, json=_remember_body("replayed"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://cairn.invalid"
    ) as http:
        session = MemorySession(_client(http), session_id=SESSION_ID)
        original = ModelTurn("Original response", (DurableObservation("original"),))
        with pytest.raises(PersistenceFailure):
            await session.persist_turn(TURN_ID, original)
        with pytest.raises(PersistenceFailure):
            await session.persist_turn(
                TURN_ID,
                ModelTurn("Rejected response", (DurableObservation(replacement),)),
            )
        failure = session.persistence_failure(TURN_ID)
        assert failure is not None
        assert failure.completed_turn == original
        recovered = await session.retry_persistence(failure)
        assert recovered.response == "Original response"
        assert session.persistence_failure(TURN_ID) is None
    assert writes == 2
