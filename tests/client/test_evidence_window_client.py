"""evidence-window client, strict validation and CLI command."""

import io
import json
import threading
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import NoRead, cli, invoke, profile

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.client import MemoryClient, RecallFailure
from cairn.client.validation import validate_evidence_window
from cairn.evidence.attic import SqliteAttic
from cairn.evidence.delivery import deliver_evidence_outbox

_SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
_SCOPE_JSON = {
    "realm": "acme",
    "segments": [{"kind": "repository", "identifier": "cairn"}],
}
_EVIDENCE = "66666666-6666-4666-8666-666666666666"
_DIGEST = "a" * 64
_EXCERPT = "".join(f"line {n}: please use port 8123 café\n" for n in range(30))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _client(http: httpx.AsyncClient) -> MemoryClient:
    return MemoryClient(http, scope=_SCOPE, classification=Classification.INTERNAL)


def _cost(document: dict[str, Any]) -> int:
    record = {key: value for key, value in document.items() if key != "budget_consumed"}
    return len(
        json.dumps(
            record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )


def _window(**changes: Any) -> dict[str, Any]:
    """A canonical query match: "port" inside "use port 8123\\n" of 40 bytes."""
    document: dict[str, Any] = {
        "evidence_id": _EVIDENCE,
        "mode": "query",
        "text": "use port 8123\n",
        "start_byte": 10,
        "end_byte": 24,
        "sha256": _DIGEST,
        "byte_length": 40,
        "match_found": True,
        "match_start_byte": 14,
        "match_end_byte": 18,
        "prefix_omitted": True,
        "suffix_omitted": True,
        "next_start_byte": 24,
        "budget_consumed": 0,
        **changes,
    }
    if "budget_consumed" not in changes:
        document["budget_consumed"] = _cost(document)
    return document


def _no_match(**changes: Any) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "text": None,
        "start_byte": None,
        "end_byte": None,
        "match_found": False,
        "match_start_byte": None,
        "match_end_byte": None,
        "prefix_omitted": None,
        "suffix_omitted": None,
        "next_start_byte": None,
    }
    return _window(**{**empty, **changes})


def deliver_evidence(instance: Any) -> None:
    """Stand in for the Attic delivery loop, deterministically."""
    deliver_evidence_outbox(
        CatalogueTransactions(
            instance.data_path,
            writer_gate=threading.Lock(),
            clock=instance.clock,
            uuid_factory=uuid4,
        ),
        SqliteAttic(instance.data_path),
        clock=instance.clock,
    )


async def _seed(memory_support: ModuleType, http: httpx.AsyncClient, token: str) -> str:
    saved = await memory_support.Api(http, token).remember(
        "The build uses port 8123.", evidence_payload=_EXCERPT
    )
    assert saved["outcome"] == "committed", saved
    evidence_id = saved["result"]["evidence_id"]
    assert isinstance(evidence_id, str)
    return evidence_id


# --- validator --------------------------------------------------------------


def test_validator_accepts_a_query_match_and_a_no_match() -> None:
    match = _window()
    assert validate_evidence_window(match, budget=match["budget_consumed"]) == match
    missing = _no_match()
    assert validate_evidence_window(missing, budget=16384) == missing


def test_validator_accepts_offset_windows_including_eof() -> None:
    offset = _window(
        mode="offset", match_found=None, match_start_byte=None, match_end_byte=None
    )
    assert validate_evidence_window(offset, budget=16384) == offset
    eof = _window(
        mode="offset",
        text="",
        start_byte=40,
        end_byte=40,
        match_found=None,
        match_start_byte=None,
        match_end_byte=None,
        prefix_omitted=True,
        suffix_omitted=False,
        next_start_byte=None,
    )
    assert validate_evidence_window(eof, budget=16384) == eof


def test_validator_rejects_an_extra_key() -> None:
    with pytest.raises(ValueError):
        validate_evidence_window({**_window(), "extra": 1}, budget=16384)


def test_validator_rejects_a_missing_key() -> None:
    document = _window()
    del document["mode"]
    with pytest.raises(ValueError):
        validate_evidence_window(document, budget=16384)


