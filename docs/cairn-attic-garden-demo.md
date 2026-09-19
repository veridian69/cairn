# Cairn, Attic and Garden: actual agent tool use

Status: **passed**. This is synthetic data in one disposable Cairn and Garden instance. Spike (Claude) and Val (Codex) made the recorded MCP calls themselves; the harness only provisioned, preflighted, launched, recorded, validated and tore down the instance.

The companion clip is 40 seconds, re-rendered in the style of `shared-memory-demo.gif`: dark navy, mint accents, peach for Claude and blue for Codex. Waiting is shortened. Message excerpts are labelled; the full messages are below. No dialogue was invented or relayed by the recording harness.

[Video](assets/cairn-attic-garden-demo.mp4) · [Animated GIF](assets/cairn-attic-garden-demo.gif) · [Poster](assets/cairn-attic-garden-demo.png)

## Dialogue and evidence

1. **Spike — `cairn_low.ingest`**
   Saved `Synthetic repair workshop plan: Saturday at 10:00, at the library, for eight people. Status: unconfirmed.`
   Fact `6dafe55b-aa43-4528-9dd2-75a38429dfae` · evidence `d0f877a1-2e57-43dc-903d-fa18b91206e7`
2. **Spike — `garden.send_message`** (`4932caf7-807a-4598-81fb-66cf817d8094`)
   > @val can you verify this? Fact ID: 6dafe55b-aa43-4528-9dd2-75a38429dfae, Evidence ID: d0f877a1-2e57-43dc-903d-fa18b91206e7
3. **Val — `garden.read_messages` → `memory.check` → `memory.recall`**
   Recalled exact fact `6dafe55b-aa43-4528-9dd2-75a38429dfae` with body `Synthetic repair workshop plan: Saturday at 10:00, at the library, for eight people. Status: unconfirmed.`
4. **Val — `cairn_low.read-evidence`**
   Evidence `d0f877a1-2e57-43dc-903d-fa18b91206e7` · 105 bytes · SHA-256 `11ca6c6e7fa930641c3925aeb87e95aa42521f970ccca335994d359a41446fea`
   > Synthetic repair workshop plan: Saturday at 10:00, at the library, for eight people. Status: unconfirmed.
5. **Val — `garden.send_message`** (`f583e1c6-0b21-4b7a-bb5d-862b7da79efb`, reply to `4932caf7-807a-4598-81fb-66cf817d8094`)
   > Verified: recorded plan is Saturday at 10:00, at the library, for eight people; status unconfirmed. Fact ID: 6dafe55b-aa43-4528-9dd2-75a38429dfae. Evidence ID: d0f877a1-2e57-43dc-903d-fa18b91206e7. Exact source excerpt: "Synthetic repair workshop plan: Saturday at 10:00, at the library, for eight people. Status: unconfirmed."
6. **Spike — `garden.read_messages` → `garden.send_message`** (`5d09482c-6825-4de8-9b6e-3cde78231017`, reply to `f583e1c6-0b21-4b7a-bb5d-862b7da79efb`)
   > Thanks Val for checking both Cairn memory and the exact Attic source.

## Model replies

- Spike: “Ingested workshop plan fact and asked val to verify the IDs.”
- Val: “Done; Spike has the verified threaded reply.”
- Spike: “Val verified the synthetic repair workshop fact against both the Cairn memory record and the exact Attic source excerpt.”

## Provenance

- Instance: `ac7927a4-2a65-4d46-90ee-8a00028dfde8`
- Scope: `acme/repository:cairn`, classification `internal`
- Claude Code: `2.1.277 (Claude Code)`, actual model `claude-haiku-4-5-20251001`
- Codex: `codex-cli 0.155.0`, requested model `gpt-5.5`
- Environment revision: `67028e3d90cd0a5e7a88e44788b6f639c393bf1f` at start and `67028e3d90cd0a5e7a88e44788b6f639c393bf1f` at end
- Checkout limitation: The checkout was already dirty and could change concurrently; the recorded revision identifies the environment, not a clean source snapshot.
- Clock caveat: Cairn catalogue, audit and receipt times use the disposable test fixture's deterministic clock (`2026-09-09T12:00:00Z`); Garden and native CLI event times record the actual 2026-09-18 capture.
- Capture date: 19 September 2026, Europe/Zurich. Cairn used the disposable fixture’s fixed clock (9 September 2026); Garden timestamps use wall time.
- Cairn version reported by the native identity check: `0.7.8`. Memory recall used the shipped catalogue-backed adapter. Attic was read via Cairn’s native `read-evidence` tool.
- The harness ran the normal evidence delivery worker. It did not save, recall, retrieve evidence or send/read conversation messages for either agent.
- Local raw logs, exact hashes, prompts and structured transcript are retained under `build/shared-garden-demo-native-20260919/`. Rendering source, storyboard and independent deterministic verification are under `build/garden-session-demo/`.
