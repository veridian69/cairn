# Legacy Task 11a Kubernetes posture harness

The filename and `reference`/Task 11a terms are retained because the existing
scripts and tests use that historical harness nomenclature. This document
describes the reusable security invariants and recovery flow; it does not
publish a target machine configuration or claim that the harness proves a
different cluster.

Use `deploy/kustomize/overlays/kubernetes-retrieval` for a complete
retrieval-enabled Kubernetes instance. The target-specific posture harness
also expects its matching egress-gateway overlay, a Cilium cluster and
pre-existing baseline inventory. Other clusters need their own reviewed
policy and acceptance procedure.

## Reference-site settings

The harness contains no default host or baseline evidence identity. Export the
following settings before preflight; an unset or empty value is a refusal:

| Variable | Meaning |
| --- | --- |
| `REFERENCE_EXPECTED_HOST` | Short hostname of the machine executing host checks. |
| `REFERENCE_NODE_NAME` | Kubernetes Node object examined by preflight. This may differ from the host short name. |
| `REFERENCE_BASELINE_NAMESPACE` | Namespace retained by the accepted baseline run. |
| `REFERENCE_GATEWAY_NAMESPACE` | Namespace containing the shared egress gateway. |
| `REFERENCE_STORAGE_CLASS` | Local storage class whose PV inventory is checked. |
| `REFERENCE_BASELINE_REVISION` | Full 40-character revision recorded by the accepted baseline. |
| `REFERENCE_BASELINE_RUN_ID` | Run identifier recorded on baseline-owned resources. |
| `REFERENCE_BASELINE_PRIMARY_SHA256` | Expected SHA-256 of the baseline primary report. |
| `REFERENCE_BASELINE_CILIUM_SHA256` | Expected SHA-256 of the baseline Cilium supplement. |
| `REFERENCE_POSTURE_TASK11_PRIMARY_REPORT` | Absolute path to the baseline primary report. |
| `REFERENCE_POSTURE_TASK11_CILIUM_REPORT` | Absolute path to its Cilium supplement. |

The earlier baseline acceptance harness likewise requires an explicit run ID,
report path, expected host identity and its owned namespaces:

```text
REFERENCE_ACCEPTANCE_RUN_ID
REFERENCE_ACCEPTANCE_REPORT
REFERENCE_EXPECTED_HOST
REFERENCE_NODE_NAME
REFERENCE_EXPECTED_DISTRIBUTION
REFERENCE_EXPECTED_SELINUX
REFERENCE_PROVIDER_SECRET_FILE
REFERENCE_ACCEPTANCE_NAMESPACE
REFERENCE_GATEWAY_NAMESPACE
REFERENCE_FOREIGN_NAMESPACE
REFERENCE_ENFORCEMENT_NAMESPACE
REFERENCE_SHAPES_NAMESPACE
```

`REFERENCE_ACCEPTANCE_REPORT` must remain below
`build/reference-acceptance/`. `REFERENCE_PROVIDER_SECRET_FILE` names an
owner-only input file; the harness checks its metadata without printing its
contents. Cluster Pod, Service and node addresses remain observed inputs.
RFC1918, loopback and link-local CIDRs in the validator are generic denial
expectations, not a published site network.

## Gateway label

`cairn.example.invalid/instance` is a grant. It belongs only on a namespace
whose owner, instance identity and lifecycle match the Cairn deployment.
Copying it to another namespace permits matching Cairn-labelled Pods there to
reach the shared egress gateway.

Restrict namespace-label updates to cluster operators. Inventory every
namespace carrying the label before a gateway change and during an incident.
Keep the label during decommissioning until workloads have stopped and the
retained-volume decision is complete.

## Bind source, image and run identity

Run from a clean checkout at the exact accepted revision. Supply the full
40-character lowercase Git revision and derive the local image tag, run ID,
namespace and report path from it:

```sh
: "${REFERENCE_POSTURE_REVIEWED_REVISION:?set the full accepted revision}"
[[ "$REFERENCE_POSTURE_REVIEWED_REVISION" =~ ^[0-9a-f]{40}$ ]]
reviewed_revision=$REFERENCE_POSTURE_REVIEWED_REVISION
[[ "$(git rev-parse --verify HEAD)" == "$reviewed_revision" ]]

export IMAGE="cairn:task11a-${reviewed_revision:0:12}"
export REFERENCE_POSTURE_RUN_ID="task11a-${reviewed_revision:0:12}-01"
export REFERENCE_POSTURE_REPORT="build/reference-posture/${reviewed_revision}-${REFERENCE_POSTURE_RUN_ID}.json"
```

