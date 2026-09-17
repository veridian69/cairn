"""Composed graded housekeeping must survive the strict public client boundary."""

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import inventory, invoke, profile

from cairn.authority.memory_types import GRADED_RELEVANT_POLICY, RELEVANT_POLICY
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.sqlite import read_connection
from cairn.client.memory import MemoryClient
from cairn.client.suggestion_validation import validate_suggestions
from cairn.projection.graphiti import GraphitiIndex
from cairn.projection.memory import MemoryIndex
from cairn.projection.semantic_evidence import (
    SEARCH_POLICY,
    FactGrade,
    PartitionGrades,
    SemanticEvidence,
    SemanticEvidenceError,
    local_body_fingerprint,
    local_representation_sha256,
    query_sha256,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import DeliveryConfig

SCOPE = Scope("acme", (ScopeSegment("repository", "cairn"),))
GRADED = "lexical-graded/v2; relevant_only: lexical overlap or semantic membership"
UNAVAILABLE = RELEVANT_POLICY + "; semantic-unavailable"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    "policy,degraded,accepted",
    [
        (RELEVANT_POLICY, False, True),
        (RELEVANT_POLICY, True, True),
        (GRADED, False, True),
        (GRADED, True, True),  # Last policy, accumulated degradation across roots.
        (UNAVAILABLE, True, True),
        (UNAVAILABLE, False, False),
        (None, False, True),
        (None, True, False),
        ("lexical-graded/v2", False, False),
        (GRADED + "; semantic-unavailable", True, False),
        (GRADED + " ", False, False),
        (
            "lexical-graded/v3; relevant_only: lexical overlap or semantic membership",
            False,
            False,
        ),
        ("unknown", False, False),
    ],
)
def test_exact_policy_admission_and_degradation_consistency(
    policy: str | None, degraded: bool, accepted: bool
) -> None:
    packet = dict(
        items=[],
        budget_consumed=0,
        budget_exhausted=False,
        semantic_degraded=degraded,
        policy=policy,
    )
    roots = (UUID("11111111-1111-4111-8111-111111111111"),)
    if accepted:
        result = validate_suggestions(
            packet, scope=SCOPE, observation=None, fact_ids=roots, budget=65536, limit=8
        )
        assert result.policy == policy and result.semantic_degraded is degraded
    else:
        with pytest.raises(ValueError):
            validate_suggestions(
                packet,
                scope=SCOPE,
                observation=None,
                fact_ids=roots,
                budget=65536,
                limit=8,
            )


class ControlledV5(MemoryIndex, GraphitiIndex):
    """Real projection state and composition gate; no provider/driver construction."""

    def __init__(self, mode: str, empty: bool) -> None:
        MemoryIndex.__init__(self)
        self.mode, self.empty, self.calls = mode, empty, 0

    def search_with_evidence(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> SemanticEvidence:
        self.calls += 1
        if self.mode == "degraded" or (self.mode == "mixed" and self.calls % 2 == 1):
            raise SemanticEvidenceError()
        parts: list[PartitionGrades] = []
        identities: list[UUID] = []
        with self._lock:
            for key in partition_keys:
                grades = tuple(
                    FactGrade(identity, key, local_body_fingerprint(state.body), 0.9)
                    for identity, state in self._facts.items()
                    if not self.empty and state.partition_key == key
                )
                identities.extend(grade.fact_id for grade in grades)
                parts.append(PartitionGrades(key, True, len(grades), grades))
        return SemanticEvidence(
            query_sha256(query),
            local_representation_sha256(),
            SEARCH_POLICY,
            tuple(identities),
            tuple(parts),
        )


class ReadTrace(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        assert "Idempotency-Key" not in request.headers
        return await self.inner.handle_async_request(request)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "input_kind,empty,mode",
    [
        (kind, empty, mode)
        for kind in ("observation", "selected")
        for empty in (False, True)
        for mode in ("healthy", "degraded")
    ]
    + [("selected-multi", False, "mixed")],
)
async def test_composed_v5_rest_client_cli_suggestions_remain_read_only(
    tmp_path: Path,
    memory_support: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    input_kind: str,
    empty: bool,
    mode: str,
) -> None:
    instance = memory_support.Instance(tmp_path)
    instance.config = instance.config.model_copy(
        update={"delivery": DeliveryConfig(interval_seconds=1)}
    )
    _, token = instance.add_actor()
    index = ControlledV5(mode, empty)
    monkeypatch.setattr(
        index,
        "_graphiti",
        SimpleNamespace(
            embedder=SimpleNamespace(
                config=SimpleNamespace(
                    embedding_dim=1024, embedding_model="text-embedding-3-small"
                )
            )
        ),
        raising=False,
    )
    assert index.memory_evidence_source() is index
    app = build_application(instance.config, clock=instance.clock, index_adapter=index)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http,
    ):
        api = memory_support.Api(http, token)
        first = (await api.remember("Build uses port 8123."))["result"]["fact_ids"][0]
        second = None
        if not empty:
            second = (await api.remember("Build uses port 8123."))["result"][
                "fact_ids"
            ][0]
        for _ in range(300):
            with read_connection(instance.data_path) as connection:
                if (
                    connection.execute(
                        "SELECT count(*) FROM projection_outbox"
                    ).fetchone()[0]
                    == 0
                ):
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("controlled projection did not drain")
        body: dict[str, Any] = (
            {"observation": "quartz" if empty else "Build uses port 8123."}
            if input_kind == "observation"
            else {
                "fact_ids": [first, second]
                if input_kind == "selected-multi"
                else [first]
            }
        )
        before = inventory(instance)
        raw = await api.call(
            "suggest",
            {
                "scope": memory_support.SCOPE,
                "expected_instance_id": str(instance.config.instance_id),
                **body,
            },
        )
        expected_policy = UNAVAILABLE if mode == "degraded" else GRADED
        assert GRADED_RELEVANT_POLICY == GRADED
        assert raw["policy"] == expected_policy
        assert raw["semantic_degraded"] is (mode != "healthy")
        assert bool(raw["items"]) is not empty
        assert inventory(instance) == before
        trace = ReadTrace(http._transport)
        async with httpx.AsyncClient(
            transport=trace,
            base_url=http.base_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as client_http:
            client = MemoryClient(
                client_http,
                scope=SCOPE,
                classification=Classification.INTERNAL,
                expected_instance_id=instance.config.instance_id,
            )
            result = await client.suggest(
                observation=body.get("observation"),
                fact_ids=tuple(UUID(value) for value in body.get("fact_ids", [])),
            )
            assert result.policy == expected_policy
            assert result.semantic_degraded is (mode != "healthy")
            assert bool(result.items) is not empty
            assert inventory(instance) == before
            path = profile(tmp_path, instance, token, str(http.base_url).rstrip("/"))
            profile_data = json.loads(path.read_text())
            profile_data.pop("session_id")
            path.write_text(json.dumps(profile_data))
            code, cli = await invoke(path, "suggest", body, trace)
            assert code == 0, cli
            assert cli["result"]["policy"] == expected_policy
            assert cli["result"]["semantic_degraded"] is (mode != "healthy")
            assert bool(cli["result"]["items"]) is not empty
        # CLI preflight precedes MemoryClient's own instance handshake.
        assert trace.paths == [
            "/memory/v1/diagnose",
            "/memory/v1/suggest",
            "/memory/v1/diagnose",
            "/memory/v1/diagnose",
            "/memory/v1/suggest",
        ]
        assert inventory(instance) == before
