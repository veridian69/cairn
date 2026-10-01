# Real work: deepdiff issue #550

Two AI agents, Val (Codex) and Spike (Claude), worked a real bug in the open-source [deepdiff](https://github.com/seperman/deepdiff) library, with an independent verifier (also Codex, in its own session). They shared one memory: Cairn. Every Cairn, Garden and Attic call in the story was made by an agent. The harness started each session with a goal-level prompt; outside the story, it also checked at startup that Garden answered, and after the last turn read the recorded evidence back from Attic for the automated checks.

![Codex hits a dead end; Claude picks the work up from Cairn, finds the second attempt incomplete and disputes it; the verifier re-runs everything, settles the dispute and validates Claude's fix; a fresh Claude session writes the pull request text from memory alone](assets/cairn-real-work-demo.gif)

[Video](assets/cairn-real-work-demo.mp4) · [Animated GIF](assets/cairn-real-work-demo.gif) · [Full technical transcript](cairn-real-work-demo/transcript.md) · [verification.json](cairn-real-work-demo/verification.json) · [verification-ruling-a1.json](cairn-real-work-demo/verification-ruling-a1.json)

The edit is 91 seconds. The five turns took 22 min 47 s. Headings and summaries are written for the edit; every panel marked **EXACT EXCERPT** or **ACTUAL REPLY** is copied verbatim from the logs, and the storyboard build refuses any excerpt it cannot find there.

## Status

- **Evidence-valid:** yes. All eight deterministic checks pass, re-run by the harness from the run directory alone. They cover actor custody, the dead end, the fix, independent resolution, the verifier's decision, code truth, the memory-only session and egress.
- **Demo-usable:** yes, under ruling A1 of 25 September 2026: a worker's disagreement before the verifier's turn counts as the dispute beat. The harness's own `verification.json` predates that ruling and reports `demo_usable: false` only because the dispute came in T2 rather than T3. `verification-ruling-a1.json` is the re-verification of the same run directory. Both are published.
- The pull request was not opened. The fix is validated in Cairn; merging it is a human decision.

## What happened

1. **T1, Val (Codex).** Reproduced the `TypeError` and tried two fixes. The first failed its own test (`2 failed, 186 passed, 3 skipped`) and was recorded as a failed approach, `dcd421c4-7fe0-4dd7-b4ec-528e13bef79c` (commit `1faef68`). The second, `7a878e9`, was recorded as the fix, `48f4e46b-daa2-4c10-a047-598b69630950`. Val believed it was done and sent no Garden message.
2. **T2, Spike (Claude).** Found the work through Cairn alone. Broader tests showed `7a878e9` still failing 10 of 30 regression cases, because the root cause is in `helper.number_to_string`. Spike recorded that as a failed approach, `f8d6c6d2-e547-4ec3-931e-cbdf516066fe`, and pushed a fix, `b8e4848`, recorded as `875ed73e-6665-4dbb-8eb6-ea2ec77e855e`. It then recorded a disagreement, `e1aeb91e-1dd7-46be-9009-fd7ad311e03b`, against Val's fix claim and told Val through Garden. Its attempt to mark Val's fact superseded was denied: its session has no correction grant.
3. **T3, Val (Codex).** Reviewed `b8e4848` against the issue, agreed with Spike's assessment and recorded the review (`f6a00c3a-7af3-4811-839d-8af4f8529dd0`, `74b4eef8-d6b9-47fb-b4bf-afe4b299fcfc`).
4. **T4, verifier (Codex).** Worked from a fresh clone, trusting no one's statement. All 30 new regression cases pass on `b8e4848`, and `7a878e9` fails 10 of them. It resolved the disagreement on its own evidence `4828e459-b5e6-440a-9a4a-a7a13c308b06` in favour of the "insufficient" finding (resolution `02f51c72-5571-4fd1-a08e-e20d705ab7e1`), then promoted Spike's fix. The validated fact is `a1037797-0e49-4ea4-881f-531cf88ebc8e`, derived from `875ed73e`.
5. **T5 and T6** (the correction loop) were skipped by design: T4 had already validated the fix.
6. **T7, a new Claude session.** With no Garden connection and no write grant, it wrote the pull request text from Cairn and origin alone. It cites 14 distinct Cairn IDs, including the dead end and the dispute, and check 7 found none unknown.

The transcript lists every fact in full, every disagreement and resolution, every evidence payload, each branch's full diff, and each turn's prompt followed by every event in order.

## Capture provenance

- Capture: 25 September 2026, Europe/Zurich, labelled `capture-3`. Captures 2 and 4 are unpublished rehearsals. Capture 2 is void because Claude Code's sandbox failed open in one turn; the harness now fails closed on that.
- Harness revision: `ac9f7961ebac96f007032c03e3b13b76d1d9da9a` at start and end.
- Models requested: Spike `claude-fable-5-1`, Val and the verifier `gpt-6-sol`. CLI versions: Claude Code `2.1.282`, Codex CLI `0.156.1`.
- Cairn instance: `ed4a40af-b01c-44da-8cae-ac65048261ab` (disposable). Its catalogue uses a fixed test clock; CLI and Garden times are wall time.
- deepdiff base revision: `79e4379278b1cfdd8e9e3dccac364956908e8989`. Venv lock SHA-256: `fb334fa6858516439466f0cddf7821c9916210ed5ef551cb7a702f5d29a325bc`.
- Isolation: each session had a fresh clone, no tool network, and a local bare repository as its only remote.
- Measured cost, as each CLI reports it: Claude USD 6.84 (T2 USD 5.41, T7 USD 1.43). Codex 3,100,972 input and output tokens (T1 735,056; T3 1,358,688; T4 1,007,228).
- Wall time: T1 233.8 s, T2 504.5 s, T3 303.8 s, T4 247.6 s, T7 77.6 s.

At publication, local paths, the directory names derived from them, the local account name and the disposable instance's bearer tokens are replaced by labelled placeholders. The publication fails closed if any provider credential appears in a published file.

## Attribution

[deepdiff](https://github.com/seperman/deepdiff) is MIT-licensed. The code shown in the transcript is the agents' work on a local clone at the revision above. Nothing was submitted upstream.
