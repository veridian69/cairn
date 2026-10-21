"""Garden add and remove: fake-runner state machines and CLI preconditions."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cairn_install import cli, garden, garden_native, workflow
from cairn_install.core import InstallError, open_context

ERRORS: list[type[BaseException]] = [InstallError, KeyboardInterrupt]

# Points are hit in flow order; occurrence 2 targets the restart stage.
ADD_FAULTS = [
    ("preflight", 1),
    ("garden_files", 1),
    ("garden_attach", 1),
    ("start", 1),
    ("ready", 1),
    ("verify_reads", 1),
    ("enrol", 1),
    ("garden_prepare", 1),
    ("garden_start", 1),
    ("garden_verify", 1),
    ("garden_stop", 1),
    ("garden_start", 2),
    ("garden_verify", 2),
]

# (point, occurrence, remove phase recorded when the interruption lands)
REMOVE_FAULTS = [
    ("garden_stop", 1, "stopping"),
    ("start", 1, "stopping"),
    ("ready", 1, "stopping"),
    ("revoke", 1, "stopping"),
    ("garden_detach", 1, "detaching"),
    ("ready", 2, "detached"),
    ("verify_reads", 1, "detached"),
]

ADD_CALLS = [
    "ownership",
    "preflight",
    "garden_files",
    "garden_attach",
    "start",
    "ready",
    "verify_reads",
    "enrol",
    "garden_prepare",
    "garden_start",
    "garden_verify",
    "profiles",
    "garden_stop",
    "garden_start",
    "garden_verify",
    "profiles",
]

REMOVE_CALLS = [
    "garden_stop",
    "start",
    "ready",
    "revoke",
    "garden_detach",
    "ready",
    "verify_reads",
]


def future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def garden_options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "endpoint": "https://garden.example.test:8443/mcp",
        "port": 8443,
        "participants": {"val": "codex"},
        "expires_at": future(),
        "stream": {"max_age": "168h", "max_bytes": 1024**3},
    }
    options.update(extra)
    return options


class Faults:
    """One-shot interruption at a named point; occurrences count from arming."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}
        self.armed: tuple[str, int, type[BaseException]] | None = None

    def arm(self, point: str, occurrence: int, error: type[BaseException]) -> None:
        self.seen[point] = 0
        self.armed = (point, occurrence, error)

    def hit(self, point: str) -> None:
        count = self.seen.get(point, 0) + 1
        self.seen[point] = count
        if self.armed is not None and self.armed[:2] == (point, count):
            error = self.armed[2]
            self.armed = None
            raise error(f"injected interruption at {point}")


class World:
    """Fake Cairn and Garden. Idempotency keys decide what the fake Cairn stores."""

    def __init__(self, faults: Faults) -> None:
        self.faults = faults
        self.ctx: Any = None
        self.calls: list[str] = []
        self.snapshots: list[Any] = []
        self.running = False
        self.garden_running = False
        self.garden_attached = False
        self.garden_data = False
        self.detach_requests: list[bool] = []
        self.principal_keys: list[str] = []
        self.principals: set[str] = set()
        self.revoke_keys: list[str] = []
        self.revoked: set[str] = set()

    def hit(self, point: str) -> None:
        self.calls.append(point)
        self.faults.hit(point)


class CairnHooks:
    runtime_python = "python"

    def __init__(self, world: World) -> None:
        self.world = world

    def validate_ownership(self) -> None:
        self.world.snapshots.append(copy.deepcopy(self.world.ctx.state.get("garden")))
        self.world.hit("ownership")

    def preflight(self) -> None:
        self.world.hit("preflight")

    def prepare(self) -> None:
        raise AssertionError("verified paths never prepare")

    def is_running(self) -> bool:
        return self.world.running

    def start(self) -> None:
        self.world.running = True
        self.world.hit("start")

    def stop(self) -> None:
        self.world.running = False

    def restart(self) -> None:
        raise AssertionError("garden add restarts only Garden")

    def rollback(self) -> None:
        self.world.running = False
        self.world.hit("rollback")

    def close(self) -> None:
        pass

    def validate_blitz_inventory(self) -> None:
        self.world.hit("validate_blitz")

    def blitz(self) -> None:
        self.world.hit("blitz")


