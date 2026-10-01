"""Normalised tool calls from Claude stream-json and Codex --json logs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: object = None
    is_error: bool = False


def _maybe_json(text: object) -> object:
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _events(path: Path) -> list[dict[str, Any]]:
    """Decoded events; a killed turn or a stray stdout line must not stop the reader."""
    events = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def undecodable(path: Path) -> int:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return len(lines) - len(_events(path))


def claude_calls(path: Path) -> list[ToolCall]:
    by_id: dict[str, ToolCall] = {}
    order: list[str] = []
    for event in _events(path):
        message = event.get("message")
        # System notices (e.g. permission_denied) carry message as a plain string.
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block["name"]
                if name.startswith("mcp__"):
                    _, server, tool = name.split("__", 2)
                    name = f"{server}.{tool}"
                by_id[block["id"]] = ToolCall(name, block.get("input") or {})
                order.append(block["id"])
            elif (
                block.get("type") == "tool_result" and block.get("tool_use_id") in by_id
            ):
                call = by_id[block["tool_use_id"]]
                call.result = _maybe_json(block.get("content"))
                call.is_error = bool(block.get("is_error"))
    return [by_id[i] for i in order]


def codex_calls(path: Path) -> list[ToolCall]:
    out: list[ToolCall] = []
    for event in _events(path):
        if event.get("type") != "item.completed":
            continue
        item = event["item"]
        if item.get("type") == "command_execution":
            out.append(
                ToolCall(
                    "command_execution",
                    {"command": item.get("command")},
                    item.get("aggregated_output"),
                    item.get("exit_code") not in (0, None),
                )
            )
        elif item.get("type") == "mcp_tool_call":
            result = (item.get("result") or {}).get("structured_content")
            failed = item.get("status") != "completed" or item.get("error") is not None
            out.append(
                ToolCall(
                    f"{item['server']}.{item['tool']}",
                    item.get("arguments") or {},
                    result,
                    failed,
                )
            )
    return out


def calls(path: Path) -> list[ToolCall]:
    events = _events(path)
    if not events:
        return []
    return (
        claude_calls(path) if events[0].get("type") == "system" else codex_calls(path)
    )


@dataclass
class Note:
    """A non-call event worth keeping: an agent's words, a file edit, a denial, usage."""

    kind: str
    text: str


def _usage(event: dict[str, Any]) -> str:
    keep = {k: v for k, v in event.items() if k not in ("type", "result")}
    return json.dumps(keep, indent=2, ensure_ascii=False)


def timeline(path: Path) -> list[ToolCall | Note]:
    """Every call and note in the order the CLI emitted them; nothing summarised."""
    events = _events(path)
    if not events:
        return []
    entries: list[ToolCall | Note] = []
    if events[0].get("type") == "system":
        calls = iter(claude_calls(path))  # same order as the tool_use blocks below
        for event in events:
            message = event.get("message")
            if event.get("type") == "assistant" and isinstance(message, dict):
                for block in message.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        entries.append(Note("agent message", block["text"]))
                    elif block.get("type") == "tool_use":
                        entries.append(next(calls))
            elif event.get("subtype") == "permission_denied":
                entries.append(Note("permission denied", str(event.get("message"))))
            elif event.get("type") == "result":
                entries.append(Note("usage", _usage(event)))
        return entries
    calls = iter(codex_calls(path))
    for event in events:
        item = event.get("item") or {}
        if event.get("type") == "turn.completed":
            entries.append(Note("usage", _usage(event)))
        elif event.get("type") != "item.completed":
            continue
        elif item.get("type") in ("command_execution", "mcp_tool_call"):
            entries.append(next(calls))
        elif item.get("type") == "agent_message":
            entries.append(Note("agent message", str(item.get("text"))))
        elif item.get("type") == "file_change":
            for change in item.get("changes") or []:
                entries.append(
                    Note("file change", f"{change.get('kind')} {change.get('path')}")
                )
    return entries
