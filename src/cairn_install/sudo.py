"""Finite, instance-bound sudo commands and the corresponding operator policy."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .core import Context, InstallError, read_owned

UNITS = ("garden-daemon.service", "garden-mcp.service")
# S11 records hold these; garden-upgrade extends a record with tar, du and mv once.
LEGACY_TOOLS = (
    "sudo",
    "groupadd",
    "useradd",
    "install",
    "sha256sum",
    "stat",
    "systemctl",
    "rm",
)
TOOLS = (*LEGACY_TOOLS, "tar", "du", "mv")
BINARY_LIMIT = 128 * 1024 * 1024
COPY_LIMIT = 8 * 1024**3
# /usr/local/sbin is a symlink to bin on Fedora 42+, which garden-server refuses.
BACKUP_SCRIPT = "/usr/local/bin/garden-backup"
DIGEST_SENTINEL = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  /dev/null"
)
# The backup hook exits 1 while this exists (v0.8.0+). /etc/garden is root-owned.
FLAG = "/etc/garden/upgrade-in-progress"
# Data commands run as the Garden account, never as root: that account owns
# every ancestor below /var/lib/garden, so root must not traverse them.
DATA = "/var/lib/garden/.garden"
RESTORE = DATA + "/upgrade-restore"
DATA_PATHS = (DATA + "/data", DATA + "/data.pre-restore", RESTORE + "/data")
DEPLOY = "/usr/local/share/garden/deploy/"
MANIFEST = "/usr/local/share/garden/install-manifest.json"
RELEASE_FIXED = frozenset(
    {
        "/usr/local/bin/garden",
        "/usr/local/bin/garden-config",
        "/usr/local/bin/garden-session",
        MANIFEST,
        *(f"/etc/systemd/system/{unit}" for unit in UNITS),
    }
)


def release_path(path: str) -> bool:
    """Files that move together across a release change; configuration never does."""
    return path in RELEASE_FIXED or (
        path.startswith(DEPLOY)
        and re.fullmatch(r"[A-Za-z0-9_.-]+", path[len(DEPLOY) :]) is not None
    )


def _server(ctx: Context) -> dict[str, Any]:
    if ctx.mode != "garden-server":
        raise InstallError(
            "Server sudo commands require garden-server mode", "invalid_arguments"
        )
    server: dict[str, Any] = ctx.state["server"]
    return server


def stage_root(ctx: Context) -> Path:
    return ctx.root / "server-stage"


def files(ctx: Context) -> dict[str, tuple[str, str, str]]:
    """The file rows of the contract table, expanded for this binding."""
    server = _server(ctx)
    account = server["account"]
    result = {
        f"/usr/local/bin/{name}": ("root", "root", "0755")
        for name in ("garden", "garden-config", "garden-session")
    }
    staged = server.get("staged")
    names: list[str] = []
    if staged is None:
        # Only actual regular deploy files from the binary's source are permitted.
        for path in sorted((ctx.source / "garden" / "deploy").iterdir()):
            if (
                path.is_symlink()
                or not path.is_file()
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name)
            ):
                raise InstallError("Garden deploy contains an unsafe entry")
            names.append(DEPLOY + path.name)
    else:
        # Once staged, the recorded releases define the rows, not the source.
        recorded = [*staged]
        for release in server.get("releases", {}).values():
            recorded += release["files"]
        names = sorted({path for path in recorded if path.startswith(DEPLOY)})
        if not all(release_path(path) for path in names):
            raise InstallError("Recorded Garden deploy contains an unsafe entry")
    for name in names:
        result[name] = ("root", "root", "0644")
    result[MANIFEST] = ("root", "root", "0644")
    for unit in UNITS:
        result[f"/etc/systemd/system/{unit}"] = ("root", "root", "0644")
    result.update(
        {
            "/etc/garden/garden.json": ("root", account, "0640"),
            "/etc/garden/tls/garden-key.pem": (account, account, "0600"),
            "/etc/garden/tls/garden-chain.pem": ("root", account, "0640"),
            "/etc/garden/tls/internal-ca.pem": ("root", account, "0640"),
            "/var/lib/garden/.garden/config.yaml": (account, account, "0600"),
        }
    )
    if server["requires"]:
        result["/etc/systemd/system/garden-mcp.service.d/requires.conf"] = (
            "root",
            "root",
            "0644",
        )
    if hook := server["backup_hook"]:
        result.update(
            {
                BACKUP_SCRIPT: ("root", "root", "0755"),
                "/etc/systemd/system/garden-backup.service": ("root", "root", "0644"),
                f"/etc/systemd/system/{hook['unit']}.d/garden.conf": (
                    "root",
                    "root",
                    "0644",
                ),
            }
        )
    return result


def directories(ctx: Context) -> dict[str, tuple[str, str, str]]:
    server = _server(ctx)
    result = {
        path: ("root", "root", "0755")
        for path in (
            "/usr/local/share/garden",
            "/usr/local/share/garden/deploy",
        )
    }
    if server["requires"]:
        result["/etc/systemd/system/garden-mcp.service.d"] = ("root", "root", "0755")
    if hook := server["backup_hook"]:
        result[f"/etc/systemd/system/{hook['unit']}.d"] = ("root", "root", "0755")
        result[hook["export_dir"]] = (
            hook["export_owner"],
            hook["export_owner"],
            "0700",
        )
    return result


def argv(ctx: Context, key: str, **params: Any) -> list[str]:
    """Build only table entries; caller-supplied argv and extra parameters fail."""
    server = _server(ctx)
    if key == "backup" and not server["backup_hook"]:
        raise InstallError(
            "garden-server backup requires backup_hook", "invalid_arguments"
        )
    tools = server.get("tools", {})
    if set(tools) != set(TOOLS) or any(
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or "\n" in path
        or "\x00" in path
        for path in tools.values()
    ):
        raise InstallError("Invalid recorded sudo executable paths")
    account = server["account"]
    expected = {
        "probe": set(),
        "group": set(),
        "user": set(),
        "dir_data": set(),
        "dir_etc": set(),
        "dir_sys": {"path"},
        "file": {"path"},
        "digest": {"paths"},
        "stat": {"path"},
        "reload": set(),
        "unit": {"action", "unit"},
        "backup": set(),
        "rm": {"path"},
        "flag_set": set(),
        "flag_clear": set(),
        **{name: set() for name in _DATA_KEYS},
    }
    if key not in expected or set(params) != expected[key]:
        raise InstallError("Sudo command is outside the server allowlist")
    if key in _DATA_KEYS:
        return [tools["sudo"], "-n", "-u", account, *_data_command(tools, key)]
    if key == "probe":
        command = ["/usr/bin/true"]
    elif key == "group":
        command = [tools["groupadd"], "--system", account]
    elif key == "user":
        command = [
            tools["useradd"],
            "--system",
            "--gid",
            account,
            "--home-dir",
            "/var/lib/garden",
            "--shell",
            "/usr/sbin/nologin",
            account,
        ]
    elif key in {"dir_data", "dir_etc", "dir_sys", "file"}:
        if key == "dir_data":
            owner, group, mode = account, account, "0700"
            paths = ["/var/lib/garden", "/var/lib/garden/.garden"]
        elif key == "dir_etc":
            owner, group, mode = "root", account, "0750"
            paths = ["/etc/garden", "/etc/garden/tls"]
        else:
            path = params["path"]
            rows = directories(ctx) if key == "dir_sys" else files(ctx)
            if not isinstance(path, str) or path not in rows:
                raise InstallError("Sudo destination is outside the server allowlist")
            owner, group, mode = rows[path]
            paths = (
                [path]
                if key == "dir_sys"
                else [str(stage_root(ctx) / path.lstrip("/")), path]
            )
        command = [
            tools["install"],
            *(["-d"] if key != "file" else []),
            "-o",
            owner,
            "-g",
            group,
            "-m",
            mode,
            *paths,
        ]
    elif key == "digest":
        paths = params["paths"]
        if (
            not isinstance(paths, (list, tuple))
            or not paths
            or len(set(paths)) != len(paths)
            or any(
                not isinstance(path, str) or (path not in files(ctx) and path != FLAG)
                for path in paths
            )
        ):
            raise InstallError("Sudo digest is outside the server allowlist")
        command = [tools["sha256sum"], "--", *paths, "/dev/null"]
    elif key == "stat":
        path = params["path"]
        if not isinstance(path, str) or (path not in files(ctx) and path != FLAG):
            raise InstallError("Sudo stat is outside the server allowlist")
        command = [tools["stat"], "--format=%F", "--", path, "/dev/null"]
    elif key == "unit":
        if (
            params["action"] not in {"start", "stop", "enable", "disable"}
            or params["unit"] not in UNITS
        ):
            raise InstallError("Sudo unit action is outside the server allowlist")
        command = [tools["systemctl"], params["action"], params["unit"]]
    elif key == "reload":
        command = [tools["systemctl"], "daemon-reload"]
    elif key == "backup":
        if not server["backup_hook"]:
            raise InstallError(
                "garden-server backup requires backup_hook", "invalid_arguments"
            )
        command = [tools["systemctl"], "start", "garden-backup.service"]
    elif key == "flag_set":
        command = [tools["install"], "-o", "root", "-g", "root", "-m", "0600"]
        command += ["--", "/dev/null", FLAG]
    elif key == "flag_clear":
        command = [tools["rm"], "-f", "--", FLAG]
    else:
        path = params["path"]
        if not isinstance(path, str) or path not in files(ctx):
            raise InstallError("Sudo removal is outside the server allowlist")
        command = [tools["rm"], "-f", "--", path]
    return [tools["sudo"], "-n", *command]


_DATA_KEYS = (
    "data_size",
    "data_take",
    "data_probe",
    "data_stage",
    "data_extract",
    "data_compare",
    "data_aside",
    "data_place",
)


def _data_command(tools: dict[str, str], key: str) -> list[str]:
    """Fixed data argv run as the Garden account; archives stream through stdio."""
    data, aside, restored = DATA_PATHS
    return {
        "data_size": [tools["du"], "--summarize", "--apparent-size"]
        + ["--block-size=1", "--", data],
        "data_take": [tools["tar"], "--create", "--file=-", f"--directory={DATA}"]
        + ["--numeric-owner", "--one-file-system", "--", "data"],
        "data_probe": [tools["stat"], "--format=%n", "--", *DATA_PATHS, "/dev/null"],
        "data_stage": [tools["install"], "-d", "-m", "0700", "--", RESTORE],
        "data_extract": [tools["tar"], "--extract", "--file=-"]
        + [f"--directory={RESTORE}", "--same-permissions", "--overwrite"]
        + ["--", "data"],
        "data_compare": [tools["tar"], "--compare", "--file=-"]
        + [f"--directory={RESTORE}", "--numeric-owner", "--", "data"],
        "data_aside": [tools["mv"], "--no-target-directory", "--", data, aside],
        "data_place": [tools["mv"], "--no-target-directory", "--", restored, data],
    }[key]


def data_present(ctx: Context) -> set[str]:
    """Which of the fixed data paths exist, as the Garden account sees them."""
    lines = run(ctx, "data_probe").removesuffix("\n").split("\n")
    if lines[-1] != "/dev/null" or not set(lines[:-1]) <= set(DATA_PATHS):
        raise InstallError("Invalid Garden data probe response; refusing mutation")
    return set(lines[:-1])


def check_policy(ctx: Context) -> None:
    """Ask sudo, before any downtime, whether every listed command is granted."""
    sudo_path = _server(ctx)["tools"]["sudo"]
    denied = 0
    for command in policy_commands(ctx):
        try:
            ctx.command(
                [sudo_path, "-n", "-l", *command[2:]],
                cwd=ctx.directory,
                env={"LC_ALL": "C"},
            )
        except InstallError:
            denied += 1
    if denied:
        raise InstallError(
            f"The sudo policy does not grant {denied} Garden server commands; install "
            f"the updated {ctx.directory / 'garden-server.sudoers'}, then run "
            "garden-upgrade again"
        )


def digest(ctx: Context, path: str) -> str | None:
    raw = run(ctx, "digest", paths=[path])
    lines = raw.removesuffix("\n").split("\n")
    # Exit 1 also covers sudo denial: the sentinel proves the command ran.
    if lines[-1] != DIGEST_SENTINEL:
        raise InstallError(
            "Invalid sudo digest response: missing /dev/null sentinel; refusing mutation"
        )
    digest = None
    if len(lines) == 2:
        match = re.fullmatch(r"([0-9a-fA-F]{64})  " + re.escape(path), lines[0])
        if match is None:
            raise InstallError("Invalid sudo digest response; refusing mutation")
        digest = match[1].lower()
    elif len(lines) != 1:
        raise InstallError("Invalid sudo digest response; refusing mutation")
    # sha256sum follows symlinks; stat checks the destination itself.
    raw = run(ctx, "stat", path=path)
    lines = raw.removesuffix("\n").split("\n")
    if lines[-1] != "character special file":
        raise InstallError(
            "Invalid sudo stat response: missing /dev/null sentinel; refusing mutation"
        )
    if len(lines) == 1:
        if digest is not None:
            raise InstallError(
                "Invalid sudo stat response: inconsistent with digest; refusing mutation"
            )
        return None
    if len(lines) == 2 and lines[0]:
        if digest is not None:
            if lines[0] in {"regular file", "regular empty file"}:
                return digest
            raise InstallError(
                f"Sudo digest target exists but is not a regular file: {path}"
            )
        raise InstallError(
            f"Sudo digest target exists but is not a hashable regular file: {path}"
        )
    raise InstallError("Invalid sudo stat response; refusing mutation")


def run(
    ctx: Context,
    key: str,
    *,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
    **params: Any,
) -> str:
    command = argv(ctx, key, **params)
    server = _server(ctx)
    pending = (
        server.get("replacements", {}).get(params["path"])
        if key in {"file", "rm"}
        else None
    )
    if pending is not None:
        # GNU install unlinks before it copies, so an interrupted release change
        # may leave this path absent or partial; the verified write repairs it.
        staged = stage_root(ctx) / params["path"].lstrip("/")
        if key == "file" and (
            pending["new"] is None
            or hashlib.sha256(read_owned(staged, BINARY_LIMIT)).hexdigest()
            != pending["new"]
        ):
            raise InstallError(f"Staged file digests changed: {params['path']}")
    elif key in {"file", "rm"}:
        path = params["path"]
        expected = server.get("installed", {}).get(path) or server.get(
            "install_intents", {}
        ).get(path)
        if expected is None:
            raise InstallError("Sudo file mutation has no ownership intent")
        actual = digest(ctx, path)
        if key == "rm":
            if actual is None:
                return ""
            if actual != expected:
                raise InstallError(f"Installed file digests changed: {path}")
        else:
            staged = stage_root(ctx) / path.lstrip("/")
            if hashlib.sha256(read_owned(staged, BINARY_LIMIT)).hexdigest() != expected:
                raise InstallError(f"Staged file digests changed: {path}")
            if actual is not None and actual != expected:
                raise InstallError(
                    f"Refusing to overwrite unowned or changed file: {path}"
                )
    if key in _DATA_KEYS:
        # The Garden account need not be able to enter the installer's directory.
        return ctx.command(
            command,
            cwd=Path("/"),
            env={"LC_ALL": "C"},
            timeout=1800,
            private=stdin_path is not None or stdout_path is not None,
            stdin_path=stdin_path,
            stdout_path=stdout_path,
            stdout_limit=COPY_LIMIT,
            allowed=(0, 1) if key == "data_probe" else (0,),
        )
    return ctx.command(
        command,
        cwd=ctx.directory,
        env={"LC_ALL": "C"},
        allowed=(0, 1) if key in {"digest", "stat"} else (0,),
    )


def policy_commands(ctx: Context) -> list[list[str]]:
    """Concrete expansion used by the flow; digest calls use one path at a time."""
    commands = [
        argv(ctx, key)
        for key in ("probe", "group", "user", "dir_data", "dir_etc", "reload")
    ]
    commands += [argv(ctx, "dir_sys", path=path) for path in directories(ctx)]
    for path in files(ctx):
        commands += [
            argv(ctx, "file", path=path),
            argv(ctx, "digest", paths=[path]),
            argv(ctx, "stat", path=path),
            argv(ctx, "rm", path=path),
        ]
    commands += [
        argv(ctx, "unit", action=action, unit=unit)
        for action in ("start", "stop", "enable", "disable")
        for unit in UNITS
    ]
    if ctx.state["server"]["backup_hook"]:
        commands.append(argv(ctx, "backup"))
    # garden-upgrade: the window flag and the data commands (run as Garden).
    commands += [argv(ctx, "flag_set"), argv(ctx, "flag_clear")]
    commands += [argv(ctx, "digest", paths=[FLAG]), argv(ctx, "stat", path=FLAG)]
    commands += [argv(ctx, key) for key in _DATA_KEYS]
    return commands


def _escape(value: str) -> str:
    # sudoers treats shell quotes literally. Escape its own argument grammar,
    # including wildcard characters, so a grant never expands to other paths.
    return re.sub(r"([\\\s,:=!*?\[\]#])", r"\\\1", value)


def snippet(ctx: Context) -> str:
    import pwd

    user = pwd.getpwuid(os.getuid()).pw_name
    lines = [
        "# WARNING: granting this policy is root-equivalent; it is not a containment boundary.",
        "# It enumerates and audits the exact commands the installer runs and proves the allowlist complete.",
        "# The installer user controls staged files installed as root, including systemd units.",
        "# Review and install separately; this installer never installs sudoers.",
    ]
    for command in policy_commands(ctx):
        runas, args = (
            (command[3], command[4:]) if command[2] == "-u" else ("root", command[2:])
        )
        lines.append(
            f"{_escape(user)} ALL=({_escape(runas)}) NOPASSWD: "
            + " ".join(_escape(arg) for arg in args)
        )
    return "\n".join(lines) + "\n"
