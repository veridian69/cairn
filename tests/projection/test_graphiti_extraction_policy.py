"""The extraction policy Cairn hands to graphiti-core, against the pinned library."""

import asyncio
import inspect
import json
from pathlib import Path
from typing import cast

from graphiti_core import Graphiti
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.nodes import EntityNode
from graphiti_core.prompts import prompt_library
from graphiti_core.utils.maintenance.node_operations import (
    _build_entity_types_context,
    _extract_entity_attributes,
)
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types

from cairn.projection.graphiti_extraction_cache import _types_fingerprint
from cairn.projection.graphiti_extraction_policy import (
    ENTITY_TYPES,
    EXTRACTION_INSTRUCTIONS,
    Person,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/memory_quality"


def test_the_pinned_library_accepts_the_policy_on_both_paths() -> None:
    for method in (Graphiti.add_episode, Graphiti.add_episode_bulk):
        parameters = inspect.signature(method).parameters
        assert "entity_types" in parameters
        assert "custom_extraction_instructions" in parameters
    assert validate_entity_types(ENTITY_TYPES)


def test_the_policy_reaches_the_entity_prompt() -> None:
    """The library must put both the instructions and each type's docstring
    in front of the model; a version that stopped doing so would silently
    return Cairn to the stock prompt."""
    context = {
        "episode_content": "text",
        "episode_timestamp": "2026-01-01T00:00:00+00:00",
        "previous_episodes": [],
        "custom_extraction_instructions": EXTRACTION_INSTRUCTIONS,
        "entity_types": _build_entity_types_context(ENTITY_TYPES),
        "source_description": "",
    }

    prompt = "\n".join(
        str(message.content)
        for message in prompt_library.extract_nodes.extract_text(context)
    )

    assert EXTRACTION_INSTRUCTIONS in prompt
    for name, model in ENTITY_TYPES.items():
        assert name in prompt
        assert json.dumps(model.__doc__)[1:-1] in prompt or str(model.__doc__) in prompt


def test_the_types_carry_no_fields_so_no_attribute_calls_are_made() -> None:
    """A type with fields costs one model call per entity; these must not."""

    class NoCalls:
        async def generate_response(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("attribute extraction called the model")

    node = EntityNode(name="Dana", group_id="g", labels=["Entity", "Person"])
    node.attributes = {"kept": True}

    for model in ENTITY_TYPES.values():
        assert not model.model_fields
        attributes = asyncio.run(
            _extract_entity_attributes(
                cast(LLMClient, NoCalls()), node, None, None, model
            )
        )
        assert attributes == {"kept": True}


def test_a_type_docstring_is_extraction_cache_key_material() -> None:
    class Reworded(Person):
        """Someone."""

    assert _types_fingerprint({"Person": Person}) != _types_fingerprint(
        {"Person": Reworded}
    )
    assert _types_fingerprint(None) == "null"


def test_the_policy_examples_come_from_no_fixture_corpus() -> None:
    """An example lifted from a corpus would flatter every measurement on it."""
    texts = [EXTRACTION_INSTRUCTIONS] + [
        " ".join(str(model.__doc__).split()) for model in ENTITY_TYPES.values()
    ]
    # Every example sits between single quotes, and nothing else does.
    quoted = [phrase for text in texts for phrase in text.split("'")[1::2]]
    assert len(quoted) >= 10
    corpora = " ".join(
        " ".join(path.read_text().lower().split()) for path in FIXTURES.glob("*.json")
    )
    assert [phrase for phrase in quoted if phrase.lower() in corpora] == []
