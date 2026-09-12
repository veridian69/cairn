"""HTTP boundary for the provider-neutral Cairn memory client."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

import httpx

from cairn.authority.custody import MAX_BATCH_FACTS, CustodyValueError, validate_reason
from cairn.authority.retrieval import MAX_BUDGET_BYTES, MAX_QUERY_BYTES
from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import canonical_timestamp
from cairn.client.diagnostics import (
    _document,
    check_expectations,
    diagnostic_result,
    safe_failure,
)
from cairn.client.errors import (
    FailureMetadata,
    MemoryOperationFailure,
    RecallFailure,
    RememberFailure,
)
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DurableObservation,
    PersistenceReceipt,
    PersistenceStatus,
    RecalledMemory,
    freeze_object,
)
from cairn.client.validation import (
    validate_correction_success,
    validate_history,
    validate_recall,
    validate_remember_success,
)
from cairn.transports.v1.wire import FailureEnvelope

if TYPE_CHECKING:
    from cairn.client.proposal_types import (
        FactsPromoted,
        ProposalMutation,
        ProposalPage,
        ProposalRecorded,
        ProposalSnapshot,
    )
    from cairn.client.session_types import SessionOperationResult, SessionSnapshot
    from cairn.client.suggestion_types import SuggestedMemory
    from cairn.client.types import ModelTurn


def _scope_body(scope: Scope) -> dict[str, object]:
    return {
        "realm": scope.realm,
        "segments": [
            {"kind": segment.kind, "identifier": segment.identifier}
            for segment in scope.segments
        ],
    }


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else canonical_timestamp(value)


def _validate_history_scopes(document: dict[str, object], scope: Scope) -> None:
    """Check structurally validated records against the host's requested scope.

    Corrections have no scope of their own; their disclosed fact endpoints are
    checked here. Read clearance is server-owned and is not the write class.
    """
    requested = cast(list[object], _scope_body(scope)["segments"])
    for collection in ("facts", "disagreements", "resolutions"):
        for record in cast(list[dict[str, object]], document[collection]):
            returned = cast(dict[str, object], record["scope"])
            segments = cast(list[object], returned["segments"])
            if (
                returned["realm"] != scope.realm
                or segments != requested[: len(segments)]
            ):
                raise ValueError("history_scope_mismatch")


async def _bounded_history_response(
    response: httpx.Response, budget: int
) -> httpx.Response:
    """Bound decoded wire bytes before JSON parsing, without truncating records.

    Record budgets measure canonical UTF-8 JSON. Six wire bytes per budget byte
    accommodates JSON Unicode escaping; 4096 bytes allow bounded envelope and
    formatting overhead. Excessive wire padding is refused, never truncated.
    Failure envelopes use the existing safe-metadata decoder's 16 KiB limit.
    Compression is refused before iteration: HTTPX decodes whole incoming
    chunks before yielding bytes, which would bypass a post-decoding cap.
    """
    if (
        response.headers.get("Content-Encoding", "identity").strip().lower()
        != "identity"
    ):
        raise RecallFailure("history", _invalid_response(response.status_code))
    limit = 6 * budget + 4096 if response.status_code == 200 else 16384
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise RecallFailure("history", _invalid_response(response.status_code))
        body.extend(chunk)
    return httpx.Response(response.status_code, content=bytes(body))


async def _bounded_small_response(response: httpx.Response) -> httpx.Response:
    """Enforce the 16 KiB metadata/receipt wire limit before buffering/decoding."""
    if (
        response.headers.get("Content-Encoding", "identity").strip().lower()
        != "identity"
    ):
        raise ValueError("invalid_response")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > 16384:
            raise ValueError("invalid_response")
        body.extend(chunk)
    return httpx.Response(response.status_code, content=bytes(body))


def _invalid_response(status_code: int | None = 200) -> FailureMetadata:
    return FailureMetadata(
        code="invalid_response",
        message="Cairn returned an invalid memory response.",
        retry="never",
        correlation_id=None,
        status_code=status_code,
    )


def _failure_metadata(response: httpx.Response) -> FailureMetadata:
    try:
        envelope = FailureEnvelope.model_validate(response.json())
    except ValueError:
        return FailureMetadata(
            code="http_error",
            message="Cairn rejected the memory request.",
            retry="never",
            correlation_id=None,
            status_code=response.status_code,
        )
    failure = envelope.failure
    return FailureMetadata(
        code=failure.code,
        message=failure.message,
        retry=failure.retry,
        correlation_id=failure.correlation_id,
        status_code=response.status_code,
    )


def _transport_failure() -> FailureMetadata:
    return FailureMetadata(
        code="transport_error",
        message="Cairn memory transport failed.",
        retry="explicit",
        correlation_id=None,
        status_code=None,
    )


def _local_failure(code: str, message: str) -> FailureMetadata:
    return FailureMetadata(
        code=code,
        message=message,
        retry="never",
        correlation_id=None,
        status_code=None,
    )


def _validate_base_url(url: httpx.URL) -> None:
    if not url.is_absolute_url or url.host is None:
        raise ValueError("http client requires an absolute base_url")
    if url.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(url.host).is_loopback
    except ValueError:
        loopback = False
    if url.scheme != "http" or not loopback:
        raise ValueError("Cairn memory requires TLS outside numeric loopback")


class MemoryClient:
    """Calls memory/v1 with credentials owned solely by the injected client."""

    __slots__ = (
        "_base_url",
        "_classification",
        "_http",
        "_retry_context",
        "_scope",
        "_scope_json",
        "_expected_instance_id",
        "_session_principal_id",
    )

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        scope: Scope,
        classification: Classification,
        expected_instance_id: UUID | None = None,
    ) -> None:
        if type(scope) is not Scope:
            raise TypeError("scope must be Scope")
        if type(classification) is not Classification:
            raise TypeError("classification must be Classification")
        _validate_base_url(http.base_url)
        self._http = http
        self._base_url = httpx.URL(http.base_url)
        self._scope = scope
        self._scope_json = _scope_body(scope)
        self._classification = classification
        self._retry_context = object()
        if expected_instance_id is not None and type(expected_instance_id) is not UUID:
            raise TypeError("expected_instance_id must be UUID or None")
        self._expected_instance_id = expected_instance_id
        self._session_principal_id: UUID | None = None

    @property
    def expected_instance_id(self) -> UUID | None:
        return self._expected_instance_id

    async def suggest(
        self,
        *,
        observation: str | None = None,
        fact_ids: tuple[UUID, ...] = (),
        budget: int = 16384,
        limit: int = 8,
    ) -> SuggestedMemory:
        """Read validated immutable evidence; never persist or execute a suggestion."""
        from cairn.client.suggestion_io import request_suggestions

        return await request_suggestions(
            self, observation=observation, fact_ids=fact_ids, budget=budget, limit=limit
        )

    async def propose(
        self,
        proposal_id: UUID,
        *,
        source_fact_id: UUID,
        target_scope: Scope,
        reason: str,
        idempotency_key: UUID,
    ) -> ProposalMutation[ProposalRecorded]:
        """Persist an attributed proposal, without publishing its source."""
        from cairn.client.proposal_io import request_proposal

        return cast(
            "ProposalMutation[ProposalRecorded]",
            await request_proposal(
                self,
                "propose",
                proposal_id=proposal_id,
                source_fact_id=source_fact_id,
                target_scope=target_scope,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )

    async def proposal_read(self, proposal_id: UUID) -> ProposalSnapshot:
        from cairn.client.proposal_io import request_proposal

        return cast(
            "ProposalSnapshot",
            await request_proposal(self, "proposal-read", proposal_id=proposal_id),
        )

    async def proposal_list(
        self, *, limit: int = 50, after: UUID | None = None
    ) -> ProposalPage:
        """Read one exact source; pagination is not a snapshot across calls."""
        from cairn.client.proposal_io import request_proposal

        return cast(
            "ProposalPage",
            await request_proposal(self, "proposal-list", limit=limit, after=after),
        )

    async def proposal_accept(
        self,
        proposal_id: UUID,
        *,
        evidence_id: UUID,
        target_classification: Classification,
        idempotency_key: UUID,
    ) -> ProposalMutation[FactsPromoted]:
        """Explicit single-source promotion; replay only with the caller's key."""
        from cairn.client.proposal_io import request_proposal

        return cast(
            "ProposalMutation[FactsPromoted]",
            await request_proposal(
                self,
                "proposal-accept",
                proposal_id=proposal_id,
                evidence_id=evidence_id,
                target_classification=target_classification,
                idempotency_key=idempotency_key,
            ),
        )

    async def proposal_reject(
        self, proposal_id: UUID, *, reason: str, idempotency_key: UUID
    ) -> ProposalMutation[ProposalRecorded]:
        """Record a decision, without invalidating the source."""
        from cairn.client.proposal_io import request_proposal

        return cast(
            "ProposalMutation[ProposalRecorded]",
            await request_proposal(
                self,
                "proposal-reject",
                proposal_id=proposal_id,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )

    async def open_session(
        self, session_id: UUID, *, idempotency_key: UUID
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        result = await request_session(
            self,
            "session-open",
            session_id,
            idempotency_key=idempotency_key,
            fields={"classification": self.classification.value},
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def begin_turn(
        self,
        session_id: UUID,
        turn_id: UUID,
        *,
        attempt_id: UUID,
        replaces_turn_id: UUID | None = None,
        idempotency_key: UUID,
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        if replaces_turn_id is not None and type(replaces_turn_id) is not UUID:
            raise TypeError("replaces_turn_id must be UUID or None")
        result = await request_session(
            self,
            "turn-begin",
            session_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            fields={
                "replaces_turn_id": None
                if replaces_turn_id is None
                else str(replaces_turn_id)
            },
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def prepare_turn(
        self,
        session_id: UUID,
        turn_id: UUID,
        turn: ModelTurn,
        *,
        attempt_id: UUID,
        idempotency_key: UUID,
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult
        from cairn.client.types import ModelTurn

        if type(turn) is not ModelTurn:
            raise TypeError("turn must be ModelTurn")
        if (
            len(turn.response.encode("utf-8")) > 32768
            or len(turn.observations) > 8
            or any(len(o.body.encode("utf-8")) > 4096 for o in turn.observations)
        ):
            raise MemoryOperationFailure(
                "turn-prepare",
                _local_failure(
                    "invalid_preparation", "The preparation exceeds session limits."
                ),
            )
        result = await request_session(
            self,
            "turn-prepare",
            session_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            fields={
                "response": turn.response,
                "observations": [
                    {
                        "body": o.body,
                        "valid_from": _timestamp(o.valid_from),
                        "valid_to": _timestamp(o.valid_to),
                        "observed_at": _timestamp(o.observed_at),
                    }
                    for o in turn.observations
                ],
            },
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def commit_turn(
        self, session_id: UUID, turn_id: UUID, *, idempotency_key: UUID
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        result = await request_session(
            self,
            "turn-commit",
            session_id,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def abandon_turn(
        self, session_id: UUID, turn_id: UUID, *, reason: str, idempotency_key: UUID
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        validate_reason(reason)
        result = await request_session(
            self,
            "turn-abandon",
            session_id,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            fields={"reason": reason},
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def read_session(
        self, session_id: UUID, *, turn_id: UUID | None = None
    ) -> SessionSnapshot:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionSnapshot

        result = await request_session(
            self, "session-read", session_id, turn_id=turn_id
        )
        assert isinstance(result, SessionSnapshot)
        return result

    async def issue_visit(
        self, session_id: UUID, *, idempotency_key: UUID
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        result = await request_session(
            self, "visit-issue", session_id, idempotency_key=idempotency_key
        )
        assert isinstance(result, SessionOperationResult)
        return result

    async def acknowledge_visit(
        self, session_id: UUID, visit_id: UUID, *, idempotency_key: UUID
    ) -> SessionOperationResult:
        from cairn.client.session_io import request_session
        from cairn.client.session_types import SessionOperationResult

        if type(visit_id) is not UUID:
            raise TypeError("visit_id must be UUID")
        result = await request_session(
            self,
            "visit-acknowledge",
            session_id,
            idempotency_key=idempotency_key,
            fields={"visit_id": str(visit_id)},
        )
        assert isinstance(result, SessionOperationResult)
        return result

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def classification(self) -> Classification:
        return self._classification

    async def diagnose(
        self,
        *,
        expected_instance_id: UUID | None = None,
        expected_contract_digest: str | None = None,
        expected_mcp_contract_digest: str | None = None,
    ) -> ConnectionDiagnostics:
        """Check memory identity and a current exact-scope grant snapshot.

        READY means a valid authenticated memory handshake with some applicable
        authority, not write readiness. Screening and re-authorisation still apply.
        """
        if expected_instance_id is not None and type(expected_instance_id) is not UUID:
            raise TypeError("expected_instance_id must be UUID or None")
        for name, value in (
            ("expected_contract_digest", expected_contract_digest),
            ("expected_mcp_contract_digest", expected_mcp_contract_digest),
        ):
            if value is not None and (
                type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self._http.base_url != self._base_url:
            return ConnectionDiagnostics(
                ConnectionStatus.UNREACHABLE,
                failure=_local_failure(
                    "client_context_changed", "Cairn memory client context changed."
                ),
            )
        try:
            async with self._http.stream(
                "POST",
                self._base_url.join("/memory/v1/diagnose"),
                json={
                    "scope": self._scope_json,
                    "classification": self._classification.value,
                },
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as streamed:
                response = await _bounded_small_response(streamed)
        except ValueError:
            return ConnectionDiagnostics(
                ConnectionStatus.INVALID_RESPONSE, failure=_invalid_response(None)
            )
        except httpx.HTTPError:
            return ConnectionDiagnostics(
                ConnectionStatus.UNREACHABLE, failure=_transport_failure()
            )
        result = diagnostic_result(
            response,
            scope=self._scope,
            scope_json=self._scope_json,
            classification=self._classification,
        )
        return check_expectations(
            result,
            expected_instance_id,
            expected_contract_digest,
            expected_mcp_contract_digest,
        )

    async def history(self, fact_id: UUID, *, budget: int = 16384) -> RecalledMemory:
        """Read bounded history in this client's exact host-owned scope."""
        if type(fact_id) is not UUID or fact_id.version != 4:
            raise RecallFailure(
                "history",
                _local_failure("invalid_fact_id", "History fact identity is invalid."),
            )
        if type(budget) is not int or not 1 <= budget <= MAX_BUDGET_BYTES:
            raise RecallFailure(
                "history",
                _local_failure("invalid_budget", "History budget is invalid."),
            )
        if self._http.base_url != self._base_url:
            raise RecallFailure(
                "history",
                _local_failure(
                    "client_context_changed", "Cairn memory client context changed."
                ),
            )
        try:
            async with self._http.stream(
                "POST",
                self._base_url.join("/memory/v1/history"),
                headers={"Accept-Encoding": "identity"},
                json={
                    "scope": self._scope_json,
                    "fact_id": str(fact_id),
                    "budget": budget,
                },
                follow_redirects=False,
            ) as streamed:
                response = await _bounded_history_response(streamed, budget)
        except httpx.HTTPError:
            raise RecallFailure("history", _transport_failure()) from None
        if response.status_code != 200:
            raise RecallFailure("history", safe_failure(response))
        try:
            decoded = validate_history(response.json(), budget=budget)
            _validate_history_scopes(decoded, self._scope)
            return RecalledMemory(freeze_object(decoded))
        except (ValueError, TypeError, RecursionError):
            raise RecallFailure("history", _invalid_response()) from None

    async def recall(
        self, query: str, *, budget: int = 16384, relevant_only: bool = False
    ) -> RecalledMemory:
        from cairn.client.recall_io import read_recall

        if type(relevant_only) is not bool:
            raise RecallFailure(
                "recall",
                _local_failure(
                    "invalid_relevance_filter", "Recall relevance filter is invalid."
                ),
            )
        if type(query) is not str:
            raise RecallFailure(
                "recall", _local_failure("invalid_query", "Recall query is invalid.")
            )
        try:
            query_size = len(query.encode("utf-8"))
        except UnicodeError:
            query_size = 0
        if not 1 <= query_size <= MAX_QUERY_BYTES:
            raise RecallFailure(
                "recall", _local_failure("invalid_query", "Recall query is invalid.")
            )
        if type(budget) is not int or not 1 <= budget <= MAX_BUDGET_BYTES:
            raise RecallFailure(
                "recall", _local_failure("invalid_budget", "Recall budget is invalid.")
            )
        if self._http.base_url != self._base_url:
            raise RecallFailure(
                "recall",
                _local_failure(
                    "client_context_changed", "Cairn memory client context changed."
                ),
            )
        request: dict[str, object] = {
            "scope": self._scope_json,
            "query": query,
            "budget": budget,
        }
        if relevant_only:
            request["relevant_only"] = True
        status_code: int | None = None
        try:
            async with self._http.stream(
                "POST",
                self._base_url.join("/memory/v1/recall"),
                json=request,
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                status_code = response.status_code
                document = await read_recall(response, budget=budget)
            decoded = validate_recall(document, budget=budget)
            return RecalledMemory(freeze_object(decoded))
        except httpx.HTTPError:
            raise RecallFailure("recall", _transport_failure()) from None
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise RecallFailure("recall", _invalid_response(status_code)) from None

    async def disagree(
        self,
        left_fact_id: UUID,
        right_fact_id: UUID,
        *,
        reason: str,
        idempotency_key: UUID,
    ) -> PersistenceReceipt:
        """Record an explicit disagreement in this client's fixed scope/classification."""
        from cairn.client.disagreement_io import disagree

        return await disagree(
            self,
            left_fact_id,
            right_fact_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    async def correct(
        self,
        fact_ids: tuple[UUID, ...],
        *,
        reason: str,
        superseded_by: UUID | None = None,
        idempotency_key: UUID,
    ) -> PersistenceReceipt:
        """Persist an explicit correction restricted to this host's exact scope."""
        try:
            if (
                type(fact_ids) is not tuple
                or not 1 <= len(fact_ids) <= MAX_BATCH_FACTS
                or any(
                    type(identity) is not UUID or identity.version != 4
                    for identity in fact_ids
                )
                or len(set(fact_ids)) != len(fact_ids)
                or type(idempotency_key) is not UUID
                or (
                    superseded_by is not None
                    and (type(superseded_by) is not UUID or superseded_by.version != 4)
                )
            ):
                raise ValueError("invalid_correction")
            validate_reason(reason)
        except (CustodyValueError, ValueError, TypeError):
            raise MemoryOperationFailure(
                "correct",
                _local_failure(
                    "invalid_correction", "The correction request is invalid."
                ),
            ) from None
        if self._http.base_url != self._base_url:
            raise MemoryOperationFailure(
                "correct",
                _local_failure(
                    "client_context_changed", "Cairn memory client context changed."
                ),
            )
        try:
            async with self._http.stream(
                "POST",
                self._base_url.join("/memory/v1/correct"),
                json={
                    "scope": self._scope_json,
                    "fact_ids": [str(identity) for identity in fact_ids],
                    "reason": reason,
                    "superseded_by": None
                    if superseded_by is None
                    else str(superseded_by),
                },
                headers={
                    "Idempotency-Key": str(idempotency_key),
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
            ) as streamed:
                response = await _bounded_small_response(streamed)
        except ValueError:
            raise MemoryOperationFailure("correct", _invalid_response(None)) from None
        except httpx.HTTPError:
            raise MemoryOperationFailure("correct", _transport_failure()) from None
        if response.status_code != 200:
            raise MemoryOperationFailure("correct", safe_failure(response))
        try:
            envelope = validate_correction_success(
                _document(response), fact_ids=fact_ids, realm=self._scope.realm
            )
        except (ValueError, TypeError, RecursionError):
            raise MemoryOperationFailure("correct", _invalid_response()) from None
        return PersistenceReceipt(
            PersistenceStatus(cast(str, envelope["outcome"])),
            idempotency_key,
            freeze_object(envelope["result"]),
            freeze_object(envelope["mutation_receipt"]),
            freeze_object(envelope["audit_receipt"]),
        )

    async def remember(
        self,
        observations: tuple[DurableObservation, ...],
        *,
        idempotency_key: UUID,
        evidence_payload: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PersistenceReceipt:
        if type(observations) is not tuple or not all(
            type(item) is DurableObservation for item in observations
        ):
            raise TypeError("observations must be a tuple of DurableObservation")
        if not observations:
            return PersistenceReceipt(PersistenceStatus.SKIPPED, None)
        if type(idempotency_key) is not UUID:
            raise TypeError("idempotency_key must be UUID")
        if evidence_payload is not None and type(evidence_payload) is not str:
            raise TypeError("evidence_payload must be str or None")
        if metadata is not None and type(metadata) is not dict:
            raise TypeError("metadata must be dict or None")
        if self._http.base_url != self._base_url:
            raise RememberFailure(
                "remember",
                _local_failure(
                    "client_context_changed", "Cairn memory client context changed."
                ),
            )

        observed_values = {item.observed_at for item in observations}
        if len(observed_values) != 1:
            raise ValueError("one remember batch requires one shared observed_at")
        observed_at = next(iter(observed_values))
        request: dict[str, object] = {
            "scope": self._scope_json,
            "classification": self._classification.value,
            "facts": [
                {
                    "body": item.body,
                    "valid_from": _timestamp(item.valid_from),
                    "valid_to": _timestamp(item.valid_to),
                }
                for item in observations
            ],
        }
        if observed_at is not None:
            request["observed_at"] = _timestamp(observed_at)
        if evidence_payload is not None:
            request["evidence_payload"] = evidence_payload
        if metadata is not None:
            request["metadata"] = metadata
        try:
            async with self._http.stream(
                "POST",
                self._base_url.join("/memory/v1/remember"),
                json=request,
                headers={
                    "Idempotency-Key": str(idempotency_key),
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
            ) as streamed:
                response = await _bounded_small_response(streamed)
        except UnicodeError:
            raise RememberFailure(
                "remember",
                _local_failure(
                    "invalid_observations", "Durable observations are invalid."
                ),
            ) from None
        except httpx.HTTPError:
            raise RememberFailure("remember", _transport_failure()) from None
        except ValueError:
            raise RememberFailure("remember", _invalid_response()) from None
        if response.status_code != 200:
            raise RememberFailure("remember", _failure_metadata(response))
        try:
            envelope = validate_remember_success(
                _document(response),
                fact_count=len(observations),
                realm=self._scope.realm,
                evidence_supplied=evidence_payload is not None,
            )
        except (ValueError, TypeError):
            raise RememberFailure("remember", _invalid_response()) from None
        status = PersistenceStatus(cast(str, envelope["outcome"]))
        return PersistenceReceipt(
            status=status,
            idempotency_key=idempotency_key,
            result=freeze_object(envelope["result"]),
            mutation_receipt=freeze_object(envelope["mutation_receipt"]),
            audit_receipt=freeze_object(envelope["audit_receipt"]),
        )
