"""§6.6 conformance: reconciled retrieval (SCOPE-01–05, TRUST-01–03,
EVIDENCE-01–03).

Slice 6 makes these applicable. Each runs against a fully started
instance — real transport, authentication, grants and catalogue — with the
deterministic in-memory index behind the P-38 seam, because the real
Graphiti adapter needs FalkorDB and model providers and P-39 forbids
constructing it in CI. The index's weakness is not a gap in the proof:
these scenarios ask what the *catalogue* discloses, and the adversarial
cases where the index lies are `INDEX-01`–`04`, next door.
"""

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from conftest import (
    COMPONENT,
    OTHER_REALM,
    OTHER_REPO,
    REALM,
    REPO,
    Instance,
    Running,
    bodies,
    hits,
    ingest_body,
    scenario,
    serve,
)
from transport import OperationOutcome

from cairn.evidence.attic import SqliteAttic
from cairn.projection.memory import MemoryIndex

EVIDENCE_PAYLOAD = "the exact evidence payload"
_ALL_TRUST = ["candidate", "failed-approach", "validated"]


def retrieve_body(
    *,
    realm: str = REALM,
    segments: list[dict[str, str]] | None = None,
    query: str = "pipeline",
    budget: int = 65536,
    trust_filters: list[str] | None = None,
    as_of: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "scope": {
            "realm": realm,
            "segments": segments if segments is not None else [REPO],
        },
        "query": query,
        "budget": budget,
    }
    if trust_filters is not None:
        body["trust_filters"] = trust_filters
    if as_of is not None:
        body["as_of"] = as_of
    return body


async def ingest_fact(
    running: Running,
    token: str,
    *,
    body_text: str,
    segments: list[dict[str, str]] | None = None,
    realm: str = REALM,
    classification: str = "internal",
    trust: str = "candidate",
    evidence: str | None = None,
) -> dict[str, Any]:
    payload = ingest_body(
        realm=realm,
        segments=segments,
        classification=classification,
        requested_trust=trust,
        facts=[{"body": body_text}],
    )
    if evidence is not None:
        payload["evidence_payload"] = evidence
    outcome = await running.client.ingest(payload, credential=token)
    assert outcome.result is not None, outcome.text
    return dict(outcome.result)


async def retrieve(
    running: Running, token: str, body: dict[str, object]
) -> OperationOutcome:
    return await running.client.retrieve(body, credential=token)


@scenario("SCOPE-01")
@pytest.mark.anyio
async def test_scope_01_exact_and_inherited_ancestor_facts_are_returned(
    tmp_path: Path,
    transport: str,
) -> None:
    """The inherited-ancestor rule: a request at ``repository/component``
    sees facts recorded there and at every ancestor up to the realm root,
    because a fact at a broader scope is in scope for everything beneath
    it."""
    instance = Instance(tmp_path, "SCOPE-01", index=MemoryIndex())
    actor = instance.add_actor(segments=[], operations=["ingest", "retrieve"])

    async with serve(instance, transport) as running:
        await ingest_fact(running, actor, body_text="root pipeline fact", segments=[])
        await ingest_fact(running, actor, body_text="repo pipeline fact")
        await ingest_fact(
            running,
            actor,
            body_text="component pipeline fact",
            segments=[REPO, COMPONENT],
        )
        instance.drain_projection()
        outcome = await retrieve(
            running,
            actor,
            retrieve_body(segments=[REPO, COMPONENT], trust_filters=["candidate"]),
        )

    assert bodies(outcome) == {
        "root pipeline fact",
        "repo pipeline fact",
        "component pipeline fact",
    }


