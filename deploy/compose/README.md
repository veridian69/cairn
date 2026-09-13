# The Compose project

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

`CAIRN_IMAGE=cairn:v0.1.0` in `../images.lock` is a local build tag, not a
published image. Build it from the repository root before first boot:

```sh
make image IMAGE=cairn:v0.1.0
```

For deployment, use the reviewed local build or a separately reviewed registry
digest and record its exact identity.

The wire contracts are already pinned. From the repository root, verify them
before deploying:

```sh
(cd contracts && sha256sum -c cairn-openapi-v1.json.sha256)
(cd contracts && sha256sum -c cairn-mcp-tools-v1.json.sha256)
```

Expected SHA-256 values:

| Contract | SHA-256 |
| --- | --- |
| REST/OpenAPI | `0e4dc424225a7bb12f3b0c1c2008a5bf30415c840a6fc60247079f986d7b4bf3` |
| MCP tools | `b75f3fa736c6f4285b180e4795945fdc0d145a8ce6bcf60884f118b9636e74ca` |

After first boot, compare those values and the expected instance UUID through
authenticated `GET /v1/instance` using the
[safe client procedure](../../docs/clients.md#check-the-instance). An image
reference alone does not prove which contract an endpoint serves.

## The shape of every command

The image pins live in `../images.lock` and nowhere else (P-61), so every
command reads two env files — the pins, then this instance's parameters:

```sh
cd deploy/compose
docker compose --env-file ../images.lock --env-file .env up -d
```

Everything below is written from this directory and uses that pair. The
`.env` file sets no image variable, so a pin can never be changed in one
place and forgotten in another.

If retrieval is enabled, add the overlay file to *every* command:

```sh
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml <command>
```

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

## First boot

```sh
cd deploy/compose

# 1. Parameters and configuration. Both copies are yours, neither is
#    committed, and both are already in .gitignore.
#
#    `.env` is created 0600 rather than copied and chmod-ed afterwards:
#    with retrieval enabled it holds the index password, and a `cp` would
#    leave it 0644 — world-readable for however long it takes to
#    remember.
#
#    `config.yaml` gets an explicit mode for the mirror-image reason, and
#    it is the operator's ambient `umask` that makes it necessary rather
#    than anything in this project. `cp` takes the mode from the source
#    minus that umask, so on a hardened host — `umask 077`, which the
#    retrieval section below sets deliberately and a careful sysadmin may
#    have as a shell default — the copy lands 0600 owned by *you*. The
#    container reads it as 65532, so it would get the world bits, and
#    there are none: Cairn cannot open its own configuration document.
#    Measured, 15 August 2026: umask 022 renders 0644 and the read
#    succeeds, umask 077 renders 0600 and it fails. The document carries
#    no credential — those are files of their own under `credentials/`,
#    per I-19 — so 0644 is the right mode and not a concession.
install -m 0600 .env.example .env
install -m 0644 config.example.yaml config.yaml
$EDITOR .env                # instance name, loopback port
$EDITOR config.yaml         # a real instance_id — `uuidgen` will do

# 2. The adapter-credential directory. Empty is correct with retrieval
#    disabled; the "Turning retrieval on" section fills it.
install -d -m 0755 credentials

# 3. Migrate the catalogue. `--no-deps` because migration needs no index,
#    and this is the same idempotent step the Kubernetes shape runs as an
#    init container (I-39). Schema mutation never rides an ordinary
#    restart — it is this command, every time.
docker compose --env-file ../images.lock --env-file .env \
  run --rm --no-deps cairn migrate --config /etc/cairn/config.yaml

# 4. Bootstrap the realm once and capture the token in a *new* owner-only
#    file outside this checkout. The subshell exits on any refusal and step
#    5 is inside it, so a failed capture can never fall through to serving.
(
  set -eu
  umask 077

  credential_file=/absolute/path/to/cairn-credential
  case "$credential_file" in
    /*) ;;
    *) printf 'credential path must be absolute\n' >&2; exit 1 ;;
  esac

  repository_root="$(git rev-parse --show-toplevel)"
  credential_parent="$(realpath -e -- "$(dirname -- "$credential_file")")"
  case "$credential_parent" in
    "$repository_root"|"$repository_root"/*)
      printf 'credential path must be outside the checkout\n' >&2
      exit 1
      ;;
  esac
  credential_file="$credential_parent/$(basename -- "$credential_file")"

  # noclobber makes creation atomic: an existing file or symlink is refused.
  # The 0077 umask creates the new file 0600 before any token exists.
  set -C
  if ! : > "$credential_file"; then
    printf 'credential path already exists or cannot be created\n' >&2
    exit 1
  fi
  set +C
  chmod 0600 "$credential_file"

  bootstrap_result="$(mktemp)"
  if ! docker compose --env-file ../images.lock --env-file .env \
      run --rm --no-deps cairn bootstrap --realm <realm> --label <label> \
      > "$bootstrap_result"; then
    rm -f "$credential_file"
    printf 'bootstrap failed; owner-only result retained at %s\n' \
      "$bootstrap_result" >&2
    exit 1
  fi

  if ! jq -er \
      'select(.status == "ok" and .operation == "bootstrap") | .token | strings | select(startswith("cairn1."))' \
      "$bootstrap_result" > "$credential_file"; then
    rm -f "$credential_file"
    printf 'token extraction failed; owner-only result retained at %s\n' \
      "$bootstrap_result" >&2
    exit 1
  fi
  if ! test -s "$credential_file" || \
      test "$(stat -c '%a' "$credential_file")" != 600; then
    rm -f "$credential_file"
    printf 'credential validation failed; owner-only result retained at %s\n' \
      "$bootstrap_result" >&2
    exit 1
  fi

  # Only the verified credential now remains. The full result also contains
  # the token, so remove it before starting the service.
  rm -f "$bootstrap_result"

  # 5. Serve only after successful credential capture.
  docker compose --env-file ../images.lock --env-file .env up -d
  docker compose --env-file ../images.lock --env-file .env ps
)
```

Health, from the host, once `ps` reports the container healthy:

```sh
curl -s http://127.0.0.1:8080/health/ready
```

Require `{"status":"ready"}`. Then run the client guide's
[authenticated instance check](../../docs/clients.md#check-the-instance) with
the owner-only credential path chosen above; do not substitute a token into a
command argument or enable verbose curl output.

## Turning retrieval on

Two halves, and both are needed: the overlay file adds the index service,
and the configuration document says to use it.

```sh
# 1. The password, written to the two files that need it, from one
#    generated secret. Cairn reads its copy as a bare value (I-92); the
#    index reads its own configuration file, which is where its
#    credential lives — not in .env, not in the service environment, and
#    not on any command line.
umask 077
openssl rand -hex 32 > credentials/falkordb-password
printf 'requirepass %s\n' "$(cat credentials/falkordb-password)" > falkordb.conf
printf 'FALKORDB_CONFIG_FILE=./falkordb.conf\n' >> .env

# 2. The provider key, as a file — never an environment variable (I-19).
$EDITOR credentials/openai-api-key

# 3. Each file readable by the container that needs it and nobody else
#    on this host. Needs root, because neither UID is you: Cairn runs as
#    65532, the index as 10001.
#
#    0400 is enough here, and would not be on Kubernetes. A bind mount
#    keeps the file's host ownership, so each of these is *owned* by the
#    UID that opens it and the owner bits are the ones that count. A
#    Kubernetes Secret projects root:root and cannot be chowned, which is
#    why the manifests need 0440 and group 0 instead.
sudo chown 65532:0 credentials/falkordb-password credentials/openai-api-key
sudo chmod 0400 credentials/falkordb-password credentials/openai-api-key
sudo chown 10001:0 falkordb.conf
sudo chmod 0400 falkordb.conf

# 4. The configuration document. Three keys, exactly as the kind overlay
#    sets them:
#
#      graphiti:
#        enabled: true
#        host: falkordb
#        port: 6379
#
#    This edit happens under the `umask 077` set above, and some editors
#    write a new file and rename it over the old one rather than writing
#    in place. Check the mode afterwards: it must stay group- or
#    world-readable, or Cairn cannot open it as 65532.
$EDITOR config.yaml
test "$(stat -c '%a' config.yaml)" = 0644 || chmod 0644 config.yaml

# 5. Up, with the overlay. The `falkordb-init` one-shot runs first and
#    hands the index's volume to the index — a fresh named volume arrives
#    root-owned, because the FalkorDB image contains no
#    /var/lib/falkordb/data for Docker to copy ownership from, and
#    Compose has no fsGroup. There is nothing to remember: it runs on
#    every `up` and is idempotent.
docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.graphiti.yaml up -d
```

Cairn waits for the index's healthcheck before it starts, and that check
asserts an *authenticated* `PONG`: `redis-cli` exits 0 on a `NOAUTH`
reply as readily as on a successful one, so a check that only read its
exit status would call an unauthenticated or wrongly-credentialled index
healthy. Measured, not assumed.

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

## Upgrade

Explicit, in I-39's spirit — the schema never migrates on a restart:

```sh
# 1. Change the pin. One line, in ../images.lock, reviewed like any other.
$EDITOR ../images.lock

# 2. Pull it.
docker compose --env-file ../images.lock --env-file .env pull

# 3. Stop the instance. `migrate` takes the data-directory lease, so it
#    refuses to run beneath a serving container — `catalogue_unavailable`,
#    exit 3, with the old schema still in place and nothing migrated.
#    Stopping first is not tidiness; it is the only order that works.
docker compose --env-file ../images.lock --env-file .env stop cairn

# 4. Migrate with the new image, before it serves.
docker compose --env-file ../images.lock --env-file .env \
  run --rm --no-deps cairn migrate --config /etc/cairn/config.yaml

# 5. Replace the container. `up -d` recreates it because the image
#    changed.
docker compose --env-file ../images.lock --env-file .env up -d
```

The stop in step 3 is the one that spends I-20's grace period: the
container gets its 60 seconds to finish in-flight work and close the
catalogue.

Take a backup first. Rolling back is restoring one.

## Backup and restore

The bundle is written by the CLI, not by an HTTP route (I-35), and a
backup can be taken while the instance serves: it deliberately takes no
lease, and its barrier is inside the command.

`--output` is a *directory* the command writes into, not a file it
creates. Each run makes its own
`cairn-backup-<instance_id>-<timestamp>/` beneath it and refuses rather
than overwriting one that exists:

```sh
# The container writes as 65532, so the destination is its to write —
# the same ownership the credential files need, and for the same reason.
sudo install -d -o 65532 -g 0 -m 0750 backups
docker compose --env-file ../images.lock --env-file .env \
  run --rm --no-deps -v "$PWD/backups:/backups" \
  cairn backup --config /etc/cairn/config.yaml --output /backups
```

The command prints the bundle it wrote as `bundle` in its JSON, which is
what a scripted backup should read rather than reconstructing the name.

Restore takes the whole bundle directory, and refuses a non-empty data
directory, a bundle from another instance and a tampered member, each as
its own typed refusal (P-66). It therefore wants a *new* volume with the
instance stopped — and, with retrieval enabled, a new index volume too:
the restored catalogue and the surviving index describe different
instants, and the index is the one that is wrong.

```sh
C="docker compose --env-file ../images.lock --env-file .env"      # add -f … with retrieval
$C down
docker volume rm <project>_cairn-data          # or restore into a fresh project
docker volume rm <project>_falkordb-data       # retrieval only: derived state, never restored
$C run --rm --no-deps -v "$PWD/backups:/backups" \
  cairn restore --config /etc/cairn/config.yaml \
  --bundle /backups/cairn-backup-<instance_id>-<timestamp>
$C up -d                                       # without retrieval, this is the last step
```

With retrieval enabled, rebuild the index *instead of* that last `up -d`
— the restored instance would otherwise start with an empty index and
answer retrievals from nothing. `rebuild-index` takes the lease, so it
runs while Cairn is still stopped, and `run` without `--no-deps` starts
the index it needs:

```sh
$C run --rm cairn rebuild-index --config /etc/cairn/config.yaml
$C up -d
```

## Publication is explicit operator work

The project publishes to `127.0.0.1` and the address is not a parameter.
Reaching an instance from anywhere else means a TLS-terminating reverse
proxy in front of that port — the Compose analogue of I-12's "external
production publication is explicit and TLS-only" — and that proxy is
also where rate limiting belongs, for the reason the top-level README
gives: an unauthenticated flood costs disk, because denials are audit
evidence.

## Production acceptance

P-69's full target-tier procedure is executable from the repository root:

```sh
make compose-acceptance
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
