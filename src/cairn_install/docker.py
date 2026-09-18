"""Owned Docker Compose backend for the guided installer."""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import socket
import stat
import sys
from pathlib import Path

from . import garden
from .core import Context, InstallError
from .docker_assets import FALKORDB_IMAGE, render_compose, render_config

_INSTANCE_LABEL = "io.cairn.install.instance"
_RUN_LABEL = "io.cairn.install.run"
_PREFLIGHT_LABEL = "io.cairn.install.preflight"
_CONFIG_PATH = "/etc/cairn/config.yaml"

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
        self.garden_image = f"{self.project}-garden:{suffix or 'source'}"
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
    def garden_enabled(self) -> bool:
        return bool(self.ctx.state.get("garden"))

    def _check_garden_options(self) -> None:
        if self.garden_enabled:
            if self.ctx.state["garden"]["options"].get("image"):
                raise InstallError(
                    "Docker Garden requires an owned source build; --garden-image is not supported"
                )
            if not (self.ctx.source / "a2a/Dockerfile").is_file():
                raise InstallError("Source checkout has no Garden a2a/Dockerfile")

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
                from .garden import require_listener_available

                require_listener_available(
                    int(self.ctx.state["garden"]["options"]["port"]), wildcard=True
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
        password_file_for_compose: Path | None = None
        if self.ctx.semantic:
            provider = Path(str(self.ctx.state.get("provider_key_file", "")))
            if not provider.is_absolute():
                raise InstallError("Semantic mode needs a protected provider key file")
            if str(provider.absolute()) not in self.ctx.state.get("owned_files", {}):
                raise InstallError(f"Refusing unowned provider credential: {provider}")
            self.ctx.check_file(provider)
            self.ctx.read_secret(provider)
            password_file_for_compose = password_file
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
            render_compose(
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
                garden_image=self.garden_image if self.garden_enabled else None,
                garden_port=int(
                    self.ctx.state.get("garden", {})
                    .get("options", {})
                    .get("port", 8443)
                ),
            ),
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

    def _build_image(self, *, garden: bool = False) -> None:
        reference = self.garden_image if garden else self.image
        key = "garden_image" if garden else "image"
        source = self.ctx.source / "a2a" if garden else self.ctx.source
        image_ids = self._ids(
            "image",
            ["--filter", f"reference={reference}"],
            extra=["--no-trunc"],
        )
        if image_ids:
            labels, image_id = self._image_details(reference)
            self._require_owned(labels, "Docker image", reference)
            self._docker[key] = {"reference": reference, "id": image_id}
            self.ctx.save()
            return
        self._docker[key] = {"reference": reference, "intent": "build"}
        self.ctx.save()
        label_arguments: list[str] = []
        for label, value in self._owner_labels.items():
            label_arguments.extend(["--label", f"{label}={value}"])
        self._command(
            [
                "docker",
                "build",
                "--pull=false",
                *label_arguments,
                "--build-arg",
                f"REVISION={self.ctx.state.get('source_fingerprint', 'unknown')}",
                "--tag",
                reference,
                str(source),
            ],
            timeout=1800,
        )
        observed, image_id = self._image_details(reference)
        self._require_owned(observed, "Docker image", reference)
        self._docker[key] = {"reference": reference, "id": image_id}
        self.ctx.save()

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

    def validate_ownership(self) -> None:
        if self.ctx.semantic:
            self._falkordb_image()
        paths = [self.config_path, self.env_path, self.compose_path]
        if self.garden_enabled:
            paths.extend(
                self.ctx.root / "garden" / name
                for name in ("host.json", "tls/server.crt", "tls/server.key")
            )
        for path in paths:
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
        if self.garden_enabled:
            images.append(("garden_image", self.garden_image))
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
        self._docker["containers"] = containers
        self._docker["network"] = network
        self._docker["volumes"] = volumes
        self.ctx.save()

    @property
    def _volume_names(self) -> tuple[str, ...]:
        names: tuple[str, ...] = ("cairn-data",)
        if self.ctx.semantic:
            names += ("cairn-credentials", "falkordb-config", "falkordb-data")
        if self.garden_enabled:
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
            {"gateway": config, "stream": {"max_age": "0", "max_bytes": 0}},
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
                self._docker[key] = {
                    "reference": reference,
                    "id": image_id,
                    "status": "removed",
                }
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
        if self.garden_enabled and set(self._docker.get("garden_image", {})) - {
            "reference"
        }:
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
        return expected

    def _blitz_image_id(self, *, garden: bool = False) -> str | None:
        reference = self.garden_image if garden else self.image
        key = "garden_image" if garden else "image"
        receipt = self._docker.get(key, {"reference": reference})
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
            for _ in range(10):
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
