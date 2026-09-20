# Install Garden with Cairn

On Linux, add `--garden-config /absolute/path/garden.json` to a new native, Docker or
Kubernetes installation. Garden becomes part of the named installation's
`status`, `resume`, `rollback` and `blitz` lifecycle. Disposable mode remains a
Cairn-only smoke test. This option does not adopt an existing Garden installation.
**Garden is not supported on macOS.** Run Garden on Linux. macOS native mode
supports Cairn catalogue memory and Attic without Garden.

## Configure HTTPS and participants

Arrange a non-loopback DNS name reachable from the agent machines and a
certificate whose subject alternative names cover it. `localhost`, names under
`.localhost`, and loopback IP addresses are not valid public Garden endpoints. Supply the PEM certificate chain and key
as regular files owned by the installer user; keep the key mode `0600`. An
internal CA is supported: include its PEM bundle as `tls_ca_file`, then install
that CA or use the generated CA setting on each adapter machine. Verification
does not skip certificate or hostname checks.

An internal CA certificate must declare `basicConstraints` with `CA:TRUE` and
`keyUsage` with `keyCertSign` (normally also `cRLSign`). The Garden leaf must
have a DNS subject alternative name for the endpoint host and `extendedKeyUsage`
with `serverAuth`. Python/OpenSSL enforces these properties even when `openssl
s_client` or `curl` accepts a looser chain. The installer reports the endpoint
hostname and the underlying OpenSSL verification reason when this check fails.

`port` is the local Garden listener and must be 1024–65535. The public endpoint
may use a different port, for example `https://garden.example.net/mcp` on 443
through an existing TLS-forwarding ingress to the local listener on 8443.

For a private test or lab CA, the repository helper generates and validates the
CA, server key and certificate chain, then saves a complete `garden.json` beside
them and prints the same configuration:

```sh
scripts/generate-garden-tls garden.example.net "$HOME/garden-tls"
```

The generated file includes absolute certificate paths, port 8443, an
`engineering` Garden scope, Val/Codex and Spike/Claude participants, internal
classification and an expiry one year ahead. Review those choices before use;
pass an optional third argument to set both the listener and endpoint port,
for example `scripts/generate-garden-tls garden.example.net "$HOME/garden-tls-2" 8444`.
Pass the file itself to the installer:

```sh
./cairn-install --mode docker --garden-config "$HOME/garden-tls/garden.json"
```

Existing certificates, keys and `garden.json` are never overwritten. Run
`scripts/generate-garden-tls` with no arguments to see its usage and a complete
example. Managed sites should use their site PKI.

Example `/home/cairn/garden.json` (replace paths, DNS, scope and expiry):

```json
{
  "endpoint": "https://garden.example.net:8443/mcp",
  "port": 8443,
  "tls_cert_file": "/home/cairn/tls/garden-chain.pem",
  "tls_key_file": "/home/cairn/tls/garden-key.pem",
  "tls_ca_file": "/home/cairn/tls/internal-ca.pem",
  "scope": {
    "realm": "local",
    "segments": [{"kind": "garden", "identifier": "engineering"}]
  },
  "classification": "internal",
  "participants": {
    "spike": "claude",
    "val": "codex",
    "opencode": "opencode"
  },
  "expires_at": "2027-09-01T00:00:00Z"
}
```

Every participant receives its own workload principal, credential and expiring
grant in the new Cairn. The grant covers only the complete configured scope and
classification. Preserve any required job/run scope segments. Participants share
one Garden room; addressed messages route attention, they are not private rooms.
An OpenCode participant name denotes an adapter configuration, not a model-family
identity: choose the real participant name for the agent running there.

The expiry is intentional. Resume checks existing credentials and permissions;
it does not extend expiry, undo revocation or mint replacement credentials behind
your back. If a one-time credential response is lost before protected capture,
installation stops with recovery information rather than claiming success.

## Native and Docker

