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

## Reproduce the synthetic conversation

```sh
uv sync --locked
uv run --locked python scripts/demo_shared_memory.py
```

The demonstration creates a temporary catalogue and synthetic credentials,
uses the real application in process and removes temporary data on exit. It
does not contact a model provider or establish acceptance for a live host.
