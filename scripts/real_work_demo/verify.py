"""Deterministic post-run checks. Agent reports count for nothing."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.real_work_demo import workspace
from scripts.real_work_demo.logs import ToolCall
from scripts.real_work_demo.run import GIT_IDENTITY

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
WRITES = {
    "cairn_low.ingest", "cairn_low.promote", "cairn_low.invalidate",
    "memory.disagree", "memory.resolve", "memory.remember", "memory.correct",
}  # fmt: skip
READS = ("retrieve", "read-evidence")
# A failing run: a pytest summary, a traceback, or a recorded non-zero exit code.
FAILING = re.compile(
    r"\b\d+ failed\b|Traceback \(most recent call last\)|exit code:? *[1-9]", re.I
)
# A passing run: a summary line with passes and no failures on that same line, so
# `30 failed, 1 passed` is not a pass but a before-and-after pair can still show one.
PASSING = re.compile(r"^(?!.*\b[1-9]\d* (?:failed|errors?)\b).*\b\d+ passed\b", re.M)
DIFF = re.compile(r"^(diff --git |--- |\+\+\+ )", re.M)
# Check 7, when no fix was promoted: a sentence claiming validation with no negation.
CLAIMS_VALID = re.compile(r"\b(validated|verified|promoted)\b", re.I)
NEGATED = re.compile(
    r"\b(not|never|no|unvalidated|declined|refused|without)\b|n't", re.I
)


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Session:
    """One verifier turn that ran: the evidence it ingested and what it resolved."""

    prefix: str
    evidence: frozenset[str]
    resolved: frozenset[str]


class Catalogue:
    """Read-only view of a catalogue snapshot, using the c4ca86ef schema."""

    def __init__(self, path: Path) -> None:
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row

    def __enter__(self) -> Catalogue:
        return self

    def __exit__(self, *_: object) -> None:
        self.con.close()

    def facts(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT f.fact_id, f.trust, f.derived_from, f.promoted_by, f.evidence_id, "
            "f.body, a.principal_id "
            "FROM facts f LEFT JOIN assertions a ON a.assertion_id = f.assertion_id"
        ).fetchall()

    def evidence_by_fact(self) -> dict[str, str]:
        """Ingested facts link evidence through their assertion; promoted ones directly."""
        rows = self.con.execute(
            "SELECT f.fact_id, COALESCE(f.evidence_id, e.evidence_id) AS evidence_id FROM facts f "
            "LEFT JOIN evidence_records e ON e.assertion_id = f.assertion_id"
        ).fetchall()
        return {r["fact_id"]: r["evidence_id"] for r in rows if r["evidence_id"]}

    def citable_ids(self) -> set[str]:
        """Every ID a PR text may legitimately cite: facts, evidence, relationships."""
        queries = (
            "SELECT fact_id FROM facts",
            "SELECT evidence_id FROM evidence_records",
            "SELECT relationship_id FROM memory_disagreements",
            "SELECT relationship_id FROM memory_resolutions",
        )
        return {row[0] for q in queries for row in self.con.execute(q)}

    def disagreements(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM memory_disagreements").fetchall()

    def resolutions(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM memory_resolutions").fetchall()

    def mutating_audit(self) -> list[tuple[str, str, str | None]]:
        rows = self.con.execute(
            "SELECT action_code, outcome, canonical_event FROM audit_events WHERE action_kind = 'data'"
        ).fetchall()
        out = []
        for row in rows:
            if row["action_code"] in READS:
                continue
            event = json.loads(bytes(row["canonical_event"]))
            out.append((row["action_code"], row["outcome"], event.get("principal_id")))
        return out


def check_custody(cat: Catalogue, principals: dict[str, str]) -> Check:
    agents = set(principals.values())
    bad = [
        (code, who)
        for code, outcome, who in cat.mutating_audit()
        if outcome == "allow" and who not in agents
    ]
    detail = (
        f"non-agent mutations: {bad}"
        if bad
        else "every allowed mutation by an agent principal"
    )
    return Check(1, "actor custody", not bad, detail)


def check_memory(
    cat: Catalogue,
    principals: dict[str, str],
    sessions: list[Session],
    payloads: dict[str, str] | None = None,
    origin: Path | None = None,
) -> list[Check]:
    facts = cat.facts()
    by_id = {f["fact_id"]: f for f in facts}
    evidence = cat.evidence_by_fact()

    def shows(fact_id: str, pattern: re.Pattern[str]) -> bool:
        if fact_id not in evidence:
            return False
        if payloads is None:
            return True
        text = payloads.get(evidence[fact_id], "")
        if not pattern.search(text):
            return False
        if DIFF.search(text):
            return True
        # A named commit in origin stands in for a pasted diff: git holds it exactly
        # (Operator, 24 September 2026).
        body = by_id[fact_id]["body"] or ""
        return origin is not None and bool(failed_commits(origin, [body, text]))

    agents = {principals[n] for n in ("val", "spike", "verifier")}
    # Any agent may record the dead end (Operator, 24 September 2026, after rehearsal 1).
    failed = [
        f
        for f in facts
        if f["trust"] == "failed-approach" and f["principal_id"] in agents
    ]
    fixes = [
        f
        for f in facts
        if f["trust"] == "candidate" and f["principal_id"] == principals["spike"]
    ]
    dis = cat.disagreements()
    # Settled by the verifier session that resolved it, on evidence that session
    # ingested itself (spec Amendment A).
    resolved = {
        r["disagreement_id"] for r in cat.resolutions()
        if r["principal_id"] == principals["verifier"]
        and any(r["disagreement_id"] in s.resolved and r["evidence_id"] in s.evidence
                for s in sessions)
    }  # fmt: skip
    # Check 5 judges the last verifier session that ran, on its own evidence.
    last = sessions[-1] if sessions else Session("", frozenset(), frozenset())
    unresolved = [
        d["relationship_id"] for d in dis if d["relationship_id"] not in resolved
    ]
    fix_ids = {f["fact_id"] for f in fixes}
    validated = [
        f for f in facts
        if f["trust"] == "validated" and f["promoted_by"] == principals["verifier"]
        and f["derived_from"] in fix_ids and f["evidence_id"] in last.evidence
    ]  # fmt: skip
    # Or a refusal on evidence (Operator, 24 September 2026): the verifier settled a
    # dispute over one of Spike's candidates against it, and it stays unpromoted.
    promoted_from = {f["derived_from"] for f in validated}
    by_rel = {d["relationship_id"]: d for d in dis}
    refused = [
        r for r in cat.resolutions()
        if r["principal_id"] == principals["verifier"]
        and r["evidence_id"] in last.evidence and r["disagreement_id"] in last.resolved
        and r["disagreement_id"] in by_rel
        and (sides := {by_rel[r["disagreement_id"]]["left_fact_id"],
                       by_rel[r["disagreement_id"]]["right_fact_id"]}) & fix_ids
        and r["selected_fact_id"] not in fix_ids
        and not (sides & fix_ids & promoted_from)
    ]  # fmt: skip
    retained = all(
        d["left_fact_id"] in by_id and d["right_fact_id"] in by_id for d in dis
    )
    good_failed = [f for f in failed if shows(f["fact_id"], FAILING)]
    good_fixes = [f for f in fixes if shows(f["fact_id"], PASSING)]
    return [
        Check(2, "dead end preserved", bool(good_failed),
              f"{len(failed)} failed-approach by an agent, {len(good_failed)} with diff and failing run"),
        Check(3, "fix claimed", bool(good_fixes),
              f"{len(fixes)} candidate by spike, {len(good_fixes)} with diff and passing run"),
        # A dispute is a demo beat, not an evidence requirement; but any that exists
        # must be settled by a verifier session on its own evidence.
        Check(4, "disagreements resolved independently", not unresolved and retained,
              "no disagreement" if not dis else
              f"{len(dis)} disagreements, unresolved by a verifier session on its own evidence: "
              f"{unresolved}, "
              f"retained={retained}"),
        Check(5, "promotion decision by the last verifier session", bool(validated or refused),
              f"{last.prefix or 'no verifier session'}: "
              f"{len(validated)} validated derived from spike's candidate; "
              f"{len(refused)} promotion(s) refused on evidence"),
    ]  # fmt: skip


def spike_fix_promoted(cat: Catalogue, principals: dict[str, str]) -> bool:
    """A validated fact the verifier derived from one of Spike's candidates."""
    facts = cat.facts()
    fixes = {
        f["fact_id"] for f in facts
        if f["trust"] == "candidate" and f["principal_id"] == principals["spike"]
    }  # fmt: skip
    return any(
        f["trust"] == "validated" and f["promoted_by"] == principals["verifier"]
        and f["derived_from"] in fixes
        for f in facts
    )  # fmt: skip


