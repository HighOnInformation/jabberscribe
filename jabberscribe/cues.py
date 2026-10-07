"""Bracket cues: non-speech audio events as a separate layer.

A local audio-event tagger (PANNs Cnn14, trained on AudioSet) scores the
recording in two-second windows on the CPU. Selected AudioSet classes become
Hebrew cues -- [צחוק], [מוזיקה], ... -- with start and end times. They stay
apart from the strict-verbatim transcript: result.json carries them as
`cues`, and transcript_cues.md shows them interleaved for reading.
transcript.md is never touched. Intonation is out of reach of this approach.

The tagger needs PyTorch and a 330 MB checkpoint, so it is the optional extra
`pip install .[cues]`, imported only when a tagger is built. Without the extra,
the checkpoint or the AudioSet label file -- or with cues disabled --
load_tagger returns None, says why in the log, and the pipeline skips the stage.
Nothing is ever downloaded: the checkpoint and the label file are placed by hand.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jabberscribe.config import CuesConfig

log = logging.getLogger(__name__)

LAUGHTER = "[צחוק]"
NOISE = "[רעש רקע]"
MUSIC = "[מוזיקה]"
SILENCE = "[שקט]"
TYPING = "[הקלדה]"
RINGING = "[צלצול]"

#: AudioSet class names, spelled as in panns_inference.labels, and the cue each one raises. Others are ignored.
AUDIOSET_CUES: dict[str, str] = {
    "Laughter": LAUGHTER,
    "Baby laughter": LAUGHTER,
    "Giggle": LAUGHTER,
    "Snicker": LAUGHTER,
    "Belly laugh": LAUGHTER,
    "Chuckle, chortle": LAUGHTER,
    "Music": MUSIC,
    "Noise": NOISE,
    "Static": NOISE,
    "Environmental noise": NOISE,
    "Hubbub, speech noise, speech babble": NOISE,
    "Silence": SILENCE,
    "Typing": TYPING,
    "Computer keyboard": TYPING,
    "Typewriter": TYPING,
    "Telephone bell ringing": RINGING,
    "Ringtone": RINGING,
}

#: PANNs models are trained on 32 kHz audio.
SAMPLE_RATE = 32000
WINDOW_SECONDS = 2.0
#: Windows per model call: bounded memory, reasonable CPU throughput.
BATCH_WINDOWS = 16
#: Pauses are normal in conversation; only a long one deserves a cue.
MIN_SILENCE_SECONDS = 6.0
#: panns_inference reads its label list from here when imported, and tries to download it (wget) when missing.
PANNS_LABELS = Path.home() / "panns_data" / "class_labels_indices.csv"
#: panns_inference re-downloads a checkpoint smaller than this instead of loading it.
PANNS_MIN_CHECKPOINT_BYTES = 300_000_000


class CueError(RuntimeError):
    """The recording could not be decoded for tagging."""


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    label: str


class Tagger(Protocol):
    def tag(self, audio: Path) -> list[Cue]: ...


def cues_from_scores(windows: Iterable[dict[str, float]], window_seconds: float, threshold: float) -> list[Cue]:
    """Turn per-window class probabilities into cues.

    Window i covers [i * window_seconds, (i + 1) * window_seconds) and maps AudioSet
    class names to probabilities. A label is present in a window when any of its
    classes reaches `threshold`; consecutive windows with the label merge into one cue.
    """
    open_since: dict[str, float] = {}
    found: list[Cue] = []
    end = 0.0
    for index, scores in enumerate(windows):
        start, end = index * window_seconds, (index + 1) * window_seconds
        present = {AUDIOSET_CUES[name] for name, p in scores.items() if name in AUDIOSET_CUES and p >= threshold}
        for label in [name for name in open_since if name not in present]:
            found.append(Cue(open_since.pop(label), start, label))
        for label in present:
            open_since.setdefault(label, start)
    found += [Cue(began, end, label) for label, began in open_since.items()]
    kept = [c for c in found if c.label != SILENCE or c.end - c.start >= MIN_SILENCE_SECONDS]
    return sorted(kept, key=lambda c: (c.start, c.label))


def read_windows(audio: Path, window_seconds: float = WINDOW_SECONDS, ffmpeg: str = "ffmpeg") -> Iterator[bytes]:
    """Decode `audio` to mono 32 kHz float32 PCM and yield it one window at a time (the last may be short).

    Streaming keeps memory flat: an hour of 32 kHz float audio is 460 MB.
    """
    if not audio.is_file():
        raise CueError(f"audio not found: {audio}")
    size = int(window_seconds * SAMPLE_RATE) * 4
    args = [ffmpeg, "-nostdin", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(SAMPLE_RATE)]
    try:
        proc = subprocess.Popen([*args, "-f", "f32le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise CueError(f"cannot start ffmpeg: {exc}") from exc
    with proc:
        stdout = proc.stdout
        assert stdout is not None
        while chunk := stdout.read(size):
            yield chunk
    if proc.returncode != 0:
        raise CueError(f"ffmpeg exited {proc.returncode} while decoding {audio}")


class PannsTagger:
    """PANNs Cnn14 audio tagging on the CPU. Importing torch happens here, never at module import."""

    def __init__(self, model_path: Path, threshold: float) -> None:
        import numpy
        from panns_inference import AudioTagging, labels

        self._np = numpy
        self._model = AudioTagging(checkpoint_path=str(model_path), device="cpu")
        self._labels: list[str] = list(labels)
        self._threshold = threshold

    def tag(self, audio: Path) -> list[Cue]:
        np = self._np
        full = int(WINDOW_SECONDS * SAMPLE_RATE)
        scores: list[dict[str, float]] = []
        batch: list = []
        for chunk in read_windows(audio):
            samples = np.frombuffer(chunk, dtype=np.float32)
            batch.append(np.pad(samples, (0, full - len(samples))))
            if len(batch) == BATCH_WINDOWS:
                scores += self._score(np.stack(batch))
                batch = []
        if batch:
            scores += self._score(np.stack(batch))
        return cues_from_scores(scores, WINDOW_SECONDS, self._threshold)

    def _score(self, batch) -> list[dict[str, float]]:
        clipwise, _embedding = self._model.inference(batch)
        return [
            {name: float(p) for name, p in zip(self._labels, row, strict=True) if name in AUDIOSET_CUES}
            for row in clipwise
        ]


def _extra_installed() -> bool:
    return importlib.util.find_spec("panns_inference") is not None


def unavailable_reason(cfg: CuesConfig) -> str | None:
    """Why the tagger cannot run here, or None when it can. Never imports torch."""
    if not cfg.enabled:
        return "disabled in config (cues.enabled: false)"
    if cfg.model_path is None:
        return "no cues.model_path configured"
    if not cfg.model_path.is_file():
        return f"model checkpoint not found: {cfg.model_path}"
    if cfg.model_path.stat().st_size < PANNS_MIN_CHECKPOINT_BYTES:
        return f"checkpoint is smaller than Cnn14_mAP=0.431.pth; panns_inference would download one: {cfg.model_path}"
    if not _extra_installed():
        return "the optional extra is not installed: pip install .[cues]"
    if not PANNS_LABELS.is_file():
        return f"AudioSet label file missing (panns_inference would download it): {PANNS_LABELS}"
    return None


def load_tagger(cfg: CuesConfig) -> Tagger | None:
    """The configured tagger, or None (logged with the reason) when the cues stage must be skipped."""
    reason = unavailable_reason(cfg)
    if reason is not None:
        log.warning("cues stage skipped: %s", reason)
        return None
    try:
        return PannsTagger(cfg.model_path, cfg.threshold)
    except Exception:
        log.exception("cues stage skipped: the tagger failed to load")
        return None
