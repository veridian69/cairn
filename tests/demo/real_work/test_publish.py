import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import publish, transcript  # noqa: E402


def test_scrub_replaces_paths() -> None:
    assert (
        publish.scrub("/tmp/rwd-abc/t1-val/work/x.py", {"/tmp/rwd-abc": "<runtime>"})
        == "<runtime>/t1-val/work/x.py"
    )


def test_secret_in_any_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("fine")
    (tmp_path / "b.jsonl").write_text('{"env": "CAIRN_TOKEN=cairn1.deadbeef"}')
    hits = publish.secret_hits(
        [tmp_path / "a.md", tmp_path / "b.jsonl"], [b"cairn1.deadbeef"]
    )
    assert hits == [str(tmp_path / "b.jsonl")]


def _out(tmp_path: Path, tokens: dict[str, str]) -> Path:
    out = tmp_path / "out"
    (out / "config").mkdir(parents=True)
    (out / "config" / "tokens.json").write_text(json.dumps(tokens))
    (out / "run-metadata.json").write_text(json.dumps({"runtime": "/tmp/rwd-x"}))
    return out


def test_run_tokens_and_credential_leaves_are_secrets(tmp_path: Path) -> None:
    out = _out(tmp_path, {"val": "cairn1.valtoken"})
    cred = tmp_path / ".credentials.json"
    cred.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-ant-oat01-XXXXXXXXXXXXXXXX", "x": 1}}
        )
    )
    secrets = publish.collect_secrets(out, [cred])
    assert b"cairn1.valtoken" in secrets
    assert b"sk-ant-oat01-XXXXXXXXXXXXXXXX" in secrets


def test_publish_refuses_when_a_provider_credential_survives(tmp_path: Path) -> None:
    out = _out(tmp_path, {"spike": "cairn1.spiketoken"})
    cred = tmp_path / "auth.json"
    cred.write_text(
        json.dumps({"tokens": {"access_token": "eyJhbGciOiJSUzI1NiJ9.provider-secret"}})
    )
    (out / "transcript.md").write_text("env dump: eyJhbGciOiJSUzI1NiJ9.provider-secret")
    (out / "verification.json").write_text("{}")
    with pytest.raises(RuntimeError, match="transcript.md"):
        publish.publish(out, tmp_path / "dest", [cred])
    assert not (tmp_path / "dest").exists()


def test_dead_run_tokens_are_redacted_not_fatal(tmp_path: Path) -> None:
    out = _out(tmp_path, {"spike": "cairn1.spiketoken"})
    (out / "transcript.md").write_text("CAIRN_TOKEN=cairn1.spiketoken")
    (out / "verification.json").write_text("{}")
    publish.publish(out, tmp_path / "dest", [])
    assert (
        tmp_path / "dest" / "transcript.md"
    ).read_text() == "CAIRN_TOKEN=<token:spike>"


def test_scan_covers_storyboard_and_companion_page(tmp_path: Path) -> None:
    out = _out(tmp_path, {"val": "cairn1.valtoken"})
    story = tmp_path / "story.json"
    story.write_text('{"body": "cairn1.valtoken"}')
    page = tmp_path / "page.md"
    page.write_text("clean")
    assert publish.scan([story, page], out, []) == [str(story)]


def test_publish_scrubs_and_copies_the_publication_set(tmp_path: Path) -> None:
    out = _out(tmp_path, {"spike": "cairn1.spiketoken"})
    (out / "transcript.md").write_text("ran in /tmp/rwd-x/t2-spike/work")
    (out / "verification.json").write_text("{}")
    written = publish.publish(out, tmp_path / "dest", [])
    assert sorted(p.name for p in written) == ["transcript.md", "verification.json"]
    assert (
        tmp_path / "dest" / "transcript.md"
    ).read_text() == "ran in <runtime>/t2-spike/work"


