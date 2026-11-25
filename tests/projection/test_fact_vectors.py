"""Fact representation tests: content preservation and bounded provider work."""

import asyncio
import math
from types import SimpleNamespace
from typing import Any, cast

import pytest


class Embedder:
    def __init__(self, response: list[list[float]] | None = None) -> None:
        self.config = SimpleNamespace(
            embedding_model="text-embedding-3-small", embedding_dim=2
        )
        self.calls: list[list[str]] = []
        self.response = response

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return (
            self.response if self.response is not None else [[3.0, 4.0] for _ in texts]
        )


def representation(embedder: Any) -> Any:
    from cairn.projection.fact_vectors import FactRepresentation

    return FactRepresentation(embedder)


def test_chunks_preserve_exact_unicode_and_special_token_spellings() -> None:
    from cairn.projection.fact_vectors import utf8_chunks

    body = "a" * 2047 + "🙂e\u0301漢字<|endoftext|>\r\n" * 200
    chunks = list(utf8_chunks(body))
    assert chunks[0] == "a" * 2047
    assert "".join(chunks) == body
    assert b"".join(chunk.encode("utf-8") for chunk in chunks) == body.encode("utf-8")
    assert all(0 < len(chunk.encode("utf-8")) <= 2048 for chunk in chunks)
    assert all(chunk.encode("utf-8").decode("utf-8") == chunk for chunk in chunks)


def test_pool_uses_byte_weights_and_reuses_one_query_batch() -> None:
    embedder = Embedder([[1.0, 0.0], [0.0, 1.0]])
    rep = representation(embedder)
    vector = asyncio.run(rep.embed_text("a" * 2048 + "b" * 1024))
    assert vector == pytest.approx([2 / math.sqrt(5), 1 / math.sqrt(5)])
    assert len(embedder.calls) == 1


def test_maximum_body_is_streamed_in_bounded_batches() -> None:
    embedder = Embedder()
    vector = asyncio.run(representation(embedder).embed_text("x" * 65536))
    assert vector == pytest.approx([0.6, 0.8])
    assert len(embedder.calls) == 4
    assert all(
        len(call) <= 32 and sum(len(text.encode()) for text in call) <= 16384
        for call in embedder.calls
    )


@pytest.mark.parametrize(
    "response",
    [
        [],
        [[1.0]],
        [[1.0, 0.0], [1.0, 0.0]],
        [[float("nan"), 1.0]],
        [[0.0, 0.0]],
        [[True, 1.0]],
    ],
)
def test_malformed_provider_result_cannot_become_a_fact_vector(response: Any) -> None:
    from cairn.projection.fact_vectors import FactVectorError

    with pytest.raises(FactVectorError):
        asyncio.run(representation(Embedder(response)).embed_text("ordinary fact"))


def test_cutoff_metadata_changes_search_identity_not_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.projection import fact_vectors

    rep = representation(Embedder())
    before = fact_vectors.policy_metadata(rep)
    monkeypatch.setattr(fact_vectors, "FACT_CUTOFF", 0.7)
    after = fact_vectors.policy_metadata(rep)
    assert before["search"]["sha256"] != after["search"]["sha256"]
    assert before["representation"] == after["representation"]
    assert after["search"]["cutoff"] == 0.7


