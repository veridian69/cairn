"""Build a privacy-safe, deterministic migration mapping report.

The report contains counts, digests, rule tallies, store names and legacy
identifiers. It never contains a record body, title, actor name, source value
or content excerpt. Raw actor and source enumerations remain beside the plan;
the report exposes only their tallies and file digest.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from cairn_migrate.mapping import (
    PLAN_INPUT_STORES,
    REJECTION_RULES,
    ExportBundle,
    MappingError,
    MappingResult,
    canonical_json,
    enumerations_bytes,
    plan_counts,
)

REPORT_SCHEMA_VERSION = "cairn-migration-report/v1"
REPORT_FILENAME = "report.json"

#: Recommended floor for a sampled projection run: enough assertions to
#: exercise retrieval comparison across all stores.
STAGE_B_SAMPLE_FLOOR = 200


def build_report(result: MappingResult, bundle: ExportBundle) -> dict[str, object]:
    counts = plan_counts(result)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source_bundle": {
            "label": bundle.label,
            "manifest_sha256": bundle.manifest_sha256,
        },
        # No target block: the ruled scope path lives in the private plan
        # manifest, and the global P-78/I-32 constraint keeps scope paths
        # out of repository-bound reports (review finding S3).
        "counts": counts,
        "balance": _balance(counts),
        "rejections": _rejection_tallies(result),
        "reconciliations": _reconciliation_tallies(result),
        "trust": {
            "validated": result.validated_count,
            "candidate": len(result.operations) - result.validated_count,
        },
        "enumerations": _enumeration_tallies(result),
        "derived_graph_counts": _derived_counts(bundle),
        "stage_b_estimate": _stage_b_estimate(result),
    }


def _enumeration_tallies(result: MappingResult) -> dict[str, object]:
    """Reduce protected enumerations to tallies and a file digest."""
    tallies: dict[str, object] = {
        field: {
            store: {
                "distinct": len(values),
                "observed": sum(values.values()),
            }
            for store, values in stores.items()
        }
        for field, stores in result.enumerations.items()
    }
    tallies["file_sha256"] = hashlib.sha256(enumerations_bytes(result)).hexdigest()
    return tallies


def _balance(counts: Mapping[str, Mapping[str, int]]) -> dict[str, object]:
    """The zero-silent-drops assertion, computed rather than asserted in
    prose.

    Every exported record occupies exactly one column — planned or
    rejected for an assertion store's records, matched, reconciled or
    rejected for an enrichment store's (a foreign-group journal event is a
    §5.8 rejection, re-review finding R2) — so one identity covers every
    store: exported = planned + rejected + matched + reconciled.
    ``balanced`` is false if any store fails it, which is the single field
    a reader of the evidence record has to look at to know the corpus is
    fully accounted for.
    """
    stores: dict[str, object] = {}
    balanced = True
    for store in PLAN_INPUT_STORES:
        entry = counts[store]
        accounted = (
            entry["planned"]
            + entry["rejected"]
            + entry["matched"]
            + entry["reconciled"]
        )
        store_balanced = accounted == entry["exported"]
        balanced = balanced and store_balanced
        stores[store] = {
            "exported": entry["exported"],
            "accounted": accounted,
            "balanced": store_balanced,
        }
    return {"balanced": balanced, "stores": stores}


def _rejection_tallies(result: MappingResult) -> dict[str, object]:
    """Every rule in the closed vocabulary, including the ones that fired
    zero times.

    A rule missing from the report and a rule that caught nothing look
    identical to a reader otherwise, and only one of those is good news.
    """
    by_rule = {
        rule: sum(1 for item in result.rejections if item.rule == rule)
        for rule in REJECTION_RULES
    }
    by_store = {
        store: sum(1 for item in result.rejections if item.store == store)
        for store in PLAN_INPUT_STORES
    }
    secret_rules: dict[str, int] = {}
    for item in result.rejections:
        if item.detail is not None:
            secret_rules[item.detail] = secret_rules.get(item.detail, 0) + 1
    return {
        "total": len(result.rejections),
        "by_rule": by_rule,
        "by_store": by_store,
        "secret_screen_rules": dict(sorted(secret_rules.items())),
    }


def _reconciliation_tallies(result: MappingResult) -> dict[str, object]:
    """§5.7's ruling list, by store and by rule, plus the identities.

    The identities are here rather than only in the plan because P-78 admits
    legacy identifiers and an operator must inspect the actual records. A
    record named here can be found in the protected bundle; no content travels
    with the identity.
    """
    by_store = {
        store: sum(1 for item in result.reconciliations if item.store == store)
        for store in PLAN_INPUT_STORES
    }
    return {
        "total": len(result.reconciliations),
        "by_store": by_store,
        "identities": [
            {"store": item.store, "legacy_id": item.legacy_id, "rule": item.rule}
            for item in result.reconciliations
        ],
    }


def _derived_counts(bundle: ExportBundle) -> dict[str, object]:
    """P-75's evidence: the derived layer was inventoried and deliberately
    not migrated.

    ``entity_edge_invalidated`` and ``entity_edge_expired`` are the numbers
    the operator's carry-over ruling turns on — production never ran the manual
    invalidation tool, so those are Graphiti's own contradiction markers, and
    any carry-over is an explicit post-migration ``/v1/invalidate`` operation
    rather than a default.
    """
    derived = bundle.manifest.get("derived_counts")
    counts = derived if isinstance(derived, dict) else {}
    return {
        "migrated": 0,
        "entity_node": counts.get("entity_node"),
        "entity_edge": counts.get("entity_edge"),
        "entity_edge_invalidated": counts.get("entity_edge_invalidated"),
        "entity_edge_expired": counts.get("entity_edge_expired"),
    }


def _stage_b_estimate(result: MappingResult) -> dict[str, object]:
    """P-77's Stage B input.

    Projection calls ``add_episode`` once per fact, so the corpus size in
    facts and in body bytes is what drives extraction cost. Both are
    reported as measured counts. **No monetary figure is produced here**,
    per P-77 as amended (the operator's ruling, 21 August 2026): the price depends
    on the provider, the model and the day, none of which this repository
    knows, so the report supplies the units and the operator prices Stage B himself
    in Task 4.
    """
    return {
        "planned_assertions": len(result.operations),
        "planned_facts": len(result.operations),
        "fact_body_bytes": result.body_bytes,
        "sample_floor": STAGE_B_SAMPLE_FLOOR,
        "full_corpus_meets_floor": len(result.operations) >= STAGE_B_SAMPLE_FLOOR,
    }


def write_report(
    result: MappingResult,
    bundle: ExportBundle,
    plan_path: Path,
) -> Path:
    """Write ``report.json`` beside the plan.

    Beside it, not inside a second directory, because the two are produced
    by one command and the report's digests describe that plan. Task 4 copies
    this file into the evidence record; the plan itself never leaves the
    private directory.
    """
    report_path = plan_path / REPORT_FILENAME
    payload = build_report(result, bundle)
    try:
        report_path.write_bytes(
            (
                json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")
        )
    except OSError as error:
        raise MappingError("output_unavailable", detail=REPORT_FILENAME) from error
    return report_path


def report_digest(payload: Mapping[str, object]) -> str:
    """The report's own canonical digest, for an evidence record to quote."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
