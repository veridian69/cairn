# Pinned provider models

The provider models Cairn's projection uses are constants in
`src/cairn/projection/models.py`. They are not operator configuration: the
embedding model is part of the stored vector identity, and the LLM roles are
chosen against fixed-corpus quality evidence.

| Role | Constant | Model | Shutdown | Replacement | Checked |
|---|---|---|---|---|---|
| Graphiti medium (extraction, batched edge dedup) | `LLM_MEDIUM_MODEL` | `gpt-6-luna` | none announced | none | 21 Nov 2026 |
| Graphiti small (and the never-called reranker) | `LLM_SMALL_MODEL` | `gpt-5.6-luna` | none announced | none | 21 Nov 2026 |
| Fact and unit embeddings | `EMBEDDING_MODEL` | `text-embedding-3-small` | none announced | none | 21 Nov 2026 |

The two LLM constants select the models: Cairn passes them to Graphiti. The
embedding constants do not. Cairn builds Graphiti's embedder with the
library's own defaults, which are `text-embedding-3-small` and the dimension
in graphiti-core's `EMBEDDING_DIM` environment setting, 1024 when unset.
`EMBEDDING_MODEL` and `EMBEDDING_DIMENSION` (1024) are what Cairn checks that
embedder against: the pooled v1 fact vector needs this model at 1 to 1536
dimensions, and the local v5 clause units need it at exactly 1024. Cairn's
deployment files do not set `EMBEDDING_DIM`; a process that sets it to
another value keeps v1 and loses v5. See
[semantic-fact-search.md](semantic-fact-search.md).

Reasoning effort is pinned beside the models, for the same reason.
`LLM_REASONING` is `none` and applies to every call except one:
`LLM_COMBINED_EDGE_REASONING` is `low`, for the batched edge-dedup call, which
judges up to ten unrelated items in one answer and misjudged contradictions
with reasoning off. Change either only with a run of
`scripts/evaluate_edge_dedup.py`; its `--combined-reasoning` option measures
another effort for that call.

Source: the [OpenAI deprecations page](https://developers.openai.com/api/docs/deprecations).
"None announced" means the model is absent from that page. It is not a
guarantee: OpenAI gives at least six months' notice for generally available
models. The API also reports `shutdown_date` per model on
`GET /v1/models/<model>`; all three returned none on 21 November 2026.

## Changing a model

1. Edit the constant and the table above in the same change.
2. LLM roles: run `scripts/evaluate_semantic_memory.py` on the fixed corpus
   and compare with the previous model's result, make one live call through `_openai_provider_clients()` for each
   changed role, and keep both results with the change.
   When the medium model changes, also run `scripts/evaluate_edge_dedup.py`
   with `--compare-batch-model` naming the outgoing model: combined edge-dedup
   calls run on the medium model, and the fixed corpus barely exercises them.
3. The medium model's name is part of the extraction cache key. The cache
   serves only the bulk path (`delivery.chunk_size` above 1), so
   on such a host the next rebuild re-extracts every fact at provider cost.
   Budget that first. At the default chunk size of 1 extraction is never
   cached and a rebuild costs the same as before.
4. Embeddings: changing the model or dimension changes the representation
   identity and needs a re-embedding migration. Do not treat it as a swap.

## History

- 21 Nov 2026: `gpt-5.4-nano` (shutdown 1 April 2027, announced 1 Oct 2026)
  replaced by `gpt-6-luna` in v0.11.2.
- 10 Oct 2026: `gpt-4.1-nano` (shutdown 23 Oct 2026) replaced by `gpt-5.6-luna`.
