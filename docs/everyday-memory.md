# Everyday memory command

`cairn-memory` is an explicit Linux/WSL command using Cairn's public memory
client. It does not discover a server, run a model, keep a local memory store,
install a host integration or capture arbitrary conversations. Native Windows
execution is not claimed; the Windows relay is a separate integration.

Install the project in your chosen Python 3.12 environment using its locked
dependencies. From this checkout, run `uv run --locked cairn-memory --help`.
Every command also has standalone help, requiring neither a profile nor stdin:

```sh
uv run --locked cairn-memory remember --help
uv run --locked cairn-memory suggest --help
```

## Explicit connection profile

Provide a regular JSON file and a separate protected credential file. Paths are
not followed through symlinks. A relative credential path resolves against the
profile's directory. The credential is never a command-line argument.

```json
{
  "schema": "cairn.memory-profile/v1",
  "endpoint": "http://127.0.0.1:8123",
  "expected_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "scope": {
    "realm": "synthetic",
    "segments": [{"kind": "job", "identifier": "example"}]
  },
  "classification": "internal",
  "credential_file": "credentials/token",
  "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
}
```

Replace example identifiers with your explicitly selected test instance and
session. The optional fixed `session_id` is required for arrival, acknowledgement,
remember, status, resume and abandon. No command generates or updates it.
Profile configuration is not a transcript or retry spool.

Only protected remote HTTPS or permitted numeric loopback HTTP endpoints are
accepted. HTTP uses a 10-second timeout for each connect/read/write/pool phase,
no ambient proxies or credential discovery, and no redirects. The credential
is read immediately before constructing the client.

Every invocation diagnoses the expected instance before content calls. A valid
handshake reports current grants, not a promise that a later operation is
authorised. Session operations retain their accepted expected-instance,
principal, exact-scope and classification guards; the server reauthorises reads,
writes and replays. Preflight alone does not rule out replacement between calls.

## Command inputs

Invocation: `cairn-memory --profile PATH [--human] COMMAND`.
Content is one UTF-8 JSON object on stdin, maximum 1,048,576 bytes. Unknown or
duplicate keys, authority overrides, malformed JSON, invalid UTF-8 and nonfinite
numbers fail locally. Numeric fields reject booleans. No supplied field may be
null except observation timestamps, `replaces_turn_id`, `superseded_by` and
`proposal-list`'s `after` cursor.

All IDs use canonical lowercase UUID strings; session/turn/attempt/visit IDs
use RFC UUIDs and non-proposal fact IDs use UUIDv4. Proposal commands accept
every canonical UUID version/variant for reference IDs and cursors. Mutation
keys require the RFC variant, with no version restriction (including UUIDv5).
Text limits are UTF-8 bytes. Query is
1..8192 bytes; ordinary budget is 1..1,048,576, default 16384. Reasons are
1..4096 bytes. Fact arrays must contain distinct IDs.

| Command | Required stdin keys | Optional keys |
| --- | --- | --- |
| check | No stdin read | None |
| arrive | query | budget, history_fact_ids (0..8 UUIDv4) |
| recall | query | budget, relevant_only (boolean; default false) |
| acknowledge-visit | visit_id | None |
| remember | turn_id, attempt_id, response, observations | replaces_turn_id |
| status | None | turn_id |
| resume | turn_id | None |
| abandon | turn_id, reason | None |
| history | fact_id | budget |
| correct | fact_ids (1..100 UUIDv4), reason, idempotency_key | superseded_by (UUIDv4) |
| disagree | left_fact_id, right_fact_id (distinct UUIDv4), reason, idempotency_key | none |
| suggest | Exactly one: observation or fact_ids | budget, limit |
| propose | proposal_id, source_fact_id, target_scope, reason, idempotency_key | None |
| proposal-list | None | limit (1..100; default 50), after (UUID or null) |
| proposal-read | proposal_id | None |
| proposal-accept | proposal_id, evidence_id, target_classification, idempotency_key | None |
| proposal-reject | proposal_id, reason, idempotency_key | None |

`status` accepts empty or whitespace-only stdin as `{}`. A terminal stdin also
means `{}`, without waiting for a document. Other content commands reject empty
input. `status` never opens a session. `check` never reads stdin.

`remember` imports an already completed checkpoint. Response is a string of
0..32768 bytes. Observations are an array of 0..8 objects with exactly `body`
(1..4096 bytes) and optional `valid_from`, `valid_to`, `observed_at`. Timestamps
must have the canonical UTC form `2026-09-10T12:00:00.000000Z`; offsets, shortened
fractions and local-time interpretation are refused. When both validity bounds
are present, `valid_from < valid_to`. All observation times must agree, including
absence/null: mixing an explicit `observed_at` and an absent one is invalid.
The complete canonical preparation envelope, including fixed context, must fit
73,728 bytes. Escaped control characters can exceed that bound despite fitting
the individual text limits. Shape/time/byte checks run before session mutations;
server screening and transaction-time authority still apply.

