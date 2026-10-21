import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import cli, garden, garden_native, garden_upgrade, native
from cairn_install.core import Context, InstallError, open_context, secure_directory
from cairn_install.garden_native import GardenBackend


@pytest.mark.parametrize("mode", ["native", "docker", "kubernetes"])
def test_macos_refuses_new_garden_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Darwin"))
    state = tmp_path / "state"
    assert (
        cli.main(
            [
                "install",
                "--mode",
                mode,
                "--garden-config",
                str(tmp_path / "garden.json"),
                "--name",
                "mac",
                "--port",
                "18231",
                "--state-root",
                str(state),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert (
        "Garden is not supported on macOS; install Garden on Linux."
        in capsys.readouterr().err
    )
    assert not state.exists()


@pytest.mark.parametrize("mode", ["native", "docker", "kubernetes"])
def test_macos_refuses_retained_garden_before_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    source = tmp_path / "source"
    (source / "src/cairn").mkdir(parents=True)
    (source / "src/cairn/__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    (source / "uv.lock").write_text("version = 1\n")
    state_root = tmp_path / "state"
    with open_context(
        state_root,
        "mac",
        create={
            "mode": mode,
            "port": 18231,
            "semantic": False,
            "source": str(source),
            "source_fingerprint": cli.source_fingerprint(source),
            "garden": {"options": {"port": 8443}},
        },
    ):
        pass
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(
        "cairn_install.workflow.run_install",
        lambda ctx: pytest.fail("workflow must not run"),
    )
    assert (
        cli.main(
            [
                "resume",
                "--name",
                "mac",
                "--state-root",
                str(state_root),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert (
        "Garden is not supported on macOS; install Garden on Linux."
        in capsys.readouterr().err
    )


def context(tmp_path: Path) -> Context:
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
            "garden": {"options": {"port": 8443}},
        },
    )


def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    units = tmp_path / "units"
    monkeypatch.setattr(native, "_unit_directory", lambda: units)
    commands: list[list[str]] = []

    def command(self: Context, argv: list[str], **kwargs: Any) -> str:
        commands.append(argv)
        if "FragmentPath" in argv:
            path = units / argv[-1]
            return str(path) if path.exists() else ""
        if "ActiveState" in argv:
            return "inactive"
        return ""

    monkeypatch.setattr(Context, "command", command)
    return commands


def test_garden_unit_uses_separate_receipts_and_preserves_data_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        original = {"service": "cairn-install-demo.service", "status": "enabled"}
        ctx.state["resources"]["native_unit"] = original.copy()
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        data = ctx.root / "garden" / "data" / "retained"
        ctx.write_file(data, "message history\n")
        backend.garden_start()
        unit = tmp_path / "units" / "cairn-install-demo-garden.service"
        assert "PartOf=cairn-install-demo.service" in unit.read_text()
        assert " host --config " in unit.read_text()
        backend.rollback()
        output = capsys.readouterr().out
        assert (
            f"Rollback retained Garden configuration and data under {ctx.root / 'garden'}."
            in output
        )
        assert "retained configuration, data and credentials" not in output
        assert not unit.exists()
        assert data.read_text() == "message history\n"
        assert ctx.state["resources"]["native_unit"] == original
        assert (
            ctx.state["resources"]["garden_native"]["native_unit"]["status"]
            == "removed"
        )
        assert not any(argv[-1] == "cairn-install-demo.service" for argv in commands)


def test_changed_garden_unit_blocks_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        backend.garden_start()
        unit = tmp_path / "units" / "cairn-install-demo-garden.service"
        unit.write_text("foreign replacement\n")
        with pytest.raises(InstallError, match="changed"):
            backend.blitz()
        assert unit.exists()


def test_changed_garden_executable_blocks_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        backend.binary.write_text("replacement\n")
        with pytest.raises(InstallError, match="executable.*changed"):
            backend.garden_start()
        assert commands == []


def test_foreign_cairn_unit_blocks_blitz_before_garden_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn_install.workflow import blitz_install

    commands = manager(monkeypatch, tmp_path)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        ctx.write_file(backend.binary, "#!/bin/sh\n", mode=0o755)
        ctx.write_file(backend.config_path, "{}\n")
        backend.garden_start()
        cairn_unit = tmp_path / "units" / "cairn-install-demo.service"
        ctx.write_file(cairn_unit, "owned Cairn unit\n", mode=0o644)
        ctx.state["resources"]["native_unit"] = {
            "path": str(cairn_unit),
            "service": cairn_unit.name,
            "status": "enabled",
        }
        cairn_unit.write_text("foreign replacement\n")
        commands.clear()
        with pytest.raises(InstallError, match="changed"):
            blitz_install(ctx)
        assert (tmp_path / "units" / "cairn-install-demo-garden.service").exists()
        assert not any("stop" in argv or "disable" in argv for argv in commands)


def test_native_build_stamps_the_release_version_and_keeps_go_caches_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cairn"\nversion = "9.9.9"\n'
    )
    builds: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

    def command(self: Context, argv: list[str], **kwargs: Any) -> str:
        if argv[:2] == ["go", "build"]:
            builds.append((argv, kwargs.get("cwd"), kwargs.get("env")))
            Path(argv[argv.index("-o") + 1]).write_text("#!/bin/sh\n")
        return ""

    monkeypatch.setattr(Context, "command", command)
    with context(tmp_path) as ctx:
        backend = GardenBackend(ctx)
        backend.binary.parent.mkdir(parents=True, mode=0o700)
        backend._prepare_binary()  # noqa: SLF001
        garden_root = ctx.root / "garden"
        assert backend.binary.stat().st_mode & 0o777 == 0o755
        assert str(backend.binary) in ctx.state["owned_files"]

    [(argv, cwd, env)] = builds
    assert cwd == tmp_path / "garden"
    assert "-trimpath" in argv and "-mod=readonly" in argv
    assert argv[argv.index("-ldflags") + 1] == (
        "-s -w -X github.com/veridian69/cairn/garden/cmd.version=9.9.9"
    )
    assert env == {
        "CGO_ENABLED": "0",
        "GOCACHE": str(garden_root / "go-build"),
        "GOMODCACHE": str(garden_root / "go-mod"),
        "GOFLAGS": "-modcacherw",
        "XDG_CONFIG_HOME": str(garden_root / "go-config"),
    }


class UserManager:
    """Fake systemd --user and Go: records every argv and models unit state."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.units = tmp_path / "units"
        self.commands: list[list[str]] = []
        self.active: set[str] = set()
        self.system_state = "running"
        self.go_works = True
        self.fail_next_reload = False
        # Bytes appended to the built executable, to exceed the default read cap.
        self.binary_padding = 0
        # A unit loaded from elsewhere under Garden's name, if set.
        self.foreign_fragment: str | None = None
        # Both modules import the unit directory helper by name.
        monkeypatch.setattr(native, "_unit_directory", lambda: self.units)
        monkeypatch.setattr(garden_native, "_unit_directory", lambda: self.units)
        monkeypatch.setattr(
            Context, "command", lambda ctx, argv, **kwargs: self.handle(list(argv))
        )

    def handle(self, argv: list[str]) -> str:
        self.commands.append(argv)
        if argv[:2] == ["go", "build"]:
            output = Path(argv[argv.index("-o") + 1])
            output.write_bytes(b"#!/bin/sh\n" + bytes(self.binary_padding))
            return ""
        if argv[:2] == ["go", "version"]:
            if not self.go_works:
                raise InstallError("go: command not found")
            return "go version go1.26.0 linux/amd64\n"
        if argv[:3] == ["systemctl", "--user", "is-system-running"]:
            return f"{self.system_state}\n"
        if argv[:2] != ["systemctl", "--user"]:
            return ""
        verb, service = argv[2], argv[-1]
        if verb == "show":
            return self._show(argv)
        if verb == "daemon-reload":
            if self.fail_next_reload:
                self.fail_next_reload = False
                raise InstallError("interrupted during daemon-reload")
            return ""
        if verb in {"enable", "start", "restart"}:
            self.active.add(service)
        elif verb in {"disable", "stop"}:
            self.active.discard(service)
        return ""

    def _show(self, argv: list[str]) -> str:
        properties = [
            argv[i + 1] for i, item in enumerate(argv) if item == "--property"
        ]
        unit = self.units / argv[-1]
        values = {
            "FragmentPath": self.foreign_fragment
            or (str(unit) if unit.exists() else ""),
            "DropInPaths": "",
            "LoadState": "loaded" if unit.exists() else "not-found",
            "ActiveState": "active" if argv[-1] in self.active else "inactive",
        }
        if "--value" in argv:
            return values[properties[0]] + "\n"
        return "".join(f"{key}={values[key]}\n" for key in properties)

    def verbs(self) -> list[str]:
        return [
            argv[2] for argv in self.commands if argv[:2] == ["systemctl", "--user"]
        ]


def garden_options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "port": 8443,
        "endpoint": "https://garden.example.test:8443/mcp",
        "scope": {
            "realm": "local",
            "segments": [{"kind": "job", "identifier": "garden"}],
        },
        "classification": "internal",
        "participants": {},
    }
    options.update(extra)
    return options


def full_context(tmp_path: Path, options: dict[str, Any]) -> Context:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cairn"\nversion = "9.9.9"\n'
    )
    (tmp_path / "garden").mkdir()
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
            "garden": {"options": options},
        },
    )


def host_document(ctx: Context) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((ctx.root / "garden" / "host.json").read_text())
    return value


@pytest.mark.parametrize(
    ("extra", "listen"),
    [
        ({}, "0.0.0.0:8443"),
        ({"listen_address": "127.0.0.1"}, "127.0.0.1:8443"),
        ({"listen_address": "::1"}, "[::1]:8443"),
    ],
)
def test_listen_renders_ipv4_ipv6_and_wildcard_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
    listen: str,
) -> None:
    UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options(**extra)) as ctx:
        GardenBackend(ctx).garden_prepare()
        assert host_document(ctx)["gateway"]["listen"] == listen


def test_legacy_options_render_the_same_host_json_as_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Options without listen_address or stream keep main's exact bytes."""
    UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        GardenBackend(ctx).garden_prepare()
        root = ctx.root / "garden"
        expected = {
            "gateway": {
                "auth": {
                    "classification": "internal",
                    "endpoint": "http://127.0.0.1:8000/memory/v1/diagnose",
                    "instance_id": ctx.instance_id,
                    "scope": garden_options()["scope"],
                },
                "daemon_url_file": str(root / "run" / "daemon.url"),
                "data_dir": str(root / "data"),
                "listen": "0.0.0.0:8443",
                "principals": {},
                "public_endpoint": "https://garden.example.test:8443/mcp",
                "tls_cert_file": str(root / "tls" / "server.crt"),
                "tls_key_file": str(root / "tls" / "server.key"),
            }
        }
        rendered = (root / "host.json").read_text()
        assert rendered == json.dumps(expected, indent=2, sort_keys=True) + "\n"


def test_stream_retention_is_top_level_only_when_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retention = {"max_age": "168h", "max_bytes": 1073741824}
    UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options(stream=retention)) as ctx:
        GardenBackend(ctx).garden_prepare()
        document = host_document(ctx)
        assert document["stream"] == retention
        assert "stream" not in document["gateway"]
    other = tmp_path / "legacy"
    other.mkdir()
    UserManager(other, monkeypatch)
    with full_context(other, garden_options()) as ctx:
        GardenBackend(ctx).garden_prepare()
        assert "stream" not in host_document(ctx)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, {}), ({"listen_address": "127.0.0.1"}, {"address": "127.0.0.1"})],
)
def test_preflight_probes_the_configured_listen_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
    expected: dict[str, str],
) -> None:
    UserManager(tmp_path, monkeypatch)
    probes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        garden,
        "require_listener_available",
        lambda *args, **kwargs: probes.append((args, kwargs)),
    )
    with full_context(tmp_path, garden_options(**extra)) as ctx:
        GardenBackend(ctx).preflight()
    assert probes == [((8443,), {"wildcard": True, **expected})]


