"""The P-59 seam's own proofs.

``OperationOutcome`` is what every scenario asserts against, so what it
refuses to represent is as load-bearing as what it carries: an envelope
missing or misshaping a receipt, or a hit body that is not text, must
not reach a scenario as a well-formed answer. The scenarios exercise the
happy paths in bulk against a surface that behaves; the malformed
answers have no other home, and a harness that took the surface under
test on trust would report its defects as passes.
"""

import pytest
from conftest import bodies, hits
from transport import OperationOutcome

MUTATION_RECEIPT: dict[str, object] = {
    "mutation_id": "22222222-2222-4222-8222-222222222222",
    "command_digest": "a" * 64,
}

AUDIT_RECEIPT: dict[str, object] = {
    "event_id": "55555555-5555-4555-8555-555555555555",
    "chain_kind": "realm",
    "chain_identity": "acme",
    "sequence": 1,
    "recorded_at": "2026-08-11T12:00:00+00:00",
    "event_hash": "b" * 64,
}

COMMITTED: dict[str, object] = {
    "outcome": "committed",
    "result": {"fact_ids": ["11111111-1111-4111-8111-111111111111"]},
    "mutation_receipt": MUTATION_RECEIPT,
    "audit_receipt": AUDIT_RECEIPT,
}

DENIED: dict[str, object] = {
    "failure": {
        "code": "authorisation_denied",
        "correlation_id": "33333333-3333-4333-8333-333333333333",
    }
}


def test_a_mutation_envelope_yields_both_receipts() -> None:
    outcome = OperationOutcome.from_document(COMMITTED, mutation=True)

    assert outcome.succeeded
    assert outcome.outcome == "committed"
    assert outcome.result == COMMITTED["result"]
    assert outcome.mutation_receipt == MUTATION_RECEIPT
    assert outcome.audit_receipt == AUDIT_RECEIPT
    assert outcome.failure is None
    assert outcome.failure_code is None


@pytest.mark.parametrize(
    "missing", ["outcome", "result", "mutation_receipt", "audit_receipt"]
)
def test_an_incomplete_mutation_envelope_is_refused(missing: str) -> None:
    """I-72's success envelope is closed. A response missing any of its
    four members is a defect in the surface under test, and the seam says
    so rather than handing a scenario an outcome with a hole in it."""
    document = {key: value for key, value in COMMITTED.items() if key != missing}

    with pytest.raises(AssertionError):
        OperationOutcome.from_document(document, mutation=True)


def test_an_envelope_carrying_more_than_i72_fixes_is_refused() -> None:
    """The envelope forbids extras on the wire, so an extra member is a
    contract breach the harness must not absorb."""
    document = {**COMMITTED, "note": "not part of the envelope"}

    with pytest.raises(AssertionError):
        OperationOutcome.from_document(document, mutation=True)


@pytest.mark.parametrize("receipt", ["mutation_receipt", "audit_receipt"])
@pytest.mark.parametrize("shape", ["empty", "partial", "extra"])
def test_a_receipt_that_is_not_its_whole_i72_shape_is_refused(
    receipt: str, shape: str
) -> None:
    """Present-and-a-dict is the check that lets an empty receipt through,
    and an empty receipt is exactly what a surface that forgot to build
    one returns. Membership is fixed, so the seam asserts membership."""
    complete = COMMITTED[receipt]
    assert isinstance(complete, dict)
    member = sorted(complete)[0]
    replacements: dict[str, dict[str, object]] = {
        "empty": {},
        "partial": {key: value for key, value in complete.items() if key != member},
        "extra": {**complete, "surplus": "not part of the receipt"},
    }
    document = {**COMMITTED, receipt: replacements[shape]}

    with pytest.raises(AssertionError):
        OperationOutcome.from_document(document, mutation=True)


def test_a_hit_body_that_is_not_text_fails_the_scenario() -> None:
    """The retrieval scenarios compare hit bodies against expected text.
    Coercing the value with ``str()`` would let a wrongly typed body
    stringify into agreement with what the scenario expected, so the
    helper asserts the type instead — and this is what would go red if
    the coercion came back."""
    document: dict[str, object] = {"hits": [{"body": 42}], "budget_consumed": 0}
    outcome = OperationOutcome.from_document(document, mutation=False)

    assert hits(outcome) == [{"body": 42}]
    with pytest.raises(AssertionError):
        bodies(outcome)


def test_a_read_answers_flat() -> None:
    document: dict[str, object] = {"hits": [], "budget_consumed": 0}

    outcome = OperationOutcome.from_document(document, mutation=False)

    assert outcome.succeeded
    assert outcome.outcome is None
    assert outcome.result == document
    assert outcome.mutation_receipt is None
    assert outcome.audit_receipt is None


def test_a_failure_carries_its_code_and_no_receipts() -> None:
    outcome = OperationOutcome.from_document(DENIED, mutation=True)

    assert not outcome.succeeded
    assert outcome.failure_code == "authorisation_denied"
    # I-72 licenses no detail on this code, and the seam reports its
    # absence as ``None`` rather than making a scenario probe for a key.
    assert outcome.detail is None
    assert outcome.outcome is None
    assert outcome.result is None
    assert outcome.mutation_receipt is None
    assert outcome.audit_receipt is None


def test_a_licensed_detail_survives_the_seam() -> None:
    detail = {
        "policy": "cairn.secret/v1",
        "rule": "cairn.secret/v1/upstream/AWSKeyDetector",
        "field_path": "facts[0].body",
    }
    document: dict[str, object] = {
        "failure": {
            "code": "secret_rejected",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "detail": detail,
        }
    }

    outcome = OperationOutcome.from_document(document, mutation=True)

    assert outcome.detail == detail


def test_the_disclosed_text_is_the_whole_document() -> None:
    """The absence assertions — a payload, a sibling realm, a secret —
    are made against this, so it must carry everything the caller was
    told, not a summary of it."""
    text = OperationOutcome.from_document(COMMITTED, mutation=True).text

    assert "11111111-1111-4111-8111-111111111111" in text
    assert "22222222-2222-4222-8222-222222222222" in text
    assert "55555555-5555-4555-8555-555555555555" in text
    assert '"chain_kind":"realm"' in text
