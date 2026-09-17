# Shared-memory Python client

`cairn.client` provides an asynchronous, provider-neutral turn wrapper for
`/memory/v1`. The host owns the HTTP client, credentials, scope,
classification, session identity and turn identity. A model callback receives
recalled content as a separately labelled immutable data packet and may return
only its response plus selected durable observations.

## Public interface

```python
MemoryClient(
    http: httpx.AsyncClient,
    *,
    scope: Scope,
    classification: Classification,
)

await MemoryClient.recall(
    query: str,
    *,
    budget: int = 16384,
    relevant_only: bool = False,
) -> RecalledMemory

await MemoryClient.history(fact_id: UUID, *, budget: int = 16384) -> RecalledMemory

await MemoryClient.remember(
    observations: tuple[DurableObservation, ...],
    *,
    idempotency_key: UUID,
) -> PersistenceReceipt

await MemoryClient.correct(
    fact_ids: tuple[UUID, ...],
    *,
    reason: str,
    superseded_by: UUID | None = None,
    idempotency_key: UUID,
) -> PersistenceReceipt

await MemoryClient.diagnose(
    *,
    expected_instance_id: UUID | None = None,
    expected_contract_digest: str | None = None,
    expected_mcp_contract_digest: str | None = None,
) -> ConnectionDiagnostics

MemorySession(client: MemoryClient, *, session_id: UUID)

await MemorySession.run_turn(
    user_input: str,
    model_callback: ModelCallback,
    *,
    turn_id: UUID,
    budget: int = 16384,
    recall_query: str | None = None,
    relevant_only: bool = False,
) -> TurnResult

await MemorySession.arrive(
    query: str,
    *,
    since: datetime | None = None,
    history_fact_ids: tuple[UUID, ...] = (),
    budget: int = 16384,
) -> ArrivalBriefing

await MemorySession.persist_turn(
    turn_id: UUID,
    turn: ModelTurn,
) -> PersistenceReceipt

await MemorySession.retry_persistence(
    failure: PersistenceFailure,
) -> TurnResult

MemorySession.persistence_progress(turn_id: UUID) -> PersistenceProgress | None
MemorySession.persistence_failure(turn_id: UUID) -> PersistenceFailure | None
```

The callback values are frozen dataclasses:

```python
DurableObservation(
    body: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    observed_at: datetime | None = None,
)

TurnInput(user_input: str, recalled: RecalledMemory)
ModelTurn(response: str, observations: tuple[DurableObservation, ...] = ())
TurnResult(response: str, persistence: PersistenceReceipt)
```

`diagnose` makes one authenticated `POST /memory/v1/diagnose`, also exposed as
the memory MCP tool `diagnose`. It checks the memory interface at the client's
exact scope and classification. The server reports the instance, authenticated
principal, memory contract digests and applicable current capabilities. The
client validates the response and compares any supplied expected identity or
digests. Authentication failure, lack of authority, incompatible contracts,
wrong instance and malformed responses remain distinct outcomes.

Diagnostics describe eligibility at the reported evaluation time. Each
subsequent operation still checks current grants, content and evidence. A
principal label supplied by a model is never the authenticated identity. The
result does not enumerate access to other scopes or contain a credential;
failure messages do not copy server prose into host status displays.

`RecalledMemory.data` is the complete recursively frozen JSON result from
`/memory/v1/recall` or `/memory/v1/history`. It retains fact provenance, source
principal, source type, trust, disagreements, resolutions and policy fields. Its `source` is
`cairn-memory/v1` and its `content_role` is `untrusted-data`. Hosts should pass
that structure through a provider's data/context facility; they must not
concatenate it into system or developer instructions.

History responses are checked against the requested scope: exact and ancestor
records are permitted; sibling, descendant and other-realm records are refused.
The client requests identity encoding and rejects compressed history before
decoding. It bounds wire bytes as well as canonical record bytes; excessive
padding or malformed nesting produces a safe `invalid_response`, not truncated
evidence. A proxy serving this endpoint must honour identity encoding.

