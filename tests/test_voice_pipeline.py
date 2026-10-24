"""Tests for the voice-note pipeline: audio download, conversion, and ASR routing."""

import asyncio
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.models as m
import app.pipeline.handler as handler_mod
from app.channels.base import IncomingMessage
from app.channels.mock import MockChannel
from app.db import Base
from app.pipeline.asr import MockASR, Transcript
from app.pipeline.audio import AudioTooLong, convert_to_wav, probe_duration
from app.pipeline.handler import handle_incoming


RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def voice_msg(mid="v1", media_ref="MEDIA1", user_ref="2348012345678"):
    return IncomingMessage(
        channel="whatsapp", user_ref=user_ref, message_id=mid,
        kind="voice", text=None, media_ref=media_ref, button_id=None,
        timestamp=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )


def seed_onboarded_user(factory, user_ref="2348012345678"):
    with factory() as s:
        u = m.User(channel="whatsapp", channel_user_id=user_ref, language="en",
                   state="DONE", consent_at=m.now_iso())
        s.add(u)
        s.flush()
        s.add(m.Business(owner_user_id=u.id, name="Shop"))
        s.commit()


class FakeChannel:
    """Channel that returns canned audio bytes and records sent messages."""

    def __init__(self, audio_bytes: bytes = b"fake-audio",
                 content_type: str = "audio/ogg"):
        self._audio = audio_bytes
        self._content_type = content_type
        self.sent: list[dict] = []

    async def download_media(self, media_id: str):
        return self._audio, self._content_type

    async def send_text(self, user_ref, text):
        self.sent.append({"to": user_ref, "type": "text", "text": text, "buttons": []})

    async def send_buttons(self, user_ref, text, buttons):
        self.sent.append({"to": user_ref, "type": "buttons", "text": text,
                          "buttons": list(buttons)})


class SidecarASR:
    """ASR that reads a .txt sidecar file next to the WAV, or returns fixed text."""

    def __init__(self, fixed_text: str | None = None):
        self._fixed = fixed_text

    def transcribe(self, wav_path: str, language_hint=None, glossary=None):
        if self._fixed is not None:
            return Transcript(text=self._fixed, language=language_hint)
        sidecar = Path(wav_path).with_suffix(".txt")
        text = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else ""
        return Transcript(text=text, language=language_hint)


# ---------- Unit tests for audio module ----------

def test_probe_duration_returns_zero_when_no_ffprobe(tmp_path, monkeypatch):
    """When ffprobe is missing, probe_duration should gracefully return 0."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert probe_duration(str(tmp_path / "nonexistent.ogg")) == 0.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_convert_to_wav_with_real_ffmpeg(tmp_path):
    """If ffmpeg is available, make sure conversion doesn't crash on a tiny wav."""
    import struct
    # Create a minimal valid WAV file (44 header bytes + 100 samples of silence)
    samples = b"\x00\x00" * 100
    wav = tmp_path / "test.wav"
    with open(wav, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(samples)))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(samples)))
        f.write(samples)
    out = tmp_path / "out.wav"
    convert_to_wav(str(wav), str(out))
    assert out.exists() and out.stat().st_size > 0


# ---------- Integration tests for voice handler ----------

def _fake_download_factory(transcript_text: str = RICE, call_counter: list | None = None):
    """Create a fake download_and_convert that writes a sidecar transcript."""
    async def fake_download(channel, media_ref):
        if call_counter is not None:
            call_counter.append(1)
        tmp_dir = Path(tempfile.mkdtemp(prefix="vl_test_"))
        wav_path = tmp_dir / "voice.wav"
        wav_path.write_text("placeholder")
        sidecar = tmp_dir / "voice.txt"
        sidecar.write_text(transcript_text, encoding="utf-8")
        return str(wav_path), tmp_dir
    return fake_download


def test_voice_note_transcribed_and_routed(factory, monkeypatch):
    """Voice note -> download -> ASR(RICE) -> confirm card with N127,500."""
    monkeypatch.setattr(handler_mod, "download_and_convert", _fake_download_factory(RICE))
    seed_onboarded_user(factory)

    channel = FakeChannel()
    asr = SidecarASR()

    with factory() as s:
        msg = voice_msg()
        asyncio.run(handle_incoming(channel, msg, s, asr=asr))
        # Should have sent a confirm card
        assert len(channel.sent) == 1
        assert channel.sent[0]["type"] == "buttons"
        assert "N127,500" in channel.sent[0]["text"]


def test_voice_too_long_returns_friendly_message(factory, monkeypatch):
    """When the audio exceeds max_voice_seconds, return a friendly error."""
    async def fake_download_too_long(channel, media_ref):
        raise AudioTooLong("Voice note is 120s, max is 90s")

    monkeypatch.setattr(handler_mod, "download_and_convert", fake_download_too_long)

    channel = FakeChannel()
    asr = SidecarASR()

    with factory() as s:
        msg = voice_msg()
        asyncio.run(handle_incoming(channel, msg, s, asr=asr))
        assert len(channel.sent) == 1
        assert "too long" in channel.sent[0]["text"].lower()


def test_voice_duplicate_is_ignored(factory, monkeypatch):
    """Same voice message ID delivered twice should only be processed once."""
    counter: list = []
    monkeypatch.setattr(handler_mod, "download_and_convert",
                        _fake_download_factory(RICE, call_counter=counter))
    seed_onboarded_user(factory)

    channel = FakeChannel()
    asr = SidecarASR()

    with factory() as s:
        msg = voice_msg(mid="v-dup")
        asyncio.run(handle_incoming(channel, msg, s, asr=asr))
        # Second delivery of same message
        asyncio.run(handle_incoming(channel, msg, s, asr=asr))
        # download_and_convert should only be called once
        assert len(counter) == 1
        # Only one outbound message
        assert len(channel.sent) == 1


def test_empty_transcript_returns_error(factory, monkeypatch):
    """When ASR returns empty text, send a try_again message."""
    monkeypatch.setattr(handler_mod, "download_and_convert",
                        _fake_download_factory(""))
    seed_onboarded_user(factory)

    channel = FakeChannel()
    asr = SidecarASR(fixed_text="")  # empty transcript

    with factory() as s:
        msg = voice_msg()
        asyncio.run(handle_incoming(channel, msg, s, asr=asr))
        assert len(channel.sent) == 1
        assert channel.sent[0]["type"] == "text"
