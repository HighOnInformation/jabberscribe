"""Audio preprocessing via ffmpeg.

Whisper wants 16 kHz mono. Telephony gives 8 kHz, often stereo with the near
and far end on separate channels -- which is a gift, because splitting those
channels yields exact speaker attribution with no model involved.

Approach ported from CallSight's pipeline/audio/preprocessor.py.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800


class AudioError(RuntimeError):
    """Audio could not be prepared. Treated as a permanent failure."""


@dataclass(frozen=True)
class Track:
    label: str
    path: Path


def _run_ffmpeg(args: list[str]) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out after {_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise AudioError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)}")


def _convert(src: Path, dest: Path, pan: str | None, ffmpeg: str) -> None:
    """Convert one (optionally panned) channel to 16 kHz mono PCM."""
    if dest.is_file() and dest.stat().st_size > 0:
        log.debug("reusing existing %s", dest)
        return
    filters = [f for f in (pan, _LOUDNORM) if f]
    partial = dest.with_suffix(dest.suffix + ".part")
    args = [ffmpeg, "-y", "-nostdin", "-i", str(src)]
    if filters:
        args += ["-af", ",".join(filters)]
    # -f wav is required, not cosmetic: the .part suffix defeats ffmpeg's
    # extension-based format detection and it refuses to choose a muxer.
    args += ["-ac", "1", "-ar", str(TARGET_RATE), "-c:a", "pcm_s16le", "-f", "wav", str(partial)]
    _run_ffmpeg(args)
    partial.replace(dest)


def prepare(src: Path, work_dir: Path, tracks: str, ffmpeg: str = "ffmpeg") -> tuple[Track, ...]:
    """Normalize `src` into 16 kHz mono track(s) inside `work_dir`.

    Idempotent: an existing non-empty output is reused, which is what makes the
    audio stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    work_dir.mkdir(parents=True, exist_ok=True)

    if tracks == "mixed":
        dest = work_dir / f"{src.stem}.mixed.wav"
        _convert(src, dest, None, ffmpeg)
        return (Track("mixed", dest),)

    if tracks == "dual":
        result: list[Track] = []
        for label, channel in (("near", 0), ("far", 1)):
            dest = work_dir / f"{src.stem}.{label}.wav"
            _convert(src, dest, f"pan=mono|c0=c{channel}", ffmpeg)
            result.append(Track(label, dest))
        return tuple(result)

    raise AudioError(f"unsupported tracks value: {tracks!r}")
