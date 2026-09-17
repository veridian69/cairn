#!/usr/bin/env python3
"""Prepare or import the maintained FalkorDB archive without registry access.

Offline import requires Docker's containerd image store; this is not a new
minimum for ordinary registry installation. The trusted release descriptor
authenticates the archive. Preparation prints its checksum for separate review
and never rewrites the descriptor. Repeated Docker saves need not be identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import IO, Any


class ReleaseError(Exception):
    """A release identity or offline import requirement was not satisfied."""


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"duplicate descriptor field: {key}")
        result[key] = value
    return result


def read_descriptor(path: Path) -> dict[str, Any]:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ReleaseError("descriptor must be a regular file")
    data = json.loads(path.read_text(), object_pairs_hook=_unique_fields)
    required = {"schema_version", "image", "local_image", "archive_sha256"}
    if not isinstance(data, dict) or set(data) != required:
        raise ReleaseError("descriptor must contain exactly the four release fields")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ReleaseError("unsupported descriptor schema_version")
    patterns = {
        "image": r"ghcr\.io/veridian69/cairn-falkordb:v[0-9]+\.[0-9]+\.[0-9]+-cairn\.[1-9][0-9]*@sha256:[0-9a-f]{64}",
        "local_image": r"cairn-local/falkordb-server:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
        "archive_sha256": r"[0-9a-f]{64}",
    }
    for field, pattern in patterns.items():
        value = data[field]
        if field == "archive_sha256" and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ReleaseError(f"invalid descriptor {field}")
    return data


def _docker(
    args: list[str],
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    absent_ok: bool = False,
) -> bytes | None:
    command = ["docker", *args]
    try:
        result = subprocess.run(
            command,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(
            f"Docker command unavailable or timed out: {args[0]}"
        ) from exc
    if result.returncode:
        if absent_ok and b"No such image:" in result.stderr:
            return None
        raise ReleaseError(
            f"Docker {' '.join(args[:2])} failed: "
            + result.stderr.decode(errors="replace").strip()
        )
    return result.stdout if isinstance(result.stdout, bytes) else None


def _containerd_daemon() -> None:
    data = _docker(["info", "--format", "{{json .}}"])
    assert data is not None
    info = json.loads(data)
    if (
        not isinstance(info, dict)
        or info.get("OSType") != "linux"
        or info.get("Architecture") not in {"amd64", "x86_64"}
        or ["driver-type", "io.containerd.snapshotter.v1"]
        not in (info.get("DriverStatus") or [])
    ):
        raise ReleaseError(
            "offline archives require a Linux amd64 Docker containerd image store; "
            "ordinary registry pulls retain their existing requirements"
        )


def _inspect(reference: str, *, absent_ok: bool = False) -> dict[str, Any] | None:
    data = _docker(
        ["image", "inspect", reference, "--format", "{{json .}}"],
        absent_ok=absent_ok,
    )
    if data is None:
        return None
    record = json.loads(data)
    if not isinstance(record, dict):
        raise ReleaseError("unexpected Docker image inspection response")
    return record


def _check_identity(
    record: dict[str, Any] | None, descriptor: dict[str, Any], *, repository: bool
) -> None:
    image = descriptor["image"]
    tag, digest = image.split("@")
    canonical = tag.rsplit(":", 1)[0] + "@" + digest
    if (
        not isinstance(record, dict)
        or record.get("Id") != digest
        or record.get("Os") != "linux"
        or record.get("Architecture") != "amd64"
        or (repository and canonical not in (record.get("RepoDigests") or []))
    ):
        raise ReleaseError("image identity does not match the released OCI index")


def _stream_sha256(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def verify_archive_graph(stream: IO[bytes], expected: str) -> None:
    """Verify that every blob reachable from the released index is preserved."""
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            members = archive.getmembers()
            names = [m.name for m in members]
            if len(names) != len(set(names)):
                raise ReleaseError("duplicate archive member")
            pending = [expected]
            visited = set()
            while pending:
                digest = pending.pop()
                if digest in visited:
                    continue
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    raise ReleaseError("unsupported digest in archive index")
                member = archive.getmember("blobs/sha256/" + digest.split(":")[1])
                if not member.isfile():
                    raise ReleaseError("archive index references a non-regular blob")
                payload = archive.extractfile(member)
                assert payload is not None
                with payload:
                    actual = _stream_sha256(payload)
                if "sha256:" + actual != digest:
                    raise ReleaseError("archive index references a corrupt blob")
                visited.add(digest)
                # JSON descriptors are small; layer blobs need hashing only.
                if member.size <= 4 * 1024 * 1024:
                    payload = archive.extractfile(member)
                    assert payload is not None
                    with payload:
                        try:
                            document = json.load(payload)
                        except (ValueError, UnicodeError):
                            continue
                    if (
                        isinstance(document, dict)
                        and document.get("schemaVersion") == 2
                    ):
                        children = document.get("manifests", []) + document.get(
                            "layers", []
                        )
                        if "config" in document:
                            children.append(document["config"])
                        pending.extend(child["digest"] for child in children)
    except (KeyError, tarfile.TarError) as exc:
        raise ReleaseError(
            "archive is missing the complete released OCI index"
        ) from exc
    finally:
        stream.seek(0)


def prepare(descriptor: dict[str, Any], archive: Path) -> dict[str, str]:
    if archive.exists() or archive.is_symlink():
        raise ReleaseError("archive already exists; preserve it and choose a new path")
    _containerd_daemon()
    _check_identity(_inspect(descriptor["local_image"]), descriptor, repository=False)
    tag = descriptor["image"].split("@")[0]
    existing = _inspect(tag, absent_ok=True)
    if existing is not None:
        _check_identity(existing, descriptor, repository=False)
    _docker(["image", "tag", descriptor["local_image"], tag])
    _check_identity(_inspect(descriptor["image"]), descriptor, repository=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=archive.parent) as output:
        _docker(["image", "save", tag], stdout=output.file)
        output.flush()
        output.seek(0)
        checksum = hashlib.file_digest(output, "sha256").hexdigest()
        if descriptor["archive_sha256"] not in (None, checksum):
            raise ReleaseError("prepared archive differs from the trusted checksum")
        verify_archive_graph(output.file, descriptor["image"].split("@")[1])
        os.fsync(output.fileno())
        # Atomic no-clobber publication, including a destination created mid-save.
        os.link(output.name, archive)
    return {"image": descriptor["image"], "archive_sha256": checksum}


def load(descriptor: dict[str, Any], archive: Path) -> dict[str, str]:
    expected = descriptor["archive_sha256"]
    if expected is None:
        raise ReleaseError("load requires a trusted non-null archive_sha256")
    # Use a private snapshot, so replacing/changing the input after verification
    # cannot feed different bytes to Docker. No Docker call occurs before hashing.
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise ReleaseError("archive must be a readable regular file") from exc
    with os.fdopen(fd, "rb") as source, tempfile.TemporaryFile() as checked:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ReleaseError("archive must be a regular file")
        digest = hashlib.sha256()
        while block := source.read(1024 * 1024):
            digest.update(block)
            checked.write(block)
        if digest.hexdigest() != expected:
            raise ReleaseError("archive checksum does not match the trusted descriptor")
        checked.seek(0)
        _containerd_daemon()
        _docker(["image", "load"], stdin=checked)
    _check_identity(_inspect(descriptor["image"]), descriptor, repository=True)
    return {"image": descriptor["image"], "archive_sha256": expected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "load"))
    parser.add_argument(
        "--descriptor", type=Path, default=Path("deploy/falkordb/release.json")
    )
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    try:
        descriptor = read_descriptor(args.descriptor)
        result = (prepare if args.command == "prepare" else load)(
            descriptor, args.archive
        )
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"falkordb-release: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "verified", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