class GardenHooks:
    def __init__(self, world: World) -> None:
        self.world = world

    def preflight(self) -> None:
        self.world.hit("garden_preflight")

    def garden_attach(self) -> None:
        self.world.garden_attached = True
        self.world.hit("garden_attach")

    def garden_detach(self, *, delete_data: bool) -> None:
        self.world.garden_attached = False
        if delete_data:
            self.world.garden_data = False
        self.world.detach_requests.append(delete_data)
        self.world.hit("garden_detach")

    def garden_prepare(self) -> None:
        self.world.garden_data = True
        self.world.hit("garden_prepare")

    def garden_start(self) -> None:
        self.world.garden_running = True
        self.world.hit("garden_start")

    def garden_stop(self) -> None:
        self.world.garden_running = False
        self.world.hit("garden_stop")

    def garden_open_endpoint(self) -> None:
        pass

    def garden_close_endpoint(self) -> None:
        pass

    def validate_blitz_inventory(self) -> None:
        self.world.hit("garden_validate_blitz")

    def blitz(self) -> None:
        self.world.hit("garden_blitz")


class DockerBackend(CairnHooks, GardenHooks):
    """Docker's adapter serves both roles; the garden hooks sit on the same object."""

    def __init__(self, world: World) -> None:
        CairnHooks.__init__(self, world)
        GardenHooks.__init__(self, world)

    def preflight(self) -> None:
        CairnHooks.preflight(self)


class Env:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        self.mode = mode
        self.state_root = tmp_path / "state"
        self.source = tmp_path / "source"
        self.faults = Faults()
        self.world = World(self.faults)
        world = self.world

        backend: Any
        if mode == "docker":
            backend = DockerBackend(world)
        else:
            backend = CairnHooks(world)
            garden_backend = GardenHooks(world)
            monkeypatch.setattr(
                garden_native, "GardenBackend", lambda ctx: garden_backend
            )

        def make_backend(ctx: Any, **kwargs: Any) -> Any:
            world.ctx = ctx
            return backend

        monkeypatch.setattr(workflow, "backend", make_backend)
        monkeypatch.setattr(
            workflow, "bootstrap", lambda *a: pytest.fail("no bootstrap")
        )
        monkeypatch.setattr(workflow, "ingest", lambda *a: pytest.fail("no ingest"))
        monkeypatch.setattr(workflow, "validate_ingest", lambda receipt: None)
        monkeypatch.setattr(workflow, "ready", self._ready)
        monkeypatch.setattr(workflow, "verify_reads", self._verify_reads)
        monkeypatch.setattr(garden, "prepare", lambda ctx: world.hit("garden_files"))
        monkeypatch.setattr(garden, "enrol", self._enrol)
        monkeypatch.setattr(garden, "revoke", self._revoke)
        monkeypatch.setattr(garden, "verify", self._verify)
        monkeypatch.setattr(garden, "profiles", lambda ctx: world.hit("profiles"))
        monkeypatch.setattr(garden, "garden_fingerprint", lambda source: "fp-1")
        monkeypatch.setattr(garden, "source_version", lambda source: "0.7.21")

    def _ready(self, ctx: Any) -> str:
        self.world.hit("ready")
        if not self.world.running:
            raise InstallError("Cairn is not running")
        return "admin-token"

    def _verify_reads(self, ctx: Any, python: str, receipt: dict[str, Any]) -> None:
        assert receipt == {"proof": "same"}
        self.world.hit("verify_reads")

    def _verify(self, ctx: Any, *args: Any, **kwargs: Any) -> None:
        self.world.hit("garden_verify")
        if not self.world.garden_running:
            raise InstallError("Garden is not running")

    def _enrol(self, ctx: Any, token: str) -> None:
        # The key is saved before the request, as the real enrolment does.
        agent = ctx.state["garden"].setdefault("agents", {}).setdefault("val", {})
        key = agent.setdefault("principal_key", str(uuid4()))
        ctx.save()
        self.world.principal_keys.append(key)
        self.world.principals.add(key)
        self.world.hit("enrol")
        agent["principal_id"] = key
        ctx.save()

    def _revoke(self, ctx: Any, token: str) -> None:
        garden_state = ctx.state["garden"]
        for name in sorted(garden_state.get("agents", {})):
            record = garden_state.setdefault("revocations", {}).setdefault(name, {})
            key = record.setdefault("revoke_key", str(uuid4()))
            ctx.save()
            self.world.revoke_keys.append(key)
            self.world.revoked.add(key)
            self.world.hit("revoke")

    def make_verified(self, status: str = "verified") -> None:
        self.source.mkdir(exist_ok=True)
        with open_context(
            self.state_root,
            "demo",
            create={
                "source": str(self.source),
                "mode": self.mode,
                "port": 19000,
                "semantic": False,
            },
        ) as ctx:
            ctx.state["status"] = status
            ctx.state["receipts"]["ingest"] = {"proof": "same"}
            ctx.save()


