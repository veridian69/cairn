"""Garden on system units, bound to an existing loopback Cairn authority."""

from __future__ import annotations

import grp
import hashlib
import ipaddress
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import garden, sudo
from .core import (
    MAX_OUTPUT,
    TOKEN,
    Context,
    InstallError,
    atomic_write,
    read_owned,
    secure_directory,
)
from .verification import NoRedirect, validate_instance

STAGES = (
    "preflight",
    "garden_files",
    "verify",
    "garden_prepare",
    "server_account",
    "server_install",
    "server_hook",
    "garden_start",
    "garden_verify",
    "garden_restart",
    "garden_enable",
)
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,190}\.service")
DIGEST = re.compile(r"[0-9a-f]{64}")
# A host's manual Garden runbook may have installed its own copy here.
RUNBOOK_BACKUP_SCRIPT = "/usr/local/sbin/garden-backup"


def _require(ctx: Context) -> dict[str, Any]:
    if ctx.mode != "garden-server":
        raise InstallError(
            "This operation requires garden-server mode", "invalid_arguments"
        )
    server: dict[str, Any] = ctx.state["server"]
    return server


def _absolute(value: Any) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("absolute native Linux path required")
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path == Path("/mnt")
        or Path("/mnt") in path.parents
        or str(path) != value
    ):
        raise ValueError("absolute canonical native Linux path required")
    return value


def _binding_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("numeric loopback Cairn URL required")
    url = urlsplit(value)
    address = ipaddress.ip_address(url.hostname or "")
    if (
        url.scheme != "http"
        or not address.is_loopback
        or "%" in value
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or url.port is None
        or not 1 <= url.port <= 65535
        or any(c.isspace() for c in value)
    ):
        raise ValueError("HTTP numeric loopback URL with explicit port required")
    host = f"[{address}]" if address.version == 6 else str(address)
    if value.rstrip("/") != f"http://{host}:{url.port}":
        raise ValueError("canonical numeric loopback URL required")
    return value.rstrip("/")


def load_options(path: Path | str) -> dict[str, Any]:
    """Read only the binding configuration; never read or copy its admin token."""
    value = garden._document(read_owned(Path(path).absolute(), 128 * 1024))
    try:
        if set(value) - {"cairn", "account", "requires", "backup_hook"} or not {
            "cairn",
            "account",
        } <= set(value):
            raise ValueError("unknown or missing server option")
        cairn = value["cairn"]
        if not isinstance(cairn, dict) or set(cairn) != {
            "url",
            "instance_id",
            "realm",
            "admin_token_file",
        }:
            raise ValueError("exact Cairn binding required")
        cairn["url"] = _binding_url(cairn["url"])
        garden._uuid(cairn["instance_id"])
        if not isinstance(cairn["realm"], str) or not garden.KIND.fullmatch(
            cairn["realm"]
        ):
            raise ValueError("invalid binding realm")
        _absolute(cairn["admin_token_file"])
        if (
            not isinstance(value["account"], str)
            or not ACCOUNT.fullmatch(value["account"])
            or value["account"] == "root"
        ):
            raise ValueError("non-root service account required")
        requires = value.setdefault("requires", [])
        if (
            not isinstance(requires, list)
            or any(
                not isinstance(unit, str)
                or not UNIT.fullmatch(unit)
                or unit.startswith("garden-")
                for unit in requires
            )
            or len(set(requires)) != len(requires)
        ):
            raise ValueError("requires must contain distinct external service units")
        hook = value.setdefault("backup_hook", None)
        if hook is not None:
            if not isinstance(hook, dict) or set(hook) != {
                "unit",
                "on_failure",
                "export_dir",
                "export_owner",
                "label",
            }:
                raise ValueError("exact backup_hook fields required")
            if (
                not isinstance(hook["unit"], str)
                or not UNIT.fullmatch(hook["unit"])
                or hook["unit"].startswith("garden-")
            ):
                raise ValueError("external backup service unit required")
            alert = hook["on_failure"]
            if (
                not isinstance(alert, str)
                or not UNIT.fullmatch(alert.replace("%n", "unit"))
                or "%" in alert.replace("%n", "")
            ):
                raise ValueError("invalid OnFailure service unit")
            _absolute(hook["export_dir"])
            export = Path(hook["export_dir"])
            if len(export.parts) < 3 or any(
                export == p or export.is_relative_to(p) or p.is_relative_to(export)
                for p in (
                    Path("/etc"),
                    Path("/usr"),
                    Path("/var/lib/garden"),
                    Path("/proc"),
                    Path("/sys"),
                    Path("/dev"),
                    Path("/run"),
                )
            ):
                raise ValueError("export_dir must be a separate data directory")
            if (
                not isinstance(hook["export_owner"], str)
                or not ACCOUNT.fullmatch(hook["export_owner"])
                or hook["export_owner"] == "root"
            ):
                raise ValueError("invalid export owner")
            if not isinstance(hook["label"], str) or not re.fullmatch(
                r"[a-z0-9]{1,16}", hook["label"]
            ):
                raise ValueError("invalid backup label")
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise InstallError(
            "Invalid server options: " + str(error), "invalid_arguments"
        ) from None


