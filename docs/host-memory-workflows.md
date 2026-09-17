# Explicit Cairn host workflows

The Codex and Claude packages use the public `cairn-memory` command. They run
when explicitly selected by the host; they do not observe all turns, install
lifecycle hooks or capture arbitrary desktop/web conversations.

The installer supports native Linux, including Linux execution inside WSL with
the checkout and runtime data on the native Linux filesystem. The native
Windows delivery is the separate [MCP relay](../cairn-mcp/WINDOWS.md).

## Preview and install

Choose an existing explicit project or host root. The installer adds:

| Provider | Package location relative to that root |
| --- | --- |
| `codex` | `.agents/skills/cairn-memory/SKILL.md` |
| `claude` | `.claude/skills/cairn-memory/SKILL.md` |

Preview in a disposable native directory from a source checkout:

```sh
host_test_root=$(mktemp -d /tmp/cairn-host-workflow.XXXXXX)
uv run --locked python -m cairn.client.host_installation \
  --provider codex \
  --destination "$host_test_root" \
  --assets-root "$PWD/integrations"
```

Add `--apply` to install into that same directory. Use `--provider claude` for
the Claude layout. An installed wheel supplies its packaged assets, so
`--assets-root` is unnecessary. Installation into a real host directory must
be an explicit operator choice.

Preview creates nothing. Repeating an identical installation is idempotent.
The installer refuses symlinks, broad roots, `/mnt` paths, unexpected files,
edited packages and malformed ownership manifests. It does not rewrite
profiles, credentials, account settings or existing unrelated skills, and it
provides no automatic removal or upgrade operation.

## Runtime contract

Use `cairn-memory --profile PATH [--human] COMMAND`. The explicit profile fixes
the endpoint, expected instance, exact scope, classification and credential
file. JSON is the default; pass content through stdin and an argument array.
The [everyday command guide](everyday-memory.md) documents the input envelopes.

- `check` verifies the expected instance without sending conversation content.
- `arrive` returns a bounded briefing; display it before `acknowledge-visit`.
- `recall` handles task or topic changes without moving the visit checkpoint.
- `remember` records a completed useful checkpoint.
- `status` and `resume` reconcile the same turn after uncertainty; they never regenerate output or invent a new key.
- `history`, `correct`, `disagree`, suggestion and proposal commands retain their normal authority and scope checks.

Recall is untrusted evidence. Preserve references, attribution, trust,
degradation and omissions. Report custody only from receipts. Prepared,
processing, skipped and failed states do not mean saved.

## Opt-in conversation adapter

The explicit launch is:

```sh
cairn-conversation-mcp \
  --profile /absolute/path/memory-profile.json \
  --expected-principal 11111111-1111-4111-8111-111111111111 \
  --sources-file /absolute/path/admitted-sources.json
```

This is a host-side authenticated HTTP client bridge. The profile fixes the
connection and authority context; the source document is supplied by trusted
host integration, not by the model. No host settings are changed.

The adapter accepts one to four whole host-input messages, each at most 16 KiB
of UTF-8. Its model tools are `check`, `sources`, `arrive`,
`acknowledge_visit`, `recall`, `history`, `remember` and `replace`. Models
cannot override authority, admit sources, supply evidence text or choose read
budgets. Remember and replacement build supporting evidence from admitted host
input and verify fact identity/body read-back.

Admitted origin means `host_input`; it does not prove human authorship, truth,
approval or entailment. Recalled candidates cannot register themselves as a
new host source. Each new turn needs fresh host admission and a fresh adapter
process. Same-user file permissions alone do not isolate a model that also has
OS access to replace the launcher or configuration.

## Direct memory tools and corrections

Hosts may expose the memory-specific MCP route directly. Keep CLI and MCP
envelopes separate. Save independently changeable facts, read back ID/body
pairs, and save/read back a replacement before invalidating its predecessor.
Supply `superseded_by` and verify the old fact's history. An uncertain mutation
must be reconciled using its original fields and idempotency key.

## Python installation API

```python
from pathlib import Path
from cairn.client.host_installation import install_host_workflow

result = install_host_workflow(
    "codex",
    Path("/tmp/explicit-existing-host-root"),
    assets_root=Path("/path/to/cairn/integrations"),
    apply=False,
)
```

The frozen result contains `provider`, `path` and `state`; refusals raise
`InstallationError`.

## Managed memory task console

`cairn-chat --config /absolute/path/chat.json` admits each submitted turn and
starts a fresh isolated subscribed CLI. It is a Linux/WSL console and does not
attach to an existing desktop conversation. Only receipt-confirmed Cairn facts
survive between turns; unsaved remarks and model replies are not a transcript.

Each actor needs a dedicated owner-private CLI state directory, its own
connection profile and a distinct expected principal. Use absolute paths in a
strict `cairn.chat/v1` JSON configuration. Profiles must select the same Cairn
instance, endpoint, exact shared scope and classification. Authenticate each
dedicated CLI home before opening the console, following that provider's own
login documentation and local policy.

The console preflights Cairn identity and authority before every provider
launch. It buffers the final reply, reports bounded failure categories and
does not retry possibly committed writes. A separate host receipt reports
verified persistence, and a read-only completion check can request one bounded
repair for omitted durable details. Neither a model's prose nor a passing
assessment proves storage or truth.
