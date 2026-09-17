from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from cairn.runtime.config import (
    ConfigError,
    DeliveryConfig,
    GraphitiConfig,
    load_config,
    resolve_config_path,
)

CONFIG = """\
schema_version: cairn.config/v1
instance_id: 11111111-1111-4111-8111-111111111111
mode: test
http:
  host: 127.0.0.1
  port: 8000
paths:
  data: /tmp/cairn-test/data
  credentials: /tmp/cairn-test/credentials
"""


def write_config(path: Path, contents: str = CONFIG) -> None:
    path.write_text(contents, encoding="utf-8")


def assert_safe_exception_graph(error: ConfigError) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None


def assert_cairn_traceback_omits(error: ConfigError, sentinel: str) -> None:
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename.endswith("/src/cairn/runtime/config.py"):
            assert sentinel not in repr(frame.f_locals)
        traceback = traceback.tb_next


def mutate_model(model: object, field: str, value: object) -> None:
    setattr(model, field, value)


def test_invalid_utf8_retains_no_source_exception(tmp_path: Path) -> None:
    config_path = tmp_path / "decoder-sentinel.yaml"
    config_path.write_bytes(b"\xffdecoder-sentinel")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "invalid_encoding"
    assert_safe_exception_graph(raised.value)


def test_invalid_yaml_retains_no_source_exception(tmp_path: Path) -> None:
    config_path = tmp_path / "yaml-sentinel.yaml"
    write_config(config_path, "yaml-sentinel: [\n")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "invalid_yaml"
    assert_safe_exception_graph(raised.value)


def test_deeply_nested_yaml_is_a_safe_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "recursion-sentinel.yaml"
    nested_value = ("[" * 1000) + "0" + ("]" * 1000)
    write_config(config_path, f"recursion-sentinel: {nested_value}\n")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "invalid_yaml"
    assert_safe_exception_graph(raised.value)


def test_rejects_string_port_without_retaining_validation_error(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pydantic-sentinel.yaml"
    write_config(config_path, CONFIG.replace("port: 8000", 'port: "8000"'))

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "invalid_config"
    assert raised.value.field == "http.port"
    assert_safe_exception_graph(raised.value)


def test_unreadable_config_retains_no_filesystem_exception(tmp_path: Path) -> None:
    config_path = tmp_path / "filesystem-sentinel.yaml"

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "config_unreadable"
    assert_safe_exception_graph(raised.value)


def test_loads_strict_v1_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    config = load_config(config_path)

    assert config.instance_id == UUID("11111111-1111-4111-8111-111111111111")
    assert config.mode == "test"
    assert config.http.host == "127.0.0.1"
    assert config.http.port == 8000
    assert config.paths.data == Path("/tmp/cairn-test/data")
    assert config.paths.credentials == Path("/tmp/cairn-test/credentials")


def test_loaded_models_are_frozen(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    config = load_config(config_path)

    with pytest.raises(ValidationError):
        mutate_model(config, "mode", "production")
    with pytest.raises(ValidationError):
        mutate_model(config.http, "port", 9999)
    with pytest.raises(ValidationError):
        mutate_model(config.paths, "data", Path("/tmp/other"))


def test_rejects_unknown_key_without_echoing_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, f"{CONFIG}password: super-secret-value\n")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "unknown_field"
    assert "super-secret-value" not in str(raised.value)
    assert_safe_exception_graph(raised.value)


@pytest.mark.parametrize("duplicate", [False, True])
def test_rejects_secret_shaped_key_without_retaining_name(
    tmp_path: Path,
    duplicate: bool,
) -> None:
    config_path = tmp_path / "config.yaml"
    secret_key = "password123"
    submitted = f"{secret_key}: first\n"
    if duplicate:
        submitted += f"{secret_key}: second\n"
    write_config(config_path, f"{CONFIG}{submitted}")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == ("duplicate_key" if duplicate else "unknown_field")
    assert raised.value.field is None
    assert secret_key not in str(raised.value)
    assert secret_key not in repr(raised.value)
    assert_safe_exception_graph(raised.value)


def test_config_error_cairn_traceback_retains_no_submitted_source(
    tmp_path: Path,
) -> None:
    sentinel = "tracebacksecret123"
    config_path = tmp_path / f"{sentinel}.yaml"
    write_config(config_path, f"{CONFIG}{sentinel}: {sentinel}\n")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "unknown_field"
    assert_cairn_traceback_omits(raised.value, sentinel)


def test_rejects_duplicate_yaml_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path, CONFIG.replace("mode: test", "mode: test\nmode: production")
    )

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "duplicate_key"
    assert raised.value.field == "mode"
    assert_safe_exception_graph(raised.value)


