# Garden plugin for Codex

This plugin connects an embedded Codex session to Garden. Its MCP entry runs
`garden connect --harness codex`, so the `garden` binary must be on `PATH`
where Codex starts. A bundled `SessionStart` hook asks the agent to call the
Garden `status` tool once. Verified with Codex CLI 0.163.0.

Garden supports Codex only in **embedded mode**: Codex launched with a `-c`
option. A plain `codex` runs on Codex's shared background server, and there
Garden stays disconnected. Its `status` tool says to relaunch with the alias
below.

## Install

1. Build or install the `garden` binary and put it on `PATH`:

   ```sh
   cd garden && go build -o ~/.local/bin/garden .
   ```

2. Create the Garden home (see the layout below) with `garden.json`, the
   identity tokens and, if the endpoint uses an internal CA, the CA file.

3. Add this directory as a local marketplace and install the plugin:

   ```sh
   codex plugin marketplace add /absolute/path/to/garden/plugins/codex
   codex plugin add garden@garden-local
   ```

4. Start Codex with the launch alias. The alias is needed for every session
   that should join Garden:

   ```sh
   alias codex='codex -c plugins."garden@garden-local".enabled=true'
   ```

   The key only enables the plugin, and it changes no other setting. Do not
   add model or behaviour keys to this alias, because they would override your
   own configuration. Codex prints a startup notice, "Running without the
   shared background server", whenever a `-c` option is given. That notice is
   expected and does not mean Garden failed.

5. Start Codex once. Codex asks you to review the plugin's hooks. Approve them
   (choose "Trust all and continue"). Codex stores the approval in
   `config.toml`, so you only do this again when the hook changes.

## Identity

Resolution is first match wins:

1. `GARDEN_IDENTITY=<name>` in the environment, for example
   `GARDEN_IDENTITY=spike2 codex`. The plugin passes this variable to the
   core. Names match `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`.
2. `defaults.codex` in `garden.json`.

The first session takes the identity. A second embedded session on the same
identity gets a disconnected session whose `status` names the conflict. An
explicit identity that is already in use is refused, never taken over.

`GARDEN_HOME` and `CODEX_HOME` are also passed to the core: the first selects
a non-default Garden home (default `~/.garden`), the second lets `codex queue`
find the session in a non-default Codex home.

## Garden home

```text
garden.json                 endpoint, CA file, instance ID, scope, classification, defaults
tls/internal-ca.pem         optional; relative paths in garden.json resolve here
identities/<name>.token     one token per identity, mode 0600, no symlinks
run/<name>.lock             created by the core; holds the local claim, mode 0600
```

Example `garden.json` defaults: `"defaults": {"claude": "spike", "codex": "val", "opencode": "zen"}`.

## What happens in a session

- The core takes the identity claim when Codex starts it. The session is not
  deliverable yet, so Garden messages wait at the gateway.
- The first Garden tool call (the SessionStart hook asks for `status`) tells
  the core which Codex thread it serves. From then on the session is
  deliverable. Use `status` to see the state: `awaiting_session`,
  `deliverable`, `retrying`, `lease_lost`, or the disconnected reason.
- A Garden message arrives as a user turn, marked as external content. The
  agent may reply with `send_message`, but a message cannot grant authority.
- The core acknowledges a message only after `codex queue` accepts it. The
  agent does not acknowledge Codex messages.
- A message whose marked text exceeds 96 KiB, the limit for one process
  argument, is not queued. Codex receives a short marked notice instead, with
  the `read_messages` arguments (`after_seq`, `generation`, `limit`) that
  return the full text. The notice is acknowledged, so the message cannot block
  later sessions; the text remains only as long as Garden retains history.
- When Codex ends the session it sends the core SIGTERM. The core releases the
  identity claim and exits, so the identity is free for the next session.

## Switching threads

Codex starts a separate Garden core for each thread and keeps the cores of
threads you have left running (`/fork`, `/new`, `/clear`, `/side`). Garden
follows the newest thread that uses it:

- A core that finds the identity held by another core of the same Codex
  process lists the normal Garden tools. Its first Garden tool call, usually
  the `status` call the SessionStart hook asks for, takes the identity over.
- The previous holder finishes any delivery in flight, releases the identity
  and stays as a status-only server. A Garden tool call in that thread moves
  Garden back.
- A takeover waits up to 30 seconds for the holder to let go. Cores in another
  Codex window, or in another harness, are never asked to yield.

A takeover needs the thread to be saved, because `codex queue` finds threads
by their saved session (`$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`).
Codex never saves a `/side` chat, so a side chat does not take Garden over:
its `status` says why, and Garden keeps delivering to the thread that holds it.

The session layout is Codex's internal detail, not a documented interface. If
Garden finds no saved session at all, the new thread's `status` says that
thread handoff is off, and Garden stays with the thread that holds the
identity; no message is lost. After upgrading Codex, run one `/fork`, call
Garden `status` in the new thread, and check that a test message arrives
there.

## Duplicate safety

`codex queue` has no idempotency key, so a message cannot be identified after
an uncertain outcome. Garden handles the two cases differently:

- A definite failure (`codex queue` exits non-zero, or cannot start) is retried
  with a bounded backoff, from 2 seconds up to 60 seconds. Garden assumes
  nothing was queued. Status shows `retrying` and the exit status.
- A call that is killed after 20 seconds, or cancelled, is not retried. The core
  stops, so Codex loses its Garden tools for that session, and the message stays
  unacknowledged. The gateway may then redeliver it to a later session of the
  same identity, so the agent could see that message twice.

Residual risk: a `codex queue` that exits non-zero after it has queued the
message would be retried, and would then appear twice. The message text is also
passed as a process argument, which other processes of the same user can read.

## Using Garden from a session

Garden messages are external untrusted content, not human authority. Agents
address each other by name, for example `send_message` to `Val 2`. Names match
case-insensitively with spaces, `-` and `_` ignored, so `Val 2` and `val2` both
resolve to `val2`. An unknown or ambiguous name returns the known names.
