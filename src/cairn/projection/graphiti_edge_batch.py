"""Pinned resolve_edge micro-batching compatibility seam (P-87).

Graphiti 0.29.3 issues one ``dedupe_edges.resolve_edge`` provider call
per extracted edge — 69.2% of all provider busy time in the P-86
calibration. This module coalesces concurrent calls into combined
calls on the medium model path: measured 27 August 2026, batch-on-small
lost dedup accuracy against single calls (11/15 vs 13/15) while
batch-on-medium matched them (13/15), with zero false duplicates
either way. Delete it when the pinned dependency batches edge
resolution itself.

``install_edge_batching`` mutates a process global: it rebinds
``edge_operations.prompt_library`` to a proxy. Any test calling it with
``batch_size >= 2`` must restore that global afterwards, as the autouse
fixture in ``tests/projection/test_graphiti_edge_batch.py`` does.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any

from graphiti_core.llm_client.config import ModelSize
from graphiti_core.prompts.models import Message
from graphiti_core.utils.maintenance import edge_operations
from pydantic import BaseModel, Field

from cairn.runtime.logging import LogEvent, SafeLogger

_LOGGER = logging.getLogger(__name__)

_GRAPHITI_CORE_VERSION = version("graphiti-core")
_GRAPHITI_EDGE_BATCH_COMPATIBILITY_VERSION = "0.29.3"


def _require_graphiti_edge_batch_compatibility() -> None:
    if _GRAPHITI_CORE_VERSION != _GRAPHITI_EDGE_BATCH_COMPATIBILITY_VERSION:
        raise RuntimeError("graphiti_edge_batch_compatibility_version")


class EdgeDuplicateItem(BaseModel):
    item: int = Field(..., description="The ITEM number this resolution answers.")
    duplicate_facts: list[int] = Field(
        ...,
        description="idx values of duplicate facts from this item's EXISTING FACTS only. Empty list if none.",
    )
    contradicted_facts: list[int] = Field(
        ...,
        description="idx values this item's NEW FACT contradicts, from either of its lists. Empty list if none.",
    )


class EdgeDuplicateBatch(BaseModel):
    resolutions: list[EdgeDuplicateItem] = Field(
        ..., description="Exactly one resolution per ITEM, in item order."
    )


def _batch_messages(contexts: list[dict[str, Any]]) -> list[Message]:
    blocks = []
    for index, context in enumerate(contexts):
        blocks.append(
            f"### ITEM {index}\n"
            f"<EXISTING FACTS>\n{context['existing_edges']}\n</EXISTING FACTS>\n"
            f"<FACT INVALIDATION CANDIDATES>\n{context['edge_invalidation_candidates']}\n"
            f"</FACT INVALIDATION CANDIDATES>\n"
            f"<NEW FACT>\n{context['new_edge']}\n</NEW FACT>"
        )
    body = "\n\n".join(blocks)
    return [
        Message(
            role="system",
            content="You are a fact deduplication assistant. "
            "NEVER mark facts with key differences as duplicates.",
        ),
        Message(
            role="user",
            content=f"""
You are given {len(contexts)} independent ITEMs. Resolve each ITEM separately,
using ONLY that item's own lists — never compare facts across items. Return
exactly one resolution per ITEM, carrying its item number.

Within each ITEM, idx numbering is continuous across its two lists: its
EXISTING FACTS are indexed first, followed by its FACT INVALIDATION
CANDIDATES.

NEVER mark facts as duplicates if they have key differences, particularly
around numeric values, dates, or key qualifiers.

IMPORTANT constraints, per item:
- duplicate_facts: ONLY idx values from that item's EXISTING FACTS (NEVER
  include FACT INVALIDATION CANDIDATES)
- contradicted_facts: idx values from EITHER of that item's lists
- If the NEW FACT represents identical factual information as an EXISTING
  FACT, its idx belongs in duplicate_facts; an EXISTING FACT can be both a
  duplicate AND contradicted when the new fact updates or supersedes it.
- Empty lists when there are no duplicates or contradictions.

