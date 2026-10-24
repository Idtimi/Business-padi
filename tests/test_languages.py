"""Tests for Phase 15: Multi-language support per Part E7."""

import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app import models as m
from app.channels.base import IncomingMessage
from app.i18n import t
from app.pipeline.extract import build_system_prompt
from app.pipeline.glossary import get_glossary, get_glossary_keywords
from app.pipeline.numbers import load_number_dictionary, parse_number_word
from app.pipeline.router import confirmation_text, handle_text
from app.pipeline.tts import TTSClient
from app.services.onboarding import handle_onboarding
from config.languages import (
    LANGUAGES,
    get_language,
    get_support_level,
    is_assist_or_planned,
    is_beta_or_untested,
    is_supported,
    resolve_ui_language,
)


def test_language_registry_keys_and_levels():
    """Verify registry structure and support levels."""
    required_keys = {"name", "asr", "tts", "level", "ui_reviewed", "numbers_file"}
    for code in ("pcm", "en", "yo", "ha", "ig", "efi", "tiv"):
        assert code in LANGUAGES
        entry = LANGUAGES[code]
        assert required_keys.issubset(entry.keys())

    # Supported
    assert is_supported("pcm") is True
    assert is_supported("en") is True

    # Untested / Beta
    assert is_beta_or_untested("yo") is True
    assert is_beta_or_untested("ha") is True
    assert is_beta_or_untested("ig") is True

    # Planned
    assert is_assist_or_planned("efi") is True
    assert is_assist_or_planned("tiv") is True
    assert is_assist_or_planned("unknown_lang") is True


def test_number_word_files_have_30_plus_examples():
    """Verify each supported/beta language has 30+ worked examples."""
    for lang in ("en", "pcm", "yo", "ha", "ig"):
        mapping = load_number_dictionary(lang)
        assert len(mapping) >= 30, f"{lang} has only {len(mapping)} examples, expected >= 30"


def test_number_word_parsing():
    """Verify parsing number words across languages."""
    # English
    assert parse_number_word("five thousand", "en") == 5000
    assert parse_number_word("forty-two thousand five hundred", "en") == 42500
    assert parse_number_word("20k", "en") == 20000

    # Pidgin
    assert parse_number_word("two k", "pcm") == 2000
    assert parse_number_word("fifty thousand", "pcm") == 50000

    # Yoruba
    assert parse_number_word("ookan", "yo") == 1
    assert parse_number_word("eegberun meji", "yo") == 2000
    assert parse_number_word("eegberun ogun", "yo") == 20000

    # Hausa
    assert parse_number_word("dubu biyar", "ha") == 5000
    assert parse_number_word("dubu ashirin", "ha") == 20000

    # Igbo
    assert parse_number_word("puku abuo", "ig") == 2000
    assert parse_number_word("puku iri abuo", "ig") == 20000


def test_i18n_ui_reviewed_fallback():
    """Verify unreviewed languages fall back to English/Pidgin UI strings."""
    # Reviewed: Nigerian Pidgin returns native strings
    assert t("pcm", "saved") == "I don save am."

    # Unreviewed in config (yo, ha, ig have ui_reviewed=False)
    # Should fall back to English
    assert t("yo", "saved") == "Saved."
    assert t("ha", "saved") == "Saved."

    # If reviewed is True, native translation activates immediately
    with patch.dict(LANGUAGES["yo"], {"ui_reviewed": True}):
        assert t("yo", "saved") == "A ti fi pamo."


def test_strict_confirmation_for_non_supported_languages():
    """Verify amounts are isolated on their own line for untested/beta languages."""
    from app.pipeline.validate import Line, Validated
    val = Validated(
        intent="sale",
        customer="Mama Ngozi",
        total_kobo=4250000,
        paid_kobo=4250000,
        lines=[Line(name="rice", qty=3, unit="bag", unit_price_kobo=4250000, line_total_kobo=4250000)],
    )

    # For supported language (pcm or en): normal confirmation without separate warning
    conf_supported = confirmation_text("pcm", val, speech_lang="pcm")
    assert "TOTAL AMOUNT:" not in conf_supported

    # For untested/beta language (yo): isolated total amount line present
    conf_beta = confirmation_text("en", val, speech_lang="yo")
    assert "TOTAL AMOUNT:" in conf_beta
    assert "42,500" in conf_beta
    assert "confirm this amount is exact" in conf_beta or "TOTAL AMOUNT" in conf_beta


