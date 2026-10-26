from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import settings


@dataclass
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None


class ASRClient(Protocol):
    def transcribe(self, wav_path: str, language_hint: str | None, glossary: list[str]) -> Transcript: ...


class MockASR:
    """Reads the text from a sidecar .txt file next to the audio, else uses the filename."""

    def transcribe(self, wav_path: str, language_hint: str | None, glossary: list[str]) -> Transcript:
        p = Path(wav_path)
        sidecar = p.with_suffix(".txt")
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8").strip()
        elif p.stem.lower() in ("voice", "audio", "recording", "input"):
            text = "I sell 2 bag of rice for 80,000 to Emeka and now e dey owe 40k"
        else:
            text = p.stem
        return Transcript(text=text, language=language_hint, confidence=1.0)


def get_asr() -> ASRClient:
    if settings.asr_provider == "mock" or settings.demo_mode:
        return MockASR()
    raise NotImplementedError("Real ASR provider comes later")
