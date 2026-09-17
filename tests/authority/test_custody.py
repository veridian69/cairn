import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from cairn.authority.custody import (
    AssertionRecord,
    CustodyValueError,
    EvidenceRecord,
    ExactEvidence,
    ExternalEvidence,
    FactDraft,
    FactRecord,
    IngestedProvenance,
    InvalidationRecord,
    PromotedProvenance,
    SourceType,
)
from cairn.catalogue.audit import (
    AuditValueError,
    Classification,
    ScopeSegment,
    TrustClass,
)

_NOW = datetime(2026, 8, 6, 9, 30, 0, tzinfo=UTC)
_NAIVE_NOW = datetime(2026, 8, 6, 9, 30, 0)
_ASSERTION_ID = UUID("11111111-1111-4111-8111-111111111111")
_FACT_ID = UUID("22222222-2222-4222-8222-222222222222")
_EVIDENCE_ID = UUID("33333333-3333-4333-8333-333333333333")
_PRINCIPAL_ID = UUID("44444444-4444-4444-8444-444444444444")
_DERIVED_FROM = UUID("55555555-5555-4555-8555-555555555555")
_PROMOTED_BY = UUID("66666666-6666-4666-8666-666666666666")
_SEGMENTS = (ScopeSegment(kind="job", identifier="42"),)
_DIGEST = bytes(range(32))


def _assertion(**overrides: object) -> AssertionRecord:
    fields: dict[str, object] = dict(
        assertion_id=_ASSERTION_ID,
        realm_id="acme",
        segments=_SEGMENTS,
        classification=Classification.INTERNAL,
        source_type=SourceType.AGENT_CLAIM,
        principal_id=_PRINCIPAL_ID,
        observed_at=_NOW,
        metadata=None,
        recorded_at=_NOW,
    )
    fields.update(overrides)
    return AssertionRecord(**fields)  # type: ignore[arg-type]


def _fact(**overrides: object) -> FactRecord:
    fields: dict[str, object] = dict(
        fact_id=_FACT_ID,
        realm_id="acme",
        segments=_SEGMENTS,
        body="a fact",
        trust=TrustClass.CANDIDATE,
        classification=Classification.INTERNAL,
        provenance=IngestedProvenance(assertion_id=_ASSERTION_ID),
        valid_from=None,
        valid_to=None,
        recorded_at=_NOW,
    )
    fields.update(overrides)
    return FactRecord(**fields)  # type: ignore[arg-type]


def _invalidation(**overrides: object) -> InvalidationRecord:
    fields: dict[str, object] = dict(
        fact_id=_FACT_ID,
        invalidated_at=_NOW,
        principal_id=_PRINCIPAL_ID,
        superseded_by=None,
        reason="superseded by newer evidence",
    )
    fields.update(overrides)
    return InvalidationRecord(**fields)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> EvidenceRecord:
    fields: dict[str, object] = dict(
        evidence_id=_EVIDENCE_ID,
        realm_id="acme",
        segments=_SEGMENTS,
        classification=Classification.INTERNAL,
        custody=ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=10, payload_digest=_DIGEST
        ),
        recorded_at=_NOW,
    )
    fields.update(overrides)
    return EvidenceRecord(**fields)  # type: ignore[arg-type]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- FactDraft.body / FactRecord.body: I-30 body bound (1-65536 UTF-8 octets) ---


def test_fact_draft_accepts_body_at_65536_bytes() -> None:
    FactDraft(body="a" * 65536, valid_from=None, valid_to=None)


def test_fact_draft_rejects_body_at_65537_bytes() -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body="a" * 65537, valid_from=None, valid_to=None)

    assert caught.value.code == "invalid_body"


def test_fact_draft_rejects_empty_body() -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body="", valid_from=None, valid_to=None)

    assert caught.value.code == "invalid_body"


def test_fact_draft_accepts_body_at_one_byte() -> None:
    FactDraft(body="a", valid_from=None, valid_to=None)


def test_fact_record_also_enforces_the_body_bound() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(body="a" * 65537)

    assert caught.value.code == "invalid_body"


def test_fact_record_accepts_body_at_65536_bytes() -> None:
    _fact(body="a" * 65536)