def test_transcript_renders_every_turn_in_full(tmp_path: Path) -> None:
    out = tmp_path
    long = "x" * 5000
    (out / "run-metadata.json").write_text(json.dumps({"turns": [
        {"prefix": "t1-val", "actor": "val", "result": {"exit": 1, "timed_out": False, "wall_s": 61.0}},
    ]}))  # fmt: skip
    (out / "t1-val.stdout.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "pytest", "exit_code": 1, "aggregated_output": long}}) + "\n")  # fmt: skip
    (out / "t1-val-final.txt").write_text("Handed over to spike.")
    text = transcript.render(out)
    assert "## t1-val" in text and "val" in text and "61.0 s" in text
    assert long in text  # never truncated
    assert "Handed over to spike." in text


def test_retained_turn_credentials_are_secrets_without_being_passed(
    tmp_path: Path,
) -> None:
    out = _out(tmp_path, {})
    (out / "config" / "t2-spike.credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-ant-oat01-REFRESHED-DURING-RUN"}}
        )
    )
    (out / "config" / "t1-val.auth.json").write_text(
        json.dumps({"tokens": {"id_token": "eyJ-codex-refreshed-token"}})
    )
    secrets = publish.collect_secrets(out, [])
    assert (
        b"sk-ant-oat01-REFRESHED-DURING-RUN" in secrets
        and b"eyJ-codex-refreshed-token" in secrets
    )


def test_every_verification_report_is_published_through_the_scrub(
    tmp_path: Path,
) -> None:
    out = _out(tmp_path, {"spike": "cairn1.spiketoken"})
    (out / "transcript.md").write_text("t")
    (out / "verification.json").write_text('{"demo_usable": false}')
    (out / "verification-ruling-a1.json").write_text('{"note": "cairn1.spiketoken"}')
    written = publish.publish(out, tmp_path / "dest", [])
    assert sorted(p.name for p in written) == [
        "transcript.md", "verification-ruling-a1.json", "verification.json",
    ]  # fmt: skip
    assert (
        "<token:spike>"
        in (tmp_path / "dest" / "verification-ruling-a1.json").read_text()
    )


def test_path_derived_directory_names_are_scrubbed_too(tmp_path: Path) -> None:
    """Claude Code names project directories after the path, with / and . as -."""
    out = _out(tmp_path, {})
    home = str(Path.home())
    slug = "-" + "/tmp/rwd-x".strip("/").replace("/", "-").replace(".", "-")
    home_slug = home.replace("/", "-").replace(".", "-")
    (out / "transcript.md").write_text(
        f"/tmp/rwd-x/t7/.claude/projects/{slug}-t7-work/1.txt {home_slug}-other"
    )
    (out / "verification.json").write_text("{}")
    publish.publish(out, tmp_path / "dest", [])
    text = (tmp_path / "dest" / "transcript.md").read_text()
    assert (
        text
        == "<runtime>/t7/.claude/projects/<runtime-slug>-t7-work/1.txt <home-slug>-other"
    )


def test_extra_redactions_apply_to_every_published_file(tmp_path: Path) -> None:
    out = _out(tmp_path, {})
    # Split with a trailing comma so the line still fits once the exporter's
    # sanitiser lengthens the address.
    (out / "transcript.md").write_text(
        'git -c user.email="operator@example.test" commit',
    )
    (out / "verification.json").write_text('{"by": "operator@example.test"}')
    publish.publish(
        out, tmp_path / "dest", [], {"operator@example.test": "<operator-email>"}
    )
    for name in ("transcript.md", "verification.json"):
        text = (tmp_path / "dest" / name).read_text()
        assert "operator@example.test" not in text and "<operator-email>" in text


def test_the_local_account_name_is_scrubbed_but_email_domains_are_not(
    tmp_path: Path,
) -> None:
    import getpass

    user = getpass.getuser()
    out = _out(tmp_path, {})
    (out / "transcript.md").write_text(
        f"-rw-rw----  1 {user} {user}    21 Sep 25 x\nauthor <val@{user}.ch>, host {user}.io"
    )
    (out / "verification.json").write_text("{}")
    publish.publish(out, tmp_path / "dest", [])
    text = (tmp_path / "dest" / "transcript.md").read_text()
    assert text == (
        f"-rw-rw----  1 <user> <user>    21 Sep 25 x\nauthor <val@{user}.ch>, host {user}.io"
    )


def test_the_whole_harness_passes_the_public_export_sanitiser() -> None:
    """Operator, 25 September 2026: the harness is published for provenance."""
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    source = root / "public-release" / "sanitise.py"
    if not source.exists():
        pytest.skip("the export sanitiser is private tooling; not in the public tree")
    spec = importlib.util.spec_from_file_location("sanitise", source)
    assert spec is not None and spec.loader is not None
    sanitise = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sanitise)
    files = sorted((root / "scripts" / "real_work_demo").glob("*.py"))
    files += sorted((root / "tests" / "demo").rglob("*.py"))
    for path in files:
        name = path.relative_to(root).as_posix()
        sanitise.public_payload({name: (path.read_bytes(), 0o644)})  # raises if not
