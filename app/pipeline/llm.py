import json
from pathlib import Path
from typing import Protocol

from app.config import settings

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "extraction_cases.jsonl"


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema_name: str) -> dict: ...


class MockLLM:
    """Returns canned JSON by exact transcript match. No internet, no keys."""

    def __init__(self, path: Path = FIXTURES):
        self.cases: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                self.cases[row["transcript"].strip().lower()] = row["expected"]

    def complete_json(self, system: str, user: str, schema_name: str) -> dict:
        found = self.cases.get(user.strip().lower())
        if found is not None:
            return found
        user_norm = user.strip().lower().rstrip("?.,!")
        if "who owe me pass" in user_norm:
            return {
                "language_detected": "pidgin",
                "transactions": [{
                    "intent": "query",
                    "query": {"type": "top_debtor", "params": {}},
                }],
            }
        return {
            "language_detected": "unknown",
            "transactions": [{
                "intent": "unknown",
                "needs_clarification": "Sorry, I no understand. Abeg talk am again.",
            }],
        }


def get_llm() -> LLMClient:
    if settings.llm_provider == "mock" or settings.demo_mode:
        return MockLLM()
    raise NotImplementedError("Real LLM provider comes later")
