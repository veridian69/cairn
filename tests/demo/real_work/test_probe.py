import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import probe  # noqa: E402


def test_parse_probe_reads_each_target() -> None:
    out = "noise\ngithub.com:443 denied\n1.1.1.1:443 reached\npypi.org:443 denied\n"
    assert probe.parse_probe(out) == {
        "github.com:443": "denied",
        "1.1.1.1:443": "reached",
        "pypi.org:443": "denied",
    }


def test_probe_script_covers_three_targets() -> None:
    for target in ("github.com", "1.1.1.1", "pypi.org"):
        assert target in probe.PROBE_SCRIPT


def test_only_tool_output_counts_not_the_model_reply() -> None:
    claude = (
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a",'
        '"content":"github.com:443 denied\\n1.1.1.1:443 denied\\npypi.org:443 denied\\n"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"github.com:443 reached"}]}}\n'
    )
    assert probe.parse_probe(probe.tool_outputs(claude)) == dict.fromkeys(
        probe.TARGETS, "denied"
    )
    codex = '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"pypi.org:443 reached\\n"}}\n'
    assert probe.parse_probe(probe.tool_outputs(codex)) == {"pypi.org:443": "reached"}


def test_write_probe_reports_push_and_outside_write() -> None:
    assert "git push" in probe.WRITE_SCRIPT and "outside" in probe.WRITE_SCRIPT
    out = "push ok\noutside denied\n"
    assert probe.parse_write(out) == {"push": "ok", "outside": "denied"}


def test_probe_script_includes_a_loopback_target() -> None:
    script = probe.probe_script(43210)
    for target in (*probe.TARGETS, "127.0.0.1:43210"):
        assert target.split(":")[0] in script
    assert probe.parse_probe("127.0.0.1:43210 denied\n") == {
        "127.0.0.1:43210": "denied"
    }


def test_config_rejection_is_recognised() -> None:
    rejected = "Error loading config.toml:\n/x/config.toml:58:1: unknown configuration field `a.b`\n"
    accepted = (
        '{"type":"thread.started"}\n{"type":"error","message":"401 Unauthorized"}\n'
    )
    assert probe.config_rejected(rejected) is True
    assert probe.config_rejected(accepted) is False


def test_tool_outputs_ignores_string_messages() -> None:
    log = '{"type":"system","subtype":"permission_denied","message":"Permission to use X"}\n'
    assert probe.tool_outputs(log) == ""
