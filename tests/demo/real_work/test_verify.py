import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "transports" / "memory")]

from memory_support import Api, Instance, serve  # noqa: E402

from scripts.real_work_demo import actors, verify  # noqa: E402
from scripts.real_work_demo.logs import ToolCall  # noqa: E402

SCOPE = actors.DEMO_SCOPE


async def v1(api: Api, name: str, args: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api.token}", "Accept": "application/json"}
    frame = (
        await api.http.post(
            "/v1/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },  # fmt: skip
        )
    ).json()
    result: dict[str, Any] = json.loads(frame["result"]["content"][0]["text"])
    return result


def good_run(tmp_path: Path) -> tuple[Instance, dict[str, Any], dict[str, Any]]:
    instance = Instance(tmp_path / "cairn", attic=True)
    made = actors.create_actors(instance, tmp_path)
    ids: dict[str, str] = {}

    async def go() -> None:
        async with serve(instance) as http:
            val, spike, ver = (
                Api(http, made[n].token, "mcp") for n in ("val", "spike", "verifier")
            )

            async def ingest(
                api: Api, body: str, trust: str, payload: str
            ) -> tuple[str, str]:
                r = await v1(api, "ingest", {
                    "scope": SCOPE, "classification": "internal", "source_type": "agent-claim",
                    "facts": [{"body": body}], "requested_trust": trust,
                    "evidence_payload": payload, "idempotency_key": str(uuid.uuid4()),
                })  # fmt: skip
                return r["result"]["fact_ids"][0], r["result"]["evidence_id"]

            ids["failed"], _ = await ingest(
                val, "Excluding datetimes breaks X", "failed-approach", "diff\n2 failed"
            )
            ids["fix"], _ = await ingest(
                spike, "Fix at call site", "candidate", "diff\n1270 passed"
            )
            ids["counter"], _ = await ingest(
                val, "String flags still odd", "candidate", "test\n1 failed"
            )
            d = await val.call("disagree", {"scope": SCOPE, "left_fact_id": ids["fix"],
                               "right_fact_id": ids["counter"], "classification": "internal",
                               "reason": "string flags"}, key=str(uuid.uuid4()))  # fmt: skip
            ids["disagreement"] = d["result"]["relationship_id"]
            _, ids["t4_evidence"] = await ingest(
                ver, "Verifier re-ran suite", "candidate", "1271 passed"
            )
            await ver.call("resolve", {"scope": SCOPE, "disagreement_id": ids["disagreement"],
                           "evidence_id": ids["t4_evidence"], "selected_fact_id": ids["counter"],
                           "reason": "own run"}, key=str(uuid.uuid4()))  # fmt: skip
            p = await v1(ver, "promote", {"fact_ids": [ids["fix"]],
                         "evidence": {"evidence_id": ids["t4_evidence"]},
                         "reason": "verified", "idempotency_key": str(uuid.uuid4())})  # fmt: skip
            ids["validated"] = p["result"]["promotions"][0]["derived_fact_id"]

    asyncio.run(go())
    return instance, made, ids


def principals(made: dict[str, Any]) -> dict[str, str]:
    return {n: str(a.principal) for n, a in made.items()}


def t4(ids: dict[str, str]) -> verify.Session:
    """The T4 verifier session: its own evidence and whatever it resolved."""
    resolved = {ids["disagreement"]} if "disagreement" in ids else set()
    return verify.Session(
        "t4-verifier", frozenset({ids["t4_evidence"]}), frozenset(resolved)
    )


def test_good_run_passes_memory_checks(tmp_path: Path) -> None:
    instance, made, ids = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        assert verify.check_custody(cat, principals(made)).passed
        checks = verify.check_memory(cat, principals(made), [t4(ids)])
    assert [c.number for c in checks] == [2, 3, 4, 5]
    assert all(c.passed for c in checks), checks


