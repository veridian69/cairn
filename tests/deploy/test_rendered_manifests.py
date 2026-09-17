"""Slice 8 task 5: the guards the committed renders must satisfy.

The renders under ``deploy/kustomize/rendered/`` are the artefact an
operator applies (P-61), so the properties that matter are properties of
the rendered bytes, not of the sources that produced them. Every
assertion here is therefore made against the render, and the sources are
never parsed: a kustomization that stops producing what it claims to
produce is exactly the failure this module exists to catch.

Four claims carry the weight:

- **I-91's absence.** With ``graphiti.enabled`` false the instance
  namespace contains no FalkorDB resource and no proxy variable. Included
  by choice means that choosing not to leaves no trace, and the only
  honest place to assert it is the rendered output.
- **I-08's silence.** No render carries a secret value. Credentials reach
  a container from an operator-supplied Secret or not at all.
- **I-13's closed posture.** Every instance render denies both directions
  by default, and each allowance is a separate, readable policy.
- **P-64's allow-list.** The gateway admits only labelled Cairn pods and
  tunnels only CONNECT, only to 443, only to allow-listed names.

Module-local helpers for the reason the other test modules record:
``tests`` has no package markers, so no ``conftest.py`` can be shared.
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
RENDER_DIR = REPOSITORY / "deploy" / "kustomize" / "rendered"

# One render per applyable configuration. The instance renders are the
# per-namespace ones; the gateway is per-cluster and applied once.
INSTANCE_RENDERS = (
    "base",
    "kind",
    "kubernetes",
    "kubernetes-retrieval",
    "openshift",
)
INDEXLESS_RENDERS = ("base", "kubernetes", "openshift")
GATEWAY_RENDER = "egress-gateway"
REFERENCE_GATEWAY_RENDER = "egress-gateway-reference"
ALL_RENDERS = (*INSTANCE_RENDERS, GATEWAY_RENDER, REFERENCE_GATEWAY_RENDER)

CAIRN_LABELS = {
    "app.kubernetes.io/name": "cairn",
    "app.kubernetes.io/instance": "cairn",
}
FALKORDB_LABELS = {
    "app.kubernetes.io/name": "falkordb",
    "app.kubernetes.io/instance": "cairn",
}
INSTANCE_NAMESPACE_LABEL = "cairn.example.invalid/instance"
CREDENTIALS_SECRET = "cairn-credentials"
# UIDs the pinned FalkorDB image names in its own ``/etc/passwd``. With
# ``runAsUser`` set and ``runAsGroup`` unset the runtime takes the primary
# group from that file, so any of these silently lands the index outside
# group 0 and it cannot read its ``root:root`` credential. Measured
# against the digest in ``deploy/images.lock``, 15 August 2026; re-measure
# when that pin moves.
FALKORDB_IMAGE_PASSWD_UIDS = frozenset(
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 33, 34, 38, 39, 42, 999, 1001, 65534}
)
GATEWAY_NAMESPACE = "cairn-egress"
GATEWAY_NAME = "cairn-egress-gateway"


def _documents(name: str) -> list[dict[str, Any]]:
    text = (RENDER_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    return [document for document in yaml.safe_load_all(text) if document]


def _of_kind(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [document for document in documents if document["kind"] == kind]


def _named(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [
        document
        for document in _of_kind(documents, kind)
        if document["metadata"]["name"] == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {kind}/{name}, found {len(matches)}"
    )
    return matches[0]


def _pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = workload["spec"]["template"]["spec"]
    return spec


def _container(workload: dict[str, Any], name: str) -> dict[str, Any]:
    containers: list[dict[str, Any]] = _pod_spec(workload)["containers"]
    matches = [container for container in containers if container["name"] == name]
    assert len(matches) == 1, f"expected exactly one container named {name}"
    return matches[0]


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _readable_by(mode: int, uid: int, gid: int) -> bool:
    """Can ``uid``:``gid`` read a file projected at ``mode``?

    A Secret volume's files are owned ``root:root`` unless an ``fsGroup``
    is injected, and I-11 fixes none. So the reader gets the owner bits
    only as uid 0, the group bits only as gid 0, and otherwise the world
    bits — which a credential must not have.
    """
    if uid == 0:
        return bool(mode & 0o400)
    if gid == 0:
        return bool(mode & 0o040)
    return bool(mode & 0o004)


def _instance_configuration(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``cairn.config/v1`` document the instance would load."""
    configmap = _named(documents, "ConfigMap", "cairn-config")
    loaded: dict[str, Any] = yaml.safe_load(configmap["data"]["config.yaml"])
    return loaded


