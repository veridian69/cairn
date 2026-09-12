import re
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from cairn.transports.v1.wire import (
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_IDEMPOTENCY_KEY_DUPLICATED,
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_INVALID_VALUE,
    RULE_MALFORMED_JSON,
    RULE_METHOD_NOT_ALLOWED,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
    WIRE_RULES,
    AuditReceiptBody,
    FailureBody,
    FailureEnvelope,
    InvalidRequestDetail,
    MutationReceiptBody,
    SecretRejectedDetail,
    SuccessEnvelope,
    WireModel,
    WireOutcome,
    encode_digest,
    encode_timestamp,
    encode_uuid,
)

_RULE_CONSTANTS = (
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_IDEMPOTENCY_KEY_DUPLICATED,
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_INVALID_VALUE,
    RULE_MALFORMED_JSON,
    RULE_METHOD_NOT_ALLOWED,
    RULE_MISSING_FIELD,
    RULE_UNKNOWN_FIELD,
)


class _StubResult(WireModel):
    """A minimal result model standing in for a per-route response body."""

    fact_id: str


def test_every_rule_constant_is_pinned_by_the_closed_set() -> None:
    assert frozenset(_RULE_CONSTANTS) == WIRE_RULES
    assert len(_RULE_CONSTANTS) == len(WIRE_RULES)


def test_every_rule_identity_is_bare_snake_case() -> None:
    for rule in WIRE_RULES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", rule) is not None


def test_uuid_encoding_is_canonical_lowercase_hyphenated() -> None:
    value = UUID("3FA85F64-5717-4562-B3FC-2C963F66AFA6")
    assert encode_uuid(value) == "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def test_timestamp_encoding_is_the_27_character_canonical_form() -> None:
    encoded = encode_timestamp(datetime(2026, 8, 7, 9, 30, 15, 123456, tzinfo=UTC))
    assert encoded == "2026-08-07T09:30:15.123456Z"
    assert len(encoded) == 27


def test_timestamp_encoding_normalises_to_utc() -> None:
    offset = timezone(timedelta(hours=2))
    encoded = encode_timestamp(datetime(2026, 8, 7, 11, 30, 15, 0, tzinfo=offset))
    assert encoded == "2026-08-07T09:30:15.000000Z"


def test_digest_encoding_is_64_character_lowercase_hex() -> None:
    encoded = encode_digest(bytes(range(32)))
    assert len(encoded) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", encoded) is not None


def test_wire_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InvalidRequestDetail.model_validate(
            {"field_path": "body", "rule": RULE_MALFORMED_JSON, "extra": 1}
        )


def test_wire_models_reject_coercible_but_wrong_types() -> None:
    with pytest.raises(ValidationError):
        AuditReceiptBody.model_validate(
            {
                "event_id": "e",
                "chain_kind": "realm",
                "chain_identity": "r",
                "sequence": "7",
                "recorded_at": "t",
                "event_hash": "h",
            }
        )


def test_wire_models_are_frozen() -> None:
    detail = InvalidRequestDetail(field_path="body", rule=RULE_MALFORMED_JSON)
    with pytest.raises(ValidationError):
        detail.field_path = "other"  # type: ignore[misc]


def test_success_envelope_carries_outcome_result_and_both_receipts() -> None:
    envelope = SuccessEnvelope[_StubResult](
        outcome=WireOutcome.REPLAYED,
        result=_StubResult(fact_id="3fa85f64-5717-4562-b3fc-2c963f66afa6"),
        mutation_receipt=MutationReceiptBody(
            mutation_id="11111111-1111-4111-8111-111111111111",
            command_digest="ab" * 32,
        ),
        audit_receipt=AuditReceiptBody(
            event_id="22222222-2222-4222-8222-222222222222",
            chain_kind="realm",
            chain_identity="realm-a",
            sequence=7,
            recorded_at="2026-08-07T09:30:15.123456Z",
            event_hash="cd" * 32,
        ),
    )
    dumped = envelope.model_dump(mode="json")
    assert dumped["outcome"] == "replayed"
    assert dumped["result"] == {"fact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}
    assert dumped["audit_receipt"]["sequence"] == 7


def test_success_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SuccessEnvelope[_StubResult].model_validate(
            {
                "outcome": "committed",
                "result": {"fact_id": "f"},
                "mutation_receipt": {"mutation_id": "m", "command_digest": "d"},
                "audit_receipt": {
                    "event_id": "e",
                    "chain_kind": "realm",
                    "chain_identity": "r",
                    "sequence": 1,
                    "recorded_at": "t",
                    "event_hash": "h",
                },
                "surprise": True,
            }
        )


def test_failure_body_matches_the_middleware_envelope_key_order() -> None:
    envelope = FailureEnvelope(
        failure=FailureBody(
            code="internal_error",
            message="The request could not be completed.",
            retry="never",
            correlation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    )
    dumped = envelope.model_dump(mode="json", exclude_none=True)
    assert list(dumped) == ["failure"]
    assert list(dumped["failure"]) == ["code", "message", "retry", "correlation_id"]


def test_secret_detail_carries_policy_rule_and_field_path_only() -> None:
    detail = SecretRejectedDetail(
        policy="cairn.secret/v1",
        rule="cairn.secret/v1/pem-block",
        field_path="facts[0].body",
    )
    assert list(detail.model_dump(mode="json")) == ["policy", "rule", "field_path"]


def test_invalid_request_detail_carries_field_path_and_rule_only() -> None:
    detail = InvalidRequestDetail(field_path="Idempotency-Key", rule=RULE_INVALID_VALUE)
    assert list(detail.model_dump(mode="json")) == ["field_path", "rule"]