def test_rejects_relative_data_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        CONFIG.replace("data: /tmp/cairn-test/data", "data: relative/data"),
    )

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "absolute_path_required"
    assert raised.value.field == "paths.data"
    assert_safe_exception_graph(raised.value)


def test_rejects_config_larger_than_256_kib(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"x" * 262145)

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "config_too_large"
    assert_safe_exception_graph(raised.value)


def test_accepts_valid_config_exactly_256_kib(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    size_limit = 256 * 1024
    encoded_config = CONFIG.encode("utf-8")
    padding = b"#" + (b"x" * (size_limit - len(encoded_config) - 2)) + b"\n"
    payload = encoded_config + padding
    assert len(payload) == 262144
    config_path.write_bytes(payload)

    config = load_config(config_path)

    assert config.instance_id == UUID("11111111-1111-4111-8111-111111111111")


def test_cli_path_precedes_cairn_config_environment(tmp_path: Path) -> None:
    cli_path = tmp_path / "cli.yaml"
    environment_path = tmp_path / "environment.yaml"
    cli_path.touch()
    environment_path.touch()

    result = resolve_config_path(cli_path, {"CAIRN_CONFIG": str(environment_path)})

    assert result == cli_path


def test_no_other_environment_value_changes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    monkeypatch.setenv("CAIRN_HTTP_PORT", "9999")

    config = load_config(config_path)

    assert config.http.port == 8000


def test_config_without_attic_block_defaults_to_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    config = load_config(config_path)

    assert config.attic.enabled is False


def test_config_with_attic_block_parses_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, f"{CONFIG}attic:\n  enabled: true\n")

    config = load_config(config_path)

    assert config.attic.enabled is True


def test_rejects_unknown_key_inside_attic_block(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, f"{CONFIG}attic:\n  enabled: true\n  extra: 1\n")

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "unknown_field"
    assert_safe_exception_graph(raised.value)


def test_rejects_invalid_attic_enabled_and_reports_the_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, f'{CONFIG}attic:\n  enabled: "yes"\n')

    with pytest.raises(ConfigError) as raised:
        load_config(config_path)

    assert raised.value.code == "invalid_config"
    assert raised.value.field == "attic.enabled"
    assert_safe_exception_graph(raised.value)


def test_delivery_chunk_size_defaults_to_one() -> None:
    assert DeliveryConfig().chunk_size == 1


def test_delivery_chunk_size_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        DeliveryConfig(chunk_size=0)


def test_delivery_chunk_size_rejects_values_above_the_bound() -> None:
    with pytest.raises(ValidationError):
        DeliveryConfig(chunk_size=501)


def test_delivery_chunk_size_accepts_the_bound() -> None:
    assert DeliveryConfig(chunk_size=500).chunk_size == 500


def test_config_with_delivery_block_parses_chunk_size(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, f"{CONFIG}delivery:\n  chunk_size: 7\n")

    config = load_config(config_path)

    assert config.delivery.chunk_size == 7


def test_graphiti_semaphore_limit_defaults_to_the_library_default() -> None:
    assert GraphitiConfig(enabled=False).semaphore_limit == 20


def test_graphiti_semaphore_limit_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        GraphitiConfig(enabled=False, semaphore_limit=0)


def test_graphiti_semaphore_limit_rejects_values_above_the_bound() -> None:
    with pytest.raises(ValidationError):
        GraphitiConfig(enabled=False, semaphore_limit=101)


def test_graphiti_semaphore_limit_accepts_the_bound() -> None:
    assert GraphitiConfig(enabled=False, semaphore_limit=100).semaphore_limit == 100


def test_config_with_graphiti_block_parses_semaphore_limit(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        f"{CONFIG}graphiti:\n  enabled: false\n  semaphore_limit: 7\n",
    )

    config = load_config(config_path)

    assert config.graphiti.semaphore_limit == 7


