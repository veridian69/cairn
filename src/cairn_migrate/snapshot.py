"""P-72: the offline snapshot readers.

Each reader turns one legacy store into a stream of canonical records
for the ``cairn-legacy-export/v1`` bundle (P-73). Every record carries
``store`` and ``legacy_id``, because those two fields are what the P-74
idempotency key is derived from downstream — ``uuid5(namespace,
f"{store}:{legacy_id}")``.

Timestamps are normalised here rather than at mapping time: the bundle
is the artefact Operator's dry-run report is computed from, so it must
already be in the I-28 form the contract uses everywhere else.

One rule decides record shape, because the stores differ in how well
their schemas are known. A **closed** schema — the graph's ``RETURN``
list, Attic's ``schema.sql`` columns, an openbrain row's fixed
``thoughts`` columns — is lifted field by field. An **open** one — a
thought file's JSON frontmatter, a journal event's JSON object — is
carried beside its lifted identity, with documented timestamps
normalised in both shapes. A field legacy grew after the gate's §4
inventory therefore survives to the dry-run instead of being silently
discarded by a reader written against a stale list.

Nothing in this module contacts the VPS. The graph is read from a
disposable container restored from Operator's snapshot; the remaining stores
are read as files.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cairn.catalogue.sqlite import canonical_timestamp

_FRONTMATTER_FENCE = "---\n"
_JOURNAL_TIMESTAMP_FIELDS = (
    "received_at",
    "accepted_at",
    "submission_accepted_at",
    "confirmed_at",
    "next_retry_at",
)

# The three Cypher reads, pinned as constants because the query text is
# the whole contract with the restored FalkorDB: no deterministic test
# can prove it against a real graph, so it is stated once, in the open,
# for the Task 4 dry-run to confirm. Episode embeddings are carried as
# part of the gate's §4.1 inventory. Entity-node and edge embeddings are
# not selected because P-75 counts those derived stores rather than
# exporting them.
EPISODE_QUERY = (
    "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content, e.source, "
    "e.source_description, e.group_id, e.created_at, e.valid_at, e.embedding "
    "ORDER BY e.uuid"
)
ENTITY_NODE_COUNT_QUERY = "MATCH (n:Entity) RETURN count(n)"
ENTITY_EDGE_COUNT_QUERY = (
    "MATCH ()-[r:RELATES_TO]->() "
    "RETURN count(r), count(r.invalid_at), count(r.expired_at)"
)

#: A read against the restored graph: Cypher in, result rows out.
GraphQuery = Callable[[str], list[list[object]]]


class SnapshotError(Exception):
    """Closed vocabulary of snapshot-reading refusal codes.

    Codes: ``snapshot_unreadable``, ``record_unparseable``,
    ``timestamp_malformed``.
    """

    def __init__(self, code: str, *, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"snapshot error: {code}: {detail}")


def normalise_timestamp(value: object, *, detail: str) -> str:
    """Legacy timestamps in I-28 form: six fractional digits, ``Z``.

    Legacy writes both ``2026-04-21 09:15:00.000000+02:00`` and
    ``2026-06-14T21:47:39.123456+00:00``; both are ISO-8601 and both
    parse. A value with no offset is read as UTC — legacy ran one
    UTC-configured stack, and inventing a local zone here would silently
    move records in time.
    """
    if not isinstance(value, str):
        raise SnapshotError("timestamp_malformed", detail=detail)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SnapshotError("timestamp_malformed", detail=detail) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return canonical_timestamp(parsed)


def read_openbrain_rows(path: Path) -> Iterator[dict[str, object]]:
    """``openbrain-dump.jsonl``: one exported ``thoughts`` row per line.

    The line number is carried because it is the only ordering the file
    itself has, and a byte-identical re-export depends on preserving it.
    """
    text = _read_utf8(path, detail=str(path))
    seen: set[str] = set()
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        where = f"{path}:{line_number}"
        try:
            record = json.loads(line)
        except ValueError as error:
            raise SnapshotError("record_unparseable", detail=where) from error
        if not isinstance(record, dict) or not isinstance(record.get("row"), dict):
            raise SnapshotError("record_unparseable", detail=where)
        row = record["row"]
        identity = _unique_identity(row.get("id"), seen=seen, detail=where)
        yield {
            "store": "openbrain-row",
            "legacy_id": identity,
            "line_number": line_number,
            "table": record.get("_table"),
            "exported_at": normalise_timestamp(
                record.get("_exported_at"), detail=where
            ),
            "had_embedding": record.get("_had_embedding"),
            "content": row.get("content"),
            "content_fingerprint": row.get("content_fingerprint"),
            "created_at": normalise_timestamp(row.get("created_at"), detail=where),
            "updated_at": normalise_timestamp(row.get("updated_at"), detail=where),
            "metadata": row.get("metadata"),
        }


def read_thought_files(root: Path) -> Iterator[dict[str, object]]:
    """``thoughts/**.md``: JSON frontmatter between ``---`` fences, then
    the body.

    ``openbrain_id`` is the approved identity input under §5.5/§5.6. The
    relative path remains provenance; a missing or duplicate identity
    stops export rather than inventing a permanent idempotency input.

    A file with no readable frontmatter stops the export rather than
    yielding a headless record. Legacy wrote every one of these files
    programmatically, so this is drift worth failing loudly on, and a
    loud stop is not a silent drop.
    """
    _require_directory(root)
    seen: set[str] = set()
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        text = _read_utf8(path, detail=relative)
        frontmatter, body = _split_thought(text, detail=relative)
        identity = _unique_identity(
            frontmatter.get("openbrain_id"),
            seen=seen,
            detail=relative,
        )
        captured_at = normalise_timestamp(
            frontmatter.get("captured_at"),
            detail=relative,
        )
        normalised_frontmatter = {**frontmatter, "captured_at": captured_at}
        yield {
            "store": "thought-file",
            "legacy_id": identity,
            "path": relative,
            "captured_at": captured_at,
            "frontmatter": normalised_frontmatter,
            "body": body,
        }


def _unique_identity(value: object, *, seen: set[str], detail: str) -> str:
    if not isinstance(value, str) or not value or value in seen:
        raise SnapshotError("record_unparseable", detail=detail)
    seen.add(value)
    return value


def _read_utf8(path: Path, *, detail: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SnapshotError("snapshot_unreadable", detail=detail) from error


def _require_directory(root: Path) -> None:
    if not root.is_dir():
        raise SnapshotError("snapshot_unreadable", detail=str(root))


def _split_thought(text: str, *, detail: str) -> tuple[dict[str, object], str]:
    parts = text.split(_FRONTMATTER_FENCE, 2)
    if len(parts) != 3 or parts[0] != "":
        raise SnapshotError("record_unparseable", detail=detail)
    try:
        frontmatter = json.loads(parts[1])
    except ValueError as error:
        raise SnapshotError("record_unparseable", detail=detail) from error
    if not isinstance(frontmatter, dict):
        raise SnapshotError("record_unparseable", detail=detail)
    return frontmatter, parts[2]


def read_attic_conversations(path: Path) -> Iterator[dict[str, object]]:
    """``transcript_conversations``, ordered by identity.

    ``started_at`` and ``ended_at`` are nullable in the legacy schema and
    stay null here; ``created_at`` and ``updated_at`` default to
    SQLite's ``CURRENT_TIMESTAMP``, which is UTC and offsetless — the
    case ``normalise_timestamp`` reads as UTC.
    """
    seen: set[str] = set()
    with _attic_connection(path) as connection:
        rows = connection.execute(
            "SELECT id, source, title, started_at, ended_at, metadata_json, "
            "created_at, updated_at FROM transcript_conversations ORDER BY id"
        )
        for identity, source, title, started, ended, metadata, created, updated in rows:
            where = f"attic-conversation:{identity}"
            identity = _unique_identity(identity, seen=seen, detail=where)
            yield {
                "store": "attic-conversation",
                "legacy_id": identity,
                "source": source,
                "title": title,
                "started_at": _optional_timestamp(started, detail=where),
                "ended_at": _optional_timestamp(ended, detail=where),
                "created_at": normalise_timestamp(created, detail=where),
                "updated_at": normalise_timestamp(updated, detail=where),
                "metadata": _json_column(metadata, detail=where),
            }


def read_attic_turns(path: Path) -> Iterator[dict[str, object]]:
    """``transcript_turns``, ordered by conversation then turn index.

    ``rowid`` is deliberately not carried: it is SQLite's own insertion
    counter, it backs the FTS index rather than the record, and a
    restored snapshot need not reproduce it. ``id`` is the legacy
    identity — a content hash over the turn, per the schema's own note —
    and ``content_sha256`` comes with it because Task 5 compares digests
    and the store already holds one.
    """
    seen: set[str] = set()
    with _attic_connection(path) as connection:
        rows = connection.execute(
            "SELECT id, conversation_id, turn_index, role, content, created_at, "
            "episode_id, content_sha256, metadata_json FROM transcript_turns "
            "ORDER BY conversation_id, turn_index, id"
        )
        for (
            identity,
            conversation,
            index,
            role,
            content,
            created,
            episode,
            digest,
            metadata,
        ) in rows:
            where = f"attic-turn:{identity}"
            identity = _unique_identity(identity, seen=seen, detail=where)
            yield {
                "store": "attic-turn",
                "legacy_id": identity,
                "conversation_id": conversation,
                "turn_index": index,
                "role": role,
                "content": content,
                "created_at": _optional_timestamp(created, detail=where),
                "episode_id": episode,
                "content_sha256": digest,
                "metadata": _json_column(metadata, detail=where),
            }


@contextmanager
def _attic_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Read-only, symlink-refusing, exactly as ``cairn backup`` opens a
    member: the snapshot is evidence and a reader must not be able to
    alter it."""
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&nofollow=1",
            timeout=5.0,
            uri=True,
        )
    except sqlite3.Error as error:
        raise SnapshotError("snapshot_unreadable", detail=str(path)) from error
    try:
        yield connection
    except sqlite3.Error as error:
        raise SnapshotError("snapshot_unreadable", detail=str(path)) from error
    finally:
        connection.close()


def _optional_timestamp(value: object, *, detail: str) -> str | None:
    if value is None:
        return None
    return normalise_timestamp(value, detail=detail)


def _json_column(value: object, *, detail: str) -> object:
    """A ``*_json`` column becomes a real object in the bundle: the
    export is canonical JSON, and JSON smuggled inside a string would
    make every downstream digest depend on legacy's whitespace."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SnapshotError("record_unparseable", detail=detail)
    try:
        return json.loads(value)
    except ValueError as error:
        raise SnapshotError("record_unparseable", detail=detail) from error


def read_journal_events(root: Path) -> Iterator[dict[str, object]]:
    """The cairn-ingest journal: ``journal/YYYY/MM/DD/<id>.json``.

    The complete event is private migration source material under P-72.
    Known timestamps are normalised in place while unknown fields remain
    untouched, so a field legacy grew after the gate's §4.2 inventory is
    carried rather than silently discarded.
    """
    _require_directory(root)
    seen: set[str] = set()
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root).as_posix()
        text = _read_utf8(path, detail=relative)
        try:
            event = json.loads(text)
        except ValueError as error:
            raise SnapshotError("record_unparseable", detail=relative) from error
        if not isinstance(event, dict):
            raise SnapshotError("record_unparseable", detail=relative)
        identity = _journal_identity(event, seen=seen, detail=relative)
        normalised_event = dict(event)
        for field in _JOURNAL_TIMESTAMP_FIELDS:
            value = event.get(field)
            if value is not None:
                normalised_event[field] = normalise_timestamp(value, detail=relative)
        yield {
            "store": "journal-event",
            "legacy_id": identity,
            "path": relative,
            "received_at": normalise_timestamp(
                event.get("received_at"), detail=relative
            ),
            "event": normalised_event,
        }


def _journal_identity(
    event: dict[str, object],
    *,
    seen: set[str],
    detail: str,
) -> str:
    primary = event.get("id")
    fallback = event.get("event_id")
    if primary is not None and fallback is not None and primary != fallback:
        raise SnapshotError("record_unparseable", detail=detail)
    identity = primary if primary is not None else fallback
    return _unique_identity(identity, seen=seen, detail=detail)


@dataclass(frozen=True, slots=True)
class EntityEdgeCounts:
    """P-75's evidence that derived data was seen and deliberately not
    migrated: how many entity edges legacy holds, and how many of them
    Graphiti had already marked contradicted or expired."""

    total: int
    invalidated: int
    expired: int


def read_graph_episodes(query: GraphQuery) -> Iterator[dict[str, object]]:
    """Graph episodes — the primary assertion source (§5.7).

    ``group_id`` is carried exactly as the graph holds it, including a
    value other than ``cairn``. Filtering here would be a silent drop;
    §5.8 rejects such a record at mapping time, counted and sampled in
    the report, which is what zero-silent-drops means.
    """
    seen: set[str] = set()
    for row in query(EPISODE_QUERY):
        if len(row) != 9:
            raise SnapshotError("record_unparseable", detail="graph episode row")
        identity, name, body, source, description, group, created, valid, embedding = (
            row
        )
        where = f"graph-episode:{identity}"
        identity = _unique_identity(identity, seen=seen, detail=where)
        yield {
            "store": "graph-episode",
            "legacy_id": identity,
            "name": name,
            "episode_body": body,
            "source": source,
            "source_description": description,
            "group_id": group,
            "created_at": normalise_timestamp(created, detail=where),
            "valid_at": _optional_timestamp(valid, detail=where),
            "embedding": embedding,
        }


def count_entity_nodes(query: GraphQuery) -> int:
    """Entity nodes are counted, never exported: they are Graphiti's
    extraction products and v0.1 rebuilds an equivalent layer itself
    (P-75). The count is the evidence that they were inventoried."""
    return _single_count(query(ENTITY_NODE_COUNT_QUERY))[0]


def count_entity_edges(query: GraphQuery) -> EntityEdgeCounts:
    """Entity edges, likewise counted rather than migrated — with the
    ``invalid_at``/``expired_at`` tallies P-75 hands to Operator, since any
    carry-over is an explicit post-migration ``/v1/invalidate`` ruling
    rather than a default."""
    total, invalidated, expired = _single_count(query(ENTITY_EDGE_COUNT_QUERY))
    return EntityEdgeCounts(total=total, invalidated=invalidated, expired=expired)


def _single_count(rows: list[list[object]]) -> list[int]:
    if len(rows) != 1:
        raise SnapshotError("record_unparseable", detail="graph count")
    counts = [value for value in rows[0] if type(value) is int]
    if len(counts) != len(rows[0]):
        raise SnapshotError("record_unparseable", detail="graph count")
    return counts


def falkordb_query(url: str, graph_name: str) -> GraphQuery:
    """The one live boundary in this module: a read-only session against
    the disposable container restored from Operator's snapshot.

    ``ro_query`` rather than ``query`` because the snapshot is evidence —
    the server itself refuses a write, so no mistake here can alter what
    the dry-run is measured against.

    No deterministic test can exercise this: it needs a real FalkorDB.
    The queries it runs are pinned as constants above and confirmed
    against the real store in the Task 4 dry-run, which is the honest
    place for that evidence.

    The import is local and untyped-ignored: ``falkordb`` ships no
    ``py.typed``, and only this function needs it — the file-backed
    readers must not drag a Redis client in behind them.
    """
    from falkordb import FalkorDB  # type: ignore[import-untyped]

    graph = FalkorDB.from_url(url).select_graph(graph_name)

    def query(cypher: str) -> list[list[object]]:
        rows: list[list[object]] = [
            list(row) for row in graph.ro_query(cypher).result_set
        ]
        return rows

    return query