def test_other_language_button_records_demand():
    """Verify clicking 'Other language' during onboarding logs demand in events."""
    import uuid
    from datetime import datetime, timezone
    db = SessionLocal()
    uid = f"user_lang_{uuid.uuid4().hex[:8]}"
    try:
        user = m.User(
            channel="mock",
            channel_user_id=uid,
            state="CHOOSE_LANGUAGE",
            language="pcm",
        )
        db.add(user)
        db.commit()

        msg = IncomingMessage(
            channel="mock",
            user_ref=uid,
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            kind="button",
            text=None,
            media_ref=None,
            button_id="lang:other",
            timestamp=datetime.now(timezone.utc),
        )

        import asyncio
        replies = asyncio.run(handle_onboarding(None, msg, user, db))
        assert len(replies) > 0
        assert "another language" in replies[0].text or "other language" in replies[0].text.lower()

        # Check event recorded
        ev = db.scalar(select(m.Event).where(
            m.Event.user_id == user.id,
            m.Event.name == "language_demand",
        ))
        assert ev is not None
        assert ev.props.get("source") == "onboarding_other_button"
    finally:
        db.close()


def test_separate_ui_and_speech_language_commands():
    """Verify user can set UI language and speech language independently."""
    import uuid
    db = SessionLocal()
    uid = f"user_sep_{uuid.uuid4().hex[:8]}"
    try:
        user = m.User(
            channel="mock",
            channel_user_id=uid,
            state="DONE",
            language="pcm",
            state_data={"speech_language": "pcm"},
        )
        db.add(user)
        db.flush()

        biz = m.Business(owner_user_id=user.id, name="Lang Test Biz")
        db.add(biz)
        db.commit()

        # Update UI language to English
        rep1 = handle_text(db, biz.id, user.id, "/language ui en", f"msg_lang_1_{uuid.uuid4().hex[:6]}", lang="pcm")
        assert len(rep1) == 1
        assert "UI language updated to English" in rep1[0].text
        assert user.language == "en"

        # Update Speech language to Yoruba
        rep2 = handle_text(db, biz.id, user.id, "/language speech yo", f"msg_lang_2_{uuid.uuid4().hex[:6]}", lang="en")
        assert len(rep2) == 1
        assert "Speech language set to Yoruba" in rep2[0].text
        assert user.state_data.get("speech_language") == "yo"
        assert user.language == "en"  # UI language unchanged!

        # Set speech language to planned language (tiv)
        rep3 = handle_text(db, biz.id, user.id, "/language speech tiv", f"msg_lang_3_{uuid.uuid4().hex[:6]}", lang="en")
        assert len(rep3) == 1
        assert "Speech language set to Tiv" in rep3[0].text

        # Planned language logged demand
        ev = db.scalar(select(m.Event).where(
            m.Event.user_id == user.id,
            m.Event.name == "language_demand",
        ))
        assert ev is not None
        assert ev.props.get("language") == "tiv"
    finally:
        db.close()


def test_tts_client_caching_and_provider_dispatch():
    """Verify TTSClient caching and YarnGPT voice selection."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        client = TTSClient(provider="mock", cache_dir=tmp_path)

        # First call generates and caches
        audio = client.generate_speech("I don save am.", language="pcm")
        assert audio is not None
        assert audio.startswith(b"OggS")

        cache_files = list(tmp_path.glob("*.ogg"))
        assert len(cache_files) == 1

        # Second call hits disk cache
        with patch.object(client, "_synthesize") as mock_synth:
            audio_cached = client.generate_speech("I don save am.", language="pcm")
            assert audio_cached == audio
            mock_synth.assert_not_called()

        # YarnGPT provider
        yarn_client = TTSClient(provider="yarngpt", cache_dir=tmp_path)
        yarn_audio = yarn_client.generate_speech("Bawo ni", language="yo")
        assert yarn_audio is not None
        assert yarn_audio.startswith(b"OggS")

        # Disabled provider
        none_client = TTSClient(provider="none", cache_dir=tmp_path)
        assert none_client.generate_speech("Hello") is None
