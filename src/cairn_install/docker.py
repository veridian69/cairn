"""Owned Docker Compose backend for the guided installer."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from . import garden
from .core import Context, InstallError, _fsync_directory, read_owned, secure_directory
from .docker_assets import FALKORDB_IMAGE, render_compose, render_config
from .garden import garden_fingerprint, source_version

_INSTANCE_LABEL = "io.cairn.install.instance"
_RUN_LABEL = "io.cairn.install.run"
_PREFLIGHT_LABEL = "io.cairn.install.preflight"
_CONFIG_PATH = "/etc/cairn/config.yaml"
_SOURCE_DIGEST_LABEL = "io.cairn.source.digest"
_VCS_REVISION_LABEL = "org.opencontainers.image.revision"
_BRIDGE_READY_SECONDS = 30
_GARDEN_SERVICES = frozenset({"garden", "garden-secret-init"})
_GARDEN_CAPTURE_LIMIT = 8 * 1024**3

_GARDEN_TAR_CODE = """\
import os, stat, sys, tarfile
from pathlib import Path
root = Path('/src') / sys.argv[1]
def ordinary(member):
    if not (member.isdir() or member.isfile()):
        raise ValueError('Garden copy contains a non-regular member')
    return member
for path in (root, *root.parents):
    if path == Path('/src'):
        break
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError('Garden copy path is not a directory')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    archive.add(root, arcname=root.name, filter=ordinary)
"""

_GARDEN_SIZE_CODE = """\
import os, stat, sys
from pathlib import Path
root = Path('/src') / sys.argv[1]
size = 0
for directory, directories, files in os.walk(root, followlinks=False):
    for name in directories + files:
        info = (Path(directory) / name).lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('Garden copy contains a non-regular member')
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
print(size)
"""

_GARDEN_RESTORE_CODE = """\
import hashlib, os, shutil, stat, sys, tarfile
from pathlib import Path, PurePosixPath
root = Path('/src')
data, aside, stage = (root / p for p in sys.argv[1:])
for path in (data, aside, stage):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError('Unexpected Garden restore path')
def members(archive):
    seen = set()
    for member in archive:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or path.parts[:1] != ('data',)
                or str(path) != member.name or member.name in seen
                or not (member.isdir() or member.isfile())):
            raise ValueError('Unsafe Garden restore member')
        seen.add(member.name)
        yield member, path.parts[1:]
    if 'data/state.db' not in seen:
        raise ValueError('Garden restore lacks data/state.db')
def digest(stream):
    hashed = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        hashed.update(block)
    return hashed.hexdigest()
