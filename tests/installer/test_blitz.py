"""Destructive cleanup must stay within a locked, owned installation."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from cairn_install import workflow
from cairn_install.core import InstallError, open_context


def instance(root: Path) -> Any:
    return open_context(
        root,
        "demo",
        create={
            "source": str(root.parent / "source"),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    )


class Backend:
    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def validate_ownership(self) -> None:
        self.calls.append("validate")

    def blitz(self) -> None:
        self.calls.append("blitz")
        if self.fail:
            raise InstallError("resource still in use")


def test_blitz_removes_instance_and_preserves_sibling_and_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    with instance(root) as ctx:
        (ctx.root / "data").mkdir()
        (ctx.root / "data" / "catalogue").write_text("owned data")
        (ctx.root / "external-link").symlink_to(outside)
        sibling = root / "other"
        sibling.mkdir()
        result = workflow.blitz_install(ctx)
        assert result["status"] == "deleted"
        assert not ctx.directory.exists()
        assert sibling.is_dir()
        assert outside.read_text() == "keep me"
    assert calls == ["blitz"]
    with instance(root) as fresh:
        assert fresh.state["status"] == "planned"


def test_blitz_backend_failure_keeps_recovery_and_blocks_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls, fail=True))
    with instance(tmp_path / "state") as ctx:
        with pytest.raises(InstallError, match="still in use"):
            workflow.blitz_install(ctx)
        assert (ctx.directory / "state.json").exists()
        assert ctx.state["status"] == "blitzing"
        with pytest.raises(InstallError, match="blitz"):
            workflow.run_install(ctx)
        monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
        workflow.blitz_install(ctx)
        assert not ctx.directory.exists()


def test_blitz_partial_filesystem_failure_retries_without_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install import destruction

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    original = destruction.remove_instance_files

    def fail(_: Any) -> None:
        raise InstallError("filesystem busy")

    monkeypatch.setattr(destruction, "remove_instance_files", fail)
    with instance(tmp_path / "state") as ctx:
        with pytest.raises(InstallError, match="filesystem busy"):
            workflow.blitz_install(ctx)
        assert ctx.state["blitz_phase"] == "resources_removed"
    monkeypatch.setattr(destruction, "remove_instance_files", original)
    with open_context(tmp_path / "state", "demo") as ctx:
        workflow.blitz_install(ctx)
    assert calls == ["blitz"]


def test_deleted_state_and_lock_recover_from_external_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    original = shutil.rmtree

    def interrupted(path: Path) -> None:
        original(path)
        raise OSError("simulated interruption after tree deletion")

    monkeypatch.setattr(shutil, "rmtree", interrupted)
    # Preserve the safety capability flag on the replacement callable.
    interrupted.avoids_symlink_attacks = True  # type: ignore[attr-defined]
    root = tmp_path / "state"
    with instance(root) as ctx:
        with pytest.raises(InstallError, match="run blitz again"):
            workflow.blitz_install(ctx)
        assert not ctx.directory.exists()
        assert (root / ".demo.blitz.json").exists()
    with pytest.raises(InstallError, match="unfinished blitz"):
        instance(root)
    monkeypatch.setattr(shutil, "rmtree", original)
    with open_context(root, "demo") as recovered:
        workflow.blitz_install(recovered)
    assert not (root / "demo").exists()
    assert not (root / ".demo.blitz.json").exists()
    assert calls == ["blitz"]


def test_mount_boundary_refused_before_resource_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install import destruction

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    with instance(tmp_path / "state") as ctx:
        mounted = ctx.root / "mounted"
        mounted.mkdir()
        monkeypatch.setattr(destruction, "_mount_points", lambda: [mounted])
        with pytest.raises(InstallError, match="mounted directory"):
            workflow.blitz_install(ctx)
        assert calls == []
        assert (ctx.directory / "state.json").exists()


def test_root_guard_blocks_context_recreation(tmp_path: Path) -> None:
    from cairn_install.core import state_root_guard

    root = tmp_path / "state"
    with state_root_guard(root, exclusive=True):
        with pytest.raises(InstallError, match="cleanup is busy"):
            instance(root)
    with instance(root) as ctx:
        assert ctx.name == "demo"


def test_mount_check_normalises_parent_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install import destruction

    (tmp_path / "detour").mkdir()
    with instance(tmp_path / "detour" / ".." / "state") as ctx:
        mounted = ctx.root / "mounted"
        mounted.mkdir()
        monkeypatch.setattr(destruction, "_mount_points", lambda: [mounted.resolve()])
        with pytest.raises(InstallError, match="mounted directory"):
            destruction.check_instance_tree(ctx)


def test_recovery_refuses_replaced_instance_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    original = shutil.rmtree

    def interrupted(_: Path) -> None:
        raise OSError("busy")

    interrupted.avoids_symlink_attacks = True  # type: ignore[attr-defined]
    monkeypatch.setattr(shutil, "rmtree", interrupted)
    root = tmp_path / "state"
    with instance(root) as ctx:
        with pytest.raises(InstallError, match="run blitz again"):
            workflow.blitz_install(ctx)
        state_file = ctx.directory / "state.json"
        state = json.loads(state_file.read_text())
        state["instance_id"] = "different-instance"
        state_file.write_text(json.dumps(state))
    monkeypatch.setattr(shutil, "rmtree", original)
    with pytest.raises(InstallError, match="disagree"):
        open_context(root, "demo")
    assert state_file.exists()
    assert (root / ".demo.blitz.json").exists()
