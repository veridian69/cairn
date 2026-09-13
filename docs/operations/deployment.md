# Deploying Cairn

**Installing for the first time?** Start with [Install Cairn](../install.md) for
purpose-specific prerequisites, [persistent native installation](native-installation.md)
or the [complete Compose procedure](../../deploy/compose/README.md). Kubernetes
is a separate deployment path. Fresh-user beginner acceptance is still pending.

This guide deploys one Cairn v0.1 instance. Read the
[backup and restore runbook](backup-restore.md) first, then use the
[client guide](../clients.md) to verify the server and configure callers.

## Image and contract boundary

`deploy/images.lock` currently sets `CAIRN_IMAGE=cairn:v0.1.0`. This is a
local build tag, not evidence of a published or registry-verified image. Build
from a trusted checkout or supply an operator-controlled immutable image
reference. Record the image digest with the rendered deployment manifest. Do
not claim that a release image exists until one has actually been published.

From the repository root, verify the checked-out contracts before deployment:

```sh
(cd contracts && sha256sum -c cairn-openapi-v1.json.sha256)
(cd contracts && sha256sum -c cairn-mcp-tools-v1.json.sha256)
```

After deployment, compare those values with authenticated
`GET /v1/instance` as described in the
[client guide](../clients.md#check-the-instance). An image reference alone
does not prove which contract an endpoint serves.

## Choose a target

- Use Docker Compose for one instance on one trusted host when container
  networking is an adequate isolation boundary.
- Use conformant Kubernetes when the deployment needs lifecycle management,
  CSI-backed `ReadWriteOncePod` storage and enforced network policy.
- Treat the OpenShift overlay as a starting point until it has passed the
  organisation's own admission, SCC, CNI, CSI and Route checks on a real
  cluster.

The `kind` overlay is a disposable test environment, not a production target.

## Kubernetes and OpenShift

Use the [overlay inventory](../../deploy/kustomize/overlays/README.md):

- `kubernetes` provides Cairn without retrieval;
- `kubernetes-retrieval` is the complete retrieval-enabled Kubernetes
  composition;
- `openshift` is the OpenShift composition;
- `egress-gateway` is the shared cluster gateway for retrieval-enabled
  instances.

Do not deploy `base` directly or adapt `kind` for production. Store each
site-specific render in the operator's GitOps repository. The committed
renders contain placeholders and are inputs to customisation, not deployable
instance manifests.

For each instance:

1. Create a dedicated namespace or OpenShift project and give it a unique
   instance label.
2. Replace only existing `app.kubernetes.io/instance: cairn` values with the
   same instance name, using the targeted example below; never add this label
   to DNS or shared-gateway destination selectors.
3. Replace `REPLACE_WITH_PER_INSTANCE_UUID` with a new UUIDv4 and retain that
   value for the catalogue's lifetime.
4. Set an operator-controlled Cairn image reference and record its resolved
   digest. Leave the pinned dependency images in `deploy/images.lock`.
5. Select CSI storage with reliable POSIX locking and `fsync`,
   `volumeMode: Filesystem` and `ReadWriteOncePod`. NFS and other shared or
   network filesystems are unsupported for the SQLite WAL catalogue.
6. Retain one replica, the security contexts, probes, 60-second termination
   grace, `ClusterIP` service and default-deny policies.
7. Configure any ingress or Route explicitly, using TLS. Edge authentication
   does not replace Cairn bearer authentication.

### Customise instance labels without changing external destinations

Replace **only existing** `app.kubernetes.io/instance: cairn` values with your
instance name. Do not use a broad Kustomize `labels` transformation with
`includeSelectors: true`. It also adds the instance label to NetworkPolicy
**destination** Pod selectors. CoreDNS and the shared egress gateway do not
carry your instance's label, so those allow rules would match no Pods: DNS
resolution and provider access would stop working.

This worked example generates targeted JSON patches from the pinned
`kubernetes-retrieval` render. Each replacement first tests that the existing
value is `cairn`; it never creates a label or selector key. It updates the
existing workload, Service, volume-template and policy selector relationships
without relabelling CoreDNS or the shared gateway. Keep the shared gateway's
separate cluster overlay out of this per-instance customisation.

Run from the repository root with the locked Python environment and pinned
kubectl available (`uv sync --locked` and `./scripts/fetch-kubectl`). These
commands render locally and do not contact a cluster. The only value to change
in the example is `instance_name`; use the same name for your dedicated namespace.
The example directory must not already exist.

```bash
set -eu
instance_name=cairn-example
example=build/instance-label-example
test ! -e "$example"
mkdir -p "$example"
build/tools/kubectl kustomize deploy/kustomize/overlays/kubernetes-retrieval \
  > "$example/baseline.yaml"

uv run --locked python - "$example" "$instance_name" <<'PY'
import json
import re
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
instance = sys.argv[2]
if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", instance):
    raise SystemExit("use a DNS-compatible instance name, at most 63 characters")
if instance == "cairn":
    raise SystemExit("choose a distinct instance name")
label = "app.kubernetes.io/instance"


def existing_paths(value, pointer=""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            if key == label and child == "cairn":
                yield path
            else:
                yield from existing_paths(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from existing_paths(child, pointer + "/" + str(index))


patches = []
for resource in yaml.safe_load_all((root / "baseline.yaml").read_text()):
    operations = []
    for path in existing_paths(resource):
        operations.extend([
            {"op": "test", "path": path, "value": "cairn"},
            {"op": "replace", "path": path, "value": instance},
        ])
    if not operations:
        continue
    group, _, version = resource["apiVersion"].rpartition("/")
    target = {
        "group": group,
        "version": version,
        "kind": resource["kind"],
        "name": "^" + re.escape(resource["metadata"]["name"]) + "$",
    }
    patches.append({"target": target, "patch": json.dumps(operations)})
if not patches:
    raise SystemExit("no existing generic instance labels found; inspect the source")
(root / "kustomization.yaml").write_text(yaml.safe_dump({
    "apiVersion": "kustomize.config.k8s.io/v1beta1",
    "kind": "Kustomization",
    "resources": ["../../deploy/kustomize/overlays/kubernetes-retrieval"],
    "patches": patches,
}, sort_keys=False))
print(f"Wrote targeted patches for {len(patches)} resources")
PY

build/tools/kubectl kustomize "$example" > "$example/rendered.yaml"
```

Inspect `build/instance-label-example/kustomization.yaml`: a label path is
encoded as `/…/app.kubernetes.io~1instance`, and each `replace` is guarded by
`test`. Regenerate and review the patches when updating their pinned source;
a failed test is source drift, not a reason to replace `replace` with `add`.

Before proceeding, verify the rendered policies and the complete label-only
change. This check fails on the broad transformation described above:

```bash
uv run --locked python - "$example" "$instance_name" <<'PY'
import copy
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
instance = sys.argv[2]
label = "app.kubernetes.io/instance"


def resources(filename):
    return {
        (item["apiVersion"], item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all((root / filename).read_text())
    }


def replace_existing(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == label and child == "cairn":
                value[key] = instance
            else:
                replace_existing(child)
    elif isinstance(value, list):
        for child in value:
            replace_existing(child)


baseline = resources("baseline.yaml")
rendered = resources("rendered.yaml")
expected = copy.deepcopy(baseline)
for resource in expected.values():
    replace_existing(resource)
for name, key, value in [
    ("cairn-allow-dns-egress", "k8s-app", "kube-dns"),
    ("cairn-allow-gateway-egress", "app.kubernetes.io/name", "cairn-egress-gateway"),
]:
    identity = ("networking.k8s.io/v1", "NetworkPolicy", name)
    before = baseline[identity]["spec"]["egress"][0]["to"][0]
    after = rendered[identity]["spec"]["egress"][0]["to"][0]
    assert after["podSelector"]["matchLabels"] == {key: value}, name
    assert label not in after["podSelector"]["matchLabels"], name
    assert after["namespaceSelector"] == before["namespaceSelector"], name
    print(name, after)

for name in ("cairn-default-deny", "falkordb-default-deny"):
    spec = rendered[("networking.k8s.io/v1", "NetworkPolicy", name)]["spec"]
    assert set(spec["policyTypes"]) == {"Ingress", "Egress"}, name
    assert not spec.get("ingress") and not spec.get("egress"), name
    assert spec["podSelector"]["matchLabels"][label] == instance, name
assert rendered == expected, "render changed more than existing instance-label values"
print("PASS: only existing instance values changed; destination peers and default deny preserved")
PY
```

Expected DNS destination: `k8s-app: kube-dns` in the existing `kube-system`
namespace selector. Expected gateway destination:
`app.kubernetes.io/name: cairn-egress-gateway` in the existing `cairn-egress`
namespace selector. Neither destination Pod selector may contain
`app.kubernetes.io/instance`. Preserve all existing namespace selectors and
both ingress/egress default-deny policies.

This is a label-only example, not an apply-ready installation: the namespace,
instance UUID, image digest, storage and publication requirements above still
apply. Keep these reviewed patches in the site overlay and inspect its **final**
rendered NetworkPolicies again after all other customisations. Server-side dry
run validates API objects; it does not prove that a selector matches real Pods.

The portable base permits no ingress. A published instance also needs narrow
selectors for its ingress controller; do not replace default-deny with broad
namespace or CIDR access.

Render, hash, validate and apply the chosen overlay:

```sh
site_overlay=/path/to/site-overlay
instance=cairn-example
namespace=cairn-example
render="cairn-$instance.yaml"

kubectl kustomize "$site_overlay" > "$render"
sha256sum "$render" > "$render.sha256"
kubectl apply --dry-run=server -n "$namespace" -f "$render"
kubectl apply -n "$namespace" -f "$render"
```

Use `oc` for corresponding OpenShift commands only after validating the
target's security, storage, network-policy and routing behaviour.

### Credentials and retrieval egress

Supply credentials as a Secret at deployment time; never render or commit
them. For retrieval, follow the
[FalkorDB component guide](../../deploy/kustomize/components/falkordb/README.md).
Create Secret values from files rather than `--from-literal`, which exposes
values on the command line. Retain the projected Secret mode `0440`.

Retrieval-enabled Kubernetes uses an HTTP CONNECT proxy in `cairn-egress`.
Cairn may reach only cluster DNS, its FalkorDB service and that proxy. Squid
enforces the FQDN allow-list because portable `NetworkPolicy` cannot safely
express it.

Edit only `allowed_fqdns` in
[`configmap.yaml`](../../deploy/kustomize/overlays/egress-gateway/configmap.yaml),
then render and apply the complete gateway overlay before the Cairn instance.
Do not add unrestricted TCP/443 egress to Cairn Pods. Cluster-specific policy
extensions require validation on that cluster.

### First boot

The StatefulSet's `migrate` init container is idempotent. Bootstrap is a
one-time operation and refuses an existing realm.

1. Apply the instance at one replica and wait for its init container to finish.
   For retrieval, wait for FalkorDB first:

   ```sh
   namespace=cairn-example
   render=./cairn-cairn-example.yaml

   kubectl apply -n "$namespace" -f "$render"
   kubectl rollout status -n "$namespace" statefulset/falkordb --timeout=420s
   kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
   kubectl get -n "$namespace" pod/cairn-0 \
     -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.reason}{"\n"}'
   ```

   Require `Completed`, then stop Cairn so bootstrap has exclusive access to
   its data claim:

   ```sh
   kubectl scale -n "$namespace" statefulset/cairn --replicas=0
   kubectl wait -n "$namespace" --for=delete pod/cairn-0 --timeout=120s
   ```

2. Create a one-off Pod from the same image, service account, ConfigMap,
   optional Secret and `data-cairn-0` claim as the StatefulSet. Give it the
   arguments below and retain the workload's security context:

   ```yaml
   args:
     - bootstrap
     - --config
     - /etc/cairn/config.yaml
     - --realm
     - REPLACE_WITH_REALM
     - --label
     - REPLACE_WITH_LABEL
   ```

   Validate and run the Pod, then require it to succeed:

   ```sh
   kubectl apply --dry-run=server -f cairn-bootstrap.yaml
   kubectl apply -f cairn-bootstrap.yaml
   kubectl wait -n "$namespace" \
     --for=jsonpath='{.status.phase}'=Succeeded \
     pod/cairn-bootstrap --timeout=180s
   kubectl logs -n "$namespace" pod/cairn-bootstrap
   ```

3. Capture the JSON line whose `operation` is `bootstrap` and place its token
   directly into the authorised credential store. Do not retain it in Git,
   shell history, annotations or long-lived logs.
4. Delete the bootstrap Pod, restore the StatefulSet to one replica and verify
   the rollout:

   ```sh
   kubectl delete -n "$namespace" pod/cairn-bootstrap --wait=true
   kubectl scale -n "$namespace" statefulset/cairn --replicas=1
   kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
   ```

Do not start another serving deployment with the same `instance_id`.

## Docker Compose

Follow the canonical commands in the
[Compose project guide](../../deploy/compose/README.md). Run them from
`deploy/compose`, pass `../images.lock` before the instance `.env`, and add
`-f compose.yaml -f compose.graphiti.yaml` to every retrieval command.

Use a unique project name, loopback port and permanent `instance_id`. Run
`migrate`, bootstrap once, retain its token, start the service, then verify
container state, `/health/ready` and authenticated `/v1/instance`. Export
backups to storage outside the Compose project.

Compose publishes only on loopback. External publication requires a
TLS-terminating, rate-limiting reverse proxy. Use Kubernetes when tenants need
an enforced network-policy boundary.

### Upgrade and rollback

Export a backup before an upgrade. Update the image pin, pull it, stop Cairn,
run `migrate` with the new image and start the replacement. Migration does not
run as part of an ordinary restart.

Rollback means restoring a backup into a fresh project, namespace or data
volume. Do not downgrade a catalogue in place or copy files over a serving
volume. See [backup and restore](backup-restore.md) for the quiesce and restore
procedure.