def test_fact_draft_rejects_body_containing_a_lone_surrogate() -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body="\ud800", valid_from=None, valid_to=None)

    assert caught.value.code == "invalid_body"


def test_fact_draft_accepts_multi_byte_body_at_65536_bytes() -> None:
    # U+1D54A is 4 UTF-8 bytes; 16384 of them is exactly 65536 octets but
    # only 16384 characters, so a character-count bound would not catch a
    # regression to len(value) in place of len(value.encode("utf-8")).
    body = "\U0001d54a" * 16384
    assert len(body.encode("utf-8")) == 65536
    FactDraft(body=body, valid_from=None, valid_to=None)


def test_fact_draft_rejects_multi_byte_body_at_65537_bytes() -> None:
    body = "\U0001d54a" * 16384 + "x"
    assert len(body.encode("utf-8")) == 65537
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body=body, valid_from=None, valid_to=None)

    assert caught.value.code == "invalid_body"


# --- FactDraft valid_from < valid_to ---


def test_fact_draft_accepts_ordered_validity_window() -> None:
    FactDraft(body="x", valid_from=_NOW, valid_to=datetime(2026, 8, 7, tzinfo=UTC))


def test_fact_draft_rejects_equal_validity_bounds() -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body="x", valid_from=_NOW, valid_to=_NOW)

    assert caught.value.code == "invalid_validity"


def test_fact_draft_rejects_inverted_validity_bounds() -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(
            body="x",
            valid_from=datetime(2026, 8, 7, tzinfo=UTC),
            valid_to=_NOW,
        )

    assert caught.value.code == "invalid_validity"


def test_fact_draft_accepts_open_validity_window() -> None:
    FactDraft(body="x", valid_from=None, valid_to=None)


# --- InvalidationRecord.reason: I-30 reason bound (1-4096 UTF-8 octets) ---


def test_invalidation_record_accepts_reason_at_4096_bytes() -> None:
    _invalidation(reason="a" * 4096)


def test_invalidation_record_rejects_reason_at_4097_bytes() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _invalidation(reason="a" * 4097)

    assert caught.value.code == "invalid_reason"


def test_invalidation_record_rejects_empty_reason() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _invalidation(reason="")

    assert caught.value.code == "invalid_reason"


def test_invalidation_record_accepts_multi_byte_reason_at_4096_bytes() -> None:
    # "é" is 2 UTF-8 bytes; 2048 of them is exactly 4096 octets but only
    # 2048 characters, isolating the octet bound from a character-count bug.
    reason = "é" * 2048
    assert len(reason.encode("utf-8")) == 4096
    _invalidation(reason=reason)


def test_invalidation_record_rejects_multi_byte_reason_at_4097_bytes() -> None:
    reason = "é" * 2048 + "a"
    assert len(reason.encode("utf-8")) == 4097
    with pytest.raises(CustodyValueError) as caught:
        _invalidation(reason=reason)

    assert caught.value.code == "invalid_reason"


# --- AssertionRecord.metadata: canonical strict JSON, <=65536 bytes, <=8 levels, <=256 keys ---


def test_assertion_accepts_none_metadata() -> None:
    _assertion(metadata=None)


def test_assertion_accepts_none_observed_at() -> None:
    _assertion(observed_at=None)


def test_assertion_accepts_canonical_metadata() -> None:
    _assertion(metadata=_canonical({"b": 1, "a": [1, 2, 3]}))


def test_assertion_rejects_metadata_over_65536_bytes() -> None:
    oversized = _canonical({"pad": "a" * 65536})
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=oversized)

    assert caught.value.code == "invalid_metadata"


def test_assertion_accepts_metadata_at_65536_bytes() -> None:
    # {"a":"...."} framing is 7 bytes ('{"a":"' + '"}'), pad the rest.
    padding = "a" * (65536 - len('{"a":""}'))
    metadata = _canonical({"a": padding})
    assert len(metadata.encode("utf-8")) == 65536
    _assertion(metadata=metadata)