`correct` always sends the client's fixed scope. Cairn requires current retrieve
access to every source and replacement at that exact scope, as well as its
normal invalidation authority. A broad credential does not widen the host's
scope. The method names existing facts; it does not create a replacement or
erase history. Up to 100 distinct fact IDs and a 1–4096-byte reason are accepted.
Replies use bounded, uncompressed receipt decoding; only complete committed or
replayed receipts count as persistence. Reusing the same request/key returns
the original correction result while rechecking current authority.

The additive memory wire request permits an optional `scope` for compatibility
with existing callers. Scope-omitting legacy requests retain their existing
grant-based behaviour; the Python host wrapper always opts into the additional
exact-scope restriction. Frozen `/v1` request models are unchanged.

## Where were we?

Call `session.arrive` when entering a conversation or resuming a topic. The
query should describe the relevant component or decision; host-known fact IDs
can anchor unfinished work or a correction. The result provides at most four
short literal excerpts with trust, source identity and disagreement warnings.
Each excerpt points into the complete immutable evidence packets underneath.

The briefing performs one recall plus at most four history reads under one
shared record-byte budget. If the initial recall spends no record bytes but
is exhausted, and no explicit anchors were supplied, it may retry recall once
using the full unused budget. At most 64 explicit anchor IDs are accepted;
this bounds omission metadata as well as network calls.
It selects cue-relevant memories and exposes any
omitted summary/history, exhausted budget or failed read. An empty or failed
briefing does not establish that the project has no unfinished work.

With a host-provided `since`, `changes` references newer recorded facts,
corrections and relationship records in the selected evidence. It uses recorded
time for new facts and the correction event's time for invalidations. A visible
invalidation timestamp still identifies a change when its reason is withheld;
the missing context is flagged. Later history evidence removes superseded facts
from the short current summary while retaining their original evidence. It is a
bounded briefing of selected memories, not a complete project change feed. An
unknown previous visit is explicit; the host owns visit identity and must not
fabricate a timestamp. Statements about decisions or unfinished work remain
attributed source claims rather than inferred task status.

The lower-level `build_arrival_briefing` helper also accepts keyword-only
`include_boundary=True` for conservative checkpoint consumers. It includes
selected records whose change timestamp equals `since`, so the same record
may be repeated. The result's `include_boundary` flag records that choice.
The default remains `False`, and the existing `MemorySession.arrive` retains
its strictly-newer behaviour. Neither mode detects clock rollback or provides
an exhaustive catalogue cursor; the durable session integration owns those
checkpoint semantics.

For conversational turns, set `relevant_only=True` to exclude facts with no
lexical cue overlap and no semantic candidate match. Existing callers retain
the prior default. A lexical-only installation cannot reliably recall a
paraphrase with no shared words; the quality evaluation measures that limit.

## Integrated turn

```python
from uuid import uuid4

import httpx

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client import DurableObservation, MemoryClient, MemorySession, ModelTurn


async def invoke_model(turn_input):
    # Adapt this typed input to the chosen provider. Recalled text remains data.
    hits = turn_input.recalled.data["hits"]
    response = f"Received {len(hits)} authorised memories."
    return ModelTurn(
        response=response,
        observations=(DurableObservation("The operator selected this fact."),),
    )


scope = Scope("cairn", (ScopeSegment("project", "example"),))
async with httpx.AsyncClient(
    base_url="https://cairn.example",
    headers={"Authorization": "Bearer <host-provided credential>"},
) as http:
    session = MemorySession(
        MemoryClient(
            http,
            scope=scope,
            classification=Classification.INTERNAL,
        ),
        session_id=uuid4(),
    )
    result = await session.run_turn(
        "What changed?",
        invoke_model,
        turn_id=uuid4(),
    )
```

The sequence is fixed: recall, callback, remember. Recall failure raises
`RecallFailure` before the callback runs. An empty observation tuple returns a
`PersistenceStatus.SKIPPED` receipt and sends no remember request. Otherwise
all observations are sent in one candidate-only, agent-claim mutation. The
callback cannot set scope, classification, trust, source identity, credentials,
promotion or relationship authority.

The recall query is bounded to the memory API's UTF-8 limit. By default the
session uses deterministic cues from the beginning and end of a long user
input, while the callback still receives the complete original input. A host
may supply a separate `recall_query`; the same non-empty size and budget checks
then apply before any HTTP request.

