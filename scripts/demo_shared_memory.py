"""Synthetic shared-memory acceptance: no network, external data or model calls.

Run from the repository with ``uv run --locked python scripts/demo_shared_memory.py``.
Every request uses the real composed ASGI application and a temporary catalogue.
Agent labels describe scripted callbacks, not calls to those model providers.
"""

import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from cairn.bootstrap.procedures import bootstrap_realm
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.verification import verify_catalogue
from cairn.client import (
    DurableObservation,
    MemoryClient,
    MemorySession,
    ModelTurn,
    PersistenceFailure,
    TurnInput,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import AtticConfig, CairnConfig, HttpConfig, PathConfig


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 9, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def demonstrate(root: Path) -> dict:
    clock = Clock()
    data, credentials = root / "data", root / "credentials"
    data.mkdir()
    credentials.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=uuid4(),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
        attic=AtticConfig(enabled=True),
    )
    migrate_catalogue(config, clock)
    bootstrap = bootstrap_realm(
        config,
        realm_id="demo",
        label="synthetic-human-operator",
        clock=clock,
        uuid_factory=uuid4,
        entropy=secrets.token_bytes,
    )
    scope = Scope("demo", (ScopeSegment("job", "shared-conversation"),))
    scope_body = {
        "realm": "demo",
        "segments": [{"kind": "job", "identifier": "shared-conversation"}],
    }
    sibling = {
        "realm": "demo",
        "segments": [{"kind": "job", "identifier": "other-conversation"}],
    }
    report: dict = {
        "synthetic": True,
        "provider_calls": 0,
        "checks": [],
        "receipts": [],
    }
    app = build_application(config, clock=clock)

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

        admin = await connect(bootstrap.token)

        async def call(http, operation, body, *, mutation=True, key=None):
            headers = {"Idempotency-Key": str(key or uuid4())} if mutation else {}
            response = await http.post(operation, json=body, headers=headers)
            document = response.json()
            if response.status_code != 200 or "failure" in document:
                code = document.get("failure", {}).get("code", "unexpected_response")
                raise RuntimeError(f"Synthetic operation {operation} failed: {code}")
            if mutation:
                report["receipts"].append(document["audit_receipt"])
                return document["result"]
            return document

        async def actor(label, actor_scope):
            principal = await call(
                admin,
                "/v1/create-principal",
                {
                    "realm_id": "demo",
                    "kind": "workload",
                    "label": label,
                },
            )
            identity = principal["principal_id"]
            credential = await call(
                admin,
                "/v1/issue-credential",
                {
                    "realm_id": "demo",
                    "principal_id": identity,
                    "expires_at": "2028-01-01T00:00:00.000000Z",
                },
            )
            await call(
                admin,
                "/v1/create-grant",
                {
                    "realm_id": "demo",
                    "grant": {
                        "principal_id": identity,
                        "realm_id": "demo",
                        "segments": actor_scope["segments"],
                        "operations": ["ingest", "retrieve"],
                        "read_clearance": "internal",
                        "write_classifications": ["internal"],
                        "expires_at": "2028-01-01T00:00:00.000000Z",
                    },
                },
            )
            return identity, await connect(credential["plaintext"])

        val_id, val = await actor("val-scripted", scope_body)
        spike_id, spike = await actor("spike-scripted", scope_body)
        deepseek_id, deepseek = await actor("deepseek-scripted", scope_body)
        _, outsider = await actor("different-job", sibling)
        report["principals"] = {
            "Val": val_id,
            "Spike": spike_id,
            "DeepSeek": deepseek_id,
        }

        def session(http):
            return MemorySession(
                MemoryClient(http, scope=scope, classification=Classification.INTERNAL),
                session_id=uuid4(),
            )

        async def val_turn(context: TurnInput) -> ModelTurn:
            return ModelTurn(
                "I have recorded the initial observation.",
                (DurableObservation("Quartz collector uses port 7000."),),
            )

        val_result = await session(val).run_turn(
            "Quartz collector", val_turn, turn_id=uuid4()
        )
        left = str(val_result.persistence.result["fact_ids"][0])

        async def spike_turn(context: TurnInput) -> ModelTurn:
            hits = context.recalled.data["hits"]
            assert any(hit["source_principal_id"] == val_id for hit in hits)
            assert all(hit["trust"] == "candidate" for hit in hits)
            return ModelTurn(
                "The observed port differs.",
                (DurableObservation("Quartz collector uses port 7001."),),
            )

        spike_result = await session(spike).run_turn(
            "Quartz collector", spike_turn, turn_id=uuid4()
        )
        right = str(spike_result.persistence.result["fact_ids"][0])
        report["facts"] = {"initial": left, "corrected": right}
        report["checks"].append(
            "automatic recall and candidate remembering across principals"
        )
        disagreement = await call(
            spike,
            "/memory/v1/disagree",
            {
                "scope": scope_body,
                "left_fact_id": left,
                "right_fact_id": right,
                "classification": "internal",
                "reason": "The reported port numbers differ.",
            },
        )
        report["disagreement_id"] = disagreement["relationship_id"]

        async def deepseek_turn(context: TurnInput) -> ModelTurn:
            packet = context.recalled.data
            assert {hit["source_principal_id"] for hit in packet["hits"]} == {
                val_id,
                spike_id,
            }
            assert packet["disagreements"][0]["principal_id"] == spike_id
            assert not packet["resolutions"]
            return ModelTurn("Both accounts are visible; this remains unresolved.")

        await session(deepseek).run_turn(
            "Quartz collector", deepseek_turn, turn_id=uuid4()
        )
        report["checks"].append(
            "a third principal sees both owners and unresolved disagreement"
        )

        evidence = await call(
            admin,
            "/memory/v1/remember",
            {
                "scope": scope_body,
                "classification": "internal",
                "facts": [{"body": "Synthetic operator check confirms port 7001."}],
                "evidence_payload": "Synthetic measurement for the demonstration: port 7001.",
            },
        )
        await call(
            admin,
            "/memory/v1/resolve",
            {
                "scope": scope_body,
                "disagreement_id": disagreement["relationship_id"],
                "evidence_id": evidence["evidence_id"],
                "selected_fact_id": right,
                "reason": "The synthetic operator check supports the second observation.",
            },
        )
        clock.now += timedelta(seconds=1)
        await call(
            admin,
            "/memory/v1/correct",
            {
                "fact_ids": [left],
                "superseded_by": right,
                "reason": "The operator checked the port and corrected the earlier belief.",
            },
        )
        history = await call(
            deepseek,
            "/memory/v1/history",
            {
                "scope": scope_body,
                "fact_id": right,
                "budget": 16384,
            },
            mutation=False,
        )
        assert {fact["fact_id"] for fact in history["facts"]} >= {left, right}
        assert any(
            c["fact_id"] == left and "checked the port" in c["reason"]
            for c in history["corrections"]
        )
        current = await call(
            deepseek,
            "/memory/v1/recall",
            {
                "scope": scope_body,
                "query": "Quartz collector",
                "budget": 16384,
            },
            mutation=False,
        )
        assert left not in {hit["fact_id"] for hit in current["hits"]}
        report["checks"].append(
            "explicit resolution and correction preserve recoverable history"
        )

        old = await call(
            val,
            "/memory/v1/remember",
            {
                "scope": scope_body,
                "classification": "internal",
                "facts": [
                    {
                        "body": "Status amber calibration baseline. "
                        + "Synthetic context. " * 32
                    }
                ],
            },
        )
        clock.now += timedelta(days=100)
        fresh = await call(
            spike,
            "/memory/v1/remember",
            {
                "scope": scope_body,
                "classification": "internal",
                "facts": [
                    {
                        "body": "Status current maintenance rota. "
                        + "Synthetic context. " * 32
                    }
                ],
            },
        )
        routine = await call(
            deepseek,
            "/memory/v1/recall",
            {
                "scope": scope_body,
                "query": "status",
                "budget": 1400,
            },
            mutation=False,
        )
        assert routine["hits"][0]["fact_id"] == fresh["fact_ids"][0]
        assert old["fact_ids"][0] not in {hit["fact_id"] for hit in routine["hits"]}
        cued = await call(
            deepseek,
            "/memory/v1/recall",
            {
                "scope": scope_body,
                "query": "amber calibration",
                "budget": 1400,
            },
            mutation=False,
        )
        assert cued["hits"][0]["fact_id"] == old["fact_ids"][0]
        report["checks"].append(
            "old memory fades from a small packet and resurfaces on a strong cue"
        )

        denied = await outsider.post(
            "/memory/v1/recall",
            json={
                "scope": scope_body,
                "query": "Quartz",
                "budget": 16384,
            },
        )
        assert denied.status_code == 403
        widened = await val.post(
            "/memory/v1/remember",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "scope": {"realm": "demo", "segments": []},
                "classification": "internal",
                "facts": [{"body": "This must not escape its job."}],
            },
        )
        assert widened.status_code == 403
        promoted = await val.post(
            "/memory/v1/resolve",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "scope": scope_body,
                "disagreement_id": disagreement["relationship_id"],
                "evidence_id": evidence["evidence_id"],
                "selected_fact_id": right,
                "reason": "A worker cannot grant itself resolution authority.",
            },
        )
        assert promoted.status_code == 403
        report["checks"].append(
            "sibling isolation, no scope widening and no worker self-promotion"
        )

        replay_session_id = uuid4()
        replay_session = MemorySession(
            MemoryClient(val, scope=scope, classification=Classification.INTERNAL),
            session_id=replay_session_id,
        )
        turn_id = uuid4()
        completed = ModelTurn(
            "Completed once.", (DurableObservation("A stable replay observation."),)
        )
        first = await replay_session.persist_turn(turn_id, completed)
        second = await replay_session.persist_turn(turn_id, completed)
        assert first.result["fact_ids"] == second.result["fact_ids"]
        assert str(second.status) == "replayed"
        restarted_session = MemorySession(
            MemoryClient(val, scope=scope, classification=Classification.INTERNAL),
            session_id=replay_session_id,
        )
        try:
            await restarted_session.persist_turn(
                turn_id,
                ModelTurn(
                    "Changed output.",
                    (
                        DurableObservation(
                            "Different output with the same turn identity."
                        ),
                    ),
                ),
            )
        except PersistenceFailure as failure:
            assert failure.response == "Changed output."
        else:
            raise AssertionError("A divergent retry was falsely reported as remembered")
        report["checks"].append(
            "exact replay is idempotent and divergent persistence fails visibly"
        )

    verified = verify_catalogue(config)
    report["catalogue_verification"] = asdict(verified)
    return report


def main() -> None:
    with TemporaryDirectory(prefix="cairn-shared-memory-demo-") as temporary:
        report = asyncio.run(demonstrate(Path(temporary)))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
