"""The single `/v1` operation table (P-51).

One entry per I-70 operation, owned here because three surfaces describe
the same eleven things — the REST routes and their OpenAPI artefact, the
MCP tool registry (I-85) and the generated manifest (I-89) — and three
independently maintained inventories is how they would quietly diverge.

The table is descriptive, not executable: it names each operation, the
models that bound it, and the two properties every projection branches
on. It holds no handler, no status code and no transport mechanics; the
I-73 status table is REST's alone (I-88). ``summary`` is one
transport-neutral sentence per operation, so neither surface authors its
own wording.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from cairn.runtime.logging import Operation
from cairn.transports.v1.requests import (
    CreateGrantRequest,
    CreatePrincipalRequest,
    IngestRequest,
    InvalidateRequest,
    IssueCredentialRequest,
    PromoteRequest,
    ReadAuditEventsRequest,
    RetrieveRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
)
from cairn.transports.v1.responses import (
    CreateGrantResult,
    CreatePrincipalResult,
    IngestResult,
    InstanceResult,
    InvalidateResult,
    IssueCredentialResult,
    PromoteResult,
    ReadAuditEventsResult,
    RetrieveResult,
    RevokeCredentialResult,
    RevokeGrantResult,
)
from cairn.transports.v1.wire import SuccessEnvelope, WireModel


@dataclass(frozen=True, slots=True)
class OperationEntry:
    """One I-70 operation and everything both transports agree about."""

    operation: Operation
    # The tool name (I-84) and the route's final path segment, deliberately
    # one string: one operation vocabulary rather than two spellings of it.
    # ``Operation`` uses underscores because it is also a metric label.
    tool: str
    method: str
    path: str
    summary: str
    request: type[WireModel] | None
    result: type[WireModel]
    # Written out rather than computed: ``SuccessEnvelope[entry.result]``
    # is a type application mypy cannot follow through a variable. It is
    # the OpenAPI 200 body and the MCP ``outputSchema`` alike.
    success: type[BaseModel]
    # The eight mutations answer inside the I-72 envelope and take the
    # I-27 key — a header over REST (P-28), an argument over MCP (I-85).
    # The three reads answer flat and refuse it on both surfaces.
    mutation: bool


OPERATIONS: tuple[OperationEntry, ...] = (
    OperationEntry(
        Operation.INGEST,
        "ingest",
        "post",
        "/v1/ingest",
        "Ingest an assertion into a scope as candidate memory.",
        IngestRequest,
        IngestResult,
        SuccessEnvelope[IngestResult],
        True,
    ),
    OperationEntry(
        Operation.PROMOTE,
        "promote",
        "post",
        "/v1/promote",
        "Promote facts to validated trust against an evidence reference.",
        PromoteRequest,
        PromoteResult,
        SuccessEnvelope[PromoteResult],
        True,
    ),
    OperationEntry(
        Operation.INVALIDATE,
        "invalidate",
        "post",
        "/v1/invalidate",
        "Invalidate facts, optionally naming what supersedes them.",
        InvalidateRequest,
        InvalidateResult,
        SuccessEnvelope[InvalidateResult],
        True,
    ),
    OperationEntry(
        Operation.CREATE_PRINCIPAL,
        "create-principal",
        "post",
        "/v1/create-principal",
        "Create a principal in a realm.",
        CreatePrincipalRequest,
        CreatePrincipalResult,
        SuccessEnvelope[CreatePrincipalResult],
        True,
    ),
    OperationEntry(
        Operation.ISSUE_CREDENTIAL,
        "issue-credential",
        "post",
        "/v1/issue-credential",
        "Issue a credential, returning its plaintext exactly once.",
        IssueCredentialRequest,
        IssueCredentialResult,
        SuccessEnvelope[IssueCredentialResult],
        True,
    ),
    OperationEntry(
        Operation.REVOKE_CREDENTIAL,
        "revoke-credential",
        "post",
        "/v1/revoke-credential",
        "Revoke a credential.",
        RevokeCredentialRequest,
        RevokeCredentialResult,
        SuccessEnvelope[RevokeCredentialResult],
        True,
    ),
    OperationEntry(
        Operation.CREATE_GRANT,
        "create-grant",
        "post",
        "/v1/create-grant",
        "Create a grant over a scope prefix.",
        CreateGrantRequest,
        CreateGrantResult,
        SuccessEnvelope[CreateGrantResult],
        True,
    ),
    OperationEntry(
        Operation.REVOKE_GRANT,
        "revoke-grant",
        "post",
        "/v1/revoke-grant",
        "Revoke a grant, including a self-issuer grant.",
        RevokeGrantRequest,
        RevokeGrantResult,
        SuccessEnvelope[RevokeGrantResult],
        True,
    ),
    OperationEntry(
        Operation.READ_AUDIT_EVENTS,
        "read-audit-events",
        "post",
        "/v1/read-audit-events",
        "Read audit events under a scope prefix.",
        ReadAuditEventsRequest,
        ReadAuditEventsResult,
        ReadAuditEventsResult,
        False,
    ),
    OperationEntry(
        Operation.RETRIEVE,
        "retrieve",
        "post",
        "/v1/retrieve",
        "Retrieve facts reconciled against the catalogue.",
        RetrieveRequest,
        RetrieveResult,
        RetrieveResult,
        False,
    ),
    OperationEntry(
        Operation.INSTANCE,
        "instance",
        "get",
        "/v1/instance",
        "Report the contract identity, product version and contract digest.",
        None,
        InstanceResult,
        InstanceResult,
        False,
    ),
)
