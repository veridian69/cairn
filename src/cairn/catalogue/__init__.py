"""Authoritative catalogue capability."""

from cairn.catalogue.migration import (
    Advanced,
    Created,
    Current,
    MigrationError,
    MigrationResult,
    migrate_catalogue,
)
from cairn.catalogue.verification import (
    VerificationError,
    VerificationReport,
    verify_catalogue,
)

__all__ = [
    "Advanced",
    "Created",
    "Current",
    "MigrationError",
    "MigrationResult",
    "VerificationError",
    "VerificationReport",
    "migrate_catalogue",
    "verify_catalogue",
]
