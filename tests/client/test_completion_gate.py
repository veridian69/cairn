"""The host, not model prose, bounds completion and repair."""

from dataclasses import replace
from pathlib import Path

import pytest

from cairn.client import completion_gate as gate
from cairn.client.chat_host import ChatActor, ChatHostError, ChatTurn
from cairn.client.turn_receipts import TurnMemory


def actor() -> ChatActor:
    from uuid import uuid4

    return ChatActor(
        "claude",
        Path("/bin/true"),
        Path("/unused"),
        Path("/unused"),
        uuid4(),
        "haiku",
        Path("/unused"),
    )


def turn(text: str, status: str = "none") -> ChatTurn:
    return ChatTurn(text, TurnMemory(status))  # type: ignore[arg-type]


def install(
    monkeypatch: pytest.MonkeyPatch, results: list[ChatTurn | Exception]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        gate,
        "assessment_context",
        lambda actor, task: (
            '{"source":"cairn-memory/v1","content_role":"untrusted-data","binding":{},"data":{"budget_exhausted":false,"semantic_degraded":false,"hits":[]}}'
        ),
    )

    def run(selected: ChatActor, task: bytes, **kwargs: object) -> ChatTurn:
        calls.append({"task": task, "model": selected.model, **kwargs})
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(gate, "_run_once", run)
    return calls


def test_required_assessment_for_no_write_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(
        monkeypatch, [turn("Answer"), turn('{"status":"complete","issues":[]}')]
    )
    result = gate.run_checked_turn(actor(), b"What are we doing?")
    assert result.completion == "complete"
    assert result.memory.status == "none"
    assert len(calls) == 2 and calls[1]["read_only"] is True
    assert calls[1]["trusted_assessment_context"] is True
    assert calls[1]["assessment_context"] is not None
    assert "host_assessment_context" in str(calls[1]["workflow"])
    assert '"hits":[]' not in str(calls[1]["workflow"])


def test_host_context_failure_stops_before_an_unobserved_assessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(monkeypatch, [turn("Answer")])

    def unavailable(actor: ChatActor, task: bytes) -> str:
        raise ChatHostError("host_assessment_context_unavailable")

    monkeypatch.setattr(gate, "assessment_context", unavailable)
    result = gate.run_checked_turn(actor(), b"How are you?")
    assert result.completion == "incomplete"
    assert result.completion_reason == "host_assessment_context_unavailable"
    assert len(calls) == 1


def test_one_repair_preserves_original_source_and_checks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(
        monkeypatch,
        [
            turn("Old priority"),
            turn('{"status":"repair","issues":["Current priority omitted"]}'),
            turn("Saved new priority", "verified"),
            turn('{"status":"complete","issues":[]}'),
        ],
    )
    task = "Mum first.\r\nCafé!".encode()
    result = gate.run_checked_turn(actor(), task)
    assert result.completion == "complete" and result.repaired
    assert result.response == "Saved new priority"
    assert len(calls) == 4
    assert all(call["task"] == task for call in calls)
    assert len({call["source_id"] for call in calls}) == 1
    assert "Current priority omitted" in str(calls[2]["workflow"])
    assert calls[2]["read_only"] is False


@pytest.mark.parametrize("status", ["partial", "unknown"])
def test_uncertain_write_never_repaired(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    calls = install(monkeypatch, [turn("Possibly saved", status)])
    result = gate.run_checked_turn(actor(), b"Remember")
    assert result.completion == "incomplete" and len(calls) == 1


@pytest.mark.parametrize(
    "verdict",
    [
        "oops",
        '{"status":"complete","issues":["missing"]}',
        '{"status":"complete","issues":[],"extra":true}',
        '{"status":"complete","status":"repair","issues":[]}',
        '{"status":"unresolved","issues":["Ambiguous subject"]}',
    ],
)
def test_invalid_or_unresolved_assessment_stops_without_repair(
    monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    calls = install(monkeypatch, [turn("Answer"), turn(verdict)])
    result = gate.run_checked_turn(actor(), b"She needs help")
    assert result.completion == "incomplete" and len(calls) == 2


def test_repair_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = '{"status":"repair","issues":["Missing priority"]}'
    calls = install(
        monkeypatch, [turn("Answer"), turn(failed), turn("Attempted"), turn(failed)]
    )
    result = gate.run_checked_turn(actor(), b"Mum first")
    assert result.completion == "incomplete" and result.repaired
    assert len(calls) == 4


def test_assessor_failure_preserves_verified_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = replace(
        turn("Saved", "verified"), memory=TurnMemory("verified", ("known-id",), 1)
    )
    calls = install(monkeypatch, [initial, ChatHostError("host_usage_limit")])
    result = gate.run_checked_turn(actor(), b"Fact")
    assert result.memory == initial.memory
    assert result.completion == "incomplete"
    assert result.completion_reason == "host_usage_limit"
    assert len(calls) == 2


def test_repair_error_aggregates_known_ids_and_marks_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = replace(
        turn("Saved", "verified"), memory=TurnMemory("verified", ("old",), 1)
    )
    error = ChatHostError(
        "host_timeout",
        {"memory": {"status": "partial", "fact_ids": ["new"], "attempted": 1}},
    )
    install(
        monkeypatch, [initial, turn('{"status":"repair","issues":["Missing"]}'), error]
    )
    result = gate.run_checked_turn(actor(), b"Fact")
    assert result.memory.status in {"partial", "unknown"}
    assert result.memory.fact_ids == ("old", "new")
    assert result.completion == "incomplete"


def test_shared_deadline_prevents_late_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([0.0, 0.0, 361.0])
    monkeypatch.setattr(
        "cairn.client.completion_gate.time.monotonic", lambda: next(ticks)
    )
    calls = install(monkeypatch, [turn("Answer")])
    result = gate.run_checked_turn(actor(), b"Question")
    assert len(calls) == 1
    assert result.completion_reason == "completion_timeout"


def test_uncertain_repair_stops_before_reassessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(
        monkeypatch,
        [
            turn("Answer"),
            turn('{"status":"repair","issues":["Missing"]}'),
            turn("Uncertain", "partial"),
        ],
    )
    result = gate.run_checked_turn(actor(), b"Fact")
    assert len(calls) == 3 and result.completion_reason == "uncertain_write"


def test_initial_provider_failure_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(monkeypatch, [ChatHostError("host_authentication_failed")])
    with pytest.raises(ChatHostError, match="host_authentication_failed"):
        gate.run_checked_turn(actor(), b"Fact")
    assert len(calls) == 1


def test_assessment_model_only_overrides_assessment_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(
        monkeypatch,
        [
            turn("Answer"),
            turn('{"status":"repair","issues":["Missing"]}'),
            turn("Repaired", "verified"),
            turn('{"status":"complete","issues":[]}'),
        ],
    )
    result = gate.run_checked_turn(replace(actor(), assessment_model="sonnet"), b"Fact")
    assert result.completion == "complete"
    assert [call["model"] for call in calls] == ["haiku", "sonnet", "haiku", "sonnet"]


def test_unresolved_assessment_exposes_bounded_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        [
            turn("Answer"),
            turn(
                '{"status":"unresolved","issues":["Which person does she refer to?"]}'
            ),
        ],
    )
    result = gate.run_checked_turn(actor(), b"She is doing it")
    assert result.completion_issues == ("Which person does she refer to?",)
