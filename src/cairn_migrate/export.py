"""P-73: the ``cairn-legacy-export/v1`` bundle.

One JSONL file per store, one canonical-JSON record per line, and a
manifest carrying the snapshot identity, the source paths, the restore
container's image digest, and per-file record counts and SHA-256
digests.

Determinism is the property everything downstream rests on: ``map`` is
pure, so a byte-identical bundle gives a byte-identical migration plan,
which gives idempotency keys that replay instead of duplicating. Each
reader therefore emits in a fixed order and each line is canonical
JSON — sorted keys, no insignificant whitespace, UTF-8 kept as UTF-8
rather than escaped, so the digests describe the records and not
Python's formatting defaults.

**This is a new persistence format**, and P-73 marks it a standing
review trigger. Say so when Task 1 lands.

The bundle contains legacy record bodies. It is written into Operator's
private directory outside the checkout (P-72) and nothing derived from
it enters this repository except counts, digests and identities (P-78).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from cairn_migrate.snapshot import (
    EntityEdgeCounts,
    GraphQuery,
    count_entity_edges,
    count_entity_nodes,
    read_attic_conversations,
    read_attic_turns,
    read_graph_episodes,
    read_journal_events,
    read_openbrain_rows,
    read_thought_files,
)

EXPORT_SCHEMA_VERSION = "cairn-legacy-export/v1"
MANIFEST_FILENAME = "manifest.json"
_SNAPSHOT_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


class ExportError(Exception):
    """Closed vocabulary of export refusal codes.

    Codes: ``output_unavailable``, ``output_inside_checkout``,
    ``bundle_exists``, ``bundle_incomplete``, ``snapshot_label_invalid``,
    ``record_not_canonical_json``.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"export error: {code}")


@dataclass(frozen=True, slots=True)
class SnapshotSet:
    """One snapshot set, as Operator produced and transferred it (P-72).

    ``graph_dump``, ``restore_image`` and ``restore_image_digest`` are
    recorded rather than read: the tool reads the *restored* graph over
    the loopback port, so the identity of the dump and of the container
    that restored it would otherwise be lost from the evidence.
    """

    label: str
    graph_dump: str
    restore_image: str
    restore_image_digest: str
    attic_path: Path
    journal_root: Path
    thoughts_root: Path
    openbrain_path: Path


@dataclass(frozen=True, slots=True)
class ExportedStore:
    store: str
    filename: str
    record_count: int
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExportResult:
    bundle_path: Path
    stores: tuple[ExportedStore, ...]
    entity_nodes: int
    entity_edges: EntityEdgeCounts


def snapshot_label_is_valid(label: str) -> bool:
    return _SNAPSHOT_LABEL_PATTERN.fullmatch(label) is not None


def write_export_bundle(
    snapshot: SnapshotSet,
    query: GraphQuery,
    output_path: Path,
) -> ExportResult:
    """Read every store into the bundle, then write the manifest last.

    The manifest is written last on purpose: a bundle whose manifest is
    present is a bundle whose stores were all read, so a run interrupted
    part-way cannot be mistaken for a complete export.
    """
    resolved_output = output_path.resolve()
    if resolved_output == _CHECKOUT_ROOT or _CHECKOUT_ROOT in resolved_output.parents:
        raise ExportError("output_inside_checkout")
    bundle_path = _create_bundle_directory(resolved_output, snapshot.label)
    stores = tuple(
        _write_store(bundle_path, store, records)
        for store, records in _store_streams(snapshot, query)
    )
    entity_nodes = count_entity_nodes(query)
    entity_edges = count_entity_edges(query)
    _write_manifest(
        bundle_path,
        snapshot=snapshot,
        stores=stores,
        entity_nodes=entity_nodes,
        entity_edges=entity_edges,
    )
    return ExportResult(
        bundle_path=bundle_path,
        stores=stores,
        entity_nodes=entity_nodes,
        entity_edges=entity_edges,
    )


def _store_streams(
    snapshot: SnapshotSet,
    query: GraphQuery,
) -> Iterator[tuple[str, Iterable[dict[str, object]]]]:
    """The P-73 store list, in a fixed order.

    Entity nodes and edges are absent by design: P-75 counts them into
    the manifest and migrates none of them, because they are Graphiti's
    extraction products and v0.1 re-derives an equivalent layer itself.
    """
    yield "graph-episode", read_graph_episodes(query)
    yield "attic-conversation", read_attic_conversations(snapshot.attic_path)
    yield "attic-turn", read_attic_turns(snapshot.attic_path)
    yield "journal-event", read_journal_events(snapshot.journal_root)
    yield "thought-file", read_thought_files(snapshot.thoughts_root)
    yield "openbrain-row", read_openbrain_rows(snapshot.openbrain_path)


def _write_store(
    bundle_path: Path,
    store: str,
    records: Iterable[dict[str, object]],
) -> ExportedStore:
    filename = f"{store}.jsonl"
    digest = hashlib.sha256()
    record_count = 0
    byte_count = 0
    try:
        with (bundle_path / filename).open("wb") as target:
            for record in records:
                try:
                    line = (
                        json.dumps(
                            record,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                        + "\n"
                    )
                    encoded = line.encode()
                except (TypeError, ValueError) as error:
                    raise ExportError("record_not_canonical_json") from error
                target.write(encoded)
                digest.update(encoded)
                record_count += 1
                byte_count += len(encoded)
    except OSError as error:
        raise ExportError("output_unavailable") from error
    return ExportedStore(
        store=store,
        filename=filename,
        record_count=record_count,
        byte_count=byte_count,
        sha256=digest.hexdigest(),
    )


def _create_bundle_directory(
    output_path: Path,
    label: str,
) -> Path:
    if not snapshot_label_is_valid(label):
        raise ExportError("snapshot_label_invalid")
    bundle_path = output_path / f"cairn-legacy-export-{label}"
    try:
        bundle_path.mkdir(parents=True)
    except FileExistsError as error:
        if bundle_path.is_dir() and not (bundle_path / MANIFEST_FILENAME).exists():
            raise ExportError("bundle_incomplete") from error
        raise ExportError("bundle_exists") from error
    except OSError as error:
        raise ExportError("output_unavailable") from error
    return bundle_path


def _write_manifest(
    bundle_path: Path,
    *,
    snapshot: SnapshotSet,
    stores: tuple[ExportedStore, ...],
    entity_nodes: int,
    entity_edges: EntityEdgeCounts,
) -> None:
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "snapshot": {
            "label": snapshot.label,
            "graph_dump": snapshot.graph_dump,
            "restore_image": snapshot.restore_image,
            "restore_image_digest": snapshot.restore_image_digest,
            "attic_path": str(snapshot.attic_path),
            "journal_root": str(snapshot.journal_root),
            "thoughts_root": str(snapshot.thoughts_root),
            "openbrain_path": str(snapshot.openbrain_path),
        },
        "stores": [
            {
                "store": store.store,
                "filename": store.filename,
                "record_count": store.record_count,
                "bytes": store.byte_count,
                "sha256": store.sha256,
            }
            for store in stores
        ],
        "derived_counts": {
            "entity_node": entity_nodes,
            "entity_edge": entity_edges.total,
            "entity_edge_invalidated": entity_edges.invalidated,
            "entity_edge_expired": entity_edges.expired,
        },
    }
    manifest = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    try:
        (bundle_path / MANIFEST_FILENAME).write_bytes(manifest)
    except OSError as error:
        raise ExportError("output_unavailable") from error
