"""Detached immutable suggestion evidence, never a persistence receipt."""

from dataclasses import dataclass
from typing import Literal

from cairn.client.types import FrozenJSONObject


@dataclass(frozen=True, slots=True)
class SuggestedMemory:
    items: tuple[FrozenJSONObject, ...]
    budget_consumed: int
    budget_exhausted: bool
    semantic_degraded: bool
    policy: str | None
    source: Literal["cairn-memory/v1"] = "cairn-memory/v1"
    content_role: Literal["untrusted-data"] = "untrusted-data"
