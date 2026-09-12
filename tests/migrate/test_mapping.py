"""Slice 9 Task 2: the deterministic mapping and ``cairn-migration-plan/v1``
(P-74, P-75, P-78).

The claims proven here are the ones the dry run and every later apply rest
on: the same bundle in gives a byte-identical plan out; every §5.8 rejection
rule fires on a fixture and lands as a counted record rather than a silent
drop; the count arithmetic balances per store; an explicitly matched episode
maps to ``human``/``validated`` and other episodes to
``agent-claim``/``candidate``;
and no fixture body text reaches the report.

Synthetic fixtures only (P-78) — every string here was written for this file.
Module-local, because ``tests`` has no package markers.
"""

import hashlib
import json
from pathlib import Path

import pytest

from cairn.authority import custody
from cairn.screening import SecretScreen
from cairn_migrate.__main__ import main
from cairn_migrate.export import MANIFEST_FILENAME
from cairn_migrate.mapping import (
    BODY_MAX_BYTES,
    ENUMERATIONS_FILENAME,
    METADATA_MAX_BYTES,
    MIGRATION_NAMESPACE,
    OPERATIONS_FILENAME,
    PAYLOAD_MAX_BYTES,
    PLAN_INPUT_STORES,
    PLAN_MANIFEST_FILENAME,
    PLAN_SCHEMA_VERSION,
    RECONCILIATIONS_FILENAME,
    REJECTION_BODY_EMPTY,
    REJECTION_BODY_TOO_LARGE,
    REJECTION_GROUP_ID,
    REJECTION_METADATA_TOO_LARGE,
    REJECTION_PAYLOAD_TOO_LARGE,
    REJECTION_RULES,
    REJECTION_SECRET_SCREEN,
    REJECTION_VALIDATION_FAILED,
    REJECTIONS_FILENAME,
    ExportBundle,
    MappingError,
    MappingResult,
    PlannedOperation,
    Reconciliation,
    Rejection,
    canonical_json,
    conversation_body,
    idempotency_key,
    map_bundle,
    parse_markers,
    read_export_bundle,
    write_plan,
)
from cairn_migrate.report import build_report

# --- fixture vocabulary -------------------------------------------------------
#
# Distinctive markers, so the privacy scan below can prove a specific string
# never reached the report rather than merely that nothing looked wrong.

BODY_MATCHED = "ALPHA-BODY-matched-episode"
BODY_ACTOR_LINKED = "BRAVO-BODY-named-actor-episode"
BODY_UNMATCHED = "CHARLIE-BODY-unmatched-episode"
BODY_TURN = "DELTA-BODY-attic-turn"
TITLE_CONVERSATION = "ECHO-TITLE-attic-conversation"
NAME_EPISODE = "FOXTROT-NAME-episode"
CONTENT_MARKERS = (
    BODY_MATCHED,
    BODY_ACTOR_LINKED,
    BODY_UNMATCHED,
    BODY_TURN,
    TITLE_CONVERSATION,
    NAME_EPISODE,
)

NAMED_ACTOR = "Example Operator"
AGENT_ACTOR = "some-agent"

# A PEM header is the least ambiguous secret shape in the policy and needs no
# entropy to trip: it is a literal the screen matches on sight.
SECRET_BODY = "-----BEGIN RSA PRIVATE KEY-----"

MATCHED_EPISODE_ID = "11111111-1111-4111-8111-111111111111"
ACTOR_LINKED_EPISODE_ID = "22222222-2222-4222-8222-222222222222"
UNMATCHED_EPISODE_ID = "33333333-3333-4333-8333-333333333333"
OPENBRAIN_MATCH_KEY = "openbrain-matched-1"


def episode(
    legacy_id: str,
    body: object,
    *,
    source_description: str = "",
    group_id: str = "cairn",
    created_at: str = "2026-06-13T21:52:17.000000Z",
    valid_at: str | None = None,
    embedding: object = None,
    name: str = NAME_EPISODE,
    source: str = "text",
) -> dict[str, object]:
    return {
        "store": "graph-episode",
        "legacy_id": legacy_id,
        "name": name,
        "episode_body": body,
        "source": source,
        "source_description": source_description,
        "group_id": group_id,
        "created_at": created_at,
        "valid_at": valid_at,
        "embedding": [0.5, 0.25] if embedding is None else embedding,
    }


def journal(
    legacy_id: str,
    actor: str,
    episode_uuid: str,
    *,
    group_id: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "id": legacy_id,
        "actor": actor,
        "source": "cairn-ingest",
        "episode_uuid": episode_uuid,
    }
    if group_id is not None:
        event["group_id"] = group_id
    return {
        "store": "journal-event",
        "legacy_id": legacy_id,
        "path": f"2026/06/13/{legacy_id}.json",
        "received_at": "2026-06-13T21:52:17.000000Z",
        "event": event,
    }


