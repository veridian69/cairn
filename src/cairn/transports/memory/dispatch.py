"""Bound, transport-neutral memory operations sharing the runtime write gate."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import RFC_4122, UUID

import anyio

from cairn.authority.diagnostics import CairnDiagnostics, Diagnose, DiagnosticSnapshot
from cairn.authority.gate import Actor, fetch_from, instance_denial_draft, instance_id
from cairn.authority.housekeeping_types import Suggest, SuggestionResult
from cairn.authority.memory_codec import memory_value
from cairn.authority.mutations import CairnAuthority
from cairn.authority.proposals import CairnProposals
from cairn.authority.sessions import CairnSessions
from cairn.catalogue.audit import ActionKind, Classification
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    Rejected,
    RetryClass,
    StableFailure,
)
from cairn.screening import SecretScreen
from cairn.transports.memory.models import (
    CorrectRequest,
    DiagnoseBody,
    DiagnoseRequest,
    DisagreeRequest,
    HistoryRequest,
    RecallRequest,
    RelationshipResult,
    RememberRequest,
    ResolveRequest,
)
from cairn.transports.memory.operations import (
    BY_TOOL,
    PROPOSAL_TOOL_NAMES,
    SESSION_TOOL_NAMES,
)
from cairn.transports.memory.proposal_models import ProposalContext
from cairn.transports.memory.proposal_translation import proposal_command
from cairn.transports.memory.session_models import SessionRequest
from cairn.transports.memory.session_translation import session_command, session_result
from cairn.transports.memory.suggestion_models import SuggestRequest
from cairn.transports.memory.suggestion_translation import (
    suggestion_command,
    suggestion_result,
)
from cairn.transports.memory.translation import (
    correct_command,
    disagree_command,
    history_command,
    history_result,
    recall_command,
    recall_result,
    remember_command,
    resolve_command,
)
from cairn.transports.v1.auth import screen_addressing
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.translation import (
    _scope,
    ingest_result,
    invalidate_result,
    promote_result,
    success_envelope,
    validated,
)
from cairn.transports.v1.wire import RULE_IDEMPOTENCY_KEY_MALFORMED, WireModel

if TYPE_CHECKING:
    from cairn.authority.memory import CairnMemory


@dataclass(frozen=True, slots=True)
class Handler:
    translate: Callable[[dict[str, object]], Any]
    invoke: Callable[[Actor, Any, UUID | None, UUID], Any]
    render: Callable[[Any], WireModel]


class MemoryDispatch:
    def __init__(
        self,
        *,
        authority: CairnAuthority,
        memory: "CairnMemory",
        sessions: CairnSessions,
        proposals: CairnProposals,
        diagnostics: CairnDiagnostics,
        product_version: str,
        contract_digest: str,
        mcp_contract_digest: str,
        transactions: CatalogueTransactions,
        screen: SecretScreen,
        data_path: Path,
        write_gate: anyio.Lock,
    ) -> None:
        self.transactions = transactions
        self.screen = screen
        self.data_path = data_path
        self.write_gate = write_gate

        def diagnostic_command(body: dict[str, object]) -> Diagnose:
            request = validated(DiagnoseRequest, body)
            return Diagnose(
                _scope(request.scope, "scope"), Classification(request.classification)
            )

        def diagnostic_body(value: DiagnosticSnapshot) -> DiagnoseBody:
            document = memory_value(value)
            assert isinstance(document, dict)
            return DiagnoseBody.model_validate(
                {
                    **document,
                    "product_version": product_version,
                    "contract_digest": contract_digest,
                    "mcp_contract_digest": mcp_contract_digest,
                }
            )

        self.handlers = {
            "suggest": Handler(
                lambda b: suggestion_command(validated(SuggestRequest, b)),
                lambda a, c, k, cid: self._suggest(memory, a, c, cid),
                suggestion_result,
            ),
            "diagnose": Handler(
                diagnostic_command,
                lambda a, c, k, cid: diagnostics.diagnose(a, c, correlation_id=cid),
                diagnostic_body,
            ),
            "remember": Handler(
                lambda b: remember_command(validated(RememberRequest, b)),
                lambda a, c, k, cid: authority.ingest(
                    a,
                    c,
                    idempotency_key=_key(k),
                    correlation_id=cid,
                    reauthorise_at_commit=True,
                ),
                ingest_result,
            ),
            "recall": Handler(
                lambda b: recall_command(validated(RecallRequest, b)),
                lambda a, c, k, cid: memory.recall(a, c, correlation_id=cid),
                recall_result,
            ),
            "history": Handler(
                lambda b: history_command(validated(HistoryRequest, b)),
                lambda a, c, k, cid: memory.history(a, c, correlation_id=cid),
                history_result,
            ),
            "disagree": Handler(
                lambda b: disagree_command(validated(DisagreeRequest, b)),
                lambda a, c, k, cid: memory.disagree(
                    a, c, idempotency_key=_key(k), correlation_id=cid
                ),
                lambda value: RelationshipResult(
                    relationship_id=str(value.relationship_id)
                ),
            ),
            "resolve": Handler(
                lambda b: resolve_command(validated(ResolveRequest, b)),
                lambda a, c, k, cid: memory.resolve(
                    a, c, idempotency_key=_key(k), correlation_id=cid
                ),
                lambda value: RelationshipResult(
                    relationship_id=str(value.relationship_id)
                ),
            ),
            "correct": Handler(
                lambda b: correct_command(validated(CorrectRequest, b)),
                lambda a, c, k, cid: authority.invalidate(
                    a,
                    c[0],
                    idempotency_key=_key(k),
                    correlation_id=cid,
                    reauthorise_at_commit=True,
                    expected_scope=c[1],
                ),
                invalidate_result,
            ),
        }
        for name, method in (
            ("session-open", sessions.open),
            ("turn-begin", sessions.begin),
            ("turn-prepare", sessions.prepare),
            ("turn-commit", sessions.commit),
            ("turn-abandon", sessions.abandon),
            ("session-read", sessions.read),
            ("visit-issue", sessions.issue_visit),
            ("visit-acknowledge", sessions.acknowledge_visit),
        ):
            entry = BY_TOOL[name]
            model = entry.request
            assert model is not None and issubclass(model, SessionRequest)
            self.handlers[name] = self._session_handler(name, model, method)

        for name, proposal_method in (
            ("propose", proposals.propose),
            ("proposal-read", proposals.read),
            ("proposal-list", proposals.list),
            ("proposal-accept", proposals.accept),
            ("proposal-reject", proposals.reject),
        ):
            self.handlers[name] = self._proposal_handler(name, proposal_method)

    def _proposal_handler(self, name: str, method: Callable[..., Any]) -> Handler:
        entry = BY_TOOL[name]
        model = entry.request
        assert model is not None and issubclass(model, ProposalContext)

        def invoke(actor: Actor, translated: Any, key: UUID | None, cid: UUID) -> Any:
            if entry.mutation and (type(key) is not UUID or key.variant != RFC_4122):
                raise WireRejection(
                    400, RULE_IDEMPOTENCY_KEY_MALFORMED, "idempotency_key"
                )
            expected, command = translated
            with read_connection(self.data_path) as connection:
                actual = instance_id(fetch_from(connection))
            if actual != str(expected):
                return self.transactions.reject(
                    instance_denial_draft(
                        actual,
                        actor,
                        f"memory-{name}",
                        "proposal_authorisation_denied",
                        cid,
                        action_kind=ActionKind.DATA,
                    ),
                    StableFailure(
                        FailureCode.AUTHORISATION_DENIED,
                        "The requested operation is not authorised.",
                        cid,
                        RetryClass.NEVER,
                    ),
                )
            if entry.mutation:
                return method(
                    actor, command, idempotency_key=_key(key), correlation_id=cid
                )
            return method(actor, command, correlation_id=cid)

        return Handler(
            lambda body: proposal_command(validated(model, body)),
            invoke,
            promote_result
            if name == "proposal-accept"
            else lambda value: entry.result.model_validate(memory_value(value)),
        )

    async def audit_proposal_rejection(
        self, actor: Actor, correlation_id: UUID, fingerprint: bytes, name: str
    ) -> None:
        assert name in PROPOSAL_TOOL_NAMES
        await self._audit_wire_rejection(actor, correlation_id, fingerprint, name)

    def _suggest(
        self,
        memory: "CairnMemory",
        actor: Actor,
        translated: tuple[UUID, Suggest],
        cid: UUID,
    ) -> SuggestionResult | Rejected:
        expected, command = translated
        with read_connection(self.data_path) as connection:
            actual = instance_id(fetch_from(connection))
        if actual != str(expected):
            return self.transactions.reject(
                instance_denial_draft(
                    actual,
                    actor,
                    "memory-suggest",
                    "authorisation_denied",
                    cid,
                    action_kind=ActionKind.DATA,
                ),
                StableFailure(
                    FailureCode.AUTHORISATION_DENIED,
                    "The requested operation is not authorised.",
                    cid,
                    RetryClass.NEVER,
                ),
            )
        return memory.suggest(actor, command, correlation_id=cid)

    async def audit_suggestion_rejection(
        self, actor: Actor, correlation_id: UUID, fingerprint: bytes
    ) -> None:
        await self._audit_wire_rejection(actor, correlation_id, fingerprint, "suggest")

    def _session_handler(
        self, name: str, model: type[SessionRequest], method: Callable[..., Any]
    ) -> Handler:
        def invoke(actor: Actor, translated: Any, key: UUID | None, cid: UUID) -> Any:
            expected, command = translated
            with read_connection(self.data_path) as connection:
                actual = instance_id(fetch_from(connection))
            if actual != str(expected):
                return self.transactions.reject(
                    instance_denial_draft(
                        actual,
                        actor,
                        f"memory-{name}",
                        "session_authorisation_denied",
                        cid,
                        action_kind=ActionKind.DATA,
                    ),
                    StableFailure(
                        FailureCode.AUTHORISATION_DENIED,
                        "The requested operation is not authorised.",
                        cid,
                        RetryClass.NEVER,
                    ),
                )
            if name == "session-read":
                return method(actor, command, correlation_id=cid)
            return method(actor, command, idempotency_key=_key(key), correlation_id=cid)

        return Handler(
            lambda body: session_command(validated(model, body)), invoke, session_result
        )

    async def audit_diagnostic_rejection(
        self, actor: Actor, correlation_id: UUID, fingerprint: bytes
    ) -> None:
        await self._audit_wire_rejection(actor, correlation_id, fingerprint, "diagnose")

    async def audit_session_rejection(
        self, actor: Actor, correlation_id: UUID, fingerprint: bytes, name: str
    ) -> None:
        assert name in SESSION_TOOL_NAMES
        await self._audit_wire_rejection(actor, correlation_id, fingerprint, name)

    async def _audit_wire_rejection(
        self, actor: Actor, correlation_id: UUID, fingerprint: bytes, name: str
    ) -> None:
        """Durably record an identified diagnostic refusal before rendering it.

        Wire admission has not established a safe realm or scope. Use the local
        instance chain, verified actor and a fixed reason; never copy the body,
        caller-authored field paths or exception prose. Audit errors propagate
        to the transport's existing closed error handler.
        """

        def append() -> None:
            with read_connection(self.data_path) as connection:
                identity = instance_id(fetch_from(connection))
            self.transactions.append_audit(
                instance_denial_draft(
                    identity,
                    actor,
                    f"memory-{name}",
                    "invalid_request",
                    correlation_id,
                    action_kind=ActionKind.DATA,
                    safe_request_fingerprint=fingerprint,
                )
            )

        await anyio.to_thread.run_sync(append)

    async def run(
        self,
        name: str,
        body: dict[str, object],
        actor: Actor,
        key: UUID | None,
        correlation_id: UUID,
    ) -> WireModel | Rejected:
        handler = self.handlers[name]
        command = handler.translate(body)
        mutation = BY_TOOL[name].mutation

        def execute() -> Any:
            denied = screen_addressing(
                body,
                screen=self.screen,
                actor=actor,
                transactions=self.transactions,
                data_path=self.data_path,
                action_code=f"memory-{name}",
                action_kind=ActionKind.DATA,
                correlation_id=correlation_id,
            )
            return (
                denied
                if denied is not None
                else handler.invoke(actor, command, key, correlation_id)
            )

        if mutation:
            async with self.write_gate:
                value = await anyio.to_thread.run_sync(execute)
        else:
            value = await anyio.to_thread.run_sync(execute)
        if isinstance(value, Rejected):
            return value
        return (
            success_envelope(value, handler.render(value.value))
            if mutation
            else handler.render(value)
        )


def _key(key: UUID | None) -> UUID:
    assert key is not None
    return key