def test_every_render_matches_its_committed_digest() -> None:
    """P-61: the digest beside a render is part of the artefact.

    ``make render`` writes both in one step, so a mismatch means a render
    was edited by hand — the one way the diff gate can be satisfied by
    something the sources never produced.
    """
    for name in ALL_RENDERS:
        render = RENDER_DIR / f"{name}.yaml"
        recorded = (RENDER_DIR / f"{name}.yaml.sha256").read_text(encoding="utf-8")
        digest = hashlib.sha256(render.read_bytes()).hexdigest()
        assert recorded.split()[0] == digest, f"{name}.yaml does not match its digest"


def test_indexless_renders_carry_no_index_workload_and_no_proxy_variable() -> None:
    """I-91 made mechanical: choosing not to include FalkorDB leaves no trace.

    Both halves matter. A stray workload would be an unauthenticated
    database nobody asked for; a stray ``HTTPS_PROXY`` would silently
    route an instance's outbound traffic through a gateway that may not
    exist on that cluster.
    """
    for name in INDEXLESS_RENDERS:
        documents = _documents(name)
        # Resources and images, not raw bytes: the base's configuration
        # document explains in a comment why the index is off, and a
        # text scan would read that explanation as the thing it forbids.
        assert not [
            document
            for document in documents
            if "falkordb" in document["metadata"]["name"]
        ], f"{name} renders a FalkorDB resource"
        for workload in _of_kind(documents, "StatefulSet"):
            containers = (
                _pod_spec(workload).get("initContainers", [])
                + _pod_spec(workload)["containers"]
            )
            assert not [
                container
                for container in containers
                if "falkordb" in container["image"]
            ], f"{name} runs a FalkorDB image"
            for container in containers:
                assert not [
                    variable
                    for variable in _environment(container)
                    if "PROXY" in variable.upper()
                ], f"{name} sets a proxy variable with the index disabled"


def test_indexless_render_configuration_disables_the_index() -> None:
    for name in INDEXLESS_RENDERS:
        configuration = _instance_configuration(_documents(name))
        assert configuration["graphiti"]["enabled"] is False


def test_kubernetes_retrieval_is_complete_and_security_pinned() -> None:
    documents = _documents("kubernetes-retrieval")
    cairn = _named(documents, "StatefulSet", "cairn")
    falkordb = _named(documents, "StatefulSet", "falkordb")
    assert _pod_spec(cairn)["securityContext"]["fsGroup"] == 65532
    assert _pod_spec(cairn)["securityContext"]["fsGroupChangePolicy"] == (
        "OnRootMismatch"
    )
    assert _pod_spec(falkordb)["securityContext"]["runAsUser"] == 10001
    assert _pod_spec(falkordb)["securityContext"]["runAsGroup"] == 0
    assert _pod_spec(falkordb)["securityContext"]["fsGroup"] == 10001
    assert _pod_spec(falkordb)["securityContext"]["fsGroupChangePolicy"] == (
        "OnRootMismatch"
    )
    proxy = _environment(_container(cairn, "cairn"))["HTTPS_PROXY"]
    assert proxy["value"] == (
        f"http://{GATEWAY_NAME}.{GATEWAY_NAMESPACE}.svc.cluster.local:3128"
    )

    assert _instance_configuration(documents)["graphiti"] == {
        "enabled": True,
        "host": "falkordb",
        "port": 6379,
    }
    for workload in (cairn, falkordb):
        claim = workload["spec"]["volumeClaimTemplates"][0]["spec"]
        assert claim["accessModes"] == ["ReadWriteOncePod"]
        assert claim["volumeMode"] == "Filesystem"
        assert claim["storageClassName"] == "cairn-local"

    gateway_egress = _named(documents, "NetworkPolicy", "cairn-allow-gateway-egress")
    assert gateway_egress["spec"]["podSelector"]["matchLabels"] == CAIRN_LABELS
    assert gateway_egress["spec"]["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": GATEWAY_NAMESPACE
                        }
                    },
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": GATEWAY_NAME}
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 3128}],
        }
    ]


