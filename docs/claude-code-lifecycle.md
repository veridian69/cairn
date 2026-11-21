# Claude Code lifecycle integration

`cairn.client.claude_lifecycle` connects Claude Code's native lifecycle hooks
to the everyday `cairn-memory` command. It is a thin adapter: every Cairn
request is one in-process invocation of that command with an explicit
[connection profile](memory-connection-profiles.md), so the expected-instance
and grant check runs before any content is sent, and the profile fixes
endpoint, scope, classification, session and credential reference. The
[explicit host skills](host-memory-workflows.md) still install no hooks; this
module is the separate, opt-in event-driven path.

## Host capability check

Checked on 8 November 2026 on native Linux under WSL2, from each host's own
hooks documentation and the installed binaries.

| Host | Installed version | Lifecycle hooks |
| --- | --- | --- |
| Claude Code | `2.1.319 (Claude Code)` | Yes. Documented events include `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact` and `SessionEnd`. `Stop` input carries `last_assistant_message`. |
| Codex CLI | `codex-cli 0.167.0` | Yes, `hooks` feature stable and enabled. Documented events are `SessionStart`, `SessionEnd`, `SubagentStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreModelRequest`, `PostModelRequest`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStop`, `Stop` and `Interrupt`. No failed-tool event; every non-managed hook needs persisted trust review. |

Claude Code is the first slice: it has a distinct failed-tool event, loads an
additional settings file with `claude --settings PATH` without editing any
user or project configuration, and needs no per-hook trust state. Codex
support is a separate follow-up.

Not covered: Claude desktop, Claude on the web (cloud sessions do not read
local hooks), the Codex IDE/desktop/cloud surfaces, and native Windows. Only
the Claude Code CLI running inside native Linux or WSL against the native
Linux filesystem is supported.

## Supported events

| Claude Code event | Cairn operation | Default |
| --- | --- | --- |
| `SessionStart` (all sources) | `arrive` with a fixed installation-time query, then `acknowledge-visit` after the complete hook output has been written | on |
| `Stop` | `remember` of the lines of one `cairn-observations` block in the final reply | on |
| `UserPromptSubmit` | bounded `recall` (relevant only) using the prompt | opt-in |
| `PostToolUseFailure` | bounded `recall` using the tool name and the first error line (at most 512 bytes) | opt-in |

`SessionEnd`, `StopFailure` and the other events are not used. The opt-in
events send prompt text or tool error lines to the configured Cairn as recall
queries; enable them only where that is acceptable.

A final reply selects durable candidate observations explicitly:

````text
```cairn-observations
- The build uses port 8123.
- Releases are cut on Fridays.
```
````

One top-level block (backtick or tilde fence, info string exactly
`cairn-observations`), one self-contained observation per line, at most 8
lines of at most 4096 UTF-8 bytes. The reply is parsed with markdown-it-py's
CommonMark parser and only a top-level fence with that label counts, so
examples inside a longer or different fence, HTML blocks and comments,
indented code, block quotes and lists select nothing. Several top-level blocks, an unclosed block, an empty
block or oversized content are refused whole rather than truncated. The reply text
itself is not stored (`response` is empty) and the transcript is never read.

## Behaviour

- Hooks always exit 0 and never return `decision`, `continue` or Stop
  context, so a failure cannot block a prompt, force a continuation or alter
  completed model output. Problems appear as a `systemMessage`.
- Recalled memory is added as `additionalContext` labelled untrusted
  attributed evidence (`content_role` `untrusted-data`). Hooks only read,
  open sessions/visits and record candidate turns; they never promote,
  validate, correct, disagree or widen scope, and nothing recalled is
  executed or parsed for observations.
- Each event has a deadline (20 s arrival, 8 s recall, 25 s Stop) below the
  host timeout written into the settings file. Degraded or refused recall
  says so, including that absence of recall is not evidence of absence.
  Context above 9500 characters is refused explicitly and the visit is then
  not acknowledged.
- Stop identities are derived from the profile session, the host session ID,
  `prompt_id` and a digest of the selected lines. Identical redelivery, after
  a timeout, lost response or server restart, reuses the original turn,
  attempt and idempotency keys and reconciles to the same facts.
- Saved facts are reported only from a committed custody receipt, with fact
  IDs and turn identities. Uncertain persistence reports the identities and
  that nothing is reported as saved; `cairn-memory status`/`resume` read the
  same turn.
- No transcript archive, local memory, retry store or session file is kept.

## Install, check and round trip

Use a profile with a fixed `session_id`, an owner-only profile and an
owner-only credential file. First check the connection, then preview and
write a dedicated settings file in an explicit existing directory:

```sh
cairn-memory --profile /abs/profile.json check
python -m cairn.client.claude_lifecycle install \
  --destination /abs/hook-dir --profile /abs/profile.json \
  --arrival-query "cairn repository current work"
python -m cairn.client.claude_lifecycle install \
  --destination /abs/hook-dir --profile /abs/profile.json \
  --arrival-query "cairn repository current work" --apply
```

Add `--event user-prompt-submit` or `--event post-tool-use-failure` for the
opt-in events. The installer makes no network request, does not read the
token, writes `cairn-lifecycle-settings.json` with mode 0600 by exclusive
creation, treats an identical file as unchanged and refuses a different one.
Hook commands use `python -I` so a package in the host's working directory
cannot shadow Cairn. The file has no effect until passed to
`claude --settings /abs/hook-dir/cairn-lifecycle-settings.json`.

The synthetic round trip is
`tests/client/test_claude_lifecycle.py`: it runs the installed commands
through `/bin/sh` as the host does, against a disposable loopback instance
that crashes after a commit, restarts, and is then read by a fresh host
session. It also covers duplicate delivery, refusal, timeouts and scope
isolation. Fixture dispatch is not host acceptance.

## Real host run

On 8 November 2026 a real Claude Code `2.1.319` host was run against a
disposable synthetic instance. Session start gave an arrival briefing; Stop
saved the one selected observation with a custody receipt; and a separate,
fresh host session recalled it from its arrival briefing. Only the two
default events were exercised on the host.

To repeat it without touching any existing configuration, run each session in
an empty project directory against a disposable instance:

```sh
claude -p "$PROMPT" --settings /abs/hook-dir/cairn-lifecycle-settings.json \
  --setting-sources project --strict-mcp-config --tools "" \
  --no-session-persistence --output-format stream-json --verbose \
  --include-hook-events
```

`--setting-sources project` in an empty project loads no user, project or
local settings, `--strict-mcp-config` loads no MCP servers, and
`--include-hook-events` shows each hook's exit status and output. A dedicated
`CLAUDE_CONFIG_DIR` with its own `claude auth login` isolates authentication
as well.
