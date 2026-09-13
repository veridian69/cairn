# Backup and restore

This runbook covers Cairn's application-consistent bundle, export to an
operator-selected sink, and replacement restore. `backup` and `restore` are
local CLI operations; there is no REST or MCP route for either.

## Safety boundary

- A backup may run beside a serving instance. Restore must never do so.
- Restore retains the immutable `instance_id`; it is not clone, rebind or
  import. The configured ID and bundle ID must match.
- Restore targets an empty replacement volume. It never merges into existing
  data and refuses a non-empty directory (`lost+found` alone is tolerated).
- Quiescence across the original and replacement volumes is an operator rule.
  The data-directory lease cannot enforce it across PVCs or Compose volumes.
- Credentials and FalkorDB are excluded from the bundle. Supply credentials
  separately and rebuild the derived index after restore.
- Keep the quiesced original volume intact until the replacement has passed
  verification and authenticated scoped reads. It is the rollback point.

The disposable kind tier and the `reference` vanilla-Kubernetes target exercised
the replacement sequence. The Compose production suite exercised its own
round trip. None of that is OpenShift target evidence.

## What a backup contains

`cairn backup --config <path> --output <directory>` creates a new directory:

```text
cairn-backup-<instance_id>-<UTC-timestamp>/
├── catalogue.sqlite3
├── attic.sqlite3       # only when Attic is enabled and has created it
└── manifest.json
```

The manifest is `cairn.backup/v1` and records the instance identity, creation
time, catalogue schema version, declared audit boundary, and byte count and
SHA-256 for each member. Treat the directory as one indivisible bundle; the
manifest, not an archive wrapper or CSI snapshot, is its byte-integrity boundary.

Backup success and matching member hashes confirm capture and byte integrity;
they do not establish semantic catalogue validity. A barrier-consistent backup
may preserve corrupt history for diagnosis. Run `cairn verify --config <path>`
on a quiesced catalogue, or follow the verified replacement-restore procedure
below, before admitting recovered data. Offline verification and restore refuse
semantically invalid proposal history even when the bundle's member hashes match.

The command prints JSON containing `bundle`, `barrier_ms` and member digests.
Capture that JSON. Do not reconstruct the generated directory name.

## Barrier trade-off

The backup takes a `BEGIN IMMEDIATE` mutation barrier, reads the audit boundary
and copies the catalogue then Attic through SQLite's online backup API. While
the barrier is held no mutation can commit. A writer which outlasts the
5,000 ms busy timeout receives the typed retryable `dependency_unavailable`
failure.

That bounded mutation unavailability is deliberate: it buys one declared
audit instant across the bundle. Removing the barrier would produce members
from different instants and an audit boundary which never existed. Recorded
barriers were 8.937 ms in the disposable kind run and 16.197 ms in the
Compose production run; they do not predict a grown production catalogue.
Monitor each backup's emitted `barrier_ms` and mutation retry rate.

## Backup

### Native systemd user service

The [persistent native installation](native-installation.md) uses these fixed
paths:

```text
~/.config/cairn/config.yaml
~/.local/share/cairn/data/
~/.local/share/cairn/credentials/local-operator.token
~/.local/share/cairn/backups/
```

Its authoritative state is the catalogue plus the Attic database in `data`.
The runtime environment is rebuildable. The base procedure leaves semantic
retrieval disabled; its supported optional extension runs FalkorDB separately.
That index remains rebuildable and is not in the Cairn bundle.

A backup may run while the service is active. The following creates an
owner-only recovery directory, validates the completed bundle, and copies the
stable configuration and one-time plaintext token beside it. Those two copies
are recovery material, not bundle members:

```sh
set -eu
umask 077

runtime_dir="$HOME/.local/share/cairn/runtime"
config_file="$HOME/.config/cairn/config.yaml"
credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
backup_root="$HOME/.local/share/cairn/backups"

test -x "$runtime_dir/bin/cairn"
test -f "$config_file"
test -f "$credential_file"
case "$(stat -c '%a' "$credential_file")" in
  400|600) ;;
  *) printf 'Credential must be owner-only (0400 or 0600)\n' >&2; exit 1 ;;
esac
recovery_dir="$(mktemp -d "$backup_root/recovery.XXXXXXXX")"
chmod 0700 "$recovery_dir"

backup_json="$(
  "$runtime_dir/bin/cairn" backup --config "$config_file" \
    --output "$recovery_dir"
)"
printf '%s\n' "$backup_json" | jq .
printf '%s\n' "$backup_json" |
  jq -e '.status == "ok" and .operation == "backup"' >/dev/null
printf '%s\n' "$backup_json" >"$recovery_dir/backup-result.json"
chmod 0600 "$recovery_dir/backup-result.json"

bundle="$(printf '%s\n' "$backup_json" | jq -er '.bundle')"
case "$bundle" in
  "$recovery_dir"/*) ;;
  *) printf 'Unexpected bundle location: %s\n' "$bundle" >&2; exit 1 ;;
esac
manifest="$bundle/manifest.json"
jq -e '.schema_version == "cairn.backup/v1" and
       (.instance_id | type == "string") and
       (.members | type == "array" and length > 0)' "$manifest" >/dev/null

verified=0
while IFS="$(printf '\t')" read -r member bytes digest; do
  case "$member" in
    catalogue.sqlite3|attic.sqlite3) ;;
    *) printf 'Unexpected bundle member: %s\n' "$member" >&2; exit 1 ;;
  esac
  member_path="$bundle/$member"
  test -f "$member_path"
  test "$(wc -c <"$member_path" | tr -d ' ')" = "$bytes"
  test "$(sha256sum "$member_path" | awk '{print $1}')" = "$digest"
  verified=$((verified + 1))
done <<EOF
$(jq -r '.members[] | [.name, (.bytes | tostring), .sha256] | @tsv' "$manifest")
EOF
test "$verified" -eq "$(jq '.members | length' "$manifest")"
sha256sum "$manifest" >"$recovery_dir/manifest.sha256"

install -m 0600 "$config_file" "$recovery_dir/config.yaml"
install -m 0600 "$credential_file" \
  "$recovery_dir/local-operator.token"
for adapter_credential in falkordb-password openai-api-key; do
  adapter_path="$HOME/.local/share/cairn/credentials/$adapter_credential"
  if [ -f "$adapter_path" ]; then
    install -m 0600 "$adapter_path" "$recovery_dir/$adapter_credential"
  fi
done
printf 'Recovery directory: %s\n' "$recovery_dir"
```

The final path is the unit to export. Transfer the whole recovery directory to
an approved encrypted, access-controlled backup sink. Record its location,
manifest digest, Cairn revision, `instance_id` and `barrier_ms`, then perform a
restore rehearsal under the retention policy. The plaintext token makes this
directory secret; do not commit it, put it in ordinary object storage, or copy
it into command output. A site may instead retain the token in its existing
secret manager and omit the adjacent token copy after proving that independent
recovery path.

### Kubernetes or OpenShift shape

The bundle starts on the Cairn PVC because that is where the local command can
write it. Export the completed directory immediately to the organisation's
encrypted, access-controlled backup sink; a CSI snapshot may supplement this
bundle but never replace it.

```sh
namespace=cairn-example

backup_json="$(
  kubectl exec -n "$namespace" pod/cairn-0 -c cairn -- \
    cairn backup --config /etc/cairn/config.yaml \
      --output /var/lib/cairn/backups
)"
printf '%s\n' "$backup_json" | jq .
bundle="$(printf '%s\n' "$backup_json" | jq -r '.bundle')"
test "$bundle" != null

sink_stage="$(mktemp -d)"
kubectl cp -n "$namespace" -c cairn \
  "cairn-0:$bundle" "$sink_stage/bundle"
manifest="$sink_stage/bundle/manifest.json"
jq -e '.schema_version == "cairn.backup/v1" and
       (.members | type == "array" and length > 0)' "$manifest" >/dev/null

verified=0
while IFS=$'\t' read -r member bytes digest; do
  case "$member" in
    catalogue.sqlite3|attic.sqlite3) ;;
    *) printf 'unexpected bundle member: %s\n' "$member" >&2; exit 1 ;;
  esac
  member_path="$sink_stage/bundle/$member"
  test -f "$member_path"
  test "$(wc -c <"$member_path" | tr -d ' ')" = "$bytes"
  test "$(sha256sum "$member_path" | awk '{print $1}')" = "$digest"
  verified=$((verified + 1))
done < <(jq -r '.members[] | [.name, (.bytes | tostring), .sha256] | @tsv' \
  "$manifest")
test "$verified" -eq "$(jq '.members | length' "$manifest")"
sha256sum "$manifest"
```

Use `oc` in place of `kubectl` on an OpenShift target. The procedure is a
command contract, not evidence that it has run there.

Transfer `$sink_stage/bundle` as a directory to the chosen sink using its
approved encryption, retention and immutability controls. Record the Cairn
revision, target, namespace, emitted `instance_id`, `barrier_ms`, bundle
location and manifest digest in the operator's backup record. Remove the PVC
copy only after the sink copy and a restore rehearsal have been verified under
the organisation's policy.

### Docker Compose

