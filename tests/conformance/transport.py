"""P-59's conformance transport seam.

I-90 requires the one corpus to run against every transport rather than
a copy of it per transport, so a scenario may not name a path, a header
or a status code. This module is the only place in ``tests/conformance``
that knows how an operation reaches Cairn: one method per I-70
operation, eleven of them, and one transport-neutral value returned by
each.

``OperationOutcome`` is everything a scenario is entitled to see — the
I-72 outcome, the stable failure code with the rule identity and field
path where I-72 discloses one, the result body and the receipts. The
I-73 status table is deliberately absent: it is REST's alone (I-88) and
is the subject of ``tests/transports/rest/v1/test_errors.py``, which
asserts a status for every failure code and the ``WWW-Authenticate``
challenge besides.

``text`` is the canonical serialisation of the whole disclosed document,
for the scenarios that assert a string reached no caller. It is
transport-neutral by construction: both surfaces disclose the same I-72
document, so a substring absent from one is absent from the other.
"""

import json
from dataclasses import dataclass
from typing import Protocol

from httpx import AsyncClient
from httpx import Response as HTTPXResponse

from cairn.transports.mcp.server import IDEMPOTENCY_KEY_ARGUMENT
from cairn.transports.v1.operations import OPERATIONS, OperationEntry
from cairn.transports.v1.paths import MCP_MOUNT_PATH
from cairn.transports.v1.wire import (
    AuditReceiptBody,
    MutationReceiptBody,
    SuccessEnvelope,
)

# The transports the corpus runs against. Each entry is one full run of
# the 54 applicable scenarios against its own fresh instance per
# scenario, and one report.
REST = "rest"
MCP = "mcp"
TRANSPORTS: tuple[str, ...] = (REST, MCP)

Body = dict[str, object]

_ENTRY: dict[str, OperationEntry] = {entry.tool: entry for entry in OPERATIONS}

# I-72's success envelope and its two receipts are closed models with no
# optional member, so their exact membership is the check. Read off the
# models rather than copied out of them: a hand-written list here would
# be a second inventory of the contract, and the one that fell behind.
_ENVELOPE_MEMBERS = frozenset(SuccessEnvelope.model_fields)
_MUTATION_RECEIPT_MEMBERS = frozenset(MutationReceiptBody.model_fields)
_AUDIT_RECEIPT_MEMBERS = frozenset(AuditReceiptBody.model_fields)


