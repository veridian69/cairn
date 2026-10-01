"""Per-turn CLI argv and MCP configuration for Claude Code and Codex."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPIKE_MODEL = "claude-fable-5-1"
VAL_MODEL = "gpt-6-sol"  # confirmed by Operator, 24 September 2026
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
GARDEN_TOOLS = ("status", "read_messages", "send_message")
WORK_MEMORY = ("diagnose", "recall", "history", "disagree", "resolve")
READ_MEMORY = ("diagnose", "recall", "history")


@dataclass(frozen=True)
class Turn:
    prefix: str
    actor: str
    provider: str
    garden: bool
    memory_tools: tuple[str, ...]
    low_tools: tuple[str, ...]


TURNS: tuple[Turn, ...] = (
    Turn("t1-val", "val", "codex", True, WORK_MEMORY, ("ingest", "read-evidence")),
    Turn("t2-spike", "spike", "claude", True, WORK_MEMORY, ("ingest", "read-evidence")),
    Turn("t3-val", "val", "codex", True, WORK_MEMORY, ("ingest", "read-evidence")),
    Turn(
        "t4-verifier",
        "verifier",
        "codex",
        True,
        WORK_MEMORY,
        ("ingest", "promote", "read-evidence"),
    ),
    # The correction loop (spec Amendment A): runs only if T4 promoted nothing.
    Turn(
        "t5-spike-correction",
        "spike",
        "claude",
        True,
        WORK_MEMORY,
        ("ingest", "read-evidence"),
    ),
    Turn(
        "t6-verifier-recheck",
        "verifier",
        "codex",
        True,
        WORK_MEMORY,
        ("ingest", "promote", "read-evidence"),
    ),
    Turn(
        "t7-spike-cold", "spike-cold", "claude", False, READ_MEMORY, ("read-evidence",)
    ),
)
LOOP = ("t5-spike-correction", "t6-verifier-recheck")


def claude_mcp(
    turn: Turn, cairn: str, token: str, garden_profile: Path | None, a2a: Path = Path()
) -> dict[str, Any]:
    auth = {"Authorization": f"Bearer {token}"}
    servers: dict[str, dict[str, Any]] = {
        "memory": {"type": "http", "url": cairn + "/memory/v1/mcp", "headers": auth},
        "cairn_low": {"type": "http", "url": cairn + "/v1/mcp", "headers": auth},
    }
    if turn.garden:
        assert garden_profile is not None
        servers["garden"] = {
            "type": "stdio",
            "command": str(a2a),
            "args": ["connect", "--profile", str(garden_profile)],
        }
    return {"mcpServers": servers}


def claude_allowed(turn: Turn) -> str:
    names = [f"mcp__memory__{t}" for t in turn.memory_tools]
    names += [f"mcp__cairn_low__{t}" for t in turn.low_tools]
    if turn.garden:
        names += [f"mcp__garden__{t}" for t in GARDEN_TOOLS]
    return ",".join([CLAUDE_TOOLS, *names])


def claude_settings(workspace: Path, writable: tuple[Path, ...] = ()) -> dict[str, Any]:
    return {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "allowLocalBinding": False},
        },
        "permissions": {"additionalDirectories": [str(workspace), *map(str, writable)]},
    }


def claude_argv(
    binary: Path,
    mcp_path: Path,
    settings_path: Path,
    max_turns: int,
    turn: Turn | None = None,
) -> list[str]:
    argv = [
        str(binary), "--print", "--restricted",
        "--tools", CLAUDE_TOOLS,
        "--strict-mcp-config", "--mcp-config", str(mcp_path),
        "--settings", str(settings_path),
        "--setting-sources", "",
        "--no-session-persistence", "--no-chrome",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--output-format", "stream-json", "--input-format", "text", "--verbose",
        "--model", SPIKE_MODEL, "--max-turns", str(max_turns),
    ]  # fmt: skip
    if turn is not None:
        argv += ["--allowedTools", claude_allowed(turn)]
    return argv


def codex_toml(
    turn: Turn,
    cairn: str,
    garden_profile: Path | None,
    a2a: Path,
    writable: tuple[Path, ...] = (),
) -> str:
    q = json.dumps
    lines = [
        'forced_login_method = "chatgpt"',
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        'web_search = "disabled"',
        'model_reasoning_effort = "high"',
        "",
        "[sandbox_workspace_write]",
        "network_access = false",
        f"writable_roots = {q([str(w) for w in writable])}",
        # /tmp and $TMPDIR are writable by default; the runtime lives under /tmp.
        "exclude_slash_tmp = true",
        "exclude_tmpdir_env_var = true",
        "",
        "[mcp_servers.memory]",
        f"url = {q(cairn + '/memory/v1/mcp')}",
        'bearer_token_env_var = "CAIRN_TOKEN"',
        f"enabled_tools = {q(list(turn.memory_tools))}",
        "",
        "[mcp_servers.cairn_low]",
        f"url = {q(cairn + '/v1/mcp')}",
        'bearer_token_env_var = "CAIRN_TOKEN"',
        f"enabled_tools = {q(list(turn.low_tools))}",
    ]
    servers = {"memory": turn.memory_tools, "cairn_low": turn.low_tools}
    if turn.garden:
        assert garden_profile is not None
        lines += [
            "",
            "[mcp_servers.garden]",
            f"command = {q(str(a2a))}",
            f"args = {q(['connect', '--profile', str(garden_profile)])}",
            f"enabled_tools = {q(list(GARDEN_TOOLS))}",
        ]
        servers["garden"] = GARDEN_TOOLS
    for server, tools in servers.items():
        for tool in tools:
            lines += [
                "",
                f"[mcp_servers.{server}.tools.{q(tool)}]",
                'approval_mode = "approve"',
            ]
    return "\n".join(lines) + "\n"


def codex_argv(binary: Path, last_message: Path) -> list[str]:
    argv = [
        str(binary), "exec", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "--strict-config", "--json",
        "--model", VAL_MODEL, "--output-last-message", str(last_message),
    ]  # fmt: skip
    for feature in ("hooks", "apps", "multi_agent", "memories", "plugins"):
        argv += ["--disable", feature]
    return argv + ["-"]
