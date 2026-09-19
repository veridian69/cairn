#!/usr/bin/env python3
"""Stage a verified local OCI archive on explicitly mapped Kubernetes nodes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import IO, Any

from falkordb_release import ReleaseError, verify_archive_graph

IMAGE_RE = re.compile(
    r"([a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)@(sha256:[0-9a-f]{64})"
)
NODE_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
SSH_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
REMOTE_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")


class StageError(Exception):
    """The archive or target nodes could not be safely staged."""


def _run(
    args: list[str],
    *,
    timeout: int,
    stdin: IO[bytes] | None = None,
    capture_stdout: bool = True,
) -> bytes:
    try:
        result = subprocess.run(
            args,
            stdin=stdin,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageError(f"command unavailable or timed out: {args[0]}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise StageError(f"{args[0]} command failed: {detail or 'no diagnostic'}")
    return result.stdout if isinstance(result.stdout, bytes) else b""


def _ssh(
    alias: str,
    command: str,
    *,
    timeout: int,
    stdin: IO[bytes] | None = None,
    capture_stdout: bool = True,
) -> bytes:
    return _run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            alias,
            command,
        ],
        timeout=timeout,
        stdin=stdin,
        capture_stdout=capture_stdout,
    )


def _parse_mappings(values: list[str]) -> list[tuple[str, str]]:
    if not values:
        raise StageError("at least one explicit node mapping is required")
    mappings: list[tuple[str, str]] = []
    names: set[str] = set()
    aliases: set[str] = set()
    for value in values:
        if value.count("=") != 1:
            raise StageError("node mappings must be NODE=SSH_ALIAS")
        name, alias = value.split("=", 1)
        if not NODE_RE.fullmatch(name) or not SSH_ALIAS_RE.fullmatch(alias):
            raise StageError("node mapping contains an unsafe name or SSH alias")
        if name in names or alias in aliases:
            raise StageError("node names and SSH aliases must be unique")
        names.add(name)
        aliases.add(alias)
        mappings.append((name, alias))
    return mappings


def _snapshot_archive(archive: Path, digest: str) -> tuple[IO[bytes], str, str]:
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise StageError("archive must be a readable regular file") from exc
    source = os.fdopen(fd, "rb")
    checked = tempfile.TemporaryFile()
    try:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise StageError("archive must be a regular file")
        checksum = hashlib.sha256()
        while block := source.read(1024 * 1024):
            checksum.update(block)
            checked.write(block)
        checked.seek(0)
        try:
            verify_archive_graph(checked, digest)
            source_ref = _archive_source_reference(checked, digest)
        except (ReleaseError, KeyError, ValueError, tarfile.TarError) as exc:
            raise StageError("archive is not a complete OCI image for --image") from exc
        checked.seek(0)
        return checked, checksum.hexdigest(), source_ref
    except BaseException:
        checked.close()
        raise
    finally:
        source.close()


def _archive_source_reference(stream: IO[bytes], digest: str) -> str:
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        matches = [
            member for member in archive.getmembers() if member.name == "index.json"
        ]
        if len(matches) != 1 or not matches[0].isfile():
            raise StageError("archive must contain one regular index.json")
        payload = archive.extractfile(matches[0])
        assert payload is not None
        with payload:
            index = json.load(payload)
    descriptors = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(descriptors, list):
        raise StageError("archive index has no manifest descriptors")
    references = []
    for descriptor in descriptors:
        if isinstance(descriptor, dict) and descriptor.get("digest") == digest:
            annotations = descriptor.get("annotations")
            if isinstance(annotations, dict):
                reference = annotations.get(
                    "io.containerd.image.name"
                ) or annotations.get("org.opencontainers.image.ref.name")
                if (
                    isinstance(reference, str)
                    and reference
                    and not reference.startswith("-")
                    and not any(ord(c) < 32 or ord(c) == 127 for c in reference)
                ):
                    references.append(reference)
    if len(references) != 1:
        raise StageError("archive needs one source reference for the requested digest")
    return references[0]


def _eligible_nodes(
    context: str, mappings: list[tuple[str, str]]
) -> list[dict[str, str]]:
    if not context or any(ord(c) < 32 for c in context):
        raise StageError("context must be a non-empty control-free value")
    raw = _run(
        ["kubectl", "--context", context, "get", "nodes", "-o", "json"],
        timeout=30,
    )
    try:
        document = json.loads(raw)
        items = document["items"]
    except (KeyError, TypeError, ValueError) as exc:
        raise StageError("kubectl returned malformed node JSON") from exc
    if not isinstance(items, list):
        raise StageError("kubectl returned malformed node JSON")
    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise StageError("kubectl returned malformed node JSON")
        metadata = item.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or name in by_name:
            raise StageError("kubectl returned missing or duplicate node names")
        by_name[name] = item

    selected: list[dict[str, str]] = []
    uids: set[str] = set()
    for name, _alias in mappings:
        item = by_name.get(name)
        if item is None or not _node_is_eligible(item):
            raise StageError(f"node {name} is not eligible for local image staging")
        uid = item["metadata"].get("uid")
        if (
            not isinstance(uid, str)
            or not uid
            or any(ord(c) < 32 or ord(c) == 127 for c in uid)
            or uid in uids
        ):
            raise StageError(f"node {name} has an invalid or duplicate UID")
        uids.add(uid)
        selected.append({"name": name, "uid": uid})
    return selected


def _node_is_eligible(item: dict[str, Any]) -> bool:
    spec = item.get("spec")
    status = item.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        return False
    taints = spec.get("taints", [])
    if spec.get("unschedulable") is True or not isinstance(taints, list):
        return False
    if any(
        isinstance(taint, dict) and taint.get("effect") in {"NoSchedule", "NoExecute"}
        for taint in taints
    ):
        return False
    conditions = status.get("conditions")
    ready = [
        condition
        for condition in conditions or []
        if isinstance(condition, dict) and condition.get("type") == "Ready"
    ]
    if len(ready) != 1 or ready[0].get("status") != "True":
        return False
    info = status.get("nodeInfo")
    return bool(
        isinstance(info, dict)
        and info.get("operatingSystem") == "linux"
        and info.get("architecture") == "amd64"
        and isinstance(info.get("containerRuntimeVersion"), str)
        and info["containerRuntimeVersion"].startswith("containerd://")
        and info["containerRuntimeVersion"] != "containerd://"
    )


def _remote_command(parts: list[str]) -> str:
    return shlex.join(parts)


def _safe_remote_path(value: str, description: str) -> str:
    parts = value.split("/")
    if (
        not REMOTE_PATH_RE.fullmatch(value)
        or parts[0]
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise StageError(f"{description} must be a normalised absolute path")
    return value


def _preflight_node(alias: str, ctr_path: str, crictl_path: str, socket: str) -> None:
    command = " && ".join(
        _remote_command(parts)
        for parts in (
            ["/usr/bin/test", "-x", "/usr/bin/sudo"],
            ["/usr/bin/test", "-x", ctr_path],
            ["/usr/bin/test", "-x", crictl_path],
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/test",
                "-S",
                socket,
            ],
        )
    )
    _ssh(alias, command, timeout=30)


def _inspect_identity(
    alias: str, image: str, crictl_path: str, socket: str, *, optional: bool
) -> bool:
    command = _remote_command(
        [
            "/usr/bin/sudo",
            "-n",
            crictl_path,
            "--runtime-endpoint",
            "unix://" + socket,
            "inspecti",
            "--output",
            "json",
            image,
        ]
    )
    if optional:
        command += " 2>/dev/null || /usr/bin/printf '%s\\n' '{\"status\":{\"repoDigests\":[]}}'"
    identity = _ssh(alias, command, timeout=60)
    try:
        repo_digests = json.loads(identity)["status"]["repoDigests"]
    except (KeyError, TypeError, ValueError) as exc:
        raise StageError("crictl returned malformed image identity") from exc
    if not isinstance(repo_digests, list):
        raise StageError("crictl returned malformed image identity")
    return image in repo_digests


def _stage_node(
    alias: str,
    archive: IO[bytes],
    checksum: str,
    image: str,
    source_ref: str,
    ctr_path: str,
    crictl_path: str,
    socket: str,
) -> None:
    existing = _inspect_identity(alias, image, crictl_path, socket, optional=True)
    directory_output = _ssh(
        alias,
        _remote_command(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/mktemp",
                "-d",
                "--",
                "/var/tmp/cairn-image-stage.XXXXXXXX",
            ]
        ),
        timeout=30,
    )
    directory = directory_output.decode(errors="replace").strip()
    if not re.fullmatch(r"/var/tmp/cairn-image-stage\.[A-Za-z0-9]{8}", directory):
        raise StageError("remote mktemp returned an unsafe path")
    remote = directory + "/archive.tar"
    try:
        archive.seek(0)
        _ssh(
            alias,
            _remote_command(["/usr/bin/sudo", "-n", "/usr/bin/tee", "--", remote]),
            timeout=1800,
            stdin=archive,
            capture_stdout=False,
        )
        checksum_output = _ssh(
            alias,
            _remote_command(
                ["/usr/bin/sudo", "-n", "/usr/bin/sha256sum", "--", remote]
            ),
            timeout=60,
        )
        fields = checksum_output.decode(errors="replace").split()
        if not fields or fields[0] != checksum:
            raise StageError("remote archive checksum does not match")
        _ssh(
            alias,
            _remote_command(
                [
                    "/usr/bin/sudo",
                    "-n",
                    ctr_path,
                    "--address",
                    socket,
                    "--namespace",
                    "k8s.io",
                    "images",
                    "import",
                    "--all-platforms",
                    remote,
                ]
            ),
            timeout=1800,
        )
        if source_ref != image and not existing:
            _ssh(
                alias,
                _remote_command(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        ctr_path,
                        "--address",
                        socket,
                        "--namespace",
                        "k8s.io",
                        "images",
                        "tag",
                        source_ref,
                        image,
                    ]
                ),
                timeout=120,
            )
        if not _inspect_identity(alias, image, crictl_path, socket, optional=False):
            raise StageError("crictl did not report the exact requested repoDigest")
    except BaseException as exc:
        try:
            _ssh(
                alias,
                _remote_command(
                    ["/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", directory]
                ),
                timeout=60,
            )
        except StageError as cleanup_error:
            exc.add_note(
                f"remote temporary-directory cleanup also failed: {cleanup_error}"
            )
        raise
    else:
        _ssh(
            alias,
            _remote_command(
                ["/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", directory]
            ),
            timeout=60,
        )


def _publish_receipt(output: Path, receipt: dict[str, Any]) -> None:
    if output.exists() or output.is_symlink():
        raise StageError("output receipt already exists")
    if not output.parent.is_dir():
        raise StageError("output receipt parent must already exist")
    payload = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        try:
            os.link(temporary_name, output)
        finally:
            os.unlink(temporary_name)
    except FileExistsError as exc:
        raise StageError("output receipt already exists") from exc
    directory = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def stage(
    context: str,
    archive: Path,
    image: str,
    mappings: list[str],
    output: Path,
    ctr_path: str = "/usr/bin/ctr",
    crictl_path: str = "/usr/bin/crictl",
    containerd_socket: str = "/run/containerd/containerd.sock",
) -> dict[str, Any]:
    match = IMAGE_RE.fullmatch(image)
    if match is None:
        raise StageError("image must be a canonical repository@sha256 reference")
    parsed_mappings = _parse_mappings(mappings)
    ctr_path = _safe_remote_path(ctr_path, "ctr path")
    crictl_path = _safe_remote_path(crictl_path, "crictl path")
    containerd_socket = _safe_remote_path(containerd_socket, "containerd socket")
    if output.exists() or output.is_symlink():
        raise StageError("output receipt already exists")
    checked, checksum, source_ref = _snapshot_archive(archive, match.group(2))
    try:
        nodes = _eligible_nodes(context, parsed_mappings)
        for _name, alias in parsed_mappings:
            _preflight_node(alias, ctr_path, crictl_path, containerd_socket)
        for (_name, alias), _record in zip(parsed_mappings, nodes, strict=True):
            _stage_node(
                alias,
                checked,
                checksum,
                image,
                source_ref,
                ctr_path,
                crictl_path,
                containerd_socket,
            )
        refreshed = _eligible_nodes(context, parsed_mappings)
        if refreshed != nodes:
            raise StageError("node identity or eligibility changed during staging")
    finally:
        checked.close()
    receipt = {
        "schema_version": 1,
        "image": image,
        "archive_sha256": checksum,
        "nodes": nodes,
    }
    _publish_receipt(output, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--node", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ctr-path", default="/usr/bin/ctr")
    parser.add_argument("--crictl-path", default="/usr/bin/crictl")
    parser.add_argument(
        "--containerd-socket", default="/run/containerd/containerd.sock"
    )
    args = parser.parse_args()
    try:
        receipt = stage(
            args.context,
            args.archive,
            args.image,
            args.node,
            args.output,
            args.ctr_path,
            args.crictl_path,
            args.containerd_socket,
        )
    except (StageError, OSError, ValueError) as exc:
        print(f"kubernetes-image-stage: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "staged", **receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
