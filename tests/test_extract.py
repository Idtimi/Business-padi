from datetime import date

import pytest

from app.pipeline.extract import ExtractionFailed, build_system_prompt, extract
from app.pipeline.llm import MockLLM

GOOD = {"language_detected": "english",
        "transactions": [{"intent": "expense", "amount": 5000, "category": "fuel"}]}


class FlakyLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.systems = []

    def complete_json(self, system, user, schema_name):
        self.systems.append(system)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_extract_with_mock_llm():
    text = "Today I buy fuel for generator, five thousand naira."
    result = extract(MockLLM(), text)
    assert result.transactions[0].intent == "expense"


def test_retries_once_on_bad_json():
    llm = FlakyLLM([{"transactions": "oops"}, GOOD])
    result = extract(llm, "anything")
    assert result.transactions[0].amount == 5000
    assert len(llm.systems) == 2
    assert "Return valid JSON only." in llm.systems[1]
    assert "Return valid JSON only." not in llm.systems[0]


def test_gives_up_after_second_failure():
    llm = FlakyLLM([ValueError("bad"), {"transactions": "x"}])
    with pytest.raises(ExtractionFailed):
        extract(llm, "anything")
    assert len(llm.systems) == 2


def test_prompt_placeholders_filled():
    p = build_system_prompt(date(2026, 10, 23), "pcm", [], ["Mama Ngozi"])
    assert "2026-10-23" in p and "Mama Ngozi" in p
    assert "{today}" not in p and "{known_customers}" not in p