def request(
    ctx: Context,
    endpoint: str,
    token: str = "",
    *,
    data: bytes | None = None,
    key: str = "",
) -> dict[str, Any]:
    """Use the exact binding address, without redirects or proxy discovery."""
    server = _require(ctx)
    url = _binding_url(server["cairn"]["url"])
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise InstallError("Invalid Cairn API route")
    headers = {"Accept": "application/json"}
    if token:
        ctx.add_secret(token)
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            Request(url + endpoint, data=data, headers=headers), timeout=10
        ) as response:
            raw = response.read(MAX_OUTPUT + 1)
            if response.status != 200 or len(raw) > MAX_OUTPUT:
                raise InstallError("Unexpected external Cairn response")
    except HTTPError as error:
        with error:
            raise InstallError(f"External Cairn returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise InstallError(
            "External Cairn connection failed; inspect the binding and resume"
        ) from None
    return garden._document(raw)


def external_ready(ctx: Context) -> str:
    server = _require(ctx)
    token = ctx.read_secret(Path(server["cairn"]["admin_token_file"]))
    if not TOKEN.fullmatch(token):
        raise InstallError("Administrator credential has invalid format")
    if request(ctx, "/health/ready").get("status") not in {"ok", "ready"}:
        raise InstallError("External Cairn is not ready")
    identity = request(ctx, "/v1/instance", token)
    validate_instance(identity, ctx.instance_id)
    pins = {}
    for field in ("contract_digest", "mcp_contract_digest"):
        if not isinstance(identity.get(field), str) or not DIGEST.fullmatch(
            identity[field]
        ):
            raise InstallError("External Cairn contract digests are invalid")
        pins[field] = identity[field]
    if (
        not isinstance(identity.get("product_version"), str)
        or not identity["product_version"]
    ):
        raise InstallError("External Cairn product version is missing")
    if server.setdefault("contracts", pins) != pins:
        raise InstallError(
            "External Cairn contract digests changed; explicit recovery is required"
        )
    ctx.state["receipts"]["instance"] = identity
    ctx.save()
    return token


def _check_path(path: Path) -> bool:
    """Reject visible symlink traversal and non-files before invoking root tools."""
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return False
        except PermissionError:
            # Root-owned private Garden directories are intentionally unreadable.
            return True
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"Unsafe server destination parent: {parent}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    if not stat.S_ISREG(info.st_mode):
        raise InstallError(f"Server destination is not a regular file: {path}")
    return True


def _check_directory(path: Path) -> bool:
    """Inspect every existing component before privileged directory creation."""
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            return False
        except PermissionError:
            # Only these fixed leaves live below our root-private directories.
            # Operator-selected destinations must remain fully inspectable.
            if component == path and path in {
                Path("/var/lib/garden/.garden"),
                Path("/etc/garden/tls"),
            }:
                return True
            raise InstallError(
                f"Cannot inspect server directory: {component}"
            ) from None
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError(
                f"Unsafe server directory (symlinks refused): {component}"
            )
    return True


def _installed(ctx: Context, *, allow_missing: bool = False) -> None:
    server = _require(ctx)
    for path, expected in {
        **server.get("install_intents", {}),
        **server["installed"],
    }.items():
        _check_path(Path(path))
        actual = sudo.digest(ctx, path)
        if actual != expected and not (
            actual is None and (allow_missing or path not in server["installed"])
        ):
            raise InstallError(
                f"Installed file digests changed: {path}; resume refused"
            )


