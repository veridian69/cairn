"""Cairn retrieval-index projection: the adapter protocol and its frozen
result values, the partition identity with its Graphiti derivation, and
the real Graphiti adapter. The outbox deliverer joins in Task 4."""

from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.partition import (
    canonical_partition,
    canonical_segments_json,
    derive_group_id,
)

# cairn.projection.graphiti is deliberately not re-exported: importing it
# pulls the whole graphiti-core dependency tree, which a disabled instance
# (P-39's default) never needs. Composition imports it explicitly, and
# only when config.graphiti.enabled is true.
__all__ = [
    "FactProjected",
    "IndexAdapter",
    "ProjectedFactState",
    "ProjectionFailed",
    "canonical_partition",
    "canonical_segments_json",
    "derive_group_id",
]