def verified_with_garden(env: Env) -> None:
    """Verified installation with a completed clean add; the call log starts empty."""
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        workflow.add_garden(ctx, garden_options())
    env.world.calls.clear()
    env.faults.seen.clear()


@pytest.mark.parametrize("mode", ["docker", "native"])
def test_clean_add_runs_the_verified_flow_once_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env = Env(tmp_path, monkeypatch, mode)
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        result = workflow.add_garden(ctx, garden_options())

        assert ctx.state["status"] == "verified"
        assert ctx.state["garden"]["add"] == {"phase": "complete"}
        assert ctx.state["garden"]["origin"] == "added"
        assert ctx.state["garden"]["source"] == {
            "path": str(env.source),
            "garden_fingerprint": "fp-1",
            "version": "0.7.21",
        }
        assert ctx.state["steps"]["garden_restart"] == "complete"
        assert "verified_recheck" not in ctx.state
        assert result["status"] == "verified"
    assert env.world.snapshots[0]["add"] == {"phase": "recorded"}
    expected = list(ADD_CALLS)
    if mode == "native":
        expected.insert(2, "garden_preflight")
    assert env.world.calls == expected
    assert len(env.world.principals) == 1
    assert env.world.garden_attached and env.world.garden_running
    assert env.world.garden_data


@pytest.mark.parametrize("mode", ["docker", "native"])
@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("point,occurrence", ADD_FAULTS)
def test_interrupted_add_resumes_with_the_saved_enrolment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    error: type[BaseException],
    point: str,
    occurrence: int,
) -> None:
    env = Env(tmp_path, monkeypatch, mode)
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm(point, occurrence, error)
        with pytest.raises(error):
            workflow.add_garden(ctx, garden_options())
        expected_status = "interrupted" if error is KeyboardInterrupt else "failed"
        assert ctx.state["status"] == expected_status
        assert ctx.state["garden"]["origin"] == "added"
        assert ctx.state["garden"]["add"] == {"phase": "recorded"}
        assert ctx.state["verified_recheck"] is True

        assert workflow.run_install(ctx)["status"] == "verified"

        assert ctx.state["garden"]["add"] == {"phase": "complete"}
        assert ctx.state["steps"]["garden_restart"] == "complete"
        assert "last_error" not in ctx.state
    # Replays reuse one saved key, so the fake Cairn never stores a second principal.
    assert len(set(env.world.principal_keys)) == 1
    assert len(env.world.principals) == 1
    assert env.world.garden_attached and env.world.garden_running
    assert env.world.garden_data


