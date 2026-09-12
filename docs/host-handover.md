# Subscribed CLI conversation adapter

`scripts/host_handover.py` supplies a reusable `CliHosts.ask(host, task,
context)` callback and an opt-in synthetic handover. The callback returns a
bounded `ModelTurn`; `MemorySession` owns recall and persistence. It targets
subscribed Linux Codex and Claude CLIs and does not require a second memory
database.

## Run safely

```sh
uv run --locked python scripts/host_handover.py --help
uv run --locked python scripts/host_handover.py preflight
uv run --locked python scripts/host_handover.py self-test
```

`preflight` checks selected provider login-file shape, provider executables that respond to `--version`
and managed-policy conflicts. It prints neither credentials nor their paths and
makes no model request. Login-file presence does not prove that the token is
current or a model is available.

`self-test` uses deterministic callbacks and no provider invocation. The
separate `run` mode invokes real subscribed CLIs, consumes provider allowance
and may update or rotate a provider session. Read its help and use only an
explicitly authorised disposable setup. Its result is a bounded synthetic
integration observation, not production or universal prompt acceptance.

## Isolation boundary

Each callback creates an owner-private temporary home/configuration and copies
only the selected provider login file. It excludes ambient API keys, alternate
provider endpoints, Cairn credentials, plugins, MCP servers and memory state.
Temporary copies and CLI scratch state are removed after the callback; changes
are not copied back.

This is configuration isolation. It is not a filesystem jail or network
firewall, and an OAuth copy is not an independent provider session. Managed
policy can override local options. Use the Python runner rather than extracting
its generated CLI arguments as a standalone security recipe.

The synthetic handover uses distinct workload principals with only the intended
scope and grants. Model callbacks receive attributed Cairn recall, never Cairn
credentials or another provider's transcript. Candidate custody, replay and
sibling-scope denial are checked against a disposable catalogue.

## Reuse in an application

Keep one `MemoryClient` and `MemorySession` for the conversation. The operator
must supply the authorised endpoint, expected instance, exact scope,
classification and authenticated HTTP client. Call `client.diagnose()` before
sending recalled material to a provider; every later operation still undergoes
current server authorisation.

```python
from uuid import uuid4

from cairn.client import MemorySession, PersistenceFailure


async def run_conversation(client, invoke_model, inputs):
    session = MemorySession(client, session_id=uuid4())
    briefing = await session.arrive(
        "Relevant decisions, unfinished work and disagreements"
    )
    print({"summary": briefing.summary, "warnings": briefing.warnings})

    for user_input in inputs:
        turn_id = uuid4()
        try:
            result = await session.run_turn(
                user_input,
                invoke_model,
                turn_id=turn_id,
                relevant_only=True,
            )
        except PersistenceFailure as failure:
            print({"response": failure.response, "persistence": "unconfirmed"})
            raise
        print(
            {
                "response": result.response,
                "persistence": result.persistence.status.value,
            }
        )
```

Pass recall as labelled untrusted data, not system/developer instructions. Only
the host receipt controls whether a turn was remembered. `processing` and
`failed` mean custody is unconfirmed; `skipped` means no observations were
submitted. Searchability and trust remain separate from custody.

For full host-source admission and managed-console behaviour, see
[Explicit Cairn host workflows](host-memory-workflows.md).
