"""Bounded read-only suggestion HTTP with an instance handshake before content."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from cairn.client.diagnostics import _unique_object, safe_failure
from cairn.client.errors import MemoryOperationFailure
from cairn.client.suggestion_types import SuggestedMemory
from cairn.client.suggestion_validation import validate_input, validate_suggestions
from cairn.client.types import ConnectionStatus
from cairn.client.validation import _uuid

if TYPE_CHECKING:
    from cairn.client.memory import MemoryClient

# At most six JSON escaping bytes per canonical UTF-8 item byte, plus 4096
# bytes for the fixed envelope, <=16 separators and bounded source policy.
# Extra formatting is refused, never silently truncated. Errors remain 16KiB.
SUGGESTION_WIRE_BYTES = 6 * 65536 + 4096


async def request_suggestions(
    client: MemoryClient,
    *,
    observation: str | None,
    fact_ids: tuple[UUID, ...],
    budget: int,
    limit: int,
) -> SuggestedMemory:
    from cairn.client.memory import (
        _invalid_response,
        _local_failure,
        _transport_failure,
    )

    if client.expected_instance_id is None:
        raise MemoryOperationFailure(
            "suggest",
            _local_failure(
                "expected_instance_required", "An expected instance is required."
            ),
        )
    try:
        _uuid(str(client.expected_instance_id))
        validate_input(observation, fact_ids, budget, limit)
    except (ValueError, TypeError, OverflowError):
        raise MemoryOperationFailure(
            "suggest",
            _local_failure("invalid_request", "The suggestion request is invalid."),
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
            "suggest",
            diagnostics.failure
            or _local_failure(
                "authorisation_denied", "The suggestion request is not authorised."
            ),
        )
    try:
        async with client._http.stream(
            "POST",
            client._base_url.join("/memory/v1/suggest"),
            json={
                "scope": client._scope_json,
                "expected_instance_id": str(client.expected_instance_id),
                "observation": observation,
                "fact_ids": [str(identity) for identity in fact_ids],
                "budget": budget,
                "limit": limit,
            },
            headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            cap = 6 * budget + 4096 if response.status_code == 200 else 16384
            if (
                response.headers.get("Content-Encoding", "identity").strip().lower()
                != "identity"
            ):
                raise ValueError("compressed_response")
            declared = response.headers.get("Content-Length")
            if declared is not None and (
                len(declared) > 6
                or not declared.isascii()
                or not declared.isdecimal()
                or int(declared) > cap
            ):
                raise ValueError("invalid_content_length")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > cap:
                    raise ValueError("oversized_response")
                data.extend(chunk)
            if declared is not None and len(data) != int(declared):
                raise ValueError("incorrect_content_length")
            if response.status_code != 200:
                raise MemoryOperationFailure(
                    "suggest",
                    safe_failure(
                        httpx.Response(response.status_code, content=bytes(data))
                    ),
                )
        document = json.loads(
            bytes(data).decode("utf-8"), object_pairs_hook=_unique_object
        )
        return validate_suggestions(
            document,
            scope=client.scope,
            observation=observation,
            fact_ids=fact_ids,
            budget=budget,
            limit=limit,
        )
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise MemoryOperationFailure("suggest", _invalid_response()) from None
    except httpx.HTTPError:
        raise MemoryOperationFailure("suggest", _transport_failure()) from None
