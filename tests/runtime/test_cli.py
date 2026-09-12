import importlib
import json
import logging
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from cairn import __version__
from cairn.projection.rebuild import RebuildReport
from cairn.runtime.config import (
    CairnConfig,
    DeliveryConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
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


def make_config(tmp_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8123),
        paths=PathConfig(
            data=tmp_path / "data",
            credentials=tmp_path / "credentials",
        ),
    )


def test_package_has_nonempty_version() -> None:
    assert __version__


def test_check_config_returns_zero_and_safe_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")

    result = cli.main(["check-config", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        '{"instance_id": "11111111-1111-4111-8111-111111111111", '
        '"mode": "test", "schema_version": "cairn.config/v1", "status": "ok"}\n'
    )
    assert captured.err == ""


def test_check_config_returns_two_for_invalid_config_without_value_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "configuration-secret-sentinel"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG.replace("mode: test", f"mode: {sentinel}"),
        encoding="utf-8",
    )

    result = cli.main(["check-config", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert json.loads(captured.err) == {
        "code": "invalid_config",
        "field": "mode",
        "status": "error",
    }
    assert captured.out == ""
    assert sentinel not in captured.err


@pytest.mark.parametrize("duplicate", [False, True])
def test_check_config_rejects_secret_shaped_key_without_disclosure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duplicate: bool,
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "secretkey123"
    submitted = f"{sentinel}: first\n"
    if duplicate:
        submitted += f"{sentinel}: second\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"{CONFIG}{submitted}", encoding="utf-8")

    result = cli.main(["check-config", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert json.loads(captured.err) == {
        "code": "duplicate_key" if duplicate else "unknown_field",
        "status": "error",
    }
    assert captured.out == ""
    assert sentinel not in captured.err


def test_cli_config_precedes_cairn_config_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    cli_path = tmp_path / "cli.yaml"
    environment_path = tmp_path / "environment.yaml"
    loader = Mock(return_value=make_config(tmp_path))
    monkeypatch.setattr(cli, "load_config", loader)
    monkeypatch.setenv("CAIRN_CONFIG", str(environment_path))

    result = cli.main(["check-config", "--config", str(cli_path)])

    assert result == 0
    loader.assert_called_once_with(cli_path)


def test_help_lists_only_supported_foundation_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "check-config" in captured.out
    assert "migrate" in captured.out
    assert "verify" in captured.out
    assert "serve" in captured.out
    assert "bootstrap" in captured.out
    assert "recover" in captured.out
    assert captured.err == ""


def test_unknown_option_returns_safe_error_without_value_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "unknown-option-secret-sentinel"

    with pytest.raises(SystemExit) as raised:
        cli.main(["check-config", f"--{sentinel}"])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == '{"code": "invalid_arguments", "status": "error"}\n'
    assert sentinel not in captured.err


def test_unknown_command_returns_safe_error_without_value_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "unknown-command-secret-sentinel"

    with pytest.raises(SystemExit) as raised:
        cli.main([sentinel])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == '{"code": "invalid_arguments", "status": "error"}\n'
    assert sentinel not in captured.err


def test_config_option_abbreviation_is_rejected_safely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    sentinel = "abbreviated-config-secret-sentinel"

    with pytest.raises(SystemExit) as raised:
        cli.main(["check-config", "--conf", sentinel])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == '{"code": "invalid_arguments", "status": "error"}\n'
    assert sentinel not in captured.err


def test_serve_calls_uvicorn_once_with_one_worker_and_no_access_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = importlib.import_module("cairn.runtime.cli")
    config = make_config(tmp_path)
    application = object()
    run_server = Mock()
    monkeypatch.setattr(cli, "load_config", Mock(return_value=config))
    monkeypatch.setattr(cli, "build_application", Mock(return_value=application))
    monkeypatch.setattr(cli.uvicorn, "run", run_server)

    result = cli.main(["serve", "--config", str(tmp_path / "config.yaml")])

    assert result == 0
    run_server.assert_called_once_with(
        application,
        host="127.0.0.1",
        port=8123,
        workers=1,
        access_log=False,
        lifespan="on",
        log_config=cli.UVICORN_LOGGING_CONFIG,
        proxy_headers=False,
    )


def test_the_rebuild_command_forwards_the_configured_chunk_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-82: ``_run_rebuild_index`` reads ``config.delivery.chunk_size``
    into the ``rebuild_index`` call (src/cairn/runtime/cli.py, near line
    239) rather than silently defaulting it — with the CLI's own
    dependency seams mocked, mirroring how
    ``test_serve_calls_uvicorn_once_with_one_worker_and_no_access_log``
    isolates ``serve``."""
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8123),
        paths=PathConfig(data=data_path, credentials=tmp_path / "credentials"),
        graphiti=GraphitiConfig(enabled=True),
        delivery=DeliveryConfig(chunk_size=42),
    )
    recorded: dict[str, object] = {}

    def fake_rebuild_index(*args: object, **kwargs: object) -> RebuildReport:
        recorded.update(kwargs)
        return RebuildReport(projected=0, failed=0, unreadable=0, superseded_rows=0)

    monkeypatch.setattr(cli, "load_config", Mock(return_value=config))
    monkeypatch.setattr(cli, "_default_index", Mock(return_value=(Mock(), None)))
    monkeypatch.setattr(cli, "rebuild_index", fake_rebuild_index)

    result = cli.main(["rebuild-index", "--config", str(tmp_path / "config.yaml")])

    assert result == 0
    assert recorded["chunk_size"] == 42


def test_the_rebuild_command_passes_a_logger_for_the_demotion_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-82 gate-4 ruling (25 August 2026): an operator running
    ``rebuild-index`` must see ``projection_bulk_demoted`` — a rebuild
    without a logger would swallow the event this remediation exists to
    emit. Seams mocked as in the chunk-size test above."""
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8123),
        paths=PathConfig(data=data_path, credentials=tmp_path / "credentials"),
        graphiti=GraphitiConfig(enabled=True),
    )
    recorded: dict[str, object] = {}

    def fake_rebuild_index(*args: object, **kwargs: object) -> RebuildReport:
        recorded.update(kwargs)
        return RebuildReport(projected=0, failed=0, unreadable=0, superseded_rows=0)

    monkeypatch.setattr(cli, "load_config", Mock(return_value=config))
    monkeypatch.setattr(cli, "_default_index", Mock(return_value=(Mock(), None)))
    monkeypatch.setattr(cli, "rebuild_index", fake_rebuild_index)

    result = cli.main(["rebuild-index", "--config", str(tmp_path / "config.yaml")])

    assert result == 0
    assert recorded.get("logger") is not None


def test_the_rebuild_command_shares_one_writer_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5/I-25: one in-process writer gate. ``_run_rebuild_index`` must
    create a single lock and pass that same object to both
    ``_default_index`` (for the extraction-cache store) and
    ``CatalogueTransactions`` — two locks would let cache writes race the
    rebuild's own catalogue mutations."""
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8123),
        paths=PathConfig(data=data_path, credentials=tmp_path / "credentials"),
        graphiti=GraphitiConfig(enabled=True),
    )
    recorded: dict[str, object] = {}

    def fake_default_index(
        config: CairnConfig,
        *,
        writer_gate: object = None,
        safe_logger: object = None,
    ) -> tuple[Mock, None]:
        recorded["index_gate"] = writer_gate
        recorded["safe_logger"] = safe_logger
        return Mock(), None

    def fake_transactions(*args: object, **kwargs: object) -> Mock:
        recorded["transactions_gate"] = kwargs.get("writer_gate")
        return Mock()

    monkeypatch.setattr(cli, "load_config", Mock(return_value=config))
    monkeypatch.setattr(cli, "_default_index", fake_default_index)
    monkeypatch.setattr(cli, "CatalogueTransactions", fake_transactions)
    monkeypatch.setattr(
        cli,
        "rebuild_index",
        Mock(
            return_value=RebuildReport(
                projected=0, failed=0, unreadable=0, superseded_rows=0
            )
        ),
    )

    result = cli.main(["rebuild-index", "--config", str(tmp_path / "config.yaml")])

    assert result == 0
    assert recorded["index_gate"] is not None
    assert recorded["index_gate"] is recorded["transactions_gate"]
    # R4: the rebuild threads its one SafeLogger into the index build too.
    assert recorded["safe_logger"] is not None


