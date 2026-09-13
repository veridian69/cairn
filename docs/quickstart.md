# Local source quickstart

> **Disposable:** this script stops Cairn and deletes its temporary data and
> credential when it exits. For an instance that survives restart and reboot,
> use the [persistent native installation](operations/native-installation.md).

This procedure runs Cairn from source on numeric loopback with disposable test data. It makes no productive access and no model-provider call: semantic retrieval remains disabled. Attic is enabled so the script also checks an exact evidence write/read round-trip.

**Semantic search requires an OpenAI API key.** This disposable quickstart
leaves semantic search disabled, so its Attic write/read check needs no OpenAI
key. For semantic search, use the [persistent native setup](operations/native-installation.md#optional-semantic-retrieval)
or [Docker Compose setup](../deploy/compose/README.md#optional-semantic-retrieval).
The OpenAI key is separate from the Cairn credential created below.

## Requirements

Complete the [disposable native prerequisites](install.md#disposable-native-quickstart-prerequisites)
before continuing. The path-specific summary is:

- Linux on a native Linux filesystem
- Python 3.12
- uv 0.12.0
- curl 8.4.0 or newer and `jq`

From the repository root, install the exact locked environment:

```sh
uv sync --locked --no-dev
```

Run the following block from that same directory. It creates the data, configuration, request bodies and owner-only credential under a new `/tmp` directory, then removes them when it exits.

```bash
set -eu
umask 077

quickstart_dir="$(mktemp -d /tmp/cairn-quickstart.XXXXXX)"
server_pid=
cleanup() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf -- "$quickstart_dir"
}
trap cleanup EXIT HUP INT TERM

install -d -m 0700 \
  "$quickstart_dir/data" \
  "$quickstart_dir/credentials"
instance_id="$(uv run --locked --no-dev python -c 'import uuid; print(uuid.uuid4())')"
config_file="$quickstart_dir/config.yaml"
cat >"$config_file" <<EOF
schema_version: cairn.config/v1
instance_id: $instance_id
mode: test
http:
  host: 127.0.0.1
  port: 8000
paths:
  data: $quickstart_dir/data
  credentials: $quickstart_dir/credentials
attic:
  enabled: true
graphiti:
  enabled: false
EOF

uv run --locked --no-dev cairn check-config --config "$config_file"
uv run --locked --no-dev cairn migrate --config "$config_file"

# Bootstrap is local-only. Capture its one-time plaintext token in an
# owner-only file outside the checkout, then remove the full result.
bootstrap_result="$quickstart_dir/bootstrap.json"
credential_file="$quickstart_dir/cairn-credential"
uv run --locked --no-dev cairn bootstrap --config "$config_file" \
  --realm local --label quickstart >"$bootstrap_result"
jq -er \
  'select(.status == "ok" and .operation == "bootstrap") | .token | strings | select(startswith("cairn1."))' \
  "$bootstrap_result" >"$credential_file"
chmod 0600 "$credential_file"
rm -f -- "$bootstrap_result"

uv run --locked --no-dev cairn serve --config "$config_file" \
  >"$quickstart_dir/server.log" 2>&1 &
server_pid=$!

attempt=0
until curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 2 \
  http://127.0.0.1:8000/health/ready >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    cat "$quickstart_dir/server.log" >&2
    exit 1
  fi
  sleep 0.1
done

# Keep the bearer value out of curl's arguments and process listing.
curl_config="$quickstart_dir/curl.conf"
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' <"$credential_file"
  printf '"\n'
} >"$curl_config"
chmod 0600 "$curl_config"

printf 'Cairn Attic check: café.\nExact second line.\n' > "$quickstart_dir/payload.txt"
jq -n --rawfile payload "$quickstart_dir/payload.txt" '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  requested_trust: "candidate",
  facts: [{body: "The example repository uses a locked dependency set."}],
  evidence_payload: $payload
}' > "$quickstart_dir/ingest.json"

idempotency_key="$(uv run --locked --no-dev python -c 'import uuid; print(uuid.uuid4())')"
curl --disable --silent --show-error --fail-with-body \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
  --request POST \
  --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $idempotency_key" \
  --data-binary "@$quickstart_dir/ingest.json" \
  --output "$quickstart_dir/ingest-response.json" \
  http://127.0.0.1:8000/v1/ingest

mutation_id="$(jq -er '.mutation_receipt.mutation_id' "$quickstart_dir/ingest-response.json")"
jq '{outcome, result, mutation_receipt, audit_receipt}' \
  "$quickstart_dir/ingest-response.json"

# Read the committed evidence; the helper compares exact UTF-8 bytes,
# SHA-256 and byte length, retrying only the read while delivery is pending.
evidence_id="$(jq -er '.result.evidence_id | strings' "$quickstart_dir/ingest-response.json")"
uv run --locked --no-dev python scripts/verify-retrieval.py \
  --base-url http://127.0.0.1:8000 \
  --credential-file "$credential_file" \
  --evidence-id "$evidence_id" \
  --payload-file "$quickstart_dir/payload.txt" \
  --deadline 120 --request-timeout 10 --max-attempts 30

cat >"$quickstart_dir/audit-request.json" <<'JSON'
{
  "realm_id": "local",
  "scope_prefix": [],
  "after_sequence": 0,
  "limit": 100
}
JSON

curl --disable --silent --show-error --fail-with-body \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
  --request POST \
  --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --data-binary "@$quickstart_dir/audit-request.json" \
  http://127.0.0.1:8000/v1/read-audit-events |
  jq --arg mutation_id "$mutation_id" \
    '.events[] | select(.mutation_id == $mutation_id and .action_code == "ingest") | {sequence, action_code, outcome, mutation_id}'
```

The ingest response acknowledges custody. The evidence check must print
`"status": "verified"`: the exact saved payload and its SHA-256 have returned
through the authenticated API. It honours `Retry-After` for pending delivery or
a retryable dependency failure, stops after 120 seconds or 30 attempts, and
fails on corruption or mismatched bytes. It never repeats ingest. The audit
read also proves the original mutation was recorded in the realm chain.
Semantic search remains outside this exercise. All synthetic data is deleted
on exit; use a persistent installation to verify retention across restart.
See the [Attic round-trip documentation](operations/evidence-verification.md)
for the API, failure handling and persistent checks.

## MCP endpoint

The same process exposes Cairn's stateless Streamable HTTP MCP endpoint at:

```text
http://127.0.0.1:8000/v1/mcp
```

MCP uses the same Cairn bearer credential and scope grants as REST. Its tool schemas are published in [`contracts/cairn-mcp-tools-v1.json`](../contracts/cairn-mcp-tools-v1.json); see the [client guide](clients.md#mcp-clients) before configuring a client. Use TLS whenever the endpoint leaves numeric loopback.
