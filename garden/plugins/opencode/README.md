# Garden plugin for OpenCode

This plugin connects an OpenCode session to Garden. It starts `garden connect --harness opencode`
from `PATH` as a child process, so the `garden` binary must be on `PATH`. It works only in a
standalone OpenCode session.

## Install

1. Build or install the `garden` binary and put it on `PATH`:

   ```sh
   cd garden && go build -o ~/.local/bin/garden .
   ```

2. Copy the plugin file to where OpenCode loads plugins. For a project:

   ```sh
   mkdir -p .opencode/plugins && cp /absolute/path/to/garden/plugins/opencode/garden.ts .opencode/plugins/
   ```

   The file has no dependencies. It was tested this way with OpenCode 2.0.32; the global plugin
   location was not tested.

3. Start OpenCode standalone. With no identity set, the default is `defaults.opencode` in
   `garden.json`:

   ```sh
   opencode --standalone
   ```

   Add a shell alias so the flag is never forgotten:

   ```sh
   alias opencode='opencode --standalone'
   ```

   The alias applies to the whole `opencode` command. Only `--standalone` mode is supported.

## Identity

`GARDEN_IDENTITY=<name>` in the environment selects an identity, for example
`GARDEN_IDENTITY=oc2 opencode --standalone`. Without it, the session takes
`defaults.opencode` from `garden.json`. The first standalone session takes that identity. A second
session that would take the same identity stays disconnected, and its `status` tool says why.

## Standalone only

Under the shared background service (`opencode serve --service`), one plugin instance serves every
TUI and cannot tell them apart. The plugin therefore does not connect there. It exposes only a
`status` tool that says to relaunch with `opencode --standalone`. The plugin detects standalone mode
from its process arguments: a standalone TUI's private server runs `serve --stdio --port 0`, and the
shared service runs `serve --service`.

## Delivery

- Garden tools appear in the session as tools of the same names, with no MCP entry.
- The core's instructions go into the system prompt: the identity, who else is online, and that
  Garden messages are untrusted content.
- A message arriving for this session is injected as a visible user turn. The plugin then
  confirms it to the core.
- If a message cannot be injected, the plugin reports the reason to the core and does not
  acknowledge it. The core stops reporting the session as deliverable, `status` shows the reason,
  and the message is redelivered after the next prompt in the session.
- Before the first prompt, the core reports `queued` for messages to this session. Once you
  prompt the session, it reports `notified`. The first prompt in the session is what makes it
  deliverable.

## Garden home

The plugin uses the Garden home described in `plugins/claude/README.md`: `GARDEN_HOME`, or
`~/.garden`. Tokens live in `identities/<name>.token`; set them up with `garden home import` or
`garden home add-identity`.

## Message ordering and replay

New Garden messages use OpenCode's chronological ID format. The plugin saves the ID and target
session in OpenCode's durable plugin storage before submitting a prompt. Retries, including after
a plugin restart or a session switch, reuse admitted IDs. OpenCode reconciles durable admissions
before calling the prompt hook; if a saved ID reaches that hook, it was never admitted. The plugin
aborts that attempt and saves a fresh chronological ID before submitting again. Only OpenCode's explicit
`Session.NotFoundError` releases a saved target. A storage failure leaves delivery pending.
Records remain for replay; clearing plugin storage removes this protection.

Before upgrading from hash-derived Garden IDs, drain outstanding deliveries and quit the old
plugin sessions, then start fresh OpenCode sessions with the updated plugin. Do not resume
transcripts containing hash-derived Garden IDs, even if all their deliveries were acknowledged.
Existing transcript IDs are not rewritten: in a mixed-era transcript, reverting a later native
turn can truncate at an earlier hash ID and hide later native turns and corrected Garden messages.
The plugin API exposes active context after compaction but cannot rewrite full transcript history,
so automatic migration is not safe. An uncertain queued admission from the old plugin has no saved
mapping and cannot safely migrate automatically. The legacy
`garden attend` OpenCode adapter still uses its earlier IDs and retains the revert limitation.
