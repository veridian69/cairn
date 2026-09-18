# Install the Kubernetes retrieval gateway

This procedure installs Cairn's shared HTTP CONNECT gateway once in an
**existing conformant Kubernetes cluster**. It does not create or repair a
cluster, install a CNI, alter cluster DNS, or grant RBAC. Those are cluster
administrator responsibilities. Do not continue when the checks below expose
an admission, CNI, DNS, or permission problem.

The gateway is required only for the `kubernetes-retrieval` instance overlay.
The index-free `kubernetes` overlay makes no provider calls and does not need
it. The gateway contains no provider credential: Cairn reads that credential
from its own Secret and sends HTTPS through this proxy.

The namespace and Service names are fixed security interfaces:

- `cairn-egress` is the dedicated gateway namespace;
- `cairn-egress-gateway.cairn-egress.svc.cluster.local:3128` is the proxy
  endpoint used by retrieval-enabled Cairn Pods; and
- a Cairn instance namespace gains access only when a cluster administrator
  gives it the `cairn.example.invalid/instance` label and its Pod has the
  expected `app.kubernetes.io/name: cairn` label.

Do not rename the gateway namespace or Service, put the instance-namespace
label on `cairn-egress`, or add another policy that broadly admits ingress or
egress. Namespace-label authority is gateway authority and should remain with
cluster administrators.

## Choose the gateway profile

Use one profile:

| `gateway_profile` | Kustomize source | When to use it |
| --- | --- | --- |
| `portable` | `deploy/kustomize/overlays/egress-gateway` | Default for a conformant Kubernetes CNI that enforces `NetworkPolicy`. Squid enforces the FQDN allow-list; portable policy permits the gateway's DNS traffic and TCP 443. |
| `cilium-reference` | `deploy/kustomize/overlays/egress-gateway-reference` | The recorded Cilium 1.19.6 target profile. It adds private IPv4 exclusions and a `CiliumNetworkPolicy` denying cluster, host, node and API-server identities on TCP 443. Use it only when the administrator has confirmed compatible Cilium behaviour and the `cilium.io/v2` policy API. |

The second overlay's name records its accepted `reference` evidence; it is not a
generic promise for every Cilium cluster. A different Cilium version or network
shape needs its own review and target evidence. OpenShift also needs separate
SCC, CNI and fixed-UID validation and is outside this procedure.

The only other operator values are:

- `allowed_fqdns`, a Bash array of exact lowercase provider hostnames. The
  example permits only `api.openai.com`. Do not enter a URL, port, IP address,
  wildcard, internal service, broad parent domain, credential, or shell-quoted
  fragment; and
- `gateway_gitops_root`, the absolute root of an existing, owner-controlled
  site GitOps checkout. The shown example must already be a Git repository;
  change it to the site's real checkout. `gateway_record_dir` is a new child
  directory created there for the baseline, final manifests, digests, selected
  profile and Cairn source revision.

Changing the allow-list changes every retrieval-enabled Cairn instance on the
cluster. Include only destinations approved for all of them.

## Render and validate locally

