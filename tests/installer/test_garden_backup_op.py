"""Backup journals and real tar verification with a simulated Garden backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn_install import cli, garden, garden_backup, garden_upgrade, native, workflow
from cairn_install.core import Context, InstallError, open_context


class World:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.snapshots: dict[str, str] = {}
        self.created = 0
        self.deleted: set[str] = set()
        self.running = True
        self.generation = "generation-one"
        self.fault: tuple[str, type[BaseException]] | None = None
        self.host = b'{"gateway":{"data_dir":"/var/lib/garden/data"}}\n'
        self.bad_export = False

    def hit(self, point: str) -> None:
        self.calls.append(point)
        if self.fault and self.fault[0] == point:
            _, error = self.fault
            self.fault = None
            raise error(f"injected {point}")

    def garden_stop(self) -> None:
        self.running = False
        self.hit("stop")

    def garden_start(self) -> None:
        self.running = True
        self.hit("start")

    def garden_open_endpoint(self) -> None:
        self.hit("open")

    def garden_close_endpoint(self) -> None:
        self.calls.append("close_endpoint")

    def garden_snapshot(self, name: str) -> str:
        assert not self.running
        if name not in self.snapshots:
            self.snapshots[name] = "20261020T010203Z-" + name
            self.created += 1
        self.hit("snapshot")
        return self.snapshots[name]

    def garden_export(self, snapshot_id: str, dest: Path) -> None:
        assert self.running
        assert snapshot_id not in self.deleted
        self.hit("export")
        payload = {
            "host.json": self.host,
            "data/.garden-owned": b"owned",
            "data/state.db": b"fixture data",
        }
        entries = [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": 0o600,
            }
            for path, content in sorted(payload.items())
        ]
        manifest = {
            "format_version": 1,
            "id": snapshot_id,
            "binary_version": "0.8.0",
            "config_file": "host.json",
            "config_sha256": hashlib.sha256(self.host).hexdigest(),
            "state_schema_version": 1,
            "memory_schema_version": 1,
            "files": entries,
            "total_bytes": sum(len(content) for content in payload.values()),
        }
        for path, content in {
            **payload,
            "manifest.json": json.dumps(manifest).encode(),
        }.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if self.bad_export:
            (dest / "data/state.db").write_bytes(b"tampered")

    def garden_snapshot_delete(self, snapshot_id: str) -> None:
        self.deleted.add(snapshot_id)
        self.hit("delete")

    def close(self) -> None:
        self.calls.append("close")


class Env:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str = "native",
        name: str = "demo",
    ) -> None:
        self.state_root = tmp_path / "state"
        self.output = tmp_path / "output"
        self.source = tmp_path / "source"
        self.world = World()
        self.name = name
        self.phase_fault: tuple[str, type[BaseException]] | None = None
        self.original_save = Context.save
        self.source.mkdir()
        self.unit = tmp_path / "units"
        self.unit.mkdir(mode=0o700)
        monkeypatch.setattr(native, "_unit_directory", lambda: self.unit)
        monkeypatch.setattr(workflow, "backend", lambda ctx: self.world)
        monkeypatch.setattr(workflow, "_garden_adapter", lambda ctx, adapter: adapter)
        monkeypatch.setattr(garden, "verify", self.verify)
        monkeypatch.setattr(Context, "save", lambda ctx: self.save(ctx))
        with open_context(
            self.state_root,
            name,
            create={
                "source": str(self.source),
                "mode": mode,
                "port": 19000,
                "semantic": False,
            },
        ) as ctx:
            ctx.state["status"] = "verified"
            ctx.state["garden"] = {"source": {"version": "0.8.0"}}
            for path, content in {
                ctx.root / "garden/host.json": self.world.host.decode(),
                ctx.root / "garden/tls/server.crt": "fixture cert",
                ctx.root / "garden/tls/server.key": "fixture key",
                ctx.root / "garden/tls/ca.crt": "fixture ca",
                ctx.root / "compose.yaml": "services: {}\n",
                self.unit / f"cairn-install-{name}-garden.service": "[Service]\n",
            }.items():
                ctx.write_file(path, content)
            ctx.save()

    def verify(self, ctx: Context) -> None:
        assert self.world.running
        self.world.hit("verify")
        ctx.state["garden"]["verification"] = {
            "val": {"generation": self.world.generation}
        }
        ctx.save()

    def save(self, ctx: Context) -> None:
        self.original_save(ctx)
        phase = ctx.state.get("garden", {}).get("backup", {}).get("phase")
        if self.phase_fault and self.phase_fault[0] == phase:
            _, error = self.phase_fault
            self.phase_fault = None
            raise error(f"injected after {phase} save")

    def run(self, *, output: Path | None = None, yes: bool = True) -> dict[str, Any]:
        result = garden_backup.run(
            argparse.Namespace(
                name=self.name,
                output=output,
                yes=yes,
                non_interactive=True,
                verbose=False,
            ),
            self.state_root,
        )
        assert isinstance(result, dict)
        return result

    def state(self) -> dict[str, Any]:
        value = json.loads((self.state_root / self.name / "state.json").read_text())
        assert isinstance(value, dict)
        return value

    def update(self, action: Any) -> None:
        with open_context(self.state_root, self.name) as ctx:
            action(ctx.state)
            ctx.save()


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_archive_is_accepted_and_tampering_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = Env(tmp_path, monkeypatch, mode)
    result = env.run()
    archive = Path(result["archive"])
    tools = garden_backup._archive_tools()
    assert (
        tools.verify_garden(archive.parent, archive.stem)["config_file"] == "host.json"
    )
    assert archive.parent == env.state_root / "demo/garden-backups"
    assert "deleted by blitz" in capsys.readouterr().out
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    assert (
        archive.with_suffix(".tar.sha256").read_text()
        == f"{result['sha256']}  {archive.name}\n"
    )
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "tls/ca.crt" in names
        assert ("compose.yaml" in names) == (mode == "docker")
        assert ("unit/cairn-install-demo-garden.service" in names) == (mode == "native")
    state = env.state()
    assert state["garden"]["backup"]["phase"] == "complete"
    assert len(state["garden"]["backups"]) == 1
    assert env.world.created == len(env.world.deleted) == 1
    assert (
        env.world.calls.index("start")
        < env.world.calls.index("export")
        < env.world.calls.index("delete")
    )
    assert env.world.running
    with archive.open("r+b") as stream:
        stream.seek(1024)
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        tools.verify_garden(archive.parent, archive.stem)


@pytest.mark.parametrize(
    "phase", ["recorded", "stopped", "snapshotted", "started", "archived", "complete"]
)
@pytest.mark.parametrize("error", [InstallError, KeyboardInterrupt])
def test_interrupt_after_each_phase_then_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: type[BaseException],
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = (phase, error)
    with pytest.raises(error):
        env.run()
    before = env.state()["garden"]["backup"]
    assert before["phase"] == phase
    assert env.state()["status"] == (
        "interrupted" if error is KeyboardInterrupt else "failed"
    )
    assert "run garden-backup --name demo again" in env.state()["last_error"]
    assert env.world.running
    if phase in {"stopped", "snapshotted"}:
        assert env.world.calls[-3:] == ["verify", "close_endpoint", "close"]
    env.run()
    state = env.state()
    assert state["garden"]["backup"]["id"] == before["id"]
    assert state["garden"]["backup"]["phase"] == "complete"
    assert env.world.created == 1
    assert len(env.world.deleted) == len(state["garden"]["backups"]) == 1
    assert state["status"] == "verified"
    assert "last_error" not in state


@pytest.mark.parametrize("point", ["stop", "snapshot", "start", "export", "delete"])
def test_interrupt_after_hook_side_effect_then_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.fault = (point, KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run(output=env.output)
    assert env.world.running
    if point in {"stop", "snapshot"}:
        assert env.world.calls[-3:] == ["verify", "close_endpoint", "close"]
    env.run()  # Retain an explicit output even when omitted on the retry.
    assert env.world.created == 1
    assert len(env.world.deleted) == 1
    assert Path(env.state()["garden"]["backup"]["archive"]).parent == env.output


@pytest.mark.parametrize("point", ["stop", "snapshot"])
@pytest.mark.parametrize(
    "error", [InstallError, OSError, RuntimeError, KeyboardInterrupt]
)
def test_hook_failure_recovers_before_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error: type[BaseException],
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.fault = (point, error)
    with pytest.raises(
        KeyboardInterrupt if error is KeyboardInterrupt else InstallError
    ):
        env.run()
    assert env.world.running
    assert env.world.calls[-5:] == [
        "start",
        "open",
        "verify",
        "close_endpoint",
        "close",
    ]
    assert not env.world.deleted
    env.run()
    assert env.world.created == len(env.world.deleted) == 1


@pytest.mark.parametrize("phase", ["stopped", "snapshotted"])
def test_journal_failure_before_write_recovers_before_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    save = Context.save

    def interrupt(ctx: Context) -> None:
        if ctx.state["garden"].get("backup", {}).get("phase") == phase:
            monkeypatch.setattr(Context, "save", save)
            raise OSError("journal persistence failed")
        save(ctx)

    monkeypatch.setattr(Context, "save", interrupt)
    with pytest.raises(InstallError):
        env.run()
    assert env.world.running
    assert env.world.calls[-3:] == ["verify", "close_endpoint", "close"]
    env.run()
    assert env.world.created == len(env.world.deleted) == 1


@pytest.mark.parametrize("point", ["start", "verify"])
def test_recovery_failure_keeps_original_error_and_attempts_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    stop = env.world.garden_stop

    def fail_stop() -> None:
        stop()
        env.world.fault = (point, InstallError)
        raise InstallError("original stop failure")

    monkeypatch.setattr(env.world, "garden_stop", fail_stop)
    with pytest.raises(InstallError, match="original stop failure"):
        env.run()
    state = env.state()
    assert state["status"] == "failed"
    assert "original stop failure" in state["last_error"]
    assert f"injected {point}" in state["last_error"]
    assert env.world.calls[-5:] == [
        "start",
        "open",
        "verify",
        "close_endpoint",
        "close",
    ]
    assert env.world.running
    monkeypatch.setattr(env.world, "garden_stop", stop)
    env.run()
    assert env.world.created == len(env.world.deleted) == 1


def test_snapshot_created_before_journal_reuses_backup_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.fault = ("snapshot", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    backup = env.state()["garden"]["backup"]
    assert backup["snapshot_id"] is None
    snapshots = dict(env.world.snapshots)
    assert list(snapshots) == [f"demo-{backup['id'].lower()}"]
    assert env.world.created == 1
    env.run()
    assert env.world.snapshots == snapshots
    assert env.world.created == 1
    assert env.world.deleted == set(snapshots.values())


@pytest.mark.parametrize("phase", ["snapshotted", "started", "archived", "complete"])
@pytest.mark.parametrize("changed", [False, True])
def test_resumed_backup_restarts_and_checks_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, changed: bool
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = (phase, KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    archive = Path(env.state()["garden"]["backup"]["archive"])
    published = archive.read_bytes() if archive.exists() else None
    env.world.running = False  # Simulate a host reboot between attempts.
    env.world.calls.clear()
    if changed:
        env.world.generation = "changed-after-reboot"
        with pytest.raises(InstallError, match="generation changed"):
            env.run()
        assert not {"export", "delete"}.intersection(env.world.calls)
        assert (archive.read_bytes() if archive.exists() else None) == published
        assert env.state()["status"] == "failed"
        env.world.generation = "generation-one"
        env.run()
    else:
        env.run()
        assert env.world.calls.index("start") < env.world.calls.index("verify")
        for point in ("export", "delete"):
            if point in env.world.calls:
                assert env.world.calls.index("verify") < env.world.calls.index(point)
    assert env.world.running
    assert env.state()["garden"]["backup"]["phase"] == "complete"


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("version", ["0.7.20", "0.8.0-rc1", "garbage"])
def test_version_refusal_has_no_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, recorded: bool
) -> None:
    env = Env(tmp_path, monkeypatch)
    if recorded:
        env.update(lambda state: state["garden"].update(source={"version": version}))
    else:
        # Installed with Cairn: the version comes from the proven back-fill.
        env.update(lambda state: state["garden"].pop("source"))
        monkeypatch.setattr(
            garden_upgrade, "_source", lambda ctx, adapter: {"version": version}
        )
    before = env.state()
    with pytest.raises(InstallError, match="garden-upgrade first"):
        env.run()
    assert env.state() == before
    # The back-fill proof opens the backend; nothing touches Garden.
    assert set(env.world.calls) <= {"close"}


def test_garden_installed_with_cairn_backs_up_after_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.update(lambda state: state["garden"].pop("source"))
    proved: list[str] = []

    def source(ctx: Context, adapter: Any) -> dict[str, Any]:
        proved.append(ctx.name)
        return {"version": "0.8.0"}

    monkeypatch.setattr(garden_upgrade, "_source", source)
    env.run()
    assert proved
    assert env.state()["garden"]["backup"]["phase"] == "complete"
    assert "source" not in env.state()["garden"]


def test_unsupported_modes_refuse_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, "kubernetes")
    before = env.state()
    with pytest.raises(InstallError, match="later release"):
        env.run()
    assert env.state() == before
    assert not env.world.calls


@pytest.mark.parametrize("name", ["a" * 17, "abcdefgh-ijklmnopq"])
def test_invalid_label_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    env = Env(tmp_path, monkeypatch, name=name)
    with pytest.raises(InstallError, match="cannot form a label"):
        env.run()
    assert not env.world.calls


def test_hyphenated_name_forms_stripped_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch, name="nat-gup")
    env.run()
    (snapshot_id,) = env.world.snapshots.values()
    tools = garden_backup._archive_tools()
    match = tools.LABELLED_SNAPSHOT.fullmatch(snapshot_id)
    assert match and match["label"] == "natgup"
    assert env.state()["garden"]["backup"]["phase"] == "complete"


def test_confirmation_required_before_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    before = env.state()
    with pytest.raises(InstallError, match="--yes is required"):
        env.run(yes=False)
    assert env.state() == before
    assert not env.world.calls
    assert not (env.state_root / "demo/garden-backups").exists()


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "parent-symlink", "file"])
def test_output_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    if unsafe == "file":
        env.output.write_text("unowned")
    else:
        env.output.mkdir(mode=0o755 if unsafe == "mode" else 0o700)
    output = env.output
    if unsafe in {"symlink", "parent-symlink"}:
        link = tmp_path / "link"
        link.symlink_to(output, target_is_directory=True)
        output = link if unsafe == "symlink" else link / "child"
    before = env.state()
    with pytest.raises(InstallError):
        env.run(output=output)
    assert env.state() == before
    assert not env.world.calls


def test_changed_output_refused_midflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.fault = ("snapshot", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run(output=env.output)
    with pytest.raises(InstallError, match="original --output"):
        env.run(output=tmp_path / "elsewhere")
    env.run(output=env.output)


def test_changed_generation_never_archives_or_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = ("started", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    env.world.generation = "different-generation"
    with pytest.raises(InstallError, match="generation changed"):
        env.run()
    assert "export" not in env.world.calls
    assert not env.world.deleted
    env.world.generation = "generation-one"
    env.run()
    assert len(env.world.deleted) == 1


def test_invalid_export_never_deletes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.bad_export = True
    with pytest.raises(InstallError, match="backup failed"):
        env.run()
    assert env.world.running
    assert not env.world.deleted
    assert env.state()["garden"]["backup"]["phase"] == "started"
    assert not Path(env.state()["garden"]["backup"]["archive"]).exists()
    env.world.bad_export = False
    env.run()
    assert len(env.world.deleted) == 1


def test_tampered_archived_publication_never_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = ("archived", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    archive = Path(env.state()["garden"]["backup"]["archive"])
    archive.write_bytes(b"tampered")
    with pytest.raises(InstallError):
        env.run()
    assert not env.world.deleted


def test_archive_resume_between_publications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    link = os.link

    def interrupt(source: Path, destination: Path) -> None:
        if destination.suffix == ".sha256":
            monkeypatch.setattr(os, "link", link)
            raise KeyboardInterrupt
        link(source, destination)

    monkeypatch.setattr(os, "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert env.state()["garden"]["backup"]["phase"] == "started"
    env.run()
    assert env.world.calls.count("export") == 1
    assert len(env.world.deleted) == 1


@pytest.mark.parametrize("kind", ["tar", "sidecar", "sidecar-symlink"])
def test_retry_refuses_unjournalled_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = ("started", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    archive = Path(env.state()["garden"]["backup"]["archive"])
    path = archive if kind == "tar" else archive.with_suffix(".tar.sha256")
    original = b"unrelated publication must survive\n"
    if kind == "sidecar-symlink":
        target = tmp_path / "foreign-checksum"
        target.write_bytes(original)
        path.symlink_to(target)
    else:
        path.write_bytes(original)
        path.chmod(0o600)
    env.world.calls.clear()
    with pytest.raises(InstallError):
        env.run()
    assert path.read_bytes() == original
    assert path.is_symlink() == (kind == "sidecar-symlink")
    assert not {"export", "delete"}.intersection(env.world.calls)
    assert env.state()["garden"]["backup"]["sha256"] is None


@pytest.mark.parametrize("tar_published", [False, True])
def test_sidecar_publication_race_preserves_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tar_published: bool
) -> None:
    env = Env(tmp_path, monkeypatch)
    link = os.link
    checksum = b"foreign checksum arriving during publication\n"

    def collide(source: Path, destination: Path) -> None:
        if destination.suffix == ".sha256":
            if tar_published:
                monkeypatch.setattr(os, "link", collide_again)
                raise KeyboardInterrupt
            destination.write_bytes(checksum)
            destination.chmod(0o600)
        link(source, destination)

    def collide_again(source: Path, destination: Path) -> None:
        if destination.suffix == ".sha256":
            destination.write_bytes(checksum)
            destination.chmod(0o600)
        link(source, destination)

    monkeypatch.setattr(os, "link", collide)
    if tar_published:
        with pytest.raises(KeyboardInterrupt):
            env.run()
    with pytest.raises(InstallError):
        env.run()
    archive = Path(env.state()["garden"]["backup"]["archive"])
    assert archive.with_suffix(".tar.sha256").read_bytes() == checksum
    assert not env.world.deleted


def test_cli_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, monkeypatch)
    assert (
        cli.main(
            [
                "garden-backup",
                "--name",
                "demo",
                "--state-root",
                str(env.state_root),
                "--yes",
                "--non-interactive",
            ]
        )
        == 0
    )
    assert env.state()["garden"]["backup"]["phase"] == "complete"


def test_output_foreign_owner_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.output.mkdir(mode=0o700)
    original_stat = Path.stat

    def foreign_stat(path: Path, **kwargs: Any) -> Any:
        info = original_stat(path, **kwargs)
        if path == env.output:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
        return info

    monkeypatch.setattr(Path, "stat", foreign_stat)
    before = env.state()
    with pytest.raises(InstallError, match="owned by you"):
        env.run(output=env.output)
    assert env.state() == before
    assert not env.world.calls


def test_history_retains_ten_receipts_without_deleting_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.update(
        lambda state: state["garden"].update(
            backups=[{"id": str(index), "archive": str(index)} for index in range(12)]
        )
    )
    result = env.run()
    history = env.state()["garden"]["backups"]
    assert len(history) == 10
    assert [item["id"] for item in history[:-1]] == [
        str(index) for index in range(3, 12)
    ]
    assert history[-1]["archive"] == result["archive"]
    assert history[-1]["phase"] == "complete"
    assert Path(result["archive"]).exists()


@pytest.mark.parametrize("operation", ["upgrade", "add", "remove"])
def test_pending_other_operation_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.update(lambda state: state["garden"].update({operation: {"phase": "recorded"}}))
    before = env.state()
    with pytest.raises(InstallError, match="in progress"):
        env.run()
    assert env.state() == before
    assert not env.world.calls


def test_no_ca_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, monkeypatch)
    (env.state_root / "demo/instance/garden/tls/ca.crt").unlink()
    result = env.run()
    with tarfile.open(result["archive"]) as archive:
        assert "tls/ca.crt" not in archive.getnames()


def test_unowned_deployment_refused_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)

    def forget(state: dict[str, Any]) -> None:
        path = str(env.unit / "cairn-install-demo-garden.service")
        state["owned_files"].pop(path, None)
        state.get("file_intents", {}).pop(path, None)

    env.update(forget)
    with pytest.raises(InstallError, match="unowned"):
        env.run()
    assert "stop" not in env.world.calls


def test_snapshot_id_must_match_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(env.world, "garden_snapshot", lambda name: "../../escape")
    with pytest.raises(InstallError, match="snapshot ID differs"):
        env.run()
    assert not env.world.deleted
    assert "export" not in env.world.calls


def test_generation_required_before_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.generation = ""
    with pytest.raises(InstallError, match="no generation"):
        env.run()
    assert "stop" not in env.world.calls


def test_completed_backup_cannot_mask_unrelated_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.run()
    env.update(
        lambda state: state.update(
            status="failed", last_error="unrelated Cairn failure"
        )
    )
    before = env.state()
    env.world.calls.clear()
    with pytest.raises(InstallError, match="run resume"):
        env.run()
    assert env.state() == before
    assert not env.world.calls


def test_same_second_does_not_reuse_snapshot_even_with_another_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    monkeypatch.setattr(garden_backup, "datetime", SimpleNamespace(now=lambda tz: now))
    env.run()
    with pytest.raises(InstallError, match="already exists for this second"):
        env.run(output=env.output)
    assert env.world.created == 1
    assert len(env.state()["garden"]["backups"]) == 1


def test_failed_initial_verification_can_retry_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.world.fault = ("verify", InstallError)
    with pytest.raises(InstallError):
        env.run()
    assert env.state()["garden"]["backup"]["phase"] == "recorded"
    assert "stop" not in env.world.calls
    env.run()
    assert env.world.created == 1


def test_no_managed_garden_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.update(lambda state: state.pop("garden"))
    before = env.state()
    with pytest.raises(InstallError, match="No managed Garden"):
        env.run()
    assert env.state() == before


def test_completion_is_one_durable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, monkeypatch)
    env.phase_fault = ("archived", KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    save = Context.save
    commits: list[str] = []

    def atomic_completion(ctx: Context) -> None:
        if ctx.state["garden"]["backup"]["phase"] == "complete":
            # Termination immediately after any save must leave recoverable state.
            assert ctx.state["status"] == "verified"
            assert "last_error" not in ctx.state
            assert ctx.state["garden"]["backups"][-1]["phase"] == "complete"
            commits.append("complete")
        save(ctx)

    monkeypatch.setattr(Context, "save", atomic_completion)
    env.run()
    assert commits == ["complete"]
    assert env.world.created == 1


def test_packaged_installer_bundles_the_archive_verifier(tmp_path: Path) -> None:
    import subprocess
    import sys
    import zipfile

    repository = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=repository,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert built.returncode == 0, "wheel build failed"
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        for name in ("backup_common.py", "garden_common.py"):
            member = f"cairn_install/backup_tools/{name}"
            assert (
                archive.read(member)
                == (repository / "scripts" / "backup" / name).read_bytes()
            )
        archive.extractall(site)
    # Outside any source tree, the installer must load its bundled copy.
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cairn_install.garden_backup import _archive_tools; "
            "print(_archive_tools().__file__)",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert loaded.stdout.strip() == str(
        site / "cairn_install" / "backup_tools" / "garden_common.py"
    )
