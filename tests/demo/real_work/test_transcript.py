import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import transcript  # noqa: E402

LONG_BODY = "Fix in b8e4848: " + "number_to_string guard. " * 300


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def run_dir(tmp_path: Path) -> Path:
    """A small but complete run: two turns that ran, one skipped, memory and code."""
    out = tmp_path / "out"
    out.mkdir()
    src = tmp_path / "src"
    ident = ["-c", "user.name=Spike", "-c", "user.email=spike@jclk.ch"]
    _git("init", "-q", "-b", "main", str(src))
    (src / "helper.py").write_text("def f():\n    return round(x)\n")
    _git("-C", str(src), *ident, "add", ".")
    _git("-C", str(src), *ident, "commit", "-qm", "base")
    base = _git("-C", str(src), "rev-parse", "HEAD")
    _git("-C", str(src), "checkout", "-qb", "fix/550")
    (src / "helper.py").write_text("def f():\n    return x  # only_numbers\n")
    _git("-C", str(src), *ident, "commit", "-qam", "Stop rounding datetimes")
    _git("clone", "-q", "--bare", str(src), str(out / "origin.git"))
    meta = {
        "label": "capture-3", "instance_id": "inst-1", "deepdiff_rev": base,
        "venv_lock_sha256": "lock-abc", "source_revision_at_start": "rev-start",
        "source_revision_at_end": "rev-end", "scope": {"realm": "acme"},
        "models_requested": {"spike": "claude-fable-5-1", "val": "gpt-6-sol"},
        "cli_versions": {"claude": "2.1.282 (Claude Code)"},
        "principals": {"val": "p-val", "spike": "p-spike", "verifier": "p-ver"},
        "grants": {"verifier": ["retrieve", "promote"]},
        "prompts": {"t1-val": "Work on deepdiff issue #550.", "t2-spike": "Finish the job."},
        "turns": [
            {"prefix": "t1-val", "actor": "val", "result": {"exit": 0, "timed_out": False, "wall_s": 61.0},
             "cost": {"claude_usd": 0.0, "codex_tokens": 1234}},
            {"prefix": "t2-spike", "actor": "spike", "result": {"exit": 0, "timed_out": False, "wall_s": 90.5},
             "cost": {"claude_usd": 5.41, "codex_tokens": 0}},
            {"prefix": "t5-spike-correction", "actor": "spike", "skipped": True},
        ],
    }  # fmt: skip
    (out / "run-metadata.json").write_text(json.dumps(meta))
    (out / "t1-val.stdout.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "I'll reproduce it."}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest",
                                            "exit_code": 1, "aggregated_output": "2 failed, 186 passed"}},
        {"type": "item.completed", "item": {"type": "file_change", "status": "completed",
                                            "changes": [{"path": "/w/diff.py", "kind": "update"}]}},
    ]))  # fmt: skip
    (out / "t1-val-final.txt").write_text("Pushed v2.")
    (out / "t2-spike.stdout.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Checking Cairn first."}]}},
        {"type": "system", "subtype": "permission_denied", "tool_name": "mcp__memory__correct",
         "message": "Permission to use mcp__memory__correct has been denied"},
    ]))  # fmt: skip
    checks = [{"number": 6, "name": "code truth re-observed", "passed": True,
               "detail": "suite_on_fix=True regression_fails_on_base=True"}]  # fmt: skip
    (out / "verification.json").write_text(json.dumps(
        {"checks": checks, "evidence_valid": True, "demo_usable": False,
         "beats": {"t3_disagreement": False}}))  # fmt: skip
    (out / "verification-ruling-a1.json").write_text(json.dumps(
        {"checks": checks, "evidence_valid": True, "demo_usable": True,
         "beats": {"worker_disagreement": True}}))  # fmt: skip
    db = out / "snapshot" / "cairn"
    db.mkdir(parents=True)
    with closing(sqlite3.connect(db / "catalogue.sqlite3")) as con:
        con.executescript(
            "CREATE TABLE facts (fact_id, trust, assertion_id, derived_from, promoted_by, evidence_id, body);"
            "CREATE TABLE assertions (assertion_id, principal_id);"
            "CREATE TABLE evidence_records (evidence_id, assertion_id);"
            "CREATE TABLE memory_disagreements (relationship_id, left_fact_id, right_fact_id, principal_id, reason);"
            "CREATE TABLE memory_resolutions (relationship_id, disagreement_id, selected_fact_id, evidence_id, principal_id, reason);"
            "INSERT INTO assertions VALUES ('a1','p-spike');"
            "INSERT INTO evidence_records VALUES ('ev-1','a1');"
            "INSERT INTO facts VALUES ('fact-1','candidate','a1',NULL,NULL,NULL,'" + LONG_BODY + "'),"
            "('fact-2','validated',NULL,'fact-1','p-ver','ev-9','promoted copy');"
            "INSERT INTO memory_disagreements VALUES ('dis-1','fact-1','fact-2','p-spike','10 of 30 fail');"
            "INSERT INTO memory_resolutions VALUES ('res-1','dis-1','fact-1','ev-9','p-ver','own rerun');"
        )  # fmt: skip
        con.commit()
    (out / "workspaces" / "t7-spike-cold").mkdir(parents=True)
    (out / "workspaces" / "t7-spike-cold" / "PR.md").write_text(
        "# Fix #550\n\nCites fact-2.\n"
    )
    (out / "snapshot" / "evidence").mkdir()
    (out / "snapshot" / "evidence" / "ev-9").write_text(
        "$ pytest -q\n30 passed in 1.2s\n"
    )
    return out


