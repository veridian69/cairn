"""OCI target-to-runtime provenance tests for the reference harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from scripts.reference_acceptance_state import (  # noqa: E402
    HarnessRefusal,
    oci_provenance,
    runtime_config_digest,
)

STATE_HELPER = REPOSITORY / "scripts" / "reference_acceptance_state.py"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
CTR_HEADER = "REF    TYPE    DIGEST    SIZE    PLATFORMS    LABELS"


def _blob(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _config_blob() -> bytes:
    return _blob(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
        }
    )


def _manifest_blob(
    config_digest: str,
    *,
    media_type: str = OCI_MANIFEST,
    config_media_type: str = OCI_CONFIG,
    config_size: Any | None = None,
    schema_version: Any = 2,
) -> bytes:
    return _blob(
        {
            "schemaVersion": schema_version,
            "mediaType": media_type,
            "config": {
                "mediaType": config_media_type,
                "digest": config_digest,
                "size": len(_config_blob()) if config_size is None else config_size,
            },
            "layers": [],
        }
    )


def _index_blob(
    manifest_digest: str,
    *,
    manifest_size: Any = 200,
    media_type: str = OCI_INDEX,
    schema_version: Any = 2,
    platforms: list[dict[str, str]] | None = None,
    manifest_media_type: str = OCI_MANIFEST,
) -> bytes:
    selected_platforms = platforms or [{"os": "linux", "architecture": "amd64"}]
    manifests = [
        {
            "mediaType": manifest_media_type,
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": platform,
        }
        for platform in selected_platforms
    ]
    manifests.append(
        {
            "mediaType": OCI_MANIFEST,
            "digest": "sha256:" + "e" * 64,
            "size": 80,
            "platform": {"os": "unknown", "architecture": "unknown"},
            "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
        }
    )
    return _blob(
        {
            "schemaVersion": schema_version,
            "mediaType": media_type,
            "manifests": manifests,
        }
    )


def _valid_index_chain() -> tuple[str, str, str, dict[str, bytes]]:
    config = _config_blob()
    config_digest = _digest(config)
    manifest = _manifest_blob(config_digest)
    manifest_digest = _digest(manifest)
    target = _index_blob(manifest_digest, manifest_size=len(manifest))
    target_digest = _digest(target)
    return (
        target_digest,
        manifest_digest,
        config_digest,
        {
            target_digest: target,
            manifest_digest: manifest,
            config_digest: config,
        },
    )


def _fetcher(contents: dict[str, bytes]) -> Callable[[str], bytes]:
    def fetch(digest: str) -> bytes:
        return contents[digest]

    return fetch


def _replace_blob(contents: dict[str, bytes], old_digest: str, content: bytes) -> str:
    contents.pop(old_digest)
    new_digest = _digest(content)
    contents[new_digest] = content
    return new_digest


def test_index_with_attestation_selects_one_linux_amd64_chain() -> None:
    target, manifest, config, contents = _valid_index_chain()

    assert oci_provenance(target, _fetcher(contents)) == {
        "target_digest": target,
        "platform_manifest_digest": manifest,
        "config_digest": config,
    }


def test_supported_direct_docker_manifest_uses_target_as_platform_manifest() -> None:
    config_blob = _config_blob()
    config = _digest(config_blob)
    target_blob = _manifest_blob(
        config,
        media_type=DOCKER_MANIFEST,
        config_media_type=DOCKER_CONFIG,
    )
    target = _digest(target_blob)
    contents = {target: target_blob, config: config_blob}

    assert oci_provenance(target, _fetcher(contents)) == {
        "target_digest": target,
        "platform_manifest_digest": target,
        "config_digest": config,
    }


def test_docker_index_can_select_one_docker_linux_amd64_manifest() -> None:
    config_blob = _config_blob()
    config = _digest(config_blob)
    manifest_blob = _manifest_blob(
        config,
        media_type=DOCKER_MANIFEST,
        config_media_type=DOCKER_CONFIG,
    )
    manifest = _digest(manifest_blob)
    target_blob = _index_blob(
        manifest,
        manifest_size=len(manifest_blob),
        media_type=DOCKER_INDEX,
        manifest_media_type=DOCKER_MANIFEST,
    )
    target = _digest(target_blob)

    assert oci_provenance(
        target,
        _fetcher({target: target_blob, manifest: manifest_blob, config: config_blob}),
    ) == {
        "target_digest": target,
        "platform_manifest_digest": manifest,
        "config_digest": config,
    }


@pytest.mark.parametrize("value", ["missing", 1, True, 2.0, "248", -1])
def test_selected_manifest_descriptor_requires_exact_integer_size(value: Any) -> None:
    target, _manifest, _config, contents = _valid_index_chain()
    payload = json.loads(contents[target])
    if value == "missing":
        payload["manifests"][0].pop("size")
    elif type(value) is int and value == 1:
        payload["manifests"][0]["size"] += 1
    else:
        payload["manifests"][0]["size"] = value
    target = _replace_blob(contents, target, _blob(payload))

    with pytest.raises(HarnessRefusal, match="size"):
        oci_provenance(target, _fetcher(contents))


@pytest.mark.parametrize("value", ["missing", 1, True, 2.0, "78", -1])
def test_config_descriptor_requires_exact_integer_size(value: Any) -> None:
    target, manifest, _config, contents = _valid_index_chain()
    manifest_payload = json.loads(contents[manifest])
    if value == "missing":
        manifest_payload["config"].pop("size")
    elif type(value) is int and value == 1:
        manifest_payload["config"]["size"] += 1
    else:
        manifest_payload["config"]["size"] = value
    manifest = _replace_blob(contents, manifest, _blob(manifest_payload))
    target = _replace_blob(
        contents,
        target,
        _index_blob(manifest, manifest_size=len(contents[manifest])),
    )

    with pytest.raises(HarnessRefusal, match="size"):
        oci_provenance(target, _fetcher(contents))


@pytest.mark.parametrize("schema_version", [2.0, True, "2"])
@pytest.mark.parametrize("kind", ["index", "manifest"])
def test_schema_version_must_be_the_integer_two(schema_version: Any, kind: str) -> None:
    target, manifest, _config, contents = _valid_index_chain()
    if kind == "index":
        target_payload = json.loads(contents[target])
        target_payload["schemaVersion"] = schema_version
        target = _replace_blob(contents, target, _blob(target_payload))
    else:
        manifest_payload = json.loads(contents[manifest])
        manifest_payload["schemaVersion"] = schema_version
        manifest = _replace_blob(contents, manifest, _blob(manifest_payload))
        target = _replace_blob(
            contents,
            target,
            _index_blob(manifest, manifest_size=len(contents[manifest])),
        )

    with pytest.raises(HarnessRefusal, match="schema"):
        oci_provenance(target, _fetcher(contents))


def test_attestation_descriptor_still_requires_a_valid_integer_size() -> None:
    target, _manifest, _config, contents = _valid_index_chain()
    payload = json.loads(contents[target])
    payload["manifests"][1]["size"] = True
    target = _replace_blob(contents, target, _blob(payload))

    with pytest.raises(HarnessRefusal, match="size"):
        oci_provenance(target, _fetcher(contents))


@pytest.mark.parametrize(
    "mutation",
    [
        "target-hash",
        "target-json",
        "target-schema",
        "target-media",
        "platform-missing",
        "platform-duplicate",
        "platform-media",
        "manifest-hash",
        "manifest-schema",
        "manifest-media",
        "config-missing",
        "config-media",
        "config-digest",
        "config-hash",
        "config-json",
    ],
)
def test_malformed_or_ambiguous_oci_chain_is_refused(mutation: str) -> None:
    target, manifest, config, contents = _valid_index_chain()
    if mutation == "target-hash":
        contents[target] += b" "
    elif mutation == "target-json":
        target = _replace_blob(contents, target, b"not-json")
    elif mutation in {"target-schema", "target-media"}:
        target = _replace_blob(
            contents,
            target,
            _index_blob(
                manifest,
                manifest_size=len(contents[manifest]),
                schema_version=1 if mutation == "target-schema" else 2,
                media_type=(
                    "application/example" if mutation == "target-media" else OCI_INDEX
                ),
            ),
        )
    elif mutation in {"platform-missing", "platform-duplicate", "platform-media"}:
        platforms = (
            [{"os": "linux", "architecture": "arm64"}]
            if mutation == "platform-missing"
            else [
                {"os": "linux", "architecture": "amd64"},
                {"os": "linux", "architecture": "amd64"},
            ]
        )
        target = _replace_blob(
            contents,
            target,
            _index_blob(
                manifest,
                manifest_size=len(contents[manifest]),
                platforms=platforms,
                manifest_media_type=(
                    "application/example"
                    if mutation == "platform-media"
                    else OCI_MANIFEST
                ),
            ),
        )
    elif mutation == "manifest-hash":
        contents[manifest] = b" " + contents[manifest][1:]
    elif mutation in {"manifest-schema", "manifest-media"}:
        manifest = _replace_blob(
            contents,
            manifest,
            _manifest_blob(
                config,
                schema_version=1 if mutation == "manifest-schema" else 2,
                media_type=(
                    "application/example"
                    if mutation == "manifest-media"
                    else OCI_MANIFEST
                ),
            ),
        )
        target = _replace_blob(
            contents,
            target,
            _index_blob(manifest, manifest_size=len(contents[manifest])),
        )
    elif mutation in {"config-missing", "config-media", "config-digest"}:
        manifest_payload = json.loads(contents[manifest])
        if mutation == "config-missing":
            manifest_payload.pop("config")
        elif mutation == "config-media":
            manifest_payload["config"]["mediaType"] = "application/example"
        else:
            manifest_payload["config"]["digest"] = "not-a-digest"
        manifest = _replace_blob(contents, manifest, _blob(manifest_payload))
        target = _replace_blob(
            contents,
            target,
            _index_blob(manifest, manifest_size=len(contents[manifest])),
        )
    elif mutation == "config-hash":
        contents[config] = b" " + contents[config][1:]
    else:
        config = _replace_blob(contents, config, b"not-json")
        manifest = _replace_blob(
            contents,
            manifest,
            _manifest_blob(config, config_size=len(contents[config])),
        )
        target = _replace_blob(
            contents,
            target,
            _index_blob(manifest, manifest_size=len(contents[manifest])),
        )

    with pytest.raises(HarnessRefusal):
        oci_provenance(target, _fetcher(contents))


def test_runtime_image_id_must_equal_provenance_config_digest() -> None:
    config = "sha256:" + "d" * 64
    assert runtime_config_digest(config, config) == config
    with pytest.raises(HarnessRefusal, match="config digest"):
        runtime_config_digest("sha256:" + "c" * 64, config)


def _key(*arguments: str) -> str:
    return json.dumps(list(arguments), separators=(",", ":"))


def _run_fake_ctr(
    tmp_path: Path,
    requested: str,
    target: str,
    contents: dict[str, bytes],
    *,
    table_media_type: str = OCI_INDEX,
    override: tuple[str, dict[str, Any]] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    canonical = "docker.io/library/cairn:task11"
    responses: dict[str, dict[str, Any]] = {
        _key("--namespace", "k8s.io", "images", "list", "-q"): {
            "stdout": canonical + "\n"
        },
        _key(
            "--namespace",
            "k8s.io",
            "images",
            "list",
            f"name=={canonical}",
        ): {
            "stdout": (
                f"{CTR_HEADER}\n{canonical} {table_media_type} "
                f"{target} 90.0 MiB linux/amd64 -\n"
            )
        },
    }
    for digest, content in contents.items():
        responses[_key("--namespace", "k8s.io", "content", "get", digest)] = {
            "stdout": content.decode()
        }
    if override is not None:
        responses[override[0]] = override[1]
    fixture = tmp_path / "ctr-fixture.json"
    fixture.write_text(json.dumps(responses), encoding="utf-8")
    log = tmp_path / "ctr-log.jsonl"
    executable = tmp_path / "ctr"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_CTR_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
with open(os.environ["FAKE_CTR_FIXTURE"], encoding="utf-8") as stream:
    responses = json.load(stream)
response = responses.get(json.dumps(arguments, separators=(",", ":")))
if response is None:
    print("unexpected fake ctr command", file=sys.stderr)
    raise SystemExit(97)
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
raise SystemExit(response.get("returncode", 0))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(STATE_HELPER), "resolve-oci-provenance", requested],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "FAKE_CTR_FIXTURE": str(fixture),
            "FAKE_CTR_LOG": str(log),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    commands = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    return result, commands


def test_fake_ctr_proves_exact_index_manifest_config_argv(tmp_path: Path) -> None:
    target, manifest, config, contents = _valid_index_chain()
    result, commands = _run_fake_ctr(
        tmp_path,
        "cairn:task11",
        target,
        contents,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "reference": "docker.io/library/cairn:task11",
        "target_digest": target,
        "platform_manifest_digest": manifest,
        "config_digest": config,
    }
    assert commands[-3:] == [
        ["--namespace", "k8s.io", "content", "get", target],
        ["--namespace", "k8s.io", "content", "get", manifest],
        ["--namespace", "k8s.io", "content", "get", config],
    ]


def test_fake_ctr_direct_manifest_fetches_target_then_config(tmp_path: Path) -> None:
    config_blob = _config_blob()
    config = _digest(config_blob)
    target_blob = _manifest_blob(
        config,
        media_type=DOCKER_MANIFEST,
        config_media_type=DOCKER_CONFIG,
    )
    target = _digest(target_blob)
    result, commands = _run_fake_ctr(
        tmp_path,
        "cairn:task11",
        target,
        {target: target_blob, config: config_blob},
        table_media_type=DOCKER_MANIFEST,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["platform_manifest_digest"] == target
    assert commands[-2:] == [
        ["--namespace", "k8s.io", "content", "get", target],
        ["--namespace", "k8s.io", "content", "get", config],
    ]


@pytest.mark.parametrize("failure", ["stderr", "nonzero"])
def test_fake_ctr_content_diagnostic_or_failure_refuses(
    tmp_path: Path, failure: str
) -> None:
    target, _manifest, _config, contents = _valid_index_chain()
    key = _key("--namespace", "k8s.io", "content", "get", target)
    response = (
        {"stdout": contents[target].decode(), "stderr": "synthetic diagnostic\n"}
        if failure == "stderr"
        else {"stderr": "synthetic failure\n", "returncode": 1}
    )
    result, commands = _run_fake_ctr(
        tmp_path,
        "cairn:task11",
        target,
        contents,
        override=(key, response),
    )

    assert result.returncode == 2
    assert commands[-1] == ["--namespace", "k8s.io", "content", "get", target]
