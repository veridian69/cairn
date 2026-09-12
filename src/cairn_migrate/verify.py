"""Verify migration receipts through retrieval, replay and audit reads."""

from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from httpx import AsyncClient, Response

from cairn_migrate.apply import (
    ApplyReceipt,
    PlanOperation,
    _is_after_delay,
    _post_with_retry,
    _response_receipt,
    _retry_delay,
    read_plan,
    read_receipts,
)
from cairn_migrate.mapping import canonical_json

VERIFY_ERROR_CODES = frozenset(
    {
        "receipt_missing",
        "receipt_conflict",
        "retrieve_failed",
        "retrieve_mismatch",
        "replay_failed",
        "audit_failed",
        "audit_mismatch",
        "response_invalid",
    }
)


class VerifyError(Exception):
    """A privacy-safe, closed refusal from the verification boundary."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in VERIFY_ERROR_CODES:
            raise ValueError(f"unknown verify error code: {code}")
        self.code = code
        self.detail = detail
        suffix = "" if detail is None else f": {detail}"
        super().__init__(f"verify error: {code}{suffix}")


@dataclass(frozen=True, slots=True)
class VerifySummary:
    receipts: int
    retrieved: int
    replayed: int
    audited: int


async def verify_plan(
    *,
    plan_path: Path,
    receipts_path: Path,
    expected_instance: str,
    client: AsyncClient,
    credential: str,
    sample_size: int = 25,
    skip_retrieval: bool = False,
    sleep: Callable[[float], Awaitable[None]] = async_sleep,
    max_attempts: int = 3,
) -> VerifySummary:
    """Verify a deterministic prefix sample and every receipt's audit key."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    operations = read_plan(plan_path)
    receipts = read_receipts(receipts_path, expected_instance=expected_instance)
    paired = _pair_receipts(operations, receipts)
    replay_samples = paired[:sample_size]
    invalidated_fact_ids = frozenset(
        fact_id
        for operation, receipt in paired
        if operation.operation == "invalidate"
        for fact_id in receipt.fact_ids
    )
    retrieval_samples = (
        ()
        if skip_retrieval
        else tuple(
            pair
            for pair in paired
            if pair[0].operation == "ingest"
            and any(fact_id not in invalidated_fact_ids for fact_id in pair[1].fact_ids)
        )[:sample_size]
    )

    for operation, receipt in retrieval_samples:
        await _verify_retrieval(
            operation,
            receipt,
            excluded_fact_ids=invalidated_fact_ids,
            client=client,
            credential=credential,
            sleep=sleep,
            max_attempts=max_attempts,
        )

    events_before = await _read_all_audit_events(client=client, credential=credential)
    if any(
        _audit_event_count(events_before, operation, receipt, replay=False) != 1
        for operation, receipt in paired
    ):
        raise VerifyError("audit_mismatch")
    replay_counts_before = {
        _receipt_identity(receipt): _audit_event_count(
            events_before, operation, receipt, replay=True
        )
        for operation, receipt in replay_samples
    }

    for operation, receipt in replay_samples:
        response = await _post_with_retry(
            operation,
            client=client,
            credential=credential,
            sleep=sleep,
            max_attempts=max_attempts,
        )
        replay = _response_receipt(operation, response, instance_id=expected_instance)
        if response.json().get("outcome") != "replayed" or replay != receipt:
            raise VerifyError("replay_failed")

    events_after = await _read_all_audit_events(client=client, credential=credential)
    if any(
        _audit_event_count(events_after, operation, receipt, replay=False) != 1
        for operation, receipt in paired
    ):
        raise VerifyError("audit_mismatch")
    for operation, receipt in replay_samples:
        if _audit_event_count(events_after, operation, receipt, replay=True) != (
            replay_counts_before[_receipt_identity(receipt)] + 1
        ):
            raise VerifyError("audit_mismatch")

    return VerifySummary(
        receipts=len(receipts),
        retrieved=len(retrieval_samples),
        replayed=len(replay_samples),
        audited=len(paired),
    )


def _pair_receipts(
    operations: tuple[PlanOperation, ...], receipts: tuple[ApplyReceipt, ...]
) -> tuple[tuple[PlanOperation, ApplyReceipt], ...]:
    indexed = {_receipt_identity(receipt): receipt for receipt in receipts}
    if len(indexed) != len(receipts):
        raise VerifyError("receipt_conflict")
    paired: list[tuple[PlanOperation, ApplyReceipt]] = []
    for operation in operations:
        receipt = indexed.get(
            (operation.operation, operation.store, operation.legacy_id)
        )
        if receipt is None:
            raise VerifyError("receipt_missing")
        if receipt.idempotency_key != operation.idempotency_key:
            raise VerifyError("receipt_conflict")
        if (operation.operation == "ingest") is (receipt.assertion_id is None):
            raise VerifyError("receipt_conflict")
        paired.append((operation, receipt))
    if len(paired) != len(receipts):
        raise VerifyError("receipt_conflict")
    return tuple(paired)


