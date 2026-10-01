import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest  # noqa: E402

from scripts.real_work_demo import run, workspace  # noqa: E402

BUILT: list[Path] = []


@pytest.fixture(autouse=True)
def no_real_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv sync needs deepdiff's real pyproject; record the checkout instead."""

    def fake(checkout: Path, venv: Path) -> str:
        BUILT.append(checkout)
        venv.mkdir(parents=True, exist_ok=True)
        return "lock-sha"

    BUILT.clear()
    monkeypatch.setattr(workspace, "build_venv", fake)


def test_timeout_kills_the_group_and_reports(tmp_path: Path) -> None:
    r = run.run_turn(["bash", "-c", "sleep 30 & sleep 30"], "", {"PATH": "/usr/bin:/bin"},
                     tmp_path, tmp_path / "t", timeout_s=1)  # fmt: skip
    assert r["timed_out"] is True and r["exit"] is None
    assert (tmp_path / "t.stdout.jsonl").exists()


def test_nonzero_exit_is_reported_not_raised(tmp_path: Path) -> None:
    r = run.run_turn(["bash", "-c", "exit 3"], "", {"PATH": "/usr/bin:/bin"},
                     tmp_path, tmp_path / "t", timeout_s=10)  # fmt: skip
    assert r == {"exit": 3, "timed_out": False, "wall_s": r["wall_s"]}


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    import subprocess

    repo = tmp_path / "deepdiff"
    repo.mkdir()
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "uv.lock").write_text("lock\n")
    subprocess.run([*g, "-C", str(repo), "add", "."], check=True)
    subprocess.run([*g, "-C", str(repo), "commit", "-qm", "seed"], check=True)
    rev = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, rev


def test_main_wires_seven_turns_without_providers(tmp_path: Path) -> None:
    import json

    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    code = run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", "cat >/dev/null"])  # fmt: skip
    assert code == 0
    meta = json.loads((out / "run-metadata.json").read_text())
    assert [t["prefix"] for t in meta["turns"]] == [
        "t1-val", "t2-spike", "t3-val", "t4-verifier",
        "t5-spike-correction", "t6-verifier-recheck", "t7-spike-cold",
    ]  # fmt: skip
    assert all(t["result"]["exit"] == 0 for t in meta["turns"])
    assert (out / "snapshot" / "cairn" / "catalogue.sqlite3").exists()
    assert (out / "origin.git" / "HEAD").exists()
    cold = json.loads((out / "config" / "t7-spike-cold.mcp.json").read_text())
    assert "garden" not in cold["mcpServers"]
    t2 = json.loads((out / "config" / "t2-spike.mcp.json").read_text())
    assert "garden" in t2["mcpServers"]
    assert set(meta["principals"]) == {"val", "spike", "verifier", "spike-cold"}
    assert meta["deepdiff_rev"] == rev and meta["runtime"] and meta["token_files"]
    report = json.loads((out / "verification.json").read_text())
    assert report["evidence_valid"] is False and report["demo_usable"] is False
    assert report["beats"]["loop_closed"] is False
    assert [c["number"] for c in report["checks"]] == [1, 2, 3, 4, 5, 6, 7, 8]
    tokens = json.loads((out / "config" / "tokens.json").read_text())
    assert set(tokens) == {"val", "spike", "verifier", "spike-cold"}
    assert (out / "config" / "tokens.json").stat().st_mode & 0o777 == 0o600
    assert (out / "transcript.md").read_text().startswith("# Real-work demo transcript")


def test_verification_reruns_from_the_snapshot_alone(tmp_path: Path) -> None:
    import json

    from scripts.real_work_demo import verify

    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    assert run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", "cat >/dev/null"]) == 0  # fmt: skip
    first = json.loads((out / "verification.json").read_text())
    # The runtime, server and venv are gone; a re-run must still work.
    assert not Path(
        json.loads((out / "run-metadata.json").read_text())["runtime"]
    ).exists()
    again = verify.verify(out, build_venv=False)
    assert [c["passed"] for c in again["checks"]] == [
        c["passed"] for c in first["checks"]
    ]


def test_a_verification_crash_is_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from scripts.real_work_demo import verify

    def boom(*_: object, **__: object) -> dict[str, object]:
        raise ValueError("bad log")

    monkeypatch.setattr(verify, "verify", boom)
    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    assert run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", "cat >/dev/null"]) == 1  # fmt: skip
    meta = json.loads((out / "run-metadata.json").read_text())
    assert "ValueError: bad log" in meta["verification_error"]
    assert (out / "snapshot" / "cairn" / "catalogue.sqlite3").exists()
    assert (out / "transcript.md").exists()


def test_evidence_payloads_round_trip_through_the_snapshot(tmp_path: Path) -> None:
    from scripts.real_work_demo import verify

    verify.save_evidence(tmp_path, {"e1": "diff --git a\n2 failed"})
    assert verify.load_evidence(tmp_path) == {"e1": "diff --git a\n2 failed"}
    assert (tmp_path / "snapshot" / "evidence" / "e1").stat().st_mode & 0o777 == 0o600


