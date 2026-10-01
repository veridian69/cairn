"""The four demo principals and their grants on a disposable Instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

# The test Instance fixes the realm; the repository segment names the target.
DEMO_SCOPE: dict[str, Any] = {
    "realm": "acme",
    "segments": [{"kind": "repository", "identifier": "deepdiff"}],
}

GRANTS: dict[str, tuple[str, ...]] = {
    "val": ("ingest", "retrieve"),
    "spike": ("ingest", "retrieve"),
    "verifier": ("ingest", "promote", "retrieve"),
    "spike-cold": ("retrieve",),
}


@dataclass(frozen=True)
class Actor:
    name: str
    principal: UUID
    token: str
    token_path: Path


def create_actors(instance: Any, runtime: Path) -> dict[str, Actor]:
    made: dict[str, Actor] = {}
    for name, operations in GRANTS.items():
        principal, token = instance.add_actor(
            label=name, operations=list(operations), segments=DEMO_SCOPE["segments"]
        )
        path = runtime / f"{name}.token"
        path.write_text(token + "\n")
        path.chmod(0o600)
        made[name] = Actor(name, principal, token, path)
    return made