def _pytest(venv: Path, cwd: Path, *args: str) -> int:
    return subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd, capture_output=True,
    ).returncode  # fmt: skip


def check_code(
    origin: Path,
    venv: Path,
    fix_branch: str,
    failed_rev: str,
    regression_tests: list[str],
    base: str,
) -> Check:
    """failed_rev is a commit a failed approach names, or origin/<branch>."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        fix = workspace.clone_for(origin, tmp / "fix", f"origin/{fix_branch}")
        suite_ok = _pytest(venv, fix) == 0
        test_files = sorted({t.split("::")[0] for t in regression_tests})

        def regression_fails_at(rev: str, name: str) -> bool:
            """Apply the fix branch's regression tests to `rev` and run only them."""
            clone = workspace.clone_for(origin, tmp / name, rev)
            subprocess.run(
                ["git", "-C", str(clone), "checkout", f"origin/{fix_branch}", "--", *test_files],
                check=True,
            )  # fmt: skip
            # By name within the touched files: a method's node id would need its
            # class, which the diff does not reliably show.
            names = " or ".join(sorted({t.split("::")[1] for t in regression_tests}))
            return _pytest(venv, clone, *test_files, "-k", names) != 0

        # A failed approach can pass the existing suite (rehearsal 1's did); what
        # proves it failed is the fix's own regression test (Operator, 24 September 2026).
        regression_fails_on_failed = regression_fails_at(failed_rev, "failed")
        regression_fails_on_base = regression_fails_at(base, "base")
    ok = suite_ok and regression_fails_on_failed and regression_fails_on_base
    return Check(6, "code truth re-observed", ok,
                 f"suite_on_fix={suite_ok} failed_rev={failed_rev} "
                 f"regression_fails_on_failed={regression_fails_on_failed} "
                 f"regression_fails_on_base={regression_fails_on_base}")  # fmt: skip


