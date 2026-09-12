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
