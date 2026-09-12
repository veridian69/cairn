-- P-90, accepted 28 August 2026: the extraction cache. A full rebuild
-- re-runs every provider call, but extraction is almost a pure function
-- of the episode and embeddings are a pure function of (model, text) —
-- the catalogue already witnessed every fact once and paid that bill
-- once. These tables convert the second payment into a read.
--
-- Invalidation is structural, not temporal: drift in the key material
-- (episode sha, graphiti-core version, effective model ids, entity-type
-- field-name fingerprint, edge-type-map fingerprint, custom
-- instructions) makes every key miss. That material is the accepted
-- key, not every input the extraction prompt consumes. Two of those
-- differences are deliberate. The key is content-scoped, not
-- episode-scoped: reference_time enters the prompt but is not key
-- material, because a fact's reference_time is stable and reproduces
-- identically on rebuild; and excluded_entity_types has no field in the
-- accepted key, so it bypasses the cache entirely rather than being
-- served blind.
--
-- Three are known gaps, and they do not share a rationale. Two are
-- inert while Cairn passes entity_types=None and edge_types=None to
-- add_episode_bulk: the entity-type fingerprint covers field names
-- only, while the pinned library feeds each type's docstring into the
-- extraction prompt, and edge_types reaches the edge prompt but is
-- neither key material nor a bypass trigger. The third is NOT inert.
-- The previous-episode window — the three most recent same-partition
-- episodes, read live from the graph — enters both prompts and is not
-- key material, so a hit can serve extraction produced under a
-- different window. It varies through failure reordering, valid_at
-- ties and chunk composition. The effect is semantic drift on already
-- nondeterministic model output rather than a broken invariant, and it
-- cannot arise under the default chunk_size of 1, which never reaches
-- the bulk path and so never populates this cache. Configuring custom
-- types, or keying the window, needs these closed first. Two distinct facts with byte-identical bodies share a
-- cache_key; storage is fact-owned (the composite primary key below,
-- amended before release under the P-90 remediation ruling R3 of
-- 29 August 2026) so each keeps its own episode-owned payload, and
-- exact reads verify both columns. Old rows become garbage, never
-- wrong answers; there is no TTL and no manual invalidation surface.
--
-- The catalogue rather than a side file because the catalogue is what a
-- rebuild is rebuilt from: backup/restore then carries the cache with
-- the data it derives from. Sensitivity is unchanged — the payloads are
-- derivations of fact bodies the same file already stores in clear.
CREATE TABLE projection_extraction_cache (
    cache_key   TEXT NOT NULL,
    fact_id     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (cache_key, fact_id)
) STRICT;

CREATE TABLE projection_embedding_cache (
    cache_key   TEXT NOT NULL PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
) STRICT;