From the trusted Cairn checkout, choose one:

```sh
./cairn-install --non-interactive --mode native --name team \
  --garden-config "$HOME/garden-tls/garden.json"

./cairn-install --non-interactive --mode docker --name team-docker \
  --garden-config "$HOME/garden-tls/garden.json"
```

Native mode builds Garden from the included `a2a` Go module and needs its Go
toolchain dependencies. The build stamps the release version from
`pyproject.toml` (`instance/garden/bin/a2a --version`), and keeps Go's build
and module caches under `instance/garden/go-build` and
`instance/garden/go-mod` in the installation state directory, so `blitz`
removes them with everything else. The Go toolchain's own telemetry directory
(under the user's config directory) is outside installer control; disable it
with `go telemetry off` if your policy requires. Native mode installs a
separate systemd user unit alongside Cairn's unit. Both use the existing
dedicated installer account; separate directories do not provide an
operating-system security boundary between those same-user processes. The
ordinary native lingering/login requirements apply.

Docker builds a separate Garden image and stamps the release version from
`pyproject.toml`. Guided builds record `io.cairn.source.digest` as the source
fingerprint; a clean checkout also records its Git commit under
`org.opencontainers.image.revision`. Dirty or history-free sources do not claim
a VCS revision. Garden shares Cairn's network namespace,
so credential diagnosis goes to `127.0.0.1:8000`; it receives its own data and
TLS/configuration mounts. Garden's TLS port is published by the Cairn container,
which owns that namespace. Cairn's own host publication remains loopback-only.
Permit the Garden port through the host firewall and route the configured DNS
name to it. The installer does not alter your firewall or public DNS.

## Kubernetes

Use the existing [Kubernetes installer prerequisites](guided-installation.md).
Garden supports either a registry-published image or a locally built image
staged into the eligible nodes' containerd caches. Both paths require an
immutable image digest. `--kube-preloaded-image` controls Cairn only; Garden's
local path requires its own `--kube-garden-receipt`.

For a registry, build `a2a/Dockerfile` with `a2a/` as its context, publish through
your normal image pipeline, and arrange node pull access. Without a Garden
receipt the installer uses `imagePullPolicy: Always`.

### Build and stage Garden without a registry

Run from the trusted Cairn checkout on Linux/amd64 with Docker, Python 3,
`kubectl`, SSH and the [node staging prerequisites](../../deploy/falkordb/README.md).
Nodes must be Ready, schedulable Linux/amd64 containerd nodes, with no
`NoSchedule` or `NoExecute` taints. The storage class must support
`ReadWriteOncePod` and use `WaitForFirstConsumer`. Select nodes that can mount
the instance's volumes. The installer constrains Cairn and Garden placement to
these nodes before creating or binding their storage.

Set the actual context and Kubernetes-node-to-SSH mapping; the names need not
match. Add a separate `--node NODE=SSH_ALIAS` argument for every eligible node
you select. Each SSH alias needs the documented non-interactive sudo access.
The example uses `reference` for both names; replace it for another cluster.

```bash
set -eu
kube_context=kubernetes-admin@kubernetes
kube_node=reference
node_ssh=reference
if test -n "$(git status --porcelain --untracked-files=all)"; then
  printf 'Build from a clean trusted checkout so the image revision is accurate.\n' >&2
  exit 2
fi
garden_version="$(uv run --locked python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
docker build --build-arg VERSION="$garden_version" \
  --build-arg REVISION="$(git rev-parse HEAD)" \
  --tag cairn-garden:local --file a2a/Dockerfile a2a
garden_digest="$(docker image inspect cairn-garden:local --format '{{.Id}}')"
garden_tag="cairn.local/garden:build-${garden_digest#sha256:}"
garden_image="cairn.local/garden@$garden_digest"
mkdir -p build
garden_stage_dir="$(mktemp -d "$PWD/build/garden-image-stage.XXXXXX")"
docker image tag cairn-garden:local "$garden_tag"
docker image save --output "$garden_stage_dir/image.tar" "$garden_tag"
python3 scripts/kubernetes_image_stage.py \
  --context "$kube_context" --archive "$garden_stage_dir/image.tar" \
  --image "$garden_image" --node "$kube_node=$node_ssh" \
  --output "$garden_stage_dir/kubernetes-receipt.json"
printf 'Garden staging directory: %s\n' "$garden_stage_dir"
```

Each run creates a fresh staging directory; keep its printed path in your installation record.
Keep the archive and receipt. The staging helper verifies the archive, imports
it into each selected node's containerd `k8s.io` namespace, verifies the exact
image and records the node names and UIDs. It does not publish an image.
The receipt is private and must not be writable by group or others; do not
hand-edit it. At installation and resume, the installer checks current node
identity and eligibility and executes the exact Garden image on each recorded
node with `imagePullPolicy: Never`. It rejects missing caches or replaced nodes
before proceeding. The stored archive checksum records staging provenance;
installation does not re-read the archive.

### Complete the configuration and install

Add these fields to the Garden configuration using the
resulting immutable digest (`$garden_image` for local staging) and the actual
client/ingress source networks:

```json
{
  "image": "registry.example.net/garden@sha256:REPLACE_WITH_64_HEX_DIGEST",
  "kubernetes_service_type": "LoadBalancer",
  "allowed_cidrs": ["192.0.2.0/24"]
}
```

These are additional fields in the same JSON document, not a second document.
`192.0.2.0/24` is a documentation network; replace it. Choose `ClusterIP` when
using an existing ingress or TCP proxy instead. That choice alone does not make
Garden reachable from another machine. Forward TLS without dropping end-to-end
certificate verification, and configure policy CIDRs for the source addresses
the cluster actually observes.

For the locally staged path, the block below adds all three fields to the
existing TLS configuration: the exact image reference from the receipt, the
Service type and the allowed source networks. Set `garden_stage_dir` to the
staging directory the previous block printed; it is a fresh `mktemp` path, so
it cannot be retyped from this guide. Give `garden_allowed_cidrs` as a
comma-separated list of the source networks the cluster observes.

```bash
garden_config="$HOME/garden-tls/garden.json"
garden_stage_dir='REPLACE_WITH_PRINTED_STAGING_DIRECTORY'
garden_service_type=ClusterIP
garden_allowed_cidrs='192.0.2.0/24'
case "$garden_stage_dir:$garden_allowed_cidrs" in
  *REPLACE_WITH_*|*192.0.2.0/24*)
    printf 'Set the printed staging directory and the real client source networks before running\n' >&2
    exit 2
    ;;
esac
test -s "$garden_stage_dir/kubernetes-receipt.json"
python3 - "$garden_config" "$garden_stage_dir/kubernetes-receipt.json" \
  "$garden_service_type" "$garden_allowed_cidrs" <<'PYCONFIG'
import json
import os
from pathlib import Path
import sys

config = Path(sys.argv[1])
value = json.loads(config.read_text())
value["image"] = json.loads(Path(sys.argv[2]).read_text())["image"]
value["kubernetes_service_type"] = sys.argv[3]
value["allowed_cidrs"] = [cidr.strip() for cidr in sys.argv[4].split(",") if cidr.strip()]
os.chmod(config, 0o600)
config.write_text(json.dumps(value, indent=2) + "\n")
PYCONFIG
```

Set the remaining site values, then choose **one** installation command below:

```sh
kube_context='YOUR_CONTEXT'
kube_namespace='YOUR_PREPARED_NAMESPACE'
kube_storage_class='YOUR_RWOP_STORAGE_CLASS'
kube_image='YOUR_CAIRN_IMAGE_WITH_SHA256_DIGEST'
# Local staging: kube_image="$cairn_image" from the guided guide's staging block.
case "$kube_context:$kube_namespace:$kube_storage_class:$kube_image" in
  *YOUR_*)
    printf 'Replace every Kubernetes value with a reviewed site value before running\n' >&2
    exit 2
    ;;
esac
```

For a registry-published Garden image:

```sh
./cairn-install --non-interactive --mode kubernetes --name team-k8s \
  --kube-context "$kube_context" --kube-namespace "$kube_namespace" \
  --kube-storage-class "$kube_storage_class" \
  --kube-image "$kube_image" \
  --garden-config "$HOME/garden-tls/garden.json"
```

For local staging, after setting the Kubernetes values above and completing
`garden.json`, run the full command with the receipt:

```sh
./cairn-install --non-interactive --mode kubernetes --name team-k8s \
  --kube-context "$kube_context" --kube-namespace "$kube_namespace" \
  --kube-storage-class "$kube_storage_class" \
  --kube-image "$kube_image" --kube-preloaded-image \
  --garden-config "$garden_config" \
  --kube-garden-receipt "$garden_stage_dir/kubernetes-receipt.json"
```

This example assumes Cairn was also staged using the linked Kubernetes guide,
whose Cairn preloaded path requires exactly one schedulable node. On a
multi-node cluster use a registry-accessible Cairn image; Garden can still use
its independently staged image and multi-node receipt.
Omit `--kube-preloaded-image` when Cairn itself comes from a registry. Add the
semantic and FalkorDB receipt arguments from that guide if semantic retrieval
is required; the Garden receipt does not replace them.

The `image` in `garden.json` must exactly match the receipt. This selects
`Never` for Garden independently of Cairn's pull policy. Resume reuses the
saved receipt when the argument is omitted and refuses a different supplied
receipt. If a recorded node loses its cache, restage the retained archive to
the same node identity, writing any new receipt to a fresh output path, then
resume. A recreated node has a different UID and requires a new installation;
do not modify the recorded state to bypass that check. Rollback and blitz do
not erase shared node image caches. An administrator may remove staged images
only after checking that no remaining workload needs them.

Garden runs in Cairn's Pod and diagnoses credentials over loopback. It has a
separate retained PVC, TLS Secret, configuration, Service and ingress policy.
Cairn starts first so enrolment can finish before the installer adds Garden.
The offline Cairn bootstrap holder never receives Garden storage or TLS keys.

Garden-enabled pods use `fsGroupChangePolicy: OnRootMismatch` to preserve strict
private file modes across restarts. CSI drivers that implement
`VOLUME_MOUNT_GROUP` override Kubernetes' permission handling; validate an actual
restart with that driver. The installer does not automatically repair altered
file permissions or rebind existing Garden data. See the [Kubernetes volume
permission policy](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/#configure-volume-permission-and-ownership-change-policy-for-pods).

The installer needs namespace permission to manage these additional resources
and patch its owned StatefulSet; it does not provision cluster infrastructure.

## Remote adapters and verification

The final result prints Garden's HTTPS endpoint and the private adapter bundle
directory. Give each agent machine only its participant's profile/token and
required CA material through your existing secure transfer mechanism. Keep
tokens private. Do not copy the server TLS key or Cairn administrator token.

Install the `a2a` binary and helper scripts on each agent machine using
`a2a/scripts/install-user`, then follow the generated bundle README. Only native
installation supplies a host-side binary at `instance/garden/bin/a2a` under the
named installation state directory. Docker and Kubernetes installations run
Garden in a container; install the adapter binary separately on the machine
where you run the verification command, including the installation host if
you choose to verify there. Host-local
paths need to refer to that machine. Codex and OpenCode additionally require the
actual local thread/socket or authenticated session details; generated examples
do not guess them. Use `garden-config --host claude|codex|opencode --profile ...
--binary ... --config ...` to merge the completed profile into the host's MCP
configuration. See [shared Garden and attention adapters](shared-garden.md) for
the exact host launch, hook and `garden-session` lifecycle procedures.

Managed Garden accepts only the hostname and public port in its configured
`endpoint`. The local listener may use a different port; ingress and proxies
must preserve the public HTTP Host while forwarding TLS. Unknown hosts are
rejected rather than treated as aliases.

Installation's `garden_verify` stage verifies authenticated MCP status for the
exact instance, scope, classification and participant bindings, then checks
again after restart. Local and port-forward verification still validates the
public certificate hostname and sends that endpoint's HTTP Host and public port, even over a local port-forward. This is the server-side deployment check.

Create the working profile in the participant bundle directory before running
`doctor`. For the Claude participant, the generated example needs no edits:

```sh
cd /absolute/path/to/participant-bundle
cp claude.profile.example.json profile.json
chmod 600 profile.json
a2a doctor --profile "$PWD/profile.json"
```

For Codex or OpenCode, copy the corresponding generated profile example and
complete the local host details as directed by that bundle's README.
`a2a doctor --profile /absolute/path/profile.json` verifies the authenticated
Garden binding. Add `--host` only when testing the local attention attachment;
that form requires a live local agent host. An `agent host rejected delivery`
result can represent Garden authentication or attachment rejection, so confirm
the profile's participant and credential before diagnosing either side. The
server-side `garden_verify` result and an HTTPS handshake ending in
unauthenticated `401` establish the deployment before adapter bundles are
distributed; each adapter still needs its own DNS, route and firewall test.

## Recovery and removal

```sh
./cairn-install status --name team
./cairn-install resume --name team --non-interactive
./cairn-install rollback --name team --non-interactive
```

`status` reports retained state and the last recorded verification, without a
network probe. `resume` freshly verifies both services and retained agent
credentials. It uses the original pinned options and copied TLS material;
passing changed options is refused. `rollback` stops/removes owned runtime
resources while retaining Garden history, inbox state, credentials and config.
Resume can recreate the owned runtime against that retained data.
Legacy `a2a` snapshot/reset commands do not manage this Garden deployment.

### Recovery

Two conditions stop `resume` from reusing the retained enrolment and need an
operator decision rather than a retry:

- `Garden authority has expired; explicit recovery is required`: the
  `expires_at` in the pinned options has passed. The grants and credentials
  Cairn issued for that window are no longer valid, and the installer never
  extends an authority window in place.
- `needs_credential_recovery` (`Credential plaintext was not captured` or
  `Garden credential capture differs from enrolment`): the one-time credential
  capture under `instance/garden/` is missing, altered or does not match the
  enrolment receipt, so the participant's `agent.token` cannot be proved.

There is no in-place re-issue path. Recover by starting the Garden lifecycle
again with a corrected configuration:

```sh
./cairn-install rollback --name team --non-interactive
# Fix garden.json: a future expires_at, and the same endpoint/participants
# unless you intend a new deployment identity.
./cairn-install blitz --name team
./cairn-install --non-interactive --mode native --name team \
  --garden-config "$HOME/garden-tls/garden.json"
```

`blitz` deletes Garden history and inbox state along with Cairn's catalogue,
and the reinstall enrols fresh principals, credentials and grants. Distribute
the new participant bundles; the previous tokens and profiles stop working.

If an interrupted Kubernetes run reports that `pod/cairn-bootstrap` is still
terminating, wait for that Pod to disappear in the recorded context/namespace,
then rerun `resume`. The installer refuses to replace a still-terminating UID;
do not force-delete the holder or alter its ownership journal.

`blitz` is the destructive removal operation:

```sh
./cairn-install blitz --name team
```

It removes proved-owned Garden runtime resources and storage together with Cairn,
including the catalogue containing Garden's principals/grants. It does not remove
externally managed DNS, certificates, namespace or infrastructure. Interrupted
deletion retains its recovery journal; rerun the same command. A replaced unit,
container, PVC or Kubernetes object is not adopted or deleted merely because its
name matches.
