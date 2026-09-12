"""Tests for the pinned resolve_edge micro-batching seam (P-87)."""

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from io import StringIO
from typing import Any

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate
from graphiti_core.prompts.models import Message
from graphiti_core.utils.maintenance import edge_operations
from pydantic import BaseModel

import cairn.projection.graphiti_edge_batch as seam
from cairn.runtime.logging import configure_logging


def _context(new_edge: str = "A likes B") -> dict[str, object]:
    return {
        "existing_edges": ["fact one"],
        "edge_invalidation_candidates": [],
        "new_edge": new_edge,
    }


def test_batch_messages_render_one_block_per_item() -> None:
    messages = seam._batch_messages([_context("first"), _context("second")])
    assert len(messages) == 2
    assert messages[0].role == "system"
    body = messages[1].content
    assert "### ITEM 0" in body
    assert "### ITEM 1" in body
    assert "first" in body and "second" in body
    assert "2 independent ITEMs" in body


def test_batch_response_model_round_trips() -> None:
    parsed = seam.EdgeDuplicateBatch(
        **{
            "resolutions": [
                {"item": 0, "duplicate_facts": [1], "contradicted_facts": []},
                {"item": 1, "duplicate_facts": [], "contradicted_facts": [0]},
            ]
        }
    )
    assert parsed.resolutions[0].duplicate_facts == [1]
    assert parsed.resolutions[1].contradicted_facts == [0]