def test_attach_is_check_only_and_passes_on_ready_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        ctx.save()
        backend = GardenBackend(ctx)
        before_state = copy.deepcopy(ctx.state)
        before_bytes = (ctx.directory / "state.json").read_bytes()

        def tree() -> list[str]:
            # commands.log receives the operator note by design; nothing else may change.
            return sorted(
                str(path) for path in tmp_path.rglob("*") if path.name != "commands.log"
            )

        before_tree = tree()
        backend.garden_attach()
        assert ctx.state == before_state
        assert (ctx.directory / "state.json").read_bytes() == before_bytes
        assert tree() == before_tree
        assert manager.commands
        assert all(
            argv[:2] == ["go", "version"]
            or argv[:3] == ["systemctl", "--user", "is-system-running"]
            or argv[:3] == ["systemctl", "--user", "show"]
            for argv in manager.commands
        )


def test_attach_refuses_foreign_loaded_unit_without_unit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    manager.foreign_fragment = str(tmp_path / "elsewhere" / GARDEN_UNIT)
    with full_context(tmp_path, garden_options()) as ctx:
        ctx.save()
        assert not (manager.units / GARDEN_UNIT).exists()
        backend = GardenBackend(ctx)
        before_state = copy.deepcopy(ctx.state)
        with pytest.raises(InstallError, match="foreign loaded native unit"):
            backend.garden_attach()
        assert ctx.state == before_state
        assert not any(argv[:2] == ["go", "build"] for argv in manager.commands)


