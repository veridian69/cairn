"""§6.6 conformance: the hostile index (INDEX-01–04).

These are the scenarios that actually prove the architecture. Everywhere
else the index is cooperative and the question is whether Cairn asks it
the right thing; here the index is an adversary that returns whatever
would most embarrass the catalogue — sibling, descendant and cross-realm
identities, facts above the caller's clearance, trust classes the request
excluded, identities that were invalidated, and identities that never
existed — and the requirement is that none of it reaches the caller.

The hostile adapter also ignores ``partition_keys`` entirely. P-38 says an
adapter honouring them is a performance property, not a security one, and
this is where that claim is tested rather than asserted.

Each scenario runs through a fully started instance: real transport, real
authentication, real grants, real catalogue. The only substitution is the
index itself, injected through the P-38 composition seam.
"""

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

from cairn.projection.adapter import (
    FactProjected,
    ProjectedFactState,
    ProjectionFailed,
)

UNKNOWN_FACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


class HostileIndex:
    """Returns every identity it has ever seen, plus one that never
    existed, in a deterministic order — ignoring the query, the limit
    bound's spirit and the partition keys entirely.

    It is not random and not clever: a hostile index does not need to be
    either. It needs only to answer a narrow question with a wide answer,
    which is exactly what I-79 reconciliation exists to survive.
    """

    def __init__(self) -> None:
        self.projected: list[UUID] = []
        self.searches: list[tuple[str, int, tuple[str, ...]]] = []

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        self.projected.append(state.fact_id)
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        self.searches.append((query, limit, partition_keys))
        # Duplicated deliberately as well as widened: I-79 must collapse
        # repeats, and a duplicate that reached assembly would be charged
        # to the budget twice.
        candidates = [*self.projected, *self.projected, UNKNOWN_FACT_ID]
        return tuple(candidates[:limit])

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        self.projected.clear()


def retrieve_body(
    *,
    realm: str = REALM,
    segments: list[dict[str, str]] | None = None,
    trust_filters: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "scope": {
            "realm": realm,
            "segments": segments if segments is not None else [REPO],
        },
        "query": "anything at all",
        "budget": 65536,
    }
    if trust_filters is not None:
        body["trust_filters"] = trust_filters
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


@scenario("INDEX-01")
@pytest.mark.anyio
async def test_index_01_hostile_sibling_descendant_and_cross_realm_hits_discarded(
    tmp_path: Path,
    transport: str,
) -> None:
    """The index returns facts from every wrong scope at once, including
    another realm's. Only the ancestry chain of the requested scope is
    disclosed."""
    index = HostileIndex()
    instance = Instance(tmp_path, "INDEX-01", index=index)
    principal = instance.add_principal()
    token = instance.add_credential(principal)
    instance.add_grant(principal, segments=[], operations=["ingest", "retrieve"])
    instance.add_grant(
        principal, segments=[], operations=["ingest", "retrieve"], realm=OTHER_REALM
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, token, body_text="root fact", segments=[])
        await ingest_fact(running, token, body_text="own fact")
        await ingest_fact(
            running, token, body_text="sibling fact", segments=[OTHER_REPO]
        )
        await ingest_fact(
            running, token, body_text="descendant fact", segments=[REPO, COMPONENT]
        )
        await ingest_fact(
            running,
            token,
            body_text="cross realm fact",
            realm=OTHER_REALM,
            segments=[REPO],
        )
        instance.drain_projection()
        outcome = await retrieve(
            running, token, retrieve_body(trust_filters=["candidate"])
        )

    assert bodies(outcome) == {"root fact", "own fact"}
    for hidden in ("sibling fact", "descendant fact", "cross realm fact"):
        assert hidden not in outcome.text
    # The adapter really was asked, really did over-answer, and really did
    # ignore the partition keys — so the discards above are Cairn's work.
    assert index.searches
    assert len(index.projected) == 5


