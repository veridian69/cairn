"""Recall-only bounded wire reader; semantic validation remains client-owned."""

import json
import math
from dataclasses import replace

import httpx

from cairn.client.diagnostics import _unique_object, safe_failure
from cairn.client.errors import FailureMetadata, RecallFailure
from cairn.transports.memory.models import PageFailureEnvelope


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_number")
    return number


def _page_failure(status_code: int, data: bytes, document: object) -> FailureMetadata:
    """Safe metadata plus the operation-local detail when the envelope is exact.

    Code, retry and correlation keep the safe decoder's closed vocabulary; the
    server's prose is never kept. Detail is trusted only on the documented
    refusal (HTTP 400, invalid_request, retry never); anything else falls back
    to that decoder.
    """
    failure = safe_failure(httpx.Response(status_code, content=data))
    if failure.correlation_id is None or (status_code, failure.code, failure.retry) != (
        400,
        "invalid_request",
        "never",
    ):
        return failure
    try:
        envelope = PageFailureEnvelope.model_validate(document)
    except ValueError:
        return failure
    detail = envelope.failure.detail.model_dump()
    return replace(failure, detail=tuple(sorted(detail.items())))


async def read_recall(
    response: httpx.Response,
    *,
    budget: int,
    operation: str = "recall",
    page_failures: bool = False,
) -> object:
    """Bound escaping, record separators and fixed metadata before JSON decoding.

    Canonical records cost at most 6B escaped bytes plus <=B array separators;
    4096 covers fixed keys, flags, bounded budget and both emitted policy strings,
    and a page's ordering, snapshot timestamps, cursor and page flags.
    Extra wire padding is refused. Failures retain the safe decoder's 16 KiB cap.
    With ``page_failures`` a failure may carry recall-page's operation-local
    detail (``PageFailureEnvelope``); every other failure keeps safe metadata.
    """
    if (
        response.headers.get("Content-Encoding", "identity").strip().lower()
        != "identity"
    ):
        raise ValueError("compressed_response")
    cap = 7 * budget + 4096 if response.status_code == 200 else 16384
    declared = response.headers.get("Content-Length")
    if declared is not None and (
        len(declared) > len(str(cap))
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
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_finite_float,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        if response.status_code == 200:
            raise
        # An unreadable error envelope retains the HTTP fallback, with no prose
        # or partially decoded metadata. Wire guard failures above stay invalid.
        raise RecallFailure(
            operation,
            safe_failure(httpx.Response(response.status_code, content=b"")),
        ) from None
    if response.status_code != 200:
        raise RecallFailure(
            operation,
            _page_failure(response.status_code, bytes(data), document)
            if page_failures
            else safe_failure(
                httpx.Response(response.status_code, content=bytes(data))
            ),
        )
    return document
