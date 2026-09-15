# Deploying Cairn

**Installing for the first time?** Start with the
[guided installer quick install](../install.md#quick-install-with-the-guided-installer)
for disposable native, persistent native and Docker. The manual
[persistent native installation](native-installation.md) and
[complete Compose procedure](../../deploy/compose/README.md) remain the full
reference. Kubernetes is a separate manual deployment path without installer
support. Fresh-user beginner acceptance is still pending.

This guide selects and deploys one Cairn v0.1 instance. Read the
[backup and restore runbook](backup-restore.md) before first production use,
then use the [client guide](../clients.md) to verify the served instance and
configure REST or MCP callers.

## Image and contract boundary

`deploy/images.lock` currently sets `CAIRN_IMAGE=cairn:v0.5.0-rc.4`. This is a
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

For a complete installation, start with the [cluster preflight](kubernetes-preflight.md)
and [complete site overlay](kubernetes-site.md). The label-only example below
is a focused explanation of the selector safeguard.

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

### Namespace and render preparation

Before any namespaced operation, an administrator must provision the dedicated
namespace and its instance label. The overlays do not create that namespace.
Replace both values below with the site's namespace and instance name. For a new
namespace, the administrator runs:

```sh
namespace=cairn-example
instance=cairn-example
kubectl create namespace "$namespace"
kubectl label namespace "$namespace" cairn.example.invalid/instance="$instance"
```

For an existing namespace, the administrator checks its ownership and adds the
label only if absent; do not overwrite a conflicting instance label. The
installation operator then sets the same two values in their terminal and
verifies the namespace before continuing:

```sh
namespace=cairn-example
instance=cairn-example
kubectl get namespace "$namespace" -o json |
  jq -e --arg instance "$instance" '.metadata.labels["cairn.example.invalid/instance"] == $instance'
```

Expect `true`. This namespace label admits the instance to the shared gateway;
it does not replace the workload's `app.kubernetes.io/instance` labels or add
labels to external destination selectors. Namespace read access is required for
this check; ask the administrator to supply it if necessary.

Render, hash and validate the reviewed site overlay. Replace `site_overlay`
with its actual path. This block does not start the workloads; the real apply
is in First boot, after credential and gateway preparation:

```sh
site_overlay=/path/to/reviewed/site-overlay
render="cairn-$instance.yaml"
kubectl kustomize "$site_overlay" > "$render"
sha256sum "$render" > "$render.sha256"
kubectl apply --dry-run=server -n "$namespace" -f "$render"
```

Use `oc` for corresponding OpenShift commands only after validating the
target's security, storage, network-policy and routing behaviour.

### Credentials and retrieval egress

Use the [shared gateway installation procedure](kubernetes-gateway.md) for exact
namespace, portable/Cilium overlay, render, apply and readiness commands.


Supply credentials as a Secret at deployment time; never render or commit
them. For retrieval, follow the
[FalkorDB component guide](../../deploy/kustomize/components/falkordb/README.md).
Create Secret values from files rather than `--from-literal`, which exposes
values on the command line. Retain the projected Secret mode `0440`.

Retrieval-enabled Kubernetes uses an HTTP CONNECT proxy in `cairn-egress`.
Cairn may reach only cluster DNS, its FalkorDB service and that proxy. Squid
enforces the FQDN allow-list because portable `NetworkPolicy` cannot safely
express it.

Follow the [gateway site-manifest procedure](kubernetes-gateway.md#render-and-validate-locally)
to set `allowed_fqdns`, generate a separate site manifest, and review and commit
it before applying the gateway. The default allow-list is `api.openai.com`;
keep the repository's source ConfigMap unchanged. Apply the gateway before the
Cairn instance. Do not add unrestricted TCP/443 egress to Cairn Pods. Cluster-specific policy
extensions require validation on that cluster.

### First boot

Bootstrap is a deliberate one-off operation. The StatefulSet's `migrate` init
container is idempotent; `bootstrap` is not, and a repeat refuses with
`realm_exists`. Run the following blocks in one shell from the repository root.
They never put the one-time token in a Pod log or command argument.

1. Set every operator-owned value explicitly. `site_render` must be the final
   reviewed render for this instance, not an unreviewed base or test overlay.
   Keep the credential outside the checkout and do not reuse an existing path:

   ```sh
   set -eu
   namespace='REPLACE_WITH_INSTANCE_NAMESPACE'
   site_render='/absolute/path/to/REPLACE_WITH_REVIEWED_RENDER.yaml'
   realm='REPLACE_WITH_REALM'
   label='REPLACE_WITH_INITIAL_OPERATOR_LABEL'
   credential_file="$HOME/.config/cairn/credentials/REPLACE_WITH_INSTANCE.token"
   bootstrap_manifest="$(dirname "$site_render")/cairn-bootstrap.yaml"

   test -f "$site_render"
   case "$namespace:$site_render:$realm:$label:$credential_file" in
     *REPLACE_WITH_*) printf '%s\n' 'Replace every operator value first.' >&2; exit 1 ;;
   esac
   case "$credential_file" in
     /*) ;;
     *) printf '%s\n' 'credential_file must be absolute.' >&2; exit 1 ;;
   esac
   test ! -e "$credential_file" && test ! -L "$credential_file"
   test ! -e "$bootstrap_manifest" && test ! -L "$bootstrap_manifest"

   repository_root="$(realpath -e "$(git rev-parse --show-toplevel)")"
   credential_dir="$(dirname "$credential_file")"
   install -d -m 0700 "$credential_dir"
   credential_dir="$(realpath -e "$credential_dir")"
   credential_file="$credential_dir/$(basename "$credential_file")"
   test -O "$credential_dir"
   test "$(stat -c '%a' "$credential_dir")" = 700
   case "$credential_file" in
     "$repository_root"|"$repository_root"/*)
       printf '%s\n' 'credential_file must be outside Git.' >&2
       exit 1
       ;;
   esac
   ```

2. Generate the holding Pod from that exact StatefulSet. This preserves its
   image, Pod labels and annotations, service account, Pod and container
   security contexts (including any reviewed UID, GID or `fsGroup`), scheduling
   fields, and the `config`, `credentials` and `tmp` volumes and mounts. It maps
   the StatefulSet's `data` claim template to the existing `data-cairn-0` PVC,
   removes the migration init container and serving probes, and replaces the
   image entrypoint with a harmless `/bin/sh` sleep loop. The generator refuses
   renamed workloads, extra application containers or an unexpected storage
   shape instead of guessing how to adapt them.

   ```sh
   uv run --locked --no-dev python - "$site_render" "$bootstrap_manifest" "$namespace" <<'PY'
   import copy
   import sys
   from pathlib import Path

   import yaml

   render_path = Path(sys.argv[1])
   output_path = Path(sys.argv[2])
   namespace = sys.argv[3]
   documents = [
       document
       for document in yaml.safe_load_all(render_path.read_text(encoding="utf-8"))
       if document
   ]
   statefulsets = [
       document
       for document in documents
       if document.get("apiVersion") == "apps/v1"
       and document.get("kind") == "StatefulSet"
       and document.get("metadata", {}).get("name") == "cairn"
   ]
   if len(statefulsets) != 1:
       raise SystemExit("reviewed render must contain exactly one StatefulSet/cairn")

   statefulset = statefulsets[0]
   rendered_namespace = statefulset.get("metadata", {}).get("namespace")
   if rendered_namespace not in (None, namespace):
       raise SystemExit("namespace does not match the reviewed StatefulSet")
   retention = statefulset["spec"].get("persistentVolumeClaimRetentionPolicy", {})
   if retention.get("whenScaled", "Retain") != "Retain":
       raise SystemExit("StatefulSet whenScaled PVC retention must be Retain")
   template = statefulset["spec"]["template"]
   template_spec = template["spec"]
   containers = template_spec.get("containers", [])
   if len(containers) != 1 or containers[0].get("name") != "cairn":
       raise SystemExit("reviewed StatefulSet must have one container named cairn")

   claim_templates = statefulset["spec"].get("volumeClaimTemplates", [])
   data_claims = [
       claim
       for claim in claim_templates
       if claim.get("metadata", {}).get("name") == "data"
   ]
   if len(claim_templates) != 1 or len(data_claims) != 1:
       raise SystemExit("reviewed StatefulSet must have only one data claim template")
   claim_name = f'data-{statefulset["metadata"]["name"]}-0'
   if claim_name != "data-cairn-0":
       raise SystemExit("unexpected StatefulSet PVC name")

   pod_spec = copy.deepcopy(template_spec)
   pod_spec.pop("initContainers", None)
   container = copy.deepcopy(containers[0])
   for field in ("startupProbe", "readinessProbe", "livenessProbe", "ports"):
       container.pop(field, None)
   container["command"] = ["/bin/sh", "-c"]
   container["args"] = ["trap 'exit 0' TERM INT; while :; do sleep 5; done"]
   pod_spec["containers"] = [container]
   pod_spec["restartPolicy"] = "Never"

   volumes = copy.deepcopy(template_spec.get("volumes", []))
   if any(volume.get("name") == "data" for volume in volumes):
       raise SystemExit("reviewed Pod volumes already contain data")
   volumes.insert(
       0,
       {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}},
   )
   pod_spec["volumes"] = volumes

   required = {"data", "config", "credentials", "tmp"}
   mounts = {mount["name"] for mount in container.get("volumeMounts", [])}
   volume_names = {volume["name"] for volume in volumes}
   if not required <= mounts or not required <= volume_names:
       raise SystemExit("reviewed StatefulSet lacks a required mount or volume")
   if pod_spec.get("securityContext") != template_spec.get("securityContext"):
       raise SystemExit("Pod security context was not preserved")
   if container.get("securityContext") != containers[0].get("securityContext"):
       raise SystemExit("container security context was not preserved")
   if container.get("image") != containers[0].get("image"):
       raise SystemExit("reviewed image was not preserved")

   metadata = copy.deepcopy(template.get("metadata", {}))
   metadata.pop("generateName", None)
   metadata["name"] = "cairn-bootstrap"
   metadata["namespace"] = namespace
   pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": pod_spec}
   with output_path.open("x", encoding="utf-8") as output:
       yaml.safe_dump(pod, output, sort_keys=False)
   PY
   ```

   Review `cairn-bootstrap.yaml` beside the site render. It contains no token.
   If a platform mutates Pods at admission, inspect the server-side dry-run
   below as part of that platform's review.

3. Apply the reviewed instance at one replica. This creates `data-cairn-0` and
   runs `migrate`. For the retrieval overlay, wait for FalkorDB before Cairn;
   omit only that first rollout command for the index-free overlay:

   ```sh
   kubectl apply -n "$namespace" -f "$site_render"
   # Retrieval overlay only:
   kubectl rollout status -n "$namespace" statefulset/falkordb --timeout=420s
   kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
   migration_state="$(
     kubectl get -n "$namespace" pod/cairn-0 \
       -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.reason}'
   )"
   test "$migration_state" = Completed

   kubectl scale -n "$namespace" statefulset/cairn --replicas=0
   kubectl wait -n "$namespace" --for=delete pod/cairn-0 --timeout=180s
   ```

   Leave FalkorDB running when it is present. Waiting for `cairn-0` to be
   deleted preserves exclusive ownership of the Cairn data claim and its lease.

4. Refuse a stale bootstrap Pod, validate the generated manifest through
   admission, then start the holder and require it to become Ready:

   ```sh
   if kubectl get -n "$namespace" pod/cairn-bootstrap >/dev/null 2>&1; then
     printf '%s\n' 'pod/cairn-bootstrap already exists; inspect it first.' >&2
     exit 1
   fi
   kubectl apply --dry-run=server -o yaml -f "$bootstrap_manifest"
   kubectl apply -f "$bootstrap_manifest"
   kubectl wait -n "$namespace" \
     --for=condition=Ready pod/cairn-bootstrap --timeout=180s
   ```

5. Execute bootstrap inside the holder. Standard output and error go directly
   to owner-only files beside the final credential, outside Git. The token is
   validated and hard-linked into the requested path, so an existing path is
   never overwritten. Nothing prints the JSON or token to the terminal:

   ```sh
   umask 077
   bootstrap_stdout="$(mktemp "$credential_dir/.bootstrap.stdout.XXXXXXXX")"
   bootstrap_stderr="$(mktemp "$credential_dir/.bootstrap.stderr.XXXXXXXX")"
   token_staging="$(mktemp "$credential_dir/.bootstrap.token.XXXXXXXX")"
   chmod 0600 "$bootstrap_stdout" "$bootstrap_stderr" "$token_staging"

   if ! kubectl exec -n "$namespace" pod/cairn-bootstrap -- \
     cairn bootstrap --config /etc/cairn/config.yaml \
       --realm "$realm" --label "$label" \
       >"$bootstrap_stdout" 2>"$bootstrap_stderr"; then
     if grep -Eq '"code"[[:space:]]*:[[:space:]]*"realm_exists"' \
       "$bootstrap_stderr"; then
       printf '%s\n' 'The realm already exists; bootstrap was correctly refused.' >&2
     fi
     printf 'Retained owner-only evidence: %s %s\n' \
       "$bootstrap_stdout" "$bootstrap_stderr" >&2
     exit 1
   fi

   if ! jq -jers --arg realm "$realm" '
     select(type == "array" and length == 1) |
     .[0] |
     select(
       .status == "ok" and
       .operation == "bootstrap" and
       .realm_id == $realm and
       (.instance_id | type == "string") and
       (.principal_id | type == "string") and
       (.credential_id | type == "string") and
       (.grant_ids | type == "array")
     ) |
     .token |
     select(
       type == "string" and
       test("^cairn1\\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\\.[A-Za-z0-9_-]{43}$")
     )
   ' "$bootstrap_stdout" >"$token_staging"; then
     printf 'Invalid result retained for recovery: %s %s\n' \
       "$bootstrap_stdout" "$bootstrap_stderr" >&2
     exit 1
   fi
   if ! ln "$token_staging" "$credential_file"; then
     printf 'Credential path appeared; result retained at %s\n' \
       "$bootstrap_stdout" >&2
     exit 1
   fi
   rm -f "$token_staging" "$bootstrap_stdout" "$bootstrap_stderr"
   test "$(stat -c '%a' "$credential_file")" = 600
   test -s "$credential_file"
   printf 'Credential saved to %s\n' "$credential_file"
   ```

   A transport interruption can occur after the catalogue commits but before
   the local capture completes. Keep both evidence files and do not blindly
   rerun bootstrap. A later `realm_exists` result proves that the realm exists;
   it does not recover the lost token. Use a retained credential if one exists.
   If access is genuinely lost, an authorised operator can use this same
   stopped holder and owner-only capture pattern with `cairn recover --config
   /etc/cairn/config.yaml --realm "$realm" --label
   REPLACE_WITH_NEW_RECOVERY_LABEL`, validating `operation == "recover"` before
   installing its replacement token. Recovery appends an audited grant and
   does not erase old credentials; revoke superseded access through the normal
   authenticated administration path when policy requires it.

6. Delete the holder before restoring the serving replica, then verify the
   rollout. Do not scale Cairn up while any Pod still mounts `data-cairn-0`:

   ```sh
   kubectl delete -n "$namespace" pod/cairn-bootstrap --wait=true
   kubectl scale -n "$namespace" statefulset/cairn --replicas=1
   kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
   kubectl get -n "$namespace" pod/cairn-0
   ```

Use `credential_file` for the authenticated client check in the installation
guide. Then exercise the [Kubernetes backup and recovery
mechanics](backup-restore.md#kubernetes-or-openshift-mechanics). Do not start a
second serving deployment with the same `instance_id`; restore retains that
identity and follows the runbook's quiesce procedure.

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
