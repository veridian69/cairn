"""Host boundary checks; no provider calls or productive configuration reads."""

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from cairn.client import ModelTurn, RecalledMemory, TurnInput
from cairn.client.types import freeze_object

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import host_handover as host  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    "value",
    [
        {"response": "Saved", "observations": [], "scope": "wider"},
        {
            "response": "Saved",
            "observations": [{"body": "candidate", "trust": "validated"}],
        },
        {"response": "Saved", "observations": [{"body": ""}]},
        {"response": "Saved", "observations": [{"body": "x" * 4097}]},
        {"response": "Saved", "observations": [] * 0, "receipt": "invented"},
        {"response": "Saved", "observations": [{"body": "candidate"}] * 9},
    ],
)
def test_model_cannot_supply_authority_or_unbounded_observations(value: object) -> None:
    with pytest.raises(host.HostFailure):
        host.parse_turn(value)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_cli_gets_only_selected_provider_auth_and_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    auth = tmp_path / "synthetic-provider-auth.json"
    auth.write_text('{"synthetic_provider_login": true}')
    monkeypatch.setenv("CAIRN_TOKEN", "synthetic-do-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    monkeypatch.setenv("CLAUDECODE", "synthetic-parent-marker")
    seen: list[Path] = []

    async def fake_capture(
        command: list[str],
        prompt: bytes = b"",
        deadline: float = 180,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[bytes, bytes]:
        assert environment is not None and cwd is not None
        assert not {"CAIRN_TOKEN", "OPENAI_API_KEY", "CLAUDECODE"} & environment.keys()
        home = Path(environment["HOME"])
        seen.append(home)
        files = await asyncio.to_thread(
            lambda: sorted(
                p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()
            )
        )
        expected = (
            ".codex/auth.json" if provider == "codex" else ".claude/.credentials.json"
        )
        assert files == [expected]
        assert (home / expected).stat().st_mode & 0o777 == 0o600
        packet = json.loads(prompt)
        assert packet["cairn_memory"]["content_role"] == "untrusted-data"
        assert "synthetic_provider_login" not in prompt.decode()
        assert packet["task"] == "A useful conversation"
        turn = {
            "response": "The model says saved",
            "observations": [{"body": "A candidate"}],
        }
        if provider == "codex":
            assert "--ignore-user-config" in command and "--ephemeral" in command
            assert "read-only" in command and 'forced_login_method="chatgpt"' in command
            (cwd / "result.json").write_text(json.dumps(turn))
            return b"", b""
        assert "--safe-mode" in command and "--bare" not in command
        assert command[command.index("--tools") + 1] == ""
        assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        return json.dumps({"structured_output": turn}).encode(), b""

    monkeypatch.setattr(host, "capture", fake_capture)
    adapter = host.CliHosts(
        {provider: {"binary": Path("/synthetic/cli"), "auth": auth}}, "test", "test"
    )
    context = TurnInput("user", RecalledMemory(freeze_object({"hits": []})))
    result = await adapter.ask(provider, "A useful conversation", context)
    assert result.response == "The model says saved"
    assert isinstance(result, ModelTurn)
    assert not hasattr(result, "persistence")
    assert adapter.invocations == 1
    assert all(not path.exists() for path in seen)
    assert auth.read_text() == '{"synthetic_provider_login": true}'


@pytest.mark.anyio
async def test_lost_remember_response_never_reinvokes_model(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fixture(provider: str, task: str, context: TurnInput) -> ModelTurn:
        calls.append(provider)
        return await host.fixture_ask(provider, task, context)

    report = await host.handover(tmp_path, fixture)
    assert calls == ["codex", "claude", "codex"]
    assert [row["status"] for row in report["receipts"]] == ["committed", "replayed"]
    assert report["catalogue_verification"]["fact_count"] == 2
    assert "server_denied_sibling_scope" in report["checks"]
    arrivals = report["arrivals"]
    assert [row["principal_id"] for row in arrivals] == [
        report["principals"]["Val"],
        report["principals"]["Spike"],
        report["principals"]["Val"],
    ]
    assert [row["summary_fact_count"] for row in arrivals] == [0, 1, 2]


@pytest.mark.anyio
@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("quoted", ["neither", "body", "author"])
async def test_failed_content_proof_reports_missing_parts_without_response_text(
    tmp_path: Path, direction: str, quoted: str
) -> None:
    report: dict[str, Any] = {}
    calls = 0
    failure_at = 2 if direction == "forward" else 3

    async def incomplete(provider: str, task: str, context: TurnInput) -> ModelTurn:
        nonlocal calls
        calls += 1
        turn = await host.fixture_ask(provider, task, context)
        if calls != failure_at:
            return turn
        hits = host.thaw(context.recalled.data)["hits"]
        row = (
            hits[0]
            if direction == "forward"
            else next(row for row in hits if "return inspection" in row["body"])
        )
        response = "synthetic-private-response-not-for-report"
        if quoted == "body":
            response += row["body"]
        elif quoted == "author":
            response += row["source_principal_id"]
        return ModelTurn(response, turn.observations)

    with pytest.raises(
        host.HostFailure, match=f"^{direction}_handover_not_demonstrated$"
    ):
        await host.handover(tmp_path, incomplete, report)
    assert calls == failure_at
    assert report["handover_proofs"][-1] == {
        "direction": direction,
        "recall_contains_expected_fact": True,
        "response_contains_expected_body": quoted == "body",
        "response_contains_expected_author": quoted == "author",
    }
    assert "synthetic-private-response-not-for-report" not in json.dumps(report)
    assert len(report["receipts"]) == failure_at - 1


@pytest.mark.anyio
async def test_process_failure_never_exposes_raw_output() -> None:
    with pytest.raises(host.HostFailure, match="^host_failed$"):
        await host.capture(
            [
                sys.executable,
                "-c",
                "import sys; print('synthetic-private-value',file=sys.stderr); sys.exit(1)",
            ]
        )


@pytest.mark.anyio
async def test_output_limit_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "LIMIT", 100)
    with pytest.raises(host.HostFailure, match="host_output_limit"):
        await host.capture([sys.executable, "-c", "print('x'*200)"])


def test_missing_cli_is_a_safe_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(host.HostFailure, match="^codex_missing$"):
        host.preflight()


def test_expired_subscription_stops_before_host_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / ".claude"
    folder.mkdir()
    (folder / ".credentials.json").write_text('{"claudeAiOauth":{"expiresAt":0}}')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/cli")
    with pytest.raises(host.HostFailure, match="^claude_login_expired$"):
        host.preflight(("claude",))


@pytest.mark.anyio
async def test_expired_oauth_error_is_classified_without_raw_output() -> None:
    with pytest.raises(host.HostFailure, match="^host_authentication_failed$"):
        await host.capture(
            [
                sys.executable,
                "-c",
                "import sys; print('Failed to authenticate: OAuth session expired and could not be refreshed'); sys.exit(1)",
            ]
        )


def test_timeout_reaps_orphan_group_without_signalling_unrelated_process(
    tmp_path: Path,
) -> None:
    # Isolate Linux subreaper state in this test supervisor, not the pytest process.
    # This lets the test prove the orphan was killed/reaped without relying on PID 1.
    supervisor = textwrap.dedent(r"""
        import asyncio, ctypes, os, signal, subprocess, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from scripts.host_handover import capture, HostFailure
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
        pidfile = Path(sys.argv[2])
        forker = (
            "import os, sys, time; from pathlib import Path\n"
            "if os.fork() == 0:\n"
            "    Path(sys.argv[1]).write_text(f'{os.getpid()} {os.getpgrp()}')\n"
            "    time.sleep(30)\n"
            "os._exit(0)\n"
        )
        async def exercise():
            unrelated = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                start_new_session=True,
            )
            child = group = None
            reaped = False
            try:
                try:
                    await capture([sys.executable, '-c', forker, str(pidfile)], deadline=.3)
                except HostFailure as failure:
                    assert str(failure) == 'host_timeout'
                else:
                    raise AssertionError('Expected held-pipe timeout')
                child, group = map(int, pidfile.read_text().split())
                assert group != os.getpgrp() and group != unrelated.pid
                for _ in range(100):
                    pid, status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                        reaped = True
                        break
                    await asyncio.sleep(.01)
                assert reaped, 'capture left its orphan descendant running'
                assert unrelated.poll() is None, 'capture signalled an unrelated process'
            finally:
                if child is None and pidfile.exists():
                    child, group = map(int, pidfile.read_text().split())
                if child is not None and not reaped:
                    try: os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError: pass
                    os.waitpid(child, 0)
                unrelated.terminate()
                unrelated.wait(timeout=5)
        asyncio.run(exercise())
    """)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            supervisor,
            str(Path(__file__).resolve().parents[2]),
            str(tmp_path / "orphan.pid"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "malformed",
    [
        b"{",
        b"\xff",
        b"[" * 2000 + b"]" * 2000,
        b"1" * 5000,
        OSError("synthetic file failure: do not expose"),
        RuntimeError("synthetic parse failure: do not expose"),
        asyncio.CancelledError(),
    ],
    ids=["syntax", "encoding", "nesting", "numeric-limit", "file", "parse", "cancel"],
)
def test_malformed_host_output_preserves_prior_receipts_and_invocation_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    malformed: bytes | BaseException,
) -> None:
    auth = tmp_path / "synthetic-provider-auth.json"
    auth.write_text('{"synthetic_provider_login":true}')
    locations = {
        name: {"binary": Path(f"/synthetic/{name}"), "auth": auth}
        for name in ("codex", "claude")
    }
    monkeypatch.setattr(host, "preflight", lambda: locations)
    calls = 0
    failure_at = 3 if provider == "codex" else 2
    original_decode = host.decode_host_json

    def decode_with_pipeline_failure(data: bytes) -> Any:
        if calls == failure_at and isinstance(malformed, BaseException):
            raise malformed
        return original_decode(data)

    monkeypatch.setattr(host, "decode_host_json", decode_with_pipeline_failure)

    async def fake_capture(
        command: list[str],
        prompt: bytes = b"",
        deadline: float = 180,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[bytes, bytes]:
        nonlocal calls
        calls += 1
        assert cwd is not None
        packet = json.loads(prompt)
        rows = packet["cairn_memory"]["data"]["hits"]
        task = packet["task"]
        _, selection, body = task.partition("Select exactly this observation: ")
        turn: dict[str, Any] = {
            "response": " ".join(
                f"{row['body']} {row['source_principal_id']}" for row in rows
            ),
            "observations": [{"body": body}] if selection else [],
        }
        codex = command[0] == "/synthetic/codex"
        if calls == failure_at:
            output = malformed if isinstance(malformed, bytes) else b"{}"
        else:
            output = json.dumps(turn if codex else {"structured_output": turn}).encode()
        if codex:
            (cwd / "result.json").write_bytes(output)
            return b"", b""
        return output, b""

    monkeypatch.setattr(host, "capture", fake_capture)
    monkeypatch.setattr(sys, "argv", ["host_handover.py", "run"])
    if isinstance(malformed, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            host.main()
        assert capsys.readouterr().out == ""
        return
    with pytest.raises(SystemExit) as stopped:
        host.main()
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    if isinstance(malformed, Exception):
        assert report["code"] == "harness_failed"
        assert "do not expose" not in json.dumps(report)
    elif isinstance(malformed, bytes) and malformed.startswith(b"["):
        # Valid nested JSON can parse under the runner's recursion limit.
        # Either decoder rejection or the host's shape check is a safe failure.
        shape_failure = (
            "invalid_structured_output"
            if provider == "codex"
            else "host_reported_failure"
        )
        assert report["code"] in {"host_invalid_json", shape_failure}
    else:
        assert report["code"] == "host_invalid_json"
    assert report["host_invocations"] == calls == failure_at
    assert report["fixture_callbacks"] == 0
    assert report["real_cli_handover"] is False
    assert report["receipts"][0]["status"] == "committed"
    assert report["receipts"][0]["audit_receipt"]["event_id"]
    assert len(report["receipts"]) == failure_at - 1
    assert len(report["arrivals"]) == failure_at
