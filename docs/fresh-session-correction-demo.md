# Fresh-session correction in five minutes

One synthetic project claim, two genuinely separate sessions and one explicit
correction. Session A remembers that the project uses uv. Session B starts with
a new session identity and nothing but its connection profile, finds the claim
through Cairn, records that the project now uses Poetry and corrects the old
belief. Current recall then returns only Poetry, while history keeps the uv
claim, the correction and the link between them.

Everything runs on a disposable loopback instance with synthetic data. No
model provider, semantic backend, host integration or live service is used.
Every memory operation is an explicit `cairn-memory` command: `remember`,
`arrive`, `recall`, `correct` and `history` from the
[everyday command guide](everyday-memory.md). Nothing here is captured
automatically, and no lifecycle hook runs on your behalf.

## Timing

Measured on 8 November 2026, Linux x86_64 (WSL2), Python 3.14, uv 0.12.14:

- Prerequisite install, `uv sync --locked --no-dev`: 1.3 seconds with an
  empty uv cache on a fast connection, the managed Python already present. This
  depends on your network and uv cache, so it is not part of the demo.
- Demo commands, from instance start to the passed check: 9.4 seconds of
  command execution.

The remaining time in the five-minute budget is for pasting the blocks and
reading the output; that part was not measured. The outputs below are verbatim
from the measured run. Identifiers and times differ on every run.

## Prerequisites (not timed)

