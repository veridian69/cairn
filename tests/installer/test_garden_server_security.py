"""Offline security regressions for the server policy and generated backup hook."""

from __future__ import annotations

import grp
import hashlib
import io
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn_install import garden_server as server
from cairn_install import sudo
from cairn_install.core import Context, InstallError

from .test_garden_server import binding

ROOT = Path(__file__).parents[2]
STAMP = "20261020T120000Z"
NAME = f"garden-backup-{STAMP}"


def archive_bytes() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("snapshot-id/payload")
        member.size = len(b"archive-bytes")
        archive.addfile(member, io.BytesIO(b"archive-bytes"))
    return output.getvalue()


@pytest.fixture
def hook_context(tmp_path: Path) -> Context:
    config = binding(tmp_path)
    config["tools"] = {name: f"/usr/bin/{name}" for name in sudo.TOOLS}
    # As on a real host, /opt/cairn-v1 belongs to export_owner, mode 0700.
    parent = tmp_path / "opt" / "cairn-v1"
    parent.mkdir(parents=True, mode=0o700)
    config["backup_hook"].update(
        export_dir=str(parent / "garden-exports"),
        export_owner=pwd.getpwuid(os.getuid()).pw_name,
    )
    return Context(
        tmp_path,
        {"mode": "garden-server", "source": str(ROOT), "server": config},
        -1,
    )


def test_sudoers_header_states_root_equivalent_authority(hook_context: Context) -> None:
    header = sudo.snippet(hook_context).split("NOPASSWD:", 1)[0]
    assert "root-equivalent" in header
    assert "not a containment boundary" in header
    assert "exact commands" in header and "allowlist complete" in header


def test_system_tool_ignores_user_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "true"
    fake.write_text("#!/bin/sh\nexit 99\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    assert shutil.which("true") == str(fake)
    assert server._system_tool("true") == str(Path("/usr/bin/true").resolve())


@pytest.mark.parametrize("name", ["install", "sudo"])
@pytest.mark.parametrize("unsafe", ["tool_owner", "ancestor_owner", "ancestor_mode"])
def test_system_tool_rejects_unsafe_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, unsafe: str
) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    link = tmp_path / name
    link.symlink_to(tool)
    original_stat = Path.stat
    reject = False

    def info(path: Path, *, follow_symlinks: bool = True) -> SimpleNamespace:
        actual = original_stat(path, follow_symlinks=follow_symlinks)
        owner = 0
        mode = actual.st_mode & ~0o022
        if reject and path == tool and unsafe == "tool_owner":
            owner = os.getuid() or 1000
        if reject and path == directory:
            if unsafe == "ancestor_owner":
                owner = os.getuid() or 1000
            if unsafe == "ancestor_mode":
                mode |= 0o020
        return SimpleNamespace(st_uid=owner, st_mode=mode)

    def which(command: str, *, path: str) -> str:
        assert command == name
        assert path == "/usr/bin:/usr/sbin:/bin:/sbin"
        return str(link)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(Path, "stat", info)
    assert server._system_tool(name) == str(tool)
    reject = True
    with pytest.raises(InstallError, match="Unsafe system executable"):
        server._system_tool(name)


def sandbox_hook(
    ctx: Context, tmp_path: Path, *, change: str = "", tar_body: str = ""
) -> str:
    server._prepare_hook(ctx)
    script = (sudo.stage_root(ctx) / "usr/local/bin/garden-backup").read_text()
    # Only service/snapshot/tar commands are simulated; publication is real.
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    source = tmp_path / "source.tar"
    source.write_bytes(archive_bytes())
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    tar = binaries / "tar"
    tar.write_text(
        "#!/bin/bash\n" + (tar_body or f"cat -- {shlex.quote(str(source))}") + "\n"
    )
    tar.chmod(0o755)
    shims = f"""
date() {{ printf '%s\\n' {STAMP}; }}
systemctl() {{ [ "$1" != is-active ]; }}
runuser() {{
  {change or ":"}
  printf 'snapshot created: snapshot-id (offline)\\n'
}}
"""
    return (
        script.replace("export PATH\n", "export PATH\n" + shims)
        .replace("PATH=/usr/sbin", f"PATH={shlex.quote(str(binaries))}:/usr/sbin")
        .replace("/run/garden-backup", str(runtime))
        .replace("/etc/garden/upgrade-in-progress", str(tmp_path / "upgrading"))
    )


