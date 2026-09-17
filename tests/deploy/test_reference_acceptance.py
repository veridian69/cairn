"""Task 11 guards for the real ``reference`` acceptance workflow.

These tests do not claim target evidence.  They hold the local harness and
operator runbook to the accepted P-70/I-95 procedure so a live run cannot
quietly omit a boundary or leak the provider credential.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from scripts.reference_acceptance_state import (  # noqa: E402
    HarnessRefusal,
    backup_command_overlap,
    bootstrap_secret_command,
    build_report,
    canonical_image_reference,
    claim_bindings,
    endpoint_candidate_converged,
    invocation_action,
    recovery_transition,
    resolve_imported_image,
    runtime_image_digest,
    rwop_refusal_is_current,
    service_selector_patch,
    stamp_targets_for_rendered_objects,
    validate_inventory_ownership,
    validate_storage_inventory,
    write_report_exclusive,
)

HARNESS = REPOSITORY / "scripts" / "reference-acceptance"
RUNBOOK = REPOSITORY / "docs" / "runbooks" / "runbook-kubernetes-recovery.md"
HOST_RUNBOOK = REPOSITORY / "docs" / "runbooks" / "runbook-reference-acceptance-host.md"
MAKEFILE = REPOSITORY / "Makefile"
IMAGES_LOCK = REPOSITORY / "deploy" / "images.lock"
STATE_HELPER = REPOSITORY / "scripts" / "reference_acceptance_state.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}", source, re.M | re.S)
    assert match, name
    return match.group(1)


def _top_level_calls(source: str) -> list[str]:
    run = source.split("# run ------------------------------------------------", 1)[1]
    return re.findall(r"^([a-z][a-z0-9_]*)(?:$| )", run, re.M)


def _run_harness_cleanup(
    tmp_path: Path, *, run_status: int, report_status: int
) -> subprocess.CompletedProcess[str]:
    cleanup = _function(_source(HARNESS), "cleanup")
    script = f"""
set -euo pipefail
evidence_active=true
temporary=$FAKE_TEMPORARY
mkdir -p "$temporary"
emit_report() {{ return "$FAKE_REPORT_STATUS"; }}
cleanup() {{
{cleanup}
}}
trap cleanup EXIT
exit "$FAKE_RUN_STATUS"
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "FAKE_TEMPORARY": str(tmp_path / "temporary"),
            "FAKE_RUN_STATUS": str(run_status),
            "FAKE_REPORT_STATUS": str(report_status),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_successful_harness_exit_fails_when_report_generation_fails(
    tmp_path: Path,
) -> None:
    result = _run_harness_cleanup(tmp_path, run_status=0, report_status=17)

    assert result.returncode == 17


def test_failed_harness_exit_is_preserved_when_report_generation_fails(
    tmp_path: Path,
) -> None:
    result = _run_harness_cleanup(tmp_path, run_status=1, report_status=17)

    assert result.returncode == 1


def test_target_harness_is_an_executable_make_target() -> None:
    mode = HARNESS.stat().st_mode
    assert mode & stat.S_IXUSR
    makefile = _source(MAKEFILE)
    assert "reference-acceptance:" in makefile
    assert "./scripts/reference-acceptance" in makefile


def test_host_helper_and_harness_work_with_python3_but_no_python(
    tmp_path: Path,
) -> None:
    (tmp_path / "python3").symlink_to(Path(sys.executable).resolve())
    for command in ("awk", "dirname", "git", "mktemp", "rm"):
        executable = shutil.which(command)
        assert executable
        (tmp_path / command).symlink_to(executable)
    host_path = str(tmp_path)
    assert shutil.which("python3", path=host_path)
    assert shutil.which("python", path=host_path) is None
    fake_id = tmp_path / "id"
    fake_id.write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")
    fake_id.chmod(0o755)

    helper = subprocess.run(
        ["python3", str(STATE_HELPER), "mutable-resources"],
        cwd=REPOSITORY,
        env={**os.environ, "PATH": host_path},
        text=True,
        capture_output=True,
        check=True,
    )
    assert "serviceaccount" in helper.stdout

    harness = subprocess.run(
        ["/bin/bash", str(HARNESS), "preflight"],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": host_path,
            "IMAGE": "cairn:python3-test",
            "REFERENCE_ACCEPTANCE_RUN_ID": "python3-test",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert harness.returncode == 1
    assert "run as root on the configured reference host" in harness.stderr
    assert "python: command not found" not in harness.stderr


def test_harness_fails_clearly_when_host_python3_is_missing(tmp_path: Path) -> None:
    dirname = tmp_path / "dirname"
    dirname.symlink_to("/usr/bin/dirname")
    result = subprocess.run(
        ["/bin/bash", str(HARNESS), "preflight"],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": str(tmp_path),
            "IMAGE": "cairn:no-python3-test",
            "REFERENCE_ACCEPTANCE_RUN_ID": "no-python3-test",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == "reference acceptance requires host python3 on PATH\n"


def test_every_host_python_call_uses_the_preflighted_interpreter() -> None:
    source = _source(HARNESS)
    assert source.count('"$host_python" scripts/reference_acceptance_state.py') == 14
    assert (
        'cairn_image_reference=$("$host_python" \\\n'
        "  scripts/reference_acceptance_state.py canonical-image-reference"
    ) in source
    assert source.count('"$host_python" -c') == 1
    assert not re.search(
        r"(?m)^\s*python scripts/reference_acceptance_state\.py", source
    )
    assert "$(python -c" not in source
    assert 'kubectl exec -i -n "$namespace" cairn-0 -c cairn -- python' in source
    assert "command: [python" in source


def _run_stdin_probe_with_fake_kubectl(
    tmp_path: Path, *, mode: str, payload: str
) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "kubectl"
    fake.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" >"$FAKE_KUBECTL_ARGS"
saw_i=false
for argument in "$@"; do
  [ "$argument" = -i ] && saw_i=true
done
[ "$saw_i" = true ] || exit 0
case "$FAKE_KUBECTL_MODE" in
  pass) cat ;;
  empty) cat >/dev/null ;;
  fail) cat >/dev/null; exit 17 ;;
  *) exit 18 ;;
