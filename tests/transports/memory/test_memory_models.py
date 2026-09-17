import pytest

from cairn.transports.memory.models import RememberRequest
from cairn.transports.memory.translation import remember_command
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.translation import validated


def test_remember_forces_authenticated_candidate_claim() -> None:
    command = remember_command(
        validated(
            RememberRequest,
            {
                "scope": {"realm": "acme", "segments": []},
                "classification": "internal",
                "facts": [{"body": "A durable observation"}],
            },
        )
    )
    assert command.source_type.value == "agent-claim"
    assert command.requested_trust.value == "candidate"


@pytest.mark.parametrize(
    "field", ["requested_trust", "source_type", "source_principal_id"]
)
def test_model_cannot_choose_authority(field: str) -> None:
    with pytest.raises(WireRejection):
        validated(
            RememberRequest,
            {
                "scope": {"realm": "acme", "segments": []},
                "classification": "internal",
                "facts": [{"body": "An observation"}],
                field: "validated",
            },
        )


def test_budget_schema_publishes_the_runtime_numeric_limits() -> None:
    from cairn.authority.retrieval import MAX_BUDGET_BYTES
    from cairn.transports.memory.models import HistoryRequest, RecallRequest

    for model in (HistoryRequest, RecallRequest):
        schema = model.model_json_schema()["properties"]["budget"]
        assert schema["minimum"] == 1
        assert schema["maximum"] == MAX_BUDGET_BYTES
        assert str(MAX_BUDGET_BYTES) in schema["description"]


def test_disagreement_context_flags_are_required_in_the_wire_contract() -> None:
    from cairn.transports.memory.models import MemoryFactBody

    schema = MemoryFactBody.model_json_schema()
    for field in ("has_disagreement", "disagreement_context_incomplete"):
        assert field in schema["required"]
        assert "default" not in schema["properties"][field]
