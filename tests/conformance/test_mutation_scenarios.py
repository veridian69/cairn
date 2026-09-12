"""§6.6 conformance: ingest, promotion and invalidation authority
(MUT-01–10).
"""

import sqlite3
from pathlib import Path

import pytest
from conftest import (
    COMPONENT,
    OTHER_REALM,
    OTHER_REPO,
    REALM,
    REPO,
    Instance,
    Running,
    ingest_body,
    scenario,
    serve,
)

from cairn.catalogue.sqlite import CATALOGUE_FILENAME

EXTERNAL_EVIDENCE = {
    "external_uri": "https://ci.example.org/run/42",
    "payload_digest": "0" * 64,
}
DEEP = [REPO, COMPONENT]


def fact_row(instance: Instance, fact_id: str) -> dict[str, object]:
    with sqlite3.connect(instance.data_path / CATALOGUE_FILENAME) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT trust, classification, scope_segments, derived_from "
            "FROM facts WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


async def ingest_fact(
    running: Running,
    token: str,
    *,
    segments: list[dict[str, str]] | None = None,
    classification: str = "internal",
) -> str:
    outcome = await running.client.ingest(
        ingest_body(
            segments=segments if segments is not None else [REPO],
            classification=classification,
        ),
        credential=token,
    )
    assert outcome.result is not None, outcome.text
    fact_ids = outcome.result["fact_ids"]
    assert isinstance(fact_ids, list)
    assert len(fact_ids) == 1
    return str(fact_ids[0])


def promote_body(
    fact_id: str,
    *,
    target_segments: list[dict[str, str]] | None = None,
    target_realm: str = REALM,
    target_classification: str | None = None,
    evidence: dict[str, str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "fact_ids": [fact_id],
        "evidence": evidence if evidence is not None else dict(EXTERNAL_EVIDENCE),
        "reason": "verified by an independent check",
    }
    if target_segments is not None:
        body["target_scope"] = {"realm": target_realm, "segments": target_segments}
    if target_classification is not None:
        body["target_classification"] = target_classification
    return body


@scenario("MUT-01")
@pytest.mark.anyio
async def test_mut_01_ingest_and_invalidation_preserve_history(
    tmp_path: Path,
    transport: str,
) -> None:
    """An authorised ingest and a subsequent invalidation preserve
    provenance and history: the fact row survives its invalidation, and
    both actions are on the audit chain."""
    instance = Instance(tmp_path, "MUT-01")
    token = instance.add_actor(segments=[REPO], operations=["ingest", "invalidate"])

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token)
        invalidated = await running.client.invalidate(
            {"fact_ids": [fact_id], "reason": "superseded by a newer measurement"},
            credential=token,
        )

    assert invalidated.result is not None
    assert invalidated.result["fact_ids"] == [fact_id]
    assert instance.count("facts") == 1
    assert instance.count("assertions") == 1
    assert instance.count("fact_invalidations") == 1
    actions = [event["action_code"] for event in instance.events("realm")]
    assert actions[-2:] == ["ingest", "invalidate"]
    outcomes = [event["outcome"] for event in instance.events("realm")]
    assert outcomes[-2:] == ["allow", "allow"]


@scenario("MUT-02")
@pytest.mark.anyio
async def test_mut_02_replay_creates_no_duplicate(
    tmp_path: Path, transport: str
) -> None:
    """Replaying an idempotency identity creates no duplicate assertion,
    fact or mutation — the receipt is returned, marked replayed."""
    instance = Instance(tmp_path, "MUT-02")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])
    key = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"

    async with serve(instance, transport) as running:
        first = await running.client.ingest(
            ingest_body(), credential=token, idempotency_key=key
        )
        second = await running.client.ingest(
            ingest_body(), credential=token, idempotency_key=key
        )

    assert first.outcome == "committed"
    assert second.outcome == "replayed"
    assert second.result == first.result
    assert second.mutation_receipt == first.mutation_receipt
    assert instance.count("assertions") == 1
    assert instance.count("facts") == 1
    assert instance.count("idempotency_records") == 1
    replay_event = instance.events("realm")[-1]
    assert replay_event["reason_code"] == "idempotent_replay"


@scenario("MUT-03")
@pytest.mark.anyio
async def test_mut_03_same_scope_promotion_derives_validated(
    tmp_path: Path,
    transport: str,
) -> None:
    """A same-scope promotion creates a validated derived fact and leaves
    the source unchanged."""
    instance = Instance(tmp_path, "MUT-03")
    token = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token)
        promoted = await running.client.promote(
            promote_body(fact_id, target_segments=[REPO]), credential=token
        )

    assert promoted.outcome == "committed"
    assert promoted.result is not None
    pairs = promoted.result["promotions"]
    assert isinstance(pairs, list)
    assert len(pairs) == 1
    assert pairs[0]["source_fact_id"] == fact_id
    derived = fact_row(instance, pairs[0]["derived_fact_id"])
    assert derived["trust"] == "validated"
    assert derived["derived_from"] == fact_id
    source = fact_row(instance, fact_id)
    assert source["trust"] == "candidate"
    assert instance.count("fact_invalidations") == 0