@pytest.mark.parametrize(
    "problem",
    ["source", "go", "systemd", "unowned_unit", "listener"],
)
def test_attach_fails_before_any_change_when_prerequisites_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    occupied = socket.socket()
    try:
        occupied.bind(("0.0.0.0", 0))
        port = occupied.getsockname()[1]
        options = garden_options(port=8443 if problem != "listener" else port)
        with full_context(tmp_path, options) as ctx:
            if problem == "source":
                (tmp_path / "garden").rmdir()
            elif problem == "go":
                manager.go_works = False
            elif problem == "systemd":
                manager.system_state = "offline"
            elif problem == "unowned_unit":
                manager.units.mkdir(parents=True, exist_ok=True)
                (manager.units / "cairn-install-demo-garden.service").write_text("x")
            backend = GardenBackend(ctx)
            before_state = copy.deepcopy(ctx.state)
            manager.commands.clear()
            with pytest.raises(InstallError):
                backend.garden_attach()
            assert ctx.state == before_state
            assert not (ctx.root / "garden" / "bin").exists()
            assert not any(argv[:2] == ["go", "build"] for argv in manager.commands)
    finally:
        occupied.close()


@pytest.mark.parametrize("target", ["unit", "binary"])
@pytest.mark.parametrize(
    ("problem", "match"),
    [("unowned", "unowned"), ("modified", "changed"), ("symlink", "symlink")],
)
def test_attach_refuses_unowned_modified_or_symlinked_garden_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    problem: str,
    match: str,
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        install_garden(ctx, manager)
        if target == "unit":
            path = manager.units / GARDEN_UNIT
        else:
            path = ctx.root / "garden" / "bin" / "garden"
        key = str(path)
        if problem == "unowned":
            ctx.state["owned_files"].pop(key)
            ctx.state.get("file_intents", {}).pop(key, None)
            ctx.save()
        elif problem == "modified":
            path.write_bytes(b"replacement\n")
        else:
            decoy = path.with_name(path.name + ".decoy")
            decoy.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(decoy)
        before_state = copy.deepcopy(ctx.state)
        manager.commands.clear()
        with pytest.raises(InstallError, match=match):
            GardenBackend(ctx).garden_attach()
        assert ctx.state == before_state
        assert not any(argv[:2] == ["go", "build"] for argv in manager.commands)


def test_large_garden_binary_attaches_and_detaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executable is recorded under a 128 MiB digest limit, not the 2 MiB read cap."""
    manager = UserManager(tmp_path, monkeypatch)
    manager.binary_padding = 3 * 1024 * 1024
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        binary = ctx.root / "garden" / "bin" / "garden"
        assert binary.stat().st_size > 2 * 1024 * 1024
        backend.garden_attach()
        backend.garden_detach(delete_data=False)
        assert not binary.exists()
        assert str(binary) not in ctx.state["owned_files"]


def test_attach_probes_the_configured_listen_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    UserManager(tmp_path, monkeypatch)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.2", 0))
        port = occupied.getsockname()[1]
        with full_context(
            tmp_path, garden_options(port=port, listen_address="127.0.0.2")
        ) as ctx:
            with pytest.raises(InstallError, match="Garden listener port"):
                GardenBackend(ctx).garden_attach()
        other = tmp_path / "other"
        other.mkdir()
        with full_context(
            other, garden_options(port=port, listen_address="127.0.0.1")
        ) as ctx:
            GardenBackend(ctx).garden_attach()


GARDEN_UNIT = "cairn-install-demo-garden.service"


def install_garden(ctx: Context, manager: UserManager) -> GardenBackend:
    """Prepare and start Garden beside a running Cairn, plus Cairn's own files."""
    backend = GardenBackend(ctx)
    backend.garden_prepare()
    backend.garden_start()
    ctx.write_file(ctx.root / "garden" / "tls" / "server.key", "key\n", secret=True)
    ctx.write_file(
        ctx.root / "credentials" / "garden" / "val" / "token", "token\n", secret=True
    )
    cairn_unit = manager.units / "cairn-install-demo.service"
    ctx.write_file(cairn_unit, "owned Cairn unit\n", mode=0o644)
    ctx.write_file(ctx.root / "config.yaml", "cairn\n")
    ctx.state["resources"]["native_unit"] = {
        "path": str(cairn_unit),
        "service": cairn_unit.name,
        "status": "enabled",
    }
    manager.active.add(cairn_unit.name)
    (ctx.root / "garden" / "data" / "retained").write_text("messages\n")
    (ctx.root / "data").mkdir(exist_ok=True)
    (ctx.root / "data" / "cairn.db").write_text("cairn\n")
    ctx.save()
    manager.commands.clear()
    return backend