def _receipt_identity(receipt: ApplyReceipt) -> tuple[str, str, str]:
    return receipt.operation, receipt.store, receipt.legacy_id


async def _verify_retrieval(
    operation: PlanOperation,
    receipt: ApplyReceipt,
    *,
    excluded_fact_ids: frozenset[str],
    client: AsyncClient,
    credential: str,
    sleep: Callable[[float], Awaitable[None]],
    max_attempts: int,
) -> None:
    request = operation.request
    scope = request.get("scope")
    facts = request.get("facts")
    trust = request.get("requested_trust", "candidate")
    if (
        not isinstance(scope, dict)
        or not isinstance(facts, list)
        or len(facts) != len(receipt.fact_ids)
        or not isinstance(trust, str)
        or receipt.assertion_id is None
    ):
        raise VerifyError("response_invalid")
    for fact, fact_id in zip(facts, receipt.fact_ids, strict=True):
        if fact_id in excluded_fact_ids:
            continue
        if not isinstance(fact, dict) or not isinstance(fact.get("body"), str):
            raise VerifyError("response_invalid")
        body = cast(str, fact["body"])
        response = await _retrieve_with_retry(
            scope=scope,
            body=body,
            trust=trust,
            client=client,
            credential=credential,
            sleep=sleep,
            max_attempts=max_attempts,
        )
        if response.status_code != 200:
            raise VerifyError("retrieve_failed")
        if not _retrieval_matches(response, fact_id, receipt.assertion_id, body):
            raise VerifyError("retrieve_mismatch")


async def _retrieve_with_retry(
    *,
    scope: dict[object, object],
    body: str,
    trust: str,
    client: AsyncClient,
    credential: str,
    sleep: Callable[[float], Awaitable[None]],
    max_attempts: int,
) -> Response:
    content = canonical_json(
        {
            "scope": scope,
            "query": _bounded_query(body),
            "budget": 1_048_576,
            "trust_filters": [trust],
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, max_attempts + 1):
        response = await client.post("/v1/retrieve", content=content, headers=headers)
        if response.status_code == 200 or not _is_after_delay(response):
            return response
        if attempt < max_attempts:
            await sleep(_retry_delay(response))
    return response


def _bounded_query(body: str) -> str:
    raw = body.encode("utf-8")[:8192]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    raise VerifyError("response_invalid")


def _retrieval_matches(
    response: Response, fact_id: str, assertion_id: str, body: str
) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        return False
    return any(
        isinstance(hit, dict)
        and hit.get("fact_id") == fact_id
        and hit.get("assertion_id") == assertion_id
        and hit.get("body") == body
        for hit in payload["hits"]
    )


async def _read_all_audit_events(
    *, client: AsyncClient, credential: str
) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    after_sequence = 0
    while True:
        response = await client.post(
            "/v1/read-audit-events",
            content=canonical_json(
                {
                    "realm_id": "cairn",
                    "scope_prefix": [],
                    "after_sequence": after_sequence,
                    "limit": 500,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code != 200:
            raise VerifyError("audit_failed")
        try:
            payload = response.json()
            page = payload["events"]
            next_sequence = payload["next_after_sequence"]
        except (KeyError, TypeError, ValueError) as error:
            raise VerifyError("response_invalid") from error
        if not isinstance(page, list) or not all(
            isinstance(item, dict) for item in page
        ):
            raise VerifyError("response_invalid")
        events.extend(cast(list[dict[str, object]], page))
        if next_sequence is None:
            return tuple(events)
        if not isinstance(next_sequence, int) or next_sequence <= after_sequence:
            raise VerifyError("response_invalid")
        after_sequence = next_sequence


def _audit_event_count(
    events: tuple[dict[str, object], ...],
    operation: PlanOperation,
    receipt: ApplyReceipt,
    *,
    replay: bool,
) -> int:
    reason_code = (
        "assertion_ingested"
        if operation.operation == "ingest"
        else ("facts_invalidated")
    )
    affected_assertion_ids = (
        [] if receipt.assertion_id is None else [receipt.assertion_id]
    )
    count = 0
    for event in events:
        if (
            event.get("action_kind") != "data"
            or event.get("action_code") != operation.operation
            or event.get("outcome") != "allow"
            or event.get("idempotency_key") != receipt.idempotency_key
            or event.get("affected_assertion_ids") != affected_assertion_ids
            or event.get("affected_fact_ids") != list(receipt.fact_ids)
        ):
            continue
        replay_of = event.get("replay_of_mutation_id")
        if replay:
            matches = (
                event.get("reason_code") == "idempotent_replay"
                and isinstance(replay_of, str)
                and bool(replay_of)
            )
        else:
            matches = event.get("reason_code") == reason_code and replay_of is None
        count += matches
    return count