@scenario("INDEX-02")
@pytest.mark.anyio
async def test_index_02_hostile_candidate_and_failed_hits_need_explicit_filters(
    tmp_path: Path,
    transport: str,
) -> None:
    index = HostileIndex()
    instance = Instance(tmp_path, "INDEX-02", index=index, attic=True)
    token = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "promote"]
    )

    async with serve(instance, transport) as running:
        await ingest_fact(running, token, body_text="candidate fact")
        await ingest_fact(
            running, token, body_text="failed fact", trust="failed-approach"
        )
        await ingest_fact(
            running,
            token,
            body_text="validated fact",
            trust="validated",
            evidence="the evidence payload",
        )
        instance.drain_projection()
        defaulted = await retrieve(running, token, retrieve_body())
        widened = await retrieve(
            running,
            token,
            retrieve_body(trust_filters=["candidate", "failed-approach"]),
        )

    assert bodies(defaulted) == {"validated fact"}
    assert "candidate fact" not in defaulted.text
    assert "failed fact" not in defaulted.text
    assert bodies(widened) == {"candidate fact", "failed fact"}


@scenario("INDEX-03")
@pytest.mark.anyio
async def test_index_03_hostile_hits_above_read_clearance_are_discarded(
    tmp_path: Path,
    transport: str,
) -> None:
    """The ceiling is derived from the caller's grant, so the request
    cannot raise it, and reconciliation applies it to every candidate the
    index offers — the two independent enforcement points I-80 pins."""
    index = HostileIndex()
    instance = Instance(tmp_path, "INDEX-03", index=index)
    writer = instance.add_actor(
        segments=[], operations=["ingest", "retrieve"], read_clearance="restricted"
    )
    limited = instance.add_actor(
        segments=[],
        operations=["ingest", "retrieve"],
        read_clearance="public",
        write_classifications=["public"],
    )

    async with serve(instance, transport) as running:
        await ingest_fact(
            running, writer, body_text="public fact", classification="public"
        )
        await ingest_fact(
            running, writer, body_text="internal fact", classification="internal"
        )
        await ingest_fact(
            running, writer, body_text="restricted fact", classification="restricted"
        )
        instance.drain_projection()
        outcome = await retrieve(
            running, limited, retrieve_body(trust_filters=["candidate"])
        )

    assert bodies(outcome) == {"public fact"}
    assert "internal fact" not in outcome.text
    assert "restricted fact" not in outcome.text


@scenario("INDEX-04")
@pytest.mark.anyio
async def test_index_04_stale_hits_for_invalidated_or_unknown_identities_discarded(
    tmp_path: Path,
    transport: str,
) -> None:
    """Two kinds of staleness. An invalidated fact the index still holds is
    invisible at query time and visible only to an ``as_of`` before its
    invalidation, which is I-81 rather than an accident. An identity the
    catalogue never held is discarded silently and raises the P-47
    operator signal, never a wire failure — the duplicate identities the
    adapter also returns collapse to one hit apiece.
    """
    index = HostileIndex()
    instance = Instance(tmp_path, "INDEX-04", index=index)
    token = instance.add_actor(
        segments=[], operations=["ingest", "retrieve", "invalidate"]
    )

    async with serve(instance, transport) as running:
        surviving = await ingest_fact(running, token, body_text="surviving fact")
        ended = await ingest_fact(running, token, body_text="ended fact")
        instance.drain_projection()
        before = instance.clock.now
        instance.clock.now = before.replace(hour=before.hour + 1)
        invalidation = await running.client.invalidate(
            {
                "fact_ids": [ended["fact_ids"][0]],
                "reason": "superseded by a later observation",
            },
            credential=token,
        )
        assert invalidation.outcome == "committed", invalidation.text
        instance.drain_projection()
        instance.clock.now = before.replace(hour=before.hour + 2)

        now = await retrieve(running, token, retrieve_body(trust_filters=["candidate"]))
        historical = await retrieve(
            running,
            token,
            {
                **retrieve_body(trust_filters=["candidate"]),
                "as_of": _timestamp(before),
            },
        )

    assert bodies(now) == {"surviving fact"}
    assert "ended fact" not in now.text
    # Duplicates collapsed: the adapter returned every identity twice.
    assert len(hits(now)) == 1
    assert bodies(historical) == {"surviving fact", "ended fact"}
    # The unknown identity never surfaced as a failure, on either request.
    assert str(UNKNOWN_FACT_ID) not in now.text
    assert now.result is not None
    assert now.result["budget_consumed"] == len("surviving fact")
    assert surviving["fact_ids"][0] == hits(now)[0]["fact_id"]


def _timestamp(value: object) -> str:
    from datetime import datetime

    from cairn.catalogue.sqlite import canonical_timestamp

    assert isinstance(value, datetime)
    return canonical_timestamp(value)