Use the canonical
[Compose backup procedure](../../deploy/compose/README.md#backup-and-restore).
It creates a host `backups/` directory owned by container UID 65532, mounts it
at `/backups`, and runs the same CLI command while Cairn serves. Export the
generated bundle directory to the selected sink and record the same metadata.

Do not run `docker compose down -v`; `-v` deletes the named data volume and is
not a backup command. A copy of the SQLite file made with `cp`, `tar` or a
volume snapshot alone is not an application-consistent Cairn bundle.

## Restore into a replacement

The sequence is invariant across targets:

1. Obtain the complete bundle from the sink and inspect its `manifest.json`.
   The configured replacement must use the same `instance_id`.
2. Quiesce the original and wait for complete termination. Do not allow any
   Route, Ingress, Service selector, reverse proxy or Compose publication to
   point to a replacement yet.
3. Create a fresh empty Cairn data volume. When retrieval is enabled, use a
   fresh FalkorDB volume or the native procedure's complete clear-and-rebuild
   of that derived index. Keep the original authoritative volume retained and
   untouched.
4. Start a non-serving recovery container or Pod with the reviewed Cairn
   image, replacement config, credentials, empty data volume and bundle
   staging volume. It must not expose the Cairn Service port.
5. Copy the bundle into the staging volume and run:

   ```sh
   cairn restore --config /etc/cairn/config.yaml \
     --bundle /backup/bundle
   ```

6. Require exit 0 and JSON `status: ok`. The command validates the manifest,
   identity, member sizes and digests; installs the members; runs catalogue,
   migration, audit, reconciliation and optional Attic integrity checks; and
   reports the verified audit boundary. If semantic verification refuses the
   catalogue, restore retains the installed bytes for diagnosis. Those files
   are not successful restore admission; keep the replacement out of service.
7. If retrieval is enabled, start the fresh FalkorDB only, then run in the
   still non-serving recovery container:

   ```sh
   cairn rebuild-index --config /etc/cairn/config.yaml
   ```

   Require exit 0 and JSON `status: ok`. A `partial` result exits 4 and is not
   an accepted rebuild.
8. Stop and remove the recovery container or Pod. Start one replacement Cairn
   server against the restored volume. Wait for readiness, then verify the
   expected instance identity, catalogue integrity and authenticated scoped
   REST and MCP reads for known pre-backup data.
9. Move traffic only after those checks pass. Retain the quiesced original
   until the operator explicitly accepts the replacement.

### Native systemd user service

This recipe restores the native installation for the same Linux user and fixed
home-directory layout. `recovery_dir` is the complete directory retrieved from
the protected sink. `known_mutation_id` is the mutation UUID recorded from a
successful ingest before the backup; checking it distinguishes recovery of
known application data from mere process health. Replace both assignment
values before running the block. Run all restore blocks in the same
shell; later blocks reuse the validated paths and identities established by
the first.

First validate the recovery material, stop the service and preserve any current
data directory as the rollback point:

```sh
set -eu
umask 077

recovery_dir=/absolute/path/to/recovery.XXXXXXXX
known_mutation_id=00000000-0000-4000-8000-000000000000
runtime_dir="$HOME/.local/share/cairn/runtime"
state_dir="$HOME/.local/share/cairn"
config_file="$HOME/.config/cairn/config.yaml"
credential_file="$HOME/.local/share/cairn/credentials/local-operator.token"
data_dir="$HOME/.local/share/cairn/data"

test -d "$recovery_dir"
test "$known_mutation_id" != 00000000-0000-4000-8000-000000000000
test -f "$recovery_dir/config.yaml"
test -f "$recovery_dir/local-operator.token"
test "$(stat -c '%a' "$recovery_dir/local-operator.token")" = 600

bundle_count="$(find "$recovery_dir" -mindepth 1 -maxdepth 1 -type d \
  -name 'cairn-backup-*' -printf '.\n' | wc -l)"
test "$bundle_count" -eq 1
bundle="$(find "$recovery_dir" -mindepth 1 -maxdepth 1 -type d \
  -name 'cairn-backup-*' -print -quit)"
expected_instance="$(jq -er '.instance_id' "$bundle/manifest.json")"
snapshot_instance="$(awk '$1 == "instance_id:" {print $2}' \
  "$recovery_dir/config.yaml")"
test "$snapshot_instance" = "$expected_instance"

systemctl --user stop cairn.service
! systemctl --user is-active --quiet cairn.service

rollback_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
rollback_material_dir="$state_dir/recovery-rollback.$rollback_stamp"
install -d -m 0700 "$rollback_material_dir"
if [ -f "$config_file" ]; then
  install -m 0600 "$config_file" "$rollback_material_dir/config.yaml"
fi
for credential_name in local-operator.token falkordb-password openai-api-key; do
  current_credential="$(dirname "$credential_file")/$credential_name"
  if [ -f "$current_credential" ]; then
    install -m 0600 "$current_credential" \
      "$rollback_material_dir/$credential_name"
  fi
done
install -d -m 0700 "$(dirname "$config_file")" "$(dirname "$credential_file")"
install -m 0600 "$recovery_dir/config.yaml" "$config_file"
install -m 0600 "$recovery_dir/local-operator.token" "$credential_file"
for adapter_credential in falkordb-password openai-api-key; do
  if [ -f "$recovery_dir/$adapter_credential" ]; then
    install -m 0600 "$recovery_dir/$adapter_credential" \
      "$(dirname "$credential_file")/$adapter_credential"
  fi
done
"$runtime_dir/bin/cairn" check-config --config "$config_file" |
  jq -e --arg expected "$expected_instance" \
    '.status == "ok" and .instance_id == $expected'

rollback_dir=
if [ -e "$data_dir" ]; then
  rollback_dir="$state_dir/data.rollback.$rollback_stamp"
  test ! -e "$rollback_dir"
  mv -- "$data_dir" "$rollback_dir"
fi
install -d -m 0700 "$data_dir"
printf 'Rollback directory: %s\n' "${rollback_dir:-none}"
printf 'Rollback configuration and credential: %s\n' "$rollback_material_dir"
```

The service is now stopped and the restore target is empty. Keep
`rollback_dir` untouched. Restore and perform offline verification:

```sh
set -eu

restore_json="$(
  "$runtime_dir/bin/cairn" restore --config "$config_file" --bundle "$bundle"
)"
printf '%s\n' "$restore_json" | jq .
printf '%s\n' "$restore_json" |
  jq -e --arg expected "$expected_instance" \
    '.status == "ok" and .operation == "restore" and
     .instance_id == $expected' >/dev/null

verify_json="$("$runtime_dir/bin/cairn" verify --config "$config_file")"
printf '%s\n' "$verify_json" | jq .
printf '%s\n' "$verify_json" |
  jq -e --arg expected "$expected_instance" \
    '.status == "ok" and .operation == "verify" and
     .instance_id == $expected' >/dev/null
```

If either command fails, do not start the candidate. Preserve the failed data
directory and command output for diagnosis. The [failure and rollback](#failure-and-rollback)
rules apply; when the error says `delete_data_directory_and_retry`, it means
this new target only, never the retained rollback directory.

If `graphiti.enabled` is true, the derived index must match the restored
catalogue before Cairn serves. If the named index container and volumes were
also lost, recreate only FalkorDB using the volume-init and start blocks in the
[native semantic procedure](native-installation.md#optional-semantic-retrieval)
with the recovered `falkordb-password`; do not generate replacement
credentials. Then run this complete clear-and-rebuild while Cairn remains
stopped:

```sh
graphiti_enabled="$(
  "$runtime_dir/bin/python" - "$config_file" <<'PY'
from pathlib import Path
import sys
import yaml

document = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if document["graphiti"]["enabled"] is True else "false")
PY
)"
if [ "$graphiti_enabled" = true ]; then
  host_uid="$(id -u)"
  index_name="cairn-native-$host_uid-falkordb"
  if [ "$(docker inspect -f '{{.State.Running}}' "$index_name")" != true ]; then
    docker start "$index_name" >/dev/null
  fi
  attempt=0
  until docker exec "$index_name" sh -c \
    '{ sed -n "s/^requirepass /AUTH /p" /etc/falkordb/cairn.conf; echo PING; } | redis-cli -h 127.0.0.1 -p 6379' |
    grep -q '^PONG$'
  do
    attempt=$((attempt + 1))
    test "$attempt" -lt 60
    sleep 2
  done
  rebuild_json="$(
    "$runtime_dir/bin/cairn" rebuild-index --config "$config_file"
  )"
  printf '%s\n' "$rebuild_json" | jq .
  printf '%s\n' "$rebuild_json" |
    jq -e '.status == "ok" and .failed == 0 and .unreadable == 0' >/dev/null
fi
```

`rebuild-index` clears the whole FalkorDB index before projecting the restored
catalogue. A failure or `partial` result leaves Cairn stopped. The original
catalogue remains at `rollback_dir`; returning to it also requires rebuilding
the derived index before that original serves.

Start the restored service and verify health, identity, the retained credential
and the known pre-backup mutation through an authenticated audit read:

```sh
set -eu
umask 077

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

curl_config="$(mktemp)"
audit_request="$(mktemp)"
audit_response="$(mktemp)"
trap 'rm -f "$curl_config" "$audit_request" "$audit_response"' EXIT HUP INT TERM
{
  printf 'header = "Authorization: Bearer '
  tr -d '\r\n' <"$credential_file"
  printf '"\n'
} >"$curl_config"
chmod 0600 "$curl_config"

curl --disable --silent --show-error --fail-with-body \
  --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
  --config "$curl_config" \
  http://127.0.0.1:8000/v1/instance |
  jq -e --arg expected "$expected_instance" '.instance_id == $expected'

audit_after=0
audit_page=0
mutation_found=0
while [ "$audit_page" -lt 100 ]; do
  audit_page=$((audit_page + 1))
  jq -n --argjson after "$audit_after" \
    '{realm_id: "local", scope_prefix: [], after_sequence: $after, limit: 500}' \
    >"$audit_request"
  curl --disable --silent --show-error --fail-with-body \
    --noproxy '*' --proto '=http' --max-redirs 0 --max-time 5 \
    --request POST --config "$curl_config" \
    --header 'Content-Type: application/json' \
    --data-binary "@$audit_request" \
    --output "$audit_response" \
    http://127.0.0.1:8000/v1/read-audit-events
  if jq -e --arg mutation_id "$known_mutation_id" \
    '.events | any(.mutation_id == $mutation_id)' "$audit_response" >/dev/null
  then
    mutation_found=1
    break
  fi
  next_after="$(jq -er '.next_after_sequence // empty' "$audit_response")" || break
  test "$next_after" -gt "$audit_after"
  audit_after="$next_after"
done
test "$mutation_found" -eq 1
printf 'Recovered mutation found after %s audit page(s).\n' "$audit_page"
```

The final `true` proves that the restored catalogue contains the named
pre-backup mutation and the retained credential can read its audit scope. It
does not prove semantic retrieval, every fact, production readiness or the
external sink's future availability. Complete any additional known-data reads
required by the recovery objective before accepting the restored directory.

The rebuild block above is mandatory when Graphiti is enabled. When it is
disabled, the audit check proves restored custody without claiming semantic
search. The [Compose recovery procedure](#docker-compose-mechanics) remains the
full-container alternative.

After acceptance, retain or remove `rollback_dir` and `rollback_material_dir`
according to the site's retention and incident policy. Both may contain
sensitive data.

If validation fails and `rollback_dir` exists, return to the original without
overwriting either copy:

```sh
set -eu

systemctl --user stop cairn.service
! systemctl --user is-active --quiet cairn.service
failed_dir="$state_dir/data.failed.$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$failed_dir"
mv -- "$data_dir" "$failed_dir"
mv -- "$rollback_dir" "$data_dir"
if [ -f "$rollback_material_dir/config.yaml" ]; then
  install -m 0600 "$rollback_material_dir/config.yaml" "$config_file"
fi
for credential_name in local-operator.token falkordb-password openai-api-key; do
  if [ -f "$rollback_material_dir/$credential_name" ]; then
    install -m 0600 "$rollback_material_dir/$credential_name" \
      "$(dirname "$credential_file")/$credential_name"
  fi
done
graphiti_enabled="$(
  "$runtime_dir/bin/python" - "$config_file" <<'PY'
from pathlib import Path
import sys
import yaml

document = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if document["graphiti"]["enabled"] is True else "false")
PY
)"
if [ "$graphiti_enabled" = true ]; then
  rebuild_json="$(
    "$runtime_dir/bin/cairn" rebuild-index --config "$config_file"
  )"
  printf '%s\n' "$rebuild_json" |
    jq -e '.status == "ok" and .failed == 0 and .unreadable == 0' >/dev/null
fi
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
printf 'Failed restored candidate retained at: %s\n' "$failed_dir"
```

Repeat the authenticated identity and known-data checks against the original.
Do not overwrite either data directory in place.

### Kubernetes or OpenShift mechanics

The following mechanics are portable. Replace every `REPLACE_*` value from the
reviewed site render. In particular, use its image digest, CSI storage class,
namespace, instance label and any target-specific Pod security additions.
`ReadWriteOncePod` is supported only by CSI volumes. Do not copy a
vanilla-Kubernetes UID or `fsGroup` into OpenShift; its SCC assigns identity.

First quiesce the original and wait for its Pods to disappear. Stop FalkorDB
too when retrieval is enabled, so the old and rebuilt indexes cannot overlap:

```sh
namespace=cairn-example
kubectl scale -n "$namespace" statefulset/cairn --replicas=0
kubectl wait -n "$namespace" --for=delete pod/cairn-0 --timeout=120s
kubectl scale -n "$namespace" statefulset/falkordb --replicas=0  # retrieval only
kubectl wait -n "$namespace" --for=delete pod/falkordb-0 --timeout=120s  # retrieval only
```

Keep the stable Service off the candidate until verification. This extra
selector intentionally matches neither the stopped original nor the candidate:

```sh
kubectl patch -n "$namespace" service/cairn --type=merge \
  -p '{"spec":{"selector":{"cairn.example.invalid/recovery":"original"}}}'
test -z "$(kubectl get -n "$namespace" endpoints/cairn \
  -o jsonpath='{.subsets[*].addresses[*].ip}')"
```

Copy the original configuration, retaining its `instance_id`. For retrieval,
change only `graphiti.host` to `falkordb-candidate`. Apply the result as a new
ConfigMap and review the diff against the original:

```sh
candidate_config="$(mktemp)"
kubectl get -n "$namespace" configmap/cairn-config \
  -o jsonpath='{.data.config\.yaml}' > "$candidate_config"
$EDITOR "$candidate_config"  # retrieval only: graphiti.host: falkordb-candidate
diff -u <(kubectl get -n "$namespace" configmap/cairn-config \
  -o jsonpath='{.data.config\.yaml}') "$candidate_config" || true
kubectl create configmap cairn-candidate-config -n "$namespace" \
  --from-file="config.yaml=$candidate_config" --dry-run=client -o yaml |
  kubectl apply -f -
```

Save this as `cairn-recovery.yaml`, replace its site values and merge the
target-specific Pod `securityContext` additions from the site render into both
Pod specs. It creates the fresh CSI claim and a non-serving recovery Pod; the
Pod has no Service and therefore cannot receive application traffic. When
retrieval is enabled, also copy the site's accepted non-secret `HTTPS_PROXY`
value into the marked placeholder so `rebuild-index` can reach its provider
through the site's egress path under default-deny. Omit the entire `env` block
when retrieval is disabled:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cairn-candidate-data
  namespace: REPLACE_WITH_INSTANCE_NAMESPACE
spec:
  accessModes:
    - ReadWriteOncePod
  volumeMode: Filesystem
  storageClassName: REPLACE_WITH_CSI_STORAGE_CLASS
  resources:
    requests:
      storage: REPLACE_WITH_CAIRN_CAPACITY
---
apiVersion: v1
kind: Pod
metadata:
  name: cairn-restore
  namespace: REPLACE_WITH_INSTANCE_NAMESPACE
  labels:
    app.kubernetes.io/name: cairn
    app.kubernetes.io/instance: REPLACE_WITH_INSTANCE_NAME
    cairn.example.invalid/recovery: candidate
spec:
  restartPolicy: Never
  serviceAccountName: cairn
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: recovery
      image: REPLACE_WITH_REVIEWED_CAIRN_IMAGE
      command:
        - python
      args:
        - -c
        - "import time; time.sleep(86400)"
      # Retrieval only: copy the exact non-secret value from the accepted site
      # render. Omit this env block when retrieval is disabled.
      env:
        - name: HTTPS_PROXY
          value: REPLACE_WITH_SITE_EGRESS_PROXY_URL
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop:
            - ALL
      volumeMounts:
        - name: data
          mountPath: /var/lib/cairn
        - name: bundle
          mountPath: /backup
        - name: config
          mountPath: /etc/cairn
          readOnly: true
        - name: credentials
          mountPath: /var/run/secrets/cairn
          readOnly: true
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: cairn-candidate-data
    - name: bundle
      emptyDir: {}
    - name: config
      configMap:
        name: cairn-candidate-config
    - name: credentials
      secret:
        secretName: cairn-credentials
        optional: true
        defaultMode: 0440
    - name: tmp
      emptyDir:
        medium: Memory
        sizeLimit: 256Mi
```

Apply it and prove the claim is empty before copying anything:

```sh
kubectl apply --dry-run=server -f cairn-recovery.yaml
kubectl apply -f cairn-recovery.yaml
kubectl wait -n "$namespace" --for=condition=Ready \
  pod/cairn-restore --timeout=180s
kubectl exec -n "$namespace" pod/cairn-restore -c recovery -- python -c \
  "import os,sys; sys.exit(0 if set(os.listdir('/var/lib/cairn')) <= {'lost+found'} else 1)"
```

When retrieval is enabled, build a fresh index from the pinned FalkorDB
component rather than hand-copying a target workload. Save this
`kustomization.yaml` in an operator-owned `recovery-index/` directory. Point
`components` at the pinned, vendored Cairn source, replace the site values and
add the same target-specific FalkorDB security patch used by the site's
production overlay. The suffix creates a distinct Service, StatefulSet and
fresh `data-falkordb-candidate-0` claim:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: REPLACE_WITH_INSTANCE_NAMESPACE
components:
  - REPLACE_WITH_PINNED_CAIRN_SOURCE/deploy/kustomize/components/falkordb
nameSuffix: -candidate
labels:
  - pairs:
      app.kubernetes.io/instance: REPLACE_WITH_INSTANCE_NAME
      cairn.example.invalid/recovery: candidate
    includeSelectors: true
patches:
  - target:
      kind: StatefulSet
      name: falkordb
    patch: |-
      - op: add
        path: /spec/volumeClaimTemplates/0/spec/storageClassName
        value: REPLACE_WITH_CSI_STORAGE_CLASS
      - op: replace
        path: /spec/volumeClaimTemplates/0/spec/resources/requests/storage
        value: REPLACE_WITH_FALKORDB_CAPACITY
```

The portable component deliberately fixes no UID. A vanilla-Kubernetes site
must add its already validated `runAsUser`, `runAsGroup`, `fsGroup` and
`fsGroupChangePolicy` patch; OpenShift must retain its SCC-assigned identity.
Render, server-dry-run, apply and wait:

```sh
kubectl kustomize recovery-index > falkordb-candidate.yaml
kubectl apply --dry-run=server -n "$namespace" -f falkordb-candidate.yaml
kubectl apply -n "$namespace" -f falkordb-candidate.yaml
kubectl rollout status -n "$namespace" \
  statefulset/falkordb-candidate --timeout=420s
```

Now copy the sink bundle, restore and verify. Compare the command's verified
audit boundary with the manifest before proceeding:

```sh
sink_bundle=/path/to/cairn-backup-instance-timestamp
kubectl cp -n "$namespace" -c recovery \
  "$sink_bundle" cairn-restore:/backup/bundle

restore_log="$(kubectl exec -n "$namespace" pod/cairn-restore -c recovery -- \
  cairn restore --config /etc/cairn/config.yaml --bundle /backup/bundle)"
restore_json="$(printf '%s\n' "$restore_log" |
  jq -Rrc 'fromjson? | select(.operation == "restore")' | tail -n 1)"
jq -e '.status == "ok"' <<<"$restore_json"
test "$(jq -c '.audit_boundary' <<<"$restore_json")" = \
  "$(jq -c '.audit_boundary' "$sink_bundle/manifest.json")"

verify_log="$(kubectl exec -n "$namespace" pod/cairn-restore -c recovery -- \
  cairn verify --config /etc/cairn/config.yaml)"
verify_json="$(printf '%s\n' "$verify_log" |
  jq -Rrc 'fromjson? | select(.operation == "verify")' | tail -n 1)"
jq -e '.status == "ok"' <<<"$verify_json"
```

For retrieval, rebuild against the fresh candidate FalkorDB and require a
complete result:

```sh
rebuild_log="$(kubectl exec -n "$namespace" pod/cairn-restore -c recovery -- \
  cairn rebuild-index --config /etc/cairn/config.yaml)"
rebuild_json="$(printf '%s\n' "$rebuild_log" |
  jq -Rrc 'fromjson? | select(.projected != null)' | tail -n 1)"
jq -e '.status == "ok" and .failed == 0 and .unreadable == 0' \
  <<<"$rebuild_json"
```

Delete the recovery Pod before starting the candidate server; the PVC permits
one Pod only:

```sh
kubectl delete -n "$namespace" pod/cairn-restore --wait=true
```

Save the following as `cairn-candidate.yaml` and replace its site values. Copy
the original site render's non-secret provider proxy setting when retrieval is
enabled, and merge the same target-specific Pod security additions used by the
site's accepted Cairn StatefulSet. This is a one-replica StatefulSet using the
fresh restored claim, plus a separate pre-cutover Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: cairn-candidate
  namespace: REPLACE_WITH_INSTANCE_NAMESPACE
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: cairn
    app.kubernetes.io/instance: REPLACE_WITH_INSTANCE_NAME
    cairn.example.invalid/recovery: candidate
  ports:
    - name: http
      port: 8000
      targetPort: http
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cairn-candidate
  namespace: REPLACE_WITH_INSTANCE_NAMESPACE
spec:
  replicas: 1
  serviceName: cairn-candidate
  selector:
    matchLabels:
      app.kubernetes.io/name: cairn
      app.kubernetes.io/instance: REPLACE_WITH_INSTANCE_NAME
      cairn.example.invalid/recovery: candidate
  template:
    metadata:
      labels:
        app.kubernetes.io/name: cairn
        app.kubernetes.io/instance: REPLACE_WITH_INSTANCE_NAME
        cairn.example.invalid/recovery: candidate
    spec:
      serviceAccountName: cairn
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 60
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: migrate
          image: REPLACE_WITH_REVIEWED_CAIRN_IMAGE
          args:
            - migrate
            - --config
            - /etc/cairn/config.yaml
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: data
              mountPath: /var/lib/cairn
            - name: config
              mountPath: /etc/cairn
              readOnly: true
            - name: tmp
              mountPath: /tmp
      containers:
        - name: cairn
          image: REPLACE_WITH_REVIEWED_CAIRN_IMAGE
          args:
            - serve
            - --config
            - /etc/cairn/config.yaml
          ports:
            - name: http
              containerPort: 8000
          startupProbe:
            httpGet:
              path: /health/startup
              port: http
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet:
              path: /health/ready
              port: http
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health/live
              port: http
            periodSeconds: 20
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: data
              mountPath: /var/lib/cairn
            - name: config
              mountPath: /etc/cairn
              readOnly: true
            - name: credentials
              mountPath: /var/run/secrets/cairn
              readOnly: true
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: cairn-candidate-data
        - name: config
          configMap:
            name: cairn-candidate-config
        - name: credentials
          secret:
            secretName: cairn-credentials
            optional: true
            defaultMode: 0440
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 256Mi
```

Render this site artefact before applying if it has target patches. Then
server-dry-run, apply and wait:

```sh
kubectl apply --dry-run=server -f cairn-candidate.yaml
kubectl apply -f cairn-candidate.yaml
kubectl rollout status -n "$namespace" \
  statefulset/cairn-candidate --timeout=420s
```

The candidate's pre-cutover endpoint is `cairn-candidate:8000` inside the
namespace. Port-forward it without changing the stable Service:

```sh
kubectl port-forward -n "$namespace" service/cairn-candidate 18080:8000
```

Against `http://127.0.0.1:18080`, require `/health/ready`, the expected
`instance_id`, catalogue verification, and authenticated scoped REST **and**
MCP reads of known pre-backup data. Use the operator's approved client which
reads its bearer token from a file; do not put the token on argv. Record those
results before cutover.

Only after verification, move the stable Service to the candidate and confirm
its endpoint is the candidate Pod:

```sh
candidate_ip="$(kubectl get -n "$namespace" pod \
  -l 'app.kubernetes.io/name=cairn,cairn.example.invalid/recovery=candidate' \
  -o jsonpath='{.items[0].status.podIP}')"
kubectl patch -n "$namespace" service/cairn --type=merge \
  -p '{"spec":{"selector":{"cairn.example.invalid/recovery":"candidate"}}}'
test "$(kubectl get -n "$namespace" endpoints/cairn \
  -o jsonpath='{.subsets[0].addresses[0].ip}')" = "$candidate_ip"
```

Retain the original StatefulSet, ConfigMap and PVC at zero replicas until the
operator accepts the replacement. Do not delete or rewrite them during
cutover. On OpenShift, also prove restricted SCC, storage, network-policy and
Route behaviour; the vanilla-Kubernetes evidence does not establish those
properties.

### Docker Compose mechanics

Use the command shape in the canonical
[Compose restore procedure](../../deploy/compose/README.md#backup-and-restore),
but create a fresh candidate project so the original project's volumes remain
an unambiguous rollback point. Restore into its fresh data volume, rebuild its
fresh derived index when enabled, then start and validate it. Do not use the
same-project volume-removal variant when the original is the rollback
candidate. Publication remains on the original until the replacement checks
pass.

## Failure and rollback

Before the restore lease is acquired, a refusal leaves the empty target
untouched. After the lease is acquired, failures may leave staged or installed
files for diagnosis. When error JSON says
`recovery: delete_data_directory_and_retry`, delete only the identified
replacement volume, recreate it empty, and repeat from the sink bundle. Never
clean or overwrite the original volume.

If replacement validation fails before or after cutover, the quiesced original
cannot receive traffic. Stop the candidate first, restart the original and its
original index, and verify them through a direct pre-traffic endpoint:

```sh
kubectl scale -n "$namespace" statefulset/cairn-candidate --replicas=0
kubectl wait -n "$namespace" --for=delete pod/cairn-candidate-0 --timeout=120s
kubectl scale -n "$namespace" statefulset/falkordb-candidate --replicas=0  # retrieval only
kubectl scale -n "$namespace" statefulset/falkordb --replicas=1  # retrieval only
kubectl rollout status -n "$namespace" statefulset/falkordb --timeout=420s  # retrieval only
kubectl scale -n "$namespace" statefulset/cairn --replicas=1
kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
kubectl port-forward -n "$namespace" pod/cairn-0 18081:8000
```

Against `http://127.0.0.1:18081`, require readiness, the expected instance
identity, and authenticated scoped REST and MCP reads. Only after those checks
pass, remove the recovery selector so the stable Service selects the verified
original again:

```sh
original_ip="$(kubectl get -n "$namespace" pod/cairn-0 \
  -o jsonpath='{.status.podIP}')"
kubectl patch -n "$namespace" service/cairn --type=merge \
  -p '{"spec":{"selector":{"cairn.example.invalid/recovery":null}}}'
test "$(kubectl get -n "$namespace" endpoints/cairn \
  -o jsonpath='{.subsets[0].addresses[0].ip}')" = "$original_ip"
```

Traffic returns only after that selector change. Preserve the stopped failed
candidate, its fresh volumes and command output until the incident owner
decides whether they are needed for diagnosis.

Do not run an older binary against a newer catalogue. Cairn v0.1 does not
support downgrade, partial target versions or skipped migrations. A rollback
is restoration of a known bundle or return to the retained original, not an
in-place binary downgrade.
