"""Strict command-specific input checks, before any content or session mutation."""

import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import BinaryIO
from uuid import RFC_4122, UUID, uuid5

from cairn.catalogue.audit import AuditValueError, Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.client.command_io import (
    MAX_INPUT_BYTES,
    CommandInputError,
    read_object,
    read_text,
)
from cairn.client.profiles import MemoryProfile
from cairn.client.types import DurableObservation, ModelTurn
from cairn.session_identity import MEMORY_REMEMBER_NAMESPACE

FIELDS = {
    "check": (set(), set()),
    "arrive": ({"query"}, {"budget", "history_fact_ids"}),
    "recall": ({"query"}, {"budget", "relevant_only"}),
    "acknowledge-visit": ({"visit_id"}, set()),
    "remember": (
        {"turn_id", "attempt_id", "response", "observations"},
        {"replaces_turn_id"},
    ),
    "status": (set(), {"turn_id"}),
    "resume": ({"turn_id"}, set()),
    "abandon": ({"turn_id", "reason"}, set()),
    "history": ({"fact_id"}, {"budget"}),
    "correct": ({"fact_ids", "reason", "idempotency_key"}, {"superseded_by"}),
    "disagree": ({"left_fact_id", "right_fact_id", "reason", "idempotency_key"}, set()),
    "suggest": (set(), {"observation", "fact_ids", "budget", "limit"}),
    "propose": (
        {"proposal_id", "source_fact_id", "target_scope", "reason", "idempotency_key"},
        set(),
    ),
    "proposal-list": (set(), {"limit", "after"}),
    "proposal-read": ({"proposal_id"}, set()),
    "proposal-accept": (
        {"proposal_id", "evidence_id", "target_classification", "idempotency_key"},
        set(),
    ),
    "proposal-reject": ({"proposal_id", "reason", "idempotency_key"}, set()),
}
PROPOSAL_MUTATIONS = frozenset({"propose", "proposal-accept", "proposal-reject"})
PROPOSAL_COMMANDS = PROPOSAL_MUTATIONS | {"proposal-list", "proposal-read"}
SESSION_COMMANDS = frozenset(
    {"arrive", "acknowledge-visit", "remember", "status", "resume", "abandon"}
)


def invalid() -> CommandInputError:
    return CommandInputError("invalid_input")


def identity(value: object, *, fact: bool = False) -> UUID:
    if type(value) is not str:
        raise invalid()
    try:
        result = UUID(value)
    except ValueError:
        raise invalid() from None
    if (
        str(result) != value
        or result.variant != RFC_4122
        or (fact and result.version != 4)
    ):
        raise invalid()
    return result


def text(value: object, maximum: int, *, empty: bool = False) -> str:
    if type(value) is not str:
        raise invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise invalid() from None
    if not (0 if empty else 1) <= size <= maximum:
        raise invalid()
    return value


def integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise invalid()
    return value


def identities(value: object, maximum: int, *, empty: bool = False) -> tuple[UUID, ...]:
    if type(value) is not list or not (0 if empty else 1) <= len(value) <= maximum:
        raise invalid()
    result = tuple(identity(item, fact=True) for item in value)
    if len(set(result)) != len(result):
        raise invalid()
    return result


def timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) != 27:
        raise invalid()
    try:
        result = parse_timestamp(value)
        if canonical_timestamp(result) != value:
            raise invalid()
        return result
    except CatalogueStorageError:
        raise invalid() from None


def completed_turn(document: dict[str, object]) -> ModelTurn:
    response = text(document["response"], 32768, empty=True)
    values = document["observations"]
    if type(values) is not list or len(values) > 8:
        raise invalid()
    observations = []
    for item in values:
        if (
            type(item) is not dict
            or "body" not in item
            or set(item) - {"body", "valid_from", "valid_to", "observed_at"}
        ):
            raise invalid()
        observation = DurableObservation(
            text(item["body"], 4096),
            timestamp(item.get("valid_from")),
            timestamp(item.get("valid_to")),
            timestamp(item.get("observed_at")),
        )
        if (
            observation.valid_from is not None
            and observation.valid_to is not None
            and observation.valid_from >= observation.valid_to
        ):
            raise invalid()
        observations.append(observation)
    if observations and any(
        o.observed_at != observations[0].observed_at for o in observations
    ):
        raise invalid()
    return ModelTurn(response, tuple(observations))


@dataclass(frozen=True)
class Input:
    left_fact_id: UUID | None = None
    right_fact_id: UUID | None = None
    query: str = ""
    budget: int = 16384
    relevant_only: bool = False
    history_fact_ids: tuple[UUID, ...] = ()
    turn_id: UUID | None = None
    attempt_id: UUID | None = None
    replaces_turn_id: UUID | None = None
    visit_id: UUID | None = None
    turn: ModelTurn | None = None
    reason: str = ""
    fact_id: UUID | None = None
    fact_ids: tuple[UUID, ...] = ()
    superseded_by: UUID | None = None
    idempotency_key: UUID | None = None
    observation: str | None = None
    limit: int = 8
    proposal_id: UUID | None = None
    source_fact_id: UUID | None = None
    target_scope: Scope | None = None
    evidence_id: UUID | None = None
    target_classification: Classification | None = None
    after: UUID | None = None


def proposal_identity(value: object) -> UUID:
    """Proposal references accept every UUID version/variant, in canonical text."""
    if type(value) is not str:
        raise invalid()
    try:
        result = UUID(value)
    except ValueError:
        raise invalid() from None
    if str(result) != value:
        raise invalid()
    return result


