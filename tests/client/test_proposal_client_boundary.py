"""Hostile responses cannot manufacture a proposal or publication receipt."""

import json
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from test_suggestion_client_boundary import AUTHOR, INSTANCE, diagnosis

from cairn.authority.proposal_codec import digest
from cairn.authority.proposal_types import ProposeMemory
from cairn.catalogue.audit import Classification, Scope
from cairn.client.errors import MemoryOperationFailure
from cairn.client.memory import MemoryClient

PID = UUID("22222222-2222-5222-8222-222222222222")
SOURCE = UUID("44444444-4444-4444-8444-444444444444")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def snapshot() -> dict[str, Any]:
    return dict(
        proposal_id=str(PID),
        scope={"realm": "acme", "segments": []},
        source_fact_id=str(SOURCE),
        source_trust="candidate",
        source_invalidated=False,
        target_scope={"realm": "acme", "segments": []},
        classification="internal",
        reason="Reuse",
        proposed_by=AUTHOR,
        recorded_at="2026-09-09T12:00:00.000000Z",
        mutation_id=str(uuid4()),
        state="pending",
        decision=None,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "delta",
    [
        {"proposal_id": "55555555-5555-4555-8555-555555555555"},
        {"scope": {"realm": "other", "segments": []}},
        {"source_invalidated": 0},
        {"reason": "é" * 2049},
        {"state": "accepted"},
        {"decision": {}},
        {"recorded_at": "2026-09-09T12:00:00Z"},
        {"proposed_by": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
        {"source_trust": "canonical"},
        {"extra": "private-input"},
    ],
)
async def test_invalid_snapshot_is_safe(delta: dict[str, Any]) -> None:
    document = {**snapshot(), **delta}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else document
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        client = MemoryClient(
            http,
            scope=Scope("acme", ()),
            classification=Classification.INTERNAL,
            expected_instance_id=INSTANCE,
        )
        with pytest.raises(MemoryOperationFailure) as error:
            await client.proposal_read(PID)
        assert error.value.failure.code == "invalid_response"
        assert "private-input" not in str(error.value)


def receipt() -> dict[str, Any]:
    return {
        "outcome": "committed",
        "result": {"proposal_id": str(PID)},
        "mutation_receipt": {
            "mutation_id": str(uuid4()),
            "command_digest": digest(
                ProposeMemory(
                    Scope("acme", ()), PID, SOURCE, Scope("acme", ()), "Reuse"
                )
            ).hex(),
        },
        "audit_receipt": {
            "event_id": str(uuid4()),
            "chain_kind": "realm",
            "chain_identity": "acme",
            "sequence": 1,
            "recorded_at": "2026-09-09T12:00:00.000000Z",
            "event_hash": "a" * 64,
        },
    }


def make_client(
    http: httpx.AsyncClient, expected: UUID | None = INSTANCE
) -> MemoryClient:
    return MemoryClient(
        http,
        scope=Scope("acme", ()),
        classification=Classification.INTERNAL,
        expected_instance_id=expected,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["propose", "proposal_accept", "proposal_reject"])
@pytest.mark.parametrize(
    "key",
    [
        *(UUID(f"22222222-2222-5222-{n}222-222222222222") for n in "01234567cdef"),
        UUID(int=0),
        UUID(int=(1 << 128) - 1),
        "not-a-typed-uuid",
        None,
    ],
)
async def test_i27_client_key_refusal_precedes_all_outbound_work(
    method: str, key: Any
) -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid key reached diagnose, pre-read or mutation")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(never), base_url="http://127.0.0.1"
    ) as http:
        kwargs: dict[str, Any] = {"idempotency_key": key}
        if method == "propose":
            kwargs.update(
                source_fact_id=SOURCE, target_scope=Scope("acme", ()), reason="Reuse"
            )
        elif method == "proposal_accept":
            kwargs.update(
                evidence_id=SOURCE, target_classification=Classification.INTERNAL
            )
        else:
            kwargs.update(reason="No")
        with pytest.raises(MemoryOperationFailure) as error:
            await getattr(make_client(http), method)(PID, **kwargs)
        assert error.value.failure.code == "invalid_request"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "foreign_proposal",
        "foreign_digest",
        "extra_result",
        "extra_envelope",
        "boolean_sequence",
        "wrong_realm",
        "bad_time",
        "bad_mutation",
        "bad_hash",
        "bad_outcome",
    ],
)
async def test_proposal_receipt_binding(case: str) -> None:
    packet = receipt()
    if case == "foreign_proposal":
        packet["result"]["proposal_id"] = str(uuid4())
    if case == "foreign_digest":
        packet["mutation_receipt"]["command_digest"] = "0" * 64
    if case == "extra_result":
        packet["result"]["fact_ids"] = [str(SOURCE)]
    if case == "extra_envelope":
        packet["published"] = True
    if case == "boolean_sequence":
        packet["audit_receipt"]["sequence"] = True
    if case == "wrong_realm":
        packet["audit_receipt"]["chain_identity"] = "other"
    if case == "bad_time":
        packet["audit_receipt"]["recorded_at"] = "private-input"
    if case == "bad_mutation":
        packet["mutation_receipt"]["mutation_id"] = "private-input"
    if case == "bad_hash":
        packet["audit_receipt"]["event_hash"] = "G" * 64
    if case == "bad_outcome":
        packet["outcome"] = "accepted"
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await make_client(http).propose(
                PID,
                source_fact_id=SOURCE,
                target_scope=Scope("acme", ()),
                reason="Reuse",
                idempotency_key=uuid4(),
            )
        assert error.value.failure.code == "invalid_response"
        assert calls == ["/memory/v1/diagnose", "/memory/v1/propose"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "evidence,published",
    [
        (None, None),
        ("55555555-5555-4555-8555-555555555555", None),
        (None, "66666666-6666-4666-8666-666666666666"),
        (
            "55555555-5555-4555-8555-555555555555",
            "66666666-6666-4666-8666-666666666666",
        ),
    ],
    ids=["both-hidden", "evidence-readable", "publication-readable", "both-readable"],
)
async def test_hidden_decision_references_are_nullable_and_detached(
    evidence: str | None, published: str | None
) -> None:
    packet = snapshot()
    packet.update(
        state="accepted",
        decision={
            "state": "accepted",
            "decided_by": AUTHOR,
            "recorded_at": packet["recorded_at"],
            "mutation_id": str(uuid4()),
            "reason": None,
            "evidence_id": evidence,
            "promoted_fact_id": published,
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        result = await make_client(http).proposal_read(PID)
        assert result.decision is not None
        assert result.decision.evidence_id == (
            None if evidence is None else UUID(evidence)
        )
        assert result.decision.promoted_fact_id == (
            None if published is None else UUID(published)
        )
        packet["reason"] = "changed"
        assert result.reason == "Reuse"
        with pytest.raises(FrozenInstanceError):
            result.reason = "changed"  # type: ignore[misc]


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "compression",
        "duplicate",
        "redirect",
        "length_negative",
        "length_float",
        "length_unicode",
        "length_duplicate",
        "length_excess",
        "length_short",
        "length_long",
        "stream_excess",
        "error_excess",
        "error_private",
        "transport",
    ],
)
async def test_strict_stream_boundary_and_safe_errors(case: str) -> None:
    from cairn.client.proposal_validation import PROPOSAL_ITEM_BYTES

    payload = json.dumps(snapshot()).encode()
    headers: list[tuple[str, str]] = []
    status = 200
    chunks = [payload]
    if case == "compression":
        headers = [("Content-Encoding", "gzip")]
    if case == "duplicate":
        chunks = [b'{"proposal_id":"a","proposal_id":"b"}']
    if case == "redirect":
        status, headers = 307, [("Location", "http://127.0.0.1/private")]
    if case == "length_negative":
        headers = [("Content-Length", "-1")]
    if case == "length_float":
        headers = [("Content-Length", "1.0")]
    if case == "length_unicode":
        headers = [("Content-Length", "x")]
    if case == "length_duplicate":
        headers = [
            ("Content-Length", str(len(payload))),
            ("Content-Length", str(len(payload))),
        ]
    if case == "length_excess":
        headers = [("Content-Length", str(PROPOSAL_ITEM_BYTES + 1))]
    if case == "length_short":
        headers = [("Content-Length", str(len(payload) - 1))]
    if case == "length_long":
        headers = [("Content-Length", str(len(payload) + 1))]
    if case == "stream_excess":
        chunks = [b" " * PROPOSAL_ITEM_BYTES, b" ", b"never consumed"]
    if case == "error_excess":
        status, chunks = 500, [b" " * 16384, b" ", b"never consumed"]
    if case == "error_private":
        status, chunks = (
            500,
            [b'{"failure":{"code":"private-input","message":"private-input"}}'],
        )
    stream = Stream(chunks)
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("diagnose"):
            return httpx.Response(200, json=diagnosis())
        if case == "transport":
            raise httpx.ReadError("private-input")
        return httpx.Response(status, headers=headers, stream=stream)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await make_client(http).proposal_read(PID)
        assert "private-input" not in repr(error.value.failure)
        assert calls == ["/memory/v1/diagnose", "/memory/v1/proposal-read"]
        if case in {"compression", "length_excess", "length_duplicate"}:
            assert stream.consumed == 0
        if case in {"stream_excess", "error_excess"}:
            assert stream.consumed == 2


