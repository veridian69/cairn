#!/usr/bin/env python3
"""Export a locally built FalkorDB runtime for operator-controlled node staging.

Run deploy/falkordb/build.sh first. This tool never pulls or pushes an image and
does not claim that mutable online package repositories reproduce an old build.
Docker must use its containerd image store so save preserves the OCI graph.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


class RuntimeError(Exception):
    """Local runtime identity or export requirement was not satisfied."""


def docker(args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, check=True, timeout=1800
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Docker operation failed: " + " ".join(args[:2])) from exc
    return result.stdout


def archive_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_archive(path: Path, digest: str, expected_tag: str | None = None) -> None:
    # Reuse the graph verifier, not the historical release descriptor or pins.
    spec = importlib.util.spec_from_file_location(
        "falkordb_release", Path(__file__).with_name("falkordb_release.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        with path.open("rb") as stream:
            module.verify_archive_graph(stream, digest)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                roots = [
                    entry
                    for entry in archive.getmembers()
                    if entry.name == "index.json"
                ]
                if len(roots) != 1 or not roots[0].isfile():
                    raise ValueError("missing unique index.json")
                root = archive.extractfile(roots[0])
                assert root is not None
                with root:
                    index = json.load(root)
                matches = [
                    entry
                    for entry in index.get("manifests", [])
                    if entry.get("digest") == digest
                ]
                if len(matches) != 1:
                    raise ValueError("missing importable source reference")
                reference = (
                    matches[0].get("annotations", {}).get("io.containerd.image.name")
                )
                if (
                    not isinstance(reference, str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", reference
                    )
                    or (expected_tag is not None and reference != expected_tag)
                ):
                    raise ValueError("archive source reference differs from saved tag")
    except (
        OSError,
        ValueError,
        KeyError,
        AttributeError,
        tarfile.TarError,
        module.ReleaseError,
    ) as exc:
        raise RuntimeError(
            "OCI archive does not preserve the local image identity"
        ) from exc


def export_runtime(reference: str, output: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", reference):
        raise RuntimeError("invalid local image reference")
    if output.exists() or output.is_symlink():
        raise RuntimeError("output already exists; choose a new build directory")
    inspection = json.loads(
        docker(["image", "inspect", reference, "--format", "{{json .}}"])
    )
    digest = inspection.get("Id", "")
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or inspection.get("Os") != "linux"
        or inspection.get("Architecture") != "amd64"
    ):
        raise RuntimeError("local image must have a Linux amd64 sha256 identity")
    image = "cairn.local/falkordb-runtime@" + digest
    tag = "cairn.local/falkordb-runtime:build-" + digest.split(":", 1)[1]
    existing = (
        docker(
            [
                "image",
                "ls",
                "--no-trunc",
                "--format",
                "{{.ID}}",
                "--filter",
                "reference=" + tag,
            ]
        )
        .decode()
        .splitlines()
    )
    if any(value != digest for value in existing):
        raise RuntimeError("local build tag already identifies a different image")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".falkordb-export-", dir=output.parent
    ) as temporary:
        staging = Path(temporary)
        archive = staging / "image.tar"
        docker(["image", "tag", digest, tag])
        docker(["image", "save", "--output", str(archive), tag])
        verify_archive(archive, digest, tag)
        record = {
            "schema_version": 1,
            "image": image,
            "local_tag": tag,
            "platform": "linux/amd64",
            "archive": "image.tar",
            "archive_sha256": archive_digest(archive),
            "published": False,
        }
        (staging / "runtime.json").write_text(json.dumps(record, indent=2) + "\n")
        (staging / "SHA256SUMS").write_text(record["archive_sha256"] + "  image.tar\n")
        # Exclusive mkdir cannot replace another operator's directory. Publish
        # the descriptor last, after every validated payload is present.
        output.mkdir(mode=0o700)
        published: list[Path] = []
        try:
            for name in ("image.tar", "SHA256SUMS", "runtime.json"):
                destination = output / name
                os.link(staging / name, destination)
                published.append(destination)
        except BaseException:
            for destination in reversed(published):
                destination.unlink()
            output.rmdir()
            raise
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="cairn-local/falkordb-server:rebuild")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = export_runtime(args.image, args.output.absolute())
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
