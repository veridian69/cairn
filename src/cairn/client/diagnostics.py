"""Strict bounded diagnostic decoding; server prose is never safe metadata."""

import json
from dataclasses import replace
from uuid import UUID

import httpx

from cairn.catalogue.audit import Classification, Scope
from cairn.catalogue.sqlite import parse_timestamp
from cairn.catalogue.transactions import FailureCode, RetryClass
from cairn.client.errors import FailureMetadata
from cairn.client.types import (
    ConnectionDiagnostics,
    ConnectionStatus,
    DiagnosticPermissions,
)
from cairn.client.validation import _timestamp, _uuid
from cairn.transports.memory.models import DiagnoseBody


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _document(response: httpx.Response) -> object:
    if len(response.content) > 16384:
        raise ValueError("diagnostic_response_too_large")
    return json.loads(
        response.content.decode("utf-8"), object_pairs_hook=_unique_object
    )


def unrecognised_failure(
    status_code: int, *, cairn_shaped: bool = False
) -> FailureMetadata:
    """Classify a refusal whose body is not a readable Cairn failure envelope.

    Cairn answers 401 and 403 with its own envelope. One without it came from
    something in front of Cairn, such as an access proxy, so it must not be
    reported as a fault in the Cairn credential. A body that has the envelope's
    shape but an unreadable member stays the generic HTTP fallback.
    """
    if status_code in (401, 403) and not cairn_shaped:
        return FailureMetadata(
            "upstream_access_denied",
            "A proxy in front of Cairn refused the request.",
            "never",
            None,
            status_code,
        )
    return FailureMetadata(
        "http_error", "Cairn rejected the memory request.", "never", None, status_code
    )


def safe_failure(response: httpx.Response) -> FailureMetadata:
    """Keep only closed vocabulary and canonical correlation identity."""
    cairn_shaped = False
    try:
        value = _document(response)
        if type(value) is not dict or type(value.get("failure")) is not dict:
            raise ValueError("invalid_failure")
        cairn_shaped = True
        failure = value["failure"]
        code = FailureCode(failure["code"]).value
        retry = RetryClass(failure["retry"]).value
        correlation = _uuid(failure["correlation_id"])
    except (ValueError, TypeError, KeyError, RecursionError):
        return unrecognised_failure(response.status_code, cairn_shaped=cairn_shaped)
    return FailureMetadata(
        code,
        "Cairn rejected the memory request.",
        retry,
        correlation,
        response.status_code,
    )


def diagnostic_result(
    response: httpx.Response,
    *,
    scope: Scope,
    scope_json: dict[str, object],
    classification: Classification,
) -> ConnectionDiagnostics:
    if response.status_code != 200:
        status = {
            401: ConnectionStatus.AUTHENTICATION_FAILED,
            403: ConnectionStatus.AUTHORISATION_DENIED,
        }.get(response.status_code, ConnectionStatus.INVALID_RESPONSE)
        failure = safe_failure(response)
        if failure.code == "upstream_access_denied":
            status = ConnectionStatus.UPSTREAM_ACCESS_DENIED
        return ConnectionDiagnostics(status, failure=failure)
    try:
        raw = _document(response)
        if type(raw) is not dict or set(raw) != set(DiagnoseBody.model_fields):
            raise ValueError("diagnostic_fields")
        body = DiagnoseBody.model_validate(raw)
        _uuid(body.instance_id)
        _uuid(body.principal_id)
        _timestamp(body.evaluated_at)
        if (
            body.scope.model_dump() != scope_json
            or body.classification != classification.value
        ):
            raise ValueError("diagnostic_scope_mismatch")
        permissions = DiagnosticPermissions(**body.permissions.model_dump())
        return ConnectionDiagnostics(
            status=ConnectionStatus.READY
            if any(body.permissions.model_dump().values())
            else ConnectionStatus.NO_AUTHORISED_OPERATIONS,
            instance_id=UUID(body.instance_id),
            product_version=body.product_version,
            contract_identity=body.contract_identity,
            contract_digest=body.contract_digest,
            mcp_contract_digest=body.mcp_contract_digest,
            principal_id=UUID(body.principal_id),
            principal_kind=body.principal_kind,
            scope=scope,
            classification=classification,
            permissions=permissions,
            evaluated_at=parse_timestamp(body.evaluated_at),
            permission_basis=body.permission_basis,
        )
    except (ValueError, TypeError, RecursionError):
        return ConnectionDiagnostics(
            ConnectionStatus.INVALID_RESPONSE,
            failure=FailureMetadata(
                "invalid_response",
                "Cairn returned an invalid memory response.",
                "never",
                None,
                response.status_code,
            ),
        )


def check_expectations(
    result: ConnectionDiagnostics,
    instance: UUID | None,
    contract: str | None,
    mcp_contract: str | None,
) -> ConnectionDiagnostics:
    if result.instance_id is None:
        return result
    if instance is not None and instance != result.instance_id:
        return replace(
            result,
            status=ConnectionStatus.INSTANCE_MISMATCH,
            failure=FailureMetadata(
                "instance_mismatch",
                "Cairn instance identity did not match.",
                "never",
                None,
                None,
            ),
        )
    if contract not in {None, result.contract_digest} or mcp_contract not in {
        None,
        result.mcp_contract_digest,
    }:
        return replace(
            result,
            status=ConnectionStatus.INCOMPATIBLE,
            failure=FailureMetadata(
                "contract_mismatch",
                "Cairn served memory contract does not match.",
                "never",
                None,
                None,
            ),
        )
    return result