def test_compatibility_guard_refuses_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seam, "_GRAPHITI_CORE_VERSION", "0.29.4")
    with pytest.raises(RuntimeError, match="graphiti_edge_batch_compatibility_version"):
        seam._require_graphiti_edge_batch_compatibility()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeProvider:
    """Records generate_response calls and answers from a scripted queue."""

    def __init__(self, responses: list[object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def __call__(
        self,
        messages: Any,
        response_model: Any = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        **keywords: Any,
    ) -> object:
        self.calls.append(
            {
                "messages": messages,
                "response_model": response_model,
                "max_tokens": max_tokens,
                "model_size": model_size,
                "prompt_name": prompt_name,
                "keywords": keywords,
            }
        )
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _resolutions(*items: dict[str, object]) -> dict[str, object]:
    return {"resolutions": list(items)}


@pytest.mark.anyio
async def test_a_full_batch_issues_one_combined_medium_call() -> None:
    provider = _FakeProvider(
        [
            _resolutions(
                *[
                    {"item": i, "duplicate_facts": [i], "contradicted_facts": []}
                    for i in range(3)
                ]
            )
        ]
    )
    batcher = seam.EdgeBatcher(
        provider, batch_size=3, linger_seconds=60.0, max_facts=1000
    )
    results = await asyncio.gather(
        *[batcher.submit(_context(f"edge {i}"), max_tokens=None) for i in range(3)]
    )
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["prompt_name"] == "dedupe_edges.resolve_edge_batch"
    assert call["model_size"] is ModelSize.medium
    assert call["response_model"] is seam.EdgeDuplicateBatch
    for i, result in enumerate(results):
        assert result == {"duplicate_facts": [i], "contradicted_facts": []}


_SEAM_LOGGER = "cairn.projection.graphiti_edge_batch"


@pytest.mark.anyio
async def test_a_combined_call_logs_its_size_and_the_backlog_behind_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Without a success-path line, an inert batcher is indistinguishable
    # from a working one: every failure mode falls through to un-batched
    # single calls, which log nothing either. This is the positive
    # witness that a combined call actually happened.
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    class _GatedProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                await gate
                return _resolutions(
                    {"item": 0, "duplicate_facts": [], "contradicted_facts": []},
                    {"item": 1, "duplicate_facts": [], "contradicted_facts": []},
                )
            return {"duplicate_facts": [], "contradicted_facts": []}

    batcher = seam.EdgeBatcher(
        _GatedProvider(), batch_size=2, linger_seconds=0.01, max_facts=1000
    )
    with caplog.at_level(logging.DEBUG, logger=_SEAM_LOGGER):
        first = asyncio.ensure_future(batcher.submit(_context("sentinel-a"), None))
        second = asyncio.ensure_future(batcher.submit(_context("sentinel-b"), None))
        await asyncio.sleep(0)  # both queue; the second trips the size flush
        third = asyncio.ensure_future(batcher.submit(_context("sentinel-c"), None))
        await asyncio.sleep(0)  # the third queues behind the in-flight batch
        gate.set_result(None)
        await asyncio.wait_for(asyncio.gather(first, second, third), 2)

    combined = [
        record.getMessage()
        for record in caplog.records
        if "edge_batch_combined" in record.getMessage()
    ]
    assert len(combined) == 1
    assert "size=2" in combined[0]
    # The third item was queued while the combined call was outstanding,
    # so the backlog behind it is a real, non-zero number here.
    assert "pending=1" in combined[0]
    # No fact content or context anywhere in the log: counts only.
    assert "sentinel" not in caplog.text
    assert "fact one" not in caplog.text


@pytest.mark.anyio
async def test_results_map_by_item_number_not_arrival_order() -> None:
    provider = _FakeProvider(
        [
            _resolutions(
                {"item": 1, "duplicate_facts": [], "contradicted_facts": [7]},
                {"item": 0, "duplicate_facts": [3], "contradicted_facts": []},
            )
        ]
    )
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    first, second = await asyncio.gather(
        batcher.submit(_context("a"), None), batcher.submit(_context("b"), None)
    )
    assert first == {"duplicate_facts": [3], "contradicted_facts": []}
    assert second == {"duplicate_facts": [], "contradicted_facts": [7]}


def _two_empty_resolutions() -> dict[str, object]:
    return _resolutions(
        {"item": 0, "duplicate_facts": [], "contradicted_facts": []},
        {"item": 1, "duplicate_facts": [], "contradicted_facts": []},
    )


@pytest.mark.anyio
async def test_a_combined_call_asks_for_the_largest_requested_max_tokens() -> None:
    # max_tokens caps the response, and one combined response carries
    # every item's answer, so the smallest request must not set the cap.
    # The larger budget arrives second, so reading only the first item's
    # value would silently under-budget the combined call.
    provider = _FakeProvider([_two_empty_resolutions()])
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    await asyncio.gather(
        batcher.submit(_context("a"), 100), batcher.submit(_context("b"), 500)
    )
    assert provider.calls[0]["max_tokens"] == 500


@pytest.mark.anyio
async def test_a_combined_call_ignores_items_that_asked_for_no_budget() -> None:
    provider = _FakeProvider([_two_empty_resolutions()])
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    await asyncio.gather(
        batcher.submit(_context("a"), None), batcher.submit(_context("b"), 250)
    )
    assert provider.calls[0]["max_tokens"] == 250


@pytest.mark.anyio
async def test_a_combined_call_stays_unbudgeted_when_no_item_asked() -> None:
    # The path graphiti 0.29.3 actually takes: it never passes
    # max_tokens, so folding an empty set must yield None, not raise.
    provider = _FakeProvider([_two_empty_resolutions()])
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    await asyncio.gather(
        batcher.submit(_context("a"), None), batcher.submit(_context("b"), None)
    )
    assert provider.calls[0]["max_tokens"] is None


@pytest.mark.anyio
async def test_an_unanswered_item_defaults_conservatively_with_one_safe_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _FakeProvider(
        [_resolutions({"item": 0, "duplicate_facts": [9], "contradicted_facts": [4]})]
    )
    stream = StringIO()
    batcher = seam.EdgeBatcher(
        provider,
        batch_size=2,
        linger_seconds=60.0,
        max_facts=1000,
        logger=configure_logging(stream),
    )
    with caplog.at_level(logging.WARNING, logger=_SEAM_LOGGER):
        answered, orphan = await asyncio.gather(
            batcher.submit(_context("answered"), None),
            batcher.submit(_context("orphaned"), None),
        )
    # item 0's payload is distinguishable from the conservative default,
    # so this would fail if the item mapping broke and both items ended
    # up with the same (conservative) result.
    assert answered == {"duplicate_facts": [9], "contradicted_facts": [4]}
    assert orphan == {"duplicate_facts": [], "contradicted_facts": []}
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(payloads) == 1
    assert payloads[0] == {
        "answered": 1,
        "event": "edge_batch_item_missing",
        "item": 1,
        "size": 2,
        "time": payloads[0]["time"],
    }
    assert not any(
        "edge_batch_item_missing" in record.getMessage() for record in caplog.records
    )
    assert "orphaned" not in stream.getvalue()
    assert "fact one" not in stream.getvalue()


@pytest.mark.anyio
async def test_an_unanswered_item_is_silent_without_a_safe_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _FakeProvider(
        [_resolutions({"item": 0, "duplicate_facts": [], "contradicted_facts": []})]
    )
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    with caplog.at_level(logging.WARNING, logger=_SEAM_LOGGER):
        results = await asyncio.gather(
            batcher.submit(_context("answered"), None),
            batcher.submit(_context("orphaned"), None),
        )
    assert results[1] == {"duplicate_facts": [], "contradicted_facts": []}
    assert not any(
        "edge_batch_item_missing" in record.getMessage() for record in caplog.records
    )


@pytest.mark.anyio
async def test_a_failed_combined_call_fails_every_waiter() -> None:
    error = RuntimeError("provider down")
    provider = _FakeProvider([error])
    batcher = seam.EdgeBatcher(
        provider, batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    results = await asyncio.gather(
        batcher.submit(_context("a"), None),
        batcher.submit(_context("b"), None),
        return_exceptions=True,
    )
    # Every waiter fails with the SAME exception object, not merely the
    # same exception type.
    assert len(results) == 2
    assert all(r is error for r in results)


@pytest.mark.anyio
async def test_the_linger_flushes_a_partial_batch() -> None:
    # A lone item takes the single path, whose provider response is
    # EdgeDuplicate-shaped and returned verbatim — no batch envelope.
    provider = _FakeProvider([{"duplicate_facts": [], "contradicted_facts": []}])
    batcher = seam.EdgeBatcher(
        provider, batch_size=10, linger_seconds=0.01, max_facts=1000
    )
    result = await asyncio.wait_for(batcher.submit(_context("solo"), None), 2)
    # A lone item takes the un-batched single path, exactly as graphiti
    # would have issued it.
    assert provider.calls[0]["prompt_name"] == "dedupe_edges.resolve_edge"
    assert provider.calls[0]["model_size"] is ModelSize.small
    assert provider.calls[0]["response_model"] is EdgeDuplicate
    assert result == {"duplicate_facts": [], "contradicted_facts": []}


@pytest.mark.anyio
async def test_the_candidate_fact_ceiling_flushes_early() -> None:
    provider = _FakeProvider(
        [
            _resolutions(
                {"item": 0, "duplicate_facts": [], "contradicted_facts": []},
                {"item": 1, "duplicate_facts": [], "contradicted_facts": []},
            )
        ]
    )
    batcher = seam.EdgeBatcher(
        provider, batch_size=10, linger_seconds=60.0, max_facts=3
    )
    # Each item contributes 2 candidate facts (one existing edge, one
    # invalidation candidate): one item alone (2) stays under the
    # ceiling (3), but the pair (4) trips it — batch_size (10) and the
    # linger (60s) are both out of reach, so only the ceiling can be
    # what causes this flush.
    wide = {
        "existing_edges": ["a"],
        "edge_invalidation_candidates": ["b"],
        "new_edge": "wide",
    }
    await asyncio.gather(batcher.submit(wide, None), batcher.submit(wide, None))
    assert len(provider.calls) == 1
    assert provider.calls[0]["prompt_name"] == "dedupe_edges.resolve_edge_batch"


@pytest.mark.anyio
async def test_a_foreign_loop_is_served_individually() -> None:
    # Both calls here take the single path (the first because a lone
    # item flushes to it, the second because its loop is foreign), so
    # both scripted responses are EdgeDuplicate-shaped.
    provider = _FakeProvider(
        [
            {"duplicate_facts": [], "contradicted_facts": []},
            {"duplicate_facts": [], "contradicted_facts": []},
        ]
    )
    batcher = seam.EdgeBatcher(
        provider, batch_size=10, linger_seconds=0.01, max_facts=1000
    )
    await batcher.submit(_context("binds the loop"), None)

    foreign_result: list[object] = []

    def foreign() -> None:
        foreign_result.append(asyncio.run(batcher.submit(_context("foreign"), None)))

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(timeout=5)
    assert foreign_result == [{"duplicate_facts": [], "contradicted_facts": []}]
    assert provider.calls[-1]["prompt_name"] == "dedupe_edges.resolve_edge"
    assert provider.calls[0]["response_model"] is EdgeDuplicate
    assert provider.calls[-1]["response_model"] is EdgeDuplicate


@pytest.mark.anyio
async def test_the_flush_task_is_tracked_for_its_whole_lifetime() -> None:
    # Regression test for the liveness hazard: asyncio's event loop
    # holds only a WEAK reference to a task, so EdgeBatcher must anchor
    # its own flush task with a strong reference for as long as it
    # runs, or it can be collected mid-await and strand every waiter.
    #
    # A black-box "the task actually got garbage-collected mid-await"
    # reproduction isn't attainable: unblocking the gated provider call
    # from outside requires an external reference to the future it
    # awaits, and that future's callback list already references the
    # task back — so the same external reference that makes the test
    # controllable also roots the task, defeating the premise. This
    # instead drives the actual invariant the fix establishes: the
    # task is present in `_inflight` while the provider call is
    # outstanding, and removed once it settles.
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    class _GatedProvider:
        async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, object]:
            await gate
            return {"duplicate_facts": [], "contradicted_facts": []}

    batcher = seam.EdgeBatcher(
        _GatedProvider(), batch_size=1, linger_seconds=60.0, max_facts=1000
    )
    submitted = asyncio.ensure_future(batcher.submit(_context("a"), None))
    await asyncio.sleep(0)  # let submit()'s synchronous flush run
    assert len(batcher._inflight) == 1
    gate.set_result(None)
    result = await asyncio.wait_for(submitted, 2)
    await asyncio.sleep(0)  # let the flush task's done-callback fire
    assert result == {"duplicate_facts": [], "contradicted_facts": []}
    assert batcher._inflight == set()


@pytest.mark.anyio
async def test_a_cancelled_flush_task_fails_every_waiter() -> None:
    # Regression test for the second face of the liveness hazard: a
    # cancelled flush task must still fail every waiting future rather
    # than stranding it, even though CancelledError itself continues
    # to propagate out of the flush task per asyncio convention.
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    class _GatedProvider:
        async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, object]:
            await gate
            return {"duplicate_facts": [], "contradicted_facts": []}

    batcher = seam.EdgeBatcher(
        _GatedProvider(), batch_size=2, linger_seconds=60.0, max_facts=1000
    )
    first = asyncio.ensure_future(batcher.submit(_context("a"), None))
    second = asyncio.ensure_future(batcher.submit(_context("b"), None))
    await asyncio.sleep(0)  # let both submits run; the second flushes
    await asyncio.sleep(0)  # let the flush task itself run to `await gate`
    assert len(batcher._inflight) == 1
    (flush_task,) = batcher._inflight
    flush_task.cancel()

    for waiter in (first, second):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 2)
    await asyncio.sleep(0)  # let the flush task's done-callback fire
    assert batcher._inflight == set()


