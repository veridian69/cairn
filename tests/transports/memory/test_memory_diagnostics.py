"""Diagnostic truth comes from real credentials, grants and packaged contracts."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.authority.credentials import mint_token
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)
from cairn.client import ConnectionStatus, MemoryClient
from cairn.transports.memory.contracts import (
    MANIFEST_NAME,
    OPENAPI_NAME,
    packaged_bytes,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostics_authenticate_identity_and_exact_scope_permissions(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor(
        operations=["retrieve", "ingest"], read_clearance="public"
    )
    async with serve(instance) as http:
        api = Api(http, token, transport)
        body = {"scope": SCOPE, "classification": "internal"}
        result = await api.call("diagnose", body)
        assert result.get("principal_id") == str(principal), result
        assert result["principal_kind"] == "workload"
        assert result["instance_id"] == str(instance.config.instance_id)
        assert result["contract_identity"] == "cairn.memory/v1"
        assert (
            result["contract_digest"]
            == hashlib.sha256(packaged_bytes(OPENAPI_NAME)).hexdigest()
        )
        assert (
            result["mcp_contract_digest"]
            == hashlib.sha256(packaged_bytes(MANIFEST_NAME)).hexdigest()
        )
        assert result["scope"] == SCOPE
        assert result["classification"] == "internal"
        assert result["permissions"] == {
            "retrieve": False,
            "ingest": True,
            "promote": False,
            "invalidate": False,
        }
        assert result["evaluated_at"] == canonical_timestamp(instance.clock())
        assert result["permission_basis"] == "current_grants_only"
        assert token not in json.dumps(result)
        assert set(result) == {
            "principal_id",
            "principal_kind",
            "instance_id",
            "product_version",
            "contract_identity",
            "contract_digest",
            "mcp_contract_digest",
            "scope",
            "classification",
            "permissions",
            "evaluated_at",
            "permission_basis",
        }
        public = await api.call("diagnose", {**body, "classification": "public"})
        assert public["permissions"]["retrieve"] is True
        sibling = await api.call(
            "diagnose",
            {
                **body,
                "scope": {
                    "realm": "acme",
                    "segments": [{"kind": "repository", "identifier": "sibling"}],
                },
            },
        )
        assert not any(sibling["permissions"].values())
        root = await api.call(
            "diagnose", {**body, "scope": {"realm": "acme", "segments": []}}
        )
        assert not any(root["permissions"].values())
    with read_connection(instance.data_path) as con:
        rows = con.execute(
            "SELECT outcome, json_extract(canonical_event, '$.principal_id') FROM audit_events WHERE action_code = 'memory-diagnose'"
        ).fetchall()
    assert len(rows) == 4
    assert all(row == ("allow", str(principal)) for row in rows)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_expired_grants_do_not_invalidate_authentication_or_reveal_realms(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        request = {"scope": SCOPE, "classification": "internal"}
        assert all((await api.call("diagnose", request))["permissions"].values())
        instance.clock.now = datetime(2040, 1, 1, tzinfo=UTC)
        expired = await api.call("diagnose", request)
        assert expired["principal_id"] == str(principal)
        assert not any(expired["permissions"].values())
        unknown = await api.call(
            "diagnose", {**request, "scope": {**SCOPE, "realm": "unknown-realm"}}
        )
        assert {k: v for k, v in expired.items() if k != "scope"} == {
            k: v for k, v in unknown.items() if k != "scope"
        }
    with read_connection(instance.data_path) as con:
        assert (
            con.execute(
                "SELECT 1 FROM realms WHERE realm_id = 'unknown-realm'"
            ).fetchone()
            is None
        )
        events = con.execute(
            "SELECT * FROM audit_events WHERE chain_kind = 'instance'"
        ).fetchall()
    assert events
    assert "unknown-realm" not in str(events)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostics_reject_bad_and_expired_credentials_without_identity(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    credential = uuid4()
    minted = mint_token(credential, lambda count: bytes(range(count)))
    token = minted.text
    with _open_write_connection(instance.data_path, create=False) as con:
        con.execute(
            "INSERT INTO credentials (credential_id, principal_id, verifier, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                str(credential),
                str(principal),
                minted.verifier,
                canonical_timestamp(instance.clock()),
                canonical_timestamp(instance.clock() + timedelta(seconds=1)),
            ),
        )
        con.commit()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        request = {"scope": SCOPE, "classification": "internal"}
        assert (await api.call("diagnose", request))["principal_id"] == str(principal)
        instance.clock.now += timedelta(seconds=1)
        for presented in (token, "bad-token"):
            response = await http.post(
                "/memory/v1/diagnose" if transport == "rest" else "/memory/v1/mcp",
                headers={
                    "Authorization": f"Bearer {presented}",
                    "Accept": "application/json",
                },
                json=request
                if transport == "rest"
                else {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "diagnose", "arguments": request},
                },
            )
            assert response.status_code == 401
            result = response.json()
            assert result["failure"]["code"] == "authentication_failed"
            assert str(principal) not in str(result)
            assert presented not in str(result)
    with read_connection(instance.data_path) as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM audit_events WHERE outcome = 'deny'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostics_refuse_model_authority_claims_and_secret_addressing(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        request = {"scope": SCOPE, "classification": "internal"}
        for extra in (
            {"principal_id": "claimed"},
            {"classification": "secret"},
            {
                "scope": {
                    "realm": "acme",
                    "segments": [
                        {"kind": "repository", "identifier": "AKIAIOSFODNN7EXAMPLE"}
                    ],
                }
            },
        ):
            result = await api.call("diagnose", {**request, **extra})
            assert result["failure"]["code"] in {"invalid_request", "secret_rejected"}
            assert "AKIAIOSFODNN7EXAMPLE" not in str(result)


@pytest.mark.anyio
async def test_real_client_diagnostics_and_history_use_authenticated_memory_surface(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    async with serve(instance) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        memory = MemoryClient(
            http,
            scope=Scope("acme", (ScopeSegment("repository", "cairn"),)),
            classification=Classification.INTERNAL,
        )
        diagnostic = await memory.diagnose(
            expected_instance_id=instance.config.instance_id
        )
        assert diagnostic.status is ConnectionStatus.READY
        assert diagnostic.principal_id == principal
        remembered = await Api(http, token).remember("A durable diagnostic test fact.")
        fact_id = UUID(remembered["result"]["fact_ids"][0])
        history = await memory.history(fact_id)
        facts = history.data["facts"]
        assert isinstance(facts, tuple)
        fact = facts[0]
        assert isinstance(fact, Mapping)
        assert fact["body"] == "A durable diagnostic test fact."
        bounded = await memory.history(fact_id, budget=1)
        assert bounded.data["facts"] == ()
        assert bounded.data["budget_exhausted"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostics_no_grants_is_successful_authentication(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, credential = uuid4(), uuid4()
    minted = mint_token(credential, lambda count: bytes(range(count)))
    with _open_write_connection(instance.data_path, create=False) as con:
        ts = canonical_timestamp(instance.clock())
        con.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) VALUES (?, 'human', 'unprivileged', ?)",
            (str(principal), ts),
        )
        con.execute(
            "INSERT INTO credentials (credential_id, principal_id, verifier, created_at) VALUES (?, ?, ?, ?)",
            (str(credential), str(principal), minted.verifier, ts),
        )
        con.commit()
    async with serve(instance) as http:
        result = await Api(http, minted.text, transport).call(
            "diagnose", {"scope": SCOPE, "classification": "internal"}
        )
        assert result["principal_id"] == str(principal)
        assert result["principal_kind"] == "human"
        assert not any(result["permissions"].values())


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_write_permission_uses_selected_grant_and_refreshes_after_revocation(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    restrictive = "00000000-0000-4000-8000-000000000000"
    with _open_write_connection(instance.data_path, create=False) as con:
        con.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, operations, read_clearance, write_classifications, expires_at, created_at) SELECT ?, principal_id, realm_id, scope_segments, operations, 'public', '[\"public\"]', expires_at, created_at FROM grants WHERE principal_id = ?",
            (restrictive, str(principal)),
        )
        con.commit()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        request = {"scope": SCOPE, "classification": "internal"}
        first = await api.call("diagnose", request)
        assert first["permissions"] == {
            "retrieve": True,
            "ingest": False,
            "promote": False,
            "invalidate": True,
        }
        assert (await api.remember("An ordinary fact."))["failure"][
            "code"
        ] == "authorisation_denied"
        with _open_write_connection(instance.data_path, create=False) as con:
            con.execute(
                "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) VALUES (?, ?, 'test_revocation')",
                (restrictive, canonical_timestamp(instance.clock())),
            )
            con.commit()
        assert all((await api.call("diagnose", request))["permissions"].values())
        assert (await api.remember("An ordinary fact."))["outcome"] == "committed"
        # A positive grant summary is not a promise that screening will pass.
        assert (await api.remember("AKIAIOSFODNN7EXAMPLE"))["failure"][
            "code"
        ] == "secret_rejected"
        with _open_write_connection(instance.data_path, create=False) as con:
            con.execute(
                "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id = ? AND grant_id != ?",
                (canonical_timestamp(instance.clock()), str(principal), restrictive),
            )
            con.commit()
        assert not any((await api.call("diagnose", request))["permissions"].values())