def test_the_transcript_is_complete_and_in_reading_order(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    order = ["## Run", "## Verdicts", "## Turns", "## Memory at the end of the run",
             "## Code on origin", "## Turn logs"]  # fmt: skip
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)
    for fact in ("inst-1", "lock-abc", "rev-start", "rev-end", "claude-fable-5-1",
                 "gpt-6-sol", "2.1.282 (Claude Code)", "p-ver"):  # fmt: skip
        assert fact in text


def test_both_verdicts_are_shown_and_labelled(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert "verification.json" in text and "verification-ruling-a1.json" in text
    assert "suite_on_fix=True regression_fails_on_base=True" in text
    assert "worker_disagreement" in text and "t3_disagreement" in text


def test_memory_is_listed_in_full_with_its_links(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert LONG_BODY in text  # never truncated
    assert "fact-2" in text and "derived from `fact-1`" in text and "ev-9" in text
    assert "evidence `ev-1`" in text  # an ingested fact's evidence, via its assertion
    assert "dis-1" in text and "10 of 30 fail" in text
    assert "res-1" in text and "own rerun" in text
    assert "spike" in text and "verifier" in text  # principals shown by name


def test_code_on_origin_shows_each_branch_with_its_full_diff(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert "fix/550" in text and "Stop rounding datetimes" in text
    assert "-    return round(x)" in text and "+    return x  # only_numbers" in text


def test_turn_logs_hold_the_prompt_and_every_event(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert "Work on deepdiff issue #550." in text and "Finish the job." in text
    assert "I'll reproduce it." in text and "Checking Cairn first." in text
    assert "update /w/diff.py" in text
    assert "Permission to use mcp__memory__correct has been denied" in text
    assert "2 failed, 186 passed" in text and "Pushed v2." in text
    assert "t5-spike-correction" in text and "skipped" in text
    t1 = text.index("### t1-val")
    assert text.index("I'll reproduce it.", t1) < text.index("2 failed, 186 passed", t1)


def test_evidence_payloads_are_reproduced_verbatim(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert "`ev-9`" in text and "$ pytest -q\n30 passed in 1.2s" in text
    assert "fixed test clock" in text  # catalogue times are not wall time


def test_the_original_verdict_comes_first_and_a_re_verification_is_labelled(
    tmp_path: Path,
) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert text.index("### `verification.json`") < text.index(
        "### `verification-ruling-a1.json`"
    )
    ruling = text.index("### `verification-ruling-a1.json`")
    assert "Re-verification" in text[ruling : ruling + 300]


def test_the_cold_sessions_pr_text_is_included_in_full(tmp_path: Path) -> None:
    text = transcript.render(run_dir(tmp_path))
    assert "## Pull request text written by the cold session" in text
    assert "# Fix #550\n\nCites fact-2." in text
