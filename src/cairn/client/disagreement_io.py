"""One explicit disagreement operation with bounded, request-bound receipts.

Digest/context checks detect inconsistent responses, not cryptographic server
authenticity. No authority implementation is imported to describe client values.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, cast
from uuid import RFC_4122, UUID

import httpx

from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn.client.diagnostics import _document, safe_failure
from cairn.client.errors import MemoryOperationFailure
from cairn.client.types import PersistenceReceipt, PersistenceStatus, freeze_object

if TYPE_CHECKING:
    from cairn.client.memory import MemoryClient


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or value.keys() != fields:
        raise ValueError("invalid_receipt")
    return cast(dict[str, object], value)


def _uuid4(value: object) -> None:
    if type(value) is not str:
        raise ValueError("invalid_identity")
    identity = UUID(value)
    if str(identity) != value or identity.version != 4:
        raise ValueError("invalid_identity")


def _digest(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid_digest")


def _receipt(
    value: object, *, command_digest: str, realm: str, key: UUID
) -> PersistenceReceipt:
    freeze_object(
        value
    )  # Reject nonfinite/invalid Unicode values, including nested ones.
    document = _object(
        value, {"outcome", "result", "mutation_receipt", "audit_receipt"}
    )
    if document["outcome"] not in ("committed", "replayed"):
        raise ValueError("invalid_outcome")
    result = _object(document["result"], {"relationship_id"})
    _uuid4(result["relationship_id"])
    mutation = _object(document["mutation_receipt"], {"mutation_id", "command_digest"})
    _uuid4(mutation["mutation_id"])
    if mutation["command_digest"] != command_digest:
        raise ValueError("foreign_command")
    audit = _object(
        document["audit_receipt"],
        {
            "event_id",
            "chain_kind",
            "chain_identity",
            "sequence",
            "recorded_at",
            "event_hash",
        },
    )
    _uuid4(audit["event_id"])
    if audit["chain_kind"] != "realm" or audit["chain_identity"] != realm:
        raise ValueError("foreign_audit")
    sequence, recorded = audit["sequence"], audit["recorded_at"]
    if type(sequence) is not int or sequence < 1 or type(recorded) is not str:
        raise ValueError("invalid_audit")
    if canonical_timestamp(parse_timestamp(recorded)) != recorded:
        raise ValueError("invalid_timestamp")
    _digest(audit["event_hash"])
    return PersistenceReceipt(
        PersistenceStatus(document["outcome"]),
        key,
        freeze_object(result),
        freeze_object(mutation),
        freeze_object(audit),
    )


async def disagree(
    client: MemoryClient,
    left_fact_id: UUID,
    right_fact_id: UUID,
    *,
    reason: str,
    idempotency_key: UUID,
) -> PersistenceReceipt:
    from cairn.client.memory import (
        _bounded_small_response,
        _invalid_response,
        _local_failure,
        _transport_failure,
    )

    try:
        if (
            any(
                type(identity) is not UUID or identity.version != 4
                for identity in (left_fact_id, right_fact_id)
            )
            or left_fact_id == right_fact_id
            or type(idempotency_key) is not UUID
            or idempotency_key.variant != RFC_4122
            or type(reason) is not str
            or not 1 <= len(reason.encode("utf-8")) <= 4096
        ):
            raise ValueError("invalid_request")
    except (ValueError, TypeError):
        raise MemoryOperationFailure(
            "disagree",
            _local_failure("invalid_request", "The disagreement request is invalid."),
        ) from None
    if client._http.base_url != client._base_url:
        raise MemoryOperationFailure(
            "disagree",
            _local_failure(
                "client_context_changed", "Cairn memory client context changed."
            ),
        )
    body = {
        "scope": asdict(client.scope),
        "classification": client._classification.value,
        "left_fact_id": str(left_fact_id),
        "right_fact_id": str(right_fact_id),
        "reason": reason,
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            {
                "schema": "cairn.memory/v1",
                "operation": "memory-disagree",
                "command": body,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    try:
        async with client._http.stream(
            "POST",
            client._base_url.join("/memory/v1/disagree"),
            json=body,
            headers={
                "Idempotency-Key": str(idempotency_key),
                "Accept-Encoding": "identity",
            },
            follow_redirects=False,
        ) as streamed:
            response = await _bounded_small_response(streamed)
    except ValueError:
        raise MemoryOperationFailure("disagree", _invalid_response(None)) from None
    except httpx.HTTPError:
        raise MemoryOperationFailure("disagree", _transport_failure()) from None
    try:
        document = _document(response)
        if response.status_code != 200:
            freeze_object(document)
            envelope = _object(document, {"failure"})
            failure = envelope["failure"]
            fields = {"code", "message", "retry", "correlation_id"}
            if type(failure) is dict and "detail" in failure:
                fields.add("detail")
            failure = _object(failure, fields)
            if any(type(failure[name]) is not str for name in fields - {"detail"}):
                raise ValueError("invalid_failure")
            raise MemoryOperationFailure("disagree", safe_failure(response))
        return _receipt(
            document,
            command_digest=expected_digest,
            realm=client.scope.realm,
            key=idempotency_key,
        )
    except (ValueError, TypeError, RecursionError, CatalogueStorageError):
        raise MemoryOperationFailure(
            "disagree", _invalid_response(response.status_code)
        ) from None
