"""The transport-neutral wire↔command translation for `/v1` (P-33, P-50).

One direction turns a validated I-71 request model into the frozen command
the authority accepts; the other turns the value it returns into the I-72
result body, and wraps a mutation's in the success envelope. Between them
sit the wire-encoding primitives I-71 and I-28 fix — canonical lowercase
hyphenated UUIDs, 64-character lowercase hex digests, offset-bearing
timestamps, closed vocabularies — each refusing with ``WireRejection``
under a wire rule and the field path that names the offender.

Shared rather than REST's, for P-50's reason: I-90 requires a scenario run
through either surface to produce the same failure code, rule identity and
field path, and a second copy of ``facts[0].valid_from`` is exactly how two
surfaces come to disagree about which field a caller got wrong. HTTP stayed
in ``rest/v1``: the header, the I-73 status table and the response object.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from cairn.administration.audit_read import AuditEventPage, ReadAuditEvents
from cairn.administration.commands import (
    CreateGrant,
    CreatePrincipal,
    CredentialIssued,
    CredentialRevoked,
    GrantCreated,
    GrantRevoked,
    IssueCredential,
    PrincipalCreated,
    RevokeCredential,
    RevokeGrant,
)
from cairn.authority.credentials import GrantOperation, PrincipalKind
from cairn.authority.custody import (
    CustodyValueError,
    FactDraft,
    IngestedProvenance,
    PromotedProvenance,
    SourceType,
)
from cairn.authority.grants import ProposedGrant
from cairn.authority.mutations import (
    AssertionIngested,
    ExternalEvidenceReference,
    FactsInvalidated,
    FactsPromoted,
    IngestAssertion,
    InvalidateFacts,
    PromoteFacts,
)
from cairn.authority.retrieval import RetrievalResult, Retrieve, RetrievedFact
from cairn.catalogue.audit import (
    AuditValueError,
    Classification,
    Scope,
    ScopeSegment,
    TrustClass,
    canonical_audit_bytes,
)
from cairn.catalogue.sqlite import canonical_timestamp
from cairn.catalogue.transactions import Committed, Replayed
from cairn.transports.v1.parsing import WireRejection, validation_rejection
from cairn.transports.v1.requests import (
    CreateGrantRequest,
    CreatePrincipalRequest,
    EvidenceIdBody,
    FactBody,
    IngestRequest,
    InvalidateRequest,
    IssueCredentialRequest,
    PromoteRequest,
    ReadAuditEventsRequest,
    RetrieveRequest,
    RevokeCredentialRequest,
    RevokeGrantRequest,
    ScopeBody,
    SegmentBody,
)
from cairn.transports.v1.responses import (
    CreateGrantResult,
    CreatePrincipalResult,
    IngestResult,
    InvalidateResult,
    IssueCredentialResult,
    PromoteResult,
    PromotionPairBody,
    ReadAuditEventsResult,
    RetrievedFactBody,
    RetrieveResult,
    RevokeCredentialResult,
    RevokeGrantResult,
)
from cairn.transports.v1.wire import (
    RULE_INVALID_VALUE,
    AuditReceiptBody,
    MutationReceiptBody,
    SuccessEnvelope,
    WireModel,
    WireOutcome,
    encode_digest,
    encode_timestamp,
    encode_uuid,
)

# The same 64-character lowercase hex rule audit.py and migration.py each
# pin privately; duplicated rather than imported for the same
# reach-without-purity-violation reason Task 1's ledger entry records.
_DIGEST_HEX = re.compile(r"[0-9a-f]{64}\Z")


def validated[ModelT: WireModel](
    model_type: type[ModelT], body: dict[str, object]
) -> ModelT:
    try:
        return model_type.model_validate(body)
    except ValidationError as error:
        raise validation_rejection(error) from error


def success_envelope[ResultT: WireModel, ValueT](
    result: Committed[ValueT] | Replayed[ValueT],
    body: ResultT,
) -> SuccessEnvelope[ResultT]:
    receipt = result.mutation_receipt
    audit = result.audit_receipt
    return SuccessEnvelope[ResultT](
        outcome=(
            WireOutcome.COMMITTED if type(result) is Committed else WireOutcome.REPLAYED
        ),
        result=body,
        mutation_receipt=MutationReceiptBody(
            mutation_id=encode_uuid(receipt.mutation_id),
            command_digest=encode_digest(receipt.command_digest),
        ),
        audit_receipt=AuditReceiptBody(
            event_id=encode_uuid(audit.event_id),
            chain_kind=audit.chain_kind.value,
            chain_identity=audit.chain_identity,
            sequence=audit.sequence,
            recorded_at=canonical_timestamp(audit.recorded_at),
            event_hash=encode_digest(audit.event_hash),
        ),
    )


def ingest_result(value: AssertionIngested) -> IngestResult:
    return IngestResult(
        assertion_id=encode_uuid(value.assertion_id),
        fact_ids=[encode_uuid(fact_id) for fact_id in value.fact_ids],
        evidence_id=(
            encode_uuid(value.evidence_id) if value.evidence_id is not None else None
        ),
    )


def promote_result(value: FactsPromoted) -> PromoteResult:
    return PromoteResult(
        promotions=[
            PromotionPairBody(
                source_fact_id=encode_uuid(source),
                derived_fact_id=encode_uuid(derived),
            )
            for source, derived in value.promotions
        ],
        evidence_id=encode_uuid(value.evidence_id),
    )


def invalidate_result(value: FactsInvalidated) -> InvalidateResult:
    return InvalidateResult(
        fact_ids=[encode_uuid(fact_id) for fact_id in value.fact_ids],
        invalidated_at=encode_timestamp(value.invalidated_at),
    )


def ingest_command(model: IngestRequest) -> IngestAssertion:
    """Translates the validated wire model into the frozen command.

    Closed-vocabulary and timestamp refusals surface as ``invalid_request``
    with the field path named; a value the command's own validation refuses
    at construction (an over-length body, a bad validity window) is the
    same failure with the fact index named — the command layer re-validates
    everything regardless, per I-66's authority order.
    """
    scope = _scope(model.scope, "scope")
    classification = _coerce(Classification, model.classification, "classification")
    source_type = _coerce(SourceType, model.source_type, "source_type")
    requested_trust = _coerce(TrustClass, model.requested_trust, "requested_trust")
    facts = tuple(_fact_draft(fact, index) for index, fact in enumerate(model.facts))
    metadata = (
        json.dumps(
            model.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if model.metadata is not None
        else None
    )
    return IngestAssertion(
        scope=scope,
        classification=classification,
        source_type=source_type,
        facts=facts,
        requested_trust=requested_trust,
        observed_at=_timestamp(model.observed_at, "observed_at"),
        metadata=metadata,
        evidence_payload=(
            model.evidence_payload.encode("utf-8")
            if model.evidence_payload is not None
            else None
        ),
    )


def promote_command(model: PromoteRequest) -> PromoteFacts:
    evidence: UUID | ExternalEvidenceReference
    # ``isinstance`` for the same reason as auth.py's narrowing: both arms
    # of a closed two-member union are consumed here.
    if isinstance(model.evidence, EvidenceIdBody):
        evidence = _uuid(model.evidence.evidence_id, "evidence.evidence_id")
    else:
        evidence = ExternalEvidenceReference(
            external_uri=model.evidence.external_uri,
            payload_digest=_digest(
                model.evidence.payload_digest, "evidence.payload_digest"
            ),
        )
    return PromoteFacts(
        fact_ids=_fact_ids(model.fact_ids),
        evidence=evidence,
        target_scope=(
            _scope(model.target_scope, "target_scope")
            if model.target_scope is not None
            else None
        ),
        target_classification=(
            _coerce(
                Classification, model.target_classification, "target_classification"
            )
            if model.target_classification is not None
            else None
        ),
        reason=model.reason,
    )


def invalidate_command(model: InvalidateRequest) -> InvalidateFacts:
    return InvalidateFacts(
        fact_ids=_fact_ids(model.fact_ids),
        reason=model.reason,
        superseded_by=(
            _uuid(model.superseded_by, "superseded_by")
            if model.superseded_by is not None
            else None
        ),
    )


def create_principal_command(model: CreatePrincipalRequest) -> CreatePrincipal:
    return CreatePrincipal(
        realm_id=model.realm_id,
        kind=_coerce(PrincipalKind, model.kind, "kind"),
        label=model.label,
    )


def create_principal_result(value: PrincipalCreated) -> CreatePrincipalResult:
    return CreatePrincipalResult(
        principal_id=encode_uuid(value.principal_id),
        kind=value.kind.value,
        label=value.label,
        created_at=encode_timestamp(value.created_at),
    )


def issue_credential_command(model: IssueCredentialRequest) -> IssueCredential:
    return IssueCredential(
        realm_id=model.realm_id,
        principal_id=_uuid(model.principal_id, "principal_id"),
        expires_at=_timestamp(model.expires_at, "expires_at"),
    )


def issue_credential_result(value: CredentialIssued) -> IssueCredentialResult:
    # I-72/I-60: the token string exactly once, on the committed emission;
    # a replay's PlaintextUnavailable serialises as null.
    plaintext = value.plaintext if type(value.plaintext) is str else None
    return IssueCredentialResult(
        credential_id=encode_uuid(value.credential_id),
        principal_id=encode_uuid(value.principal_id),
        expires_at=(
            encode_timestamp(value.expires_at) if value.expires_at is not None else None
        ),
        created_at=encode_timestamp(value.created_at),
        plaintext=plaintext,
    )


def revoke_credential_command(model: RevokeCredentialRequest) -> RevokeCredential:
    return RevokeCredential(
        realm_id=model.realm_id,
        credential_id=_uuid(model.credential_id, "credential_id"),
        reason_code=model.reason_code,
    )


def revoke_credential_result(value: CredentialRevoked) -> RevokeCredentialResult:
    return RevokeCredentialResult(
        credential_id=encode_uuid(value.credential_id),
        revoked_at=encode_timestamp(value.revoked_at),
    )


def create_grant_command(model: CreateGrantRequest) -> CreateGrant:
    grant = model.grant
    return CreateGrant(
        realm_id=model.realm_id,
        grant=ProposedGrant(
            principal_id=_uuid(grant.principal_id, "grant.principal_id"),
            realm_id=grant.realm_id,
            segments=_segments(grant.segments, "grant.segments"),
            operations=frozenset(
                _coerce(GrantOperation, value, f"grant.operations[{index}]")
                for index, value in enumerate(grant.operations)
            ),
            read_clearance=_coerce(
                Classification, grant.read_clearance, "grant.read_clearance"
            ),
            write_classifications=frozenset(
                _coerce(
                    Classification,
                    value,
                    f"grant.write_classifications[{index}]",
                )
                for index, value in enumerate(grant.write_classifications)
            ),
            # I-71: the wire cannot express delegable_operations; absence is
            # the only proposal I-61 accepts.
            delegable_operations=None,
            expires_at=_timestamp(grant.expires_at, "grant.expires_at"),
        ),
    )


def create_grant_result(value: GrantCreated) -> CreateGrantResult:
    return CreateGrantResult(
        grant_id=encode_uuid(value.grant_id),
        created_at=encode_timestamp(value.created_at),
    )


def revoke_grant_command(model: RevokeGrantRequest) -> RevokeGrant:
    return RevokeGrant(
        realm_id=model.realm_id,
        grant_id=_uuid(model.grant_id, "grant_id"),
        reason_code=model.reason_code,
    )


def revoke_grant_result(value: GrantRevoked) -> RevokeGrantResult:
    return RevokeGrantResult(
        grant_id=encode_uuid(value.grant_id),
        revoked_at=encode_timestamp(value.revoked_at),
    )


def retrieve_command(model: RetrieveRequest) -> Retrieve:
    """I-71 decoding only — every bound and vocabulary check belongs to the
    pipeline, which owns them for the CLI and conformance paths too. The one
    thing settled here is the trust vocabulary, because an unknown spelling
    cannot become a ``TrustClass`` at all, and duplicates, which strict
    parsing must reject rather than silently collapse into a set."""
    filters: list[TrustClass] = []
    for index, value in enumerate(model.trust_filters):
        try:
            member = TrustClass(value)
        except ValueError:
            raise WireRejection(
                400, RULE_INVALID_VALUE, f"trust_filters[{index}]"
            ) from None
        if member in filters:
            raise WireRejection(400, RULE_INVALID_VALUE, f"trust_filters[{index}]")
        filters.append(member)
    return Retrieve(
        scope=_scope(model.scope, "scope"),
        query=model.query,
        budget=model.budget,
        trust_filters=frozenset(filters),
        as_of=_timestamp(model.as_of, "as_of"),
    )


def retrieve_result(value: RetrievalResult) -> RetrieveResult:
    return RetrieveResult(
        hits=[_retrieved_fact(hit) for hit in value.hits],
        budget_consumed=value.budget_consumed,
        budget_exhausted=value.budget_exhausted,
    )


def read_audit_events_command(model: ReadAuditEventsRequest) -> ReadAuditEvents:
    return ReadAuditEvents(
        realm_id=model.realm_id,
        scope_prefix=_segments(model.scope_prefix, "scope_prefix"),
        after_sequence=model.after_sequence,
        limit=model.limit,
    )


def read_audit_events_result(page: AuditEventPage) -> ReadAuditEventsResult:
    """I-54: the events are the canonical audit bytes, parsed back into
    JSON rather than re-modelled — the hash chain covers exactly those
    bytes, so anything that reconstructed them field by field could
    publish a document the chain does not cover."""
    return ReadAuditEventsResult(
        events=[json.loads(canonical_audit_bytes(event)) for event in page.events],
        next_after_sequence=page.next_after_sequence,
    )


def _retrieved_fact(hit: RetrievedFact) -> RetrievedFactBody:
    ingested = isinstance(hit.provenance, IngestedProvenance)
    promoted = None if ingested else cast(PromotedProvenance, hit.provenance)
    return RetrievedFactBody(
        fact_id=encode_uuid(hit.fact_id),
        body=hit.body,
        scope=ScopeBody(
            realm=hit.scope.realm,
            segments=[
                SegmentBody(kind=segment.kind, identifier=segment.identifier)
                for segment in hit.scope.segments
            ],
        ),
        classification=hit.classification.value,
        trust=hit.trust.value,
        assertion_id=(
            encode_uuid(cast(IngestedProvenance, hit.provenance).assertion_id)
            if ingested
            else None
        ),
        derived_from=None if promoted is None else encode_uuid(promoted.derived_from),
        promoted_by=None if promoted is None else encode_uuid(promoted.promoted_by),
        evidence_id=None if promoted is None else encode_uuid(promoted.evidence_id),
        valid_from=(
            None if hit.valid_from is None else encode_timestamp(hit.valid_from)
        ),
        valid_to=None if hit.valid_to is None else encode_timestamp(hit.valid_to),
        recorded_at=encode_timestamp(hit.recorded_at),
        invalidated_at=(
            None if hit.invalidated_at is None else encode_timestamp(hit.invalidated_at)
        ),
    )


def _segments(values: list[SegmentBody], field_path: str) -> tuple[ScopeSegment, ...]:
    try:
        return tuple(
            ScopeSegment(kind=segment.kind, identifier=segment.identifier)
            for segment in values
        )
    except AuditValueError as error:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path) from error


def _fact_ids(values: list[str]) -> tuple[UUID, ...]:
    return tuple(
        _uuid(value, f"fact_ids[{index}]") for index, value in enumerate(values)
    )


def _scope(body: ScopeBody, field_path: str) -> Scope:
    segments = _segments(body.segments, field_path)
    try:
        return Scope(realm=body.realm, segments=segments)
    except AuditValueError as error:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path) from error


def _fact_draft(fact: FactBody, index: int) -> FactDraft:
    try:
        return FactDraft(
            body=fact.body,
            valid_from=_timestamp(fact.valid_from, f"facts[{index}].valid_from"),
            valid_to=_timestamp(fact.valid_to, f"facts[{index}].valid_to"),
        )
    except CustodyValueError as error:
        raise WireRejection(400, RULE_INVALID_VALUE, f"facts[{index}]") from error


def _coerce[EnumT](
    vocabulary: Callable[[str], EnumT], value: str, field_path: str
) -> EnumT:
    try:
        return vocabulary(value)
    except ValueError:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path) from None


def _uuid(value: str, field_path: str) -> UUID:
    """I-71: UUIDs are canonical lowercase hyphenated strings — the same
    round-trip check P-28 applies to the idempotency key."""
    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
    except ValueError:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path) from None
    return parsed


def _digest(value: str, field_path: str) -> bytes:
    """I-71: digests are 64-character lowercase hex strings."""
    if _DIGEST_HEX.fullmatch(value) is None:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path)
    return bytes.fromhex(value)


def _timestamp(value: str | None, field_path: str) -> datetime | None:
    """I-28: an incoming observation timestamp must carry an offset and is
    normalised; a naive or unparseable value is ``invalid_request``."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path) from None
    if parsed.tzinfo is None:
        raise WireRejection(400, RULE_INVALID_VALUE, field_path)
    return parsed
