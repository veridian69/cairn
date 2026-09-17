"""Fixed-scope proposal HTTP, strict streaming and explicit receipt replay."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from uuid import RFC_4122, UUID

import httpx

from cairn.authority.custody import CustodyValueError, validate_reason
from cairn.catalogue.audit import AuditValueError, Classification, Scope
from cairn.catalogue.sqlite import CatalogueStorageError
from cairn.client.diagnostics import _unique_object, safe_failure
from cairn.client.errors import MemoryOperationFailure
from cairn.client.proposal_types import (
    FactsPromoted,
    ProposalDecision,
    ProposalMutation,
    ProposalPage,
    ProposalRecorded,
    ProposalSnapshot,
)
from cairn.client.proposal_validation import (
    PROPOSAL_ITEM_BYTES,
    PROPOSAL_MUTATION_BYTES,
    acceptance_digest,
    legal_target,
    mutation,
    page,
    recorded_digest,
    snapshot,
)
from cairn.client.types import ConnectionStatus

if TYPE_CHECKING:
    from cairn.client.memory import MemoryClient

type ProposalOperation = Literal[
    "propose", "proposal-read", "proposal-list", "proposal-accept", "proposal-reject"
]
type ProposalResponse = (
    ProposalSnapshot
    | ProposalPage
    | ProposalMutation[ProposalRecorded]
    | ProposalMutation[FactsPromoted]
)


async def _exchange(
    client: MemoryClient,
    name: ProposalOperation,
    body: dict[str, object],
    key: UUID | None,
    cap: int,
) -> object:
    from cairn.client.memory import _invalid_response, _transport_failure

    headers = {"Accept-Encoding": "identity"}
    if key is not None:
        headers["Idempotency-Key"] = str(key)
    try:
        async with client._http.stream(
            "POST",
            client._base_url.join(f"/memory/v1/{name}"),
            json=body,
            headers=headers,
            follow_redirects=False,
        ) as response:
            limit = cap if response.status_code == 200 else 16384
            if (
                response.headers.get("Content-Encoding", "identity").strip().lower()
                != "identity"
            ):
                raise ValueError("compressed_response")
            lengths = response.headers.get_list("Content-Length")
            declared = lengths[0] if lengths else None
            if len(lengths) > 1 or (
                declared is not None
                and (
                    len(declared) > len(str(limit))
                    or not declared.isascii()
                    or not declared.isdecimal()
                    or int(declared) > limit
                )
            ):
                raise ValueError("invalid_length")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > limit:
                    raise ValueError("oversized_response")
                data.extend(chunk)
            if declared is not None and len(data) != int(declared):
                raise ValueError("incorrect_length")
            if response.status_code != 200:
                raise MemoryOperationFailure(
                    name,
                    safe_failure(
                        httpx.Response(response.status_code, content=bytes(data))
                    ),
                )
        return json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise MemoryOperationFailure(name, _invalid_response()) from None
    except httpx.HTTPError:
        raise MemoryOperationFailure(name, _transport_failure()) from None


async def request_proposal(
    client: MemoryClient,
    name: ProposalOperation,
    *,
    proposal_id: UUID | None = None,
    source_fact_id: UUID | None = None,
    target_scope: Scope | None = None,
    reason: str | None = None,
    evidence_id: UUID | None = None,
    target_classification: Classification | None = None,
    idempotency_key: UUID | None = None,
    limit: int = 50,
    after: UUID | None = None,
) -> ProposalResponse:
    from cairn.client.memory import _invalid_response, _local_failure, _scope_body

    if client.expected_instance_id is None:
        raise MemoryOperationFailure(
            name,
            _local_failure(
                "expected_instance_required", "An expected instance is required."
            ),
        )
    try:
        for uid in (
            client.expected_instance_id,
            proposal_id,
            source_fact_id,
            evidence_id,
            idempotency_key,
            after,
        ):
            if uid is not None and type(uid) is not UUID:
                raise ValueError("invalid_identity")
        mutation_operation = name in {"propose", "proposal-accept", "proposal-reject"}
        if idempotency_key is not None and idempotency_key.variant != RFC_4122:
            raise ValueError("invalid_idempotency_key")
        if mutation_operation != (idempotency_key is not None) or (
            name != "proposal-list" and proposal_id is None
        ):
            raise ValueError("invalid_request")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_limit")
        if name in {"propose", "proposal-reject"}:
            if reason is None:
                raise ValueError("missing_reason")
            validate_reason(reason)
        if name == "propose":
            if source_fact_id is None or target_scope is None:
                raise ValueError("missing_source")
            legal_target(client.scope, target_scope)
        if name == "proposal-accept" and (
            evidence_id is None or type(target_classification) is not Classification
        ):
            raise ValueError("invalid_acceptance")
    except (ValueError, TypeError, OverflowError, CustodyValueError, AuditValueError):
        raise MemoryOperationFailure(
            name, _local_failure("invalid_request", "The proposal request is invalid.")
        ) from None

    diagnostics = await client.diagnose(
        expected_instance_id=client.expected_instance_id
    )
    if (
        diagnostics.status is not ConnectionStatus.READY
        or diagnostics.permissions is None
        or not diagnostics.permissions.retrieve
    ):
        raise MemoryOperationFailure(
            name,
            diagnostics.failure
            or _local_failure(
                "authorisation_denied", "The proposal request is not authorised."
            ),
        )
    context: dict[str, object] = {
        "scope": _scope_body(client.scope),
        "expected_instance_id": str(client.expected_instance_id),
    }
    body = dict(context)
    if proposal_id is not None:
        body["proposal_id"] = str(proposal_id)
    try:
        command_digest = None
        accepted_decision: ProposalDecision | None = None
        if name == "proposal-list":
            body.update(limit=limit, after=None if after is None else str(after))
        elif name == "propose":
            assert (
                proposal_id is not None
                and source_fact_id is not None
                and target_scope is not None
                and reason is not None
            )
            body.update(
                source_fact_id=str(source_fact_id),
                target_scope=_scope_body(target_scope),
                reason=reason,
            )
            command_digest = recorded_digest(
                client.scope,
                proposal_id,
                reason,
                source_fact_id=source_fact_id,
                target_scope=target_scope,
            )
        elif name == "proposal-reject":
            assert proposal_id is not None and reason is not None
            body["reason"] = reason
            command_digest = recorded_digest(client.scope, proposal_id, reason)
        elif name == "proposal-accept":
            assert (
                proposal_id is not None
                and evidence_id is not None
                and target_classification is not None
            )
            # Bind immutable proposal context before the write, including replay.
            # A later loss of read authority cannot undo a valid success receipt.
            known = snapshot(
                await _exchange(
                    client, "proposal-read", body, None, PROPOSAL_ITEM_BYTES
                ),
                scope=client.scope,
                proposal_id=proposal_id,
            )
            source_fact_id = known.source_fact_id
            if known.state == "accepted":
                accepted_decision = known.decision
            command_digest = acceptance_digest(
                known, evidence_id, target_classification
            )
            body.update(
                evidence_id=str(evidence_id),
                target_classification=target_classification.value,
            )
        cap = (
            PROPOSAL_MUTATION_BYTES
            if mutation_operation
            else (
                limit * (PROPOSAL_ITEM_BYTES + 1) + 1024
                if name == "proposal-list"
                else PROPOSAL_ITEM_BYTES
            )
        )
        document = await _exchange(client, name, body, idempotency_key, cap)
        if name == "proposal-list":
            return page(document, scope=client.scope, limit=limit, after=after)
        assert proposal_id is not None
        if name == "proposal-read":
            return snapshot(document, scope=client.scope, proposal_id=proposal_id)
        assert command_digest is not None
        return mutation(
            document,
            proposal_id=proposal_id,
            scope=client.scope,
            command_digest=command_digest,
            source_fact_id=source_fact_id if name == "proposal-accept" else None,
            evidence_id=evidence_id,
            accepted_decision=accepted_decision,
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        CustodyValueError,
        AuditValueError,
        CatalogueStorageError,
    ):
        raise MemoryOperationFailure(name, _invalid_response()) from None
