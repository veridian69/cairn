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
2. Replace every generic `app.kubernetes.io/instance: cairn` value with the
   same instance name while preserving selector relationships.
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
