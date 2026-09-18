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
        "--kube-image registry.example/cairn@sha256:",
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