def test_graphiti_index_concurrency_limit_defaults_below_the_untuned_ceiling() -> None:
    assert GraphitiConfig(enabled=False).index_concurrency_limit == 16


def test_graphiti_index_concurrency_limit_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        GraphitiConfig(enabled=False, index_concurrency_limit=0)


def test_graphiti_index_concurrency_limit_rejects_the_untuned_ceiling() -> None:
    """P-82 gate-4, Operator's ruling of 25 August 2026 as clarified on the
    re-review: the bound is on the admissible range, not merely on the
    default. No configurable value may reach the pinned image's untuned
    ``MAX_QUEUED_QUERIES`` of 25, so the guarantee holds against an index
    nobody has tuned rather than only out of the box."""
    with pytest.raises(ValidationError):
        GraphitiConfig(enabled=False, index_concurrency_limit=25)


def test_graphiti_index_concurrency_limit_rejects_values_above_the_bound() -> None:
    with pytest.raises(ValidationError):
        GraphitiConfig(enabled=False, index_concurrency_limit=129)


def test_graphiti_index_concurrency_limit_accepts_the_bound() -> None:
    assert (
        GraphitiConfig(
            enabled=False, index_concurrency_limit=24
        ).index_concurrency_limit
        == 24
    )


def test_config_with_graphiti_block_parses_index_concurrency_limit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        f"{CONFIG}graphiti:\n  enabled: false\n  index_concurrency_limit: 7\n",
    )

    config = load_config(config_path)

    assert config.graphiti.index_concurrency_limit == 7


def test_config_with_graphiti_block_parses_the_edge_batch_dials(
    tmp_path: Path,
) -> None:
    # The model-level tests reach the fields through GraphitiConfig
    # directly; this pins the loader path operators actually use, where
    # a renamed or unwired field would be refused by extra="forbid"
    # rather than silently ignored.
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        f"{CONFIG}graphiti:\n"
        "  enabled: false\n"
        "  edge_batch_size: 4\n"
        "  edge_batch_linger_ms: 20\n"
        "  edge_batch_max_facts: 33\n",
    )

    config = load_config(config_path)

    assert config.graphiti.edge_batch_size == 4
    assert config.graphiti.edge_batch_linger_ms == 20
    assert config.graphiti.edge_batch_max_facts == 33


def test_graphiti_edge_batch_defaults_are_the_measured_values() -> None:
    config = GraphitiConfig(enabled=True)
    assert config.edge_batch_size == 10
    assert config.edge_batch_linger_ms == 75
    assert config.edge_batch_max_facts == 80


@pytest.mark.parametrize(
    "field, value",
    [
        ("edge_batch_size", 0),
        ("edge_batch_size", 51),
        ("edge_batch_linger_ms", -1),
        ("edge_batch_linger_ms", 1001),
        ("edge_batch_max_facts", 0),
        ("edge_batch_max_facts", 501),
    ],
)
def test_graphiti_edge_batch_bounds_refuse(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as raised:
        cast(Any, GraphitiConfig)(enabled=True, **{field: value})
    errors = raised.value.errors()
    assert [error["type"] for error in errors] == [
        "greater_than_equal" if value < 1 else "less_than_equal"
    ]
    assert [error["loc"] for error in errors] == [(field,)]


def test_graphiti_edge_batch_size_accepts_the_bounds() -> None:
    assert GraphitiConfig(enabled=True, edge_batch_size=1).edge_batch_size == 1
    assert GraphitiConfig(enabled=True, edge_batch_size=50).edge_batch_size == 50


def test_graphiti_edge_batch_linger_ms_accepts_the_bounds() -> None:
    assert (
        GraphitiConfig(enabled=True, edge_batch_linger_ms=0).edge_batch_linger_ms == 0
    )
    assert (
        GraphitiConfig(enabled=True, edge_batch_linger_ms=1000).edge_batch_linger_ms
        == 1000
    )


def test_graphiti_edge_batch_max_facts_accepts_the_bounds() -> None:
    assert (
        GraphitiConfig(enabled=True, edge_batch_max_facts=1).edge_batch_max_facts == 1
    )
    assert (
        GraphitiConfig(enabled=True, edge_batch_max_facts=500).edge_batch_max_facts
        == 500
    )
