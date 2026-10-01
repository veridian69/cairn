import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import storyboard as sb  # noqa: E402

LOG_LINE = "TypeError: type datetime.datetime doesn't define __round__ method"


def test_exact_excerpt_passes_and_near_miss_raises() -> None:
    assert (
        sb.exact(LOG_LINE, "doesn't define __round__ method")
        == "doesn't define __round__ method"
    )
    with pytest.raises(sb.ExcerptMissing):
        sb.exact(LOG_LINE, "doesn't  define __round__ method")
    with pytest.raises(sb.ExcerptMissing):
        sb.exact(LOG_LINE, "doesn't define __round__ method.")


def test_promotion_adds_a_linked_chip_and_keeps_the_candidate() -> None:
    events = [
        sb.Chip("f1", "failed", "val", None),
        sb.Chip("c1", "candidate", "spike", None),
        sb.Chip("d1", "disagreement", "val", "c1"),
        sb.Chip("v1", "validated", "verifier", "c1"),
    ]
    rail = sb.rail_after(5, events)
    kinds = {c.fact_id: c.kind for c in rail}
    assert kinds["c1"] == "candidate" and kinds["v1"] == "validated"
    assert next(c for c in rail if c.fact_id == "v1").linked_from == "c1"
    assert [c.kind for c in sb.rail_after(1, events)] == ["failed"]


def test_chapters_match_amendment_a_and_total_115s() -> None:
    assert [n for n, _ in sb.CHAPTERS] == [
        "A real bug", "Dead end", "Handoff", "Fix", "Dispute", "Verify",
        "Correction", "Validated", "Memory only", "Human gate",
    ]  # fmt: skip
    assert sum(s for _, s in sb.CHAPTERS) == 115
    assert sb.CHAPTERS[-1] == ("Human gate", 7)  # Operator, 25 September 2026: +3 s


def _run_dir(tmp_path: Path, panels: dict[str, Any], summaries: dict[str, Any]) -> Path:
    out = tmp_path
    principals = {
        "val": "p-val",
        "spike": "p-spike",
        "verifier": "p-ver",
        "spike-cold": "p-cold",
    }
    (out / "run-metadata.json").write_text(json.dumps({
        "principals": principals,
        "turns": [{"prefix": "t1-val", "actor": "val", "result": {"wall_s": 372.0}}],
    }))  # fmt: skip
    (out / "t1-val.stdout.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "pytest", "exit_code": 1,
        "aggregated_output": LOG_LINE}}) + "\n")  # fmt: skip
    (out / "excerpts.json").write_text(json.dumps(panels))
    (out / "summaries.json").write_text(json.dumps(summaries))
    db = out / "snapshot" / "cairn"
    db.mkdir(parents=True)
    with closing(sqlite3.connect(db / "catalogue.sqlite3")) as con:
        con.executescript(
            "CREATE TABLE facts (fact_id, trust, assertion_id, derived_from, promoted_by);"
            "CREATE TABLE assertions (assertion_id, principal_id);"
            "CREATE TABLE memory_disagreements (relationship_id, left_fact_id, right_fact_id, principal_id);"
            "INSERT INTO assertions VALUES ('a1','p-val'),('a2','p-spike');"
            "INSERT INTO facts VALUES ('f1','failed-approach','a1',NULL,NULL),"
            "('c1','candidate','a2',NULL,NULL),('v1','validated',NULL,'c1','p-ver');"
            "INSERT INTO memory_disagreements VALUES ('d1','c1','f1','p-val');"
        )
        con.commit()
    return out


def test_build_emits_scenes_with_rail_and_wall_time(tmp_path: Path) -> None:
    panels = {"1": [{"label": "EXACT EXCERPT", "title": "Val hits a wall", "body": "doesn't define __round__ method"}],
              "5": [{"label": "SUMMARY", "title": "Verified", "summary": "verify"}]}  # fmt: skip
    out = _run_dir(tmp_path, panels, {"verify": "The verifier re-ran everything."})
    scenes = sb.build(out)
    one = next(s for s in scenes if s["chapter"] == 1)
    assert (
        one["body"] == "doesn't define __round__ method"
        and one["wall"] == "T1: 6 m 12 s"
    )
    five = next(s for s in scenes if s["chapter"] == 5)
    assert (
        five["label"] == "SUMMARY" and five["body"] == "The verifier re-ran everything."
    )
    assert {c["kind"] for c in five["rail"]} == {
        "failed",
        "candidate",
        "disagreement",
        "validated",
    }
    assert (
        next(c for c in five["rail"] if c["kind"] == "validated")["linked_from"] == "c1"
    )
    # The loop did not run: its two chapters are dropped and the bar shows eight.
    assert sorted({s["chapter"] for s in scenes}) == list(range(8))
    assert scenes[0]["chapters"] == [
        "A real bug", "Dead end", "Handoff", "Fix", "Dispute", "Verify", "Memory only", "Human gate",
    ]  # fmt: skip


def test_build_refuses_an_excerpt_not_in_the_logs(tmp_path: Path) -> None:
    panels = {
        "1": [{"label": "EXACT EXCERPT", "title": "x", "body": "a line nobody printed"}]
    }
    with pytest.raises(sb.ExcerptMissing):
        sb.build(_run_dir(tmp_path, panels, {}))


