from pathlib import Path
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_attributed_remember_recall_and_history(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    author, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        key = str(uuid4())
        remembered = await api.remember(
            "SQLite remains the catalogue authority.", key=key
        )
        assert remembered["outcome"] == "committed", remembered
        assert (await api.remember("SQLite remains the catalogue authority.", key=key))[
            "outcome"
        ] == "replayed"
        recall = await api.call("recall", {"scope": SCOPE, "query": "catalogue"})
        hit = recall["hits"][0]
        assert hit["source_principal_id"] == str(author)
        assert hit["source_type"] == "agent-claim"
        assert hit["trust"] == "candidate"
        assert hit["body"] == "SQLite remains the catalogue authority."
        history = await api.call("history", {"scope": SCOPE, "fact_id": hit["fact_id"]})
        assert history["facts"][0]["fact_id"] == hit["fact_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_relationships_resolution_correction_and_scope_isolation(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    author, token = instance.add_actor()
    _, outsider = instance.add_actor(
        segments=[{"kind": "repository", "identifier": "sibling"}]
    )
    async with serve(instance) as http:
        api = Api(http, token, transport)
        first = await api.remember(
            "Batch size should be 32.", evidence_payload="A controlled benchmark."
        )
        second = await api.remember("Batch size should be 64.")
        left, right = first["result"]["fact_ids"][0], second["result"]["fact_ids"][0]
        disagreement = await api.call(
            "disagree",
            {
                "scope": SCOPE,
                "left_fact_id": left,
                "right_fact_id": right,
                "classification": "internal",
                "reason": "Competing measurements.",
            },
            key=str(uuid4()),
        )
        did = disagreement["result"]["relationship_id"]
        resolved = await api.call(
            "resolve",
            {
                "scope": SCOPE,
                "disagreement_id": did,
                "evidence_id": first["result"]["evidence_id"],
                "selected_fact_id": left,
                "reason": "The controlled benchmark favours 32.",
            },
            key=str(uuid4()),
        )
        assert resolved["outcome"] == "committed", resolved
        recalled = await api.call("recall", {"scope": SCOPE, "query": "batch"})
        assert recalled["disagreements"][0]["principal_id"] == str(author)
        assert recalled["resolutions"][0]["selected_fact_id"] == left
        await api.call(
            "correct",
            {
                "fact_ids": [left],
                "reason": "Updated measurement.",
                "superseded_by": right,
            },
            key=str(uuid4()),
        )
        history = await api.call("history", {"scope": SCOPE, "fact_id": left})
        assert history["corrections"][0]["superseded_by"] == right
        denied = await Api(http, outsider, transport).call(
            "history", {"scope": SCOPE, "fact_id": left}
        )
        assert denied["failure"]["code"] == "authorisation_denied"
        assert left not in str(denied)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_secrets_and_authority_fields_are_refused(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(operations=["ingest", "retrieve"])
    async with serve(instance) as http:
        api = Api(http, token, transport)
        for field in ["source_type", "requested_trust", "source_principal_id"]:
            refused = await api.remember("A candidate.", **{field: "validated"})
            assert refused["failure"]["code"] == "invalid_request"
        for body in [
            {"scope": SCOPE, "query": "AKIAIOSFODNN7EXAMPLE"},
            {
                "scope": {
                    "realm": "acme",
                    "segments": [
                        {"kind": "repository", "identifier": "AKIAIOSFODNN7EXAMPLE"}
                    ],
                },
                "query": "safe",
            },
        ]:
            refused = await api.call("recall", body)
            assert refused["failure"]["code"] == "secret_rejected", refused
            assert "AKIAIOSFODNN7EXAMPLE" not in str(refused)
        refused = await api.call(
            "recall", {"scope": SCOPE, "query": "safe"}, key=str(uuid4())
        )
        assert refused["failure"]["detail"]["rule"] == "idempotency_key_forbidden"
        first = await api.remember("A candidate.")
        refused = await api.call(
            "correct",
            {
                "fact_ids": first["result"]["fact_ids"],
                "reason": "Cannot self-authorise.",
            },
            key=str(uuid4()),
        )
        assert refused["failure"]["code"] == "authorisation_denied"