def test_rebuild_quarantines_falkordb_driver_before_adapter_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I-32: adapter construction may query FalkorDB and its dependency
    logger includes full query parameters on failure, so quarantine must be
    installed before ``_default_index`` runs."""
    cli = importlib.import_module("cairn.runtime.cli")
    data_path = tmp_path / "data"
    data_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8123),
        paths=PathConfig(data=data_path, credentials=tmp_path / "credentials"),
        graphiti=GraphitiConfig(enabled=True),
    )
    sentinel = "adapter-construction-query-parameter-sentinel"
    dependencies = [
        logging.getLogger("graphiti_core"),
        logging.getLogger("graphiti_core.driver.falkordb_driver"),
    ]
    originals = [
        (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in dependencies
    ]

    def fake_default_index(
        config: CairnConfig, *args: object, **kwargs: object
    ) -> tuple[Mock, None]:
        dependencies[1].error("unsafe dependency query parameter: %s", sentinel)
        return Mock(), None

    monkeypatch.setattr(cli, "_default_index", fake_default_index)
    monkeypatch.setattr(
        cli,
        "rebuild_index",
        Mock(
            return_value=RebuildReport(
                projected=0, failed=0, unreadable=0, superseded_rows=0
            )
        ),
    )
    for dependency in dependencies:
        dependency.handlers = []
        dependency.setLevel(logging.ERROR)
        dependency.propagate = True
        dependency.disabled = False
    caplog.set_level(logging.ERROR)
    try:
        result = cli._run_rebuild_index(config)
        assert result == 0
        assert sentinel not in caplog.text
    finally:
        for dependency, original in zip(dependencies, originals, strict=True):
            handlers, level, propagate, disabled = original
            dependency.handlers = handlers
            dependency.setLevel(level)
            dependency.propagate = propagate
            dependency.disabled = disabled


def test_real_serve_emits_only_safe_json_without_bind_configuration(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    credentials_path = tmp_path / "credentials"
    data_path.mkdir()
    credentials_path.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    sentinel = "uvicorn-config-sentinel"
    config_path = tmp_path / f"{sentinel}.yaml"
    config_path.write_text(
        CONFIG.replace("port: 8000", f"port: {port}")
        .replace("/tmp/cairn-test/data", str(data_path))
        .replace("/tmp/cairn-test/credentials", str(credentials_path)),
        encoding="utf-8",
    )
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "cairn",
            "migrate",
            "--config",
            str(config_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert migration.returncode == 0
    assert migration.stderr == ""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cairn",
            "serve",
            "--config",
            str(config_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = time.monotonic() + 8
        while True:
            if process.poll() is not None:
                pytest.fail(f"serve exited before health check: {process.returncode}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live",
                    timeout=0.25,
                ) as response:
                    assert response.status == 200
                    break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    pytest.fail("serve did not become live within 8 seconds")
                time.sleep(0.05)
        process.terminate()
        stdout, stderr = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)

    assert process.returncode in {0, -signal.SIGTERM}
    assert stdout == ""
    records = [json.loads(line) for line in stderr.splitlines()]
    events = [record["event"] for record in records]
    assert events[:2] == ["catalogue_verified", "runtime_started"]
    assert "request_completed" in events
    assert events[-1] == "runtime_stopped"
    assert str(port) not in stderr
    assert "127.0.0.1" not in stderr
    assert sentinel not in stderr
