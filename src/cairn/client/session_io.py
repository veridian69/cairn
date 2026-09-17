"""Bounded session HTTP and fixed-context validation behind MemoryClient."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import httpx

from cairn.authority.custody import CustodyValueError
from cairn.client.diagnostics import _unique_object, safe_failure
from cairn.client.errors import MemoryOperationFailure
from cairn.client.session_types import SessionOperationResult, SessionSnapshot
from cairn.client.session_validation import (
    SESSION_WIRE_BYTES,
    SnapshotContext,
    require_preparation_binding,
    validate_operation,
    validate_snapshot,
)
from cairn.client.types import ConnectionStatus

if TYPE_CHECKING:
    from cairn.client.memory import MemoryClient

type SessionOperation = Literal[
    "session-open",
    "turn-begin",
    "turn-prepare",
    "turn-commit",
    "turn-abandon",
    "session-read",
    "visit-issue",
    "visit-acknowledge",
]


async def request_session(
    client: MemoryClient,
    operation: SessionOperation,
    session_id: UUID,
    *,
    turn_id: UUID | None = None,
    attempt_id: UUID | None = None,
    idempotency_key: UUID | None = None,
    fields: dict[str, object] | None = None,
) -> SessionOperationResult | SessionSnapshot:
    from cairn.client.memory import (
        _invalid_response,
        _local_failure,
        _transport_failure,
    )
    from cairn.client.session_validation import session_uuid

    def local(code: str) -> MemoryOperationFailure:
        return MemoryOperationFailure(
            operation, _local_failure(code, "The session request is invalid.")
        )

    if client._expected_instance_id is None:
        raise local("expected_instance_required")
    for identity in (
        session_id,
        client._expected_instance_id,
        turn_id,
        attempt_id,
        idempotency_key,
    ):
        if identity is not None:
            if type(identity) is not UUID:
                raise local("invalid_session_identity")
            session_uuid(str(identity))
    if (operation != "session-read") != (idempotency_key is not None):
        raise local("invalid_idempotency_key")
    known: SessionSnapshot | None = None
    if operation not in {"session-open", "session-read"}:
        # Binding is immutable in the catalogue. Validate it before sending
        # output or requesting custody, not only after a foreign-class write.
        # This read performs the same expected-instance/principal handshake.
        known = await client.read_session(
            session_id, turn_id=turn_id if operation == "turn-commit" else None
        )
    else:
        diagnostics = await client.diagnose(
            expected_instance_id=client._expected_instance_id
        )
        if diagnostics.status is not ConnectionStatus.READY:
            raise MemoryOperationFailure(
                operation,
                diagnostics.failure
                or _local_failure(
                    "authorisation_denied", "The session request is not authorised."
                ),
            )
        assert diagnostics.principal_id is not None
        if client._session_principal_id is None:
            client._session_principal_id = diagnostics.principal_id
        elif client._session_principal_id != diagnostics.principal_id:
            raise local("client_context_changed")
    assert client._session_principal_id is not None
    body: dict[str, object] = {
        "scope": client._scope_json,
        "session_id": str(session_id),
        "expected_instance_id": str(client._expected_instance_id),
    }
    if turn_id is not None:
        body["turn_id"] = str(turn_id)
    if attempt_id is not None:
        body["attempt_id"] = str(attempt_id)
    body.update(fields or {})
    headers = {"Accept-Encoding": "identity"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = str(idempotency_key)
    try:
        async with client._http.stream(
            "POST",
            client._base_url.join(f"/memory/v1/{operation}"),
            json=body,
            headers=headers,
            follow_redirects=False,
        ) as response:
            limit = SESSION_WIRE_BYTES if response.status_code == 200 else 16384
            if (
                response.headers.get("Content-Encoding", "identity").strip().lower()
                != "identity"
            ):
                raise ValueError("compressed_session_response")
            declared = response.headers.get("Content-Length")
            if declared is not None and (
                not declared.isascii()
                or not declared.isdecimal()
                or int(declared) > limit
            ):
                raise ValueError("invalid_response_size")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > limit:
                    raise ValueError("session_response_too_large")
                data.extend(chunk)
            if declared is not None and len(data) != int(declared):
                raise ValueError("invalid_response_size")
            if response.status_code != 200:
                raise MemoryOperationFailure(
                    operation,
                    safe_failure(
                        httpx.Response(response.status_code, content=bytes(data))
                    ),
                )
        document = json.loads(
            bytes(data).decode("utf-8"), object_pairs_hook=_unique_object
        )
        context = SnapshotContext(
            session_id=session_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            instance_id=client._expected_instance_id,
            principal_id=client._session_principal_id,
            scope=client.scope,
            scope_json=client._scope_json,
            classification=client.classification,
        )
        if operation == "session-read":
            snapshot = validate_snapshot(document, **context)
            if snapshot.visit_id is not None:
                raise ValueError("unexpected_visit")
            return snapshot
        result = validate_operation(document, **context)
        snapshot = result.snapshot
        expected_states = {
            "session-open": {"open"},
            "turn-begin": {"started"},
            "turn-prepare": {"prepared"},
            "turn-commit": {"committed", "skipped"},
            "turn-abandon": {"abandoned"},
            "visit-issue": {"open"},
            "visit-acknowledge": {"open"},
        }
        if snapshot.state not in expected_states[operation]:
            raise ValueError("unexpected_session_state")
        if operation == "turn-commit":
            assert known is not None
            require_preparation_binding(known, snapshot)
        if operation == "visit-issue" and snapshot.visit_id is None:
            raise ValueError("missing_visit")
        if operation != "visit-issue" and snapshot.visit_id is not None:
            raise ValueError("unexpected_visit")
        if operation == "turn-begin" and (
            None
            if snapshot.replaces_turn_id is None
            else str(snapshot.replaces_turn_id)
        ) != body.get("replaces_turn_id"):
            raise ValueError("unexpected_predecessor")
        if operation == "turn-prepare" and (
            snapshot.response != body["response"]
            or document["result"]["observations"] != body["observations"]
        ):
            raise ValueError("preparation_mismatch")
        if (
            operation == "turn-abandon"
            and snapshot.abandonment_reason != body["reason"]
        ):
            raise ValueError("abandonment_mismatch")
        return result
    except (ValueError, TypeError, RecursionError, OverflowError, CustodyValueError):
        raise MemoryOperationFailure(operation, _invalid_response()) from None
    except httpx.HTTPError:
        raise MemoryOperationFailure(operation, _transport_failure()) from None