def _closed(value: object, members: frozenset[str], document: Body) -> Body:
    """An object with exactly the members I-72 fixes for it.

    Present-and-a-dict is not enough: an empty receipt satisfies that and
    tells a scenario nothing, and the surface under test is precisely
    what a conformance harness may not take on trust.
    """
    assert isinstance(value, dict), document
    assert set(value) == members, document
    return value


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """One operation's answer, with nothing transport-shaped in it."""

    # The I-72 document as disclosed, kept whole so ``text`` can answer
    # the absence questions without a second rendering of the same data.
    document: Body
    succeeded: bool
    # The I-72 envelope's outcome — ``committed`` or ``replayed`` — for a
    # mutation that succeeded, and ``None`` for a read or a failure.
    outcome: str | None
    # A mutation's result object, or a read's flat document.
    result: Body | None
    # Both of I-72's receipts: the mutation identity and the audit event
    # the mutation appended. Extracted rather than left in ``document``
    # so a success envelope missing one is a failure here, at the seam,
    # rather than a scenario quietly asserting nothing about it.
    mutation_receipt: Body | None
    audit_receipt: Body | None
    # The I-72 failure object verbatim, correlation identity included.
    failure: Body | None

    @classmethod
    def from_document(cls, document: Body, *, mutation: bool) -> "OperationOutcome":
        failure = document.get("failure")
        if failure is not None:
            assert isinstance(failure, dict), document
            return cls(document, False, None, None, None, None, failure)
        if not mutation:
            return cls(document, True, None, document, None, None, None)
        assert set(document) == _ENVELOPE_MEMBERS, document
        outcome = document["outcome"]
        result = document["result"]
        assert isinstance(outcome, str), document
        assert isinstance(result, dict), document
        return cls(
            document,
            True,
            outcome,
            result,
            _closed(document["mutation_receipt"], _MUTATION_RECEIPT_MEMBERS, document),
            _closed(document["audit_receipt"], _AUDIT_RECEIPT_MEMBERS, document),
            None,
        )

    @property
    def failure_code(self) -> str | None:
        if self.failure is None:
            return None
        code = self.failure["code"]
        assert isinstance(code, str), self.failure
        return code

    @property
    def detail(self) -> Body | None:
        """The licensed ``detail``, absent on the codes I-72 does not
        license one for — so a scenario asserting non-disclosure asserts
        ``is None`` rather than a key's absence."""
        if self.failure is None:
            return None
        detail = self.failure.get("detail")
        if detail is None:
            return None
        assert isinstance(detail, dict), self.failure
        return detail

    @property
    def text(self) -> str:
        return json.dumps(
            self.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


class TransportClient(Protocol):
    """One method per I-70 operation.

    Each takes the operation's arguments as the wire object the caller
    would send, the caller's credential — ``None`` for an unauthenticated
    attempt — and, for the eight mutations, the I-27 idempotency key,
    minted from the instance-wide sequence when the caller does not name
    one. The three reads take no key: both surfaces refuse it.
    """

    async def ingest(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def promote(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def invalidate(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def create_principal(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def issue_credential(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def revoke_credential(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def create_grant(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def revoke_grant(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome: ...

    async def read_audit_events(
        self, body: Body, *, credential: str | None
    ) -> OperationOutcome: ...

    async def retrieve(
        self, body: Body, *, credential: str | None
    ) -> OperationOutcome: ...

    async def instance(self, *, credential: str | None) -> OperationOutcome: ...


class SeamClient:
    """The eleven methods, once, over one transport-specific ``_call``.

    Both implementations inherit them rather than writing them out, so
    the two transports cannot come to offer the corpus a differently
    shaped seam — a method one client accepted an argument on and the
    other silently ignored would be a divergence I-90 forbids, hiding in
    the harness rather than in Cairn.
    """

    def __init__(self) -> None:
        self._issued = 0

    def _next_key(self) -> str:
        """The instance-wide idempotency sequence.

        It belongs to the client rather than to a per-actor helper: a
        counter that restarted whenever a scenario switched actor would
        collide two unrelated requests on one key, and the second would
        answer ``idempotency_conflict``. The scenario would then be
        measuring the harness.
        """
        self._issued += 1
        return f"{self._issued:08x}-cccc-4ccc-8ccc-cccccccccccc"

    async def _call(
        self,
        tool: str,
        body: Body | None,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        raise NotImplementedError

    async def ingest(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("ingest", body, credential, idempotency_key)

    async def promote(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("promote", body, credential, idempotency_key)

    async def invalidate(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("invalidate", body, credential, idempotency_key)

    async def create_principal(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("create-principal", body, credential, idempotency_key)

    async def issue_credential(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("issue-credential", body, credential, idempotency_key)

    async def revoke_credential(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("revoke-credential", body, credential, idempotency_key)

    async def create_grant(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("create-grant", body, credential, idempotency_key)

    async def revoke_grant(
        self,
        body: Body,
        *,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        return await self._call("revoke-grant", body, credential, idempotency_key)

    async def read_audit_events(
        self, body: Body, *, credential: str | None
    ) -> OperationOutcome:
        return await self._call("read-audit-events", body, credential)

    async def retrieve(self, body: Body, *, credential: str | None) -> OperationOutcome:
        return await self._call("retrieve", body, credential)

    async def instance(self, *, credential: str | None) -> OperationOutcome:
        return await self._call("instance", None, credential)


class RestTransportClient(SeamClient):
    """The seam over the REST surface: bearer header, I-27 header on the
    mutations, the operation table's own path and method."""

    def __init__(self, http: AsyncClient) -> None:
        super().__init__()
        self._http = http

    async def _call(
        self,
        tool: str,
        body: Body | None,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        entry = _ENTRY[tool]
        headers = {"Content-Type": "application/json"}
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        if entry.mutation:
            headers["Idempotency-Key"] = idempotency_key or self._next_key()
        if entry.method == "get":
            response = await self._http.get(entry.path, headers=headers)
        else:
            assert body is not None
            response = await self._http.post(
                entry.path, content=json.dumps(body), headers=headers
            )
        document = response.json()
        assert isinstance(document, dict), response.text
        return OperationOutcome.from_document(document, mutation=entry.mutation)


# The two headers every frame carries. ``Accept: application/json`` alone
# is what P-52's JSON-response posture admits: by inspection of
# ``streamable_http.py:451-456`` the SDK requires ``text/event-stream``
# too only when it may answer with a stream, which this endpoint never
# does.
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# One identifier for every frame the corpus sends. Nothing in the corpus
# correlates two outstanding calls — the seam is request-response and
# strictly sequential — and a varying identifier would be one more thing
# a divergence investigation had to rule out.
FRAME_ID = 1


class McpTransportClient(SeamClient):
    """The seam over `/v1/mcp`: one ``tools/call`` frame per operation,
    the I-27 key as an argument (I-85), and the I-72 document dug back
    out of whichever of the endpoint's three answer shapes came back.

    The three shapes are not this client's invention and it may not
    collapse them: a tool result carries an identified operation's
    outcome (P-56), a JSON-RPC error object carries a protocol fault
    Cairn refused before any operation was identified, and a bare
    envelope carries I-88's one HTTP-layer failure, the authentication
    denial. All three disclose the same I-72 document, which is what
    makes one ``OperationOutcome`` answerable from any of them — and what
    makes a difference between the transports a finding about Cairn
    rather than about the harness.
    """

    def __init__(self, http: AsyncClient) -> None:
        super().__init__()
        self._http = http

    async def _call(
        self,
        tool: str,
        body: Body | None,
        credential: str | None,
        idempotency_key: str | None = None,
    ) -> OperationOutcome:
        entry = _ENTRY[tool]
        arguments: Body = dict(body) if body is not None else {}
        if entry.mutation:
            arguments[IDEMPOTENCY_KEY_ARGUMENT] = idempotency_key or self._next_key()
        headers = dict(MCP_HEADERS)
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        response = await self._http.post(
            MCP_MOUNT_PATH,
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": FRAME_ID,
                    "method": "tools/call",
                    "params": {"name": entry.tool, "arguments": arguments},
                }
            ),
            headers=headers,
        )
        return OperationOutcome.from_document(
            mcp_document(response), mutation=entry.mutation
        )


def mcp_document(response: HTTPXResponse) -> Body:
    """The I-72 document inside one `/v1/mcp` answer.

    Every shape assertion here is I-85 or I-88 stated as a check, because
    a harness that unwrapped loosely would report the shapes it tolerated
    as conformant. In particular a failure carries **no**
    ``structuredContent``: its presence would mean the SDK, not Cairn,
    built the result, and the text block would be the SDK's own error
    prose rather than the envelope (P-55).
    """
    payload = response.json()
    assert isinstance(payload, dict), response.text
    if "jsonrpc" not in payload:
        # I-88's HTTP-layer failure: the shared envelope, verbatim, as it
        # is on REST. Anything else at this layer is a defect.
        assert set(payload) == {"failure"}, response.text
        return payload
    error = payload.get("error")
    if error is not None:
        assert isinstance(error, dict), response.text
        data = error["data"]
        assert isinstance(data, dict), response.text
        return data
    result = payload["result"]
    assert isinstance(result, dict), response.text
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1, response.text
    block = content[0]
    assert block["type"] == "text", response.text
    disclosed = json.loads(block["text"])
    assert isinstance(disclosed, dict), response.text
    if result["isError"] is True:
        assert "structuredContent" not in result, response.text
        assert "failure" in disclosed, response.text
        return disclosed
    assert result["isError"] is False, response.text
    structured = result["structuredContent"]
    assert isinstance(structured, dict), response.text
    # I-85 requires the text block to be that same object serialised, so
    # a surface disclosing two different documents in one result fails
    # here rather than at whichever of the two a scenario happened to
    # read.
    assert disclosed == structured, response.text
    return structured


def build_client(transport: str, http: AsyncClient) -> TransportClient:
    """The one place a transport name becomes an implementation."""
    if transport == REST:
        return RestTransportClient(http)
    assert transport == MCP, transport
    return McpTransportClient(http)
