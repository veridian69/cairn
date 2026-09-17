"""Consumer-level shared-memory story over both authenticated transports."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_three_principals_keep_both_accounts_then_explain_correction(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    val_id, val_token = instance.add_actor(
        operations=["ingest", "retrieve"], label="val"
    )
    spike_id, spike_token = instance.add_actor(
        operations=["ingest", "retrieve"], label="spike"
    )
    _, third_token = instance.add_actor(operations=["retrieve"], label="third-agent")
    _, human_token = instance.add_actor(label="operator")
    async with serve(instance) as http:
        val = Api(http, val_token, transport)
        spike = Api(http, spike_token, transport)
        third = Api(http, third_token, transport)
        human = Api(http, human_token, transport)
        old = (await val.remember("The shared collector uses port 7000."))["result"][
            "fact_ids"
        ][0]
        new = (await spike.remember("The shared collector uses port 7001."))["result"][
            "fact_ids"
        ][0]
        disagreement = await spike.call(
            "disagree",
            {
                "scope": SCOPE,
                "left_fact_id": old,
                "right_fact_id": new,
                "classification": "internal",
                "reason": "The observations disagree about the listening port.",
            },
            key=str(uuid4()),
        )
        assert disagreement["outcome"] == "committed"
        recalled = await third.call("recall", {"scope": SCOPE, "query": "collector"})
        assert {
            hit["fact_id"]: hit["source_principal_id"] for hit in recalled["hits"]
        } == {
            old: str(val_id),
            new: str(spike_id),
        }
        assert {hit["trust"] for hit in recalled["hits"]} == {"candidate"}
        assert len(recalled["disagreements"]) == 1
        assert recalled["resolutions"] == []

        changed = await human.call(
            "correct",
            {
                "fact_ids": [old],
                "superseded_by": new,
                "reason": "The operator measured port 7001.",
            },
            key=str(uuid4()),
        )
        assert changed["outcome"] == "committed"
        current = await third.call("recall", {"scope": SCOPE, "query": "collector"})
        assert old not in {hit["fact_id"] for hit in current["hits"]}
        history = await third.call("history", {"scope": SCOPE, "fact_id": new})
        assert {hit["fact_id"] for hit in history["facts"]} == {old, new}
        assert history["corrections"][0]["reason"] == "The operator measured port 7001."


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_disclosure_budget_measures_the_records_consumers_receive(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        first = await api.remember("Mémoire: the setting is one.")
        second = await api.remember("Mémoire: the setting is two.")
        await api.call(
            "disagree",
            {
                "scope": SCOPE,
                "left_fact_id": first["result"]["fact_ids"][0],
                "right_fact_id": second["result"]["fact_ids"][0],
                "classification": "internal",
                "reason": "Two different recollections remain unresolved.",
            },
            key=str(uuid4()),
        )
        for budget in (16384, 1000):
            result = await api.call(
                "recall", {"scope": SCOPE, "query": "mémoire", "budget": budget}
            )
            actual = sum(
                len(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                for group in ("hits", "disagreements", "resolutions")
                for record in result[group]
            )
            assert result["budget_consumed"] == actual
            assert actual <= budget
            assert all(hit["has_disagreement"] for hit in result["hits"])
            if budget == 1000:
                assert len(result["hits"]) == 1
                assert result["hits"][0]["disagreement_context_incomplete"] is True
                assert result["disagreements"] == []
                assert result["budget_exhausted"] is True
            else:
                assert len(result["hits"]) == 2
                assert len(result["disagreements"]) == 1
                assert all(
                    hit["disagreement_context_incomplete"] is False
                    for hit in result["hits"]
                )
