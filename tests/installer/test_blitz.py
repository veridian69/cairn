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

    def close(self) -> None:
        pass

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

    def interrupted(path: Path, **_: Any) -> None:
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

    def interrupted(_: Path, **__: Any) -> None:
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


def test_blitz_removes_read_only_go_toolchain_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Go extracts a downloaded toolchain with 0555 directories (live WSL run)."""
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    with instance(tmp_path / "state") as ctx:
        toolchain = ctx.root / "garden" / "go-mod" / "toolchain@go1.25.0" / "bin"
        toolchain.mkdir(parents=True)
        (toolchain / "go").write_text("binary")
        (toolchain / "go").chmod(0o555)
        for directory in (toolchain, toolchain.parent):
            directory.chmod(0o555)
        workflow.blitz_install(ctx)
        assert not ctx.directory.exists()
        assert not (ctx.directory.parent / ".demo.blitz.json").exists()


def test_blitz_permission_repair_never_follows_symlinks_out_of_the_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    outside = tmp_path / "outside" / "bin"
    outside.mkdir(parents=True)
    (outside / "go").write_text("external")
    outside.chmod(0o555)
    with instance(tmp_path / "state") as ctx:
        cache = ctx.root / "garden" / "go-mod"
        cache.mkdir(parents=True)
        (cache / "toolchain").symlink_to(outside.parent)
        workflow.blitz_install(ctx)
        assert not ctx.directory.exists()
    assert (outside / "go").read_text() == "external"
    assert outside.stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)


def test_blitz_permission_repair_skips_a_directory_swapped_for_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window between listing a directory and opening it."""
    import os

    calls: list[str] = []
    monkeypatch.setattr(workflow, "backend", lambda _: Backend(calls))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "go").write_text("external")
    outside.chmod(0o555)
    real_open = os.open
    with instance(tmp_path / "state") as ctx:
        swapped = ctx.root / "garden" / "go-mod" / "bin"
        swapped.mkdir(parents=True)
        swapped.chmod(0o555)

        def swapping_open(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if name == "bin" and kwargs.get("dir_fd") is not None:
                swapped.chmod(0o755)
                swapped.rmdir()
                swapped.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swapping_open)
        workflow.blitz_install(ctx)
        monkeypatch.setattr(os, "open", real_open)
        assert not ctx.directory.exists()
    assert (outside / "go").read_text() == "external"
    assert outside.stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)


def test_blitz_permission_repair_holds_only_ancestor_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Go's module cache has 256 shard directories; never open siblings at once."""
    import os

    from cairn_install.destruction import _restore_owner_write

    tree = tmp_path / "tree"
    for index in range(300):
        shard = tree / f"{index:03x}" / "inner"
        shard.mkdir(parents=True)
        shard.chmod(0o555)
    real_open, real_close = os.open, os.close
    held: set[int] = set()
    peak = 0

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        nonlocal peak
        fd = real_open(*args, **kwargs)
        held.add(fd)
        peak = max(peak, len(held))
        return fd

    def tracking_close(fd: int) -> None:
        held.discard(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    _restore_owner_write(tree)
    monkeypatch.setattr(os, "open", real_open)
    monkeypatch.setattr(os, "close", real_close)
    assert peak <= 3
    assert not held
    assert all(
        (tree / f"{index:03x}" / "inner").stat().st_mode & 0o700 == 0o700
        for index in range(300)
    )
    shutil.rmtree(tree)