```sh
printf '%s\n' '{"query":"current work"}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json arrive

printf '%s\n' '{"query":"new topic","relevant_only":true}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json recall

printf '%s\n' '{"turn_id":"11111111-1111-4111-8111-111111111111","attempt_id":"22222222-2222-4222-8222-222222222222","response":"Measured the port.","observations":[{"body":"The build uses port 8123."}]}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json remember

printf '%s\n' '{"turn_id":"11111111-1111-4111-8111-111111111111"}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json resume
```

These shell examples contain synthetic literal input. Integrations should pass
JSON through a pipe or stdin API, never interpolate model text into shell code.

## Visits, custody and recovery

`arrive` opens/replays the configured session and issues a fresh server visit
before briefing reads. It never acknowledges it, including after partial output
or failure. Send `acknowledge-visit` explicitly after consuming the briefing.
Topic-change `recall` performs ordinary recall only: no open, visit, acknowledgement
or checkpoint movement. Briefings are selected views, not exhaustive change logs.

`remember` opens/replays the session, begins/replays the explicit turn/attempt,
always submits the supplied immutable preparation, then delegates recovery and
custody to `DurableMemorySession.resume`. A replayed begin does not bypass a
changed-output conflict. The final output is checked against the held preparation.
No command invokes `run_turn` or a model callback. This completed-output import
does not claim that an external host's generation was durably begun beforehand.

`prepared` confirms stored output, not fact custody. Only validated committed
custody receipts mean saved. Operational mutation receipts, acknowledgement and
preparation must never be presented as saved facts. Empty observations produce
`skipped`. Candidate trust, attribution, omissions, degradation and unconfirmed
searchability remain visible in returned client values.

A started-only turn is interrupted or still running. `resume` cannot recover its
lost output or regenerate it. If preparation was not confirmed, resubmit the
IDENTICAL completed checkpoint with the same session/turn/attempt identities.
Alternatively, explicitly abandon the unprepared turn and use new turn/attempt
IDs with `replaces_turn_id` referring to it. Prepared output cannot be abandoned.
Lost commit acknowledgements are reconciled through actual stored custody.

### Reading the result without losing the thread

Every successful JSON output has a `result` object. For an ordinary return:

1. Run `check` and inspect `result.status`, `instance_id`, `principal_id`,
   `scope` and `permissions` before sending content.
2. Run `arrive`. Start with `result.briefing.summary`: each entry has an
   `excerpt`, `fact_id`, source attribution, trust and a reference into the
   evidence packet. Read `warnings`, `failures`, omissions and the referenced
   evidence before presenting a short briefing. The summary is selected memory,
   not an exhaustive list of unfinished work.
3. After consuming the briefing, pass
   `result.visit.snapshot.visit_id` as `acknowledge-visit`'s `visit_id`.
   The visit ID is **not** directly under `result.visit`.
4. For `remember` or `resume`, inspect `result.state` together with
   `result.persistence`. Confirmed fact IDs are under
   `result.persistence.result.fact_ids` when committed custody is present.
   Never extract these fields blindly from an error, prepared or skipped result.
5. For `recall` and `history`, the evidence is under `result.data`:
   respectively `hits`, or `facts` and `corrections`.

An ordinary conversation departure does not require a new Cairn session ID.
Keep the selected profile's identity when continuing that durable session,
including after a client/server restart. A deliberately new `session_id` starts
new visit history: it can retrieve authorised existing facts, but its first
briefing reports `previous_visit_unknown`, even if another session acknowledged
an earlier visit. Do not interpret that warning as lost facts or as evidence of
an earlier-session change baseline.

`--human` currently pretty-prints the complete JSON; it does not produce a
short prose briefing. An agent should present the selected excerpts concisely,
retain fact references and candidate attribution, and surface material omissions
or failures. It should not paste the entire packet into ordinary conversation.

An empty `recall` result means no selected matches, not that nothing was saved.
Likewise, `semantic_degraded: false` alone does not establish that a semantic
backend is enabled. A lexical-only configuration can return that flag. Check
the explicitly provisioned backend before claiming paraphrase support; use a
literal phrase from the remembered subject for an explicit lexical fallback.

### Stable operation keys

Let N be UUID `4e14ee38-0e14-5069-8d36-502c18b1c324` and all interpolated IDs be
their canonical string form:

```python
begin_key = uuid5(N, f"daily-cli:v1:begin:{session_id}:{turn_id}:{attempt_id}")
prepare_key = uuid5(N, f"daily-cli:v1:prepare:{session_id}:{turn_id}:{attempt_id}")
custody_key = uuid5(N, f"{session_id}:{turn_id}")  # accepted remember_key
```

