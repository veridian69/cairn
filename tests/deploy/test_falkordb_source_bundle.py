"""Behavioural checks for the maintained FalkorDB source-bundle generator."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "deploy" / "falkordb" / "prepare_source_bundle.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("falkordb_source_bundle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(path: Path, filename: str) -> str:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Source Bundle Test")
    _git(path, "config", "user.email", "source-bundle@example.invalid")
    (path / filename).write_text(f"contents of {filename}\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "--quiet", "-m", f"add {filename}")
    return _git(path, "rev-parse", "HEAD")


def _recursive_fixture(tmp_path: Path) -> tuple[Path, str]:
    leaf = tmp_path / "leaf"
    _repository(leaf, "leaf.txt")

    child = tmp_path / "child"
    _repository(child, "child.txt")
    _git(
        child,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(leaf),
        "vendor/leaf",
    )
    _git(child, "commit", "--quiet", "-am", "add leaf source")

    root = tmp_path / "root"
    _repository(root, "root.txt")
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(child),
        "deps/child",
    )
    _git(root, "commit", "--quiet", "-am", "add recursive source")
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
    )
    return root, _git(root, "rev-parse", "HEAD")


def test_git_archive_contains_every_recursive_gitlink_at_its_recorded_commit(
    tmp_path: Path,
) -> None:
    """Dropping a nested gitlink must make this complete-source test fail."""
    module = _load_script()
    checkout, commit = _recursive_fixture(tmp_path)
    archive = tmp_path / "source.tar.gz"

    module.create_git_source_archive(
        checkout,
        commit,
        archive,
        prefix="FalkorDB-cairn",
        source_date_epoch=1_700_000_000,
    )

    with tarfile.open(archive, "r:gz") as source:
        names = set(source.getnames())
        assert "FalkorDB-cairn/root.txt" in names
        assert "FalkorDB-cairn/deps/child/child.txt" in names
        assert "FalkorDB-cairn/deps/child/vendor/leaf/leaf.txt" in names
        provenance_file = source.extractfile("FalkorDB-cairn/SOURCE_PROVENANCE.json")
        assert provenance_file is not None
        provenance = json.load(provenance_file)
    assert [entry["path"] for entry in provenance["repositories"]] == [
        ".",
        "deps/child",
        "deps/child/vendor/leaf",
    ]


def test_git_archive_is_byte_reproducible_and_rejects_dirty_source(
    tmp_path: Path,
) -> None:
    """Using worktree bytes or ambient timestamps must make this test fail."""
    module = _load_script()
    checkout, commit = _recursive_fixture(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    arguments = {
        "prefix": "FalkorDB-cairn",
        "source_date_epoch": 1_700_000_000,
    }

    _git(checkout, "config", "tar.umask", "0000")
    module.create_git_source_archive(checkout, commit, first, **arguments)
    _git(checkout, "config", "tar.umask", "0077")
    module.create_git_source_archive(checkout, commit, second, **arguments)
    assert first.read_bytes() == second.read_bytes()

    (checkout / "root.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(module.BundleError, match="checkout is not clean"):
        module.create_git_source_archive(checkout, commit, second, **arguments)


def test_debian_inventory_requires_source_identity_and_builds_exact_source_urls() -> (
    None
):
    """Losing source package/version provenance must make this test fail."""
    module = _load_script()
    inventory = (
        "libssl3t64:amd64\topenssl\t3.5.7-1~deb13u2\t"
        "3.5.7-1~deb13u2\thttps://openssl-library.org\n"
        "openssl\topenssl\t3.5.7-1~deb13u2\t"
        "3.5.7-1~deb13u2\thttps://openssl-library.org\n"
    )

    packages, sources = module.parse_debian_inventory(inventory)

    assert len(packages) == 2
    assert sources == [
        {
            "name": "openssl",
            "version": "3.5.7-1~deb13u2",
            "url": "https://sources.debian.org/src/openssl/3.5.7-1~deb13u2/",
        }
    ]
    with pytest.raises(module.BundleError, match="missing Debian source identity"):
        module.parse_debian_inventory("openssl\t\t\t3.5.7\t\n")


def test_only_verified_regular_debian_source_files_enter_the_bundle(
    tmp_path: Path,
) -> None:
    """Copying a whole cache or following a selected symlink must fail this test."""
    module = _load_script()
    cache = tmp_path / "cache"
    component = cache / "openssl" / "3.5.7-1"
    component.mkdir(parents=True)
    selected = component / "openssl_3.5.7-1.dsc"
    selected.write_text("selected\n", encoding="utf-8")
    (component / "stale-secret").write_text("must not ship\n", encoding="utf-8")
    (component / "stale-link").symlink_to("stale-secret")
    records = [
        {
            "name": "openssl",
            "version": "3.5.7-1",
            "files": [
                {
                    "filename": selected.name,
                    "sha1": "19ff2ad0f5a8deac38a16bfd9a8e9bf93d3393ae",
                    "sha256": (
                        "8575b2c9683596d519df1c89545df6f65d4ddd2fe3387b9a6b3cadd0b4ba2c36"
                    ),
                    "size": 9,
                    "url": "https://snapshot.debian.org/file/example",
                }
            ],
        }
    ]
    output = tmp_path / "bundle"

    module.copy_verified_debian_sources(records, cache, output)

    assert [
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    ] == ["openssl/3.5.7-1/openssl_3.5.7-1.dsc"]
    selected.unlink()
    selected.symlink_to("stale-secret")
    with pytest.raises(module.BundleError, match="not a regular file"):
        module.copy_verified_debian_sources(records, cache, output)


def test_repository_provenance_strips_credentials_and_request_metadata(
    tmp_path: Path,
) -> None:
    """Leaking URL credentials, queries or fragments must make this test fail."""
    module = _load_script()
    repository = tmp_path / "repository"
    _repository(repository, "source.txt")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://example-user:placeholder@example.invalid/source.git?token=placeholder#x",
    )

    assert module._repository_url(repository) == ("https://example.invalid/source.git")


def test_release_bundle_output_refuses_to_replace_existing_bytes(
    tmp_path: Path,
) -> None:
    """A rerun clobbering a reviewed archive must make this test fail."""
    module = _load_script()
    temporary = tmp_path / "new"
    target = tmp_path / "release.tar.gz"
    temporary.write_bytes(b"new bytes")
    target.write_bytes(b"reviewed bytes")

    with pytest.raises(module.BundleError, match="already exists"):
        module.publish_no_clobber(temporary, target)

    assert target.read_bytes() == b"reviewed bytes"


def test_internal_manifest_covers_nested_vendor_checksum_files(tmp_path: Path) -> None:
    """Skipping nested SHA256SUMS integrity metadata must make this test fail."""
    module = _load_script()
    (tmp_path / "SHA256SUMS").write_text("self\n", encoding="utf-8")
    nested = tmp_path / "build-recipe" / "upstream"
    nested.mkdir(parents=True)
    (nested / "SHA256SUMS").write_text("vendor\n", encoding="utf-8")

    manifest = module._checksums(tmp_path)

    assert "build-recipe/upstream/SHA256SUMS" in manifest
    assert "  SHA256SUMS\n" not in manifest


def test_upstream_recipe_copies_only_manifested_verified_files(tmp_path: Path) -> None:
    """Untracked or changed recipe files entering the bundle must fail this test."""
    module = _load_script()
    source = tmp_path / "source"
    source.mkdir()
    recipe = source / "Dockerfile.server"
    recipe.write_text("FROM scratch\n", encoding="utf-8")
    runner = source / "run.sh"
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    (source / "untracked-secret").write_text("must not ship\n", encoding="utf-8")
    (source / "SHA256SUMS").write_text(
        "bb57c7da220a8753d7bdabac0d3afdb6efa742e4c736c5bc93ab40dfd5e23b9b"
        "  Dockerfile.server\n"
        "306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb"
        "  run.sh\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    module.copy_verified_upstream_recipe(source, output)

    assert sorted(path.name for path in output.iterdir()) == [
        "Dockerfile.server",
        "SHA256SUMS",
        "run.sh",
    ]
    assert (output / "run.sh").stat().st_mode & 0o111 == 0o111
    recipe.write_text("changed\n", encoding="utf-8")
    with pytest.raises(module.BundleError, match="checksum mismatch"):
        module.copy_verified_upstream_recipe(source, output)


def test_debian_copyright_archive_resolves_hardlinks_to_regular_files(
    tmp_path: Path,
) -> None:
    """A package copyright hardlink must retain bytes without link semantics."""
    module = _load_script()
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:") as archive:
        contents = b"shared copyright text\n"
        canonical = tarfile.TarInfo("usr/share/doc/base/copyright")
        canonical.size = len(contents)
        archive.addfile(canonical, io.BytesIO(contents))
        linked = tarfile.TarInfo("usr/share/doc/library/copyright")
        linked.type = tarfile.LNKTYPE
        linked.linkname = canonical.name
        archive.addfile(linked)
        transitive = tarfile.TarInfo("usr/share/doc/transitive/copyright")
        transitive.type = tarfile.LNKTYPE
        transitive.linkname = linked.name
        archive.addfile(transitive)

    output = tmp_path / "copyright.tar.gz"
    module._repack_tar(source.getvalue(), output, epoch=1_700_000_000)

    with tarfile.open(output, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.isfile() for member in members)
        for member in members:
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == contents
