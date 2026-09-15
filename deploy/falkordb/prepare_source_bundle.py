#!/usr/bin/env python3
"""Prepare the corresponding-source bundle for Cairn's FalkorDB derivative."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

REDIS_VERSION = "8.6.3"
REDIS_URL = f"https://download.redis.io/releases/redis-{REDIS_VERSION}.tar.gz"
REDIS_SHA256 = "9f54d4458c52be5472cdd1347d737f1d488b520fc3d0911cba47302de8d836e2"
EXPECTED_DEBIAN_PACKAGES = 73
EXPECTED_PATCHED_EPOCH = 1_789_467_161  # 2026-09-15T10:12:41Z
DEBIAN_SNAPSHOT = "https://snapshot.debian.org"
INVENTORY_FORMAT = (
    "${binary:Package}\\t${source:Package}\\t${source:Version}\\t"
    "${Version}\\t${Homepage}\\n"
)


class BundleError(RuntimeError):
    """The requested bundle cannot be tied to its declared inputs."""


def _run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    text: bool = True,
    input_bytes: bytes | None = None,
) -> str | bytes:
    try:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=text,
            input=None if text else input_bytes,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        stderr = getattr(error, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        detail = str(stderr).strip()
        raise BundleError(f"command failed: {' '.join(arguments)}: {detail}") from error
    return cast(str | bytes, completed.stdout)


def _git(checkout: Path, *arguments: str) -> str:
    output = _run(["git", "-C", str(checkout), *arguments])
    assert isinstance(output, str)
    # ``git submodule status`` uses a significant leading status character.
    return output.rstrip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_member(member: tarfile.TarInfo, name: str, epoch: int) -> None:
    member.name = name
    member.uid = 0
    member.gid = 0
    member.uname = "root"
    member.gname = "root"
    member.mtime = epoch
    member.pax_headers = {}


def _open_reproducible_tar(
    path: Path,
) -> tuple[BinaryIO, gzip.GzipFile, tarfile.TarFile]:
    raw = path.open("wb")
    compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    archive = tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT)
    return raw, compressed, archive


def _add_bytes(
    archive: tarfile.TarFile,
    name: str,
    contents: bytes,
    *,
    epoch: int,
    mode: int = 0o644,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mode = mode
    _normalise_member(member, name, epoch)
    archive.addfile(member, io.BytesIO(contents))


def _repository_url(checkout: Path) -> str:
    try:
        raw = _git(checkout, "remote", "get-url", "origin")
    except BundleError:
        return "local-fixture"
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme in {"http", "https", "git", "ssh"} and parsed.hostname:
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    scp = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", raw)
    if scp is not None:
        return f"https://{scp.group(1)}/{scp.group(2)}"
    return "local-checkout"


def _repositories(checkout: Path, expected_commit: str) -> list[dict[str, str]]:
    if not checkout.is_dir():
        raise BundleError(f"FalkorDB checkout does not exist: {checkout}")
    actual_commit = _git(checkout, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise BundleError(
            f"FalkorDB commit mismatch: expected {expected_commit}, got {actual_commit}"
        )
    if dirty := _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise BundleError(f"FalkorDB checkout is not clean:\n{dirty}")

    repositories = [
        {
            "path": ".",
            "commit": actual_commit,
            "url": _repository_url(checkout),
        }
    ]
    status = _git(checkout, "submodule", "status", "--recursive")
    for line in status.splitlines():
        if not line.startswith(" "):
            raise BundleError(f"submodule is absent, changed or conflicted: {line}")
        match = re.fullmatch(r" ([0-9a-f]{40}) (.+?)(?: \(.+\))?", line)
        if match is None:
            raise BundleError(f"cannot parse submodule identity: {line}")
        commit, relative = match.groups()
        submodule = checkout / relative
        if _git(submodule, "rev-parse", "HEAD") != commit:
            raise BundleError(f"submodule commit mismatch: {relative}")
        repositories.append(
            {"path": relative, "commit": commit, "url": _repository_url(submodule)}
        )
    repositories[1:] = sorted(repositories[1:], key=lambda entry: entry["path"])
    return repositories


def _copy_git_archive(
    output: tarfile.TarFile,
    checkout: Path,
    commit: str,
    archive_prefix: PurePosixPath,
    epoch: int,
) -> None:
    process = subprocess.Popen(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "tar.umask=0022",
            "archive",
            "--format=tar",
            commit,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    with tarfile.open(fileobj=process.stdout, mode="r|") as source:
        for member in source:
            name = str(archive_prefix / member.name.rstrip("/"))
            contents = source.extractfile(member) if member.isfile() else None
            _normalise_member(member, name, epoch)
            output.addfile(member, contents)
    stderr = process.stderr.read() if process.stderr is not None else b""
    if process.wait() != 0:
        raise BundleError(f"git archive failed: {stderr.decode('utf-8', 'replace')}")


def create_git_source_archive(
    checkout: Path,
    expected_commit: str,
    output_path: Path,
    *,
    prefix: str,
    source_date_epoch: int,
) -> list[dict[str, str]]:
    """Archive the superproject and every recursive gitlink deterministically."""
    repositories = _repositories(checkout, expected_commit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw, compressed, archive = _open_reproducible_tar(output_path)
    try:
        for repository in repositories:
            relative = repository["path"]
            source = checkout if relative == "." else checkout / relative
            archive_prefix = PurePosixPath(prefix)
            if relative != ".":
                archive_prefix /= relative
            _copy_git_archive(
                archive,
                source,
                repository["commit"],
                archive_prefix,
                source_date_epoch,
            )
        provenance = {
            "schema_version": 1,
            "repositories": repositories,
        }
        _add_bytes(
            archive,
            f"{prefix}/SOURCE_PROVENANCE.json",
            (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode(),
            epoch=source_date_epoch,
        )
    finally:
        archive.close()
        compressed.close()
        raw.close()
    return repositories


def parse_debian_inventory(
    text: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    packages: list[dict[str, str]] = []
    source_identities: dict[tuple[str, str], dict[str, str]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != 5:
            raise BundleError(f"malformed Debian inventory row: {line}")
        binary, source, source_version, binary_version, homepage = cells
        if not source or not source_version:
            raise BundleError(f"missing Debian source identity for {binary}")
        packages.append(
            {
                "binary": binary,
                "source": source,
                "source_version": source_version,
                "binary_version": binary_version,
                "homepage": homepage,
            }
        )
        escaped_name = urllib.parse.quote(source, safe="+~.-")
        escaped_version = urllib.parse.quote(source_version, safe="+~.:-")
        source_identities[(source, source_version)] = {
            "name": source,
            "version": source_version,
            "url": f"https://sources.debian.org/src/{escaped_name}/{escaped_version}/",
        }
    packages.sort(key=lambda entry: entry["binary"])
    sources = sorted(source_identities.values(), key=lambda entry: entry["name"])
    return packages, sources


def _docker_output(image: str, entrypoint: str, arguments: list[str]) -> bytes:
    output = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            entrypoint,
            image,
            *arguments,
        ],
        text=False,
    )
    assert isinstance(output, bytes)
    return output


def _repack_tar(
    source_bytes: bytes,
    output_path: Path,
    *,
    epoch: int,
) -> list[str]:
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(source_bytes), mode="r:") as source:
        for member in source:
            if not (member.isfile() or member.islnk()):
                raise BundleError(
                    f"Debian copyright archive contains unsupported entry: {member.name}"
                )
            extracted = source.extractfile(member)
            if extracted is None:
                raise BundleError(
                    f"Debian copyright archive entry has no bytes: {member.name}"
                )
            contents = extracted.read()
            output_member = copy.copy(member)
            output_member.type = tarfile.REGTYPE
            output_member.linkname = ""
            output_member.size = len(contents)
            members.append((output_member, contents))
    raw, compressed, output = _open_reproducible_tar(output_path)
    try:
        for member, contents in sorted(members, key=lambda pair: pair[0].name):
            _normalise_member(member, member.name, epoch)
            output.addfile(member, io.BytesIO(contents))
    finally:
        output.close()
        compressed.close()
        raw.close()
    return [member.name for member, _ in members]


def collect_runtime_metadata(
    image: str,
    provenance: dict[str, Any],
    output_dir: Path,
    *,
    source_date_epoch: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    inspect_raw = _run(["docker", "image", "inspect", image])
    assert isinstance(inspect_raw, str)
    inspected = json.loads(inspect_raw)[0]
    expected_digest = provenance["image_index"]
    repo_digests = inspected.get("RepoDigests") or []
    if inspected.get("Id") != expected_digest and not any(
        digest.endswith(f"@{expected_digest}") for digest in repo_digests
    ):
        raise BundleError("runtime image does not resolve to the declared image index")
    if (inspected.get("Os"), inspected.get("Architecture")) != ("linux", "amd64"):
        raise BundleError("runtime image is not linux/amd64")

    module_line = _docker_output(
        image,
        "sha256sum",
        ["/var/lib/falkordb/bin/falkordb.so"],
    ).decode()
    module_sha = module_line.split(maxsplit=1)[0]
    if module_sha != provenance["module_sha256"]:
        raise BundleError("runtime FalkorDB module checksum differs from provenance")

    inventory_bytes = _docker_output(
        image, "dpkg-query", ["-W", f"-f={INVENTORY_FORMAT}"]
    )
    packages, sources = parse_debian_inventory(inventory_bytes.decode())
    if len(packages) != EXPECTED_DEBIAN_PACKAGES:
        raise BundleError(
            f"expected {EXPECTED_DEBIAN_PACKAGES} Debian packages, got {len(packages)}"
        )
    copyright_paths = [
        f"usr/share/doc/{entry['binary'].split(':', 1)[0]}/copyright"
        for entry in packages
    ]
    copyright_tar = _docker_output(
        image,
        "tar",
        ["-C", "/", "-chf", "-", *copyright_paths],
    )
    names = _repack_tar(
        copyright_tar,
        output_dir / "debian-copyright.tar.gz",
        epoch=source_date_epoch,
    )
    if sorted(names) != sorted(copyright_paths):
        raise BundleError(
            "Debian copyright archive does not cover every binary package"
        )

    runtime = {
        "schema_version": 1,
        "requested_reference": image,
        "image_id": inspected["Id"],
        "repo_digests": repo_digests,
        "os": inspected["Os"],
        "architecture": inspected["Architecture"],
        "size": inspected["Size"],
        "module_sha256": module_sha,
        "debian_binary_package_count": len(packages),
        "debian_source_package_count": len(sources),
        "debian_copyright_path_count": len(names),
    }
    return packages, sources, runtime


def _download(url: str, target: Path, expected_sha256: str | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (
        expected_sha256 is None or _sha256(target) == expected_sha256
    ):
        return
    temporary = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "Cairn-source-bundle/1"}
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as error:
        temporary.unlink(missing_ok=True)
        raise BundleError(f"download failed: {url}: {error}") from error
    if expected_sha256 is not None and _sha256(temporary) != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise BundleError(f"download checksum mismatch: {url}")
    temporary.replace(target)


def _json_url(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Cairn-source-bundle/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            document = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise BundleError(f"metadata request failed: {url}: {error}") from error
    if not isinstance(document, dict):
        raise BundleError(f"metadata response is not an object: {url}")
    return document


def _dsc_sha256(contents: str) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    in_checksums = False
    for line in contents.splitlines():
        if line == "Checksums-Sha256:":
            in_checksums = True
            continue
        if in_checksums and not line.startswith(" "):
            break
        if in_checksums:
            checksum, size, name = line.split(maxsplit=2)
            result[name] = (int(size), checksum)
    if not result:
        raise BundleError("Debian .dsc lacks Checksums-Sha256")
    return result


def download_debian_sources(
    sources: list[dict[str, str]],
    cache_dir: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in sources:
        name = source["name"]
        version = source["version"]
        escaped_name = urllib.parse.quote(name, safe="+~.-")
        escaped_version = urllib.parse.quote(version, safe="+~.:-")
        api = f"{DEBIAN_SNAPSHOT}/mr/package/{escaped_name}/{escaped_version}/srcfiles"
        result = _json_url(api).get("result")
        if not isinstance(result, list) or not result:
            raise BundleError(
                f"Debian snapshot has no source files for {name} {version}"
            )
        component_dir = cache_dir / name / version
        component_records: list[dict[str, Any]] = []
        for item in result:
            sha1 = item.get("hash") if isinstance(item, dict) else None
            if not isinstance(sha1, str) or not re.fullmatch(r"[0-9a-f]{40}", sha1):
                raise BundleError(f"invalid Debian snapshot hash for {name} {version}")
            info = _json_url(f"{DEBIAN_SNAPSHOT}/mr/file/{sha1}/info").get("result")
            if not isinstance(info, list) or not info:
                raise BundleError(f"Debian snapshot lacks file metadata for {sha1}")
            filenames = {entry.get("name") for entry in info if isinstance(entry, dict)}
            if len(filenames) != 1:
                raise BundleError(f"ambiguous Debian snapshot filename for {sha1}")
            filename = filenames.pop()
            if not isinstance(filename, str) or "/" in filename:
                raise BundleError(f"unsafe Debian snapshot filename for {sha1}")
            target = component_dir / filename
            _download(f"{DEBIAN_SNAPSHOT}/file/{sha1}", target)
            if _sha1(target) != sha1:
                raise BundleError(f"Debian snapshot SHA-1 mismatch for {filename}")
            component_records.append(
                {
                    "filename": filename,
                    "sha1": sha1,
                    "sha256": _sha256(target),
                    "size": target.stat().st_size,
                    "url": f"{DEBIAN_SNAPSHOT}/file/{sha1}",
                }
            )
        dscs = [
            record
            for record in component_records
            if record["filename"].endswith(".dsc")
        ]
        if len(dscs) != 1:
            raise BundleError(f"expected one Debian .dsc for {name} {version}")
        checksums = _dsc_sha256((component_dir / dscs[0]["filename"]).read_text())
        actual_files = {record["filename"]: record for record in component_records}
        required_names = set(checksums) | {dscs[0]["filename"]}
        if not required_names.issubset(actual_files):
            raise BundleError(
                f"Debian .dsc source file is missing for {name} {version}"
            )
        for filename, (size, checksum) in checksums.items():
            record = actual_files[filename]
            if (record["size"], record["sha256"]) != (size, checksum):
                raise BundleError(f"Debian .dsc checksum mismatch for {filename}")
        selected_records = [actual_files[filename] for filename in required_names]
        records.append(
            {
                "name": name,
                "version": version,
                "sources_debian_url": source["url"],
                "snapshot_api": api,
                "files": sorted(selected_records, key=lambda entry: entry["filename"]),
            }
        )
    return records


def copy_verified_debian_sources(
    records: list[dict[str, Any]],
    cache_dir: Path,
    output_dir: Path,
) -> None:
    """Copy only regular source files selected and verified by their .dsc."""
    for component in records:
        name = component["name"]
        version = component["version"]
        if not re.fullmatch(r"[A-Za-z0-9+.-]+", name) or "/" in version:
            raise BundleError(f"unsafe Debian source identity: {name} {version}")
        for record in component["files"]:
            filename = record["filename"]
            if Path(filename).name != filename:
                raise BundleError(f"unsafe Debian source filename: {filename}")
            source = cache_dir / name / version / filename
            if source.is_symlink() or not source.is_file():
                raise BundleError(
                    f"verified Debian source is not a regular file: {source}"
                )
            actual = (source.stat().st_size, _sha1(source), _sha256(source))
            expected = (record["size"], record["sha1"], record["sha256"])
            if actual != expected:
                raise BundleError(f"cached Debian source identity changed: {source}")
            target = output_dir / name / version / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _write_tsv(path: Path, headings: list[str], rows: Iterable[dict[str, str]]) -> None:
    lines = ["\t".join(headings)]
    for row in rows:
        lines.append("\t".join(row[heading] for heading in headings))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def copy_verified_upstream_recipe(source: Path, target: Path) -> None:
    manifest = source / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file():
        raise BundleError(f"upstream recipe checksum manifest is missing: {manifest}")
    records: list[tuple[str, str]] = []
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None or match.group(2) == "SHA256SUMS":
            raise BundleError(f"unsafe upstream recipe checksum row: {line}")
        records.append((match.group(1), match.group(2)))
    if not records:
        raise BundleError("upstream recipe checksum manifest is empty")
    target.mkdir(parents=True, exist_ok=True)
    for checksum, name in records:
        candidate = source / name
        if candidate.is_symlink() or not candidate.is_file():
            raise BundleError(f"upstream recipe is not a regular file: {candidate}")
        if _sha256(candidate) != checksum:
            raise BundleError(f"upstream recipe checksum mismatch: {candidate}")
        shutil.copy2(candidate, target / name)
    shutil.copy2(manifest, target / manifest.name)


def _copy_tree_files(source: Path, target: Path) -> None:
    included = [
        "README.md",
        "SOURCE_BUNDLE.md",
        "MODIFICATIONS.md",
        "build.sh",
        "prepare_source_bundle.py",
        "provenance.json",
    ]
    target.mkdir(parents=True, exist_ok=True)
    for name in included:
        candidate = source / name
        if not candidate.is_file():
            raise BundleError(f"distribution source file is missing: {candidate}")
        shutil.copy2(candidate, target / name)
    copy_verified_upstream_recipe(source / "upstream", target / "upstream")


def _checksums(root: Path) -> str:
    lines = []
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        if path.relative_to(root) == Path("SHA256SUMS"):
            continue
        lines.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}")
    return "\n".join(lines) + "\n"


def _archive_directory(source: Path, output: Path, prefix: str, epoch: int) -> None:
    raw, compressed, archive = _open_reproducible_tar(output)
    try:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            name = f"{prefix}/{relative}"
            member = archive.gettarinfo(str(path), arcname=name)
            _normalise_member(member, name, epoch)
            if member.isfile():
                with path.open("rb") as contents:
                    archive.addfile(member, contents)
            else:
                archive.addfile(member)
    finally:
        archive.close()
        compressed.close()
        raw.close()


def publish_no_clobber(temporary: Path, target: Path) -> None:
    """Atomically link a prepared artefact without replacing reviewed bytes."""
    try:
        os.link(temporary, target)
    except FileExistsError as error:
        raise BundleError(f"release artefact already exists: {target}") from error
    temporary.unlink()


def prepare(arguments: argparse.Namespace) -> Path:
    distribution = cast(Path, arguments.distribution_dir).resolve()
    falkordb_checkout = cast(Path, arguments.falkordb_checkout).resolve()
    runtime_image = cast(str, arguments.runtime_image)
    provenance = json.loads((distribution / "provenance.json").read_text())
    expected_commit = provenance["patched_commit"]
    source_epoch = int(
        _git(
            falkordb_checkout,
            "show",
            "-s",
            "--format=%ct",
            expected_commit,
        )
    )
    if source_epoch != EXPECTED_PATCHED_EPOCH:
        raise BundleError(
            f"patched commit epoch mismatch: expected {EXPECTED_PATCHED_EPOCH}, "
            f"got {source_epoch}"
        )
    output_dir = cast(Path, arguments.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cast(Path, arguments.download_cache).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cairn-falkordb-source-") as temporary:
        staging = Path(temporary) / "bundle"
        sources_dir = staging / "sources"
        metadata_dir = staging / "metadata"
        notices_dir = staging / "notices"
        recipe_dir = staging / "build-recipe"
        for directory in (sources_dir, metadata_dir, notices_dir, recipe_dir):
            directory.mkdir(parents=True)

        falkordb_archive = sources_dir / "FalkorDB-v4.20.4-cairn.1.tar.gz"
        repositories = create_git_source_archive(
            falkordb_checkout,
            expected_commit,
            falkordb_archive,
            prefix="FalkorDB-v4.20.4-cairn.1",
            source_date_epoch=source_epoch,
        )
        _write_json(metadata_dir / "falkordb-repositories.json", repositories)

        redis_cache = cache_dir / f"redis-{REDIS_VERSION}.tar.gz"
        _download(REDIS_URL, redis_cache, REDIS_SHA256)
        shutil.copyfile(redis_cache, sources_dir / redis_cache.name)
        with tarfile.open(redis_cache, "r:gz") as redis_source:
            licence = redis_source.extractfile(f"redis-{REDIS_VERSION}/LICENSE.txt")
            if licence is None:
                raise BundleError("Redis source archive does not contain LICENSE.txt")
            (notices_dir / "redis-LICENSE.txt").write_bytes(licence.read())

        packages, debian_sources, runtime = collect_runtime_metadata(
            runtime_image,
            provenance,
            notices_dir,
            source_date_epoch=source_epoch,
        )
        _write_tsv(
            metadata_dir / "debian-packages.tsv",
            ["binary", "binary_version", "source", "source_version", "homepage"],
            packages,
        )
        _write_tsv(
            metadata_dir / "debian-source-access.tsv",
            ["name", "version", "url"],
            debian_sources,
        )
        debian_cache = cache_dir / "debian"
        debian_records = download_debian_sources(debian_sources, debian_cache)
        _write_json(metadata_dir / "debian-source-files.json", debian_records)
        copy_verified_debian_sources(
            debian_records,
            debian_cache,
            sources_dir / "debian",
        )
        _write_json(metadata_dir / "runtime-image.json", runtime)

        source_license = falkordb_checkout / "LICENSE.txt"
        if not source_license.is_file():
            raise BundleError("FalkorDB checkout does not contain LICENSE.txt")
        shutil.copyfile(source_license, notices_dir / "falkordb-LICENSE.txt")
        _copy_tree_files(distribution, recipe_dir)
        shutil.copyfile(distribution / "MODIFICATIONS.md", staging / "MODIFICATIONS.md")
        (staging / "README.md").write_text(
            "Cairn-maintained FalkorDB v4.20.4-cairn.1 corresponding-source bundle.\n"
            "See build-recipe/SOURCE_BUNDLE.md and MODIFICATIONS.md before use.\n"
        )
        (staging / "SHA256SUMS").write_text(_checksums(staging))

        name = "cairn-falkordb-v4.20.4-cairn.1-source"
        archive_path = output_dir / f"{name}.tar.gz"
        checksum_path = output_dir / f"{archive_path.name}.sha256"
        if archive_path.exists() or checksum_path.exists():
            raise BundleError(
                "release artefact already exists; choose an empty output directory"
            )
        archive_handle, archive_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".part", dir=output_dir
        )
        checksum_handle, checksum_name = tempfile.mkstemp(
            prefix=f".{checksum_path.name}.", suffix=".part", dir=output_dir
        )
        os.close(archive_handle)
        os.close(checksum_handle)
        temporary_archive = Path(archive_name)
        temporary_checksum = Path(checksum_name)
        try:
            _archive_directory(staging, temporary_archive, name, source_epoch)
            checksum = _sha256(temporary_archive)
            temporary_checksum.write_text(f"{checksum}  {archive_path.name}\n")
            publish_no_clobber(temporary_archive, archive_path)
            publish_no_clobber(temporary_checksum, checksum_path)
        finally:
            temporary_archive.unlink(missing_ok=True)
            temporary_checksum.unlink(missing_ok=True)
    return archive_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--falkordb-checkout", required=True, type=Path)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--download-cache",
        required=True,
        type=Path,
        help="retained cache for checksum-verified Redis and Debian source files",
    )
    parser.add_argument(
        "--distribution-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    return parser


def main(argv: list[str]) -> int:
    try:
        archive = prepare(_parser().parse_args(argv))
    except (BundleError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f"source bundle preparation failed: {error}", file=sys.stderr)
        return 1
    print(f"prepared {archive}")
    print(f"sha256 {_sha256(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
