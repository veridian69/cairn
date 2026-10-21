"""Release changes with fake adapters and durable interruption points."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import platform
import tarfile
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cairn_install import cli, core, docker, garden, workflow
from cairn_install import garden_upgrade as upgrade
from cairn_install.core import Context, InstallError, atomic_write, open_context

PHASES = [
    "recorded",
    "staged",
    "checked",
    "stopped",
    "copied",
    "swapped",
    "started",
    "verified",
    "complete",
]


def checkout(path: Path, version: str, content: str) -> Path:
    (path / "src/cairn").mkdir(parents=True, exist_ok=True)
    (path / "garden").mkdir(exist_ok=True)
    (path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (path / "src/cairn/__init__.py").write_text("# Cairn\n")
    (path / "garden/main.go").write_text(content)
    return path


class World:
    def __init__(self) -> None:
        self.ctx: Context
        self.calls: list[str] = []
        self.builds: dict[str, dict[str, str]] = {}
        self.staged: dict[str, dict[str, str]] = {}
        self.binaries = {hashlib.sha256(b"old").hexdigest(): b"old"}
        self.copies: list[Path] = []
        self.pruned: list[Any] = []
        self.running = True
        self.generation = {"val": "generation-1", "spike": "generation-1"}
        self.messages = b"before-upgrade"
        self.restores = 0
        self.armed: str | None = None
        self.error: type[BaseException] = InstallError

    def hit(self, point: str) -> None:
        self.calls.append(point)
        if self.armed == point:
            self.armed = None
            raise self.error(f"injected {point}")


class Hooks:
    def __init__(self, world: World) -> None:
        self.world = world

    def validate_ownership(self, *, require_garden_image: bool = False) -> None:
        self.world.hit("ownership")

    def garden_stage(self, source: Path) -> dict[str, str]:
        world = self.world
        journal = world.ctx.state["garden"]["upgrade"]
        assert journal["phase"] == "staged"
        if journal["id"] not in world.staged:
            digest = hashlib.sha256(
                (source / "garden/main.go").read_bytes()
            ).hexdigest()
            world.binaries[digest] = (source / "garden/main.go").read_bytes()
            world.builds[journal["id"]] = (
                {"binary_sha256": digest}
                if world.ctx.mode == "native"
                else {"image": f"owned:g-{digest[:22]}", "id": "sha256:" + digest}
            )
            world.staged[journal["id"]] = world.builds[journal["id"]]
        world.hit("stage:after")
        return world.staged[journal["id"]]

    def garden_copy(self, dest: Path) -> None:
        world = self.world
        assert not world.running
        assert world.ctx.state["garden"]["upgrade"]["phase"] == "copied"
        if not dest.exists():
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with tarfile.open(dest, "w") as archive:
                member = tarfile.TarInfo("data/state.db")
                member.size = len(world.messages)
                archive.addfile(member, io.BytesIO(world.messages))
            dest.chmod(0o600)
            world.copies.append(dest)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            world.ctx.state["owned_files"][str(dest)] = digest
            if world.ctx.mode == "native":
                world.ctx.state.setdefault("file_intents", {})[str(dest)] = digest
            else:
                world.ctx.state["resources"]["docker"].setdefault(
                    "garden_captures", {}
                )[str(dest)] = {"sha256": digest, "bytes": dest.stat().st_size}
            world.ctx.save()
        world.hit("copy:after")

    def garden_swap(self, artefact: dict[str, str]) -> None:
        world = self.world
        assert world.ctx.state["garden"]["upgrade"]["phase"] == "swapped"
        assert not world.running
        if world.ctx.mode == "docker":
            world.ctx.state["resources"]["docker"]["garden_image"] = {
                "reference": artefact["image"],
                "id": artefact["id"],
            }
        else:
            path = world.ctx.root / "garden/bin/garden"
            atomic_write(path, world.binaries[artefact["binary_sha256"]], 0o755)
            world.ctx.state["owned_files"][str(path)] = artefact["binary_sha256"]
            world.ctx.save()
        world.hit("swap:after")

    def garden_start(self) -> None:
        world = self.world
        assert world.ctx.state["garden"]["upgrade"]["new_started"]
        world.running = True
        world.hit("start:after")

    def garden_stop(self) -> None:
        self.world.running = False
        self.world.hit("stop:after")

    def garden_restore_data(self, copy: Path) -> None:
        world = self.world
        journal = world.ctx.state["garden"]["upgrade"]
        assert not world.running
        assert journal["phase"] == "copied"
        if journal["aside"] is None:
            journal["aside"] = str(
                world.ctx.root / "garden" / f"data.pre-restore-{journal['id']}"
            )
            world.ctx.save()
            world.restores += 1
            with tarfile.open(copy) as archive:
                stream = archive.extractfile("data/state.db")
                assert stream is not None
                world.messages = stream.read()
        world.hit("restore:after")

    def garden_prune(self, keep: Any) -> None:
        world = self.world
        world.pruned.append(copy.deepcopy(keep))
        for key, artefact in list(world.staged.items()):
            if artefact not in keep:
                del world.staged[key]
        staging = world.ctx.state["garden"]["upgrade"]["phase"] == "staged"
        world.hit("prune:staged" if staging else "prune:after")

    def garden_open_endpoint(self) -> None:
        pass

    def garden_close_endpoint(self) -> None:
        pass

    def close(self) -> None:
        self.world.hit("close")


class Env:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        self.root = tmp_path / "state"
        self.source = checkout(tmp_path / "cairn", "1.0.0", "old")
        self.target = checkout(tmp_path / "new", "2.0.0", "new")
        self.world = World()
        world = self.world
        hooks = Hooks(world)

        def backend(ctx: Context, **kwargs: Any) -> Any:
            world.ctx = ctx
            return hooks

        monkeypatch.setattr(workflow, "backend", backend)
        monkeypatch.setattr(workflow, "_garden_adapter", lambda ctx, adapter: adapter)
        monkeypatch.setattr(garden, "verify", self.verify)
        monkeypatch.setattr(garden, "profiles", lambda ctx: None)
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        original_save = Context.save

        def save(ctx: Context) -> None:
            original_save(ctx)
            phase = ctx.state.get("garden", {}).get("upgrade", {}).get("phase")
            if phase:
                world.hit("phase:" + phase)

        monkeypatch.setattr(Context, "save", save)
        with open_context(
            self.root,
            "demo",
            create={
                "source": str(self.source),
                "source_fingerprint": cli.source_fingerprint(self.source),
                "mode": mode,
                "port": 19000,
                "semantic": False,
            },
        ) as ctx:
            ctx.state["status"] = "verified"
            ctx.state["garden"] = {
                "options": {
                    "endpoint": "https://garden.example.test:8443/mcp",
                    "participants": {"val": "codex", "spike": "claude"},
                    "expires_at": (datetime.now(UTC) + timedelta(days=7)).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                }
            }
            data = ctx.root / "garden/data"
            data.mkdir(parents=True, mode=0o700)
            (data / "state.db").write_bytes(world.messages)
            binary = ctx.root / "garden/bin/garden"
            binary.parent.mkdir(mode=0o700)
            atomic_write(binary, b"old", 0o755)
            ctx.state["owned_files"][str(binary)] = hashlib.sha256(b"old").hexdigest()
            ctx.state["resources"]["docker"] = {
                "garden_image": {"reference": "owned:old", "id": "sha256:" + "a" * 64}
            }
            ctx.save()

    @property
    def state_path(self) -> Path:
        return self.root / "demo/state.json"

    def state(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.state_path.read_bytes())
        return result

    def verify(self, ctx: Context) -> None:
        assert self.world.running
        ctx.state["garden"]["verification"] = {
            name: {"generation": generation}
            for name, generation in self.world.generation.items()
        }
        ctx.save()
        self.world.hit("verify:after")

    def run(self, **kwargs: Any) -> Any:
        args = {
            "name": "demo",
            "source": self.target,
            "rollback": False,
            "restore_data": False,
            "keep_data": False,
            "yes": True,
            "non_interactive": True,
            "verbose": False,
        }
        args.update(kwargs)
        if args["rollback"] and "source" not in kwargs:
            args["source"] = None
        return upgrade.run(Namespace(**args), self.root)


@pytest.fixture(params=["native", "docker"])
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Env:
    return Env(tmp_path, monkeypatch, request.param)


@pytest.mark.parametrize(
    "point",
    ["phase:" + phase for phase in PHASES]
    + [
        "prune:staged",
        "stage:after",
        "copy:after",
        "stop:after",
        "swap:after",
        "start:after",
        "prune:after",
    ],
)
@pytest.mark.parametrize("error", [InstallError, KeyboardInterrupt])
def test_interrupt_then_resume(
    env: Env, point: str, error: type[BaseException]
) -> None:
    env.world.armed, env.world.error = point, error
    with pytest.raises(error, match="injected"):
        env.run()
    failed = env.state()
    identifier = failed["garden"]["upgrade"]["id"]
    assert failed["status"] == (
        "interrupted" if error is KeyboardInterrupt else "failed"
    )
    if point == "phase:complete":
        _resume_after_terminal_interrupt(env)
    result = env.run()
    record = env.state()["garden"]
    assert result["status"] == "verified"
    assert record["upgrade"]["id"] == identifier
    assert record["upgrade"]["phase"] == "complete"
    assert (
        len(env.world.builds) == len(env.world.copies) == len(record["upgrades"]) == 1
    )
    assert record["upgrade"]["generation_before"] == env.world.generation
    assert record["previous"]["version"] == "1.0.0"
    assert len(env.world.pruned[-1]) == 2


def _resume_after_terminal_interrupt(env: Env) -> None:
    # A complete journal never excuses a failed status: resume re-verifies.
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="run resume first"):
        env.run()
    assert env.state_path.read_bytes() == before
    with open_context(env.root, "demo") as ctx:
        ctx.state["status"] = "verified"
        ctx.save()


def test_fingerprint_scope_and_legacy_backfill(env: Env) -> None:
    original = env.state()["source_fingerprint"]
    env.run()
    state = env.state()
    assert state["source_fingerprint"] == original
    assert state["source_fingerprint_scope"] == "excluding_garden"
    assert state["cairn_fingerprint"] == cli.source_fingerprint(
        env.source, exclude_garden=True
    )
    assert state["garden"]["previous"]["artefact"]
    with open_context(env.root, "demo") as ctx:
        cli._assert_cairn_source(ctx, env.source)
    (env.source / "src/cairn/__init__.py").write_text("changed")
    with pytest.raises(InstallError, match="cairn_fingerprint"):
        env.run()


@pytest.mark.parametrize("direction", ["upgrade", "restore", "keep"])
@pytest.mark.parametrize("error", [InstallError, KeyboardInterrupt])
def test_restart_resumes_after_stop_before_start(
    env: Env,
    direction: str,
    error: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if direction != "upgrade":
        env.run()
    original_restart = workflow._restart_garden

    def interrupt_restart(ctx: Context, adapter: workflow.GardenAdapter) -> None:
        env.world.armed, env.world.error = "stop:after", error
        original_restart(ctx, adapter)

    monkeypatch.setattr(workflow, "_restart_garden", interrupt_restart)
    kwargs = {
        "rollback": direction != "upgrade",
        "restore_data": direction == "restore",
        "keep_data": direction == "keep",
    }
    with pytest.raises(error, match="injected stop:after"):
        env.run(**kwargs)
    failed = env.state()["garden"]["upgrade"]
    assert failed["phase"] == "verified"
    assert not env.world.running
    monkeypatch.setattr(workflow, "_restart_garden", original_restart)
    assert env.run(**kwargs)["status"] == "verified"
    record = env.state()["garden"]
    assert record["upgrade"]["id"] == failed["id"]
    assert record["upgrade"]["phase"] == "complete"
    assert record["upgrade"]["restart_started"]
    assert env.world.running
    assert len(env.world.builds) == len(env.world.copies) == 1
    assert len(record["upgrades"]) == (1 if direction == "upgrade" else 2)
    assert record["upgrade"]["generation_before"] == env.world.generation
    assert env.world.restores == int(direction == "restore")


@pytest.mark.parametrize(
    "part", ["garden/main.go", "pyproject.toml", "src/cairn/__init__.py"]
)
def test_changed_legacy_source_is_never_backfilled(env: Env, part: str) -> None:
    (env.source / part).write_text("changed")
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError):
        env.run()
    assert env.state_path.read_bytes() == before
    assert "source" not in env.state()["garden"]


def test_changed_owned_artefact_is_never_backfilled(env: Env) -> None:
    if env.state()["mode"] == "native":
        (env.root / "demo/instance/garden/bin/garden").write_bytes(b"tampered")
    else:
        with open_context(env.root, "demo") as ctx:
            del ctx.state["resources"]["docker"]["garden_image"]["id"]
            ctx.save()
    with pytest.raises(InstallError, match="[Oo]wned Garden"):
        env.run()
    assert "source" not in env.state()["garden"]


def test_missing_legacy_docker_image_refuses_backfill_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    with open_context(env.root, "demo") as ctx:
        resources = ctx.state["resources"]["docker"]
        resources["project"] = "cairn-install-demo"
        resources["owner_labels"] = {
            "io.cairn.install.instance": ctx.instance_id,
            "io.cairn.install.run": ctx.run_id,
        }
        resources["garden_image"]["reference"] = (
            f"cairn-install-demo-garden:{ctx.state['source_fingerprint'][:24]}"
        )
        ctx.save()

    def backend(ctx: Context) -> docker.Backend:
        env.world.ctx = ctx
        return docker.Backend(ctx)

    monkeypatch.setattr(workflow, "backend", backend)
    monkeypatch.setattr(
        workflow, "_garden_adapter", lambda ctx, adapter: Hooks(env.world)
    )
    monkeypatch.setattr(docker.Backend, "_ids", lambda *a, **kw: [])
    monkeypatch.setattr(docker.Backend, "_optional_image_details", lambda *a: None)
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="Owned Garden image is missing"):
        env.run()
    assert env.state_path.read_bytes() == before
    assert "source" not in env.state()["garden"]
    assert "upgrade" not in env.state()["garden"]
    assert not env.world.builds


@pytest.mark.parametrize("version", ["0.9.99", "1.0rc1", "2..0", "2.0-beta"])
def test_invalid_or_lower_version_refused(env: Env, version: str) -> None:
    (env.target / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="downgrade|numeric|invalid"):
        env.run()
    assert env.state_path.read_bytes() == before
    assert not env.world.builds


def test_same_fingerprint_is_read_only_noop(env: Env) -> None:
    with open_context(env.root, "demo") as ctx:
        ctx.state["garden"]["source"] = {
            "path": str(env.source),
            "version": "1.0.0",
            "garden_fingerprint": garden.garden_fingerprint(env.source),
        }
        ctx.save()
    (env.target / "garden/main.go").write_text("old")
    before = env.state_path.read_bytes()
    assert env.run()["status"] == "verified"
    assert env.state_path.read_bytes() == before
    assert env.world.calls == []


def test_equal_version_rebuild_is_announced(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    (env.target / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    env.run()
    assert "Rebuilding Garden 1.0.0" in capsys.readouterr().out
    assert len(env.world.builds) == 1


@pytest.mark.parametrize("alias", [False, True])
def test_cairn_checkout_refused_including_realpath(env: Env, alias: bool) -> None:
    source = env.source
    if alias:
        source = env.target.parent / "alias"
        source.symlink_to(env.source, target_is_directory=True)
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="separate checkout"):
        env.run(source=source)
    assert env.state_path.read_bytes() == before


@pytest.mark.parametrize("change", ["path", "contents", "version"])
def test_changed_source_midflight_refused(env: Env, change: str) -> None:
    env.world.armed = "phase:staged"
    with pytest.raises(InstallError):
        env.run()
    source = env.target
    if change == "path":
        source = checkout(env.target.parent / "other", "2.0.0", "new")
    elif change == "contents":
        (source / "garden/main.go").write_text("other")
    else:
        (source / "pyproject.toml").write_text('[project]\nversion = "2.0.1"\n')
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="different --source"):
        env.run(source=source)
    assert env.state_path.read_bytes() == before


@pytest.mark.parametrize("change", ["contents", "version"])
def test_source_changed_during_staging_is_not_published(
    env: Env, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = env.world.hit

    def mutate(point: str) -> None:
        if point == "stage:after":
            if change == "contents":
                (env.target / "garden/main.go").write_text("other")
            else:
                (env.target / "pyproject.toml").write_text(
                    '[project]\nversion = "2.0.1"\n'
                )
        hit(point)

    monkeypatch.setattr(env.world, "hit", mutate)
    with pytest.raises(InstallError, match="changed while staging"):
        env.run()
    journal = env.state()["garden"]["upgrade"]
    assert journal["phase"] == "staged"
    assert "artefact" not in journal["to"]


def test_refused_staging_output_is_rebuilt_after_revert(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = env.target / "garden/main.go"
    original = main.read_bytes()
    check = upgrade._check_staged_source
    calls = 0

    def change_after_precheck(target: dict[str, Any], path: Path) -> None:
        nonlocal calls
        check(target, path)
        calls += 1
        if calls == 1:
            main.write_text("changed before the build read it")

    monkeypatch.setattr(upgrade, "_check_staged_source", change_after_precheck)
    with pytest.raises(InstallError, match="changed while staging"):
        env.run()
    main.write_bytes(original)
    env.run()
    artefact = env.state()["garden"]["source"]["artefact"]
    built = hashlib.sha256(original).hexdigest()
    assert artefact == (
        {"binary_sha256": built}
        if env.world.ctx.mode == "native"
        else {"image": f"owned:g-{built[:22]}", "id": "sha256:" + built}
    )


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("choice", [None, "restore", "keep"])
def test_rollback_data_choice_matrix(env: Env, phase: str, choice: str | None) -> None:
    env.world.armed = "phase:" + phase
    with pytest.raises(InstallError):
        env.run()
    if phase == "complete":
        _resume_after_terminal_interrupt(env)
    started = PHASES.index(phase) >= PHASES.index("started")
    before = env.state_path.read_bytes()
    kwargs = {
        "rollback": True,
        "restore_data": choice == "restore",
        "keep_data": choice == "keep",
    }
    if started and choice is None:
        with pytest.raises(InstallError) as error:
            env.run(**kwargs)
        assert error.value.code == "data_choice_required"
        assert "discards messages" in str(error.value)
        assert "later backups" in str(error.value)
        assert env.state_path.read_bytes() == before
        return
    env.world.messages = b"new messages"
    result = env.run(**kwargs)
    record = env.state()["garden"]
    assert result["status"] == "verified"
    assert record["source"]["version"] == "1.0.0"
    assert "previous" not in record
    assert record["upgrade"]["direction"] == "rollback"
    assert record["upgrade"]["data_choice"] == (choice if started else None)
    assert env.world.restores == int(started and choice == "restore")
    assert env.world.messages == (
        b"before-upgrade" if started and choice == "restore" else b"new messages"
    )
    with open_context(env.root, "demo") as ctx:
        assert workflow._garden_pending(ctx) is None


@pytest.mark.parametrize("operation", ["install", "remove", "rollback"])
def test_lifecycle_refuses_inflight_upgrade(env: Env, operation: str) -> None:
    env.world.armed = "phase:staged"
    with pytest.raises(InstallError):
        env.run()
    before = env.state_path.read_bytes()
    with open_context(env.root, "demo") as ctx:
        with pytest.raises(InstallError, match="run garden-upgrade --name demo again"):
            if operation == "install":
                workflow.run_install(ctx)
            elif operation == "remove":
                workflow.remove_garden(ctx, delete_data=False)
            else:
                workflow.rollback_install(ctx)
    assert env.state_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["kubernetes", "garden-server"])
def test_unsupported_modes_refused_without_writes(env: Env, mode: str) -> None:
    with open_context(env.root, "demo") as ctx:
        ctx.state["mode"] = mode
        ctx.save()
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="later release") as error:
        env.run()
    assert error.value.code == "invalid_arguments"
    assert env.state_path.read_bytes() == before
    assert env.world.calls == []


def test_status_reads_recorded_upgrade_and_backup_fields(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.run()
    with open_context(env.root, "demo") as ctx:
        ctx.state["garden"]["backup"] = {"phase": "complete"}
        backup = {
            "archive": "/recorded/archive.tar",
            "sha256": "b" * 64,
            "at": "recorded-time",
            "snapshot_id": "saved-snapshot",
        }
        ctx.state["garden"]["backups"] = [backup]
        monkeypatch.setattr(
            workflow, "backend", lambda *a: pytest.fail("status must not probe")
        )
        status = workflow.status_install(ctx)["garden"]
    assert status["version"] == "2.0.0"
    assert status["previous_version"] == "1.0.0"
    assert status["upgrade_phase"] == status["backup_phase"] == "complete"
    assert status["last_upgrade"]["from"] == "1.0.0"
    assert status["last_upgrade"]["to"] == "2.0.0"
    assert status["cold_copies"] == [status["last_upgrade"]["cold_copy"]]
    assert status["last_backup"] == backup


@pytest.mark.parametrize("phase", ["checked", "verified", "restart"])
def test_generation_change_is_detected(
    env: Env, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if phase != "restart":
        env.world.armed = "phase:" + phase
        with pytest.raises(InstallError):
            env.run()
        env.world.generation["spike"] = "wrong-generation"
    else:
        original_restart = workflow._restart_garden

        def restart(ctx: Context, adapter: workflow.GardenAdapter) -> None:
            env.world.generation["spike"] = "wrong-generation"
            original_restart(ctx, adapter)

        monkeypatch.setattr(workflow, "_restart_garden", restart)
    with pytest.raises(InstallError, match="generation changed"):
        env.run()
    assert env.state()["garden"]["upgrade"]["phase"] != "complete"
    assert env.state()["status"] == "failed"


def test_restore_rollback_resumes_once_and_locks_data_choice(env: Env) -> None:
    env.run()
    env.world.messages = b"after-upgrade"
    env.world.armed = "restore:after"
    with pytest.raises(InstallError, match="injected"):
        env.run(rollback=True, restore_data=True)
    identifier = env.state()["garden"]["upgrade"]["id"]
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="different data choice"):
        env.run(rollback=True, keep_data=True)
    assert env.state_path.read_bytes() == before
    with pytest.raises(InstallError, match="rollback is in progress"):
        env.run()
    env.run(rollback=True)
    assert env.state()["garden"]["upgrade"]["id"] == identifier
    assert env.world.restores == 1
    assert env.world.messages == b"before-upgrade"


def _assert_copy_forgotten(env: Env, path: Path) -> None:
    state = env.state()
    for table in (
        state["owned_files"],
        state.get("file_intents", {}),
        state["resources"]["docker"].get("garden_captures", {}),
    ):
        assert str(path) not in table


@pytest.mark.parametrize("ownership", ["owned", "intent", "capture", "partial"])
def test_rollback_removes_unrecorded_copy(env: Env, ownership: str) -> None:
    env.world.armed, env.world.error = "copy:after", KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt, match="injected"):
        env.run()
    path = env.world.copies[0]
    with open_context(env.root, "demo") as ctx:
        journal = ctx.state["garden"]["upgrade"]
        assert journal["cold_copy"] is None
        digest = ctx.state["owned_files"].pop(str(path))
        ctx.state.get("file_intents", {}).pop(str(path), None)
        captures = ctx.state["resources"]["docker"].setdefault("garden_captures", {})
        captures.pop(str(path), None)
        if ownership == "owned":
            ctx.state["owned_files"][str(path)] = digest
        elif ownership == "intent":
            ctx.state.setdefault("file_intents", {})[str(path)] = digest
        else:
            if ctx.mode != "docker":
                pytest.skip("Capture records belong to Docker")
            captures[str(path)] = (
                {"sha256": digest, "bytes": path.stat().st_size}
                if ownership == "capture"
                else {"intent": "capture"}
            )
            if ownership == "partial":
                path.write_bytes(b"incomplete tar")
        ctx.save()
    result = env.run(rollback=True)
    assert result["status"] == "verified"
    assert env.world.restores == 0
    assert not path.exists()
    _assert_copy_forgotten(env, path)
    assert env.state()["garden"]["upgrades"][-1]["cold_copy"] is None
    _three_upgrades_keep_only_two_copies_and_artifacts(env)


@pytest.mark.parametrize("change", ["unowned", "changed", "symlink", "missing"])
def test_rollback_reconciles_unrecorded_copy_safely(
    env: Env, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.world.armed = "copy:after"
    with pytest.raises(InstallError, match="injected"):
        env.run()
    path = env.world.copies[0]
    old = env.state()["garden"]["upgrade"]
    if change == "missing":
        # The unlink may have landed without its directory sync.
        path.unlink()
        synced: list[Path] = []
        monkeypatch.setattr(core, "_fsync_directory", synced.append)
        env.run(rollback=True)
        assert path.parent in synced
        _assert_copy_forgotten(env, path)
        return
    with open_context(env.root, "demo") as ctx:
        if change == "unowned":
            ctx.state["owned_files"].pop(str(path))
            ctx.state.get("file_intents", {}).pop(str(path), None)
            ctx.state["resources"]["docker"].get("garden_captures", {}).pop(
                str(path), None
            )
        elif change == "symlink":
            path.unlink()
            path.symlink_to(env.world.ctx.root / "garden/data/state.db")
        else:
            path.write_bytes(b"changed archive")
        ctx.save()
    before = path.read_bytes()
    with pytest.raises(InstallError):
        env.run(rollback=True)
    assert path.read_bytes() == before
    assert env.state()["garden"]["upgrade"] == old


def _three_upgrades_keep_only_two_copies_and_artifacts(env: Env) -> None:
    first = len(env.world.copies)
    env.run()
    for number in (3, 4):
        env.target = checkout(
            env.target.parent / f"source-{number}", f"{number}.0.0", f"release-{number}"
        )
        env.run()
    record = env.state()["garden"]
    assert len(record["upgrades"]) == len(env.world.builds) == first + 3
    assert not env.world.copies[first].exists()
    _assert_copy_forgotten(env, env.world.copies[first])
    assert all(path.exists() for path in env.world.copies[first + 1 :])
    assert env.world.pruned[-1] == [
        record["source"]["artefact"],
        record["previous"]["artefact"],
    ]
    env.run(rollback=True, keep_data=True)
    with open_context(env.root, "demo") as ctx:
        copies = workflow.status_install(ctx)["garden"]["cold_copies"]
    assert {receipt["path"] for receipt in copies} == {
        str(path) for path in env.world.copies[first + 1 :]
    }


def test_three_upgrades_keep_only_two_copies_and_artifacts(env: Env) -> None:
    _three_upgrades_keep_only_two_copies_and_artifacts(env)


def test_second_upgrade_abort_returns_to_current_not_earlier_previous(env: Env) -> None:
    env.run()
    env.target = checkout(env.target.parent / "third", "3.0.0", "third")
    env.world.armed = "swap:after"
    with pytest.raises(InstallError):
        env.run()
    env.run(rollback=True)
    record = env.state()["garden"]
    assert record["source"]["version"] == "2.0.0"
    assert "previous" not in record


def test_new_preflight_failure_does_not_poison_previous_journal(env: Env) -> None:
    env.run()
    env.target = checkout(env.target.parent / "third", "3.0.0", "third")
    previous = env.state()["garden"]["upgrade"]
    env.world.armed = "ownership"
    with pytest.raises(InstallError, match="injected"):
        env.run()
    assert env.state()["status"] == "verified"
    assert env.state()["garden"]["upgrade"] == previous
    env.run()
    assert env.state()["garden"]["source"]["version"] == "3.0.0"


def test_cold_copy_tampering_refuses_restore(env: Env) -> None:
    env.run()
    path = env.world.copies[0]
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"before-upgrade", b"tamper-upgrade"))
    with pytest.raises(InstallError, match="cold copy changed"):
        env.run(rollback=True, restore_data=True)
    assert not env.world.restores


@pytest.mark.parametrize(
    "name, kind",
    [
        ("../state.db", "file"),
        ("data/link", "link"),
        ("data/pipe", "fifo"),
        ("data/other", "file"),
    ],
)
def test_unsafe_or_incomplete_copy_never_swaps(
    env: Env, name: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsafe(self: Hooks, dest: Path) -> None:
        with tarfile.open(dest, "w") as archive:
            member = tarfile.TarInfo(name)
            if kind == "link":
                member.type, member.linkname = tarfile.SYMTYPE, "/outside"
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        dest.chmod(0o600)

    monkeypatch.setattr(Hooks, "garden_copy", unsafe)
    with pytest.raises(InstallError, match="unsafe member|no data/state.db"):
        env.run()
    assert "swap:after" not in env.world.calls


def test_confirmation_is_required_before_effects(env: Env) -> None:
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError) as error:
        env.run(yes=False)
    assert error.value.code == "confirmation_required"
    assert env.state_path.read_bytes() == before
    assert not env.world.calls


def test_initial_live_failure_leaves_resume_available(env: Env) -> None:
    env.world.armed = "verify:after"
    with pytest.raises(InstallError, match="run resume first"):
        env.run()
    assert env.state()["status"] == "verified"
    assert "upgrade" not in env.state()["garden"]
    with open_context(env.root, "demo") as ctx:
        assert workflow._garden_pending(ctx) is None


def test_native_cold_copy_limit_refused_before_journal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env.state()["mode"] != "native":
        pytest.skip("native measurement")
    monkeypatch.setattr(upgrade, "_COLD_COPY_LIMIT", 1024)
    with pytest.raises(InstallError, match="8 GiB cold-copy limit"):
        env.run()
    assert "upgrade" not in env.state()["garden"]
    assert not env.world.builds and env.world.running


def test_complete_journal_does_not_excuse_a_later_failure(env: Env) -> None:
    env.run()
    with open_context(env.root, "demo") as ctx:
        ctx.state["status"] = "failed"
        ctx.save()
    before = env.state_path.read_bytes()
    with pytest.raises(InstallError, match="run resume first"):
        env.run()
    with pytest.raises(InstallError, match="run resume first"):
        env.run(rollback=True, keep_data=True)
    assert env.state_path.read_bytes() == before


def test_native_space_boundary_before_journal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env.state()["mode"] != "native":
        pytest.skip(
            "native measurement; Docker measurement is a shared-interface decision"
        )
    import shutil
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    size = len(env.world.messages)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: Usage(1000, 1000 - (2 * size - 1), 2 * size - 1),
    )
    with pytest.raises(InstallError, match="twice the data size"):
        env.run()
    assert "upgrade" not in env.state()["garden"]
    assert not env.world.builds
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: Usage(1000, 1000 - 2 * size, 2 * size)
    )
    env.run()
