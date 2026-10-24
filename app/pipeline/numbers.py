"""Per-language number-word parsing and few-shot examples for extraction prompts."""

import os
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _clean_key(text: str) -> str:
    """Normalize text: lowercase, collapse whitespace, strip accents/hyphens."""
    text = text.lower().replace("-", " ").strip()
    return re.sub(r"\s+", " ", text)


def load_number_dictionary(lang: str) -> dict[str, int]:
    """Loads word->int mappings from app/prompts/numbers_<lang>.txt."""
    file_path = PROMPTS_DIR / f"numbers_{lang}.txt"
    if not file_path.exists():
        # Fall back to English
        file_path = PROMPTS_DIR / "numbers_en.txt"
        if not file_path.exists():
            return {}

    mapping: dict[str, int] = {}
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "->" in line:
                parts = line.split("->", 1)
                word = _clean_key(parts[0])
                try:
                    val = int(parts[1].strip())
                    mapping[word] = val
                except ValueError:
                    continue
    return mapping


# Cache loaded mappings
_MAPPINGS: dict[str, dict[str, int]] = {}


def parse_number_word(phrase: str, lang: str = "en") -> int | None:
    """Parses a local language number phrase to its integer value."""
    if lang not in _MAPPINGS:
        _MAPPINGS[lang] = load_number_dictionary(lang)
    mapping = _MAPPINGS[lang]

    cleaned = _clean_key(phrase)
    if cleaned in mapping:
        return mapping[cleaned]

    # Handle direct digit strings like "20000" or "20k"
    digit_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*k\b", cleaned)
    if digit_match:
        try:
            return int(float(digit_match.group(1)) * 1000)
        except ValueError:
            pass

    if cleaned.isdigit():
        return int(cleaned)

    return None


def get_number_few_shots(lang: str, limit: int = 15) -> str:
    """Formats few-shot examples for inclusion in LLM extraction prompts."""
    mapping = load_number_dictionary(lang)
    if not mapping:
        mapping = load_number_dictionary("en")

    lines = [f"{word} = {val}" for word, val in list(mapping.items())[:limit]]
    return "\n".join(lines)
