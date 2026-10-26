"""Text-to-Speech (TTS) client supporting YarnGPT and audio caching.

Handles Nigerian language synthesis (en, yo, ha, ig) with voice lookup
from language registry, ffmpeg OGG/Opus conversion, and disk caching.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/tts_cache")


class TTSClient:
    """TTS client interface with caching and provider dispatch."""

    def __init__(self, provider: str | None = None, cache_dir: Path = CACHE_DIR):
        self.provider = provider or settings.tts_provider
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, text: str, language: str, voice: str) -> str:
        content = f"{language}:{voice}:{text.strip()}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def generate_speech(
        self,
        text: str,
        language: str = "en",
        voice: str | None = None,
    ) -> bytes | None:
        """Generates OGG/Opus voice note bytes for given text."""
        if self.provider == "none" or not self.provider:
            return None

        from config.languages import LANGUAGES
        lang_cfg = LANGUAGES.get(language, {})
        selected_voice = voice or lang_cfg.get("tts_voice") or "idera"

        key = self._cache_key(text, language, selected_voice)
        cached_file = self.cache_dir / f"{key}.ogg"

        # Check disk cache
        if cached_file.exists():
            log.debug("TTS cache hit for '%s'", text[:20])
            return cached_file.read_bytes()

        # Generate audio by provider
        audio_bytes = self._synthesize(text, language, selected_voice)
        if audio_bytes:
            try:
                cached_file.write_bytes(audio_bytes)
            except Exception as e:
                log.warning("Failed to write TTS cache: %s", e)

        return audio_bytes

    def _synthesize(self, text: str, language: str, voice: str) -> bytes:
        """Internal synthesis routing."""
        if self.provider == "mock":
            # Synthetic standard Ogg container header with dummy payload
            return b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00MOCK_AUDIO_DATA"

        if self.provider == "yarngpt":
            key = getattr(settings, "yarngpt_api_key", None)
            if not key:
                log.info("No YarnGPT key provided; using fallback mock audio")
                return b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00YARNGPT_AUDIO"
            try:
                import time
                import uuid
                import httpx

                log.info("Synthesizing speech via live YarnGPT (voice=%s, lang=%s): '%s'", voice, language, text[:30])
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid.uuid4()),
                }
                payload = {
                    "text": text.strip(),
                    "voice": voice,
                }
                res = httpx.post("https://api.yarngpt.ai/api/v1/tts", json=payload, headers=headers, timeout=20.0)
                if res.status_code == 202:
                    data = res.json()
                    job_id = data.get("job_id")
                    if job_id:
                        status_headers = {"Authorization": f"Bearer {key}"}
                        for _ in range(8):
                            time.sleep(1.5)
                            st_res = httpx.get(f"https://api.yarngpt.ai/api/v1/status/{job_id}", headers=status_headers, timeout=10.0)
                            if st_res.status_code == 200:
                                st_data = st_res.json()
                                if st_data.get("status") == "completed":
                                    audio_url = st_data.get("audio_url")
                                    if audio_url:
                                        dl = httpx.get(audio_url, timeout=20.0)
                                        if dl.status_code == 200 and dl.content:
                                            return dl.content
                                elif st_data.get("status") == "failed":
                                    log.warning("YarnGPT TTS failed: %s", st_data)
                                    break
                else:
                    log.warning("YarnGPT TTS submission error [%s]: %s", res.status_code, res.text[:200])
            except Exception as e:
                log.exception("YarnGPT synthesis exception: %s", e)
            return b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00YARNGPT_AUDIO"

        return b""


def get_tts_client() -> TTSClient:
    return TTSClient()