def garden_paths(ctx: Context, manager: UserManager) -> list[Path]:
    return [
        ctx.root / "garden" / "bin" / "garden",
        ctx.root / "garden" / "host.json",
        ctx.root / "garden" / "tls" / "server.key",
        ctx.root / "credentials" / "garden" / "val" / "token",
        manager.units / GARDEN_UNIT,
    ]


def test_detach_removes_owned_garden_files_and_keeps_data_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        backend.garden_detach(delete_data=False)
        for path in garden_paths(ctx, manager):
            assert not path.exists(), path
        assert (ctx.root / "garden" / "data" / "retained").read_text() == "messages\n"
        owned = ctx.state["owned_files"]
        assert not [key for key in owned if key.startswith(str(ctx.root / "garden"))]
        assert not [
            key
            for key in owned
            if key.startswith(str(ctx.root / "credentials" / "garden"))
        ]
        assert str(manager.units / GARDEN_UNIT) not in owned
        assert ctx.state["resources"]["garden_native"]["native_unit"]["status"] == (
            "removed"
        )
        # Cairn keeps its files, its unit and its running service.
        assert (ctx.root / "config.yaml").read_text() == "cairn\n"
        assert (ctx.root / "data" / "cairn.db").read_text() == "cairn\n"
        cairn_unit = manager.units / "cairn-install-demo.service"
        assert cairn_unit.read_text() == "owned Cairn unit\n"
        assert str(cairn_unit) in owned
        assert "cairn-install-demo.service" in manager.active
        assert not any(
            argv[:2] == ["systemctl", "--user"]
            and argv[2] in {"stop", "disable", "enable"}
            and argv[-1] == "cairn-install-demo.service"
            for argv in manager.commands
        )


def test_detach_delete_data_removes_only_the_garden_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        backend.garden_detach(delete_data=True)
        assert not (ctx.root / "garden" / "data").exists()
        assert not (ctx.root / "garden" / "host.json").exists()
        assert (ctx.root / "garden").is_dir()
        assert (ctx.root / "data" / "cairn.db").read_text() == "cairn\n"
        assert (ctx.root / "config.yaml").read_text() == "cairn\n"


def test_detach_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        backend.garden_detach(delete_data=False)
        first = copy.deepcopy(ctx.state)
        manager.commands.clear()
        backend.garden_detach(delete_data=False)
        assert ctx.state == first
        assert "stop" not in manager.verbs()
        assert "disable" not in manager.verbs()
        assert (ctx.root / "garden" / "data" / "retained").exists()


def test_detach_refuses_modified_owned_file_before_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        (ctx.root / "garden" / "host.json").write_text('{"edited": true}\n')
        with pytest.raises(InstallError, match="changed"):
            backend.garden_detach(delete_data=False)
        assert (manager.units / GARDEN_UNIT).exists()
        assert GARDEN_UNIT in manager.active
        assert "disable" not in manager.verbs()
        assert (ctx.root / "garden" / "bin" / "garden").exists()


def test_detach_resumes_after_interruption_during_unit_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        manager.fail_next_reload = True
        with pytest.raises(InstallError, match="daemon-reload"):
            backend.garden_detach(delete_data=False)
        assert not (manager.units / GARDEN_UNIT).exists()
        backend.garden_detach(delete_data=False)
        for path in garden_paths(ctx, manager):
            assert not path.exists(), path
        assert ctx.state["resources"]["garden_native"]["native_unit"]["status"] == (
            "removed"
        )


def test_detach_resumes_after_files_were_removed_but_not_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        (ctx.root / "garden" / "bin" / "garden").unlink()
        (ctx.root / "garden" / "host.json").unlink()
        backend.garden_detach(delete_data=False)
        for path in garden_paths(ctx, manager):
            assert not path.exists(), path
        assert str(ctx.root / "garden" / "host.json") not in ctx.state["owned_files"]
        assert (ctx.root / "garden" / "data" / "retained").exists()


@pytest.mark.parametrize("delete_data", [False, True])
def test_prepare_after_detach_recreates_the_garden_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_data: bool
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        backend.garden_detach(delete_data=delete_data)
        backend.garden_prepare()
        backend.garden_start()
        assert (manager.units / GARDEN_UNIT).exists()
        assert (ctx.root / "garden" / "bin" / "garden").exists()
        assert (ctx.root / "garden" / "host.json").exists()
        assert GARDEN_UNIT in manager.active
        assert str(ctx.root / "garden" / "bin" / "garden") in ctx.state["owned_files"]
        assert ctx.state["resources"]["garden_native"]["native_unit"]["status"] == (
            "enabled"
        )
        assert (ctx.root / "garden" / "data").is_dir()
        assert (ctx.root / "config.yaml").read_text() == "cairn\n"


