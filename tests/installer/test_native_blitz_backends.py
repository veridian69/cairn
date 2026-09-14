from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from cairn_install import native
from cairn_install.core import Context, InstallError
from cairn_install.native import Backend
from cairn_install.native_index import ProcessIdentity

INSTANCE = "11111111-1111-4111-8111-111111111111"


class NativeContext:
    def __init__(self, tmp_path: Path, *, semantic: bool = True) -> None:
        self.directory = tmp_path / "journal"
        self.root = tmp_path / "root"
        self.source = tmp_path / "source"
        self.directory.mkdir()
        self.root.mkdir()
        self.source.mkdir()
        self.state: dict[str, Any] = {
            "name": "demo",
            "mode": "native",
            "semantic": semantic,
            "instance_id": INSTANCE,
            "run_id": "22222222-2222-4222-8222-222222222222",
            "resources": {},
            "owned_files": {},
            "file_intents": {},
        }
        self.commands: list[list[str]] = []
        self.active_state = "active"
        self.stop_succeeds = True
        self.unit_loaded = True
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.fail_volume_once: str | None = None

    @property
    def name(self) -> str:
        return str(self.state["name"])

    @property
    def mode(self) -> str:
        return str(self.state["mode"])

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
        del kwargs
        command = list(argv)
        self.commands.append(command)
        if "FragmentPath" in command:
            if not self.unit_loaded:
                return ""
            receipt = self.state["resources"].get("native_unit")
            return "" if receipt is None else f"{receipt['path']}\n"
        if "DropInPaths" in command:
            return ""
        if "ActiveState" in command:
            load_state = "loaded" if self.unit_loaded else "not-found"
            return f"LoadState={load_state}\nActiveState={self.active_state}\n"
        if command[:4] == ["systemctl", "--user", "disable", "--now"]:
            if self.stop_succeeds:
                self.active_state = "inactive"
            return ""
        if command[:3] == ["systemctl", "--user", "daemon-reload"]:
            self.unit_loaded = False
            return ""
        if command[:2] == ["docker", "inspect"]:
            value = self.containers.get(command[-1])
            return "[]" if value is None else json.dumps([value])
        if command[:3] == ["docker", "container", "ls"]:
            name = command[-1].removeprefix("name=^/").removesuffix("$")
            return name if name in self.containers else ""
        if command[:3] == ["docker", "volume", "ls"]:
            name = command[-1].removeprefix("name=^").removesuffix("$")
            return name if name in self.volumes else ""
        if command[:3] == ["docker", "volume", "inspect"]:
            value = self.volumes.get(command[-1])
            return "[]" if value is None else json.dumps([value])
        if command[:3] == ["docker", "run", "--detach"]:
            name = command[command.index("--name") + 1]
            self.containers[name] = _owned()
            return name
        if command[:4] == ["docker", "rm", "--force", command[-1]]:
            self.containers.pop(command[-1], None)
            return command[-1]
        if command[:3] == ["docker", "volume", "rm"]:
            if self.fail_volume_once == command[-1]:
                self.fail_volume_once = None
                raise InstallError("injected volume removal failure")
            self.volumes.pop(command[-1], None)
            return command[-1]
        return ""

    def write_file(self, path: Path, content: str, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        self.state["owned_files"][str(path)] = hashlib.sha256(
            content.encode()
        ).hexdigest()

    def check_file(self, path: Path) -> None:
        expected = self.state["owned_files"].get(str(path))
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if expected != actual:
            raise InstallError(f"owned file changed: {path}")

    def save(self) -> None:
        pass

    def note(self, message: str) -> None:
        del message


def _owned() -> dict[str, Any]:
    return {"Config": {"Labels": {native._INDEX_LABEL: INSTANCE}}}


def _record_index(ctx: NativeContext, backend: Backend) -> None:
    assert backend._index is not None  # noqa: SLF001
    index = backend._index  # noqa: SLF001
    ctx.state["resources"].update(
        {
            "native_index_container": {
                "name": index.container,
                "label": INSTANCE,
                "status": "running",
            },
            "native_index_data_volume": {"name": index.data_volume, "label": INSTANCE},
            "native_index_config_volume": {
                "name": index.config_volume,
                "label": INSTANCE,
            },
        }
    )
    ctx.containers[index.container] = _owned()
    ctx.volumes[index.data_volume] = {"Labels": {native._INDEX_LABEL: INSTANCE}}
    ctx.volumes[index.config_volume] = {"Labels": {native._INDEX_LABEL: INSTANCE}}


def test_native_blitz_proves_service_stopped_before_removing_owned_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path)
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(cast(Context, ctx))
    unit = unit_dir / backend._service_name()  # noqa: SLF001
    ctx.write_file(unit, backend._unit_contents(), mode=0o644)  # noqa: SLF001
    ctx.state["resources"]["native_unit"] = {
        "path": str(unit),
        "service": unit.name,
        "status": "enabled",
    }
    _record_index(ctx, backend)

    backend.blitz()
    backend.blitz()

    active_check = next(
        i for i, command in enumerate(ctx.commands) if "ActiveState" in command
    )
    first_volume_rm = next(
        i
        for i, command in enumerate(ctx.commands)
        if command[:3] == ["docker", "volume", "rm"]
    )
    assert active_check < first_volume_rm
    assert not unit.exists()
    assert ctx.state["resources"]["native_blitz"] == "complete"
    assert not any(command[:3] == ["docker", "image", "rm"] for command in ctx.commands)


