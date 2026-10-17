"""#108: the connector conventions, sent as the MCP ``instructions`` field.

Guidance text only. Scope stays an explicit, authorised argument on every
call; the configured realm is named here and never used as a default.
``docs/operations/browser-connectors.md`` carries the same block for
clients that ignore server instructions, and a test pins the two together.
"""

REALM_PLACEHOLDER = "<realm>"

TEMPLATE = """\
You are connected to the user's personal memory, Cairn.
Conventions for every memory tool call:
- scope: {"realm": "<realm>", "segments": []} on every memory tool call.
- classification: "internal" on every call whose tool takes one (remember, disagree, diagnose, session-open).
- idempotency_key: a fresh random UUID version 4 on every call that changes something (remember, disagree, resolve, correct and every other tool whose schema requires one). Reuse a key only to retry the same call, with identical arguments, after an uncertain response; never for a different call. Reads such as recall take none.
- When the user asks you to remember something, call remember with one short, self-contained fact per item in facts, each as {"body": "..."}. Do not add facts the user did not state.
- When the user asks about something they may have told you before, call recall with a short natural-language query before answering. Answer from what comes back; say so if nothing relevant is stored.
- Memories you store are candidate claims attributed to this connector; the user validates them separately."""

SCOPE_DESCRIPTION = (
    "The server never fills in a scope from its configuration. Its "
    "instructions name this deployment's realm when one is configured."
)


def connector_instructions(realm: str | None) -> str | None:
    """The instructions for ``realm``, or none when no realm is configured."""
    if realm is None:
        return None
    return TEMPLATE.replace(REALM_PLACEHOLDER, realm)
