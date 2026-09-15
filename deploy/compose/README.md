# The Compose project

**Semantic search requires an OpenAI API key.** The base Compose installation
and Attic write/read check work without one. Follow the
[optional semantic setup](#optional-semantic-retrieval) to store the OpenAI key
in the protected credential file before enabling search. It is separate from
your Cairn administrator token.

**Prefer the guided installer.** From the checkout root, one command builds
the image, materialises a private Compose project, bootstraps, verifies and
leaves the instance running, with `--semantic` adding FalkorDB and OpenAI
search from a protected key file:

```sh
./cairn-install --non-interactive --mode docker --name notes-docker --port 8124
```

See the [quick install](../../docs/install.md#quick-install-with-the-guided-installer)
for prerequisites and the expected result, and the
[installer reference](../../docs/operations/guided-installation.md) for every
flag. This page is the manual Compose procedure behind it, kept complete for
operators who need to reproduce, upgrade, back up or accept the stack by hand.
The installer does not adopt a project created by this manual procedure.

One Cairn instance on one host, packaged as a Compose project.

**One project is one instance.** The project name is the instance name,
and Compose prefixes this project's network and both volumes with it, so
two instances on one host share nothing but the daemon.

Start here for the exact Compose commands. The broader
[deployment guide](../../docs/operations/deployment.md) explains when to
choose this target; the [client guide](../../docs/clients.md) covers REST and
MCP calls, grants, trust filters and safe bearer handling; the
[backup and restore runbook](../../docs/operations/backup-restore.md) owns the
recovery procedure.

## Image and contract boundary

`CAIRN_IMAGE=cairn:v0.5.0-rc.4` in `../images.lock` is a local build tag, not a
published image. Use the reviewed local build produced below or a separately
reviewed registry digest and record its exact identity.

The wire contracts are already pinned. The first-boot sequence verifies them
from the repository root before deploying.

Expected SHA-256 values:

| Contract | SHA-256 |
| --- | --- |
| REST/OpenAPI | `16a6d8b6f81182bc29ea3df0c3f68408a124a06ebc7bc33d03913f1e8cb8f045` |
| MCP tools | `691a3603c9368b8386b3546ea04a02a924aa8d4fe5bfab594cfa71a395f825fb` |

After first boot, compare those values and the expected instance UUID through
authenticated `GET /v1/instance` using the
[safe client procedure](../../docs/clients.md#check-the-instance). An image
reference alone does not prove which contract an endpoint serves.

## Compose prerequisites and local image

The [installation guide](../../docs/install.md#docker-compose-installation)
separates the prerequisites for this path from the native and developer paths.
This supported procedure uses a native **Linux x86-64** checkout and a
systemd-managed Linux Docker host. Other architectures have not passed the
published Compose acceptance baseline.

Required runtime and command-line tools are:

- Docker Engine 25.0 or newer. Engine 25.0 is required because this project
  uses health-check `start_interval`;
- the Docker Compose v2 plugin, version 2.20.2 or newer. Older Compose clients
  do not understand that field;
- Bash, `git`, `make`, `systemctl`, `sed`, and GNU coreutils including
  `sha256sum`, `install`, `realpath`, `stat`, `timeout` and `tr` for the local build,
  service check and safe file preparation; and
- curl 8.4.0 or newer, `jq` and Python 3.12–3.14 as `python3` for bootstrap and the bounded client
  checks. The helper uses curl's [`--max-filesize`](https://curl.se/docs/manpage.html#--max-filesize)
  transfer-time limit, which protects unknown-length responses from 8.4.0;
  `openssl` and privilege through `sudo` or an existing root session are
  required only when semantic retrieval is enabled.

Check them from the repository root before creating any instance files:

```bash
uname -s
uname -m
for tool in bash docker git make systemctl sed sha256sum install realpath stat \
    timeout tr curl jq python3; do
  command -v "$tool" || exit 1
done
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker compose version --short
docker info --format 'daemon={{.OSType}}/{{.Architecture}}'
systemctl is-enabled docker
curl --version
```

For RC4 semantic retrieval, first load the [maintained FalkorDB offline
archive](../falkordb/README.md#install-the-rc4-offline-image) on this Docker host.
The loader requires Docker's containerd image store; GHCR publication is pending.

If you plan to enable semantic retrieval, run its additional checks before
creating the instance:

```bash
command -v openssl || exit 1
(
  cd deploy/compose || exit 1
if (( EUID != 0 )); then
  command -v sudo || exit 1
  sudo -v || exit 1
  sudo -l -- chown 65532:0 credentials/falkordb-password credentials/openai-api-key >/dev/null || exit 1
  sudo -l -- chmod 0400 credentials/falkordb-password credentials/openai-api-key >/dev/null || exit 1
  sudo -l -- chown 10001:0 falkordb.conf >/dev/null || exit 1
  sudo -l -- chmod 0400 falkordb.conf >/dev/null || exit 1
fi
) || exit 1
```

The expected results are `Linux`, `x86_64`, Engine client and server versions
of at least 25.0, Compose v2.20.2 or newer, `daemon=linux/x86_64`, `enabled`,
curl 8.4.0 or newer, and one absolute executable path per required tool. The semantic checks must
also print the OpenSSL path and, for a non-root operator, the `sudo` path.
`sudo -v` must authenticate successfully, and each `sudo -l -- COMMAND` must
confirm permission to run that exact ownership or mode command as root. Merely
having sudo installed is insufficient. A refusal means stop and ask the host
administrator to arrange the required access; do not stop Cairn or create
credentials first. These checks do not change file ownership. Cairn runs on
Python 3.14 inside its image; the host helper uses only the standard library
and does not require replacing the system Python. If a
command is absent or below the
minimum version, follow the installation guide's platform instructions, then
repeat the complete checklist. If `docker version` or `docker info` reports a
permission error, stop here: access to the Docker socket is effectively
root-equivalent. Have the host administrator grant the trusted operator access
under local policy, sign in again if group membership changed, and repeat the
checks. Do not alternate between privileged and unprivileged Docker commands.
If automatic start after reboot is wanted and the systemd check says
`disabled`, ask an administrator to run `systemctl enable --now docker` (or run
it through the host's approved privilege mechanism).

Go, `uv`, Bubblewrap and `kubectl` belong to developer or other deployment
paths; they are not required to build and run this Compose instance. From the
repository root, verify the contracts, build the image named by
`deploy/images.lock`, and confirm that it exists:

```sh
(cd contracts && sha256sum -c cairn-openapi-v1.json.sha256)
(cd contracts && sha256sum -c cairn-mcp-tools-v1.json.sha256)
make image IMAGE=cairn:v0.5.0-rc.4
docker image inspect --format '{{.Id}}' cairn:v0.5.0-rc.4
```

Both checksum commands must report `OK`; the build must finish successfully;
and the inspect command must print an image ID.

## Container-to-container connectivity preflight

Run this disposable check from the repository root before creating `.env`,
configuration or credentials. It starts no Cairn or FalkorDB process. Instead,
two containers use the Python runtime in the image just built: one serves a
fixed HTTP body and the other checks Docker DNS, TCP and that body over a new
user-defined bridge. No port is published and neither container makes an
external request.

The Compose project uses an ordinary user-defined bridge, so the probe verifies
`bridge false`; adding `--internal` here would test a different network shape.
Every temporary object has a random name and matching ownership label. Cleanup
removes an object only when both still match.

```bash
set -eu
set -o pipefail

preflight_image='cairn:v0.5.0-rc.4'
preflight_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
preflight_label_key='io.cairn.compose-preflight'
preflight_network="cairn-preflight-$preflight_id"
preflight_server="cairn-preflight-server-$preflight_id"
preflight_client="cairn-preflight-client-$preflight_id"

cleanup_preflight() {
  local cleanup_status=0 name owner owned_containers owned_networks
  for name in "$preflight_client" "$preflight_server"; do
    if owner="$(timeout 5 docker container inspect --format \
        '{{ index .Config.Labels "io.cairn.compose-preflight" }}' \
        "$name" 2>/dev/null)"; then
      if test "$owner" = "$preflight_id"; then
        if ! timeout 10 docker container rm --force "$name" >/dev/null 2>&1; then
          printf 'CLEANUP_FAILED: could not remove owned container: %s\n' \
            "$name" >&2
          cleanup_status=1
        fi
      else
        printf 'not removing unowned container: %s\n' "$name" >&2
        cleanup_status=1
      fi
    fi
  done
  if owner="$(timeout 5 docker network inspect --format \
      '{{ index .Labels "io.cairn.compose-preflight" }}' \
      "$preflight_network" 2>/dev/null)"; then
    if test "$owner" = "$preflight_id"; then
      if ! timeout 10 docker network rm "$preflight_network" >/dev/null 2>&1; then
        printf 'CLEANUP_FAILED: could not remove owned network: %s\n' \
          "$preflight_network" >&2
        cleanup_status=1
      fi
    else
      printf 'not removing unowned network: %s\n' "$preflight_network" >&2
      cleanup_status=1
    fi
  fi
  if ! owned_containers="$(timeout 5 docker container ls --all --quiet \
      --filter "label=$preflight_label_key=$preflight_id")"; then
    printf 'CLEANUP_UNVERIFIED: could not inventory labelled containers\n' >&2
    cleanup_status=1
  elif test -n "$owned_containers"; then
    printf 'CLEANUP_RESIDUE: labelled container IDs: %s\n' \
      "$owned_containers" >&2
    cleanup_status=1
  fi
  if ! owned_networks="$(timeout 5 docker network ls --quiet \
      --filter "label=$preflight_label_key=$preflight_id")"; then
    printf 'CLEANUP_UNVERIFIED: could not inventory labelled networks\n' >&2
    cleanup_status=1
  elif test -n "$owned_networks"; then
    printf 'CLEANUP_RESIDUE: labelled network IDs: %s\n' \
      "$owned_networks" >&2
    cleanup_status=1
  fi
  return "$cleanup_status"
}
finish_preflight() {
  local status=$? cleanup_status
  trap - EXIT
  set +e
  cleanup_preflight
  cleanup_status=$?
  if test "$cleanup_status" != 0; then
    printf 'preflight cleanup incomplete; remove only the reported labelled resources\n' >&2
    if test "$status" = 0; then status=1; fi
  fi
  exit "$status"
}
trap finish_preflight EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker image inspect "$preflight_image" >/dev/null
docker network create --driver bridge \
  --label "$preflight_label_key=$preflight_id" \
  "$preflight_network" >/dev/null
test "$(docker network inspect --format '{{.Driver}} {{.Internal}}' \
  "$preflight_network")" = 'bridge false'

server_code="$(cat <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b"cairn-compose-bridge-ok\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(BODY)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(BODY)


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
PY
)"
docker run --detach --name "$preflight_server" \
  --label "$preflight_label_key=$preflight_id" \
  --network "$preflight_network" --network-alias compose-http \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python "$preflight_image" -u -c "$server_code" >/dev/null

server_ready=0
for ((attempt = 1; attempt <= 10; attempt++)); do
  if timeout 3 docker exec "$preflight_server" python -c \
      'import socket; socket.create_connection(("127.0.0.1", 8000), 2).close()' \
      >/dev/null 2>&1; then
    server_ready=1
    break
  fi
  sleep 1
done
if test "$server_ready" != 1; then
  printf 'SERVER_FAILED: fixture did not listen inside its container\n' >&2
  docker logs --tail 50 "$preflight_server" >&2 || true
  exit 1
fi

set +e
timeout 20 docker run --interactive --rm --name "$preflight_client" \
  --label "$preflight_label_key=$preflight_id" \
  --network "$preflight_network" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python "$preflight_image" - <<'PY'
import http.client
import socket
import sys

host = "compose-http"
port = 8000
expected = b"cairn-compose-bridge-ok\n"
try:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except socket.gaierror as error:
    print(f"DNS_FAILED: {host}: {error}", file=sys.stderr)
    raise SystemExit(10)
if not addresses:
    print(f"DNS_FAILED: {host}: no addresses", file=sys.stderr)
    raise SystemExit(10)
print("DNS_OK: " + ",".join(sorted({item[4][0] for item in addresses})))

connect_errors = []
for family, socket_type, protocol, _, address in addresses:
    connection = socket.socket(family, socket_type, protocol)
    connection.settimeout(5)
    try:
        connection.connect(address)
    except OSError as error:
        connect_errors.append(f"{address[0]}:{address[1]}: {error}")
        connection.close()
        continue
    print(f"TCP_OK: {address[0]}:{address[1]}")
    try:
        connection.sendall(
            b"GET /probe HTTP/1.1\r\nHost: compose-http\r\nConnection: close\r\n\r\n"
        )
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = response.read(1024)
    except (OSError, http.client.HTTPException) as error:
        print(f"HTTP_FAILED: {error}", file=sys.stderr)
        raise SystemExit(12)
    finally:
        connection.close()
    if response.status != 200 or body != expected:
        print(
            f"HTTP_FAILED: status={response.status} body={body!r}",
            file=sys.stderr,
        )
        raise SystemExit(12)
    print("HTTP_OK: fixed body matched")
    raise SystemExit(0)

print("TCP_FAILED: " + "; ".join(connect_errors), file=sys.stderr)
raise SystemExit(11)
PY
client_status=$?
set -e

case "$client_status" in
  0) ;;
  10) printf 'Docker DNS failed; inspect daemon/firewall policy.\n' >&2; exit 1 ;;
  11) printf 'Docker bridge TCP failed after DNS; inspect host firewall forwarding policy.\n' >&2; exit 1 ;;
  12) printf 'Docker bridge HTTP response was wrong; inspect the fixture diagnostics.\n' >&2; exit 1 ;;
  124) printf 'Docker bridge client timed out after 20 seconds.\n' >&2; exit 1 ;;
  *) printf 'Docker bridge client failed with exit %s.\n' "$client_status" >&2; exit 1 ;;
esac

if ! cleanup_preflight; then
  printf 'preflight succeeded but cleanup could not be proved complete\n' >&2
  exit 1
fi
trap - EXIT
printf 'PASS: Docker DNS, bridge TCP and fixed HTTP response; cleanup complete\n'
```

Expected output includes `DNS_OK`, `TCP_OK`, `HTTP_OK` and the final `PASS`.
A DNS, TCP, HTTP or timeout diagnostic is a failed prerequisite. Give it to the
host administrator with `docker info` and follow the read-only
[Docker/firewalld diagnostics](../../docs/operations/docker-firewalld.md); do
not disable the firewall, open broad forwarding or change Cairn's Compose
files. Cleanup runs on failure, never removes an object whose random name has
lost its matching label, and reports labelled residue or an inventory it could
not complete. Remove reported residue only after verifying its ownership label.

After this passes, make the guide's single working-directory change:

```sh
cd deploy/compose
```

Every Compose command below runs from `deploy/compose`; the separate client
verification says explicitly when to move back to the repository root.

## Command and state shape

The image pins live in `../images.lock` and nowhere else (P-61), so every
Compose command reads two env files: the pins first, then this instance's
parameters. `.env` contains no image pin. The base form is:

```sh
docker compose --env-file ../images.lock --env-file .env -f compose.yaml ps
```

When semantic retrieval is enabled, every Compose command includes the
retrieval overlay as well:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml ps
```

The default `config.example.yaml` enables Attic and leaves semantic retrieval
off. The persistent pieces are:

| Location | Contents | Backup status |
| --- | --- | --- |
| `${COMPOSE_PROJECT_NAME}_cairn-data` named volume | The authoritative catalogue, audit history, delivery state and Attic exact evidence | Included in a Cairn backup bundle |
| `${COMPOSE_PROJECT_NAME}_falkordb-data` named volume | Semantic Graphiti/FalkorDB index | Derived; rebuild from the catalogue rather than backing it up |
| `.env` and `config.yaml` bind mounts | Project name, loopback port, stable instance UUID and non-secret runtime configuration | Preserve securely with the deployment record |
| `credentials/` and `falkordb.conf` bind mounts | Optional semantic provider and index credentials | Preserve in the approved credential store; never in a Cairn backup |
| `$HOME/.config/cairn/*-admin.token` | Bootstrap administrator bearer token used by clients | Preserve in an owner-only credential store; never in Git or the backup bundle |

Compose expands `COMPOSE_PROJECT_NAME` when it creates both volume names.
Cairn runs as numeric UID/GID `65532:0`. Docker initialises a fresh
`cairn-data` volume from the image with the required ownership. The host
configuration file is deliberately `0644` because it contains no secret and
UID 65532 must read it. Optional credential files need different ownership,
which the semantic procedure handles where it becomes necessary.

## One rule that governs every procedure below

**`backup` is the only command that runs beneath a serving instance.**
Everything else refuses rather than touching anything, so each of them is
a `docker compose stop cairn`, the command, then `up -d`.

The refusals are not one diagnostic, and a runbook that promised one
would send an operator looking for the wrong string. Measured against
this project with the instance serving:

| Command | Refusal | Exit |
| --- | --- | --- |
| `migrate`, `verify`, `bootstrap`, `recover` | `catalogue_unavailable` | 3 |
| `rebuild-index` | `already_locked` | 4 |
| `restore` | `data_directory_not_empty` | 4 |
| `backup` | — it succeeds | 0 |

`restore` is the odd one: it refuses on the *directory*, before it ever
reaches the lease, so stopping the instance does not satisfy it. It
wants an empty data volume, which is why its procedure below replaces
one.

That is not a Compose quirk; it is the same lease the Kubernetes shape
relies on to make "deliberately replace, never overlap" structural. The
procedures below already have the stop in the right place — this is here
so that a procedure nobody wrote down still comes out right.

## First boot with Attic

Run the following in Bash from `deploy/compose`. The four values at the top are
the only operator choices. `cairn_project` is a lowercase Compose project name;
`cairn_port` is an unused loopback TCP port; `cairn_realm` is a lowercase Cairn
realm identifier; and `cairn_label` is the administrator's label: 1–63 lowercase
letters, digits or hyphens, starting with a letter and ending with a letter or
digit.
The shown values are valid and can be used for a first local instance.

```bash
set -eu

cairn_project='cairn-a'
cairn_port='8080'
cairn_realm='local'
cairn_label='local-administrator'
cairn_credential_dir="$HOME/.config/cairn"
cairn_credential_file="$cairn_credential_dir/${cairn_project}-admin.token"

if ! [[ "$cairn_project" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  printf 'invalid Compose project name\n' >&2
  exit 1
fi
if ! [[ "$cairn_realm" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  printf 'invalid Cairn realm identifier\n' >&2
  exit 1
fi
if ! [[ "$cairn_label" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  printf 'invalid Cairn administrator label\n' >&2
  exit 1
fi
if ! [[ "$cairn_port" =~ ^[0-9]+$ ]] ||
    (( cairn_port < 1 || cairn_port > 65535 )); then
  printf 'invalid TCP port\n' >&2
  exit 1
fi

instance_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"

# Create the instance parameters without putting an image pin or a secret in
# .env. Redirection keeps the 0600 mode established before content is written.
for path in .env config.yaml credentials falkordb.conf; do
  if test -e "$path"; then
    printf 'refusing to overwrite existing path: %s\n' "$path" >&2
    exit 1
  fi
done
install -m 0600 /dev/null .env
{
  printf 'COMPOSE_PROJECT_NAME=%s\n' "$cairn_project"
  printf 'CAIRN_HOST_PORT=%s\n' "$cairn_port"
  printf 'CAIRN_CONFIG_FILE=./config.yaml\n'
  printf 'CAIRN_CREDENTIALS_DIR=./credentials\n'
} > .env

# Preserve this generated UUID for the lifetime of the instance. The template
# contains exactly one deliberately invalid marker, so refuse unexpected input.
install -m 0644 config.example.yaml config.yaml
python3 - "$instance_id" <<'PY'
from pathlib import Path
import sys

path = Path("config.yaml")
marker = "REPLACE_WITH_PER_INSTANCE_UUID"
text = path.read_text(encoding="utf-8")
if text.count(marker) != 1:
    raise SystemExit("config template has an unexpected instance_id marker")
path.write_text(text.replace(marker, sys.argv[1]), encoding="utf-8")
PY
chmod 0644 config.yaml

# Empty is correct while semantic retrieval is disabled. The directory itself
# is readable so UID 65532 can traverse the read-only bind mount.
install -d -m 0755 credentials
stat -c '%a %n' .env config.yaml credentials

docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml config -q
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml run --rm --no-deps cairn migrate \
  --config /etc/cairn/config.yaml

# Bootstrap exactly once. Create the destination before the token exists,
# refuse an existing file or symlink, and keep it outside the checkout.
install -d -m 0700 "$cairn_credential_dir"
repository_root="$(git rev-parse --show-toplevel)"
credential_parent="$(realpath -e -- "$cairn_credential_dir")"
case "$credential_parent" in
  "$repository_root"|"$repository_root"/*)
    printf 'credential directory must be outside the checkout\n' >&2
    exit 1
    ;;
esac
cairn_credential_file="$credential_parent/$(basename -- "$cairn_credential_file")"

umask 077
set -C
if ! : > "$cairn_credential_file"; then
  printf 'credential path already exists; refusing to overwrite it\n' >&2
  exit 1
fi
set +C
chmod 0600 "$cairn_credential_file"

bootstrap_result="$(mktemp)"
if ! docker compose --env-file ../images.lock --env-file .env \
    -f compose.yaml run --rm --no-deps cairn bootstrap \
    --config /etc/cairn/config.yaml \
    --realm "$cairn_realm" --label "$cairn_label" \
    > "$bootstrap_result"; then
  rm -f "$cairn_credential_file"
  printf 'bootstrap failed; owner-only result retained at %s\n' \
    "$bootstrap_result" >&2
  exit 1
fi

if ! jq -er \
    'select(.status == "ok" and .operation == "bootstrap") | .token | strings | select(startswith("cairn1."))' \
    "$bootstrap_result" > "$cairn_credential_file"; then
  rm -f "$cairn_credential_file"
  printf 'token extraction failed; owner-only result retained at %s\n' \
    "$bootstrap_result" >&2
  exit 1
fi
if ! test -s "$cairn_credential_file" || \
    test "$(stat -c '%a' "$cairn_credential_file")" != 600; then
  rm -f "$cairn_credential_file"
  printf 'credential validation failed; owner-only result retained at %s\n' \
    "$bootstrap_result" >&2
  exit 1
fi
rm -f "$bootstrap_result"

docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml up -d --wait --wait-timeout 330
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml ps
curl --disable --silent --show-error --fail --noproxy '*' --proto '=http' \
  --max-redirs 0 --max-time 5 \
  "http://127.0.0.1:${cairn_port}/health/ready"
printf '\ninstance_id=%s\ncredential_file=%s\n' \
  "$instance_id" "$cairn_credential_file"
```

The mode check must print `600 .env`, `644 config.yaml` and `755 credentials`.
`config -q` and migration must exit zero. `up --wait` must finish with `cairn`
healthy, and the health request must return `{"status":"ready"}`. Cairn is
published only on the host's `127.0.0.1` address at `cairn_port`. Set
`base_url="http://127.0.0.1:${cairn_port}"` and
`credential_file="$cairn_credential_file"`, then run the client guide's
[Credentials setup](../../docs/clients.md#credentials) followed by its
[authenticated instance check](../../docs/clients.md#check-the-instance).
The setup's `:=` defaults preserve these two values when they are already set.
It must return the generated `instance_id` and the pinned contract digests.

Attic is enabled now. It stores exact evidence in the `cairn-data` volume and
participates in retrieval only after a semantic index supplies candidates.
With semantic retrieval disabled, authenticated identity and ingest custody can
still be verified. A REST retrieval is refused as HTTP 400 with public code
`invalid_request`; the internal audit reason is `retrieval_disabled`, so this
is permanent until semantic retrieval is configured rather than an indexing
delay.

If bootstrap returns `realm_exists`, the volume is already bootstrapped. Do not
bootstrap again, change `instance_id`, overwrite a retained token, or delete the
volume. A normal start uses `up -d` with the existing `.env`, `config.yaml`,
volume and token. If the only administrator token has been lost, keep the data
stopped and use the local `cairn recover` incident procedure; ordinary restart
does not mint replacement credentials.

## Quick Attic write/read test

From `deploy/compose`, return to the repository root with `cd ../..`. Keep the
`base_url`, `credential_file` and protected `curl_config` from the client setup
above. With Python 3.12–3.14 as `python3`, curl 8.4.0 or newer and jq, commit a
small synthetic payload and read back its exact bytes:

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

Expect `"status": "verified"` with the saved evidence ID and SHA-256. The helper
checks exact UTF-8 bytes, byte length and digest, honours `Retry-After`, retries
only reads and stops after 120 seconds or 30 attempts. Corruption or mismatched
bytes fail immediately. Retain the printed directory; the payload remains in
the persistent volume. No FalkorDB or semantic provider is required.

See the [Attic round-trip documentation](../../docs/operations/evidence-verification.md)
for failure details and uncertain-write recovery. Do not repeat a committed
ingest. Then restart using the same Compose configuration:

```bash
(cd deploy/compose && docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml restart cairn)
for attempt in $(seq 1 60); do
  if curl --disable --silent --show-error --fail --noproxy '*' --max-time 5 \
    "$base_url/health/ready"; then break; fi
  test "$attempt" -lt 60 || { printf 'Cairn did not become ready after restart\n' >&2; exit 1; }
  sleep 2
done
```

Repeat only the saved evidence-read command from the repository root. Keep the
same `.env`, configuration, volumes and credential. This checks exact evidence
retention without FalkorDB or a provider.

## Optional semantic retrieval

Semantic retrieval adds Graphiti, a project-private FalkorDB index and outbound
OpenAI API calls. It is optional; Attic remains enabled either way. Two halves
are required: the overlay adds FalkorDB, and `config.yaml` tells Cairn to use
it. If Docker, firewalld or host network policy has changed since first boot,
repeat the [container connectivity preflight](#container-to-container-connectivity-preflight)
in a separate repository-root terminal before stopping Cairn. Recheck
privileged access immediately before stopping Cairn, even if the
initial preflight passed. Run this block from `deploy/compose`; a failed
authentication or command-permission check exits before shutdown or credential
creation. Stop Cairn before changing the configuration.

```bash
set -eu

if (( EUID != 0 )); then
  command -v sudo || exit 1
  sudo -v || exit 1
  sudo -l -- chown 65532:0 credentials/falkordb-password credentials/openai-api-key >/dev/null || exit 1
  sudo -l -- chmod 0400 credentials/falkordb-password credentials/openai-api-key >/dev/null || exit 1
  sudo -l -- chown 10001:0 falkordb.conf >/dev/null || exit 1
  sudo -l -- chmod 0400 falkordb.conf >/dev/null || exit 1
fi

docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml stop cairn

test ! -e credentials/falkordb-password
test ! -e credentials/openai-api-key
test ! -e falkordb.conf
umask 077
openssl rand -hex 32 > credentials/falkordb-password
{
  printf 'requirepass '
  tr -d '\r\n' < credentials/falkordb-password
  printf '\n'
} > falkordb.conf

# Read the provider key from the terminal without echo, argv, environment or
# shell history. An empty value is refused.
IFS= read -r -s -p 'OpenAI API key: ' openai_api_key
printf '\n'
test -n "$openai_api_key"
printf '%s\n' "$openai_api_key" > credentials/openai-api-key
unset openai_api_key

# These operations need privilege because the consuming numeric identities are
# Cairn 65532:0 and FalkorDB 10001:0, not the host operator.
if (( EUID == 0 )); then
  privileged=()
else
  privileged=(sudo)
fi
"${privileged[@]}" chown 65532:0 \
  credentials/falkordb-password credentials/openai-api-key
"${privileged[@]}" chmod 0400 \
  credentials/falkordb-password credentials/openai-api-key
"${privileged[@]}" chown 10001:0 falkordb.conf
"${privileged[@]}" chmod 0400 falkordb.conf
stat -c '%u:%g %a %n' \
  credentials/falkordb-password credentials/openai-api-key falkordb.conf

printf 'FALKORDB_CONFIG_FILE=./falkordb.conf\n' >> .env
python3 <<'PY'
from pathlib import Path

path = Path("config.yaml")
lines = path.read_text(encoding="utf-8").splitlines()
start = lines.index("graphiti:")
enabled = next(
    position
    for position in range(start + 1, len(lines))
    if lines[position].startswith("  enabled:")
)
if lines[enabled] != "  enabled: false":
    raise SystemExit("graphiti.enabled is not the expected disabled value")
lines[enabled : enabled + 1] = [
    "  enabled: true",
    "  host: falkordb",
    "  port: 6379",
]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
chmod 0644 config.yaml

docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml config -q
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml up -d --wait --wait-timeout 330
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml ps
```

The ownership check must show `65532:0 400` for both files under
`credentials/` and `10001:0 400` for `falkordb.conf`. If it does not, do not
start the services.

Cairn waits for the index's healthcheck before it starts, and that check
asserts an *authenticated* `PONG`: `redis-cli` exits 0 on a `NOAUTH`
reply as readily as on a successful one, so a check that only read its
exit status would call an unauthenticated or wrongly-credentialled index
healthy. Measured, not assumed. Run the client guide's
[bounded ingest and retrieval verification](../../docs/clients.md#bounded-ingest-and-retrieval-verification).
It captures the committed ingest before polling, preserves HTTP failures and
`Retry-After`, and never repeats a committed ingest while indexing catches up.
That client procedure explicitly changes to the repository root. Return with
`cd deploy/compose` before running any later Compose command in this guide.

If the index is lost, it rebuilds from the catalogue — it is derived
state, and the backup bundle excludes it on purpose (P-65). The rebuild
takes the data-directory lease, so the instance stops first:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml stop cairn
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml \
  run --rm cairn rebuild-index --config /etc/cairn/config.yaml
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml up -d
```

**Outbound traffic leaves this host directly.** The Kubernetes shape
routes provider calls through a per-cluster egress gateway (I-93, P-64);
Compose has no equivalent, and this project sets no proxy variable. An
operator who terminates egress through a proxy adds it in an override
file of their own:

```yaml
# compose.egress.yaml
services:
  cairn:
    environment:
      HTTPS_PROXY: http://proxy.internal:3128
```

## Normal operation, reboot and cleanup

For the base instance, these are the complete status, stop, start and restart
commands:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml ps
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml stop
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml start
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml restart
```

With semantic retrieval enabled, use the same operations with both files:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml ps
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml stop
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml start
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml restart
```

`stop` retains the containers, project network, both named volumes and every
host file. `start` reuses them. `restart` stops and starts the existing
containers with the same stable instance UUID, catalogue, Attic evidence,
semantic index and credentials. It does not migrate a new image or schema.
After `stop`, `ps --all` must show the service containers as exited. After
`start` or `restart`, `ps` must show Cairn healthy and the loopback readiness
request from first boot must again return `{"status":"ready"}`.

The services use `restart: unless-stopped`. After the first successful `up`,
they return after a host reboot when the Docker daemon is enabled and the
operator did not deliberately stop them. A deliberate `stop` suppresses that
automatic return; run `start` or `up -d` to enable the instance again. `down`
removes the containers and project network, so nothing starts automatically
after reboot, but it retains named volumes and host files. Recreate the base
instance with:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml down
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml up -d --wait --wait-timeout 330
```

For a semantic instance, the exact equivalent is:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml down
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml \
  up -d --wait --wait-timeout 330
```

Never add `-v` to normal `down`: `down -v` deletes `cairn-data`, including the
authoritative catalogue, audit history and Attic evidence, and also deletes
the rebuildable FalkorDB index volume when present. Removing `config.yaml`
loses the configured stable identity; removing the external administrator
token loses that credential. Removing `.env` loses the project-to-volume
selection, while removing `credentials/` or `falkordb.conf` prevents a semantic
instance from starting. Compose does not delete those bind-mounted host files
for you.

Before deleting or replacing any volume, take and export a verified Cairn
bundle using [Backup and restore](#backup-and-restore) and the authoritative
[backup runbook](../../docs/operations/backup-restore.md). A backup contains
the catalogue and Attic, not credentials or the derived semantic index.

## Upgrade

Explicit, in I-39's spirit — the schema never migrates on a restart:

```bash
set -eu

# 1. Supply the reviewed tag or name@sha256:digest, then replace exactly the
#    Cairn pin in ../images.lock. This value is not a credential.
IFS= read -r -p 'Reviewed Cairn image reference: ' new_cairn_image
test -n "$new_cairn_image"
python3 - "$new_cairn_image" <<'PY'
from pathlib import Path
import sys

path = Path("../images.lock")
lines = path.read_text(encoding="utf-8").splitlines()
matches = [position for position, line in enumerate(lines) if line.startswith("CAIRN_IMAGE=")]
if len(matches) != 1:
    raise SystemExit("images.lock must contain exactly one CAIRN_IMAGE pin")
lines[matches[0]] = f"CAIRN_IMAGE={sys.argv[1]}"
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

# 2. Pull it.
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml pull

# 3. Stop the instance. `migrate` takes the data-directory lease, so it
#    refuses to run beneath a serving container — `catalogue_unavailable`,
#    exit 3, with the old schema still in place and nothing migrated.
#    Stopping first is not tidiness; it is the only order that works.
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml stop cairn

# 4. Migrate with the new image, before it serves.
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml run --rm --no-deps cairn migrate \
  --config /etc/cairn/config.yaml

# 5. Replace the container. `up -d` recreates it because the image
#    changed.
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml up -d --wait --wait-timeout 330
```

The stop in step 3 is the one that spends I-20's grace period: the
container gets its 60 seconds to finish in-flight work and close the
catalogue.

Take a backup first. Rolling back is restoring one.

## Backup and restore

The bundle is written by the CLI, not by an HTTP route (I-35), and a
backup can be taken while the instance serves: it deliberately takes no
lease, and its barrier is inside the command.

`--output` is a *directory* the command writes into, not a file it creates.
Each run makes its own directory, whose name begins `cairn-backup-` and includes
the instance ID and timestamp, and refuses rather than overwriting one that
exists:

```sh
# The container writes as 65532, so make the destination writable by that UID.
sudo install -d -o 65532 -g 0 -m 0750 backups
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml \
  run --rm --no-deps -v "$PWD/backups:/backups" \
  cairn backup --config /etc/cairn/config.yaml --output /backups
```

The command prints the bundle it wrote as `bundle` in its JSON, which is
what a scripted backup should read rather than reconstructing the name.

Restore takes the whole emitted bundle directory and refuses a non-empty data
directory, a bundle from another instance and a tampered member, each as its
own typed refusal (P-66). Follow the
[replacement restore procedure](../../docs/operations/backup-restore.md#restore-into-a-replacement):
it preserves the original volume, restores into a fresh empty `cairn-data`
volume with the same configured `instance_id`, and admits the replacement only
after verification. With semantic retrieval enabled it also creates a fresh
FalkorDB volume and runs `rebuild-index` before serving. Do not improvise this
with `down -v`, a copied SQLite file or a reconstructed bundle name.

## Publication is explicit operator work

The project publishes to `127.0.0.1` and the address is not a parameter.
Reaching an instance from anywhere else means a TLS-terminating reverse
proxy in front of that port — the Compose analogue of I-12's "external
production publication is explicit and TLS-only" — and that proxy is
also where rate limiting belongs, for the reason the top-level README
gives: an unauthenticated flood costs disk, because denials are audit
evidence.

## Production acceptance

P-69's full target-tier procedure runs from the repository root. From this
guide's `deploy/compose` working directory, execute it in a subshell:

```sh
(cd ../.. && make compose-acceptance)
```

It requires root-equivalent access to the Docker daemon and the recorded
`reference` versions (Docker Engine 29.7.2 and Compose v5.4.0). The harness
builds the checked-out Cairn source, creates three disposable projects whose
names begin `cairn-t10-`, uses loopback ports 18180–18182, and writes
`build/compose-acceptance/report.json`. It removes only those prefixed
projects, networks and volumes when the run ends; pass `--keep-resources`
directly to `scripts/compose-acceptance` when they must remain for inspection.

The procedure exercises both the base and retrieval-enabled compositions.
The retrieval project uses synthetic local FalkorDB and provider credentials;
it proves the one-shot, authenticated index healthcheck and Cairn startup, but
does not claim a live model-provider round trip. Backup and restore cross the
container boundary through `docker exec` and `docker cp`, and the upgrade
rehearsal refuses to pass unless the replacement image has a different image
ID. Every cross-project refusal is paired with a same-window connection from
the destination project's own network to the identical address and port.

## What Compose does not give you

Stated here rather than discovered later. None of it is a defect in the
packaging; all of it is the difference between a container runtime and an
orchestrator.

- **No policy isolation.** Compose never carries the I-06/I-13 evidence,
  because Docker network drivers are not enforced NetworkPolicy.
  Co-tenant Compose instances on one host are separated by Docker's
  namespaces, by project-scoped networks and volumes, and by the
  two-project checks the acceptance suite actually runs — not by an
  enforced policy layer. An operator who needs the latter deploys the
  Kubernetes shape.
- **No liveness action.** Docker restarts a container that *exits*, never
  one that is up and failing its healthcheck. The check gates
  `depends_on` and informs `docker compose ps`; it is not a liveness
  probe.
- **No Secret object.** Compose has no equivalent of a Secret: every
  credential here is a file an operator created and made readable to one
  container. That is why nothing in this project puts a password in
  `.env`, in a service environment or on a command line — there is no
  kubelet to compose a value from a reference, so the file *is* the
  arrangement. Keep the two credential files 0400 and owned by the UID
  that reads them, and `.env` 0600, which the first-boot procedure does.
- **No egress gateway.** See above.

## Validation

`docker compose config -q` runs over both compositions inside
`make check`, so this artefact cannot drift silently, and
`tests/deploy/test_compose_project.py` holds its production posture — the
loopback publication, the read-only mounts, the grace period, the pin
indirection and the configuration document's agreement with the
Kustomize base — to the same standard as the rendered manifests.
