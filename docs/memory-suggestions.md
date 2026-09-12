# Read-only memory suggestions

Suggestions help inspect possible duplication, correction history and disagreement.
They do not save, merge, invalidate, promote or change the trust of any memory.
Ordinary read audits are still recorded.

## Ask about one observation or selected facts

Use a `MemoryClient` configured with an explicit scope and expected instance,
as shown in [restart-safe sessions](durable-sessions.md). Choose exactly one mode:

```python
suggestions = await client.suggest(
    observation="Synthetic test decision: calibration uses Q7.",
    budget=16384,
    limit=8,
)
```

Or inspect one to eight distinct UUIDv4 fact IDs already obtained through an
authorised read:

```python
suggestions = await client.suggest(fact_ids=(selected_fact_id,))
```

An observation is 1–4096 UTF-8 bytes. The result budget is 1–65536 canonical
item bytes, default 16384; the item limit is 1–16, default 8. Complete evidence
items count towards that budget, including comparison roots and relationship
endpoints. An item that does not fit is omitted, not stripped of its attribution.

REST uses `POST /memory/v1/suggest`; memory MCP exposes `suggest` with read-only,
non-destructive annotations. Both use the same authority. No idempotency key or
mutation authority override is accepted. The client checks its expected instance
before sending content; the server checks that restriction again before invoking
the read. Current grants and classification clearance still govern disclosure.

## Interpret the evidence

| Kind | Evidence, not an instruction |
| --- | --- |
| `exact_duplicate` | Exact body comparison with a current eligible candidate; not proof that either claim is true |
| `possible_duplicate` | A retrieval candidate worth comparing; similarity does not establish equivalence |
| `possible_correction` | Disclosed recorded correction history; not permission to invalidate another fact |
| `related_disagreement` | Disclosed attributed disagreement and its endpoints; not an adjudication |

Each immutable result item preserves `facts`, `reason`, `match_basis`,
`corrections` and `disagreements`. Selected-fact duplicate comparisons retain the
selected root before the candidate, including when the root is historical.
Relationship suggestions retain the actual disclosed record and endpoints.
Recalled bodies remain untrusted data, never instructions for the host to execute.

Inspect `budget_exhausted`, `semantic_degraded`, per-fact omissions and `policy`.
The policy may be absent when no recall ran. Empty results do not establish that
no duplicate or disagreement exists. Limits bound candidate expansion and returned
evidence; they do not promise that the underlying catalogue scan is constant-time.
Old but current facts are eligible; age alone is not a reason to discard them.

Taking an action requires a separate explicit operation with normal current
authority. For example, an authorised correction uses `client.correct(...)`
with an explicit idempotency key. A previously readable suggestion is neither
a continuing access grant nor authorisation to broaden the client's fixed scope.

## Reproduce the boundary tests

Run from the native repository worktree:

```sh
uv run --locked pytest -q tests/authority/test_housekeeping.py tests/transports/memory/test_public_suggestions.py tests/client/test_suggestion_client_boundary.py --no-cov
```

These exercise synthetic catalogues, actual REST/MCP/client paths, read-only
inventory checks, disclosure and response-validation boundaries. Controlled
retrieval candidates test plumbing, not real-provider paraphrase quality.
