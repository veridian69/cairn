# Install Garden with Cairn

On Linux, add `--garden-config /absolute/path/garden.json` to a new native, Docker or
Kubernetes installation. Garden becomes part of the named installation's
`status`, `resume`, `rollback` and `blitz` lifecycle. Disposable mode remains a
Cairn-only smoke test. This option does not adopt an existing Garden installation.
**Garden is not supported on macOS.** Run Garden on Linux. macOS native mode
supports Cairn catalogue memory and Attic without Garden.

## Configure HTTPS and participants

Arrange a DNS name reachable from the agent machines and a certificate whose
subject alternative names cover it. Supply the PEM certificate chain and key
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
CA, server key and certificate chain, then prints matching Garden JSON:

```sh
scripts/generate-garden-tls garden.example.net /home/cairn/garden-tls
```

Run `scripts/generate-garden-tls` with no arguments to see its usage and a
complete example. Managed sites should use their site PKI.

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
  --garden-config /home/cairn/garden.json

./cairn-install --non-interactive --mode docker --name team-docker \
  --garden-config /home/cairn/garden.json
```

Native mode builds Garden from the included `a2a` Go module and needs its Go
toolchain dependencies. It installs a separate systemd user unit alongside
Cairn's unit. Both use the existing dedicated installer account; separate
directories do not provide an operating-system security boundary between those
same-user processes. The ordinary native lingering/login requirements apply.

Docker builds a separate Garden image. Garden shares Cairn's network namespace,
so credential diagnosis goes to `127.0.0.1:8000`; it receives its own data and
TLS/configuration mounts. Garden's TLS port is published by the Cairn container,
which owns that namespace. Cairn's own host publication remains loopback-only.
Permit the Garden port through the host firewall and route the configured DNS
name to it. The installer does not alter your firewall or public DNS.

## Kubernetes

Use the existing [Kubernetes installer prerequisites](guided-installation.md).
Build and publish `a2a/Dockerfile` using `a2a/` as its build context through your
normal image pipeline. Add these fields to the Garden configuration using the
resulting immutable digest and the actual client/ingress source networks:

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

```sh
kube_context='YOUR_CONTEXT'
kube_namespace='YOUR_PREPARED_NAMESPACE'
kube_storage_class='YOUR_RWOP_STORAGE_CLASS'
kube_image='YOUR_CAIRN_IMAGE_WITH_SHA256_DIGEST'
case "$kube_context:$kube_namespace:$kube_storage_class:$kube_image" in
  *YOUR_*)
    printf 'Replace every Kubernetes value with a reviewed site value before running\n' >&2
    exit 2
    ;;
esac
./cairn-install --non-interactive --mode kubernetes --name team-k8s \
  --kube-context "$kube_context" --kube-namespace "$kube_namespace" \
  --kube-storage-class "$kube_storage_class" \
  --kube-image "$kube_image" \
  --garden-config /home/cairn/garden.json
```

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
`a2a/scripts/install-user`, then follow the generated bundle README. Host-local
paths need to refer to that machine. Codex and OpenCode additionally require the
actual local thread/socket or authenticated session details; generated examples
do not guess them. Use `garden-config --host claude|codex|opencode --profile ...
--binary ... --config ...` to merge the completed profile into the host's MCP
configuration. See [shared Garden and attention adapters](shared-garden.md) for
the exact host launch, hook and `garden-session` lifecycle procedures.

Installation's `garden_verify` stage verifies authenticated MCP status for the
exact instance, scope, classification and participant bindings, then checks
again after restart. Local and port-forward verification still validates the
public certificate hostname. This is the server-side deployment check.

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
