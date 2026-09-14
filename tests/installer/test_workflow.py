from pathlib import Path
from typing import Any

import pytest

from cairn_install import workflow
from cairn_install.core import InstallError, open_context


class Adapter:
    runtime_python = "python"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def validate_ownership(self) -> None:
        self.calls.append("ownership")

    def preflight(self) -> None:
        self.calls.append("preflight")

    def prepare(self) -> None:
        self.calls.append("prepare")

    def is_running(self) -> bool:
        return False

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def restart(self) -> None:
        self.calls.append("restart")

    def rollback(self) -> None:
        self.calls.append("rollback")

    def lifecycle_argv(self, operation: str) -> list[str]:
        return [operation]


def test_failed_verification_retains_stage_and_does_not_claim_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda ctx: Adapter(calls))
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)

    def refuse(*args: Any) -> str:
        raise InstallError("wrong UUID")

    monkeypatch.setattr(workflow, "ready", refuse)
    root = tmp_path / "state"
    with open_context(
        root,
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        with pytest.raises(InstallError, match="wrong UUID"):
            workflow.run_install(ctx)
        assert ctx.state["status"] == "failed"
        assert ctx.state["steps"]["verify"] == "running"
        assert "restart" not in calls
        workflow.rollback_install(ctx)
        assert ctx.state["status"] == "rolled_back"
        assert calls[-1] == "rollback"


def test_restart_only_reads_same_receipt_and_disposable_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda ctx: Adapter(calls))
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)
    monkeypatch.setattr(workflow, "ready", lambda *args: "token")

    def ingest(ctx: Any, token: str) -> dict[str, str]:
        calls.append("ingest")
        ctx.state["receipts"]["ingest"] = {"proof": "same"}
        return {"proof": "same"}

    monkeypatch.setattr(workflow, "ingest", ingest)
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(
        workflow, "verify_reads", lambda ctx, python, receipt: seen.append(receipt)
    )
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        workflow.run_install(ctx)
        assert ctx.state["status"] == "verified"
        assert calls.count("ingest") == 1
        assert seen == [{"proof": "same"}, {"proof": "same"}]
        assert calls[-1] == "stop"
