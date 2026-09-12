"""Task 10: the contract artefact, its digest and the proofs P-30 and
P-32 name.

The artefact is checked three ways that cannot all pass by accident: the
render is deterministic, the two committed copies and the digest file
agree with it, and the document's own claims are compared against the
running application and the real canonical audit bytes rather than
against a second transcription of them.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from starlette.routing import Route

import cairn.runtime.composition as composition_module
from cairn import __version__
from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import SourceType
from cairn.catalogue.audit import (
    ActionKind,
    AuditDraft,
    AuditEvent,
    ChainKind,
    Classification,
    ClassificationTransition,
    Outcome,
    Scope,
    ScopeSegment,
    TrustClass,
    TrustTransition,
    canonical_audit_bytes,
)
from cairn.catalogue.transactions import FailureCode, RetryClass
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig
from cairn.runtime.logging import Operation, configure_logging
from cairn.transports.rest.v1.errors import STATUS_BY_FAILURE_CODE
from cairn.transports.rest.v1.openapi import (
    CONTRACT_VERSION,
    OPENAPI_VERSION,
    PACKAGED_ARTEFACT,
    packaged_contract_bytes,
    render_document,
)
from cairn.transports.v1.wire import CONTRACT_IDENTITY, WIRE_RULES

# tests/transports/rest/v1/test_openapi.py -> repository root is four
# parents up (v1 -> rest -> transports -> tests -> root); P-50's Task 1
# move added the "rest" directory level, so this was parents[3] before it.
REPOSITORY_ROOT = Path(__file__).parents[4]
ARTEFACT = REPOSITORY_ROOT / "contracts" / "cairn-openapi-v1.json"
DIGEST_FILE = ARTEFACT.with_suffix(".json.sha256")
PACKAGED = REPOSITORY_ROOT / "src" / "cairn" / PACKAGED_ARTEFACT

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)

# The I-70 map, written out rather than imported from the generator's own
# route table: a test that reads the table it is checking proves only that
# the table equals itself.
ROUTE_MAP = {
    ("post", "/v1/ingest"),
    ("post", "/v1/promote"),
    ("post", "/v1/invalidate"),
    ("post", "/v1/create-principal"),
    ("post", "/v1/issue-credential"),
    ("post", "/v1/revoke-credential"),
    ("post", "/v1/create-grant"),
    ("post", "/v1/revoke-grant"),
    ("post", "/v1/read-audit-events"),
    ("post", "/v1/retrieve"),
    ("get", "/v1/instance"),
}
MUTATION_PATHS = {path for method, path in ROUTE_MAP} - {
    "/v1/read-audit-events",
    "/v1/retrieve",
    "/v1/instance",
}
# Written out for the same reason as ROUTE_MAP above: deriving these by
# re-implementing the generator's naming convention would agree with a
# misnamed model rather than catching it.
SUCCESS_SCHEMAS = {
    "/v1/ingest": "IngestResultEnvelope",
    "/v1/promote": "PromoteResultEnvelope",
    "/v1/invalidate": "InvalidateResultEnvelope",
    "/v1/create-principal": "CreatePrincipalResultEnvelope",
    "/v1/issue-credential": "IssueCredentialResultEnvelope",
    "/v1/revoke-credential": "RevokeCredentialResultEnvelope",
    "/v1/create-grant": "CreateGrantResultEnvelope",
    "/v1/revoke-grant": "RevokeGrantResultEnvelope",
    "/v1/read-audit-events": "ReadAuditEventsResult",
    "/v1/retrieve": "RetrieveResult",
    "/v1/instance": "InstanceResult",
}
FOUNDATION_PATHS = {
    "/health/live",
    "/health/startup",
    "/health/ready",
    "/metrics",
}


def document() -> Any:
    """The committed artefact, parsed. ``Any`` for the same reason the
    sibling route tests read ``response.json()`` that way: this is
    arbitrary JSON being asserted against, not a typed value."""
    return json.loads(ARTEFACT.read_text(encoding="utf-8"))


def schemas() -> Any:
    return document()["components"]["schemas"]


def make_config(data_path: Path) -> CairnConfig:
    data = data_path / "data"
    credentials = data_path / "credentials"
    data.mkdir(exist_ok=True)
    credentials.mkdir(exist_ok=True)
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
    )


def operations(path_item: Any) -> Any:
    method, operation = next(iter(path_item.items()))
    assert method in {"get", "post"}
    return operation


def test_generation_is_deterministic() -> None:
    """I-76's first requirement: nothing in the document varies between
    two renders, so a regeneration can only differ when the models did."""
    assert render_document() == render_document()


def test_the_committed_artefact_is_the_rendered_document() -> None:
    """P-30: the committed document is never hand-maintained. This is the
    same comparison ``make check``'s dirty-diff gate makes, held here as
    well so a failure names the artefact rather than the git index."""
    assert ARTEFACT.read_text(encoding="utf-8") == render_document()


def test_the_packaged_artefact_is_byte_identical_to_the_repository_copy() -> None:
    """P-30: the two copies exist because a wheel cannot reach outside the
    package and a container has no repository. Neither may drift."""
    assert PACKAGED.read_bytes() == ARTEFACT.read_bytes()
    assert packaged_contract_bytes() == ARTEFACT.read_bytes()


def test_the_committed_digest_names_the_artefact_in_sha256sum_format() -> None:
    expected = hashlib.sha256(ARTEFACT.read_bytes()).hexdigest()

    assert DIGEST_FILE.read_text(encoding="utf-8") == (f"{expected}  {ARTEFACT.name}\n")


def test_the_document_carries_no_environment_derived_content() -> None:
    """I-76: no timestamps, hostnames or environment-derived content. The
    product version is the live case — it comes from ``git describe``, so
    embedding it would make the artefact dirty on every commit — and
    ``info.version`` carries the contract's own version instead."""
    text = ARTEFACT.read_text(encoding="utf-8")
    info = document()["info"]

    assert __version__ not in text
    assert str(REPOSITORY_ROOT) not in text
    assert info["version"] == CONTRACT_VERSION
    assert info["summary"] == f"The {CONTRACT_IDENTITY} contract."
    assert document()["openapi"] == OPENAPI_VERSION
    # No "servers": a server URL is environment by definition.
    assert "servers" not in document()