def test_kind_render_wires_the_index_to_its_own_falkordb() -> None:
    """The one overlay that includes the component wires all of it.

    Three ends have to meet or the instance starts and fails later: the
    configuration must point at the Service the component creates, the
    policy must permit that connection, and the proxy variable must name
    the gateway.
    """
    documents = _documents("kind")
    configuration = _instance_configuration(documents)
    assert configuration["graphiti"] == {
        "enabled": True,
        "host": "falkordb",
        "port": 6379,
    }

    service = _named(documents, "Service", "falkordb")
    assert service["spec"]["type"] == "ClusterIP"
    assert [port["port"] for port in service["spec"]["ports"]] == [6379]
    assert service["spec"]["selector"] == FALKORDB_LABELS

    falkordb = _named(documents, "StatefulSet", "falkordb")
    assert falkordb["spec"]["replicas"] == 1

    cairn = _container(_named(documents, "StatefulSet", "cairn"), "cairn")
    proxy = _environment(cairn)["HTTPS_PROXY"]
    assert proxy["value"] == (
        f"http://{GATEWAY_NAME}.{GATEWAY_NAMESPACE}.svc.cluster.local:3128"
    )

    egress = _named(documents, "NetworkPolicy", "cairn-allow-index-egress")
    assert egress["spec"]["podSelector"]["matchLabels"] == CAIRN_LABELS
    destinations = egress["spec"]["egress"][0]["to"]
    assert destinations == [{"podSelector": {"matchLabels": FALKORDB_LABELS}}]
    assert egress["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 6379}]

    gateway_egress = _named(documents, "NetworkPolicy", "cairn-allow-gateway-egress")
    assert gateway_egress["spec"]["egress"][0]["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": GATEWAY_NAMESPACE}
            },
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": GATEWAY_NAME}},
        }
    ]
    assert gateway_egress["spec"]["egress"][0]["ports"] == [
        {"protocol": "TCP", "port": 3128}
    ]


@pytest.mark.parametrize("overlay", ("kind", "kubernetes-retrieval"))
def test_retrieval_configuration_differs_from_the_base_only_in_the_index_block(
    overlay: str,
) -> None:
    """The overlay restates the whole configuration document, so it can drift.

    Kustomize cannot patch inside a YAML string, and P-62 puts the
    document in the base as one. This is the guard that keeps the second
    copy honest: enabling the index is the only thing either retrieval
    overlay is allowed to change, and a base value edited in one place and
    not the other fails here rather than in an acceptance run.
    """
    base = _instance_configuration(_documents("base"))
    retrieval = _instance_configuration(_documents(overlay))
    differing = {
        key
        for key in base.keys() | retrieval.keys()
        if base.get(key) != retrieval.get(key)
    }
    assert differing == {"graphiti"}


def test_no_render_carries_a_secret_value() -> None:
    """I-08: secrets are operator-supplied and never live in an artefact.

    The FalkorDB password is the interesting case. Since Operator's ruling of
    13 August 2026 it reaches redis the way every other credential in
    this deployment does — as a file from the instance's Secret, named by
    ``REDIS_ARGS`` because redis reads its first argument as a
    configuration file. No environment variable carries it, and neither
    does any command line: the manifest names a path and a Secret key.
    """
    for name in ALL_RENDERS:
        documents = _documents(name)
        assert not _of_kind(documents, "Secret"), f"{name} renders a Secret"

    falkordb = _named(_documents("kind"), "StatefulSet", "falkordb")
    container = _container(falkordb, "falkordb")
    environment = _environment(container)
    assert environment["REDIS_ARGS"]["value"] == "/etc/falkordb/cairn.conf"
    # No environment entry mentions the credential at all, by value or by
    # reference — the file is the whole arrangement.
    assert not [
        name
        for name, entry in environment.items()
        if "PASSWORD" in name.upper() or "valueFrom" in entry
    ]

    mount = [
        entry
        for entry in container["volumeMounts"]
        if entry["mountPath"] == "/etc/falkordb"
    ]
    assert len(mount) == 1
    assert mount[0]["readOnly"] is True
    volume = [
        entry
        for entry in _pod_spec(falkordb)["volumes"]
        if entry["name"] == mount[0]["name"]
    ]
    assert len(volume) == 1
    assert volume[0]["secret"]["secretName"] == CREDENTIALS_SECRET
    assert volume[0]["secret"]["items"] == [
        {"key": "falkordb.conf", "path": "cairn.conf"}
    ]
    # Readable by the arbitrary runtime UID's root group, for the reason
    # the base records: no fsGroup is fixed, so 0400 would be unreadable.
    assert volume[0]["secret"]["defaultMode"] == 288


def test_falkordb_disables_the_bundled_browser() -> None:
    """The image starts an unauthenticated web UI unless told not to.

    ``BROWSER`` defaults to 1 in the pinned image and its entrypoint then
    runs a node server beside redis. Nothing in Cairn uses it, no policy
    would reach it, and an unauthenticated console next to the index is
    not a surface to leave to a default.
    """
    falkordb = _named(_documents("kind"), "StatefulSet", "falkordb")
    environment = _environment(_container(falkordb, "falkordb"))
    assert environment["BROWSER"]["value"] == "0"


