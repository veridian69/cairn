"""Bounded stdin decoding for host commands; contents are data, never code."""

import json
from typing import BinaryIO, cast

from cairn.client.diagnostics import _unique_object
from cairn.client.types import freeze_object

MAX_INPUT_BYTES = 1048576


class CommandInputError(ValueError):
    """Closed local error codes without input, path or operating-system details."""


def read_text(stream: BinaryIO, *, limit: int) -> str:
    """Consume at most limit+1 bytes, preserving valid UTF-8 content exactly."""
    if type(limit) is not int or not 1 <= limit <= MAX_INPUT_BYTES:
        raise CommandInputError("invalid_input_limit")
    body = bytearray()
    try:
        while len(body) <= limit:
            chunk = stream.read(limit + 1 - len(body))
            if type(chunk) is not bytes:
                raise CommandInputError("input_unavailable")
            if not chunk:
                break
            if len(chunk) > limit - len(body):
                raise CommandInputError("input_too_large")
            body.extend(chunk)
    except CommandInputError:
        raise
    except (OSError, ValueError):
        raise CommandInputError("input_unavailable") from None
    try:
        return body.decode("utf-8")
    except UnicodeError:
        raise CommandInputError("invalid_input") from None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    for key, _ in pairs:
        key.encode("utf-8")
    return _unique_object(pairs)


def read_object(stream: BinaryIO, *, limit: int) -> dict[str, object]:
    """Decode an unambiguous finite JSON object; handlers validate its fields."""
    text = read_text(stream, limit=limit)
    try:
        value = json.loads(text, object_pairs_hook=_object)
        freeze_object(value)  # Shared finite-number and Unicode validation.
        return cast(dict[str, object], value)
    except (ValueError, TypeError, RecursionError):
        raise CommandInputError("invalid_input") from None
