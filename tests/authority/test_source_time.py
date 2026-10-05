from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from test_memory import _memory, _seed
from test_retrieval import (
    _CORRELATION_ID,
    _NOW,
    _SCOPE,
    _agent_actor,
    _authority,
)

from cairn.authority.credentials import CLEARANCE_ORDER
from cairn.authority.custody import FactDraft, SourceType
from cairn.authority.gate import fetch_from
from cairn.authority.memory_page_types import SourceTimeStatus
from cairn.authority.mutations import IngestAssertion, PromoteFacts
from cairn.authority.retrieval import _load_candidates
from cairn.authority.source_time import SourceProjection, project_sources
from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import Committed

_RESTRICTED = CLEARANCE_ORDER[Classification.RESTRICTED]
_INTERNAL = CLEARANCE_ORDER[Classification.INTERNAL]


def _ingest(
    path: Path,
    body: str,
    *,
    observed_at: datetime | None,
    payload: bytes | None = b"conversation excerpt",
    classification: Classification = Classification.INTERNAL,
) -> tuple[UUID, UUID | None]:
    outcome = _authority(path).ingest(
        _agent_actor(),
        IngestAssertion(
            scope=_SCOPE,
            classification=classification,
            source_type=SourceType.AGENT_CLAIM,
            facts=(FactDraft(body=body, valid_from=None, valid_to=None),),
            requested_trust=TrustClass.CANDIDATE,
            observed_at=observed_at,
            evidence_payload=payload,
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    return outcome.value.fact_ids[0], outcome.value.evidence_id


def _project(
    path: Path,
    ids: tuple[UUID, ...],
    *,
    scope: Scope = _SCOPE,
    ceiling: int = _RESTRICTED,
) -> dict[UUID, SourceProjection]:
    with read_connection(path) as connection:
        fetch = fetch_from(connection)
        loaded, _ = _load_candidates(fetch, ids)
        return project_sources(
            fetch, {f.fact_id: f for f in loaded}, scope, ceiling, _NOW
        )


def test_ingested_origin_discloses_observation_time_and_exact_evidence(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    said = _NOW - timedelta(days=400)
    fact, evidence = _ingest(tmp_path, "old decision", observed_at=said)
    projection = _project(tmp_path, (fact,))[fact]
    assert projection.observed_at == said
    assert projection.status is SourceTimeStatus.AVAILABLE
    assert projection.evidence_id == evidence


def test_missing_observation_time_is_unavailable_but_evidence_remains(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    fact, evidence = _ingest(tmp_path, "undated", observed_at=None)
    projection = _project(tmp_path, (fact,))[fact]
    assert projection.status is SourceTimeStatus.UNAVAILABLE
    assert projection.observed_at is None
    assert projection.evidence_id == evidence


def _promote_with_restricted_evidence(path: Path, observed: datetime) -> UUID:
    origin, _ = _ingest(path, "promotable", observed_at=observed)
    _, restricted_evidence = _ingest(
        path,
        "restricted proof",
        observed_at=None,
        classification=Classification.RESTRICTED,
    )
    assert restricted_evidence is not None
    outcome = _authority(path).promote(
        _agent_actor(),
        PromoteFacts(
            (origin,),
            restricted_evidence,
            Scope(_SCOPE.realm, ()),
            Classification.INTERNAL,
            "published",
        ),
        idempotency_key=uuid4(),
        correlation_id=_CORRELATION_ID,
    )
    assert isinstance(outcome, Committed)
    return outcome.value.promotions[0][1]


def test_promoted_fact_inherits_origin_time_only_when_chain_is_visible(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    observed = _NOW - timedelta(days=30)
    promoted = _promote_with_restricted_evidence(tmp_path, observed)
    visible = _project(tmp_path, (promoted,), ceiling=_RESTRICTED)[promoted]
    hidden = _project(tmp_path, (promoted,), ceiling=_INTERNAL)[promoted]
    assert visible.observed_at == observed
    assert hidden.status is SourceTimeStatus.UNAVAILABLE
    assert hidden.evidence_id is None


def test_hidden_origin_time_cannot_influence_the_projection(tmp_path: Path) -> None:
    _seed(tmp_path)
    early = _promote_with_restricted_evidence(tmp_path, _NOW - timedelta(days=900))
    late = _promote_with_restricted_evidence(tmp_path, _NOW - timedelta(days=1))
    projections = _project(tmp_path, (early, late), ceiling=_INTERNAL)
    assert projections[early] == projections[late]


def test_projection_agrees_with_legacy_provenance_disclosure(tmp_path: Path) -> None:
    _seed(tmp_path)
    promoted = _promote_with_restricted_evidence(tmp_path, _NOW - timedelta(days=3))
    plain, _ = _ingest(tmp_path, "plain", observed_at=_NOW)
    service = _memory(tmp_path)
    for ceiling in (_INTERNAL, _RESTRICTED):
        with read_connection(tmp_path) as connection:
            fetch = fetch_from(connection)
            loaded, _ = _load_candidates(fetch, (promoted, plain))
            projections = project_sources(
                fetch, {f.fact_id: f for f in loaded}, _SCOPE, ceiling, _NOW
            )
            for fact in loaded:
                legacy = service._fact(fetch, fact, _SCOPE, ceiling, _NOW)
                origin_disclosed = legacy.source_principal_id is not None
                assert (
                    projections[fact.fact_id].evidence_id is not None
                ) == origin_disclosed
                assert (
                    projections[fact.fact_id].observed_at is not None
                ) <= origin_disclosed
