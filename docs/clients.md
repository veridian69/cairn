# Cairn client guide

**Last updated:** 13 September 2026.

This guide is for software calling one Cairn v0.1 instance through REST or
MCP. Operators deploying, backing up or migrating an instance should use the
[deployment](operations/deployment.md),
[Docker Compose](../deploy/compose/README.md),
[backup and restore](operations/backup-restore.md), and
[legacy migration](operations/migration.md) runbooks instead.

## Pin the contract

The generated contracts are the wire authority:

| Surface | Artefact | SHA-256 |
| --- | --- | --- |
| REST | [`contracts/cairn-openapi-v1.json`](../contracts/cairn-openapi-v1.json) | `16a6d8b6f81182bc29ea3df0c3f68408a124a06ebc7bc33d03913f1e8cb8f045` |
| MCP | [`contracts/cairn-mcp-tools-v1.json`](../contracts/cairn-mcp-tools-v1.json) | `691a3603c9368b8386b3546ea04a02a924aa8d4fe5bfab594cfa71a395f825fb` |

Verify a checkout before generating a client or accepting its schemas:

```sh
(cd contracts && sha256sum -c cairn-openapi-v1.json.sha256 cairn-mcp-tools-v1.json.sha256)
```

An authenticated `GET /v1/instance` returns `contract_identity`,
`contract_digest`, `mcp_contract_digest`, `product_version` and `instance_id`.
Compare both served digests with the files above before sending data. A client
should also pin the expected instance UUID: a valid credential presented to
the wrong Cairn is still the wrong destination.

The REST base path is `/v1`; the direct Streamable HTTP MCP endpoint is
`POST /v1/mcp`. TLS is mandatory outside numeric loopback. Cairn does not
terminate TLS itself, perform OAuth discovery or expose an anonymous `/v1`
operation.

## Credentials

Every `/v1` REST operation and every MCP protocol method requires exactly one
opaque bearer credential. Tokens have the form
`cairn1.<credential-uuid>.<url-safe-secret>`, but clients must treat the whole
value as opaque.

There is no network bootstrap or token-recovery endpoint. An operator creates
the first human principal and credential locally with `cairn bootstrap`; an
authorised `grant-manage` principal can then create principals, issue
credentials and create grants through `/v1`. Plaintext credentials are
returned once. Store them in an owner-only credential store; do not place them
in Git, configuration committed to Git, command arguments, environment
variables, URLs, logs or traces.

