"""Live evaluation must not inherit productive provider configuration."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def environment() -> Any:
    path = (
        Path(__file__).resolve().parents[2] / "scripts/semantic_memory_environment.py"
    )
    assert path.is_file(), "Semantic environment boundary is missing"
    spec = importlib.util.spec_from_file_location("semantic_environment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_explicit_key_and_allowlisted_process_settings_reach_child(
    tmp_path: Path,
) -> None:
    module = environment()
    key = tmp_path / "designated-test-key"
    key.write_text("sk-synthetic-not-a-real-provider-key\n")
    result = module.provider_environment(
        key,
        {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "OPENAI_API_KEY": "productive-secret",
            "OPENAI_BASE_URL": "https://wrong.invalid",
            "OPENAI_PROJECT_ID": "wrong-project",
            "HTTPS_PROXY": "https://wrong.invalid",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://wrong.invalid",
            "PYTHONPATH": "/untrusted",
            "OTHER_SECRET": "hidden",
        },
    )
    assert result["OPENAI_API_KEY"] == "sk-synthetic-not-a-real-provider-key"
    assert result["PATH"] == "/usr/bin"
    assert result["OTEL_SDK_DISABLED"] == "true"
    assert result["GRAPHITI_TELEMETRY_ENABLED"] == "false"
    assert result["PYTHONUNBUFFERED"] == "1"
    assert not set(result) & {
        "OPENAI_BASE_URL",
        "OPENAI_PROJECT_ID",
        "HTTPS_PROXY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "PYTHONPATH",
        "OTHER_SECRET",
    }


@pytest.mark.parametrize(
    "value", ["", "dummy", "test-key", "short", "x" * 4097, "two\nlines"]
)
def test_missing_dummy_or_malformed_key_refuses_without_value(
    tmp_path: Path, value: str
) -> None:
    module = environment()
    key = tmp_path / "test-key"
    key.write_text(value)
    with pytest.raises(module.EnvironmentError) as caught:
        module.provider_environment(key, {})
    assert str(caught.value) == "test_provider_key_invalid"


def test_missing_and_symlink_key_refuse(tmp_path: Path) -> None:
    module = environment()
    with pytest.raises(module.EnvironmentError, match="test_provider_key_unavailable"):
        module.provider_environment(tmp_path / "missing", {})
    target = tmp_path / "selected"
    target.write_text("sk-synthetic-not-a-real-provider-key")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(module.EnvironmentError, match="test_provider_key_unavailable"):
        module.provider_environment(link, {})


def test_image_pin_parser_never_executes_shell_values() -> None:
    module = environment()
    digest = "a" * 64
    assert (
        module.falkordb_image(
            f"# ignored\nFALKORDB_IMAGE=falkordb/falkordb:v1@sha256:{digest}\n"
        )
        == f"falkordb/falkordb:v1@sha256:{digest}"
    )
    for value in [
        "FALKORDB_IMAGE=$(touch nope)",
        "FALKORDB_IMAGE=falkordb/falkordb:latest",
        "",
        f"FALKORDB_IMAGE=falkordb/falkordb:v1@sha256:{digest}\nFALKORDB_IMAGE=other",
    ]:
        with pytest.raises(module.EnvironmentError, match="image_pin_invalid"):
            module.falkordb_image(value)


@pytest.mark.parametrize(
    "repository",
    [
        "ghcr.io/veridian69/cairn-falkordb",
        "falkordb/falkordb",
        "falkordb/falkordb-server",
        "docker.io/falkordb/falkordb",
        "docker.io/falkordb/falkordb-server",
    ],
)
def test_image_pin_accepts_maintained_and_upstream_repositories(
    repository: str,
) -> None:
    image = f"{repository}:v4.20.4-cairn.1@sha256:{'a' * 64}"
    assert environment().falkordb_image(f"FALKORDB_IMAGE={image}\n") == image


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/veridian69/cairn-falkordb:v4.20.4-cairn.1",
        f"ghcr.io/other/cairn-falkordb:v1@sha256:{'a' * 64}",
        f"ghcr.io/veridian69/cairn-falkordb:$(touch nope)@sha256:{'a' * 64}",
        f"ghcr.io/veridian69/cairn-falkordb:v1@sha256:{'a' * 64};touch nope",
        f"ghcr.io/veridian69/cairn-falkordb:-option@sha256:{'a' * 64}",
        f"ghcr.io/veridian69/cairn-falkordb:{'a' * 129}@sha256:{'a' * 64}",
    ],
)
def test_image_pin_rejects_untrusted_or_invalid_derivative_references(
    image: str,
) -> None:
    module = environment()
    with pytest.raises(module.EnvironmentError, match="image_pin_invalid"):
        module.falkordb_image(f"FALKORDB_IMAGE={image}\n")


def test_image_override_takes_precedence_over_lock() -> None:
    module = environment()
    locked = f"falkordb/falkordb:v1@sha256:{'a' * 64}"
    override = f"ghcr.io/veridian69/cairn-falkordb:v2@sha256:{'b' * 64}"
    assert module.falkordb_image(f"FALKORDB_IMAGE={locked}\n", override) == override


@pytest.mark.parametrize(
    "image",
    [
        f"cairn.local/falkordb-runtime@sha256:{'b' * 64}",
    ],
)
def test_image_override_accepts_reviewed_local_runtime(image: str) -> None:
    assert environment().falkordb_image("", image) == image


@pytest.mark.parametrize(
    "image",
    [
        f"cairn.local/falkordb-runtime@sha256:{'B' * 64}",
        f"cairn.local/falkordb-runtime@sha256:{'b' * 63}",
        f"cairn.local/other-runtime@sha256:{'b' * 64}",
        "cairn.local/falkordb-runtime:latest",
        f"cairn.local/falkordb-runtime:build-{'b' * 64}@sha256:{'b' * 64}",
        "cairn.local/falkordb-runtime",
        "ghcr.io/veridian69/cairn-falkordb:v1",
        f"ghcr.io/other/cairn-falkordb:v1@sha256:{'a' * 64}",
        f"docker.io/library/redis:v1@sha256:{'a' * 64}",
    ],
)
def test_image_override_refuses_unpinned_and_other_repositories(image: str) -> None:
    module = environment()
    with pytest.raises(module.EnvironmentError, match="image_pin_invalid"):
        module.falkordb_image("", image)


def test_lock_refuses_local_runtime_without_override() -> None:
    line = f"FALKORDB_IMAGE=cairn.local/falkordb-runtime@sha256:{'b' * 64}\n"
    module = environment()
    with pytest.raises(module.EnvironmentError, match="image_pin_invalid"):
        module.falkordb_image(line)


def test_zero_placeholder_lock_is_refused_and_names_the_flag() -> None:
    lock = "FALKORDB_IMAGE=cairn.local/falkordb-runtime@sha256:" + "0" * 64 + "\n"
    module = environment()
    with pytest.raises(module.EnvironmentError, match="--falkordb-image") as raised:
        module.falkordb_image(lock)
    assert "image_pin_placeholder" in str(raised.value)


def test_zero_placeholder_override_is_refused_and_names_the_flag() -> None:
    locked = f"FALKORDB_IMAGE=falkordb/falkordb:v1@sha256:{'a' * 64}\n"
    override = "cairn.local/falkordb-runtime@sha256:" + "0" * 64
    module = environment()
    with pytest.raises(module.EnvironmentError, match="--falkordb-image") as raised:
        module.falkordb_image(locked, override)
    assert "image_pin_placeholder" in str(raised.value)