def test_native_blitz_refuses_data_deletion_when_service_did_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path)
    ctx.stop_succeeds = False
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(cast(Context, ctx))
    unit = unit_dir / backend._service_name()  # noqa: SLF001
    ctx.write_file(unit, backend._unit_contents(), mode=0o644)  # noqa: SLF001
    ctx.state["resources"]["native_unit"] = {
        "path": str(unit),
        "service": unit.name,
        "status": "enabled",
    }
    _record_index(ctx, backend)

    with pytest.raises(InstallError, match="did not stop"):
        backend.blitz()

    assert unit.exists()
    assert ctx.volumes
    assert not any(
        command[:3] == ["docker", "volume", "rm"] for command in ctx.commands
    )


def test_native_blitz_reconciles_early_index_intents_without_configuration(
    tmp_path: Path,
) -> None:
    ctx = NativeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    assert backend._index is not None  # noqa: SLF001
    index = backend._index  # noqa: SLF001
    for kind, name in (("data", index.data_volume), ("config", index.config_volume)):
        ctx.state["resources"][f"native_index_{kind}_volume_intent"] = {
            "name": name,
            "label": INSTANCE,
        }
        ctx.volumes[name] = {"Labels": {native._INDEX_LABEL: INSTANCE}}
    ctx.state["resources"]["native_index_container_intent"] = {
        "name": index.container,
        "label": INSTANCE,
    }
    ctx.containers[index.container] = _owned()

    backend.blitz()

    assert not ctx.containers
    assert not ctx.volumes
    assert any(command[:3] == ["docker", "container", "ls"] for command in ctx.commands)
    assert any(command[:3] == ["docker", "volume", "ls"] for command in ctx.commands)


def test_disposable_blitz_stops_the_exact_process_before_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path, semantic=False)
    ctx.state["mode"] = "disposable"
    backend = Backend(cast(Context, ctx))
    identity = ProcessIdentity(
        pid=4321,
        start_time=20,
        uid=1000,
        executable="/usr/bin/python3",
        argv=("python3", "serve"),
    )
    ctx.state["resources"]["native_process"] = identity.to_json()
    alive = True

    monkeypatch.setattr(
        native, "process_identity", lambda pid: identity if alive else None
    )

    def stopped(receipt: ProcessIdentity) -> bool:
        nonlocal alive
        assert receipt == identity
        alive = False
        return True

    monkeypatch.setattr(native, "stop_process", stopped)

    backend.blitz()

    assert alive is False
    assert ctx.state["resources"]["native_blitz"] == "complete"


def test_native_blitz_rejects_active_service_from_a_removed_unit_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path)
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(cast(Context, ctx))
    unit = unit_dir / backend._service_name()  # noqa: SLF001
    ctx.state["resources"]["native_unit"] = {
        "path": str(unit),
        "service": unit.name,
        "status": "removed",
    }
    _record_index(ctx, backend)

    with pytest.raises(InstallError, match="did not stop"):
        backend.blitz()

    assert ctx.volumes
    assert not any(
        command[:3] == ["docker", "volume", "rm"] for command in ctx.commands
    )


def test_native_blitz_reconciles_missing_unlinked_unit_without_disabling_name_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path, semantic=False)
    ctx.active_state = "inactive"
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: unit_dir)
    backend = Backend(cast(Context, ctx))
    unit = unit_dir / backend._service_name()  # noqa: SLF001
    ctx.state["resources"].update(
        {
            "native_unit": {
                "path": str(unit),
                "service": unit.name,
                "status": "enabled",
            },
            "native_unit_delete_intent": {
                "path": str(unit),
                "service": unit.name,
                "phase": "unlink_pending",
            },
        }
    )

    backend.blitz()

    assert not any("disable" in command for command in ctx.commands)
    assert ["systemctl", "--user", "daemon-reload"] in ctx.commands
    assert ctx.state["resources"]["native_unit"]["status"] == "removed"


def test_native_blitz_retries_after_one_of_two_volumes_was_already_removed(
    tmp_path: Path,
) -> None:
    ctx = NativeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    _record_index(ctx, backend)
    assert backend._index is not None  # noqa: SLF001
    ctx.fail_volume_once = backend._index.config_volume  # noqa: SLF001

    with pytest.raises(InstallError, match="injected volume removal failure"):
        backend.blitz()

    assert backend._index.data_volume not in ctx.volumes  # noqa: SLF001
    assert backend._index.config_volume in ctx.volumes  # noqa: SLF001

    backend.blitz()

    assert not ctx.volumes
    assert ctx.state["resources"]["native_blitz"] == "complete"


def test_native_blitz_accepts_container_intent_shape_emitted_by_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = NativeContext(tmp_path)
    backend = Backend(cast(Context, ctx))
    assert backend._index is not None  # noqa: SLF001
    index = backend._index  # noqa: SLF001
    ctx.state["resources"]["native_index_port"] = 19123
    monkeypatch.setattr(index, "_locked_image", lambda: "locked-image")

    index._ensure_container()  # noqa: SLF001
    ctx.state = json.loads(json.dumps(ctx.state))

    emitted = {
        "name": index.container,
        "label": INSTANCE,
        "status": "running",
    }
    assert ctx.state["resources"]["native_index_container_intent"] == emitted
    assert ctx.state["resources"]["native_index_container"] == emitted

    backend.blitz()

    assert index.container not in ctx.containers
    assert ctx.state["resources"]["native_blitz"] == "complete"