esac
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    function = _function(_source(HARNESS), "run_python_stdin_probe")
    script = f"""
set -eu
namespace=acceptance-test
fail() {{ printf 'refused: %s\\n' "$*" >&2; exit 1; }}
run_python_stdin_probe() {{
{function}
}}
printf %s "$PAYLOAD" | run_python_stdin_probe wal-mode cairn-0 cairn argument
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_KUBECTL_ARGS": str(tmp_path / "argv"),
            "FAKE_KUBECTL_MODE": mode,
            "PAYLOAD": payload,
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_stdin_probe_streams_exact_bytes_through_interactive_kubectl_exec(
    tmp_path: Path,
) -> None:
    result = _run_stdin_probe_with_fake_kubectl(
        tmp_path, mode="pass", payload="known remote bytes"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "known remote bytes\n"
    assert (tmp_path / "argv").read_text(encoding="utf-8").splitlines() == [
        "exec",
        "-i",
        "-n",
        "acceptance-test",
        "cairn-0",
        "-c",
        "cairn",
        "--",
        "python",
        "-",
        "argument",
    ]


@pytest.mark.parametrize(
    ("mode", "message"),
    [("empty", "returned empty output"), ("fail", "failed")],
)
def test_stdin_probe_refuses_empty_output_and_remote_failure(
    tmp_path: Path, mode: str, message: str
) -> None:
    result = _run_stdin_probe_with_fake_kubectl(
        tmp_path, mode=mode, payload="known remote bytes"
    )

    assert result.returncode == 1
    assert message in result.stderr


def test_every_streamed_exec_is_interactive_and_producer_exec_is_not() -> None:
    source = _source(HARNESS)
    storage = _function(source, "prove_storage")
    recovery = _function(source, "prove_backup_restore")
    bootstrap = _function(source, "bootstrap_target")

    assert storage.count("run_python_stdin_probe") == 2
    assert recovery.count("run_python_stdin_probe") == 2
    assert source.count("kubectl exec -i") == 3
    assert "kubectl exec -i" not in bootstrap
    assert 'kubectl exec -n "$namespace" cairn-bootstrap' in bootstrap
    assert not re.search(r"kubectl exec(?! -i)[^\n]*<<", source)
    assert not re.search(r"kubectl exec(?! -i)[^\n]*\\\n[^\n]*<", source)


def _run_duplicate_probe_with_fake_kubectl(
    tmp_path: Path, *, output: str, returncode: int
) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "kubectl"
    fake.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" >"$FAKE_KUBECTL_ARGS"
printf '%s' "$FAKE_KUBECTL_OUTPUT"
exit "$FAKE_KUBECTL_STATUS"
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    function = _function(_source(HARNESS), "duplicate_process_observation")
    script = f"""
