from pathlib import Path
from typing import Any

import pytest

from cairn_install import workflow
from cairn_install.core import InstallError, open_context


def test_shared_spine_stages_have_operator_facing_titles() -> None:
    expected = {
        "garden_stage": "Build and check the new Garden release",
        "garden_copy": "Stop Garden and take a cold copy of its data",
        "garden_swap": "Switch Garden to the selected release, keeping the other",
        "garden_restore_data": "Restore Garden data from the cold copy",
        "garden_snapshot": "Stop Garden and take a validated snapshot",
        "garden_archive": "Write and verify the Garden backup archive",
        "server_account": "Create Garden service account",
        "server_install": "Install Garden server files",
        "server_hook": "Install Garden backup hook",
        "garden_enable": "Enable Garden services",
    }
    assert {key: dict(workflow.STAGES)[key] for key in expected} == expected


@pytest.mark.parametrize("pending", ["upgrade", "backup"])
@pytest.mark.parametrize("operation", ["install", "add", "remove", "rollback"])
def test_pending_garden_operations_refuse_workflow_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending: str,
    operation: str,
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "docker",
            "port": 8000,
            "semantic": False,
            "garden": {pending: {"phase": "recorded"}},
        },
    ) as ctx:
        saved = (ctx.directory / "state.json").read_bytes()
        monkeypatch.setattr(
            workflow,
            "backend",
            lambda *a, **kw: pytest.fail("backend must not be opened"),
        )
        with pytest.raises(
            InstallError, match=f"run garden-{pending} --name demo again"
        ):
            if operation == "install":
                workflow.run_install(ctx)
            elif operation == "add":
                workflow.add_garden(ctx, {})
            elif operation == "remove":
                workflow.remove_garden(ctx, delete_data=False)
            else:
                workflow.rollback_install(ctx)
        assert (ctx.directory / "state.json").read_bytes() == saved


@pytest.mark.parametrize("phase", ["recorded", "complete"])
@pytest.mark.parametrize("operation", ["upgrade", "backup"])
def test_garden_pending_only_refuses_incomplete_operations(
    tmp_path: Path, phase: str, operation: str
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "docker",
            "port": 8000,
            "semantic": False,
            "garden": {operation: {"phase": phase}},
        },
    ) as ctx:
        pending = workflow._garden_pending(ctx)
        assert pending == (
            f"Garden {operation} is in progress; run garden-{operation} --name demo again"
            if phase != "complete"
            else None
        )


def test_every_managed_garden_stage_has_an_operator_facing_title() -> None:
    titles = dict(workflow.STAGES)

    assert titles["garden_files"] == "Prepare Garden files and participant credentials"
    assert titles["garden_prepare"] == "Prepare Garden runtime and configuration"
    assert titles["garden_start"] == "Start Garden"
    assert titles["garden_verify"] == "Verify Garden endpoint and participant bindings"
    assert titles["garden_rollback"] == "Stop Garden and preserve its data"
    assert titles["stop"] == "Stop the disposable Cairn process and retain its data"
    assert titles["rollback"] == "Stop Cairn and preserve its data"


def test_unknown_stage_cannot_leak_its_internal_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        monkeypatch.setattr(ctx, "note", lambda message: None)
        with pytest.raises(KeyError, match="internal_typo"):
            workflow.stage(ctx, "internal_typo", lambda: None)


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


class KubernetesAdapter(Adapter):
    def open_endpoint(self) -> None:
        self.calls.append("open_endpoint")

    def close_endpoint(self) -> None:
        self.calls.append("close_endpoint")


class PartiallyOpenedKubernetesAdapter(KubernetesAdapter):
    def open_endpoint(self) -> None:
        self.calls.append("partially_opened_endpoint")
        raise InstallError("endpoint readiness failed")


class ManagedGardenAdapter(KubernetesAdapter):
    def garden_prepare(self) -> None:
        self.calls.append("garden_prepare")

    def garden_start(self) -> None:
        self.calls.append("garden_start")

    def garden_stop(self) -> None:
        self.calls.append("garden_stop")

    def garden_open_endpoint(self) -> None:
        self.calls.append("garden_open")

    def garden_close_endpoint(self) -> None:
        self.calls.append("garden_close")


@pytest.mark.parametrize("fail_garden", [False, True])
def test_managed_garden_enrols_before_attach_and_rechecks_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_garden: bool,
) -> None:
    from cairn_install import garden

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda ctx: ManagedGardenAdapter(calls))
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: calls.append("bootstrap"))
    monkeypatch.setattr(workflow, "ready", lambda *args: "admin-token")
    monkeypatch.setattr(
        workflow, "verify_reads", lambda *args: calls.append("cairn_verify")
    )

    def ingest(ctx: Any, token: str) -> dict[str, str]:
        ctx.state["receipts"]["ingest"] = {"proof": "same"}
        return {"proof": "same"}

    def verify(ctx: Any, **_: Any) -> None:
        calls.append("garden_verify")
        if fail_garden:
            raise InstallError("Garden TLS certificate mismatch")

    monkeypatch.setattr(workflow, "ingest", ingest)
    monkeypatch.setattr(garden, "prepare", lambda ctx: calls.append("garden_files"))
    monkeypatch.setattr(garden, "enrol", lambda ctx, token: calls.append("enrol"))
    monkeypatch.setattr(garden, "verify", verify)
    monkeypatch.setattr(garden, "profiles", lambda ctx: calls.append("profiles"))
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "kubernetes",
            "port": 19000,
            "semantic": False,
            "kubernetes": {"context": "test", "namespace": "demo"},
            "garden": {"options": {"endpoint": "https://garden.example:8443/mcp"}},
        },
    ) as ctx:
        if fail_garden:
            with pytest.raises(InstallError, match="certificate mismatch"):
                workflow.run_install(ctx)
            assert ctx.state["status"] == "failed"
            assert calls[-1] == "garden_close"
            assert "profiles" not in calls
            assert "restart" not in calls
        else:
            workflow.run_install(ctx)
            assert ctx.state["status"] == "verified"
            assert calls.count("garden_verify") == 2
            assert (
                calls.index("garden_verify") < calls.index("restart") < len(calls) - 1
            )
            assert calls[-1] == "profiles"
        assert (
            calls.index("enrol")
            < calls.index("close_endpoint")
            < calls.index("garden_prepare")
        )
        assert calls.count("enrol") == 1


