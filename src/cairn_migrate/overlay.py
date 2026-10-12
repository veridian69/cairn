"""Curation rulings overlay: a full plan plus Operator's rulings in, a curated plan out.

Pure and offline, like ``subset``. Every input is pinned by SHA-256 so the
same inputs give a byte-identical plan. The freeze plan overlaid with the
staged plan as its prior reproduces every staged operation byte for byte,
which is what lets ``apply`` skip the staged records on the day by receipt.

Inputs:

- the source plan: the full plan being curated (the 1 October plan for G1,
  the freeze plan in the window);
- the base plan: an already curated plan whose operations are carried
  byte-verbatim (the 1 October curated plan);
- the covered plan: the full plan the base was curated from. Its graph
  episodes are represented by the base, so the same episodes in the source
  plan are dropped and counted, never re-ingested raw;
- the rulings file (``cairn-curation-rulings/v1``), keyed by store and
  legacy id;
- optionally a prior plan (G1, when overlaying the freeze plan) whose lines
  are carried verbatim for every identity it holds.

Graph episodes the covered plan does not hold are accepted unruled. Ruled
Attic turns become curated facts that keep the turn's identity and key;
unruled Attic turns and conversations are rejected and counted.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    parse_timestamp,
)
from cairn_migrate.mapping import (
    ENUMERATIONS_FILENAME,
    OPERATIONS_FILENAME,
    PLAN_INPUT_STORES,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    RECONCILIATIONS_FILENAME,
    REJECTIONS_FILENAME,
    MappingError,
    _create_plan_directory,
    _file_line_count,
    _PlanFile,
    _SourcePlanManifest,
    _SourcePlanOperation,
    _write_jsonl,
    canonical_json,
    idempotency_key,
    read_source_plan,
)

OVERLAY_PLAN_PROFILE = "curated-overlay-v1"
RULINGS_SCHEMA_VERSION = "cairn-curation-rulings/v1"
CURATION_EVIDENCE_FORMAT = "cairn-attic-curation-evidence/1"

OVERLAY_ERROR_CODES = frozenset(
    {
        "rulings_unreadable",
        "rulings_digest_mismatch",
        "rulings_invalid",
        "ruled_record_missing",
        "overlay_identity_mismatch",
        "overlay_line_invalid",
        "prior_plan_mismatch",
        "covered_plan_mismatch",
    }
)

_EMITTING_RULINGS = frozenset({"ACCEPT", "ACCEPT-AS-REWRITE"})
_COUNTED_RULINGS = frozenset({"ARCHIVE", "MERGED"})
_SOURCE_FIELDS = frozenset({"conversation_id", "created_at", "content_sha256"})
_RULING_FIELDS = frozenset(
    {"store", "legacy_id", "proposal_id", "ruling", "body", "source"}
)


class OverlayError(Exception):
    """Closed vocabulary of overlay refusal codes — ``OVERLAY_ERROR_CODES``."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in OVERLAY_ERROR_CODES:
            raise ValueError(f"unknown overlay error code: {code}")
        self.code = code
        self.detail = detail
        suffix = "" if detail is None else f": {detail}"
        super().__init__(f"overlay error: {code}{suffix}")


@dataclass(frozen=True, slots=True)
class _Ruling:
    legacy_id: str
    proposal_id: str
    ruling: str
    body: str | None
    source: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class _Rulings:
    sha256: str
    label: str
    approved_by: str
    approved_on: str
    by_turn: Mapping[str, _Ruling]


@dataclass(frozen=True, slots=True)
class OverlaySummary:
    plan: Path
    counts: Mapping[str, int]


def read_rulings(path: Path, *, expected_sha256: str) -> _Rulings:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise OverlayError("rulings_unreadable") from error
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise OverlayError("rulings_digest_mismatch")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise OverlayError("rulings_invalid", detail="json") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "label",
        "approved_by",
        "approved_on",
        "rulings",
    }:
        raise OverlayError("rulings_invalid", detail="fields")
    if value["schema_version"] != RULINGS_SCHEMA_VERSION:
        raise OverlayError("rulings_invalid", detail="schema_version")
    for field in ("label", "approved_by", "approved_on"):
        if not isinstance(value[field], str) or not value[field]:
            raise OverlayError("rulings_invalid", detail=field)
    if not isinstance(value["rulings"], list):
        raise OverlayError("rulings_invalid", detail="rulings")
    by_turn: dict[str, _Ruling] = {}
    for index, entry in enumerate(value["rulings"]):
        ruling = _ruling(entry, index)
        if ruling.legacy_id in by_turn:
            raise OverlayError("rulings_invalid", detail=f"duplicate {index}")
        by_turn[ruling.legacy_id] = ruling
    return _Rulings(
        sha256=digest,
        label=value["label"],
        approved_by=value["approved_by"],
        approved_on=value["approved_on"],
        by_turn=by_turn,
    )