The harness uses
`task11a-<7-to-12-character-revision-prefix>-<two-decimal-digits>` and the
namespace `cairn-<run-id>`. The image is a locally built harness tag. It is not
a published Cairn release image.

Each run needs a fresh UUIDv4, unused report paths and unclaimed retained PVs.
Bind operator-supplied baseline report paths and credential source paths
without printing their contents. Credential files must be non-empty regular
files, not symlinks, and accessible only to the operator account.

The accepted revision must pin:

- the Kubernetes client and Python environment;
- source render digests;
- runtime UID, GID and supplemental groups;
- the expected gateway object inventory;
- PV identities and paths reserved for the run; and
- finite timeouts for every mutation, probe and cleanup step.

## Build and prove image identity

Build only from the bound revision, import the image into the cluster runtime
and record the OCI target, platform-manifest and configuration digests:

```sh
make image IMAGE="$IMAGE" REVISION="$reviewed_revision"
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")" == \
  "$reviewed_revision" ]]
docker save "$IMAGE" | ctr --namespace k8s.io images import - >/dev/null
image_provenance=$(./.venv/bin/python -I -B \
  scripts/reference_acceptance_state.py resolve-oci-provenance "$IMAGE")
jq -e '
  (.reference | type == "string" and length > 0) and
  (.target_digest | test("^sha256:[0-9a-f]{64}$")) and
  (.platform_manifest_digest | test("^sha256:[0-9a-f]{64}$")) and
  (.config_digest | test("^sha256:[0-9a-f]{64}$"))' \
  <<<"$image_provenance" >/dev/null
```

The legacy renderer accepts a canonical non-`latest` named tag and binds its
resolved digests during preflight. Digest-form input is refused by this
harness because that renderer writes Kustomize `newName` and `newTag`.

## Preflight and execution

Provision the repository's locked tools and isolated Python environment. Do
not substitute an ambient interpreter or client:

```sh
./scripts/fetch-kubectl
./scripts/fetch-uv
build/tools/uv sync --locked
./.venv/bin/python -I -B -c 'import cairn, yaml'
```

Run the read-only preflight first:

```sh
./scripts/reference-posture-acceptance preflight
```

Preflight must verify the cluster, Cilium and client identities, storage
class, render digests, tools, credentials, reserved PVs and the complete
baseline gateway inventory. Any refusal stops the run. Do not alter cluster
state merely to make preflight pass.

After preflight succeeds, launch the same bound harness:

```sh
make reference-posture-launch
```

The launcher records its report, progress journal and log paths. Treat
timeouts as failures. The run must prove, at minimum:

- admitted and live image provenance;
- actual PV mount roots, ownership and write permissions;
- SQLite WAL, lease exclusion, restart and integrity behaviour;
- FalkorDB authentication, persistence and restart;
- DNS and allowed provider delivery through the egress gateway;
- denied cluster, host, node, API-server and unapproved external paths, each
  with a positive control; and
- projection progress, drain and retrieval using the effective adapter
  configuration.

Temporary Pods, Services, policies, listeners and host firewall rules must
have explicit ownership, bounded lifetimes and read-back absence checks.
Retained instance resources, claims and immutable reports stay outside
automatic cleanup.

## Immutable outcomes and recovery

Success and failure reports are separate, exclusively created files. A failed
report cannot be overwritten or converted into success. Once mutation starts,
preserve the report and inventory the retained namespace, Secrets,
StatefulSets, PVCs, PV bindings and gateway state before choosing recovery.

Do not clear PV claim references, wipe retained volumes, delete resources by a
broad label, edit reports or relax gateway policy as an ad-hoc repair.

Before rerunning, restore the exact accepted Task 11 gateway pre-state. Remove
only the failed run's owned narrowed policy, restore the baseline broad policy
with its original ownership labels, and verify the complete baseline gateway
inventory. Complete and review that recovery before allocating a new Task 11a run ID.
A fresh identifier does not repair an incorrect pre-state.

A successful report applies only to its bound revision, run and cluster. A
single-node run does not establish cross-node enforcement, and a
vanilla-Kubernetes run does not establish OpenShift support.