Start at the Cairn repository root after completing the Kubernetes tooling
steps in [Install Cairn](../install.md#prerequisites-and-access). These commands
use the pinned kubectl/Kustomize and locked Python environment. They do not
contact a cluster.

```bash
set -eu
set -o pipefail
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -x build/tools/kubectl

gateway_profile=portable
allowed_fqdns=(api.openai.com)
gateway_gitops_root="$HOME/cairn-site-gitops"

case "$gateway_profile" in
  portable)
    gateway_overlay=deploy/kustomize/overlays/egress-gateway
    ;;
  cilium-reference)
    gateway_overlay=deploy/kustomize/overlays/egress-gateway-reference
    ;;
  *)
    printf 'gateway_profile must be portable or cilium-reference\n' >&2
    exit 1
    ;;
esac
case "$gateway_gitops_root" in
  /*) ;;
  *) printf 'gateway_gitops_root must be an absolute path\n' >&2; exit 1 ;;
esac
gateway_gitops_root="$(realpath -e -- "$gateway_gitops_root")"
test "$(git -C "$gateway_gitops_root" rev-parse --show-toplevel)" = \
  "$gateway_gitops_root"
gateway_record_dir="$gateway_gitops_root/cairn-egress"
if test -e "$gateway_record_dir"; then
  printf 'refusing to overwrite gateway record: %s\n' "$gateway_record_dir" >&2
  exit 1
fi
umask 027
install -d -m 0750 "$gateway_record_dir"
gateway_baseline="$gateway_record_dir/baseline.yaml"
gateway_render="$gateway_record_dir/gateway.yaml"
gateway_namespace_render="$gateway_record_dir/namespace.yaml"

build/tools/kubectl kustomize "$gateway_overlay" > "$gateway_baseline"
uv run --locked python - "$gateway_baseline" "$gateway_render" \
  "${allowed_fqdns[@]}" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
hosts = sys.argv[3:]
if not hosts:
    raise SystemExit("the provider FQDN allow-list cannot be empty")
pattern = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
if len(set(hosts)) != len(hosts) or any(pattern.fullmatch(host) is None for host in hosts):
    raise SystemExit("use distinct lowercase fully qualified hostnames")

text = source.read_text(encoding="utf-8")
acl = re.compile(r"(?m)^    acl allowed_fqdns dstdomain [^\n]+$")
if len(acl.findall(text)) != 1:
    raise SystemExit("the rendered gateway has an unexpected allow-list shape")
configured = acl.sub("    acl allowed_fqdns dstdomain " + " ".join(hosts), text)
destination.write_text(configured, encoding="utf-8")
PY

printf '%s\n' "$gateway_profile" > "$gateway_record_dir/profile"
git rev-parse HEAD > "$gateway_record_dir/source-revision"

uv run --locked python - "$gateway_render" "$gateway_namespace_render" "$gateway_profile" \
  "${allowed_fqdns[@]}" <<'PY'
from pathlib import Path
import sys

import yaml

path = Path(sys.argv[1])
namespace_path = Path(sys.argv[2])
profile = sys.argv[3]
hosts = sys.argv[4:]
documents = [item for item in yaml.safe_load_all(path.read_text()) if item]
objects = {(item["kind"], item["metadata"]["name"]): item for item in documents}
expected = {
    ("Namespace", "cairn-egress"),
    ("ConfigMap", "egress-gateway-config"),
    ("Service", "cairn-egress-gateway"),
    ("Deployment", "cairn-egress-gateway"),
    ("NetworkPolicy", "egress-gateway-default-deny"),
    ("NetworkPolicy", "egress-gateway-allow-cairn-ingress"),
    ("NetworkPolicy", "egress-gateway-allow-egress"),
}
if profile == "cilium-reference":
    expected.add(("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https"))
if set(objects) != expected or len(objects) != len(documents):
    raise SystemExit("gateway render does not contain the exact expected objects")
for item in documents:
    if item["kind"] != "Namespace" and item["metadata"].get("namespace") != "cairn-egress":
        raise SystemExit("a gateway object escaped the fixed namespace")
namespace_path.write_text(
    yaml.safe_dump(objects[("Namespace", "cairn-egress")], sort_keys=False),
    encoding="utf-8",
)

configuration = objects[("ConfigMap", "egress-gateway-config")]["data"]["squid.conf"]
lines = configuration.splitlines()
acl = "acl allowed_fqdns dstdomain " + " ".join(hosts)
if lines.count(acl) != 1:
    raise SystemExit("the final allow-list differs from the requested hostnames")
access = [line for line in lines if line.startswith("http_access ")]
if access != [
    "http_access deny !CONNECT",
    "http_access deny CONNECT !SSL_ports",
    "http_access allow CONNECT allowed_fqdns",
    "http_access deny all",
]:
    raise SystemExit("the ordered CONNECT deny/allow policy changed")

deny = objects[("NetworkPolicy", "egress-gateway-default-deny")]["spec"]
if set(deny["policyTypes"]) != {"Ingress", "Egress"} or deny.get("ingress") or deny.get("egress"):
    raise SystemExit("the ingress/egress default deny changed")
ingress = objects[("NetworkPolicy", "egress-gateway-allow-cairn-ingress")]["spec"]
expression = ingress["ingress"][0]["from"][0]["namespaceSelector"]["matchExpressions"]
if expression != [{"key": "cairn.example.invalid/instance", "operator": "Exists"}]:
    raise SystemExit("the instance-namespace grant selector changed")
if ingress["ingress"][0]["from"][0]["podSelector"]["matchLabels"] != {
    "app.kubernetes.io/name": "cairn"
}:
    raise SystemExit("the Cairn source Pod selector changed")

egress = objects[("NetworkPolicy", "egress-gateway-allow-egress")]["spec"]["egress"]
if egress[0]["ports"] != [
    {"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}
]:
    raise SystemExit("the gateway DNS ports changed")
if egress[0]["to"][0]["namespaceSelector"]["matchLabels"] != {
    "kubernetes.io/metadata.name": "kube-system"
} or egress[0]["to"][0]["podSelector"]["matchLabels"] != {"k8s-app": "kube-dns"}:
    raise SystemExit("the gateway DNS selectors changed")
if egress[1]["ports"] != [{"protocol": "TCP", "port": 443}]:
    raise SystemExit("the provider egress port changed")
if profile == "portable":
    if "to" in egress[1]:
        raise SystemExit("the portable gateway egress unexpectedly has address selectors")
else:
    block = egress[1]["to"][0]["ipBlock"]
    required = {"127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"}
    if block["cidr"] != "0.0.0.0/0" or set(block["except"]) != required:
        raise SystemExit("the Cilium target's private IPv4 exclusions changed")
    policy = objects[("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https")]
    denied = set(policy["spec"]["egressDeny"][0]["toEntities"])
    if denied != {"cluster", "host", "remote-node", "kube-apiserver"}:
        raise SystemExit("the Cilium identity deny changed")
    if policy["spec"]["egressDeny"][0]["toPorts"] != [{
        "ports": [{"port": "443", "protocol": "TCP"}]
    }]:
        raise SystemExit("the Cilium identity deny port changed")

deployment = objects[("Deployment", "cairn-egress-gateway")]
container = deployment["spec"]["template"]["spec"]["containers"][0]
if "@sha256:" not in container["image"] or container["securityContext"].get("readOnlyRootFilesystem") is not True:
    raise SystemExit("the pinned image or read-only container posture changed")
print(f"PASS: {profile} gateway has {len(objects)} exact objects; allow-list={','.join(hosts)}")
PY

(cd "$gateway_record_dir" && sha256sum gateway.yaml namespace.yaml > manifests.sha256)
cat "$gateway_record_dir/manifests.sha256"
```

Expect `PASS: portable gateway has 7 exact objects` for the default profile, or
`PASS: cilium-reference gateway has 8 exact objects` for the recorded Cilium
profile, followed by SHA-256 digests for `gateway.yaml` and the derived
namespace-only manifest. Review both manifests, the profile, revision and
digests. Add all six generated files to the site repository, complete its
normal review and commit them before the cluster checks. Do not apply
`baseline.yaml`; it exists to make the allow-list change reviewable.

## Check access and prepare the namespace

The following commands contact the selected cluster but change nothing. Run
them from the repository root in the same trusted operator session. The gateway
is a cluster-shared boundary, so its installer needs explicit authority for its
Namespace and namespaced objects. A `no`, an unexpected context, or a command
error is an administrator action, not an invitation to grant cluster-admin or
weaken admission.

```bash
set -eu
set -o pipefail
gateway_namespace=cairn-egress
gateway_gitops_root="$HOME/cairn-site-gitops"
gateway_gitops_root="$(realpath -e -- "$gateway_gitops_root")"
test "$(git -C "$gateway_gitops_root" rev-parse --show-toplevel)" = \
  "$gateway_gitops_root"
gateway_record_dir="$gateway_gitops_root/cairn-egress"
gateway_render="$gateway_record_dir/gateway.yaml"
gateway_namespace_render="$gateway_record_dir/namespace.yaml"
gateway_profile="$(cat "$gateway_record_dir/profile")"
test -s "$gateway_render"
test -s "$gateway_namespace_render"
gateway_record_relative="${gateway_record_dir#"$gateway_gitops_root"/}"
test "$gateway_record_relative" != "$gateway_record_dir"
for file in baseline.yaml gateway.yaml namespace.yaml manifests.sha256 profile source-revision; do
  git -C "$gateway_gitops_root" ls-files --error-unmatch -- \
    "$gateway_record_relative/$file" >/dev/null
done
test -z "$(git -C "$gateway_gitops_root" status --porcelain -- \
  "$gateway_record_relative")"
printf 'Committed gateway record: %s at %s\n' "$gateway_record_relative" \
  "$(git -C "$gateway_gitops_root" rev-parse HEAD)"

kubectl config current-context
for check in 'get namespaces' 'create namespaces' 'patch namespaces'; do
  # Namespace is cluster-scoped. Keep these checks separate from the
  # namespace-scoped loop below so kubectl does not emit its misleading
  # "resource is not namespace scoped" warning.
  test "$(kubectl auth can-i $check)" = yes
done
for check in \
  'get configmaps --namespace cairn-egress' \
  'list configmaps --namespace cairn-egress' \
  'create configmaps --namespace cairn-egress' \
  'patch configmaps --namespace cairn-egress' \
  'get services --namespace cairn-egress' \
  'list services --namespace cairn-egress' \
  'create services --namespace cairn-egress' \
  'patch services --namespace cairn-egress' \
  'get deployments.apps --namespace cairn-egress' \
  'list deployments.apps --namespace cairn-egress' \
  'create deployments.apps --namespace cairn-egress' \
  'patch deployments.apps --namespace cairn-egress' \
  'get networkpolicies.networking.k8s.io --namespace cairn-egress' \
  'list networkpolicies.networking.k8s.io --namespace cairn-egress' \
  'create networkpolicies.networking.k8s.io --namespace cairn-egress' \
  'patch networkpolicies.networking.k8s.io --namespace cairn-egress' \
  'get pods --namespace cairn-egress' \
  'list pods --namespace cairn-egress' \
  'watch pods --namespace cairn-egress' \
  'watch deployments.apps --namespace cairn-egress'; do
  # Word splitting is intentional: every entry above is a fixed kubectl query.
  test "$(kubectl auth can-i $check)" = yes
done
if test "$gateway_profile" = cilium-reference; then
  test "$(kubectl get customresourcedefinition \
    ciliumnetworkpolicies.cilium.io -o jsonpath='{.spec.group}')" = cilium.io
  for verb in get create patch; do
    test "$(kubectl auth can-i "$verb" ciliumnetworkpolicies.cilium.io \
      --namespace "$gateway_namespace")" = yes
  done
fi

existing_namespace="$(kubectl get namespace "$gateway_namespace" \
  --ignore-not-found -o name)"
if test -n "$existing_namespace"; then
  gateway_label="$(kubectl get namespace "$gateway_namespace" \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/name}')"
  case "$gateway_label" in
    ''|cairn-egress-gateway) ;;
    *) printf 'namespace has a conflicting owner label: %s\n' "$gateway_label" >&2; exit 1 ;;
  esac
  instance_grant="$(kubectl get namespace "$gateway_namespace" -o json | \
    jq -r '.metadata.labels["cairn.example.invalid/instance"] // ""')"
  if test -n "$instance_grant"; then
    printf 'shared gateway namespace carries an instance access label\n' >&2
    exit 1
  fi
  kubectl get configmap,service,deployment,networkpolicy \
    --namespace "$gateway_namespace" --ignore-not-found
else
  printf 'namespace %s is available for the gateway\n' "$gateway_namespace"
fi

for identity in \
  configmap/egress-gateway-config \
  service/cairn-egress-gateway \
  deployment.apps/cairn-egress-gateway \
  networkpolicy.networking.k8s.io/egress-gateway-default-deny \
  networkpolicy.networking.k8s.io/egress-gateway-allow-cairn-ingress \
  networkpolicy.networking.k8s.io/egress-gateway-allow-egress; do
  if kubectl get --namespace "$gateway_namespace" "$identity" >/dev/null 2>&1; then
    printf 'managed gateway object already exists; use the reviewed update procedure: %s\n' \
      "$identity" >&2
    exit 1
  fi
done
if test "$gateway_profile" = cilium-reference && \
    kubectl get --namespace "$gateway_namespace" \
      ciliumnetworkpolicy/egress-gateway-deny-cluster-https >/dev/null 2>&1; then
  printf 'managed Cilium gateway policy already exists; use the reviewed update procedure\n' >&2
  exit 1
fi
```

The context command must name the intended cluster, every RBAC check must
return `yes`, and the Cilium check must return `cilium.io` when selected. A new
installation may use an absent namespace or a pre-provisioned dedicated
namespace whose owner label is absent or already `cairn-egress-gateway`.
Existing gateway objects deliberately stop this first-install procedure before
it can overwrite them. Inspect and update an established GitOps installation
instead.

Do not delete or recreate an existing namespace to make this check pass.
Namespace deletion can remove unrelated resources, and adding an instance
label to this shared namespace would widen gateway ingress. Existing policies
remain in force; if any unrelated object or policy appears in the inventory,
have the administrator confirm that `cairn-egress` is genuinely dedicated
before continuing.

## Create the namespace, validate and apply

Server dry run does not persist a Namespace for later objects in the same
multi-object file. Validate and apply the derived Namespace manifest first,
then verify its two security labels before validating the complete gateway.
Server-side apply preserves unrelated existing labels and uses no
`--force-conflicts`; a conflict therefore stops rather than stealing another
manager's fields.

```bash
kubectl apply --server-side --dry-run=server \
  --field-manager=cairn-gateway-installer -f "$gateway_namespace_render"
kubectl apply --server-side \
  --field-manager=cairn-gateway-installer -f "$gateway_namespace_render"
kubectl get namespace "$gateway_namespace" -o json | jq -e '
  .metadata.labels["app.kubernetes.io/name"] == "cairn-egress-gateway" and
  (.metadata.labels["cairn.example.invalid/instance"] == null)'

kubectl apply --server-side --dry-run=server \
  --field-manager=cairn-gateway-installer -f "$gateway_render"
kubectl apply --server-side \
  --field-manager=cairn-gateway-installer -f "$gateway_render"
```

The namespace-only dry run and apply must report the Namespace, and the label
check must print `true`. For the portable profile, each complete-manifest
command must report one Namespace, ConfigMap, Service, Deployment and three
NetworkPolicies. The Cilium profile also reports one CiliumNetworkPolicy. On a
fresh installation, the namespace is `created` by the first apply, then
`configured` or `unchanged` by the complete apply; the namespaced objects are
`created`. A pre-provisioned namespace may be `configured`. Stop on an unknown
kind, admission refusal, ownership conflict or namespace error; do not remove a
policy, security context, digest, or selector to coax admission.

## Readiness and installed-state checks

```bash
kubectl rollout status --namespace "$gateway_namespace" \
  deployment/cairn-egress-gateway --timeout=420s
kubectl wait --namespace "$gateway_namespace" --for=condition=Ready \
  pod --selector app.kubernetes.io/name=cairn-egress-gateway --timeout=420s

kubectl get namespace "$gateway_namespace" -o json | jq -e '
  .metadata.labels["app.kubernetes.io/name"] == "cairn-egress-gateway" and
  (.metadata.labels["cairn.example.invalid/instance"] == null)'
kubectl get deployment/cairn-egress-gateway --namespace "$gateway_namespace" \
  -o json | jq -e '
    .spec.replicas == 1 and .status.observedGeneration == .metadata.generation and
    .status.availableReplicas == 1 and .status.readyReplicas == 1'
kubectl get service/cairn-egress-gateway --namespace "$gateway_namespace" \
  -o json | jq -e '
    .spec.type == "ClusterIP" and
    .spec.ports == [{"name":"proxy","port":3128,"protocol":"TCP","targetPort":"proxy"}]'
kubectl get networkpolicy --namespace "$gateway_namespace" -o json | jq -e '
  [.items[] | select(.metadata.labels["app.kubernetes.io/name"] == "cairn-egress-gateway") |
    .metadata.name] | sort == [
      "egress-gateway-allow-cairn-ingress",
      "egress-gateway-allow-egress",
      "egress-gateway-default-deny"
    ]'

expected_acl="$(uv run --locked python - "$gateway_render" <<'PY'
import sys
import yaml
for item in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")):
    if item and item.get("kind") == "ConfigMap" and item["metadata"]["name"] == "egress-gateway-config":
        print(next(line for line in item["data"]["squid.conf"].splitlines()
                   if line.startswith("acl allowed_fqdns dstdomain ")))
        break
else:
    raise SystemExit("gateway ConfigMap missing from reviewed render")
PY
)"
kubectl get configmap/egress-gateway-config --namespace "$gateway_namespace" \
  -o json | jq -e --arg acl "$expected_acl" \
  '.data["squid.conf"] | split("\n") | index($acl) != null'

if test "$gateway_profile" = cilium-reference; then
  kubectl get ciliumnetworkpolicy/egress-gateway-deny-cluster-https \
    --namespace "$gateway_namespace" -o json | jq -e '
      .apiVersion == "cilium.io/v2" and
      (.spec.egressDeny[0].toEntities | sort) ==
        ["cluster", "host", "kube-apiserver", "remote-node"]'
fi
kubectl get pods --namespace "$gateway_namespace" \
  --selector app.kubernetes.io/name=cairn-egress-gateway -o wide
```

Expect `successfully rolled out`, one Ready Pod, and `true` from every `jq`
check. The final row must show one `Running` Pod with `1/1` ready. The gateway
is deliberately a single stateless replica and stores no provider credential
or application data.

A Ready Pod proves that Squid accepted the mounted configuration and listens
on its Service port. It does not prove external delivery, DNS-policy matching,
or access from an instance namespace. Install the reviewed
`kubernetes-retrieval` instance, preserve its default-deny and exact gateway
destination selectors, and use the [bounded client verification](../clients.md#bounded-ingest-and-retrieval-verification)
to prove the approved provider path. Do not create a privileged diagnostic Pod,
temporarily allow direct internet access, or broaden a policy as a test.

## Allow-list changes

The ConfigMap is mounted with `subPath`, so a later allow-list apply does not
change the running Pod. Regenerate and review the complete manifest, record its
new digest, apply it without `--force-conflicts`, then deliberately restart and
wait for the Deployment:

```bash
kubectl rollout restart --namespace "$gateway_namespace" \
  deployment/cairn-egress-gateway
kubectl rollout status --namespace "$gateway_namespace" \
  deployment/cairn-egress-gateway --timeout=420s
```

Repeat the installed-state checks and the bounded retrieval verification for
every affected Cairn instance. A restart keeps the Namespace, ConfigMap,
Service and policies. Do not use namespace deletion as an uninstall or repair
command.

## Remove the shared gateway safely

Removal is a separate, cluster-wide change. It interrupts provider delivery for
every retrieval-enabled instance, so do it only when the cluster administrator
has confirmed that no retrieval-enabled Cairn instance remains. The gateway
record is the ownership boundary: use the exact committed `gateway.yaml` and
`namespace.yaml` from the site GitOps checkout that was applied. Do not delete
the namespace first and do not substitute a hand-written selector.

First verify the intended cluster, the clean committed record and the absence of
instance namespaces. The conservative check below refuses to continue while
any Cairn instance namespace still carries the gateway access label; remove or
disable those instances through their own reviewed procedure first.

```bash
set -eu
set -o pipefail
gateway_namespace=cairn-egress
gateway_gitops_root="$HOME/cairn-site-gitops"
gateway_gitops_root="$(realpath -e -- "$gateway_gitops_root")"
test "$(git -C "$gateway_gitops_root" rev-parse --show-toplevel)" = \
  "$gateway_gitops_root"
gateway_record_dir="$gateway_gitops_root/cairn-egress"
gateway_render="$gateway_record_dir/gateway.yaml"
gateway_namespace_render="$gateway_record_dir/namespace.yaml"
gateway_objects="$(mktemp)"
gateway_inventory="$(mktemp)"
trap 'rm -f "$gateway_objects" "$gateway_inventory"' EXIT
test -s "$gateway_render"
test -s "$gateway_namespace_render"
gateway_record_relative="${gateway_record_dir#"$gateway_gitops_root"/}"
test "$gateway_record_relative" != "$gateway_record_dir"
for file in gateway.yaml namespace.yaml manifests.sha256 profile source-revision; do
  git -C "$gateway_gitops_root" ls-files --error-unmatch -- \
    "$gateway_record_relative/$file" >/dev/null
done
test -z "$(git -C "$gateway_gitops_root" status --porcelain -- \
  "$gateway_record_relative")"

# Derive a deletion input which cannot contain the Namespace. Refuse a changed
# record shape instead of trusting a hand-edited filter.
uv run --locked python - "$gateway_render" "$gateway_objects" \
  "$(cat "$gateway_record_dir/profile")" <<'PY'
from pathlib import Path
import sys

import yaml

source, destination, profile = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
documents = [item for item in yaml.safe_load_all(source.read_text()) if item]
objects = {(item["kind"], item["metadata"]["name"]): item for item in documents}
expected = {
    ("Namespace", "cairn-egress"),
    ("ConfigMap", "egress-gateway-config"),
    ("Service", "cairn-egress-gateway"),
    ("Deployment", "cairn-egress-gateway"),
    ("NetworkPolicy", "egress-gateway-default-deny"),
    ("NetworkPolicy", "egress-gateway-allow-cairn-ingress"),
    ("NetworkPolicy", "egress-gateway-allow-egress"),
}
if profile == "cilium-reference":
    expected.add(("CiliumNetworkPolicy", "egress-gateway-deny-cluster-https"))
if set(objects) != expected or len(objects) != len(documents):
    raise SystemExit("gateway record does not contain the exact expected objects")
namespaced = []
for item in documents:
    if item["kind"] == "Namespace":
        continue
    if item["metadata"].get("namespace") != "cairn-egress":
        raise SystemExit("a gateway object escaped the fixed namespace")
    namespaced.append(item)
destination.write_text(yaml.safe_dump_all(namespaced, sort_keys=False))
PY

kubectl config current-context
gateway_label="$(kubectl get namespace "$gateway_namespace" \
  -o jsonpath='{.metadata.labels.app\.kubernetes\.io/name}')"
test "$gateway_label" = cairn-egress-gateway
test -z "$(kubectl get namespaces \
  -l cairn.example.invalid/instance -o name)"
kubectl get namespace "$gateway_namespace" -o name
kubectl get -f "$gateway_objects" --ignore-not-found
kubectl get -f "$gateway_namespace_render"
```

Review that inventory with the administrator. The next command is a server-side
dry run of exactly the recorded objects; it must name only the gateway objects
and must not include an unexpected kind or namespace. If it does, stop and
repair the GitOps record rather than widening the delete command.

```bash
kubectl delete --dry-run=server --wait=false --ignore-not-found \
  -f "$gateway_objects"
```

After that review, remove the namespaced gateway objects and verify that they
are gone. The Namespace is deliberately retained at this point so a later
administrator can inspect the empty boundary and its audit history.

```bash
kubectl delete --wait=true --ignore-not-found -f "$gateway_objects"
kubectl get -f "$gateway_objects" --ignore-not-found
```

Deleting the dedicated namespace is optional and requires one additional
inventory. Run this only when the namespace was created for this gateway and
contains no objects except the controller-created `default` ServiceAccount and
`kube-root-ca.crt` ConfigMap; otherwise leave the namespace in place for the
administrator.

```bash
test "$(kubectl get namespace "$gateway_namespace" \
  -o jsonpath='{.metadata.labels.app\.kubernetes\.io/name}')" = \
  cairn-egress-gateway
kubectl api-resources --verbs=list --namespaced -o name | while read -r resource; do
  kubectl get --namespace "$gateway_namespace" "$resource" \
    --ignore-not-found -o name >> "$gateway_inventory"
done
sort -u -o "$gateway_inventory" "$gateway_inventory"
uv run --locked python - "$gateway_inventory" <<'PY'
from pathlib import Path
import sys

observed = set(Path(sys.argv[1]).read_text().splitlines())
allowed = {"serviceaccount/default", "configmap/kube-root-ca.crt"}
unexpected = sorted(observed - allowed)
if unexpected:
    raise SystemExit("namespace contains unexpected objects: " + ", ".join(unexpected))
PY
```

Only after that check succeeds may the administrator run:

```bash
kubectl delete --wait=true --ignore-not-found \
  -f "$gateway_namespace_render"
```

Confirm the namespace and all recorded objects are absent, then record the
removal commit and cluster change in the site's normal audit trail. Never use
`kubectl delete namespace cairn-egress` as a shortcut: it can remove unrelated
objects and bypasses the ownership and empty-inventory checks above.
