from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cairn_install import workflow
from cairn_install.core import InstallError, open_context


@pytest.mark.parametrize(
    "mode,keep_running",
    [("native", False), ("docker", False), ("disposable", False), ("disposable", True)],
)
@pytest.mark.parametrize("failure", [False, True])
def test_verified_resume_checks_live_persistent_service_without_new_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure: bool,
    keep_running: bool,
) -> None:
    calls: list[str] = []
    receipt = {
        "outcome": "committed",
        "mutation_receipt": {},
        "audit_receipt": {},
        "result": {
            "evidence_id": str(uuid4()),
            "assertion_id": str(uuid4()),
            "fact_ids": [str(uuid4())],
        },
    }

    class Backend:
        runtime_python = "python"
        running = False

        def validate_ownership(self) -> None:
            calls.append("ownership")

        def close(self) -> None:
            pass

        def wait_foreground(self) -> None:
            calls.append("hold")
            raise KeyboardInterrupt

        def preflight(self) -> None:
            calls.append("preflight")

        def start(self) -> None:
            calls.append("start")
            self.running = True

    backend = Backend()
    monkeypatch.setattr(workflow, "backend", lambda ctx, **kwargs: backend)

    def ready(ctx: Any) -> str:
        assert backend.running
        calls.append("identity")
        if failure:
            raise InstallError("wrong identity")
        return "token"

    def reads(ctx: Any, python: str, saved: dict[str, Any]) -> None:
        assert saved == receipt
        calls.append("reads")

    monkeypatch.setattr(workflow, "ready", ready)
    monkeypatch.setattr(workflow, "verify_reads", reads)
    monkeypatch.setattr(workflow, "ingest", lambda *args: pytest.fail("no new writes"))
    monkeypatch.setattr(
        workflow, "bootstrap", lambda *args: pytest.fail("no bootstrap")
    )
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
        ctx.state["status"] = "verified"
        ctx.state["receipts"]["ingest"] = receipt
        ctx.save()
        if failure and (mode != "disposable" or keep_running):
            with pytest.raises(InstallError, match="wrong identity"):
                workflow.run_install(ctx, keep_running=keep_running)
            assert ctx.state["status"] == "failed"
        else:
            assert (
                workflow.run_install(ctx, keep_running=keep_running)["status"]
                == "verified"
            )
        if mode == "disposable" and not keep_running:
            assert calls == ["ownership"]
        else:
            assert "start" in calls
            assert "identity" in calls
            assert ("reads" in calls) != failure
        assert ctx.state["receipts"]["ingest"] == receipt

    if failure and (mode != "disposable" or keep_running):
        failure = False
        backend.running = False
        with open_context(tmp_path / "state", "demo") as resumed:
            assert (
                workflow.run_install(resumed, keep_running=keep_running)["status"]
                == "verified"
            )
            assert resumed.state["receipts"]["ingest"] == receipt
            assert "reads" in calls