@pytest.mark.parametrize("position", [0, 32768, 65535], ids=["start", "middle", "end"])
def test_maximum_body_all_positions_contribute_without_truncation(
    position: int,
) -> None:
    class Controlled(Embedder):
        async def create_batch(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return [[1.0, 0.0] if "!" in text else [0.0, 1.0] for text in texts]

    body = "x" * position + "!" + "x" * (65535 - position)
    embedder = Controlled()
    vector = asyncio.run(representation(embedder).embed_text(body))
    assert "".join(text for batch in embedder.calls for text in batch) == body
    assert vector == pytest.approx([1 / math.sqrt(962), 31 / math.sqrt(962)])


def test_invalid_second_result_publishes_no_first_fact() -> None:
    from cairn.projection.fact_vectors import FactVectorError

    published = []

    async def run() -> None:
        async for result in representation(
            Embedder([[1.0, 0.0], [0.0, 0.0]])
        ).embed_many(["first", "second"]):
            published.append(result)

    with pytest.raises(FactVectorError):
        asyncio.run(run())
    assert published == []


def test_opposing_chunks_cannot_publish_a_zero_pooled_vector() -> None:
    from cairn.projection.fact_vectors import FactVectorError

    with pytest.raises(FactVectorError):
        asyncio.run(
            representation(Embedder([[1.0, 0.0], [-1.0, 0.0]])).embed_text("x" * 4096)
        )


def test_body_and_dimension_bind_representation_fingerprint() -> None:
    embedder = Embedder()
    old = representation(embedder)
    assert old.fingerprint("e\u0301") != old.fingerprint("é")
    embedder.config.embedding_dim = 3
    new = representation(embedder)
    assert old.metadata()["sha256"] != new.metadata()["sha256"]
    assert old.fingerprint("same") != new.fingerprint("same")


@pytest.mark.parametrize("text", ["", "\ud800"])
def test_invalid_text_fails_before_embedding(text: str) -> None:
    from cairn.projection.fact_vectors import FactVectorError

    embedder = Embedder()
    with pytest.raises(FactVectorError):
        asyncio.run(representation(embedder).embed_text(text))
    assert embedder.calls == []


def test_vector_audit_sends_the_gate_rows_for_one_partition_and_folds_them() -> None:
    from cairn.projection import fact_vectors as module
    from cairn.projection.adapter import CoverageAudit

    seen: list[tuple[str, dict[str, Any]]] = []

    class Driver:
        async def execute_query(self, query: str, **kwargs: Any) -> Any:
            seen.append((query, kwargs))
            return (
                [{"uuid": "a" * 8, "valid": True}, {"uuid": "b" * 8, "valid": False}],
                None,
                None,
            )

    index = module.FactVectorIndex(representation(Embedder()))
    audit = asyncio.run(index.audit(cast(Any, Driver()), "g" * 64))

    assert seen == [
        (
            module._COVERAGE_ROWS,
            {
                "group_id": "g" * 64,
                "representation": index.representation.metadata()["sha256"],
                "dimension": 2,
            },
        )
    ]
    assert audit == CoverageAudit(
        frozenset({"a" * 8, "b" * 8}), frozenset({"b" * 8}), 0
    )


def test_vector_coverage_rows_share_the_gate_predicate_and_nothing_of_the_search() -> (
    None
):
    from cairn.projection import fact_vectors as module

    gate = module._COVERAGE_SEARCH.partition("WITH e.uuid AS uuid")[0]
    assert module._COVERAGE_ROWS.startswith(gate)
    assert module._COVERAGE_ONLY.startswith(gate)
    assert "coalesce(" in gate
    assert module._COVERAGE_ROWS.endswith("RETURN e.uuid AS uuid, valid\n")
    for forbidden in ("$query_vector", "$cutoff", "$limit", "$scoring", "score"):
        assert forbidden not in module._COVERAGE_ROWS


# --- #318: partition verdicts ------------------------------------------------

EVEN = (3, 3, 3)


def _verdicts() -> Any:
    from cairn.projection.fact_vectors import PartitionVerdicts

    return PartitionVerdicts()


def test_a_passed_gated_read_establishes_a_verdict_for_its_own_scope() -> None:
    verdicts = _verdicts()
    assert verdicts.held("g", False) is None
    verdicts.establish("g", verdicts.epoch("g"), EVEN, False)
    assert verdicts.held("g", False) == (0, EVEN)
    # A read that did not walk the unit gate proves nothing about units.
    assert verdicts.held("g", True) is None
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    assert verdicts.held("g", True) == (0, EVEN)
    # A later narrower pass does not shrink the proof already held.
    verdicts.establish("g", verdicts.epoch("g"), EVEN, False)
    assert verdicts.held("g", True) == (0, EVEN)
    assert verdicts.held("other", False) is None


@pytest.mark.parametrize(
    ("counts", "units"),
    [((3, 2, 3), False), ((3, 4, 3), False), ((3, 3, 2), True), ((3, 3, 4), True)],
)
def test_disagreeing_counts_establish_nothing(
    counts: tuple[int, int, int], units: bool
) -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), counts, units)
    assert verdicts.held("g", units) is None


def test_headers_are_not_counted_where_units_are_not_graded() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), (3, 3, 0), False)
    assert verdicts.held("g", False) == (0, (3, 3, 0))