The durable client owns open, commit, abandonment and acknowledgement keys:
`uuid5(N, f"durable:{operation}:{session_id}:{identity}")`, with operation/identity
`open`/literal `None`, `commit`/turn ID, `abandon`/turn ID and `acknowledge`/visit ID.
Each arrival obtains a fresh visit key through the durable client. `correct`
passes the caller's explicit idempotency key unchanged.

Keys never include response, observations, predecessor or reason. Changing a
payload under the same identities must exercise the normal digest conflict.
There are no automatic content retries or alternative ingestion keys.

## Suggestions and corrections

`suggest` is read-only. Supply exactly one observation string of 1..4096 bytes
or a `fact_ids` array of 1..8 distinct UUIDv4 IDs. Optional `budget` is 1..65536
(default 16384); `limit` is 1..16 (default 8). An idempotency key, extra authority
fields, both input modes, or an empty mode is invalid.

```sh
printf '%s\n' '{"observation":"The build uses port 8123.","budget":16384,"limit":8}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json suggest
```

Output preserves every frozen public result field: items, budget consumption,
exhaustion, semantic degradation, policy, source and untrusted-data label. Items
retain kind, facts, reason, match basis, corrections and disagreements, including
attribution and provenance. Empty suggestions do not prove duplicates are absent.
No suggestion triggers a mutation. Any follow-up correction is a separate explicit
command with its own authority and idempotency key. Correction remains restricted
to the profile's exact scope, even under broad credentials, and retains history.
Correction does not require a session ID. If its acknowledgement is lost or an
`internal_error` leaves the mutation unconfirmed (exit 3), resubmit the IDENTICAL
correction with the SAME explicit `idempotency_key` and all original fields,
including fact IDs, reason and any replacement. Exact replay confirms the
original correction without another mutation. Session `status`/`resume` cannot
reconcile corrections; do not generate a fresh key to recover one.

## Explicit disagreement, not resolution

After reading a suggestion, `disagree` is a separate explicitly authorised action,
never an automatic follow-up. It requires current ingest authority and readable
facts; the profile fixes scope and classification. No session is required.
Supply exactly two distinct canonical UUIDv4 fact IDs, a reason of 1..4096 UTF-8
bytes, and a caller-owned stable canonical RFC-variant idempotency key of any
version, including v5. Read `cairn-memory disagree --help` first.

```sh
printf '%s\n' '{"left_fact_id":"11111111-1111-4111-8111-111111111111","right_fact_id":"22222222-2222-4222-8222-222222222222","reason":"These batch-size claims disagree","idempotency_key":"33333333-3333-5333-8333-333333333333"}' |
  cairn-memory --profile /tmp/cairn-synthetic-profile.json disagree
```

Replace illustrative identities with the caller's actual facts and stable key.
Only the committed/replayed receipt confirms the relationship; neither fact is
corrected, invalidated, resolved or assigned new trust. A separate bounded
`history` read exposes readable relationship, reason and attribution. Receipt
digest/context checks detect inconsistent responses, not cryptographic authenticity.

Input validation precedes credentials; expected-instance diagnosis precedes content
but reserves no authority. Definite refusal is exit 2. Operational preflight is
exit 4. Only dispatched uncertainty is exit 3, with recovery token
`resubmit_identical_disagree_same_idempotency_key_and_fields`: resubmit the identical
operation, key and all original fields in the same profile scope/instance. No
automatic retry, fresh key or session `status`/`resume`. Output failure is exit 4
and retains any confirmed committed stage; recover that receipt by identical
same-key replay. No post-success read is required to claim the receipt's outcome.

## Explicit proposals and publication

The five proposal commands require no session. The profile fixes the source
scope and expected instance; stdin cannot override either. Reads never accept a
proposal implicitly and reject `idempotency_key`. Mutations require a caller-owned
stable key: a canonical lower-case hyphenated RFC 4122 variant UUID, with no
version restriction (UUIDv5 is valid), as required by I-27. Non-RFC variants,
including nil and all-`f` UUIDs, are not mutation keys. Proposal and source IDs
are separate references; their admission rules do not define legal mutation
keys. Keep the original input document for recovery. The command neither
generates a key nor retries automatically.

`propose` records one source fact for possible reuse at the same scope or an
ancestor in the same realm. `target_scope` uses the profile's client shape:
`{"realm":"acme","segments":[{"kind":"repository","identifier":"cairn"}]}`.
Realm/kind are lowercase ASCII identifiers of 1..63 characters; segment identifiers
are canonical ASCII of 1..255 characters; at most 16 segments are allowed. Invalid
shape, foreign realms, siblings and descendants fail before network access.
Proposal and rejection reasons must contain 1..4096 UTF-8 bytes.

