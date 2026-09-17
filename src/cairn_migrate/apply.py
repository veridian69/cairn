"""Apply a verified migration plan to Cairn's ordinary ``/v1`` surface.

The module deliberately knows no catalogue internals.  It validates the
digest-pinned plan, sends its canonical request bodies unchanged, and makes
restart progress durable only after Cairn has returned the authoritative
identities for a successful ingest.
"""

import hashlib
import json
import os
from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from httpx import AsyncClient, Response

from cairn_migrate.mapping import (
    ASSERTION_STORES,
    OPERATIONS_FILENAME,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    MappingError,
    PlannedOperation,
    _canonical_uuid,
    canonical_json,
)

APPLY_ERROR_CODES = frozenset(
    {
        "plan_unreadable",
        "plan_manifest_invalid",
        "plan_digest_mismatch",
        "plan_value_invalid",
        "receipts_unreadable",
        "receipt_value_invalid",
        "receipt_conflict",
        "receipt_instance_mismatch",
        "request_failed",
        "response_invalid",
        "instance_check_failed",
        "instance_mismatch",
    }
)

_SERVER_FAILURE_CODES = frozenset(
    {
        "invalid_request",
        "authentication_failed",
        "authorisation_denied",
        "secret_rejected",
        "not_found",
        "idempotency_conflict",
        "index_pending",
        "stale_index",
        "dependency_unavailable",
        "instance_mismatch",
        "internal_error",
    }
)

_AFTER_DELAY_FAILURE_CODES = frozenset(
    {"index_pending", "stale_index", "dependency_unavailable"}
)


