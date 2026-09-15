# Persistent Linux native installation

This procedure installs one persistent Cairn instance for one Linux user. It
runs on numeric loopback as a `systemd --user` service, survives ordinary
restart, and can start again when the user manager starts. It does not expose
Cairn to another machine.

**Prefer the guided installer.** The same persistent native shape, with or
without semantic search, is installed and verified by one command from the
checkout root; it resumes after a failure and can roll back or remove what it
created:

```sh
./cairn-install --non-interactive --mode native --name notes --port 8123
```

See the [quick install](../install.md#quick-install-with-the-guided-installer)
for prerequisites and the expected result, and the
[installer reference](guided-installation.md) for every flag. This page is the
manual procedure behind it, kept complete so the result can be reproduced and
understood step by step.

Use the [disposable quickstart](../quickstart.md) if you only want a
temporary demonstration; its manual script deliberately deletes its instance
on exit. Use the [Docker Compose procedure](../../deploy/compose/README.md) for a
complete local semantic-retrieval stack with FalkorDB. This native baseline
enables Attic evidence custody but leaves semantic retrieval disabled, so its
verification proves catalogue and evidence custody rather than search.

For macOS catalogue memory and Attic, use the [macOS foreground
guide](macos-foreground.md) for a temporary process or [macOS native
installation](macos-native.md) for the login-scoped LaunchAgent path. macOS
native acceptance passed on macOS 26 Intel and Apple Silicon; semantic
retrieval remains Linux-only.

RC4 semantic setup also requires loading the [maintained FalkorDB offline
archive](../../deploy/falkordb/README.md#install-the-maintained-offline-image) into
Docker's containerd image store before starting the index.

**Semantic search requires an OpenAI API key.** The native baseline and Attic
write/read check need no OpenAI key. Before enabling search, complete the
[optional semantic setup](#optional-semantic-retrieval), including its protected
OpenAI credential file. This key is separate from the Cairn administrator token.

## Requirements

Complete these checks before creating any files:

- A dedicated, non-root Linux user on an `x86_64` machine, with the checkout
  and home directory on a native Linux filesystem. This is the supported and
  acceptance-targeted native shape. WSL without systemd, macOS, Windows and
  other architectures are outside this procedure.
- Python 3.12–3.14 as `python3`, uv 0.12.14, Git, `curl`, `jq`, `sha256sum`,
  `systemctl`, `systemd-analyze` and `loginctl`. The host Python launches the
  installer; uv installs Cairn's managed Python 3.14 runtime. Go,
  Bubblewrap, Docker, `kubectl`, linters and test packages are developer or
  other-deployment tools and are not needed to run this native instance.
- A working `systemd --user` manager. `systemctl --user is-system-running`
  should print `running` or `degraded`. If it reports that no bus is available,
  ask the host administrator to enable user services; do not substitute a
  background shell process and assume reboot persistence.
- At least 1 GiB free before installation for the checkout, locked Python
  environment and a small new catalogue. Cairn data and backups grow with
  ingested content. Reserve separate backup space at least as large as the
  current catalogue and Attic files, plus the organisation's retention margin.
  No production CPU, memory or throughput sizing floor has been established
  for this single-user path.
- Network access to the checkout's configured Python package indexes for the
  first `uv sync`, unless every locked artefact is already cached. Once
  installed, this retrieval-disabled loopback service needs no model-provider
  or FalkorDB network access.
- Permission to create owner-controlled paths below `$HOME/.config`,
  `$HOME/.local/share` and `$HOME/.config/systemd/user`. No root command is
  needed for installation or normal operation. Only the optional boot-before-
  login setting may require administrator or polkit authority.

Optional semantic retrieval adds Docker Engine 25.0 or newer, the Docker
Compose v2 plugin 2.20.2 or newer, curl 8.4.0 or newer for bounded verification,
`openssl`, access to the Docker daemon, and
outbound HTTPS access to the configured OpenAI provider. Docker access is
effectively root-equivalent. The [Compose prerequisite checks](../../deploy/compose/README.md#compose-prerequisites-and-local-image)
give the supported versions and administrator boundary. The native baseline
does not require any of them. Provider calls can incur charges.

Resolve missing commands through the installation guide's
[prerequisite router](../install.md#get-missing-prerequisites), then check the
exact runtime versions from the repository root:

```sh
test "$(uname -m)" = x86_64
python3 --version
uv --version
test "$(uv --version | awk '{print $2}')" = 0.12.14
for tool in bash git curl jq sha256sum systemctl systemd-analyze loginctl journalctl \
  awk grep df id stat install cp mv tar mktemp chmod tr cat date sleep; do
  command -v "$tool" || exit 1
done
systemctl --user is-system-running | grep -Eq '^(running|degraded)$'
test "$(df -Pk "$HOME" | awk 'NR == 2 {print $4}')" -ge 1048576
```

Expected: Python reports `3.12.x`, `3.13.x` or `3.14.x`, uv reports `0.12.14`, and every command exits
zero. Install a missing prerequisite using the host's normal package policy,
then repeat the checks. Do not continue after a failed check.

## Install the locked runtime and persistent directories

Keep the checkout until installation completes. The installed service does not
run from it: uv creates an owner-controlled, non-editable environment at the
stable runtime path below.

From the repository root, run:

```sh
set -eu
umask 077

config_dir="$HOME/.config/cairn"
state_dir="$HOME/.local/share/cairn"
runtime_dir="$state_dir/runtime"
data_dir="$state_dir/data"
credential_dir="$state_dir/credentials"
backup_dir="$state_dir/backups"
unit_dir="$HOME/.config/systemd/user"
config_file="$config_dir/config.yaml"
credential_file="$credential_dir/local-operator.token"

test -f pyproject.toml
test -f uv.lock
install -d -m 0700 \
  "$config_dir" "$state_dir" "$data_dir" "$credential_dir" "$backup_dir"
install -d -m 0755 "$unit_dir"

UV_PROJECT_ENVIRONMENT="$runtime_dir" \
  uv sync --locked --no-dev --no-editable
test -x "$runtime_dir/bin/cairn"
```

`runtime`, `data`, `credentials` and `backups` have different jobs. The runtime
can be recreated from the same reviewed source and lock. `data` is authoritative
catalogue and Attic state. `credentials` holds local secret material. `backups`
is only a staging area; copy completed bundles to a separate protected backup
sink.

## Create the identity and strict configuration

The instance UUID is permanent. Restarts, upgrades and restores reuse it. The
following block refuses to overwrite an existing configuration, which prevents
accidentally assigning a new identity to existing data:

```sh
set -eu
umask 077

config_file="$HOME/.config/cairn/config.yaml"
data_dir="$HOME/.local/share/cairn/data"
credential_dir="$HOME/.local/share/cairn/credentials"
runtime_dir="$HOME/.local/share/cairn/runtime"

if [ -e "$config_file" ]; then
  printf 'Refusing to replace existing configuration: %s\n' "$config_file" >&2
  exit 1
fi

instance_id="$("$runtime_dir/bin/python" -c 'import uuid; print(uuid.uuid4())')"
cat >"$config_file" <<EOF
schema_version: cairn.config/v1
instance_id: $instance_id
mode: production
http:
  host: 127.0.0.1
  port: 8000
paths:
  data: $data_dir
  credentials: $credential_dir
attic:
  enabled: true
graphiti:
  enabled: false
EOF
chmod 0600 "$config_file"

"$runtime_dir/bin/cairn" check-config --config "$config_file" |
  jq -e --arg instance_id "$instance_id" \
    '.status == "ok" and .instance_id == $instance_id and .mode == "production"'
"$runtime_dir/bin/cairn" migrate --config "$config_file" |
  jq -e --arg instance_id "$instance_id" \
    '.status == "ok" and .operation == "migrate" and .instance_id == $instance_id'
```

The YAML schema is strict: quoted numbers, relative paths, duplicate keys and
unknown fields are refused. The configured data and credential paths are
absolute and stay unchanged for the life of this installation.

## Bootstrap exactly once

Bootstrap creates realm `local`, its first human principal and grants, and one
plaintext bearer token. The token is printed only once. Keep the service
stopped while running this local command.

First inspect the migrated catalogue:

```sh
runtime_dir="$HOME/.local/share/cairn/runtime"
config_file="$HOME/.config/cairn/config.yaml"

verify_json="$("$runtime_dir/bin/cairn" verify --config "$config_file")"
printf '%s\n' "$verify_json" | jq .
printf '%s\n' "$verify_json" |
  jq -e '.status == "ok" and .realm_count == 0' >/dev/null
```

Continue only when `realm_count` is `0`. A non-zero count means this catalogue
has already been bootstrapped; use its retained credential and skip the next
block. Running bootstrap again returns `{"status":"error","code":"realm_exists"}`
and must not become a normal restart step.

Capture the one-time result outside Git in the owner-only credential directory:

```sh
set -eu
umask 077

runtime_dir="$HOME/.local/share/cairn/runtime"
config_file="$HOME/.config/cairn/config.yaml"
credential_dir="$HOME/.local/share/cairn/credentials"
credential_file="$credential_dir/local-operator.token"
bootstrap_result="$credential_dir/bootstrap-once.json"
credential_staging="$credential_dir/local-operator.token.new"

test ! -e "$credential_file"
test ! -e "$bootstrap_result"
test ! -e "$credential_staging"

"$runtime_dir/bin/cairn" bootstrap --config "$config_file" \
  --realm local --label local-operator >"$bootstrap_result"
chmod 0600 "$bootstrap_result"
jq -er \
  'select(.status == "ok" and .operation == "bootstrap" and .realm_id == "local") | .token | strings | select(startswith("cairn1."))' \
  "$bootstrap_result" >"$credential_staging"
chmod 0600 "$credential_staging"
mv -- "$credential_staging" "$credential_file"
rm -f -- "$bootstrap_result"
```

If token extraction fails after bootstrap succeeds, stop. The owner-only
`bootstrap-once.json` is then the sole recovery copy; inspect and extract it
without rerunning bootstrap. Never print, commit, log, email or place the token
in an environment variable or command argument. Back it up separately in an
approved encrypted secret store.

## Install and start the user service

Create the unit with no credential value in it:

```sh
set -eu
umask 077

unit_file="$HOME/.config/systemd/user/cairn.service"
cat >"$unit_file" <<'EOF'
[Unit]
Description=Cairn persistent loopback service
After=network.target

[Service]
Type=simple
ExecStart="%h/.local/share/cairn/runtime/bin/cairn" serve --config "%h/.config/cairn/config.yaml"
Restart=on-failure
RestartSec=5s
TimeoutStopSec=60s
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
chmod 0644 "$unit_file"

systemd-analyze --user verify "$unit_file"
systemctl --user daemon-reload
systemctl --user enable --now cairn.service
systemctl --user is-enabled cairn.service
systemctl --user is-active cairn.service
attempt=0
until curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 2 \
  http://127.0.0.1:8000/health/ready |
  jq -e '.status == "ready"' >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    journalctl --user -u cairn.service -n 100 --no-pager >&2
    exit 1
  fi
  sleep 1
done
```

Expected: the two `systemctl` checks print `enabled` and `active`; readiness
returns `{"status":"ready"}`. If the service fails, inspect safe service output
with `journalctl --user -u cairn.service -n 100 --no-pager`. Do not enable shell
tracing or copy a bearer token into the journal.

Verify the retained credential and instance UUID using an owner-only temporary
curl configuration:

```sh
set -eu
umask 077

credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
config_file="$HOME/.config/cairn/config.yaml"
curl_config="$(mktemp)"
trap 'rm -f "$curl_config"' EXIT HUP INT TERM
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' <"$credential_file"
  printf '"\n'
} >"$curl_config"
chmod 0600 "$curl_config"

expected_instance="$(awk '$1 == "instance_id:" {print $2}' "$config_file")"
curl --disable --silent --show-error --fail-with-body \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
  --config "$curl_config" http://127.0.0.1:8000/v1/instance |
  jq -e --arg expected "$expected_instance" \
    '.contract_identity == "cairn/v1" and .instance_id == $expected'
```

Set the native endpoint and retained credential in the same shell before
continuing with the [client guide](../clients.md):

```sh
base_url=http://127.0.0.1:8000
credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
```

Use those values to pin contract digests and run scoped ingest and audit
checks. With Graphiti disabled, do not treat an absent semantic result as a
failed custody check or claim this procedure verified search.

## Quick Attic write/read test

From the repository root, reuse the endpoint, retained credential and protected
`curl_config` already created above in this shell. Do not create a second curl
configuration. Python 3.12–3.14 as `python3`, curl 8.4.0 or newer
and jq are required. This small check needs no semantic provider. The bootstrap
administrator can ingest and read this synthetic evidence at
`local/repository:example`.

```bash
set -eu
umask 077
install -d -m 0700 "$HOME/.local/state/cairn-checks"
evidence_check_dir="$(mktemp -d "$HOME/.local/state/cairn-checks/evidence.XXXXXXXX")"
printf 'Retained evidence verification files: %s\n' "$evidence_check_dir"
printf 'Cairn Attic check: café.\nExact second line.\n' > "$evidence_check_dir/payload.txt"
python3 -c 'import uuid; print(uuid.uuid4())' > "$evidence_check_dir/idempotency-key"
jq -n --rawfile payload "$evidence_check_dir/payload.txt" '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  facts: [{body: "The installation has submitted a synthetic Attic payload."}],
  evidence_payload: $payload
}' > "$evidence_check_dir/ingest.json"

if http_status="$(curl --disable --silent --show-error --noproxy '*' \
  --max-time 30 --request POST --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $(cat "$evidence_check_dir/idempotency-key")" \
  --data-binary "@$evidence_check_dir/ingest.json" \
  --output "$evidence_check_dir/ingest-response.json" \
  --dump-header "$evidence_check_dir/ingest-headers" --write-out '%{http_code}' \
  "$base_url/v1/ingest")"; then
  printf 'Ingest HTTP %s\n' "$http_status"
  cat "$evidence_check_dir/ingest-response.json"
  printf '\n'
else
  printf 'Commit uncertain; retain the same request and key in %s.\n' "$evidence_check_dir" >&2
  exit 1
fi
test "$http_status" = 200
jq -e '
  (.outcome == "committed" or .outcome == "replayed") and
  (.mutation_receipt | type == "object") and
  (.audit_receipt | type == "object") and
  (.result.evidence_id | type == "string")
' "$evidence_check_dir/ingest-response.json" >/dev/null
jq -er '.result.evidence_id' "$evidence_check_dir/ingest-response.json" > "$evidence_check_dir/evidence-id"

python3 scripts/verify-retrieval.py \
  --base-url "$base_url" \
  --credential-file "$credential_file" \
  --evidence-id "$(cat "$evidence_check_dir/evidence-id")" \
  --payload-file "$evidence_check_dir/payload.txt" \
  --deadline 120 --request-timeout 10 --max-attempts 30
```

Expect `"status": "verified"`: the exact saved UTF-8 bytes, SHA-256 and byte
length have returned through Cairn. The helper honours `Retry-After` and retries
only reads, stopping after 120 seconds or 30 attempts. Corruption and mismatched
bytes fail the check. Retain the printed directory and saved evidence ID.

Restart with `systemctl --user restart cairn.service`, wait for the readiness
check above, and repeat **only** the final evidence-read command. Do not repeat
a committed ingest. This checks custody and retention without semantic search;
the synthetic payload remains in the persistent data directory. See the
[Attic round-trip documentation](evidence-verification.md) for uncertain-write
recovery, failure details and what a successful check proves.

## Optional semantic retrieval

BI-2 acceptance includes saving a synthetic fact, restarting the instance and
retrieving that exact fact. That check requires Graphiti. This supported native
extension keeps Cairn under the user service above and runs only its per-instance
FalkorDB index in the same pinned container used by the Compose procedure.
FalkorDB publishes only to `127.0.0.1:16379`; it is not reachable from another
machine. Attic remains enabled.

Complete the optional prerequisites above. Keep working from the repository
root so `deploy/images.lock` and the bounded verification helper refer to the
same reviewed revision as the installed runtime. Stop Cairn, generate separate
owner-only provider and index credentials, and retain both in the established
credential directory:

```bash
set -eu
umask 077

runtime_dir="$HOME/.local/share/cairn/runtime"
config_file="$HOME/.config/cairn/config.yaml"
credential_dir="$HOME/.local/share/cairn/credentials"
index_password_file="$credential_dir/falkordb-password"
provider_key_file="$credential_dir/openai-api-key"

test -f deploy/images.lock
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker compose version
curl --version
command -v openssl >/dev/null
systemctl --user stop cairn.service
! systemctl --user is-active --quiet cairn.service

for path in "$index_password_file" "$provider_key_file"; do
  if [ -e "$path" ]; then
    printf 'Refusing to overwrite existing credential: %s\n' "$path" >&2
    exit 1
  fi
done
openssl rand -hex 32 >"$index_password_file"
chmod 0600 "$index_password_file"
IFS= read -r -s -p 'OpenAI API key: ' provider_key
printf '\n'
test -n "$provider_key"
printf '%s\n' "$provider_key" >"$provider_key_file"
unset provider_key
chmod 0600 "$provider_key_file"
```

The provider key enters neither shell history nor a command argument. Cairn
reads both convention-named files and places the provider key only in its own
process environment because the pinned Graphiti library requires it there.
Do not enable shell tracing around these commands.

Create the stable, per-user index volumes and an authenticated FalkorDB
configuration. The one-shot container is the only root container; it can reach
no network and only gives the two volumes to FalkorDB's numeric identity:

```sh
credential_dir="$HOME/.local/share/cairn/credentials"
index_password_file="$credential_dir/falkordb-password"

set -eu
umask 077

host_uid="$(id -u)"
case "$host_uid" in *[!0-9]*|'') exit 1 ;; esac
index_name="cairn-native-$host_uid-falkordb"
index_data_volume="cairn-native-$host_uid-falkordb-data"
index_config_volume="cairn-native-$host_uid-falkordb-config"
falkordb_image="$(awk -F= '$1 == "FALKORDB_IMAGE" {print $2}' deploy/images.lock)"
case "$falkordb_image" in *@sha256:*) ;; *) exit 1 ;; esac

if docker container inspect "$index_name" >/dev/null 2>&1; then
  printf 'Refusing to replace existing index container: %s\n' "$index_name" >&2
  exit 1
fi
docker volume create "$index_data_volume" >/dev/null
docker volume create "$index_config_volume" >/dev/null

docker run --rm \
  --interactive \
  --name "$index_name-init" \
  --network none \
  --user 0:0 \
  --read-only \
  --cap-drop ALL \
  --cap-add CHOWN \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=$index_config_volume,target=/config" \
  --mount "type=volume,source=$index_data_volume,target=/var/lib/falkordb/data" \
  --entrypoint sh \
  "$falkordb_image" -c '
    set -eu
    umask 077
    { printf "requirepass "; tr -d "\r\n"; printf "\n"; } >/config/cairn.conf.new
    chmod 0400 /config/cairn.conf.new
    chown 10001:0 /config/cairn.conf.new /var/lib/falkordb/data
    mv /config/cairn.conf.new /config/cairn.conf
  ' <"$index_password_file"
```

The host shell opens the owner-only password file and supplies its bytes on the
one-shot container's standard input. The password never appears in an
environment variable, bind mount or argument. Start FalkorDB with the same
security and server settings as the canonical Compose overlay:

```sh
host_uid="$(id -u)"
case "$host_uid" in *[!0-9]*|'') exit 1 ;; esac
index_name="cairn-native-$host_uid-falkordb"
index_data_volume="cairn-native-$host_uid-falkordb-data"
index_config_volume="cairn-native-$host_uid-falkordb-config"
falkordb_image="$(awk -F= '$1 == "FALKORDB_IMAGE" {print $2}' deploy/images.lock)"
case "$falkordb_image" in *@sha256:*) ;; *) exit 1 ;; esac

docker run -d \
  --name "$index_name" \
  --hostname falkordb \
  --restart unless-stopped \
  --stop-timeout 30 \
  --user 10001:0 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --publish 127.0.0.1:16379:6379 \
  --env REDIS_ARGS=/etc/falkordb/cairn.conf \
  --env BROWSER=0 \
  --env TLS=0 \
  --env 'FALKORDB_ARGS=MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000' \
  --mount "type=volume,source=$index_config_volume,target=/etc/falkordb,readonly" \
  --mount "type=volume,source=$index_data_volume,target=/var/lib/falkordb/data" \
  "$falkordb_image" >/dev/null

attempt=0
until docker exec "$index_name" sh -c \
  '{ sed -n "s/^requirepass /AUTH /p" /etc/falkordb/cairn.conf; echo PING; } | redis-cli -h 127.0.0.1 -p 6379' |
  grep -q '^PONG$'
do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    docker logs --tail 100 "$index_name" >&2
    exit 1
  fi
  sleep 2
done
docker port "$index_name" 6379/tcp |
  grep -Fx '127.0.0.1:16379'
```

The health loop checks the authenticated `PONG`, not merely `redis-cli`'s exit
status. A `NOAUTH` response also exits zero and is not healthy.

Enable Graphiti by changing the one expected disabled block. The edit refuses
an unexpected or already-enabled configuration rather than rewriting arbitrary
YAML:

```sh
runtime_dir="$HOME/.local/share/cairn/runtime"
config_file="$HOME/.config/cairn/config.yaml"

"$runtime_dir/bin/python" - "$config_file" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
old = "graphiti:\n  enabled: false\n"
new = "graphiti:\n  enabled: true\n  host: 127.0.0.1\n  port: 16379\n"
text = path.read_text(encoding="utf-8")
if text.count(old) != 1:
    raise SystemExit("configuration has no single disabled Graphiti block")
path.write_text(text.replace(old, new), encoding="utf-8")
PY
chmod 0600 "$config_file"
"$runtime_dir/bin/cairn" check-config --config "$config_file" |
  jq -e '.status == "ok" and .mode == "production"'
systemctl --user start cairn.service
attempt=0
until curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 2 \
  http://127.0.0.1:8000/health/ready |
  jq -e '.status == "ready"' >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  test "$attempt" -lt 60
  sleep 1
done
```

Set the native values in the same shell, then run the client guide's
[bounded ingest and retrieval verification](../clients.md#bounded-ingest-and-retrieval-verification).
Keep its committed `fact_id`. Then prove restart persistence from the repository
root without repeating ingest:

```sh
base_url=http://127.0.0.1:8000
credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
```

```sh
fact_id=00000000-0000-4000-8000-000000000000
test "$fact_id" != 00000000-0000-4000-8000-000000000000
systemctl --user restart cairn.service
attempt=0
until curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 2 \
  http://127.0.0.1:8000/health/ready |
  jq -e '.status == "ready"' >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  test "$attempt" -lt 60
  sleep 1
done

credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
config_file="$HOME/.config/cairn/config.yaml"
expected_instance="$(awk '$1 == "instance_id:" {print $2}' "$config_file")"
curl_config="$(mktemp)"
trap 'rm -f "$curl_config"' EXIT HUP INT TERM
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' <"$credential_file"
  printf '"\n'
} >"$curl_config"
chmod 0600 "$curl_config"
curl --disable --silent --show-error --fail-with-body \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
  --config "$curl_config" http://127.0.0.1:8000/v1/instance |
  jq -e --arg expected "$expected_instance" '.instance_id == $expected'

"$HOME/.local/share/cairn/runtime/bin/python" scripts/verify-retrieval.py \
  --base-url http://127.0.0.1:8000 \
  --credential-file "$HOME/.local/share/cairn/credentials/local-operator.token" \
  --fact-id "$fact_id" \
  --deadline 120 \
  --request-timeout 10
```

Replace `fact_id` with the UUID returned by the successful ingest. The helper
performs reads only, uses a finite deadline and request timeout, and succeeds
only when the exact fact ID and body return. This proves that the same instance,
catalogue, credential and semantic index survive a Cairn restart. It does not
prove general recall quality, every fact, backup recovery or production
readiness.

The index container uses `restart: unless-stopped`, but its reboot behaviour
also depends on Docker starting at boot. Check `systemctl is-enabled docker`;
an administrator must enable the daemon if it is disabled. Normal native Cairn
stop/start/restart commands do not stop or delete the index. Inspect it with:

```sh
host_uid="$(id -u)"
index_name="cairn-native-$host_uid-falkordb"
docker ps --filter "name=^/${index_name}$"
docker logs --tail 100 "$index_name"
```

Never run `docker volume rm` as a restart operation.

## Operate the service

These are the complete normal lifecycle commands:

```sh
systemctl --user start cairn.service
systemctl --user stop cairn.service
systemctl --user restart cairn.service
systemctl --user status cairn.service --no-pager
```

`start` returns the existing instance to service. `stop` leaves configuration,
identity, catalogue, Attic data and credential untouched. `restart` performs a
stop followed by a start and does not bootstrap again. Status should show
`Loaded: ... enabled` and `Active: active (running)` while serving.

After a restart, repeat the readiness and authenticated instance checks above.
The UUID must be unchanged and the same credential must still authenticate.
Use `systemctl --user disable --now cairn.service` to stop Cairn and prevent it
starting with the user manager; this preserves every file.

## Login and reboot behaviour

`systemctl --user enable` attaches Cairn to the user's default target. Without
lingering, the user manager normally starts at login, so Cairn starts at login
rather than necessarily at machine boot and may stop after the last session.

To start the user manager at boot and keep it after logout, inspect lingering:

```sh
loginctl show-user "$USER" -p Linger
```

If it prints `Linger=no` and boot-before-login is required, run:

```sh
loginctl enable-linger "$USER"
```

The host may require `sudo loginctl enable-linger "$USER"` or an administrator
to run it. This is the only potentially privileged step in this procedure.
After the next reboot, check `systemctl --user is-active cairn.service` and the
readiness endpoint. Disabling Cairn's automatic start only requires
`systemctl --user disable cairn.service`; do not disable lingering merely to
stop Cairn, because lingering applies to every user service owned by the user.

## Backup and recovery

Use the [native backup and replacement-restore commands](backup-restore.md#native-systemd-user-service).
They preserve the immutable instance UUID, keep the original data directory as
the rollback point, retain the plaintext credential separately, and explain
which retrieval state would need rebuilding. A copied SQLite file is not a
Cairn backup.

## Upgrade the installed runtime

From the reviewed replacement checkout, verify its revision and lock, then run:

```sh
systemctl --user stop cairn.service
UV_PROJECT_ENVIRONMENT="$HOME/.local/share/cairn/runtime" \
  uv sync --locked --no-dev --no-editable
"$HOME/.local/share/cairn/runtime/bin/cairn" migrate \
  --config "$HOME/.config/cairn/config.yaml"
systemctl --user start cairn.service
attempt=0
until curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 2 \
  http://127.0.0.1:8000/health/ready |
  jq -e '.status == "ready"' >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  test "$attempt" -lt 60
  sleep 1
done
```

Do not run an older runtime against a newer catalogue. Take and export a Cairn
backup before upgrading; rollback means restoring a known compatible bundle or
returning to a retained original, not downgrading the catalogue in place.
