"""Real catalogue acceptance of the separately versioned shared memory authority."""

import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_retrieval import (
    _AGENT_ID,
    _CORRELATION_ID,
    _DATA_GRANT_ID,
    _NOW,
    _SCOPE,
    _SIBLING_JOB,
    _agent_actor,
    _authority,
    _ingest_facts,
    _ingest_with_evidence,
    _rows,
    _seed_agent,
    _seed_catalogue,
)

from cairn.authority import memory
from cairn.authority.custody import SourceType
from cairn.authority.mutations import InvalidateFacts
from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.catalogue.sqlite import CURRENT_SCHEMA_VERSION, _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    MutationOutcome,
    Rejected,
    Replayed,
)
from cairn.screening import SecretScreen


def _memory(path: Path, *, now: datetime = _NOW) -> memory.CairnMemory:
    return memory.CairnMemory(
        path,
        CatalogueTransactions(
            path, writer_gate=threading.Lock(), clock=lambda: now, uuid_factory=uuid4
        ),
        lambda: now,
        uuid4,
        SecretScreen(),
    )


def _seed(path: Path) -> None:
    _seed_catalogue(path)
    _seed_agent(path, segments=())


def _recall(
    path: Path,
    query: str = "memory",
    *,
    now: datetime = _NOW,
    scope: Scope = _SCOPE,
    budget: int = 16384,
) -> memory.RecallResult:
    result = _memory(path, now=now).recall(
        _agent_actor(),
        memory.Recall(scope, query, budget),
        correlation_id=_CORRELATION_ID,
    )

    assert isinstance(result, memory.RecallResult)
    return result


