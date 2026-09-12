"""Explicit Linux/WSL memory task console with automatic current-turn admission."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal, TextIO
from uuid import RFC_4122, UUID, uuid4

from cairn.client.chat_connection import checked_turn
from cairn.client.chat_host import (
    ChatActor,
    ChatHostError,
    ChatTurn,
    actor_auth_locks,
    recover_actor_auth,
    run_turn,
    validate_dedicated_auth_config,
)
from cairn.client.conversation_sources import MAX_SOURCE_BYTES
from cairn.client.diagnostics import _unique_object
from cairn.client.profiles import ProfileError, _read_regular, load_profile
from cairn.client.turn_receipts import TurnMemory


class ChatConfigError(ValueError):
    """Content-free console configuration failure."""


_BASE_ACTORS = ("val", "spike")
_PROVIDERS: dict[str, Literal["codex", "claude"]] = {
    "val": "codex",
    "spike": "claude",
}


def _path(value: object) -> Path:
    if type(value) is not str or not value or "\0" in value:
        raise ChatConfigError("invalid_chat_config")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_relative_to("/mnt"):
        raise ChatConfigError("native_absolute_path_required")
    return path


def load_config(path: Path) -> dict[str, ChatActor]:
    """Load explicit actors without reading provider credentials or launching."""
    try:
        document = json.loads(
            _read_regular(path, limit=32768, kind="chat_config"),
            object_pairs_hook=_unique_object,
        )
        if type(document) is not dict or set(document) != {"schema", *_BASE_ACTORS}:
            raise ChatConfigError("invalid_chat_config")
        if document["schema"] != "cairn.chat/v1":
            raise ChatConfigError("invalid_chat_config")
        actors = {}
        for name in _BASE_ACTORS:
            data = document[name]
            if type(data) is not dict or set(data) - {"assessment_model"} != {
                "executable",
                "auth_file",
                "auth_state_dir",
                "profile_path",
                "expected_principal",
                "model",
            }:
                raise ChatConfigError("invalid_chat_config")
            principal = UUID(data["expected_principal"])
            model = data["model"]
            if (
                str(principal) != data["expected_principal"]
                or principal.variant != RFC_4122
                or principal.version != 4
                or type(model) is not str
                or not model
                or len(model) > 100
                or any(not (c.isascii() and (c.isalnum() or c in "._-")) for c in model)
            ):
                raise ChatConfigError("invalid_chat_config")
            assessment_model = data.get("assessment_model")
            if assessment_model is not None and (
                type(assessment_model) is not str
                or not assessment_model
                or len(assessment_model) > 100
                or any(
                    not (c.isascii() and (c.isalnum() or c in "._-"))
                    for c in assessment_model
                )
            ):
                raise ChatConfigError("invalid_chat_config")
            actors[name] = ChatActor(
                provider=_PROVIDERS[name],
                executable=_path(data["executable"]),
                auth_file=_path(data["auth_file"]),
                auth_state_dir=_path(data["auth_state_dir"]),
                profile_path=_path(data["profile_path"]),
                expected_principal=principal,
                model=model,
                assessment_model=assessment_model,
            )
            validate_dedicated_auth_config(actors[name])
        profiles = {
            name: load_profile(actor.profile_path) for name, actor in actors.items()
        }
        val = profiles["val"]
        context = (
            val.expected_instance_id,
            val.endpoint,
            val.scope,
            val.classification,
        )
        if any(
            (
                profile.expected_instance_id,
                profile.endpoint,
                profile.scope,
                profile.classification,
            )
            != context
            for name, profile in profiles.items()
            if name != "val"
        ) or len({actor.expected_principal for actor in actors.values()}) != len(
            actors
        ):
            raise ChatConfigError("actor_context_mismatch")
        return actors
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
        raise ChatConfigError("invalid_chat_config") from None


@contextmanager
def session_actors(actors: dict[str, ChatActor]) -> Iterator[dict[str, ChatActor]]:
    """Hold dedicated auth state while giving a console fresh memory sessions."""
    with ExitStack() as stack:
        stack.enter_context(actor_auth_locks(actors.values()))
        temp = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="cairn-chat-session-", dir="/tmp")
        )
        result = {}
        for name, actor in actors.items():
            profile = load_profile(actor.profile_path)
            document = {
                "schema": "cairn.memory-profile/v1",
                "endpoint": profile.endpoint,
                "expected_instance_id": str(profile.expected_instance_id),
                "scope": {
                    "realm": profile.scope.realm,
                    "segments": [
                        {"kind": s.kind, "identifier": s.identifier}
                        for s in profile.scope.segments
                    ],
                },
                "classification": profile.classification.value,
                "credential_file": str(profile.credential_file),
                "session_id": str(uuid4()),
            }
            path = Path(temp) / (name + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "w") as stream:
                json.dump(document, stream)
            result[name] = replace(actor, profile_path=path)
        yield result


def _terminal(text: str) -> str:
    return "".join(
        c
        if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
        else f"\\u{ord(c):04x}"
        for c in text
    )


HELP = (
    "Cairn memory task console. Only successfully saved facts carry between turns; "
    "unsaved remarks and replies do not.\n"
    "Tools: Cairn memory only; no local files, shell or web.\n"
    "Each turn starts a fresh agent; replies appear when the turn finishes.\n"
    "A separate memory receipt reports verified saves or missing/uncertain persistence.\n"
    "Type a message, then /send on its own line. /cancel discards a draft.\n"
    "With no draft: /val, /spike, /help, /quit. EOF discards unsent text.\n"
    "Use /literal followed by a space to send a literal command line.\n"
)


def _help(actors: dict[str, ChatActor]) -> str:
    return HELP


_PREFLIGHT_FAILURES = frozenset(
    {
        "memory_connection_unavailable",
        "memory_context_mismatch",
        "memory_retrieve_unavailable",
    }
)
_PUBLIC_FAILURES = _PREFLIGHT_FAILURES | {
    "host_timeout",
    "host_output_limit",
    "host_failed",
    "host_launch_failed",
    "host_usage_limit",
    "host_authentication_failed",
    "host_configuration_failed",
    "host_auth_unavailable",
    "host_auth_refresh_failed",
    "host_auth_recovery_required",
    "host_auth_locked",
    "invalid_host_auth_state",
    "invalid_host_auth",
    "invalid_host_actor",
    "invalid_host_profile",
    "invalid_host_output",
    "invalid_host_task",
    "invalid_host_refresh_target",
}


def console(
    actors: dict[str, ChatActor],
    source: BinaryIO,
    output: TextIO,
    *,
    invoke: Callable[[ChatActor, bytes], str | ChatTurn] = run_turn,
) -> int:
    """Run a bounded input loop; never admit prior replies or retry a turn."""
    selected = "val"
    draft = bytearray()
    oversized = False

    def say(message: str) -> None:
        output.write(message)
        output.flush()

    say(_help(actors) + "Val selected.\n")
    while True:
        line = source.readline(MAX_SOURCE_BYTES + 16)
        if not line:
            if draft or oversized:
                say("Unsent draft discarded.\n")
            return 0
        if len(line) >= MAX_SOURCE_BYTES + 16 and not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_SOURCE_BYTES + 16)
            oversized = True
            draft.clear()
            say("Message exceeds 16 KiB. Use /cancel before starting again.\n")
            continue
        command = line.removesuffix(b"\n").removesuffix(b"\r")
        if command == b"/cancel":
            draft.clear()
            oversized = False
            say("Draft discarded.\n")
            continue
        if oversized:
            say("Oversized draft refused. Use /cancel.\n")
            continue
        actor_commands = {b"/" + name.encode("ascii") for name in actors}
        if not draft and command in actor_commands | {b"/help", b"/quit"}:
            if command == b"/quit":
                return 0
            if command == b"/help":
                say(_help(actors))
            else:
                selected = command[1:].decode("ascii")
                say(selected.capitalize() + " selected.\n")
            continue
        if command == b"/send":
            task = bytes(draft)
            draft.clear()
            try:
                valid = bool(task.decode("utf-8").strip())
            except UnicodeError:
                valid = False
            if not valid:
                say("Message must be non-empty UTF-8; nothing sent.\n")
                continue
            say(selected.capitalize() + " is working…\n")
            started = time.monotonic()
            try:
                response = invoke(actors[selected], task)
            except (ChatHostError, OSError, ValueError) as error:
                code = str(error) if isinstance(error, ChatHostError) else "host_failed"
                if code not in _PUBLIC_FAILURES:
                    code = "host_failed"
                elapsed = time.monotonic() - started
                if code in _PREFLIGHT_FAILURES:
                    say("No agent launched; no turn writes were attempted.\n")
                else:
                    say(
                        "Turn unconfirmed. Stopped without retry; writes may have committed. "
                        "Use read-back and, if needed, manual reconciliation.\n"
                    )
                say(f"Reason: {code}; elapsed: {elapsed:.1f} s\n")
                details = getattr(error, "public_details", None)
                if isinstance(details, dict):
                    say(_terminal(json.dumps(details, sort_keys=True)) + "\n")
                return 1
            say(f"Turn completed in {time.monotonic() - started:.1f} s.\n")
            memory = (
                response.memory
                if isinstance(response, ChatTurn)
                else TurnMemory("unknown")
            )
            prose = response.response if isinstance(response, ChatTurn) else response
            say(selected.capitalize() + ":\n" + _terminal(prose) + "\n")
            if memory.status == "none":
                say(
                    "Memory receipt: No facts saved from this turn. New context in this turn will not carry to another agent.\n"
                )
            elif memory.status == "verified":
                count = len(memory.fact_ids)
                noun = "fact" if count == 1 else "facts"
                say(f"Memory receipt: {count} saved {noun} verified by read-back.\n")
            else:
                say(f"Memory receipt: Memory persistence is {memory.status}.\n")
                if memory.fact_ids:
                    say("Verified saved fact IDs: " + ", ".join(memory.fact_ids) + "\n")
                say(
                    "Stopped without retry. Check saved facts before repeating a write.\n"
                )
                return 1
            if isinstance(response, ChatTurn):
                if response.completion == "complete":
                    suffix = " after one repair" if response.repaired else ""
                    say(f"Memory check: passed{suffix} (model assessment).\n")
                else:
                    reason = response.completion_reason or "assessment_unavailable"
                    say("Memory check: incomplete (" + _terminal(reason) + ").\n")
                    for issue in response.completion_issues:
                        say("Memory check detail: " + _terminal(issue) + "\n")
                    say(
                        "Stopped. Saved facts remain as shown above; memory completeness is unconfirmed.\n"
                    )
                    return 1
            continue
        if line.startswith(b"/literal "):
            line = line[len(b"/literal ") :]
        if len(draft) + len(line) > MAX_SOURCE_BYTES:
            draft.clear()
            oversized = True
            say("Message exceeds 16 KiB. Use /cancel before starting again.\n")
        else:
            draft.extend(line)


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--recover-auth", action="store_true")
    args = parser.parse_args()
    try:
        if sys.platform != "linux":
            raise ChatConfigError("linux_required")
        actors = load_config(args.config)
        if args.recover_auth:
            recover_actor_auth(actors.values())
            print("Dedicated authentication recovery completed.")
            raise SystemExit(0)
        with session_actors(actors) as current:
            status = console(current, sys.stdin.buffer, sys.stdout, invoke=checked_turn)
    except ChatHostError as error:
        guidance = {
            "host_auth_locked": "Authentication state is in use; stop the other console and retry.",
            "host_auth_recovery_required": (
                "Dedicated authentication recovery required; stop all consoles, log in "
                "again in the dedicated provider directory, then use --recover-auth."
            ),
        }.get(str(error), "Chat configuration unavailable or invalid.")
        print(guidance, file=sys.stderr)
        status = 2
    except (ChatConfigError, ProfileError, OSError, ValueError):
        print("Chat configuration unavailable or invalid.", file=sys.stderr)
        status = 2
    except KeyboardInterrupt:
        print(
            "\nStopped without retry; inspect Cairn if a turn was in progress.",
            file=sys.stderr,
        )
        status = 130
    raise SystemExit(status)


if __name__ == "__main__":
    run()
