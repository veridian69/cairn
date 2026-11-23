"""Pinned behavioural tests for Cairn's Graphiti bulk-dedupe seam."""

import hashlib
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance import dedup_helpers

import cairn.projection.graphiti as graphiti_module
from cairn.projection.graphiti_bulk import (
    _dedupe_nodes_across_batch,
    coalesce_edge_copies,
)


def _node(uuid: str, name: str, labels: list[str] | None = None) -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id="test-group",
        labels=["Entity"] if labels is None else labels,
    )


def test_cross_batch_dedupe_preserves_the_pinned_graphiti_results() -> None:
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


_T0 = datetime(2026, 9, 1, tzinfo=UTC)
_T1 = datetime(2026, 9, 5, tzinfo=UTC)
_T2 = datetime(2026, 9, 9, tzinfo=UTC)


def _edge(uuid: str, episodes: list[str], **fields: Any) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        group_id="test-group",
        source_node_uuid="source",
        target_node_uuid="target",
        created_at=_T0,
        name="WORKS_AT",
        fact=f"fact {uuid}",
        episodes=episodes,
        valid_at=_T0,
        **fields,
    )


def test_a_restated_and_contradicted_edge_keeps_both_changes() -> None:
    """One chunk restates a stored fact in one episode and contradicts it in
    another. Each episode holds its own copy; saving both loses one change."""
    restated = _edge("stored", ["first", "restatement"])
    contradicted = _edge("stored", ["first"], invalid_at=_T1, expired_at=_T2)
    new = _edge("new", ["contradiction"])

    resolved, invalidated = coalesce_edge_copies(
        [restated, new], [contradicted], edge_types_configured=False
    )

    assert resolved == [restated, new]
    assert resolved[0] is restated and resolved[1] is new
    assert invalidated == [restated] and invalidated[0] is restated
    assert restated.episodes == ["first", "restatement"]
    assert (restated.invalid_at, restated.expired_at) == (_T1, _T2)


def test_two_restatements_of_one_edge_keep_both_episodes() -> None:
    first = _edge("stored", ["first", "restatement-a"])
    second = _edge("stored", ["first", "restatement-b"])

    resolved, invalidated = coalesce_edge_copies(
        [first, second], [], edge_types_configured=False
    )

    assert len(resolved) == 1 and resolved[0] is first
    assert invalidated == []
    assert first.episodes == ["first", "restatement-a", "restatement-b"]
    assert first.invalid_at is None and first.expired_at is None


def test_two_contradictions_keep_the_earliest_end_and_the_first_expiry() -> None:
    # Per fact, the library moves invalid_at to the earlier end but leaves
    # the expiry it stamped when the first contradiction was processed.
    stamped_first = datetime(2026, 9, 21, tzinfo=UTC)
    stamped_second = datetime(2026, 9, 22, tzinfo=UTC)
    later = _edge("stored", ["first"], invalid_at=_T2, expired_at=stamped_first)
    earlier = _edge(
        "stored",
        ["first"],
        invalid_at=_T1.replace(tzinfo=None),
        expired_at=stamped_second,
    )

    resolved, invalidated = coalesce_edge_copies(
        [], [later, earlier], edge_types_configured=False
    )

    assert resolved == []
    assert len(invalidated) == 1 and invalidated[0] is later
    assert later.invalid_at == _T1.replace(tzinfo=None)
    assert later.expired_at == stamped_first


def test_without_edge_types_a_cleared_copy_clears_the_stored_attributes() -> None:
    """A stored edge can carry attributes Cairn did not write. With no edge
    types the library keeps them on an exact restatement and clears them
    when a paraphrase resolves to the edge; per fact the edge ends cleared
    whichever comes first."""
    for order in ([{"status": "old"}, {}], [{}, {"status": "old"}]):
        exact_or_cleared = [
            _edge("stored", ["first", f"restatement-{index}"], attributes=dict(value))
            for index, value in enumerate(order)
        ]

        resolved, _ = coalesce_edge_copies(
            exact_or_cleared, [], edge_types_configured=False
        )

        assert len(resolved) == 1 and resolved[0].attributes == {}
        assert resolved[0].episodes == ["first", "restatement-0", "restatement-1"]


@pytest.mark.parametrize(
    ("attributes", "edge_types_configured"),
    [
        # Two different non-empty values: nothing in the library does this
        # without edge types, so there is no rule to follow.
        (({"status": "old"}, {"status": "new"}), False),
        # With edge types a restatement can rewrite or clear the attributes,
        # and the copies do not say which holds the stored value.
        (({"status": "old"}, {"status": "new"}), True),
        (({"status": "old"}, {}), True),
    ],
)
def test_attribute_disagreements_without_a_rule_are_refused(
    attributes: tuple[dict[str, str], dict[str, str]], edge_types_configured: bool
) -> None:
    copies = [
        _edge("stored", ["first"], attributes=dict(value)) for value in attributes
    ]

    with pytest.raises(ValueError, match="edge_copies_attributes_differ"):
        coalesce_edge_copies(copies, [], edge_types_configured=edge_types_configured)


