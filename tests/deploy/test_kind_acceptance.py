"""Slice 8 task 7: the guards the kind acceptance harness must satisfy.

The harness itself is an evidence tier run on demand against a real
cluster (P-68), and nothing here runs it: these tests must mean the same
thing on a host with no Docker, no kind and no network, exactly as
``test_compose_project.py`` beside them means the same thing on a host
with no Docker. What they hold is the harness's *structure* — the three
properties that decide whether a passing run is worth anything.

- **P-61's single pin.** Every version, digest and checksum the harness
  depends on comes from ``deploy/images.lock``. A literal pinned in the
  script is a pin that can be changed in one consumer and forgotten in
  another, which is the whole reason the lock file exists.
- **I-07's order, verbatim.** Enforcement is proved active *before*
  anything else runs, and a run in which a default-deny probe connects
  is a failed run, never a skipped check. The guard is on the order of
  the top-level calls, because "first" is a claim about sequence and
  nothing else can hold it.
- **Total coverage of the committed renders.** The server-side shape
  check (moved here from task 4 on Operator's ruling of 13 August 2026)
  iterates the render directory rather than a list, so a render added
  tomorrow is covered from the moment it is committed.

The report emitter is held to its P-68 field list for the same reason
the checks are recorded at all: a transcript that does not say which
kind, which Kubernetes, which Calico and which image produced it is not
evidence of anything.
"""

import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import threading
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY / "scripts" / "kind-acceptance"
IMAGES_LOCK = REPOSITORY / "deploy" / "images.lock"
MAKEFILE = REPOSITORY / "Makefile"
RENDER_DIR = REPOSITORY / "deploy" / "kustomize" / "rendered"
ACCEPTANCE_SCRIPTS = REPOSITORY / "tests" / "acceptance" / "kind"
UNIQUE_NAME_FILTER = ACCEPTANCE_SCRIPTS / "unique-check-name.jq"
COREDNS_REWRITE = ACCEPTANCE_SCRIPTS / "rewrite-coredns.awk"
PROVIDER = ACCEPTANCE_SCRIPTS / "provider.py"
CATALOGUE_SOURCE = REPOSITORY / "src" / "cairn" / "catalogue" / "sqlite.py"
ATTIC_SOURCE = REPOSITORY / "src" / "cairn" / "evidence" / "attic.py"
ATTIC_PROBE = ACCEPTANCE_SCRIPTS / "attic.py"

SOURCE = HARNESS.read_text(encoding="utf-8")


def _load_provider() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kind_acceptance_provider", PROVIDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fake_provider_returns_schema_conforming_empty_extractions() -> None:
    provider = _load_provider()
    schema = {
        "$defs": {
            "Entity": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
        },
        "type": "object",
        "properties": {
            "extracted_entities": {
                "type": "array",
                "items": {"$ref": "#/$defs/Entity"},
            }
        },
        "required": ["extracted_entities"],
    }

    assert provider.value_for_schema(schema) == {"extracted_entities": []}


