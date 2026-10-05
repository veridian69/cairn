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

## Ordered recall (`recall-page`)

`POST /memory/v1/recall-page` and the MCP tool `recall-page` return recall
results in a stated order and continue them across pages. Both are reads: they
reject idempotency keys, need a live covering `retrieve` grant and do not create
knowledge or strengthen trust. The Python client wraps them as
`MemoryClient.recall_page` and `continue_recall`, and the CLI as `recall-page`.
Legacy `recall` is unchanged: its frozen ordering and skip-to-fit accounting
stay as they were. `recall-page` is separate because pages must not lose records:
it stops before the first whole record that does not fit (a monotone prefix)
instead of skipping it.

```json
{
  "scope": {"realm": "cairn", "segments": []},
  "query": "What labels should Acrid receipts receive?",
  "order": "relevance",
  "relevant_only": true,
  "budget": 16384,
  "limit": 20,
  "trust_filters": []
}
```

| Field | Rule |
| --- | --- |
| `order` | `relevance` (default), `newest` or `oldest`. |
| `time_basis` | `source` (default) or `recorded`, for `newest`/`oldest` only. Omit it for `relevance`; supplying it, even null, is refused. |
| `relevant_only` | Default `true`. Recency alone never selects a memory, even in a chronological order. |
| `limit` | 1-100, default 20. |
| `budget` | 1-1,048,576 canonical-record byte units, default 16,384. |
| `trust_filters` | As for `recall`; empty is not the core API's validated-only rule. |

A continuation sends exactly `scope`, `cursor`, `budget` and `limit`. The query,
order, time basis, relevance and trust settings were bound when the cursor was
issued and cannot change. The scope must match the original exactly. `cursor`
is an opaque 43-character URL-safe token; do not parse it.

Each hit is the `recall` fact plus `observed_at` (nullable),
`source_time_status` (`available` or `unavailable`), `ordering_time_basis`
(`source`, `recorded` or null for relevance) and `source_evidence_id`. The last
is the Cairn-held evidence record of the fact's origin assertion, disclosed only
when that origin is disclosable; null otherwise. Pass it to `evidence-window`
(below) to read the exact source text around the fact. Source time is a caller-supplied
observation claim, not custody time. Under the source basis, facts with an
available source time come first in the requested direction; the rest follow,
ordered by recorded time in the same direction. A withheld source time is indistinguishable from a
missing one. Relevance order keeps the existing policy's comparison, and each
`relevance_score` is the value computed when the snapshot was created.

Besides the `recall` fields, a page carries:

- `ordering`: `order`, `time_basis` and policy `memory-order/v1`. The top-level
  `policy` is still the relevance or fallback policy actually used.
- `snapshot_created_at` and `snapshot_expires_at`. The expiry is null when no
  snapshot was published, that is when `next_cursor` is null on an initial page.
- `next_cursor`: null when no cursor can be issued (see `facts_remaining`).
- `facts_remaining`: another presently disclosable fact exists. If a snapshot
  expired or was evicted before the next cursor could be issued, the page is
  still returned with `facts_remaining: true` and `next_cursor: null`; it never
  claims completion. Run a fresh query.
- `context_incomplete`: permitted disagreement or resolution context was omitted
  by its bounds. Use `history` for the rest.
- `selection_complete`: false when more eligible matches existed than a
  snapshot holds. It says nothing about recall quality or whole-archive
  completeness; narrow the query or scope.

`budget_exhausted` reports budget and context omissions, not the page count.

### Operation-local failure

Both refusals are HTTP 400, code `invalid_request`, retry `never`, with a
`detail` that only this operation uses (the schema is `PageFailureEnvelope`):

- `{"reason":"page_budget_too_small","minimum_budget":N}`: the first eligible
  record (the whole paged record, including its time fields and the relationship
  flags reserved at maximum length) needs N canonical bytes, 1 <= N <= 1,048,576. No fact is returned and
  no cursor advances; resubmit with `budget` of at least N. A valid cursor stays
  usable.
- `{"reason":"continuation_unavailable"}`: the cursor is expired, unknown,
  evicted, from another principal, instance or process, or from before a
  restart; or the source-time projection of the snapshot changed. Nothing says
  which. Start a fresh query.

Other refusals, including request validation and `invalid_limit`, `invalid_scope`
and `invalid_query`, keep the existing `FailureEnvelope`. The 400 schema for this
path therefore allows either envelope.

### Snapshots

A snapshot is made only when a page has a `next_cursor`. It holds ranked fact
identities, order keys and request bindings, never bodies, the raw query or
credentials. It is process-local, dropped on shutdown and not backed up.

| Limit | Value |
| --- | --- |
| Facts per snapshot | 4,096 |
| Lifetime | 300 seconds, absolute |
| Per principal | 4; a fifth evicts that principal's oldest |
| Per process | 64 |
| Metadata ceiling | 32 MiB |