def _account(ctx: Context, kind: str) -> Any:
    name = ctx.state["server"]["account"]
    try:
        return grp.getgrnam(name) if kind == "group" else pwd.getpwnam(name)
    except KeyError:
        return None


def _validate_account(ctx: Context, kind: str, record: Any) -> None:
    if kind == "group":
        if record.gr_gid == 0:
            raise InstallError("Garden service group must not be root")
    else:
        group = _account(ctx, "group")
        if (
            record.pw_uid == 0
            or group is None
            or record.pw_gid != group.gr_gid
            or record.pw_dir != "/var/lib/garden"
            or record.pw_shell != "/usr/sbin/nologin"
        ):
            raise InstallError("Recorded Garden service account changed")


def _system_tool(name: str) -> str:
    """Resolve policy executables independently of the installer's PATH."""
    found = shutil.which(name, path="/usr/bin:/usr/sbin:/bin:/sbin")
    if found is None or not Path(found).is_absolute():
        raise InstallError(f"An absolute {name} executable is required")
    try:
        path = Path(found).resolve(strict=True)
        for component in [path, *path.parents]:
            info = component.stat()
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or not (
                    stat.S_ISREG(info.st_mode)
                    if component == path
                    else stat.S_ISDIR(info.st_mode)
                )
            ):
                raise InstallError(f"Unsafe system executable path: {component}")
    except (OSError, RuntimeError) as error:
        raise InstallError(f"Cannot inspect system executable: {found}") from error
    return str(path)


def _refuse_runbook_backup() -> None:
    """Refuse a manual copy at the retired path; it would sit beside ours."""
    try:
        Path(RUNBOOK_BACKUP_SCRIPT).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        raise InstallError(
            f"Cannot inspect {RUNBOOK_BACKUP_SCRIPT}; retire the manual Garden backup first"
        ) from None
    # Where /usr/local/sbin links to bin, this name resolves to our own destination.
    if os.path.realpath(RUNBOOK_BACKUP_SCRIPT) == sudo.BACKUP_SCRIPT:
        return
    raise InstallError(
        f"Retire the manual Garden backup first: {RUNBOOK_BACKUP_SCRIPT}, with its "
        "garden-backup.service and cairn-v1-backup drop-in"
    )


def preflight(ctx: Context) -> None:
    server = _require(ctx)
    if os.geteuid() == 0 or platform.system() != "Linux":
        raise InstallError("garden-server requires a non-root Linux installer user")
    tools = {name: _system_tool(name) for name in sudo.TOOLS}
    if server.setdefault("tools", tools) != tools:
        raise InstallError("Recorded sudo executable paths changed")
    ctx.save()
    sudo.run(ctx, "probe")
    garden.require_unexpired(ctx.state["garden"]["options"])
    if ctx.state["garden"]["options"]["scope"]["realm"] != garden.garden_realm(ctx):
        raise InstallError("Garden scope realm differs from the Cairn binding")
    for kind in ("group", "user"):
        record = _account(ctx, kind)
        journal = server.get("accounts", {}).get(kind)
        if record is not None:
            if journal is None:
                raise InstallError(f"Refusing to adopt existing Garden {kind}")
            _validate_account(ctx, kind, record)
        elif journal == "complete":
            raise InstallError(f"Recorded Garden {kind} disappeared")
    # A private topology is never adopted, including retained data from a
    # different run. Check roots before creating directories or credentials.
    dirs = {"/var/lib/garden", "/etc/garden", *sudo.directories(ctx)}
    for path in sorted(dirs):
        if _check_directory(Path(path)):
            if path not in server.get("directories", {}):
                raise InstallError(f"Refusing to adopt existing directory: {path}")
    if server["backup_hook"]:
        _refuse_runbook_backup()
    recorded = {**server.get("install_intents", {}), **server["installed"]}
    for path in sudo.files(ctx):
        if _check_path(Path(path)):
            actual = sudo.digest(ctx, path)
            if actual is not None and recorded.get(path) != actual:
                raise InstallError(f"Refusing to adopt existing server file: {path}")
    _installed(ctx)
    ctx.write_file(ctx.directory / "garden-server.sudoers", sudo.snippet(ctx))