def default_records() -> dict[str, list[dict[str, object]]]:
    """One record of every shape the mapping distinguishes.

    Three episodes: one reaching a repository record through its marker, one
    reaching only a named-actor journal event, and one reaching neither.
    """
    return {
        "graph-episode": [
            episode(
                MATCHED_EPISODE_ID,
                BODY_MATCHED,
                source_description=f"openbrain_id={OPENBRAIN_MATCH_KEY}",
                valid_at="2026-06-13T21:52:16.000000Z",
            ),
            episode(
                ACTOR_LINKED_EPISODE_ID,
                BODY_ACTOR_LINKED,
                source_description="cairn_event_id=event-actor",
            ),
            episode(
                UNMATCHED_EPISODE_ID,
                BODY_UNMATCHED,
                source_description="cairn_event_id=event-agent",
            ),
        ],
        "attic-conversation": [
            {
                "store": "attic-conversation",
                "legacy_id": "conversation-1",
                "source": "claude",
                "title": TITLE_CONVERSATION,
                "started_at": "2026-06-13T21:00:00.000000Z",
                "ended_at": None,
                "created_at": "2026-06-13T21:00:01.000000Z",
                "updated_at": "2026-06-13T21:00:02.000000Z",
                "metadata": {"kind": "synthetic"},
            }
        ],
        "attic-turn": [
            {
                "store": "attic-turn",
                "legacy_id": "turn-1",
                "conversation_id": "conversation-1",
                "turn_index": 0,
                "role": "user",
                "content": BODY_TURN,
                "created_at": "2026-06-13T21:00:03.000000Z",
                "episode_id": MATCHED_EPISODE_ID,
                "content_sha256": "0" * 64,
                "metadata": None,
            }
        ],
        "journal-event": [
            journal("event-agent", AGENT_ACTOR, UNMATCHED_EPISODE_ID),
            journal("event-actor", NAMED_ACTOR, ACTOR_LINKED_EPISODE_ID),
            journal(
                "event-orphan", AGENT_ACTOR, "44444444-4444-4444-8444-444444444444"
            ),
        ],
        "thought-file": [
            {
                "store": "thought-file",
                "legacy_id": OPENBRAIN_MATCH_KEY,
                "path": "2026/04/thought.md",
                "captured_at": "2026-04-21T09:15:00.000000Z",
                "frontmatter": {
                    "openbrain_id": OPENBRAIN_MATCH_KEY,
                    "fingerprint": "fingerprint-matched-1",
                    "captured_at": "2026-04-21T09:15:00.000000Z",
                    "source": "openbrain-app",
                },
                "body": "GOLF-BODY-thought-file",
            }
        ],
        "openbrain-row": [
            {
                "store": "openbrain-row",
                "legacy_id": "openbrain-orphan-1",
                "line_number": 1,
                "table": "thoughts",
                "exported_at": "2026-05-01T00:00:00.000000Z",
                "had_embedding": True,
                "content": "HOTEL-BODY-openbrain-row",
                "content_fingerprint": "fingerprint-orphan-1",
                "created_at": "2026-05-01T00:00:00.000000Z",
                "updated_at": "2026-05-01T00:00:00.000000Z",
                "metadata": None,
            }
        ],
    }