@pytest.mark.anyio
async def test_large_legal_page_fits_derived_cap_and_exact_limit() -> None:
    from cairn.client.proposal_validation import PROPOSAL_PAGE_BYTES

    scope = {
        "realm": "a" * 63,
        "segments": [{"kind": "a" * 63, "identifier": "x" * 255} for _ in range(16)],
    }
    items = []
    for i in range(100):
        item = snapshot()
        item.update(
            proposal_id=str(UUID(int=i)),
            scope=scope,
            target_scope=scope,
            reason="\x01" * 4096,
            state="rejected",
            decision={
                "state": "rejected",
                "decided_by": AUTHOR,
                "recorded_at": item["recorded_at"],
                "mutation_id": str(uuid4()),
                "reason": "\x01" * 4096,
                "evidence_id": None,
                "promoted_fact_id": None,
            },
        )
        items.append(item)
    packet = {"items": items, "next_cursor": None}
    encoded = json.dumps(packet, ensure_ascii=True).encode()
    assert 5_000_000 < len(encoded) < PROPOSAL_PAGE_BYTES
    diag = diagnosis()
    diag["scope"] = scope

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("diagnose"):
            return httpx.Response(200, json=diag)
        return httpx.Response(
            200, content=encoded + b" " * (PROPOSAL_PAGE_BYTES - len(encoded))
        )

    from cairn.catalogue.audit import ScopeSegment

    actual_scope = Scope(
        "a" * 63, tuple(ScopeSegment("a" * 63, "x" * 255) for _ in range(16))
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        client = MemoryClient(
            http,
            scope=actual_scope,
            classification=Classification.INTERNAL,
            expected_instance_id=INSTANCE,
        )
        result = await client.proposal_list(limit=100)
        assert len(result.items) == 100 and result.items[-1].reason == "\x01" * 4096


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "source",
        "evidence",
        "derived",
        "extra_pair",
        "proposal_receipt",
        "legacy_digest",
        "other_proposal_digest",
        "reason_digest",
        "scope_digest",
    ],
)
async def test_acceptance_rejects_foreign_publication_and_operation(case: str) -> None:
    from cairn.authority.mutations import (
        PromoteFacts,
        _promote_digest,
        _proposal_accept_digest,
    )

    known = snapshot()
    evidence = uuid4()
    packet = receipt()
    command = PromoteFacts(
        (SOURCE,), evidence, Scope("acme", ()), Classification.INTERNAL, "Reuse"
    )
    packet["mutation_receipt"]["command_digest"] = _proposal_accept_digest(
        command, PID
    ).hex()
    packet["result"] = {
        "promotions": [
            {"source_fact_id": str(SOURCE), "derived_fact_id": str(uuid4())}
        ],
        "evidence_id": str(evidence),
    }
    if case == "source":
        packet["result"]["promotions"][0]["source_fact_id"] = str(uuid4())
    if case == "evidence":
        packet["result"]["evidence_id"] = str(uuid4())
    if case == "derived":
        packet["result"]["promotions"][0]["derived_fact_id"] = str(SOURCE)
    if case == "extra_pair":
        packet["result"]["promotions"] *= 2
    if case == "proposal_receipt":
        packet["result"] = {"proposal_id": str(PID)}
    if case == "legacy_digest":
        packet["mutation_receipt"]["command_digest"] = _promote_digest(command).hex()
    if case == "other_proposal_digest":
        packet["mutation_receipt"]["command_digest"] = _proposal_accept_digest(
            command, uuid4()
        ).hex()
    if case == "reason_digest":
        known["reason"] = "Substituted reason"
    if case == "scope_digest":
        known["target_scope"] = {
            "realm": "acme",
            "segments": [{"kind": "run", "identifier": "hidden"}],
        }
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = (
            diagnosis()
            if request.url.path.endswith("diagnose")
            else known
            if request.url.path.endswith("proposal-read")
            else packet
        )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await make_client(http).proposal_accept(
                PID,
                evidence_id=evidence,
                target_classification=Classification.INTERNAL,
                idempotency_key=uuid4(),
            )
        assert error.value.failure.code == "invalid_response"
        assert calls == ["/memory/v1/diagnose", "/memory/v1/proposal-read"] + (
            [] if case == "scope_digest" else ["/memory/v1/proposal-accept"]
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "pending_decision",
        "state_mismatch",
        "rejected_no_reason",
        "rejected_evidence",
        "accepted_reason",
        "creation_mutation",
        "source_as_publication",
        "bad_decider",
        "bad_time",
        "extra_decision",
    ],
)
async def test_decision_inconsistency_is_refused(case: str) -> None:
    packet = snapshot()
    decision: dict[str, Any] = {
        "state": "accepted",
        "decided_by": AUTHOR,
        "recorded_at": packet["recorded_at"],
        "mutation_id": str(uuid4()),
        "reason": None,
        "evidence_id": None,
        "promoted_fact_id": None,
    }
    packet.update(state="accepted", decision=decision)
    if case == "pending_decision":
        packet["state"] = "pending"
    if case == "state_mismatch":
        packet["state"] = "rejected"
    if case in {"rejected_no_reason", "rejected_evidence"}:
        packet["state"] = decision["state"] = "rejected"
        if case == "rejected_evidence":
            decision.update(reason="No", evidence_id=str(uuid4()))
    if case == "accepted_reason":
        decision["reason"] = "No"
    if case == "creation_mutation":
        decision["mutation_id"] = packet["mutation_id"]
    if case == "source_as_publication":
        decision["promoted_fact_id"] = str(SOURCE)
    if case == "bad_decider":
        decision["decided_by"] = "private-input"
    if case == "bad_time":
        decision["recorded_at"] = "2026-09-09T12:00:00Z"
    if case == "extra_decision":
        decision["hidden"] = "private-input"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await make_client(http).proposal_read(PID)
        assert error.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "too_many",
        "duplicate",
        "unordered",
        "before_after",
        "hidden_cursor",
        "short_page_cursor",
        "extra_page",
    ],
)
async def test_page_is_bound_to_requested_limit_cursor_and_order(case: str) -> None:
    first, second = snapshot(), snapshot()
    first["proposal_id"], second["proposal_id"] = str(UUID(int=1)), str(UUID(int=2))
    packet: dict[str, Any] = {"items": [first, second], "next_cursor": None}
    after = None
    limit = 2
    if case == "too_many":
        limit = 1
    if case == "duplicate":
        packet["items"] = [first, first]
    if case == "unordered":
        packet["items"] = [second, first]
    if case == "before_after":
        after = UUID(int=1)
    if case == "hidden_cursor":
        packet["next_cursor"] = str(uuid4())
    if case == "short_page_cursor":
        packet.update(items=[first], next_cursor=first["proposal_id"])
    if case == "extra_page":
        packet["total_hidden"] = 1

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        with pytest.raises(MemoryOperationFailure) as error:
            await make_client(http).proposal_list(limit=limit, after=after)
        assert error.value.failure.code == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "missing_instance",
        "wrong_instance",
        "bad_key",
        "bad_reason",
        "bad_target",
        "bool_limit",
        "bad_after",
    ],
)
async def test_diagnosis_precedes_content_and_invalid_input_never_mutates(
    case: str,
) -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        diag = diagnosis()
        diag["instance_id"] = str(uuid4())
        return httpx.Response(200, json=diag)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        client = make_client(http, None if case == "missing_instance" else INSTANCE)
        with pytest.raises(MemoryOperationFailure):
            if case == "bool_limit":
                await client.proposal_list(limit=True)
            elif case == "bad_after":
                await client.proposal_list(after="bad")  # type: ignore[arg-type]
            else:
                await client.propose(
                    PID,
                    source_fact_id=SOURCE,
                    target_scope=Scope("other", ())
                    if case == "bad_target"
                    else Scope("acme", ()),
                    reason="é" * 2049 if case == "bad_reason" else "Reuse",
                    idempotency_key="bad"  # type: ignore[arg-type]
                    if case == "bad_key"
                    else uuid5(NAMESPACE_URL, "proposal"),
                )
        assert calls == (["/memory/v1/diagnose"] if case == "wrong_instance" else [])