@scenario("SCOPE-02")
@pytest.mark.anyio
async def test_scope_02_a_descendant_grant_cannot_query_an_ancestor_scope(
    tmp_path: Path,
    transport: str,
) -> None:
    """Inheritance runs one way. A grant at ``repository/component`` reads
    ancestor *facts* when querying its own scope, but may not address the
    ancestor scope itself — that request is a wider one than the grant
    covers, and is refused before any fact is considered."""
    instance = Instance(tmp_path, "SCOPE-02", index=MemoryIndex())
    rooted = instance.add_actor(segments=[], operations=["ingest", "retrieve"])
    descendant = instance.add_actor(
        segments=[REPO, COMPONENT], operations=["ingest", "retrieve"]
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, rooted, body_text="repo pipeline fact")
        instance.drain_projection()
        refused = await retrieve(
            running, descendant, retrieve_body(trust_filters=["candidate"])
        )
        allowed = await retrieve(
            running,
            descendant,
            retrieve_body(segments=[REPO, COMPONENT], trust_filters=["candidate"]),
        )

    assert refused.failure_code == "authorisation_denied"
    # The same actor, querying its own scope, still inherits the ancestor
    # fact — so the refusal above is about the addressed scope, not about
    # the fact being unreachable.
    assert bodies(allowed) == {"repo pipeline fact"}


@scenario("SCOPE-03")
@pytest.mark.anyio
async def test_scope_03_sibling_facts_are_not_returned(
    tmp_path: Path, transport: str
) -> None:
    instance = Instance(tmp_path, "SCOPE-03", index=MemoryIndex())
    actor = instance.add_actor(segments=[], operations=["ingest", "retrieve"])

    async with serve(instance, transport) as running:
        await ingest_fact(running, actor, body_text="own pipeline fact")
        await ingest_fact(
            running, actor, body_text="sibling pipeline fact", segments=[OTHER_REPO]
        )
        instance.drain_projection()
        outcome = await retrieve(
            running, actor, retrieve_body(trust_filters=["candidate"])
        )

    assert bodies(outcome) == {"own pipeline fact"}


@scenario("SCOPE-04")
@pytest.mark.anyio
async def test_scope_04_descendant_facts_are_not_returned(
    tmp_path: Path, transport: str
) -> None:
    """The other half of SCOPE-01: inheritance is downward only. A fact
    recorded at ``repository/component`` is not disclosed to a query at
    ``repository``, which is a narrower statement than the query asked
    about."""
    instance = Instance(tmp_path, "SCOPE-04", index=MemoryIndex())
    actor = instance.add_actor(segments=[], operations=["ingest", "retrieve"])

    async with serve(instance, transport) as running:
        await ingest_fact(running, actor, body_text="repo pipeline fact")
        await ingest_fact(
            running,
            actor,
            body_text="component pipeline fact",
            segments=[REPO, COMPONENT],
        )
        instance.drain_projection()
        outcome = await retrieve(
            running, actor, retrieve_body(trust_filters=["candidate"])
        )

    assert bodies(outcome) == {"repo pipeline fact"}


@scenario("SCOPE-05")
@pytest.mark.anyio
async def test_scope_05_cross_realm_facts_are_not_returned_or_disclosed(
    tmp_path: Path,
    transport: str,
) -> None:
    """Two realms, one principal holding a grant in each — so the actor is
    genuinely entitled to both, and only the addressed realm answers.
    Nothing in either response mentions the other."""
    instance = Instance(tmp_path, "SCOPE-05", index=MemoryIndex())
    principal = instance.add_principal()
    token = instance.add_credential(principal)
    instance.add_grant(principal, segments=[], operations=["ingest", "retrieve"])
    instance.add_grant(
        principal, segments=[], operations=["ingest", "retrieve"], realm=OTHER_REALM
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, token, body_text="acme pipeline fact")
        await ingest_fact(
            running,
            token,
            body_text="umbra pipeline fact",
            realm=OTHER_REALM,
            segments=[REPO],
        )
        instance.drain_projection()
        here = await retrieve(
            running, token, retrieve_body(trust_filters=["candidate"])
        )
        there = await retrieve(
            running,
            token,
            retrieve_body(realm=OTHER_REALM, trust_filters=["candidate"]),
        )

    assert bodies(here) == {"acme pipeline fact"}
    assert "umbra" not in here.text
    assert bodies(there) == {"umbra pipeline fact"}
    assert "acme pipeline fact" not in there.text


@scenario("TRUST-01")
@pytest.mark.anyio
async def test_trust_01_default_retrieval_returns_validated_facts_only(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path, "TRUST-01", index=MemoryIndex(), attic=True)
    actor = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, actor, body_text="candidate pipeline fact")
        await ingest_fact(
            running,
            actor,
            body_text="validated pipeline fact",
            trust="validated",
            evidence=EVIDENCE_PAYLOAD,
        )
        instance.drain_projection()
        outcome = await retrieve(running, actor, retrieve_body())

    assert bodies(outcome) == {"validated pipeline fact"}


