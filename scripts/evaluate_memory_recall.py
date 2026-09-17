"""Reproducible synthetic recall-quality evaluation against Cairn's composed app.

Run: uv run --locked python scripts/evaluate_memory_recall.py

Creates and removes a temporary catalogue and synthetic credentials. No server,
productive configuration, provider, semantic stub or network transport is used.
JSON goes to stdout. Exit zero means the evaluation ran, NOT that quality passed;
inspect quality_expectations_met and failed_expectations. Unexpected API/catalogue
failures exit nonzero. Timings describe this local run only. Both baseline and
relevant_only modes run against separate fresh catalogues with identical fixture
content, recording times, queries and byte budgets. Both remain lexical-only.

The inline fixture is intentionally small and auditable: 24 newer same-scope
distractors compete with six target facts. Unique controlled recording times
avoid UUID tie-breaking. Recall@3 measures distinct relevant facts within the
first three returned positions, not whether an unbounded packet contains them.
Rank is one-based over the full returned packet; reciprocal rank is cut off at
k. Empty relevance has undefined (null) recall/RR, and clutter counts remain
meaningful. Duplicate results occupy rank positions but cannot inflate recall.
"""

import asyncio
import hashlib
import json
import platform
import re
import secrets
import subprocess
from collections.abc import Sequence, Set
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any
from uuid import uuid4

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from cairn.bootstrap.procedures import bootstrap_realm
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.verification import verify_catalogue
from cairn.runtime.composition import build_application
from cairn.runtime.config import AtticConfig, CairnConfig, HttpConfig, PathConfig

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, 12, tzinfo=UTC)
SCOPE = {
    "realm": "recall-evaluation",
    "segments": [
        {"kind": "job", "identifier": "synthetic-quality"},
        {"kind": "run", "identifier": "isolated-evaluation"},
    ],
}
K = 3
DISTRACTORS = 24


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def measure(returned: Sequence[str], relevant: Set[str], *, k: int) -> dict[str, Any]:
    """Calculate metrics independently of Cairn's relevance scores."""
    if type(k) is not int or k <= 0:
        raise ValueError("k must be a positive integer")
    first = next((i for i, value in enumerate(returned, 1) if value in relevant), None)
    return {
        "k": k,
        "relevant_count": len(relevant),
        "returned_count": len(returned),
        "recall_at_k": len(set(returned[:k]) & relevant) / len(relevant)
        if relevant
        else None,
        "first_relevant_rank": first,
        "reciprocal_rank_at_k": (1 / first if first is not None and first <= k else 0)
        if relevant
        else None,
        "irrelevant_at_k": sum(value not in relevant for value in returned[:k]),
        "irrelevant_returned": sum(value not in relevant for value in returned),
        "duplicate_returned": len(returned) - len(set(returned)),
    }


