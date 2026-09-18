# Build FalkorDB locally

Cairn ships a recipe for a Linux amd64 server runtime. The operator builds it
locally from pinned source: FalkorDB 4.20.4 at commit
`5ac6db8059013c9d74842c02b6a9f1a4858a6a1b` with its exact recursive gitlinks,
Redis 8.6.3 with SHA-256
`9f54d4458c52be5472cdd1347d737f1d488b520fc3d0911cba47302de8d836e2`, and
`google/cpu_features` at commit
`438a66e41807cd73e0c403966041b358f5eafc68`. The builder and final runtime
stages use Ubuntu 24.04 pinned at digest
`sha256:496754492fb28b4d3049432f2ca787449331e23fb14f0dd3fffea86bf5a93eb4`.
Cairn does not publish this database image to GHCR or attach database images or
source bundles to new releases.

The `FALKORDB_IMAGE` value in `deploy/images.lock` and the committed Kubernetes
renders is an all-zero, deliberately unusable placeholder. It is not a fallback
image. For manual Compose or Kubernetes deployment, replace it with the reviewed
local runtime descriptor's `image` value; guided native, Docker and Kubernetes
installs require that local runtime explicitly.

The recipe fetches the pinned sources during each local build, compiles the
upstream FalkorDB module and omits the browser. It takes `run.sh` and
`gen-certs.sh` from that verified FalkorDB checkout and copies the FalkorDB and
recursive-dependency, Redis and `cpu_features` licence files into
`/usr/share/licenses/cairn-falkordb` in the runtime. It does not ship a new
source bundle. Redis is built with TLS support and without bundled modules.
FalkorDB's VecSim tests and AVX, AVX512F and AVX512DQ specialisation are
disabled for the supported generic amd64 baseline; the compilation steps run
without network access. The final runtime upgrades the pinned Ubuntu base from
its current APT repositories and installs its runtime libraries, so a fresh
build has its own identity and needs fresh checks. Retain the accepted local
archive for restaging; do not rebuild merely to reload a node.

## Build and retain the runtime

Requirements: Linux amd64, Docker with its containerd image store, Git, curl,
`sha256sum`, Python 3.12 or newer, and outbound HTTPS access to GitHub, the
Redis download site, the Ubuntu image registry and Ubuntu APT repositories.
Run from the trusted Cairn checkout root. The build uses at most ten parallel
jobs by default; choose a lower bound from 1 to 10 on a smaller host:

```sh
CAIRN_BUILD_JOBS=10 bash deploy/falkordb/build.sh
python3 scripts/falkordb_runtime.py --output build/falkordb-local
```

`CAIRN_BUILD_JOBS` defaults to `10` and rejects zero, negative values and values
above `10`. The output directory must be new. It contains `image.tar`,
`runtime.json` and `SHA256SUMS`. The descriptor records the actual image digest
and archive hash; it makes no registry-publication claim. Scan and test that
build before using it for a real installation. The export verifies the complete
OCI graph, so a Docker store which loses the image index is rejected.

For a guided Docker or Linux native semantic install, provide
`--falkordb-runtime /absolute/path/to/build/falkordb-local/runtime.json` alongside
the normal `--semantic` and protected provider-key options. The runtime must be
present in the Docker daemon used by the installer. A different build requires
a new descriptor; resume retains the originally selected runtime.

## Prepare Kubernetes nodes

The node-preparation command is a separate host-administrator operation.
It requires existing SSH access, verified host keys and non-interactive sudo
for containerd administration. It does not create privileged Kubernetes pods
or grant itself permissions. The initial transport supports containerd on
Linux amd64 nodes. Supply explicit node-name to SSH-host mappings; Kubernetes
node addresses are not automatically trusted as SSH destinations.

The helper always uses SSH, including when the checkout and the only Kubernetes
node are on the same host. Before starting, verify every `--node NODE=SSH_ALIAS`
mapping non-interactively with `ssh SSH_ALIAS true`, accept and verify its host
key through the site's normal process, and confirm that the SSH identity can run
the documented containerd commands through non-interactive `sudo`.

The following example prepares the single node `reference` using the operator's
existing SSH configuration alias of the same name:

```sh
runtime_dir="$PWD/build/falkordb-local"
image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$runtime_dir/runtime.json")
python3 scripts/kubernetes_image_stage.py \
  --context YOUR_CONTEXT \
  --archive "$runtime_dir/image.tar" \
  --image "$image" \
  --node reference=reference \
  --output "$runtime_dir/kubernetes-receipt.json"
```

Repeat `--node NODE=SSH_ALIAS` for each eligible node. The helper checks current
node identity, readiness, architecture, scheduling status and runtime before
transfer. It verifies the copied archive's checksum before privileged import,
imports into containerd's `k8s.io` namespace, and verifies the canonical image
digest through CRI. A receipt is written only after all requested nodes pass.
Partial failure may leave useful cached images; it does not certify partial
success. Rerun with a fresh output receipt after correcting the failure.

## Install with the prepared nodes

Add the following to the normal semantic Kubernetes installation command:

```sh
--kube-falkordb-receipt /absolute/path/to/build/falkordb-local/kubernetes-receipt.json
```

The installer retains the receipt, verifies the recorded node UIDs and runs
ordinary unprivileged image probes. FalkorDB uses the actual local digest,
`imagePullPolicy: Never`, and required node affinity to the prepared node names.
Cairn and Garden image options remain independent. Storage topology, taints and
admission rules still apply; a prepared node is not automatically a valid
location for a bound PVC.

Node affinity matches names, not UIDs. Install/resume rechecks the recorded UIDs;
replacement or additional nodes require preparation. Between installer runs,
a replacement node with a reused name may be considered by the scheduler, but
cannot pull the local image. Kubelet may also garbage-collect unused cached
images. If the image is absent, the pod fails with a missing-image error;
restage the retained archive. This first implementation does not run a cache
maintenance controller. Sites without node administration access can use their
own authenticated registry and the manual site-manifest workflow instead.

## Earlier releases

<a id="install-the-maintained-offline-image"></a>
<a id="install-the-rc3-offline-image"></a>
<a id="install-the-rc4-offline-image"></a>
<a id="kubernetes-with-an-offline-containerd-image-store"></a>

Older RC3/RC4 tags described a prebuilt maintained image and paired source
bundle. Their metadata and historical acceptance do not identify a fresh local
build. Use documentation from those immutable tags when operating those older
releases. New installations follow the recipe and local receipts above.
