"""Strict ``/v1`` body admission — the fail-before-custody boundary.

Everything here runs before any application call: the I-30 2 MiB
whole-request cap, content-type discipline per P-28 and
duplicate-key-rejecting JSON parsing per I-71. A refusal raises
``WireRejection``, which each transport's own error rendering turns into
its failure shape — REST under the I-73 status refinements (413 for the
cap, 415 for the media type); the future MCP adapter under the I-88
JSON-RPC error mapping.

Moved here transport-neutral per P-50, ahead of that MCP adapter: only
REST calls ``admit_body`` today (``cairn.transports.rest.v1.routes``), but
the module lives outside ``cairn.transports.rest`` so that when slice 7's
MCP frame-admission wrapper (I-86) lands, it calls the same function at the
same point in the same order rather than a reimplementation the two
transports could drift apart on. ``Idempotency-Key`` header extraction
stays in ``cairn.transports.rest.v1.parsing`` — it is an HTTP header, and
I-85 makes the idempotency key an MCP tool argument instead.

``validation_rejection`` joined it from ``rest/v1/errors.py`` with the
translation layer it serves (Task 7): the step after admission is the
strict wire model, and a refusal there is the same rule identity and
field path on both transports for the same reason a refusal here is.
"""

import json
import re

from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.requests import Request

from cairn.transports.v1.wire import (
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_INVALID_VALUE,
    RULE_MALFORMED_JSON,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
)

_WIRE_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")

# I-30: the complete request is capped at 2 MiB before JSON parsing.
MAX_REQUEST_BYTES = 2 * 1024 * 1024

_CONTENT_TYPE_HEADER = "Content-Type"


# Bounds a ``field_path`` that echoes a caller-authored key name (a
# duplicated or unknown JSON key is the caller's own text, not a schema
# pointer), so the reflection in ``detail`` cannot approach the 2 MiB body
# cap. The Task 4 review named the wider trap: for those two rules the
# value is caller content wearing a field-path costume, so it must never
# reach logs, metric labels or audit events (I-32) — Task 11's sweep
# drives corpus positives through it.
_FIELD_PATH_MAX_CHARACTERS = 128


class WireRejection(Exception):
    """An admission refusal, before any application call.

    Carries the refined HTTP status (I-73), the wire rule identity and the
    field path the ``invalid_request`` ``detail`` will name — never the
    submitted content, and never more than ``_FIELD_PATH_MAX_CHARACTERS``
    of a caller-authored key name. The exception message is
    ``wire rejection: <rule>`` — the rule identity and nothing else.
    """

    def __init__(self, status: int, rule: str, field_path: str) -> None:
        self.status = status
        self.rule = rule
        self.field_path = field_path[:_FIELD_PATH_MAX_CHARACTERS]
        super().__init__(f"wire rejection: {rule}")


async def admit_body(request: Request) -> dict[str, object]:
    """Admits a request body per P-28 and I-71, or raises ``WireRejection``.

    Order is deliberate: media type first (415 without reading the body),
    then the byte cap (413, checked against the declared length and again
    while streaming, so a lying ``Content-Length`` cannot bypass it), then
    UTF-8 decoding, then duplicate-key-rejecting JSON parsing.
    """
    _require_json_content_type(request.headers)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdecimal():
        if int(declared) > MAX_REQUEST_BYTES:
            raise WireRejection(413, RULE_BODY_TOO_LARGE, "body")

    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_REQUEST_BYTES:
            raise WireRejection(413, RULE_BODY_TOO_LARGE, "body")

    try:
        text = bytes(received).decode("utf-8")
    except UnicodeDecodeError:
        raise WireRejection(400, RULE_INVALID_ENCODING, "body") from None

    try:
        parsed: object = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except WireRejection:
        raise
    except (ValueError, RecursionError):
        # ``json.loads`` has two documented failure modes beyond
        # ``JSONDecodeError`` (itself a ``ValueError``): a pathologically
        # nested document raises ``RecursionError``, and an integer
        # literal over the interpreter's digit limit raises a bare
        # ``ValueError``. Both are well-formed-looking bodies inside the
        # 2 MiB cap, and both must refuse here rather than escape the
        # fail-before-custody boundary as an unhandled exception.
        raise WireRejection(400, RULE_MALFORMED_JSON, "body") from None

    if type(parsed) is not dict:
        raise WireRejection(400, RULE_BODY_NOT_OBJECT, "body")
    return parsed