@scenario("MUT-04")
@pytest.mark.anyio
async def test_mut_04_ancestor_promotion_derives_validated(
    tmp_path: Path,
    transport: str,
) -> None:
    """An ancestor-scope promotion creates a validated derived fact at
    the ancestor and leaves the source unchanged."""
    instance = Instance(tmp_path, "MUT-04")
    token = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token, segments=DEEP)
        promoted = await running.client.promote(
            promote_body(fact_id, target_segments=[REPO]), credential=token
        )

    assert promoted.result is not None, promoted.text
    pairs = promoted.result["promotions"]
    assert isinstance(pairs, list)
    derived = fact_row(instance, pairs[0]["derived_fact_id"])
    assert derived["trust"] == "validated"
    assert '"acme-repo"' in str(derived["scope_segments"])
    assert '"ingestion"' not in str(derived["scope_segments"])
    source = fact_row(instance, fact_id)
    assert source["trust"] == "candidate"


@scenario("MUT-05")
@pytest.mark.anyio
async def test_mut_05_promotion_without_evidence_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A promotion carrying no evidence is rejected before custody."""
    instance = Instance(tmp_path, "MUT-05")
    token = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token)
        body = promote_body(fact_id, target_segments=[REPO])
        del body["evidence"]
        outcome = await running.client.promote(body, credential=token)

    assert outcome.failure_code == "invalid_request"
    assert outcome.detail == {"field_path": "evidence", "rule": "missing_field"}
    assert instance.count("facts") == 1


@scenario("MUT-06")
@pytest.mark.anyio
async def test_mut_06_promotion_without_source_retrieve_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A promotion by an actor without retrieve authority over the source
    is rejected."""
    instance = Instance(tmp_path, "MUT-06")
    token = instance.add_actor(segments=[REPO], operations=["ingest", "promote"])

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token)
        outcome = await running.client.promote(
            promote_body(fact_id, target_segments=[REPO]), credential=token
        )

    assert outcome.failure_code == "authorisation_denied"
    deny = instance.events("instance")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == "source_retrieve_denied"


@scenario("MUT-07")
@pytest.mark.anyio
async def test_mut_07_promotion_without_target_promote_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A promotion whose target scope the actor holds no promote
    authority over is rejected."""
    instance = Instance(tmp_path, "MUT-07")
    principal_id = instance.add_principal()
    token = instance.add_credential(principal_id)
    # Ingest and retrieve at the deep scope; promote nowhere.
    instance.add_grant(principal_id, segments=DEEP, operations=["ingest", "retrieve"])

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token, segments=DEEP)
        outcome = await running.client.promote(
            promote_body(fact_id, target_segments=DEEP), credential=token
        )

    assert outcome.failure_code == "authorisation_denied"
    deny = instance.events("realm")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == "target_promote_denied"


@scenario("MUT-08")
@pytest.mark.anyio
async def test_mut_08_descendant_sibling_and_cross_realm_targets_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """Descendant, sibling and cross-realm promotion targets are each
    rejected: promotion may only widen towards an ancestor."""
    instance = Instance(tmp_path, "MUT-08")
    principal_id = instance.add_principal()
    token = instance.add_credential(principal_id)
    instance.add_grant(
        principal_id,
        segments=[],
        operations=["ingest", "retrieve", "promote"],
    )
    instance.add_grant(
        principal_id,
        segments=[],
        operations=["ingest", "retrieve", "promote"],
        realm=OTHER_REALM,
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token, segments=[REPO])
        descendant = await running.client.promote(
            promote_body(fact_id, target_segments=DEEP), credential=token
        )
        sibling = await running.client.promote(
            promote_body(fact_id, target_segments=[OTHER_REPO]), credential=token
        )
        cross_realm = await running.client.promote(
            promote_body(fact_id, target_segments=[REPO], target_realm=OTHER_REALM),
            credential=token,
        )

    # A target-shape violation is structural, refused as invalid_request
    # before authorisation is ever consulted (the seam's documented
    # ordering), with the denial recorded durably.
    for outcome in (descendant, sibling, cross_realm):
        assert outcome.failure_code == "invalid_request"
    reasons = [
        event["reason_code"]
        for event in instance.events("realm")
        if event["outcome"] == "deny"
    ]
    assert reasons.count("target_not_ancestor") == 3
    assert instance.count("facts") == 1


@scenario("MUT-09")
@pytest.mark.anyio
async def test_mut_09_classification_raise_outside_writable_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A promotion raising classification beyond the grant's writable
    classifications is rejected."""
    instance = Instance(tmp_path, "MUT-09")
    token = instance.add_actor(
        segments=[REPO],
        operations=["ingest", "retrieve", "promote"],
        write_classifications=["internal", "public"],
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token)
        outcome = await running.client.promote(
            promote_body(
                fact_id,
                target_segments=[REPO],
                target_classification="restricted",
            ),
            credential=token,
        )

    assert outcome.failure_code == "authorisation_denied"
    deny = instance.events("realm")[-1]
    assert deny["reason_code"] == "classification_not_writable"


@scenario("MUT-10")
@pytest.mark.anyio
async def test_mut_10_every_classification_lowering_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A classification-lowering promotion is rejected even when the
    lower classification is within the grant's writable set."""
    instance = Instance(tmp_path, "MUT-10")
    token = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        fact_id = await ingest_fact(running, token, classification="internal")
        outcome = await running.client.promote(
            promote_body(
                fact_id, target_segments=[REPO], target_classification="public"
            ),
            credential=token,
        )

    # Lowering is structural per I-67 — refused as invalid_request no
    # matter what the grant would permit — and never reaches the
    # writable-classifications check.
    assert outcome.failure_code == "invalid_request"
    deny = instance.events("realm")[-1]
    assert deny["reason_code"] == "classification_lowered"
    assert instance.count("facts") == 1
