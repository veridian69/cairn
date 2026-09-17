"""Closed public session command translation and actual snapshot rendering."""

import json
from dataclasses import asdict
from uuid import UUID

from cairn.authority.session_codec import canonical
from cairn.authority.session_types import (
    AbandonTurn,
    AcknowledgeVisit,
    BeginTurn,
    CommitTurn,
    DurableObservation,
    IssueVisit,
    OpenSession,
    PrepareTurn,
    ReadSession,
    SessionCommand,
    SessionSnapshot,
)
from cairn.catalogue.audit import Classification
from cairn.transports.memory.session_models import (
    AbandonTurnRequest,
    AcknowledgeVisitRequest,
    BeginTurnRequest,
    OpenSessionRequest,
    PrepareTurnRequest,
    ReadSessionRequest,
    SessionRequest,
    SessionSnapshotBody,
    TurnRequest,
)
from cairn.transports.v1.translation import _scope, _timestamp, _uuid


def session_command(model: SessionRequest) -> tuple[UUID, SessionCommand]:
    expected = _uuid(model.expected_instance_id, "expected_instance_id")
    scope = _scope(model.scope, "scope")
    sid = _uuid(model.session_id, "session_id")
    command: SessionCommand
    if isinstance(model, OpenSessionRequest):
        command = OpenSession(scope, sid, Classification(model.classification))
    elif isinstance(model, BeginTurnRequest):
        command = BeginTurn(
            scope,
            sid,
            _uuid(model.turn_id, "turn_id"),
            _uuid(model.attempt_id, "attempt_id"),
            None
            if model.replaces_turn_id is None
            else _uuid(model.replaces_turn_id, "replaces_turn_id"),
        )
    elif isinstance(model, PrepareTurnRequest):
        command = PrepareTurn(
            scope,
            sid,
            _uuid(model.turn_id, "turn_id"),
            _uuid(model.attempt_id, "attempt_id"),
            model.response,
            tuple(
                DurableObservation(
                    o.body,
                    _timestamp(o.valid_from, "valid_from"),
                    _timestamp(o.valid_to, "valid_to"),
                    _timestamp(o.observed_at, "observed_at"),
                )
                for o in model.observations
            ),
        )
    elif isinstance(model, AbandonTurnRequest):
        command = AbandonTurn(scope, sid, _uuid(model.turn_id, "turn_id"), model.reason)
    elif isinstance(model, TurnRequest):
        command = CommitTurn(scope, sid, _uuid(model.turn_id, "turn_id"))
    elif isinstance(model, ReadSessionRequest):
        command = ReadSession(
            scope,
            sid,
            None if model.turn_id is None else _uuid(model.turn_id, "turn_id"),
        )
    elif isinstance(model, AcknowledgeVisitRequest):
        command = AcknowledgeVisit(scope, sid, _uuid(model.visit_id, "visit_id"))
    else:
        command = IssueVisit(scope, sid)
    return expected, command


def session_result(value: SessionSnapshot) -> SessionSnapshotBody:
    return SessionSnapshotBody.model_validate(json.loads(canonical(asdict(value))))