@pytest.mark.anyio
async def test_exact_mutation_wire_cap_and_immutable_receipts() -> None:
    from cairn.client.proposal_validation import PROPOSAL_MUTATION_BYTES

    packet = receipt()
    payload = json.dumps(packet).encode()
    excess = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("diagnose"):
            return httpx.Response(200, json=diagnosis())
        return httpx.Response(
            200,
            content=payload
            + b" " * (PROPOSAL_MUTATION_BYTES - len(payload) + int(excess)),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        client = make_client(http)
        result = await client.propose(
            PID,
            source_fact_id=SOURCE,
            target_scope=Scope("acme", ()),
            reason="Reuse",
            idempotency_key=uuid4(),
        )
        assert result.result.proposal_id == PID and result.outcome == "committed"
        with pytest.raises(TypeError):
            result.mutation_receipt["mutation_id"] = "changed"  # type: ignore[index]
        excess = True
        with pytest.raises(MemoryOperationFailure) as error:
            await client.propose(
                PID,
                source_fact_id=SOURCE,
                target_scope=Scope("acme", ()),
                reason="Reuse",
                idempotency_key=uuid4(),
            )
        assert error.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_canonical_decision_time_does_not_invent_monotonic_clock_contract() -> (
    None
):
    packet = snapshot()
    packet.update(
        state="rejected",
        decision={
            "state": "rejected",
            "decided_by": AUTHOR,
            "recorded_at": "2026-09-09T11:59:59.000000Z",
            "mutation_id": str(uuid4()),
            "reason": "No",
            "evidence_id": None,
            "promoted_fact_id": None,
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=diagnosis() if request.url.path.endswith("diagnose") else packet
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        result = await make_client(http).proposal_read(PID)
        assert (
            result.decision is not None
            and result.decision.recorded_at < result.recorded_at
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "mutation",
        "publication",
        "evidence",
        "outcome",
        "readable",
        "hidden-publication",
        "hidden-evidence",
        "hidden-both",
        "pending",
    ],
)
async def test_acceptance_binds_already_read_decision(case: str) -> None:
    from cairn.authority.mutations import PromoteFacts, _proposal_accept_digest

    evidence, publication = uuid4(), uuid4()
    packet = receipt()
    packet["outcome"] = "committed" if case == "pending" else "replayed"
    packet["mutation_receipt"]["command_digest"] = _proposal_accept_digest(
        PromoteFacts(
            (SOURCE,), evidence, Scope("acme", ()), Classification.INTERNAL, "Reuse"
        ),
        PID,
    ).hex()
    packet["result"] = {
        "promotions": [
            {"source_fact_id": str(SOURCE), "derived_fact_id": str(publication)}
        ],
        "evidence_id": str(evidence),
    }
    known = snapshot()
    if case != "pending":
        known.update(
            state="accepted",
            decision={
                "state": "accepted",
                "decided_by": AUTHOR,
                "recorded_at": known["recorded_at"],
                "mutation_id": packet["mutation_receipt"]["mutation_id"],
                "reason": None,
                "evidence_id": None
                if case in {"hidden-evidence", "hidden-both"}
                else str(evidence),
                "promoted_fact_id": None
                if case in {"hidden-publication", "hidden-both"}
                else str(publication),
            },
        )
    if case == "mutation":
        packet["mutation_receipt"]["mutation_id"] = str(uuid4())
    elif case == "publication":
        packet["result"]["promotions"][0]["derived_fact_id"] = str(uuid4())
    elif case == "evidence":
        # Response still matches caller evidence/digest; only the decision differs.
        known["decision"]["evidence_id"] = str(uuid4())
    elif case == "outcome":
        packet["outcome"] = "committed"
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        value = (
            diagnosis()
            if request.url.path.endswith("diagnose")
            else known
            if request.url.path.endswith("proposal-read")
            else packet
        )
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as http:
        client = make_client(http)
        if case in {"mutation", "publication", "evidence", "outcome"}:
            with pytest.raises(MemoryOperationFailure) as error:
                await client.proposal_accept(
                    PID,
                    evidence_id=evidence,
                    target_classification=Classification.INTERNAL,
                    idempotency_key=uuid4(),
                )
            assert error.value.failure.code == "invalid_response"
        else:
            result = await client.proposal_accept(
                PID,
                evidence_id=evidence,
                target_classification=Classification.INTERNAL,
                idempotency_key=uuid4(),
            )
            assert result.outcome == ("committed" if case == "pending" else "replayed")
            assert result.result.promotions == ((SOURCE, publication),)
            assert result.result.evidence_id == evidence
        assert calls == [
            "/memory/v1/diagnose",
            "/memory/v1/proposal-read",
            "/memory/v1/proposal-accept",
        ]
