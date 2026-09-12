"""Deterministic subset-plan generation tests for migration plans.

The subset command reads a full ``cairn-migration-plan/v1`` directory and writes
an independent, materialised subset directory that is safe for Stage-B apply.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from cairn_migrate.__main__ import _CHECKOUT_ROOT, main
from cairn_migrate.apply import read_plan
from cairn_migrate.export import MANIFEST_FILENAME as EXPORT_MANIFEST_FILENAME
from cairn_migrate.mapping import (
    ENUMERATIONS_FILENAME,
    OPERATIONS_FILENAME,
    PLAN_INPUT_STORES,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    RECONCILIATIONS_FILENAME,
    REJECTIONS_FILENAME,
    SUBSET_CONVERSATION_IDENTITY_PREFIX,
    SUBSET_PLAN_PROFILE,
    SUBSET_PLAN_RANKING_ALGORITHM,
    SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT,
    SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT,
    SUBSET_PLAN_REQUIRED_TURN_COUNT,
    SUBSET_TURN_IDENTITY_PREFIX,
    MappingError,
    canonical_json,
    map_bundle,
    read_export_bundle,
    write_plan,
    write_subset_plan,
)


def _write_source_records(
    root: Path,
    *,
    conversation_count: int,
    turns_per_conversation: int,
    graph_episode_count: int = 1,
) -> Path:
    records: dict[str, list[dict[str, object]]] = {
        "graph-episode": [],
        "attic-conversation": [],
        "attic-turn": [],
        "journal-event": [],
        "thought-file": [],
        "openbrain-row": [],
    }

    for index in range(graph_episode_count):
        records["graph-episode"].append(
            {
                "store": "graph-episode",
                "legacy_id": f"episode-{index:04d}",
                "name": "SYNTHETIC-Episode",
                "episode_body": f"body-episode-{index:04d}",
                "source": "synthetic",
                "source_description": "",
                "group_id": "cairn",
                "created_at": "2026-08-23T12:00:00.000000Z",
                "valid_at": None,
                "embedding": [0.1, 0.2],
            }
        )

    for index in range(conversation_count):
        conversation_id = f"conversation-{index:04d}"
        records["attic-conversation"].append(
            {
                "store": "attic-conversation",
                "legacy_id": conversation_id,
                "source": "subset-test",
                "title": f"conversation {index}",
                "started_at": "2026-08-23T12:00:00.000000Z",
                "ended_at": None,
                "created_at": "2026-08-23T12:00:01.000000Z",
                "updated_at": "2026-08-23T12:00:02.000000Z",
                "metadata": {"index": index},
            }
        )
        for turn_index in range(turns_per_conversation):
            records["attic-turn"].append(
                {
                    "store": "attic-turn",
                    "legacy_id": f"turn-{index:04d}-{turn_index:02d}",
                    "conversation_id": conversation_id,
                    "turn_index": turn_index,
                    "role": "user",
                    "content": f"Turn {index}-{turn_index}",
                    "created_at": "2026-08-23T12:00:03.000000Z",
                    "episode_id": f"episode-{index % graph_episode_count:04d}",
                    "content_sha256": f"turn-{index:04d}-{turn_index:02d}",
                    "metadata": {"index": turn_index},
                }
            )

    bundle = (
        root
        / f"cairn-legacy-export-subset-{conversation_count}-{turns_per_conversation}"
    )
    bundle.mkdir(parents=True)

    stores = []
    for store in PLAN_INPUT_STORES:
        store_records = records[store]
        filename = f"{store}.jsonl"
        payload = "".join(
            canonical_json(record) + "\n" for record in store_records
        ).encode("utf-8")
        (bundle / filename).write_bytes(payload)
        stores.append(
            {
                "store": store,
                "filename": filename,
                "record_count": len(store_records),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    manifest = {
        "schema_version": "cairn-legacy-export/v1",
        "snapshot": {
            "label": "subset-fixture",
            "graph_dump": "/private/dump.tar.gz",
            "restore_image": "falkordb/falkordb",
            "restore_image_digest": "sha256:" + "0" * 64,
            "attic_path": "/private/attic.sqlite3",
            "journal_root": "/private/journal",
            "thoughts_root": "/private/thoughts",
            "openbrain_path": "/private/openbrain",
        },
        "stores": stores,
        "derived_counts": {
            "entity_node": 12,
            "entity_edge": 34,
            "entity_edge_invalidated": 5,
            "entity_edge_expired": 2,
        },
    }
    (bundle / EXPORT_MANIFEST_FILENAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return bundle


def _write_full_plan(
    root: Path,
    *,
    conversation_count: int,
    turns_per_conversation: int,
) -> Path:
    bundle = _write_source_records(
        root / f"source-bundle-{conversation_count}-{turns_per_conversation}",
        conversation_count=conversation_count,
        turns_per_conversation=turns_per_conversation,
    )
    parsed = read_export_bundle(bundle)
    return write_plan(
        map_bundle(parsed),
        parsed,
        root / f"source-plan-{conversation_count}-{turns_per_conversation}",
        checkout_root=root / "repo",
    )


def _jsonl_records(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n") if line]


def _jsonl_raw_lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().splitlines()]


def _selection_key(store: str, legacy_id: str) -> tuple[str, str]:
    if store == "attic-conversation":
        identity = f"{SUBSET_CONVERSATION_IDENTITY_PREFIX}{legacy_id}"
    elif store == "attic-turn":
        identity = f"{SUBSET_TURN_IDENTITY_PREFIX}{legacy_id}"
    else:
        identity = f"{store}:{legacy_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), identity


def _expected_selection(
    source_records: list[dict[str, object]],
) -> tuple[
    set[tuple[str, str]],
    set[str],
    set[str],
    dict[str, list[str]],
]:
    graph = [record for record in source_records if record["store"] == "graph-episode"]
    conversations = [
        record for record in source_records if record["store"] == "attic-conversation"
    ]

    turns_by_conversation: dict[
        str, list[tuple[tuple[str, str], dict[str, object]]]
    ] = defaultdict(list)
    for turn in (
        record for record in source_records if record["store"] == "attic-turn"
    ):
        request = cast(dict[str, object], turn["request"])
        metadata = request.get("metadata")
        assert isinstance(metadata, dict)
        conversation_id = metadata.get("conversation_id")
        assert isinstance(metadata, dict)
        assert isinstance(conversation_id, str)
        turns_by_conversation[conversation_id].append(
            (_selection_key("attic-turn", str(turn["legacy_id"])), turn)
        )

    ranked_conversations = sorted(
        (
            _selection_key("attic-conversation", str(conversation["legacy_id"])),
            conversation,
        )
        for conversation in conversations
        if turns_by_conversation.get(str(conversation["legacy_id"]))
    )
    selected_conversations = [
        conversation
        for _, conversation in ranked_conversations[
            :SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT
        ]
    ]

    selected_turn_records: list[dict[str, object]] = []
    remaining_turn_records: list[tuple[tuple[str, str], dict[str, object]]] = []
    turn_rankings: dict[str, list[str]] = {}

    for conversation in selected_conversations:
        ranked_turns = sorted(
            turns_by_conversation[str(conversation["legacy_id"])],
            key=lambda item: item[0],
        )
        turn_rankings[str(conversation["legacy_id"])] = [
            str(turn["legacy_id"]) for _, turn in ranked_turns
        ]
        selected_turn_records.append(ranked_turns[0][1])
        remaining_turn_records.extend(ranked_turns[1:])

    additional_turn_records = (
        operation
        for _, operation in sorted(remaining_turn_records, key=lambda item: item[0])[
            : SUBSET_PLAN_REQUIRED_TURN_COUNT - len(selected_turn_records)
        ]
    )
    selected_turn_records.extend(additional_turn_records)

    selected = {
        (str(record["store"]), str(record["legacy_id"]))
        for record in (
            *graph,
            *selected_conversations,
            *selected_turn_records,
        )
    }

    return (
        selected,
        {str(conversation["legacy_id"]) for conversation in selected_conversations},
        {str(turn["legacy_id"]) for turn in selected_turn_records},
        turn_rankings,
    )


def _rewrite_source_operations(
    source_plan: Path,
    transform: Callable[[int, dict[str, object]], dict[str, object]],
) -> None:
    operations = _jsonl_records(source_plan / OPERATIONS_FILENAME)
    new_operations = [
        transform(index, record) for index, record in enumerate(operations)
    ]
    raw = b"".join(
        canonical_json(record).encode("utf-8") + b"\n" for record in new_operations
    )
    (source_plan / OPERATIONS_FILENAME).write_bytes(raw)

    manifest = json.loads((source_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    for index, entry in enumerate(manifest["files"]):
        if isinstance(entry, dict) and entry["filename"] == OPERATIONS_FILENAME:
            manifest["files"][index] = {
                **entry,
                "record_count": len(raw.splitlines()),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            break
    (source_plan / PLAN_MANIFEST_FILENAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _replace_source_operations_raw(source_plan: Path, raw: bytes) -> None:
    (source_plan / OPERATIONS_FILENAME).write_bytes(raw)
    manifest = json.loads((source_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    for index, entry in enumerate(manifest["files"]):
        if isinstance(entry, dict) and entry["filename"] == OPERATIONS_FILENAME:
            manifest["files"][index] = {
                **entry,
                "record_count": raw.count(b"\n"),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            break
    (source_plan / PLAN_MANIFEST_FILENAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _plan_manifest_sha256(plan: Path) -> str:
    return hashlib.sha256((plan / PLAN_MANIFEST_FILENAME).read_bytes()).hexdigest()


def _select_subset(
    source_plan: Path,
) -> tuple[Path, tuple[int, int, int], tuple[int, int, int], str]:
    source_manifest_sha256 = _plan_manifest_sha256(source_plan)
    subset_path, source_counts, selected_counts = write_subset_plan(
        source_plan=source_plan,
        output_path=source_plan.parent / "subset",
        checkout_root=source_plan.parent / "repo",
        expected_source_manifest_sha256=source_manifest_sha256,
    )
    return subset_path, source_counts, selected_counts, source_manifest_sha256


def test_subset_plan_selection_is_deterministic_and_materialised(
    tmp_path: Path,
) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=250,
        turns_per_conversation=3,
    )

    source_records = _jsonl_records(source_plan / OPERATIONS_FILENAME)
    subset_plan, source_counts, selected_counts, _ = _select_subset(source_plan)

    expected_selected, selected_conversations, selected_turns, turn_rankings = (
        _expected_selection(source_records)
    )
    subset_records = _jsonl_records(subset_plan / OPERATIONS_FILENAME)
    actual_selected = {
        (record["store"], str(record["legacy_id"])) for record in subset_records
    }

    assert source_counts == (1, 250, 750)
    assert selected_counts == (
        1,
        SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT,
        SUBSET_PLAN_REQUIRED_TURN_COUNT,
    )
    assert actual_selected == expected_selected
    assert len(subset_records) == sum(selected_counts)

    selected_turn_records = [
        record for record in subset_records if record["store"] == "attic-turn"
    ]
    assert len(selected_turn_records) == SUBSET_PLAN_REQUIRED_TURN_COUNT

    selected_turn_conversations = {
        conversation_id
        for record in selected_turn_records
        for request in [cast(dict[str, object], record["request"])]
        for metadata in [request.get("metadata")]
        if isinstance(metadata, dict)
        for conversation_id in [metadata.get("conversation_id")]
        if isinstance(conversation_id, str)
    }
    assert selected_turn_conversations.issubset(selected_conversations)
    for conversation_id in selected_conversations:
        assert conversation_id in selected_turn_conversations
        ranked = turn_rankings[conversation_id]
        assert ranked[0] in selected_turns

    expected_raw = [
        line
        for line, record in zip(
            _jsonl_raw_lines(source_plan / OPERATIONS_FILENAME),
            source_records,
            strict=False,
        )
        if (record["store"], str(record["legacy_id"])) in actual_selected
    ]
    assert _jsonl_raw_lines(subset_plan / OPERATIONS_FILENAME) == expected_raw


def test_subset_plan_manifest_carries_required_subset_metadata(tmp_path: Path) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )
    source_manifest = json.loads((source_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    subset_plan, _, _, _ = _select_subset(source_plan)

    subset_manifest = json.loads((subset_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    assert subset_manifest["schema_version"] == PLAN_SCHEMA_VERSION
    assert subset_manifest["source_plan"]["schema_version"] == PLAN_SCHEMA_VERSION
    assert subset_manifest["source_plan"]["manifest_sha256"] == _plan_manifest_sha256(
        source_plan
    )
    assert subset_manifest["source_bundle"] == source_manifest["source_bundle"]
    assert subset_manifest["target"] == source_manifest["target"]
    assert (
        subset_manifest["idempotency_namespace"]
        == source_manifest["idempotency_namespace"]
    )
    assert subset_manifest["subset"]["profile"] == SUBSET_PLAN_PROFILE
    assert (
        subset_manifest["subset"]["ranking_algorithm"] == SUBSET_PLAN_RANKING_ALGORITHM
    )
    assert subset_manifest["subset"]["required"]["conversations"] == (
        SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT
    )
    assert subset_manifest["subset"]["required"]["turns"] == (
        SUBSET_PLAN_REQUIRED_TURN_COUNT
    )
    assert subset_manifest["subset"]["required"]["additional_turns"] == (
        SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT
    )


def test_subset_plan_is_apply_compatible_and_materialises_the_v1_plan_shape(
    tmp_path: Path,
) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )
    subset_plan, _source_counts, selected_counts, _ = _select_subset(source_plan)

    operations = read_plan(subset_plan)
    assert len(operations) == sum(selected_counts)
    assert (subset_plan / REJECTIONS_FILENAME).read_bytes() == b""
    assert (subset_plan / RECONCILIATIONS_FILENAME).read_bytes() == b""
    assert (subset_plan / ENUMERATIONS_FILENAME).read_bytes() == (
        source_plan / ENUMERATIONS_FILENAME
    ).read_bytes()

    manifest = json.loads((subset_plan / PLAN_MANIFEST_FILENAME).read_bytes())
    assert {entry["filename"] for entry in manifest["files"]} == {
        OPERATIONS_FILENAME,
        REJECTIONS_FILENAME,
        RECONCILIATIONS_FILENAME,
    }
    selected_by_store = {
        "graph-episode": selected_counts[0],
        "attic-conversation": selected_counts[1],
        "attic-turn": selected_counts[2],
    }
    assert manifest["counts"] == {
        store: {
            "exported": selected_by_store.get(store, 0),
            "planned": selected_by_store.get(store, 0),
            "rejected": 0,
            "matched": 0,
            "reconciled": 0,
        }
        for store in PLAN_INPUT_STORES
    }


def test_subset_command_writes_a_materialised_plan_and_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )

    assert (
        main(
            [
                "subset",
                "--source-plan",
                str(source_plan),
                "--output",
                str(tmp_path / "subset-cli"),
                "--expected-source-manifest-sha256",
                _plan_manifest_sha256(source_plan),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["subset_profile"] == SUBSET_PLAN_PROFILE
    assert payload["ranking_algorithm"] == SUBSET_PLAN_RANKING_ALGORITHM
    assert payload["selected_counts"]["conversations"] == (
        SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT
    )
    assert payload["selection_required"]["turns"] == SUBSET_PLAN_REQUIRED_TURN_COUNT
    assert (Path(payload["plan"]) / OPERATIONS_FILENAME).is_file()


def test_subset_plan_refuses_wrong_source_manifest_sha256(tmp_path: Path) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )

    with pytest.raises(MappingError) as error:
        write_subset_plan(
            source_plan=source_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256="0" * 64,
        )
    assert error.value.code == "source_plan_digest_mismatch"


def test_subset_plan_refuses_an_incomplete_source_plan(tmp_path: Path) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )
    expected_manifest_sha256 = _plan_manifest_sha256(source_plan)
    (source_plan / REJECTIONS_FILENAME).unlink()

    with pytest.raises(MappingError) as error:
        write_subset_plan(
            source_plan=source_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=expected_manifest_sha256,
        )
    assert error.value.code == "source_plan_manifest_invalid"
    assert error.value.detail == REJECTIONS_FILENAME


@pytest.mark.parametrize(
    "malformation",
    ["invalid-json", "blank-line", "invalid-uuid", "non-rfc-uuid"],
)
def test_subset_plan_refuses_malformed_source_operations(
    tmp_path: Path,
    malformation: str,
) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )
    if malformation == "invalid-json":
        _replace_source_operations_raw(source_plan, b"not-json\n")
    elif malformation == "blank-line":
        raw = (source_plan / OPERATIONS_FILENAME).read_bytes()
        _replace_source_operations_raw(source_plan, b"\n" + raw)
    else:

        def invalid_uuid(_: int, record: dict[str, object]) -> dict[str, object]:
            record["idempotency_key"] = (
                "not-a-uuid"
                if malformation == "invalid-uuid"
                else "00000000-0000-0000-0000-000000000000"
            )
            return record

        _rewrite_source_operations(source_plan, invalid_uuid)

    with pytest.raises(MappingError) as error:
        write_subset_plan(
            source_plan=source_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=_plan_manifest_sha256(source_plan),
        )
    assert error.value.code == "source_plan_unknown_operation"


def test_subset_plan_rejects_turns_without_a_source_conversation(
    tmp_path: Path,
) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )

    def mutation(_: int, record: dict[str, object]) -> dict[str, object]:
        if record["store"] == "attic-turn":
            request = cast(dict[str, object], record["request"])
            metadata = request.get("metadata")
            assert isinstance(metadata, dict)
            metadata["conversation_id"] = "missing-conversation"
        return record

    _rewrite_source_operations(source_plan, mutation)

    with pytest.raises(MappingError) as error:
        write_subset_plan(
            source_plan=source_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=_plan_manifest_sha256(source_plan),
        )
    assert error.value.code == "source_plan_relationship_invalid"


def test_subset_plan_rejects_unknown_store_or_operation(tmp_path: Path) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )

    def mutation(_: int, record: dict[str, object]) -> dict[str, object]:
        if record["store"] == "graph-episode":
            record["store"] = "journal-event"
        return record

    _rewrite_source_operations(source_plan, mutation)

    with pytest.raises(MappingError) as error:
        write_subset_plan(
            source_plan=source_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=_plan_manifest_sha256(source_plan),
        )
    assert error.value.code == "source_plan_unknown_operation"


def test_subset_plan_refuses_insufficient_conversation_and_turn_candidates(
    tmp_path: Path,
) -> None:
    short_conversation_plan = _write_full_plan(
        tmp_path,
        conversation_count=199,
        turns_per_conversation=3,
    )
    short_turn_plan = _write_full_plan(
        tmp_path,
        conversation_count=200,
        turns_per_conversation=1,
    )

    with pytest.raises(MappingError) as insufficient_conversations:
        write_subset_plan(
            source_plan=short_conversation_plan,
            output_path=tmp_path / "subset",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=_plan_manifest_sha256(
                short_conversation_plan
            ),
        )
    assert insufficient_conversations.value.code == "subset_selection_short"

    with pytest.raises(MappingError) as insufficient_turns:
        write_subset_plan(
            source_plan=short_turn_plan,
            output_path=tmp_path / "subset-2",
            checkout_root=tmp_path / "repo",
            expected_source_manifest_sha256=_plan_manifest_sha256(short_turn_plan),
        )
    assert insufficient_turns.value.code == "subset_selection_short"
    assert insufficient_turns.value.detail == "turns"


def test_subset_command_refuses_output_inside_checkout(tmp_path: Path) -> None:
    source_plan = _write_full_plan(
        tmp_path,
        conversation_count=220,
        turns_per_conversation=3,
    )
    result = main(
        [
            "subset",
            "--source-plan",
            str(source_plan),
            "--output",
            str(_CHECKOUT_ROOT / "tmp-subset-checkout"),
            "--expected-source-manifest-sha256",
            _plan_manifest_sha256(source_plan),
        ]
    )
    assert result == 2