def test_validator_rejects_budget_consumed_other_than_canonical_cost() -> None:
    document = _window()
    with pytest.raises(ValueError):
        validate_evidence_window(
            {**document, "budget_consumed": document["budget_consumed"] + 1},
            budget=16384,
        )


def test_validator_rejects_a_record_over_budget() -> None:
    document = _window()
    with pytest.raises(ValueError):
        validate_evidence_window(document, budget=document["budget_consumed"] - 1)


def test_validator_rejects_text_length_other_than_the_byte_span() -> None:
    with pytest.raises(ValueError):
        validate_evidence_window(_window(text="use port 8123é"), budget=16384)


def test_validator_rejects_a_match_span_outside_the_window() -> None:
    with pytest.raises(ValueError):
        validate_evidence_window(
            _window(match_start_byte=20, match_end_byte=26), budget=16384
        )


def test_validator_rejects_next_start_byte_without_suffix_omitted() -> None:
    final = {
        "start_byte": 26,
        "end_byte": 40,
        "match_start_byte": 30,
        "match_end_byte": 34,
        "suffix_omitted": False,
    }
    assert validate_evidence_window(
        _window(**final, next_start_byte=None), budget=16384
    )
    with pytest.raises(ValueError):
        validate_evidence_window(_window(**final, next_start_byte=40), budget=16384)


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_id": "66666666-6666-1666-8666-666666666666"},
        {"evidence_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()},
        {"mode": "semantic"},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"byte_length": -1},
        {"byte_length": True},
        {"match_found": False},
        {"match_found": None},
        {"prefix_omitted": False},
        {"suffix_omitted": False, "next_start_byte": None},
        {"next_start_byte": 23},
        {"end_byte": 41, "text": "use port 8123\n" + "x" * 17, "next_start_byte": 41},
        {"start_byte": 0, "prefix_omitted": False},
        {"match_start_byte": 18, "match_end_byte": 18},
        {"budget_consumed": True},
    ],
)
def test_validator_rejects_inconsistent_windows(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_evidence_window(_window(**changes), budget=16384)


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"start_byte": 0},
        {"prefix_omitted": False},
        {"next_start_byte": 0},
        {"match_start_byte": 0},
    ],
)
def test_validator_rejects_a_no_match_carrying_any_extent(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        validate_evidence_window(_no_match(**changes), budget=16384)


def test_validator_rejects_an_offset_window_with_a_match() -> None:
    with pytest.raises(ValueError):
        validate_evidence_window(_window(mode="offset", match_found=None), budget=16384)


def test_validator_rejects_a_continuation_without_progress() -> None:
    with pytest.raises(ValueError):
        validate_evidence_window(
            _window(
                mode="offset",
                text="",
                end_byte=10,
                match_found=None,
                match_start_byte=None,
                match_end_byte=None,
                next_start_byte=10,
            ),
            budget=16384,
        )


# --- wire requests and local refusals ---------------------------------------


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.bodies: list[dict[str, object]] = []
        self.paths: list[str] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.response is not None:
            return self.response
        if "query" in body:
            return httpx.Response(200, json=_window())
        start = body.get("start", 0)
        return httpx.Response(
            200,
            json=_window(
                mode="offset",
                text="x" * (40 - start),
                start_byte=start,
                end_byte=40,
                match_found=None,
                match_start_byte=None,
                match_end_byte=None,
                prefix_omitted=start > 0,
                suffix_omitted=False,
                next_start_byte=None,
            ),
        )


@pytest.mark.anyio
async def test_request_bodies_omit_unset_fields() -> None:
    recorder = Recorder()
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(recorder)
    ) as http:
        client = _client(http)
        evidence = UUID(_EVIDENCE)
        await client.evidence_window(evidence, query="port")
        await client.evidence_window(evidence)
        await client.evidence_window(evidence, start=12, budget=9999)
    base = {"scope": _SCOPE_JSON, "evidence_id": _EVIDENCE}
    assert recorder.paths == ["/memory/v1/evidence-window"] * 3
    assert recorder.bodies == [
        {**base, "budget": 16384, "query": "port"},
        {**base, "budget": 16384},
        {**base, "budget": 9999, "start": 12},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call,code",
    [
        ({"evidence_id": str(_EVIDENCE)}, "invalid_evidence_id"),
        (
            {"evidence_id": UUID("66666666-6666-1666-8666-666666666666")},
            "invalid_evidence_id",
        ),
        ({"query": "x", "start": 0}, "invalid_query"),
        ({"query": ""}, "invalid_query"),
        ({"query": "  \n"}, "invalid_query"),
        ({"query": "\ud800"}, "invalid_query"),
        ({"query": "é" * 4097}, "invalid_query"),
        ({"query": 7}, "invalid_query"),
        ({"start": -1}, "invalid_offset"),
        ({"start": 1_048_577}, "invalid_offset"),
        ({"start": True}, "invalid_offset"),
        ({"budget": 0}, "invalid_budget"),
        ({"budget": 1_048_577}, "invalid_budget"),
        ({"budget": True}, "invalid_budget"),
    ],
)
async def test_local_refusals_precede_io(call: dict[str, Any], code: str) -> None:
    recorder = Recorder()
    arguments = dict(call)
    evidence = arguments.pop("evidence_id", UUID(_EVIDENCE))
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid", transport=httpx.MockTransport(recorder)
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).evidence_window(evidence, **arguments)
    assert raised.value.operation == "evidence-window"
    assert raised.value.failure.code == code
    assert recorder.paths == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call,response",
    [
        # A query request answered with an offset window.
        (
            {"query": "port"},
            _window(
                mode="offset",
                match_found=None,
                match_start_byte=None,
                match_end_byte=None,
            ),
        ),
        # An offset request answered from another offset.
        (
            {"start": 12},
            _window(
                mode="offset",
                match_found=None,
                match_start_byte=None,
                match_end_byte=None,
            ),
        ),
        # Another evidence record.
        (
            {"query": "port"},
            _window(evidence_id="77777777-7777-4777-8777-777777777777"),
        ),
    ],
)
async def test_response_must_answer_the_request(
    call: dict[str, Any], response: dict[str, Any]
) -> None:
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(Recorder(httpx.Response(200, json=response))),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).evidence_window(UUID(_EVIDENCE), **call)
    assert raised.value.failure.code == "invalid_response"


