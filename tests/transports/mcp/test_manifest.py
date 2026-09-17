"""Task 11: the MCP tool-schema manifest, its digest and the proofs I-89
and P-58 name.

The artefact is checked the same three ways its OpenAPI twin is, because
the same failures are available to it: the render is deterministic across
processes rather than merely within one, the two committed copies and the
digest file agree with it, and what the document claims is compared
against the surface a client actually meets over the protocol rather than
against a second transcription of the generator.

The determinism proof is deliberately stronger than
``test_generation_is_deterministic`` in the OpenAPI suite. Two calls in
one process share every cached schema Pydantic built, so they agree even
when the render depends on iteration order that a fresh interpreter would
choose differently; I-89 asks for two generations in separate processes,
and these run under two different hash seeds.
"""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import cairn.runtime.composition as composition_module
from cairn import __version__
from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection, canonical_timestamp
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import configure_logging
from cairn.transports.mcp.manifest import (
    PACKAGED_ARTEFACT,
    packaged_manifest_bytes,
    render_document,
)
from cairn.transports.mcp.mount import MOUNT_PATH
from cairn.transports.rest.v1.openapi import render_document as render_openapi
from cairn.transports.v1.responses import AUDIT_EVENT_SCHEMA

# tests/transports/mcp/test_manifest.py -> repository root is four parents
# up (mcp -> transports -> tests -> root).
REPOSITORY_ROOT = Path(__file__).parents[3]
ARTEFACT = REPOSITORY_ROOT / "contracts" / "cairn-mcp-tools-v1.json"
DIGEST_FILE = ARTEFACT.with_suffix(".json.sha256")
PACKAGED = REPOSITORY_ROOT / "src" / "cairn" / PACKAGED_ARTEFACT

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = UUID("55555555-5555-4555-8555-555555555555")
CREDENTIAL_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

# P-49's pin, written out rather than imported from the SDK: reading the
# constant the generator reads would prove only that it equals itself,
# and the revision is the one thing in this document a dependency bump
# can change without any Cairn source changing with it.
PROTOCOL_REVISION = "2025-11-25"

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_REVISION,
        "capabilities": {},
        "clientInfo": {"name": "conformance", "version": "0"},
    },
}

# I-84's surface, verbatim, and I-89's licence in full: these three keys
# and these four per-tool keys are what the document may carry.
TOOL_NAMES = [
    "read-evidence",
    "ingest",
    "promote",
    "invalidate",
    "create-principal",
    "issue-credential",
    "revoke-credential",
    "create-grant",
    "revoke-grant",
    "read-audit-events",
    "retrieve",
    "instance",
]
DOCUMENT_KEYS = {"protocolVersion", "tools", "failureCodes"}
TOOL_KEYS = {"name", "description", "inputSchema", "outputSchema"}

# I-26's closed set, written out for the reason above.
FAILURE_CODES = [
    "invalid_request",
    "authentication_failed",
    "authorisation_denied",
    "secret_rejected",
    "not_found",
    "idempotency_conflict",
    "evidence_pending",
    "evidence_corrupt",
    "index_pending",
    "stale_index",
    "dependency_unavailable",
    "instance_mismatch",
    "internal_error",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def document() -> Any:
    """The committed artefact, parsed. ``Any`` for the reason the OpenAPI
    suite reads its own that way: this is arbitrary JSON being asserted
    against, not a typed value."""
    return json.loads(ARTEFACT.read_text(encoding="utf-8"))


def make_config(tmp_path: Path) -> CairnConfig:
    data_path = tmp_path / "data"
    credentials_path = tmp_path / "credentials"
    data_path.mkdir()
    credentials_path.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=credentials_path),
    )
    migrate_catalogue(config, lambda: NOW)
    return config