def _stage_hashes(ctx: Context) -> dict[str, str]:
    return {
        path: hashlib.sha256(
            read_owned(sudo.stage_root(ctx) / path.lstrip("/"), sudo.BINARY_LIMIT)
        ).hexdigest()
        for path in sudo.files(ctx)
    }


def prepare(ctx: Context) -> None:
    server = _require(ctx)
    if "staged" in server:
        if _stage_hashes(ctx) != server["staged"]:
            raise InstallError("Staged server files changed")
        return
    root = sudo.stage_root(ctx)
    if (root.exists() or root.is_symlink()) and not server.get("staging"):
        raise InstallError("Refusing to adopt an existing server stage")
    server["staging"] = True
    ctx.save()
    secure_directory(root, private=True)
    # Payloads, ownership manifests and unit transformations are separate
    # publications. Rebuild an incomplete, installer-owned stage on retry.
    shutil.rmtree(root)
    secure_directory(root, private=True)
    build = ctx.root / "server-build"
    secure_directory(build, private=True)
    binary = build / "garden"
    ctx.command(
        [
            "go",
            "build",
            "-trimpath",
            "-mod=readonly",
            "-ldflags",
            f"-s -w -X github.com/veridian69/cairn/garden/cmd.version={garden.source_version(ctx.source)}",
            "-o",
            str(binary),
            ".",
        ],
        cwd=ctx.source / "garden",
        env={
            "CGO_ENABLED": "0",
            "GOCACHE": str(build / "cache"),
            "GOMODCACHE": str(build / "mod"),
            "GOFLAGS": "-modcacherw",
            "XDG_CONFIG_HOME": str(build / "config"),
        },
        timeout=900,
    )
    ctx.command(
        [
            str(ctx.source / "garden/scripts/install-server"),
            "--root",
            str(root),
            "--binary",
            str(binary),
        ],
        cwd=ctx.source,
        timeout=900,
    )
    for unit in sudo.UNITS:
        source = read_owned(ctx.source / "garden/deploy" / unit).decode()
        if unit == "garden-daemon.service" and (
            "Type=notify\n" not in source or "NotifyAccess=main\n" not in source
        ):
            raise InstallError("Garden daemon unit must support sd_notify")
        text = source.replace("User=garden\n", f"User={server['account']}\n").replace(
            "Group=garden\n", f"Group={server['account']}\n"
        )
        atomic_write(root / "etc/systemd/system" / unit, text.encode(), 0o644)
    config = garden.gateway_config(
        ctx,
        data_dir="/var/lib/garden/.garden/data",
        cert_file="/etc/garden/tls/garden-chain.pem",
        key_file="/etc/garden/tls/garden-key.pem",
        daemon_url_file="/var/lib/garden/.garden/daemon.url",
        listen=garden.listen(
            ctx.state["garden"]["options"], ctx.state["garden"]["options"]["port"]
        ),
        cairn_url=server["cairn"]["url"],
    )
    atomic_write(
        root / "etc/garden/garden.json",
        (json.dumps(config, indent=2, sort_keys=True) + "\n").encode(),
    )
    for field, name in (
        ("key_file", "garden-key.pem"),
        ("cert_file", "garden-chain.pem"),
        ("ca_file", "internal-ca.pem"),
    ):
        source_path = ctx.state["garden"]["tls"].get(field)
        if source_path is None:
            # Publicly trusted chains need no additional CA at the gateway.
            source_path = ctx.state["garden"]["tls"]["cert_file"]
        ctx.check_file(Path(source_path))
        atomic_write(root / "etc/garden/tls" / name, read_owned(Path(source_path)))
    if server["requires"]:
        units = " ".join(server["requires"])
        atomic_write(
            root / "etc/systemd/system/garden-mcp.service.d/requires.conf",
            f"[Unit]\nRequires={units}\nAfter={units}\n".encode(),
            0o644,
        )
    if server["backup_hook"]:
        _prepare_hook(ctx)
    # install-server owns these manifest entries. Re-pin transformed units so
    # a manual install-server upgrade sees the actual installed unit bytes.
    manifest_path = root / "usr/local/share/garden/install-manifest.json"
    manifest = garden._document(read_owned(manifest_path))
    for unit in sudo.UNITS:
        relative = "etc/systemd/system/" + unit
        manifest["files"][relative] = hashlib.sha256(
            read_owned(root / relative)
        ).hexdigest()
    atomic_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        0o644,
    )
    # Pin every byte before granting any root-side installation intent.
    server["staged"] = _stage_hashes(ctx)
    ctx.save()


