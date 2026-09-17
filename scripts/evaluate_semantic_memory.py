"""Run the real semantic path on synthetic data in a disposable local Docker.

No ambient provider configuration, existing containers or volumes are used.
Exit 0 requires measured quality success; 1 is a measured miss; 2 is a failed
or incomplete run. A designated test-provider key is mandatory. This script
does not tune retrieval or turn deterministic tests into semantic evidence.
"""

import argparse
import json
import math
import os
import re
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from semantic_memory_corpus import CorpusError, validate_corpus
from semantic_memory_environment import (
    EnvironmentError,
    falkordb_image,
    provider_environment,
)

ROOT = Path(__file__).resolve().parents[1]
LABEL = "invalid.example.cairn.semantic-evaluation"


def docker(*args: str) -> str:
    """Only the explicitly authorised local daemon; no ambient Docker context."""
    try:
        result = subprocess.run(
            ["docker", "--host", "unix:///var/run/docker.sock", *args],
            env={"PATH": os.environ.get("PATH", os.defpath)},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise EnvironmentError("disposable_docker_failed") from None
    return result.stdout.strip()


def remove_owned(kind: str, identity: str, owner: str) -> None:
    """Refuse cleanup if exact resource ownership cannot be reconfirmed."""
    if (
        kind not in ("container", "network")
        or re.fullmatch(r"[0-9a-f]{64}", identity) is None
    ):
        raise EnvironmentError("disposable_resource_identity_invalid")
    document = json.loads(docker(kind, "inspect", identity))[0]
    labels = document["Config"]["Labels"] if kind == "container" else document["Labels"]
    if labels.get(LABEL) != owner:
        raise EnvironmentError("disposable_resource_owner_mismatch")
    if kind == "container":
        docker("container", "rm", "--force", identity)
    else:
        docker("network", "rm", identity)


def wait_ready(port: int) -> None:
    """Container start is not Redis readiness; probe only our published port."""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=0.5
            ) as connection:
                connection.sendall(b"*1\r\n$4\r\nPING\r\n")
                if connection.recv(64) == b"+PONG\r\n":
                    return
        except OSError:
            pass
        sleep(0.05)
    raise EnvironmentError("disposable_falkordb_not_ready")


def reconcile_owned(kind: str, identity: str | None, name: str, owner: str) -> None:
    """Recover an uncertain creation response by its preselected name and label."""
    if identity is None or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        args = [kind, "ls"]
        if kind == "container":
            args.append("--all")
        candidates = docker(
            *args,
            "--quiet",
            "--no-trunc",
            "--filter",
            f"name={name}",
            "--filter",
            f"label={LABEL}={owner}",
        ).splitlines()
        if not candidates:
            return
        if len(candidates) != 1 or re.fullmatch(r"[0-9a-f]{64}", candidates[0]) is None:
            raise EnvironmentError("disposable_resource_identity_invalid")
        identity = candidates[0]
        document = json.loads(docker(kind, "inspect", identity))[0]
        if document["Name"].removeprefix("/") != name:
            raise EnvironmentError("disposable_resource_owner_mismatch")
    remove_owned(kind, identity, owner)