def overclaims(text: str) -> list[str]:
    """Sentences that present something as validated without negating it."""
    return [
        s.strip() for s in re.split(r"[.!?\n]+", text)
        if CLAIMS_VALID.search(s) and not NEGATED.search(s)
    ]  # fmt: skip


def check_cold(
    calls: list[ToolCall],
    pr_text: str,
    fact_ids: set[str],
    failed_ids: set[str],
    fix_validated: bool = True,
) -> Check:
    writes = [c.name for c in calls if c.name in WRITES]
    garden = [c.name for c in calls if c.name.startswith("garden.")]
    cited = set(UUID_RE.findall(pr_text))
    unknown = cited - fact_ids
    cites_failure = bool(cited & failed_ids)
    # The PR text must match the final verifier decision (spec Amendment A).
    overclaimed = [] if fix_validated else overclaims(pr_text)
    ok = (
        not writes
        and not garden
        and bool(cited)
        and not unknown
        and cites_failure
        and not overclaimed
    )
    return Check(7, "cold start from memory only", ok,
                 f"writes={writes} garden={garden} cited={len(cited)} unknown={sorted(unknown)} "
                 f"cites_failed_approach={cites_failure} fix_validated={fix_validated} "
                 f"overclaims={overclaimed}")  # fmt: skip


# git's global options, as git.c's handle_options parses them. `--opt=value`
# spellings carry their own value; these take the next word as theirs.
_GIT_VALUED = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env", "--attr-source", "--shallow-file",
}  # fmt: skip
_GIT_FLAGS = {
    "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
    "--no-lazy-fetch", "--no-optional-locks", "--no-advice", "--literal-pathspecs",
    "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
}  # fmt: skip
# These print something and exit, so no subcommand runs.
_GIT_EXITS = {
    "-h", "--help", "-v", "--version",
    "--exec-path", "--html-path", "--man-path", "--info-path",
}  # fmt: skip
# git push options that take the next word as their value.
_PUSH_VALUED = {"-o", "--push-option", "--receive-pack", "--exec"}
# Reported for a push behind a global option the table above does not know: its
# arity is unknown, so the target is too, and check 8 must fail rather than guess.
UNPARSED_PUSH = "<push behind an unrecognised git option>"
# Shell control operators end a simple command; redirection operators take the next
# token as their target. shlex's punctuation mode splits both from adjacent words
# (`status;git`, `(git`, `2>&1`) while a quoted `'>evil'` stays one ordinary word.
_CONTROL = set("();|&")
_REDIRECT = set("<>&|")