def test_kubernetes_status_prints_a_context_bound_port_forward_command(
    tmp_path: Path,
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "kubernetes",
            "port": 19000,
            "semantic": False,
            "kubernetes": {
                "context": "reference",
                "namespace": "demo",
                "storage_class": "cairn-rwop",
                "image": "registry.example/cairn@sha256:" + "a" * 64,
                "image_policy": "Always",
                "preloaded_image": False,
            },
        },
    ) as ctx:
        result = workflow.status_install(ctx)

    assert result["endpoint"] == (
        "kubectl --context reference --namespace demo port-forward service/cairn 19000:8000"
    )


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


def test_failed_rollback_records_failure_instead_of_retaining_verified_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    adapter = Adapter(calls)

    def fail() -> None:
        raise InstallError("rollback failed provider-secret-value")

    adapter.rollback = fail  # type: ignore[method-assign]
    monkeypatch.setattr(workflow, "backend", lambda ctx: adapter)
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        ctx.state["status"] = "verified"
        ctx.add_secret("provider-secret-value")
        ctx.save()

        with pytest.raises(InstallError, match="rollback failed"):
            workflow.rollback_install(ctx)

        assert ctx.state["status"] == "failed"
        assert ctx.state["steps"]["rollback"] == "running"
        assert ctx.state["last_error"] == "rollback failed [redacted]"


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


def test_kubernetes_closes_endpoint_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda ctx: KubernetesAdapter(calls))
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)
    monkeypatch.setattr(workflow, "ready", lambda *args: "token")

    def ingest(ctx: Any, token: str) -> dict[str, str]:
        ctx.state["receipts"]["ingest"] = {"proof": "same"}
        return {"proof": "same"}

    monkeypatch.setattr(workflow, "ingest", ingest)
    monkeypatch.setattr(workflow, "verify_reads", lambda *args: None)
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "kubernetes",
            "port": 19000,
            "semantic": False,
            "kubernetes": {"context": "reference", "namespace": "demo"},
        },
    ) as ctx:
        workflow.run_install(ctx)

    assert calls.count("open_endpoint") == 2
    assert calls.count("close_endpoint") == 2
    assert calls == [
        "ownership",
        "preflight",
        "prepare",
        "ownership",
        "start",
        "open_endpoint",
        "close_endpoint",
        "restart",
        "open_endpoint",
        "close_endpoint",
    ]


def test_kubernetes_closes_endpoint_when_verification_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda ctx: KubernetesAdapter(calls))
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)
    monkeypatch.setattr(workflow, "ready", lambda *args: "token")
    monkeypatch.setattr(workflow, "ingest", lambda *args: {"proof": "same"})
    monkeypatch.setattr(
        workflow,
        "verify_reads",
        lambda *args: (_ for _ in ()).throw(InstallError("verification failed")),
    )
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "kubernetes",
            "port": 19000,
            "semantic": False,
            "kubernetes": {"context": "reference", "namespace": "demo"},
        },
    ) as ctx:
        with pytest.raises(InstallError, match="verification failed"):
            workflow.run_install(ctx)

    assert calls.count("open_endpoint") == 1
    assert calls.count("close_endpoint") == 1
    assert calls.index("open_endpoint") < calls.index("close_endpoint")


def test_kubernetes_closes_a_partially_opened_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        workflow, "backend", lambda ctx: PartiallyOpenedKubernetesAdapter(calls)
    )
    monkeypatch.setattr(workflow, "bootstrap", lambda *args: None)
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "kubernetes",
            "port": 19000,
            "semantic": False,
            "kubernetes": {"context": "reference", "namespace": "demo"},
        },
    ) as ctx:
        with pytest.raises(InstallError, match="endpoint readiness failed"):
            workflow.run_install(ctx)

    assert calls[-2:] == ["partially_opened_endpoint", "close_endpoint"]


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


def test_status_and_summary_report_managed_garden_as_a_feature(
    tmp_path: Path,
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 19000,
            "semantic": False,
            "garden": {"options": {"endpoint": "https://garden.example.test/mcp"}},
        },
    ) as ctx:
        result = workflow.status_install(ctx)

    assert result["features"] == "Attic only; Garden"


@pytest.mark.parametrize(
    ("mode", "expected"), [("kubernetes", "127.0.0.1"), ("docker", None)]
)
def test_garden_verification_dials_loopback_only_for_kubernetes_forward(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str | None
) -> None:
    from types import SimpleNamespace

    from cairn_install import garden

    calls: list[str | None] = []
    monkeypatch.setattr(
        garden,
        "verify",
        lambda ctx, connect_host=None, connect_port=None: calls.append(connect_host),
    )
    monkeypatch.setattr(garden, "profiles", lambda ctx: None)
    adapter = SimpleNamespace(
        garden_open_endpoint=lambda: None, garden_close_endpoint=lambda: None
    )
    workflow._verify_garden(SimpleNamespace(mode=mode), adapter)  # type: ignore[arg-type]
    assert calls == [expected]