def _prepare_hook(ctx: Context) -> None:
    hook = ctx.state["server"]["backup_hook"]
    account = ctx.state["server"]["account"]
    root = sudo.stage_root(ctx)
    script = """#!/bin/bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
# An upgrade, by hand or by a later garden-upgrade, holds this root-owned flag.
# Fail loudly (OnFailure alerts) rather than stop or snapshot Garden under it.
if [ -e /etc/garden/upgrade-in-progress ]; then
  echo 'garden-backup: Garden upgrade in progress; run the backup after it completes' >&2
  exit 1
fi
out=EXPORT_DIR
test ! -L "$out"
recorded_export=$(stat -Lc '%d:%i' -- "$out")
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="garden-backup-$stamp"
list=/run/garden-backup/restart
cancelled=
start_recorded() {
  local unit failed=0
  while read -r unit; do
    if [ -n "$cancelled" ]; then
      systemctl --no-block start "$unit" || failed=1
    else
      systemctl start "$unit" || failed=1
    fi
  done < "$list"
  [ "$failed" -eq 0 ] && [ -z "$cancelled" ] && : > "$list"
  return "$failed"
}
restart() {
  local status=$?
  set +e
  if ! start_recorded; then
    echo 'garden-backup: restarting Garden failed' >&2
    [ "$status" -eq 0 ] && status=1
  fi
  exit "$status"
}
: > "$list"
trap restart EXIT
trap 'cancelled=1; exit 143' TERM INT
for unit in garden-daemon.service garden-mcp.service; do
  if systemctl is-active --quiet "$unit"; then
    printf '%s\\n' "$unit" >> "$list"
  fi
done
systemctl stop garden-mcp.service
systemctl stop garden-daemon.service
created=$(runuser -u ACCOUNT -- /usr/local/bin/garden snapshot create "LABEL-${stamp,,}")
printf '%s\\n' "$created"
id=$(printf '%s\\n' "$created" | sed -n 's/^snapshot created: \\([^ ]*\\) (.*/\\1/p')
test -n "$id"
# The id names the directory tar reads and rm removes, and tar would read a
# leading dash as an option: accept one plain name and nothing else.
case "$id" in
  ''|.*|-*|*[!A-Za-z0-9._-]*) echo 'garden-backup: unexpected snapshot id' >&2; exit 1;;
esac
start_recorded
# The requires drop-in is optional; every other listed member is mandatory.
extra=()
if [ -d /etc/systemd/system/garden-mcp.service.d ]; then
  extra=(garden-mcp.service.d)
fi
python3 -I - "$out" OWNER "$name" "$recorded_export" tar -cf - \\
  -C /var/lib/garden/.garden/backups "$id" \\
  -C /etc/garden garden.json tls \\
  -C /etc/systemd/system garden-daemon.service garden-mcp.service "${extra[@]}" <<'PY'
import grp
import hashlib
import os
import pwd
import secrets
import stat
import subprocess
import sys

owner = pwd.getpwnam(sys.argv[2])
group = grp.getgrnam(sys.argv[2])
name = sys.argv[3]
directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
temporaries = []
try:
    info = os.fstat(directory)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or f"{info.st_dev}:{info.st_ino}" != sys.argv[4]):
        raise SystemExit("garden-backup: export directory changed or is unsafe")

    def stage(final, chunks):
        while True:
            temporary = ".garden-backup." + secrets.token_hex(16)
            try:
                output = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                 | os.O_WRONLY, 0o600, dir_fd=directory)
                break
            except FileExistsError:
                continue
        temporaries.append((temporary, final))
        with os.fdopen(output, "wb") as stream:
            for chunk in chunks:
                stream.write(chunk)
            stream.flush()
            os.fchown(stream.fileno(), owner.pw_uid, group.gr_gid)
            os.fsync(stream.fileno())

    digest = hashlib.sha256()
    # Own the pipe so tar's exit status is known before publishing anything.
    producer = subprocess.Popen(sys.argv[5:], stdout=subprocess.PIPE)
    try:
        with producer.stdout as archive:
            def chunks():
                size = 0
                tail = b""
                while chunk := archive.read(1024 * 1024):
                    size += len(chunk)
                    tail = (tail + chunk)[-1024:]
                    digest.update(chunk)
                    yield chunk
                if producer.wait() != 0:
                    raise SystemExit("garden-backup: tar failed")
                if size < 1024 or size % 512 or tail != bytes(1024):
                    raise SystemExit("garden-backup: short archive stream")
            stage(name + ".tar", chunks())
    finally:
        if producer.poll() is None:
            producer.kill()
        producer.wait()
    stage(name + ".tar.sha256", [f"{digest.hexdigest()}  {name}.tar\\n".encode()])
    stage("latest", [(name + "\\n").encode()])
    for temporary, final in temporaries:
        os.rename(temporary, final, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)
finally:
    for temporary, _ in temporaries:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
    os.close(directory)
PY
# The published archive is the retained copy, as after the installer's own
# garden-backup; without this every run keeps a full snapshot on the host.
# Rename first: Garden's listing ignores dot-prefixed entries, so an
# interrupted removal never looks like a snapshot, and the next run ends it.
# Garden's shared runtime lease excludes snapshot maintenance meanwhile, as
# the running daemon would; Garden may be stopped if it was stopped before.
backups=/var/lib/garden/.garden/backups
lease=/var/lib/garden/.garden/runtime.lock
runuser -u ACCOUNT -- flock -s -w 300 "$lease" sh -c '
  mv -T -- "$1/$2" "$1/.deleting-$2" &&
  find "$1" -mindepth 1 -maxdepth 1 -name ".deleting-*" -exec rm -rf -- {} +
' sh "$backups" "$id"
"""
    for key, value in (
        ("EXPORT_DIR", shlex.quote(hook["export_dir"])),
        ("ACCOUNT", account),
        ("LABEL", hook["label"]),
        ("OWNER", hook["export_owner"]),
    ):
        script = script.replace(key, value)
    atomic_write(root / sudo.BACKUP_SCRIPT.lstrip("/"), script.encode(), 0o755)
    service = (
        "[Unit]\nDescription=Garden stopped snapshot, paired with Cairn backup\n"
        "After=garden-daemon.service garden-mcp.service\n"
        f"OnFailure={hook['on_failure']}\n\n"
        f"[Service]\nType=oneshot\nUser=root\nExecStart={sudo.BACKUP_SCRIPT}\n"
        "RuntimeDirectory=garden-backup\nRuntimeDirectoryMode=0700\n"
        "ExecStopPost=/bin/sh -c 'test -s /run/garden-backup/restart || exit 0; exec xargs /bin/systemctl --no-block start < /run/garden-backup/restart'\n"
        "TimeoutStartSec=10min\n"
    )
    atomic_write(
        root / "etc/systemd/system/garden-backup.service", service.encode(), 0o644
    )
    atomic_write(
        root / f"etc/systemd/system/{hook['unit']}.d/garden.conf",
        b"[Unit]\nWants=garden-backup.service\nAfter=garden-backup.service\n",
        0o644,
    )