def _simple_commands(command: str) -> list[list[str]]:
    """The words of each simple command in a shell line, redirections removed."""
    lexer = shlex.shlex(command.replace("`", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    commands: list[list[str]] = [[]]
    rest = iter(tokens)
    for token in rest:
        if token and set(token) <= _REDIRECT and ("<" in token or ">" in token):
            if commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()  # the descriptor in `2>&1`
            next(rest, None)
        elif token and set(token) <= _CONTROL:
            commands.append([])
        else:
            commands[-1].append(token)
    return commands


def _git_subcommand(args: list[str]) -> int | None:
    """Index of git's subcommand in `args` (len(args) if none runs), or None when
    an unrecognised global option makes the position unknowable."""
    j = 0
    while j < len(args) and args[j].startswith("-"):
        name, eq, _ = args[j].partition("=")
        if args[j] in _GIT_EXITS or name == "--list-cmds":
            return len(args)
        if eq and (name in _GIT_VALUED or name == "--exec-path"):
            j += 1
        elif args[j] in _GIT_VALUED:
            j += 2
        elif args[j] in _GIT_FLAGS:
            j += 1
        else:
            return None
    return j


def _push_remote(args: list[str]) -> str:
    """The repository a `git push` with these arguments pushes to."""
    repo = None
    rest = iter(args)
    for arg in rest:
        if arg == "--repo":
            repo = next(rest, None)
        elif arg.startswith("--repo="):
            repo = arg.partition("=")[2]
        elif arg in _PUSH_VALUED:
            next(rest, None)
        elif not arg.startswith("-"):
            return arg  # the first positional wins over --repo, as in git
    return repo or "origin"  # bare `git push` pushes to the configured upstream


def push_targets(command: str) -> list[str]:
    """The repository each `git push` in a shell command line pushes to.

    Exact for direct git invocations, as tested against git's own trace. It cannot
    see a push hidden behind an alias, `sh -c`, `eval` or a script file; the agent
    sandboxes' network denial is the enforcement, and this is its audit line.
    """
    targets: list[str] = []
    for words in _simple_commands(command):
        for i, word in enumerate(words):
            if word != "git" and not word.endswith("/git"):
                continue
            args = words[i + 1 :]
            sub = _git_subcommand(args)
            if sub is None:
                if "push" in args:
                    targets.append(UNPARSED_PUSH)
            elif sub < len(args) and args[sub] == "push":
                targets.append(_push_remote(args[sub + 1 :]))
    return targets


# Claude Code fails open when its sandbox cannot start (capture 2, T5): every later
# command in that session runs without network denial or write confinement.
SANDBOX_OFF = "Sandboxing is disabled for the rest of this session"


def unsandboxed(out: Path, prefixes: list[str]) -> list[str]:
    """Turns whose raw stdout or stderr shows the sandbox failed open."""
    return [
        p for p in prefixes
        if any(f.exists() and SANDBOX_OFF in f.read_text(errors="replace")
               for f in (out / f"{p}.stdout.jsonl", out / f"{p}.stderr.log"))
    ]  # fmt: skip


def check_egress(
    workspaces: list[Path],
    origin: Path,
    all_calls: list[ToolCall],
    unsandboxed_turns: Sequence[str] = (),
) -> Check:
    foreign_remotes = {
        str(w): r for w in workspaces for r in workspace.remotes(w) if r != str(origin)
    }
    pushes = [
        t for c in all_calls
        for t in push_targets(str(c.arguments.get("command", "")))
        if t not in ("origin", str(origin))
    ]  # fmt: skip
    ok = not foreign_remotes and not pushes and not unsandboxed_turns
    return Check(
        8, "no egress", ok,
        f"foreign_remotes={foreign_remotes} foreign_pushes={pushes} "
        f"unsandboxed={list(unsandboxed_turns)}",
    )  # fmt: skip


# --- orchestration -----------------------------------------------------------


def _git(origin: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--git-dir", str(origin), *args], text=True
    ).strip()


def branches(origin: Path, base: str) -> dict[str, list[str]]:
    """Pushed branches by the actor who authored their tip, oldest tip first."""
    by_actor: dict[str, list[str]] = {"spike": [], "val": []}
    for line in _git(
        origin, "for-each-ref", "--sort=committerdate",
        "--format=%(refname:short) %(authoremail)", "refs/heads",
    ).splitlines():  # fmt: skip
        name, email = line.split(" ", 1)
        if name == "main" or _git(origin, "rev-parse", name) == base:
            continue
        for actor in by_actor:
            if email.strip("<>") == GIT_IDENTITY[actor].split("<")[1].rstrip(">"):
                by_actor[actor].append(name)
    return by_actor


_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def failed_commits(origin: Path, texts: list[str]) -> list[str]:
    """Full SHAs of the commits failed approaches name that exist in origin."""
    found: list[str] = []
    for text in texts:
        for candidate in _SHA.findall(text):
            probe = subprocess.run(
                ["git", "--git-dir", str(origin), "rev-parse", "--verify", "-q",
                 f"{candidate}^{{commit}}"],
                capture_output=True, text=True,
            )  # fmt: skip
            sha = probe.stdout.strip()
            if probe.returncode == 0 and sha not in found:
                found.append(sha)
    return found


def regression_tests(origin: Path, base: str, branch: str) -> list[str]:
    """Every test function the branch adds under tests/, as pytest node ids.

    All of them, not the first: rehearsal 1's fix added seven, and the one that
    exposed the incomplete attempt was the sixth.
    """
    current = None
    found: list[str] = []
    for line in _git(origin, "diff", base, branch, "--", "tests").splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        # Module-level functions and methods added to existing test classes alike
        # (rehearsal 2's fix added only methods).
        match = re.match(r"^\+\s*def (test_\w+)\(", line)
        if match and current:
            found.append(f"{current}::{match.group(1)}")
    return found


def read_evidence(
    endpoint: str, token: str, scope: dict[str, Any], ids: set[str], wait_s: float = 60
) -> dict[str, str]:
    """Evidence payloads as the verifier sees them, waiting out Attic delivery."""
    import time

    import httpx

    payloads: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    deadline = time.monotonic() + wait_s
    pending = set(ids)
    with httpx.Client(base_url=endpoint, headers=headers, timeout=10) as http:
        while pending and time.monotonic() < deadline:
            for evidence_id in sorted(pending):
                frame = http.post("/v1/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "read-evidence", "arguments": {"scope": scope, "evidence_id": evidence_id}},
                }).json()  # fmt: skip
                body = json.loads(frame["result"]["content"][0]["text"])
                if "payload" in body:
                    payloads[evidence_id] = body["payload"]
                    pending.discard(evidence_id)
                elif (body.get("failure") or {}).get("code") != "evidence_pending":
                    pending.discard(
                        evidence_id
                    )  # refused: the check will fail on its absence
            if pending:
                time.sleep(0.5)
    return payloads


def save_evidence(out: Path, payloads: dict[str, str]) -> None:
    """Persist what the harness read from Attic, so verification can be re-run offline."""
    folder = out / "snapshot" / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    for evidence_id, payload in payloads.items():
        path = folder / evidence_id
        path.write_text(payload)
        path.chmod(0o600)


def load_evidence(out: Path) -> dict[str, str]:
    folder = out / "snapshot" / "evidence"
    return {p.name: p.read_text() for p in folder.iterdir()} if folder.exists() else {}


def _ingested_evidence(calls: list[ToolCall]) -> set[str]:
    out = set()
    for c in calls:
        if c.name == "cairn_low.ingest" and isinstance(c.result, dict):
            evidence_id = (c.result.get("result") or {}).get("evidence_id")
            if evidence_id:
                out.add(evidence_id)
    return out


def sessions(
    turns: list[dict[str, Any]], calls: dict[str, list[ToolCall]]
) -> list[Session]:
    """The verifier turns that ran, in order, with what their logs show they did."""
    return [
        Session(
            t["prefix"],
            frozenset(_ingested_evidence(calls.get(t["prefix"], []))),
            frozenset(
                str(c.arguments.get("disagreement_id"))
                for c in calls.get(t["prefix"], [])
                if c.name == "memory.resolve" and not c.is_error
            ),
        )
        for t in turns
        if t["actor"] == "verifier" and not t.get("skipped")
    ]


def worker_disputed(
    turns: list[dict[str, Any]], calls: dict[str, list[ToolCall]]
) -> bool:
    """A worker's disagreement before the first verifier turn (Operator, ruling A1)."""
    for t in turns:
        if t["actor"] == "verifier":
            return False
        if t["actor"] in ("val", "spike") and any(
            c.name == "memory.disagree" and not c.is_error
            for c in calls.get(t["prefix"], [])
        ):
            return True
    return False


def verify(out: Path, build_venv: bool = True) -> dict[str, Any]:
    """Checks 1-8 from the run directory alone; re-runnable after teardown."""
    from scripts.real_work_demo import logs

    meta = json.loads((out / "run-metadata.json").read_text())
    principals: dict[str, str] = meta["principals"]
    origin = out / "origin.git"
    runtime_origin = Path(meta["runtime"]) / "origin.git"
    calls = {
        t["prefix"]: logs.calls(out / f"{t['prefix']}.stdout.jsonl")
        for t in meta["turns"]
        if not t.get("skipped")
    }
    verifier = sessions(meta["turns"], calls)
    with Catalogue(out / "snapshot" / "cairn" / "catalogue.sqlite3") as cat:
        payloads = load_evidence(out)
        custody = check_custody(cat, principals)
        memory = check_memory(cat, principals, verifier, payloads, origin)
        fix_validated = spike_fix_promoted(cat, principals)
        facts = cat.facts()
        citable = cat.citable_ids()
        evidence = cat.evidence_by_fact()
    failed_texts = [
        text
        for f in facts if f["trust"] == "failed-approach"
        for text in (f["body"], payloads.get(evidence.get(f["fact_id"], ""), ""))
    ]  # fmt: skip
    # Garden: the harness writes the agents' Garden profiles and uses one only for
    # `a2a doctor`, which never sends (a2a/cmd/attention.go). Every message is an
    # agent's own send_message; the verifier (T4, T6) and the cold session (T7) send none.
    stray = [t["prefix"] for t in meta["turns"] if t["actor"] in ("verifier", "spike-cold")
             for c in calls.get(t["prefix"], []) if c.name == "garden.send_message"]  # fmt: skip
    if stray:
        custody = Check(
            1, custody.name, False, f"{custody.detail}; garden sends by {stray}"
        )

    found = branches(origin, meta["deepdiff_rev"])
    fix = found["spike"][-1] if found["spike"] else None
    test_ids = regression_tests(origin, meta["deepdiff_rev"], fix) if fix else []
    code = Check(
        6,
        "code truth re-observed",
        False,
        f"branches={found} regression_tests={test_ids} venv_built={build_venv}",
    )
    # Commits the failed approaches name (Operator, 24 September 2026: they may sit on the
    # shared branch), then any separate Val branches.
    failed_revs = failed_commits(origin, failed_texts) + [
        f"origin/{b}" for b in found["val"]
    ]
    if fix and failed_revs and test_ids and build_venv:
        # The harness's own venv, rebuilt from the pinned lock (spec check 6).
        with tempfile.TemporaryDirectory(prefix="rwd-verify-") as raw:
            rev = meta["deepdiff_rev"]
            base = workspace.clone_for(origin, Path(raw) / "base", rev)
            venv = Path(raw) / "venv"
            lock = workspace.build_venv(base, venv)
            results = [
                check_code(origin, venv, fix, f, test_ids, rev) for f in failed_revs
            ]
        passing = [r for r in results if r.passed]
        chosen = passing[0] if passing else results[0]
        same_lock = lock == meta.get("venv_lock_sha256")
        code = Check(6, chosen.name, chosen.passed and same_lock,
                     f"{chosen.detail} lock_matches_run={same_lock}")  # fmt: skip

    pr = out / "workspaces" / "t7-spike-cold" / "PR.md"
    cold = check_cold(
        calls.get("t7-spike-cold", []),
        pr.read_text() if pr.exists() else "",
        citable,
        {f["fact_id"] for f in facts if f["trust"] == "failed-approach"},
        fix_validated,
    )
    workspaces = (
        sorted(p for p in (out / "workspaces").iterdir())
        if (out / "workspaces").exists()
        else []
    )
    egress = check_egress(
        workspaces, runtime_origin, [c for cs in calls.values() for c in cs],
        unsandboxed(out, list(calls)),
    )  # fmt: skip

    checks = [custody, *memory, code, cold, egress]
    timed_out = [
        t["prefix"]
        for t in meta["turns"]
        if not t.get("skipped") and t["result"]["timed_out"]
    ]
    evidence_valid = all(c.passed for c in checks) and not timed_out
    t1_failed_run = any(
        c.name in ("command_execution", "Bash")
        and isinstance(c.result, str)
        and FAILING.search(c.result)
        for c in calls.get("t1-val", [])
    )
    # Publishable only once the loop closed: a Spike fix validated in T4 or T6.
    beats = {"t1_failing_run": t1_failed_run,
             "worker_disagreement": worker_disputed(meta["turns"], calls),
             "loop_closed": fix_validated}  # fmt: skip
    return {
        "checks": [c.__dict__ for c in checks],
        "timed_out": timed_out,
        "evidence_valid": evidence_valid,
        "demo_usable": evidence_valid and all(beats.values()),
        "beats": beats,
    }
