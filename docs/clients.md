# Cairn client guide

**Last updated:** 1 September 2026.

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
| REST | [`contracts/cairn-openapi-v1.json`](../contracts/cairn-openapi-v1.json) | `0e4dc424225a7bb12f3b0c1c2008a5bf30415c840a6fc60247079f986d7b4bf3` |
| MCP | [`contracts/cairn-mcp-tools-v1.json`](../contracts/cairn-mcp-tools-v1.json) | `b75f3fa736c6f4285b180e4795945fdc0d145a8ce6bcf60884f118b9636e74ca` |

Verify a checkout before generating a client or accepting its schemas:

```sh
sha256sum -c contracts/cairn-openapi-v1.json.sha256
sha256sum -c contracts/cairn-mcp-tools-v1.json.sha256
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
`curl`'s command line. Run them on Linux with `curl`, `jq` and Python 3:

```sh
base_url=http://127.0.0.1:8080
credential_file=/absolute/path/to/cairn-credential

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

```sh
curl --silent --show-error --fail-with-body \
  --config "$curl_config" \
  "$base_url/v1/instance" |
  jq '{instance_id, product_version, contract_identity, contract_digest, mcp_contract_digest}'
```

Require `contract_identity` to be `cairn/v1`, both digests to match the pinned
values above, and `instance_id` to match the intended target.

### Ingest

This example writes synthetic candidate memory at one repository scope:

```sh
request_file="$(mktemp)"
response_file="$(mktemp)"
trap 'rm -f "$curl_config" "$request_file" "$response_file"' EXIT

jq -n '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  facts: [{body: "The example repository uses a locked dependency set."}]
}' > "$request_file"

idempotency_key="$(python -c 'import uuid; print(uuid.uuid4())')"
curl --silent --show-error --fail-with-body \
  --request POST \
  --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $idempotency_key" \
  --data-binary "@$request_file" \
  --output "$response_file" \
  "$base_url/v1/ingest"
jq '{outcome, result, mutation_receipt, audit_receipt}' "$response_file"
```

Retain the idempotency key until the caller has durably accepted the response.
Retry an uncertain mutation with the same key and byte-equivalent request.
Never reuse that key for different intent: Cairn returns
`idempotency_conflict` rather than guessing.

### Retrieve and trust filters

Retrieval is available only when projection is enabled and current. The query
is opaque UTF-8; Cairn owns no query language. `budget` is a fact-body byte
budget from 1 to 1,048,576. Omitted or empty `trust_filters` means
`["validated"]`; candidate and failed-approach memory must be requested
explicitly. Duplicate filters are invalid.

```sh
jq -n '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  query: "locked dependency set",
  budget: 65536,
  trust_filters: ["candidate"]
}' > "$request_file"

curl --silent --show-error --fail-with-body \
  --request POST \
  --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --data-binary "@$request_file" \
  "$base_url/v1/retrieve" |
  jq '{hits, budget_consumed, budget_exhausted}'
```

An ingest acknowledgement proves custody, not immediate searchability. Treat
`index_pending`, `stale_index` and `dependency_unavailable` as delayed retries
and honour `Retry-After`. Do not add an idempotency key to retrieval.

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
`tools/call` and the client's `notifications/initialized`. The eleven tools
are named exactly:

```text
ingest  promote  invalidate  create-principal  issue-credential
revoke-credential  create-grant  revoke-grant  read-audit-events
retrieve  instance
```

For the eight mutations, `idempotency_key` is a tool argument, not an HTTP
header. The three reads reject it. Otherwise the arguments and result objects
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