@scenario("TRUST-02")
@pytest.mark.anyio
async def test_trust_02_candidate_and_failed_approach_require_their_filters(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path, "TRUST-02", index=MemoryIndex())
    actor = instance.add_actor(segments=[], operations=["ingest", "retrieve"])

    async with serve(instance, transport) as running:
        await ingest_fact(running, actor, body_text="candidate pipeline fact")
        await ingest_fact(
            running,
            actor,
            body_text="failed pipeline fact",
            trust="failed-approach",
        )
        instance.drain_projection()
        defaulted = await retrieve(running, actor, retrieve_body())
        candidate_only = await retrieve(
            running, actor, retrieve_body(trust_filters=["candidate"])
        )
        both = await retrieve(
            running,
            actor,
            retrieve_body(trust_filters=["candidate", "failed-approach"]),
        )

    assert hits(defaulted) == []
    assert bodies(candidate_only) == {"candidate pipeline fact"}
    assert bodies(both) == {"candidate pipeline fact", "failed pipeline fact"}


@scenario("TRUST-03")
@pytest.mark.anyio
async def test_trust_03_facts_above_read_clearance_are_not_returned(
    tmp_path: Path,
    transport: str,
) -> None:
    """Two enforcement points, as I-80 pins: the ceiling is derived from
    the grant, so a caller cannot ask for one it does not hold, and the
    reconciliation discards anything above it regardless."""
    instance = Instance(tmp_path, "TRUST-03", index=MemoryIndex())
    writer = instance.add_actor(
        segments=[], operations=["ingest", "retrieve"], read_clearance="restricted"
    )
    limited = instance.add_actor(
        segments=[],
        operations=["ingest", "retrieve"],
        read_clearance="internal",
        write_classifications=["internal", "public"],
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, writer, body_text="internal pipeline fact")
        await ingest_fact(
            running,
            writer,
            body_text="restricted pipeline fact",
            classification="restricted",
        )
        instance.drain_projection()
        cleared = await retrieve(
            running, writer, retrieve_body(trust_filters=["candidate"])
        )
        capped = await retrieve(
            running, limited, retrieve_body(trust_filters=["candidate"])
        )

    assert bodies(cleared) == {
        "internal pipeline fact",
        "restricted pipeline fact",
    }
    assert bodies(capped) == {"internal pipeline fact"}
    assert "restricted pipeline fact" not in capped.text


@scenario("EVIDENCE-01")
@pytest.mark.anyio
async def test_evidence_01_an_authorised_exact_evidence_hit_is_returned(
    tmp_path: Path,
    transport: str,
) -> None:
    """P-43: an Attic hit is reconciled as evidence, then mapped to the
    facts of its assertion, which carry the catalogue's own provenance.
    Both reach directions are exercised — evidence at the requested scope
    and at an ancestor of it."""
    index = MemoryIndex()
    instance = Instance(tmp_path, "EVIDENCE-01", index=index, attic=True)
    actor = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        at_scope = await ingest_fact(
            running,
            actor,
            body_text="validated pipeline fact",
            trust="validated",
            evidence=EVIDENCE_PAYLOAD,
        )
        at_ancestor = await ingest_fact(
            running,
            actor,
            body_text="ancestor evidence fact",
            segments=[],
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " (root)",
        )
        _store_attic(instance, at_scope["evidence_id"], EVIDENCE_PAYLOAD)
        _store_attic(instance, at_ancestor["evidence_id"], EVIDENCE_PAYLOAD + " (root)")
        # The index finds nothing for this query, so every hit below
        # arrives through the Attic modality alone.
        instance.drain_projection()
        outcome = await retrieve(running, actor, retrieve_body(query="payload"))

    assert bodies(outcome) == {"validated pipeline fact", "ancestor evidence fact"}
    hit = next(
        item for item in hits(outcome) if item["body"] == "validated pipeline fact"
    )
    assert hit["assertion_id"] == at_scope["assertion_id"]
    assert hit["trust"] == "validated"
    # I-77's hit shape is the stored fact fields: payload bytes are not on
    # the wire under any key.
    assert EVIDENCE_PAYLOAD not in outcome.text


