"""Slice 9 Task 2: the privacy-safe mapping report (P-74, P-75, P-77, P-78).

The report is the one map-stage artefact that leaves protected storage
and lands in this repository, so the claim that matters most here is
negative: no fixture body text reaches it. It contains the reconciliation
list, derived-relationship counts and Stage B sizing data.

Synthetic fixtures only (P-78), shared with ``test_mapping.py`` by import.
Module-local, because ``tests`` has no package markers.
"""

import hashlib
import json
from pathlib import Path

from test_mapping import (
    AGENT_ACTOR,
    CONTENT_MARKERS,
    NAMED_ACTOR,
    SECRET_BODY,
    default_records,
    rejecting_records,
    write_bundle,
)

from cairn_migrate.mapping import (
    REJECTION_RULES,
    enumerations_bytes,
    map_bundle,
    read_export_bundle,
    write_plan,
)
from cairn_migrate.report import (
    REPORT_FILENAME,
    REPORT_SCHEMA_VERSION,
    STAGE_B_SAMPLE_FLOOR,
    build_report,
    report_digest,
    write_report,
)


def report_for(
    tmp_path: Path,
    records: dict[str, list[dict[str, object]]] | None = None,
) -> dict[str, object]:
    bundle = read_export_bundle(
        write_bundle(tmp_path, records if records is not None else default_records())
    )
    return build_report(map_bundle(bundle), bundle)


# --- the privacy boundary -----------------------------------------------------


def test_no_fixture_content_reaches_the_report(tmp_path: Path) -> None:
    """P-78, asserted by scanning the serialised report rather than by
    inspecting the fields one at a time.

    A field-by-field check proves only that the fields a reader thought of
    are clean. The scan proves the artefact is, including anything a later
    change adds to it.
    """
    serialised = json.dumps(report_for(tmp_path, rejecting_records()), sort_keys=True)

    for marker in (*CONTENT_MARKERS, SECRET_BODY):
        assert marker not in serialised, f"{marker} leaked into the report"
    for value in (AGENT_ACTOR, NAMED_ACTOR):
        assert f'"{value}"' not in serialised, f"{value} leaked into the report"


def test_the_written_report_file_is_equally_clean(tmp_path: Path) -> None:
    bundle = read_export_bundle(write_bundle(tmp_path / "in", rejecting_records()))
    result = map_bundle(bundle)
    plan = write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")
    report_path = write_report(result, bundle, plan)

    assert report_path.name == REPORT_FILENAME
    text = report_path.read_text(encoding="utf-8")
    for marker in (*CONTENT_MARKERS, SECRET_BODY):
        assert marker not in text, f"{marker} leaked into {REPORT_FILENAME}"
    assert json.loads(text)["schema_version"] == REPORT_SCHEMA_VERSION


def test_the_report_is_deterministic(tmp_path: Path) -> None:
    """Same bundle, same report, byte for byte — the digest an evidence
    record quotes has to mean something."""
    first = report_for(tmp_path / "one", rejecting_records())
    second = report_for(tmp_path / "two", rejecting_records())

    assert report_digest(first) == report_digest(second)


# --- review figures -----------------------------------------------------------


def test_the_report_tallies_the_enumerations_and_pins_the_private_file(
    tmp_path: Path,
) -> None:
    """The report carries tallies and the protected enumeration file's digest,
    never raw actor or source values."""
    bundle = read_export_bundle(write_bundle(tmp_path, default_records()))
    result = map_bundle(bundle)
    report = build_report(result, bundle)

    enumerations = report["enumerations"]
    assert isinstance(enumerations, dict)
    assert enumerations["actor"]["journal-event"] == {"distinct": 2, "observed": 3}
    # The episode fixtures carry no actor= marker, so the absent case is
    # what this store observes — counted, not skipped.
    assert enumerations["actor"]["graph-episode"] == {"distinct": 1, "observed": 3}
    assert enumerations["source"]["graph-episode"] == {"distinct": 1, "observed": 3}
    assert enumerations["source"]["journal-event"] == {"distinct": 1, "observed": 3}
    assert enumerations["source"]["attic-conversation"] == {
        "distinct": 1,
        "observed": 1,
    }
    # Re-review finding R3: thought-file frontmatter carries an inventoried
    # ``source`` (gate record §4.4) and is part of the enumeration matrix.
    assert enumerations["source"]["thought-file"] == {"distinct": 1, "observed": 1}
    assert (
        enumerations["file_sha256"]
        == hashlib.sha256(enumerations_bytes(result)).hexdigest()
    )