def server_account(ctx: Context) -> None:
    server = _require(ctx)
    accounts = server.setdefault("accounts", {})
    for kind in ("group", "user"):
        record = _account(ctx, kind)
        if record is not None:
            if kind not in accounts:
                raise InstallError(f"Refusing to adopt existing Garden {kind}")
            _validate_account(ctx, kind, record)
        else:
            if accounts.get(kind) == "complete":
                raise InstallError(f"Recorded Garden {kind} disappeared")
            accounts[kind] = "pending"
            ctx.save()
            sudo.run(ctx, kind)
            record = _account(ctx, kind)
            if record is None:
                raise InstallError(f"Garden {kind} creation was not observed")
            _validate_account(ctx, kind, record)
        accounts[kind] = "complete"
        ctx.save()


def _directory(ctx: Context, key: str, paths: list[str], **params: Any) -> None:
    server = _require(ctx)
    journal = server.setdefault("directories", {})
    existing = {path: _check_directory(Path(path)) for path in paths}
    if all(journal.get(path) == "complete" for path in paths):
        return
    for path in paths:
        if existing[path] and path not in journal:
            raise InstallError(f"Refusing to adopt existing directory: {path}")
        journal[path] = "pending"
    ctx.save()
    sudo.run(ctx, key, **params)
    for path in paths:
        journal[path] = "complete"
    ctx.save()


