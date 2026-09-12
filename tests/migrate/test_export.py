"""Slice 9 Task 1: the ``cairn-legacy-export/v1`` bundle (P-73).

The bundle claims proven here: every store lands as canonical JSONL,
the manifest's counts and digests describe what was actually written,
a second export over the same snapshot is byte-identical, entity nodes
and edges are counted rather than exported (P-75), and a record whose
``group_id`` is not ``cairn`` is carried through for §5.8 to reject
rather than dropped on the way out.

Synthetic fixtures only (P-78). Module-local, because ``tests`` has no
package markers.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from redis.exceptions import RedisError

import cairn_migrate.export as export_module
from cairn_migrate.__main__ import main
from cairn_migrate.export import (
    EXPORT_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    ExportError,
    SnapshotSet,
    write_export_bundle,
)
from cairn_migrate.snapshot import (
    ENTITY_EDGE_COUNT_QUERY,
    ENTITY_NODE_COUNT_QUERY,
    EPISODE_QUERY,
    GraphQuery,
)

FOREIGN_EPISODE_ID = "33333333-3333-4333-8333-333333333333"

EPISODE_ROWS: list[list[object]] = [
    [
        "22222222-2222-4222-8222-222222222222",
        "synthetic episode",
        "synthetic episode body",
        "text",
        "cairn_event_id=synthetic-1",
        "cairn",
        "2026-06-13T21:52:17+00:00",
        "2026-06-13T21:52:16+00:00",
        [0.125, -0.5, 0.75],
    ],
    [
        FOREIGN_EPISODE_ID,
        "foreign episode",
        "foreign body",
        "text",
        "",
        "other-realm",
        "2026-06-13T21:52:18+00:00",
        None,
        [0.25, -0.125, 0.5],
    ],
]

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


def _graph() -> GraphQuery:
    responses: dict[str, list[list[object]]] = {
        EPISODE_QUERY: EPISODE_ROWS,
        ENTITY_NODE_COUNT_QUERY: [[412]],
        ENTITY_EDGE_COUNT_QUERY: [[988, 17, 4]],
    }

    def query(cypher: str) -> list[list[object]]:
        return responses[cypher]

    return query


def _snapshot_set(root: Path) -> SnapshotSet:
    """One synthetic record in every file-backed store."""
    attic = root / "transcripts.sqlite"
    connection = sqlite3.connect(attic)
    try:
        connection.executescript(ATTIC_SCHEMA)
        connection.execute(
            "INSERT INTO transcript_conversations "
            "(id, source, title, metadata_json, created_at, updated_at) "
            "VALUES ('conv-1', 'synthetic', 'title', '{}', "
            "'2026-08-01 10:00:00', '2026-08-01 10:00:00')"
        )
        connection.execute(
            "INSERT INTO transcript_turns "
            "(id, conversation_id, turn_index, role, content, content_sha256) "
            "VALUES ('turn-1', 'conv-1', 0, 'user', 'synthetic turn', 'sha-1')"
        )
        connection.commit()
    finally:
        connection.close()

    journal = root / "journal" / "2026" / "06" / "13"
    journal.mkdir(parents=True)
    (journal / "synthetic-1.json").write_text(
        json.dumps(
            {
                "actor": "spike",
                "group_id": "cairn",
                "id": "synthetic-1",
                "received_at": "2026-06-13T21:52:16Z",
                "source": "claude-code",
                "text": "synthetic submitted text",
            }
        )
    )

    thoughts = root / "thoughts" / "2026-04"
    thoughts.mkdir(parents=True)
    frontmatter = json.dumps(
        {"captured_at": "2026-04-21T09:15:00+02:00", "openbrain_id": "ob-1"},
        sort_keys=True,
    )
    (thoughts / "synthetic.md").write_text(f"---\n{frontmatter}\n---\nsynthetic body\n")

    openbrain = root / "openbrain-dump.jsonl"
    openbrain.write_text(
        json.dumps(
            {
                "_table": "thoughts",
                "_exported_at": "2026-06-14T21:47:39.123456+00:00",
                "_had_embedding": True,
                "row": {
                    "id": "ob-1",
                    "content": "synthetic body",
                    "content_fingerprint": "fp-1",
                    "created_at": "2026-04-21 09:15:00.000000+02:00",
                    "updated_at": "2026-04-21 09:15:00.000000+02:00",
                    "metadata": {},
                },
            }
        )
        + "\n"
    )

    return SnapshotSet(
        label="synthetic-2026-08-20",
        graph_dump="falkor-synthetic.tar.gz",
        restore_image="falkordb/falkordb:synthetic",
        restore_image_digest="sha256:" + "0" * 64,
        attic_path=attic,
        journal_root=root / "journal",
        thoughts_root=root / "thoughts",
        openbrain_path=openbrain,
    )


def _export(
    snapshot: SnapshotSet,
    output: Path,
) -> Path:
    result = write_export_bundle(
        snapshot,
        _graph(),
        output,
    )
    return result.bundle_path


def _exported(tmp_path: Path, name: str = "bundle") -> Path:
    return _export(_snapshot_set(_snapshot_root(tmp_path)), tmp_path / name)


def _snapshot_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir()
    return root


def _manifest(bundle: Path) -> dict[str, object]:
    loaded = json.loads((bundle / MANIFEST_FILENAME).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_export_writes_one_canonical_jsonl_file_for_every_store(
    tmp_path: Path,
) -> None:
    bundle = _exported(tmp_path)

    written = sorted(path.name for path in bundle.glob("*.jsonl"))
    assert written == [
        "attic-conversation.jsonl",
        "attic-turn.jsonl",
        "graph-episode.jsonl",
        "journal-event.jsonl",
        "openbrain-row.jsonl",
        "thought-file.jsonl",
    ]
    line = (bundle / "graph-episode.jsonl").read_text().splitlines()[0]
    assert line == json.dumps(
        json.loads(line),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def test_manifest_counts_and_digests_describe_the_emitted_files(
    tmp_path: Path,
) -> None:
    bundle = _exported(tmp_path)

    manifest = _manifest(bundle)
    assert manifest["schema_version"] == EXPORT_SCHEMA_VERSION
    stores = manifest["stores"]
    assert isinstance(stores, list)
    for store in stores:
        assert isinstance(store, dict)
        path = bundle / str(store["filename"])
        content = path.read_bytes()
        assert store["bytes"] == len(content)
        assert store["sha256"] == hashlib.sha256(content).hexdigest()
        assert store["record_count"] == len(content.decode().splitlines())
    counts = {store["store"]: store["record_count"] for store in stores}
    assert counts == {
        "graph-episode": 2,
        "attic-conversation": 1,
        "attic-turn": 1,
        "journal-event": 1,
        "thought-file": 1,
        "openbrain-row": 1,
    }


def test_manifest_records_the_snapshot_identity_and_restore_container(
    tmp_path: Path,
) -> None:
    manifest = _manifest(_exported(tmp_path))

    snapshot = manifest["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["label"] == "synthetic-2026-08-20"
    assert snapshot["graph_dump"] == "falkor-synthetic.tar.gz"
    assert snapshot["restore_image"] == "falkordb/falkordb:synthetic"
    assert snapshot["restore_image_digest"] == "sha256:" + "0" * 64
    assert "created_at" not in manifest


def test_manifest_counts_entity_nodes_and_edges_without_exporting_them(
    tmp_path: Path,
) -> None:
    bundle = _exported(tmp_path)

    assert not (bundle / "entity-node.jsonl").exists()
    assert not (bundle / "entity-edge.jsonl").exists()
    assert _manifest(bundle)["derived_counts"] == {
        "entity_node": 412,
        "entity_edge": 988,
        "entity_edge_invalidated": 17,
        "entity_edge_expired": 4,
    }


def test_a_second_export_over_the_same_snapshot_is_byte_identical(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_set(_snapshot_root(tmp_path))

    first = _export(snapshot, tmp_path / "first")
    second = _export(snapshot, tmp_path / "second")

    assert first.name == second.name == "cairn-legacy-export-synthetic-2026-08-20"
    for path in sorted(first.glob("*.jsonl")):
        assert path.read_bytes() == (second / path.name).read_bytes()
    assert _manifest(first) == _manifest(second)


def test_an_episode_outside_the_cairn_group_is_exported_not_dropped(
    tmp_path: Path,
) -> None:
    bundle = _exported(tmp_path)

    episodes = [
        json.loads(line)
        for line in (bundle / "graph-episode.jsonl").read_text().splitlines()
    ]
    foreign = [record for record in episodes if record["group_id"] != "cairn"]
    assert [record["legacy_id"] for record in foreign] == [FOREIGN_EPISODE_ID]


def test_export_refuses_non_finite_numbers_in_canonical_json(tmp_path: Path) -> None:
    snapshot = _snapshot_set(_snapshot_root(tmp_path))
    record = json.loads(snapshot.openbrain_path.read_text())
    record["row"]["metadata"] = {"score": float("nan")}
    snapshot.openbrain_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(ExportError) as refusal:
        _export(snapshot, tmp_path / "private")

    assert refusal.value.code == "record_not_canonical_json"


def test_export_refuses_a_lone_surrogate_in_canonical_json(tmp_path: Path) -> None:
    snapshot = _snapshot_set(_snapshot_root(tmp_path))
    record = json.loads(snapshot.openbrain_path.read_text())
    record["row"]["metadata"] = {"invalid_unicode": "\ud800"}
    snapshot.openbrain_path.write_text(json.dumps(record) + "\n")

    with pytest.raises(ExportError) as refusal:
        _export(snapshot, tmp_path / "private")

    assert refusal.value.code == "record_not_canonical_json"


def test_export_writer_refuses_an_output_inside_the_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(export_module, "_CHECKOUT_ROOT", checkout, raising=False)
    snapshot = _snapshot_set(_snapshot_root(tmp_path))

    with pytest.raises(ExportError) as refusal:
        _export(snapshot, checkout / "private")

    assert refusal.value.code == "output_inside_checkout"


def test_export_diagnoses_a_manifestless_existing_bundle(tmp_path: Path) -> None:
    snapshot = _snapshot_set(_snapshot_root(tmp_path))
    output = tmp_path / "private"
    incomplete = output / f"cairn-legacy-export-{snapshot.label}"
    incomplete.mkdir(parents=True)

    with pytest.raises(ExportError) as refusal:
        _export(snapshot, output)

    assert refusal.value.code == "bundle_incomplete"


def _command(tmp_path: Path, output: Path) -> list[str]:
    root = _snapshot_root(tmp_path)
    snapshot = _snapshot_set(root)
    return [
        "export",
        "--snapshot-label",
        snapshot.label,
        "--graph-dump",
        snapshot.graph_dump,
        "--restore-image",
        snapshot.restore_image,
        "--restore-image-digest",
        snapshot.restore_image_digest,
        "--attic",
        str(snapshot.attic_path),
        "--journal",
        str(snapshot.journal_root),
        "--thoughts",
        str(snapshot.thoughts_root),
        "--openbrain",
        str(snapshot.openbrain_path),
        "--output",
        str(output),
    ]


def test_export_help_documents_the_disposable_restore_container(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_code:
        main(["export", "--help"])

    assert exit_code.value.code == 0
    help_text = capsys.readouterr().out
    assert "docker run" in help_text
    assert "--falkor-url" in help_text
    assert "never contacts the VPS" in help_text


def test_export_command_writes_a_bundle(tmp_path: Path) -> None:
    output = tmp_path / "private"

    assert (
        main(_command(tmp_path, output), graph_factory=lambda url, name: _graph()) == 0
    )

    bundle = next(output.iterdir())
    assert _manifest(bundle)["schema_version"] == EXPORT_SCHEMA_VERSION


def test_export_command_resolves_relative_snapshot_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "private"
    command = _command(tmp_path, output)
    for option in ("--attic", "--journal", "--thoughts", "--openbrain"):
        index = command.index(option) + 1
        command[index] = str(Path(command[index]).relative_to(tmp_path))
    monkeypatch.chdir(tmp_path)

    exit_code = main(command, graph_factory=lambda url, name: _graph())

    assert exit_code == 0
    snapshot = _manifest(next(output.iterdir()))["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["graph_dump"] == str(tmp_path / "falkor-synthetic.tar.gz")
    assert snapshot["attic_path"] == str(tmp_path / "snapshot/transcripts.sqlite")
    assert snapshot["journal_root"] == str(tmp_path / "snapshot/journal")
    assert snapshot["thoughts_root"] == str(tmp_path / "snapshot/thoughts")
    assert snapshot["openbrain_path"] == str(tmp_path / "snapshot/openbrain-dump.jsonl")


def test_export_command_translates_an_eager_redis_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable_graph(url: str, name: str) -> GraphQuery:
        raise RedisError("synthetic unavailable graph")

    exit_code = main(
        _command(tmp_path, tmp_path / "private"),
        graph_factory=unavailable_graph,
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "graph_unavailable",
        "status": "error",
    }


def test_export_command_translates_an_eager_redis_url_value_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def invalid_graph_url(url: str, name: str) -> GraphQuery:
        raise ValueError("synthetic invalid Redis database path")

    command = _command(tmp_path, tmp_path / "private")
    command.extend(("--falkor-url", "redis://127.0.0.1:6399/not-a-db"))

    exit_code = main(command, graph_factory=invalid_graph_url)

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "graph_unavailable",
        "status": "error",
    }


def test_export_command_translates_a_midrun_redis_failure_and_diagnoses_debris(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "private"
    command = _command(tmp_path, output)

    def unavailable_query(cypher: str) -> list[list[object]]:
        raise RedisError("synthetic lost graph")

    first_exit = main(command, graph_factory=lambda url, name: unavailable_query)
    first_error = json.loads(capsys.readouterr().err)
    second_exit = main(command, graph_factory=lambda url, name: unavailable_query)
    second_error = json.loads(capsys.readouterr().err)

    assert first_exit == 2
    assert first_error == {"code": "graph_unavailable", "status": "error"}
    assert second_exit == 2
    assert second_error == {"code": "bundle_incomplete", "status": "error"}


def test_export_command_reports_graph_row_shape_drift_as_an_unparseable_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responses: dict[str, list[list[object]]] = {
        EPISODE_QUERY: [EPISODE_ROWS[0][:-1]],
        ENTITY_NODE_COUNT_QUERY: [[412]],
        ENTITY_EDGE_COUNT_QUERY: [[988, 17, 4]],
    }

    def malformed_query(cypher: str) -> list[list[object]]:
        return responses[cypher]

    exit_code = main(
        _command(tmp_path, tmp_path / "private"),
        graph_factory=lambda url, name: malformed_query,
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "record_unparseable",
        "detail": "graph episode row",
        "status": "error",
    }


@pytest.mark.parametrize(
    "falkor_url",
    (
        "redis://legacy.example:6379",
        "redis://productive-secret@127.0.0.1:6379",
    ),
)
def test_export_command_refuses_non_loopback_or_credential_bearing_graph_urls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    falkor_url: str,
) -> None:
    command = _command(tmp_path, tmp_path / "private")
    command.extend(("--falkor-url", falkor_url))

    exit_code = main(command, graph_factory=lambda url, name: _graph())

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "falkor_url_not_loopback",
        "status": "error",
    }


def test_export_command_refuses_a_snapshot_label_that_can_escape_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _command(tmp_path, tmp_path / "private")
    command[command.index("--snapshot-label") + 1] = "safe/../../escaped"

    exit_code = main(command, graph_factory=lambda url, name: _graph())

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {
        "code": "snapshot_label_invalid",
        "status": "error",
    }


def test_export_command_refuses_to_write_inside_the_checkout(tmp_path: Path) -> None:
    inside = Path(__file__).resolve().parent / "unwanted-bundle"

    exit_code = main(
        _command(tmp_path, inside),
        graph_factory=lambda url, name: _graph(),
    )

    assert exit_code == 2
    assert not inside.exists()
