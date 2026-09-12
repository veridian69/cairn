"""Explicit test-provider inputs for an isolated semantic evaluator process.

Does not discover configuration, launch providers or authenticate credentials.
The caller must pass the returned environment only to its dedicated child;
never print it or write it into an evidence report.
"""

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path


class EnvironmentError(ValueError):
    """Only closed, input-free failure codes leave this boundary."""


def provider_environment(
    key_file: Path, inherited: Mapping[str, str]
) -> dict[str, str]:
    """Read only the designated regular file; inherit no ambient service config."""
    descriptor: int | None = None
    try:
        descriptor = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EnvironmentError("test_provider_key_unavailable")
        raw = os.read(descriptor, 4097)
    except OSError:
        raise EnvironmentError("test_provider_key_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        key = raw.decode("ascii").strip()
    except UnicodeError:
        raise EnvironmentError("test_provider_key_invalid") from None
    if len(raw) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]{24,4096}", key):
        raise EnvironmentError("test_provider_key_invalid")
    environment = {
        name: inherited[name]
        for name in ("PATH", "LANG", "LC_ALL", "TZ")
        if name in inherited
    }
    environment.update(
        OPENAI_API_KEY=key,
        OTEL_SDK_DISABLED="true",
        PYTHONUNBUFFERED="1",
        GRAPHITI_TELEMETRY_ENABLED="false",
    )
    return environment


def falkordb_image(lock_text: str) -> str:
    """Parse a literal locked image reference; never source a shell file."""
    values = [
        line.removeprefix("FALKORDB_IMAGE=")
        for line in lock_text.splitlines()
        if line.startswith("FALKORDB_IMAGE=")
    ]
    if len(values) != 1 or not re.fullmatch(
        r"falkordb/falkordb:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}", values[0]
    ):
        raise EnvironmentError("image_pin_invalid")
    return values[0]