def test_assertion_accepts_multi_byte_metadata_at_65536_bytes() -> None:
    # "é" is 2 UTF-8 bytes, so this isolates the octet bound from a
    # character-count bug the way the all-ASCII case above cannot.
    padding = "é" * 32764
    metadata = _canonical({"a": padding})
    assert len(metadata.encode("utf-8")) == 65536
    _assertion(metadata=metadata)


def test_assertion_rejects_multi_byte_metadata_at_65537_bytes() -> None:
    padding = "é" * 32764 + "x"
    metadata = _canonical({"a": padding})
    assert len(metadata.encode("utf-8")) == 65537
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=metadata)

    assert caught.value.code == "invalid_metadata"


def test_assertion_rejects_metadata_nested_nine_levels_deep() -> None:
    value: object = "leaf"
    for _ in range(9):
        value = {"n": value}
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=_canonical(value))

    assert caught.value.code == "invalid_metadata"


def test_assertion_accepts_metadata_nested_eight_levels_deep() -> None:
    value: object = "leaf"
    for _ in range(8):
        value = {"n": value}
    _assertion(metadata=_canonical(value))


def test_assertion_rejects_metadata_with_257_keys() -> None:
    value = {f"k{index}": index for index in range(257)}
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=_canonical(value))

    assert caught.value.code == "invalid_metadata"


def test_assertion_accepts_metadata_with_256_keys() -> None:
    value = {f"k{index}": index for index in range(256)}
    _assertion(metadata=_canonical(value))


def _wide_nested_value(levels: int, width: int) -> object:
    value: object = {f"k{index}": index for index in range(width)}
    for _ in range(levels - 1):
        inner: dict[str, object] = {f"k{index}": index for index in range(width - 1)}
        inner["n"] = value
        value = inner
    return value


def test_metadata_key_count_is_totalled_across_the_whole_structure() -> None:
    # Controller ruling: "at most 256 keys" counts every key anywhere in the
    # nested value, not the widest single object. 8 levels x 40 keys/level
    # = 320 keys total, but only 40 keys in any one object (depth 8, at the
    # allowed boundary) — a per-object reading would accept this (40 <=
    # 256); the ruled total-count reading rejects it.
    value = _wide_nested_value(levels=8, width=40)
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=_canonical(value))

    assert caught.value.code == "invalid_metadata"


def test_assertion_rejects_non_canonical_metadata_whitespace() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata='{"a": 1}')

    assert caught.value.code == "invalid_metadata"


def test_assertion_rejects_non_canonical_metadata_key_order() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata='{"b":1,"a":2}')

    assert caught.value.code == "invalid_metadata"


def test_assertion_rejects_deeply_nested_metadata_without_leaking_recursion_error() -> (
    None
):
    # Regression: this shape is well within the 65536-byte bound but its
    # 20000-level nesting must be rejected as invalid_metadata *before*
    # json.loads is called on it, not leaked as a bare RecursionError.
    metadata = "[" * 20000 + "]" * 20000
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=metadata)

    assert caught.value.code == "invalid_metadata"


def test_metadata_string_content_with_brace_noise_does_not_inflate_depth() -> None:
    # The raw pre-parse depth scan must treat "{[" characters, escaped
    # quotes and a trailing escaped backslash inside a JSON string as
    # ordinary content, not structure. Real depth here is 1 (a single
    # top-level object); a scanner fooled by string content would see the
    # ten unescaped "{[" pairs as real nesting and wrongly reject this.
    content = "{[" * 10 + 'has "quotes" and a trailing backslash\\'
    metadata = _canonical({"a": content})

    _assertion(metadata=metadata)


def test_assertion_rejects_malformed_metadata_json() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata="{not json")

    assert caught.value.code == "invalid_metadata"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_assertion_rejects_metadata_containing_forbidden_constants(
    constant: str,
) -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=f'{{"x":{constant}}}')

    assert caught.value.code == "invalid_metadata"


# --- payload_digest: exactly 32 bytes ---


def test_exact_evidence_accepts_32_byte_digest() -> None:
    ExactEvidence(
        assertion_id=_ASSERTION_ID, payload_length=1, payload_digest=bytes(32)
    )


def test_exact_evidence_rejects_31_byte_digest() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=1, payload_digest=bytes(31)
        )

    assert caught.value.code == "invalid_digest"


def test_exact_evidence_rejects_33_byte_digest() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=1, payload_digest=bytes(33)
        )

    assert caught.value.code == "invalid_digest"


def test_external_evidence_also_enforces_the_digest_bound() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExternalEvidence(external_uri="s:x", payload_digest=bytes(31))

    assert caught.value.code == "invalid_digest"


def test_external_evidence_accepts_32_byte_digest() -> None:
    ExternalEvidence(external_uri="s:x", payload_digest=bytes(32))


# --- ExactEvidence.payload_length: 1-1048576 ---


def test_exact_evidence_rejects_zero_payload_length() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=0, payload_digest=_DIGEST
        )

    assert caught.value.code == "invalid_payload"


def test_exact_evidence_rejects_1048577_payload_length() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=1048577, payload_digest=_DIGEST
        )

    assert caught.value.code == "invalid_payload"


def test_exact_evidence_accepts_payload_length_at_one() -> None:
    ExactEvidence(assertion_id=_ASSERTION_ID, payload_length=1, payload_digest=_DIGEST)


def test_exact_evidence_accepts_payload_length_at_1048576() -> None:
    ExactEvidence(
        assertion_id=_ASSERTION_ID, payload_length=1048576, payload_digest=_DIGEST
    )


def test_exact_evidence_rejects_bool_payload_length() -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=True, payload_digest=_DIGEST
        )

    assert caught.value.code == "invalid_payload"


# --- ExternalEvidence.external_uri: ASCII absolute URI, 1-2048 bytes, no whitespace/control ---


def test_external_evidence_accepts_uri_at_2048_bytes() -> None:
    uri = "s:" + "x" * (2048 - len("s:"))
    assert len(uri.encode("utf-8")) == 2048
    ExternalEvidence(external_uri=uri, payload_digest=_DIGEST)


def test_external_evidence_rejects_uri_at_2049_bytes() -> None:
    uri = "s:" + "x" * (2049 - len("s:"))
    with pytest.raises(CustodyValueError) as caught:
        ExternalEvidence(external_uri=uri, payload_digest=_DIGEST)

    assert caught.value.code == "invalid_uri"


@pytest.mark.parametrize(
    "uri",
    [
        "relative/path",
        "://missing-scheme",
        "s:has space",
        "s:has\tcontrol",
        "s:has\x01control",
        "s:non-ascii-é",
        "",
    ],
)
def test_external_evidence_rejects_malformed_uris(uri: str) -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExternalEvidence(external_uri=uri, payload_digest=_DIGEST)

    assert caught.value.code == "invalid_uri"


def test_external_evidence_accepts_realistic_absolute_uris() -> None:
    for uri in (
        "https://example.com/blob/1",
        "s3://bucket/key",
        "urn:uuid:" + str(_FACT_ID),
    ):
        ExternalEvidence(external_uri=uri, payload_digest=_DIGEST)


# --- wrong enum member types ---


def test_assertion_rejects_source_type_given_as_plain_string() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(source_type="agent-claim")

    assert caught.value.code == "invalid_source_type"


def test_assertion_rejects_source_type_given_as_wrong_enum() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(source_type=Classification.PUBLIC)

    assert caught.value.code == "invalid_source_type"


def test_fact_record_rejects_trust_given_as_plain_string() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(trust="validated")

    assert caught.value.code == "invalid_trust"


def test_fact_record_rejects_trust_given_as_wrong_enum() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(trust=Classification.PUBLIC)

    assert caught.value.code == "invalid_trust"


# --- Classification is deliberately not validated at this layer (I-65: single vocabulary,
#     mypy plus the schema CHECK plus Task 12 verification are the enforcement points) ---


def test_classification_wrong_value_is_not_checked_by_this_layer() -> None:
    _assertion(classification="not-a-classification")
    _fact(classification="not-a-classification")
    _evidence(classification="not-a-classification")


# --- UUID fields are deliberately not validated at this layer (mypy plus the schema's
#     canonical-v4-UUID CHECK plus Task 12 verification are the enforcement points) ---


def test_uuid_fields_are_not_checked_by_this_layer() -> None:
    _assertion(assertion_id="not-a-uuid")
    _fact(fact_id="not-a-uuid")
    _invalidation(principal_id="not-a-uuid")


# --- tz-naive datetimes rejected (invalid_validity) on every temporal field ---


def test_assertion_rejects_naive_observed_at() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(observed_at=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


def test_assertion_rejects_naive_recorded_at() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _assertion(recorded_at=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


def test_fact_record_rejects_naive_recorded_at() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(recorded_at=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


def test_fact_record_rejects_naive_valid_from() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(valid_from=_NAIVE_NOW, valid_to=_NOW)

    assert caught.value.code == "invalid_validity"


def test_fact_record_rejects_naive_valid_to() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _fact(valid_from=_NOW, valid_to=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


def test_invalidation_record_rejects_naive_invalidated_at() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _invalidation(invalidated_at=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


def test_evidence_record_rejects_naive_recorded_at() -> None:
    with pytest.raises(CustodyValueError) as caught:
        _evidence(recorded_at=_NAIVE_NOW)

    assert caught.value.code == "invalid_validity"


# --- scope handling: delegated to cairn.catalogue.audit.Scope (I-65, no parallel vocabulary) ---


def test_assertion_accepts_zero_segments_as_explicit_realm_root() -> None:
    _assertion(segments=())


def test_evidence_record_accepts_zero_segments_as_explicit_realm_root() -> None:
    _evidence(segments=())


def test_assertion_rejects_invalid_realm_via_scope_delegation() -> None:
    with pytest.raises(AuditValueError) as caught:
        _assertion(realm_id="Not A Realm!")

    assert caught.value.code == "invalid_realm"


def test_fact_record_rejects_seventeen_segments_via_scope_delegation() -> None:
    segments = tuple(
        ScopeSegment(kind="job", identifier=str(index)) for index in range(17)
    )
    with pytest.raises(AuditValueError) as caught:
        _fact(segments=segments)

    assert caught.value.code == "invalid_scope"


# --- PromotedProvenance / IngestedProvenance construct with unchecked UUID fields ---


def test_promoted_provenance_constructs() -> None:
    PromotedProvenance(
        derived_from=_DERIVED_FROM, promoted_by=_PROMOTED_BY, evidence_id=_EVIDENCE_ID
    )


def test_ingested_provenance_constructs() -> None:
    IngestedProvenance(assertion_id=_ASSERTION_ID)


def test_fact_record_accepts_promoted_provenance() -> None:
    _fact(
        provenance=PromotedProvenance(
            derived_from=_DERIVED_FROM,
            promoted_by=_PROMOTED_BY,
            evidence_id=_EVIDENCE_ID,
        )
    )


# --- I-23 Hypothesis invariants ---


_ascii_body_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=200,
)
_aware_datetimes = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 1, 1),
).map(lambda value: value.replace(tzinfo=UTC))


@given(
    body=_ascii_body_text,
    valid_from=st.none() | _aware_datetimes,
    valid_to=st.none() | _aware_datetimes,
)
def test_valid_fact_drafts_always_construct(
    body: str, valid_from: datetime | None, valid_to: datetime | None
) -> None:
    if valid_from is not None and valid_to is not None:
        assume(valid_from < valid_to)

    FactDraft(body=body, valid_from=valid_from, valid_to=valid_to)


@given(length=st.integers(min_value=65537, max_value=70000))
def test_body_over_bound_always_raises_invalid_body_and_nothing_else(
    length: int,
) -> None:
    with pytest.raises(CustodyValueError) as caught:
        FactDraft(body="a" * length, valid_from=None, valid_to=None)

    assert caught.value.code == "invalid_body"


@given(length=st.integers(min_value=4097, max_value=8000))
def test_reason_over_bound_always_raises_invalid_reason_and_nothing_else(
    length: int,
) -> None:
    with pytest.raises(CustodyValueError) as caught:
        _invalidation(reason="a" * length)

    assert caught.value.code == "invalid_reason"


@given(
    length=st.integers(min_value=0, max_value=31)
    | st.integers(min_value=33, max_value=64)
)
def test_digest_wrong_length_always_raises_invalid_digest_and_nothing_else(
    length: int,
) -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=1, payload_digest=bytes(length)
        )

    assert caught.value.code == "invalid_digest"


@given(
    value=st.integers(max_value=0) | st.integers(min_value=1048577, max_value=2000000)
)
def test_payload_length_out_of_bounds_always_raises_invalid_payload(value: int) -> None:
    with pytest.raises(CustodyValueError) as caught:
        ExactEvidence(
            assertion_id=_ASSERTION_ID, payload_length=value, payload_digest=_DIGEST
        )

    assert caught.value.code == "invalid_payload"


@given(depth=st.integers(min_value=9, max_value=20))
def test_metadata_over_depth_bound_always_raises_invalid_metadata(depth: int) -> None:
    value: object = "leaf"
    for _ in range(depth):
        value = {"n": value}

    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=_canonical(value))

    assert caught.value.code == "invalid_metadata"


@given(depth=st.integers(min_value=21, max_value=50000))
def test_metadata_arbitrarily_deep_raw_json_always_raises_invalid_metadata_and_nothing_else(
    depth: int,
) -> None:
    # Built directly as text (no nested Python object, no json.dumps), so
    # the property itself never recurses and can safely reach depths well
    # past Python's default recursion limit — exactly the shape a
    # pre-parse-only depth check must reject before json.loads ever runs.
    metadata = "[" * depth + "]" * depth

    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=metadata)

    assert caught.value.code == "invalid_metadata"


@given(count=st.integers(min_value=257, max_value=400))
def test_metadata_over_key_bound_always_raises_invalid_metadata(count: int) -> None:
    value = {f"k{index}": index for index in range(count)}

    with pytest.raises(CustodyValueError) as caught:
        _assertion(metadata=_canonical(value))

    assert caught.value.code == "invalid_metadata"


# --- _validate_scope must check segment content, not merely segment type ----
#
# Tasks 8 and 9 build AssertionRecord/FactRecord from stored rows rather than
# from a command whose scope a caller already validated, so this validator is
# the barrier they depend on. Scope re-validates the realm, the tuple type and
# the length bound, but never looks inside a segment — segment content is
# ScopeSegment's own invariant.


def _bypassed_segment(*, kind: object = "job", identifier: object = "job-1") -> object:
    segment = ScopeSegment(kind="job", identifier="job-1")
    object.__setattr__(segment, "kind", kind)
    object.__setattr__(segment, "identifier", identifier)
    return segment


def test_fact_record_rejects_a_segment_with_hostile_identifier_content() -> None:
    with pytest.raises(AuditValueError) as caught:
        _fact(segments=(_bypassed_segment(identifier="bad id!"),))

    assert caught.value.code == "invalid_scope_segment_id"


def test_assertion_record_rejects_a_segment_with_hostile_kind_content() -> None:
    with pytest.raises(AuditValueError) as caught:
        _assertion(segments=(_bypassed_segment(kind="BAD"),))

    assert caught.value.code == "invalid_scope_segment_kind"


def test_fact_record_rejects_a_segment_holding_a_non_string_field() -> None:
    with pytest.raises(AuditValueError) as caught:
        _fact(segments=(_bypassed_segment(identifier=1),))

    assert caught.value.code == "invalid_scope_segment_id"


def test_a_row_shaped_scope_of_integers_is_refused_where_rows_are_parsed() -> None:
    """The route with no bypass at all: ``scope_segments`` is a JSON column
    whose CHECK pins minification only, so a row holding ``[{"kind": 7,
    "id": 1}]`` parses to ints. Rebuilding segments from it — what a stored-row
    reader does — must be a typed refusal, not a raw TypeError."""
    parsed = json.loads('[{"kind": 7, "id": 1}]')

    with pytest.raises(AuditValueError) as caught:
        tuple(ScopeSegment(kind=item["kind"], identifier=item["id"]) for item in parsed)

    assert caught.value.code == "invalid_scope_segment_kind"


def test_a_valid_row_shaped_scope_still_parses() -> None:
    parsed = json.loads('[{"id":"job-1","kind":"job"}]')
    segments = tuple(
        ScopeSegment(kind=item["kind"], identifier=item["id"]) for item in parsed
    )

    assert _fact(segments=segments).segments == segments
