"""Ephemeral host UI state for a write; custody and search are distinct."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from cairn.client.types import PersistenceReceipt


class PersistencePhase(StrEnum):
    PROCESSING = "processing"
    SAVED = "saved"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PersistenceProgress:
    """A failed attempt means custody is unconfirmed, not necessarily absent."""

    phase: PersistencePhase
    receipt: PersistenceReceipt | None = None
    failure_code: str | None = None
    searchability: Literal["unconfirmed", "not-applicable"] = "unconfirmed"
