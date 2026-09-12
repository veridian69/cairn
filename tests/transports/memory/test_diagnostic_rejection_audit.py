"""Authenticated diagnostic wire denials must be durably audited first."""

import json
from pathlib import Path
from typing import Any

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import CatalogueContention, CatalogueTransactions


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "extra,rule",
    [
        ({"principal_id": "UNTRUSTED-PRINCIPAL"}, "unknown_field"),
        ({"classification": "secret"}, "invalid_value"),
        (
            {
                "scope": {
                    "realm": "unknown-realm",
                    "segments": [
                        {"kind": "project", "identifier": "unsafe whitespace"}
                    ],
                }
            },
            "invalid_value",
        ),
    ],
)
async def test_diagnostic_wire_denial_is_audited_without_untrusted_values(
    tmp_path: Path, transport: str, extra: dict[str, Any], rule: str
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    async with serve(instance) as http:
        result = await Api(http, token, transport).call(
            "diagnose", {"scope": SCOPE, "classification": "internal", **extra}
        )
        assert result["failure"]["code"] == "invalid_request"
        assert result["failure"]["detail"]["rule"] == rule
        with read_connection(instance.data_path) as con:
            rows = con.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code = 'memory-diagnose'"
            ).fetchall()
            assert (
                con.execute(
                    "SELECT 1 FROM realms WHERE realm_id = 'unknown-realm'"
                ).fetchone()
                is None
            )
        assert len(rows) == 1
        event = json.loads(rows[0][0])
        assert event["chain_kind"] == "instance"
        assert event["chain_identity"] == str(instance.config.instance_id)
        assert event["principal_id"] == str(principal)
        assert event["credential_verifier_id"] == token.split(".")[1]
        assert event["outcome"] == "deny"
        assert event["correlation_id"] == result["failure"]["correlation_id"]
        assert event["requested_scope"] is None
        assert event["grant_id"] is None
        assert event["safe_request_fingerprint"] is not None
        for value in (
            token,
            "UNTRUSTED-PRINCIPAL",
            "unknown-realm",
            "unsafe whitespace",
        ):
            assert value not in str(rows)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostic_forbidden_idempotency_key_is_audited(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        result = await Api(http, token, transport).call(
            "diagnose",
            {"scope": SCOPE, "classification": "internal"},
            key="11111111-1111-4111-8111-111111111111",
        )
        assert result["failure"]["detail"]["rule"] == "idempotency_key_forbidden"
        with read_connection(instance.data_path) as con:
            assert (
                con.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action_code = 'memory-diagnose' AND outcome = 'deny'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.anyio
async def test_rest_diagnostic_malformed_json_is_audited_after_authentication(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        response = await http.post(
            "/memory/v1/diagnose",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=b'{"PRIVATE-RAW-BODY":',
        )
        assert response.status_code == 400
        with read_connection(instance.data_path) as con:
            rows = con.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code = 'memory-diagnose'"
            ).fetchall()
        assert len(rows) == 1
        assert "PRIVATE-RAW-BODY" not in str(rows)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("contention", [False, True])
async def test_diagnostic_rejection_audit_failure_never_returns_ordinary_denial(
    tmp_path: Path, transport: str, contention: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:

        def fail(*args: Any, **kwargs: Any) -> Any:
            if contention:
                raise CatalogueContention()
            raise RuntimeError("PRIVATE-AUDIT-FAILURE")

        monkeypatch.setattr(CatalogueTransactions, "append_audit", fail)
        result = await Api(http, token, transport).call(
            "diagnose", {"scope": SCOPE, "classification": "secret"}
        )
        assert result["failure"]["code"] == (
            "dependency_unavailable" if contention else "internal_error"
        )
        assert "PRIVATE-AUDIT-FAILURE" not in str(result)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_diagnostic_fingerprint_tracks_malformed_arguments_without_raw_storage(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        for identifier in (
            "private scope one",
            "private scope one",
            "private scope two",
        ):
            result = await Api(http, token, transport).call(
                "diagnose",
                {
                    "scope": {
                        "realm": "unknown-realm",
                        "segments": [{"kind": "project", "identifier": identifier}],
                    },
                    "classification": "internal",
                },
            )
            assert result["failure"]["code"] == "invalid_request"
        with read_connection(instance.data_path) as con:
            rows = con.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code = 'memory-diagnose' ORDER BY sequence"
            ).fetchall()
        fingerprints = [json.loads(row[0])["safe_request_fingerprint"] for row in rows]
        assert len(fingerprints) == 3
        assert fingerprints[0] is not None
        assert fingerprints[0] == fingerprints[1]
        assert fingerprints[1] != fingerprints[2]
        assert "private scope" not in str(rows)
        assert "unknown-realm" not in str(rows)


@pytest.mark.anyio
async def test_rest_malformed_admission_fingerprint_tracks_read_bytes(
    tmp_path: Path,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        for content in (
            b'{"PRIVATE-BODY-ONE":',
            b'{"PRIVATE-BODY-ONE":',
            b'{"PRIVATE-BODY-TWO":',
        ):
            response = await http.post(
                "/memory/v1/diagnose",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content=content,
            )
            assert response.status_code == 400
        with read_connection(instance.data_path) as con:
            rows = con.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code = 'memory-diagnose' ORDER BY sequence"
            ).fetchall()
        fingerprints = [json.loads(row[0])["safe_request_fingerprint"] for row in rows]
        assert len(fingerprints) == 3
        assert fingerprints[0] is not None
        assert fingerprints[0] == fingerprints[1]
        assert fingerprints[1] != fingerprints[2]
        assert "PRIVATE-BODY" not in str(rows)
