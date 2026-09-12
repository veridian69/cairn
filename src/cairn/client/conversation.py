"""Opt-in conversation workflow over public memory HTTP, with no local store.

Mutations are deliberately sequential and never retried automatically. A caller
can resubmit identical arguments and the same root key after an interruption.
Public idempotency records and correction history provide recovery evidence.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, cast
from uuid import RFC_4122, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cairn.client.conversation_sources import SourceBundle, SourceError
from cairn.client.durable_session import DurableMemorySession
from cairn.client.errors import MemoryOperationFailure
from cairn.client.memory import MemoryClient
from cairn.client.profiles import MemoryProfile
from cairn.client.rendering import render_result
from cairn.client.turn_receipts import ReceiptJournal, ReceiptJournalError
from cairn.client.types import ConnectionStatus, DurableObservation

HISTORY_BUDGET = 131072
READ_BUDGET = 16384
WORKFLOW = "cairn.conversation/v1"
CANONICAL_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CANONICAL_UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CanonicalUUID = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=CANONICAL_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    ),
]
CanonicalUUID4 = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=CANONICAL_UUID4_PATTERN,
        json_schema_extra={"format": "uuid"},
    ),
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def bounded_value(cls, value: object, info: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("blank_input")
            name = getattr(info, "field_name", "")
            limit = 8192 if name == "query" else 4096
            if len(value.encode("utf-8")) > limit:
                raise ValueError("input_too_large")
            if name in {"fact_id", "visit_id", "idempotency_key", "source_id"}:
                identity = UUID(value)
                if str(identity) != value or identity.variant != RFC_4122:
                    raise ValueError("invalid_uuid")
                if name == "fact_id" and identity.version != 4:
                    raise ValueError("invalid_fact_id")
        return value


class Query(Input):
    query: str = Field(min_length=1, max_length=8192)


class History(Input):
    fact_id: CanonicalUUID4


class Remember(Input):
    body: str = Field(min_length=1, max_length=4096)
    source_id: CanonicalUUID
    idempotency_key: CanonicalUUID


class Replace(Input):
    fact_id: CanonicalUUID4
    expected_old_body: str = Field(min_length=1, max_length=4096)
    replacement_body: str = Field(min_length=1, max_length=4096)
    source_id: CanonicalUUID
    idempotency_key: CanonicalUUID


class Acknowledge(Input):
    visit_id: CanonicalUUID


INPUTS: dict[str, type[Input]] = {
    "check": Input,
    "sources": Input,
    "recall": Query,
    "history": History,
    "remember": Remember,
    "replace": Replace,
    "arrive": Query,
    "acknowledge_visit": Acknowledge,
}


def plain(value: object) -> dict[str, object]:
    """Reuse the client's bounded serializer; discard its CLI-only wrapper."""
    return cast(
        dict[str, object], json.loads(render_result("history", value))["result"]
    )


class WorkflowFailure(Exception):
    """A closed, content-free reason; never retain input or exception reprs."""

    def __init__(self, code: str) -> None:
        self.code = code


READ_ONLY_TOOLS = frozenset({"check", "sources", "recall", "history"})


