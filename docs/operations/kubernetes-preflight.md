# Preflight an existing Kubernetes cluster

This procedure checks whether an existing vanilla-Kubernetes cluster is a
credible Cairn target. It does not create or repair a cluster. Stop on any
failure and give the exact failed check to the cluster or storage
administrator; do not grant yourself broader access or weaken Cairn's render.

The preflight has two phases:

1. read-only inventory before choosing site values; and
2. disposable functional probes after the dedicated namespace and final site
   render have been prepared, but before any Cairn StatefulSet is applied.

Inventory proves only what the API currently reports. The second phase proves
an exact image pull, DNS resolution, one bounded NetworkPolicy path and one
bounded storage path. It is not a complete CNI, CSI, failure-domain or disaster
recovery acceptance suite.

## What the cluster must provide

- At least one Ready, schedulable Linux `amd64` worker without an untolerated
  `NoSchedule` or `NoExecute` taint.
- Working cluster DNS. Cairn's policy selects Service `kube-dns` in namespace
  `kube-system` by Pod label `k8s-app: kube-dns`.
- A CNI which enforces Kubernetes `NetworkPolicy` ingress and egress rules.
  An API object being accepted is not enforcement evidence.
- A CSI driver and StorageClass approved for persistent `Filesystem` volumes,
  `ReadWriteOncePod`, SQLite WAL, reliable POSIX advisory locks and durable
  `fsync`. NFS and other shared or network filesystems are unsupported.
- Worker access to the registry-published Cairn image by its exact digest.
- The namespace-scoped permissions listed below. Read-only node,
  StorageClass, CSIDriver and PersistentVolume inspection may require an
  administrator. Do not replace these narrow permissions with cluster-admin.

The recorded reference run used Kubernetes 1.35.0 and Cilium 1.19.6. It proved
DNS and NetworkPolicy behaviour on one production node; a separate disposable
three-node run covered a cross-node matrix. Its static local storage used
`kubernetes.io/no-provisioner`, which is outside Kubernetes' CSI-only support
statement for `ReadWriteOncePod`. That measured result does not satisfy this
guide's CSI prerequisite on another cluster.

## Phase 1: read-only inventory

