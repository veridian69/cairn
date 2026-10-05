# Shared evolving memory

Cairn's shared-memory interface lets agents contribute and recall uncertain,
attributed knowledge. An observation is a candidate belief, not established
truth. A disagreement records a conflict; it does not decide who is right. A
correction ends a belief's current validity while preserving its history.

The interface is versioned separately at `/memory/v1`, with MCP at
`/memory/v1/mcp`. Existing `/v1` clients retain their original behaviour. Both
interfaces use the same authoritative catalogue and authenticated principals.

## Conversation entry points

The [Python client](shared-memory-client.md) provides the conversation workflow:

| Need | Entry point |
| --- | --- |
| Where were we? | `MemorySession.arrive`: bounded attributed excerpts, correction history and evidence references. |
| Which Cairn, identity and access? | `MemoryClient.diagnose`, or the REST/MCP `diagnose` operation, at the exact host scope and classification. |
| Has it been saved? | `persistence_progress`: processing, saved with a confirmed receipt, failed/unconfirmed, or skipped. |
| Recover a failed write | `retry_persistence` with the exact completed output and original operation identity. |

Arrival briefings flag unknown previous visits, omitted history and incomplete
disagreement context. Their excerpts remain claims with attribution; they are
not task acceptance or instructions. The [CLI adapter](host-handover.md) shows
how an explicit host can connect this workflow to fresh agent processes.

## What an agent remembers

Agents should contribute durable observations, decisions, corrections and
failed approaches that will help a later conversation. They should not copy
every message, repeat unchanged memories, store credentials or treat recalled
text as instructions. Evidence and source identities remain distinct from an
agent's interpretation.

The Python turn integration recalls before the model callback and remembers
selected durable observations afterwards. The host fixes scope and
classification. A callback cannot grant itself promotion rights or write to a
different scope.

Other MCP hosts can follow the same workflow:

1. Recall at task start and when the subject changes. Preserve trust and attribution in model context.
2. Remember useful durable observations at checkpoints.
3. Record disagreement when identifiable beliefs conflict; present both perspectives.
4. Correct only with the required authority and a reason, then retain the history.
5. Report uncertain persistence honestly and retry only the exact completed operation.

MCP tools cannot observe every turn in a host application. Automatic use needs
an explicit host integration or an agent following this workflow.

## Sharing and authority

Each agent or provider uses its own authenticated principal. Sharing context
does not require sharing credentials. Run-specific knowledge remains within its
authorised scope until an actor with suitable rights deliberately widens it.
Current access rules apply to historical queries too.

Remembering requires `ingest`; recall and history require `retrieve`.
Disagreement requires readable endpoints and `ingest` at their shared exact
scope. Resolution requires readable evidence and `promote`; correction uses
`invalidate`. Candidate, validated and failed-approach trust states remain
distinct. Ordinary memory recall exposes uncertainty; original `/v1/retrieve`
still defaults to validated facts.

Some provenance and correction fields are withheld when disclosure would cross
the caller's scope or clearance. A missing reference therefore does not prove
that no history exists.

## Fading, relevance and limits

Fading changes which eligible memories fit into everyday recall. It does not
delete them, lower their trust or erase authorship. A strong cue can resurface
an old memory; repeated retrieval does not make it more authoritative.

The initial policy combines lexical relevance and bounded age decay. An
optional semantic index supplies candidate advice, but the catalogue remains
authoritative. If semantic retrieval fails, recall falls back to the catalogue
and reports `semantic_degraded`. Set `relevant_only: true` to require a lexical
or semantic signal; this does not give a lexical-only instance semantic
understanding.

Recall gives facts priority over relationship detail. Preserve
`has_disagreement`, `disagreement_context_incomplete`, degradation, budget and
omission warnings alongside the fact. History expansion is bounded and
cycle-safe; an exhausted result is not proof that every related record was
returned. Permanent deletion is a separate retention operation, not fading.

