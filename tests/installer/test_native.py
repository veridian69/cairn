import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from cairn_install import native, native_index
from cairn_install.core import Context, InstallError, open_context
from cairn_install.native import Backend
from cairn_install.native_index import (
    ProcessIdentity,
    process_identity,
    spawn_detached,
    stop_process,
)

LOCAL_FALKORDB_IMAGE = "cairn.local/falkordb-runtime@sha256:" + "b" * 64


def test_local_runtime_native_initialiser_and_server_never_pull(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    ctx.state["resources"]["native_index_port"] = 19123
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    index._prepare_volumes()  # noqa: SLF001
    index._ensure_container()  # noqa: SLF001
    launches = [
        argv
        for argv, _ in ctx.commands
        if argv[:2] in (["docker", "create"], ["docker", "run"])
    ]
    assert len(launches) == 2
    for argv in launches:
        assert LOCAL_FALKORDB_IMAGE in argv
        assert argv[argv.index("--pull") + 1] == "never"


def test_local_runtime_native_refuses_missing_exact_engine_digest(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    with pytest.raises(InstallError, match="local.*FalkorDB|FalkorDB.*local"):
        index.preflight()
    assert "native_index_image" not in ctx.state["resources"]


def test_local_runtime_native_refuses_changed_retained_image(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    ctx.state["resources"]["native_index_image"] = "other@sha256:" + "a" * 64
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    with pytest.raises(InstallError, match="FalkorDB.*changed"):
        index.preflight()


def test_local_runtime_native_resume_refuses_changed_image_without_preflight(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    ctx.state["resources"]["native_index_image"] = "other@sha256:" + "a" * 64
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    with pytest.raises(InstallError, match="FalkorDB.*changed"):
        index.validate_ownership()


def test_legacy_native_resume_retains_its_recorded_falkordb_image(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    legacy = "ghcr.io/example/legacy-falkordb@sha256:" + "a" * 64
    ctx.state["resources"]["native_index_image"] = legacy

    assert native._NativeIndex(cast(Context, ctx))._locked_image() == legacy  # noqa: SLF001


@pytest.mark.parametrize("operation", ["validate_ownership", "_ensure_container"])
def test_local_runtime_native_refuses_owned_container_with_different_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    ctx.state["resources"].update(
        native_index_port=19123,
        native_index_container={"name": index.container, "label": ctx.instance_id},
    )
    value = {
        "Config": {
            "Image": "other@sha256:" + "a" * 64,
            "Labels": {native._INDEX_LABEL: ctx.instance_id},
        },
        "HostConfig": {
            "PortBindings": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19123"}]}
        },
    }  # noqa: SLF001
    monkeypatch.setattr(index, "_inspect", lambda kind, name: value)
    with pytest.raises(InstallError, match="FalkorDB.*changed"):
        getattr(index, operation)()
    assert not any(argv[:2] == ["docker", "start"] for argv, _ in ctx.commands)


def test_local_runtime_native_preflight_records_exact_available_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    ctx.state["falkordb_runtime"] = {"image": LOCAL_FALKORDB_IMAGE}
    original = ctx.command

    def command(argv: list[str], **kwargs: Any) -> str:
        if argv == ["docker", "image", "inspect", LOCAL_FALKORDB_IMAGE]:
            ctx.commands.append((argv, kwargs))
            return json.dumps(
                [
                    {
                        "RepoDigests": [LOCAL_FALKORDB_IMAGE],
                        "Os": "linux",
                        "Architecture": "amd64",
                    }
                ]
            )
        return original(argv, **kwargs)

    monkeypatch.setattr(ctx, "command", command)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    index.preflight()
    assert ctx.state["resources"]["native_index_image"] == LOCAL_FALKORDB_IMAGE
    assert not any(argv[:2] == ["docker", "pull"] for argv, _ in ctx.commands)


class FakeContext:
    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "disposable",
        semantic: bool = False,
    ) -> None:
        self.directory = tmp_path / "journal"
        self.root = tmp_path / "root with % mark"
        self.source = tmp_path / "source"
        self.directory.mkdir()
        self.root.mkdir()
        self.source.mkdir()
        (self.source / "pyproject.toml").write_text("[project]\n")
        (self.source / "uv.lock").write_text("version = 1\n")
        (self.source / "deploy").mkdir()
        (self.source / "deploy" / "images.lock").write_text(
            "FALKORDB_IMAGE=falkordb/falkordb:v4.20.4@sha256:" + "a" * 64 + "\n"
        )
        self.state: dict[str, Any] = {
            "name": "demo",
            "mode": mode,
            "port": 18000,
            "semantic": semantic,
            "instance_id": "11111111-1111-4111-8111-111111111111",
            "run_id": "22222222-2222-4222-8222-222222222222",
            "steps": {},
            "owned_files": {},
            "resources": {},
            "receipts": {},
        }
        self.commands: list[tuple[list[str], dict[str, Any]]] = []
        self.notes: list[str] = []
        self.saved = 0

    @property
    def name(self) -> str:
        return str(self.state["name"])

    @property
    def mode(self) -> str:
        return str(self.state["mode"])

    @property
    def port(self) -> int:
        return int(self.state["port"])

    @property
    def semantic(self) -> bool:
        return bool(self.state["semantic"])

    @property
    def instance_id(self) -> str:
        return str(self.state["instance_id"])

    @property
    def run_id(self) -> str:
        return str(self.state["run_id"])

    def command(self, argv: list[str], **kwargs: Any) -> str:
        self.commands.append((list(argv), kwargs))
        if argv[:2] == ["uname", "-m"]:
            return "x86_64\n"
        if argv[:2] == ["uv", "--version"]:
            return "uv 0.12.14\n"
        if "is-system-running" in argv:
            return "running\n"
        if argv[:3] == ["docker", "version", "--format"]:
            return "25.0.0\n"
        if "FragmentPath" in argv:
            receipt = self.state["resources"].get("native_unit")
            return f"{receipt['path']}\n" if receipt is not None else ""
        if "DropInPaths" in argv:
            return ""
        if argv[:2] == ["docker", "inspect"] or argv[:3] == [
            "docker",
            "volume",
            "inspect",
        ]:
            return "[]\n"
        if "show" in argv and "ActiveState" in " ".join(argv):
            return "inactive\n"
        return ""

    def write_file(
        self,
        path: Path,
        content: str,
        *,
        mode: int = 0o600,
        secret: bool = False,
    ) -> None:
        del secret
        if path.exists() and str(path) not in self.state["owned_files"]:
            raise InstallError(f"refusing unowned file: {path}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        self.state["owned_files"][str(path)] = hashlib.sha256(
            content.encode()
        ).hexdigest()

    def check_file(self, path: Path) -> None:
        expected = self.state["owned_files"].get(str(path))
        observed = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if expected is None or observed != expected:
            raise InstallError(f"owned file changed: {path}")

    def note(self, message: str) -> None:
        self.notes.append(message)

    def save(self) -> None:
        self.saved += 1

    def read_secret(self, path: Path) -> str:
        return path.read_text()

    def add_secret(self, value: str) -> None:
        del value


def test_prepare_installs_locked_runtime_and_stable_configuration(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]

    backend.prepare()

    assert ctx.commands[0] == (
        [
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--no-editable",
            "--python",
            "3.14",
        ],
        {
            "cwd": ctx.source,
            "env": {"UV_PROJECT_ENVIRONMENT": str(ctx.root / "runtime")},
            "timeout": 900,
        },
    )
    assert (ctx.root / "config.yaml").read_text() == (
        "schema_version: cairn.config/v1\n"
        "instance_id: 11111111-1111-4111-8111-111111111111\n"
        "mode: production\n"
        "http:\n"
        "  host: 127.0.0.1\n"
        "  port: 18000\n"
        "paths:\n"
        f'  data: "{ctx.root / "data"}"\n'
        f'  credentials: "{ctx.root / "credentials"}"\n'
        "attic:\n"
        "  enabled: true\n"
        "graphiti:\n"
        "  enabled: false\n"
    )
    assert backend.lifecycle_argv("verify") == [
        str(ctx.root / "runtime" / "bin" / "cairn"),
        "verify",
        "--config",
        str(ctx.root / "config.yaml"),
    ]


def test_native_unit_is_unique_and_quotes_paths_and_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    backend.prepare()

    backend.start()

    unit = unit_dir / "cairn-install-demo.service"
    assert unit.is_file()
    assert (
        f'ExecStart="{str(ctx.root / "runtime" / "bin" / "cairn").replace("%", "%%")}" '
        f'serve --config "{str(ctx.root / "config.yaml").replace("%", "%%")}"\n'
    ) in unit.read_text()
    assert any(
        argv
        == [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "cairn-install-demo.service",
        ]
        for argv, _ in ctx.commands
    )
    assert ctx.state["resources"]["native_unit"]["service"] == (
        "cairn-install-demo.service"
    )


def test_existing_foreign_unit_is_refused_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    unit = unit_dir / "cairn-install-demo.service"
    unit.write_text("foreign\n")
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]

    with pytest.raises(InstallError, match="unowned"):
        backend.start()

    assert unit.read_text() == "foreign\n"
    assert not any("enable" in argv for argv, _ in ctx.commands)


def test_loaded_foreign_unit_or_dropin_is_refused_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    original = ctx.command

    def foreign_fragment(argv: list[str], **kwargs: Any) -> str:
        if "FragmentPath" in argv:
            ctx.commands.append((argv, kwargs))
            return "/usr/lib/systemd/user/cairn-install-demo.service\n"
        if "DropInPaths" in argv:
            ctx.commands.append((argv, kwargs))
            return "/etc/systemd/user/cairn-install-demo.service.d/foreign.conf\n"
        return original(argv, **kwargs)

    ctx.command = foreign_fragment  # type: ignore[method-assign]

    with pytest.raises(InstallError, match="foreign.*unit|unit.*foreign"):
        backend.start()

    assert not (unit_dir / "cairn-install-demo.service").exists()
    assert not any("enable" in argv for argv, _ in ctx.commands)


def test_preflight_records_safe_vendor_user_service_dropin_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    vendor_dir = tmp_path / "usr" / "lib" / "systemd" / "user" / "service.d"
    dropin = vendor_dir / "10-timeout-abort.conf"
    original = ctx.command

    def vendor_dropin(argv: list[str], **kwargs: Any) -> str:
        if "DropInPaths" in argv:
            ctx.commands.append((argv, kwargs))
            return ""
        return original(argv, **kwargs)

    ctx.command = vendor_dropin  # type: ignore[method-assign]
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(native, "_vendor_dropin_directory", lambda: vendor_dir)
    monkeypatch.setattr(
        native,
        "_vendor_dropin_inventory",
        lambda directory: [{"path": str(dropin), "sha256": "a" * 64}],
    )

    backend = Backend(ctx)  # type: ignore[arg-type]
    backend.preflight()

    assert ctx.state["resources"]["native_vendor_dropin_policy"] == {
        "schema": 1,
        "files": [{"path": str(dropin), "sha256": "a" * 64}],
    }
    assert any(str(dropin) in note and "a" * 64 in note for note in ctx.notes)

    def loaded_with_vendor_policy(argv: list[str], **kwargs: Any) -> str:
        if "FragmentPath" in argv:
            return str(native._unit_directory() / backend._service_name())  # noqa: SLF001
        if "DropInPaths" in argv:
            return f"{dropin}\n"
        return original(argv, **kwargs)

    ctx.command = loaded_with_vendor_policy  # type: ignore[method-assign]
    monkeypatch.setattr(native, "_vendor_dropin_digest", lambda path: "a" * 64)
    backend._validate_loaded_unit(require_expected=True)  # noqa: SLF001


def test_vendor_dropin_acceptance_is_reported_once_but_rechecked_by_each_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    vendor_dir = tmp_path / "usr" / "lib" / "systemd" / "user" / "service.d"
    dropin = vendor_dir / "10-timeout-abort.conf"
    checks = 0

    def inventory(_directory: Path) -> list[dict[str, str]]:
        nonlocal checks
        checks += 1
        return [{"path": str(dropin), "sha256": "a" * 64}]

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(native, "_vendor_dropin_directory", lambda: vendor_dir)
    monkeypatch.setattr(native, "_vendor_dropin_inventory", inventory)

    Backend(ctx).preflight()  # type: ignore[arg-type]
    Backend(ctx).preflight()  # type: ignore[arg-type]

    accepted = [
        note
        for note in ctx.notes
        if note.startswith("Accepted system vendor user-service drop-in")
    ]
    assert checks == 2
    assert accepted == [
        f"Accepted system vendor user-service drop-in {dropin} with SHA256 {'a' * 64}."
    ]


def test_vendor_dropin_policy_change_and_nonvendor_override_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    vendor_dir = tmp_path / "usr" / "lib" / "systemd" / "user" / "service.d"
    dropin = vendor_dir / "10-timeout-abort.conf"
    original = ctx.command

    def selected_dropin(argv: list[str], **kwargs: Any) -> str:
        if "DropInPaths" in argv:
            ctx.commands.append((argv, kwargs))
            return f"{dropin}\n"
        return original(argv, **kwargs)

    ctx.command = selected_dropin  # type: ignore[method-assign]
    monkeypatch.setattr(native, "_vendor_dropin_directory", lambda: vendor_dir)
    monkeypatch.setattr(native, "_vendor_dropin_digest", lambda path: "b" * 64)
    ctx.state["resources"]["native_vendor_dropin_policy"] = {
        "schema": 1,
        "files": [{"path": str(dropin), "sha256": "a" * 64}],
    }
    backend = Backend(ctx)  # type: ignore[arg-type]

    with pytest.raises(InstallError, match="policy changed"):
        backend._validate_loaded_unit(require_expected=False)  # noqa: SLF001

    override = tmp_path / "etc" / "systemd" / "user" / "service.d" / "override.conf"

    def override_dropin(argv: list[str], **kwargs: Any) -> str:
        if "DropInPaths" in argv:
            ctx.commands.append((argv, kwargs))
            return f"{override}\n"
        return original(argv, **kwargs)

    ctx.command = override_dropin  # type: ignore[method-assign]
    with pytest.raises(InstallError, match="foreign drop-in"):
        backend._validate_loaded_unit(require_expected=False)  # noqa: SLF001


def test_rollback_preserves_state_and_refuses_changed_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    backend.prepare()
    backend.start()
    unit = unit_dir / "cairn-install-demo.service"
    unit.write_text("changed by somebody else\n")

    with pytest.raises(InstallError, match="changed"):
        backend.rollback()

    assert unit.read_text() == "changed by somebody else\n"
    assert (ctx.root / "config.yaml").exists()
    assert (ctx.root / "data").is_dir()
    assert (ctx.root / "credentials").is_dir()
    assert not any("disable" in argv for argv, _ in ctx.commands)


def test_rollback_reconciles_unit_deleted_before_daemon_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    backend.prepare()
    backend.start()
    original = ctx.command
    failed = False

    def fail_first_reload(argv: list[str], **kwargs: Any) -> str:
        nonlocal failed
        if argv[-1:] == ["daemon-reload"] and not failed:
            failed = True
            raise InstallError("simulated daemon reload interruption")
        return original(argv, **kwargs)

    ctx.command = fail_first_reload  # type: ignore[method-assign]

    with pytest.raises(InstallError, match="simulated"):
        backend.rollback()
    assert not (unit_dir / "cairn-install-demo.service").exists()
    backend.rollback()
    assert ctx.state["resources"]["native_unit"]["status"] == "removed"


def test_materialised_unit_may_be_unloaded_before_replayed_daemon_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    ctx.write_file(backend.config_path, "owned\n")
    unit = unit_dir / "cairn-install-demo.service"
    ctx.write_file(unit, backend._unit_contents(), mode=0o644)  # noqa: SLF001
    ctx.state["resources"]["native_unit"] = {
        "path": str(unit),
        "service": unit.name,
        "status": "materialised",
    }
    original = ctx.command

    def unloaded(argv: list[str], **kwargs: Any) -> str:
        if "FragmentPath" in argv:
            ctx.commands.append((argv, kwargs))
            return ""
        return original(argv, **kwargs)

    ctx.command = unloaded  # type: ignore[method-assign]

    backend.validate_ownership()


def test_rollback_uses_journal_cwd_when_source_has_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(ctx)  # type: ignore[arg-type]
    backend.prepare()
    backend.start()
    (ctx.source / "pyproject.toml").unlink()
    (ctx.source / "uv.lock").unlink()
    (ctx.source / "deploy" / "images.lock").unlink()
    (ctx.source / "deploy").rmdir()
    ctx.source.rmdir()

    backend.rollback()

    assert not (unit_dir / "cairn-install-demo.service").exists()
    assert (ctx.root / "config.yaml").exists()
    assert (ctx.root / "data").is_dir()
    assert (ctx.root / "credentials").is_dir()
    rollback_commands = [
        kwargs
        for argv, kwargs in ctx.commands
        if argv[:2] in (["systemctl", "--user"], ["systemd-analyze", "--user"])
    ]
    assert rollback_commands
    assert all(kwargs.get("cwd") == ctx.directory for kwargs in rollback_commands)


def test_validation_distinguishes_new_foreign_and_missing_owned_config(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "mode": "disposable",
            "port": 18000,
            "semantic": False,
            "source": str(source),
            "source_fingerprint": "test",
        },
    ) as ctx:
        backend = Backend(ctx)
        backend.validate_ownership()
        backend.config_path.write_text("foreign\n")
        with pytest.raises(InstallError, match="unowned"):
            backend.validate_ownership()
        backend.config_path.unlink()
        ctx.write_file(backend.config_path, "owned\n")
        backend.config_path.unlink()
        with pytest.raises(InstallError, match="disappeared"):
            backend.validate_ownership()


def test_semantic_preflight_rejects_unsupported_docker_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    backend = Backend(ctx)  # type: ignore[arg-type]
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    original = ctx.command

    def old_docker(argv: list[str], **kwargs: Any) -> str:
        if argv[:3] == ["docker", "version", "--format"]:
            ctx.commands.append((argv, kwargs))
            return "24.0.9\n"
        return original(argv, **kwargs)

    ctx.command = old_docker  # type: ignore[method-assign]

    with pytest.raises(InstallError, match="Docker Engine 25"):
        backend.preflight()


def test_fresh_semantic_ownership_validation_does_not_allocate_port(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    backend = Backend(ctx)  # type: ignore[arg-type]

    backend.validate_ownership()

    assert "native_index_port" not in ctx.state["resources"]


@pytest.mark.parametrize(
    "system,mode",
    [
        ("Windows", "disposable"),
        ("FreeBSD", "disposable"),
        ("Windows", "native"),
        ("FreeBSD", "native"),
    ],
)
def test_preflight_rejects_non_linux_before_host_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, mode: str
) -> None:
    ctx = FakeContext(tmp_path, mode=mode)
    backend = Backend(ctx)  # type: ignore[arg-type]
    monkeypatch.setattr(platform, "system", lambda: system)

    def unexpected_host_check() -> int:
        pytest.fail("Unsupported OS reached native host checks")

    monkeypatch.setattr(os, "geteuid", unexpected_host_check)

    with pytest.raises(
        InstallError, match="Native installation requires Linux or macOS"
    ):
        backend.preflight()

    assert ctx.commands == []
    assert ctx.state["resources"] == {}
    assert ctx.saved == 0


@pytest.mark.parametrize("architecture", ["arm64", "x86_64"])
def test_darwin_disposable_selects_owned_foreground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    ctx = FakeContext(tmp_path)
    original = ctx.command

    def command(argv: list[str], **kwargs: Any) -> str:
        if argv[:2] == ["uname", "-m"]:
            return architecture
        return original(argv, **kwargs)

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ctx, "command", command)
    backend = Backend(cast(Context, ctx))
    monkeypatch.setattr(backend, "_require_available_port", lambda: None)
    backend.preflight()
    assert backend.foreground
    assert not backend.is_running()
    assert ctx.state["resources"] == {}


def test_darwin_semantic_refuses_before_host_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    backend = Backend(cast(Context, ctx))
    with pytest.raises(InstallError, match="Attic only"):
        backend.preflight()
    assert ctx.commands == []


def test_foreground_requires_disposable_and_linux_close_is_noop(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, mode="native")
    with pytest.raises(InstallError, match="requires disposable"):
        Backend(cast(Context, ctx), foreground=True)
    backend = Backend(cast(Context, ctx))
    backend.close()
    assert ctx.commands == []
    assert ctx.saved == 0


def test_preflight_accepts_uv_platform_build_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    original = ctx.command

    def platform_uv(argv: list[str], **kwargs: Any) -> str:
        if argv[:2] == ["uv", "--version"]:
            ctx.commands.append((argv, kwargs))
            return "uv 0.12.14 (x86_64-unknown-linux-gnu)\n"
        return original(argv, **kwargs)

    ctx.command = platform_uv  # type: ignore[method-assign]

    backend.preflight()


def test_semantic_prepare_uses_protected_credentials_and_pinned_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    provider_path = ctx.root / "credentials" / "openai-api-key"
    ctx.write_file(provider_path, "sk-example-provider-secret\n", secret=True)
    ctx.state["provider_key_file"] = str(provider_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    assert backend._index is not None  # noqa: SLF001
    monkeypatch.setattr(  # noqa: SLF001
        backend._index, "_allocate_loopback_port", lambda: 19123
    )

    backend.prepare()

    config = (ctx.root / "config.yaml").read_text()
    assert "graphiti:\n  enabled: true\n  host: 127.0.0.1\n  port: 19123\n" in config
    assert ctx.state["resources"]["native_index_port"] == 19123
    assert (
        ctx.root / "credentials" / "falkordb-password"
    ).stat().st_mode & 0o777 == 0o600
    assert (ctx.root / "credentials" / "falkordb.conf").stat().st_mode & 0o777 == 0o600
    serialised_state = json.dumps(ctx.state)
    assert "sk-example-provider-secret" not in serialised_state
    password = (ctx.root / "credentials" / "falkordb-password").read_text().strip()
    assert password not in serialised_state
    assert all(password not in item for argv, _ in ctx.commands for item in argv)
    initialise = next(
        argv for argv, _ in ctx.commands if argv[:2] == ["docker", "create"]
    )
    assert initialise[-1] == (
        "chown 0:0 /config/cairn.conf.new && "
        "chmod 0400 /config/cairn.conf.new && "
        "chown 10001:0 /config/cairn.conf.new /var/lib/falkordb/data && "
        "mv /config/cairn.conf.new /config/cairn.conf"
    )
    run = next(argv for argv, _ in ctx.commands if argv[:2] == ["docker", "run"])
    assert "--read-only" in run
    assert ["--publish", "127.0.0.1:19123:6379"] == run[
        run.index("--publish") : run.index("--publish") + 2
    ]
    assert run[-1] == "falkordb/falkordb:v4.20.4@sha256:" + "a" * 64


def test_semantic_port_is_allocated_once_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    allocations: list[int] = []

    def allocate() -> int:
        allocations.append(19124)
        return 19124

    monkeypatch.setattr(index, "_allocate_loopback_port", allocate)

    index.ensure_port()
    index.ensure_port()

    assert allocations == [19124]
    assert ctx.state["resources"]["native_index_port"] == 19124


def test_semantic_resume_refuses_busy_foreign_recorded_port(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    ctx.state["resources"]["native_index_port"] = listener.getsockname()[1]
    try:
        with pytest.raises(InstallError, match="[Ss]emantic.*port|port.*semantic"):
            index.ensure_port()
    finally:
        listener.close()


def test_semantic_resume_accepts_busy_port_only_for_owned_container_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    ctx.state["resources"]["native_index_port"] = 19125
    ctx.state["resources"]["native_index_container"] = {
        "name": index.container,
        "label": ctx.instance_id,
        "status": "running",
    }
    mapping = {"HostIp": "127.0.0.1", "HostPort": "19125"}
    inspected = {
        "Config": {"Labels": {native._INDEX_LABEL: ctx.instance_id}},
        "HostConfig": {
            "PortBindings": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19125"}]}
        },
        "State": {"Running": True},
        "NetworkSettings": {"Ports": {"6379/tcp": [mapping]}},
    }
    monkeypatch.setattr(index, "_inspect", lambda kind, name: inspected)
    monkeypatch.setattr(
        index,
        "_require_loopback_port_available",
        lambda port: pytest.fail("owned running mapping must prove the busy port"),
    )

    index.ensure_port()

    mapping["HostPort"] = "19126"
    with pytest.raises(InstallError, match="mapping"):
        index.ensure_port()


def test_semantic_stopped_owned_container_uses_persistent_port_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    ctx.state["resources"]["native_index_port"] = 19127
    ctx.state["resources"]["native_index_container"] = {
        "name": index.container,
        "label": ctx.instance_id,
        "status": "stopped",
    }
    inspected = {
        "Config": {"Labels": {native._INDEX_LABEL: ctx.instance_id}},
        "HostConfig": {
            "PortBindings": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19127"}]}
        },
        "State": {"Running": False},
        "NetworkSettings": {"Ports": {}},
    }
    monkeypatch.setattr(index, "_inspect", lambda kind, name: inspected)

    index._ensure_container()  # noqa: SLF001

    assert ["docker", "start", index.container] in [argv for argv, _ in ctx.commands]


def test_semantic_container_reads_labels_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    receipt = {
        "name": index.container,
        "label": ctx.instance_id,
        "status": "running",
    }
    ctx.state["resources"]["native_index_container"] = receipt
    ctx.state["resources"]["native_index_port"] = 19123
    inspected = {
        "Config": {"Labels": {native._INDEX_LABEL: ctx.instance_id}},
        "HostConfig": {
            "PortBindings": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19123"}]}
        },
        "State": {"Running": False},
        "NetworkSettings": {"Ports": {}},
    }
    monkeypatch.setattr(index, "_inspect", lambda kind, name: inspected)

    index.validate_ownership()


def test_missing_recorded_semantic_volume_is_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    ctx.state["resources"]["native_index_data_volume"] = {
        "name": index.data_volume,
        "label": ctx.instance_id,
    }
    monkeypatch.setattr(index, "_inspect", lambda kind, name: None)

    with pytest.raises(InstallError, match="disappeared|missing"):
        index._ensure_volume(index.data_volume, "data")  # noqa: SLF001

    assert not any(
        argv[:3] == ["docker", "volume", "create"] for argv, _ in ctx.commands
    )


def test_semantic_init_reconciles_owned_survivor_before_recreating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    init_name = f"{index.container}-init-{ctx.run_id[:8]}"
    intent = {"name": init_name, "label": ctx.instance_id}
    ctx.state["resources"]["native_index_volume_init_intent"] = intent
    inspected = {"Config": {"Labels": {native._INDEX_LABEL: ctx.instance_id}}}
    inspections = iter([inspected, None])
    monkeypatch.setattr(index, "_inspect", lambda kind, name: next(inspections))

    index._prepare_volumes()  # noqa: SLF001

    commands = [argv for argv, _ in ctx.commands]
    remove_position = commands.index(["docker", "rm", "--force", init_name])
    create_position = next(
        i for i, argv in enumerate(commands) if argv[:2] == ["docker", "create"]
    )
    assert remove_position < create_position


def test_semantic_rollback_reconciles_container_and_init_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path, semantic=True)
    index = native._NativeIndex(cast(Context, ctx))  # noqa: SLF001
    init_name = f"{index.container}-init-{ctx.run_id[:8]}"
    ctx.state["resources"]["native_index_container_intent"] = {
        "name": index.container,
        "label": ctx.instance_id,
    }
    ctx.state["resources"]["native_index_volume_init_intent"] = {
        "name": init_name,
        "label": ctx.instance_id,
    }
    inspected = {"Config": {"Labels": {native._INDEX_LABEL: ctx.instance_id}}}
    monkeypatch.setattr(index, "_inspect", lambda kind, name: inspected)

    index.rollback()

    commands = [argv for argv, _ in ctx.commands]
    assert ["docker", "stop", "--time", "30", index.container] in commands
    assert ["docker", "rm", "--force", init_name] in commands
    assert ctx.state["resources"]["native_index_container"]["status"] == "stopped"


def test_preflight_refuses_busy_loopback_port_unless_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    ctx.state["port"] = listener.getsockname()[1]
    listener.listen()
    try:
        with pytest.raises(InstallError, match="port"):
            backend.preflight()
        monkeypatch.setattr(backend, "is_running", lambda: True)
        backend.preflight()
    finally:
        listener.close()


def test_disposable_process_receipt_detects_pid_reuse_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    receipt = ProcessIdentity(
        pid=1234,
        start_time=10,
        uid=os.getuid(),
        executable="/usr/bin/python3",
        argv=("python3", "-c", "pass"),
    )
    ctx.state["resources"]["native_process"] = receipt.to_json()
    replacement = ProcessIdentity(
        pid=1234,
        start_time=11,
        uid=os.getuid(),
        executable="/usr/bin/python3",
        argv=("python3", "-c", "pass"),
    )
    monkeypatch.setattr(native, "process_identity", lambda pid: replacement)
    signalled: list[int] = []
    monkeypatch.setattr(
        native, "stop_process", lambda owned: signalled.append(owned.pid)
    )

    assert backend.is_running() is False
    with pytest.raises(InstallError, match="identity"):
        backend.stop()
    assert signalled == []
    assert ctx.state["resources"]["native_process_status"] == "identity_mismatch"


def test_disposable_resume_reconciles_receipt_written_before_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    expected_argv = [
        str(ctx.root / "runtime" / "bin" / "python"),
        str(ctx.root / "runtime" / "bin" / "cairn"),
        "serve",
        "--config",
        str(ctx.root / "config.yaml"),
    ]
    receipt_path = ctx.root / "process-1.json"
    receipt_path.write_text("{}\n")
    receipt_path.chmod(0o600)
    identity = ProcessIdentity(
        pid=4321,
        start_time=20,
        uid=os.getuid(),
        executable=str(ctx.root / "runtime" / "bin" / "python"),
        argv=tuple(expected_argv),
    )
    ctx.state["resources"]["native_process_intent"] = {
        "generation": 1,
        "receipt_path": str(receipt_path),
        "argv": expected_argv,
    }
    monkeypatch.setattr(native, "load_process_receipt", lambda path: identity)
    monkeypatch.setattr(native, "process_identity", lambda pid: identity)

    backend.start()

    assert ctx.commands == []
    assert ctx.state["resources"]["native_process"] == identity.to_json()
    assert ctx.state["resources"]["native_process_generation"] == 1
    assert ctx.state["resources"]["native_process_status"] == "running"


def test_is_running_and_stop_reconcile_pending_process_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeContext(tmp_path)
    backend = Backend(ctx)  # type: ignore[arg-type]
    argv = [
        str(backend.runtime_python),
        str(ctx.root / "runtime" / "bin" / "cairn"),
        "serve",
        "--config",
        str(ctx.root / "config.yaml"),
    ]
    receipt_path = ctx.root / "process-1.json"
    receipt_path.write_text("{}\n")
    receipt_path.chmod(0o600)
    identity = ProcessIdentity(
        4321,
        20,
        os.getuid(),
        "/usr/bin/python3",
        tuple(argv),
    )
    ctx.state["resources"]["native_process_intent"] = {
        "generation": 1,
        "receipt_path": str(receipt_path),
        "argv": argv,
    }
    alive = True
    monkeypatch.setattr(native, "load_process_receipt", lambda path: identity)
    monkeypatch.setattr(
        native, "process_identity", lambda pid: identity if alive else None
    )

    def stopped(receipt: ProcessIdentity) -> bool:
        nonlocal alive
        assert receipt == identity
        alive = False
        return True

    monkeypatch.setattr(native, "stop_process", stopped)

    assert backend.is_running() is True
    del ctx.state["resources"]["native_process"]
    backend.stop()
    assert alive is False
    assert ctx.state["resources"]["native_process_status"] == "stopped"


def test_helper_turns_termination_into_controlled_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> ProcessIdentity:
        del args, kwargs
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("signal handler did not interrupt the helper")

    monkeypatch.setattr(native_index, "spawn_detached", interrupted)

    result = native_index.main(
        [
            "spawn",
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--log",
            str(tmp_path / "service.log"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )

    assert result == 130


def test_detached_spawn_defers_signal_until_child_handle_is_assigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Process:
        pid = 4321

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            return 0

        def kill(self) -> None:
            events.append("kill")

    process = Process()

    @contextmanager
    def interrupted_spawn() -> Iterator[list[int]]:
        events.append("defer")
        yield [signal.SIGTERM]

    monkeypatch.setattr(
        native_index, "defer_spawn_signals", interrupted_spawn, raising=False
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    identity = ProcessIdentity(
        process.pid,
        20,
        os.getuid(),
        os.path.realpath(sys.executable),
        (sys.executable, "-c", "pass"),
    )
    monkeypatch.setattr(native_index, "process_identity", lambda pid: identity)

    with pytest.raises(KeyboardInterrupt):
        spawn_detached(
            tmp_path / "receipt.json",
            tmp_path / "service.log",
            identity.argv,
            cwd=tmp_path,
        )

    assert events == ["defer", "defer", "kill", "wait:1.0"]


def test_detached_helper_records_real_kernel_identity_and_stops_only_that_process(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "process.json"
    log_path = tmp_path / "service.log"
    identity = spawn_detached(
        receipt_path,
        log_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
    )
    try:
        assert (
            ProcessIdentity.from_json(json.loads(receipt_path.read_text())) == identity
        )
        assert process_identity(identity.pid) == identity
        assert identity.pid in native_index._spawned_processes
    finally:
        stop_process(identity, timeout=2.0)
    assert process_identity(identity.pid) is None
    assert identity.pid not in native_index._spawned_processes


def test_detached_helper_retries_only_transient_empty_startup_cmdline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspect = native_index.process_identity
    calls = 0

    def transient_identity(pid: int) -> ProcessIdentity | None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise InstallError(f"Native process {pid} has no inspectable command line")
        return inspect(pid)

    monkeypatch.setattr(native_index, "process_identity", transient_identity)
    identity = spawn_detached(
        tmp_path / "process.json",
        tmp_path / "service.log",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
    )
    try:
        assert calls >= 3
        assert (
            ProcessIdentity.from_json(
                json.loads((tmp_path / "process.json").read_text())
            )
            == identity
        )
    finally:
        stop_process(identity, timeout=2.0)