def test_the_documented_routes_are_exactly_the_i70_map() -> None:
    paths = document()["paths"]

    documented = {(method, path) for path, item in paths.items() for method in item}

    assert documented == ROUTE_MAP


def test_the_documented_routes_are_exactly_the_applications_v1_routes(
    tmp_path: Path,
) -> None:
    """The frozen v1 inventory and foundation routes remain unchanged.

    The separately versioned memory namespace has its own contract test.
    Neither v1 side is derived from the other.
    """
    application = build_application(make_config(tmp_path))
    registered = {
        (method.lower(), route.path)
        for route in application.routes
        if isinstance(route, Route)
        if not route.path.startswith("/memory/v1/")
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    paths = document()["paths"]

    assert registered == ROUTE_MAP | {("get", path) for path in FOUNDATION_PATHS}
    assert set(paths) == {path for _, path in ROUTE_MAP}


def test_every_operation_id_is_an_operation_member() -> None:
    """P-32: the ``Operation`` enum has one member per ``/v1`` route, and
    the document names the same vocabulary, so a route's artefact entry
    and its metric label can never disagree."""
    paths = document()["paths"]

    documented = {operations(item)["operationId"] for item in paths.values()}
    members = {member.value for member in Operation}

    assert documented <= members
    assert len(documented) == len(paths)


def test_the_mcp_digest_is_optional_and_not_nullable() -> None:
    """I-89's field, in I-89's shape: optional — I-29 licenses an added
    response field, so a consumer of the previous document is unaffected —
    but a plain string, never null and defaulting to nothing. The first
    publication was ``string | null`` with a ``default: null``, which
    licenses an explicitly null digest no instance can honestly serve: an
    instance that cannot read its packaged manifest refuses to start
    (Val's gate review of ``5b6a5ec``, finding 5)."""
    instance = schemas()["InstanceResult"]
    digest = instance["properties"]["mcp_contract_digest"]

    assert digest == {"title": "Mcp Contract Digest", "type": "string"}
    assert "mcp_contract_digest" not in instance["required"]
    assert "contract_digest" in instance["required"]


def test_only_the_mutation_routes_require_the_idempotency_key() -> None:
    """I-27 and P-28 at the boundary: required on the eight mutations,
    absent on the two reads, which forbid it outright."""
    paths = document()["paths"]

    for path, item in paths.items():
        parameters = operations(item)["parameters"]
        named = {parameter["name"]: parameter["required"] for parameter in parameters}
        assert named["X-Correlation-ID"] is False
        assert named.get("Idempotency-Key") == (
            True if path in MUTATION_PATHS else None
        )
        # A parameter list cannot say "forbidden", so an absent
        # Idempotency-Key would read as "not used" rather than "refused".
        # The two reads state the prohibition in prose instead.
        description = operations(item).get("description", "")
        if path in MUTATION_PATHS:
            assert "Idempotency-Key" not in description
        else:
            assert "Idempotency-Key" in description
            assert "invalid_request" in description


def test_every_route_publishes_the_whole_failure_table() -> None:
    """I-73 never varies by operation, so neither does the document. The
    two admission refinements ride only where a body is admitted."""
    paths = document()["paths"]
    table = {str(status) for status in STATUS_BY_FAILURE_CODE.values()}

    for path, item in paths.items():
        operation = operations(item)
        responses = operation["responses"]
        admission = {"413", "415"} if "requestBody" in operation else set()

        assert set(responses) == {"200"} | table | admission
        assert set(responses["401"]["headers"]) == {
            "WWW-Authenticate",
            "X-Correlation-ID",
        }
        assert set(responses["503"]["headers"]) == {"Retry-After", "X-Correlation-ID"}
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"] == (
            f"#/components/schemas/{SUCCESS_SCHEMAS[path]}"
        )