def proposal_scope(value: object) -> Scope:
    if type(value) is not dict or value.keys() != {"realm", "segments"}:
        raise invalid()
    segments = value["segments"]
    if type(segments) is not list or len(segments) > 16:
        raise invalid()
    if any(type(s) is not dict or s.keys() != {"kind", "identifier"} for s in segments):
        raise invalid()
    try:
        return Scope(
            value["realm"],
            tuple(ScopeSegment(s["kind"], s["identifier"]) for s in segments),
        )
    except AuditValueError:
        raise invalid() from None


def proposal_input(command: str, document: dict[str, object]) -> Input:
    if any(value is None and key != "after" for key, value in document.items()):
        raise invalid()
    classification = None
    if "target_classification" in document:
        try:
            classification = Classification(text(document["target_classification"], 63))
        except ValueError:
            raise invalid() from None
    return Input(
        proposal_id=proposal_identity(document["proposal_id"])
        if "proposal_id" in document
        else None,
        source_fact_id=proposal_identity(document["source_fact_id"])
        if "source_fact_id" in document
        else None,
        target_scope=proposal_scope(document["target_scope"])
        if "target_scope" in document
        else None,
        evidence_id=proposal_identity(document["evidence_id"])
        if "evidence_id" in document
        else None,
        target_classification=classification,
        reason=text(document["reason"], 4096) if "reason" in document else "",
        idempotency_key=identity(document["idempotency_key"])
        if command in PROPOSAL_MUTATIONS
        else None,
        limit=integer(document.get("limit", 50), 100),
        after=proposal_identity(document["after"])
        if document.get("after") is not None
        else None,
    )


def parse(command: str, stream: BinaryIO) -> Input:
    if command == "check":
        return Input()
    if command == "status":
        raw = "" if stream.isatty() else read_text(stream, limit=MAX_INPUT_BYTES)
        document = (
            {}
            if not raw.strip()
            else read_object(io.BytesIO(raw.encode()), limit=MAX_INPUT_BYTES)
        )
    else:
        document = read_object(stream, limit=MAX_INPUT_BYTES)
    required, optional = FIELDS[command]
    if required - document.keys() or document.keys() - (required | optional):
        raise invalid()
    if command == "disagree":
        left, right = (
            identity(document["left_fact_id"], fact=True),
            identity(document["right_fact_id"], fact=True),
        )
        if left == right:
            raise invalid()
        return Input(
            left_fact_id=left,
            right_fact_id=right,
            reason=text(document["reason"], 4096),
            idempotency_key=identity(document["idempotency_key"]),
        )
    if command in PROPOSAL_COMMANDS:
        return proposal_input(command, document)
    if any(
        value is None and key not in {"replaces_turn_id", "superseded_by"}
        for key, value in document.items()
    ):
        raise invalid()
    if command == "suggest" and (
        ("observation" in document) == ("fact_ids" in document)
    ):
        raise invalid()
    if "relevant_only" in document and type(document["relevant_only"]) is not bool:
        raise invalid()
    return Input(
        query=text(document["query"], 8192) if "query" in document else "",
        budget=integer(
            document.get("budget", 16384), 65536 if command == "suggest" else 1048576
        ),
        relevant_only=document.get("relevant_only") is True,
        history_fact_ids=identities(document["history_fact_ids"], 8, empty=True)
        if "history_fact_ids" in document
        else (),
        turn_id=identity(document["turn_id"]) if "turn_id" in document else None,
        attempt_id=identity(document["attempt_id"])
        if "attempt_id" in document
        else None,
        replaces_turn_id=identity(document["replaces_turn_id"])
        if document.get("replaces_turn_id") is not None
        else None,
        visit_id=identity(document["visit_id"]) if "visit_id" in document else None,
        turn=completed_turn(document) if command == "remember" else None,
        reason=text(document["reason"], 4096) if "reason" in document else "",
        fact_id=identity(document["fact_id"], fact=True)
        if "fact_id" in document
        else None,
        fact_ids=identities(document["fact_ids"], 8 if command == "suggest" else 100)
        if "fact_ids" in document
        else (),
        superseded_by=identity(document["superseded_by"], fact=True)
        if document.get("superseded_by") is not None
        else None,
        idempotency_key=identity(document["idempotency_key"])
        if "idempotency_key" in document
        else None,
        observation=text(document["observation"], 4096)
        if "observation" in document
        else None,
        limit=integer(document.get("limit", 8), 16),
    )


def command_key(
    operation: str, session_id: UUID, turn_id: UUID, attempt_id: UUID
) -> UUID:
    return uuid5(
        MEMORY_REMEMBER_NAMESPACE,
        f"daily-cli:v1:{operation}:{session_id}:{turn_id}:{attempt_id}",
    )


def preparation_size(value: Input, profile: MemoryProfile, principal_id: UUID) -> None:
    """Mirror the canonical envelope shape, never its authority or custody."""
    assert value.turn is not None
    payload = {
        "schema": "cairn.session.preparation/v1",
        "instance_id": str(profile.expected_instance_id),
        "principal_id": str(principal_id),
        "classification": profile.classification.value,
        "operation": "session-prepare",
        "command": {
            "scope": asdict(profile.scope),
            "session_id": str(profile.session_id),
            "turn_id": str(value.turn_id),
            "attempt_id": str(value.attempt_id),
            "response": value.turn.response,
            "observations": [
                {
                    "body": o.body,
                    "valid_from": None
                    if o.valid_from is None
                    else canonical_timestamp(o.valid_from),
                    "valid_to": None
                    if o.valid_to is None
                    else canonical_timestamp(o.valid_to),
                    "observed_at": None
                    if o.observed_at is None
                    else canonical_timestamp(o.observed_at),
                }
                for o in value.turn.observations
            ],
        },
    }
    if (
        len(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        )
        > 73728
    ):
        raise invalid()