def tree(top):
    found = {}
    for directory, directories, files in os.walk(top, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            key = path.relative_to(top).as_posix()
            if stat.S_ISDIR(mode):
                found[key] = None
            elif stat.S_ISREG(mode):
                with path.open('rb') as stream:
                    found[key] = digest(stream)
            else:
                raise ValueError('Garden data changed after interrupted restore')
    return found
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    if aside.exists() and data.exists():
        # Publication landed before its engine phase was saved.
        expected = {}
        for member, parts in members(archive):
            if parts:
                expected['/'.join(parts)] = (
                    None if member.isdir() else digest(archive.extractfile(member))
                )
        if tree(data) != expected:
            raise ValueError('Garden data changed after interrupted restore')
        os.sync()
        raise SystemExit(0)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    for member, parts in members(archive):
        target = stage.joinpath(*parts)
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as output:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
if not aside.exists():
    data.rename(aside)
stage.rename(data)
os.sync()
"""

_GARDEN_SNAPSHOT_LIST_CODE = """\
import re, stat, sys
from pathlib import Path
root = Path('/src/backups')
if root.is_symlink() or (root.exists() and not root.is_dir()):
    raise ValueError('Unexpected Garden backup directory')
if root.exists():
    for path in root.iterdir():
        if re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-' + re.escape(sys.argv[1]), path.name):
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError('Unexpected Garden snapshot path')
            print(path.name)
"""

_GARDEN_SNAPSHOT_DELETE_CODE = """\
import shutil, sys
from pathlib import Path
root = Path('/src/backups')
if root.is_symlink():
    raise ValueError('Garden backup directory is a symlink')
path = root / sys.argv[1]
if path.is_symlink() or (path.exists() and not path.is_dir()):
    raise ValueError('Unexpected Garden snapshot path')
if path.exists():
    shutil.rmtree(path)
"""


def _clean_git_revision(source: Path) -> str | None:
    """Return HEAD only when source is exactly a clean Git worktree root."""

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(source), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    try:
        top = git("rev-parse", "--show-toplevel")
        if (
            top.returncode != 0
            or Path(top.stdout.strip()).resolve() != source.resolve()
        ):
            return None
        revision_result = git("rev-parse", "--verify", "HEAD")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = revision_result.stdout.strip().lower()
    if (
        revision_result.returncode != 0
        or status.returncode != 0
        or status.stdout
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) is None
    ):
        return None
    return revision


_SERVER_CODE = """\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b"cairn-compose-bridge-ok\\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(BODY)


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""

_CLIENT_CODE = """\
import http.client
import socket

host = "compose-http"
port = 8000
addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
if not addresses:
    raise SystemExit("DNS_FAILED: no addresses")
print("DNS_OK")
errors = []
for family, socket_type, protocol, _, address in addresses:
    connection = socket.socket(family, socket_type, protocol)
    connection.settimeout(5)
    try:
        connection.connect(address)
        print("TCP_OK")
        connection.sendall(
            b"GET /probe HTTP/1.1\\r\\nHost: compose-http\\r\\nConnection: close\\r\\n\\r\\n"
        )
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = response.read(1024)
        if response.status != 200 or body != b"cairn-compose-bridge-ok\\n":
            raise SystemExit("HTTP_FAILED: fixed response did not match")
        print("HTTP_OK")
        raise SystemExit(0)
    except OSError as error:
        errors.append(str(error))
    finally:
        connection.close()
raise SystemExit("TCP_FAILED: " + "; ".join(errors))
"""


class Backend:
    """Compose lifecycle constrained to resources labelled for one run."""

    def close(self) -> None:
        """Persistent containers remain available after the installer exits."""

    def wait_foreground(self) -> None:
        raise InstallError("Foreground mode requires a disposable installation")

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.project = f"cairn-install-{ctx.name}"
        fingerprint = str(ctx.state.get("source_fingerprint", "source"))
        suffix = re.sub(r"[^a-z0-9_.-]", "-", fingerprint.lower())[:24]
        self.image = f"{self.project}:{suffix or 'source'}"
        self._legacy_garden_image = f"{self.project}-garden:{suffix or 'source'}"
        self.compose_path = ctx.root / "compose.yaml"
        self.env_path = ctx.root / "compose.env"
        self.config_path = ctx.root / "config.yaml"
        self.credentials_path = ctx.root / "credentials"
        self._docker = ctx.state.setdefault("resources", {}).setdefault(
            "docker",
            {
                "project": self.project,
                "image": {"reference": self.image},
                "owner_labels": self._owner_labels,
                "containers": {},
                "network": {},
                "volumes": {},
            },
        )
        if self._docker.get("project") != self.project:
            raise InstallError(
                "Recorded Docker project does not match this installation"
            )
        if self._docker.get("owner_labels") != self._owner_labels:
            raise InstallError("Recorded Docker ownership labels do not match this run")

    @property
    def garden_image(self) -> str:
        artefact = self.ctx.state.get("garden", {}).get("source", {}).get("artefact")
        return str(artefact["image"]) if artefact else self._legacy_garden_image

    @property
    def garden_enabled(self) -> bool:
        return bool(self.ctx.state.get("garden"))

    @property
    def _garden_layout_active(self) -> bool:
        """Garden services and volumes belong to the live layout until detach completes."""
        detach = self._docker.get("garden_detach")
        complete = isinstance(detach, dict) and detach.get("phase") == "complete"
        return self.garden_enabled and not complete

    def _check_garden_options(self) -> None:
        if self.garden_enabled:
            if self.ctx.state["garden"]["options"].get("image"):
                raise InstallError(
                    "Docker Garden requires an owned source build; --garden-image is not supported"
                )
            if not (self.ctx.source / "garden/Dockerfile").is_file():
                raise InstallError("Source checkout has no Garden garden/Dockerfile")

    @property
    def _all_compose(self) -> list[str]:
        return (
            [*self._compose, "--profile", "garden"]
            if self.garden_enabled
            else self._compose
        )

    @property
    def runtime_python(self) -> str:
        return sys.executable

    @property
    def _owner_labels(self) -> dict[str, str]:
        return {
            _INSTANCE_LABEL: self.ctx.instance_id,
            _RUN_LABEL: self.ctx.run_id,
        }

    @property
    def _compose(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            str(self.ctx.root),
            "--env-file",
            str(self.env_path),
            "-f",
            str(self.compose_path),
            "-p",
            self.project,
        ]

    def _command(
        self,
        argv: list[str],
        *,
        timeout: float = 120,
        allowed: tuple[int, ...] = (0,),
    ) -> str:
        return self.ctx.command(
            argv,
            cwd=self.ctx.directory,
            env={},
            timeout=timeout,
            allowed=allowed,
        )

    def preflight(self) -> None:
        self._check_garden_options()
        if not (3, 12) <= sys.version_info[:2] <= (3, 14):
            raise InstallError("The installer needs host Python 3.12–3.14")
        if platform.system() != "Linux" or platform.machine() not in {
            "x86_64",
            "amd64",
        }:
            raise InstallError("Docker mode supports Linux x86_64 only")
        docker_version = self._command(
            [
                "docker",
                "version",
                "--format",
                "{{.Client.Version}} {{.Server.Version}}",
            ]
        )
        versions = docker_version.split()
        if len(versions) != 2 or any(
            _version_tuple(version) < (25, 0) for version in versions
        ):
            raise InstallError(
                "Docker Engine client and server 25.0 or newer are required"
            )
        compose_version = self._command(["docker", "compose", "version", "--short"])
        if _version_tuple(compose_version.strip()) < (2, 20, 2):
            raise InstallError("Docker Compose 2.20.2 or newer is required")
        daemon = self._command(
            ["docker", "info", "--format", "{{.OSType}} {{.Architecture}}"]
        ).strip()
        if daemon not in {"linux x86_64", "linux amd64"}:
            raise InstallError("Docker daemon must be Linux x86_64")
        if not (self.ctx.source / "Dockerfile").is_file():
            raise InstallError("Source checkout has no Dockerfile")
        self._check_local_falkordb()
        if not self._project_container_ids():
            _check_loopback_port(self.ctx.port)
            if "garden" in self.ctx.state:
                self._check_garden_listener()

    def _check_garden_listener(self) -> None:
        options = self.ctx.state["garden"]["options"]
        garden.require_listener_available(
            int(options["port"]),
            wildcard=True,
            address=options.get("listen_address"),
        )

    def _falkordb_image(self) -> str:
        runtime = self.ctx.state.get("falkordb_runtime")
        if runtime is None:
            recorded = self._docker.get("semantic", {}).get("falkordb_image")
            if isinstance(recorded, str) and re.fullmatch(
                r"[^@\s]+@sha256:[0-9a-f]{64}", recorded
            ):
                return recorded
            return FALKORDB_IMAGE
        image = runtime.get("image") if isinstance(runtime, dict) else None
        if not isinstance(image, str) or not re.fullmatch(
            r"cairn\.local/falkordb-runtime@sha256:[0-9a-f]{64}", image
        ):
            raise InstallError("Invalid local FalkorDB runtime image")
        recorded = self._docker.get("semantic", {}).get("falkordb_image", image)
        if recorded != image:
            raise InstallError("Recorded FalkorDB runtime image changed")
        return image

    def _check_local_falkordb(self) -> None:
        if not self.ctx.semantic or "falkordb_runtime" not in self.ctx.state:
            return
        image = self._falkordb_image()
        try:
            items = json.loads(self._command(["docker", "image", "inspect", image]))
            valid = (
                isinstance(items, list)
                and len(items) == 1
                and isinstance(items[0], dict)
                and image in (items[0].get("RepoDigests") or [])
                and items[0].get("Os") == "linux"
                and items[0].get("Architecture") == "amd64"
            )
        except (InstallError, ValueError, TypeError):
            valid = False
        if not valid:
            raise InstallError(
                "Exact local FalkorDB runtime is unavailable; build or load the "
                "retained runtime into Docker's containerd image store"
            )

    def prepare(self) -> None:
        self._check_garden_options()
        self._check_local_falkordb()
        password_file = self.credentials_path / "falkordb-password.source"
        if self.ctx.semantic:
            password_present = password_file.exists() or password_file.is_symlink()
            password_recorded = str(password_file.absolute()) in self.ctx.state.get(
                "owned_files", {}
            ) or str(password_file.absolute()) in self.ctx.state.get("file_intents", {})
            if password_recorded and not password_present:
                raise InstallError(
                    f"Previously recorded FalkorDB credential is missing: {password_file}"
                )
        self.ctx.save()
        self.validate_ownership()
        self._prepare_directories()
        provider: Path | None = None
        if self.ctx.semantic:
            provider = Path(str(self.ctx.state.get("provider_key_file", "")))
            if not provider.is_absolute():
                raise InstallError("Semantic mode needs a protected provider key file")
            if str(provider.absolute()) not in self.ctx.state.get("owned_files", {}):
                raise InstallError(f"Refusing unowned provider credential: {provider}")
            self.ctx.check_file(provider)
            self.ctx.read_secret(provider)
            if password_file.exists() or password_file.is_symlink():
                if not password_recorded:
                    raise InstallError(
                        f"Refusing unowned FalkorDB credential: {password_file}"
                    )
                self.ctx.check_file(password_file)
                password = self.ctx.read_secret(password_file)
            else:
                password = secrets.token_hex(32)
                self.ctx.write_file(password_file, password + "\n", secret=True)
            self.ctx.add_secret(password)
            self._docker["semantic"] = {
                "falkordb_image": self._falkordb_image(),
                "provider_key_file": str(provider),
                "falkordb_password_file": str(password_file),
            }
            self.ctx.save()
        self.ctx.write_file(
            self.config_path,
            render_config(self.ctx.instance_id, semantic=self.ctx.semantic),
            mode=0o644,
        )
        self.ctx.write_file(self.env_path, "\n", mode=0o600)
        self.ctx.write_file(
            self.compose_path,
            self._compose_text(garden_enabled=self.garden_enabled),
            mode=0o644,
        )
        self._build_image()
        if self.garden_enabled:
            self._build_image(garden=True)
        self._command([*self._compose, "config", "--quiet"])
        self._bridge_preflight()
        self._docker["project_intent"] = "create_without_starting"
        self.ctx.save()
        self._command([*self._compose, "create", "--no-build"], timeout=600)
        self.validate_ownership()
        self._docker["project_state"] = "created"
        self._docker["prepared"] = True
        self.ctx.save()

    def _compose_text(
        self, *, garden_enabled: bool, garden_image: str | None = None
    ) -> str:
        """Render the Compose file; garden_enabled is explicit so detach can ignore state."""
        password_file = self.credentials_path / "falkordb-password.source"
        provider: Path | None = None
        password_file_for_compose: Path | None = None
        if self.ctx.semantic:
            provider = Path(str(self.ctx.state.get("provider_key_file", "")))
            password_file_for_compose = password_file
        options = self.ctx.state.get("garden", {}).get("options", {})
        return render_compose(
            image=self.image,
            port=self.ctx.port,
            root=self.ctx.root,
            instance_id=self.ctx.instance_id,
            run_id=self.ctx.run_id,
            semantic=self.ctx.semantic,
            provider_key_file=provider,
            falkordb_password_file=password_file_for_compose,
            falkordb_image=self._falkordb_image(),
            falkordb_local="falkordb_runtime" in self.ctx.state,
            garden_image=(garden_image or self.garden_image)
            if garden_enabled
            else None,
            garden_port=int(options.get("port", 8443)),
            garden_listen=garden.listen_address(options),
        )

    def _prepare_directories(self) -> None:
        self._docker["credentials_directory"] = {
            "path": str(self.credentials_path),
            "intent": "create",
        }
        self.ctx.save()
        try:
            self.credentials_path.mkdir(mode=0o700, exist_ok=True)
            info = self.credentials_path.lstat()
        except OSError as error:
            raise InstallError(
                f"Cannot prepare Docker credentials directory: {error}"
            ) from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise InstallError(
                f"Docker credentials directory must be owned mode 0700: {self.credentials_path}"
            )
        self._docker["credentials_directory"] = {
            "path": str(self.credentials_path),
            "status": "owned",
        }
        self.ctx.save()

    def _build_image(
        self,
        *,
        garden: bool = False,
        source: Path | None = None,
        version: str | None = None,
        reference: str | None = None,
    ) -> dict[str, str]:
        staging = reference is not None
        reference = reference or (self.garden_image if garden else self.image)
        key = "garden_image" if garden else "image"
        receipts = (
            self._docker.setdefault("garden_builds", {}) if staging else self._docker
        )
        key = reference if staging else key
        source = source or self.ctx.source
        context = source / "garden" if garden else source
        fingerprint = (
            garden_fingerprint(source)
            if staging
            else str(self.ctx.state.get("source_fingerprint", "unknown"))
        )
        image_ids = self._ids(
            "image",
            ["--filter", f"reference={reference}"],
            extra=["--no-trunc"],
        )
        if image_ids:
            labels, image_id = self._image_details(reference)
            self._require_owned(labels, "Docker image", reference)
            if staging and labels.get(_SOURCE_DIGEST_LABEL) != f"sha256:{fingerprint}":
                raise InstallError(
                    f"Garden image source fingerprint changed: {reference}"
                )
            if receipts.get(key, {}).get("id", image_id) != image_id:
                raise InstallError(
                    f"Recorded Docker image reference was replaced: {reference}"
                )
            receipts[key] = {"reference": reference, "id": image_id}
            self.ctx.save()
            return dict(receipts[key])
        receipts[key] = {"reference": reference, "intent": "build"}
        self.ctx.save()
        label_arguments: list[str] = []
        for label, value in self._owner_labels.items():
            label_arguments.extend(["--label", f"{label}={value}"])
        label_arguments.extend(
            ["--label", f"{_SOURCE_DIGEST_LABEL}=sha256:{fingerprint}"]
        )
        revision = _clean_git_revision(source)
        if revision is not None:
            label_arguments.extend(["--label", f"{_VCS_REVISION_LABEL}={revision}"])
        self._command(
            [
                "docker",
                "build",
                "--pull=false",
                *label_arguments,
                "--build-arg",
                f"VERSION={version or source_version(source)}",
                "--tag",
                reference,
                str(context),
            ],
            timeout=1800,
        )
        observed, image_id = self._image_details(reference)
        self._require_owned(observed, "Docker image", reference)
        receipts[key] = {"reference": reference, "id": image_id}
        self.ctx.save()
        return dict(receipts[key])

    def lifecycle_argv(self, operation: str) -> list[str]:
        if operation not in {"check-config", "migrate", "verify", "bootstrap"}:
            raise InstallError(f"Unsupported Docker lifecycle operation: {operation}")
        return [
            *self._compose,
            "run",
            "--rm",
            "--no-deps",
            "cairn",
            operation,
            "--config",
            _CONFIG_PATH,
        ]

    def validate_ownership(self, *, require_garden_image: bool = False) -> None:
        if require_garden_image and self._blitz_image_id(garden=True) is None:
            raise InstallError(
                "Owned Garden image is missing; restore it before upgrading"
            )
        self._reconcile_interrupted_removals()
        if self.ctx.semantic:
            self._falkordb_image()
        paths = [self.config_path, self.env_path, self.compose_path]
        if self._garden_layout_active:
            paths.extend(
                self.ctx.root / "garden" / name
                for name in ("host.json", "tls/server.crt", "tls/server.key")
            )
        pending = self.ctx.state.get("file_replacements", {})
        compose_key = str(self.compose_path.absolute())
        # Only the compose file has a journaled replacement the owning hook
        # resumes; any other pending file, checked here or not, stops lifecycle.
        for key in sorted(pending):
            if key != compose_key:
                raise InstallError(
                    f"Owned file has an interrupted replacement; resume the operation: {key}"
                )
        for path in paths:
            key = str(path.absolute())
            if key in pending:
                # Its content must still be one of the two journaled digests.
                observed = hashlib.sha256(read_owned(path)).hexdigest()
                if observed not in (pending[key]["old"], pending[key]["new"]):
                    raise InstallError(
                        f"Interrupted replacement left unexpected content; preserve and inspect: {path}"
                    )
                continue
            if (
                path.exists()
                or path.is_symlink()
                or str(path.absolute()) in self.ctx.state.get("owned_files", {})
            ):
                self.ctx.check_file(path)
        if self.ctx.semantic:
            semantic = self._docker.get("semantic", {})
            for value in (
                semantic.get("provider_key_file"),
                semantic.get("falkordb_password_file"),
            ):
                if value:
                    path = Path(str(value))
                    if str(path.absolute()) in self.ctx.state.get("owned_files", {}):
                        self.ctx.check_file(path)
                    else:
                        self.ctx.read_secret(path)

        containers: dict[str, str] = {}
        for container_id in self._project_container_ids():
            labels = self._inspect_labels("container", container_id)
            self._require_owned(labels, "Docker container", container_id)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(
                    f"Docker container has wrong project label: {container_id}"
                )
            service = labels.get("com.docker.compose.service")
            if not service:
                raise InstallError(
                    f"Docker container has no Compose service label: {container_id}"
                )
            containers[service] = container_id

        network: dict[str, str] = {}
        network_name = f"{self.project}_default"
        for network_id in self._ids("network", ["--filter", f"name=^{network_name}$"]):
            labels = self._inspect_labels("network", network_id)
            self._require_owned(labels, "Docker network", network_name)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(
                    f"Docker network has wrong project label: {network_name}"
                )
            network = {"name": network_name, "id": network_id}

        recorded_volumes = dict(self._docker.get("volumes", {}))
        volumes: dict[str, str] = {}
        for short_name in self._volume_names:
            name = f"{self.project}_{short_name}"
            for volume_id in self._ids("volume", ["--filter", f"name=^{name}$"]):
                labels = self._inspect_labels("volume", volume_id)
                self._require_owned(labels, "Docker volume", name)
                if labels.get("com.docker.compose.project") != self.project:
                    raise InstallError(f"Docker volume has wrong project label: {name}")
                volumes[short_name] = volume_id
        missing_volumes = sorted(set(recorded_volumes) - set(volumes))
        if missing_volumes:
            raise InstallError(
                "recorded Docker volume is missing; refusing to create an empty "
                f"replacement: {', '.join(missing_volumes)}"
            )

        images = [("image", self.image)]
        if self._garden_layout_active:
            current = self._docker.get("garden_images", {}).get("current", {})
            images.append(("garden_image", current.get("reference", self.garden_image)))
        for key, reference in images:
            self._docker.setdefault(key, {"reference": reference})
            image_ids = self._ids(
                "image", ["--filter", f"reference={reference}"], extra=["--no-trunc"]
            )
            if image_ids:
                labels, image_id = self._image_details(reference)
                self._require_owned(labels, "Docker image", reference)
                receipt = self._docker[key]
                if (
                    receipt.get("reference") != reference
                    or receipt.get("id", image_id) != image_id
                ):
                    raise InstallError(
                        f"Recorded Docker image reference was replaced: {reference}"
                    )
                self._docker[key] = {"reference": reference, "id": image_id}
        for role, receipt in self._docker.get("garden_images", {}).items():
            if (
                receipt
                and self._check_garden_image(receipt) is None
                and (
                    role == "current"
                    and receipt.get("reference") != self._legacy_garden_image
                )
                and not self._can_rollback_missing_garden_image()
            ):
                # Resume rebuilds only from the Cairn source, never the
                # recorded Garden source, so an upgraded image cannot return.
                raise InstallError(
                    f"Upgraded Garden image is missing: {receipt['reference']}; "
                    "restore it or run garden-upgrade --rollback"
                )
        if self._docker.get("retained_garden_data") is not None:
            self._check_retained_garden_data(volumes)
        self._docker["containers"] = containers
        self._docker["network"] = network
        self._docker["volumes"] = volumes
        self.ctx.save()

    def _can_rollback_missing_garden_image(self) -> bool:
        journal = self.ctx.state.get("garden", {}).get("upgrade", {})
        if journal.get("direction") != "rollback" or journal.get("phase") not in {
            "stopped",
            "copied",
            "swapped",
        }:
            return False
        previous = self._docker.get("garden_images", {}).get("previous", {})
        target = journal.get("to", {}).get("artefact")
        return (
            bool(previous)
            and target == {"image": previous.get("reference"), "id": previous.get("id")}
            and self._check_garden_image(previous) is not None
        )

    def _reconcile_interrupted_removals(self) -> None:
        """Finish bookkeeping for a removal whose effect landed before its save."""
        self._reconcile_garden_image_removal()
        intent = self._docker.get("volume_removal")
        if intent is not None and not self._ids(
            "volume", ["--filter", f"name=^{intent['volume']}$"]
        ):
            self._docker["volumes"] = {
                short: volume
                for short, volume in self._docker.get("volumes", {}).items()
                if volume != intent["volume"]
            }
            del self._docker["volume_removal"]
            self.ctx.save()
        deletions = self._docker.get("file_deletions", [])
        gone = [key for key in deletions if not _path_exists(Path(key))]
        if gone:
            for key in gone:
                for table in ("owned_files", "file_intents"):
                    self.ctx.state.get(table, {}).pop(key, None)
                deletions.remove(key)
            if not deletions:
                del self._docker["file_deletions"]
            self.ctx.save()

    def _retained_garden_volume(self, retained: object) -> str:
        name = f"{self.project}_garden-data"
        if not isinstance(retained, dict) or retained.get("volume") != name:
            raise InstallError("Recorded retained Garden data ownership is malformed")
        return name

    def _check_retained_garden_data(self, volumes: dict[str, str]) -> None:
        """Kept Garden data stays owned until garden-attach reuses it.

        Compose reattaches the same named volume, so once garden-data is live again
        the retained receipt is moved back to the live volume receipt.
        """
        name = self._retained_garden_volume(self._docker["retained_garden_data"])
        if "garden-data" in volumes:
            del self._docker["retained_garden_data"]
            return
        identifiers = self._ids("volume", ["--filter", f"name=^{name}$"])
        if not identifiers:
            raise InstallError(
                "retained Garden data volume is missing; refusing to create an empty "
                "replacement"
            )
        for identifier in identifiers:
            labels = self._inspect_labels("volume", identifier)
            self._require_owned(labels, "Docker volume", name)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(f"Docker volume has wrong project label: {name}")

    @property
    def _volume_names(self) -> tuple[str, ...]:
        names: tuple[str, ...] = ("cairn-data",)
        if self.ctx.semantic:
            names += ("cairn-credentials", "falkordb-config", "falkordb-data")
        if self._garden_layout_active:
            names += ("garden-data", "garden-tls")
        return names

    def garden_prepare(self) -> None:
        self._check_garden_options()
        self.validate_ownership()
        config = garden.gateway_config(
            self.ctx,
            data_dir="/var/lib/garden/data",
            daemon_url_file="/var/lib/garden/run/daemon.url",
            cert_file="/var/run/secrets/garden/server.crt",
            key_file="/var/run/secrets/garden/server.key",
            listen="0.0.0.0:9443",
            cairn_url="http://127.0.0.1:8000/memory/v1/diagnose",
        )
        garden.write_json_config(
            self.ctx,
            self.ctx.root / "garden/host.json",
            {
                "gateway": config,
                "stream": garden.retention(
                    self.ctx.state["garden"]["options"],
                    {"max_age": "0", "max_bytes": 0},
                ),
            },
            mode=0o644,
        )
        self._command(
            [
                *self._all_compose,
                "up",
                "--no-deps",
                "--no-build",
                "--abort-on-container-exit",
                "--exit-code-from",
                "garden-secret-init",
                "garden-secret-init",
            ],
            timeout=120,
        )
        self.validate_ownership()

    def garden_start(self) -> None:
        self.validate_ownership()
        if not self._docker["containers"].get("cairn"):
            raise InstallError("Garden needs the owned Cairn container")
        # Recreate explicitly: a recreated Cairn container has a new namespace.
        # Inspect the attachment before allowing Garden to authenticate or serve.
        self._command(
            [
                *self._all_compose,
                "up",
                "--no-start",
                "--no-deps",
                "--force-recreate",
                "--no-build",
                "garden",
            ],
            timeout=120,
        )
        self._check_garden_namespace()
        self._command(
            [
                *self._all_compose,
                "up",
                "--detach",
                "--no-deps",
                "--no-recreate",
                "--no-build",
                "--wait",
                "garden",
            ],
            timeout=120,
        )
        self._check_garden_namespace()

    def _check_garden_namespace(self) -> None:
        self.validate_ownership()
        containers = self._docker["containers"]
        central, cairn = containers.get("garden"), containers.get("cairn")
        if not central or not cairn:
            raise InstallError("Garden or Cairn container is missing")
        namespace = self._command(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.HostConfig.NetworkMode}}",
                central,
            ]
        ).strip()
        if namespace != "container:" + cairn:
            raise InstallError(
                "Garden is not attached to the owned Cairn network namespace"
            )

    def garden_stop(self) -> None:
        self.validate_ownership()
        if self._docker["containers"].get("garden"):
            self._command(
                [*self._all_compose, "stop", "--timeout", "30", "garden"], timeout=60
            )

    def garden_open_endpoint(self) -> int:
        self._check_garden_namespace()
        return int(self.ctx.state["garden"]["options"]["port"])

    def garden_close_endpoint(self) -> None:
        pass

    def _garden_hook(self) -> None:
        if self.ctx.mode != "docker":
            raise InstallError("Garden upgrade and backup hooks require Docker mode")
        if not self._garden_layout_active:
            raise InstallError("Garden is not attached to this Docker installation")
        self.validate_ownership()

    def _check_garden_image(self, receipt: dict[str, str]) -> str | None:
        reference = receipt.get("reference", "")
        if reference != self._legacy_garden_image and not re.fullmatch(
            re.escape(f"{self.project}-garden:g-") + r"[0-9a-f]{22}", reference
        ):
            raise InstallError("Recorded Garden image reference is malformed")
        recorded = receipt.get("id")
        if not recorded:
            if receipt.get("intent") != "build":
                raise InstallError("Recorded Garden image identity is missing")
            observed = self._optional_image_details(reference)
            if observed is None:
                return None
            labels, recorded = observed
            self._require_owned(labels, "Docker image", reference)
            fingerprint = labels.get(_SOURCE_DIGEST_LABEL, "").removeprefix("sha256:")
            if not reference.endswith(f":g-{fingerprint[:22]}") or not re.fullmatch(
                r"[0-9a-f]{64}", fingerprint
            ):
                raise InstallError("Recorded Garden build source fingerprint changed")
            receipt.clear()
            receipt.update(reference=reference, id=recorded)
            self.ctx.save()
        present = False
        for identifier in (reference, recorded):
            observed = self._optional_image_details(identifier)
            if observed is None:
                continue
            labels, image_id = observed
            self._require_owned(labels, "Docker image", identifier)
            if image_id != recorded:
                raise InstallError(
                    f"Recorded Docker image reference was replaced: {reference}"
                )
            if identifier == recorded:
                present = True
        return recorded if present else None

    def _garden_one_shot(self, *, writable: bool = False) -> list[str]:
        self._garden_hook()
        if self._blitz_image_id() is None:
            raise InstallError("Owned Cairn image is missing")
        volume = f"{self.project}_garden-data"
        if self._docker.get("volumes", {}).get("garden-data") != volume:
            raise InstallError("Owned Garden data volume is missing")
        labels = [
            arg
            for key, value in self._owner_labels.items()
            for arg in ("--label", f"{key}={value}")
        ]
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            *labels,
            "-v",
            f"{volume}:/src:{'rw' if writable else 'ro'}",
            "--entrypoint",
            "python",
            self.image,
        ]

    def garden_stage(self, source: Path) -> dict[str, str]:
        self._garden_hook()
        fingerprint = garden_fingerprint(source)
        version = source_version(source)
        reference = f"{self.project}-garden:g-{fingerprint[:22]}"
        # The engine discards unpublished builds first. One that survived (a
        # container still uses it) may hold other bytes under this label.
        record = self.ctx.state["garden"]
        published = {
            item["artefact"]["image"]
            for item in (record.get("source"), record.get("previous"))
            if item and item.get("artefact")
        }
        if reference not in published and reference in self._docker.get(
            "garden_builds", {}
        ):
            raise InstallError(
                "An unpublished Garden image could not be discarded; remove the "
                f"containers using it and rerun: {reference}"
            )
        receipt = self._build_image(
            garden=True, source=source, version=version, reference=reference
        )
        labels = [
            arg
            for key, value in self._owner_labels.items()
            for arg in ("--label", f"{key}={value}")
        ]
        observed = self._command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                *labels,
                "--entrypoint",
                "/usr/local/bin/garden",
                receipt["id"],
                "--version",
            ]
        )
        if observed.strip() != f"garden version {version}":
            raise InstallError("Staged Garden image reports an unexpected version")
        return {"image": reference, "id": receipt["id"]}

    @staticmethod
    def _garden_file_digest(path: Path) -> tuple[str, int]:
        secure_directory(path.parent)
        try:
            with os.fdopen(
                os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
            ) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise InstallError(
                        f"Garden capture must be an owned mode 0600 file: {path}"
                    )
                return hashlib.file_digest(stream, "sha256").hexdigest(), info.st_size
        except OSError as error:
            raise InstallError(
                f"Cannot read Garden capture: {path}: {error}"
            ) from error

    def _check_garden_file(self, path: Path) -> None:
        digest, _ = self._garden_file_digest(path)
        key = str(path.absolute())
        expected = self.ctx.state.get("owned_files", {}).get(key) or self.ctx.state.get(
            "file_intents", {}
        ).get(key)
        if expected != digest:
            raise InstallError(f"Owned Garden file changed or is unowned: {path}")
        if key not in self.ctx.state["owned_files"]:
            self.ctx.state["owned_files"][key] = digest
            self.ctx.save()

    @staticmethod
    def _garden_tar(path: Path, root: str, *, cold: bool = False) -> tuple[str, int]:
        digest, size = Backend._garden_file_digest(path)
        try:
            if size > _GARDEN_CAPTURE_LIMIT:
                raise InstallError("Garden capture exceeds 8 GiB")
            seen: set[str] = set()
            files: set[str] = set()
            with tarfile.open(path, "r:") as archive:
                for member in archive:
                    relative = PurePosixPath(member.name)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or relative.parts[:1] != (root,)
                        or str(relative) != member.name
                        or member.name in seen
                        or not (member.isdir() or member.isfile())
                    ):
                        raise InstallError("Unsafe Garden tar member")
                    if member.offset_data + member.size > size:
                        raise InstallError("Truncated Garden tar member")
                    seen.add(member.name)
                    if member.isfile():
                        files.add(member.name)
            required = f"{root}/state.db" if cold else f"{root}/manifest.json"
            if required not in files:
                raise InstallError(f"Garden tar lacks {required}")
        except (OSError, tarfile.TarError) as error:
            raise InstallError(
                f"Cannot validate Garden tar: {path}: {error}"
            ) from error
        return digest, size

    def _garden_capture(self, dest: Path, member: str, *, cold: bool = False) -> None:
        argv = self._garden_one_shot()
        key = str(dest.absolute())
        captures = self._docker.setdefault("garden_captures", {})
        if _path_exists(dest) and captures.get(key) == {"intent": "capture"}:
            # stdout may be a partial yet syntactically valid tar after a helper
            # failure. Only a successful command may publish its digest.
            self._garden_file_digest(dest)
            dest.unlink()
        if _path_exists(dest):
            self._check_garden_file(dest)
        else:
            secure_directory(dest.parent, private=True)
            captures[key] = {"intent": "capture"}
            self.ctx.save()
            self.ctx.command(
                [*argv, "-c", _GARDEN_TAR_CODE, member],
                cwd=self.ctx.directory,
                env={},
                timeout=1800,
                private=True,
                stdout_path=dest,
                stdout_limit=_GARDEN_CAPTURE_LIMIT,
            )
        digest, size = self._garden_tar(dest, PurePosixPath(member).name, cold=cold)
        prior = captures.get(key, {})
        if prior.get("sha256", digest) != digest or prior.get("bytes", size) != size:
            raise InstallError("Garden capture digest or size changed")
        # The command synced the file, not its directory entry.
        _fsync_directory(dest.parent)
        self.ctx.state.setdefault("owned_files", {})[key] = digest
        captures[key] = {"sha256": digest, "bytes": size}
        self.ctx.save()

    def garden_copy(self, dest: Path) -> None:
        if not _path_exists(dest):
            # As the engine does natively: only this backend can measure the volume.
            measured = self.ctx.command(
                [*self._garden_one_shot(), "-c", _GARDEN_SIZE_CODE, "data"],
                cwd=self.ctx.directory,
                env={},
                timeout=600,
            ).strip()
            if not measured.isdigit():
                raise InstallError("Could not measure the Garden data volume")
            secure_directory(dest.parent, private=True)
            if shutil.disk_usage(dest.parent).free < int(measured) * 2:
                raise InstallError(
                    "Garden upgrade needs free space of at least twice the data size"
                )
        self._garden_capture(dest, "data", cold=True)

    def garden_swap(self, artefact: dict[str, str]) -> None:
        self._garden_hook()
        receipt = {"reference": artefact["image"], "id": artefact["id"]}
        known = [self._docker.get("garden_image", {})]
        known.extend(self._docker.get("garden_images", {}).values())
        known.extend(self._docker.get("garden_builds", {}).values())
        if receipt not in known or self._check_garden_image(receipt) is None:
            raise InstallError("Garden swap requires a recorded owned image")
        images = self._docker.setdefault("garden_images", {})
        current = images.get("current", self._docker.get("garden_image", {}))
        if current != receipt:
            self._docker.setdefault("garden_builds", {})[current["reference"]] = dict(
                current
            )
            images["previous"] = dict(current)
        images["current"] = receipt
        self._docker["garden_image"] = receipt
        self.ctx.save()
        self.ctx.replace_file(
            self.compose_path,
            self._compose_text(garden_enabled=True, garden_image=artefact["image"]),
            mode=0o644,
        )

    def garden_restore_data(self, copy: Path) -> None:
        argv = self._garden_one_shot(writable=True)
        self._check_garden_file(copy)
        digest, size = self._garden_tar(copy, "data", cold=True)
        upgrade = self.ctx.state["garden"].get("upgrade", {})
        identifier = str(upgrade.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
            raise InstallError("Garden restore requires a recorded upgrade id")
        receipt = upgrade.get("cold_copy", {})
        if receipt and (
            receipt.get("sha256") != digest
            or receipt.get("bytes") != size
            or receipt.get("path") != str(copy)
        ):
            raise InstallError("Garden cold copy digest, size or path changed")
        aside = f"/var/lib/garden/data.pre-restore-{identifier}"
        if upgrade.get("aside") not in (None, aside):
            raise InstallError("Recorded Garden restore aside path changed")
        upgrade["aside"] = aside
        self.ctx.save()
        # Docker only keeps stdin open with --interactive; no terminal is allocated.
        self.ctx.command(
            [
                *argv[:3],
                "--interactive",
                *argv[3:],
                "-c",
                _GARDEN_RESTORE_CODE,
                "data",
                f"data.pre-restore-{identifier}",
                f"data.restore-{identifier}",
            ],
            cwd=self.ctx.directory,
            env={},
            timeout=1800,
            private=True,
            stdin_path=copy,
        )

    def garden_snapshot(self, name: str) -> str:
        self._garden_hook()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name):
            raise InstallError("Invalid Garden snapshot name")
        candidates = self._command(
            [*self._garden_one_shot(), "-c", _GARDEN_SNAPSHOT_LIST_CODE, name]
        ).splitlines()
        if len(candidates) > 1:
            raise InstallError("Garden snapshot recovery is ambiguous")
        if candidates:
            self._garden_snapshot_id(candidates[0])
            return candidates[0]
        output = self._command(
            [
                *self._all_compose,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "/usr/local/bin/garden",
                "garden",
                "snapshot",
                "create",
                "--host-config",
                "/etc/garden/host.json",
                "--backup-dir",
                "/var/lib/garden/backups",
                name,
            ],
            timeout=1800,
        )
        match = re.fullmatch(
            r"snapshot created: ([A-Za-z0-9][A-Za-z0-9_-]{0,127}) \([^\n]+\)\s*",
            output.strip(),
        )
        if match is None:
            raise InstallError("Garden snapshot did not report a snapshot id")
        return match[1]

    @staticmethod
    def _garden_snapshot_id(snapshot_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", snapshot_id):
            raise InstallError("Invalid Garden snapshot id")

    def garden_export(self, snapshot_id: str, dest: Path) -> None:
        """Export contents into dest; the engine chooses the archive's root name."""
        self._garden_snapshot_id(snapshot_id)
        self._garden_hook()
        secure_directory(dest, private=True)
        exports = self._docker.setdefault("garden_exports", {})
        destination = str(dest.absolute())
        temp = dest / f".garden-export-{snapshot_id}"
        if exports.get(destination) not in (None, snapshot_id):
            raise InstallError("Resume the Garden export with the same snapshot id")
        if _path_exists(temp):
            if exports.get(destination) != snapshot_id:
                raise InstallError(
                    f"Garden export working directory is unowned: {temp}"
                )
            self._clear_garden_export(temp)
        exports[destination] = snapshot_id
        self.ctx.save()
        capture = temp / "snapshot.tar"
        try:
            secure_directory(temp, private=True)
            self._garden_capture(capture, f"backups/{snapshot_id}")
            with tarfile.open(capture, "r:") as archive:
                for member in archive:
                    path = dest.joinpath(*PurePosixPath(member.name).parts[1:])
                    if path == temp or path.is_relative_to(temp):
                        raise InstallError(
                            "Garden snapshot contains the export working path"
                        )
                    if member.isdir():
                        secure_directory(path, private=True)
                    else:
                        source = archive.extractfile(member)
                        if source is None:
                            raise InstallError(
                                f"Garden export member is not a file: {path}"
                            )
                        with source:
                            secure_directory(path.parent, private=True)
                            fd, temporary = tempfile.mkstemp(
                                prefix=".export-", dir=temp
                            )
                            try:
                                with os.fdopen(fd, "wb") as output:
                                    shutil.copyfileobj(source, output, 1024 * 1024)
                                    output.flush()
                                    os.fsync(output.fileno())
                                digest = self._garden_file_digest(Path(temporary))[0]
                                key = str(path.absolute())
                                if _path_exists(path):
                                    self._check_garden_file(path)
                                    if self.ctx.state["owned_files"][key] != digest:
                                        raise InstallError(
                                            f"Garden export member changed: {path}"
                                        )
                                else:
                                    intents = self.ctx.state.setdefault(
                                        "file_intents", {}
                                    )
                                    if intents.get(key, digest) != digest:
                                        raise InstallError(
                                            f"Garden export intent changed: {path}"
                                        )
                                    intents[key] = digest
                                    self.ctx.save()
                                    os.link(temporary, path, follow_symlinks=False)
                                    directory = os.open(
                                        path.parent, os.O_RDONLY | os.O_DIRECTORY
                                    )
                                    try:
                                        os.fsync(directory)
                                    finally:
                                        os.close(directory)
                                    self.ctx.state["owned_files"][key] = digest
                                    self.ctx.save()
                            finally:
                                os.unlink(temporary)
        finally:
            self._clear_garden_export(temp)
            exports.pop(destination, None)
            self.ctx.save()

    def _clear_garden_export(self, directory: Path) -> None:
        if _path_exists(directory):
            secure_directory(directory, private=True)
            shutil.rmtree(directory)
        for table in (
            self.ctx.state["owned_files"],
            self.ctx.state.get("file_intents", {}),
            self._docker.get("garden_captures", {}),
        ):
            for key in list(table):
                if Path(key).is_relative_to(directory):
                    del table[key]

    def garden_prune(self, keep: Sequence[dict[str, str]]) -> None:
        self._garden_hook()
        protected = {item["image"] for item in keep}
        protected_ids = {item["id"] for item in keep}
        if self._docker.get("image", {}).get("id"):
            protected_ids.add(self._docker["image"]["id"])
        # The engine prunes after publishing source and previous; the journal's
        # from/to would wrongly retain the release a rollback rejected.
        record = self.ctx.state["garden"]
        active = [record.get("source", {}), record.get("previous", {})]
        protected.update(
            item["artefact"]["image"] for item in active if item.get("artefact")
        )
        protected_ids.update(
            item["artefact"]["id"] for item in active if item.get("artefact")
        )
        builds = self._docker.get("garden_builds", {})
        for reference, receipt in list(builds.items()):
            if reference in protected or not reference.startswith(
                f"{self.project}-garden:g-"
            ):
                continue
            image_id = self._check_garden_image(receipt)
            if image_id is not None:
                # An immutable image can have several tags/receipts. Never remove
                # its content while another retained record still refers to it.
                other = [r for ref, r in builds.items() if ref in protected]
                other.extend(
                    r
                    for r in self._docker.get("garden_images", {}).values()
                    if r.get("reference") in protected
                )
                if image_id in protected_ids or any(
                    r.get("id") == image_id for r in other
                ):
                    continue
                if self._ids(
                    "container", ["--filter", f"ancestor={image_id}"], extra=["--all"]
                ):
                    continue
                self._command(["docker", "image", "rm", image_id])
            del builds[reference]
            for key, value in list(self._docker.get("garden_images", {}).items()):
                if value.get("reference") == reference:
                    del self._docker["garden_images"][key]
            self.ctx.save()

    def garden_snapshot_delete(self, snapshot_id: str) -> None:
        self._garden_snapshot_id(snapshot_id)
        argv = self._garden_one_shot(writable=True)
        self._command(
            [*argv, "-c", _GARDEN_SNAPSHOT_DELETE_CODE, snapshot_id], timeout=1800
        )

    @property
    def attach_copy_path(self) -> Path:
        return self.ctx.root / "garden" / "attach" / "compose.pre-add.yaml"

    def garden_attach(self) -> None:
        """Make the owned Cairn container able to host Garden.

        Journal-first: the pre-add Compose copy and its digest are recorded before
        the Compose file changes. Each phase is durable before the next begins, so
        a rerun after any interruption converges without a second copy or build.
        """
        if not self.garden_enabled:
            raise InstallError("Garden attach needs recorded Garden state")
        self._check_garden_options()
        self.validate_ownership()
        garden_state = self.ctx.state["garden"]
        attach = garden_state.get("attach")
        if attach is None:
            if not self._docker["containers"].get("cairn"):
                raise InstallError("Garden attach needs the owned Cairn container")
            detach = self._docker.get("garden_detach")
            if detach is not None and detach.get("phase") != "complete":
                raise InstallError(
                    "Garden detach is unfinished; resume it before attaching"
                )
            if (
                detach is not None
                and self._docker.get("garden_image", {}).get("status") == "removed"
            ):
                # A new attachment may use the legacy image again. Restore only
                # its retained receipt, before reactivating Garden validation.
                retained = self._docker.get("garden_builds", {}).get(self.garden_image)
                self._docker["garden_image"] = dict(
                    retained or {"reference": self.garden_image}
                )
            self._docker.pop("garden_detach", None)
            self._check_garden_listener()
            raw = read_owned(self.compose_path)
            copy = self.attach_copy_path
            self.ctx.write_file(copy, raw.decode("utf-8"), mode=0o600)
            attach = {
                "phase": "copied",
                "pre_add_compose": hashlib.sha256(raw).hexdigest(),
                "copy": str(copy),
                "cairn_running": self.is_running(),
            }
            garden_state["attach"] = attach
            self.ctx.save()
        if attach["phase"] == "copied":
            self.ctx.replace_file(
                self.compose_path,
                self._compose_text(garden_enabled=True),
                mode=0o644,
            )
            attach["phase"] = "compose_replaced"
            self.ctx.save()
        if attach["phase"] == "compose_replaced":
            # Garden services stay stopped: no profile is passed and garden_start owns them.
            self._build_image(garden=True)
            self._command([*self._all_compose, "config", "--quiet"])
            attach["phase"] = "built"
            self.ctx.save()
        if attach["phase"] == "built":
            self._recreate_cairn(running=bool(attach["cairn_running"]))
            attach["phase"] = "recreated"
            self.ctx.save()
        self.validate_ownership()

    def garden_detach(self, *, delete_data: bool) -> None:
        """Return the owned Cairn to its non-Garden layout; journaled and idempotent.

        Garden must already be stopped. Garden-data is kept unless delete_data.
        """
        self.validate_ownership()
        detach = self._docker.get("garden_detach")
        if detach is None:
            if not self._docker["containers"].get("cairn"):
                raise InstallError("Garden detach needs the owned Cairn container")
            detach = {
                "phase": "started",
                "delete_data": delete_data,
                "cairn_running": self.is_running(),
            }
            self._docker["garden_detach"] = detach
            self.ctx.save()
        if detach["phase"] == "complete":
            return
        if bool(detach["delete_data"]) != delete_data:
            raise InstallError("Resume the Garden detach with the same data choice")
        if detach["phase"] == "started":
            self._remove_garden_containers()
            detach["phase"] = "containers_removed"
            self.ctx.save()
        if detach["phase"] == "containers_removed":
            self._restore_pre_add_compose()
            self._recreate_cairn(running=bool(detach["cairn_running"]))
            detach["phase"] = "cairn_recreated"
            self.ctx.save()
        if detach["phase"] == "cairn_recreated":
            self._remove_garden_image()
            if detach["delete_data"]:
                receipts = list(self._docker.get("garden_builds", {}).values())
                receipts.extend(self._docker.get("garden_images", {}).values())
                for receipt in receipts:
                    image_id = self._check_garden_image(receipt)
                    if image_id is not None:
                        self._remove_unused_garden_image(image_id)
                self._docker.pop("garden_images", None)
                self._docker.pop("garden_builds", None)
                self.ctx.save()
            detach["phase"] = "image_removed"
            self.ctx.save()
        if detach["phase"] == "image_removed":
            self._remove_garden_volume("garden-tls")
            if detach["delete_data"]:
                self._remove_garden_volume("garden-data")
            detach["phase"] = "volumes_removed"
            self.ctx.save()
        if detach["phase"] == "volumes_removed":
            if detach["delete_data"]:
                self._delete_garden_copies()
            for path in self._garden_files():
                self._remove_owned_file(path)
            self._remove_empty_garden_directories()
            detach["phase"] = "files_removed"
            self.ctx.save()
        if detach["phase"] == "files_removed":
            volumes = dict(self._docker.get("volumes", {}))
            kept = volumes.pop("garden-data", None)
            volumes.pop("garden-tls", None)
            self._docker["volumes"] = volumes
            if kept is not None and not detach["delete_data"]:
                # Kept data remains owned outside state["garden"], so blitz still
                # removes it. A later garden-attach moves the receipt back.
                self._docker["retained_garden_data"] = {"volume": kept}
            self.ctx.state.get("garden", {}).pop("attach", None)
            detach["phase"] = "complete"
            self.ctx.save()

    def _recreate_cairn(self, *, running: bool) -> None:
        # Garden joins this container's network namespace, so Cairn must be
        # recreated. Without --profile, Compose leaves Garden services stopped.
        self._command(
            [
                *self._compose,
                "up",
                *(["--detach"] if running else ["--no-start"]),
                "--no-deps",
                "--no-build",
                "--force-recreate",
                "cairn",
            ],
            timeout=300,
        )
        self.validate_ownership()

    def _restore_pre_add_compose(self) -> None:
        attach = self.ctx.state.get("garden", {}).get("attach")
        if attach is None:
            text = self._compose_text(garden_enabled=False)
        else:
            copy = Path(str(attach["copy"]))
            if copy != self.attach_copy_path:
                raise InstallError(
                    f"Recorded pre-add Compose copy is outside the instance: {copy}"
                )
            self.ctx.check_file(copy)
            raw = read_owned(copy)
            if hashlib.sha256(raw).hexdigest() != attach["pre_add_compose"]:
                raise InstallError(
                    f"Pre-add Compose copy changed; preserve and inspect: {copy}"
                )
            text = raw.decode("utf-8")
        self.ctx.replace_file(self.compose_path, text, mode=0o644)

    def _remove_garden_containers(self) -> None:
        for identifier in self._project_container_ids():
            labels = self._inspect_labels("container", identifier)
            self._require_owned(labels, "Docker container", identifier)
            if labels.get("com.docker.compose.service") in _GARDEN_SERVICES:
                self._command(
                    ["docker", "container", "rm", "--force", identifier], timeout=90
                )
        self.validate_ownership()

    def _remove_garden_image(self) -> None:
        intent = self._docker.get("garden_image_removal")
        if intent is None:
            image_id = self._blitz_image_id(garden=True)
            current = self._docker.get("garden_images", {}).get("current", {})
            receipt = dict(current or self._docker.get("garden_image", {}))
            receipt.setdefault("reference", self.garden_image)
            intent = {"receipt": receipt}
            self._docker["garden_image_removal"] = intent
            self.ctx.save()
        else:
            receipt = self._garden_image_removal_receipt(intent)
            image_id = self._blitz_image_id(garden=True)
        if image_id is not None:
            self._remove_unused_garden_image(image_id)
        self._finish_garden_image_removal(receipt)

    def _reconcile_garden_image_removal(self) -> None:
        intent = self._docker.get("garden_image_removal")
        if intent is None:
            return
        receipt = self._garden_image_removal_receipt(intent)
        if self._blitz_image_id(garden=True) is None:
            self._finish_garden_image_removal(receipt)

    def _garden_image_removal_receipt(self, intent: dict[str, Any]) -> dict[str, str]:
        current = dict(
            self._docker.get("garden_images", {}).get(
                "current", self._docker.get("garden_image", {})
            )
        )
        current.setdefault("reference", self.garden_image)
        receipt = intent.get("receipt")
        if (
            self._docker.get("garden_detach", {}).get("phase") != "cairn_recreated"
            or not isinstance(receipt, dict)
            or receipt != current
        ):
            raise InstallError("Recorded Garden image removal intent is malformed")
        return dict(receipt)

    def _finish_garden_image_removal(self, receipt: dict[str, str]) -> None:
        removed: dict[str, str] = {
            "reference": receipt["reference"],
            "status": "removed",
        }
        if receipt.get("id"):
            removed["id"] = receipt["id"]
        self._docker["garden_image"] = removed
        self._docker.get("garden_images", {}).pop("current", None)
        self._docker.pop("garden_image_removal", None)
        self.ctx.save()

    def _remove_unused_garden_image(self, image_id: str) -> None:
        if self._ids(
            "container", ["--filter", f"ancestor={image_id}"], extra=["--all"]
        ):
            raise InstallError(
                "Owned Garden image is used by another container; refusing "
                f"forced removal: {image_id}"
            )
        self._command(["docker", "image", "rm", image_id])

    def _delete_garden_copies(self) -> None:
        from .garden_upgrade import _remove_copy

        root = self.ctx.root / "garden-upgrades"
        files = {
            Path(key)
            for table in (
                self.ctx.state["owned_files"],
                self.ctx.state.get("file_intents", {}),
                self._docker.get("garden_captures", {}),
            )
            for key in table
            if Path(key).is_relative_to(root)
        }
        for path in sorted(files):
            _remove_copy(self.ctx, path)
        if not _path_exists(root):
            return
        secure_directory(root, private=True)
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
        if any(root.iterdir()):
            raise InstallError("Garden cold-copy directory contains unowned files")
        root.rmdir()
        self.ctx.save()

    def _remove_garden_volume(self, short_name: str) -> None:
        name = f"{self.project}_{short_name}"
        for identifier in self._ids("volume", ["--filter", f"name=^{name}$"]):
            labels = self._inspect_labels("volume", identifier)
            self._require_owned(labels, "Docker volume", name)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(f"Docker volume has wrong project label: {name}")
            # Journal the intent first: a removal that lands before its receipt
            # save is reconciled by validate_ownership on resume.
            self._docker["volume_removal"] = {"volume": identifier}
            self.ctx.save()
            # No --force, as blitz: a still-attached volume is shared state.
            self._command(["docker", "volume", "rm", identifier])
        self._docker["volumes"] = {
            short: volume
            for short, volume in self._docker.get("volumes", {}).items()
            if short != short_name
        }
        self._docker.pop("volume_removal", None)
        self.ctx.save()

    def _garden_files(self) -> list[Path]:
        """Every owned file under garden/ and credentials/garden/: host.json, TLS
        copies, the pre-add Compose copy, profile bundles and credential captures,
        as native detach does."""
        garden_root = self.ctx.root / "garden"
        # Credential captures must go too: a later garden-add would otherwise
        # resume the revoked capture instead of issuing a new credential.
        credentials = self.ctx.root / "credentials" / "garden"
        owned = set(self.ctx.state["owned_files"]) | set(
            self.ctx.state.get("file_intents", {})
        )
        selected = {
            Path(key)
            for key in owned
            if Path(key).is_relative_to(garden_root)
            or Path(key).is_relative_to(credentials)
        }
        selected |= {
            garden_root / "host.json",
            garden_root / "tls" / "server.crt",
            garden_root / "tls" / "server.key",
            garden_root / "tls" / "ca.crt",
        }
        attach = self.ctx.state.get("garden", {}).get("attach")
        if attach is not None:
            selected.add(Path(str(attach["copy"])))
        return sorted(selected)

    def _remove_empty_garden_directories(self) -> None:
        root = self.ctx.root / "garden"
        if not root.is_dir():
            return
        # Deepest first, so a directory emptied by its children goes too.
        for directory in sorted(root.rglob("*"), reverse=True):
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and not any(directory.iterdir())
            ):
                directory.rmdir()

    def _remove_owned_file(self, path: Path) -> None:
        key = str(path.absolute())
        if _path_exists(path):
            if path.is_relative_to(self.ctx.root / "garden-upgrades"):
                self._check_garden_file(path)
            else:
                self.ctx.check_file(path)
            # Journal the deletion before unlinking; reconciled on resume if the
            # unlink lands before the bookkeeping is saved.
            deletions = self._docker.setdefault("file_deletions", [])
            if key not in deletions:
                deletions.append(key)
            self.ctx.save()
            path.unlink()
        for table in ("owned_files", "file_intents"):
            self.ctx.state.get(table, {}).pop(key, None)
        deletions = self._docker.get("file_deletions", [])
        if key in deletions:
            deletions.remove(key)
            if not deletions:
                del self._docker["file_deletions"]
        self.ctx.save()

    def is_running(self) -> bool:
        self.validate_ownership()
        services = set(
            self._command([*self._compose, "ps", "--status", "running", "--services"])
            .strip()
            .splitlines()
        )
        # Cairn can retain its catalogue lease while the index is unavailable.
        return "cairn" in services

    def start(self) -> None:
        self.validate_ownership()
        self._docker["service_intent"] = "start"
        self.ctx.save()
        self._command([*self._compose, "up", "--detach", "--wait"], timeout=600)
        self.validate_ownership()
        if not self.is_running():
            raise InstallError("Owned Docker services did not reach running state")
        self._docker["service_state"] = "running"
        self.ctx.save()

    def stop(self) -> None:
        self.validate_ownership()
        if not self._project_container_ids():
            return
        self._docker["service_intent"] = "stop"
        self.ctx.save()
        self._command([*self._all_compose, "stop", "--timeout", "60"], timeout=90)
        self.validate_ownership()
        self._docker["service_state"] = "stopped"
        self.ctx.save()

    def restart(self) -> None:
        self.stop()
        self.start()

    def rollback(self) -> None:
        self._docker["rollback_intent"] = "remove_containers_and_network"
        self.ctx.save()
        self._cleanup_recorded_bridge_preflight()
        self.validate_ownership()
        if self._project_container_ids() or self._ids(
            "network", ["--filter", f"name=^{self.project}_default$"]
        ):
            self._command([*self._all_compose, "down"], timeout=120)
        if self._project_container_ids() or self._ids(
            "network", ["--filter", f"name=^{self.project}_default$"]
        ):
            raise InstallError(
                "Owned Docker container or network remains after rollback"
            )
        self._docker["containers"] = {}
        self._docker["network"] = {}
        self._docker["rollback"] = "containers_removed"
        self.ctx.note(
            "Rollback retained Docker volumes, configuration, data and credentials under "
            f"{self.ctx.root}."
        )
        self.ctx.save()

    def blitz(self) -> None:
        """Remove every still-provable Docker resource owned by this instance."""
        if self._docker.get("blitz") == "complete":
            return
        if not self._blitz_may_have_resources():
            self._docker["blitz_intent"] = "remove_all_owned_resources"
            self._docker["blitz"] = "complete"
            self.ctx.save()
            return
        self._docker["blitz_intent"] = "remove_all_owned_resources"
        self.ctx.save()
        self._cleanup_recorded_bridge_preflight()

        containers, networks, volumes, image_id = self._blitz_inventory()
        images = [("image", self.image, image_id)]
        if self.garden_enabled:
            images.append(
                ("garden_image", self.garden_image, self._blitz_image_id(garden=True))
            )
            containers.sort(
                key=lambda identifier: (
                    self._inspect_labels("container", identifier).get(
                        "com.docker.compose.service"
                    )
                    == "cairn"
                )
            )
        retained = list(self._docker.get("garden_builds", {}).values())
        retained.extend(self._docker.get("garden_images", {}).values())
        for receipt in retained:
            image_id = self._check_garden_image(receipt)
            if not any(reference == receipt["reference"] for _, reference, _ in images):
                images.append(("garden_builds", receipt["reference"], image_id))
        for container_id in containers:
            self._command(
                ["docker", "container", "rm", "--force", container_id],
                timeout=90,
            )
            self._docker["containers"] = {}
            self.ctx.save()
        for network_id in networks:
            self._command(["docker", "network", "rm", network_id])
            self._docker["network"] = {}
            self.ctx.save()
        for volume_name in volumes:
            # Deliberately omit --force: a still-attached volume is shared state,
            # or evidence that a container escaped the proved-owned inventory.
            self._command(["docker", "volume", "rm", volume_name])
            self._docker["volumes"] = {
                key: value
                for key, value in self._docker.get("volumes", {}).items()
                if value != volume_name
            }
            retained = self._docker.get("retained_garden_data")
            if retained is not None and retained.get("volume") == volume_name:
                del self._docker["retained_garden_data"]
            self.ctx.save()

        for key, reference, image_id in images:
            if image_id is not None:
                users = self._ids(
                    "container",
                    ["--filter", f"ancestor={image_id}"],
                    extra=["--all"],
                )
                if users:
                    raise InstallError(
                        "Owned Docker image is used by another container; refusing "
                        f"forced removal: {image_id}"
                    )
                # Removing by immutable ID proves this is the recorded build. Without
                # --force Docker also protects other tags and unexpected references.
                self._command(["docker", "image", "rm", image_id])
                receipt = {
                    "reference": reference,
                    "id": image_id,
                    "status": "removed",
                }
                if key == "garden_builds":
                    self._docker.setdefault(key, {})[reference] = receipt
                else:
                    self._docker[key] = receipt
                self.ctx.save()

        if self._project_container_ids() or self._ids(
            "network", ["--filter", f"name=^{self.project}_default$"]
        ):
            raise InstallError("Owned Docker container or network remains after blitz")
        remaining_volumes = [
            name
            for name in self._expected_volume_receipts().values()
            if self._ids("volume", ["--filter", f"name=^{name}$"])
        ]
        if remaining_volumes:
            raise InstallError(
                "Owned Docker volume remains after blitz: "
                + ", ".join(remaining_volumes)
            )
        for _, _, image_id in images:
            if (
                image_id is not None
                and self._optional_image_details(image_id) is not None
            ):
                raise InstallError(
                    f"Owned Docker image remains after blitz: {image_id}"
                )
        self._docker["blitz"] = "complete"
        self.ctx.save()

    def _blitz_may_have_resources(self) -> bool:
        if self._docker.get("garden_builds") or self._docker.get("garden_images"):
            return True
        if self.garden_enabled and set(self._docker.get("garden_image", {})) - {
            "reference"
        }:
            return True
        if self._docker.get("retained_garden_data") is not None:
            return True
        image = self._docker.get("image")
        if not isinstance(image, dict) or image.get("reference") != self.image:
            raise InstallError("Recorded Docker image ownership is malformed")
        if set(image) != {"reference"}:
            return True
        return any(
            self._docker.get(key)
            for key in (
                "bridge_preflight",
                "project_intent",
                "project_state",
                "prepared",
                "service_intent",
                "service_state",
                "rollback_intent",
                "containers",
                "network",
                "volumes",
            )
        )

    def _blitz_inventory(
        self,
    ) -> tuple[list[str], list[str], list[str], str | None]:
        """Prove delete-time ownership while accepting already-absent receipts."""
        containers = self._project_container_ids()
        for container_id in containers:
            labels = self._inspect_labels("container", container_id)
            self._require_owned(labels, "Docker container", container_id)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(
                    f"Docker container has wrong project label: {container_id}"
                )

        network_name = f"{self.project}_default"
        networks = self._ids("network", ["--filter", f"name=^{network_name}$"])
        for network_id in networks:
            labels = self._inspect_labels("network", network_id)
            self._require_owned(labels, "Docker network", network_name)
            if labels.get("com.docker.compose.project") != self.project:
                raise InstallError(
                    f"Docker network has wrong project label: {network_name}"
                )

        volume_receipts = self._expected_volume_receipts()
        volumes: list[str] = []
        for volume_name in volume_receipts.values():
            identifiers = self._ids("volume", ["--filter", f"name=^{volume_name}$"])
            for identifier in identifiers:
                labels = self._inspect_labels("volume", identifier)
                self._require_owned(labels, "Docker volume", volume_name)
                if labels.get("com.docker.compose.project") != self.project:
                    raise InstallError(
                        f"Docker volume has wrong project label: {volume_name}"
                    )
                volumes.append(identifier)

        return containers, networks, volumes, self._blitz_image_id()

    def _expected_volume_receipts(self) -> dict[str, str]:
        recorded = self._docker.get("volumes", {})
        if not isinstance(recorded, dict):
            raise InstallError("Recorded Docker volume ownership is malformed")
        expected = {
            short_name: f"{self.project}_{short_name}"
            for short_name in self._volume_names
        }
        for short_name, identifier in recorded.items():
            if short_name not in expected or identifier != expected[short_name]:
                raise InstallError("Recorded Docker volume ownership is malformed")
        # Fixed Compose names let blitz reconcile a create that completed before
        # validate_ownership durably published its receipt.
        retained = self._docker.get("retained_garden_data")
        if retained is not None:
            expected["retained-garden-data"] = self._retained_garden_volume(retained)
        return expected

    def _blitz_image_id(self, *, garden: bool = False) -> str | None:
        reference = self.garden_image if garden else self.image
        key = "garden_image" if garden else "image"
        receipt = self._docker.get(key, {"reference": reference})
        if garden and self._docker.get("garden_images", {}).get("current"):
            reference = self._docker["garden_images"]["current"]["reference"]
            receipt = self._docker["garden_images"]["current"]
        if not isinstance(receipt, dict) or receipt.get("reference") != reference:
            raise InstallError("Recorded Docker image ownership is malformed")
        recorded_id = receipt.get("id")
        if recorded_id is None:
            by_reference = self._optional_image_details(reference)
            if by_reference is None:
                return None
            if receipt.get("intent") != "build":
                raise InstallError(
                    "Docker image exists without a recorded build intent; refusing "
                    f"removal: {reference}"
                )
            labels, recorded_id = by_reference
            self._require_owned(labels, "Docker image", reference)
            # Reconcile the narrow crash window after docker build returned but
            # before the immutable image identity was durably published.
            self._docker[key] = {
                "reference": reference,
                "id": recorded_id,
            }
            self.ctx.save()
        if not isinstance(recorded_id, str) or not recorded_id.startswith("sha256:"):
            raise InstallError("Recorded Docker image ownership is malformed")

        by_reference = self._optional_image_details(reference)
        if by_reference is not None:
            labels, observed_id = by_reference
            self._require_owned(labels, "Docker image", reference)
            if observed_id != recorded_id:
                raise InstallError(
                    f"Recorded Docker image reference was replaced: {reference}"
                )
        by_id = self._optional_image_details(recorded_id)
        if by_id is None:
            if by_reference is not None:
                raise InstallError(
                    f"Recorded Docker image identity disappeared: {recorded_id}"
                )
            return None
        labels, observed_id = by_id
        self._require_owned(labels, "Docker image", recorded_id)
        if observed_id != recorded_id:
            raise InstallError(f"Recorded Docker image identity changed: {recorded_id}")
        return recorded_id

    def _cleanup_recorded_bridge_preflight(self) -> None:
        receipt = self._docker.get("bridge_preflight")
        if receipt is None:
            return
        if not isinstance(receipt, dict):
            raise InstallError("Recorded Docker bridge preflight receipt is malformed")
        token, network, server, client = self._bridge_names()
        expected = {
            "label": token,
            "network": network,
            "server": server,
            "client": client,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise InstallError(
                "Recorded Docker bridge preflight ownership is malformed"
            )
        self._remove_probe_container(client, token)
        self._remove_probe_container(server, token)
        self._remove_probe_network(network, token)
        receipt["cleanup"] = "rollback_verified"
        self.ctx.save()

    def _project_container_ids(self) -> list[str]:
        return self._ids(
            "container",
            ["--filter", f"label=com.docker.compose.project={self.project}"],
            extra=["--all", "--no-trunc"] if self.garden_enabled else ["--all"],
        )

    def _ids(
        self,
        kind: str,
        filters: list[str],
        *,
        extra: list[str] | None = None,
    ) -> list[str]:
        output = self._command(
            ["docker", kind, "ls", *(extra or []), "--quiet", *filters]
        )
        return [line for line in output.splitlines() if line]

    def _inspect_labels(self, kind: str, identifier: str) -> dict[str, str]:
        template = (
            "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}"
        )
        raw = self._command(
            ["docker", kind, "inspect", "--format", template, identifier]
        ).strip()
        try:
            labels = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise InstallError(
                f"Could not read Docker {kind} ownership: {identifier}"
            ) from error
        if not isinstance(labels, dict):
            raise InstallError(f"Docker {kind} has no ownership labels: {identifier}")
        return {str(key): str(value) for key, value in labels.items()}

    def _image_details(self, reference: str) -> tuple[dict[str, str], str]:
        raw = self._command(["docker", "image", "inspect", reference]).strip()
        try:
            result = json.loads(raw)
            item = result[0]
            labels = item["Config"]["Labels"] or {}
            image_id = item["Id"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise InstallError(
                f"Could not inspect built Docker image: {reference}"
            ) from error
        if not isinstance(labels, dict) or not isinstance(image_id, str):
            raise InstallError(f"Docker image metadata is malformed: {reference}")
        return ({str(key): str(value) for key, value in labels.items()}, image_id)

    def _optional_image_details(
        self, reference: str
    ) -> tuple[dict[str, str], str] | None:
        if reference.startswith("sha256:"):
            listed = self._ids("image", [], extra=["--all", "--no-trunc"])
            if reference not in listed:
                return None
        else:
            listed = self._ids(
                "image",
                ["--filter", f"reference={reference}"],
                extra=["--all", "--no-trunc"] if self.garden_enabled else ["--all"],
            )
            if not listed:
                return None
            if len(set(listed)) != 1:
                raise InstallError(f"Docker image reference is ambiguous: {reference}")
        # The strict listing above is the absence proof. Once present, inspect is
        # deliberately strict so daemon, permission and API errors cannot look
        # like successful deletion.
        return self._image_details(reference)

    def _require_owned(
        self, labels: dict[str, str], resource_kind: str, identifier: str
    ) -> None:
        if any(labels.get(key) != value for key, value in self._owner_labels.items()):
            raise InstallError(f"Refusing foreign {resource_kind}: {identifier}")

    def _bridge_preflight(self) -> None:
        token, network, server, client = self._bridge_names()
        receipt = {
            "network": network,
            "server": server,
            "client": client,
            "label": token,
            "intent": "create",
        }
        self._docker["bridge_preflight"] = receipt
        self.ctx.save()
        self._remove_probe_container(client, token)
        self._remove_probe_container(server, token)
        self._remove_probe_network(network, token)
        label_args = [
            "--label",
            f"{_INSTANCE_LABEL}={self.ctx.instance_id}",
            "--label",
            f"{_RUN_LABEL}={self.ctx.run_id}",
            "--label",
            f"{_PREFLIGHT_LABEL}={token}",
        ]
        try:
            self._command(
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    *label_args,
                    network,
                ]
            )
            self._require_probe_owned("network", network, token)
            shape = self._command(
                [
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    "{{.Driver}} {{.Internal}}",
                    network,
                ]
            ).strip()
            if shape != "bridge false":
                raise InstallError(
                    f"Docker preflight network has unexpected shape: {shape}"
                )
            self._command(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    server,
                    *label_args,
                    "--network",
                    network,
                    "--network-alias",
                    "compose-http",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--entrypoint",
                    "python",
                    self.image,
                    "-u",
                    "-c",
                    _SERVER_CODE,
                ]
            )
            self._require_probe_owned("container", server, token)
            ready = False
            # A cold image on a loaded daemon can take well over the few
            # seconds ten back-to-back probes allow; bound by wall clock.
            deadline = time.monotonic() + _BRIDGE_READY_SECONDS
            while True:
                output = self._command(
                    [
                        "docker",
                        "exec",
                        server,
                        "python",
                        "-c",
                        'import socket; socket.create_connection(("127.0.0.1", 8000), 2).close(); print("ready")',
                    ],
                    timeout=4,
                    allowed=(0, 1),
                )
                if output.strip() == "ready":
                    ready = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            if not ready:
                raise InstallError(
                    "Docker bridge preflight server did not become ready"
                )
            output = self._command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    client,
                    *label_args,
                    "--network",
                    network,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--entrypoint",
                    "python",
                    self.image,
                    "-u",
                    "-c",
                    _CLIENT_CODE,
                ],
                timeout=30,
            )
            if not {"DNS_OK", "TCP_OK", "HTTP_OK"}.issubset(output.splitlines()):
                raise InstallError(
                    "Docker bridge preflight did not prove DNS, TCP and HTTP"
                )
            receipt["status"] = "verified"
            self.ctx.save()
        finally:
            self._remove_probe_container(client, token)
            self._remove_probe_container(server, token)
            self._remove_probe_network(network, token)
        receipt["cleanup"] = "verified"
        self.ctx.save()

    def _bridge_names(self) -> tuple[str, str, str, str]:
        token = re.sub(r"[^a-z0-9]", "", self.ctx.run_id.lower())[:24]
        return (
            token,
            f"cairn-preflight-{token}",
            f"cairn-preflight-server-{token}",
            f"cairn-preflight-client-{token}",
        )

    def _require_probe_owned(self, kind: str, name: str, token: str) -> None:
        labels = self._inspect_labels(kind, name)
        self._require_owned(labels, f"Docker preflight {kind}", name)
        if labels.get(_PREFLIGHT_LABEL) != token:
            raise InstallError(f"Refusing foreign Docker preflight {kind}: {name}")

    def _remove_probe_container(self, name: str, token: str) -> None:
        ids = self._ids("container", ["--filter", f"name=^/{name}$"], extra=["--all"])
        for container_id in ids:
            self._require_probe_owned("container", container_id, token)
            self._command(["docker", "container", "rm", "--force", container_id])

    def _remove_probe_network(self, name: str, token: str) -> None:
        ids = self._ids("network", ["--filter", f"name=^{name}$"])
        for network_id in ids:
            self._require_probe_owned("network", network_id, token)
            self._command(["docker", "network", "rm", network_id])


DockerBackend = Backend


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"(?:v)?(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return ()
    return tuple(int(part) for part in match.groups(default="0"))


def _check_loopback_port(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError as error:
        raise InstallError(f"Loopback port {port} is unavailable: {error}") from error
