import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.real_work_demo import agents, prompts  # noqa: E402

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_one_prompt_per_turn() -> None:
    assert set(prompts.PROMPTS) == {t.prefix for t in agents.TURNS}


def test_no_ids_are_handed_to_agents() -> None:
    for text in prompts.PROMPTS.values():
        assert not UUID.search(text)


def test_cold_prompt_mentions_neither_garden_nor_history() -> None:
    cold = prompts.PROMPTS["t7-spike-cold"].lower()
    assert "garden" not in cold and "val" not in cold and "message" not in cold


def test_no_prompt_scripts_the_dead_end_or_the_dispute() -> None:
    joined = " ".join(prompts.PROMPTS.values()).lower()
    for leak in ("numeric path", "exclude datetime", "string flag", "ignore_string"):
        assert leak not in joined


def test_every_prompt_names_the_demo_scope() -> None:
    for text in prompts.PROMPTS.values():
        assert (
            '{"realm":"acme","segments":[{"kind":"repository","identifier":"deepdiff"}]}'
            in text
        )
        assert "classification internal" in text


def test_verifier_is_told_to_promote_the_authors_fix_not_its_own_facts() -> None:
    t4 = prompts.PROMPTS["t4-verifier"]
    assert "fact its author recorded" in t4
    assert "never facts you recorded yourself" in t4


def test_spike_is_asked_to_record_an_insufficient_colleague_attempt() -> None:
    t2 = prompts.PROMPTS["t2-spike"]
    assert "If a colleague's attempt turns out insufficient" in t2
    assert "failed approach, naming its commit" in t2


def test_the_correction_prompt_names_neither_the_counterexample_nor_the_remedy() -> (
    None
):
    t5 = prompts.PROMPTS["t5-spike-correction"]
    assert (
        "declined to validate your fix" in t5 and "tell `verifier` through Garden" in t5
    )
    for leak in ("timezone", "tz", "aware", "counterexample", "none path", "utc"):
        assert leak not in t5.lower()


def test_the_recheck_prompt_is_t4_plus_a_second_look() -> None:
    t6 = prompts.PROMPTS["t6-verifier-recheck"]
    assert t6 == prompts.PROMPTS["t4-verifier"] + (
        " This is your second look: judge the latest fix fact its author recorded."
    )
