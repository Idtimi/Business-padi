"""Language registry and support levels for VoiceLedger.

Support levels:
- untested: Not yet measured; treated like beta until tests are run.
- supported: Passed accuracy tests with real native-speaker notes; normal confirm-before-save.
- beta: Works sometimes; always confirm; amounts re-confirmed on separate line.
- assist: No usable speech recognition yet; trader can type or bot asks to repeat in English/Pidgin.
- planned: Data being collected; chosen via [Other language] button to record demand.
"""

from typing import Any

# Initial levels start as "untested" or "planned";
# Promoted to "supported", "beta", or "assist" based on test evaluation results.
LANGUAGES: dict[str, dict[str, Any]] = {
    "pcm": {
        "name": "Nigerian Pidgin",
        "asr": "cloud",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "supported",  # Primary supported language in pilot
        "ui_reviewed": True,
        "numbers_file": "numbers_pcm.txt",
    },
    "en": {
        "name": "English",
        "asr": "cloud",
        "asr_model": None,
        "tts": "yarngpt",
        "tts_voice": "idera",
        "level": "supported",  # Primary supported language in pilot
        "ui_reviewed": True,
        "numbers_file": "numbers_en.txt",
    },
    "yo": {
        "name": "Yoruba",
        "asr": "cloud",
        "asr_model": None,
        "tts": "yarngpt",
        "tts_voice": "yoruba_female2",
        "level": "untested",
        "ui_reviewed": False,
        "numbers_file": "numbers_yo.txt",
    },
    "ha": {
        "name": "Hausa",
        "asr": "cloud",
        "asr_model": None,
        "tts": "yarngpt",
        "tts_voice": "hausa_female2",
        "level": "untested",
        "ui_reviewed": False,
        "numbers_file": "numbers_ha.txt",
    },
    "ig": {
        "name": "Igbo",
        "asr": "cloud",
        "asr_model": None,
        "tts": "yarngpt",
        "tts_voice": "igbo_female2",
        "level": "untested",
        "ui_reviewed": False,
        "numbers_file": "numbers_ig.txt",
    },
    # Planned languages (demand tracked via [Other language])
    "efi": {
        "name": "Efik / Ibibio",
        "asr": "none",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "planned",
        "ui_reviewed": False,
        "numbers_file": "numbers_efi.txt",
    },
    "tiv": {
        "name": "Tiv",
        "asr": "none",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "planned",
        "ui_reviewed": False,
        "numbers_file": "numbers_tiv.txt",
    },
    "ful": {
        "name": "Fulfulde",
        "asr": "none",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "planned",
        "ui_reviewed": False,
        "numbers_file": "numbers_ful.txt",
    },
    "bin": {
        "name": "Edo (Bini)",
        "asr": "none",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "planned",
        "ui_reviewed": False,
        "numbers_file": "numbers_bin.txt",
    },
    "kan": {
        "name": "Kanuri",
        "asr": "none",
        "asr_model": None,
        "tts": "none",
        "tts_voice": None,
        "level": "planned",
        "ui_reviewed": False,
        "numbers_file": "numbers_kan.txt",
    },
}


def get_language(code: str) -> dict[str, Any]:
    """Retrieve language configuration, defaulting to English if unknown."""
    return LANGUAGES.get(code, LANGUAGES["en"])


def get_support_level(code: str) -> str:
    """Return support level: supported, beta, untested, assist, or planned."""
    lang = LANGUAGES.get(code)
    if not lang:
        return "planned"
    return lang.get("level", "untested")


def is_supported(code: str) -> bool:
    """Return True if language has passed native accuracy tests."""
    return get_support_level(code) == "supported"


def is_beta_or_untested(code: str) -> bool:
    """Return True if language works but requires strict re-confirmation of amounts."""
    level = get_support_level(code)
    return level in ("beta", "untested")


def is_assist_or_planned(code: str) -> bool:
    """Return True if speech recognition is not ready and fallback is needed."""
    level = get_support_level(code)
    return level in ("assist", "planned")


def resolve_ui_language(requested_code: str) -> str:
    """Resolve UI language, falling back to English if the requested language is unreviewed."""
    lang = LANGUAGES.get(requested_code)
    if not lang or not lang.get("ui_reviewed", False):
        return "en"
    return requested_code
