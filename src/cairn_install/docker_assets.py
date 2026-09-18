"""Interpolation-free Compose assets for a guided Docker installation."""

from __future__ import annotations

import json
from pathlib import Path

FALKORDB_IMAGE = (
    "cairn.local/falkordb-runtime@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

_SECRET_INIT = """\
import os
import stat
import tempfile
from pathlib import Path


def publish(target, expected, uid):
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() or target_path.is_symlink():
        info = target_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o400 or target_path.read_bytes() != expected:
            raise SystemExit("refusing to replace a different installed credential")
        return
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=target_path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), uid, 0)
            os.fchmod(stream.fileno(), 0o400)
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target_path, follow_symlinks=False)
        directory_fd = os.open(target_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def install(source, target, uid):
    value = Path(source).read_bytes()
    if not value.strip() or b"\\x00" in value or b"\\n" in value.rstrip(b"\\r\\n"):
        raise SystemExit("credential source must contain exactly one line")
    publish(target, value.rstrip(b"\\r\\n") + b"\\n", uid)


def main():
    install("/source/falkordb-password", "/target/cairn/falkordb-password", 65532)
    install("/source/openai-api-key", "/target/cairn/openai-api-key", 65532)
    password = Path("/source/falkordb-password").read_bytes().rstrip(b"\\r\\n")
    config = Path("/target/falkordb/cairn.conf")
    expected_config = b"requirepass " + password + b"\\n"
    publish(config, expected_config, 10001)
    os.chown("/target/falkordb-data", 10001, 0)


if __name__ == "__main__":
    main()
"""


def _quoted(value: str | Path) -> str:
    return json.dumps(str(value).replace("$", "$$"))


def _source(path: Path, root: Path) -> str:
    """Use project-relative binds so special characters in the root stay literal."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return _quoted(path)
    return _quoted("./" + relative.as_posix())


def _labels(instance_id: str, run_id: str, indent: str) -> str:
    return "\n".join(
        (
            f"{indent}io.cairn.install.instance: {_quoted(instance_id)}",
            f"{indent}io.cairn.install.run: {_quoted(run_id)}",
        )
    )


def render_config(instance_id: str, *, semantic: bool) -> str:
    graphiti = "  enabled: false"
    if semantic:
        graphiti = "  enabled: true\n  host: falkordb\n  port: 6379"
    return f"""\
schema_version: cairn.config/v1
instance_id: {instance_id}
mode: production
http:
  host: 0.0.0.0
  port: 8000
paths:
  data: /var/lib/cairn
  credentials: /var/run/secrets/cairn
attic:
  enabled: true
graphiti:
{graphiti}
delivery:
  interval_seconds: 5
"""


def render_compose(
    *,
    image: str,
    port: int,
    root: Path,
    instance_id: str,
    run_id: str,
    semantic: bool,
    provider_key_file: Path | None = None,
    falkordb_password_file: Path | None = None,
    falkordb_image: str = FALKORDB_IMAGE,
    falkordb_local: bool = False,
    garden_image: str | None = None,
    garden_port: int = 8443,
) -> str:
    credentials_mount = f"""\
      - type: bind
        source: {_source(root / "credentials", root)}
        target: /var/run/secrets/cairn
        read_only: true
        bind:
          create_host_path: false"""
    depends = ""
    semantic_services = ""
    semantic_volumes = ""
    if semantic:
        if provider_key_file is None or falkordb_password_file is None:
            raise ValueError("semantic Compose needs both protected source files")
        credentials_mount = """\
      - type: volume
        source: cairn-credentials
        target: /var/run/secrets/cairn
        read_only: true"""
        depends = """\
    depends_on:
      secret-init:
        condition: service_completed_successfully
      falkordb:
        condition: service_healthy
"""
        init_code = "\n".join(f"        {line}" for line in _SECRET_INIT.splitlines())
        semantic_services = f"""
  secret-init:
    image: {_quoted(image)}
    user: "0:0"
    read_only: true
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - DAC_OVERRIDE
      - FOWNER
    network_mode: none
    security_opt:
      - no-new-privileges:true
    entrypoint:
      - python
      - -c
    command:
      - |-
{init_code}
    volumes:
      - type: bind
        source: {_source(provider_key_file, root)}
        target: /source/openai-api-key
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: {_source(falkordb_password_file, root)}
        target: /source/falkordb-password
        read_only: true
        bind:
          create_host_path: false
      - type: volume
        source: cairn-credentials
        target: /target/cairn
      - type: volume
        source: falkordb-config
        target: /target/falkordb
      - type: volume
        source: falkordb-data
        target: /target/falkordb-data
    labels:
{_labels(instance_id, run_id, "      ")}
    restart: "no"

  falkordb:
    image: {_quoted(falkordb_image)}
{("    pull_policy: never" + chr(10)) if falkordb_local else ""}\
    depends_on:
      secret-init:
        condition: service_completed_successfully
    user: "10001:0"
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    environment:
      REDIS_ARGS: /etc/falkordb/cairn.conf
      BROWSER: "0"
      TLS: "0"
      FALKORDB_ARGS: "MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000"
    volumes:
      - type: volume
        source: falkordb-config
        target: /etc/falkordb
        read_only: true
      - type: volume
        source: falkordb-data
        target: /var/lib/falkordb/data
    healthcheck:
      test:
        - CMD-SHELL
        - >-
          {{ sed -n 's/^requirepass /AUTH /p' /etc/falkordb/cairn.conf; echo PING; }}
          | redis-cli -h 127.0.0.1 -p 6379 | grep -q '^PONG$$'
      start_period: 60s
      start_interval: 2s
      interval: 10s
      timeout: 5s
      retries: 3
    stop_grace_period: 30s
    restart: unless-stopped
    labels:
{_labels(instance_id, run_id, "      ")}
"""
        semantic_volumes = f"""
  cairn-credentials:
    labels:
{_labels(instance_id, run_id, "      ")}
  falkordb-config:
    labels:
{_labels(instance_id, run_id, "      ")}
  falkordb-data:
    labels:
{_labels(instance_id, run_id, "      ")}
"""

    garden_services = ""
    garden_volumes = ""
    garden_publish = ""
    if garden_image:
        garden_publish = f"\n      - {_quoted(f'0.0.0.0:{garden_port}:9443')}"
        # Reuse only the atomic publisher; TLS PEM deliberately permits newlines.
        code = (
            _SECRET_INIT.split("def install(")[0]
            + """
for name in ("server.crt", "server.key"):
    value = Path("/source/" + name).read_bytes()
    if not value or len(value) > 1048576:
        raise SystemExit("invalid TLS source")
    publish("/target/tls/" + name, value, 65532)
os.chown("/target/data", 65532, 65532)
os.chmod("/target/data", 0o700)
"""
        )
        init_code = "\n".join(f"        {line}" for line in code.splitlines())
        garden_services = f"""
  garden:
    profiles: [garden]
    image: {_quoted(garden_image)}
    user: "65532:65532"
    network_mode: service:cairn
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=16m
    volumes:
      - type: bind
        source: {_source(root / "garden/host.json", root)}
        target: /etc/garden/host.json
        read_only: true
        bind:
          create_host_path: false
      - type: volume
        source: garden-data
        target: /var/lib/garden
      - type: volume
        source: garden-tls
        target: /var/run/secrets/garden
        read_only: true
    stop_grace_period: 30s
    restart: unless-stopped
    labels:
{_labels(instance_id, run_id, "      ")}
  garden-secret-init:
    profiles: [garden]
    image: {_quoted(image)}
    user: "0:0"
    network_mode: none
    read_only: true
    cap_drop: [ALL]
    cap_add: [CHOWN, DAC_OVERRIDE, FOWNER]
    security_opt: [no-new-privileges:true]
    entrypoint: [python, -c]
    command:
      - |-
{init_code}
    volumes:
      - type: bind
        source: {_source(root / "garden/tls/server.crt", root)}
        target: /source/server.crt
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: {_source(root / "garden/tls/server.key", root)}
        target: /source/server.key
        read_only: true
        bind:
          create_host_path: false
      - type: volume
        source: garden-tls
        target: /target/tls
      - type: volume
        source: garden-data
        target: /target/data
    restart: "no"
    labels:
{_labels(instance_id, run_id, "      ")}
"""
        garden_volumes = f"""
  garden-data:
    labels:
{_labels(instance_id, run_id, "      ")}
  garden-tls:
    labels:
{_labels(instance_id, run_id, "      ")}
"""

    return f"""\
services:
  cairn:
    image: {_quoted(image)}
    ports:
      - {_quoted(f"127.0.0.1:{port}:8000")}{garden_publish}
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=256m
    volumes:
      - type: bind
        source: {_source(root / "config.yaml", root)}
        target: /etc/cairn/config.yaml
        read_only: true
        bind:
          create_host_path: false
{credentials_mount}
      - type: volume
        source: cairn-data
        target: /var/lib/cairn
{depends}    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=2)
      start_period: 300s
      start_interval: 5s
      interval: 10s
      timeout: 5s
      retries: 3
    stop_grace_period: 60s
    restart: unless-stopped
    labels:
{_labels(instance_id, run_id, "      ")}
{semantic_services}
{garden_services}
volumes:
  cairn-data:
    labels:
{_labels(instance_id, run_id, "      ")}
{semantic_volumes}
{garden_volumes}
networks:
  default:
    labels:
{_labels(instance_id, run_id, "      ")}
"""
