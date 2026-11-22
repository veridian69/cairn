"""garden-upgrade for garden-server against fake root files and Garden data."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import cli, garden, garden_backup, sudo, workflow
from cairn_install import garden_server as server
from cairn_install import garden_server_upgrade as server_upgrade
from cairn_install import garden_upgrade as upgrade
from cairn_install.core import Context, InstallError, atomic_write, open_context

from .test_garden import configuration as configuration
from .test_garden_server import ROOT, World
from .test_garden_server import managed as managed

DATA, ASIDE, RESTORED = sudo.DATA_PATHS
EXTRA = sudo.DEPLOY + "extra.example.json"
TARGET_VERSION = "99.0.0"
PHASE = upgrade._phase
LINK, RENAME = os.link, os.rename


class Fake:
    """Wraps the S11 World: adds the Garden account's data and the build trees."""

    def __init__(
        self, ctx: Context, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.world = world
        self.trees: dict[str, dict[str, bytes]] = {DATA: {"state.db": b"M1"}}
        self.events: list[str] = []
        self.active: set[str] = set()
        self.enabled: set[str] = set()
        self.backup_state = "inactive"
        self.granted: list[list[str]] | None = None
        self.armed: str | None = None
        self.builds = 0
        self.member_uid = 950
        self.free = 10**12
        self.generation = "generation-1"
        self.delegate = world.command
        # The install context and every later upgrade context use this runner.
        monkeypatch.setattr(
            Context,
            "command",
            lambda context, argv, **kw: self.command(context, argv, **kw),
        )
        monkeypatch.setattr(
            ctx, "command", lambda argv, **kw: self.command(ctx, argv, **kw)
        )
        monkeypatch.setattr(garden, "verify", self.verify)
        monkeypatch.setattr(
            shutil, "disk_usage", lambda path: SimpleNamespace(free=self.free)
        )

        def phase(ctx: Context, journal: dict[str, Any], name: str) -> None:
            PHASE(ctx, journal, name)
            self.hit("phase:" + name)

        monkeypatch.setattr(upgrade, "_phase", phase)

    def hit(self, point: str) -> None:
        if self.armed == point:
            self.armed = None
            raise KeyboardInterrupt(f"injected {point}")

    def verify(self, ctx: Context) -> None:
        assert self.active == set(sudo.UNITS), "Garden must run while verified"
        ctx.state["garden"]["verification"] = {
            name: {"generation": self.generation}
            for name in ctx.state["garden"]["options"]["participants"]
        }
        ctx.save()

    def stage_release(self, source: Path, root: Path, binary: Path) -> None:
        files = {
            f"usr/local/bin/{name}": (source / "garden/scripts" / name).read_bytes()
            for name in ("garden-config", "garden-session")
        }
        files["usr/local/bin/garden"] = binary.read_bytes()
        for path in sorted((source / "garden/deploy").iterdir()):
            files["usr/local/share/garden/deploy/" + path.name] = path.read_bytes()
        for unit in sudo.UNITS:
            files["etc/systemd/system/" + unit] = (
                source / "garden/deploy" / unit
            ).read_bytes()
        for relative, data in files.items():
            atomic_write(root / relative, data, 0o755 if "/bin/" in relative else 0o644)
        atomic_write(
            root / "var/lib/garden/.garden/config.yaml",
            (source / "garden/deploy/daemon.yaml").read_bytes(),
        )
        manifest = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
        atomic_write(
            root / sudo.MANIFEST.lstrip("/"),
            json.dumps({"version": 1, "kind": "server", "files": manifest}).encode(),
            0o644,
        )

    def archive(self, tree: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            root = tarfile.TarInfo("data")
            root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o700, 950, 950
            archive.addfile(root)
            for name, data in sorted(tree.items()):
                member = tarfile.TarInfo("data/" + name)
                member.size, member.uid, member.gid = len(data), self.member_uid, 950
                archive.addfile(member, io.BytesIO(data))
        return buffer.getvalue()

    @staticmethod
    def members(path: Path) -> dict[str, bytes]:
        result = {}
        with tarfile.open(path) as archive:
            for member in archive:
                if member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    result[member.name.removeprefix("data/")] = stream.read()
        return result

    def data(self, argv: list[str], kw: dict[str, Any]) -> str:
        verb = Path(argv[4]).name
        key = next(
            name
            for name in sudo._DATA_KEYS
            if sudo._data_command(self.world.ctx.state["server"]["tools"], name)
            == argv[4:]
        )
        self.events.append(key)
        assert kw["cwd"] == Path("/") and verb
        if key == "data_size":
            return f"{sum(map(len, self.trees[DATA].values()))}\t{DATA}\n"
        if key == "data_take":
            assert not self.active
            Path(kw["stdout_path"]).write_bytes(self.archive(self.trees[DATA]))
        elif key == "data_probe":
            return "".join(f"{p}\n" for p in sudo.DATA_PATHS if p in self.trees) + (
                "/dev/null\n"
            )
        elif key == "data_extract":
            assert not self.active
            self.trees[RESTORED] = self.members(kw["stdin_path"])
        elif key == "data_compare":
            if self.trees.get(RESTORED) != self.members(kw["stdin_path"]):
                raise InstallError("tar --compare exit 1")
        elif key == "data_aside":
            assert ASIDE not in self.trees
            self.trees[ASIDE] = self.trees.pop(DATA)
        elif key == "data_place":
            assert DATA not in self.trees
            self.trees[DATA] = self.trees.pop(RESTORED)
        self.hit(key)
        return ""

    def command(self, ctx: Context, args: Any, **kw: Any) -> str:
        argv = [str(arg) for arg in args]
        self.world.ctx = ctx
        if argv[:2] == ["go", "build"]:
            self.builds += 1
            version = argv[argv.index("-ldflags") + 1].rsplit("=", 1)[1]
            atomic_write(
                Path(argv[argv.index("-o") + 1]), b"garden " + version.encode(), 0o755
            )
            return ""
        if argv[0].endswith("garden/scripts/install-server"):
            self.stage_release(Path(argv[0]).parents[2], Path(argv[2]), Path(argv[4]))
            return ""
        if argv[1:] == ["--version"]:
            binary = (
                self.world.files[argv[0]]
                if argv[0] == server_upgrade.BINARY
                else Path(argv[0]).read_bytes()
            )
            return "garden version " + binary.decode().split(" ", 1)[1] + "\n"
        if argv[1:] == ["is-active", "garden-backup.service"]:
            self.events.append("is-active")
            return self.backup_state + "\n"
        if argv[:3] == ["/usr/bin/sudo", "-n", "-l"]:
            wanted = ["/usr/bin/sudo", "-n", *argv[3:]]
            if self.granted is not None and wanted not in self.granted:
                raise InstallError("sudo: a password is required")
            return argv[3] + "\n"
        if argv[:2] == ["/usr/bin/sudo", "-n"]:
            assert argv in sudo.policy_commands(ctx), argv
            assert "-rf" not in argv and not any("*" in arg for arg in argv)
            if argv[2] == "-u":
                assert argv[3] == "garden"
                return self.data(argv, kw)
            if Path(argv[2]).name == "systemctl" and argv[3] in {
                "start",
                "stop",
                "enable",
                "disable",
            }:
                self.events.append(f"{argv[3]} {argv[4]}")
                target = self.active if argv[3] in {"start", "stop"} else self.enabled
                (target.add if argv[3] in {"start", "enable"} else target.discard)(
                    argv[4]
                )
            elif argv[-1] == sudo.FLAG:
                self.events.append("flag " + Path(argv[2]).name)
            elif Path(argv[2]).name == "install" and "-d" not in argv:
                self.events.append("file " + argv[-1])
            elif Path(argv[2]).name == "rm":
                self.events.append("rm " + argv[-1])
            elif argv[2:] == ["/usr/bin/systemctl", "daemon-reload"]:
                self.events.append("reload")
        output = self.delegate(argv, **kw)
        if argv[-1] == sudo.FLAG and Path(argv[2]).name in {"install", "rm"}:
            self.hit("flag")
        return output


def target_source(tmp_path: Path, *, extra: bool = True) -> Path:
    source = tmp_path / "target"
    (source / "src/cairn").mkdir(parents=True)
    (source / "src/cairn/__init__.py").write_text("# Cairn\n")
    (source / "pyproject.toml").write_text(f'[project]\nversion = "{TARGET_VERSION}"\n')
    shutil.copytree(ROOT / "garden/deploy", source / "garden/deploy")
    shutil.copytree(ROOT / "garden/scripts", source / "garden/scripts")
    if extra:
        (source / "garden/deploy/extra.example.json").write_text("{}\n")
    return source


class Env:
    def __init__(self, ctx: Context, fake: Fake, source: Path) -> None:
        self.state_root = ctx.directory.parent
        self.state_path = ctx.directory / "state.json"
        self.fake = fake
        self.source = source

    def run(self, **options: Any) -> dict[str, Any]:
        rollback = options.pop("rollback", False)
        args = Namespace(
            name="system",
            non_interactive=True,
            yes=True,
            verbose=False,
            rollback=rollback,
            source=None if rollback else str(self.source),
            restore_data=options.pop("restore_data", False),
            keep_data=options.pop("keep_data", False),
        )
        assert not options
        result = upgrade.run(args, self.state_root)
        assert isinstance(result, dict)
        return result

    def state(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.state_path.read_text())
        return value

    def until_done(self, **options: Any) -> dict[str, Any]:
        for _ in range(4):
            try:
                return self.run(**options)
            except (InstallError, KeyboardInterrupt):
                continue
        pytest.fail("garden-upgrade did not converge")


@pytest.fixture
def env(managed: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    ctx, world, _, _ = managed
    fake = Fake(ctx, world, monkeypatch)
    server.run(ctx)
    assert fake.active == fake.enabled == set(sudo.UNITS)
    ctx.__exit__(None, None, None)
    fake.events.clear()
    return Env(ctx, fake, target_source(tmp_path))


def release(env: Env, key: str) -> dict[str, str]:
    files: dict[str, str] = env.state()["server"]["releases"][key]["files"]
    return files


def installed_release(env: Env) -> dict[str, str]:
    return {
        path: value
        for path, value in env.state()["server"]["installed"].items()
        if sudo.release_path(path)
    }


def test_upgrade_then_restore_rollback(env: Env) -> None:
    fake, world = env.fake, env.fake.world
    old = installed_release(env)
    result = env.run()
    state = env.state()
    key = state["garden"]["source"]["artefact"]["release_sha256"]
    assert installed_release(env) == release(env, key)
    assert world.files[server_upgrade.BINARY] == b"garden " + TARGET_VERSION.encode()
    assert EXTRA in world.files and sudo.FLAG not in world.files
    assert result["garden"]["version"] == TARGET_VERSION
    assert result["server"]["upgrade_flag"] == "clear"
    assert fake.active == fake.enabled == set(sudo.UNITS)
    # A3: the flag, then the idle hook, then the stops and the reboot guard.
    events = fake.events
    window = events[events.index("flag install") :]
    assert window[:6] == [
        "flag install",
        "is-active",
        "stop garden-mcp.service",
        "stop garden-daemon.service",
        "disable garden-mcp.service",
        "disable garden-daemon.service",
    ]
    files = [event for event in events if event.startswith("file ")]
    assert files[-1] == "file " + sudo.MANIFEST and f"file {EXTRA}" in files
    assert events.index("data_take") < events.index(files[0])
    assert events.index(files[-1]) < events.index("reload")
    assert events.index("reload") < events.index("start garden-daemon.service")
    assert events[-1] == "flag rm"
    assert "data_extract" not in events
    # The snippet now covers the target's rows, line for line.
    snippet = (env.state_root / "system/garden-server.sudoers").read_text()
    with open_context(env.state_root, "system") as ctx:
        assert snippet == sudo.snippet(ctx)
        assert sudo.argv(ctx, "file", path=EXTRA) in sudo.policy_commands(ctx)
    lines = [line for line in snippet.splitlines() if "NOPASSWD" in line]
    assert len(set(lines)) == len(lines)
    copy = Path(state["garden"]["upgrade"]["cold_copy"]["path"])
    assert Fake.members(copy) == {"state.db": b"M1"}

    fake.trees[DATA] = {"state.db": b"M2"}
    fake.events.clear()
    with pytest.raises(InstallError) as error:
        env.run(rollback=True)
    assert error.value.code == "data_choice_required"
    result = env.run(rollback=True, restore_data=True)
    assert installed_release(env) == old
    assert EXTRA not in world.files and sudo.FLAG not in world.files
    assert fake.trees[DATA] == {"state.db": b"M1"}
    assert fake.trees[ASIDE] == {"state.db": b"M2"}
    assert RESTORED not in fake.trees
    events = fake.events
    assert events.index("data_compare") < events.index("data_aside")
    assert events.index("data_place") < events.index(f"rm {EXTRA}")
    assert result["garden"]["version"] == garden.source_version(ROOT)
    state = env.state()
    assert list(state["server"]["releases"]) == [
        state["garden"]["source"]["artefact"]["release_sha256"]
    ]
    assert not state["server"].get("replacements")


@pytest.mark.parametrize(
    "point",
    [
        "phase:staged",
        "phase:checked",
        "phase:stopped",
        "flag",
        "phase:copied",
        "data_take",
        "file",
        "phase:swapped",
        "phase:started",
        "phase:verified",
    ],
)
def test_forward_interrupts_converge(env: Env, point: str) -> None:
    if point == "file":
        env.fake.world.effect_fault = "file"
    else:
        env.fake.armed = point
    with pytest.raises((InstallError, KeyboardInterrupt)):
        env.run()
    assert env.fake.armed is None and env.fake.world.effect_fault is None
    env.until_done()
    state = env.state()
    key = state["garden"]["source"]["artefact"]["release_sha256"]
    assert installed_release(env) == release(env, key)
    assert len(state["garden"]["upgrades"]) == 1
    assert sorted(
        p.name for p in (env.state_root / "system/instance/garden-upgrades").iterdir()
    ) == [state["garden"]["upgrade"]["id"]]
    assert env.fake.trees == {DATA: {"state.db": b"M1"}}
    assert sudo.FLAG not in env.fake.world.files
    assert env.fake.active == env.fake.enabled == set(sudo.UNITS)
    assert not state["server"].get("replacements")
    # Interrupts after staging never rebuild: install plus one upgrade build.
    if point not in {"phase:staged"}:
        assert env.fake.builds == 2


@pytest.mark.parametrize(
    "point", ["data_stage", "data_extract", "data_aside", "data_place", "rm", "file"]
)
def test_restore_interrupts_converge(env: Env, point: str) -> None:
    env.run()
    env.fake.trees[DATA] = {"state.db": b"M2"}
    if point in {"rm", "file"}:
        env.fake.world.effect_fault = point
    else:
        env.fake.armed = point
    with pytest.raises((InstallError, KeyboardInterrupt)):
        env.run(rollback=True, restore_data=True)
    env.until_done(rollback=True, restore_data=True)
    assert env.fake.trees == {
        DATA: {"state.db": b"M1"},
        ASIDE: {"state.db": b"M2"},
    }
    assert EXTRA not in env.fake.world.files
    assert len(env.state()["garden"]["upgrades"]) == 2


def test_restore_refuses_a_foreign_aside_before_stopping(env: Env) -> None:
    env.run()
    env.fake.trees[ASIDE] = {"state.db": b"old aside"}
    env.fake.events.clear()
    with pytest.raises(InstallError, match="not this restore's"):
        env.run(rollback=True, restore_data=True)
    assert not any(event.startswith("stop ") for event in env.fake.events)
    assert env.fake.active == set(sudo.UNITS)


def test_keep_data_rollback_keeps_current_data(env: Env) -> None:
    env.run()
    env.fake.trees[DATA] = {"state.db": b"M3"}
    env.run(rollback=True, keep_data=True)
    assert env.fake.trees == {DATA: {"state.db": b"M3"}}
    assert "data_extract" not in env.fake.events


def test_policy_denial_refuses_before_any_stop(env: Env) -> None:
    with open_context(env.state_root, "system") as ctx:
        env.fake.granted = sudo.policy_commands(ctx)
    with pytest.raises(InstallError, match="install the updated"):
        env.run()
    assert not any(event.startswith("stop ") for event in env.fake.events)
    snippet = (env.state_root / "system/garden-server.sudoers").read_text()
    assert "extra.example.json" in snippet
    assert sudo.FLAG not in env.fake.world.files
    # Negative control: once the operator grants the new snippet it proceeds.
    env.fake.granted = None
    env.run()
    assert EXTRA in env.fake.world.files


def test_old_snippet_refuses_before_the_journal(env: Env) -> None:
    env.fake.granted = []
    with pytest.raises(InstallError, match="install the updated"):
        env.run()
    assert "upgrade" not in env.state()["garden"]
    assert env.fake.builds == 1


@pytest.mark.parametrize("case", ["unguarded", "flag", "space", "missing"])
def test_refusals_before_downtime(env: Env, case: str) -> None:
    if case == "unguarded":
        # An S11 trial hook without the flag check (Sol r1 P1-2).
        with open_context(env.state_root, "system") as ctx:
            script = sudo.stage_root(ctx) / sudo.BACKUP_SCRIPT.lstrip("/")
            text = script.read_bytes().replace(sudo.FLAG.encode(), b"/nonexistent")
            atomic_write(script, text, 0o755)
            digest = hashlib.sha256(text).hexdigest()
            ctx.state["server"]["installed"][sudo.BACKUP_SCRIPT] = digest
            ctx.state["server"]["staged"][sudo.BACKUP_SCRIPT] = digest
            ctx.save()
            env.fake.world.files[sudo.BACKUP_SCRIPT] = text
        match = "predates the upgrade guard"
    elif case == "flag":
        env.fake.world.files[sudo.FLAG] = b""
        match = "did not create it"
    elif case == "space":
        env.fake.free = 3
        match = "twice the data size"
    else:
        del env.fake.trees[DATA]
        match = "data directory is missing"
    with pytest.raises(InstallError, match=match):
        env.run()
    assert "upgrade" not in env.state()["garden"]
    assert not any(event.startswith("stop ") for event in env.fake.events)


def test_running_backup_hook_refuses_after_the_flag(env: Env) -> None:
    env.fake.backup_state = "activating"
    with pytest.raises(InstallError, match="garden-backup.service is activating"):
        env.run()
    assert sudo.FLAG in env.fake.world.files
    assert not any(event.startswith("stop ") for event in env.fake.events)
    assert env.fake.active == set(sudo.UNITS)
    env.fake.backup_state = "inactive"
    env.run()
    assert sudo.FLAG not in env.fake.world.files


@pytest.mark.parametrize("member", ["foreign_uid", "symlink"])
def test_cold_copy_members_are_validated(
    env: Env, member: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if member == "foreign_uid":
        env.fake.member_uid = 0
        match = "foreign member"
    else:
        original = env.fake.archive

        def archive(tree: dict[str, bytes]) -> bytes:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as out:
                with tarfile.open(fileobj=io.BytesIO(original(tree))) as source:
                    for item in source:
                        stream = source.extractfile(item) if item.isfile() else None
                        out.addfile(item, stream)
                link = tarfile.TarInfo("data/link")
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
                link.uid = link.gid = 950
                out.addfile(link)
            return raw.getvalue()

        monkeypatch.setattr(env.fake, "archive", archive)
        match = "unsafe member"
    with pytest.raises(InstallError, match=match):
        env.run()
    assert not any(event.startswith("file ") for event in env.fake.events)


def test_inflight_upgrade_refuses_resume_remove_and_backup(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.fake.armed = "phase:copied"
    with pytest.raises(KeyboardInterrupt):
        env.run()
    args = ["--name", "system", "--state-root", str(env.state_root)]
    before = env.state_path.read_bytes()
    events = list(env.fake.events)
    capsys.readouterr()
    for argv in (
        ["resume", *args, "--non-interactive"],
        ["garden-remove", *args, "--yes", "--non-interactive"],
        ["garden-backup", *args, "--yes", "--non-interactive"],
    ):
        assert cli.main(argv, default_source=ROOT) != 0
        assert "run garden-upgrade --name system again" in capsys.readouterr().err
    assert env.state_path.read_bytes() == before
    assert env.fake.events == events
    with open_context(env.state_root, "system") as ctx:
        with pytest.raises(InstallError, match="run garden-upgrade --name system"):
            garden_backup._check(ctx)


def test_files_follow_the_recorded_release(env: Env, tmp_path: Path) -> None:
    with open_context(env.state_root, "system") as ctx:
        before = sudo.files(ctx)
        empty = tmp_path / "empty"
        (empty / "garden/deploy").mkdir(parents=True)
        ctx.source = empty
        assert sudo.files(ctx) == before
        assert (
            sudo.snippet(ctx)
            == (env.state_root / "system/garden-server.sudoers").read_text()
        )


def test_legacy_tool_record_is_extended_once(env: Env) -> None:
    with open_context(env.state_root, "system") as ctx:
        tools = ctx.state["server"]["tools"]
        ctx.state["server"]["tools"] = {name: tools[name] for name in sudo.LEGACY_TOOLS}
        server.record_tools(ctx)
        assert ctx.state["server"]["tools"] == tools
        ctx.state["server"]["tools"] = {
            **{name: tools[name] for name in sudo.LEGACY_TOOLS},
            "rm": "/tmp/rm",
        }
        with pytest.raises(InstallError, match="paths changed"):
            server.record_tools(ctx)


@pytest.mark.parametrize(
    "key,params",
    [
        ("data_take", {"path": "/etc"}),
        ("data_extract", {"directory": "/etc"}),
        ("data_aside", {"path": "/var/lib/garden/.garden/other"}),
        ("flag_set", {"path": "/etc/shadow"}),
        ("rm", {"path": "/var/lib/garden/.garden/data"}),
        ("rm", {"path": "/etc/garden"}),
        ("digest", {"paths": ["/etc/shadow"]}),
        ("stat", {"path": "/var/lib/garden/.garden/data"}),
        ("file", {"path": sudo.FLAG}),
    ],
)
def test_allowlist_negatives(env: Env, key: str, params: dict[str, Any]) -> None:
    env.fake.events.clear()
    with open_context(env.state_root, "system") as ctx:
        with pytest.raises(InstallError, match="allowlist"):
            sudo.run(ctx, key, **params)
    assert env.fake.events == []


def test_data_probe_parser_fails_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(env.state_root, "system") as ctx:
        for raw in ("", f"{DATA}\n", "/etc/shadow\n/dev/null\n"):
            monkeypatch.setattr(sudo, "run", lambda context, key, raw=raw: raw)
            with pytest.raises(InstallError, match="probe response"):
                sudo.data_present(ctx)


def test_status_reports_recorded_upgrade(env: Env) -> None:
    env.run()
    with open_context(env.state_root, "system") as ctx:
        result = workflow.status_install(ctx)
    assert result["garden"]["upgrade_phase"] == "complete"
    assert result["garden"]["previous_version"] == garden.source_version(ROOT)
    assert len(result["garden"]["cold_copies"]) == 1
    assert set(result["server"]["releases"].values()) == {
        TARGET_VERSION,
        garden.source_version(ROOT),
    }


def test_abort_before_the_first_release_is_retained(env: Env) -> None:
    # Codex P2: an interrupt before staging, then --rollback, must not leave
    # Garden stopped for want of a retained copy of the unchanged release.
    old = installed_release(env)
    env.fake.armed = "phase:staged"
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert "releases" not in env.state()["server"]
    env.run(rollback=True)
    assert installed_release(env) == old
    assert env.fake.active == env.fake.enabled == set(sudo.UNITS)
    assert sudo.FLAG not in env.fake.world.files
    assert env.state()["garden"]["upgrade"]["phase"] == "complete"


def _after(monkeypatch: pytest.MonkeyPatch, name: str, match: Any) -> list[bool]:
    """Interrupt once, right after the real os.<name> call that match accepts."""
    original = getattr(os, name)
    fired: list[bool] = []

    def wrapper(source: Any, dest: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(source, dest, *args, **kwargs)
        if not fired and match(Path(source), Path(dest)):
            fired.append(True)
            raise KeyboardInterrupt(f"after {name}")
        return result

    monkeypatch.setattr(os, name, wrapper)
    return fired


def test_cold_copy_linked_before_its_receipt_is_adopted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex P2: os.link landed, the owned_files receipt did not.
    fired = _after(monkeypatch, "link", lambda _, dest: dest.name == "data.tar")
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert fired
    monkeypatch.setattr(os, "link", LINK)
    env.until_done()
    state = env.state()
    receipt = state["garden"]["upgrade"]["cold_copy"]
    assert state["owned_files"][receipt["path"]] == receipt["sha256"]
    assert len(state["garden"]["upgrades"]) == 1


def test_cold_copy_without_a_matching_intent_stays_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _after(monkeypatch, "link", lambda _, dest: dest.name == "data.tar")
    with pytest.raises(KeyboardInterrupt):
        env.run()
    monkeypatch.setattr(os, "link", LINK)
    with open_context(env.state_root, "system") as ctx:
        intents = ctx.state["file_intents"]
        copy = next(key for key in intents if key.endswith("data.tar"))
        intents[copy] = "0" * 64
        ctx.save()
    with pytest.raises(InstallError, match="unowned"):
        env.run()


@pytest.mark.parametrize("which", ["current", "target"])
def test_release_published_before_its_record_converges(
    env: Env, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    # Codex P2: the release directory was renamed into place, its record lost.
    calls: list[Path] = []

    def match(source: Path, dest: Path) -> bool:
        if not source.name.startswith(".tmp-"):
            return False
        calls.append(dest)
        return len(calls) == (1 if which == "current" else 2)

    fired = _after(monkeypatch, "rename", match)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert fired
    monkeypatch.setattr(os, "rename", RENAME)
    env.until_done()
    state = env.state()
    key = state["garden"]["source"]["artefact"]["release_sha256"]
    assert installed_release(env) == release(env, key)
    assert len(state["server"]["releases"]) == 2