def test_resolution_citing_non_verifier_evidence_fails(tmp_path: Path) -> None:
    instance, made, _ = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        checks = verify.check_memory(cat, principals(made), sessions=[])
    assert not next(c for c in checks if c.number == 4).passed
    assert not next(c for c in checks if c.number == 5).passed


def test_custody_fails_for_a_non_agent_mutation(tmp_path: Path) -> None:
    instance, made, _ = good_run(tmp_path)
    agents_only = {n: p for n, p in principals(made).items() if n != "verifier"}
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        assert not verify.check_custody(cat, agents_only).passed


def test_cold_turn_that_wrote_or_messaged_fails() -> None:
    wrote = [ToolCall("memory.recall", {}), ToolCall("cairn_low.ingest", {})]
    assert not verify.check_cold(wrote, "PR", set(), set()).passed
    messaged = [ToolCall("garden.send_message", {})]
    assert not verify.check_cold(messaged, "PR", set(), set()).passed


def test_cold_pr_citing_unknown_fact_fails() -> None:
    text = "Fixed (fact 11111111-1111-4111-8111-111111111111). Rejected: X."
    known = {"22222222-2222-4222-8222-222222222222"}
    assert not verify.check_cold(
        [ToolCall("memory.recall", {})], text, known, set()
    ).passed


def test_cold_pr_must_cite_a_failed_approach() -> None:
    fid = "22222222-2222-4222-8222-222222222222"
    failed = {"33333333-3333-4333-8333-333333333333"}
    assert not verify.check_cold(
        [ToolCall("memory.recall", {})], f"Fix ({fid}).", {fid}, failed
    ).passed
    both = f"Fix ({fid}); rejected 33333333-3333-4333-8333-333333333333."
    assert verify.check_cold(
        [ToolCall("memory.recall", {})], both, {fid} | failed, failed
    ).passed


def test_egress_flags_foreign_remote_and_foreign_push(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "remote", "add", "origin", str(origin)], check=True
    )
    subprocess.run(
        ["git", "-C", str(ws), "remote", "add", "gh", "https://github.com/x/y"],
        check=True,
    )
    assert not verify.check_egress([ws], origin, []).passed
    subprocess.run(["git", "-C", str(ws), "remote", "remove", "gh"], check=True)
    push = [ToolCall("Bash", {"command": "git push https://github.com/x/y HEAD"})]
    assert not verify.check_egress([ws], origin, push).passed
    ok = [ToolCall("Bash", {"command": "git push -u origin fix"})]
    assert verify.check_egress([ws], origin, ok).passed


def test_push_with_options_to_a_foreign_url_is_caught(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "remote", "add", "origin", str(origin)], check=True
    )
    sneaky = [
        ToolCall(
            "command_execution",
            {"command": "cd x && git push -u --force https://github.com/x/y fix"},
        )
    ]
    assert not verify.check_egress([ws], origin, sneaky).passed


def test_evidence_payloads_must_show_the_failure_and_the_pass(tmp_path: Path) -> None:
    instance, made, ids = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        evidence = cat.evidence_by_fact()
        good = {
            evidence[ids["failed"]]: "diff --git a/x b/x\n2 failed, 10 passed",
            evidence[ids["fix"]]: "diff --git a/x b/x\n1270 passed",
        }
        assert all(
            c.passed for c in verify.check_memory(cat, principals(made), [], good)[:2]
        )
        empty = dict.fromkeys(good, "no output")
        checks = verify.check_memory(cat, principals(made), [], empty)
    assert not checks[0].passed and not checks[1].passed


def _commit(repo: Path, who: str, files: dict[str, str], msg: str) -> None:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    name, email = who.split(":")
    subprocess.run(["git", "-C", str(repo), "-c", f"user.name={name}", "-c", f"user.email={email}",
                    "commit", "-qm", msg], check=True)  # fmt: skip


