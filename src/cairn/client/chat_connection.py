"""Fail closed before a subscribed host is allowed to see a turn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast
from uuid import UUID

import httpx

from cairn.client.chat_host import ChatActor, ChatHostError, ChatTurn, run_turn
from cairn.client.memory import MemoryClient
from cairn.client.profiles import (
    MemoryProfile,
    ProfileError,
    load_credential,
    load_profile,
)
from cairn.client.types import ConnectionDiagnostics, ConnectionStatus

DiagnosticCall = Callable[
    [MemoryProfile, str, UUID], Coroutine[Any, Any, ConnectionDiagnostics]
]
HostCall = Callable[[ChatActor, bytes], str | ChatTurn]


async def _diagnose(
    profile: MemoryProfile,
    credential: str,
    expected_principal: UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionDiagnostics:
    """Perform the same bounded authenticated handshake as the MCP adapter."""
    del expected_principal
    async with httpx.AsyncClient(
        base_url=profile.endpoint,
        headers={"Authorization": f"Bearer {credential}"},
        timeout=httpx.Timeout(10),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as http:
        client = MemoryClient(
            http,
            scope=profile.scope,
            classification=profile.classification,
            expected_instance_id=profile.expected_instance_id,
        )
        return await client.diagnose(expected_instance_id=profile.expected_instance_id)


def _details(
    actor: ChatActor,
    profile: MemoryProfile,
    diagnostic: ConnectionDiagnostics,
    *,
    field: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": diagnostic.status.value,
        "expected_instance_id": str(profile.expected_instance_id),
        "expected_principal_id": str(actor.expected_principal),
    }
    if profile.session_id is not None:
        result["session_id"] = str(profile.session_id)
    if field is not None:
        result["field"] = field
    return result


def _failed_details(actor: ChatActor, profile: MemoryProfile) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "check_failed",
        "expected_instance_id": str(profile.expected_instance_id),
        "expected_principal_id": str(actor.expected_principal),
    }
    if profile.session_id is not None:
        result["session_id"] = str(profile.session_id)
    return result


def _checked_turn(
    actor: ChatActor,
    task: bytes,
    *,
    profile: MemoryProfile,
    credential: str,
    diagnose: DiagnosticCall,
    host: HostCall,
) -> str | ChatTurn:
    try:
        diagnostic = asyncio.run(
            diagnose(profile, credential, actor.expected_principal)
        )
    except Exception:
        raise ChatHostError(
            "memory_connection_unavailable", _failed_details(actor, profile)
        ) from None
    if diagnostic.status is not ConnectionStatus.READY:
        raise ChatHostError(
            "memory_connection_unavailable",
            _details(actor, profile, diagnostic),
        )

    expected = (
        ("instance_id", diagnostic.instance_id, profile.expected_instance_id),
        ("principal_id", diagnostic.principal_id, actor.expected_principal),
        ("scope", diagnostic.scope, profile.scope),
        (
            "classification",
            diagnostic.classification,
            profile.classification,
        ),
        ("permission_basis", diagnostic.permission_basis, "current_grants_only"),
    )
    mismatch = next(
        (name for name, actual, wanted in expected if actual != wanted), None
    )
    if mismatch is not None:
        raise ChatHostError(
            "memory_context_mismatch",
            _details(actor, profile, diagnostic, field=mismatch),
        )
    if diagnostic.permissions is None or not diagnostic.permissions.retrieve:
        raise ChatHostError(
            "memory_retrieve_unavailable",
            _details(actor, profile, diagnostic, field="retrieve"),
        )
    return host(actor, task)


def checked_turn(actor: ChatActor, task: bytes) -> str | ChatTurn:
    """Check Cairn once, then launch exactly one subscribed host turn."""
    try:
        profile = load_profile(actor.profile_path)
        credential = load_credential(profile)
    except (AttributeError, ProfileError):
        raise ChatHostError("invalid_host_profile") from None
    return _checked_turn(
        actor,
        task,
        profile=profile,
        credential=credential,
        diagnose=cast(DiagnosticCall, _diagnose),
        host=run_turn,
    )
