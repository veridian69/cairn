"""Stable turn custody identity shared by authority and legacy clients."""

from uuid import UUID, uuid5

MEMORY_REMEMBER_NAMESPACE = UUID("4e14ee38-0e14-5069-8d36-502c18b1c324")


def remember_key(session_id: UUID, turn_id: UUID) -> UUID:
    return uuid5(MEMORY_REMEMBER_NAMESPACE, f"{session_id}:{turn_id}")
