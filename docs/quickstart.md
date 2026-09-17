# Disposable quickstart

The fastest way to see Cairn working is the guided installer in **disposable**
mode. It installs one throwaway **Attic only** instance on numeric loopback,
proves an authenticated identity, an exact Attic write/read round-trip and
retention across a restart, then stops the process. Nothing listens beyond
`127.0.0.1`, and no model provider is called. Disposable mode does not offer
semantic search; choose persistent native or Docker for that.

For a Cairn that keeps running, use the [quick install](install.md#quick-install-with-the-guided-installer).
For the full installer reference, see [guided installation](operations/guided-installation.md).

## Prerequisites

- Linux `x86_64`, with the checkout on a native Linux filesystem (not `/mnt/c`),
  run as an ordinary non-root user.
- A [trusted checkout](install.md#obtain-a-trusted-checkout) of this
  repository, with access to its locked Python package indexes.
- `python3` 3.12–3.14 to launch the installer, and
  [uv 0.12.14](install.md#get-missing-prerequisites) for the Cairn runtime.
- An unused loopback port; the default is 8000.

The installer checks prerequisites before preparing the runtime. It may retain
private installer state and a transcript when a check fails.

## Install

From the root of the checkout:

```sh
./cairn-install --non-interactive --mode disposable --name trial
```

Running `./cairn-install` without arguments asks the same questions
interactively: the mode, an installation name and a port. The name must be
lowercase and unique within the selected installer state directory. Disposable mode is always Attic only, so
it asks no feature question.

## Expected result

The installer prints its configuration, then a heading and result for each
stage: preflight, prepare, bootstrap, start, verify, restart and a final stop.
It ends with a summary whose paths are absolute:

```text
Installation complete
  Status: verified
  Endpoint: http://127.0.0.1:8000
  State: /home/USER/.local/state/cairn-install/trial/state.json
  Credential: /home/USER/.local/state/cairn-install/trial/instance/credentials/admin.token
Transcript: /home/USER/.local/state/cairn-install/trial/commands.log
```

`Status: verified` means the authenticated identity, the exact Attic bytes and
restart retention all passed. The disposable process is then stopped, so the
endpoint is no longer listening; its data, credential and evidence are retained
under the private state directory for inspection. The credential file is
owner-only. Never paste it into a chat, an issue or a terminal transcript.

Anything else is a failure. Read the reported problem, fix it, and run
`./cairn-install resume --name trial`; the transcript path printed on failure
holds the redacted command log.

## Manage the instance

```sh
./cairn-install ls                      # recorded installations, no live probe
./cairn-install status --name trial     # configuration, then recorded state JSON
./cairn-install resume --name trial     # continue after a fix or interruption
./cairn-install rollback --name trial   # stop owned resources, keep all data
./cairn-install blitz --name trial      # delete everything owned by "trial"
```

`rollback` preserves: it stops or disables only what the installer owns and
keeps catalogue data, credentials and evidence, so a later `resume` restores the
same instance. `blitz` destroys: it permanently removes every file, container
and volume owned by that name, asks you to type the name to confirm, and cannot
be undone. If deletion is interrupted, run `blitz` again to finish it. Both are explained in
[guided installation](operations/guided-installation.md#preserving-rollback).

## Manual disposable script

For every command and the temporary-data cleanup procedure, follow the
[manual disposable installation](operations/manual-disposable-installation.md).

## MCP endpoint

The disposable installer stops the service after verification. To connect an
MCP client, install a persistent instance and follow the
[client guide](clients.md#mcp-clients).
