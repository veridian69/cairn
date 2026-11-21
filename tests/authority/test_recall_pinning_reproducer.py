"""Issue #43 reproducer: what existing identities already pin after correction.

Evidence for ``docs/evidence/2026-11-08-recall-pinning-investigation.md``.
Provider-free and in-process against a disposable catalogue. These tests pin
the properties that document's no-change decision relies on: a fact identity
names immutable content, that content stays recoverable after correction or
withdrawal, and a read's audit event names the disclosed identities without
any packet digest. They also pin why a server-recomputed recall packet is not
a stable object: the same request at a later instant yields different bytes
with no catalogue change. If ranking or audit semantics change deliberately,
update the evidence document with these tests.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_memory import _memory, _recall, _seed
from test_retrieval import (
    _CORRELATION_ID,
    _NOW,
    _SCOPE,
    _agent_actor,
    _authority,
    _command,
    _ingest_facts,
    _last_realm_event,
    _mark_projection_delivered,
    _retrieve,
    _ScriptedIndex,
)

from cairn.authority import memory
from cairn.authority.memory import _json
from cairn.authority.memory_codec import memory_value
from cairn.authority.mutations import InvalidateFacts
from cairn.authority.retrieval import RetrievalResult, RetrievedFact
from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.catalogue.transactions import Committed

_T_RECALL = _NOW + timedelta(hours=1)
_T_CORRECT = _NOW + timedelta(hours=2)
_T_LATER = _NOW + timedelta(hours=3)

# The I-67 stored fields a fact identity fixes for ever (migration 0003's
# trg_facts_no_update). invalidated_at is lifecycle state, not content.
_IMMUTABLE = (
    "fact_id",
    "body",
    "scope",
    "classification",
    "trust",
    "provenance",
    "valid_from",
    "valid_to",
    "recorded_at",
)


def _content(fact: memory.MemoryFactRecord | RetrievedFact) -> dict[str, object]:
    return {name: getattr(fact, name) for name in _IMMUTABLE}


def _content_bytes(fact: memory.MemoryFactRecord | RetrievedFact) -> bytes:
    """A caller-side canonical form of the immutable fields, as a consumer
    holding an authorised response could compute it without any new field."""
    return _json({name: memory_value(value) for name, value in _content(fact).items()})


def _correct(path: Path, old: UUID, new: UUID, *, now: datetime) -> None:
    outcome = _authority(path, now=now).invalidate(
        _agent_actor(),
        InvalidateFacts((old,), "window moved after review", new),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    _mark_projection_delivered(path)


def test_fact_identity_pins_immutable_content_after_correction(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (old,) = _ingest_facts(tmp_path, bodies=("deploy window is Tuesday 14:00",))

    # The external decision consumes this packet and keeps it.
    consumed = _recall(tmp_path, "deploy window", now=_T_RECALL)
    assert [hit.fact.fact_id for hit in consumed.hits] == [old]
    held = consumed.hits[0].fact
    held_bytes = _content_bytes(held)
    assert held.invalidated_at is None

    (new,) = _ingest_facts(
        tmp_path, bodies=("deploy window is Wednesday 09:00",), now=_T_CORRECT
    )
    _correct(tmp_path, old, new, now=_T_CORRECT)

    # The current lifecycle-dependent view has moved on.
    current = _recall(tmp_path, "deploy window", now=_T_LATER)
    assert [hit.fact.fact_id for hit in current.hits] == [new]

    # History by the replacement's identity recovers the consumed version
    # byte-for-byte in its immutable fields; only lifecycle state differs.
    history = _memory(tmp_path, now=_T_LATER).history(
        _agent_actor(), memory.History(_SCOPE, new), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    recovered = {f.fact.fact_id: f.fact for f in history.facts}[old]
    assert _content_bytes(recovered) == held_bytes
    assert recovered.invalidated_at == _T_CORRECT
    assert [(c.fact_id, c.superseded_by) for c in history.corrections] == [(old, new)]

    # Point-in-time retrieval at the consumption instant reproduces the
    # catalogue's answer for these candidates, independent of the later
    # correction. The candidate set is the index's, not the catalogue's.
    index = _ScriptedIndex((old, new))
    replay = _retrieve(
        tmp_path,
        _command(query="deploy window", as_of=_T_RECALL),
        index=index,
        now=_T_LATER,
    )
    assert isinstance(replay, RetrievalResult)
    assert [hit.fact_id for hit in replay.hits] == [old]
    assert _content_bytes(replay.hits[0]) == held_bytes
    assert replay.hits[0].invalidated_at is None


def test_withdrawal_without_replacement_keeps_the_version_recoverable(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    (fact,) = _ingest_facts(tmp_path, bodies=("rollback uses snapshot 7",))
    held = _recall(tmp_path, "rollback snapshot", now=_T_RECALL).hits[0].fact
    outcome = _authority(tmp_path, now=_T_CORRECT).invalidate(
        _agent_actor(),
        InvalidateFacts((fact,), "snapshot retired", None),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)

    assert _recall(tmp_path, "rollback snapshot", now=_T_LATER).hits == ()
    history = _memory(tmp_path, now=_T_LATER).history(
        _agent_actor(), memory.History(_SCOPE, fact), correlation_id=_CORRELATION_ID
    )
    assert isinstance(history, memory.MemoryHistory)
    assert _content_bytes(history.facts[0].fact) == _content_bytes(held)
    assert [(c.fact_id, c.superseded_by) for c in history.corrections] == [(fact, None)]


def test_catalogue_refuses_to_rewrite_a_consumed_fact(tmp_path: Path) -> None:
    _seed(tmp_path)
    (fact,) = _ingest_facts(tmp_path, bodies=("deploy window is Tuesday 14:00",))
    connection = sqlite3.connect(tmp_path / CATALOGUE_FILENAME)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable_fact"):
            connection.execute(
                "UPDATE facts SET body = ? WHERE fact_id = ?",
                ("deploy window is Friday", str(fact)),
            )
    finally:
        connection.close()


def test_read_audit_names_disclosed_identities_but_no_packet(tmp_path: Path) -> None:
    _seed(tmp_path)
    first, second = _ingest_facts(
        tmp_path, bodies=("deploy window is Tuesday", "deploy owner is Ana")
    )

    recalled = _recall(tmp_path, "deploy", now=_T_RECALL)
    event = _last_realm_event(tmp_path).draft
    assert event.action_code == "memory-recall"
    assert event.correlation_id == _CORRELATION_ID
    assert event.affected_fact_ids == tuple(
        sorted((h.fact.fact_id for h in recalled.hits), key=str)
    )
    assert set(event.affected_fact_ids) == {first, second}
    # No request fingerprint, no content digest and no relationship identities:
    # the event cannot say which budget, query, order or context was served.
    assert event.safe_request_fingerprint is None
    assert event.command_digest is None
    assert event.evidence_digest is None

    # /v1 retrieve fingerprints budget, trust filters and an explicit as_of,
    # never the query; a defaulted as_of is recorded as null.
    for as_of in (None, _T_RECALL):
        result = _retrieve(
            tmp_path,
            _command(query="deploy", budget=4096, as_of=as_of),
            index=_ScriptedIndex((first, second)),
            now=_T_LATER,
        )
        assert isinstance(result, RetrievalResult)
        expected = (
            b"cairn.retrieve.request/v1\x00"
            + b'{"as_of":'
            + (b"null" if as_of is None else b'"2026-08-05T11:11:12.123456Z"')
            + b',"budget":4096,"trust_filters":[]}'
        )
        retrieve_event = _last_realm_event(tmp_path).draft
        assert retrieve_event.safe_request_fingerprint == (
            hashlib.sha256(expected).digest()
        )
        assert retrieve_event.affected_fact_ids == tuple(
            sorted((first, second), key=str)
        )


def test_recall_packet_bytes_change_with_time_alone(tmp_path: Path) -> None:
    _seed(tmp_path)
    (fact,) = _ingest_facts(tmp_path, bodies=("deploy window is Tuesday",))

    early = _recall(tmp_path, "deploy window", now=_T_RECALL)
    late = _recall(tmp_path, "deploy window", now=_T_RECALL + timedelta(days=30))

    assert [h.fact.fact_id for h in early.hits] == [fact]
    assert [h.fact.fact_id for h in late.hits] == [fact]
    assert early.policy == late.policy
    assert _content_bytes(early.hits[0].fact) == _content_bytes(late.hits[0].fact)
    # The legacy recency term moves the disclosed score, so the canonical
    # packet differs although nothing in the catalogue changed.
    assert early.hits[0].relevance_score != late.hits[0].relevance_score
    assert _json(memory_value(early)) != _json(memory_value(late))
