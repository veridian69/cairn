# Build a complete Kubernetes site overlay

Run from the repository root after the [cluster preflight](kubernetes-preflight.md).
This example produces a complete, reviewable instance render with Attic and
optional semantic retrieval. It does not create or repair a cluster. Keep the
result in your site's GitOps repository; it contains configuration, not secrets.

## Publish the Cairn image first

This manual GitOps installation path requires a registry that every selected
worker can pull from. A local Docker build or a containerd import alone is
insufficient here: the worker must resolve the **exact digest reference** used
in the Pod. Importing an image under a tag does not necessarily register that
digest reference. The guided installer separately documents a supported
single-node node-local staging exception; it is deliberately outside this
multi-node-capable manual procedure. See the
[guided installation reference](guided-installation.md#install-to-an-existing-kubernetes-namespace).

If your distributor provides a reviewed registry digest, use it directly below.
Otherwise, on a trusted Linux x86_64 build host with Docker access, replace the
registry/repository value and publish the checkout you reviewed. Authenticate
with your registry's credential helper or interactive login first; never put
passwords or tokens in commands, files committed to Git, or image references.

```bash
set -eu
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -z "$(git status --porcelain --untracked-files=all)"
image_repository='REPLACE_WITH_REGISTRY/REPLACE_WITH_REPOSITORY/cairn'
case "$image_repository" in *REPLACE_WITH_*) exit 1 ;; esac
revision="$(git rev-parse HEAD)"
image_tag="$image_repository:$revision"
docker build --build-arg REVISION="$revision" -t "$image_tag" .
docker push "$image_tag"
docker image inspect "$image_tag" --format '{{range .RepoDigests}}{{println .}}{{end}}'
```

Copy the matching repository's `@sha256:…` reference from the final output into
`cairn_image` below. Do not use the local image ID or the mutable tag. Private
registries also need administrator-provisioned pull access for the instance
namespace; set `pull_secret` below to that existing registry Secret's name, or
leave it empty for an anonymously readable registry. The preflight's worker-pull
probe must succeed using this exact image and pull Secret before first boot.

## Generate the site

Change the four site values and, if needed, the two optional choices below.
Use the same value for namespace and instance name. `storage_class` must be the
CSI class accepted by preflight. `site` must be a new directory inside an existing operator-owned GitOps
checkout, outside this source checkout’s disposable `build/` tree; the UUID is
generated once and retained in its render. Reuse this directory and identity
for restarts and upgrades, rather than generating a replacement instance.

```bash
set -eu
instance_name='REPLACE_WITH_INSTANCE_NAME'
namespace="$instance_name"
cairn_image='REPLACE_WITH_REGISTRY/REPLACE_WITH_REPOSITORY/cairn@sha256:REPLACE_WITH_DIGEST'
storage_class='REPLACE_WITH_CSI_STORAGE_CLASS'
site='/absolute/path/to/REPLACE_WITH_SITE_GITOPS_CHECKOUT/REPLACE_WITH_INSTANCE_DIRECTORY'
# Choose kubernetes for index-free custody, or kubernetes-retrieval for search.
source_overlay=kubernetes-retrieval
pull_secret=''
case "$instance_name:$cairn_image:$storage_class:$site" in
  *REPLACE_WITH_*) printf '%s\n' 'Replace all site values first.' >&2; exit 1 ;;
esac
case "$source_overlay" in kubernetes|kubernetes-retrieval) ;; *) exit 1 ;; esac
case "$site" in /*) ;; *) printf '%s\n' 'site must be absolute.' >&2; exit 1 ;; esac
test ! -e "$site"
site_root="$(git -C "$(dirname "$site")" rev-parse --show-toplevel)"
test -z "$(git -C "$site_root" status --porcelain --untracked-files=all)"
mkdir -p "$site"
site="$(realpath "$site")"
site_relative="$(realpath --relative-to="$site_root" "$site")"
export PATH="$PWD/build/tools:$PATH"
kubectl kustomize "deploy/kustomize/overlays/$source_overlay" > "$site/baseline.yaml"
uv run --locked python - "$site" "$instance_name" "$namespace" "$cairn_image" "$storage_class" "$pull_secret" <<'PY'
import copy
import re
import sys
from pathlib import Path
from uuid import uuid4

import yaml

root, instance, namespace, image, storage, pull_secret = sys.argv[1:]
root = Path(root)
name_pattern = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
for value in (instance, namespace):
    if not re.fullmatch(name_pattern, value):
        raise SystemExit("use DNS-label names of at most 63 characters")
if instance == "cairn" or namespace != instance:
    raise SystemExit("choose a distinct instance name and use it as the namespace")
if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
    raise SystemExit("cairn_image must be a repository@sha256 digest reference")
subdomain_pattern = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
for value in (storage, pull_secret):
    if value and (len(value) > 253 or not re.fullmatch(subdomain_pattern, value)):
        raise SystemExit("storage class and pull Secret must be DNS subdomain names")
if not storage:
    raise SystemExit("storage class is required")
baseline = list(yaml.safe_load_all((root / "baseline.yaml").read_text()))
documents = copy.deepcopy(baseline)
label = "app.kubernetes.io/instance"
identity = str(uuid4())


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


for document in documents:
    replace_existing(document)
    document["metadata"]["namespace"] = namespace
    if document["kind"] == "ConfigMap" and "config.yaml" in document.get("data", {}):
        config = yaml.safe_load(document["data"]["config.yaml"])
        assert config["instance_id"] == "REPLACE_WITH_PER_INSTANCE_UUID"
        config["instance_id"] = identity
        document["data"]["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
    if document["kind"] == "StatefulSet":
        spec = document["spec"]
        spec["persistentVolumeClaimRetentionPolicy"] = {"whenDeleted": "Retain", "whenScaled": "Retain"}
        for claim in spec["volumeClaimTemplates"]:
            claim["spec"]["storageClassName"] = storage
        pod = spec["template"]["spec"]
        if pull_secret:
            pod["imagePullSecrets"] = [{"name": pull_secret}]
        if document["metadata"]["name"] == "cairn":
            pod.setdefault("securityContext", {}).update(
                fsGroup=65532, fsGroupChangePolicy="OnRootMismatch"
            )
            for container in pod.get("initContainers", []) + pod["containers"]:
                assert container["image"] == "cairn:v0.7.10"
                container["image"] = image

# NetworkPolicy changes must consist only of replacing existing instance values.
# This preserves all namespace selectors, external peers and default-deny rules.
for before, after in zip(baseline, documents, strict=True):
    if before["kind"] == "NetworkPolicy":
        expected = copy.deepcopy(before["spec"])
        replace_existing(expected)
        assert after["spec"] == expected
        for rule in after["spec"].get("egress", []):
            for peer in rule.get("to", []):
                matches = peer.get("podSelector", {}).get("matchLabels", {})
                if matches.get("k8s-app") == "kube-dns":
                    assert matches == {"k8s-app": "kube-dns"}
                if matches.get("app.kubernetes.io/name") == "cairn-egress-gateway":
                    assert matches == {"app.kubernetes.io/name": "cairn-egress-gateway"}
(root / "site.yaml").write_text(yaml.safe_dump_all(documents, sort_keys=False))
(root / "kustomization.yaml").write_text(
    "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - site.yaml\n"
)
print("Instance UUID:", identity)
print("PASS: site values set; external peers and default-deny policies preserved")
PY
kubectl kustomize "$site" > "$site/rendered.yaml"
site_render="$(realpath "$site/rendered.yaml")"
sha256sum "$site_render" > "$site_render.sha256"
```

Review the final render, including namespace, UUID, image digests, both storage
claims and policy peers. The generated instance uses Linux image ownership
`fsGroup: 65532`; OpenShift requires a separate admitted-security procedure and
is not this example. Dependency images retain their repository pins.


Commit only the generated site directory before continuing. The GitOps checkout
was required to be clean before generation; these checks require all five files
to be tracked and the checkout clean afterwards. If a check fails, resolve it
before deployment. Retain the printed commit and UUID through upgrades.

```bash
git -C "$site_root" add -- "$site_relative"
git -C "$site_root" commit --only -m "Add Cairn instance configuration" -- "$site_relative"
for file in baseline.yaml site.yaml kustomization.yaml rendered.yaml rendered.yaml.sha256; do
  git -C "$site_root" ls-files --error-unmatch -- "$site_relative/$file" >/dev/null
done
test -z "$(git -C "$site_root" status --porcelain --untracked-files=all)"
git -C "$site_root" rev-parse HEAD
```

Continue with [namespace preparation](deployment.md#namespace-and-render-preparation),
then the preflight's exact-image and storage probes in that namespace. For
semantic retrieval, install the [shared gateway](kubernetes-gateway.md) and
[credential Secret](../../deploy/kustomize/components/falkordb/README.md).
Finally follow [first boot](deployment.md#first-boot), setting `site_render` to
the absolute path produced above. Do not apply the instance before these gates.