def seed_bearer(data_path: Path) -> str:
    """``test_tools.py``'s recipe: one principal and one credential written
    directly, since this package carries no shared ``conftest.py``."""
    minted = mint_token(CREDENTIAL_ID, lambda count: bytes(range(count)))
    timestamp = canonical_timestamp(NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(PRINCIPAL_ID), "human", "operator", timestamp),
        )
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(CREDENTIAL_ID), str(PRINCIPAL_ID), minted.verifier, timestamp, None),
        )
        connection.commit()
    return f"Bearer {minted.text}"


async def served(tmp_path: Path, *frames: dict[str, object]) -> list[Any]:
    """Each frame's ``result``, taken over the protocol behind the P-54
    bearer check. ``initialize`` is prepended because nothing else is
    answered before it."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    results: list[Any] = []
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            for body in (INITIALIZE, *frames):
                response = await client.post(
                    MOUNT_PATH,
                    content=json.dumps(body),
                    headers={**MCP_HEADERS, "Authorization": bearer},
                )
                results.append(response.json()["result"])
    return results[1:]


def generated(seed: str) -> bytes:
    """One generation in its own interpreter, under a named hash seed."""
    return subprocess.run(
        [sys.executable, "-m", "cairn.transports.mcp.manifest"],
        capture_output=True,
        check=True,
        env={"PATH": "", "PYTHONHASHSEED": seed},
    ).stdout


def test_generation_is_deterministic_across_processes() -> None:
    """I-89's byte-equality proof, and the reason it is spelled with
    subprocesses: a within-process comparison shares Pydantic's schema
    cache and every interned set, so it cannot see an ordering that varies
    between runs. Two hash seeds is what makes the two runs differ in the
    way a rebuild on another machine would."""
    first = generated("0")
    second = generated("1")

    assert first == second
    assert first.decode("utf-8") == render_document()


def test_the_committed_artefact_is_the_rendered_document() -> None:
    """P-58: the manifest is never hand-maintained. The same comparison
    ``make check``'s dirty-diff gate makes, held here as well so a failure
    names the artefact rather than the git index."""
    assert ARTEFACT.read_text(encoding="utf-8") == render_document()


def test_the_packaged_manifest_is_byte_identical_to_the_repository_copy() -> None:
    """P-58, as P-30 for the OpenAPI artefact: the two copies exist because
    a wheel cannot reach outside the package and a container has no
    repository. Neither may drift."""
    assert PACKAGED.read_bytes() == ARTEFACT.read_bytes()
    assert packaged_manifest_bytes() == ARTEFACT.read_bytes()


def test_the_committed_digest_names_the_artefact_in_sha256sum_format() -> None:
    expected = hashlib.sha256(ARTEFACT.read_bytes()).hexdigest()

    assert DIGEST_FILE.read_text(encoding="utf-8") == (f"{expected}  {ARTEFACT.name}\n")


def test_the_document_carries_exactly_what_i89_licenses() -> None:
    """I-89's "and nothing else", which is the clause a manifest drifts
    past first: the eleven tools with their two schemas, the closed
    failure-code enumeration and the protocol revision. No product
    version, no server URL, no capability advertisement."""
    parsed = document()

    assert set(parsed) == DOCUMENT_KEYS
    assert [tool["name"] for tool in parsed["tools"]] == TOOL_NAMES
    assert parsed["failureCodes"] == FAILURE_CODES
    assert parsed["protocolVersion"] == PROTOCOL_REVISION
    for tool in parsed["tools"]:
        assert set(tool) == TOOL_KEYS
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"


def test_the_document_carries_no_environment_derived_content() -> None:
    """I-76's determinism rules, which I-89 applies unchanged. The product
    version is the live case — it comes from ``git describe``, so
    embedding it would make the artefact dirty on every commit — and
    ``GET /v1/instance`` is where a caller learns which build answers."""
    text = ARTEFACT.read_text(encoding="utf-8")

    assert __version__ not in text
    assert str(REPOSITORY_ROOT) not in text


@pytest.mark.anyio
async def test_the_published_tools_are_the_tools_the_server_advertises(
    tmp_path: Path,
) -> None:
    """The manifest's whole purpose: a caller may pin this document
    instead of calling ``tools/list``. Compared against the protocol
    response rather than against ``TOOLS``, because reading the registry
    the generator read would prove only that the generator is a
    function."""
    (listed,) = await served(
        tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )

    assert listed["tools"] == document()["tools"]


@pytest.mark.anyio
async def test_the_published_revision_is_the_one_the_handshake_settles_on(
    tmp_path: Path,
) -> None:
    """I-89 records the negotiated revision, so the document is held
    against a real handshake rather than against the SDK constant it was
    generated from."""
    config = make_config(tmp_path)
    bearer = seed_bearer(config.paths.data)
    application = build_application(config)
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                MOUNT_PATH,
                content=json.dumps(INITIALIZE),
                headers={**MCP_HEADERS, "Authorization": bearer},
            )

    negotiated = response.json()["result"]["protocolVersion"]

    assert negotiated == document()["protocolVersion"]


def test_the_failure_codes_are_the_enumeration_the_openapi_artefact_publishes() -> None:
    """I-90's equivalence at the level of the published documents: one
    closed failure vocabulary, not two that agree today. The OpenAPI side
    publishes it on the failure body's ``code``; this is the same list."""
    published = json.loads(render_openapi())["components"]["schemas"]["FailureBody"]

    assert document()["failureCodes"] == published["properties"]["code"]["enum"]


