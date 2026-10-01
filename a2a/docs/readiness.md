# Worker startup readiness

Use the maintained Garden adapter's read-only readiness observation before starting a worker's model process:

```sh
a2a doctor --profile /run/garden/profile.json --readiness
```

A valid observation is one JSON object with `schema_version: "garden.readiness/v1"`, `ready`, `reason` and `retryable`. The process completing successfully only means the observation was emitted. Start model work only after validating the schema and receiving `ready: true`, `reason: "ready"`, `retryable: false` within the worker's frozen startup budget.

| Reason | Ready | Retryable | Meaning |
| --- | --- | --- | --- |
| `ready` | true | false | Authenticated participant, Cairn instance, classification and exact ordered scope match the profile. |
| `unavailable` | false | true | The maintained adapter classified the observation as unavailable or busy. A bounded observation retry is permitted. |
| `rejected` | false | false | Authentication, binding or local credential admission failed. Do not start the worker or retry this refusal. |
| `deadline` | false | false | The observation context expired or was cancelled. |

Invalid flags or profile files still produce a nonzero CLI exit. Treat nonzero exits, malformed output and unknown schemas or reasons as startup failures. The caller must enforce its own total deadline, bounded output capture and bounded retry backoff; each CLI observation also has the existing 30-second ceiling. Repeating this command never extends the worker's accepted run deadline.

This mode does not send messages, poll or acknowledge an attention inbox, or start/resume a host session. `--host` and `--readiness` are mutually exclusive. The existing `doctor` output is unchanged when `--readiness` is absent. Credentials remain in the protected file named by the profile and are never part of the readiness report.
