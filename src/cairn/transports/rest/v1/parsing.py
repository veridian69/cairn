"""``Idempotency-Key`` header extraction — REST-specific per P-28.

Split from the shared admission pipeline by P-50: this is an HTTP header,
and I-85 makes the idempotency key an MCP tool argument instead, so it has
no transport-neutral counterpart. Everything else that used to live here —
``admit_body``, ``WireRejection``, the byte cap and the JSON parsing hooks
— moved to ``cairn.transports.v1.parsing``, which both transports call.
"""

from uuid import UUID

from starlette.datastructures import Headers

from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.wire import (
    RULE_IDEMPOTENCY_KEY_DUPLICATED,
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
)

_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def require_idempotency_key(headers: Headers) -> UUID:
    """Extracts the P-28 ``Idempotency-Key``: exactly one header carrying
    the canonical lowercase hyphenated text form of an RFC 4122 UUID.

    The round-trip comparison is the whole check: uppercase, braced,
    URN-prefixed and unhyphenated spellings all parse as UUIDs but do not
    reproduce themselves, so all are ``idempotency_key_malformed``.
    """
    values = headers.getlist(_IDEMPOTENCY_KEY_HEADER)
    if not values:
        raise WireRejection(400, RULE_IDEMPOTENCY_KEY_MISSING, _IDEMPOTENCY_KEY_HEADER)
    if len(values) > 1:
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_DUPLICATED, _IDEMPOTENCY_KEY_HEADER
        )
    submitted = values[0]
    try:
        parsed = UUID(submitted)
        if str(parsed) != submitted:
            raise ValueError
    except ValueError:
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_MALFORMED, _IDEMPOTENCY_KEY_HEADER
        ) from None
    return parsed


def forbid_idempotency_key(headers: Headers) -> None:
    """P-28: the header present on a read route is ``invalid_request``."""
    if headers.getlist(_IDEMPOTENCY_KEY_HEADER):
        raise WireRejection(
            400, RULE_IDEMPOTENCY_KEY_FORBIDDEN, _IDEMPOTENCY_KEY_HEADER
        )