def native_scratch_parent(mountinfo: str | None = None) -> Path:
    """Select native /tmp independently of inherited tempfile settings."""
    parent = Path("/tmp")
    if parent.resolve() != parent or not parent.is_dir():
        raise EnvironmentError("native_scratch_unavailable")
    if mountinfo is None:
        with Path("/proc/self/mountinfo").open() as stream:
            mountinfo = stream.read(1024 * 1024 + 1)
        if len(mountinfo) > 1024 * 1024:
            raise EnvironmentError("native_scratch_unavailable")
    matches: list[tuple[int, str]] = []
    device = parent.stat().st_dev
    device_id = f"{os.major(device)}:{os.minor(device)}"
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        mount = Path(
            re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        )
        if fields[2] == device_id and parent.is_relative_to(mount):
            matches.append((len(mount.parts), fields[fields.index("-") + 1]))
    native = {"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "overlay", "zfs"}
    if not matches or max(matches)[1] not in native:
        raise EnvironmentError("native_scratch_unavailable")
    return parent


@contextmanager
def disposable_falkordb(image: str) -> Iterator[tuple[str, int]]:
    owner = str(uuid4())
    name = f"cairn-semantic-{owner}"
    network: str | None = None
    container: str | None = None
    try:
        network = docker("network", "create", "--label", f"{LABEL}={owner}", name)
        container = docker(
            "container",
            "create",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{LABEL}={owner}",
            "--network",
            name,
            "--publish",
            "127.0.0.1::6379",
            "--tmpfs",
            "/data",
            "--memory",
            "2g",
            "--cpus",
            "2",
            # Match the reviewed Compose/Kubernetes index configuration. The
            # image alone defaults to a 1s query timeout and a web console;
            # neither is the environment Cairn actually deploys. These are
            # fixed fixture settings, never inherited provider/operator input.
            "--env",
            "FALKORDB_ARGS=MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000",
            "--env",
            "BROWSER=0",
            "--env",
            "TLS=0",
            image,
        )
        if re.fullmatch(r"[0-9a-f]{64}", container) is None:
            raise EnvironmentError("disposable_resource_identity_invalid")
        docker("container", "start", container)
        document = json.loads(docker("container", "inspect", container))[0]
        binding = document["NetworkSettings"]["Ports"]["6379/tcp"]
        if len(binding) != 1 or binding[0]["HostIp"] != "127.0.0.1":
            raise EnvironmentError("disposable_binding_invalid")
        port = int(binding[0]["HostPort"])
        if not 1 <= port <= 65535:
            raise EnvironmentError("disposable_binding_invalid")
        wait_ready(port)
        yield owner, port
    finally:
        try:
            reconcile_owned("container", container, name, owner)
        finally:
            reconcile_owned("network", network, name, owner)


def load_corpus(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CorpusError()
    return validate_corpus(json.loads(raw))


def failure_report(document: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
    """Retain only validated scalar outcomes, never arbitrary child diagnostics."""
    from cairn.authority.memory_types import (
        GRADED_POLICY,
        GRADED_RELEVANT_POLICY,
        POLICY,
        RELEVANT_POLICY,
        SEMANTIC_UNAVAILABLE,
    )

    def require(condition: bool) -> None:
        if not condition:
            raise EnvironmentError("semantic_evaluation_incomplete")

    def number(value: Any) -> bool:
        return (
            type(value) in (int, float)
            and 0 <= value <= sys.float_info.max
            and math.isfinite(value)
        )

    policies = {
        GRADED_POLICY,
        GRADED_RELEVANT_POLICY,
        POLICY,
        RELEVANT_POLICY,
        POLICY + SEMANTIC_UNAVAILABLE,
        RELEVANT_POLICY + SEMANTIC_UNAVAILABLE,
    }
    rows = document["scenarios"]
    require(type(rows) is list and 0 < len(rows) <= len(corpus["queries"]))
    safe_rows = []
    metric_names = {
        "precision_at_k",
        "recall_at_k",
        "reciprocal_rank_at_k",
        "returned_count",
        "irrelevant_at_k",
        "irrelevant_returned",
        "duplicate_returned",
    }
    for query, row in zip(corpus["queries"], rows, strict=False):
        require(type(row) is dict and row["name"] == query["name"])
        require(type(row["policy"]) is str and row["policy"] in policies)
        require(number(row["latency_ms"]))
        require(
            type(row["semantic_degraded"]) is bool
            and type(row["expectations_met"]) is bool
        )
        metrics = row["metrics"]
        require(type(metrics) is dict and set(metrics) <= metric_names)
        require(all(value is None or number(value) for value in metrics.values()))
        safe_rows.append(
            {
                key: row[key]
                for key in (
                    "name",
                    "policy",
                    "semantic_degraded",
                    "expectations_met",
                    "latency_ms",
                    "metrics",
                )
            }
        )
    history = document["correction_history_checks"]
    require(
        type(history) is list
        and len(history) <= len(corpus["corrections"])
        and all(type(value) is bool for value in history)
    )
    unrun = [query["name"] for query in corpus["queries"][len(rows) :]]
    remaining_history = len(corpus["corrections"]) - len(history)
    require(document["unrun_queries"] == unrun)
    require(
        type(document["unrun_history_checks"]) is int
        and document["unrun_history_checks"] == remaining_history
    )
    require(number(document["elapsed_seconds"]))
    result = {
        "schema": "cairn.semantic-memory-evaluation/v1",
        "complete": False,
        "semantic_evidence": False,
        "quality_expectations_met": False,
        "failure": "semantic_evaluation_stopped",
        "scenarios": safe_rows,
        "elapsed_seconds": document["elapsed_seconds"],
        "unrun_queries": unrun,
        "unrun_history_checks": remaining_history,
        "correction_history_checks": history,
    }
    for key in ("source_sha256", "corpus_sha256", "lock_sha256"):
        if key in document:
            require(
                type(document[key]) is str
                and re.fullmatch(r"[0-9a-f]{64}", document[key]) is not None
            )
            result[key] = document[key]
    return result


def run(args: argparse.Namespace) -> int:
    if os.path.lexists(args.output):
        raise EnvironmentError("output_exists")
    corpus = load_corpus(args.corpus)
    child_environment = provider_environment(args.provider_key_file, os.environ)
    image = falkordb_image((ROOT / "deploy/images.lock").read_text())
    with TemporaryDirectory(
        prefix="cairn-semantic-", dir=native_scratch_parent()
    ) as scratch:
        root = Path(scratch)
        (root / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False))
        document: dict[str, Any] | None = None
        cleanup_succeeded = False
        try:
            with disposable_falkordb(image) as (owner, port):
                try:
                    child = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/semantic_memory_live.py"),
                            str(root),
                            str(port),
                            str(args.deadline_seconds),
                            owner,
                        ],
                        cwd=ROOT,
                        env=child_environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=args.deadline_seconds + 30,
                    )
                except subprocess.TimeoutExpired:
                    raise EnvironmentError("semantic_evaluation_deadline") from None
                if (
                    child.returncode not in (0, 1)
                    or not (root / "result.json").is_file()
                ):
                    raise EnvironmentError("semantic_evaluation_incomplete")
                with (root / "result.json").open("rb") as stream:
                    raw = stream.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise EnvironmentError("semantic_evaluation_incomplete")
                candidate = json.loads(raw)
                if (
                    type(candidate) is not dict
                    or candidate.get("schema") != "cairn.semantic-memory-evaluation/v1"
                    or type(candidate.get("complete")) is not bool
                    or type(candidate.get("quality_expectations_met")) is not bool
                    or candidate.get("semantic_evidence") is not True
                ):
                    raise EnvironmentError("semantic_evaluation_incomplete")
                json.dumps(candidate, allow_nan=False)
                safe_failure = failure_report(candidate, corpus)
                if not candidate["complete"]:
                    if (
                        child.returncode != 1
                        or candidate["quality_expectations_met"]
                        or candidate.get("failure") != "semantic_evaluation_stopped"
                    ):
                        raise EnvironmentError("semantic_evaluation_incomplete")
                    candidate = safe_failure
                elif (
                    safe_failure["unrun_queries"]
                    or safe_failure["unrun_history_checks"]
                    or any(
                        row["semantic_degraded"] for row in safe_failure["scenarios"]
                    )
                    or candidate["quality_expectations_met"]
                    != (
                        all(
                            row["expectations_met"] for row in safe_failure["scenarios"]
                        )
                        and all(safe_failure["correction_history_checks"])
                    )
                    or child.returncode
                    != (0 if candidate["quality_expectations_met"] else 1)
                ):
                    raise EnvironmentError("semantic_evaluation_incomplete")
                document = candidate
            cleanup_succeeded = True
        except Exception:
            if document is None:
                raise
            # A valid child report exists; failure after that point is cleanup.
            # Preserve scalar evidence but never certify resource removal.
            document = failure_report(document, corpus)
            document["failure"] = "disposable_cleanup_failed"
        document["falkordb_image"] = image
        document["disposable_resources_removed"] = cleanup_succeeded
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(
                document,
                output,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            output.write("\n")
        if not document["complete"]:
            return 2
        return 0 if document["quality_expectations_met"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=ROOT / "tests/fixtures/memory_quality/everyday-v1.json",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=900,
        choices=range(60, 1801),
        metavar="60..1800",
    )
    args = parser.parse_args()
    try:
        return run(args)
    except (CorpusError, EnvironmentError) as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print("semantic_evaluation_incomplete", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
