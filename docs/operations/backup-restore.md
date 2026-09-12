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
3. Create a fresh empty Cairn data volume and, when retrieval is enabled, a
   fresh FalkorDB volume. Keep the original volumes retained and untouched.
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