The examples below read a token from an owner-only file without putting it on
`curl`'s command line. Run them on Linux with curl 8.4.0 or newer, `jq` and
Python 3.12–3.14 available as `python3`. The standard-library helper runs
independently of Cairn's pinned Python 3.12 runtime; the host system interpreter
does not need replacing. The bounded helper uses curl's
[`--max-filesize`](https://curl.se/docs/manpage.html#--max-filesize)
transfer-time limit, which protects unknown-length responses from 8.4.0. Check
the installed version with `curl --version`. The default credential path
matches the Compose guide; change only that variable if the installation
retained it elsewhere.

```bash
set -eu
umask 077
: "${base_url:=http://127.0.0.1:8080}"
: "${credential_file:=$HOME/.config/cairn/cairn-a-admin.token}"
test -f "$credential_file" && test ! -L "$credential_file"
test "$(stat -c '%u' "$credential_file")" = "$(id -u)"
python3 - "$credential_file" <<'PYTHON'
import pathlib, re, sys
value = pathlib.Path(sys.argv[1]).read_bytes()
if len(value) > 16384 or not re.fullmatch(rb"cairn1\.[A-Za-z0-9_.-]+[\r\n]*", value):
    raise SystemExit("invalid credential file")
PYTHON

credential_mode="$(stat -c '%a' "$credential_file")"
case "$credential_mode" in
  400|600) ;;
  *) printf 'credential must be owner-only (0400 or 0600)\n' >&2; exit 1 ;;
esac
curl_config="$(mktemp)"
chmod 600 "$curl_config"
trap 'rm -f "$curl_config"' EXIT
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' < "$credential_file"
  printf '"\n'
} > "$curl_config"
```

Do not use `curl -v`, shell tracing or a diagnostic proxy with this
configuration: all can disclose the header.

## Scopes, principals and grants

A scope is a realm plus an ordered path of `(kind, identifier)` segments:

```json
{
  "realm": "local",
  "segments": [
    {"kind": "repository", "identifier": "example"},
    {"kind": "job", "identifier": "build-42"}
  ]
}
```

Segment kinds are lowercase labels; identifiers are opaque, case-sensitive
identities rather than display names. A caller-supplied scope is a request,
not authority. Cairn evaluates the authenticated principal's current grants
server-side, denies by default, and never exposes sibling or descendant
scopes through an ancestor request.

Principals are `human` or `workload`. A grant binds one principal to one realm
and scope prefix, explicit operations, a read clearance and explicit writable
classifications. The classification order is
`public < internal < restricted`; even `public` data requires authentication.
Workload grants require an expiry. Grants are immutable: replace and revoke
them rather than editing them.

The grant operation vocabulary is closed:

- data: `retrieve`, `ingest`, `promote`, `invalidate`;
- metadata-only audit access: `audit-read`;
- bounded grant administration: `grant-manage`.

`grant-manage` is not a wildcard. Delegated grants must remain within the
manager grant's realm, scope, operation, clearance, classification and expiry
envelope, and `grant-manage` itself cannot be delegated in v0.1.

## REST calls

All request bodies are strict JSON: unknown fields and duplicate keys are
refused, and the body limit is 2 MiB. Send `Content-Type: application/json`.
An optional UUIDv4 `X-Correlation-ID` is echoed; an invalid value is replaced
rather than rejected.

Eight mutation routes also require an `Idempotency-Key` header containing a
canonical lowercase UUID:

| Route | Required authority | Purpose |
| --- | --- | --- |
| `POST /v1/ingest` | `ingest` at the target scope and classification; `promote` too when requesting `validated` | Accept one assertion and its facts into custody. |
| `POST /v1/promote` | `retrieve` at each source scope and `promote` at the target | Create validated facts without changing their sources. |
| `POST /v1/invalidate` | `invalidate` at each fact's scope | End validity without deleting history. |
| `POST /v1/create-principal` | `grant-manage` | Create a `human` or `workload` principal in the realm. |
| `POST /v1/issue-credential` | `grant-manage` | Return a new plaintext credential once. |
| `POST /v1/revoke-credential` | `grant-manage` | Revoke one credential with a reason code. |
| `POST /v1/create-grant` | `grant-manage` | Create a grant inside the caller's delegation envelope. |
| `POST /v1/revoke-grant` | issuing grant or realm-root `grant-manage` | Revoke one immutable grant with a reason code. |

The three reads reject `Idempotency-Key`:

| Route | Required authority | Purpose |
| --- | --- | --- |
| `POST /v1/retrieve` | `retrieve` covering the requested scope | Return reconciled facts within a byte budget. |
| `POST /v1/read-audit-events` | `audit-read` covering the requested prefix | Return metadata-only events; it is a POST so scopes never enter URLs. |
| `GET /v1/instance` | authenticated credential | Return instance and contract identity. |

The OpenAPI artefact is authoritative for every field and response. Mutation
successes always return HTTP 200 with `outcome`, `result`,
`mutation_receipt` and `audit_receipt`; a replay is
`outcome: "replayed"`, not a different status. The three reads return their
result directly.

### Check the instance

```bash
instance_response="$(mktemp)"
instance_headers="$(mktemp)"
trap 'rm -f "$curl_config" "$instance_response" "$instance_headers"' EXIT
if instance_status="$(curl --disable --silent --show-error --noproxy '*' \
    --max-time 10 --config "$curl_config" \
    --output "$instance_response" --dump-header "$instance_headers" \
    --write-out '%{http_code}' "$base_url/v1/instance")"; then
  printf 'Instance HTTP %s\n' "$instance_status"
  cat "$instance_response"
  printf '\n'
else
  printf 'Instance transport failed\n' >&2
  exit 1
fi
test "$instance_status" = 200
jq -e '{instance_id, product_version, contract_identity, contract_digest,
        mcp_contract_digest} |
       select(.contract_identity == "cairn/v1" and
              (.instance_id | type == "string") and
              (.contract_digest | type == "string") and
              (.mcp_contract_digest | type == "string"))' \
  "$instance_response"
```

Require `contract_identity` to be `cairn/v1`, both digests to match the pinned
values above, and `instance_id` to match the intended target.

## Bounded ingest and retrieval verification

Run this in the same Bash session as the credential setup above. Move to the
repository root explicitly before continuing:

```sh
cd "$(git rev-parse --show-toplevel)"
```

Keep `base_url` on numeric loopback. The example uses the `local`
realm and needs `ingest` and `retrieve` authority at repository `example` with
`internal` clearance. The documented bootstrap credential has this authority.
Semantic retrieval must be configured first; see the
[Compose procedure](../deploy/compose/README.md#optional-semantic-retrieval).

### Ingest

This writes one synthetic candidate fact. It retains the exact request,
idempotency key and response outside Git in an owner-only directory. Do not
run this block again merely because retrieval is waiting.

```bash
set -eu
umask 077
install -d -m 0700 "$HOME/.local/state/cairn-checks"
verification_dir="$(mktemp -d "$HOME/.local/state/cairn-checks/check.XXXXXXXX")"
printf 'Retained verification files: %s\n' "$verification_dir"
request_file="$verification_dir/ingest.json"
response_file="$verification_dir/ingest-response.json"
python3 -c 'import uuid; print(uuid.uuid4())' > "$verification_dir/idempotency-key"
idempotency_key="$(cat "$verification_dir/idempotency-key")"

jq -n '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  facts: [{body: "The example repository uses a locked dependency set."}]
}' > "$request_file"

if http_status="$(curl --disable --silent --show-error --noproxy '*' \
  --max-time 30 --request POST --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $idempotency_key" \
  --data-binary "@$request_file" --output "$response_file" \
  --dump-header "$verification_dir/ingest-headers" --write-out '%{http_code}' \
  "$base_url/v1/ingest")"; then
  printf 'Ingest HTTP %s\n' "$http_status"
  cat "$response_file"
  printf '\n'
else
  printf 'Transport failed; commit is uncertain. Keep %s and replay the same key/request.\n' "$verification_dir" >&2
  exit 1
fi
test "$http_status" = 200
jq -e '
  (.outcome == "committed" or .outcome == "replayed") and
  (.mutation_receipt | type == "object") and
  (.audit_receipt | type == "object") and
  (.result.fact_ids | type == "array" and length == 1 and
    all(.[]; type == "string"))
' "$response_file" >/dev/null
jq -er '.result.fact_ids[0] | select(type == "string")' "$response_file" > "$verification_dir/fact-id"
```

A successful acknowledgement and its mutation/audit receipts establish committed
custody. They do **not** establish completed indexing. If transport fails after
submission, do not create a fresh key: recover the printed directory, set
`verification_dir`, `request_file`, `response_file` and `idempotency_key` from
its saved files, and repeat only the `if http_status=...` request and response
checks above. A byte-equivalent request with the same key is safe to replay.
An HTTP error is printed in full before the block stops; follow the failure
policy below rather than retrying blindly.

### Retrieve and trust filters

Run this after committed/replayed custody, with the same `verification_dir`:

```sh
python3 scripts/verify-retrieval.py \
  --base-url "$base_url" \
  --credential-file "$credential_file" \
  --fact-id "$(cat "$verification_dir/fact-id")" \
  --deadline 120 --request-timeout 10 --max-attempts 30
```

The helper makes only `POST /v1/retrieve` calls, without an idempotency key. It
queries `locked dependency set` at `local/repository:example`, uses budget
65536 and explicitly requests `trust_filters: ["candidate"]`. The default
trust filter is `validated`; omitting the candidate filter would conceal this
newly ingested candidate. The query is opaque UTF-8; Cairn owns no query
language. Budgets are fact-body bytes, from 1 to 1,048,576.

While waiting, stderr retains HTTP status, the failure envelope (including code,
retry class and correlation ID) and `Retry-After`. Only HTTP 503 with
`index_pending`, `stale_index` or `dependency_unavailable` **and**
`retry: "after-delay"` permits another retrieval. `index_pending` and
`stale_index` describe projection readiness; `dependency_unavailable` may mean
a failed or contended dependency and must not be presented as harmless indexing.
A valid numeric or HTTP-date `Retry-After` controls the wait. A missing or invalid
header, a delay beyond the remaining deadline, another failure class, malformed
success or HTTP 200 without the exact fact ID/body is a failed check.

The helper stops after at most 30 attempts or 120 seconds, whichever comes
first; each request has a 10-second timeout. It returns nonzero on failure.
Inspect the retained failure and the installation's service health,
logs and semantic-provider configuration; correct the cause and rerun **only the
retrieval command with the same fact ID**. Do not repeat committed ingest. The
helper refuses redirects, environment proxies and non-loopback endpoints, and
reads an owner-owned regular credential file with mode 0400 or 0600.

Success prints `"status": "verified"` with the exact saved ID and body. This
proves authenticated custody-to-search retrieval for this synthetic candidate
through the configured projection. It does not prove every fact is indexed,
recall quality, backups or production readiness. Retain the verification directory
for the restart test: restart Cairn using your installation guide and rerun this
same read-only command. Do not filter an error response through a success-only
`jq '{hits, budget_consumed, budget_exhausted}'` projection: that hides the reason
for failure behind null fields.

## Read exact evidence

`POST /v1/read-evidence` and MCP `read-evidence` take `scope` and the
`evidence_id` returned by ingest. They require a live `retrieve` grant covering
the requested scope. The catalogue permits only evidence in the same realm,
at that scope or an ancestor, within the caller's read clearance. Reads reject
idempotency keys. Attic remains private; clients never address its database.

The flat result contains `evidence_id`, `payload`, `sha256`, `byte_length` and
`media_type`. Payload is the exact UTF-8 text accepted by `evidence_payload`;
SHA-256 and byte length describe its UTF-8 bytes. The fixed media type is
`text/plain; charset=utf-8`. This does not introduce binary uploads. The server
checks the stored bytes against the catalogue before disclosing them.

Exact reads inspect source custody, independently of semantic indexing and
associated facts' trust or invalidation. Returned source text is not a validated
fact claim. Unknown, inaccessible and external-reference evidence all return
`not_found`. Queued delivery returns `evidence_pending` (503, `after-delay`);
corruption returns `evidence_corrupt` (500, `never`). Infrastructure failure or
missing stored bytes without queued delivery returns `dependency_unavailable`.

Follow the [Attic payload round-trip](operations/evidence-verification.md) for
literal ingest, bounded byte/hash comparison and restart verification commands.

## Failures and retry policy

REST failures use one stable envelope:

```json
{"failure":{"code":"...","message":"...","retry":"...","correlation_id":"..."}}
```

Only `invalid_request` and `secret_rejected` may include bounded `detail`.
Clients should branch on `failure.code` and `failure.retry`, never the prose
message. The closed codes are:

```text
invalid_request  authentication_failed  authorisation_denied  secret_rejected
not_found  idempotency_conflict  index_pending  stale_index
dependency_unavailable  instance_mismatch  internal_error
```

Authentication failures deliberately do not reveal whether the credential,
principal or grant exists. A denied or failed call still appends an audit
event, so blind retries are both noisy and durable. Retry only when the
returned retry class permits it; preserve the idempotency key for a mutation.

## MCP clients

The direct endpoint is stateless Streamable HTTP at `POST /v1/mcp`. Configure
the same Cairn bearer credential as a static header. Cairn deliberately
implements no MCP OAuth flow, discovery metadata, subscriptions,
notifications from server to client or server-side sessions. A compatible
client must accept JSON and support protocol revision `2025-11-25`.

The admitted protocol methods are `initialize`, `ping`, `tools/list`,
`tools/call` and the client's `notifications/initialized`. The twelve tools
are named exactly:

```text
ingest  promote  invalidate  create-principal  issue-credential
revoke-credential  create-grant  revoke-grant  read-audit-events
retrieve  read-evidence  instance
```

For the eight mutations, `idempotency_key` is a tool argument, not an HTTP
header. The four reads reject it. Otherwise the arguments and result objects
match REST. Operation failures are successful JSON-RPC responses containing
an MCP error result (`isError: true`) with the same Cairn failure envelope;
authentication and protocol faults are decided before a tool exists and use
HTTP or JSON-RPC errors respectively.

The canonical generated tool schemas and failure vocabulary are in the pinned
MCP contract at the start of this guide. Do not infer schemas from a
particular SDK's generated types.

For the Cloudflare-fronted Cairn service, use the local `cairn-mcp` relay
rather than placing Cloudflare credentials in an MCP client. Its local bearer
token authenticates the loopback relay and is not a Cairn credential. Follow
the [relay operations guide](operations/cairn-mcp-relay.md) for its STDIO and
HTTP configurations, credential-file rules and rotation procedure.
