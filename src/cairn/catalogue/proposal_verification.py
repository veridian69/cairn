"""Independently reconstruct offline proposal commands, receipts and history.

Only catalogue values and the standard library belong here. In particular the
producer's proposal codec and promotion authority are not verification oracles.
Audit sequence establishes history; current grants and wall-clock ordering do
not. This runs after the general structural, audit and custody checks.
"""

import hashlib
import json
import sqlite3
from typing import Any
from uuid import UUID

from cairn.catalogue.audit import AuditEvent, Scope, ScopeSegment
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)

_PROPOSE = "memory-propose"
_ACCEPT = "memory-proposal-accept"
_REJECT = "memory-proposal-reject"
_OPERATIONS = {_PROPOSE, _ACCEPT, _REJECT}
_LEVELS = ("public", "internal", "restricted")


class ProposalVerificationError(ValueError):
    """Opaque integrity failure, never a recovered value or identity."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProposalVerificationError("proposal_integrity_invalid")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _digest(value: object) -> bytes:
    return hashlib.sha256(_canonical(value)).digest()


def _uuid(value: Any) -> None:
    _require(type(value) is str and str(UUID(value)) == value)


def _timestamp(value: Any) -> None:
    _require(
        type(value) is str
        and len(value) == 27
        and canonical_timestamp(parse_timestamp(value)) == value
    )


def _reason(value: Any) -> None:
    _require(type(value) is str and 1 <= len(value.encode()) <= 4096)


def _scope(realm: Any, raw: Any) -> Scope:
    _require(type(raw) is str)
    segments = json.loads(raw)
    _require(type(segments) is list and _canonical(segments).decode() == raw)
    _require(all(type(s) is dict and s.keys() == {"kind", "id"} for s in segments))
    return Scope(realm, tuple(ScopeSegment(s["kind"], s["id"]) for s in segments))


def _document(scope: Scope) -> dict[str, object]:
    return {
        "realm": scope.realm,
        "segments": [{"kind": s.kind, "id": s.identifier} for s in scope.segments],
    }


def _ancestor(target: Scope, source: Scope) -> bool:
    return (
        target.realm == source.realm
        and source.segments[: len(target.segments)] == target.segments
    )


def _rows(
    connection: sqlite3.Connection, table: str, columns: str = "*"
) -> list[dict[str, Any]]:
    cursor = connection.execute(f"SELECT {columns} FROM {table}")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def _accept_command(
    p: dict[str, Any], evidence_id: str, level: str
) -> dict[str, object]:
    promotion = {
        "command": "promote",
        "schema": "cairn.authority/v1",
        "fact_ids": [p["source_fact_id"]],
        "evidence": {"evidence_id": evidence_id},
        "target_scope": _document(_scope(p["realm_id"], p["target_segments"])),
        "target_classification": level,
        "reason": p["reason"],
    }
    return {
        "command": _ACCEPT,
        "proposal_id": p["proposal_id"],
        "promotion_digest": _digest(promotion).hex(),
    }


def verify_proposals(
    connection: sqlite3.Connection, instance_id: UUID, events: dict[str, AuditEvent]
) -> None:
    """Validate bidirectional inventories and each historical decision's custody."""
    try:
        _History(connection, instance_id, events).verify()
    except ProposalVerificationError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
        RecursionError,
        sqlite3.Error,
        CatalogueStorageError,
    ):
        raise ProposalVerificationError("proposal_integrity_invalid") from None


