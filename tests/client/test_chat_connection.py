"""A subscribed host is never launched before Cairn passes its handshake."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client.chat_connection import _checked_turn, _diagnose
from cairn.client.chat_host import ChatActor, ChatHostError
from cairn.client.profiles import MemoryProfile
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DiagnosticPermissions,
)


def _profile(tmp_path: Path) -> MemoryProfile:
    return MemoryProfile(
        endpoint="http://127.0.0.1:18423",
        expected_instance_id=uuid4(),
        scope=Scope("everyday", (ScopeSegment("job", "workshop"),)),
        classification=Classification.INTERNAL,
        credential_file=tmp_path / "token",
        session_id=uuid4(),
    )


def _actor(tmp_path: Path, profile: MemoryProfile) -> ChatActor:
    return ChatActor(
        provider="codex",
        executable=tmp_path / "codex",
        auth_file=tmp_path / "auth.json",
        profile_path=tmp_path / "profile.json",
        expected_principal=uuid4(),
        model="gpt-test",
    )


def _ready(actor: ChatActor, profile: MemoryProfile) -> ConnectionDiagnostics:
    return ConnectionDiagnostics(
        ConnectionStatus.READY,
        instance_id=profile.expected_instance_id,
        principal_id=actor.expected_principal,
        principal_kind="workload",
        scope=profile.scope,
        classification=profile.classification,
        permissions=DiagnosticPermissions(
            retrieve=True, ingest=True, promote=False, invalidate=False
        ),
        permission_basis="current_grants_only",
    )


def _run(
    actor: ChatActor,
    profile: MemoryProfile,
    diagnostic: ConnectionDiagnostics,
    task: bytes = "line one\r\nSälü 🪨\n".encode(),
) -> tuple[str, list[bytes]]:
    launched: list[bytes] = []
    checks = 0

    async def diagnose(_: MemoryProfile, __: str, ___: UUID) -> ConnectionDiagnostics:
        nonlocal checks
        checks += 1
        return diagnostic

    def host(_: ChatActor, submitted: bytes) -> str:
        launched.append(submitted)
        return "answer"

    result = _checked_turn(
        actor,
        task,
        profile=profile,
        credential="cairn1.secret",
        diagnose=diagnose,
        host=host,
    )
    assert checks == 1
    assert isinstance(result, str)
    return result, launched


def test_ready_check_runs_once_then_preserves_exact_task(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    actor = _actor(tmp_path, profile)

    result, launched = _run(actor, profile, _ready(actor, profile))

    assert result == "answer"
    assert launched == ["line one\r\nSälü 🪨\n".encode()]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda value, _actor, _profile: ConnectionDiagnostics(
                ConnectionStatus.UNREACHABLE
            ),
            "memory_connection_unavailable",
        ),
        (
            lambda value, actor, _profile: replace(value, principal_id=uuid4()),
            "memory_context_mismatch",
        ),
        (
            lambda value, _actor, _profile: replace(value, instance_id=uuid4()),
            "memory_context_mismatch",
        ),
        (
            lambda value, _actor, _profile: replace(value, scope=Scope("other", ())),
            "memory_context_mismatch",
        ),
        (
            lambda value, _actor, _profile: replace(
                value, classification=Classification.RESTRICTED
            ),
            "memory_context_mismatch",
        ),
        (
            lambda value, _actor, _profile: replace(
                value,
                permissions=DiagnosticPermissions(
                    retrieve=False,
                    ingest=True,
                    promote=False,
                    invalidate=False,
                ),
            ),
            "memory_retrieve_unavailable",
        ),
    ],
)
def test_failed_or_mismatched_check_never_launches_host(
    tmp_path: Path,
    change: Callable[
        [ConnectionDiagnostics, ChatActor, MemoryProfile], ConnectionDiagnostics
    ],
    code: str,
) -> None:
    profile = _profile(tmp_path)
    actor = _actor(tmp_path, profile)
    launched = False

    async def diagnose(_: MemoryProfile, __: str, ___: UUID) -> ConnectionDiagnostics:
        return change(_ready(actor, profile), actor, profile)

    def host(_: ChatActor, __: bytes) -> str:
        nonlocal launched
        launched = True
        return "unexpected"

    with pytest.raises(ChatHostError, match=f"^{code}$") as failure:
        _checked_turn(
            actor,
            b"never submitted",
            profile=profile,
            credential="cairn1.secret",
            diagnose=diagnose,
            host=host,
        )

    assert not launched
    assert failure.value.public_details["status"] in {
        "unreachable",
        "ready",
    }
    assert "endpoint" not in failure.value.public_details


def test_diagnostic_exception_is_closed_and_never_launches_host(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    actor = _actor(tmp_path, profile)
    launched = False

    async def diagnose(_: MemoryProfile, __: str, ___: UUID) -> ConnectionDiagnostics:
        raise RuntimeError("SECRET DIAGNOSTIC DETAIL")

    def host(_: ChatActor, __: bytes) -> str:
        nonlocal launched
        launched = True
        return "unexpected"

    with pytest.raises(
        ChatHostError, match="^memory_connection_unavailable$"
    ) as failure:
        _checked_turn(
            actor,
            b"never submitted",
            profile=profile,
            credential="cairn1.secret",
            diagnose=diagnose,
            host=host,
        )

    assert not launched
    assert failure.value.public_details["status"] == "check_failed"
    assert "SECRET" not in repr(failure.value.public_details)


def test_diagnose_uses_bounded_memory_handshake(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    actor = _actor(tmp_path, profile)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "instance_id": str(profile.expected_instance_id),
                "principal_id": str(actor.expected_principal),
                "principal_kind": "workload",
                "product_version": "0.1.0",
                "contract_identity": "cairn.memory/v1",
                "contract_digest": "a" * 64,
                "mcp_contract_digest": "b" * 64,
                "scope": {
                    "realm": profile.scope.realm,
                    "segments": [
                        {"kind": item.kind, "identifier": item.identifier}
                        for item in profile.scope.segments
                    ],
                },
                "classification": profile.classification.value,
                "permissions": {
                    "retrieve": True,
                    "ingest": True,
                    "promote": False,
                    "invalidate": False,
                },
                "evaluated_at": "2026-09-12T12:00:00.000000Z",
                "permission_basis": "current_grants_only",
            },
        )

    result = asyncio.run(
        _diagnose(
            profile,
            "cairn1.secret",
            actor.expected_principal,
            transport=httpx.MockTransport(respond),
        )
    )

    assert result.status is ConnectionStatus.READY
    assert requests[0].url.path == "/memory/v1/diagnose"
    assert requests[0].headers["Authorization"] == "Bearer cairn1.secret"
