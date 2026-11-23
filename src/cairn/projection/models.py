"""The provider models Cairn's projection pins, in one place.

These are constants, not operator configuration, on purpose:

* The embedding model and dimension are part of the stored representation
  identity (``cairn.fact-vector``, ``cairn.fact-local-vector``). Changing
  either invalidates every stored vector, so it is a migration, never a dial.
* The LLM roles are chosen against fixed-corpus quality evidence
  (``docs/evidence/shared-memory/``). A configuration dial would let an
  unmeasured model into extraction, and P-87 measured that the small model
  degrades edge dedup, so the role split is deliberate.

Retirement dates are tracked in ``docs/operations/pinned-models.md``; update
that table with any change here.
"""

# Graphiti's medium role: extraction, and combined edge-dedup calls.
LLM_MEDIUM_MODEL = "gpt-6-luna"

# Graphiti's small role. The reranker inherits it but is never called: no Cairn
# path selects a cross-encoder config (_SEARCH_CONFIG reranks by RRF), and
# gpt-5.6-luna rejects OpenAIRerankerClient's request shape.
LLM_SMALL_MODEL = "gpt-5.6-luna"

# Reasoning effort. Every call runs with reasoning off except the combined
# edge-dedup call, which judges up to ten unrelated items in one answer: with
# reasoning off it retired unrelated facts and missed real contradictions, the
# more of those the more items shared the call (I-102).
LLM_REASONING = "none"
LLM_COMBINED_EDGE_REASONING = "low"

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1024