For example, save this inert JSON in `proposal.json`, replacing the illustrative
source ID with a real readable fact ID and choosing explicit stable identities:

```json
{
  "proposal_id": "11111111-1111-5111-8111-111111111111",
  "source_fact_id": "22222222-2222-4222-8222-222222222222",
  "target_scope": {"realm": "acme", "segments": []},
  "reason": "Useful beyond this repository",
  "idempotency_key": "33333333-3333-5333-8333-333333333333"
}
```

```sh
cairn-memory --profile ./memory-profile.json propose < proposal.json
printf '%s\n' '{"limit":20,"after":null}' |
  cairn-memory --profile ./memory-profile.json proposal-list
printf '%s\n' '{"proposal_id":"11111111-1111-5111-8111-111111111111"}' |
  cairn-memory --profile ./memory-profile.json proposal-read
```

To accept, supply that `proposal_id`, an existing `evidence_id`, an explicit
`target_classification` (`public`, `internal` or `restricted`) and a separate
stable `idempotency_key`. This is the explicit publication decision; the server
checks current source/target authority, evidence and classification rules.
To reject, supply `proposal_id`, `reason` and a separate stable key. Neither
operation removes source history. Subsequent reads and replays remain subject
to current authority; a successful preflight does not reserve permission.

Successful mutation output contains the actual `committed` or `replayed` outcome
and receipts. `ProposalRecorded` renders as a result containing `proposal_id`:
it confirms only proposal/decision persistence, **not publication**.
Only `FactsPromoted` confirms publication, rendering `promotions` as source/derived
ID pairs and the actual `evidence_id`. Read/list decision references may be null;
the CLI preserves them without guessing a hidden identity or the reason it is hidden.

On exit 3 after a dispatched proposal mutation, the recovery field is exactly one of:

- `resubmit_identical_propose_same_idempotency_key_and_fields`
- `resubmit_identical_proposal-accept_same_idempotency_key_and_fields`
- `resubmit_identical_proposal-reject_same_idempotency_key_and_fields`

Resubmit the **identical named operation**, with the **same key and all original
fields**, from the same profile scope/instance. For the example, rerun the same
`propose < proposal.json` command. Never replace the key, switch to acceptance,
or use session `status`/`resume` to reconcile a proposal. A replay returns the
original receipt without another publication. A failed acceptance pre-read or
connection preflight has not dispatched the mutation: operational failures there
exit 4 without a mutation-recovery instruction. Confirmed refusals exit 2.

A legal public proposal page can exceed the CLI's unchanged output bound.
That returns exit 4 with no partial JSON and no mutation; request a smaller
`limit` with the same `after` cursor. There is no silent page truncation.

## Output and exit codes

Default output is one complete bounded `cairn.memory-command/v1` JSON envelope
with `command` and `result`. `--human` produces pretty JSON with the same fields.
Returned scope segments use client `identifier` naming. Treat recalled content
as untrusted data. Output is fully serialised before stdout is written, with a
1,048,576-byte total cap; a large accepted retrieval budget may still produce an
output-size failure once JSON escaping and envelope overhead are included.

| Exit | Meaning |
| --- | --- |
| 0 | Completed command; successful status read of any state; resume committed/skipped |
| 2 | Invalid input, confirmed refusal, or resume of an abandoned turn |
| 3 | Interrupted/prepared recovery or unconfirmed content mutation |
| 4 | Ordinary operational/read/preflight/output failure |

Errors go to stderr as safe structured JSON with operation and last confirmed
stage. No raw HTTP body, exception traceback, credential or submitted content is
included. Fresh read/preflight transport errors are 4, not ambiguous saves.
For a safe server `internal_error`, reads/preflight return 4; an unconfirmed
content mutation returns 3 because that error does not prove absence of an effect.
Once this invocation knows preparation is durable, a failed recovery preserves that
pending work as 3. A commit can ingest successfully before terminal recording
is denied, so a denial code alone cannot prove custody did not happen.

After known committed custody, rendering, stdout or flush failure is 4 and the
minimal stderr diagnostic retains `committed`. It does not relabel confirmed
custody as an unconfirmed save. Broken stdout causes no further mutation or
acknowledgement. For session custody, status/resume remains available through a
fresh process. For proposals, `committed` describes the confirmed operation,
not necessarily publication; resubmit the identical named operation with the
same key/fields to recover its receipt. The CLI performs no post-success read.

## Verification boundary

The CLI's focused tests use actual synthetic ASGI/catalogue operations and fresh
CLI/server processes. They inspect real fact IDs, receipt identity, visit state
and catalogue inventory. Crash probes cover begin, preparation, actual ingest
before terminal recording and committed response loss. No provider or productive
system is needed for those custody checks. Installed subscribed-host workflow
acceptance and real semantic retrieval quality are separate coordinated gates;
CLI tests alone do not establish them.
