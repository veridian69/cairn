"""P-39 as amended: the partition identity, its canonical encoding, and
the versioned Graphiti derivation applied at the adapter boundary."""

import hashlib

from cairn.authority.mutations import _segments_column
from cairn.catalogue.audit import ScopeSegment
from cairn.projection.partition import (
    canonical_partition,
    canonical_segments_json,
    derive_group_id,
)

SEGMENTS = (
    ScopeSegment("repo", "api"),
    ScopeSegment("job", "build:42/step@2+x%7E"),
)


def test_the_canonical_segments_json_matches_the_stored_column() -> None:
    """The derivation's preimage must be byte-identical to what the
    catalogue stores, or an upgrade-vs-fresh instance could partition the
    same scope twice. Pinned against the authority module's own writer,
    which every ``scope_segments`` column goes through."""
    assert canonical_segments_json(SEGMENTS) == _segments_column(SEGMENTS)


def test_the_canonical_partition_is_realm_newline_json() -> None:
    encoded = canonical_partition("acme", canonical_segments_json(SEGMENTS))

    realm, _, segments_json = encoded.partition("\n")
    assert realm == "acme"
    assert segments_json == canonical_segments_json(SEGMENTS)


def test_the_realm_root_partition_is_the_empty_array() -> None:
    assert canonical_partition("acme", canonical_segments_json(())) == "acme\n[]"


def test_the_derivation_is_the_ruled_versioned_digest() -> None:
    """The exact formula Operator ruled on 9 August 2026, recomputed here from
    first principles so the implementation cannot drift from the plan:
    sha256 of the domain label, a NUL, and the canonical encoding."""
    encoded = canonical_partition("acme", canonical_segments_json(SEGMENTS))

    expected = hashlib.sha256(
        b"cairn.graphiti.partition/v1\x00" + encoded.encode("utf-8")
    ).hexdigest()

    assert derive_group_id(encoded) == expected


def test_the_derived_key_is_graphiti_safe_and_scope_free() -> None:
    """The two properties the amendment exists for: the spelling always
    fits Graphiti's ``[a-zA-Z0-9_-]`` charset at fixed length, and no
    fragment of the scope path survives into it."""
    encoded = canonical_partition("acme", canonical_segments_json(SEGMENTS))

    derived = derive_group_id(encoded)

    assert len(derived) == 64
    assert derived == derived.lower()
    assert all(character in "0123456789abcdef" for character in derived)
    for fragment in ("acme", "repo", "api", "job", "build"):
        assert fragment not in derived


def test_distinct_partitions_derive_distinct_keys() -> None:
    """The I-78 invariant in Operator's terms: distinct Cairn partitions map
    deterministically and unambiguously to distinct groups. Sibling,
    ancestor, cross-realm and realm-root partitions must all separate."""
    api = canonical_partition("acme", canonical_segments_json(SEGMENTS[:1]))
    sibling = canonical_partition(
        "acme", canonical_segments_json((ScopeSegment("repo", "web"),))
    )
    deeper = canonical_partition("acme", canonical_segments_json(SEGMENTS))
    root = canonical_partition("acme", canonical_segments_json(()))
    other_realm = canonical_partition("beta", canonical_segments_json(SEGMENTS[:1]))

    derived = {
        derive_group_id(key) for key in (api, sibling, deeper, root, other_realm)
    }

    assert len(derived) == 5
    assert derive_group_id(api) == derive_group_id(api)
