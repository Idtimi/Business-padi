from datetime import date
from pathlib import Path

from app.pipeline.llm import LLMClient
from app.pipeline.schemas import ExtractionResult

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "extract_system.txt"
RETRY_HINT = "\n\nYour last answer was not valid. Return valid JSON only."


class ExtractionFailed(Exception):
    """The AI did not give usable JSON, even after one retry."""


def build_system_prompt(today: date, language: str, known_items, known_customers) -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    try:
        from app.pipeline.numbers import get_number_few_shots
        few_shots = get_number_few_shots(language, limit=10)
        if few_shots:
            text += f"\n\nNumber words reference for {language}:\n{few_shots}"
    except Exception:
        pass
    # plain replace (not .format) because the prompt is full of { } from the JSON shape
    return (text.replace("{today}", today.isoformat())
                .replace("{language}", language)
                .replace("{known_items}", ", ".join(known_items) or "none yet")
                .replace("{known_customers}", ", ".join(known_customers) or "none yet"))


def extract(llm: LLMClient, transcript: str, language: str = "pcm",
            known_items=(), known_customers=(), today: date | None = None) -> ExtractionResult:
    system = build_system_prompt(today or date.today(), language, known_items, known_customers)
    for attempt in range(2):
        prompt = system if attempt == 0 else system + RETRY_HINT
        try:
            raw = llm.complete_json(prompt, transcript, "extraction")
            return ExtractionResult.model_validate(raw)
        except (ValueError, TypeError):  # bad JSON or wrong shape (pydantic errors are ValueErrors)
            continue
    raise ExtractionFailed("AI returned invalid JSON twice")
