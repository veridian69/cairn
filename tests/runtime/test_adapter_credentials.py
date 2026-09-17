"""I-92/P-67: adapter credentials are files under ``paths.credentials``.

Before this, a production instance built its FalkorDB driver with
``username=None, password=None`` — the constructor parameters existed and
nothing passed them — so cross-instance index isolation rested on
NetworkPolicy alone, and any workload that reached the port owned every
graph behind it. The provider key had the same shape of hole from the
other side: graphiti-core reads ``OPENAI_API_KEY`` from the environment,
and I-19 forbids the manifest carrying it, so in a conforming deployment
there was nothing to read.

What these tests fix is the whole of that bridge and its refusals: the
files are the only source, an absent required one stops the start rather
than degrading to an unauthenticated connection, and no failure — typed,
logged or chained — ever carries a value out of the directory.
"""

import json
import os
import threading
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import cairn.runtime.composition as composition
from cairn.catalogue.extraction_cache import ExtractionCacheStore
from cairn.projection.memory import MemoryIndex
from cairn.runtime.cli import main
from cairn.runtime.composition import AdapterCredentialError, _default_index
from cairn.runtime.config import (
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)
from cairn.runtime.logging import configure_logging

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PASSWORD_SENTINEL = "falkordb-password-sentinel"
API_KEY_SENTINEL = "openai-api-key-sentinel"


class RecordingIndex:
    """Stands in for ``GraphitiIndex`` and records what it was built with,
    including the environment at the moment of construction — which is
    where the ``OPENAI_API_KEY`` export has to have happened, not merely
    by the time the test looks."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.environment_key = os.environ.get("OPENAI_API_KEY")

    def close(self) -> None:
        return None


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[RecordingIndex]:
    built: list[RecordingIndex] = []

    def construct(**kwargs: object) -> RecordingIndex:
        index = RecordingIndex(**kwargs)
        built.append(index)
        return index

    monkeypatch.setattr(composition, "GraphitiIndex", construct)
    # Every test in this file owns the variable for its duration: set
    # through monkeypatch so the export the code performs is undone
    # afterwards whether or not the ambient environment had one.
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-value-that-must-be-replaced")
    return built


def _config(
    tmp_path: Path, *, mode: str = "production", graphiti: bool = True
) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode=mode,  # type: ignore[arg-type]
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=tmp_path, credentials=tmp_path / "credentials"),
        graphiti=GraphitiConfig(enabled=graphiti),
    )


def _write(
    directory: Path,
    *,
    username: str | bytes | None = None,
    password: str | bytes | None = PASSWORD_SENTINEL,
    api_key: str | bytes | None = API_KEY_SENTINEL,
) -> Path:
    """The Secret mount as Kubernetes projects it: one file per key, each
    ending in the newline every tool that writes these files adds."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("falkordb-username", username),
        ("falkordb-password", password),
        ("openai-api-key", api_key),
    ):
        if value is None:
            continue
        raw = value if isinstance(value, bytes) else f"{value}\n".encode()
        (directory / name).write_bytes(raw)
    return directory


# --- the credentials a production adapter is built with -----------------------


def test_the_files_reach_the_adapter_and_the_environment(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """The whole of I-92 in one assertion set: FalkorDB's credentials go
    to the constructor, the provider's goes to the environment, and the
    environment is already correct when the adapter is constructed."""
    config = _config(tmp_path)
    _write(config.paths.credentials, username="falkor-user")

    index, owned = _default_index(config, writer_gate=threading.Lock())

    assert len(built) == 1
    # Both names are annotated against the real adapter types and the
    # fixture patches those names with a stand-in, so identity is asserted
    # through ``object`` — the idiom test_index_lifecycle.py already uses.
    assert cast(object, index) is built[0]
    assert cast(object, owned) is built[0]
    kwargs = dict(built[0].kwargs)
    # P-90 task 6: the extraction cache store, checked by type here since
    # it is not itself part of I-92's credential bridge — the rest of this
    # assertion is unchanged. R4 threads the safe logger the same way
    # (None here: this test composes no logger).
    assert isinstance(kwargs.pop("extraction_cache"), ExtractionCacheStore)
    assert kwargs.pop("safe_logger") is None
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 6379,
        "username": "falkor-user",
        "password": PASSWORD_SENTINEL,
        "index_concurrency_limit": 16,
        "edge_batch_size": 10,
        "edge_batch_linger_ms": 75,
        "edge_batch_max_facts": 80,
    }
    assert built[0].environment_key == API_KEY_SENTINEL


