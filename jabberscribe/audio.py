"""Audio preprocessing via ffmpeg.

The STT endpoint gets one compact mono file: both telephony channels
downmixed (speaker labels are a later extra), resampled to 16 kHz,
loudness-normalized, and encoded as Opus so an hour-long call stays around
15 MB -- well under typical transcription upload limits.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

STT_FILENAME = "stt.ogg"
TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800


class AudioError(RuntimeError):
    """Audio could not be prepared."""


def _run_ffmpeg(args: list[str]) -> None:
    try:
        # ffmpeg writes UTF-8; the Windows locale code page (cp1255, cp437) cannot decode every message.
        proc = subprocess.run(
            args,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out after {_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise AudioError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)}")


def prepare_for_stt(src: Path, work_dir: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Encode `src` as work_dir/stt.ogg for transcription.

    Idempotent: an existing non-empty output is reused, which is what makes the
    stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / STT_FILENAME
    if dest.is_file() and dest.stat().st_size > 0:
        log.debug("reusing existing %s", dest)
        return dest
    partial = dest.with_suffix(dest.suffix + ".part")
    # -f ogg is required, not cosmetic: the .part suffix defeats ffmpeg's
    # extension-based format detection and it refuses to choose a muxer.
    try:
        _run_ffmpeg(
            [
                ffmpeg, "-y", "-nostdin", "-i", str(src),
                "-af", _LOUDNORM, "-ac", "1", "-ar", str(TARGET_RATE),
                "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(partial),
            ]
        )
    except AudioError:
        # A half-written Opus file is still the caller's voice; it must not linger.
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest
