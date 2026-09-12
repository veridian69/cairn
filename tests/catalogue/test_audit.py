import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest

from cairn.catalogue.audit import (
    ZERO_HASH,
    ActionKind,
    AuditDraft,
    AuditEvent,
    AuditValueError,
    ChainKind,
    Classification,
    ClassificationTransition,
    Outcome,
    Scope,
    ScopeSegment,
    TrustClass,
    TrustTransition,
    canonical_audit_bytes,
    hash_audit_event,
    parse_canonical_audit_bytes,
)


def _chained_realm_denial() -> AuditEvent:
    return AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.REALM,
            chain_identity="local",
            principal_id=UUID("99999999-9999-4999-8999-999999999999"),
            credential_verifier_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            grant_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            action_kind=ActionKind.ADMINISTRATION,
            action_code="scope-widen",
            source_scope=Scope(realm="local", segments=()),
            requested_scope=Scope(
                realm="local",
                segments=tuple(
                    ScopeSegment(
                        kind=f"level-{index:02d}", identifier=f"id/{index:02d}"
                    )
                    for index in range(16)
                ),
            ),
            target_scope=None,
            outcome=Outcome.DENY,
            reason_code="scope_forbidden",
            affected_assertion_ids=(UUID("44444444-4444-4444-8444-444444444444"),),
            affected_fact_ids=(
                UUID("55555555-5555-4555-8555-555555555555"),
                UUID("66666666-6666-4666-8666-666666666666"),
            ),
            affected_evidence_ids=(UUID("77777777-7777-4777-8777-777777777777"),),
            affected_grant_ids=(UUID("88888888-8888-4888-8888-888888888888"),),
            classification_transition=ClassificationTransition(
                previous=Classification.PUBLIC,
                current=Classification.RESTRICTED,
            ),
            trust_transition=TrustTransition(
                previous=TrustClass.CANDIDATE,
                current=TrustClass.VALIDATED,
            ),
            evidence_reference=None,
            evidence_digest=None,
            correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            idempotency_key=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            mutation_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            command_digest=bytes.fromhex("22" * 32),
            replay_of_mutation_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
            safe_request_fingerprint=bytes.fromhex("33" * 32),
        ),
        sequence=2,
        event_id=UUID("12345678-1234-4234-8234-123456789abc"),
        recorded_at=datetime(2026, 8, 5, 10, 12, 13, 654321, tzinfo=UTC),
        previous_hash=bytes.fromhex("44" * 32),
    )


def test_caller_supplied_uuid5_idempotency_key_round_trips_in_audit() -> None:
    """I-27 permits a canonical RFC 4122 caller key; applying the UUIDv4
    rule for Cairn-assigned identities would make Task 2's fixed UUIDv5 keys
    fail after admission but before the mutation can commit."""
    key = uuid5(
        UUID("c4961664-0ded-4ade-aa38-69214bad2678"),
        "graph-episode:synthetic-audit-regression",
    )
    original = _chained_realm_denial()
    event = replace(original, draft=replace(original.draft, idempotency_key=key))

    parsed = parse_canonical_audit_bytes(canonical_audit_bytes(event))

    assert parsed.draft.idempotency_key == key


def test_instance_genesis_event_has_literal_canonical_vector() -> None:
    event = AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.INSTANCE,
            chain_identity="11111111-1111-4111-8111-111111111111",
            principal_id=None,
            credential_verifier_id=None,
            grant_id=None,
            action_kind=ActionKind.SYSTEM,
            action_code="catalogue-created",
            source_scope=None,
            requested_scope=None,
            target_scope=None,
            outcome=Outcome.ALLOW,
            reason_code="catalogue_created",
            affected_assertion_ids=(),
            affected_fact_ids=(),
            affected_evidence_ids=(),
            affected_grant_ids=(),
            classification_transition=None,
            trust_transition=None,
            evidence_reference=None,
            evidence_digest=None,
            correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
            idempotency_key=None,
            mutation_id=None,
            command_digest=None,
            replay_of_mutation_id=None,
            safe_request_fingerprint=None,
        ),
        sequence=1,
        event_id=UUID("33333333-3333-4333-8333-333333333333"),
        recorded_at=datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC),
        previous_hash=ZERO_HASH,
    )
    expected = (
        b'{"action_code":"catalogue-created","action_kind":"system",'
        b'"affected_assertion_ids":[],"affected_evidence_ids":[],'
        b'"affected_fact_ids":[],"affected_grant_ids":[],'
        b'"chain_identity":"11111111-1111-4111-8111-111111111111",'
        b'"chain_kind":"instance","classification_transition":null,'
        b'"command_digest":null,'
        b'"correlation_id":"22222222-2222-4222-8222-222222222222",'
        b'"credential_verifier_id":null,'
        b'"event_id":"33333333-3333-4333-8333-333333333333",'
        b'"evidence_digest":null,"evidence_reference":null,"grant_id":null,'
        b'"idempotency_key":null,"mutation_id":null,"outcome":"allow",'
        b'"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
        b'"principal_id":null,"reason_code":"catalogue_created",'
        b'"recorded_at":"2026-08-05T10:11:12.123456Z",'
        b'"replay_of_mutation_id":null,"requested_scope":null,'
        b'"safe_request_fingerprint":null,"schema":"cairn.audit/v1",'
        b'"sequence":1,"source_scope":null,"target_scope":null,'
        b'"trust_transition":null}'
    )

    assert canonical_audit_bytes(event) == expected
    assert hash_audit_event(event).hex() == (
        "88c8ea48cbdf6405a51c1c12e5a8d548f0a69e8a5f625fdf61ada22b04b65603"
    )
    assert parse_canonical_audit_bytes(expected) == event


def test_realm_genesis_event_has_literal_root_scope_vector() -> None:
    event = AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.REALM,
            chain_identity="local",
            principal_id=None,
            credential_verifier_id=None,
            grant_id=None,
            action_kind=ActionKind.ADMINISTRATION,
            action_code="realm-bootstrap",
            source_scope=None,
            requested_scope=None,
            target_scope=Scope(realm="local", segments=()),
            outcome=Outcome.ALLOW,
            reason_code="realm_created",
            affected_assertion_ids=(),
            affected_fact_ids=(),
            affected_evidence_ids=(),
            affected_grant_ids=(),
            classification_transition=None,
            trust_transition=None,
            evidence_reference=None,
            evidence_digest=None,
            correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
            idempotency_key=None,
            mutation_id=None,
            command_digest=None,
            replay_of_mutation_id=None,
            safe_request_fingerprint=None,
        ),
        sequence=1,
        event_id=UUID("33333333-3333-4333-8333-333333333333"),
        recorded_at=datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC),
        previous_hash=ZERO_HASH,
    )
    expected = (
        b'{"action_code":"realm-bootstrap","action_kind":"administration",'
        b'"affected_assertion_ids":[],"affected_evidence_ids":[],'
        b'"affected_fact_ids":[],"affected_grant_ids":[],'
        b'"chain_identity":"local","chain_kind":"realm",'
        b'"classification_transition":null,"command_digest":null,'
        b'"correlation_id":"22222222-2222-4222-8222-222222222222",'
        b'"credential_verifier_id":null,'
        b'"event_id":"33333333-3333-4333-8333-333333333333",'
        b'"evidence_digest":null,"evidence_reference":null,"grant_id":null,'
        b'"idempotency_key":null,"mutation_id":null,"outcome":"allow",'
        b'"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
        b'"principal_id":null,"reason_code":"realm_created",'
        b'"recorded_at":"2026-08-05T10:11:12.123456Z",'
        b'"replay_of_mutation_id":null,"requested_scope":null,'
        b'"safe_request_fingerprint":null,"schema":"cairn.audit/v1",'
        b'"sequence":1,"source_scope":null,'
        b'"target_scope":{"realm":"local","segments":[]},'
        b'"trust_transition":null}'
    )

    assert canonical_audit_bytes(event) == expected
    assert parse_canonical_audit_bytes(expected) == event
    assert hash_audit_event(event).hex() == (
        "aef3e5ce68381b13a7a21033e120cc765a60b6c6e20c4c5f822d70e5425e9f48"
    )


def test_chained_denial_round_trips_root_and_sixteen_segment_scope() -> None:
    event = _chained_realm_denial()
    expected = (
        b'{"action_code":"scope-widen","action_kind":"administration",'
        b'"affected_assertion_ids":["44444444-4444-4444-8444-444444444444"],'
        b'"affected_evidence_ids":["77777777-7777-4777-8777-777777777777"],'
        b'"affected_fact_ids":["55555555-5555-4555-8555-555555555555",'
        b'"66666666-6666-4666-8666-666666666666"],'
        b'"affected_grant_ids":["88888888-8888-4888-8888-888888888888"],'
        b'"chain_identity":"local","chain_kind":"realm",'
        b'"classification_transition":{"from":"public","to":"restricted"},'
        b'"command_digest":"2222222222222222222222222222222222222222222222222222222222222222",'
        b'"correlation_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
        b'"credential_verifier_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
        b'"event_id":"12345678-1234-4234-8234-123456789abc",'
        b'"evidence_digest":null,'
        b'"evidence_reference":null,'
        b'"grant_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",'
        b'"idempotency_key":"dddddddd-dddd-4ddd-8ddd-dddddddddddd",'
        b'"mutation_id":"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",'
        b'"outcome":"deny",'
        b'"previous_hash":"4444444444444444444444444444444444444444444444444444444444444444",'
        b'"principal_id":"99999999-9999-4999-8999-999999999999",'
        b'"reason_code":"scope_forbidden",'
        b'"recorded_at":"2026-08-05T10:12:13.654321Z",'
        b'"replay_of_mutation_id":"ffffffff-ffff-4fff-8fff-ffffffffffff",'
        b'"requested_scope":{"realm":"local","segments":['
        b'{"id":"id/00","kind":"level-00"},'
        b'{"id":"id/01","kind":"level-01"},'
        b'{"id":"id/02","kind":"level-02"},'
        b'{"id":"id/03","kind":"level-03"},'
        b'{"id":"id/04","kind":"level-04"},'
        b'{"id":"id/05","kind":"level-05"},'
        b'{"id":"id/06","kind":"level-06"},'
        b'{"id":"id/07","kind":"level-07"},'
        b'{"id":"id/08","kind":"level-08"},'
        b'{"id":"id/09","kind":"level-09"},'
        b'{"id":"id/10","kind":"level-10"},'
        b'{"id":"id/11","kind":"level-11"},'
        b'{"id":"id/12","kind":"level-12"},'
        b'{"id":"id/13","kind":"level-13"},'
        b'{"id":"id/14","kind":"level-14"},'
        b'{"id":"id/15","kind":"level-15"}]},'
        b'"safe_request_fingerprint":"3333333333333333333333333333333333333333333333333333333333333333",'
        b'"schema":"cairn.audit/v1","sequence":2,'
        b'"source_scope":{"realm":"local","segments":[]},'
        b'"target_scope":null,'
        b'"trust_transition":{"from":"candidate","to":"validated"}}'
    )

    assert canonical_audit_bytes(event) == expected
    assert parse_canonical_audit_bytes(expected) == event
    assert hash_audit_event(event).hex() == (
        "9014032ddd27830f1cc1d104ef04bc4dc3c5995d8c375ec99fbaece223044e97"
    )


def test_allow_promote_event_has_literal_vector_with_both_evidence_fields() -> None:
    """The third golden vector, and the only one carrying a non-null
    ``evidence_digest`` and ``evidence_reference``.

    P-16's headline consequence is that a data-plane allow-promote event must
    carry *both* — and P-16 is also what forced the chained-denial vector's
    pair to null, since a deny event may carry neither. That left all three
    existing literals pinning the null rendering and nothing pinning the
    populated one: the digest's lower-hex form, its placement between
    ``event_id`` and ``evidence_reference`` in key order, and the two scope
    roles a promotion is the first action to populate.
    """
    event = AuditEvent(
        draft=AuditDraft(
            chain_kind=ChainKind.REALM,
            chain_identity="local",
            principal_id=UUID("99999999-9999-4999-8999-999999999999"),
            credential_verifier_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            grant_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            action_kind=ActionKind.DATA,
            action_code="promote",
            source_scope=Scope(
                realm="local",
                segments=(ScopeSegment(kind="job", identifier="job-1"),),
            ),
            requested_scope=None,
            target_scope=Scope(realm="local", segments=()),
            outcome=Outcome.ALLOW,
            reason_code="facts_promoted",
            affected_assertion_ids=(),
            affected_fact_ids=(
                UUID("55555555-5555-4555-8555-555555555555"),
                UUID("66666666-6666-4666-8666-666666666666"),
            ),
            affected_evidence_ids=(UUID("77777777-7777-4777-8777-777777777777"),),
            affected_grant_ids=(),
            classification_transition=ClassificationTransition(
                previous=Classification.INTERNAL,
                current=Classification.RESTRICTED,
            ),
            trust_transition=TrustTransition(
                previous=TrustClass.CANDIDATE,
                current=TrustClass.VALIDATED,
            ),
            evidence_reference=UUID("77777777-7777-4777-8777-777777777777"),
            evidence_digest=bytes.fromhex("11" * 32),
            correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            idempotency_key=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            mutation_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            command_digest=bytes.fromhex("22" * 32),
            replay_of_mutation_id=None,
            safe_request_fingerprint=None,
        ),
        sequence=3,
        event_id=UUID("12345678-1234-4234-8234-123456789abc"),
        recorded_at=datetime(2026, 8, 5, 10, 12, 13, 654321, tzinfo=UTC),
        previous_hash=bytes.fromhex("44" * 32),
    )
    expected = (
        b'{"action_code":"promote","action_kind":"data",'
        b'"affected_assertion_ids":[],'
        b'"affected_evidence_ids":["77777777-7777-4777-8777-777777777777"],'
        b'"affected_fact_ids":["55555555-5555-4555-8555-555555555555",'
        b'"66666666-6666-4666-8666-666666666666"],'
        b'"affected_grant_ids":[],"chain_identity":"local","chain_kind":"realm",'
        b'"classification_transition":{"from":"internal","to":"restricted"},'
        b'"command_digest":"2222222222222222222222222222222222222222222222222222222222222222",'
        b'"correlation_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
        b'"credential_verifier_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
        b'"event_id":"12345678-1234-4234-8234-123456789abc",'
        b'"evidence_digest":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"evidence_reference":"77777777-7777-4777-8777-777777777777",'
        b'"grant_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",'
        b'"idempotency_key":"dddddddd-dddd-4ddd-8ddd-dddddddddddd",'
        b'"mutation_id":"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",'
        b'"outcome":"allow",'
        b'"previous_hash":"4444444444444444444444444444444444444444444444444444444444444444",'
        b'"principal_id":"99999999-9999-4999-8999-999999999999",'
        b'"reason_code":"facts_promoted",'
        b'"recorded_at":"2026-08-05T10:12:13.654321Z",'
        b'"replay_of_mutation_id":null,"requested_scope":null,'
        b'"safe_request_fingerprint":null,"schema":"cairn.audit/v1","sequence":3,'
        b'"source_scope":{"realm":"local","segments":'
        b'[{"id":"job-1","kind":"job"}]},'
        b'"target_scope":{"realm":"local","segments":[]},'
        b'"trust_transition":{"from":"candidate","to":"validated"}}'
    )

    assert canonical_audit_bytes(event) == expected
    assert parse_canonical_audit_bytes(expected) == event
    assert hash_audit_event(event).hex() == (
        "64b07eceac313da58668c122753bb4f6819a33d5647cc5cfc98eb8a0cd534bf6"
    )


def _decoded_denial() -> dict[str, object]:
    document = json.loads(canonical_audit_bytes(_chained_realm_denial()))
    assert isinstance(document, dict)
    return document


@pytest.mark.parametrize("change", ["unknown", "omitted", "free_form"])
def test_parser_rejects_non_closed_event_fields(change: str) -> None:
    document = _decoded_denial()
    if change == "omitted":
        del document["grant_id"]
    elif change == "unknown":
        document["extension"] = None
    else:
        document["message"] = "free text"
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(encoded)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 1.5),
        ("principal_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("recorded_at", "2026-08-05T10:12:13.654321+00:00"),
        ("previous_hash", "A" * 64),
        ("action_code", "rétrieve"),
    ],
)
def test_parser_rejects_noncanonical_scalar_fields(
    field: str,
    value: object,
) -> None:
    document = _decoded_denial()
    document[field] = value
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(encoded)


def test_parser_rejects_unsorted_identity_set() -> None:
    document = _decoded_denial()
    facts = document["affected_fact_ids"]
    assert isinstance(facts, list)
    document["affected_fact_ids"] = list(reversed(facts))

    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )


def test_parser_rejects_seventeen_scope_segments() -> None:
    document = _decoded_denial()
    requested_scope = document["requested_scope"]
    assert isinstance(requested_scope, dict)
    segments = requested_scope["segments"]
    assert isinstance(segments, list)
    segments.append({"id": "id/16", "kind": "level-16"})

    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )


def test_parser_rejects_duplicate_json_field() -> None:
    encoded = canonical_audit_bytes(_chained_realm_denial()).replace(
        b'{"action_code":',
        b'{"action_code":"duplicate","action_code":',
        1,
    )

    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"sequence":1,"schema":"cairn.audit/v1"}',
        canonical_audit_bytes(_chained_realm_denial()) + b"\n",
    ],
)
def test_parser_rejects_noncanonical_json_encoding(encoded: bytes) -> None:
    with pytest.raises(AuditValueError):
        parse_canonical_audit_bytes(encoded)


def test_instance_chain_rejects_scope_text() -> None:
    realm_draft = _chained_realm_denial().draft

    with pytest.raises(AuditValueError) as caught:
        replace(
            realm_draft,
            chain_kind=ChainKind.INSTANCE,
            chain_identity="11111111-1111-4111-8111-111111111111",
        )

    assert caught.value.code == "instance_scope_forbidden"


def test_event_rejects_inconsistent_genesis_hash() -> None:
    event = _chained_realm_denial()

    with pytest.raises(AuditValueError) as caught:
        replace(event, sequence=1, previous_hash=bytes.fromhex("44" * 32))

    assert caught.value.code == "invalid_previous_hash"


# --- P-16: per-action evidence-field validation, replacing the wrong global
# ambiguous_evidence_reference XOR. Truth table (see plan): allow+promote
# requires both fields; allow+ingest carries both or neither, never one
# alone; every other action code and every non-allow outcome carries
# neither, because the evidence may be precisely what is unknown or
# unauthorised (denials) or the action never has evidence at all.


def _evidence_draft(**overrides: object) -> AuditDraft:
    fields: dict[str, object] = dict(
        chain_kind=ChainKind.REALM,
        chain_identity="acme",
        principal_id=UUID("99999999-9999-4999-8999-999999999999"),
        credential_verifier_id=None,
        grant_id=None,
        action_kind=ActionKind.DATA,
        action_code="promote",
        source_scope=None,
        requested_scope=None,
        target_scope=Scope(realm="acme", segments=()),
        outcome=Outcome.ALLOW,
        reason_code="fact_promoted",
        affected_assertion_ids=(),
        affected_fact_ids=(UUID("55555555-5555-4555-8555-555555555555"),),
        affected_evidence_ids=(),
        affected_grant_ids=(),
        classification_transition=None,
        trust_transition=None,
        evidence_reference=UUID("77777777-7777-4777-8777-777777777777"),
        evidence_digest=bytes.fromhex("11" * 32),
        correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=None,
    )
    fields.update(overrides)
    return AuditDraft(**fields)  # type: ignore[arg-type]


def _evidence_event(draft: AuditDraft) -> AuditEvent:
    return AuditEvent(
        draft=draft,
        sequence=1,
        event_id=UUID("33333333-3333-4333-8333-333333333333"),
        recorded_at=datetime(2026, 8, 6, 9, 30, 0, tzinfo=UTC),
        previous_hash=ZERO_HASH,
    )


def test_allow_promote_with_both_evidence_fields_constructs_and_round_trips() -> None:
    event = _evidence_event(_evidence_draft())

    encoded = canonical_audit_bytes(event)

    assert parse_canonical_audit_bytes(encoded) == event
    hash_audit_event(event)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "evidence_reference": UUID("77777777-7777-4777-8777-777777777777"),
            "evidence_digest": None,
        },
        {"evidence_reference": None, "evidence_digest": bytes.fromhex("11" * 32)},
        {"evidence_reference": None, "evidence_digest": None},
    ],
    ids=["reference_only", "digest_only", "neither"],
)
def test_allow_promote_requires_both_evidence_fields(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(**overrides)

    assert caught.value.code == "evidence_fields_required"


def test_administration_kind_promote_action_code_forbids_evidence_fields() -> None:
    # P-16 is pinned to action_kind, action_code and outcome together — an
    # administration- or system-kind event that happens to reuse the
    # action_code "promote" is not a data-plane promotion and must not be
    # forced to carry evidence fields.
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(action_kind=ActionKind.ADMINISTRATION, action_code="promote")

    assert caught.value.code == "evidence_fields_forbidden"


def test_administration_kind_promote_action_code_constructs_with_neither_field() -> (
    None
):
    _evidence_draft(
        action_kind=ActionKind.ADMINISTRATION,
        action_code="promote",
        evidence_reference=None,
        evidence_digest=None,
    )


def test_allow_ingest_with_both_evidence_fields_constructs() -> None:
    _evidence_draft(action_code="ingest")


def test_allow_ingest_with_neither_evidence_field_constructs() -> None:
    _evidence_draft(action_code="ingest", evidence_reference=None, evidence_digest=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "evidence_reference": UUID("77777777-7777-4777-8777-777777777777"),
            "evidence_digest": None,
        },
        {"evidence_reference": None, "evidence_digest": bytes.fromhex("11" * 32)},
    ],
    ids=["reference_only", "digest_only"],
)
def test_allow_ingest_rejects_exactly_one_evidence_field(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(action_code="ingest", **overrides)

    assert caught.value.code == "evidence_fields_unpaired"


def test_allow_invalidate_with_neither_field_constructs() -> None:
    _evidence_draft(
        action_code="invalidate", evidence_reference=None, evidence_digest=None
    )


def test_allow_invalidate_rejects_evidence_digest() -> None:
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(action_code="invalidate", evidence_reference=None)

    assert caught.value.code == "evidence_fields_forbidden"


def test_deny_promote_rejects_evidence_fields_despite_allow_promote_requiring_them() -> (
    None
):
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(outcome=Outcome.DENY, reason_code="scope_forbidden")

    assert caught.value.code == "evidence_fields_forbidden"


def test_deny_data_action_with_neither_field_constructs() -> None:
    _evidence_draft(
        outcome=Outcome.DENY,
        reason_code="scope_forbidden",
        evidence_reference=None,
        evidence_digest=None,
    )


def test_error_outcome_rejects_evidence_fields() -> None:
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(outcome=Outcome.ERROR, reason_code="internal_error")

    assert caught.value.code == "evidence_fields_forbidden"


@pytest.mark.parametrize(
    ("action_kind", "action_code"),
    [
        (ActionKind.ADMINISTRATION, "scope-widen"),
        (ActionKind.SYSTEM, "catalogue-created"),
        (ActionKind.ADMINISTRATION, "audit-read"),
    ],
    ids=["administration", "system", "audit_read"],
)
def test_administration_system_and_audit_read_reject_evidence_fields(
    action_kind: ActionKind, action_code: str
) -> None:
    with pytest.raises(AuditValueError) as caught:
        _evidence_draft(action_kind=action_kind, action_code=action_code)

    assert caught.value.code == "evidence_fields_forbidden"


@pytest.mark.parametrize(
    ("action_kind", "action_code"),
    [
        (ActionKind.ADMINISTRATION, "scope-widen"),
        (ActionKind.SYSTEM, "catalogue-created"),
        (ActionKind.ADMINISTRATION, "audit-read"),
    ],
    ids=["administration", "system", "audit_read"],
)
def test_administration_system_and_audit_read_construct_with_neither_field(
    action_kind: ActionKind, action_code: str
) -> None:
    _evidence_draft(
        action_kind=action_kind,
        action_code=action_code,
        evidence_reference=None,
        evidence_digest=None,
    )


def test_historical_none_none_shape_still_constructs_and_round_trips() -> None:
    # Every slice 1-3 event carried both evidence fields as None. P-16 must
    # not disturb that shape for any action code, chain kind or outcome.
    event = _evidence_event(
        _evidence_draft(
            action_kind=ActionKind.ADMINISTRATION,
            action_code="realm-bootstrap",
            evidence_reference=None,
            evidence_digest=None,
        )
    )

    encoded = canonical_audit_bytes(event)

    assert parse_canonical_audit_bytes(encoded) == event


# --- non-str scope fields are typed refusals, not raw TypeErrors -------------
#
# The regexes below these constructors accept only str. Without an explicit
# type check they raise TypeError from inside `re`, which is not an
# AuditValueError and so carries no code for a caller to map to an audit
# reason — the value escapes with no durable event at all. Scope paths reach
# these constructors from stored JSON columns as well as from callers, where
# `[{"kind": 7, "id": 1}]` parses to ints with no tampering involved.


def test_scope_segment_rejects_a_non_string_kind() -> None:
    with pytest.raises(AuditValueError) as caught:
        ScopeSegment(kind=7, identifier="job-1")  # type: ignore[arg-type]

    assert caught.value.code == "invalid_scope_segment_kind"


def test_scope_segment_rejects_a_non_string_identifier() -> None:
    with pytest.raises(AuditValueError) as caught:
        ScopeSegment(kind="job", identifier=1)  # type: ignore[arg-type]

    assert caught.value.code == "invalid_scope_segment_id"


def test_scope_segment_rejects_a_none_identifier() -> None:
    with pytest.raises(AuditValueError) as caught:
        ScopeSegment(kind="job", identifier=None)  # type: ignore[arg-type]

    assert caught.value.code == "invalid_scope_segment_id"


def test_scope_rejects_a_non_string_realm() -> None:
    with pytest.raises(AuditValueError) as caught:
        Scope(realm=7, segments=())  # type: ignore[arg-type]

    assert caught.value.code == "invalid_realm"


def test_valid_scope_values_are_unaffected_by_the_type_check() -> None:
    """The check may only narrow: every previously-valid value still
    constructs, and the accepted set is unchanged."""
    segment = ScopeSegment(kind="job", identifier="job-1")
    assert Scope(realm="acme", segments=(segment,)).segments == (segment,)
    assert Scope(realm="a", segments=()).realm == "a"
    assert ScopeSegment(kind="j", identifier="A").identifier == "A"
