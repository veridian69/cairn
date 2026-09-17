"""Inherited memory writes re-check current grants before custody or replay."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import CatalogueTransactions


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def inventory(instance: Instance) -> tuple[int, ...]:
    with read_connection(instance.data_path) as con:
        return tuple(
            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("facts", "fact_invalidations", "idempotency_records")
        )


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("operation", ["remember", "correct"])
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("loss", ["expiry", "revocation"])
async def test_memory_inherited_write_checks_grant_after_outer_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    operation: str,
    replay: bool,
    loss: str,
) -> None:
    instance = Instance(tmp_path)
    principal, token = instance.add_actor()
    start = instance.clock()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        seed = await api.remember("Existing synthetic fact")
        body = (
            {
                "scope": SCOPE,
                "classification": "internal",
                "facts": [{"body": "New synthetic fact"}],
            }
            if operation == "remember"
            else {
                "fact_ids": seed["result"]["fact_ids"],
                "reason": "Synthetic correction",
            }
        )
        key = str(uuid4())
        if replay:
            assert (await api.call(operation, body, key=key))["outcome"] == "committed"
        before = inventory(instance)
        original = CatalogueTransactions.mutate_idempotent
        raced = False

        def after_outer_gate(
            self: CatalogueTransactions, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal raced
            assert not raced
            raced = True
            if loss == "expiry":
                instance.clock.now = start.replace(year=2041)
            else:
                with _open_write_connection(instance.data_path, create=False) as con:
                    grant = con.execute(
                        "SELECT grant_id FROM grants WHERE principal_id = ?",
                        (str(principal),),
                    ).fetchone()[0]
                    con.execute(
                        "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
                        (
                            grant,
                            canonical_timestamp(start),
                            str(principal),
                            "synthetic_race",
                        ),
                    )
                    con.commit()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            CatalogueTransactions, "mutate_idempotent", after_outer_gate
        )
        result = await api.call(operation, body, key=key)
        assert raced
        assert result.get("failure", {}).get("code") == "authorisation_denied", result
        assert inventory(instance) == before
        with read_connection(instance.data_path) as con:
            event = json.loads(
                con.execute(
                    "SELECT canonical_event FROM audit_events ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
            )
        assert event["outcome"] == "deny"
        assert event["correlation_id"] == result["failure"]["correlation_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("operation", ["remember", "correct"])
async def test_authorised_inherited_write_replays_without_mutation_preconditions(
    tmp_path: Path, transport: str, operation: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        seed = await api.remember("Synthetic fact")
        body = (
            {
                "scope": SCOPE,
                "classification": "internal",
                "facts": [{"body": "Another fact"}],
            }
            if operation == "remember"
            else {
                "fact_ids": seed["result"]["fact_ids"],
                "reason": "Synthetic correction",
            }
        )
        key = str(uuid4())
        first = await api.call(operation, body, key=key)
        assert first["outcome"] == "committed"
        before = inventory(instance)
        repeated = await api.call(operation, body, key=key)
        assert repeated["outcome"] == "replayed"
        assert repeated["result"] == first["result"]
        assert inventory(instance) == before