@pytest.mark.anyio
async def test_a_cancelled_submit_leaves_nothing_queued() -> None:
    # submit() appends its entry then awaits. GraphitiIndex._call's
    # timeout and shutdown paths cancel that await, and nothing removed
    # the entry: the armed linger timer would later flush it and issue a
    # real provider call for a request already declared quiescent.
    provider = _FakeProvider([])
    batcher = seam.EdgeBatcher(
        provider, batch_size=10, linger_seconds=0.01, max_facts=1000
    )
    submitted = asyncio.ensure_future(batcher.submit(_context("abandoned"), None))
    await asyncio.sleep(0)  # let it queue and await its future
    assert len(batcher._pending) == 1
    submitted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitted
    assert batcher._pending == []
    # Past the linger: the timer must find nothing left to send.
    await asyncio.sleep(0.05)
    assert provider.calls == []


@pytest.mark.anyio
async def test_a_flush_skips_entries_whose_waiter_already_went_away() -> None:
    # The other half of the same hazard, and the reason submit()'s own
    # cleanup is not sufficient: cancellation is only delivered when the
    # submitting task next runs, so a flush landing first would still
    # slice a dead entry into a live provider call.
    provider = _FakeProvider([])
    batcher = seam.EdgeBatcher(
        provider, batch_size=10, linger_seconds=60.0, max_facts=1000
    )
    submitted = asyncio.ensure_future(batcher.submit(_context("abandoned"), None))
    await asyncio.sleep(0)  # let it queue and await its future
    assert len(batcher._pending) == 1
    # Cancel the future and flush before the submitting task resumes,
    # so submit()'s cleanup has demonstrably not run yet.
    batcher._pending[0][1].cancel()
    batcher._flush()
    await asyncio.sleep(0)
    assert provider.calls == []
    with pytest.raises(asyncio.CancelledError):
        await submitted


