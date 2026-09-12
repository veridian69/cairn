"""Runner preflight must fail before Docker or provider access."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluate_semantic_memory.py"


def runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("semantic_runner_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "semantic_live_integration", SCRIPT.parent / "semantic_memory_live.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("cleanup_failed", [False, True])
@pytest.mark.parametrize("output_race", [False, True])
@pytest.mark.parametrize("child_code", [0, 1, 2])
def test_launcher_retains_safe_partial_report_after_scratch_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failed: bool,
    output_race: bool,
    child_code: int,
) -> None:
    module = runner(monkeypatch)
    destination = tmp_path / "partial.json"
    roots: list[Path] = []
    child_calls: list[bool] = []
    monkeypatch.setattr(
        module,
        "load_corpus",
        lambda _: {
            "queries": [{"name": "first"}, {"name": "later"}],
            "corrections": [{}],
        },
    )
    monkeypatch.setattr(module, "provider_environment", lambda *a: {})
    monkeypatch.setattr(module, "falkordb_image", lambda _: "locked-image")

    @contextmanager
    def lifecycle(image: str) -> Any:
        try:
            yield "owned", 1234
        finally:
            if output_race:
                assert destination.read_text() == "existing evidence"
            else:
                assert not destination.exists()  # No premature cleanup attestation.
            if cleanup_failed:
                raise module.EnvironmentError("disposable_docker_failed")

    def child(command: list[str], **kwargs: Any) -> Any:
        child_calls.append(True)
        assert (
            kwargs["stdout"] is subprocess.DEVNULL
            and kwargs["stderr"] is subprocess.DEVNULL
        )
        root = Path(command[2])
        roots.append(root)
        (root / "result.json").write_text(
            json.dumps(
                {
                    "schema": "cairn.semantic-memory-evaluation/v1",
                    "complete": False,
                    "semantic_evidence": True,
                    "quality_expectations_met": False,
                    "failure": "semantic_evaluation_stopped",
                    "elapsed_seconds": 2.0,
                    "scenarios": [
                        {
                            "name": "first",
                            "policy": "lexical-graded/v2",
                            "semantic_degraded": True,
                            "expectations_met": False,
                            "latency_ms": 123.5,
                            "metrics": {"returned_count": 1},
                            "query": "secret-sentinel",
                            "returned_labels": ["secret-sentinel"],
                            "raw_provider_error": "secret-sentinel",
                        }
                    ],
                    "unrun_queries": ["later"],
                    "unrun_history_checks": 1,
                    "correction_history_checks": [],
                    "raw_stderr": "secret-sentinel",
                }
            )
        )
        if output_race:
            destination.write_text("existing evidence")
        return SimpleNamespace(
            returncode=child_code, stdout="secret-sentinel", stderr="secret-sentinel"
        )

    monkeypatch.setattr(module, "disposable_falkordb", lifecycle)
    monkeypatch.setattr(module.subprocess, "run", child)
    args = SimpleNamespace(
        output=destination,
        corpus=tmp_path / "not-read",
        provider_key_file=tmp_path / "not-read",
        deadline_seconds=60,
    )
    if child_code != 1:
        # A file cannot turn a refused/failed worker invocation into a usable
        # evaluation outcome. The sibling harness must emit exit 1 for partials.
        with pytest.raises(module.EnvironmentError):
            module.run(args)
        assert child_calls == [True] and all(not root.exists() for root in roots)
        if output_race:
            assert destination.read_text() == "existing evidence"
        else:
            assert not destination.exists()
        return
    if output_race:
        with pytest.raises(FileExistsError):
            module.run(args)
        assert destination.read_text() == "existing evidence"
        assert all(not root.exists() for root in roots)
        assert child_calls == [True]
        return
    assert module.run(args) == 2
    raw = destination.read_text()
    saved = json.loads(raw)
    assert all(not root.exists() for root in roots)
    assert child_calls == [True]
    assert saved["complete"] is False and saved["quality_expectations_met"] is False
    assert saved["semantic_evidence"] is False
    assert saved["disposable_resources_removed"] is (not cleanup_failed)
    assert saved["scenarios"][0]["latency_ms"] == 123.5
    assert saved["scenarios"][0]["metrics"]["returned_count"] == 1
    assert saved["unrun_queries"] == ["later"] and saved["unrun_history_checks"] == 1
    assert "secret-sentinel" not in raw
    if cleanup_failed:
        assert saved["failure"] == "disposable_cleanup_failed"
    existing = destination.read_bytes()
    with pytest.raises(module.EnvironmentError, match="output_exists"):
        module.run(args)
    assert destination.read_bytes() == existing and child_calls == [True]


@pytest.mark.parametrize("child_code", [0, 1, 2])
@pytest.mark.parametrize("quality_claim", [False, True])
@pytest.mark.parametrize(
    "outcome", ["healthy", "scenario_miss", "history_miss", "degraded"]
)
def test_launcher_complete_flags_bind_to_measured_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_code: int,
    quality_claim: bool,
    outcome: str,
) -> None:
    from cairn.authority.memory_types import RELEVANT_POLICY, SEMANTIC_UNAVAILABLE

    module = runner(monkeypatch)
    destination = tmp_path / "complete.json"
    cleaned, roots, calls = [], [], []
    monkeypatch.setattr(
        module,
        "load_corpus",
        lambda _: {
            "queries": [{"name": "observed"}],
            "corrections": [{}],
        },
    )
    monkeypatch.setattr(module, "provider_environment", lambda *a: {})
    monkeypatch.setattr(module, "falkordb_image", lambda _: "locked-image")

    @contextmanager
    def lifecycle(image: str) -> Any:
        try:
            yield "owned", 1234
        finally:
            assert not destination.exists()
            cleaned.append(True)

    def child(command: list[str], **kwargs: Any) -> Any:
        calls.append(True)
        root = Path(command[2])
        roots.append(root)
        (root / "result.json").write_text(
            json.dumps(
                {
                    "schema": "cairn.semantic-memory-evaluation/v1",
                    "complete": True,
                    "semantic_evidence": True,
                    "quality_expectations_met": quality_claim,
                    "failure": None,
                    "elapsed_seconds": 2.0,
                    "scenarios": [
                        {
                            "name": "observed",
                            "policy": (
                                RELEVANT_POLICY + SEMANTIC_UNAVAILABLE
                                if outcome == "degraded"
                                else "lexical-graded/v2"
                            ),
                            "semantic_degraded": outcome == "degraded",
                            "expectations_met": outcome != "scenario_miss",
                            "latency_ms": 123.5,
                            "metrics": {"returned_count": 1},
                        }
                    ],
                    "unrun_queries": [],
                    "unrun_history_checks": 0,
                    "correction_history_checks": [outcome != "history_miss"],
                }
            )
        )
        return SimpleNamespace(returncode=child_code)

    monkeypatch.setattr(module, "disposable_falkordb", lifecycle)
    monkeypatch.setattr(module.subprocess, "run", child)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--provider-key-file",
            "unused",
            "--corpus",
            "unused",
            "--output",
            str(destination),
        ],
    )
    # Complete healthy success is child 0; a genuine full measured miss is 1.
    accepted = (outcome == "healthy" and quality_claim and child_code == 0) or (
        outcome in ("scenario_miss", "history_miss")
        and not quality_claim
        and child_code == 1
    )
    assert module.main() == ((0 if quality_claim else 1) if accepted else 2)
    assert cleaned == [True] and calls == [True]
    assert all(not root.exists() for root in roots)
    if accepted:
        saved = json.loads(destination.read_text())
        assert saved["complete"] and saved["semantic_evidence"]
        assert saved["quality_expectations_met"] is quality_claim
        assert saved["disposable_resources_removed"]
        assert saved["scenarios"][0]["latency_ms"] == 123.5
    else:
        assert not destination.exists()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "{",
        {},
        {"complete": False},
        {
            "schema": "cairn.semantic-memory-evaluation/v1",
            "complete": True,
            "semantic_evidence": True,
            "quality_expectations_met": True,
            "scenarios": [],
        },
        {
            "schema": "cairn.semantic-memory-evaluation/v1",
            "complete": False,
            "semantic_evidence": True,
            "quality_expectations_met": False,
            "scenarios": [{"latency_ms": float("nan")}],
        },
    ],
)
def test_launcher_missing_or_malformed_partial_report_stays_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    module = runner(monkeypatch)
    cleaned: list[bool] = []
    monkeypatch.setattr(
        module, "load_corpus", lambda _: {"queries": [], "corrections": []}
    )
    monkeypatch.setattr(module, "provider_environment", lambda *a: {})
    monkeypatch.setattr(module, "falkordb_image", lambda _: "image")

    @contextmanager
    def lifecycle(image: str) -> Any:
        try:
            yield "owned", 1
        finally:
            cleaned.append(True)

    def child(command: list[str], **kwargs: Any) -> Any:
        if bad is not None:
            (Path(command[2]) / "result.json").write_text(
                bad if isinstance(bad, str) else json.dumps(bad)
            )
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(module, "disposable_falkordb", lifecycle)
    monkeypatch.setattr(module.subprocess, "run", child)
    destination = tmp_path / "absent.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--provider-key-file",
            "unused",
            "--output",
            str(destination),
            "--corpus",
            "unused",
        ],
    )
    assert module.main() == 2
    assert cleaned == [True] and not destination.exists()


def test_scored_operation_latches_and_blocks_both_search_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.projection.graphiti import GraphitiIndex, GraphitiIndexError
    from cairn.projection.semantic_evidence import SemanticEvidenceError

    live = live_module(monkeypatch)
    calls: list[str] = []

    def failed(*args: Any, **kwargs: Any) -> Any:
        calls.append("scored")
        raise SemanticEvidenceError()

    monkeypatch.setattr(GraphitiIndex, "search_with_evidence", failed)
    index = live.BoundedIndex.__new__(live.BoundedIndex)
    index.failed = False
    for _ in range(2):
        with pytest.raises(SemanticEvidenceError):
            index.search_with_evidence("synthetic", 256, ("partition",))
    assert calls == ["scored"]
    assert index.failed
    with pytest.raises(GraphitiIndexError, match="evaluation_stopped"):
        index.search("synthetic", 256, ("partition",))


@pytest.mark.parametrize("fail_at", [1, 3, None])
@pytest.mark.parametrize("failure_kind", ["source", "envelope"])
def test_graded_recall_returns_and_persists_partial_evidence_before_stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int | None,
    failure_kind: str,
) -> None:
    from cairn.projection.adapter import FactProjected
    from cairn.projection.semantic_evidence import (
        SEARCH_POLICY,
        PartitionGrades,
        SemanticEvidence,
        SemanticEvidenceError,
        local_representation_sha256,
        query_sha256,
    )

    live = live_module(monkeypatch)

    # The worker's CLI-only sibling imports are not on mypy's module path.
    # Check overrides against its concrete base; execute the actual latch class.
    if TYPE_CHECKING:
        from cairn.projection.graphiti import GraphitiIndex as BoundedIndex
    else:
        from scripts.semantic_memory_live import BoundedIndex

    class Controlled(BoundedIndex):
        def __init__(self) -> None:
            self.failed = False
            self.closed = 0
            self.searches = 0
            self.bare_searches = 0
            monkeypatch.setattr(
                self,
                "_graphiti",
                SimpleNamespace(
                    llm_client=SimpleNamespace(
                        model="controlled", small_model="controlled"
                    ),
                    embedder=SimpleNamespace(
                        config=SimpleNamespace(
                            embedding_dim=1024, embedding_model="text-embedding-3-small"
                        )
                    ),
                ),
                raising=False,
            )

        def project(self, state: Any) -> Any:
            return FactProjected()

        def project_many(self, states: Any) -> Any:
            return tuple(FactProjected() for _ in states)

        def _call(self, coroutine: Any, **kwargs: Any) -> Any:
            return asyncio.run(coroutine)

        async def _search_with_evidence(
            self, query: str, limit: int, partitions: tuple[str, ...], query_hash: str
        ) -> Any:
            self.searches += 1
            if self.searches == fail_at and failure_kind == "source":
                raise SemanticEvidenceError()
            return SemanticEvidence(
                "0" * 64 if self.searches == fail_at else query_sha256(query),
                local_representation_sha256(),
                SEARCH_POLICY,
                (),
                tuple(PartitionGrades(key, True, 0, ()) for key in partitions),
            )

        def search(self, *args: Any, **kwargs: Any) -> Any:
            self.bare_searches += 1
            raise AssertionError("graded recall must not use bare-ID search")

        def close(self) -> None:
            self.closed += 1

    index = Controlled()
    monkeypatch.setattr(live, "BoundedIndex", lambda port: index)
    # This tiny development-only input is not the full/held-out corpus; only
    # corpus admission is replaced, not seed_events, assessment, SQLite or API.
    document = {
        "facts": [
            {
                "label": "old",
                "body": "signal old",
                "scope": "job",
                "recorded_at": "2024-01-01T00:00:00+00:00",
            },
            {
                "label": "new",
                "body": "signal current",
                "scope": "job",
                "recorded_at": "2024-01-02T00:00:00+00:00",
            },
        ],
        "corrections": [
            {
                "source": "old",
                "replacement": "new",
                "reason": "replacement",
                "recorded_at": "2024-01-03T00:00:00+00:00",
            }
        ],
        "queries": [
            {
                "name": f"q{n}",
                "split": "development",
                "query": "signal",
                "relevant": ["new"],
                "top_k": 2,
                "minimum_recall": 1,
            }
            for n in range(3)
        ],
    }
    (tmp_path / "corpus.json").write_text(json.dumps(document))
    monkeypatch.setattr(live, "validate_corpus", lambda value: value)
    monkeypatch.setattr(live, "source_digest", lambda: "a" * 64)
    calls: list[str] = []
    original_post = live.AsyncClient.post

    async def observed_post(client: Any, url: str, **kwargs: Any) -> Any:
        calls.append(url)
        return await original_post(client, url, **kwargs)

    monkeypatch.setattr(live.AsyncClient, "post", observed_post)
    returned: list[Any] = []
    evaluate = live.evaluate

    async def capture(*args: Any) -> Any:
        result = await evaluate(*args)
        returned.append(result)
        return result

    monkeypatch.setattr(live, "evaluate", capture)
    monkeypatch.setattr(sys, "argv", ["live", str(tmp_path), "1", "30", "integration"])
    assert live.main() == (1 if fail_at else 0)
    persisted = json.loads((tmp_path / "result.json").read_bytes())
    assert persisted == returned[0]
    assert index.searches == (fail_at or 3)
    assert index.bare_searches == 0  # Authority can catch a backend exception.
    assert index.closed == 1
    rows = persisted["scenarios"]
    assert len(rows) == (fail_at or 3)
    assert all(r["latency_ms"] >= 0 for r in rows)
    assert calls.count("/memory/v1/recall") == (fail_at or 3)
    assert calls.count("/memory/v1/history") == (0 if fail_at else 1)
    assert persisted["complete"] is (fail_at is None)
    assert persisted["quality_expectations_met"] is (fail_at is None)
    assert persisted["unrun_queries"] == [f"q{n}" for n in range(fail_at or 3, 3)]
    if fail_at:
        assert rows[-1]["semantic_degraded"] is True
        assert "semantic-unavailable" in rows[-1]["policy"]
        assert rows[-1]["returned_labels"] == ["new"]
        assert persisted["failure"] == "semantic_evaluation_stopped"
        assert persisted["unrun_history_checks"] == 1
        assert index.failed is (failure_kind == "source")
        safe = runner(monkeypatch).failure_report(persisted, document)
        assert safe["scenarios"][-1]["semantic_degraded"] is True
        assert safe["source_sha256"] == "a" * 64
        assert safe["unrun_history_checks"] == 1
        assert "query" not in safe["scenarios"][-1]
    else:
        assert rows[-1]["policy"].startswith("lexical-graded/v2")


def test_provider_metadata_reports_available_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphiti_core.llm_client.token_tracker import TokenUsageTracker

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "semantic_live_metadata", SCRIPT.parent / "semantic_memory_live.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tracker = TokenUsageTracker()  # type: ignore[no-untyped-call]  # Pinned dependency constructor lacks annotations.
    tracker.record("extract", 100, 20)
    tracker.record("resolve", 50, 10)
    graphiti = SimpleNamespace(
        llm_client=SimpleNamespace(
            model="synthetic-medium",
            small_model="synthetic-small",
            token_tracker=tracker,
            max_tokens=321,
            temperature=0.25,
            reasoning="low",
            verbosity="medium",
        ),
        embedder=SimpleNamespace(
            config=SimpleNamespace(
                embedding_model="synthetic-embedding", embedding_dim=42
            )
        ),
        cross_encoder=SimpleNamespace(
            config=SimpleNamespace(model="synthetic-reranker")
        ),
    )
    metadata = module.provider_metadata(graphiti)
    assert metadata["models"]["embedding_model"] == "synthetic-embedding"
    assert metadata["models"]["embedding_dimensions"] == 42
    assert metadata["models"]["reranker"] == "synthetic-reranker"
    assert metadata["output_settings"]["extraction"] == {
        "max_tokens": 321,
        "temperature": 0.25,
        "reasoning": "low",
        "verbosity": "medium",
    }
    assert (
        metadata["output_settings"]["reranking"]["max_tokens"]
        == "unavailable_from_client_configuration"
    )
    graphiti.llm_client = SimpleNamespace(model="m", small_model="s")
    assert set(
        module.provider_metadata(graphiti)["output_settings"]["extraction"].values()
    ) == {"unavailable"}
    usage = metadata["usage"]
    assert usage["input_including_cached"] == 150
    assert usage["output"] == 30
    assert usage["model_calls"] == 2
    assert usage["fresh_input"] == usage["reused_input"] == "unavailable"
    assert usage["coverage"] == "successful_llm_calls_only"
    assert usage["excludes"] == ["embeddings", "reranking", "failed_calls"]


def test_disposable_engine_matches_reviewed_deployment_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    module = runner(monkeypatch)
    expected = {
        "FALKORDB_ARGS": "MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000",
        "BROWSER": "0",
        "TLS": "0",
    }
    root = SCRIPT.parents[1]
    compose = yaml.safe_load(
        (root / "deploy/compose/compose.graphiti.yaml").read_text()
    )["services"]["falkordb"]["environment"]
    statefulset = yaml.safe_load(
        (root / "deploy/kustomize/components/falkordb/statefulset.yaml").read_text()
    )
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    kubernetes = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert {key: compose[key] for key in expected} == expected
    assert {key: kubernetes[key] for key in expected} == expected
    for key in expected:
        monkeypatch.setenv(key, "unreviewed-ambient-override")
    calls: list[tuple[str, ...]] = []
    removed: list[tuple[str, str | None, str, str]] = []

    def docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("network", "create"):
            return "a" * 64
        if args[:2] == ("container", "create"):
            return "b" * 64
        if args[:2] == ("container", "start"):
            return "b" * 64
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                [
                    {
                        "NetworkSettings": {
                            "Ports": {
                                "6379/tcp": [
                                    {"HostIp": "127.0.0.1", "HostPort": "16379"}
                                ]
                            }
                        }
                    }
                ]
            )
        raise AssertionError(args)

    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(module, "wait_ready", lambda port: None)
    monkeypatch.setattr(module, "reconcile_owned", lambda *args: removed.append(args))
    with module.disposable_falkordb("synthetic-test-image") as (owner, port):
        assert port == 16379
        create = next(call for call in calls if call[:2] == ("container", "create"))
        environment = dict(
            create[i + 1].split("=", 1)
            for i, argument in enumerate(create)
            if argument == "--env"
        )
        assert environment == expected
        assert create[-1] == "synthetic-test-image"
        assert create[create.index("--cpus") + 1] == "2"
        assert create[create.index("--memory") + 1] == "2g"
        assert create[create.index("--publish") + 1] == "127.0.0.1::6379"
        assert "--volume" not in create
    assert [entry[0] for entry in removed] == ["container", "network"]
    assert all(entry[3] == owner for entry in removed)


def test_scratch_parent_ignores_windows_temp_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner(monkeypatch)
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, "/mnt/c/not-native")
    assert module.native_scratch_parent() == Path("/tmp")
    with pytest.raises(module.EnvironmentError, match="native_scratch_unavailable"):
        module.native_scratch_parent(
            "1 0 0:1 / / rw - ext4 none rw\n2 1 0:2 / /tmp rw - 9p none rw\n"
        )


@pytest.mark.parametrize("failed_kind", ["network", "container"])
def test_ambiguous_docker_create_reconciles_only_owned_exact_ids(
    monkeypatch: pytest.MonkeyPatch, failed_kind: str
) -> None:
    module = runner(monkeypatch)
    resources: dict[str, dict[str, Any]] = {}
    removed: list[str] = []

    def docker(*args: str) -> str:
        kind, action = args[:2]
        if action == "create":
            owner = args[args.index("--label") + 1].split("=", 1)[1]
            name = args[-1] if kind == "network" else args[args.index("--name") + 1]
            resources[kind] = {
                "id": ("a" if kind == "network" else "b") * 64,
                "owner": owner,
                "name": name,
            }
            if kind == failed_kind:
                raise module.EnvironmentError("disposable_docker_failed")
            return str(resources[kind]["id"])
        if action == "ls":
            return str(resources[kind]["id"]) if kind in resources else ""
        if action == "inspect":
            resource = resources[kind]
            labels = {module.LABEL: resource["owner"]}
            return json.dumps(
                [
                    {
                        "Id": resource["id"],
                        "Name": resource["name"],
                        "Labels": labels,
                        "Config": {"Labels": labels},
                    }
                ]
            )
        if action == "rm":
            assert args[-1] == resources[kind]["id"]
            removed.append(kind)
            del resources[kind]
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(module, "docker", docker)
    with pytest.raises(module.EnvironmentError, match="disposable_docker_failed"):
        with module.disposable_falkordb("synthetic-test-image"):
            raise AssertionError("A failed create cannot enter the context")
    assert resources == {}
    assert removed == (
        ["network"] if failed_kind == "network" else ["container", "network"]
    )


def test_missing_key_refuses_without_docker_or_ambient_credentials(
    tmp_path: Path,
) -> None:
    assert SCRIPT.is_file(), "Real semantic runner is missing"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--provider-key-file",
            str(tmp_path / "absent"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        env={"PATH": "", "OPENAI_API_KEY": "ambient-do-not-use"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "test_provider_key_unavailable" in result.stderr
    assert "ambient-do-not-use" not in result.stdout + result.stderr
    assert not (tmp_path / "report.json").exists()


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), "Real semantic runner is missing"
    output = tmp_path / "report.json"
    output.write_text("existing evidence")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--provider-key-file",
            str(tmp_path / "absent"),
            "--output",
            str(output),
        ],
        env={"PATH": ""},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "output_exists" in result.stderr
    assert output.read_text() == "existing evidence"


def test_help_requires_explicit_key_and_exposes_no_fake_success_mode() -> None:
    assert SCRIPT.is_file(), "Real semantic runner is missing"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--provider-key-file" in result.stdout
    assert "--output" in result.stdout
    assert "--fake" not in result.stdout


def test_real_api_plumbing_with_controlled_index_cannot_claim_semantic_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.projection.memory import MemoryIndex

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "semantic_live_plumbing", SCRIPT.parent / "semantic_memory_live.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ControlledIndex(MemoryIndex):
        failed = False
        closed = False
        _graphiti: Any = SimpleNamespace(
            llm_client=SimpleNamespace(model="controlled", small_model="controlled"),
            embedder=None,
        )

        def close(self) -> None:
            self.closed = True

    index = ControlledIndex()
    monkeypatch.setattr(module, "BoundedIndex", lambda port: index)
    corpus = SCRIPT.parent.parent / "tests/fixtures/memory_quality/everyday-v1.json"
    (tmp_path / "corpus.json").write_bytes(corpus.read_bytes())
    report = asyncio.run(module.evaluate(tmp_path, 1, 30, "synthetic-plumbing"))
    assert index.closed
    assert report["complete"] is True
    assert report["catalogue_verified"] is True
    assert all(report["correction_history_checks"])
    assert len(report["scenarios"]) == 8
    assert report["semantic_evidence"] is False
    assert report["quality_expectations_met"] is False
    assert report["retrieval_mode"] == "controlled-index-not-semantic"
    assert not (tmp_path / "result.json").exists()