def write_bundle(
    root: Path,
    records: dict[str, list[dict[str, object]]],
    *,
    label: str = "synthetic",
    derived_counts: dict[str, int] | None = None,
) -> Path:
    """A ``cairn-legacy-export/v1`` bundle built directly, not via ``export``.

    Directly on purpose: ``map``'s contract is with the *format*, and a
    bundle assembled here can carry shapes the Task 1 readers would never
    emit — which is exactly what the validation-failure rule must be proven
    against.
    """
    bundle = root / f"cairn-legacy-export-{label}"
    bundle.mkdir(parents=True)
    stores = []
    for store in PLAN_INPUT_STORES:
        filename = f"{store}.jsonl"
        raw = "".join(
            canonical_json(record) + "\n" for record in records.get(store, [])
        ).encode("utf-8")
        (bundle / filename).write_bytes(raw)
        stores.append(
            {
                "store": store,
                "filename": filename,
                "record_count": len(records.get(store, [])),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "cairn-legacy-export/v1",
        "snapshot": {
            "label": label,
            "graph_dump": "/private/dump.tar.gz",
            "restore_image": "falkordb/falkordb",
            "restore_image_digest": "sha256:" + "0" * 64,
            "attic_path": "/private/attic.sqlite3",
            "journal_root": "/private/journal",
            "thoughts_root": "/private/thoughts",
            "openbrain_path": "/private/openbrain-dump.jsonl",
        },
        "stores": stores,
        "derived_counts": derived_counts
        or {
            "entity_node": 12,
            "entity_edge": 34,
            "entity_edge_invalidated": 5,
            "entity_edge_expired": 2,
        },
    }
    (bundle / MANIFEST_FILENAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return bundle


def mapped(
    root: Path, records: dict[str, list[dict[str, object]]]
) -> tuple[ExportBundle, MappingResult]:
    bundle = read_export_bundle(write_bundle(root, records))
    return bundle, map_bundle(bundle)


def operation_for(
    result: MappingResult, store: str, legacy_id: str
) -> PlannedOperation:
    for operation in result.operations:
        if operation.store == store and operation.legacy_id == legacy_id:
            return operation
    raise AssertionError(f"no planned operation for {store}:{legacy_id}")


def rejection_rules(result: MappingResult) -> dict[str, str]:
    return {item.legacy_id: item.rule for item in result.rejections}


# --- the approved mapping -----------------------------------------------------


def test_every_planned_request_targets_the_approved_scope(tmp_path: Path) -> None:
    """§5.1-§5.3: realm ``cairn``, the realm root, classification
    ``internal``, for every record without exception."""
    _bundle, result = mapped(tmp_path, default_records())

    assert result.operations
    for operation in result.operations:
        assert operation.operation == "ingest"
        assert operation.request["scope"] == {"realm": "cairn", "segments": []}
        assert operation.request["classification"] == "internal"


def test_a_matched_episode_is_human_and_validated(tmp_path: Path) -> None:
    """§5.4/§5.5: a graph episode reaching a repository record through its
    approved marker has explicit capture evidence and carries the payload
    required for a validated assertion."""
    _bundle, result = mapped(tmp_path, default_records())

    operation = operation_for(result, "graph-episode", MATCHED_EPISODE_ID)
    assert operation.request["source_type"] == "human"
    assert operation.request["requested_trust"] == "validated"
    payload = operation.request["evidence_payload"]
    assert isinstance(payload, str)
    assert json.loads(payload)["legacy_id"] == MATCHED_EPISODE_ID
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["repository_matches"] == [
        {
            "store": "thought-file",
            "legacy_id": OPENBRAIN_MATCH_KEY,
            "key": "openbrain_id",
            "value": OPENBRAIN_MATCH_KEY,
        }
    ]


def test_a_named_actor_journal_event_stays_candidate(
    tmp_path: Path,
) -> None:
    """Free-form actor text is provenance and grants no trust by itself."""
    _bundle, result = mapped(tmp_path, default_records())

    operation = operation_for(result, "graph-episode", ACTOR_LINKED_EPISODE_ID)
    assert operation.request["source_type"] == "agent-claim"
    assert operation.request["requested_trust"] == "candidate"
    assert "evidence_payload" not in operation.request
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["journal_event_id"] == "event-actor"


def test_actor_case_and_whitespace_variants_stay_candidate(tmp_path: Path) -> None:
    """Normalising an actor name must not turn provenance into authority."""
    for observed in ("Example Operator", "EXAMPLE OPERATOR", " example operator "):
        records = default_records()
        records["journal-event"][1] = journal(
            "event-actor", observed, ACTOR_LINKED_EPISODE_ID
        )
        _bundle, result = mapped(tmp_path / observed.strip(), records)
        operation = operation_for(result, "graph-episode", ACTOR_LINKED_EPISODE_ID)
        assert operation.request["source_type"] == "agent-claim", observed
        assert operation.request["requested_trust"] == "candidate", observed


def test_a_smuggled_title_actor_stays_candidate(tmp_path: Path) -> None:
    """Unattributable text in ``actor`` must remain candidate provenance."""
    records = default_records()
    records["journal-event"][1] = journal(
        "event-actor", "a smuggled episode title", ACTOR_LINKED_EPISODE_ID
    )
    _bundle, result = mapped(tmp_path, records)

    operation = operation_for(result, "graph-episode", ACTOR_LINKED_EPISODE_ID)
    assert operation.request["source_type"] == "agent-claim"
    assert operation.request["requested_trust"] == "candidate"


def test_an_unmatched_episode_is_agent_claim_and_candidate(
    tmp_path: Path,
) -> None:
    """An episode without explicit capture evidence carries no validation."""
    _bundle, result = mapped(tmp_path, default_records())

    operation = operation_for(result, "graph-episode", UNMATCHED_EPISODE_ID)
    assert operation.request["source_type"] == "agent-claim"
    assert operation.request["requested_trust"] == "candidate"
    assert "evidence_payload" not in operation.request


def test_attic_rows_are_candidate_and_always_carry_their_verbatim_row(
    tmp_path: Path,
) -> None:
    """P-74: attic turns and conversations are transcript evidence, so the
    payload is unconditional, but neither is an explicit human capture based
    only on the turn's role."""
    _bundle, result = mapped(tmp_path, default_records())

    for store, legacy_id in (
        ("attic-turn", "turn-1"),
        ("attic-conversation", "conversation-1"),
    ):
        operation = operation_for(result, store, legacy_id)
        assert operation.request["source_type"] == "agent-claim"
        assert operation.request["requested_trust"] == "candidate"
        payload = operation.request["evidence_payload"]
        assert isinstance(payload, str)
        assert json.loads(payload)["legacy_id"] == legacy_id


def test_the_episode_valid_from_prefers_valid_at_over_created_at(
    tmp_path: Path,
) -> None:
    """§5.5: legacy's own validity where it recorded one, the capture time
    otherwise — and never the migration's clock."""
    _bundle, result = mapped(tmp_path, default_records())

    matched = operation_for(result, "graph-episode", MATCHED_EPISODE_ID)
    unmatched = operation_for(result, "graph-episode", UNMATCHED_EPISODE_ID)
    assert _valid_from(matched) == "2026-06-13T21:52:16.000000Z"
    assert _valid_from(unmatched) == "2026-06-13T21:52:17.000000Z"
    assert unmatched.request["observed_at"] == "2026-06-13T21:52:17.000000Z"


def _valid_from(operation: PlannedOperation) -> object:
    facts = operation.request["facts"]
    assert isinstance(facts, list)
    return facts[0]["valid_from"]


def test_the_conversation_header_line_is_fixed_format(tmp_path: Path) -> None:
    """The one generated body in the mapping. Absent fields render empty
    rather than as a Python repr."""
    assert (
        conversation_body({"legacy_id": "c-1", "source": "claude", "title": "t"})
        == "legacy attic conversation id=c-1 source=claude title=t"
    )
    assert conversation_body({"legacy_id": "c-1", "source": None, "title": None}) == (
        "legacy attic conversation id=c-1 source= title="
    )


def test_repository_and_journal_stores_plan_nothing(tmp_path: Path) -> None:
    """§5.7 and P-75: the repository stores enrich and verify, the journal
    enriches, and the derived graph layer is not even a store. None of them
    becomes an independent assertion."""
    _bundle, result = mapped(tmp_path, default_records())

    planned_stores = {operation.store for operation in result.operations}
    assert planned_stores == {"graph-episode", "attic-conversation", "attic-turn"}


# --- idempotency --------------------------------------------------------------


def test_idempotency_keys_are_uuid5_over_the_pinned_namespace() -> None:
    """P-74's key rule, asserted against literals rather than recomputed the
    same way twice: the namespace is permanent, and a change to it would
    orphan every already-applied record from its legacy identity."""
    assert str(MIGRATION_NAMESPACE) == "c4961664-0ded-4ade-aa38-69214bad2678"
    assert (
        idempotency_key("graph-episode", MATCHED_EPISODE_ID)
        == "31e18bca-a343-5507-8366-1ec6da7ce602"
    )
    assert idempotency_key("attic-turn", "turn-1") != idempotency_key(
        "attic-conversation", "turn-1"
    )


def test_planned_operations_carry_their_idempotency_key(tmp_path: Path) -> None:
    _bundle, result = mapped(tmp_path, default_records())

    for operation in result.operations:
        assert operation.idempotency_key == idempotency_key(
            operation.store, operation.legacy_id
        )


# --- §5.8 rejection rules -----------------------------------------------------


def rejecting_records() -> dict[str, list[dict[str, object]]]:
    """One fixture per rejection rule, all in one bundle.

    Together in one bundle rather than one each, because the rules run in a
    fixed order and a rule that fires only when nothing else is present would
    be an untested ordering.
    """
    records = default_records()
    records["graph-episode"].extend(
        [
            episode("reject-group", "body", group_id="other-realm"),
            episode("reject-empty", "   "),
            episode("reject-body-size", "x" * (BODY_MAX_BYTES + 1)),
            episode(
                "reject-metadata-size",
                "body",
                source_description="m" * (METADATA_MAX_BYTES + 1),
            ),
            episode(
                "reject-payload-size",
                "body",
                source_description=f"openbrain_id={OPENBRAIN_MATCH_KEY}",
                embedding=[0.123456] * 150000,
            ),
            episode("reject-validation", "body", created_at="not-a-timestamp"),
            episode("reject-secret", SECRET_BODY),
        ]
    )
    return records


def test_every_rejection_rule_fires_and_is_counted(tmp_path: Path) -> None:
    """§5.8 in full. Each rule catches its fixture, and the whole closed
    vocabulary is exercised — a rule nothing can trip is a rule nobody can
    trust."""
    _bundle, result = mapped(tmp_path, rejecting_records())

    rules = rejection_rules(result)
    assert rules["reject-group"] == REJECTION_GROUP_ID
    assert rules["reject-empty"] == REJECTION_BODY_EMPTY
    assert rules["reject-body-size"] == REJECTION_BODY_TOO_LARGE
    assert rules["reject-metadata-size"] == REJECTION_METADATA_TOO_LARGE
    assert rules["reject-payload-size"] == REJECTION_PAYLOAD_TOO_LARGE
    assert rules["reject-validation"] == REJECTION_VALIDATION_FAILED
    assert rules["reject-secret"] == REJECTION_SECRET_SCREEN
    assert set(rules.values()) == set(REJECTION_RULES)


def test_a_rejected_record_is_never_planned(tmp_path: Path) -> None:
    """The whole point of a rejection: it is a counted record, and it is not
    a request anybody can send."""
    _bundle, result = mapped(tmp_path, rejecting_records())

    planned = {operation.legacy_id for operation in result.operations}
    for rejection in result.rejections:
        assert rejection.legacy_id not in planned


def test_a_secret_rejection_carries_its_rule_and_no_content(
    tmp_path: Path,
) -> None:
    """P-78 admits a rule tally. The rule identity travels; the field's text
    does not, and neither does the secret."""
    _bundle, result = mapped(tmp_path, rejecting_records())

    (secret,) = [
        item for item in result.rejections if item.rule == REJECTION_SECRET_SCREEN
    ]
    assert secret.detail is not None
    assert secret.detail.startswith("cairn.secret/v1")
    assert SECRET_BODY not in canonical_json(
        {"store": secret.store, "legacy_id": secret.legacy_id, "detail": secret.detail}
    )


def test_a_turn_carrying_its_real_content_digest_is_planned(tmp_path: Path) -> None:
    """I-96 at the migration seam: the envelope quotes the exporter's
    ``content_sha256`` in ingest metadata and repeats it in the verbatim-row
    payload, and a realistic digest trips ``HexHighEntropyString`` — the
    take2 dry run rejected 24,200 clean-bodied turns on exactly this. The
    guard proves the digest fires on its own; the attested-digest mask is
    what plans the turn anyway."""
    records = default_records()
    turn = records["attic-turn"][0]
    digest = hashlib.sha256(str(turn["content"]).encode("utf-8")).hexdigest()
    turn["content_sha256"] = digest
    assert (
        SecretScreen().screen(
            "metadata",
            json.dumps({"content_sha256": digest}, separators=(",", ":")),
        )
        != ()
    )

    _bundle, result = mapped(tmp_path, records)

    operation_for(result, "attic-turn", "turn-1")
    assert "turn-1" not in rejection_rules(result)


def test_a_turn_with_a_hex_row_id_is_planned(tmp_path: Path) -> None:
    """The dry-run finding over merged main (23 August 2026, ruled by the operator
    in session): the attic row id is itself a 64-hex value, independent of
    the attested content digest, and quoted in canonical metadata it trips
    ``HexHighEntropyString`` on its own — all 24,939 turns rejected on
    exactly this. The guard proves the id fires as metadata; the
    store-prefixed envelope encoding is what plans the turn anyway.
    Identity stays raw: the plan operation and the verbatim-row payload
    keep the exporter's id."""
    records = default_records()
    turn = records["attic-turn"][0]
    row_id = hashlib.sha256(b"attic row id, not the content digest").hexdigest()
    turn["legacy_id"] = row_id
    turn["content_sha256"] = hashlib.sha256(
        str(turn["content"]).encode("utf-8")
    ).hexdigest()
    assert (
        SecretScreen().screen(
            "metadata", json.dumps({"legacy_id": row_id}, separators=(",", ":"))
        )
        != ()
    )

    _bundle, result = mapped(tmp_path, records)

    operation = operation_for(result, "attic-turn", row_id)
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["legacy_id"] == f"attic-turn:{row_id}"
    payload = operation.request["evidence_payload"]
    assert isinstance(payload, str)
    assert json.loads(payload)["legacy_id"] == row_id
    assert row_id not in rejection_rules(result)


def test_a_conversation_with_an_identifier_heavy_legacy_metadata_is_planned(
    tmp_path: Path,
) -> None:
    """I-97 at the migration seam: the verbatim conversation row carries its
    legacy ``metadata`` inside the evidence payload, and a path-shaped
    ``backfill_source`` slug in the base64 alphabet trips the statistical
    rules there — all 119 take2 conversation rejections were this class.
    The guard proves the slug fires as ordinary text; the payload's
    pattern-rules-only matrix is what plans the conversation anyway."""
    records = default_records()
    conversation = records["attic-conversation"][0]
    slug = (
        "imports/openbrain/backfill/2026-05-01/"
        "DeepWorkSessionsAndContextRecoveryNotes+capture9081Zx4Vb8Ln3Jm5Tp0Ys6Ue2Ia9"
    )
    conversation["metadata"] = {"backfill_source": slug}
    assert SecretScreen().screen("x", f'"{slug}"') != ()

    _bundle, result = mapped(tmp_path, records)

    operation_for(result, "attic-conversation", "conversation-1")
    assert "conversation-1" not in rejection_rules(result)


def test_the_size_limits_track_the_custody_module() -> None:
    """The limits are quoted here because they are private there. A limit
    that moves must fail this test rather than silently plan a record the
    server will refuse."""
    assert BODY_MAX_BYTES == custody._BODY_MAX_BYTES
    assert METADATA_MAX_BYTES == custody._METADATA_MAX_BYTES
    assert PAYLOAD_MAX_BYTES == custody._PAYLOAD_MAX_LENGTH


# --- zero silent drops --------------------------------------------------------


def test_the_count_arithmetic_balances_for_every_store(tmp_path: Path) -> None:
    """Zero silent drops, asserted as arithmetic rather than asserted in
    prose: an assertion store's exports are planned or rejected, an
    enrichment store's are matched or reconciled, and nothing is anywhere
    else."""
    bundle, result = mapped(tmp_path, rejecting_records())

    report = build_report(result, bundle)
    balance = report["balance"]
    assert isinstance(balance, dict)
    assert balance["balanced"] is True
    stores = balance["stores"]
    assert isinstance(stores, dict)
    for store in PLAN_INPUT_STORES:
        entry = stores[store]
        assert entry["accounted"] == entry["exported"], store


def test_unmatched_repository_and_journal_records_become_reconciliations(
    tmp_path: Path,
) -> None:
    """§5.7: a repository record with no live-graph counterpart is the operator's
    ruling, not a silent migration and not a silent drop."""
    _bundle, result = mapped(tmp_path, default_records())

    reconciled = {
        (item.store, item.legacy_id, item.rule) for item in result.reconciliations
    }
    assert ("openbrain-row", "openbrain-orphan-1", "no_graph_counterpart") in reconciled
    assert ("journal-event", "event-orphan", "unmatched_journal_event") in reconciled
    assert (
        "thought-file",
        OPENBRAIN_MATCH_KEY,
        "no_graph_counterpart",
    ) not in reconciled


def test_a_repository_record_matched_to_a_rejected_episode_still_counts(
    tmp_path: Path,
) -> None:
    """Matching is computed before admission and independently of it.

    The episode's fate is reported under its own rejection rule; smearing it
    across a second store's reconciliation list would report one problem
    twice and make the arithmetic depend on rule order.
    """
    records = default_records()
    records["graph-episode"] = [
        episode(
            "rejected-but-matched",
            "",
            source_description=f"openbrain_id={OPENBRAIN_MATCH_KEY}",
        )
    ]
    _bundle, result = mapped(tmp_path, records)

    assert rejection_rules(result)["rejected-but-matched"] == REJECTION_BODY_EMPTY
    assert not [item for item in result.reconciliations if item.store == "thought-file"]


def test_complementary_repository_records_both_match_one_episode(
    tmp_path: Path,
) -> None:
    """Review finding P1, the real-bundle blocker: a thought file's
    ``openbrain_id`` *is* its openbrain row's ``id``, so the two stores'
    records legitimately share their identity. Both must match, both must
    enrich, and neither may abort the map or land in reconciliation."""
    records = default_records()
    records["openbrain-row"] = [
        {
            **records["openbrain-row"][0],
            "legacy_id": OPENBRAIN_MATCH_KEY,
        }
    ]
    _bundle, result = mapped(tmp_path, records)

    operation = operation_for(result, "graph-episode", MATCHED_EPISODE_ID)
    assert operation.request["requested_trust"] == "validated"
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    matches = metadata["repository_matches"]
    assert isinstance(matches, list)
    assert {(match["store"], match["legacy_id"]) for match in matches} == {
        ("thought-file", OPENBRAIN_MATCH_KEY),
        ("openbrain-row", OPENBRAIN_MATCH_KEY),
    }
    assert all(match["key"] == "openbrain_id" for match in matches)
    assert not [
        item
        for item in result.reconciliations
        if item.store in ("thought-file", "openbrain-row")
    ]


def test_an_unrelated_marker_collision_never_escalates_trust(
    tmp_path: Path,
) -> None:
    """Review finding P2, adversarial: a repository identity appearing as
    the value of a non-approved marker — an event id, an actor — must not
    produce ``human``/``validated``. Only the approved key names match."""
    records = default_records()
    records["graph-episode"].append(
        episode(
            "collision-event-id",
            "collision body one",
            source_description=f"cairn_event_id={OPENBRAIN_MATCH_KEY}",
        )
    )
    records["graph-episode"].append(
        episode(
            "collision-actor",
            "collision body two",
            source_description=f"actor={OPENBRAIN_MATCH_KEY}",
        )
    )
    _bundle, result = mapped(tmp_path, records)

    for legacy_id in ("collision-event-id", "collision-actor"):
        operation = operation_for(result, "graph-episode", legacy_id)
        assert operation.request["source_type"] == "agent-claim"
        assert operation.request["requested_trust"] == "candidate"
        metadata = operation.request["metadata"]
        assert isinstance(metadata, dict)
        assert "repository_matches" not in metadata


def test_a_cross_domain_key_collision_never_matches(tmp_path: Path) -> None:
    """Re-review finding R1, adversarial: the §5.7 relation is
    field-specific. An ``openbrain_id`` marker equal to some thought's
    *fingerprint* — or a ``fingerprint`` marker equal to a row's
    ``content_fingerprint`` — is a collision across incompatible domains
    and must match nothing."""
    records = default_records()
    frontmatter = records["thought-file"][0]["frontmatter"]
    assert isinstance(frontmatter, dict)
    frontmatter["fingerprint"] = "XDOMAIN-collide"
    records["graph-episode"].append(
        episode(
            "cross-domain-one",
            "cross domain body one",
            source_description="openbrain_id=XDOMAIN-collide",
        )
    )
    records["openbrain-row"][0]["content_fingerprint"] = "XDOMAIN-collide-2"
    records["graph-episode"].append(
        episode(
            "cross-domain-two",
            "cross domain body two",
            source_description="fingerprint=XDOMAIN-collide-2",
        )
    )
    _bundle, result = mapped(tmp_path, records)

    for legacy_id in ("cross-domain-one", "cross-domain-two"):
        operation = operation_for(result, "graph-episode", legacy_id)
        assert operation.request["source_type"] == "agent-claim"
        assert operation.request["requested_trust"] == "candidate"
        metadata = operation.request["metadata"]
        assert isinstance(metadata, dict)
        assert "repository_matches" not in metadata


def test_a_same_domain_match_still_works_per_field(tmp_path: Path) -> None:
    """The positive side of R1's restriction: each approved marker matches
    its own declared field — a ``fingerprint`` marker reaches the thought
    that declared that fingerprint, a ``content_fingerprint`` marker the
    row that declared it."""
    records = default_records()
    records["graph-episode"].append(
        episode(
            "fingerprint-matched",
            "fingerprint matched body",
            source_description="fingerprint=fingerprint-matched-1",
        )
    )
    records["graph-episode"].append(
        episode(
            "content-fingerprint-matched",
            "content fingerprint matched body",
            source_description="content_fingerprint=fingerprint-orphan-1",
        )
    )
    _bundle, result = mapped(tmp_path, records)

    thought = operation_for(result, "graph-episode", "fingerprint-matched")
    assert thought.request["requested_trust"] == "validated"
    metadata = thought.request["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["repository_matches"] == [
        {
            "store": "thought-file",
            "legacy_id": OPENBRAIN_MATCH_KEY,
            "key": "fingerprint",
            "value": "fingerprint-matched-1",
        }
    ]
    row = operation_for(result, "graph-episode", "content-fingerprint-matched")
    assert row.request["requested_trust"] == "validated"
    row_metadata = row.request["metadata"]
    assert isinstance(row_metadata, dict)
    assert row_metadata["repository_matches"] == [
        {
            "store": "openbrain-row",
            "legacy_id": "openbrain-orphan-1",
            "key": "content_fingerprint",
            "value": "fingerprint-orphan-1",
        }
    ]


def test_a_foreign_group_journal_event_never_grants_validation(
    tmp_path: Path,
) -> None:
    """Re-review finding R2: §5.8 rejects any record carrying a group other
    than ``cairn``, enrichment stores included. A foreign-group journal
    event with a named actor must not grant ``human``/``validated``, must
    land as a counted rejection, and must not haunt the reconciliation
    list as a second entry."""
    records = default_records()
    records["journal-event"][1] = journal(
        "event-actor", NAMED_ACTOR, ACTOR_LINKED_EPISODE_ID, group_id="other-realm"
    )
    _bundle, result = mapped(tmp_path, records)

    operation = operation_for(result, "graph-episode", ACTOR_LINKED_EPISODE_ID)
    assert operation.request["source_type"] == "agent-claim"
    assert operation.request["requested_trust"] == "candidate"
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    assert "journal_event_id" not in metadata
    assert rejection_rules(result)["event-actor"] == REJECTION_GROUP_ID
    reconciled = {item.legacy_id for item in result.reconciliations}
    assert "event-actor" not in reconciled


def test_a_foreign_group_journal_events_values_still_enumerate(
    tmp_path: Path,
) -> None:
    """Re-review finding P2 (second pass, 4045188): rejection removes a
    journal event from matching, trust and reconciliation, but not from
    the inventory of what the snapshot contained. Its actor and source
    values still belong in the private enumeration; they must never reach
    the repository-bound report as raw values."""
    sentinel_actor = "ROMEO-sentinel-actor"
    sentinel_source = "SIERRA-sentinel-source"
    records = default_records()
    foreign = journal(
        "event-actor", NAMED_ACTOR, ACTOR_LINKED_EPISODE_ID, group_id="other-realm"
    )
    event = foreign["event"]
    assert isinstance(event, dict)
    event["actor"] = sentinel_actor
    event["source"] = sentinel_source
    records["journal-event"][1] = foreign
    bundle = read_export_bundle(write_bundle(tmp_path, records))
    result = map_bundle(bundle)

    # 1: the linked episode is unaffected — still agent-claim/candidate.
    operation = operation_for(result, "graph-episode", ACTOR_LINKED_EPISODE_ID)
    assert operation.request["source_type"] == "agent-claim"
    assert operation.request["requested_trust"] == "candidate"

    # 2: counted exactly once, as group_id_not_cairn.
    rejections = [item for item in result.rejections if item.legacy_id == "event-actor"]
    assert len(rejections) == 1
    assert rejections[0].rule == REJECTION_GROUP_ID

    # 3: absent from provenance and reconciliation.
    metadata = operation.request["metadata"]
    assert isinstance(metadata, dict)
    assert "journal_event_id" not in metadata
    assert "event-actor" not in {item.legacy_id for item in result.reconciliations}

    # 4: both sentinels appear in the private enumeration.
    assert result.enumerations["actor"]["journal-event"][sentinel_actor] == 1
    assert result.enumerations["source"]["journal-event"][sentinel_source] == 1

    # 5: the repository-bound report carries neither raw sentinel.
    report = build_report(result, bundle)
    serialised = json.dumps(report, sort_keys=True)
    assert f'"{sentinel_actor}"' not in serialised
    assert f'"{sentinel_source}"' not in serialised


def test_a_foreign_group_journal_rejection_keeps_the_arithmetic_balanced(
    tmp_path: Path,
) -> None:
    """R2's zero-silent-drop half: a rejected enrichment record occupies
    exactly one accounting column, and the store still balances."""
    records = default_records()
    records["journal-event"][1] = journal(
        "event-actor", NAMED_ACTOR, ACTOR_LINKED_EPISODE_ID, group_id="other-realm"
    )
    bundle, result = mapped(tmp_path, records)

    report = build_report(result, bundle)
    balance = report["balance"]
    assert isinstance(balance, dict)
    assert balance["balanced"] is True
    stores = balance["stores"]
    assert isinstance(stores, dict)
    assert stores["journal-event"]["accounted"] == stores["journal-event"]["exported"]


def test_mapping_error_codes_are_a_closed_vocabulary() -> None:
    """Re-review finding R4, the remaining half of S5: the documented code
    vocabulary is enforced, not merely described."""
    with pytest.raises(ValueError, match="unknown mapping error code"):
        MappingError("not-a-code")


def test_plan_value_vocabularies_are_enforced_at_construction() -> None:
    """Review finding S5: the plan is a persistence format, so its
    discriminants are closed vocabularies enforced where the value is
    born."""
    with pytest.raises(MappingError) as operation_error:
        PlannedOperation(
            store="journal-event",
            legacy_id="x",
            idempotency_key=idempotency_key("graph-episode", "x"),
            operation="ingest",
            request={},
        )
    assert operation_error.value.code == "plan_value_invalid"
    with pytest.raises(MappingError) as rejection_error:
        Rejection(store="graph-episode", legacy_id="x", rule="not-a-rule", detail=None)
    assert rejection_error.value.code == "plan_value_invalid"
    with pytest.raises(MappingError) as reconciliation_error:
        Reconciliation(store="not-a-store", legacy_id="x", rule="no_graph_counterpart")
    assert reconciliation_error.value.code == "plan_value_invalid"


def test_markers_parse_the_confirmed_pipe_separated_format() -> None:
    """The format confirmed against the productive graph, 21 August 2026:
    ``" | "``-separated ``key=value`` fields, markers leading."""
    description = (
        "cairn_event_id=e-1 | content_sha256=abc | cairn_episode_uuid=u-1"
        " | type=memory_input | source=smoke-test | actor=Example Operator"
        " | originally_from=cairn-ingest"
    )
    markers = parse_markers(description)
    assert markers["cairn_event_id"] == "e-1"
    assert markers["cairn_episode_uuid"] == "u-1"
    assert markers["actor"] == "Example Operator"


def test_marker_values_keep_their_internal_spaces() -> None:
    """The corpus smuggles whole titles into some ``actor`` fields; a
    whitespace split would shear those into garbage tokens."""
    assert parse_markers("cairn_event_id=e-1 | actor=a smuggled title") == {
        "cairn_event_id": "e-1",
        "actor": "a smuggled title",
    }


def test_prose_descriptions_parse_to_nothing() -> None:
    """161 of the measured corpus's 611 descriptions are free-form prose.
    They must yield no markers — not one garbage pseudo-marker each."""
    assert parse_markers("a prose description, 2026-07-04") == {}
    assert parse_markers("prose where a = sign appears mid-sentence") == {}
    assert parse_markers(None) == {}


def test_the_first_occurrence_of_a_marker_key_wins() -> None:
    assert parse_markers("cairn_event_id=first | cairn_event_id=second") == {
        "cairn_event_id": "first"
    }


# --- the plan artefact --------------------------------------------------------


def test_the_same_bundle_maps_to_a_byte_identical_plan(tmp_path: Path) -> None:
    """``map`` is pure, and this is what purity is for: a re-run derives the
    same idempotency keys, so I-27 replays them instead of duplicating."""
    bundle = read_export_bundle(write_bundle(tmp_path / "in", rejecting_records()))
    first = write_plan(
        map_bundle(bundle), bundle, tmp_path / "one", checkout_root=tmp_path / "repo"
    )
    second = write_plan(
        map_bundle(bundle), bundle, tmp_path / "two", checkout_root=tmp_path / "repo"
    )

    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_the_plan_manifest_describes_what_was_written(tmp_path: Path) -> None:
    bundle = read_export_bundle(write_bundle(tmp_path / "in", default_records()))
    result = map_bundle(bundle)
    plan = write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")

    manifest = json.loads((plan / PLAN_MANIFEST_FILENAME).read_bytes())
    assert manifest["schema_version"] == PLAN_SCHEMA_VERSION
    assert manifest["source_bundle"]["manifest_sha256"] == bundle.manifest_sha256
    assert manifest["idempotency_namespace"] == str(MIGRATION_NAMESPACE)
    assert manifest["target"] == {
        "realm": "cairn",
        "segments": [],
        "classification": "internal",
    }
    for entry in manifest["files"]:
        raw = (plan / entry["filename"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert len(raw) == entry["bytes"]
        assert raw.count(b"\n") == entry["record_count"]


def test_the_plan_files_carry_one_record_each(tmp_path: Path) -> None:
    bundle = read_export_bundle(write_bundle(tmp_path / "in", rejecting_records()))
    result = map_bundle(bundle)
    plan = write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")

    operations = _jsonl(plan / OPERATIONS_FILENAME)
    assert len(operations) == len(result.operations)
    assert all(line["operation"] == "ingest" for line in operations)
    rejections = _jsonl(plan / REJECTIONS_FILENAME)
    assert len(rejections) == len(result.rejections)
    assert all(
        set(line) == {"store", "legacy_id", "rule", "detail"} for line in rejections
    )
    reconciliations = _jsonl(plan / RECONCILIATIONS_FILENAME)
    assert len(reconciliations) == len(result.reconciliations)
    assert all(set(line) == {"store", "legacy_id", "rule"} for line in reconciliations)


def _jsonl(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n") if line]


def test_the_plan_carries_the_private_enumerations_file(tmp_path: Path) -> None:
    """Review findings S1/P3: the raw actor/source enumerations are a
    private plan file, digest-pinned in the manifest — the committable
    report carries tallies only."""
    bundle = read_export_bundle(write_bundle(tmp_path / "in", default_records()))
    result = map_bundle(bundle)
    plan = write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")

    raw = (plan / ENUMERATIONS_FILENAME).read_bytes()
    payload = json.loads(raw)
    assert payload["actor"]["journal-event"][NAMED_ACTOR] == 1
    assert payload["actor"]["journal-event"][AGENT_ACTOR] == 2
    assert payload["source"]["graph-episode"]["text"] == 3
    assert payload["source"]["attic-conversation"]["claude"] == 1
    manifest = json.loads((plan / PLAN_MANIFEST_FILENAME).read_bytes())
    assert manifest["enumerations"]["filename"] == ENUMERATIONS_FILENAME
    assert manifest["enumerations"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["enumerations"]["bytes"] == len(raw)


def test_the_rejection_and_reconciliation_files_carry_no_content(
    tmp_path: Path,
) -> None:
    """P-78 at the file level: only ``operations.jsonl`` holds legacy text,
    which is what lets the report built from the other two be committed."""
    bundle = read_export_bundle(write_bundle(tmp_path / "in", rejecting_records()))
    plan = write_plan(
        map_bundle(bundle), bundle, tmp_path / "out", checkout_root=tmp_path / "repo"
    )

    for filename in (REJECTIONS_FILENAME, RECONCILIATIONS_FILENAME):
        text = (plan / filename).read_text(encoding="utf-8")
        for marker in (*CONTENT_MARKERS, SECRET_BODY):
            assert marker not in text, f"{marker} leaked into {filename}"


def test_a_plan_output_inside_the_checkout_refuses(tmp_path: Path) -> None:
    """``operations.jsonl`` carries legacy bodies. A mistyped ``--output``
    is the one plausible way those reach this repository."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    bundle = read_export_bundle(write_bundle(tmp_path / "in", default_records()))
    with pytest.raises(MappingError) as error:
        write_plan(map_bundle(bundle), bundle, checkout / "out", checkout_root=checkout)
    assert error.value.code == "output_inside_checkout"


def test_an_existing_plan_is_never_overwritten(tmp_path: Path) -> None:
    bundle = read_export_bundle(write_bundle(tmp_path / "in", default_records()))
    result = map_bundle(bundle)
    write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")
    with pytest.raises(MappingError) as error:
        write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")
    assert error.value.code == "plan_exists"


def test_a_plan_directory_without_a_manifest_is_incomplete(tmp_path: Path) -> None:
    bundle = read_export_bundle(write_bundle(tmp_path / "in", default_records()))
    result = map_bundle(bundle)
    plan = write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")
    (plan / PLAN_MANIFEST_FILENAME).unlink()
    with pytest.raises(MappingError) as error:
        write_plan(result, bundle, tmp_path / "out", checkout_root=tmp_path / "repo")
    assert error.value.code == "plan_incomplete"


# --- reading the bundle -------------------------------------------------------


def test_a_bundle_whose_digest_disagrees_refuses(tmp_path: Path) -> None:
    """A plan derived from a corrupted bundle would carry that corruption
    into the catalogue under an idempotency key that makes it permanent."""
    bundle_path = write_bundle(tmp_path, default_records())
    (bundle_path / "attic-turn.jsonl").write_bytes(b'{"legacy_id":"tampered"}\n')
    with pytest.raises(MappingError) as error:
        read_export_bundle(bundle_path)
    assert error.value.code == "bundle_digest_mismatch"


def test_a_missing_store_refuses(tmp_path: Path) -> None:
    bundle_path = write_bundle(tmp_path, default_records())
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_bytes())
    manifest["stores"] = [
        entry for entry in manifest["stores"] if entry["store"] != "journal-event"
    ]
    (bundle_path / MANIFEST_FILENAME).write_bytes(
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    )
    with pytest.raises(MappingError) as error:
        read_export_bundle(bundle_path)
    assert error.value.code == "bundle_store_missing"


def test_a_foreign_schema_version_refuses(tmp_path: Path) -> None:
    bundle_path = write_bundle(tmp_path, default_records())
    manifest = json.loads((bundle_path / MANIFEST_FILENAME).read_bytes())
    manifest["schema_version"] = "cairn-legacy-export/v2"
    (bundle_path / MANIFEST_FILENAME).write_bytes(
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    )
    with pytest.raises(MappingError) as error:
        read_export_bundle(bundle_path)
    assert error.value.code == "bundle_manifest_invalid"


def test_a_missing_bundle_refuses(tmp_path: Path) -> None:
    with pytest.raises(MappingError) as error:
        read_export_bundle(tmp_path / "absent")
    assert error.value.code == "bundle_unreadable"


# --- the command line ---------------------------------------------------------


def test_the_map_command_writes_a_plan_and_a_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = write_bundle(tmp_path / "in", rejecting_records())
    assert (
        main(["map", "--bundle", str(bundle_path), "--output", str(tmp_path / "out")])
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["planned"] > 0
    assert report["rejected"] == len(REJECTION_RULES)
    plan = Path(str(report["plan"]))
    assert (plan / PLAN_MANIFEST_FILENAME).is_file()
    assert Path(str(report["report"])).is_file()


def test_the_map_command_refuses_an_output_inside_the_checkout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = write_bundle(tmp_path / "in", default_records())
    assert (
        main(["map", "--bundle", str(bundle_path), "--output", str(Path.cwd() / "out")])
        == 2
    )
    assert json.loads(capsys.readouterr().err)["code"] == "output_inside_checkout"


def test_the_map_command_reports_a_refusal_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal contract Task 1 hardened, extended to ``map``: a code on
    stderr and exit 2, never a traceback."""
    assert (
        main(["map", "--bundle", str(tmp_path / "absent"), "--output", str(tmp_path)])
        == 2
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["status"] == "error"
    assert failure["code"] == "bundle_unreadable"
