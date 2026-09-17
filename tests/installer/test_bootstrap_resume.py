import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn_install.bootstrap import bootstrap
from cairn_install.core import Context, InstallError, open_context

TOKEN = "cairn1.12345678-1234-4234-8234-123456789abc." + "a" * 43
OTHER_TOKEN = "cairn1.87654321-4321-4321-8321-cba987654321." + "b" * 43


class FakeBackend:
    def lifecycle_argv(self, operation: str) -> list[str]:
        return [operation]


def _context(tmp_path: Path) -> Context:
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    )


def _lifecycle(ctx: Context, calls: list[str]) -> Callable[..., str]:
    def command(argv: list[str], **kwargs: Any) -> str:
        calls.append(argv[0])
        return json.dumps(
            {
                "status": "ok",
                "operation": argv[0],
                "instance_id": ctx.instance_id,
                "realm_count": 1,
            }
        )

    return command


def _record_completed_bootstrap(
    ctx: Context, *, capture_payload: dict[str, Any] | None = None
) -> tuple[Path, Path]:
    capture = ctx.root / "credentials" / "bootstrap.json"
    credential = ctx.root / "credentials" / "admin.token"
    payload = capture_payload or {
        "status": "ok",
        "operation": "bootstrap",
        "instance_id": ctx.instance_id,
        "realm_id": "local",
        "token": TOKEN,
    }
    ctx.state["bootstrap_intent"] = str(capture)
    ctx.save()
    ctx.write_file(capture, json.dumps(payload), secret=True)
    ctx.write_file(credential, TOKEN + "\n", secret=True)
    return capture, credential


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "wrong-instance"),
        ("operation", "recover"),
        ("realm_id", "other"),
        ("token", OTHER_TOKEN),
    ],
)
def test_resume_rejects_admin_token_when_bootstrap_capture_disagrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    with _context(tmp_path) as ctx:
        payload = {
            "status": "ok",
            "operation": "bootstrap",
            "instance_id": ctx.instance_id,
            "realm_id": "local",
            "token": TOKEN,
        }
        payload[field] = value
        capture, credential = _record_completed_bootstrap(ctx, capture_payload=payload)
        before = (capture.read_bytes(), credential.read_bytes())
        calls: list[str] = []
        monkeypatch.setattr(ctx, "command", _lifecycle(ctx, calls))

        with pytest.raises(InstallError):
            bootstrap(ctx, FakeBackend())

        assert (capture.read_bytes(), credential.read_bytes()) == before
        assert "bootstrap" not in calls
        log = (ctx.directory / "commands.log").read_text()
        assert TOKEN not in log
        assert OTHER_TOKEN not in log


def test_resume_rejects_missing_recorded_bootstrap_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _context(tmp_path) as ctx:
        capture, credential = _record_completed_bootstrap(ctx)
        capture.unlink()
        before = credential.read_bytes()
        calls: list[str] = []
        monkeypatch.setattr(ctx, "command", _lifecycle(ctx, calls))

        with pytest.raises(InstallError):
            bootstrap(ctx, FakeBackend())

        assert not capture.exists()
        assert credential.read_bytes() == before
        assert "bootstrap" not in calls


def test_resume_accepts_matching_recorded_capture_created_by_bootstrap_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _context(tmp_path) as ctx:
        capture, credential = _record_completed_bootstrap(ctx)
        del ctx.state["owned_files"][str(capture)]
        del ctx.state["file_intents"][str(capture)]
        ctx.save()
        calls: list[str] = []
        monkeypatch.setattr(ctx, "command", _lifecycle(ctx, calls))

        bootstrap(ctx, FakeBackend())

        assert ctx.read_secret(capture)
        assert ctx.read_secret(credential) == TOKEN
        assert "bootstrap" not in calls


def test_resume_rejects_unowned_bootstrap_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _context(tmp_path) as ctx:
        capture, credential = _record_completed_bootstrap(ctx)
        del ctx.state["bootstrap_intent"]
        del ctx.state["owned_files"][str(capture)]
        del ctx.state["file_intents"][str(capture)]
        ctx.save()
        calls: list[str] = []
        monkeypatch.setattr(ctx, "command", _lifecycle(ctx, calls))

        with pytest.raises(InstallError, match="Unowned bootstrap capture"):
            bootstrap(ctx, FakeBackend())

        assert credential.read_text().strip() == TOKEN
        assert "bootstrap" not in calls
