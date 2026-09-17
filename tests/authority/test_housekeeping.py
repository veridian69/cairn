"""Read-only suggestions from real authorised catalogue memory, never actions."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import test_retrieval as h
from test_memory import _memory, _seed
from test_retrieval import (
    _CORRELATION_ID,
    _SCOPE,
    _agent_actor,
    _authority,
    _ingest_facts,
    _rows,
)

from cairn.authority.mutations import InvalidateFacts
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import Committed, Rejected


def _suggest(path: Path, **kwargs: object) -> Any:
    service = _memory(path)
    assert hasattr(service, "suggest"), "CairnMemory.suggest is not implemented"
    from cairn.authority.housekeeping_types import Suggest

    return service.suggest(
        _agent_actor(),
        Suggest(_SCOPE, **kwargs),  # type: ignore[arg-type]
        correlation_id=_CORRELATION_ID,
    )


def test_exact_duplicate_is_evidence_not_an_automatic_mutation(tmp_path: Path) -> None:
    _seed(tmp_path)
    identity = _ingest_facts(
        tmp_path, bodies=("quartz calibration uses reference Q7",)
    )[0]
    before = {
        table: _rows(tmp_path, f"SELECT * FROM {table}")
        for table in ("facts", "fact_invalidations", "projection_outbox")
    }
    result = _suggest(tmp_path, observation="quartz calibration uses reference Q7")
    assert not isinstance(result, Rejected)
    assert len(result.items) == 1
    suggestion = result.items[0]
    assert suggestion.kind == "exact_duplicate"
    assert suggestion.match_basis == "exact_body"
    assert [fact.fact.fact_id for fact in suggestion.facts] == [identity]
    assert result.semantic_degraded is True  # No semantic index in this fixture.
    for table, rows in before.items():
        assert _rows(tmp_path, f"SELECT * FROM {table}") == rows


def test_similar_but_distinct_claim_remains_an_unverified_candidate(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("quartz calibration uses reference Q8",))
    result = _suggest(tmp_path, observation="quartz calibration uses reference Q7")
    assert not isinstance(result, Rejected)
    assert [item.kind for item in result.items] == ["possible_duplicate"]
    assert result.items[0].match_basis == "retrieval_candidate"


def test_irrelevant_memory_is_not_a_duplicate_suggestion(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("gardening tomatoes",))
    result = _suggest(tmp_path, observation="quartz calibration")
    assert not isinstance(result, Rejected)
    assert result.items == ()


def test_selected_fact_never_suggests_itself_as_a_duplicate(tmp_path: Path) -> None:
    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("quartz calibration",))[0]
    result = _suggest(tmp_path, fact_ids=(identity,))
    assert not isinstance(result, Rejected)
    assert result.items == ()


def test_selected_correction_requires_real_history_and_keeps_both_facts(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    old, new = _ingest_facts(tmp_path, bodies=("quartz Q7", "quartz Q8"))
    outcome = _authority(tmp_path).invalidate(
        _agent_actor(),
        InvalidateFacts((old,), "reference changed", new),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    result = _suggest(tmp_path, fact_ids=(old,))
    assert not isinstance(result, Rejected)
    corrections = [item for item in result.items if item.kind == "possible_correction"]
    assert len(corrections) == 1
    assert {item.fact.fact_id for item in corrections[0].facts} == {old, new}
    assert corrections[0].corrections[0].reason == "reference changed"
    assert not any(item.kind == "exact_duplicate" for item in result.items)


def test_tiny_result_budget_reports_omission_without_truncating_fact(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("quartz calibration",))
    result = _suggest(tmp_path, observation="quartz calibration", budget=1)
    assert not isinstance(result, Rejected)
    assert result.items == ()
    assert result.budget_consumed == 0
    assert result.budget_exhausted is True


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"observation": ""},
        {"observation": "x" * 4097},
        {"observation": "quartz", "fact_ids": (uuid4(),)},
        {"fact_ids": tuple(uuid4() for _ in range(9))},
        {"observation": "quartz", "limit": 0},
        {"observation": "quartz", "budget": 65537},
    ],
)
def test_invalid_input_is_a_typed_audited_refusal(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    _seed(tmp_path)
    before = len(_rows(tmp_path, "SELECT event_id FROM audit_events"))
    result = _suggest(tmp_path, **arguments)
    assert isinstance(result, Rejected)
    assert len(_rows(tmp_path, "SELECT event_id FROM audit_events")) == before + 1


def test_private_candidate_cap_precedes_fact_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.authority.memory import CairnMemory, Recall, RecallResult

    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=tuple(f"quartz reference {n}" for n in range(20)))
    original = CairnMemory._fact
    expanded = []

    def counting(self, fetch, fact, *args, **kwargs):  # type: ignore[no-untyped-def]
        expanded.append(fact.fact_id)
        return original(self, fetch, fact, *args, **kwargs)

    monkeypatch.setattr(CairnMemory, "_fact", counting)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        Recall(_SCOPE, "quartz", relevant_only=True),
        correlation_id=_CORRELATION_ID,
        _candidate_limit=3,
        _include_relationships=False,
    )
    assert isinstance(result, RecallResult)
    assert len(expanded) == 3
    assert len(result.hits) == 3
    assert result.budget_exhausted is True


@pytest.mark.parametrize("lower_clearance", [False, True])
def test_final_disclosure_rechecks_revocation_and_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lower_clearance: bool
) -> None:
    from cairn.authority.memory import CairnMemory

    _seed(tmp_path)
    _ingest_facts(
        tmp_path,
        bodies=("quartz calibration",),
        classification=Classification.RESTRICTED,
    )
    original = CairnMemory.recall

    def changed(self: CairnMemory, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        with _open_write_connection(tmp_path, create=False) as connection:
            connection.execute(
                "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
                (str(h._DATA_GRANT_ID), h._TS, str(h._AGENT_ID), "test"),
            )
            connection.commit()
        if lower_clearance:
            h._insert_grant(
                tmp_path,
                grant_id=uuid4(),
                segments=(),
                read_clearance=Classification.INTERNAL,
            )
        return result

    monkeypatch.setattr(CairnMemory, "recall", changed)
    result = _suggest(tmp_path, observation="quartz calibration")
    assert isinstance(result, Rejected)


def test_current_duplicate_cannot_survive_invalidation_during_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.authority.memory import CairnMemory

    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("quartz calibration",))[0]
    original = CairnMemory.recall

    def changed(self: CairnMemory, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        outcome = _authority(tmp_path).invalidate(
            _agent_actor(),
            InvalidateFacts((identity,), "new measurement", None),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(outcome, Committed)
        return result

    monkeypatch.setattr(CairnMemory, "recall", changed)
    assert isinstance(_suggest(tmp_path, observation="quartz calibration"), Rejected)


def test_hidden_selected_root_is_checked_even_when_output_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    visible = _ingest_facts(tmp_path, bodies=("quartz calibration",))[0]
    hidden = _ingest_facts(
        tmp_path,
        bodies=("hidden quartz",),
        scope=Scope(_SCOPE.realm, (h._SIBLING_JOB,)),
    )[0]
    assert isinstance(
        _suggest(tmp_path, fact_ids=(visible, hidden), budget=1), Rejected
    )


def test_hidden_siblings_do_not_change_suggestion_metadata(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("quartz calibration",))
    before = _suggest(tmp_path, observation="quartz calibration")
    _ingest_facts(
        tmp_path,
        bodies=("quartz calibration",),
        scope=Scope(_SCOPE.realm, (h._SIBLING_JOB,)),
    )
    after = _suggest(tmp_path, observation="quartz calibration")
    assert before == after


def test_recorded_disagreement_is_read_only_evidence(tmp_path: Path) -> None:
    from cairn.authority.memory import Disagree

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("quartz Q7", "quartz Q8"))
    outcome = _memory(tmp_path).disagree(
        _agent_actor(),
        Disagree(
            _SCOPE, left, right, Classification.INTERNAL, "different measurements"
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    result = _suggest(tmp_path, fact_ids=(left,))
    assert not isinstance(result, Rejected)
    links = [item for item in result.items if item.kind == "related_disagreement"]
    assert len(links) == 1
    assert links[0].disagreements[0].relationship_id == outcome.value.relationship_id
    observation = _suggest(tmp_path, observation="quartz")
    assert not isinstance(observation, Rejected)
    observed_links = [
        item for item in observation.items if item.kind == "related_disagreement"
    ]
    assert len(observed_links) == 1
    assert (
        observed_links[0].disagreements[0].relationship_id
        == outcome.value.relationship_id
    )
    assert {f.fact.fact_id for f in observed_links[0].facts} == {left, right}


def test_old_current_fact_is_still_an_exact_duplicate(tmp_path: Path) -> None:
    from cairn.authority.housekeeping_types import Suggest

    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("quartz calibration",))[0]
    result = _memory(tmp_path, now=h._NOW + timedelta(days=100)).suggest(
        _agent_actor(),
        Suggest(_SCOPE, observation="quartz calibration"),
        correlation_id=_CORRELATION_ID,
    )
    assert not isinstance(result, Rejected)
    assert result.items[0].kind == "exact_duplicate"
    assert result.items[0].facts[0].fact.fact_id == identity


def test_controlled_index_routes_candidates_without_proving_equivalence(
    tmp_path: Path,
) -> None:
    from cairn.authority.housekeeping_types import Suggest

    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("automobile engine",))[0]
    service = _memory(tmp_path)
    service._index = h._ScriptedIndex((identity,))
    result = service.suggest(
        _agent_actor(),
        Suggest(_SCOPE, observation="car"),
        correlation_id=_CORRELATION_ID,
    )
    assert not isinstance(result, Rejected)
    assert result.semantic_degraded is False
    assert result.items[0].kind == "possible_duplicate"
    assert result.items[0].match_basis == "retrieval_candidate"


def test_each_selected_duplicate_comparison_keeps_both_attributed_endpoints(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    identities = _ingest_facts(tmp_path, bodies=("quartz calibration",) * 3)
    first, second, other = identities
    result = _suggest(tmp_path, fact_ids=(first, second), limit=16)
    assert not isinstance(result, Rejected)
    pairs = {
        tuple(f.fact.fact_id for f in item.facts)
        for item in result.items
        if item.kind == "exact_duplicate"
    }
    assert (first, other) in pairs and (second, other) in pairs
    assert all(len(pair) == 2 and pair[0] != pair[1] for pair in pairs)


def test_historical_root_is_retained_without_claiming_a_current_duplicate(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    old, current = _ingest_facts(tmp_path, bodies=("quartz calibration",) * 2)
    assert isinstance(
        _authority(tmp_path).invalidate(
            _agent_actor(),
            InvalidateFacts((old,), "older claim retired", None),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        ),
        Committed,
    )
    result = _suggest(tmp_path, fact_ids=(old,))
    assert not isinstance(result, Rejected)
    assert len(result.items) == 1
    comparison = result.items[0]
    assert comparison.kind == "possible_duplicate"
    assert [fact.fact.fact_id for fact in comparison.facts] == [old, current]
    assert comparison.facts[0].fact.invalidated_at is not None


def test_comparison_budget_admits_both_endpoints_or_neither(tmp_path: Path) -> None:
    _seed(tmp_path)
    first, _ = _ingest_facts(tmp_path, bodies=("quartz calibration",) * 2)
    complete = _suggest(tmp_path, fact_ids=(first,))
    assert not isinstance(complete, Rejected) and len(complete.items) == 1
    limited = _suggest(tmp_path, fact_ids=(first,), budget=complete.budget_consumed - 1)
    assert not isinstance(limited, Rejected)
    assert limited.items == ()
    assert limited.budget_consumed == 0 and limited.budget_exhausted