## Persistence failure and exact retry

Hosts can show the current write state using `session.persistence_progress`.
This inspects ephemeral session state without making another network request:

| Phase | Meaning |
| --- | --- |
| `processing` | The remember request is being prepared or is in flight; no receipt has been confirmed. |
| `saved` | A complete committed or replayed custody receipt has been validated. |
| `failed` | This attempt did not confirm custody. A lost response may conceal a committed write. |
| `skipped` | The callback selected no durable observations; nothing was written. |

Before a persistence attempt, the result is `None`. A saved result includes
the validated receipt. `searchability` remains `unconfirmed`: custody does
not prove that asynchronous indexing or evidence delivery has finished. This
state is for a host status display; it is not a durable queue or memory store.
Cancellation marks a write as failed/unconfirmed rather than leaving an
endless processing indicator.

A confirmed receipt survives a later failed retry or conflicting attempt;
the progress value retains that receipt and exposes the latest failure code.
Cancellation retains normal cancellation semantics. While the session still
exists, `persistence_failure(turn_id)` retrieves the exact completed output and
retry context even after cancellation. Pass that failure to `retry_persistence`
to recover without a new model call. This recovery data is cleared on a
successful persistence attempt and disappears when the process ends.

The remember key is UUIDv5-derived from the fixed remember namespace, the host
session ID and the host turn ID. Replaying the same turn therefore uses the
same key. Reusing a turn ID with different observations raises
`PersistenceConflict` before another request is sent. That exception is also a
`PersistenceFailure` and preserves the newly completed response and
observations. Empty observation selections participate in the same comparison,
so changing a turn from empty to durable or from durable to empty is also a
conflict.

Within one session, `run_turn` refuses a turn ID whose model callback has
already started, including after a persistence failure. Use
`retry_persistence` for the completed turn. A recall failure occurs before
the callback and may be retried with the same turn ID. These guards protect
the current process only; after a process restart the host must preserve
turn identity and completed output through its own conversation lifecycle.

If remembering fails after the callback completes, `run_turn` raises
`PersistenceFailure`. It carries the completed `response`, exact
`observations`, `turn_id`, `idempotency_key` and content-free failure metadata.
The host can return the response to its caller and explicitly retry persistence
without another model call:

```python
from cairn.client import PersistenceFailure

try:
    result = await session.run_turn(prompt, invoke_model, turn_id=turn_id)
except PersistenceFailure as failure:
    completed_response = failure.response
    # Retry only under host policy; there is no automatic retry loop.
    result = await session.retry_persistence(failure)
```

Retry with the same `MemorySession` and `MemoryClient` instance. The client
snapshots its non-secret HTTP destination at construction and rejects a changed
injected `base_url` before sending. A failure cannot be moved to another client,
scope or classification, even when the new session reuses the same session ID.
The host must preserve the authenticated principal across attempts; credential
rotation for that principal remains host-owned, and the server authorises every
request afresh.

The retry may return `PersistenceStatus.REPLAYED` when the first request was
committed but its response was lost. A server `idempotency_conflict`, authority
denial, secret rejection, network error or malformed success remains explicit;
the client never reports such a result as remembered.

Successful recall packets and remember receipts are checked as complete current
memory/v1 documents before the client reports memory or persistence. This
includes canonical identities and timestamps, attribution, relationship
endpoints and flags, exact disclosed-record byte accounting, receipt cardinality
and receipt digests. Malformed success documents fail explicitly.

The `/memory/v1/remember` wire format has one batch-level `observed_at`.
Consequently, observations in one `ModelTurn` must all use the same
`observed_at` value, including all using `None`. A mismatch becomes a
`PersistenceFailure` with the completed response preserved. Observation
encoding failures and other local preparation failures preserve the response
and exact selected observations in the same way.

The injected HTTP client retains the credential; the memory client neither
accepts a credential argument nor copies credential values into request bodies,
failures or logs. Redirects are disabled per authenticated request. HTTPS is
required except for numeric loopback addresses, which keeps local ASGI and
loopback testing possible without weakening remote configuration.
