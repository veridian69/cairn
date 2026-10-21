"""Finite, instance-bound sudo commands and the corresponding operator policy."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .core import Context, InstallError, read_owned

UNITS = ("garden-daemon.service", "garden-mcp.service")
TOOLS = (
    "sudo",
    "groupadd",
    "useradd",
    "install",
    "sha256sum",
    "stat",
    "systemctl",
    "rm",
)
BINARY_LIMIT = 128 * 1024 * 1024
# /usr/local/sbin is a symlink to bin on Fedora 42+, which garden-server refuses.
BACKUP_SCRIPT = "/usr/local/bin/garden-backup"
DIGEST_SENTINEL = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  /dev/null"
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
    # Only actual regular deploy files from the binary's source are permitted.
    for path in sorted((ctx.source / "garden" / "deploy").iterdir()):
        if (
            path.is_symlink()
            or not path.is_file()
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name)
        ):
            raise InstallError("Garden deploy contains an unsafe entry")
        result[f"/usr/local/share/garden/deploy/{path.name}"] = ("root", "root", "0644")
    result["/usr/local/share/garden/install-manifest.json"] = ("root", "root", "0644")
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
    }
    if key not in expected or set(params) != expected[key]:
        raise InstallError("Sudo command is outside the server allowlist")
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
                not isinstance(path, str) or path not in files(ctx) for path in paths
            )
        ):
            raise InstallError("Sudo digest is outside the server allowlist")
        command = [tools["sha256sum"], "--", *paths, "/dev/null"]
    elif key == "stat":
        path = params["path"]
        if not isinstance(path, str) or path not in files(ctx):
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
    else:
        path = params["path"]
        if not isinstance(path, str) or path not in files(ctx):
            raise InstallError("Sudo removal is outside the server allowlist")
        command = [tools["rm"], "-f", "--", path]
    return [tools["sudo"], "-n", *command]


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


def run(ctx: Context, key: str, **params: Any) -> str:
    command = argv(ctx, key, **params)
    server = _server(ctx)
    if key in {"file", "rm"}:
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
    lines += [
        f"{_escape(user)} ALL=(root) NOPASSWD: "
        + " ".join(_escape(arg) for arg in command[2:])
        for command in policy_commands(ctx)
    ]
    return "\n".join(lines) + "\n"