class ConversationAdapter:
    def __init__(
        self,
        client: MemoryClient,
        *,
        profile: MemoryProfile,
        expected_principal: UUID,
        sources: SourceBundle,
        receipts: ReceiptJournal | None = None,
        read_only: bool = False,
        assessment_context: dict[str, object] | None = None,
    ) -> None:
        if (
            type(read_only) is not bool
            or (assessment_context is not None and not read_only)
            or (
                assessment_context is not None
                and (
                    type(assessment_context) is not dict
                    or set(assessment_context)
                    != {"source", "content_role", "binding", "data"}
                    or assessment_context["source"] != "cairn-memory/v1"
                    or assessment_context["content_role"] != "untrusted-data"
                    or type(assessment_context["binding"]) is not dict
                    or type(assessment_context["data"]) is not dict
                    or assessment_context["data"].get("budget_exhausted") is not False
                    or assessment_context["data"].get("semantic_degraded") is not False
                )
            )
            or type(client) is not MemoryClient
            or type(profile) is not MemoryProfile
            or type(expected_principal) is not UUID
            or client.scope != profile.scope
            or client.classification != profile.classification
            or client.expected_instance_id != profile.expected_instance_id
            or type(sources) is not SourceBundle
            or (receipts is not None and type(receipts) is not ReceiptJournal)
            or (receipts is not None and not receipts._bound_to(sources))
        ):
            raise ValueError("adapter_context_mismatch")
        sources.require_context(profile, expected_principal)
        if assessment_context is not None and assessment_context["binding"] != {
            "instance_id": str(profile.expected_instance_id),
            "principal_id": str(expected_principal),
            "scope": {
                "realm": profile.scope.realm,
                "segments": [
                    {"kind": segment.kind, "identifier": segment.identifier}
                    for segment in profile.scope.segments
                ],
            },
            "classification": profile.classification.value,
            "session_id": str(profile.session_id),
        }:
            raise ValueError("adapter_context_mismatch")
        self.client = client
        self.profile = profile
        self.expected_principal = expected_principal
        self.sources = sources
        self.receipts = receipts
        self.read_only = read_only
        self.assessment_context = assessment_context
        self._write_lock = asyncio.Lock()
        self.scope = {
            "realm": profile.scope.realm,
            "segments": [
                {"kind": segment.kind, "identifier": segment.identifier}
                for segment in profile.scope.segments
            ],
        }

    async def _check(self) -> dict[str, object]:
        diagnostic = await self.client.diagnose(
            expected_instance_id=self.profile.expected_instance_id
        )
        if diagnostic.status is not ConnectionStatus.READY:
            raise WorkflowFailure("connection_refused")
        if diagnostic.principal_id != self.expected_principal:
            raise WorkflowFailure("principal_mismatch")
        return plain(diagnostic)

    async def _history(self, fact_id: str) -> dict[str, object]:
        await self._check()
        packet = await self.client.history(UUID(fact_id), budget=HISTORY_BUDGET)
        data = cast(dict[str, object], plain(packet)["data"])
        if data["budget_exhausted"]:
            raise WorkflowFailure("history_incomplete")
        return data

    def _fact(
        self, history: dict[str, object], fact_id: str, body: str
    ) -> dict[str, object]:
        facts = cast(list[dict[str, object]], history["facts"])
        selected = [fact for fact in facts if fact["fact_id"] == fact_id]
        if len(selected) != 1:
            raise WorkflowFailure("fact_unverified")
        fact = selected[0]
        if fact["scope"] != self.scope:
            raise WorkflowFailure("exact_scope_required")
        if fact["body"] != body:
            raise WorkflowFailure("body_mismatch")
        return fact

    def _link(
        self, history: dict[str, object], fact_id: str, reason: str, body: str
    ) -> str:
        corrections = cast(list[dict[str, object]], history["corrections"])
        links = [row for row in corrections if row["fact_id"] == fact_id]
        if (
            len(links) != 1
            or links[0]["reason"] != reason
            or links[0]["principal_id"] != str(self.expected_principal)
            or not isinstance(links[0]["superseded_by"], str)
        ):
            raise WorkflowFailure("replacement_link_unverified")
        replacement = links[0]["superseded_by"]
        self._new_fact(self._fact(history, replacement, body))
        return replacement

    def _new_fact(self, fact: dict[str, object]) -> None:
        if (
            fact["invalidated_at"] is not None
            or fact["source_principal_id"] != str(self.expected_principal)
            or fact["classification"] != self.profile.classification.value
            or fact["source_type"] != "agent-claim"
            or fact["trust"] != "candidate"
        ):
            raise WorkflowFailure("replacement_fact_unverified")

    async def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Validate write context, then journal one complete write transaction."""
        if self.read_only and name not in READ_ONLY_TOOLS:
            return {
                "status": "rejected",
                "stage": "input",
                "error": {"code": "read_only_tool"},
            }
        if name not in {"remember", "replace"} or self.receipts is None:
            return await self._call_untracked(name, arguments)
        try:
            value = INPUTS[name].model_validate(arguments)
            assert isinstance(value, Remember | Replace)
            self.sources.source(UUID(value.source_id))
            if (
                isinstance(value, Replace)
                and value.expected_old_body == value.replacement_body
            ):
                return await self._call_untracked(name, arguments)
        except (SourceError, ValidationError, ValueError, UnicodeError):
            return await self._call_untracked(name, arguments)
        async with self._write_lock:
            try:
                sequence = self.receipts.begin(name, UUID(value.source_id))
            except ReceiptJournalError as exc:
                return {
                    "status": "unconfirmed",
                    "stage": "input",
                    "mapping": None,
                    "remember_receipt": None,
                    "correction_receipt": None,
                    "link_verified": False,
                    "error": {"code": exc.code},
                }
            result = await self._call_untracked(name, arguments)
            try:
                self.receipts.finish(sequence, result)
            except ReceiptJournalError:
                # The durable write result is still returned. The unfinished
                # journal intent makes the host summary unknown without retrying.
                pass
            return result

    async def _call_untracked(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "unconfirmed",
            "stage": "input",
            "mapping": None,
            "remember_receipt": None,
            "correction_receipt": None,
            "link_verified": False,
            "error": None,
        }
        try:
            if name not in INPUTS:
                raise WorkflowFailure("unknown_tool")
            value = INPUTS[name].model_validate(arguments)
            if isinstance(value, Remember | Replace):
                selected = self.sources.source(UUID(value.source_id))
                result["selected_source"] = {
                    "source_id": str(selected.source_id),
                    "origin": selected.origin,
                    "relationship": "context for a candidate claim; not proof of approval or truth",
                }
            if (
                isinstance(value, Replace)
                and value.expected_old_body == value.replacement_body
            ):
                raise WorkflowFailure("unchanged_replacement")
            result["stage"] = "check"
            diagnostic = await self._check()
            if name == "check":
                return {"status": "ok", "result": diagnostic}
            if name == "sources":
                document = {
                    **self.sources.to_document(),
                    "content_role": "untrusted-data",
                }
                if self.assessment_context is not None:
                    document["host_assessment_context"] = self.assessment_context
                return {
                    "status": "ok",
                    "result": document,
                }
            if isinstance(value, Remember | Replace):
                return await self._write(value, result)
            result["stage"] = name
            if name == "recall" and isinstance(value, Query):
                packet = await self.client.recall(value.query, budget=READ_BUDGET)
            elif isinstance(value, History):
                packet = await self.client.history(
                    UUID(value.fact_id), budget=READ_BUDGET
                )
            else:
                if self.profile.session_id is None:
                    raise WorkflowFailure("session_id_required")
                session = DurableMemorySession(
                    self.client, session_id=self.profile.session_id
                )
                if name == "arrive" and isinstance(value, Query):
                    result["stage"] = "session_open"
                    opened = await session.open()
                    result["session_receipt"] = plain(opened)
                    result["stage"] = "arrive"
                    await self._check()
                    arrival = await session.arrive(value.query, budget=READ_BUDGET)
                    incomplete = bool(
                        arrival.briefing.failures
                        or arrival.briefing.budget_exhausted
                        or arrival.briefing.omitted_history_fact_ids
                        or arrival.briefing.omitted_summary_count
                        or arrival.clock_rollback
                    )
                    return {
                        "status": "partial" if incomplete else "ok",
                        "result": plain(arrival),
                    }
                if isinstance(value, Acknowledge):
                    acknowledgement = await session.acknowledge_visit(
                        UUID(value.visit_id)
                    )
                    return {"status": "ok", "result": plain(acknowledgement)}
                raise WorkflowFailure("unknown_tool")
            return {"status": "ok", "result": plain(packet)}
        except SourceError as exc:
            result["error"] = {"code": exc.code}
            result["status"] = (
                "rejected" if result["stage"] == "input" else "unconfirmed"
            )
        except (ValidationError, ValueError, UnicodeError):
            result["error"] = {"code": "invalid_input"}
            result["status"] = (
                "rejected" if result["stage"] == "input" else "unconfirmed"
            )
        except WorkflowFailure as exc:
            result["error"] = {"code": exc.code}
            if result["stage"] == "input":
                result["status"] = "rejected"
        except MemoryOperationFailure as exc:
            result["error"] = {"code": exc.failure.code, "operation": exc.operation}
        except Exception:
            result["error"] = {"code": "internal_error"}
        if (
            result["remember_receipt"] is not None
            or result["correction_receipt"] is not None
            or result.get("session_receipt") is not None
        ):
            result["status"] = "partial"
        return result

    async def _write(
        self, value: Remember | Replace, result: dict[str, object]
    ) -> dict[str, object]:
        root = UUID(value.idempotency_key)
        mode = "replace" if isinstance(value, Replace) else "remember"
        # The server hashes the complete request for idempotency. Include the
        # replacement-only arguments as screened metadata so changing them also
        # conflicts, rather than deriving a fresh child key from changed input.
        metadata: dict[str, object] = {
            "workflow": WORKFLOW,
            "operation": mode,
            "root": str(root),
            "source_id": value.source_id,
            "source_origin": "host_input",
        }
        if isinstance(value, Replace):
            metadata.update(
                fact_id=value.fact_id, expected_old_body=value.expected_old_body
            )
        reason = f"Conversation replacement ({WORKFLOW}); root={root}"
        linked_id: str | None = None
        if isinstance(value, Replace):
            result["stage"] = "old_readback"
            history = await self._history(value.fact_id)
            old = self._fact(history, value.fact_id, value.expected_old_body)
            if old["invalidated_at"] is not None:
                linked_id = self._link(
                    history, value.fact_id, reason, value.replacement_body
                )
        body = value.replacement_body if isinstance(value, Replace) else value.body
        result["stage"] = "remember"
        await self._check()
        receipt = await self.client.remember(
            (DurableObservation(body),),
            idempotency_key=uuid5(root, f"{WORKFLOW}:remember"),
            evidence_payload=self.sources.evidence_payload(UUID(value.source_id)),
            metadata=metadata,
        )
        result["remember_receipt"] = plain(receipt)
        if receipt.result is None:
            raise WorkflowFailure("custody_unconfirmed")
        fact_id = cast(tuple[str, ...], receipt.result["fact_ids"])[0]
        if linked_id is not None and fact_id != linked_id:
            raise WorkflowFailure("replacement_identity_conflict")
        result["stage"] = "new_readback"
        history = await self._history(fact_id)
        fact = self._fact(history, fact_id, body)
        if fact["assertion_id"] != receipt.result["assertion_id"]:
            raise WorkflowFailure("assertion_identity_conflict")
        self._new_fact(fact)
        result["mapping"] = fact
        if isinstance(value, Replace):
            result["stage"] = "correct"
            await self._check()
            correction = await self.client.correct(
                (UUID(value.fact_id),),
                reason=reason,
                superseded_by=UUID(fact_id),
                idempotency_key=uuid5(root, f"{WORKFLOW}:correct"),
            )
            result["correction_receipt"] = plain(correction)
            result["stage"] = "link_readback"
            history = await self._history(value.fact_id)
            self._fact(history, value.fact_id, value.expected_old_body)
            if self._link(history, value.fact_id, reason, body) != fact_id:
                raise WorkflowFailure("replacement_identity_conflict")
            result["link_verified"] = True
        result["stage"] = "complete"
        result["status"] = "verified"
        return result
