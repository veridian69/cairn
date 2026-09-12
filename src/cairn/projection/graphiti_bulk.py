"""Pinned Graphiti bulk-deduplication compatibility seam.

Graphiti 0.29.3 rebuilds its complete MinHash/LSH candidate index for each
node in the deterministic cross-batch pass. This module retains that
release's resolution semantics while adding each canonical candidate to one
index once. Delete it when the pinned dependency supplies an incremental
implementation.
"""

from collections import defaultdict
from importlib.metadata import version

from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.helpers import semaphore_gather
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.utils.bulk_utils import _build_directed_uuid_map
from graphiti_core.utils.maintenance import dedup_helpers
from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes
from pydantic import BaseModel

_GRAPHITI_CORE_VERSION = version("graphiti-core")
_GRAPHITI_BULK_COMPATIBILITY_VERSION = "0.29.3"


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
    """Add one canonical node using Graphiti 0.29.3's index rules."""
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
                # canonical value in 0.29.3. Preserve that rare behaviour; it
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
    """Graphiti 0.29.3's provider pass followed by the incremental pass."""
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
        ]
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


__all__ = ["dedupe_nodes_bulk_incremental"]