def test_falkordb_declares_the_measured_query_controls() -> None:
    """P-82 gate-5 live evidence (25 August 2026): the declared queue
    ceiling fixed the original refusal, then the retained graph exposed the
    pinned image's 1,000 ms timeout. The exact failing read completed in
    1,207.9 ms under a temporary 5,000 ms bound, which remains bounded and
    must be identical on both deployment targets."""
    falkordb = _named(_documents("kind"), "StatefulSet", "falkordb")
    environment = _environment(_container(falkordb, "falkordb"))
    assert environment["FALKORDB_ARGS"]["value"] == (
        "MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000"
    )


def test_falkordb_can_read_its_credential_as_the_identity_it_runs_as() -> None:
    """The index's identity and its Secret's mode are one property.

    Pinning the two separately is what let this through the first time.
    A guard on ``runAsGroup == 0`` alone still passes if the mode later
    becomes 0400 — the exact value task 4's review had to correct — and a
    guard on the mode alone still passes if the identity moves out of
    group 0. So this asserts the thing that actually has to hold: the
    identity the pod declares can open the file at the mode it is
    projected with, and no one else can.

    The history is worth keeping. This overlay ran as uid 999 with no
    ``runAsGroup``; the pinned image names 999 as ``redis`` in group 999,
    the Secret is ``root:root`` at 0440 (P-67), and the index crash-looped
    on ``Permission denied`` reading the ``requirepass`` file it is
    started with — found by the kind acceptance harness on 13 August 2026,
    on a render whose every other guard was green.
    """
    falkordb = _named(_documents("kind"), "StatefulSet", "falkordb")
    security = _pod_spec(falkordb)["securityContext"]
    container = _container(falkordb, "falkordb")

    mount = [
        entry
        for entry in container["volumeMounts"]
        if entry["mountPath"] == "/etc/falkordb"
    ]
    assert len(mount) == 1
    volume = [
        entry
        for entry in _pod_spec(falkordb)["volumes"]
        if entry["name"] == mount[0]["name"]
    ]
    assert len(volume) == 1
    mode = volume[0]["secret"]["defaultMode"]

    uid = security["runAsUser"]
    gid = security["runAsGroup"]
    assert _readable_by(mode, uid, gid), (
        f"uid {uid} gid {gid} cannot read a root:root credential at {mode:04o}"
    )
    # A credential is not world-readable, so the group bit above is doing
    # the work rather than a permissive mode hiding the question.
    assert not mode & 0o007, f"credential is other-readable at {mode:04o}"

    # Belt to the group's braces: the UID must be one the image's passwd
    # file does not name, so that dropping ``runAsGroup`` could not
    # reintroduce the fault silently.
    assert uid not in FALKORDB_IMAGE_PASSWD_UIDS, (
        f"uid {uid} has a passwd entry in the pinned image; "
        "without runAsGroup it would resolve outside group 0"
    )


def test_every_instance_render_denies_ingress_and_egress_by_default() -> None:
    for name in INSTANCE_RENDERS:
        documents = _documents(name)
        deny = _named(documents, "NetworkPolicy", "cairn-default-deny")
        assert deny["spec"]["podSelector"]["matchLabels"] == CAIRN_LABELS
        assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]
        assert "ingress" not in deny["spec"]
        assert "egress" not in deny["spec"]