def test_a_write_or_a_drop_during_a_gated_read_establishes_nothing() -> None:
    verdicts = _verdicts()
    epoch = verdicts.epoch("g")
    verdicts.begin_write("g")
    verdicts.end_write("g", EVEN, True)
    verdicts.establish("g", epoch, EVEN, True)
    assert verdicts.held("g", False) is None
    epoch = verdicts.epoch("g")
    verdicts.drop("g")
    verdicts.establish("g", epoch, EVEN, True)
    assert verdicts.held("g", False) is None
    # A write still running refuses even a read that began after it did.
    verdicts.begin_write("g")
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    assert verdicts.held("g", False) is None


@pytest.mark.parametrize("held_before", [False, True])
@pytest.mark.parametrize("counts", [None, (4, 4, 4)])
def test_a_gated_read_that_began_during_a_write_establishes_nothing(
    held_before: bool, counts: tuple[int, int, int] | None
) -> None:
    """Security review, P2: the read took its epoch after the write began
    and finished after it ended, failed or not."""
    verdicts = _verdicts()
    if held_before:
        verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    verdicts.begin_write("g")
    epoch = verdicts.epoch("g")
    verdicts.end_write("g", counts, True)
    before = verdicts.held("g", True)
    verdicts.establish("g", epoch, (9, 9, 9), True)
    assert verdicts.held("g", True) == before
    assert before is None or before[1] == (4, 4, 4)


def test_a_successful_verified_write_restores_the_verdict_with_new_counts() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    held = verdicts.held("g", True)
    assert held is not None
    assert verdicts.begin_write("g") is True
    assert verdicts.held("g", True) is None
    assert verdicts.unchanged("g", held[0]) is False
    verdicts.end_write("g", (4, 4, 4), True)
    restored = verdicts.held("g", True)
    assert restored is not None and restored[1] == (4, 4, 4)
    assert restored[0] > held[0]


def test_a_write_cannot_create_a_verdict_that_was_not_held() -> None:
    verdicts = _verdicts()
    assert verdicts.begin_write("g") is False
    verdicts.end_write("g", EVEN, True)
    assert verdicts.held("g", False) is None


@pytest.mark.parametrize("counts", [None, (4, 3, 4), (4, 4, 3)])
def test_a_failed_or_inconsistent_write_leaves_the_partition_unverified(
    counts: tuple[int, int, int] | None,
) -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    verdicts.begin_write("g")
    verdicts.end_write("g", counts, True)
    assert verdicts.held("g", False) is None


def test_a_write_that_did_not_verify_units_narrows_the_restored_proof() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    verdicts.begin_write("g")
    verdicts.end_write("g", (4, 4, 3), False)
    assert verdicts.held("g", True) is None
    assert verdicts.held("g", False) is not None


def test_overlapping_writes_restore_only_when_all_succeed() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    assert verdicts.begin_write("g") is True
    assert verdicts.begin_write("g") is True
    verdicts.end_write("g", (4, 4, 4), True)
    assert verdicts.held("g", True) is None  # one writer still running
    verdicts.end_write("g", (5, 5, 5), True)
    held = verdicts.held("g", True)
    assert held is not None and held[1] == (5, 5, 5)

    assert verdicts.begin_write("g") is True
    assert verdicts.begin_write("g") is True
    verdicts.end_write("g", None, True)
    assert verdicts.begin_write("g") is False  # joins a failed set of writes
    verdicts.end_write("g", (6, 6, 6), True)
    verdicts.end_write("g", (6, 6, 6), True)
    assert verdicts.held("g", False) is None


def test_a_drop_during_a_write_is_not_undone_by_its_end() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    verdicts.establish("h", verdicts.epoch("h"), EVEN, True)
    verdicts.begin_write("g")
    verdicts.drop()
    verdicts.end_write("g", (4, 4, 4), True)
    assert verdicts.held("g", False) is None
    assert verdicts.held("h", False) is None


def test_reads_of_any_number_of_partitions_retain_nothing() -> None:
    """Codex security review, P3: a caller may name any partition. Asking
    about one, or passing the gates of an empty one, must not keep state."""
    verdicts = _verdicts()
    for position in range(1000):
        group = f"g{position}"
        epoch = verdicts.epoch(group)
        assert verdicts.held(group, False) is None
        assert verdicts.unchanged(group, epoch) is False
        verdicts.establish(group, epoch, (0, 0, 0), True)
        verdicts.drop(group)
        assert verdicts.held(group, False) is None
    assert verdicts._verdicts == {}