def test_every_reference_resolves() -> None:
    """A dangling ``$ref`` makes the document unusable to a generator, and
    the schema renaming is where one would come from."""
    references = set(_references(document()))
    defined = {f"#/components/schemas/{name}" for name in schemas()}

    assert references
    assert references <= defined


def _references(node: object) -> list[str]:
    if type(node) is dict:
        found = [str(value) for key, value in node.items() if key == "$ref"]
        for value in node.values():
            found.extend(_references(value))
        return found
    if type(node) is list:
        return [reference for item in node for reference in _references(item)]
    return []


def test_the_failure_body_publishes_the_closed_vocabularies() -> None:
    """I-76 names the closed failure-code enumeration explicitly; the
    retry classes and the adapter's own wire rules travel with it."""
    failure = schemas()["FailureBody"]["properties"]

    assert failure["code"]["enum"] == [member.value for member in FailureCode]
    assert failure["retry"]["enum"] == [member.value for member in RetryClass]
    assert schemas()["InvalidRequestDetail"]["properties"]["rule"]["enum"] == sorted(
        WIRE_RULES
    )
    # I-31 versions the secret policy apart from the API, so its rule
    # vocabulary is deliberately not pinned into this contract.
    assert "enum" not in schemas()["SecretRejectedDetail"]["properties"]["rule"]


def test_the_published_vocabularies_are_the_closed_vocabularies() -> None:
    """Every enumeration in the document is sourced from the vocabulary it
    describes, so this is a parity check rather than a transcription."""
    ingest = schemas()["IngestRequest"]["properties"]
    grant = schemas()["GrantBody"]["properties"]

    assert ingest["classification"]["enum"] == [c.value for c in Classification]
    assert ingest["source_type"]["enum"] == [s.value for s in SourceType]
    assert ingest["requested_trust"]["enum"] == [t.value for t in TrustClass]
    assert grant["operations"]["items"]["enum"] == [g.value for g in GrantOperation]
    assert grant["read_clearance"]["enum"] == [c.value for c in Classification]
    assert grant["write_classifications"]["items"]["enum"] == [
        c.value for c in Classification
    ]
    assert schemas()["CreatePrincipalRequest"]["properties"]["kind"]["enum"] == [
        k.value for k in PrincipalKind
    ]
    assert schemas()["AuditReceiptBody"]["properties"]["chain_kind"]["enum"] == [
        k.value for k in ChainKind
    ]


