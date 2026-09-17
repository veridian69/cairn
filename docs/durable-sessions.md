# Restart-safe memory sessions

`DurableMemorySession` keeps bounded recovery state in Cairn's catalogue, not in
a host transcript file or local retry database. It is opt-in: existing
`MemorySession` behaviour has not changed. This guide covers the Python API;
[host workflow packages](host-memory-workflows.md) describe explicit installation
and its separate limits.

## What the state means

| State | Meaning | Recovery |
| --- | --- | --- |
| `open` | Session context exists; no selected turn | Begin an explicit turn |
| `started` / recovered `interrupted` | A generation attempt was claimed, but completed output was not durably prepared | Do not rerun automatically; explicitly abandon before a fresh replacement |
| `prepared` | Screened completed output and observations are durable | Resume the same turn without a model callback |
| `committed` | Normal ingest supplied actual fact custody and its receipts | Read or resume the same turn; do not add its observations again |
| `skipped` | Completed output contained no observations | No fact custody exists or is implied |
| `abandoned` | An unprepared attempt was explicitly fenced off | A replacement needs fresh turn and attempt identities |
| host `pending` / `failed` | The client could not confirm an operation, or received a refusal | Inspect safe failure metadata and current server state; neither label means saved |

An operational receipt acknowledges a session operation. It is not an ingest
receipt. Fact custody, index projection and usefulness of retrieval are different
claims: committed observations may not yet be searchable. Candidate observations
remain attributed candidate claims; storage does not make them verified facts.

Failures retain the actual failing operation and `last_confirmed_stage`. A
commit can ingest facts and then lose authority before recording its terminal
marker: even an authorisation denial at that point means pending reconciliation,
not proof that storage failed. Inspect current state when authorised again.
`completed_turn` in a failure is the original validated output held in that
process; it is not a replacement response supplied by a failed commit reply.

## Fixed connection and identities

Use an explicit [connection profile](memory-connection-profiles.md) for a
designated test Cairn. It fixes endpoint, expected instance, scope, classification
and credential-file location. Durable use also requires an explicit session UUID.
Do not silently generate a replacement profile/session after a failure.

The HTTP client owns its credential. Session responses bind instance, authenticated
principal, exact scope and immutable session classification; current server grants
are still required for every operation and replay. A diagnostic handshake alone
is not permission to perform a later write. Session requests also carry the
expected instance for a server-side check before authority execution.

Use canonical RFC UUIDs for session, turn and attempt identities. A new turn needs
a fresh attempt identity. Stable operation keys are derived by the durable client;
actual ingest retains the existing session/turn `remember_key` derivation. Keep
those identities in explicit caller context, not a locally mutated memory log.

## A complete synthetic turn

This code uses a supplied test profile and an explicit turn/attempt. Its callback
is deliberately a fixed synthetic example, not a claim that a host/model was
invoked. Do not point it at productive Cairn data.

```python
import asyncio
from pathlib import Path
from uuid import UUID

import httpx

from cairn.client import (
    DurableMemorySession,
    DurableObservation,
    MemoryClient,
    ModelTurn,
)
from cairn.client.profiles import load_credential, load_profile


async def main(profile_path: Path, turn_id: UUID, attempt_id: UUID) -> None:
    profile = load_profile(profile_path)
    if profile.session_id is None:
        raise ValueError("the test profile needs an explicit session_id")
    async with httpx.AsyncClient(
        base_url=profile.endpoint,
        headers={"Authorization": f"Bearer {load_credential(profile)}"},
        timeout=30.0,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        client = MemoryClient(
            http,
            scope=profile.scope,
            classification=profile.classification,
            expected_instance_id=profile.expected_instance_id,
        )
        session = DurableMemorySession(client, session_id=profile.session_id)
        await session.open()

        async def synthetic_callback(_turn_input):
            return ModelTurn(
                "Synthetic checkpoint: use calibration reference Q7.",
                (DurableObservation("Synthetic test decision: calibration uses Q7."),),
            )

        result = await session.run_turn(
            "Record the synthetic calibration decision.",
            synthetic_callback,
            turn_id=turn_id,
            attempt_id=attempt_id,
            relevant_only=True,
        )
        print(result.state)
        print(None if result.persistence is None else result.persistence.status.value)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--turn-id", required=True, type=UUID)
    parser.add_argument("--attempt-id", required=True, type=UUID)
    args = parser.parse_args()
    asyncio.run(main(args.profile, args.turn_id, args.attempt_id))
```

Only a newly committed begin permits the callback. A replayed or uncertain begin
does not authorise another generation. Preparation is bounded: response 32768
UTF-8 bytes, at most 8 observations of 4096 bytes each, and a 73728-byte canonical
preparation envelope. Invalid, oversized or screened output is rejected rather
than silently shortened.

## Resume and abandonment

Construct a fresh client/session with the same configured identity, then call:

```python
result = await session.resume(turn_id)
```

There is deliberately no callback argument. Prepared output is read from Cairn;
normal ingest replays its stable key across the ingest/finalisation crash gap.
The terminal response carries the actual assertion/fact IDs and mutation/audit
receipts. If only `started` survived, completed output cannot be reconstructed
from that marker. If the caller still has the identical completed checkpoint,
explicit preparation may recover it; otherwise do not pretend it was retained.

Only an unprepared started turn can be abandoned:

```python
await session.abandon(turn_id, reason="The host ended before preparing output.")
```

A later `run_turn` can name that turn as `replaces_turn_id`, with fresh turn and
attempt IDs. Abandonment fences late preparation; it is not deletion of history
and is not a command for closing a visit.

## Arrival is not acknowledgement

```python
arrival = await session.arrive("calibration decisions and unfinished work")
# Present and consume arrival.briefing before this separate explicit call:
visit_id = arrival.visit.snapshot.visit_id
assert visit_id is not None
await session.acknowledge_visit(visit_id)
```

A failed read, partial display or broken output pipe must leave the visit
unacknowledged. A topic change uses `client.recall(...)`, not another arrival or
an implicit acknowledgement. Acknowledgements advance a server-issued ordinal
monotonically. Equal-time selected changes are included conservatively; detected
clock rollback removes the timestamp filter and labels uncertainty. This is a
selected-memory briefing, not an exhaustive catalogue change feed.

## Reproduce the process boundary tests

From the native repository worktree:

```sh
uv run --locked pytest -q tests/client/test_session_process_restart.py --no-cov
```

These tests create synthetic catalogues and loopback servers. They stop and
restart actual server/client processes after preparation and after actual ingest
but before terminal recording, then compare real fact IDs/counts on recovery.
They do not call a provider, install a host skill or prove semantic recall quality.
