"""Offline reconstruction of session history and its normal-ingest custody.

This is deliberately independent of the session producer and its encoder.
Audit sequence orders mutations; wall time does not. Each saved result is
checked at its historical boundary, never against the current live counters.
"""

import hashlib
import json
import sqlite3
from collections import Counter
from typing import Any, cast
from uuid import RFC_4122, UUID, uuid5

from cairn.catalogue.audit import AuditEvent, hash_audit_event
from cairn.catalogue.sqlite import canonical_timestamp, parse_timestamp


class SessionVerificationError(ValueError):
    """Opaque failure: no recovered content or identities in diagnostics."""


def _require(condition: bool) -> None:
    if not condition:
        raise SessionVerificationError("session_integrity_invalid")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _document(raw: bytes) -> dict[str, Any]:
    _require(type(raw) is bytes)
    value = json.loads(raw)
    _require(type(value) is dict and _canonical(value) == raw)
    return value  # type: ignore[no-any-return]


def _uuid(value: str) -> None:
    identity = UUID(value)
    _require(str(identity) == value and identity.variant == RFC_4122)


def _timestamp(value: str) -> None:
    _require(
        type(value) is str
        and len(value) == 27
        and canonical_timestamp(parse_timestamp(value)) == value
    )


def _data_audit(event: AuditEvent) -> None:
    """Sessions and their evidence-free candidate ingest have one scope role."""
    draft = event.draft
    _require(
        draft.action_kind.value == "data"
        and draft.source_scope is None
        and draft.target_scope is None
        and draft.classification_transition is None
        and draft.trust_transition is None
        and draft.evidence_reference is None
        and draft.evidence_digest is None
        and not draft.affected_grant_ids
    )


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table}")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


_TABLE_ACTIONS = {
    "memory_sessions": "session-open",
    "memory_session_turns": "session-begin",
    "memory_session_preparations": "session-prepare",
    "memory_session_abandonments": "session-abandon",
    "memory_session_visits": "session-issue-visit",
    "memory_session_acknowledgements": "session-acknowledge-visit",
    "memory_session_commit_claims": "session-commit-claim",
    "memory_session_terminals": "session-commit",
}
_CUSTODY_FIELDS = ("custody_result", "custody_audit_receipt", "custody_idempotency_key")


def verify_sessions(
    connection: sqlite3.Connection, instance_id: UUID, events: dict[str, AuditEvent]
) -> None:
    """Called after structural, audit-chain and general custody verification."""
    try:
        _History(connection, instance_id, events).verify()
    except SessionVerificationError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
        sqlite3.Error,
    ) as error:
        raise SessionVerificationError("session_integrity_invalid") from error