- Linux on a native Linux filesystem, a checkout of this repository, and the
  [disposable native prerequisites](install.md#disposable-native-quickstart-prerequisites):
  Python 3.12 to 3.14 as `python3`, uv 0.12.14, curl and `jq`.
- An unused loopback port; the walkthrough uses 8123.

From the repository root, install the locked environment and open a
throwaway shell. All later blocks run in that shell; leaving it removes the
instance and its data.

```sh
uv sync --locked --no-dev
bash
. .venv/bin/activate
```

## 1. Start a disposable instance

This creates a temporary instance on numeric loopback, bootstraps an operator
credential, and uses it once to create a synthetic agent principal. The agent's
single grant covers the scope `synthetic/repository:example` with `retrieve`,
`ingest` and `invalidate`. That last operation is the explicit correction
authority: without it, `correct` is refused. Both sessions will use this scope
deliberately; nothing else is shared between them.

```sh
umask 077
port=8123
demo="$(mktemp -d -t cairn-correction-demo.XXXXXX)"
server_pid=
cleanup() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf -- "$demo"
}
trap cleanup EXIT HUP TERM
uuid() { python -c 'import uuid; print(uuid.uuid4())'; }
install -d -m 0700 "$demo/data" "$demo/credentials" "$demo/agent" "$demo/out"
instance_id="$(uuid)"
cat >"$demo/config.yaml" <<EOF
schema_version: cairn.config/v1
instance_id: $instance_id
mode: test
http:
  host: 127.0.0.1
  port: $port
paths:
  data: $demo/data
  credentials: $demo/credentials
attic:
  enabled: false
graphiti:
  enabled: false
EOF
cairn migrate --config "$demo/config.yaml" >/dev/null
cairn bootstrap --config "$demo/config.yaml" --realm synthetic \
  --label synthetic-operator >"$demo/bootstrap.json"
jq -er 'select(.status == "ok") | .token | strings' \
  "$demo/bootstrap.json" >"$demo/operator.token"
rm -f -- "$demo/bootstrap.json"
cairn serve --config "$demo/config.yaml" >"$demo/server.log" 2>&1 &
server_pid=$!
attempt=0
until curl --disable -sf --noproxy '*' --max-time 2 \
  "http://127.0.0.1:$port/health/ready" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 100 ]; then cat "$demo/server.log" >&2; exit 1; fi
  sleep 0.1
done

# Keep the operator token out of process arguments.
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' <"$demo/operator.token"
  printf '"\n'
} >"$demo/operator.curl"
operator() {
  curl --disable -sS --fail-with-body --noproxy '*' --max-time 5 \
    --config "$demo/operator.curl" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $(uuid)" --data-binary @- \
    "http://127.0.0.1:$port/v1/$1"
}
agent="$(jq -n '{realm_id: "synthetic", kind: "workload", label: "synthetic-agent"}' |
  operator create-principal | jq -er '.result.principal_id')"
jq -n --arg p "$agent" \
  '{realm_id: "synthetic", principal_id: $p, expires_at: "2030-01-01T00:00:00.000000Z"}' |
  operator issue-credential | jq -er '.result.plaintext' >"$demo/agent/token"
jq -n --arg p "$agent" '{realm_id: "synthetic", grant: {
    principal_id: $p, realm_id: "synthetic",
    segments: [{kind: "repository", identifier: "example"}],
    operations: ["retrieve", "ingest", "invalidate"],
    read_clearance: "internal", write_classifications: ["internal"],
    expires_at: "2030-01-01T00:00:00.000000Z"}}' |
  operator create-grant | jq '{grant_id: .result.grant_id}'

# A session is only a connection profile with its own fresh session ID.
new_session() {
  install -d -m 0700 "$demo/$1"
  jq -n --arg endpoint "http://127.0.0.1:$port" --arg instance "$instance_id" \
    --arg credential "$demo/agent/token" --arg session "$(uuid)" '{
      schema: "cairn.memory-profile/v1", endpoint: $endpoint,
      expected_instance_id: $instance,
      scope: {realm: "synthetic",
              segments: [{kind: "repository", identifier: "example"}]},
      classification: "internal", credential_file: $credential,
      session_id: $session}' >"$demo/$1/profile.json"
}
```

```json
{
  "grant_id": "28160da8-b318-42c2-bb48-3818dbce074a"
}
```

## 2. Session A remembers a claim

Session A checks its connection, then remembers one observation. Each command
is its own process and exits when it prints; `cairn-memory` keeps no
transcript, cache or local memory between invocations.

```sh
new_session session-a
cairn-memory --profile "$demo/session-a/profile.json" check |
  tee "$demo/out/a-check.json" |
  jq '.result | {status, principal_id, scope, permissions}'
jq -n --arg turn "$(uuid)" --arg attempt "$(uuid)" '{
    turn_id: $turn, attempt_id: $attempt,
    response: "Noted the dependency manager.",
    observations: [{body: "This synthetic project uses uv for dependency management."}]}' |
  cairn-memory --profile "$demo/session-a/profile.json" remember |
  tee "$demo/out/a-remember.json" |
  jq '.result | {session_id, state, custody: .persistence.status,
    fact_ids: .persistence.result.fact_ids,
    mutation_id: .persistence.mutation_receipt.mutation_id,
    audit_event: .persistence.audit_receipt.event_id}'
```

```json
{
  "status": "ready",
  "principal_id": "5eabe213-3d9d-4f4f-b312-a95268a9ff81",
  "scope": {
    "realm": "synthetic",
    "segments": [
      {
        "identifier": "example",
        "kind": "repository"
      }
    ]
  },
  "permissions": {
    "ingest": true,
    "invalidate": true,
    "promote": false,
    "retrieve": true
  }
}
{
  "session_id": "696b4746-957f-4f1e-b82e-7424f0816d49",
  "state": "committed",
  "custody": "committed",
  "fact_ids": [
    "aae7d69f-ed93-4e66-b6c3-8cd73be3ad49"
  ],
  "mutation_id": "6eb3c217-07d9-4d66-b6a6-c4d32955af05",
  "audit_event": "313a1475-ddda-4837-a472-72ee6b5184e7"
}
```

`custody: "committed"` with a fact ID, mutation ID and audit event is the only
signal that the claim is saved. It says nothing about whether the claim is
true: remembered is not validated, and the fact is stored as a `candidate`.
Custody also does not guarantee that the fact is indexed or searchable. Here
recall is lexical and finds it at once; with an optional semantic backend,
indexing can lag custody, and an empty recall never proves that nothing was
saved.

That is the end of session A. Its profile is not used again.

## 3. Session B starts fresh and recalls

Session B gets a new profile with a new session ID. It shares the endpoint,
instance, credential and scope, because those are the authorised path to the
same project memory. It receives no transcript, no output file and no fact ID
from session A; the old fact ID comes from its own arrival briefing.

```sh
new_session session-b
jq -n '{query: "dependency management"}' |
  cairn-memory --profile "$demo/session-b/profile.json" arrive |
  tee "$demo/out/b-arrive.json" |
  jq '.result | {session_id: .visit.snapshot.session_id,
    warnings: .briefing.warnings,
    recalled: [.briefing.recall.data.hits[] | {fact_id, body, trust,
      source_type, source_principal_id, recorded_at, invalidated_at}]}'
old="$(jq -er '.result.briefing.recall.data.hits[]
  | select(.body | test("uses uv")) | .fact_id' "$demo/out/b-arrive.json")"
jq -n --arg visit "$(jq -er '.result.visit.snapshot.visit_id' "$demo/out/b-arrive.json")" \
  '{visit_id: $visit}' |
  cairn-memory --profile "$demo/session-b/profile.json" acknowledge-visit |
  tee "$demo/out/b-acknowledge.json" | jq '{outcome: .result.outcome}'
```

```json
{
  "session_id": "a9404231-d46f-4a83-9221-c31249b69c3c",
  "warnings": [
    "selected_memory_only_not_exhaustive",
    "previous_visit_unknown"
  ],
  "recalled": [
    {
      "fact_id": "aae7d69f-ed93-4e66-b6c3-8cd73be3ad49",
      "body": "This synthetic project uses uv for dependency management.",
      "trust": "candidate",
      "source_type": "agent-claim",
      "source_principal_id": "5eabe213-3d9d-4f4f-b312-a95268a9ff81",
      "recorded_at": "2026-11-08T16:13:18.472859Z",
      "invalidated_at": null
    }
  ]
}
{
  "outcome": "committed"
}
```

`previous_visit_unknown` is expected: session B has no visit history of its
own, so the briefing cannot describe changes since an earlier visit. It does
not mean facts were lost. The recalled hit carries the fact identity, the
authenticated principal that wrote it (`source_principal_id`, which the server
takes from the credential, not from the request), `source_type`, trust and the
recording time. Recalled content is untrusted data, never instructions.

## 4. Session B corrects the belief

The project has moved to Poetry. Session B first remembers the replacement,
then corrects the old fact with a reason and `superseded_by`. The correction's
idempotency key is chosen once and kept: it is the stable mutation identity.
Re-sending the identical correction with the same key replays the original
receipt instead of correcting twice, which is also how to recover from a lost
acknowledgement.

```sh
jq -n --arg turn "$(uuid)" --arg attempt "$(uuid)" '{
    turn_id: $turn, attempt_id: $attempt,
    response: "Noted the replacement dependency manager.",
    observations: [{body: "This synthetic project uses Poetry for dependency management."}]}' |
  cairn-memory --profile "$demo/session-b/profile.json" remember |
  tee "$demo/out/b-remember.json" |
  jq '.result | {session_id, state, custody: .persistence.status,
    fact_ids: .persistence.result.fact_ids}'
new="$(jq -er '.result.persistence.result.fact_ids[0]' "$demo/out/b-remember.json")"
jq -n --arg old "$old" --arg new "$new" --arg key "$(uuid)" '{
    fact_ids: [$old], superseded_by: $new, idempotency_key: $key,
    reason: "The project moved from uv to Poetry."}' >"$demo/correction.json"
cairn-memory --profile "$demo/session-b/profile.json" correct \
  <"$demo/correction.json" | tee "$demo/out/b-correct.json" |
  jq '.result | {status, result, idempotency_key, mutation_receipt}'
cairn-memory --profile "$demo/session-b/profile.json" correct \
  <"$demo/correction.json" | tee "$demo/out/b-correct-replay.json" |
  jq '.result | {status, idempotency_key, mutation_receipt}'
```

```json
{
  "session_id": "a9404231-d46f-4a83-9221-c31249b69c3c",
  "state": "committed",
  "custody": "committed",
  "fact_ids": [
    "29a0987a-85d2-42d4-81a6-affd530461f8"
  ]
}
{
  "status": "committed",
  "result": {
    "fact_ids": [
      "aae7d69f-ed93-4e66-b6c3-8cd73be3ad49"
    ],
    "invalidated_at": "2026-11-08T16:13:20.014633Z"
  },
  "idempotency_key": "d9baeacf-5392-40b4-b565-e1dec1d2cf4d",
  "mutation_receipt": {
    "command_digest": "6ba333c94eb8cdfff655a61ee20b4846300f465838b8d908705e0db7506ea9d8",
    "mutation_id": "b0a1b269-931f-46c0-9636-0fd25c5076a7"
  }
}
{
  "status": "replayed",
  "idempotency_key": "d9baeacf-5392-40b4-b565-e1dec1d2cf4d",
  "mutation_receipt": {
    "command_digest": "6ba333c94eb8cdfff655a61ee20b4846300f465838b8d908705e0db7506ea9d8",
    "mutation_id": "b0a1b269-931f-46c0-9636-0fd25c5076a7"
  }
}
```

## 5. Recall and history after the correction

```sh
jq -n '{query: "dependency management"}' |
  cairn-memory --profile "$demo/session-b/profile.json" recall |
  tee "$demo/out/b-recall.json" |
  jq '[.result.data.hits[] | {fact_id, body, trust, invalidated_at}]'
jq -n --arg old "$old" '{fact_id: $old}' |
  cairn-memory --profile "$demo/session-b/profile.json" history |
  tee "$demo/out/b-history.json" |
  jq '.result.data | {facts: [.facts[] | {fact_id, body, trust, invalidated_at}],
    corrections}'
```

```json
[
  {
    "fact_id": "29a0987a-85d2-42d4-81a6-affd530461f8",
    "body": "This synthetic project uses Poetry for dependency management.",
    "trust": "candidate",
    "invalidated_at": null
  }
]
{
  "facts": [
    {
      "fact_id": "aae7d69f-ed93-4e66-b6c3-8cd73be3ad49",
      "body": "This synthetic project uses uv for dependency management.",
      "trust": "candidate",
      "invalidated_at": "2026-11-08T16:13:20.014633Z"
    },
    {
      "fact_id": "29a0987a-85d2-42d4-81a6-affd530461f8",
      "body": "This synthetic project uses Poetry for dependency management.",
      "trust": "candidate",
      "invalidated_at": null
    }
  ],
  "corrections": [
    {
      "fact_id": "aae7d69f-ed93-4e66-b6c3-8cd73be3ad49",
      "invalidated_at": "2026-11-08T16:13:20.014633Z",
      "principal_id": "5eabe213-3d9d-4f4f-b312-a95268a9ff81",
      "reason": "The project moved from uv to Poetry.",
      "superseded_by": "29a0987a-85d2-42d4-81a6-affd530461f8"
    }
  ]
}
```

Current recall excludes the invalidated uv claim. History still returns it
unchanged, with its invalidation time, the correction's reason and principal,
and `superseded_by` pointing at the Poetry fact. Both facts remain candidates:
correcting a belief does not validate its replacement.

## 6. Run the check

```sh
python scripts/check_fresh_session_correction.py "$demo/out"
```

```json
{
  "status": "passed",
  "checks": [
    "custody: session A's claim has a committed custody receipt",
    "fresh session: session B, with a new session ID and the same scope, recalled session A's candidate claim through Cairn",
    "correction: committed once, replayed under the same mutation",
    "recall: the invalidated belief is excluded, the replacement found",
    "history: old belief, correction reason and replacement link retained"
  ]
}
```

[`scripts/check_fresh_session_correction.py`](../scripts/check_fresh_session_correction.py)
reads only the saved JSON results. It makes no network or provider call and
does not depend on timing or generated IDs. It asserts committed custody;
that session B used a different session ID with the same principal and scope
and found session A's candidate claim through Cairn; that the correction
committed once and replayed under the same mutation; that current recall
excludes the old belief; and that history retains the old belief, the
correction and the replacement link. The repository test
`tests/demo/test_fresh_session_correction_demo.py` runs every block on this
page verbatim against a fresh instance, then runs the check.

## 7. Clean up

```sh
exit
```

Leaving the throwaway shell stops the server and deletes the temporary
directory, including the catalogue, credentials and saved outputs.

## What this does and does not show

- Sessions are separated by process and session identity, not by principal.
  Both use one synthetic agent principal on purpose, so attribution is the
  same for both sessions; see the [shared-memory guide](shared-memory.md)
  for several principals.
- Agent hosts are not involved. A host skill can run the same commands when
  explicitly invoked (see [host workflows](host-memory-workflows.md)), but this
  walkthrough records no host evidence and claims no automatic capture or
  lifecycle hooks.
- Trust stays `candidate` throughout. Validation is a separate decision that
  this walkthrough does not make.
