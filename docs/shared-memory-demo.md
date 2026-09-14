# Cairn: one plan, two agents, shared memory

This is an edited presentation of real Claude Code and Codex CLI turns through Cairn’s managed host integration. The text layout is re-rendered for readability and waiting time is shortened. The prompts and replies below are verbatim; the workshop is synthetic data. No replies were invented.

Each turn starts a fresh CLI process. Only the current prompt and authorised Cairn memory are available; one agent’s transcript is not passed to the other. The instance was disposable, with Attic enabled and semantic search disabled.

Source commit: `25ee4db46810d1f394bc2b06b14b978ad462f7dd`. Requested models: Claude `sonnet`, Codex `gpt-5.5`. Cairn version: `0.1.0rc2`.

## 1. Claude

**Prompt**

Remember our repair workshop plan: Saturday at 10:00, at the library, for eight people. This is a plan, not a confirmed booking. Please keep your reply to two short sentences.

**Reply**

Saved: repair workshop plan for Saturday 10:00 at the library for eight people, noted explicitly as unconfirmed (not a booking). No prior related facts were found in memory.

Memory receipt: `verified`. Saved fact IDs: 5c682f99-61d0-486d-a2ae-fc3808440026. Real elapsed time: 16.2 seconds.

## 2. Codex

**Prompt**

What is our repair workshop plan? Please check Cairn and answer in two short sentences. Do not save anything new.

**Reply**

Cairn says the repair workshop plan is Saturday at 10:00, at the library, for eight people. It is recorded as unconfirmed, not a confirmed booking.

Memory receipt: `none`. Saved fact IDs: none. Real elapsed time: 23.3 seconds.

## 3. Codex

**Prompt**

Correction to the repair workshop plan: the venue is now the community hall because the library is unavailable. Saturday at 10:00 and eight people are unchanged. Update the saved venue and keep the correction history. Please reply in two short sentences.

**Reply**

Updated the saved repair workshop venue to the community hall, with Saturday 10:00 and eight people preserved. The prior library venue was replaced with correction history kept.

Memory receipt: `verified`. Saved fact IDs: 6c850ade-33ba-42ce-aef9-99f218b8e089. Real elapsed time: 34.9 seconds.

## 4. Claude

**Prompt**

Where is our repair workshop now, and what changed? Please check the saved plan and its correction history. Answer in two short sentences and do not save anything new.

**Reply**

The current (still unconfirmed/candidate) plan puts the repair workshop at the community hall, Saturday 10:00 for eight people. It was corrected from an earlier version that listed the library as the venue, changed because the library became unavailable.

Memory receipt: `none`. Saved fact IDs: none. Real elapsed time: 13.6 seconds.

## Independent history check

Read through the public history API after the session:

- Original record, saved by Claude: Repair workshop plan (unconfirmed, not a confirmed booking): Saturday at 10:00, at the library, for eight people.
- Current record, saved by Codex: Repair workshop plan (unconfirmed, not a confirmed booking): Saturday at 10:00, at the community hall because the library is unavailable, for eight people.
- The original is invalidated but retained; the replacement is active.
- The correction links the original to the replacement and is attributed to Codex’s distinct principal.
- Both records remain candidate claims; this demonstration does not establish that a real workshop exists.
- This clip demonstrates saving, cross-agent recall and correction history. It does not demonstrate disagreement handling.

Repository: https://github.com/veridian69/cairn