def test_branches_and_regression_test_come_from_git(tmp_path: Path) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(src, "t:t@t", {"tests/test_a.py": "def test_old():\n    pass\n"}, "base")
    base = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    subprocess.run(
        ["git", "-C", str(src), "checkout", "-qb", "val-attempt"], check=True
    )
    _commit(src, "Val:val@jclk.ch", {"x.py": "x = 1\n"}, "attempt")
    subprocess.run(
        ["git", "-C", str(src), "push", "-q", str(origin), "val-attempt"], check=True
    )
    subprocess.run(["git", "-C", str(src), "checkout", "-q", base], check=True)
    subprocess.run(["git", "-C", str(src), "checkout", "-qb", "fix-550"], check=True)
    _commit(src, "Spike:spike@jclk.ch",
            {"tests/test_a.py": "def test_old():\n    pass\n\n\ndef test_issue_550():\n    pass\n"}, "fix")  # fmt: skip
    subprocess.run(
        ["git", "-C", str(src), "push", "-q", str(origin), "fix-550"], check=True
    )
    found = verify.branches(origin, base)
    assert found == {"spike": ["fix-550"], "val": ["val-attempt"]}
    assert verify.regression_tests(origin, base, "fix-550") == [
        "tests/test_a.py::test_issue_550"
    ]


def test_citable_ids_cover_evidence_and_relationships_not_only_facts(
    tmp_path: Path,
) -> None:
    instance, _, ids = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        known = cat.citable_ids()
    assert {
        ids["fix"],
        ids["failed"],
        ids["validated"],
        ids["t4_evidence"],
        ids["disagreement"],
    } <= known
    text = f"Fix {ids['fix']} (evidence {ids['t4_evidence']}); rejected {ids['failed']}; see {ids['disagreement']}."
    assert verify.check_cold(
        [ToolCall("memory.recall", {})], text, known, {ids["failed"]}
    ).passed


def _only_verifier_failed_approach(
    tmp_path: Path,
) -> tuple[Instance, dict[str, Any], dict[str, str]]:
    """Rehearsal 1's shape: the verifier, not Val, records the failed approach; no dispute."""
    instance = Instance(tmp_path / "cairn", attic=True)
    made = actors.create_actors(instance, tmp_path)
    ids: dict[str, str] = {}

    async def go() -> None:
        async with serve(instance) as http:
            spike, ver = (
                Api(http, made[n].token, "mcp") for n in ("spike", "verifier")
            )

            async def ingest(api: Api, body: str, trust: str) -> tuple[str, str]:
                r = await v1(api, "ingest", {
                    "scope": SCOPE, "classification": "internal", "source_type": "agent-claim",
                    "facts": [{"body": body}], "requested_trust": trust,
                    "evidence_payload": "x", "idempotency_key": str(uuid.uuid4()),
                })  # fmt: skip
                return r["result"]["fact_ids"][0], r["result"]["evidence_id"]

            ids["fix"], _ = await ingest(spike, "Fix at call site", "candidate")
            ids["failed"], ids["failed_evidence"] = await ingest(
                ver, "Attempt f854b8d fails", "failed-approach"
            )

    asyncio.run(go())
    return instance, made, ids


def test_a_failed_approach_by_any_agent_counts_and_no_dispute_passes_check_4(
    tmp_path: Path,
) -> None:
    instance, made, ids = _only_verifier_failed_approach(tmp_path)
    payloads = {ids["failed_evidence"]: "diff --git a/x b/x\n3 failed, 1261 passed"}
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        checks = {
            c.number: c
            for c in verify.check_memory(cat, principals(made), [], payloads)
        }
    assert checks[2].passed, checks[2].detail
    assert checks[4].passed and "no disagreement" in checks[4].detail


def test_an_unresolved_disagreement_still_fails_check_4(tmp_path: Path) -> None:
    instance, made, _ = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        checks = {c.number: c for c in verify.check_memory(cat, principals(made), [])}
    assert not checks[4].passed


