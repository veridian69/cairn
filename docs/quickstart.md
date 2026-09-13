# Local source quickstart

> **Disposable:** this script stops Cairn and deletes its temporary data and
> credential when it exits. For an instance that survives restart and reboot,
> use the [persistent native installation](operations/native-installation.md).

This procedure runs Cairn from source on numeric loopback with disposable test data. It makes no productive access and no model-provider call: retrieval and the optional evidence adapter remain disabled.

## Requirements

Complete the [disposable native prerequisites](install.md#disposable-native-quickstart-prerequisites)
before continuing. The path-specific summary is:

- Linux on a native Linux filesystem
- Python 3.12
- uv 0.12.0
- `curl` and `jq`

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
  enabled: false
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

cat >"$quickstart_dir/ingest.json" <<'JSON'
{
  "scope": {
    "realm": "local",
    "segments": [{"kind": "repository", "identifier": "example"}]
  },
  "classification": "internal",
  "source_type": "human",
  "requested_trust": "candidate",
  "facts": [{"body": "The example repository uses a locked dependency set."}]
}
JSON

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

The ingest response is a custody acknowledgement. The audit read proves that the corresponding mutation was recorded in the realm chain. Search retrieval is a separate, optional projection and is intentionally outside this local exercise.

## MCP endpoint

The same process exposes Cairn's stateless Streamable HTTP MCP endpoint at:

```text
http://127.0.0.1:8000/v1/mcp
```

MCP uses the same Cairn bearer credential and scope grants as REST. Its eleven tool schemas are published in [`contracts/cairn-mcp-tools-v1.json`](../contracts/cairn-mcp-tools-v1.json); see the [client guide](clients.md#mcp-clients) before configuring a client. Use TLS whenever the endpoint leaves numeric loopback.
