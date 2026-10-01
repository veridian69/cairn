"""Evidence-backed storyboard: every EXACT EXCERPT must appear verbatim in the logs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from scripts.real_work_demo import agents, logs

# Canonical chapter indices: excerpts.json keys, rail timing and wall times use
# these. A scene's "chapter" is its position among the chapters actually shown.
CHAPTERS: tuple[tuple[str, int], ...] = (
    ("A real bug", 3), ("Dead end", 15), ("Handoff", 8), ("Fix", 15),
    ("Dispute", 10), ("Verify", 18), ("Correction", 12), ("Validated", 12),
    ("Memory only", 15), ("Human gate", 7),
)  # fmt: skip
# Chapter index at which each chip kind first appears on the rail.
_APPEARS = {"failed": 1, "candidate": 3, "disagreement": 4, "validated": 5}
# The turn whose wall time each chapter declares.
_TURN_OF_CHAPTER = {
    1: "t1-val",
    2: "t1-val",
    3: "t2-spike",
    4: "t3-val",
    5: "t4-verifier",
    6: "t5-spike-correction",
    7: "t6-verifier-recheck",
    8: "t7-spike-cold",
}
LABELS = ("EXACT EXCERPT", "ACTUAL REPLY", "SUMMARY")


class ExcerptMissing(Exception):
    pass


@dataclass(frozen=True)
class Chip:
    fact_id: str
    kind: str
    principal: str
    linked_from: str | None
    # The chapter of the turn that created it; None falls back to its kind's chapter.
    chapter: int | None = None


# A turn's first chapter: where anything it creates may first appear on the rail.
_FIRST_CHAPTER = {
    "t1-val": 1,
    "t2-spike": 3,
    "t3-val": 4,
    "t4-verifier": 5,
    "t5-spike-correction": 6,
    "t6-verifier-recheck": 7,
    "t7-spike-cold": 8,
}
_CREATES = {"cairn_low.ingest", "cairn_low.promote", "memory.disagree"}


def exact(logs_text: str, excerpt: str) -> str:
    if excerpt not in logs_text:
        raise ExcerptMissing(excerpt[:80])
    return excerpt


def rail_after(chapter: int, events: list[Chip]) -> list[Chip]:
    return [
        c
        for c in events
        if (c.chapter if c.chapter is not None else _APPEARS[c.kind]) <= chapter
    ]


def created_in(out: Path, turns: list[dict[str, Any]], ids: set[str]) -> dict[str, int]:
    """Chapter of the first turn whose successful create call returned each ID."""
    found: dict[str, int] = {}
    for turn in turns:
        chapter = _FIRST_CHAPTER.get(turn["prefix"])
        if chapter is None or turn.get("skipped"):
            continue
        for call in logs.calls(out / f"{turn['prefix']}.stdout.jsonl"):
            if call.name not in _CREATES or call.is_error:
                continue
            text = json.dumps(call.result)
            for identifier in ids - set(found):
                if identifier in text:
                    found[identifier] = chapter
    return found


def _logs_text(out: Path, turns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for turn in turns:
        if turn.get("skipped"):
            continue
        for call in logs.calls(out / f"{turn['prefix']}.stdout.jsonl"):
            parts.append(json.dumps(call.arguments, ensure_ascii=False))
            parts.append(
                call.result
                if isinstance(call.result, str)
                else json.dumps(call.result, ensure_ascii=False)
            )
        final = out / f"{turn['prefix']}-final.txt"
        if final.exists():
            parts.append(final.read_text())
    # Chapter 8's subject is the file the cold session wrote, not a tool argument.
    pr = out / "workspaces" / "t7-spike-cold" / "PR.md"
    if pr.exists():
        parts.append(pr.read_text())
    return "\n".join(parts)


def chips(catalogue: Path, principals: dict[str, str]) -> list[Chip]:
    names = {v: k for k, v in principals.items()}
    with closing(sqlite3.connect(f"file:{catalogue}?mode=ro", uri=True)) as con:
        facts = con.execute(
            "SELECT f.fact_id, f.trust, f.derived_from, f.promoted_by, a.principal_id "
            "FROM facts f LEFT JOIN assertions a ON a.assertion_id = f.assertion_id ORDER BY f.rowid"
        ).fetchall()
        disagreements = con.execute(
            "SELECT relationship_id, left_fact_id, principal_id FROM memory_disagreements ORDER BY rowid"
        ).fetchall()
    out: list[Chip] = []
    for fact_id, trust, derived_from, promoted_by, author in facts:
        who = names.get(promoted_by or author, "?")
        if trust == "failed-approach":
            out.append(Chip(fact_id, "failed", who, None))
        elif trust == "candidate" and who in ("val", "spike"):
            out.append(Chip(fact_id, "candidate", who, None))
        elif trust == "validated":
            out.append(Chip(fact_id, "validated", who, derived_from))
    out += [
        Chip(rid, "disagreement", names.get(p, "?"), left)
        for rid, left, p in disagreements
    ]
    return out


def _wall(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60} m {whole % 60} s"


def build(out: Path) -> list[dict[str, Any]]:
    meta = json.loads((out / "run-metadata.json").read_text())
    text = _logs_text(out, meta["turns"])
    ran = [t for t in meta["turns"] if not t.get("skipped")]
    walls = {t["prefix"]: t["result"]["wall_s"] for t in ran}
    # The loop's chapters are shown only if its turns ran (spec Amendment A).
    shown = [
        (index, name, seconds)
        for index, (name, seconds) in enumerate(CHAPTERS)
        if _TURN_OF_CHAPTER.get(index) not in agents.LOOP
        or _TURN_OF_CHAPTER[index] in walls
    ]
    names = [name for _, name, _ in shown]
    panels: dict[str, list[dict[str, Any]]] = json.loads(
        (out / "excerpts.json").read_text()
    )
    summaries: dict[str, str] = json.loads((out / "summaries.json").read_text())
    events = chips(out / "snapshot" / "cairn" / "catalogue.sqlite3", meta["principals"])
    born = created_in(out, meta["turns"], {c.fact_id for c in events})
    events = [replace(c, chapter=born.get(c.fact_id)) for c in events]
    # A real run records more than the rail fits; an optional rail.json names the
    # records shown (kept in creation order) and every frame says so.
    rail_note = None
    curated = out / "rail.json"
    if curated.exists():
        keep = set(json.loads(curated.read_text()))
        total = len(events)
        events = [c for c in events if c.fact_id in keep]
        rail_note = f"{len(events)} of {total} memory records shown"
    scenes: list[dict[str, Any]] = []
    for position, (index, name, seconds) in enumerate(shown):
        chapter_panels = panels.get(str(index)) or [
            {"label": None, "title": name, "body": ""}
        ]
        turn = _TURN_OF_CHAPTER.get(index)
        wall = (
            f"T{turn[1]}: {_wall(walls[turn])}"
            if turn is not None and turn in walls
            else None
        )
        for panel in chapter_panels:
            label = panel["label"]
            if label == "SUMMARY":
                body = summaries[panel["summary"]]
            elif label in LABELS:
                body = exact(text, panel["body"])
            else:
                body = panel.get("body", "")
            scenes.append({
                "duration": round(seconds / len(chapter_panels), 2),
                "chapter": position, "eyebrow": f"{position:02d} / {name}", "chapters": names,
                "title": panel["title"], "label": label, "body": body,
                "rail": [asdict(c) for c in rail_after(index, events)], "wall": wall,
                "rail_note": rail_note, "gate": name == "Human gate",
            })  # fmt: skip
    return scenes
