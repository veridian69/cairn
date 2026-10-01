"""The full technical record of a run, from its own artefacts. Never truncated.

Sections: run provenance, every verdict, the turns, Cairn's memory at the end of the
run (facts, disagreements, resolutions, evidence payloads), the code each branch
pushed to origin, and every turn's prompt followed by each event in emitted order.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from scripts.real_work_demo import logs


def _block(value: object, lang: str = "") -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, indent=2, ensure_ascii=False)
    )
    runs = [len(m) for m in re.findall(r"`{3,}", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _run(meta: dict[str, Any]) -> list[str]:
    rows = [
        ("Label", meta.get("label")),
        ("Harness revision at start", meta.get("source_revision_at_start")),
        ("Harness revision at end", meta.get("source_revision_at_end")),
        ("Cairn instance", meta.get("instance_id")),
        ("Scope", json.dumps(meta.get("scope"))),
        ("deepdiff base revision", meta.get("deepdiff_rev")),
        ("Venv lock SHA-256", meta.get("venv_lock_sha256")),
        ("Models requested", json.dumps(meta.get("models_requested"))),
        ("CLI versions", json.dumps(meta.get("cli_versions"))),
        ("Principals", json.dumps(meta.get("principals"))),
        ("Grants", json.dumps(meta.get("grants"))),
    ]
    return [
        "## Run",
        "",
        *[f"- **{k}:** `{v}`" for k, v in rows if v not in (None, "null")],
        "",
    ]


def _verdicts(out: Path) -> list[str]:
    parts = ["## Verdicts", ""]
    reports = sorted(out.glob("verification*.json"),
                     key=lambda p: (p.name != "verification.json", p.name))  # fmt: skip
    for path in reports:
        report = json.loads(path.read_text())
        if path.name != "verification.json":
            parts += [f"### `{path.name}`", "",
                      "Re-verification of this same run directory, written after the "
                      "capture under a later ruling; the original report is above.", ""]  # fmt: skip
        else:
            parts += [
                f"### `{path.name}`",
                "",
                "Written by the harness at the end of the run.",
                "",
            ]
        parts += [
            f"- evidence_valid: `{report.get('evidence_valid')}`",
            f"- demo_usable: `{report.get('demo_usable')}`",
            f"- beats: `{json.dumps(report.get('beats'))}`",
            f"- timed_out: `{json.dumps(report.get('timed_out', []))}`", "",
            "| # | Check | Passed | Detail |", "|---|---|---|---|",
            *[f"| {c['number']} | {_cell(c['name'])} | {c['passed']} | {_cell(c['detail'])} |"
              for c in report.get("checks", [])],
            "",
        ]  # fmt: skip
    return parts


def _model(meta: dict[str, Any], actor: str) -> str:
    requested = meta.get("models_requested") or {}
    return str(requested.get("spike" if actor.startswith("spike") else "val", ""))


def _turns(meta: dict[str, Any]) -> list[str]:
    parts = ["## Turns", "", "| Turn | Actor | Model requested | Wall s | Exit | Timed out | Cost |",
             "|---|---|---|---|---|---|---|"]  # fmt: skip
    for t in meta["turns"]:
        if t.get("skipped"):
            parts.append(f"| {t['prefix']} | {t['actor']} | | | | | skipped |")
            continue
        r, cost = t["result"], t.get("cost") or {}
        spent = (f"USD {cost['claude_usd']}" if cost.get("claude_usd")
                 else f"{cost.get('codex_tokens', 0)} Codex tokens")  # fmt: skip
        parts.append(f"| {t['prefix']} | {t['actor']} | {_model(meta, t['actor'])} | "
                     f"{r['wall_s']} | {r['exit']} | {r['timed_out']} | {spent} |")  # fmt: skip
    return [*parts, ""]


def _memory(out: Path, meta: dict[str, Any]) -> list[str]:
    from scripts.real_work_demo.verify import Catalogue, load_evidence

    catalogue = out / "snapshot" / "cairn" / "catalogue.sqlite3"
    if not catalogue.exists():
        return []
    names = {v: k for k, v in (meta.get("principals") or {}).items()}

    def who(principal: object) -> str:
        return f"{names.get(str(principal), '?')} (`{principal}`)"

    parts = [
        "## Memory at the end of the run", "",
        "Read from the catalogue snapshot taken after the last turn. `recorded_at` "
        "values come from the disposable instance's fixed test clock, not wall time.", "",
        "### Facts", "",
    ]  # fmt: skip
    with Catalogue(catalogue) as cat:
        evidence = cat.evidence_by_fact()
        for f in cat.facts():
            author = f["principal_id"] or f["promoted_by"]
            links = [f"trust `{f['trust']}`", f"by {who(author)}"]
            if f["derived_from"]:
                links.append(f"derived from `{f['derived_from']}`")
            if f["fact_id"] in evidence:
                links.append(f"evidence `{evidence[f['fact_id']]}`")
            parts += [
                f"#### `{f['fact_id']}`",
                "",
                "; ".join(links),
                "",
                _block(f["body"] or ""),
                "",
            ]
        for title, rows in (("Disagreements", cat.disagreements()),
                            ("Resolutions", cat.resolutions())):  # fmt: skip
            parts += [f"### {title}", ""]
            for row in rows:
                parts.append(f"#### `{row['relationship_id']}`")
                parts.append("")
                for key in row.keys():
                    value = who(row[key]) if key == "principal_id" else f"`{row[key]}`"
                    parts.append(f"- {key}: {value}")
                parts.append("")
            if not rows:
                parts += ["None.", ""]
    payloads = load_evidence(out)
    parts += ["### Evidence payloads", "",
              "Exactly as the harness read them from Attic after the run.", ""]  # fmt: skip
    for evidence_id in sorted(payloads):
        parts += [f"#### `{evidence_id}`", "", _block(payloads[evidence_id]), ""]
    return parts


def _git(origin: Path, *args: str) -> str:
    return subprocess.check_output(["git", "--git-dir", str(origin), *args], text=True)


def _code(out: Path, meta: dict[str, Any]) -> list[str]:
    origin, base = out / "origin.git", meta.get("deepdiff_rev")
    if not origin.exists() or not base:
        return []
    parts = ["## Code on origin", "", f"Every branch pushed to the run's only remote, "
             f"diffed against the pinned base `{base}`.", ""]  # fmt: skip
    refs = _git(origin, "for-each-ref", "--sort=committerdate",
                "--format=%(refname:short)", "refs/heads").split()  # fmt: skip
    for branch in refs:
        if branch == "main":
            continue
        log = _git(
            origin, "log", "--format=%H %an <%ae> %cI%n    %s", f"{base}..{branch}"
        )
        parts += [f"### `{branch}`", "", _block(log.rstrip()), "",
                  _block(_git(origin, "diff", base, branch).rstrip(), "diff"), ""]  # fmt: skip
    return parts


def _pull_request(out: Path) -> list[str]:
    pr = out / "workspaces" / "t7-spike-cold" / "PR.md"
    if not pr.exists():
        return []
    return ["## Pull request text written by the cold session", "",
            "`PR.md` from the T7 workspace, exactly as written.", "",
            _block(pr.read_text(), "markdown"), ""]  # fmt: skip


def _entry(number: int, entry: logs.ToolCall | logs.Note) -> list[str]:
    if isinstance(entry, logs.Note):
        return [f"#### {number}. {entry.kind}", "", _block(entry.text), ""]
    flag = " (error)" if entry.is_error else ""
    return [f"#### {number}. `{entry.name}`{flag}", "", "Arguments:", "", _block(entry.arguments), "",
            "Result:", "", _block(entry.result), ""]  # fmt: skip


def _turn_logs(out: Path, meta: dict[str, Any]) -> list[str]:
    parts = ["## Turn logs", ""]
    prompts = meta.get("prompts") or {}
    for turn in meta["turns"]:
        prefix = turn["prefix"]
        if turn.get("skipped"):
            parts += [f"### {prefix} ({turn['actor']}, skipped)", "",
                      "Not run: T4 had already promoted the fix.", ""]  # fmt: skip
            continue
        parts += [f"### {prefix} ({turn['actor']}, {turn['result']['wall_s']} s)", ""]
        if prefix in prompts:
            parts += ["#### Prompt", "", _block(prompts[prefix]), ""]
        for number, entry in enumerate(
            logs.timeline(out / f"{prefix}.stdout.jsonl"), 1
        ):
            parts += _entry(number, entry)
        final = out / f"{prefix}-final.txt"
        if final.exists():
            parts += ["#### Final reply", "", final.read_text().strip(), ""]
    return parts


def render(out: Path) -> str:
    meta = json.loads((out / "run-metadata.json").read_text())
    parts = [
        "# Real-work demo transcript", "",
        "Generated from the run directory by `scripts/real_work_demo/transcript.py`. "
        "Nothing is summarised or truncated. At publication, local paths and the "
        "directory names derived from them become `<runtime>`, `<home>`, "
        "`<runtime-slug>` or `<home-slug>`; the local account name becomes `<user>`; "
        "and the disposable instance's bearer tokens become `<token:name>`.", "",
    ]  # fmt: skip
    for section in (_run(meta), _verdicts(out), _turns(meta), _memory(out, meta),
                    _code(out, meta), _pull_request(out), _turn_logs(out, meta)):  # fmt: skip
        parts += section
    return "\n".join(parts)


def write(out: Path) -> Path:
    path = out / "transcript.md"
    path.write_text(render(out))
    return path
