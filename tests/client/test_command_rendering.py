"""Bounded, inert command output including frozen client receipt objects."""

import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import uuid4

import pytest


def render(value: object, *, human: bool = False) -> bytes:
    assert importlib.util.find_spec("cairn.client.rendering") is not None, (
        "command renderer absent"
    )
    from cairn.client.rendering import render_result

    return render_result("status", value, human=human)


@pytest.mark.parametrize("human", [False, True])
def test_frozen_nested_values_render_without_repr_or_terminal_commands(
    human: bool,
) -> None:
    identity = uuid4()
    value = MappingProxyType(
        {
            "state": "prepared",
            "identity": identity,
            "response": "\x1b]52;c;payload\x07\n$(touch nope)",
        }
    )
    output = render(value, human=human)
    assert output.endswith(b"\n")
    assert b"\x1b" not in output and b"\x07" not in output
    parsed = json.loads(output)
    assert parsed["schema"] == "cairn.memory-command/v1"
    assert parsed["command"] == "status"
    assert parsed["result"]["state"] == "prepared"
    assert parsed["result"]["identity"] == str(identity)
    assert parsed["result"]["response"] == value["response"]


def test_complete_dataclass_is_rendered_without_inventing_saved_state() -> None:
    @dataclass(frozen=True)
    class Prepared:
        state: str = "prepared"
        custody_receipt: None = None

    parsed = json.loads(render(Prepared()))
    assert parsed["result"] == {"state": "prepared", "custody_receipt": None}


@pytest.mark.parametrize(
    "value",
    [object(), float("nan"), {1: "bad key"}, "\ud800", "x" * 1048577],
    ids=["object", "nan", "key", "unicode", "oversized"],
)
def test_invalid_or_oversized_result_fails_before_any_output(value: object) -> None:
    assert importlib.util.find_spec("cairn.client.rendering") is not None, (
        "command renderer absent"
    )
    from cairn.client.rendering import CommandOutputError

    with pytest.raises(CommandOutputError) as caught:
        render(value)
    assert str(caught.value) in {"invalid_output", "output_too_large"}


def test_cyclic_result_is_a_closed_failure() -> None:
    assert importlib.util.find_spec("cairn.client.rendering") is not None, (
        "command renderer absent"
    )
    from cairn.client.rendering import CommandOutputError

    value: list[object] = []
    value.append(value)
    with pytest.raises(CommandOutputError, match="invalid_output"):
        render(value)


def test_naive_timestamp_is_a_closed_failure() -> None:
    from cairn.client.rendering import CommandOutputError

    with pytest.raises(CommandOutputError, match="invalid_output"):
        render(datetime(2026, 9, 10))


def test_combined_nested_strings_are_bounded() -> None:
    from cairn.client.rendering import CommandOutputError

    with pytest.raises(CommandOutputError, match="output_too_large"):
        render(["x" * 262144] * 5)


def test_scope_matches_its_existing_client_value_representation() -> None:
    from cairn.catalogue.audit import Scope, ScopeSegment

    scope = Scope("cairn", (ScopeSegment("job", "example"),))
    mapping = {
        "realm": "cairn",
        "segments": ({"kind": "job", "identifier": "example"},),
    }
    assert render(scope) == render(mapping)