@pytest.mark.parametrize("upgrading", [True, False])
def test_backup_hook_refuses_during_upgrade_window(
    hook_context: Context, tmp_path: Path, upgrading: bool
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    if upgrading:
        (tmp_path / "upgrading").touch()
    calls = tmp_path / "calls"
    script = sandbox_hook(hook_context, tmp_path).replace(
        "runuser() {",
        f'systemctl() {{ echo "systemctl $*" >> {shlex.quote(str(calls))};'
        ' [ "$1" != is-active ]; }\nrunuser() {',
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    if upgrading:
        assert result.returncode == 1
        assert "Garden upgrade in progress" in result.stderr
        # Garden is neither stopped nor snapshotted, and nothing is published.
        assert not calls.exists()
        assert not list(out.iterdir())
    else:
        assert result.returncode == 0, result.stderr
        assert "systemctl stop garden-daemon.service" in calls.read_text()
        assert (out / "latest").read_text() == NAME + "\n"


def test_backup_hook_streams_large_archive(
    hook_context: Context, tmp_path: Path
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    # A real tar stream larger than the publisher's address-space allowance.
    producer = """
import sys
import tarfile
size = 80 * 1024 * 1024
member = tarfile.TarInfo("snapshot-id/payload")
member.size = size
sys.stdout.buffer.write(member.tobuf())
for _ in range(size // (1024 * 1024)):
    sys.stdout.buffer.write(b"x" * (1024 * 1024))
sys.stdout.buffer.write(b"\\0" * (10240 - (size + 512) % 10240))
"""
    script = sandbox_hook(
        hook_context,
        tmp_path,
        tar_body="exec python3 -I -c " + shlex.quote(producer),
    )
    # Any archive staging attempt fails; only the restart ledger may use /run.
    script = script.replace(
        "export PATH\n",
        "export PATH\nmktemp() { echo 'archive staging forbidden' >&2; return 1; }\n",
    )
    result = subprocess.run(
        ["bash", "-c", "ulimit -v 65536\n" + script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    archive = out / f"{NAME}.tar"
    assert archive.stat().st_size > 80 * 1024 * 1024
    with tarfile.open(archive) as contents:
        assert contents.getmember("snapshot-id/payload").size == 80 * 1024 * 1024
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    assert (out / f"{NAME}.tar.sha256").read_text() == f"{digest}  {NAME}.tar\n"
    assert (out / "latest").read_text() == NAME + "\n"
    assert {path.name for path in out.iterdir()} == {
        f"{NAME}.tar",
        f"{NAME}.tar.sha256",
        "latest",
    }
    assert {path.name for path in (tmp_path / "runtime").iterdir()} == {"restart"}


@pytest.mark.parametrize(
    "failure",
    ["empty", "partial", "complete", "empty_success", "short", "aligned_short"],
)
def test_backup_hook_does_not_publish_failed_or_short_tar(
    hook_context: Context, tmp_path: Path, failure: str
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    source = shlex.quote(str(tmp_path / "source.tar"))
    body = {
        "empty": "exit 2",
        "partial": f"head -c 600 -- {source}; exit 2",
        "complete": f"cat -- {source}; exit 2",
        "empty_success": "exit 0",
        "short": f"head -c 600 -- {source}",
        "aligned_short": f"head -c 1024 -- {source}",
    }[failure]
    result = subprocess.run(
        ["bash", "-c", sandbox_hook(hook_context, tmp_path, tar_body=body)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stderr
    assert not list(out.iterdir())


def test_backup_hook_accepts_owned_private_parent(
    hook_context: Context, tmp_path: Path
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    assert out.parent.stat().st_uid == out.stat().st_uid == os.getuid()
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    result = subprocess.run(
        ["bash", "-c", sandbox_hook(hook_context, tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "latest").read_text() == NAME + "\n"


@pytest.mark.parametrize(
    "target",
    [
        f"{NAME}.tar",
        f"{NAME}.tar.sha256",
        ".latest.tmp",
        f".{NAME}.tar.part",
        "latest",
    ],
)
def test_backup_hook_does_not_follow_output_symlinks(
    hook_context: Context, tmp_path: Path, target: str
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    victim.chmod(0o640)
    (out / target).symlink_to(victim)
    script = sandbox_hook(hook_context, tmp_path)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "untouched\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o640
    assert (out / "latest").read_text() == NAME + "\n"
    assert (out / f"{NAME}.tar").read_bytes() == archive_bytes()
    digest = hashlib.sha256(archive_bytes()).hexdigest()
    assert (out / f"{NAME}.tar.sha256").read_text() == f"{digest}  {NAME}.tar\n"
    for name in ("latest", f"{NAME}.tar", f"{NAME}.tar.sha256"):
        assert not (out / name).is_symlink()
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
        assert (out / name).stat().st_uid == os.getuid()
        assert (out / name).stat().st_gid == grp.getgrnam(
            pwd.getpwuid(os.getuid()).pw_name
        ).gr_gid
    assert not list((tmp_path / "runtime").glob("archive.*"))
    assert not list(out.glob(".garden-backup.*"))


@pytest.mark.parametrize("change", ["symlink", "replacement", "mode"])
def test_backup_hook_refuses_changed_export_directory(
    hook_context: Context, tmp_path: Path, change: str
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    if change == "mode":
        mutation = 'chmod 0777 "$out"'
    else:
        mutation = f'mv -- "$out" {shlex.quote(str(tmp_path / "original"))}; '
        mutation += (
            f'ln -s -- {shlex.quote(str(other))} "$out"'
            if change == "symlink"
            else 'mkdir -m 0700 -- "$out"'
        )
    script = sandbox_hook(hook_context, tmp_path, change=mutation)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert not list(out.iterdir()) and not list(other.iterdir())
    assert not list((tmp_path / "runtime").glob("archive.*"))


def test_backup_hook_refuses_symlinked_export_directory(
    hook_context: Context, tmp_path: Path
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    out.symlink_to(other, target_is_directory=True)
    result = subprocess.run(
        ["bash", "-c", sandbox_hook(hook_context, tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not list(other.iterdir())


@pytest.mark.parametrize("attack", ["parent_redirect", "temporary_collision"])
def test_publisher_resists_redirection(
    hook_context: Context, tmp_path: Path, attack: str
) -> None:
    out = Path(hook_context.state["server"]["backup_hook"]["export_dir"])
    out.mkdir(mode=0o700)
    info = out.stat()
    archive = tmp_path / "archive.tar"
    archive.write_bytes(archive_bytes())
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    victim.chmod(0o640)
    collision = out / (".garden-backup." + "a" * 32)
    collision.symlink_to(victim)
    script = sandbox_hook(hook_context, tmp_path)
    code = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    if attack == "parent_redirect":
        # Redirect the entire parent after open: all writes must stay on the fd.
        prefix = """
import os
import sys
from pathlib import Path
original_open = os.open
def redirected_open(path, flags, *args, **kwargs):
    fd = original_open(path, flags, *args, **kwargs)
    if path == sys.argv[1]:
        parent = Path(path).parent
        parent.rename(parent.with_name("original"))
        parent.mkdir(mode=0o700)
        Path(path).symlink_to(Path(sys.argv[6]).parent, target_is_directory=True)
    return fd
os.open = redirected_open
"""
        published = out.parent.with_name("original") / out.name
    else:
        # Force the first random name to collide with a planted symlink.
        prefix = """
import secrets
names = iter(["a" * 32, "b" * 32, "c" * 32, "d" * 32])
secrets.token_hex = lambda count: next(names)
"""
        published = out
    result = subprocess.run(
        [
            "python3",
            "-I",
            "-c",
            prefix + code,
            str(out),
            hook_context.state["server"]["backup_hook"]["export_owner"],
            NAME,
            f"{info.st_dev}:{info.st_ino}",
            "cat",
            str(archive),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "untouched\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o640
    assert (published / "latest").read_text() == NAME + "\n"
    assert (published / f"{NAME}.tar").read_bytes() == archive.read_bytes()
    assert not (tmp_path / "latest").exists()
