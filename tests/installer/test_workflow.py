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

    def close(self) -> None:
        pass

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


@pytest.mark.parametrize("hold_error", [KeyboardInterrupt, InstallError])
def test_foreground_hold_preserves_verified_on_interrupt_and_always_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold_error: type[BaseException]
) -> None:
    calls: list[str] = []
    adapter = Adapter(calls)
    monkeypatch.setattr(workflow, "backend", lambda ctx, **kwargs: adapter)
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)
    monkeypatch.setattr(workflow, "ready", lambda *args: "token")
    monkeypatch.setattr(workflow, "verify_reads", lambda *args: None)

    def ingest(ctx: Any, token: str) -> dict[str, str]:
        ctx.state["receipts"]["ingest"] = {"proof": "same"}
        return {"proof": "same"}

    monkeypatch.setattr(workflow, "ingest", ingest)
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

        def hold() -> None:
            assert ctx.state["status"] == "verified"
            assert "stop" not in calls
            raise hold_error("hold ended")

        monkeypatch.setattr(adapter, "wait_foreground", hold, raising=False)
        monkeypatch.setattr(
            adapter, "close", lambda: calls.append("close"), raising=False
        )
        if hold_error is InstallError:
            with pytest.raises(InstallError, match="hold ended"):
                workflow.run_install(ctx, keep_running=True)
            assert ctx.state["status"] == "failed"
            assert ctx.state["verified_recheck"] is True
            monkeypatch.setattr(workflow, "validate_ingest", lambda receipt: None)
            monkeypatch.setattr(
                workflow,
                "bootstrap",
                lambda *args: pytest.fail("no repeated bootstrap"),
            )
            monkeypatch.setattr(
                workflow, "ingest", lambda *args: pytest.fail("no repeated ingest")
            )

            def interrupt() -> None:
                raise KeyboardInterrupt

            monkeypatch.setattr(adapter, "wait_foreground", interrupt)
            assert workflow.run_install(ctx, keep_running=True)["status"] == "verified"
        else:
            assert workflow.run_install(ctx, keep_running=True)["status"] == "verified"
        assert calls[-1] == "close"


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_keep_running_rejects_persistent_modes_before_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(workflow, "backend", lambda *a, **kw: pytest.fail("no backend"))
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": mode,
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        with pytest.raises(InstallError, match="disposable"):
            workflow.run_install(ctx, keep_running=True)


def test_failed_foreground_cleanup_is_recorded_before_context_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = Adapter([])
    monkeypatch.setattr(workflow, "backend", lambda ctx: adapter)

    def close() -> None:
        raise InstallError("owned child did not stop", "cleanup_failed")

    monkeypatch.setattr(adapter, "close", close)
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
        ctx.state["status"] = "verified"
        with pytest.raises(InstallError, match="owned child did not stop"):
            workflow.run_install(ctx)
        assert ctx.state["status"] == "failed"
        assert "owned child did not stop" in ctx.state["last_error"]
        assert ctx.state["verified_recheck"] is True
