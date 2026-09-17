"""One suggestion translation for REST and MCP."""

from uuid import UUID

from cairn.authority.housekeeping_types import Suggest, SuggestionResult
from cairn.authority.memory_codec import memory_value
from cairn.transports.memory.suggestion_models import (
    SuggestionResultBody,
    SuggestRequest,
)
from cairn.transports.v1.translation import _scope, _uuid


def suggestion_command(request: SuggestRequest) -> tuple[UUID, Suggest]:
    return _uuid(request.expected_instance_id, "expected_instance_id"), Suggest(
        _scope(request.scope, "scope"),
        request.observation,
        tuple(_uuid(identity, "fact_ids") for identity in request.fact_ids),
        request.budget,
        request.limit,
    )


def suggestion_result(result: SuggestionResult) -> SuggestionResultBody:
    return SuggestionResultBody.model_validate(memory_value(result))
