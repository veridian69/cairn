# Install Cairn

**Semantic search requires an OpenAI API key.** Basic installation and Attic
write/read checks need no OpenAI key. The key is separate from your Cairn
administrator credential; follow your chosen guide's semantic setup to store
it securely before enabling search.

Choose one path before installing tools. These instructions target Linux
`x86_64`, including WSL with a native Linux checkout; the persistent service
requires a working systemd user manager. Keep the checkout, Python environment
and data on Linux storage, not `/mnt/c`. Other platforms and architectures
have not completed this installation acceptance exercise.

Passing developer tests is separate from following these instructions as a new
user. The Kubernetes path installs Cairn into an existing cluster; creating or
repairing the cluster is separate work.

| Purpose | Follow this procedure | What remains afterwards |
| --- | --- | --- |
| Try the native service | [Disposable quickstart](quickstart.md) | Nothing: the script stops the server and deletes its temporary data and credential. |
| Keep a native instance | [Persistent native installation](operations/native-installation.md) | Stable identity, owner-controlled data and credential, a systemd user service and backup procedure. |
| Run Cairn with Attic and optional semantic search | [Docker Compose installation](../deploy/compose/README.md) | Persistent volumes, protected credentials and a managed container stack. |
| Deploy Cairn to an existing cluster | [Kubernetes installation](#kubernetes-installation) | A dedicated namespace, persistent claims, restricted workloads and network policies. |
| Develop Cairn | [Full developer validation](#full-developer-validation) | A development environment; this does not install a persistent service. |

## Obtain a trusted checkout

Use the repository URL supplied by your distributor. In the following block,
replace `REPLACE_WITH_TRUSTED_REPOSITORY_URL` with that URL; it is the only
substitution. Do not put a password or token in the URL. Use your Git client's
credential manager if the distributor requires authentication.

```sh
mkdir -p "$HOME/projects"
cd "$HOME/projects"
repository_url='REPLACE_WITH_TRUSTED_REPOSITORY_URL'
test "$repository_url" != REPLACE_WITH_TRUSTED_REPOSITORY_URL
git clone "$repository_url" cairn
cd cairn
git rev-parse HEAD
```

If you already have this checkout, open a terminal in its root instead. Unless
a guide explicitly changes directory, commands run from the root containing
`pyproject.toml`, `uv.lock` and `deploy/`. Record the revision printed above for
acceptance. Initial source installation needs access to the locked Python
package indexes; Docker additionally needs access to the image registries.

## Get missing prerequisites

Check versions **before** following a procedure. A missing command is a failed
prerequisite, not a reason to improvise an alias. On Ubuntu 24.04 x86_64, an
administrator can install the common shell tools and native Python with:

```sh
sudo apt-get update
sudo apt-get install --yes git bash coreutils curl jq python3.12
```

The Compose and developer paths also need `make` and `sed`:

```sh
sudo apt-get install --yes make sed
```

Install `openssl` only for optional semantic retrieval:

```sh
sudo apt-get install --yes openssl
```

For another Linux distribution, have its administrator install the equivalent
packages through its normal package policy. Native service installation also
requires systemd user services and `loginctl`; installing a package alone does
not enable a user manager on a host without systemd. The native guide checks
that manager before writing configuration.

For native paths, install uv 0.12.0 using the
[uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
with that pinned version. For example, after reviewing the downloaded script:

```sh
uv_installer="$(mktemp)"
curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://astral.sh/uv/0.12.0/install.sh -o "$uv_installer"
# Read the downloaded installer before executing it.
cat "$uv_installer"
sh "$uv_installer"
rm -f "$uv_installer"
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Expect `uv 0.12.0`. Add `$HOME/.local/bin` to your shell's normal startup PATH
if it is not already there. The commands above set it for the current session.
The disposable path can use `uv python install 3.12` when the distribution has
no Python 3.12. For the persistent guide's explicit `python3.12` checks, install
that executable through the host package policy first.

Docker users should follow the official
[Engine installation](https://docs.docker.com/engine/install/) and
[Compose plugin installation](https://docs.docker.com/compose/install/linux/)
for their distribution. Access to the Docker socket is effectively root access.
Have an administrator grant it only to a trusted operator; do not blindly add
users to a privileged group. The Compose guide describes its required `sudo`
file-ownership operations before first boot.

## Disposable native quickstart prerequisites

Required: Linux x86_64, Bash, Git, coreutils (including `mktemp`, `chmod`,
`sha256sum`, `tr` and `rm`), curl 8.4.0 or newer, jq 1.6 or newer, a system
Python 3 available as `python3`, and uv 0.12.0 with Cairn’s Python 3.12 runtime. You need a writable checkout and `/tmp`, an unused loopback port
8000, and network access for locked dependency installation. You do not need
Go, Bubblewrap, kubectl, Docker or administrator access to run the quickstart.

```sh
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
for tool in bash git mktemp chmod sha256sum tr rm curl jq python3 uv; do
  command -v "$tool" || exit 1
done
curl --version
jq --version
python3 --version
uv --version
uv python find 3.12
test -w .
test -w /tmp
python3 - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 8000))
PY
```

Expect an executable path for every tool, curl 8.4.0 or newer, jq 1.6 or newer,
and uv `0.12.0`. `python3 --version` reports the system Python and may show
`3.14.x`; it is used here only for the standard-library socket check.
`uv python find 3.12` must report the separate Python 3.12 interpreter used by
Cairn. Do not replace the system interpreter. Expect writable-directory checks
and successful loopback binding to produce no output. A bind error means
port 8000 is already in use. Both documented native procedures use port 8000;
resolve the conflict before continuing, or use the Docker Compose procedure,
which documents its `cairn_port` setting. Resolve missing tools using
[missing prerequisites](#get-missing-prerequisites), then repeat the checklist
before following the [disposable quickstart](quickstart.md). If locked package
installation cannot reach its configured indexes, ask the network or repository
administrator for the approved access or cache. The runtime installation uses
only locked runtime dependencies; developer tools are a separate path.

## Persistent native prerequisites

Use the disposable checklist plus `python3.12`, systemd with user service
support, `systemctl`, `systemd-analyze`, `loginctl`, `awk`, `grep`, `df`, `id`,
`stat`, `install`, `cp`, `mv` and `tar`. These are supplied by the standard
Linux userland/systemd packages. You need an ordinary non-root account, at least
1 GiB free for a small installation, writable owner-controlled home directories,
and separately protected backup storage. Only optional startup before login
requires administrator/polkit authority. The [native guide](operations/native-installation.md#requirements)
provides exact checks and expected results before installation.
The inline Attic check uses the same curl 8.4.0 minimum as the disposable
checklist. Its standard-library helper supports Python 3.12–3.14 as `python3`.

## Docker Compose installation

Required: Linux x86_64 with systemd, Engine 25.0 or newer, Compose plugin 2.20.2
or newer, Bash, Git, make, `systemctl`, `sed`, GNU coreutils (`sha256sum`,
`install`, `realpath`, `stat` and `tr`), curl 8.4.0+, jq 1.6+ and Python 3.12–3.14
as `python3` for the standard-library verification helper. Cairn itself uses
its pinned Python 3.12 runtime inside the image; the helper does not import
Cairn or its installed dependencies. Keep the host's system interpreter: Python
3.14 does not need replacing. Curl 8.4.0 is required because its
[`--max-filesize`](https://curl.se/docs/manpage.html#--max-filesize) limit also
guards unknown-length responses during transfer. Semantic retrieval additionally needs
OpenSSL, a provider API key, outbound provider access, the FalkorDB image and
privilege through `sudo` or an existing root session for numeric file ownership.
Provider calls may incur charges; the base Attic-enabled stack does not call a
model provider. Go, uv, Bubblewrap and kubectl are not host prerequisites here.

You need trusted Docker daemon access and administrator authority for the
explicit numeric UID ownership changes. Container data lives in persistent
volumes; configuration and credential files remain on the host. The
[Compose prerequisite checks](../deploy/compose/README.md#compose-prerequisites-and-local-image)
verify daemon access and versions before changing files. Follow that guide
from beginning to end, including its choice of base or semantic lifecycle
commands. `down` and volume deletion have different retention consequences.

## Kubernetes installation

**For semantic search, have an OpenAI API key ready.** Supply it through the
protected credential Secret described in [credentials and retrieval egress](operations/deployment.md#credentials-and-retrieval-egress).
The base Kubernetes installation and inline Attic check need no OpenAI key.


Use this path to install Cairn into an **existing conformant Kubernetes cluster**.
It includes Attic, with semantic retrieval optional. Cluster creation, CNI/CSI
installation and cluster repair are administrator prerequisites. OpenShift has a
separate overlay and requires its own admission, SCC, storage and network-policy
validation; do not assume the Kubernetes result proves OpenShift support.

### Prerequisites and access

- A Linux x86_64 operator checkout and Linux workers able to run the reviewed
  image. The recorded reference deployment used Kubernetes 1.35.0 with Cilium
  1.19.6. The repository pins kubectl 1.35.0 and Kustomize 5.7.1; other target
  versions need their own compatibility and admission checks.
- Bash, Git, GNU coreutils, `awk`, curl 8.4.0+, jq 1.6+, Python 3.12 and uv 0.12.0.
  The locked Python environment supplies YAML support for generating and checking
  manifests. Go and Bubblewrap are not installation requirements. Docker and make
  are needed locally only if you build the Cairn image yourself.
- A reviewed Cairn image available to the workers by immutable digest, including
  any registry pull credentials under the cluster's normal policy. The checkout's
  `cairn:v0.1.0-rc.2` tag is a local build tag, not a published registry image. See the
  [image boundary](operations/deployment.md).
- A dedicated namespace and an approved CSI StorageClass supporting
  `ReadWriteOncePod`, reliable POSIX locks and `fsync`. NFS and other shared or
  network filesystems are unsuitable for the SQLite WAL catalogue.
- A CNI that enforces the rendered NetworkPolicies, functioning cluster DNS, and
  namespace-scoped authority to manage the documented workloads, ConfigMaps,
  Secrets, Services, ServiceAccounts, PVCs and policies. Bootstrap also requires
  Pod create/delete, rollout/scale and `pods/exec` access. Namespace creation and
  the shared egress gateway may require separate administrator authority.
- For semantic retrieval: the pinned FalkorDB image, provider credentials supplied
  as Secret files, and the shared egress gateway with its approved provider
  allow-list. Provider calls can incur charges. The index-free path requires none
  of those semantic dependencies.

Resolve missing local tools using [the prerequisite instructions](#get-missing-prerequisites).
From the repository root, install the locked manifest tooling and put the pinned
kubectl first on PATH for this terminal:

```sh
set -eu
for tool in bash git awk curl jq uv sha256sum; do
  command -v "$tool" || exit 1
done
uv --version
curl --version
jq --version
uv sync --locked
./scripts/fetch-kubectl
export PATH="$PWD/build/tools:$PATH"
kubectl version --client
kubectl config current-context
```

Expect uv 0.12.0, curl at least 8.4.0, jq at least 1.6, and kubectl v1.35.0 with
Kustomize v5.7.1. The final command must name the intended cluster context; stop
if no context is configured or the selected context is wrong. Obtain a kubeconfig
and the required permissions from the cluster administrator. Do not solve an
RBAC refusal by granting yourself cluster-admin.

### Installation sequence

Run the [cluster preflight](operations/kubernetes-preflight.md) inventory first;
it identifies missing prerequisites without repairing the cluster. Follow the
[Kubernetes deployment procedure](operations/deployment.md#kubernetes-and-openshift)
in this order. All site-specific values must be chosen before applying anything:

1. Select `kubernetes` for Attic-backed custody without semantic search, or
   `kubernetes-retrieval` for the complete FalkorDB and gateway path. Use the
   [overlay inventory](../deploy/kustomize/overlays/README.md); `kind` is a test
   environment, not the production installation path.
2. Generate the [complete site overlay](operations/kubernetes-site.md) with the
   dedicated namespace, stable instance UUID, registry-published image digest and
   approved storage class, then complete namespace preparation and the
   preflight probes. The image must be published to a registry first; a local
   containerd import alone is not this installation path. Use the
   [targeted instance-label example](operations/deployment.md#customise-instance-labels-without-changing-external-destinations)
   to replace existing labels. Verify DNS/gateway destination selectors and
   namespace selectors remain intact and retain both default-deny directions.
3. Supply the required credential Secret from protected files and, for retrieval,
   follow the [exact shared gateway procedure](operations/kubernetes-gateway.md)
   once per cluster, choosing its portable or Cilium variant as appropriate. Follow
   [credentials and retrieval egress](operations/deployment.md#credentials-and-retrieval-egress).
   Never put provider keys in a rendered manifest or Git.
4. Render and inspect the complete site configuration, run server-side dry run,
   then follow [first boot](operations/deployment.md#first-boot). That procedure
   creates the persistent claim, runs migration, stops serving for exclusive
   bootstrap, retains the one-time credential safely and restarts Cairn. Bootstrap
   is not a normal restart step; retain the same UUID, claims and credential.
5. Check authenticated instance identity and run the
   [quick Attic write/read test](#quick-attic-writeread-test) below, including
   its read after restarting the StatefulSet and restoring the port-forward.
   From the repository root, use the namespace selected for the installation:

   ```sh
   build/tools/kubectl rollout restart -n "$namespace" statefulset/cairn
   build/tools/kubectl rollout status -n "$namespace" statefulset/cairn --timeout=420s
   ```

   With semantic retrieval enabled,
   perform the [bounded synthetic write/read check](clients.md#bounded-ingest-and-retrieval-verification).
   A committed ingest proves custody; indexing can complete later. Retain the
   saved fact ID for the same read-only check after a restart.
6. Before relying on the instance, follow the [backup and recovery runbook](operations/backup-restore.md)
   and record the site's restart, storage and policy results. Restarting Pods
   preserves their claims; deleting a namespace or PVC is not a restart procedure
   and can destroy data depending on the StorageClass reclaim policy.

### Connect locally for verification

The default Service is `ClusterIP`; it does not publish Cairn to the internet.
In a separate terminal opened at the repository root, use the namespace and
Service name from your reviewed
render (the defaults below assume `cairn-example` and `cairn`):

```sh
export PATH="$PWD/build/tools:$PATH"
namespace=cairn-example
kubectl port-forward --address 127.0.0.1 -n "$namespace" service/cairn 8080:8000
```

Leave that terminal running. In the client terminal, set
`base_url=http://127.0.0.1:8080` and `credential_file` to the owner-only file
retained by first boot, then follow the [client credential setup](clients.md#credentials)
and verification commands. Port forwarding also needs the administrator-approved
`pods/portforward` permission. It provides a local API check, not proof of an
external ingress path or cross-node CNI isolation. External publication remains
an explicit TLS-only ingress/proxy configuration.

### Quick Attic write/read test

After first boot, run the [client credential setup](clients.md#credentials)
with the `base_url` and retained `credential_file` above, in the client terminal
at the repository root. Keep the loopback port-forward running. These commands
use that protected `curl_config` and the bootstrap administrator's grant at
`local/repository:example`; no semantic provider is needed.

Commit one small synthetic text payload and read it back byte-for-byte:

```bash
set -eu
umask 077
install -d -m 0700 "$HOME/.local/state/cairn-checks"
evidence_check_dir="$(mktemp -d "$HOME/.local/state/cairn-checks/evidence.XXXXXXXX")"
printf 'Retained evidence verification files: %s\n' "$evidence_check_dir"
printf 'Cairn Attic check: café.\nExact second line.\n' > "$evidence_check_dir/payload.txt"
python3 -c 'import uuid; print(uuid.uuid4())' > "$evidence_check_dir/idempotency-key"
jq -n --rawfile payload "$evidence_check_dir/payload.txt" '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  facts: [{body: "The installation has submitted a synthetic Attic payload."}],
  evidence_payload: $payload
}' > "$evidence_check_dir/ingest.json"

if http_status="$(curl --disable --silent --show-error --noproxy '*' \
  --max-time 30 --request POST --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $(cat "$evidence_check_dir/idempotency-key")" \
  --data-binary "@$evidence_check_dir/ingest.json" \
  --output "$evidence_check_dir/ingest-response.json" \
  --dump-header "$evidence_check_dir/ingest-headers" --write-out '%{http_code}' \
  "$base_url/v1/ingest")"; then
  printf 'Ingest HTTP %s\n' "$http_status"
  cat "$evidence_check_dir/ingest-response.json"
  printf '\n'
else
  printf 'Commit uncertain; retain the same request and key in %s.\n' "$evidence_check_dir" >&2
  exit 1
fi
test "$http_status" = 200
jq -e '
  (.outcome == "committed" or .outcome == "replayed") and
  (.mutation_receipt | type == "object") and
  (.audit_receipt | type == "object") and
  (.result.evidence_id | type == "string")
' "$evidence_check_dir/ingest-response.json" >/dev/null
jq -er '.result.evidence_id' "$evidence_check_dir/ingest-response.json" > "$evidence_check_dir/evidence-id"

python3 scripts/verify-retrieval.py \
  --base-url "$base_url" \
  --credential-file "$credential_file" \
  --evidence-id "$(cat "$evidence_check_dir/evidence-id")" \
  --payload-file "$evidence_check_dir/payload.txt" \
  --deadline 120 --request-timeout 10 --max-attempts 30
```

Expect `"status": "verified"` with the saved evidence ID and SHA-256. The helper
checks exact UTF-8 bytes, byte length and digest. It retries only evidence reads,
honours `Retry-After`, and fails after 120 seconds or 30 attempts. Corruption or
mismatched bytes fail immediately. This proves Attic custody and recovery of
this payload, not semantic indexing or factual truth.

The payload remains in the persistent volume. Retain the printed directory.
After the StatefulSet restart in step 5, restore the port-forward and rerun
**only** the final `python3 scripts/verify-retrieval.py` command with the same
saved ID and payload file. Do not repeat a committed ingest. If the ingest
transport fails, its commit is uncertain: reuse the saved request and key as
explained in the [full round-trip procedure](operations/evidence-verification.md).

## Full developer validation

This path is for changing or validating Cairn, not a prerequisite for using it.
It requires the native tools above, Python 3.12, uv 0.12.0, Go 1.22+, make,
a C compiler for Go race tests, Docker with the Compose plugin, and the pinned
kubectl fetched below. Bubblewrap at `/usr/bin/bwrap` (verified with 0.9.0),
`/usr/bin/python3`, merged-`/usr` and permitted unprivileged user/PID/mount
namespaces are required for host-isolation tests. Do not disable host security
policy to conceal a failed namespace check. Ask the host administrator for a
supported test environment. No Kubernetes cluster access is required.

On Ubuntu, the extra build tools are provided by `build-essential`, `golang-go`
and `bubblewrap`. Check that the packaged Go version meets 1.22; otherwise use
the [Go installation instructions](https://go.dev/doc/install). Docker/Compose
installation is described above. From the repository root:

```sh
for tool in git make cc go uv jq docker; do
  command -v "$tool" || exit 1
done
go version
uv --version
docker compose version
/usr/bin/python3 --version
/usr/bin/bwrap --version
/usr/bin/bwrap --unshare-user --unshare-pid --ro-bind / / /usr/bin/true
uv sync --locked
./scripts/fetch-kubectl
build/tools/kubectl version --client
make check
```

Expect every prerequisite command to exit zero, the stated versions, and a
successful `make check`. The render and Compose configuration gates are
client-side and contact neither cluster nor Docker daemon. Tests, locked package
installation and dependency auditing may need internet access. The default suite
leaves the opt-in real database tests skipped; passing it does not claim those
integrations or beginner installation acceptance. Record the actual summary,
revision and environment rather than treating a historical test count as a gate.