class _History:
    def __init__(
        self,
        connection: sqlite3.Connection,
        instance_id: UUID,
        events: dict[str, AuditEvent],
    ) -> None:
        self.connection = connection
        self.events = events
        _require(
            connection.execute("SELECT instance_id FROM catalogue_metadata").fetchone()
            == (str(instance_id),)
        )
        self.records = {
            r["mutation_id"]: r for r in _rows(connection, "idempotency_records")
        }
        self.proposals = {
            r["proposal_id"]: r for r in _rows(connection, "memory_proposals")
        }
        self.decisions = {
            r["proposal_id"]: r for r in _rows(connection, "memory_proposal_decisions")
        }
        self.entities = {
            r["mutation_id"]: r
            for r in (*self.proposals.values(), *self.decisions.values())
        }
        # Compare publication bodies inside SQLite; do not load unrelated fact
        # content into an offline diagnostic process.
        self.facts = {
            r["fact_id"]: r
            for r in _rows(
                connection,
                "facts",
                "fact_id, realm_id, scope_segments, classification, trust, assertion_id, derived_from, evidence_id, promoted_by, valid_from, valid_to",
            )
        }
        self.evidence = {
            r["evidence_id"]: r
            for r in _rows(
                connection,
                "evidence_records",
                "evidence_id, realm_id, scope_segments, classification, payload_digest",
            )
        }
        self.fact_creators: dict[str, list[AuditEvent]] = {}
        self.evidence_mentions: dict[str, list[AuditEvent]] = {}
        self.invalidations: dict[str, list[AuditEvent]] = {}
        for event in events.values():
            d = event.draft
            if (
                d.outcome.value != "allow"
                or d.mutation_id is None
                or d.replay_of_mutation_id is not None
            ):
                continue
            if d.action_code in {"ingest", "promote"}:
                for identity in d.affected_fact_ids:
                    self.fact_creators.setdefault(str(identity), []).append(event)
                for identity in d.affected_evidence_ids:
                    self.evidence_mentions.setdefault(str(identity), []).append(event)
            if d.action_code == "invalidate":
                for identity in d.affected_fact_ids:
                    self.invalidations.setdefault(str(identity), []).append(event)

    def verify(self) -> None:
        _require(len(self.entities) == len(self.proposals) + len(self.decisions))
        _require(self.decisions.keys() <= self.proposals.keys())
        ids = {mid for mid, r in self.records.items() if r["operation"] in _OPERATIONS}
        _require(ids == self.entities.keys())
        for p in self.proposals.values():
            self._proposal(p)
        self._publication_inventory()
        # Reverse audit inventory includes normal promotion: acceptance deliberately
        # retains its action code. A missing record cannot become legacy history.
        for event in self.events.values():
            draft = event.draft
            mid = str(draft.mutation_id or draft.replay_of_mutation_id)
            relevant = draft.action_code in _OPERATIONS | {"promote"} or mid in ids
            if draft.outcome.value != "allow" or not relevant:
                continue
            record = self.records[mid]
            _require(record["operation"] in _OPERATIONS | {"promote"})
            expected = (
                "promote" if record["operation"] == _ACCEPT else record["operation"]
            )
            _require(draft.action_code == expected)
            original = self.events[record["original_event_id"]]
            if draft.mutation_id is not None:
                _require(event == original and draft.replay_of_mutation_id is None)
                if record["operation"] == "promote":
                    # Legacy promotion is allowed alongside proposals. But a
                    # surviving proposal-domain digest must not be relabelled as
                    # legacy after its decision is deleted.
                    for p in self.proposals.values():
                        if not any(
                            self.facts.get(str(fid), {}).get("derived_from")
                            == p["source_fact_id"]
                            for fid in draft.affected_fact_ids
                        ):
                            continue
                        if (
                            draft.evidence_reference is not None
                            and draft.classification_transition is not None
                        ):
                            _require(
                                record["command_digest"]
                                != _digest(
                                    _accept_command(
                                        p,
                                        str(draft.evidence_reference),
                                        draft.classification_transition.current.value,
                                    )
                                )
                            )
            else:
                self._replay(event, original, record)

    def _publication_inventory(self) -> None:
        """Every publication of a proposed source needs real, unique custody.

        Legacy promotion remains a separate legitimate publisher. Matching a
        source/evidence pair alone never makes that publication an acceptance.
        """
        sources = {p["source_fact_id"] for p in self.proposals.values()}
        for fact in self.facts.values():
            if fact["derived_from"] not in sources:
                continue
            creators = self.fact_creators.get(fact["fact_id"], [])
            _require(len(creators) == 1)
            event = creators[0]
            _require(event.draft.action_code == "promote")
            record = self.records[str(event.draft.mutation_id)]
            _require(record["operation"] in {"promote", _ACCEPT})
            _require(record["original_event_id"] == str(event.event_id))
            _require(record["result_schema"] == "cairn.authority.promotion/v1")
            result = json.loads(record["result_bytes"])["result"]
            _require([fact["derived_from"], fact["fact_id"]] in result["promotions"])
            _require(result["evidence_id"] == fact["evidence_id"])
            if record["operation"] == _ACCEPT:
                decision = self.entities[record["mutation_id"]]
                _require(decision["promoted_fact_id"] == fact["fact_id"])

    def _binding(
        self,
        row: dict[str, Any],
        command: dict[str, object],
        result: object,
        schema: str,
        scope: Scope,
    ) -> AuditEvent:
        for field in ("proposal_id", "principal_id", "idempotency_key", "mutation_id"):
            _uuid(row[field])
        _timestamp(row["recorded_at"])
        digest = _digest(command)
        record = self.records[row["mutation_id"]]
        _require(
            all(
                record[k] == row[k]
                for k in ("principal_id", "operation", "idempotency_key", "mutation_id")
            )
        )
        _require(row["command_digest"] == record["command_digest"] == digest)
        payload = _canonical(result)
        _require(
            record["result_schema"] == schema and record["result_bytes"] == payload
        )
        _require(record["result_digest"] == hashlib.sha256(payload).digest())
        event = self.events[record["original_event_id"]]
        draft = event.draft
        _require(
            draft.chain_kind.value == "realm"
            and draft.chain_identity == scope.realm
            and draft.action_kind.value == "data"
            and draft.outcome.value == "allow"
            and str(draft.principal_id) == row["principal_id"]
            and str(draft.idempotency_key) == row["idempotency_key"]
            and str(draft.mutation_id) == row["mutation_id"]
            and draft.command_digest == digest
            and draft.replay_of_mutation_id is None
            and record["created_at"] == canonical_timestamp(event.recorded_at)
            and draft.credential_verifier_id is not None
            and draft.grant_id is not None
            and not draft.affected_assertion_ids
            and not draft.affected_grant_ids
            and draft.safe_request_fingerprint is None
        )
        return event

    def _recorded(
        self, row: dict[str, Any], scope: Scope, command: dict[str, object]
    ) -> AuditEvent:
        result = {
            "proposal_id": row["proposal_id"],
            "mutation_id": row["mutation_id"],
            "command_digest": _digest(command).hex(),
        }
        event = self._binding(row, command, result, "cairn.proposal.recorded/v1", scope)
        d = event.draft
        _require(
            d.action_code == row["operation"]
            and d.reason_code == "proposal_recorded"
            and d.requested_scope == scope
            and d.source_scope is None
            and d.target_scope is None
            and d.classification_transition is None
            and d.trust_transition is None
            and d.evidence_reference is None
            and d.evidence_digest is None
            and not d.affected_fact_ids
            and not d.affected_evidence_ids
        )
        # These mutations only record discussion, never custody or invalidation.
        for table in ("projection_outbox", "evidence_outbox"):
            _require(
                not self.connection.execute(
                    f"SELECT 1 FROM {table} WHERE mutation_id=?", (row["mutation_id"],)
                ).fetchone()
            )
        return event

    def _proposal(self, p: dict[str, Any]) -> None:
        _require(p["operation"] == _PROPOSE and p["classification"] in _LEVELS)
        _reason(p["reason"])
        _uuid(p["source_fact_id"])
        scope = _scope(p["realm_id"], p["scope_segments"])
        target = _scope(p["realm_id"], p["target_segments"])
        _require(_ancestor(target, scope))
        source = self.facts[p["source_fact_id"]]
        _require(
            (source["realm_id"], source["scope_segments"], source["classification"])
            == (p["realm_id"], p["scope_segments"], p["classification"])
        )
        command: dict[str, object] = {
            "operation": _PROPOSE,
            "scope": _document(scope),
            "proposal_id": p["proposal_id"],
            "reason": p["reason"],
            "source_fact_id": p["source_fact_id"],
            "target_scope": _document(target),
        }
        original = self._recorded(p, scope, command)
        creators = self.fact_creators.get(p["source_fact_id"], [])
        _require(len(creators) == 1)
        _require(
            creators[0].draft.chain_identity == scope.realm
            and creators[0].sequence < original.sequence
        )
        self._source_invalidation(p["source_fact_id"], scope)
        decision = self.decisions.get(p["proposal_id"])
        if decision is None:
            return
        if decision["state"] == "rejected":
            _require(decision["operation"] == _REJECT)
            _require(
                all(
                    decision[k] is None
                    for k in (
                        "evidence_id",
                        "promoted_fact_id",
                        "target_classification",
                    )
                )
            )
            _reason(decision["reason"])
            event = self._recorded(
                decision,
                scope,
                {
                    "operation": _REJECT,
                    "scope": _document(scope),
                    "proposal_id": p["proposal_id"],
                    "reason": decision["reason"],
                },
            )
        else:
            _require(
                decision["state"] == "accepted"
                and decision["operation"] == _ACCEPT
                and decision["reason"] is None
            )
            event = self._acceptance(p, decision, source, scope, target)
        _require(event.sequence > original.sequence)

    def _source_invalidation(self, fact_id: str, scope: Scope) -> None:
        # Rejection cannot itself invalidate the source. A later (or earlier)
        # independent normal invalidation is legitimate and must have its own
        # receipt, rather than being silently attributed to a proposal mutation.
        row = self.connection.execute(
            "SELECT principal_id, invalidated_at FROM fact_invalidations WHERE fact_id=?",
            (fact_id,),
        ).fetchone()
        events = self.invalidations.get(fact_id, [])
        if row is None:
            _require(not events)
            return
        _require(len(events) == 1)
        event = events[0]
        record = self.records[str(event.draft.mutation_id)]
        _require(
            record["operation"] == "invalidate"
            and record["result_schema"] == "cairn.authority.invalidation/v1"
            and record["original_event_id"] == str(event.event_id)
            and record["principal_id"] == str(event.draft.principal_id) == row[0]
            and event.draft.requested_scope == scope
        )
        result = json.loads(record["result_bytes"])["result"]
        _require(fact_id in result["fact_ids"] and result["invalidated_at"] == row[1])

    def _acceptance(
        self,
        p: dict[str, Any],
        decision: dict[str, Any],
        source: dict[str, Any],
        scope: Scope,
        target: Scope,
    ) -> AuditEvent:
        level = decision["target_classification"]
        _require(
            level in _LEVELS
            and _LEVELS.index(level) >= _LEVELS.index(p["classification"])
        )
        evidence_id, fact_id = decision["evidence_id"], decision["promoted_fact_id"]
        _uuid(evidence_id)
        _uuid(fact_id)
        command = _accept_command(p, evidence_id, level)
        result = {
            "mutation_receipt": {
                "mutation_id": decision["mutation_id"],
                "command_digest": _digest(command).hex(),
            },
            "result": {
                "evidence_id": evidence_id,
                "promotions": [[p["source_fact_id"], fact_id]],
            },
        }
        event = self._binding(
            decision, command, result, "cairn.authority.promotion/v1", scope
        )
        evidence, fact = self.evidence[evidence_id], self.facts[fact_id]
        _require(
            _ancestor(_scope(evidence["realm_id"], evidence["scope_segments"]), scope)
        )
        _require(evidence["classification"] in _LEVELS)
        _require(
            (
                fact["derived_from"],
                fact["evidence_id"],
                fact["promoted_by"],
                fact["realm_id"],
                fact["scope_segments"],
                fact["classification"],
                fact["trust"],
                fact["assertion_id"],
            )
            == (
                p["source_fact_id"],
                evidence_id,
                decision["principal_id"],
                p["realm_id"],
                p["target_segments"],
                level,
                "validated",
                None,
            )
        )
        _require(all(fact[k] == source[k] for k in ("valid_from", "valid_to")))
        _require(
            self.connection.execute(
                "SELECT CAST(p.body AS BLOB)=CAST(s.body AS BLOB) FROM facts p, facts s WHERE p.fact_id=? AND s.fact_id=?",
                (fact_id, p["source_fact_id"]),
            ).fetchone()
            == (1,)
        )
        _require(self.fact_creators.get(fact_id) == [event])
        _require(
            any(
                e.draft.chain_identity == scope.realm and e.sequence < event.sequence
                for e in self.evidence_mentions.get(evidence_id, [])
            )
        )
        _require(
            all(
                e.draft.chain_identity == scope.realm and e.sequence > event.sequence
                for e in self.invalidations.get(p["source_fact_id"], [])
            )
        )
        work = self.connection.execute(
            "SELECT kind, fact_id FROM projection_outbox WHERE mutation_id=?",
            (decision["mutation_id"],),
        ).fetchall()
        _require(
            all(row == ("fact-promoted", fact_id) for row in work) and len(work) <= 1
        )
        _require(
            not self.connection.execute(
                "SELECT 1 FROM evidence_outbox WHERE mutation_id=?",
                (decision["mutation_id"],),
            ).fetchone()
        )
        d = event.draft
        _require(
            d.action_code == "promote"
            and d.reason_code == "facts_promoted"
            and d.requested_scope is None
            and d.source_scope == scope
            and d.target_scope == target
            and tuple(map(str, d.affected_fact_ids)) == (fact_id,)
            and tuple(map(str, d.affected_evidence_ids)) == (evidence_id,)
            and str(d.evidence_reference) == evidence_id
            and d.evidence_digest == evidence["payload_digest"]
            and d.classification_transition is not None
            and d.classification_transition.previous == p["classification"]
            and d.classification_transition.current == level
            and d.trust_transition is not None
            and d.trust_transition.previous == source["trust"]
            and d.trust_transition.current == "validated"
        )
        return event

    def _replay(
        self, event: AuditEvent, original: AuditEvent, record: dict[str, Any]
    ) -> None:
        d, old = event.draft, original.draft
        _require(
            d.mutation_id is None
            and str(d.replay_of_mutation_id) == record["mutation_id"]
            and d.reason_code == "idempotent_replay"
            and event.sequence > original.sequence
            and str(d.principal_id) == record["principal_id"]
            and str(d.idempotency_key) == record["idempotency_key"]
            and d.command_digest == record["command_digest"]
        )
        # Replays restate the original publication identities, not new writes.
        for field in (
            "chain_kind",
            "chain_identity",
            "action_kind",
            "action_code",
            "outcome",
            "source_scope",
            "requested_scope",
            "target_scope",
            "affected_assertion_ids",
            "affected_fact_ids",
            "affected_evidence_ids",
            "affected_grant_ids",
            "classification_transition",
            "trust_transition",
            "evidence_reference",
            "evidence_digest",
        ):
            _require(getattr(d, field) == getattr(old, field))