class ApplyError(Exception):
    """A privacy-safe, closed refusal from the apply boundary."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in APPLY_ERROR_CODES:
            raise ValueError(f"unknown apply error code: {code}")
        self.code = code
        self.detail = detail
        suffix = "" if detail is None else f": {detail}"
        super().__init__(f"apply error: {code}{suffix}")


@dataclass(frozen=True, slots=True)
class PlanOperation:
    operation: str
    store: str
    legacy_id: str
    idempotency_key: str
    request: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ApplyReceipt:
    instance_id: str
    operation: str
    store: str
    legacy_id: str
    idempotency_key: str
    assertion_id: str | None
    fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplySummary:
    planned: int
    applied: int
    resumed: int


async def verify_instance(
    *, client: AsyncClient, credential: str, expected_instance: str
) -> None:
    response = await client.get(
        "/v1/instance",
        headers={"Authorization": f"Bearer {credential}"},
    )
    if response.status_code != 200:
        raise ApplyError("instance_check_failed", detail=_failure_code(response))
    try:
        payload = response.json()
    except ValueError as error:
        raise ApplyError("instance_check_failed") from error
    if not isinstance(payload, dict) or payload.get("instance_id") != expected_instance:
        raise ApplyError("instance_mismatch")


async def apply_plan(
    *,
    plan_path: Path,
    receipts_path: Path,
    expected_instance: str,
    client: AsyncClient,
    credential: str,
    sleep: Callable[[float], Awaitable[None]] = async_sleep,
    max_attempts: int = 3,
) -> ApplySummary:
    """Apply every not-yet-receipted operation in plan order."""
    if not _canonical_uuid(expected_instance, version=4):
        raise ValueError("expected_instance must be a canonical UUIDv4")
    operations = read_plan(plan_path)
    receipts = read_receipts(receipts_path, expected_instance=expected_instance)
    by_identity = {
        (receipt.operation, receipt.store, receipt.legacy_id): receipt
        for receipt in receipts
    }
    applied = 0
    resumed = 0
    for operation in operations:
        identity = (operation.operation, operation.store, operation.legacy_id)
        existing = by_identity.get(identity)
        if existing is not None:
            if existing.idempotency_key != operation.idempotency_key:
                raise ApplyError("receipt_conflict")
            resumed += 1
            continue
        response = await _post_with_retry(
            operation,
            client=client,
            credential=credential,
            sleep=sleep,
            max_attempts=max_attempts,
        )
        receipt = _response_receipt(operation, response, instance_id=expected_instance)
        _append_receipt(receipts_path, receipt)
        by_identity[identity] = receipt
        applied += 1
    return ApplySummary(planned=len(operations), applied=applied, resumed=resumed)


async def _post_with_retry(
    operation: PlanOperation,
    *,
    client: AsyncClient,
    credential: str,
    sleep: Callable[[float], Awaitable[None]],
    max_attempts: int,
) -> Response:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for attempt in range(1, max_attempts + 1):
        response = await client.post(
            f"/v1/{operation.operation}",
            content=canonical_json(operation.request).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Idempotency-Key": operation.idempotency_key,
            },
        )
        if response.status_code == 200 or not _is_after_delay(response):
            return response
        if attempt < max_attempts:
            await sleep(_retry_delay(response))
    return response


def _is_after_delay(response: Response) -> bool:
    payload = _failure_payload(response)
    return (
        response.status_code == 503
        and payload is not None
        and set(payload) == {"code", "message", "correlation_id", "retry"}
        and type(payload.get("code")) is str
        and payload["code"] in _AFTER_DELAY_FAILURE_CODES
        and isinstance(payload.get("message"), str)
        and bool(payload["message"])
        and _canonical_uuid(payload.get("correlation_id"), version=4)
        and payload.get("retry") == "after-delay"
        and _retry_after_value(response) is not None
    )


def _retry_delay(response: Response) -> float:
    parsed = _retry_after_value(response)
    return 1.0 if parsed is None else min(parsed, 30.0)


def _retry_after_value(response: Response) -> int | None:
    value = response.headers.get("Retry-After")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read_plan(plan_path: Path) -> tuple[PlanOperation, ...]:
    """Read the digest-pinned operation stream from a complete plan."""
    try:
        manifest_raw = (plan_path / PLAN_MANIFEST_FILENAME).read_bytes()
        manifest = json.loads(manifest_raw)
    except (OSError, ValueError) as error:
        raise ApplyError("plan_unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != (
        PLAN_SCHEMA_VERSION
    ):
        raise ApplyError("plan_manifest_invalid")
    entry = _operations_entry(manifest)
    try:
        raw = (plan_path / OPERATIONS_FILENAME).read_bytes()
    except OSError as error:
        raise ApplyError("plan_unreadable") from error
    if (
        entry.get("bytes") != len(raw)
        or entry.get("sha256") != hashlib.sha256(raw).hexdigest()
        or entry.get("record_count") != raw.count(b"\n")
    ):
        raise ApplyError("plan_digest_mismatch")
    if raw and not raw.endswith(b"\n"):
        raise ApplyError("plan_value_invalid")
    operations: list[PlanOperation] = []
    identities: set[tuple[str, str, str]] = set()
    for encoded in raw.splitlines():
        try:
            value = json.loads(encoded)
        except ValueError as error:
            raise ApplyError("plan_value_invalid") from error
        operation = _plan_operation(value)
        identity = (operation.operation, operation.store, operation.legacy_id)
        if identity in identities:
            raise ApplyError("plan_value_invalid")
        identities.add(identity)
        operations.append(operation)
    return tuple(operations)


def _operations_entry(manifest: Mapping[str, object]) -> Mapping[str, object]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ApplyError("plan_manifest_invalid")
    matches = [
        value
        for value in files
        if isinstance(value, dict) and value.get("filename") == OPERATIONS_FILENAME
    ]
    if len(matches) != 1:
        raise ApplyError("plan_manifest_invalid")
    return cast(Mapping[str, object], matches[0])


def _plan_operation(value: object) -> PlanOperation:
    if not isinstance(value, dict) or set(value) != {
        "operation",
        "store",
        "legacy_id",
        "idempotency_key",
        "request",
    }:
        raise ApplyError("plan_value_invalid")
    if (
        type(value["operation"]) is not str
        or type(value["store"]) is not str
        or type(value["legacy_id"]) is not str
        or type(value["idempotency_key"]) is not str
        or not isinstance(value["request"], dict)
    ):
        raise ApplyError("plan_value_invalid")
    try:
        planned = PlannedOperation(
            operation=value["operation"],
            store=value["store"],
            legacy_id=value["legacy_id"],
            idempotency_key=value["idempotency_key"],
            request=cast(dict[str, object], value["request"]),
        )
    except (MappingError, TypeError, ValueError) as error:
        raise ApplyError("plan_value_invalid") from error
    if not _canonical_uuid(planned.idempotency_key):
        raise ApplyError("plan_value_invalid")
    return PlanOperation(
        operation=planned.operation,
        store=planned.store,
        legacy_id=planned.legacy_id,
        idempotency_key=planned.idempotency_key,
        request=planned.request,
    )


def read_receipts(
    receipts_path: Path, *, expected_instance: str
) -> tuple[ApplyReceipt, ...]:
    if not receipts_path.exists():
        return ()
    try:
        raw = receipts_path.read_bytes()
    except OSError as error:
        raise ApplyError("receipts_unreadable") from error
    if raw and not raw.endswith(b"\n"):
        raise ApplyError("receipt_value_invalid")
    receipts: list[ApplyReceipt] = []
    seen: dict[tuple[str, str, str], ApplyReceipt] = {}
    for encoded in raw.splitlines():
        try:
            value = json.loads(encoded)
            receipt = _receipt_value(value)
        except (ValueError, TypeError) as error:
            raise ApplyError("receipt_value_invalid") from error
        if receipt.instance_id != expected_instance:
            raise ApplyError("receipt_instance_mismatch")
        identity = (receipt.operation, receipt.store, receipt.legacy_id)
        if identity in seen:
            raise ApplyError("receipt_conflict")
        seen[identity] = receipt
        receipts.append(receipt)
    return tuple(receipts)


def _receipt_value(value: object) -> ApplyReceipt:
    if not isinstance(value, dict) or set(value) != {
        "instance_id",
        "operation",
        "store",
        "legacy_id",
        "idempotency_key",
        "assertion_id",
        "fact_ids",
    }:
        raise ValueError("receipt shape")
    fact_ids = value["fact_ids"]
    if not isinstance(fact_ids, list) or not fact_ids:
        raise ValueError("fact ids")
    strings = (
        value["instance_id"],
        value["operation"],
        value["store"],
        value["legacy_id"],
        value["idempotency_key"],
        *fact_ids,
    )
    if not all(isinstance(item, str) and item for item in strings):
        raise ValueError("receipt string")
    if value["store"] not in ASSERTION_STORES:
        raise ValueError("receipt store")
    if value["operation"] not in {"ingest", "invalidate"}:
        raise ValueError("receipt operation")
    if not _canonical_uuid(value["instance_id"], version=4):
        raise ValueError("receipt instance")
    assertion_id = value["assertion_id"]
    if assertion_id is not None and not _canonical_uuid(assertion_id, version=4):
        raise ValueError("receipt assertion")
    if (value["operation"] == "ingest") is (assertion_id is None):
        raise ValueError("receipt operation result")
    if not _canonical_uuid(value["idempotency_key"]):
        raise ValueError("receipt idempotency")
    if not all(_canonical_uuid(identity, version=4) for identity in fact_ids):
        raise ValueError("receipt facts")
    return ApplyReceipt(
        instance_id=cast(str, value["instance_id"]),
        operation=cast(str, value["operation"]),
        store=cast(str, value["store"]),
        legacy_id=cast(str, value["legacy_id"]),
        idempotency_key=cast(str, value["idempotency_key"]),
        assertion_id=cast(str | None, assertion_id),
        fact_ids=tuple(cast(list[str], fact_ids)),
    )


def _response_receipt(
    operation: PlanOperation, response: Response, *, instance_id: str
) -> ApplyReceipt:
    if response.status_code != 200:
        raise ApplyError("request_failed", detail=_failure_code(response))
    try:
        payload = response.json()
        result = payload["result"]
        assertion_id = (
            result["assertion_id"] if operation.operation == "ingest" else None
        )
        receipt = _receipt_value(
            {
                "instance_id": instance_id,
                "operation": operation.operation,
                "store": operation.store,
                "legacy_id": operation.legacy_id,
                "idempotency_key": operation.idempotency_key,
                "assertion_id": assertion_id,
                "fact_ids": result["fact_ids"],
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ApplyError("response_invalid") from error
    if payload.get("outcome") not in {"committed", "replayed"}:
        raise ApplyError("response_invalid")
    return receipt


def _failure_code(response: Response) -> str | None:
    payload = _failure_payload(response)
    if payload is None:
        return None
    code = payload.get("code")
    return code if isinstance(code, str) and code in _SERVER_FAILURE_CODES else None


def _failure_payload(response: Response) -> Mapping[str, object] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload) != {"failure"}:
        return None
    failure = payload.get("failure")
    return cast(Mapping[str, object], failure) if isinstance(failure, dict) else None


def _append_receipt(path: Path, receipt: ApplyReceipt) -> None:
    payload = {
        "instance_id": receipt.instance_id,
        "operation": receipt.operation,
        "store": receipt.store,
        "legacy_id": receipt.legacy_id,
        "idempotency_key": receipt.idempotency_key,
        "assertion_id": receipt.assertion_id,
        "fact_ids": list(receipt.fact_ids),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as target:
            target.write((canonical_json(payload) + "\n").encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
    except OSError as error:
        raise ApplyError("receipts_unreadable") from error