def test_the_index_denies_everything_it_is_not_explicitly_told_to_serve() -> None:
    documents = _documents("kind")
    deny = _named(documents, "NetworkPolicy", "falkordb-default-deny")
    assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]

    allow = _named(documents, "NetworkPolicy", "falkordb-allow-cairn-ingress")
    assert allow["spec"]["policyTypes"] == ["Ingress"]
    assert allow["spec"]["ingress"][0]["from"] == [
        {"podSelector": {"matchLabels": CAIRN_LABELS}}
    ]
    assert allow["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 6379}]


def test_kubernetes_overlay_names_its_storage_class_and_keeps_the_strict_claim() -> (
    None
):
    """I-10 on the one target whose provisioner this project controls."""
    claim = _named(_documents("kubernetes"), "StatefulSet", "cairn")
    template = claim["spec"]["volumeClaimTemplates"][0]["spec"]
    assert template["storageClassName"] == "cairn-local"
    assert template["accessModes"] == ["ReadWriteOncePod"]


def test_kind_overlay_relaxes_the_claim_and_says_nothing_about_storage() -> None:
    """I-10's stated allowance: kind's provisioner has no ReadWriteOncePod.

    The acceptance run then proves duplicate-process exclusion through
    the data-directory lease instead, which is why relaxing it here does
    not weaken the property — only the layer that enforces it.
    """
    claim = _named(_documents("kind"), "StatefulSet", "cairn")
    template = claim["spec"]["volumeClaimTemplates"][0]["spec"]
    assert template["accessModes"] == ["ReadWriteOnce"]
    assert "storageClassName" not in template


def test_gateway_admits_only_labelled_cairn_pods_and_egresses_only_to_dns_and_443() -> (
    None
):
    """I-93 verbatim, asserted on the render rather than on the prose."""
    documents = _documents(GATEWAY_RENDER)
    namespace = _named(documents, "Namespace", GATEWAY_NAMESPACE)
    assert namespace["metadata"]["labels"]["app.kubernetes.io/name"] == GATEWAY_NAME

    deny = _named(documents, "NetworkPolicy", "egress-gateway-default-deny")
    assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]

    ingress = _named(documents, "NetworkPolicy", "egress-gateway-allow-cairn-ingress")
    assert ingress["spec"]["ingress"][0]["from"] == [
        {
            "namespaceSelector": {
                "matchExpressions": [
                    {"key": INSTANCE_NAMESPACE_LABEL, "operator": "Exists"}
                ]
            },
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "cairn"}},
        }
    ]
    assert ingress["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 3128}]

    egress = _named(documents, "NetworkPolicy", "egress-gateway-allow-egress")
    ports = [port for rule in egress["spec"]["egress"] for port in rule["ports"]]
    assert {"protocol": "TCP", "port": 443} in ports
    assert {"protocol": "UDP", "port": 53} in ports
    assert {"protocol": "TCP", "port": 53} in ports
    assert not [port for port in ports if port["port"] not in (443, 53)]


def test_reference_gateway_denies_cluster_tcp_443() -> None:
    """Reference keeps the portable DNS rule but cannot reach cluster HTTPS."""
    documents = _documents(REFERENCE_GATEWAY_RENDER)
    policy = _named(documents, "NetworkPolicy", "egress-gateway-allow-egress")
    generic_policy = _named(
        _documents(GATEWAY_RENDER), "NetworkPolicy", "egress-gateway-allow-egress"
    )

    assert len(policy["spec"]["egress"]) == 2
    assert policy["spec"]["egress"][0] == generic_policy["spec"]["egress"][0]
    assert policy["spec"]["egress"][1]["to"] == [
        {
            "ipBlock": {
                "cidr": "0.0.0.0/0",
                "except": [
                    "127.0.0.0/8",
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "169.254.0.0/16",
                ],
            }
        }
    ]
    assert policy["spec"]["egress"][1]["ports"] == [{"protocol": "TCP", "port": 443}]

    cilium = _named(
        documents, "CiliumNetworkPolicy", "egress-gateway-deny-cluster-https"
    )
    assert cilium["metadata"]["namespace"] == GATEWAY_NAMESPACE
    assert cilium["spec"]["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": GATEWAY_NAME}
    }
    assert cilium["spec"]["egressDeny"] == [
        {
            "toEntities": ["cluster", "host", "remote-node", "kube-apiserver"],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
    ]
    assert not _of_kind(_documents(GATEWAY_RENDER), "CiliumNetworkPolicy")


def test_gateway_proxy_configuration_permits_only_allow_listed_connect_on_443() -> None:
    """P-64: the one-page squid.conf a reviewer can read whole.

    Order is the whole of squid's semantics — a rule after ``http_access
    deny all`` is a rule that never fires — so the directives are checked
    in sequence and not merely for presence.
    """
    configmap = _named(_documents(GATEWAY_RENDER), "ConfigMap", "egress-gateway-config")
    directives = [
        line.strip()
        for line in configmap["data"]["squid.conf"].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    access = [line for line in directives if line.startswith("http_access")]
    assert access == [
        "http_access deny !CONNECT",
        "http_access deny CONNECT !SSL_ports",
        "http_access allow CONNECT allowed_fqdns",
        "http_access deny all",
    ]
    assert "acl SSL_ports port 443" in directives
    assert "acl allowed_fqdns dstdomain api.openai.com" in directives
    assert "http_port 3128" in directives
    assert "max_filedescriptors 65536" in directives
    assert "cache deny all" in directives
