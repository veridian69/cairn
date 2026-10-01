import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import agents  # noqa: E402

A2A = Path("/x/a2a")


def turn(prefix: str) -> agents.Turn:
    return next(t for t in agents.TURNS if t.prefix == prefix)


def test_turn_table_matches_spec() -> None:
    assert [(t.prefix, t.actor, t.provider, t.garden) for t in agents.TURNS] == [
        ("t1-val", "val", "codex", True),
        ("t2-spike", "spike", "claude", True),
        ("t3-val", "val", "codex", True),
        ("t4-verifier", "verifier", "codex", True),
        ("t5-spike-correction", "spike", "claude", True),
        ("t6-verifier-recheck", "verifier", "codex", True),
        ("t7-spike-cold", "spike-cold", "claude", False),
    ]
    for t in agents.TURNS:
        assert "retrieve" not in t.low_tools and "invalidate" not in t.low_tools
    assert turn("t7-spike-cold").memory_tools == ("diagnose", "recall", "history")
    assert all("check" not in t.memory_tools for t in agents.TURNS)
    assert turn("t7-spike-cold").low_tools == ("read-evidence",)


def test_the_correction_loop_reuses_the_t2_and_t4_tool_sets() -> None:
    assert agents.LOOP == ("t5-spike-correction", "t6-verifier-recheck")
    for new, old in (
        ("t5-spike-correction", "t2-spike"),
        ("t6-verifier-recheck", "t4-verifier"),
    ):
        assert (turn(new).memory_tools, turn(new).low_tools) == (
            turn(old).memory_tools, turn(old).low_tools,
        )  # fmt: skip


def test_cold_turn_has_no_garden_server() -> None:
    cfg = agents.claude_mcp(turn("t7-spike-cold"), "http://c", "tok", None)
    assert set(cfg["mcpServers"]) == {"memory", "cairn_low"}


def test_claude_argv_is_restricted_with_named_tools(tmp_path: Path) -> None:
    argv = agents.claude_argv(
        Path("/bin/claude"), tmp_path / "m.json", tmp_path / "s.json", 60
    )
    assert "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert argv[argv.index("--settings") + 1] == str(tmp_path / "s.json")


def test_claude_allowed_tools_follow_the_turn(tmp_path: Path) -> None:
    argv = agents.claude_argv(
        Path("/bin/claude"),
        tmp_path / "m.json",
        tmp_path / "s.json",
        60,
        turn("t7-spike-cold"),
    )
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert (
        "mcp__memory__recall" in allowed and "mcp__cairn_low__read-evidence" in allowed
    )
    assert not any(a.startswith("mcp__garden__") for a in allowed)
    assert "mcp__cairn_low__ingest" not in allowed


def test_claude_sandbox_settings_deny_network(tmp_path: Path) -> None:
    s = agents.claude_settings(tmp_path)
    assert s["sandbox"]["enabled"] is True
    assert s["sandbox"]["network"]["allowedDomains"] == []


def test_codex_toml_limits_tools_and_garden() -> None:
    text = agents.codex_toml(turn("t4-verifier"), "http://c", Path("/g.json"), A2A)
    assert 'sandbox_mode = "workspace-write"' in text
    assert "network_access = false" in text
    assert '"promote"' in text and '"retrieve"' not in text
    assert "[mcp_servers.garden]" in text


def test_codex_argv_uses_val_model(tmp_path: Path) -> None:
    argv = agents.codex_argv(Path("/bin/codex"), tmp_path / "last.txt")
    assert argv[argv.index("--model") + 1] == "gpt-6-sol"
    assert (
        "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    )


def test_origin_is_the_only_extra_writable_root(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    s = agents.claude_settings(tmp_path / "work", writable=(origin,))
    assert s["permissions"]["additionalDirectories"] == [
        str(tmp_path / "work"),
        str(origin),
    ]
    text = agents.codex_toml(
        turn("t2-spike"), "http://c", Path("/g.json"), A2A, writable=(origin,)
    )
    assert f'writable_roots = ["{origin}"]' in text


def test_codex_does_not_get_tmp_for_free() -> None:
    text = agents.codex_toml(turn("t1-val"), "http://c", Path("/g.json"), A2A)
    assert "exclude_slash_tmp = true" in text
    assert "exclude_tmpdir_env_var = true" in text
