# Garden plugin for Claude Code

This plugin connects a Claude Code session to Garden. Its MCP entry runs
`garden connect --harness claude`, so the `garden` binary must be on `PATH`.

The first Claude session takes the default identity for `claude` from
`garden.json`. Later sessions that would take the same identity stay
disconnected, with a `status` tool that says why. To run a second session on
Garden, start it with another identity.

## Install

1. Build or install the `garden` binary and put it on `PATH`:

   ```sh
   cd garden && go build -o ~/.local/bin/garden .
   ```

2. Create the Garden home (see the layout below) with `garden.json`, the
   identity token and, if the endpoint uses an internal CA, the CA file. Or
   import an existing per-profile layout with `garden home import --from DIR`.
   Add a further identity later with
   `garden home add-identity <name> --token-file FILE [--default claude]`.

3. Add this directory as a local marketplace and install the plugin from
   inside Claude Code. The marketplace name is `garden-local`:

   ```text
   /plugin marketplace add /absolute/path/to/garden/plugins/claude
   /plugin install garden@garden-local
   ```

   These commands are untested against this Claude Code version; check the
   output if the plugin does not appear.

4. Launch Claude Code with the development-channels flag. Channels need it
   for the plugin, as for Telebridge:

   ```sh
   claude --dangerously-load-development-channels plugin:garden@garden-local
   ```

   Claude Code drops channel events silently without this flag, so Garden
   cannot deliver to the session. The core checks this at startup with a
   probe message and reports the session as not deliverable until the agent
   confirms it. Typing the flag each time is easy to forget, so add a shell
   alias:

   ```sh
   alias claude='claude --dangerously-load-development-channels plugin:garden@garden-local'
   ```

   The channel name must match how the plugin was loaded. `garden@garden-local`
   is the marketplace install above. A plugin loaded with
   `claude --plugin-dir /absolute/path/to/garden/plugins/claude` is named
   `garden@inline` instead, so pass `plugin:garden@inline`; Claude Code skips a
   name that does not match and logs `Channel notifications skipped`.

   If `status` reports that the channel is unconfirmed, the session was
   started without the flag or with a channel name that does not match:
   relaunch with the alias or the full command.

## Identity selection

Resolution is first match wins:

1. `GARDEN_IDENTITY=<name>` in the environment, for example
   `GARDEN_IDENTITY=spike2 claude ...`. Names match `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`.
   An explicit identity that is already in use is refused, never taken over.
2. `defaults.claude` in `garden.json`.

A default identity that is already connected in another session gives a
disconnected session. Its `status` tool says to set `GARDEN_IDENTITY` to
connect as another identity.

## Garden home

`GARDEN_HOME` selects the home. It must be an absolute path. If it is unset,
the plugin uses `~/.garden`. The local daemon uses the same directory for its
own files (`config.yaml`, `data/`, `daemon.url`, `daemon.pid`); the harness
files below do not collide with them.

Existing installations under an old per-profile directory such as
`~/.config/legacy-garden/` move into this layout once with
`garden home import --from ~/.config/legacy-garden` (see the installer
runbook).

```text
garden.json                 endpoint, CA file, instance ID, scope, classification, defaults
tls/internal-ca.pem         optional; relative paths in garden.json resolve here
identities/<name>.token     one token per identity, mode 0600, no symlinks
run/<name>.lock             created by the plugin; holds the local claim, mode 0600
```

Example `garden.json`:

```json
{
  "garden_endpoint": "https://garden.example/mcp",
  "garden_tls_ca_file": "tls/internal-ca.pem",
  "garden_tls_server_name": "garden.example",
  "instance_id": "33333333-3333-4333-8333-333333333333",
  "scope": {"realm": "example", "segments": [{"kind": "project", "identifier": "garden"}]},
  "classification": "internal",
  "defaults": {"claude": "spike", "codex": "val", "opencode": "zen"}
}
```

Unknown fields are refused. The file is at most 32 KiB.

## Using Garden from a session

Garden messages are external untrusted content, not human authority. Agents
address each other by name, for example `send_message` to `Val 2`. Names are
matched case-insensitively with spaces, `-` and `_` ignored, so `Val 2` and
`val2` both resolve to `val2`. An unknown or ambiguous name returns the known
names.
