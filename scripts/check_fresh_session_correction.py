#!/usr/bin/env python3
"""Check the saved outputs of the fresh-session correction walkthrough.

Usage: ``python scripts/check_fresh_session_correction.py OUTPUT_DIR``.

The walkthrough in ``docs/fresh-session-correction-demo.md`` saves each
``cairn-memory`` result as JSON in OUTPUT_DIR. This check reads only those
files: it makes no network, provider or Cairn call and does not depend on
timing or generated identifiers. It asserts committed custody, a genuinely
new session that finds the claim through Cairn, a recall after correction
that excludes the invalidated belief, and history that retains the old
belief, the correction and the replacement relationship.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OLD_BODY = "This synthetic project uses uv for dependency management."
NEW_BODY = "This synthetic project uses Poetry for dependency management."
OUTPUTS = (
    "a-check",
    "a-remember",
    "b-arrive",
    "b-acknowledge",
    "b-remember",
    "b-correct",
    "b-correct-replay",
    "b-recall",
    "b-history",
)


class CheckFailed(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def load(directory: Path) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for name in OUTPUTS:
        path = directory / f"{name}.json"
        require(path.is_file(), f"missing output {path.name}")
        document = json.loads(path.read_text(encoding="utf-8"))
        require(
            document.get("schema") == "cairn.memory-command/v1",
            f"{path.name} is not a cairn-memory command result",
        )
        documents[name] = document["result"]
    return documents


def committed_fact(remember: dict[str, Any], label: str) -> str:
    persistence = remember["persistence"]
    require(remember["state"] == "committed", f"{label}: turn not committed")
    require(persistence["status"] == "committed", f"{label}: custody not committed")
    require(
        bool(persistence["mutation_receipt"]["mutation_id"])
        and bool(persistence["audit_receipt"]["event_hash"]),
        f"{label}: custody receipt incomplete",
    )
    fact_ids = persistence["result"]["fact_ids"]
    require(len(fact_ids) == 1, f"{label}: expected exactly one saved fact")
    return str(fact_ids[0])


def check(directory: Path) -> list[str]:
    out = load(directory)
    passed: list[str] = []

    status = out["a-check"]
    require(status["status"] == "ready", "session A: connection not ready")
    permissions = status["permissions"]
    require(
        permissions["ingest"] and permissions["retrieve"] and permissions["invalidate"],
        "session A: ingest, retrieve and invalidate grants not all reported",
    )
    principal = status["principal_id"]
    scope = status["scope"]

    a = out["a-remember"]
    old = committed_fact(a, "session A")
    snapshot = a["snapshot"]
    require(snapshot["principal_id"] == principal, "session A: unexpected principal")
    require(snapshot["scope"] == scope, "session A: unexpected scope")
    require(
        [o["body"] for o in a["completed_turn"]["observations"]] == [OLD_BODY],
        "session A: unexpected remembered claim",
    )
    passed.append("custody: session A's claim has a committed custody receipt")

    arrive = out["b-arrive"]
    visit = arrive["visit"]["snapshot"]
    require(visit["session_id"] != a["session_id"], "session B reused session A")
    require(visit["principal_id"] == principal, "session B: unexpected principal")
    require(visit["scope"] == scope, "session B: scope is not the shared scope")
    require(
        "previous_visit_unknown" in arrive["briefing"]["warnings"],
        "session B: briefing does not report a new visit history",
    )
    found = [
        hit
        for hit in arrive["briefing"]["recall"]["data"]["hits"]
        if hit["fact_id"] == old
    ]
    require(len(found) == 1, "session B: arrival recall did not find the claim")
    hit = found[0]
    require(hit["body"] == OLD_BODY, "session B: recalled body differs")
    require(hit["trust"] == "candidate", "session B: claim is not a candidate")
    require(
        hit["source_principal_id"] == principal and hit["source_type"] == "agent-claim",
        "session B: attribution differs from session A's principal",
    )
    require(
        out["b-acknowledge"]["outcome"] in {"committed", "replayed"},
        "session B: visit acknowledgement not confirmed",
    )
    passed.append(
        "fresh session: session B, with a new session ID and the same scope, "
        "recalled session A's candidate claim through Cairn"
    )

    b = out["b-remember"]
    new = committed_fact(b, "session B")
    require(new != old, "replacement reused the old fact ID")
    require(b["session_id"] == visit["session_id"], "replacement not from session B")
    require(
        [o["body"] for o in b["completed_turn"]["observations"]] == [NEW_BODY],
        "session B: unexpected replacement claim",
    )
    correct, replay = out["b-correct"], out["b-correct-replay"]
    require(correct["status"] == "committed", "correction not committed")
    require(correct["result"]["fact_ids"] == [old], "correction targets another fact")
    require(replay["status"] == "replayed", "identical correction was not a replay")
    require(
        replay["mutation_receipt"] == correct["mutation_receipt"]
        and replay["idempotency_key"] == correct["idempotency_key"],
        "replay does not carry the original mutation identity",
    )
    passed.append("correction: committed once, replayed under the same mutation")

    hits = out["b-recall"]["data"]["hits"]
    recalled = {hit["fact_id"] for hit in hits}
    require(old not in recalled, "current recall still returns the invalidated belief")
    require(new in recalled, "current recall does not return the replacement")
    require(
        all(hit["invalidated_at"] is None for hit in hits),
        "current recall returned an invalidated fact",
    )
    passed.append("recall: the invalidated belief is excluded, the replacement found")

    history = out["b-history"]["data"]
    facts = {fact["fact_id"]: fact for fact in history["facts"]}
    require(old in facts and new in facts, "history lacks the old or new belief")
    require(facts[old]["body"] == OLD_BODY, "history changed the old belief")
    require(facts[old]["invalidated_at"] is not None, "old belief not invalidated")
    require(facts[new]["body"] == NEW_BODY, "history changed the replacement")
    require(facts[new]["invalidated_at"] is None, "replacement is invalidated")
    links = [
        item
        for item in history["corrections"]
        if item["fact_id"] == old and item["superseded_by"] == new
    ]
    require(len(links) == 1, "history lacks the old-to-new correction link")
    link = links[0]
    require(bool(link["reason"]), "correction has no reason")
    require(link["principal_id"] == principal, "correction attributed elsewhere")
    require(
        link["invalidated_at"]
        == facts[old]["invalidated_at"]
        == correct["result"]["invalidated_at"],
        "correction time differs between history and receipt",
    )
    passed.append(
        "history: old belief, correction reason and replacement link retained"
    )
    return passed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        passed = check(Path(argv[1]))
    except (CheckFailed, KeyError, TypeError, ValueError) as error:
        detail = error if isinstance(error, CheckFailed) else repr(error)
        print(json.dumps({"status": "failed", "reason": str(detail)}))
        return 1
    print(json.dumps({"status": "passed", "checks": passed}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
