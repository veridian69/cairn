"""Bounded inert JSON output; pretty JSON is the human-readable variant.

Serialise completely before a caller writes anything. This module never infers
custody, changes status, executes content, or acknowledges a displayed visit.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import cast
from uuid import UUID

from cairn.catalogue.sqlite import CatalogueStorageError, canonical_timestamp

MAX_OUTPUT_BYTES = 1048576
_COMMANDS = frozenset(
    {
        "check",
        "arrive",
        "recall",
        "acknowledge-visit",
        "remember",
        "status",
        "resume",
        "abandon",
        "history",
        "correct",
        "disagree",
        "propose",
        "proposal-list",
        "proposal-read",
        "proposal-accept",
        "proposal-reject",
        "suggest",
    }
)


class CommandOutputError(ValueError):
    """Closed output failures without raw values, paths or object reprs."""


def render_result(command: str, result: object, *, human: bool = False) -> bytes:
    if type(command) is not str or command not in _COMMANDS or type(human) is not bool:
        raise CommandOutputError("invalid_output")
    remaining_nodes = 16384
    remaining_text_bytes = MAX_OUTPUT_BYTES

    def plain(value: object, depth: int = 0) -> object:
        nonlocal remaining_nodes, remaining_text_bytes
        remaining_nodes -= 1
        if depth > 32 or remaining_nodes < 0:
            raise CommandOutputError("invalid_output")
        if value is None or type(value) in {bool, int}:
            return value
        if type(value) is str:
            remaining_text_bytes -= len(value.encode("utf-8"))
            if remaining_text_bytes < 0:
                raise CommandOutputError("output_too_large")
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise CommandOutputError("invalid_output")
            return value
        if isinstance(value, Enum):
            return plain(value.value, depth + 1)
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CommandOutputError("invalid_output")
            return canonical_timestamp(value)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: plain(getattr(value, field.name), depth + 1)
                for field in fields(value)
            }
        if isinstance(value, Mapping):
            output = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise CommandOutputError("invalid_output")
                plain(key, depth + 1)
                output[key] = plain(item, depth + 1)
            return output
        if type(value) in {tuple, list}:
            return [
                plain(item, depth + 1)
                for item in cast(tuple[object, ...] | list[object], value)
            ]
        raise CommandOutputError("invalid_output")

    try:
        document = {
            "schema": "cairn.memory-command/v1",
            "command": command,
            "result": plain(result),
        }
        encoder = json.JSONEncoder(
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            indent=2 if human else None,
            separators=None if human else (",", ":"),
        )
        output = bytearray()
        for chunk in encoder.iterencode(document):
            encoded = chunk.encode("utf-8")
            if len(output) + len(encoded) + 1 > MAX_OUTPUT_BYTES:
                raise CommandOutputError("output_too_large")
            output.extend(encoded)
        output.extend(b"\n")
        return bytes(output)
    except CommandOutputError:
        raise
    except (
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        CatalogueStorageError,
    ):
        raise CommandOutputError("invalid_output") from None
