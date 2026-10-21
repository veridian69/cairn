#!/bin/sh
# SessionStart: ask the agent to call Garden status once. The core learns this Codex thread from
# that first tool call and only then becomes deliverable. The hook reads its input and does nothing else.
cat >/dev/null
printf '%s' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Garden is available in this session. Call the Garden status tool now, once. It reports whether Garden is connected here and which identity it uses; Garden can deliver messages to this session only after that call."}}'
