from pathlib import Path

from cairn_install.workflow import STAGES

ROOT = Path(__file__).parents[2]
GUIDE = ROOT / "docs" / "operations" / "guided-installation.md"


def test_guided_installation_stage_headings_match_workflow() -> None:
    text = GUIDE.read_text()
    headings = [
        line.removeprefix("### ")
        for line in text.splitlines()
        if line.startswith("### ") and " — " in line
    ]

    assert headings == [f"{key} — {title}" for key, title in STAGES]


def test_install_indexes_link_to_guided_installation() -> None:
    install = " ".join((ROOT / "docs" / "install.md").read_text().split())

    assert "operations/guided-installation.md" in install
    assert "Four modes, two feature choices:" in install
    assert "| `kubernetes` |" in install
    assert "Kubernetes mode:" in install
    assert "explicit kube context" in install
    assert "shared gateway" in install
    assert "locked YAML helper runtime" in install
    assert "namespace-scoped mutation permissions" in install
    assert "cluster-scoped read access" in install
    assert "Namespace, Nodes, StorageClass and CSIDriver" in install
    assert "never mutates cluster prerequisites" in install
    assert (
        "operations/guided-installation.md#install-to-an-existing-kubernetes-namespace"
    ) in install
    assert "docs/operations/guided-installation.md" in (ROOT / "README.md").read_text()


def test_kubernetes_guided_installation_documents_operator_interface() -> None:
    text = " ".join(GUIDE.read_text().split())

    for expected in (
        "--mode kubernetes",
        "--non-interactive",
        "--name cairn-v05",
        "--kube-context reference",
        "--kube-namespace cairn-v05",
        "--kube-storage-class cairn-local",
        "kube_image='REPLACE_WITH_TRUSTED_CAIRN_IMAGE@sha256:REPLACE_WITH_64_HEX_DIGEST'",
        "Replace kube_image with the distributor-supplied immutable image before running",
        '--kube-image "$kube_image"',
        "--semantic",
        "--provider-key-file /home/operator/.config/cairn/openai-api-key",
        "--state-root /home/operator/.local/state/cairn-install",
        "--source /home/operator/projects/cairn",
        "--kube-preloaded-image",
        "normal registry path",
        "registry-credential policy",
        "IfNotPresent",
        "exactly one schedulable node",
        "scripts/kubernetes_image_stage.py",
        "cairn.local/cairn-runtime@sha256",
        "Context and namespace are immutable on resume",
        "kubectl --context reference --namespace cairn-v05 port-forward service/cairn 8126:8000",
        "status` prints a temporary, explicit port-forward command",
        "instead of a permanent endpoint",
        "Ready Linux/amd64 capacity",
        "ReadWriteOncePod",
        "shared gateway",
        "locked YAML helper runtime",
        "namespace-scoped mutation permissions",
        "cluster-scoped read access",
        "Namespace, Nodes, StorageClass and CSIDriver",
        "never mutates cluster prerequisites",
        "never creates or",
        "deletes the namespace",
        "gateway",
        "StorageClass",
        "CNI",
        "CSI",
        "nodes",
        "registry credentials",
        "image cache",
        "retains the namespace, resources, PVCs, credentials and state",
        "UID and ownership labels",
        "rendered YAML",
    ):
        assert expected in text


def test_installation_docs_make_external_inputs_and_shared_gateway_boundaries_explicit() -> (
    None
):
    guide = " ".join(GUIDE.read_text().split())
    gateway = " ".join(
        (ROOT / "docs" / "operations" / "kubernetes-gateway.md").read_text().split()
    )
    garden = " ".join(
        (ROOT / "docs" / "operations" / "managed-garden.md").read_text().split()
    )
    staging = " ".join((ROOT / "deploy" / "falkordb" / "README.md").read_text().split())
    public_readme_path = ROOT / "public-release" / "overlay" / "README.md"
    if not public_readme_path.exists():
        public_readme_path = ROOT / "README.md"
    public_readme = " ".join(public_readme_path.read_text().split())

    assert "installer does not modify or delete it" in guide
    assert "blitz` leaves it untouched" in guide
    assert '--semantic --falkordb-runtime "$falkordb_runtime"' in guide
    assert '--falkordb-runtime "$PWD/build/falkordb-local/runtime.json"' in guide
    assert "diagnostic outside those listed categories" in staging
    assert "build.sh` exits zero" in staging
    assert "falkordb_runtime.py` exits zero" in staging
    assert "package-configuration deferral" in staging
    assert "container service-start/runlevel handling" in staging
    assert "unused manually supplied variables" in staging
    assert "scripts/generate-garden-tls" in garden
    assert "Stop Garden and preserve its data" in guide
    assert "managed Garden is appended as `; Garden`" in guide
    assert "kubectl delete --dry-run=server" in gateway
    assert "cairn.example.invalid/instance -o name" in gateway
    assert "destination.write_text(yaml.safe_dump_all(namespaced" in gateway
    assert (
        'kubectl delete --wait=true --ignore-not-found -f "$gateway_objects"' in gateway
    )
    assert (
        'allowed = {"serviceaccount/default", "configmap/kube-root-ca.crt"}' in gateway
    )
    assert (
        'kubectl delete --wait=true --ignore-not-found -f "$gateway_render"'
        not in gateway
    )
    assert "The first command must succeed without a prompt" in staging
    assert "REPLACE_WITH_KUBE_CONTEXT" in staging
    assert (
        "Replace every Kubernetes value with a reviewed site value before running"
        in garden
    )
    assert '--kube-context "$kube_context"' in garden
    assert "REPLACE_WITH_TRUSTED_REPOSITORY_URL" in public_readme
    assert "github.com/veridian69/cairn.git" not in public_readme