class HookRunner:
    """Only builds and snapshots are commands; file mutations stay real."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.commands: list[tuple[list[str], dict[str, Any]]] = []
        self.version = "garden version 9.9.9\n"
        self.snapshot_output: str | None = None
        self.interrupt_snapshot = False
        monkeypatch.setattr(Context, "command", self.command)

    def command(self, argv: list[str], **kwargs: Any) -> str:
        self.commands.append((argv, kwargs))
        if argv[:2] == ["go", "build"]:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"new executable\n")
            return ""
        if argv[-1] == "--version":
            return self.version
        assert argv[1:3] == ["snapshot", "create"]
        root = Path(argv[argv.index("--backup-dir") + 1])
        snapshot_id = "20261019T120000Z-" + argv[-1]
        snapshot = root / snapshot_id
        (snapshot / "data").mkdir(parents=True, mode=0o700)
        snapshot.chmod(0o700)
        (snapshot / "data" / "state.db").write_bytes(b"snapshot database\n")
        (snapshot / "host.json").write_text("{}\n")
        (snapshot / "manifest.json").write_text("{}\n")
        if self.interrupt_snapshot:
            self.interrupt_snapshot = False
            raise KeyboardInterrupt
        return self.snapshot_output or f"snapshot created: {snapshot_id} (20 B)\n"


def prepare_hooks(ctx: Context, tmp_path: Path) -> tuple[GardenBackend, Path, Path]:
    backend = GardenBackend(ctx)
    ctx.write_file(backend.binary, "old executable\n", mode=0o755)
    ctx.write_file(backend.config_path, "{}\n")
    backend.data_path.mkdir(mode=0o700)
    (backend.data_path / "state.db").write_bytes(b"old database\n")
    source = tmp_path / "new-source"
    (source / "garden").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    ctx.state["garden"]["upgrade"] = {"id": "upgrade-1", "aside": None}
    ctx.save()
    cold = ctx.root / "garden-upgrades" / "upgrade-1" / "data.tar"
    return backend, source, cold


def record_cold_copy(ctx: Context, cold: Path) -> None:
    import hashlib

    ctx.state["garden"]["upgrade"]["cold_copy"] = {
        "path": str(cold),
        "sha256": hashlib.sha256(cold.read_bytes()).hexdigest(),
        "bytes": cold.stat().st_size,
    }
    ctx.save()


def test_upgrade_stage_builds_selected_source_and_reuses_owned_artefact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    runner = HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        before = backend.binary.read_bytes()
        artefact = backend.garden_stage(source)
        staged = backend.binary.with_name(".garden-upgrade-1")
        assert artefact == {
            "binary_sha256": hashlib.sha256(staged.read_bytes()).hexdigest()
        }
        assert ctx.state["file_intents"][str(staged)] == artefact["binary_sha256"]
        assert ctx.state["owned_files"][str(staged)] == artefact["binary_sha256"]
        assert staged.stat().st_mode & 0o777 == 0o755
        assert backend.garden_stage(source) == artefact
        assert backend.binary.read_bytes() == before
        builds = [
            (argv, kwargs)
            for argv, kwargs in runner.commands
            if argv[:2] == ["go", "build"]
        ]
        assert len(builds) == 1
        argv, kwargs = builds[0]
        assert kwargs["cwd"] == source / "garden"
        assert kwargs["env"] == backend._go_env()
        assert kwargs["timeout"] == 900
        assert "-trimpath" in argv and "-mod=readonly" in argv
        assert argv[argv.index("-ldflags") + 1].endswith("cmd.version=9.9.9")
        assert any(argv == [str(staged), "--version"] for argv, _ in runner.commands)


def test_upgrade_stage_rejects_wrong_version_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HookRunner(monkeypatch)
    runner.version = "garden version dev\n"
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        with pytest.raises(InstallError, match="wrong version"):
            backend.garden_stage(source)
        assert not backend.binary.with_name(".garden-upgrade-1").exists()
        assert backend.binary.read_text() == "old executable\n"


@pytest.mark.parametrize("change", ["unowned", "content", "mode", "symlink"])
def test_upgrade_stage_refuses_foreign_or_changed_staged_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        backend.garden_stage(source)
        staged = backend.binary.with_name(".garden-upgrade-1")
        if change == "unowned":
            ctx.state["owned_files"].pop(str(staged))
            ctx.state["file_intents"].pop(str(staged))
        elif change == "content":
            staged.write_text("tampered\n")
        elif change == "mode":
            staged.chmod(0o700)
        else:
            staged.unlink()
            staged.symlink_to(backend.binary)
        with pytest.raises(InstallError):
            backend.garden_stage(source)


def test_upgrade_copy_is_owned_checked_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import tarfile

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        (backend.data_path / "nested").mkdir(mode=0o700)
        (backend.data_path / "nested" / "messages").write_text("messages\n")
        backend.garden_copy(cold)
        initial = cold.read_bytes()
        assert (
            ctx.state["owned_files"][str(cold)] == hashlib.sha256(initial).hexdigest()
        )
        assert cold.stat().st_mode & 0o777 == 0o600
        with tarfile.open(cold) as archive:
            assert "data/state.db" in archive.getnames()
            assert "data/nested/messages" in archive.getnames()
        (backend.data_path / "state.db").write_text("changed after copy\n")
        # A landed publication whose directory sync failed is synced on retry.
        synced: list[Path] = []
        monkeypatch.setattr(garden_native, "_fsync_directory", synced.append)
        backend.garden_copy(cold)
        assert synced == [cold.parent]
        assert cold.read_bytes() == initial
        cold.write_bytes(initial + b"tampered")
        with pytest.raises(InstallError, match="changed"):
            backend.garden_copy(cold)


def test_cold_copy_size_matches_the_native_archive(tmp_path: Path) -> None:
    import os

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        nested = backend.data_path / ("n" * 90) / ("m" * 90)
        nested.mkdir(mode=0o700, parents=True)
        for number in range(30):
            path = nested / f"{'p' * 120}-{number}"
            path.write_bytes(b"x" * (number * 97))
            os.utime(path, (1.5 + number, 1_700_000_000.25 + number))
        # The writer dereferences, so a hard link is archived as a plain file.
        os.link(nested / f"{'p' * 120}-29", nested / f"{'q' * 120}-link")
        size, archive = garden_upgrade._cold_copy_size(backend.data_path)
        backend.garden_copy(cold)
        assert archive == cold.stat().st_size
        assert size == sum(
            path.stat().st_size
            for path in backend.data_path.rglob("*")
            if path.is_file()
        )


@pytest.mark.parametrize("member", ["symlink", "fifo", "missing-db"])
def test_upgrade_copy_refuses_unsafe_tree_or_missing_state(
    tmp_path: Path, member: str
) -> None:
    import os

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        if member == "symlink":
            (backend.data_path / "escape").symlink_to(tmp_path)
        elif member == "fifo":
            os.mkfifo(backend.data_path / "pipe")
        else:
            (backend.data_path / "state.db").unlink()
        with pytest.raises(InstallError):
            backend.garden_copy(cold)
        assert not cold.exists()
        with pytest.raises(InstallError, match="destination"):
            backend.garden_copy(tmp_path / "not-owned.tar")


@pytest.mark.parametrize("landed", [False, True])
@pytest.mark.parametrize("rollback", [False, True])
def test_upgrade_swap_resumes_both_landed_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: bool, rollback: bool
) -> None:
    import os

    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        old = {"binary_sha256": ctx.state["owned_files"][str(backend.binary)]}
        new = backend.garden_stage(source)
        expected = new
        if rollback:
            backend.garden_swap(new)
            expected = old
        replace = os.replace

        def interrupt(source: Any, dest: Any) -> None:
            if Path(dest) == backend.binary:
                # Both digests must be durable before any rename.
                journal = (
                    json.loads(ctx.state_path.read_text())
                    if hasattr(ctx, "state_path")
                    else json.loads((ctx.directory / "state.json").read_text())
                )
                assert (
                    journal["file_replacements"][str(backend.binary)]["new"]
                    == expected["binary_sha256"]
                )
                if landed:
                    replace(source, dest)
                raise KeyboardInterrupt
            replace(source, dest)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupt)
            with pytest.raises(KeyboardInterrupt):
                backend.garden_swap(expected)
    # A fresh context proves recovery uses persisted state, not in-memory edits.
    with open_context(tmp_path / "state", "demo") as ctx:
        backend = GardenBackend(ctx)
        backend.garden_swap(expected)
        backend.garden_swap(expected)
        assert (
            ctx.state["owned_files"][str(backend.binary)] == expected["binary_sha256"]
        )
        other = new if rollback else old
        previous = backend.binary.with_name("garden.previous")
        assert ctx.state["owned_files"][str(previous)] == other["binary_sha256"]
        assert previous.stat().st_mode & 0o777 == 0o755
        assert str(backend.binary) not in ctx.state["file_replacements"]
        backend._verify_owned(backend.binary)
        backend._verify_owned(previous)


@pytest.mark.parametrize("landed", [False, True])
def test_upgrade_swap_abort_selects_old_release_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: bool
) -> None:
    import os

    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        old = {"binary_sha256": ctx.state["owned_files"][str(backend.binary)]}
        new = backend.garden_stage(source)
        replace = os.replace

        def interrupt(source: Any, dest: Any) -> None:
            if Path(dest) == backend.binary:
                if landed:
                    replace(source, dest)
                raise KeyboardInterrupt
            replace(source, dest)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupt)
            with pytest.raises(KeyboardInterrupt):
                backend.garden_swap(new)
        backend.garden_swap(old)
        backend._verify_owned(backend.binary)
        assert backend.binary.read_text() == "old executable\n"


@pytest.mark.parametrize("change", ["current", "target", "previous", "unknown-digest"])
def test_upgrade_swap_refuses_changed_or_missing_artefacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        new = backend.garden_stage(source)
        if change == "current":
            backend.binary.write_text("changed\n")
        elif change == "target":
            backend.binary.with_name(".garden-upgrade-1").write_text("changed\n")
        elif change == "previous":
            backend.binary.with_name("garden.previous").write_text("foreign\n")
        else:
            new = {"binary_sha256": "0" * 64}
        before = backend.binary.read_bytes()
        with pytest.raises(InstallError):
            backend.garden_swap(new)
        assert backend.binary.read_bytes() == before


@pytest.mark.parametrize("interrupt_at", ["none", "aside", "publish"])
def test_upgrade_restore_retains_aside_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt_at: str
) -> None:
    import os

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        backend.garden_copy(cold)
        record_cold_copy(ctx, cold)
        (backend.data_path / "state.db").write_text("new database\n")
        rename = os.rename

        def interrupt(source: Any, dest: Any) -> None:
            rename(source, dest)
            if (interrupt_at == "aside" and Path(source) == backend.data_path) or (
                interrupt_at == "publish" and Path(dest) == backend.data_path
            ):
                raise KeyboardInterrupt

        if interrupt_at != "none":
            with monkeypatch.context() as patch:
                patch.setattr(os, "rename", interrupt)
                with pytest.raises(KeyboardInterrupt):
                    backend.garden_restore_data(cold)
    with open_context(tmp_path / "state", "demo") as ctx:
        backend = GardenBackend(ctx)
        backend.garden_restore_data(cold)
        backend.garden_restore_data(cold)
        assert (backend.data_path / "state.db").read_text() == "old database\n"
        aside = Path(ctx.state["garden"]["upgrade"]["aside"])
        assert (aside / "state.db").read_text() == "new database\n"


@pytest.mark.parametrize(
    "member",
    ["symlink", "hardlink", "fifo", "traversal", "absolute", "duplicate", "missing-db"],
)
def test_upgrade_restore_refuses_unsafe_archive_before_moving_data(
    tmp_path: Path, member: str
) -> None:
    import hashlib
    import io
    import tarfile

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        cold.parent.mkdir(parents=True, mode=0o700)
        with tarfile.open(cold, "w") as archive:
            if member != "missing-db":
                info = tarfile.TarInfo("data/state.db")
                info.size = 3
                archive.addfile(info, io.BytesIO(b"old"))
            info = tarfile.TarInfo("data/member")
            if member == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/tmp"
            elif member == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, "data/state.db"
            elif member == "fifo":
                info.type = tarfile.FIFOTYPE
            elif member == "traversal":
                info.name = "data/../../escape"
            elif member == "absolute":
                info.name = "/escape"
            elif member == "duplicate":
                info.name = "data/state.db"
            archive.addfile(info)
        ctx.state["owned_files"][str(cold)] = hashlib.sha256(
            cold.read_bytes()
        ).hexdigest()
        record_cold_copy(ctx, cold)
        with pytest.raises(InstallError):
            backend.garden_restore_data(cold)
        assert (backend.data_path / "state.db").read_text() == "old database\n"
        assert ctx.state["garden"]["upgrade"]["aside"] is None


def test_upgrade_restore_refuses_bad_receipt_and_changed_published_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        backend.garden_copy(cold)
        record_cold_copy(ctx, cold)
        ctx.state["garden"]["upgrade"]["cold_copy"]["bytes"] += 1
        with pytest.raises(InstallError, match="receipt"):
            backend.garden_restore_data(cold)
        record_cold_copy(ctx, cold)
        backend.garden_restore_data(cold)
        # A retry after the publication landed makes the rename durable too.
        synced: list[Path] = []
        monkeypatch.setattr(garden_native, "_fsync_directory", synced.append)
        backend.garden_restore_data(cold)
        assert backend.data_path.parent in synced
        (backend.data_path / "state.db").write_text("changed\n")
        with pytest.raises(InstallError, match="changed after interrupted restore"):
            backend.garden_restore_data(cold)


def test_snapshot_retry_recovers_by_name_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HookRunner(monkeypatch)
    runner.interrupt_snapshot = True
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        with pytest.raises(KeyboardInterrupt):
            backend.garden_snapshot("backup1")
        backend.ctx.state["resources"].pop("snapshots")
        ctx.save()
        backend = GardenBackend(ctx)
        snapshot_id = backend.garden_snapshot("backup1")
        assert snapshot_id == "20261019T120000Z-backup1"
        assert len(runner.commands) == 1
        assert (
            backend.data_path.parent / "backups" / snapshot_id / "data/state.db"
        ).read_bytes() == b"snapshot database\n"
        backend.garden_export(snapshot_id, ctx.root / "export")


def test_snapshot_lookup_does_not_confuse_overlapping_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        root = backend.data_path.parent / "backups"
        secure_directory(root, private=True)
        (root / "20261019T110000Z-weekly-backup1").mkdir(mode=0o700)
        assert backend.garden_snapshot("backup1") == "20261019T120000Z-backup1"
        assert len(runner.commands) == 1


@pytest.mark.parametrize("recorded", [False, True])
def test_snapshot_refuses_multiple_matching_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    runner = HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        backend.garden_snapshot("backup1")
        if not recorded:
            backend.ctx.state["resources"].pop("snapshots")
        root = backend.data_path.parent / "backups"
        (root / "20261019T120001Z-backup1").mkdir(mode=0o700)
        with pytest.raises(InstallError, match="ambiguous"):
            backend.garden_snapshot("backup1")
        assert len(runner.commands) == 1


@pytest.mark.parametrize("interrupted", [False, True])
def test_snapshot_export_delete_uses_owned_binary_and_reuses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    runner = HookRunner(monkeypatch)
    runner.interrupt_snapshot = interrupted
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                backend.garden_snapshot("backup1")
        snapshot_id = backend.garden_snapshot("backup1")
        assert backend.garden_snapshot("backup1") == snapshot_id
        assert snapshot_id == "20261019T120000Z-backup1"
        [(argv, kwargs)] = runner.commands
        assert argv == [
            str(backend.binary),
            "snapshot",
            "create",
            "--host-config",
            str(backend.config_path),
            "--backup-dir",
            str(ctx.root / "garden" / "backups"),
            "backup1",
        ]
        assert kwargs["private"] is True
        dest = ctx.root / "garden-backups" / "export"
        backend.garden_export(snapshot_id, dest)
        backend.garden_export(snapshot_id, dest)
        assert (dest / "data" / "state.db").read_bytes() == b"snapshot database\n"
        assert str(dest / "host.json") in ctx.state["owned_files"]
        assert dest.stat().st_mode & 0o777 == 0o700
        assert (dest / "host.json").stat().st_mode & 0o777 == 0o600
        backend.garden_snapshot_delete(snapshot_id)
        backend.garden_snapshot_delete(snapshot_id)
        assert not (ctx.root / "garden" / "backups" / snapshot_id).exists()
        assert (backend.data_path / "state.db").exists()
        assert (dest / "host.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        "binary",
        "name",
        "output",
        "snapshot",
        "symlink",
        "unowned-export",
        "changed-export",
        "unowned-delete",
        "traversal",
    ],
)
def test_snapshot_hooks_refuse_unsafe_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    runner = HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        if change == "binary":
            backend.binary.write_text("changed\n")
        elif change == "name":
            with pytest.raises(InstallError):
                backend.garden_snapshot("../escape")
            assert not runner.commands
            return
        elif change == "output":
            runner.snapshot_output = "snapshot created: ../../escape (20 B)\n"
        if change in {"binary", "output"}:
            with pytest.raises(InstallError):
                backend.garden_snapshot("backup1")
            return
        snapshot_id = backend.garden_snapshot("backup1")
        root = ctx.root / "garden" / "backups" / snapshot_id
        dest = ctx.root / "export"
        if change == "snapshot":
            (root / "host.json").write_text("changed\n")
        elif change == "symlink":
            (root / "escape").symlink_to(tmp_path)
        elif change in {"unowned-export", "changed-export"}:
            if change == "changed-export":
                backend.garden_export(snapshot_id, dest)
            else:
                dest.mkdir(mode=0o700)
            (dest / "host.json").write_text("foreign\n")
        elif change == "unowned-delete":
            with pytest.raises(InstallError, match="not owned"):
                backend.garden_snapshot_delete("20261019T120001Z-foreign")
            return
        elif change == "traversal":
            with pytest.raises(InstallError):
                backend.garden_export("../escape", dest)
            return
        with pytest.raises(InstallError):
            backend.garden_export(snapshot_id, dest)
        if change in {"snapshot", "symlink"}:
            with pytest.raises(InstallError):
                backend.garden_snapshot_delete(snapshot_id)
        assert root.exists()


def test_upgrade_prune_only_deletes_owned_unretained_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, source, _ = prepare_hooks(ctx, tmp_path)
        new = backend.garden_stage(source)
        old = {"binary_sha256": ctx.state["owned_files"][str(backend.binary)]}
        backend.garden_swap(new)
        previous = backend.binary.with_name("garden.previous")
        stale = backend.binary.with_name(".garden-stale")
        ctx.write_file(stale, "stale\n", mode=0o755)
        foreign = backend.binary.with_name(".garden-foreign")
        foreign.write_text("foreign\n")
        backend.garden_prune([new, old])
        assert previous.exists() and not stale.exists() and foreign.exists()
        backend.garden_prune([new])
        backend.garden_prune([new])
        assert not previous.exists()
        backend._verify_owned(backend.binary)
        ctx.write_file(stale, "stale\n", mode=0o755)
        stale.write_text("tampered\n")
        with pytest.raises(InstallError, match="changed"):
            backend.garden_prune([])
        assert stale.exists()


@pytest.mark.parametrize("delete_data", [False, True])
def test_detach_retains_upgrade_data_unless_delete_data_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_data: bool
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        ctx.state["garden"]["upgrade"] = {"id": "upgrade-1"}
        (backend.data_path / "state.db").write_text("database\n")
        cold = ctx.root / "garden-upgrades" / "upgrade-1" / "data.tar"
        backend.garden_copy(cold)
        record_cold_copy(ctx, cold)
        backend.garden_restore_data(cold)
        previous = backend.binary.with_name("garden.previous")
        ctx.write_file(previous, "previous\n", mode=0o755)
        aside = Path(ctx.state["garden"]["upgrade"]["aside"])
        backend.garden_detach(delete_data=delete_data)
        backend.garden_detach(delete_data=delete_data)
        assert cold.exists() is not delete_data
        assert aside.exists() is not delete_data
        assert previous.exists() is not delete_data
        assert (ctx.root / "data" / "cairn.db").exists()
        if delete_data:
            assert not (ctx.root / "garden-upgrades").exists()
            assert str(cold) not in ctx.state["owned_files"]
            assert str(previous) not in ctx.state["owned_files"]


@pytest.mark.parametrize(
    "path",
    ["garden-upgrades", "garden/data.pre-restore-bad", "garden/bin/garden.previous"],
)
def test_delete_data_refuses_foreign_paths_without_following_them(
    tmp_path: Path, path: str
) -> None:
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        (outside / "retained").write_text("retained\n")
        target = ctx.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside)
        with pytest.raises(InstallError):
            backend._delete_data()
        assert (outside / "retained").read_text() == "retained\n"
        assert (backend.data_path / "state.db").exists()


@pytest.mark.parametrize("mode", ["docker", "kubernetes"])
def test_new_native_hooks_refuse_other_modes(tmp_path: Path, mode: str) -> None:
    with context(tmp_path) as ctx:
        ctx.state["mode"] = mode
        with pytest.raises(InstallError, match="requires native mode"):
            GardenBackend(ctx)


def test_snapshot_delete_resumes_after_partial_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        snapshot_id = backend.garden_snapshot("backup1")
        root = backend.data_path.parent / "backups" / snapshot_id
        original = shutil.rmtree

        def interrupt(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == root:
                (root / "host.json").unlink()
                raise KeyboardInterrupt
            original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(shutil, "rmtree", interrupt)
            with pytest.raises(KeyboardInterrupt):
                backend.garden_snapshot_delete(snapshot_id)
        with pytest.raises(InstallError, match="deletion is in progress"):
            backend.garden_export(snapshot_id, ctx.root / "export")
    with open_context(tmp_path / "state", "demo") as ctx:
        backend = GardenBackend(ctx)
        backend.garden_snapshot_delete(snapshot_id)
        backend.garden_snapshot_delete(snapshot_id)
        assert not root.exists()


def test_snapshot_delete_refuses_changed_remaining_member_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        snapshot_id = backend.garden_snapshot("backup1")
        root = backend.data_path.parent / "backups" / snapshot_id
        original = shutil.rmtree

        def interrupt(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == root:
                (root / "host.json").unlink()
                raise KeyboardInterrupt
            original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(shutil, "rmtree", interrupt)
            with pytest.raises(KeyboardInterrupt):
                backend.garden_snapshot_delete(snapshot_id)
        (root / "data" / "state.db").write_text("changed\n")
        with pytest.raises(InstallError, match="changed"):
            backend.garden_snapshot_delete(snapshot_id)
        assert root.exists()


def test_delete_data_removes_staged_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    HookRunner(monkeypatch)
    with context(tmp_path) as ctx:
        backend, _, _ = prepare_hooks(ctx, tmp_path)
        snapshot_id = backend.garden_snapshot("backup1")
        backend._delete_data()
        assert not (backend.data_path.parent / "backups" / snapshot_id).exists()
        backend._delete_data()


def test_delete_data_refuses_changed_previous_before_detach_stops_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = UserManager(tmp_path, monkeypatch)
    with full_context(tmp_path, garden_options()) as ctx:
        backend = install_garden(ctx, manager)
        previous = backend.binary.with_name("garden.previous")
        ctx.write_file(previous, "previous\n", mode=0o755)
        previous.write_text("changed\n")
        with pytest.raises(InstallError, match="changed"):
            backend.garden_detach(delete_data=True)
        assert GARDEN_UNIT in manager.active
        assert "stop" not in manager.verbs()
        assert backend.binary.exists() and backend.config_path.exists()


def test_upgrade_copy_refuses_unreadable_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    with context(tmp_path) as ctx:
        backend, _, cold = prepare_hooks(ctx, tmp_path)
        original = os.walk

        def denied(path: Any, **kwargs: Any) -> Any:
            # os.walk normally ignores scandir failures unless onerror is set.
            if path == backend.data_path:
                error = PermissionError(13, "Permission denied", str(path / "messages"))
                if kwargs.get("onerror") is not None:
                    kwargs["onerror"](error)
                return iter([(str(path), [], ["state.db"])])
            return original(path, **kwargs)

        monkeypatch.setattr(os, "walk", denied)
        with pytest.raises(InstallError, match="Cannot inspect Garden tree"):
            backend.garden_copy(cold)
        assert not cold.exists()