@pytest.fixture(autouse=True)
def _restore_prompt_library(monkeypatch: pytest.MonkeyPatch) -> None:
    # Captures whatever prompt_library is at the START of this test and
    # restores exactly that object at teardown, regardless of what the
    # test (or install_edge_batching) reassigns it to meanwhile. This
    # holds under xdist's unordered test execution, unlike restoring by
    # test order. The Any-typed alias sidesteps edge_operations' lack of
    # an __all__ entry for prompt_library under --no-implicit-reexport.
    module: Any = edge_operations
    monkeypatch.setattr(edge_operations, "prompt_library", module.prompt_library)


class _FakeClient:
    def __init__(self, provider: _FakeProvider) -> None:
        self.generate_response = provider


@pytest.mark.anyio
async def test_install_routes_context_carrying_resolve_edge_calls() -> None:
    provider = _FakeProvider(
        [
            _resolutions(
                {"item": 0, "duplicate_facts": [], "contradicted_facts": []},
                {"item": 1, "duplicate_facts": [], "contradicted_facts": []},
            )
        ]
    )
    client = _FakeClient(provider)
    batcher = seam.install_edge_batching(
        client, batch_size=2, linger_ms=75, max_facts=1000
    )
    assert batcher is not None
    messages_a = seam._ContextMessages([Message(role="user", content="rendered")])
    messages_a.cairn_edge_context = _context("a")
    messages_b = seam._ContextMessages([Message(role="user", content="rendered")])
    messages_b.cairn_edge_context = _context("b")
    result_a, result_b = await asyncio.gather(
        client.generate_response(messages_a, prompt_name="dedupe_edges.resolve_edge"),
        client.generate_response(messages_b, prompt_name="dedupe_edges.resolve_edge"),
    )
    assert len(provider.calls) == 1
    assert provider.calls[0]["prompt_name"] == "dedupe_edges.resolve_edge_batch"
    assert result_a == {"duplicate_facts": [], "contradicted_facts": []}
    assert result_b == {"duplicate_facts": [], "contradicted_facts": []}