def test_the_audit_document_the_manifest_publishes_is_the_canonical_one() -> None:
    """The consequence Task 6 created and this artefact publishes: the
    shared ``cairn.audit/v1`` schema moved out of the OpenAPI generator,
    so the manifest must carry it self-containedly in the read tool's
    ``$defs`` and it must be the same document the OpenAPI artefact
    publishes as a component. Two surfaces describing one hash-chained
    event two ways is exactly the divergence I-90 forbids."""
    read = next(
        tool for tool in document()["tools"] if tool["name"] == "read-audit-events"
    )
    carried = read["outputSchema"]["$defs"]["AuditEvent"]
    openapi = json.loads(render_openapi())["components"]["schemas"]["AuditEvent"]

    assert carried == AUDIT_EVENT_SCHEMA
    assert carried == openapi
    # Self-contained: every reference the tool makes resolves inside it.
    defined = {f"#/$defs/{name}" for name in read["outputSchema"]["$defs"]}
    assert set(_references(read["outputSchema"])) <= defined


def _references(node: object) -> list[str]:
    if type(node) is dict:
        found = [str(value) for key, value in node.items() if key == "$ref"]
        for value in node.values():
            found.extend(_references(value))
        return found
    if type(node) is list:
        return [reference for item in node for reference in _references(item)]
    return []


def test_an_instance_without_its_packaged_manifest_refuses_to_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest is a packaged artefact on the same terms as the
    OpenAPI document: a package built without it is a broken build, not a
    runtime condition, so it fails at start rather than serving a null
    ``mcp_contract_digest``."""
    log_stream = StringIO()
    test_logger = configure_logging(log_stream)
    monkeypatch.setattr(
        composition_module, "configure_logging", lambda stream: test_logger
    )

    def unreadable() -> bytes:
        raise FileNotFoundError(str(tmp_path / "cairn-mcp-tools-v1.json"))

    monkeypatch.setattr(composition_module, "packaged_manifest_bytes", unreadable)

    with pytest.raises(FileNotFoundError):
        build_application(make_config(tmp_path))

    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert records == [
        {
            "event": "runtime_start_failed",
            "failure_code": "contract_unavailable",
            "instance_id": str(INSTANCE_ID),
            "time": records[0]["time"],
        }
    ]
    # I-32: the path the failure named never reaches the log.
    assert str(tmp_path) not in log_stream.getvalue()
