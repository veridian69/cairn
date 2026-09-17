# The overlays

Six applyable configurations, each rendered and committed under
`../rendered/` with a digest beside it (P-61). Apply a render, not a
kustomization: what a reviewer read in the diff is then exactly what the
cluster received.

| Overlay | Rendered as | What it is |
| --- | --- | --- |
| `kubernetes/` | `kubernetes.yaml` | Vanilla Kubernetes. The base plus the explicitly named `cairn-local` storage class (I-10). No retrieval index. |
| `openshift/` | `openshift.yaml` | OpenShift. Deliberately empty — the base was written to need nothing here. Rendered and statically validated; nothing further is claimed until a cluster proves it (I-14). |
| `kind/` | `kind.yaml` | The disposable acceptance tier, and the worked example of an instance with retrieval switched on. **Never a production target.** |
| `egress-gateway/` | `egress-gateway.yaml` | The cluster's one outbound route (I-93). Applied once per cluster, not once per instance. |
| `kubernetes-retrieval/` | `kubernetes-retrieval.yaml` | The only supported complete retrieval-enabled vanilla-Kubernetes instance path: Cairn, FalkorDB, proxy, gateway allowance and both `cairn-local` `ReadWriteOncePod` claims. |
| `egress-gateway-reference/` | `egress-gateway-reference.yaml` | The reference-only strengthening of the shared gateway: the ordinary NetworkPolicy address exclusions plus the Cilium TCP/443 entity deny. It is not portable material. |

## What an operator supplies per instance

The instance overlays render one instance's resources with placeholder
identity. Before applying, per instance:

1. **A namespace**, labelled `cairn.example.invalid/instance: <name>`.
   One instance per namespace: the namespace *is* the isolation
   boundary, and the gateway admits only pods in namespaces carrying
   that label.
2. **`instance_id`** in the ConfigMap — a real UUID, unique per
   instance. The placeholder is not a valid UUID, so forgetting fails
   configuration loading rather than passing silently (I-41).
3. **The `cairn-credentials` Secret** — never rendered, never committed
   (I-08, I-19). With the index enabled it carries `falkordb-password`
   and `openai-api-key`, plus `falkordb.conf` — the index's own
   `requirepass` line, written from the same token as
   `falkordb-password` — and optionally `falkordb-username`.
   `../components/falkordb/README.md` creates all of them in one step.
4. **The bootstrap step**, before the StatefulSet first serves. See
   `../base/README.md`; it is a one-off Pod, not an init container.

## Turning retrieval on

`kubernetes/` and `openshift/` render no FalkorDB resource and no proxy
variable at all — I-91's "included by choice", made mechanical rather
than promised. For a complete production retrieval instance, use
`deploy/kustomize/overlays/kubernetes-retrieval`; it is the only supported
complete retrieval instance path. Do not assemble an equivalent deployment by
patching `kubernetes/`, `kind/`, or the base ad hoc.

The retrieval instance needs the cluster's gateway applied once. On reference,
that prerequisite is `egress-gateway-reference`, not the broad
`egress-gateway` render: it depends on a working Cilium
`CiliumNetworkPolicy` CRD and its target-specific policy is what denies
gateway TCP/443 access to cluster, host, remote-node and kube-apiserver
identities. Do not apply that Cilium overlay to another Kubernetes target or
to OpenShift. Without the gateway, a retrieval instance starts, passes its
probes and fails at its first provider call — the base default-deny is what
stops it going directly.

The reference execution, preflight, label-governance and failure boundary are in
[`runbook-reference-task11a-posture.md`](../../../docs/runbooks/runbook-reference-task11a-posture.md).