def test_a_partition_without_an_entry_still_notices_writes_and_drops() -> None:
    verdicts = _verdicts()
    epoch = verdicts.epoch("g")
    verdicts.begin_write("g")
    verdicts.end_write("g", EVEN, True)
    verdicts.establish("g", epoch, EVEN, True)
    assert verdicts.held("g", False) is None

    epoch = verdicts.epoch("h")
    verdicts.drop()
    verdicts.establish("h", epoch, EVEN, True)
    assert verdicts.held("h", False) is None
    verdicts.establish("h", verdicts.epoch("h"), EVEN, True)
    assert verdicts.held("h", True) is not None


def test_a_drop_of_everything_forgets_all_but_running_writes() -> None:
    verdicts = _verdicts()
    for group in ("a", "b", "c"):
        verdicts.establish(group, verdicts.epoch(group), EVEN, True)
    verdicts.begin_write("b")
    epoch = verdicts.epoch("a")
    verdicts.drop()
    assert set(verdicts._verdicts) == {"b"}
    # No epoch taken before the drop is good after it, entry or not.
    assert verdicts.epoch("a") != epoch
    verdicts.establish("a", epoch, EVEN, True)
    assert verdicts.held("a", False) is None
    verdicts.end_write("b", (4, 4, 4), True)
    assert verdicts.held("b", False) is None


def test_an_abandoned_write_keeps_the_partition_gated_for_good() -> None:
    """Security review, round 2: the call gave up, the work may run on."""
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    verdicts.establish("h", verdicts.epoch("h"), EVEN, True)
    verdicts.begin_write("g")
    epoch = verdicts.epoch("g")
    verdicts.abandon_write("g")
    assert verdicts.held("g", False) is None
    verdicts.establish("g", epoch, EVEN, True)
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    assert verdicts.held("g", False) is None
    # A later write that succeeds cannot restore it either.
    assert verdicts.begin_write("g") is False
    verdicts.end_write("g", (4, 4, 4), True)
    verdicts.establish("g", verdicts.epoch("g"), (4, 4, 4), True)
    assert verdicts.held("g", False) is None
    assert verdicts.held("h", True) is not None


def test_a_trusted_read_notices_a_write_that_began_after_it() -> None:
    verdicts = _verdicts()
    verdicts.establish("g", verdicts.epoch("g"), EVEN, True)
    held = verdicts.held("g", True)
    assert held is not None and verdicts.unchanged("g", held[0])
    verdicts.begin_write("g")
    verdicts.end_write("g", (4, 4, 4), True)
    # Restored, but not the verdict that read relied on.
    assert verdicts.unchanged("g", held[0]) is False


def test_named_rows_must_name_exactly_the_written_facts_all_valid() -> None:
    from cairn.projection.fact_vectors import named_rows_valid

    good = [{"uuid": "a", "valid": True}, {"uuid": "b", "valid": True}]
    assert named_rows_valid(good, ["a", "b"])
    assert named_rows_valid(good, ["b", "a", "a"])
    assert named_rows_valid([], [])
    for rows in (
        good[:1],
        good + [{"uuid": "c", "valid": True}],
        [good[0], {"uuid": "b", "valid": False}],
        [good[0], {"uuid": "b", "valid": 1}],
        [good[0], {"uuid": "a", "valid": True}],
        [good[0], {"uuid": 2, "valid": True}],
        [good[0], {"uuid": "b", "valid": True, "extra": 1}],
        "rows",
        None,
    ):
        assert not named_rows_valid(rows, ["a", "b"])


def test_the_named_gate_is_the_gate_restricted_to_named_episodes() -> None:
    import cairn.projection.fact_unit_index as fact_unit_index
    import cairn.projection.fact_vectors as fact_vectors

    for module in (fact_vectors, fact_unit_index):
        named, rows = module._COVERAGE_NAMED, module._COVERAGE_ROWS
        assert named.count("e.uuid IN $uuids") == 1
        assert (
            named.replace(" WHERE e.uuid IN $uuids AND", " WHERE").replace(
                " WHERE e.uuid IN $uuids", ""
            )
            == rows
        )