@pytest.mark.anyio
async def test_page_failure_detail_is_kept_only_on_the_documented_refusal() -> None:
    body = {
        "failure": {
            "code": "invalid_request",
            "message": "PRIVATE prose",
            "retry": "never",
            "correlation_id": "77777777-7777-4777-8777-777777777777",
            "detail": {"reason": "page_budget_too_small", "minimum_budget": 900},
        }
    }
    async with httpx.AsyncClient(
        base_url="https://cairn.invalid",
        transport=httpx.MockTransport(Recorder(httpx.Response(400, json=body))),
    ) as http:
        with pytest.raises(RecallFailure) as raised:
            await _client(http).evidence_window(UUID(_EVIDENCE), query="x")
    failure = raised.value.failure
    assert raised.value.operation == "evidence-window"
    assert failure.detail == (
        ("minimum_budget", 900),
        ("reason", "page_budget_too_small"),
    )
    assert "PRIVATE" not in failure.message


# --- live ASGI round trips -------------------------------------------------


@pytest.mark.anyio
async def test_client_reads_query_and_offset_windows(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        evidence_id = UUID(await _seed(memory_support, http, token))
        deliver_evidence(instance)
        http.headers["Authorization"] = f"Bearer {token}"
        client = _client(http)
        found = await client.evidence_window(evidence_id, query="line 12")
        pieces: list[str] = []
        start: int | None = None
        for _ in range(40):
            window = await client.evidence_window(evidence_id, start=start, budget=600)
            pieces.append(str(window.data["text"]))
            following = window.data["next_start_byte"]
            if following is None:
                break
            assert type(following) is int
            start = following
        else:
            raise AssertionError("offset paging did not finish")
        with pytest.raises(RecallFailure) as raised:
            await client.evidence_window(evidence_id, query="port", budget=20)
    assert found.content_role == "untrusted-data"
    assert found.data["match_found"] is True
    assert "line 12: please" in str(found.data["text"])
    assert "".join(pieces) == _EXCERPT and len(pieces) > 1
    detail = dict(raised.value.failure.detail or ())
    assert detail["reason"] == "page_budget_too_small"
    assert int(detail["minimum_budget"]) > 20


# --- CLI --------------------------------------------------------------------


class Capture(httpx.AsyncBaseTransport):
    def __init__(self, upstream: httpx.AsyncBaseTransport) -> None:
        self.upstream = upstream
        self.windows: list[dict[str, object]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/memory/v1/evidence-window":
            self.windows.append(json.loads(request.content))
        return await self.upstream.handle_async_request(request)


@pytest.mark.anyio
async def test_cli_evidence_window_round_trip_continuation_and_budget_refusal(
    tmp_path: Path, memory_support: ModuleType
) -> None:
    instance = memory_support.Instance(tmp_path)
    _, token = instance.add_actor()
    async with memory_support.serve(instance) as http:
        evidence_id = await _seed(memory_support, http, token)
        deliver_evidence(instance)
        path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
        transport = Capture(http._transport)
        code, found = await invoke(
            path,
            "evidence-window",
            {"evidence_id": evidence_id, "query": "port 8123"},
            transport,
        )
        assert code == 0, found
        assert found["command"] == "evidence-window"
        match = found["result"]["data"]
        assert match["match_found"] is True and match["mode"] == "query"
        code, first = await invoke(
            path,
            "evidence-window",
            {"evidence_id": evidence_id, "budget": 500},
            transport,
        )
        assert code == 0, first
        following = first["result"]["data"]["next_start_byte"]
        assert type(following) is int and following > 0
        code, rest = await invoke(
            path,
            "evidence-window",
            {"evidence_id": evidence_id, "start": following, "budget": 500},
            transport,
        )
        assert code == 0, rest
        assert rest["result"]["data"]["start_byte"] == following
        code, refused = await invoke(
            path,
            "evidence-window",
            {"evidence_id": evidence_id, "query": "port", "budget": 10},
            transport,
        )
    base = {"scope": _SCOPE_JSON, "evidence_id": evidence_id}
    assert transport.windows[:3] == [
        {**base, "budget": 16384, "query": "port 8123"},
        {**base, "budget": 500},
        {**base, "budget": 500, "start": following},
    ]
    assert code == 2
    error = refused["result"]["error"]
    assert error["code"] == "invalid_request"
    assert error["operation"] == "evidence-window"
    assert error["detail"]["reason"] == "page_budget_too_small"
    assert error["detail"]["minimum_budget"] > 10
    assert set(error) == {"code", "operation", "detail"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": "x"},
        {"evidence_id": None},
        {"evidence_id": "66666666-6666-1666-8666-666666666666"},
        {"evidence_id": _EVIDENCE, "query": "x", "start": 0},
        {"evidence_id": _EVIDENCE, "query": None},
        {"evidence_id": _EVIDENCE, "query": ""},
        {"evidence_id": _EVIDENCE, "query": "   "},
        {"evidence_id": _EVIDENCE, "query": "é" * 4097},
        {"evidence_id": _EVIDENCE, "start": None},
        {"evidence_id": _EVIDENCE, "start": -1},
        {"evidence_id": _EVIDENCE, "start": 1_048_577},
        {"evidence_id": _EVIDENCE, "start": True},
        {"evidence_id": _EVIDENCE, "budget": 0},
        {"evidence_id": _EVIDENCE, "budget": 1_048_577},
        {"evidence_id": _EVIDENCE, "cursor": "A" * 43},
        {"evidence_id": _EVIDENCE, "scope": {}},
    ],
)
async def test_cli_evidence_window_strict_inputs_before_any_network(
    body: dict[str, object],
) -> None:
    code, result = await invoke(Path("/nonexistent-profile"), "evidence-window", body)
    assert code == 2
    assert result["result"]["error"]["code"] == "invalid_input"


@pytest.mark.anyio
async def test_cli_evidence_window_help_is_standalone() -> None:
    out = io.BytesIO()
    code = await cli().execute(
        ["evidence-window", "--help"], stdin=NoRead(), stdout=out, stderr=io.BytesIO()
    )
    text = out.getvalue()
    assert code == 0 and b"usage:" in text
    assert all(
        field in text
        for field in (b"evidence_id", b"query", b"start", b"budget", b"next_start_byte")
    )