def _publish(ctx: Context, paths: list[str]) -> None:
    server = _require(ctx)
    for path in paths:
        _check_path(Path(path))
        actual = sudo.digest(ctx, path)
        expected = server["staged"][path]
        recorded = server["installed"].get(path) or server.get(
            "install_intents", {}
        ).get(path)
        if actual is not None and (recorded != actual or actual != expected):
            raise InstallError(
                f"Refusing to adopt or overwrite changed server file: {path}"
            )
        if path in server["installed"]:
            if actual != expected:
                raise InstallError(f"Installed file digests changed: {path}")
            continue
        server.setdefault("install_intents", {})[path] = expected
        ctx.save()
        sudo.run(ctx, "file", path=path)
        if sudo.digest(ctx, path) != expected:
            raise InstallError(f"Server file publication did not match: {path}")
        server["installed"][path] = expected
        server["install_intents"].pop(path, None)
        ctx.save()


def _hook_paths(ctx: Context) -> set[str]:
    hook = ctx.state["server"]["backup_hook"]
    if not hook:
        return set()
    return {
        sudo.BACKUP_SCRIPT,
        "/etc/systemd/system/garden-backup.service",
        f"/etc/systemd/system/{hook['unit']}.d/garden.conf",
    }


def server_install(ctx: Context) -> None:
    _directory(ctx, "dir_data", ["/var/lib/garden", "/var/lib/garden/.garden"])
    _directory(ctx, "dir_etc", ["/etc/garden", "/etc/garden/tls"])
    hook = ctx.state["server"]["backup_hook"]
    hook_dirs = (
        {hook["export_dir"], f"/etc/systemd/system/{hook['unit']}.d"} if hook else set()
    )
    for path in sudo.directories(ctx):
        if path not in hook_dirs:
            _directory(ctx, "dir_sys", [path], path=path)
    _publish(ctx, [path for path in sudo.files(ctx) if path not in _hook_paths(ctx)])
    sudo.run(ctx, "reload")


def server_hook(ctx: Context) -> None:
    hook = _require(ctx)["backup_hook"]
    if hook:
        for path in (hook["export_dir"], f"/etc/systemd/system/{hook['unit']}.d"):
            _directory(ctx, "dir_sys", [path], path=path)
        _publish(ctx, sorted(_hook_paths(ctx)))
        sudo.run(ctx, "reload")


def _units(ctx: Context, action: str) -> None:
    units = reversed(sudo.UNITS) if action in {"stop", "disable"} else sudo.UNITS
    for unit in units:
        sudo.run(ctx, "unit", action=action, unit=unit)


def _verify(ctx: Context) -> None:
    garden.verify(ctx)
    garden.profiles(ctx)


def _restart(ctx: Context) -> None:
    _units(ctx, "stop")
    _units(ctx, "start")
    external_ready(ctx)
    _verify(ctx)


def run(ctx: Context) -> dict[str, Any]:
    _require(ctx)
    if ctx.state["garden"].get("origin") != "server":
        raise InstallError("Garden server origin is missing")
    if "remove" in ctx.state["garden"]:
        raise InstallError("Garden removal is recorded; run garden-remove again")
    from .workflow import stage

    ctx.state["status"] = "installing"
    ctx.save()
    actions = {
        "preflight": lambda: preflight(ctx),
        "garden_files": lambda: garden.prepare(ctx),
        "verify": lambda: garden.enrol(ctx, external_ready(ctx)),
        "garden_prepare": lambda: prepare(ctx),
        "server_account": lambda: server_account(ctx),
        "server_install": lambda: server_install(ctx),
        "server_hook": lambda: server_hook(ctx),
        "garden_start": lambda: _units(ctx, "start"),
        "garden_verify": lambda: _verify(ctx),
        "garden_restart": lambda: _restart(ctx),
        "garden_enable": lambda: _units(ctx, "enable"),
    }
    try:
        for key in STAGES:
            stage(ctx, key, actions[key])
        ctx.state["status"] = "verified"
        ctx.state.pop("last_error", None)
        ctx.save()
        return status(ctx)
    except (InstallError, KeyboardInterrupt) as error:
        ctx.state["status"] = (
            "interrupted"
            if isinstance(error, KeyboardInterrupt)
            or getattr(error, "code", "") == "interrupted"
            else "failed"
        )
        ctx.state["last_error"] = ctx.redact(str(error))
        ctx.save()
        raise