@scenario("EVIDENCE-02")
@pytest.mark.anyio
async def test_evidence_02_sibling_descendant_and_cross_realm_hits_are_discarded(
    tmp_path: Path,
    transport: str,
) -> None:
    index = MemoryIndex()
    instance = Instance(tmp_path, "EVIDENCE-02", index=index, attic=True)
    principal = instance.add_principal()
    token = instance.add_credential(principal)
    instance.add_grant(
        principal, segments=[], operations=["ingest", "retrieve", "promote"]
    )
    instance.add_grant(
        principal,
        segments=[],
        operations=["ingest", "retrieve", "promote"],
        realm=OTHER_REALM,
    )

    async with serve(instance, transport) as running:
        sibling = await ingest_fact(
            running,
            token,
            body_text="sibling evidence fact",
            segments=[OTHER_REPO],
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " sibling",
        )
        descendant = await ingest_fact(
            running,
            token,
            body_text="descendant evidence fact",
            segments=[REPO, COMPONENT],
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " descendant",
        )
        cross_realm = await ingest_fact(
            running,
            token,
            body_text="cross realm evidence fact",
            realm=OTHER_REALM,
            segments=[REPO],
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " cross",
        )
        for result, payload in (
            (sibling, EVIDENCE_PAYLOAD + " sibling"),
            (descendant, EVIDENCE_PAYLOAD + " descendant"),
            (cross_realm, EVIDENCE_PAYLOAD + " cross"),
        ):
            _store_attic(instance, result["evidence_id"], payload)
        instance.drain_projection()
        outcome = await retrieve(running, token, retrieve_body(query="payload"))

    assert hits(outcome) == []
    assert "evidence fact" not in outcome.text


@scenario("EVIDENCE-03")
@pytest.mark.anyio
async def test_evidence_03_unknown_stale_and_above_clearance_hits_are_discarded(
    tmp_path: Path,
    transport: str,
) -> None:
    """Three ways an Attic candidate fails reconciliation: an identity the
    catalogue never held, a record whose stored payload digest no longer
    matches the bytes Attic returns, and a record above the caller's
    derived ceiling. None is a public failure."""
    index = MemoryIndex()
    instance = Instance(tmp_path, "EVIDENCE-03", index=index, attic=True)
    writer = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "promote"]
    )
    limited = instance.add_actor(
        segments=[],
        operations=["ingest", "retrieve", "promote"],
        read_clearance="internal",
    )

    async with serve(instance, transport) as running:
        restricted = await ingest_fact(
            running,
            writer,
            body_text="restricted evidence fact",
            classification="restricted",
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " restricted",
        )
        stale = await ingest_fact(
            running,
            writer,
            body_text="stale evidence fact",
            trust="validated",
            evidence=EVIDENCE_PAYLOAD + " stale",
        )
        _store_attic(
            instance, restricted["evidence_id"], EVIDENCE_PAYLOAD + " restricted"
        )
        # Stored under the right identity with the wrong bytes: the I-69
        # digest check is what refuses this one.
        _store_attic(instance, stale["evidence_id"], "tampered payload")
        # And an identity the catalogue has never held.
        _store_attic(instance, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "orphan payload")
        instance.drain_projection()
        outcome = await retrieve(running, limited, retrieve_body(query="payload"))

    assert hits(outcome) == []
    assert "restricted evidence fact" not in outcome.text
    assert "stale evidence fact" not in outcome.text


def _store_attic(instance: Instance, evidence_id: str, payload: str) -> None:
    """Puts payload bytes in Attic directly.

    The evidence deliverer would do this from the outbox; running it here
    keeps the scenario's timing exact for the same reason
    ``drain_projection`` exists. Digests are the caller's business: two of
    the `EVIDENCE-03` cases depend on the bytes *not* matching what the
    catalogue recorded.
    """
    attic = SqliteAttic(instance.data_path)
    attic.store(UUID(evidence_id), payload.encode("utf-8"))


def _digest(payload: str) -> bytes:
    return hashlib.sha256(payload.encode("utf-8")).digest()
