# Semantic fact-body index

The optional Graphiti adapter maintains derived fact vectors inside each
partition. The authoritative fact body, current validity, trust, scope and
classification remain in Cairn's catalogue. Search candidates never override
those decisions.

## Representation and search policy

The retained `cairn.fact-vector/v1` format embeds bounded UTF-8 chunks, computes
a byte-weighted normalised mean and stores a float32 vector. The supported
`text-embedding-3-small` configuration normally uses 1024 dimensions. Whole
batches and final vectors are validated before publication.

`cairn.fact-search/v1` uses normalised cosine similarity with a strict score
greater than `0.60`, then orders by score and fact UUID. Results join the
existing episode/edge candidates before the catalogue reconciles authority and
disclosure. Blank queries return no semantic candidate without embedding.

For the 1024-dimension configuration, `cairn.fact-local-vector/v5` also stores
non-overlapping clause units while retaining every exact UTF-8 body byte.
Units are normally 32–512 bytes, with a shorter final tail allowed. Repeated
text is embedded once per fact while every occurrence retains its span. Limits
bound occurrences and vector children for the maximum fact size.

`cairn.fact-local-search/v5` grades a fact by its best matching local unit.
Memory recall combines lexical overlap with that quantised grade; recency and
UUID provide deterministic tie-breaking. Scores advise attention only. Final
admission, currentness, body binding, ranking budget and disclosure remain
catalogue operations. Original `/v1/retrieve` retains pooled-vector behaviour.

If the semantic source, envelope or coverage check fails, Cairn discards all
semantic advice, falls back to catalogue-only recall and reports
`semantic_degraded: true`. Healthy empty semantic evidence is not degradation.

## Completion and rebuild

Projection completes only after extraction and a matching valid fact vector
exist. When local vectors are enabled it also requires complete matching clause
data. Retries preserve completed valid work; cached extraction alone does not
prove vector coverage.

Before a nonblank search, requested partitions are checked for missing,
incompatible or malformed coverage. A defect raises
`fact_vector_rebuild_required`; the authority path returns degraded recall
without partial semantic candidates, and logs `semantic_rebuild_needed`,
pointing operators to targeted repair or rebuild below. Read-side fingerprints are format checks,
not independent authenticity proofs. Delivery and explicit rebuild bind derived
records to immutable fact bodies.

There is no automatic vector migration. Rebuild explicitly with the normal
operator access and provider controls:

```sh
cairn rebuild-index --config /etc/cairn/config.yaml
```

The command enqueues authoritative facts before clearing derived graphs.
Interrupted work remains in the durable outbox for retry. Rebuild can require
new provider embeddings. It does not change fact custody or trust.

### Targeted repair

A host hard reset can lose index writes after the catalogue has already
retired their outbox rows. Such a fact is then torn, which fails the coverage
check above, or missing, which the check cannot see. Cairn repairs both without
a full rebuild:

- At startup the delivery loop audits every partition and enqueues a
  re-projection for each torn or missing fact, provided there are at most
  `delivery.repair_cap` of them (default 50; 0 turns startup repair off).
  Semantic search reports degraded until that audit finishes and every fact it
  found has projected; with nothing to repair this takes seconds.
- Above the cap nothing is enqueued, `projection_repair_over_cap` is logged
  with the number of facts owed, and search stays degraded until an operator
  runs the uncapped repair with Cairn stopped, then restarts it:

```sh
cairn repair-index --config /etc/cairn/config.yaml --dry-run   # report only
cairn repair-index --config /etc/cairn/config.yaml
```

  The command drains the outbox before exiting and exits 4 with
  `status: partial` while work is still owed; a dry run that finds work also
  exits 4. `projection_repair_unrepairable` means a defective indexed episode
  matches no catalogue fact, so no row can name it; use `rebuild-index`.

Re-projection can call providers, normally served from the projection caches.

## Provider-free engine check

Default tests do not launch Docker. To exercise the pinned Falkor/Graphiti path
with controlled embeddings and no provider client, use a reviewed checkout with
the locked image already cached:

```sh
CAIRN_FACT_DB_TESTS=1 .venv/bin/python -B -m pytest -q -o addopts='' \
  -p no:cacheprovider tests/projection/test_fact_vectors_db.py
```

The fixture owns a disposable loopback-bound container/network and refuses an
existing server endpoint. It establishes query plumbing and cleanup, not
semantic quality, an SLA or a production capacity limit.