def test_the_report_contains_no_actor_trust_policy(tmp_path: Path) -> None:
    """Actor strings are provenance only, so the report pins no allow-list."""
    report = report_for(tmp_path)
    assert not [key for key in report if "actor_set" in key]


def test_the_report_carries_no_scope_path(tmp_path: Path) -> None:
    """Review finding S3: the global P-78/I-32 constraint keeps scope paths
    out of repository-bound reports. The ruled target lives in the private
    plan manifest instead."""
    report = report_for(tmp_path)

    assert "target" not in report
    serialised = json.dumps(report, sort_keys=True)
    assert "realm" not in serialised
    assert "segments" not in serialised


def test_the_report_carries_the_derived_graph_counts_and_migrates_none(
    tmp_path: Path,
) -> None:
    """P-75: the derived layer was inventoried and deliberately not
    migrated. ``invalid_at`` and ``expired_at`` are the numbers any
    carry-over ruling turns on."""
    report = report_for(tmp_path)

    derived = report["derived_graph_counts"]
    assert isinstance(derived, dict)
    assert derived["migrated"] == 0
    assert derived["entity_node"] == 12
    assert derived["entity_edge"] == 34
    assert derived["entity_edge_invalidated"] == 5
    assert derived["entity_edge_expired"] == 2


def test_the_report_lists_every_reconciliation_identity(tmp_path: Path) -> None:
    """§5.7 names records an operator can look up in the protected bundle."""
    report = report_for(tmp_path)

    reconciliations = report["reconciliations"]
    assert isinstance(reconciliations, dict)
    identities = reconciliations["identities"]
    assert isinstance(identities, list)
    assert reconciliations["total"] == len(identities)
    assert {
        "store": "openbrain-row",
        "legacy_id": "openbrain-orphan-1",
        "rule": "no_graph_counterpart",
    } in identities


def test_the_report_tallies_every_rejection_rule_including_the_quiet_ones(
    tmp_path: Path,
) -> None:
    """A rule missing from the report and a rule that caught nothing look
    identical to a reader otherwise, and only one of those is good news."""
    clean = report_for(tmp_path / "clean")
    dirty = report_for(tmp_path / "dirty", rejecting_records())

    for report in (clean, dirty):
        rejections = report["rejections"]
        assert isinstance(rejections, dict)
        by_rule = rejections["by_rule"]
        assert isinstance(by_rule, dict)
        assert set(by_rule) == set(REJECTION_RULES)
    clean_rejections = clean["rejections"]
    dirty_rejections = dirty["rejections"]
    assert isinstance(clean_rejections, dict)
    assert isinstance(dirty_rejections, dict)
    assert clean_rejections["total"] == 0
    assert dirty_rejections["total"] == len(REJECTION_RULES)
    assert dirty_rejections["secret_screen_rules"]


def test_the_report_counts_trust_by_class(tmp_path: Path) -> None:
    report = report_for(tmp_path)

    trust = report["trust"]
    assert isinstance(trust, dict)
    assert trust["validated"] == 1
    assert trust["candidate"] == 4


def test_the_stage_b_estimate_reports_units_and_invents_no_price(
    tmp_path: Path,
) -> None:
    """P-77: projection calls ``add_episode`` per fact, so facts and body
    bytes are the cost drivers. A monetary figure would depend on the
    provider, the model and the day — none of which this repository knows,
    and a fabricated number in an evidence record is worse than none."""
    report = report_for(tmp_path)

    estimate = report["stage_b_estimate"]
    assert isinstance(estimate, dict)
    assert estimate["planned_assertions"] == 5
    assert estimate["planned_facts"] == 5
    assert estimate["fact_body_bytes"] > 0
    assert estimate["sample_floor"] == STAGE_B_SAMPLE_FLOOR
    assert estimate["full_corpus_meets_floor"] is False
    assert not [key for key in estimate if "cost" in key or "price" in key]


def test_the_report_pins_the_bundle_it_was_computed_from(tmp_path: Path) -> None:
    """Evidence is revision-bound, and a dry-run report is snapshot-bound:
    the label and the manifest digest say which corpus these figures
    describe."""
    bundle = read_export_bundle(write_bundle(tmp_path, default_records()))
    report = build_report(map_bundle(bundle), bundle)

    source = report["source_bundle"]
    assert isinstance(source, dict)
    assert source["label"] == "synthetic"
    assert source["manifest_sha256"] == bundle.manifest_sha256