class _History:
    def __init__(
        self,
        connection: sqlite3.Connection,
        instance_id: UUID,
        events: dict[str, AuditEvent],
    ) -> None:
        self.connection = connection
        self.instance_id = str(instance_id)
        self.events = events
        self.records = {
            r["mutation_id"]: r for r in _rows(connection, "idempotency_records")
        }
        self.ingests = {
            (r["principal_id"], r["idempotency_key"]): r
            for r in self.records.values()
            if r["operation"] == "ingest"
        }
        self.ingest_events: dict[tuple[str, str], list[AuditEvent]] = {}
        for event in events.values():
            draft = event.draft
            if draft.action_code == "ingest" and draft.outcome.value == "allow":
                self.ingest_events.setdefault(
                    (str(draft.principal_id), str(draft.idempotency_key)), []
                ).append(event)
        self.operations = {
            r["mutation_id"]: r for r in _rows(connection, "memory_session_operations")
        }
        self.entities: dict[str, tuple[str, dict[str, Any]]] = {}
        for table, action in _TABLE_ACTIONS.items():
            for row in _rows(connection, table):
                mid = row["mutation_id"]
                _require(mid not in self.entities)
                self.entities[mid] = action, row
        self.sessions: dict[str, dict[str, Any]] = {}
        self.turns: dict[tuple[str, str], dict[str, Any]] = {}
        self.preparations: dict[tuple[str, str], dict[str, Any]] = {}
        self.visits: dict[tuple[str, str], dict[str, Any]] = {}
        self.claims: dict[tuple[str, str], dict[str, Any]] = {}
        self.terminals: dict[tuple[str, str], dict[str, Any]] = {}

    def verify(self) -> None:
        ids = {
            mid
            for mid, row in self.records.items()
            if row["operation"] in _TABLE_ACTIONS.values()
        }
        _require(ids == set(self.entities))
        _require(
            set(self.operations)
            == {
                mid
                for mid, (action, _) in self.entities.items()
                if action not in ("session-commit", "session-commit-claim")
            }
        )
        # In particular, deleting BOTH the claim row and its idempotency row
        # cannot turn audited post-0011 history into apparent legacy history.
        audited = {
            str(e.draft.mutation_id)
            for e in self.events.values()
            if e.draft.action_code in _TABLE_ACTIONS.values()
            and e.draft.outcome.value == "allow"
            and e.draft.mutation_id is not None
        }
        _require(ids == audited)
        ordered = sorted(
            ids,
            key=lambda mid: (
                self.events[
                    self.records[mid]["original_event_id"]
                ].draft.chain_identity,
                self.events[self.records[mid]["original_event_id"]].sequence,
            ),
        )
        for mid in ordered:
            self._operation(mid)
        for event in self.events.values():
            draft = event.draft
            replay = str(draft.replay_of_mutation_id)
            if draft.outcome.value != "allow" or (
                draft.action_code not in _TABLE_ACTIONS.values() and replay not in ids
            ):
                continue
            if draft.mutation_id is not None:
                record = self.records[str(draft.mutation_id)]
                _require(record["original_event_id"] == str(event.event_id))
                _require(draft.replay_of_mutation_id is None)
                continue
            record = self.records[replay]
            original = self.events[record["original_event_id"]]
            _data_audit(event)
            _require(
                draft.action_code == record["operation"]
                and str(draft.principal_id) == record["principal_id"]
                and str(draft.idempotency_key) == record["idempotency_key"]
                and draft.command_digest == record["command_digest"]
                and draft.chain_kind == original.draft.chain_kind
                and draft.chain_identity == original.draft.chain_identity
                and draft.requested_scope == original.draft.requested_scope
                and event.sequence > original.sequence
                and not draft.affected_fact_ids
                and not draft.affected_assertion_ids
                and not draft.affected_evidence_ids
            )
        # Prepared + actual ingest, without a terminal, is a legitimate crash
        # gap. The custody itself must nevertheless match the immutable input.
        for key, preparation in self.preparations.items():
            self._custody(key, preparation, None)

    def _binding(
        self,
        mid: str,
        action: str,
        row: dict[str, Any],
        session: dict[str, Any],
        digest: bytes,
    ) -> tuple[dict[str, Any], AuditEvent]:
        record = self.records[mid]
        event = self.events[record["original_event_id"]]
        _data_audit(event)
        draft = event.draft
        _uuid(mid)
        _uuid(record["idempotency_key"])
        _require(
            record["operation"] == action
            and record["principal_id"] == session["principal_id"]
            and record["command_digest"] == digest
            and record["result_schema"] == "cairn.session.snapshot/v1"
        )
        _require(
            draft.action_code == action
            and draft.outcome.value == "allow"
            and draft.chain_kind.value == "realm"
            and draft.chain_identity == session["scope"]["realm"]
            and draft.action_kind.value == "data"
        )
        _require(
            str(draft.principal_id) == record["principal_id"]
            and str(draft.idempotency_key) == record["idempotency_key"]
            and str(draft.mutation_id) == mid
            and draft.command_digest == digest
        )
        _require(
            draft.requested_scope is not None
            and self._scope(draft.requested_scope) == session["scope"]
        )
        _require(
            not draft.affected_fact_ids
            and not draft.affected_assertion_ids
            and not draft.affected_evidence_ids
        )
        operation = self.operations.get(mid, row)
        for field in (
            "mutation_id",
            "principal_id",
            "operation",
            "idempotency_key",
            "command_digest",
        ):
            _require(operation[field] == record[field])
        if "recorded_at" in operation:
            _timestamp(operation["recorded_at"])
        result = _document(record["result_bytes"])
        _require(
            hashlib.sha256(record["result_bytes"]).digest() == record["result_digest"]
        )
        _require(set(result) == {"snapshot", "mutation_receipt"})
        _require(
            result["mutation_receipt"]
            == {"mutation_id": mid, "command_digest": digest.hex()}
        )
        return result, event

    @staticmethod
    def _scope(scope: Any) -> dict[str, Any]:
        return {
            "realm": scope.realm,
            "segments": [
                {"kind": s.kind, "identifier": s.identifier} for s in scope.segments
            ],
        }

    def _operation(self, mid: str) -> None:
        action, row = self.entities[mid]
        sid = row["session_id"]
        _uuid(sid)
        if action == "session-open":
            _require(
                sid not in self.sessions and row["instance_id"] == self.instance_id
            )
            _uuid(row["principal_id"])
            _require(row["classification"] in ("public", "internal", "restricted"))
            segments = json.loads(row["scope_segments"])
            _require(
                type(segments) is list
                and len(segments) <= 16
                and all(type(s) is dict and set(s) == {"id", "kind"} for s in segments)
                and _canonical(segments).decode() == row["scope_segments"]
            )
            scope = {
                "realm": row["realm_id"],
                "segments": [
                    {"kind": s["kind"], "identifier": s["id"]} for s in segments
                ],
            }
            self.sessions[sid] = {
                "session_id": sid,
                "instance_id": self.instance_id,
                "principal_id": row["principal_id"],
                "scope": scope,
                "classification": row["classification"],
                "turn_count": 0,
                "prepared_bytes": 0,
                "acknowledged_watermark": 0,
                "acknowledged_at": None,
            }
        session = self.sessions[sid]
        command: dict[str, Any] = {"scope": session["scope"], "session_id": sid}
        tid = row.get("turn_id")
        # Only turn actions dereference this key; session/visit actions have
        # no turn and never enter the turn-state maps.
        key = sid, cast(str, tid)
        if tid is not None:
            _uuid(tid)
            command["turn_id"] = tid
        visit_fields: dict[str, Any] = {}
        if action == "session-open":
            command["classification"] = session["classification"]
        elif action == "session-begin":
            _require(key not in self.turns)
            _uuid(row["attempt_id"])
            _require(
                all(
                    t["attempt_id"] != row["attempt_id"]
                    for k, t in self.turns.items()
                    if k[0] == sid
                )
            )
            predecessor = row["replaces_turn_id"]
            if predecessor is not None:
                _uuid(predecessor)
                _require(self.turns[sid, predecessor]["state"] == "abandoned")
                _require(
                    all(
                        t["replaces_turn_id"] != predecessor
                        for k, t in self.turns.items()
                        if k[0] == sid
                    )
                )
            command.update(attempt_id=row["attempt_id"], replaces_turn_id=predecessor)
            self.turns[key] = {
                "turn_id": tid,
                "attempt_id": row["attempt_id"],
                "replaces_turn_id": predecessor,
                "state": "started",
            }
            session["turn_count"] += 1
        elif action == "session-prepare":
            turn = self.turns[key]
            _require(turn["state"] == "started")
            payload = row["payload"]
            _require(1 <= len(payload) <= 73728)
            document = _document(payload)
            output = document["command"]
            self._output(output)
            command.update(
                attempt_id=turn["attempt_id"],
                response=output["response"],
                observations=output["observations"],
            )
            _require(document == self._envelope(session, action, command))
            _require(hashlib.sha256(payload).digest() == row["payload_digest"])
            turn.update(
                state="prepared",
                response=output["response"],
                observations=output["observations"],
            )
            self.preparations[key] = row
            session["prepared_bytes"] += len(payload)
        elif action == "session-abandon":
            turn = self.turns[key]
            _require(
                turn["state"] == "started"
                and type(row["reason"]) is str
                and 1 <= len(row["reason"].encode()) <= 4096
            )
            command["reason"] = row["reason"]
            turn.update(state="abandoned", abandonment_reason=row["reason"])
        elif action == "session-issue-visit":
            _uuid(row["visit_id"])
            _timestamp(row["issued_at"])
            _require(
                type(row["watermark"]) is int
                and row["watermark"] == 1 + sum(k[0] == sid for k in self.visits)
            )
            _require((sid, row["visit_id"]) not in self.visits)
            self.visits[sid, row["visit_id"]] = row
            visit_fields = {
                "visit_id": row["visit_id"],
                "visit_watermark": row["watermark"],
                "visit_at": row["issued_at"],
            }
        elif action == "session-acknowledge-visit":
            visit = self.visits[sid, row["visit_id"]]
            command["visit_id"] = row["visit_id"]
            if visit["watermark"] > session["acknowledged_watermark"]:
                session.update(
                    acknowledged_watermark=visit["watermark"],
                    acknowledged_at=visit["issued_at"],
                )
        elif action in ("session-commit-claim", "session-commit"):
            preparation = self.preparations[key]
            _require(
                row["preparation_mutation_id"] == preparation["mutation_id"]
                and row["preparation_digest"] == preparation["payload_digest"]
            )
            if action == "session-commit-claim":
                claim_key = row["principal_id"], row["idempotency_key"]
                _require(claim_key not in self.claims)
                self.claims[claim_key] = row
            else:
                _require(
                    key not in self.terminals and self.turns[key]["state"] == "prepared"
                )
                claim = self.claims.get((row["principal_id"], row["idempotency_key"]))
                if claim is not None:
                    _require(
                        all(
                            row[f] == claim[f]
                            for f in (
                                "session_id",
                                "turn_id",
                                "command_digest",
                                "preparation_mutation_id",
                                "preparation_digest",
                            )
                        )
                    )
                # No claim is permitted only for genuine pre-0011 history.
                # The global audited/record/entity equality rejects deletion.
                custody = self._custody(key, preparation, row)
                self.turns[key].update(
                    state="skipped" if custody is None else "committed"
                )
                if custody is not None:
                    self.turns[key].update(custody)
                _require(row["state"] == self.turns[key]["state"])
                self.terminals[key] = row
        else:
            raise SessionVerificationError("session_integrity_invalid")
        if action in ("session-commit", "session-commit-claim"):
            preparation = self.preparations[key]
            envelope = {
                "schema": "cairn.session.commit/v1",
                "operation": "session-commit",
                "command": command,
                "principal_id": session["principal_id"],
                "preparation_mutation_id": preparation["mutation_id"],
                "preparation_digest": preparation["payload_digest"].hex(),
            }
        else:
            envelope = self._envelope(session, action, command)
        digest = hashlib.sha256(_canonical(envelope)).digest()
        result, _ = self._binding(mid, action, row, session, digest)
        expected = {
            **session,
            "state": "open",
            "operational_receipt": result["mutation_receipt"],
            "custody_receipt": None,
            "turn_id": None,
            "attempt_id": None,
            "replaces_turn_id": None,
            "response": None,
            "observations": [],
            "abandonment_reason": None,
            "visit_id": None,
            "visit_watermark": None,
            "visit_at": None,
            **dict.fromkeys(_CUSTODY_FIELDS),
        }
        if tid is not None:
            expected.update(self.turns[key])
        expected.update(visit_fields)
        snapshot = result["snapshot"]
        # 0009 snapshots predate the three appended custody fields. Accept
        # absent optional nulls, but never omit a value representing custody.
        for field in _CUSTODY_FIELDS:
            if field not in snapshot and expected[field] is None:
                expected.pop(field)
        _require(_canonical(snapshot) == _canonical(expected))
        if action == "session-commit":
            _timestamp(row["recorded_at"])
            _require(row["result"] == self.records[mid]["result_bytes"])

    @staticmethod
    def _envelope(
        session: dict[str, Any], action: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema": "cairn.session.preparation/v1"
            if action == "session-prepare"
            else "cairn.session.command/v1",
            "instance_id": session["instance_id"],
            "principal_id": session["principal_id"],
            "classification": session["classification"],
            "operation": action,
            "command": command,
        }

    @staticmethod
    def _output(command: dict[str, Any]) -> None:
        _require(
            type(command["response"]) is str
            and len(command["response"].encode()) <= 32768
        )
        observations = command["observations"]
        _require(type(observations) is list and len(observations) <= 8)
        times = set()
        for observation in observations:
            _require(
                type(observation) is dict
                and set(observation)
                == {"body", "valid_from", "valid_to", "observed_at"}
            )
            _require(
                type(observation["body"]) is str
                and 1 <= len(observation["body"].encode()) <= 4096
            )
            for field in ("valid_from", "valid_to", "observed_at"):
                if observation[field] is not None:
                    _timestamp(observation[field])
            start, end = observation["valid_from"], observation["valid_to"]
            _require(end is None or start is None or start < end)
            times.add(observation["observed_at"])
        _require(len(times) <= 1)

    def _ingest_record(self, principal: str, key: str) -> dict[str, Any] | None:
        """Audit-to-record reconciliation must precede any no-custody decision.

        The caller key is principal-wide for ingest. An intact different
        command can reserve it legitimately, but an original successful event
        can never lose its record or be replaced by a second mutation.
        """
        record = self.ingests.get((principal, key))
        events = self.ingest_events.get((principal, key), [])
        if not events:
            _require(record is None)
            return None
        _require(record is not None)
        assert record is not None
        originals = [event for event in events if event.draft.mutation_id is not None]
        _require(len(originals) == 1)
        original = originals[0]
        _require(
            record["original_event_id"] == str(original.event_id)
            and str(original.draft.mutation_id) == record["mutation_id"]
            and original.draft.replay_of_mutation_id is None
            and record["result_schema"] == "cairn.authority.assertion/v1"
        )
        result = _document(record["result_bytes"])["result"]
        for event in events:
            draft = event.draft
            _require(
                draft.command_digest == record["command_digest"]
                and draft.action_kind.value == "data"
                and draft.chain_kind == original.draft.chain_kind
                and draft.chain_identity == original.draft.chain_identity
                and draft.requested_scope == original.draft.requested_scope
                and tuple(map(str, draft.affected_fact_ids))
                == tuple(result["fact_ids"])
                and tuple(map(str, draft.affected_assertion_ids))
                == (result["assertion_id"],)
                and tuple(map(str, draft.affected_evidence_ids))
                == (() if result["evidence_id"] is None else (result["evidence_id"],))
            )
            if event != original:
                _require(
                    draft.mutation_id is None
                    and str(draft.replay_of_mutation_id) == record["mutation_id"]
                    and event.sequence > original.sequence
                )
        return record

    def _custody(
        self,
        key: tuple[str, str],
        preparation: dict[str, Any],
        terminal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        session = self.sessions[key[0]]
        observations = _document(preparation["payload"])["command"]["observations"]
        stable_key = str(
            uuid5(UUID("4e14ee38-0e14-5069-8d36-502c18b1c324"), f"{key[0]}:{key[1]}")
        )
        record = self._ingest_record(session["principal_id"], stable_key)
        if not observations:
            # Empty turns never invoke ingest. A separately ingested command
            # at this key is not their custody and does not prevent skipping.
            if terminal is not None:
                _require(
                    all(
                        terminal[f] is None
                        for f in (
                            "custody_mutation_id",
                            "custody_event_id",
                            "custody_assertion_id",
                        )
                    )
                )
            return None
        if record is None:
            _require(terminal is None)
            return None
        _require(record["result_schema"] == "cairn.authority.assertion/v1")
        scope = session["scope"]
        segments = [
            {"kind": s["kind"], "id": s["identifier"]} for s in scope["segments"]
        ]
        facts = [
            {f: o[f] for f in ("body", "valid_from", "valid_to")} for o in observations
        ]
        digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "cairn.authority/v1",
                    "command": "ingest",
                    "scope": {"realm": scope["realm"], "segments": segments},
                    "classification": session["classification"],
                    "source_type": "agent-claim",
                    "requested_trust": "candidate",
                    "observed_at": observations[0]["observed_at"],
                    "metadata": None,
                    "facts": facts,
                    "payload_sha256": None,
                }
            )
        ).digest()
        if record["command_digest"] != digest:
            # An intact prior command may reserve the key, making normal
            # commit reject. That leaves a valid prepared/claimed session.
            _require(terminal is None)
            return None
        document = _document(record["result_bytes"])
        result = document["result"]
        receipt = {"mutation_id": record["mutation_id"], "command_digest": digest.hex()}
        _require(document == {"result": result, "mutation_receipt": receipt})
        _require(
            set(result) == {"assertion_id", "fact_ids", "evidence_id"}
            and result["evidence_id"] is None
        )
        assertion_id = result["assertion_id"]
        _uuid(assertion_id)
        ids = result["fact_ids"]
        _require(
            type(ids) is list
            and len(ids) == len(observations)
            and len(set(ids)) == len(ids)
        )
        for fid in ids:
            _uuid(fid)
        assertion = self.connection.execute(
            "SELECT realm_id,scope_segments,classification,source_type,principal_id,observed_at,metadata,recorded_at FROM assertions WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        _require(
            assertion is not None
            and assertion[:7]
            == (
                scope["realm"],
                _canonical(segments).decode(),
                session["classification"],
                "agent-claim",
                session["principal_id"],
                observations[0]["observed_at"],
                None,
            )
        )
        stored = self.connection.execute(
            "SELECT fact_id,body,valid_from,valid_to,realm_id,scope_segments,classification,trust,derived_from,promoted_by,evidence_id,recorded_at FROM facts WHERE assertion_id=?",
            (assertion_id,),
        ).fetchall()
        _require({r[0] for r in stored} == set(ids))
        _require(
            Counter((r[1], r[2], r[3]) for r in stored)
            == Counter(
                (o["body"], o["valid_from"], o["valid_to"]) for o in observations
            )
        )
        for r in stored:
            _require(
                r[4:]
                == (
                    scope["realm"],
                    _canonical(segments).decode(),
                    session["classification"],
                    "candidate",
                    None,
                    None,
                    None,
                    assertion[7],
                )
            )
        original = self.events[record["original_event_id"]]
        event = (
            original if terminal is None else self.events[terminal["custody_event_id"]]
        )
        for audit in (original, event):
            _data_audit(audit)
            draft = audit.draft
            _require(
                draft.action_code == "ingest"
                and draft.outcome.value == "allow"
                and str(draft.mutation_id or draft.replay_of_mutation_id)
                == record["mutation_id"]
                and str(draft.principal_id) == session["principal_id"]
                and str(draft.idempotency_key) == stable_key
                and draft.command_digest == digest
            )
            _require(
                draft.requested_scope is not None
                and self._scope(draft.requested_scope) == scope
                and draft.chain_kind.value == "realm"
                and draft.chain_identity == scope["realm"]
            )
            _require(
                set(map(str, draft.affected_fact_ids)) == set(ids)
                and tuple(map(str, draft.affected_assertion_ids)) == (assertion_id,)
                and not draft.affected_evidence_ids
            )
        _require(original.draft.outcome.value == "allow")
        prep_event = self.events[
            self.records[preparation["mutation_id"]]["original_event_id"]
        ]
        _require(original.sequence <= event.sequence)
        if terminal is not None:
            _require(
                terminal["custody_mutation_id"] == record["mutation_id"]
                and terminal["custody_assertion_id"] == assertion_id
            )
            terminal_event = self.events[
                self.records[terminal["mutation_id"]]["original_event_id"]
            ]
            _require(prep_event.sequence < event.sequence < terminal_event.sequence)
        return {
            "custody_result": result,
            "custody_receipt": receipt,
            "custody_idempotency_key": stable_key,
            "custody_audit_receipt": {
                "event_id": str(event.event_id),
                "chain_kind": event.draft.chain_kind.value,
                "chain_identity": event.draft.chain_identity,
                "sequence": event.sequence,
                "recorded_at": canonical_timestamp(event.recorded_at),
                "event_hash": hash_audit_event(event).hex(),
            },
        }
