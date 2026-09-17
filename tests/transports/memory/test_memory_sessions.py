"""Public sessions preserve real authority, custody and REST/MCP parity."""

from pathlib import Path
from uuid import uuid4

import pytest
from memory_support import SCOPE, Api, Instance, serve

from cairn.catalogue.sqlite import (
    _open_write_connection,
    canonical_timestamp,
    read_connection,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_preparation_is_private_and_commit_returns_actual_custody(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    _, other = instance.add_actor()
    session, turn, attempt = map(str, (uuid4(), uuid4(), uuid4()))
    context = {
        "scope": SCOPE,
        "session_id": session,
        "expected_instance_id": str(instance.config.instance_id),
    }
    async with serve(instance) as http:
        api = Api(http, token, transport)
        opened = await api.call(
            "session-open", {**context, "classification": "internal"}, key=str(uuid4())
        )
        assert opened.get("outcome") == "committed", opened
        begun = await api.call(
            "turn-begin",
            {**context, "turn_id": turn, "attempt_id": attempt},
            key=str(uuid4()),
        )
        assert begun["result"]["state"] == "started"
        prepared = await api.call(
            "turn-prepare",
            {
                **context,
                "turn_id": turn,
                "attempt_id": attempt,
                "response": "The chosen port is 8123.",
                "observations": [{"body": "The port is 8123."}],
            },
            key=str(uuid4()),
        )
        assert prepared["result"]["state"] == "prepared"
        assert prepared["result"]["custody_receipt"] is None
        denied = await Api(http, other, transport).call(
            "session-read", {**context, "turn_id": turn}
        )
        assert denied["failure"]["code"] == "authorisation_denied"
        assert "8123" not in str(denied)
        key = str(uuid4())
        committed = await api.call("turn-commit", {**context, "turn_id": turn}, key=key)
        assert committed["result"]["state"] == "committed", committed
        value = committed["result"]
        assert value["custody_receipt"] != value["operational_receipt"]
        assert len(value["custody_result"]["fact_ids"]) == 1
        replay = await api.call("turn-commit", {**context, "turn_id": turn}, key=key)
        assert replay["outcome"] == "replayed"
        assert replay["result"] == value
        recovered = await api.call("session-read", {**context, "turn_id": turn})
        assert recovered == value


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_visit_requires_explicit_ack_and_wrong_instance_cannot_open(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    context = {
        "scope": SCOPE,
        "session_id": str(uuid4()),
        "expected_instance_id": str(instance.config.instance_id),
    }
    async with serve(instance) as http:
        api = Api(http, token, transport)
        wrong = await api.call(
            "session-open",
            {
                **context,
                "classification": "internal",
                "expected_instance_id": str(uuid4()),
            },
            key=str(uuid4()),
        )
        assert wrong.get("failure", {}).get("code") == "authorisation_denied", wrong
        opened = await api.call(
            "session-open", {**context, "classification": "internal"}, key=str(uuid4())
        )
        assert opened["outcome"] == "committed"
        visit = await api.call("visit-issue", context, key=str(uuid4()))
        assert visit["result"]["acknowledged_watermark"] == 0
        read = await api.call("session-read", context)
        assert read["acknowledged_watermark"] == 0
        ack = await api.call(
            "visit-acknowledge",
            {**context, "visit_id": visit["result"]["visit_id"]},
            key=str(uuid4()),
        )
        assert ack["result"]["acknowledged_watermark"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("loss", ["revoked", "expired"])
async def test_current_grant_loss_denies_read_and_mutation_replay(
    tmp_path: Path, transport: str, loss: str
) -> None:
    instance = Instance(tmp_path)
    owner, token = instance.add_actor()
    context = {
        "scope": SCOPE,
        "session_id": str(uuid4()),
        "expected_instance_id": str(instance.config.instance_id),
    }
    key = str(uuid4())
    async with serve(instance) as http:
        api = Api(http, token, transport)
        assert (
            await api.call(
                "session-open", {**context, "classification": "internal"}, key=key
            )
        )["outcome"] == "committed"
        if loss == "revoked":
            with _open_write_connection(instance.data_path, create=False) as con:
                con.execute(
                    "INSERT INTO grant_revocations (grant_id, revoked_at, reason_code) SELECT grant_id, ?, 'test_revocation' FROM grants WHERE principal_id=?",
                    (canonical_timestamp(instance.clock()), str(owner)),
                )
                con.commit()
        else:
            instance.clock.now = instance.clock.now.replace(year=2041)
        for name, body, replay_key in [
            ("session-read", context, None),
            ("session-open", {**context, "classification": "internal"}, key),
        ]:
            denied = await api.call(name, body, key=replay_key)
            assert denied["failure"]["code"] == "authorisation_denied", denied
            assert str(context["session_id"]) not in str(denied)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_abandonment_fences_late_prepare_and_empty_commit_skips(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    context = {
        "scope": SCOPE,
        "session_id": str(uuid4()),
        "expected_instance_id": str(instance.config.instance_id),
    }
    tid, attempt = str(uuid4()), str(uuid4())
    async with serve(instance) as http:
        api = Api(http, token, transport)
        await api.call(
            "session-open", {**context, "classification": "internal"}, key=str(uuid4())
        )
        await api.call(
            "turn-begin",
            {**context, "turn_id": tid, "attempt_id": attempt},
            key=str(uuid4()),
        )
        abandoned = await api.call(
            "turn-abandon",
            {**context, "turn_id": tid, "reason": "Interrupted"},
            key=str(uuid4()),
        )
        assert abandoned["result"]["state"] == "abandoned"
        late = await api.call(
            "turn-prepare",
            {
                **context,
                "turn_id": tid,
                "attempt_id": attempt,
                "response": "Late",
                "observations": [],
            },
            key=str(uuid4()),
        )
        assert late["failure"]["code"] == "idempotency_conflict"
        replacement = str(uuid4())
        await api.call(
            "turn-begin",
            {
                **context,
                "turn_id": replacement,
                "attempt_id": str(uuid4()),
                "replaces_turn_id": tid,
            },
            key=str(uuid4()),
        )
        read = await api.call("session-read", {**context, "turn_id": replacement})
        await api.call(
            "turn-prepare",
            {
                **context,
                "turn_id": replacement,
                "attempt_id": read["attempt_id"],
                "response": "",
                "observations": [],
            },
            key=str(uuid4()),
        )
        terminal = await api.call(
            "turn-commit", {**context, "turn_id": replacement}, key=str(uuid4())
        )
        assert terminal["result"]["state"] == "skipped"
        assert terminal["result"]["response"] == ""
        assert terminal["result"]["custody_result"] is None
        with read_connection(instance.data_path) as con:
            assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_session_wire_refusal_is_audited_without_raw_content(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path)
    _, token = instance.add_actor()
    async with serve(instance) as http:
        refused = await Api(http, token, transport).call(
            "session-open",
            {
                "scope": SCOPE,
                "session_id": str(uuid4()),
                "expected_instance_id": str(instance.config.instance_id),
                "classification": "internal",
                "PRIVATE-RAW-FIELD": "PRIVATE-RAW-VALUE",
            },
            key=str(uuid4()),
        )
        assert refused["failure"]["code"] == "invalid_request"
        with read_connection(instance.data_path) as con:
            rows = con.execute(
                "SELECT canonical_event FROM audit_events WHERE action_code='memory-session-open' AND outcome='deny'"
            ).fetchall()
        assert len(rows) == 1
        assert "PRIVATE-RAW" not in str(rows)
