"""Pinned behavioural tests for Cairn's Graphiti bulk-dedupe seam."""

import hashlib

import pytest
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance import dedup_helpers

from cairn.projection.graphiti_bulk import _dedupe_nodes_across_batch


def _node(uuid: str, name: str, labels: list[str] | None = None) -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id="test-group",
        labels=["Entity"] if labels is None else labels,
    )


def test_cross_batch_dedupe_preserves_graphiti_0302_results() -> None:
    """Changing matching order, promotion or UUID compression must fail."""
    acme = _node("acme", "Acme Corporation Holdings")
    observatory = _node("observatory", "Northern Observatory")
    fuzzy_acme = _node(
        "acme-alias",
        "Acme Corporation Holding",
        ["Entity", "Organisation"],
    )
    exact_observatory = _node("observatory-alias", "Northern Observatory")
    unique = _node("unique", "Southern Research Institute")

    nodes_by_episode, uuid_map = _dedupe_nodes_across_batch(
        [
            ("episode-1", [acme, observatory]),
            ("episode-2", [fuzzy_acme, exact_observatory, unique]),
        ],
        [{"raw-acme": "acme"}, {"raw-alias": "acme-alias"}],
        [("first-pass-alias", "observatory")],
    )

    assert [node.uuid for node in nodes_by_episode["episode-1"]] == [
        "acme",
        "observatory",
    ]
    assert [node.uuid for node in nodes_by_episode["episode-2"]] == [
        "acme",
        "observatory",
        "unique",
    ]
    assert acme.labels == ["Entity", "Organisation"]
    assert uuid_map == {
        "raw-acme": "acme",
        "acme": "acme",
        "raw-alias": "acme",
        "acme-alias": "acme",
        "observatory-alias": "observatory",
        "observatory": "observatory",
        "first-pass-alias": "observatory",
    }


def test_cross_batch_dedupe_computes_minhash_signatures_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuilding the candidate index per node makes this count quadratic."""
    real_signature = dedup_helpers._minhash_signature
    signature_calls = 0

    def counted_signature(shingles: set[str]) -> tuple[int, ...]:
        nonlocal signature_calls
        signature_calls += 1
        return real_signature(shingles)

    monkeypatch.setattr(dedup_helpers, "_minhash_signature", counted_signature)
    nodes = [
        _node(
            f"node-{index}",
            hashlib.sha256(str(index).encode()).hexdigest(),
        )
        for index in range(40)
    ]

    nodes_by_episode, _ = _dedupe_nodes_across_batch(
        [("episode", nodes)],
        [{}],
        [],
    )

    assert len(nodes_by_episode["episode"]) == 40
    assert signature_calls <= 80