def test_build_refuses_a_missing_summary(tmp_path: Path) -> None:
    panels = {"2": [{"label": "SUMMARY", "title": "x", "summary": "absent"}]}
    with pytest.raises(KeyError):
        sb.build(_run_dir(tmp_path, panels, {}))


def test_a_chip_appears_in_the_chapter_of_the_turn_that_created_it(
    tmp_path: Path,
) -> None:
    out = _run_dir(tmp_path, {}, {})
    meta = json.loads((out / "run-metadata.json").read_text())
    meta["turns"].append(
        {"prefix": "t3-val", "actor": "val", "result": {"wall_s": 90.0}}
    )
    (out / "run-metadata.json").write_text(json.dumps(meta))
    # Val's T1 root-cause candidate 'c1' and T3 counter-claim... here: c1 is created in T1.
    (out / "t1-val.stdout.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "cairn_low", "tool": "ingest", "arguments": {},
        "status": "completed", "error": None,
        "result": {"structured_content": {"result": {"fact_ids": ["c1"]}}}}}) + "\n")  # fmt: skip
    (out / "t3-val.stdout.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "memory", "tool": "disagree", "arguments": {},
        "status": "completed", "error": None,
        "result": {"structured_content": {"result": {"relationship_id": "d1"}}}}}) + "\n")  # fmt: skip
    scenes = sb.build(out)
    rail_at = {s["chapter"]: {c["fact_id"] for c in s["rail"]} for s in scenes}
    assert "c1" in rail_at[1]  # created in T1, shown from chapter 1
    assert "d1" not in rail_at[3] and "d1" in rail_at[4]  # created in T3


def test_the_cold_sessions_pr_text_is_excerptable(tmp_path: Path) -> None:
    panels = {
        "8": [
            {
                "label": "EXACT EXCERPT",
                "title": "PR",
                "body": "Rejected: excluding datetimes",
            }
        ]
    }
    out = _run_dir(tmp_path, panels, {})
    pr = out / "workspaces" / "t7-spike-cold"
    pr.mkdir(parents=True)
    (pr / "PR.md").write_text("## Summary\nRejected: excluding datetimes (fact f1).\n")
    six = next(s for s in sb.build(out) if s["chapter"] == 6)
    assert six["body"] == "Rejected: excluding datetimes"


def test_a_curated_rail_keeps_order_and_says_how_much_is_shown(tmp_path: Path) -> None:
    out = _run_dir(tmp_path, {}, {})
    (out / "rail.json").write_text(json.dumps(["v1", "f1"]))
    five = next(s for s in sb.build(out) if s["chapter"] == 5)
    assert [c["fact_id"] for c in five["rail"]] == [
        "f1",
        "v1",
    ]  # creation order, not list order
    assert five["rail_note"] == "2 of 4 memory records shown"


def test_without_a_curated_rail_every_chip_is_shown_and_no_note(tmp_path: Path) -> None:
    five = next(s for s in sb.build(_run_dir(tmp_path, {}, {})) if s["chapter"] == 5)
    assert len(five["rail"]) == 4 and five["rail_note"] is None


def test_the_loop_chapters_appear_when_the_loop_ran(tmp_path: Path) -> None:
    panels = {"7": [{"label": None, "title": "Validated", "body": ""}],
              "8": [{"label": None, "title": "PR", "body": ""}]}  # fmt: skip
    out = _run_dir(tmp_path, panels, {})
    meta = json.loads((out / "run-metadata.json").read_text())
    for prefix, actor in (
        ("t5-spike-correction", "spike"),
        ("t6-verifier-recheck", "verifier"),
    ):
        meta["turns"].append(
            {"prefix": prefix, "actor": actor, "result": {"wall_s": 61.0}}
        )
        (out / f"{prefix}.stdout.jsonl").write_text("")
    (out / "run-metadata.json").write_text(json.dumps(meta))
    scenes = sb.build(out)
    assert len(scenes[0]["chapters"]) == 10
    seven = next(s for s in scenes if s["chapter"] == 7)
    assert seven["title"] == "Validated" and seven["wall"] == "T6: 1 m 1 s"
    assert seven["eyebrow"] == "07 / Validated"
    assert next(s for s in scenes if s["chapter"] == 8)["title"] == "PR"


def test_skipped_turns_are_ignored_by_the_log_readers(tmp_path: Path) -> None:
    out = _run_dir(tmp_path, {}, {})
    meta = json.loads((out / "run-metadata.json").read_text())
    meta["turns"].append(
        {"prefix": "t5-spike-correction", "actor": "spike", "skipped": True}
    )
    (out / "run-metadata.json").write_text(json.dumps(meta))
    scenes = sb.build(out)  # no log file for the skipped turn: must not raise
    assert "Correction" not in scenes[0]["chapters"]


def test_only_the_human_gate_chapter_carries_the_gate_chip(tmp_path: Path) -> None:
    scenes = sb.build(_run_dir(tmp_path, {}, {}))
    assert {s["eyebrow"] for s in scenes if s["gate"]} == {"07 / Human gate"}
