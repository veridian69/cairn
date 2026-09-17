"""Slice 9 Task 1: the legacy snapshot readers.

Every fixture here is synthetic, authored for the test (P-78): the shapes
come from the plan-authoring gate's §4 field inventory and from a
structural inspection of Operator's snapshot set — key names and delimiters
only, never record content. No legacy record, redacted or otherwise,
lives in this repository.

Module-local fixtures for the reason the other test modules record:
``tests`` has no package markers, so no ``conftest.py`` can be shared.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from cairn_migrate.snapshot import (
    ENTITY_EDGE_COUNT_QUERY,
    ENTITY_NODE_COUNT_QUERY,
    EPISODE_QUERY,
    EntityEdgeCounts,
    GraphQuery,
    SnapshotError,
    count_entity_edges,
    count_entity_nodes,
    read_attic_conversations,
    read_attic_turns,
    read_graph_episodes,
    read_journal_events,
    read_openbrain_rows,
    read_thought_files,
)


def _write_openbrain(path: Path, records: list[dict[str, object]]) -> Path:
    dump = path / "openbrain-dump.jsonl"
    dump.write_text("".join(json.dumps(record) + "\n" for record in records))
    return dump


OPENBRAIN_ROW = {
    "_table": "thoughts",
    "_exported_at": "2026-06-14T21:47:39.123456+00:00",
    "_had_embedding": True,
    "row": {
        "id": "ob-1",
        "content": "synthetic body",
        "content_fingerprint": "fp-1",
        "created_at": "2026-04-21 09:15:00.000000+02:00",
        "updated_at": "2026-04-21 09:15:00.000000+02:00",
        "metadata": {"topics": ["synthetic"], "type": "note"},
    },
}


def test_openbrain_reader_produces_the_documented_fields(tmp_path: Path) -> None:
    dump = _write_openbrain(tmp_path, [OPENBRAIN_ROW])

    rows = list(read_openbrain_rows(dump))

    assert rows == [
        {
            "store": "openbrain-row",
            "legacy_id": "ob-1",
            "line_number": 1,
            "table": "thoughts",
            "exported_at": "2026-06-14T21:47:39.123456Z",
            "had_embedding": True,
            "content": "synthetic body",
            "content_fingerprint": "fp-1",
            "created_at": "2026-04-21T07:15:00.000000Z",
            "updated_at": "2026-04-21T07:15:00.000000Z",
            "metadata": {"topics": ["synthetic"], "type": "note"},
        }
    ]


@pytest.mark.parametrize("identity", (None, 17, ""))
def test_openbrain_reader_refuses_an_invalid_identity(
    tmp_path: Path,
    identity: object,
) -> None:
    record = json.loads(json.dumps(OPENBRAIN_ROW))
    record["row"]["id"] = identity
    dump = _write_openbrain(tmp_path, [record])

    with pytest.raises(SnapshotError) as refusal:
        list(read_openbrain_rows(dump))

    assert refusal.value.code == "record_unparseable"


def test_openbrain_reader_refuses_duplicate_identities(tmp_path: Path) -> None:
    dump = _write_openbrain(tmp_path, [OPENBRAIN_ROW, OPENBRAIN_ROW])

    with pytest.raises(SnapshotError) as refusal:
        list(read_openbrain_rows(dump))

    assert refusal.value.code == "record_unparseable"


def test_openbrain_reader_keeps_unicode_line_separators_inside_a_json_string(
    tmp_path: Path,
) -> None:
    record = json.loads(json.dumps(OPENBRAIN_ROW))
    record["row"]["content"] = "first\u2028second\u2029third\u0085fourth"
    dump = tmp_path / "openbrain-dump.jsonl"
    dump.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = list(read_openbrain_rows(dump))

    assert rows[0]["content"] == "first\u2028second\u2029third\u0085fourth"


def _write_thought(
    root: Path,
    relative: str,
    frontmatter: dict[str, object] | None,
    body: str,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        path.write_text(body)
        return
    header = json.dumps(frontmatter, indent=2, sort_keys=True)
    path.write_text(f"---\n{header}\n---\n{body}")


THOUGHT_FRONTMATTER: dict[str, object] = {
    "action_items": [],
    "captured_at": "2026-04-21T09:15:00+02:00",
    "dates_mentioned": ["2026-04-21"],
    "fingerprint": "fp-1",
    "openbrain_id": "ob-1",
    "source": "synthetic",
    "topics": ["synthetic"],
    "type": "note",
}


def test_thought_reader_uses_openbrain_identity_and_normalises_frontmatter(
    tmp_path: Path,
) -> None:
    _write_thought(
        tmp_path,
        "2026-04/2026-04-21-synthetic.md",
        THOUGHT_FRONTMATTER,
        "category:synthetic type:note\n\nsynthetic body\n",
    )

    files = list(read_thought_files(tmp_path))

    assert files == [
        {
            "store": "thought-file",
            "legacy_id": "ob-1",
            "path": "2026-04/2026-04-21-synthetic.md",
            "captured_at": "2026-04-21T07:15:00.000000Z",
            "frontmatter": {
                **THOUGHT_FRONTMATTER,
                "captured_at": "2026-04-21T07:15:00.000000Z",
            },
            "body": "category:synthetic type:note\n\nsynthetic body\n",
        }
    ]


def test_thought_reader_reads_every_file_in_a_stable_order(tmp_path: Path) -> None:
    for relative in ("2026-05/b.md", "2026-04/a.md", "2026-04/b.md"):
        _write_thought(
            tmp_path,
            relative,
            {**THOUGHT_FRONTMATTER, "openbrain_id": f"id:{relative}"},
            "body\n",
        )

    identities = [record["legacy_id"] for record in read_thought_files(tmp_path)]

    assert identities == [
        "id:2026-04/a.md",
        "id:2026-04/b.md",
        "id:2026-05/b.md",
    ]


def test_thought_reader_refuses_a_missing_openbrain_identity(tmp_path: Path) -> None:
    frontmatter = dict(THOUGHT_FRONTMATTER)
    del frontmatter["openbrain_id"]
    _write_thought(tmp_path, "2026-04/headless.md", frontmatter, "body\n")

    with pytest.raises(SnapshotError) as refusal:
        list(read_thought_files(tmp_path))

    assert refusal.value.code == "record_unparseable"


def test_thought_reader_refuses_a_file_without_readable_frontmatter(
    tmp_path: Path,
) -> None:
    _write_thought(tmp_path, "2026-04/headless.md", None, "no frontmatter here\n")

    with pytest.raises(SnapshotError) as refusal:
        list(read_thought_files(tmp_path))

    assert refusal.value.code == "record_unparseable"


@pytest.mark.parametrize(
    "reader",
    (read_thought_files, read_journal_events),
)
def test_directory_reader_refuses_a_missing_snapshot_root(
    tmp_path: Path,
    reader: Callable[[Path], Iterator[dict[str, object]]],
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(SnapshotError) as refusal:
        list(reader(missing))

    assert refusal.value.code == "snapshot_unreadable"


ATTIC_SCHEMA = """
CREATE TABLE transcript_conversations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    started_at TEXT,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE transcript_turns (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT,
    episode_id TEXT,
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES transcript_conversations(id)
);
"""


def _write_attic(
    path: Path,
    conversations: Sequence[tuple[object, ...]],
    turns: Sequence[tuple[object, ...]],
) -> Path:
    attic = path / "transcripts.sqlite"
    connection = sqlite3.connect(attic)
    try:
        connection.executescript(ATTIC_SCHEMA)
        connection.executemany(
            "INSERT INTO transcript_conversations "
            "(id, source, title, started_at, ended_at, metadata_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            conversations,
        )
        connection.executemany(
            "INSERT INTO transcript_turns "
            "(id, conversation_id, turn_index, role, content, created_at, "
            "episode_id, content_sha256, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            turns,
        )
        connection.commit()
    finally:
        connection.close()
    return attic


CONVERSATION = (
    "conv-1",
    "claude-code",
    "synthetic conversation",
    "2026-08-01T10:00:00+00:00",
    None,
    '{"channel": "synthetic"}',
    "2026-08-01 10:00:00",
    "2026-08-01 10:05:00",
)
TURN = (
    "turn-1",
    "conv-1",
    0,
    "user",
    "synthetic turn",
    "2026-08-01T10:00:01+00:00",
    "episode-1",
    "sha-1",
    '{"kind": "synthetic"}',
)


def test_attic_conversation_reader_produces_the_documented_fields(
    tmp_path: Path,
) -> None:
    attic = _write_attic(tmp_path, [CONVERSATION], [])

    assert list(read_attic_conversations(attic)) == [
        {
            "store": "attic-conversation",
            "legacy_id": "conv-1",
            "source": "claude-code",
            "title": "synthetic conversation",
            "started_at": "2026-08-01T10:00:00.000000Z",
            "ended_at": None,
            "created_at": "2026-08-01T10:00:00.000000Z",
            "updated_at": "2026-08-01T10:05:00.000000Z",
            "metadata": {"channel": "synthetic"},
        }
    ]


@pytest.mark.parametrize("identity", (None, ""))
def test_attic_conversation_reader_refuses_an_invalid_identity(
    tmp_path: Path,
    identity: object,
) -> None:
    conversation = (identity, *CONVERSATION[1:])
    attic = _write_attic(tmp_path, [conversation], [])

    with pytest.raises(SnapshotError) as refusal:
        list(read_attic_conversations(attic))

    assert refusal.value.code == "record_unparseable"


def test_attic_conversation_reader_refuses_duplicate_identities(tmp_path: Path) -> None:
    attic = tmp_path / "transcripts.sqlite"
    connection = sqlite3.connect(attic)
    try:
        connection.execute(
            "CREATE TABLE transcript_conversations ("
            "id TEXT, source TEXT, title TEXT, started_at TEXT, ended_at TEXT, "
            "metadata_json TEXT, created_at TEXT, updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO transcript_conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (CONVERSATION, CONVERSATION),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SnapshotError) as refusal:
        list(read_attic_conversations(attic))

    assert refusal.value.code == "record_unparseable"


def test_attic_turn_reader_produces_the_documented_fields(tmp_path: Path) -> None:
    attic = _write_attic(tmp_path, [CONVERSATION], [TURN])

    assert list(read_attic_turns(attic)) == [
        {
            "store": "attic-turn",
            "legacy_id": "turn-1",
            "conversation_id": "conv-1",
            "turn_index": 0,
            "role": "user",
            "content": "synthetic turn",
            "created_at": "2026-08-01T10:00:01.000000Z",
            "episode_id": "episode-1",
            "content_sha256": "sha-1",
            "metadata": {"kind": "synthetic"},
        }
    ]


def test_attic_turn_reader_refuses_an_empty_identity(tmp_path: Path) -> None:
    turn = ("", *TURN[1:])
    attic = _write_attic(tmp_path, [CONVERSATION], [turn])

    with pytest.raises(SnapshotError) as refusal:
        list(read_attic_turns(attic))

    assert refusal.value.code == "record_unparseable"


def test_attic_turn_reader_orders_by_conversation_then_turn_index(
    tmp_path: Path,
) -> None:
    turns = [
        ("turn-b1", "conv-b", 1, "user", "b1", None, None, "sha", "{}"),
        ("turn-a2", "conv-a", 2, "user", "a2", None, None, "sha", "{}"),
        ("turn-a1", "conv-a", 1, "user", "a1", None, None, "sha", "{}"),
    ]
    conversations = [
        (
            "conv-a",
            "synthetic",
            None,
            None,
            None,
            "{}",
            "2026-08-01 10:00:00",
            "2026-08-01 10:00:00",
        ),
        (
            "conv-b",
            "synthetic",
            None,
            None,
            None,
            "{}",
            "2026-08-01 10:00:00",
            "2026-08-01 10:00:00",
        ),
    ]
    attic = _write_attic(tmp_path, conversations, turns)

    identities = [record["legacy_id"] for record in read_attic_turns(attic)]

    assert identities == ["turn-a1", "turn-a2", "turn-b1"]


def test_attic_reader_refuses_unparseable_metadata(tmp_path: Path) -> None:
    conversation = (*CONVERSATION[:5], "not json", *CONVERSATION[6:])
    attic = _write_attic(tmp_path, [conversation], [])

    with pytest.raises(SnapshotError) as refusal:
        list(read_attic_conversations(attic))

    assert refusal.value.code == "record_unparseable"


JOURNAL_EVENT: dict[str, object] = {
    "accepted_at": "2026-06-13 21:52:17",
    "actor": "spike",
    "confirmed_at": "2026-06-13T23:52:19+02:00",
    "episode_uuid": "11111111-1111-4111-8111-111111111111",
    "group_id": "cairn",
    "id": "2026-06-13T21-52-16Z_e49f4b5f",
    "kind": "add_memory",
    "metadata": {"topics": ["synthetic"]},
    "next_retry_at": "2026-06-13T21:53:00Z",
    "payload_sha256": "sha-1",
    "received_at": "2026-06-13T21:52:16Z",
    "source": "claude-code",
    "state": "confirmed",
    "submission_accepted_at": "2026-06-13T21:52:18+00:00",
    "text": "synthetic submitted text",
    "version": "1",
}


def _write_journal_event(root: Path, relative: str, event: dict[str, object]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event, indent=2, sort_keys=True))


def test_journal_reader_carries_documented_fields_and_normalises_timestamps(
    tmp_path: Path,
) -> None:
    _write_journal_event(
        tmp_path,
        "2026/06/13/2026-06-13T21-52-16Z_e49f4b5f.json",
        JOURNAL_EVENT,
    )

    events = list(read_journal_events(tmp_path))

    assert events == [
        {
            "store": "journal-event",
            "legacy_id": "2026-06-13T21-52-16Z_e49f4b5f",
            "path": "2026/06/13/2026-06-13T21-52-16Z_e49f4b5f.json",
            "received_at": "2026-06-13T21:52:16.000000Z",
            "event": {
                "accepted_at": "2026-06-13T21:52:17.000000Z",
                "actor": "spike",
                "confirmed_at": "2026-06-13T21:52:19.000000Z",
                "episode_uuid": "11111111-1111-4111-8111-111111111111",
                "group_id": "cairn",
                "id": "2026-06-13T21-52-16Z_e49f4b5f",
                "kind": "add_memory",
                "metadata": {"topics": ["synthetic"]},
                "next_retry_at": "2026-06-13T21:53:00.000000Z",
                "payload_sha256": "sha-1",
                "received_at": "2026-06-13T21:52:16.000000Z",
                "source": "claude-code",
                "state": "confirmed",
                "submission_accepted_at": "2026-06-13T21:52:18.000000Z",
                "text": "synthetic submitted text",
                "version": "1",
            },
        }
    ]


def test_journal_reader_reads_the_date_tree_in_a_stable_order(tmp_path: Path) -> None:
    for relative in ("2026/06/14/b.json", "2026/06/13/a.json", "2026/05/31/c.json"):
        _write_journal_event(tmp_path, relative, {**JOURNAL_EVENT, "id": relative})

    paths = [record["path"] for record in read_journal_events(tmp_path)]

    assert paths == ["2026/05/31/c.json", "2026/06/13/a.json", "2026/06/14/b.json"]


def test_journal_reader_refuses_an_event_without_an_identity(tmp_path: Path) -> None:
    event = {key: value for key, value in JOURNAL_EVENT.items() if key != "id"}
    _write_journal_event(tmp_path, "2026/06/13/headless.json", event)

    with pytest.raises(SnapshotError) as refusal:
        list(read_journal_events(tmp_path))

    assert refusal.value.code == "record_unparseable"


def test_journal_reader_accepts_event_id_as_the_legacy_identity(tmp_path: Path) -> None:
    event = {
        **JOURNAL_EVENT,
        "event_id": "event-1",
    }
    del event["id"]
    _write_journal_event(tmp_path, "2026/06/13/event-1.json", event)

    records = list(read_journal_events(tmp_path))

    assert records[0]["legacy_id"] == "event-1"


def test_journal_reader_refuses_duplicate_identities(tmp_path: Path) -> None:
    _write_journal_event(tmp_path, "2026/06/13/first.json", JOURNAL_EVENT)
    _write_journal_event(tmp_path, "2026/06/14/second.json", JOURNAL_EVENT)

    with pytest.raises(SnapshotError) as refusal:
        list(read_journal_events(tmp_path))

    assert refusal.value.code == "record_unparseable"


def test_text_snapshot_readers_request_utf8_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = _write_openbrain(tmp_path, [OPENBRAIN_ROW])
    thoughts = tmp_path / "thoughts"
    _write_thought(thoughts, "synthetic.md", THOUGHT_FRONTMATTER, "body\n")
    journal = tmp_path / "journal"
    _write_journal_event(journal, "synthetic.json", JOURNAL_EVENT)
    original_read_text = Path.read_text

    def require_utf8(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        assert encoding == "utf-8"
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", require_utf8)

    assert len(list(read_openbrain_rows(dump))) == 1
    assert len(list(read_thought_files(thoughts))) == 1
    assert len(list(read_journal_events(journal))) == 1


EPISODE_ROW: list[object] = [
    "22222222-2222-4222-8222-222222222222",
    "synthetic episode",
    "synthetic episode body",
    "text",
    "cairn_event_id=2026-06-13T21-52-16Z_e49f4b5f cairn_episode_uuid=1111",
    "cairn",
    "2026-06-13T21:52:17+00:00",
    "2026-06-13T21:52:16+00:00",
    [0.125, -0.5, 0.75],
]


def _graph(responses: dict[str, list[list[object]]]) -> GraphQuery:
    def query(cypher: str) -> list[list[object]]:
        return responses[cypher]

    return query


def test_episode_query_selects_the_verified_corpus_properties() -> None:
    # The Episodic property set was verified against the restored 22 Aug 2026
    # dump via GRAPH.RO_QUERY keys(e): the body lives in `content`; there is
    # no `episode_body` property, and selecting one exports every body as
    # null (the 23 Aug PoC dry run rejected all 632 episodes as body_empty).
    assert "e.content" in EPISODE_QUERY
    assert "episode_body" not in EPISODE_QUERY


def test_graph_episode_reader_produces_the_documented_fields() -> None:
    query = _graph({EPISODE_QUERY: [EPISODE_ROW]})

    assert list(read_graph_episodes(query)) == [
        {
            "store": "graph-episode",
            "legacy_id": "22222222-2222-4222-8222-222222222222",
            "name": "synthetic episode",
            "episode_body": "synthetic episode body",
            "source": "text",
            "source_description": (
                "cairn_event_id=2026-06-13T21-52-16Z_e49f4b5f cairn_episode_uuid=1111"
            ),
            "group_id": "cairn",
            "created_at": "2026-06-13T21:52:17.000000Z",
            "valid_at": "2026-06-13T21:52:16.000000Z",
            "embedding": [0.125, -0.5, 0.75],
        }
    ]


def test_graph_episode_reader_keeps_a_null_valid_at_null() -> None:
    row = [*EPISODE_ROW[:7], None, EPISODE_ROW[8]]
    query = _graph({EPISODE_QUERY: [row]})

    assert list(read_graph_episodes(query))[0]["valid_at"] is None


@pytest.mark.parametrize("identity", (None, 17, ""))
def test_graph_episode_reader_refuses_an_invalid_identity(identity: object) -> None:
    row = [identity, *EPISODE_ROW[1:]]
    query = _graph({EPISODE_QUERY: [row]})

    with pytest.raises(SnapshotError) as refusal:
        list(read_graph_episodes(query))

    assert refusal.value.code == "record_unparseable"


def test_graph_episode_reader_refuses_duplicate_identities() -> None:
    query = _graph({EPISODE_QUERY: [EPISODE_ROW, EPISODE_ROW]})

    with pytest.raises(SnapshotError) as refusal:
        list(read_graph_episodes(query))

    assert refusal.value.code == "record_unparseable"


def test_graph_episode_reader_carries_a_foreign_group_id_rather_than_dropping_it() -> (
    None
):
    row = [
        "33333333-3333-4333-8333-333333333333",
        *EPISODE_ROW[1:5],
        "other-realm",
        *EPISODE_ROW[6:],
    ]
    query = _graph({EPISODE_QUERY: [EPISODE_ROW, row]})

    group_ids = [record["group_id"] for record in read_graph_episodes(query)]

    assert group_ids == ["cairn", "other-realm"]


def test_entity_node_and_edge_counts_are_read_for_the_p75_record() -> None:
    query = _graph(
        {
            ENTITY_NODE_COUNT_QUERY: [[412]],
            ENTITY_EDGE_COUNT_QUERY: [[988, 17, 4]],
        }
    )

    assert count_entity_nodes(query) == 412
    assert count_entity_edges(query) == EntityEdgeCounts(
        total=988,
        invalidated=17,
        expired=4,
    )
