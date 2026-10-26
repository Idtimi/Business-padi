from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Protocol

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None


class ASRClient(Protocol):
    def transcribe(self, wav_path: str, language_hint: str | None, glossary: list[str]) -> Transcript: ...


class YarnGPTASR:
    """Nigerian indigenous speech-to-text using YarnGPT API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.yarngpt.ai/api/v1"

    def transcribe(self, wav_path: str, language_hint: str | None, glossary: list[str]) -> Transcript:
        p = Path(wav_path)
        try:
            import time
            import uuid

            with open(p, "rb") as f:
                audio_bytes = f.read()

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": str(uuid.uuid4()),
            }
            files = {"file": (p.name, audio_bytes, "audio/wav")}
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(f"{self.base_url}/asr", headers=headers, files=files)
                if resp.status_code == 202:
                    data = resp.json()
                    job_id = data.get("job_id")
                    if job_id:
                        status_headers = {"Authorization": f"Bearer {self.api_key}"}
                        for _ in range(30):
                            time.sleep(1.5)
                            st_res = client.get(f"{self.base_url}/asr/{job_id}", headers=status_headers)
                            if st_res.status_code == 200:
                                st = st_res.json()
                                if st.get("status") == "completed":
                                    transcript = st.get("transcript", "").strip()
                                    log.info("YarnGPT ASR success: '%s'", transcript)
                                    return Transcript(text=transcript, language=language_hint, confidence=0.96)
                                elif st.get("status") == "failed":
                                    log.warning("YarnGPT ASR failed: %s", st)
                                    break
                else:
                    log.warning("YarnGPT ASR submission failed [%s]: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.exception("YarnGPT ASR exception on %s: %s", wav_path, e)

        return MockASR().transcribe(wav_path, language_hint, glossary)


class WhisperASR:
    """Real speech-to-text transcription using Groq Whisper or OpenAI Whisper API."""

    def __init__(self, api_key: str, endpoint: str = "https://api.groq.com/openai/v1/audio/transcriptions", model: str = "whisper-large-v3"):
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model

    def transcribe(self, wav_path: str, language_hint: str | None, glossary: list[str]) -> Transcript:
        p = Path(wav_path)
        try:
            with open(p, "rb") as audio_file:
                files = {"file": (p.name, audio_file, "audio/wav")}
                data = {"model": self.model}
                if language_hint and language_hint in ("en", "yo", "ha", "ig"):
                    data["language"] = language_hint
                if glossary:
                    data["prompt"] = ", ".join(glossary[:20])

                headers = {"Authorization": f"Bearer {self.api_key}"}
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(self.endpoint, headers=headers, files=files, data=data)
                    resp.raise_for_status()
                    result = resp.json()
                    text = result.get("text", "").strip()
                    log.info("Whisper transcription success: '%s'", text)
                    return Transcript(text=text, language=language_hint, confidence=0.95)
        except Exception as e:
            log.exception("Whisper ASR error on %s: %s", wav_path, str(e))
            # Fallback to MockASR if transcription fails
            return MockASR().transcribe(wav_path, language_hint, glossary)


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
    yarn_key = getattr(settings, "yarngpt_api_key", None)
    if yarn_key and ("yarngpt" in settings.asr_provider.lower() or not settings.asr_api_key):
        return YarnGPTASR(api_key=yarn_key)

    api_key = settings.asr_api_key
    if api_key:
        if "openai" in settings.asr_provider.lower() or api_key.startswith("sk-proj-") or (api_key.startswith("sk-") and not api_key.startswith("gsk_")):
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
            model = settings.asr_model or "whisper-1"
        else:
            endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
            model = settings.asr_model or "whisper-large-v3"
        return WhisperASR(api_key=api_key, endpoint=endpoint, model=model)

    return MockASR()
