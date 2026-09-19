# Native Cairn correction with Garden and Attic

A 30-second revision of the shared-memory demo. Dark navy, mint accents, blue for Val/Codex and peach for Spike/Claude. Waiting is shortened; exact excerpts are labelled.

[Video](assets/cairn-attic-garden-correction-demo.mp4) · [Animated GIF](assets/cairn-attic-garden-correction-demo.gif)

Status: **passed**. This is synthetic data in one disposable Cairn and Garden instance. Val (Codex) and Spike (Claude) made every displayed Cairn, Garden and Attic call themselves.

1. **Val bobs the initial plan** — `b2b63aeb-d5ac-4785-8e06-c0df29455c28` with Attic evidence `779cf517-f411-4792-89c7-3b0df7b10c9e`.
2. **Spike reads Val's Garden message, bobs the correction, then invalidates the original** — replacement `1b2f5027-5ae7-4b10-92c7-0407a47cdd68`, evidence `af66d38d-9152-4622-ae80-ea93c546c9e9`; the replacement link preserves the history.
3. **Spike Gardens Val** — “Venue changed to community hall. Original fact b2b63aeb-d5ac-4785-8e06-c0df29455c28 (evidence 779cf517-f411-4792-89c7-3b0df7b10c9e) superseded by 1b2f5027-5ae7-4b10-92c7-0407a47cdd68 (evidence af66d38d-9152-4622-ae80-ea93c546c9e9).”
4. **Asked where it is now, Val reads Garden, Cairn history and both Attic payloads** — “The workshop is now at the community hall. It changed because the original library venue became unavailable.”

## Exact Attic sources

> Synthetic repair workshop plan: Saturday at 10:00, at the library, for eight people. Status: unconfirmed.

> Synthetic repair workshop plan: Saturday at 10:00, at the community hall, for eight people. Status: unconfirmed. The library is unavailable.

The recorded setup also includes Val sending the original fact and evidence IDs to Spike through Garden. That setup message is omitted from the short edit. The human question is supplied directly to Val; it is not a Garden message.

The correction uses native `ingest` followed by `invalidate` with `superseded_by`. Val reads both records through `memory.history` and both exact evidence payloads through `read-evidence`. No harness relays messages or performs these actor operations. Independent verification checks distinct authors, an invalidated-but-retained original, an active replacement, the correction link, Garden delivery and both source hashes.

Local raw logs, hashes, prompts and structured evidence are retained under `build/correction-garden-demo-native-20260919/`. The rendering source, storyboard and separate verification are under `build/garden-session-demo/`.

## Capture provenance

- Synthetic workshop; both saved facts remain candidate claims and the plan remains unconfirmed.
- Capture date: 19 September 2026, Europe/Zurich. The disposable Cairn fixture uses a fixed 9 September clock for catalogue/audit records; Garden and CLI times are wall time.
- Only the completed capture is represented. Earlier abandoned attempts are excluded.

- Requested models: {"spike": "haiku", "val": "gpt-5.5"}
- CLI versions: {"claude": "2.1.277 (Claude Code)", "codex": "codex-cli 0.155.0"}
- Instance: `601deabd-5ec7-4195-9252-66f15356bfb6`
- Environment revision: `67028e3d90cd0a5e7a88e44788b6f639c393bf1f`. This identifies the checkout environment, not a clean source snapshot.