@pytest.mark.anyio
async def test_resolve_edge_without_context_passes_through_unbatched() -> None:
    # The routing condition is a conjunction (prompt_name AND context); this
    # pins the context-is-None half, which is exactly what keeps
    # _single_resolve's unproxied call from re-entering routed.
    provider = _FakeProvider([{"duplicate_facts": [], "contradicted_facts": []}])
    client = _FakeClient(provider)
    seam.install_edge_batching(client, batch_size=2, linger_ms=75, max_facts=1000)
    result = await client.generate_response(
        ["plain"], prompt_name="dedupe_edges.resolve_edge"
    )
    assert result == {"duplicate_facts": [], "contradicted_facts": []}
    assert provider.calls[0]["prompt_name"] == "dedupe_edges.resolve_edge"


@pytest.mark.anyio
async def test_a_context_free_resolve_edge_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A resolve_edge call reaching routed() without context means the
    # prompt proxy is not carrying it — the batcher is silently inert.
    # _single_resolve bypasses routed() via the captured original, so
    # this genuinely cannot happen in a healthy install. Warn once: it
    # is a standing condition, not a per-call event.
    provider = _FakeProvider(
        [
            {"duplicate_facts": [], "contradicted_facts": []},
            {"duplicate_facts": [], "contradicted_facts": []},
        ]
    )
    client = _FakeClient(provider)
    seam.install_edge_batching(client, batch_size=2, linger_ms=75, max_facts=1000)
    with caplog.at_level(logging.WARNING, logger=_SEAM_LOGGER):
        await client.generate_response(
            ["plain"], prompt_name="dedupe_edges.resolve_edge"
        )
        await client.generate_response(
            ["plain"], prompt_name="dedupe_edges.resolve_edge"
        )
    warnings = [
        record
        for record in caplog.records
        if "edge_batch_context_missing" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


@pytest.mark.anyio
async def test_other_prompts_pass_through_unbatched() -> None:
    provider = _FakeProvider([{"answer": "unrelated"}])
    client = _FakeClient(provider)
    seam.install_edge_batching(client, batch_size=2, linger_ms=75, max_facts=1000)
    result = await client.generate_response(
        ["plain"],
        prompt_name="extract_nodes.extract_text",
        attribute_extraction=True,
    )
    assert result == {"answer": "unrelated"}
    assert provider.calls[0]["prompt_name"] == "extract_nodes.extract_text"
    assert provider.calls[0]["keywords"] == {"attribute_extraction": True}


@pytest.mark.anyio
async def test_a_second_install_returns_the_existing_batcher() -> None:
    provider = _FakeProvider([])
    client = _FakeClient(provider)
    first_batcher = seam.install_edge_batching(
        client, batch_size=2, linger_ms=75, max_facts=1000
    )
    wrapped = client.generate_response
    second_batcher = seam.install_edge_batching(
        client, batch_size=2, linger_ms=75, max_facts=1000
    )
    assert second_batcher is first_batcher
    assert client.generate_response is wrapped


def test_a_batch_size_of_one_installs_nothing() -> None:
    provider = _FakeProvider([])
    client = _FakeClient(provider)
    assert (
        seam.install_edge_batching(client, batch_size=1, linger_ms=75, max_facts=1000)
        is None
    )
    assert client.generate_response is provider
    module: Any = edge_operations
    assert not isinstance(module.prompt_library, seam._PromptLibraryProxy)


def test_the_prompt_proxy_installs_once_and_carries_context() -> None:
    provider = _FakeProvider([])
    seam.install_edge_batching(
        _FakeClient(provider), batch_size=2, linger_ms=75, max_facts=1000
    )
    module: Any = edge_operations
    first = module.prompt_library
    seam.install_edge_batching(
        _FakeClient(provider), batch_size=2, linger_ms=75, max_facts=1000
    )
    assert module.prompt_library is first
    rendered = module.prompt_library.dedupe_edges.resolve_edge(_context("carried"))
    assert rendered.cairn_edge_context == _context("carried")
    # The wrapped section still renders graphiti's own messages.
    assert len(list(rendered)) > 0


@pytest.mark.anyio
async def test_graphiti_hands_the_rendered_messages_through_unsliced() -> None:
    # The whole seam rests on this: edge_operations passes the object
    # returned by prompt_library.dedupe_edges.resolve_edge STRAIGHT to
    # generate_response (edge_operations.py:726-731). Any slice or
    # list() copy along the way drops the cairn_edge_context attribute
    # — the batcher would go inert and every call would quietly fall
    # back to un-batched. Nothing else in this suite drives the real
    # graphiti call site, so a pass-through change upstream would
    # otherwise land silently.
    seam._install_prompt_proxy()

    received: list[Any] = []

    class _RecordingClient(LLMClient):
        # A real LLMClient subclass, not a duck type: resolve_extracted
        # _edge is typed against the ABC, and the messages object this
        # test inspects only survives if the real call path is used.
        def __init__(self) -> None:
            super().__init__(config=None)

        async def _generate_response(
            self,
            messages: list[Message],
            response_model: type[BaseModel] | None = None,
            max_tokens: int = DEFAULT_MAX_TOKENS,
            model_size: ModelSize = ModelSize.medium,
        ) -> dict[str, Any]:
            raise AssertionError("the recorder answers before reaching the provider")

        async def generate_response(
            self,
            messages: list[Message],
            response_model: type[BaseModel] | None = None,
            max_tokens: int | None = None,
            model_size: ModelSize = ModelSize.medium,
            group_id: str | None = None,
            prompt_name: str | None = None,
            *,
            attribute_extraction: bool = False,
        ) -> dict[str, Any]:
            received.append(messages)
            # Resolving to a duplicate returns an existing edge, which
            # skips the timestamp-extraction call this test does not
            # script.
            return {"duplicate_facts": [0], "contradicted_facts": []}

    now = datetime.now(UTC)
    extracted = EntityEdge(
        group_id="g",
        source_node_uuid="source",
        target_node_uuid="target",
        created_at=now,
        name="LIKES",
        fact="A likes B",
    )
    # A different fact text, or the verbatim-match fast path at
    # edge_operations.py:684-695 would return before any provider call.
    related = EntityEdge(
        group_id="g",
        source_node_uuid="source",
        target_node_uuid="target",
        created_at=now,
        name="LIKES",
        fact="A is fond of B",
    )
    episode = EpisodicNode(
        name="episode",
        group_id="g",
        source=EpisodeType.message,
        source_description="test",
        content="A likes B",
        valid_at=now,
    )

    await edge_operations.resolve_extracted_edge(
        _RecordingClient(), extracted, [related], [], episode, None
    )

    assert len(received) == 1
    context = getattr(received[0], "cairn_edge_context", None)
    assert context is not None
    assert context["new_edge"] == "A likes B"
    assert context["existing_edges"] == [{"idx": 0, "fact": "A is fond of B"}]


def test_install_refuses_version_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seam, "_GRAPHITI_CORE_VERSION", "0.30.0")
    with pytest.raises(RuntimeError, match="graphiti_edge_batch_compatibility_version"):
        seam.install_edge_batching(
            _FakeClient(_FakeProvider([])), batch_size=2, linger_ms=75, max_facts=1000
        )
