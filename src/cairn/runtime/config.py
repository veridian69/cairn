from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_CONFIG_PATH = Path("/etc/cairn/config.yaml")
MAX_CONFIG_BYTES = 256 * 1024
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "0.0.0.0"})
_SAFE_ERROR_FIELDS = frozenset(
    {
        "schema_version",
        "instance_id",
        "mode",
        "http",
        "http.host",
        "http.port",
        "paths",
        "paths.data",
        "paths.credentials",
        "attic",
        "attic.enabled",
    }
)


class ConfigError(Exception):
    def __init__(self, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field if field in _SAFE_ERROR_FIELDS else None
        detail = f"configuration error: {code}"
        if self.field is not None:
            detail = f"{detail} ({self.field})"
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _ConfigFailure:
    code: str
    field: str | None


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    host: str
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value not in _ALLOWED_HOSTS:
            raise ValueError("host is not permitted")
        return value


class PathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    data: Path
    credentials: Path

    @field_validator("data", "credentials")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("absolute path required")
        return value


class AtticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: bool


class GraphitiConfig(BaseModel):
    """P-39: the retrieval-index block, disabled by default. Connection
    location only — this file carries no secret.

    I-92 amends where the secrets do come from: FalkorDB credentials and
    the model/embedding provider key are convention-named files under
    ``paths.credentials``, read by composition, and the provider key is
    exported into the process environment there because graphiti-core
    reads it from nowhere else. The claim this comment used to make —
    that they come from the environment — lost to I-19, which permits
    files from Secrets and forbids secret environment variables in the
    manifest."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: bool
    host: str = "127.0.0.1"
    port: int = 6379
    # P-82, Operator's ruling of 25 August 2026: the configured form of
    # graphiti-core's SEMAPHORE_LIMIT throughput dial — in-flight LLM
    # calls for the whole delivery process — so configuration stays the
    # single authoritative layer (I-19) instead of an environment
    # variable beside it. Default matches the library's own. Bounded at
    # 100 so a typo cannot point three-digit concurrency at a provider
    # tier; raising it is an explicit operator choice made against that
    # tier's rate limits.
    semaphore_limit: int = Field(default=20, ge=1, le=100)
    # P-82, Operator's gate-4 ruling of 25 August 2026: the ceiling on in-flight
    # index queries for the whole delivery process, enforced at the driver
    # so graphiti-core's per-call gathers cannot multiply past it. Both
    # ends of the range sit below the pinned image's untuned
    # MAX_QUEUED_QUERIES of 25, so the bound holds against an index nobody
    # has configured whatever an operator writes here — the ruling binds
    # the admissible range, not merely the default (Operator's clarification of
    # 25 August 2026, on the gate-4 re-review). The deployment
    # configuration raises the server ceiling to 200, but no evidence yet
    # says the delivery path benefits from spending that headroom: gate
    # 5's chunk-size sweep is the event that may justify raising this cap,
    # with measurements behind it.
    index_concurrency_limit: int = Field(default=16, ge=1, le=24)
    # P-87, measured 27 August 2026: resolve_edge calls coalesce into
    # combined provider calls. Size and linger are the values every
    # P-89 run measured; max_facts flushes early so one burst cannot
    # build an oversized prompt for a nano model. A size of 1 disables
    # batching entirely. The combined call's model path is medium by
    # constant, not configuration: batch-on-small measurably degraded
    # dedup accuracy (11/15 vs 13/15) and a dial would invite it back.
    # That accuracy choice carries a per-call cost. Every batch of two
    # or more moves edge dedup off the small model (gpt-4.1-nano) onto
    # graphiti's default (gpt-5.5 at the pinned 0.29.3), and batching
    # ships ON by default: fewer calls, each dearer. Deliberate, and
    # not free.
    # The ranges below are admissibility bounds, not measured values —
    # evidence stops at size 10 and 80 facts, so a tuner reaching for
    # 50 or 500 is past where anything was actually measured.
    edge_batch_size: int = Field(default=10, ge=1, le=50)
    edge_batch_linger_ms: int = Field(default=75, ge=0, le=1000)
    edge_batch_max_facts: int = Field(default=80, ge=1, le=500)


class DeliveryConfig(BaseModel):
    """P-41: the background delivery loop's cadence.

    Five seconds by default — brisk enough that the I-83 ``index_pending``
    window stays short in practice, slow enough that an idle instance is
    not polling its own catalogue continuously. Bounded 1–3600 so neither
    a busy-loop nor an hour-long stall can be configured by accident. P-82:
    chunk_size is also bounded at 500 to prevent a single library call from
    becoming unaccountably large.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    interval_seconds: int = Field(default=5, ge=1, le=3600)
    # P-82: facts per bulk projection call. 1 — the default — keeps the
    # per-fact path byte-for-byte; above 1, delivery and rebuild group
    # facts by partition into sequential chunks projected through
    # ``project_many``. Bounded at 500 so one library call cannot become
    # unaccountably large. Outbox delivery has its own, separate ceiling:
    # ``deliver_projection_outbox``'s ``limit`` (default 100) bounds rows
    # fetched per pass, so the effective chunk there is ``min(chunk_size,
    # limit)`` regardless of this bound; ``rebuild_index`` has no such
    # ceiling and honours ``chunk_size`` in full.
    chunk_size: int = Field(default=1, ge=1, le=500)


class CairnConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["cairn.config/v1"]
    instance_id: UUID
    mode: Literal["production", "test"]
    http: HttpConfig
    paths: PathConfig
    attic: AtticConfig = Field(default=AtticConfig(enabled=False))
    graphiti: GraphitiConfig = Field(default=GraphitiConfig(enabled=False))
    delivery: DeliveryConfig = Field(default=DeliveryConfig())


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            raise ConfigError("invalid_yaml")
        key = loader.construct_object(key_node, deep=deep)
        invalid_key = False
        try:
            duplicate = key in mapping
        except TypeError:
            invalid_key = True
        if invalid_key:
            raise ConfigError("invalid_yaml")
        if duplicate:
            raise ConfigError("duplicate_key", str(key))
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def resolve_config_path(cli_path: Path | None, environ: Mapping[str, str]) -> Path:
    if cli_path is not None:
        return cli_path
    selected = environ.get("CAIRN_CONFIG")
    return Path(selected) if selected else DEFAULT_CONFIG_PATH


def load_config(path: Path) -> CairnConfig:
    result = _load_config_result(path)
    del path
    if isinstance(result, CairnConfig):
        return result
    code = result.code
    field = result.field
    del result
    raise ConfigError(code, field)


def _load_config_result(path: Path) -> CairnConfig | _ConfigFailure:
    try:
        return _load_config_untrusted(path)
    except ConfigError as error:
        failure = _ConfigFailure(error.code, error.field)
    return failure


def _load_config_untrusted(path: Path) -> CairnConfig:
    raw = _read_config_bytes(path)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large")
    decoding_failure: ConfigError | None = None
    try:
        contents = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        decoding_failure = ConfigError("invalid_encoding")
    if decoding_failure is not None:
        raise decoding_failure
    yaml_failure: tuple[str, str | None] | None = None
    try:
        document = yaml.load(contents, Loader=_StrictSafeLoader)
    except ConfigError as error:
        yaml_failure = (error.code, error.field)
    except (RecursionError, yaml.YAMLError):
        yaml_failure = ("invalid_yaml", None)
    if yaml_failure is not None:
        raise ConfigError(*yaml_failure)
    if not isinstance(document, dict):
        raise ConfigError("top_level_mapping_required")
    validation_failure: ConfigError | None = None
    try:
        config = CairnConfig.model_validate(_normalise_yaml_scalars(document))
    except ValidationError as error:
        validation_failure = _safe_validation_error(error)
    if validation_failure is not None:
        raise validation_failure
    return config


def _read_config_bytes(path: Path) -> bytes:
    read_failure: ConfigError | None = None
    try:
        with path.open("rb") as config_file:
            raw = config_file.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        read_failure = ConfigError("config_unreadable")
    if read_failure is not None:
        raise read_failure
    return raw


def _normalise_yaml_scalars(document: dict[object, object]) -> dict[object, object]:
    normalised = dict(document)
    instance_id = normalised.get("instance_id")
    if isinstance(instance_id, str):
        try:
            normalised["instance_id"] = UUID(instance_id)
        except ValueError:
            pass
    paths = normalised.get("paths")
    if isinstance(paths, Mapping):
        normalised_paths = dict(paths)
        for key in ("data", "credentials"):
            value = normalised_paths.get(key)
            if isinstance(value, str):
                normalised_paths[key] = Path(value)
        normalised["paths"] = normalised_paths
    return normalised


def _safe_validation_error(error: ValidationError) -> ConfigError:
    details = error.errors(include_input=False)
    if not details:
        return ConfigError("invalid_config")
    detail = details[0]
    field = _field_from_location(detail.get("loc", ()))
    error_type = detail.get("type")
    if error_type == "extra_forbidden":
        return ConfigError("unknown_field", field)
    if error_type == "value_error" and field in {"paths.data", "paths.credentials"}:
        return ConfigError("absolute_path_required", field)
    return ConfigError("invalid_config", field)


def _field_from_location(location: object) -> str | None:
    if not isinstance(location, tuple):
        return None
    parts = [str(part) for part in location if isinstance(part, (str, int))]
    return ".".join(parts) if parts else None
