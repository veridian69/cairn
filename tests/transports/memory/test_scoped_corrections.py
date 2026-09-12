"""REST and MCP share the additive host-scope correction boundary."""

from pathlib import Path
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_scoped_correction_replays_real_result_but_refuses_wrong_scope(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token, transport)
        seed = await api.remember("Synthetic correction source")
        body = {
            "scope": SCOPE,
            "fact_ids": seed["result"]["fact_ids"],
            "reason": "Measured correction",
        }
        key = str(uuid4())
        first = await api.call("correct", body, key=key)
        assert first["outcome"] == "committed"
        replay = await api.call("correct", body, key=key)
        assert replay["outcome"] == "replayed"
        assert replay["result"] == first["result"]
        assert replay["mutation_receipt"] == first["mutation_receipt"]
        denied = await api.call(
            "correct", {**body, "scope": {"realm": "acme", "segments": []}}, key=key
        )
        assert denied["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_scoped_correction_refuses_sibling_replacement(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor(segments=[])
    async with serve(instance) as http:
        api = Api(http, token, transport)
        old = await api.remember("Synthetic old thought")
        sibling = await api.remember(
            "Unrelated sibling",
            scope={
                "realm": "acme",
                "segments": [{"kind": "repository", "identifier": "other"}],
            },
        )
        denied = await api.call(
            "correct",
            {
                "scope": SCOPE,
                "fact_ids": old["result"]["fact_ids"],
                "superseded_by": sibling["result"]["fact_ids"][0],
                "reason": "Do not widen scope",
            },
            key=str(uuid4()),
        )
        assert denied["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_legacy_scope_omission_stays_compatible(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        api = Api(http, token, transport)
        seed = await api.remember("Legacy synthetic source")
        result = await api.call(
            "correct",
            {"fact_ids": seed["result"]["fact_ids"], "reason": "Legacy correction"},
            key=str(uuid4()),
        )
        assert result["outcome"] == "committed"