def test_every_request_schema_forbids_unknown_fields() -> None:
    """P-33's ``extra='forbid'`` reaching the published contract, so a
    generated client cannot offer a field Cairn will reject."""
    request_names = [name for name in schemas() if name.endswith("Request")]

    assert len(request_names) == 10
    for name in request_names:
        assert schemas()[name]["additionalProperties"] is False


def test_the_audit_event_schema_matches_the_canonical_document() -> None:
    """The audit-event schema is hand-authored because the adapter passes
    the authoritative bytes through verbatim rather than re-modelling
    them. Its fidelity is held here, against a real event's canonical
    bytes — the only thing that can catch the schema drifting from the
    document it claims to describe."""
    event_schema = schemas()["AuditEvent"]
    canonical = json.loads(canonical_audit_bytes(_event()))

    assert set(event_schema["properties"]) == set(canonical)
    assert event_schema["additionalProperties"] is False
    assert event_schema["properties"]["schema"]["const"] == canonical["schema"]
    assert event_schema["properties"]["action_kind"]["enum"] == [
        kind.value for kind in ActionKind
    ]
    assert event_schema["properties"]["outcome"]["enum"] == [
        outcome.value for outcome in Outcome
    ]
    # I-54's every-key-present guarantee, stated the way JSON Schema
    # states it. The inverted reading — "no optional members, so nothing
    # to require" — shipped an all-optional object that validated `{}`,
    # and was caught by the Task 10 correctness review.
    assert event_schema["required"] == sorted(canonical)


def test_the_audit_event_schema_reads_the_documents_own_null_form() -> None:
    """A value that does not apply is null rather than absent, and the
    schema says so through a 2020-12 type union rather than the 3.0
    ``nullable`` keyword OpenAPI 3.1 dropped — one spelling for scalars
    and objects alike, so a reader never has to learn two."""
    properties = schemas()["AuditEvent"]["properties"]
    canonical = json.loads(canonical_audit_bytes(_event()))

    assert canonical["mutation_id"] is None
    assert canonical["source_scope"] is None
    assert properties["mutation_id"]["type"] == ["string", "null"]
    assert properties["source_scope"]["type"] == ["object", "null"]
    assert properties["source_scope"]["properties"]["segments"]["type"] == "array"
    assert "nullable" not in json.dumps(schemas())
    assert "oneOf" not in json.dumps(schemas())


