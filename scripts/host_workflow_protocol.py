"""Pure input grammar for the opt-in acceptance bridge, not a shell interface.

No file access or process launch happens here. The caller must still enforce
the reviewed immutable mounts, raw transport bounds and effective tool inventory.
"""

from dataclasses import dataclass

# This is the acceptance transport's independent ceiling, not a client import:
# loading cairn.client also initialises runtime dependencies with file reads.
MAX_INPUT_BYTES = 1048576
PROFILE_PATH = "/cli/profile.json"
COMMANDS = frozenset(
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
        "suggest",
        "propose",
        "proposal-list",
        "proposal-read",
        "proposal-accept",
        "proposal-reject",
    }
)
SKILL_PATHS = {
    "codex": "/work/.agents/skills/cairn-memory/SKILL.md",
    "claude": "/work/.claude/skills/cairn-memory/SKILL.md",
}


class WorkflowInputError(ValueError):
    """Only fixed local codes; never echo paths, content or runtime exceptions."""


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    argv: tuple[str, ...]
    stdin: bytes
    command: str | None
    help: bool


def validate_daily_request(
    value: object, *, allowed_commands: frozenset[str]
) -> CommandInvocation:
    """Admit only fixed-profile argv plus inert bounded UTF-8 stdin.

    The caller adds its pinned executable; neither executable nor environment
    comes from this request. Command-specific JSON is validated by the real CLI.
    """
    if (
        type(allowed_commands) is not frozenset
        or not allowed_commands
        or any(type(command) is not str for command in allowed_commands)
        or not allowed_commands <= COMMANDS
    ):
        raise WorkflowInputError("invalid_workflow_configuration")
    if type(value) is not dict or set(value) != {"argv", "stdin"}:
        raise WorkflowInputError("invalid_workflow_request")
    argv, content = value["argv"], value["stdin"]
    if (
        type(argv) is not list
        or not 1 <= len(argv) <= 3
        or any(type(arg) is not str or not 1 <= len(arg) <= 4096 for arg in argv)
        or type(content) is not str
        or len(content) > MAX_INPUT_BYTES
        or "\x00" in content
    ):
        raise WorkflowInputError("invalid_workflow_request")
    args = tuple(argv)
    command: str | None
    if args == ("--help",):
        command, help_requested = None, True
    elif len(args) == 2 and args[1] == "--help" and args[0] in allowed_commands:
        command, help_requested = args[0], True
    elif (
        len(args) == 3
        and args[:2] == ("--profile", PROFILE_PATH)
        and args[2] in allowed_commands
    ):
        command, help_requested = args[2], False
    else:
        raise WorkflowInputError("invalid_workflow_request")
    if (help_requested or command == "check") and content:
        raise WorkflowInputError("invalid_workflow_request")
    try:
        body = content.encode("utf-8")
    except UnicodeError:
        raise WorkflowInputError("invalid_workflow_request") from None
    if len(body) > MAX_INPUT_BYTES:
        raise WorkflowInputError("invalid_workflow_request")
    return CommandInvocation(args, body, command, help_requested)


def validate_skill_request(value: object, *, provider: str) -> str:
    """Return only the selected provider's exact installed skill path.

    The eventual fixed-file reader must independently verify immutable bytes
    and their expected digest; this grammar does not establish file integrity.
    """
    if type(provider) is not str or provider not in SKILL_PATHS:
        raise WorkflowInputError("invalid_workflow_configuration")
    expected = SKILL_PATHS[provider]
    if (
        type(value) is not dict
        or set(value) != {"path"}
        or type(value["path"]) is not str
        or value["path"] != expected
    ):
        raise WorkflowInputError("invalid_workflow_request")
    return expected
