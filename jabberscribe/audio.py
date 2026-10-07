"""Audio preprocessing via ffmpeg.

The STT endpoint gets compact mono files: resampled to 16 kHz,
loudness-normalized, and encoded as Opus so an hour-long call stays around
15 MB -- well under typical transcription upload limits.

A call is sent either as one downmix of all channels (stt.ogg) or, for a
dual-track call with speaker labels on, as one file per channel
(stt-ch0.ogg, stt-ch1.ogg) so each end is transcribed on its own (speakers.py).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

STT_FILENAME = "stt.ogg"
#: Every STT copy a job can leave in its work folder: the downmix, the channel files, and .part leftovers.
STT_GLOB = "stt*.ogg*"
TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800
_MAX_VOLUME = re.compile(r"max_volume: (-?[\d.]+|-inf) dB")


class AudioError(RuntimeError):
    """Audio could not be prepared."""


def channel_filename(channel: int) -> str:
    return f"stt-ch{channel}.ogg"


def _run_ffmpeg(args: list[str]) -> str:
    """Run ffmpeg; return its stderr, where it also reports measurements."""
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
    return proc.stderr or ""


def _encode(src: Path, dest: Path, audio_filter: str, ffmpeg: str) -> Path:
    """Encode `src` through `audio_filter` as mono 16 kHz Opus at `dest`.

    Idempotent: an existing non-empty output is reused, which is what makes the
    stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
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
                "-af", audio_filter, "-ac", "1", "-ar", str(TARGET_RATE),
                "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(partial),
            ]
        )
    except AudioError:
        # A half-written Opus file is still the caller's voice; it must not linger.
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest


def prepare_for_stt(src: Path, work_dir: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Encode all channels of `src`, downmixed, as work_dir/stt.ogg."""
    return _encode(src, work_dir / STT_FILENAME, _LOUDNORM, ffmpeg)


def prepare_channel_for_stt(src: Path, work_dir: Path, channel: int, ffmpeg: str = "ffmpeg") -> Path:
    """Encode one channel of `src` as work_dir/stt-ch<channel>.ogg."""
    return _encode(src, work_dir / channel_filename(channel), f"pan=mono|c0=c{channel},{_LOUDNORM}", ffmpeg)


def channel_count(src: Path, ffprobe: str = "ffprobe") -> int:
    """Number of channels in the first audio stream of `src`."""
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    args = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0"]
    try:
        proc = subprocess.run(
            [*args, str(src)], capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffprobe not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffprobe timed out") from exc
    found = (proc.stdout or "").strip()
    if proc.returncode != 0 or not found.isdigit():
        raise AudioError(f"ffprobe cannot read the channels of {src}: {(proc.stderr or '').strip()[:200]}")
    return int(found)


def channel_peak_db(src: Path, channel: int, ffmpeg: str = "ffmpeg") -> float:
    """Loudest sample of one channel in dBFS, before any normalization. Digital silence is about -91."""
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    stderr = _run_ffmpeg(
        [ffmpeg, "-nostdin", "-i", str(src), "-af", f"pan=mono|c0=c{channel},volumedetect", "-f", "null", "-"]
    )
    match = _MAX_VOLUME.search(stderr)
    if match is None:
        raise AudioError(f"ffmpeg reported no level for channel {channel} of {src}")
    return float(match.group(1))