def _ruling(entry: object, index: int) -> _Ruling:
    detail = f"ruling {index}"
    if not isinstance(entry, dict) or set(entry) != _RULING_FIELDS:
        raise OverlayError("rulings_invalid", detail=detail)
    # Only Attic turns are ruled: graph episodes follow the base and the
    # covered plan, every other store is rejected unruled.
    if entry["store"] != "attic-turn":
        raise OverlayError("rulings_invalid", detail=detail)
    for field in ("legacy_id", "proposal_id", "ruling"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise OverlayError("rulings_invalid", detail=detail)
    ruling = entry["ruling"]
    body = entry["body"]
    if ruling in _EMITTING_RULINGS:
        if not isinstance(body, str) or not body.strip():
            raise OverlayError("rulings_invalid", detail=detail)
    elif ruling in _COUNTED_RULINGS:
        if body is not None:
            raise OverlayError("rulings_invalid", detail=detail)
    else:
        raise OverlayError("rulings_invalid", detail=detail)
    source = entry["source"]
    if source is not None:
        if (
            ruling not in _EMITTING_RULINGS
            or not isinstance(source, dict)
            or set(source) != _SOURCE_FIELDS
            or not all(isinstance(item, str) and item for item in source.values())
        ):
            raise OverlayError("rulings_invalid", detail=detail)
        # created_at becomes the request's observed_at: hold it to the
        # catalogue's timestamp contract here, not at apply. strptime's %f
        # takes one to six digits, so only the canonical rendering passes.
        created_at = source["created_at"]
        try:
            canonical = canonical_timestamp(parse_timestamp(created_at))
        except CatalogueStorageError:
            raise OverlayError("rulings_invalid", detail=detail) from None
        if canonical != created_at:
            raise OverlayError("rulings_invalid", detail=detail)
    return _Ruling(
        legacy_id=entry["legacy_id"],
        proposal_id=entry["proposal_id"],
        ruling=ruling,
        body=body,
        source=cast(Mapping[str, str] | None, source),
    )


def write_overlay_plan(
    *,
    source_plan: Path,
    expected_source_manifest_sha256: str,
    base_plan: Path,
    expected_base_manifest_sha256: str,
    covered_plan: Path,
    expected_covered_manifest_sha256: str,
    rulings_path: Path,
    expected_rulings_sha256: str,
    output_path: Path,
    checkout_root: Path,
    prior_plan: Path | None = None,
    expected_prior_manifest_sha256: str | None = None,
) -> OverlaySummary:
    """Write the curated plan, manifest last.

    With a prior plan (G1, when overlaying the freeze plan), every identity
    the prior plan carries is emitted from it byte for byte and only new
    identities are built fresh, so the prior plan is a subset of the output
    by construction. A fresh rebuild that differs from the prior line is
    counted, not carried: the staged record is what the instance holds.
    """
    resolved = output_path.resolve()
    if resolved == checkout_root or checkout_root in resolved.parents:
        raise MappingError("output_inside_checkout")
    source_operations, source_manifest = _read_plan(
        source_plan, expected_source_manifest_sha256
    )
    base_operations, base_manifest = _read_plan(
        base_plan, expected_base_manifest_sha256
    )
    covered_operations, covered_manifest = _read_plan(
        covered_plan, expected_covered_manifest_sha256
    )
    # A curated plan's manifest records only its export, not the plan it was
    # curated from, so the export is the strongest provenance available: a
    # covered plan from another export would drop graph episodes the base
    # never replaced.
    if base_manifest.source_bundle.get(
        "manifest_sha256"
    ) != covered_manifest.source_bundle.get("manifest_sha256"):
        raise OverlayError("covered_plan_mismatch")
    rulings = read_rulings(rulings_path, expected_sha256=expected_rulings_sha256)
    prior_operations: tuple[_SourcePlanOperation, ...] = ()
    prior_reference: dict[str, object] | None = None
    if prior_plan is not None:
        prior_operations, prior_manifest = _read_plan(
            prior_plan, cast(str, expected_prior_manifest_sha256)
        )
        _check_prior(prior_plan, base_manifest, covered_manifest, rulings)
        prior_reference = _plan_reference(prior_manifest)
    prior_identities = {
        (operation.store, operation.legacy_id) for operation in prior_operations
    }
    covered = {
        operation.legacy_id
        for operation in covered_operations
        if operation.store == "graph-episode"
    }

    counts = {
        "base": len(base_operations),
        "graph_episode_accepted": 0,
        "graph_episode_covered": 0,
        "attic_turn_accepted": 0,
        "attic_turn_rewritten": 0,
        "attic_turn_archived": 0,
        "attic_turn_merged": 0,
        "attic_turn_derived": 0,
        "attic_turn_unruled_rejected": 0,
        "attic_conversation_unruled_rejected": 0,
    }
    fresh: dict[tuple[str, str], bytes] = {}
    for operation in base_operations:
        _check_identity(operation.store, operation.legacy_id, operation.idempotency_key)
        _emit(fresh, operation.store, operation.legacy_id, operation.raw)

    curated: list[tuple[str, bytes]] = []
    seen_turns: set[str] = set()
    for operation in source_operations:
        if operation.store == "graph-episode":
            if operation.legacy_id in covered:
                counts["graph_episode_covered"] += 1
                continue
            _check_identity(
                operation.store, operation.legacy_id, operation.idempotency_key
            )
            _emit(fresh, operation.store, operation.legacy_id, operation.raw)
            counts["graph_episode_accepted"] += 1
        elif operation.store == "attic-turn":
            ruling = rulings.by_turn.get(operation.legacy_id)
            if ruling is None:
                counts["attic_turn_unruled_rejected"] += 1
                continue
            seen_turns.add(operation.legacy_id)
            if ruling.ruling == "ARCHIVE":
                counts["attic_turn_archived"] += 1
                continue
            if ruling.ruling == "MERGED":
                counts["attic_turn_merged"] += 1
                continue
            _check_identity(
                operation.store, operation.legacy_id, operation.idempotency_key
            )
            if ruling.source is not None:
                # The turn was screened out when the rulings were made and
                # passes now. Keep the hash-only form the rulings pinned, as
                # long as the plan describes the same turn.
                _check_source(operation, ruling)
                curated.append((operation.legacy_id, _derived_line(ruling, rulings)))
                counts["attic_turn_derived"] += 1
                continue
            curated.append(
                (operation.legacy_id, _curated_line(operation, ruling, rulings))
            )
            counts[_emitted_count(ruling)] += 1
        else:
            counts["attic_conversation_unruled_rejected"] += 1

    for legacy_id, ruling in rulings.by_turn.items():
        if legacy_id in seen_turns:
            continue
        if ruling.ruling == "ARCHIVE":
            # Nothing to emit, so a turn the plan does not carry (the secret
            # screen rejected it) needs no source block.
            counts["attic_turn_archived"] += 1
            continue
        if ruling.ruling == "MERGED":
            counts["attic_turn_merged"] += 1
            continue
        if ruling.source is None:
            if ("attic-turn", legacy_id) in prior_identities:
                # Staged already; the prior plan carries it (prior_only).
                continue
            # A ruled turn neither the plan nor a prior plan carries, without
            # a source block: either the wrong plan or a turn deleted from
            # legacy before staging. Stop rather than drop a ruling silently.
            raise OverlayError("ruled_record_missing", detail=ruling.proposal_id)
        curated.append((legacy_id, _derived_line(ruling, rulings)))
        counts["attic_turn_derived"] += 1
    for legacy_id, line in curated:
        _emit(fresh, "attic-turn", legacy_id, line)

    if prior_plan is None:
        lines = list(fresh.values())
    else:
        lines = []
        counts["prior_pinned"] = len(prior_operations)
        counts["prior_drifted"] = 0
        counts["prior_only"] = 0
        for operation in prior_operations:
            identity = (operation.store, operation.legacy_id)
            _check_identity(*identity, operation.idempotency_key)
            lines.append(operation.raw)
            rebuilt = fresh.get(identity)
            if rebuilt is None:
                counts["prior_only"] += 1
            elif rebuilt != operation.raw:
                counts["prior_drifted"] += 1
        lines.extend(
            line for identity, line in fresh.items() if identity not in prior_identities
        )

    payload = b"".join(lines)
    plan_path = _create_plan_directory(
        resolved, source_manifest.label, suffix=OVERLAY_PLAN_PROFILE
    )
    try:
        (plan_path / OPERATIONS_FILENAME).write_bytes(payload)
        (plan_path / ENUMERATIONS_FILENAME).write_bytes(source_manifest.enumerations)
    except OSError as error:
        raise MappingError("output_unavailable", detail=OPERATIONS_FILENAME) from error
    files = (
        _PlanFile(
            filename=OPERATIONS_FILENAME,
            record_count=_file_line_count(payload),
            byte_count=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        _write_jsonl(plan_path, REJECTIONS_FILENAME, ()),
        _write_jsonl(plan_path, RECONCILIATIONS_FILENAME, ()),
    )
    _write_overlay_manifest(
        plan_path=plan_path,
        source=source_manifest,
        base=base_manifest,
        covered=covered_manifest,
        prior=prior_reference,
        rulings=rulings,
        counts=counts,
        planned=_planned_by_store(payload),
        files=files,
    )
    return OverlaySummary(plan=plan_path, counts=counts)


def _read_plan(
    plan: Path, expected_manifest_sha256: str
) -> tuple[tuple[_SourcePlanOperation, ...], _SourcePlanManifest]:
    operations, manifest = read_source_plan(
        plan, expected_source_manifest_sha256=expected_manifest_sha256
    )
    # The reader splits lines on CR as well as LF and re-terminates them
    # with LF, so a CR would make "byte-verbatim" carriage untrue. Plan
    # writers never emit one; refuse rather than normalise.
    if b"\r" in (plan / OPERATIONS_FILENAME).read_bytes():
        raise OverlayError("overlay_line_invalid", detail=str(plan))
    return operations, manifest


def _check_source(operation: _SourcePlanOperation, ruling: _Ruling) -> None:
    source = cast(Mapping[str, str], ruling.source)
    metadata = operation.request.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("content_sha256") != source["content_sha256"]
        or operation.conversation_id != source["conversation_id"]
    ):
        raise OverlayError("rulings_invalid", detail=ruling.proposal_id)


def _field(value: Mapping[str, object], key: str, field: str) -> object:
    reference = value.get(key)
    return reference.get(field) if isinstance(reference, dict) else None


def _check_prior(
    prior_plan: Path,
    base: _SourcePlanManifest,
    covered: _SourcePlanManifest,
    rulings: _Rulings,
) -> None:
    # Pinning lines from a prior plan is only sound if it was produced from
    # the same base, covered plan and rulings.
    manifest = json.loads((prior_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    overlay = manifest.get("overlay") if isinstance(manifest, dict) else None
    if (
        not isinstance(overlay, dict)
        or overlay.get("profile") != OVERLAY_PLAN_PROFILE
        or _field(overlay, "base_plan", "manifest_sha256") != base.manifest_sha256
        or _field(overlay, "covered_plan", "manifest_sha256") != covered.manifest_sha256
        or _field(overlay, "rulings", "sha256") != rulings.sha256
    ):
        raise OverlayError("prior_plan_mismatch")


def _emitted_count(ruling: _Ruling) -> str:
    if ruling.ruling == "ACCEPT":
        return "attic_turn_accepted"
    return "attic_turn_rewritten"


def _check_identity(store: str, legacy_id: str, key: str) -> None:
    # Plan reading accepts any canonical UUID as a key. Receipts match on
    # identity and key, so a key not derived from the identity would make
    # the freeze-day apply raise receipt_conflict instead of resuming.
    if key != idempotency_key(store, legacy_id):
        raise OverlayError("overlay_identity_mismatch", detail=f"{store}:{legacy_id}")


def _emit(
    emitted: dict[tuple[str, str], bytes], store: str, legacy_id: str, line: bytes
) -> None:
    if (store, legacy_id) in emitted:
        raise OverlayError("overlay_identity_mismatch", detail=f"{store}:{legacy_id}")
    emitted[(store, legacy_id)] = line


def _curated_line(
    operation: _SourcePlanOperation, ruling: _Ruling, rulings: _Rulings
) -> bytes:
    request = operation.request
    evidence = _evidence(ruling, rulings)
    evidence["source_evidence_payload"] = request.get("evidence_payload")
    evidence["original_turn_payload_included"] = True
    observed_at = request.get("observed_at")
    return _line(
        legacy_id=operation.legacy_id,
        key=operation.idempotency_key,
        ruling=ruling,
        rulings=rulings,
        conversation_id=cast(str, operation.conversation_id),
        observed_at=observed_at if isinstance(observed_at, str) else None,
        evidence=evidence,
    )


def _derived_line(ruling: _Ruling, rulings: _Rulings) -> bytes:
    # The plan holds no operation for this turn (the secret screen rejected
    # it), so the key is derived exactly as the mapper would have derived
    # it, and the evidence carries the content hash, never the turn itself.
    source = cast(Mapping[str, str], ruling.source)
    evidence = _evidence(ruling, rulings)
    evidence["source_content_sha256"] = source["content_sha256"]
    evidence["original_turn_payload_included"] = False
    return _line(
        legacy_id=ruling.legacy_id,
        key=idempotency_key("attic-turn", ruling.legacy_id),
        ruling=ruling,
        rulings=rulings,
        conversation_id=source["conversation_id"],
        observed_at=source["created_at"],
        evidence=evidence,
    )


def _evidence(ruling: _Ruling, rulings: _Rulings) -> dict[str, object]:
    return {
        "format": CURATION_EVIDENCE_FORMAT,
        "curation_label": rulings.label,
        "proposal_id": ruling.proposal_id,
        "ruling": ruling.ruling,
        "approved_by": rulings.approved_by,
        "approved_on": rulings.approved_on,
        "approved_text": ruling.body,
        "rulings_sha256": rulings.sha256,
        "source_store": "attic-turn",
        "source_legacy_id": ruling.legacy_id,
        "verification_caveat": (
            "Curated from an Attic turn under the named ruling. Source claims "
            "remain candidate trust; remembered approvals do not authorise "
            "future actions."
        ),
    }


def _line(
    *,
    legacy_id: str,
    key: str,
    ruling: _Ruling,
    rulings: _Rulings,
    conversation_id: str,
    observed_at: str | None,
    evidence: Mapping[str, object],
) -> bytes:
    request: dict[str, object] = {
        "scope": {"realm": "cairn", "segments": []},
        "classification": "internal",
        "source_type": "agent-claim",
        "requested_trust": "candidate",
        "facts": [{"body": ruling.body}],
        "metadata": {
            "curation_label": rulings.label,
            "curation_proposal": ruling.proposal_id,
            "curation_ruling": ruling.ruling,
            "legacy_store": "attic-turn",
            "legacy_id": f"attic-turn:{legacy_id}",
            "conversation_id": conversation_id,
            "historical_source": True,
        },
        "evidence_payload": json.dumps(
            evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ),
    }
    if observed_at is not None:
        request["observed_at"] = observed_at
    line = {
        "operation": "ingest",
        "store": "attic-turn",
        "legacy_id": legacy_id,
        "idempotency_key": key,
        "request": request,
    }
    return (canonical_json(line) + "\n").encode("utf-8")


def _planned_by_store(payload: bytes) -> dict[str, int]:
    planned: dict[str, int] = {}
    for encoded in payload.splitlines():
        store = json.loads(encoded)["store"]
        planned[store] = planned.get(store, 0) + 1
    return planned


def _plan_reference(manifest: _SourcePlanManifest) -> dict[str, object]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "label": manifest.label,
        "manifest_sha256": manifest.manifest_sha256,
    }


def _write_overlay_manifest(
    *,
    plan_path: Path,
    source: _SourcePlanManifest,
    base: _SourcePlanManifest,
    covered: _SourcePlanManifest,
    prior: Mapping[str, object] | None,
    rulings: _Rulings,
    counts: Mapping[str, int],
    planned: Mapping[str, int],
    files: tuple[_PlanFile, ...],
) -> None:
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_bundle": source.source_bundle,
        "source_plan": _plan_reference(source),
        "target": source.target,
        "idempotency_namespace": source.idempotency_namespace,
        "overlay": {
            "profile": OVERLAY_PLAN_PROFILE,
            "base_plan": _plan_reference(base),
            "covered_plan": _plan_reference(covered),
            "prior_plan": prior,
            "rulings": {
                "schema_version": RULINGS_SCHEMA_VERSION,
                "label": rulings.label,
                "sha256": rulings.sha256,
            },
            "counts": dict(counts),
        },
        "enumerations": {
            "filename": ENUMERATIONS_FILENAME,
            "bytes": len(source.enumerations),
            "sha256": hashlib.sha256(source.enumerations).hexdigest(),
        },
        "files": [
            {
                "filename": entry.filename,
                "record_count": entry.record_count,
                "bytes": entry.byte_count,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
        "counts": {
            store: {
                "exported": planned.get(store, 0),
                "planned": planned.get(store, 0),
                "rejected": 0,
                "matched": 0,
                "reconciled": 0,
            }
            for store in PLAN_INPUT_STORES
        },
    }
    manifest = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        (plan_path / PLAN_MANIFEST_FILENAME).write_bytes(manifest)
    except OSError as error:
        raise MappingError(
            "output_unavailable", detail=PLAN_MANIFEST_FILENAME
        ) from error