def _require_json_content_type(headers: Headers) -> None:
    """P-28: ``application/json``, optionally ``charset=utf-8``; anything
    else — absent, duplicated, another type, another parameter — is 415."""
    if not _is_acceptable_content_type(headers.getlist(_CONTENT_TYPE_HEADER)):
        raise WireRejection(415, RULE_INVALID_CONTENT_TYPE, _CONTENT_TYPE_HEADER)


def _is_acceptable_content_type(values: list[str]) -> bool:
    # Stricter than RFC 7231 by design: a quoted parameter value
    # (``charset="utf-8"``) fails the exact token match and is 415. P-28
    # pins the token form only, and failing closed costs a conforming
    # client nothing — every mainstream HTTP library emits the bare token.
    if len(values) != 1:
        return False
    media_type, separator, remainder = values[0].partition(";")
    if media_type.strip().lower() != "application/json":
        return False
    if separator == "":
        return True
    if remainder.strip() == "":
        # A trailing semicolon — bare or followed only by whitespace — is
        # not the pinned token form: RFC 9110 requires ``parameter=value``
        # after ``;``, and no mainstream client emits it (post-acceptance
        # review, 8 August 2026).
        return False
    parameter, _, value = remainder.partition("=")
    return parameter.strip().lower() == "charset" and value.strip().lower() == "utf-8"


def _reject_non_finite(value: str) -> object:
    """``json.loads`` admits ``NaN``, ``Infinity`` and ``-Infinity`` by
    default; none is JSON (RFC 8259), and a non-finite float must refuse
    at admission rather than reach wire or domain validation
    (post-acceptance review, 8 August 2026). Raising ``ValueError`` lands
    in ``admit_body``'s malformed-JSON arm."""
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """I-71: duplicate JSON keys anywhere in the body are rejected during
    parsing, before model validation. The hook runs for every object at
    every nesting level, so the guarantee is document-wide."""
    admitted: dict[str, object] = {}
    for key, value in pairs:
        if key in admitted:
            raise WireRejection(400, RULE_DUPLICATE_JSON_KEY, key)
        admitted[key] = value
    return admitted


def validation_rejection(error: ValidationError) -> WireRejection:
    """Translates a strict wire-model refusal into a ``WireRejection``.

    Only the first error is reported, matching the custody seam's
    first-finding discipline; the closed rule vocabulary stays small by
    folding every shape mismatch that is neither an unknown nor a missing
    field into ``invalid_value``.

    Transport-neutral for P-50's stated reason, and moved here from
    ``rest/v1/errors.py`` when the translation layer it serves moved: it
    reads a Pydantic error and returns a ``WireRejection``, touching no
    part of HTTP. Both transports validate the same strict models, so
    both owe a caller the same rule identity and field path for the same
    malformed argument — which is what makes ``unknown_field`` mean one
    thing rather than two. The I-73 status table stays REST's (I-88); it
    is what turns this rejection into a status, and that is the part that
    does not cross over.
    """
    first = error.errors()[0]
    if first["type"] == "extra_forbidden":
        rule = RULE_UNKNOWN_FIELD
    elif first["type"] == "missing":
        rule = RULE_MISSING_FIELD
    else:
        rule = RULE_INVALID_VALUE
    return WireRejection(400, rule, _field_path(first["loc"]))


def _field_path(loc: tuple[int | str, ...]) -> str:
    """Joins a Pydantic location into the seam's field-path form —
    ``facts[0].body``, the command-level convention Task 3 fixed.

    Union-member tags are dropped: a failed union field's location embeds
    the Python class name of each tried member (``('evidence',
    'EvidenceIdBody', 'external_uri')``), and a class name is an
    implementation detail that must not enter the contract. Wire field
    names are snake_case per I-71, so the CapWords filter is exact.
    """
    rendered = ""
    for part in loc:
        if type(part) is int:
            rendered += f"[{part}]"
            continue
        text = str(part)
        if _WIRE_FIELD_NAME.fullmatch(text) is None:
            continue
        rendered = text if rendered == "" else f"{rendered}.{text}"
    return rendered if rendered else "body"