set -eu
namespace=acceptance-test
host_python={sys.executable}
duplicate_process_observation() {{
{function}
}}
duplicate_process_observation
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_KUBECTL_ARGS": str(tmp_path / "argv"),
            "FAKE_KUBECTL_OUTPUT": output,
            "FAKE_KUBECTL_STATUS": str(returncode),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_duplicate_process_probe_runs_a_bounded_second_cairn_server(
    tmp_path: Path,
) -> None:
    result = _run_duplicate_probe_with_fake_kubectl(
        tmp_path,
        output=(
            '{"event":"runtime_start_failed","failure_code":"already_locked",'
            '"instance_id":"cairn","timestamp":"2026-08-17T13:00:00Z"}\n'
            "command terminated with exit code 3\n"
        ),
        returncode=3,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "already_locked\n"
    assert (tmp_path / "argv").read_text(encoding="utf-8").splitlines() == [
        "exec",
        "-n",
        "acceptance-test",
        "cairn-0",
        "-c",
        "cairn",
        "--",
        "timeout",
        "--signal=TERM",
        "--kill-after=2s",
        "10s",
        "env",
        "GRAPHITI_TELEMETRY_ENABLED=false",
        "cairn",
        "serve",
        "--config",
        "/etc/cairn/config.yaml",
    ]


@pytest.mark.parametrize(
    ("output", "returncode"),
    [
        (
            '{"code":"catalogue_unavailable","status":"error"}\n'
            "command terminated with exit code 3\n",
            3,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n',
            3,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 3\n",
            0,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 3\n",
            124,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 3\n",
            137,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 3\n",
            3,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 4\n",
            3,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "command terminated with exit code 3\n"
            "unexpected diagnostic\n",
            3,
        ),
        (
            '{"event":"runtime_start_failed","failure_code":"already_locked"}\n'
            "[PostHog] analytics lane flush ran out of budget (1.0s granted) "
            "with 1 items pending.\n"
            "command terminated with exit code 3\n",
            3,
        ),
        ("not-json\ncommand terminated with exit code 3\n", 3),
        ("", 3),
    ],
)
def test_duplicate_process_probe_never_false_passes_other_outcomes(
    tmp_path: Path, output: str, returncode: int
) -> None:
    result = _run_duplicate_probe_with_fake_kubectl(
        tmp_path, output=output, returncode=returncode
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "other\n"


def test_storage_uses_lifecycle_lock_proof_not_ambiguous_verify_cli() -> None:
    storage = _function(_source(HARNESS), "prove_storage")
    assert "duplicate_process_observation" in storage
    assert "cairn verify" not in storage


def test_approved_target_pins_have_one_source() -> None:
    source = _source(HARNESS)
    lock = _source(IMAGES_LOCK)
    for key in (
        "TARGET_KUBERNETES_VERSION",
        "TARGET_CILIUM_VERSION",
        "TARGET_CILIUM_AGENT_IMAGE",
        "TARGET_CILIUM_OPERATOR_IMAGE",
        "TARGET_CILIUM_ENVOY_IMAGE",
        "KUBECTL_VERSION",
        "KUBECTL_LINUX_AMD64_SHA256",
        "FALKORDB_IMAGE",
        "EGRESS_PROXY_IMAGE",
    ):
        assert re.search(rf"^{key}=", lock, re.M), key
        assert f"lock_value {key}" in source
    assert "v1.35.0" not in source
    assert "v1.19.6" not in source


@pytest.mark.parametrize(
    ("requested", "canonical"),
    [
        ("cairn:task11", "docker.io/library/cairn:task11"),
        ("drystane/cairn:task11", "docker.io/drystane/cairn:task11"),
        ("ghcr.io/drystane/cairn:task11", "ghcr.io/drystane/cairn:task11"),
        ("localhost:5000/cairn:task11", "localhost:5000/cairn:task11"),
        (
            "cairn@sha256:" + "a" * 64,
            "docker.io/library/cairn@sha256:" + "a" * 64,
        ),
    ],
)
def test_docker_reference_is_canonical_for_containerd_and_cri(
    requested: str, canonical: str
) -> None:
    assert canonical_image_reference(requested) == canonical


def test_containerd_filter_syntax_cannot_enter_a_canonical_reference() -> None:
    with pytest.raises(HarnessRefusal, match="malformed"):
        canonical_image_reference("cairn:task11,name==foreign")


def test_imported_image_resolution_uses_the_exact_canonical_target_digest() -> None:
    digest = "sha256:" + "a" * 64
    qualified_digest = "sha256:" + "b" * 64
    images = [
        {
            "reference": "docker.io/library/cairn:task11",
            "target_digest": digest,
        },
        {
            "reference": "ghcr.io/drystane/cairn:task11",
            "target_digest": qualified_digest,
        },
    ]
    assert resolve_imported_image("cairn:task11", images) == {
        "reference": "docker.io/library/cairn:task11",
        "target_digest": digest,
    }
    assert resolve_imported_image("ghcr.io/drystane/cairn:task11", images) == {
        "reference": "ghcr.io/drystane/cairn:task11",
        "target_digest": qualified_digest,
    }
    digest_reference = "docker.io/library/cairn@" + digest
    assert resolve_imported_image(
        "cairn@" + digest,
        [{"reference": digest_reference, "target_digest": digest}],
    ) == {"reference": digest_reference, "target_digest": digest}


def test_imported_image_resolution_refuses_ambiguity_missing_or_wrong_digest() -> None:
    digest = "sha256:" + "a" * 64
    canonical = {
        "reference": "docker.io/library/cairn:task11",
        "target_digest": digest,
    }
    with pytest.raises(HarnessRefusal, match="ambiguous"):
        resolve_imported_image(
            "cairn:task11",
            [canonical, {"reference": "cairn:task11", "target_digest": digest}],
        )
    with pytest.raises(HarnessRefusal, match="canonical imported image is absent"):
        resolve_imported_image("cairn:task11", [])
    with pytest.raises(HarnessRefusal, match="target digest"):
        resolve_imported_image(
            "cairn:task11",
            [{"reference": canonical["reference"], "target_digest": ""}],
        )
    with pytest.raises(HarnessRefusal, match="does not match requested digest"):
        resolve_imported_image(
            "cairn@" + digest,
            [
                {
                    "reference": "docker.io/library/cairn@" + digest,
                    "target_digest": "sha256:" + "b" * 64,
                }
            ],
        )


@pytest.mark.parametrize(
    "image_id",
    [
        "docker.io/library/cairn@sha256:" + "c" * 64,
        "docker-pullable://docker.io/library/cairn@sha256:" + "c" * 64,
        "sha256:" + "c" * 64,
    ],
)
def test_runtime_image_id_yields_the_exact_oci_target_digest(image_id: str) -> None:
    assert runtime_image_digest(image_id) == "sha256:" + "c" * 64


def test_runtime_image_id_without_a_digest_is_refused() -> None:
    with pytest.raises(HarnessRefusal, match="runtime image ID has no OCI digest"):
        runtime_image_digest("docker.io/library/cairn:task11")


def test_harness_uses_canonical_image_for_containerd_kubernetes_and_report() -> None:
    source = _source(HARNESS)
    load = _function(source, "load_image")
    report = _function(source, "emit_report")
    assert "resolve-oci-provenance" in load
    assert "images inspect" not in load
    assert "--output json" not in load
    assert "runtime-config-digest" in source
    assert '"$cairn_config_digest"' in _function(source, "deploy_target")
    assert "image: ${IMAGE}" not in source
    assert "image: ${cairn_image_reference}" in source
    assert '--arg cairn_image "$cairn_image_reference"' in report


CTR_HEADER = "REF TYPE DIGEST SIZE PLATFORMS LABELS"
CTR_ALIGNED_HEADER = "REF    TYPE    DIGEST    SIZE    PLATFORMS    LABELS"


def _ctr_row(reference: str, digest: str) -> str:
    return (
        f"{reference} application/vnd.oci.image.manifest.v1+json {digest} "
        "87.5 MiB linux/amd64 io.cri-containerd.image=managed"
    )


def _run_containerd_resolver(
    tmp_path: Path,
    requested: str,
    *,
    references: dict[str, Any],
    table: dict[str, Any] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    canonical = canonical_image_reference(requested)
    responses = {
        json.dumps(
            ["--namespace", "k8s.io", "images", "list", "-q"],
            separators=(",", ":"),
        ): references,
    }
    if table is not None:
        responses[
            json.dumps(
                [
                    "--namespace",
                    "k8s.io",
                    "images",
                    "list",
                    f"name=={canonical}",
                ],
                separators=(",", ":"),
            )
        ] = table
    fixture = tmp_path / "ctr-fixture.json"
    fixture.write_text(json.dumps(responses), encoding="utf-8")
    log = tmp_path / "ctr-log.jsonl"
    executable = tmp_path / "ctr"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_CTR_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
with open(os.environ["FAKE_CTR_FIXTURE"], encoding="utf-8") as stream:
    responses = json.load(stream)
key = json.dumps(arguments, separators=(",", ":"))
response = responses.get(key)
if response is None:
    print("unexpected fake ctr command: " + key, file=sys.stderr)
    raise SystemExit(97)
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
raise SystemExit(response.get("returncode", 0))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(STATE_HELPER),
            "resolve-containerd-image",
            requested,
        ],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "FAKE_CTR_FIXTURE": str(fixture),
            "FAKE_CTR_LOG": str(log),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    commands = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, commands


@pytest.mark.parametrize(
    "requested",
    [
        "cairn:task11",
        "docker.io/library/cairn:task11",
        "cairn@sha256:" + "a" * 64,
    ],
)
def test_containerd_v23_table_binds_exact_reference_and_target_digest(
    tmp_path: Path, requested: str
) -> None:
    canonical = canonical_image_reference(requested)
    digest = canonical.rsplit("@", 1)[1] if "@" in canonical else "sha256:" + "a" * 64
    result, commands = _run_containerd_resolver(
        tmp_path,
        requested,
        references={"stdout": canonical + "\n"},
        table={"stdout": f"{CTR_ALIGNED_HEADER}\n{_ctr_row(canonical, digest)}\n"},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "reference": canonical,
        "target_digest": digest,
    }
    assert commands == [
        ["--namespace", "k8s.io", "images", "list", "-q"],
        [
            "--namespace",
            "k8s.io",
            "images",
            "list",
            f"name=={canonical}",
        ],
    ]


def test_containerd_resolution_refuses_bare_and_canonical_ambiguity(
    tmp_path: Path,
) -> None:
    result, commands = _run_containerd_resolver(
        tmp_path,
        "cairn:task11",
        references={"stdout": "cairn:task11\ndocker.io/library/cairn:task11\n"},
    )

    assert result.returncode == 2
    assert "ambiguous" in result.stderr
    assert commands == [["--namespace", "k8s.io", "images", "list", "-q"]]


def test_containerd_digest_reference_must_match_table_target(tmp_path: Path) -> None:
    requested = "cairn@sha256:" + "a" * 64
    canonical = canonical_image_reference(requested)
    result, commands = _run_containerd_resolver(
        tmp_path,
        requested,
        references={"stdout": canonical + "\n"},
        table={
            "stdout": f"{CTR_HEADER}\n"
            + _ctr_row(canonical, "sha256:" + "b" * 64)
            + "\n"
        },
    )

    assert result.returncode == 2
    assert "does not match requested digest" in result.stderr
    assert len(commands) == 2


@pytest.mark.parametrize(
    "output",
    [
        "NAME TYPE DIGEST SIZE PLATFORMS LABELS\n",
        CTR_HEADER + "\n",
        CTR_HEADER
        + "\n"
        + _ctr_row("docker.io/library/cairn:task11", "sha256:" + "a" * 64)
        + "\n"
        + _ctr_row("docker.io/library/cairn:task11", "sha256:" + "a" * 64)
        + "\n",
        CTR_HEADER
        + "\n"
        + _ctr_row("docker.io/library/foreign:task11", "sha256:" + "a" * 64)
        + "\n",
        CTR_HEADER
        + "\n"
        + _ctr_row("docker.io/library/cairn:task11", "not-a-digest")
        + "\n",
    ],
)
def test_containerd_resolution_refuses_malformed_human_table(
    tmp_path: Path, output: str
) -> None:
    canonical = "docker.io/library/cairn:task11"
    result, commands = _run_containerd_resolver(
        tmp_path,
        "cairn:task11",
        references={"stdout": canonical + "\n"},
        table={"stdout": output},
    )

    assert result.returncode == 2
    assert len(commands) == 2


@pytest.mark.parametrize(
    "table",
    [
        {
            "stdout": CTR_HEADER + "\n",
            "stderr": "synthetic containerd diagnostic\n",
        },
        {"stderr": "synthetic failure\n", "returncode": 1},
    ],
)
def test_containerd_resolution_refuses_stderr_or_nonzero(
    tmp_path: Path, table: dict[str, Any]
) -> None:
    canonical = "docker.io/library/cairn:task11"
    result, commands = _run_containerd_resolver(
        tmp_path,
        "cairn:task11",
        references={"stdout": canonical + "\n"},
        table=table,
    )

    assert result.returncode == 2
    assert len(commands) == 2


def test_preflight_is_first_and_refuses_the_wrong_target_or_dirty_layout() -> None:
    source = _source(HARNESS)
    assert _top_level_calls(source)[0] == "preflight"
    body = _function(source, "preflight")
    for required in (
        "REFERENCE_EXPECTED_HOST",
        "REFERENCE_EXPECTED_DISTRIBUTION",
        "REFERENCE_EXPECTED_SELINUX",
        "cairn-local",
        "WaitForFirstConsumer",
        "kubernetes.io/no-provisioner",
        "ReadWriteOncePod",
        "container_file_t",
        "/mnt/cairn-local/pv",
        "REFERENCE_PROVIDER_SECRET_FILE",
        "600 root:root",
    ):
        assert required in body
    assert "no Cairn namespace, workload or bound claim" in body
    assert "--force" not in source


def test_preflight_checks_the_current_cluster_against_every_approved_pin() -> None:
    body = _function(_source(HARNESS), "preflight")
    for identity in (
        "target_kubernetes_version",
        "target_cilium_version",
        "target_cilium_agent_image",
        "target_cilium_operator_image",
        "target_cilium_envoy_image",
        "kubectl_version",
        "kubectl_sha256",
    ):
        assert identity in body
    assert "KubeProxyReplacement" in body
    assert "False" in body
    assert "validate_mutable_inventory" in body
    ownership = _function(_source(HARNESS), "validate_mutable_inventory")
    assert "repository_revision" in ownership
    assert "validate-ownership" in ownership


def test_report_is_revision_bound_and_records_i14_identity() -> None:
    source = _source(HARNESS)
    report = _function(source, "emit_report")
    report_contract = report + _source(STATE_HELPER)
    for field in (
        "repository_revision",
        "distribution",
        "kubernetes_version",
        "container_runtime",
        "storage_class",
        "cni",
        "cilium_chart",
        "cilium_images",
        "rendered_manifest_digests",
        "workload_images",
        "cairn_image_digest",
        "cairn_target_digest",
        "cairn_platform_manifest_digest",
        "cairn_config_digest",
        "backup_barrier_ms",
        "backup_command_started_ns",
        "backup_mutation_completed_ns",
        "backup_child_pid",
        "backup_child_start_ticks",
        "backup_child_alive_observed_ns",
        "backup_command_finished_ns",
        "backup_overlap_scope",
        "checks",
        "gaps",
        "task_11_observations",
        "task_11a_status",
        "task_11a_observations",
    ):
        assert field in report_contract
    assert "git rev-parse HEAD" in source
    assert "sha256sum" in source
    assert "imageID" in source
    assert "reference_acceptance_state.py build-report" in report


def test_i95_gaps_are_verbatim() -> None:
    source = _source(HARNESS)
    expected = (
        "no cross-node policy on one node, and OpenShift unclaimed until its "
        "suite passes on a real cluster."
    )
    assert expected in source


def test_provider_key_is_created_server_side_without_render_or_file_output() -> None:
    source = _source(HARNESS)
    body = _function(source, "create_live_provider_secret")
    assert '. "$REFERENCE_PROVIDER_SECRET_FILE"' in body
    assert "set +x" in body
    assert "kubectl create secret generic" in body
    assert "--from-env-file=/dev/stdin" in body
    assert "--from-literal=openai-api-key" not in body
    assert "--dry-run=client -o json" in body
    assert '"cairn.example.invalid/acceptance-run":$run' in body
    assert "kubectl apply -f -" in body
    outside = source.replace(body, "")
    assert (
        ': "${REFERENCE_PROVIDER_SECRET_FILE:?REFERENCE_PROVIDER_SECRET_FILE is required}"'
        in outside
    )
    assert "stat -c '%a %U:%G' \"$REFERENCE_PROVIDER_SECRET_FILE\"" in outside
    assert 'cat "$REFERENCE_PROVIDER_SECRET_FILE"' not in source
    assert "printenv OPENAI_API_KEY" not in source


def test_admission_and_storage_checks_use_the_named_boundaries() -> None:
    source = _source(HARNESS)
    calls = _top_level_calls(source)
    assert calls.index("prove_enforcement_active") < calls.index(
        "prove_restricted_admission"
    )
    enforcement = _function(source, "prove_enforcement_active")
    assert "enforcement-default-deny" in enforcement
    assert "enforcement-permitted-control" in enforcement
    admission = _function(source, "prove_restricted_admission")
    assert "pod-security.kubernetes.io/enforce=restricted" in admission
    assert "--dry-run=server" in admission
    storage = _function(source, "prove_storage")
    for check in (
        "wal-mode",
        "rwop-scheduler-refusal",
        "duplicate-process-refusal",
        "forced-restart-recovery",
        "pragma-integrity-check",
    ):
        assert check in storage


def test_single_node_matrix_pairs_each_refusal_with_a_control() -> None:
    body = _function(_source(HARNESS), "prove_connectivity_matrix")
    for check in (
        "client-to-cairn-allowed",
        "foreign-client-to-cairn-refused",
        "cairn-to-own-index-allowed",
        "foreign-pod-to-index-refused",
        "cairn-to-gateway-allowed",
        "cairn-direct-provider-refused",
        "gateway-live-provider-allowed",
        "gateway-other-fqdn-refused",
        "live-provider-round-trip",
    ):
        assert check in body
    assert "no-cross-node-policy" in body


def test_runtime_checks_record_observations_not_predeclared_answers() -> None:
    source = _source(HARNESS)
    self_confirming = re.findall(
        r"^\s*record\s+\S+\s+\S+\s+(\S+)\s+\1\s*$", source, re.M
    )
    assert self_confirming == []


def test_recovery_order_preserves_rollback_and_moves_traffic_last() -> None:
    source = _source(HARNESS)
    body = _function(source, "prove_backup_restore")
    ordered = (
        "backup-under-live-traffic",
        "original-quiesced",
        "replacement-pvc-fresh",
        "restore-command",
        "restored-catalogue-integrity",
        "rebuild-index",
        "original-pvc-retained",
        "traffic-moved-after-verification",
    )
    offsets = [body.index(name) for name in ordered]
    assert offsets == sorted(offsets)
    assert "claimName: cairn-restored-data" in source
    assert "persistentVolumeReclaimPolicy" not in body
    assert "restored-attic-payload" in body
    assert "restored-index-episode" in body


def test_reruns_clean_only_ephemeral_probe_resources() -> None:
    body = _function(_source(HARNESS), "prepare_run")
    assert "--ignore-not-found" in body
    assert '"$enforcement_namespace"' in body
    assert '"$shapes_namespace"' in body
    assert '"$foreign_namespace"' in body
    assert "cairn.example.invalid/acceptance=task11" in body
    assert "acceptance owner label" in body
    for forbidden in ("delete pv", "patch pv", "rm -rf", "mkfs", "wipefs"):
        assert forbidden not in body


def _object(
    kind: str, name: str, namespace: str | None, run: str, revision: str
) -> dict[str, Any]:
    metadata: dict[str, object] = {
        "name": name,
        "labels": {
            "cairn.example.invalid/acceptance-owner": "task11",
            "cairn.example.invalid/acceptance-run": run,
            "cairn.example.invalid/acceptance-revision": revision,
        },
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return {"kind": kind, "metadata": metadata}


def test_inventory_ownership_covers_secrets_pvcs_gateway_and_probe_namespaces() -> None:
    revision = "a" * 40
    run = "run-17"
    inventory = [
        _object("Namespace", "cairn-target-acceptance", None, run, revision),
        _object(
            "Secret", "cairn-credentials", "cairn-target-acceptance", run, revision
        ),
        _object(
            "PersistentVolumeClaim",
            "data-cairn-0",
            "cairn-target-acceptance",
            run,
            revision,
        ),
        _object("Namespace", "cairn-egress", None, run, revision),
        _object("Service", "cairn-egress-gateway", "cairn-egress", run, revision),
        _object("NetworkPolicy", "gateway-ingress", "cairn-egress", run, revision),
        _object("ServiceAccount", "cairn", "cairn-target-acceptance", run, revision),
        _object("Namespace", "cairn-acceptance-foreign", None, run, revision),
    ]
    validate_inventory_ownership(inventory, run, revision)
    inventory[1]["metadata"]["labels"].pop("cairn.example.invalid/acceptance-run")
    with pytest.raises(HarnessRefusal, match="Secret/.*/cairn-credentials"):
        validate_inventory_ownership(inventory, run, revision)
    inventory[1]["metadata"]["labels"]["cairn.example.invalid/acceptance-run"] = run
    inventory[6]["metadata"]["labels"].pop("cairn.example.invalid/acceptance-revision")
    with pytest.raises(HarnessRefusal, match="ServiceAccount/.*/cairn"):
        validate_inventory_ownership(inventory, run, revision)


def test_every_exact_rendered_kind_is_owned_and_service_account_is_stamped() -> None:
    rendered = _source(REPOSITORY / "deploy" / "kustomize" / "rendered" / "kind.yaml")
    gateway = _source(
        REPOSITORY / "deploy" / "kustomize" / "rendered" / "egress-gateway.yaml"
    )
    kinds = set(re.findall(r"^kind: (\S+)$", rendered + gateway, re.M))
    assert kinds == {
        "ConfigMap",
        "Deployment",
        "Namespace",
        "NetworkPolicy",
        "Service",
        "ServiceAccount",
        "StatefulSet",
    }
    assert re.search(
        r"^kind: ServiceAccount\nmetadata:\n(?:  .+\n)+  name: cairn$", rendered, re.M
    )
    objects = [
        {
            "kind": kind,
            "metadata": {"name": "cairn" if kind == "ServiceAccount" else kind.lower()},
        }
        for kind in sorted(kinds)
    ]
    targets = stamp_targets_for_rendered_objects(objects)
    assert ("serviceaccount", "cairn") in targets
    assert len(targets) == len(objects) - 1  # Namespace is stamped separately.
    resources = (
        subprocess.run(
            [sys.executable, str(STATE_HELPER), "mutable-resources"],
            text=True,
            capture_output=True,
            check=True,
        )
        .stdout.strip()
        .split(",")
    )
    assert set(resource for resource, _name in targets) <= set(resources)
    assert {"replicaset", "controllerrevision", "endpoints", "endpointslice"} <= set(
        resources
    )
    source = _source(HARNESS)
    assert 'kubectl get "$mutable_namespaced_resources"' in source
    assert "${mutable_namespaced_resources//,/ }" in source


def test_preflight_and_completed_runs_preserve_immutable_report(tmp_path: Path) -> None:
    report = tmp_path / "evidence.json"
    report.write_text('{"status":"passed","checks":["real"]}\n', encoding="utf-8")
    before = report.read_bytes()
    assert (
        invocation_action("preflight", report_exists=True, durable_phase="complete")
        == "preflight-only"
    )
    with pytest.raises(HarnessRefusal, match="immutable evidence already exists"):
        invocation_action("run", report_exists=True, durable_phase="complete")
    assert report.read_bytes() == before
    with pytest.raises(FileExistsError):
        write_report_exclusive(report, {"status": "passed", "checks": []})
    assert report.read_bytes() == before


def test_report_population_uses_actual_preflight_and_revision_bound_image() -> None:
    revision = "b" * 40
    cilium_images = [
        {
            "workload_kind": "DaemonSet",
            "workload_name": "cilium",
            "workload_uid": "uid-cilium",
            "namespace": "kube-system",
            "container_name": "cilium-agent",
            "spec_image": "quay.io/cilium/cilium:v1@sha256:" + "1" * 64,
            "runtime_image_id": "quay.io/cilium/cilium@sha256:" + "1" * 64,
            "runtime_image_digest": "sha256:" + "1" * 64,
            "pod_names": ["cilium-node"],
            "pod_uids": ["uid-cilium-node"],
            "desired_pods": 1,
            "selector_labels": {"app": "cilium"},
            "controller_chain": [
                {"kind": "DaemonSet", "name": "cilium", "uid": "uid-cilium"},
                {"kind": "Pod", "name": "cilium-node", "uid": "uid-cilium-node"},
            ],
        },
        {
            "workload_kind": "Deployment",
            "workload_name": "cilium-operator",
            "workload_uid": "uid-cilium-operator",
            "namespace": "kube-system",
            "container_name": "cilium-operator",
            "spec_image": "quay.io/cilium/operator:v1@sha256:" + "2" * 64,
            "runtime_image_id": "quay.io/cilium/operator@sha256:" + "2" * 64,
            "runtime_image_digest": "sha256:" + "2" * 64,
            "pod_names": ["cilium-operator-a"],
            "pod_uids": ["uid-cilium-operator-a"],
            "desired_pods": 1,
            "selector_labels": {"app": "cilium-operator"},
            "controller_chain": [
                {
                    "kind": "Deployment",
                    "name": "cilium-operator",
                    "uid": "uid-cilium-operator",
                },
                {
                    "kind": "ReplicaSet",
                    "name": "cilium-operator-rs",
                    "uid": "uid-cilium-operator-rs",
                },
                {
                    "kind": "Pod",
                    "name": "cilium-operator-a",
                    "uid": "uid-cilium-operator-a",
                },
            ],
        },
        {
            "workload_kind": "DaemonSet",
            "workload_name": "cilium-envoy",
            "workload_uid": "uid-cilium-envoy",
            "namespace": "kube-system",
            "container_name": "cilium-envoy",
            "spec_image": "quay.io/cilium/envoy:v1@sha256:" + "3" * 64,
            "runtime_image_id": "quay.io/cilium/envoy@sha256:" + "3" * 64,
            "runtime_image_digest": "sha256:" + "3" * 64,
            "pod_names": ["cilium-envoy-node"],
            "pod_uids": ["uid-cilium-envoy-node"],
            "desired_pods": 1,
            "selector_labels": {"app": "cilium-envoy"},
            "controller_chain": [
                {
                    "kind": "DaemonSet",
                    "name": "cilium-envoy",
                    "uid": "uid-cilium-envoy",
                },
                {
                    "kind": "Pod",
                    "name": "cilium-envoy-node",
                    "uid": "uid-cilium-envoy-node",
                },
            ],
        },
    ]

    def make_report(
        image_revision: str, images: list[dict[str, Any]] = cilium_images
    ) -> dict[str, Any]:
        return build_report(
            status="passed",
            stage="complete",
            run_id="run-17",
            repository_revision=revision,
            image_revision=image_revision,
            image_digest="sha256:" + "c" * 64,
            cairn_target_digest="sha256:" + "a" * 64,
            cairn_platform_manifest_digest="sha256:" + "b" * 64,
            cairn_config_digest="sha256:" + "c" * 64,
            preflight={
                "distribution": "Fedora Linux 44",
                "kubernetes_version": "v1.35.0",
                "container_runtime": "containerd://2.3.3",
                "storage_class": "cairn-local",
                "cni": "Cilium",
                "cilium_chart": "cilium-1.19.6",
            },
            rendered_manifest_digests=[{"file": "target.yaml", "sha256": "d" * 64}],
            workload_images=[],
            cilium_images=images,
            checks=[{"name": "real", "outcome": "passed"}],
            backup_barrier_ms=12,
            backup_command_started_ns=1_000,
            backup_mutation_completed_ns=1_500,
            backup_child_pid=41,
            backup_child_start_ticks="9876",
            backup_child_alive_observed_ns=1_700,
            backup_command_finished_ns=2_000,
        )

    report = make_report(revision)
    assert report["distribution"] == "Fedora Linux 44"
    assert report["kubernetes_version"] == "v1.35.0"
    assert report["image_revision"] == revision
    assert report["cairn_target_digest"] == "sha256:" + "a" * 64
    assert report["cairn_platform_manifest_digest"] == "sha256:" + "b" * 64
    assert report["cairn_config_digest"] == "sha256:" + "c" * 64
    assert report["backup_mutation_completed_ns"] == 1_500
    assert report["backup_child_pid"] == 41
    assert report["backup_child_start_ticks"] == "9876"
    assert report["backup_child_alive_observed_ns"] == 1_700
    assert (
        "a mutation completed before a later observation proved the exact "
        "server-side cairn backup child was still alive"
        in report["backup_overlap_scope"]
    )
    assert (
        "does not prove overlap with the narrower SQLite barrier"
        in report["backup_overlap_scope"]
    )
    assert report["task_11_observations"] == [
        "The f1418ad live attempt observed a PostHog upload through the "
        "gateway refused with HTTP 403; this is fail-closed egress evidence, "
        "not a provider-success claim."
    ]
    assert all("PostHog" not in item for item in report["task_11a_observations"])
    with pytest.raises(HarnessRefusal, match="image revision"):
        make_report("e" * 40)
    with pytest.raises(HarnessRefusal, match="Cilium image identities"):
        make_report(revision, [])


def test_service_selector_is_replaced_and_endpoint_must_be_stably_singular() -> None:
    patch = service_selector_patch("cairn-target-acceptance")
    assert patch == [
        {
            "op": "replace",
            "path": "/spec/selector",
            "value": {
                "app.kubernetes.io/name": "cairn",
                "app.kubernetes.io/instance": "cairn-target-acceptance",
                "cairn.example.invalid/recovery": "restored",
            },
        }
    ]
    assert endpoint_candidate_converged([["10.0.0.4"]] * 3, "10.0.0.4", 3)
    assert not endpoint_candidate_converged(
        [["10.0.0.4"], ["10.0.0.4", "10.0.0.5"], ["10.0.0.4"]], "10.0.0.4", 3
    )


def test_rwop_refusal_is_bound_to_current_uid_and_scheduling_condition() -> None:
    pod: dict[str, Any] = {
        "metadata": {"uid": "new-uid"},
        "status": {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "ReadWriteOncePod access mode already in-use",
                }
            ],
        },
    }
    current = [
        {
            "involvedObject": {"uid": "new-uid"},
            "message": "ReadWriteOncePod access mode already in-use",
        }
    ]
    stale = [
        {
            "involvedObject": {"uid": "old-uid"},
            "message": "ReadWriteOncePod access mode already in-use",
        }
    ]
    assert rwop_refusal_is_current(pod, current)
    assert not rwop_refusal_is_current(pod, stale)
    pod["status"]["phase"] = "Running"
    assert not rwop_refusal_is_current(pod, current)


def test_storage_inventory_and_claim_binding_are_exact() -> None:
    mounts = [
        {
            "source": f"/dev/sd{letter}",
            "target": f"/mnt/cairn-local/pv{number}",
            "fstype": "xfs",
            "size_bytes": 20 * 1024**3,
        }
        for number, letter in enumerate("bcde", start=1)
    ]
    pvs: list[dict[str, Any]] = []
    for number in range(1, 5):
        pvs.append(
            {
                "metadata": {"name": f"cairn-local-pv{number}"},
                "spec": {
                    "storageClassName": "cairn-local",
                    "volumeMode": "Filesystem",
                    "accessModes": ["ReadWriteOncePod"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "capacity": {"storage": "20Gi"},
                    "local": {"path": f"/mnt/cairn-local/pv{number}"},
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": ["reference"],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
                "status": {"phase": "Available"},
            }
        )
    validate_storage_inventory(mounts, pvs)
    claims = [
        {
            "metadata": {"name": f"claim-{n}"},
            "spec": {"volumeName": f"cairn-local-pv{n}"},
            "status": {"phase": "Bound"},
        }
        for n in range(1, 5)
    ]
    bindings = claim_bindings(claims, pvs)
    assert bindings["claim-1"] == {
        "pv": "cairn-local-pv1",
        "path": "/mnt/cairn-local/pv1",
        "reclaim_policy": "Retain",
    }
    pvs[3]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
    with pytest.raises(HarnessRefusal, match="cairn-local-pv4"):
        validate_storage_inventory(mounts, pvs)


def test_recovery_state_machine_refuses_unsafe_automatic_resume() -> None:
    assert recovery_transition("prepared", "bootstrap-started") == "bootstrap-started"
    assert (
        recovery_transition("bootstrap-started", "bootstrap-complete")
        == "bootstrap-complete"
    )
    assert (
        recovery_transition("bootstrap-complete", "recovery-started")
        == "recovery-started"
    )
    with pytest.raises(HarnessRefusal, match="manual recovery route"):
        recovery_transition("bootstrap-started", "prepared")
    with pytest.raises(HarnessRefusal, match="manual recovery route"):
        invocation_action("run", report_exists=False, durable_phase="recovery-started")


def test_backup_overlap_requires_a_mutation_during_the_observed_live_process() -> None:
    assert (
        backup_command_overlap(
            backup_started_ns=1_000,
            backup_finished_ns=2_000,
            wrapper_observed_running=True,
            exact_child_observed_alive=True,
            child_alive_observed_ns=1_700,
            mutation_completed_ns=[900, 1_500, 2_100],
        )
        == 1_500
    )
    with pytest.raises(HarnessRefusal, match="no mutation completed"):
        backup_command_overlap(
            backup_started_ns=1_000,
            backup_finished_ns=2_000,
            wrapper_observed_running=True,
            exact_child_observed_alive=True,
            child_alive_observed_ns=1_700,
            mutation_completed_ns=[900, 2_100],
        )
    with pytest.raises(HarnessRefusal, match="wrapper was not observed running"):
        backup_command_overlap(
            backup_started_ns=1_000,
            backup_finished_ns=2_000,
            wrapper_observed_running=False,
            exact_child_observed_alive=True,
            child_alive_observed_ns=1_700,
            mutation_completed_ns=[1_500],
        )
    with pytest.raises(HarnessRefusal, match="exact backup child was not alive"):
        backup_command_overlap(
            backup_started_ns=1_000,
            backup_finished_ns=2_000,
            wrapper_observed_running=True,
            exact_child_observed_alive=False,
            child_alive_observed_ns=None,
            mutation_completed_ns=[1_500],
        )
    with pytest.raises(HarnessRefusal, match="no mutation completed"):
        backup_command_overlap(
            backup_started_ns=1_000,
            backup_finished_ns=2_000,
            wrapper_observed_running=True,
            exact_child_observed_alive=True,
            child_alive_observed_ns=1_400,
            mutation_completed_ns=[1_500],
        )
    recovery = _function(_source(HARNESS), "prove_backup_restore")
    assert 'kill -0 "$backup_pid"' in recovery
    assert "tests/acceptance/kind/backup_child.py" in recovery
    assert '"$backup_marker" "$run_id" "$repository_revision"' in recovery
    assert "exact_child_observed_alive" in recovery
    assert "backup_child_alive_observed_ns" in recovery
    assert "validate-backup-overlap" in recovery
    assert "mutation-completed-while-exact-server-side-backup-child-alive" in recovery
    assert "backup_command_started_ns" in recovery
    assert "backup_command_finished_ns" in recovery


def test_credential_flow_never_places_a_value_in_argv_log_or_temporary_file() -> None:
    source = _source(HARNESS)
    bootstrap = _function(source, "bootstrap_target")
    assert "kubectl logs" not in bootstrap
    assert "--from-literal=token" not in bootstrap
    assert "token=$(" not in bootstrap
    assert "--from-file=token=/dev/stdin" in bootstrap
    assert "extract-bootstrap-token" in bootstrap
    assert "bootstrap-token" not in " ".join(
        re.findall(r'>\s*"?\$temporary/([^" ]+)', bootstrap)
    )
    synthetic_secret = "cairn1.synthetic-not-a-real-credential"
    argv = bootstrap_secret_command("cairn-target-acceptance")
    assert synthetic_secret not in argv
    assert "--from-file=token=/dev/stdin" in argv
    extracted = subprocess.run(
        [sys.executable, str(STATE_HELPER), "extract-bootstrap-token"],
        input=json.dumps({"operation": "bootstrap", "token": synthetic_secret}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert extracted.stdout == synthetic_secret
    assert extracted.stderr == ""


def test_behaviour_helper_cli_rejects_unowned_inventory() -> None:
    result = subprocess.run(
        [sys.executable, str(STATE_HELPER), "validate-ownership", "run-17", "f" * 40],
        input='[{"kind":"Secret","metadata":{"name":"foreign","namespace":"cairn-egress","labels":{}}}]',
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Secret/cairn-egress/foreign" in result.stderr


def test_candidate_contract_includes_all_health_probes_and_provider_delivery() -> None:
    source = _source(HARNESS)
    recovery = _function(source, "prove_backup_restore")
    assert "startupProbe:" in recovery
    assert "readinessProbe:" in recovery
    assert "livenessProbe:" in recovery
    for check in ("candidate-startup", "candidate-readiness", "candidate-liveness"):
        assert check in recovery
    connectivity = _function(source, "prove_connectivity_matrix")
    assert "foreign-to-gateway-refused" in connectivity
    assert "provider-index-delivery" in connectivity
    assert connectivity.count("falkordb_completed_marker_present") == 1
    assert recovery.count("falkordb_completed_marker_present") == 1
    assert "/opt/acceptance/client.py restore-fixture" in connectivity
    assert "/opt/acceptance/client.py ingest" not in connectivity
    assert "pre-backup-attic-payload" in connectivity
    assert '/var/lib/cairn/attic.sqlite3 "attic evidence ${restore_marker}"' in (
        connectivity
    )
    provider_probe = _function(source, "falkordb_completed_marker_present")
    assert "GRAPH.RO_QUERY" in provider_probe
    assert "falkordb_read_only_query" in provider_probe
    assert "attic.py" not in provider_probe
    assert "/v1/retrieve" not in provider_probe
    provider_transport = _function(source, "falkordb_read_only_query")
    assert '"$pod" -c falkordb' in provider_transport
    assert "redis-cli -e --raw" in provider_transport
    assert "backup-bundle-members" in recovery
    assert '["catalogue.sqlite3", "attic.sqlite3"]' in recovery


def _run_falkordb_query_with_fake_kubectl(
    tmp_path: Path,
    *,
    config: bytes,
    redis_output: str,
    redis_status: int = 0,
    redis_server_error: bool = False,
) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "cairn.conf"
    config_path.write_bytes(config)
    fake_kubectl = tmp_path / "kubectl"
    fake_kubectl.write_text(
        """#!/bin/bash
printf '%s\n' "$@" >"$FAKE_KUBECTL_ARGS"
while [[ $# -gt 0 && $1 != -- ]]; do shift; done
[[ $# -gt 0 ]] || exit 64
shift
args=("$@")
for index in "${!args[@]}"; do
  [[ ${args[$index]} != /etc/falkordb/cairn.conf ]] || \
    args[$index]=$FAKE_FALKORDB_CONFIG
done
exec "${args[@]}"
""",
        encoding="utf-8",
    )
    fake_kubectl.chmod(0o755)
    fake_redis = tmp_path / "redis-cli"
    fake_redis.write_text(
        """#!/bin/bash
printf '%s\n' "$@" >"$FAKE_REDIS_ARGS"
dd of="$FAKE_REDIS_STDIN" status=none
printf '%s' "$FAKE_REDIS_OUTPUT"
if [[ $FAKE_REDIS_SERVER_ERROR == true && " $* " == *" -e "* ]]; then
  exit 1
fi
exit "$FAKE_REDIS_STATUS"
""",
        encoding="utf-8",
    )
    fake_redis.chmod(0o755)
    function = _function(_source(HARNESS), "falkordb_read_only_query")
    script = f"""
set -euo pipefail
namespace=acceptance-test
falkordb_read_only_query() {{
{function}
}}
falkordb_read_only_query falkordb-0 \
  'GRAPH.RO_QUERY digest "MATCH (e:Episodic) RETURN e.content"'
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_FALKORDB_CONFIG": str(config_path),
            "FAKE_KUBECTL_ARGS": str(tmp_path / "argv"),
            "FAKE_REDIS_OUTPUT": redis_output,
            "FAKE_REDIS_STATUS": str(redis_status),
            "FAKE_REDIS_SERVER_ERROR": str(redis_server_error).lower(),
            "FAKE_REDIS_ARGS": str(tmp_path / "redis-argv"),
            "FAKE_REDIS_STDIN": str(tmp_path / "redis-stdin"),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_falkordb_query_frames_auth_when_secret_file_lacks_final_newline(
    tmp_path: Path,
) -> None:
    secret = "synthetic-index-secret"
    query = 'GRAPH.RO_QUERY digest "MATCH (e:Episodic) RETURN e.content"'
    result = _run_falkordb_query_with_fake_kubectl(
        tmp_path,
        config=f"requirepass {secret}".encode(),
        redis_output="OK\nprovider marker\n",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "redis-stdin").read_bytes() == (
        f"AUTH {secret}\n{query}\n".encode()
    )
    argv = (tmp_path / "argv").read_text(encoding="utf-8")
    assert secret not in argv
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert (tmp_path / "redis-argv").read_text(encoding="utf-8").splitlines() == [
        "-e",
        "--raw",
    ]


@pytest.mark.parametrize(
    ("config", "redis_output", "redis_status", "redis_server_error"),
    [
        (b"save 60 1", "", 0, False),
        (
            b"requirepass synthetic-index-secret\nrequirepass second-secret",
            "",
            0,
            False,
        ),
        (b"requirepass ", "", 0, False),
        (b"requirepass synthetic-index-secret trailing", "", 0, False),
        (
            b"requirepass synthetic-index-secret",
            "WRONGPASS invalid username-password pair\n",
            0,
            True,
        ),
        (b"requirepass synthetic-index-secret", "transport failed\n", 70, False),
    ],
)
def test_falkordb_query_refuses_missing_auth_and_redis_failure_without_leaking(
    tmp_path: Path,
    config: bytes,
    redis_output: str,
    redis_status: int,
    redis_server_error: bool,
) -> None:
    result = _run_falkordb_query_with_fake_kubectl(
        tmp_path,
        config=config,
        redis_output=redis_output,
        redis_status=redis_status,
        redis_server_error=redis_server_error,
    )

    assert result.returncode != 0
    assert "synthetic-index-secret" not in result.stdout
    assert "synthetic-index-secret" not in result.stderr
    assert "synthetic-index-secret" not in (tmp_path / "argv").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("output", "expected_status"),
    [
        ("provider marker\n", 0),
        ("prefix provider marker\n", 1),
        ("provider marker suffix\n", 1),
    ],
)
def test_completed_provider_marker_requires_one_exact_returned_line(
    output: str, expected_status: int
) -> None:
    matcher = _function(_source(HARNESS), "full_stream_has_exact_line")
    function = _function(_source(HARNESS), "falkordb_completed_marker_present")
    script = f"""
set -euo pipefail
falkordb_read_only_query() {{ printf '%s' "$FAKE_FALKORDB_OUTPUT"; }}
full_stream_has_exact_line() {{
{matcher}
}}
falkordb_completed_marker_present() {{
{function}
}}
falkordb_completed_marker_present falkordb-0 digest 'provider marker'
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPOSITORY,
        env={**os.environ, "FAKE_FALKORDB_OUTPUT": output},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == expected_status


def test_completed_provider_marker_consumes_large_trailing_result_before_success(
    tmp_path: Path,
) -> None:
    matcher = _function(_source(HARNESS), "full_stream_has_exact_line")
    function = _function(_source(HARNESS), "falkordb_completed_marker_present")
    producer_done = tmp_path / "producer-done"
    script = f"""
set -euo pipefail
falkordb_read_only_query() {{
  printf 'provider marker\n'
  for value in $(seq 1 200000); do printf 'trailing-%s\n' "$value"; done
  printf done >"$FAKE_PRODUCER_DONE"
}}
full_stream_has_exact_line() {{
{matcher}
}}
falkordb_completed_marker_present() {{
{function}
}}
falkordb_completed_marker_present falkordb-0 digest 'provider marker'
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPOSITORY,
        env={**os.environ, "FAKE_PRODUCER_DONE": str(producer_done)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert producer_done.read_text(encoding="utf-8") == "done"
