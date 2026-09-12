"""§6.6 conformance: scope syntax and trust admission (SCOPE-06–08,
TRUST-04–06).

The retrieval-shaped scope and trust scenarios (SCOPE-01–05,
TRUST-01–03) live in ``test_retrieval_scenarios.py``, which slice 6 made
applicable.
"""

from pathlib import Path

import pytest
from conftest import (
    REALM,
    REPO,
    Instance,
    ingest_body,
    scenario,
    serve,
)

# The canonical AWS documentation example key — a synthetic positive that
# trips cairn.secret/v1/upstream/AWSKeyDetector (see
# tests/transports/v1/test_auth.py for provenance), never a real
# credential.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"


@scenario("SCOPE-06")
@pytest.mark.anyio
async def test_scope_06_realm_root_writes_require_an_explicit_root_grant(
    tmp_path: Path,
    transport: str,
) -> None:
    """A repository-scoped grant cannot write or promote at the realm
    root; an explicit root grant can."""
    instance = Instance(tmp_path, "SCOPE-06")
    scoped = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )
    rooted = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "promote"]
    )
    root_write = ingest_body(segments=[])

    def root_promotion(fact_id: object) -> dict[str, object]:
        return {
            "fact_ids": [fact_id],
            "evidence": {
                "external_uri": "https://ci.example.org/run/1",
                "payload_digest": "0" * 64,
            },
            "target_scope": {"realm": REALM, "segments": []},
            "reason": "promotion to the realm root",
        }

    async with serve(instance, transport) as running:
        client = running.client
        denied_write = await client.ingest(root_write, credential=scoped)
        allowed_write = await client.ingest(root_write, credential=rooted)
        # The same root-targeted promotion, once from each actor: the
        # repository-scoped grant is refused and the explicit root grant
        # succeeds. Proving only the refusal would leave a promotion path
        # that always denies looking correct.
        scoped_fact = await client.ingest(ingest_body(), credential=scoped)
        rooted_fact = await client.ingest(ingest_body(), credential=rooted)
        assert scoped_fact.result is not None
        assert rooted_fact.result is not None
        scoped_ids = scoped_fact.result["fact_ids"]
        rooted_ids = rooted_fact.result["fact_ids"]
        assert isinstance(scoped_ids, list)
        assert isinstance(rooted_ids, list)
        denied_promotion = await client.promote(
            root_promotion(scoped_ids[0]), credential=scoped
        )
        allowed_promotion = await client.promote(
            root_promotion(rooted_ids[0]), credential=rooted
        )

    assert denied_write.failure_code == "authorisation_denied"
    assert allowed_write.outcome == "committed"
    assert denied_promotion.failure_code == "authorisation_denied"
    assert allowed_promotion.outcome == "committed"


@scenario("SCOPE-07")
@pytest.mark.anyio
async def test_scope_07_malformed_identifiers_are_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """Malformed realm, segment kind and segment identifier are each
    rejected before custody with the field named."""
    instance = Instance(tmp_path, "SCOPE-07")
    token = instance.add_actor(segments=[], operations=["ingest"])

    async with serve(instance, transport) as running:
        client = running.client
        bad_realm = await client.ingest(
            ingest_body(realm="ACME!", segments=[REPO]), credential=token
        )
        bad_kind = await client.ingest(
            ingest_body(segments=[{"kind": "Repo!", "identifier": "acme-repo"}]),
            credential=token,
        )
        bad_identifier = await client.ingest(
            ingest_body(segments=[{"kind": "repository", "identifier": "no spaces"}]),
            credential=token,
        )

    for outcome in (bad_realm, bad_kind, bad_identifier):
        assert outcome.failure_code == "invalid_request"
        assert outcome.detail is not None
        field_path = outcome.detail["field_path"]
        assert isinstance(field_path, str), outcome.detail
        assert field_path.startswith("scope")
    assert instance.count("assertions") == 0


@scenario("SCOPE-08")
@pytest.mark.anyio
async def test_scope_08_sixteen_segments_pass_and_seventeen_fail(
    tmp_path: Path,
    transport: str,
) -> None:
    """A 16-segment path is accepted and a 17-segment path is rejected."""
    instance = Instance(tmp_path, "SCOPE-08")
    token = instance.add_actor(segments=[], operations=["ingest"])
    segments_16 = [
        {"kind": "component", "identifier": f"segment-{index:02d}"}
        for index in range(16)
    ]
    segments_17 = segments_16 + [{"kind": "component", "identifier": "segment-16"}]

    async with serve(instance, transport) as running:
        accepted = await running.client.ingest(
            ingest_body(segments=segments_16), credential=token
        )
        rejected = await running.client.ingest(
            ingest_body(segments=segments_17), credential=token
        )

    assert accepted.outcome == "committed"
    assert rejected.failure_code == "invalid_request"
    assert instance.count("assertions") == 1


@scenario("TRUST-04")
@pytest.mark.anyio
async def test_trust_04_ingest_without_classification_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """An ingest without a classification is rejected before custody."""
    instance = Instance(tmp_path, "TRUST-04")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])
    body = ingest_body()
    del body["classification"]

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(body, credential=token)

    assert outcome.failure_code == "invalid_request"
    assert outcome.detail == {
        "field_path": "classification",
        "rule": "missing_field",
    }
    assert instance.count("assertions") == 0


@scenario("TRUST-05")
@pytest.mark.anyio
async def test_trust_05_custom_classification_is_rejected(
    tmp_path: Path, transport: str
) -> None:
    """A classification outside the closed vocabulary is rejected."""
    instance = Instance(tmp_path, "TRUST-05")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(
            ingest_body(classification="top-secret"), credential=token
        )

    assert outcome.failure_code == "invalid_request"
    assert outcome.detail == {
        "field_path": "classification",
        "rule": "invalid_value",
    }
    assert instance.count("assertions") == 0


@scenario("TRUST-06")
@pytest.mark.anyio
async def test_trust_06_suspected_secret_is_rejected_not_classified(
    tmp_path: Path,
    transport: str,
) -> None:
    """Suspected secret material is rejected rather than classified: the
    custody screen refuses the fact, nothing reaches custody, and the
    denial names the rule without the content."""
    instance = Instance(tmp_path, "TRUST-06")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(
            ingest_body(facts=[{"body": f"the key is {AWS_EXAMPLE_KEY}"}]),
            credential=token,
        )

    assert outcome.failure_code == "secret_rejected"
    assert outcome.detail == {
        "policy": "cairn.secret/v1",
        "rule": "cairn.secret/v1/upstream/AWSKeyDetector",
        "field_path": "facts[0].body",
    }
    assert instance.count("assertions") == 0
    assert instance.count("facts") == 0
    deny = instance.events("realm")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == "secret_upstream_awskeydetector"
    assert AWS_EXAMPLE_KEY.encode() not in instance.catalogue_bytes()
