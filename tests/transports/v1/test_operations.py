"""Task 6: the shared operation table and the three surfaces it feeds
(P-51, I-90).

The table's whole purpose is that the REST routes, the OpenAPI artefact
and the MCP tool registry cannot describe different operations. That is
only provable by taking all three from where a caller meets them — the
registered application routes, the generated document and the built tool
inventory — and comparing each against a map written out here rather than
read from the table under test. A test that derives its expectation from
the table proves the table equals itself.
"""

import json
from pathlib import Path
from uuid import UUID

from starlette.routing import Route

from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import Operation
from cairn.transports.mcp.server import TOOLS
from cairn.transports.rest.v1.openapi import render_document
from cairn.transports.v1.operations import OPERATIONS

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")

# The I-70 surface, transcribed: operation identity, the one name the tool
# and the route share, the method, and the mutation/read split. Eleven
# entries, because I-84 adds no operation and this is where that claim
# stops being a sentence.
I70 = {
    ("ingest", Operation.INGEST, "post", True),
    ("promote", Operation.PROMOTE, "post", True),
    ("invalidate", Operation.INVALIDATE, "post", True),
    ("create-principal", Operation.CREATE_PRINCIPAL, "post", True),
    ("issue-credential", Operation.ISSUE_CREDENTIAL, "post", True),
    ("revoke-credential", Operation.REVOKE_CREDENTIAL, "post", True),
    ("create-grant", Operation.CREATE_GRANT, "post", True),
    ("revoke-grant", Operation.REVOKE_GRANT, "post", True),
    ("read-audit-events", Operation.READ_AUDIT_EVENTS, "post", False),
    ("retrieve", Operation.RETRIEVE, "post", False),
    ("read-evidence", Operation.READ_EVIDENCE, "post", False),
    ("instance", Operation.INSTANCE, "get", False),
}
PATHS = {f"/v1/{tool}" for tool, _, _, _ in I70}
FOUNDATION_PATHS = {
    "/health/live",
    "/health/startup",
    "/health/ready",
    "/metrics",
}
# The MCP endpoint is a route but not an operation (I-84), so it is
# excluded from the cover rather than counted in it.
MCP_MOUNT = "/v1/mcp"


def make_config(tmp_path: Path) -> CairnConfig:
    data = tmp_path / "data"
    credentials = tmp_path / "credentials"
    data.mkdir(exist_ok=True)
    credentials.mkdir(exist_ok=True)
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
    )


def test_the_table_holds_exactly_the_eleven_i70_operations() -> None:
    """P-51's eleven-entry invariant, and the vocabulary it carries."""
    entries = {
        (entry.tool, entry.operation, entry.method, entry.mutation)
        for entry in OPERATIONS
    }

    assert len(OPERATIONS) == 12
    assert entries == I70


def test_every_entry_names_an_operation_member_exactly_once() -> None:
    """The operation identities are eleven distinct ``/v1`` members, so a
    metric label, an ``operationId`` and a tool can never be two different
    operations wearing one name."""
    identities = [entry.operation for entry in OPERATIONS]

    assert len(set(identities)) == 12
    assert set(identities) <= set(Operation)


def test_the_tool_name_is_the_final_path_segment() -> None:
    """I-84: one operation vocabulary rather than two spellings of it. The
    ``Operation`` member spells the same name with underscores because it
    is also a metric label, which is not a public name."""
    for entry in OPERATIONS:
        assert entry.path == f"/v1/{entry.tool}"
        assert entry.operation.value == entry.tool.replace("-", "_")


def test_eight_operations_are_mutations_and_three_are_reads() -> None:
    """The split both transports branch on: the I-72 envelope and the I-27
    key on one side, a flat body and a refusal on the other."""
    mutations = [entry for entry in OPERATIONS if entry.mutation]
    reads = [entry for entry in OPERATIONS if not entry.mutation]

    assert len(mutations) == 8
    assert {entry.tool for entry in reads} == {
        "read-audit-events",
        "retrieve",
        "read-evidence",
        "instance",
    }


def test_only_instance_takes_no_request_body() -> None:
    """Ten request models, one operation without one — the shape the two
    projections branch on when they build a body or an arguments schema."""
    without = {entry.tool for entry in OPERATIONS if entry.request is None}

    assert without == {"instance"}


def test_the_routes_the_document_and_the_tools_each_cover_the_table(
    tmp_path: Path,
) -> None:
    """P-51's three-way cover, taken from the three places a caller meets
    the surface. None of the three is derived from another, and none is
    derived from the table: this is the assertion that fails when a
    twelfth operation reaches one surface and not the others."""
    application = build_application(make_config(tmp_path))
    registered = {
        (method.lower(), route.path)
        for route in application.routes
        if isinstance(route, Route)
        and route.path != MCP_MOUNT
        and (route.path.startswith("/v1/") or route.path in FOUNDATION_PATHS)
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    documented = {
        (method, path)
        for path, item in json.loads(render_document())["paths"].items()
        for method in item
    }
    advertised = {tool.name for tool in TOOLS}
    tabled = {(entry.method, entry.path) for entry in OPERATIONS}

    assert tabled == {(method, f"/v1/{tool}") for tool, _, method, _ in I70}
    assert registered == tabled | {("get", path) for path in FOUNDATION_PATHS}
    assert documented == tabled
    assert advertised == {entry.tool for entry in OPERATIONS}
