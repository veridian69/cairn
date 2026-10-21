"""Offline system-server lifecycle, authority binding and sudo policy boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import runpy
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cairn_install import cli, garden, garden_backup, sudo, workflow
from cairn_install import garden_server as server
from cairn_install.core import Context, InstallError, atomic_write, open_context

from .test_garden import Authority
from .test_garden import configuration as configuration

ROOT = Path(__file__).parents[2]
EXTERNAL_REQUEST = server.request
CHECK_PATH = server._check_path
SYSTEM_TOOL = server._system_tool
NULL_DIGEST = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  /dev/null\n"
)
NULL_TYPE = "character special file\n"


def binding(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    result = {
        "cairn": {
            "url": "http://127.0.0.1:8100",
            "instance_id": str(uuid4()),
            "realm": "cairn",
            "admin_token_file": str(tmp_path / "admin.token"),
        },
        "account": "garden",
        "requires": ["tailscaled.service"],
        "backup_hook": {
            "unit": "cairn-v1-backup.service",
            "on_failure": "site-backup-alert@%n.service",
            "export_dir": "/opt/cairn-test-garden-exports",
            "export_owner": "operator",
            "label": "site",
        },
    }
    result.update(extra)
    return result


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value))
    return path


class World:
    """No system commands run. Root files and account state live only in memory."""

    def __init__(self, ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ctx = ctx
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.accounts: set[str] = set()
        self.calls: list[list[str]] = []
        self.effect_fault: str | None = None
        self.authority = Authority(ctx)
        self.changed_contract = False
        self.digest_denied = False
        atomic_write(
            Path(ctx.state["server"]["cairn"]["admin_token_file"]),
            (self.authority.admin + "\n").encode(),
        )
        monkeypatch.setattr(ctx, "command", self.command)
        monkeypatch.setattr(server, "request", self.request)
        monkeypatch.setattr(server, "_account", self.account)
        monkeypatch.setattr(server, "_check_path", lambda path: str(path) in self.files)
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(server, "_system_tool", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(garden, "verify", self.verify)
        exists, is_dir, lstat = Path.exists, Path.is_dir, Path.lstat
        self.virtual = {
            "/var/lib/garden",
            "/var/lib/garden/.garden",
            "/etc/garden",
            "/etc/garden/tls",
            *sudo.directories(ctx),
        }
        monkeypatch.setattr(
            Path,
            "exists",
            lambda path: (
                str(path) in self.dirs if str(path) in self.virtual else exists(path)
            ),
        )
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: (
                str(path) in self.dirs if str(path) in self.virtual else is_dir(path)
            ),
        )

        def directory_info(path: Path, **kwargs: Any) -> Any:
            if str(path) in self.virtual:
                if str(path) not in self.dirs:
                    raise FileNotFoundError(str(path))
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
            return lstat(path, **kwargs)

        monkeypatch.setattr(Path, "lstat", directory_info)

    def account(self, ctx: Context, kind: str) -> Any:
        if kind not in self.accounts:
            return None
        return (
            SimpleNamespace(gr_gid=950)
            if kind == "group"
            else SimpleNamespace(
                pw_uid=950,
                pw_gid=950,
                pw_dir="/var/lib/garden",
                pw_shell="/usr/sbin/nologin",
            )
        )

    def request(
        self,
        ctx: Context,
        endpoint: str,
        token: str = "",
        *,
        data: bytes | None = None,
        key: str = "",
    ) -> dict[str, Any]:
        if endpoint == "/health/ready":
            return {"status": "ready"}
        status, result = self.authority.result(
            endpoint, json.loads(data) if data else {}, token, key
        )
        if status != 200:
            raise InstallError("fake HTTP failure")
        if endpoint == "/v1/instance":
            result.update(
                product_version="0.1.0rc2",
                contract_digest=("9" if self.changed_contract else "a") * 64,
                mcp_contract_digest="b" * 64,
            )
        if endpoint == "/memory/v1/diagnose":
            result.update(contract_digest="c" * 64, mcp_contract_digest="d" * 64)
        return result

    def verify(self, ctx: Context) -> None:
        assert self.files["/etc/garden/garden.json"]
        assert ctx.state["garden"]["agents"]

    def command(self, args: Any, **kwargs: Any) -> str:
        argv: list[str] = list(args)
        self.calls.append(argv)
        if argv[:2] == ["go", "build"]:
            atomic_write(Path(argv[argv.index("-o") + 1]), b"fake Garden binary", 0o755)
            return ""
        if argv[0] == str(self.ctx.source / "garden/scripts/install-server"):
            assert argv[1:3] == ["--root", str(sudo.stage_root(self.ctx))]
            root = sudo.stage_root(self.ctx)
            sources = {
                f"usr/local/bin/{name}": self.ctx.source / "garden/scripts" / name
                for name in ("garden-config", "garden-session")
            }
            sources["usr/local/bin/garden"] = Path(argv[-1])
            for source in (self.ctx.source / "garden/deploy").iterdir():
                sources["usr/local/share/garden/deploy/" + source.name] = source
            for unit in sudo.UNITS:
                sources["etc/systemd/system/" + unit] = (
                    self.ctx.source / "garden/deploy" / unit
                )
            sources["var/lib/garden/.garden/config.yaml"] = (
                self.ctx.source / "garden/deploy/daemon.yaml"
            )
            manifest = {}
            for path, source in sources.items():
                data = source.read_bytes()
                atomic_write(root / path, data)
                manifest[path] = hashlib.sha256(data).hexdigest()
            atomic_write(
                root / "usr/local/share/garden/install-manifest.json",
                json.dumps(
                    {"version": 1, "kind": "server", "files": manifest}
                ).encode(),
                0o644,
            )
            return ""
        assert argv in sudo.policy_commands(self.ctx), argv
        verb = Path(argv[2]).name
        if verb == "sha256sum":
            assert argv[-1] == "/dev/null"
            assert kwargs["allowed"] == (0, 1)
            path = argv[-2]
            if self.digest_denied:
                return ""
            output = (
                hashlib.sha256(self.files[path]).hexdigest() + "  " + path + "\n"
                if path in self.files
                else ""
            )
            return output + NULL_DIGEST
        if verb == "stat":
            assert argv[3:] == ["--format=%F", "--", argv[-2], "/dev/null"]
            assert kwargs["allowed"] == (0, 1)
            return ("regular file\n" if argv[-2] in self.files else "") + NULL_TYPE
        if verb in {"groupadd", "useradd"}:
            self.accounts.add("group" if verb == "groupadd" else "user")
        elif verb == "install":
            if "-d" in argv:
                self.dirs.update(argv[argv.index("-m") + 2 :])
            else:
                self.files[argv[-1]] = Path(argv[-2]).read_bytes()
                self.fault("file")
        elif verb == "rm":
            self.files.pop(argv[-1], None)
            self.fault("rm")
        return ""

    def fault(self, point: str) -> None:
        if self.effect_fault == point:
            self.effect_fault = None
            raise KeyboardInterrupt("after fake side effect")


@pytest.fixture
def managed(
    tmp_path: Path,
    configuration: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    path, value = configuration
    value["scope"]["realm"] = "cairn"
    options = garden.load_options(
        write_json(path, value), "garden-server", realm="cairn"
    )
    config = server.load_options(
        write_json(tmp_path / "binding.json", binding(tmp_path))
    )
    create = {
        "mode": "garden-server",
        "port": 8100,
        "semantic": False,
        "source": str(ROOT),
        "source_fingerprint": cli.source_fingerprint(ROOT),
        "server": {**config, "installed": {}},
        "garden": {"origin": "server", "options": options},
    }
    with open_context(tmp_path / "state", "system", create=create) as ctx:
        world = World(ctx, monkeypatch)
        yield ctx, world, path, tmp_path / "binding.json"


def test_strict_binding_and_optional_fields(tmp_path: Path) -> None:
    value = binding(tmp_path)
    value.pop("requires")
    value.pop("backup_hook")
    path = write_json(tmp_path / "server.json", value)
    loaded = server.load_options(path)
    assert loaded["requires"] == [] and loaded["backup_hook"] is None
    value["cairn"]["url"] = "http://[::1]:8100/"
    assert (
        server.load_options(write_json(path, value))["cairn"]["url"]
        == "http://[::1]:8100"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "http://localhost:8100"),
        ("url", "http://127.1:8100"),
        ("url", "http://0x7f000001:8100"),
        ("url", "https://127.0.0.1:8100"),
        ("url", "http://192.0.2.1:8100"),
        ("url", "http://127.0.0.1"),
        ("url", "http://127.0.0.1:8100/api"),
        ("url", "http://a@127.0.0.1:8100"),
        ("url", "http://[::1%25lo]:8100"),
        ("instance_id", "92433992-DD93-444D-B333-FA1C2DB315AA"),
        ("realm", "bad realm"),
        ("admin_token_file", "relative"),
    ],
)
def test_binding_refuses_bad_identity(tmp_path: Path, field: str, value: Any) -> None:
    config = binding(tmp_path)
    config["cairn"][field] = value
    with pytest.raises(InstallError):
        server.load_options(write_json(tmp_path / "server.json", config))


@pytest.mark.parametrize(
    "change",
    [
        {"account": "root"},
        {"account": "garden;id"},
        {"requires": ["tailscaled.service", "tailscaled.service"]},
        {"requires": ["garden-daemon.service"]},
        {"requires": "tailscaled.service"},
        {"kubernetes": {}},
        {"backup_hook": {}},
        {"extra": True},
    ],
)
def test_binding_refuses_unknown_and_unsafe_fields(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    with pytest.raises(InstallError):
        server.load_options(
            write_json(tmp_path / "server.json", binding(tmp_path, **change))
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("export_dir", "/etc"),
        ("export_dir", "/var/lib/garden/data"),
        ("export_dir", "/"),
        ("export_owner", "root"),
        ("label", "bad label"),
        ("on_failure", "a.service\nExecStart=/bin/false"),
        ("unit", "garden-mcp.service"),
    ],
)
def test_hook_refusals(tmp_path: Path, field: str, value: Any) -> None:
    config = binding(tmp_path)
    config["backup_hook"][field] = value
    with pytest.raises(InstallError):
        server.load_options(write_json(tmp_path / "server.json", config))


@pytest.mark.parametrize("label", ["a", "a1" * 8])
def test_backup_label_boundaries(tmp_path: Path, label: str) -> None:
    config = binding(tmp_path)
    config["backup_hook"]["label"] = label
    loaded = server.load_options(write_json(tmp_path / "server.json", config))
    assert loaded["backup_hook"]["label"] == label


@pytest.mark.parametrize("label", ["a" * 17, "a" * 48, "ab-c", "A", "a_b", "é", ""])
def test_backup_label_refused_before_provisioning(
    managed: Any, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, world, garden_path, config_path = managed
    config = binding(config_path.parent)
    config["backup_hook"]["label"] = label
    write_json(config_path, config)
    provisioned = []
    monkeypatch.setattr(server, "run", lambda context: provisioned.append(context))
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "garden-server",
                "--name",
                "bad-label",
                "--server-config",
                str(config_path),
                "--garden-config",
                str(garden_path),
                "--state-root",
                str(ctx.directory.parent),
                "--non-interactive",
            ],
            default_source=ROOT,
        )
        == 2
    )
    assert not provisioned and not world.calls
    assert not (ctx.directory.parent / "bad-label").exists()


@pytest.mark.parametrize("point", ["preflight", "pending", "complete"])
@pytest.mark.parametrize("destination", ["export", "unit_dropin"])
def test_directory_ancestor_symlinks_refused(
    managed: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    destination: str,
) -> None:
    ctx, world, _, _ = managed
    hook = ctx.state["server"]["backup_hook"]
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    if destination == "export":
        target = str(link / "missing" / "exports")
        hook["export_dir"] = target
    else:
        target = f"/etc/systemd/system/{hook['unit']}.d"
        lstat = Path.lstat

        def ancestor(path: Path, **kwargs: Any) -> Any:
            if path == Path("/etc/systemd"):
                return lstat(link, **kwargs)
            return lstat(path, **kwargs)

        monkeypatch.setattr(Path, "lstat", ancestor)
    if point != "preflight":
        ctx.state["server"]["tools"] = {name: "/usr/bin/" + name for name in sudo.TOOLS}
        ctx.state["server"]["directories"] = {target: point}
    with pytest.raises(InstallError, match="[Uu]nsafe|[Ss]ymlink"):
        if point == "preflight":
            server.preflight(ctx)
        else:
            server._directory(ctx, "dir_sys", [target], path=target)
    assert not (outside / "missing").exists()
    assert not any(Path(call[2]).name == "install" for call in world.calls)


def test_complete_flow_policy_exactness_and_remove_keeps_data(managed: Any) -> None:
    ctx, world, _, _ = managed
    result = server.run(ctx)
    assert result["status"] == "verified"
    assert list(ctx.state["steps"]) == list(server.STAGES)
    assert all(value == "complete" for value in ctx.state["steps"].values())
    assert (
        ctx.instance_id
        == ctx.state["server"]["cairn"]["instance_id"]
        != ctx.state["instance_id"]
    )
    assert ctx.state["server"]["contracts"] == {
        "contract_digest": "a" * 64,
        "mcp_contract_digest": "b" * 64,
    }
    assert ctx.state["server"]["memory_contracts"] == {
        "contract_digest": "c" * 64,
        "mcp_contract_digest": "d" * 64,
    }
    assert b"Type=notify\n" in world.files["/etc/systemd/system/garden-daemon.service"]
    assert b"max_age: 168h" in world.files["/var/lib/garden/.garden/config.yaml"]
    assert world.authority.admin.encode() not in b"".join(world.files.values())
    assert not (ctx.root / "credentials/admin.token").exists()
    bodies = [
        body
        for path, body, _ in world.authority.calls
        if path.startswith("/v1/") and path != "/v1/instance"
    ]
    assert bodies and all(body["realm_id"] == "cairn" for body in bodies)
    assert all(
        body["grant"]["realm_id"] == "cairn" for body in bodies if "grant" in body
    )
    assert server.backup(ctx)["status"] == "complete"
    server.remove(ctx)
    assert "/var/lib/garden" in world.dirs
    assert list(world.files) == ["/var/lib/garden/.garden/config.yaml"]
    assert world.authority.revoked_targets
    # Every privileged argv is one exact policy expansion, with no wildcard grant.
    commands = sudo.policy_commands(ctx)
    privileged = [call for call in world.calls if call[0].endswith("/sudo")]
    assert all(call in commands for call in privileged)
    text = sudo.snippet(ctx)
    emitted = [
        line.split("NOPASSWD: ", 1)[1]
        for line in text.splitlines()
        if "NOPASSWD:" in line
    ]
    assert emitted == [
        " ".join(sudo._escape(arg) for arg in call[2:]) for call in commands
    ]
    assert len(set(emitted)) == len(emitted)
    for path in sudo.files(ctx):
        assert [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/sha256sum",
            "--",
            path,
            "/dev/null",
        ] in commands
        assert [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/stat",
            "--format=%F",
            "--",
            path,
            "/dev/null",
        ] in commands
    assert "rm -rf" not in text
    starts = [
        call[-1] for call in privileged if call[2:4] == ["/usr/bin/systemctl", "start"]
    ]
    assert starts[:2] == list(sudo.UNITS)
    stops = [
        call[-1] for call in privileged if call[2:4] == ["/usr/bin/systemctl", "stop"]
    ]
    assert stops[:2] == list(reversed(sudo.UNITS))
    calls = len(world.calls)
    server.remove(ctx)
    assert not any(
        call[2:4] == ["/usr/bin/systemctl", "stop"] for call in world.calls[calls:]
    )


@pytest.mark.parametrize("point", server.STAGES)
@pytest.mark.parametrize("error", [KeyboardInterrupt, InstallError])
def test_every_stage_interrupt_and_resume(
    managed: Any,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error: type[BaseException],
) -> None:
    ctx, world, _, _ = managed
    original = workflow.stage
    armed = True

    def stage(context: Context, key: str, action: Any) -> None:
        nonlocal armed

        def interrupted() -> Any:
            nonlocal armed
            if key == point and armed:
                armed = False
                raise error("injected stage interruption")
            return action()

        original(context, key, interrupted)

    monkeypatch.setattr(workflow, "stage", stage)
    with pytest.raises(error):
        server.run(ctx)
    assert ctx.state["status"] in {"failed", "interrupted"}
    assert ctx.state["steps"][point] == "running"
    server.run(ctx)
    assert ctx.state["status"] == "verified"
    assert len(world.authority.tokens) == len(
        ctx.state["garden"]["options"]["participants"]
    )


@pytest.mark.parametrize("fault", ["file", "rm"])
def test_resume_after_root_side_effect(managed: Any, fault: str) -> None:
    ctx, world, _, _ = managed
    if fault == "file":
        world.effect_fault = fault
        with pytest.raises(KeyboardInterrupt):
            server.run(ctx)
        server.run(ctx)
    else:
        server.run(ctx)
        world.effect_fault = fault
        with pytest.raises(KeyboardInterrupt):
            server.remove(ctx)
        server.remove(ctx)
    assert ctx.state["status"] == ("verified" if fault == "file" else "rolled_back")


@pytest.mark.parametrize("kind", ["file", "manifest", "directory", "account"])
def test_no_adoption_before_enrolment(managed: Any, kind: str) -> None:
    ctx, world, _, _ = managed
    if kind == "file":
        world.files["/usr/local/bin/garden"] = b"foreign"
    elif kind == "manifest":
        world.files["/usr/local/share/garden/install-manifest.json"] = b"foreign"
    elif kind == "directory":
        world.dirs.add("/var/lib/garden")
    else:
        world.accounts.add("group")
    with pytest.raises(InstallError, match="adopt"):
        server.run(ctx)
    assert not world.authority.calls
    assert not any(
        Path(call[2]).name in {"install", "useradd", "groupadd"}
        for call in world.calls
        if call[0].endswith("/sudo")
    )


def test_changed_digests_refuse_resume_and_remove(managed: Any) -> None:
    ctx, world, _, _ = managed
    server.run(ctx)
    world.files["/usr/local/bin/garden"] = b"manually upgraded"
    result = server.status(ctx)
    assert result["server"]["digest_check"] == "not_checked"
    before = len(world.calls)
    with pytest.raises(InstallError):
        server.run(ctx)
    with pytest.raises(InstallError):
        server.remove(ctx)
    assert all(
        Path(call[2]).name in {"true", "sha256sum", "stat"}
        for call in world.calls[before:]
    )


@pytest.mark.parametrize("operation", ["resume", "remove"])
@pytest.mark.parametrize(
    "path", ["/usr/local/bin/garden", "/var/lib/garden/.garden/config.yaml"]
)
def test_hashed_symlink_refuses_resume_and_remove(
    managed: Any, monkeypatch: pytest.MonkeyPatch, operation: str, path: str
) -> None:
    ctx: Context
    ctx, world, _, _ = managed
    server.run(ctx)
    command, check_path, lstat = ctx.command, server._check_path, Path.lstat

    def symlink_command(args: Any, **kwargs: Any) -> str:
        if Path(args[2]).name == "stat" and args[-2] == path:
            world.calls.append(list(args))
            assert list(args) in sudo.policy_commands(ctx)
            return "symbolic link\n" + NULL_TYPE
        return command(args, **kwargs)

    def private_destination(target: Path, **kwargs: Any) -> Any:
        if str(target) == path:
            raise PermissionError("root-private destination")
        return lstat(target, **kwargs)

    monkeypatch.setattr(ctx, "command", symlink_command)
    if path.startswith("/var/lib/garden/"):
        monkeypatch.setattr(Path, "lstat", private_destination)
        monkeypatch.setattr(
            server,
            "_check_path",
            lambda target: (
                CHECK_PATH(target) if str(target) == path else check_path(target)
            ),
        )
        assert server._check_path(Path(path))
    before = json.loads(json.dumps(ctx.state["server"]))
    files = dict(world.files)
    calls = len(world.calls)
    with pytest.raises(InstallError, match="exists but is not a regular file"):
        if operation == "resume":
            server.run(ctx)
        else:
            server.remove(ctx)
    assert ctx.state["server"] == before
    assert json.loads((ctx.directory / "state.json").read_bytes())["server"] == before
    assert world.files == files
    assert all(
        Path(call[2]).name in {"true", "sha256sum", "stat"}
        for call in world.calls[calls:]
    )


def test_external_contract_pins_and_admin_permissions(managed: Any) -> None:
    ctx, world, _, _ = managed
    token = server.external_ready(ctx)
    assert token == world.authority.admin
    world.changed_contract = True
    with pytest.raises(InstallError, match="digests changed"):
        server.external_ready(ctx)
    world.changed_contract = False
    path = Path(ctx.state["server"]["cairn"]["admin_token_file"])
    path.chmod(0o644)
    with pytest.raises(InstallError, match="0400 or 0600"):
        server.external_ready(ctx)
    assert token not in (ctx.directory / "state.json").read_text()


def test_memory_digest_pins_refuse_change(managed: Any) -> None:
    ctx, world, _, _ = managed
    server.run(ctx)
    ctx.state["server"]["memory_contracts"]["contract_digest"] = "e" * 64
    with pytest.raises(InstallError, match="memory contract digests changed"):
        garden.enrol(ctx, world.authority.admin)


def test_negative_controls_delete_data_no_hook_and_kubernetes(managed: Any) -> None:
    ctx, world, _, _ = managed
    with pytest.raises(InstallError, match="delete-data"):
        server.remove(ctx, delete_data=True)
    ctx.state["server"]["backup_hook"] = None
    with pytest.raises(InstallError, match="requires backup_hook"):
        server.backup(ctx)
    with pytest.raises(InstallError, match="requires backup_hook"):
        sudo.run(ctx, "backup")
    ctx.state["mode"] = "kubernetes"
    for action in (server.run, server.status, server.backup, server.remove):
        with pytest.raises(InstallError, match="garden-server"):
            action(ctx)
    assert not world.calls


@pytest.mark.parametrize(
    "key,params",
    [
        ("shell", {"argv": ["/bin/sh"]}),
        ("probe", {"argv": []}),
        ("file", {"path": "/etc/passwd"}),
        ("rm", {"path": "/var/lib/garden"}),
        ("unit", {"action": "restart", "unit": "garden-mcp.service"}),
        ("unit", {"action": "start", "unit": "sshd.service"}),
        ("dir_sys", {"path": "/etc"}),
        ("digest", {"paths": ["/etc/shadow"]}),
        ("stat", {"path": "/etc/shadow"}),
        ("stat", {"path": "/dev/null"}),
        ("stat", {"paths": ["/usr/local/bin/garden"]}),
    ],
)
def test_allowlist_negative_controls(
    managed: Any, key: str, params: dict[str, Any]
) -> None:
    ctx, world, _, _ = managed
    ctx.state["server"]["tools"] = {name: "/usr/bin/" + name for name in sudo.TOOLS}
    with pytest.raises(InstallError):
        sudo.run(ctx, key, **params)
    assert not world.calls


def test_denied_digest_is_not_absence(managed: Any) -> None:
    ctx, world, _, _ = managed
    world.files["/usr/local/bin/garden"] = b"foreign"
    world.digest_denied = True
    with pytest.raises(InstallError):
        server.run(ctx)
    assert world.files["/usr/local/bin/garden"] == b"foreign"
    assert not any(
        Path(call[2]).name == "install"
        for call in world.calls
        if call[0].endswith("/sudo")
    )


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_local_realm_byte_identity(
    managed: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    ctx, world, _, _ = managed
    # The existing deterministic administration renderer must emit the exact
    # same local JSON regardless of an unrelated external server state entry.
    ctx.state["mode"] = mode
    assert garden.garden_realm(ctx) == "local"
    expected = '{"kind":"workload","label":"garden-val","realm_id":"local"}'
    assert (
        garden._json(
            {
                "realm_id": garden.garden_realm(ctx),
                "kind": "workload",
                "label": "garden-val",
            }
        )
        == expected
    )


@pytest.mark.parametrize(
    "flag",
    [
        ["--port", "8100"],
        ["--semantic"],
        ["--kube-context", "example-context"],
        ["--kube-preloaded-image"],
        ["--falkordb-runtime", "/some/runtime.json"],
        ["--provider-key-file", "/some/key"],
    ],
)
def test_cli_refuses_irrelevant_options(
    tmp_path: Path, flag: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "garden-server",
                "--name",
                "system",
                "--state-root",
                str(tmp_path / "state"),
                "--non-interactive",
                *flag,
            ],
            default_source=ROOT,
        )
        == 2
    )
    assert "garden-server refuses" in capsys.readouterr().err


def test_cli_install_resume_status_remove_and_refuse_upgrade(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, world, path, config_path = managed
    # Use a different name so install wiring creates the record; no operation
    # reaches a real runner. Direct lifecycle tests above cover its full flow.
    monkeypatch.setattr(
        server,
        "run",
        lambda context: {"status": "verified", "instance_id": context.instance_id},
    )
    args = [
        "--name",
        "wired",
        "--state-root",
        str(ctx.directory.parent),
        "--non-interactive",
    ]
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "garden-server",
                "--server-config",
                str(config_path),
                "--garden-config",
                str(path),
                *args,
            ],
            default_source=ROOT,
        )
        == 0
    )
    assert cli.main(["resume", *args], default_source=ROOT) == 0
    assert cli.main(["status", *args]) == 0
    assert cli.main(["garden-upgrade", *args]) == 2
    assert cli.main(["garden-remove", "--delete-data", "--yes", *args]) == 2
    monkeypatch.setattr(server, "check_remove", lambda context, **kw: None)
    monkeypatch.setattr(
        server, "remove", lambda context, **kw: {"status": "rolled_back"}
    )
    assert cli.main(["garden-remove", "--yes", *args]) == 0


def test_workflow_uses_server_status(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, _, _ = managed
    result = {"server": {"account": "garden"}}
    monkeypatch.setattr(server, "status", lambda context: result)
    assert workflow.status_install(ctx) is result


@pytest.mark.parametrize("installed", [False, True])
def test_cli_server_status_is_read_only(
    managed: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: bool,
) -> None:
    ctx, _, _, _ = managed
    if installed:
        server.run(ctx)
    ctx.__exit__(None, None, None)
    capsys.readouterr()
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ctx.directory.rglob("*")
        if path.is_file()
    }

    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Status must not save state or run logged commands")

    monkeypatch.setattr(Context, "save", refuse)
    monkeypatch.setattr(Context, "command", refuse)
    assert (
        cli.main(
            [
                "status",
                "--name",
                ctx.name,
                "--state-root",
                str(ctx.directory.parent),
                "--non-interactive",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    result = json.loads(output[output.index("{") :])
    assert result["mode"] == "garden-server"
    assert result["features"] == "Garden server"
    assert result["instance_id"] == ctx.instance_id
    assert result["endpoint"] == ctx.state["server"]["cairn"]["url"]
    assert result["credential_file"] == ctx.state["server"]["cairn"]["admin_token_file"]
    assert result["garden"]["origin"] == "server"
    assert result["server"]["digest_check"] == (
        "not_checked" if installed else "not_installed"
    )
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ctx.directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "non_interactive,yes,answer,code",
    [
        (True, True, "", 0),
        (False, True, "", 0),
        (False, False, "yes", 0),
        (False, False, "no", 2),
        (True, False, "", 2),
    ],
)
def test_cli_server_backup_honours_confirmation(
    managed: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    non_interactive: bool,
    yes: bool,
    answer: str,
    code: int,
) -> None:
    ctx, world, _, _ = managed
    server.run(ctx)
    ctx.__exit__(None, None, None)
    capsys.readouterr()
    world.calls.clear()
    prompts = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(
        Context, "command", lambda context, argv, **kw: world.command(argv, **kw)
    )
    args = [
        "garden-backup",
        "--name",
        ctx.name,
        "--state-root",
        str(ctx.directory.parent),
        "--verbose",
    ]
    if yes:
        args.append("--yes")
    if non_interactive:
        args.append("--non-interactive")
    assert cli.main(args) == code
    output = capsys.readouterr()
    assert "Garden will be unavailable" in output.out
    assert bool(prompts) == (not non_interactive and not yes)
    if code == 0:
        assert world.calls[-1] == sudo.argv(ctx, "backup")
        assert world.calls.count(sudo.argv(ctx, "backup")) == 1
        assert ctx.state["server"]["backup_hook"]["export_dir"] in output.out
    else:
        assert "[confirmation_required]" in output.err
        assert not world.calls
    assert not (ctx.directory / "garden-backups").exists()


@pytest.mark.parametrize("refusal", ["no_hook", "output"])
def test_cli_server_backup_rejects_arguments_before_writable_context(
    managed: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
) -> None:
    ctx, world, _, _ = managed
    server.run(ctx)
    if refusal == "no_hook":
        ctx.state["server"]["backup_hook"] = None
        ctx.save()
    ctx.__exit__(None, None, None)
    capsys.readouterr()
    world.calls.clear()
    path = ctx.directory / "state.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid server backup arguments must not open writable state")

    monkeypatch.setattr(garden_backup, "open_context", refuse)
    args = [
        "garden-backup",
        "--name",
        ctx.name,
        "--state-root",
        str(ctx.directory.parent),
        "--non-interactive",
    ]
    output = ctx.directory / "unwanted-backups"
    if refusal == "output":
        args.extend(["--yes", "--output", str(output)])
    assert cli.main(args) == 2
    error = capsys.readouterr().err
    assert "[invalid_arguments]" in error
    assert ("requires backup_hook" if refusal == "no_hook" else "--output") in error
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    assert not output.exists()
    assert not world.calls


def test_custom_account_manifest_matches_final_units(managed: Any) -> None:
    ctx, world, _, _ = managed
    ctx.state["server"]["account"] = "garden-test"
    server.run(ctx)
    manifest = json.loads(world.files["/usr/local/share/garden/install-manifest.json"])
    for unit in sudo.UNITS:
        path = "etc/systemd/system/" + unit
        assert b"User=garden-test\n" in world.files["/" + path]
        assert (
            manifest["files"][path]
            == hashlib.sha256(world.files["/" + path]).hexdigest()
        )


@pytest.mark.parametrize(
    "point", ["payload", "manifest", "unit", "transformed_manifest"]
)
@pytest.mark.parametrize("published", [False, True])
def test_resume_during_stage_publication(
    managed: Any, monkeypatch: pytest.MonkeyPatch, point: str, published: bool
) -> None:
    world: World
    ctx, world, _, _ = managed
    ctx.state["server"]["account"] = "garden-test"
    # Exercise install-server's real ownership checks and publication order,
    # keeping the Go build and all privileged operations in the fake world.
    installer = runpy.run_path(str(ctx.source / "garden/scripts/install-user"))
    install_main = installer["main"]
    install_write = install_main.__globals__["atomic_write"]
    transformed_write = atomic_write
    armed = True
    root = sudo.stage_root(ctx)
    relative = {
        "payload": "usr/local/bin/garden",
        "manifest": "usr/local/share/garden/install-manifest.json",
        "unit": "etc/systemd/system/garden-daemon.service",
        "transformed_manifest": "usr/local/share/garden/install-manifest.json",
    }[point]

    def write(path: Path, data: bytes, mode: int, *, transforming: bool) -> None:
        nonlocal armed
        hit = (
            armed
            and path == root / relative
            and transforming == (point in {"unit", "transformed_manifest"})
        )
        if hit:
            armed = False
            if not published:
                raise KeyboardInterrupt("before staging publication")
        (transformed_write if transforming else install_write)(path, data, mode)
        if hit:
            raise KeyboardInterrupt("after staging publication")

    monkeypatch.setitem(
        install_main.__globals__,
        "atomic_write",
        lambda path, data, mode: write(path, data, mode, transforming=False),
    )
    monkeypatch.setattr(
        server,
        "atomic_write",
        lambda path, data, mode=0o600: write(path, data, mode, transforming=True),
    )

    def command(args: Any, **kwargs: Any) -> str:
        if str(args[0]) == str(ctx.source / "garden/scripts/install-server"):
            monkeypatch.setattr(sys, "argv", list(map(str, args)))
            if install_main("server") != 0:
                raise InstallError("install-server failed")
            return ""
        return world.command(args, **kwargs)

    monkeypatch.setattr(ctx, "command", command)
    with pytest.raises(KeyboardInterrupt):
        server.run(ctx)
    assert not armed
    assert ctx.state["server"]["staging"]
    assert "staged" not in ctx.state["server"]
    assert not world.files and not world.accounts
    # Reload the journal, as a new invocation would, before resuming.
    ctx.state = json.loads((ctx.directory / "state.json").read_text())
    assert server.run(ctx)["status"] == "verified"
    manifest = json.loads(world.files["/usr/local/share/garden/install-manifest.json"])
    for name in sudo.UNITS:
        path = "etc/systemd/system/" + name
        assert b"User=garden-test\n" in world.files["/" + path]
        assert (
            manifest["files"][path]
            == hashlib.sha256(world.files["/" + path]).hexdigest()
        )


@pytest.mark.parametrize("kind", ["unrecorded", "symlink", "public", "pinned"])
def test_stage_rebuild_preserves_ownership_boundaries(managed: Any, kind: str) -> None:
    ctx, world, _, _ = managed
    root = sudo.stage_root(ctx)
    if kind == "pinned":
        server.run(ctx)
        target = root / "usr/local/bin/garden"
        target.write_bytes(b"changed pinned payload")
    else:
        target = ctx.root / "foreign-stage" if kind == "symlink" else root
        target.mkdir(mode=0o755 if kind == "public" else 0o700)
        target = target / "foreign"
        target.write_bytes(b"foreign")
        if kind == "symlink":
            root.symlink_to(target.parent, target_is_directory=True)
        if kind != "unrecorded":
            ctx.state["server"]["staging"] = True
    before = target.read_bytes()
    calls = len(world.calls)
    with pytest.raises(InstallError):
        server.prepare(ctx)
    assert target.read_bytes() == before
    assert len(world.calls) == calls


@pytest.mark.parametrize(
    "path,private",
    [
        ("/var/lib/garden/.garden", True),
        ("/etc/garden/tls", True),
        ("/opt/private/exports", False),
    ],
)
def test_directory_inspection_permission_boundary(
    monkeypatch: pytest.MonkeyPatch, path: str, private: bool
) -> None:
    target = Path(path)
    inspected = []

    def lstat(component: Path) -> Any:
        inspected.append(component)
        if component == target:
            raise PermissionError("private directory")
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)

    monkeypatch.setattr(Path, "lstat", lstat)
    if private:
        assert server._check_directory(target)
    else:
        with pytest.raises(InstallError, match="Cannot inspect"):
            server._check_directory(target)
    assert inspected == [*reversed(target.parents), target]


def test_symlink_destinations_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("foreign")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(InstallError, match="regular file"):
        server._check_path(link)


@pytest.mark.parametrize("operation", ["blitz", "rollback", "garden-upgrade"])
def test_interactive_recovery_refuses_server_after_name_prompt(
    managed: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    ctx, world, _, _ = managed
    answers = iter([ctx.name])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert cli.main([operation, "--state-root", str(ctx.directory.parent)]) == 2
    assert not world.calls


def test_remove_after_no_adoption_refusal_does_not_touch_foreign_units(
    managed: Any,
) -> None:
    ctx, world, _, _ = managed
    world.files["/etc/systemd/system/garden-mcp.service"] = b"foreign unit"
    with pytest.raises(InstallError, match="adopt"):
        server.run(ctx)
    before = len(world.calls)
    server.remove(ctx)
    assert world.calls[before:] == []
    assert world.files["/etc/systemd/system/garden-mcp.service"] == b"foreign unit"


def test_optional_hook_and_requires_are_not_installed(managed: Any) -> None:
    ctx, world, _, _ = managed
    ctx.state["server"].update(requires=[], backup_hook=None)
    server.run(ctx)
    assert sudo.BACKUP_SCRIPT not in world.files
    assert "/usr/local/sbin/garden-backup" not in world.files
    assert "/etc/systemd/system/garden-mcp.service.d/requires.conf" not in world.files
    assert ctx.state["steps"]["server_hook"] == "complete"


def test_backup_script_is_installed_under_bin_not_sbin(managed: Any) -> None:
    # /usr/local/sbin is a symlink to bin on Fedora 42+, and garden-server refuses
    # symlinked ancestors. The backup path must therefore sit under /usr/local/bin.
    ctx, world, _, _ = managed
    server.run(ctx)
    assert sudo.BACKUP_SCRIPT == "/usr/local/bin/garden-backup"
    assert world.files[sudo.BACKUP_SCRIPT].startswith(b"#!/bin/bash\n")
    assert "/usr/local/sbin/garden-backup" not in world.files
    unit = world.files["/etc/systemd/system/garden-backup.service"]
    assert b"ExecStart=/usr/local/bin/garden-backup\n" in unit
    assert b"sbin" not in unit
    stage = sudo.stage_root(ctx)
    assert (stage / "usr/local/bin/garden-backup").is_file()
    assert not (stage / "usr/local/sbin/garden-backup").exists()
    assert "/usr/local/bin/garden-backup" in sudo.snippet(ctx)
    assert "/usr/local/sbin/garden-backup" not in sudo.snippet(ctx)
    server.remove(ctx)
    assert sudo.BACKUP_SCRIPT not in world.files
    assert [
        call
        for call in world.calls
        if call[0].endswith("/sudo") and call[-1] == sudo.BACKUP_SCRIPT
    ]


def test_preflight_refuses_runbook_backup_before_any_mutation(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, world, _, _ = managed
    runbook = tmp_path / "sbin" / "garden-backup"
    runbook.parent.mkdir()
    runbook.write_text("#!/bin/bash\n")
    monkeypatch.setattr(server, "RUNBOOK_BACKUP_SCRIPT", str(runbook))
    with pytest.raises(InstallError, match="Retire the manual Garden backup first"):
        server.preflight(ctx)
    # Only the read-only sudo probe runs before the refusal.
    assert world.calls == [["/usr/bin/sudo", "-n", "/usr/bin/true"]]
    assert not world.files
    # Without a hook the installer never writes a backup script, so nothing to protect.
    ctx.state["server"]["backup_hook"] = None
    server.preflight(ctx)


@pytest.mark.parametrize("layout", ["missing_parent", "absent_file", "file_parent"])
def test_runbook_backup_check_tolerates_absent_or_odd_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    parent = tmp_path / "sbin"
    if layout == "absent_file":
        parent.mkdir()
    elif layout == "file_parent":
        parent.write_text("not a directory")
    monkeypatch.setattr(server, "RUNBOOK_BACKUP_SCRIPT", str(parent / "garden-backup"))
    server._refuse_runbook_backup()


def test_runbook_backup_check_follows_symlinked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "sbin").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(
        server, "RUNBOOK_BACKUP_SCRIPT", str(tmp_path / "sbin/garden-backup")
    )
    server._refuse_runbook_backup()
    (real / "garden-backup").write_text("#!/bin/bash\n")
    with pytest.raises(InstallError, match="Retire the manual Garden backup first"):
        server._refuse_runbook_backup()


def test_runbook_backup_resolving_to_installed_path_is_not_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Fedora the sbin name resolves to bin; then it is our own destination,
    # and the ordinary digest check in preflight governs it instead.
    installed = tmp_path / "bin" / "garden-backup"
    installed.parent.mkdir()
    installed.write_text("#!/bin/bash\n")
    (tmp_path / "sbin").symlink_to(installed.parent, target_is_directory=True)
    monkeypatch.setattr(
        server, "RUNBOOK_BACKUP_SCRIPT", str(tmp_path / "sbin/garden-backup")
    )
    monkeypatch.setattr(sudo, "BACKUP_SCRIPT", str(installed))
    server._refuse_runbook_backup()


@pytest.mark.parametrize("refusal", ["root", "relative_tool", "realm"])
def test_preflight_refusals_precede_any_mutation(
    managed: Any, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    ctx, world, _, _ = managed
    if refusal == "root":
        monkeypatch.setattr(os, "geteuid", lambda: 0)
    elif refusal == "relative_tool":
        monkeypatch.setattr(server, "_system_tool", SYSTEM_TOOL)
        monkeypatch.setattr(shutil, "which", lambda name, **kwargs: name)
    else:
        ctx.state["garden"]["options"]["scope"]["realm"] = "other"
    with pytest.raises(InstallError):
        server.preflight(ctx)
    assert not any(
        Path(call[2]).name in {"install", "groupadd", "useradd", "systemctl"}
        for call in world.calls
    )


def test_preflight_refuses_unsafe_sudo_before_any_command(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, world, _, _ = managed

    def resolve(name: str) -> str:
        assert name == "sudo"
        raise InstallError("Unsafe system executable path: sudo")

    monkeypatch.setattr(server, "_system_tool", resolve)
    with pytest.raises(InstallError, match="Unsafe system executable"):
        server.preflight(ctx)
    assert not world.calls


def test_status_reports_unavailable_digest_checks(managed: Any) -> None:
    ctx, world, _, _ = managed
    server.run(ctx)
    world.digest_denied = True
    assert server.status(ctx)["server"]["digest_check"] == "not_checked"


def probe_runner(
    ctx: Context,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    *,
    code: int = 0,
    stat_output: str = NULL_TYPE,
    stat_code: int = 1,
) -> None:
    runner = ctx.root / "fake-root-runner"
    responses = {"sha256sum": (output, code), "stat": (stat_output, stat_code)}
    atomic_write(
        runner,
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            f"responses = {responses!r}\n"
            "output, code = responses[sys.argv[2].rsplit('/', 1)[-1]]\n"
            "sys.stdout.write(output)\n"
            "sys.exit(code)\n"
        ).encode(),
        0o700,
    )
    ctx.state["server"]["tools"] = {name: "/usr/bin/" + name for name in sudo.TOOLS}
    ctx.state["server"]["tools"]["sudo"] = str(runner)
    monkeypatch.setattr(ctx, "command", Context.command.__get__(ctx, Context))


@pytest.mark.parametrize("missing", [(), ("/etc/garden/garden.json",)])
def test_real_runner_digest_present_and_missing(
    managed: Any, monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...]
) -> None:
    ctx, world, _, _ = managed
    paths = ("/usr/local/bin/garden", "/etc/garden/garden.json")
    lstat = Path.lstat

    def private_destination(path: Path, **kwargs: Any) -> Any:
        if str(path) in paths:
            raise PermissionError("root-private destination")
        return lstat(path, **kwargs)

    monkeypatch.setattr(Path, "lstat", private_destination)
    for path in paths:
        output = "" if path in missing else "a" * 64 + "  " + path + "\n"
        probe_runner(
            ctx,
            monkeypatch,
            output + NULL_DIGEST,
            code=int(path in missing),
            stat_output=("" if path in missing else "regular file\n") + NULL_TYPE,
            stat_code=int(path in missing),
        )
        assert sudo.digest(ctx, path) == (None if path in missing else "a" * 64)
    assert not world.calls


@pytest.mark.parametrize("kind", ["regular file", "regular empty file"])
def test_real_runner_hashed_regular_file_accepted(
    managed: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ctx, _, _, _ = managed
    path = "/usr/local/bin/garden"
    digest = hashlib.sha256(b"").hexdigest()
    probe_runner(
        ctx,
        monkeypatch,
        digest + "  " + path + "\n" + NULL_DIGEST,
        stat_output=kind + "\n" + NULL_TYPE,
        stat_code=0,
    )
    assert sudo.digest(ctx, path) == digest


def test_real_runner_hash_without_stat_path_refused(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, _, _ = managed
    path = "/usr/local/bin/garden"
    probe_runner(ctx, monkeypatch, "a" * 64 + "  " + path + "\n" + NULL_DIGEST)
    with pytest.raises(InstallError, match="Invalid sudo stat response.*inconsistent"):
        sudo.digest(ctx, path)


@pytest.mark.parametrize("kind", ["symbolic link", "directory", "fifo"])
def test_real_runner_hashed_non_regular_file_refused(
    managed: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ctx, _, _, _ = managed
    path = "/usr/local/bin/garden"
    probe_runner(
        ctx,
        monkeypatch,
        "a" * 64 + "  " + path + "\n" + NULL_DIGEST,
        stat_output=kind + "\n" + NULL_TYPE,
        stat_code=0,
    )
    with pytest.raises(InstallError, match="exists but is not a regular file"):
        sudo.digest(ctx, path)


@pytest.mark.parametrize(
    "output",
    [
        "a" * 64 + " /usr/local/bin/garden\n" + NULL_DIGEST,
        "a" * 63 + "  /usr/local/bin/garden\n" + NULL_DIGEST,
        "g" * 64 + "  /usr/local/bin/garden\n" + NULL_DIGEST,
        "a" * 64 + "  /etc/garden/garden.json\n" + NULL_DIGEST,
        "a" * 64 + "  /usr/local/bin/garden\n\n" + NULL_DIGEST,
        ("a" * 64 + "  /usr/local/bin/garden\n") * 2 + NULL_DIGEST,
        "a" * 64 + "  /usr/local/bin/garden\n",
        NULL_DIGEST.replace("e3b0", "ffff"),
        NULL_DIGEST.replace("  /dev/null", " /dev/null"),
        NULL_DIGEST + "unexpected\n",
        NULL_DIGEST * 2,
        NULL_DIGEST + "\n",
    ],
)
def test_real_runner_digest_refuses_invalid_output(
    managed: Any, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    ctx, world, _, _ = managed
    probe_runner(ctx, monkeypatch, output)
    with pytest.raises(InstallError, match="Invalid sudo digest response"):
        sudo.digest(ctx, "/usr/local/bin/garden")
    assert not world.calls


@pytest.mark.parametrize("probe", ["digest", "stat"])
def test_real_runner_denied_probe_never_means_missing(
    managed: Any, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    ctx, world, _, _ = managed
    probe_runner(
        ctx,
        monkeypatch,
        "" if probe == "digest" else NULL_DIGEST,
        code=1,
        stat_output="",
        stat_code=1,
    )
    with pytest.raises(InstallError, match=f"Invalid sudo {probe} response.*sentinel"):
        sudo.digest(ctx, "/usr/local/bin/garden")
    assert not world.calls


@pytest.mark.parametrize("kind", ["directory", "symbolic link", "regular file"])
def test_real_runner_undigestible_destination_is_not_absent(
    managed: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ctx, _, _, _ = managed
    probe_runner(
        ctx,
        monkeypatch,
        NULL_DIGEST,
        code=1,
        stat_output=kind + "\n" + NULL_TYPE,
        stat_code=0,
    )
    with pytest.raises(InstallError, match="exists but is not a hashable regular file"):
        sudo.digest(ctx, "/usr/local/bin/garden")


@pytest.mark.parametrize(
    "output",
    [
        "",
        "character special fil\n",
        NULL_TYPE + "unexpected\n",
        "\n" + NULL_TYPE,
        "directory\nsymbolic link\n" + NULL_TYPE,
        NULL_TYPE + "\n",
    ],
)
@pytest.mark.parametrize("hashed", [False, True])
def test_real_runner_stat_refuses_invalid_output(
    managed: Any, monkeypatch: pytest.MonkeyPatch, output: str, hashed: bool
) -> None:
    ctx, _, _, _ = managed
    path = "/usr/local/bin/garden"
    probe_runner(
        ctx,
        monkeypatch,
        ("a" * 64 + "  " + path + "\n" if hashed else "") + NULL_DIGEST,
        code=int(not hashed),
        stat_output=output,
    )
    with pytest.raises(InstallError, match="Invalid sudo stat response"):
        sudo.digest(ctx, path)


@pytest.mark.parametrize("probe", ["digest", "stat"])
@pytest.mark.parametrize("output", ["", NULL_DIGEST])
def test_real_runner_failed_probe_never_means_missing(
    managed: Any, monkeypatch: pytest.MonkeyPatch, probe: str, output: str
) -> None:
    ctx, _, _, _ = managed
    probe_runner(
        ctx,
        monkeypatch,
        output if probe == "digest" else NULL_DIGEST,
        code=2 if probe == "digest" else 1,
        stat_output=output,
        stat_code=2,
    )
    with pytest.raises(InstallError, match="exit 2"):
        sudo.digest(ctx, "/usr/local/bin/garden")


@pytest.mark.parametrize("operation", ["publish", "remove"])
@pytest.mark.parametrize("probe", ["digest", "stat"])
def test_denied_probe_preserves_files_and_ownership(
    managed: Any, monkeypatch: pytest.MonkeyPatch, operation: str, probe: str
) -> None:
    ctx, world, _, _ = managed
    path = "/usr/local/bin/garden"
    if operation == "remove":
        server.run(ctx)
    else:
        world.files[path] = b"foreign"
        ctx.state["server"]["staged"] = {path: hashlib.sha256(b"owned").hexdigest()}
    probe_runner(
        ctx,
        monkeypatch,
        "" if probe == "digest" else NULL_DIGEST,
        code=1,
        stat_output="",
        stat_code=1,
    )
    ctx.save()
    before = json.loads(json.dumps(ctx.state))
    persisted = (ctx.directory / "state.json").read_bytes()
    files = dict(world.files)
    with pytest.raises(InstallError, match=f"Invalid sudo {probe} response.*sentinel"):
        if operation == "publish":
            server._publish(ctx, [path])
        else:
            server.remove(ctx)
    assert ctx.state == before
    assert (ctx.directory / "state.json").read_bytes() == persisted
    assert world.files == files


def test_cancelled_digest_does_not_become_absence(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, world, _, _ = managed
    ctx.state["server"]["tools"] = {name: "/usr/bin/" + name for name in sudo.TOOLS}

    def interrupted(*args: Any, **kwargs: Any) -> str:
        raise InstallError("cancelled", "interrupted")

    monkeypatch.setattr(ctx, "command", interrupted)
    with pytest.raises(InstallError, match="cancelled") as failure:
        sudo.digest(ctx, "/usr/local/bin/garden")
    assert failure.value.code == "interrupted"
    with pytest.raises(InstallError, match="allowlist"):
        sudo.digest(ctx, "/not-in-the-table")
    assert not world.calls


@pytest.mark.parametrize("url", ["http://127.0.0.1:8100", "http://[::1]:8100"])
def test_external_transport_uses_exact_loopback_binding_without_proxy(
    managed: Any, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    ctx, world, _, _ = managed
    ctx.state["server"]["cairn"]["url"] = url
    seen = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, limit: int) -> bytes:
            return b'{"ok":true}'

    def open_request(request: Any, *, timeout: int) -> Response:
        assert timeout == 10
        seen.append(request)
        return Response()

    def opener(*handlers: Any) -> Any:
        assert handlers[0].proxies == {}
        assert type(handlers[1]).__name__ == "NoRedirect"
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(server, "build_opener", opener)
    assert EXTERNAL_REQUEST(
        ctx, "/v1/create-principal", world.authority.admin, data=b"{}", key="key"
    ) == {"ok": True}
    assert seen[0].full_url == url + "/v1/create-principal"
    assert seen[0].get_header("Authorization") == "Bearer " + world.authority.admin
    assert seen[0].get_header("Idempotency-key") == "key"
    with pytest.raises(InstallError, match="route"):
        EXTERNAL_REQUEST(ctx, "//foreign.example/path")