def test_add_refuses_before_writing_unless_the_installation_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    env.make_verified(status="failed")
    with open_context(env.state_root, "demo") as ctx:
        with pytest.raises(InstallError, match="needs a verified installation"):
            workflow.add_garden(ctx, garden_options())
        assert "garden" not in ctx.state
    assert env.world.calls == []


def test_journal_precedes_every_side_effect_of_the_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("ownership", 1, KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            workflow.add_garden(ctx, garden_options())
    assert env.world.calls == ["ownership"]
    assert env.world.snapshots[0]["origin"] == "added"


@pytest.mark.parametrize("mode", ["docker", "native"])
@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("point,occurrence,phase", REMOVE_FAULTS)
@pytest.mark.parametrize("delete_data", [False, True])
def test_interrupted_remove_reruns_to_the_requested_data_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    error: type[BaseException],
    point: str,
    occurrence: int,
    phase: str,
    delete_data: bool,
) -> None:
    env = Env(tmp_path, monkeypatch, mode)
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm(point, occurrence, error)
        with pytest.raises(error):
            workflow.remove_garden(ctx, delete_data=delete_data)
        assert ctx.state["garden"]["remove"] == {
            "phase": phase,
            "delete_data": delete_data,
        }

        result = workflow.remove_garden(ctx, delete_data=delete_data)

        assert result["status"] == "verified"
        assert "garden" not in ctx.state
        assert "verified_recheck" not in ctx.state
        [archived] = ctx.state["garden_history"]
        assert archived["origin"] == "added"
        assert archived["remove"] == {"phase": "detached", "delete_data": delete_data}
        assert archived["removed_at"].endswith("Z")
    # Revocation reuses one saved key; the fake Cairn revokes that single credential.
    assert len(set(env.world.revoke_keys)) == 1
    assert env.world.revoked == set(env.world.revoke_keys)
    assert env.world.detach_requests[-1] is delete_data
    assert env.world.garden_attached is False
    assert env.world.garden_running is False
    assert env.world.garden_data is (not delete_data)


@pytest.mark.parametrize("mode", ["docker", "native"])
def test_clean_remove_revokes_before_detaching_and_keeps_data_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env = Env(tmp_path, monkeypatch, mode)
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        workflow.remove_garden(ctx, delete_data=False)
        assert ctx.state["status"] == "verified"
        assert ctx.state["garden_history"][0]["remove"]["phase"] == "detached"
    assert env.world.calls == REMOVE_CALLS
    assert env.world.garden_data is True