def source_digest() -> str:
    """Fingerprint executed repository source, including concurrent uncommitted edits."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/cairn").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def record_size(record: dict[str, Any]) -> int:
    return len(
        json.dumps(
            record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )


def source_metadata(digest: str) -> dict[str, Any]:
    return {
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "cairn_python_sha256": digest,
        "evaluation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "unchanged_during_run": True,
    }


async def evaluate_mode(root: Path, *, relevant_only: bool) -> dict[str, Any]:
    """Evaluate one mode on a fresh catalogue, retaining raw measured outcomes."""
    started = perf_counter()
    before = source_metadata(source_digest())
    clock = Clock()
    data, credentials = root / "data", root / "credentials"
    data.mkdir(parents=True)
    credentials.mkdir()
    config = CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=uuid4(),
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
        attic=AtticConfig(enabled=False),
    )
    if config.graphiti.enabled:
        raise RuntimeError("This evaluation requires the semantic index to be disabled")
    migrate_catalogue(config, clock)
    bootstrap = bootstrap_realm(
        config,
        realm_id=str(SCOPE["realm"]),
        label="synthetic-evaluation-operator",
        clock=clock,
        uuid_factory=uuid4,
        entropy=secrets.token_bytes,
    )
    app = build_application(config, clock=clock)
    facts: dict[str, dict[str, str]] = {}
    labels: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    recall_ms: list[float] = []
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(LifespanManager(app))

        async def connect(token: str) -> AsyncClient:
            return await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://127.0.0.1:8000",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )

        async def call(
            http: AsyncClient, path: str, body: dict[str, Any], *, mutation: bool = True
        ) -> dict[str, Any]:
            response = await http.post(
                path,
                json=body,
                headers={"Idempotency-Key": str(uuid4())} if mutation else {},
            )
            document = response.json()
            if response.status_code != 200 or "failure" in document:
                code = document.get("failure", {}).get("code", "unexpected_response")
                raise RuntimeError(
                    f"Synthetic {path} failed: {response.status_code} {code}"
                )
            result = document["result"] if mutation else document
            if not isinstance(result, dict):
                raise RuntimeError("Synthetic operation returned a non-object result")
            return result

        admin = await connect(bootstrap.token)
        principal = await call(
            admin,
            "/v1/create-principal",
            {
                "realm_id": SCOPE["realm"],
                "kind": "workload",
                "label": "synthetic-recall-worker",
            },
        )
        credential = await call(
            admin,
            "/v1/issue-credential",
            {
                "realm_id": SCOPE["realm"],
                "principal_id": principal["principal_id"],
                "expires_at": "2028-01-01T00:00:00.000000Z",
            },
        )
        await call(
            admin,
            "/v1/create-grant",
            {
                "realm_id": SCOPE["realm"],
                "grant": {
                    "realm_id": SCOPE["realm"],
                    "principal_id": principal["principal_id"],
                    "segments": SCOPE["segments"],
                    "operations": ["ingest", "retrieve"],
                    "read_clearance": "internal",
                    "write_classifications": ["internal"],
                    "expires_at": "2028-01-01T00:00:00.000000Z",
                },
            },
        )
        worker = await connect(credential["plaintext"])

        async def remember(label: str, body: str) -> None:
            result = await call(
                worker,
                "/memory/v1/remember",
                {
                    "scope": SCOPE,
                    "classification": "internal",
                    "facts": [{"body": body}],
                },
            )
            identity = result["fact_ids"][0]
            facts[label] = {
                "fact_id": identity,
                "body": body,
                "recorded_at": clock.now.isoformat(),
            }
            labels[identity] = label
            clock.now += timedelta(seconds=1)

        for label, body in (
            ("old_calibration", "Status amber calibration baseline."),
            ("vehicle_schedule", "Vehicle maintenance occurs fortnightly."),
            ("port_original", "Quartz collector listens on port 7000."),
            ("port_corrected", "Quartz collector listens on port 7001."),
            ("retention_short", "Archive retention lasts seven days."),
            ("retention_long", "Archive retention lasts thirty days."),
        ):
            await remember(label, body)
        await call(
            worker,
            "/memory/v1/disagree",
            {
                "scope": SCOPE,
                "classification": "internal",
                "left_fact_id": facts["retention_short"]["fact_id"],
                "right_fact_id": facts["retention_long"]["fact_id"],
                "reason": "Synthetic accounts disagree about archive retention duration.",
            },
        )
        clock.now += timedelta(days=100)
        for number in range(DISTRACTORS):
            await remember(
                f"distractor_{number:02d}",
                f"Status maintenance rota entry {number:02d}: inspect cupboard hinges.",
            )

        async def recall(
            name: str, query: str, relevant: set[str], *, budget: int = 65536
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            tick = perf_counter()
            packet = await call(
                worker,
                "/memory/v1/recall",
                {
                    "scope": SCOPE,
                    "query": query,
                    "budget": budget,
                    "relevant_only": relevant_only,
                },
                mutation=False,
            )
            recall_ms.append((perf_counter() - tick) * 1000)
            returned = [labels[hit["fact_id"]] for hit in packet["hits"]]
            row: dict[str, Any] = {
                "name": name,
                "mode": "relevant_only" if relevant_only else "baseline",
                "relevant_only": relevant_only,
                "query": query,
                "at": clock.now.isoformat(),
                "relevant_labels": sorted(relevant),
                "returned_labels": returned,
                "metrics": measure(returned, relevant, k=K),
                "budget_requested": budget,
                "budget_consumed": packet["budget_consumed"],
                "budget_exhausted": packet["budget_exhausted"],
                "disagreement_count": len(packet["disagreements"]),
                "resolution_count": len(packet["resolutions"]),
                "scope_matches": all(hit["scope"] == SCOPE for hit in packet["hits"]),
                "candidate_only": all(
                    hit["trust"] == "candidate" for hit in packet["hits"]
                ),
                "policy": packet["policy"],
                "semantic_degraded": packet["semantic_degraded"],
                "expectations": {},
            }
            disclosed_bytes = sum(
                record_size(record)
                for field in ("hits", "disagreements", "resolutions")
                for record in packet[field]
            )
            if disclosed_bytes != packet["budget_consumed"] or disclosed_bytes > budget:
                raise RuntimeError(
                    "Recall packet violated its disclosed-record byte budget"
                )
            rows.append(row)
            return row, packet

        row, _ = await recall(
            "literal", "vehicle maintenance fortnightly", {"vehicle_schedule"}
        )
        row["expectations"]["relevant_ranked_first"] = (
            row["metrics"]["first_relevant_rank"] == 1
        )

        row, _ = await recall(
            "specific_literal", "vehicle fortnightly", {"vehicle_schedule"}
        )
        row["expectations"]["relevant_ranked_first"] = (
            row["metrics"]["first_relevant_rank"] == 1
        )

        row, _ = await recall(
            "paraphrase_no_overlap",
            "automobile servicing cadence",
            {"vehicle_schedule"},
        )
        row["literal_overlap"] = sorted(
            set(re.findall(r"\w+", row["query"].casefold()))
            & set(re.findall(r"\w+", facts["vehicle_schedule"]["body"].casefold()))
        )
        row["expectations"]["relevant_in_top_k"] = row["metrics"]["recall_at_k"] == 1

        row, _ = await recall("unrelated_query", "xylophonic nebula", set())
        row["expectations"]["no_irrelevant_results"] = (
            row["metrics"]["irrelevant_returned"] == 0
        )

        disputed = {"retention_short", "retention_long"}
        row, packet = await recall(
            "contradiction_context", "archive retention duration", disputed
        )
        endpoints = [
            hit for hit in packet["hits"] if labels[hit["fact_id"]] in disputed
        ]
        row["expectations"] = {
            "both_accounts_in_top_k": row["metrics"]["recall_at_k"] == 1,
            "unresolved_context_disclosed": len(packet["disagreements"]) == 1
            and not packet["resolutions"],
            "both_accounts_flagged": len(endpoints) == 2
            and all(
                hit["has_disagreement"] and not hit["disagreement_context_incomplete"]
                for hit in endpoints
            ),
        }
        # Reserve exactly the two facts as the real budgeter first sees them;
        # no bytes remain for their disagreement record or unrelated facts.
        # A complete retrieval miss must remain a quality result, not cause a
        # zero-budget request that the API correctly rejects as invalid.
        tight_budget = max(
            1,
            sum(
                record_size(
                    {
                        **hit,
                        "has_disagreement": False,
                        "disagreement_context_incomplete": False,
                    }
                )
                for hit in endpoints
            ),
        )
        row, packet = await recall(
            "contradiction_limited_context",
            "archive retention duration",
            disputed,
            budget=tight_budget,
        )
        row["expectations"] = {
            "both_accounts_in_top_k": row["metrics"]["recall_at_k"] == 1,
            "omission_explicit": len(packet["hits"]) == 2
            and not packet["disagreements"]
            and packet["budget_exhausted"]
            and all(
                hit["has_disagreement"] and hit["disagreement_context_incomplete"]
                for hit in packet["hits"]
            ),
        }

        await call(
            admin,
            "/memory/v1/correct",
            {
                "fact_ids": [facts["port_original"]["fact_id"]],
                "superseded_by": facts["port_corrected"]["fact_id"],
                "reason": "Synthetic operator measurement corrects the port number.",
            },
        )
        row, _ = await recall(
            "correction_history", "Quartz collector port", {"port_corrected"}
        )
        history = await call(
            worker,
            "/memory/v1/history",
            {
                "scope": SCOPE,
                "fact_id": facts["port_corrected"]["fact_id"],
                "budget": 16384,
            },
            mutation=False,
        )
        historical = {labels[hit["fact_id"]]: hit for hit in history["facts"]}
        row["history_labels"] = [label for label in facts if label in historical]
        row["history_correction_count"] = len(history["corrections"])
        row["expectations"] = {
            "current_in_top_k": row["metrics"]["recall_at_k"] == 1,
            "superseded_absent_from_recall": "port_original"
            not in row["returned_labels"],
            "both_versions_recoverable": set(historical)
            == {"port_original", "port_corrected"},
            "correction_link_preserved": any(
                item["fact_id"] == facts["port_original"]["fact_id"]
                and item["superseded_by"] == facts["port_corrected"]["fact_id"]
                and item["reason"]
                == "Synthetic operator measurement corrects the port number."
                for item in history["corrections"]
            ),
            "invalidation_visible": historical.get("port_original", {}).get(
                "invalidated_at"
            )
            is not None
            and historical.get("port_corrected", {}).get("invalidated_at") is None,
        }

        row, _ = await recall("fading", "status", {"old_calibration"}, budget=1000)
        row["expectations"] = {
            "newer_status_preferred": row["returned_labels"] == ["distractor_23"],
            "old_fact_outside_small_packet": "old_calibration"
            not in row["returned_labels"],
        }
        row, packet = await recall(
            "resurfacing", "amber calibration", {"old_calibration"}, budget=1000
        )
        row["expectations"] = {
            "old_fact_returns_on_strong_cue": row["returned_labels"]
            == ["old_calibration"],
            "recording_time_preserved": len(packet["hits"]) == 1
            and datetime.fromisoformat(packet["hits"][0]["recorded_at"]) == START,
            "trust_unchanged": row["candidate_only"],
        }

    verification = verify_catalogue(config)
    if verification.fact_count != len(facts) or verification.invalidation_count != 1:
        raise RuntimeError("Synthetic catalogue inventory did not match the fixture")
    after = source_metadata(source_digest())
    if before != after:
        raise RuntimeError(
            "Source or environment changed during evaluation; evidence is not stable"
        )
    failed = [
        f"{row['mode']}.{row['name']}.{name}"
        for row in rows
        for name, passed in row["expectations"].items()
        if not passed
    ]
    return {
        "schema": "cairn-recall-quality/v2",
        "synthetic": True,
        "retrieval_mode": "lexical-only",
        "semantic_evidence": False,
        "provider_calls": 0,
        "scope": SCOPE,
        "same_scope_distractors": DISTRACTORS,
        "fixture": {
            label: {key: value for key, value in fact.items() if key != "fact_id"}
            for label, fact in facts.items()
        },
        "controlled_clock": {"start": START.isoformat(), "end": clock.now.isoformat()},
        "source_snapshot": before,
        "catalogue_verified": True,
        "scenarios": rows,
        "quality_expectations_met": not failed,
        "failed_expectations": failed,
        "limitations": [
            "Both modes are lexical-only: no semantic index, controlled-index stub or provider was evaluated.",
            "relevant_only excludes recency-only matches; shared-word distractors remain, and no-overlap paraphrases still miss.",
            "semantic_degraded reports index failure; false does not establish semantic retrieval.",
            "Synthetic fixture expectations are quality goals, not all existing implementation guarantees.",
            "Recorded disagreement is explicit input; this does not test automatic contradiction detection.",
            "Fading measures competition under a byte budget, not deletion or learning from repeated recall.",
            "Temporary catalogue UUIDs and credentials vary; labelled quality results and controlled timestamps do not.",
        ],
        "timings": {
            "label": "descriptive local timing; not SLA or semantic benchmark",
            "recall_ms": recall_ms,
            "total_ms": (perf_counter() - started) * 1000,
        },
    }


async def evaluate(root: Path) -> dict[str, Any]:
    """Compare modes without letting one catalogue's writes affect the other."""
    started = perf_counter()
    baseline = await evaluate_mode(root / "baseline", relevant_only=False)
    filtered = await evaluate_mode(root / "relevant-only", relevant_only=True)
    if baseline["source_snapshot"] != filtered["source_snapshot"]:
        raise RuntimeError("Evaluation source or environment changed between modes")
    if (
        baseline["fixture"] != filtered["fixture"]
        or baseline["controlled_clock"] != filtered["controlled_clock"]
    ):
        raise RuntimeError("Mode comparison did not use identical fixtures and clocks")
    modes = [
        {
            "name": name,
            "relevant_only": enabled,
            "quality_expectations_met": result["quality_expectations_met"],
            "failed_expectations": result["failed_expectations"],
        }
        for name, enabled, result in (
            ("baseline", False, baseline),
            ("relevant_only", True, filtered),
        )
    ]
    return {
        **baseline,
        "evaluation_modes": modes,
        "scenarios": baseline["scenarios"] + filtered["scenarios"],
        "quality_expectations_met": all(
            mode["quality_expectations_met"] for mode in modes
        ),
        "failed_expectations": baseline["failed_expectations"]
        + filtered["failed_expectations"],
        "timings": {
            "label": baseline["timings"]["label"],
            "recall_ms": baseline["timings"]["recall_ms"]
            + filtered["timings"]["recall_ms"],
            "total_ms": (perf_counter() - started) * 1000,
        },
    }


def main() -> None:
    with TemporaryDirectory(prefix="cairn-recall-evaluation-") as temporary:
        report = asyncio.run(evaluate(Path(temporary)))
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