def status(ctx: Context) -> dict[str, Any]:
    server = _require(ctx)
    return {
        "name": ctx.name,
        "mode": ctx.mode,
        "status": ctx.state["status"],
        "instance_id": ctx.instance_id,
        "features": "Garden server",
        "endpoint": server["cairn"]["url"],
        "steps": ctx.state["steps"],
        "state": str(ctx.directory / "state.json"),
        "log": str(ctx.directory / "commands.log"),
        "credential_file": server["cairn"]["admin_token_file"],
        "garden_endpoint": ctx.state["garden"]["options"]["endpoint"],
        "garden_profiles": str(ctx.root / "garden/profiles"),
        "garden": {
            "enabled": ctx.state["garden"].get("remove", {}).get("phase") != "complete",
            "origin": "server",
            "data_policy": "Removal keeps /var/lib/garden; --delete-data is refused.",
            "remove_phase": ctx.state["garden"].get("remove", {}).get("phase"),
        },
        "server": {
            "cairn": server["cairn"],
            "account": server["account"],
            "installed": dict(server["installed"]),
            "digest_check": "not_checked" if server["installed"] else "not_installed",
            "backup_hook": server["backup_hook"],
        },
        "note": "Recorded result; status does not probe unit activity or installed file digests.",
    }


def check_remove(ctx: Context, *, delete_data: bool = False) -> None:
    _require(ctx)
    if delete_data:
        raise InstallError(
            "--delete-data is refused for garden-server; /var/lib/garden is kept",
            "invalid_arguments",
        )
    _installed(ctx, allow_missing=True)


def remove(ctx: Context, *, delete_data: bool = False) -> dict[str, Any]:
    check_remove(ctx, delete_data=delete_data)
    server = ctx.state["server"]
    record = ctx.state["garden"].setdefault("remove", {"phase": "revoking"})
    ctx.save()
    if record["phase"] == "complete":
        return status(ctx)
    from .workflow import stage

    if record["phase"] == "revoking":
        if ctx.state["garden"].get("agents"):
            stage(ctx, "garden_revoke", lambda: garden.revoke(ctx, external_ready(ctx)))
        record["phase"] = "removing"
        ctx.save()
    # A failed no-adoption preflight owns no units. Never stop an existing
    # installation merely because its unit names match this topology.
    owned = set(server["installed"]) | set(server.get("install_intents", {}))
    for action in ("stop", "disable"):
        if record.get(action) == "complete":
            continue
        for unit in reversed(sudo.UNITS):
            if f"/etc/systemd/system/{unit}" in owned:
                sudo.run(ctx, "unit", action=action, unit=unit)
        record[action] = "complete"
        ctx.save()
    for path in sorted(
        set(server["installed"]) | set(server.get("install_intents", {})), reverse=True
    ):
        # The daemon configuration belongs to the retained data tree too.
        if path.startswith("/var/lib/garden/"):
            continue
        sudo.run(ctx, "rm", path=path)
        server["installed"].pop(path, None)
        server.get("install_intents", {}).pop(path, None)
        ctx.save()
    if owned:
        sudo.run(ctx, "reload")
    record["phase"] = "complete"
    ctx.state["status"] = "rolled_back"
    ctx.save()
    return status(ctx)


def backup(ctx: Context) -> dict[str, Any]:
    server = _require(ctx)
    if not server["backup_hook"]:
        raise InstallError(
            "garden-server backup requires backup_hook", "invalid_arguments"
        )
    if ctx.state["status"] != "verified" or "remove" in ctx.state["garden"]:
        raise InstallError("Garden server backup requires a verified installation")
    _installed(ctx)
    sudo.run(ctx, "backup")
    return {
        "name": ctx.name,
        "mode": ctx.mode,
        "status": "complete",
        "export_dir": server["backup_hook"]["export_dir"],
    }