def test_remove_may_add_delete_data_only_before_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("revoke", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.remove_garden(ctx, delete_data=False)
        # Before detachment the recorded policy may still become delete.
        workflow.remove_garden(ctx, delete_data=True)
        assert ctx.state["garden_history"][0]["remove"]["delete_data"] is True
    assert env.world.garden_data is False


def test_delete_data_is_frozen_once_backend_detachment_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("garden_detach", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.remove_garden(ctx, delete_data=False)
        assert ctx.state["garden"]["remove"]["phase"] == "detaching"
        before = dict(ctx.state["garden"]["remove"])
        with pytest.raises(InstallError, match="already started keeping its data"):
            workflow.remove_garden(ctx, delete_data=True)
        # The refusal writes nothing, so the original choice still finishes.
        assert ctx.state["garden"]["remove"] == before
        workflow.remove_garden(ctx, delete_data=False)
        assert ctx.state["garden_history"][0]["remove"]["delete_data"] is False
    assert env.world.detach_requests == [False, False]
    assert env.world.garden_data is True


def test_recorded_delete_data_cannot_revert_to_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("revoke", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.remove_garden(ctx, delete_data=True)
        with pytest.raises(InstallError, match="already recorded --delete-data"):
            workflow.remove_garden(ctx, delete_data=False)
        assert ctx.state["garden"]["remove"]["delete_data"] is True


def test_detached_keep_cannot_be_upgraded_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("verify_reads", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.remove_garden(ctx, delete_data=False)
        assert ctx.state["garden"]["remove"]["phase"] == "detached"
        with pytest.raises(InstallError, match="cannot be deleted now"):
            workflow.remove_garden(ctx, delete_data=True)
        assert ctx.state["garden"]["remove"]["delete_data"] is False


def test_remove_refuses_an_incomplete_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("start", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.add_garden(ctx, garden_options())
        with pytest.raises(InstallError, match="Garden add is incomplete"):
            workflow.remove_garden(ctx, delete_data=False)
        assert "remove" not in ctx.state["garden"]


def test_rollback_during_an_incomplete_add_still_resumes_the_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("garden_attach", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.add_garden(ctx, garden_options())
        assert workflow.rollback_install(ctx)["status"] == "rolled_back"
        assert ctx.state["verified_recheck"] is True
        assert workflow.run_install(ctx)["status"] == "verified"
        assert ctx.state["garden"]["add"] == {"phase": "complete"}
    assert len(env.world.principals) == 1


def test_rollback_is_refused_while_garden_removal_is_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    with open_context(env.state_root, "demo") as ctx:
        env.faults.arm("revoke", 1, InstallError)
        with pytest.raises(InstallError):
            workflow.remove_garden(ctx, delete_data=False)
        with pytest.raises(InstallError, match="Garden removal is in progress"):
            workflow.rollback_install(ctx)
        assert env.world.running is True


def test_status_reports_added_garden_fields_without_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    verified_with_garden(env)
    monkeypatch.setattr(
        workflow,
        "ready",
        lambda ctx: pytest.fail("status must not probe the live service"),
    )
    with open_context(env.state_root, "demo") as ctx:
        replacement = str(ctx.root / "garden" / "x")
        ctx.state["file_replacements"] = {replacement: {"old": "a", "new": "b"}}
        ctx.save()
        result = workflow.status_install(ctx)

    garden_status = result["garden"]
    assert garden_status["origin"] == "added"
    assert garden_status["version"] == "0.7.21"
    assert garden_status["add_phase"] == "complete"
    assert garden_status["expires_at"].endswith("Z")
    assert garden_status["days_left"] in {6, 7}
    assert "remove_phase" not in garden_status
    assert result["pending_replacements"] == {replacement: {"old": "a", "new": "b"}}


def test_status_reports_an_installed_garden_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "docker")
    env.make_verified()
    with open_context(env.state_root, "demo") as ctx:
        ctx.state["garden"] = {"options": {"endpoint": "https://g.example/mcp"}}
        ctx.save()
        result = workflow.status_install(ctx)
    assert result["garden"]["origin"] == "installed"
    assert "version" not in result["garden"]
    assert "pending_replacements" not in result


# CLI preconditions and confirmation. Workflow operations are replaced by
# recorders; the real precondition helpers run. cli.main reports InstallError
# on stderr and returns 2, so refusals are asserted through that contract.


def cli_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "src" / "cairn").mkdir(parents=True)
    (source / "src" / "cairn" / "__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    return source


def cli_installation(
    root: Path,
    source: Path,
    *,
    mode: str = "native",
    status: str = "verified",
    garden: dict[str, Any] | None = None,
    port: int = 19000,
) -> Path:
    state_root = root / "state"
    with open_context(
        state_root,
        "demo",
        create={
            "source": str(source),
            "mode": mode,
            "port": port,
            "semantic": False,
            "source_fingerprint": cli.source_fingerprint(source),
        },
    ) as ctx:
        ctx.state["status"] = status
        ctx.state["receipts"]["ingest"] = {"proof": "same"}
        if garden is not None:
            ctx.state["garden"] = garden
        ctx.save()
    return state_root


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    calls: list[tuple[str, Any]] = []
    config = tmp_path / "garden.json"
    config.write_text("{}\n")
    options = garden_options()
    monkeypatch.setattr(garden, "load_options", lambda path, mode: dict(options))

    def add_garden(ctx: Any, opts: dict[str, Any]) -> dict[str, str]:
        calls.append(("add", opts))
        return {"status": "verified"}

    def remove_garden(ctx: Any, *, delete_data: bool) -> dict[str, str]:
        calls.append(("remove", delete_data))
        return {"status": "verified"}

    monkeypatch.setattr(workflow, "add_garden", add_garden)
    monkeypatch.setattr(workflow, "remove_garden", remove_garden)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
    )
    return SimpleNamespace(
        root=tmp_path,
        source=cli_source(tmp_path),
        config=config,
        options=options,
        calls=calls,
    )


def add_args(env: SimpleNamespace, state_root: Path, *extra: str) -> list[str]:
    return [
        "garden-add",
        "--name",
        "demo",
        "--garden-config",
        str(env.config),
        "--state-root",
        str(state_root),
        *extra,
    ]


def remove_args(state_root: Path, *extra: str) -> list[str]:
    return ["garden-remove", "--name", "demo", "--state-root", str(state_root), *extra]


def run_cli(
    argv: list[str], source: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    code = cli.main(argv, default_source=source)
    return code, capsys.readouterr().err


def test_add_requires_a_garden_configuration_file(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = run_cli(
        ["garden-add", "--name", "demo", "--state-root", str(cli_env.root)],
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "--garden-config is required for garden-add" in err


@pytest.mark.parametrize(
    "status,garden,match",
    [
        ("failed", None, "needs a verified installation"),
        (
            "verified",
            {"options": {}, "origin": "added", "add": {"phase": "recorded"}},
            "Garden add is incomplete; run resume --name demo",
        ),
        (
            "verified",
            {"options": {}, "origin": "added", "add": {"phase": "complete"}},
            "Garden is already managed",
        ),
        (
            "verified",
            {
                "options": {},
                "origin": "added",
                "add": {"phase": "complete"},
                "remove": {"phase": "stopping", "delete_data": False},
            },
            "Garden removal is in progress; run garden-remove --name demo again",
        ),
    ],
)
def test_add_precondition_refusals(
    cli_env: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    status: str,
    garden: dict[str, Any] | None,
    match: str,
) -> None:
    state_root = cli_installation(
        cli_env.root, cli_env.source, status=status, garden=garden
    )
    code, err = run_cli(add_args(cli_env, state_root), cli_env.source, capsys)
    assert code == 2
    assert match in err
    assert cli_env.calls == []


def test_add_refuses_kubernetes_and_disposable_installations(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    kube = cli_installation(cli_env.root / "kube", cli_env.source, mode="kubernetes")
    code, err = run_cli(add_args(cli_env, kube), cli_env.source, capsys)
    assert code == 2
    assert "Kubernetes garden-add arrives in a later release" in err
    disposable = cli_installation(
        cli_env.root / "disposable", cli_env.source, mode="disposable"
    )
    code, err = run_cli(add_args(cli_env, disposable), cli_env.source, capsys)
    assert code == 2
    assert "needs a native or docker installation" in err
    assert cli_env.calls == []


def test_add_refuses_a_port_collision_and_expired_authority(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source, port=8443)
    code, err = run_cli(add_args(cli_env, state_root), cli_env.source, capsys)
    assert code == 2
    assert "different listening ports" in err
    cli_env.options["port"] = 9443
    cli_env.options["expires_at"] = "2000-01-01T00:00:00.000000Z"
    code, err = run_cli(add_args(cli_env, state_root), cli_env.source, capsys)
    assert code == 2
    assert "authority has expired" in err
    assert cli_env.calls == []


def test_add_refuses_a_different_source_like_resume(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source)
    other = cli_source(cli_env.root / "other")
    code, err = run_cli(
        add_args(cli_env, state_root, "--source", str(other)),
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "Recorded installation options differ" in err
    assert cli_env.calls == []


def test_native_add_needs_no_confirmation_and_passes_added_defaults(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source)
    code, _ = run_cli(
        add_args(cli_env, state_root, "--non-interactive"),
        cli_env.source,
        capsys,
    )
    assert code == 0
    [(operation, options)] = cli_env.calls
    assert operation == "add"
    assert options["stream"] == {"max_age": "168h", "max_bytes": 1024**3}


def test_docker_add_non_interactive_refuses_without_yes(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source, mode="docker")
    code, err = run_cli(
        add_args(cli_env, state_root, "--non-interactive"),
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "--yes is required for garden-add" in err
    assert cli_env.calls == []


def test_docker_add_prompt_answers_decide(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source, mode="docker")
    answers = iter(["n", "Y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    code, err = run_cli(add_args(cli_env, state_root), cli_env.source, capsys)
    assert code == 2
    assert "not confirmed" in err
    assert cli_env.calls == []
    code, _ = run_cli(add_args(cli_env, state_root), cli_env.source, capsys)
    assert code == 0
    assert [name for name, _ in cli_env.calls] == ["add"]


def test_docker_add_announces_the_cairn_restart(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source, mode="docker")
    code = cli.main(
        add_args(cli_env, state_root, "--non-interactive", "--yes"),
        default_source=cli_env.source,
    )
    assert code == 0
    assert "Cairn will be recreated" in capsys.readouterr().out
    assert [name for name, _ in cli_env.calls] == ["add"]


def test_yes_is_limited_to_confirmed_garden_operations(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source)
    code, err = run_cli(
        ["status", "--name", "demo", "--state-root", str(state_root), "--yes"],
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "--yes is only valid with blitz, garden-add and garden-remove" in err


def test_delete_data_is_only_valid_for_garden_remove(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source)
    code, err = run_cli(
        ["status", "--name", "demo", "--state-root", str(state_root), "--delete-data"],
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "--delete-data is only valid with garden-remove" in err


def test_remove_requires_a_managed_garden(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = cli_installation(cli_env.root, cli_env.source)
    code, err = run_cli(remove_args(state_root, "--yes"), cli_env.source, capsys)
    assert code == 2
    assert "not managed by this installation" in err
    assert cli_env.calls == []


def test_remove_non_interactive_needs_yes(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
    }
    state_root = cli_installation(cli_env.root, cli_env.source, garden=garden_state)
    code, err = run_cli(
        remove_args(state_root, "--non-interactive"), cli_env.source, capsys
    )
    assert code == 2
    assert "--yes is required for garden-remove" in err
    assert cli_env.calls == []


def test_remove_prompt_refusal_changes_nothing(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
    }
    state_root = cli_installation(cli_env.root, cli_env.source, garden=garden_state)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    code, err = run_cli(remove_args(state_root), cli_env.source, capsys)
    assert code == 2
    assert "not confirmed" in err
    assert cli_env.calls == []


def test_remove_yes_with_delete_data_records_the_request(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
    }
    state_root = cli_installation(cli_env.root, cli_env.source, garden=garden_state)
    code = cli.main(
        remove_args(state_root, "--yes", "--delete-data"),
        default_source=cli_env.source,
    )
    assert code == 0
    assert cli_env.calls == [("remove", True)]
    assert "permanently deleted" in capsys.readouterr().out


def test_remove_refuses_delete_data_downgrade_before_confirmation(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
        "remove": {"phase": "stopping", "delete_data": True},
    }
    state_root = cli_installation(
        cli_env.root, cli_env.source, status="failed", garden=garden_state
    )
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("no prompt"))
    code, err = run_cli(remove_args(state_root, "--yes"), cli_env.source, capsys)
    assert code == 2
    assert "already recorded --delete-data" in err
    assert cli_env.calls == []


def test_docker_remove_announces_the_cairn_restart(
    cli_env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
    }
    state_root = cli_installation(
        cli_env.root, cli_env.source, mode="docker", garden=garden_state
    )
    code = cli.main(
        remove_args(state_root, "--non-interactive", "--yes"),
        default_source=cli_env.source,
    )
    assert code == 0
    assert "Cairn will be recreated to detach Garden" in capsys.readouterr().out
    assert cli_env.calls == [("remove", False)]


def test_resume_refuses_while_garden_removal_is_in_progress(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    garden_state = {
        "options": garden_options(),
        "origin": "added",
        "add": {"phase": "complete"},
        "remove": {"phase": "stopping", "delete_data": False},
    }
    state_root = cli_installation(
        cli_env.root, cli_env.source, status="failed", garden=garden_state
    )
    monkeypatch.setattr(workflow, "run_install", lambda ctx: pytest.fail("no resume"))
    code, err = run_cli(
        ["resume", "--name", "demo", "--state-root", str(state_root)],
        cli_env.source,
        capsys,
    )
    assert code == 2
    assert "Garden removal is in progress; run garden-remove --name demo again" in err


def test_resume_completes_an_interrupted_add_without_a_config_file(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    garden_state = {
        "options": garden.with_add_defaults(garden_options()),
        "origin": "added",
        "add": {"phase": "recorded"},
        "source": {
            "path": str(cli_env.source),
            "garden_fingerprint": "fp-1",
            "version": "0.7.21",
        },
    }
    state_root = cli_installation(
        cli_env.root, cli_env.source, status="failed", garden=garden_state
    )
    seen: list[str] = []
    monkeypatch.setattr(workflow, "run_install", lambda ctx: seen.append(ctx.name))
    code, _ = run_cli(
        ["resume", "--name", "demo", "--state-root", str(state_root)],
        cli_env.source,
        capsys,
    )
    assert code == 0
    assert seen == ["demo"]


def test_resume_with_the_config_file_matches_the_added_defaults(
    cli_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Reuse one options dict: garden_options() stamps expires_at from the clock.
    garden_state = {
        "options": garden.with_add_defaults(dict(cli_env.options)),
        "origin": "added",
        "add": {"phase": "recorded"},
    }
    state_root = cli_installation(
        cli_env.root, cli_env.source, status="failed", garden=garden_state
    )
    monkeypatch.setattr(workflow, "run_install", lambda ctx: None)
    resume = [
        "resume",
        "--name",
        "demo",
        "--garden-config",
        str(cli_env.config),
        "--state-root",
        str(state_root),
    ]
    code, _ = run_cli(resume, cli_env.source, capsys)
    assert code == 0
    cli_env.options["endpoint"] = "https://replacement.example.test/mcp"
    code, err = run_cli(resume, cli_env.source, capsys)
    assert code == 2
    assert "Recorded Garden options differ" in err


@pytest.mark.parametrize("mode", ["docker", "native"])
@pytest.mark.parametrize("state", ["added-complete", "added-incomplete", "removed"])
def test_blitz_deletes_an_installation_with_an_added_garden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    state: str,
) -> None:
    fake = Env(tmp_path, monkeypatch, mode)
    if state == "added-incomplete":
        fake.make_verified()
        with open_context(fake.state_root, "demo") as ctx:
            fake.faults.arm("start", 1, InstallError)
            with pytest.raises(InstallError):
                workflow.add_garden(ctx, garden_options())
    else:
        verified_with_garden(fake)
        if state == "removed":
            with open_context(fake.state_root, "demo") as ctx:
                workflow.remove_garden(ctx, delete_data=True)
    fake.world.calls.clear()

    with open_context(fake.state_root, "demo") as ctx:
        if state == "removed":
            assert "garden" not in ctx.state
            assert len(ctx.state["garden_history"]) == 1
        else:
            assert ctx.state["garden"]["origin"] == "added"
        result = workflow.blitz_install(ctx)
        assert result["status"] == "deleted"
        assert not ctx.directory.exists()

    assert "blitz" in fake.world.calls
    expects_garden_blitz = mode == "native" and state != "removed"
    assert ("garden_blitz" in fake.world.calls) is expects_garden_blitz