def test_tokens_are_not_on_disk_under_out_while_agents_run(tmp_path: Path) -> None:
    import json

    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    probe = f"test -e {out}/config/tokens.json && exit 7; cat >/dev/null"
    assert run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", probe]) == 0  # fmt: skip
    meta = json.loads((out / "run-metadata.json").read_text())
    assert all(t["result"]["exit"] == 0 for t in meta["turns"])
    assert (out / "config" / "tokens.json").exists()


def test_the_venv_is_built_from_a_clone_at_the_pin_not_the_checkout(
    tmp_path: Path,
) -> None:
    import json
    import subprocess

    repo, rev = _fixture_repo(tmp_path)
    (repo / "later.txt").write_text("drift\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "drift"], check=True)  # fmt: skip
    out = tmp_path / "out"
    run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
              "--fake-agent", "cat >/dev/null"])  # fmt: skip
    assert BUILT and BUILT[0] != repo
    assert (
        json.loads((out / "run-metadata.json").read_text())["venv_lock_sha256"]
        == "lock-sha"
    )


def test_cold_session_has_its_own_git_identity() -> None:
    emails = {who: ident.split("<")[1] for who, ident in run.GIT_IDENTITY.items()}
    assert len(set(emails.values())) == len(emails)


def test_refreshed_credentials_are_retained_for_the_scrub(tmp_path: Path) -> None:
    config = tmp_path / ".claude"
    config.mkdir()
    (config / ".credentials.json").write_text(
        '{"refreshed": "token-after-refresh-xyz"}'
    )
    out = tmp_path / "out"
    (out / "config").mkdir(parents=True)
    kept = run.retain_credentials(config / ".credentials.json", out, "t2-spike")
    assert kept == out / "config" / "t2-spike.credentials.json"
    assert kept.read_text() == '{"refreshed": "token-after-refresh-xyz"}'
    assert kept.stat().st_mode & 0o777 == 0o600


def test_runtime_lives_outside_tmp_where_codex_refuses_its_helpers(
    tmp_path: Path,
) -> None:
    import json

    from scripts.real_work_demo import probe

    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
              "--fake-agent", "cat >/dev/null"])  # fmt: skip
    runtime = Path(json.loads((out / "run-metadata.json").read_text())["runtime"])
    assert runtime.parent == run.RUNTIME_PARENT == probe.RUNTIME_PARENT
    assert not str(runtime).startswith("/tmp/")


def test_the_correction_loop_is_skipped_when_t4_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from scripts.real_work_demo import verify

    asked: list[int] = []

    def promoted(*_: object) -> bool:
        asked.append(1)
        return True

    monkeypatch.setattr(verify, "spike_fix_promoted", promoted)
    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    assert run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", "cat >/dev/null"]) == 0  # fmt: skip
    meta = json.loads((out / "run-metadata.json").read_text())
    skipped = [t["prefix"] for t in meta["turns"] if t.get("skipped")]
    assert skipped == ["t5-spike-correction", "t6-verifier-recheck"]
    assert (
        meta["turns"][-1]["prefix"] == "t7-spike-cold" and "result" in meta["turns"][-1]
    )
    assert not (out / "t5-spike-correction.stdout.jsonl").exists()
    assert not (out / "workspaces" / "t6-verifier-recheck").exists()
    assert asked, "the loop decision must come from the catalogue"
    assert "t5-spike-correction (spike, skipped)" in (out / "transcript.md").read_text()


def test_a_sandbox_that_failed_open_stops_the_run_after_that_turn(
    tmp_path: Path,
) -> None:
    import json

    repo, rev = _fixture_repo(tmp_path)
    out = tmp_path / "out"
    fake = "cat >/dev/null; echo 'Sandboxing is disabled for the rest of this session'"
    assert run.main(["smoke", "--deepdiff", str(repo), "--rev", rev, "--out", str(out),
                     "--fake-agent", fake]) == 0  # fmt: skip
    meta = json.loads((out / "run-metadata.json").read_text())
    assert [t["prefix"] for t in meta["turns"]] == ["t1-val"]
    assert meta["aborted"] == "sandbox failed open in t1-val"
    report = json.loads((out / "verification.json").read_text())
    eight = next(c for c in report["checks"] if c["number"] == 8)
    assert not eight["passed"] and report["evidence_valid"] is False


def test_codex_cost_counts_input_and_output_once(tmp_path: Path) -> None:
    # Capture 3's T1 event: cached and reasoning counts are parts of the input
    # and output totals, not additions to them.
    stdout = tmp_path / "t1.stdout.jsonl"
    stdout.write_text(
        '{"type":"turn.completed","usage":{"input_tokens":727571,'
        '"cached_input_tokens":690688,"cache_write_input_tokens":0,'
        '"output_tokens":7485,"reasoning_output_tokens":2883}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    assert run._cost(stdout) == {"claude_usd": 0.0, "codex_tokens": 735056 + 15}
