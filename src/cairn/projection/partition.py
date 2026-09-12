"""Partition identity and its Graphiti transport spelling (P-39 as amended
9 August 2026).

The Cairn partition identity is I-78's ``(realm_id, scope_segments)``
pair, canonically encoded as the realm identifier and the canonical
``scope_segments`` JSON joined by a single ``\\n`` — the same JSON the
catalogue stores, so at delivery time the stored column is the encoding.

Graphiti cannot accept that encoding as a ``group_id`` (its charset is
``[a-zA-Z0-9_-]``, and under FalkorDB the key becomes a graph name), so
the adapter spells it through a versioned, domain-separated SHA-256
derivation. The digest is a transport spelling applied at this boundary
and nowhere else: nothing above the adapter reasons in digests, nothing
is allocated, and no mapping is stored — the derivation is a pure
function recomputable from the pair at any time, which is why I-78's
"no new identifier is minted" holds literally.

The canonical encoding contains a scope path and must never be logged
(I-32). The digest deliberately does not, which shrinks the blast radius
of an accidental index-side diagnostic; that is not a licence to log it.
"""

import hashlib
import json

from cairn.catalogue.audit import ScopeSegment

# Version and domain separation in one label: a future derivation is a new
# label, and another Cairn digest over the same bytes can never share this
# namespace. The NUL joint cannot appear in the UTF-8 canonical encoding.
_DERIVATION_DOMAIN = b"cairn.graphiti.partition/v1\x00"


def canonical_segments_json(segments: tuple[ScopeSegment, ...]) -> str:
    """The canonical ``scope_segments`` JSON — byte-identical to the
    catalogue's stored column, which ``cairn.authority.mutations`` writes
    through the same sorted-key minified encoding. A test pins the two
    against each other so this cannot drift silently."""
    documents = [
        {"kind": segment.kind, "id": segment.identifier} for segment in segments
    ]
    return json.dumps(
        documents, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_partition(realm_id: str, segments_json: str) -> str:
    """The canonical encoding of the partition identity. ``segments_json``
    is the canonical JSON — a stored ``scope_segments`` column value, or
    ``canonical_segments_json`` output; the two are the same bytes."""
    return f"{realm_id}\n{segments_json}"


def derive_group_id(partition: str) -> str:
    """The Graphiti ``group_id`` for a canonical partition encoding: a
    fixed 64-character lowercase hex digest, never truncated."""
    return hashlib.sha256(_DERIVATION_DOMAIN + partition.encode("utf-8")).hexdigest()
