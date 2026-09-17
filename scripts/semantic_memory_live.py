"""Private real-provider worker for evaluate_semantic_memory.py.

The launcher supplies a fresh directory, its own loopback FalkorDB port and
an allowlisted environment. Never invoke against an existing Cairn directory.
Expected labels are used only here for scoring, not to prompt the provider.
"""

import asyncio
import hashlib
import json
import math
import secrets
import sys
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from asgi_lifespan import LifespanManager
from graphiti_core.cross_encoder.openai_reranker_client import DEFAULT_MODEL
from graphiti_core.llm_client.openai_client import OpenAIClient
from httpx import ASGITransport, AsyncClient
from semantic_memory_corpus import assess_query, seed_events, validate_corpus

from cairn.bootstrap.procedures import bootstrap_realm
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.verification import verify_catalogue
from cairn.projection.adapter import FactProjected, ProjectedFactState, ProjectionFailed
from cairn.projection.graphiti import GraphitiIndex, GraphitiIndexError
from cairn.projection.semantic_evidence import SemanticEvidence, SemanticEvidenceError
from cairn.runtime.composition import build_application
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    DeliveryConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)

ROOT = Path(__file__).resolve().parents[1]


class BoundedIndex(GraphitiIndex):
    """Real adapter, with evaluator-only fail-stop instead of outbox retries."""

    def __init__(self, port: int) -> None:
        self.failed = False
        super().__init__(host="127.0.0.1", port=port, index_concurrency_limit=4)
        # The shared SDK client serves LLM, embedding and reranking roles.
        # Disable SDK and invalid-response retries for this bounded experiment.
        self._provider_client.max_retries = 0
        if not isinstance(self._graphiti.llm_client, OpenAIClient):
            self.close()
            raise GraphitiIndexError("evaluation_provider_unexpected")
        type(self._graphiti.llm_client).MAX_RETRIES = 0

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        if self.failed:
            return ProjectionFailed("evaluation_stopped")
        try:
            result = super().project(state)
        except Exception:
            self.failed = True
            raise
        if isinstance(result, ProjectionFailed):
            self.failed = True
        return result

    def search(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        if self.failed:
            raise GraphitiIndexError("evaluation_stopped")
        try:
            return super().search(*args, **kwargs)
        except Exception:
            self.failed = True
            raise

    def search_with_evidence(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> SemanticEvidence:
        if self.failed:
            raise SemanticEvidenceError()
        try:
            return super().search_with_evidence(query, limit, partition_keys)
        except Exception:
            self.failed = True
            raise SemanticEvidenceError() from None


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def source_digest() -> str:
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in (ROOT / "src/cairn").rglob("*")
        if path.is_file() and path.suffix in (".py", ".sql", ".json")
    )
    paths += sorted((ROOT / "scripts").glob("*semantic*.py"))
    for path in paths:
        digest.update(
            str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0"
        )
    return digest.hexdigest()


def index_policy_metadata(index: Any) -> dict[str, Any]:
    """Report the executing adapter, never infer policy from fixture settings."""
    if isinstance(index, GraphitiIndex):
        return index.index_policy_metadata()
    return {"available": False, "source": "controlled-index-not-semantic"}


def provider_metadata(graphiti: Any) -> dict[str, Any]:
    """Expose measured successful LLM totals without claiming full provider usage."""
    embedding = getattr(graphiti.embedder, "config", None)
    reranker = getattr(getattr(graphiti, "cross_encoder", None), "config", None)
    tracker = getattr(graphiti.llm_client, "token_tracker", None)
    extraction_settings: dict[str, Any] = {}
    for name in ("max_tokens", "temperature", "reasoning", "verbosity"):
        value = getattr(graphiti.llm_client, name, "unavailable")
        extraction_settings[name] = (
            value
            if type(value) in (int, float, str, type(None))
            and not (type(value) is float and not math.isfinite(value))
            else "unavailable"
        )
    usage: dict[str, Any] = {
        "coverage": "successful_llm_calls_only",
        "excludes": ["embeddings", "reranking", "failed_calls"],
        "input_including_cached": "unavailable",
        "fresh_input": "unavailable",
        "reused_input": "unavailable",
        "output": "unavailable",
        "model_calls": "unavailable",
        "retries": "unavailable",
    }
    if tracker is not None:
        calls = tracker.get_usage().values()
        usage.update(
            input_including_cached=sum(call.total_input_tokens for call in calls),
            output=sum(call.total_output_tokens for call in calls),
            model_calls=sum(call.call_count for call in calls),
        )
    return {
        "models": {
            "medium": graphiti.llm_client.model,
            "small": graphiti.llm_client.small_model,
            "embedder": type(graphiti.embedder).__name__,
            "embedding_model": getattr(embedding, "embedding_model", "unavailable"),
            "embedding_dimensions": getattr(embedding, "embedding_dim", "unavailable"),
            "reranker": (reranker.model or DEFAULT_MODEL)
            if reranker is not None
            else "unavailable",
            "sdk_retries": 0,
            "response_retries": 0,
        },
        "usage": usage,
        "output_settings": {
            "extraction": extraction_settings,
            "source": "executing graphiti.llm_client attributes; defaults, not every request",
            "reranking": {
                "max_tokens": "unavailable_from_client_configuration",
                "source": "reranker request limits are not extraction defaults",
            },
        },
    }


async def evaluate(root: Path, port: int, duration: int, owner: str) -> dict[str, Any]:
    started, deadline = monotonic(), monotonic() + duration
    before = source_digest()
    corpus_bytes = (root / "corpus.json").read_bytes()
    corpus = validate_corpus(json.loads(corpus_bytes))
    events = seed_events(corpus)
    clock = Clock(datetime.fromisoformat(events[0]["recorded_at"]) - timedelta(days=1))
    data, credentials = root / "data", root / "credentials"
    data.mkdir()
    credentials.mkdir()
    realm = f"synthetic-{owner}"
    scope = {"realm": realm, "segments": [{"kind": "job", "identifier": "quality"}]}
    sibling = {"realm": realm, "segments": [{"kind": "job", "identifier": "sibling"}]}
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=uuid4(),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
        attic=AtticConfig(enabled=False),
        graphiti=GraphitiConfig(enabled=True, port=port),
        delivery=DeliveryConfig(interval_seconds=1, chunk_size=1),
    )
    migrate_catalogue(config, clock)
    bootstrap = bootstrap_realm(
        config,
        realm_id=realm,
        label="synthetic-evaluator",
        clock=clock,
        uuid_factory=uuid4,
        entropy=secrets.token_bytes,
    )
    index = BoundedIndex(port)
    facts: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    history_checks: list[bool] = []
    stopped = False
    try:
        # Explicit real adapter injection prevents mode=test selecting MemoryIndex.
        app = build_application(config, clock=clock, index_adapter=index)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(LifespanManager(app))

            async def connect(token: str) -> AsyncClient:
                return await stack.enter_async_context(
                    AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="http://127.0.0.1:8000",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                )

            async def call(
                client: AsyncClient,
                path: str,
                body: dict[str, Any],
                *,
                mutation: bool = True,
            ) -> dict[str, Any]:
                if monotonic() >= deadline or index.failed:
                    raise RuntimeError("semantic_evaluation_stopped")
                response = await client.post(
                    path,
                    json=body,
                    headers={"Idempotency-Key": str(uuid4())} if mutation else {},
                )
                document = response.json()
                if response.status_code != 200 or "failure" in document:
                    raise RuntimeError("synthetic_operation_failed")
                value = document["result"] if mutation else document
                if not isinstance(value, dict):
                    raise RuntimeError("synthetic_operation_failed")
                return value

            admin = await connect(bootstrap.token)
            principal = await call(
                admin,
                "/v1/create-principal",
                {"realm_id": realm, "kind": "workload", "label": "synthetic-reader"},
            )
            expiry = (
                datetime.fromisoformat(events[-1]["recorded_at"]) + timedelta(days=3650)
            ).isoformat()
            credential = await call(
                admin,
                "/v1/issue-credential",
                {
                    "realm_id": realm,
                    "principal_id": principal["principal_id"],
                    "expires_at": expiry,
                },
            )
            await call(
                admin,
                "/v1/create-grant",
                {
                    "realm_id": realm,
                    "grant": {
                        "realm_id": realm,
                        "principal_id": principal["principal_id"],
                        "segments": scope["segments"],
                        "operations": ["retrieve"],
                        "read_clearance": "internal",
                        "write_classifications": [],
                        "expires_at": expiry,
                    },
                },
            )
            reader = await connect(credential["plaintext"])
            for event in events:
                clock.now = datetime.fromisoformat(event["recorded_at"])
                value = event["value"]
                if event["kind"] == "fact":
                    result = await call(
                        admin,
                        "/memory/v1/remember",
                        {
                            "scope": scope if value["scope"] == "job" else sibling,
                            "classification": "internal",
                            "facts": [{"body": value["body"]}],
                        },
                    )
                    facts[value["label"]] = result["fact_ids"][0]
                else:
                    await call(
                        admin,
                        "/memory/v1/correct",
                        {
                            "fact_ids": [facts[value["source"]]],
                            "superseded_by": facts[value["replacement"]],
                            "reason": value["reason"],
                        },
                    )
            clock.now += timedelta(days=180)
            while True:
                with read_connection(data) as connection:
                    remaining, attempts = connection.execute(
                        "SELECT count(*), coalesce(sum(attempts), 0) FROM projection_outbox"
                    ).fetchone()
                if index.failed or attempts or monotonic() >= deadline:
                    raise RuntimeError("projection_incomplete")
                if remaining == 0:
                    break
                await asyncio.sleep(0.1)
            labels = {identity: label for label, identity in facts.items()}
            for query in corpus["queries"]:
                tick = monotonic()
                packet = await call(
                    reader,
                    "/memory/v1/recall",
                    {
                        "scope": scope,
                        "query": query["query"],
                        "budget": 65536,
                        "relevant_only": True,
                    },
                    mutation=False,
                )
                returned = [
                    labels.get(hit["fact_id"], "unknown") for hit in packet["hits"]
                ]
                assessment = assess_query(
                    corpus,
                    query,
                    returned,
                    projection_complete=True,
                    semantic_degraded=packet["semantic_degraded"],
                )
                if any(
                    hit["scope"] != scope or hit["trust"] != "candidate"
                    for hit in packet["hits"]
                ):
                    assessment["expectations_met"] = False
                    assessment["failed_expectations"].append("scope_or_trust")
                rows.append(
                    {
                        "name": query["name"],
                        "split": query["split"],
                        "query": query["query"],
                        "returned_labels": returned,
                        "returned_fact_ids": [hit["fact_id"] for hit in packet["hits"]],
                        "policy": packet["policy"],
                        "semantic_degraded": packet["semantic_degraded"],
                        "latency_ms": (monotonic() - tick) * 1000,
                        **assessment,
                    }
                )
                # Authority may return safe catalogue fallback after source or
                # envelope failure. Keep that response and its timing, then leave
                # normally so main can persist the incomplete report. Raising at
                # the next call's guard would discard all accumulated evidence.
                if index.failed or packet["semantic_degraded"]:
                    stopped = True
                    break
            for correction in corpus["corrections"]:
                if stopped:
                    break
                packet = await call(
                    reader,
                    "/memory/v1/history",
                    {
                        "scope": scope,
                        "fact_id": facts[correction["replacement"]],
                        "budget": 32768,
                    },
                    mutation=False,
                )
                history_ids = {fact["fact_id"] for fact in packet["facts"]}
                history_checks.append(
                    {facts[correction["source"]], facts[correction["replacement"]]}
                    <= history_ids
                    and any(
                        row["fact_id"] == facts[correction["source"]]
                        and row["superseded_by"] == facts[correction["replacement"]]
                        for row in packet["corrections"]
                    )
                )
        verify_catalogue(config)
        if before != source_digest():
            raise RuntimeError("source_changed_during_evaluation")
        complete = (
            not stopped
            and len(rows) == len(corpus["queries"])
            and len(history_checks) == len(corpus["corrections"])
        )
        provider_backed = isinstance(index, GraphitiIndex)
        quality = (
            provider_backed
            and complete
            and all(row["expectations_met"] for row in rows)
            and all(history_checks)
        )
        metadata = provider_metadata(index._graphiti)
        return {
            "schema": "cairn.semantic-memory-evaluation/v1",
            "complete": complete,
            "failure": "semantic_evaluation_stopped" if stopped else None,
            "unrun_queries": [
                query["name"] for query in corpus["queries"][len(rows) :]
            ],
            "unrun_history_checks": len(corpus["corrections"]) - len(history_checks),
            "synthetic": True,
            "semantic_evidence": provider_backed,
            "retrieval_mode": "real-graphiti"
            if provider_backed
            else "controlled-index-not-semantic",
            "instance_id": str(config.instance_id),
            "quality_expectations_met": quality,
            "scenarios": rows,
            "correction_history_checks": history_checks,
            "projection_complete": True,
            "catalogue_verified": True,
            "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            "source_sha256": before,
            "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
            "graphiti_version": version("graphiti-core"),
            "openai_version": version("openai"),
            "model_configuration": metadata["models"],
            "llm_output_settings": metadata["output_settings"],
            "index_policy": index_policy_metadata(index),
            "successful_llm_usage": metadata["usage"],
            "usage": {
                "fresh_input": "unavailable",
                "reused_input": "unavailable",
                "output": "unavailable",
                "model_calls": "unavailable",
                "retries": "unavailable",
            },
            "elapsed_seconds": monotonic() - started,
        }
    finally:
        # Composition does not close caller-owned adapters, even on startup failure.
        index.close()


def main() -> int:
    root, port, duration, owner = (
        Path(sys.argv[1]),
        int(sys.argv[2]),
        int(sys.argv[3]),
        sys.argv[4],
    )
    try:
        result = asyncio.run(evaluate(root, port, duration, owner))
        with (root / "result.json").open("x") as output:
            json.dump(result, output, sort_keys=True)
        return 0 if result["quality_expectations_met"] else 1
    except Exception:
        # Provider bodies and raw exceptions never reach the parent's evidence/log.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