def test_candidates_are_attributed_and_strong_cues_resurface_old_memory(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    old = _ingest_facts(
        tmp_path,
        bodies=("quartz calibration failed approach",),
        requested_trust=TrustClass.CANDIDATE,
    )[0]
    now = _NOW + timedelta(days=100)
    recent = _ingest_facts(
        tmp_path,
        bodies=("ordinary memory",),
        now=now,
        requested_trust=TrustClass.CANDIDATE,
    )[0]
    result = _recall(tmp_path, "quartz calibration", now=now)
    assert isinstance(result, memory.RecallResult)
    assert [h.fact.fact_id for h in result.hits] == [old, recent]
    assert result.hits[0].source_principal_id == _AGENT_ID
    assert result.hits[0].source_type == SourceType.AGENT_CLAIM
    assert result.hits[0].fact.trust == TrustClass.CANDIDATE
    assert _recall(tmp_path, "memory", now=now).hits[0].fact.fact_id == recent


def test_correction_history_preserves_reason_and_current_recall_excludes_old(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    old, new = _ingest_facts(tmp_path, bodies=("old memory", "corrected memory"))
    changed = _authority(tmp_path).invalidate(
        _agent_actor(),
        InvalidateFacts((old,), "measurement corrected", new),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(changed, Committed)
    assert [h.fact.fact_id for h in _recall(tmp_path).hits] == [new]
    history = _memory(tmp_path).history(
        _agent_actor(), memory.History(_SCOPE, new), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert {f.fact.fact_id for f in history.facts} == {old, new}
    assert history.corrections[0].reason == "measurement corrected"
    assert history.corrections[0].principal_id == _AGENT_ID


def test_disagreement_resolution_and_replay_preserve_owners_and_facts(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(
        tmp_path, bodies=("left memory", "right memory")
    )
    command = memory.Disagree(
        _SCOPE, left, right, Classification.INTERNAL, "measurements differ"
    )
    service = _memory(tmp_path)
    key = uuid4()
    outcome = service.disagree(
        _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
    )
    assert isinstance(outcome, Committed)
    assert isinstance(
        service.disagree(
            _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
        ),
        Replayed,
    )
    recalled = _recall(tmp_path)
    assert recalled.disagreements[0].principal_id == _AGENT_ID
    resolution = service.resolve(
        _agent_actor(),
        memory.Resolve(
            _SCOPE,
            outcome.value.relationship_id,
            evidence,
            left,
            "verified measurement",
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(resolution, Committed)
    assert _recall(tmp_path).resolutions[0].selected_fact_id == left
    assert len(_rows(tmp_path, "SELECT * FROM fact_invalidations")) == 0
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(
            "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
            (
                str(_DATA_GRANT_ID),
                "2026-08-05T10:11:12.123456Z",
                str(_AGENT_ID),
                "test",
            ),
        )
        connection.commit()
    assert isinstance(
        service.disagree(
            _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
        ),
        Rejected,
    )


def test_hidden_sibling_correction_identity_and_reason_are_not_disclosed(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    visible = _ingest_facts(tmp_path, bodies=("visible memory",))[0]
    hidden = _ingest_facts(
        tmp_path, bodies=("hidden memory",), scope=Scope(_SCOPE.realm, (_SIBLING_JOB,))
    )[0]
    result = _authority(tmp_path).invalidate(
        _agent_actor(),
        InvalidateFacts((visible,), "hidden explanation", hidden),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    history = _memory(tmp_path).history(
        _agent_actor(), memory.History(_SCOPE, visible), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert history.corrections == ()
    assert str(hidden) not in repr(history)
    assert "hidden explanation" not in repr(history)


def _disagree(
    path: Path,
    left: UUID,
    right: UUID,
    *,
    classification: Classification = Classification.INTERNAL,
    reason: str = "independent disagreement",
) -> MutationOutcome[memory.RelationshipRecorded]:
    command = memory.Disagree(
        _SCOPE,
        left,
        right,
        classification,
        reason,
    )
    return _memory(path).disagree(
        _agent_actor(), command, idempotency_key=uuid4(), correlation_id=_CORRELATION_ID
    )


def test_history_joins_both_disagreement_endpoints(tmp_path: Path) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    assert isinstance(_disagree(tmp_path, left, right), Committed)
    history = _memory(tmp_path).history(
        _agent_actor(), memory.History(_SCOPE, left), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert {f.fact.fact_id for f in history.facts} == {left, right}
    assert len(history.disagreements) == 1


def test_a_trust_filter_cannot_make_one_disagreement_endpoint_appear_settled(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left = _ingest_facts(tmp_path, bodies=("validated memory",))[0]
    right = _ingest_facts(
        tmp_path, bodies=("candidate memory",), requested_trust=TrustClass.CANDIDATE
    )[0]
    assert isinstance(_disagree(tmp_path, left, right), Committed)
    result = _memory(tmp_path).recall(
        _agent_actor(),
        memory.Recall(
            _SCOPE, "memory", trust_filters=frozenset({TrustClass.VALIDATED})
        ),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, memory.RecallResult)
    assert [hit.fact.fact_id for hit in result.hits] == [left]
    assert result.hits[0].has_disagreement
    assert result.hits[0].disagreement_context_incomplete
    assert result.disagreements == ()


def test_tight_budget_preserves_facts_and_marks_omitted_disagreement_context(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    solo_cost = _recall(tmp_path).budget_consumed
    assert isinstance(_disagree(tmp_path, left, right), Committed)
    result = _recall(tmp_path, budget=solo_cost)
    assert {hit.fact.fact_id for hit in result.hits} == {left, right}
    assert all(
        hit.has_disagreement and hit.disagreement_context_incomplete
        for hit in result.hits
    )
    assert result.disagreements == ()
    assert result.budget_exhausted


def test_cross_scope_disagreement_and_low_classification_are_refused(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    sibling = _ingest_facts(
        tmp_path, bodies=("sibling",), scope=Scope(_SCOPE.realm, (_SIBLING_JOB,))
    )[0]
    assert isinstance(_disagree(tmp_path, left, sibling), Rejected)
    assert isinstance(
        _disagree(tmp_path, left, right, classification=Classification.PUBLIC), Rejected
    )
    assert _rows(tmp_path, "SELECT * FROM memory_disagreements") == []


def test_secret_reasons_and_queries_never_reach_custody_or_audit(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    secret = "-----BEGIN RSA PRIVATE KEY-----"
    assert isinstance(_disagree(tmp_path, left, right, reason=secret), Rejected)
    assert isinstance(
        _memory(tmp_path).recall(
            _agent_actor(),
            memory.Recall(_SCOPE, secret),
            correlation_id=_CORRELATION_ID,
        ),
        Rejected,
    )
    assert _rows(tmp_path, "SELECT * FROM memory_disagreements") == []
    assert secret not in repr(_rows(tmp_path, "SELECT * FROM audit_events"))


def test_recall_does_not_disclose_future_invalidation(tmp_path: Path) -> None:
    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("memory",))[0]
    result = _authority(tmp_path, now=_NOW + timedelta(days=1)).invalidate(
        _agent_actor(),
        InvalidateFacts((identity,), "future correction", None),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    assert _recall(tmp_path).hits[0].fact.invalidated_at is None


def test_history_byte_budget_bounds_reasons_as_well_as_bodies(tmp_path: Path) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    assert isinstance(_disagree(tmp_path, left, right, reason="x" * 4096), Committed)
    result = _memory(tmp_path).history(
        _agent_actor(),
        memory.History(_SCOPE, left, budget=1500),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, memory.MemoryHistory)
    assert result.budget_consumed <= 1500
    assert result.budget_exhausted
    assert result.disagreements == ()


def test_semantic_membership_surfaces_synonyms_without_using_uuid_order(
    tmp_path: Path,
) -> None:
    from test_retrieval import _ScriptedIndex

    _seed(tmp_path)
    old = _ingest_facts(tmp_path, bodies=("automobile engine",))[0]
    now = _NOW + timedelta(days=100)
    new = _ingest_facts(tmp_path, bodies=("garden flowers",), now=now)[0]
    service = _memory(tmp_path, now=now)
    service._index = _ScriptedIndex((old,))
    result = service.recall(
        _agent_actor(), memory.Recall(_SCOPE, "car"), correlation_id=_CORRELATION_ID
    )
    assert isinstance(result, memory.RecallResult)
    assert [f.fact.fact_id for f in result.hits] == [old, new]
    assert not result.semantic_degraded


def test_semantic_failure_is_explicit_with_authoritative_fallback(
    tmp_path: Path,
) -> None:
    from test_retrieval import _ScriptedIndex

    class BrokenIndex(_ScriptedIndex):
        def search(
            self, query: str, limit: int, partition_keys: tuple[str, ...]
        ) -> tuple[UUID, ...]:
            raise RuntimeError("provider failed")

    _seed(tmp_path)
    identity = _ingest_facts(tmp_path, bodies=("memory",))[0]
    service = _memory(tmp_path)
    service._index = BrokenIndex()
    result = service.recall(
        _agent_actor(), memory.Recall(_SCOPE, "memory"), correlation_id=_CORRELATION_ID
    )
    assert isinstance(result, memory.RecallResult)
    assert result.semantic_degraded
    assert result.hits[0].fact.fact_id == identity


def test_resolution_requires_visible_evidence_and_promotion_authority(
    tmp_path: Path,
) -> None:
    from test_retrieval import (
        _OUTSIDER_ID,
        _insert_grant,
        _outsider_actor,
        _seed_outsider,
    )

    from cairn.authority.credentials import GrantOperation

    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(tmp_path, bodies=("left", "right"))
    outcome = _disagree(tmp_path, left, right)
    assert isinstance(outcome, Committed)
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=uuid4(),
        principal_id=_OUTSIDER_ID,
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.INGEST}),
    )
    command = memory.Resolve(
        _SCOPE, outcome.value.relationship_id, evidence, None, "no winner"
    )
    assert isinstance(
        _memory(tmp_path).resolve(
            _outsider_actor(),
            command,
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        ),
        Rejected,
    )
    bad = memory.Resolve(
        _SCOPE, outcome.value.relationship_id, uuid4(), None, "no winner"
    )
    assert isinstance(
        _memory(tmp_path).resolve(
            _agent_actor(), bad, idempotency_key=uuid4(), correlation_id=_CORRELATION_ID
        ),
        Rejected,
    )


def test_competing_resolutions_remain_attributed_without_mutating_truth(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(tmp_path, bodies=("left", "right"))
    outcome = _disagree(tmp_path, left, right)
    assert isinstance(outcome, Committed)
    for selected in (left, right, None):
        result = _memory(tmp_path).resolve(
            _agent_actor(),
            memory.Resolve(
                _SCOPE,
                outcome.value.relationship_id,
                evidence,
                selected,
                "independent finding",
            ),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(result, Committed)
    recalled = _recall(tmp_path)
    assert {r.selected_fact_id for r in recalled.resolutions} == {left, right, None}
    assert len(recalled.hits) == 2


def test_memory_relationships_pass_offline_verification(tmp_path: Path) -> None:
    from test_retrieval import _config

    from cairn.catalogue.verification import verify_catalogue

    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(tmp_path, bodies=("left", "right"))
    outcome = _disagree(tmp_path, left, right)
    assert isinstance(outcome, Committed)
    assert isinstance(
        _memory(tmp_path).resolve(
            _agent_actor(),
            memory.Resolve(
                _SCOPE, outcome.value.relationship_id, evidence, left, "verified"
            ),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        ),
        Committed,
    )
    assert verify_catalogue(_config(tmp_path)).schema_version == CURRENT_SCHEMA_VERSION


def test_replay_is_reauthorised_inside_transaction_after_concurrent_revocation(
    tmp_path: Path,
) -> None:
    from test_mutations import _GrantRaceTransactions

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    service = _memory(tmp_path)
    command = memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different")
    key = uuid4()
    assert isinstance(
        service.disagree(
            _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
        ),
        Committed,
    )

    def revoke_at_gate(data_path: Path) -> None:
        with _open_write_connection(data_path, create=False) as connection:
            connection.execute(
                "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
                (
                    str(_DATA_GRANT_ID),
                    "2026-08-05T10:11:12.123456Z",
                    str(_AGENT_ID),
                    "test",
                ),
            )
            connection.commit()

    service._transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: _NOW,
        uuid_factory=uuid4,
        interfere=revoke_at_gate,
    )
    outcome = service.disagree(
        _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
    )
    assert isinstance(outcome, Rejected)
    assert len(_rows(tmp_path, "SELECT * FROM memory_disagreements")) == 1


def test_offline_verifier_rejects_memory_classification_downgrade(
    tmp_path: Path,
) -> None:
    import pytest
    from test_retrieval import _config

    from cairn.catalogue.verification import VerificationError, verify_catalogue

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    assert isinstance(_disagree(tmp_path, left, right), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute(
            "INSERT INTO memory_disagreements SELECT ?, realm_id, scope_segments, left_fact_id, right_fact_id, 'public', principal_id, reason, recorded_at, mutation_id FROM memory_disagreements",
            (str(uuid4()),),
        )
        connection.commit()
    with pytest.raises(VerificationError, match="memory_reference_invalid"):
        verify_catalogue(_config(tmp_path))


def test_promoted_fact_remains_readable_without_hidden_source_provenance(
    tmp_path: Path,
) -> None:
    from cairn.authority.mutations import PromoteFacts

    _seed(tmp_path)
    (source,), evidence = _ingest_with_evidence(tmp_path, bodies=("promotable memory",))
    result = _authority(tmp_path).promote(
        _agent_actor(),
        PromoteFacts(
            (source,),
            evidence,
            Scope(_SCOPE.realm, ()),
            Classification.INTERNAL,
            "broader published finding",
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    recalled = _recall(tmp_path, scope=Scope(_SCOPE.realm, ()))
    assert len(recalled.hits) == 1
    assert recalled.hits[0].fact.provenance is None
    assert recalled.hits[0].source_principal_id is None
    assert recalled.hits[0].source_type is None
    assert str(source) not in repr(recalled)
    assert str(evidence) not in repr(recalled)


def test_history_does_not_follow_a_future_correction_link(tmp_path: Path) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("old", "future replacement"))
    result = _authority(tmp_path, now=_NOW + timedelta(days=1)).invalidate(
        _agent_actor(),
        InvalidateFacts((left,), "future reason", right),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    history = _memory(tmp_path).history(
        _agent_actor(), memory.History(_SCOPE, left), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert [f.fact.fact_id for f in history.facts] == [left]
    assert history.corrections == ()


def test_relationship_rows_are_immutable_and_key_conflicts_write_nothing(
    tmp_path: Path,
) -> None:
    import sqlite3

    import pytest

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    service = _memory(tmp_path)
    key = uuid4()
    command = memory.Disagree(
        _SCOPE, left, right, Classification.INTERNAL, "original reason"
    )
    result = service.disagree(
        _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
    )
    assert isinstance(result, Committed)
    changed = memory.Disagree(
        _SCOPE, left, right, Classification.INTERNAL, "different reason"
    )
    conflict = service.disagree(
        _agent_actor(), changed, idempotency_key=key, correlation_id=_CORRELATION_ID
    )
    assert isinstance(conflict, Rejected)
    assert conflict.failure.code.value == "idempotency_conflict"
    for sql in (
        "DELETE FROM memory_disagreements",
        "UPDATE memory_disagreements SET reason = 'changed'",
    ):
        with _open_write_connection(tmp_path, create=False) as connection:
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable_memory_relationship"
            ):
                connection.execute(sql)
    assert len(_rows(tmp_path, "SELECT * FROM memory_disagreements")) == 1


def test_history_has_a_hard_record_bound_even_with_repeated_resolutions(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(tmp_path, bodies=("left", "right"))
    outcome = _disagree(tmp_path, left, right)
    assert isinstance(outcome, Committed)
    for _ in range(130):
        assert isinstance(
            _memory(tmp_path).resolve(
                _agent_actor(),
                memory.Resolve(
                    _SCOPE, outcome.value.relationship_id, evidence, None, "unresolved"
                ),
                idempotency_key=uuid4(),
                correlation_id=_CORRELATION_ID,
            ),
            Committed,
        )
    history = _memory(tmp_path).history(
        _agent_actor(),
        memory.History(_SCOPE, left, budget=1048576),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(history, memory.MemoryHistory)
    assert (
        sum(
            map(
                len,
                (
                    history.facts,
                    history.corrections,
                    history.disagreements,
                    history.resolutions,
                ),
            )
        )
        <= 128
    )
    assert history.budget_exhausted


def test_semantic_order_is_ignored_and_recall_does_not_reinforce_usage(
    tmp_path: Path,
) -> None:
    from test_retrieval import _ScriptedIndex

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("matching memory", "matching memory"))
    service = _memory(tmp_path)
    service._index = _ScriptedIndex((right, left))
    command = memory.Recall(_SCOPE, "memory")
    first = service.recall(_agent_actor(), command, correlation_id=_CORRELATION_ID)
    assert isinstance(first, memory.RecallResult)
    assert [hit.fact.fact_id for hit in first.hits] == sorted((left, right), key=str)
    assert service._index.calls[0][1] == 256
    service._index = _ScriptedIndex((left, right))
    second = service.recall(_agent_actor(), command, correlation_id=_CORRELATION_ID)
    assert isinstance(second, memory.RecallResult)
    assert second == first


def test_offline_verification_accepts_audit_clock_advancing_after_request_start(
    tmp_path: Path,
) -> None:
    from test_retrieval import _config

    from cairn.catalogue.verification import verify_catalogue

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    service = _memory(tmp_path)
    service._transactions._clock = lambda: _NOW + timedelta(seconds=1)
    result = service.disagree(
        _agent_actor(),
        memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different"),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    assert verify_catalogue(_config(tmp_path)).schema_version == CURRENT_SCHEMA_VERSION


def test_hidden_correction_fan_in_does_not_change_disclosed_history_or_exhaustion(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    visible = _ingest_facts(tmp_path, bodies=("visible memory",))[0]
    for size in (100, 30):
        hidden = _ingest_facts(
            tmp_path,
            bodies=tuple("hidden memory" for _ in range(size)),
            scope=Scope(_SCOPE.realm, (_SIBLING_JOB,)),
        )
        result = _authority(tmp_path).invalidate(
            _agent_actor(),
            InvalidateFacts(hidden, "hidden explanation", visible),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        )
        assert isinstance(result, Committed)
    history = _memory(tmp_path).history(
        _agent_actor(), memory.History(_SCOPE, visible), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert [f.fact.fact_id for f in history.facts] == [visible]
    assert history.corrections == ()
    assert not history.budget_exhausted


def test_duplicate_large_disagreements_cannot_remove_readable_facts(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    for _ in range(4):
        assert isinstance(
            _disagree(tmp_path, left, right, reason="x" * 4096), Committed
        )
    result = _recall(tmp_path)
    assert {hit.fact.fact_id for hit in result.hits} == {left, right}
    assert all(
        hit.has_disagreement and hit.disagreement_context_incomplete
        for hit in result.hits
    )
    assert result.budget_exhausted
    assert result.budget_consumed <= 16384


def test_long_disagreement_chain_preserves_ranked_facts_with_explicit_context(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    identities = _ingest_facts(
        tmp_path, bodies=tuple(f"memory {index}" for index in range(20))
    )
    for left, right in zip(identities, identities[1:], strict=False):
        assert isinstance(_disagree(tmp_path, left, right), Committed)
    result = _recall(tmp_path, budget=2000)
    assert result.hits
    assert result.hits[0].fact.fact_id == identities[0]
    assert any(hit.disagreement_context_incomplete for hit in result.hits)
    selected = {hit.fact.fact_id for hit in result.hits}
    assert all(
        link.left_fact_id in selected and link.right_fact_id in selected
        for link in result.disagreements
    )
    assert result.budget_consumed <= 2000


def test_relationship_discovery_has_a_hard_cap_and_exact_incomplete_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    for _ in range(130):
        assert isinstance(
            _disagree(tmp_path, left, right, reason="different"), Committed
        )
    import sqlite3

    from cairn.authority.gate import Fetch, fetch_from

    original = fetch_from
    materialised: list[int] = []

    def observe(connection: sqlite3.Connection) -> Fetch:
        fetch = original(connection)

        def measured(
            sql: str, parameters: Sequence[object]
        ) -> tuple[tuple[object, ...], ...]:
            rows = fetch(sql, parameters)
            if "FROM memory_disagreements" in sql and "reason" in sql:
                materialised.append(len(rows))
            return rows

        return measured

    monkeypatch.setattr(memory, "fetch_from", observe)
    result = _recall(tmp_path, budget=1048576)
    assert materialised and max(materialised) <= 129
    assert len(result.hits) == 2
    assert len(result.disagreements) + len(result.resolutions) <= 128
    assert all(
        hit.has_disagreement and hit.disagreement_context_incomplete
        for hit in result.hits
    )
    assert result.budget_exhausted


def test_hidden_disagreement_does_not_set_disclosure_flags(tmp_path: Path) -> None:
    from test_retrieval import (
        _OUTSIDER_ID,
        _insert_grant,
        _outsider_actor,
        _seed_outsider,
    )

    from cairn.authority.credentials import GrantOperation

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    assert isinstance(
        _disagree(tmp_path, left, right, classification=Classification.RESTRICTED),
        Committed,
    )
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=uuid4(),
        principal_id=_OUTSIDER_ID,
        read_clearance=Classification.INTERNAL,
        operations=frozenset({GrantOperation.RETRIEVE}),
    )
    result = _memory(tmp_path).recall(
        _outsider_actor(),
        memory.Recall(_SCOPE, "memory"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, memory.RecallResult)
    assert len(result.hits) == 2
    assert all(
        not hit.has_disagreement and not hit.disagreement_context_incomplete
        for hit in result.hits
    )
    assert not result.budget_exhausted


def test_recorded_disagreement_flag_survives_resolution_when_context_complete(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (left, right), evidence = _ingest_with_evidence(
        tmp_path, bodies=("left memory", "right memory")
    )
    disagreement = _disagree(tmp_path, left, right)
    assert isinstance(disagreement, Committed)
    assert isinstance(
        _memory(tmp_path).resolve(
            _agent_actor(),
            memory.Resolve(
                _SCOPE, disagreement.value.relationship_id, evidence, left, "verified"
            ),
            idempotency_key=uuid4(),
            correlation_id=_CORRELATION_ID,
        ),
        Committed,
    )
    result = _recall(tmp_path)
    assert len(result.disagreements) == len(result.resolutions) == 1
    assert all(
        hit.has_disagreement and not hit.disagreement_context_incomplete
        for hit in result.hits
    )


@pytest.mark.parametrize("replay", [False, True])
def test_expiry_is_checked_at_transaction_time_for_fresh_and_replayed_writes(
    tmp_path: Path,
    replay: bool,
) -> None:
    from test_mutations import _GrantRaceTransactions

    path = tmp_path / str(replay)
    path.mkdir()
    _seed(path)
    left, right = _ingest_facts(path, bodies=("left", "right"))
    command = memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different")
    key = uuid4()
    now = [_NOW]
    service = _memory(path)
    if replay:
        assert isinstance(
            service.disagree(
                _agent_actor(),
                command,
                idempotency_key=key,
                correlation_id=_CORRELATION_ID,
            ),
            Committed,
        )

    def advance(_path: Path) -> None:
        now[0] = _NOW.replace(year=2027)

    service._clock = lambda: now[0]
    service._transactions = _GrantRaceTransactions(
        path,
        writer_gate=threading.Lock(),
        clock=lambda: now[0],
        uuid_factory=uuid4,
        interfere=advance,
    )
    outcome = service.disagree(
        _agent_actor(), command, idempotency_key=key, correlation_id=_CORRELATION_ID
    )
    assert isinstance(outcome, Rejected)
    assert len(_rows(path, "SELECT * FROM memory_disagreements")) == int(replay)


def test_relationship_timestamp_is_the_fresh_transaction_instant(
    tmp_path: Path,
) -> None:
    from test_mutations import _GrantRaceTransactions

    from cairn.catalogue.sqlite import canonical_timestamp

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left", "right"))
    now = [_NOW]
    service = _memory(tmp_path)

    def advance(_path: Path) -> None:
        now[0] = _NOW + timedelta(hours=1)

    service._clock = lambda: now[0]
    service._transactions = _GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: now[0],
        uuid_factory=uuid4,
        interfere=advance,
    )
    result = service.disagree(
        _agent_actor(),
        memory.Disagree(_SCOPE, left, right, Classification.INTERNAL, "different"),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, Committed)
    assert _rows(tmp_path, "SELECT recorded_at FROM memory_disagreements") == [
        (canonical_timestamp(now[0]),)
    ]
    assert result.audit_receipt.recorded_at == now[0]


def test_hidden_resolution_does_not_make_disagreement_context_incomplete(
    tmp_path: Path,
) -> None:
    from test_retrieval import (
        _OUTSIDER_ID,
        _insert_grant,
        _outsider_actor,
        _seed_outsider,
    )

    from cairn.authority.credentials import GrantOperation

    _seed(tmp_path)
    left, right = _ingest_facts(tmp_path, bodies=("left memory", "right memory"))
    _, evidence = _ingest_with_evidence(
        tmp_path,
        bodies=("restricted evidence",),
        classification=Classification.RESTRICTED,
    )
    disagreement = _disagree(tmp_path, left, right)
    assert isinstance(disagreement, Committed)
    resolution = _memory(tmp_path).resolve(
        _agent_actor(),
        memory.Resolve(
            _SCOPE,
            disagreement.value.relationship_id,
            evidence,
            left,
            "restricted conclusion",
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(resolution, Committed)
    _seed_outsider(tmp_path)
    _insert_grant(
        tmp_path,
        grant_id=uuid4(),
        principal_id=_OUTSIDER_ID,
        read_clearance=Classification.INTERNAL,
        operations=frozenset({GrantOperation.RETRIEVE}),
    )
    result = _memory(tmp_path).recall(
        _outsider_actor(),
        memory.Recall(_SCOPE, "memory"),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(result, memory.RecallResult)
    assert len(result.hits) == 2 and len(result.disagreements) == 1
    assert result.resolutions == ()
    assert all(
        hit.has_disagreement and not hit.disagreement_context_incomplete
        for hit in result.hits
    )
    assert not result.budget_exhausted
