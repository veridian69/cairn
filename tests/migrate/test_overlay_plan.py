"""Curation rulings overlay tests.

The overlay turns a full plan plus a rulings file into a curated plan. The
property the cutover rests on is identity: a later full plan mapped under
the same rulings must reproduce every curated operation byte for byte, so
apply resumes the staged records from their receipts.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cairn_migrate.__main__ import _CHECKOUT_ROOT, main
from cairn_migrate.apply import read_plan
from cairn_migrate.mapping import (
    ENUMERATIONS_FILENAME,
    OPERATIONS_FILENAME,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    RECONCILIATIONS_FILENAME,
    REJECTIONS_FILENAME,
    MappingError,
    canonical_json,
    idempotency_key,
)
from cairn_migrate.overlay import (
    OVERLAY_PLAN_PROFILE,
    RULINGS_SCHEMA_VERSION,
    OverlayError,
    OverlaySummary,
    write_overlay_plan,
)

NAMESPACE = "c4961664-0ded-4ade-aa38-69214bad2678"


def _graph(legacy_id: str, body: str = "") -> dict[str, object]:
    return {
        "operation": "ingest",
        "store": "graph-episode",
        "legacy_id": legacy_id,
        "idempotency_key": idempotency_key("graph-episode", legacy_id),
        "request": {"facts": [{"body": body or f"episode {legacy_id}"}]},
    }


def _conversation(legacy_id: str) -> dict[str, object]:
    return {
        "operation": "ingest",
        "store": "attic-conversation",
        "legacy_id": legacy_id,
        "idempotency_key": idempotency_key("attic-conversation", legacy_id),
        "request": {"facts": [{"body": f"conversation {legacy_id}"}]},
    }


def _turn(
    legacy_id: str,
    conversation_id: str = "conv-1",
    observed_at: str = "2026-09-20T10:00:00.000000Z",
    content_sha256: str = "d" * 64,
) -> dict[str, object]:
    return {
        "operation": "ingest",
        "store": "attic-turn",
        "legacy_id": legacy_id,
        "idempotency_key": idempotency_key("attic-turn", legacy_id),
        "request": {
            "facts": [{"body": f"raw turn {legacy_id}"}],
            "evidence_payload": canonical_json({"content": f"raw {legacy_id}"}),
            "metadata": {
                "conversation_id": conversation_id,
                "content_sha256": content_sha256,
            },
            "observed_at": observed_at,
        },
    }


def _write_plan(
    root: Path,
    label: str,
    operations: list[dict[str, object]],
    bundle_sha256: str = "a" * 64,
) -> Path:
    plan = root / f"plan-{label}"
    plan.mkdir(parents=True)
    raw = b"".join(canonical_json(op).encode("utf-8") + b"\n" for op in operations)
    enumerations = b"{}\n"
    (plan / OPERATIONS_FILENAME).write_bytes(raw)
    (plan / REJECTIONS_FILENAME).write_bytes(b"")
    (plan / RECONCILIATIONS_FILENAME).write_bytes(b"")
    (plan / ENUMERATIONS_FILENAME).write_bytes(enumerations)

    def entry(name: str, content: bytes) -> dict[str, object]:
        return {
            "filename": name,
            "record_count": content.count(b"\n"),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    manifest = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_bundle": {"label": label, "manifest_sha256": bundle_sha256},
        "target": {"realm": "cairn", "segments": [], "classification": "internal"},
        "idempotency_namespace": NAMESPACE,
        "enumerations": {
            "filename": ENUMERATIONS_FILENAME,
            "bytes": len(enumerations),
            "sha256": hashlib.sha256(enumerations).hexdigest(),
        },
        "files": [
            entry(OPERATIONS_FILENAME, raw),
            entry(REJECTIONS_FILENAME, b""),
            entry(RECONCILIATIONS_FILENAME, b""),
        ],
    }
    (plan / PLAN_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return plan


def _ruling(
    legacy_id: str,
    ruling: str,
    body: str | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "store": "attic-turn",
        "legacy_id": legacy_id,
        "proposal_id": f"BL-{legacy_id}",
        "ruling": ruling,
        "body": body,
        "source": source,
    }


SCREENED_SOURCE = {
    "conversation_id": "conv-2",
    "created_at": "2026-09-21T09:00:00.000000Z",
    "content_sha256": "c" * 64,
}

RULINGS = [
    _ruling("turn-accept", "ACCEPT", "Operator prefers one question at a time."),
    _ruling("turn-rewrite", "ACCEPT-AS-REWRITE", "Rewritten by Operator."),
    _ruling("turn-archive", "ARCHIVE"),
    _ruling("turn-merged", "MERGED"),
    _ruling(
        "turn-screened", "ACCEPT-AS-REWRITE", "From a screened turn.", SCREENED_SOURCE
    ),
]


def _write_rulings(root: Path, rulings: list[dict[str, object]] = RULINGS) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "rulings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": RULINGS_SCHEMA_VERSION,
                "label": "backlog-test",
                "approved_by": "Operator",
                "approved_on": "2026-10-11",
                "rulings": rulings,
            }
        )
    )
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_sha(plan: Path) -> str:
    return _sha(plan / PLAN_MANIFEST_FILENAME)


ONE_OCTOBER = [
    _graph("ep-old-1"),
    _graph("ep-old-2"),
    _graph("ep-new-1"),
    _conversation("conv-1"),
    _turn("turn-accept"),
    _turn("turn-rewrite"),
    _turn("turn-archive"),
    _turn("turn-merged"),
    _turn("turn-unruled"),
]
BASE = [_graph("curated-fact:C001", "baseline fact"), _graph("curated-fact:C002")]


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Path]:
    covered = _write_plan(tmp_path, "covered", [_graph("ep-old-1"), _graph("ep-old-2")])
    return {
        "source": _write_plan(tmp_path, "source", ONE_OCTOBER),
        "base": _write_plan(tmp_path, "base", BASE),
        "covered": covered,
        "rulings": _write_rulings(tmp_path),
    }


def _overlay(inputs: dict[str, Path], output: Path, **overrides: Any) -> OverlaySummary:
    arguments: dict[str, Any] = {
        "source_plan": inputs["source"],
        "expected_source_manifest_sha256": _manifest_sha(inputs["source"]),
        "base_plan": inputs["base"],
        "expected_base_manifest_sha256": _manifest_sha(inputs["base"]),
        "covered_plan": inputs["covered"],
        "expected_covered_manifest_sha256": _manifest_sha(inputs["covered"]),
        "rulings_path": inputs["rulings"],
        "expected_rulings_sha256": _sha(inputs["rulings"]),
        "output_path": output,
        "checkout_root": output.parent / "repo",
    }
    arguments.update(overrides)
    return write_overlay_plan(**arguments)


def _lines(plan: Path) -> list[bytes]:
    return (plan / OPERATIONS_FILENAME).read_bytes().splitlines(keepends=True)


def _by_identity(plan: Path) -> dict[tuple[str, str], dict[str, object]]:
    return {
        (record["store"], record["legacy_id"]): record
        for record in (json.loads(line) for line in _lines(plan))
    }


def test_overlay_selects_base_new_episodes_and_ruled_turns(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    summary = _overlay(inputs, tmp_path / "out")

    assert summary.plan.name == f"cairn-migration-plan-source-{OVERLAY_PLAN_PROFILE}"
    assert dict(summary.counts) == {
        "base": 2,
        "graph_episode_accepted": 1,
        "graph_episode_covered": 2,
        "attic_turn_accepted": 1,
        "attic_turn_rewritten": 1,
        "attic_turn_archived": 1,
        "attic_turn_merged": 1,
        "attic_turn_derived": 1,
        "attic_turn_unruled_rejected": 1,
        "attic_conversation_unruled_rejected": 1,
    }
    assert list(_by_identity(summary.plan)) == [
        ("graph-episode", "curated-fact:C001"),
        ("graph-episode", "curated-fact:C002"),
        ("graph-episode", "ep-new-1"),
        ("attic-turn", "turn-accept"),
        ("attic-turn", "turn-rewrite"),
        ("attic-turn", "turn-screened"),
    ]


def test_overlay_carries_base_and_new_episodes_byte_verbatim(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lines = _lines(_overlay(inputs, tmp_path / "out").plan)

    base = _lines(inputs["base"])
    assert lines[:2] == base
    assert lines[2] == canonical_json(_graph("ep-new-1")).encode("utf-8") + b"\n"


def test_overlay_rulings_keep_the_turn_identity_and_change_only_the_body(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    records = _by_identity(_overlay(inputs, tmp_path / "out").plan)
    source = {
        (op["store"], op["legacy_id"]): op
        for op in ONE_OCTOBER
        if op["store"] == "attic-turn"
    }

    for legacy_id, body in (
        ("turn-accept", "Operator prefers one question at a time."),
        ("turn-rewrite", "Rewritten by Operator."),
    ):
        record = records[("attic-turn", legacy_id)]
        original = source[("attic-turn", legacy_id)]
        for field in ("operation", "store", "legacy_id", "idempotency_key"):
            assert record[field] == original[field]
        request = record["request"]
        assert isinstance(request, dict)
        assert request["facts"] == [{"body": body}]
        assert request["observed_at"] == "2026-09-20T10:00:00.000000Z"
        assert request["metadata"]["conversation_id"] == "conv-1"
        assert request["metadata"]["curation_proposal"] == f"BL-{legacy_id}"
        evidence = json.loads(request["evidence_payload"])
        original_request = original["request"]
        assert isinstance(original_request, dict)
        assert (
            evidence["source_evidence_payload"] == original_request["evidence_payload"]
        )
        assert evidence["original_turn_payload_included"] is True
        assert evidence["approved_text"] == body


def test_overlay_derives_the_key_for_a_ruled_turn_the_plan_does_not_carry(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    record = _by_identity(_overlay(inputs, tmp_path / "out").plan)[
        ("attic-turn", "turn-screened")
    ]

    assert record["idempotency_key"] == idempotency_key("attic-turn", "turn-screened")
    request = record["request"]
    assert isinstance(request, dict)
    assert request["observed_at"] == SCREENED_SOURCE["created_at"]
    assert request["metadata"]["conversation_id"] == "conv-2"
    evidence = json.loads(request["evidence_payload"])
    assert evidence["source_content_sha256"] == "c" * 64
    assert evidence["original_turn_payload_included"] is False
    assert "source_evidence_payload" not in evidence


def test_overlay_is_byte_deterministic(inputs: dict[str, Path], tmp_path: Path) -> None:
    first = _overlay(inputs, tmp_path / "one").plan
    second = _overlay(inputs, tmp_path / "two").plan

    assert (first / OPERATIONS_FILENAME).read_bytes() == (
        second / OPERATIONS_FILENAME
    ).read_bytes()
    assert (first / PLAN_MANIFEST_FILENAME).read_bytes() == (
        second / PLAN_MANIFEST_FILENAME
    ).read_bytes()


def test_freeze_plan_under_the_same_rulings_reproduces_every_curated_operation(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    g1 = _lines(_overlay(inputs, tmp_path / "g1").plan)
    freeze_source = _write_plan(
        tmp_path,
        "freeze",
        [
            *ONE_OCTOBER,
            _graph("ep-after-freeze"),
            _turn("turn-after-freeze"),
            _conversation("conv-after-freeze"),
        ],
    )

    summary = _overlay(
        {**inputs, "source": freeze_source},
        tmp_path / "freeze-out",
        source_plan=freeze_source,
        expected_source_manifest_sha256=_manifest_sha(freeze_source),
    )
    freeze = _lines(summary.plan)

    assert set(g1) <= set(freeze)
    assert set(freeze) - set(g1) == {
        canonical_json(_graph("ep-after-freeze")).encode("utf-8") + b"\n"
    }
    assert summary.counts["attic_turn_unruled_rejected"] == 2
    assert summary.counts["attic_conversation_unruled_rejected"] == 2


def test_overlay_output_reads_as_an_applyable_plan(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    plan = _overlay(inputs, tmp_path / "out").plan

    operations = read_plan(plan)
    assert len(operations) == 6
    manifest = json.loads((plan / PLAN_MANIFEST_FILENAME).read_text())
    assert manifest["overlay"]["base_plan"]["manifest_sha256"] == _manifest_sha(
        inputs["base"]
    )
    assert manifest["overlay"]["rulings"]["sha256"] == _sha(inputs["rulings"])
    assert manifest["counts"]["graph-episode"]["planned"] == 3
    assert manifest["counts"]["attic-turn"]["planned"] == 3


def test_overlay_refuses_a_key_not_derived_from_the_identity(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    # Plan reading accepts any canonical UUID; the overlay must not.
    forged = _turn("turn-rewrite")
    forged["idempotency_key"] = idempotency_key("attic-turn", "something-else")
    source = _write_plan(
        tmp_path,
        "forged",
        [op for op in ONE_OCTOBER if op["legacy_id"] != "turn-rewrite"] + [forged],
    )

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            source_plan=source,
            expected_source_manifest_sha256=_manifest_sha(source),
        )
    assert error.value.code == "overlay_identity_mismatch"


def test_overlay_counts_an_archived_turn_the_plan_does_not_carry(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    source = _write_plan(
        tmp_path,
        "screened-archive",
        [op for op in ONE_OCTOBER if op["legacy_id"] != "turn-archive"],
    )

    summary = _overlay(
        inputs,
        tmp_path / "out",
        source_plan=source,
        expected_source_manifest_sha256=_manifest_sha(source),
    )
    assert summary.counts["attic_turn_archived"] == 1
    assert ("attic-turn", "turn-archive") not in _by_identity(summary.plan)


def test_overlay_refuses_a_ruled_turn_missing_from_the_plan(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    source = _write_plan(
        tmp_path,
        "missing",
        [op for op in ONE_OCTOBER if op["legacy_id"] != "turn-accept"],
    )

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            source_plan=source,
            expected_source_manifest_sha256=_manifest_sha(source),
        )
    assert error.value.code == "ruled_record_missing"


def test_overlay_keeps_the_pinned_form_when_a_screened_turn_reappears(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1")
    source = _write_plan(
        tmp_path,
        "reappeared",
        [*ONE_OCTOBER, _turn("turn-screened", "conv-2", content_sha256="c" * 64)],
    )

    summary = _overlay(
        inputs,
        tmp_path / "out",
        source_plan=source,
        expected_source_manifest_sha256=_manifest_sha(source),
    )
    assert _lines(summary.plan) == _lines(g1.plan)
    assert summary.counts["attic_turn_derived"] == 1


def test_overlay_refuses_a_reappeared_turn_with_different_content(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    source = _write_plan(
        tmp_path,
        "changed",
        [*ONE_OCTOBER, _turn("turn-screened", "conv-2", content_sha256="e" * 64)],
    )

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            source_plan=source,
            expected_source_manifest_sha256=_manifest_sha(source),
        )
    assert error.value.code == "rulings_invalid"


def test_prior_plan_pins_staged_lines_and_counts_drift(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1").plan
    drifted = [
        _turn("turn-accept", observed_at="2026-09-20T11:00:00.000000Z")
        if op["legacy_id"] == "turn-accept"
        else op
        for op in ONE_OCTOBER
    ]
    freeze_source = _write_plan(
        tmp_path, "freeze", [*drifted, _graph("ep-after-freeze")]
    )

    summary = _overlay(
        inputs,
        tmp_path / "freeze-out",
        source_plan=freeze_source,
        expected_source_manifest_sha256=_manifest_sha(freeze_source),
        prior_plan=g1,
        expected_prior_manifest_sha256=_manifest_sha(g1),
    )
    freeze = _lines(summary.plan)

    assert freeze[: len(_lines(g1))] == _lines(g1)
    assert freeze[len(_lines(g1)) :] == [
        canonical_json(_graph("ep-after-freeze")).encode("utf-8") + b"\n"
    ]
    assert summary.counts["prior_pinned"] == 6
    assert summary.counts["prior_drifted"] == 1
    assert summary.counts["prior_only"] == 0
    manifest = json.loads((summary.plan / PLAN_MANIFEST_FILENAME).read_text())
    assert manifest["overlay"]["prior_plan"]["manifest_sha256"] == _manifest_sha(g1)


def test_prior_plan_carries_a_staged_episode_the_freeze_plan_lacks(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1").plan
    source = _write_plan(
        tmp_path,
        "deleted",
        [op for op in ONE_OCTOBER if op["legacy_id"] != "ep-new-1"],
    )

    summary = _overlay(
        inputs,
        tmp_path / "out",
        source_plan=source,
        expected_source_manifest_sha256=_manifest_sha(source),
        prior_plan=g1,
        expected_prior_manifest_sha256=_manifest_sha(g1),
    )
    assert _lines(summary.plan) == _lines(g1)
    assert summary.counts["prior_only"] == 1


@pytest.mark.parametrize("legacy_id", ["turn-accept", "turn-rewrite"])
def test_prior_plan_carries_a_staged_turn_the_freeze_plan_lacks(
    inputs: dict[str, Path], tmp_path: Path, legacy_id: str
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1").plan
    source = _write_plan(
        tmp_path,
        "deleted-turn",
        [op for op in ONE_OCTOBER if op["legacy_id"] != legacy_id],
    )

    summary = _overlay(
        inputs,
        tmp_path / "out",
        source_plan=source,
        expected_source_manifest_sha256=_manifest_sha(source),
        prior_plan=g1,
        expected_prior_manifest_sha256=_manifest_sha(g1),
    )
    assert _lines(summary.plan) == _lines(g1)
    assert summary.counts["prior_only"] == 1


@pytest.mark.parametrize("field", ["base_plan", "covered_plan", "rulings"])
def test_prior_plan_with_a_malformed_reference_is_refused(
    inputs: dict[str, Path], tmp_path: Path, field: str
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1").plan
    manifest = json.loads((g1 / PLAN_MANIFEST_FILENAME).read_text())
    manifest["overlay"][field] = None
    (g1 / PLAN_MANIFEST_FILENAME).write_text(json.dumps(manifest))

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            prior_plan=g1,
            expected_prior_manifest_sha256=_manifest_sha(g1),
        )
    assert error.value.code == "prior_plan_mismatch"


def test_covered_plan_from_another_export_is_refused(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    other = _write_plan(
        tmp_path / "other", "covered", [_graph("ep-old-1")], bundle_sha256="b" * 64
    )

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            covered_plan=other,
            expected_covered_manifest_sha256=_manifest_sha(other),
        )
    assert error.value.code == "covered_plan_mismatch"
    assert not (tmp_path / "out").exists()


def test_prior_plan_from_other_rulings_is_refused(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    g1 = _overlay(inputs, tmp_path / "g1").plan
    rulings = _write_rulings(tmp_path / "other", RULINGS[:4])

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            rulings_path=rulings,
            expected_rulings_sha256=_sha(rulings),
            prior_plan=g1,
            expected_prior_manifest_sha256=_manifest_sha(g1),
        )
    assert error.value.code == "prior_plan_mismatch"


def test_overlay_refuses_a_plan_with_carriage_returns(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    base = inputs["base"]
    raw = (base / OPERATIONS_FILENAME).read_bytes().replace(b"\n", b"\r\n")
    (base / OPERATIONS_FILENAME).write_bytes(raw)
    manifest = json.loads((base / PLAN_MANIFEST_FILENAME).read_text())
    manifest["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (base / PLAN_MANIFEST_FILENAME).write_text(json.dumps(manifest))

    with pytest.raises(OverlayError) as error:
        _overlay(inputs, tmp_path / "out")
    assert error.value.code == "overlay_line_invalid"


@pytest.mark.parametrize(
    "ruling",
    [
        _ruling("turn-archive", "ARCHIVE", "a body on an archive"),
        _ruling("turn-accept", "ACCEPT"),
        _ruling("turn-accept", "REJECT"),
        {**_ruling("ep-new-1", "ACCEPT", "body"), "store": "graph-episode"},
        _ruling("turn-merged", "MERGED", None, SCREENED_SOURCE),
    ],
)
def test_overlay_refuses_invalid_rulings(
    inputs: dict[str, Path], tmp_path: Path, ruling: dict[str, object]
) -> None:
    rulings = _write_rulings(tmp_path / "bad", [ruling])

    with pytest.raises(OverlayError) as error:
        _overlay(
            inputs,
            tmp_path / "out",
            rulings_path=rulings,
            expected_rulings_sha256=_sha(rulings),
        )
    assert error.value.code == "rulings_invalid"


def test_overlay_refuses_a_rulings_digest_mismatch(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(OverlayError) as error:
        _overlay(inputs, tmp_path / "out", expected_rulings_sha256="0" * 64)
    assert error.value.code == "rulings_digest_mismatch"


def test_overlay_refuses_a_base_plan_digest_mismatch(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(MappingError) as error:
        _overlay(inputs, tmp_path / "out", expected_base_manifest_sha256="0" * 64)
    assert error.value.code == "source_plan_digest_mismatch"


def test_overlay_command_writes_the_plan_and_counts(
    inputs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "overlay",
            "--source-plan",
            str(inputs["source"]),
            "--expected-source-manifest-sha256",
            _manifest_sha(inputs["source"]),
            "--base-plan",
            str(inputs["base"]),
            "--expected-base-manifest-sha256",
            _manifest_sha(inputs["base"]),
            "--covered-plan",
            str(inputs["covered"]),
            "--expected-covered-manifest-sha256",
            _manifest_sha(inputs["covered"]),
            "--rulings",
            str(inputs["rulings"]),
            "--expected-rulings-sha256",
            _sha(inputs["rulings"]),
            "--output",
            str(tmp_path / "cli"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["profile"] == OVERLAY_PLAN_PROFILE
    assert payload["counts"]["attic_turn_derived"] == 1
    assert (Path(payload["plan"]) / OPERATIONS_FILENAME).is_file()


def test_overlay_refuses_output_inside_checkout(inputs: dict[str, Path]) -> None:
    with pytest.raises(MappingError) as error:
        _overlay(
            inputs,
            _CHECKOUT_ROOT / "never-written",
            checkout_root=_CHECKOUT_ROOT,
        )
    assert error.value.code == "output_inside_checkout"
    assert not (_CHECKOUT_ROOT / "never-written").exists()


def test_overlay_command_refuses_a_prior_plan_without_its_digest(
    inputs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "overlay",
            "--source-plan",
            str(inputs["source"]),
            "--expected-source-manifest-sha256",
            _manifest_sha(inputs["source"]),
            "--base-plan",
            str(inputs["base"]),
            "--expected-base-manifest-sha256",
            _manifest_sha(inputs["base"]),
            "--covered-plan",
            str(inputs["covered"]),
            "--expected-covered-manifest-sha256",
            _manifest_sha(inputs["covered"]),
            "--rulings",
            str(inputs["rulings"]),
            "--expected-rulings-sha256",
            _sha(inputs["rulings"]),
            "--prior-plan",
            str(inputs["base"]),
            "--output",
            str(tmp_path / "cli"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "arguments_invalid"
    assert not (tmp_path / "cli").exists()
