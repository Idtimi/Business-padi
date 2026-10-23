"""Voice-note audio processing.

Downloads voice notes from the channel, checks duration, converts to 16 kHz
mono WAV for the ASR engine, and cleans up temp files.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


class AudioTooLong(Exception):
    """The voice note exceeds the configured max_voice_seconds."""


class AudioConversionError(Exception):
    """ffmpeg failed or is not installed."""


def _find_ffmpeg() -> str:
    """Return the path to ffmpeg, or raise AudioConversionError."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise AudioConversionError(
            "ffmpeg is not installed or not on PATH. "
            "Install it to process voice notes."
        )
    return path


def probe_duration(file_path: str) -> float:
    """Return the duration of an audio file in seconds using ffprobe.

    Falls back to 0.0 if ffprobe is unavailable (lets the pipeline continue
    and rely on ASR to reject overly long files).
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        log.warning("ffprobe not found; skipping duration check")
        return 0.0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("ffprobe duration check failed for %s", file_path, exc_info=True)
        return 0.0


def convert_to_wav(input_path: str, output_path: str) -> None:
    """Convert any audio file to 16 kHz mono WAV using ffmpeg."""
    ffmpeg = _find_ffmpeg()
    cmd = [
        ffmpeg, "-y",            # overwrite output
        "-i", input_path,
        "-ar", "16000",          # 16 kHz sample rate
        "-ac", "1",              # mono
        "-sample_fmt", "s16",    # 16-bit signed PCM
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise AudioConversionError(f"ffmpeg timed out converting {input_path}") from exc
    if proc.returncode != 0:
        raise AudioConversionError(f"ffmpeg exited {proc.returncode}: {proc.stderr[:500]}")


async def download_and_convert(channel, media_ref: str) -> tuple[str, Path]:
    """Download a voice note and convert it to WAV.

    Returns (transcript_wav_path, temp_dir).
    The caller MUST delete temp_dir when done.

    Raises AudioTooLong if the note exceeds settings.max_voice_seconds.
    Raises AudioConversionError if ffmpeg is missing or fails.
    """
    content, content_type = await channel.download_media(media_ref)

    # Pick a sensible extension from the MIME type
    ext = ".ogg"
    if "mpeg" in content_type:
        ext = ".mp3"
    elif "wav" in content_type:
        ext = ".wav"
    elif "mp4" in content_type or "m4a" in content_type:
        ext = ".m4a"

    tmp_dir = Path(tempfile.mkdtemp(prefix="vl_voice_"))
    raw_path = tmp_dir / f"raw{ext}"
    wav_path = tmp_dir / "voice.wav"

    try:
        raw_path.write_bytes(content)

        # Duration check BEFORE the expensive conversion
        duration = probe_duration(str(raw_path))
        if duration > settings.max_voice_seconds:
            raise AudioTooLong(
                f"Voice note is {duration:.0f}s, max is {settings.max_voice_seconds}s"
            )

        # If the input is already a WAV, skip conversion
        if ext == ".wav":
            wav_path = raw_path
        else:
            convert_to_wav(str(raw_path), str(wav_path))

        return str(wav_path), tmp_dir
    except Exception:
        # Clean up on any error
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