def test_the_fake_provider_speaks_the_openai_responses_shape() -> None:
    provider = _load_provider()
    server = provider.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses",
            data=json.dumps(
                {
                    "model": "gpt-5.5",
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "ExtractedEntities",
                            "schema": {
                                "type": "object",
                                "properties": {"extracted_entities": {"type": "array"}},
                                "required": ["extracted_entities"],
                            },
                        }
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            document = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert document["object"] == "response"
    content = document["output"][0]["content"][0]
    assert content["type"] == "output_text"
    assert json.loads(content["text"]) == {"extracted_entities": []}


def test_the_fake_provider_keeps_zero_embeddings_unless_opted_into_valid_vectors() -> (
    None
):
    provider = _load_provider()

    def embedding(*, valid_embeddings: bool) -> list[float]:
        server = provider.make_server("127.0.0.1", 0, valid_embeddings=valid_embeddings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/embeddings",
                data=json.dumps(
                    {"model": "text-embedding-3-small", "input": ["x"]}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return cast(list[float], json.load(response)["data"][0]["embedding"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    default = embedding(valid_embeddings=False)
    valid = embedding(valid_embeddings=True)

    assert default == [0.0] * 1024
    assert valid == [1.0] + [0.0] * 1023


def _unique_name_result(
    checks: str, name: str, stage: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "jq",
            "-e",
            "-s",
            "--arg",
            "name",
            name,
            "--arg",
            "stage",
            stage,
            "-f",
            str(UNIQUE_NAME_FILTER),
        ],
        input=checks,
        text=True,
        capture_output=True,
        check=False,
    )


def test_a_new_check_identity_is_accepted() -> None:
    checks = '{"name":"first","stage":"bring-up"}\n'
    assert _unique_name_result(checks, "second", "bring-up").returncode == 0


def test_the_same_check_name_in_another_stage_is_accepted() -> None:
    checks = '{"name":"first","stage":"bring-up"}\n'
    assert _unique_name_result(checks, "first", "single-instance").returncode == 0


def test_a_duplicate_check_identity_is_refused() -> None:
    checks = '{"name":"first","stage":"bring-up"}\n'
    assert _unique_name_result(checks, "first", "bring-up").returncode == 1


def test_both_report_recorders_refuse_a_duplicate_before_append() -> None:
    for recorder in ("require_check", "require_bound"):
        body = re.search(rf"^{recorder}\(\) \{{\n(.*?)\n\}}", SOURCE, re.M | re.S)
        assert body, recorder
        assert body.group(1).index(
            'require_unique_check_name "$name" "$stage"'
        ) < body.group(1).index("jq -nc")


def test_the_coredns_rewrite_is_exact_and_preserves_the_corefile() -> None:
    source = ".:53 {\n    errors\n    forward . /etc/resolv.conf\n}\n"
    result = subprocess.run(
        [
            "awk",
            "-v",
            "source=api.example.invalid",
            "-v",
            "target=fake-provider.test.svc.cluster.local",
            "-f",
            str(COREDNS_REWRITE),
        ],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        ".:53 {\n"
        "    rewrite name exact api.example.invalid "
        "fake-provider.test.svc.cluster.local\n"
        "    errors\n"
        "    forward . /etc/resolv.conf\n"
        "}\n"
    )


# Every key the harness is required to read from the lock rather than
# carry itself. Each one is a pin some other consumer also reads.
REQUIRED_LOCK_KEYS = (
    "CAIRN_IMAGE",
    "FALKORDB_IMAGE",
    "EGRESS_PROXY_IMAGE",
    "KIND_VERSION",
    "KIND_LINUX_AMD64_SHA256",
    "KIND_NODE_IMAGE",
    "KUBECTL_VERSION",
    "KUBECTL_LINUX_AMD64_SHA256",
    "CALICO_MANIFEST_URL",
    "CALICO_MANIFEST_SHA256",
    "CILIUM_CNP_CRD_URL",
    "CILIUM_CNP_CRD_SHA256",
)

# The P-68 identity record. A run's report names each of these or the
# run cannot be quoted as evidence for a particular stack.
REQUIRED_IDENTITY_FIELDS = (
    "kind_version",
    "kubernetes_version",
    "kubectl_version",
    "kind_node_image",
    "calico_manifest_url",
    "calico_manifest_sha256",
    "cilium_cnp_crd_url",
    "cilium_cnp_crd_sha256",
    "cairn_image",
    "cairn_image_id",
    "falkordb_image",
    "egress_proxy_image",
)


def _lock_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in IMAGES_LOCK.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key] = value
    return values


def _top_level_calls() -> list[str]:
    """The harness's unindented call sequence, in source order.

    Unindented because that is the main body: a function *definition*
    proves nothing about when — or whether — it runs, and this module's
    claim is about order.
    """
    calls = []
    for line in SOURCE.splitlines():
        if line.startswith((" ", "\t", "#")) or not line.strip():
            continue
        match = re.match(r"^([a-z_]+)(?: |$)", line)
        if match and f"{match.group(1)}()" in SOURCE:
            calls.append(match.group(1))
    return calls


def test_the_harness_is_executable() -> None:
    assert HARNESS.is_file()
    assert HARNESS.stat().st_mode & stat.S_IXUSR


def test_every_pin_is_read_from_the_lock() -> None:
    """P-61: the harness reads each pin, and the lock supplies it."""
    lock = _lock_values()
    for key in REQUIRED_LOCK_KEYS:
        assert f"lock_value {key}" in SOURCE, key
        assert lock.get(key), key


def test_the_kind_binary_checksum_is_a_checksum() -> None:
    """The pin added by this task, held to the shape of the one beside
    it: kubectl's checksum has always been verified, and the binary that
    creates the cluster is no less load-bearing than the one that talks
    to it."""
    lock = _lock_values()
    assert re.fullmatch(r"[0-9a-f]{64}", lock["KIND_LINUX_AMD64_SHA256"])


def test_the_cilium_validation_crd_matches_the_target_version() -> None:
    """The schema fixture must be the official CNP CRD for the Cilium
    version named by the target profile, with its content pinned locally.
    """
    lock = _lock_values()
    expected_url = (
        "https://raw.githubusercontent.com/cilium/cilium/"
        f"{lock['TARGET_CILIUM_VERSION']}"
        "/pkg/k8s/apis/cilium.io/client/crds/v2/ciliumnetworkpolicies.yaml"
    )
    assert lock["CILIUM_CNP_CRD_URL"] == expected_url
    assert re.fullmatch(r"[0-9a-f]{64}", lock["CILIUM_CNP_CRD_SHA256"])


def test_the_harness_carries_no_pin_of_its_own() -> None:
    """Nothing that belongs in the lock may appear in the script.

    The search is over literal values rather than key names: a script may
    name ``KIND_VERSION`` as often as it likes, and may never name
    ``v0.31.0``.
    """
    lock = _lock_values()
    pinned = {key: lock[key] for key in REQUIRED_LOCK_KEYS}
    for key, value in pinned.items():
        assert value not in SOURCE, f"{key} is pinned in the harness"
    # Digests and checksums, wherever they came from. A 64-character hex
    # run in a shell script is a pin by any other name.
    assert not re.search(r"\b[0-9a-f]{64}\b", SOURCE)


def test_enforcement_is_proved_active_before_anything_else() -> None:
    """I-07 verbatim, and the reason this module exists at all.

    A harness that proves isolation on a cluster whose policy engine was
    never enforcing proves nothing, and the failure is silent — every
    probe is refused, every expectation is met, and the run is green.
    """
    calls = _top_level_calls()
    assert calls, "the harness has no top-level calls to order"
    # Acquiring tools and creating the cluster are not evidence; the
    # claim is that nothing which *concludes* anything runs before
    # enforcement has been shown to be on.
    evidence = [
        call
        for call in calls
        if call.startswith(("prove_", "validate_", "bring_up_", "bootstrap_"))
    ]
    assert evidence[0] == "prove_enforcement_active"
    assert evidence.index("validate_committed_renders") < evidence.index(
        "bring_up_instance"
    )


def test_the_cilium_crd_is_only_a_shape_validation_fixture() -> None:
    """The kind tier uses Calico for enforcement. It installs only the
    CNP schema, after proving Calico enforcement and before validating every
    render; it must not install a Cilium agent or controller.
    """
    calls = _top_level_calls()
    assert calls.index("prove_enforcement_active") < calls.index(
        "install_shape_validation_crds"
    )
    assert calls.index("install_shape_validation_crds") < calls.index(
        "validate_committed_renders"
    )
    function = re.search(
        r"install_shape_validation_crds\(\) \{\n(.*?)\n\}", SOURCE, re.S
    )
    assert function
    body = function.group(1)
    assert '"$kubectl" apply --server-side -f "$temporary/cilium-cnp-crd.yaml"' in body
    assert "crd/ciliumnetworkpolicies.cilium.io" in body
    assert "cilium install" not in SOURCE
    assert "helm install" not in SOURCE


def test_the_enforcement_probe_requires_both_observations() -> None:
    """Denied *and* permitted. A probe that cannot observe a connection
    at all reports every target as blocked, which is indistinguishable
    from enforcement working perfectly."""
    assert "enforcement-default-deny" in SOURCE
    assert "enforcement-permitted-control" in SOURCE


def _records(name: str) -> bool:
    """Whether the harness records a check whose name starts here.

    A prefix rather than a whole name because the per-instance checks are
    built by interpolation — `"instance-to-own-index:${instance}"` — and
    the guard is that the claim is recorded at all, not how the suffix is
    spelled. Either recorder: the termination claim is a bound rather
    than a value, and `require_bound` records it in the same stream.
    """
    return bool(
        re.search(rf'^\s*require_(?:check|bound) "?{re.escape(name)}', SOURCE, re.M)
    )


def test_the_single_instance_checks_are_all_present() -> None:
    """The task's own list, each entry named in the report by the name a
    reader would look for."""
    for name in (
        "migrate-init-container:",
        "bootstrap-token:",
        "v1-round-trip:",
        "persistence-across-pod-deletion",
        "duplicate-process-refusal",
        "termination-within-grace",
    ):
        assert _records(name), name


# P-68 stage 2, entry by entry. Each is an attempted connection with a
# predicted outcome, and the point of listing them here is that the
# matrix cannot quietly lose one: a claim that stops being made is a
# claim that stops being able to fail.
TWO_INSTANCE_CHECKS = (
    "v1-round-trip:",
    "instance-to-other-instance:",
    "instance-to-own-index:",
    "instance-to-foreign-index:",
    "client-to-other-instance:",
    "client-to-own-index:",
    "client-to-gateway:",
    "instance-to-gateway:",
    "instance-to-provider-direct:",
    "provider-reachable-control",
    "gateway-connect-allowed",
    "gateway-connect-refused",
    "gateway-connect-refused-reachable",
    "endpoint-own-selection:",
    "endpoint-foreign-selection:",
    "credential-cross-selection",
    "fixture-cross-selection:",
)

# The pairing that makes each refusal mean something, and the lesson the
# enforcement proof at the top of the harness already records: a probe
# that cannot reach its target reports every destination as blocked,
# which is indistinguishable from policy working perfectly. Every
# negative below is therefore named with the positive that proves the
# target was reachable by *something*.
CONTROLLED_NEGATIVES = {
    "instance-to-other-instance:": "v1-round-trip:",
    "client-to-other-instance:": "v1-round-trip:",
    "instance-to-foreign-index:": "instance-to-own-index:",
    "client-to-own-index:": "instance-to-own-index:",
    "client-to-gateway:": "instance-to-gateway:",
    "instance-to-provider-direct:": "provider-reachable-control",
    "gateway-connect-refused": "gateway-connect-allowed",
    "gateway-connect-refused-reachable": "provider-reachable-control",
    "endpoint-foreign-selection:": "endpoint-own-selection:",
    "credential-cross-selection": "v1-round-trip:",
    "fixture-cross-selection:": "v1-round-trip:",
}


def test_the_two_instance_matrix_is_complete() -> None:
    for name in TWO_INSTANCE_CHECKS:
        assert _records(name), name


def test_the_backup_restore_stage_records_every_i17_observation() -> None:
    for name in (
        "pre-backup-attic-payload",
        "stage3-provider-rollout",
        "backup-under-live-traffic",
        "backup-exec",
        "backup-bundle-copied",
        "backup-member-digests",
        "original-quiesced",
        "replacement-pvc-fresh",
        "restore-command",
        "restored-catalogue-integrity",
        "restored-audit-boundary",
        "rebuild-index",
        "restored-attic-payload",
        "restored-index-episode",
        "restored-scoped-read-rest",
        "restored-scoped-read-mcp",
        "original-pvc-retained",
        "traffic-moved-after-acceptance",
    ):
        assert _records(name), name


def test_backup_restore_asks_attic_and_the_rebuilt_index_directly() -> None:
    attic = re.search(r"^attic_payload_presence\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S)
    index = re.search(
        r"^restored_index_episode_presence\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S
    )
    probe = ATTIC_PROBE.read_text(encoding="utf-8")
    assert attic and '<"$acceptance_scripts/attic.py"' in attic.group(1)
    assert "mode=ro" in probe and "SELECT 1 FROM payloads" in probe
    assert index and "GRAPH.RO_QUERY" in index.group(1)
    assert "pod/falkordb-restored-0" in index.group(1)
    assert "redis-cli --raw" in index.group(1)
    assert "requirepass /AUTH /p" in index.group(1)


def test_traffic_moves_only_after_the_endpoints_controller_converges() -> None:
    wait = re.search(
        r"^wait_for_service_endpoint\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S
    )
    restore = re.search(r"^prove_backup_restore\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S)
    assert wait and "endpoints/cairn" in wait.group(1)
    assert "for _ in $(seq 1 " in wait.group(1) and "sleep " in wait.group(1)
    assert restore and "wait_for_service_endpoint" in restore.group(1)


def test_backup_restore_runs_after_isolation_and_records_the_barrier() -> None:
    calls = _top_level_calls()
    assert calls.index("prove_cross_selection") < calls.index("prove_backup_restore")
    assert re.search(r"^\s*--argjson backup_barrier_ms ", SOURCE, re.M)
    assert "barrier_ms" in SOURCE
    assert SOURCE.count("fromjson?") >= 2


def test_backup_copy_uses_kubectl_pod_file_specs() -> None:
    assert '"cairn-0:${bundle_path}" "$bundle_host"' in SOURCE
    assert '"$bundle_host" "cairn-restore:/backup/bundle"' in SOURCE
    assert '"pod/cairn-0:${bundle_path}"' not in SOURCE


def test_backup_member_names_match_the_product_constants() -> None:
    catalogue = re.search(
        r'^CATALOGUE_FILENAME = "([^"]+)"$',
        CATALOGUE_SOURCE.read_text(encoding="utf-8"),
        re.M,
    )
    attic = re.search(
        r'^ATTIC_FILENAME = "([^"]+)"$',
        ATTIC_SOURCE.read_text(encoding="utf-8"),
        re.M,
    )
    assert catalogue and attic
    expected = f'["{attic.group(1)}", "{catalogue.group(1)}"]'
    assert expected in SOURCE


def test_every_refusal_has_a_positive_control() -> None:
    for negative, control in CONTROLLED_NEGATIVES.items():
        assert _records(negative), negative
        assert _records(control), f"{negative} has no control: {control}"


def test_the_second_instance_is_a_second_identity_in_its_own_stage() -> None:
    """P-68 stage 2 is two instances, each brought up and bootstrapped by
    the same path as the first — distinct namespace, credentials, PVC and
    `instance_id` fall out of that, and a shortcut that reused any of
    them would make every refusal meaningless.

    Each also records under the stage it actually ran in. A report that
    files the second instance's bring-up under `single-instance` is a
    report that cannot be read back against the stage it evidences.
    """
    for verb in ("bring_up_instance", "bootstrap_instance", "prove_round_trip"):
        assert re.search(rf"^{verb} cairn-a single-instance$", SOURCE, re.M), verb
        assert re.search(rf"^{verb} cairn-b two-instance$", SOURCE, re.M), verb


def test_the_allow_listed_destination_comes_from_the_render() -> None:
    """The gateway's allow-list is the operator-editable value in
    `egress-gateway.yaml`. A copy of it here is a copy that can disagree
    with the artefact under test, and the disagreement would show up as a
    passing run."""
    rendered = (RENDER_DIR / "egress-gateway.yaml").read_text(encoding="utf-8")
    allowed = re.search(r"acl allowed_fqdns dstdomain (\S+)", rendered)
    assert allowed, "the rendered gateway has no allow-list to read"
    assert allowed.group(1) not in SOURCE
    assert "allowed_fqdns" in SOURCE


def test_the_provider_dns_rewrite_precedes_the_gateway_checks() -> None:
    calls = _top_level_calls()
    assert calls.index("deploy_fake_provider") < calls.index(
        "install_provider_dns_rewrite"
    )
    assert calls.index("install_provider_dns_rewrite") < calls.index(
        "bring_up_instance"
    )
    assert calls.index("install_provider_dns_rewrite") < calls.index(
        "prove_gateway_allow_list"
    )


def test_the_provider_dns_rewrite_is_bring_up_evidence() -> None:
    body = re.search(
        r"^install_provider_dns_rewrite\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S
    )
    assert body
    for name in ("coredns-rewrite-rollout", "allow-listed-dns-is-hermetic"):
        assert re.search(rf"require_check {name} bring-up ", body.group(1)), name


def test_the_fake_provider_listens_on_the_gateway_policy_port() -> None:
    assert re.search(r"^provider_target_port=\$tls_port$", SOURCE, re.M)


def test_the_graph_fixture_explicitly_requests_valid_provider_vectors() -> None:
    """The provider keeps zero vectors as its compatibility default. This
    graph-backed acceptance fixture must opt into the non-zero vector required
    by the production validation path rather than changing that default.
    """
    body = re.search(r"^deploy_fake_provider\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S)
    assert body
    assert re.search(
        r'/opt/acceptance/provider\.py "\$provider_target_port" \\\n'
        r"    --valid-embeddings",
        body.group(1),
    )
    assert SOURCE.count("--valid-embeddings") == 1


def test_only_the_recovery_candidate_gets_the_fake_provider_route() -> None:
    render = re.search(r"^render_instance\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S)
    route = re.search(
        r"^install_recovery_provider_route\(\) \{\n(.*?)\n\}", SOURCE, re.M | re.S
    )
    assert render and "OPENAI_BASE_URL" not in render.group(1)
    assert route and "- stage3" in route.group(1) and "- restored" in route.group(1)
    assert "fake-provider.${provider_namespace}.svc.cluster.local" in SOURCE


def test_endpoint_selection_observes_both_processes_from_both_indexes() -> None:
    for name in (
        "endpoint-own-selection:${a}",
        "endpoint-foreign-selection:${a}->${b}",
        "endpoint-own-selection:${b}",
        "endpoint-foreign-selection:${b}->${a}",
    ):
        assert name in SOURCE, name
    assert "CLIENT LIST" in SOURCE
    assert "{.status.podIP}" in SOURCE


def test_the_refused_destination_cannot_resolve() -> None:
    """RFC 2606's reserved suffix, deliberately: if the ACL were broken
    the probe must still be incapable of reaching anything real."""
    refused = re.search(r"^refused_fqdn=(\S+)", SOURCE, re.M)
    assert refused, "the harness names no destination the gateway must refuse"
    assert refused.group(1).endswith(".invalid")


def test_probes_from_an_instance_run_the_committed_scripts() -> None:
    """The workload's own pod is the only place a probe can wear the
    workload's labels, and the image carries no acceptance scripts — so
    they arrive on stdin. That they arrive *from the committed files* is
    the guard: an inline program would be a second copy of code this
    repository reviews once.
    """
    for script in ("probe.py", "connect.py"):
        assert f'<"$acceptance_scripts/{script}"' in SOURCE, script
    assert "python -c" not in SOURCE


def test_the_shape_check_iterates_the_render_directory() -> None:
    """Coverage that cannot fall behind the artefacts.

    A hard-coded list is a list somebody has to remember to extend; the
    day they do not, a render nobody validated is the one an operator
    applies.
    """
    assert RENDER_DIR.glob("*.yaml"), "there are no committed renders to check"
    loop = re.search(
        r"for render in deploy/kustomize/rendered/\*\.yaml; do\n(.*?)\n  done",
        SOURCE,
        re.S,
    )
    assert loop, "the shape check is not a loop over the render directory"
    assert "--dry-run=server" in loop.group(1)
    # Once, and only inside that loop. A second invocation elsewhere would
    # be a render validated by name, which is the arrangement this guard
    # exists to refuse.
    code = "\n".join(
        line for line in SOURCE.splitlines() if not line.lstrip().startswith("#")
    )
    assert code.count("--dry-run=server") == 1


def test_the_report_names_every_identity() -> None:
    for field in REQUIRED_IDENTITY_FIELDS:
        assert field in SOURCE, field


def test_the_report_is_emitted_whatever_the_outcome() -> None:
    """A failed run is the one whose transcript is worth most."""
    assert "emit_report" in SOURCE
    assert re.search(r"trap .*cleanup.* EXIT", SOURCE)


@pytest.mark.parametrize("owned", [False, True])
def test_interrupt_reports_failure_and_cleans_only_the_owned_cluster(
    tmp_path: Path, owned: bool
) -> None:
    """SIGINT keeps its status even when cleanup fails, and cannot claim pass."""
    report = tmp_path / "report.json"
    calls = tmp_path / "kind.calls"
    fake_kind = tmp_path / "kind"
    fake_kind.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$KIND_CALLS"\nexit 23\n',
        encoding="utf-8",
    )
    fake_kind.chmod(0o755)

    lifecycle_end = SOURCE.index("# One prediction, one observation")
    lifecycle = SOURCE[:lifecycle_end].replace(
        'cd "$(dirname "${BASH_SOURCE[0]}")/.."',
        f"cd {str(REPOSITORY)!r}",
    )
    probe = tmp_path / "interrupt-probe"
    probe.write_text(
        lifecycle
        + f"kind={str(fake_kind)!r}\n"
        + f"cluster_owned={str(owned).lower()}\n"
        + 'printf "ready %s\\n" "$temporary"\n'
        + "while :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)

    process = subprocess.Popen(
        [str(probe)],
        cwd=REPOSITORY,
        env={
            "IMAGE": "cairn:test-signal",
            "KIND_ACCEPTANCE_REPORT": str(report),
            "KIND_CALLS": str(calls),
            "PATH": "/usr/bin:/bin",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        # A suite launched as a shell background job inherits SIGINT ignored,
        # and bash cannot trap a signal ignored at entry; the probe must see
        # the default disposition whatever launched pytest.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    assert process.stdout is not None
    temporary = Path(process.stdout.readline().removeprefix("ready ").strip())
    os.killpg(process.pid, signal.SIGINT)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 130, (stdout, stderr)
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["termination_signal"] == "INT"
    assert document["checks"] == []
    expected_calls = [f"delete cluster --name {document['cluster']}"] if owned else []
    observed_calls = (
        calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    )
    assert observed_calls == expected_calls
    assert not temporary.exists()


def test_the_cluster_is_the_pinned_shape() -> None:
    """I-15: one control plane and two workers, and Calico rather than
    kindnet — a single-node cluster cannot observe a policy decision made
    between nodes."""
    assert SOURCE.count("role: worker") == 2
    assert "role: control-plane" in SOURCE
    assert "disableDefaultCNI: true" in SOURCE


def test_make_exposes_the_harness() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "kind-acceptance:" in makefile
    assert "IMAGE=$(IMAGE) ./scripts/kind-acceptance" in makefile
    assert re.search(r"^\.PHONY:(.|\\\n)*kind-acceptance", makefile, re.MULTILINE)