def test_failed_commits_are_the_named_shas_that_exist_in_origin(tmp_path: Path) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(src, "t:t@t", {"a.py": "a = 1\n"}, "base")
    sha = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    texts = [
        f"Failed approach at commit {sha[:7]}: breaks X",
        "unrelated deadbeef0 text",
    ]
    assert verify.failed_commits(origin, texts) == [sha]


def test_a_named_existing_commit_stands_in_for_the_diff(tmp_path: Path) -> None:
    instance, made, ids = _only_verifier_failed_approach(tmp_path)
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(src, "t:t@t", {"a.py": "a = 1\n"}, "attempt")
    sha = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    named = {ids["failed_evidence"]: f"repro at {sha[:7]}: 1 failed"}
    unnamed = {ids["failed_evidence"]: "repro: 1 failed"}
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        assert verify.check_memory(cat, principals(made), [], named, origin)[0].passed
        assert not verify.check_memory(cat, principals(made), [], unnamed, origin)[
            0
        ].passed


def test_check_code_applies_the_regression_test_to_the_failed_commit(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(
        src,
        "t:t@t",
        {"lib.py": "def f():\n    return 0\n", "tests/__init__.py": ""},
        "base",
    )
    base = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    # A partial fix that the existing (empty) suite cannot catch.
    _commit(src, "Val:val@jclk.ch", {"lib.py": "def f():\n    return 1\n"}, "partial")
    partial = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    _commit(src, "Spike:spike@jclk.ch", {"lib.py": "def f():\n    return 2\n",
            "tests/test_f.py": "from lib import f\n\n\ndef test_f():\n    assert f() == 2\n"}, "fix")  # fmt: skip
    subprocess.run(["git", "-C", str(src), "branch", "fix"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    venv = Path(sys.executable).parent.parent
    result = verify.check_code(
        origin, venv, "fix", partial, ["tests/test_f.py::test_f"], base
    )
    assert result.passed, result.detail
    assert "regression_fails_on_failed=True" in result.detail


def test_a_traceback_or_nonzero_exit_counts_as_a_failing_run() -> None:
    assert verify.FAILING.search("Exit code: 1\nTraceback (most recent call last):")
    assert verify.FAILING.search("===== 2 failed, 10 passed =====")
    assert not verify.FAILING.search("Exit code: 0\n1264 passed")


def test_every_added_test_is_a_regression_test(tmp_path: Path) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(src, "t:t@t", {"tests/test_a.py": "def test_old():\n    pass\n"}, "base")
    base = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    _commit(src, "Spike:spike@jclk.ch", {
        "tests/test_a.py": "def test_old():\n    pass\n\n\ndef test_one(x):\n    pass\n",
        "tests/test_b.py": "def test_two():\n    pass\n"}, "fix")  # fmt: skip
    subprocess.run(["git", "-C", str(src), "branch", "fix"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    assert verify.regression_tests(origin, base, "fix") == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]


def test_tests_added_as_methods_in_existing_classes_are_found_and_run(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    old = "class TestF:\n    def test_old(self):\n        pass\n"
    _commit(src, "t:t@t", {"lib.py": "def f():\n    return 0\n", "tests/__init__.py": "",
                           "tests/test_f.py": "from lib import f\n\n\n" + old}, "base")  # fmt: skip
    base = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    _commit(src, "Val:val@jclk.ch", {"lib.py": "def f():\n    return 1\n"}, "partial")
    partial = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    new = old + "\n    def test_new(self):\n        assert f() == 2\n"
    _commit(src, "Spike:spike@jclk.ch", {"lib.py": "def f():\n    return 2\n",
            "tests/test_f.py": "from lib import f\n\n\n" + new}, "fix")  # fmt: skip
    subprocess.run(["git", "-C", str(src), "branch", "fix"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    found = verify.regression_tests(origin, base, "fix")
    assert found == ["tests/test_f.py::test_new"]
    venv = Path(sys.executable).parent.parent
    result = verify.check_code(origin, venv, "fix", partial, found, base)
    assert result.passed, result.detail


def _refusal_run(
    tmp_path: Path, resolve: bool
) -> tuple[Instance, dict[str, Any], dict[str, str]]:
    """The capture's shape: Val disputes Spike's fix; the verifier sides with Val."""
    instance = Instance(tmp_path / "cairn", attic=True)
    made = actors.create_actors(instance, tmp_path)
    ids: dict[str, str] = {}

    async def go() -> None:
        async with serve(instance) as http:
            val, spike, ver = (
                Api(http, made[n].token, "mcp") for n in ("val", "spike", "verifier")
            )

            async def ingest(api: Api, body: str) -> tuple[str, str]:
                r = await v1(api, "ingest", {
                    "scope": SCOPE, "classification": "internal", "source_type": "agent-claim",
                    "facts": [{"body": body}], "evidence_payload": "x",
                    "idempotency_key": str(uuid.uuid4()),
                })  # fmt: skip
                return r["result"]["fact_ids"][0], r["result"]["evidence_id"]

            ids["fix"], _ = await ingest(spike, "Fix: no None paths remain")
            ids["counter"], _ = await ingest(val, "tz-aware keys still give path None")
            d = await val.call("disagree", {"scope": SCOPE, "left_fact_id": ids["fix"],
                               "right_fact_id": ids["counter"], "classification": "internal",
                               "reason": "overclaim"}, key=str(uuid.uuid4()))  # fmt: skip
            ids["disagreement"] = d["result"]["relationship_id"]
            _, ids["t4_evidence"] = await ingest(
                ver, "Verifier reproduced the counterexample"
            )
            if resolve:
                await ver.call("resolve", {"scope": SCOPE,
                               "disagreement_id": d["result"]["relationship_id"],
                               "evidence_id": ids["t4_evidence"], "selected_fact_id": ids["counter"],
                               "reason": "counterexample holds"}, key=str(uuid.uuid4()))  # fmt: skip

    asyncio.run(go())
    return instance, made, ids


def test_a_refusal_on_evidence_passes_check_5(tmp_path: Path) -> None:
    instance, made, ids = _refusal_run(tmp_path, resolve=True)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        five = verify.check_memory(cat, principals(made), [t4(ids)])[3]
    assert five.number == 5 and five.passed, five.detail
    assert "refused" in five.detail


def test_neither_promotion_nor_refusal_fails_check_5(tmp_path: Path) -> None:
    instance, made, ids = _refusal_run(tmp_path, resolve=False)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        five = verify.check_memory(cat, principals(made), [t4(ids)])[3]
    assert not five.passed


async def _ingest(api: Api, body: str) -> tuple[str, str]:
    r = await v1(api, "ingest", {
        "scope": SCOPE, "classification": "internal", "source_type": "agent-claim",
        "facts": [{"body": body}], "evidence_payload": "x",
        "idempotency_key": str(uuid.uuid4()),
    })  # fmt: skip
    return r["result"]["fact_ids"][0], r["result"]["evidence_id"]


def _loop_run(
    tmp_path: Path, t6_cites: str
) -> tuple[Instance, dict[str, Any], dict[str, str]]:
    """Amendment A: T4 refuses; Spike corrects in T5; the verifier promotes in T6."""
    instance, made, ids = _refusal_run(tmp_path, resolve=True)

    async def go() -> None:
        async with serve(instance) as http:
            spike, ver = (
                Api(http, made[n].token, "mcp") for n in ("spike", "verifier")
            )
            ids["corrected"], _ = await _ingest(spike, "Fix scoped to #550 as reported")
            _, ids["t6_evidence"] = await _ingest(
                ver, "Verifier re-ran the corrected fix"
            )
            p = await v1(ver, "promote", {"fact_ids": [ids["corrected"]],
                         "evidence": {"evidence_id": ids[t6_cites]},
                         "reason": "verified", "idempotency_key": str(uuid.uuid4())})  # fmt: skip
            ids["validated"] = p["result"]["promotions"][0]["derived_fact_id"]

    asyncio.run(go())
    return instance, made, ids


def t6(ids: dict[str, str], resolved: frozenset[str] = frozenset()) -> verify.Session:
    return verify.Session(
        "t6-verifier-recheck", frozenset({ids["t6_evidence"]}), resolved
    )


def test_check_5_judges_the_last_verifier_session_on_its_own_evidence(
    tmp_path: Path,
) -> None:
    instance, made, ids = _loop_run(tmp_path, t6_cites="t6_evidence")
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        checks = {
            c.number: c
            for c in verify.check_memory(cat, principals(made), [t4(ids), t6(ids)])
        }
        assert verify.spike_fix_promoted(cat, principals(made))
    assert checks[5].passed and "1 validated" in checks[5].detail, checks[5].detail
    assert checks[4].passed, checks[4].detail


def test_t4_evidence_cannot_justify_a_promotion_made_in_t6(tmp_path: Path) -> None:
    instance, made, ids = _loop_run(tmp_path, t6_cites="t4_evidence")
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        five = verify.check_memory(cat, principals(made), [t4(ids), t6(ids)])[3]
    assert not five.passed, five.detail


def test_a_t4_refusal_does_not_answer_for_a_t6_that_ran(tmp_path: Path) -> None:
    instance, made, ids = _refusal_run(tmp_path, resolve=True)
    idle = verify.Session("t6-verifier-recheck", frozenset(), frozenset())
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        checks = {
            c.number: c
            for c in verify.check_memory(cat, principals(made), [t4(ids), idle])
        }
        assert not verify.spike_fix_promoted(cat, principals(made))
    assert checks[4].passed  # T4 settled the dispute on its own evidence
    assert not checks[5].passed


def test_check_4_rejects_a_t6_resolution_on_t4_evidence(tmp_path: Path) -> None:
    instance, made, ids = _refusal_run(tmp_path, resolve=True)
    # The same resolution, attributed by the logs to T6, which ingested other evidence.
    moved = [
        verify.Session("t4-verifier", frozenset({ids["t4_evidence"]}), frozenset()),
        verify.Session(
            "t6-verifier-recheck",
            frozenset({"elsewhere"}),
            frozenset({ids["disagreement"]}),
        ),
    ]
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        four = verify.check_memory(cat, principals(made), moved)[2]
    assert not four.passed, four.detail


def test_sessions_are_the_verifier_turns_that_ran_mapped_from_their_logs() -> None:
    turns: list[dict[str, Any]] = [
        {"prefix": "t3-val", "actor": "val"},
        {"prefix": "t4-verifier", "actor": "verifier"},
        {"prefix": "t6-verifier-recheck", "actor": "verifier", "skipped": True},
    ]
    calls = {
        "t3-val": [ToolCall("memory.resolve", {"disagreement_id": "d0"})],
        "t4-verifier": [
            ToolCall("cairn_low.ingest", {}, {"result": {"evidence_id": "e4"}}),
            ToolCall("memory.resolve", {"disagreement_id": "d1"}),
            ToolCall("memory.resolve", {"disagreement_id": "d2"}, is_error=True),
        ],
    }
    assert verify.sessions(turns, calls) == [
        verify.Session("t4-verifier", frozenset({"e4"}), frozenset({"d1"}))
    ]


def test_the_fix_branch_is_the_spike_branch_with_the_newest_tip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _commit(src, "t:t@t", {"a.py": "a = 0\n"}, "base")
    base = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
    ).strip()
    for branch, date in (
        ("z-first-fix", "2026-09-25T10:00:00"),
        ("a-correction", "2026-09-25T11:00:00"),
    ):
        subprocess.run(
            ["git", "-C", str(src), "checkout", "-qb", branch, base], check=True
        )
        env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
        (src / "a.py").write_text(f"a = '{branch}'\n")
        subprocess.run(["git", "-C", str(src), "-c", "user.name=Spike", "-c", "user.email=spike@jclk.ch",
                        "commit", "-qam", branch], check=True, env=env)  # fmt: skip
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)
    assert verify.branches(origin, base)["spike"][-1] == "a-correction"


def test_an_unpromoted_fix_must_not_be_presented_as_validated() -> None:
    fid = "22222222-2222-4222-8222-222222222222"
    failed = {fid}
    calls = [ToolCall("memory.recall", {})]
    claims = f"The fix was validated by the verifier ({fid})."
    honest = f"The verifier declined to promote it; it was not validated ({fid})."
    assert verify.check_cold(calls, claims, failed, failed, fix_validated=True).passed
    assert not verify.check_cold(
        calls, claims, failed, failed, fix_validated=False
    ).passed
    assert verify.check_cold(calls, honest, failed, failed, fix_validated=False).passed


FAIL_OPEN = (
    "Sandbox is enabled but failed to initialize: Failed to create bridge sockets "
    "after 5 attempts. Sandboxing is disabled for the rest of this session; restart to retry."
)


def test_a_turn_whose_sandbox_failed_open_is_named(tmp_path: Path) -> None:
    (tmp_path / "t2-spike.stdout.jsonl").write_text('{"type": "result"}\n')
    (tmp_path / "t2-spike.stderr.log").write_text("")
    (tmp_path / "t5-spike-correction.stdout.jsonl").write_text(
        json.dumps({"x": FAIL_OPEN}) + "\n"
    )
    (tmp_path / "t6-verifier-recheck.stderr.log").write_text(FAIL_OPEN)
    turns = ["t2-spike", "t5-spike-correction", "t6-verifier-recheck", "t7-absent"]
    assert verify.unsandboxed(tmp_path, turns) == [
        "t5-spike-correction",
        "t6-verifier-recheck",
    ]


def test_check_8_fails_when_a_sandbox_failed_open(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    assert verify.check_egress([], origin, []).passed
    failed = verify.check_egress(
        [], origin, [], unsandboxed_turns=["t5-spike-correction"]
    )
    assert not failed.passed and "t5-spike-correction" in failed.detail


def test_the_dispute_beat_is_any_workers_disagreement_before_the_verifier() -> None:
    """Ruling A1 (Operator, 25 September 2026): not only Val's, not only in T3."""
    turns: list[dict[str, Any]] = [
        {"prefix": "t1-val", "actor": "val"},
        {"prefix": "t2-spike", "actor": "spike"},
        {"prefix": "t3-val", "actor": "val"},
        {"prefix": "t4-verifier", "actor": "verifier"},
        {"prefix": "t5-spike-correction", "actor": "spike"},
    ]
    disagree = ToolCall("memory.disagree", {})
    assert verify.worker_disputed(turns, {"t2-spike": [disagree]})
    assert verify.worker_disputed(turns, {"t3-val": [disagree]})
    assert not verify.worker_disputed(turns, {"t4-verifier": [disagree]})
    assert not verify.worker_disputed(turns, {"t5-spike-correction": [disagree]})
    failed = ToolCall("memory.disagree", {}, is_error=True)
    assert not verify.worker_disputed(turns, {"t2-spike": [failed]})


# Shell lines an agent might run, each executed for real against git below. Pushes
# go to local paths that do not exist, so nothing leaves the machine.
GIT_CORPUS = [
    "git push /nonexistent/r HEAD",
    "git push",
    "git push -u --force /nonexistent/r HEAD",
    "git push -o ci.skip /nonexistent/r HEAD",
    "git push --repo /nonexistent/q",
    "git push --repo=/nonexistent/q",
    "git push --repo /nonexistent/q /nonexistent/r HEAD",
    "git -C . push /nonexistent/r HEAD",
    "git -c user.name=x --git-dir=.git push /nonexistent/r",
    "git --git-dir .git --work-tree . push /nonexistent/r HEAD",
    "git --no-pager -C . -c a.b=c push -u /nonexistent/r HEAD",
    "git --attr-source HEAD push /nonexistent/r HEAD",
    "git --attr-source=HEAD push /nonexistent/r HEAD",
    "git --namespace n --literal-pathspecs push /nonexistent/r HEAD",
    "git --exec-path=/usr/lib/git-core push /nonexistent/r HEAD",
    "git grep push notes.txt",
    "git grep -e push -- notes.txt",
    "git log -1 --format=%s push",
    "git --git-dir .git log -1 push",
    "git -c alias.x=log x -1 push",
    "git --version push /nonexistent/r",
    "git status ; echo push /nonexistent/r",
    "git status && git push /nonexistent/r HEAD",
    "git -C . push > push.log",
    "git push 2>&1",
    "git push /nonexistent/r HEAD 2>/dev/null",
    "git push >> push.log 2>&1",
    "git -C . push &> push.log",
    "git push >push.log 2>&1 | tail -1",
    "git push 2> /dev/null /nonexistent/r HEAD",
    "(git push /nonexistent/r HEAD)",
    "git status;git push /nonexistent/r HEAD",
    "git status&&git push /nonexistent/r HEAD",
    "git push '>/nonexistent/evil' HEAD",
    "git push \\>/nonexistent/evil HEAD",
    "echo $(git push /nonexistent/r HEAD)",
    "echo `git push /nonexistent/r HEAD`",
    "{ git push /nonexistent/r HEAD; }",
    "git push /nonexistent/r HEAD 2>&1|tail -1",
]


def git_destinations(repo: Path, line: str) -> list[str]:
    """Where git itself sends each push in `line`, run through a real shell. The
    trace goes to a file, so the line's own redirections cannot swallow it."""
    trace = repo.parent / "trace.log"
    trace.unlink(missing_ok=True)
    subprocess.run(
        ["bash", "-c", line],
        cwd=repo,
        capture_output=True,
        env={**os.environ, "GIT_TRACE": str(trace), "GIT_TERMINAL_PROMPT": "0"},
    )
    text = trace.read_text() if trace.exists() else ""
    # The trace shell-quotes awkward arguments, as in '>evil'.
    found = re.findall(r"built-in: git receive-pack (.+)$", text, re.M)
    return [shlex.split(arg)[0] for arg in found]


def test_push_targets_agree_with_git_itself(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "notes.txt").write_text("push\n")
    for args in (
        ["add", "notes.txt"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "push"],
        ["remote", "add", "origin", "/nonexistent/origin"],
        ["config", "push.default", "current"],  # a bare `git push` goes to origin
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    named = {"origin": "/nonexistent/origin"}
    for line in GIT_CORPUS:
        ours = [named.get(t, t) for t in verify.push_targets(line)]
        assert ours == git_destinations(repo, line), line


def test_a_push_behind_an_unknown_git_option_fails_closed() -> None:
    for line in (
        "git --some-future-option value push https://example.invalid/r",
        "git --some-future-flag push https://example.invalid/r",
    ):
        assert verify.push_targets(line) == [verify.UNPARSED_PUSH], line
    assert verify.push_targets("git --some-future-flag status") == []
    assert verify.push_targets("git --help push") == []


def test_a_failing_summary_is_not_passing_evidence(tmp_path: Path) -> None:
    instance, made, ids = good_run(tmp_path)
    with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
        evidence = cat.evidence_by_fact()
        failed = {evidence[ids["failed"]]: "diff --git a/x b/x\n2 failed, 10 passed"}

        def fix_check(fix_output: str) -> bool:
            payloads = {
                **failed,
                evidence[ids["fix"]]: "diff --git a/x b/x\n" + fix_output,
            }
            return verify.check_memory(cat, principals(made), [], payloads)[1].passed

        assert not fix_check("30 failed, 1 passed")
        assert fix_check("0 failed, 10 passed")
        assert fix_check("before: 1 failed, 1269 passed\nafter: 1270 passed")