@pytest.mark.anyio
async def test_the_bulk_resolve_override_refuses_disagreeing_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _edge("stored", ["first"], attributes={"status": "old"})
    rewritten = _edge("stored", ["first"], attributes={"status": "new"})

    async def upstream(self: Any, *args: Any, **keywords: Any) -> Any:
        return [], [stored, rewritten], [], {}

    monkeypatch.setattr(Graphiti, "_resolve_nodes_and_edges_bulk", upstream)
    instance = object.__new__(graphiti_module._CairnGraphiti)

    with pytest.raises(graphiti_module.GraphitiIndexError) as raised:
        await instance._resolve_nodes_and_edges_bulk({}, {}, [], None, None, {}, [])

    assert raised.value.code == "graphiti_bulk_edge_attributes"


@pytest.mark.anyio
@pytest.mark.parametrize("edge_types", [None, {}])
async def test_the_bulk_resolve_override_clears_attributes_without_edge_types(
    monkeypatch: pytest.MonkeyPatch, edge_types: dict[str, Any] | None
) -> None:
    exact = _edge("stored", ["first", "exact"], attributes={"status": "old"})
    paraphrase = _edge("stored", ["first", "paraphrase"])

    async def upstream(self: Any, *args: Any, **keywords: Any) -> Any:
        return [], [exact, paraphrase], [], {}

    monkeypatch.setattr(Graphiti, "_resolve_nodes_and_edges_bulk", upstream)
    instance = object.__new__(graphiti_module._CairnGraphiti)

    _, resolved, _, _ = await instance._resolve_nodes_and_edges_bulk(
        {}, {}, [], None, edge_types, {}, []
    )

    assert len(resolved) == 1 and resolved[0].attributes == {}
    assert resolved[0].episodes == ["first", "exact", "paraphrase"]


def test_an_expiry_without_an_end_date_is_kept() -> None:
    plain = _edge("stored", ["first", "restatement"])
    expired = _edge("stored", ["first"], expired_at=_T2)

    coalesce_edge_copies([plain], [expired], edge_types_configured=False)

    assert plain.invalid_at is None and plain.expired_at == _T2


def test_edges_that_appear_once_are_left_alone() -> None:
    one = _edge("one", ["a"])
    two = _edge("two", ["b"], invalid_at=_T1, expired_at=_T2)
    before = (one.model_dump(), two.model_dump())

    resolved, invalidated = coalesce_edge_copies(
        [one], [two], edge_types_configured=False
    )

    assert resolved[0] is one and invalidated[0] is two
    assert (one.model_dump(), two.model_dump()) == before


def test_the_same_object_in_both_lists_is_not_a_second_copy() -> None:
    edge = _edge("stored", ["first"], invalid_at=_T1, expired_at=_T2)

    resolved, invalidated = coalesce_edge_copies(
        [edge], [edge], edge_types_configured=False
    )

    assert resolved[0] is edge and invalidated[0] is edge
    assert edge.episodes == ["first"]


def test_the_bulk_resolve_override_has_upstreams_signature() -> None:
    upstream = inspect.signature(Graphiti._resolve_nodes_and_edges_bulk)
    override = inspect.signature(
        graphiti_module._CairnGraphiti._resolve_nodes_and_edges_bulk
    )

    assert [(p.name, p.kind, p.default) for p in override.parameters.values()] == [
        (p.name, p.kind, p.default) for p in upstream.parameters.values()
    ]


@pytest.mark.anyio
async def test_the_bulk_resolve_override_coalesces_upstreams_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restated = _edge("stored", ["first", "restatement"])
    contradicted = _edge("stored", ["first"], invalid_at=_T1, expired_at=_T2)
    received: list[Any] = []
    node = _node("node", "Node")

    async def upstream(self: Any, *args: Any, **keywords: Any) -> Any:
        received.append((args, keywords))
        return [node], [restated], [contradicted], {"a": "b"}

    monkeypatch.setattr(Graphiti, "_resolve_nodes_and_edges_bulk", upstream)
    instance = object.__new__(graphiti_module._CairnGraphiti)
    clients = object()

    result = await instance._resolve_nodes_and_edges_bulk(
        {},
        {},
        [],
        None,
        None,
        {},
        [],
        clients=clients,  # type: ignore[arg-type]
    )

    assert received == [(({}, {}, [], None, None, {}, []), {"clients": clients})]
    nodes, resolved, invalidated, uuid_map = result
    assert nodes == [node] and uuid_map == {"a": "b"}
    assert resolved == [restated] and invalidated == [restated]
    assert invalidated[0] is restated
    assert restated.invalid_at == _T1 and restated.episodes == ["first", "restatement"]