def test_the_writer_gate_reaches_the_extraction_cache_store(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    # I3: the store must serialise its writes against the same in-process
    # gate every other catalogue writer takes, so composition threads the
    # exact object it hands CatalogueTransactions through to here.
    config = _config(tmp_path)
    _write(config.paths.credentials, username="falkor-user")
    gate = threading.Lock()

    _default_index(config, writer_gate=gate)

    store = built[0].kwargs["extraction_cache"]
    assert isinstance(store, ExtractionCacheStore)
    assert store._writer_gate is gate


def test_the_safe_logger_reaches_the_store_and_the_adapter(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    # R4: one SafeLogger threaded from composition through the cache
    # store and on into the adapter for the Graphiti seams.
    config = _config(tmp_path)
    _write(config.paths.credentials, username="falkor-user")
    logger = configure_logging(StringIO())

    _default_index(config, writer_gate=threading.Lock(), safe_logger=logger)

    store = built[0].kwargs["extraction_cache"]
    assert isinstance(store, ExtractionCacheStore)
    assert store._logger is logger
    assert built[0].kwargs["safe_logger"] is logger


def test_an_absent_username_file_is_a_credential_of_none(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """FalkorDB's default ACL user has no name. The file is optional
    precisely so that deployment does not have to invent one."""
    config = _config(tmp_path)
    _write(config.paths.credentials)

    _default_index(config, writer_gate=threading.Lock())

    assert built[0].kwargs["username"] is None
    assert built[0].kwargs["password"] == PASSWORD_SENTINEL


def test_an_empty_username_file_is_absence_not_an_empty_name(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """A Secret key projected with no value is the same fact as a key
    that is not there, and P-67 says so for both."""
    config = _config(tmp_path)
    _write(config.paths.credentials, username="")

    _default_index(config, writer_gate=threading.Lock())

    assert built[0].kwargs["username"] is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (b"secret-without-newline", "secret-without-newline"),
        (b"secret-with-one\n", "secret-with-one"),
        # Exactly one newline is stripped: the second belongs to the value,
        # because a secret that genuinely ends in a newline is a secret and
        # not a formatting accident.
        (b"secret-with-two\n\n", "secret-with-two\n"),
        (b"  spaced secret  \n", "  spaced secret  "),
        (b"multi\nline\nsecret\n", "multi\nline\nsecret"),
    ],
)
def test_a_value_is_byte_exact_but_for_one_trailing_newline(
    tmp_path: Path, built: list[RecordingIndex], written: bytes, expected: str
) -> None:
    config = _config(tmp_path)
    _write(config.paths.credentials, password=written)

    _default_index(config, writer_gate=threading.Lock())

    assert built[0].kwargs["password"] == expected


# --- the refusals -------------------------------------------------------------


@pytest.mark.parametrize("missing", ["falkordb-password", "openai-api-key"])
def test_a_missing_required_file_refuses_the_start_by_name(
    tmp_path: Path, built: list[RecordingIndex], missing: str
) -> None:
    """The refusal I-92 exists for: no adapter is constructed, so no
    unauthenticated connection is opened, and the operator is told which
    file is wrong."""
    config = _config(tmp_path)
    _write(
        config.paths.credentials,
        password=None if missing == "falkordb-password" else PASSWORD_SENTINEL,
        api_key=None if missing == "openai-api-key" else API_KEY_SENTINEL,
    )

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_missing"
    assert caught.value.credential == missing
    assert built == []


def test_a_missing_credentials_directory_refuses_the_start(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """The Secret that failed to mount at all, which is the likelier
    production accident than a half-populated directory."""
    config = _config(tmp_path)

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_missing"
    assert caught.value.credential == "falkordb-password"
    assert built == []


@pytest.mark.parametrize("written", [b"", b"\n"])
@pytest.mark.parametrize("empty", ["falkordb-password", "openai-api-key"])
def test_an_empty_required_file_is_refused_as_missing(
    tmp_path: Path, built: list[RecordingIndex], empty: str, written: bytes
) -> None:
    """An empty password would otherwise become a blank credential and an
    authenticated-looking connection that is nothing of the sort. Both
    required files answer for themselves — a Secret can project either key
    with no value."""
    config = _config(tmp_path)
    _write(
        config.paths.credentials,
        password=written if empty == "falkordb-password" else PASSWORD_SENTINEL,
        api_key=written if empty == "openai-api-key" else API_KEY_SENTINEL,
    )

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_missing"
    assert caught.value.credential == empty
    assert built == []


def test_an_unreadable_file_is_its_own_refusal(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """A directory where a file belongs: the shape a mis-projected Secret
    key takes, and an ``OSError`` that must not be read as absence."""
    config = _config(tmp_path)
    _write(config.paths.credentials, password=None)
    (config.paths.credentials / "falkordb-password").mkdir()

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_unreadable"
    assert caught.value.credential == "falkordb-password"
    assert built == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads regardless of the mode bits")
def test_a_file_the_runtime_uid_cannot_read_is_refused(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """The permission accident P-67's posture is chosen to avoid: the
    Secret is mounted, but not readable by the arbitrary runtime UID."""
    config = _config(tmp_path)
    _write(config.paths.credentials)
    (config.paths.credentials / "openai-api-key").chmod(0o000)

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_unreadable"
    assert caught.value.credential == "openai-api-key"
    assert built == []


# --- no value escapes ---------------------------------------------------------


def test_a_refusal_carries_no_other_credential_value(
    tmp_path: Path, built: list[RecordingIndex]
) -> None:
    """One file is wrong; the two that are readable have been read by the
    time it fails. Neither may appear in what the refusal says."""
    config = _config(tmp_path)
    _write(config.paths.credentials, username="falkor-user", api_key=None)

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    rendered = f"{caught.value!r} {caught.value!s} {caught.value.args}"
    assert PASSWORD_SENTINEL not in rendered
    assert "falkor-user" not in rendered
    assert "openai-api-key" in rendered


@pytest.mark.parametrize(
    "written",
    [
        pytest.param(b"\xff\xfe-malformed-secret\n", id="undecodable"),
        # A NUL decodes cleanly and then cannot survive the environment
        # bridge: ``os.environ`` assignment raises ``ValueError`` on one.
        # Untyped, it escaped every handler that knows what a credential
        # failure is and reached the operator as a traceback.
        pytest.param(b"has\x00nul-malformed-secret\n", id="embedded-nul"),
    ],
)
@pytest.mark.parametrize("malformed", ["falkordb-password", "openai-api-key"])
def test_a_malformed_value_is_refused_without_quoting_the_value(
    tmp_path: Path, built: list[RecordingIndex], malformed: str, written: bytes
) -> None:
    """Both required files, both ways a non-empty value can be unusable.

    ``UnicodeDecodeError`` quotes the byte it choked on and its position —
    part of the secret, in a traceback the operator is meant to read —
    which is why that path is raised unchained.
    """
    config = _config(tmp_path)
    _write(
        config.paths.credentials,
        password=written if malformed == "falkordb-password" else PASSWORD_SENTINEL,
        api_key=written if malformed == "openai-api-key" else API_KEY_SENTINEL,
    )

    with pytest.raises(AdapterCredentialError) as caught:
        _default_index(config, writer_gate=threading.Lock())

    assert caught.value.code == "credential_unreadable"
    assert caught.value.credential == malformed
    assert caught.value.__cause__ is None
    assert "malformed-secret" not in f"{caught.value!r} {caught.value!s}"
    assert built == []


# --- the paths that must not have moved ---------------------------------------


def test_test_mode_reads_no_credentials_at_all(
    tmp_path: Path, built: list[RecordingIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory substitute needs no FalkorDB and no provider, and an
    empty credentials directory must stay a legal test-mode deployment."""
    monkeypatch.setenv("OPENAI_API_KEY", "untouched")

    index, owned = _default_index(
        _config(tmp_path, mode="test"), writer_gate=threading.Lock()
    )

    assert isinstance(index, MemoryIndex)
    assert owned is None
    assert built == []
    assert os.environ["OPENAI_API_KEY"] == "untouched"


def test_a_disabled_index_reads_no_credentials_at_all(
    tmp_path: Path, built: list[RecordingIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "untouched")

    index, owned = _default_index(
        _config(tmp_path, graphiti=False), writer_gate=threading.Lock()
    )

    assert index is None
    assert owned is None
    assert built == []
    assert os.environ["OPENAI_API_KEY"] == "untouched"


# --- the operator's view ------------------------------------------------------


def test_the_start_failure_is_logged_by_code_and_not_by_value(
    tmp_path: Path, built: list[RecordingIndex], capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused start joins the lease and catalogue ones in the log
    stream, carrying the failure code and nothing out of the directory —
    including the credential it had already read successfully."""
    config = _config(tmp_path)
    _write(config.paths.credentials, api_key=None)

    with pytest.raises(AdapterCredentialError):
        composition.build_application(config)

    logged = capsys.readouterr().err
    assert '"failure_code": "credentials_unavailable"' in logged
    assert '"event": "runtime_start_failed"' in logged
    assert PASSWORD_SENTINEL not in logged
    assert built == []


def _write_config_file(tmp_path: Path) -> Path:
    """A production instance with the retrieval index on — the one
    configuration I-92 governs — and no credentials directory."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    config_path = tmp_path / "cairn.yaml"
    config_path.write_text(
        "\n".join(
            (
                "schema_version: cairn.config/v1",
                f"instance_id: {INSTANCE_ID}",
                "mode: production",
                "http:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "paths:",
                f"  data: {data}",
                f"  credentials: {tmp_path / 'credentials'}",
                "graphiti:",
                "  enabled: true",
                "",
            )
        ),
        encoding="utf-8",
    )
    return config_path


def test_the_serve_command_refuses_before_it_binds_a_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command I-92 exists for. The refusal has to arrive as an exit
    code and a JSON line, not a traceback out of ``uvicorn.run`` — which
    is asserted here by making any call to it a failure.

    Two lines reach stderr and both are wanted: the structured start
    failure the log stream carries, and the operator's refusal naming the
    file. ``rebuild-index`` emits only the second, because it never builds
    an application.
    """

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server was started on a refused credential")

    monkeypatch.setattr("cairn.runtime.cli.uvicorn.run", never)
    config_path = _write_config_file(tmp_path)

    exit_code = main(["serve", "--config", str(config_path)])

    captured = capsys.readouterr()
    logged, refusal = captured.err.splitlines()
    assert exit_code == 3
    assert json.loads(logged)["failure_code"] == "credentials_unavailable"
    assert json.loads(refusal) == {
        "status": "error",
        "code": "credential_missing",
        "credential": "falkordb-password",
    }
    assert captured.out == ""


def test_the_rebuild_command_refuses_and_names_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rebuild-index`` builds the real adapter too, so I-92 governs it
    as much as ``serve``. Without its own handler the refusal would reach
    the operator as ``internal_error`` with nothing to act on."""
    config_path = _write_config_file(tmp_path)

    exit_code = main(["rebuild-index", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.err) == {
        "status": "error",
        "code": "credential_missing",
        "credential": "falkordb-password",
    }
    assert captured.out == ""