def test_an_instance_without_its_packaged_contract_refuses_to_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package built without its own contract is a broken build, not a
    runtime condition, so it fails like the lease and catalogue failures
    beside it rather than serving a null digest (Operator, 7 August 2026)."""
    log_stream = StringIO()
    test_logger = configure_logging(log_stream)
    monkeypatch.setattr(
        composition_module, "configure_logging", lambda stream: test_logger
    )

    def unreadable() -> bytes:
        raise FileNotFoundError(str(tmp_path / "cairn-openapi-v1.json"))

    monkeypatch.setattr(composition_module, "packaged_contract_bytes", unreadable)

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


def test_real_audit_documents_satisfy_the_published_schema() -> None:
    """The property-set check above catches a renamed or dropped field but
    not a lying one — a nullability, enum or pattern the real bytes do not
    honour. This validates every value of a fully-null and a fully-
    populated event against what the artefact declares, which is the only
    check that would catch the schema being confidently wrong about a
    type it was hand-authored to describe.
    """
    event_schema = schemas()["AuditEvent"]

    for event in (_event(), _populated_event()):
        document = json.loads(canonical_audit_bytes(event))

        assert set(document) == set(event_schema["properties"])
        for name, value in document.items():
            _satisfies(value, event_schema["properties"][name], name)


def _satisfies(value: object, spec: Any, where: str) -> None:
    declared = spec.get("type")
    allowed = {declared} if type(declared) is str else set(declared or [])
    actual = _JSON_TYPES[type(value)]

    assert actual in allowed, f"{where}: {actual} not in {sorted(allowed)}"
    if "enum" in spec:
        assert value in spec["enum"], f"{where}: {value!r} not enumerated"
    if "const" in spec:
        assert value == spec["const"], f"{where}: {value!r} is not the const"
    if "pattern" in spec and type(value) is str:
        assert re.fullmatch(spec["pattern"].strip("^$"), value), f"{where}: pattern"
    if "minimum" in spec and type(value) is int:
        assert value >= spec["minimum"], f"{where}: below minimum"
    if type(value) is list:
        for index, item in enumerate(value):
            _satisfies(item, spec["items"], f"{where}[{index}]")
    if type(value) is dict:
        assert set(value) == set(spec["properties"]), f"{where}: property set"
        for name, item in value.items():
            _satisfies(item, spec["properties"][name], f"{where}.{name}")


_JSON_TYPES = {
    str: "string",
    bool: "boolean",
    int: "integer",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _populated_event() -> AuditEvent:
    """The opposite extreme: a realm-chain promote with every nullable
    member carrying a value, so the schema is checked against populated
    scopes, both transitions and all three digests as well as nulls."""
    scope = Scope(
        realm="acme",
        segments=(ScopeSegment(kind="repository", identifier="acme-repo"),),
    )
    return AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.REALM,
            chain_identity="acme",
            principal_id=UUID("66666666-6666-4666-8666-666666666666"),
            credential_verifier_id=UUID("77777777-7777-4777-8777-777777777777"),
            grant_id=UUID("88888888-8888-4888-8888-888888888888"),
            action_kind=ActionKind.DATA,
            action_code="promote",
            source_scope=scope,
            requested_scope=scope,
            target_scope=scope,
            outcome=Outcome.ALLOW,
            reason_code="promote_committed",
            affected_assertion_ids=(UUID("99999999-9999-4999-8999-999999999999"),),
            affected_fact_ids=(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),),
            affected_evidence_ids=(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),),
            affected_grant_ids=(UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),),
            classification_transition=ClassificationTransition(
                previous=Classification.PUBLIC, current=Classification.INTERNAL
            ),
            trust_transition=TrustTransition(
                previous=TrustClass.CANDIDATE, current=TrustClass.VALIDATED
            ),
            evidence_reference=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            evidence_digest=bytes(range(32)),
            correlation_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            idempotency_key=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            mutation_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
            command_digest=bytes(range(32)),
            replay_of_mutation_id=UUID("12121212-1212-4121-8121-121212121212"),
            safe_request_fingerprint=bytes(range(32)),
        ),
        sequence=2,
        event_id=UUID("13131313-1313-4131-8131-131313131313"),
        recorded_at=NOW,
        previous_hash=bytes(range(32)),
    )


def _event() -> AuditEvent:
    """A minimal real event: an instance-chain denial, which is the shape
    carrying the fewest populated fields, so every nullable member of the
    canonical document is exercised as null."""
    return AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.INSTANCE,
            chain_identity=str(INSTANCE_ID),
            principal_id=None,
            credential_verifier_id=None,
            grant_id=None,
            action_kind=ActionKind.SYSTEM,
            action_code="instance",
            source_scope=None,
            requested_scope=None,
            target_scope=None,
            outcome=Outcome.DENY,
            reason_code="authentication_failed",
            affected_assertion_ids=(),
            affected_fact_ids=(),
            affected_evidence_ids=(),
            affected_grant_ids=(),
            classification_transition=None,
            trust_transition=None,
            evidence_reference=None,
            evidence_digest=None,
            correlation_id=UUID("44444444-4444-4444-8444-444444444444"),
            idempotency_key=None,
            mutation_id=None,
            command_digest=None,
            replay_of_mutation_id=None,
            safe_request_fingerprint=None,
        ),
        sequence=1,
        event_id=UUID("55555555-5555-4555-8555-555555555555"),
        recorded_at=NOW,
        previous_hash=bytes(32),
    )
