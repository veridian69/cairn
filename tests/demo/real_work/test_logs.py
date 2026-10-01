import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import logs  # noqa: E402


def write(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_claude_failing_bash_is_a_result_not_an_exception(tmp_path: Path) -> None:
    p = write(tmp_path / "c.jsonl", [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "pytest"}},
            {"type": "tool_use", "id": "b", "name": "mcp__cairn_low__ingest", "input": {"facts": []}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "2 failed"},
            {"type": "tool_result", "tool_use_id": "b", "content": "{\"outcome\": \"committed\"}"},
        ]}},
    ])  # fmt: skip
    calls = logs.calls(p)
    assert [(c.name, c.is_error) for c in calls] == [
        ("Bash", True),
        ("cairn_low.ingest", False),
    ]
    assert calls[0].result == "2 failed"
    assert calls[1].result == {"outcome": "committed"}


def test_codex_mcp_and_shell_calls(tmp_path: Path) -> None:
    p = write(tmp_path / "x.jsonl", [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest",
                                            "exit_code": 1, "aggregated_output": "1 failed"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "memory", "tool": "recall",
                                            "arguments": {"query": "x"}, "status": "completed", "error": None,
                                            "result": {"structured_content": {"facts": []}}}},
    ])  # fmt: skip
    calls = logs.calls(p)
    assert [(c.name, c.is_error) for c in calls] == [
        ("command_execution", True),
        ("memory.recall", False),
    ]
    assert calls[0].result == "1 failed"


def test_a_truncated_or_foreign_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    good = {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest",
                                               "exit_code": 0, "aggregated_output": "ok"}}  # fmt: skip
    p = tmp_path / "x.jsonl"
    p.write_text(
        "WARNING: something printed to stdout\n"
        + json.dumps(good) + "\n" + '{"type": "item.completed", "item": {"ty'
    )  # fmt: skip
    assert [c.name for c in logs.calls(p)] == ["command_execution"]
    assert logs.undecodable(p) == 2


def test_claude_events_whose_message_is_a_string_are_ignored(tmp_path: Path) -> None:
    p = write(tmp_path / "c.jsonl", [
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "notice", "message": "Rate limit approaching"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}]}},
    ])  # fmt: skip
    assert [c.name for c in logs.calls(p)] == ["Bash"]


def test_codex_timeline_keeps_messages_and_file_changes_in_order(
    tmp_path: Path,
) -> None:
    p = write(tmp_path / "x.jsonl", [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "I'll reproduce it."}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest",
                                            "exit_code": 1, "aggregated_output": "2 failed"}},
        {"type": "item.completed", "item": {"type": "file_change", "status": "completed",
                                            "changes": [{"path": "/w/diff.py", "kind": "update"}]}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ])  # fmt: skip
    entries = logs.timeline(p)
    assert [type(e).__name__ for e in entries] == ["Note", "ToolCall", "Note", "Note"]
    assert entries[0] == logs.Note("agent message", "I'll reproduce it.")
    call, usage = entries[1], entries[3]
    assert isinstance(call, logs.ToolCall) and isinstance(usage, logs.Note)
    assert call.name == "command_execution" and call.is_error
    assert entries[2] == logs.Note("file change", "update /w/diff.py")
    assert usage.kind == "usage" and '"output_tokens": 2' in usage.text


def test_claude_timeline_keeps_text_denials_and_the_result(tmp_path: Path) -> None:
    p = write(tmp_path / "c.jsonl", [
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "Checking Cairn first."},
            {"type": "tool_use", "id": "a", "name": "mcp__memory__correct", "input": {"x": 1}},
        ]}},
        {"type": "system", "subtype": "permission_denied", "tool_name": "mcp__memory__correct",
         "message": "Permission to use mcp__memory__correct has been denied"},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "denied"}]}},
        {"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.5},
    ])  # fmt: skip
    entries = logs.timeline(p)
    kinds = [e.kind if isinstance(e, logs.Note) else e.name for e in entries]
    assert kinds == ["agent message", "memory.correct", "permission denied", "usage"]
    call, denied, usage = entries[1], entries[2], entries[3]
    assert isinstance(call, logs.ToolCall) and call.is_error and call.result == "denied"
    assert isinstance(denied, logs.Note) and "mcp__memory__correct" in denied.text
    assert isinstance(usage, logs.Note) and '"total_cost_usd": 0.5' in usage.text