Past the process or metadata limit, after expired entries and the caller's own
oldest snapshot are removed, the request fails with `dependency_unavailable`
(`after-delay`); no other principal's snapshot is evicted. Every page re-checks
authority, validity, corrections and provenance, and drops facts that are no
longer eligible. Ranking and membership stay frozen, so new knowledge needs a
new query. Restart a query after corrections or material new knowledge.

## Evidence window (`evidence-window`)

`POST /memory/v1/evidence-window` and the MCP tool `evidence-window` return one
bounded, byte-exact window of a single Cairn-held evidence payload. They are
reads: they reject idempotency keys, need a live covering `retrieve` grant and
create no knowledge. This is an authenticated source read, not a fact or a trust
assertion, and it does not depend on semantic readiness. The Python client
wraps it as `MemoryClient.evidence_window(evidence_id, *, query=None,
start=None, budget=16384)`, the CLI as `evidence-window` and the conversation
adapter as the read-only `source_window` tool.

The usual flow is `recall-page` hit, then its `source_evidence_id`, then
`evidence-window`. A null `source_evidence_id` means there is no disclosable
Cairn-held origin to read.

```json
{
  "scope": {"realm": "cairn", "segments": []},
  "evidence_id": "66666666-6666-4666-8666-666666666666",
  "query": "port 8123",
  "budget": 16384
}
```

| Field | Rule |
| --- | --- |
| `evidence_id` | Canonical evidence UUID. |
| `query` | 1-8,192 UTF-8 bytes, not whitespace-only, at most 32 distinct casefolded terms. Anchors a query window. |
| `start` | UTF-8 byte offset, 0-1,048,576, on a character boundary. Default 0 when `query` is absent. |
| `budget` | 1-1,048,576 canonical record bytes, metadata included; default 16,384. |

`query` and `start` together are refused. Omit whichever is unused; do not send
null.

Each call repeats the `read-evidence` catalogue checks before touching Attic and
verifies the whole payload's SHA-256, UTF-8 and length before disclosing any
window. Unknown, inaccessible and external-reference evidence are
indistinguishable (`not_found`); pending delivery, corruption and infrastructure
failures keep their existing categories (`evidence_pending`, `evidence_corrupt`,
`dependency_unavailable`). The query is secret-screened before any Attic read.

### Locating a query: `literal-terms/v1`

A local, deterministic literal locator, not FTS5 syntax or semantic matching:

- The whole query (casefolded, leading and trailing whitespace stripped,
  internal whitespace exact) is tried first and needs no word boundaries.
- Otherwise each whitespace-separated, Unicode-casefolded term must occur as a
  complete term: each neighbour is the text edge or a character that is neither
  alphanumeric nor `_`.
- The earliest original start wins. Ties go to the longest original span, then
  the longest folded term, then codepoint order.
- Neither text nor query is Unicode-normalised. Both match ends must fall on
  whole original characters: `ss` may match a whole `ß`, but not half of it.
- More than 32 distinct folded terms is refused. Offsets are always bytes of the
  original UTF-8 payload, never of folded text.

A query window starts at most two LF-delimited lines before the anchor line and
runs to at most six lines from the anchor line, inclusive. A match spanning more
lines stays whole. To fit the budget, preceding context is trimmed first, then
following context; the matched span itself is never cut.

### Result

| Field | Meaning |
| --- | --- |
| `evidence_id`, `mode` | The record read; `query` or `offset`. |
| `text` | Exact source text of the window. |
| `start_byte`, `end_byte` | Window in payload bytes, end exclusive. |
| `sha256`, `byte_length` | Whole-payload digest and length. |
| `match_found` | `true` or `false` for a query; null for an offset read. |
| `match_start_byte`, `match_end_byte` | The original matched span, inside the window. |
| `prefix_omitted`, `suffix_omitted` | Whether payload text lies before or after the window. |
| `next_start_byte` | `end_byte` while a suffix remains; null otherwise. |
| `budget_consumed` | Canonical bytes of this record without `budget_consumed`. |

A query without a match returns `match_found: false` with null `text`, window,
match span, omission flags and `next_start_byte`; the digest and length are still
reported, and this metadata still consumes budget. An offset read at the end of
the payload returns empty text, `start_byte = end_byte = byte_length`,
`prefix_omitted` true when the payload is non-empty, `suffix_omitted: false` and
no continuation.

Continue by repeating the request with `start: next_start_byte` and no query.
Every call re-authorises and re-verifies the full digest; there is no server
snapshot, and a continuation always makes progress. The client validates the
exact keys, the variant shape, the byte arithmetic and that `budget_consumed`
equals the record's canonical cost.

If even the metadata and the whole match (or one character) cannot fit, the
refusal is HTTP 400, `invalid_request`, `never`, with
`{"reason":"page_budget_too_small","minimum_budget":N}` (`PageFailureEnvelope`);
retry with a budget of at least N. Other refusals, such as a mid-character or
out-of-range `start` or an invalid query, keep `FailureEnvelope`.

This reads one stored payload. It does not parse JSON or dialogue, join turns
across separately stored evidence records, or authenticate speaker identity; a
role label inside the text is only text. Use `read-evidence` for the complete
envelope.

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
