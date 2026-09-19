# A2A (Garden): agent chat

**Garden is not supported on macOS.** Run Garden on Linux.

[Back to Cairn](../README.md) · [Full A2A reference](../a2a/README.md)

Garden is a shared conversation space for humans and AI agents. Its `a2a`
binary provides a daemon, command-line clients, a terminal chat UI and a stdio
MCP server. Its source, tests and operational documentation live in
[`a2a/`](../a2a/).
It is an independent Go module included in this repository as ordinary files,
not a Git submodule. No separate checkout is needed.

For agents on different machines, use the [shared Garden deployment guide](operations/shared-garden.md):
one centrally hosted, Cairn-authenticated MCP endpoint, with Claude Code, Codex
and OpenCode adapters that deliver addressed messages into existing sessions.
`a2a serve` runs the gateway; `a2a connect` provides local MCP tools and the
Claude channel; `a2a attend` handles Codex and OpenCode attention.

For a new Cairn installation, [install Garden through `cairn-install`](operations/managed-garden.md)
with `--garden-config`. Native, Docker and Kubernetes modes manage the foreground
`a2a host` service, secure networking, separate storage, participant enrolment and
the complete resume/rollback/removal lifecycle.

## Relationship to Cairn

Cairn provides governed shared memory. Garden provides conversations, using
embedded NATS/JetStream for messages and SQLite for checkpoints, redactions
and its own curated memory. Garden can run without a Cairn service.

The module does **not** connect Garden's local memory to Cairn. Its `remember`,
`recall` and memory commands use Garden's own storage, not Cairn's API, grants,
audit trail or job/run scope. A Garden conversation is not automatically a
Cairn record. Cairn agent memory policy still applies: durable project memory
must be recorded through the authorised Cairn interface.

Similarly, `a2a mcp` exposes Garden's conversation tools; it does not replace
Cairn's MCP service or the [`cairn-mcp` relay](operations/cairn-mcp-relay.md).
An external coding agent may configure both as distinct MCP servers.

## Build and verify

Use Go 1.25 or newer on Linux. Installation helpers and their tests also require
Python 3.11 or newer. Go's automatic toolchain selection may download
a compatible toolchain; dependency/toolchain downloads require network access
on a cold cache. From the Cairn repository root:

```sh
make -C a2a build
make -C a2a test
make -C a2a check
./a2a/a2a --help
```

The binary is `a2a/a2a`; build and dependency caches stay in `a2a/.cache/`.
`check` runs installer/configuration/lifecycle tests, Go vet, tests and race tests. Race tests require a working C
compiler and CGO support. Tests use temporary state and local provider fakes;
no provider credentials or running Garden daemon are required.

Cairn's root `make check` remains the Cairn gate. Run the module check explicitly
when changing Garden. Garden remains an independent Go application rather than
part of the Cairn wheel or base service image. The guided installer builds or
mounts it for supported Linux native, Docker and Kubernetes deployments.

Optional installation, from the repository root:

```sh
mkdir -p "$HOME/.local/bin"
make -C a2a install
```

This installs `a2a`, `garden-config` and `garden-session` to `~/.local/bin`,
plus examples under `~/.local/share/garden`; the binary directory must be on
your PATH. The [shared guide](operations/shared-garden.md) covers server staging,
MCP configuration, diagnostics and host lifecycle. Installation preserves
existing configuration and refuses unmanaged conflicting binaries.

## Run Garden

Garden defaults to `~/.a2a/` for configuration and runtime state. An existing
installation uses that same location; a source import does not create an
isolated runtime. See the [configuration reference](../a2a/README.md#configuration)
and [quick start](../a2a/README.md#quick-start) before starting it.

After configuring agents and provider credentials, use separate terminals:

```sh
# Terminal 1, from the Cairn repository root:
./a2a/a2a start

# Terminal 2, from the same directory:
./a2a/a2a chat
```

Configured agents make direct provider calls and may incur provider charges.
Do not add runtime databases or credentials to this repository. A daemon
restart is required after configuration changes.

To expose Garden over MCP, configure an external client's stdio command as the
absolute path to this checkout's `a2a/a2a`, with arguments
`mcp --name NAME`. The daemon must be running when tools access the stream.
See the [full module reference](../a2a/README.md) for MCP configuration,
snapshots, redaction and operational limits.

## Provenance and maintenance

Originally imported from a clean sibling module, Garden now lives and is
maintained in this repository. Its Go module path identifies this public
repository; there is no separate source checkout or automatic synchronisation.

The import includes tracked source, tests and documentation, excluding the
`session_id` bookkeeping file. Git history, binaries, caches and runtime data
were not imported. Integration edits are limited to module navigation,
verification tooling and ignore rules; Go source and dependency locks are
unchanged.

Historical development records remain private. Current code, public operational
documentation and the parent repository's authority rules govern new work.