## Ordered recall

`recall-page` (`POST /memory/v1/recall-page`, MCP tool `recall-page`) is a read
that returns recall results ordered by `relevance` (default), `newest` or
`oldest`, and continues them with an opaque `cursor`. It needs `retrieve`,
rejects idempotency keys and creates no knowledge. Legacy `recall` is unchanged;
the new operation stops at the first whole record that does not fit, so pages
lose nothing.

- `time_basis` (`source` default, or `recorded`) is for chronological orders
  only; omit it for `relevance`. Source time is a caller-supplied claim, and a
  withheld source time looks the same as a missing one. Under the source basis,
  facts without an available source time come last, ordered by recorded time in
  the same direction.
- `relevant_only` defaults to `true`; recency alone never selects a memory.
  `limit` is 1-100 (default 20); `budget` is 1-1,048,576 (default 16,384).
- A continuation sends exactly `scope`, `cursor`, `budget` and `limit`.
- Each hit adds `observed_at`, `source_time_status`, `ordering_time_basis` and
  `source_evidence_id`, the Cairn-held evidence record of the origin assertion,
  disclosed only when that origin is disclosable.
- `next_cursor` is null when no cursor can be issued. `facts_remaining` says
  whether another presently disclosable fact exists. If a snapshot expired or was
  evicted before the next cursor could be issued, the page is still returned with
  `facts_remaining: true` and `next_cursor: null`; it never claims completion, so
  start a fresh query. Preserve `context_incomplete` and `selection_complete`
  too; `selection_complete: false` means narrow the query.
- Refusals use HTTP 400 with `detail` of exactly
  `{"reason":"page_budget_too_small","minimum_budget":N}` (N is the size of the
  whole first eligible record; resubmit with at least that budget) or `{"reason":"continuation_unavailable"}` (start a fresh query).
- Snapshots are process-local, hold at most 4,096 fact identities for 300
  seconds, 4 per principal (the oldest of that principal is evicted), 64 per
  process and 32 MiB in total. They do not survive a restart.

## Evidence window

`evidence-window` (`POST /memory/v1/evidence-window`, MCP tool `evidence-window`)
reads one bounded, byte-exact excerpt of a single Cairn-held evidence payload,
usually the `source_evidence_id` of a `recall-page` hit. It is a read: it needs
`retrieve`, rejects idempotency keys and creates no knowledge. Every call repeats
the evidence read's authorisation and verifies the whole payload's SHA-256 before
disclosing anything. The conversation adapter exposes it as `source_window`.

- Send `evidence_id` with either a literal `query` (1-8,192 UTF-8 bytes) or a
  UTF-8 byte `start` offset (default 0), never both. `budget` is 1-1,048,576
  canonical record bytes, metadata included (default 16,384).
- `literal-terms/v1` tries the whole query (casefolded) first, then whole
  casefolded terms on word boundaries. The earliest original start wins; ties go to the longest
  span, then the longest term, then codepoint order. At most 32 distinct terms.
  There is no Unicode normalisation and no FTS or semantic matching.
- A query window holds up to two lines before the match and six from it; the
  budget trims preceding context first and never cuts the match. Offsets are
  original payload bytes.
- No match is a separate result: `match_found: false`, no text, window or
  continuation, but the digest and length are reported. An offset read at the
  end of the payload returns empty text and no continuation.
- Continue with `start: next_start_byte`. If even the match (or one character)
  cannot fit, the refusal carries
  `{"reason":"page_budget_too_small","minimum_budget":N}`.
- It reads one payload. It does not parse dialogue, join turns across evidence
  records or establish who said what.

## Reproduce the synthetic conversation

```sh
uv sync --locked
uv run --locked python scripts/demo_shared_memory.py
```

The demonstration creates a temporary catalogue and synthetic credentials,
uses the real application in process and removes temporary data on exit. It
does not contact a model provider or establish acceptance for a live host.