{body}
""",
        ),
    ]


async def _single_resolve(
    original_call: Any, context: dict[str, Any], max_tokens: int | None
) -> dict[str, Any]:
    """One un-batched resolve_edge call, exactly as graphiti would issue it."""
    # Deliberately the UNPROXIED prompt_library — _install_prompt_proxy only
    # ever rebinds edge_operations.prompt_library, never this one. That is
    # what keeps these messages free of cairn_edge_context, which is one of
    # the two independent guards stopping this single-item call from
    # re-entering routed. Do not "fix" this to use the proxied library.
    from graphiti_core.prompts import prompt_library
    from graphiti_core.prompts.dedupe_edges import EdgeDuplicate

    result: dict[str, Any] = await original_call(
        list(prompt_library.dedupe_edges.resolve_edge(context)),
        response_model=EdgeDuplicate,
        max_tokens=max_tokens,
        model_size=ModelSize.small,
        group_id=None,
        prompt_name="dedupe_edges.resolve_edge",
    )
    return result


class EdgeBatcher:
    """Coalesce concurrent resolve_edge calls into combined provider calls.

    Single-event-loop by design: Cairn's graphiti adapter owns one loop
    and every resolve_edge call awaits on it. A call arriving on a
    different loop is served individually rather than batched.
    """

    def __init__(
        self,
        original_call: Any,
        *,
        batch_size: int,
        linger_seconds: float,
        max_facts: int,
        logger: SafeLogger | None = None,
    ) -> None:
        self._original_call = original_call
        self._batch_size = batch_size
        self._linger_seconds = linger_seconds
        self._max_facts = max_facts
        self._logger = logger
        self._pending: list[
            tuple[dict[str, Any], asyncio.Future[dict[str, Any]], int | None]
        ] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        # Strong references: asyncio holds only a weak reference to a
        # task, so an unanchored task can be garbage-collected mid-await.
        self._inflight: set[asyncio.Task[None]] = set()

    def _candidate_count(self, context: dict[str, Any]) -> int:
        return len(context.get("existing_edges") or []) + len(
            context.get("edge_invalidation_candidates") or []
        )

    async def submit(
        self, context: dict[str, Any], max_tokens: int | None
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if loop is not self._loop:
            return await _single_resolve(self._original_call, context, max_tokens)
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending.append((context, future, max_tokens))
        pending_facts = sum(self._candidate_count(c) for c, _, _ in self._pending)
        if len(self._pending) >= self._batch_size or pending_facts >= self._max_facts:
            self._flush()
        elif self._timer is None:
            self._timer = loop.call_later(self._linger_seconds, self._flush)
        try:
            return await future
        except asyncio.CancelledError:
            # GraphitiIndex._call cancels this await on timeout and at
            # shutdown. Drop the entry: a later flush must not issue a
            # provider call for a request already declared quiescent.
            # A no-op if the entry was already sliced into a batch.
            self._pending = [item for item in self._pending if item[1] is not future]
            raise

    def _flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        # Cancellation reaches submit() only when its task next runs,
        # which can be after this flush; without this, a dead entry
        # would still be sliced into a live provider call.
        self._pending = [item for item in self._pending if not item[1].done()]
        if not self._pending:
            return
        batch, self._pending = (
            self._pending[: self._batch_size],
            self._pending[self._batch_size :],
        )
        assert self._loop is not None
        task = self._loop.create_task(self._run(batch))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        if self._pending:
            self._timer = self._loop.call_later(self._linger_seconds, self._flush)

    async def _run(
        self,
        batch: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]], int | None]],
    ) -> None:
        contexts = [context for context, _, _ in batch]
        # max_tokens caps the response, and one combined response covers
        # every item, so the batch needs at least the largest budget any
        # single item asked for — taking one item's value could truncate
        # the answer for the rest. Graphiti 0.29.3 never passes one
        # (edge_operations.py:726-731), so today this always folds to None.
        budgets = [tokens for _, _, tokens in batch if tokens is not None]
        max_tokens = max(budgets) if budgets else None
        try:
            if len(batch) == 1:
                result = await _single_resolve(
                    self._original_call, contexts[0], max_tokens
                )
                if not batch[0][1].done():
                    batch[0][1].set_result(result)
                return
            response = await self._original_call(
                _batch_messages(contexts),
                response_model=EdgeDuplicateBatch,
                max_tokens=max_tokens,
                model_size=ModelSize.medium,
                group_id=None,
                prompt_name="dedupe_edges.resolve_edge_batch",
            )
            parsed = EdgeDuplicateBatch(**response)
        except BaseException as error:
            for _, future, _ in batch:
                if not future.done():
                    future.set_exception(error)
            # Anything that is not a normal Exception (CancelledError,
            # KeyboardInterrupt, SystemExit, ...) must still propagate
            # out of this task per asyncio convention; the waiters
            # above are already unblocked with the same exception,
            # satisfying "every waiter fails" regardless.
            if not isinstance(error, Exception):
                raise
            return
        # The success-path witness. Every failure mode of this seam
        # degrades to un-batched single calls, which look exactly like a
        # healthy un-installed client; without this line an inert
        # batcher is indistinguishable from a working one. `pending` is
        # the backlog that accumulated while this call was outstanding.
        _LOGGER.debug(
            "edge_batch_combined size=%d pending=%d", len(batch), len(self._pending)
        )
        by_item = {resolution.item: resolution for resolution in parsed.resolutions}
        for index, (_, future, _) in enumerate(batch):
            resolution = by_item.get(index)
            if resolution is None:
                # Conservative: an unanswered item dedupes against
                # nothing rather than guessing; content-free log.
                # `answered` distinguishes "the model dropped one item"
                # from "the model is answering with the wrong item
                # numbering and dedup is globally off" — those look
                # identical without it.
                if self._logger is not None:
                    self._logger.emit(
                        LogEvent.EDGE_BATCH_ITEM_MISSING,
                        transport=None,
                        level=logging.WARNING,
                        item=index,
                        size=len(batch),
                        answered=len(parsed.resolutions),
                    )
                payload: dict[str, Any] = {
                    "duplicate_facts": [],
                    "contradicted_facts": [],
                }
            else:
                payload = {
                    "duplicate_facts": resolution.duplicate_facts,
                    "contradicted_facts": resolution.contradicted_facts,
                }
            if not future.done():
                future.set_result(payload)


class _ContextMessages(list[Message]):
    """The rendered messages, carrying the structured context they came from."""

    cairn_edge_context: dict[str, Any]


class _DedupeSectionProxy:
    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def resolve_edge(self, context: dict[str, Any]) -> _ContextMessages:
        messages = _ContextMessages(self._real.resolve_edge(context))
        messages.cairn_edge_context = context
        return messages


class _PromptLibraryProxy:
    def __init__(self, real: Any) -> None:
        self._real = real
        self._dedupe = _DedupeSectionProxy(real.dedupe_edges)

    def __getattr__(self, name: str) -> Any:
        if name == "dedupe_edges":
            return self._dedupe
        return getattr(self._real, name)


def _install_prompt_proxy() -> None:
    # edge_operations doesn't list prompt_library in an __all__, so a
    # dotted attribute expression on it is flagged under mypy's
    # --no-implicit-reexport; an Any-typed alias reaches the same
    # module global honestly, as the deliberate cross-module
    # monkeypatch it is.
    module: Any = edge_operations
    if not isinstance(module.prompt_library, _PromptLibraryProxy):
        module.prompt_library = _PromptLibraryProxy(module.prompt_library)


def install_edge_batching(
    llm_client: Any,
    *,
    batch_size: int,
    linger_ms: int,
    max_facts: int,
    logger: SafeLogger | None = None,
) -> EdgeBatcher | None:
    if batch_size <= 1:
        return None
    # A second install on an already-wrapped client would otherwise capture
    # the first routed() as its "original", chaining wrappers rather than
    # sharing one batcher. The tag set on the wrapper below lets a repeat
    # call recognise that and hand back the existing batcher instead.
    existing: EdgeBatcher | None = getattr(
        llm_client.generate_response, "_cairn_edge_batcher", None
    )
    if existing is not None:
        return existing
    _require_graphiti_edge_batch_compatibility()
    _install_prompt_proxy()
    original_call: Callable[..., Awaitable[dict[str, Any]]] = (
        llm_client.generate_response
    )
    batcher = EdgeBatcher(
        original_call,
        batch_size=batch_size,
        linger_seconds=linger_ms / 1000,
        max_facts=max_facts,
        logger=logger,
    )

    warned_context_missing = False

    async def routed(
        messages: Any,
        response_model: Any = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        **keywords: Any,
    ) -> dict[str, Any]:
        nonlocal warned_context_missing
        context = getattr(messages, "cairn_edge_context", None)
        if prompt_name == "dedupe_edges.resolve_edge":
            if context is not None:
                return await batcher.submit(context, max_tokens)
            # Cannot happen in a healthy install: the prompt proxy
            # attaches the context, and _single_resolve's un-batched
            # call bypasses this wrapper entirely via the captured
            # original. Reaching here means the proxy is not in place,
            # so batching is silently inert. A standing condition, not
            # a per-call event — warn once per install.
            if not warned_context_missing:
                warned_context_missing = True
                _LOGGER.warning(
                    "edge_batch_context_missing prompt_name=%s", prompt_name
                )
        return await original_call(
            messages,
            response_model,
            max_tokens,
            model_size,
            group_id,
            prompt_name,
            **keywords,
        )

    llm_client.generate_response = routed
    llm_client.generate_response._cairn_edge_batcher = batcher
    return batcher


__all__ = ["EdgeBatcher", "install_edge_batching"]
