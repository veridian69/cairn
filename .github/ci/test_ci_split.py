"""Offline CI split contracts: no policy or real namespace operations.

The inventory comes from the independent 930ff7e design review. Adding a
kernel-dependent test requires reviewing its classification, not a broad skip.
Run with the locked project interpreter; this file is outside normal pytest.
"""

import ast
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HOST_FUNCTIONS = {
    "test_host_workflow_sandbox.py": [
        "test_actual_allowed_read_immutable_paths_and_private_scratch",
        "test_nested_cli_excludes_oauth_server_state_and_host_runtime",
        "test_maximum_checkpoint_content_and_json_escaping_reach_cli",
        "test_cli_stdin_exact_limit_and_one_byte_over",
        "test_renderer_accepted_packet_reaches_stdout_without_stderr_using_its_budget",
        "test_cli_stream_exact_limits_and_one_byte_over",
        "test_whole_host_event_limits_are_separate_and_finite",
        "test_output_capped_without_payload_in_error",
        "test_stdin_bounds_and_metacharacters_are_data",
        "test_closed_stdin_is_not_successful_delivery",
        "test_nonzero_exit_and_inert_capture",
        "test_cleanup_failure_does_not_disclose_argv",
        "test_pid_namespace_cleanup_with_new_session",
    ],
    "test_host_workflow_stdio.py": [
        "test_duplex_before_eof_then_complete_drain",
        "test_stream_uses_only_fixed_mounts_environment_and_entry",
        "test_cumulative_exact_limits_and_one_byte_over",
        "test_failures_never_become_orderly_eof",
        "test_slow_consumer_backpressures_input_and_recovers_without_loss",
        "test_initially_nonblocking_pipes_remain_nonblocking",
        "test_cancel_reaps_nested_namespace_but_not_independent_sibling",
        "test_cleanup_failure_is_fixed_and_restores_borrowed_flags",
        "test_actual_sdk_skill_and_nested_wheel_help_over_streaming_pipes",
        "test_partial_frame_eof_preserves_bridge_protocol_refusal",
    ],
    "test_host_workflow_native.py": [
        "test_admission_is_single_use_even_after_bridge_crash",
        "test_concurrent_launcher_cannot_start_second_bridge",
        "test_eof_does_not_certify_incomplete_protocol",
        "test_complete_exchange_still_requires_terminal_and_receipts",
        "test_real_scoped_checkpoint_receipt_and_recovery",
        "test_active_nested_cli_cleanup_leaves_independent_sibling",
        "test_bad_output_cannot_become_transport_success",
        "test_cumulative_input_limit_is_not_reset_by_frames",
        "test_output_cumulative_bound_preserves_server_notifications",
        "test_process_crash_keeps_admission_consumed",
        "test_stage_changed_during_session_fails_after_drain",
        "test_terminal_text_bound_and_no_failed_finish_retry",
        "test_slow_pipe_peer_backpressure_has_no_loss_or_budget_restart",
        "test_simultaneous_admission_has_one_winner",
        "test_stderr_is_bounded_discarded_and_never_replayed",
    ],
    "test_host_workflow_runtime.py": [
        "test_actual_wheel_cli_inside_nested_sandbox",
        "test_cli_runtime_does_not_use_host_python",
    ],
    "test_host_workflow_bridge.py": [
        "test_active_sdk_cancel_jailed_main_preserves_session",
        "test_actual_jailed_sdk_bridge_and_nested_wheel_cli",
    ],
    "test_host_proposals.py": [
        "test_built_skill_sdk_lifecycle_and_real_lost_publication_replay"
    ],
    "test_host_disagreement.py": [
        "test_wheel_sdk_suggestion_confirmation_refusal_and_lost_response_replay"
    ],
}


def workflow(name):
    # BaseLoader preserves GitHub's 'on' key instead of YAML 1.1 booleans.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


