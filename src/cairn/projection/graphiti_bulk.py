"""Pinned Graphiti bulk-deduplication compatibility seam.

Graphiti 0.31.2 rebuilds its complete MinHash/LSH candidate index for each
node in the deterministic cross-batch pass. This module retains that
release's resolution semantics while adding each canonical candidate to one
index once. Delete it when the pinned dependency supplies an incremental
implementation.
"""

from collections import defaultdict
from datetime import UTC, datetime
from importlib.metadata import version

from graphiti_core.edges import EntityEdge
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.helpers import semaphore_gather
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.utils.bulk_utils import _build_directed_uuid_map
from graphiti_core.utils.maintenance import dedup_helpers
from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes
from pydantic import BaseModel

_GRAPHITI_CORE_VERSION = version("graphiti-core")
_GRAPHITI_BULK_COMPATIBILITY_VERSION = "0.31.2"


def _require_graphiti_bulk_compatibility() -> None:
    if _GRAPHITI_CORE_VERSION != _GRAPHITI_BULK_COMPATIBILITY_VERSION:
        raise RuntimeError("graphiti_bulk_compatibility_version")


def _empty_candidate_indexes() -> dedup_helpers.DedupCandidateIndexes:
    return dedup_helpers.DedupCandidateIndexes(
        existing_nodes=[],
        nodes_by_uuid={},
        normalized_existing=defaultdict(list),
        shingles_by_candidate={},
        lsh_buckets=defaultdict(list),
    )


def _add_candidate(
    indexes: dedup_helpers.DedupCandidateIndexes,
    candidate: EntityNode,
) -> None:
    """Add one canonical node using Graphiti 0.31.2's index rules."""
    indexes.existing_nodes.append(candidate)
    normalized = dedup_helpers._normalize_string_exact(candidate.name)
    indexes.normalized_existing[normalized].append(candidate)
    indexes.nodes_by_uuid[candidate.uuid] = candidate

    shingles = dedup_helpers._cached_shingles(
        dedup_helpers._normalize_name_for_fuzzy(candidate.name)
    )
    indexes.shingles_by_candidate[candidate.uuid] = shingles
    signature = dedup_helpers._minhash_signature(shingles)
    for band_index, band in enumerate(dedup_helpers._lsh_bands(signature)):
        indexes.lsh_buckets[(band_index, band)].append(candidate.uuid)


def _dedupe_nodes_across_batch(
    episode_resolutions: list[tuple[str, list[EntityNode]]],
    per_episode_uuid_maps: list[dict[str, str]],
    first_pass_duplicate_pairs: list[tuple[str, str]],
) -> tuple[dict[str, list[EntityNode]], dict[str, str]]:
    """Run Graphiti's ordered second pass with an incremental candidate index."""
    duplicate_pairs = list(first_pass_duplicate_pairs)
    canonical_nodes: dict[str, EntityNode] = {}
    indexes = _empty_candidate_indexes()

    for _, resolved_nodes in episode_resolutions:
        for node in resolved_nodes:
            normalized = dedup_helpers._normalize_string_exact(node.name)
            exact_matches = indexes.normalized_existing.get(normalized, [])
            if exact_matches:
                exact_match = exact_matches[0]
                if exact_match.uuid != node.uuid:
                    duplicate_pairs.append((node.uuid, exact_match.uuid))
                continue

            state = dedup_helpers.DedupResolutionState(
                resolved_nodes=[None],
                uuid_map={},
                unresolved_indices=[],
            )
            dedup_helpers._resolve_with_similarity([node], indexes, state)
            resolved = state.resolved_nodes[0]
            if resolved is not None:
                canonical_nodes.setdefault(resolved.uuid, resolved)
                if resolved.uuid != node.uuid:
                    duplicate_pairs.append((node.uuid, resolved.uuid))
                continue

            replacing = node.uuid in canonical_nodes
            canonical_nodes[node.uuid] = node
            if replacing:
                # A repeated UUID with a different unmatched name replaces the
                # canonical value in 0.31.2. Preserve that rare behaviour; it
                # is not the distinct-node path responsible for P-87's O(n²).
                indexes = dedup_helpers._build_candidate_indexes(
                    list(canonical_nodes.values())
                )
            else:
                _add_candidate(indexes, node)

    union_pairs = [
        pair for uuid_map in per_episode_uuid_maps for pair in uuid_map.items()
    ]
    union_pairs.extend(duplicate_pairs)
    compressed_map = _build_directed_uuid_map(union_pairs)

    nodes_by_episode: dict[str, list[EntityNode]] = {}
    for episode_uuid, resolved_nodes in episode_resolutions:
        deduped_nodes: list[EntityNode] = []
        seen: set[str] = set()
        for node in resolved_nodes:
            canonical_uuid = compressed_map.get(node.uuid, node.uuid)
            if canonical_uuid in seen:
                continue
            seen.add(canonical_uuid)
            canonical_node = canonical_nodes.get(canonical_uuid)
            if canonical_node is None:
                canonical_node = node
            deduped_nodes.append(canonical_node)
        nodes_by_episode[episode_uuid] = deduped_nodes

    return nodes_by_episode, compressed_map


