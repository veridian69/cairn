"""Recall-only bounded wire reader; semantic validation remains client-owned."""

import json
import math

import httpx

from cairn.client.diagnostics import _unique_object, safe_failure
from cairn.client.errors import RecallFailure


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_number")
    return number


async def read_recall(response: httpx.Response, *, budget: int) -> object:
    """Bound escaping, record separators and fixed metadata before JSON decoding.

    Canonical records cost at most 6B escaped bytes plus <=B array separators;
    4096 covers fixed keys, flags, bounded budget and both emitted policy strings.
    Extra wire padding is refused. Failures retain the safe decoder's 16 KiB cap.
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
            "recall", safe_failure(httpx.Response(response.status_code, content=b""))
        ) from None
    if response.status_code != 200:
        raise RecallFailure(
            "recall",
            safe_failure(httpx.Response(response.status_code, content=bytes(data))),
        )
    return document