class SplitTests(unittest.TestCase):
    def test_every_hosted_job_requires_a_public_repository(self):
        paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
        self.assertTrue(paths)
        for path in paths:
            for name, job in workflow(path.name)["jobs"].items():
                with self.subTest(workflow=path.name, job=name):
                    # A job-level condition is evaluated before runner allocation.
                    condition = job.get("if", "")
                    self.assertTrue(
                        condition.startswith(
                            "${{ github.event.repository.private == false && "
                        ),
                        condition,
                    )

    def test_mac_acceptance_is_manual_bounded_and_secret_free(self):
        doc = workflow("macos-foreground.yml")
        self.assertEqual(set(doc["on"]), {"workflow_dispatch"})
        self.assertEqual(doc["permissions"], {"contents": "read"})
        self.assertEqual(set(doc["jobs"]), {"native"})
        job = doc["jobs"]["native"]
        self.assertEqual(
            job["if"],
            "${{ github.event.repository.private == false && github.event_name == 'workflow_dispatch' }}",
        )
        self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(job["timeout-minutes"], "${{ matrix.timeout }}")
        self.assertEqual(
            job["strategy"]["matrix"]["include"],
            [
                {
                    "runner": "macos-26-intel",
                    "architecture": "x86_64",
                    "timeout": "10",
                    "acceptance_deadline": "300",
                },
                {
                    "runner": "macos-26",
                    "architecture": "arm64",
                    "timeout": "4",
                    "acceptance_deadline": "180",
                },
            ],
        )
        text = (ROOT / ".github/workflows/macos-foreground.yml").read_text()
        self.assertNotIn("secrets.", text)
        self.assertNotIn("secrets[", text)
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")
            self.assertNotIn("continue-on-error", step)
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("scripts/test_macos_native.py", commands)
        self.assertIn('test -z "${OPENAI_API_KEY:-}"', commands)
        self.assertIn('test -z "${ANTHROPIC_API_KEY:-}"', commands)

    def test_marker_inventory_is_individual_and_exact(self):
        actual = {}
        for path in (ROOT / "tests").rglob("test_*.py"):
            tree = ast.parse(path.read_text())
            marked = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(
                        ast.unparse(d) == "pytest.mark.host_isolation"
                        for d in node.decorator_list
                    ):
                        marked.append(node.name)
            if marked:
                self.assertEqual(path.parent, ROOT / "tests/client")
                actual[path.name] = sorted(marked)
        self.assertEqual(actual, {p: sorted(n) for p, n in HOST_FUNCTIONS.items()})
        self.assertEqual(sum(map(len, actual.values())), 44)

    def test_automatic_gate_is_lightweight_and_full_check_is_manual(self):
        doc = workflow("check.yml")
        self.assertEqual(
            set(doc["on"]),
            {"schedule", "pull_request", "push", "workflow_dispatch"},
        )
        self.assertEqual(doc["on"]["push"]["branches"], ["main"])
        self.assertEqual(doc["on"]["schedule"], [{"cron": "17 3 * * *"}])
        self.assertNotIn("env", doc)
        jobs = doc["jobs"]
        self.assertEqual(set(jobs), {"lightweight", "full_check", "image"})

        lightweight = jobs["lightweight"]
        self.assertEqual(lightweight["name"], "Lightweight policy and wheel checks")
        self.assertEqual(
            lightweight["if"],
            "${{ github.event.repository.private == false && (github.event_name == 'pull_request' || github.event_name == 'push') }}",
        )
        self.assertNotIn("env", lightweight)
        self.assertNotIn("needs", lightweight)
        light_setup_python = [
            step
            for step in lightweight["steps"]
            if "actions/setup-python@" in step.get("uses", "")
        ]
        self.assertEqual(len(light_setup_python), 1)
        self.assertEqual(light_setup_python[0]["with"]["python-version"], "3.14")
        light_commands = "\n".join(step.get("run", "") for step in lightweight["steps"])
        for required in (
            "uv sync --locked",
            "uv build --wheel",
            ".github/ci/test_bwrap_policy.py",
            ".github/ci/test_private_namespace_canary.py",
            ".github/ci/test_ci_split.py",
        ):
            self.assertIn(required, light_commands)
        for forbidden in ("fetch-kubectl", "PYTEST_ADDOPTS"):
            self.assertNotIn(forbidden, light_commands)
        self.assertFalse(
            any(step.get("run") == "make check" for step in lightweight["steps"])
        )
        self.assertIn("does not run make check", light_commands)
        self.assertIn(
            "does not run make check or certify the repository test suite",
            light_commands,
        )

        job = jobs["full_check"]
        self.assertEqual(
            job["name"],
            "Full repository check (Python ${{ matrix.python-version }}, on demand)",
        )
        self.assertEqual(
            job["if"],
            "${{ github.event.repository.private == false && github.event_name == 'workflow_dispatch' }}",
        )
        self.assertEqual(job["timeout-minutes"], "25")
        self.assertEqual(
            job["strategy"],
            {
                "fail-fast": "false",
                "matrix": {"python-version": ["3.12", "3.13", "3.14"]},
            },
        )
        self.assertEqual(job["env"], {"UV_PYTHON": "${{ matrix.python-version }}"})
        self.assertNotIn("needs", job)
        steps = job["steps"]
        setup_python = [
            step for step in steps if "actions/setup-python@" in step.get("uses", "")
        ]
        self.assertEqual(len(setup_python), 1)
        self.assertEqual(
            setup_python[0]["with"]["python-version"],
            "${{ matrix.python-version }}",
        )
        gate = [step for step in steps if step.get("run") == "make check"]
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["env"], {"PYTEST_ADDOPTS": "-m 'not host_isolation'"})
        for step in steps:
            if step is not gate[0]:
                self.assertNotIn("PYTEST_ADDOPTS", step.get("env", {}))
            self.assertNotIn("continue-on-error", step)
        commands = "\n".join(step.get("run", "") for step in steps)
        for required in (
            "uv sync --locked",
            "uv build --wheel",
            "./scripts/fetch-kubectl",
            "make check",
            ".github/ci/test_bwrap_policy.py",
            ".github/ci/test_private_namespace_canary.py",
            ".github/ci/test_ci_split.py",
        ):
            self.assertIn(required, commands)
        self.assertLess(
            commands.index("./scripts/fetch-kubectl"), commands.index("make check")
        )
        for forbidden in (
            "sudo",
            "sysctl",
            "private-namespace-canary.py",
            "bwrap-policy.py",
            "exit 1",
            "continue-on-error",
        ):
            self.assertNotIn(forbidden, commands)
        self.assertIn("host isolation", commands)
        self.assertIn("does not certify the complete test suite", commands)
        self.assertNotIn("continue-on-error", job)

        make_check_steps = [
            step
            for candidate in jobs.values()
            for step in candidate["steps"]
            if step.get("run") == "make check"
        ]
        self.assertEqual(len(make_check_steps), 1)

    def test_event_routes_and_concurrency_keep_manual_checks_independent(self):
        doc = workflow("check.yml")
        concurrency = doc["concurrency"]
        self.assertIn("github.event_name", concurrency["group"])
        self.assertEqual(
            concurrency["cancel-in-progress"],
            "${{ github.event_name != 'workflow_dispatch' }}",
        )
        image = doc["jobs"]["image"]
        self.assertEqual(
            image["if"],
            "${{ github.event.repository.private == false && github.event_name != 'workflow_dispatch' }}",
        )
        self.assertEqual(
            doc["jobs"]["full_check"]["if"],
            "${{ github.event.repository.private == false && github.event_name == 'workflow_dispatch' }}",
        )
        self.assertNotEqual(
            doc["jobs"]["lightweight"]["if"], doc["jobs"]["full_check"]["if"]
        )

    def test_makefile_and_default_coverage_remain_intact(self):
        self.assertEqual(
            hashlib.sha256((ROOT / "Makefile").read_bytes()).hexdigest(),
            "1bb10c5c676999942ab62385d8533ec70b5278eaa027bf3e7b1cc4cc984e5ead",
        )
        import tomllib

        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            config["tool"]["pytest"]["ini_options"]["addopts"],
            "--strict-config --strict-markers --cov=cairn "
            "--cov-branch --cov-report=term-missing",
        )
        self.assertTrue(config["tool"]["coverage"]["run"]["branch"])

    def test_image_has_no_optional_dependency_or_suppression(self):
        image = workflow("check.yml")["jobs"]["image"]
        self.assertNotIn("needs", image)
        self.assertEqual(
            image["if"],
            "${{ github.event.repository.private == false && github.event_name != 'workflow_dispatch' }}",
        )
        self.assertNotIn("continue-on-error", image)
        steps = image["steps"]
        self.assertIn({"run": "make image IMAGE=cairn:ci"}, steps)
        self.assertIn({"run": "make smoke-image IMAGE=cairn:ci"}, steps)
        trivy = [s["with"] for s in steps if "trivy-action@" in s.get("uses", "")]
        self.assertEqual(
            [(s["exit-code"], s["ignore-unfixed"]) for s in trivy],
            [("0", "false"), ("1", "true")],
        )
        for step in steps:
            self.assertNotIn("continue-on-error", step)

    def test_manual_diagnostics_cannot_mutate_policy_or_hide_failure(self):
        path = ROOT / ".github/workflows/host-isolation.yml"
        self.assertTrue(path.is_file(), "manual diagnostics workflow missing")
        doc = workflow(path.name)
        self.assertEqual(set(doc["on"]), {"workflow_dispatch"})
        option = doc["on"]["workflow_dispatch"]["inputs"]["run_host_diagnostics"]
        self.assertEqual(option["type"], "boolean")
        self.assertEqual(option["default"], "false")
        self.assertEqual(doc["concurrency"]["cancel-in-progress"], "false")
        self.assertNotEqual(
            doc["concurrency"]["group"], workflow("check.yml")["concurrency"]["group"]
        )
        job = doc["jobs"]["host_diagnostics"]
        self.assertEqual(
            job["if"],
            "${{ github.event.repository.private == false && github.event_name == 'workflow_dispatch' && inputs.run_host_diagnostics }}",
        )
        self.assertNotIn("continue-on-error", job)
        self.assertEqual(job["timeout-minutes"], "15")
        setup_python = [
            step
            for step in job["steps"]
            if "actions/setup-python@" in step.get("uses", "")
        ]
        self.assertEqual(len(setup_python), 1)
        self.assertEqual(setup_python[0]["with"]["python-version"], "3.14")
        commands = "\n".join(s.get("run", "") for s in job["steps"])
        self.assertIn('test "$CAIRN_RUNNER_ENVIRONMENT" = github-hosted', commands)
        self.assertIn("pytest -n auto --no-cov -m host_isolation", commands)
        for forbidden in (
            "apparmor_parser",
            "sysctl",
            "private-namespace-canary.py",
            "bwrap-policy.py",
            "||",
            "continue-on-error",
        ):
            self.assertNotIn(forbidden, commands)
        for step in job["steps"]:
            self.assertNotIn("continue-on-error", step)
        self.assertIn("not full host acceptance", commands)

    def test_collection_partitions_preserve_every_conformance_case(self):
        def collect(selector):
            env = os.environ.copy()
            env.pop("PYTEST_ADDOPTS", None)
            if selector:
                env["PYTEST_ADDOPTS"] = "-m " + repr(selector)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "--no-cov",
                    "tests/conformance",
                    "tests",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return {
                line
                for line in result.stdout.splitlines()
                if line.startswith("tests/") and "::" in line
            }

        full = collect(None)
        ordinary = collect("not host_isolation")
        host = collect("host_isolation")
        self.assertTrue(full)
        self.assertTrue(host)
        self.assertFalse(ordinary & host)
        self.assertEqual(ordinary | host, full)
        expected = {
            f"tests/client/{path}::{name}"
            for path, names in HOST_FUNCTIONS.items()
            for name in names
        }
        self.assertEqual({node.split("[")[0] for node in host}, expected)
        self.assertEqual(
            host, {node for node in full if node.split("[")[0] in expected}
        )
        conformance = {node for node in full if node.startswith("tests/conformance/")}
        self.assertTrue(conformance)
        self.assertTrue(conformance <= ordinary)
        print(
            f"collection: full={len(full)} ordinary={len(ordinary)} "
            f"host={len(host)} conformance={len(conformance)}"
        )
        for name, nodes in (("full", full), ("ordinary", ordinary), ("host", host)):
            digest = hashlib.sha256(("\n".join(sorted(nodes)) + "\n").encode())
            print(f"{name} node IDs sha256={digest.hexdigest()}")

    def test_retained_mixed_modules_never_reach_real_spawn(self):
        # The real seam creates a jailed process; intercept it before any kernel
        # action. A recorded attempt fails even if a test catches the exception.
        script = """
import sys
from pathlib import Path
import os
sys.path.insert(0, str(Path.cwd()))
from scripts import host_workflow_sandbox as sandbox
attempts = []
def forbidden(*args, **kwargs):
    attempts.append(True)
    raise AssertionError("ordinary test reached real sandbox spawn")
sandbox._spawn = forbidden
# Ordinary checks must also work without a Bubblewrap executable installed.
# Keep all staged-file checks real; simulate absence only for this exact path.
real_stat, real_lstat, real_access = os.stat, os.lstat, os.access
def absent(operation):
    def checked(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == '/usr/bin/bwrap':
            raise FileNotFoundError('/usr/bin/bwrap')
        return operation(path, *args, **kwargs)
    return checked
os.stat, os.lstat = absent(real_stat), absent(real_lstat)
def access(path, *args, **kwargs):
    if os.fsdecode(path) == '/usr/bin/bwrap':
        return False
    return real_access(path, *args, **kwargs)
os.access = access
import pytest
status = pytest.main(sys.argv[1:])
raise SystemExit(1 if attempts else status)
"""
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                "-q",
                "--no-cov",
                "-m",
                "not host_isolation",
                *(f"tests/client/{path}" for path in HOST_FUNCTIONS),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