Run from the repository root after installing the pinned `kubectl` described
in [Install Cairn](../install.md#kubernetes-installation). Replace all four
values with decisions reviewed for this cluster. `cni_evidence` and
`storage_evidence` are references to administrator-owned records, tickets or
reports; they are not commands and must contain no credentials.

```bash
set -euo pipefail
expected_context='REPLACE_WITH_KUBECTL_CONTEXT'
expected_server_version='REPLACE_WITH_REVIEWED_SERVER_VERSION'
storage_class='REPLACE_WITH_CSI_STORAGE_CLASS'
cni_evidence='REPLACE_WITH_ACCEPTED_CNI_NETWORKPOLICY_EVIDENCE'
storage_evidence='REPLACE_WITH_ACCEPTED_CSI_RWOP_POSIX_EVIDENCE'
case "$expected_context:$expected_server_version:$storage_class:$cni_evidence:$storage_evidence" in
  *REPLACE_WITH_*) printf '%s\n' 'Replace every preflight value first.' >&2; exit 1 ;;
esac

test "$(kubectl config current-context)" = "$expected_context"
client_version="$(kubectl version -o json | jq -er '.clientVersion.gitVersion')"
server_version="$(kubectl version -o json | jq -er '.serverVersion.gitVersion')"
test "$server_version" = "$expected_server_version"
printf 'Client %s; reviewed server %s\n' "$client_version" "$server_version"

for resource in nodes storageclasses.storage.k8s.io \
  csidrivers.storage.k8s.io persistentvolumes; do
  test "$(kubectl auth can-i get "$resource")" = yes
done
test "$(kubectl auth can-i list nodes)" = yes
kubectl api-resources --api-group=networking.k8s.io -o name |
  awk '$0 == "networkpolicies.networking.k8s.io" {found=1} END {exit !found}'
```

Expected: the selected context and exact reviewed server version match, every
authorisation check returns `yes`, and the NetworkPolicy resource exists. API
discovery does not prove that the CNI enforces a policy; retain the named CNI
evidence and run Phase 2.

Inspect Ready workers and retain the eligible names. The filter matches the
unspecialised Cairn Pod: Linux `amd64`, scheduling enabled, Ready, and no
blocking taint. It does not prove free CPU, memory or volume topology.

```bash
nodes_json="$(kubectl get nodes -o json)"
eligible_workers="$(
  jq -er '
    [
      .items[] |
      select(.spec.unschedulable != true) |
      select(.metadata.labels["kubernetes.io/os"] == "linux") |
      select(.metadata.labels["kubernetes.io/arch"] == "amd64") |
      select(any(.status.conditions[];
        .type == "Ready" and .status == "True")) |
      select(all(.spec.taints[]?;
        .effect != "NoSchedule" and .effect != "NoExecute")) |
      [.metadata.name, .metadata.labels["kubernetes.io/hostname"]] |
      select(.[1] | type == "string" and length > 0) |
      @tsv
    ] |
    select(length > 0) |
    .[]
  ' <<<"$nodes_json"
)"
test -n "$eligible_workers"
while IFS=$'\t' read -r node hostname; do
  printf 'Eligible worker: %s (hostname label %s)\n' "$node" "$hostname"
done <<<"$eligible_workers"
```

Expected: at least one worker name. A later pull Pod must become Ready on every
name printed here; that scheduling result is the functional capacity check.

Check the exact DNS selectors used by Cairn and require at least one currently
ready endpoint:

```bash
kubectl get service -n kube-system kube-dns -o json |
  jq -e '
    .metadata.labels["k8s-app"] == "kube-dns" and
    (.spec.clusterIP | type == "string" and . != "" and . != "None")
  ' >/dev/null
kubectl get endpointslices.discovery.k8s.io -n kube-system \
  -l kubernetes.io/service-name=kube-dns -o json |
  jq -e 'any(.items[].endpoints[]?; .conditions.ready != false)' >/dev/null
printf '%s\n' 'DNS Service and a ready endpoint are present.'
```

Expected: the final message. These are inventory observations. Phase 2 resolves
`kubernetes.default.svc` from a real Pod.

Inspect the selected StorageClass and its registered CSI driver:

```bash
storage_json="$(kubectl get storageclass "$storage_class" -o json)"
provisioner="$(jq -er '.provisioner | select(type == "string" and length > 0)' \
  <<<"$storage_json")"
case "$provisioner" in
  kubernetes.io/*)
    printf 'StorageClass uses non-CSI provisioner %s.\n' "$provisioner" >&2
    exit 1
    ;;
esac
binding_mode="$(jq -er '.volumeBindingMode // "Immediate"' <<<"$storage_json")"
case "$binding_mode" in Immediate|WaitForFirstConsumer) ;; *) exit 1 ;; esac
reclaim_policy="$(jq -er '.reclaimPolicy // "Delete"' <<<"$storage_json")"
case "$reclaim_policy" in Delete|Retain) ;; *) exit 1 ;; esac
kubectl get csidriver "$provisioner" -o json |
  jq -e '
    ((.spec.volumeLifecycleModes // ["Persistent"]) | index("Persistent")) != null
  ' >/dev/null
printf 'CSI provisioner %s; binding %s; reclaim %s\n' \
  "$provisioner" "$binding_mode" "$reclaim_policy"
```

Expected: a non-`kubernetes.io/*` provisioner with persistent-volume support.
The Kubernetes API does not declare filesystem durability, lock correctness or
the CSI sidecar versions needed by this driver for `ReadWriteOncePod`. The
storage administrator must confirm those facts in `storage_evidence`, including
supported worker topology and capacity. Phase 2 then exercises the requested
access mode, SQLite WAL, a POSIX lock and `fsync` on one disposable claim.

Record the context, server version, eligible nodes, CNI evidence, StorageClass,
CSI provisioner, reclaim policy and storage evidence with the site review. Do
not treat the historical reference versions above as evidence for this target.

Continue with [the complete site overlay](kubernetes-site.md), then prepare the
dedicated namespace as directed in
[deployment](deployment.md#namespace-and-render-preparation). Return here before
applying Cairn.

## Phase 2: disposable functional probes

The namespace must exist, carry its reviewed instance label and contain no
Cairn StatefulSet or data claim. The exact Cairn image must already be published
to a registry. These probes create short-lived Pods, two NetworkPolicies and
one PVC. They do not use provider credentials or start Cairn.

The storage probe accepts either standard reclaim policy. `Delete` should
remove the disposable volume after its claim is deleted. `Retain` deliberately
leaves the released probe volume for the storage administrator's reviewed
backend cleanup; this procedure does not patch or delete that cluster-scoped
object.

Set the same values used by the final site render. `pull_secret` is empty for a
public registry or the name of an existing image-pull Secret in this namespace.
Choose a unique DNS-label `probe_run`; the cleanup selector is bound to it.

```bash
set -euo pipefail
namespace='REPLACE_WITH_INSTANCE_NAMESPACE'
instance_name="$namespace"
cairn_image='REPLACE_WITH_REGISTRY/REPLACE_WITH_REPOSITORY/cairn@sha256:REPLACE_WITH_DIGEST'
storage_class='REPLACE_WITH_CSI_STORAGE_CLASS'
pull_secret=''
probe_run='REPLACE_WITH_UNIQUE_PROBE_RUN'
probe_size='1Gi'
case "$namespace:$cairn_image:$storage_class:$probe_run" in
  *REPLACE_WITH_*) printf '%s\n' 'Replace every probe value first.' >&2; exit 1 ;;
esac
[[ "$cairn_image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]
[[ "$probe_run" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]

kubectl get namespace "$namespace" -o json |
  jq -e --arg instance "$instance_name" \
    '.metadata.labels["cairn.example.invalid/instance"] == $instance' >/dev/null
require_absent() {
  if ! existing="$(
    kubectl get "$1" -n "$namespace" --ignore-not-found -o name
  )"; then
    printf 'Could not check managed name: %s\n' "$1" >&2
    exit 1
  fi
  if test -n "$existing"; then
    printf 'Refusing existing managed name: %s\n' "$existing" >&2
    exit 1
  fi
}
require_absent statefulset/cairn
require_absent pvc/data-cairn-0
for object in pod/cairn-preflight-target pod/cairn-preflight-prober \
  pod/cairn-preflight-storage networkpolicy/cairn-preflight-deny-ingress \
  networkpolicy/cairn-preflight-deny-egress pvc/cairn-preflight-data; do
  require_absent "$object"
done
reclaim_policy="$(kubectl get storageclass "$storage_class" \
  -o jsonpath='{.reclaimPolicy}')"
case "$reclaim_policy" in Delete|Retain) ;; *) exit 1 ;; esac
if test -n "$pull_secret"; then
  pull_type="$(kubectl get secret -n "$namespace" "$pull_secret" -o jsonpath='{.type}')"
  case "$pull_type" in
    kubernetes.io/dockerconfigjson|kubernetes.io/dockercfg) ;;
    *) printf '%s\n' 'pull_secret is not a registry credential Secret.' >&2; exit 1 ;;
  esac
fi

for permission in 'get statefulsets.apps' 'get secrets' \
  'create pods' 'get pods' 'list pods' 'watch pods' \
  'delete pods' 'deletecollection pods' 'create pods/exec' \
  'create networkpolicies.networking.k8s.io' \
  'get networkpolicies.networking.k8s.io' \
  'list networkpolicies.networking.k8s.io' \
  'delete networkpolicies.networking.k8s.io' \
  'deletecollection networkpolicies.networking.k8s.io' \
  'create persistentvolumeclaims' 'get persistentvolumeclaims' \
  'list persistentvolumeclaims' 'delete persistentvolumeclaims' \
  'deletecollection persistentvolumeclaims'; do
  set -- $permission
  test "$(kubectl auth can-i "$1" "$2" -n "$namespace")" = yes
done
probe_selector="cairn.example.invalid/preflight=$probe_run"
if ! existing_labelled="$(kubectl get pod,networkpolicy,pvc -n "$namespace" \
  -l "$probe_selector" -o name)"; then
  printf '%s\n' 'Could not inventory the probe selector.' >&2
  exit 1
fi
test -z "$existing_labelled"
```

Expected: no output after the variable checks. A `no` permission or existing
labelled object is a refusal. Ask the administrator for only the named missing
permission or choose a new `probe_run` after inspecting the collision.

Install cleanup before creating anything. It removes only objects carrying the
unique run label, in dependency order:

```bash
cleanup_preflight() (
  set +e
  if test -z "${probe_pv:-}"; then
    probe_pv="$(kubectl get pvc/cairn-preflight-data -n "$namespace" \
      -o jsonpath='{.spec.volumeName}' 2>/dev/null)"
  fi
  kubectl delete networkpolicy -n "$namespace" -l "$probe_selector" \
    --ignore-not-found --wait=true
  kubectl delete pod -n "$namespace" -l "$probe_selector" \
    --ignore-not-found --wait=true
  kubectl delete pvc -n "$namespace" -l "$probe_selector" \
    --ignore-not-found --wait=true
  if test "${reclaim_policy:-}" = Retain && test -n "${probe_pv:-}"; then
    printf 'ACTION: storage administrator must dispose of retained probe PV %s.\n' \
      "$probe_pv" >&2
  fi
)
trap cleanup_preflight EXIT HUP INT TERM
```

### Pull the exact image and resolve DNS

Recompute eligible workers so the observation is current. Create one Pod per
eligible worker with `imagePullPolicy: Always`. Kubernetes must resolve the
exact digest at the registry for every Pod start; success is not merely a
node-local tag-cache check.

```bash
eligible_workers="$(
  kubectl get nodes -o json |
  jq -er '
    [
      .items[] |
      select(.spec.unschedulable != true) |
      select(.metadata.labels["kubernetes.io/os"] == "linux") |
      select(.metadata.labels["kubernetes.io/arch"] == "amd64") |
      select(any(.status.conditions[];
        .type == "Ready" and .status == "True")) |
      select(all(.spec.taints[]?;
        .effect != "NoSchedule" and .effect != "NoExecute")) |
      [.metadata.name, .metadata.labels["kubernetes.io/hostname"]] |
      select(.[1] | type == "string" and length > 0) |
      @tsv
    ] |
    select(length > 0) |
    .[]
  '
)"
index=0
first_pull_pod=''
while IFS=$'\t' read -r node hostname; do
  pod="cairn-preflight-pull-$index"
  test -n "$first_pull_pod" || first_pull_pod="$pod"
  require_absent "pod/$pod"
  jq -nc --arg pod "$pod" --arg namespace "$namespace" \
    --arg run "$probe_run" --arg hostname "$hostname" --arg pull "$pull_secret" \
    --arg image "$cairn_image" '
    {
      apiVersion: "v1", kind: "Pod",
      metadata: {
        name: $pod, namespace: $namespace,
        labels: {"cairn.example.invalid/preflight": $run}
      },
      spec: {
        restartPolicy: "Never",
        nodeSelector: {"kubernetes.io/hostname": $hostname},
        securityContext: {
          runAsNonRoot: true, seccompProfile: {type: "RuntimeDefault"}
        },
        containers: [{
          name: "probe", image: $image, imagePullPolicy: "Always",
          command: ["/bin/sh", "-c"],
          args: ["while :; do sleep 5; done"],
          securityContext: {
            allowPrivilegeEscalation: false,
            readOnlyRootFilesystem: true,
            capabilities: {drop: ["ALL"]}
          }
        }]
      }
    } |
    if $pull == "" then . else .spec.imagePullSecrets = [{name: $pull}] end
  ' | kubectl create -f -
  kubectl wait pod/"$pod" -n "$namespace" \
    --for=condition=Ready --timeout=300s
  kubectl get pod/"$pod" -n "$namespace" -o json |
    jq -e --arg image "$cairn_image" --arg node "$node" '
      .spec.nodeName == $node and
      .spec.containers[0].image == $image and
      .spec.containers[0].imagePullPolicy == "Always" and
      ([.status.containerStatuses[]?] | length == 1 and .[0].ready == true)
    ' >/dev/null
  index=$((index + 1))
done <<<"$eligible_workers"
test "$index" -gt 0

kubectl exec -n "$namespace" "$first_pull_pod" -- python -c \
  'import socket; answer=socket.getaddrinfo("kubernetes.default.svc", 443); assert answer; print("PASS: cluster DNS resolved")'
```

Expected: every pull Pod becomes Ready on its selected worker and the final
line is `PASS: cluster DNS resolved`. `Always` requires registry resolution but
can reuse verified local image layers. A failure on one node disqualifies that
node from the unspecialised render; do not conceal it with `IfNotPresent`.

### Prove one ingress and egress policy path

Use two eligible workers when available; otherwise both Pods use the only
eligible worker. This proves one current path, not every node pair or CNI
failure mode. Production use still requires the administrator's broader CNI
evidence named in Phase 1.

```bash
readarray -t worker_records <<<"$eligible_workers"
target_record="${worker_records[0]}"
prober_record="${worker_records[1]:-${worker_records[0]}}"
IFS=$'\t' read -r target_node target_hostname <<<"$target_record"
IFS=$'\t' read -r prober_node prober_hostname <<<"$prober_record"

for role in target prober; do
  case "$role" in
    target) hostname="$target_hostname" ;;
    prober) hostname="$prober_hostname" ;;
  esac
  jq -nc --arg role "$role" --arg namespace "$namespace" \
    --arg run "$probe_run" --arg hostname "$hostname" --arg pull "$pull_secret" \
    --arg image "$cairn_image" '
    {
      apiVersion: "v1", kind: "Pod",
      metadata: {
        name: ("cairn-preflight-" + $role), namespace: $namespace,
        labels: {
          "cairn.example.invalid/preflight": $run,
          app: ("cairn-preflight-" + $role)
        }
      },
      spec: {
        restartPolicy: "Never",
        nodeSelector: {"kubernetes.io/hostname": $hostname},
        securityContext: {
          runAsNonRoot: true, seccompProfile: {type: "RuntimeDefault"}
        },
        containers: [{
          name: $role, image: $image, imagePullPolicy: "Always",
          command: (
            if $role == "target"
            then ["python", "-m", "http.server", "8080", "--bind", "0.0.0.0"]
            else ["/bin/sh", "-c"]
            end
          ),
          args: (if $role == "target" then [] else ["while :; do sleep 5; done"] end),
          securityContext: {
            allowPrivilegeEscalation: false,
            readOnlyRootFilesystem: true,
            capabilities: {drop: ["ALL"]}
          }
        }]
      }
    } |
    if $pull == "" then . else .spec.imagePullSecrets = [{name: $pull}] end
  ' | kubectl create -f -
done
kubectl wait pod/cairn-preflight-target pod/cairn-preflight-prober \
  -n "$namespace" --for=condition=Ready --timeout=300s
target_ip="$(kubectl get pod/cairn-preflight-target -n "$namespace" \
  -o jsonpath='{.status.podIP}')"

connection_state() {
  state="$(kubectl exec -i -n "$namespace" cairn-preflight-prober -- \
    python - "$target_ip" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], 8080), timeout=3):
        pass
except OSError:
    print("BLOCKED")
else:
    print("CONNECTED")
PY
  )" || return 1
  case "$state" in CONNECTED|BLOCKED) printf '%s\n' "$state" ;; *) return 1 ;; esac
}
baseline_ready=false
for _ in {1..20}; do
  state="$(connection_state)"
  if test "$state" = CONNECTED; then baseline_ready=true; break; fi
  sleep 1
done
test "$baseline_ready" = true

cat <<EOF | kubectl create -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: cairn-preflight-deny-ingress
  namespace: $namespace
  labels: {cairn.example.invalid/preflight: $probe_run}
spec:
  podSelector: {matchLabels: {app: cairn-preflight-target}}
  policyTypes: [Ingress]
EOF
ingress_blocked=false
for _ in {1..20}; do
  state="$(connection_state)"
  if test "$state" = BLOCKED; then ingress_blocked=true; break; fi
  sleep 1
done
test "$ingress_blocked" = true
kubectl delete networkpolicy/cairn-preflight-deny-ingress -n "$namespace" --wait=true
baseline_restored=false
for _ in {1..20}; do
  state="$(connection_state)"
  if test "$state" = CONNECTED; then baseline_restored=true; break; fi
  sleep 1
done
test "$baseline_restored" = true

cat <<EOF | kubectl create -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: cairn-preflight-deny-egress
  namespace: $namespace
  labels: {cairn.example.invalid/preflight: $probe_run}
spec:
  podSelector: {matchLabels: {app: cairn-preflight-prober}}
  policyTypes: [Egress]
EOF
egress_blocked=false
for _ in {1..20}; do
  state="$(connection_state)"
  if test "$state" = BLOCKED; then egress_blocked=true; break; fi
  sleep 1
done
test "$egress_blocked" = true
kubectl delete networkpolicy/cairn-preflight-deny-egress -n "$namespace" --wait=true
baseline_restored=false
for _ in {1..20}; do
  state="$(connection_state)"
  if test "$state" = CONNECTED; then baseline_restored=true; break; fi
  sleep 1
done
test "$baseline_restored" = true
printf 'PASS: ingress and egress default-deny enforced from %s to %s\n' \
  "$prober_node" "$target_node"
```

Expected: baseline connection succeeds, each policy blocks it within 20
seconds, removing the ingress policy restores the positive control, and the
final line names the tested nodes. A successful connection while either deny
policy is active is a failed preflight.

### Exercise CSI storage and `ReadWriteOncePod`

Create one disposable `Filesystem` claim and a holder using the same image,
pull Secret, storage class and Linux group as the site render:

```bash
jq -nc \
  --arg namespace "$namespace" --arg run "$probe_run" \
  --arg storage "$storage_class" --arg size "$probe_size" \
  --arg image "$cairn_image" --arg pull "$pull_secret" \
  --arg hostname "$target_hostname" '
  {
    apiVersion: "v1", kind: "List", items: [
      {
        apiVersion: "v1", kind: "PersistentVolumeClaim",
        metadata: {
          name: "cairn-preflight-data", namespace: $namespace,
          labels: {"cairn.example.invalid/preflight": $run}
        },
        spec: {
          accessModes: ["ReadWriteOncePod"], volumeMode: "Filesystem",
          storageClassName: $storage, resources: {requests: {storage: $size}}
        }
      },
      {
        apiVersion: "v1", kind: "Pod",
        metadata: {
          name: "cairn-preflight-storage", namespace: $namespace,
          labels: {"cairn.example.invalid/preflight": $run}
        },
        spec: {
          restartPolicy: "Never",
          nodeSelector: {"kubernetes.io/hostname": $hostname},
          securityContext: {
            runAsNonRoot: true, fsGroup: 65532,
            fsGroupChangePolicy: "OnRootMismatch",
            seccompProfile: {type: "RuntimeDefault"}
          },
          containers: [{
            name: "holder", image: $image, imagePullPolicy: "Always",
            command: ["/bin/sh", "-c"],
            args: ["while :; do sleep 5; done"],
            securityContext: {
              allowPrivilegeEscalation: false,
              readOnlyRootFilesystem: true,
              capabilities: {drop: ["ALL"]}
            },
            volumeMounts: [{name: "data", mountPath: "/data"}]
          }],
          volumes: [{
            name: "data",
            persistentVolumeClaim: {claimName: "cairn-preflight-data"}
          }]
        }
      }
    ]
  } |
  if $pull == "" then .
  else .items[1].spec.imagePullSecrets = [{name: $pull}]
  end
' | kubectl create -f -
kubectl wait pod/cairn-preflight-storage -n "$namespace" \
  --for=condition=Ready --timeout=420s
test "$(kubectl get pvc/cairn-preflight-data -n "$namespace" \
  -o jsonpath='{.status.phase}')" = Bound
probe_pv="$(kubectl get pvc/cairn-preflight-data -n "$namespace" \
  -o jsonpath='{.spec.volumeName}')"
test -n "$probe_pv"
test "$(kubectl get persistentvolume "$probe_pv" \
  -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')" = "$reclaim_policy"
```

Expected: the holder is Ready and the PVC is `Bound`. A Pending PVC or Pod is
not evidence of working capacity; inspect its current conditions and Events,
then stop and give them to the storage administrator.

Run a real lock, file and directory `fsync`, and SQLite WAL transaction on the
mounted filesystem:

```bash
kubectl exec -i -n "$namespace" cairn-preflight-storage -- python - <<'PY'
import fcntl
import os
import sqlite3
from pathlib import Path

root = Path("/data")
with (root / "lock-probe").open("a+b") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.write(b"cairn-storage-preflight\n")
    lock.flush()
    os.fsync(lock.fileno())
    database = sqlite3.connect(root / "wal-probe.sqlite3")
    try:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        database.execute("PRAGMA synchronous=FULL")
        database.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        database.execute("INSERT INTO probe VALUES ('committed')")
        database.commit()
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        database.close()
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
print("PASS: POSIX lock, fsync and SQLite WAL transaction")
PY
```

Expected: exactly the final `PASS` line. This bounded exercise cannot prove
durability through node loss or storage-system failure; retain the CSI
administrator's durability evidence.

This positive storage probe requests `ReadWriteOncePod`, but it does not by
itself prove scheduler exclusion or durability through failure. Those remain
requirements of the administrator's `storage_evidence`; do not substitute a
successfully bound PVC for that evidence.

## Cleanup and acceptance boundary

Remove all namespaced probe resources. Then require the CSI reclaim behaviour
reported in Phase 1:

```bash
cleanup_preflight
trap - EXIT HUP INT TERM
leftovers="$(kubectl get pod,networkpolicy,pvc -n "$namespace" \
  -l "$probe_selector" -o name)"
test -z "$leftovers"
if test "$reclaim_policy" = Delete; then
  pv_deleted=false
  for _ in {1..120}; do
    remaining_pv="$(kubectl get persistentvolume "$probe_pv" \
      --ignore-not-found -o name)"
    if test -z "$remaining_pv"; then
      pv_deleted=true
      break
    fi
    sleep 1
  done
  test "$pv_deleted" = true
  printf '%s\n' 'PASS: disposable resources and CSI volume were removed.'
else
  pv_released=false
  for _ in {1..120}; do
    pv_phase="$(kubectl get persistentvolume "$probe_pv" \
      -o jsonpath='{.status.phase}')"
    if test "$pv_phase" = Released; then
      pv_released=true
      break
    fi
    sleep 1
  done
  test "$pv_released" = true
  printf 'ACTION: storage administrator must dispose of retained probe PV %s.\n' \
    "$probe_pv"
fi
```

Expected: no labelled namespaced object remains. With `Delete`, the disposable
PV disappears and the `PASS` line prints. With `Retain`, the exact released PV
name prints as a required administrator action; preflight is not complete until
the administrator confirms its provider-specific backend cleanup. The
namespace and pull Secret remain because cleanup does not select or delete them.

Record the exact site render digest, image digest, tested node names, DNS
result, ingress/egress node pair, PVC/PV names, CSI provisioner, administrator
RWOP evidence and cleanup result. These observations qualify this installation attempt only.
They do not claim OpenShift support, every cross-node path, storage durability
under failure, external ingress, semantic provider access or a fresh-user Cairn
installation. Continue with gateway and credential preparation, then
[First boot](deployment.md#first-boot).