async def dedupe_nodes_bulk_incremental(
    clients: GraphitiClients,
    extracted_nodes: list[list[EntityNode]],
    episode_tuples: list[tuple[EpisodicNode, list[EpisodicNode]]],
    entity_types: dict[str, type[BaseModel]] | None = None,
) -> tuple[dict[str, list[EntityNode]], dict[str, str]]:
    """Graphiti 0.31.2's provider pass followed by the incremental pass."""
    _require_graphiti_bulk_compatibility()
    first_pass_results = await semaphore_gather(
        *[
            resolve_extracted_nodes(
                clients,
                nodes,
                episode_tuples[index][0],
                episode_tuples[index][1],
                entity_types,
            )
            for index, nodes in enumerate(extracted_nodes)
        ],
        max_coroutines=getattr(clients, "max_coroutines", None),
    )

    episode_resolutions: list[tuple[str, list[EntityNode]]] = []
    per_episode_uuid_maps: list[dict[str, str]] = []
    duplicate_pairs: list[tuple[str, str]] = []
    for (resolved_nodes, uuid_map, duplicates), (episode, _) in zip(
        first_pass_results,
        episode_tuples,
        strict=True,
    ):
        episode_resolutions.append((episode.uuid, resolved_nodes))
        per_episode_uuid_maps.append(uuid_map)
        duplicate_pairs.extend(
            (source.uuid, target.uuid) for source, target in duplicates
        )

    return _dedupe_nodes_across_batch(
        episode_resolutions,
        per_episode_uuid_maps,
        duplicate_pairs,
    )


def coalesce_edge_copies(
    resolved_edges: list[EntityEdge],
    invalidated_edges: list[EntityEdge],
    *,
    edge_types_configured: bool,
) -> tuple[list[EntityEdge], list[EntityEdge]]:
    """Leave one object per edge uuid across a bulk chunk's two edge lists.

    Graphiti's bulk path resolves each episode's edges against the graph on
    its own, so two episodes of one chunk that touch the same stored edge
    each hold a separate copy of it: a restatement appends its episode to
    one copy, a contradiction sets ``invalid_at`` and ``expired_at`` on
    another, and a second restatement appends to a third. The library then
    saves every copy under the one uuid and the index keeps one of them,
    dropping the others' change: a contradicted fact stays current, or a
    restatement's provenance is lost.

    The copies are folded into the first one, which is what the per-fact
    path arrives at when it meets the same episodes one by one: the union of
    their episodes in first-seen order, the earliest ``invalid_at`` any copy
    carries, and the earliest ``expired_at`` (the library stamps expiry once
    and never moves it later). Each returned list names a uuid at most once,
    in its original order, and an edge in both lists is the same object.
    Edges that appear once are returned untouched.

    ``attributes`` are the one field the copies can disagree on without a
    rule to settle it. Without configured edge types, which is how Cairn
    calls the bulk path, the library never extracts attributes; the only
    change it makes is to clear a stored edge's attributes when a paraphrase
    resolves to it (an exact restatement leaves them). Per fact, the edge
    ends up cleared whichever comes first, so a cleared copy wins. Any other
    disagreement, and every disagreement with edge types configured, where a
    restatement can rewrite the attributes and the copies do not say which
    holds the stored value, is refused rather than guessed.
    """
    copies: dict[str, list[EntityEdge]] = defaultdict(list)
    for edge in [*resolved_edges, *invalidated_edges]:
        if not any(edge is seen for seen in copies[edge.uuid]):
            copies[edge.uuid].append(edge)

    canonical: dict[str, EntityEdge] = {}
    for uuid, group in copies.items():
        first = group[0]
        canonical[uuid] = first
        if len(group) == 1:
            continue
        distinct = [
            attributes
            for index, attributes in enumerate(copy.attributes for copy in group)
            if attributes not in [copy.attributes for copy in group[:index]]
        ]
        if len(distinct) > 1:
            kept = [attributes for attributes in distinct if attributes]
            if edge_types_configured or len(kept) != 1:
                raise ValueError("edge_copies_attributes_differ")
            first.attributes = {}
        first.episodes = list(dict.fromkeys(e for copy in group for e in copy.episodes))
        first.invalid_at = min(
            (copy.invalid_at for copy in group if copy.invalid_at is not None),
            key=_instant,
            default=None,
        )
        first.expired_at = min(
            (copy.expired_at for copy in group if copy.expired_at is not None),
            key=_instant,
            default=None,
        )

    def unique(edges: list[EntityEdge]) -> list[EntityEdge]:
        return [canonical[uuid] for uuid in dict.fromkeys(edge.uuid for edge in edges)]

    return unique(resolved_edges), unique(invalidated_edges)


def _instant(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["coalesce_edge_copies", "dedupe_nodes_bulk_incremental"]
